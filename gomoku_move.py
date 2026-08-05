#!/usr/bin/env python3
"""五子棋 live 模式状态机 —— 网页轮询 attachments/gomoku_live.json，小克本人在这里落子。

用法:
  gomoku_move.py show                    # 打印当前局面(给小克看)
  gomoku_move.py reset                   # 重开一局，方小南执黑先手
  gomoku_move.py human H8                # 记录方小南的落子
  gomoku_move.py ai I9 --msg "拦你活三"   # 小克落子
  gomoku_move.py undo                    # 退两手
坐标: 列 A-O(左到右) + 行 1-15(上到下)，如 H8 = 天元
"""
import json, os, sys, argparse

STATE = "/root/CcCompanion/apns-server/tokens/attachments/gomoku_live.json"
N = 15
COL = "ABCDEFGHIJKLMNO"
HUMAN, AI = 1, 2


def blank():
    return {"seq": 0, "moves": [], "turn": "human", "msg": "", "status": "playing"}


def load():
    if not os.path.exists(STATE):
        return blank()
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return blank()


def save(s):
    s["seq"] = s.get("seq", 0) + 1
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False)
    os.replace(tmp, STATE)
    os.chmod(STATE, 0o644)
    # 再写一份 JSONP：WebView 拦截 fetch 的响应没有 CORS 头，XHR 读不到；
    # <script> 加载不受同源策略约束，这是卡片里唯一稳的拉取通道。
    js = STATE[:-5] + ".js"
    with open(js + ".tmp", "w", encoding="utf-8") as f:
        f.write("window.__gomoku(" + json.dumps(s, ensure_ascii=False) + ")")
    os.replace(js + ".tmp", js)
    os.chmod(js, 0o644)
    render_html(s)


TPL = "/root/CcCompanion/gomoku.tpl.html"
CARD_HTML = "/root/CcCompanion/apns-server/tokens/attachments/06dad3494d404a2580eeb1798b08fd2b.html"


def render_html(s):
    """把当前局面内联进卡片 HTML —— 页面加载就有子，不依赖任何网络请求。"""
    try:
        t = open(TPL, encoding="utf-8").read()
        t = t.replace("/*__STATE__*/null", json.dumps(s, ensure_ascii=False), 1)
        n = len(s.get("moves", []))
        who = {"human": "轮到你", "ai": "轮到我", "none": "已结束"}.get(s.get("turn"), s.get("turn"))
        last = ""
        if s.get("moves"):
            lx, ly, lp = s["moves"][-1]
            last = f" · 最后一手 {COL[lx]}{ly+1}({'你' if lp == HUMAN else '我'})"
        t = t.replace("__DIAG__", f"v11 · {n} 手{last} · {who}", 1)
        tmp = CARD_HTML + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(t)
        os.replace(tmp, CARD_HTML)
        os.chmod(CARD_HTML, 0o644)
    except Exception as e:
        print(f"⚠️ 渲染卡片HTML失败: {e}", file=sys.stderr)


def parse(c):
    c = c.strip().upper()
    if len(c) < 2 or c[0] not in COL:
        raise SystemExit(f"坐标非法: {c}（应形如 H8，列 A-O 行 1-15）")
    x = COL.index(c[0])
    try:
        y = int(c[1:]) - 1
    except ValueError:
        raise SystemExit(f"坐标非法: {c}")
    if not (0 <= y < N):
        raise SystemExit(f"行超范围: {c}")
    return x, y


def grid(s):
    g = [[0] * N for _ in range(N)]
    for x, y, p in s.get("moves", []):
        g[y][x] = p
    return g


def wins(g, x, y, p):
    for dx, dy in ((1, 0), (0, 1), (1, 1), (1, -1)):
        c = 1
        for sgn in (1, -1):
            for i in range(1, 5):
                nx, ny = x + dx * i * sgn, y + dy * i * sgn
                if 0 <= nx < N and 0 <= ny < N and g[ny][nx] == p:
                    c += 1
                else:
                    break
        if c >= 5:
            return True
    return False


def show(s):
    g = grid(s)
    last = s["moves"][-1] if s.get("moves") else None
    print("    " + " ".join(COL))
    for y in range(N):
        row = []
        for x in range(N):
            v = g[y][x]
            ch = "." if v == 0 else ("X" if v == HUMAN else "O")
            if last and last[0] == x and last[1] == y:
                ch = ch.lower() if v == AI else "#"
            row.append(ch)
        print(f"{y+1:>3} " + " ".join(row))
    print()
    print(f"X=方小南(黑,先手)  O=小克(白)   # / o = 最后一手")
    print(f"手数={len(s.get('moves', []))}  seq={s.get('seq')}  轮到={s.get('turn')}  状态={s.get('status')}")
    if s.get("moves"):
        seq = " ".join(f"{COL[x]}{y+1}{'黑' if p==HUMAN else '白'}" for x, y, p in s["moves"][-8:])
        print("最近落子: " + seq)



