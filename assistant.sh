#!/bin/bash
# Ethan Assistant — 飞书对话服务
# 监听飞书消息并用 Claude 生成回复

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$SCRIPT_DIR/assistant.log"
SYSTEM_PROMPT_FILE="$SCRIPT_DIR/system_prompt.txt"

LARK_CLI="/Users/ethanhuang/.local/node-v24.15.0-darwin-arm64/bin/lark-cli"
CLAUDE_CLI="/Users/ethanhuang/.local/bin/claude"

# bot 自身的 open_id，避免回复自己发的消息
BOT_OPEN_ID="${BOT_OPEN_ID:-}"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

get_system_prompt() {
    if [[ -f "$SYSTEM_PROMPT_FILE" ]]; then
        cat "$SYSTEM_PROMPT_FILE"
    else
        echo "你是 Ethan Huang 的 AI 助理。请用专业友善的语气回复消息。如果不确定如何回答，可以告知对方你会转达给 Ethan。回复请简洁明了。"
    fi
}

generate_reply() {
    local message_content="$1"
    local sender_id="$2"
    local chat_type="$3"
    local system_prompt
    system_prompt="$(get_system_prompt)"

    local user_prompt="$message_content"

    # 用 claude CLI 生成回复
    local reply
    reply=$("$CLAUDE_CLI" -p "$user_prompt" --system-prompt "$system_prompt" --max-tokens 1024 2>/dev/null) || {
        log "ERROR: claude CLI failed for message from $sender_id"
        echo "抱歉，我暂时无法处理这条消息，稍后 Ethan 会回复你。"
        return
    }

    echo "$reply"
}

send_reply() {
    local message_id="$1"
    local reply_text="$2"

    "$LARK_CLI" im +messages-reply \
        --message-id "$message_id" \
        --text "$reply_text" \
        --as bot 2>> "$LOG_FILE"
}

process_event() {
    local event_json="$1"

    local sender_id chat_type message_type content message_id
    sender_id=$(echo "$event_json" | jq -r '.sender_id // empty')
    chat_type=$(echo "$event_json" | jq -r '.chat_type // empty')
    message_type=$(echo "$event_json" | jq -r '.message_type // empty')
    content=$(echo "$event_json" | jq -r '.content // empty')
    message_id=$(echo "$event_json" | jq -r '.message_id // empty')

    # 跳过 bot 自己发的消息
    if [[ -n "$BOT_OPEN_ID" && "$sender_id" == "$BOT_OPEN_ID" ]]; then
        log "SKIP: message from self (bot)"
        return
    fi

    # 目前只处理文本消息
    if [[ "$message_type" != "text" && "$message_type" != "post" ]]; then
        log "SKIP: unsupported message_type=$message_type from $sender_id"
        send_reply "$message_id" "抱歉，我目前只能处理文字消息。"
        return
    fi

    if [[ -z "$content" ]]; then
        log "SKIP: empty content from $sender_id"
        return
    fi

    log "RECV: [$chat_type] from=$sender_id type=$message_type msg_id=$message_id content=$(echo "$content" | head -c 100)"

    # 生成 AI 回复
    local reply
    reply=$(generate_reply "$content" "$sender_id" "$chat_type")

    if [[ -n "$reply" ]]; then
        log "REPLY: msg_id=$message_id reply=$(echo "$reply" | head -c 100)..."
        send_reply "$message_id" "$reply"
    fi
}

main() {
    log "=== Ethan Assistant started ==="
    log "Listening for messages..."

    # 使用 event consume 监听消息，通过 tail -f /dev/null 保持 stdin 不关闭
    "$LARK_CLI" event consume im.message.receive_v1 --as bot < <(tail -f /dev/null) 2>> "$LOG_FILE" | while IFS= read -r line; do
        if [[ -z "$line" ]]; then
            continue
        fi

        # 验证是有效 JSON
        if ! echo "$line" | jq empty 2>/dev/null; then
            log "WARN: non-JSON line: $(echo "$line" | head -c 80)"
            continue
        fi

        process_event "$line" &
    done

    log "=== Ethan Assistant stopped ==="
}

main "$@"
