"""Market prices for the ``market`` tool: currencies, crypto, DEX tokens, stocks, funds, indices and commodities.

Keyless public sources, asked in an order that suits the name: Binance spot for crypto pairs (and its futures
for funding and open interest), the Moscow Exchange's ISS for Russian shares and indices, Yahoo Finance's
chart API for everything else traded (stocks, funds, indices, futures such as gold and Brent, currency pairs),
CoinGecko for coins Binance doesn't list and GeckoTerminal for on-chain DEX pools. An answer is a ``Quote``:
the price now, a price series over the asked period (change, range, stats, chart) and facts worth knowing
(volume, liquidity, funding). Requests go through ``Lookups.json`` (deadline, size cap, short cache), so an
outage is a ``LookupError`` like any lookup's; a source that doesn't know the name answers None and the next
one is asked.
"""

import asyncio
import itertools
import json
import math
import re
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BeforeValidator

from derp.tools.lookups import LookupError, Lookups  # noqa: A004 - the lookups' own error

BINANCE = "https://api.binance.com/api/v3"
FUTURES = "https://fapi.binance.com/fapi/v1"
COINGECKO = "https://api.coingecko.com/api/v3"
GECKOTERMINAL = "https://api.geckoterminal.com/api/v2"
YAHOO = "https://query1.finance.yahoo.com"
MOEX = "https://iss.moex.com/iss"
POLYMARKET = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
EVENTS_BYTES = 4_000_000  # Polymarket events carry every market's rules: a few busy ones run past a megabyte
POPULAR = frozenset(
    {"", "popular", "trending", "top", "hot", "популярное", "топ"}
)  # Polymarket's busiest events
MSK = ZoneInfo("Europe/Moscow")  # the Moscow Exchange's clock
LIVE, CANDLES, NAMES = 30, 60, 3600  # cache seconds: prices, price series, name searches
# Binance quote assets a pair may use as is; other currencies go through USDT and the dollar's rate.
PAIR_QUOTES = frozenset({"USDT", "USDC", "FDUSD", "BTC", "ETH", "BNB", "EUR", "TRY", "BRL", "JPY"})
FIAT = frozenset({
    "USD", "EUR", "RUB", "GBP", "CNY", "JPY", "CHF", "KZT", "UAH", "BYN", "TRY", "AED", "GEL", "AMD", "UZS", "KGS",
    "AZN", "TJS", "INR", "CAD", "AUD", "NZD", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "RON", "BGN", "ILS", "HKD",
    "SGD", "KRW", "THB", "VND", "IDR", "MYR", "PHP", "BRL", "MXN", "ARS", "CLP", "COP", "ZAR", "EGP", "SAR", "QAR",
    "RSD", "MDL",
})  # fmt: skip
# Words people use for what Yahoo or the Moscow Exchange know by a symbol.
ALIASES = {
    "gold": "GC=F", "золото": "GC=F", "xau": "GC=F", "silver": "SI=F", "серебро": "SI=F", "xag": "SI=F",
    "oil": "BZ=F", "brent": "BZ=F", "нефть": "BZ=F", "брент": "BZ=F", "wti": "CL=F", "gas": "NG=F", "газ": "NG=F",
    "copper": "HG=F", "медь": "HG=F", "platinum": "PL=F", "платина": "PL=F", "s&p 500": "^GSPC", "s&p": "^GSPC",
    "sp500": "^GSPC", "spx": "^GSPC", "nasdaq": "^IXIC", "dow": "^DJI", "dow jones": "^DJI", "dax": "^GDAXI",
    "nikkei": "^N225", "vix": "^VIX", "dxy": "DX-Y.NYB", "мосбиржа": "IMOEX", "индекс мосбиржи": "IMOEX",
}  # fmt: skip
COINS = {
    "bitcoin": "BTC", "биткоин": "BTC", "биток": "BTC", "ethereum": "ETH", "эфир": "ETH", "эфириум": "ETH",
    "toncoin": "TON", "тон": "TON", "solana": "SOL", "солана": "SOL", "dogecoin": "DOGE", "notcoin": "NOT",
}  # fmt: skip
MOEX_BOARDS = {"TQBR": "shares", "TQTF": "shares", "SNDX": "index"}
TICKER = re.compile(r"[A-Za-z0-9^][A-Za-z0-9.\-=^]{0,14}")
PERIOD_WORDS = {
    "day": "1d", "today": "1d", "week": "1w", "month": "1m", "quarter": "3m", "year": "1y", "ytd": "1y",
    "max": "5y", "all": "5y",
}  # fmt: skip
UNITS = {"min": 60, "h": 3600, "d": 86400, "w": 7 * 86400, "m": 30 * 86400, "y": 365 * 86400}
KINDS = ("crypto", "dex", "stock", "prediction")
BETS = frozenset({"polymarket", "odds", "bet", "bets", "betting"})


