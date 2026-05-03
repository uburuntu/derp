/** Extract media from Telegram messages for LLM context */

import type { Api } from "grammy";
import type { Message } from "grammy/types";

export interface ExtractedMedia {
	type: "image" | "video" | "audio" | "document";
	data: Buffer;
	mimeType: string;
	fileId: string;
}

export interface MediaInfo {
	type: ExtractedMedia["type"];
	fileId: string;
	mimeType: string;
	sizeBytes?: number;
	durationSeconds?: number;
}

interface DownloadOptions {
	maxBytes?: number;
	timeoutMs?: number;
}

const MiB = 1024 * 1024;
const DOWNLOAD_TIMEOUT_MS = 15_000;
const DEFAULT_MAX_DOWNLOAD_BYTES = 50 * MiB;
const MAX_MEDIA_BYTES: Record<ExtractedMedia["type"], number> = {
	image: 10 * MiB,
	video: 50 * MiB,
	audio: 25 * MiB,
	document: 10 * MiB,
};
const MAX_DURATION_SECONDS: Partial<Record<ExtractedMedia["type"], number>> = {
	video: 120,
	audio: 600,
};
const ALLOWED_MIME_TYPES: Record<ExtractedMedia["type"], Set<string>> = {
	image: new Set(["image/jpeg", "image/png", "image/webp"]),
	video: new Set(["video/mp4", "video/webm", "image/gif"]),
	audio: new Set([
		"audio/aac",
		"audio/flac",
		"audio/m4a",
		"audio/mp3",
		"audio/mpeg",
		"audio/mp4",
		"audio/ogg",
		"audio/wav",
		"audio/webm",
		"audio/x-m4a",
		"audio/x-wav",
	]),
	document: new Set(["application/pdf", "text/plain"]),
};

function normalizeMimeType(mimeType: string): string {
	return mimeType.split(";")[0]?.trim().toLowerCase() || mimeType;
}

function assertMediaAllowed(info: MediaInfo): void {
	const mimeType = normalizeMimeType(info.mimeType);
	const allowedTypes = ALLOWED_MIME_TYPES[info.type];
	if (!allowedTypes.has(mimeType)) {
		throw new Error(`Unsupported ${info.type} MIME type: ${info.mimeType}`);
	}

	const maxBytes = MAX_MEDIA_BYTES[info.type];
	if (info.sizeBytes != null && info.sizeBytes > maxBytes) {
		throw new Error(
			`${info.type} is too large: ${info.sizeBytes} bytes > ${maxBytes} bytes`,
		);
	}

	const maxDuration = MAX_DURATION_SECONDS[info.type];
	if (
		maxDuration != null &&
		info.durationSeconds != null &&
		info.durationSeconds > maxDuration
	) {
		throw new Error(
			`${info.type} is too long: ${info.durationSeconds}s > ${maxDuration}s`,
		);
	}
}

async function readResponseLimited(
	response: Response,
	maxBytes: number,
): Promise<Buffer> {
	const contentLength = response.headers.get("content-length");
	if (contentLength) {
		const bytes = Number(contentLength);
		if (Number.isFinite(bytes) && bytes > maxBytes) {
			throw new Error(`Downloaded file is too large: ${bytes} bytes`);
		}
	}

	if (!response.body) {
		const data = Buffer.from(await response.arrayBuffer());
		if (data.byteLength > maxBytes) {
			throw new Error(`Downloaded file is too large: ${data.byteLength} bytes`);
		}
		return data;
	}

	const reader = response.body.getReader();
	const chunks: Buffer[] = [];
	let total = 0;

	while (true) {
		const { done, value } = await reader.read();
		if (done) break;
		if (!value) continue;

		total += value.byteLength;
		if (total > maxBytes) {
			await reader.cancel();
			throw new Error(`Downloaded file is too large: ${total} bytes`);
		}
		chunks.push(Buffer.from(value));
	}

	return Buffer.concat(chunks, total);
}

/** Download a file from Telegram by file_id */
export async function downloadTelegramFile(
	api: Api,
	fileId: string,
	options: DownloadOptions = {},
): Promise<Buffer> {
	const maxBytes = options.maxBytes ?? DEFAULT_MAX_DOWNLOAD_BYTES;
	const timeoutMs = options.timeoutMs ?? DOWNLOAD_TIMEOUT_MS;
	const file = await api.getFile(fileId);
	if (file.file_size != null && file.file_size > maxBytes) {
		throw new Error(
			`Telegram file is too large: ${file.file_size} bytes > ${maxBytes} bytes`,
		);
	}
	if (!file.file_path) {
		throw new Error(`No file_path for file_id: ${fileId}`);
	}
	const url = `https://api.telegram.org/file/bot${api.token}/${file.file_path}`;
	const abortController = new AbortController();
	const timeout = setTimeout(() => abortController.abort(), timeoutMs);

	try {
		const response = await fetch(url, { signal: abortController.signal });
		if (!response.ok) {
			throw new Error(`Failed to download file: ${response.status}`);
		}
		return await readResponseLimited(response, maxBytes);
	} catch (err) {
		if (abortController.signal.aborted) {
			throw new Error(`Telegram file download timed out after ${timeoutMs}ms`);
		}
		throw err;
	} finally {
		clearTimeout(timeout);
	}
}

