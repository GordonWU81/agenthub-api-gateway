#!/usr/bin/env python3
"""
Hermes 模型路由器 v1.1 (Model Router)
——智能体的"自主择模大脑"（多智能体并发版）

修复:
- v1.0: 假流式/单线程/空指针崩溃/不回退HTTP错误
- v1.1: 真SSE流式 + ThreadingMixIn并发 + 全场景fallback + 熔断器

用法:
  python model_router.py --port 9099
"""

import argparse
import http.server
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Optional

# ─── 配置 ─────────────────────────────────────────────

ROUTER_DIR = Path(os.environ.get("MODEL_ROUTER_DIR", Path(__file__).parent))
MODELS_FILE = ROUTER_DIR / "models.json"
ROUTER_LOG = ROUTER_DIR / "router.log"
HEALTH_CACHE_TTL = 60  # 健康检查缓存秒数

# ─── 熔断器 ───────────────────────────────────────────

_circuit_breaker = {}
_cb_lock = threading.Lock()

MAX_FAILURES = 3
CIRCUIT_TIMEOUT = 120  # 熔断后 2 分钟自动恢复


def cb_failure(provider_name: str):
    with _cb_lock:
        cb = _circuit_breaker.setdefault(provider_name, {"failures": 0, "open_until": 0})
        cb["failures"] += 1
        if cb["failures"] >= MAX_FAILURES:
            cb["open_until"] = time.time() + CIRCUIT_TIMEOUT
            log(f"🔴 CIRCUIT BREAKER OPEN: {provider_name} for {CIRCUIT_TIMEOUT}s")


def cb_success(provider_name: str):
    with _cb_lock:
        if provider_name in _circuit_breaker:
            _circuit_breaker[provider_name] = {"failures": 0, "open_until": 0}


def cb_is_open(provider_name: str) -> bool:
    with _cb_lock:
        cb = _circuit_breaker.get(provider_name, {})
        if cb.get("open_until", 0) > time.time():
            return True
        if cb.get("open_until", 0) > 0 and cb["open_until"] <= time.time():
            # 熔断过期，半开状态
            _circuit_breaker[provider_name] = {"failures": 0, "open_until": 0}
        return False


# ─── 模型注册表 ────────────────────────────────────────


class ModelRegistry:
    def __init__(self, path: Path):
        self.path = path
        self.data = {}
        self._mtime = 0
        self._reload()

    def _reload(self):
        if self.path.exists():
            mtime = self.path.stat().st_mtime
            if mtime != self._mtime:
                try:
                    with open(self.path, encoding="utf-8") as f:
                        self.data = json.load(f)
                    self._mtime = mtime
                except Exception as e:
                    log(f"Failed to load models.json: {e}")

    @property
    def providers(self):
        self._reload()
        return self.data.get("providers", {})

    @property
    def models(self):
        self._reload()
        return self.data.get("models", {})

    @property
    def routing_rules(self):
        self._reload()
        return self.data.get("routing_rules", {})

    def get_model(self, name: str):
        return self.models.get(name, {})

    def get_provider(self, name: str):
        return self.providers.get(name, {})

    def default_model(self):
        return self.routing_rules.get("default_model", list(self.models.keys())[0] if self.models else "deepseek-v4-pro")

    def fallback_model(self):
        return self.routing_rules.get("fallback_model", self.default_model())

    def models_for_task(self, task: str):
        return self.routing_rules.get("task_routing", {}).get(task, [self.default_model()])


registry = ModelRegistry(MODELS_FILE)

# ─── API Key ──────────────────────────────────────────


def resolve_api_key(provider: dict) -> Optional[str]:
    key = provider.get("api_key")
    if key:
        return key
    env_name = provider.get("api_key_env")
    if env_name:
        val = os.environ.get(env_name, "")
        return val if val else None
    return None


# ─── 健康检查 ──────────────────────────────────────────

_health_cache = {}
_health_lock = threading.Lock()


