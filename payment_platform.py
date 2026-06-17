#!/usr/bin/env python3
"""
Hermes Pay Unified — 统一国际支付网关
融合方案A(手动确认) + 方案B(多通道自动API)

架构:
  API Gateway → 统一支付平台 (:9188) → 多种支付渠道 → 回调充值

渠道:
  - alipay:    支付宝个人收款码 (手动确认)
  - wechat:    微信个人收款码 (手动确认)
  - stripe:    Stripe 国际信用卡 (自动回调)
  - paypal:    PayPal (自动回调)
  - crypto:    USDT/TRC20 加密货币 (手动确认)
"""
import argparse, hashlib, json, os, sqlite3, sys, time, uuid
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote
import urllib.request

# ─── 配置 ─────────────────────────────────────────────
PAY_DIR = Path(__file__).parent
DB_PATH = PAY_DIR / "payments.db"
QR_DIR = PAY_DIR / "qrcodes"
SITE_NAME = "Hermes Pay"
SITE_URL = "http://47.97.68.146"
WEBHOOK_URL = "http://127.0.0.1:9199/api/payment/webhook"
TOKEN_PER_YUAN = 100000

# 渠道配置（可动态切换）
CHANNELS = {
    "alipay": {"name": "支付宝", "icon": "💳", "enabled": True, "manual": True,
               "qr": str(QR_DIR / "alipay_qr.png"), "account": "1278373034@qq.com"},
    "wechat": {"name": "微信支付", "icon": "💚", "enabled": True, "manual": True,
               "qr": str(QR_DIR / "wechat_qr.png"), "account": "gordon38steam"},
    "stripe": {"name": "💳 Stripe", "icon": "💳", "enabled": False, "manual": False,
               "note": "Stripe 不向中国大陆个人开放注册，改用PayPal"},
    "paypal": {"name": "PayPal", "icon": "🅿️", "enabled": False, "manual": False,
               "client_id": "", "secret": "", "email": "1278373034@qq.com"},
    "crypto": {"name": "USDT (TRC20)", "icon": "₿", "enabled": True, "manual": True,
               "address": "TXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX", "network": "TRC20"},
}

# 管理密码
ADMIN_PASSWORD = hashlib.sha256(os.urandom(16)).hexdigest()[:8]

# ─── 数据库 ─────────────────────────────────────────────
def get_db():
    db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db

def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_no TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            amount_yuan REAL NOT NULL,
            tokens INTEGER NOT NULL,
            channel TEXT NOT NULL DEFAULT 'alipay',
            description TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            callback_url TEXT DEFAULT '',
            channel_order_id TEXT DEFAULT '',
            created_at REAL NOT NULL,
            paid_at REAL,
            confirmed_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
        CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
        CREATE INDEX IF NOT EXISTS idx_orders_channel ON orders(channel);
    """)
    QR_DIR.mkdir(parents=True, exist_ok=True)
    db.close()

# ─── 业务逻辑 ─────────────────────────────────────────
def create_order(user_id, amount_yuan, channel="alipay", callback_url=WEBHOOK_URL):
    order_no = f"PAY{int(time.time()*1000)}{uuid.uuid4().hex[:6].upper()}"
    tokens = int(amount_yuan * TOKEN_PER_YUAN)
    
    ch = CHANNELS.get(channel, CHANNELS["alipay"])
    if not ch["enabled"]:
        return {"success": False, "error": f"支付方式 {channel} 未启用"}
    
    db = get_db()
    db.execute("""INSERT INTO orders 
        (order_no, user_id, amount_yuan, tokens, channel, callback_url, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [order_no, user_id, amount_yuan, tokens, channel, callback_url, "pending", time.time()])
    db.commit()
    db.close()
    
    return {
        "success": True,
        "order_no": order_no,
        "amount_yuan": amount_yuan,
        "tokens": tokens,
        "channel": channel,
        "pay_url": f"{SITE_URL}/pay/{channel}/{order_no}",
        "manual": ch["manual"],
    }

