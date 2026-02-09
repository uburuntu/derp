/** Settings handler — interactive menu for bot configuration */

import { Menu } from "@grammyjs/menu";
import { Composer } from "grammy";
import type { DerpContext } from "../bot/context";
import { updateChatPersonality, updateChatSettings } from "../db/queries/chats";
import { getBalances } from "../db/queries/credits";

// ── Settings Menu ───────────────────────────────────────────────────────────

const settingsMenu = new Menu<DerpContext>("settings")
	.text("Personality", (ctx) => ctx.menu.nav("personality"))
	.row()
	.text("Language", (ctx) => ctx.menu.nav("language"))
	.row()
	.text("Permissions", (ctx) => ctx.menu.nav("permissions"))
	.row()
	.text("Memory", (ctx) => ctx.menu.nav("memory-menu"))
	.row()
	.text("Balance", async (ctx) => {
		if (!ctx.dbUser || !ctx.dbChat) return;
		const { userCredits, chatCredits } = await getBalances(
			ctx.db,
			ctx.dbUser.telegramId,
			ctx.dbChat.telegramId,
		);
		const sub = ctx.dbUser.subscriptionTier
			? `\nSubscription: ${ctx.dbUser.subscriptionTier.toUpperCase()}`
			: "";
		await ctx.answerCallbackQuery();
		await ctx.reply(
			`Your credits: ${userCredits}\nChat pool: ${chatCredits}${sub}\n\nUse /buy to get more.`,
		);
	})
	.row()
	.text("Close", (ctx) => ctx.deleteMessage());

// ── Personality submenu ─────────────────────────────────────────────────────

const personalityMenu = new Menu<DerpContext>("personality")
	.text("Default", async (ctx) => {
		if (!ctx.dbChat) return;
		await updateChatPersonality(ctx.db, ctx.dbChat.id, "default");
		await ctx.answerCallbackQuery("Personality set to Default");
	})
	.text("Professional", async (ctx) => {
		if (!ctx.dbChat) return;
		await updateChatPersonality(ctx.db, ctx.dbChat.id, "professional");
		await ctx.answerCallbackQuery("Personality set to Professional");
	})
	.row()
	.text("Casual", async (ctx) => {
		if (!ctx.dbChat) return;
		await updateChatPersonality(ctx.db, ctx.dbChat.id, "casual");
		await ctx.answerCallbackQuery("Personality set to Casual");
	})
	.text("Creative", async (ctx) => {
		if (!ctx.dbChat) return;
		await updateChatPersonality(ctx.db, ctx.dbChat.id, "creative");
		await ctx.answerCallbackQuery("Personality set to Creative");
	})
	.row()
	.text("Custom (subscribers)", async (ctx) => {
		if (!ctx.dbUser || !ctx.dbChat) return;
		if (!ctx.dbUser.subscriptionTier) {
			await ctx.answerCallbackQuery(
				"Custom prompts require a subscription. Use /buy",
			);
			return;
		}
		await ctx.answerCallbackQuery();
		await ctx.reply(
			"Send your custom system prompt as a reply to this message. Max 2000 characters.\n\nCurrent: " +
				(ctx.dbChat.customPrompt ?? "(none)"),
		);
	})
	.row()
	.text("« Back", (ctx) => ctx.menu.nav("settings"));

// ── Language submenu ────────────────────────────────────────────────────────

