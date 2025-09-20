# Mark a message as deleted in telegram::MessageLog by message_key

update telegram::MessageLog
filter .message_key = <str>$message_key
set { deleted_at := <datetime>$when };
