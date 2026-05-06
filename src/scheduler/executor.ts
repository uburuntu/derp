/** Executor — fires individual reminders (plain text or LLM mode) */

import { eq } from "drizzle-orm";
import type { Bot } from "grammy";
import type { DerpContext } from "../bot/context";
import { notifyAdmins } from "../common/admin-notify";
import { derpMetrics, logger } from "../common/observability";
import { escapeHtml } from "../common/sanitize";
import { config, getGoogleApiKeys } from "../config";
import type { Database } from "../db/connection";
import {
    addChatCredits,
    addUserCredits,
    deductChatCredits,
    deductUserCredits,
    getBalances,
    markLedgerSpendStatus,
} from "../db/queries/credits";
import { getOpenDebtAmount } from "../db/queries/finance";
import {
    markReminderCompleted,
    markReminderFailed,
    updateNextFireAt,
} from "../db/queries/reminders";
import {
    type Chat,
    chats,
    type Reminder,
    type User,
    users,
} from "../db/schema";
import { GoogleLLMProvider } from "../llm/providers/google";
import { createDbProviderCallRecorder } from "../platform/provider-call-recorder";
import { parseCronToNextDate } from "./cron";

const LLM_REMINDER_COST = 1;
const REPLY_TARGET_ERROR = /reply|message to reply|replied message/i;

type LlmReminderReservation =
    | {
          ok: true;
          source: "chat" | "user";
          ownerId: string;
          userId: string;
          idempotencyKey: string;
          ledgerId?: string;
      }
    | {
          ok: false;
          userReason: string;
          internalReason: string;
      };

async function reserveLlmReminderCredit(
    db: Database,
    chat: Chat,
    user: User,
    reminder: Reminder,
): Promise<LlmReminderReservation> {
    const { userCredits, chatCredits } = await getBalances(
        db,
        user.telegramId,
        chat.telegramId,
    );
    const [userDebt, chatDebt] = await Promise.all([
        getOpenDebtAmount(db, { userId: user.id }),
        getOpenDebtAmount(db, { userId: user.id, chatId: chat.id }),
    ]);
    const idempotencyKey = `reminder:${reminder.id}:fire:${reminder.fireCount + 1}:llm`;
    const meta = {
        reminderId: reminder.id,
        description: reminder.description,
    };

    try {
        if (chatCredits >= LLM_REMINDER_COST && chatDebt === 0) {
            const debit = await deductChatCredits(
                db,
                chat.id,
                user.id,
                LLM_REMINDER_COST,
                "reminder_llm",
                "gemini-2.5-flash",
                idempotencyKey,
                meta,
            );
            if (!debit.applied) {
                return {
                    ok: false,
                    userReason: "This LLM reminder fire was already reserved",
                    internalReason: "Duplicate LLM reminder credit reservation",
                };
            }
            return {
                ok: true,
                source: "chat",
                ownerId: chat.id,
                userId: user.id,
                idempotencyKey,
                ledgerId: debit.ledgerId,
            };
        }

        if (userCredits >= LLM_REMINDER_COST && userDebt === 0) {
            const debit = await deductUserCredits(
                db,
                user.id,
                LLM_REMINDER_COST,
                "reminder_llm",
                "gemini-2.5-flash",
                idempotencyKey,
                meta,
            );
            if (!debit.applied) {
                return {
                    ok: false,
                    userReason: "This LLM reminder fire was already reserved",
                    internalReason: "Duplicate LLM reminder credit reservation",
                };
            }
            return {
                ok: true,
                source: "user",
                ownerId: user.id,
                userId: user.id,
                idempotencyKey,
                ledgerId: debit.ledgerId,
            };
        }
    } catch (err) {
        const reason = err instanceof Error ? err.message : String(err);
        logger.error("reminder_llm_credit_reservation_failed", {
            reminderId: reminder.id,
            error: reason,
        });
        return {
            ok: false,
            userReason: "I could not reserve a credit for this LLM reminder",
            internalReason: reason,
        };
    }

    return {
        ok: false,
        userReason: `LLM reminders need ${LLM_REMINDER_COST} credit`,
        internalReason: `LLM reminders need ${LLM_REMINDER_COST} credit`,
    };
}

