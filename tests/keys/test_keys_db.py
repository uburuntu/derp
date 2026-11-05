from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from derp.agent.runs import start_run
from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat, User
from derp.inference.api import Credential, OpenRouterError, SubKey
from derp.inference.models import ProviderCall
from derp.keys.catalog import sync_registrations
from derp.keys.flows import key_page
from derp.keys.models import ContributedKey, KeyChat
from derp.keys.register import Refused, plug_in, take_out
from derp.keys.routing import next_cycle
from derp.keys.sweep import detach_member
from derp.keys.vault import seal, unseal
from derp.money.funding import Funder, Funding, run_cost, run_upstream
from derp.money.wallets import credit, wallet

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
GROUP = -100500
pytestmark = pytest.mark.db


@pytest.fixture
def keyed(settings: Settings) -> Settings:
    return settings.model_copy(update={"keys_secret": "test-secret"})


async def add_key(
    database: Database, settings: Settings, user_id: int, *, chats: tuple[int, ...] = (), cap: str = "100",
    enabled: datetime = NOW, model: str = "openai/gpt-6.1-sol",
) -> int:  # fmt: skip
    async with database.transaction() as session:
        key = ContributedKey(
            user_id=user_id, provider="openai", label="sk-…abcd", model=model, cap_usd=Decimal(cap),
            cycle_anchor=date(2026, 11, 1), sub_key_hash=f"hash{user_id}{len(chats)}{cap}",
            sub_key=seal(settings.keys_secret, f"sk-or-sub-{user_id}"), credential_id=f"cred{user_id}",
        )  # fmt: skip
        session.add(key)
        await session.flush()
        for chat_id in (user_id, *chats):
            session.add(KeyChat(key_id=key.id, chat_id=chat_id, state="on", enabled_at=enabled))
        return key.id


async def spend(database: Database, key_id: int, usd: str, *, run_id: int | None = None) -> None:
    async with database.transaction() as session:
        session.add(
            ProviderCall(
                kind="chat", model="openai/gpt-6.1-sol", funding="key", key_id=key_id, upstream_usd=Decimal(usd),
                cost_usd=Decimal("0.0001"), run_id=run_id, chat_id=GROUP, created_at=NOW,
            )
        )  # fmt: skip


async def test_a_key_on_in_the_chat_pays_first_and_a_wallet_backs_its_side_work(
    database: Database, keyed: Settings
) -> None:
    funder = Funder(database, keyed)
    key_id = await add_key(database, keyed, 7, chats=(GROUP,))
    bare = await funder.reserve(chat_id=GROUP, private=False, user_id=8, operation_id="turn:1", now=NOW)
    assert bare is not None and bare.key is not None and bare.key.key_id == key_id
    assert (bare.funding, bare.wallet_id, bare.reservation_id, bare.credits) == (Funding.PAID, None, None, 0)
    assert unseal(keyed.keys_secret, seal(keyed.keys_secret, bare.key.sub_key)) == "sk-or-sub-7"
    assert not bare.key.pin.zdr and bare.key.left_usd == Decimal(100)

    async with database.transaction() as session:
        chat_wallet = (await wallet(session, "chat", GROUP)).id
        await credit(session, chat_wallet, "stars:c", 5, "purchase")  # less than a turn: enough for side work
    backed = await funder.reserve(chat_id=GROUP, private=False, user_id=8, operation_id="turn:2", now=NOW)
    assert (
        backed is not None
        and backed.key is not None
        and (backed.wallet_id, backed.credits) == (chat_wallet, 5)
    )

    without = await funder.reserve(
        chat_id=GROUP, private=False, user_id=8, operation_id="turn:3", now=NOW, keys=False
    )
    assert without is None or without.key is None
    approved = await funder.reserve(
        chat_id=GROUP, private=False, user_id=8, operation_id="turn:4", now=NOW, paid_minimum=1
    )
    assert approved is None  # priced in Derpcoins (the chat's few are held): never a key


