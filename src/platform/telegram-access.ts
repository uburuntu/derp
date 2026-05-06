/** Telegram access helpers shared by handlers and tool adapters. */

type AccessSetting = "admins" | "everyone";

interface ChatAdminLookupContext {
    chat?: { type?: string };
    from?: { id: number };
    getChatMember(userId: number): Promise<{ status: string }>;
}

export function isGroupChatType(type: string | undefined): boolean {
    return type === "group" || type === "supergroup";
}

export function isGroupChat(ctx: { chat?: { type?: string } }): boolean {
    return isGroupChatType(ctx.chat?.type);
}

export async function isChatAdmin(
    ctx: ChatAdminLookupContext,
): Promise<boolean> {
    if (ctx.chat?.type === "private") return true;
    if (!ctx.from) return false;

    try {
        const member = await ctx.getChatMember(ctx.from.id);
        return member.status === "administrator" || member.status === "creator";
    } catch {
        return false;
    }
}

export function canUseAdminGatedSetting(
    setting: AccessSetting | undefined,
    isAdmin: boolean,
): boolean {
    return setting !== "admins" || isAdmin;
}
