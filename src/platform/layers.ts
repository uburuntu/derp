/** Import boundary rules for Derp's layered architecture. */

export interface ImportReference {
    kind: "internal" | "external";
    specifier: string;
    targetPath?: string;
}

export interface ImportBoundaryRule {
    name: string;
    owner: string;
    source: RegExp;
    forbiddenInternal: RegExp[];
    forbiddenExternal: RegExp[];
    reason: string;
}

export const IMPORT_BOUNDARY_RULES: ImportBoundaryRule[] = [
    {
        name: "tools are isolated instruments",
        owner: "src/tools/<instrument>.ts",
        source: /^src\/tools\/(?!credit-gate\.ts$|loader\.ts$|registry\.ts$|types\.ts$)[^/]+\.ts$/,
        forbiddenInternal: [
            /^src\/bot\//,
            /^src\/handlers\//,
            /^src\/middleware\//,
            /^src\/tools\/credit-gate\.ts$/,
            /^src\/tools\/registry\.ts$/,
        ],
        forbiddenExternal: [/^grammy$/, /^grammy\//, /^@grammyjs\//],
        reason: "Instruments own schema, pricing, parsing, and execution only. Telegram delivery, command registration, confirmations, and credit gating stay behind ToolContext and ToolRegistry.",
    },
    {
        name: "llm providers use ports, not storage",
        owner: "src/llm/providers/*",
        source: /^src\/llm\/providers\//,
        forbiddenInternal: [
            /^src\/db\//,
            /^src\/bot\//,
            /^src\/handlers\//,
            /^src\/middleware\//,
            /^src\/tools\//,
        ],
        forbiddenExternal: [/^drizzle-orm($|\/)/, /^postgres$/],
        reason: "Provider adapters call external AI APIs and report accounting through ProviderCallRecorder. They must not know the database or Telegram runtime.",
    },
    {
        name: "data layer has no product adapters",
        owner: "src/db/**",
        source: /^src\/db\//,
        forbiddenInternal: [
            /^src\/bot\//,
            /^src\/handlers\//,
            /^src\/middleware\//,
            /^src\/tools\//,
            /^src\/llm\/providers\//,
        ],
        forbiddenExternal: [/^grammy$/, /^@grammyjs\//],
        reason: "Query modules persist state and expose repository functions. Product workflows and Telegram behavior belong above the data layer.",
    },
    {
        name: "common utilities stay context-free",
        owner: "src/common/**",
        source: /^src\/common\//,
        forbiddenInternal: [
            /^src\/bot\//,
            /^src\/handlers\//,
            /^src\/middleware\//,
            /^src\/tools\//,
            /^src\/scheduler\//,
        ],
        forbiddenExternal: [],
        reason: "Common modules provide reusable platform utilities. They must not depend on the bot context, handlers, instruments, or scheduler workflows.",
    },
];

export function boundaryViolationsForImport(
    sourcePath: string,
    reference: ImportReference,
): string[] {
    return IMPORT_BOUNDARY_RULES.flatMap((rule) => {
        if (!rule.source.test(sourcePath)) return [];

        const internalViolation =
            reference.kind === "internal" &&
            reference.targetPath != null &&
            rule.forbiddenInternal.some((pattern) =>
                pattern.test(reference.targetPath ?? ""),
            );
        const externalViolation =
            reference.kind === "external" &&
            rule.forbiddenExternal.some((pattern) =>
                pattern.test(reference.specifier),
            );

        if (!internalViolation && !externalViolation) return [];
        const target =
            reference.kind === "internal"
                ? (reference.targetPath ?? reference.specifier)
                : reference.specifier;
        return [
            `${rule.name}: ${sourcePath} imports ${target}. ${rule.reason}`,
        ];
    });
}
