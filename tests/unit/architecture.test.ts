import { describe, expect, test } from "bun:test";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { boundaryViolationsForImport } from "../../src/platform/layers";

const IMPORT_RE =
    /(?:import|export)\s+(?:type\s+)?(?:[^"']*?\sfrom\s*)?["']([^"']+)["']|import\(\s*["']([^"']+)["']\s*\)/g;

function sourceFiles(dir = "src"): string[] {
    return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
        const fullPath = path.posix.join(dir, entry.name);
        if (entry.isDirectory()) return sourceFiles(fullPath);
        return entry.isFile() && fullPath.endsWith(".ts") ? [fullPath] : [];
    });
}

function resolveInternalImport(sourcePath: string, specifier: string): string {
    const base = path.posix.normalize(
        path.posix.join(path.dirname(sourcePath), specifier),
    );
    const candidates = [base, `${base}.ts`, path.posix.join(base, "index.ts")];
    return candidates.find((candidate) => existsSync(candidate)) ?? base;
}

describe("architecture boundaries", () => {
    test("enforces instrument, provider, data, and common import boundaries", () => {
        const violations: string[] = [];

        for (const sourcePath of sourceFiles()) {
            const source = readFileSync(sourcePath, "utf8");
            for (const match of source.matchAll(IMPORT_RE)) {
                const specifier = match[1] ?? match[2];
                if (!specifier) continue;
                const reference = specifier.startsWith(".")
                    ? {
                          kind: "internal" as const,
                          specifier,
                          targetPath: resolveInternalImport(
                              sourcePath,
                              specifier,
                          ),
                      }
                    : { kind: "external" as const, specifier };
                violations.push(
                    ...boundaryViolationsForImport(sourcePath, reference),
                );
            }
        }

        expect(violations).toEqual([]);
    });
});
