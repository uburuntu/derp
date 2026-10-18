"""Derp's knowledge of itself: a guide assembled from what is running, and its own source code.

``about_derp`` joins the concept guide (``derp/agent/guide.md``, which holds no
numbers) with facts generated at call time — prices and allowances from settings,
the command menus, the tool registry with tiers, the privacy and terms pages — so
it can't drift from the deployed bot. ``search_source`` / ``read_source`` read the
package shipped in the image (code, developer notes, locales) and link each
excerpt to the deployed revision on GitHub.
"""

from collections.abc import Sequence
from functools import cache
from pathlib import Path
from typing import Literal, get_args

from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

import derp
from derp.agent.deps import RunDeps
from derp.agent.toolsets import ALL_TIERS, Family
from derp.config import Settings
from derp.inference.openrouter import REASONING_ROOM
from derp.inference.roles import ModelRole
from derp.locales import t
from derp.memory import notes
from derp.money.funding import Funding
from derp.scheduler import jobs
from derp.settings.start import EPHEMERAL_IN_GROUPS, GROUP_COMMANDS, PRIVATE_COMMANDS

Topic = Literal[
    "overview", "groups", "money", "memory", "reminders", "privacy", "terms", "limits", "commands", "tools",
    "source",
]  # fmt: skip
TOPICS: tuple[Topic, ...] = get_args(Topic)
GUIDE = Path(__file__).parents[1] / "guide.md"
PACKAGE = Path(derp.__file__).parent
SOURCE_SUFFIXES = (".py", ".md", ".toml")
MATCHES = 40
READ_LINES = 150


@cache
def guide_sections() -> dict[str, str]:
    sections: dict[str, str] = {}
    for chunk in GUIDE.read_text("utf-8").split("\n## ")[1:]:
        name, _, body = chunk.partition("\n")
        sections[name.strip()] = body.strip()
    return sections


def _tools(families: Sequence[Family]) -> list[str]:
    lines: list[str] = []
    for family in families:
        tiers = "every tier" if family.tiers == ALL_TIERS else ", ".join(sorted(family.tiers))
        for name, tool in family.toolset.tools.items():
            toolbox = family.deferred is True or (
                isinstance(family.deferred, tuple) and name in family.deferred
            )
            summary = (tool.description or "").strip().partition("\n")[0]
            lines.append(f"- {name} ({tiers}{', toolbox' if toolbox else ''}): {summary}")
    return lines


def facts(topic: Topic, settings: Settings, username: str, families: Sequence[Family]) -> list[str]:
    """The current values for a topic, generated from what is running."""
    match topic:
        case "overview":
            return [f"- This bot: @{username}."]
        case "money":
            plan = settings.plan
            speech = settings.speech
            voice_credits = settings.credits_for_usd(
                speech.price_usd(settings.model_pins[ModelRole.SPEECH].model, speech.max_chars)
            )
            return [
                (
                    f"- 1 Derpcoin = ${settings.usd_per_credit:g} of provider cost; answers are charged at cost × "
                    f"{settings.markup:g}, rounded up."
                ),
                f"- Free daily allowance: {settings.sponsored_daily_credits} Derpcoins per person per UTC day.",
                "- Packs: "
                + ", ".join(f"{p.stars}⭐ → {p.credits:,}" for p in settings.packs)
                + f" Derpcoins ({settings.credits_per_star} per Star).",
                (
                    f"- Derp+: {plan.stars}⭐ every {plan.period_seconds // 86400} days → {plan.credits:,} Derpcoins "
                    "for that period."
                ),
                (
                    f"- Asks first above {settings.approval_credits} Derpcoins or "
                    f"{settings.approval_balance_fraction:.0%} of the payer's balance; the button lasts "
                    f"{settings.approval_minutes} minutes."
                ),
                f"- One image: up to about {settings.credits_for_usd(settings.image_price_usd)} Derpcoins.",
                (
                    f"- Heavy media effects (video memes, reversed clips, cutouts): +{settings.effect_credits} "
                    f"Derpcoins on paid answers; {settings.sponsored_effects_per_day} per person per day on the "
                    "free allowance, voice messages included."
                ),
                (
                    f"- A voice message: up to {settings.speech.max_chars} characters; the longest costs about "
                    f"{voice_credits} Derpcoins."
                ),
            ]
        case "memory":
            return [
                (
                    f"- Notes up to {notes.NOTE_CHARS} characters; {notes.CHAT_NOTES_CAP} chat notes and "
                    f"{notes.USER_NOTES_CAP} personal notes per person per chat; rules up to {notes.RULES_CHARS} "
                    "characters."
                )
            ]
        case "reminders":
            return [
                (
                    f"- {jobs.CHAT_LIMIT} active jobs per chat, {jobs.PERSON_LIMIT} per person per chat, up to "
                    f"{jobs.MAX_AHEAD.days} days ahead; {jobs.MISS_LIMIT} unpaid firings in a row pause a job."
                )
            ]
        case "privacy":
            retention = settings.retention
            options = ", ".join(str(days) for days in retention.options)
            return [
                (
                    f"- Kept by default {retention.group_days} days in groups and {retention.private_days} in "
                    f"private chats; /settings offers {options} days. Day summaries last up to "
                    f"{retention.digest_days} days."
                ),
                t("en", "pages.privacy").strip(),
            ]
        case "terms":
            return [t("en", "pages.terms").strip()]
        case "limits":
            caps, media = settings.run_caps, settings.media
            return [
                (
                    f"- Per answer: {caps.tool_calls} tool calls, {caps.visible_outputs} visible outputs (messages, "
                    f"polls, stickers…), {caps.output_tokens:,} output tokens per model reply (up to "
                    f"{caps.output_tokens * max(REASONING_ROOM.values()):,} when it thinks harder), "
                    f"{caps.timeout_seconds:g} seconds, {caps.credits} Derpcoins."
                ),
                (
                    f"- Media: files up to {media.max_input_bytes // 2**20} MB and {media.max_pixels / 1e6:g} "
                    f"megapixels; voice, audio and video transcribed up to {media.voice_max_seconds // 60} minutes."
                ),
            ]
        case "commands":
            private = [f"- /{name} — {t('en', f'commands.{name}')}" for name in PRIVATE_COMMANDS]
            group = [
                f"- /{name}@{username} — {t('en', f'commands.{name}')}"
                + (" (ephemeral)" if name in EPHEMERAL_IN_GROUPS else "")
                for name in GROUP_COMMANDS
            ]
            return ["Private chats:", *private, "Groups:", *group]
        case "tools":
            return _tools(families)
        case "source":
            return [
                (
                    f"- Repository: {settings.source_url}; this deployment: "
                    f"{settings.source_url}/tree/{settings.source_revision}"
                )
            ]
        case _:
            return []