async def test_keys_serve_in_the_order_they_were_turned_on_until_their_cap_is_spent(
    database: Database, keyed: Settings
) -> None:
    funder = Funder(database, keyed)
    first = await add_key(
        database, keyed, 7, chats=(GROUP,), cap="1", enabled=datetime(2026, 11, 1, tzinfo=UTC)
    )
    second = await add_key(database, keyed, 9, chats=(GROUP,), enabled=datetime(2026, 11, 2, tzinfo=UTC))
    funds = await funder.reserve(chat_id=GROUP, private=False, user_id=8, operation_id="turn:1", now=NOW)
    assert funds is not None and funds.key is not None and funds.key.key_id == first
    await spend(database, first, "0.999")  # what is left can't cover a typical turn on its model
    funds = await funder.reserve(chat_id=GROUP, private=False, user_id=8, operation_id="turn:2", now=NOW)
    assert funds is not None and funds.key is not None and funds.key.key_id == second
    async with database.session() as session:
        paused = await session.get(ContributedKey, first)
        assert paused is not None and paused.paused == "cap"
        assert paused.paused_until == next_cycle(date(2026, 11, 1), NOW)


async def test_a_key_behind_another_says_which_answers_first(database: Database, keyed: Settings) -> None:
    funder = Funder(database, keyed)
    first = await add_key(database, keyed, 7, chats=(GROUP,), enabled=datetime(2026, 11, 1, tzinfo=UTC))
    second = await add_key(database, keyed, 9, chats=(GROUP,), enabled=datetime(2026, 11, 2, tzinfo=UTC))
    async with database.transaction() as session:
        session.add_all([Chat(id=GROUP, type="supergroup", title="Crew"), User(id=7, first_name="Ana")])
    async with database.session() as session:
        waiting, _ = await key_page(
            session, keyed, funder, await session.get_one(ContributedKey, second), "en"
        )
        serving, _ = await key_page(
            session, keyed, funder, await session.get_one(ContributedKey, first), "en"
        )
    assert "Crew: $0.00 · on, waiting: **Ana**'s key answers here first" in waiting
    assert "Crew: $0.00 · on\n" in serving + "\n"


async def test_the_owners_key_serves_their_private_chat_and_nobody_elses(
    database: Database, keyed: Settings
) -> None:
    funder = Funder(database, keyed)
    key_id = await add_key(database, keyed, 7)
    mine = await funder.reserve(chat_id=7, private=True, user_id=7, operation_id="turn:1", now=NOW)
    assert mine is not None and mine.key is not None and mine.key.key_id == key_id
    other = await funder.reserve(chat_id=8, private=True, user_id=8, operation_id="turn:2", now=NOW)
    assert other is None or other.key is None
    sharing = await funder.reserve(chat_id=GROUP, private=False, user_id=7, operation_id="turn:3", now=NOW)
    assert sharing is None or sharing.key is None  # not turned on in the group


async def test_a_keys_calls_are_never_charged_and_are_summed_as_its_providers_charge(
    database: Database, keyed: Settings
) -> None:
    key_id = await add_key(database, keyed, 7, chats=(GROUP,))
    async with database.transaction() as session:
        run = await start_run(
            session, chat_id=GROUP, user_id=8, trigger="message", role="chat", funding="paid", key_id=key_id
        )
        session.add(
            ProviderCall(kind="digest", model="x", funding="paid", run_id=run.id, cost_usd=Decimal("0.002"))
        )
    await spend(database, key_id, "0.05", run_id=run.id)
    async with database.session() as session:
        assert await run_cost(session, run.id) == Decimal("0.002")
        assert await run_upstream(session, run.id) == Decimal("0.05")


@dataclass
class FakeManagement:
    calls: list[tuple[str, object]] = field(default_factory=list[tuple[str, object]])
    register_status: int | None = None

    async def create_key(self, name: str, *, limit_usd: float) -> SubKey:
        self.calls.append(("create_key", limit_usd))
        return SubKey(secret="sk-or-v1-sub", hash="a" * 64)

    async def delete_key(self, key_hash: str) -> None:
        self.calls.append(("delete_key", key_hash))

    async def register(
        self, provider: str, key: str, *, name: str, sub_key_hash: str, models: list[str]
    ) -> Credential:
        self.calls.append(("register", (provider, sub_key_hash, tuple(models))))
        if self.register_status:
            raise OpenRouterError(self.register_status, "no")
        return Credential(id="cred-1", label="sk-...wxyz")

    async def allow_models(self, credential_id: str, models: list[str]) -> None:
        self.calls.append(("allow_models", credential_id))

    async def unregister(self, credential_id: str) -> None:
        self.calls.append(("unregister", credential_id))


