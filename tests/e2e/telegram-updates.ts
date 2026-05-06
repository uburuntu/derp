import type { Chat, Message, Update, User } from "grammy/types";

let updateSequence = 100_000;
let messageSequence = 200_000;
let callbackSequence = 300_000;

export interface TestActor {
    user: User;
    privateChat: Chat.PrivateChat;
}

export function nextUpdateId(): number {
    updateSequence += 1;
    return updateSequence;
}

export function nextMessageId(): number {
    messageSequence += 1;
    return messageSequence;
}

export function testActor(label: string, id?: number): TestActor {
    const telegramId = id ?? uniqueTelegramId();
    return {
        user: {
            id: telegramId,
            is_bot: false,
            first_name: `E2E ${label}`,
            username: `e2e_${label}_${telegramId}`,
            language_code: "en",
        },
        privateChat: {
            id: telegramId,
            type: "private",
            first_name: `E2E ${label}`,
            username: `e2e_${label}_${telegramId}`,
        },
    };
}

export function testGroup(label: string, id?: number): Chat.SupergroupChat {
    const telegramId = id ?? -uniqueTelegramId();
    return {
        id: telegramId,
        type: "supergroup",
        title: `E2E ${label}`,
    };
}

export function messageUpdate(input: {
    actor: TestActor;
    chat?: Chat;
    text: string;
    messageId?: number;
    threadId?: number;
    replyToMessageId?: number;
}): Update {
    const messageId = input.messageId ?? nextMessageId();
    const entities = input.text.startsWith("/")
        ? [
              {
                  offset: 0,
                  length:
                      input.text.split(/\s+/, 1)[0]?.length ??
                      input.text.length,
                  type: "bot_command" as const,
              },
          ]
        : undefined;

    return {
        update_id: nextUpdateId(),
        message: {
            message_id: messageId,
            date: nowTelegramDate(),
            chat: input.chat ?? input.actor.privateChat,
            from: input.actor.user,
            text: input.text,
            entities,
            message_thread_id: input.threadId,
            reply_to_message: input.replyToMessageId
                ? ({
                      message_id: input.replyToMessageId,
                      date: nowTelegramDate(),
                      chat: input.chat ?? input.actor.privateChat,
                  } as Message)
                : undefined,
        },
    } as unknown as Update;
}

export function callbackUpdate(input: {
    actor: TestActor;
    chat?: Chat;
    data: string;
    messageId?: number;
    messageText?: string;
    threadId?: number;
}): Update {
    callbackSequence += 1;
    return {
        update_id: nextUpdateId(),
        callback_query: {
            id: `cb-${callbackSequence}`,
            from: input.actor.user,
            chat_instance: `chat-instance-${input.chat?.id ?? input.actor.privateChat.id}`,
            data: input.data,
            message: {
                message_id: input.messageId ?? nextMessageId(),
                date: nowTelegramDate(),
                chat: input.chat ?? input.actor.privateChat,
                text: input.messageText ?? "callback source",
                message_thread_id: input.threadId,
            },
        },
    } as unknown as Update;
}

export function preCheckoutUpdate(input: {
    actor: TestActor;
    payload: string;
    totalAmount: number;
    currency?: string;
}): Update {
    callbackSequence += 1;
    return {
        update_id: nextUpdateId(),
        pre_checkout_query: {
            id: `pcq-${callbackSequence}`,
            from: input.actor.user,
            currency: input.currency ?? "XTR",
            total_amount: input.totalAmount,
            invoice_payload: input.payload,
        },
    } as unknown as Update;
}

export function successfulPaymentUpdate(input: {
    actor: TestActor;
    chat?: Chat;
    payload: string;
    totalAmount: number;
    chargeId: string;
    currency?: string;
    providerChargeId?: string;
}): Update {
    return {
        update_id: nextUpdateId(),
        message: {
            message_id: nextMessageId(),
            date: nowTelegramDate(),
            chat: input.chat ?? input.actor.privateChat,
            from: input.actor.user,
            successful_payment: {
                currency: input.currency ?? "XTR",
                total_amount: input.totalAmount,
                invoice_payload: input.payload,
                telegram_payment_charge_id: input.chargeId,
                provider_payment_charge_id:
                    input.providerChargeId ?? `provider-${input.chargeId}`,
            },
        },
    } as unknown as Update;
}

export function nowTelegramDate(): number {
    return Math.floor(Date.now() / 1000);
}

function uniqueTelegramId(): number {
    messageSequence += 1;
    return 7_000_000_000 + (Date.now() % 1_000_000) * 100 + messageSequence;
}