def _period(value: Any) -> Any:
    """A period as models write it ("7d", "30 days", "6mo", "week") as the nearest span; anything else as is."""
    if not isinstance(value, str) or value in SPANS:
        return value
    text = re.sub(r"\s+", "", value.lower())
    if text in PERIOD_WORDS:
        return PERIOD_WORDS[text]
    if not (match := re.fullmatch(r"(\d+)(min|h|d|w|m|y)[a-z]*", text)) or not int(match[1]):
        return value
    seconds = int(match[1]) * UNITS[match[2]]
    return min(SPANS, key=lambda key: abs(math.log(seconds / SPANS[key].seconds)))


def _kind(value: Any) -> Any:
    """A kind as models write it; one naming no kind here is none ("currency": currencies need no kind)."""
    if not isinstance(value, str):
        return value
    text = value.strip().lower()
    return text if text in KINDS else "prediction" if text in BETS else None


# Plain aliases, not ``type`` statements: tool schemas inline them (see derp/agent/tools/AGENTS.md).
Period = Annotated[Literal["1d", "1w", "1m", "3m", "1y", "5y"], BeforeValidator(_period)]
Kind = Annotated[Literal["crypto", "dex", "stock", "prediction"] | None, BeforeValidator(_kind)]


@dataclass(frozen=True, slots=True)
class Span:
    seconds: int  # how far back the period reaches
    binance: tuple[str, int]  # kline interval, count
    yahoo: tuple[str, str]  # range, interval
    moex: tuple[int, int]  # ISS candle interval, days back
    gecko: tuple[str, int, int]  # GeckoTerminal timeframe, aggregate, count
    days: int  # CoinGecko's market_chart span
    polymarket: tuple[str, int]  # Polymarket's history interval, minutes a point
    label: str


SPANS: dict[str, Span] = {
    "1d": Span(86400, ("15m", 96), ("1d", "5m"), (10, 1), ("minute", 15, 96), 1, ("1d", 15), "day"),
    "1w": Span(7 * 86400, ("1h", 168), ("5d", "30m"), (60, 7), ("hour", 1, 168), 7, ("1w", 60), "week"),
    "1m": Span(30 * 86400, ("4h", 180), ("1mo", "1d"), (24, 31), ("hour", 4, 180), 30, ("1m", 240), "month"),
    "3m": Span(
        91 * 86400, ("1d", 92), ("3mo", "1d"), (24, 92), ("day", 1, 92), 90, ("max", 1440), "3 months"
    ),
    "1y": Span(
        365 * 86400, ("1d", 365), ("1y", "1d"), (24, 366), ("day", 1, 365), 365, ("max", 1440), "year"
    ),
    "5y": Span(
        1826 * 86400, ("1w", 261), ("5y", "1wk"), (7, 1827), ("day", 1, 1000), 1825, ("max", 1440), "5 years"
    ),
}


@dataclass(slots=True)
class Quote:
    name: str  # "Bitcoin", "Apple Inc."
    symbol: str  # "BTC/USDT", "AAPL"
    source: str  # "Binance", "Yahoo Finance, NasdaqGS, may lag"…
    currency: str  # what prices are in
    price: float
    series: list[tuple[float, float]] = field(
        default_factory=list[tuple[float, float]]
    )  # (unix s, close), oldest first
    previous: float | None = None  # the close a day's move is measured from, when the source gives one
    facts: list[str] = field(default_factory=list[str])  # volume, liquidity, funding…
    year_steps: float | None = None  # series steps in a year where volatility means something (None: skip it)
    lines: list[tuple[str, list[tuple[float, float]]]] = field(
        default_factory=list[tuple[str, list[tuple[float, float]]]]
    )  # a prediction market's likeliest outcomes, each charted as its own line
    link: str | None = None  # where people can see it for themselves (a Polymarket event)

    def priced_in(self, rate: float, currency: str) -> Quote:
        """The same quote in another currency, at today's ``rate``."""
        return replace(
            self,
            price=self.price * rate,
            series=[(at, close * rate) for at, close in self.series],
            previous=self.previous * rate if self.previous is not None else None,
            currency=currency,
            facts=[*self.facts, f"converted at {price_text(rate)} {currency} per {self.currency}"],
        )

    def inverted(self, currency: str) -> Quote:
        """One ``currency`` (what this quote is priced in) in the base: USD/BTC from BTC/USDT."""
        base = self.symbol.split("/")[0]
        return replace(
            self,
            name=f"{currency}/{base}",
            symbol=f"{currency}/{base}",
            price=1 / self.price,
            series=[(at, 1 / close) for at, close in self.series if close],
            previous=1 / self.previous if self.previous else None,
            currency=base,
            facts=[],
        )


