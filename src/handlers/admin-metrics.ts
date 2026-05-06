import { sql } from "drizzle-orm";
import type { DerpContext } from "../bot/context";
import { escapeHtml } from "../common/sanitize";
import { config } from "../config";
import { creditUsdFloor } from "../credits/economy";
import { formatUsd } from "./admin-format";

export async function replyAdminMetrics(
    ctx: DerpContext,
    args: string,
): Promise<void> {
    const daysArg = Number.parseInt(args.trim() || "30", 10);
    const days = Number.isFinite(daysArg)
        ? Math.max(1, Math.min(365, daysArg))
        : 30;
    const creditFloorUsd = creditUsdFloor();
    const todayWindow = new Date().toISOString().slice(0, 10);

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
            (SELECT COALESCE(sum(used), 0)::int FROM quota_windows WHERE created_at >= now() - make_interval(days => ${days})) AS free_quota_uses,
            (SELECT COALESCE(sum(used), 0)::int FROM quota_windows WHERE scope = 'free_chat' AND subject_key = 'bot' AND window_key = ${todayWindow}) AS bot_free_chat_uses,
            (SELECT COALESCE(sum(used), 0)::int FROM quota_windows WHERE scope = 'web_search' AND subject_key = 'bot' AND window_key = ${todayWindow}) AS bot_free_search_uses
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

    const fallbackRows = await ctx.db.execute(sql`
        SELECT
            provider,
            COALESCE(actual_model_id, model_id) AS model,
            status,
            COALESCE(error_code, '') AS error_code,
            count(*)::int AS calls,
            COALESCE(sum(actual_cost_micros), 0)::bigint AS cost_micros
        FROM provider_calls
        WHERE created_at >= now() - make_interval(days => ${days})
            AND route = 'fallback'
        GROUP BY provider, model, status, error_code
        ORDER BY calls DESC, cost_micros DESC
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
        bot_free_chat_uses?: number;
        bot_free_search_uses?: number;
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
    const fallbackHealth = fallbackRows as unknown as Array<{
        provider: string;
        model: string;
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
    const fallbackHealthLines =
        fallbackHealth.length > 0
            ? fallbackHealth
                  .map((row) => {
                      const cost = Number(row.cost_micros ?? 0) / 1_000_000;
                      const error = row.error_code
                          ? `/${escapeHtml(row.error_code)}`
                          : "";
                      return `${escapeHtml(row.provider)} ${escapeHtml(row.model)} ${escapeHtml(row.status)}${error}: ${row.calls} calls, ${formatUsd(cost)}`;
                  })
                  .join("\n")
            : "No fallback calls.";
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
            `Bot free chat/search today: ${overviewRow.bot_free_chat_uses ?? 0}/${config.freeChatDailyBotLimit} · ${overviewRow.bot_free_search_uses ?? 0}/${config.freeSearchDailyBotLimit}\n` +
            `Unsettled payments: ${overviewRow.unsettled_payments ?? 0}\n` +
            `Stars gross/refunded/net: ${grossStars}⭐ / ${refundedStars}⭐ / ${grossStars - refundedStars}⭐\n\n` +
            `<b>Provider Cost</b>\n` +
            `Provider cost: ${formatUsd(providerCostUsd)}\n` +
            `Free/promo burn: ${formatUsd(freeProviderCostUsd)}\n` +
            `Stale provider calls: ${overviewRow.stale_provider_calls ?? 0}\n` +
            `Net revenue estimate: ${formatUsd(netRevenueUsd)}\n` +
            `Gross margin estimate: ${formatUsd(grossMarginUsd)}\n\n` +
            `<b>Provider health</b>\n${providerHealthLines}\n\n` +
            `<b>Fallback health</b>\n${fallbackHealthLines}\n\n` +
            `<b>Liability</b>\n` +
            `Outstanding credits: ${outstandingCredits} cr (${formatUsd(outstandingUsd)} floor value)\n` +
            `User/chat split: ${overviewRow.user_credit_liability ?? 0} / ${overviewRow.chat_credit_liability ?? 0} cr\n` +
            `Credit floor: ${formatUsd(creditFloorUsd)}/cr\n\n` +
            `<b>Top tools</b>\n${toolLines}\n\n` +
            `<b>Payments</b>\n${paymentLines}`,
        { parse_mode: "HTML" },
    );
}