def confirm_order(order_no):
    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE order_no=? AND status='pending'", [order_no]).fetchone()
    if not order:
        db.close()
        return None, "订单不存在或已处理"
    
    now = time.time()
    channel = order["channel"]
    db.execute("UPDATE orders SET status='paid', paid_at=?, confirmed_at=? WHERE order_no=?",
               [now, now, order_no])
    db.commit()
    db.close()
    
    # 回调API Gateway
    cb_url = order["callback_url"]
    if cb_url:
        try:
            data = json.dumps({
                "out_trade_no": order_no,
                "trade_status": "TRADE_SUCCESS",
                "total_amount": str(order["amount_yuan"]),
                "user_id": order["user_id"],
                "channel": channel,
            }).encode()
            req = urllib.request.Request(cb_url, data=data,
                headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=10)
        except:
            pass
    
    return dict(order), None

def get_order(order_no):
    db = get_db()
    o = db.execute("SELECT * FROM orders WHERE order_no=?", [order_no]).fetchone()
    db.close()
    return dict(o) if o else None

def list_orders(channel=None, status=None, page=1, limit=50):
    db = get_db()
    where = []
    params = []
    if channel:
        where.append("channel=?")
        params.append(channel)
    if status:
        where.append("status=?")
        params.append(status)
    sql = "SELECT * FROM orders"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params += [limit, (page-1)*limit]
    rows = db.execute(sql, params).fetchall()
    db.close()
    return [dict(r) for r in rows]

def get_stats():
    db = get_db()
    r = db.execute("SELECT channel, status, COUNT(*) as cnt FROM orders GROUP BY channel, status").fetchall()
    db.close()
    stats = {}
    for row in r:
        ch = row["channel"]
        if ch not in stats:
            stats[ch] = {"total": 0, "pending": 0, "paid": 0}
        stats[ch]["total"] += row["cnt"]
        stats[ch][row["status"]] = row["cnt"]
    return stats

