#!/usr/bin/env python3
"""kt-vpn-usage — VPN 节点用量日报数字员工。

链路：kt-tick 节律 vpn-usage-daily → kt-event-router 规则 ops.vpn-usage-daily
→ KTAIorg/kt-agent-skills workflow vpn-usage-daily.yml → 本脚本。

只读采集：每节点 3x-ui 面板 API（inbounds 流量）+ KiwiVM API（月配额计数），
与上一轮 ledger 快照求 24h 增量，汇总成 Telegram HTML 日报。

环境变量（CI 由 repo Secrets 注入；本机回落 `kt secret get` 由调用方自行导出）：
  VPN_PANEL_USER / VPN_PANEL_PASS   3x-ui 面板管理员（当前各节点共用）
  KIWIVM_API_KEY                    KiwiVM API key（VEID 在 nodes.json）
  TELEGRAM_BOT_TOKEN                发报 bot（wenzi）
  TG_CHAT_ID                        目标会话（DM 或群聊 -100xxx）
  DAILY_WARN_GB（默认 50）          单日计费用量告警阈值
  MONTH_WARN_PCT（默认 80）         月配额百分比告警阈值
"""

import argparse
import html
import http.cookiejar
import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

UA = "kt-vpn-usage/1.0"
TZ = ZoneInfo("Asia/Shanghai")
HERE = Path(__file__).resolve().parent
DEFAULT_NODES = HERE.parent / "nodes.json"
DEFAULT_LEDGER = Path.home() / ".kt" / "vpn-usage" / "ledger.jsonl"


def http_json(url, method="GET", body=None, headers=None, timeout=20):
    data = None
    hdrs = {"User-Agent": UA}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode() or "{}")


def gb(n):
    return n / 1e9


class Panel:
    """3x-ui 面板 API：GET 首页取 csrf-token + cookie → POST /login → 业务接口。"""

    def __init__(self, base):
        self.base = base.rstrip("/")
        self.cj = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(self.cj)]
        if self.base.startswith("https://"):
            # 面板证书为自签（10 年期，见 issue #23），跳过 CA 校验；仅作用于面板 opener
            handlers.append(urllib.request.HTTPSHandler(
                context=ssl._create_unverified_context()))
        self.opener = urllib.request.build_opener(*handlers)
        self.opener.addheaders = [("User-Agent", UA)]

    def _open(self, path, method="GET", body=None, headers=None):
        url = self.base + path
        data = None
        hdrs = {}
        if headers:
            hdrs.update(headers)
        if body is not None:
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        with self.opener.open(req, timeout=20) as resp:
            return resp.status, resp.read().decode()

    def login(self, user, password):
        _, html = self._open("/")
        m = re.search(r'name="csrf-token"\s+content="([^"]+)"', html)
        if not m:
            raise RuntimeError("panel page has no csrf-token meta")
        status, body = self._open("/login", method="POST",
                                  body={"username": user, "password": password},
                                  headers={"X-Csrf-Token": m.group(1)})
        obj = json.loads(body)
        if not obj.get("success"):
            raise RuntimeError("panel login failed: %s" % obj.get("msg"))

    def inbounds(self):
        _, body = self._open("/panel/api/inbounds/list")
        obj = json.loads(body)
        if not obj.get("success"):
            raise RuntimeError("inbounds/list failed: %s" % obj.get("msg"))
        return obj["obj"]


def kiwivm_info(veid, api_key):
    url = ("https://api.64clouds.com/v1/getServiceInfo?"
           + urllib.parse.urlencode({"veid": veid, "api_key": api_key}))
    _, obj = http_json(url)
    plan = obj.get("plan") or ""
    quota_gb = None
    m = re.search(r"-(\d+)t(?:-|$)", plan)
    if m:
        quota_gb = int(m.group(1)) * 1024
    return {
        "month_bytes": obj.get("data_counter") or 0,
        "plan": plan,
        "quota_gb": quota_gb,
    }


def load_ledger_prev(ledger: Path, node):
    if not ledger.exists():
        return None
    prev = None
    for line in ledger.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("node") == node:
            prev = rec
    return prev


