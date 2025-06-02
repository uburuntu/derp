# Upsert User (insert or update based on user_id)
insert telegram::User {
    user_id := <int64>$user_id,
    is_bot := <bool>$is_bot,
    first_name := <str>$first_name,
    last_name := <optional str>$last_name,
    username := <optional str>$username,
    language_code := <optional str>$language_code,
    is_premium := <optional bool>$is_premium ?? false,
    added_to_attachment_menu := <optional bool>$added_to_attachment_menu ?? false,
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
    }
); 