type Source = Callable[[Lookups, str, str | None, Span], Awaitable[Quote | None]]


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except TypeError, ValueError:
        return None
    return number if math.isfinite(number) else None


def _rows(table: dict[str, Any]) -> list[dict[str, Any]]:
    """An ISS table (columns + data) as dicts."""
    return [dict(zip(table["columns"], row, strict=True)) for row in table["data"]]


async def _maybe(lookups: Lookups, url: str, params: dict[str, Any] | None = None, *, ttl: float) -> Any:
    """JSON from a source, or None when it doesn't know what was asked (HTTP 400 or 404)."""
    try:
        return await lookups.json(url, params=params, ttl=ttl)
    except LookupError as error:
        if error.reason in ("HTTP 400", "HTTP 404"):
            return None
        raise


async def binance(lookups: Lookups, base: str, quote: str | None, span: Span) -> Quote | None:
    base = COINS.get(base.lower(), base).upper()
    wanted = "USDT" if quote in (None, "USD") else quote
    pair_quote = wanted if wanted in PAIR_QUOTES else "USDT"
    if not re.fullmatch(r"[A-Z0-9]{2,12}", base) or base == pair_quote:
        return None
    symbol = base + pair_quote
    ticker = await _maybe(lookups, f"{BINANCE}/ticker/24hr", {"symbol": symbol}, ttl=LIVE)
    if not ticker:
        return None
    interval, count = span.binance
    klines = await lookups.json(
        f"{BINANCE}/klines", params={"symbol": symbol, "interval": interval, "limit": count}, ttl=CANDLES
    )
    found = Quote(
        name=base,
        symbol=f"{base}/{pair_quote}",
        source="Binance",
        currency=pair_quote,
        price=float(ticker["lastPrice"]),
        series=[(row[0] / 1000, float(row[4])) for row in klines],
        previous=_float(ticker.get("openPrice")) if span is SPANS["1d"] else None,  # 24 hours ago
        year_steps=365.25 * 86400 / _seconds(interval),
    )
    if volume := _float(ticker.get("quoteVolume")):
        found.facts.append(f"24h volume {_big(volume)} {pair_quote}")
    if pair_quote == "USDT":
        found.facts += await _perpetual(lookups, symbol, base)
    if wanted == pair_quote:
        return found
    rate = await fx_rate(lookups, "USD", wanted)  # a currency Binance doesn't pair: through the dollar
    return found.priced_in(rate, wanted) if rate else None


async def _perpetual(lookups: Lookups, symbol: str, base: str) -> list[str]:
    """Funding and open interest of the coin's USDT perpetual on Binance futures, when it has one."""
    premium = await _maybe(lookups, f"{FUTURES}/premiumIndex", {"symbol": symbol}, ttl=LIVE)
    if not premium:
        return []
    facts: list[str] = []
    if (rate := _float(premium.get("lastFundingRate"))) is not None:
        facts.append(f"perpetual funding {rate * 100:+.4f}% per 8h")
    interest = await _maybe(lookups, f"{FUTURES}/openInterest", {"symbol": symbol}, ttl=LIVE)
    if interest and (amount := _float(interest.get("openInterest"))) is not None:
        mark = _float(premium.get("markPrice"))
        facts.append(
            f"open interest {_big(amount)} {base}" + (f" ({_big(amount * mark)} USDT)" if mark else "")
        )
    return facts


async def coingecko(lookups: Lookups, name: str, quote: str | None, span: Span) -> Quote | None:
    coins = (await lookups.json(f"{COINGECKO}/search", params={"query": name}, ttl=NAMES)).get("coins") or []
    wanted = name.lower()
    exact = [c for c in coins if wanted in (str(c.get("symbol", "")).lower(), str(c.get("name", "")).lower())]
    if not (picks := exact or coins[:1]):
        return None
    coin = min(picks, key=lambda c: c.get("market_cap_rank") or 10**9)
    currency = "usd" if quote in (None, "USD", "USDT", "USDC") else quote.lower()
    rows = await lookups.json(
        f"{COINGECKO}/coins/markets", params={"vs_currency": currency, "ids": coin["id"]}, ttl=LIVE
    )
    if not rows:
        return None
    row = rows[0]
    chart = await lookups.json(
        f"{COINGECKO}/coins/{coin['id']}/market_chart",
        params={"vs_currency": currency, "days": span.days},
        ttl=CANDLES,
    )
    series = [(at / 1000, float(price)) for at, price in chart.get("prices") or []]
    found = Quote(
        name=str(row.get("name") or coin["id"]),
        symbol=str(row.get("symbol") or name).upper(),
        source="CoinGecko",
        currency=currency.upper(),
        price=float(row["current_price"]),
        series=series,
        year_steps=365.25 * 86400 / _step(series) if len(series) > 2 else None,
    )
    if cap := _float(row.get("market_cap")):
        rank = f", rank #{row['market_cap_rank']}" if row.get("market_cap_rank") else ""
        found.facts.append(f"market cap {_big(cap)} {found.currency}{rank}")
    if volume := _float(row.get("total_volume")):
        found.facts.append(f"24h volume {_big(volume)} {found.currency}")
    if (high := _float(row.get("ath"))) and (off := _float(row.get("ath_change_percentage"))) is not None:
        found.facts.append(f"all-time high {price_text(high)} ({off:+.0f}% from it)")
    return found