def collect_node(node, panel_user, panel_pass, kiwivm_key):
    errors = []
    inbounds = []
    base = "%s://%s:%s%s" % (node.get("panel_scheme", "https"), node["host"], node["panel_port"], node["panel_base_path"])
    try:
        p = Panel(base)
        p.login(panel_user, panel_pass)
        raw = p.inbounds()
        client_stats = {}
        for ib in raw:
            raw_settings = ib.get("settings")
            if isinstance(raw_settings, str):
                settings = json.loads(raw_settings or "{}")
            elif isinstance(raw_settings, dict):
                settings = raw_settings
            else:
                settings = {}
            stat_map = {s.get("email"): s for s in (ib.get("clientStats") or [])}
            clients = []
            for c in settings.get("clients", []):
                email = c.get("email") or c.get("comment") or c.get("id", "?")
                clients.append(email)
                s = stat_map.get(email) or {}
                up, down, total = s.get("up") or 0, s.get("down") or 0, s.get("total") or 0
                if email in client_stats:
                    # v3.8.5: 同 email clientStats 跨入站重复显示全局值，取最大即全局
                    old = client_stats[email]
                    client_stats[email] = [max(old[0], up), max(old[1], down), max(old[2], total)]
                else:
                    client_stats[email] = [up, down, total]
            inbounds.append({
                "id": ib.get("id"), "port": ib.get("port"),
                "protocol": ib.get("protocol"), "remark": ib.get("remark"),
                "enable": bool(ib.get("enable")),
                "up": ib.get("up") or 0, "down": ib.get("down") or 0,
                "clients": clients,
            })
    except Exception as e:  # noqa: BLE001 — 单节点失败不能拖垮整份日报
        errors.append("panel: %s" % e)
        client_stats = {}

    kv = None
    if node.get("kiwivm_veid") and kiwivm_key:
        try:
            kv = kiwivm_info(node["kiwivm_veid"], kiwivm_key)
        except Exception as e:  # noqa: BLE001
            errors.append("kiwivm: %s" % e)

    return {"inbounds": inbounds, "clients": client_stats, "kiwivm": kv, "errors": errors}


def fmt_gb(n):
    return "%.1fG" % gb(n) if n >= 1e9 else "%.0fM" % (n / 1e6)


def render_table(header, rows):
    """等宽管线表格：与 kt-agent-runtime RenderMarkdownToTelegramHTML 的
    wrapPipeTables 输出同构——保留 | 管道符 + 空格列对齐，包 <pre> 等宽渲染，
    竖线即表格视觉。中文按 2 列宽计算对齐（CJK 双宽）。"""
    def width(s):
        return sum(2 if ord(ch) > 0x2E7F else 1 for ch in s)

    def pad(s, w):
        return s + " " * (w - width(s))

    widths = [width(h) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], width(cell))
    def line(cells):
        return "| " + " | ".join(pad(c, w) for c, w in zip(cells, widths)) + " |"
    out = [line(header), "|" + "|".join("-" * (w + 2) for w in widths) + "|"]
    for row in rows:
        out.append(line(row))
    return "<pre>%s</pre>" % html.escape("\n".join(out), quote=False)


