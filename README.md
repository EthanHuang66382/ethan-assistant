# Ethan Assistant

飞书对话 AI 助理 — 自动接收消息并用 Claude 回复。

## 架构

```
飞书用户 → 消息 → lark-cli event consume → assistant.py → AWS Bedrock Claude → lark-cli im +messages-reply → 飞书用户
```

## 部署 (GitHub Actions)

### 所需 Secrets

在 GitHub repo Settings → Secrets and variables → Actions 中设置：

| Secret | 说明 |
|--------|------|
| `LARK_APP_ID` | 飞书应用 App ID |
| `LARK_APP_SECRET` | 飞书应用 App Secret |
| `AWS_ACCESS_KEY_ID` | AWS IAM Access Key |
| `AWS_SECRET_ACCESS_KEY` | AWS IAM Secret Key |
| `AWS_REGION` | AWS Region (如 `us-east-1`) |

### 可选 Variables

| Variable | 说明 | 默认值 |
|----------|------|--------|
| `BEDROCK_MODEL_ID` | Bedrock 模型 ID | `anthropic.claude-sonnet-4-20250514` |
| `BOT_OPEN_ID` | Bot 的 open_id（防止回复自己） | 空 |

### 飞书应用权限

确保应用已开启以下权限：
- `im:message:send_as_bot` — 以 bot 身份发送消息
- `im:message` — 消息读取
- 事件订阅：`im.message.receive_v1`

### 启动

- 自动：cron 每 5 小时触发一次，concurrency 保证只有一个实例
- 手动：Actions → Ethan Assistant → Run workflow

## 本地运行

```bash
export AWS_ACCESS_KEY_ID=xxx
export AWS_SECRET_ACCESS_KEY=xxx
export AWS_REGION=us-east-1
python3 assistant.py
```

## 自定义

编辑 `system_prompt.txt` 调整 Assistant 的回复风格和能力范围。
