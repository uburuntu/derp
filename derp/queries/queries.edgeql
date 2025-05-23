# ============================================================================
# INSERT QUERIES
# ============================================================================

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

# ============================================================================
# UPSERT QUERIES  
# ============================================================================

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

# ============================================================================
# SELECT QUERIES
# ============================================================================

# Select ActiveUpdates per chat_id, sorted in ascending order
# Parameters: $chat_id
select telegram::ActiveBotUpdates {
    id,
    update_id,
    update_type,
    raw_data,
    handled,
    created_at,
    expires_at,
    from_user: {
        user_id,
        display_name,
        is_bot
    },
    chat: {
        chat_id,
        display_name,
        type
    }
}
filter .chat.chat_id = <int64>$chat_id
order by .update_id asc;

# ============================================================================
# ADDITIONAL USEFUL QUERIES
# ============================================================================

# Get chat statistics (update counts by type)
# Parameters: $chat_id
select telegram::Chat {
    chat_id,
    display_name,
    total_updates := count(.<chat[is telegram::BotUpdate]),
    active_updates := count(.<chat[is telegram::ActiveBotUpdates]),
    handled_updates := count((select .<chat[is telegram::BotUpdate] filter .handled)),
    unhandled_updates := count((select .<chat[is telegram::BotUpdate] filter not .handled))
}
filter .chat_id = <int64>$chat_id;

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

# Cleanup expired updates (returns count of deleted updates)
select count((delete telegram::ExpiredBotUpdates));

# Get recent activity across all chats (last 24 hours)
# Parameters: none
with cutoff_time := datetime_of_statement() - <cal::relative_duration>'24 hours'
select telegram::ActiveBotUpdates {
    id,
    update_id,
    update_type,
    created_at,
    chat: {
        chat_id,
        display_name
    },
    from_user: {
        user_id,
        display_name
    }
}
filter .created_at >= cutoff_time
order by .created_at desc
limit 100;

# Get chat leaderboard (most active chats by update count)
select telegram::Chat {
    chat_id,
    display_name,
    type,
    update_count := count(.<chat[is telegram::ActiveBotUpdates]),
    last_activity := max(.<chat[is telegram::BotUpdate].created_at)
}
filter exists .<chat[is telegram::BotUpdate]
order by .update_count desc
limit 20;

# Get updates by type and time range
# Parameters: $update_type, $start_time?, $end_time?
select telegram::ActiveBotUpdates {
    id,
    update_id,
    update_type,
    created_at,
    handled,
    chat: {
        chat_id,
        display_name
    },
    from_user: {
        user_id,
        display_name
    }
}
filter 
    .update_type = <str>$update_type and
    .created_at >= (<optional datetime>$start_time ?? datetime_of_statement() - <cal::relative_duration>'7 days') and
    .created_at <= (<optional datetime>$end_time ?? datetime_of_statement())
order by .created_at desc;

# Get bot users vs regular users stats
select {
    bot_users := count((select telegram::User filter .is_bot)),
    regular_users := count((select telegram::User filter not .is_bot)),
    premium_users := count((select telegram::User filter .is_premium)),
    total_users := count(telegram::User)
}; 