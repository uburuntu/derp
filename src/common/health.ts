/** Health check server — lightweight HTTP endpoint for Docker/deployment probes */

import { logger } from "./observability";

const HEALTH_PORT = Number(process.env.HEALTH_PORT ?? 8080);

interface HealthStatus {
    db: boolean;
    schema: boolean;
    bot: boolean;
    scheduler: boolean;
    startedAt: string | null;
    lastCheckedAt: string | null;
    error: string | null;
    botDetails: Record<string, unknown> | null;
    schedulerDetails: Record<string, unknown> | null;
}

const status: HealthStatus = {
    db: false,
    schema: false,
    bot: false,
    scheduler: false,
    startedAt: null,
    lastCheckedAt: null,
    error: null,
    botDetails: null,
    schedulerDetails: null,
};

export interface ReadinessResult {
    db: boolean;
    schema: boolean;
    error?: string;
}

type ReadinessCheck = () => Promise<ReadinessResult>;
type ComponentCheck = () => {
    ready: boolean;
    error?: string;
    details?: Record<string, unknown>;
};

let readinessCheck: ReadinessCheck | null = null;
let botCheck: ComponentCheck | null = null;
let schedulerCheck: ComponentCheck | null = null;

export function setReadinessCheck(check: ReadinessCheck): void {
    readinessCheck = check;
}

export function setBotCheck(check: ComponentCheck): void {
    botCheck = check;
}

export function setSchedulerCheck(check: ComponentCheck): void {
    schedulerCheck = check;
}

export function markReady(
    component: keyof Pick<HealthStatus, "db" | "schema" | "bot" | "scheduler">,
): void {
    status[component] = true;
    markStartedIfReady();
}

export function markNotReady(
    component: keyof Pick<HealthStatus, "db" | "schema" | "bot" | "scheduler">,
    error?: string,
): void {
    status[component] = false;
    if (error) status.error = error;
}

export async function isHealthy(): Promise<boolean> {
    await refreshReadiness();
    return status.db && status.schema && status.bot && status.scheduler;
}

export function startHealthServer(): void {
    Bun.serve({
        port: HEALTH_PORT,
        async fetch(req) {
            const url = new URL(req.url);

            if (url.pathname === "/health" || url.pathname === "/ready") {
                const healthy = await isHealthy();
                return Response.json(
                    {
                        status: healthy ? "ok" : "starting",
                        endpoint: url.pathname,
                        uptime: process.uptime(),
                        ...status,
                    },
                    { status: healthy ? 200 : 503 },
                );
            }

            return new Response("", { status: 404 });
        },
    });

    logger.info("health_server_started", { port: HEALTH_PORT });
}

async function refreshReadiness(): Promise<void> {
    status.lastCheckedAt = new Date().toISOString();

    if (!readinessCheck) {
        status.db = false;
        status.schema = false;
        status.error = "readiness_check_not_registered";
        return;
    }

    try {
        const readiness = await readinessCheck();
        status.db = readiness.db;
        status.schema = readiness.schema;
        status.error = readiness.error ?? null;
    } catch (error) {
        status.db = false;
        status.schema = false;
        status.error = error instanceof Error ? error.message : String(error);
    }

    refreshComponent("bot", botCheck);
    refreshComponent("scheduler", schedulerCheck);
    markStartedIfReady();
}

function refreshComponent(
    component: "bot" | "scheduler",
    check: ComponentCheck | null,
): void {
    const detailKey = component === "bot" ? "botDetails" : "schedulerDetails";
    if (!check) {
        status[detailKey] = null;
        return;
    }

    try {
        const result = check();
        status[component] = result.ready;
        status[detailKey] = result.details ?? null;
        if (!result.ready && result.error) status.error = result.error;
    } catch (error) {
        status[component] = false;
        status[detailKey] = null;
        status.error = error instanceof Error ? error.message : String(error);
    }
}

function markStartedIfReady(): void {
    if (status.db && status.schema && status.bot && status.scheduler) {
        status.startedAt ??= new Date().toISOString();
    }
}
