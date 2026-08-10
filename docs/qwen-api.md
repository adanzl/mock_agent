# Qwen 网页模拟 API

通过无头浏览器操作 [chat.qwen.ai](https://chat.qwen.ai)，对外提供 HTTP 接口。

默认地址：`http://127.0.0.1:8765`  
前缀：`/api/qwen`

**默认关闭**：需在 `.env` 设置 `QWEN_ENABLED=1` 后才会启动 / 接受 chat；未开启时 `/api/qwen/chat` 返回 503。

## 启动

```bash
conda activate flask_env
cd backend
python main.py
```

或在 Cursor 使用 launch 配置：`Python: Flask`。

启动前在项目根目录 `.env` 配置账号与 Chrome 路径（见文末）。

服务启动时会自动用 `.env` 中的账号登录 Qwen；会话失效时，`/chat` 也会自动重登。无需也不提供独立 login 接口。

未登录时若页面仍有输入框（访客态），也可发消息；多轮续聊建议配置账号登录。

---

## 快速开始（多轮对话）

### 1. 新开对话

不传 `conversation_id` 即新建会话。

```bash
curl -s http://127.0.0.1:8765/api/qwen/chat \
  -H "Content-Type: application/json" \
  -d '{"question":"你好，介绍一下你自己","mode":"auto"}'
```

响应示例：

```json
{
  "ok": true,
  "question": "你好，介绍一下你自己",
  "answer": "...",
  "conversation_id": "xxxxxxxx",
  "mode": "auto",
  "deep_thinking": false,
  "search": false,
  "url": "https://chat.qwen.ai/c/..."
}
```

请保存返回的 `conversation_id`，后续多轮必须带上。

### 2. 多轮续聊

```bash
curl -s http://127.0.0.1:8765/api/qwen/chat \
  -H "Content-Type: application/json" \
  -d '{"conversation_id":"上一轮返回的id","question":"再说详细一点"}'
```

---

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 进程健康检查（根路径也有 `/health`） |
| GET | `/api/qwen/health` | Qwen 模块健康检查 |
| GET | `/api/qwen/doc` | 返回本文档 Markdown 原文 |
| GET | `/api/qwen/status` | 登录态 / 浏览器状态 |
| POST | `/api/qwen/chat` | 提问（同步，长内容易被网关超时掐断） |
| POST | `/api/qwen/chat/async` | 异步提问（立即返回 `job_id`） |
| GET | `/api/qwen/chat/jobs/<id>` | 查询异步任务状态 / 结果 |
| GET | `/api/qwen/conversations` | 本地会话列表 |
| GET | `/api/qwen/conversations/<id>` | 会话详情 + 消息历史 |

---

## POST `/api/qwen/chat`

### 请求体

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `question` | string | 是 | — | 用户问题（别名：`prompt` / `message`） |
| `conversation_id` | string | 否 | 无 | **有则续聊，无则新开**（别名：`chat_id`） |
| `mode` | string | 否 | `auto` | 模型（别名：`model`）；`auto` 不切换 |
| `deep_thinking` | bool | 否 | `false` | 深度思考 / Thinking（别名：`think` / `deep_think`） |
| `search` | bool | 否 | `false` | 联网搜索（别名：`web_search` / `smart_search`） |
| `timeout` | int | 否 | 见下 | 等待回复秒数；不传时：普通 `QWEN_CHAT_TIMEOUT_S`(默认 300)，深度思考用 `QWEN_THINK_TIMEOUT_S`(默认 600) |

### `mode` 取值

| 值 | 含义 |
| --- | --- |
| `auto` / `default` | 不切换模型，沿用页面当前选择 |
| `plus` / `qwen3.5-plus` | 尝试选 `Qwen3.5-Plus` |
| `flash` / `qwen3.5-flash` | 尝试选 `Qwen3.5-Flash` |
| `max` / `qwen3-max` | 尝试选 `Qwen3-Max` |
| 其它字符串 | 按页面模型选择器文案精确匹配（如 `Qwen3.6-Plus`） |

说明：

- **模型只在新对话时尝试切换**；带 `conversation_id` 续聊时不切换 `mode`。
- 页面 DOM 常变，模型 / 思考 / 搜索切换为 best-effort；失败会打日志，不阻断发消息。

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
  "url": "https://chat.qwen.ai/c/...",
  "worker_id": 0
}
```

说明：`worker_id` 表示处理该请求的浏览器 worker（`QWEN_WORKERS` 并行池中的编号）。多路 `/chat` 会分到不同空闲 worker，不再互相堵死；worker 都忙时仍会排队。

### 错误码

| HTTP | 含义 |
| --- | --- |
| 400 | 参数错误（缺 question 等） |
| 401 | 未登录 / 自动登录失败 |
| 503 | `QWEN_ENABLED=0` |
| 504 | 等待回复超时 |
| 500 | 其它异常 |

---

## 异步 Chat（推荐长内容）

长回复 / 深度思考容易超过客户端或网关 HTTP 超时。异步接口提交后立即返回，再轮询结果。

### 1. 提交

`POST /api/qwen/chat/async`

请求体与同步 `/chat` 相同。

```bash
curl -s http://127.0.0.1:8765/api/qwen/chat/async \
  -H "Content-Type: application/json" \
  -d '{"question":"写一篇较长的说明","mode":"auto","deep_thinking":true}'
