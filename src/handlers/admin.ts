/** Admin handler — bot admin commands, /refund, and debug/e2e test helpers */

import { Composer } from "grammy";
import type { DerpContext } from "../bot/context";
import { formatRefundNotification, notifyAdmins } from "../common/admin-notify";
import { escapeHtml } from "../common/sanitize";
import { config } from "../config";
import {
	addUserCredits,
	getBalances,
	getTransactionByIdempotencyKey,
	reconcileStarRefund,
} from "../db/queries/credits";
import { getUserByTelegramId } from "../db/queries/users";
import { toolRegistry } from "../tools/registry";

const adminComposer = new Composer<DerpContext>();

function isAdmin(ctx: DerpContext): boolean {
	return config.botAdminIds.includes(ctx.from?.id ?? 0);
}

// ── /refund <userId> <chargeId> — standalone refund command ─────────────────

adminComposer.command("refund", async (ctx) => {
	if (!isAdmin(ctx)) return;
	const adminId = ctx.from?.id;
	if (!adminId) return;

	const parts = (ctx.match ?? "").split(" ").filter(Boolean);
	if (parts.length < 2) {
		await ctx.reply("Usage: /refund <user_telegram_id> <telegram_charge_id>");
		return;
	}

	const [targetUserIdArg, chargeId] = parts;
	if (!targetUserIdArg || !chargeId) return;
	const targetUserId = Number.parseInt(targetUserIdArg, 10);

	if (Number.isNaN(targetUserId)) {
		await ctx.reply("Invalid user ID");
		return;
	}

	const existingRefund = await getTransactionByIdempotencyKey(
		ctx.db,
		`refund:${chargeId}`,
	);
	if (existingRefund) {
		await ctx.reply(
			`Refund already reconciled locally.\nUser: <code>${targetUserId}</code>\nCharge: <code>${chargeId}</code>`,
			{ parse_mode: "HTML" },
		);
		return;
	}

	try {
		await ctx.api.raw.refundStarPayment({
			user_id: targetUserId,
			telegram_payment_charge_id: chargeId,
		});
	} catch (err) {
		const msg = err instanceof Error ? err.message : String(err);
		await ctx.reply(`Refund failed: ${msg}`);

		await notifyAdmins(
			formatRefundNotification({
				adminId,
				targetUserId,
				chargeId,
				success: false,
				error: msg,
			}),
		);
		return;
	}

	try {
		const reconciliation = await reconcileStarRefund(ctx.db, chargeId, {
			adminId,
			targetUserId,
		});
		const unrecovered =
			reconciliation.unrecoveredAmount > 0
				? `\nUnrecovered credits: ${reconciliation.unrecoveredAmount}`
				: "";
		await ctx.reply(
			`Refund processed.\nUser: <code>${targetUserId}</code>\nCharge: <code>${chargeId}</code>\nReversed: ${reconciliation.recoveredAmount}/${reconciliation.originalAmount} ${reconciliation.target} credits${unrecovered}\nBalance after: ${reconciliation.balanceAfter}`,
			{ parse_mode: "HTML" },
		);

		await notifyAdmins(
			formatRefundNotification({
				adminId,
				targetUserId,
				chargeId,
				success: true,
			}),
		);
	} catch (err) {
		const msg = err instanceof Error ? err.message : String(err);
		await ctx.reply(
			`Refund processed in Telegram, but local reconciliation failed: ${msg}`,
		);

		await notifyAdmins(
			formatRefundNotification({
				adminId,
				targetUserId,
				chargeId,
				success: false,
				error: msg,
			}),
		);
	}
});

// ── /admin <subcommand> — admin panel ───────────────────────────────────────

