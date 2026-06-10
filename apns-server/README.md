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
| GET    | `/chat/history`       | iPhone fetches history                     | shared_secret |
| GET    | `/chain/sessions`     | List tmux sessions                         | shared_secret |
| POST   | `/chain/new_session`  | Create new tmux session                    | shared_secret |
| POST   | `/chain/switch`       | Set active tmux session                    | shared_secret |
| POST   | `/chain/abort`        | Send Ctrl+C to abort current reply         | shared_secret |
| POST   | `/tmux/send`          | Inject keys into a tmux session            | shared_secret |
| POST   | `/register-device-token` | iPhone reports its APNs device token   | none (公开)   |
| GET    | `/tool/schedule`      | List tool-dispatcher rules + run state      | shared_secret |
| POST   | `/tool/trigger`       | Manually fire a rule now (test/on-demand)   | shared_secret |

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

其它端点 (`/diary/*`, `/group/*`, `/favorites/*`, `/timeline/*`, `/todos/*`, `/calendar/*` etc.) 是给私有客户端用的, CcCompanion iOS app 不调它们。保留在 codebase 里因为 `push.py` 引用了对应 module — 删模块会让 import graph 散架。

---

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
