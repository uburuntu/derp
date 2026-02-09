/** Core chat handler — trigger detection, LLM call, tool execution, response delivery */

import { Composer, InputFile } from "grammy";
import type { DerpContext } from "../bot/context";
import { extractMedia } from "../common/extractor";
import { markdownToHtml, stripHtmlTags } from "../common/markdown";
import { derpMetrics, logger, withSpan } from "../common/observability";
import { appendFooterToChunks, splitMessage } from "../common/reply";
import { config, getBotId, getGoogleApiKeys } from "../config";
import { getBalances } from "../db/queries/credits";
import { getMembersWithUsers } from "../db/queries/members";
import { getRecentMessages, insertMessage } from "../db/queries/messages";
import type { MessageMetadata } from "../db/schema";
import { buildContext, type ContextParticipant } from "../llm/context-builder";
import { buildSystemPrompt } from "../llm/prompt";
import { GoogleLLMProvider } from "../llm/providers/google";
import type { ConversationMessage, MediaAttachment } from "../llm/types";
import { executeWithCreditGate } from "../tools/credit-gate";
import { toolRegistry } from "../tools/registry";
import type { ToolContext } from "../tools/types";

const chatComposer = new Composer<DerpContext>();

/** Check if a message should trigger the bot */
function shouldTrigger(ctx: DerpContext): boolean {
	const msg = ctx.message;
	if (!msg) return false;

	// Private chat — always trigger
	if (ctx.chat?.type === "private") return true;

	const text = msg.text ?? msg.caption ?? "";
	const botUsername = config.botUsername.toLowerCase();

	// Mention: @DerpRobot
	if (text.toLowerCase().includes(`@${botUsername}`)) return true;

	// Name mention: "derp" (case-insensitive, word boundary)
	if (/\bderp\b/i.test(text)) return true;

	// Direct reply to bot
	if (msg.reply_to_message?.from?.id === getBotId(config)) return true;

	// /derp command
	if (text.startsWith("/derp")) return true;

	return false;
}

/** Build a ToolContext from DerpContext */
function buildToolContext(ctx: DerpContext): ToolContext {
	return {
		db: ctx.db,
		user: ctx.dbUser,
		chat: ctx.dbChat,
		creditService: ctx.creditService,
		tier: ctx.tier,
		sendMessage: async (text: string) => {
			await ctx.reply(text);
		},
		sendPhoto: async (photo: Buffer, caption?: string) => {
			await ctx.replyWithPhoto(new InputFile(photo), { caption });
		},
		sendVoice: async (audio: Buffer) => {
			await ctx.replyWithVoice(new InputFile(audio));
		},
		sendVideo: async (video: Buffer, caption?: string) => {
			await ctx.replyWithVideo(new InputFile(video), { caption });
		},
		editMessage: async (messageId: number, text: string) => {
			await ctx.api.editMessageText(ctx.chat!.id, messageId, text);
		},
		deleteMessage: async (messageId: number) => {
			await ctx.api.deleteMessage(ctx.chat!.id, messageId);
		},
	};
}