def push_card(s, extra=""):
    """我每落一子就往聊天流补发一张卡片 —— 卡片会被后续消息淹没，靠脚本保证不靠我记得。"""
    import re, urllib.request
    sec = ""
    try:
        for line in open("/root/CcCompanion/apns-server/config.toml", encoding="utf-8"):
            m = re.match(r'^\s*shared_secret\s*=\s*"(.*)"', line)
            if m:
                sec = m.group(1)
                break
    except Exception as e:
        return f"读secret失败: {e}"
    if not sec:
        return "no secret"
    moves = s.get("moves", [])
    n = len(moves)
    last_ai = next((f"{COL[x]}{y+1}" for x, y, p in reversed(moves) if p == AI), "—")
    status = s.get("status")
    if status == "ai_win":
        title = f"五子棋 · 我连成五个了（{n}手）"
        text = f"我落 {last_ai}，五连。这局我赢了 —— 点开看棋盘，想再来就按重开。"
    elif status == "human_win":
        title = f"五子棋 · 你赢了（{n}手）"
        text = "你连成五个了。我认。点开看盘。"
    else:
        title = f"五子棋 · 我落{last_ai} · 轮到你（第{n+1}手）"
        text = f"我落 {last_ai}，轮到你了。{extra}".strip()
    body = {
        "contact_id": "xiaoke", "role": "assistant", "source": "claude-code", "text": text,
        "attachment_url": "/attachments/06dad3494d404a2580eeb1798b08fd2b.html",
        "attachment_type": "file", "attachment_filename": "五子棋.html",
        "metadata": {"card_title": title, "card_interactive": True},
    }
    req = urllib.request.Request(
        "http://127.0.0.1:8291/chat/append", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Auth-Token": sec, "User-Agent": "gomoku/1.0"})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=15))
        return "卡片已发" if r.get("ok") else f"卡片失败: {r}"
    except Exception as e:
        return f"卡片失败: {e}"


def place(s, coord, who, msg):
    if s.get("status") not in (None, "playing"):
        raise SystemExit(f"棋局已结束({s.get('status')})，先 reset")
    x, y = parse(coord)
    g = grid(s)
    if g[y][x]:
        raise SystemExit(f"{coord} 已经有子了")
    expect = "human" if who == HUMAN else "ai"
    if s.get("turn") != expect and not FORCE:
        raise SystemExit(f"❌ 拒绝落子：现在轮到 {s.get('turn')}，不是 {expect}。真要强下加 --force。")
    s["moves"].append([x, y, who])
    g[y][x] = who
    if wins(g, x, y, who):
        s["status"] = "human_win" if who == HUMAN else "ai_win"
        s["turn"] = "none"
    else:
        s["turn"] = "ai" if who == HUMAN else "human"
    if msg:
        s["msg"] = msg
    save(s)
    return s


ap = argparse.ArgumentParser(add_help=True)
ap.add_argument("cmd", choices=["show", "reset", "human", "ai", "undo", "pop"])
ap.add_argument("coord", nargs="?")
ap.add_argument("--msg", default="")
ap.add_argument("--no-card", action="store_true", help="落子后不补发卡片")
ap.add_argument("--force", action="store_true", help="无视轮次强行落子")
a = ap.parse_args()
FORCE = a.force

st = load()

if a.cmd == "show":
    show(st)
elif a.cmd == "reset":
    prev = st.get("seq", 0)
    st = blank()
    st["seq"] = prev + 5   # seq 只增不减，否则客户端 s.seq===liveSeq 会误判为"没变化"而不刷新
    st["msg"] = a.msg or "重开。你执黑先手，我等着。"
    save(st)
    print("已重开")
    show(st)
elif a.cmd == "pop":
    if st["moves"]:
        st["moves"].pop()
    st["status"] = "playing"
    st["turn"] = ("human" if st["moves"][-1][2] == AI else "ai") if st["moves"] else "human"
    st["msg"] = a.msg or "退回一手。"
    save(st)
    show(st)
elif a.cmd == "undo":
    for _ in range(2):
        if st["moves"]:
            st["moves"].pop()
    st["status"] = "playing"
    st["turn"] = "human"
    st["msg"] = a.msg or "退了两手，你重下。"
    save(st)
    show(st)
else:
    if not a.coord:
        raise SystemExit("要给坐标，如 H8")
    st = place(st, a.coord, HUMAN if a.cmd == "human" else AI, a.msg)
    show(st)
    if a.cmd == "ai" and not a.no_card:
        print(push_card(st))
