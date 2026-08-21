# DeepSeek 网页模拟 API

通过无头浏览器操作 [chat.deepseek.com](https://chat.deepseek.com)，对外提供 HTTP 接口。

默认地址：`http://127.0.0.1:8765`  
前缀：`/api/deepseek`

## 启动

```bash
conda activate flask_env
cd backend
python main.py
```

或在 Cursor 使用 launch 配置：`Python: Flask`。

启动前在项目根目录 `.env` 配置账号与 Chrome 路径（见文末）。

服务启动时会自动用 `.env` 中的账号登录 DeepSeek；会话失效时，`/chat` 也会自动重登。无需也不提供独立 login 接口。

---

## 快速开始（多轮对话）

### 1. 新开对话

不传 `conversation_id` 即新建会话。

```bash
curl -s http://127.0.0.1:8765/api/deepseek/chat ^
  -H "Content-Type: application/json" ^
  -d "{\"question\":\"你好，介绍一下你自己\",\"mode\":\"instant\"}"
```

响应示例：

```json
{
  "ok": true,
  "question": "你好，介绍一下你自己",
  "answer": "...",
  "conversation_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "mode": "instant",
  "deep_thinking": false,
  "search": false,
  "url": "https://chat.deepseek.com/a/chat/s/..."
}
```

请保存返回的 `conversation_id`，后续多轮必须带上。

### 2. 多轮续聊

```bash
curl -s http://127.0.0.1:8765/api/deepseek/chat ^
  -H "Content-Type: application/json" ^
  -d "{\"conversation_id\":\"上一轮返回的id\",\"question\":\"再说详细一点\"}"
```

---

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 进程健康检查（根路径也有 `/health`） |
| GET | `/api/deepseek/health` | DeepSeek 模块健康检查 |
| GET | `/api/deepseek/doc` | 返回本文档 Markdown 原文 |
| GET | `/api/deepseek/status` | 登录态 / 浏览器状态 |
| POST | `/api/deepseek/chat` | 提问（同步，长内容易被网关超时掐断） |
| POST | `/api/deepseek/chat/async` | 异步提问（立即返回 `job_id`） |
| GET | `/api/deepseek/chat/jobs/<id>` | 查询异步任务状态 / 结果 |
| GET | `/api/deepseek/conversations` | 本地会话列表 |
| GET | `/api/deepseek/conversations/<id>` | 会话详情 + 消息历史（本地无记录时可加 `?sync=1` 从网页拉取） |
| POST | `/api/deepseek/conversations/<id>/sync` | 从 chat.deepseek.com 拉取账号内已有会话并写入本地库 |

---

## POST `/api/deepseek/chat`

### 请求体

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `question` | string | 是 | — | 用户问题（别名：`prompt` / `message`） |
| `conversation_id` | string | 否 | 无 | **有则续聊，无则新开**（别名：`chat_id`） |
| `mode` | string | 否 | `instant` | 模式（别名：`model`） |
| `deep_thinking` | bool | 否 | `false` | 深度思考（别名：`think` / `deep_think`） |
| `search` | bool | 否 | `false` | 智能搜索（别名：`web_search` / `smart_search`） |
| `timeout` | int | 否 | 见下 | 等待回复秒数；不传时：普通 `DEEPSEEK_CHAT_TIMEOUT_S`(默认 300)，专家/深度思考用 `DEEPSEEK_THINK_TIMEOUT_S`(默认 600) |

### `mode` 取值

| 值 | 含义 |
| --- | --- |
| `instant` / `fast` / `快速` / `快速模式` | 快速模式 |
| `expert` / `专家` / `专家模式` | 专家模式 |

说明：

- 专家模式不支持 `search=true`，否则返回 400。
- **模式只在新对话时生效**；带 `conversation_id` 续聊时不切换 `mode`，沿用该会话已有模式。

### 成功响应

```json
{
  "ok": true,
  "question": "...",
  "answer": "...",
  "conversation_id": "uuid",
  "mode": "instant",
  "deep_thinking": true,
  "search": false,
  "url": "https://chat.deepseek.com/a/chat/s/uuid",
  "worker_id": 0
}
```

说明：`worker_id` 表示处理该请求的浏览器 worker（`DEEPSEEK_WORKERS` 并行池中的编号）。多路 `/chat` 会分到不同空闲 worker，不再互相堵死；worker 都忙时仍会排队。

### 错误码

| HTTP | 含义 |
| --- | --- |
| 400 | 参数错误（缺 question、非法 mode、专家+搜索等） |
| 401 | 未登录 / 运行时失败 |
| 504 | 等待回复超时 |
| 500 | 其他异常 |

---

## 异步 Chat（推荐长内容）

长回复 / 专家模式 / 深度思考容易超过客户端或网关 HTTP 超时。异步接口提交后立即返回，再轮询结果。

### 1. 提交

`POST /api/deepseek/chat/async`

请求体与同步 `/chat` 相同。

```bash
curl -s http://127.0.0.1:8765/api/deepseek/chat/async \
  -H "Content-Type: application/json" \
  -d "{\"question\":\"写一篇较长的说明\",\"mode\":\"expert\",\"deep_thinking\":true}"
```

响应 `202`：

```json
{
  "ok": true,
  "job_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "status": "queued",
  "provider": "deepseek"
}
```

### 2. 轮询

`GET /api/deepseek/chat/jobs/<job_id>`

`status`：`queued` / `running` / `succeeded` / `failed`。

成功时响应含 `result`（结构与同步 `/chat` 成功体相同）；失败时含 `error` / `error_kind`（`value` / `runtime` / `timeout` / `other`）。

Python 轮询示例：

```python
import time
import requests

BASE = "http://127.0.0.1:8765/api/deepseek"
r = requests.post(
    f"{BASE}/chat/async",
    json={"question": "你好", "mode": "instant"},
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

## 带选项示例

快速模式 + 深度思考 + 联网搜索：

```json
{
  "question": "今天有哪些 AI 新闻？",
  "mode": "instant",
  "deep_thinking": true,
  "search": true,
  "timeout": 180
}
```

专家模式 + 深度思考：

```json
{
  "question": "证明根号2是无理数",
  "mode": "expert",
  "deep_thinking": true
}
```

续聊并保持选项：

```json
{
  "conversation_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "question": "把证明写得更短一些",
  "mode": "expert",
  "deep_thinking": true
}
```

---

## 会话查询

### 列表

`GET /api/deepseek/conversations?limit=50`

```json
{
  "ok": true,
  "items": [
    {
      "id": "uuid",
      "title": "你好，介绍一下你自己",
      "mode": "instant",
      "deep_thinking": 0,
      "search": 0,
      "url": "...",
      "updated_at": "..."
    }
  ]
}
```

### 详情

`GET /api/deepseek/conversations/<conversation_id>`

本地 SQLite 里有记录时直接返回。若会话是你在 chat.deepseek.com 上手动开的、不是 mock-agent 创建的，本地可能没有；此时：

- 加 `?sync=1` 自动从网页拉取后再返回
- 或先调下面的 `POST .../sync`

```bash
curl -s "http://127.0.0.1:8765/api/deepseek/conversations/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx?sync=1"
```

```json
{
  "ok": true,
  "conversation": { "...": "..." },
  "messages": [
    { "id": 1, "role": "user", "content": "...", "created_at": "..." },
    { "id": 2, "role": "assistant", "content": "...", "created_at": "..." }
  ]
}
```

### 从网页同步已有会话

`POST /api/deepseek/conversations/<conversation_id>/sync`

用 `.env` 账号登录态打开 DeepSeek 网页上的该会话，滚动加载全部消息后写入本地库（**覆盖**本地同 id 的消息）。

```bash
curl -s -X POST "http://127.0.0.1:8765/api/deepseek/conversations/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx/sync"
```

成功响应含 `conversation`、`messages`、`message_count`、`synced: true`。之后可带该 `conversation_id` 续聊。

本地库路径由 `SQLITE_PATH` 决定，默认 `backend/data/data.db`。

---

## 登录与状态

登录由服务端自动完成：启动时、以及 `/chat` 发现未登录时，都会用 `.env` 的 `DEEPSEEK_USERNAME` / `DEEPSEEK_PASSWORD` 填表登录，并把登录态写入 SQLite（`browser_session`）。

查看当前状态：

`GET /api/deepseek/status`

关注字段：`ready`（是否在聊天页）、`state`（`chat` / `auth` / `unknown`）、`session_saved`；并行池还会返回 `workers` / `busy` / `idle` / `queued` / `workers_detail`。

---

## Python 调用示例

```python
import requests

BASE = "http://127.0.0.1:8765/api/deepseek"

# 新对话
r1 = requests.post(
    f"{BASE}/chat",
    json={
        "question": "用一句话解释什么是 REST",
        "mode": "instant",
        "deep_thinking": False,
        "search": False,
    },
    timeout=300,
).json()
assert r1["ok"]
cid = r1["conversation_id"]
print(r1["answer"])

# 多轮
r2 = requests.post(
    f"{BASE}/chat",
    json={
        "conversation_id": cid,
        "question": "再给一个简单例子",
    },
    timeout=300,
).json()
print(r2["answer"])
```

---

## 相关环境变量（`.env`）

| 变量 | 说明 |
| --- | --- |
| `HOST` / `PORT` | 服务监听，默认 `0.0.0.0:8765` |
| `CHROME_PATH` | Chrome 可执行文件路径 |
| `BROWSER_CHANNEL` | 默认 `chrome` |
| `HEADLESS` | `1` 无头 / `0` 有界面 |
| `SQLITE_PATH` | 默认 `data/data.db` |
| `DEEPSEEK_ENABLED` | 总开关，默认 `1`（开启）；设为 `0` 可关闭 |
| `DEEPSEEK_USERNAME` / `DEEPSEEK_PASSWORD` | 网页登录账号 |
| `DEEPSEEK_AUTO_LOGIN` | 是否自动填表登录 |
| `DEEPSEEK_TIMEOUT_MS` | Playwright 页面操作超时（登录/点击等），默认 120000 |
| `DEEPSEEK_CHAT_TIMEOUT_S` | 普通聊天等待回复秒数，默认 300 |
| `DEEPSEEK_THINK_TIMEOUT_S` | 专家模式或深度思考等待回复秒数，默认 600 |
| `DEEPSEEK_WORKERS` | 并行浏览器 worker 数（1–4），默认 2；每个 worker 独立页面，多路 `/chat` 可同时进行 |
| `LOG_DIR` | 日志目录，默认 `logs` |
