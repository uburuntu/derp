import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import TypeAdapter, ValidationError

from derp.config import Settings
from derp.tools import markets
from derp.tools.lookups import LookupError, Lookups  # noqa: A004 - the lookups' own error
from derp.tools.markets import Quote, change, describe, find, price_text, rsi, volatility

NOW = time.time()
Routes = dict[str, Any | Callable[[httpx.Request], Any]]  # "host/path": a JSON body, or one made per request


def lookups(settings: Settings, routes: Routes, seen: list[httpx.Request] | None = None) -> Lookups:
    """Lookups answering from ``routes``; a missing route is a 404 (the source doesn't know the name)."""

    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        body = routes.get(request.url.host + request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, json=body(request) if callable(body) else body)

    return Lookups(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))


def binance(closes: list[float], *, price: float, opened: float) -> Routes:
    klines = [[(NOW - (len(closes) - i) * 900) * 1000, 0, 0, 0, str(c)] for i, c in enumerate(closes)]
    return {
        "api.binance.com/api/v3/ticker/24hr": {
            "lastPrice": str(price), "openPrice": str(opened), "quoteVolume": "1800000000",
        },
        "api.binance.com/api/v3/klines": klines,
        "fapi.binance.com/fapi/v1/premiumIndex": {"lastFundingRate": "0.0001", "markPrice": "76900"},
        "fapi.binance.com/fapi/v1/openInterest": {"openInterest": "93700"},
    }  # fmt: skip


def yahoo_chart(symbol: str, price: float, closes: list[float], **meta: Any) -> dict[str, Any]:
    stamps = [NOW - (len(closes) - i) * 300 for i in range(len(closes))]
    return {
        "chart": {
            "result": [
                {
                    "meta": {"symbol": symbol, "regularMarketPrice": price, "regularMarketTime": NOW, **meta},
                    "timestamp": stamps,
                    "indicators": {"quote": [{"close": closes}]},
                }
            ]
        }
    }


async def test_a_coin_comes_from_binance_with_its_perpetual(settings: Settings) -> None:
    found = await find(lookups(settings, binance([80000, 75000, 76881], price=76881, opened=80000)), "btc")
    assert found is not None
    assert describe(found, "1d") == (
        "BTC (BTC/USDT, Binance): 76,881 USDT. Past day: -3.9%, range 75,000–80,000. 24h volume 1.8B USDT; "
        "perpetual funding +0.0100% per 8h; open interest 93.7K BTC (7.2B USDT)."
    )


async def test_a_long_series_gets_its_stats(settings: Settings) -> None:
    closes = [100 + (i % 5) - i * 0.5 for i in range(60)]
    found = await find(lookups(settings, binance(closes, price=closes[-1], opened=closes[0])), "ETH")
    assert found is not None
    text = describe(found, "1d")
    assert "RSI(14) " in text and "volatility " in text


async def test_a_coin_in_a_currency_binance_does_not_pair_goes_through_the_dollar(settings: Settings) -> None:
    routes = binance([80000, 76881], price=76881, opened=80000)
    routes["query1.finance.yahoo.com/v8/finance/chart/USDRUB=X"] = yahoo_chart("USDRUB=X", 81.5, [81.5])
    found = await find(lookups(settings, routes), "BTC", "rub")
    assert found is not None and (found.currency, found.symbol) == ("RUB", "BTC/USDT")
    assert found.price == pytest.approx(76881 * 81.5)
    assert "converted at 81.50 RUB per USDT" in describe(found, "1d")


async def test_currencies_and_conversions(settings: Settings) -> None:
    routes: Routes = {
        "query1.finance.yahoo.com/v8/finance/chart/USDRUB=X": yahoo_chart(
            "USDRUB=X", 81.5, [80.0, 81.5], chartPreviousClose=80.0, instrumentType="CURRENCY"
        )
    }
    found = await find(lookups(settings, routes), "usd", "RUB")
    assert found is not None
    assert describe(found, "1d", 100) == (
        "100 USD = 8,150.00 RUB. USD/RUB (Yahoo Finance): 81.50 RUB. Past day: +1.9%, range 80.00–81.50."
    )


