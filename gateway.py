#!/usr/bin/env python3
"""
AgentHub API Gateway v1.0 — Model Router 计费网关
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


# ─── 邮件通知 ─────────────────────────────────────────
import smtplib
from email.mime.text import MIMEText

SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465
FROM_EMAIL = "1278373034@qq.com"
AUTH_CODE = "mfziljrapgmeffjd"

def send_email(to, subject, body):
    try:
        msg = MIMEText(body, "html", "utf-8")
        msg["From"] = FROM_EMAIL
        msg["To"] = to
        msg["Subject"] = subject
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(FROM_EMAIL, AUTH_CODE)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"Email fail: {e}")
        return False

REG_WELCOME = '''<div style="max-width:600px;margin:0 auto;font-family:sans-serif;padding:20px">
<div style="background:linear-gradient(135deg,#667eea,#764ba2);border-radius:12px 12px 0 0;padding:30px;text-align:center;color:#fff">
<h1 style="margin:0">Welcome to AgentHub API!</h1>
<p style="opacity:.9;margin:8px 0 0">72+ AI Models | One API Key</p></div>
<div style="padding:24px;border:1px solid #eee;border-top:0;border-radius:0 0 12px 12px">
<p style="color:#333">Hi <strong>{name}</strong>,</p>
<p style="color:#666">Your account has been created with <strong style="color:#667eea">{bal:,} free tokens</strong>.</p>
<div style="background:#f8f7ff;border:1px solid #e0dcff;border-radius:8px;padding:16px;margin:16px 0">
<p style="color:#667eea;font-size:12px;margin:0 0 4px">YOUR API KEY</p>
<p style="font-family:monospace;font-size:14px;color:#333;word-break:break-all;user-select:all">{key}</p></div>
<h3 style="color:#333;margin:20px 0 8px">Quick Start</h3>
<div style="background:#f5f5f5;border-radius:8px;padding:12px;font-family:monospace;font-size:13px;line-height:1.6">
curl https://api.agenthub-wu.cn/v1/chat/completions \\<br>
&nbsp;&nbsp;-H "Authorization: Bearer {key}" \\<br>
&nbsp;&nbsp;-d '{{"model":"deepseek-chat","messages":[{{"role":"user","content":"Hello!"}}]}}'
</div>
<p style="color:#999;font-size:12px;margin-top:20px">
<a href="https://api.agenthub-wu.cn/v1/payment/page" style="color:#667eea">Top up →</a></p></div></div>'''

PAY_CONFIRM = '''<div style="max-width:600px;margin:0 auto;font-family:sans-serif;padding:20px">
<div style="background:linear-gradient(135deg,#52c41a,#389e0d);border-radius:12px 12px 0 0;padding:30px;text-align:center;color:#fff">
<h1 style="margin:0">Payment Confirmed!</h1></div>
<div style="padding:24px;border:1px solid #eee;border-top:0;border-radius:0 0 12px 12px">
<p style="color:#333">Hi <strong>{name}</strong>,</p>
<p style="color:#666">Your payment has been confirmed.</p>
<div style="background:#f6ffed;border:1px solid #b7eb8f;border-radius:8px;padding:16px;margin:16px 0;text-align:center">
<p style="font-size:28px;font-weight:700;color:#333">+{tokens:,} tokens</p>
<p style="color:#389e0d;font-size:14px">¥{amount}</p></div>
<p style="color:#999;font-size:12px;text-align:center">
<a href="https://api.agenthub-wu.cn/v1/payment/page" style="color:#667eea">Check balance →</a></p></div></div>'''


GATEWAY_DIR = Path(__file__).parent
DB_PATH = GATEWAY_DIR / "gateway.db"
ROUTER_URL = "http://127.0.0.1:9099"
DEFAULT_FREE_QUOTA = 1000000
ADMIN_KEY = "admin-" + hashlib.sha256(os.urandom(16)).hexdigest()[:16]
LOG_FILE = GATEWAY_DIR / "gateway.log"

# ─── 面包多支付 ────────────────────────────────────────
PAYMENT_API = "http://127.0.0.1:9188/pay/order/create"
PAYMENT_WEBHOOK = "http://127.0.0.1:9199/api/payment/webhook"
MBD_API_BASE = "https://newapi.mbd.pub"
MBD_RETURN_URL = "http://localhost:9199/v1/payment/success"
MBD_WEBHOOK_URL = "https://agenthub-wu.cn/api/payment/webhook"
TOKEN_PER_YUAN = 1000000  # 1元 = 10万 tokens


def hash_password(password):
    return hashlib.sha256(password.encode('utf-8')).hexdigest()

def verify_password(password, hash_val):
    return hashlib.sha256(password.encode('utf-8')).hexdigest() == hash_val
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
            password_hash TEXT DEFAULT '',
            created_at REAL NOT NULL,
            balance INTEGER DEFAULT 0,
            total_used INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            plan TEXT DEFAULT 'free',
            plan_expires REAL DEFAULT 0
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

def deduct_tokens(user_id, tokens, plan_type="free"):
    """扣费: 先用plan_balance(包月), 再用topup_balance(加购)"""
    db = get_db()
    user = db.execute("SELECT plan_balance, topup_balance, plan_type FROM users WHERE id=?", [user_id]).fetchone()
    if not user:
        db.close()
        return
    
    remaining = tokens
    plan_used = 0
    topup_used = 0
    
    # 先用plan_balance
    if user["plan_balance"] > 0:
        plan_used = min(remaining, user["plan_balance"])
        remaining -= plan_used
    
    # 再用topup_balance
    if remaining > 0:
        topup_used = min(remaining, user["topup_balance"])
        remaining -= topup_used
    
    db.execute("""UPDATE users SET 
        plan_balance = plan_balance - ?,
        topup_balance = topup_balance - ?,
        total_used = total_used + ?
        WHERE id=?""", [plan_used, topup_used, tokens, user_id])
    db.commit()
    db.close()

def add_tokens(user_id, tokens, method="mbdpay", payment_id=""):
    db = get_db()
    db.execute("UPDATE users SET balance = balance + ? WHERE id=?", [tokens, user_id])
    db.execute("INSERT INTO topup_log (user_id, amount, payment_method, payment_id, created_at, note) VALUES (?, ?, ?, ?, ?, ?)",
               [user_id, tokens, method, payment_id, time.time(), f"充值{tokens} tokens"])
    db.commit()
    db.close()

# Override get_user_balance to include plan+topup
# Update balance display in headers to return total


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
def create_payment_order(user_id, amount_yuan, channel="alipay"):
    """通过自建支付平台创建订单"""
    tokens = int(amount_yuan * TOKEN_PER_YUAN)
    
    try:
        data = json.dumps({
            "user_id": user_id,
            "amount_yuan": amount_yuan,
            "channel": channel,
            "callback_url": "http://127.0.0.1:9199/api/payment/webhook",
        }).encode("utf-8")
        req = urllib.request.Request(
            PAYMENT_API, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read())
        
        if result.get("success"):
            # 保存待处理订单
            db = get_db()
            db.execute("INSERT INTO pending_topups (user_id, amount, mbd_order_id, status, created_at) VALUES (?, ?, ?, ?, ?)",
                       [user_id, tokens, result["order_no"], "pending", time.time()])
            db.commit()
            db.close()
            return {"success": True, "pay_url": result["pay_url"], "order_no": result["order_no"], "tokens": tokens}
        else:
            return {"success": False, "error": result.get("error", "创建失败")}
    except Exception as e:
        return {"success": False, "error": str(e)}

def verify_mbd_webhook(data):
    # Deprecated: 已切换至自建支付平台
    return True

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
        

        # 自助注册（GET）
        if path == "/v1/register":
            html = open("/opt/hermes-api-stack/api-gateway/register.html").read()
            self._send_html(html)
            return
        
        # 健康检查
        if path == "/v1/health":
            self._send_json({"status": "ok", "gateway": "agenthub-api-gateway", "router": ROUTER_URL, "pay_platform": "http://127.0.0.1:9188/pay/health"})
        
        elif path == "/v1/models":
            try:
                req = urllib.request.Request(f"{ROUTER_URL}/v1/models")
                resp = urllib.request.urlopen(req, timeout=10)
                self._send_json(json.loads(resp.read()))
            except Exception as e:
                self._send_json({"error": str(e)}, 502)
        elif path == "/v1/payment/page":
            auth = validate_api_key(self.headers.get("Authorization", ""))
            html = open("/opt/hermes-api-stack/api-gateway/payment.html").read()
            if auth:
                html = html.replace("id=\"balance\">--", "id=\"balance\">" + str(get_user_balance(auth["user_id"])))
            self._send_html(html)
            return
            db.commit()
        elif path == "/v1/payment/create":
            auth = validate_api_key(self.headers.get("Authorization", ""))
            if not auth:
                self._send_json({"error": "Unauthorized"}, 401)
                return
            amount_yuan = int(body.get("amount_yuan", 0))
            if amount_yuan < 1:
                self._send_json({"error": "金额至少1元"}, 400)
                return
            result = create_payment_order(auth["user_id"], amount_yuan)
            if result["success"]:
                self._send_json({"pay_url": result["pay_url"], "out_trade_no": result.get("order_no",result.get("out_trade_no","")), "tokens": result["tokens"]})
            else:
                self._send_json({"error": result["error"]}, 500)
        
        # ═══ 面包多Webhook ═══
        elif path == "/api/payment/webhook":
            # 自建支付平台回调（本机，无需签名验证）
            out_trade_no = body.get("out_trade_no", "")
            trade_status = body.get("trade_status", "")
            user_id = body.get("user_id", 0)
            if trade_status in ("TRADE_SUCCESS", "TRADE_FINISHED"):
                db = get_db()
                pending = db.execute("SELECT id, user_id, amount FROM pending_topups WHERE mbd_order_id=? AND status='pending'",
                                     [out_trade_no]).fetchone()
                if pending:
                    add_tokens(pending["user_id"], pending["amount"], "selfpay", out_trade_no)
                    # Send payment email
                    user_info = db.execute("SELECT name, email FROM users WHERE id=?", [pending["user_id"]]).fetchone()
                    if user_info and user_info["email"]:
                        try:
                            name = user_info["name"] or "User"
                            html = PAY_CONFIRM.format(name=name, tokens=pending["amount"], amount=pending["amount"]//1000000)
                            send_email(user_info["email"], "Payment Confirmed - Tokens Added!", html)
                        except:
                            pass
                    db.execute("UPDATE pending_topups SET status='paid' WHERE id=?", [pending["id"]])
                    db.commit()
                    print(f"✅ 支付成功: {out_trade_no}, 用户{pending['user_id']}, +{pending['amount']} tokens")
                db.close()
            self._send_json({"code": 0, "message": "success"})
        
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
    

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        content_length = int(self.headers.get("Content-Length", 0))
        body_raw = self.rfile.read(content_length) if content_length else b"{}"
        body = json.loads(body_raw) if body_raw else {}
        

        # ═══ 邮箱密码登录 ═══
        if path == "/v1/auth/login":
            email = body.get("email", "").strip().lower()
            password = body.get("password", "")
            if not email or not password:
                self._send_json({"error": "请填写邮箱和密码"}, 400)
                return
            db = get_db()
            user = db.execute("SELECT id, name, password_hash, balance, plan FROM users WHERE email=?", [email]).fetchone()
            db.close()
            if not user or not verify_password(password, user["password_hash"]):
                self._send_json({"error": "邮箱或密码错误"}, 401)
                return
            # 获取API Key
            db = get_db()
            key_row = db.execute("SELECT key FROM api_keys WHERE user_id=?", [user["id"]]).fetchone()
            db.close()
            api_key = key_row["key"] if key_row else ""
            self._send_json({
                "success": True,
                "user_id": user["id"],
                "name": user["name"],
                "api_key": api_key,
                "balance": user["balance"],
                "plan": user["plan"],
            })
            return
        
        # ═══ 邮箱注册（带密码）═══
        if path == "/v1/auth/register":
            name = body.get("name", "").strip()
            email = body.get("email", "").strip().lower()
            password = body.get("password", "")
            if not name or not email or not password:
                self._send_json({"error": "请填写用户名、邮箱和密码"}, 400)
                return
            if len(password) < 6:
                self._send_json({"error": "密码至少6位"}, 400)
                return
            db = get_db()
            exist = db.execute("SELECT id FROM users WHERE email=?", [email]).fetchone()
            if exist:
                db.close()
                self._send_json({"error": "该邮箱已注册"}, 409)
                return
            ts = time.time()
            pw_hash = hash_password(password)
            db.execute("INSERT INTO users (name, email, password_hash, created_at, balance, status, plan) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       [name, email, pw_hash, ts, 1000000, "active", "free"])
            uid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            api_key = "sk-" + uuid.uuid4().hex[:24]
            db.execute("INSERT INTO api_keys (key, user_id, name, created_at) VALUES (?, ?, ?, ?)",
                       [api_key, uid, "default", ts])
            db.commit()
            db.close()
            # Welcome email
            if email:
                try:
                    html = REG_WELCOME.format(name=name, key=api_key, bal=1000000)
                    send_email(email, "Welcome to Duozhilian / 多智联!", html)
                except:
                    pass
            self._send_json({"success": True, "api_key": api_key, "user_id": uid, "balance": 1000000, "name": name})
            return
        # ═══ 自助注册API ═══
        if path == "/v1/register":
            name = body.get("name","").strip()
            email = body.get("email","").strip()
            if not name or not email:
                self._send_json({"error":"请填写昵称和邮箱"},400)
                return
            db = get_db()
            ts = time.time()
            db.execute("INSERT INTO users (name, email, created_at, balance, status) VALUES (?, ?, ?, ?, ?)",
                       [name, email, ts, 1000000, "active"])
            uid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            api_key = "sk-" + uuid.uuid4().hex[:24]
            db.execute("INSERT INTO api_keys (key, user_id, name, created_at) VALUES (?, ?, ?, ?)",
                       [api_key, uid, "default", ts])
            db.commit()
            db.close()
            # Send welcome email
            if email and "@" in email:
                try:
                    html = REG_WELCOME.format(name=name, key=api_key, bal=1000000)
                    send_email(email, "Welcome to AgentHub API!", html)
                except:
                    pass
            self._send_json({"success":True, "api_key":api_key, "user_id":uid, "balance":1000000})
            return
        
        # ═══ Chat Completion ═══

        # ═══ 重置包月额度（吸血鬼引擎内部调用）═══
        if path == "/v1/admin/reset-plan":
            auth = validate_api_key(self.headers.get("Authorization", ""))
            if not auth:
                self._send_json({"error": "Unauthorized"}, 401)
                return
            # 重置所有用户的plan_balance到当月上限
            db = get_db()
            today = time.strftime("%Y-%m-%d")
            caps = {"free": 100000, "pro": 5000000, "enterprise": 30000000,
                    "combo_pro": 6000000, "combo_enterprise": 50000000}
            users = db.execute("SELECT id, plan_type FROM users WHERE last_reset_date != ? OR last_reset_date IS NULL", [today]).fetchall()
            reset_count = 0
            for u in users:
                cap = caps.get(u["plan_type"] or "free", 100000)
                db.execute("UPDATE users SET plan_balance = ?, last_reset_date = ? WHERE id=?", [cap, today, u["id"]])
                reset_count += 1
            db.commit()
            db.close()
            self._send_json({"success": True, "reset_count": reset_count})
            return

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
                self._send_json({"error": "余额不足", "balance": balance, "estimated_cost": estimated_cost}, 402)
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
                except:
                    prompt_tokens = estimated_cost // 2
                    completion_tokens = estimated_cost // 2
            total_tokens = prompt_tokens + completion_tokens
            deduct_tokens(auth["user_id"], int(total_tokens * 1.1))
            log_usage(auth["user_id"], auth["api_key"], route_to, prompt_tokens, completion_tokens, route_to)
            self.send_response(status)
            for k, v in resp_headers.items():
                if k.lower() in ("content-type", "content-length", "x-routed-model", "x-routed-provider"):
                    self.send_header(k, v)
            self.send_header("X-Gateway-Balance-Deduction", str(total_tokens))
            self.send_header("X-Gateway-Balance-Remaining", str(get_user_balance(auth["user_id"])))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(resp_body if isinstance(resp_body, bytes) else resp_body.encode())
            return
        
        # ═══ 创建支付订单 ═══
        if path == "/v1/payment/create":
            auth = validate_api_key(self.headers.get("Authorization", ""))
            if not auth:
                self._send_json({"error": "Unauthorized"}, 401)
                return
            amount_yuan = int(body.get("amount_yuan", 0))
            channel = body.get("channel", "alipay")
            if amount_yuan < 1:
                self._send_json({"error": "金额至少1元"}, 400)
                return
            result = create_payment_order(auth["user_id"], amount_yuan, channel)
            if result["success"]:
                self._send_json({"pay_url": result["pay_url"], "order_no": result["order_no"], "tokens": result["tokens"]})
            else:
                self._send_json({"error": result["error"]}, 500)
            return
        
        # ═══ 支付回调Webhook ═══
        if path == "/api/payment/webhook":
            out_trade_no = body.get("out_trade_no", "")
            trade_status = body.get("trade_status", "")
            if trade_status in ("TRADE_SUCCESS", "TRADE_FINISHED"):
                db = get_db()
                pending = db.execute("SELECT id, user_id, amount FROM pending_topups WHERE mbd_order_id=? AND status='pending'",
                                     [out_trade_no]).fetchone()
                if pending:
                    add_tokens(pending["user_id"], pending["amount"], "selfpay", out_trade_no)
                    db.execute("UPDATE pending_topups SET status='paid' WHERE id=?", [pending["id"]])
                    # Send payment email
                    user_info = db.execute("SELECT name, email FROM users WHERE id=?", [pending["user_id"]]).fetchone()
                    if user_info and user_info["email"]:
                        try:
                            html = PAY_CONFIRM.format(name=user_info["name"] or "User", tokens=pending["amount"], amount=pending["amount"]//1000000)
                            send_email(user_info["email"], "Payment Confirmed - Tokens Added!", html)
                        except:
                            pass
                    db.commit()
                db.close()
            self._send_json({"code": 0, "message": "success"})
            return
        
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
    parser = argparse.ArgumentParser(description="AgentHub API Gateway")
    parser.add_argument("--port", type=int, default=9199, help="监听端口")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--router", default=ROUTER_URL, help="Model Router地址")
    args = parser.parse_args()
    ROUTER_URL = args.router
    init_db()
    server = ThreadedGatewayServer((args.host, args.port), GatewayHandler)
    print(f"🚀 AgentHub API Gateway v1.0")
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
