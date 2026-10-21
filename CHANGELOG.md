# Changelog

## 0.1.4

- 默认中文 README，新增完整英文版，明确宿主版本要求、功能和网页安装流程。
- 添加插件包直接下载入口，将开发、权限和队列细节移入中英文技术说明。
- 插件包包含中英文文档；播放同步逻辑与 0.1.3 相同。
- Chinese-first README with a full English version, explicit compatibility, features, and web installation steps.
- Direct plugin download links and separate technical notes in both languages, bundled with the plugin.
- Playback behavior is unchanged from 0.1.3.

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