def _sides(pool: dict[str, Any]) -> tuple[str, str]:
    """A pool's two token symbols from its name ("DOGS / TON 0.25%": DOGS, TON)."""
    base, _, quote = str(pool["attributes"].get("name", "")).partition(" / ")
    return base.strip().lstrip("$").lower(), (quote.split() or [""])[0].lstrip("$").lower()


async def dex(lookups: Lookups, name: str, quote: str | None, span: Span) -> Quote | None:
    """An on-chain token on GeckoTerminal: of the tokens going by the name, the one with the most liquidity across
    its pools (a symbol can name different tokens on different chains), priced in its deepest pool, in USD."""
    found = await lookups.json(f"{GECKOTERMINAL}/search/pools", params={"query": name}, ttl=NAMES)
    wanted = name.strip().lstrip("$").lower()
    holdings: dict[str, list[tuple[float, str, dict[str, Any]]]] = {}  # token id: (liquidity, side, pool)
    for pool in found.get("data") or []:
        for side, symbol in zip(("base", "quote"), _sides(pool), strict=True):
            if symbol == wanted:
                token = pool["relationships"][f"{side}_token"]["data"]["id"]
                liquidity = _float(pool["attributes"].get("reserve_in_usd")) or 0
                holdings.setdefault(token, []).append((liquidity, side, pool))
    if not holdings:
        return None
    pools = max(holdings.values(), key=lambda held: sum(liquidity for liquidity, _, _ in held))
    liquidity, side, pool = max(pools, key=lambda held: held[0])
    attributes = pool["attributes"]
    network = str(pool["id"]).removesuffix(f"_{attributes['address']}")  # pool ids are "<network>_<address>"
    timeframe, aggregate, count = span.gecko
    candles = await lookups.json(
        f"{GECKOTERMINAL}/networks/{network}/pools/{attributes['address']}/ohlcv/{timeframe}",
        params={"aggregate": aggregate, "limit": count, "currency": "usd", "token": side},
        ttl=CANDLES,
    )
    series = sorted((float(row[0]), float(row[4])) for row in candles["data"]["attributes"]["ohlcv_list"])
    venue = ((pool["relationships"].get("dex") or {}).get("data") or {}).get("id")
    symbol = name.strip().lstrip("$").upper()
    quoted = Quote(
        name=symbol,
        symbol=symbol,
        source=f"GeckoTerminal, {network}" + (f" {venue}" if venue else ""),
        currency="USD",
        price=float(attributes[f"{side}_token_price_usd"]),
        series=series,
        year_steps=365.25 * 86400 / _step(series) if len(series) > 2 else None,
    )
    quoted.facts.append(f"liquidity {_big(sum(held[0] for held in pools))} USD in {len(pools)} pools")
    if side == "base":  # the pool's volume and valuation are its base token's
        if volume := _float((attributes.get("volume_usd") or {}).get("h24")):
            quoted.facts.append(f"24h volume {_big(volume)} USD in its deepest pool")
        if cap := _float(attributes.get("market_cap_usd")) or _float(attributes.get("fdv_usd")):
            quoted.facts.append(f"market cap {_big(cap)} USD")
    if quote in (None, "USD", "USDT", "USDC"):
        return quoted
    rate = await fx_rate(lookups, "USD", quote)
    return quoted.priced_in(rate, quote) if rate else None


async def _chart(
    lookups: Lookups, symbol: str, span: Span
) -> tuple[dict[str, Any], list[tuple[float, float]]] | None:
    """Yahoo's chart of a symbol over the span: its meta and closes, None for a symbol it doesn't know."""
    data = await _maybe(
        lookups,
        f"{YAHOO}/v8/finance/chart/{symbol}",
        {"range": span.yahoo[0], "interval": span.yahoo[1]},
        ttl=CANDLES,
    )
    results = ((data or {}).get("chart") or {}).get("result") or []
    if not results or _float(results[0]["meta"].get("regularMarketPrice")) is None:
        return None
    result = results[0]
    closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    series = [
        (float(at), float(c))
        for at, c in zip(result.get("timestamp") or [], closes, strict=False)
        if c is not None
    ]
    return result["meta"], series


