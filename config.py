"""Configuration: server URL + API key + preferred format.

Stored via Calibre's JSONConfig under ~/.config/calibre/plugins/mimicreader_send.json.
Secrets (API key) are plaintext on disk — same trust model as Calibre Content Server password.
"""

from calibre.utils.config import JSONConfig

try:
    from qt.core import (
        QWidget, QLineEdit, QFormLayout, QLabel, QComboBox, QCheckBox, QVBoxLayout,
        QPushButton, QFrame, QMessageBox, QScrollArea, Qt
    )
except ImportError:
    from PyQt5.Qt import (
        QWidget, QLineEdit, QFormLayout, QLabel, QComboBox, QCheckBox, QVBoxLayout,
        QPushButton, QFrame, QMessageBox, QScrollArea, Qt
    )


prefs = JSONConfig('plugins/mimicreader_send')
prefs.defaults['server_url'] = 'https://mimicreader.ai'
prefs.defaults['api_key'] = ''
prefs.defaults['preferred_format'] = 'EPUB'
prefs.defaults['auto_start_generation'] = False  # future: trigger generator directly
prefs.defaults['tier'] = 'standard'              # future: user picks tier
prefs.defaults['last_sync_per_lib'] = {}         # {library_uuid: epoch_seconds} — for incremental sync
prefs.defaults['last_update_check_at'] = 0       # epoch — throttles auto-update check to 1×/day
prefs.defaults['last_notified_version'] = [0, 0, 0]  # avoid pestering after the user dismissed once
prefs.defaults['install_wizard_done'] = False    # first-run dialog: where to show the plugin


