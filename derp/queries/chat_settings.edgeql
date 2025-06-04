select telegram::Chat {
    llm_memory,
} filter .chat_id = <int64>$chat_id; 