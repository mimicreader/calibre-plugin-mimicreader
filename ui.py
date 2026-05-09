"""GUI integration — toolbar button + right-click menu + progress + status dialogs."""

import traceback

try:
    from qt.core import (QIcon, QMenu, QProgressDialog, QPixmap, QMessageBox,
                         QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
                         QLabel, QLineEdit, QComboBox, QRadioButton, QButtonGroup,
                         QDialogButtonBox, QCheckBox, QFrame)
except ImportError:
    from PyQt5.Qt import (QIcon, QMenu, QProgressDialog, QPixmap, QMessageBox,
                          QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
                          QLabel, QLineEdit, QComboBox, QRadioButton, QButtonGroup,
                          QDialogButtonBox, QCheckBox, QFrame)

from calibre.gui2 import info_dialog, error_dialog, question_dialog
from calibre.gui2.actions import InterfaceAction

from calibre_plugins.mimicreader_send.config import prefs
from calibre_plugins.mimicreader_send.main import send_book, trigger_generation
from calibre_plugins.mimicreader_send.sync import export_library_metadata, sync_library, push_covers
from calibre_plugins.mimicreader_send.poller import PendingPoller
from calibre_plugins.mimicreader_send.update_check import start_background_check

PLUGIN_VERSION = (0, 4, 12)

# Display name registered with Calibre. Changing this is a breaking change for
# anyone with the old action name still in their gprefs — see _migrate_old_name()
# in genesis() for the cleanup that replaces 'MimicReader Send' → 'MimicReader'.
PLUGIN_NAME = 'MimicReader'
LEGACY_PLUGIN_NAMES = ('MimicReader Send',)

# Calibre gprefs keys for menu/toolbar action layouts. The plugin name is
# appended to each layout it gets registered to.
AUTO_MENU_LOCATIONS = (
    ('action-layout-toolbar',      'Main toolbar'),
    ('action-layout-context-menu', 'Right-click context menu (Library)'),
)
OPTIONAL_MENU_LOCATIONS = (
    ('action-layout-menubar',                   'Show in main menubar',
     'File / Edit / Tools… menu bar'),
    ('action-layout-context-menu-cover-browser','Show in cover browser context menu',
     'Right-click on a cover in the grid view'),
)
ALL_LAYOUT_KEYS = tuple(k for k, _ in AUTO_MENU_LOCATIONS) + \
                  tuple(k for k, _, _ in OPTIONAL_MENU_LOCATIONS)