async def test_a_currency_is_priced_as_one_whatever_kind_the_model_guessed(settings: Settings) -> None:
    routes: Routes = {
        "query1.finance.yahoo.com/v8/finance/chart/USDRUB=X": yahoo_chart("USDRUB=X", 81.5, [81.5])
    }
    for kind in ("crypto", "stock", None):  # not Tether, nor the ETF whose ticker is USD
        found = await find(lookups(settings, routes), "USD", "RUB", kind=kind)
        assert found is not None and (found.symbol, found.price) == ("USD/RUB", 81.5)


def test_periods_and_kinds_as_models_write_them() -> None:
    period, kind = TypeAdapter(markets.Period), TypeAdapter(markets.Kind)
    written = ["1w", "7d", "30 days", "24h", "week", "max", "2y", "6mo"]
    assert [period.validate_python(p) for p in written] == ["1w", "1w", "1m", "1d", "1w", "5y", "1y", "3m"]
    with pytest.raises(ValidationError):
        period.validate_python("forever")
    assert [kind.validate_python(k) for k in ("dex", "Polymarket", "currency", None)] == [
        "dex",
        "prediction",
        None,
        None,
    ]


async def test_dollars_in_bitcoin_are_bitcoin_turned_over(settings: Settings) -> None:
    found = await find(lookups(settings, binance([80000, 76881], price=76881, opened=80000)), "USD", "BTC")
    assert found is not None and (found.symbol, found.currency) == ("USD/BTC", "BTC")
    assert price_text(found.price) == "0.00001301"


async def test_a_russian_name_finds_the_moscow_exchange_share(settings: Settings) -> None:
    seen: list[httpx.Request] = []
    columns = ["secid", "shortname", "name", "primary_boardid", "group", "is_traded"]
    routes: Routes = {
        "iss.moex.com/iss/securities.json": {
            "securities": {
                "columns": columns,
                "data": [
                    ["SBER", "Сбербанк", "Сбербанк России ПАО ао", "TQBR", "stock_shares", 1],
                    ["SBERP", "Сбербанк-п", "Сбербанк ап", "TQBR", "stock_shares", 1],
                ],
            }
        },
        "iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/SBER.json": {
            "marketdata": {"columns": ["LAST", "LASTTOPREVPRICE", "VALTODAY"], "data": [[290.0, 1.0, 5.4e9]]},
            "securities": {"columns": ["SECNAME"], "data": [["Сбербанк России ПАО ао"]]},
        },
        "iss.moex.com/iss/engines/stock/markets/shares/securities/SBER/candles.json": {
            "candles": {"columns": ["close", "end"], "data": [[287.0, "2026-11-05 10:59:59"]]}
        },
    }
    found = await find(lookups(settings, routes, seen), "сбер")
    assert found is not None
    assert describe(found, "1d").startswith(
        "Сбербанк России ПАО ао (SBER, Moscow Exchange, may lag 15 min): 290.00 RUB. Past day: +1.0%"
    )
    assert "Turnover today 5.4B RUB." in describe(found, "1d")
    assert not any(r.url.host == "api.binance.com" for r in seen)  # a Cyrillic name is no Binance symbol


async def test_a_name_no_exchange_lists_falls_through_to_yahoo_search(settings: Settings) -> None:
    routes: Routes = {
        "query1.finance.yahoo.com/v1/finance/search": {
            "quotes": [
                {"symbol": "TSLA-USD", "quoteType": "CRYPTOCURRENCY"},  # coins are Binance's and CoinGecko's
                {"symbol": "TSLA", "quoteType": "EQUITY"},
            ]
        },
        "query1.finance.yahoo.com/v8/finance/chart/TSLA": yahoo_chart(
            "TSLA", 450.0, [440.0, 450.0], longName="Tesla, Inc.", currency="USD", fullExchangeName="NasdaqGS"
        ),
    }
    found = await find(lookups(settings, routes), "tesla")
    assert found is not None
    assert describe(found, "1d").startswith(
        "Tesla, Inc. (TSLA, Yahoo Finance, NasdaqGS, may lag): 450.00 USD. Past day: +2.3%"
    )


