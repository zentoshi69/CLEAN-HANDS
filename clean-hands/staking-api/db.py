#!/usr/bin/env python3
"""SQLite store for the staking API. The source of truth that both the website
and the Telegram Mini App read from."""

from __future__ import annotations

import os
import time
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("STAKE_DB", os.path.join(os.path.dirname(__file__), "staking.db"))

# --------------------------------------------------------------------------- #
#  MONEY UNITS — everything is stored & accrued in INTEGER base units          #
#  (10^decimals), never floats, to eliminate rounding drift. Convert to a      #
#  human-readable amount only at the API boundary.                             #
# --------------------------------------------------------------------------- #
DECIMALS = int(os.environ.get("DEFAULT_TOKEN_DECIMALS", "6"))
BASE = 10**DECIMALS
SCHEMA_VERSION = 4  # bumped by migrations


def to_base(ui_amount: float) -> int:
    """Human token amount -> integer base units (rounded to nearest unit)."""
    return int(round(float(ui_amount) * BASE))


def to_ui(base_amount) -> float:
    """Integer base units -> human token amount (for API responses)."""
    return (int(base_amount) if base_amount is not None else 0) / BASE


# --------------------------------------------------------------------------- #
#  DIALECT — SQLite by default (single node), Postgres when DATABASE_URL is set #
#  (horizontal scale). The shim keeps the SQLite path byte-identical.          #
# --------------------------------------------------------------------------- #
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DIALECT = "pg" if DATABASE_URL.startswith(("postgres://", "postgresql://")) else "sqlite"


def dialect() -> str:
    return DIALECT


def _translate(sql: str) -> str:
    """Rewrite our SQLite-flavoured SQL for Postgres. Params stay bound (no
    interpolation), so this introduces no injection surface."""
    s = sql
    if "INSERT OR IGNORE INTO" in s:
        s = s.replace("INSERT OR IGNORE INTO", "INSERT INTO").rstrip().rstrip(";")
        s += " ON CONFLICT DO NOTHING"
    return s.replace("?", "%s")


class _PgConn:
    """Thin wrapper so app/db code written for sqlite3 (`?` params, `.execute`,
    `.commit`, cursor `.rowcount/.fetchone/.fetchall`) runs unchanged on psycopg3."""

    def __init__(self, raw):
        self._c = raw

    def execute(self, sql, params=()):
        return self._c.execute(_translate(sql), params)

    def commit(self):
        self._c.commit()

    def rollback(self):
        self._c.rollback()

    def close(self):
        self._c.close()


@contextmanager
def db():
    if DIALECT == "pg":
        import psycopg
        from psycopg.rows import dict_row

        raw = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        try:
            yield _PgConn(raw)
        finally:
            raw.close()
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()


# Postgres DDL — money columns are BIGINT base units (built on the P3.1 shape, so
# no float→int migration is ever needed on PG; it starts at the final schema).
_PG_DDL = """
CREATE TABLE IF NOT EXISTS stakers (
    wallet TEXT PRIMARY KEY, tg_id BIGINT UNIQUE, username TEXT,
    recorded_staked BIGINT NOT NULL DEFAULT 0, cached_balance BIGINT NOT NULL DEFAULT 0,
    balance_ts BIGINT NOT NULL DEFAULT 0, stake_start_ts BIGINT NOT NULL DEFAULT 0,
    last_accrual_ts BIGINT NOT NULL DEFAULT 0, accrued BIGINT NOT NULL DEFAULT 0,
    claimed_total BIGINT NOT NULL DEFAULT 0, total_burned BIGINT NOT NULL DEFAULT 0,
    referred_by TEXT, ref_code TEXT UNIQUE, created_at BIGINT NOT NULL);
ALTER TABLE stakers ADD COLUMN IF NOT EXISTS ref_code TEXT UNIQUE;
CREATE TABLE IF NOT EXISTS burns (
    signature TEXT PRIMARY KEY, wallet TEXT NOT NULL, amount BIGINT NOT NULL, ts BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger (
    id BIGSERIAL PRIMARY KEY, ts BIGINT NOT NULL, wallet TEXT NOT NULL,
    action TEXT NOT NULL, amount BIGINT NOT NULL, detail TEXT);
CREATE INDEX IF NOT EXISTS idx_stakers_staked ON stakers(recorded_staked DESC);
CREATE INDEX IF NOT EXISTS idx_stakers_ref ON stakers(referred_by);
CREATE INDEX IF NOT EXISTS idx_ledger_wallet ON ledger(wallet, ts);
CREATE TABLE IF NOT EXISTS claims (
    id BIGSERIAL PRIMARY KEY, wallet TEXT NOT NULL, amount BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested', tx_sig TEXT,
    created_at BIGINT NOT NULL, paid_at BIGINT);
CREATE INDEX IF NOT EXISTS idx_claims_wallet ON claims(wallet, created_at);
CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
CREATE TABLE IF NOT EXISTS notifs (
    wallet TEXT NOT NULL, kind TEXT NOT NULL, last_ts BIGINT NOT NULL,
    PRIMARY KEY (wallet, kind));
"""


