# CcCompanion APNs Server

Python HTTP server, runs on your Mac. Forwards chat messages between your iPhone (CcCompanion app) and a local `tmux` session running `claude` (Claude Code CLI). Pushes Claude's replies back to your iPhone via APNs (or Bark fallback).

This is **not** a managed service. You run it on your own machine, your data never leaves your local network (except for the push notification preview, which goes through Apple APNs or Bark relay).

---

## Supported Regions Policy

This project relies on Anthropic's Claude API / Claude Code. **Mainland China is NOT in Anthropic's officially supported regions list.** China users connecting via VPN may experience unstable connections and risk account suspension under Anthropic's Terms of Service. Use at your own discretion.

For security, this server ships with `strict_auth = true` and `allow_remote_control = false` by default. Do **NOT** expose port 8795 to the public internet without a HTTPS reverse proxy (Caddy / Nginx / Traefik) in front.

---

## Architecture

```
              ┌──────────────────────────┐
              │  iPhone running ccc app  │
              └─────────────┬────────────┘
                            │  HTTPS poll + APNs push
                            │  (or Bark fallback)
              ┌─────────────▼────────────┐
              │  Mac running apns-server │
              │  (this directory)        │
              └─────────────┬────────────┘
                            │  tmux send-keys / capture-pane
              ┌─────────────▼────────────┐
              │  tmux session "opia"     │
              │  └ claude (CLI agent)    │
              └──────────────────────────┘
```

`/chat/send` accepts a message from iPhone, injects into the `tmux` session, captures the reply, persists to `chat_history.jsonl`, and pushes the reply preview back to iPhone via APNs (or Bark).

---

## Endpoints (主要)

| Method | Path                  | Use                                        | Auth          |
|--------|-----------------------|--------------------------------------------|---------------|
| GET    | `/health`             | Health probe                               | none          |
| GET    | `/version`            | Server version                             | none          |
| POST   | `/chat/send`          | iPhone sends a chat message                | shared_secret |
| POST   | `/chat/stop`          | Stop the exact active XiaoKe App turn      | shared_secret |
| GET    | `/chat/history`       | iPhone fetches history                     | shared_secret |
| GET    | `/chain/sessions`     | List tmux sessions                         | shared_secret |
| POST   | `/chain/new_session`  | Create new tmux session                    | shared_secret |
| POST   | `/chain/switch`       | Set active tmux session                    | shared_secret |
| POST   | `/chain/abort`        | Send Ctrl+C to abort current reply         | shared_secret |
| POST   | `/tmux/send`          | Inject keys into a tmux session            | shared_secret |
| POST   | `/register-device-token` | iPhone reports its APNs device token   | none (公开)   |
| GET    | `/tool/schedule`      | List tool-dispatcher rules + run state      | shared_secret |
| POST   | `/tool/trigger`       | Manually fire a rule now (test/on-demand)   | shared_secret |

XiaoKe Stop requires the matching repository Stop hook from
`claude_hooks/ccc_stop_hook.sh`. `/chat/send` injects an opaque per-turn marker;
the hook returns that marker with the tmux session so a late completion can
only clear its own turn. Deploy `push.py` and the hook together. Copy the hook
to the configured Claude hook path and keep it executable; do not enable the
Android Stop control against a backend that has not installed the correlated
hook.

### 小克·工具版 (tool-version dispatcher)

A rule-driven scheduler (no AI / no LLM calls) baked into the server. At
scheduled times it injects a fixed trigger string into the main `claude` session
through the **same channel transport** a user chat message uses (with tmux
fallback), and writes the trigger to chat history so the user sees what woke the
assistant. The session then generates the real reply (e.g. a morning greeting)
with its own context/memory.

- Module: `tool_dispatcher.py` (`ScheduleStore` + `ToolDispatcher`); wired into
  `ServerState` and started from `run_server`.
- Schedule file: `tokens/tool_dispatcher.json` (auto-seeded on first run).
- Rules dedupe by local date (`last_fired`) so restarts don't double-fire; a
  missed slot past `catch_up_grace_minutes` is marked served instead of firing
  stale. A failed delivery retries on the next tick within the grace window.
