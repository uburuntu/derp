# Select ActiveUpdates per chat_id, sorted in descending order (most recent first)
# Parameters: $chat_id, $limit
select telegram::ActiveBotUpdates {
    id,
    update_id,
    update_type,
    raw_data,
    handled,
    created_at,
    expires_at,
    from_user: {
        user_id,
        display_name,
        is_bot
    },
    chat: {
        chat_id,
        display_name,
        type
    }
}
filter .chat.chat_id = <int64>$chat_id
order by .created_at desc
limit <int64>$limit; 