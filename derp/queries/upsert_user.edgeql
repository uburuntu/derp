# Upsert User (insert or update based on user_id)
# Parameters: $user_id, $is_bot, $first_name, $last_name?, $username?, $language_code?, $is_premium?, $added_to_attachment_menu?, $metadata?
select (
    insert telegram::User {
        user_id := <int64>$user_id,
        is_bot := <bool>$is_bot,
        first_name := <str>$first_name,
        last_name := <optional str>$last_name,
        username := <optional str>$username,
        language_code := <optional str>$language_code,
        is_premium := <optional bool>$is_premium ?? false,
        added_to_attachment_menu := <optional bool>$added_to_attachment_menu ?? false,
        metadata := <optional json>$metadata ?? <json>'{}'
    }
    unless conflict on .user_id
    else (
        update telegram::User
        set {
            is_bot := <bool>$is_bot,
            first_name := <str>$first_name,
            last_name := <optional str>$last_name,
            username := <optional str>$username,
            language_code := <optional str>$language_code,
            is_premium := <optional bool>$is_premium ?? false,
            added_to_attachment_menu := <optional bool>$added_to_attachment_menu ?? false,
            metadata := <optional json>$metadata ?? <json>'{}'
        }
    )
) {
    id,
    user_id,
    display_name,
    created_at,
    updated_at
}; 