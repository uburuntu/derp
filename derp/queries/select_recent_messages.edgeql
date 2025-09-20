# Pending until schema with telegram::MessageLog is applied
# Select recent cleaned messages for a chat from MessageLog
select telegram::MessageLog {
    direction,
    message_id,
    tg_date,
    edited_at,
    content_type,
    text,
    reply_to_message_id,
    media_group_id,
    attachment_type,
    attachment_file_id,
    from_user: {
        user_id,
        display_name,
        username,
    },
    chat: { chat_id },
}
filter .chat.chat_id = <int64>$chat_id and not .is_deleted
order by (.edited_at ?? .updated_at ?? .created_at) asc then .message_id desc
limit <int64>$limit;
