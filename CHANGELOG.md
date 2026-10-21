# Changelog

## 0.1.3

- Honor explicit completion events even when MovieClaw resets the resume position to zero.
- Preserve the original timestamp for local viewing completions identified by `viewing_id`, including recent uploads.
- Isolate local viewing sessions by UUID; clear matching live watching without creating a second completed record.
- Document the generic v0.41.0 server compatibility fix required for complete offline rewatch and out-of-order replay support.

## 0.1.2

- Treat Trakt HTTP 422 for stops below 1% as the documented short-play no-op, rather than a media-matching failure. Covers queued pause requests from earlier versions too.
- Use the stop endpoint for player exits at every progress value.

## 0.1.1

- Accept the official `auth.trakt.tv` device authorization hostname returned by the live Trakt API, alongside `trakt.tv` and `app.trakt.tv`; keep rejecting unrelated domains.
- Add a regression test for the live device response URL.

## 0.1.0 (initial beta)

- Per-member OAuth device authorization, encrypted credentials, persistent playback event queue and retries.
- Administrator Web connection page; SDK lifecycle, route and deduplication tests.
