# Get user activity summary
# Parameters: $user_id
select telegram::User {
    user_id,
    display_name,
    total_updates := count(.updates),
    active_updates := count((select .updates filter not .is_expired)),
    recent_chats := (
        select distinct (.updates.chat) {
            chat_id,
            display_name,
            last_activity := max(.<chat[is telegram::BotUpdate].created_at)
        }
        order by .last_activity desc
        limit 10
    )
}
filter .user_id = <int64>$user_id; 