def check_provider_health(provider_name: str) -> bool:
    with _health_lock:
        cached = _health_cache.get(provider_name)
        if cached and time.time() - cached["ts"] < HEALTH_CACHE_TTL:
            return cached["ok"]

    if cb_is_open(provider_name):
        with _health_lock:
            _health_cache[provider_name] = {"ts": time.time(), "ok": False}
        return False

    provider = registry.get_provider(provider_name)
    if not provider:
        return False

    base_url = provider.get("base_url", "")
    api_key = resolve_api_key(provider)

    try:
        url = f"{base_url.rstrip('/')}/models"
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/json")
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        resp = urllib.request.urlopen(req, timeout=8)
        ok = resp.status == 200
        if ok:
            cb_success(provider_name)
    except Exception:
        ok = False
        cb_failure(provider_name)

    with _health_lock:
        _health_cache[provider_name] = {"ts": time.time(), "ok": ok}
    return ok


# ─── 任务推断 ──────────────────────────────────────────


def infer_task_from_messages(messages: list) -> str:
    full_text = " ".join(
        m.get("content", "") or ""
        for m in messages
        if isinstance(m.get("content"), str)
    ).lower()

    patterns = {
        "coding": r"\b(code|bug|fix|refactor|function|api|test|build|deploy|git|pr|commit|import |def |class |npm |pip |docker|react|python|typescript|rust|javascript|node|json|yaml|html|css)\b",
        "reasoning": r"\b(推理|逻辑|证明|分析原因|为什么|why|explain|reason|think|solve|数学|公式|定理|证明|推导)\b",
        "research": r"\b(研究|论文|paper|调查|survey|文献|arxiv|综述|总结|summary|报告|分析方法)\b",
        "creative": r"\b(写|创作|故事|诗歌|文案|设计|画|生成|generat|write|story|design|creative|创作|写出|撰写)\b",
        "lightweight": r"\b(简单|快速|短|quick|simple|hello|list|check|status|查看|列出|是什么|怎么|吗\?|呢\?)\b",
    }

    scores = {}
    for task, pattern in patterns.items():
        scores[task] = len(re.findall(pattern, full_text))

    # 中文单独判断：超过30%是中文字符 → 偏向中文模型
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", full_text))
    if len(full_text) > 0 and chinese_chars / len(full_text) > 0.15:
        scores["chinese"] = scores.get("chinese", 0) + 5

    if scores:
        best = max(scores, key=scores.get)
        if scores[best] > 0:
            return best
    return "general"


# ─── 模型选择 ──────────────────────────────────────────


def select_model(messages: list, requested_model: Optional[str] = None) -> tuple:
    """
    选择最佳模型。strategy: auto 会用 task inference；explicit 只用 registry 中的匹配模型。
    返回 (model_name, provider_name, base_url, api_key)
    """
    STRATEGY = os.environ.get("MODEL_ROUTER_STRATEGY", "auto")

    # 1. 显式策略 + 指定模型名 → 在注册表中查找
    if requested_model:
        model_info = registry.get_model(requested_model)
        if model_info:
            provider_name = model_info.get("provider", "")
            provider = registry.get_provider(provider_name)
            if provider and not cb_is_open(provider_name):
                api_key = resolve_api_key(provider)
                if api_key or provider.get("type") == "local":
                    return (requested_model, provider_name, provider["base_url"], api_key)

        # 模型在注册表但 Provider 不可用 → 尝试透传到默认 Provider
        default_prov = _get_safe_provider("deepseek") or _get_any_provider()
        if default_prov:
            return (requested_model, default_prov[0], default_prov[1]["base_url"], resolve_api_key(default_prov[1]))

    # 2. 自动策略 → 任务推断
    if STRATEGY == "auto":
        task = infer_task_from_messages(messages)
        candidates = registry.models_for_task(task)

        for model_name in candidates:
            model_info = registry.get_model(model_name)
            if not model_info:
                continue
            provider_name = model_info.get("provider", "")
            if cb_is_open(provider_name):
                log(f"Provider {provider_name} circuit open, skip {model_name}")
                continue
            provider = registry.get_provider(provider_name)
            if not provider:
                continue
            # 免费/本地 Provider 做健康检查；付费的信任可用（除非熔断）
            if provider.get("type") in ("free_local", "free_tier", "free_pool"):
                if not check_provider_health(provider_name):
                    log(f"Provider {provider_name} unhealthy, skip {model_name}")
                    continue
            api_key = resolve_api_key(provider)
            if not api_key and provider.get("type") != "local":
                log(f"Provider {provider_name} has no API key, skip {model_name}")
                continue
            log(f"Auto-selected: {model_name} ({provider_name}) for task '{task}'")
            return (model_name, provider_name, provider["base_url"], api_key)

    # 3. 回退：遍历所有 Provider 找第一个可用的
    for prov_name, provider in registry.providers.items():
        if cb_is_open(prov_name):
            continue
        api_key = resolve_api_key(provider)
        if api_key or provider.get("type") == "local":
            default_model_name = _find_model_for_provider(prov_name) or registry.default_model()
            log(f"Fallback: {default_model_name} ({prov_name})")
            return (default_model_name, prov_name, provider["base_url"], api_key)

    # 4. 最后的最后：硬编码 DeepSeek
    log("CRITICAL: No provider available, using hardcoded DeepSeek fallback")
    return (
        "deepseek-v4-pro",
        "deepseek",
        "https://api.deepseek.com/v1",
        os.environ.get("DEEPSEEK_API_KEY", ""),
    )


