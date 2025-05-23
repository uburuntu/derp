# Get chat leaderboard (most active chats by update count)
select telegram::Chat {
    chat_id,
    display_name,
    type,
    update_count := count(.<chat[is telegram::ActiveBotUpdates]),
    last_activity := max(.<chat[is telegram::BotUpdate].created_at)
}
filter exists .<chat[is telegram::BotUpdate]
order by .update_count desc
limit 20; 