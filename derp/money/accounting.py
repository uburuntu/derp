"""Read-only, chat-scoped accounting for the conversational tool and Telegram receipts."""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlsplit

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from derp.agent.models import AgentRun
from derp.agent.runs import runs_for_output
from derp.history.models import Chat, User
from derp.history.queries import members_named
from derp.inference.models import ProviderCall
from derp.keys.models import ContributedKey
from derp.money.amounts import format_dc
from derp.money.funding import run_fees
from derp.money.models import Reservation, Wallet
from derp.money.wallets import balances

GroupBy = Literal["total", "day", "member", "model", "call_type", "funding", "key_contributor"]
FundingSource = Literal["chat_wallet", "personal_wallet", "sponsored", "free", "creator", "key"]


def message_ref(chat: Chat, ref: str) -> int | None:
    """An id or a Telegram permalink, including a forum link. Verify the peer before using the id."""
    if ref.startswith(("https://", "http://")):
        url = urlsplit(ref)
        parts = url.path.strip("/").split("/")
        if url.netloc.lower() != "t.me":
            return None
        if parts[0] == "c":
            if len(parts) < 3 or chat.id >= 0 or str(chat.id) != f"-100{parts[1]}":
                return None
            parts = parts[2:]
        elif chat.username and parts[0].casefold() == chat.username.casefold():
            parts = parts[1:]
        else:
            return None
        if len(parts) not in (1, 2) or not all(re.fullmatch(r"[0-9]{1,12}", p) for p in parts):
            return None
        ref = parts[-1]
    match = re.fullmatch(r"#?([1-9][0-9]{0,11})", ref)
    return int(match[1]) if match else None


async def wallet_snapshot(session: AsyncSession, chat: Chat, user_id: int, now: datetime) -> dict[str, str]:
    private = chat.type == "private"
    wallet_id = await session.scalar(
        select(Wallet.id).where(
            Wallet.kind == ("user" if private else "chat"),
            Wallet.owner_id == (user_id if private else chat.id),
        )
    )
    available = sum((await balances(session, wallet_id, now=now)).values()) if wallet_id is not None else 0
    held = (
        await session.scalar(
            select(func.sum(Reservation.amount)).where(
                Reservation.wallet_id == wallet_id, Reservation.state == "held"
            )
        )
        if wallet_id is not None
        else 0
    )
    return {
        "wallet": "yours" if private else "shared",
        "available_dc": format_dc(available),
        "held_dc": format_dc(held or 0),
        "total_dc": format_dc(available + (held or 0)),
    }


@dataclass(frozen=True)
class Receipt:
    run: AgentRun
    revision: bool
    wallet_kind: str | None
    key: ContributedKey | None
    contributor: User | None
    provider_usd: Decimal
    upstream_usd: Decimal
    fee_dc: Decimal


async def receipts(session: AsyncSession, chat_id: int, message_id: int) -> list[Receipt]:
    result: list[Receipt] = []
    for run, revision in await runs_for_output(session, chat_id, message_id):
        wallet = await session.get(Wallet, run.wallet_id) if run.wallet_id is not None else None
        key = await session.get(ContributedKey, run.key_id) if run.key_id is not None else None
        owner = await session.get(User, key.user_id) if key is not None else None
        usd, upstream = (
            await session.execute(
                select(func.sum(ProviderCall.cost_usd), func.sum(ProviderCall.upstream_usd)).where(
                    ProviderCall.chat_id == chat_id, ProviderCall.run_id == run.id
                )
            )
        ).one()
        result.append(
            Receipt(
                run,
                revision,
                wallet.kind if wallet else None,
                key,
                owner,
                Decimal(usd or 0),
                Decimal(upstream or 0),
                Decimal(await run_fees(session, run.id)),
            )
        )
    return result


def date_range(
    since: date | None, until: date | None, now: datetime, tz: tzinfo
) -> tuple[datetime, datetime]:
    today = now.astimezone(tz).date()
    first, last = since or until or today, until or today
    if last < first or last == date.max:
        raise ValueError("Give an inclusive end date on or after the start.")
    return datetime.combine(first, time.min, tz), datetime.combine(last + timedelta(days=1), time.min, tz)


