/** Extract media from Telegram messages for LLM context */

import type { Api } from "grammy";
import type { Message } from "grammy/types";

export interface ExtractedMedia {
	type: "image" | "video" | "audio" | "document";
	data: Buffer;
	mimeType: string;
	fileId: string;
}

/** Download a file from Telegram by file_id */
async function downloadFile(api: Api, fileId: string): Promise<Buffer> {
	const file = await api.getFile(fileId);
	if (!file.file_path) {
		throw new Error(`No file_path for file_id: ${fileId}`);
	}
	const url = `https://api.telegram.org/file/bot${api.token}/${file.file_path}`;
	const response = await fetch(url);
	if (!response.ok) {
		throw new Error(`Failed to download file: ${response.status}`);
	}
	return Buffer.from(await response.arrayBuffer());
}

/** Extract the best photo file_id from a PhotoSize array (largest by file_size) */
function bestPhotoFileId(
	photos: Array<{ file_id: string; file_size?: number }>,
): string {
	let best = photos[0]!;
	for (const p of photos) {
		if ((p.file_size ?? 0) > (best.file_size ?? 0)) {
			best = p;
		}
	}
	return best.file_id;
}

/** Determine content type and file_id from a message */
export function getMessageMediaInfo(
	msg: Message,
): { type: ExtractedMedia["type"]; fileId: string; mimeType: string } | null {
	if (msg.photo && msg.photo.length > 0) {
		return {
			type: "image",
			fileId: bestPhotoFileId(msg.photo),
			mimeType: "image/jpeg",
		};
	}

	if (msg.sticker) {
		if (msg.sticker.is_video) {
			return {
				type: "video",
				fileId: msg.sticker.file_id,
				mimeType: "video/webm",
			};
		}
		if (msg.sticker.is_animated) {
			return {
				type: "document",
				fileId: msg.sticker.file_id,
				mimeType: "application/x-tgsticker",
			};
		}
		// Static sticker (WebP)
		return {
			type: "image",
			fileId: msg.sticker.file_id,
			mimeType: "image/webp",
		};
	}

	if (msg.animation) {
		return {
			type: "video",
			fileId: msg.animation.file_id,
			mimeType: msg.animation.mime_type ?? "video/mp4",
		};
	}

	if (msg.video) {
		return {
			type: "video",
			fileId: msg.video.file_id,
			mimeType: msg.video.mime_type ?? "video/mp4",
		};
	}

	if (msg.video_note) {
		return {
			type: "video",
			fileId: msg.video_note.file_id,
			mimeType: "video/mp4",
		};
	}

	if (msg.voice) {
		return {
			type: "audio",
			fileId: msg.voice.file_id,
			mimeType: msg.voice.mime_type ?? "audio/ogg",
		};
	}

	if (msg.audio) {
		return {
			type: "audio",
			fileId: msg.audio.file_id,
			mimeType: msg.audio.mime_type ?? "audio/mpeg",
		};
	}

	if (msg.document) {
		const mime = msg.document.mime_type ?? "application/octet-stream";
		// Treat image documents as images
		if (mime.startsWith("image/")) {
			return { type: "image", fileId: msg.document.file_id, mimeType: mime };
		}
		return { type: "document", fileId: msg.document.file_id, mimeType: mime };
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

	const data = await downloadFile(api, info.fileId);
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