async function refundLlmReminderCredit(
    db: Database,
    reservation: LlmReminderReservation | null,
    reminder: Reminder,
    error: string,
): Promise<void> {
    if (!reservation?.ok) return;
    const refundKey = `${reservation.idempotencyKey}:refund`;
    const meta = {
        reminderId: reminder.id,
        reason: "llm_reminder_failed_after_reservation",
        error,
    };

    try {
        if (reservation.source === "chat") {
            await addChatCredits(
                db,
                reservation.ownerId,
                reservation.userId,
                LLM_REMINDER_COST,
                "refund",
                undefined,
                refundKey,
                meta,
            );
            return;
        }
        await addUserCredits(
            db,
            reservation.ownerId,
            LLM_REMINDER_COST,
            "refund",
            undefined,
            refundKey,
            meta,
        );
    } catch (err) {
        logger.error("reminder_llm_credit_refund_failed", {
            reminderId: reminder.id,
            refundKey,
            error: errorText(err),
        });
    }
}

async function buildReminderMessageText(
    db: Database,
    chat: Chat,
    reminder: Reminder,
    delayNote: string,
    reservation: LlmReminderReservation | null,
): Promise<{
    text: string;
    providerCallIds?: string[];
    costMicros?: number;
}> {
    if (reminder.usesLlm && reminder.prompt) {
        if (!reservation?.ok) {
            throw new Error("LLM reminder missing credit reservation");
        }
        const provider = new GoogleLLMProvider(
            getGoogleApiKeys(config),
            config.googleApiPaidKey,
        );

        const result = await provider.chat({
            model: "gemini-2.5-flash",
            systemPrompt:
                "You are Derp, an AI assistant executing a scheduled reminder. " +
                "Follow the prompt instructions. Be concise and helpful.",
            messages: [{ role: "user", content: reminder.prompt }],
            timeoutMs: 30_000,
            tracking: {
                recorder: createDbProviderCallRecorder(db),
                logicalRequestKey: reservation.idempotencyKey,
                operation: "reminder",
                keyClass: "paid",
                userId: reservation.userId,
                chatId: chat.id,
                ledgerId: reservation.ledgerId,
                toolName: "reminder_llm",
                creditsCharged: LLM_REMINDER_COST,
                creditSource: reservation.source,
                meta: {
                    reminderId: reminder.id,
                    fireCount: reminder.fireCount + 1,
                },
            },
        });

        const responseText = result.text?.trim();
        if (!responseText) {
            throw new Error("LLM reminder returned an empty response");
        }

        return {
            text: `🔔 ${responseText}${delayNote}`,
            providerCallIds: result.providerCallIds,
            costMicros: result.costMicros,
        };
    }

    if (reminder.message) {
        return { text: `🔔 ${reminder.message}${delayNote}` };
    }

    return { text: `🔔 Reminder: ${reminder.description}${delayNote}` };
}

async function sendTelegramMessageWithFallback(
    bot: Bot<DerpContext>,
    chat: Chat,
    reminder: Reminder,
    text: string,
): Promise<number> {
    const threadedOptions = {
        message_thread_id: reminder.threadId ?? undefined,
        reply_to_message_id: reminder.replyToMessageId ?? undefined,
    };

    try {
        const sent = await bot.api.sendMessage(
            chat.telegramId,
            text,
            threadedOptions,
        );
        return sent.message_id;
    } catch (err) {
        if (!reminder.replyToMessageId || !isMissingReplyTargetError(err)) {
            throw err;
        }

        logger.warn("reminder_reply_target_missing", {
            reminderId: reminder.id,
            replyToMessageId: reminder.replyToMessageId,
            error: errorText(err),
        });
        const sent = await bot.api.sendMessage(chat.telegramId, text, {
            message_thread_id: reminder.threadId ?? undefined,
        });
        return sent.message_id;
    }
}

