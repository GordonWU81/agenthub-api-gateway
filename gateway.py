#!/usr/bin/env python3
"""
Hermes API Gateway v1.0 — Model Router 计费网关
在 Model Router (port 9099) 前面加一层：API Key 认证 + 用量计费 + 面包多支付

架构:
  用户 → API Gateway (:9199) → Model Router (:9099) → LLM Provider

依赖: pip install requests
"""
import argparse, hashlib, hmac, json, os, sqlite3, sys, time, uuid
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import urllib.request, urllib.parse

# ─── 配置 ─────────────────────────────────────────────
GATEWAY_DIR = Path(__file__).parent
DB_PATH = GATEWAY_DIR / "gateway.db"
ROUTER_URL = "http://127.0.0.1:9099"
DEFAULT_FREE_QUOTA = 100000
ADMIN_KEY = "admin-" + hashlib.sha256(os.urandom(16)).hexdigest()[:16]
LOG_FILE = GATEWAY_DIR / "gateway.log"

# ─── 面包多支付 ────────────────────────────────────────
MBD_APP_ID = "359464223249994"
MBD_APP_KEY = "7ca63efb7fd9391f0d4f7806eb4fc1bc"
MBD_API_BASE = "https://newapi.mbd.pub"
MBD_RETURN_URL = "http://localhost:9199/v1/payment/success"
MBD_WEBHOOK_URL = "https://agenthub-wu.cn/api/payment/webhook"
TOKEN_PER_YUAN = 100000  # 1元 = 10万 tokens

# ─── 数据库 ─────────────────────────────────────────────
def get_db():
    db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    return db

