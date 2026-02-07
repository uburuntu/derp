/** Health check server — lightweight HTTP endpoint for Docker/deployment probes */

import { logger } from "./observability";

const HEALTH_PORT = Number(process.env.HEALTH_PORT ?? 8080);

interface HealthStatus {
	db: boolean;
	bot: boolean;
	scheduler: boolean;
	startedAt: string | null;
}

const status: HealthStatus = {
	db: false,
	bot: false,
	scheduler: false,
	startedAt: null,
};

export function markReady(
	component: keyof Omit<HealthStatus, "startedAt">,
): void {
	status[component] = true;
	if (status.db && status.bot && status.scheduler && !status.startedAt) {
		status.startedAt = new Date().toISOString();
	}
}

export function isHealthy(): boolean {
	return status.db && status.bot;
}

export function startHealthServer(): void {
	Bun.serve({
		port: HEALTH_PORT,
		fetch(req) {
			const url = new URL(req.url);

			if (url.pathname === "/health") {
				const healthy = isHealthy();
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
