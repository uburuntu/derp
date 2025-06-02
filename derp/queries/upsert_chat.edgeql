# Upsert Chat (insert or update based on chat_id)
insert telegram::Chat {
    chat_id := <int64>$chat_id,
    type := <str>$type,
    title := <optional str>$title,
    username := <optional str>$username,
    first_name := <optional str>$first_name,
    last_name := <optional str>$last_name,
    is_forum := <optional bool>$is_forum ?? false,
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
    }
);
