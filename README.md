# MovieClaw Trakt

将 MovieClaw 播放活动同步到成员自己的 Trakt 账号。插件在服务器独立进程运行，无需重新构建 iOS App。

**首版测试版：要求 MovieClaw v0.41.0 / SDK 1.0。** 仅适用于向 MovieClaw 上报播放事件的客户端；独立使用 Infuse/Plex 的播放不会经过本插件。

## v0.41.0 的离线补传兼容修复

基础实时播放可直接安装本插件。完整的 iOS 本机副本／离线补传（包括重看和乱序补传）还需要 [通用服务端修复](https://github.com/o1xhack/movieclaw/tree/fix/offline-viewing-completion)：保留事件实际进度，为每次本机观看发一次完成事件，并附带可选 `viewing_id`。未修复的原版 v0.41.0 可能只给同一单元首次完成发事件，插件无法补出宿主没有发来的事件。此修复没有私人配置、品牌或新数据库迁移。

iOS 已有按观看 UUID 保存、确认后清理和恢复连接补传的机制，不需本插件向手机下发凭据。恢复网络后仍需 App 运行并能连接到 MovieClaw 服务器；强制结束的 App 不会自行持续联网。

## 功能与边界

- 开始 / 恢复播放同步到 Trakt watching；停止时按 Trakt 规则处理，进度超过 80% 记为已看；不足 1% 的短播放只清除 watching，Trakt 不保存恢复进度。
- MovieClaw 暂无独立暂停事件；关闭播放器产生停止事件后才同步暂停状态。实时性受客户端上报和队列影响，通常约 5 秒。
- 电影使用 TMDB / IMDb 编号；剧集使用剧集所属节目的外部编号及季、集编号。没有明确编号就跳过，不猜标题。
- 本机观看完成按 `viewing_id` 识别并用事件原时间添加历史；已知同次本机正在看时先核对再暂停，不重复制造已看记录。
- 宿主完成事件即使把续播点清零也按完成处理；单独的历史 `played` 标志不会把提前退出的重看误当完成。
- 各 MovieClaw 成员独立授权；未绑定成员不会借用管理员账号。
- Web 管理页面由管理员为指定成员绑定；当前不开放普通成员自助管理其他账号。
- 凭据加密持久化，事件去重，断网 / 429 后重试。延迟超过 5 分钟的完成事件保留原观看时间，不伪装成当前 watching。
- 对结果不确定的已看请求，重试前检查相邻观看记录，降低重复记入风险。外部服务与本地存储间没有分布式事务，不能承诺绝对 exactly-once。
- 不批量导入历史，不删除 Trakt 已有记录，不同步收藏、评分或双向播放进度。

## 安装

1. 从本仓库 Releases 下载 `.mcplugin`，按 `SHA256SUMS` 验证。
2. MovieClaw「设置 → 插件」上传并批准安装，或使用 `mclaw app plugins packages upload --file <文件>`。
3. 批准说明：监听四个敏感播放事件，联网访问 Trakt，使用插件独立数据存储；不申请媒体删除、文件路径或其他宿主操作。Web 页面运行在 MovieClaw 同源，管理员打开时使用管理员会话；只安装你信任的代码。
4. 在插件设置填写自己在 [Trakt Developer](https://developer.trakt.tv/apps) 创建的 **Client ID**。Client Secret 已被 Trakt 弃用，可留空；如需填写则只在服务器加密保存。不要使用其他 App 的凭据。开发者门户可能要求先绑定 GitHub。
5. 打开侧边栏「Trakt」，选择 MovieClaw 成员 ID（管理员为 `0`），点击连接，前往 Trakt 输入设备授权码并确认。
6. 回到页面确认已连接，再使用 MovieClaw 客户端播放。可查看待同步数量和失败状态。

设备授权只需服务器出站 HTTPS，无需公网 IP、回调端口或 VPN。iOS 端不需要再次登录 Trakt。

## 断开、更新与排错

- 在 Trakt 页面点「断开连接」，清除此成员的本地凭据及队列，并尝试撤销远端授权；若撤销未确认，前往 Trakt 已连接应用手动移除。
- 更新插件保留同 ID 的独立存储；更换 Client ID 前先断开原连接，再授权新应用。
- 插件禁用时不接收新播放活动，已有队列保留；卸载数据处理遵循宿主选项。
- 连接错误会按退避重试；授权错误保留队列并提示重新连接。重新连接会清除旧队列，不补录旧观看。
- 每成员最多 64 条待发请求、256 个近期事件去重 ID、16 个近期播放会话。队列满时可靠事件留给宿主重试；实时进度可合并或丢弃。需要保证 MovieClaw 的可靠事件重放窗口覆盖实际离线时间。
- 无法匹配的 404 / 422 不阻塞后续播放，最多保留最近 16 条失败记录，检查编号后点重试。长期故障请及时处理，勿把队列当作无限历史库。
- 同时让 Infuse/Plex 等其他客户端向同一 Trakt 账号提交同一次观看，仍可能产生冲突或重复。请为一次播放选择一个同步来源。

## 开发与打包

Python 3.11+。测试需 MovieClaw v0.41.0 源码提供 SDK；运行插件时依赖由 MovieClaw 提供，无需 NAS 上联网安装 pip 包。

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
# MOVIECLAW_SOURCE 指向干净的 MovieClaw v0.41.0 源码目录
PYTHONPATH="$MOVIECLAW_SOURCE/src" .venv/bin/python -m pytest -q
.venv/bin/ruff check trakt_sync tests scripts
.venv/bin/python scripts/package.py
```

打包采用文件白名单；不包含本地设置、凭据、虚拟环境或测试数据。源码不携带任何部署的 Client ID。自动化测试使用虚构凭据与模拟 Trakt 响应，不需要真实账号，不向真实观看历史写入测试记录。

English: Self-hosted, per-member Trakt scrobbling for MovieClaw. Install the release `.mcplugin`, configure your own Trakt application, and authorize each member using the device flow. Only playback reported to MovieClaw is tracked. No bulk history import, deletion, or bundled account credentials. See [Privacy](PRIVACY.md).

MovieClaw Trakt uses Trakt but is not endorsed or certified by Trakt. This is an independent community plugin, not an official MovieClaw or Trakt product.
