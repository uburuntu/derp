# Upsert Chat (insert or update based on chat_id)
# Parameters: $chat_id, $type, $title?, $username?, $first_name?, $last_name?, $is_forum?, $metadata?
select (
    insert telegram::Chat {
        chat_id := <int64>$chat_id,
        type := <str>$type,
        title := <optional str>$title,
        username := <optional str>$username,
        first_name := <optional str>$first_name,
        last_name := <optional str>$last_name,
        is_forum := <optional bool>$is_forum ?? false,
        metadata := <optional json>$metadata ?? <json>'{}'
    }
    unless conflict on .chat_id
    else (
        update telegram::Chat
        set {
            type := <str>$type,
            title := <optional str>$title,
            username := <optional str>$username,
            first_name := <optional str>$first_name,
            last_name := <optional str>$last_name,
            is_forum := <optional bool>$is_forum ?? false,
            metadata := <optional json>$metadata ?? <json>'{}'
        }
    )
) {
    id,
    chat_id,
    display_name,
    type,
    created_at,
    updated_at
}; 