CREATE INDEX "messages_chat_thread_date_idx" ON "messages" USING btree ("chat_id","thread_id","telegram_date");--> statement-breakpoint
CREATE INDEX "reminders_status_fire_at_idx" ON "reminders" USING btree ("status","fire_at");--> statement-breakpoint
CREATE INDEX "reminders_chat_status_idx" ON "reminders" USING btree ("chat_id","status");--> statement-breakpoint
CREATE INDEX "reminders_user_status_recurring_idx" ON "reminders" USING btree ("user_id","status","is_recurring");