/** ffmpeg wrapper using Bun.spawn for media conversion */

const MiB = 1024 * 1024;
const AUDIO_INPUT_LIMIT_BYTES = 25 * MiB;
const AUDIO_OUTPUT_LIMIT_BYTES = 25 * MiB;
const VIDEO_INPUT_LIMIT_BYTES = 50 * MiB;
const VIDEO_OUTPUT_LIMIT_BYTES = 50 * MiB;
const AUDIO_CONVERT_TIMEOUT_MS = 30_000;
const VIDEO_CONVERT_TIMEOUT_MS = 60_000;

async function readStreamLimited(
	stream: ReadableStream<Uint8Array>,
	maxBytes: number,
): Promise<Buffer> {
	const reader = stream.getReader();
	const chunks: Buffer[] = [];
	let total = 0;

	while (true) {
		const { done, value } = await reader.read();
		if (done) break;
		if (!value) continue;

		total += value.byteLength;
		if (total > maxBytes) {
			await reader.cancel();
			throw new Error(`ffmpeg output is too large: ${total} bytes`);
		}
		chunks.push(Buffer.from(value));
	}

	return Buffer.concat(chunks, total);
}

async function runFfmpeg(
	args: string[],
	input: Buffer,
	label: string,
	limits: { maxInputBytes: number; maxOutputBytes: number; timeoutMs: number },
): Promise<Buffer> {
	if (input.byteLength > limits.maxInputBytes) {
		throw new Error(
			`${label} input is too large: ${input.byteLength} bytes > ${limits.maxInputBytes} bytes`,
		);
	}

	const proc = Bun.spawn(args, {
		stdin: "pipe",
		stdout: "pipe",
		stderr: "pipe",
	});
	let timedOut = false;
	const timeout = setTimeout(() => {
		timedOut = true;
		proc.kill();
	}, limits.timeoutMs);
	const stderrPromise = new Response(proc.stderr).text();
	const outputPromise = readStreamLimited(proc.stdout, limits.maxOutputBytes);

	try {
		proc.stdin.write(input);
		proc.stdin.end();

		const output = await outputPromise;
		const exitCode = await proc.exited;
		const stderr = await stderrPromise;

		if (timedOut) {
			throw new Error(`ffmpeg ${label} timed out after ${limits.timeoutMs}ms`);
		}
		if (exitCode !== 0) {
			throw new Error(`ffmpeg ${label} failed (exit ${exitCode}): ${stderr}`);
		}

		return output;
	} catch (err) {
		proc.kill();
		await proc.exited.catch(() => undefined);
		if (timedOut) {
			throw new Error(`ffmpeg ${label} timed out after ${limits.timeoutMs}ms`);
		}
		throw err;
	} finally {
		clearTimeout(timeout);
	}
}

/** Convert WAV audio buffer to OGG Opus (for Telegram voice messages) */
export async function convertToOggOpus(wavBuffer: Buffer): Promise<Buffer> {
	return runFfmpeg(
		[
			"ffmpeg",
			"-i",
			"pipe:0",
			"-c:a",
			"libopus",
			"-b:a",
			"64k",
			"-vbr",
			"on",
			"-f",
			"ogg",
			"pipe:1",
		],
		wavBuffer,
		"WAV->OGG",
		{
			maxInputBytes: AUDIO_INPUT_LIMIT_BYTES,
			maxOutputBytes: AUDIO_OUTPUT_LIMIT_BYTES,
			timeoutMs: AUDIO_CONVERT_TIMEOUT_MS,
		},
	);
}

/** Convert WebM buffer to MP4 (for animated stickers) */
export async function convertWebmToMp4(webmBuffer: Buffer): Promise<Buffer> {
	return runFfmpeg(
		[
			"ffmpeg",
			"-i",
			"pipe:0",
			"-c:v",
			"libx264",
			"-pix_fmt",
			"yuv420p",
			"-movflags",
			"+faststart",
			"-an",
			"-f",
			"mp4",
			"pipe:1",
		],
		webmBuffer,
		"WebM->MP4",
		{
			maxInputBytes: VIDEO_INPUT_LIMIT_BYTES,
			maxOutputBytes: VIDEO_OUTPUT_LIMIT_BYTES,
			timeoutMs: VIDEO_CONVERT_TIMEOUT_MS,
		},
	);
}

/** Convert any audio buffer to PCM WAV (for Gemini input) */
export async function convertToWav(
	audioBuffer: Buffer,
	inputFormat?: string,
): Promise<Buffer> {
	const inputArgs = inputFormat ? ["-f", inputFormat] : [];
	return runFfmpeg(
		[
			"ffmpeg",
			...inputArgs,
			"-i",
			"pipe:0",
			"-ar",
			"16000",
			"-ac",
			"1",
			"-f",
			"wav",
			"pipe:1",
		],
		audioBuffer,
		"audio->WAV",
		{
			maxInputBytes: AUDIO_INPUT_LIMIT_BYTES,
			maxOutputBytes: AUDIO_OUTPUT_LIMIT_BYTES,
			timeoutMs: AUDIO_CONVERT_TIMEOUT_MS,
		},
	);
}
