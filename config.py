"""Configuration: server URL + API key + preferred format.

Stored via Calibre's JSONConfig under ~/.config/calibre/plugins/mimicreader_send.json.
Secrets (API key) are plaintext on disk — same trust model as Calibre Content Server password.
"""

from calibre.utils.config import JSONConfig

try:
    from qt.core import (
        QWidget, QLineEdit, QFormLayout, QLabel, QComboBox, QCheckBox, QVBoxLayout, QPushButton
    )
except ImportError:
    from PyQt5.Qt import (
        QWidget, QLineEdit, QFormLayout, QLabel, QComboBox, QCheckBox, QVBoxLayout, QPushButton
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


class ConfigWidget(QWidget):
    def __init__(self):
        super().__init__()

        layout = QVBoxLayout()
        self.setLayout(layout)

        header = QLabel(
            '<b>MimicReader Send</b><br>'
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

        help_text = QLabel(
            '<small>Get an API key at '
            '<a href="https://mimicreader.ai/dashboard/api-keys">mimicreader.ai/dashboard/api-keys</a>. '
            'One upload consumes credits only when you actually start generating audio.</small>'
        )
        help_text.setOpenExternalLinks(True)
        help_text.setWordWrap(True)
        layout.addWidget(help_text)

        layout.addStretch(1)

    def save_settings(self):
        prefs['server_url'] = (self.server_url_input.text() or '').strip().rstrip('/')
        prefs['api_key'] = (self.api_key_input.text() or '').strip()
        prefs['preferred_format'] = self.format_combo.currentText()
        prefs['tier'] = self.tier_combo.currentText()
        prefs['auto_start_generation'] = self.auto_start_cb.isChecked()