/** Main chat handler */
chatComposer.on("message", async (ctx) => {
	if (!shouldTrigger(ctx)) return;
	if (!ctx.dbUser || !ctx.dbChat || !ctx.creditService) return;

	const startTime = Date.now();

	// Get orchestrator config (tier, model, context limit)
	const orchestratorConfig = await ctx.creditService.getOrchestratorConfig();
	const { tier, modelId, contextLimit } = orchestratorConfig;

	// Fetch recent messages from DB
	const threadId = ctx.message?.message_thread_id ?? null;
	const recentMessages = await getRecentMessages(
		ctx.db,
		ctx.dbChat.id,
		contextLimit,
		threadId,
	);

	// Get participant info with real user data (joined from users table)
	const userIds = [
		...new Set(
			recentMessages
				.map((m) => m.userId)
				.filter((id): id is string => id != null),
		),
	];

	const membersWithUsers = await getMembersWithUsers(
		ctx.db,
		ctx.dbChat.id,
		userIds,
	);

	// Build participant map with real names
	const members = new Map<string, ContextParticipant>();
	for (const m of membersWithUsers) {
		members.set(m.userId, {
			userId: m.userId,
			telegramId: m.telegramId,
			firstName: m.firstName,
			lastName: m.lastName,
			username: m.username,
			role: m.role,
		});
	}

	// Build compact context
	const builtContext = buildContext(
		recentMessages,
		members,
		config.botUsername,
	);

	// Build system prompt
	const systemPrompt = buildSystemPrompt(
		ctx.dbChat.personality ?? "default",
		ctx.dbChat.customPrompt,
		ctx.dbChat.memory,
	);

	const fullSystemPrompt = `${systemPrompt}\n\n${builtContext.participants}`;

	// Convert DB messages to LLM conversation format
	// Datetime goes here (dynamic section) — NOT in system prompt (cached stable prefix)
	const now = new Date();
	const conversationMessages: ConversationMessage[] = [
		{
			role: "user",
			content: `[Current time: ${now.toISOString()}]\n\n${builtContext.messageStream}`,
		},
	];

	// Extract media from current message
	const mediaAttachments: MediaAttachment[] = [];
	try {
		const media = await extractMedia(ctx.api, ctx.message!);
		for (const m of media) {
			mediaAttachments.push({
				type: m.type,
				data: m.data,
				mimeType: m.mimeType,
				fileId: m.fileId,
			});
		}
	} catch (err) {
		logger.error("media_extract_failed", {
			error: err instanceof Error ? err.message : String(err),
		});
	}

	// Also extract media from replied-to message
	if (ctx.message?.reply_to_message) {
		try {
			const replyMedia = await extractMedia(
				ctx.api,
				ctx.message.reply_to_message,
			);
			for (const m of replyMedia) {
				mediaAttachments.push({
					type: m.type,
					data: m.data,
					mimeType: m.mimeType,
					fileId: m.fileId,
				});
			}
		} catch (err) {
			logger.error("reply_media_extract_failed", {
				error: err instanceof Error ? err.message : String(err),
			});
		}
	}

	// Get tool schemas for the LLM
	const toolSchemas = toolRegistry.getLLMToolSchemas();
	const toolContext = buildToolContext(ctx);

	// Create LLM provider
	const apiKeys = getGoogleApiKeys(config);
	const provider = new GoogleLLMProvider(apiKeys, config.googleApiPaidKey);

	const toolsUsed: string[] = [];
	let creditsSpent = 0;
	let creditSource: string | undefined;
	let creditsRemaining: number | null = null;

	try {
		const result = await provider.chatWithTools(
			{
				model: modelId,
				systemPrompt: fullSystemPrompt,
				messages: conversationMessages,
				tools: toolSchemas.length > 0 ? toolSchemas : undefined,
				media: mediaAttachments.length > 0 ? mediaAttachments : undefined,
			},
			async (toolName, args) => {
				const tool = toolRegistry.getTool(toolName);
				if (!tool) {
					return { error: `Unknown tool: ${toolName}` };
				}

				const parsed = tool.parameters.safeParse(args);
				if (!parsed.success) {
					return { error: `Invalid parameters: ${parsed.error.message}` };
				}

				const toolResult = await executeWithCreditGate(
					tool,
					parsed.data,
					toolContext,
				);

				toolsUsed.push(toolName);
				if (toolResult.creditResult) {
					creditsSpent += toolResult.creditResult.creditsToDeduct;
					if (toolResult.creditResult.creditsRemaining != null) {
						creditsRemaining = toolResult.creditResult.creditsRemaining;
					}
					if (!creditSource && toolResult.creditResult.source !== "rejected") {
						creditSource = toolResult.creditResult.source;
					}
				}

				if (toolResult.handled) {
					return { result: "Response sent directly to chat." };
				}
				if (toolResult.error) {
					return { error: toolResult.error };
				}
				return { result: toolResult.text ?? "Done." };
			},
		);

		// Get remaining balance for footer if not set by tool calls
		if (creditsRemaining == null && creditsSpent > 0) {
			const balances = await getBalances(
				ctx.db,
				ctx.dbUser.telegramId,
				ctx.dbChat.telegramId,
			);
			creditsRemaining =
				creditSource === "chat" ? balances.chatCredits : balances.userCredits;
		}

		const durationMs = Date.now() - startTime;
		const metadata: MessageMetadata = {
			model: modelId,
			tier,
			inputTokens: result.usage.inputTokens,
			outputTokens: result.usage.outputTokens,
			cacheHitTokens: result.usage.cacheHitTokens,
			toolsUsed: toolsUsed.length > 0 ? toolsUsed : undefined,
			creditsSpent: creditsSpent > 0 ? creditsSpent : undefined,
			creditSource,
			durationMs,
		};

		// Send the response and store in DB
		const responseText = result.text;
		if (!responseText && result.images && result.images.length > 0) {
			const img = result.images[0]!;
			const sent = await ctx.replyWithPhoto(new InputFile(img.data), {
				reply_to_message_id: ctx.message?.message_id,
			});
			await insertMessage(ctx.db, {
				chatId: ctx.dbChat.id,
				userId: null,
				telegramMessageId: sent.message_id,
				threadId,
				direction: "out",
				contentType: "photo",
				text: sent.caption ?? null,
				attachmentType: "image",
				metadata,
				telegramDate: new Date(sent.date * 1000),
			});
		} else if (responseText) {
			const chunks = splitMessage(responseText);
			const withFooter = appendFooterToChunks(
				chunks,
				creditsSpent,
				creditsRemaining ?? 0,
			);

			for (let i = 0; i < withFooter.length; i++) {
				let sent;
				const html = markdownToHtml(withFooter[i]!);
				try {
					sent = await ctx.reply(html, {
						parse_mode: "HTML",
						reply_to_message_id: i === 0 ? ctx.message?.message_id : undefined,
					});
				} catch {
					// HTML parse error → fallback to plain text
					try {
						sent = await ctx.reply(stripHtmlTags(html), {
							reply_to_message_id:
								i === 0 ? ctx.message?.message_id : undefined,
						});
					} catch {
						sent = await ctx.reply(stripHtmlTags(html));
					}
				}

				// Store the last chunk (with metadata) — first chunks are continuations
				if (i === withFooter.length - 1) {
					await insertMessage(ctx.db, {
						chatId: ctx.dbChat.id,
						userId: null,
						telegramMessageId: sent.message_id,
						threadId,
						direction: "out",
						contentType: "text",
						text: responseText,
						metadata,
						telegramDate: new Date(sent.date * 1000),
					});
				}
			}
		}

		logger.info("chat_response", {
			model: modelId,
			tier,
			inputTokens: result.usage.inputTokens,
			outputTokens: result.usage.outputTokens,
			cacheHitTokens: result.usage.cacheHitTokens,
			toolsUsed,
			creditsSpent,
			creditSource,
			durationMs,
			chatId: ctx.dbChat.telegramId,
			userId: ctx.dbUser.telegramId,
		});

		derpMetrics.contextTokens.record(recentMessages.length, { tier });
	} catch (err) {
		logger.error("chat_llm_failed", {
			error: err instanceof Error ? err.message : String(err),
			chatId: ctx.dbChat.telegramId,
			userId: ctx.dbUser.telegramId,
		});
		await ctx.reply(ctx.t("chat-error"), {
			reply_to_message_id: ctx.message?.message_id,
		});
	}
});

