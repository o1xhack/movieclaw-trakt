# 隐私与授权 / Privacy

本插件在用户自己的 MovieClaw 服务器运行，无开发者运营的中转服务器、遥测或数据收集服务。

插件内置专用 Trakt 应用的公开 Client ID，仅用于识别应用；不包含任何用户令牌或 Client Secret。每个用户仍须明确授权，账号凭据由自己的 MovieClaw 服务器保存，插件开发者不会收到这些凭据。高级用户可覆盖为自己注册的应用。

连接时访问 Trakt 官方 OAuth 与用户设置接口。服务器将 Client Secret（若提供）、访问及刷新令牌、待授权设备码保存在 MovieClaw 插件加密存储；管理员状态页只返回连接状态和用户名，不返回令牌。

播放同步向 `api.trakt.tv` 发送影片外部编号、剧集季集编号、进度；延迟完成事件附带原观看时间。重试可能读取该时间附近的 Trakt 观看记录以避免重复。本地保留有限的事件 ID、播放会话、待同步及失败队列。另外保留每成员最近最多 60 条观看／收藏及 20 条播放发送展示记录，包含影片标题、季集、观看和处理时间、发送结果；不包含文件路径、设备名称或授权令牌。展示记录仅向服务器管理员开放，断开该成员连接时清除近期活动。凭据、媒体数据不会发送给 GitHub 或插件开发者。

账号和同步偏好按成员隔离，但管理接口仅管理员可用，沿用宿主已有的管理员鉴权。普通成员自助管理尚未提供，不依赖网页自报身份。请只绑定成员明确同意使用的账号。

断开连接会移除该成员的本地凭据、授权请求与同步队列，并尝试撤销 Trakt 授权。网络失败时需在 Trakt 的已连接应用中手动撤销。断开不会删除 Trakt 已有观看记录。MovieClaw 备份及日志保留遵循服务器自己的管理策略；向公开 issue 提供日志前请脱敏。

This plugin runs on your own MovieClaw server. It includes only the dedicated application's public Client ID, not a Client Secret or user tokens. Each user must authorize separately; tokens remain on their own server and are not sent to the plugin developer. Custom application settings are optional. It sends authorized playback identifiers, progress, and (for delayed completion) original timestamps directly to Trakt. It may read nearby history for deduplication. The server retains up to 60 watch/Watchlist and 20 playback display records per member, including titles, episodes, timestamps and outcomes, without file paths, device names or tokens. They are visible only to administrators and are removed when the member disconnects. OAuth credentials are encrypted by the host. No developer-operated relay, analytics, or telemetry is used. Disconnect removes the member's local connection and queue and attempts remote revocation; existing Trakt history is retained.

## 0.3.0 的同步资料

三个新增开关保存在成员作用域的插件资料中，默认关闭。开启取消已看同步后，手动取消会请求 Trakt 删除相应影片或集数的全部观看记录。收藏开关只把新收藏加入 Watchlist，不因取消收藏删除 Watchlist。

历史同步由管理员手动扫描、核对预览、Confirm 后在当前网页逐批推进。导出扫描本地库和观看事实，导入会分页读取已绑定 Trakt 账号的电影与单集观看历史，按编号匹配本地库存。需要单独批准主程序的读取和导入历史操作权限。没有成员额外设备配对，不保存或转发 MovieClaw 登录凭据。

历史任务、预览清单、日期、处理结果与去重回执保存在插件自己的存储，供管理员分页查看；详情不会在打开 History 时全部加载。断开后旧任务不能继续写入新绑定，历史摘要与回执保留，卸载数据删除由宿主选项决定。成功导入的观看事实由上游 WatchRecord 表保存，即使卸载插件也不自动撤销。不会将其伪装成播放会话或发送播放事件。

Explicitly confirmed bulk transfers read local inventory and/or the bound account's paginated Trakt history. Plans, watch times, outcomes and receipts persist in plugin storage for administrator review. Disconnect prevents old tasks from writing under a new binding but retains historical receipts. Imported facts remain host watch records; uninstalling the plugin does not undo them. No MovieClaw login credentials, extra device pairing, background bulk worker, relay or telemetry is used.