async def yahoo(lookups: Lookups, name: str, quote: str | None, span: Span) -> Quote | None:
    """A stock, fund, index, future or currency pair: its symbol as given, else the first match of the name."""
    symbol = ALIASES.get(name.lower(), name)
    chart = await _chart(lookups, symbol, span) if TICKER.fullmatch(symbol) else None
    if chart is None:
        search = await lookups.json(
            f"{YAHOO}/v1/finance/search", params={"q": name, "quotesCount": 5, "newsCount": 0}, ttl=NAMES
        )
        kinds = {"EQUITY", "ETF", "INDEX", "FUTURE", "CURRENCY", "MUTUALFUND"}  # coins: Binance, CoinGecko
        hit = next(
            (q for q in search.get("quotes") or [] if q.get("quoteType") in kinds and q.get("symbol")), None
        )
        if hit is None or (chart := await _chart(lookups, str(hit["symbol"]), span)) is None:
            return None
    meta, series = chart
    traded = str(meta.get("instrumentType") or "") != "CURRENCY"
    exchange = str(meta.get("fullExchangeName") or meta.get("exchangeName") or "")
    found = Quote(
        name=str(meta.get("longName") or meta.get("shortName") or meta["symbol"]),
        symbol=str(meta["symbol"]),
        source="Yahoo Finance"
        + (f", {exchange}" if exchange and traded else "")
        + (", may lag" if traded else ""),
        currency=str(meta.get("currency") or "USD"),
        price=float(meta["regularMarketPrice"]),
        series=series,
        previous=_float(meta.get("chartPreviousClose")) if span is SPANS["1d"] else None,
        year_steps={"1d": 252, "1wk": 52}.get(span.yahoo[1]),
    )
    if (at := _float(meta.get("regularMarketTime"))) and datetime.now(UTC) - datetime.fromtimestamp(
        at, UTC
    ) > timedelta(hours=1):
        found.facts.append(
            f"last trade {datetime.fromtimestamp(at, UTC):%Y-%m-%d %H:%M} UTC, market closed now"
        )
    if quote is None or quote == found.currency:
        return found
    rate = await fx_rate(lookups, found.currency, quote)
    return found.priced_in(rate, quote) if rate else None


async def moex(lookups: Lookups, name: str, quote: str | None, span: Span) -> Quote | None:
    """A Moscow Exchange share, fund or index, by ticker or name (Cyrillic or Latin)."""
    columns = "secid,shortname,name,primary_boardid,group,is_traded"
    params = {
        "q": ALIASES.get(name.lower(), name),
        "limit": 20,
        "iss.meta": "off",
        "securities.columns": columns,
    }
    found = await lookups.json(f"{MOEX}/securities.json", params=params, ttl=NAMES)
    listed = [
        r for r in _rows(found["securities"]) if r["is_traded"] == 1 and r["primary_boardid"] in MOEX_BOARDS
    ]
    if not listed:
        return None
    security = next((r for r in listed if str(r["secid"]).upper() == name.upper()), listed[0])
    secid, board = str(security["secid"]), str(security["primary_boardid"])
    market = MOEX_BOARDS[board]
    path = f"{MOEX}/engines/stock/markets/{market}"
    data = await lookups.json(
        f"{path}{f'/boards/{board}' if market == 'shares' else ''}/securities/{secid}.json",
        params={"iss.meta": "off", "iss.only": "marketdata,securities"},
        ttl=LIVE,
    )
    trading = (_rows(data["marketdata"]) or [{}])[0]
    info = (_rows(data["securities"]) or [{}])[0]
    keys = ("LAST", "CURRENTVALUE", "LCURRENTPRICE", "MARKETPRICE", "LASTVALUE", "PREVPRICE")
    price = next((p for key in keys if (p := _float(trading.get(key, info.get(key)))) is not None), None)
    if price is None:
        return None
    interval, days = span.moex
    since = (datetime.now(MSK) - timedelta(days=days)).date().isoformat()
    candles = await lookups.json(
        f"{path}/securities/{secid}/candles.json",
        params={"from": since, "interval": interval, "iss.meta": "off"},
        ttl=CANDLES,
    )
    series = [
        (datetime.fromisoformat(row["end"]).replace(tzinfo=MSK).timestamp(), float(row["close"]))
        for row in _rows(candles["candles"])
    ]
    moved = _float(trading.get("LASTTOPREVPRICE", trading.get("LASTCHANGEPRC")))
    shares = market == "shares"
    quoted = Quote(
        name=str(info.get("SECNAME") or info.get("NAME") or security["name"]),
        symbol=secid,
        source="Moscow Exchange, may lag 15 min",
        currency="RUB" if shares else "points",
        price=price,
        series=series,
        previous=price / (1 + moved / 100) if moved is not None and span is SPANS["1d"] else None,
        year_steps={24: 252, 7: 52}.get(interval),
    )
    if turnover := _float(trading.get("VALTODAY")):
        quoted.facts.append(f"turnover today {_big(turnover)} RUB")
    if not shares or quote in (None, "RUB", "RUR"):
        return quoted
    rate = await fx_rate(lookups, "RUB", quote)
    return quoted.priced_in(rate, quote) if rate else None


