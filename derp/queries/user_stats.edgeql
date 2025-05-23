# Get bot users vs regular users stats
select {
    bot_users := count((select telegram::User filter .is_bot)),
    regular_users := count((select telegram::User filter not .is_bot)),
    premium_users := count((select telegram::User filter .is_premium)),
    total_users := count(telegram::User)
}; 