adminComposer.command("admin", async (ctx) => {
	if (!isAdmin(ctx)) return;
	if (!ctx.dbUser) return;
	const adminId = ctx.from?.id;
	if (!adminId) return;

	const parts = (ctx.match ?? "").split(" ");
	const subcommand = parts[0];
	const args = parts.slice(1).join(" ");

	switch (subcommand) {
		case "status": {
			const uptime = process.uptime();
			const hours = Math.floor(uptime / 3600);
			const mins = Math.floor((uptime % 3600) / 60);
			const tools = toolRegistry.getTools();
			const mem = process.memoryUsage();

			await ctx.reply(
				"<b>Bot Status</b>\n" +
					`Uptime: ${hours}h ${mins}m\n` +
					`Env: ${config.environment}\n` +
					`Tools: ${tools.length}\n` +
					`Heap: ${Math.round(mem.heapUsed / 1024 / 1024)}MB / ${Math.round(mem.heapTotal / 1024 / 1024)}MB\n` +
					`RSS: ${Math.round(mem.rss / 1024 / 1024)}MB\n` +
					`Events chat: ${config.botAdminEventsChatId ?? "not set"}`,
				{ parse_mode: "HTML" },
			);
			break;
		}

		case "credits": {
			const creditParts = args.split(" ");
			const amount = Number.parseInt(creditParts[0] ?? "0", 10);
			if (Number.isNaN(amount) || amount <= 0) {
				await ctx.reply("Usage: /admin credits <amount> [userId]");
				return;
			}

			const targetTgId = creditParts[1]
				? Number.parseInt(creditParts[1], 10)
				: adminId;

			const targetUser = await getUserByTelegramId(ctx.db, targetTgId);
			if (!targetUser) {
				await ctx.reply("User not found");
				return;
			}

			const newBalance = await addUserCredits(
				ctx.db,
				targetUser.id,
				amount,
				"grant",
				undefined,
				`admin:grant:${Date.now()}`,
				{ grantedBy: adminId },
			);

			await ctx.reply(
				`Granted ${amount} credits to <code>${targetTgId}</code>. Balance: ${newBalance}`,
				{ parse_mode: "HTML" },
			);
			break;
		}

		case "reset": {
			// /admin reset [userId] — reset user credits to 0
			const targetTgId = args.trim()
				? Number.parseInt(args.trim(), 10)
				: adminId;
			const targetUser = await getUserByTelegramId(ctx.db, targetTgId);
			if (!targetUser) {
				await ctx.reply("User not found");
				return;
			}

			const { eq } = await import("drizzle-orm");
			const { users } = await import("../db/schema");
			await ctx.db
				.update(users)
				.set({
					credits: 0,
					subscriptionTier: null,
					subscriptionExpiresAt: null,
				})
				.where(eq(users.id, targetUser.id));

			await ctx.reply(
				`Reset user <code>${targetTgId}</code>: credits=0, subscription=none`,
				{ parse_mode: "HTML" },
			);
			break;
		}

		case "tools": {
			const tools = toolRegistry.getTools();
			const lines = tools.map((t) => {
				const cmds = t.commands.length > 0 ? t.commands.join(", ") : "(agent)";
				return `<code>${t.name}</code> ${cmds} — ${t.credits}cr, ${t.freeDaily}/day`;
			});
			await ctx.reply(`<b>Tools</b>\n\n${lines.join("\n")}`, {
				parse_mode: "HTML",
			});
			break;
		}

		case "user": {
			// /admin user [userId] — inspect user's DB state
			const targetTgId = args.trim()
				? Number.parseInt(args.trim(), 10)
				: adminId;
			const targetUser = await getUserByTelegramId(ctx.db, targetTgId);
			if (!targetUser) {
				await ctx.reply("User not found");
				return;
			}

			const { userCredits, chatCredits } = await getBalances(
				ctx.db,
				targetUser.telegramId,
				ctx.dbChat?.telegramId ?? 0,
			);

			const subInfo = targetUser.subscriptionTier
				? `${escapeHtml(targetUser.subscriptionTier)} (expires ${targetUser.subscriptionExpiresAt?.toISOString() ?? "?"})`
				: "none";
			const name =
				`${targetUser.firstName} ${targetUser.lastName ?? ""}`.trim();
			const username = targetUser.username
				? `@${escapeHtml(targetUser.username)}`
				: "-";

			await ctx.reply(
				`<b>User</b> <code>${targetUser.telegramId}</code>\n` +
					`Name: ${escapeHtml(name)}\n` +
					`Username: ${username}\n` +
					`DB ID: <code>${targetUser.id}</code>\n` +
					`Credits: ${userCredits}\n` +
					`Chat pool: ${chatCredits}\n` +
					`Subscription: ${subInfo}\n` +
					`Premium: ${targetUser.isPremium}\n` +
					`Created: ${targetUser.createdAt.toISOString()}`,
				{ parse_mode: "HTML" },
			);
			break;
		}

		case "ledger": {
			// /admin ledger [userId] — last 10 transactions
			const targetTgId = args.trim()
				? Number.parseInt(args.trim(), 10)
				: adminId;
			const targetUser = await getUserByTelegramId(ctx.db, targetTgId);
			if (!targetUser) {
				await ctx.reply("User not found");
				return;
			}

			const { eq } = await import("drizzle-orm");
			const { desc } = await import("drizzle-orm");
			const { ledger } = await import("../db/schema");
			const rows = await ctx.db
				.select()
				.from(ledger)
				.where(eq(ledger.userId, targetUser.id))
				.orderBy(desc(ledger.createdAt))
				.limit(10);

			if (rows.length === 0) {
				await ctx.reply("No transactions found");
				return;
			}

			const lines = rows.map((r) => {
				const sign = r.amount >= 0 ? "+" : "";
				const tool = r.toolName ? ` (${escapeHtml(r.toolName)})` : "";
				const charge = r.telegramChargeId
					? `\n   charge: <code>${escapeHtml(r.telegramChargeId)}</code>`
					: "";
				return `${sign}${r.amount} ${escapeHtml(r.type)}${tool} → bal:${r.balanceAfter}${charge}`;
			});

			await ctx.reply(
				`<b>Ledger</b> for <code>${targetTgId}</code> (last ${rows.length})\n\n${lines.join("\n")}`,
				{ parse_mode: "HTML" },
			);
			break;
		}

		case "stars": {
			const result = await ctx.api.getMyStarBalance();
			await ctx.reply(
				`⭐ <b>Bot Stars Balance</b>\n\nBalance: ${result.amount} ⭐`,
				{ parse_mode: "HTML" },
			);
			break;
		}

		case "db": {
			// /admin db — table row counts
			const { sql } = await import("drizzle-orm");
			const tables = [
				"users",
				"chats",
				"chat_members",
				"messages",
				"ledger",
				"usage_quotas",
				"reminders",
			];
			const counts: string[] = [];
			for (const table of tables) {
				const [row] = await ctx.db.execute(
					sql.raw(`SELECT count(*)::int AS c FROM ${table}`),
				);
				counts.push(`${table}: ${(row as { c: number })?.c ?? "?"}`);
			}
			await ctx.reply(`<b>DB Stats</b>\n\n${counts.join("\n")}`, {
				parse_mode: "HTML",
			});
			break;
		}

		case "test": {
			// /admin test — run a quick e2e smoke test
			const results: string[] = [];
			const check = (name: string, ok: boolean, detail?: string) => {
				results.push(
					`${ok ? "pass" : "FAIL"} ${name}${detail ? `: ${detail}` : ""}`,
				);
			};

			// 1. Check DB connectivity
			try {
				const { sql } = await import("drizzle-orm");
				await ctx.db.execute(sql`SELECT 1`);
				check("DB connection", true);
			} catch (e) {
				check("DB connection", false, String(e));
			}

			// 2. Check user exists in DB
			check("User in DB", !!ctx.dbUser, ctx.dbUser?.id);

			// 3. Check chat exists in DB
			check("Chat in DB", !!ctx.dbChat, ctx.dbChat?.id);

			// 4. Check credit service
			if (ctx.creditService) {
				const oc = await ctx.creditService.getOrchestratorConfig();
				check(
					"Orchestrator",
					true,
					`tier=${oc.tier} model=${oc.modelId} ctx=${oc.contextLimit}`,
				);
			} else {
				check("Orchestrator", false);
			}

			// 5. Check tools registered
			const tools = toolRegistry.getTools();
			check("Tools", tools.length > 0, `${tools.length} registered`);

			// 6. Check balances
			if (ctx.dbUser && ctx.dbChat) {
				const bal = await getBalances(
					ctx.db,
					ctx.dbUser.telegramId,
					ctx.dbChat.telegramId,
				);
				check(
					"Balances",
					true,
					`user=${bal.userCredits} chat=${bal.chatCredits}`,
				);
			}

			// 7. Check events chat reachable
			if (config.botAdminEventsChatId) {
				try {
					await ctx.api.sendMessage(
						config.botAdminEventsChatId,
						"Test ping from /admin test",
					);
					check("Events chat", true);
				} catch (e) {
					check("Events chat", false, String(e));
				}
			} else {
				check("Events chat", false, "not configured");
			}

			// 8. Grant 100 test credits
			if (ctx.dbUser) {
				const nb = await addUserCredits(
					ctx.db,
					ctx.dbUser.id,
					100,
					"grant",
					undefined,
					`admin:test:${Date.now()}`,
					{ reason: "e2e_test" },
				);
				check("Grant 100 credits", true, `balance=${nb}`);
			}

			// 9. Check orchestrator again (should be STANDARD now)
			if (ctx.creditService) {
				const oc = await ctx.creditService.getOrchestratorConfig();
				check("Tier after grant", oc.tier === "standard", oc.tier);
			}

			await ctx.reply(
				`<b>E2E Smoke Test</b>\n\n<pre>${results.join("\n")}</pre>`,
				{ parse_mode: "HTML" },
			);
			break;
		}

		default:
			await ctx.reply(
				"<b>Admin Commands</b>\n\n" +
					"/admin status — System diagnostics\n" +
					"/admin credits &lt;n&gt; [userId] — Grant credits\n" +
					"/admin reset [userId] — Reset credits + subscription\n" +
					"/admin user [userId] — Inspect user DB state\n" +
					"/admin ledger [userId] — Last 10 transactions\n" +
					"/admin tools — List registered tools with pricing\n" +
					"/admin stars — Bot Stars balance\n" +
					"/admin db — Table row counts\n" +
					"/admin test — E2E smoke test (grants 100 credits)\n\n" +
					"/refund &lt;userId&gt; &lt;chargeId&gt; — Refund a payment",
				{ parse_mode: "HTML" },
			);
	}
});

export { adminComposer };
