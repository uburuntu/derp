/** Hydrator middleware — upserts user, chat, member, and message on every update */

import type { NextFunction } from "grammy";
import type { DerpContext } from "../bot/context";
import { getAttachmentInfo, getContentType } from "../common/extractor";
import { logger } from "../common/observability";
import type { Database } from "../db/connection";
import { upsertChat } from "../db/queries/chats";
import { updateMemberRole, upsertChatMember } from "../db/queries/members";
import { insertMessage, updateMessageText } from "../db/queries/messages";
import { upsertUser } from "../db/queries/users";

export function createHydrator(db: Database) {
	return async function hydratorMiddleware(
		ctx: DerpContext,
		next: NextFunction,
	): Promise<void> {
		ctx.db = db;

		// Upsert user
		if (ctx.from) {
			ctx.dbUser = await upsertUser(db, ctx.from);
		}

		// Upsert chat
		if (ctx.chat) {
			ctx.dbChat = await upsertChat(db, ctx.chat);
		}

		// Upsert chat member (user in this chat)
		if (ctx.dbUser && ctx.dbChat) {
			await upsertChatMember(db, ctx.dbChat.id, ctx.dbUser.id);
		}

		// Persist incoming message
		if (ctx.message && ctx.dbUser && ctx.dbChat) {
			const msg = ctx.message;
			const attachment = getAttachmentInfo(msg);

			await insertMessage(db, {
				chatId: ctx.dbChat.id,
				userId: ctx.dbUser.id,
				telegramMessageId: msg.message_id,
				threadId: msg.message_thread_id ?? null,
				direction: "in",
				contentType: getContentType(msg),
				text: msg.text ?? msg.caption ?? null,
				mediaGroupId: msg.media_group_id ?? null,
				attachmentType: attachment?.type ?? null,
				attachmentFileId: attachment?.fileId ?? null,
				replyToMessageId: msg.reply_to_message?.message_id ?? null,
				telegramDate: new Date(msg.date * 1000),
			});
		}

		// Handle edited messages
		if (ctx.editedMessage && ctx.dbChat) {
			const edited = ctx.editedMessage;
			const newText = edited.text ?? edited.caption ?? null;
			if (newText) {
				await updateMessageText(db, ctx.dbChat.id, edited.message_id, newText);
			}
		}

		// Handle chat_member updates (role changes)
		if (ctx.chatMember && ctx.dbChat) {
			const memberUpdate = ctx.chatMember;
			const tgUser = memberUpdate.new_chat_member.user;
			const dbUser = await upsertUser(db, tgUser);
			const status = memberUpdate.new_chat_member.status;
			const isActive = status !== "left" && status !== "kicked";

			await updateMemberRole(db, ctx.dbChat.id, dbUser.id, status, isActive);
		}

		// Track message reactions for quality analysis
		if (ctx.messageReaction && ctx.dbChat) {
			const reaction = ctx.messageReaction;
			const formatReaction = (r: { type: string }) =>
				"emoji" in r ? (r as { emoji: string }).emoji : r.type;
			const newReactions = (reaction.new_reaction ?? []).map(formatReaction);
			const oldReactions = (reaction.old_reaction ?? []).map(formatReaction);
			logger.info("user_feedback", {
				chatId: ctx.dbChat.telegramId,
				messageId: reaction.message_id,
				userId: reaction.user?.id,
				oldReactions,
				newReactions,
			});
		}

		await next();
	};
}
