/** i18n setup — @grammyjs/i18n with Fluent .ftl locales */

import path from "node:path";
import { I18n } from "@grammyjs/i18n";
import type { DerpContext } from "../bot/context";

export const i18n = new I18n<DerpContext>({
	defaultLocale: "en",
	directory: path.resolve(import.meta.dir, "locales"),
	useSession: false,
	fluentBundleOptions: {
		useIsolating: false,
	},
});
