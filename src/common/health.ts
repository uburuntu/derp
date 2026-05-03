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
}

const status: HealthStatus = {
	db: false,
	schema: false,
	bot: false,
	scheduler: false,
	startedAt: null,
	lastCheckedAt: null,
	error: null,
};

export interface ReadinessResult {
	db: boolean;
	schema: boolean;
	error?: string;
}

type ReadinessCheck = () => Promise<ReadinessResult>;

let readinessCheck: ReadinessCheck | null = null;

export function setReadinessCheck(check: ReadinessCheck): void {
	readinessCheck = check;
}

export function markReady(
	component: keyof Pick<HealthStatus, "db" | "schema" | "bot" | "scheduler">,
): void {
	status[component] = true;
	markStartedIfReady();
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

			if (url.pathname === "/health") {
				const healthy = await isHealthy();
				return Response.json(
					{
						status: healthy ? "ok" : "starting",
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

	markStartedIfReady();
}

function markStartedIfReady(): void {
	if (status.db && status.schema && status.bot && status.scheduler) {
		status.startedAt ??= new Date().toISOString();
	}
}
