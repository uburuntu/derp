/** DB-backed implementations of the narrow data ports exposed to tools. */

import type { Database } from "../db/connection";
import { updateChatMemory } from "../db/queries/chats";
import {
    cancelReminder,
    countActiveReminders,
    countRecurringReminders,
    createReminder,
    getReminderById,
    getRemindersForChat,
} from "../db/queries/reminders";
import type { Chat, User } from "../db/schema";
import type { ToolMemoryStore, ToolReminderStore } from "../tools/types";

export function createDbToolMemoryStore(
    db: Database,
    chat: Pick<Chat, "id">,
): ToolMemoryStore {
    return {
        update: (memory) => updateChatMemory(db, chat.id, memory),
    };
}

export function createDbToolReminderStore(
    db: Database,
    user: Pick<User, "id">,
    chat: Pick<Chat, "id">,
): ToolReminderStore {
    return {
        countActive: (threadId) =>
            countActiveReminders(db, user.id, chat.id, threadId),
        countRecurringForUser: () => countRecurringReminders(db, user.id),
        create: async (input) => {
            await createReminder(db, {
                chatId: chat.id,
                userId: user.id,
                ...input,
            });
        },
        list: (threadId) =>
            getRemindersForChat(db, chat.id, undefined, threadId),
        getById: (id) => getReminderById(db, id),
        cancel: (id) => cancelReminder(db, id),
    };
}
