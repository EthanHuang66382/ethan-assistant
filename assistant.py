#!/usr/bin/env python3
"""Ethan Assistant — 飞书对话服务

监听飞书消息，用 AWS Bedrock Claude 生成回复。
支持 Bearer Token 认证（AWS_BEARER_TOKEN_BEDROCK）。
支持查询 Ethan / Aaron / Jackson Li 的日历忙闲。
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
MAX_HISTORY = 10  # 保留最近 10 轮对话
conversation_history: dict[str, list] = defaultdict(list)

CALENDAR_KEYWORDS = re.compile(
    r"(日历|日程|calendar|schedule|忙|闲|空闲|有空|meeting|会议|安排|行程|freebusy|忙闲|有没有空|什么时候有空)",
    re.IGNORECASE,
)

CHAT_SUMMARY_KEYWORDS = re.compile(
    r"(总结|摘要|summary|summarize|概括|回顾|近期.*消息|最近.*消息|聊了什么|说了什么|讨论了什么)",
    re.IGNORECASE,
)

CHAT_NAME_QUOTED = re.compile(r"[「「\"【](.+?)[」」\"】]")

# 人名匹配：Aaron = Aaron + Jackson Li，Thomas = Thomas Chang + Deric Chan
PERSON_PATTERNS = {
    "ethan": re.compile(r"(ethan|我的|你的|老板)", re.IGNORECASE),
    "aaron": re.compile(r"(aaron|jackson\s*li)", re.IGNORECASE),
    "alvin": re.compile(r"(alvin|xiao)", re.IGNORECASE),
    "thomas": re.compile(r"(thomas|deric|chang|chan)", re.IGNORECASE),
}


UTC8 = timezone(timedelta(hours=8))


def now_utc8() -> datetime:
    return datetime.now(UTC8)


def log(msg: str):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def get_system_prompt() -> str:
    if SYSTEM_PROMPT_FILE.exists():
        return SYSTEM_PROMPT_FILE.read_text().strip()
    return "你是 Ethan Huang 的 AI 助理。请用专业友善的语气回复消息。如果不确定如何回答，可以告知对方你会转达给 Ethan。回复请简洁明了。"


def query_freebusy_raw(user_id: str, start_date: str, end_date: str) -> list | None:
    """查询指定用户的忙闲信息（仅飞书日历，排除外部日历），返回时段列表。失败返回 None。"""
    if not user_id:
        log("ERROR: freebusy query skipped — empty user_id")
        return None
    # 将日期转为 RFC 3339 格式
    time_min = f"{start_date}T00:00:00+08:00"
    # end_date 取当天结束
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
        # 原生 API 返回格式: {code:0, data:{freebusy_list:[...]}}
        if data.get("code") == 0:
            return data.get("data", {}).get("freebusy_list", [])
        # 兼容 shortcut 格式
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


def format_freebusy(raw_slots: list | None, offset_hours: int = 8) -> str:
    """将原始 freebusy JSON 数据处理为人类可读的 UTC+8 格式，去重并合并"""
    if raw_slots is None:
        return "查询失败，请稍后再试"
    if not raw_slots:
        return "无忙碌时段（全天空闲）"

    # 去重：用 (start, end) 元组做 set
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
        start_local = parse_utc_to_local(start_utc, offset_hours)
        end_local = parse_utc_to_local(end_utc, offset_hours)
        slots.append((start_local, end_local))

    # 合并相邻/重叠时段
    merged = merge_time_slots(slots)

    # 按日期分组输出
    from collections import OrderedDict
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
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


def parse_date_range_from_message(content: str) -> tuple[str, str]:
    """从消息中提取日期范围 (start, end)，支持单日和多日范围，最多一个月"""
    today = now_utc8()

    # 范围表达式
    if re.search(r"这[个]?月|本月|this month", content, re.IGNORECASE):
        start = today.replace(day=1)
        next_month = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        return start.strftime("%Y-%m-%d"), next_month.strftime("%Y-%m-%d")

    if re.search(r"下[个]?月|next month", content, re.IGNORECASE):
        first_next = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        last_next = (first_next + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        return first_next.strftime("%Y-%m-%d"), last_next.strftime("%Y-%m-%d")

    if re.search(r"这[个]?周|本周|this week", content, re.IGNORECASE):
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
        return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    if re.search(r"下[个]?周|next week", content, re.IGNORECASE):
        start = today - timedelta(days=today.weekday()) + timedelta(weeks=1)
        end = start + timedelta(days=6)
        return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    m = re.search(r"未来(\d+)[天日]|接下来(\d+)[天日]|(?:next|coming|upcoming)\s+(\d+)\s*days?", content, re.IGNORECASE)
    if m:
        days = int(m.group(1) or m.group(2) or m.group(3))
        days = min(days, 30)
        return today.strftime("%Y-%m-%d"), (today + timedelta(days=days - 1)).strftime("%Y-%m-%d")

    m = re.search(r"未来(\d+)[周]|接下来(\d+)[周]|next (\d+) weeks?", content, re.IGNORECASE)
    if m:
        weeks = int(m.group(1) or m.group(2) or m.group(3))
        weeks = min(weeks, 4)
        return today.strftime("%Y-%m-%d"), (today + timedelta(weeks=weeks) - timedelta(days=1)).strftime("%Y-%m-%d")

    if re.search(r"未来一[个]?月|接下来一[个]?月", content):
        return today.strftime("%Y-%m-%d"), (today + timedelta(days=29)).strftime("%Y-%m-%d")

    if re.search(r"一周|一个星期|7天", content):
        return today.strftime("%Y-%m-%d"), (today + timedelta(days=6)).strftime("%Y-%m-%d")

    # 单日表达式
    if re.search(r"今天|今日|today", content, re.IGNORECASE):
        d = today.strftime("%Y-%m-%d")
        return d, d
    if re.search(r"明天|明日|tomorrow", content, re.IGNORECASE):
        d = (today + timedelta(days=1)).strftime("%Y-%m-%d")
        return d, d
    if re.search(r"后天", content):
        d = (today + timedelta(days=2)).strftime("%Y-%m-%d")
        return d, d
    if re.search(r"昨天|昨日|yesterday", content, re.IGNORECASE):
        d = (today - timedelta(days=1)).strftime("%Y-%m-%d")
        return d, d

    # 匹配中文周几
    weekday_map = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
    m = re.search(r"(下)?周([一二三四五六日天])", content)
    if m:
        next_week = m.group(1) is not None
        target_wd = weekday_map[m.group(2)]
        current_wd = today.weekday()
        days_ahead = target_wd - current_wd
        if next_week:
            days_ahead += 7
        elif days_ahead <= 0:
            days_ahead += 7
        d = (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        return d, d

    # 匹配英文星期
    en_weekday_map = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                      "friday": 4, "saturday": 5, "sunday": 6,
                      "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    m = re.search(r"(next\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)", content, re.IGNORECASE)
    if m:
        next_week = m.group(1) is not None
        target_wd = en_weekday_map[m.group(2).lower()]
        current_wd = today.weekday()
        days_ahead = target_wd - current_wd
        if next_week:
            days_ahead += 7
        elif days_ahead <= 0:
            days_ahead += 7
        d = (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        return d, d

    # 匹配具体日期 YYYY-MM-DD 或 MM-DD
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", content)
    if m:
        return m.group(0), m.group(0)
    m = re.search(r"(\d{1,2})[月/](\d{1,2})[日号]?", content)
    if m:
        d = f"{today.year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        return d, d

    # 默认今天
    d = today.strftime("%Y-%m-%d")
    return d, d


def detect_calendar_query(content: str) -> list:
    """检测是否是日历查询，返回需要查询的用户列表"""
    if not CALENDAR_KEYWORDS.search(content):
        return []

    targets = []
    for key, pattern in PERSON_PATTERNS.items():
        if pattern.search(content):
            targets.append(key)

    # 如果提到了日历但没指定人，默认查 Ethan
    if not targets:
        targets = ["ethan"]

    return targets


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
            # 只取文本消息，过滤系统消息
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


def extract_chat_name(content: str) -> str | None:
    """从消息中提取群名"""
    # 优先匹配引号/括号包裹的群名
    m = CHAT_NAME_QUOTED.search(content)
    if m:
        return m.group(1).strip()

    # 去掉所有指令性词汇，留下群名部分
    cleaned = content.replace("\n", " ")
    noise_words = [
        "帮我", "请", "麻烦", "总结一下", "总结", "摘要", "概括",
        "查看", "看看", "看下", "一下", "这个", "群里的", "群里",
        "群的", "群聊", "近期", "最近", "近一周", "本周", "上周",
        "消息", "信息", "内容", "聊天记录", "的",
    ]
    for word in noise_words:
        cleaned = cleaned.replace(word, " ")
    cleaned = cleaned.strip()
    # 取最长的连续非空片段
    parts = [p.strip() for p in cleaned.split() if p.strip()]
    if parts:
        longest = max(parts, key=len)
        if len(longest) >= 2:
            return longest
    return None


def detect_chat_summary(content: str) -> str | None:
    """检测是否是群聊总结请求，返回群名关键词"""
    if not CHAT_SUMMARY_KEYWORDS.search(content):
        return None
    return extract_chat_name(content)


def get_chat_summary_context(content: str) -> str:
    """检测群聊总结请求并获取消息"""
    chat_name = detect_chat_summary(content)
    if not chat_name:
        return ""

    log(f"CHAT_SUMMARY: searching for chat '{chat_name}'")
    chat_id = search_chat_by_name(chat_name)
    if not chat_id:
        return f"【群聊搜索结果】未找到名为「{chat_name}」的群聊，可能 bot 未加入该群。"

    messages = fetch_chat_messages(chat_id)
    if not messages:
        return f"【群聊「{chat_name}」】没有找到近期文本消息。"

    msg_text = "\n".join(messages)
    return f"【群聊「{chat_name}」近期消息（最新在前）】\n{msg_text}\n\n请基于以上消息内容为用户生成摘要总结。"


def get_calendar_context(content: str) -> str:
    """根据消息内容查询日历信息，返回上下文（已预处理为 UTC+8 并合并）"""
    targets = detect_calendar_query(content)
    if not targets:
        return ""

    start_date, end_date = parse_date_range_from_message(content)
    date_label = start_date if start_date == end_date else f"{start_date} ~ {end_date}"
    log(f"CALENDAR: querying {targets} for {date_label}")

    results = []
    for key in targets:
        if key == "aaron":
            # 合并 Aaron + Jackson Li 两个账号的时段
            slots_aaron = query_freebusy_raw(USERS["aaron"]["open_id"], start_date, end_date)
            slots_jackson = query_freebusy_raw(USERS["jackson"]["open_id"], start_date, end_date)
            if slots_aaron is None and slots_jackson is None:
                formatted = format_freebusy(None)
            else:
                all_slots = (slots_aaron or []) + (slots_jackson or [])
                formatted = format_freebusy(all_slots)
            results.append(f"【Aaron 在 {date_label} 的忙碌时段】\n{formatted}")
        elif key == "thomas":
            # 合并 Thomas Chang + Deric Chan 两个账号的时段
            slots_thomas = query_freebusy_raw(USERS["thomas"]["open_id"], start_date, end_date)
            slots_deric = query_freebusy_raw(USERS["deric"]["open_id"], start_date, end_date)
            if slots_thomas is None and slots_deric is None:
                formatted = format_freebusy(None)
            else:
                all_slots = (slots_thomas or []) + (slots_deric or [])
                formatted = format_freebusy(all_slots)
            results.append(f"【Thomas Chang 在 {date_label} 的忙碌时段】\n{formatted}")
        else:
            user = USERS[key]
            slots = query_freebusy_raw(user["open_id"], start_date, end_date)
            formatted = format_freebusy(slots)
            results.append(f"【{user['name']} 在 {date_label} 的忙碌时段】\n{formatted}")

    return "\n\n".join(results)


def generate_reply(content: str, sender_id: str, chat_type: str, conv_key: str) -> str:
    """调用 AWS Bedrock Claude 生成回复（Bearer Token 认证），带对话历史"""
    if not AWS_BEARER_TOKEN:
        log("ERROR: AWS_BEARER_TOKEN_BEDROCK not set")
        return "抱歉，我暂时无法处理这条消息，稍后 Ethan 会回复你。"

    # 检查是否需要群聊总结（优先级高于日历）
    chat_summary_context = get_chat_summary_context(content)
    # 群聊总结和日历查询互斥，避免误触发
    calendar_context = "" if chat_summary_context else get_calendar_context(content)

    try:
        model_path = BEDROCK_MODEL_ID.replace(":", "%3A")
        url = f"https://bedrock-runtime.{AWS_REGION}.amazonaws.com/model/{model_path}/converse"

        today = now_utc8()
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        today_info = f"今天是 {today.strftime('%Y-%m-%d')} {weekday_names[today.weekday()]}"

        system_prompt = get_system_prompt()
        system_prompt += f"\n\n## 当前时间\n{today_info}"
        if calendar_context:
            system_prompt += f"\n\n## 日历查询结果（实时数据）\n\n{calendar_context}\n\n请基于以上数据回答用户的日历相关问题。只需告知哪些时间段被占用即可，格式简洁。注意：标注星期几时请根据日期准确计算，不要猜测。"
        if chat_summary_context:
            system_prompt += f"\n\n## 群聊消息数据\n\n{chat_summary_context}"

        # 构建含历史的消息列表
        messages = list(conversation_history[conv_key])
        messages.append({"role": "user", "content": [{"text": content}]})

        max_tokens = 4096 if chat_summary_context else 1024

        payload = {
            "system": [{"text": system_prompt}],
            "messages": messages,
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0.7},
        }

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
            result = json.loads(resp.read())

        reply = result["output"]["message"]["content"][0]["text"]
        reply = reply.strip()

        # 保存对话历史
        conversation_history[conv_key].append({"role": "user", "content": [{"text": content}]})
        conversation_history[conv_key].append({"role": "assistant", "content": [{"text": reply}]})
        # 限制历史长度（每轮 2 条，保留最近 MAX_HISTORY 轮）
        if len(conversation_history[conv_key]) > MAX_HISTORY * 2:
            conversation_history[conv_key] = conversation_history[conv_key][-(MAX_HISTORY * 2):]

        return reply

    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        log(f"ERROR: Bedrock HTTP {e.code}: {body[:200]}")
        return "抱歉，我暂时无法处理这条消息，稍后 Ethan 会回复你。"
    except Exception as e:
        log(f"ERROR: Bedrock call failed: {e}")
        return "抱歉，我暂时无法处理这条消息，稍后 Ethan 会回复你。"


def send_reply(message_id: str, reply_text: str):
    """通过 lark-cli 回复消息"""
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
        model_path = BEDROCK_MODEL_ID.replace(":", "%3A")
        url = f"https://bedrock-runtime.{AWS_REGION}.amazonaws.com/model/{model_path}/converse"

        history = list(conversation_history[conv_key])
        history.append({"role": "user", "content": [{"text": content}]})

        payload = {
            "system": [{"text": "你是一个消息摘要助手。请根据对话上下文，用 1-3 句话总结对方想要转达给 Ethan 的核心内容。只输出摘要，不要加前缀或解释。如果上下文不足，就直接用原始消息内容。"}],
            "messages": history,
            "inferenceConfig": {"maxTokens": 256, "temperature": 0.3},
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {AWS_BEARER_TOKEN}",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())

        return result["output"]["message"]["content"][0]["text"].strip()
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
    source = f"群聊" if chat_type != "p2p" else "私聊"
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


# 消息去重：记录已处理的 event_id，防止重复投递
processed_events: set[str] = set()
MAX_PROCESSED_EVENTS = 1000


def process_event(event: dict):
    """处理单条消息事件"""
    # 去重：跳过已处理的事件
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

    # 跳过 bot 自己发的消息
    if BOT_OPEN_ID and sender_id == BOT_OPEN_ID:
        log(f"SKIP: message from self")
        return

    # 群聊中只回复 @bot 或包含 bot 名字的消息
    if chat_type == "group":
        mention_markers = ["@ethan assistant", "@ethanassistant", "@assistant"]
        content_lower = content.lower()
        has_mention = any(m in content_lower for m in mention_markers)
        if not has_mention:
            log(f"SKIP: group message without @bot from {sender_id}")
            return

    # 只处理文本/富文本消息
    if message_type not in ("text", "post"):
        log(f"SKIP: unsupported type={message_type} from {sender_id}")
        send_reply(message_id, "抱歉，我目前只能处理文字消息。")
        return

    if not content:
        log(f"SKIP: empty content from {sender_id}")
        return

    log(f"RECV: [{chat_type}] from={sender_id} type={message_type} msg_id={message_id} content={content[:80]}")

    # 对话上下文 key：私聊按 sender_id，群聊按 chat_id
    conv_key = sender_id if chat_type == "p2p" else chat_id

    reply = generate_reply(content, sender_id, chat_type, conv_key)

    if reply:
        # 检测 AI 是否决定转达（回复中包含 [RELAY] 标记）
        if "[RELAY]" in reply:
            reply_clean = reply.replace("[RELAY]", "").strip()
            log(f"RELAY: AI decided to relay, from {sender_id}")
            notify_ethan(sender_id, chat_type, chat_id, content, conv_key)
            send_reply(message_id, reply_clean)
        else:
            log(f"REPLY: {reply[:80]}...")
            send_reply(message_id, reply)


def main():
    log("=== Ethan Assistant started ===")
    log(f"Model: {BEDROCK_MODEL_ID}, Region: {AWS_REGION}")

    # 启动 event consume 子进程
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

    # 等待 ready marker
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

    # 优雅关闭
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

    # 持续读取 stderr（后台）以避免缓冲区满
    import threading

    def drain_stderr():
        for line in proc.stderr:
            line = line.strip()
            if line:
                log(f"[event] {line}")

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()

    # 主循环：从 stdout 读取 NDJSON 事件
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

    # 进程结束
    proc.wait()
    log(f"Event consume exited with code {proc.returncode}")
    log("=== Ethan Assistant stopped ===")
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