# Keep calls, run/standalone charges, and flat fees at their own granularity. Joining all three by
# run would repeat both the paid amount and token count for every image/fee attached to the run.
_USAGE = """
WITH events AS (
    SELECT p.created_at AS at, p.user_id, p.model,
           CASE WHEN p.role = 'rule_screen' THEN 'screening' ELSE p.kind END AS call_type,
           CASE WHEN p.funding = 'paid' THEN CASE WHEN p.reservation_id IS NOT NULL OR w.kind = 'chat'
                THEN 'chat_wallet' ELSE 'personal_wallet' END ELSE p.funding END AS funding,
           CASE WHEN k.id IS NULL THEN NULL WHEN patron.patron_named THEN k.user_id::text
                ELSE 'anonymous patron' END AS contributor, 1 AS calls, p.input_tokens AS tokens_in,
           p.output_tokens AS tokens_out, p.cache_read_tokens AS cached,
           p.cost_usd AS provider_usd, coalesce(p.upstream_usd, 0) AS key_usd,
           coalesce(screen.captured_dc, 0) AS charged_dc, 0 AS fee_dc
    FROM provider_calls p
    LEFT JOIN agent_runs r ON r.id = p.run_id AND r.chat_id = p.chat_id
    LEFT JOIN wallets w ON w.id = r.wallet_id
    LEFT JOIN contributed_keys k ON k.id = p.key_id
    LEFT JOIN users patron ON patron.id = k.user_id
    LEFT JOIN reservations screen ON screen.id = p.reservation_id AND NOT EXISTS (
        SELECT 1 FROM provider_calls earlier
        WHERE earlier.reservation_id = p.reservation_id AND earlier.id < p.id
    )
    WHERE p.chat_id = :chat AND p.created_at >= :since AND p.created_at < :until
    UNION ALL
    SELECT coalesce(r.finished_at, r.created_at), r.user_id, NULL, 'settlement', CASE WHEN w.kind = 'chat' THEN 'chat_wallet'
           WHEN r.funding = 'sponsored' THEN 'sponsored' ELSE 'personal_wallet' END, NULL, 0, 0, 0, 0, 0, 0, r.charged_credits_dc, 0
    FROM agent_runs r JOIN wallets w ON w.id = r.wallet_id
    WHERE r.chat_id = :chat AND coalesce(r.finished_at, r.created_at) >= :since AND coalesce(r.finished_at, r.created_at) < :until
          AND r.charged_credits_dc > 0 AND (w.kind = 'chat' OR :private)
    UNION ALL
    SELECT f.created_at, f.user_id, NULL, f.item, CASE WHEN w.kind = 'chat' THEN 'chat_wallet'
           WHEN r.funding = 'sponsored' THEN 'sponsored' ELSE 'personal_wallet' END, NULL, 0, 0, 0, 0, 0, 0, 0, f.credits_dc
    FROM run_fees f JOIN agent_runs r ON r.id = f.run_id AND r.chat_id = f.chat_id
    JOIN wallets w ON w.id = r.wallet_id
    WHERE f.chat_id = :chat AND f.created_at >= :since AND f.created_at < :until
          AND f.credits_dc > 0 AND (w.kind = 'chat' OR :private)
), filtered AS (
    SELECT * FROM events
    WHERE (CAST(:member AS bigint) IS NULL OR user_id = :member)
      AND (CAST(:model AS text) IS NULL OR model = :model)
      AND (CAST(:call_type AS text) IS NULL OR call_type = :call_type)
      AND (CAST(:funding AS text) IS NULL OR funding = :funding)
), bucketed AS (
    SELECT *, CASE CAST(:group_by AS text)
        WHEN 'day' THEN to_char(timezone(:zone, at), 'YYYY-MM-DD')
        WHEN 'member' THEN user_id::text WHEN 'key_contributor' THEN contributor::text
        WHEN 'model' THEN model WHEN 'call_type' THEN call_type WHEN 'funding' THEN funding
        ELSE 'total' END AS bucket
    FROM filtered
)
SELECT bucket, GROUPING(bucket) AS grand, sum(calls) AS calls, sum(tokens_in) AS input_tokens,
    sum(tokens_out) AS output_tokens, sum(cached) AS cached_input_tokens,
    sum(provider_usd) AS provider_usd, sum(key_usd) AS key_usd,
    sum(charged_dc) AS wallet_dc, sum(fee_dc) AS tool_fees_dc
FROM bucketed GROUP BY GROUPING SETS ((bucket), ())
ORDER BY grand DESC, sum(charged_dc) DESC, sum(tokens_in) DESC, bucket LIMIT 51
"""


async def usage(
    session: AsyncSession,
    chat: Chat,
    since: datetime,
    until: datetime,
    *,
    group_by: GroupBy = "total",
    member: str | None = None,
    model: str | None = None,
    call_type: str | None = None,
    funding: FundingSource | None = None,
) -> dict[str, Any]:
    members = await members_named(session, chat.id, [member]) if member else {}
    if member and len(members) != 1:
        raise ValueError("Use the exact @username of one member of this chat.")
    rows = (
        (
            await session.execute(
                text(_USAGE),
                {
                    "chat": chat.id,
                    "private": chat.type == "private",
                    "since": since,
                    "until": until,
                    "zone": str(since.tzinfo),
                    "group_by": group_by,
                    "member": members.get(member) if member else None,
                    "model": model,
                    "call_type": call_type,
                    "funding": funding,
                },
            )
        )
        .mappings()
        .all()
    )
    names: dict[int, str] = {}
    if group_by in {"member", "key_contributor"}:
        ids = [int(r["bucket"]) for r in rows if r["bucket"] is not None and r["bucket"].isdigit()]
        names = {u.id: u.label for u in await session.scalars(select(User).where(User.id.in_(ids)))}
    result: dict[str, Any] = {"since": since.isoformat(), "until_exclusive": until.isoformat()}
    groups: list[dict[str, Any]] = []
    for row in rows:
        values = {
            k: (
                int(row[k] or 0)
                if k in ("calls", "input_tokens", "output_tokens", "cached_input_tokens")
                else format_dc(row[k] or 0)
            )
            for k in row
            if k not in {"bucket", "grand", "events"}
        }
        if row["grand"]:
            result["totals"] = values
        elif group_by != "total":
            label = row["bucket"]
            if group_by in {"member", "key_contributor"} and label is not None and label.isdigit():
                label = names.get(int(label), "unknown")
            groups.append({group_by: label or "unattributed", **values})
    if group_by != "total":
        result["groups_top_50"] = groups
    result["units"] = (
        "Cached input is included in input. Provider USD is Derp's provider cost; key USD is the "
        "contributor's upstream cost. Wallet dc is actual settled shared spend (own spend in private). "
        "A whole run settles once and has no single call type or model. Tool fees are nominal dc included "
        "in its price; do not add them to the wallet debit."
    )
    return result