- Config keys under `[server]`: `tool_dispatcher_enabled`,
  `tool_dispatcher_schedule_path`, `tool_dispatcher_seed_default`,
  `tool_dispatcher_tick_seconds` (see `config.example.toml`).
- `POST /tool/trigger` with `{"rule_id": "..."}` (or `{"text": "...", "contact_id": "..."}`)
  fires immediately without marking the rule served — handy for testing.

### 通话屏幕共享（`screen_capture_v1`，2026-10-21）

小克在通话里想看她手机屏幕时，在 VPS 上跑 `call-screenshot`（`/usr/local/bin/call-screenshot` → 本目录 `call_screenshot.py` 的软链），10 秒内打印截图绝对路径（存在 attachments 目录），再 Read 图片即可。失败退出码：2=没在通话/App 太旧，3=她没开屏幕共享授权，4=超时。

- 流程：CLI 在 `state/screen-capture/requests/` 投递请求 → `voice_call_ws.py` 找到声明了 `screen_capture_v1` 且上报 `screen_share_state{active:true}` 的通话，写 `pending/` 标记并下发 `{"type":"screen_capture_request","request_id"}` → App 抓当前一帧（MediaProjection，长边 ≤1600，JPEG≈80）→ `POST /voice-call/screen-capture?request_id=…`（native shared secret；只接受刚派发过、20s 内的一次性 id）→ 存图并写 `results/`，CLI 取到路径。
- 授权：App 在通话接通后请求一次系统屏幕录制授权（Android 14+ 每次都要她点同意，状态栏有系统共享指示）；通话页/悬浮窗上有「小克能看到屏幕」标识，点一下即关闭。通话结束自动释放。
- 旧客户端不声明能力，永远收不到这个帧；协议细节见 `screen_capture.py` 文件头。

### 通话自定义壁纸 / 长等待音乐

见 `voice-wallpaper/README.md` 与 `voice-ambience/README.md`（`ambience_no_mute_v1`）。

其它端点 (`/diary/*`, `/group/*`, `/favorites/*`, `/timeline/*`, `/todos/*`, `/calendar/*` etc.) 是给私有客户端用的, CcCompanion iOS app 不调它们。保留在 codebase 里因为 `push.py` 引用了对应 module — 删模块会让 import graph 散架。

---

### Crew 联系人 `kirocrew`（2026-10-31，和 `kiro` 并排试用；显示名原叫「Kiro Crew」，当天改成「Crew」，内部 id 不变）

方小南「先做一个 crew，然后功能和接口对齐 cli，稳定运行几天再看用哪个」。App 里多出一个
联系人「Crew」（服务端 `/chat/contacts` 下发，安卓不用发版），背后是东京
`kirocrew.service`（Crew 网关，用户 kiro，只绑 127.0.0.1:5476），人设 = 同一份 xiaoke
steering + 记忆库 MCP。原来的 `kiro`（kiro-cli）完全不动。

- 代码：`kirocrew_client.py`（经 `ssh -l kiro tokyo python3 -c <kirocrew_remote_bridge.py>`
  调网关，内部 secret 只在东京读）、`contacts/kirocrew.py`、push.py `_handle_kirocrew_*`。
- 一个固定的 Crew 会话槽 `ccc-kirocrew` = 持久会话；聊天指令 `/new /stop /model /effort
  /status /compact /help`（只有 /compact 走模型）；HTTP：`GET|POST /kirocrew/preferences`、
  `POST /kirocrew/new_session`、`/chat/stop`（contact_id + user_ts）。`/kirocrew/` 和 `/kiro/`
  同闸（只认 native pairing 的 X-Auth-Token）。
- `/kirocrew/preferences` 的 JSON 和 `/kiro/preferences` 同形（models / available_models /
  available_efforts / selection / current_model / context_usage_percent / header_display 对象），
  安卓用同一个解析器；`auto` 永远在模型表第一位；`effort_applies=false` 表示 auto 下网关忽略 effort。
