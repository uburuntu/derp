/** Video generation tool — generates short videos from text prompts */

import { z } from "zod";
import { captionPartsForMedia } from "../common/reply";
import { config, getGoogleApiKeys } from "../config";
import { GoogleLLMProvider } from "../llm/providers/google";
import { ModelCapability } from "../llm/registry";
import type { ToolContext, ToolDefinition, ToolResult } from "./types";

const videoParamsSchema = z.object({
    prompt: z
        .string()
        .describe("A detailed description of the video to generate"),
});

type VideoParams = z.infer<typeof videoParamsSchema>;

function isBillableProviderFailure(err: unknown): boolean {
    return (
        typeof err === "object" &&
        err !== null &&
        "billableFailure" in err &&
        (err as { billableFailure?: boolean }).billableFailure === true
    );
}

function providerMetaFromError(err: unknown): {
    providerCallIds?: string[];
    costMicros?: number;
} {
    if (typeof err !== "object" || err === null) return {};
    const providerError = err as {
        providerCallIds?: string[];
        costMicros?: number;
    };
    return {
        providerCallIds: providerError.providerCallIds,
        costMicros: providerError.costMicros,
    };
}

async function executeVideo(
    params: VideoParams,
    ctx: ToolContext,
): Promise<ToolResult> {
    // Send progress message
    await ctx.sendMessage(
        "Generating video... This may take a couple of minutes.",
    );

    const provider = new GoogleLLMProvider(
        getGoogleApiKeys(config),
        config.googleApiPaidKey,
    );

    let result: Awaited<ReturnType<GoogleLLMProvider["generateVideo"]>>;
    try {
        result = await provider.generateVideo({
            model: "veo-3.1-fast-generate-preview",
            prompt: params.prompt,
            timeoutMs: 180_000,
            tracking: {
                recorder: ctx.providerRecorder,
                logicalRequestKey: ctx.idempotencyKey,
                operation: "video",
                keyClass: "paid",
                userId: ctx.user.id,
                chatId: ctx.chat.id,
                ledgerId: ctx.creditResult?.ledgerId,
                toolName: "video",
                creditsCharged: ctx.creditResult?.creditsToDeduct ?? 0,
                creditSource: ctx.creditResult?.source,
            },
        });
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        const providerMeta = providerMetaFromError(err);
        if (isBillableProviderFailure(err)) {
            ctx.recordProviderResult?.(providerMeta);
        }
        return {
            text: isBillableProviderFailure(err)
                ? `Video generated, but I couldn't retrieve it: ${msg}`
                : `Video generation failed: ${msg}`,
            error: msg,
            billableFailure: isBillableProviderFailure(err),
            providerCallIds: providerMeta.providerCallIds,
            costMicros: providerMeta.costMicros,
        };
    }

    ctx.recordProviderResult?.({
        providerCallIds: result.providerCallIds,
        costMicros: result.costMicros,
    });

    try {
        const caption = captionPartsForMedia(params.prompt);
        await ctx.sendVideo(result.video.data, caption.caption);
        for (const chunk of caption.followUpChunks) {
            await ctx.sendMessage(chunk);
        }
        return {
            handled: true,
            providerCallIds: result.providerCallIds,
            costMicros: result.costMicros,
        };
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return {
            text: `Video generated, but I couldn't deliver it to Telegram: ${msg}`,
            error: msg,
            billableFailure: true,
            providerCallIds: result.providerCallIds,
            costMicros: result.costMicros,
        };
    }
}

export const videoTool: ToolDefinition<VideoParams> = {
    name: "video",
    commands: ["/video", "/v", "/vid", "/veo"],
    description: "Generate a short 5-second video from a text description",
    helpText: "tool-video",
    category: "media",
    parameters: videoParamsSchema,
    execute: executeVideo,
    credits: 250,
    freeDaily: 0,
    capability: ModelCapability.VIDEO,
    defaultModel: "veo-3.1-fast-generate-preview",
};