def build_report(now, nodes_results, prev_map, daily_warn_gb, month_warn_pct):
    def esc(s):
        return html.escape(str(s), quote=False)

    lines = ["📊 <b>VPN 用量日报</b> · %s" %
             now.strftime("%Y-%m-%d %a %H:%M").replace("Mon", "一").replace("Tue", "二")
             .replace("Wed", "三").replace("Thu", "四").replace("Fri", "五")
             .replace("Sat", "六").replace("Sun", "日")]
    alerts = []

    ib_icon = {"hysteria": "🟢", "vless": "🔵", "vmess": "🟣", "trojan": "🟠",
               "shadowsocks": "⚪", "wireguard": "🟡", "tuic": "🟤"}

    def bar(pct, width=20):
        filled = max(0, min(width, round(pct / 100 * width)))
        return "▰" * filled + "▱" * (width - filled)

    for node, res in nodes_results:
        lines.append("")
        lines.append("🖥 <b>%s</b>　<i>%s</i>" % (esc(node["name"]), esc(node.get("label", ""))))
        if res["errors"]:
            for e in res["errors"]:
                alerts.append("%s 采集异常: %s" % (node["name"], e))
                lines.append("⚠️ %s" % esc(e))

        day_up = day_down = 0
        prev = prev_map.get(node["name"])
        prev_ib = (prev or {}).get("inbounds") or {}
        for ib in res["inbounds"]:
            key = str(ib["id"])
            p = prev_ib.get(key) or [0, 0]
            up_d = max(ib["up"] - p[0], 0)
            down_d = max(ib["down"] - p[1], 0)
            if ib["up"] < p[0] or ib["down"] < p[1]:
                up_d, down_d = ib["up"], ib["down"]  # 计数器重置（入站重建）
            day_up += up_d
            day_down += down_d

        ib_rows = []
        for ib in res["inbounds"]:
            icon = ib_icon.get((ib["protocol"] or "").lower(), "▫️")
            ib_rows.append(["%s %s" % (icon, ib["remark"] or ib["protocol"]),
                            "⬆️ " + fmt_gb(ib["up"]), "⬇️ " + fmt_gb(ib["down"])])
        if ib_rows:
            lines.append("")
            lines.append("⬇️ <b>入站累计</b>")
            lines.append(render_table(["入站", "上行 ⬆️", "下行 ⬇️"], ib_rows))

        cs = res.get("clients") or {}
        prev_cs = (prev or {}).get("clients") or {}
        if cs:
            lines.append("")
            lines.append("👤 <b>客户端</b>（%d）" % len(cs))
            cli_rows = []
            for email in sorted(cs):
                up, down, total = cs[email]
                name = email
                if prev:
                    p = prev_cs.get(email)
                    if p:
                        d_up, d_down = max(up - p[0], 0), max(down - p[1], 0)
                        if up < p[0] or down < p[1]:
                            d_up, d_down = up, down
                        if gb(d_up + d_down) >= daily_warn_gb:
                            alerts.append("👤 客户端 %s 单日计费 %.1f GB ≥ %.0f GB" %
                                          (email, gb(d_up + d_down), daily_warn_gb))
                        cli_rows.append([name, "⬆️ " + fmt_gb(up), "⬇️ " + fmt_gb(down),
                                         "⬆️ " + fmt_gb(d_up), "⬇️ " + fmt_gb(d_down)])
                        continue
                    elif prev_cs:
                        # 上一轮已有客户端快照但无此 email → 基线之后新出现，需人工确认
                        name += " 🆕"
                        alerts.append("👤 出现新客户端 %s（确认是否授权添加，防盗用）" % email)
                cli_rows.append([name, "⬆️ " + fmt_gb(up), "⬇️ " + fmt_gb(down),
                                 fmt_gb(total) if total > 0 else "—", "—"])
            lines.append(render_table(
                ["客户端", "累计⬆️", "累计⬇️", "今日⬆️", "今日⬇️"], cli_rows))

        kv = res["kiwivm"]
        if kv:
            quota = kv.get("quota_gb") or node.get("monthly_quota_gb")
            month_gb = gb(kv["month_bytes"])
            lines.append("")
            if quota:
                pct = month_gb / quota * 100
                lines.append("💾 <b>月配额</b>　<code>%.1f / %d GB</code>" % (month_gb, quota))
                lines.append("<code>%s %.0f%%</code>" % (bar(pct), pct))
                if pct >= month_warn_pct:
                    alerts.append("💾 %s 月配额已用 %.0f%%" % (node["name"], pct))
            else:
                lines.append("💾 <b>月配额</b>　<code>%.1f GB</code>" % month_gb)
        if prev:
            lines.append("")
            lines.append("📈 <b>全天增量</b>　⬆️ %s · ⬇️ %s · 计费 %s" %
                         (fmt_gb(day_up), fmt_gb(day_down), fmt_gb(day_up + day_down)))
            day_bill = day_up + day_down
            if gb(day_bill) >= daily_warn_gb:
                alerts.append("📈 %s 单日计费 %.1f GB ≥ %.0f GB" %
                              (node["name"], gb(day_bill), daily_warn_gb))
        else:
            lines.append("📈 <b>全天增量</b>　首轮基线，明起出增量")

    lines.append("")
    if alerts:
        for a in alerts:
            lines.append("🚨 %s" % esc(a))
    else:
        lines.append("✅ 无异常")
    return "\n".join(lines), alerts


