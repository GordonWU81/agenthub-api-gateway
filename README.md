<div align="center">

# AgentHub API Gateway

**OpenAI-compatible API Gateway with built-in billing, Model Router, and self-hosted payment**

[![GitHub stars](https://img.shields.io/github/stars/GordonWU81/agenthub-api-gateway?style=flat-square)](https://github.com/GordonWU81/agenthub-api-gateway/stargazers)
[![License](https://img.shields.io/github/license/GordonWU81/agenthub-api-gateway?style=flat-square)](LICENSE)
[![Status](https://img.shields.io/badge/status-production%20ready-green?style=flat-square)]()
[![Models](https://img.shields.io/badge/models-100+-blue?style=flat-square)]()

[English](README.md) | [中文](#)

</div>

---

## 📋 Overview

AgentHub API Gateway is a production-ready API gateway for LLMs that combines:
- **Model Router** — 100++ models from 5 providers with automatic failover
- **API Key Authentication** — user management, token-based billing
- **Self-hosted Payment** — Alipay, WeChat Pay, and USDT/TRC20 crypto
- **Self-registration** — users sign up and get API keys automatically

Designed for independent developers and small teams who want to offer AI API services without relying on third-party payment platforms.

## ✨ Features

### 🧠 Smart Model Routing
- 100++ models from DeepSeek, SiliconFlow, and other providers
- Automatic failover when a provider is unavailable
- Circuit breaker pattern prevents cascading failures
- Routes to the cheapest available model first

### 🔑 API Key Management
- Self-service user registration
- Per-user API keys with granular control
- Token-based billing (¥3 / 1M tokens)
- Usage logs and balance tracking

### 💳 Self-Hosted Payment
- **No third-party payment platform required**
- Alipay QR code (scan to pay)
- WeChat Pay QR code
- USDT/TRC20 cryptocurrency (global payments)
- Admin confirms payment → tokens auto-credited
- Webhook integration for auto-topup

### 🌐 Multi-Language Ready
- Payment page supports Chinese & English (crypto)
- OpenAI SDK compatible
- Works with any OpenAI client

## 🚀 Quick Start

### For Users

```python
import openai

client = openai.OpenAI(
    base_url="https://api.hermes.example/v1",
    api_key="sk-your-api-key-here"
)

response = client.chat.completions.create(
    model="deepseek-chat",
    messages=[{"role": "user", "content": "Hello!"}]
)
print(response.choices[0].message.content)
```

### For Developers (Self-Host)

```bash
# 1. Install dependencies
pip install requests

# 2. Start Model Router
python3 model_router.py --port 9099

# 3. Start API Gateway
python3 gateway.py --port 9199 --router http://127.0.0.1:9099

# 4. Access the service
curl http://localhost:9199/v1/health
```

## 🏗️ Architecture

```
User → Nginx (:80)
         ├── /v1/* → API Gateway (:9199) → Model Router (:9099) → LLM Providers
         ├── /pay/* → Payment Platform (:9188) → Alipay / WeChat / USDT
         ├── /api   → Landing Page
         └── /       → Forum / Blog
```

### Component Details

| Component | Port | Description |
|-----------|:----:|-------------|
| Model Router | 9099 | Routes requests to 100++ models, handles failover |
| API Gateway | 9199 | API key auth, token billing, user management |
| Payment Platform | 9188 | Order creation, payment pages, webhook callbacks |

## 🔧 Available Models

100++ models from 5 providers including:
- DeepSeek (V2, V3, chat, coder)
- SiliconFlow (various models)
- And more...

## 📊 Pricing

| Amount | Tokens | Price/M Tokens |
|:------:|:------:|:--------------:|
| Free registration | 330K | — |
| ¥10 (~$1.40) | 3.3M | ¥3 |
| ¥30 (~$4.20) | 10M | ¥3 |
| ¥100 (~$14) | 33M | ¥3 |
| $1 USDT | 2.4M | — |

## 📦 Components

```
agenthub-api-gateway/
├── gateway.py          # API Gateway (auth + billing + registration)
├── model_router.py     # Model Router (routing + failover + circuit breaker)
├── payment_platform.py # Self-hosted payment (Alipay/WeChat/USDT)
├── render_xhs.py       # XHS image renderer
├── landing.html        # Landing page
└── README.md
```

## 💳 Payment Setup

1. Place your Alipay QR code at `qrcodes/alipay_qr.png`
2. Place your WeChat QR code at `qrcodes/wechat_qr.png`
3. Set your USDT/TRC20 wallet address in `payment_platform.py`
4. Access admin panel at `/pay/admin` (password: configured in `payment_platform.py`)

## 🛡️ Anti-Bot Note

Xiaohongshu (Little Red Book) auto-publishing requires a real browser session.
Use Playwright with your cookies to automate posts - the Chromium binary is included.

## 📄 License

MIT

---

<div align="center">
  <p><strong>Built by one person, for everyone.</strong></p>
  <p>独立开发者 · 一人企业 · 开源</p>
</div>
