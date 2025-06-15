# Update BotUpdate handled status by id
update telegram::BotUpdate
filter .id = <uuid>$bot_update_id
set {
    handled := <bool>$handled
}; 