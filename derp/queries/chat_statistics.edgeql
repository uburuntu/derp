# Get chat statistics (update counts by type)
# Parameters: $chat_id
select telegram::Chat {
    chat_id,
    display_name,
    total_updates := count(.<chat[is telegram::BotUpdate]),
    active_updates := count(.<chat[is telegram::ActiveBotUpdates]),
    handled_updates := count((select .<chat[is telegram::BotUpdate] filter .handled)),
    unhandled_updates := count((select .<chat[is telegram::BotUpdate] filter not .handled))
}
filter .chat_id = <int64>$chat_id; 