/** Core chat handler — trigger detection, LLM call, tool execution, response delivery */

import { Composer, InputFile } from "grammy";
import type { DerpContext } from "../bot/context";
import { notifyAdmins } from "../common/admin-notify";
import {
	bestPhotoFileId,
	downloadTelegramFile,
	extractMedia,
} from "../common/extractor";
import { markdownToHtml, stripHtmlTags } from "../common/markdown";
import {
	derpMetrics,
	logger,
	recordHandledFailure,
} from "../common/observability";
import {
	appendFooterToChunks,
	captionPartsForMedia,
	replyHtml,
	splitMessage,
} from "../common/reply";
import { escapeHtml } from "../common/sanitize";
import { config, getBotId, getGoogleApiKeys } from "../config";
import { creditUsdFloor, TARGET_GROSS_MARGIN } from "../credits/economy";
import {
	hasActiveSubscription,
	STANDARD_CHAT_CREDITS,
	STANDARD_CHAT_TOOL_NAME,
} from "../credits/service";
import { getBalances, markLedgerSpendStatus } from "../db/queries/credits";
import { recordFreeChatUsage } from "../db/queries/finance";
import { getMembersWithUsers } from "../db/queries/members";
import { getRecentMessages, insertMessage } from "../db/queries/messages";
import type { MessageMetadata } from "../db/schema";
import { buildContext, type ContextParticipant } from "../llm/context-builder";
import { buildSystemPrompt, detectTaskSpecialist } from "../llm/prompt";
import { GoogleLLMProvider } from "../llm/providers/google";
import { OpenRouterProvider } from "../llm/providers/openrouter";
import {
	CONTEXT_LIMITS,
	getDefaultModel,
	ModelCapability,
	ModelTier,
} from "../llm/registry";
import type { ConversationMessage, MediaAttachment } from "../llm/types";
import { normalizeUserPreferences } from "../preferences/user";
import { executeWithCreditGate } from "../tools/credit-gate";
import { toolRegistry } from "../tools/registry";
import type { ToolContext } from "../tools/types";

const chatComposer = new Composer<DerpContext>();
const FREE_CHAT_DAILY_LIMIT = 25;
const CHAT_CONTEXT_BUDGET: Record<ModelTier.FREE | ModelTier.STANDARD, number> =
	{
		[ModelTier.FREE]: 8_000,
		[ModelTier.STANDARD]: 12_000,
	};
const CHAT_MESSAGE_CHAR_BUDGET = 1_200;

function fallbackCostCapMicros(): number {
	return Math.round(
		STANDARD_CHAT_CREDITS *
			creditUsdFloor() *
			(1 - TARGET_GROSS_MARGIN) *
			1_000_000,
	);
}

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

function canUseAdminGatedSetting(
	setting: "admins" | "everyone" | undefined,
	admin: boolean,
): boolean {
	return setting !== "admins" || admin;
}

async function getParticipantProfilePhoto(
	ctx: DerpContext,
	participantRefs: Map<string, ContextParticipant>,
	participantRef: string,
): Promise<MediaAttachment | null> {
	const participant = participantRefs.get(participantRef.trim().toLowerCase());
	if (!participant) return null;

	const photos = await ctx.api.raw.getUserProfilePhotos({
		user_id: participant.telegramId,
		limit: 1,
	});
	const sizes = photos.photos[0];
	if (!sizes) return null;

	const fileId = bestPhotoFileId(sizes);
	if (!fileId) return null;

	return {
		type: "image",
		data: await downloadTelegramFile(ctx.api, fileId),
		mimeType: "image/jpeg",
		fileId,
	};
}

async function sendCaptionFollowUps(
	ctx: DerpContext,
	followUpChunks: string[],
): Promise<void> {
	for (const chunk of followUpChunks) {
		await ctx.reply(chunk, {
			message_thread_id: ctx.message?.message_thread_id,
		});
	}
}

type ProviderResultMetadata = {
	providerCallIds?: string[];
	costMicros?: number;
};

function mergeProviderResultMetadata(
	target: ProviderResultMetadata,
	result: ProviderResultMetadata,
): void {
	if (result.providerCallIds?.length) {
		const ids = new Set([
			...(target.providerCallIds ?? []),
			...result.providerCallIds,
		]);
		target.providerCallIds = [...ids];
	}
	if (result.costMicros && result.costMicros > 0) {
		target.costMicros = (target.costMicros ?? 0) + result.costMicros;
	}
}

function publicCreditSource(
	ctx: DerpContext,
	source?: string,
): string | undefined {
	const groupChat =
		ctx.chat?.type === "group" || ctx.chat?.type === "supergroup";
	return source === "user" && groupChat ? "user_private" : source;
}

