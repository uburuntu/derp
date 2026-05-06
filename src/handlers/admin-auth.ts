import type { DerpContext } from "../bot/context";
import { config } from "../config";

export function isAdmin(ctx: DerpContext): boolean {
    return config.botAdminIds.includes(ctx.from?.id ?? 0);
}
