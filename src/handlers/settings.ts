/** Settings handler — interactive menu for bot configuration */

import { Menu, type MenuFlavor } from "@grammyjs/menu";
import { eq } from "drizzle-orm";
import { Composer, type NextFunction } from "grammy";
import type { DerpContext } from "../bot/context";
import { escapeHtml } from "../common/sanitize";
import { hasActiveSubscription } from "../credits/service";
import {
	updateChatMemory,
	updateChatPersonality,
	updateChatSettings,
} from "../db/queries/chats";
import { getBalances } from "../db/queries/credits";
import { chats } from "../db/schema";
import {
	getLocaleForContext,
	type SupportedLocale,
	toSupportedLocale,
} from "../i18n/index";

// ── Helpers ────────────────────────────────────────────────────────────────

const CUSTOM_PROMPT_MAX_LENGTH = 2000;
const CUSTOM_PROMPT_TTL_MS = 10 * 60 * 1000;

type AccessSetting = "admins" | "everyone";
type Personality =
	| "default"
	| "professional"
	| "casual"
	| "creative"
	| "custom";
type SettingsMenuContext = DerpContext & MenuFlavor;

interface PendingCustomPrompt {
	messageId: number;
	expiresAt: number;
}

const pendingCustomPrompts = new Map<string, PendingCustomPrompt>();

function pendingKey(ctx: DerpContext): string | null {
	const chatId = ctx.chat?.id ?? ctx.dbChat?.telegramId;
	const userId = ctx.from?.id ?? ctx.dbUser?.telegramId;
	if (chatId == null || userId == null) return null;
	return `${chatId}:${userId}`;
}

function getPendingCustomPrompt(key: string): PendingCustomPrompt | null {
	const pending = pendingCustomPrompts.get(key);
	if (!pending) return null;
	if (pending.expiresAt < Date.now()) {
		pendingCustomPrompts.delete(key);
		return null;
	}
	return pending;
}

async function isChatAdmin(ctx: DerpContext): Promise<boolean> {
	if (ctx.chat?.type === "private") return true;
	if (!ctx.from) return false;

	try {
		const member = await ctx.getChatMember(ctx.from.id);
		return member.status === "administrator" || member.status === "creator";
	} catch {
		return false;
	}
}

async function ensureCanMutateSettings(ctx: DerpContext): Promise<boolean> {
	if (await isChatAdmin(ctx)) return true;

	if (ctx.callbackQuery) {
		await ctx.answerCallbackQuery(ctx.t("settings-admin-only"));
	} else {
		await ctx.reply(ctx.t("settings-admin-only"), {
			parse_mode: "HTML",
			reply_to_message_id: ctx.message?.message_id,
		});
	}
	return false;
}

function accessLabel(ctx: DerpContext, access: AccessSetting): string {
	return ctx.t(`settings-access-${access}`);
}

function getAccessSettings(ctx: DerpContext): {
	memoryAccess: AccessSetting;
	remindersAccess: AccessSetting;
} {
	return {
		memoryAccess: ctx.dbChat?.settings?.memoryAccess ?? "admins",
		remindersAccess: ctx.dbChat?.settings?.remindersAccess ?? "admins",
	};
}

function personalityLabel(
	ctx: DerpContext,
	personality: string | null,
): string {
	switch (personality) {
		case "professional":
		case "casual":
		case "creative":
		case "custom":
			return ctx.t(`settings-personality-${personality}`);
		default:
			return ctx.t("settings-personality-default");
	}
}

function localeLabel(ctx: DerpContext, locale: SupportedLocale): string {
	return ctx.t(`settings-language-${locale}`);
}

function languageLabel(ctx: DerpContext, languageCode: string | null): string {
	if (!languageCode) return ctx.t("settings-language-auto");

	const supported = toSupportedLocale(languageCode);
	return supported ? localeLabel(ctx, supported) : languageCode;
}

