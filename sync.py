"""Batch metadata export: Calibre DB → POST /api/calibre/sync-catalog (gzipped).

Stage 2a: metadata-only push. Files are not uploaded here — that happens on demand
when the user clicks Generate in MimicReader.
"""

import gzip
import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


BATCH_SIZE = 1000  # books per HTTP request; trades latency for progress granularity
COVER_PUSH_WORKERS = 3  # parallel cover uploads after metadata sync

# Cover thumbnail — do it client-side to save user bandwidth
COVER_MAX_W = 200
COVER_MAX_H = 300
COVER_JPEG_Q = 75


def _shrink_cover(content):
    """Resize + JPEG-recompress a cover before upload. Falls back to raw bytes on error."""
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(content))
        img.thumbnail((COVER_MAX_W, COVER_MAX_H), Image.LANCZOS)
        if img.mode != 'RGB':
            img = img.convert('RGB')
        out = BytesIO()
        img.save(out, format='JPEG', quality=COVER_JPEG_Q, optimize=True)
        return out.getvalue()
    except Exception:
        return content


def export_library_metadata(db, since_ts=None):
    """Yield one metadata dict per book in the current Calibre library.

    Uses Calibre's `db.new_api` to avoid SQLite locking issues with the running app.
    Skips books whose metadata fails to parse — caller gets a count via the result
    dict from sync_library().
    """
    all_ids = list(db.all_book_ids())
    for book_id in all_ids:
        try:
            mi = db.get_metadata(book_id, get_cover=False, get_user_categories=False)

            # Skip unchanged since last sync (incremental mode)
            last_modified = getattr(mi, 'last_modified', None)
            if since_ts and last_modified:
                try:
                    if last_modified.timestamp() <= since_ts:
                        continue
                except Exception:
                    pass

            formats = []
            try:
                formats = [str(f).upper() for f in (db.formats(book_id, verify_formats=False) or [])]
            except Exception:
                pass

            size_bytes = 0
            for fmt in formats:
                try:
                    fm = db.format_metadata(book_id, fmt) or {}
                    s = fm.get('size') or 0
                    if s > size_bytes:
                        size_bytes = int(s)
                except Exception:
                    pass

            yield {
                'calibre_id': int(book_id),
                'title': (mi.title or '').strip(),
                'authors': [str(a).strip() for a in (mi.authors or []) if a],
                'series': (mi.series or None),
                'series_index': mi.series_index if mi.series_index is not None else None,
                'tags': [str(t).strip() for t in (mi.tags or []) if t],
                'language': (mi.language or '').strip() or None,
                'publisher': (mi.publisher or '').strip() or None,
                'pubdate': mi.pubdate.isoformat() if mi.pubdate else None,
                'added_at': mi.timestamp.isoformat() if mi.timestamp else None,
                'rating': int(mi.rating) if mi.rating is not None else None,
                'comments': (mi.comments or '')[:500],
                'formats': formats,
                'size_bytes': size_bytes or None,
                'last_modified_at': last_modified.isoformat() if last_modified else None,
            }
        except Exception:
            # Skip broken rows silently — they're reported as errors by the server
            continue