def guide(topics: Sequence[Topic], *, settings: Settings, username: str, families: Sequence[Family]) -> str:
    parts: list[str] = []
    for topic in dict.fromkeys(topics or TOPICS):
        body = [guide_sections().get(topic, ""), *facts(topic, settings, username, families)]
        parts.append(f"## {topic}\n" + "\n".join(line for line in body if line))
    return "\n\n".join(parts)


toolset = FunctionToolset[RunDeps]()


@toolset.tool
async def about_derp(ctx: RunContext[RunDeps], topics: list[Topic]) -> str:
    """How Derp itself works, with current prices and limits. Answer questions about Derp from this, never memory.

    Args:
        topics: Sections to read — overview, groups, money (prices, packs, Derp+, who pays), memory, reminders,
            privacy, terms, limits, commands, tools (what Derp can do, per tier), source. Pick several if unsure.
    """
    from derp.agent.tools import FAMILIES  # the registry imports this module

    deps = ctx.deps
    me = await deps.bot.me()
    families = [family for family in FAMILIES if family.available(deps)]
    return guide(topics, settings=deps.settings, username=me.username or "", families=families)


@cache
def source_files() -> dict[str, tuple[str, ...]]:
    """The shipped package by repository path; reads never leave it (lookups are by key, not by path)."""
    files: dict[str, tuple[str, ...]] = {}
    for path in sorted(PACKAGE.rglob("*")):
        if path.suffix not in SOURCE_SUFFIXES or not path.is_file() or path.is_symlink():
            continue
        if "__pycache__" not in path.parts:
            key = "derp/" + path.relative_to(PACKAGE).as_posix()
            files[key] = tuple(path.read_text("utf-8", errors="replace").splitlines())
    return files


def repository_path(path: str) -> str:
    path = path.strip().lstrip("/")
    return path if path.startswith("derp/") else "derp/" + path


source_toolset = FunctionToolset[RunDeps]()


@source_toolset.tool
async def search_source(ctx: RunContext[RunDeps], text: str, under: str = "") -> str:
    """Search Derp's own source code, developer notes (AGENTS.md) and texts: a case-insensitive text match.

    Args:
        text: What to find: a function, setting, message text or key, e.g. "sponsored_daily_credits".
        under: Only files under this path, e.g. "derp/money/".
    """
    needle = text.strip().lower()
    if len(needle) < 3:
        return "Search for at least 3 characters."
    prefix = repository_path(under) if under.strip() else "derp/"
    hits: list[str] = []
    for path, lines in source_files().items():
        if not path.startswith(prefix):
            continue
        hits.extend(
            f"{path}:{n}: {line.strip()[:160]}" for n, line in enumerate(lines, 1) if needle in line.lower()
        )
        if len(hits) >= MATCHES:
            return "\n".join(hits[:MATCHES]) + "\n\n(First matches only: narrow the search.)"
    return "\n".join(hits) + "\n\nOpen a file with read_source." if hits else f"No matches under {prefix}."


@source_toolset.tool
async def read_source(ctx: RunContext[RunDeps], path: str, start_line: int = 1) -> str:
    """Read one of Derp's own files with line numbers and a GitHub link to cite.

    Args:
        path: Repository path, e.g. "derp/money/funding.py" or "derp/agent/AGENTS.md".
        start_line: First line to show; a page is up to 150 lines.
    """
    key = repository_path(path)
    lines = source_files().get(key)
    if lines is None:
        name = key.rsplit("/", 1)[-1]
        close = [p for p in source_files() if p.endswith("/" + name)][:5]
        return f"No such file in Derp's source: {key}." + (
            f" Did you mean {', '.join(close)}?" if close else ""
        )
    start = max(1, start_line)
    if start > len(lines):
        return f"{key} has {len(lines)} lines."
    end = min(len(lines), start + READ_LINES - 1)
    settings = ctx.deps.settings
    url = f"{settings.source_url}/blob/{settings.source_revision}/{key}#L{start}-L{end}"
    body = "\n".join(f"{n:>4} {lines[n - 1]}" for n in range(start, end + 1))
    return f"{key}, lines {start}–{end} of {len(lines)}: {url}\n{body}"


family = Family("about", toolset, detached="about_derp")
source_family = Family(
    "source",
    source_toolset,
    tiers=frozenset({Funding.PAID, Funding.SPONSORED}),
    deferred=True,
    detached="Derp's own source code",
)
