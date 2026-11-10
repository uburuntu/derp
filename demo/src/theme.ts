import { loadFont } from "@remotion/fonts";
import { staticFile } from "remotion";

export const FPS = 30;

export const brand = {
  yellow: "#F4C150",
  yellowLight: "#F9D98A",
  yellowDeep: "#E3A933",
  ink: "#1B1A17",
  inkSoft: "#4A4436",
};

// Telegram's night theme, approximately.
export const tg = {
  bg: "#0E1621",
  header: "#17212B",
  incoming: "#182533",
  outgoing: "#2B5278",
  text: "#F5F5F5",
  muted: "#7D8B99",
  outMuted: "#7DA8D3",
  link: "#6AB3F3",
  accent: "#5EB5F7",
  button: "rgba(24, 37, 51, 0.82)",
};

export const font = {
  sans: "'Source Sans 3', -apple-system, system-ui, sans-serif",
  mono: "'JetBrains Mono', ui-monospace, Menlo, monospace",
};

const LATIN =
  "U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2190-2195, U+2212, U+2215, U+FEFF, U+FFFD";
const CYRILLIC = "U+0301, U+0400-045F, U+0490-0491, U+04B0-04B1, U+2116";

// Self-hosted (SIL Open Font License, public/fonts/licenses/), so renders never fetch fonts.
const faces: [string, string, string, string][] = [
  ["Source Sans 3", "fonts/source-sans-3-latin.woff2", "400 700", LATIN],
  ["Source Sans 3", "fonts/source-sans-3-cyrillic.woff2", "400 700", CYRILLIC],
  ["JetBrains Mono", "fonts/jetbrains-mono-latin.woff2", "400 600", LATIN],
  ["JetBrains Mono", "fonts/jetbrains-mono-cyrillic.woff2", "400 600", CYRILLIC],
];
for (const [family, file, weight, unicodeRange] of faces) {
  loadFont({ family, url: staticFile(file), weight, unicodeRange });
}