function settingsSummary(ctx: DerpContext): string {
	const personality = personalityLabel(
		ctx,
		ctx.dbChat?.personality ?? "default",
	);
	const lang = languageLabel(ctx, ctx.dbChat?.languageCode ?? null);
	const access = getAccessSettings(ctx);

	return (
		`⚙️ <b>${ctx.t("settings-title")}</b>\n\n` +
		`${ctx.t("settings-personality", { personality })}\n` +
		`${ctx.t("settings-language", { lang })}\n` +
		`${ctx.t("settings-memory-access", {
			access: accessLabel(ctx, access.memoryAccess),
		})}\n` +
		`${ctx.t("settings-reminders-access", {
			access: accessLabel(ctx, access.remindersAccess),
		})}`
	);
}

async function setPresetPersonality(
	ctx: SettingsMenuContext,
	personality: Exclude<Personality, "custom">,
): Promise<void> {
	if (!ctx.dbChat) return;
	if (!(await ensureCanMutateSettings(ctx))) return;

	await updateChatPersonality(ctx.db, ctx.dbChat.id, personality);
	ctx.dbChat.personality = personality;
	ctx.dbChat.customPrompt = null;
	ctx.menu.update();
	await ctx.answerCallbackQuery(
		ctx.t("settings-personality-set", {
			personality: personalityLabel(ctx, personality),
		}),
	);
}

async function startCustomPromptFlow(ctx: DerpContext): Promise<void> {
	if (!ctx.dbUser || !ctx.dbChat) return;
	if (!(await ensureCanMutateSettings(ctx))) return;

	if (!hasActiveSubscription(ctx.dbUser)) {
		await ctx.answerCallbackQuery(ctx.t("settings-custom-sub-required"));
		return;
	}

	await ctx.answerCallbackQuery();
	const current = escapeHtml(
		ctx.dbChat.customPrompt ?? ctx.t("settings-custom-current-none"),
	);
	const sent = await ctx.reply(
		ctx.t("settings-custom-prompt", {
			current,
			max: CUSTOM_PROMPT_MAX_LENGTH,
		}),
		{
			parse_mode: "HTML",
			reply_markup: {
				force_reply: true,
				selective: true,
				input_field_placeholder: ctx.t("settings-custom-placeholder"),
			},
		},
	);
	const key = pendingKey(ctx);
	if (key) {
		pendingCustomPrompts.set(key, {
			messageId: sent.message_id,
			expiresAt: Date.now() + CUSTOM_PROMPT_TTL_MS,
		});
	}
}

async function setChatLanguage(
	ctx: SettingsMenuContext,
	languageCode: SupportedLocale | null,
): Promise<void> {
	if (!ctx.dbChat) return;
	if (!(await ensureCanMutateSettings(ctx))) return;

	await ctx.db
		.update(chats)
		.set({ languageCode })
		.where(eq(chats.id, ctx.dbChat.id));

	ctx.dbChat.languageCode = languageCode;
	ctx.i18n.useLocale(languageCode ?? getLocaleForContext(ctx));
	ctx.menu.update();

	const message =
		languageCode == null
			? ctx.t("settings-lang-auto")
			: ctx.t("settings-lang-set", {
					lang: localeLabel(ctx, languageCode),
				});
	await ctx.answerCallbackQuery(message);
}

async function toggleAccess(
	ctx: SettingsMenuContext,
	key: "memoryAccess" | "remindersAccess",
): Promise<void> {
	if (!ctx.dbChat) return;
	if (!(await ensureCanMutateSettings(ctx))) return;

	const currentSettings = getAccessSettings(ctx);
	const current = currentSettings[key];
	const next: AccessSetting = current === "admins" ? "everyone" : "admins";
	const updated = { ...currentSettings, [key]: next };

	await updateChatSettings(ctx.db, ctx.dbChat.id, { [key]: next });
	ctx.dbChat.settings = updated;
	ctx.menu.update();

	const answerKey =
		key === "memoryAccess"
			? "settings-memory-access-set"
			: "settings-reminders-access-set";
	await ctx.answerCallbackQuery(
		ctx.t(answerKey, { access: accessLabel(ctx, next) }),
	);
}

