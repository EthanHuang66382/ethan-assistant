# Ethan Assistant

飞书对话 AI 助理 — 自动接收消息并用 Claude 回复，支持日历查询和消息转达。

## 架构

```
飞书用户 → 消息 → lark-cli event consume → assistant.py → AWS Bedrock Claude → lark-cli im +messages-reply → 飞书用户
```

## 功能

- **AI 对话**：监听所有飞书消息（私聊+群聊），用 Claude 生成回复
- **日历查询**：查询 Ethan / Aaron 的忙闲状态（支持今天、明天、本周、下周、未来N天，最多一个月）
- **上下文记忆**：按会话维护最近 10 轮对话历史
- **消息转达**：AI 判断需要转达时，自动通知 Ethan（发送到 Ethan Assistant Group）

## 部署 (GitHub Actions)

通过 cron 每 5 小时触发，`concurrency: cancel-in-progress` 保证只有一个实例运行。

### 所需 Secrets

| Secret | 说明 |
|--------|------|
| `LARK_APP_ID` | 飞书应用 App ID |
| `LARK_APP_SECRET` | 飞书应用 App Secret |
| `AWS_BEARER_TOKEN_BEDROCK` | AWS Bedrock Bearer Token |
| `BOT_OPEN_ID` | Bot 的 open_id（防止回复自己） |
| `OPEN_ID_ETHAN` | Ethan 的 open_id |
| `OPEN_ID_AARON` | Aaron 的 open_id |
| `OPEN_ID_JACKSON` | Jackson Li 的 open_id（Aaron 第二账号） |
| `OPEN_ID_ALVIN` | Alvin Xiao 的 open_id |
| `OPEN_ID_THOMAS` | Thomas Chang 的 open_id |
| `OPEN_ID_DERIC` | Deric Chan 的 open_id（Thomas 第二账号） |
| `RELAY_CHAT_ID` | 转达通知发送的群 chat_id |

### 飞书应用权限

- `im:message:send_as_bot` — 以 bot 身份发送消息
- `im:message` — 消息读取
- `calendar:calendar.free_busy:read` — 查询日历忙闲
- `contact:user.base:readonly` — 查询用户信息
- 事件订阅：`im.message.receive_v1`

### 启动

- 自动：cron 每 5 小时触发
- 手动：Actions → Ethan Assistant → Run workflow

## 自定义

编辑 `system_prompt.txt` 调整 Assistant 的回复风格和能力范围。

## 项目结构

```
EthanAssistant/
├── .github/workflows/assistant.yml  # GitHub Actions 部署
├── assistant.py                     # 主服务脚本
├── system_prompt.txt                # AI 系统提示词
├── requirements.txt                 # 依赖（纯 stdlib）
└── README.md
```