```

响应 `202`：

```json
{
  "ok": true,
  "job_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "status": "queued",
  "provider": "qwen"
}
```

### 2. 轮询

`GET /api/qwen/chat/jobs/<job_id>`

`status`：`queued` / `running` / `succeeded` / `failed`。

成功示例：

```json
{
  "ok": true,
  "job_id": "...",
  "provider": "qwen",
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
    "deep_thinking": true,
    "search": false,
    "url": "https://chat.qwen.ai/c/...",
    "worker_id": 0
  }
}
```

失败时带 `error` / `error_kind`（`value` / `runtime` / `timeout` / `other`）。

Python 轮询示例：

```python
import time
import requests

BASE = "http://127.0.0.1:8765/api/qwen"
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

## GET `/api/qwen/status`

```json
{
  "ok": true,
  "enabled": true,
  "ready": true,
  "state": "chat",
  "url": "https://chat.qwen.ai/...",
  "headless": true,
  "browser": "channel:chrome",
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

`GET /api/qwen/conversations?limit=50`

`GET /api/qwen/conversations/<conversation_id>`

会话与消息存在本地 SQLite，按 `provider=qwen` 隔离。

---

## 登录说明

登录由服务端自动完成：启动时、以及 `/chat` 发现未登录时，都会用 `.env` 的 `QWEN_USERNAME` / `QWEN_PASSWORD` 填表登录，并把登录态写入 SQLite（`browser_session`）。

登录页：`https://chat.qwen.ai/auth`。成功标志为 `token` cookie（domain 含 `qwen.ai`）或聊天输入框可见。

---

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `QWEN_ENABLED` | 总开关，默认 `0`（关闭）；设为 `1` 开启 |
| `QWEN_TIMEOUT_MS` | Playwright UI 操作超时，默认 `120000` |
| `QWEN_CHAT_TIMEOUT_S` | 普通回复等待秒数，默认 `300` |
| `QWEN_THINK_TIMEOUT_S` | 深度思考回复等待秒数，默认 `600` |
| `QWEN_WORKERS` | 并行浏览器 worker 数（1–4），默认 2；每个 worker 独立页面，多路 `/chat` 可同时进行 |
| `QWEN_AUTO_LOGIN` | 是否自动填表登录，默认 `1` |
| `QWEN_USERNAME` / `QWEN_EMAIL` | 登录账号 |
| `QWEN_PASSWORD` | 登录密码 |
| `BOOTSTRAP_QWEN` | 启动时是否预热登录，默认 `1`（仅当 `QWEN_ENABLED=1`） |
| `HEADLESS` / `BROWSER_CHANNEL` / `CHROME_PATH` | 与 DeepSeek / ChatGPT 共用 |
