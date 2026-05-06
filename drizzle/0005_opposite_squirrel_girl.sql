CREATE TABLE "credit_debt_events" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"debt_id" uuid NOT NULL,
	"user_id" uuid NOT NULL,
	"chat_id" uuid,
	"ledger_id" uuid,
	"payment_receipt_id" uuid,
	"type" varchar(20) NOT NULL,
	"amount" integer NOT NULL,
	"meta" jsonb,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "credit_debts" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"chat_id" uuid,
	"payment_receipt_id" uuid,
	"telegram_charge_id" varchar(255),
	"target" varchar(20) NOT NULL,
	"status" varchar(20) DEFAULT 'open' NOT NULL,
	"amount" integer NOT NULL,
	"recovered_amount" integer DEFAULT 0 NOT NULL,
	"outstanding_amount" integer NOT NULL,
	"meta" jsonb,
	"settled_at" timestamp with time zone,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "credit_debts_amount_check" CHECK ("credit_debts"."amount" > 0),
	CONSTRAINT "credit_debts_outstanding_check" CHECK ("credit_debts"."outstanding_amount" >= 0)
);
--> statement-breakpoint
CREATE TABLE "pending_tool_confirmations" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"chat_id" uuid NOT NULL,
	"telegram_chat_id" bigint NOT NULL,
	"message_id" integer,
	"thread_id" integer,
	"tool_name" varchar(50) NOT NULL,
	"params" jsonb NOT NULL,
	"cost" integer NOT NULL,
	"source" varchar(20) NOT NULL,
	"status" varchar(20) DEFAULT 'pending' NOT NULL,
	"idempotency_key" varchar(255) NOT NULL,
	"expires_at" timestamp with time zone NOT NULL,
	"meta" jsonb,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "pending_tool_confirmations_key_unique" UNIQUE("idempotency_key")
);
--> statement-breakpoint
CREATE TABLE "provider_calls" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"logical_request_key" varchar(255),
	"attempt_no" integer DEFAULT 1 NOT NULL,
	"provider" varchar(40) NOT NULL,
	"operation" varchar(40) NOT NULL,
	"route" varchar(40) DEFAULT 'primary' NOT NULL,
	"key_class" varchar(20) DEFAULT 'free' NOT NULL,
	"model_id" varchar(150) NOT NULL,
	"actual_model_id" varchar(150),
	"user_id" uuid,
	"chat_id" uuid,
	"ledger_id" uuid,
	"message_id" uuid,
	"tool_name" varchar(50),
	"status" varchar(30) DEFAULT 'started' NOT NULL,
	"input_tokens" integer DEFAULT 0 NOT NULL,
	"output_tokens" integer DEFAULT 0 NOT NULL,
	"cache_hit_tokens" integer DEFAULT 0 NOT NULL,
	"media_input_count" integer DEFAULT 0 NOT NULL,
	"media_output_count" integer DEFAULT 0 NOT NULL,
	"duration_seconds" integer,
	"estimated_cost_micros" integer DEFAULT 0 NOT NULL,
	"actual_cost_micros" integer DEFAULT 0 NOT NULL,
	"credits_charged" integer DEFAULT 0 NOT NULL,
	"credit_source" varchar(20),
	"provider_request_id" varchar(255),
	"finish_reason" varchar(100),
	"error_code" varchar(100),
	"error_message" varchar(500),
	"meta" jsonb,
	"started_at" timestamp with time zone DEFAULT now() NOT NULL,
	"finished_at" timestamp with time zone,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "provider_calls_logical_attempt_unique" UNIQUE("logical_request_key","attempt_no")
);
--> statement-breakpoint
CREATE TABLE "quota_windows" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"scope" varchar(40) NOT NULL,
	"subject_key" varchar(120) NOT NULL,
	"user_id" uuid,
	"chat_id" uuid,
	"window_key" varchar(40) NOT NULL,
	"used" integer DEFAULT 0 NOT NULL,
	"limit" integer NOT NULL,
	"meta" jsonb,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "quota_windows_scope_user_chat_window_unique" UNIQUE("scope","subject_key","window_key")
);
--> statement-breakpoint
ALTER TABLE "payment_receipts" ALTER COLUMN "status" SET DATA TYPE varchar(30);--> statement-breakpoint
ALTER TABLE "payment_receipts" ALTER COLUMN "status" SET DEFAULT 'settled';--> statement-breakpoint
ALTER TABLE "payment_receipts" ADD COLUMN "settled_at" timestamp with time zone;--> statement-breakpoint
ALTER TABLE "credit_debt_events" ADD CONSTRAINT "credit_debt_events_debt_id_credit_debts_id_fk" FOREIGN KEY ("debt_id") REFERENCES "public"."credit_debts"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "credit_debt_events" ADD CONSTRAINT "credit_debt_events_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "credit_debt_events" ADD CONSTRAINT "credit_debt_events_chat_id_chats_id_fk" FOREIGN KEY ("chat_id") REFERENCES "public"."chats"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "credit_debt_events" ADD CONSTRAINT "credit_debt_events_ledger_id_ledger_id_fk" FOREIGN KEY ("ledger_id") REFERENCES "public"."ledger"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "credit_debt_events" ADD CONSTRAINT "credit_debt_events_payment_receipt_id_payment_receipts_id_fk" FOREIGN KEY ("payment_receipt_id") REFERENCES "public"."payment_receipts"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "credit_debts" ADD CONSTRAINT "credit_debts_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "credit_debts" ADD CONSTRAINT "credit_debts_chat_id_chats_id_fk" FOREIGN KEY ("chat_id") REFERENCES "public"."chats"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "credit_debts" ADD CONSTRAINT "credit_debts_payment_receipt_id_payment_receipts_id_fk" FOREIGN KEY ("payment_receipt_id") REFERENCES "public"."payment_receipts"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "pending_tool_confirmations" ADD CONSTRAINT "pending_tool_confirmations_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "pending_tool_confirmations" ADD CONSTRAINT "pending_tool_confirmations_chat_id_chats_id_fk" FOREIGN KEY ("chat_id") REFERENCES "public"."chats"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "provider_calls" ADD CONSTRAINT "provider_calls_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "provider_calls" ADD CONSTRAINT "provider_calls_chat_id_chats_id_fk" FOREIGN KEY ("chat_id") REFERENCES "public"."chats"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "provider_calls" ADD CONSTRAINT "provider_calls_ledger_id_ledger_id_fk" FOREIGN KEY ("ledger_id") REFERENCES "public"."ledger"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "provider_calls" ADD CONSTRAINT "provider_calls_message_id_messages_id_fk" FOREIGN KEY ("message_id") REFERENCES "public"."messages"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "quota_windows" ADD CONSTRAINT "quota_windows_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "quota_windows" ADD CONSTRAINT "quota_windows_chat_id_chats_id_fk" FOREIGN KEY ("chat_id") REFERENCES "public"."chats"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
CREATE INDEX "credit_debt_events_debt_id_idx" ON "credit_debt_events" USING btree ("debt_id");--> statement-breakpoint
CREATE INDEX "credit_debt_events_user_id_idx" ON "credit_debt_events" USING btree ("user_id");--> statement-breakpoint
CREATE INDEX "credit_debt_events_chat_id_idx" ON "credit_debt_events" USING btree ("chat_id");--> statement-breakpoint
CREATE INDEX "credit_debts_user_status_idx" ON "credit_debts" USING btree ("user_id","status");--> statement-breakpoint
CREATE INDEX "credit_debts_chat_status_idx" ON "credit_debts" USING btree ("chat_id","status");--> statement-breakpoint
CREATE INDEX "credit_debts_charge_idx" ON "credit_debts" USING btree ("telegram_charge_id");--> statement-breakpoint
CREATE INDEX "pending_tool_confirmations_user_status_idx" ON "pending_tool_confirmations" USING btree ("user_id","status");--> statement-breakpoint
CREATE INDEX "pending_tool_confirmations_chat_status_idx" ON "pending_tool_confirmations" USING btree ("chat_id","status");--> statement-breakpoint
CREATE INDEX "pending_tool_confirmations_expires_idx" ON "pending_tool_confirmations" USING btree ("expires_at");--> statement-breakpoint
CREATE INDEX "provider_calls_user_id_idx" ON "provider_calls" USING btree ("user_id");--> statement-breakpoint
CREATE INDEX "provider_calls_chat_id_idx" ON "provider_calls" USING btree ("chat_id");--> statement-breakpoint
CREATE INDEX "provider_calls_provider_model_idx" ON "provider_calls" USING btree ("provider","model_id");--> statement-breakpoint
CREATE INDEX "provider_calls_status_idx" ON "provider_calls" USING btree ("status");--> statement-breakpoint
CREATE INDEX "provider_calls_created_at_idx" ON "provider_calls" USING btree ("created_at");--> statement-breakpoint
CREATE INDEX "quota_windows_scope_window_idx" ON "quota_windows" USING btree ("scope","window_key");--> statement-breakpoint
CREATE INDEX "quota_windows_subject_idx" ON "quota_windows" USING btree ("subject_key");--> statement-breakpoint
CREATE INDEX "quota_windows_user_scope_idx" ON "quota_windows" USING btree ("user_id","scope");