async def test_a_dex_token_is_the_one_with_the_most_liquidity(settings: Settings) -> None:
    def pool(
        network: str, address: str, name: str, liquidity: float, base: str, quote: str
    ) -> dict[str, Any]:
        return {
            "id": f"{network}_{address}",
            "attributes": {
                "name": name, "address": address, "reserve_in_usd": str(liquidity),
                "base_token_price_usd": "0.0002", "quote_token_price_usd": "1.9",
                "volume_usd": {"h24": "350000"}, "fdv_usd": "103000000",
            },
            "relationships": {
                "base_token": {"data": {"id": base}}, "quote_token": {"data": {"id": quote}},
                "dex": {"data": {"id": "stonfi"}},
            },
        }  # fmt: skip

    asked: list[httpx.Request] = []
    routes: Routes = {
        "api.geckoterminal.com/api/v2/search/pools": {
            "data": [
                pool("bsc", "0xa", "DOGS / WBNB", 900_000, "bsc_dogs", "bsc_wbnb"),
                pool("ton", "EQb", "DOGS / TON", 700_000, "ton_dogs", "ton_ton"),
                pool("ton", "EQc", "USDT / DOGS 0.3%", 400_000, "ton_usdt", "ton_dogs"),
            ]
        },
        "api.geckoterminal.com/api/v2/networks/ton/pools/EQb/ohlcv/minute": lambda request: (
            asked.append(request),
            {"data": {"attributes": {"ohlcv_list": [[NOW, 0, 0, 0, 0.0002], [NOW - 900, 0, 0, 0, 0.0001]]}}},
        )[1],
    }
    found = await find(lookups(settings, routes), "$DOGS", kind="dex")
    assert found is not None
    assert (found.source, found.price) == ("GeckoTerminal, ton stonfi", 0.0002)
    assert asked[0].url.params["token"] == "base"
    text = describe(found, "1d")
    assert text.startswith("DOGS (GeckoTerminal, ton stonfi): 0.0002000 USD. Past day: +100.0%")
    assert (
        "Liquidity 1.1M USD in 2 pools; 24h volume 350.0K USD in its deepest pool; market cap 103.0M USD"
        in text
    )


def poly_market(title: str, yes: float, token: str) -> dict[str, Any]:
    return {
        "groupItemTitle": title,
        "question": f"{title}?",
        "active": True,
        "closed": False,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": json.dumps([str(yes), str(round(1 - yes, 3))]),
        "clobTokenIds": json.dumps([token, token + "0"]),
    }


FED = {
    "title": "Fed Decision in December?",
    "slug": "fed-decision-in-december",
    "volume": 14_323_818,
    "volume24hr": 1_752_254,
    "endDate": "2026-12-10T00:00:00Z",
    "markets": [
        poly_market("No change", 0.28, "1"),
        poly_market("25 bps increase", 0.7, "2"),
        poly_market("50+ bps increase", 0.004, "3"),
        {**poly_market("75 bps increase", 0.0, "4"), "closed": True},
    ],
}
DISSENT = {
    "title": "How many dissent at the December Fed meeting?",
    "slug": "dissent",
    "volume": 47_615,
    "volume24hr": 61,
    "markets": [poly_market("2", 0.4, "5")],
}


def clob(request: httpx.Request) -> dict[str, Any]:
    start = {"1": 0.3, "2": 0.65, "3": 0.01}[request.url.params["market"]]
    return {"history": [{"t": NOW - 7200, "p": start}, {"t": NOW - 3600, "p": start + 0.03}]}


async def test_bets_on_an_event_pick_the_one_with_the_most_money(settings: Settings) -> None:
    routes: Routes = {
        "gamma-api.polymarket.com/public-search": {"events": [DISSENT, FED]},  # the side bet ranks first
        "clob.polymarket.com/prices-history": clob,
    }
    found = await find(lookups(settings, routes), "fed december", kind="prediction")
    assert found is not None
    assert describe(found, "1d") == (
        "Fed Decision in December? (Polymarket). Odds: 25 bps increase 70%; No change 28%; 50+ bps increase <1%. "
        "25 bps increase: +5.0 pts over the past day, range 65–70%. Volume 14.3M USD, resolves by 2026-12-10. "
        "Other events found: How many dissent at the December Fed meeting?. "
        "https://polymarket.com/event/fed-decision-in-december"
    )
    assert [label for label, _ in found.lines] == ["25 bps increase", "No change", "50+ bps increase"]
    assert [points[-1][1] for _, points in found.lines] == pytest.approx([70, 28, 0.4])  # the odds now


