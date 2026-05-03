/** Web search tool — Brave Search API (primary), DuckDuckGo (fallback) */

import { z } from "zod";
import { config } from "../config";
import { ModelCapability } from "../llm/registry";
import type { ToolContext, ToolDefinition, ToolResult } from "./types";

const searchParamsSchema = z.object({
	query: z.string().describe("The search query to look up"),
});

type SearchParams = z.infer<typeof searchParamsSchema>;

const SEARCH_TIMEOUT_MS = 10_000;
const SEARCH_RETRY_ATTEMPTS = 2;
const SEARCH_RETRY_DELAY_MS = 1_000;

interface BraveSearchResult {
	web?: {
		results?: Array<{
			title: string;
			url: string;
			description: string;
		}>;
	};
}

function sleep(ms: number): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

function isRetryableStatus(status: number): boolean {
	return status === 429 || status >= 500;
}

async function fetchWithTimeout(
	url: URL,
	init: RequestInit | undefined,
	label: string,
): Promise<Response> {
	const abortController = new AbortController();
	const timeout = setTimeout(() => abortController.abort(), SEARCH_TIMEOUT_MS);

	try {
		return await fetch(url, {
			...init,
			signal: abortController.signal,
		});
	} catch (err) {
		if (abortController.signal.aborted) {
			throw new Error(`${label} timed out after ${SEARCH_TIMEOUT_MS}ms`);
		}
		throw err;
	} finally {
		clearTimeout(timeout);
	}
}

async function fetchWithRetry(
	url: URL,
	init: RequestInit | undefined,
	label: string,
): Promise<Response> {
	let lastError: unknown;
	for (let attempt = 0; attempt < SEARCH_RETRY_ATTEMPTS; attempt++) {
		try {
			const response = await fetchWithTimeout(url, init, label);
			if (
				!isRetryableStatus(response.status) ||
				attempt === SEARCH_RETRY_ATTEMPTS - 1
			) {
				return response;
			}
			lastError = new Error(`${label} API error: ${response.status}`);
		} catch (err) {
			lastError = err;
			if (attempt === SEARCH_RETRY_ATTEMPTS - 1) break;
		}
		await sleep(SEARCH_RETRY_DELAY_MS);
	}

	throw lastError instanceof Error ? lastError : new Error(String(lastError));
}

async function searchBrave(query: string): Promise<string> {
	const apiKey = config.braveSearchApiKey;
	if (!apiKey) throw new Error("BRAVE_SEARCH_API_KEY not configured");

	const url = new URL("https://api.search.brave.com/res/v1/web/search");
	url.searchParams.set("q", query);
	url.searchParams.set("count", "5");

	const response = await fetchWithRetry(
		url,
		{
			headers: {
				Accept: "application/json",
				"Accept-Encoding": "gzip",
				"X-Subscription-Token": apiKey,
			},
		},
		"Brave Search",
	);

	if (!response.ok) {
		throw new Error(`Brave Search API error: ${response.status}`);
	}

	const data = (await response.json()) as BraveSearchResult;
	const results = data.web?.results ?? [];

	if (results.length === 0) {
		return "No search results found.";
	}

	return results
		.map((r, i) => `${i + 1}. ${r.title}\n   ${r.url}\n   ${r.description}`)
		.join("\n\n");
}

async function searchDuckDuckGo(query: string): Promise<string> {
	const url = new URL("https://api.duckduckgo.com/");
	url.searchParams.set("q", query);
	url.searchParams.set("format", "json");
	url.searchParams.set("no_html", "1");
	url.searchParams.set("skip_disambig", "1");

	const response = await fetchWithRetry(url, undefined, "DuckDuckGo");
	if (!response.ok) {
		throw new Error(`DuckDuckGo API error: ${response.status}`);
	}

	const data = (await response.json()) as {
		Abstract?: string;
		AbstractURL?: string;
		RelatedTopics?: Array<{
			Text?: string;
			FirstURL?: string;
		}>;
	};

	const parts: string[] = [];

	if (data.Abstract) {
		parts.push(`${data.Abstract}\nSource: ${data.AbstractURL ?? ""}`);
	}

	const topics = data.RelatedTopics ?? [];
	for (const topic of topics.slice(0, 5)) {
		if (topic.Text && topic.FirstURL) {
			parts.push(`- ${topic.Text}\n  ${topic.FirstURL}`);
		}
	}

	if (parts.length === 0) {
		return "No search results found.";
	}

	return parts.join("\n\n");
}

async function executeSearch(
	params: SearchParams,
	_ctx: ToolContext,
): Promise<ToolResult> {
	try {
		let results: string;
		if (config.braveSearchApiKey) {
			try {
				results = await searchBrave(params.query);
			} catch (braveErr) {
				try {
					results = await searchDuckDuckGo(params.query);
				} catch (duckErr) {
					const braveMsg =
						braveErr instanceof Error ? braveErr.message : String(braveErr);
					const duckMsg =
						duckErr instanceof Error ? duckErr.message : String(duckErr);
					throw new Error(
						`Brave failed: ${braveMsg}; DuckDuckGo failed: ${duckMsg}`,
					);
				}
			}
		} else {
			results = await searchDuckDuckGo(params.query);
		}
		return { text: results };
	} catch (err) {
		const msg = err instanceof Error ? err.message : String(err);
		return { text: `Search failed: ${msg}`, error: msg };
	}
}

export const webSearchTool: ToolDefinition<SearchParams> = {
	name: "webSearch",
	commands: ["/search", "/s"],
	description: "Search the web for current information",
	helpText: "tool-web-search",
	category: "search",
	parameters: searchParamsSchema,
	execute: executeSearch,
	credits: 0,
	freeDaily: 15,
	capability: ModelCapability.TEXT,
	allowAutoCall: true,
};
