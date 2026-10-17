from derp.history.links import link_references, references
from derp.history.models import Chat
from derp.telegram.text import as_written

LOUNGE = Chat(id=-1001130715084, type="supergroup")
BASE = "https://t.me/c/1130715084"


def test_message_links_depend_on_the_chat() -> None:
    assert LOUNGE.message_link(970746) == f"{BASE}/970746"
    public = Chat(id=-1001234, type="supergroup", username="mechmath")
    assert public.message_link(820712) == "https://t.me/mechmath/820712"
    assert Chat(id=-1005550001, type="channel", username="news").message_link(7) == "https://t.me/news/7"
    assert Chat(id=-4021, type="group").message_link(7) is None  # basic groups have no message links
    assert Chat(id=501, type="private").message_link(7) is None


def test_only_resolved_references_outside_code_become_links() -> None:
    text = (
        "#970746 started it, then #12 and #99 piled on (see [#5](x)), #news, x#12, `#12`\n"
        "```\n#12\n```\n- #12 again"
    )
    assert references(text) == {970746, 12, 99}
    linked = link_references(text, lambda m: LOUNGE.message_link(m) if m in {970746, 12} else None)
    assert linked == (
        f"[#970746]({BASE}/970746) started it, then [#12]({BASE}/12) and #99 piled on "
        f"(see [#5](x)), #news, x#12, `#12`\n```\n#12\n```\n- [#12]({BASE}/12) again"
    )
    assert as_written(linked).startswith(f"[#970746]({BASE}/970746) started it")  # a link, not a heading
