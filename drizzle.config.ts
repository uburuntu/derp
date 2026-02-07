import type { defineConfig } from "drizzle-kit";

export default {
	schema: "./src/db/schema.ts",
	out: "./src/db/migrations",
	dialect: "postgresql",
	dbCredentials: {
		url: process.env.DATABASE_URL || "postgresql://derp:derp@localhost:5433/derp",
	},
} satisfies ReturnType<typeof defineConfig>;
