# Cleanup expired updates (returns count of deleted updates)
select count((delete telegram::ExpiredBotUpdates)); 