/** Extract the best photo file_id from a PhotoSize array (largest by file_size) */
export function bestPhotoFileId(
	photos: Array<{ file_id: string; file_size?: number }>,
): string | null {
	let best = photos[0];
	if (!best) return null;
	for (const p of photos) {
		if ((p.file_size ?? 0) > (best.file_size ?? 0)) {
			best = p;
		}
	}
	return best.file_id;
}

/** Determine content type and file_id from a message */
export function getMessageMediaInfo(msg: Message): MediaInfo | null {
	if (msg.photo && msg.photo.length > 0) {
		const best = msg.photo.reduce((current, candidate) =>
			(candidate.file_size ?? 0) > (current.file_size ?? 0)
				? candidate
				: current,
		);
		const fileId = bestPhotoFileId(msg.photo);
		if (!fileId) return null;
		return {
			type: "image",
			fileId,
			mimeType: "image/jpeg",
			sizeBytes: best.file_size,
		};
	}

	if (msg.sticker) {
		if (msg.sticker.is_video) {
			return {
				type: "video",
				fileId: msg.sticker.file_id,
				mimeType: "video/webm",
				sizeBytes: msg.sticker.file_size,
			};
		}
		if (msg.sticker.is_animated) {
			return {
				type: "document",
				fileId: msg.sticker.file_id,
				mimeType: "application/x-tgsticker",
				sizeBytes: msg.sticker.file_size,
			};
		}
		// Static sticker (WebP)
		return {
			type: "image",
			fileId: msg.sticker.file_id,
			mimeType: "image/webp",
			sizeBytes: msg.sticker.file_size,
		};
	}

	if (msg.animation) {
		return {
			type: "video",
			fileId: msg.animation.file_id,
			mimeType: msg.animation.mime_type ?? "video/mp4",
			sizeBytes: msg.animation.file_size,
			durationSeconds: msg.animation.duration,
		};
	}

	if (msg.video) {
		return {
			type: "video",
			fileId: msg.video.file_id,
			mimeType: msg.video.mime_type ?? "video/mp4",
			sizeBytes: msg.video.file_size,
			durationSeconds: msg.video.duration,
		};
	}

	if (msg.video_note) {
		return {
			type: "video",
			fileId: msg.video_note.file_id,
			mimeType: "video/mp4",
			sizeBytes: msg.video_note.file_size,
			durationSeconds: msg.video_note.duration,
		};
	}

	if (msg.voice) {
		return {
			type: "audio",
			fileId: msg.voice.file_id,
			mimeType: msg.voice.mime_type ?? "audio/ogg",
			sizeBytes: msg.voice.file_size,
			durationSeconds: msg.voice.duration,
		};
	}

	if (msg.audio) {
		return {
			type: "audio",
			fileId: msg.audio.file_id,
			mimeType: msg.audio.mime_type ?? "audio/mpeg",
			sizeBytes: msg.audio.file_size,
			durationSeconds: msg.audio.duration,
		};
	}

	if (msg.document) {
		const mime = normalizeMimeType(
			msg.document.mime_type ?? "application/octet-stream",
		);
		// Treat image documents as images
		if (mime.startsWith("image/")) {
			return {
				type: "image",
				fileId: msg.document.file_id,
				mimeType: mime,
				sizeBytes: msg.document.file_size,
			};
		}
		return {
			type: "document",
			fileId: msg.document.file_id,
			mimeType: mime,
			sizeBytes: msg.document.file_size,
		};
	}

	return null;
}

/** Extract and download all media from a message */
export async function extractMedia(
	api: Api,
	msg: Message,
): Promise<ExtractedMedia[]> {
	const info = getMessageMediaInfo(msg);
	if (!info) return [];
	assertMediaAllowed(info);

	const data = await downloadTelegramFile(api, info.fileId, {
		maxBytes: MAX_MEDIA_BYTES[info.type],
		timeoutMs: DOWNLOAD_TIMEOUT_MS,
	});
	return [
		{ type: info.type, data, mimeType: info.mimeType, fileId: info.fileId },
	];
}

/** Get the content type string for DB storage */
export function getContentType(msg: Message): string | null {
	if (msg.text) return "text";
	if (msg.photo) return "photo";
	if (msg.sticker) return "sticker";
	if (msg.animation) return "animation";
	if (msg.video) return "video";
	if (msg.video_note) return "video_note";
	if (msg.voice) return "voice";
	if (msg.audio) return "audio";
	if (msg.document) return "document";
	if (msg.contact) return "contact";
	if (msg.location) return "location";
	if (msg.poll) return "poll";
	if (msg.dice) return "dice";
	return null;
}

/** Get attachment type and file_id for DB storage (without downloading) */
export function getAttachmentInfo(
	msg: Message,
): { type: string; fileId: string } | null {
	const info = getMessageMediaInfo(msg);
	if (!info) return null;
	return { type: info.type, fileId: info.fileId };
}
