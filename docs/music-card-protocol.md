# 网易云音乐卡片协议（music_card / netease_login_card）

> Phase 1 已交付：两个开源项目部署 + push.py 收卡端点 + 本文档。
> Phase 2 已交付：Android app 渲染 music_card、netease_login_card 交互、
> 服务端 `/netease-login/start|import` 端点（§4 已按实际实现更新）。
>
> 面向读者：后续维护者。写代码前先把 §2、§4 读完。

## 1. 架构总览

```
小克 / Kimi（MCP 客户端）
   │  调工具 song_share / lyric_share
   ▼
netease-music-mcp.service      127.0.0.1:18012（Anko3o MCP，Streamable HTTP）
   │  POST CARD_WEBHOOK_URL（Authorization: Bearer <webhook token>）
   ▼
push.py  POST /music/card      127.0.0.1:8291（cc-companion.service）
   │  校验 + 裁剪 → chat_history.append(metadata=music_card)
   ▼
App 轮询 /chat/history 拿到带 metadata 的 assistant 消息 → 渲染卡片
   │  点击 → player_url（网页播放器，nginx basic_auth）
   ▼
netease-music-server.service   127.0.0.1:9090（Anko3o 播放器后端）
   ▲  https://stackchan-backend.xiaonancaleb.xyz/music/（nginx 反代 + basic_auth）

netease-vael-mcp.service       127.0.0.1:3456（Vael-KY 账号工具，当前只读 12 件）
```

部署细节（unit 名、env 文件、cookie 路径）见本文 §5。

## 2. music_card metadata schema

收卡端点写入 chat_history 的消息形如：

```json
{
  "ts": "2026-09-01T15:04:05.123+08:00",
  "role": "assistant",
  "text": "🎵 分享歌曲：海阔天空 — Beyond",
  "source": "music-mcp:kimi",
  "metadata": { "music_card": true, "...": "见下" }
}
```

`metadata` 完整字段（服务端已做校验和裁剪，app 可以信任类型与长度上限）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `music_card` | `true` | 卡片类型判别标志（app 按此分发渲染，同 `recall_card` 惯例） |
| `music_card_type` | `"song"` \| `"lyric"` | 歌曲卡 / 歌词卡 |
| `song.song_id` | string | 网易云歌曲 ID，纯数字，≤20 位 |
| `song.name` | string | 歌名，1–200 字符 |
| `song.artist` | string | 歌手，≤200 字符，可空 |
| `song.album` | string | 专辑，≤200 字符，可空 |
| `song.cover` | string | 封面 URL，强制 `https://`，≤500 字符，非法时置空串 |
| `text` | string | 分享时附言（MCP 的 note），≤500 字符，可空 |
| `by` | string | 署名（MCP 侧 `MCP_SIGN_AS`，当前 `kimi`），≤32 字符 |
| `player_url` | string | 跳转 URL，见 §3 |
| `turn_terminal` | `false` | 辅助卡片，永远不是某轮回复的完成证据（同 recall_card 惯例） |
| `turn_message_kind` | `"auxiliary_music"` | 同上 |
| `lyric` | object | 仅歌词卡有，见下 |

歌词卡的 `lyric` 字段：

```json
{
  "at": 62.5,
  "line": {"time": 62.5, "text": "原谅我这一生不羁放纵爱自由", "trans": "可选翻译"},
  "prev": {"time": 58.0, "text": "上一句"},
  "next": {"time": 66.0, "text": "下一句"}
}
```

- `at`：定位秒数（0–14400，float）。
- `line` / `prev` / `next`：`{time, text, trans?}`，`text` 1–200 字符；`prev`/`next` 可缺省。
- 消息 `text` 已含 `mm:ss` 格式的定位，例如 `🎧 分享歌词：海阔天空 01:02「原谅我这一生不羁放纵爱自由」`。

### 2.1 收卡端点（已实现）

```
POST http://127.0.0.1:8291/music/card
Authorization: Bearer <webhook token>
Content-Type: application/json
```

- 只应被本机回环的 MCP 服务调用；token 存 `/root/netease-music/music-card-webhook.token`（0600），
  服务端每请求重读文件，轮换 token 不需要重启 cc-companion。
