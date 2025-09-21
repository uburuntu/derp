# Pending until schema with telegram::MessageLog is applied
# Select recent cleaned messages for a chat from MessageLog
#
# Intent: pick the latest N records by update/create time (and message_id as
# tiebreaker), then reverse them so the final result is chronological (oldest
# first). This ensures the limit applies to the most recent updates while the
# consumer receives them in forward-reading order.
with selected := (
    select telegram::MessageLog
    filter .chat.chat_id = <int64>$chat_id and not .is_deleted
    order by .created_at desc then .message_id desc
    limit <int64>$limit
)
select selected {
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
order by selected.created_at asc then selected.message_id asc;
