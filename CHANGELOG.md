# Changelog

## 0.1.2

- Treat Trakt HTTP 422 for stops below 1% as the documented short-play no-op, rather than a media-matching failure. Covers queued pause requests from earlier versions too.
- Use the stop endpoint for player exits at every progress value.

## 0.1.1

- Accept the official `auth.trakt.tv` device authorization hostname returned by the live Trakt API, alongside `trakt.tv` and `app.trakt.tv`; keep rejecting unrelated domains.
- Add a regression test for the live device response URL.

## 0.1.0 (initial beta)

- Per-member OAuth device authorization, encrypted credentials, persistent playback event queue and retries.
- Administrator Web connection page; SDK lifecycle, route and deduplication tests.
