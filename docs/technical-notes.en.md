# Technical notes and development

[Back to installation](../README.en.md) · [中文](technical-notes.zh-CN.md)

## Compatibility and playback rules

The tested host is MovieClaw v0.41.0 / SDK 1.0. Required event and service versions are declared in [movieclaw-plugin.toml](../movieclaw-plugin.toml). Untested host versions are not a compatibility promise.

- Start/resume updates Trakt watching. Stops follow Trakt rules: over 80% records a watch; below 1% only clears watching without saving resume progress.
- MovieClaw has no separate pause event. Leaving the player produces a stop event; timing depends on client reports and the delivery queue, so immediate updates are not guaranteed.
- Movies use TMDB / IMDb IDs. Episodes use show IDs plus season/episode numbers. Missing identifiers are skipped rather than guessed from titles.
- Explicit host completion is honored even if the resume position has been reset to zero. A historical `played` flag does not turn an early rewatch exit into a completion.
- Completed events delayed by more than five minutes preserve their original timestamp rather than appearing as current watching.
- Local viewings are isolated by `viewing_id` and written to history with their original time. When clearing watching for that same local viewing, the plugin checks Trakt's current item first to avoid interrupting another viewing.
- Uncertain completed writes are checked against nearby history before retrying. There is no distributed transaction between Trakt and local storage, so absolute exactly-once delivery is not guaranteed.

## Offline compatibility fix for stock v0.41.0

Full local/downloaded/offline support requires the [generic server fix](https://github.com/o1xhack/movieclaw/tree/fix/offline-viewing-completion), commit `2cd4373e`. It preserves actual event positions, publishes completion per local viewing, and adds an optional `viewing_id`. It contains no personal configuration, branding, or new database migration.

Stock v0.41.0 may only emit the first completion for a unit, missing rewatches and older uploaded records. A plugin cannot recreate events the host never emits. Deploy the compatibility fix before relying on full offline synchronization.

iOS already journals by viewing UUID, cleans up acknowledged records, and retries after reconnecting. No client changes or Trakt credentials on the phone are required. The app must run and reach MovieClaw; automatic uploads from a force-quit app are not guaranteed.

## Authorization, permissions, and troubleshooting

- Each member authorizes independently; an unconnected member never falls back to the administrator's account. Web management is administrator-only.
- The plugin listens to four playback events, reaches Trakt, and uses separate data storage. It requests no media deletion, media file paths, or other host operations.
- Its web page shares MovieClaw's origin and uses the administrator session. Only install plugin code you trust.
- Use a Client ID from your own Trakt application. Client Secret may be blank; if supplied, it is encrypted on the server. Do not reuse another app's credentials.
- Updates with the same plugin ID retain storage. Disconnect before changing Client ID, then authorize the new application.
- Disabling stops new playback events while retaining queued requests. Uninstall data handling follows the host's options.
- Network failures retry with backoff. Authorization errors retain the queue and request reconnection. **Reconnecting clears the old queue and does not backfill those watches.**
- Per member: up to 64 pending requests, 256 recent deduplication event IDs, and 16 recent sessions. When full, reliable events rely on host retries; realtime progress may be merged or dropped. The host's durable-event replay window must cover the actual interruption.
- Unmatchable 404 / 422 responses do not block later playback. Up to 16 failures are retained for review and retry. A 422 for a stop below 1% is a short-play exception, not a matching error.
- Disconnect removes local credentials and queues and attempts remote revocation. If revocation is unconfirmed, manually remove the connection in Trakt's connected applications.

The queue is not an unlimited history archive. There is no bulk history import, history deletion, collection/rating sync, or two-way progress sync.

## Verify a download (optional)

Each release includes `SHA256SUMS`. To verify integrity, run this in the folder containing both downloaded files:

```sh
shasum -a 256 -c SHA256SUMS
```

## Command-line installation (optional)

Most users should follow the web steps in the [README](../README.en.md). Administrators with `mclaw` configured can also upload using:

```sh
mclaw app plugins packages upload --file trakt-sync-0.1.4.mcplugin
```

Then review permissions and approve installation on the MovieClaw plugin page.

## Development and packaging

Requires Python 3.11+ and the SDK from the MovieClaw v0.41.0 source tree. Runtime dependencies are provided by MovieClaw; no manual pip installation on the NAS is needed.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
# Point MOVIECLAW_SOURCE at the MovieClaw v0.41.0 source tree
PYTHONPATH="$MOVIECLAW_SOURCE/src" .venv/bin/python -m pytest -q
.venv/bin/ruff check trakt_sync tests scripts
.venv/bin/python scripts/package.py
```

Packaging uses an allowlist and includes both documentation languages. Local settings, credentials, virtual environments, and test data are excluded. No deployed Client ID is bundled. Automated tests use fictional credentials and mocked Trakt responses without adding records to a real watch history.
