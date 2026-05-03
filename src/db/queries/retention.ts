/** Retention jobs for sensitive Telegram text, file IDs, and debug metadata. */

import { and, lt, sql } from "drizzle-orm";
import type { Database } from "../connection";
import { ledger, messages } from "../schema";

const MESSAGE_RETENTION_DAYS = 30;
const LEDGER_META_RETENTION_DAYS = 400;

export interface RetentionResult {
	messagesScrubbed: number;
	ledgerRowsScrubbed: number;
	messageCutoff: string;
	ledgerCutoff: string;
}

export async function scrubRetention(
	db: Database,
	now = new Date(),
): Promise<RetentionResult> {
	const messageCutoff = daysAgo(now, MESSAGE_RETENTION_DAYS);
	const ledgerCutoff = daysAgo(now, LEDGER_META_RETENTION_DAYS);

	const messageRows = await db
		.update(messages)
		.set({
			text: null,
			attachmentFileId: null,
			metadata: null,
			updatedAt: now,
		})
		.where(
			and(
				lt(messages.createdAt, messageCutoff),
				sql`(${messages.text} IS NOT NULL OR ${messages.attachmentFileId} IS NOT NULL OR ${messages.metadata} IS NOT NULL)`,
			),
		)
		.returning({ id: messages.id });

	const ledgerRows = await db
		.update(ledger)
		.set({ meta: null })
		.where(
			and(lt(ledger.createdAt, ledgerCutoff), sql`${ledger.meta} IS NOT NULL`),
		)
		.returning({ id: ledger.id });

	return {
		messagesScrubbed: messageRows.length,
		ledgerRowsScrubbed: ledgerRows.length,
		messageCutoff: messageCutoff.toISOString(),
		ledgerCutoff: ledgerCutoff.toISOString(),
	};
}

function daysAgo(now: Date, days: number): Date {
	return new Date(now.getTime() - days * 24 * 60 * 60 * 1000);
}