# ─── HTTP Handler ────────────────────────────────────
class PayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    
    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    
    def _send_html(self, html, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    
    def _check_auth(self):
        return self.headers.get("Authorization", "").replace("Bearer ", "").strip() == ADMIN_PASSWORD
    
    def _render_channel_page(self, order):
        channel = order["channel"]
        ch = CHANNELS.get(channel, CHANNELS["alipay"])
        
        if channel == "crypto":
            return self._render_crypto_page(order, ch)
        elif channel in ("alipay", "wechat"):
            return self._render_qr_page(order, ch)
        elif channel == "stripe":
            return self._render_stripe_page(order, ch)
        elif channel == "paypal":
            return self._render_paypal_page(order, ch)
        else:
            return self._send_html("<h1>未知支付方式</h1>", 400)
    
    def _render_qr_page(self, order, ch):
        icon = ch["icon"]
        name = ch["name"]
        qr_exists = os.path.exists(ch["qr"])
        self._send_html(f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{SITE_NAME} - {name}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,sans-serif;background:#f5f5f5;display:flex;justify-content:center;padding:40px 16px}}
.card{{background:#fff;border-radius:16px;padding:32px;max-width:420px;width:100%;box-shadow:0 2px 12px rgba(0,0,0,.08);text-align:center}}
.price{{font-size:48px;font-weight:700;color:#333;margin:16px 0 8px}}
.token{{color:#1677ff;font-size:14px;margin-bottom:24px}}
.qr-box{{background:#fafafa;border:2px dashed #ddd;border-radius:12px;padding:20px;margin:16px 0;min-height:240px;display:flex;flex-direction:column;align-items:center;justify-content:center}}
.qr-box img{{width:220px;height:220px;border-radius:8px}}
.account{{background:#f0f5ff;border-radius:8px;padding:12px;margin:12px 0;font-size:14px;color:#333}}
.btn{{padding:14px 40px;border:none;border-radius:8px;font-size:16px;cursor:pointer;margin:12px 0;display:inline-block}}
.btn.primary{{background:#1677ff;color:#fff;width:100%}}
.status{{padding:12px;margin:12px 0;border-radius:8px;font-size:14px;display:none}}
.status.pending{{display:block;background:#fff7e6;color:#d48806}}
.status.success{{display:block;background:#f6ffed;color:#389e0d}}
</style></head><body>
<div class="card">
<div style="font-size:40px;margin-bottom:8px">{icon}</div>
<h2 style="color:#333;font-size:20px">{name} 支付</h2>
<div class="price">¥{order["amount_yuan"]:.0f}</div>
<div class="token">= {order["tokens"]:,} tokens</div>

<div class="qr-box">
{"<img src='/pay/qrcode/" + channel + "' alt='收款码'><p style='color:#999;font-size:13px;margin-top:12px'>请使用" + name + "扫码付款</p>" if qr_exists else "<p style='color:#999'>⏳ 收款码待上传<br><span style='font-size:12px'>请手动转账到下方账号</span></p>"}
</div>

<div class="account">
📱 收款账号<br><strong>{ch["account"]}</strong>
</div>

<button class="btn primary" onclick="doPay()">✅ 我已付款</button>

<div class="status pending" id="sPending">⏳ 等待确认中...</div>
<div class="status success" id="sSuccess">✅ 支付成功！Token 已到账</div>
</div>
<script>
function doPay(){{
    var btn=event.target;btn.disabled=true;btn.textContent='⏳ 确认中...';
    document.getElementById('sPending').style.display='block';
    fetch('/pay/confirm/{order["order_no"]}',{{method:'POST'}})
    .then(r=>r.json()).then(d=>{{
        if(d.success){{document.getElementById('sPending').style.display='none';
        document.getElementById('sSuccess').style.display='block';
        btn.textContent='✅ 已确认';}}
        else{{alert('确认失败');btn.disabled=false;btn.textContent='✅ 我已付款';}}
    }})
}}
</script>
</body></html>""")
    
    def _render_crypto_page(self, order, ch):
        self._send_html(f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{SITE_NAME} - USDT</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,sans-serif;background:#f5f5f5;display:flex;justify-content:center;padding:40px 16px}}
.card{{background:#fff;border-radius:16px;padding:32px;max-width:420px;width:100%;box-shadow:0 2px 12px rgba(0,0,0,.08);text-align:center}}
.price{{font-size:48px;font-weight:700;color:#333;margin:16px 0 8px}}
.rate{{color:#666;font-size:14px;margin-bottom:16px}}
.address-box{{background:#f0f5ff;border-radius:12px;padding:16px;margin:16px 0;word-break:break-all;font-size:13px;color:#1677ff;font-family:monospace}}
.copy-btn{{padding:10px 24px;border:1px solid #1677ff;border-radius:8px;background:#fff;color:#1677ff;cursor:pointer;font-size:14px;margin:8px 0}}
.btn{{padding:14px 40px;border:none;border-radius:8px;font-size:16px;cursor:pointer;margin:12px 0;width:100%}}
.btn.primary{{background:#1677ff;color:#fff}}
.network{{background:#fff7e6;color:#d48806;border-radius:8px;padding:10px;font-size:13px;margin:12px 0}}
</style></head><body>
<div class="card">
<div style="font-size:40px;margin-bottom:8px">{ch["icon"]}</div>
<h2 style="color:#333;font-size:20px">USDT ({ch["network"]})</h2>
<div class="price">≈ ¥{order["amount_yuan"]:.0f}</div>
<div class="rate">≈ ${order["amount_yuan"]/7.2:.2f} USDT</div>
<div class="network">⚠️ 仅支持 {ch["network"]} 网络，其他网络会导致资金丢失</div>
<div style="font-size:14px;color:#333;margin-top:16px">转账到以下地址：</div>
<div class="address-box" id="addr">{ch["address"]}</div>
<button class="copy-btn" onclick="copyAddr()">📋 复制地址</button>
<p style="color:#999;font-size:13px;margin:12px 0">转账完成后点击下方按钮</p>
<button class="btn primary" onclick="doPay()">✅ 我已转账</button>
<div class="status" id="sPending" style="display:none;padding:12px;border-radius:8px;background:#fff7e6;color:#d48806">⏳ 等待确认中（通常1-5分钟）</div>
</div>
<script>
function copyAddr(){{
    navigator.clipboard.writeText(document.getElementById('addr').textContent);
    event.target.textContent='✅ 已复制';
}}
function doPay(){{
    event.target.disabled=true;event.target.textContent='⏳ 等待确认...';
    document.getElementById('sPending').style.display='block';
    fetch('/pay/confirm/{order["order_no"]}',{{method:'POST'}})
    .then(r=>r.json()).then(d=>{{
        if(d.success) location.reload();
        else alert('确认失败');
    }})
}}
</script>
</body></html>""")
    
    def _render_stripe_page(self, order, ch):
        self._send_html(f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{SITE_NAME} - 国际支付</title>
<style>body{{font-family:sans-serif;padding:40px;text-align:center;background:#f5f5f5}}
.card{{background:#fff;border-radius:16px;padding:32px;max-width:420px;margin:0 auto;box-shadow:0 2px 12px rgba(0,0,0,.08)}}
.price{{font-size:48px;font-weight:700;color:#333;margin:16px 0}}
.btn{{padding:14px 40px;background:#ffc439;color:#333;border:none;border-radius:8px;font-size:16px;cursor:pointer;display:inline-block;text-decoration:none;margin:12px 0;width:100%}}
</style></head><body>
<div class="card">
<h2>🌍 国际信用卡支付</h2>
<div class="price">¥{order["amount_yuan"]:.0f} ≈ ${order["amount_yuan"]/7.2:.2f} USD</div>
<p style="color:#999;margin:20px 0">我们的国际支付由 PayPal 处理<br>支持 Visa · Mastercard · Amex · Discover</p>
<a class="btn" href="/pay/paypal/{order["order_no"]}">🅿️ 前往 PayPal 支付</a>
<p style="color:#999;font-size:12px;margin-top:20px">PayPal 是对中国大陆个人开发者开放的<br>最可靠的国际收款方案</p>
</div></body></html>""")
    
    def _render_paypal_page(self, order, ch):
        self._send_html(f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{SITE_NAME} - PayPal</title>
<style>body{{font-family:sans-serif;padding:40px;text-align:center;background:#f5f5f5}}
.card{{background:#fff;border-radius:16px;padding:32px;max-width:420px;margin:0 auto;box-shadow:0 2px 12px rgba(0,0,0,.08)}}
.price{{font-size:48px;font-weight:700;color:#333;margin:16px 0}}
.btn{{padding:14px 40px;background:#ffc439;color:#333;border:none;border-radius:8px;font-size:16px;cursor:pointer;display:inline-block;text-decoration:none;margin:12px 0;width:100%}}
</style></head><body>
<div class="card">
<h2>🅿️ PayPal 支付</h2>
<div class="price">¥{order["amount_yuan"]:.0f} ≈ ${order["amount_yuan"]/7.2:.2f} USD</div>
<p style="color:#999;margin:20px 0">此支付方式待配置 PayPal API 密钥</p>
<p style="font-size:14px;color:#333">你需要先到 <a href="https://developer.paypal.com" target="_blank">PayPal Developer</a> 注册，<br>拿到 Client ID 和 Secret 后告诉我</p>
</div></body></html>""")
    
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        
        # 支付页面
        if path.startswith("/pay/") and path.count("/") >= 3:
            parts = path.split("/")
            channel = parts[2]
            order_no = "/".join(parts[3:])
            order = get_order(order_no)
            if not order:
                return self._send_html("<h1>订单不存在</h1>", 404)
            if order["status"] != "pending":
                return self._send_html("<h1>订单已支付</h1>")
            return self._render_channel_page(order)
        
        # 选择支付方式（新订单入口）
        elif path == "/pay/new":
            user_id = qs.get("user_id", ["0"])[0]
            amount = qs.get("amount", ["10"])[0]
            order = create_order(int(user_id), float(amount), "alipay")
            if order["success"]:
                self._redirect(f"/pay/select/{order['order_no']}")
            else:
                self._send_json({"error": order["error"]}, 400)
        
        # 支付方式选择页面
        elif path.startswith("/pay/select/"):
            order_no = path.split("/pay/select/")[-1]
            order = get_order(order_no)
            if not order:
                return self._send_html("<h1>订单不存在</h1>", 404)
            
            enabled = {k: v for k, v in CHANNELS.items() if v["enabled"]}
            cards_html = ""
            for cid, ch in enabled.items():
                qr_ok = os.path.exists(ch["qr"]) if ch.get("manual") else True
                status_icon = "✅" if qr_ok else "⏳"
                cards_html += f"""<a href="/pay/{cid}/{order_no}" class="ch-card">
<div class="ch-icon">{ch["icon"]}</div>
<div class="ch-info"><div class="ch-name">{ch["name"]}</div>
<div class="ch-desc">{'扫码支付·手动确认' if ch.get('manual') else '在线支付·自动到账'} {status_icon}</div></div>
</a>"""
            
            self._send_html(f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{SITE_NAME} - 选择支付方式</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,sans-serif;background:#f5f5f5;display:flex;justify-content:center;padding:40px 16px}}
.card{{background:#fff;border-radius:16px;padding:32px;max-width:420px;width:100%;box-shadow:0 2px 12px rgba(0,0,0,.08)}}
.price{{font-size:36px;font-weight:700;color:#333;margin:8px 0 16px}}
.token{{color:#1677ff;font-size:14px;margin-bottom:20px}}
.ch-card{{display:flex;align-items:center;padding:16px;margin:8px 0;border:2px solid #eee;border-radius:12px;text-decoration:none;color:#333;transition:.2s}}
.ch-card:hover{{border-color:#1677ff;background:#f0f5ff}}
.ch-icon{{font-size:32px;margin-right:16px}}
.ch-name{{font-size:16px;font-weight:600}}
.ch-desc{{font-size:12px;color:#999;margin-top:4px}}
.order-no{{color:#999;font-size:11px;margin-top:20px;text-align:center}}
</style></head><body>
<div class="card">
<h2 style="text-align:center;margin-bottom:16px">💰 选择支付方式</h2>
<div style="text-align:center">
<div class="price">¥{order["amount_yuan"]:.0f}</div>
<div class="token">= {order["tokens"]:,} tokens</div>
</div>
{cards_html}
<div class="order-no">订单: {order_no}</div>
</div></body></html>""")
        
        # 收款码图片
        elif path.startswith("/pay/qrcode/"):
            channel = path.split("/pay/qrcode/")[-1]
            ch = CHANNELS.get(channel)
            if ch and os.path.exists(ch["qr"]):
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(os.path.getsize(ch["qr"])))
                self.end_headers()
                with open(ch["qr"], "rb") as f:
                    self.wfile.write(f.read())
            else:
                self._send_html("No QR", 404)
        
        # 管理后台
        elif path == "/pay/admin":
            if not self._check_auth():
                return self._send_html("<!DOCTYPE html><html><head><meta charset='utf-8'><title>登录</title><style>body{font-family:sans-serif;padding:40px;display:flex;justify-content:center}form{max-width:400px;width:100%}input{padding:12px;border:1px solid #ddd;border-radius:8px;width:100%;font-size:16px}button{padding:12px 32px;background:#1677ff;color:#fff;border:none;border-radius:8px;font-size:16px;cursor:pointer;margin-top:12px}</style></head><body><form method='GET'><h2>管理登录</h2><p style='color:#999;margin:12px 0'>请通过 Authorization Header 认证</p></form></body></html>", 401)
            
            stats = get_stats()
            orders = list_orders(status="pending")
            
            stats_html = ""
            for ch, s in stats.items():
                name = CHANNELS.get(ch, {}).get("name", ch)
                stats_html += f'<div class="stat"><div>{name}</div><div style="font-size:24px;font-weight:700;color:#333">{s["pending"]}</div><div style="font-size:12px;color:#999">待确认</div></div>'
            
            orders_rows = ""
            for o in orders:
                ch_name = CHANNELS.get(o["channel"], {}).get("icon", "💰")
                ts = datetime.fromtimestamp(o["created_at"]).strftime("%H:%M")
                orders_rows += f"""<tr>
<td style="font-size:12px">{o["order_no"][:16]}...</td>
<td>{o["user_id"]}</td>
<td>{ch_name}</td>
<td>¥{o["amount_yuan"]:.0f}</td>
<td>{o["tokens"]:,}</td>
<td>{ts}</td>
<td><button class="btn btn-confirm" onclick="confirmOrder('{o["order_no"]}')">确认</button></td>
</tr>"""
            
            self._send_html(f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{SITE_NAME} 管理</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,sans-serif;padding:20px;background:#f5f5f5;max-width:1200px;margin:0 auto}}
h1{{margin-bottom:20px;font-size:24px;display:flex;align-items:center;gap:12px}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:24px}}
.stat{{background:#fff;border-radius:12px;padding:20px;text-align:center}}
.tabs{{margin-bottom:16px;display:flex;gap:8px}}
.tab{{padding:8px 20px;border-radius:8px;text-decoration:none;font-size:14px;color:#666;background:#fff}}
.tab.active{{background:#1677ff;color:#fff}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden}}
th,td{{padding:10px 12px;text-align:left;border-bottom:1px solid #eee;font-size:13px}}
th{{background:#fafafa;font-weight:600;color:#333}}
.btn{{padding:6px 16px;border:none;border-radius:4px;cursor:pointer;font-size:12px}}
.btn-confirm{{background:#1677ff;color:#fff}}
.btn-confirm:hover{{background:#4096ff}}
.config{{background:#fff;border-radius:12px;padding:20px;margin-top:24px;font-size:13px;color:#666}}
.config code{{background:#f0f5ff;padding:2px 6px;border-radius:4px}}
</style></head><body>
<h1>🏦 {SITE_NAME} 管理</h1>
<div class="stats">{stats_html}</div>
<div class="tabs">
<a class="tab active" href="/pay/admin">待确认</a>
<a class="tab" href="/pay/admin/orders">全部订单</a>
</div>
<table><thead><tr><th>订单</th><th>用户</th><th>渠道</th><th>金额</th><th>Token</th><th>时间</th><th>操作</th></tr></thead>
<tbody>{orders_rows if orders_rows else '<tr><td colspan="7" style="text-align:center;padding:40px;color:#999">暂无待确认订单</td></tr>'}</tbody></table>
<div class="config">
<strong>⚙️ 渠道状态</strong><br>
{''.join([f'{ch["icon"]} {ch["name"]}: {"✅ 已启用" if ch["enabled"] else "❌ 未启用"}<br>' for ch in CHANNELS.values()])}
<br>管理密码: <code>{ADMIN_PASSWORD}</code>
</div>
<script>
function confirmOrder(no){{if(confirm('确认到账？'))fetch('/pay/admin/confirm/'+no,{{method:'POST'}}).then(r=>r.json()).then(d=>{{if(d.success)location.reload()}})}}
</script>
</body></html>""")
        
        elif path == "/pay/admin/orders":
            if not self._check_auth(): return self._send_json({"error":"Unauthorized"},401)
            channel = qs.get("channel", [None])[0]
            status = qs.get("status", [None])[0]
            orders = list_orders(channel=channel, status=status, limit=100)
            self._send_json({"orders": orders, "total": len(orders)})
        
        # 健康检查
        elif path == "/pay/health":
            pending = len(list_orders(status="pending"))
            stats = get_stats()
            self._send_json({"status": "ok", "pending_orders": pending, "channels": list(CHANNELS.keys()), "stats": stats})
        
        else:
            self._send_json({"error": "not found"}, 404)
    
    def _redirect(self, url):
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()
    
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length)) if content_length else {}
        
        # 创建订单（供API Gateway调用）
        if path == "/pay/order/create":
            user_id = body.get("user_id", 0)
            amount_yuan = float(body.get("amount_yuan", 0))
            channel = body.get("channel", "alipay")
            callback_url = body.get("callback_url", WEBHOOK_URL)
            
            if amount_yuan < 1:
                return self._send_json({"error": "金额至少1元"}, 400)
            
            result = create_order(user_id, amount_yuan, channel, callback_url)
            self._send_json(result)
        
        # 用户确认付款（手动模式）
        elif path.startswith("/pay/confirm/"):
            order_no = path.split("/pay/confirm/")[-1]
            order, error = confirm_order(order_no)
            if order:
                self._send_json({"success": True, "tokens": order["tokens"]})
            else:
                self._send_json({"success": False, "error": error}, 400)
        
        # 管理员确认到账
        elif path.startswith("/pay/admin/confirm/"):
            if not self._check_auth():
                return self._send_json({"error": "Unauthorized"}, 401)
            order_no = path.split("/pay/admin/confirm/")[-1]
            order, error = confirm_order(order_no)
            if order:
                self._send_json({"success": True, "tokens": order["tokens"]})
            else:
                self._send_json({"success": False, "error": error}, 400)
        
        # Stripe/PayPal Webhook（自动回调模式）
        elif path == "/pay/webhook/stripe":
            # 验证 Stripe Webhook 签名
            payload = self.rfile.read(content_length) if content_length else b"{}"
            sig = self.headers.get("Stripe-Signature", "")
            # TODO: 验证成功后回调 confirm_order
            self._send_json({"received": True})
        
        elif path == "/pay/webhook/paypal":
            self._send_json({"received": True})
        
        else:
            self._send_json({"error": "not found"}, 404)
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()


class ThreadedPayServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(description="Hermes Pay Unified")
    parser.add_argument("--port", type=int, default=9188)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    
    init_db()
    
    print(f"""
🏦 {SITE_NAME} — 统一国际支付网关 v2.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ✅ 服务已启动
  ✅ 管理密码: {ADMIN_PASSWORD}
  
  📱 支付入口:  {SITE_URL}/pay/new?amount=10
  📊 管理后台:  {SITE_URL}/pay/admin
  🔗 创建订单:  POST /pay/order/create
  
  💳 已启用渠道:
""")
    for cid, ch in CHANNELS.items():
        status = "✅" if ch["enabled"] else "⏳"
        mode = "手动确认" if ch.get("manual") else "自动回调"
        print(f"    {status} {ch['icon']} {ch['name']} ({mode})")
    
    print(f"""
  ⚠️ 待配置:
     - 上传支付宝收款码到 {CHANNELS['alipay']['qr']}
     - 上传微信收款码到 {CHANNELS['wechat']['qr']}
     - 配置 USDT 地址（当前为测试地址）
     - 配置 Stripe/PayPal 密钥（如需启用）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")
    
    server = ThreadedPayServer((args.host, args.port), PayHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
