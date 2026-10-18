"""Recall eval for ``search``: can people find what they half-remember?

A synthetic chat (en/ru, voice transcripts, a photo description, nicknames)
and questions worded the way people remember things, not the way they were
written. Each case lists the queries the chat model sends in one
``search_history`` call: its own words first, then its paraphrases and
translations, none of them borrowing words from the answer. A case is a hit
when one of its messages is among the top five matches or the context lines
shown with them.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from derp.agent.tools.history import SEARCH_CONTEXT
from derp.db.engine import Database
from derp.history.models import Chat, ChatMember, Message, User
from derp.history.queries import Scope, conversations, resolve_members, search

CHAT = -760
NOW = datetime.now(UTC)
ANN, BOB, DIMA, KATYA = 1, 2, 3, 4

# (id, sender, text, searchable media text, days ago)
CHAT_LINES: list[tuple[int, int, str | None, str | None, float]] = [
    (1, ANN, "всем привет, кто в субботу на шашлыки?", None, 14),
    (2, BOB, "я в деле, возьму мангал", None, 14),
    (3, DIMA, "Сходили вчера в грузинское место у вокзала, хинкали огонь, всем советую", None, 13),
    (4, KATYA, "как называется?", None, 13),
    (5, DIMA, "Сакартвело, прямо напротив Курского", None, 13),
    (6, BOB, "the train was late again today, station was packed", None, 12),
    (7, ANN, "anyone know a good dentist? my tooth is killing me", None, 12),
    (8, KATYA, "Dr. Petrova on Lenina street, she's gentle and cheap", None, 12),
    (9, BOB, None, ("короче я посмотрел квартиру на Таганке, сорок пять квадратов, хозяин просит "
     "шестьдесят тысяч в месяц, но торгуется"), 11),
    (10, DIMA, "кто-нибудь брал мой зарядник? белый, type-c", None, 11),
    (11, ANN, "@dvolk it's on the kitchen shelf", None, 11),
    (12, KATYA, "фильм вечером? предлагаю Дюну", None, 10),
    (13, BOB, "I'd rather watch Interstellar tbh", None, 10),
    (14, KATYA, "у Димона днюха 14 ноября, скидываемся на подарок?", None, 10),
    (15, BOB, "скинул 500", None, 10),
    (16, DIMA, "guys my laptop died, screen just went black", None, 9),
    (17, ANN, "try holding the power button for 30 seconds", None, 9),
    (18, KATYA, None, ("so I talked to the landlord and he says the heating will be fixed by Thursday, "
     "finally"), 9),
    (19, BOB, "where should we go for vacation in may?", None, 8),
    (20, ANN, "Грузия! дешево и вкусно, Тбилиси и Казбеги", None, 8),
    (21, DIMA, "I vote Portugal, Lisbon is amazing in spring", None, 8),
    (22, KATYA, "recipe for that eggplant thing pls", None, 7),
    (23, ANN, "баклажаны режешь, солишь, жаришь, потом чеснок и орехи", None, 7),
    (24, BOB, "the wifi password is horse-battery-staple", None, 7),
    (25, DIMA, "spotify family plan: 4 people, 200 rub each", None, 6),
    (26, KATYA, "кто забыл зонт у меня дома? синий", None, 6),
    (27, BOB, "мой, заберу завтра", None, 6),
    (28, ANN, "meeting moved to 3pm tomorrow, same zoom link", None, 6),
    (29, DIMA, None, "a ginger cat sleeping on a laptop keyboard", 5),
    (30, KATYA, "лол он прям как мой", None, 5),
    (31, BOB, "Interstellar it is, starting 9pm", None, 5),
    (32, ANN, "кстати, ключи от дачи у соседки, тёти Гали", None, 5),
    (33, DIMA, "rent for the dacha this summer is 30k per month", None, 4),
    (34, KATYA, None, "привет, я на месте, но вас никого нет, я у главного входа", 4),
    (35, BOB, "сорри, пробки", None, 4),
    (36, ANN, "I recommend the book Project Hail Mary, couldn't put it down", None, 3),
    (37, DIMA, "Кто-то советовал сериал, как он назывался?", None, 3),
    (38, KATYA, "Severance, season 2 is out", None, 3),
    (39, BOB, "моя машина в ремонте до пятницы, сцепление полетело", None, 3),
    (40, ANN, "the vet said Murzik needs to lose weight lol", None, 2),
    (41, DIMA, "ахаха жирный котик", None, 2),
    (42, KATYA, "booked tickets to Kazan for the 12th, train leaves at 7:40 from Kazansky", None, 2),
    (43, BOB, "кто-нибудь знает хорошего мастера по ремонту стиралок?", None, 2),
    (44, DIMA, "звони Сергею, он мне стиралку за час починил", None, 2),
    (45, ANN, None, "напоминаю что завтра скидываемся на коммуналку по две тысячи", 1),
    (46, KATYA, "the concert got cancelled, refunds by email", None, 1),
    (47, BOB, "nooo I was waiting for it", None, 1),
    (48, DIMA, "пицца или суши сегодня?", None, 1),
    (49, ANN, "the sushi place near the office closed down", None, 1),
    (50, KATYA, "лучший кофе в городе в Скуратове на Покровке", None, 0.5),
    (51, BOB, "what time is it in Lisbon now", None, 0.4),
    (52, DIMA, "who wants to go to the station cafe for lunch", None, 0.3),
    (53, KATYA, "Бобби опять проспал, начинаем без него", None, 0.2),
]  # fmt: skip


@dataclass(frozen=True, slots=True)
class Case:
    question: str
    queries: tuple[str, ...]  # one search_history call: the model's words, then its paraphrases
    expected: frozenset[int]
    user: str | None = None


def case(question: str, expected: set[int], *queries: str, user: str | None = None) -> Case:
    return Case(question, queries, frozenset(expected), user)


CASES = [
    case("the restaurant someone recommended near the station", {3, 5},
         "restaurant recommended near the station", "ресторан возле вокзала посоветовали", "кафе у вокзала"),
    case("who was the dentist Katya suggested?", {8},
         "dentist", "стоматолог", "зубной врач"),
    case("how much was the flat Bob looked at?", {9},
         "flat rent price", "квартира цена", "снять квартиру аренда", user="Bob"),
    case("чей белый зарядник потерялся?", {10, 11},
         "зарядник", "charger", "зарядка type-c"),
    case("when is Dima's birthday?", {14},
         "Dima birthday", "Дима день рождения", "днюха Димы"),
    case("how did Ann say to fix the dead laptop?", {17},
         "fix laptop", "laptop died black screen", "ноутбук сломался"),
    case("когда починят отопление?", {18},
         "отопление", "heating fixed", "heating landlord"),
    case("where did Ann want to go on vacation?", {20},
         "vacation", "куда поехать в отпуск", "отпуск в мае", user="Ann"),
    case("рецепт с баклажанами", {23},
         "рецепт баклажаны", "eggplant recipe", "баклажан"),
    case("what was the wfi pasword", {24},
         "wfi pasword", "wifi password", "пароль от вайфая"),
    case("сколько платим за спотифай?", {25},
         "спотифай", "spotify", "spotify family plan"),
    case("whose blue umbrella was left at Katya's?", {26, 27},
         "umbrella", "зонт", "синий зонт"),
    case("the photo of the cat on the computer", {29},
         "cat on the computer", "cat laptop", "кот на ноутбуке", "cat photo"),
    case("where are the keys to the country house?", {32},
         "keys country house", "ключи от дачи", "ключи дача", "dacha keys"),
    case("где Катя ждала нас?", {34},
         "Катя ждала", "жду вас где", "waiting for us", user="Katya"),
    case("какую книгу советовали?", {36},
         "книга советовали", "book recommend", "книгу посоветовать"),
    case("what's wrong with Bob's car?", {39},
         "car", "машина ремонт", "машина сломалась", user="Bob"),
    case("что сказал ветеринар про кота?", {40},
         "ветеринар кот", "vet cat", "vet said"),
    case("when does the train to Kazan leave?", {42},
         "train to Kazan", "поезд в Казань", "Kazan tickets"),
    case("who repairs washing machines?", {44, 43},
         "washing machine repair", "ремонт стиральной машины", "мастер стиралка"),
    case("сколько скидываемся на коммуналку?", {45},
         "коммуналка скидываемся", "коммуналку", "utilities bill"),
    case("what happened with the concert?", {46},
         "concert", "концерт", "concert cancelled"),
    case("where's the best coffee in town?", {50},
         "best coffee", "лучший кофе", "кофе"),
    case("the show someone recommended, Северанс?", {38},
         "Северанс", "Severance", "сериал"),
    case("did Bob oversleep again?", {53},
         "Bob overslept", "Боб проспал", "Бобби проспал"),
]  # fmt: skip


@pytest.fixture
async def recall_chat(database: Database) -> Database:
    async with database.transaction() as session:
        session.add(Chat(id=CHAT, type="supergroup", title="Flat"))
        session.add_all(
            [
                User(id=ANN, first_name="Ann", username="annie"),
                User(id=BOB, first_name="Bob", last_name="Stone", username="bobst"),
                User(id=DIMA, first_name="Dima", last_name="Volkov", username="dvolk"),
                User(id=KATYA, first_name="Katya", username="kat_k"),
            ]
        )
        session.add_all(
            ChatMember(chat_id=CHAT, user_id=uid, names=[name])
            for uid, name in [(ANN, "Ann"), (BOB, "Bob Stone"), (DIMA, "Dima Volkov"), (KATYA, "Katya")]
        )
        session.add_all(
            Message(
                chat_id=CHAT, message_id=mid, user_id=uid, date=NOW - timedelta(days=ago),
                kind="text" if text else "voice", text=text, extra_text=extra, capture="ambient", payload={},
            )
            for mid, uid, text, extra, ago in CHAT_LINES
        )  # fmt: skip
    return database


async def hit_rate(database: Database, *, phrasings: int, context: int) -> float:
    """Share of cases with a wanted message among the top five hits or their context lines."""
    hits = 0
    for item in CASES:
        async with database.session() as session:
            users = await resolve_members(session, CHAT, [item.user]) if item.user else []
            found = (await search(session, Scope(CHAT), item.queries[:phrasings], user_ids=users))[:5]
            windows = await conversations(session, found, before=context, after=context)
        hits += any(row.message_id in item.expected for window in windows for row in window)
    return hits / len(CASES)


@pytest.mark.db
async def test_search_finds_what_people_half_remember(recall_chat: Database) -> None:
    # The old single-phrasing, all-words search found 5 of these 25 (0.20).
    asker_words = await hit_rate(recall_chat, phrasings=1, context=0)
    one_call = await hit_rate(recall_chat, phrasings=5, context=SEARCH_CONTEXT)
    print(f"hit@5: asker's words only {asker_words:.2f}, one search_history call {one_call:.2f}")
    assert asker_words >= 0.28 and one_call >= 0.92
