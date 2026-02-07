/** ffmpeg wrapper using Bun.spawn for media conversion */

/** Convert WAV audio buffer to OGG Opus (for Telegram voice messages) */
export async function convertToOggOpus(wavBuffer: Buffer): Promise<Buffer> {
	const proc = Bun.spawn(
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
		{
			stdin: "pipe",
			stdout: "pipe",
			stderr: "pipe",
		},
	);

	proc.stdin.write(wavBuffer);
	proc.stdin.end();

	const output = await new Response(proc.stdout).arrayBuffer();
	const exitCode = await proc.exited;

	if (exitCode !== 0) {
		const stderr = await new Response(proc.stderr).text();
		throw new Error(`ffmpeg WAV->OGG failed (exit ${exitCode}): ${stderr}`);
	}

	return Buffer.from(output);
}

/** Convert WebM buffer to MP4 (for animated stickers) */
export async function convertWebmToMp4(webmBuffer: Buffer): Promise<Buffer> {
	const proc = Bun.spawn(
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
		{
			stdin: "pipe",
			stdout: "pipe",
			stderr: "pipe",
		},
	);

	proc.stdin.write(webmBuffer);
	proc.stdin.end();

	const output = await new Response(proc.stdout).arrayBuffer();
	const exitCode = await proc.exited;

	if (exitCode !== 0) {
		const stderr = await new Response(proc.stderr).text();
		throw new Error(`ffmpeg WebM->MP4 failed (exit ${exitCode}): ${stderr}`);
	}

	return Buffer.from(output);
}

/** Convert any audio buffer to PCM WAV (for Gemini input) */
export async function convertToWav(
	audioBuffer: Buffer,
	inputFormat?: string,
): Promise<Buffer> {
	const inputArgs = inputFormat ? ["-f", inputFormat] : [];
	const proc = Bun.spawn(
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
		{
			stdin: "pipe",
			stdout: "pipe",
			stderr: "pipe",
		},
	);

	proc.stdin.write(audioBuffer);
	proc.stdin.end();

	const output = await new Response(proc.stdout).arrayBuffer();
	const exitCode = await proc.exited;

	if (exitCode !== 0) {
		const stderr = await new Response(proc.stderr).text();
		throw new Error(`ffmpeg audio->WAV failed (exit ${exitCode}): ${stderr}`);
	}

	return Buffer.from(output);
}
