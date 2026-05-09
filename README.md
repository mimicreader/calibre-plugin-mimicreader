# MimicReader Send — Calibre Plugin

Send books from your Calibre library to [MimicReader.ai](https://mimicreader.ai)
for mobile reading and AI audiobook generation.

## Install

1. Download `mimicreader_send.zip` from the [Releases page](../../releases).
2. In Calibre: **Preferences → Plugins → Load plugin from file** → select the zip.
3. Restart Calibre.
4. First-run wizard prompts for an API key — get yours at
   [mimicreader.ai/dashboard/api-keys](https://mimicreader.ai/dashboard/api-keys)
   (free signup).

## What it does

- **Library sync** — incremental metadata sync, tested on a 27,121-book library.
- **Cover upload** — automatic, parallel; library tiles render correctly on mobile.
- **Long-poll fetch** — open a book on mobile, plugin uploads the file in the background.
- **Bulk queue** — up to 500 books, runs in the background, close Calibre when done.
- **Audiobook attach** — generated M4B files come back into Calibre as a new format.
- **Read-only `metadata.db`** — never modifies your Calibre library.
- **Daily auto-update check** — stays current without manual intervention.
- **One-button wipe** — removes your data from the MimicReader server.

## Privacy

- The plugin reads `metadata.db` read-only. Your Calibre library is never modified.
- Book files are transferred only when you explicitly request it (Send to MimicReader,
  bulk queue, or long-poll fetch from mobile).
- API key is stored in Calibre's plugin config (plaintext on disk — same trust
  model as the Calibre Content Server password).

## Requirements

- **Calibre:** 5.0 or later
- **Platforms:** Windows, macOS, Linux
- A free [MimicReader.ai](https://mimicreader.ai) account

## Building from source

```bash
zip -r mimicreader_send.zip __init__.py config.py main.py poller.py sync.py ui.py update_check.py images/ plugin-import-name-mimicreader_send.txt
```

Then load the resulting zip via **Preferences → Plugins → Load plugin from file**.

## Issues and contributions

- Bug reports and feature requests: [GitHub Issues](../../issues)
- Pull requests welcome
- For private issues (security, GDPR): hello@mimicreader.ai

## License

MIT — see [LICENSE](LICENSE).
