import { drizzle } from "drizzle-orm/postgres-js";
import postgres from "postgres";
import * as schema from "./schema";

let db: ReturnType<typeof createDb> | null = null;
let sql: ReturnType<typeof postgres> | null = null;

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

export async function closeDb(): Promise<void> {
	if (sql) {
		await sql.end();
		sql = null;
		db = null;
	}
}