- 终端 tab：`GET /tmux/capture?session=kirocrew` 每次（缓存 2.5 秒，发送后立即失效）经同一条
  ssh 桥读 `GET /api/chat/slots/ccc-kirocrew?limit=40`（Crew 自己已脱敏的会话记录：用户/回复/
  工具行含 purpose/input/output 截断），加正在生成的草稿尾巴，整页再过 `_kirocrew_terminal_redact`
  （header/查询串/常见 key 形状/长 hex 与 token_urlsafe 串）。**不读网关 journal**（那里有 5 分钟
  dashboard `?token=` 链接）。
  可输入（2026-10-31）：Crew 没有能接到固定槽上的 TUI（`kirocrew chat` 是独立 CLI 会话），所以终端
  是「中继」——capture 回 `state: ready, mode: crew_relay, writable: true`（生成中也可写）；
  `/tmux/send {session: kirocrew, keys, enter: true}` 整行 = 一条聊天消息（走 `_handle_kirocrew_chat_send`，
  进聊天记录、忙时排队，`/status` 等聊天指令照样认，metadata `via: terminal`）；`enter: false` 400
  `kirocrew_terminal_line_only`；`C-c` / `Escape` = 停止当前回复（不清队列）；其余 kiro 白名单键 200
  `ignored`；resize 200 `deferred`、release 200 `released: false`（都是空操作）。
- 自适应 forge（2026-10-31，和 kiro 同一套）：硬阈值 = min(`kirocrew_auto_forge_threshold_percent`(80) × 窗口,
  `kirocrew_auto_forge_max_tokens`(300k))，窗口取 Crew 上报的 `context_window_tokens`（无读数时查 kiro 的模型表，
  auto = 1M）→ 1M 模型 30%/300k、200k 模型 80%/160k；软阈值 × 75/80 发一次系统通知，它可以单独一行写
  `[[KIRO_FORGE]]`（从 app、草稿、微信 1000008、终端视图里都摘掉），本轮结束后 forge（队列里有消息就挪到下一条
  之前）。forge = 旧对话总结一轮 → `reset-conversation`（同 `/kirocrew/new_session`，epoch+1）→ seed（摘要 + 状态 +
  最近 80 条原文，跳过聊天指令与回执）→ 聊天行 `system:crew-forge` + 推送；总结失败/为空不换会话。手动：
  `POST /kirocrew/forge`。配置键 `kirocrew_auto_forge_*` 缺省沿用同名 `kiro_auto_forge_*`。东京 Crew 自己的
  autocompact 已调到上限 90%（全局 `session.autocompact_pct` + 槽 override），不会抢在这里之前；`/compact` 仍可手动。
- 企业微信（2026-10-31，自建应用 1000008「Crew」）：同 Kiro——`wecom_bridge.INBOUND_CONTACTS`
  / `IMAGE_CONTACTS` 放行，微信图片拷到东京以路径交给 Crew；只有回答微信消息的最终回复
  （`_kirocrew_finish`）和命令回执（`_kirocrew_reply_now`；微信里打 `/status` 等也认）才经
  `_mirror_reply_to_wecom` 写 outbox，取回的图片随 `files` 发成微信图片，`[bqb:]` 由
  wecom-channel 展开；`send_bell("kirocrew", …)` 可用。测试在 `_wecom_bridge_test`。
- 关掉：`config.toml` 里 `kirocrew_enabled = false`，重启 cc-companion（联系人消失，
  `tokens/chat_history_kirocrew.jsonl` 保留）；东京 `sudo systemctl disable --now kirocrew`。
  完整回滚（含 ~/.kiro 备份）见 vps-xiaoke-config `system-config/tokyo/README.md`「Crew」。
- 测试：`.venv/bin/python3 -m unittest _kirocrew_test`。

## Quick start

### 1. Deps

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

### 2. Config

```bash
cp config.example.toml config.toml
# 编辑 config.toml 填四件:
#   shared_secret  写接口鉴权 (留空 server 自动生成并写 ~/.ots/secret)
#   strict_auth    建议 true
#   [apns] 段     如果你有 Apple Developer 账号填 p8/team_id/key_id/bundle_id; 没有就跳过, 走 Bark fallback
```

