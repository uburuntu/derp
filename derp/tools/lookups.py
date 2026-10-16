"""Web lookups for the toolbox: weather, exchange rates, Wikipedia, Urban Dictionary, arXiv, WolframAlpha, code.

Every call has a deadline and a response-size cap and returns compact text for
the model. Keyless public APIs are used where possible; Wolfram and the code
runner need keys and their tools hide themselves without them. Results that
change slowly are cached in memory for a while. URLs, queries and response
bodies are never logged.
"""

import time
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree

import httpx

from derp.config import Settings

TIMEOUT = httpx.Timeout(10, connect=5)
MAX_BYTES = 1_000_000
USER_AGENT = "DerpBot/2 (+https://t.me/DerpRobot)"
CACHE_SECONDS = 600
WEATHER = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "rime fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}  # fmt: skip
COINS = {
    "btc": "bitcoin", "eth": "ethereum", "ton": "the-open-network", "sol": "solana", "usdt": "tether",
    "bnb": "binancecoin", "xrp": "ripple", "doge": "dogecoin", "ada": "cardano", "trx": "tron", "ltc": "litecoin",
    "not": "notcoin", "usdc": "usd-coin",
}  # fmt: skip
ATOM = "{http://www.w3.org/2005/Atom}"


class LookupError(Exception):  # noqa: A001 - module-scoped name, always imported qualified
    """A lookup failed; ``reason`` is safe to show (no URLs or bodies)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _cyrillic(text: str) -> bool:
    return any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in text)


class Lookups:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client or httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        self._cache: dict[str, tuple[float, Any]] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(
        self, url: str, *, params: dict[str, Any] | None = None, cache: bool = False
    ) -> httpx.Response:
        key = url + repr(sorted((params or {}).items()))
        if cache and (hit := self._cache.get(key)) and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
        try:
            response = await self._client.get(url, params=params)
        except httpx.HTTPError as error:
            raise LookupError(type(error).__name__) from None
        if response.status_code >= 400:
            raise LookupError(f"HTTP {response.status_code}")
        if len(response.content) > MAX_BYTES:
            raise LookupError("response too large")
        if cache:
            self._cache[key] = (time.monotonic(), response)
        return response

    async def weather(self, place: str) -> str:
        found = (
            await self._get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": place, "count": 1, "language": "ru" if _cyrillic(place) else "en"},
                cache=True,
            )
        ).json()
        results = found.get("results") or []
        if not results:
            return f"No place called {place!r} found."
        spot = results[0]
        forecast = (
            await self._get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": spot["latitude"],
                    "longitude": spot["longitude"],
                    "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                    "timezone": "auto",
                    "forecast_days": 3,
                },
                cache=True,
            )
        ).json()
        now = forecast["current"]
        where = ", ".join(str(p) for p in (spot.get("name"), spot.get("admin1"), spot.get("country")) if p)
        lines = [
            (
                f"{where} — now {now['temperature_2m']}°C (feels {now['apparent_temperature']}°C), "
                f"{WEATHER.get(now['weather_code'], 'unknown')}, wind {now['wind_speed_10m']} km/h, "
                f"humidity {now['relative_humidity_2m']}%"
            )
        ]
        daily = forecast["daily"]
        for i, day in enumerate(daily["time"]):
            lines.append(
                f"{day}: {daily['temperature_2m_min'][i]}…{daily['temperature_2m_max'][i]}°C, "
                f"{WEATHER.get(daily['weather_code'][i], 'unknown')}, rain {daily['precipitation_probability_max'][i]}%"
            )
        return "\n".join(lines) + "\n(Open-Meteo)"

    async def exchange_rate(self, base: str, quote_currency: str, amount: float) -> str:
        base, quote_currency = base.strip().lower(), quote_currency.strip().lower()
        if base in COINS or quote_currency in COINS:
            coin, fiat, invert = (
                (base, quote_currency, False) if base in COINS else (quote_currency, base, True)
            )
            data = (
                await self._get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": COINS[coin], "vs_currencies": fiat, "include_24hr_change": "true"},
                    cache=True,
                )
            ).json()
            prices = data.get(COINS[coin]) or {}
            if fiat not in prices:
                return f"No {coin.upper()} price in {fiat.upper()}."
            price = float(prices[fiat])
            change = prices.get(f"{fiat}_24h_change")
            rate = 1 / price if invert else price
            moved = f" ({change:+.1f}% in 24h)" if isinstance(change, int | float) else ""
            return f"{amount:g} {base.upper()} = {amount * rate:,.6g} {quote_currency.upper()}{moved} (CoinGecko)"
        data = (
            await self._get(f"https://open.er-api.com/v6/latest/{quote(base.upper())}", cache=True)
        ).json()
        rates = data.get("rates") or {}
        if data.get("result") != "success" or quote_currency.upper() not in rates:
            return f"No rate for {base.upper()} → {quote_currency.upper()}."
        rate = float(rates[quote_currency.upper()])
        return f"{amount:g} {base.upper()} = {amount * rate:,.6g} {quote_currency.upper()} (open.er-api.com, daily)"

    async def wiki(self, query: str) -> str:
        lang = "ru" if _cyrillic(query) else "en"
        found = (
            await self._get(
                f"https://{lang}.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": query,
                    "srlimit": 1,
                    "format": "json",
                },
            )
        ).json()
        hits = (found.get("query") or {}).get("search") or []
        if not hits:
            return f"Nothing on {lang}.wikipedia.org for {query!r}."
        title = str(hits[0]["title"])
        summary = (
            await self._get(
                f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(title.replace(' ', '_'))}"
            )
        ).json()
        page = ((summary.get("content_urls") or {}).get("desktop") or {}).get("page", "")
        return f"{title}: {str(summary.get('extract', ''))[:1500]}\n{page}"

    async def urban(self, term: str) -> str:
        data = (await self._get("https://api.urbandictionary.com/v0/define", params={"term": term})).json()
        entries = sorted(data.get("list") or [], key=lambda e: -int(e.get("thumbs_up", 0)))[:2]
        if not entries:
            return f"Urban Dictionary has nothing for {term!r}."
        return "\n\n".join(
            f"{e.get('word')}: {str(e.get('definition', '')).replace('[', '').replace(']', '')[:600]}\n"
            f"e.g. {str(e.get('example', '')).replace('[', '').replace(']', '')[:300]}"
            for e in entries
        )

    async def arxiv(self, query: str) -> str:
        response = await self._get(
            "https://export.arxiv.org/api/query",
            params={"search_query": f"all:{query}", "max_results": 5, "sortBy": "relevance"},
        )
        root = ElementTree.fromstring(response.content)  # noqa: S314 - size-capped; expat refuses entity expansion attacks
        lines: list[str] = []
        for entry in root.iter(f"{ATOM}entry"):
            title = " ".join((entry.findtext(f"{ATOM}title") or "").split())
            authors = [a.findtext(f"{ATOM}name") or "" for a in entry.iter(f"{ATOM}author")][:3]
            link = entry.findtext(f"{ATOM}id") or ""
            year = (entry.findtext(f"{ATOM}published") or "")[:4]
            summary = " ".join((entry.findtext(f"{ATOM}summary") or "").split())[:300]
            lines.append(f"{title} ({', '.join(authors)}, {year}) {link}\n  {summary}")
        return "\n".join(lines) or f"No arXiv papers for {query!r}."

    async def wolfram(self, query: str) -> str:
        appid = self.settings.require("wolfram_app_id")
        try:
            response = await self._client.get(
                "https://api.wolframalpha.com/v1/result",
                params={"appid": appid, "i": query, "units": "metric"},
            )
        except httpx.HTTPError as error:
            raise LookupError(type(error).__name__) from None
        if response.status_code == 501:
            return "WolframAlpha has no short answer for that; try rephrasing or compute it with run_code."
        if response.status_code >= 400:
            raise LookupError(f"HTTP {response.status_code}")
        return response.text[:1500] + "\n(WolframAlpha)"

    async def run_program(self, language: str, source: str, stdin: str) -> str:
        client_id, secret = self.settings.require("jdoodle_client_id", "jdoodle_client_secret")
        body = {
            "clientId": client_id, "clientSecret": secret, "script": source[:20_000], "stdin": stdin[:5_000],
            "language": language, "versionIndex": "0",
        }  # fmt: skip
        try:
            response = await self._client.post("https://api.jdoodle.com/v1/execute", json=body, timeout=30)
        except httpx.HTTPError as error:
            raise LookupError(type(error).__name__) from None
        if response.status_code >= 400:
            raise LookupError(f"HTTP {response.status_code}")
        data = response.json()
        output = "\n".join(str(data.get("output") or "").splitlines()[:80])[:3000]
        return f"{output or '(no output)'}\n— cpu {data.get('cpuTime')} s, memory {data.get('memory')} KB"