async def fx_rate(lookups: Lookups, base: str, quote: str) -> float | None:
    """One ``base`` in ``quote``: Yahoo's live pair, else open.er-api.com's daily rate."""
    if base == quote:
        return 1.0
    if not (re.fullmatch(r"[A-Z]{3}", base) and re.fullmatch(r"[A-Z]{3}", quote)):
        return None
    if chart := await _chart(lookups, f"{base}{quote}=X", SPANS["1d"]):
        return float(chart[0]["regularMarketPrice"])
    daily = await _maybe(lookups, f"https://open.er-api.com/v6/latest/{base}", ttl=NAMES)
    return _float(((daily or {}).get("rates") or {}).get(quote))


def _open(market: dict[str, Any]) -> bool:
    return bool(market.get("active")) and not market.get("closed") and bool(market.get("outcomePrices"))


def _chances(market: dict[str, Any]) -> list[tuple[str, float]]:
    """A Polymarket market's outcomes and their chances (prices of 0–1)."""
    names, prices = json.loads(market["outcomes"]), json.loads(market["outcomePrices"])
    return [(str(name), float(price)) for name, price in zip(names, prices, strict=True)]


def _yes(market: dict[str, Any]) -> float:
    return _chances(market)[0][1]


def chance_text(percent: float) -> str:
    return "<1%" if 0 < percent < 1 else ">99%" if 99 < percent < 100 else f"{percent:.0f}%"


def _odds(event: dict[str, Any], top: int = 8) -> str:
    """An event's open outcomes with their chances, likeliest first."""
    markets = sorted((m for m in event["markets"] if _open(m)), key=_yes, reverse=True)
    if len(markets) == 1:
        return ", ".join(f"{outcome} {chance_text(p * 100)}" for outcome, p in _chances(markets[0]))
    odds = "; ".join(
        f"{m.get('groupItemTitle') or m.get('question')} {chance_text(_yes(m) * 100)}" for m in markets[:top]
    )
    return odds + (f" (+{len(markets) - top} more)" if len(markets) > top else "")


async def polymarket(lookups: Lookups, name: str, quote: str | None, span: Span) -> Quote | None:
    """Polymarket's odds on an open event about ``name`` (of the search's matches, the one with the most money
    on it; "popular": the events with the most bets in the last day), with the history of its likeliest
    outcomes: ``series`` the leader's, ``lines`` up to four (in percent), skipping near-certain ones, which make
    flat lines."""
    if popular := name.lower() in POPULAR:
        params = {
            "active": "true",
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
            "limit": 6,
        }
        events = await lookups.json(f"{POLYMARKET}/events", params=params, ttl=LIVE, max_bytes=EVENTS_BYTES)
    else:
        params = {"q": name, "limit_per_type": 6, "events_status": "active"}
        found = await lookups.json(
            f"{POLYMARKET}/public-search", params=params, ttl=LIVE, max_bytes=EVENTS_BYTES
        )
        events = found.get("events") or []
    events = [e for e in events if any(_open(m) for m in e.get("markets") or [])]
    if not events:
        return None
    if not popular:  # relevance alone picks side bets ("dissents at the Fed meeting" over its decision)
        events.sort(key=lambda e: _float(e.get("volume")) or 0, reverse=True)
    event = events[0]
    markets = sorted((m for m in event["markets"] if _open(m)), key=_yes, reverse=True)
    charted = ([m for m in markets if _yes(m) < 0.99] or markets)[:4]
    interval, fidelity = span.polymarket

    async def history(market: dict[str, Any]) -> list[tuple[float, float]]:
        """The outcome's chance over the span, ending at its chance now (the odds the text gives)."""
        params = {"market": json.loads(market["clobTokenIds"])[0], "interval": interval, "fidelity": fidelity}
        found = await lookups.json(f"{CLOB}/prices-history", params=params, ttl=CANDLES)
        points = [(float(point["t"]), float(point["p"]) * 100) for point in found.get("history") or []]
        return [*points, (time.time(), _yes(market) * 100)]

    def label(market: dict[str, Any]) -> str:
        return (
            str(market.get("groupItemTitle") or market.get("question"))
            if len(markets) > 1
            else _chances(market)[0][0]
        )

    lines = [
        (label(m), series)
        for m, series in zip(charted, await asyncio.gather(*map(history, charted)), strict=True)
    ]
    found = Quote(
        name=lines[0][0],
        symbol=str(event.get("title") or charted[0].get("question")),
        source="Polymarket",
        currency="%",
        price=_yes(charted[0]) * 100,
        series=lines[0][1],
        lines=lines if len(lines) > 1 else [],
        link=f"https://polymarket.com/event/{event['slug']}" if event.get("slug") else None,
    )
    if popular:
        found.facts.append(
            "The most bet-on events in the last day: "
            + " | ".join(
                f"{e.get('title')}: {_odds(e, top=3)} (24h volume {_big(_float(e.get('volume24hr')) or 0)} USD)"
                for e in events
            )
        )
    else:
        found.facts.append(f"Odds: {_odds(event)}")
    money = f"volume {_big(volume)} USD" if (volume := _float(event.get("volume"))) else ""
    ends = f"resolves by {str(event['endDate'])[:10]}" if event.get("endDate") else ""
    if about := ", ".join(part for part in (money, ends) if part):
        found.facts.append(f"The first one: {about}" if popular else about[0].upper() + about[1:])
    if not popular and (others := [str(e.get("title")) for e in events[1:4] if e.get("title")]):
        found.facts.append("Other events found: " + " | ".join(others))
    return found


