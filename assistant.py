#!/usr/bin/env python3
"""Ethan Assistant — 飞书对话服务 (Tool Use 模式)

监听飞书消息，用 AWS Bedrock Claude 生成回复。
Claude 自主决定何时调用工具（日历查询、群聊摘要）。
"""

import json
import os
import re
import subprocess
import sys
import signal
import time
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
SYSTEM_PROMPT_FILE = SCRIPT_DIR / "system_prompt.txt"

LARK_CLI = os.environ.get("LARK_CLI", "lark-cli")
BOT_OPEN_ID = os.environ.get("BOT_OPEN_ID", "")

# AWS Bedrock 配置
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0")
AWS_BEARER_TOKEN = os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "")

# Token 用量记录：写入 Lark 多维表格（私有 wiki，不再落地 public repo）
# 表位于 "PMO 工作desk" 下的 "BD Agent Token 用量记录"
TOKEN_USAGE_ENABLED = os.environ.get("TOKEN_USAGE_ENABLED", "true").lower() not in ("0", "false", "no", "off")
TOKEN_USAGE_BASE_TOKEN = os.environ.get("TOKEN_USAGE_BASE_TOKEN", "H1Dpb7wrZabp9XsWiMglgZdvgWf")
TOKEN_USAGE_TABLE_ID = os.environ.get("TOKEN_USAGE_TABLE_ID", "tblcC3rCjJIuMt3V")
# 写入表的 question/answer 截断长度，避免完整业务内容留存
TOKEN_USAGE_QUESTION_LIMIT = 200
TOKEN_USAGE_ANSWER_LIMIT = 500

# 用户 ID 映射（从环境变量读取）
USERS = {
    "ethan": {"name": "Ethan Huang", "open_id": os.environ.get("OPEN_ID_ETHAN", "")},
    "aaron": {"name": "Aaron", "open_id": os.environ.get("OPEN_ID_AARON", "")},
    "jackson": {"name": "Jackson Li", "open_id": os.environ.get("OPEN_ID_JACKSON", "")},
    "alvin": {"name": "Alvin Xiao", "open_id": os.environ.get("OPEN_ID_ALVIN", "")},
    "thomas": {"name": "Thomas Chang", "open_id": os.environ.get("OPEN_ID_THOMAS", "")},
    "deric": {"name": "Deric Chan", "open_id": os.environ.get("OPEN_ID_DERIC", "")},
}

# 对话历史：按 chat_id（群聊）或 sender_id（私聊）维护上下文
MAX_HISTORY = 10
MAX_CONVERSATIONS = 200
conversation_history: dict[str, list] = defaultdict(list)

# PM/Product skill library
PM_SKILLS_ENABLED = os.environ.get("PM_SKILLS_ENABLED", "true").lower() not in ("0", "false", "no", "off")
PM_SKILLS_DIR_RAW = os.environ.get("PM_SKILLS_DIR", "pm_skills")
PM_SKILLS_DIR = Path(PM_SKILLS_DIR_RAW)
if not PM_SKILLS_DIR.is_absolute():
    PM_SKILLS_DIR = SCRIPT_DIR / PM_SKILLS_DIR
PM_SKILL_CONTENT_LIMIT = 2500
PM_SESSION_TTL_SECONDS = 30 * 60
pm_sessions: dict[str, dict] = {}

UTC8 = timezone(timedelta(hours=8))


def now_utc8() -> datetime:
    return datetime.now(UTC8)


def log(msg: str):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def get_system_prompt() -> str:
    if SYSTEM_PROMPT_FILE.exists():
        return SYSTEM_PROMPT_FILE.read_text().strip()
    return "你是 Ethan Huang 的 AI 助理。请用专业友善的语气回复消息。如果不确定如何回答，可以告知对方你会转达给 Ethan。回复请简洁明了。"


def new_token_usage() -> dict:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "tools": [],
    }


def add_token_usage(summary: dict, result: dict):
    usage = result.get("usage", {}) or {}
    input_tokens = int(usage.get("inputTokens", 0) or 0)
    output_tokens = int(usage.get("outputTokens", 0) or 0)
    total_tokens = int(usage.get("totalTokens", 0) or 0) or input_tokens + output_tokens
    summary["input_tokens"] += input_tokens
    summary["output_tokens"] += output_tokens
    summary["total_tokens"] += total_tokens


def merge_token_usage(target: dict, source: dict):
    target["input_tokens"] += int(source.get("input_tokens", 0) or 0)
    target["output_tokens"] += int(source.get("output_tokens", 0) or 0)
    target["total_tokens"] += int(source.get("total_tokens", 0) or 0)
    for tool in source.get("tools", []):
        if tool not in target["tools"]:
            target["tools"].append(tool)


def add_usage_tool(summary: dict, tool_name: str):
    if tool_name and tool_name not in summary["tools"]:
        summary["tools"].append(tool_name)


def model_label() -> str:
    if "claude-sonnet-4" in BEDROCK_MODEL_ID:
        return "claude-sonnet-4"
    return BEDROCK_MODEL_ID


def _truncate(text: str, limit: int) -> str:
    """单行化并截断文本，超长加省略号。"""
    t = (text or "").strip().replace("\n", " ")
    if len(t) > limit:
        return t[:limit] + "…"
    return t


