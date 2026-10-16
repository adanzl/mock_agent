# 豆包网页模拟 API

通过浏览器操作 [www.doubao.com/chat](https://www.doubao.com/chat/)，对外提供 HTTP 接口。

默认地址：`http://127.0.0.1:8765`  
前缀：`/api/doubao`

**默认关闭**：需在 `.env` 设置 `DOUBAO_ENABLED=1` 后才会启动浏览器、接受 chat。未开启时 `/api/doubao/chat` 返回 503。

## 启动与扫码

```bash
conda activate flask_env
cd backend
python main.py
```

服务启动后会在后台打开豆包。已有登录态时直接进入对话，不打二维码。

没有登录态时，会截下页面上的码，用 `zxing-cpp` 读出内容，再用 `qrcode` 按最低纠错生成矩阵，以盲文打进日志（约 31 列、16 行）。用豆包或飞书 App 扫**最下面一块**。码刷新会再打一块。`DOUBAO_LOGIN_TIMEOUT_S`（默认 180 秒）内未扫完，只关闭豆包浏览器，其他提供方继续跑。本次进程不再重复弹码，要再扫就重启服务。

登录成功后会留下两份会话，减少之后的扫码：

- Chrome profile：`backend/data/doubao-browser`（可用 `DOUBAO_USER_DATA_DIR` 改）
- SQLite `browser_session`，`provider=doubao`，profile 失效时用来回灌 cookie

这两处都在 `backend/data/` 下，不要提交到 git。超时关闭浏览器时不会删 profile，也不会清掉已经存过的会话。

看日志时用终端 `tail -f`，颜色码才会变成可扫的黑白块。

## 快速开始

```bash
curl -s http://127.0.0.1:8765/api/doubao/chat \
  -H "Content-Type: application/json" \
  -d '{"question":"你好，介绍一下你自己"}'
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
  "url": "https://www.doubao.com/chat/xxxxxxxx"
}
```

多轮续聊带上 `conversation_id`：

```bash
curl -s http://127.0.0.1:8765/api/doubao/chat \
  -H "Content-Type: application/json" \
  -d '{"conversation_id":"上一轮返回的id","question":"再说详细一点"}'
```

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 进程健康检查（根路径也有 `/health`） |
| GET | `/api/doubao/health` | 豆包模块健康检查 |
| GET | `/api/doubao/doc` | 返回本文档 Markdown 原文 |
| GET | `/api/doubao/status` | 登录态 / 浏览器状态 |
| POST | `/api/doubao/chat` | 提问（同步） |
| POST | `/api/doubao/chat/async` | 异步提问（立即返回 `job_id`） |
| GET | `/api/doubao/chat/jobs/<id>` | 查询异步任务 |
| GET | `/api/doubao/conversations` | 本地会话列表 |
| GET | `/api/doubao/conversations/<id>` | 会话详情 + 消息历史 |

## POST `/api/doubao/chat`

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `question` | string | 是 | — | 用户问题（别名：`prompt` / `message`） |
| `conversation_id` | string | 否 | 无 | 有则续聊，无则新开（别名：`chat_id`） |
| `mode` | string | 否 | `auto` | `think` / `thinking` / `expert` / `思考` 会打开深度思考 |
| `deep_thinking` | bool | 否 | `false` | 深度思考（别名：`think` / `deep_think`） |
| `search` | bool | 否 | `false` | 豆包页面没有单独的联网开关，搜索并进深度思考。传 `true` 只记在会话上，不额外点按钮 |
| `timeout` | int | 否 | 见下 | 等待回复秒数。普通对话用 `DOUBAO_CHAT_TIMEOUT_S`（默认 300），深度思考用 `DOUBAO_THINK_TIMEOUT_S`（默认 600） |

图片上传这一版会返回 400。

### 错误码

| HTTP | 含义 |
| --- | --- |
| 400 | 参数错误 |
| 401 | 未登录，或本次启动扫码已超时 |
| 503 | `DOUBAO_ENABLED=0` |
| 504 | 等待回复或启动扫码超时 |
| 500 | 其它异常 |

## 异步 Chat

`POST /api/doubao/chat/async` 的请求体与同步接口相同，立即返回 `job_id`。再用 `GET /api/doubao/chat/jobs/<job_id>` 取结果。

## 环境变量

```bash
DOUBAO_ENABLED=0
DOUBAO_TIMEOUT_MS=120000
DOUBAO_CHAT_TIMEOUT_S=300
DOUBAO_THINK_TIMEOUT_S=600
DOUBAO_LOGIN_TIMEOUT_S=180
# DOUBAO_USER_DATA_DIR=data/doubao-browser
```

浏览器通道沿用 `HEADLESS`、`BROWSER_CHANNEL`、`CHROME_PATH`。豆包固定一个 profile，不能并行开多个窗口。