class ConfigWidget(QWidget):
    def __init__(self):
        super().__init__()

        # Outer layout — holds the scroll area so the whole config fits on
        # screens of any size. Without this, the Calibre Customize-plugin
        # dialog squashes content when it can't grow vertically.
        outer = QVBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        self.setLayout(outer)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)

        inner = QWidget()
        scroll.setWidget(inner)
        layout = QVBoxLayout(inner)

        header = QLabel(
            '<b>MimicReader</b><br>'
            'Upload books from your Calibre library directly to MimicReader for audiobook generation.'
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        form = QFormLayout()
        layout.addLayout(form)

        self.server_url_input = QLineEdit(self)
        self.server_url_input.setText(prefs['server_url'])
        self.server_url_input.setPlaceholderText('https://mimicreader.ai')
        form.addRow(QLabel('Server URL:'), self.server_url_input)

        self.api_key_input = QLineEdit(self)
        self.api_key_input.setText(prefs['api_key'])
        self.api_key_input.setEchoMode(QLineEdit.Password)
        self.api_key_input.setPlaceholderText('mk_live_...')
        form.addRow(QLabel('API key:'), self.api_key_input)

        self.format_combo = QComboBox(self)
        self.format_combo.addItems(['EPUB', 'AZW3', 'MOBI', 'PDF', 'TXT', 'FB2'])
        current_idx = self.format_combo.findText(prefs['preferred_format'])
        self.format_combo.setCurrentIndex(max(0, current_idx))
        form.addRow(QLabel('Preferred format:'), self.format_combo)

        self.tier_combo = QComboBox(self)
        self.tier_combo.addItems(['standard', 'premium'])
        tier_idx = self.tier_combo.findText(prefs['tier'])
        self.tier_combo.setCurrentIndex(max(0, tier_idx))
        form.addRow(QLabel('Default tier:'), self.tier_combo)

        self.auto_start_cb = QCheckBox('Automatically start audiobook generation after upload')
        self.auto_start_cb.setChecked(prefs['auto_start_generation'])
        layout.addWidget(self.auto_start_cb)

        # --- Account / credits panel (loaded async after dialog opens) ---
        self.account_label = QLabel('<i>Loading account info…</i>')
        self.account_label.setOpenExternalLinks(True)
        self.account_label.setWordWrap(True)
        self.account_label.setStyleSheet(
            'QLabel { padding: 8px; border: 1px solid palette(mid); border-radius: 4px; }'
        )
        layout.addWidget(self.account_label)
        # Refresh once Qt event loop is idle so the dialog renders first
        try:
            from PyQt5.QtCore import QTimer  # type: ignore
        except ImportError:
            from PyQt6.QtCore import QTimer  # type: ignore
        QTimer.singleShot(50, self._refresh_account_info)

        help_text = QLabel(
            '<small>Get an API key at '
            '<a href="https://mimicreader.ai/dashboard/api-keys">mimicreader.ai/dashboard/api-keys</a>. '
            'One upload consumes credits only when you actually start generating audio.</small>'
        )
        help_text.setOpenExternalLinks(True)
        help_text.setWordWrap(True)
        layout.addWidget(help_text)

        # --- Danger zone: server-side data wipe ---
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        layout.addWidget(sep)

        danger_label = QLabel('<b style="color:#c0392b">Danger zone</b>')
        layout.addWidget(danger_label)

        self.btn_wipe_server = QPushButton('Delete my Calibre data on MimicReader')
        self.btn_wipe_server.setStyleSheet(
            'QPushButton { color: #c0392b; padding: 6px 12px; }'
            'QPushButton:hover { background-color: rgba(192, 57, 43, 0.1); }'
        )
        self.btn_wipe_server.clicked.connect(self._on_wipe_server_data)
        layout.addWidget(self.btn_wipe_server)

        wipe_help = QLabel(
            '<small>Removes your synced Calibre catalog, covers, and pending '
            'uploads from the MimicReader server. Your generated audiobooks '
            'and Calibre library on this computer are <b>not</b> affected.</small>'
        )
        wipe_help.setWordWrap(True)
        layout.addWidget(wipe_help)

        layout.addStretch(1)

    def _on_wipe_server_data(self):
        """Confirm + DELETE /api/calibre/library on the MimicReader server."""
        api_key = (self.api_key_input.text() or '').strip()
        server_url = (self.server_url_input.text() or '').strip().rstrip('/')
        if not api_key or not server_url:
            QMessageBox.warning(self, 'MimicReader',
                                'Set Server URL and API key first.')
            return

        confirm = QMessageBox.question(
            self,
            'Delete Calibre data on MimicReader?',
            'This will remove your synced Calibre catalog, covers, and pending '
            'uploads from the MimicReader server.\n\n'
            'Your generated audiobooks and your local Calibre library are '
            'NOT affected.\n\n'
            'Continue?',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        self.btn_wipe_server.setEnabled(False)
        self.btn_wipe_server.setText('Deleting…')
        try:
            from PyQt5.QtCore import QTimer  # type: ignore
        except ImportError:
            from PyQt6.QtCore import QTimer  # type: ignore

        import threading
        def _bg():
            result = _delete_calibre_library(server_url, api_key)
            QTimer.singleShot(0, lambda: self._on_wipe_done(result))
        threading.Thread(target=_bg, daemon=True).start()

    def _on_wipe_done(self, result):
        self.btn_wipe_server.setEnabled(True)
        self.btn_wipe_server.setText('Delete my Calibre data on MimicReader')
        if not result or result.get('error'):
            err = (result or {}).get('error', 'unknown error')
            QMessageBox.critical(self, 'MimicReader',
                                 'Could not delete: %s' % err)
            return
        QMessageBox.information(
            self,
            'MimicReader — done',
            'Deleted on the server:\n'
            '  • %d catalog books\n'
            '  • %d covers\n'
            '  • %d pending uploads' % (
                result.get('books_deleted', 0),
                result.get('covers_deleted', 0),
                result.get('pending_deleted', 0),
            ),
        )

    def _refresh_account_info(self):
        """Fetch /api/auth/me + /api/credits/balance in a thread, show in label."""
        api_key = (self.api_key_input.text() or '').strip()
        server_url = (self.server_url_input.text() or '').strip().rstrip('/')
        if not api_key or not server_url:
            self.account_label.setText(
                '<small>Set Server URL and API key above, then re-open this dialog '
                'to see your free tier balance.</small>'
            )
            return

        import threading
        def _bg():
            info = _fetch_account_info(server_url, api_key)
            try:
                from PyQt5.QtCore import QTimer  # type: ignore
            except ImportError:
                from PyQt6.QtCore import QTimer  # type: ignore
            QTimer.singleShot(0, lambda: self._render_account_info(info, server_url))
        threading.Thread(target=_bg, daemon=True).start()

    def _render_account_info(self, info, server_url):
        if not info or info.get('error'):
            err = (info or {}).get('error', 'unknown')
            self.account_label.setText(
                '<small><b>Account:</b> could not load (%s).<br/>'
                'Check your Server URL and API key.</small>' % err
            )
            return
        email = info.get('email') or '—'
        balance = info.get('balance')
        total_purchased = info.get('total_purchased')

        bal_str = '%.2f kr' % balance if isinstance(balance, (int, float)) else '—'

        spent_line = ''
        if isinstance(total_purchased, (int, float)) and total_purchased > 0:
            spent_line = '<br/><small>Total purchased: £%.2f</small>' % total_purchased

        self.account_label.setText(
            '<b>Account:</b> %s<br/>'
            '<b>Credits:</b> %s%s<br/>'
            '<small><a href="%s/pricing">Buy credits</a> · '
            '<a href="%s/dashboard/api-keys">API keys</a></small>' % (
                email, bal_str, spent_line, server_url, server_url,
            )
        )

    def save_settings(self):
        prefs['server_url'] = (self.server_url_input.text() or '').strip().rstrip('/')
        prefs['api_key'] = (self.api_key_input.text() or '').strip()
        prefs['preferred_format'] = self.format_combo.currentText()
        prefs['tier'] = self.tier_combo.currentText()
        prefs['auto_start_generation'] = self.auto_start_cb.isChecked()


def _delete_calibre_library(server_url: str, api_key: str) -> dict:
    """Calls DELETE /api/calibre/library to wipe the user's catalog + covers
    + pending uploads on the MimicReader server. Returns response dict or
    {'error': str}."""
    import json as _json
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError

    req = Request(server_url + '/api/calibre/library', method='DELETE')
    req.add_header('Authorization', 'Bearer ' + api_key)
    req.add_header('User-Agent', 'MimicReader-Calibre-Plugin/0.4-config')
    try:
        with urlopen(req, timeout=30) as resp:
            return _json.loads(resp.read().decode('utf-8'))
    except HTTPError as e:
        return {'error': 'HTTP %d' % e.code}
    except URLError as e:
        return {'error': 'network: %s' % e.reason}
    except Exception as e:
        return {'error': str(e)[:120]}


def _fetch_account_info(server_url: str, api_key: str) -> dict:
    """Calls /api/auth/me — that endpoint already includes the credit balance
    so a single round-trip is enough. Falls back to /api/payments/balance only
    if /me somehow doesn't return credits. Returns merged dict or {error}.
    """
    import json as _json
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError

    def _get(path):
        req = Request(server_url + path, method='GET')
        req.add_header('Authorization', 'Bearer ' + api_key)
        req.add_header('User-Agent', 'MimicReader-Calibre-Plugin/0.4-config')
        with urlopen(req, timeout=15) as resp:
            return _json.loads(resp.read().decode('utf-8'))

    out = {}
    try:
        me = _get('/api/auth/me')
    except HTTPError as e:
        return {'error': 'HTTP %d (check your API key)' % e.code}
    except URLError as e:
        return {'error': 'network: %s' % e.reason}
    except Exception as e:
        return {'error': str(e)[:120]}

    if isinstance(me, dict):
        out['email'] = me.get('email')
        if 'credits' in me:
            out['balance'] = me.get('credits')
        if 'total_purchased' in me:
            out['total_purchased'] = me.get('total_purchased')
        if 'plan' in me:
            out['plan'] = me.get('plan')

    # Fallback only if /me didn't include credits (older server versions)
    if 'balance' not in out:
        try:
            data = _get('/api/payments/balance')
            if isinstance(data, dict) and 'balance' in data:
                out['balance'] = data.get('balance')
                if 'total_purchased' in data:
                    out['total_purchased'] = data.get('total_purchased')
        except Exception:
            pass

    return out
