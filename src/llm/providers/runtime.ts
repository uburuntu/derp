/** Provider runtime utilities shared by LLM vendor adapters. */

export function isTransientProviderError(err: unknown): boolean {
    if (err instanceof Error) {
        const msg = err.message.toLowerCase();
        return (
            msg.includes("500") ||
            msg.includes("503") ||
            msg.includes("429") ||
            msg.includes("rate limit") ||
            msg.includes("overloaded") ||
            msg.includes("internal") ||
            msg.includes("unavailable") ||
            msg.includes("timeout") ||
            msg.includes("aborted")
        );
    }
    return false;
}

export function sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

export function remainingMs(deadlineMs: number): number {
    return Math.max(0, deadlineMs - Date.now());
}

export function operationNameFrom(operation: unknown): string | null {
    if (!operation || typeof operation !== "object") return null;
    const maybeName = (operation as { name?: unknown }).name;
    return typeof maybeName === "string" && maybeName ? maybeName : null;
}

export async function withAbortTimeout<T>(
    timeoutMs: number,
    label: string,
    operation: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
    if (timeoutMs <= 0) {
        throw new Error(`${label} timed out`);
    }
    const abortController = new AbortController();
    const timeout = setTimeout(() => abortController.abort(), timeoutMs);

    try {
        return await operation(abortController.signal);
    } catch (err) {
        if (abortController.signal.aborted) {
            throw new Error(`${label} timed out after ${timeoutMs}ms`);
        }
        throw err;
    } finally {
        clearTimeout(timeout);
    }
}

export async function readResponseLimited(
    response: Response,
    maxBytes: number,
    label: string,
): Promise<Buffer> {
    const contentLength = response.headers.get("content-length");
    if (contentLength) {
        const bytes = Number(contentLength);
        if (Number.isFinite(bytes) && bytes > maxBytes) {
            throw new Error(`${label} is too large: ${bytes} bytes`);
        }
    }

    if (!response.body) {
        const data = Buffer.from(await response.arrayBuffer());
        if (data.byteLength > maxBytes) {
            throw new Error(`${label} is too large: ${data.byteLength} bytes`);
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
            throw new Error(`${label} is too large: ${total} bytes`);
        }
        chunks.push(Buffer.from(value));
    }

    return Buffer.concat(chunks, total);
}

export class KeyRotator {
    private index = 0;

    constructor(private keys: string[]) {
        if (keys.length === 0) throw new Error("No API keys provided");
    }

    next(): string {
        const key = this.keys[this.index % this.keys.length];
        if (!key) throw new Error("No API keys configured");
        this.index++;
        return key;
    }
}
