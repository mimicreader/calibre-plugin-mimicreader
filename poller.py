"""Background long-poll worker — Stage 2b on-demand file delivery.

Runs in a daemon thread started from MimicReaderAction.genesis(). Loop:
1. GET /api/calibre/pending (long-poll, up to 55s).
2. For each item:
   - kind="file": read best-available format via db.format(), POST to /fulfill-upload/{id}.
   - kind="cover": read cover via db.cover(), POST as JPEG.
3. Back to step 1. On error: backoff 10s before retry.

Thread exits when `stop_event` is set (plugin shutdown).
"""

import json
import logging
import threading
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import quote


POLL_URL_SUFFIX = '/api/calibre/pending'
FULFILL_URL_SUFFIX = '/api/calibre/fulfill-upload/%d'
USER_AGENT = 'MimicReader-Calibre-Plugin/0.2-poller'
ERROR_BACKOFF = 10  # seconds after an unexpected error

log = logging.getLogger('mimicreader.poller')


class PendingPoller(threading.Thread):
    """Long-poll /api/calibre/pending and fulfill each request."""

    def __init__(self, gui, prefs_dict):
        super().__init__(daemon=True, name='MimicReader-poller')
        self.gui = gui
        self.prefs = prefs_dict
        self.stop_event = threading.Event()

    # --- public lifecycle ---

    def shutdown(self):
        self.stop_event.set()

    # --- main loop ---

    def run(self):
        # Startup grace — give Calibre GUI time to finish initializing current_db
        time.sleep(5)
        log.info('Started long-poll worker')
        while not self.stop_event.is_set():
            server_url = (self.prefs.get('server_url') or '').rstrip('/')
            api_key = self.prefs.get('api_key') or ''
            if not server_url or not api_key:
                # Not configured — sit idle, re-check every 30s
                self.stop_event.wait(30)
                continue
            try:
                items = self._fetch_pending(server_url, api_key)
                if items:
                    for item in items:
                        if self.stop_event.is_set():
                            break
                        try:
                            self._fulfill(server_url, api_key, item)
                        except Exception as e:
                            log.warning('Fulfill failed for %s: %s', item, e)
            except Exception as e:
                log.warning('Long-poll error: %s', e)
                self.stop_event.wait(ERROR_BACKOFF)

    # --- HTTP calls ---

    def _fetch_pending(self, server_url, api_key):
        url = '%s%s' % (server_url, POLL_URL_SUFFIX)
        req = Request(url, method='GET')
        req.add_header('Authorization', 'Bearer %s' % api_key)
        req.add_header('User-Agent', USER_AGENT)
        try:
            with urlopen(req, timeout=75) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                return data.get('items') or []
        except HTTPError as e:
            # 401/403 = auth problem → back off longer
            if e.code in (401, 403):
                log.warning('Long-poll auth failed (%d) — sleeping 60s', e.code)
                self.stop_event.wait(60)
                return []
            raise
        except URLError:
            raise

    def _fulfill(self, server_url, api_key, item):
        kind = item.get('kind')
        pending_id = item.get('pending_id')
        calibre_id = item.get('calibre_id')
        if not pending_id or not calibre_id:
            return

        db = None
        try:
            db = self.gui.current_db.new_api
        except Exception:
            log.warning('Calibre DB not ready yet, skipping %s', pending_id)
            return

        if kind == 'cover':
            self._fulfill_cover(server_url, api_key, pending_id, calibre_id, db)
        elif kind == 'audiobook':
            self._fulfill_audiobook(server_url, api_key, pending_id, calibre_id,
                                    item.get('audiobook_id'), db)
        else:
            self._fulfill_file(server_url, api_key, pending_id, calibre_id, db)

    def _fulfill_file(self, server_url, api_key, pending_id, calibre_id, db):
        try:
            mi = db.get_metadata(calibre_id, get_cover=False)
        except Exception as e:
            log.warning('Cannot read metadata for %d: %s', calibre_id, e)
            return

        pref_fmt = (self.prefs.get('preferred_format') or 'EPUB').upper()
        available = set(str(f).upper() for f in (db.formats(calibre_id, verify_formats=False) or []))
        priorities = [pref_fmt] + [f for f in ('EPUB', 'AZW3', 'MOBI', 'FB2', 'PDF', 'TXT') if f != pref_fmt]
        fmt = next((f for f in priorities if f in available), None)
        if not fmt:
            log.warning('No supported format for book %d — skipping', calibre_id)
            return

        content = db.format(calibre_id, fmt, as_file=False)
        if not content:
            log.warning('Empty content for book %d / %s', calibre_id, fmt)
            return

        title = (mi.title or 'Book %d' % calibre_id)[:500]
        author = (' & '.join(mi.authors or []) or 'Unknown')[:500]

        boundary, body = _build_multipart(
            fields={'title': title, 'author': author, 'format': fmt},
            file_field='file',
            filename='%s.%s' % (title.replace('/', '_')[:80], fmt.lower()),
            content=content,
            content_type=_CONTENT_TYPES.get(fmt, 'application/octet-stream'),
        )

        url = '%s%s' % (server_url, FULFILL_URL_SUFFIX % pending_id)
        req = Request(url, data=body, method='POST')
        req.add_header('Authorization', 'Bearer %s' % api_key)
        req.add_header('Content-Type', 'multipart/form-data; boundary=%s' % boundary)
        req.add_header('User-Agent', USER_AGENT)
        with urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            log.info('Fulfilled file %d → book_id=%s', calibre_id, data.get('uploaded_book_id'))

    def _fulfill_cover(self, server_url, api_key, pending_id, calibre_id, db):
        try:
            cover = db.cover(calibre_id, as_image=False, as_file=False)
        except Exception as e:
            log.warning('Cannot read cover for %d: %s', calibre_id, e)
            return
        if not cover:
            log.info('No cover stored for book %d — skipping', calibre_id)
            return

        # Shrink client-side
        from calibre_plugins.mimicreader_send.sync import _shrink_cover
        cover = _shrink_cover(cover)

        boundary, body = _build_multipart(
            fields={},
            file_field='file',
            filename='cover_%d.jpg' % calibre_id,
            content=cover,
            content_type='image/jpeg',
        )
        url = '%s%s' % (server_url, FULFILL_URL_SUFFIX % pending_id)
        req = Request(url, data=body, method='POST')
        req.add_header('Authorization', 'Bearer %s' % api_key)
        req.add_header('Content-Type', 'multipart/form-data; boundary=%s' % boundary)
        req.add_header('User-Agent', USER_AGENT)
        with urlopen(req, timeout=60) as resp:
            resp.read()
        log.info('Fulfilled cover %d', calibre_id)

    def _fulfill_audiobook(self, server_url, api_key, pending_id, calibre_id,
                           audiobook_id, db):
        """Download finished M4A from MimicReader, save as M4B format on the
        matching Calibre book record. Chapter atoms are already inside the file
        (pipeline injects them), so the renamed M4B becomes a proper audiobook."""
        if not audiobook_id:
            log.warning('audiobook task without audiobook_id, skipping')
            return

        # Make sure the book still exists in this Calibre library
        try:
            mi = db.get_metadata(calibre_id, get_cover=False)
            if not mi:
                self._ack_attach(server_url, api_key, pending_id,
                                 error='book_not_found_in_calibre')
                return
        except Exception as e:
            log.warning('Cannot read metadata for %d: %s', calibre_id, e)
            return

        # Download the M4A from the API (it's already encoded with chapter atoms)
        url = '%s/api/audiobooks/%d/download' % (server_url, audiobook_id)
        req = Request(url, method='GET')
        req.add_header('Authorization', 'Bearer %s' % api_key)
        req.add_header('User-Agent', USER_AGENT)
        try:
            with urlopen(req, timeout=300) as resp:
                content = resp.read()
        except Exception as e:
            log.warning('Audiobook download failed: %s', e)
            self._ack_attach(server_url, api_key, pending_id, error='download_failed:%s' % e)
            return

        if not content:
            self._ack_attach(server_url, api_key, pending_id, error='empty_audio')
            return

        # Save to a temp file then attach as M4B (Calibre stores extension =
        # format name; the underlying container is identical to M4A).
        import tempfile, os
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix='.m4b', delete=False) as f:
                f.write(content)
                tmp_path = f.name
            db.add_format(calibre_id, 'M4B', tmp_path, replace=True, run_hooks=True)
            log.info('Attached audiobook %d to calibre book %d (%.1f MB)',
                     audiobook_id, calibre_id, len(content) / 1024 / 1024)
        except Exception as e:
            log.warning('add_format failed: %s', e)
            self._ack_attach(server_url, api_key, pending_id, error='add_format:%s' % e)
            return
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        self._ack_attach(server_url, api_key, pending_id, error=None)

        # Best-effort GUI notification (status bar)
        try:
            from PyQt5.QtCore import QTimer  # type: ignore
            title = (mi.title if mi else 'Audiobook') if mi else 'Audiobook'
            QTimer.singleShot(0, lambda: self.gui.status_bar.show_message(
                'MimicReader: audiobook attached — %s' % title, 5000))
        except Exception:
            pass

    def _ack_attach(self, server_url, api_key, pending_id, error):
        try:
            url = '%s/api/calibre/ack-attach/%d' % (server_url, pending_id)
            if error:
                url += '?error=' + quote(error[:200])
            req = Request(url, method='POST')
            req.add_header('Authorization', 'Bearer %s' % api_key)
            req.add_header('User-Agent', USER_AGENT)
            with urlopen(req, timeout=15) as resp:
                resp.read()
        except Exception as e:
            log.warning('ack-attach failed: %s', e)


# --- helpers ---

_CONTENT_TYPES = {
    'EPUB': 'application/epub+zip',
    'AZW3': 'application/vnd.amazon.ebook',
    'MOBI': 'application/x-mobipocket-ebook',
    'PDF': 'application/pdf',
    'TXT': 'text/plain',
    'FB2': 'application/x-fictionbook+xml',
}


def _build_multipart(fields, file_field, filename, content, content_type):
    import uuid as _uuid
    boundary = '----MimicPoll%s' % _uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(('--%s\r\n' % boundary).encode())
        parts.append(('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode())
        parts.append(('%s\r\n' % value).encode('utf-8'))
    parts.append(('--%s\r\n' % boundary).encode())
    parts.append(
        ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (file_field, filename)).encode('utf-8')
    )
    parts.append(('Content-Type: %s\r\n\r\n' % content_type).encode())
    parts.append(content)
    parts.append(('\r\n--%s--\r\n' % boundary).encode())
    return boundary, b''.join(parts)
