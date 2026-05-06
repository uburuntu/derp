CREATE TABLE "payment_receipts" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"chat_id" uuid,
	"telegram_charge_id" varchar(255) NOT NULL,
	"provider_charge_id" varchar(255),
	"invoice_payload" varchar(255),
	"currency" varchar(10) DEFAULT 'XTR' NOT NULL,
	"stars" integer NOT NULL,
	"product_type" varchar(30) NOT NULL,
	"product_id" varchar(100),
	"credit_target" varchar(20),
	"credits" integer DEFAULT 0 NOT NULL,
	"status" varchar(20) DEFAULT 'paid' NOT NULL,
	"refunded_at" timestamp with time zone,
	"meta" jsonb,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "subscription_periods" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"payment_id" uuid NOT NULL,
	"telegram_charge_id" varchar(255) NOT NULL,
	"plan_id" varchar(50) NOT NULL,
	"credits" integer NOT NULL,
	"starts_at" timestamp with time zone DEFAULT now() NOT NULL,
	"expires_at" timestamp with time zone NOT NULL,
	"status" varchar(20) DEFAULT 'active' NOT NULL,
	"refunded_at" timestamp with time zone,
	"meta" jsonb,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
ALTER TABLE "payment_receipts" ADD CONSTRAINT "payment_receipts_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "payment_receipts" ADD CONSTRAINT "payment_receipts_chat_id_chats_id_fk" FOREIGN KEY ("chat_id") REFERENCES "public"."chats"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "subscription_periods" ADD CONSTRAINT "subscription_periods_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "subscription_periods" ADD CONSTRAINT "subscription_periods_payment_id_payment_receipts_id_fk" FOREIGN KEY ("payment_id") REFERENCES "public"."payment_receipts"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
CREATE UNIQUE INDEX "payment_receipts_telegram_charge_unique" ON "payment_receipts" USING btree ("telegram_charge_id");--> statement-breakpoint
CREATE INDEX "payment_receipts_user_id_idx" ON "payment_receipts" USING btree ("user_id");--> statement-breakpoint
CREATE INDEX "payment_receipts_chat_id_idx" ON "payment_receipts" USING btree ("chat_id");--> statement-breakpoint
CREATE INDEX "payment_receipts_status_idx" ON "payment_receipts" USING btree ("status");--> statement-breakpoint
CREATE UNIQUE INDEX "subscription_periods_charge_unique" ON "subscription_periods" USING btree ("telegram_charge_id");--> statement-breakpoint
CREATE INDEX "subscription_periods_user_status_expiry_idx" ON "subscription_periods" USING btree ("user_id","status","expires_at");