def sync_library(server_url, api_key, library_uuid, library_name, books, progress_cb=None):
    """POST metadata in batches. `books` is a list (not generator) — we need total upfront.

    progress_cb(done_batches, total_batches, message) — optional, returns False to cancel.
    Returns dict with totals.
    """
    total = len(books)
    if total == 0:
        return {'total': 0, 'inserted': 0, 'updated': 0, 'errors': 0, 'batches': 0}

    batches = [books[i:i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
    total_batches = len(batches)

    result = {
        'total': total,
        'inserted': 0,
        'updated': 0,
        'errors': 0,
        'batches': total_batches,
    }

    for idx, batch in enumerate(batches, start=1):
        if progress_cb:
            keep_going = progress_cb(idx, total_batches, 'Uploading batch %d / %d' % (idx, total_batches))
            if keep_going is False:
                result['cancelled'] = True
                break

        payload = {
            'library_uuid': library_uuid,
            'library_name': library_name,
            'batch_num': idx,
            'total_batches': total_batches,
            'books': batch,
        }
        raw = json.dumps(payload, default=str, ensure_ascii=False).encode('utf-8')
        gzipped = gzip.compress(raw, compresslevel=6)

        url = '%s/api/calibre/sync-catalog' % server_url.rstrip('/')
        req = Request(url, data=gzipped, method='POST')
        req.add_header('Authorization', 'Bearer %s' % api_key)
        req.add_header('Content-Type', 'application/json')
        req.add_header('Content-Encoding', 'gzip')
        req.add_header('User-Agent', 'MimicReader-Calibre-Plugin/0.2')
        req.add_header('X-Source', 'calibre-sync')

        try:
            with urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                result['inserted'] += int(data.get('inserted', 0))
                result['updated'] += int(data.get('updated', 0))
                result['errors'] += int(data.get('errors', 0))
                result['total_on_server'] = int(data.get('total_on_server', 0))
        except HTTPError as e:
            err_body = ''
            try:
                err_body = e.read().decode('utf-8', errors='replace')[:500]
            except Exception:
                pass
            if e.code == 401:
                raise RuntimeError('API key rejected (401). Check it in Preferences → Plugins.')
            if e.code == 403:
                raise RuntimeError('Catalog sync is currently admin-only while we test. '
                                   'It will open to all users soon.')
            if e.code == 413:
                raise RuntimeError('Batch too large (413) — reduce BATCH_SIZE in the plugin.')
            if e.code == 429:
                raise RuntimeError('Rate limit (429): max 5 full syncs per day.')
            raise RuntimeError('HTTP %d on batch %d: %s' % (e.code, idx, err_body or e.reason))
        except URLError as e:
            raise RuntimeError('Network error on batch %d: %s' % (idx, e.reason))

    return result


def _upload_one_cover(cid, cover_bytes, server_url, api_key, library_uuid):
    """Worker — resize + HTTP upload one cover. Returns True on success.
    Retries on 429 (rate limit) with exponential backoff up to 3 times."""
    import uuid as _uuid
    import time as _time
    try:
        shrunk = _shrink_cover(cover_bytes)
    except Exception:
        return False

    boundary = '----MimicCover%s' % _uuid.uuid4().hex
    body = []
    body.append(('--%s\r\n' % boundary).encode())
    body.append(('Content-Disposition: form-data; name="file"; filename="cover_%d.jpg"\r\n' % cid).encode())
    body.append(b'Content-Type: image/jpeg\r\n\r\n')
    body.append(shrunk)
    body.append(('\r\n--%s--\r\n' % boundary).encode())
    payload = b''.join(body)

    url = '%s/api/calibre/cover-upload/%d?library_uuid=%s' % (server_url.rstrip('/'), cid, library_uuid)

    for attempt in range(4):  # initial + 3 retries on 429
        req = Request(url, data=payload, method='POST')
        req.add_header('Authorization', 'Bearer %s' % api_key)
        req.add_header('Content-Type', 'multipart/form-data; boundary=%s' % boundary)
        req.add_header('User-Agent', 'MimicReader-Calibre-Plugin/0.4-covers')
        try:
            with urlopen(req, timeout=30) as resp:
                resp.read()
            return True
        except HTTPError as e:
            if e.code == 429 and attempt < 3:
                # Rate limited — back off (1s, 3s, 7s) and retry
                _time.sleep(1 + 2 * attempt)
                continue
            return False
        except URLError:
            return False
    return False


def push_covers(server_url, api_key, library_uuid, book_ids_with_covers, db, progress_cb=None):
    """Push covers after metadata sync. PURE PYTHON — no Qt. Designed to run from a
    worker QThread so it never touches the GUI directly.

    Reads covers from `db` and uploads them with a 6-worker ThreadPoolExecutor.
    progress_cb(done, total, msg) → False to abort (returns whatever was uploaded).
    Returns: {'uploaded', 'failed', 'skipped', 'total'}.
    """
    import concurrent.futures
    import sys

    book_ids = list(book_ids_with_covers)
    total = len(book_ids)
    uploaded = 0
    failed = 0
    skipped = 0
    cancelled = False
    first_error_logged = False

    PARALLEL = 6
    CHUNK = 24

    def _format_msg(done, total, uploaded, skipped, failed):
        # 'skipped' = books with no cover in Calibre (has_cover=0). NOT an error.
        return 'Covers %d / %d  sent %d  no-cover %d%s' % (
            done, total, uploaded, skipped,
            ('  failed %d' % failed) if failed else '',
        )

    def _emit(done):
        if progress_cb:
            if not progress_cb(done, total, _format_msg(done, total, uploaded, skipped, failed)):
                return False
        return True

    print('[MimicReader] push_covers: starting %d books, parallel=%d chunk=%d' % (
        total, PARALLEL, CHUNK), file=sys.stderr)

    with concurrent.futures.ThreadPoolExecutor(max_workers=PARALLEL) as executor:
        for chunk_start in range(0, total, CHUNK):
            if cancelled:
                break

            chunk_ids = book_ids[chunk_start:chunk_start + CHUNK]

            # 1) Read covers
            chunk_work = []
            for cid in chunk_ids:
                try:
                    cover = db.cover(cid, as_image=False, as_file=False)
                except Exception as e:
                    if not first_error_logged:
                        print('[MimicReader] db.cover() error cid=%d: %s' % (cid, e), file=sys.stderr)
                        first_error_logged = True
                    cover = None
                if cover:
                    chunk_work.append((cid, cover))
                else:
                    skipped += 1

            done_count = uploaded + failed + skipped
            if not _emit(done_count):
                cancelled = True
                break

            if not chunk_work:
                continue

            # 2) Submit + wait this chunk
            futures = [
                executor.submit(_upload_one_cover, cid, cb, server_url, api_key, library_uuid)
                for cid, cb in chunk_work
            ]
            for future in concurrent.futures.as_completed(futures):
                try:
                    ok = future.result()
                except Exception as e:
                    if not first_error_logged:
                        print('[MimicReader] upload worker exception: %s' % e, file=sys.stderr)
                        first_error_logged = True
                    ok = False
                if ok:
                    uploaded += 1
                else:
                    failed += 1

            done_count = uploaded + failed + skipped
            if not _emit(done_count):
                cancelled = True

            if done_count % 1000 < CHUNK:
                print('[MimicReader] %d/%d  sent=%d failed=%d no-cover=%d' % (
                    done_count, total, uploaded, failed, skipped), file=sys.stderr)

    print('[MimicReader] push_covers DONE  sent=%d failed=%d no-cover=%d' % (
        uploaded, failed, skipped), file=sys.stderr)

    return {'uploaded': uploaded, 'failed': failed, 'skipped': skipped, 'total': total}