async def currencies(lookups: Lookups, base: str, quote: str, span: Span) -> Quote | None:
    if chart := await _chart(lookups, f"{base}{quote}=X", span):
        meta, series = chart
        return Quote(
            name=f"{base}/{quote}",
            symbol=f"{base}/{quote}",
            source="Yahoo Finance",
            currency=quote,
            price=float(meta["regularMarketPrice"]),
            series=series,
            previous=_float(meta.get("chartPreviousClose")) if span is SPANS["1d"] else None,
            year_steps={"1d": 252, "1wk": 52}.get(span.yahoo[1]),
        )
    rate = await fx_rate(lookups, base, quote)
    return (
        Quote(f"{base}/{quote}", f"{base}/{quote}", "open.er-api.com, daily", quote, rate) if rate else None
    )


def _moex_like(name: str) -> bool:
    """A Moscow Exchange ticker (SBER, GAZP, SBERP) or a Russian name."""
    return bool(re.fullmatch(r"[A-Z]{4}P?", name)) or bool(re.search("[а-яё]", name.lower()))


async def find(
    lookups: Lookups, base: str, quote: str | None = None, period: Period = "1d", kind: Kind = None
) -> Quote | None:
    """The quote for ``base`` priced in ``quote`` (else its own currency); None when no source knows it.

    A currency is priced as one whatever ``kind`` says: models fill the hint even where it can't apply."""
    base = " ".join(base.split())
    quote = " ".join((quote or "").split()).upper() or None
    span = SPANS[period]
    upper = base.upper()
    fiat = upper in FIAT and kind != "prediction"
    try:
        if fiat and (quote is None or quote in FIAT):
            return await currencies(lookups, upper, quote or ("EUR" if upper == "USD" else "USD"), span)
        if fiat:  # dollars in bitcoin: bitcoin in dollars, turned
            other = await find(lookups, quote or "", upper, period, kind)
            return other.inverted(upper) if other else None
        if kind in (None, "stock") and (alias := ALIASES.get(base.lower())):
            return await (moex if alias == "IMOEX" else yahoo)(lookups, alias, quote, span)
        sources: dict[str | None, tuple[Source, ...]] = {
            None: (binance, moex, yahoo, coingecko, dex),
            "crypto": (binance, coingecko),
            "dex": (dex,),
            "stock": (moex, yahoo),
            "prediction": (polymarket,),
        }
        for source in sources[kind]:
            if source is moex and kind is None and not _moex_like(base):
                continue
            if (found := await source(lookups, base, quote, span)) is not None:
                return replace(
                    found,
                    series=_window(found.series, span.seconds),
                    lines=[(label, _window(points, span.seconds)) for label, points in found.lines],
                )
    except KeyError, IndexError, TypeError, ValueError, AttributeError, ZeroDivisionError:
        raise LookupError("unexpected response") from None
    return None


# Numbers and text


def _window(series: list[tuple[float, float]], seconds: int) -> list[tuple[float, float]]:
    """The period's points up to the latest one, after the one point before them (what the move is measured from).

    Sparse sources (a quiet DEX pool) hand back candles reaching further back than the period; counting from
    the latest point keeps a closed market's last session whole."""
    if not series:
        return series
    start = series[-1][0] - seconds
    before = [point for point in series if point[0] < start]
    return before[-1:] + [point for point in series if point[0] >= start]


