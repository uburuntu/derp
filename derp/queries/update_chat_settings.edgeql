update telegram::Chat
filter .chat_id = <int64>$chat_id
set {
    llm_memory := <optional str>$llm_memory
}; 