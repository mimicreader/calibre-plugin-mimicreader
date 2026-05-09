"""Periodic version check — fetches latest plugin version from mimicreader.ai
and shows a one-shot info dialog if a newer release is available.

Runs in a background thread on plugin startup. Skips quietly on network errors.
Checks at most once every 24h (cached in prefs.last_update_check_at).
"""

import json
import logging
import threading
import time
from urllib.request import Request, urlopen
from urllib.error import URLError

from calibre.gui2 import info_dialog


VERSION_URL = '%s/static/mimicreader_send_version.txt'
CHECK_INTERVAL_SECONDS = 86400  # 24h
USER_AGENT = 'MimicReader-Calibre-Plugin/0.3-updater'

log = logging.getLogger('mimicreader.updater')


def parse_version(s):
    """'0.3.1' → (0, 3, 1). Returns (0, 0, 0) on garbage input."""
    try:
        parts = [int(p) for p in s.strip().split('.')[:3]]
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts)
    except (ValueError, AttributeError):
        return (0, 0, 0)


def fetch_latest_version(server_url):
    """Returns dict {'version': (x,y,z), 'download_url': str, 'notes': str} or None on failure.

    Server file format (one or more lines):
        0.3.1
        https://mimicreader.ai/static/mimicreader_send.zip
        Optional release notes line(s)...
    """
    url = VERSION_URL % server_url.rstrip('/')
    req = Request(url, method='GET')
    req.add_header('User-Agent', USER_AGENT)
    try:
        with urlopen(req, timeout=10) as resp:
            body = resp.read().decode('utf-8', errors='replace').strip()
    except (URLError, OSError, ValueError) as e:
        log.debug('Version check failed: %s', e)
        return None

    lines = [l.strip() for l in body.splitlines() if l.strip()]
    if not lines:
        return None
    return {
        'version': parse_version(lines[0]),
        'download_url': lines[1] if len(lines) > 1 else 'https://mimicreader.ai/calibre',
        'notes': '\n'.join(lines[2:]) if len(lines) > 2 else '',
    }


def check_for_update(gui, prefs, current_version):
    """Background thread entrypoint. current_version = (major, minor, patch)."""
    try:
        # Throttle — at most one check per 24h
        last_check = prefs.get('last_update_check_at') or 0
        if time.time() - last_check < CHECK_INTERVAL_SECONDS:
            return

        server_url = (prefs.get('server_url') or '').strip()
        if not server_url:
            return

        latest = fetch_latest_version(server_url)
        # Update timestamp regardless of result so we don't hammer on every Calibre restart
        try:
            prefs['last_update_check_at'] = time.time()
        except Exception:
            pass

        if not latest or latest['version'] <= current_version:
            return

        # Newer version available — but only nudge once per discovered version
        already_notified = prefs.get('last_notified_version') or [0, 0, 0]
        if tuple(already_notified) >= latest['version']:
            return

        # Marshal back to GUI thread to show the dialog
        from qt.core import QTimer
        def _show():
            current_str = '.'.join(str(x) for x in current_version)
            new_str = '.'.join(str(x) for x in latest['version'])
            msg = ('A newer version of the MimicReader plugin is available.\n\n'
                   'You have: %s\n'
                   'Latest:  %s\n\n'
                   'Download: %s\n\n'
                   'Open Calibre Preferences → Plugins → "Load plugin from file" '
                   'and pick the new ZIP. Restart Calibre afterwards.' % (
                       current_str, new_str, latest['download_url']))
            if latest.get('notes'):
                msg += '\n\nRelease notes:\n' + latest['notes']
            try:
                info_dialog(gui, 'MimicReader plugin update available', msg, show=True)
                prefs['last_notified_version'] = list(latest['version'])
            except Exception:
                pass

        QTimer.singleShot(0, _show)
    except Exception as e:
        log.debug('Update check raised: %s', e)


def start_background_check(gui, prefs, current_version):
    """Spawn a daemon thread that runs check_for_update(). Non-blocking."""
    t = threading.Thread(
        target=check_for_update,
        args=(gui, prefs, current_version),
        name='MimicReader-update-check',
        daemon=True,
    )
    t.start()
    return t
