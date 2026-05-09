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

## What the plugin sends to the server

Full data flow disclosure — every network call the plugin makes:

### `POST /api/calibre/sync-catalog` (when you run "Sync library catalog")
Per book: `title`, `authors`, `tags`, `series`, `series_index`, `language`,
`publisher`, `pubdate`, `rating`, `comments (first 500 characters)`, format list,
size in bytes, `last_modified` timestamp.

**Heads-up:** if you keep personal notes or reviews in the Calibre `comments`
field, the first 500 characters are uploaded as part of the metadata. Book
contents are NOT uploaded by this endpoint.

### `POST /api/calibre/cover-upload/{id}` (after metadata sync)
Cover thumbnails resized client-side to 200×300 JPEG quality 75.

### `POST /api/library/upload` (when you click "Send to MimicReader")
The book file in your preferred format (EPUB/AZW3/MOBI/PDF/TXT/FB2) plus title
and author. Only fired on explicit user action.

### `POST /api/calibre/fulfill-upload/{id}` (long-poll, on-demand)
Same as `/upload` but triggered when you open a book on mobile —
the plugin uploads the file in response to the server's pending request.

### `GET /api/calibre/pending` (long-poll, idle)
A long-poll request (~55s timeout) waiting for upload requests from your phone.
Sends only your API key in the `Authorization` header — no library data.

### `GET /static/mimicreader_send_version.txt` (once per 24h)
Plain unauthenticated GET to check for plugin updates. Sends nothing about you
or your library — just a regular HTTP request with the plugin's User-Agent.
The plugin shows you a dialog if a newer version exists; download and install
are always manual.

### What the plugin does NOT do
- No telemetry, no analytics, no usage tracking
- No phone-home with library statistics
- No background uploads beyond what's listed above
- No modification of your Calibre library or `metadata.db`

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
