"""
Database layer — trip-centric schema.

Core concept
------------
A *trip* is a named expense group (like a holiday, a house, a dinner).
Trips live inside a Telegram chat (group or private).
Each trip has *trip_members*, which can be:
  - Real Telegram users (telegram_user_id is set)
  - Virtual participants (telegram_user_id is NULL — for solo / private-chat testing)

All monetary references use trip_member.id, not telegram user_id directly.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from sqlalchemy import (
    BigInteger, Column, DateTime, Float, ForeignKey,
    Integer, MetaData, String, Table, Text, func, select, insert, update, text,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import bot.config as cfg

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

metadata = MetaData()

# Telegram users (anyone who has interacted with the bot)
t_users = Table("users", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("username", String(64), nullable=True),
    Column("first_name", String(64), nullable=False),
    Column("last_name", String(64), nullable=True),
    Column("timezone", String(64), nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

# Telegram groups/supergroups (tracked so we can list their members)
t_groups = Table("groups", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("title", String(256), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

# Telegram group membership (who has interacted in a group)
t_group_members = Table("group_members", metadata,
    Column("group_id", BigInteger, ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("joined_at", DateTime(timezone=True), server_default=func.now()),
)

# Named expense trips — the core organising unit
t_trips = Table("trips", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(256), nullable=False),
    Column("chat_id", BigInteger, nullable=False),
    Column("base_currency", String(8), nullable=False, default="SGD"),
    Column("created_by", BigInteger, ForeignKey("users.id"), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

# Participants in a trip (Telegram users OR virtual people)
t_trip_members = Table("trip_members", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("trip_id", Integer, ForeignKey("trips.id", ondelete="CASCADE"), nullable=False),
    Column("telegram_user_id", BigInteger, ForeignKey("users.id"), nullable=True),
    Column("display_name", String(128), nullable=False),
    Column("added_at", DateTime(timezone=True), server_default=func.now()),
)

# Expenses (owned by a trip; payer is a trip_member)
t_expenses = Table("expenses", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("trip_id", Integer, ForeignKey("trips.id", ondelete="CASCADE"), nullable=False),
    Column("paid_by_member", Integer, ForeignKey("trip_members.id"), nullable=False),
    Column("description", Text, nullable=False),
    Column("amount", Float, nullable=False),
    Column("currency", String(8), nullable=False),
    Column("amount_base", Float, nullable=False),
    Column("base_currency", String(8), nullable=False),
    Column("fx_rate", Float, nullable=False, default=1.0),
    Column("split_mode", String(16), nullable=False),
    Column("created_by", BigInteger, ForeignKey("users.id"), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

# Per-member share within an expense
t_shares = Table("expense_shares", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("expense_id", Integer, ForeignKey("expenses.id", ondelete="CASCADE"), nullable=False),
    Column("trip_member_id", Integer, ForeignKey("trip_members.id"), nullable=False),
    Column("share_amount", Float, nullable=False),
)

# Recorded settlements between trip members
t_settlements = Table("settlements", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("trip_id", Integer, ForeignKey("trips.id", ondelete="CASCADE"), nullable=False),
    Column("from_member_id", Integer, ForeignKey("trip_members.id"), nullable=False),
    Column("to_member_id", Integer, ForeignKey("trip_members.id"), nullable=False),
    Column("amount", Float, nullable=False),
    Column("currency", String(8), nullable=False),
    Column("note", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

# Per-chat settings (active trip selection)
t_chat_settings = Table("chat_settings", metadata,
    Column("chat_id", BigInteger, primary_key=True),
    Column("active_trip_id", Integer, nullable=True),
)

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

engine = create_async_engine(cfg.DATABASE_URL, echo=False)
_SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    logger.info("init_db: creating tables if needed")
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        # Migration: add timezone column if it doesn't exist yet
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN timezone VARCHAR(64) DEFAULT NULL"))
            logger.info("init_db: added timezone column to users")
        except Exception:
            pass  # column already exists — expected on all non-fresh deploys

        # One-time cleanup: delete trips produced by the name-corruption bug.
        # Pattern: trip name exactly matches the creator's display_name in trip_members.
        # We delete these regardless of whether they have expenses — the trip was
        # created by accident (a user typed their own name as the trip name).
        try:
            result = await conn.execute(text("""
                DELETE FROM trips
                WHERE id IN (
                    SELECT DISTINCT t.id
                    FROM trips t
                    JOIN trip_members tm
                      ON tm.trip_id = t.id
                     AND lower(tm.display_name) = lower(t.name)
                     AND tm.telegram_user_id = t.created_by
                )
            """))
            deleted = result.rowcount if result.rowcount is not None else 0
            if deleted:
                logger.info("init_db: removed %d corrupted trip(s) where name = creator display_name", deleted)
        except Exception as exc:
            logger.warning("init_db: corrupted-trip cleanup failed: %s", exc)

        # Repair chat_settings.active_trip_id after deletions.
        # If the active trip was deleted, point to the most recent remaining trip
        # so the chat stays usable (rather than leaving it NULL).
        try:
            await conn.execute(text("""
                UPDATE chat_settings
                SET active_trip_id = (
                    SELECT t.id FROM trips t
                    WHERE t.chat_id = chat_settings.chat_id
                    ORDER BY t.created_at DESC
                    LIMIT 1
                )
                WHERE active_trip_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM trips WHERE id = chat_settings.active_trip_id
                  )
            """))
        except Exception as exc:
            logger.warning("init_db: chat_settings repair failed: %s", exc)

    logger.info("init_db: done")


@asynccontextmanager
async def get_db():
    async with _SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# User & Telegram group helpers
# ---------------------------------------------------------------------------

async def upsert_user(
    session: AsyncSession,
    user_id: int,
    username: str | None,
    first_name: str,
    last_name: str | None,
) -> None:
    existing = await session.scalar(select(t_users.c.id).where(t_users.c.id == user_id))
    if existing is None:
        await session.execute(
            insert(t_users).values(
                id=user_id, username=username, first_name=first_name, last_name=last_name
            )
        )
        logger.info("upsert_user: created user=%s username=%r", user_id, username)
    else:
        await session.execute(
            update(t_users).where(t_users.c.id == user_id).values(
                username=username, first_name=first_name, last_name=last_name
            )
        )


async def upsert_group(session: AsyncSession, group_id: int, title: str) -> None:
    existing = await session.scalar(select(t_groups.c.id).where(t_groups.c.id == group_id))
    if existing is None:
        await session.execute(insert(t_groups).values(id=group_id, title=title))
        logger.info("upsert_group: created group=%s title=%r", group_id, title)
    else:
        await session.execute(update(t_groups).where(t_groups.c.id == group_id).values(title=title))


async def ensure_member(session: AsyncSession, group_id: int, user_id: int) -> None:
    existing = await session.scalar(
        select(t_group_members.c.user_id).where(
            t_group_members.c.group_id == group_id,
            t_group_members.c.user_id == user_id,
        )
    )
    if existing is None:
        await session.execute(insert(t_group_members).values(group_id=group_id, user_id=user_id))
        logger.debug("ensure_member: added user=%s to group=%s", user_id, group_id)


async def get_group_telegram_members(session: AsyncSession, group_id: int) -> list[dict]:
    """Return Telegram users who have interacted in this group."""
    rows = (
        await session.execute(
            select(t_users)
            .join(t_group_members, t_group_members.c.user_id == t_users.c.id)
            .where(t_group_members.c.group_id == group_id)
            .order_by(t_users.c.first_name)
        )
    ).mappings().fetchall()
    return [dict(r) for r in rows]


async def get_user(session: AsyncSession, user_id: int) -> dict | None:
    row = (await session.execute(select(t_users).where(t_users.c.id == user_id))).mappings().fetchone()
    return dict(row) if row else None


async def get_user_timezone(session: AsyncSession, user_id: int) -> str | None:
    return await session.scalar(
        select(t_users.c.timezone).where(t_users.c.id == user_id)
    )


async def set_user_timezone(session: AsyncSession, user_id: int, tz: str) -> None:
    await session.execute(
        update(t_users).where(t_users.c.id == user_id).values(timezone=tz)
    )
    logger.info("set_user_timezone: user=%s tz=%r", user_id, tz)


# ---------------------------------------------------------------------------
# Trip helpers
# ---------------------------------------------------------------------------

async def create_trip(
    session: AsyncSession,
    name: str,
    chat_id: int,
    base_currency: str,
    created_by: int,
) -> int:
    result = await session.execute(
        insert(t_trips).values(
            name=name, chat_id=chat_id, base_currency=base_currency, created_by=created_by
        )
    )
    trip_id: int = result.inserted_primary_key[0]
    logger.info("create_trip: name=%r chat=%s currency=%s → id=%s", name, chat_id, base_currency, trip_id)
    return trip_id


async def get_trip(session: AsyncSession, trip_id: int) -> dict | None:
    row = (await session.execute(select(t_trips).where(t_trips.c.id == trip_id))).mappings().fetchone()
    result = dict(row) if row else None
    logger.debug("get_trip: id=%s → name=%r chat=%s", trip_id,
                 result["name"] if result else None,
                 result["chat_id"] if result else None)
    return result


async def get_trips_in_chat(session: AsyncSession, chat_id: int) -> list[dict]:
    rows = (
        await session.execute(
            select(t_trips).where(t_trips.c.chat_id == chat_id).order_by(t_trips.c.created_at.desc())
        )
    ).mappings().fetchall()
    result = [dict(r) for r in rows]
    logger.debug("get_trips_in_chat: chat=%s → %d trips: %s",
                 chat_id, len(result), [(t["id"], t["name"]) for t in result])
    return result


async def set_trip_currency(session: AsyncSession, trip_id: int, currency: str) -> None:
    await session.execute(update(t_trips).where(t_trips.c.id == trip_id).values(base_currency=currency))
    logger.info("set_trip_currency: trip=%s currency=%s", trip_id, currency)


async def delete_trip(session: AsyncSession, trip_id: int, requestor_id: int) -> bool:
    """Delete a trip only if the requestor is the creator. Use delete_trip_by_id for admin ops."""
    trip = await get_trip(session, trip_id)
    if not trip or trip["created_by"] != requestor_id:
        return False
    await session.execute(t_trips.delete().where(t_trips.c.id == trip_id))
    logger.info("delete_trip: trip=%s by user=%s", trip_id, requestor_id)
    return True


# ---------------------------------------------------------------------------
# Trip member helpers
# ---------------------------------------------------------------------------

async def add_trip_member(
    session: AsyncSession,
    trip_id: int,
    display_name: str,
    telegram_user_id: int | None = None,
) -> int:
    result = await session.execute(
        insert(t_trip_members).values(
            trip_id=trip_id,
            display_name=display_name,
            telegram_user_id=telegram_user_id,
        )
    )
    member_id: int = result.inserted_primary_key[0]
    logger.info("add_trip_member: trip=%s name=%r tg_user=%s → member_id=%s",
                trip_id, display_name, telegram_user_id, member_id)
    return member_id


async def get_trip_members(session: AsyncSession, trip_id: int) -> list[dict]:
    rows = (
        await session.execute(
            select(t_trip_members)
            .where(t_trip_members.c.trip_id == trip_id)
            .order_by(t_trip_members.c.id)
        )
    ).mappings().fetchall()
    return [dict(r) for r in rows]


async def get_trip_member_by_telegram_id(
    session: AsyncSession, trip_id: int, telegram_user_id: int
) -> dict | None:
    row = (
        await session.execute(
            select(t_trip_members).where(
                t_trip_members.c.trip_id == trip_id,
                t_trip_members.c.telegram_user_id == telegram_user_id,
            )
        )
    ).mappings().fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Expense operations
# ---------------------------------------------------------------------------

async def create_expense(
    session: AsyncSession,
    trip_id: int,
    paid_by_member: int,
    description: str,
    amount: float,
    currency: str,
    amount_base: float,
    base_currency: str,
    fx_rate: float,
    split_mode: str,
    created_by: int,
    shares: dict[int, float],
) -> int:
    result = await session.execute(
        insert(t_expenses).values(
            trip_id=trip_id,
            paid_by_member=paid_by_member,
            description=description,
            amount=amount,
            currency=currency,
            amount_base=amount_base,
            base_currency=base_currency,
            fx_rate=fx_rate,
            split_mode=split_mode,
            created_by=created_by,
        )
    )
    expense_id: int = result.inserted_primary_key[0]

    for member_id, share_amount in shares.items():
        await session.execute(
            insert(t_shares).values(
                expense_id=expense_id, trip_member_id=member_id, share_amount=share_amount
            )
        )

    logger.info(
        "create_expense: id=%s trip=%s payer=%s desc=%r amount=%s %s (base=%s %s) split=%s",
        expense_id, trip_id, paid_by_member, description,
        amount, currency, amount_base, base_currency, split_mode,
    )
    return expense_id


async def get_expense_history(
    session: AsyncSession, trip_id: int, limit: int = 10, offset: int = 0
) -> list[dict]:
    rows = (
        await session.execute(
            select(
                t_expenses,
                t_trip_members.c.display_name.label("payer_name"),
            )
            .outerjoin(t_trip_members, t_trip_members.c.id == t_expenses.c.paid_by_member)
            .where(t_expenses.c.trip_id == trip_id)
            .order_by(t_expenses.c.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).mappings().fetchall()
    result = []
    for r in rows:
        d = dict(r)
        if not d.get("payer_name"):
            d["payer_name"] = "Unknown"
        result.append(d)
    return result


async def count_expenses(session: AsyncSession, trip_id: int) -> int:
    return (
        await session.scalar(
            select(func.count()).select_from(t_expenses).where(t_expenses.c.trip_id == trip_id)
        )
    ) or 0


async def get_trip_currencies(session: AsyncSession, trip_id: int) -> list[str]:
    """Return distinct currencies used in a trip, most recently used first."""
    rows = (
        await session.execute(
            select(t_expenses.c.currency)
            .where(t_expenses.c.trip_id == trip_id)
            .order_by(t_expenses.c.created_at.desc())
        )
    ).fetchall()
    seen: set[str] = set()
    result: list[str] = []
    for (currency,) in rows:
        if currency not in seen:
            seen.add(currency)
            result.append(currency)
    return result


# ---------------------------------------------------------------------------
# Balance calculation
# ---------------------------------------------------------------------------

_NET_BALANCE_SQL = """
SELECT
    tm.id,
    tm.display_name,
    tm.telegram_user_id,
    u.username,
    COALESCE(paid.total, 0)
        - COALESCE(owed.total, 0)
        - COALESCE(recv.total, 0)
        + COALESCE(sent.total, 0) AS net
