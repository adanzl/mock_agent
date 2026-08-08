# ChatGPT 网页模拟 API

通过无头浏览器操作 [chatgpt.com](https://chatgpt.com)，经本地 HTTP 代理访问，对外提供 HTTP 接口。

默认地址：`http://127.0.0.1:8765`  
前缀：`/api/chatgpt`

浏览器流量默认走 `http://127.0.0.1:7890`（可用 `CHATGPT_PROXY` 覆盖）。DeepSeek 模块不受影响。

**默认关闭**：需在 `.env` 设置 `CHATGPT_ENABLED=1` 后才会启动 / 接受 chat；未开启时 `/api/chatgpt/chat` 返回 503。

## 启动

```bash
conda activate flask_env
cd backend
python main.py
```

或在 Cursor 使用 launch 配置：`Python: Flask`。

启动前在项目根目录 `.env` 配置账号、代理与 Chrome 路径（见文末）。

服务启动时会打开**有界面**浏览器（默认 `CHATGPT_MANUAL_LOGIN=1`）：由你在窗口里手动完成登录/真人验证；成功后会话写入持久化目录。会话失效时，`/chat` 会再次打开窗口等你登录。不提供独立 login 接口。

若设置 `CHATGPT_MANUAL_LOGIN=0` 且配置了账号密码，才会尝试自动填表登录（仍可能被 Cloudflare 拦截，不推荐）。

---

## 快速开始（多轮对话）

### 1. 新开对话

不传 `conversation_id` 即新建会话。

```bash
curl -s http://127.0.0.1:8765/api/chatgpt/chat ^
  -H "Content-Type: application/json" ^
  -d "{\"question\":\"你好，介绍一下你自己\",\"mode\":\"auto\"}"
```

响应示例：

```json
{
  "ok": true,
  "question": "你好，介绍一下你自己",
  "answer": "...",
  "conversation_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "mode": "auto",
  "deep_thinking": false,
  "search": false,
  "url": "https://chatgpt.com/c/..."
}
```

请保存返回的 `conversation_id`，后续多轮必须带上。

### 2. 多轮续聊

```bash
curl -s http://127.0.0.1:8765/api/chatgpt/chat ^
  -H "Content-Type: application/json" ^
  -d "{\"conversation_id\":\"上一轮返回的id\",\"question\":\"再说详细一点\"}"
```

---

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 进程健康检查（根路径也有 `/health`） |
| GET | `/api/chatgpt/health` | ChatGPT 模块健康检查 |
| GET | `/api/chatgpt/status` | 登录态 / 浏览器 / 代理状态 |
| POST | `/api/chatgpt/chat` | 提问（新对话或按 `conversation_id` 续聊） |
| GET | `/api/chatgpt/conversations` | 本地会话列表 |
| GET | `/api/chatgpt/conversations/<id>` | 会话详情 + 消息历史 |

---

## POST `/api/chatgpt/chat`

### 请求体

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `question` | string | 是 | — | 用户问题（别名：`prompt` / `message`） |
| `conversation_id` | string | 否 | 无 | **有则续聊，无则新开**（别名：`chat_id`） |
| `mode` | string | 否 | `auto` | 模型选择（别名：`model`） |
| `deep_thinking` | bool | 否 | `false` | 尝试开启思考类能力（UI 无对应控件则忽略） |
| `search` | bool | 否 | `false` | 尝试开启 Search / 联网（UI 无对应控件则忽略） |
| `timeout` | int | 否 | `CHATGPT_CHAT_TIMEOUT_S`(默认 300) | 等待回复秒数 |

### `mode` 取值

| 值 | 含义 |
| --- | --- |
| `auto` / `default` | 不主动切换模型，沿用网页当前选择 |
| `gpt-4o` / `4o` | 尝试选择 GPT-4o |
| `gpt-4.1` / `4.1` | 尝试选择 GPT-4.1 |
| `gpt-5` | 尝试选择 GPT-5 |
| `o3` / `o4-mini` | 尝试选择对应模型 |
| 其他短字符串 | 按 UI 菜单文案做包含匹配 |

说明：

- **模型只在新对话时尝试切换**；带 `conversation_id` 续聊时不切换 `mode`。
- ChatGPT 网页模型列表会变；若选不到对应项，会打日志并继续用当前模型。

### 成功响应

```json
{
  "ok": true,
  "question": "...",
  "answer": "...",
  "conversation_id": "uuid",
  "mode": "auto",
  "deep_thinking": false,
  "search": false,
  "url": "https://chatgpt.com/c/uuid"
}
```

### 错误码

| HTTP | 含义 |
| --- | --- |
| 400 | 参数错误（缺 question 等） |
| 401 | 未登录 / 运行时失败 |
| 504 | 等待回复超时 |
| 500 | 其他异常 |

---

## 带选项示例

指定模型 + 联网搜索：

```json
{
  "question": "今天有哪些 AI 新闻？",
  "mode": "gpt-4o",
  "search": true,
  "timeout": 180
}
```

续聊：

```json
{
  "conversation_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "question": "把要点列成三条"
}
```

---

## 会话查询

### 列表

`GET /api/chatgpt/conversations?limit=50`

```json
{
  "ok": true,
  "items": [
    {
      "id": "uuid",
      "title": "你好，介绍一下你自己",
      "mode": "auto",
      "deep_thinking": 0,
      "search": 0,
      "url": "...",
      "updated_at": "..."
    }
  ]
}
```

### 详情

`GET /api/chatgpt/conversations/<conversation_id>`

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

本地库路径由 `SQLITE_PATH` 决定，默认 `backend/data/data.db`。会话 `provider` 为 `chatgpt`，与 DeepSeek 隔离。

---

## 登录与状态

默认 **手动登录**（`CHATGPT_MANUAL_LOGIN=1`）。

**推荐：连接本机真实 Chrome（CDP）**，避免 Playwright 自带浏览器被 Cloudflare 永久卡在「请稍候」：

1. 先关掉所有 Chrome，再启动调试 Chrome（系统 Chrome，不要用 `chrome-win64` 便携包）：

```bat
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="%TEMP%\chrome-chatgpt-debug"
```

2. 在该窗口打开 chatgpt.com，**手动登录成功**（代理用你平时能上的方式）。

3. `.env` 设置：

```
CHATGPT_CDP_URL=http://127.0.0.1:9222
CHATGPT_MANUAL_LOGIN=1
```

4. 再启动本服务；服务会附着到这个 Chrome，检测到聊天页即可用 `/api/chatgpt/chat`。

未配置 `CHATGPT_CDP_URL` 时，仍会用 Playwright 拉起有界面浏览器（容易被 Cloudflare 拦，不推荐作为登录手段）。

查看状态：`GET /api/chatgpt/status`（`cdp_url` / `manual_login` / `ready`）。

---

## Python 调用示例

```python
import requests

BASE = "http://127.0.0.1:8765/api/chatgpt"

# 新对话
r1 = requests.post(
    f"{BASE}/chat",
    json={
        "question": "用一句话解释什么是 REST",
        "mode": "auto",
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
| `CHATGPT_ENABLED` | 总开关，默认 `0`（关闭）；设为 `1` 才启用 GPT |
| `CHATGPT_PROXY` | Playwright 代理，默认 `http://127.0.0.1:7890` |
| `CHATGPT_MANUAL_LOGIN` | `1`（默认）手动登录，不自动填密码 |
| `CHATGPT_CDP_URL` | 连接本机 Chrome/Edge，如 `http://127.0.0.1:9222`（推荐） |
| `CHATGPT_AUTO_LOGIN` | 仅当 `MANUAL_LOGIN=0` 时尝试密码自动填表，默认 `0` |
| `CHATGPT_USERNAME` / `CHATGPT_PASSWORD` | 自动填表用（手动模式可不配） |
| `CHATGPT_TIMEOUT_MS` | Playwright 页面操作超时，默认 120000 |
| `CHATGPT_CHAT_TIMEOUT_S` | 聊天等待回复秒数，默认 300 |
| `CHATGPT_CAPTCHA_TIMEOUT_S` | 等待手动登录/真人验证的秒数，默认 600 |
| `CHATGPT_USER_DATA_DIR` | 持久化浏览器配置目录，默认 `data/chatgpt-browser` |
| `LOG_DIR` | 日志目录，默认 `logs` |