/** Build a ToolContext from DerpContext */
async function buildToolContext(
	ctx: DerpContext,
	participantRefs: Map<string, ContextParticipant>,
	replyMedia: MediaAttachment[],
	providerMeta: ProviderResultMetadata,
): Promise<ToolContext> {
	const admin = await isChatAdmin(ctx);
	const replyOptions = {
		message_thread_id: ctx.message?.message_thread_id,
		reply_to_message_id: ctx.message?.message_id,
	};
	return {
		db: ctx.db,
		user: ctx.dbUser,
		chat: ctx.dbChat,
		creditService: ctx.creditService,
		tier: ctx.tier,
		isChatAdmin: admin,
		isGroupChat: ctx.chat?.type === "group" || ctx.chat?.type === "supergroup",
		canManageMemory: canUseAdminGatedSetting(
			ctx.dbChat.settings?.memoryAccess,
			admin,
		),
		canManageReminders: canUseAdminGatedSetting(
			ctx.dbChat.settings?.remindersAccess,
			admin,
		),
		participants: participantRefs,
		getParticipantProfilePhoto: (participantRef) =>
			getParticipantProfilePhoto(ctx, participantRefs, participantRef),
		recordProviderResult: (result) =>
			mergeProviderResultMetadata(providerMeta, result),
		sendMessage: async (text: string) => {
			await ctx.reply(text, replyOptions);
		},
		sendPhoto: async (photo: Buffer, caption?: string) => {
			const mediaCaption = captionPartsForMedia(caption);
			await ctx.replyWithPhoto(new InputFile(photo), {
				caption: mediaCaption.caption,
				...replyOptions,
			});
			await sendCaptionFollowUps(ctx, mediaCaption.followUpChunks);
		},
		sendVoice: async (audio: Buffer) => {
			await ctx.replyWithVoice(new InputFile(audio), replyOptions);
		},
		sendVideo: async (video: Buffer, caption?: string) => {
			const mediaCaption = captionPartsForMedia(caption);
			await ctx.replyWithVideo(new InputFile(video), {
				caption: mediaCaption.caption,
				...replyOptions,
			});
			await sendCaptionFollowUps(ctx, mediaCaption.followUpChunks);
		},
		editMessage: async (messageId: number, text: string) => {
			const chatId = ctx.chat?.id;
			if (chatId == null) throw new Error("No chat for editMessage");
			await ctx.api.editMessageText(chatId, messageId, text);
		},
		deleteMessage: async (messageId: number) => {
			const chatId = ctx.chat?.id;
			if (chatId == null) throw new Error("No chat for deleteMessage");
			await ctx.api.deleteMessage(chatId, messageId);
		},
		replyMedia,
		threadId: ctx.message?.message_thread_id ?? null,
		replyToMessageId: ctx.message?.reply_to_message?.message_id ?? null,
	};
}

