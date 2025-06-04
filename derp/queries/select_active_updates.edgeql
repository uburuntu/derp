# Select the most recent ActiveUpdates per chat_id, then sort them in ascending order (oldest first)
with recent_updates := (
    select telegram::ActiveBotUpdates
    filter .chat.chat_id = <int64>$chat_id
    order by .created_at desc
    limit <int64>$limit
)
select recent_updates {
    raw_data,
}
order by .created_at asc;
