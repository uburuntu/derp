"""Eval scenarios: one ask each in a fresh copy of the synthetic group, and a check of what Derp did.

A check reads the run's tool calls, tool results and final text (``Result``). Checks name the current
tools and arguments: a change to the tool surface updates them in the same change, so an A/B between two
commits runs each side's own scenarios. Keep checks about the decision that matters (the right tool,
the right source, no wasted retry), not the wording, which varies run to run.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import update

from derp.history.models import Chat
from derp.scheduler.models import Job
from tests.e2e.harness import Person
from tools.eval.world import ANYA, BOB, JO, Seeded, World


@dataclass
class Result:
    name: str
    status: str = ""
    tools: list[tuple[str, dict[str, Any]]] = field(default_factory=list[tuple[str, dict[str, Any]]])
    returns: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    retries: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    text: str = ""
    outputs: list[str] = field(default_factory=list[str])  # Bot API methods the run called in the chat
    requests: int = 0
    input_tokens: int = 0
    cache_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0  # the run's chat turns
    side_cost: float = 0.0  # its other provider calls: images, voice, media notes
    seconds: float = 0.0
    model: str = ""
    error: str | None = None
    passed: bool = False

    def called(self, name: str, **want: Any) -> bool:
        """Whether ``name`` was called with arguments matching ``want`` (values, or predicates on them)."""
        return any(
            tool == name and all(_match(args.get(k), v) for k, v in want.items()) for tool, args in self.tools
        )

    def returned(self, name: str, *starts: str) -> bool:
        return any(tool == name and result.startswith(starts) for tool, result in self.returns)

    def says(self, *patterns: str) -> bool:
        return any(re.search(p, self.text, re.IGNORECASE) for p in patterns)


def _match(value: Any, want: Any) -> bool:
    if not callable(want):
        return value == want
    try:
        return bool(want(value))
    except TypeError, KeyError, IndexError, AttributeError:
        return False


type Check = Callable[[Result, Seeded], tuple[bool, str]]
type Setup = Callable[[World, Seeded], Awaitable[None]]


@dataclass(frozen=True)
class Scenario:
    name: str
    text: str
    check: Check
    asker: Person = JO
    reply_to: str | None = None  # a seeded message key the ask replies to
    setup: Setup | None = None
    tags: tuple[str, ...] = ()


def ok(passed: object, why: str) -> tuple[bool, str]:
    return bool(passed), why


def has(*words: str) -> Callable[[Any], bool]:
    """A predicate: the value is text containing every word (case-insensitive)."""
    return lambda v: isinstance(v, str) and all(w.lower() in v.lower() for w in words)


async def moscow(world: World, s: Seeded) -> None:
    async with world.db.transaction() as session:
        await session.execute(update(Chat).where(Chat.id == s.room.id).values(timezone="Europe/Moscow"))


async def film_list(world: World, s: Seeded) -> None:
    await world.note(s.room, "Фильмы посмотреть: Матрица, Интерстеллар, Бойцовский клуб", author=ANYA.id)


async def long_film_list(world: World, s: Seeded) -> None:
    films = (
        "Матрица, Интерстеллар, Бойцовский клуб, Начало, Престиж, Дюна, Бегущий по лезвию 2049, Остров "
        "проклятых, Зелёная миля, Побег из Шоушенка, Криминальное чтиво, Большой куш, Карты, деньги, два ствола, "
        "Однажды в Голливуде, Джанго освобождённый, Бесславные ублюдки, Игры разума, Гладиатор, Тёмный рыцарь, "
        "Отступники, Волк с Уолл-стрит, Остров собак, Отель «Гранд Будапешт», Королевство полной луны, Паразиты, "
        "Олдбой, Ла-Ла Ленд"
    )
    await world.note(s.room, f"Фильмы посмотреть: {films}", author=ANYA.id)


async def reminders(world: World, s: Seeded) -> None:
    """Jo's one-off reminder to call mom and Bob's daily gym reminder, tomorrow."""
    await moscow(world, s)
    when = datetime.now(UTC) + timedelta(days=1)
    async with world.db.transaction() as session:
        session.add_all(
            [
                Job(chat_id=s.room.id, creator_id=JO.id, kind="message", text="позвонить маме", repeat=None,
                    next_at=when, anchor_at=when),
                Job(chat_id=s.room.id, creator_id=BOB.id, kind="message", text="gym", repeat="daily",
                    next_at=when, anchor_at=when),
            ]
        )  # fmt: skip


def _photo_of(key: str) -> Callable[[Result, Seeded], bool]:
    """generate_image started from the seeded photo: by its #id or as "this"."""
    return lambda r, s: r.called(
        "generate_image", images=lambda v: any(str(s.ids[key]) in x or x.strip().lower() == "this" for x in v)
    )