def html_to_markdown(text):
    """把 build_report 的 Telegram HTML 子集转回富文本 markdown：
    <b>→**、<i>→*、<code>/<pre>→`/```。表格 <pre> 内容本就是管线 markdown，
    转围栏代码块外的处理：sendRichMessage 的 markdown 原生渲染管线表格，
    故 <pre> 内的管线表格去掉围栏直接透传。动态字段已在源端转义过，
    此处反转义回纯文本。"""
    def unescape(s):
        return s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")

    out, i = [], 0
    pre_buf, in_pre = [], False
    while i < len(text):
        if text.startswith("<pre>", i):
            in_pre = True
            i += 5
        elif text.startswith("</pre>", i):
            in_pre = False
            content = unescape("".join(pre_buf))
            pre_buf = []
            # 管线表格透传给原生渲染；非表格 pre 内容退回围栏代码块。
            # 表格前后必须空行分隔：否则 markdown 解析器按软换行把表格并进
            # 前一段 paragraph，管道符原样输出不渲染成 table 块（实测探针确认）。
            if content.lstrip().startswith("|"):
                out.append("\n%s\n" % content)
            else:
                out.append("```\n%s\n```" % content)
            i += 6
        elif in_pre:
            pre_buf.append(text[i])
            i += 1
        elif text.startswith("<b>", i):
            out.append("**"); i += 3
        elif text.startswith("</b>", i):
            out.append("**"); i += 4
        elif text.startswith("<i>", i):
            out.append("*"); i += 3
        elif text.startswith("</i>", i):
            out.append("*"); i += 4
        elif text.startswith("<code>", i):
            out.append("`"); i += 6
        elif text.startswith("</code>", i):
            out.append("`"); i += 7
        elif text[i] == "<":
            j = text.find(">", i)
            i = len(text) if j < 0 else j + 1  # 丢弃其余未知标签
        else:
            out.append(text[i])
            i += 1
    md = unescape("".join(out))
    return re.sub(r"\n{3,}", "\n\n", md)


def send_telegram(token, chat_id, text):
    """优先 Bot API sendRichMessage（markdown 原生表格渲染，同 kt-agent-runtime
    lib/telegram/rich.go 链路）；群不支持时回落 sendMessage+HTML。"""
    url = "https://api.telegram.org/bot%s/sendRichMessage" % token
    md = html_to_markdown(text)
    status, obj = http_json(url, method="POST", body={
        "chat_id": chat_id, "rich_message": {"markdown": md[:3900]},
    })
    if obj.get("ok"):
        return status
    # 回落：sendRichMessage 不被支持（description 含 method not found / not supported）
    err = str(obj.get("description", "")).lower()
    if "method" not in err and "not found" not in err:
        raise RuntimeError("telegram sendRichMessage failed: %s" % obj)
    url = "https://api.telegram.org/bot%s/sendMessage" % token
    status, obj = http_json(url, method="POST", body={
        "chat_id": chat_id, "text": text[:3900],
        "parse_mode": "HTML", "disable_web_page_preview": True,
    })
    if not obj.get("ok"):
        raise RuntimeError("telegram send failed: %s" % obj)
    return status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="打印报告但不发送、不写台账")
    ap.add_argument("--nodes", default=str(DEFAULT_NODES))
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    args = ap.parse_args()

    panel_user = os.environ.get("VPN_PANEL_USER")
    panel_pass = os.environ.get("VPN_PANEL_PASS")
    kiwivm_key = os.environ.get("KIWIVM_API_KEY")
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat = os.environ.get("TG_CHAT_ID")
    daily_warn = float(os.environ.get("DAILY_WARN_GB", "50"))
    month_warn = float(os.environ.get("MONTH_WARN_PCT", "80"))

    nodes = json.loads(os.environ.get("VPN_NODES_JSON")
                       or Path(args.nodes).read_text())  # 仓 public，节点清单走 secret 注入
    ledger = Path(args.ledger)
    now = datetime.now(TZ)

    nodes_results = []
    prev_map = {}
    fresh = []
    for node in nodes:
        res = collect_node(node, panel_user, panel_pass, kiwivm_key)
        nodes_results.append((node, res))
        prev = load_ledger_prev(ledger, node["name"])
        if prev:
            prev_map[node["name"]] = prev
        fresh.append({
            "ts": now.isoformat(timespec="seconds"),
            "node": node["name"],
            "inbounds": {str(ib["id"]): [ib["up"], ib["down"]] for ib in res["inbounds"]},
            "clients": res.get("clients") or {},
            "month_bytes": (res["kiwivm"] or {}).get("month_bytes"),
        })

    report, _alerts = build_report(now, nodes_results, prev_map, daily_warn, month_warn)
    print(report)

    if args.dry_run:
        print("[dry-run] 不发送、不写台账")
        return 0

    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a") as f:
        for rec in fresh:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    if not (tg_token and tg_chat):
        print("::warning::TELEGRAM_BOT_TOKEN/TG_CHAT_ID 未配置，跳过推送（台账已写）")
        return 0
    send_telegram(tg_token, tg_chat, report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
