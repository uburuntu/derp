# Comprehensive atomic operation: upsert user/chats and insert BotUpdate with relations
with
    # Upsert User if provided
    user_result := (
        insert telegram::User {
            user_id := <optional int64>$user_id,
            is_bot := <optional bool>$user_is_bot ?? false,
            first_name := <optional str>$user_first_name ?? '',
            last_name := <optional str>$user_last_name,
            username := <optional str>$user_username,
            language_code := <optional str>$user_language_code,
            is_premium := <optional bool>$user_is_premium ?? false,
            added_to_attachment_menu := <optional bool>$user_added_to_attachment_menu ?? false,
        }
        unless conflict on .user_id
        else (
            update telegram::User
            set {
                is_bot := <optional bool>$user_is_bot ?? false,
                first_name := <optional str>$user_first_name ?? '',
                last_name := <optional str>$user_last_name,
                username := <optional str>$user_username,
                language_code := <optional str>$user_language_code,
                is_premium := <optional bool>$user_is_premium ?? false,
                added_to_attachment_menu := <optional bool>$user_added_to_attachment_menu ?? false,
            }
        )
    ) if exists <optional int64>$user_id else <telegram::User>{},
    
    # Upsert Chat if provided
    chat_result := (
        insert telegram::Chat {
            chat_id := <optional int64>$chat_id,
            type := <optional str>$chat_type ?? '',
            title := <optional str>$chat_title,
            username := <optional str>$chat_username,
            first_name := <optional str>$chat_first_name,
            last_name := <optional str>$chat_last_name,
            is_forum := <optional bool>$chat_is_forum ?? false,
        }
        unless conflict on .chat_id
        else (
            update telegram::Chat
            set {
                type := <optional str>$chat_type ?? '',
                title := <optional str>$chat_title,
                username := <optional str>$chat_username,
                first_name := <optional str>$chat_first_name,
                last_name := <optional str>$chat_last_name,
                is_forum := <optional bool>$chat_is_forum ?? false,
            }
        )
    ) if exists <optional int64>$chat_id else <telegram::Chat>{},
    
    # Upsert Sender Chat if provided (different from main chat)
    sender_chat_result := (
        insert telegram::Chat {
            chat_id := <optional int64>$sender_chat_id,
            type := <optional str>$sender_chat_type ?? '',
            title := <optional str>$sender_chat_title,
            username := <optional str>$sender_chat_username,
            first_name := <optional str>$sender_chat_first_name,
            last_name := <optional str>$sender_chat_last_name,
            is_forum := <optional bool>$sender_chat_is_forum ?? false,
        }
        unless conflict on .chat_id
        else (
            update telegram::Chat
            set {
                type := <optional str>$sender_chat_type ?? '',
                title := <optional str>$sender_chat_title,
                username := <optional str>$sender_chat_username,
                first_name := <optional str>$sender_chat_first_name,
                last_name := <optional str>$sender_chat_last_name,
                is_forum := <optional bool>$sender_chat_is_forum ?? false,
            }
        )
    ) if exists <optional int64>$sender_chat_id else <telegram::Chat>{}

# Insert BotUpdate and return its id
select (
    insert telegram::BotUpdate {
        update_id := <int64>$update_id,
        update_type := <str>$update_type,
        raw_data := <json>$raw_data,
        handled := <bool>$handled ?? false,
        from_user := user_result if exists <optional int64>$user_id else {},
        chat := chat_result if exists <optional int64>$chat_id else {},
    }
) {
    id
}; 