SCENARIOS: list[Scenario] = [
    # history: answers that live past the window
    Scenario("activity_week", "дерп, кто больше всех болтал за последнюю неделю?",
             lambda r, s: ok(r.called("chat_query", preset="member_activity") or r.called("chat_query", group_by="user"),
                             "chat_query by member"), tags=("history",)),
    Scenario("recall_place_en", "derp what was that place near the station Lena recommended for friday?",
             lambda r, s: ok(r.says("хинкал", "khinkal"), "finds Хинкальная"), tags=("history",)),
    Scenario("recall_password", "дерп, какой там у Васи пароль от вайфая на даче был?",
             lambda r, s: ok(r.says("kartoshka2024"), "finds the password"), tags=("history",)),
    Scenario("birthday", "дерп, когда у Ани днюха?",
             lambda r, s: ok(r.says("14 ноя", r"14\.11", "четырнадцат"), "14 November"), tags=("history",)),
    Scenario("user_said", "derp what did Vasya say about the bar plan?",
             lambda r, s: ok(r.says("20:00", "8 ?pm", "хинкал", "khinkal", "friday", "пятниц"), "Vasya's decision"),
             tags=("history",)),
    Scenario("catch_up", "дерп, что я пропустил?",
             lambda r, s: ok(r.called("digest", period="since_my_last_message"), "digest since_my_last_message"),
             tags=("history",)),
    Scenario("yesterday", "derp what happened in here yesterday?",
             lambda r, s: ok(r.called("digest", period="yesterday"), "digest yesterday"), tags=("history",)),
    Scenario("photos_who", "дерп, кто кидал фотки за последние три дня?",
             lambda r, s: ok(r.called("chat_query") and r.says("Bob|Боб", "Вас"), "chat_query photos"),
             tags=("history",)),
    Scenario("day_count", "дерп, сколько сообщений было в чате позавчера?",
             lambda r, s: ok(r.called("chat_query") and not any(t == "chat_query" for t, _ in r.retries),
                             "chat_query, no retry"), tags=("history",)),
    Scenario("inspect_old_photo", "дерп, что там было на фотке Боба с котом?",
             lambda r, s: ok(r.called("inspect_media", source=has(str(s.ids["cat_photo"]))), "inspect_media cat photo"),
             tags=("history", "media")),
    Scenario("avatar_look", "дерп, что у Лены на аватарке?",
             lambda r, s: ok(r.called("inspect_media", source=has("лен")) or r.called("inspect_media", source=has("lena")),
                             "looks at Lena's avatar"), tags=("media",)),
    # memory
    Scenario("remember_fact", "derp remember that Bob is allergic to cats",
             lambda r, s: ok(r.called("remember", text=has("bob")), "remember chat"), tags=("memory",)),
    Scenario("remember_me", "дерп, запомни, я не ем мясо",
             lambda r, s: ok(r.called("remember", about="me"), "remember me"), tags=("memory",)),
    Scenario("list_add", "дерп, добавь Дюну 3 в наш список фильмов",
             lambda r, s: ok(r.called("edit_note", text=has("матриц", "дюн")), "edit_note keeps the list"),
             setup=film_list, tags=("memory",)),
    Scenario("list_add_long", "дерп, добавь «Джентльмены» в наш список фильмов",
             lambda r, s: ok(r.called("edit_note", text=has("джентльм")) and not r.returned("edit_note", "Not changed"),
                             "edit_note on a long list"), setup=long_film_list, tags=("memory",)),
    Scenario("rules", "derp set the chat rules: no politics, swearing is fine",
             lambda r, s: ok(r.called("set_chat_rules"), "set_chat_rules"), tags=("memory",)),
    # scheduler
    Scenario("reminder_relative", "derp remind me in 2 hours to take the pizza out",
             lambda r, s: ok(r.called("schedule", in_minutes=120) or r.called("schedule", at=bool), "schedule +2h"),
             tags=("scheduler",)),
    Scenario("reminder_no_tz", "дерп, напомни мне завтра в 9 утра позвонить маме",
             lambda r, s: ok(not r.returned("schedule", "Scheduled") and r.says("город", "city", "час.*пояс", "зон"),
                             "asks where they are before scheduling"), tags=("scheduler",)),
    Scenario("reminder_tz", "дерп, напомни мне завтра в 9 утра позвонить маме",
             lambda r, s: ok(r.called("schedule", at=has("09:00")), "schedule at 09:00"), setup=moscow,
             tags=("scheduler",)),
    Scenario("cancel_job", "derp cancel my reminder about calling mom",
             lambda r, s: ok(r.returned("edit_job", "Cancelled job"), "edit_job cancel"), setup=reminders,
             tags=("scheduler",)),
    Scenario("pause_job", "дерп, поставь на паузу моё напоминание про маму",
             lambda r, s: ok(r.returned("edit_job", "Paused job"), "edit_job pause"), setup=reminders,
             tags=("scheduler",)),
    Scenario("tz_set", "дерп, мы все в Москве живём, поставь наше время",
             lambda r, s: ok(r.called("change_setting", setting="time_zone"), "change_setting time_zone"),
             tags=("settings",)),
    # games and Telegram outputs
    Scenario("quiz", "дерп, устрой квиз из 3 вопросов про наш чат",
             lambda r, s: ok(r.returned("post_polls", "Posted 3 of 3"), "three quizzes posted"), tags=("games",)),
    Scenario("quiz5", "дерп, квиз из 5 вопросов про наш чат, погнали",
             lambda r, s: ok(r.returned("post_polls", "Posted 5 of 5"), "five quizzes posted"), tags=("games",)),
    Scenario("poll", "дерп, сделай опрос: куда идём в пятницу — бар или кино",
             lambda r, s: ok(r.returned("post_polls", "Posted 1 of 1"), "a poll"), tags=("games",)),
    Scenario("most_likely", "derp make a poll: who's most likely to be late on friday?",
             lambda r, s: ok(r.called("post_polls", polls=lambda p: len(p) == 1 and len(p[0]["options"]) >= 3),
                             "a poll of members"), tags=("games",)),
    Scenario("title", "дерп, кто сегодня красавчик дня?",
             lambda r, s: ok(r.called("title_of_the_day"), "title_of_the_day"), tags=("games",)),
    Scenario("dice", "derp roll a dice for me", lambda r, s: ok(r.called("send_dice"), "send_dice"), tags=("telegram",)),
    Scenario("sticker", "дерп, ответь стикером на это",
             lambda r, s: ok(r.called("send_sticker"), "send_sticker"), reply_to="crash", tags=("telegram",)),
    Scenario("react", "derp react to this with a clown",
             lambda r, s: ok(r.called("react", emoji=has("🤡")), "react clown"), reply_to="deploy", tags=("telegram",)),
    Scenario("pin", "дерп, закрепи это",
             lambda r, s: ok(r.called("message_action", action="pin", message_id=s.ids["decision"]), "pin"),
             reply_to="decision", tags=("telegram",)),
    # utilities and Derp itself
    Scenario("weather", "derp what's the weather in Berlin tomorrow?",
             lambda r, s: ok(r.called("weather"), "weather"), tags=("utilities",)),
    Scenario("fx", "дерп, сколько будет 100 долларов в рублях?",
             lambda r, s: ok(r.called("market", quote=has("rub")), "market in RUB"), tags=("market",)),
    Scenario("btc_chart", "derp show me a bitcoin chart for the week",
             lambda r, s: ok(r.called("market", chart=True, period="1w") and "sendPhoto" in r.outputs,
                             "a week's chart posted"), tags=("market",)),
    Scenario("moex", "дерп, как там сбер сегодня?",
             lambda r, s: ok(r.returned("market", "Сбербанк"), "SBER on the Moscow Exchange"), tags=("market",)),
    Scenario("funding", "derp what's the funding rate on ETH perps right now?",
             lambda r, s: ok(any(t == "market" and "perpetual funding" in out for t, out in r.returns),
                             "funding from Binance futures"), tags=("market",)),
    Scenario("odds", "derp what are the odds the Fed changes rates in December?",
             lambda r, s: ok(r.called("market", kind="prediction"), "Polymarket odds"), tags=("market",)),
    Scenario("bets_chart", "дерп, на что сейчас больше всего ставят на полимаркете? покажи график",
             lambda r, s: ok(r.called("market", kind="prediction", chart=True) and "sendPhoto" in r.outputs,
                             "popular bets charted"), tags=("market",)),
    Scenario("chart_meme", "derp make a meme of this week's bitcoin chart: 'HODL'",
             lambda r, s: ok(r.called("market", chart=True, final=False) and r.called("make_meme", source="r1"),
                             "chart kept, then memed"), tags=("market",)),
    Scenario("math", "дерп, сколько будет 17% от 23 450, плюс НДС 20% сверху?",
             lambda r, s: ok(r.says("4[  ]?783"), "4783.8"), tags=("utilities",)),
    Scenario("urban", "дерп, глянь в урбан дикшенери, что такое rizz",
             lambda r, s: ok(r.called("lookup", source="urban_dictionary"), "urban dictionary"), tags=("utilities",)),
    Scenario("about_price", "derp how much does a picture cost?",
             lambda r, s: ok(r.called("about_derp"), "about_derp"), tags=("about",)),
    Scenario("balance", "derp what's my balance?",
             lambda r, s: ok(r.called("wallet_status"), "wallet_status"), tags=("self",)),
    Scenario("feedback", "derp tell your devs the quiz scoring is broken",
             lambda r, s: ok(r.called("leave_feedback"), "leave_feedback"), tags=("self",)),
    # media effects and images (these spend more: images ~$0.03, voice lines)
    Scenario("meme_reply", "derp make a meme of this: 'when you parallel park for the first time'",
             lambda r, s: ok(r.called("make_meme"), "make_meme"), reply_to="car_photo", tags=("effects",)),
    Scenario("sticker_from_photo", "derp make this a sticker",
             lambda r, s: ok(r.called("media_effect", effect="sticker"), "sticker"), reply_to="car_photo",
             tags=("effects",)),
    Scenario("mirror", "дерп, отзеркаль эту фотку",
             lambda r, s: ok(r.called("media_effect", effect="reverse"), "mirror"), reply_to="car_photo",
             tags=("effects",)),
    Scenario("quote_reply", "дерп, процитируй это",
             lambda r, s: ok(r.called("make_quote"), "make_quote"), reply_to="crash", tags=("effects",)),
    Scenario("avatar_meme", "дерп, сделай мем из аватарки Васи с подписью «водитель года»",
             lambda r, s: ok(r.called("make_meme", source=has("ас")) or r.called("make_meme", source=has("vasya")),
                             "make_meme on Vasya's avatar"), tags=("effects",)),
    Scenario("speak", "дерп, крикни голосом Васе, что он опять опоздает",
             lambda r, s: ok(r.called("speak"), "speak"), tags=("effects",)),
    Scenario("draw_people", "derp draw Аня and Bob as medieval knights",
             lambda r, s: ok(r.called("generate_image", images=lambda v: len(v) == 2), "both drawn from their photos"),
             tags=("images",)),
    Scenario("edit_photo", "дерп, пририсуй на эту машину огромный спойлер",
             lambda r, s: ok(_photo_of("car_photo")(r, s), "edits the car photo"), reply_to="car_photo", tags=("images",)),
    # voice: no tools needed
    Scenario("banter", "дерп, ты тупой", lambda r, s: ok(not r.tools and len(r.text) < 300, "no tools, short"),
             tags=("voice",)),
    Scenario("slang", "дерп, что значит rizz?", lambda r, s: ok(r.text, "answers"), tags=("voice",)),
]  # fmt: skip
