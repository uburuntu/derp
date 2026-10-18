import re
from types import SimpleNamespace
from typing import Any, cast

from pydantic_ai import RunContext

from derp.agent.deps import RunDeps
from derp.agent.tools import FAMILIES
from derp.agent.tools.about import (
    GUIDE,
    TOPICS,
    about_derp,
    guide,
    guide_sections,
    read_source,
    search_source,
)
from derp.config import Settings, WatchLimits
from derp.settings.start import GROUP_COMMANDS, PRIVATE_COMMANDS

SETTINGS = Settings(_env_file=None, source_revision="abc123")  # pyright: ignore[reportCallIssue]


def render(*topics: str, settings: Settings = SETTINGS) -> str:
    return guide(
        cast(Any, topics), settings=settings, username="DerpRobot", families=FAMILIES, voice_credits=9
    )


def context(settings: Settings = SETTINGS) -> RunContext[RunDeps]:
    return cast(RunContext[RunDeps], SimpleNamespace(deps=SimpleNamespace(settings=settings)))


def test_the_written_guide_holds_no_numbers_so_values_cannot_go_stale() -> None:
    text = GUIDE.read_text("utf-8")
    assert not re.findall(r"\d", text), "numbers belong in the generated facts (derp/agent/tools/about.py)"
    assert set(guide_sections()) <= set(TOPICS)
    assert all(topic in (about_derp.__doc__ or "") for topic in TOPICS)  # the model learns topics from it


def test_facts_follow_the_running_configuration() -> None:
    changed = SETTINGS.model_copy(update={"sponsored_daily_credits": 77})
    assert "77 Derpcoins per person per UTC day" in render("money", settings=changed)
    money = render("money")
    assert all(f"{pack.stars}⭐ → {pack.credits:,}" in money for pack in SETTINGS.packs)
    assert f"{SETTINGS.plan.stars}⭐" in money
    assert "the longest costs up to about 9 Derpcoins" in money  # priced by voice.rates, passed in
    watching = SETTINGS.watching.model_copy(
        update={"paid": WatchLimits(clip_seconds=90, audio_seconds=60, pdf_pages=7)}
    )
    limits = render("limits", settings=SETTINGS.model_copy(update={"watching": watching}))
    assert (
        "paid answers take clips with sound up to 1.5 minutes, audio up to 1 minute and PDFs up to 7 pages"
        in limits
    )


def test_commands_and_every_registered_tool_are_listed() -> None:
    commands = render("commands")
    assert all(f"/{name} — " in commands for name in PRIVATE_COMMANDS)
    assert all(f"/{name}@DerpRobot" in commands for name in GROUP_COMMANDS)
    tools = render("tools")
    names = [name for family in FAMILIES for name in family.toolset.tools]
    assert all(f"- {name} (" in tools for name in names)
    assert "- generate_image (every tier)" in tools and "- read_source (paid, sponsored, toolbox)" in tools


async def test_source_search_and_read_link_the_deployed_revision() -> None:
    found = await search_source(context(), "sponsored_daily_credits", under="derp/config.py")
    assert found.startswith("derp/config.py:")
    line = int(found.split(":")[1])
    read = await read_source(context(), "config.py", start_line=line)
    assert f"https://github.com/uburuntu/derp/blob/abc123/derp/config.py#L{line}-L" in read
    assert "sponsored_daily_credits" in read.splitlines()[1]


async def test_source_reads_stay_inside_the_package() -> None:
    for path in ("../pyproject.toml", "derp/../.env", "/etc/passwd", "derp/__pycache__/app.cpython-314.pyc"):
        assert (await read_source(context(), path)).startswith("No such file")
    assert await search_source(context(), "ab") == "Search for at least 3 characters."
