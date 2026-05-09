"""MimicReader Send — Calibre plugin.

Sends selected books from Calibre library to MimicReader for audiobook generation.
Stage 1 of the Calibre Bridge concept (simple push model, ~200 LOC).
"""

from calibre.customize import InterfaceActionBase


class MimicReaderSendPlugin(InterfaceActionBase):
    name = 'MimicReader Send'
    description = 'Send selected book to MimicReader to generate a high-quality AI audiobook.'
    supported_platforms = ['windows', 'osx', 'linux']
    author = 'MimicReader'
    version = (0, 3, 1)
    minimum_calibre_version = (5, 0, 0)

    # Tells Calibre where the GUI action lives (inside the plugin zip).
    actual_plugin = 'calibre_plugins.mimicreader_send.ui:MimicReaderAction'

    def is_customizable(self):
        return True

    def config_widget(self):
        from calibre_plugins.mimicreader_send.config import ConfigWidget
        return ConfigWidget()

    def save_settings(self, config_widget):
        config_widget.save_settings()
        # Reinitialise the action if it is running
        ac = self.actual_plugin_
        if ac is not None:
            ac.apply_settings()
