# Agnes 网页模拟 API

通过无头浏览器操作 [app.agnes-ai.com](https://app.agnes-ai.com/)，经本地 HTTP 代理访问，对外提供 HTTP 接口。

默认地址：`http://127.0.0.1:8765`  
前缀：`/api/agnes`

浏览器流量默认走 `http://127.0.0.1:7890`（可用 `AGNES_PROXY` 覆盖）。DeepSeek / Qwen 模块不受影响。

**默认关闭**：需在 `.env` 设置 `AGNES_ENABLED=1` 后才会启动 / 接受 chat；未开启时 `/api/agnes/chat` 返回 503。

## 启动

```bash
conda activate flask_env
cd backend
python main.py
```

或在 Cursor 使用 launch 配置：`Python: Flask`。

启动前在项目根目录 `.env` 配置账号与 Chrome 路径（见文末）。

服务启动时会自动用 `.env` 中的账号登录 Agnes；会话失效时，`/chat` 也会自动重登。无需也不提供独立 login 接口。

访客态也能看到聊天输入框，但多轮续聊**必须账号登录**（与 Qwen account 模式一致）。

---

## 快速开始（多轮对话）

### 1. 新开对话

不传 `conversation_id` 即新建会话。

```bash
curl -s http://127.0.0.1:8765/api/agnes/chat \
  -H "Content-Type: application/json" \
  -d '{"question":"你好，介绍一下你自己","mode":"auto"}'
```

响应示例：

```json
{
  "ok": true,
  "question": "你好，介绍一下你自己",
  "answer": "...",
  "conversation_id": "346576967445184512",
  "mode": "auto",
  "deep_thinking": false,
  "search": false,
  "url": "https://app.agnes-ai.com/?conversationId=346576967445184512"
}
```

请保存返回的 `conversation_id`，后续多轮必须带上。

### 2. 多轮续聊

```bash
curl -s http://127.0.0.1:8765/api/agnes/chat \
  -H "Content-Type: application/json" \
  -d '{"conversation_id":"上一轮返回的id","question":"再说详细一点"}'
```

---

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 进程健康检查（根路径也有 `/health`） |
| GET | `/api/agnes/health` | Agnes 模块健康检查 |
| GET | `/api/agnes/doc` | 返回本文档 Markdown 原文 |
| GET | `/api/agnes/status` | 登录态 / 浏览器状态 |
| POST | `/api/agnes/chat` | 提问（同步，长内容易被网关超时掐断） |
| POST | `/api/agnes/chat/async` | 异步提问（立即返回 `job_id`） |
| GET | `/api/agnes/chat/jobs/<id>` | 查询异步任务状态 / 结果 |
| GET | `/api/agnes/conversations` | 本地会话列表 |
| GET | `/api/agnes/conversations/<id>` | 会话详情 + 消息历史 |

---

## POST `/api/agnes/chat`

### 请求体

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `question` | string | 是 | — | 用户问题（别名：`prompt` / `message`） |
| `conversation_id` | string | 否 | 无 | **有则续聊，无则新开**（别名：`chat_id`） |
| `mode` | string | 否 | `auto` | 保留字段；当前 **no-op**（页面无可靠模型切换） |
| `deep_thinking` | bool | 否 | `false` | 保留字段；当前 **no-op**（别名：`think` / `deep_think`） |
| `search` | bool | 否 | `false` | 保留字段；当前 **no-op**（别名：`web_search` / `smart_search`） |
| `timeout` | int | 否 | 见下 | 等待回复秒数；不传时：普通 `AGNES_CHAT_TIMEOUT_S`(默认 300)，深度思考用 `AGNES_THINK_TIMEOUT_S`(默认 600) |

说明：

- `mode` / `deep_thinking` / `search` 与 Qwen API 形状对齐，便于共用客户端；Agnes 网页侧暂未实现对应切换，传入会被忽略。
- 超时仍会因 `deep_thinking=true` 走更长的 `AGNES_THINK_TIMEOUT_S`。

### 成功响应

```json
{
  "ok": true,
  "question": "...",
  "answer": "...",
  "conversation_id": "...",
  "mode": "auto",
  "deep_thinking": true,
  "search": false,
  "url": "https://app.agnes-ai.com/...",
  "worker_id": 0
}
```

说明：`worker_id` 表示处理该请求的浏览器 worker（`AGNES_WORKERS` 并行池中的编号）。多路 `/chat` 会分到不同空闲 worker；worker 都忙时仍会排队。

### 错误码

| HTTP | 含义 |
| --- | --- |
| 400 | 参数错误（缺 question 等） |
| 401 | 未登录 / 自动登录失败 |
| 503 | `AGNES_ENABLED=0` |
| 504 | 等待回复超时 |
| 500 | 其它异常 |

---

## 异步 Chat（推荐长内容）

长回复容易超过客户端或网关 HTTP 超时。异步接口提交后立即返回，再轮询结果。

### 1. 提交

`POST /api/agnes/chat/async`

请求体与同步 `/chat` 相同。

```bash
curl -s http://127.0.0.1:8765/api/agnes/chat/async \
  -H "Content-Type: application/json" \
  -d '{"question":"写一篇较长的说明","mode":"auto"}'
```

响应 `202`：

```json
{
  "ok": true,
  "job_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "status": "queued",
  "provider": "agnes"
}
```

### 2. 轮询

`GET /api/agnes/chat/jobs/<job_id>`

`status`：`queued` / `running` / `succeeded` / `failed`。

成功示例：

```json
{
  "ok": true,
  "job_id": "...",
  "provider": "agnes",
  "status": "succeeded",
  "question": "...",
  "created_at": "...",
  "started_at": "...",
  "finished_at": "...",
  "result": {
    "ok": true,
    "question": "...",
    "answer": "...",
    "conversation_id": "...",
    "mode": "auto",
    "deep_thinking": false,
    "search": false,
    "url": "https://app.agnes-ai.com/...",
    "worker_id": 0
  }
}
```

失败时带 `error` / `error_kind`（`value` / `runtime` / `timeout` / `other`）。

Python 轮询示例：

```python
import time
import requests

BASE = "http://127.0.0.1:8765/api/agnes"
r = requests.post(
    f"{BASE}/chat/async",
    json={"question": "你好", "mode": "auto"},
    timeout=30,
)
r.raise_for_status()
job_id = r.json()["job_id"]

while True:
    j = requests.get(f"{BASE}/chat/jobs/{job_id}", timeout=30).json()
    status = j.get("status")
    if status == "succeeded":
        print(j["result"]["answer"])
        break
    if status == "failed":
        raise RuntimeError(j.get("error"))
    time.sleep(2)
```

---

## GET `/api/agnes/status`

```json
{
  "ok": true,
  "enabled": true,
  "ready": true,
  "state": "chat",
  "url": "https://app.agnes-ai.com/...",
  "headless": true,
  "browser": "channel:chrome",
  "proxy": "http://127.0.0.1:7890",
  "session_saved": true,
  "workers": 2,
  "busy": 0,
  "idle": 2,
  "queued": 0
}
```

`state` 常见值：`chat` / `auth` / `unknown` / `disabled`。

---

## 会话查询

`GET /api/agnes/conversations?limit=50`

`GET /api/agnes/conversations/<conversation_id>`

会话与消息存在本地 SQLite，按 `provider=agnes` 隔离。

会话 URL 形态：`https://app.agnes-ai.com/?conversationId=<id>`。接口返回的 `conversation_id` 即该查询参数；续聊优先用本地保存的完整 URL。

---

## 登录说明

登录由服务端自动完成：启动时、以及 `/chat` 发现未登录时，都会用 `.env` 的 `AGNES_USERNAME` / `AGNES_PASSWORD` 填表登录，并把登录态写入 SQLite（`browser_session`）。

登录页：`https://app.agnes-ai.com/login`（邮箱 tab → `#login_email` / `#login_password` → `#login`）。

成功标志：cookie / localStorage 含 token、session、auth 等线索；或已离开 `/login`、聊天输入框可见、且顶栏无「登录」按钮。

---

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `AGNES_ENABLED` | 总开关，默认 `0`（关闭）；设为 `1` 开启 |
| `AGNES_PROXY` | Playwright 代理，默认 `http://127.0.0.1:7890` |
| `AGNES_TIMEOUT_MS` | Playwright UI 操作超时，默认 `120000` |
| `AGNES_CHAT_TIMEOUT_S` | 普通回复等待秒数，默认 `300` |
| `AGNES_THINK_TIMEOUT_S` | `deep_thinking=true` 时等待秒数，默认 `600`（开关本身为 no-op） |
| `AGNES_WORKERS` | 并行浏览器 worker 数（1–4），默认 2 |
| `AGNES_AUTO_LOGIN` | 是否自动填表登录，默认 `1` |
| `AGNES_USERNAME` / `AGNES_EMAIL` | 登录账号 |
| `AGNES_PASSWORD` | 登录密码 |
| `BOOTSTRAP_AGNES` | 启动时是否预热登录，默认 `1`（仅当 `AGNES_ENABLED=1`） |
| `HEADLESS` / `BROWSER_CHANNEL` / `CHROME_PATH` | 与 DeepSeek / ChatGPT / Qwen 共用 |
