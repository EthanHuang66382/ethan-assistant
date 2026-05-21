#!/usr/bin/env python3
"""Ethan Assistant — 飞书对话服务

监听飞书消息，用 AWS Bedrock Claude 生成回复。
"""

import json
import os
import subprocess
import sys
import signal
import time
from pathlib import Path

import boto3

SCRIPT_DIR = Path(__file__).parent
SYSTEM_PROMPT_FILE = SCRIPT_DIR / "system_prompt.txt"

LARK_CLI = os.environ.get("LARK_CLI", "lark-cli")
BOT_OPEN_ID = os.environ.get("BOT_OPEN_ID", "")

# AWS Bedrock 配置
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-sonnet-4-20250514")


def log(msg: str):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def get_system_prompt() -> str:
    if SYSTEM_PROMPT_FILE.exists():
        return SYSTEM_PROMPT_FILE.read_text().strip()
    return "你是 Ethan Huang 的 AI 助理。请用专业友善的语气回复消息。如果不确定如何回答，可以告知对方你会转达给 Ethan。回复请简洁明了。"


def generate_reply(content: str, sender_id: str, chat_type: str) -> str:
    """调用 AWS Bedrock Claude 生成回复"""
    try:
        client = boto3.client("bedrock-runtime", region_name=AWS_REGION)

        response = client.converse(
            modelId=BEDROCK_MODEL_ID,
            system=[{"text": get_system_prompt()}],
            messages=[
                {"role": "user", "content": [{"text": content}]}
            ],
            inferenceConfig={"maxTokens": 1024, "temperature": 0.7},
        )

        reply = response["output"]["message"]["content"][0]["text"]
        return reply.strip()

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

    reply = generate_reply(content, sender_id, chat_type)

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
