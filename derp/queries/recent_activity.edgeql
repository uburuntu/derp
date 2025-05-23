# Get recent activity across all chats (last 24 hours)
# Parameters: none
with cutoff_time := datetime_of_statement() - <cal::relative_duration>'24 hours'
select telegram::ActiveBotUpdates {
    id,
    update_id,
    update_type,
    created_at,
    chat: {
        chat_id,
        display_name
    },
    from_user: {
        user_id,
        display_name
    }
}
filter .created_at >= cutoff_time
order by .created_at desc
limit 100; 