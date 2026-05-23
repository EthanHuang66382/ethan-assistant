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
conversation_history: dict[str, list] = defaultdict(list)

UTC8 = timezone(timedelta(hours=8))


def now_utc8() -> datetime:
    return datetime.now(UTC8)


def log(msg: str):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def get_system_prompt() -> str:
    if SYSTEM_PROMPT_FILE.exists():
        return SYSTEM_PROMPT_FILE.read_text().strip()
    return "你是 Ethan Huang 的 AI 助理。请用专业友善的语气回复消息。如果不确定如何回答，可以告知对方你会转达给 Ethan。回复请简洁明了。"


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
            "description": "搜索指定群聊并获取最近的消息，用于生成群聊摘要/总结。需要 bot 已加入该群。",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "chat_name": {
                            "type": "string",
                            "description": "要搜索的群聊名称关键词"
                        },
                        "limit": {
                            "type": "integer",
                            "description": "获取最近消息的数量，默认 50，最大 100",
                            "default": 50
                        }
                    },
                    "required": ["chat_name"]
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
        if data.get("code") == 0:
            return data.get("data", {}).get("freebusy_list", [])
        if data.get("ok") and "data" in data:
            return data["data"]
        log(f"ERROR: freebusy unexpected response: {json.dumps(data)[:200]}")
        return None
    except Exception as e:
        log(f"ERROR: freebusy exception: {e}")
        return None


def parse_utc_to_local(utc_str: str, offset_hours: int = 8) -> datetime:
    """将 UTC 时间字符串转为本地时间"""
    dt = datetime.strptime(utc_str, "%Y-%m-%dT%H:%M:%SZ")
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
    from collections import OrderedDict
    days: dict[str, list[str]] = OrderedDict()
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


def execute_query_freebusy(person: str, start_date: str, end_date: str) -> str:
    """执行日历查询工具"""
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


def execute_search_chat_messages(chat_name: str, limit: int = 50) -> str:
    """执行群聊消息搜索工具"""
    limit = min(limit, 100)
    log(f"TOOL search_chat_messages: chat_name={chat_name}, limit={limit}")

    chat_id = search_chat_by_name(chat_name)
    if not chat_id:
        return f"未找到名为「{chat_name}」的群聊，可能 bot 未加入该群。"

    messages = fetch_chat_messages(chat_id, limit)
    if not messages:
        return f"群聊「{chat_name}」没有找到近期文本消息。"

    msg_text = "\n".join(messages)
    return f"群聊「{chat_name}」近期消息（最新在前）:\n{msg_text}"