def record_token_usage(user_name: str, chat_type: str, question: str, answer: str, usage: dict):
    """把单条消息的 token 用量写入 Lark 多维表格。

    写入失败只记日志，绝不影响 bot 回复。出于隐私，question/answer 截断后存表，
    CI 日志只打印聚合数字（不含原文）。
    """
    if not TOKEN_USAGE_ENABLED or not usage.get("total_tokens"):
        return
    if not TOKEN_USAGE_BASE_TOKEN or not TOKEN_USAGE_TABLE_ID:
        log("WARN: token usage table not configured, skipping record")
        return

    question_trunc = _truncate(question, TOKEN_USAGE_QUESTION_LIMIT)
    answer_trunc = _truncate(answer, TOKEN_USAGE_ANSWER_LIMIT)

    row = [
        now_utc8().strftime("%Y-%m-%d %H:%M:%S"),
        user_name or "",
        model_label(),
        int(usage.get("input_tokens", 0) or 0),
        int(usage.get("output_tokens", 0) or 0),
        int(usage.get("total_tokens", 0) or 0),
        ",".join(usage.get("tools", [])) or "none",
        question_trunc,
        answer_trunc,
        chat_type or "",
    ]
    payload = json.dumps({
        "fields": ["时间", "用户", "模型", "输入Token", "输出Token", "总Token", "调用工具", "问题", "回答", "会话类型"],
        "rows": [row],
    }, ensure_ascii=False)

    try:
        result = subprocess.run(
            [LARK_CLI, "base", "+record-batch-create",
             "--base-token", TOKEN_USAGE_BASE_TOKEN,
             "--table-id", TOKEN_USAGE_TABLE_ID,
             "--json", payload,
             "--as", "bot"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            log(f"ERROR: token usage write failed: {result.stderr[:200]}")
        else:
            # 日志只打印数字，不含 question 原文（CI 日志是 public）
            log(f"TOKEN_USAGE: user={user_name} total={row[5]} (in={row[3]}/out={row[4]}) tools={row[6]}")
    except Exception as e:
        log(f"ERROR: record_token_usage exception: {e}")


# =============================================================================
# PM Skill Library
# =============================================================================

PM_SKILL_ALIASES = {
    "prd-development": [
        "prd", "product requirements document", "需求文档", "产品需求文档", "产品方案",
        "需求说明", "产品说明书", "写需求", "写prd",
    ],
    "roadmap-planning": [
        "roadmap", "路线图", "产品路线图", "排期", "规划路线", "季度规划",
        "半年规划", "q1", "q2", "q3", "q4",
    ],
    "prioritization-advisor": [
        "优先级", "排序", "取舍", "排优先级", "需求排序", "rice", "ice",
        "value effort", "价值 effort", "只能做", "一个sprint",
    ],
    "user-story": [
        "user story", "用户故事", "用户需求", "验收标准", "acceptance criteria",
    ],
    "user-story-splitting": [
        "拆故事", "拆需求", "故事拆分", "切需求", "需求拆分", "story splitting",
    ],
    "epic-breakdown-advisor": [
        "epic", "史诗", "拆epic", "epic拆解", "大需求拆解", "initiative拆解",
    ],
    "problem-statement": [
        "problem statement", "问题陈述", "定义问题", "问题定义", "问题描述",
    ],
    "problem-framing-canvas": [
        "问题框定", "框定问题", "problem framing", "问题画布",
    ],
    "jobs-to-be-done": [
        "jtbd", "jobs to be done", "用户任务", "用户动机", "job story",
    ],
    "opportunity-solution-tree": [
        "ost", "opportunity solution tree", "机会树", "机会方案树", "机会地图",
    ],
    "product-strategy-session": [
        "产品策略", "strategy", "战略", "产品方向", "战略会",
    ],
    "project-management-general": [
        "项目管理", "项目进度", "进度", "里程碑", "延期", "风险", "依赖",
        "阻塞", "blocker", "milestone", "risk", "dependency", "owner",
        "负责人", "跟进", "项目复盘", "项目延期",
    ],
}


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _extract_frontmatter(text: str) -> tuple[dict, str]:
    """Parse the simple YAML frontmatter shape used by PM skills."""
    if not text.startswith("---"):
        return {}, text

    lines = text.splitlines()
    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        return {}, text

    metadata: dict[str, str | list[str]] = {}
    current_key = ""
    for raw_line in lines[1:end_idx]:
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        if not raw_line.startswith(" ") and ":" in line:
            key, value = line.split(":", 1)
            current_key = key.strip()
            value = value.strip()
            if value in (">", ">-", "|", "|-"):
                metadata[current_key] = ""
            elif value:
                metadata[current_key] = _strip_quotes(value)
            else:
                metadata[current_key] = []
            continue

        if stripped.startswith("- ") and current_key:
            existing = metadata.get(current_key)
            if not isinstance(existing, list):
                existing = []
                metadata[current_key] = existing
            existing.append(_strip_quotes(stripped[2:]))
            continue

        if current_key and isinstance(metadata.get(current_key), str):
            metadata[current_key] = (metadata[current_key] + " " + stripped).strip()

    body = "\n".join(lines[end_idx + 1:]).strip()
    return metadata, body


def _metadata_text(value) -> str:
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    return str(value or "")


def _tokenize(text: str) -> set[str]:
    text = text.lower()
    tokens = set(re.findall(r"[a-z0-9][a-z0-9_-]*|[\u4e00-\u9fff]{2,}", text))
    return {token.replace("_", "-") for token in tokens if len(token) > 1}


def load_pm_skills() -> list[dict]:
    if not PM_SKILLS_ENABLED:
        return []
    if not PM_SKILLS_DIR.exists():
        log(f"WARN: PM skills directory not found: {PM_SKILLS_DIR}")
        return []

    skills = []
    for skill_file in sorted(PM_SKILLS_DIR.glob("*/SKILL.md")):
        try:
            raw = skill_file.read_text(encoding="utf-8")
        except Exception as e:
            log(f"WARN: failed to read PM skill {skill_file}: {e}")
            continue

        metadata, _ = _extract_frontmatter(raw)
        name = _metadata_text(metadata.get("name")) or skill_file.parent.name
        description = _metadata_text(metadata.get("description"))
        intent = _metadata_text(metadata.get("intent"))
        best_for = metadata.get("best_for") if isinstance(metadata.get("best_for"), list) else []
        scenarios = metadata.get("scenarios") if isinstance(metadata.get("scenarios"), list) else []
        skill_type = _metadata_text(metadata.get("type")) or "component"
        search_text = " ".join([
            name,
            description,
            intent,
            _metadata_text(best_for),
            _metadata_text(scenarios),
            skill_type,
        ]).lower()
        skills.append({
            "name": name,
            "description": description,
            "intent": intent,
            "best_for": best_for,
            "scenarios": scenarios,
            "type": skill_type,
            "path": skill_file,
            "search_text": search_text,
            "tokens": _tokenize(search_text),
        })
    log(f"Loaded {len(skills)} PM skills from {PM_SKILLS_DIR}")
    return skills


PM_SKILLS = load_pm_skills()


def _is_pm_followup(query: str) -> bool:
    compact = re.sub(r"\s+", "", query.strip().lower())
    if not compact:
        return False
    if re.fullmatch(r"(选)?[0-9一二三四五六七八九十]+", compact):
        return True
    followup_markers = [
        "继续", "展开", "详细", "按这个", "就这个", "用这个", "下一步", "帮我展开",
        "生成", "写出来", "给模板", "继续问", "继续做", "ok", "好的", "可以",
    ]
    return any(marker in compact for marker in followup_markers) and len(compact) <= 20


def _score_pm_skill(skill: dict, query: str, query_tokens: set[str], conv_key: str = "") -> int:
    query_lower = query.lower()
    score = 0

    skill_name = skill["name"]
    if skill_name in query_lower:
        score += 30
    for name_part in skill_name.split("-"):
        if len(name_part) > 2 and name_part in query_lower:
            score += 3

    for alias in PM_SKILL_ALIASES.get(skill_name, []):
        if alias.lower() in query_lower:
            score += 25

    score += len(query_tokens & skill["tokens"]) * 2

    session = pm_sessions.get(conv_key) if conv_key else None
    if session and session.get("skill_name") == skill_name and _is_pm_followup(query):
        score += 40

    return score


def _select_pm_skill(query: str, conv_key: str = "") -> list[tuple[int, dict]]:
    if not PM_SKILLS_ENABLED or not PM_SKILLS:
        return []
    query_tokens = _tokenize(query)
    scored = []
    for skill in PM_SKILLS:
        score = _score_pm_skill(skill, query, query_tokens, conv_key)
        if score > 0:
            scored.append((score, skill))
    scored.sort(key=lambda item: (-item[0], item[1]["name"]))
    return scored[:3]


def _extract_named_sections(body: str, section_names: list[str]) -> str:
    sections = []
    lines = body.splitlines()
    idx = 0
    wanted = {f"## {name}".lower() for name in section_names}
    while idx < len(lines):
        line = lines[idx].strip().lower()
        if line in wanted:
            chunk = [lines[idx]]
            idx += 1
            while idx < len(lines) and not lines[idx].startswith("## "):
                chunk.append(lines[idx])
                idx += 1
            sections.append("\n".join(chunk).strip())
            continue
        idx += 1
    return "\n\n".join(section for section in sections if section)


def _truncate_text(text: str, limit: int = PM_SKILL_CONTENT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    truncated = text[:limit].rsplit("\n", 1)[0].strip()
    return truncated + "\n...[truncated]"


def _read_skill_excerpt(skill: dict) -> str:
    raw = skill["path"].read_text(encoding="utf-8")
    metadata, body = _extract_frontmatter(raw)
    frontmatter = "\n".join([
        f"name: {_metadata_text(metadata.get('name'))}",
        f"description: {_metadata_text(metadata.get('description'))}",
        f"type: {_metadata_text(metadata.get('type'))}",
    ]).strip()
    sections = _extract_named_sections(body, ["Purpose", "Key Concepts", "Application"])
    if not sections:
        sections = body
    return _truncate_text(frontmatter + "\n\n" + sections)


def execute_get_pm_guidance(query: str, conv_key: str = "") -> str:
    if not PM_SKILLS_ENABLED:
        return "PM skill library is disabled by PM_SKILLS_ENABLED=false."

    _cleanup_pm_sessions()
    matches = _select_pm_skill(query, conv_key)
    if not matches:
        return "未找到相关产品/项目管理建议。请让用户更具体描述需求，例如 PRD、roadmap、优先级、用户故事、项目风险、里程碑或进度跟进。"

    primary_score, primary = matches[0]
    try:
        excerpt = _read_skill_excerpt(primary)
    except Exception as e:
        log(f"ERROR: failed to read matched PM skill {primary['name']}: {e}")
        return "读取产品/项目管理 skill 时出错，请稍后再试。"

    if conv_key:
        pm_sessions[conv_key] = {
            "skill_name": primary["name"],
            "updated_at": time.time(),
        }
        _cleanup_pm_sessions()

    candidates = []
    for score, skill in matches:
        candidates.append(
            f"- {skill['name']} ({skill['type']}, score={score}): {skill['description']}"
        )

    return (
        "PM guidance match\n"
        f"Primary skill: {primary['name']}\n"
        f"Type: {primary['type']}\n"
        f"Description: {primary['description']}\n\n"
        "Skill excerpt:\n"
        f"{excerpt}\n\n"
        "Top candidates:\n"
        + "\n".join(candidates)
    )


def _cleanup_pm_sessions():
    now = time.time()
    expired = [
        key for key, session in pm_sessions.items()
        if now - session.get("updated_at", 0) > PM_SESSION_TTL_SECONDS
    ]
    for key in expired:
        del pm_sessions[key]


# =============================================================================
# Tool Definitions (Bedrock Converse format)
# =============================================================================

TOOLS = [
    {
        "toolSpec": {
            "name": "query_freebusy",
            "description": "查询指定人员在某个日期范围内的日历忙碌时段。支持查询的人：ethan, aaron, alvin, thomas。Aaron 的数据会自动合并 Aaron + Jackson Li 两个账号；Thomas 会自动合并 Thomas Chang + Deric Chan 两个账号。可以多次调用查询不同人的日历。",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "person": {
                            "type": "string",
                            "enum": ["ethan", "aaron", "alvin", "thomas"],
                            "description": "要查询的人员标识"
                        },
                        "start_date": {
                            "type": "string",
                            "description": "查询开始日期，格式 YYYY-MM-DD"
                        },
                        "end_date": {
                            "type": "string",
                            "description": "查询结束日期，格式 YYYY-MM-DD"
                        }
                    },
                    "required": ["person", "start_date", "end_date"]
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "search_chat_messages",
            "description": "获取群聊的最近消息，用于生成群聊摘要/总结。可以通过 chat_id 直接获取（当用户说「本群」时使用系统提供的当前 chat_id），也可以通过 chat_name 搜索群聊。需要 bot 已加入该群。",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "chat_id": {
                            "type": "string",
                            "description": "群聊 ID（oc_ 开头）。当用户说「本群」「这个群」时，使用系统提供的当前对话 chat_id"
                        },
                        "chat_name": {
                            "type": "string",
                            "description": "要搜索的群聊名称关键词。当没有 chat_id 时使用"
                        },
                        "limit": {
                            "type": "integer",
                            "description": "获取最近消息的数量，默认 50，最大 100",
                            "default": 50
                        }
                    },
                    "required": []
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "get_pm_guidance",
            "description": "获取产品管理或项目管理的方法论指导。当用户问到 PRD、roadmap、优先级排序、用户故事、epic 拆解、问题定义、项目风险、里程碑、进度、负责人、依赖或跟进机制等问题时使用。",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "用户问题的核心意图关键词。可以包含中文或英文，例如 PRD、需求文档、roadmap、优先级、项目延期、风险、里程碑。"
                        }
                    },
                    "required": ["query"]
                }
            }
        }
    },
]