def _get_safe_provider(name: str):
    """安全获取 Provider，不存在返回 None 而不是崩溃"""
    provider = registry.get_provider(name)
    if provider and not cb_is_open(name):
        return (name, provider)
    return None


def _get_any_provider():
    """获取任意可用的 Provider"""
    for name, prov in registry.providers.items():
        if not cb_is_open(name):
            return (name, prov)
    return None


def _find_model_for_provider(prov_name: str) -> Optional[str]:
    for model_name, info in registry.models.items():
        if info.get("provider") == prov_name:
            return model_name
    return None


# ─── 请求转发 ──────────────────────────────────────────


def forward_request(base_url: str, api_key: Optional[str], path: str, body: bytes, headers: dict):
    """转发到目标 Provider，返回 (response, error_type)。
    error_type: None=成功, 'http'=HTTP错误, 'network'=网络错误"""
    target_url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"

    req = urllib.request.Request(target_url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    for key in ["Accept", "User-Agent"]:
        if key in headers:
            req.add_header(key, headers[key])

    try:
        resp = urllib.request.urlopen(req, timeout=300)
        return (resp, None)
    except urllib.error.HTTPError as e:
        return (e, "http")
    except Exception as e:
        log(f"Network error to {target_url}: {e}")
        return (None, "network")


# ─── HTTP 服务器（多线程） ──────────────────────────────


class ThreadingHTTPServer(ThreadingMixIn, http.server.HTTPServer):
    """多线程 HTTP 服务器——多个智能体可同时请求"""
    daemon_threads = True
    allow_reuse_address = True


class ModelRouterHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        log(f"{self.client_address[0]} - {format % args}")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            self._json_response({
                "status": "ok",
                "router": "hermes-model-router",
                "version": "1.1",
                "models": len(registry.models),
                "providers": len(registry.providers),
                "circuits_open": [k for k, v in _circuit_breaker.items() if v.get("open_until", 0) > time.time()],
            })
        elif parsed.path == "/v1/models":
            models = []
            for name, info in registry.models.items():
                models.append({
                    "id": name,
                    "object": "model",
                    "owned_by": info.get("provider", "?"),
                    "cost": info.get("cost", "paid"),
                    "context_length": info.get("context_length", 32768),
                    "capabilities": info.get("capabilities", []),
                    "strength": info.get("strength", ""),
                })
            self._json_response({"object": "list", "data": models})
        elif parsed.path == "/registry":
            self._json_response(registry.data)
        elif parsed.path == "/providers/health":
            health = {n: check_provider_health(n) for n in registry.providers}
            circuits = {n: cb_is_open(n) for n in registry.providers}
            self._json_response({"health": health, "circuits": circuits})
        elif parsed.path == "/dashboard" or parsed.path == "/":
            self._serve_dashboard()
        else:
            self._json_response({"error": "Not found"}, status=404)

    def _serve_dashboard(self):
        """Serve the HTML dashboard"""
        dashboard_path = ROUTER_DIR / "dashboard.html"
        if dashboard_path.exists():
            html = dashboard_path.read_text(encoding="utf-8")
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json_response({"error": "Dashboard not found"}, status=404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/v1/chat/completions", "/chat/completions"):
            self._handle_chat_completion()
        else:
            self._json_response({"error": "Only /v1/chat/completions is supported"}, status=404)

    def _handle_chat_completion(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > 10 * 1024 * 1024:  # 10MB limit
                self._json_response({"error": "Request too large"}, status=413)
                return
            body = self.rfile.read(content_length)
            data = json.loads(body)

            messages = data.get("messages", [])
            requested_model = data.get("model")
            stream = data.get("stream", False)

            strategy = self.headers.get("X-Model-Router-Strategy", os.environ.get("MODEL_ROUTER_STRATEGY", "auto"))

            # 选择模型
            model_name, provider_name, base_url, api_key = select_model(messages, requested_model)

            log(f"Routing: {requested_model or 'auto'} → {model_name} ({provider_name}) stream={stream}")

            # 替换 model 并转发
            data["model"] = model_name
            body = json.dumps(data).encode("utf-8")

            resp, err_type = forward_request(base_url, api_key, "chat/completions", body, self.headers)

            # 回退逻辑：任何失败都尝试回退
            if err_type is not None:
                cb_failure(provider_name)
                log(f"Primary failed ({err_type}), trying fallback...")

                # 尝试回退模型
                fallback_name = registry.fallback_model()
                fb_info = registry.get_model(fallback_name)
                fb_prov_name = fb_info.get("provider", "") if fb_info else ""
                fb_provider = registry.get_provider(fb_prov_name)

                if fb_provider and not cb_is_open(fb_prov_name) and fb_prov_name != provider_name:
                    data["model"] = fallback_name
                    body = json.dumps(data).encode("utf-8")
                    resp, err_type = forward_request(
                        fb_provider["base_url"], resolve_api_key(fb_provider),
                        "chat/completions", body, self.headers,
                    )
                    if err_type is None:
                        model_name, provider_name = fallback_name, fb_prov_name
                        log(f"Fallback OK: {fallback_name} ({fb_prov_name})")

                if err_type is not None:
                    # 最后尝试：找任意可用 Provider
                    for pn, pv in registry.providers.items():
                        if pn == provider_name or pn == fb_prov_name or cb_is_open(pn):
                            continue
                        ak = resolve_api_key(pv)
                        if not ak and pv.get("type") != "local":
                            continue
                        any_model = _find_model_for_provider(pn) or "deepseek-v4-pro"
                        data["model"] = any_model
                        body = json.dumps(data).encode("utf-8")
                        resp, err_type = forward_request(pv["base_url"], ak, "chat/completions", body, self.headers)
                        if err_type is None:
                            model_name, provider_name = any_model, pn
                            log(f"Last-resort fallback OK: {any_model} ({pn})")
                            break

            if err_type is not None or resp is None:
                self._json_response({"error": "All providers failed"}, status=502)
                return

            # 返回响应
            resp_status = resp.status if hasattr(resp, 'status') else resp.getcode()
            self.send_response(resp_status)
            self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
            self.send_header("X-Routed-Model", model_name)
            self.send_header("X-Routed-Provider", provider_name)
            self.send_header("Access-Control-Allow-Origin", "*")

            if stream:
                # 真正的流式传输：逐行转发 SSE
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()

                buf = b""
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        self.wfile.write(line + b"\n")
                        self.wfile.flush()
                if buf:
                    self.wfile.write(buf)
                    self.wfile.flush()
            else:
                content_length = resp.headers.get("Content-Length")
                if content_length:
                    self.send_header("Content-Length", content_length)
                self.end_headers()
                body_resp = resp.read()
                self.wfile.write(body_resp)

            cb_success(provider_name)

        except json.JSONDecodeError:
            self._json_response({"error": "Invalid JSON"}, status=400)
        except Exception as e:
            log(f"Error: {traceback.format_exc()}")
            self._json_response({"error": str(e)}, status=500)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def _json_response(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ─── 日志 ──────────────────────────────────────────────


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line, file=sys.stderr, flush=True)
    try:
        with open(ROUTER_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ─── 入口 ──────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Hermes Model Router v1.1")
    parser.add_argument("--port", type=int, default=9099)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--strategy", default="auto", choices=["auto", "explicit"])
    args = parser.parse_args()

    os.environ["MODEL_ROUTER_STRATEGY"] = args.strategy

    log(f"Model Router v1.1 on {args.host}:{args.port} (strategy={args.strategy}, threaded)")
    log(f"Loaded {len(registry.models)} models across {len(registry.providers)} providers")

    server = ThreadingHTTPServer((args.host, args.port), ModelRouterHandler)

    try:
        log("Ready.")
        server.serve_forever()
    except KeyboardInterrupt:
        log("Shutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