async function handleCustomPromptReply(
	ctx: DerpContext,
	next: NextFunction,
): Promise<void> {
	const key = pendingKey(ctx);
	if (!key) return next();

	const pending = getPendingCustomPrompt(key);
	if (!pending) return next();

	const isReplyToPrompt =
		ctx.message?.reply_to_message?.message_id === pending.messageId;
	const isPrivateChat = ctx.chat?.type === "private";
	if (!isReplyToPrompt && !isPrivateChat) return next();

	const text = ctx.message?.text?.trim();
	if (!text) return next();

	if (text === "/cancel") {
		pendingCustomPrompts.delete(key);
		await ctx.reply(ctx.t("settings-custom-cancelled"), {
			parse_mode: "HTML",
			reply_to_message_id: ctx.message?.message_id,
		});
		return;
	}

	if (!ctx.dbUser || !ctx.dbChat) return next();
	if (!(await ensureCanMutateSettings(ctx))) return;

	if (!hasActiveSubscription(ctx.dbUser)) {
		pendingCustomPrompts.delete(key);
		await ctx.reply(ctx.t("settings-custom-sub-required"), {
			parse_mode: "HTML",
			reply_to_message_id: ctx.message?.message_id,
		});
		return;
	}

	if (text.length > CUSTOM_PROMPT_MAX_LENGTH) {
		await ctx.reply(
			ctx.t("settings-custom-too-long", {
				max: CUSTOM_PROMPT_MAX_LENGTH,
			}),
			{
				parse_mode: "HTML",
				reply_to_message_id: ctx.message?.message_id,
			},
		);
		return;
	}

	await updateChatPersonality(ctx.db, ctx.dbChat.id, "custom", text);
	ctx.dbChat.personality = "custom";
	ctx.dbChat.customPrompt = text;
	pendingCustomPrompts.delete(key);
	await ctx.reply(ctx.t("settings-custom-saved"), {
		parse_mode: "HTML",
		reply_to_message_id: ctx.message?.message_id,
	});
}

// ── Settings Menu ───────────────────────────────────────────────────────────

const settingsMenu = new Menu<DerpContext>("settings")
	.text(
		(ctx) => ctx.t("settings-menu-personality"),
		(ctx) => ctx.menu.nav("personality"),
	)
	.row()
	.text(
		(ctx) => ctx.t("settings-menu-language"),
		(ctx) => ctx.menu.nav("language"),
	)
	.row()
	.text(
		(ctx) => ctx.t("settings-menu-permissions"),
		(ctx) => ctx.menu.nav("permissions"),
	)
	.row()
	.text(
		(ctx) => ctx.t("settings-menu-memory"),
		(ctx) => ctx.menu.nav("memory-menu"),
	)
	.row()
	.text(
		(ctx) => ctx.t("settings-menu-balance"),
		async (ctx) => {
			if (!ctx.dbUser || !ctx.dbChat) return;
			const { userCredits, chatCredits } = await getBalances(
				ctx.db,
				ctx.dbUser.telegramId,
				ctx.dbChat.telegramId,
			);
			const tier = ctx.dbUser.subscriptionTier?.toUpperCase() ?? "";
			const subscription = hasActiveSubscription(ctx.dbUser)
				? ctx.t("settings-balance-subscription", {
						tier,
					})
				: "";
			await ctx.answerCallbackQuery();
			await ctx.reply(
				ctx.t("settings-balance-info", {
					userCredits,
					chatCredits,
					subscription,
				}),
				{ parse_mode: "HTML" },
			);
		},
	)
	.row()
	.text(
		(ctx) => ctx.t("settings-close"),
		(ctx) => ctx.deleteMessage(),
	);

// ── Personality submenu ─────────────────────────────────────────────────────

