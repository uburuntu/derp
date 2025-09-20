# Upsert a cleaned message into telegram::MessageLog using message_key

with
  chat := assert_single((
    select telegram::Chat filter .chat_id = <int64>$chat_id
  )),
  from_user := (
    select telegram::User filter .user_id = <optional int64>$from_user_id
  ),
  src_update := (
    select telegram::BotUpdate
    filter .update_id = <optional int64>$source_update_id
    order by .created_at desc
    limit 1
  )
select (
  insert telegram::MessageLog {
    message_key := <str>$message_key,
    direction := <str>$direction,
    message_id := <int64>$message_id,
    thread_id := <optional int64>$thread_id,
    chat := chat,
    from_user := from_user ?? <telegram::User>{},
    content_type := <str>$content_type,
    text := <optional str>$text,
    media_group_id := <optional str>$media_group_id,
    attachment_type := <optional str>$attachment_type,
    attachment_file_id := <optional str>$attachment_file_id,
    reply_to_message_id := <optional int64>$reply_to_message_id,
    tg_date := <datetime>$tg_date,
    edited_at := <optional datetime>$edited_at,
    source_update := src_update ?? <telegram::BotUpdate>{},
  }
  unless conflict on .message_key
  else (
    update telegram::MessageLog
    set {
      direction := <str>$direction,
      content_type := <str>$content_type,
      text := <optional str>$text if exists <optional str>$text else __subject__.text,
      media_group_id := <optional str>$media_group_id if exists <optional str>$media_group_id else __subject__.media_group_id,
      attachment_type := <optional str>$attachment_type if exists <optional str>$attachment_type else __subject__.attachment_type,
      attachment_file_id := <optional str>$attachment_file_id if exists <optional str>$attachment_file_id else __subject__.attachment_file_id,
      reply_to_message_id := <optional int64>$reply_to_message_id if exists <optional int64>$reply_to_message_id else __subject__.reply_to_message_id,
      edited_at := <optional datetime>$edited_at if exists <optional datetime>$edited_at else __subject__.edited_at,
      source_update := src_update ?? __subject__.source_update,
    }
  )
) { id };