def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT,
            created_at REAL NOT NULL,
            balance INTEGER DEFAULT 0,
            total_used INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS api_keys (
            key TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            name TEXT,
            created_at REAL NOT NULL,
            last_used REAL,
            quota INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            api_key TEXT NOT NULL,
            model TEXT,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            cost_tokens INTEGER DEFAULT 0,
            route_to TEXT,
            created_at REAL NOT NULL,
            status TEXT DEFAULT 'success',
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS topup_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            payment_method TEXT,
            payment_id TEXT,
            created_at REAL NOT NULL,
            note TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS pending_topups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            mbd_order_id TEXT,
            status TEXT DEFAULT 'pending',
            created_at REAL NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_log(user_id);
        CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_log(created_at);
        CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_topups(status);
    """)

    admin = db.execute("SELECT id FROM users WHERE name=?", ["admin"]).fetchone()
    if not admin:
        ts = time.time()
        db.execute("INSERT INTO users (name, created_at, balance, status) VALUES (?, ?, ?, ?)",
                   ["admin", ts, 999999999, "active"])
        admin_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        admin_key = "sk-admin-" + uuid.uuid4().hex[:16]
        db.execute("INSERT INTO api_keys (key, user_id, name, created_at) VALUES (?, ?, ?, ?)",
                   [admin_key, admin_id, "admin-key", ts])
        db.commit()
        print(f"\n🎉 Admin API Key: {admin_key}")
        print(f"   保存此密钥，用于管理后台\n")
    db.close()

# ─── 计费函数 ─────────────────────────────────────────
def get_user_balance(user_id):
    db = get_db()
    row = db.execute("SELECT balance FROM users WHERE id=?", [user_id]).fetchone()
    db.close()
    return row["balance"] if row else 0

def deduct_tokens(user_id, tokens):
    db = get_db()
    db.execute("UPDATE users SET balance = balance - ?, total_used = total_used + ? WHERE id=?",
               [tokens, tokens, user_id])
    db.commit()
    db.close()

def add_tokens(user_id, tokens, method="mbdpay", payment_id=""):
    db = get_db()
    db.execute("UPDATE users SET balance = balance + ? WHERE id=?", [tokens, user_id])
    db.execute("INSERT INTO topup_log (user_id, amount, payment_method, payment_id, created_at, note) VALUES (?, ?, ?, ?, ?, ?)",
               [user_id, tokens, method, payment_id, time.time(), f"充值{tokens} tokens"])
    db.commit()
    db.close()

def log_usage(user_id, api_key, model, prompt_tokens, completion_tokens, route_to, status="success"):
    total = prompt_tokens + completion_tokens
    db = get_db()
    db.execute("""INSERT INTO usage_log 
        (user_id, api_key, model, prompt_tokens, completion_tokens, total_tokens, cost_tokens, route_to, created_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [user_id, api_key, model, prompt_tokens, completion_tokens, total, total, route_to, time.time(), status])
    db.commit()
    db.close()

# ─── API Key 验证 ─────────────────────────────────────
def validate_api_key(auth_header):
    if not auth_header:
        return None
    key = auth_header.replace("Bearer ", "").strip()
    db = get_db()
    row = db.execute("""
        SELECT k.key, k.user_id, u.status as user_status, u.balance
        FROM api_keys k JOIN users u ON k.user_id = u.id
        WHERE k.key=?
    """, [key]).fetchone()
    db.close()
    if not row or row["user_status"] != "active":
        return None
    db = get_db()
    db.execute("UPDATE api_keys SET last_used=? WHERE key=?", [time.time(), key])
    db.commit()
    db.close()
    return {"user_id": row["user_id"], "api_key": key, "balance": row["balance"]}

# ─── 面包多支付 ──────────────────────────────────────
def mbd_sign(params):
    """签名: 参数key排序 + app_key拼接 → MD5小写"""
    keys = sorted(params.keys())
    raw = "&".join([f"{k}={params[k]}" for k in keys])
    raw += "&key=" + MBD_APP_KEY
    return hashlib.md5(raw.encode("utf-8")).hexdigest()  # 小写!

def create_mbd_order(user_id, amount_yuan):
    """创建面包多支付宝支付订单"""
    tokens = int(amount_yuan * TOKEN_PER_YUAN)
    amount_fen = int(amount_yuan * 100)
    out_trade_no = f"TOP{int(time.time())}{user_id}"
    
    params = {
        "app_id": MBD_APP_ID,
        "out_trade_no": out_trade_no,
        "description": f"API Token充值{amount_yuan}元",
        "amount_total": amount_fen,
        "url": MBD_RETURN_URL,
        "callback_url": MBD_WEBHOOK_URL,
    }
    params["sign"] = mbd_sign(params)
    
    try:
        req = urllib.request.Request(
            f"{MBD_API_BASE}/release/alipay/pay",
            data=json.dumps(params).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=15)
        result_text = resp.read().decode("utf-8")
        
        # 面包多返回的是支付宝支付表单HTML
        # 提取表单中的action URL
        import re
        action_match = re.search(r'action=\'([^\']+)\'', result_text)
        if action_match:
            pay_url = action_match.group(1)
            # 把所有input参数加到URL上
            inputs = re.findall(r"name='([^']+)'\s*value='([^']*)'", result_text)
            params_list = [f"{urllib.parse.quote(n)}={urllib.parse.quote(v)}" for n, v in inputs]
            pay_url += "&" + "&".join(params_list)
            
            # 保存待处理订单
            db = get_db()
            db.execute("INSERT INTO pending_topups (user_id, amount, mbd_order_id, status, created_at) VALUES (?, ?, ?, ?, ?)",
                       [user_id, tokens, out_trade_no, "pending", time.time()])
            db.commit()
            db.close()
            return {"success": True, "pay_url": pay_url, "out_trade_no": out_trade_no, "tokens": tokens}
        else:
            return {"success": False, "error": "无法解析支付地址", "raw": result_text[:200]}
    except Exception as e:
        return {"success": False, "error": str(e)}

def verify_mbd_webhook(data):
    """验证面包多Webhook回调"""
    sign = data.pop("sign", "")
    keys = sorted(data.keys())
    raw = "&".join([f"{k}={data[k]}" for k in keys])
    raw += "&key=" + MBD_APP_KEY
    expected = hashlib.md5(raw.encode("utf-8")).hexdigest()  # 小写!
    return sign == expected

# ─── HTTP Handler ────────────────────────────────────
class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    
    def log_message(self, fmt, *args):
        msg = f"[{datetime.now().isoformat()}] {self.client_address[0]} - {fmt % args}"
        with open(LOG_FILE, "a") as f:
            f.write(msg + "\n")
    
    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
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
    
    def _forward_to_router(self, path, body, headers):
        url = f"{ROUTER_URL}{path}"
        req = urllib.request.Request(url, data=body if isinstance(body, bytes) else body.encode(),
            headers={"Content-Type": "application/json", "User-Agent": "Hermes-Gateway/1.0"},
            method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=60)
            return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers
        except Exception as e:
            return 502, json.dumps({"error": str(e)}).encode(), {"Content-Type": "application/json"}
    
    def _count_tokens(self, messages):
        text = json.dumps(messages)
        return len(text) // 4 + len(text) // 2

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        
        if path == "/v1/health":
            self._send_json({"status": "ok", "gateway": "hermes-api-gateway", "router": ROUTER_URL, "mbd_pay": MBD_APP_ID})
        
        elif path == "/v1/models":
            try:
                req = urllib.request.Request(f"{ROUTER_URL}/v1/models")
                resp = urllib.request.urlopen(req, timeout=10)
                self._send_json(json.loads(resp.read()))
            except Exception as e:
                self._send_json({"error": str(e)}, 502)
        
        elif path == "/v1/payment/page":
            # 自助充值页面
            auth = validate_api_key(self.headers.get("Authorization", ""))
            if not auth:
                self._send_json({"error": "请先登录（提供API Key）"}, 401)
                return
            self._send_html(f"""<!DOCTYPE html>
<html><meta charset="utf-8"><title>API Token 充值</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:sans-serif;max-width:600px;margin:50px auto;padding:20px;text-align:center}}
.card{{background:#f5f5f5;border-radius:12px;padding:30px;margin:20px 0}}
.price{{font-size:48px;color:#333;margin:10px 0}}
.balance{{color:#666;margin:20px 0}}
.btn{{display:inline-block;padding:15px 40px;background:#1677ff;color:#fff;border:none;
border-radius:8px;font-size:18px;cursor:pointer;text-decoration:none;margin:10px}}
.btn:hover{{background:#4096ff}}
.amount-btn{{padding:10px 20px;margin:5px;border:2px solid #ddd;border-radius:8px;
background:#fff;cursor:pointer;font-size:16px;min-width:100px}}
.amount-btn.active{{border-color:#1677ff;background:#e6f4ff}}
input[type=hidden]{{display:none}}
</style></head><body>
<h2>💎 API Token 充值</h2>
<div class="card">
<div class="balance">💰 当前余额: <strong id="balance">{auth["balance"]:,}</strong> tokens</div>
<div style="margin:20px 0"><p style="color:#999">选择充值金额</p>
<button class="amount-btn" data-yuan="10" data-tokens="1,000,000">¥10<br><small>100万 tokens</small></button>
<button class="amount-btn active" data-yuan="30" data-tokens="3,000,000">¥30<br><small>300万 tokens</small></button>
<button class="amount-btn" data-yuan="50" data-tokens="5,000,000">¥50<br><small>500万 tokens</small></button>
<button class="amount-btn" data-yuan="100" data-tokens="10,000,000">¥100<br><small>1000万 tokens</small></button>
</div>
<input type="hidden" id="amount" value="30">
<div class="price" id="amount-display">30 元</div>
<button class="btn" onclick="pay()">🚀 支付宝支付</button>
<script>
document.querySelectorAll('.amount-btn').forEach(b=>b.onclick=()=>{{
document.querySelectorAll('.amount-btn').forEach(x=>x.classList.remove('active'))
b.classList.add('active')
let y=b.dataset.yuan
document.getElementById('amount').value=y
document.getElementById('amount-display').textContent=y+' 元'
}})
function pay(){{
let yuan=document.getElementById('amount').value
let key=document.getElementById('api-key').value||''
fetch('/v1/payment/create',{{
method:'POST',headers:{{'Content-Type':'application/json',
'Authorization':'Bearer '+key}},
body:JSON.stringify({{amount_yuan:parseInt(yuan)}})
}}).then(r=>r.json()).then(d=>{{
if(d.pay_url)window.location.href=d.pay_url
else alert('支付创建失败: '+JSON.stringify(d))
}})
}}
</script></div>
<input type="hidden" id="api-key" value="{auth["api_key"]}">
<p style="color:#999;margin-top:30px;font-size:14px">支付成功后，tokens 自动到账</p>
<p style="color:#999;font-size:12px">由 面包多Pay 提供支付能力 · 个人开发者可用</p>
</body></html>""")
        
        elif path == "/v1/payment/success":
            # 支付成功跳转页
            self._send_html("""<!DOCTYPE html>
<html><meta charset="utf-8"><title>支付成功</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font-family:sans-serif;text-align:center;padding:80px 20px}
h1{color:#52c41a} .btn{padding:15px 40px;background:#1677ff;color:#fff;border-radius:8px;
text-decoration:none;display:inline-block;margin-top:30px}</style>
<body><h1>✅ 支付成功！</h1>
<p>Token 已自动充值到您的账户</p>
<p>返回 <a href="/v1/payment/page">充值页面</a> 查看余额</p></body></html>""")
        
        elif path.startswith("/v1/admin/"):
            auth = validate_api_key(self.headers.get("Authorization", ""))
            if not auth:
                self._send_json({"error": "Unauthorized"}, 401)
                return
            # GET admin endpoints
            if path == "/v1/admin/users":
                db = get_db()
                rows = db.execute("SELECT id,name,email,balance,total_used,status,created_at FROM users").fetchall()
                db.close()
                self._send_json({"users": [dict(r) for r in rows]})
            elif path == "/v1/admin/api_keys":
                user_id = qs.get("user_id", [None])[0]
                db = get_db()
                if user_id:
                    rows = db.execute("SELECT * FROM api_keys WHERE user_id=?", [user_id]).fetchall()
                else:
                    rows = db.execute("SELECT * FROM api_keys").fetchall()
                db.close()
                self._send_json({"api_keys": [dict(r) for r in rows]})
            else:
                self._send_json({"error": "Not found"}, 404)
        
        else:
            self._send_json({"error": "Not found"}, 404)
    
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        content_length = int(self.headers.get("Content-Length", 0))
        body_raw = self.rfile.read(content_length) if content_length else b"{}"
        body = json.loads(body_raw) if body_raw else {}
        
        # ═══ Chat Completion ═══
        if path == "/v1/chat/completions":
            auth = validate_api_key(self.headers.get("Authorization", ""))
            if not auth:
                self._send_json({"error": "Unauthorized", "message": "请提供有效的API Key"}, 401)
                return
            data = body
            messages = data.get("messages", [])
            estimated_cost = self._count_tokens(messages) * 2
            balance = auth["balance"]
            if balance < estimated_cost:
                self._send_json({
                    "error": "Insufficient balance",
                    "message": f"余额不足: 需要约{estimated_cost} tokens，当前{balance} tokens",
                    "balance": balance, "estimated_cost": estimated_cost,
                }, 402)
                return
            status, resp_body, resp_headers = self._forward_to_router("/v1/chat/completions", body_raw, self.headers)
            prompt_tokens = completion_tokens = 0
            route_to = data.get("model", "unknown")
            if status == 200:
                try:
                    rd = json.loads(resp_body)
                    usage = rd.get("usage", {})
                    prompt_tokens = usage.get("prompt_tokens", estimated_cost // 2)
                    completion_tokens = usage.get("completion_tokens", estimated_cost // 2)
                    route_to = resp_headers.get("X-Routed-Model", route_to)
                except:
                    prompt_tokens = estimated_cost // 2
                    completion_tokens = estimated_cost // 2
            total_tokens = prompt_tokens + completion_tokens
            deduct_tokens(auth["user_id"], total_tokens)
            log_usage(auth["user_id"], auth["api_key"], route_to,
                      prompt_tokens, completion_tokens, route_to,
                      "success" if status == 200 else "failed")
            self.send_response(status)
            for k, v in resp_headers.items():
                if k.lower() in ("content-type", "content-length", "x-routed-model", "x-routed-provider"):
                    self.send_header(k, v)
            self.send_header("X-Gateway-Balance-Deduction", str(total_tokens))
            self.send_header("X-Gateway-Balance-Remaining", str(get_user_balance(auth["user_id"])))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(resp_body if isinstance(resp_body, bytes) else resp_body.encode())
        
        # ═══ 充值接口 ═══
        elif path == "/v1/payment/create":
            auth = validate_api_key(self.headers.get("Authorization", ""))
            if not auth:
                self._send_json({"error": "Unauthorized"}, 401)
                return
            amount_yuan = int(body.get("amount_yuan", 0))
            if amount_yuan < 1:
                self._send_json({"error": "金额至少1元"}, 400)
                return
            result = create_mbd_order(auth["user_id"], amount_yuan)
            if result["success"]:
                self._send_json({"pay_url": result["pay_url"], "out_trade_no": result["out_trade_no"], "tokens": result["tokens"]})
            else:
                self._send_json({"error": result["error"]}, 500)
        
        # ═══ 面包多Webhook ═══
        elif path == "/api/payment/webhook":
            if verify_mbd_webhook(dict(body)):
                out_trade_no = body.get("out_trade_no", "")
                trade_status = body.get("trade_status", "")
                if trade_status in ("TRADE_SUCCESS", "TRADE_FINISHED"):
                    # 查找待处理订单
                    db = get_db()
                    pending = db.execute("SELECT id, user_id, amount FROM pending_topups WHERE mbd_order_id=? AND status='pending'",
                                         [out_trade_no]).fetchone()
                    if pending:
                        # 充值
                        add_tokens(pending["user_id"], pending["amount"], "mbdpay", out_trade_no)
                        db.execute("UPDATE pending_topups SET status='paid' WHERE id=?", [pending["id"]])
                        db.commit()
                        print(f"✅ 支付成功: {out_trade_no}, 用户{pending['user_id']}, +{pending['amount']} tokens")
                    db.close()
                self._send_json({"code": 0, "message": "success"})
            else:
                self._send_json({"code": -1, "message": "sign invalid"}, 403)
        
        # ═══ Admin 充值 ═══
        elif path == "/v1/admin/topup":
            auth = validate_api_key(self.headers.get("Authorization", ""))
            if not auth:
                self._send_json({"error": "Unauthorized"}, 401)
                return
            user_id = body.get("user_id", auth["user_id"])
            amount = int(body.get("amount", 0))
            if amount <= 0:
                self._send_json({"error": "Invalid amount"}, 400)
                return
            add_tokens(user_id, amount, "admin", "")
            self._send_json({"success": True, "balance": get_user_balance(user_id), "amount": amount})
        
        # ═══ Admin 创建用户 ═══
        elif path == "/v1/admin/create_user":
            auth = validate_api_key(self.headers.get("Authorization", ""))
            if not auth:
                self._send_json({"error": "Unauthorized"}, 401)
                return
            name = body.get("name", f"user_{int(time.time())}")
            email = body.get("email", "")
            quota = int(body.get("quota", DEFAULT_FREE_QUOTA))
            db = get_db()
            ts = time.time()
            db.execute("INSERT INTO users (name, email, created_at, balance, status) VALUES (?, ?, ?, ?, ?)",
                       [name, email, ts, quota, "active"])
            uid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            api_key = "sk-" + uuid.uuid4().hex[:24]
            db.execute("INSERT INTO api_keys (key, user_id, name, created_at, quota) VALUES (?, ?, ?, ?, ?)",
                       [api_key, uid, "default", ts, 0])
            db.commit()
            db.close()
            self._send_json({"user_id": uid, "name": name, "api_key": api_key, "balance": quota})
        
        else:
            self._send_json({"error": "Not found"}, 404)
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()


class ThreadedGatewayServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    global ROUTER_URL
    parser = argparse.ArgumentParser(description="Hermes API Gateway")
    parser.add_argument("--port", type=int, default=9199, help="监听端口")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--router", default=ROUTER_URL, help="Model Router地址")
    args = parser.parse_args()
    ROUTER_URL = args.router
    init_db()
    server = ThreadedGatewayServer((args.host, args.port), GatewayHandler)
    print(f"🚀 Hermes API Gateway v1.0")
    print(f"   Listen: http://{args.host}:{args.port}")
    print(f"   Router: {ROUTER_URL}")
    print(f"   充值页: http://{args.host}:{args.port}/v1/payment/page")
    print(f"   Webhook: {MBD_WEBHOOK_URL}")
    print(f"   DB: {DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