async def test_popular_bets_are_the_busiest_events(settings: Settings) -> None:
    seen: list[httpx.Request] = []
    routes: Routes = {
        "gamma-api.polymarket.com/events": [FED, DISSENT],
        "clob.polymarket.com/prices-history": clob,
    }
    found = await find(lookups(settings, routes, seen), "Popular", "USD", "1w", kind="prediction")
    assert found is not None and found.symbol == "Fed Decision in December?"
    assert seen[0].url.params["order"] == "volume24hr"
    assert describe(found, "1w").startswith(
        "Fed Decision in December? (Polymarket). The most bet-on events in the last day: Fed Decision in "
        "December?: 25 bps increase 70%; No change 28%; 50+ bps increase <1% (24h volume 1.8M USD) | How many "
        "dissent at the December Fed meeting?: Yes 40%, No 60% (24h volume 61 USD). 25 bps increase: +5.0 pts"
    )


async def test_nothing_open_to_bet_on_is_no_answer(settings: Settings) -> None:
    closed = {**DISSENT, "markets": [{**poly_market("2", 0.4, "5"), "closed": True}]}
    routes: Routes = {"gamma-api.polymarket.com/public-search": {"events": [closed]}}
    assert await find(lookups(settings, routes), "dissent", kind="prediction") is None


async def test_an_unknown_name_is_no_answer(settings: Settings) -> None:
    routes: Routes = {
        "query1.finance.yahoo.com/v1/finance/search": {"quotes": []},
        "api.coingecko.com/api/v3/search": {"coins": []},
        "api.geckoterminal.com/api/v2/search/pools": {"data": []},
    }
    assert await find(lookups(settings, routes), "zzqx") is None


async def test_a_broken_answer_or_an_outage_is_a_lookup_error(settings: Settings) -> None:
    broken: Routes = {
        "api.binance.com/api/v3/ticker/24hr": {"lastPrice": "n/a"},
        "api.binance.com/api/v3/klines": [],
    }
    with pytest.raises(LookupError, match="unexpected response"):
        await find(lookups(settings, broken), "BTC")
    down = Lookups(
        settings, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(503)))
    )
    with pytest.raises(LookupError, match="HTTP 503"):
        await find(down, "BTC")


def test_numbers_read_like_prices() -> None:
    assert [price_text(v) for v in (76881.4, 293.49, 81.5, 1.08423, 1.5, 0.0001234, 0)] == [
        "76,881", "293.49", "81.50", "1.0842", "1.50", "0.0001234", "0",
    ]  # fmt: skip


def test_strength_and_swings() -> None:
    assert rsi([float(i) for i in range(20)]) == 100
    assert rsi([1.0, 2.0, 3.0]) is None
    assert rsi([10.0 + (i % 2) for i in range(30)]) == pytest.approx(50, abs=5)
    assert volatility([100.0] * 5, 252) is None
    assert volatility([100.0 * 1.01**i for i in range(20)], 252) == pytest.approx(0, abs=1e-9)


def test_a_period_keeps_the_point_before_it() -> None:
    series = [(0.0, 1.0), (50.0, 2.0), (100.0, 3.0), (150.0, 4.0)]
    assert markets._window(series, 60) == [(50.0, 2.0), (100.0, 3.0), (150.0, 4.0)]
    assert markets._window([], 60) == []


def test_a_chance_moves_in_points() -> None:
    odds = Quote("Yes", "Event", "Polymarket", "%", 70.0, series=[(0.0, 60.0), (1.0, 70.0)])
    assert change(odds) == (10.0, "+10.0 pts")
    moved = change(Quote("BTC", "BTC/USDT", "Binance", "USDT", 110.0, series=[(0.0, 100.0)]))
    assert moved is not None and moved[0] == pytest.approx(10) and moved[1] == "+10.0%"