class MimicReaderAction(InterfaceAction):
    name = PLUGIN_NAME
    action_spec = ('MimicReader', 'images/icon.png',
                   'Upload selected book(s) to MimicReader for audiobook generation', 'Ctrl+Shift+M')
    action_type = 'current'

    def genesis(self):
        """Called once after the GUI has been set up."""
        # Build menu BEFORE touching qaction — matches the KindleUnpack / KFX Input
        # pattern used by long-standing third-party plugins.
        self.menu = QMenu(self.gui)
        self.menu.addAction('Send selected book(s) to MimicReader', self.send_selected_books)
        self.menu.addAction('Bulk queue selected books (background upload)', self.bulk_queue_selected)
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

        # Migration: rename old action keys ('MimicReader Send') to PLUGIN_NAME
        # in every gprefs menu/toolbar layout. Runs every start, idempotent.
        try:
            from calibre.gui2 import gprefs
            self._migrate_old_plugin_name(gprefs)
        except Exception:
            pass

        # First-run wizard: shown once after install. Auto-adds plugin to the
        # toolbar + library context menu, then offers a setup dialog.
        # Modifies Calibre's `gprefs` action layouts → restart needed.
        try:
            if not prefs.get('install_wizard_done', False):
                # Defer until the main window is fully shown so the dialog
                # actually has a parent and the icon flashes correctly.
                from functools import partial
                try:
                    from PyQt5.QtCore import QTimer  # type: ignore
                except ImportError:
                    from qt.core import QTimer  # type: ignore
                QTimer.singleShot(800, partial(self._show_install_wizard))
        except Exception:
            pass

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

    def _add_to_gprefs_layout(self, gprefs, gprefs_key):
        """Append PLUGIN_NAME to a Calibre menu/toolbar layout if missing.
        Returns True if added, False if already present, None on error."""
        try:
            current = list(gprefs.get(gprefs_key, ()))
            if PLUGIN_NAME in current:
                return False
            current.append(PLUGIN_NAME)
            gprefs[gprefs_key] = tuple(current)
            return True
        except Exception:
            return None

    def _migrate_old_plugin_name(self, gprefs):
        """Replace any legacy plugin names (e.g. 'MimicReader Send') with
        PLUGIN_NAME in every menu/toolbar layout. Idempotent — safe on every
        plugin start. Without this migration, users who installed v0.4.0–v0.4.2
        would end up with orphaned 'MimicReader Send' entries in gprefs after
        upgrading to v0.4.3."""
        for gprefs_key in ALL_LAYOUT_KEYS:
            try:
                current = list(gprefs.get(gprefs_key, ()))
                changed = False
                rewritten = []
                seen_new = False
                for item in current:
                    if item in LEGACY_PLUGIN_NAMES:
                        if not seen_new and PLUGIN_NAME not in rewritten:
                            rewritten.append(PLUGIN_NAME)
                            seen_new = True
                        changed = True
                        continue
                    rewritten.append(item)
                if changed:
                    gprefs[gprefs_key] = tuple(rewritten)
            except Exception:
                pass

    def _show_install_wizard(self):
        """First-run setup: auto-registers plugin in toolbar + library context
        menu, then shows a single dialog for server settings + optional menu
        locations. Modifies Calibre's `gprefs` → restart required.
        """
        try:
            from calibre.gui2 import gprefs
        except Exception:
            return

        # Re-check inside the deferred call — user may have already dismissed
        if prefs.get('install_wizard_done', False):
            return

        # 1) Always auto-add to toolbar + library context menu (user requirement)
        for gprefs_key, _label in AUTO_MENU_LOCATIONS:
            self._add_to_gprefs_layout(gprefs, gprefs_key)

        # 2) Build comprehensive setup dialog
        dlg = QDialog(self.gui)
        dlg.setWindowTitle('MimicReader — First-time setup')
        dlg.setMinimumWidth(520)
        layout = QVBoxLayout(dlg)

        # Header
        header = QLabel(
            '<b>Welcome to MimicReader!</b><br>'
            '<small>The plugin has been added to your toolbar and right-click '
            'menu. Configure your account below — you can change everything '
            'later via "Customize plugin".</small>'
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        layout.addWidget(self._hr())

        # --- Section: Server settings ---
        layout.addWidget(QLabel('<b>Server connection</b>'))
        form_server = QFormLayout()
        layout.addLayout(form_server)

        server_url_input = QLineEdit(prefs['server_url'])
        server_url_input.setPlaceholderText('https://mimicreader.ai')
        form_server.addRow('Server URL:', server_url_input)

        api_key_input = QLineEdit(prefs['api_key'])
        api_key_input.setEchoMode(QLineEdit.Password)
        api_key_input.setPlaceholderText('mk_live_…')
        form_server.addRow('API key:', api_key_input)

        api_help = QLabel(
            '<small>Get an API key at '
            '<a href="https://mimicreader.ai/dashboard/api-keys">'
            'mimicreader.ai/dashboard/api-keys</a>.</small>'
        )
        api_help.setOpenExternalLinks(True)
        api_help.setWordWrap(True)
        layout.addWidget(api_help)

        layout.addWidget(self._hr())

        # --- Section: Generation defaults ---
        layout.addWidget(QLabel('<b>Generation defaults</b>'))
        form_gen = QFormLayout()
        layout.addLayout(form_gen)

        format_combo = QComboBox()
        format_combo.addItems(['EPUB', 'AZW3', 'MOBI', 'PDF', 'TXT', 'FB2'])
        idx = format_combo.findText(prefs['preferred_format'])
        format_combo.setCurrentIndex(max(0, idx))
        form_gen.addRow('Preferred format:', format_combo)

        tier_combo = QComboBox()
        tier_combo.addItems(['standard', 'premium'])
        tier_idx = tier_combo.findText(prefs['tier'])
        tier_combo.setCurrentIndex(max(0, tier_idx))
        form_gen.addRow('Default tier:', tier_combo)

        auto_start_cb = QCheckBox('Automatically start audiobook generation after upload')
        auto_start_cb.setChecked(prefs['auto_start_generation'])
        layout.addWidget(auto_start_cb)

        layout.addWidget(self._hr())

        # --- Section: Where else to show the plugin ---
        layout.addWidget(QLabel('<b>Where should the plugin appear?</b>'))
        layout.addWidget(QLabel(
            '<small>Already added: <b>Main toolbar</b> + '
            '<b>Right-click context menu (Library)</b>.<br>'
            'Optional extras:</small>'
        ))

        optional_checkboxes = []
        for _gprefs_key, label, hint in OPTIONAL_MENU_LOCATIONS:
            cb = QCheckBox(label)
            cb.setChecked(False)
            layout.addWidget(cb)
            hint_lbl = QLabel(
                '<small style="color:#888">&nbsp;&nbsp;&nbsp;%s</small>' % hint
            )
            hint_lbl.setWordWrap(True)
            layout.addWidget(hint_lbl)
            optional_checkboxes.append(cb)

        layout.addWidget(self._hr())

        layout.addWidget(QLabel(
            '<i><small>Note: restart Calibre once after saving for menu changes '
            'to take effect.</small></i>'
        ))

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText('Save')
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        layout.addWidget(bb)

        if dlg.exec() != QDialog.Accepted:
            # User cancelled — but we already added toolbar + context menu.
            # Don't pester again on next start.
            prefs['install_wizard_done'] = True
            return

        # 3) Persist server + generation settings
        prefs['server_url'] = (server_url_input.text() or '').strip().rstrip('/')
        prefs['api_key'] = (api_key_input.text() or '').strip()
        prefs['preferred_format'] = format_combo.currentText()
        prefs['tier'] = tier_combo.currentText()
        prefs['auto_start_generation'] = auto_start_cb.isChecked()

        # 4) Apply optional menu locations
        added_optional = []
        for cb, (gprefs_key, label, _hint) in zip(optional_checkboxes, OPTIONAL_MENU_LOCATIONS):
            if not cb.isChecked():
                continue
            if self._add_to_gprefs_layout(gprefs, gprefs_key):
                added_optional.append(label)

        prefs['install_wizard_done'] = True

        # 5) Confirmation message
        added_locs = ['Main toolbar', 'Right-click context menu (Library)'] + added_optional
        msg_lines = ['MimicReader is now in:']
        for loc in added_locs:
            msg_lines.append('  • %s' % loc)
        msg_lines.append('')
        msg_lines.append('Restart Calibre once to see the menu items.')

        info_dialog(self.gui, 'Setup complete', '\n'.join(msg_lines), show=True)

    def _hr(self):
        """Horizontal separator line."""
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

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
        progress.setWindowTitle('MimicReader')
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

    def bulk_queue_selected(self):
        """Server-side queue: send only metadata IDs, plugin's background poller
        uploads files later. User can close Calibre after this finishes."""
        if not prefs['api_key'] or not prefs['server_url']:
            self._prompt_config('Configure server URL and API key first.')
            return

        rows = self.gui.library_view.selectionModel().selectedRows()
        if not rows:
            return error_dialog(self.gui, 'No book selected',
                                'Select one or more books, then try again.',
                                show=True)

        book_ids = [self.gui.library_view.model().id(r) for r in rows]
        if len(book_ids) > 500:
            return error_dialog(self.gui, 'Too many books',
                                'Bulk queue accepts up to 500 books at once. '
                                'Please split your selection.', show=True)

        if len(book_ids) > 50:
            confirmed = question_dialog(
                self.gui, 'Bulk queue %d books?' % len(book_ids),
                'Server will queue %d books. Plugin will keep uploading them in '
                'the background — you can close Calibre after the queue is sent. '
                'Continue?' % len(book_ids),
                show_copy_button=False,
            )
            if not confirmed:
                return

        # Build library_uuid (Calibre exposes it via db.library_id)
        try:
            library_uuid = self.gui.current_db.library_id
        except Exception:
            library_uuid = ''

        try:
            from calibre_plugins.mimicreader_send.main import bulk_queue
            result = bulk_queue(prefs['server_url'], prefs['api_key'],
                                library_uuid, book_ids)
        except Exception as e:
            return error_dialog(self.gui, 'Bulk queue failed', str(e), show=True)

        msg = ('Bulk queue sent.\n\n'
               'Queued: %d\n'
               'Already uploaded: %d\n'
               'Not in catalog (run "Sync library catalog" first): %d\n\n'
               'Plugin will upload files in the background. You can close Calibre.'
               % (len(result.get('queued', [])),
                  len(result.get('skipped', [])),
                  len(result.get('not_in_catalog', []))))
        info_dialog(self.gui, 'MimicReader bulk queue', msg, show=True)

    def sync_library_catalog(self):
        """Push library metadata (and covers) to MimicReader. Runs all heavy work
        in a QThread so the Calibre GUI never freezes. The progress dialog talks
        to the worker via Qt signals/slots only — no processEvents() hacks."""
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

        try:
            library_uuid = str(db.library_id)
        except Exception:
            library_uuid = 'default'
        try:
            library_name = str(self.gui.current_db.library_path).rstrip('/\\').rsplit('/', 1)[-1].rsplit('\\', 1)[-1]
        except Exception:
            library_name = 'My library'

        last_syncs = prefs.get('last_sync_per_lib') or {}
        prev_sync_ts = last_syncs.get(library_uuid)
        mode = self._ask_sync_mode(total_books, prev_sync_ts)
        if mode is None:
            return
        since_ts = prev_sync_ts if mode == 'incremental' else None

        # ----- Set up progress dialog -----
        progress = QProgressDialog('Starting sync…', 'Cancel', 0, 100, self.gui)
        progress.setWindowTitle('MimicReader — Sync library catalog')
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setValue(0)
        progress.show()

        # ----- Set up worker thread -----
        try:
            from qt.core import QThread  # type: ignore
        except ImportError:
            from PyQt5.QtCore import QThread  # type: ignore
        from calibre_plugins.mimicreader_send.worker import SyncWorker

        sync_started_at = __import__('time').time()
        thread = QThread(self.gui)
        worker = SyncWorker(
            server_url=prefs['server_url'],
            api_key=prefs['api_key'],
            library_uuid=library_uuid,
            library_name=library_name,
            db=db,
            since_ts=since_ts,
            total_books=total_books,
        )
        worker.moveToThread(thread)

        # Keep references on the action so they survive until cleanup
        self._sync_thread = thread
        self._sync_worker = worker

        # Phase tracking — the worker tells us what stage it's in; we map that
        # to a progress-bar range so the bar fills smoothly across the whole sync.
        state = {'phase': 'metadata'}

        def on_phase(phase):
            state['phase'] = phase
            if phase == 'metadata':
                progress.setLabelText('Reading metadata…')
            elif phase == 'sync':
                progress.setLabelText('Sending metadata to server…')
            elif phase == 'covers':
                progress.setLabelText('Uploading covers…')

        def on_progress(done, total, msg):
            if state['phase'] == 'metadata':
                pct = int(15 * done / max(total, 1))
            elif state['phase'] == 'sync':
                pct = 15 + int(20 * done / max(total, 1))
            else:
                pct = 35 + int(64 * done / max(total, 1))
            progress.setValue(min(99, pct))
            progress.setLabelText(msg)

        def cleanup_thread():
            try:
                thread.quit()
                thread.wait(5000)
            except Exception:
                pass
            self._sync_thread = None
            self._sync_worker = None

        def on_finished(result):
            progress.setValue(100)
            progress.close()

            if result.get('cancelled'):
                sr = result.get('sync_result') or {}
                msg = 'Sync was cancelled.'
                if sr.get('total'):
                    msg += '\n\nAdded so far: %d / Updated: %d' % (
                        sr.get('inserted', 0), sr.get('updated', 0))
                info_dialog(self.gui, 'Cancelled', msg, show=True)
                cleanup_thread()
                return

            # Persist last-sync timestamp so the next run can default to incremental
            try:
                ls = dict(prefs.get('last_sync_per_lib') or {})
                ls[library_uuid] = sync_started_at
                prefs['last_sync_per_lib'] = ls
            except Exception:
                pass

            sync_r = result.get('sync_result') or {}
            cov_r = result.get('cover_result') or {}
            mode_label = 'Incremental sync' if since_ts else 'Full sync'
            cov_total = cov_r.get('total', 0)
            cov_no = cov_r.get('skipped', 0)
            cov_failed = cov_r.get('failed', 0)
            cov_sent = cov_r.get('uploaded', 0)
            covers_line = '• Covers uploaded: %d / %d  (no-cover books: %d%s)' % (
                cov_sent, cov_total, cov_no,
                (', failed: %d' % cov_failed) if cov_failed else '',
            )

            info_dialog(
                self.gui, 'Library synced',
                '%s complete.\n\n'
                '• Books in this batch: %d\n'
                '• Added (new): %d\n'
                '• Updated: %d\n'
                '• Errors: %d\n'
                '• Server now has: %d books\n'
                '%s\n\n'
                'Open the Calibre tab in mimicreader.ai/app to browse your library.' % (
                    mode_label,
                    sync_r.get('total', 0),
                    sync_r.get('inserted', 0),
                    sync_r.get('updated', 0),
                    sync_r.get('errors', 0),
                    sync_r.get('total_on_server', 0),
                    covers_line,
                ),
                show=True,
            )
            cleanup_thread()

        def on_error(msg):
            progress.close()
            error_dialog(self.gui, 'Sync failed',
                         'The sync worker raised an error:\n\n%s' % msg, show=True)
            cleanup_thread()

        # ----- Wire signals -----
        worker.phase_changed.connect(on_phase)
        worker.progress.connect(on_progress)
        worker.finished.connect(on_finished)
        worker.error_occurred.connect(on_error)
        thread.started.connect(worker.run)
        # Cancel button → tell worker to stop. Worker keeps running until it
        # checks `_cancelled` between loop iterations, then emits finished.
        progress.canceled.connect(worker.cancel)

        thread.start()

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
            self.gui, 'MimicReader — configure', message + '\n\nOpen configuration now?',
            show_copy_button=False,
        )
        if confirmed:
            self.interface_action_base_plugin.do_user_config(parent=self.gui)


