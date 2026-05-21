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
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
SYSTEM_PROMPT_FILE = SCRIPT_DIR / "system_prompt.txt"

LARK_CLI = os.environ.get("LARK_CLI", "lark-cli")
BOT_OPEN_ID = os.environ.get("BOT_OPEN_ID", "")

# AWS Bedrock 配置
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0")
AWS_BEARER_TOKEN = os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "")

# 用户 ID 映射
USERS = {
    "ethan": {"name": "Ethan Huang", "open_id": "ou_698c308d80f763548aea6ac4d09366ea"},
    "aaron": {"name": "Aaron", "open_id": "ou_a34ef34252262d466f5b7b5ede682293"},
    "jackson": {"name": "Jackson Li", "open_id": "ou_e6aa709de5c54635c209414d527eab1d"},
}

# 对话历史：按 chat_id（群聊）或 sender_id（私聊）维护上下文
MAX_HISTORY = 10  # 保留最近 10 轮对话
conversation_history: dict[str, list] = defaultdict(list)

CALENDAR_KEYWORDS = re.compile(
    r"(日历|日程|calendar|schedule|忙|闲|空闲|有空|meeting|会议|安排|行程|freebusy|忙闲|时间)",
    re.IGNORECASE,
)

# Aaron = Aaron + Jackson Li 合并视为一个人
PERSON_PATTERNS = {
    "ethan": re.compile(r"(ethan|我的|你的|老板)", re.IGNORECASE),
    "aaron": re.compile(r"(aaron|jackson|li)", re.IGNORECASE),
}


def log(msg: str):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def get_system_prompt() -> str:
    if SYSTEM_PROMPT_FILE.exists():
        return SYSTEM_PROMPT_FILE.read_text().strip()
    return "你是 Ethan Huang 的 AI 助理。请用专业友善的语气回复消息。如果不确定如何回答，可以告知对方你会转达给 Ethan。回复请简洁明了。"


def query_freebusy(user_id: str, date_str: str = None) -> str:
    """查询指定用户的忙闲信息"""
    cmd = [LARK_CLI, "calendar", "+freebusy", "--user-id", user_id, "--as", "bot"]
    if date_str:
        cmd.extend(["--start", date_str, "--end", date_str])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log(f"ERROR: freebusy query failed: {result.stderr}")
            return f"查询失败: {result.stderr[:100]}"
        return result.stdout.strip()
    except Exception as e:
        log(f"ERROR: freebusy exception: {e}")
        return f"查询异常: {e}"


def parse_date_from_message(content: str) -> str:
    """从消息中提取日期，默认今天"""
    today = datetime.now()

    if re.search(r"明天|明日|tomorrow", content, re.IGNORECASE):
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    if re.search(r"后天", content):
        return (today + timedelta(days=2)).strftime("%Y-%m-%d")
    if re.search(r"昨天|昨日|yesterday", content, re.IGNORECASE):
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")

    # 匹配周几
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
        return (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    # 匹配具体日期 YYYY-MM-DD 或 MM-DD
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", content)
    if m:
        return m.group(0)
    m = re.search(r"(\d{1,2})[月/](\d{1,2})[日号]?", content)
    if m:
        return f"{today.year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"

    return today.strftime("%Y-%m-%d")


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


def get_calendar_context(content: str) -> str:
    """根据消息内容查询日历信息，返回上下文"""
    targets = detect_calendar_query(content)
    if not targets:
        return ""

    date_str = parse_date_from_message(content)
    log(f"CALENDAR: querying {targets} for {date_str}")

    results = []
    for key in targets:
        if key == "aaron":
            # Aaron = Aaron + Jackson Li 合并查询
            freebusy_aaron = query_freebusy(USERS["aaron"]["open_id"], date_str)
            freebusy_jackson = query_freebusy(USERS["jackson"]["open_id"], date_str)
            results.append(
                f"【Aaron 在 {date_str} 的忙碌时段】\n"
                f"(Aaron 账号): {freebusy_aaron}\n"
                f"(Jackson Li 账号): {freebusy_jackson}\n"
                f"注意：以上两个账号都是 Aaron 的，需要合并看所有忙碌时段。"
            )
        else:
            user = USERS[key]
            freebusy = query_freebusy(user["open_id"], date_str)
            results.append(f"【{user['name']} 在 {date_str} 的忙碌时段】\n{freebusy}")

    return "\n\n".join(results)


def generate_reply(content: str, sender_id: str, chat_type: str, conv_key: str) -> str:
    """调用 AWS Bedrock Claude 生成回复（Bearer Token 认证），带对话历史"""
    if not AWS_BEARER_TOKEN:
        log("ERROR: AWS_BEARER_TOKEN_BEDROCK not set")
        return "抱歉，我暂时无法处理这条消息，稍后 Ethan 会回复你。"

    # 检查是否需要日历上下文
    calendar_context = get_calendar_context(content)

    try:
        model_path = BEDROCK_MODEL_ID.replace(":", "%3A")
        url = f"https://bedrock-runtime.{AWS_REGION}.amazonaws.com/model/{model_path}/converse"

        system_prompt = get_system_prompt()
        if calendar_context:
            system_prompt += f"\n\n## 日历查询结果（实时数据）\n\n{calendar_context}\n\n请基于以上数据回答用户的日历相关问题。只需告知哪些时间段被占用即可，格式简洁。"

        # 构建含历史的消息列表
        messages = list(conversation_history[conv_key])
        messages.append({"role": "user", "content": [{"text": content}]})

        payload = {
            "system": [{"text": system_prompt}],
            "messages": messages,
            "inferenceConfig": {"maxTokens": 1024, "temperature": 0.7},
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


def process_event(event: dict):
    """处理单条消息事件"""
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
        proc.stdin.close()
        proc.wait(timeout=10)
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


if __name__ == "__main__":
    main()
