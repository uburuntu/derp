/** Entry point — config validation, DB init, bot creation, runner start */

import { run } from "@grammyjs/runner";
import { createBot, registerCommands } from "./bot/bot";
import { initAdminNotify, notifyAdmins } from "./common/admin-notify";
import {
	markReady,
	setReadinessCheck,
	startHealthServer,
} from "./common/health";
import {
	initObservability,
	logger,
	shutdownObservability,
} from "./common/observability";
import { config } from "./config";
import {
	assertDatabaseReady,
	checkDatabaseReady,
	closeDb,
	getDb,
} from "./db/connection";
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
	setReadinessCheck(() => checkDatabaseReady(db));
	await assertDatabaseReady(db);
	logger.info("database_ready");
	markReady("db");
	markReady("schema");

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
