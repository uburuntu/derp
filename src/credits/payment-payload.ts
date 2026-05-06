import { createHmac, timingSafeEqual } from "node:crypto";

export type SignedCreditPaymentPayload =
    | {
          type: "sub";
          planId: string;
          targetChatId: number;
          targetThreadId: number | null;
      }
    | {
          type: "pack";
          packId: string;
          target: "user" | "chat";
          targetChatId: number;
          targetThreadId: number | null;
      };

export type SignedDonationPayload = {
    amount: number;
    targetChatId: number;
    targetThreadId: number | null;
};

export function buildCreditPaymentPayload(
    payload: SignedCreditPaymentPayload,
): string {
    const body =
        payload.type === "sub"
            ? bodyFor([
                  "pay",
                  "s",
                  payload.planId,
                  "u",
                  payload.targetChatId,
                  payload.targetThreadId ?? 0,
              ])
            : bodyFor([
                  "pay",
                  "p",
                  payload.packId,
                  payload.target === "chat" ? "c" : "u",
                  payload.targetChatId,
                  payload.targetThreadId ?? 0,
              ]);
    return `${body}:${sign(body)}`;
}

export function parseCreditPaymentPayload(
    payload: string,
): SignedCreditPaymentPayload | null {
    const parts = payload.split(":");
    if (parts.length !== 7 || parts[0] !== "pay") return null;
    const body = parts.slice(0, 6).join(":");
    const sig = parts[6];
    if (!sig || !validSignature(body, sig)) return null;

    const [, productType, productId, target, rawChatId, rawThreadId] = parts;
    const targetChatId = parseInteger(rawChatId);
    const targetThreadId = parseThreadId(rawThreadId);
    if (
        !productType ||
        !productId ||
        (target !== "u" && target !== "c") ||
        targetChatId == null ||
        targetThreadId === undefined
    ) {
        return null;
    }

    if (productType === "s") {
        return {
            type: "sub",
            planId: productId,
            targetChatId,
            targetThreadId,
        };
    }
    if (productType === "p") {
        return {
            type: "pack",
            packId: productId,
            target: target === "c" ? "chat" : "user",
            targetChatId,
            targetThreadId,
        };
    }
    return null;
}

export function buildDonationPayload(payload: SignedDonationPayload): string {
    const body = bodyFor([
        "don",
        payload.amount,
        payload.targetChatId,
        payload.targetThreadId ?? 0,
    ]);
    return `${body}:${sign(body)}`;
}

export function parseDonationPayload(
    payload: string,
): SignedDonationPayload | null {
    const parts = payload.split(":");
    if (parts.length !== 5 || parts[0] !== "don") return null;
    const body = parts.slice(0, 4).join(":");
    const sig = parts[4];
    if (!sig || !validSignature(body, sig)) return null;

    const amount = parseInteger(parts[1]);
    const targetChatId = parseInteger(parts[2]);
    const targetThreadId = parseThreadId(parts[3]);
    if (
        amount == null ||
        amount <= 0 ||
        targetChatId == null ||
        targetThreadId === undefined
    ) {
        return null;
    }
    return { amount, targetChatId, targetThreadId };
}

function bodyFor(parts: Array<string | number>): string {
    return parts.map(String).join(":");
}

function sign(body: string): string {
    return createHmac("sha256", paymentPayloadSecret())
        .update(body)
        .digest("base64url")
        .slice(0, 16);
}

function paymentPayloadSecret(): string {
    return (
        process.env.PAYMENT_PAYLOAD_SECRET ??
        process.env.TELEGRAM_BOT_TOKEN ??
        "derp-test-payment-payload-secret"
    );
}

function validSignature(body: string, signature: string): boolean {
    const expected = sign(body);
    const given = Buffer.from(signature);
    const actual = Buffer.from(expected);
    return given.length === actual.length && timingSafeEqual(given, actual);
}

function parseInteger(value: string | undefined): number | null {
    if (!value || !/^-?\d+$/.test(value)) return null;
    const parsed = Number(value);
    return Number.isSafeInteger(parsed) ? parsed : null;
}

function parseThreadId(value: string | undefined): number | null | undefined {
    const parsed = parseInteger(value);
    if (parsed == null) return undefined;
    return parsed === 0 ? null : parsed;
}
