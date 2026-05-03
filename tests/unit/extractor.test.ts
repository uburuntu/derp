import { describe, expect, test } from "bun:test";
import type { Api } from "grammy";
import type { Message } from "grammy/types";
import { extractMedia, getMessageMediaInfo } from "../../src/common/extractor";

const MiB = 1024 * 1024;

async function expectRejectsWith(
	promise: Promise<unknown>,
	messagePart: string,
): Promise<void> {
	let error: unknown;
	try {
		await promise;
	} catch (err) {
		error = err;
	}
	expect(error).toBeInstanceOf(Error);
	expect((error as Error).message).toContain(messagePart);
}

function makeApi(fileSize?: number): Api {
	return {
		token: "token",
		getFile: async (fileId: string) => ({
			file_id: fileId,
			file_unique_id: `${fileId}-unique`,
			file_path: `${fileId}.bin`,
			file_size: fileSize,
		}),
	} as unknown as Api;
}

function stubFetch(handler: () => Promise<Response>): typeof fetch {
	const stub = Object.assign(
		(
			_input: Parameters<typeof fetch>[0],
			_init?: Parameters<typeof fetch>[1],
		) => handler(),
		{ preconnect: () => {} },
	);
	return stub;
}

describe("getMessageMediaInfo", () => {
	test("includes Telegram size and duration hints", () => {
		const msg = {
			video: {
				file_id: "video-1",
				file_unique_id: "video-1u",
				width: 640,
				height: 360,
				duration: 30,
				mime_type: "video/mp4",
				file_size: 1024,
			},
		} as Message;

		expect(getMessageMediaInfo(msg)).toEqual({
			type: "video",
			fileId: "video-1",
			mimeType: "video/mp4",
			sizeBytes: 1024,
			durationSeconds: 30,
		});
	});
});

describe("extractMedia", () => {
	test("rejects oversized video before download", async () => {
		let fetchCalled = false;
		const originalFetch = globalThis.fetch;
		globalThis.fetch = stubFetch(async () => {
			fetchCalled = true;
			return new Response("unused");
		});
		try {
			const msg = {
				video: {
					file_id: "video-1",
					file_unique_id: "video-1u",
					width: 640,
					height: 360,
					duration: 30,
					mime_type: "video/mp4",
					file_size: 51 * MiB,
				},
			} as Message;

			await expectRejectsWith(extractMedia(makeApi(), msg), "too large");
			expect(fetchCalled).toBe(false);
		} finally {
			globalThis.fetch = originalFetch;
		}
	});

	test("rejects long audio before download", async () => {
		const msg = {
			voice: {
				file_id: "voice-1",
				file_unique_id: "voice-1u",
				duration: 601,
				mime_type: "audio/ogg",
				file_size: 1024,
			},
		} as Message;

		await expectRejectsWith(extractMedia(makeApi(), msg), "too long");
	});

	test("rejects downloads above the media limit", async () => {
		const originalFetch = globalThis.fetch;
		globalThis.fetch = stubFetch(
			async () =>
				new Response("", {
					headers: { "content-length": String(11 * MiB) },
				}),
		);
		try {
			const msg = {
				photo: [
					{
						file_id: "photo-1",
						file_unique_id: "photo-1u",
						width: 1024,
						height: 1024,
					},
				],
			} as Message;

			await expectRejectsWith(extractMedia(makeApi(), msg), "too large");
		} finally {
			globalThis.fetch = originalFetch;
		}
	});
});