const personalityMenu = new Menu<DerpContext>("personality")
	.text(
		(ctx) => ctx.t("settings-personality-default"),
		(ctx) => setPresetPersonality(ctx, "default"),
	)
	.text(
		(ctx) => ctx.t("settings-personality-professional"),
		(ctx) => setPresetPersonality(ctx, "professional"),
	)
	.row()
	.text(
		(ctx) => ctx.t("settings-personality-casual"),
		(ctx) => setPresetPersonality(ctx, "casual"),
	)
	.text(
		(ctx) => ctx.t("settings-personality-creative"),
		(ctx) => setPresetPersonality(ctx, "creative"),
	)
	.row()
	.text(
		(ctx) => ctx.t("settings-personality-custom-button"),
		startCustomPromptFlow,
	)
	.row()
	.text(
		(ctx) => ctx.t("settings-back"),
		(ctx) => ctx.menu.nav("settings"),
	);

// ── Language submenu ────────────────────────────────────────────────────────

const languageMenu = new Menu<DerpContext>("language")
	.text(
		(ctx) => ctx.t("settings-language-en"),
		(ctx) => setChatLanguage(ctx, "en"),
	)
	.text(
		(ctx) => ctx.t("settings-language-ru"),
		(ctx) => setChatLanguage(ctx, "ru"),
	)
	.row()
	.text(
		(ctx) => ctx.t("settings-language-auto"),
		(ctx) => setChatLanguage(ctx, null),
	)
	.row()
	.text(
		(ctx) => ctx.t("settings-back"),
		(ctx) => ctx.menu.nav("settings"),
	);

// ── Permissions submenu ─────────────────────────────────────────────────────

const permissionsMenu = new Menu<DerpContext>("permissions")
	.text(
		(ctx) =>
			ctx.t("settings-menu-memory-access", {
				access: accessLabel(ctx, getAccessSettings(ctx).memoryAccess),
			}),
		(ctx) => toggleAccess(ctx, "memoryAccess"),
	)
	.row()
	.text(
		(ctx) =>
			ctx.t("settings-menu-reminders-access", {
				access: accessLabel(ctx, getAccessSettings(ctx).remindersAccess),
			}),
		(ctx) => toggleAccess(ctx, "remindersAccess"),
	)
	.row()
	.text(
		(ctx) => ctx.t("settings-back"),
		(ctx) => ctx.menu.nav("settings"),
	);

// ── Memory submenu ──────────────────────────────────────────────────────────

const memoryMenu = new Menu<DerpContext>("memory-menu")
	.text(
		(ctx) => ctx.t("settings-memory-view-button"),
		async (ctx) => {
			if (!ctx.dbChat) return;
			const memory = ctx.dbChat.memory;
			if (!memory) {
				await ctx.answerCallbackQuery(ctx.t("settings-memory-none"));
				return;
			}
			await ctx.answerCallbackQuery();
			await ctx.reply(
				`📝 <b>${ctx.t("settings-memory-title")}</b>\n\n${escapeHtml(memory)}`,
				{ parse_mode: "HTML" },
			);
		},
	)
	.row()
	.text(
		(ctx) => ctx.t("settings-memory-clear-button"),
		async (ctx) => {
			if (!ctx.dbChat) return;
			if (!(await ensureCanMutateSettings(ctx))) return;

			await updateChatMemory(ctx.db, ctx.dbChat.id, null);
			ctx.dbChat.memory = null;
			await ctx.answerCallbackQuery(ctx.t("settings-memory-cleared"));
		},
	)
	.row()
	.text(
		(ctx) => ctx.t("settings-back"),
		(ctx) => ctx.menu.nav("settings"),
	);

// Register submenus
settingsMenu.register(personalityMenu);
settingsMenu.register(languageMenu);
settingsMenu.register(permissionsMenu);
settingsMenu.register(memoryMenu);

// ── Composer ────────────────────────────────────────────────────────────────

const settingsComposer = new Composer<DerpContext>();
settingsComposer.use(settingsMenu);

settingsComposer.command("settings", async (ctx) => {
	if (!ctx.dbChat) return;

	await ctx.reply(settingsSummary(ctx), {
		parse_mode: "HTML",
		reply_markup: settingsMenu,
		reply_to_message_id: ctx.message?.message_id,
	});
});

settingsComposer.on("message:text", handleCustomPromptReply);

export { settingsComposer };
