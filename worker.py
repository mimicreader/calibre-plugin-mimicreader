"""Background sync worker — runs the heavy lifting (metadata read, batch upload,
cover push) in a QThread so the Calibre GUI never freezes. Communicates with the
main thread exclusively via Qt signals/slots — no processEvents() hacks needed.

Owner: ui.py:MimicReaderAction.sync_library_catalog()
"""
try:
    from qt.core import QObject, pyqtSignal, pyqtSlot  # type: ignore
except ImportError:
    from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot  # type: ignore


class SyncWorker(QObject):
    """Lives in a QThread. Reads Calibre metadata + uploads it + pushes covers.
    Emits signals for progress / phase / completion. Cancel via cancel() slot.
    """

    # done, total, label
    progress = pyqtSignal(int, int, str)
    # 'metadata' | 'sync' | 'covers'
    phase_changed = pyqtSignal(str)
    # final result dict with keys: cancelled, sync_result, cover_result, books_count
    finished = pyqtSignal(object)
    # exception trace string
    error_occurred = pyqtSignal(str)

    def __init__(self, server_url, api_key, library_uuid, library_name,
                 db, since_ts, total_books):
        super().__init__()
        self.server_url = server_url
        self.api_key = api_key
        self.library_uuid = library_uuid
        self.library_name = library_name
        self.db = db
        self.since_ts = since_ts
        self.total_books = total_books
        self._cancelled = False

    @pyqtSlot()
    def cancel(self):
        """Called from the main thread when the user clicks Cancel.
        Just flips a bool — long-running loops poll this between iterations."""
        self._cancelled = True

    @pyqtSlot()
    def run(self):
        """Entry point. Connect to QThread.started before calling QThread.start()."""
        try:
            # Imports done lazily so we don't initialize anything at moduleimport time
            from calibre_plugins.mimicreader_send.sync import (
                export_library_metadata, sync_library, push_covers,
            )

            # ---- PHASE 1: read metadata from Calibre db ----
            self.phase_changed.emit('metadata')
            books = []
            for i, book in enumerate(export_library_metadata(self.db, since_ts=self.since_ts)):
                if self._cancelled:
                    self.finished.emit({'cancelled': True, 'phase': 'metadata',
                                        'books_count': len(books)})
                    return
                books.append(book)
                if i % 50 == 0:
                    self.progress.emit(i + 1, max(self.total_books, 1),
                                       'Reading metadata: %d / %d' % (i + 1, self.total_books))

            self.progress.emit(len(books), max(len(books), 1),
                               'Read %d books' % len(books))

            if self._cancelled or not books:
                self.finished.emit({'cancelled': self._cancelled,
                                    'books_count': len(books)})
                return

            # ---- PHASE 2: send metadata batches to server ----
            self.phase_changed.emit('sync')

            def _sync_cb(done, total, msg):
                if self._cancelled:
                    return False
                self.progress.emit(done, max(total, 1), msg)
                return True

            sync_result = sync_library(
                server_url=self.server_url,
                api_key=self.api_key,
                library_uuid=self.library_uuid,
                library_name=self.library_name,
                books=books,
                progress_cb=_sync_cb,
            )

            if self._cancelled:
                self.finished.emit({'cancelled': True, 'sync_result': sync_result,
                                    'books_count': len(books)})
                return

            # ---- PHASE 3: push covers ----
            self.phase_changed.emit('covers')

            def _cover_cb(done, total, msg):
                if self._cancelled:
                    return False
                self.progress.emit(done, max(total, 1), msg)
                return True

            all_cids = [b['calibre_id'] for b in books]
            cover_result = push_covers(
                server_url=self.server_url,
                api_key=self.api_key,
                library_uuid=self.library_uuid,
                book_ids_with_covers=all_cids,
                db=self.db,
                progress_cb=_cover_cb,
            )

            self.finished.emit({
                'cancelled': self._cancelled,
                'sync_result': sync_result,
                'cover_result': cover_result,
                'books_count': len(books),
            })

        except Exception as exc:
            import traceback
            self.error_occurred.emit('%s\n\n%s' % (exc, traceback.format_exc()[:1500]))