def execute_tool(tool_name: str, tool_input: dict) -> str:
    """路由并执行工具调用"""
    if tool_name == "query_freebusy":
        return execute_query_freebusy(
            person=tool_input.get("person", "ethan"),
            start_date=tool_input.get("start_date", ""),
            end_date=tool_input.get("end_date", ""),
        )
    elif tool_name == "search_chat_messages":
        return execute_search_chat_messages(
            chat_name=tool_input.get("chat_name", ""),
            limit=tool_input.get("limit", 50),
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


def generate_reply(content: str, sender_id: str, chat_type: str, conv_key: str) -> str:
    """调用 Bedrock Claude，支持多轮 tool use 循环"""
    if not AWS_BEARER_TOKEN:
        log("ERROR: AWS_BEARER_TOKEN_BEDROCK not set")
        return "抱歉，我暂时无法处理这条消息，稍后 Ethan 会回复你。"

    try:
        today = now_utc8()
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        today_info = f"今天是 {today.strftime('%Y-%m-%d')} {weekday_names[today.weekday()]}，当前时间 {today.strftime('%H:%M')} (UTC+8)"

        system_prompt = get_system_prompt()
        system_prompt += f"\n\n## 当前时间\n{today_info}"

        messages = list(conversation_history[conv_key])
        messages.append({"role": "user", "content": [{"text": content}]})

        max_rounds = 5
        for _ in range(max_rounds):
            result = call_bedrock(system_prompt, messages)

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

                return reply

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
                        tool_result = execute_tool(tool_name, tool_input)
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
                return "\n".join(reply_parts).strip() if reply_parts else "抱歉，处理过程中出现了问题。"

        log("WARN: max tool rounds reached")
        return "抱歉，处理时间过长，请稍后再试。"

    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        log(f"ERROR: Bedrock HTTP {e.code}: {body[:200]}")
        return "抱歉，我暂时无法处理这条消息，稍后 Ethan 会回复你。"
    except Exception as e:
        log(f"ERROR: Bedrock call failed: {e}")
        return "抱歉，我暂时无法处理这条消息，稍后 Ethan 会回复你。"


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


def summarize_for_relay(content: str, conv_key: str) -> str:
    """用 AI 解析对话上下文，生成转达摘要"""
    if not AWS_BEARER_TOKEN:
        return content

    try:
        history = list(conversation_history[conv_key])
        history.append({"role": "user", "content": [{"text": content}]})

        result = call_bedrock(
            system_prompt="你是一个消息摘要助手。请根据对话上下文，用 1-3 句话总结对方想要转达给 Ethan 的核心内容。只输出摘要，不要加前缀或解释。如果上下文不足，就直接用原始消息内容。",
            messages=history,
            use_tools=False,
            max_tokens=256,
        )

        output_content = result.get("output", {}).get("message", {}).get("content", [])
        for block in output_content:
            if "text" in block:
                return block["text"].strip()
        return content
    except Exception as e:
        log(f"ERROR: summarize_for_relay failed: {e}")
        return content


def notify_ethan(sender_id: str, chat_type: str, chat_id: str, content: str, conv_key: str):
    """转达消息给 Ethan：AI 摘要后发到 Ethan Assistant Group"""
    if not RELAY_CHAT_ID:
        log("ERROR: RELAY_CHAT_ID not set, skipping relay")
        return
    sender_name = get_user_name(sender_id)
    summary = summarize_for_relay(content, conv_key)
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


# =============================================================================
# Event Processing
# =============================================================================

processed_events: set[str] = set()
MAX_PROCESSED_EVENTS = 1000


def process_event(event: dict):
    """处理单条消息事件"""
    event_id = event.get("event_id", "")
    if event_id:
        if event_id in processed_events:
            log(f"SKIP: duplicate event {event_id}")
            return
        processed_events.add(event_id)
        if len(processed_events) > MAX_PROCESSED_EVENTS:
            to_remove = list(processed_events)[:MAX_PROCESSED_EVENTS // 2]
            for eid in to_remove:
                processed_events.discard(eid)

    sender_id = event.get("sender_id", "")
    chat_id = event.get("chat_id", "")
    chat_type = event.get("chat_type", "")
    message_type = event.get("message_type", "")
    content = event.get("content", "")
    message_id = event.get("message_id", "")

    if BOT_OPEN_ID and sender_id == BOT_OPEN_ID:
        log(f"SKIP: message from self")
        return

    if chat_type == "group":
        mention_markers = ["@ethan assistant", "@ethanassistant", "@assistant"]
        content_lower = content.lower()
        has_mention = any(m in content_lower for m in mention_markers)
        if not has_mention:
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
    reply = generate_reply(content, sender_id, chat_type, conv_key)

    if reply:
        if "[RELAY]" in reply:
            reply_clean = reply.replace("[RELAY]", "").strip()
            log(f"RELAY: AI decided to relay, from {sender_id}")
            notify_ethan(sender_id, chat_type, chat_id, content, conv_key)
            send_reply(message_id, reply_clean)
        else:
            log(f"REPLY: {reply[:80]}...")
            send_reply(message_id, reply)


# =============================================================================
# Main
# =============================================================================

def main():
    log("=== Ethan Assistant started (tool-use mode) ===")
    log(f"Model: {BEDROCK_MODEL_ID}, Region: {AWS_REGION}")

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

    ready = False
    while True:
        line = proc.stderr.readline()
        if not line:
            break
        line = line.strip()
        log(f"[event] {line}")
        if "[event] ready" in line:
            ready = True
            break

    if not ready:
        log("ERROR: event consume did not become ready")
        proc.terminate()
        sys.exit(1)

    log("Event consumer ready, listening for messages...")

    def shutdown(signum, frame):
        log(f"Received signal {signum}, shutting down...")
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=10)
        except Exception:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        log("=== Ethan Assistant stopped ===")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    import threading

    def drain_stderr():
        for line in proc.stderr:
            line = line.strip()
            if line:
                log(f"[event] {line}")

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
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
    log(f"Event consume exited with code {proc.returncode}")
    log("=== Ethan Assistant stopped ===")
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
