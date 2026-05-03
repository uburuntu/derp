import { sql as drizzleSql } from "drizzle-orm";
import { drizzle } from "drizzle-orm/postgres-js";
import postgres from "postgres";
import * as schema from "./schema";

let db: ReturnType<typeof createDb> | null = null;
let sql: ReturnType<typeof postgres> | null = null;

export interface DatabaseReadiness {
	db: boolean;
	schema: boolean;
	error?: string;
}

const SCHEMA_CHECKS = [
	"SELECT id, telegram_id, credits, subscription_tier FROM users LIMIT 0",
	"SELECT id, telegram_id, credits, settings FROM chats LIMIT 0",
	"SELECT id, chat_id, user_id, role FROM chat_members LIMIT 0",
	"SELECT id, chat_id, user_id, telegram_message_id, telegram_date FROM messages LIMIT 0",
	"SELECT id, user_id, chat_id, amount, balance_after FROM ledger LIMIT 0",
	"SELECT id, user_id, chat_id, usage_date, usage FROM usage_quotas LIMIT 0",
	"SELECT id, chat_id, user_id, description, status, fire_at, cron_expression FROM reminders LIMIT 0",
];

function createDb(databaseUrl: string) {
	const client = postgres(databaseUrl, {
		max: 10,
		idle_timeout: 20,
		connect_timeout: 10,
	});
	sql = client;
	return drizzle(client, { schema });
}

export type Database = ReturnType<typeof createDb>;

export function getDb(databaseUrl: string): Database {
	if (!db) {
		db = createDb(databaseUrl);
	}
	return db;
}

export async function checkDatabaseReady(
	database: Database,
): Promise<DatabaseReadiness> {
	try {
		await database.execute(drizzleSql`SELECT 1`);
	} catch (error) {
		return {
			db: false,
			schema: false,
			error: getErrorMessage(error),
		};
	}

	try {
		for (const check of SCHEMA_CHECKS) {
			await database.execute(drizzleSql.raw(check));
		}
	} catch (error) {
		return {
			db: true,
			schema: false,
			error: getErrorMessage(error),
		};
	}

	return { db: true, schema: true };
}

export async function assertDatabaseReady(database: Database): Promise<void> {
	const readiness = await checkDatabaseReady(database);
	if (!readiness.db || !readiness.schema) {
		throw new Error(readiness.error ?? "database_not_ready");
	}
}

export async function closeDb(): Promise<void> {
	if (sql) {
		await sql.end();
		sql = null;
		db = null;
	}
}

function getErrorMessage(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}