### 3. Apple Developer p8 (可选, 不要 Bark 也行)

如果你想走原生 APNs 推送, 详见 [`../docs/01_apple_developer_p8_checklist.md`](../docs/01_apple_developer_p8_checklist.md)。

没 Apple Developer 账号 → 跳过, 装 [Bark](https://github.com/Finb/Bark) 走 free fallback。详见根目录 `README.md` 的 Quick Start 段。

### 4. Run

**前台调试:**

```bash
.venv/bin/python3 push.py --config config.toml
```

**后台 LaunchAgent (macOS):**

```bash
cp deploy/com.cccompanion.apns-server.plist ~/Library/LaunchAgents/
# 编辑 plist 把路径改成你的
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.cccompanion.apns-server.plist
launchctl print gui/$(id -u)/com.cccompanion.apns-server | grep state
```

### 5. Health check

```bash
curl http://127.0.0.1:8795/health
# {"ok": true, ...}

curl -H "X-Auth-Token: <你的 shared_secret>" http://127.0.0.1:8795/chain/sessions
# {"ok": true, "sessions": [...]}
```

---

## Testing

```bash
cd apns-server
.venv/bin/python3 -m pytest tests/ -q
```

---

## Troubleshooting

### Server 起不来

- `.venv/` 没装好 `requirements.txt` → `pip install -r requirements.txt`
- `config.toml` 不存在 → `cp config.example.toml config.toml`
- `.p8` 路径错或权限不对 → 路径绝对化 + `chmod 600`
- 详细错误看 `server.err.log`

### APNs push 失败 (装了 [apns] 配置)

- `410 BadDeviceToken` → iPhone 端 token 失效, 让 ccc app 重新启动 (重新 `registerForRemoteNotifications`)
- `403 ExpiredProviderToken` → JWT 过期, 检查 `date` / NTP 时钟漂移
- `400 BadTopic` → `apns-topic` 跟 `bundle_id` 不一致, 检查 config
- `429 TooManyRequests` → Apple rate limit, 降低频率

### iPhone 收不到推送 (App 是装好的)

- iOS "设置 → ccc → 通知" 全部允许了吗
- "后台 App 刷新" 打开了吗
- App 切到后台太久被 iOS 杀掉是正常 — 用 Bark fallback 兜底
- server 端 `tail -40 server.err.log` 看是不是真的发了

### Server 起来但 iPhone connect 不上

- mac 防火墙拦 8795: 系统设置 → 网络 → 防火墙 → 选项, 加 python3 进允许列表
- Tailscale / ZeroTier overlay 网络是不是通: `ping <iPhone overlay IP>` 在 mac 上
- `config.toml` `host = "0.0.0.0"` (绑所有网卡), 不是 `127.0.0.1`
- iPhone 端 server URL 用 overlay IP (Tailscale `100.x` 之类), 不是 `127.0.0.1`

---

## File layout

```
apns-server/
├── push.py                # 主 server (HTTP listen + route)
├── apns_client.py         # APNs HTTP/2 client
├── jwt_helper.py          # JWT ES256 signer
├── token_store.py         # shared_secret 持久化
├── device_token_store.py  # iPhone device token 持久化
├── task_queue.py          # 后台任务池
├── chat_history.py        # chat 持久化 + 搜索
├── usage.py               # Anthropic usage probe (可选)
├── config.example.toml    # 配置模板
├── deploy/                # LaunchAgent plist 等部署文件
├── requirements.txt       # Python 依赖
└── tests/                 # 单元测试 (.gitignored)
```

其它 `.py` 模块 (`diary`, `favorites`, `group_chat`, `rp_history`, `studyroom`, `timeline`, `todos`, `worklog`, `reminders`, `calendar_store`, `pet_state`, `tts`, `settings`, `diary_stream`, `studyroom_indexer`) 是给私有客户端用的 endpoint, CcCompanion iOS app 不调它们, 保留在 tree 里因为 `push.py` 引用了它们。
