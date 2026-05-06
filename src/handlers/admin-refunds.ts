import type { Composer } from "grammy";
import type { DerpContext } from "../bot/context";
import { formatRefundNotification, notifyAdmins } from "../common/admin-notify";
import { escapeHtml } from "../common/sanitize";
import {
    getTransactionByIdempotencyKey,
    type RefundReconciliationResult,
    reconcileStarRefund,
} from "../db/queries/credits";
import { isAdmin } from "./admin-auth";
import { formatReconciliation } from "./admin-format";

function looksAlreadyRefunded(error: string): boolean {
    return /payment_already_refunded|already.*refund|refund.*already/i.test(
        error,
    );
}

export function registerRefundCommand(composer: Composer<DerpContext>): void {
    composer.command("refund", async (ctx) => {
        if (!isAdmin(ctx)) return;
        const adminId = ctx.from?.id;
        if (!adminId) return;

        const parts = (ctx.match ?? "").split(" ").filter(Boolean);
        if (parts.length < 2) {
            await ctx.reply(
                "Usage: /refund <user_telegram_id> <telegram_charge_id>",
            );
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
                    reconciliation = await reconcileStarRefund(
                        ctx.db,
                        chargeId,
                        {
                            adminId,
                            targetUserId,
                            source: "admin_refund_already_refunded",
                            telegramRefundError: msg,
                        },
                    );
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

                await ctx.reply(
                    formatReconciliation(chargeId, reconciliation),
                    {
                        parse_mode: "HTML",
                    },
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
}
