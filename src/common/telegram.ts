/** Telegram display name and mention utilities */

import type { User as TelegramUser } from "grammy/types";

/** Build a display name from a Telegram user */
export function displayName(user: TelegramUser): string {
	if (user.last_name) {
		return `${user.first_name} ${user.last_name}`;
	}
	return user.first_name;
}

/** Build a MarkdownV2-safe mention link */
export function userMention(user: TelegramUser): string {
	const name = escapeMarkdownV2(displayName(user));
	return `[${name}](tg://user?id=${user.id})`;
}

/** Escape special characters for Telegram MarkdownV2 */
export function escapeMarkdownV2(text: string): string {
	return text.replace(/([_*[\]()~`>#+\-=|{}.!\\])/g, "\\$1");
}

/** Telegram message effect IDs for sendMessage */
export const MESSAGE_EFFECTS = {
	party: "5046509860389126442",
	fire: "5107584321108051014",
	heart: "5159385139981059251",
	thumbsUp: "5104841245755180586",
} as const;

/** Build display name from DB user fields */
export function displayNameFromDb(
	firstName: string,
	lastName: string | null,
): string {
	if (lastName) return `${firstName} ${lastName}`;
	return firstName;
}
