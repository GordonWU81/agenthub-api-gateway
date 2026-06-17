# Hermes API Gateway

> 基于 Model Router 的 OpenAI 兼容 API 计费网关，内置支付宝支付

## ✨ 功能

- **OpenAI 兼容接口** — 一行代码切换 `base_url` 即可使用
- **72+ 模型** — DeepSeek、SiliconFlow 等主流模型
- **API Key 认证** — 安全可控的访问管理
- **Token 计费** — 自动追踪用量并扣费
- **自助充值** — 支付宝扫码充值，1元=10万tokens
- **用户管理** — 创建子用户、分配额度、用量报表

## 🚀 快速开始

```bash
# 安装依赖
pip install requests

# 启动 Model Router
python3 model_router.py --port 9099

# 启动 API Gateway
python3 gateway.py --port 9199 --router http://127.0.0.1:9099
```

## 🔑 使用示例

```python
# 一行切换
import openai
client = openai.OpenAI(
    base_url="http://47.97.68.146/v1",
    api_key="sk-admin-xxxxxxxxxxxx"
)

# 正常调用
response = client.chat.completions.create(
    model="deepseek-v4-flash",
    messages=[{"role": "user", "content": "Hello!"}]
)
```

## 💳 自助充值

```
http://47.97.68.146/v1/payment/page
```

## ⚙️ 架构

```
用户 → API Gateway (:9199) → Model Router (:9099) → LLM Provider
       ├── API Key 认证
       ├── Token 计费
       └── 面包多支付
```

## 📄 License

MIT