- Body ≤16 KiB。入参 schema 即 Anko3o MCP 的原始投递：
  `{"type":"song"|"lyric","text":...,"by":...,"song":{"songId","name","artist","album","cover"},"lyric":{...}}`。
- 可选字段 `contact`：`"kimi"`（默认）或 `"xiaoke"`，决定卡片写进哪个联系人的历史。
- 响应：成功 `200 {"ok":true,"contact_id":...,"ts":...}`；鉴权失败 `401`；格式错误 `400`。
- 不触发 APNs 推送、不计未读——与 recall_card 一致，卡片随下一次历史拉取出现。

## 3. 点击跳转（player_url）

- 形式：`https://stackchan-backend.xiaonancaleb.xyz/music/?song=<song_id>`，歌词卡追加 `&at=<秒>`。
- **注意**：上游网页播放器（client/index.html）当前**不解析** `song`/`at` 查询参数——
  打开后是播放器首页，深链参数只是协议预留。深链需要改播放器前端（小改，Phase 2 可选做，
  或 Phase 1.5 在 VPS 侧补）。app 侧先把整张卡片跳到这个 URL 即可。
- 该 URL 有 nginx basic_auth。凭据在 VPS `/root/netease-music/PLAYER-CREDENTIALS.txt`（0600）。
  app 可选方案：
  - 直接用系统浏览器 / Custom Tab 打开，用户首次输入一次 basic_auth（浏览器会记住）；
  - 或 app 内 WebView 预置 `Authorization: Basic ...` 头（凭据不落 app 代码，走下发）。
- 音频流走 `/music/file/...`，Range 请求 nginx 已透传。

## 4. netease_login_card（Phase 2 已实现）

目标：Astra 在 app 里完成网易云网页登录，cookie（`MUSIC_U` + `__csrf`）安全落到 VPS，
三个 netease 服务获得登录态。**cookie 全程不进聊天文本、不进日志、不进 git。**

实现完全复用 XhsLoginCard 的 WebView 收 cookie 模式（`xhs_login.py` ↔ `netease_login.py`、
`/xhs-login/*` ↔ `/netease-login/*`、`XhsLoginCard.kt` ↔ `NeteaseLoginCard.kt`）。
与原设计稿的两处偏差：卡片**不内嵌 login_session_id**（一次性 nonce 由 app 点开卡片时
经 `/netease-login/start` 现场换取，避免了历史卡片里 session 过期的问题）；登录结果
**不单独写结果卡**（import 同步完成，app 侧 toast 提示成功/失败）。

### 4.1 流程

```
1. 服务端门槛：push.py 每轮检查 anko3o/server/.netease_cred 是否缺少 MUSIC_U
   （NeteaseLoginManager.needs_login()）。缺失时 Kimi prompt 附加 [网易云登录卡片] 段，
   允许模型在回复末尾单独一行输出一次 [[CCC_NETEASE_LOGIN_CARD:v1]] 标记
2. push.py 提取标记（同 xhs 的窄语法：唯一、独立行、最后一行），在该轮终态 assistant
   消息 metadata 上写 netease_login_card=true；标记本身不进聊天文本
3. app 渲染登录卡（NeteaseLoginCardPreview）→ 点击打开全屏 Dialog 内嵌 WebView：
   - 先 POST /netease-login/start（X-Auth-Token pairing 鉴权 + android-app 来源）换取
     一次性 nonce（TTL 300s，绑定 contact_id + device_id + origin）
   - WebView 打开 https://music.163.com/，导航限 *.163.com、子资源限 163/126/127 域，
     桌面 UA；用户扫码（可保存二维码到相册）或密码登录
4. 用户点「我已确认，返回检测」，app 从 CookieManager 收割 music.163.com 域 cookie，
   白名单过滤到 MUSIC_U/__csrf 后 POST /netease-login/import（Body ≤8 KiB，cookie
   只进请求体——同 xhs-login 纪律）
5. 服务器校验 nonce（一次性、300s TTL、绑定一致）、cookie 白名单与字符集，原子写
   两个 0600 文件：
     /root/netease-music/anko3o/server/.netease_cred   → "MUSIC_U=<值>"
     /root/netease-music/env/vael-mcp.env              → NETEASE_COOKIE=/NETEASE_CSRF=
     （其余行如 NETEASE_READONLY=1 原样保留）
   然后固定 argv 执行 systemctl restart netease-vael-mcp netease-music-server
   netease-music-mcp（cookie 只在启动时读取，必须重启；这三个服务与 cc-companion
   无关，随时可重启）
6. 成功后 app toast「网易云登录已同步」，并清空本进程 WebView cookie jar
```

