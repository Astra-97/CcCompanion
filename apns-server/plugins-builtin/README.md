# 插件开发指南（服务端 MVP，2026-09-25）

CcCompanion 插件 = 一个静态页面小应用，托管在 apns-server 上，数据通过
插件级 KV 接口读写。本目录（`plugins-builtin/`）是**入库的内置插件区**；
运行时安装的插件与所有插件数据在 `apns-server/plugins/`（gitignore，
数据目录优先、内置目录回落）。

## 目录结构

```
plugins-builtin/
  <plugin-id>/            # 插件 id：小写字母/数字/连字符，^[a-z0-9][a-z0-9-]{0,39}$
    manifest.json         # 必填
    index.html            # entry（默认 index.html）
    app.js / style.css    # 任意静态文件
  _template/              # 骨架，id 以 _ 开头不会被列出/托管，复制它开新插件
```

`manifest.json`：

```json
{
  "id": "demo-checkin",        // 必须等于目录名
  "name": "打卡 Demo",
  "version": "0.1.0",
  "description": "...",
  "entry": "index.html"        // 可选，默认 index.html
}
```

保留路径：插件命名空间下的 `data/` 是 KV 接口（`/plugins/<id>/data/<doc>`），
不要在插件里放真实 `data/` 目录。

## 端点与鉴权

| 端点 | 鉴权 |
| --- | --- |
| `GET /plugins` 插件清单 | `_require_auth`：X-Auth-Token（shared_secret）或 PWA web session cookie |
| `GET /plugins/<id>/...` 静态托管 | 同上 |
| `GET /plugins/<id>/data/<doc>` | fail-closed 三选一：X-Auth-Token / web session（仅 GET）/ scoped token |
| `PUT /plugins/<id>/data/<doc>` | fail-closed 二选一：X-Auth-Token / scoped token（web session 不能写） |
| `POST /plugins/<id>/token` 签发 scoped token | 仅 X-Auth-Token（native pairing 闸门，web session 无权） |

shared_secret **绝不**下发到页面。App 内 WebView 走原生 bridge 代发请求
（主链路）；纯浏览器测试用 scoped token 直连 fetch（降级通道）。

静态托管响应头固定带：
`Content-Security-Policy: default-src 'self'`、`X-Content-Type-Options: nosniff`、
`Referrer-Policy: no-referrer`。**后果**：禁止一切远程脚本/样式/字体/图片 CDN，
也禁止内联 `<script>`/`style=""`——JS 和 CSS 必须放插件目录的独立文件。
路径穿越（`..`、反斜杠、符号链接）一律 404；静态文件只允许常见后缀
（html/js/css/json/svg/png/jpg/gif/webp/ico/txt/md/woff/woff2）。

## KV 协议（单文档，乐观并发）

每个插件的数据互相隔离，doc 名：`^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`。

- `GET /plugins/<id>/data/<doc>`
  → `200 {"ok":true,"doc","version":N,"body":<任意JSON>,"updated_at"}`
  文档不存在 → `404 {"ok":false,"error":"not_found","version":0}`（按 version=0/body=null 处理）。
- `PUT /plugins/<id>/data/<doc>`，body 为任意 JSON（≤256KB），**必须带**
  `If-Match: <int>` 头：
  - `If-Match: 0` —— 仅新建：文档已存在则 409；
  - `If-Match: N` —— 当前 version 恰好为 N 才写入，成功后 version 变为 N+1；
  - 缺/非法 If-Match → `428 {"error":"if_match_required"}`；
  - 版本不符 → `409 {"error":"version_conflict","version":<当前>,"body":<当前内容>}`，
    调用方用响应里的最新版本/内容**重读-合并-重试**（模板 `kvMutate` 已封装）。

并发写由服务端全局锁 + `.tmp`→`replace` 原子写保证不丢版本。

## scoped token

- 签发：`POST /plugins/<id>/token`，头 `X-Auth-Token: <shared_secret>`，
  body `{"ttl_seconds": 86400}`（可选，默认 7 天，上限 30 天）。
  → `{"ok":true,"token":"p1....","expires_at":<epoch>,"scope":"/plugins/<id>/data/* GET+PUT"}`
- 使用：`X-Plugin-Token: <token>`（或 `Authorization: Bearer <token>`）。
- 边界：token 绑定插件 id，只在 `/plugins/<同id>/data/*` 的 GET/PUT 处理器里校验，
  对任何其他端点（包括别的插件）一律 401。格式 `p1.<b64url payload>.<hmac-sha256>`，
  以 shared_secret 为 key 无状态签名，token 本身不泄露 secret。

## App 端 bridge 契约（JS 侧调用约定）

App 在插件 WebView 注入 `window.CCBridge`，页面检测到即走代发通道
（页面不接触任何凭据）：

```js
// 读文档；不存在时 resolve { version: 0, body: null }
const { version, body } = await window.CCBridge.readData(doc);

// 写文档；version 为上面读到的值（新建传 0）。成功 resolve { version: 新版本 }；
// 版本冲突时 reject { status: 409, version: <当前>, body: <当前内容> }，页面重读合并重试。
const { version: v2 } = await window.CCBridge.writeData(doc, body, version);

// 把一段文本发给小克（可选能力，MVP 页面不依赖它也能跑）
window.CCBridge.sendToXiaoke("帮我记一下：今天已打卡");
```

无 bridge 时页面降级为 fetch + scoped token（见 `_template/app.js`，两者同一套
`kvRead/kvWrite/kvMutate` 封装）。demo-checkin 可在纯浏览器里跑通：先
`POST /plugins/demo-checkin/token` 拿 token，粘贴到页面底部调试框即可。

## 部署注意

改了服务端代码（push.py / plugins_store.py）需重启 `cc-companion.service` 生效；
插件本身（manifest/静态文件）每次请求现读，**改插件不用重启**。
