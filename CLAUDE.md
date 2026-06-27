# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

A long-running Feishu chatbot that listens for messages (via `lark-cli event consume`), generates replies using AWS Bedrock Claude (Converse API with tool use), and can query calendar freebusy or summarize group chat messages. Runs as a single Python process on GitHub Actions (restarts every 5 hours via cron).

## Architecture

```
Feishu message → lark-cli event consume (NDJSON stdout) → assistant.py → Bedrock Claude → lark-cli im +messages-reply → Feishu
```

The process is event-driven: `lark-cli event consume im.message.receive_v1` streams events as NDJSON lines. The script reads stdout line-by-line, parses each event, generates a reply (potentially with multi-round tool use), and sends it back.

## Running

```bash
cd EthanAssistant/

# Requires all env vars (see .github/workflows/assistant.yml for the full list)
python3 assistant.py
```

No pip dependencies — uses only stdlib (`urllib.request` for Bedrock HTTP calls, `subprocess` for lark-cli). Requires `lark-cli` configured with bot credentials.

## Key Design Decisions

- **Tool Use loop**: Claude decides when to call tools (up to 5 rounds). Tools: `query_freebusy` (calendar), `search_chat_messages` (group chat history)
- **PM guidance tool**: `get_pm_guidance` exposes the local `pm_skills/` library for product/project management questions. Python matches skill metadata locally and returns only the top skill excerpt plus candidate metadata, keeping token use bounded.
- **Person merging**: Aaron = Aaron + Jackson Li accounts; Thomas = Thomas Chang + Deric Chan accounts. Freebusy queries both and merges overlapping slots
- **Group chat filter**: only responds when `@bd agent` appears in content (case-insensitive)
- **Relay mechanism**: if Claude's reply contains `[RELAY]`, the system forwards a summarized message to `RELAY_CHAT_ID` (Ethan Assistant Group) and strips the marker before sending the visible reply
- **Conversation history**: per-conversation (by `sender_id` for p2p, `chat_id` for group), max 10 turns
- **PM session memory**: `pm_sessions` remembers the last matched PM skill for 30 minutes per conversation so short follow-ups like "continue" or "option 2" can reuse the same workflow.
- **Token usage logging**: each replied event writes one aggregated row to a private Lark Bitable ("BD Agent Token 用量记录" under the "PMO 工作desk" wiki). The bot has `edit` permission via its `appid`. Nothing is persisted to the repo, so no user names or questions leak into public git history.
- **Markdown stripping**: all replies go through `strip_markdown()` since Feishu text messages render symbols literally
- **Deduplication**: tracks processed `event_id`s in memory (set, capped at 1000)

## PM Skills

`pm_skills/` contains 49 upstream Product Manager Skills plus one local `project-management-general` skill for execution management. Upstream skills are from `deanpeters/Product-Manager-Skills` under CC BY-NC-SA 4.0; attribution is stored in `pm_skills/LICENSE`.

Runtime behavior:

- Startup scans only `pm_skills/*/SKILL.md` frontmatter metadata.
- `get_pm_guidance(query)` uses alias-weighted local matching over name, description, intent, best_for, scenarios, and type.
- The tool returns one bounded excerpt: frontmatter plus Purpose, Key Concepts, and Application sections, capped at about 2500 characters.
- The assistant should call this tool for PRD, roadmap, prioritization, user story, epic, problem framing, JTBD, milestone, risk, dependency, blocker, owner, and project follow-up questions.

Config:

- `PM_SKILLS_ENABLED` defaults to `true`; set `false` to disable.
- `PM_SKILLS_DIR` defaults to `pm_skills`, relative to this directory.
- `TOKEN_USAGE_ENABLED` defaults to `true`; set `false` to stop writing usage rows.
- `TOKEN_USAGE_BASE_TOKEN` / `TOKEN_USAGE_TABLE_ID` override the target Lark Bitable (defaults point to the "BD Agent Token 用量记录" table).

## Token Usage

`record_token_usage()` writes one row to a private Lark Bitable after a reply is sent (via `lark-cli base +record-batch-create --as bot`). A row represents one incoming Feishu event and aggregates all Bedrock Converse calls for that event, including multi-round tool use and relay summarization. Columns: 时间, 用户, 模型, 输入Token, 输出Token, 总Token, 调用工具, 问题, 会话类型.

Privacy: the `问题` (question) is truncated to `TOKEN_USAGE_QUESTION_LIMIT` (200) chars before storage, and the CI log prints only aggregate numbers (never the question text), since the repo is public. The bot writes with `edit` permission granted to its `appid` on that Bitable. A write failure only logs a warning and never blocks the reply.

Smoke test:

```bash
python3 -m py_compile assistant.py
python3 - <<'PY'
import assistant
print(assistant.execute_get_pm_guidance("帮我写 PRD", "smoke")[:500])
print(assistant.execute_get_pm_guidance("项目延期了，怎么跟进风险和里程碑", "smoke")[:500])
PY
```

## CI Deployment

GitHub Actions workflow restarts the process every 5 hours (cron: `0 1,6,11,16,21 * * *`). Uses `concurrency: cancel-in-progress: true` to ensure only one instance runs. Job timeout is 350 minutes (~5.8 hours).

## Bedrock Auth

Uses Bearer token auth (`AWS_BEARER_TOKEN_BEDROCK`) directly against `bedrock-runtime.{region}.amazonaws.com/model/{model}/converse`. No AWS SDK dependency.

## System Prompt

Lives in `system_prompt.txt`. Defines persona, reply style (no emoji, no markdown, concise), tool usage rules, and a strict relay-trigger policy (only explicit "please tell Ethan" phrasing).