/** Main chat handler */
chatComposer.on("message", async (ctx) => {
	if (!shouldTrigger(ctx)) return;
	if (!ctx.dbUser || !ctx.dbChat || !ctx.creditService) return;
	const message = ctx.message;
	if (!message) return;

	const startTime = Date.now();

	// Get orchestrator config (tier, model, context limit)
	const orchestratorConfig = await ctx.creditService.getOrchestratorConfig();
	let { tier, modelId, contextLimit } = orchestratorConfig;
	let chatCreditResult: Awaited<
		ReturnType<typeof ctx.creditService.checkToolAccess>
	> | null = null;
	let chatDebitReserved = false;
	const chatTurnIdempotencyKey = ctx.chat
		? `chat:${ctx.chat.id}:${message.message_id}`
		: undefined;

	if (tier === ModelTier.STANDARD) {
		chatCreditResult = await ctx.creditService.checkToolAccess(
			STANDARD_CHAT_TOOL_NAME,
		);

		if (!chatCreditResult.allowed) {
			const freeModel = getDefaultModel(ModelCapability.TEXT, ModelTier.FREE);
			tier = ModelTier.FREE;
			modelId = freeModel.id;
			contextLimit = CONTEXT_LIMITS[ModelTier.FREE];
			chatCreditResult = null;
		}
	}

	if (tier === ModelTier.FREE) {
		const freeReservation = await recordFreeChatUsage(ctx.db, {
			userId: ctx.dbUser.id,
			chatId: ctx.dbChat.id,
			modelId,
			limit: FREE_CHAT_DAILY_LIMIT,
			botWideLimit: config.freeChatDailyBotLimit,
			idempotencyKey: chatTurnIdempotencyKey
				? `free:${chatTurnIdempotencyKey}`
				: undefined,
			meta: { chatId: ctx.dbChat.id, threadId: ctx.message?.message_thread_id },
		});
		if (freeReservation === "duplicate") return;
		if (freeReservation === "quota_exhausted") {
			await replyHtml(ctx, ctx.t("chat-free-quota-reached"), {
				message_thread_id: ctx.message?.message_thread_id,
				reply_to_message_id: ctx.message?.message_id,
			});
			return;
		}
	}

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
		{
			maxMessageChars: CHAT_MESSAGE_CHAR_BUDGET,
			maxStreamChars:
				tier === ModelTier.STANDARD
					? CHAT_CONTEXT_BUDGET[ModelTier.STANDARD]
					: CHAT_CONTEXT_BUDGET[ModelTier.FREE],
		},
	);

	// Build system prompt
	const systemPrompt = buildSystemPrompt(
		ctx.dbChat.personality ?? "default",
		hasActiveSubscription(ctx.dbUser) ? ctx.dbChat.customPrompt : null,
		ctx.dbChat.memory,
		ctx.dbUser.preferences,
		detectTaskSpecialist(message.text ?? message.caption ?? ""),
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
		const media = await extractMedia(ctx.api, message);
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
	const userPreferences = normalizeUserPreferences(ctx.dbUser.preferences);
	const toolSchemas = toolRegistry.getAutoCallableLLMToolSchemas(
		userPreferences.disabledTools,
	);
	const toolProviderMeta: ProviderResultMetadata = {};
	const toolContext = await buildToolContext(
		ctx,
		builtContext.participantRefs,
		mediaAttachments,
		toolProviderMeta,
	);

	// Create LLM provider
	const apiKeys = getGoogleApiKeys(config);
	const provider = new GoogleLLMProvider(apiKeys, config.googleApiPaidKey);

	const toolsUsed: string[] = [];
	let creditsSpent = chatCreditResult?.creditsToDeduct ?? 0;
	let creditSource =
		chatCreditResult && chatCreditResult.source !== "rejected"
			? chatCreditResult.source
			: undefined;
	let creditsRemaining = chatCreditResult?.creditsRemaining ?? null;
	let providerCompleted = false;
	let providerRoute: "primary" | "fallback" = "primary";
	let fallbackFrom: string | undefined;
	let billableProviderCallIds: string[] | undefined;
	let billableProviderCostMicros = 0;

	try {
		if (chatCreditResult?.allowed) {
			const reserved = await ctx.creditService.deduct(
				chatCreditResult,
				STANDARD_CHAT_TOOL_NAME,
				chatTurnIdempotencyKey,
				{ phase: "chat_turn" },
			);
			if (reserved === "duplicate") return;
			chatDebitReserved = true;
		}

		const tracking = {
			db: ctx.db,
			logicalRequestKey: chatTurnIdempotencyKey,
			operation: "chat",
			keyClass:
				tier === ModelTier.STANDARD ? ("paid" as const) : ("free" as const),
			userId: ctx.dbUser.id,
			chatId: ctx.dbChat.id,
			ledgerId: chatCreditResult?.ledgerId,
			creditsCharged: chatCreditResult?.creditsToDeduct ?? 0,
			creditSource:
				chatCreditResult && chatCreditResult.source !== "rejected"
					? chatCreditResult.source
					: "free",
			mediaInputCount: mediaAttachments.length,
			meta: { threadId },
		};
		const chatParams = {
			model: modelId,
			systemPrompt: fullSystemPrompt,
			messages: conversationMessages,
			tools: toolSchemas.length > 0 ? toolSchemas : undefined,
			media: mediaAttachments.length > 0 ? mediaAttachments : undefined,
			maxOutputTokens: tier === ModelTier.FREE ? 512 : 1024,
			tracking,
		};

		let result: Awaited<ReturnType<typeof provider.chatWithTools>>;
		try {
			result = await provider.chatWithTools(
				chatParams,
				async (toolName, args) => {
					if (userPreferences.disabledTools.includes(toolName)) {
						return {
							error: `Tool disabled by user settings: ${toolName}`,
						};
					}

					const tool = toolRegistry.getTool(toolName);
					if (!tool) {
						return { error: `Unknown tool: ${toolName}` };
					}

					const parsed = tool.parameters.safeParse(args);
					if (!parsed.success) {
						return { error: `Invalid parameters: ${parsed.error.message}` };
					}

					const toolCallIndex = toolsUsed.length;
					const toolResult = await executeWithCreditGate(tool, parsed.data, {
						...toolContext,
						idempotencyKey:
							ctx.chat && ctx.message
								? `tool:${toolName}:llm:${ctx.chat.id}:${ctx.message.message_id}:${toolCallIndex}`
								: undefined,
					});
					toolsUsed.push(toolName);
					if (toolResult.creditResult) {
						creditsSpent += toolResult.creditResult.creditsToDeduct;
						if (toolResult.creditResult.creditsRemaining != null) {
							creditsRemaining = toolResult.creditResult.creditsRemaining;
						}
						if (
							!creditSource &&
							toolResult.creditResult.source !== "rejected"
						) {
							creditSource = toolResult.creditResult.source;
						}
					}

					if (toolResult.handled) {
						return { result: "Response sent directly to chat." };
					}
					if (toolResult.error) {
						return {
							result:
								toolResult.text ??
								"I couldn't complete that tool request. Please try again later.",
						};
					}
					return { result: toolResult.text ?? "Done." };
				},
			);
		} catch (primaryErr) {
			if (
				tier === ModelTier.STANDARD &&
				config.openrouterApiKey &&
				mediaAttachments.length === 0
			) {
				logger.warn("chat_primary_provider_failed_fallback_openrouter", {
					error:
						primaryErr instanceof Error
							? primaryErr.message
							: String(primaryErr),
					model: modelId,
				});
				const fallback = new OpenRouterProvider(config.openrouterApiKey);
				providerRoute = "fallback";
				fallbackFrom = modelId;
				result = await fallback.chat({
					model: config.openrouterPaidFallbackModel,
					systemPrompt: fullSystemPrompt,
					messages: conversationMessages,
					maxOutputTokens: 1024,
					tracking: {
						...tracking,
						route: "fallback",
						meta: { ...tracking.meta, primaryModel: modelId },
					},
				});
				const maxFallbackCostMicros = fallbackCostCapMicros();
				if ((result.costMicros ?? 0) > maxFallbackCostMicros) {
					const costUsd = ((result.costMicros ?? 0) / 1_000_000).toFixed(4);
					const capUsd = (maxFallbackCostMicros / 1_000_000).toFixed(4);
					recordHandledFailure(
						"openrouter_cost_guard",
						"fallback_cost_over_cap",
						{
							model: result.actualModel ?? config.openrouterPaidFallbackModel,
							costMicros: result.costMicros ?? 0,
							maxFallbackCostMicros,
						},
					);
					logger.error("openrouter_fallback_cost_over_cap", {
						model: result.actualModel ?? config.openrouterPaidFallbackModel,
						costMicros: result.costMicros ?? 0,
						maxFallbackCostMicros,
					});
					await notifyAdmins(
						`⚠️ <b>OpenRouter fallback cost over cap</b>\n\nModel: <code>${escapeHtml(result.actualModel ?? config.openrouterPaidFallbackModel)}</code>\nCost: $${costUsd}\nCap: $${capUsd}\nCredits charged: ${chatCreditResult?.creditsToDeduct ?? 0}`,
						{ critical: true },
					).catch((notifyErr) => {
						logger.error("openrouter_cost_admin_notify_failed", {
							error:
								notifyErr instanceof Error
									? notifyErr.message
									: String(notifyErr),
						});
					});
				}
			} else {
				throw primaryErr;
			}
		}
		providerCompleted = true;
		billableProviderCallIds = [
			...(result.providerCallIds ?? []),
			...(toolProviderMeta.providerCallIds ?? []),
		];
		billableProviderCostMicros =
			(result.costMicros ?? 0) + (toolProviderMeta.costMicros ?? 0);

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
		const actualModel = result.actualModel ?? modelId;
		const providerCallIds =
			result.providerCallIds || toolProviderMeta.providerCallIds
				? [
						...(result.providerCallIds ?? []),
						...(toolProviderMeta.providerCallIds ?? []),
					]
				: undefined;
		const metadata: MessageMetadata = {
			model: actualModel,
			tier,
			inputTokens: result.usage.inputTokens,
			outputTokens: result.usage.outputTokens,
			cacheHitTokens: result.usage.cacheHitTokens,
			toolsUsed: toolsUsed.length > 0 ? toolsUsed : undefined,
			creditsSpent: creditsSpent > 0 ? creditsSpent : undefined,
			creditSource,
			providerCallIds,
			costMicros:
				billableProviderCostMicros > 0 ? billableProviderCostMicros : undefined,
			providerRoute,
			fallbackFrom,
			durationMs,
		};

		// Send the response and store in DB
		const responseText = result.text;
		if (!responseText && result.images && result.images.length > 0) {
			const img = result.images[0];
			if (!img) return;
			const sent = await ctx.replyWithPhoto(new InputFile(img.data), {
				message_thread_id: ctx.message?.message_thread_id,
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
				publicCreditSource(ctx, creditSource),
			);

			for (let i = 0; i < withFooter.length; i++) {
				let sent: Awaited<ReturnType<typeof ctx.reply>>;
				const chunk = withFooter[i];
				const storedText = chunks[i] ?? chunk;
				if (!chunk) continue;
				const html = markdownToHtml(chunk);
				try {
					sent = await ctx.reply(html, {
						parse_mode: "HTML",
						message_thread_id: ctx.message?.message_thread_id,
						reply_to_message_id: i === 0 ? ctx.message?.message_id : undefined,
					});
				} catch {
					// HTML parse error → fallback to plain text
					try {
						sent = await ctx.reply(stripHtmlTags(html), {
							message_thread_id: ctx.message?.message_thread_id,
							reply_to_message_id:
								i === 0 ? ctx.message?.message_id : undefined,
						});
					} catch {
						sent = await ctx.reply(stripHtmlTags(html), {
							message_thread_id: ctx.message?.message_thread_id,
						});
					}
				}

				await insertMessage(ctx.db, {
					chatId: ctx.dbChat.id,
					userId: null,
					telegramMessageId: sent.message_id,
					threadId,
					direction: "out",
					contentType: "text",
					text: storedText,
					metadata,
					telegramDate: new Date(sent.date * 1000),
				});
			}
		}
		await markLedgerSpendStatus(
			ctx.db,
			chatCreditResult?.ledgerId,
			"delivered",
			{
				providerCallIds: billableProviderCallIds,
				costMicros: billableProviderCostMicros,
			},
		);

		logger.info("chat_response", {
			model: actualModel,
			requestedModel: modelId,
			providerRoute,
			fallbackFrom,
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
		const error = err instanceof Error ? err.message : String(err);
		if (chatCreditResult && chatDebitReserved && !providerCompleted) {
			await ctx.creditService
				.refundDeduction(
					chatCreditResult,
					STANDARD_CHAT_TOOL_NAME,
					chatTurnIdempotencyKey,
					{ error },
				)
				.catch((refundErr) => {
					logger.error("chat_credit_refund_failed", {
						error:
							refundErr instanceof Error
								? refundErr.message
								: String(refundErr),
					});
				});
			await markLedgerSpendStatus(
				ctx.db,
				chatCreditResult.ledgerId,
				"refunded",
				{
					error,
				},
			);
		} else if (chatCreditResult && chatDebitReserved && providerCompleted) {
			logger.warn("chat_billable_failure_not_refunded", {
				error,
				creditsDeducted: chatCreditResult.creditsToDeduct,
				source: chatCreditResult.source,
				chatId: ctx.dbChat.telegramId,
				userId: ctx.dbUser.telegramId,
				providerCallIds: billableProviderCallIds,
				costMicros: billableProviderCostMicros,
			});
			await notifyAdmins(
				`⚠️ <b>Billable chat delivery failure</b>\n\nCredits kept: ${chatCreditResult.creditsToDeduct}\nSource: <code>${chatCreditResult.source}</code>\nProvider calls: <code>${escapeHtml(billableProviderCallIds?.join(", ") || "n/a")}</code>\nProvider cost: $${(billableProviderCostMicros / 1_000_000).toFixed(4)}\nChat/user: <code>${ctx.dbChat.telegramId}</code> / <code>${ctx.dbUser.telegramId}</code>\nReason: ${escapeHtml(error)}`,
				{ critical: true },
			).catch((notifyErr) => {
				logger.error("chat_billable_failure_admin_notify_failed", {
					error:
						notifyErr instanceof Error ? notifyErr.message : String(notifyErr),
				});
			});
			await markLedgerSpendStatus(
				ctx.db,
				chatCreditResult.ledgerId,
				"delivery_failed",
				{
					error,
					providerCallIds: billableProviderCallIds,
					costMicros: billableProviderCostMicros,
				},
			);
		}
		recordHandledFailure("chat", error, {
			chatId: ctx.dbChat.telegramId,
			userId: ctx.dbUser.telegramId,
		});
		logger.error("chat_llm_failed", {
			error,
			chatId: ctx.dbChat.telegramId,
			userId: ctx.dbUser.telegramId,
		});
		await replyHtml(ctx, ctx.t("chat-error"), {
			message_thread_id: ctx.message?.message_thread_id,
			reply_to_message_id: ctx.message?.message_id,
		});
	}
});

export { chatComposer };