FROM trip_members tm
LEFT JOIN users u ON u.id = tm.telegram_user_id
LEFT JOIN (
    SELECT paid_by_member, SUM(amount_base) AS total
    FROM expenses WHERE trip_id = :tid GROUP BY paid_by_member
) paid ON paid.paid_by_member = tm.id
LEFT JOIN (
    SELECT es.trip_member_id, SUM(es.share_amount) AS total
    FROM expense_shares es
    JOIN expenses e ON e.id = es.expense_id AND e.trip_id = :tid
    GROUP BY es.trip_member_id
) owed ON owed.trip_member_id = tm.id
LEFT JOIN (
    SELECT to_member_id, SUM(amount) AS total
    FROM settlements WHERE trip_id = :tid GROUP BY to_member_id
) recv ON recv.to_member_id = tm.id
LEFT JOIN (
    SELECT from_member_id, SUM(amount) AS total
    FROM settlements WHERE trip_id = :tid GROUP BY from_member_id
) sent ON sent.from_member_id = tm.id
WHERE tm.trip_id = :tid
ORDER BY net DESC
"""


async def get_net_balances(session: AsyncSession, trip_id: int) -> list[dict]:
    rows = (await session.execute(text(_NET_BALANCE_SQL), {"tid": trip_id})).mappings().fetchall()
    return [dict(r) for r in rows]


async def rename_trip(session: AsyncSession, trip_id: int, name: str) -> None:
    await session.execute(update(t_trips).where(t_trips.c.id == trip_id).values(name=name))
    logger.info("rename_trip: trip=%s new_name=%r", trip_id, name)


async def get_expense_shares(session: AsyncSession, expense_id: int) -> list[dict]:
    """Return per-member shares for an expense, ordered by member id."""
    rows = (
        await session.execute(
            select(
                t_shares.c.share_amount,
                t_trip_members.c.display_name,
                t_trip_members.c.id.label("member_id"),
            )
            .outerjoin(t_trip_members, t_trip_members.c.id == t_shares.c.trip_member_id)
            .where(t_shares.c.expense_id == expense_id)
            .order_by(t_trip_members.c.id)
        )
    ).mappings().fetchall()
    result = []
    for r in rows:
        d = dict(r)
        if not d.get("display_name"):
            d["display_name"] = "Unknown"
        result.append(d)
    return result


async def get_expense_by_id(session: AsyncSession, expense_id: int) -> dict | None:
    row = (
        await session.execute(
            select(t_expenses, t_trip_members.c.display_name.label("payer_name"))
            .outerjoin(t_trip_members, t_trip_members.c.id == t_expenses.c.paid_by_member)
            .where(t_expenses.c.id == expense_id)
        )
    ).mappings().fetchone()
    if row is None:
        return None
    d = dict(row)
    if not d.get("payer_name"):
        d["payer_name"] = "Unknown"
    return d


async def delete_expense(session: AsyncSession, expense_id: int) -> None:
    await session.execute(t_shares.delete().where(t_shares.c.expense_id == expense_id))
    await session.execute(t_expenses.delete().where(t_expenses.c.id == expense_id))
    logger.info("delete_expense: expense=%s", expense_id)


async def update_expense_description(session: AsyncSession, expense_id: int, description: str) -> None:
    await session.execute(
        update(t_expenses).where(t_expenses.c.id == expense_id).values(description=description)
    )
    logger.info("update_expense_description: expense=%s desc=%r", expense_id, description)


async def clear_trip_expenses(session: AsyncSession, trip_id: int) -> None:
    """Delete all expenses, shares, and settlements for a trip. Members are kept."""
    expense_ids = select(t_expenses.c.id).where(t_expenses.c.trip_id == trip_id).scalar_subquery()
    await session.execute(t_shares.delete().where(t_shares.c.expense_id.in_(expense_ids)))
    await session.execute(t_expenses.delete().where(t_expenses.c.trip_id == trip_id))
    await session.execute(t_settlements.delete().where(t_settlements.c.trip_id == trip_id))
    logger.info("clear_trip_expenses: trip=%s", trip_id)


async def delete_trip_by_id(session: AsyncSession, trip_id: int) -> None:
    """Delete a trip and everything belonging to it. No creator check — caller is responsible."""
    expense_ids = select(t_expenses.c.id).where(t_expenses.c.trip_id == trip_id).scalar_subquery()
    await session.execute(t_shares.delete().where(t_shares.c.expense_id.in_(expense_ids)))
    await session.execute(t_expenses.delete().where(t_expenses.c.trip_id == trip_id))
    await session.execute(t_settlements.delete().where(t_settlements.c.trip_id == trip_id))
    await session.execute(t_trip_members.delete().where(t_trip_members.c.trip_id == trip_id))
    await session.execute(t_trips.delete().where(t_trips.c.id == trip_id))
    logger.info("delete_trip_by_id: trip=%s", trip_id)


async def get_member_expense_shares(
    session: AsyncSession, trip_id: int, member_id: int
) -> list[dict]:
    """Expenses in a trip where member_id has a share but did NOT pay — ordered newest first."""
    rows = (
        await session.execute(
            select(
                t_expenses.c.id,
                t_expenses.c.description,
                t_expenses.c.base_currency,
                t_shares.c.share_amount,
                t_trip_members.c.display_name.label("payer_name"),
            )
            .join(t_shares, t_shares.c.expense_id == t_expenses.c.id)
            .join(t_trip_members, t_trip_members.c.id == t_expenses.c.paid_by_member)
            .where(
                t_expenses.c.trip_id == trip_id,
                t_shares.c.trip_member_id == member_id,
                t_expenses.c.paid_by_member != member_id,
            )
            .order_by(t_expenses.c.created_at.desc())
        )
    ).mappings().fetchall()
    return [dict(r) for r in rows]


_PAIRWISE_DEBTS_SQL = """
SELECT
    s.trip_member_id AS debtor_id,
    e.paid_by_member AS creditor_id,
    tm_d.display_name AS debtor_name,
    tm_d.telegram_user_id AS debtor_telegram_id,
    tm_c.display_name AS creditor_name,
    SUM(s.share_amount) - COALESCE(MAX(stl.total), 0) AS net_owed