const languageMenu = new Menu<DerpContext>("language")
	.text("English", async (ctx) => {
		if (!ctx.dbChat) return;
		const { eq } = await import("drizzle-orm");
		const { chats } = await import("../db/schema");
		await ctx.db
			.update(chats)
			.set({ languageCode: "en" })
			.where(eq(chats.id, ctx.dbChat.id));
		await ctx.answerCallbackQuery("Language set to English");
	})
	.text("Русский", async (ctx) => {
		if (!ctx.dbChat) return;
		const { eq } = await import("drizzle-orm");
		const { chats } = await import("../db/schema");
		await ctx.db
			.update(chats)
			.set({ languageCode: "ru" })
			.where(eq(chats.id, ctx.dbChat.id));
		await ctx.answerCallbackQuery("Язык установлен: Русский");
	})
	.row()
	.text("Auto-detect", async (ctx) => {
		if (!ctx.dbChat) return;
		const { eq } = await import("drizzle-orm");
		const { chats } = await import("../db/schema");
		await ctx.db
			.update(chats)
			.set({ languageCode: null })
			.where(eq(chats.id, ctx.dbChat.id));
		await ctx.answerCallbackQuery("Language: auto-detect from messages");
	})
	.row()
	.text("« Back", (ctx) => ctx.menu.nav("settings"));

// ── Permissions submenu ─────────────────────────────────────────────────────

const permissionsMenu = new Menu<DerpContext>("permissions")
	.text(
		(ctx) => `Memory: ${ctx.dbChat?.settings?.memoryAccess ?? "admins"}`,
		async (ctx) => {
			if (!ctx.dbChat) return;
			const current = ctx.dbChat.settings?.memoryAccess ?? "admins";
			const next = current === "admins" ? "everyone" : "admins";
			await updateChatSettings(ctx.db, ctx.dbChat.id, {
				memoryAccess: next,
			});
			ctx.dbChat.settings = { ...ctx.dbChat.settings!, memoryAccess: next };
			ctx.menu.update();
			await ctx.answerCallbackQuery(`Memory access: ${next}`);
		},
	)
	.row()
	.text(
		(ctx) => `Reminders: ${ctx.dbChat?.settings?.remindersAccess ?? "admins"}`,
		async (ctx) => {
			if (!ctx.dbChat) return;
			const current = ctx.dbChat.settings?.remindersAccess ?? "admins";
			const next = current === "admins" ? "everyone" : "admins";
			await updateChatSettings(ctx.db, ctx.dbChat.id, {
				remindersAccess: next,
			});
			ctx.dbChat.settings = {
				...ctx.dbChat.settings!,
				remindersAccess: next,
			};
			ctx.menu.update();
			await ctx.answerCallbackQuery(`Reminders access: ${next}`);
		},
	)
	.row()
	.text("« Back", (ctx) => ctx.menu.nav("settings"));

// ── Memory submenu ──────────────────────────────────────────────────────────

const memoryMenu = new Menu<DerpContext>("memory-menu")
	.text("View Memory", async (ctx) => {
		if (!ctx.dbChat) return;
		const memory = ctx.dbChat.memory;
		if (!memory) {
			await ctx.answerCallbackQuery("No memory stored");
			return;
		}
		await ctx.answerCallbackQuery();
		await ctx.reply(`Chat memory:\n\n${memory}`);
	})
	.row()
	.text("Clear Memory", async (ctx) => {
		if (!ctx.dbChat) return;
		const { updateChatMemory } = await import("../db/queries/chats");
		await updateChatMemory(ctx.db, ctx.dbChat.id, null);
		ctx.dbChat.memory = null;
		await ctx.answerCallbackQuery("Memory cleared");
	})
	.row()
	.text("« Back", (ctx) => ctx.menu.nav("settings"));

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

	const personality = ctx.dbChat.personality ?? "default";
	const lang = ctx.dbChat.languageCode ?? "auto";
	const memAccess = ctx.dbChat.settings?.memoryAccess ?? "admins";
	const remAccess = ctx.dbChat.settings?.remindersAccess ?? "admins";

	await ctx.reply(
		`⚙️ <b>Settings</b>\n\n` +
			`<b>Personality:</b> ${personality}\n` +
			`<b>Language:</b> ${lang}\n` +
			`<b>Memory access:</b> ${memAccess}\n` +
			`<b>Reminders access:</b> ${remAccess}`,
		{
			parse_mode: "HTML",
			reply_markup: settingsMenu,
			reply_to_message_id: ctx.message?.message_id,
		},
	);
});

export { settingsComposer };