// ── Memory Commands ─────────────────────────────────────────────────────────

chatComposer.command("memory", async (ctx) => {
	if (!ctx.dbChat) return;
	const replyTo = ctx.message?.message_id;
	const memory = ctx.dbChat.memory;
	if (!memory) {
		await ctx.reply(ctx.t("memory-none"), { reply_to_message_id: replyTo });
		return;
	}
	await ctx.reply(`📝 <b>Chat Memory</b>\n\n${memory}`, {
		parse_mode: "HTML",
		reply_to_message_id: replyTo,
	});
});

chatComposer.command("memory_set", async (ctx) => {
	if (!ctx.dbChat) return;
	const replyTo = ctx.message?.message_id;
	const text = ctx.match;
	if (!text) {
		await ctx.reply(ctx.t("memory-usage"), { reply_to_message_id: replyTo });
		return;
	}

	const settings = ctx.dbChat.settings;
	if (settings?.memoryAccess === "admins" && ctx.chat?.type !== "private") {
		const chatMember = await ctx.getChatMember(ctx.from!.id);
		if (
			chatMember.status !== "administrator" &&
			chatMember.status !== "creator"
		) {
			await ctx.reply(ctx.t("memory-admin-only", { action: "set" }), {
				reply_to_message_id: replyTo,
			});
			return;
		}
	}

	const { updateChatMemory } = await import("../db/queries/chats");
	await updateChatMemory(ctx.db, ctx.dbChat.id, text.slice(0, 4096));
	await ctx.reply(ctx.t("memory-updated"), { reply_to_message_id: replyTo });
});

chatComposer.command("memory_clear", async (ctx) => {
	if (!ctx.dbChat) return;
	const replyTo = ctx.message?.message_id;

	const settings = ctx.dbChat.settings;
	if (settings?.memoryAccess === "admins" && ctx.chat?.type !== "private") {
		const chatMember = await ctx.getChatMember(ctx.from!.id);
		if (
			chatMember.status !== "administrator" &&
			chatMember.status !== "creator"
		) {
			await ctx.reply(ctx.t("memory-admin-only", { action: "clear" }), {
				reply_to_message_id: replyTo,
			});
			return;
		}
	}

	const { updateChatMemory } = await import("../db/queries/chats");
	await updateChatMemory(ctx.db, ctx.dbChat.id, null);
	await ctx.reply(ctx.t("memory-cleared"), { reply_to_message_id: replyTo });
});

chatComposer.command("derp", async (_ctx) => {
	// Handled by the message handler above via shouldTrigger
});

export { chatComposer };