FROM expense_shares s
JOIN expenses e ON e.id = s.expense_id AND e.trip_id = :tid
JOIN trip_members tm_d ON tm_d.id = s.trip_member_id
JOIN trip_members tm_c ON tm_c.id = e.paid_by_member
LEFT JOIN (
    SELECT from_member_id, to_member_id, SUM(amount) AS total
    FROM settlements WHERE trip_id = :tid
    GROUP BY from_member_id, to_member_id
) stl ON stl.from_member_id = s.trip_member_id AND stl.to_member_id = e.paid_by_member
WHERE s.trip_member_id != e.paid_by_member
GROUP BY s.trip_member_id, e.paid_by_member
HAVING SUM(s.share_amount) - COALESCE(MAX(stl.total), 0) > 0.005
ORDER BY SUM(s.share_amount) - COALESCE(MAX(stl.total), 0) DESC
"""


async def get_pairwise_debts(session: AsyncSession, trip_id: int) -> list[dict]:
    rows = (await session.execute(text(_PAIRWISE_DEBTS_SQL), {"tid": trip_id})).mappings().fetchall()
    return [dict(r) for r in rows]


async def get_debtor_expense_breakdown(
    session: AsyncSession, trip_id: int, debtor_id: int, creditor_id: int
) -> list[dict]:
    """Expenses in trip paid by creditor_id where debtor_id has a share."""
    rows = (
        await session.execute(
            select(
                t_expenses.c.id,
                t_expenses.c.description,
                t_expenses.c.base_currency,
                t_shares.c.share_amount,
            )
            .join(t_shares, t_shares.c.expense_id == t_expenses.c.id)
            .where(
                t_expenses.c.trip_id == trip_id,
                t_expenses.c.paid_by_member == creditor_id,
                t_shares.c.trip_member_id == debtor_id,
            )
            .order_by(t_expenses.c.created_at.desc())
        )
    ).mappings().fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Chat settings helpers
# ---------------------------------------------------------------------------

async def get_active_trip_id(session: AsyncSession, chat_id: int) -> int | None:
    return await session.scalar(
        select(t_chat_settings.c.active_trip_id).where(t_chat_settings.c.chat_id == chat_id)
    )


async def set_active_trip_id(session: AsyncSession, chat_id: int, trip_id: int | None) -> None:
    existing = await session.scalar(
        select(t_chat_settings.c.chat_id).where(t_chat_settings.c.chat_id == chat_id)
    )
    if existing is None:
        await session.execute(insert(t_chat_settings).values(chat_id=chat_id, active_trip_id=trip_id))
    else:
        await session.execute(
            update(t_chat_settings).where(t_chat_settings.c.chat_id == chat_id).values(active_trip_id=trip_id)
        )
    logger.debug("set_active_trip_id: chat=%s trip=%s", chat_id, trip_id)


async def get_member_expense_count(session: AsyncSession, member_id: int) -> int:
    paid = await session.scalar(
        select(func.count()).select_from(t_expenses).where(t_expenses.c.paid_by_member == member_id)
    ) or 0
    shared = await session.scalar(
        select(func.count()).select_from(t_shares).where(t_shares.c.trip_member_id == member_id)
    ) or 0
    return paid + shared


async def remove_trip_member(session: AsyncSession, member_id: int) -> bool:
    if await get_member_expense_count(session, member_id) > 0:
        return False
    await session.execute(t_trip_members.delete().where(t_trip_members.c.id == member_id))
    logger.info("remove_trip_member: member=%s", member_id)
    return True


# ---------------------------------------------------------------------------
# Settlement operations
# ---------------------------------------------------------------------------

async def create_settlement(
    session: AsyncSession,
    trip_id: int,
    from_member_id: int,
    to_member_id: int,
    amount: float,
    currency: str,
    note: str | None = None,
) -> int:
    result = await session.execute(
        insert(t_settlements).values(
            trip_id=trip_id,
            from_member_id=from_member_id,
            to_member_id=to_member_id,
            amount=amount,
            currency=currency,
            note=note,
        )
    )
    settlement_id: int = result.inserted_primary_key[0]
    logger.info(
        "create_settlement: id=%s trip=%s from=%s to=%s amount=%s %s",
        settlement_id, trip_id, from_member_id, to_member_id, amount, currency,
    )
    return settlement_id
