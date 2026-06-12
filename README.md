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
- **产品/项目管理指导**：通过本地 PM skill 库回答 PRD、roadmap、优先级、用户故事、epic、问题定义、项目风险、里程碑、进度等问题
- **消息转达**：AI 判断需要转达时，自动通知 Ethan（发送到 Ethan Assistant Group）

## PM 技能库

`assistant.py` 提供 `get_pm_guidance` 工具。Claude 判断用户在问产品管理或项目管理问题时，会调用该工具；Python 端从 `pm_skills/` 本地匹配最相关的 `SKILL.md`，返回一个主要 skill 片段和最多 3 个候选 skill 元数据。

设计要点：

- 启动时只扫描 skill frontmatter 元数据，不把 49 个 skill 全部放进 prompt
- 每次工具调用只返回一个主要 skill 的有限片段，控制 token 消耗
- 支持中文 alias 匹配，如"需求文档/PRD""排期/roadmap""优先级/取舍""项目延期/风险/里程碑"
- 使用 `pm_sessions` 在同一会话内记住最近一次 PM skill，用户回复"继续""选 2""展开"时可延续上下文
- 项目管理通用能力由本地 `pm_skills/project-management-general/SKILL.md` 补充

技能来源：

- 产品管理 skills 来自 `deanpeters/Product-Manager-Skills`
- 上游许可为 CC BY-NC-SA 4.0，attribution 见 `pm_skills/LICENSE`

### PM 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PM_SKILLS_ENABLED` | `true` | 设为 `false` 可禁用 PM skill 工具 |
| `PM_SKILLS_DIR` | `pm_skills` | skill 目录，相对 `EthanAssistant/` |

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
| `PM_SKILLS_ENABLED` | 可选，是否启用 PM skill 库 |
| `PM_SKILLS_DIR` | 可选，PM skill 目录 |
| `TOKEN_USAGE_ENABLED` | 可选，是否写入 token_usage.jsonl，默认 true |

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

## Token 用量记录

每条已回复的飞书消息会追加一行 JSON 到 `token_usage.jsonl`。记录按用户消息聚合本轮所有 Bedrock 调用，包括 tool-use 多轮调用；如果触发转达，转达摘要的 Bedrock token 也会合并到同一行。

示例：

```json
{"ts":"2026-06-13 14:30:05","user":"Pan Haifeng","model":"claude-sonnet-4","input_tokens":1234,"output_tokens":456,"total_tokens":1690,"tools":["query_freebusy"],"question":"Thomas 和 Aaron 周五有空吗","chat_type":"p2p"}
```

GitHub Actions 在 `Run Ethan Assistant` 结束后会执行 `Persist token usage`，把 `token_usage.jsonl` commit 并 push 回 `main`。如需禁用本地记录，可设置 `TOKEN_USAGE_ENABLED=false`。

## 测试

```bash
cd EthanAssistant/
python3 -m py_compile assistant.py
python3 - <<'PY'
import assistant
for q in ["帮我写 PRD", "12 个需求只能做一个 sprint，怎么排优先级", "项目延期了，怎么跟进风险和里程碑"]:
    print(q)
    print(assistant.execute_get_pm_guidance(q, conv_key="smoke")[:500])
    print("---")
PY
```

## 项目结构

```
EthanAssistant/
├── .github/workflows/assistant.yml  # GitHub Actions 部署
├── assistant.py                     # 主服务脚本
├── system_prompt.txt                # AI 系统提示词
├── pm_skills/                       # 产品/项目管理 skill 库
├── requirements.txt                 # 依赖（纯 stdlib）
└── README.md
```