async function markDelivered(
    db: Database,
    reminder: Reminder,
    telegramMessageId: number,
): Promise<void> {
    const meta = {
        ...(reminder.meta ?? {}),
        lastDelivery: {
            telegramMessageId,
            deliveredAt: new Date().toISOString(),
        },
    };

    if (reminder.isRecurring && reminder.cronExpression) {
        const nextFire = parseCronToNextDate(reminder.cronExpression);
        if (nextFire) {
            await updateNextFireAt(db, reminder.id, nextFire, meta);
        } else {
            await markReminderCompleted(db, reminder.id, meta);
        }
        return;
    }

    await markReminderCompleted(db, reminder.id, meta);
}

function isMissingReplyTargetError(err: unknown): boolean {
    return REPLY_TARGET_ERROR.test(errorText(err).toLowerCase());
}

function errorText(err: unknown): string {
    return err instanceof Error ? err.message : String(err);
}

function recordReminderFired(reminder: Reminder, isStartup: boolean): void {
    derpMetrics.remindersFired.add(1, {
        recurring: String(reminder.isRecurring),
        uses_llm: String(reminder.usesLlm),
    });

    logger.info("reminder_fired", {
        reminderId: reminder.id,
        isRecurring: reminder.isRecurring,
        usesLlm: reminder.usesLlm,
        isStartup,
    });
}

