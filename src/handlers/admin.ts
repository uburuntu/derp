/** Admin handler — bot admin commands, /refund, and debug/e2e test helpers */

import { Composer } from "grammy";
import type { DerpContext } from "../bot/context";
import { formatRefundNotification, notifyAdmins } from "../common/admin-notify";
import { logger } from "../common/observability";
import { escapeHtml } from "../common/sanitize";
import { config } from "../config";
import {
    addUserCredits,
    getBalances,
    markPaymentSettlementFailed,
    reconcileStarRefund,
    retryPaymentSettlement,
} from "../db/queries/credits";
import { waiveCreditDebt } from "../db/queries/finance";
import { getUserByTelegramId } from "../db/queries/users";
import { toolRegistry } from "../tools/registry";
import { isAdmin } from "./admin-auth";
import { formatReconciliation, formatUsd } from "./admin-format";
import { replyAdminMetrics } from "./admin-metrics";
import { registerRefundCommand } from "./admin-refunds";

const adminComposer = new Composer<DerpContext>();

// ── /refund <userId> <chargeId> — standalone refund command ─────────────────

registerRefundCommand(adminComposer);

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
                const cmds =
                    t.commands.length > 0 ? t.commands.join(", ") : "(agent)";
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
            await replyAdminMetrics(ctx, args);
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
            await ctx.reply(
                `<b>Open Refund Debt</b>\n\n${lines.join("\n\n")}`,
                {
                    parse_mode: "HTML",
                },
            );
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
                await ctx.reply(
                    "Usage: /admin reconcile_refund <telegram_charge_id>",
                );
                return;
            }

            try {
                const reconciliation = await reconcileStarRefund(
                    ctx.db,
                    chargeId,
                    {
                        adminId,
                        source: "admin_manual_reconcile_refund",
                    },
                );
                await ctx.reply(
                    formatReconciliation(chargeId, reconciliation),
                    {
                        parse_mode: "HTML",
                    },
                );
            } catch (err) {
                const msg = err instanceof Error ? err.message : String(err);
                await ctx.reply(
                    `Refund reconciliation failed: ${escapeHtml(msg)}`,
                    {
                        parse_mode: "HTML",
                    },
                );
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
                await ctx.reply(
                    "Usage: /admin settle_payment <telegram_charge_id>",
                );
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
                await markPaymentSettlementFailed(ctx.db, chargeId, msg).catch(
                    (markErr) => {
                        logger.error("payment_settle_retry_mark_failed", {
                            chargeId,
                            error:
                                markErr instanceof Error
                                    ? markErr.message
                                    : String(markErr),
                        });
                    },
                );
                await ctx.reply(
                    `Payment settlement retry failed: ${escapeHtml(msg)}`,
                    {
                        parse_mode: "HTML",
                    },
                );
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
                        (Date.now() - new Date(payment.created_at).getTime()) /
                            60000,
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
            await ctx.reply(
                `<b>Unsettled Payments</b>\n\n${lines.join("\n\n")}`,
                {
                    parse_mode: "HTML",
                },
            );
            break;
        }

        case "stale_provider_calls": {
            const limitArg = Number.parseInt(args.trim() || "10", 10);
            const limit = Number.isFinite(limitArg)
                ? Math.max(1, Math.min(25, limitArg))
                : 10;
            const { sql } = await import("drizzle-orm");
            const rows = await ctx.db.execute(sql`
				SELECT
					p.id,
					p.logical_request_key,
					p.provider,
					p.operation,
					p.route,
					p.key_class,
					p.model_id,
					p.tool_name,
					p.status,
					p.provider_request_id,
					p.error_code,
					p.error_message,
					p.actual_cost_micros,
					p.credits_charged,
					p.created_at,
					u.telegram_id AS user_telegram_id,
					c.telegram_id AS chat_telegram_id
				FROM provider_calls p
				LEFT JOIN users u ON u.id = p.user_id
				LEFT JOIN chats c ON c.id = p.chat_id
				WHERE p.status = 'started'
					AND p.created_at < now() - interval '10 minutes'
				ORDER BY p.created_at ASC
				LIMIT ${limit}
			`);
            const calls = rows as unknown as Array<{
                id: string;
                logical_request_key: string | null;
                provider: string;
                operation: string;
                route: string;
                key_class: string;
                model_id: string;
                tool_name: string | null;
                status: string;
                provider_request_id: string | null;
                error_code: string | null;
                error_message: string | null;
                actual_cost_micros: number | bigint | null;
                credits_charged: number;
                created_at: Date;
                user_telegram_id: number | null;
                chat_telegram_id: number | null;
            }>;
            if (calls.length === 0) {
                await ctx.reply("No stale provider calls.");
                break;
            }
            const lines = calls.map((call) => {
                const ageMinutes = Math.max(
                    0,
                    Math.round(
                        (Date.now() - new Date(call.created_at).getTime()) /
                            60000,
                    ),
                );
                const cost = Number(call.actual_cost_micros ?? 0) / 1_000_000;
                const tool = call.tool_name
                    ? ` · tool ${escapeHtml(call.tool_name)}`
                    : "";
                const request = call.provider_request_id
                    ? `\nrequest: <code>${escapeHtml(call.provider_request_id)}</code>`
                    : "";
                const logical = call.logical_request_key
                    ? `\nlogical: <code>${escapeHtml(call.logical_request_key)}</code>`
                    : "";
                const error = call.error_code
                    ? `\nerror: ${escapeHtml(call.error_code)} ${escapeHtml(call.error_message ?? "")}`.slice(
                          0,
                          260,
                      )
                    : "";
                return (
                    `<code>${escapeHtml(call.id)}</code>\n` +
                    `${escapeHtml(call.provider)} ${escapeHtml(call.route)} ${escapeHtml(call.operation)}${tool}\n` +
                    `Model: ${escapeHtml(call.model_id)} · ${escapeHtml(call.key_class)} · age ${ageMinutes}m\n` +
                    `User/chat: <code>${call.user_telegram_id ?? "n/a"}</code> / <code>${call.chat_telegram_id ?? "n/a"}</code>\n` +
                    `Credits/cost: ${call.credits_charged} cr / ${formatUsd(cost)}` +
                    request +
                    logical +
                    error
                );
            });
            await ctx.reply(
                `<b>Stale Provider Calls</b>\n\n${lines.join("\n\n")}`,
                {
                    parse_mode: "HTML",
                },
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
                    "/admin stale_provider_calls [limit] — List provider calls stuck in started\n" +
                    "/admin db — Table row counts\n" +
                    "/admin test — E2E smoke test (grants 100 credits)\n\n" +
                    "/refund &lt;userId&gt; &lt;chargeId&gt; — Refund a payment",
                { parse_mode: "HTML" },
            );
    }
});

export { adminComposer };
