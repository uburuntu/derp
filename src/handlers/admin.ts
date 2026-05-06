/** Admin handler — bot admin commands, /refund, and debug/e2e test helpers */

import { Composer } from "grammy";
import type { DerpContext } from "../bot/context";
import { formatRefundNotification, notifyAdmins } from "../common/admin-notify";
import { escapeHtml } from "../common/sanitize";
import { config } from "../config";
import { creditUsdFloor } from "../credits/economy";
import {
	addUserCredits,
	getBalances,
	getTransactionByIdempotencyKey,
	type RefundReconciliationResult,
	reconcileStarRefund,
	retryPaymentSettlement,
} from "../db/queries/credits";
import { waiveCreditDebt } from "../db/queries/finance";
import { getUserByTelegramId } from "../db/queries/users";
import { toolRegistry } from "../tools/registry";

const adminComposer = new Composer<DerpContext>();

function isAdmin(ctx: DerpContext): boolean {
	return config.botAdminIds.includes(ctx.from?.id ?? 0);
}

function looksAlreadyRefunded(error: string): boolean {
	return /payment_already_refunded|already.*refund|refund.*already/i.test(
		error,
	);
}

function formatUsd(value: number): string {
	return `$${value.toFixed(2)}`;
}

function formatReconciliation(
	chargeId: string,
	reconciliation: RefundReconciliationResult,
): string {
	const action = reconciliation.applied
		? "Refund reconciled locally."
		: "Refund was already reconciled locally.";
	const unrecovered =
		reconciliation.unrecoveredAmount > 0
			? `\nUnrecovered credits: ${reconciliation.unrecoveredAmount}`
			: "";

	return `${action}\nCharge: <code>${escapeHtml(chargeId)}</code>\nReversed: ${reconciliation.recoveredAmount}/${reconciliation.originalAmount} ${reconciliation.target} credits${unrecovered}\nBalance after: ${reconciliation.balanceAfter}`;
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
		if (looksAlreadyRefunded(msg)) {
			let reconciliation: RefundReconciliationResult;
			try {
				reconciliation = await reconcileStarRefund(ctx.db, chargeId, {
					adminId,
					targetUserId,
					source: "admin_refund_already_refunded",
					telegramRefundError: msg,
				});
			} catch (reconcileErr) {
				const reconcileMsg =
					reconcileErr instanceof Error
						? reconcileErr.message
						: String(reconcileErr);
				await ctx.reply(
					`Telegram says this charge is already refunded, but local reconciliation failed: ${escapeHtml(reconcileMsg)}`,
					{ parse_mode: "HTML" },
				);
				await notifyAdmins(
					formatRefundNotification({
						adminId,
						targetUserId,
						chargeId,
						success: false,
						error: reconcileMsg,
					}),
					{ critical: true },
				);
				return;
			}

			await ctx.reply(formatReconciliation(chargeId, reconciliation), {
				parse_mode: "HTML",
			});
			await notifyAdmins(
				formatRefundNotification({
					adminId,
					targetUserId,
					chargeId,
					success: true,
				}),
				{ critical: true },
			);
			return;
		}
		await ctx.reply(`Refund failed: ${msg}`);

		await notifyAdmins(
			formatRefundNotification({
				adminId,
				targetUserId,
				chargeId,
				success: false,
				error: msg,
			}),
			{ critical: true },
		);
		return;
	}

	let reconciliation: RefundReconciliationResult;
	try {
		reconciliation = await reconcileStarRefund(ctx.db, chargeId, {
			adminId,
			targetUserId,
		});
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
			{ critical: true },
		);
		return;
	}

	await ctx.reply(
		`Refund processed in Telegram.\nUser: <code>${targetUserId}</code>\n${formatReconciliation(chargeId, reconciliation)}`,
		{ parse_mode: "HTML" },
	);

	await notifyAdmins(
		formatRefundNotification({
			adminId,
			targetUserId,
			chargeId,
			success: true,
		}),
		{ critical: true },
	);
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

		case "metrics": {
			const daysArg = Number.parseInt(args.trim() || "30", 10);
			const days = Number.isFinite(daysArg)
				? Math.max(1, Math.min(365, daysArg))
				: 30;
			const { sql } = await import("drizzle-orm");
			const creditFloorUsd = creditUsdFloor();

			const [overview] = await ctx.db.execute(sql`
				SELECT
					(SELECT count(*)::int FROM users) AS users,
					(SELECT count(DISTINCT user_id)::int FROM messages WHERE telegram_date >= now() - make_interval(days => ${days}) AND user_id IS NOT NULL) AS active_users,
					(SELECT count(*)::int FROM messages WHERE telegram_date >= now() - make_interval(days => ${days}) AND direction = 'in') AS inbound_messages,
					(SELECT count(*)::int FROM messages WHERE telegram_date >= now() - make_interval(days => ${days}) AND direction = 'out') AS outbound_messages,
					(SELECT COALESCE(sum(-amount), 0)::int FROM ledger WHERE created_at >= now() - make_interval(days => ${days}) AND type = 'spend') AS credits_spent,
					(SELECT COALESCE(sum(amount), 0)::int FROM ledger WHERE created_at >= now() - make_interval(days => ${days}) AND type = 'grant') AS credits_granted,
					(SELECT COALESCE(sum(-amount), 0)::int FROM ledger WHERE created_at >= now() - make_interval(days => ${days}) AND type = 'refund') AS credits_refunded,
					(SELECT COALESCE(sum(((meta->>'unrecoveredAmount')::int)), 0)::int FROM ledger WHERE created_at >= now() - make_interval(days => ${days}) AND type = 'refund' AND meta ? 'unrecoveredAmount') AS refund_debt_credits,
					(SELECT COALESCE(sum(outstanding_amount), 0)::int FROM credit_debts WHERE status = 'open') AS open_debt_credits,
					(SELECT COALESCE(sum(stars), 0)::int FROM payment_receipts WHERE created_at >= now() - make_interval(days => ${days})) AS gross_stars,
					(SELECT COALESCE(sum(stars), 0)::int FROM payment_receipts WHERE refunded_at >= now() - make_interval(days => ${days}) AND status = 'refunded') AS refunded_stars,
					(SELECT count(*)::int FROM payment_receipts WHERE status IN ('received', 'settlement_failed', 'refund_pending')) AS unsettled_payments,
					(SELECT COALESCE(sum(actual_cost_micros), 0)::bigint FROM provider_calls WHERE created_at >= now() - make_interval(days => ${days})) AS provider_cost_micros,
					(SELECT COALESCE(sum(actual_cost_micros), 0)::bigint FROM provider_calls WHERE created_at >= now() - make_interval(days => ${days}) AND credits_charged = 0) AS free_provider_cost_micros,
					(SELECT count(*)::int FROM provider_calls WHERE status = 'started' AND created_at < now() - interval '10 minutes') AS stale_provider_calls,
					(SELECT COALESCE(sum(credits), 0)::int FROM users) AS user_credit_liability,
					(SELECT COALESCE(sum(credits), 0)::int FROM chats) AS chat_credit_liability,
					(SELECT COALESCE(sum(used), 0)::int FROM quota_windows WHERE created_at >= now() - make_interval(days => ${days})) AS free_quota_uses
			`);

			const toolRows = await ctx.db.execute(sql`
				SELECT
					COALESCE(tool_name, 'unknown') AS tool,
					count(*)::int AS uses,
					COALESCE(sum(-amount), 0)::int AS credits
				FROM ledger
				WHERE created_at >= now() - make_interval(days => ${days})
					AND type = 'spend'
				GROUP BY tool_name
				ORDER BY uses DESC, credits DESC
				LIMIT 8
			`);

			const paymentRows = await ctx.db.execute(sql`
				SELECT
					product_type AS product,
					count(*)::int AS payments,
					COALESCE(sum(stars), 0)::int AS stars
				FROM payment_receipts
				WHERE created_at >= now() - make_interval(days => ${days})
					AND status IN ('paid', 'settled')
				GROUP BY product_type
				ORDER BY stars DESC
			`);

			const providerRows = await ctx.db.execute(sql`
				SELECT
					provider,
					COALESCE(route, 'primary') AS route,
					status,
					COALESCE(error_code, '') AS error_code,
					count(*)::int AS calls,
					COALESCE(sum(actual_cost_micros), 0)::bigint AS cost_micros
				FROM provider_calls
				WHERE created_at >= now() - make_interval(days => ${days})
				GROUP BY provider, route, status, error_code
				ORDER BY calls DESC, cost_micros DESC
				LIMIT 10
			`);

			const overviewRow = overview as {
				users?: number;
				active_users?: number;
				inbound_messages?: number;
				outbound_messages?: number;
				credits_spent?: number;
				credits_granted?: number;
				credits_refunded?: number;
				refund_debt_credits?: number;
				open_debt_credits?: number;
				gross_stars?: number;
				refunded_stars?: number;
				unsettled_payments?: number;
				provider_cost_micros?: number;
				free_provider_cost_micros?: number;
				stale_provider_calls?: number;
				user_credit_liability?: number;
				chat_credit_liability?: number;
				free_quota_uses?: number;
			};
			const tools = toolRows as unknown as Array<{
				tool: string;
				uses: number;
				credits: number;
			}>;
			const payments = paymentRows as unknown as Array<{
				product: string;
				payments: number;
				stars: number;
			}>;
			const providerHealth = providerRows as unknown as Array<{
				provider: string;
				route: string;
				status: string;
				error_code: string;
				calls: number;
				cost_micros: bigint | number;
			}>;

			const toolLines =
				tools.length > 0
					? tools
							.map(
								(row) =>
									`${escapeHtml(row.tool)}: ${row.uses} uses, ${row.credits} cr`,
							)
							.join("\n")
					: "No tool spend yet.";
			const paymentLines =
				payments.length > 0
					? payments
							.map(
								(row) =>
									`${escapeHtml(row.product)}: ${row.payments} payments, ${row.stars}⭐`,
							)
							.join("\n")
					: "No Stars payments yet.";
			const providerHealthLines =
				providerHealth.length > 0
					? providerHealth
							.map((row) => {
								const cost = Number(row.cost_micros ?? 0) / 1_000_000;
								const error = row.error_code
									? `/${escapeHtml(row.error_code)}`
									: "";
								return `${escapeHtml(row.provider)} ${escapeHtml(row.route)} ${escapeHtml(row.status)}${error}: ${row.calls} calls, ${formatUsd(cost)}`;
							})
							.join("\n")
					: "No provider calls yet.";
			const outstandingCredits =
				(overviewRow.user_credit_liability ?? 0) +
				(overviewRow.chat_credit_liability ?? 0);
			const outstandingUsd = outstandingCredits * creditFloorUsd;
			const grossStars = overviewRow.gross_stars ?? 0;
			const refundedStars = overviewRow.refunded_stars ?? 0;
			const providerCostUsd =
				Number(overviewRow.provider_cost_micros ?? 0) / 1_000_000;
			const freeProviderCostUsd =
				Number(overviewRow.free_provider_cost_micros ?? 0) / 1_000_000;
			const netRevenueUsd = (grossStars - refundedStars) * 0.013;
			const grossMarginUsd = netRevenueUsd - providerCostUsd;

			await ctx.reply(
				`📈 <b>Usage Metrics</b> (${days}d)\n\n` +
					`Users: ${overviewRow.users ?? 0}\n` +
					`Active users: ${overviewRow.active_users ?? 0}\n` +
					`Messages: ${overviewRow.inbound_messages ?? 0} in / ${overviewRow.outbound_messages ?? 0} out\n` +
					`Credits spent: ${overviewRow.credits_spent ?? 0}\n` +
					`Credits granted: ${overviewRow.credits_granted ?? 0}\n` +
					`Credits refunded/recovered: ${overviewRow.credits_refunded ?? 0}\n` +
					`Refund debt opened/window: ${overviewRow.refund_debt_credits ?? 0} cr\n` +
					`Open refund debt: ${overviewRow.open_debt_credits ?? 0} cr\n` +
					`Free quota uses: ${overviewRow.free_quota_uses ?? 0}\n` +
					`Unsettled payments: ${overviewRow.unsettled_payments ?? 0}\n` +
					`Stars gross/refunded/net: ${grossStars}⭐ / ${refundedStars}⭐ / ${grossStars - refundedStars}⭐\n\n` +
					`<b>Provider Cost</b>\n` +
					`Provider cost: ${formatUsd(providerCostUsd)}\n` +
					`Free/promo burn: ${formatUsd(freeProviderCostUsd)}\n` +
					`Stale provider calls: ${overviewRow.stale_provider_calls ?? 0}\n` +
					`Net revenue estimate: ${formatUsd(netRevenueUsd)}\n` +
					`Gross margin estimate: ${formatUsd(grossMarginUsd)}\n\n` +
					`<b>Provider health</b>\n${providerHealthLines}\n\n` +
					`<b>Liability</b>\n` +
					`Outstanding credits: ${outstandingCredits} cr (${formatUsd(outstandingUsd)} floor value)\n` +
					`User/chat split: ${overviewRow.user_credit_liability ?? 0} / ${overviewRow.chat_credit_liability ?? 0} cr\n` +
					`Credit floor: ${formatUsd(creditFloorUsd)}/cr\n\n` +
					`<b>Top tools</b>\n${toolLines}\n\n` +
					`<b>Payments</b>\n${paymentLines}`,
				{ parse_mode: "HTML" },
			);
			break;
		}

		case "debts": {
			const limitArg = Number.parseInt(args.trim() || "10", 10);
			const limit = Number.isFinite(limitArg)
				? Math.max(1, Math.min(25, limitArg))
				: 10;
			const { sql } = await import("drizzle-orm");
			const rows = await ctx.db.execute(sql`
				SELECT
					d.id,
					d.target,
					d.amount,
					d.recovered_amount,
					d.outstanding_amount,
					d.telegram_charge_id,
					d.created_at,
					u.telegram_id AS user_telegram_id,
					c.telegram_id AS chat_telegram_id
				FROM credit_debts d
				JOIN users u ON u.id = d.user_id
				LEFT JOIN chats c ON c.id = d.chat_id
				WHERE d.status = 'open'
				ORDER BY d.created_at ASC
				LIMIT ${limit}
			`);
			const debts = rows as unknown as Array<{
				id: string;
				target: string;
				amount: number;
				recovered_amount: number;
				outstanding_amount: number;
				telegram_charge_id: string | null;
				created_at: Date;
				user_telegram_id: number;
				chat_telegram_id: number | null;
			}>;
			if (debts.length === 0) {
				await ctx.reply("No open refund debt.");
				break;
			}
			const lines = debts.map(
				(row) =>
					`<code>${escapeHtml(row.id)}</code>\n${escapeHtml(row.target)} · outstanding ${row.outstanding_amount}/${row.amount} cr · user <code>${row.user_telegram_id}</code>${row.chat_telegram_id ? ` · chat <code>${row.chat_telegram_id}</code>` : ""}${row.telegram_charge_id ? `\ncharge: <code>${escapeHtml(row.telegram_charge_id)}</code>` : ""}`,
			);
			await ctx.reply(`<b>Open Refund Debt</b>\n\n${lines.join("\n\n")}`, {
				parse_mode: "HTML",
			});
			break;
		}

		case "waive_debt": {
			const debtId = args.trim();
			if (!debtId) {
				await ctx.reply("Usage: /admin waive_debt <debt_id>");
				return;
			}
			const waived = await waiveCreditDebt(ctx.db, {
				debtId,
				adminId,
				meta: { source: "admin_command" },
			});
			await ctx.reply(waived ? "Debt waived." : "Open debt not found.");
			break;
		}

		case "reconcile_refund": {
			const chargeId = args.trim();
			if (!chargeId) {
				await ctx.reply("Usage: /admin reconcile_refund <telegram_charge_id>");
				return;
			}

			try {
				const reconciliation = await reconcileStarRefund(ctx.db, chargeId, {
					adminId,
					source: "admin_manual_reconcile_refund",
				});
				await ctx.reply(formatReconciliation(chargeId, reconciliation), {
					parse_mode: "HTML",
				});
			} catch (err) {
				const msg = err instanceof Error ? err.message : String(err);
				await ctx.reply(`Refund reconciliation failed: ${escapeHtml(msg)}`, {
					parse_mode: "HTML",
				});
				await notifyAdmins(
					formatRefundNotification({
						adminId,
						targetUserId: 0,
						chargeId,
						success: false,
						error: msg,
					}),
					{ critical: true },
				);
			}
			break;
		}

		case "settle_payment": {
			const chargeId = args.trim();
			if (!chargeId) {
				await ctx.reply("Usage: /admin settle_payment <telegram_charge_id>");
				return;
			}

			try {
				const result = await retryPaymentSettlement(ctx.db, chargeId);
				await ctx.reply(
					result.applied
						? `Payment settled.\nCharge: <code>${escapeHtml(chargeId)}</code>\nCredited: ${result.creditedAmount ?? "n/a"}\nDebt recovered: ${result.debtRecovered ?? 0}\nBalance after: ${result.balanceAfter}`
						: `Payment is not pending settlement or was already settled.\nCharge: <code>${escapeHtml(chargeId)}</code>`,
					{ parse_mode: "HTML" },
				);
			} catch (err) {
				const msg = err instanceof Error ? err.message : String(err);
				await ctx.reply(`Payment settlement retry failed: ${escapeHtml(msg)}`, {
					parse_mode: "HTML",
				});
				await notifyAdmins(
					`⚠️ <b>Payment settlement retry failed</b>\n\nAdmin: <code>${adminId}</code>\nCharge: <code>${escapeHtml(chargeId)}</code>\nReason: ${escapeHtml(msg)}`,
					{ critical: true },
				);
			}
			break;
		}

		case "unsettled_payments": {
			const limitArg = Number.parseInt(args.trim() || "10", 10);
			const limit = Number.isFinite(limitArg)
				? Math.max(1, Math.min(25, limitArg))
				: 10;
			const { sql } = await import("drizzle-orm");
			const rows = await ctx.db.execute(sql`
				SELECT
					p.telegram_charge_id,
					p.status,
					p.product_type,
					p.product_id,
					p.stars,
					p.credits,
					p.created_at,
					p.updated_at,
					p.meta->>'lastSettlementError' AS last_error,
					COALESCE((p.meta->>'settlementAttemptCount')::int, 0) AS attempts,
					u.telegram_id AS user_telegram_id,
					c.telegram_id AS chat_telegram_id
				FROM payment_receipts p
				JOIN users u ON u.id = p.user_id
				LEFT JOIN chats c ON c.id = p.chat_id
				WHERE p.status IN ('received', 'settlement_failed', 'refund_pending')
				ORDER BY p.updated_at ASC
				LIMIT ${limit}
			`);
			const payments = rows as unknown as Array<{
				telegram_charge_id: string;
				status: string;
				product_type: string;
				product_id: string | null;
				stars: number;
				credits: number;
				created_at: Date;
				updated_at: Date;
				last_error: string | null;
				attempts: number;
				user_telegram_id: number;
				chat_telegram_id: number | null;
			}>;
			if (payments.length === 0) {
				await ctx.reply("No unsettled payments.");
				break;
			}
			const lines = payments.map((payment) => {
				const ageMinutes = Math.max(
					0,
					Math.round(
						(Date.now() - new Date(payment.created_at).getTime()) / 60000,
					),
				);
				const product = payment.product_id
					? `${payment.product_type}/${payment.product_id}`
					: payment.product_type;
				return (
					`<code>${escapeHtml(payment.telegram_charge_id)}</code>\n` +
					`Status: ${escapeHtml(payment.status)} · Attempts: ${payment.attempts} · Age: ${ageMinutes}m\n` +
					`User/chat: <code>${payment.user_telegram_id}</code> / <code>${payment.chat_telegram_id ?? "n/a"}</code>\n` +
					`Product: ${escapeHtml(product)} · ${payment.stars}⭐ · ${payment.credits} cr\n` +
					`Retry: <code>/admin settle_payment ${escapeHtml(payment.telegram_charge_id)}</code>` +
					(payment.last_error
						? `\nLast error: ${escapeHtml(payment.last_error).slice(0, 240)}`
						: "")
				);
			});
			await ctx.reply(`<b>Unsettled Payments</b>\n\n${lines.join("\n\n")}`, {
				parse_mode: "HTML",
			});
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
				"payment_receipts",
				"provider_calls",
				"credit_debts",
				"credit_debt_events",
				"quota_windows",
				"pending_tool_confirmations",
				"subscription_periods",
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
					"/admin metrics [days] — Usage, credit, and payment rollup\n" +
					"/admin debts [limit] — Open refund debt\n" +
					"/admin waive_debt &lt;debtId&gt; — Waive open refund debt\n" +
					"/admin stars — Bot Stars balance\n" +
					"/admin reconcile_refund &lt;chargeId&gt; — Reconcile an already-refunded charge\n" +
					"/admin settle_payment &lt;chargeId&gt; — Retry a pending payment settlement\n" +
					"/admin unsettled_payments [limit] — List payments needing settlement\n" +
					"/admin db — Table row counts\n" +
					"/admin test — E2E smoke test (grants 100 credits)\n\n" +
					"/refund &lt;userId&gt; &lt;chargeId&gt; — Refund a payment",
				{ parse_mode: "HTML" },
			);
	}
});

export { adminComposer };