# =============================================================================
# Tool Implementation Functions
# =============================================================================

def query_freebusy_raw(user_id: str, start_date: str, end_date: str) -> list | None:
    """查询指定用户的忙闲信息，返回时段列表。失败返回 None。"""
    if not user_id:
        log("ERROR: freebusy query skipped — empty user_id")
        return None

    time_min = f"{start_date}T00:00:00+08:00"
    time_max = f"{end_date}T23:59:59+08:00"

    data_payload = json.dumps({
        "user_id": user_id,
        "time_min": time_min,
        "time_max": time_max,
        "include_external_calendar": False,
        "only_busy": True,
    })

    cmd = [LARK_CLI, "calendar", "freebusys", "list",
           "--params", json.dumps({"user_id_type": "open_id"}),
           "--data", data_payload,
           "--as", "bot"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log(f"ERROR: freebusy query failed: {result.stderr}")
            return None
        data = json.loads(result.stdout)
        # lark-cli output format varies by version:
        #   raw API envelope: {"code":0,"data":{"freebusy_list":[...]}}
        #   ok envelope:      {"ok":true,"data":{"freebusy_list":[...]}} or {"ok":true,"data":[...]}
        if data.get("code") == 0 or (data.get("ok") and "data" in data):
            payload = data.get("data", [])
            if isinstance(payload, dict):
                return payload.get("freebusy_list", [])
            if isinstance(payload, list):
                return payload
        log(f"ERROR: freebusy unexpected response: {json.dumps(data)[:200]}")
        return None
    except Exception as e:
        log(f"ERROR: freebusy exception: {e}")
        return None


def parse_utc_to_local(utc_str: str, offset_hours: int = 8) -> datetime:
    """将日历时间字符串转为目标时区的本地时间（naive datetime）。

    正确处理 RFC3339 offset：Lark 通常返回 UTC（Z / +00:00），但 offset 本身
    是合法输入。先按真实时区解析，再换算到目标 offset；解析失败时回退到旧逻辑
    （去掉 offset 后按 naive 处理）以保证健壮性。
    """
    target_tz = timezone(timedelta(hours=offset_hours))
    iso = utc_str.strip()
    # datetime.fromisoformat 在 3.11 支持 Z 后缀；老版本需替换为 +00:00
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            # 无时区信息：按 UTC 解释（保持与历史行为一致）
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(target_tz).replace(tzinfo=None)
    except ValueError:
        # 回退：剥掉小数秒和 offset，按 UTC + 固定偏移处理
        cleaned = utc_str.split(".")[0].rstrip("Z")
        if "+" in cleaned:
            cleaned = cleaned[:cleaned.rindex("+")]
        elif cleaned.endswith("-00:00"):
            cleaned = cleaned[:-6]
        dt = datetime.strptime(cleaned, "%Y-%m-%dT%H:%M:%S")
        return dt + timedelta(hours=offset_hours)


def merge_time_slots(slots: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """合并重叠和相邻的时段"""
    if not slots:
        return []
    slots.sort(key=lambda x: x[0])
    merged = [slots[0]]
    for start, end in slots[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def format_freebusy(raw_slots: list | None) -> str:
    """将原始 freebusy JSON 数据处理为人类可读的 UTC+8 格式"""
    if raw_slots is None:
        return "查询失败，请稍后再试"
    if not raw_slots:
        return "无忙碌时段（全天空闲）"

    seen = set()
    slots = []
    for item in raw_slots:
        start_utc = item.get("start_time", "")
        end_utc = item.get("end_time", "")
        if not start_utc or not end_utc:
            continue
        key = (start_utc, end_utc)
        if key in seen:
            continue
        seen.add(key)
        start_local = parse_utc_to_local(start_utc)
        end_local = parse_utc_to_local(end_utc)
        slots.append((start_local, end_local))

    merged = merge_time_slots(slots)

    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    days: dict[str, list[str]] = {}
    for start, end in merged:
        date_key = start.strftime("%Y-%m-%d")
        wd = weekday_names[start.weekday()]
        label = f"{date_key}（{wd}）"
        time_range = f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}"
        if label not in days:
            days[label] = []
        days[label].append(time_range)

    lines = []
    for day_label, times in days.items():
        lines.append(f"{day_label}: {', '.join(times)}")

    return "\n".join(lines) + "\n(时区: UTC+8)"


def _validate_date(date_str: str) -> bool:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def execute_query_freebusy(person: str, start_date: str, end_date: str) -> str:
    """执行日历查询工具"""
    if not _validate_date(start_date) or not _validate_date(end_date):
        return f"日期格式错误，需要 YYYY-MM-DD 格式。收到: start={start_date}, end={end_date}"

    date_label = start_date if start_date == end_date else f"{start_date} ~ {end_date}"
    log(f"TOOL query_freebusy: person={person}, range={date_label}")

    if person == "aaron":
        slots_a = query_freebusy_raw(USERS["aaron"]["open_id"], start_date, end_date)
        slots_j = query_freebusy_raw(USERS["jackson"]["open_id"], start_date, end_date)
        if slots_a is None and slots_j is None:
            formatted = format_freebusy(None)
        else:
            formatted = format_freebusy((slots_a or []) + (slots_j or []))
        return f"Aaron 在 {date_label} 的忙碌时段:\n{formatted}"

    elif person == "thomas":
        slots_t = query_freebusy_raw(USERS["thomas"]["open_id"], start_date, end_date)
        slots_d = query_freebusy_raw(USERS["deric"]["open_id"], start_date, end_date)
        if slots_t is None and slots_d is None:
            formatted = format_freebusy(None)
        else:
            formatted = format_freebusy((slots_t or []) + (slots_d or []))
        return f"Thomas Chang 在 {date_label} 的忙碌时段:\n{formatted}"

    elif person in USERS:
        user = USERS[person]
        slots = query_freebusy_raw(user["open_id"], start_date, end_date)
        formatted = format_freebusy(slots)
        return f"{user['name']} 在 {date_label} 的忙碌时段:\n{formatted}"

    return f"未知人员: {person}"


def search_chat_by_name(query: str) -> str | None:
    """通过群名搜索 chat_id"""
    try:
        result = subprocess.run(
            [LARK_CLI, "im", "+chat-search",
             "--query", query,
             "--as", "bot"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            chats = data.get("data", {}).get("chats", [])
            if chats:
                return chats[0].get("chat_id")
    except Exception as e:
        log(f"ERROR: search_chat failed: {e}")
    return None


def fetch_chat_messages(chat_id: str, limit: int = 50) -> list[str]:
    """获取群聊最近的消息"""
    try:
        result = subprocess.run(
            [LARK_CLI, "im", "+chat-messages-list",
             "--chat-id", chat_id,
             "--as", "bot"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            messages = data.get("data", {}).get("messages", [])
            text_msgs = []
            for m in messages[:limit]:
                content = m.get("content", "")
                sender = m.get("sender", {}).get("name", "?")
                create_time = m.get("create_time", "")
                msg_type = m.get("msg_type", "")
                if msg_type in ("text", "post") and content and not content.startswith("["):
                    text_msgs.append(f"[{create_time}] {sender}: {content}")
            return text_msgs
    except Exception as e:
        log(f"ERROR: fetch_chat_messages failed: {e}")
    return []


def execute_search_chat_messages(chat_id: str = "", chat_name: str = "", limit: int = 50) -> str:
    """执行群聊消息搜索工具"""
    limit = min(limit, 100)
    log(f"TOOL search_chat_messages: chat_id={chat_id}, chat_name={chat_name}, limit={limit}")

    if not chat_id and chat_name:
        found = search_chat_by_name(chat_name)
        if not found:
            return f"未找到名为「{chat_name}」的群聊，可能 bot 未加入该群。"
        chat_id = found

    if not chat_id:
        return "请提供群聊名称或 chat_id。"

    messages = fetch_chat_messages(chat_id, limit)
    if not messages:
        label = chat_name or chat_id
        return f"群聊「{label}」没有找到近期文本消息。"

    msg_text = "\n".join(messages)
    label = chat_name or chat_id
    return f"群聊「{label}」近期消息（最新在前）:\n{msg_text}"


def execute_tool(tool_name: str, tool_input: dict, conv_key: str = "") -> str:
    """路由并执行工具调用"""
    if tool_name == "query_freebusy":
        return execute_query_freebusy(
            person=tool_input.get("person", "ethan"),
            start_date=tool_input.get("start_date", ""),
            end_date=tool_input.get("end_date", ""),
        )
    elif tool_name == "search_chat_messages":
        return execute_search_chat_messages(
            chat_id=tool_input.get("chat_id", ""),
            chat_name=tool_input.get("chat_name", ""),
            limit=tool_input.get("limit", 50),
        )
    elif tool_name == "get_pm_guidance":
        return execute_get_pm_guidance(
            query=tool_input.get("query", ""),
            conv_key=conv_key,
        )
    return f"未知工具: {tool_name}"


# =============================================================================
# Bedrock API Call with Tool Use Loop
# =============================================================================

def call_bedrock(system_prompt: str, messages: list, use_tools: bool = True, max_tokens: int = 4096) -> dict:
    """调用 Bedrock Converse API，返回原始响应"""
    model_path = BEDROCK_MODEL_ID.replace(":", "%3A")
    url = f"https://bedrock-runtime.{AWS_REGION}.amazonaws.com/model/{model_path}/converse"

    payload = {
        "system": [{"text": system_prompt}],
        "messages": messages,
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0.7},
    }
    if use_tools:
        payload["toolConfig"] = {"tools": TOOLS}

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {AWS_BEARER_TOKEN}",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def generate_reply(content: str, sender_id: str, chat_type: str, chat_id: str, conv_key: str) -> tuple[str, dict]:
    """调用 Bedrock Claude，支持多轮 tool use 循环"""
    token_usage = new_token_usage()
    if not AWS_BEARER_TOKEN:
        log("ERROR: AWS_BEARER_TOKEN_BEDROCK not set")
        return "抱歉，我暂时无法处理这条消息，稍后 Ethan 会回复你。", token_usage

    try:
        today = now_utc8()
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        today_info = f"今天是 {today.strftime('%Y-%m-%d')} {weekday_names[today.weekday()]}，当前时间 {today.strftime('%H:%M')} (UTC+8)"

        system_prompt = get_system_prompt()
        system_prompt += f"\n\n## 当前时间\n{today_info}"
        if chat_type == "group" and chat_id:
            system_prompt += f"\n\n## 当前对话信息\n当前群聊 chat_id: {chat_id}"

        messages = list(conversation_history[conv_key])
        messages.append({"role": "user", "content": [{"text": content}]})

        max_rounds = 5
        for _ in range(max_rounds):
            result = call_bedrock(system_prompt, messages)
            add_token_usage(token_usage, result)

            stop_reason = result.get("stopReason", "")
            output_message = result.get("output", {}).get("message", {})
            output_content = output_message.get("content", [])

            if stop_reason == "end_turn":
                # 提取文本回复
                reply_parts = []
                for block in output_content:
                    if "text" in block:
                        reply_parts.append(block["text"])
                reply = "\n".join(reply_parts).strip()

                # 保存对话历史
                conversation_history[conv_key].append({"role": "user", "content": [{"text": content}]})
                conversation_history[conv_key].append({"role": "assistant", "content": [{"text": reply}]})
                if len(conversation_history[conv_key]) > MAX_HISTORY * 2:
                    conversation_history[conv_key] = conversation_history[conv_key][-(MAX_HISTORY * 2):]
                if len(conversation_history) > MAX_CONVERSATIONS:
                    keys_to_remove = list(conversation_history.keys())[:len(conversation_history) - MAX_CONVERSATIONS]
                    for k in keys_to_remove:
                        del conversation_history[k]

                return reply, token_usage

            elif stop_reason == "tool_use":
                # 将 assistant 的响应（含 tool_use blocks）加入 messages
                messages.append({"role": "assistant", "content": output_content})

                # 执行所有 tool calls 并构建 tool results
                tool_results = []
                for block in output_content:
                    if "toolUse" in block:
                        tool_use = block["toolUse"]
                        tool_id = tool_use["toolUseId"]
                        tool_name = tool_use["name"]
                        tool_input = tool_use.get("input", {})

                        log(f"TOOL_CALL: {tool_name}({json.dumps(tool_input, ensure_ascii=False)[:100]})")
                        add_usage_tool(token_usage, tool_name)
                        tool_result = execute_tool(tool_name, tool_input, conv_key=conv_key)
                        log(f"TOOL_RESULT: {tool_name} -> {tool_result[:80]}...")

                        tool_results.append({
                            "toolResult": {
                                "toolUseId": tool_id,
                                "content": [{"text": tool_result}],
                            }
                        })

                messages.append({"role": "user", "content": tool_results})

            else:
                log(f"WARN: unexpected stopReason: {stop_reason}")
                reply_parts = [b["text"] for b in output_content if "text" in b]
                reply = "\n".join(reply_parts).strip() if reply_parts else "抱歉，处理过程中出现了问题。"
                return reply, token_usage

        log("WARN: max tool rounds reached")
        return "抱歉，处理时间过长，请稍后再试。", token_usage

    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        log(f"ERROR: Bedrock HTTP {e.code}: {body[:200]}")
        return "抱歉，我暂时无法处理这条消息，稍后 Ethan 会回复你。", token_usage
    except Exception as e:
        log(f"ERROR: Bedrock call failed: {e}")
        return "抱歉，我暂时无法处理这条消息，稍后 Ethan 会回复你。", token_usage


# =============================================================================
# Reply & Relay
# =============================================================================

def strip_markdown(text: str) -> str:
    """移除 Markdown 格式符号，飞书文本消息会原样显示"""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text


def send_reply(message_id: str, reply_text: str):
    """通过 lark-cli 回复消息"""
    reply_text = strip_markdown(reply_text)
    try:
        result = subprocess.run(
            [LARK_CLI, "im", "+messages-reply",
             "--message-id", message_id,
             "--text", reply_text,
             "--as", "bot"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            log(f"ERROR: reply failed: {result.stderr}")
        else:
            log(f"SENT: reply to {message_id}")
    except Exception as e:
        log(f"ERROR: send_reply exception: {e}")


RELAY_CHAT_ID = os.environ.get("RELAY_CHAT_ID", "")


def get_user_name(open_id: str) -> str:
    """通过 open_id 查询用户姓名"""
    try:
        result = subprocess.run(
            [LARK_CLI, "contact", "+get-user",
             "--user-id", open_id,
             "--as", "bot"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return data.get("data", {}).get("user", {}).get("name", open_id)
    except Exception:
        pass
    return open_id


def summarize_for_relay(content: str, conv_key: str) -> tuple[str, dict]:
    """用 AI 解析对话上下文，生成转达摘要"""
    token_usage = new_token_usage()
    if not AWS_BEARER_TOKEN:
        return content, token_usage

    try:
        history = list(conversation_history[conv_key])
        history.append({"role": "user", "content": [{"text": content}]})

        result = call_bedrock(
            system_prompt="你是一个消息摘要助手。请根据对话上下文，用 1-3 句话总结对方想要转达给 Ethan 的核心内容。只输出摘要，不要加前缀或解释。如果上下文不足，就直接用原始消息内容。",
            messages=history,
            use_tools=False,
            max_tokens=256,
        )
        add_token_usage(token_usage, result)

        output_content = result.get("output", {}).get("message", {}).get("content", [])
        for block in output_content:
            if "text" in block:
                return block["text"].strip(), token_usage
        return content, token_usage
    except Exception as e:
        log(f"ERROR: summarize_for_relay failed: {e}")
        return content, token_usage


def notify_ethan(sender_id: str, chat_type: str, chat_id: str, content: str, conv_key: str) -> dict:
    """转达消息给 Ethan：AI 摘要后发到 Ethan Assistant Group"""
    token_usage = new_token_usage()
    if not RELAY_CHAT_ID:
        log("ERROR: RELAY_CHAT_ID not set, skipping relay")
        return token_usage
    sender_name = get_user_name(sender_id)
    summary, relay_usage = summarize_for_relay(content, conv_key)
    merge_token_usage(token_usage, relay_usage)
    source = "群聊" if chat_type != "p2p" else "私聊"
    msg = f"[转达通知]\n来自: {sender_name}（{source}）\n内容: {summary}"
    try:
        result = subprocess.run(
            [LARK_CLI, "im", "+messages-send",
             "--chat-id", RELAY_CHAT_ID,
             "--text", msg,
             "--as", "bot"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            log(f"ERROR: notify_ethan failed: {result.stderr}")
        else:
            log(f"NOTIFY: forwarded to Ethan from {sender_name}")
    except Exception as e:
        log(f"ERROR: notify_ethan exception: {e}")
    return token_usage


# =============================================================================
# Event Processing
# =============================================================================

processed_events: dict[str, None] = {}
MAX_PROCESSED_EVENTS = 1000


def process_event(event: dict):
    """处理单条消息事件"""
    event_id = event.get("event_id", "")
    if event_id:
        if event_id in processed_events:
            log(f"SKIP: duplicate event {event_id}")
            return
        processed_events[event_id] = None
        if len(processed_events) > MAX_PROCESSED_EVENTS:
            keys = list(processed_events.keys())[:MAX_PROCESSED_EVENTS // 2]
            for eid in keys:
                del processed_events[eid]

    sender_id = event.get("sender_id", "")
    chat_id = event.get("chat_id", "")
    chat_type = event.get("chat_type", "")
    message_type = event.get("message_type", "")
    content = event.get("content", "")
    message_id = event.get("message_id", "")

    if BOT_OPEN_ID and sender_id == BOT_OPEN_ID:
        log(f"SKIP: message from self")
        return

    # 群聊过滤：只回复 @BD Agent 的消息
    if chat_type == "group":
        if "@bd agent" not in content.lower():
            log(f"SKIP: group message without @bot from {sender_id}")
            return

    if message_type not in ("text", "post"):
        log(f"SKIP: unsupported type={message_type} from {sender_id}")
        send_reply(message_id, "抱歉，我目前只能处理文字消息。")
        return

    if not content:
        log(f"SKIP: empty content from {sender_id}")
        return

    log(f"RECV: [{chat_type}] from={sender_id} type={message_type} msg_id={message_id} content={content[:80]}")

    conv_key = sender_id if chat_type == "p2p" else chat_id
    sender_name = get_user_name(sender_id)
    reply, token_usage = generate_reply(content, sender_id, chat_type, chat_id, conv_key)

    if reply:
        if "[RELAY]" in reply:
            reply_clean = reply.replace("[RELAY]", "").strip()
            log(f"RELAY: AI decided to relay, from {sender_id}")
            relay_usage = notify_ethan(sender_id, chat_type, chat_id, content, conv_key)
            merge_token_usage(token_usage, relay_usage)
            send_reply(message_id, reply_clean)
        else:
            log(f"REPLY: {reply[:80]}...")
            send_reply(message_id, reply)

        record_token_usage(sender_name, chat_type, content, reply, token_usage)


# =============================================================================
# Main
# =============================================================================

# 启动重试配置：lark-cli 取 tenant access token 偶发 HTTP 429，
# 不重试就要等下一个 5 小时 cron，期间 bot 全程不可用。
MAX_STARTUP_ATTEMPTS = 5
STARTUP_BACKOFF_SECONDS = [10, 30, 60, 120, 300]
CONSUMER_RESTART_BACKOFF_SECONDS = 30
# 启动看门狗：等待 ready 标记的上限。lark-cli 卡住但不退出也不打印 ready 时，
# 没有超时会让 readline 无限阻塞，重试逻辑永远不触发。
STARTUP_READY_TIMEOUT_SECONDS = 90
# 熔断：ready 之后若 consumer 在 CONSUMER_CRASH_WINDOW 秒内连续异常退出
# 超过 MAX_CONSECUTIVE_CRASHES 次，说明是永久性故障（配置/权限失效），
# 直接失败退出让 workflow 显性报错，而不是无限空转。
CONSUMER_CRASH_WINDOW_SECONDS = 300
MAX_CONSECUTIVE_CRASHES = 4


def start_consumer():
    """启动 lark-cli event consume 并在看门狗超时内等待 ready 标记。

    成功返回 proc；启动失败（认证 429、卡住超时、子进程退出）清理后返回 None。
    """
    import threading

    cmd = [LARK_CLI, "event", "consume", "im.message.receive_v1", "--as", "bot"]
    log(f"Starting: {' '.join(cmd)}")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    ready_event = threading.Event()

    def watch_stderr():
        # 在后台线程读 stderr，命中 ready 标记就置位事件。
        # readline 在子进程被 kill / stderr 关闭后会返回空串退出循环。
        try:
            while True:
                line = proc.stderr.readline()
                if not line:
                    break
                line = line.strip()
                if line:
                    log(f"[event] {line}")
                if "[event] ready" in line:
                    ready_event.set()
                    return
        except Exception:
            pass

    watcher = threading.Thread(target=watch_stderr, daemon=True)
    watcher.start()

    if ready_event.wait(timeout=STARTUP_READY_TIMEOUT_SECONDS):
        return proc

    # 超时仍未 ready（卡住或慢启动）：清理子进程后返回 None
    log(f"WARN: consumer not ready within {STARTUP_READY_TIMEOUT_SECONDS}s")
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    return None


def main():
    log("=== Ethan Assistant started (tool-use mode) ===")
    log(f"Model: {BEDROCK_MODEL_ID}, Region: {AWS_REGION}")

    current_proc = {"proc": None}

    def shutdown(signum, frame):
        log(f"Received signal {signum}, shutting down...")
        proc = current_proc.get("proc")
        try:
            if proc and proc.stdin:
                proc.stdin.close()
            if proc:
                proc.wait(timeout=10)
        except Exception:
            if proc:
                proc.terminate()
            try:
                if proc:
                    proc.wait(timeout=5)
            except Exception:
                if proc:
                    proc.kill()
        log("=== Ethan Assistant stopped ===")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    import threading

    def drain_stderr(proc):
        for line in proc.stderr:
            line = line.strip()
            if line:
                log(f"[event] {line}")

    # 熔断状态：记录最近一次 ready 之后连续“短命退出”的次数和首次时间
    crash_count = 0
    crash_window_start = 0.0

    while True:
        proc = None
        for attempt in range(1, MAX_STARTUP_ATTEMPTS + 1):
            proc = start_consumer()
            if proc:
                break
            if attempt < MAX_STARTUP_ATTEMPTS:
                delay = STARTUP_BACKOFF_SECONDS[min(attempt - 1, len(STARTUP_BACKOFF_SECONDS) - 1)]
                log(f"WARN: consumer not ready (attempt {attempt}/{MAX_STARTUP_ATTEMPTS}), retrying in {delay}s...")
                time.sleep(delay)

        if not proc:
            log(f"ERROR: event consume did not become ready after {MAX_STARTUP_ATTEMPTS} attempts")
            sys.exit(1)

        current_proc["proc"] = proc
        log("Event consumer ready, listening for messages...")
        started_at = time.monotonic()

        stderr_thread = threading.Thread(target=drain_stderr, args=(proc,), daemon=True)
        stderr_thread.start()

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                log(f"WARN: invalid JSON: {line[:80]}")
                continue

            try:
                process_event(event)
            except Exception as e:
                log(f"ERROR: process_event failed: {e}")

        proc.wait()
        current_proc["proc"] = None
        uptime = time.monotonic() - started_at
        log(f"Event consume exited with code {proc.returncode} after {uptime:.0f}s uptime")

        # 熔断：只统计“ready 后很快就崩”的退出（短命）。长时间正常运行后的退出
        # （如网络抖动、cron 周期到期）重置计数，避免误熔断。
        now = time.monotonic()
        if uptime < CONSUMER_CRASH_WINDOW_SECONDS:
            if crash_count == 0 or (now - crash_window_start) > CONSUMER_CRASH_WINDOW_SECONDS:
                crash_window_start = now
                crash_count = 1
            else:
                crash_count += 1
            if crash_count >= MAX_CONSECUTIVE_CRASHES:
                log(f"ERROR: consumer crashed {crash_count} times within "
                    f"{CONSUMER_CRASH_WINDOW_SECONDS}s — likely a permanent failure, exiting")
                sys.exit(1)
        else:
            crash_count = 0

        log(f"WARN: restarting event consumer in {CONSUMER_RESTART_BACKOFF_SECONDS}s (crash_count={crash_count})...")
        time.sleep(CONSUMER_RESTART_BACKOFF_SECONDS)


if __name__ == "__main__":
    main()
