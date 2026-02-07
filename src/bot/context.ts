/** Custom context type for the Derp bot — combines all custom properties */

import type { AutoChatActionFlavor } from "@grammyjs/auto-chat-action";
import type { I18nFlavor } from "@grammyjs/i18n";
import type { Context } from "grammy";
import type { CreditService } from "../credits/service";
import type { Database } from "../db/connection";
import type { Chat, User } from "../db/schema";
import type { ModelTier } from "../llm/registry";

/** Custom properties injected by middleware */
export interface DerpContextProps {
	db: Database;
	dbUser: User;
	dbChat: Chat;
	creditService: CreditService;
	tier: ModelTier;
}

/** The full Derp context type */
export type DerpContext = Context &
	DerpContextProps &
	AutoChatActionFlavor &
	I18nFlavor;
