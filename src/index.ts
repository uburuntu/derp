/** Entry point — config validation, DB init, bot creation, runner start */

import { run } from "@grammyjs/runner";
import { createBot, registerCommands } from "./bot/bot";
import { initAdminNotify, notifyAdmins } from "./common/admin-notify";
import { markReady, startHealthServer } from "./common/health";
import {
	initObservability,
	logger,
	shutdownObservability,
} from "./common/observability";
import { config } from "./config";
import { closeDb, getDb } from "./db/connection";
import { startScheduler, stopScheduler } from "./scheduler/scheduler";

// ── Startup ─────────────────────────────────────────────────────────────────

async function main() {
	// Initialize observability FIRST
	initObservability(config);

	// Start health check server immediately (so probes work during startup)
	startHealthServer();

	logger.info("startup", {
		environment: config.environment,
		bot: `@${config.botUsername}`,
	});

	// Initialize database
	const db = getDb(config.databaseUrl);
	logger.info("database_connected");
	markReady("db");

	// Push schema to database
	try {
		const { execSync } = await import("node:child_process");
		execSync("bunx drizzle-kit push --force", {
			cwd: import.meta.dir,
			stdio: "pipe",
			env: { ...process.env, DATABASE_URL: config.databaseUrl },
		});
		logger.info("schema_synced");
	} catch (err) {
		logger.warn("schema_push_skipped", {
			error: err instanceof Error ? err.message : String(err),
		});
	}

	// Create bot
	const bot = createBot(db);

	// Initialize admin notifications
	initAdminNotify(bot.api);

	// Register commands with Telegram
	await registerCommands(bot);

	// Start runner (concurrent long-polling)
	const runner = run(bot, {
		runner: {
			fetch: {
				allowed_updates: [
					"message",
					"edited_message",
					"callback_query",
					"inline_query",
					"chosen_inline_result",
					"chat_member",
					"my_chat_member",
					"pre_checkout_query",
					"message_reaction",
				],
			},
		},
	});

	logger.info("bot_running");
	markReady("bot");

	// Notify admins
	await notifyAdmins(
		`<b>BOT STARTED</b>\nEnv: ${config.environment}\nBot: @${config.botUsername}`,
	);

	// Start reminder scheduler
	startScheduler(db, bot, config.reminderCheckIntervalMs);
	markReady("scheduler");

	// ── Graceful Shutdown ───────────────────────────────────────────────
	const shutdown = async (signal: string) => {
		logger.info("shutdown_initiated", { signal });
		stopScheduler();
		runner.stop();
		await closeDb();
		await shutdownObservability();
		logger.info("shutdown_complete");
		process.exit(0);
	};

	process.on("SIGTERM", () => shutdown("SIGTERM"));
	process.on("SIGINT", () => shutdown("SIGINT"));
}

main().catch((err) => {
	console.error("[fatal]", err);
	process.exit(1);
});