不做热加载的理由：两个项目都在启动时一次性读 cookie，热加载需要改上游代码；
重启三个小服务亚秒级完成，比维护 fork 补丁便宜。

### 4.2 netease_login_card metadata schema

```json
{
  "netease_login_card": true,
  "turn_terminal": true,
  "turn_message_kind": "terminal_answer"
}
```

卡片依附于当轮终态 assistant 消息（同 xhs_login_card 惯例），不是独立消息行；
cookie 缺失期间每轮 prompt 都带卡片段，登录成功（cookie 落盘）后门槛自动关闭。

### 4.3 风控与合规提醒

- 网易云账号写操作（红心写回 `/api/song/like`、听歌打卡 `/api/feedback/weblog`、
  Vael 侧的歌单增删改）**当前在服务端全部关闭**：
  - Anko3o server：`MUSIC_DISABLE_ACCOUNT_WRITES=1`（`/music/netease/like|scrobble` 返回 403）；
  - Vael MCP：`NETEASE_READONLY=1`（6 件写工具不注册、不可调用）。
  开启方式见 §5，但建议只读跑 2–4 周确认接口存活率后再放。
- 服务器 IP 与常用登录地差异大，`like` 类操作最容易触发 -460 风控。
- 上游 License 实际约束按 CC BY-NC-SA 4.0 理解（fork 标 MIT 是上游瑕疵）：
  自用/二改可以，商用不行，保留 eryu 署名。详情见调研报告
  `/root/.kimi-code/task-reports/netease-mcp-feasibility.md`。

## 5. 部署清单（Phase 1 实际状态）

| 组件 | unit | 监听 | 备注 |
|---|---|---|---|
| Vael-KY 账号 MCP | `netease-vael-mcp.service` | 127.0.0.1:3456 | 只读 12 工具（`NETEASE_READONLY=1`） |
| Anko3o 播放器后端 | `netease-music-server.service` | 127.0.0.1:9090 | 账号写操作已禁 |
| Anko3o 卡片 MCP | `netease-music-mcp.service` | 127.0.0.1:18012 | CARD_WEBHOOK_URL → push.py |
| 网页播放器入口 | nginx `stackchan-backend` site | 443 `/music/` | basic_auth（`/etc/nginx/.htpasswd-music`） |

关键路径：

- 代码：`/root/netease-music/vael-mcp/`、`/root/netease-music/anko3o/`（本地 clone，不进任何 git 远端）
- env（0600，`EnvironmentFile=`）：`/root/netease-music/env/{vael-mcp,anko3o-server,anko3o-mcp}.env`
- cookie：`/root/netease-music/anko3o/server/.netease_cred`（当前为空，待登录卡提供）
- 收卡 token：`/root/netease-music/music-card-webhook.token`（0600）
- 播放器 basic_auth 凭据：`/root/netease-music/PLAYER-CREDENTIALS.txt`（0600）
- 服务端收卡代码：`apns-server/push.py`（`/music/card` 路由 + `_music_card_*` 方法），
  测试 `apns-server/_music_card_test.py`
- 服务端登录桥：`apns-server/netease_login.py`（`/netease-login/start|import` 由 push.py
  挂接，鉴权同 xhs-login），测试 `apns-server/_netease_login_test.py`
- app 侧：`MusicCard.kt` / `NeteaseLoginCard.kt`（ui/chat），数据类与解析在
  `ChatMessage.kt` / `ApiClient.kt`
- 本地 MCP 允许清单：`apns-server/mcp_services.py` 的 `LOCAL_PROVIDERS`
  （`netease_account` → :3456，`netease_player` → :18012；回环校验在 `local_provider_endpoints()`）

开启写操作（观察期过后）：把 `env/anko3o-server.env` 的 `MUSIC_DISABLE_ACCOUNT_WRITES=0`、
`env/vael-mcp.env` 的 `NETEASE_READONLY=0`，然后 `systemctl restart netease-*`。