/** Execute a single reminder */
export async function executeReminder(
    db: Database,
    bot: Bot<DerpContext>,
    reminder: Reminder,
    isStartup: boolean,
): Promise<void> {
    const chat = await getChatByUuid(db, reminder.chatId);
    if (!chat) {
        logger.error("reminder_chat_not_found", { reminderId: reminder.id });
        await markReminderFailed(db, reminder.id, "Chat not found");
        return;
    }

    let llmReservation: LlmReminderReservation | null = null;
    if (reminder.usesLlm) {
        if (reminder.isRecurring) {
            await sendTelegramMessageWithFallback(
                bot,
                chat,
                reminder,
                "🔔 Recurring LLM reminders are disabled. Create a plain recurring reminder or a one-time LLM reminder.",
            );
            await markReminderFailed(
                db,
                reminder.id,
                "Recurring LLM reminders disabled",
            );
            return;
        }

        const user = await getUserByUuid(db, reminder.userId);
        if (!user) {
            await markReminderFailed(db, reminder.id, "User not found");
            return;
        }

        const reservation = await reserveLlmReminderCredit(
            db,
            chat,
            user,
            reminder,
        );
        if (!reservation.ok) {
            await sendTelegramMessageWithFallback(
                bot,
                chat,
                reminder,
                `🔔 LLM reminder skipped: ${reservation.userReason}. Use /buy to top up.`,
            );
            await markReminderFailed(
                db,
                reminder.id,
                reservation.internalReason,
            );
            return;
        }
        llmReservation = reservation;
    }

    const delayNote = isStartup ? "\n(delayed — bot was restarting)" : "";

    let sentMessageId: number;
    let reminderText: string | null = null;
    let billableProviderCallIds: string[] | undefined;
    let billableProviderCostMicros = 0;
    let billableReminderFailure = false;
    try {
        const reminderOutput = await buildReminderMessageText(
            db,
            chat,
            reminder,
            delayNote,
            llmReservation,
        );
        reminderText = reminderOutput.text;
        billableProviderCallIds = reminderOutput.providerCallIds;
        billableProviderCostMicros = reminderOutput.costMicros ?? 0;
        billableReminderFailure = reminder.usesLlm;
        sentMessageId = await sendTelegramMessageWithFallback(
            bot,
            chat,
            reminder,
            reminderText,
        );
    } catch (err) {
        const errorMsg = errorText(err);
        logger.error("reminder_execution_failed", {
            reminderId: reminder.id,
            error: errorMsg,
        });

        // Retry once
        try {
            if (!reminderText) {
                const reminderOutput = await buildReminderMessageText(
                    db,
                    chat,
                    reminder,
                    delayNote,
                    llmReservation,
                );
                reminderText = reminderOutput.text;
                billableProviderCallIds = reminderOutput.providerCallIds;
                billableProviderCostMicros = reminderOutput.costMicros ?? 0;
                billableReminderFailure = reminder.usesLlm;
            }
            sentMessageId = await sendTelegramMessageWithFallback(
                bot,
                chat,
                reminder,
                reminderText,
            );
        } catch (retryErr) {
            const retryMsg = errorText(retryErr);
            if (billableReminderFailure) {
                logger.warn("reminder_billable_failure_not_refunded", {
                    reminderId: reminder.id,
                    error: `${errorMsg}; retry: ${retryMsg}`,
                    ledgerId: llmReservation?.ok
                        ? llmReservation.ledgerId
                        : undefined,
                    providerCallIds: billableProviderCallIds,
                    costMicros: billableProviderCostMicros,
                });
                await notifyAdmins(
                    `⚠️ <b>Billable reminder delivery failure</b>\n\nReminder: <code>${escapeHtml(reminder.id)}</code>\nCredits kept: ${LLM_REMINDER_COST}\nSource: <code>${llmReservation?.ok ? llmReservation.source : "unknown"}</code>\nLedger: <code>${escapeHtml(llmReservation?.ok ? (llmReservation.ledgerId ?? "n/a") : "n/a")}</code>\nProvider calls: <code>${escapeHtml(billableProviderCallIds?.join(", ") ?? "n/a")}</code>\nProvider cost: $${(billableProviderCostMicros / 1_000_000).toFixed(4)}\nChat/user: <code>${chat.telegramId}</code> / <code>${reminder.userId}</code>\nReason: ${escapeHtml(`${errorMsg}; retry: ${retryMsg}`)}`,
                    { critical: true },
                ).catch((notifyErr) => {
                    logger.error(
                        "reminder_billable_failure_admin_notify_failed",
                        {
                            reminderId: reminder.id,
                            error: errorText(notifyErr),
                        },
                    );
                });
                await markLedgerSpendStatus(
                    db,
                    llmReservation?.ok ? llmReservation.ledgerId : undefined,
                    "delivery_failed",
                    {
                        error: `${errorMsg}; retry: ${retryMsg}`,
                        providerCallIds: billableProviderCallIds,
                        costMicros: billableProviderCostMicros,
                    },
                );
            } else {
                await refundLlmReminderCredit(
                    db,
                    llmReservation,
                    reminder,
                    `${errorMsg}; retry: ${retryMsg}`,
                );
                await markLedgerSpendStatus(
                    db,
                    llmReservation?.ok ? llmReservation.ledgerId : undefined,
                    "refunded",
                    { error: `${errorMsg}; retry: ${retryMsg}` },
                );
            }
            await markReminderFailed(
                db,
                reminder.id,
                `${errorMsg}; retry: ${retryMsg}`,
            );
            throw new Error(
                `Reminder delivery failed: ${errorMsg}; retry: ${retryMsg}`,
            );
        }
    }

    try {
        await markDelivered(db, reminder, sentMessageId);
        await markLedgerSpendStatus(
            db,
            llmReservation?.ok ? llmReservation.ledgerId : undefined,
            "delivered",
            {
                providerCallIds: billableProviderCallIds,
                costMicros: billableProviderCostMicros,
            },
        );
        recordReminderFired(reminder, isStartup);
    } catch (err) {
        const errorMsg = errorText(err);
        logger.error("reminder_state_update_failed_after_send", {
            reminderId: reminder.id,
            telegramMessageId: sentMessageId,
            error: errorMsg,
        });

        try {
            await markDelivered(db, reminder, sentMessageId);
            recordReminderFired(reminder, isStartup);
        } catch (retryErr) {
            const retryMsg = errorText(retryErr);
            await markReminderFailed(
                db,
                reminder.id,
                `Delivered message ${sentMessageId}, but state update failed: ${errorMsg}; retry: ${retryMsg}`,
            );
            throw new Error(
                `Reminder state update failed after delivery ${sentMessageId}: ${errorMsg}; retry: ${retryMsg}`,
            );
        }
    }
}

/** Helper: get a chat by its DB UUID (not telegram ID) */
async function getChatByUuid(
    db: Database,
    chatUuid: string,
): Promise<Chat | null> {
    const [row] = await db
        .select()
        .from(chats)
        .where(eq(chats.id, chatUuid))
        .limit(1);
    return row ?? null;
}

async function getUserByUuid(
    db: Database,
    userUuid: string,
): Promise<User | null> {
    const [row] = await db
        .select()
        .from(users)
        .where(eq(users.id, userUuid))
        .limit(1);
    return row ?? null;
}
