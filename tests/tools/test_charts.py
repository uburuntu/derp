import io
from zoneinfo import ZoneInfo

from PIL import Image

from derp.tools import charts
from derp.tools.markets import Quote

MOSCOW = ZoneInfo("Europe/Moscow")


def opened(png: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(png))
    assert (image.format, image.size) == ("PNG", (charts.WIDTH, charts.HEIGHT))
    return image.convert("RGB")


def test_a_price_chart_is_green_up_and_red_down() -> None:
    series = [(1_762_300_000 + i * 900.0, 100 + i * 0.5) for i in range(96)]
    up = Quote("Bitcoin", "BTC/USDT", "Binance", "USDT", series[-1][1], series=series)
    down = Quote(
        "Bitcoin", "BTC/USDT", "Binance", "USDT", 90.0, series=[*series, (series[-1][0] + 900, 90.0)]
    )

    def greens_and_reds(found: Quote) -> tuple[int, int]:
        pixels = opened(charts.render(found, "day", MOSCOW)).get_flattened_data()
        return sum(p == charts.UP for p in pixels), sum(p == charts.DOWN for p in pixels)

    green, red = greens_and_reds(up)
    assert green > 200 and red == 0
    green, red = greens_and_reds(down)
    assert red > 200 and green == 0


def test_odds_get_a_line_for_each_likely_outcome() -> None:
    hours = [1_762_300_000 + i * 3600.0 for i in range(48)]
    lines = [  # Noto Sans has no arrows: the spare font draws them
        ("↓ 75,000", [(at, 50 + i / 2) for i, at in enumerate(hours)]),
        ("↑ 82,500 with a very long name that will not fit the legend", [(at, 40.0) for at in hours]),
        ("Paris Saint-Germain", [(at, 0.4) for at in hours]),
    ]
    found = Quote("↓ 75,000", "What price will Bitcoin hit?", "Polymarket", "%", 73.5, series=lines[0][1])
    found.lines = lines
    pixels = set(opened(charts.render(found, "week", MOSCOW)).get_flattened_data())
    assert set(charts.PALETTE[:3]) <= pixels and charts.PALETTE[3] not in pixels


def test_levels_are_round_and_about_four() -> None:
    assert charts._ticks(17, 99) == [20, 40, 60, 80]
    assert charts._ticks(280, 298) == [280, 285, 290, 295]
    assert charts._ticks(5, 5) == [5]


def test_labels_move_apart_only_as_far_as_they_must() -> None:
    assert charts._spread([100, 105, 300], 30) == [100, 130, 300]
