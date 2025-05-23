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