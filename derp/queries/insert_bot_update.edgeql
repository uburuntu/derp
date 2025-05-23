# Insert BotUpdate
# Parameters: $update_id, $update_type, $raw_data, $user_id?, $chat_id?, $handled?
with
    user := (select telegram::User filter .user_id = <optional int64>$user_id limit 1),
    chat := (select telegram::Chat filter .chat_id = <optional int64>$chat_id limit 1)
insert telegram::BotUpdate {
    update_id := <int64>$update_id,
    update_type := <str>$update_type,
    raw_data := <json>$raw_data,
    handled := <optional bool>$handled ?? false,
    from_user := user,
    chat := chat
}; 