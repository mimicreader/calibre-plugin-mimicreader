"""HTTP upload logic — POST /api/library/upload with Bearer API key.

Uses stdlib urllib to avoid bundling extra deps. Calibre's Python env has what we need.
Multipart form encoding is hand-rolled (file + title + author fields).
"""

import json
import uuid


FORMAT_CONTENT_TYPES = {
    'EPUB': 'application/epub+zip',
    'AZW3': 'application/vnd.amazon.ebook',
    'MOBI': 'application/x-mobipocket-ebook',
    'PDF': 'application/pdf',
    'TXT': 'text/plain',
    'FB2': 'application/x-fictionbook+xml',
}


def build_multipart_body(fields: dict, file_field: str, filename: str, content: bytes, content_type: str):
    """Return (body_bytes, boundary). fields is a dict of str->str (added as plain form fields)."""
    boundary = f'----MimicReader{uuid.uuid4().hex}'
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\n'.encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(f'{value}\r\n'.encode('utf-8'))
    parts.append(f'--{boundary}\r\n'.encode())
    parts.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode('utf-8')
    )
    parts.append(f'Content-Type: {content_type}\r\n\r\n'.encode())
    parts.append(content)
    parts.append(f'\r\n--{boundary}--\r\n'.encode())
    return b''.join(parts), boundary


def send_book(server_url: str, api_key: str, title: str, author: str,
              fmt: str, content: bytes) -> dict:
    """Upload a book. Returns parsed JSON response or raises RuntimeError."""
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError

    if not server_url:
        raise RuntimeError('Server URL not configured — open Preferences → Plugins → MimicReader.')
    if not api_key:
        raise RuntimeError('API key not configured — open Preferences → Plugins → MimicReader.')

    content_type = FORMAT_CONTENT_TYPES.get(fmt.upper(), 'application/octet-stream')
    safe_name = ''.join(c if c.isalnum() or c in ' -_.' else '_' for c in (title or 'book'))[:80].strip() or 'book'
    filename = f'{safe_name}.{fmt.lower()}'

    body, boundary = build_multipart_body(
        fields={},  # backend infers title/author from ebook metadata after upload
        file_field='file',
        filename=filename,
        content=content,
        content_type=content_type,
    )

    url = f'{server_url.rstrip("/")}/api/library/upload'
    req = Request(url, data=body, method='POST')
    req.add_header('Authorization', f'Bearer {api_key}')
    req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
    req.add_header('User-Agent', 'MimicReader-Calibre-Plugin/0.1')
    req.add_header('X-Source', 'calibre')

    try:
        with urlopen(req, timeout=120) as resp:
            data = resp.read()
            try:
                return json.loads(data.decode('utf-8'))
            except json.JSONDecodeError:
                raise RuntimeError(f'Server returned non-JSON response (HTTP {resp.status}).')
    except HTTPError as e:
        err_body = ''
        try:
            err_body = e.read().decode('utf-8', errors='replace')[:500]
        except Exception:
            pass
        if e.code == 401:
            raise RuntimeError('API key rejected (401). Check the key in Preferences → Plugins.')
        if e.code == 413:
            raise RuntimeError('File too large (413). MimicReader accepts up to 50 MB per upload.')
        if e.code == 402:
            raise RuntimeError('No credits (402). Top up at mimicreader.ai/pricing.')
        if e.code == 429:
            raise RuntimeError('Rate limit hit (429). Wait a bit and try again.')
        raise RuntimeError(f'HTTP {e.code}: {err_body or e.reason}')
    except URLError as e:
        raise RuntimeError(f'Network error: {e.reason}')


def trigger_generation(server_url: str, api_key: str, book_id: int, tier: str = 'standard',
                       voice: str = '__default__') -> dict:
    """OPTIONAL: fire POST /api/generator/start after upload, if user opted in."""
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError

    payload = json.dumps({
        'book_id': book_id,
        'tier': tier,
        'voice': voice,
        'force': False,
    }).encode('utf-8')

    url = f'{server_url.rstrip("/")}/api/generator/start'
    req = Request(url, data=payload, method='POST')
    req.add_header('Authorization', f'Bearer {api_key}')
    req.add_header('Content-Type', 'application/json')

    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except HTTPError as e:
        err_body = ''
        try:
            err_body = e.read().decode('utf-8', errors='replace')[:500]
        except Exception:
            pass
        if e.code == 402:
            raise RuntimeError('Not enough credits to start generation — top up at mimicreader.ai/pricing.')
        raise RuntimeError(f'Generation start failed (HTTP {e.code}): {err_body or e.reason}')
    except URLError as e:
        raise RuntimeError(f'Network error starting generation: {e.reason}')


def bulk_queue(server_url: str, api_key: str, library_uuid: str, book_ids: list) -> dict:
    """POST /api/calibre/bulk-queue with selected book IDs.

    Server validates which IDs are in the synced catalog and queues uploads.
    The plugin's background poller fulfills each row later (long-poll cycle).
    """
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError

    payload = json.dumps({
        'library_uuid': library_uuid,
        'calibre_ids': list(book_ids),
    }).encode('utf-8')

    url = f'{server_url.rstrip("/")}/api/calibre/bulk-queue'
    req = Request(url, data=payload, method='POST')
    req.add_header('Authorization', f'Bearer {api_key}')
    req.add_header('Content-Type', 'application/json')

    try:
        with urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except HTTPError as e:
        err_body = ''
        try:
            err_body = e.read().decode('utf-8', errors='replace')[:500]
        except Exception:
            pass
        raise RuntimeError(f'Bulk queue failed (HTTP {e.code}): {err_body or e.reason}')
    except URLError as e:
        raise RuntimeError(f'Network error: {e.reason}')
