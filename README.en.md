# MovieClaw Trakt

[中文](README.md) · English

Automatically track movies and episodes you watch in MovieClaw on your own Trakt account. Install the plugin on your MovieClaw server; no separate plugin installation is needed on your phone or TV.

**[Download plugin v0.1.5 (beta)](https://github.com/o1xhack/movieclaw-trakt/releases/download/v0.1.5/trakt-sync-0.1.5.mcplugin)** · [Release notes and downloads](https://github.com/o1xhack/movieclaw-trakt/releases/tag/v0.1.5)

## Which MovieClaw version do I need?

| Use case | MovieClaw requirement |
| --- | --- |
| Online playback tracking | **Tested with v0.41.0**; install directly |
| iOS downloaded/local playback and offline uploads | **v0.41.0 + the [server compatibility fix](https://github.com/o1xhack/movieclaw/tree/fix/offline-viewing-completion)**; stock v0.41.0 may miss rewatches and older uploaded completions |
| Other MovieClaw versions | Not yet tested; compatibility is not guaranteed |

You need MovieClaw administrator access, a Trakt account, and a server that can reach Trakt. **A dedicated Trakt application is built in. You do not need to register an app or enter a Client ID.**

> This is a beta. The offline path has been checked in source and isolated integration tests, but a complete real-iPhone airplane-mode/playback/reconnection test is still pending. From v0.1.5, install the plugin, click Connect Trakt, and authorize your account.

## Features

- **Now watching:** Show the current movie or episode on Trakt when online playback starts or resumes.
- **Automatic watch history:** Report progress and completion so Trakt can record watches under its rules. Autoplaying the next episode is not required.
- **Movies and TV episodes:** Match external media IDs and season/episode numbers.
- **Separate accounts for each member:** Sync each family member to their own Trakt account. Unconnected members are not tracked.
- **Retry after network failures:** Keep pending requests when the server cannot reach Trakt, retry later, and deduplicate repeated events.
- **Offline completion uploads:** With the server fix above, preserve original viewing times. The app must run again and reconnect to MovieClaw. Offline viewing cannot appear live as “now watching.”

Only clients that **report playback to MovieClaw** are tracked. Infuse playing independently through Plex does not pass through this plugin. Immediate pause reporting depends on MovieClaw's event support; currently stopping is mainly reported when you leave the player.

The plugin does not import past history, sync collections or ratings, or write Trakt progress back to MovieClaw.

## Installation

### 1. Download the plugin

Click **[Download trakt-sync-0.1.5.mcplugin](https://github.com/o1xhack/movieclaw-trakt/releases/download/v0.1.5/trakt-sync-0.1.5.mcplugin)** and save it to your computer.

Keep the `.mcplugin` file as downloaded. **Do not extract it or download GitHub's Source code archives.** No terminal commands or Python installation are needed.

### 2. Upload it to MovieClaw

Open the MovieClaw **web interface** as an administrator:

1. Go to **Settings → Plugins**.
2. Upload the `.mcplugin` file you downloaded.
3. Review and approve installation, then confirm that **Trakt 自动追踪** is enabled.

The plugin needs playback events, network access to Trakt, and storage for authorization and pending requests. It does not request media deletion or media file access.

### 3. Sign in and authorize Trakt

1. Open **Trakt** in the MovieClaw web sidebar.
2. For the administrator's own account, keep the default **member ID `0`**.
3. Click **连接 Trakt** (Connect Trakt), open the provided Trakt authorization link, enter the displayed code, and approve access.
4. Return to MovieClaw and wait for **已连接** (Connected).

For another family member, the administrator selects that member's MovieClaw ID and lets them authorize their own Trakt account. Connect each member separately.

**You're ready.** Play a recognized title as the connected MovieClaw member and check Trakt's “now watching.” The server only needs outbound HTTPS to Trakt: no public inbound port or VPN is required for the plugin. There is no second Trakt login on the phone.

## Updates and common questions

| Question | What to do |
| --- | --- |
| How do I update? | Download the new `.mcplugin`, upload it through the same plugin screen, and approve the update. Updates with the same plugin ID retain connections and plugin data |
| Nothing is syncing? | Check the Trakt connection, the MovieClaw member playing the title, pending requests, and any error shown on the Trakt page |
| Why didn't offline viewing upload immediately? | Run the app again and reconnect to MovieClaw. A force-quit app cannot guarantee automatic uploads. Full support also requires the server fix above |
| Why is a title skipped? | It needs recognizable external IDs such as TMDB / IMDb; the plugin does not guess from titles |
| How do I disconnect? | Click Disconnect on the Trakt page. Existing Trakt watch history is retained |

**Advanced:** You can still override the built-in application with your own Client ID in plugin settings. Most users should leave it blank. Existing custom application settings are preserved on update.

Avoid submitting the same viewing to the same Trakt account from multiple tools; this can still create duplicate records.

[Technical notes, troubleshooting, and development](docs/technical-notes.en.md) · [Privacy](PRIVACY.md) · [Changelog](CHANGELOG.md) · [Report an issue](https://github.com/o1xhack/movieclaw-trakt/issues)

This is an independent community plugin, not an official MovieClaw or Trakt product. It is not endorsed or certified by Trakt.