@dataclass
class FakePing:
    status: int | None = None
    pinged: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])

    async def ping(self, model: str, provider: str, *, api_key: str) -> None:
        self.pinged.append((model, provider, api_key))
        if self.status:
            raise OpenRouterError(self.status, "no")


async def test_plugging_in_registers_the_key_behind_its_own_sub_key(
    database: Database, keyed: Settings
) -> None:
    management, ping = FakeManagement(), FakePing()
    key = await plug_in(database, keyed, management, ping, 7, "sk-proj-" + "z" * 40)  # type: ignore[arg-type]
    assert isinstance(key, ContributedKey) and key.provider == "openai" and key.label == "sk-...wxyz"
    assert ping.pinged == [("openai/gpt-6-luna", "openai", "sk-or-v1-sub")]  # the cheapest curated model
    registered = next(args for name, args in management.calls if name == "register")
    assert registered == (
        "openai",
        "a" * 64,
        tuple(m for m in (*[x.model for x in keyed.keys.models["openai"]], keyed.keys.images["openai"])),
    )
    async with database.session() as session:
        row = await session.get(ContributedKey, key.id)
        links = list(await session.scalars(select(KeyChat).where(KeyChat.key_id == key.id)))
    assert row is not None and unseal(keyed.keys_secret, row.sub_key) == "sk-or-v1-sub"
    assert [(link.chat_id, link.state) for link in links] == [(7, "on")]
    assert row.model == "openai/gpt-6.1-sol"  # the recommended one until they pick


async def test_startup_registers_every_live_key_for_todays_models(
    database: Database, keyed: Settings
) -> None:
    live = await add_key(database, keyed, 7)
    gone = await add_key(database, keyed, 9)
    async with database.transaction() as session:
        (await session.get_one(ContributedKey, gone)).removed_at = NOW
    management = FakeManagement()
    await sync_registrations(database, keyed, management)  # type: ignore[arg-type]
    assert management.calls == [("allow_models", f"cred{7}")] and live


@pytest.mark.parametrize(("status", "why"), [(401, "rejected"), (402, "no_credits"), (502, "unavailable")])
async def test_a_key_the_provider_refuses_is_taken_out_again(
    database: Database, keyed: Settings, status: int, why: str
) -> None:
    management = FakeManagement()
    refused = await plug_in(database, keyed, management, FakePing(status), 7, "sk-ant-" + "q" * 40)  # type: ignore[arg-type]
    assert refused == Refused(why)  # type: ignore[comparison-overlap]
    assert [name for name, _ in management.calls] == ["create_key", "register", "unregister", "delete_key"]
    async with database.session() as session:
        assert await session.scalar(select(ContributedKey.id)) is None


async def test_removing_unregisters_and_unplugs_everywhere(database: Database, keyed: Settings) -> None:
    key_id = await add_key(database, keyed, 7, chats=(GROUP,))
    async with database.session() as session:
        key = await session.get(ContributedKey, key_id)
    assert key is not None
    management = FakeManagement()
    await take_out(database, management, key)  # type: ignore[arg-type]
    assert [name for name, _ in management.calls] == ["unregister", "delete_key"]
    async with database.session() as session:
        row = await session.get(ContributedKey, key_id)
        states = set(await session.scalars(select(KeyChat.state).where(KeyChat.key_id == key_id)))
    assert row is not None and row.removed_at is not None and states == {"off"}


async def test_leaving_a_chat_unplugs_your_keys_there_and_leaves_the_owner_to_be_told(
    database: Database, keyed: Settings
) -> None:
    key_id = await add_key(database, keyed, 7, chats=(GROUP,))
    async with database.transaction() as session:
        await detach_member(session, GROUP, 7)
    async with database.session() as session:
        group = await session.get(KeyChat, (key_id, GROUP))
        private = await session.get(KeyChat, (key_id, 7))
    assert group is not None and (group.state, group.off_noticed_at) == ("off", None)
    assert private is not None and private.state == "on"
