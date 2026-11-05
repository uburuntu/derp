from collections.abc import Callable

import httpx
import pytest

from derp.config import Settings
from derp.tools import lookups
from derp.tools.lookups import Lookups

Handler = Callable[[httpx.Request], httpx.Response]


def make(settings: Settings, handler: Handler) -> Lookups:
    return Lookups(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_json_answers_are_kept_for_their_ttl(settings: Settings) -> None:
    seen: list[httpx.URL] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json={"asked": len(seen)})

    tools = make(settings, handle)
    assert await tools.json("https://x.example/a", params={"q": 1}, ttl=60) == {"asked": 1}
    assert await tools.json("https://x.example/a", params={"q": 1}, ttl=60) == {"asked": 1}
    assert await tools.json("https://x.example/a", params={"q": 2}, ttl=60) == {"asked": 2}
    assert await tools.json("https://x.example/a", params={"q": 1}) == {"asked": 3}  # no ttl: asked again
    garbled = make(settings, lambda _r: httpx.Response(200, content=b"<html>"))
    with pytest.raises(lookups.LookupError, match="unexpected response"):
        await garbled.json("https://x.example/a")
    await tools.close()


async def test_a_blip_is_asked_once_more_and_an_http_error_never(settings: Settings) -> None:
    seen: list[int] = []

    def flaky(failures: int, status: int = 200) -> Handler:
        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(1)
            if len(seen) <= failures:
                raise httpx.ReadTimeout("slow", request=request)
            return httpx.Response(status, json={"ok": True})

        return handle

    assert await make(settings, flaky(1)).json("https://x.example/a") == {"ok": True}
    assert len(seen) == 2
    seen.clear()
    with pytest.raises(lookups.LookupError, match="ReadTimeout"):
        await make(settings, flaky(2)).json("https://x.example/a")
    assert len(seen) == 2
    seen.clear()
    with pytest.raises(lookups.LookupError, match="HTTP 503"):
        await make(settings, flaky(0, status=503)).json("https://x.example/a")
    assert len(seen) == 1


async def test_wikipedia_picks_the_language_by_script(settings: Settings) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "ru.wikipedia.org"
        if request.url.path.endswith("api.php"):
            return httpx.Response(200, json={"query": {"search": [{"title": "Казань"}]}})
        return httpx.Response(
            200, json={"extract": "Город.", "content_urls": {"desktop": {"page": "https://x"}}}
        )

    assert await make(settings, handle).wiki("казань") == "Казань: Город.\nhttps://x"


async def test_arxiv_entries_are_compact(settings: Settings) -> None:
    feed = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/1</id>
    <published>2017-06-12</published><title>Attention  Is All
    You Need</title><summary>Transformers.</summary><author><name>A. Vaswani</name></author></entry></feed>"""
    tools = make(settings, lambda _r: httpx.Response(200, content=feed))
    assert await tools.arxiv("attention") == (
        "Attention Is All You Need (A. Vaswani, 2017) http://arxiv.org/abs/1\n  Transformers."
    )


async def test_failures_carry_no_urls_or_bodies(settings: Settings) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret-host.example", request=request)

    with pytest.raises(lookups.LookupError) as down:
        await make(settings, boom).urban("rizz")
    assert down.value.reason == "ConnectError"
    big = make(settings, lambda _r: httpx.Response(200, content=b"x" * (lookups.MAX_BYTES + 1)))
    with pytest.raises(lookups.LookupError, match="too large"):
        await big.urban("rizz")


async def test_keyed_lookups_send_their_keys_only_to_their_service(settings: Settings) -> None:
    keyed = settings.model_copy(
        update={"wolfram_app_id": "W-KEY", "jdoodle_client_id": "id", "jdoodle_client_secret": "J-KEY"}
    )

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.wolframalpha.com":
            assert request.url.params["appid"] == "W-KEY"
            return httpx.Response(501) if request.url.params["i"] == "??" else httpx.Response(200, text="42")
        assert request.url.host == "api.jdoodle.com" and b"J-KEY" in request.content
        return httpx.Response(200, json={"output": "hi\n", "cpuTime": "0.01", "memory": "1024"})

    tools = make(keyed, handle)
    assert await tools.wolfram("meaning of life") == "42\n(WolframAlpha)"
    assert "no short answer" in await tools.wolfram("??")
    assert await tools.run_program("nodejs", "console.log('hi')", "") == "hi\n— cpu 0.01 s, memory 1024 KB"


async def test_odd_answers_fail_the_lookup_not_the_run(settings: Settings) -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        if request.url.path.endswith("api.php"):
            return httpx.Response(200, json={"query": {"search": [{"title": "AC/DC"}]}})
        return httpx.Response(200, text="<html>maintenance</html>")

    tools = make(settings, handle)
    with pytest.raises(lookups.LookupError, match="unexpected response"):
        await tools.wiki("acdc")
    assert seen[-1].endswith("/page/summary/AC%2FDC")
    with pytest.raises(lookups.LookupError, match="unexpected response"):
        await make(settings, lambda _r: httpx.Response(200, content=b"<feed><entry>")).arxiv("x")