def init_db():
    if DIALECT == "pg":
        with db() as conn:
            for stmt in _PG_DDL.split(";"):
                if stmt.strip():
                    conn.execute(stmt)
            conn.commit()
        return
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS stakers (
                wallet          TEXT PRIMARY KEY,
                tg_id           INTEGER UNIQUE,
                username        TEXT,
                recorded_staked REAL NOT NULL DEFAULT 0,   -- tokens enrolled
                cached_balance  REAL NOT NULL DEFAULT 0,   -- last on-chain balance
                balance_ts      INTEGER NOT NULL DEFAULT 0,
                stake_start_ts  INTEGER NOT NULL DEFAULT 0,
                last_accrual_ts INTEGER NOT NULL DEFAULT 0,
                accrued         REAL NOT NULL DEFAULT 0,    -- claimable rewards
                claimed_total   REAL NOT NULL DEFAULT 0,
                total_burned    REAL NOT NULL DEFAULT 0,    -- lifetime $CLEAN burned
                referred_by     TEXT,
                created_at      INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS burns (
                signature  TEXT PRIMARY KEY,                -- idempotency: 1 credit per tx
                wallet     TEXT NOT NULL,
                amount     REAL NOT NULL,
                ts         INTEGER NOT NULL
            );
            -- Append-only audit trail of every balance-affecting action. Never
            -- UPDATE or DELETE rows here — it's the forensic / reconciliation log.
            CREATE TABLE IF NOT EXISTS ledger (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      INTEGER NOT NULL,
                wallet  TEXT NOT NULL,
                action  TEXT NOT NULL,                      -- stake|unstake|claim|burn
                amount  REAL NOT NULL,
                detail  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_stakers_staked ON stakers(recorded_staked DESC);
            CREATE INDEX IF NOT EXISTS idx_stakers_ref ON stakers(referred_by);
            CREATE INDEX IF NOT EXISTS idx_ledger_wallet ON ledger(wallet, ts);
            -- Claim requests: an idempotent payout state machine. In manual mode a
            -- claim is 'requested' and an operator/cron marks it 'paid' with a tx.
            CREATE TABLE IF NOT EXISTS claims (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet     TEXT NOT NULL,
                amount     INTEGER NOT NULL,              -- base units
                status     TEXT NOT NULL DEFAULT 'requested',  -- requested|paid|failed
                tx_sig     TEXT,
                created_at INTEGER NOT NULL,
                paid_at    INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_claims_wallet ON claims(wallet, created_at);
            CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
            CREATE TABLE IF NOT EXISTS notifs (
                wallet  TEXT NOT NULL,
                kind    TEXT NOT NULL,
                last_ts INTEGER NOT NULL,
                PRIMARY KEY (wallet, kind)
            );
            """
        )
        conn.commit()
        _migrate(conn)


# Money columns that hold token amounts (migrated REAL tokens -> int base units).
_MONEY_COLS = {
    "stakers": ["recorded_staked", "cached_balance", "accrued", "claimed_total", "total_burned"],
    "burns": ["amount"],
    "ledger": ["amount"],
}


def _migrate(conn) -> None:
    """Additive, idempotent migrations guarded by PRAGMA user_version. Never drops
    or recreates tables — preserves all existing rows."""
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    if ver < 1:
        # v1: token amounts move from REAL tokens to INTEGER base units. Multiply
        # existing values in place (SQLite columns are dynamically typed, so no
        # table rebuild / cascade is needed).
        for table, cols in _MONEY_COLS.items():
            for col in cols:
                conn.execute(
                    f"UPDATE {table} SET {col} = CAST(ROUND({col} * ?) AS INTEGER) "
                    f"WHERE {col} IS NOT NULL",
                    (BASE,),
                )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        ver = 1
    if ver < 2:
        # v2: claims payout state machine (idempotent). CREATE IF NOT EXISTS so
        # it's safe whether or not init's executescript already made it.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT, wallet TEXT NOT NULL,
                amount INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'requested',
                tx_sig TEXT, created_at INTEGER NOT NULL, paid_at INTEGER);
            CREATE INDEX IF NOT EXISTS idx_claims_wallet ON claims(wallet, created_at);
            CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
            """
        )
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        ver = 2
    if ver < 3:
        # v3: push-notification dedupe table.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS notifs (
                wallet TEXT NOT NULL, kind TEXT NOT NULL, last_ts INTEGER NOT NULL,
                PRIMARY KEY (wallet, kind));
            """
        )
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
        ver = 3
    if ver < 4:
        # v4: short shareable referral codes (lazily generated per staker).
        conn.execute("ALTER TABLE stakers ADD COLUMN ref_code TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_stakers_refcode ON stakers(ref_code)")
        conn.execute("PRAGMA user_version = 4")
        conn.commit()


def get_staker(conn, wallet: str):
    return conn.execute("SELECT * FROM stakers WHERE wallet=?", (wallet,)).fetchone()


def get_staker_by_tg(conn, tg_id: int):
    return conn.execute("SELECT * FROM stakers WHERE tg_id=?", (tg_id,)).fetchone()


def record(conn, wallet: str, action: str, amount_base: int, detail: str = "") -> None:
    """Append a money event (amount in integer base units) to the audit ledger."""
    conn.execute(
        "INSERT INTO ledger (ts, wallet, action, amount, detail) VALUES (?,?,?,?,?)",
        (int(time.time()), wallet, action, int(amount_base), detail),
    )
    conn.commit()


def upsert_staker(conn, wallet: str, tg_id=None, username=None, referred_by=None):
    now = int(time.time())
    row = get_staker(conn, wallet)
    if row:
        if tg_id is not None or username is not None:
            conn.execute(
                "UPDATE stakers SET tg_id=COALESCE(?,tg_id), username=COALESCE(?,username) WHERE wallet=?",
                (tg_id, username, wallet),
            )
            conn.commit()
        return get_staker(conn, wallet)
    conn.execute(
        "INSERT INTO stakers (wallet, tg_id, username, referred_by, created_at, last_accrual_ts) "
        "VALUES (?,?,?,?,?,?)",
        (wallet, tg_id, username, referred_by, now, now),
    )
    conn.commit()
    return get_staker(conn, wallet)


def active_referrals(conn, wallet: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM stakers WHERE referred_by=? AND recorded_staked > 0",
        (wallet,),
    ).fetchone()["n"]


# No 0/O/1/I/L — codes survive being read aloud or hand-typed.
_REF_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def ref_code(conn, wallet: str) -> str | None:
    """The staker's short shareable referral code, generated lazily once."""
    import secrets as _secrets

    row = conn.execute("SELECT ref_code FROM stakers WHERE wallet=?", (wallet,)).fetchone()
    if row is None:
        return None
    if row["ref_code"]:
        return row["ref_code"]
    for _ in range(8):  # UNIQUE collision at 6 chars is ~1e-9; retry regardless
        code = "".join(_secrets.choice(_REF_ALPHABET) for _ in range(6))
        try:
            conn.execute(
                "UPDATE stakers SET ref_code=? WHERE wallet=? AND ref_code IS NULL",
                (code, wallet),
            )
            conn.commit()
        except Exception:  # noqa: BLE001 — unique race with another worker
            conn.rollback()
            continue
        row = conn.execute("SELECT ref_code FROM stakers WHERE wallet=?", (wallet,)).fetchone()
        if row and row["ref_code"]:
            return row["ref_code"]
    return None


def wallet_by_ref_code(conn, code: str) -> str | None:
    row = conn.execute("SELECT wallet FROM stakers WHERE ref_code=?", (code,)).fetchone()
    return row["wallet"] if row else None


def burn_seen(conn, signature: str) -> bool:
    return conn.execute("SELECT 1 FROM burns WHERE signature=?", (signature,)).fetchone() is not None


def create_claim(conn, wallet: str, amount_base: int, status: str = "requested") -> None:
    conn.execute(
        "INSERT INTO claims (wallet, amount, status, created_at) VALUES (?,?,?,?)",
        (wallet, int(amount_base), status, int(time.time())),
    )
    conn.commit()


def list_pending_claims(conn, limit: int = 200):
    return conn.execute(
        "SELECT * FROM claims WHERE status='requested' ORDER BY created_at LIMIT ?", (limit,)
    ).fetchall()


def notif_last(conn, wallet: str, kind: str) -> int:
    row = conn.execute("SELECT last_ts FROM notifs WHERE wallet=? AND kind=?", (wallet, kind)).fetchone()
    return row["last_ts"] if row else 0


def notif_mark(conn, wallet: str, kind: str, ts: int) -> None:
    # delete+insert is portable across SQLite and Postgres (no dialect upsert)
    conn.execute("DELETE FROM notifs WHERE wallet=? AND kind=?", (wallet, kind))
    conn.execute(
        "INSERT INTO notifs (wallet, kind, last_ts) VALUES (?,?,?)", (wallet, kind, int(ts))
    )
    conn.commit()


def stakers_with_tg(conn):
    return conn.execute(
        "SELECT * FROM stakers WHERE tg_id IS NOT NULL AND recorded_staked > 0"
    ).fetchall()


def mark_claim_paid(conn, claim_id: int, tx_sig: str) -> int:
    """Idempotent: only a still-'requested' claim transitions to 'paid'."""
    cur = conn.execute(
        "UPDATE claims SET status='paid', tx_sig=?, paid_at=? WHERE id=? AND status='requested'",
        (tx_sig, int(time.time()), claim_id),
    )
    conn.commit()
    return cur.rowcount