def _seconds(interval: str) -> float:
    return float(interval[:-1]) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[interval[-1]]


def _step(series: list[tuple[float, float]]) -> float:
    return statistics.median(b[0] - a[0] for a, b in itertools.pairwise(series)) or 1.0


def rsi(closes: list[float], length: int = 14) -> float | None:
    """Wilder's relative strength index at the last close."""
    if len(closes) <= length:
        return None
    moves = [b - a for a, b in itertools.pairwise(closes)]
    gain = sum(max(m, 0) for m in moves[:length]) / length
    loss = sum(max(-m, 0) for m in moves[:length]) / length
    for move in moves[length:]:
        gain = (gain * (length - 1) + max(move, 0)) / length
        loss = (loss * (length - 1) + max(-move, 0)) / length
    return 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)


def volatility(closes: list[float], year_steps: float) -> float | None:
    """Annualized volatility of the series' log returns."""
    returns = [math.log(b / a) for a, b in itertools.pairwise(closes) if a > 0 and b > 0]
    return statistics.stdev(returns) * math.sqrt(year_steps) if len(returns) >= 10 else None


def price_text(value: float) -> str:
    """A price as people write it: no exponent, enough digits for a memecoin."""
    size = abs(value)
    if size >= 10_000:
        return f"{value:,.0f}"
    if size >= 10:
        return f"{value:,.2f}"
    if size >= 1:  # currency pairs: four decimals, without trailing zeros past two
        return f"{value:.4f}".rstrip("0").ljust(len(f"{value:.2f}"), "0")
    if size == 0:
        return "0"
    return f"{value:.{min(12, 3 - math.floor(math.log10(size)))}f}"


def _big(value: float) -> str:
    for size, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= size:
            return f"{value / size:.1f}{suffix}"
    return f"{value:.0f}"


def change(found: Quote) -> tuple[float, str] | None:
    """The move over the period, signed and as text: in percent from the last close for a day, else from the
    series' start; a chance (percent already) moves in points."""
    start = found.previous or (found.series[0][1] if found.series else None)
    if start is None:
        return None
    if found.currency == "%":
        return found.price - start, f"{found.price - start:+.1f} pts"
    if not start:
        return None
    moved = (found.price / start - 1) * 100
    return moved, f"{moved:+.1f}%"


def describe(found: Quote, period: Period, amount: float = 1) -> str:
    """The quote as text for the model: the price, the period's move and range, stats, then facts."""
    named = (
        f"{found.name} ({found.symbol}, {found.source})"
        if found.name != found.symbol
        else f"{found.symbol} ({found.source})"
    )
    if found.currency == "%":  # a prediction market: the odds, the leading outcome's move, then the rest
        parts = [f"{found.symbol} ({found.source})", *found.facts]
        closes = [close for _, close in found.series]
        if (moved := change(found)) is not None:
            swing = f", range {min(closes):.0f}–{max(closes):.0f}%" if closes else ""
            parts.insert(2, f"{found.name}: {moved[1]} over the past {SPANS[period].label}{swing}")
        return ". ".join(parts) + "." + (f" {found.link}" if found.link else "")
    lines = [f"{named}: {price_text(found.price)} {found.currency}."]
    if amount != 1:
        lines.insert(
            0,
            f"{amount:g} {found.symbol.split('/')[0]} = {price_text(amount * found.price)} {found.currency}.",
        )
    closes = [close for _, close in found.series]
    moves = []
    if (moved := change(found)) is not None:
        moves.append(moved[1])
    if closes:
        moves.append(f"range {price_text(min(*closes, found.price))}–{price_text(max(*closes, found.price))}")
    stats = []
    if (strength := rsi(closes)) is not None:
        stats.append(f"RSI(14) {strength:.0f}")
    if found.year_steps and (swing := volatility(closes, found.year_steps)) is not None:
        stats.append(f"volatility {swing * 100:.0f}%/yr")
    daily = len(closes) > 2 and abs(_step(found.series) - 86400) < 6 * 3600
    for length in (50, 200):
        if daily and len(closes) >= length:
            stats.append(
                f"{(found.price / statistics.fmean(closes[-length:]) - 1) * 100:+.1f}% vs {length}-day average"
            )
    if moves:
        lines.append(f"Past {SPANS[period].label}: {', '.join(moves)}.")
    if stats:
        lines.append(f"Stats: {', '.join(stats)}.")
    if found.facts:
        facts = "; ".join(found.facts)
        lines.append(facts[0].upper() + facts[1:] + ".")
    return " ".join(lines)
