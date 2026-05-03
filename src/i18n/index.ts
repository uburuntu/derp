/** i18n setup — @grammyjs/i18n with Fluent .ftl locales */

import path from "node:path";
import { I18n } from "@grammyjs/i18n";
import type { DerpContext } from "../bot/context";

export const DEFAULT_LOCALE = "en";
export const SUPPORTED_LOCALES = ["en", "ru"] as const;

export type SupportedLocale = (typeof SUPPORTED_LOCALES)[number];

const supportedLocaleSet = new Set<string>(SUPPORTED_LOCALES);

export function toSupportedLocale(
	languageCode: string | null | undefined,
): SupportedLocale | undefined {
	if (!languageCode) return undefined;

	const normalized = languageCode.toLowerCase().replace("_", "-");
	if (supportedLocaleSet.has(normalized)) {
		return normalized as SupportedLocale;
	}

	const baseLanguage = normalized.split("-")[0];
	if (baseLanguage && supportedLocaleSet.has(baseLanguage)) {
		return baseLanguage as SupportedLocale;
	}

	return undefined;
}

export function getLocaleForContext(ctx: DerpContext): SupportedLocale {
	return (
		toSupportedLocale(ctx.dbChat?.languageCode) ??
		toSupportedLocale(ctx.dbUser?.languageCode) ??
		toSupportedLocale(ctx.from?.language_code) ??
		DEFAULT_LOCALE
	);
}

export const i18n = new I18n<DerpContext>({
	defaultLocale: DEFAULT_LOCALE,
	directory: path.resolve(import.meta.dir, "locales"),
	useSession: false,
	localeNegotiator: getLocaleForContext,
	fluentBundleOptions: {
		useIsolating: false,
	},
});
