"""GUI integration — toolbar button + right-click menu + progress + status dialogs."""

import traceback

try:
    from qt.core import (QIcon, QMenu, QProgressDialog, QPixmap, QMessageBox,
                         QDialog, QVBoxLayout, QLabel, QRadioButton, QButtonGroup,
                         QDialogButtonBox)
except ImportError:
    from PyQt5.Qt import (QIcon, QMenu, QProgressDialog, QPixmap, QMessageBox,
                          QDialog, QVBoxLayout, QLabel, QRadioButton, QButtonGroup,
                          QDialogButtonBox)

from calibre.gui2 import info_dialog, error_dialog, question_dialog
from calibre.gui2.actions import InterfaceAction

from calibre_plugins.mimicreader_send.config import prefs
from calibre_plugins.mimicreader_send.main import send_book, trigger_generation
from calibre_plugins.mimicreader_send.sync import export_library_metadata, sync_library, push_covers
from calibre_plugins.mimicreader_send.poller import PendingPoller
from calibre_plugins.mimicreader_send.update_check import start_background_check

PLUGIN_VERSION = (0, 3, 1)


class MimicReaderAction(InterfaceAction):
    name = 'MimicReader Send'
    action_spec = ('Send to MimicReader', 'images/icon.png',
                   'Upload selected book(s) to MimicReader for audiobook generation', 'Ctrl+Shift+M')
    action_type = 'current'

    def genesis(self):
        """Called once after the GUI has been set up."""
        # Build menu BEFORE touching qaction — matches the KindleUnpack / KFX Input
        # pattern used by long-standing third-party plugins.
        self.menu = QMenu(self.gui)
        self.menu.addAction('Send selected book(s) to MimicReader', self.send_selected_books)
        self.menu.addSeparator()
        self.menu.addAction('Sync library catalog to MimicReader', self.sync_library_catalog)
        self.qaction.setMenu(self.menu)

        # Icon: tolerate missing resources, but never let icon loading break genesis()
        # — a crash here would leave actual_iaction_plugin_loaded=False and hide the
        # toolbar action entirely (Calibre src/calibre/gui2/ui.py init_iaction).
        try:
            icon_resources = self.load_resources(['images/icon.png'])
            data = icon_resources.get('images/icon.png') if icon_resources else None
            if data:
                pix = QPixmap()
                if pix.loadFromData(data):
                    self.qaction.setIcon(QIcon(pix))
        except Exception:
            pass

        self.qaction.triggered.connect(self.send_selected_books)

        # Background worker — long-polls server for pending upload requests
        # (books user clicked in the Calibre tab on mimicreader.ai/app)
        try:
            self._poller = PendingPoller(self.gui, prefs)
            self._poller.start()
        except Exception:
            self._poller = None

        # Daily auto-update check (no-op if checked within last 24h)
        try:
            start_background_check(self.gui, prefs, PLUGIN_VERSION)
        except Exception:
            pass

        # Auto-attach to library list right-click context menu (v0.3.1).
        # Users no longer need Preferences → Toolbars & menus → Context menu
        # to add the action manually.
        self._attach_to_context_menu()

    def _attach_to_context_menu(self):
        """Add this plugin's action to the library list right-click menu.

        Calibre normally requires the user to add plugin actions via
        Preferences → Toolbars & menus → "The context menu for the books in
        the library list". This method does that automatically by appending
        the qaction to library_view.context_menu when it gets built.

        Idempotent: never adds twice. Silent on any error — context menu
        access is best-effort, the toolbar button always works as fallback.
        """
        try:
            view = getattr(self.gui, 'library_view', None)
            if view is None:
                return
            cm = getattr(view, 'context_menu', None)
            if cm is None:
                return
            # Don't add twice on plugin reload / library_changed re-entry.
            for existing in cm.actions():
                if existing is self.qaction:
                    return
            cm.addSeparator()
            cm.addAction(self.qaction)
        except Exception:
            pass

    def library_changed(self, db):
        """Called by Calibre when the user switches/opens a library.
        The context menu is rebuilt then, so we re-attach our action."""
        self._attach_to_context_menu()

    def shutting_down(self):
        """Calibre calls this when the plugin is being unloaded."""
        try:
            if getattr(self, '_poller', None):
                self._poller.shutdown()
        except Exception:
            pass

    def apply_settings(self):
        """Called when config dialog is saved. Nothing to refresh for now."""
        pass

    def send_selected_books(self):
        """Main action: grab selection → loop → POST upload → show summary."""
        if not prefs['api_key']:
            self._prompt_config('Please configure your MimicReader API key first.')
            return
        if not prefs['server_url']:
            self._prompt_config('Please set your MimicReader server URL first.')
            return

        rows = self.gui.library_view.selectionModel().selectedRows()
        if not rows:
            return error_dialog(self.gui, 'No book selected',
                                'Select one or more books in your library first, then try again.',
                                show=True)

        db = self.gui.current_db.new_api
        book_ids = [self.gui.library_view.model().id(r) for r in rows]

        if len(book_ids) > 10:
            confirmed = question_dialog(
                self.gui, f'Send {len(book_ids)} books?',
                f'You are about to upload {len(book_ids)} books to MimicReader. Continue?',
                show_copy_button=False,
            )
            if not confirmed:
                return

        progress = QProgressDialog(
            'Uploading to MimicReader...', 'Cancel', 0, len(book_ids), self.gui
        )
        progress.setWindowTitle('MimicReader Send')
        progress.setMinimumDuration(500)

        uploaded = []
        failed = []

        for i, book_id in enumerate(book_ids):
            if progress.wasCanceled():
                break
            progress.setValue(i)

            try:
                metadata = db.get_metadata(book_id, get_cover=False)
                title = metadata.title or f'Book {book_id}'
                author = ' & '.join(metadata.authors or []) or 'Unknown'

                progress.setLabelText(f'Uploading: {title[:60]}')

                fmt = self._pick_best_format(db, book_id)
                if not fmt:
                    failed.append((title, 'No supported format available'))
                    continue

                content = db.format(book_id, fmt, as_file=False)
                if not content:
                    failed.append((title, f'Format {fmt} empty or unreadable'))
                    continue

                result = send_book(
                    server_url=prefs['server_url'],
                    api_key=prefs['api_key'],
                    title=title,
                    author=author,
                    fmt=fmt,
                    content=content,
                )
                uploaded.append((title, result))

                # Optional: auto-start generation
                if prefs['auto_start_generation'] and result.get('book_id'):
                    try:
                        trigger_generation(
                            server_url=prefs['server_url'],
                            api_key=prefs['api_key'],
                            book_id=result['book_id'],
                            tier=prefs['tier'],
                        )
                    except Exception as gen_err:
                        failed.append((title, f'Uploaded but generation failed: {gen_err}'))

            except Exception as e:
                failed.append((title if 'title' in dir() else f'Book {book_id}', str(e)))

        progress.setValue(len(book_ids))
        self._show_summary(uploaded, failed)

    def sync_library_catalog(self):
        """Push the full library metadata (no files) to MimicReader.

        Currently admin-only on the server side — non-admin users get a clear 403 message.
        """
        if not prefs['api_key']:
            self._prompt_config('Please configure your MimicReader API key first.')
            return
        if not prefs['server_url']:
            self._prompt_config('Please set your MimicReader server URL first.')
            return

        db = self.gui.current_db.new_api
        total_books = len(db.all_book_ids())
        if total_books == 0:
            return info_dialog(self.gui, 'Empty library',
                               'Your Calibre library has no books to sync.', show=True)

        # Library UUID from Calibre metadata.db (stable across devices when copying the library)
        try:
            library_uuid = str(db.library_id)
        except Exception:
            library_uuid = 'default'
        try:
            library_name = str(self.gui.current_db.library_path).rstrip('/\\').rsplit('/', 1)[-1].rsplit('\\', 1)[-1]
        except Exception:
            library_name = 'My library'

        # Incremental sync support: if we already synced this library before, default to delta
        last_syncs = prefs.get('last_sync_per_lib') or {}
        prev_sync_ts = last_syncs.get(library_uuid)
        mode = self._ask_sync_mode(total_books, prev_sync_ts)
        if mode is None:
            return  # user cancelled
        since_ts = prev_sync_ts if mode == 'incremental' else None

        progress = QProgressDialog('Reading Calibre metadata...', 'Cancel', 0, 100, self.gui)
        progress.setWindowTitle('MimicReader — Sync library catalog')
        progress.setMinimumDuration(0)
        progress.setValue(2)

        cancelled = [False]
        sync_started_at = __import__('time').time()
        try:
            # Stage A: read metadata into memory
            books = []
            for i, book in enumerate(export_library_metadata(db, since_ts=since_ts)):
                if progress.wasCanceled():
                    cancelled[0] = True
                    break
                books.append(book)
                if i % 500 == 0:
                    # 2% → 15% while reading metadata
                    pct = 2 + min(13, int(13 * (i + 1) / max(total_books, 1)))
                    progress.setValue(pct)
                    progress.setLabelText('Reading metadata: %d / %d' % (i + 1, total_books))

            if cancelled[0]:
                progress.close()
                return info_dialog(self.gui, 'Cancelled', 'Sync was cancelled.', show=True)

            progress.setValue(15)
            progress.setLabelText('Sending %d books in batches...' % len(books))

            def progress_cb(done_batch, total_batch, msg):
                if progress.wasCanceled():
                    cancelled[0] = True
                    return False
                pct = 15 + int(80 * done_batch / max(total_batch, 1))
                progress.setValue(pct)
                progress.setLabelText(msg)
                return True

            result = sync_library(
                server_url=prefs['server_url'],
                api_key=prefs['api_key'],
                library_uuid=library_uuid,
                library_name=library_name,
                books=books,
                progress_cb=progress_cb,
            )

            progress.setValue(100)

            if cancelled[0]:
                return info_dialog(self.gui, 'Cancelled',
                                   'Upload was cancelled after some batches.\n\n'
                                   'Added so far: %d / Updated: %d' % (result.get('inserted', 0),
                                                                        result.get('updated', 0)),
                                   show=True)

            # Push covers right after metadata — so tiles look nice immediately on mimicreader.ai
            progress.setLabelText('Uploading covers...')
            progress.setRange(0, 100)
            progress.setValue(95)

            def cover_progress(done, total, msg):
                if progress.wasCanceled():
                    return False
                pct = 95 + int(4 * done / max(total, 1))
                progress.setValue(min(99, pct))
                progress.setLabelText(msg)
                return True

            all_cids = [b['calibre_id'] for b in books]
            cover_result = push_covers(
                server_url=prefs['server_url'],
                api_key=prefs['api_key'],
                library_uuid=library_uuid,
                book_ids_with_covers=all_cids,
                db=db,
                progress_cb=cover_progress,
            )

            # Close the progress dialog before showing the summary — otherwise
            # Qt can leave it stuck on screen behind the info dialog.
            progress.setValue(100)
            progress.close()

            # Persist sync timestamp so next run can do incremental
            try:
                last_syncs = dict(prefs.get('last_sync_per_lib') or {})
                last_syncs[library_uuid] = sync_started_at
                prefs['last_sync_per_lib'] = last_syncs
            except Exception:
                pass

            mode_label = 'Incremental sync' if since_ts else 'Full sync'
            info_dialog(
                self.gui, 'Library synced',
                '%s complete.\n\n'
                '• Books in this batch: %d\n'
                '• Added (new): %d\n'
                '• Updated: %d\n'
                '• Errors: %d\n'
                '• Server now has: %d books\n'
                '• Covers uploaded: %d / %d\n\n'
                'Open the Calibre tab in mimicreader.ai/app to browse your library.' % (
                    mode_label,
                    result.get('total', 0),
                    result.get('inserted', 0),
                    result.get('updated', 0),
                    result.get('errors', 0),
                    result.get('total_on_server', 0),
                    cover_result.get('uploaded', 0),
                    cover_result.get('total', 0),
                ),
                show=True,
            )
        except Exception as e:
            progress.close()
            tb = traceback.format_exc()
            error_dialog(self.gui, 'Sync failed',
                         'Error: %s\n\nDetails:\n%s' % (e, tb[:1500]), show=True)

    def _ask_sync_mode(self, total_books, prev_sync_ts):
        """Show a dialog asking Full vs Incremental. Returns 'full', 'incremental', or None (cancel)."""
        if not prev_sync_ts:
            # First sync — only Full makes sense
            confirmed = question_dialog(
                self.gui, 'Sync %d books?' % total_books,
                'This will upload metadata (title, authors, tags, series, language) '
                'for all <b>%d</b> books in your library to MimicReader.\n\n'
                'Files are NOT uploaded — only metadata. You can generate audiobooks '
                'from any book afterwards via mimicreader.ai/app.\n\n'
                'This feature is currently admin-only while we test it.' % total_books,
                show_copy_button=False,
            )
            return 'full' if confirmed else None

        import datetime as _dt
        last_dt = _dt.datetime.fromtimestamp(prev_sync_ts).strftime('%Y-%m-%d %H:%M')

        dlg = QDialog(self.gui)
        dlg.setWindowTitle('Sync library to MimicReader')
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(
            '<b>Choose sync mode</b><br><br>'
            'Library: %d books<br>'
            'Last sync: %s' % (total_books, last_dt)
        ))

        rb_inc = QRadioButton('Incremental — upload only books changed since last sync (fast)')
        rb_full = QRadioButton('Full — re-upload metadata for all %d books (slow)' % total_books)
        rb_inc.setChecked(True)

        group = QButtonGroup(dlg)
        group.addButton(rb_inc)
        group.addButton(rb_full)
        layout.addWidget(rb_inc)
        layout.addWidget(rb_full)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        layout.addWidget(bb)

        if dlg.exec() != QDialog.Accepted:
            return None
        return 'incremental' if rb_inc.isChecked() else 'full'

    def _pick_best_format(self, db, book_id: int) -> str | None:
        """Return the format name Calibre has for this book, preferred first."""
        pref = prefs['preferred_format']
        priorities = [pref] + [f for f in ('EPUB', 'AZW3', 'MOBI', 'PDF', 'FB2', 'TXT') if f != pref]
        available = set(f.upper() for f in (db.formats(book_id) or []))
        for fmt in priorities:
            if fmt.upper() in available:
                return fmt.upper()
        return None

    def _show_summary(self, uploaded: list, failed: list):
        if not uploaded and not failed:
            info_dialog(self.gui, 'Nothing uploaded', 'No books were sent.', show=True)
            return

        lines = []
        if uploaded:
            lines.append(f'Uploaded {len(uploaded)} book(s):')
            for title, _ in uploaded[:10]:
                lines.append(f'  • {title}')
            if len(uploaded) > 10:
                lines.append(f'  … and {len(uploaded) - 10} more')
            lines.append('')
            lines.append('Open mimicreader.ai/app to generate audiobooks.')

        if failed:
            lines.append('')
            lines.append(f'Failed: {len(failed)}')
            for title, err in failed[:10]:
                lines.append(f'  • {title}: {err}')

        dialog_fn = info_dialog if uploaded else error_dialog
        dialog_fn(self.gui, 'MimicReader — upload summary', '\n'.join(lines), show=True)

    def _prompt_config(self, message: str):
        confirmed = question_dialog(
            self.gui, 'MimicReader Send — configure', message + '\n\nOpen configuration now?',
            show_copy_button=False,
        )
        if confirmed:
            self.interface_action_base_plugin.do_user_config(parent=self.gui)


