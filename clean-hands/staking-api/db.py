#!/usr/bin/env python3
"""SQLite store for the staking API. The source of truth that both the website
and the Telegram Mini App read from."""

from __future__ import annotations

import os
import time
import math
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("STAKE_DB", os.path.join(os.path.dirname(__file__), "staking.db"))

# The ledger IS the money — keep it private (0600) so no other local account or
# co-tenant process can read who's owed payouts. Best-effort, runs once/process.
_DB_PERMS_SET = False


def _harden_db_perms() -> None:
    global _DB_PERMS_SET
    if _DB_PERMS_SET:
        return
    _DB_PERMS_SET = True
    for ext in ("", "-wal", "-shm"):
        try:
            os.chmod(DB_PATH + ext, 0o600)
        except OSError:
            pass

# --------------------------------------------------------------------------- #
#  MONEY UNITS — everything is stored & accrued in INTEGER base units          #
#  (10^decimals), never floats, to eliminate rounding drift. Convert to a      #
#  human-readable amount only at the API boundary.                             #
# --------------------------------------------------------------------------- #
DECIMALS = int(os.environ.get("DEFAULT_TOKEN_DECIMALS", "6"))
BASE = 10**DECIMALS
SCHEMA_VERSION = 12  # bumped by migrations (v12: social gate for Escape rewards)


def to_base(ui_amount: float) -> int:
    """Human token amount -> integer base units (rounded to nearest unit).
    Rejects non-finite / negative inputs (e.g. a malformed or hostile RPC
    balance) as 0 so NaN/Inf can never pollute the ledger."""
    f = float(ui_amount)
    if not math.isfinite(f) or f < 0:
        return 0
    return int(round(f * BASE))


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
        # Money ledger: fsync the WAL on every commit so an acknowledged claim/
        # burn/stake survives a host crash or hard reboot (the WAL default is
        # NORMAL, which can drop the last committed transactions on power loss).
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        # Concurrent writers serialize under WAL; wait for the lock (up to 5s)
        # instead of raising SQLITE_BUSY and 500-ing a money request.
        conn.execute("PRAGMA busy_timeout=5000")
        _harden_db_perms()
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
    mm_liquidity_cents BIGINT NOT NULL DEFAULT 0, mm_vip BIGINT NOT NULL DEFAULT 0,
    referred_by TEXT, ref_code TEXT UNIQUE, payout_wallet TEXT,
    payout_confirmed_ts BIGINT NOT NULL DEFAULT 0, created_at BIGINT NOT NULL);
ALTER TABLE stakers ADD COLUMN IF NOT EXISTS ref_code TEXT UNIQUE;
ALTER TABLE stakers ADD COLUMN IF NOT EXISTS payout_wallet TEXT;
ALTER TABLE stakers ADD COLUMN IF NOT EXISTS payout_confirmed_ts BIGINT NOT NULL DEFAULT 0;
ALTER TABLE stakers ADD COLUMN IF NOT EXISTS mm_liquidity_cents BIGINT NOT NULL DEFAULT 0;
ALTER TABLE stakers ADD COLUMN IF NOT EXISTS mm_vip BIGINT NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS burns (
    signature TEXT PRIMARY KEY, wallet TEXT NOT NULL, amount BIGINT NOT NULL, ts BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS mm_deposits (
    signature TEXT PRIMARY KEY, wallet TEXT NOT NULL, usd_cents BIGINT NOT NULL,
    lamports BIGINT NOT NULL DEFAULT 0, clean_base BIGINT NOT NULL DEFAULT 0, ts BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger (
    id BIGSERIAL PRIMARY KEY, ts BIGINT NOT NULL, wallet TEXT NOT NULL,
    action TEXT NOT NULL, amount BIGINT NOT NULL, detail TEXT);
CREATE INDEX IF NOT EXISTS idx_stakers_staked ON stakers(recorded_staked DESC);
CREATE INDEX IF NOT EXISTS idx_stakers_ref ON stakers(referred_by);
CREATE INDEX IF NOT EXISTS idx_ledger_wallet ON ledger(wallet, ts);
CREATE TABLE IF NOT EXISTS claims (
    id BIGSERIAL PRIMARY KEY, wallet TEXT NOT NULL, amount BIGINT NOT NULL,
    gross_amount BIGINT NOT NULL DEFAULT 0, fee_amount BIGINT NOT NULL DEFAULT 0,
    fee_usd DOUBLE PRECISION NOT NULL DEFAULT 0, destination TEXT,
    rules_version TEXT NOT NULL DEFAULT 'v1',
    status TEXT NOT NULL DEFAULT 'requested', tx_sig TEXT,
    created_at BIGINT NOT NULL, paid_at BIGINT);
ALTER TABLE claims ADD COLUMN IF NOT EXISTS gross_amount BIGINT NOT NULL DEFAULT 0;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS fee_amount BIGINT NOT NULL DEFAULT 0;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS fee_usd DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS destination TEXT;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS rules_version TEXT NOT NULL DEFAULT 'v1';
CREATE INDEX IF NOT EXISTS idx_claims_wallet ON claims(wallet, created_at);
CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_tx_sig_unique ON claims(tx_sig) WHERE tx_sig IS NOT NULL;
CREATE TABLE IF NOT EXISTS notifs (
    wallet TEXT NOT NULL, kind TEXT NOT NULL, last_ts BIGINT NOT NULL,
    PRIMARY KEY (wallet, kind));
CREATE TABLE IF NOT EXISTS ops_flags (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS wallet_links (
    wallet TEXT PRIMARY KEY, owner TEXT NOT NULL, ts BIGINT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_links_owner ON wallet_links(owner);
CREATE TABLE IF NOT EXISTS game_state (
    player TEXT PRIMARY KEY, name TEXT, state TEXT NOT NULL,
    score BIGINT NOT NULL DEFAULT 0, updated_ts BIGINT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_game_score ON game_state(score DESC);
CREATE TABLE IF NOT EXISTS game_verification (
    player TEXT PRIMARY KEY,
    verified_escape_score DOUBLE PRECISION NOT NULL DEFAULT 0,
    verified_prestige BIGINT NOT NULL DEFAULT 0,
    raw_escape_score DOUBLE PRECISION NOT NULL DEFAULT 0,
    raw_prestige BIGINT NOT NULL DEFAULT 0,
    first_seen_ts BIGINT NOT NULL DEFAULT 0,
    last_save_ts BIGINT NOT NULL DEFAULT 0,
    active_seconds BIGINT NOT NULL DEFAULT 0,
    save_count BIGINT NOT NULL DEFAULT 0,
    risk_score BIGINT NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'unverified',
    hold_until_ts BIGINT NOT NULL DEFAULT 0,
    session_id TEXT,
    last_seq BIGINT NOT NULL DEFAULT 0,
    reason TEXT,
    updated_ts BIGINT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_game_verify_status ON game_verification(status, risk_score);
CREATE TABLE IF NOT EXISTS game_verify_events (
    id BIGSERIAL PRIMARY KEY, ts BIGINT NOT NULL, player TEXT NOT NULL,
    kind TEXT NOT NULL, raw_escape_score DOUBLE PRECISION NOT NULL DEFAULT 0,
    verified_escape_score DOUBLE PRECISION NOT NULL DEFAULT 0,
    risk_score BIGINT NOT NULL DEFAULT 0, detail TEXT);
CREATE INDEX IF NOT EXISTS idx_game_verify_events_player ON game_verify_events(player, ts);
CREATE TABLE IF NOT EXISTS social_verifications (
    wallet TEXT NOT NULL,
    platform TEXT NOT NULL,
    handle TEXT,
    verified BIGINT NOT NULL DEFAULT 0,
    method TEXT,
    proof TEXT,
    status TEXT NOT NULL DEFAULT 'missing',
    verified_at BIGINT NOT NULL DEFAULT 0,
    updated_at BIGINT NOT NULL,
    PRIMARY KEY (wallet, platform));
CREATE INDEX IF NOT EXISTS idx_social_status ON social_verifications(platform, status);
CREATE TABLE IF NOT EXISTS social_verify_events (
    id BIGSERIAL PRIMARY KEY,
    ts BIGINT NOT NULL,
    wallet TEXT NOT NULL,
    platform TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT);
CREATE INDEX IF NOT EXISTS idx_social_events_wallet ON social_verify_events(wallet, ts);
CREATE TABLE IF NOT EXISTS bridge_orders (
    order_id TEXT PRIMARY KEY, created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL,
    send_coin TEXT NOT NULL, send_network TEXT, recv_coin TEXT NOT NULL, recv_network TEXT,
    send_amount TEXT, recv_amount TEXT, recv_address TEXT, deposit_address TEXT,
    fee_usd DOUBLE PRECISION NOT NULL DEFAULT 0, fee_pct DOUBLE PRECISION NOT NULL DEFAULT 0,
    send_usd DOUBLE PRECISION, status TEXT, ip_hash TEXT);
CREATE INDEX IF NOT EXISTS idx_bridge_created ON bridge_orders(created_at);
CREATE INDEX IF NOT EXISTS idx_bridge_status ON bridge_orders(status);
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
            CREATE TABLE IF NOT EXISTS mm_deposits (
                signature  TEXT PRIMARY KEY,                -- idempotency: 1 credit per tx
                wallet     TEXT NOT NULL,
                usd_cents  INTEGER NOT NULL,                -- credited USD value (cents)
                lamports   INTEGER NOT NULL DEFAULT 0,      -- SOL leg
                clean_base INTEGER NOT NULL DEFAULT 0,      -- $CLEAN leg (base units)
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
                gross_amount INTEGER NOT NULL DEFAULT 0,  -- rewards before fee
                fee_amount INTEGER NOT NULL DEFAULT 0,    -- fee deducted
                fee_usd    REAL NOT NULL DEFAULT 0,
                destination TEXT,                         -- immutable payout wallet snapshot
                rules_version TEXT NOT NULL DEFAULT 'v1',
                status     TEXT NOT NULL DEFAULT 'requested',  -- requested|paid|failed
                tx_sig     TEXT,
                created_at INTEGER NOT NULL,
                paid_at    INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_claims_wallet ON claims(wallet, created_at);
            CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_tx_sig_unique ON claims(tx_sig) WHERE tx_sig IS NOT NULL;
            CREATE TABLE IF NOT EXISTS notifs (
                wallet  TEXT NOT NULL,
                kind    TEXT NOT NULL,
                last_ts INTEGER NOT NULL,
                PRIMARY KEY (wallet, kind)
            );
            CREATE TABLE IF NOT EXISTS ops_flags (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            -- Multi-wallet portfolio: a wallet may be LINKED under one owner
            -- (the cluster's anchor). Ownership of BOTH sides is proven by
            -- wallet signature before a row lands here (see /api/link).
            CREATE TABLE IF NOT EXISTS wallet_links (
                wallet TEXT PRIMARY KEY,        -- linked wallet: one cluster max
                owner  TEXT NOT NULL,           -- the anchor wallet
                ts     INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_links_owner ON wallet_links(owner);
            -- No Stains Bridge order log: a forensic/reconciliation trail of every
            -- EasyBit order opened through the app + the fee we charged. Amounts
            -- are TEXT decimal strings (crypto precision; never floats). ip_hash
            -- is a salted HMAC of the client IP — abuse forensics without storing
            -- a raw IP. The swap itself is non-custodial; this is record-keeping.
            CREATE TABLE IF NOT EXISTS bridge_orders (
                order_id        TEXT PRIMARY KEY,   -- EasyBit order id (idempotent)
                created_at      INTEGER NOT NULL,
                updated_at      INTEGER NOT NULL,
                send_coin       TEXT NOT NULL,
                send_network    TEXT,
                recv_coin       TEXT NOT NULL,
                recv_network    TEXT,
                send_amount     TEXT,
                recv_amount     TEXT,
                recv_address    TEXT,
                deposit_address TEXT,
                fee_usd         REAL NOT NULL DEFAULT 0,
                fee_pct         REAL NOT NULL DEFAULT 0,
                send_usd        REAL,
                status          TEXT,
                ip_hash         TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_bridge_created ON bridge_orders(created_at);
            CREATE INDEX IF NOT EXISTS idx_bridge_status ON bridge_orders(status);
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
                    f"UPDATE {table} SET {col} = CAST(ROUND({col} * ?) AS INTEGER) "  # nosec B608
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
        ver = 4
    if ver < 5:
        # v5: payout wallet confirmation (set in the pre-unlock window).
        conn.execute("ALTER TABLE stakers ADD COLUMN payout_wallet TEXT")
        conn.execute("ALTER TABLE stakers ADD COLUMN payout_confirmed_ts INTEGER NOT NULL DEFAULT 0")
        conn.execute("PRAGMA user_version = 5")
        conn.commit()
        ver = 5
    if ver < 6:
        # v6: No Stains Bridge order log (idempotent CREATE IF NOT EXISTS so it's
        # safe whether or not init's executescript already made it).
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS bridge_orders (
                order_id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                send_coin TEXT NOT NULL, send_network TEXT, recv_coin TEXT NOT NULL, recv_network TEXT,
                send_amount TEXT, recv_amount TEXT, recv_address TEXT, deposit_address TEXT,
                fee_usd REAL NOT NULL DEFAULT 0, fee_pct REAL NOT NULL DEFAULT 0, send_usd REAL,
                status TEXT, ip_hash TEXT);
            CREATE INDEX IF NOT EXISTS idx_bridge_created ON bridge_orders(created_at);
            CREATE INDEX IF NOT EXISTS idx_bridge_status ON bridge_orders(status);
            """
        )
        conn.execute("PRAGMA user_version = 6")
        conn.commit()
        ver = 6
    if ver < 7:
        # v7: market-maker liquidity booster — per-staker deposit total (USD cents)
        # plus an idempotent deposit log (one credit per tx).
        conn.execute("ALTER TABLE stakers ADD COLUMN mm_liquidity_cents INTEGER NOT NULL DEFAULT 0")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS mm_deposits (
                signature TEXT PRIMARY KEY, wallet TEXT NOT NULL, usd_cents INTEGER NOT NULL,
                lamports INTEGER NOT NULL DEFAULT 0, clean_base INTEGER NOT NULL DEFAULT 0,
                ts INTEGER NOT NULL);
            """
        )
        conn.execute("PRAGMA user_version = 7")
        conn.commit()
        ver = 7
    if ver < 8:
        # v8: VIP — a verified MM deposit permanently locks the 3x booster and
        # marks the wallet for the VIP airdrop snapshot.
        conn.execute("ALTER TABLE stakers ADD COLUMN mm_vip INTEGER NOT NULL DEFAULT 0")
        conn.execute("PRAGMA user_version = 8")
        conn.commit()
        ver = 8
    if ver < 9:
        # v9: game (Clean Hands tap/idle) cloud save + Most Wanted leaderboard.
        # An opaque client save blob keyed by player, plus a numeric lifetime-
        # laundered score for ranking. Additive; never touches money tables.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS game_state (
                player     TEXT PRIMARY KEY,
                name       TEXT,
                state      TEXT NOT NULL,
                score      INTEGER NOT NULL DEFAULT 0,
                updated_ts INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_game_score ON game_state(score DESC);
            -- Verified Escape rewards: raw game saves are untrusted cloud-state.
            -- Staking APR reads this server-side ledger only.
            CREATE TABLE IF NOT EXISTS game_verification (
                player                TEXT PRIMARY KEY,
                verified_escape_score REAL NOT NULL DEFAULT 0,
                verified_prestige     INTEGER NOT NULL DEFAULT 0,
                raw_escape_score      REAL NOT NULL DEFAULT 0,
                raw_prestige          INTEGER NOT NULL DEFAULT 0,
                first_seen_ts         INTEGER NOT NULL DEFAULT 0,
                last_save_ts          INTEGER NOT NULL DEFAULT 0,
                active_seconds        INTEGER NOT NULL DEFAULT 0,
                save_count            INTEGER NOT NULL DEFAULT 0,
                risk_score            INTEGER NOT NULL DEFAULT 0,
                status                TEXT NOT NULL DEFAULT 'unverified',
                hold_until_ts         INTEGER NOT NULL DEFAULT 0,
                session_id            TEXT,
                last_seq              INTEGER NOT NULL DEFAULT 0,
                reason                TEXT,
                updated_ts            INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_game_verify_status ON game_verification(status, risk_score);
            CREATE TABLE IF NOT EXISTS game_verify_events (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                    INTEGER NOT NULL,
                player                TEXT NOT NULL,
                kind                  TEXT NOT NULL,
                raw_escape_score      REAL NOT NULL DEFAULT 0,
                verified_escape_score REAL NOT NULL DEFAULT 0,
                risk_score            INTEGER NOT NULL DEFAULT 0,
                detail                TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_game_verify_events_player ON game_verify_events(player, ts);
            """
        )
        conn.execute("PRAGMA user_version = 9")
        conn.commit()
        ver = 9
    if ver < 10:
        # v10: immutable claim economics + payout destination snapshots, a
        # unique settlement tx guard, and operator kill-switch flags. Existing
        # claims are backfilled conservatively.
        existing = _column_names(conn, "claims")
        if "gross_amount" not in existing:
            conn.execute("ALTER TABLE claims ADD COLUMN gross_amount INTEGER NOT NULL DEFAULT 0")
        if "fee_amount" not in existing:
            conn.execute("ALTER TABLE claims ADD COLUMN fee_amount INTEGER NOT NULL DEFAULT 0")
        if "fee_usd" not in existing:
            conn.execute("ALTER TABLE claims ADD COLUMN fee_usd REAL NOT NULL DEFAULT 0")
        if "destination" not in existing:
            conn.execute("ALTER TABLE claims ADD COLUMN destination TEXT")
        if "rules_version" not in existing:
            conn.execute("ALTER TABLE claims ADD COLUMN rules_version TEXT NOT NULL DEFAULT 'v1'")
        conn.execute("UPDATE claims SET gross_amount=amount WHERE COALESCE(gross_amount,0)=0")
        conn.execute("UPDATE claims SET destination=wallet WHERE destination IS NULL OR destination=''")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_tx_sig_unique "
            "ON claims(tx_sig) WHERE tx_sig IS NOT NULL"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ops_flags ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL)"
        )
        conn.execute("PRAGMA user_version = 10")
        conn.commit()
        ver = 10
    if ver < 11:
        # v11: raw game saves are no longer trusted for money. Verified Escape
        # progress lives in a separate server-side ledger with risk state.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS game_verification (
                player                TEXT PRIMARY KEY,
                verified_escape_score REAL NOT NULL DEFAULT 0,
                verified_prestige     INTEGER NOT NULL DEFAULT 0,
                raw_escape_score      REAL NOT NULL DEFAULT 0,
                raw_prestige          INTEGER NOT NULL DEFAULT 0,
                first_seen_ts         INTEGER NOT NULL DEFAULT 0,
                last_save_ts          INTEGER NOT NULL DEFAULT 0,
                active_seconds        INTEGER NOT NULL DEFAULT 0,
                save_count            INTEGER NOT NULL DEFAULT 0,
                risk_score            INTEGER NOT NULL DEFAULT 0,
                status                TEXT NOT NULL DEFAULT 'unverified',
                hold_until_ts         INTEGER NOT NULL DEFAULT 0,
                session_id            TEXT,
                last_seq              INTEGER NOT NULL DEFAULT 0,
                reason                TEXT,
                updated_ts            INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_game_verify_status ON game_verification(status, risk_score);
            CREATE TABLE IF NOT EXISTS game_verify_events (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                    INTEGER NOT NULL,
                player                TEXT NOT NULL,
                kind                  TEXT NOT NULL,
                raw_escape_score      REAL NOT NULL DEFAULT 0,
                verified_escape_score REAL NOT NULL DEFAULT 0,
                risk_score            INTEGER NOT NULL DEFAULT 0,
                detail                TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_game_verify_events_player ON game_verify_events(player, ts);
            """
        )
        conn.execute("PRAGMA user_version = 11")
        conn.commit()
        ver = 11
    if ver < 12:
        # v12: verified socials gate Escape reward activation. Raw client UI
        # clicks never unlock money; X/Discord can sit pending until operator
        # review, while Telegram is verified from signed initData.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS social_verifications (
                wallet      TEXT NOT NULL,
                platform    TEXT NOT NULL, -- tg|x|discord
                handle      TEXT,
                verified    INTEGER NOT NULL DEFAULT 0,
                method      TEXT,
                proof       TEXT,
                status      TEXT NOT NULL DEFAULT 'missing',
                verified_at INTEGER NOT NULL DEFAULT 0,
                updated_at  INTEGER NOT NULL,
                PRIMARY KEY (wallet, platform)
            );
            CREATE INDEX IF NOT EXISTS idx_social_status ON social_verifications(platform, status);
            CREATE TABLE IF NOT EXISTS social_verify_events (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       INTEGER NOT NULL,
                wallet   TEXT NOT NULL,
                platform TEXT NOT NULL,
                status   TEXT NOT NULL,
                detail   TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_social_events_wallet ON social_verify_events(wallet, ts);
            """
        )
        conn.execute("PRAGMA user_version = 12")
        conn.commit()


def _column_names(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


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


# --------------------------------------------------------------------------- #
#  GAME state (Clean Hands tap/idle): cloud save + leaderboard.                #
# --------------------------------------------------------------------------- #
def game_load(conn, player: str):
    return conn.execute(
        "SELECT player, name, state, score, updated_ts FROM game_state WHERE player=?",
        (player,),
    ).fetchone()


def game_save(conn, player: str, name: str, state: str, score: int) -> None:
    """Upsert a player's opaque game save. Score (lifetime laundered) is kept
    monotonic via a portable CASE — a stale client can never lower a ranking.
    (Avoids SQLite-only 2-arg MAX(); the CASE form is valid on Postgres too.)"""
    conn.execute(
        "INSERT INTO game_state (player, name, state, score, updated_ts) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(player) DO UPDATE SET "
        "  name=excluded.name, state=excluded.state, "
        "  score=CASE WHEN excluded.score > game_state.score "
        "             THEN excluded.score ELSE game_state.score END, "
        "  updated_ts=excluded.updated_ts",
        (player, name, state, int(score), int(time.time())),
    )
    conn.commit()


def game_top(conn, limit: int = 20):
    return conn.execute(
        "SELECT name, score FROM game_state ORDER BY score DESC, updated_ts ASC LIMIT ?",
        (int(limit),),
    ).fetchall()


def game_verify_load(conn, player: str):
    return conn.execute(
        "SELECT * FROM game_verification WHERE player=?",
        (player,),
    ).fetchone()


def game_verify_save(
    conn,
    *,
    player: str,
    verified_escape_score: float,
    verified_prestige: int,
    raw_escape_score: float,
    raw_prestige: int,
    first_seen_ts: int,
    last_save_ts: int,
    active_seconds: int,
    save_count: int,
    risk_score: int,
    status: str,
    hold_until_ts: int,
    session_id: str | None,
    last_seq: int,
    reason: str,
    updated_ts: int,
) -> None:
    conn.execute(
        "INSERT INTO game_verification "
        "(player, verified_escape_score, verified_prestige, raw_escape_score, raw_prestige, "
        "first_seen_ts, last_save_ts, active_seconds, save_count, risk_score, status, "
        "hold_until_ts, session_id, last_seq, reason, updated_ts) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(player) DO UPDATE SET "
        "verified_escape_score=excluded.verified_escape_score, "
        "verified_prestige=excluded.verified_prestige, "
        "raw_escape_score=excluded.raw_escape_score, raw_prestige=excluded.raw_prestige, "
        "first_seen_ts=excluded.first_seen_ts, last_save_ts=excluded.last_save_ts, "
        "active_seconds=excluded.active_seconds, save_count=excluded.save_count, "
        "risk_score=excluded.risk_score, status=excluded.status, "
        "hold_until_ts=excluded.hold_until_ts, session_id=excluded.session_id, "
        "last_seq=excluded.last_seq, reason=excluded.reason, updated_ts=excluded.updated_ts",
        (
            player,
            float(verified_escape_score or 0),
            int(verified_prestige or 0),
            float(raw_escape_score or 0),
            int(raw_prestige or 0),
            int(first_seen_ts or 0),
            int(last_save_ts or 0),
            int(active_seconds or 0),
            int(save_count or 0),
            int(risk_score or 0),
            str(status or "unverified")[:32],
            int(hold_until_ts or 0),
            (str(session_id)[:64] if session_id else None),
            int(last_seq or 0),
            str(reason or "")[:240],
            int(updated_ts or time.time()),
        ),
    )
    conn.commit()


def game_verify_event(
    conn,
    player: str,
    kind: str,
    raw_escape_score: float,
    verified_escape_score: float,
    risk_score: int,
    detail: str = "",
) -> None:
    conn.execute(
        "INSERT INTO game_verify_events "
        "(ts, player, kind, raw_escape_score, verified_escape_score, risk_score, detail) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            int(time.time()),
            player,
            str(kind or "")[:32],
            float(raw_escape_score or 0),
            float(verified_escape_score or 0),
            int(risk_score or 0),
            str(detail or "")[:240],
        ),
    )
    conn.commit()


SOCIAL_PLATFORMS = ("tg", "x", "discord")


def _social_platform(platform: str) -> str:
    p = str(platform or "").strip().lower()
    aliases = {
        "telegram": "tg",
        "twitter": "x",
        "𝕏": "x",
        "disc": "discord",
    }
    p = aliases.get(p, p)
    if p not in SOCIAL_PLATFORMS:
        raise ValueError("unknown social platform")
    return p


def social_set(
    conn,
    *,
    wallet: str,
    platform: str,
    verified: bool,
    status: str | None = None,
    handle: str | None = None,
    method: str = "",
    proof: str = "",
) -> None:
    """Upsert a social verification row.

    The staking payout path reads only this server-side ledger. Client-submitted
    X/Discord handles land as pending until an admin verifies them.
    """
    p = _social_platform(platform)
    now = int(time.time())
    is_verified = 1 if verified else 0
    st = str(status or ("verified" if verified else "pending")).strip().lower()[:32]
    verified_at = now if verified else 0
    conn.execute(
        "INSERT INTO social_verifications "
        "(wallet, platform, handle, verified, method, proof, status, verified_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(wallet, platform) DO UPDATE SET "
        "handle=excluded.handle, verified=excluded.verified, method=excluded.method, "
        "proof=excluded.proof, status=excluded.status, verified_at=excluded.verified_at, "
        "updated_at=excluded.updated_at",
        (
            wallet,
            p,
            (str(handle or "")[:96] if handle is not None else None),
            is_verified,
            str(method or "")[:48],
            str(proof or "")[:240],
            st,
            verified_at,
            now,
        ),
    )
    conn.execute(
        "INSERT INTO social_verify_events (ts, wallet, platform, status, detail) VALUES (?,?,?,?,?)",
        (now, wallet, p, st, str(method or "")[:120]),
    )
    conn.commit()


def social_status(conn, wallet: str) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT platform, handle, verified, method, status, verified_at, updated_at "
        "FROM social_verifications WHERE wallet=?",
        (wallet,),
    ).fetchall()
    out = {
        p: {
            "platform": p,
            "verified": False,
            "status": "missing",
            "handle": "",
            "verified_at": 0,
            "updated_at": 0,
        }
        for p in SOCIAL_PLATFORMS
    }
    for r in rows:
        try:
            p = _social_platform(r["platform"])
        except ValueError:
            continue
        out[p] = {
            "platform": p,
            "verified": bool(r["verified"]),
            "status": r["status"] or ("verified" if r["verified"] else "pending"),
            "handle": r["handle"] or "",
            "verified_at": int(r["verified_at"] or 0),
            "updated_at": int(r["updated_at"] or 0),
        }
    return out


def social_summary(conn, wallet: str) -> dict:
    platforms = social_status(conn, wallet)
    count = sum(1 for p in SOCIAL_PLATFORMS if platforms[p]["verified"])
    return {
        "required": len(SOCIAL_PLATFORMS),
        "verified_count": count,
        "multiplier": count / len(SOCIAL_PLATFORMS),
        "platforms": platforms,
    }


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


def effective_staked_expr() -> str:
    """Portable SQL expression for canonical effective stake.

    Use this everywhere public/economic ranking is computed so ghost stakes
    (recorded stake with no verified on-chain balance) do not count.
    """
    return (
        "CASE "
        "WHEN recorded_staked <= 0 OR cached_balance <= 0 THEN 0 "
        "WHEN recorded_staked < cached_balance THEN recorded_staked "
        "ELSE cached_balance END"
    )


def referral_min_base() -> int:
    return to_base(float(os.environ.get("STAKE_REFERRAL_MIN_TOKENS", "1") or 0))


def referral_min_age_secs() -> int:
    return int(os.environ.get("STAKE_REFERRAL_MIN_AGE_SECS", "0") or 0)


def active_referrals(conn, wallet: str) -> int:
    eff = effective_staked_expr()
    params = [wallet, referral_min_base()]
    age = referral_min_age_secs()
    age_clause = ""
    if age > 0:
        age_clause = " AND stake_start_ts > 0 AND stake_start_ts <= ?"
        params.append(int(time.time()) - age)
    # eff/age_clause are internal SQL constants, not user input.
    return conn.execute(
        f"SELECT COUNT(*) AS n FROM stakers WHERE referred_by=? AND ({eff}) >= ?{age_clause}",  # nosec B608
        tuple(params),
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


def mm_seen(conn, signature: str) -> bool:
    return conn.execute("SELECT 1 FROM mm_deposits WHERE signature=?", (signature,)).fetchone() is not None


def list_vips(conn) -> list[dict]:
    """Every VIP wallet (verified MM depositor) for airdrop snapshots, biggest first."""
    rows = conn.execute(
        "SELECT wallet, mm_liquidity_cents FROM stakers WHERE mm_vip=1 ORDER BY mm_liquidity_cents DESC"
    ).fetchall()
    return [
        {"wallet": r["wallet"], "deposit_usd": round((r["mm_liquidity_cents"] or 0) / 100.0, 2)}
        for r in rows
    ]


def create_claim(
    conn,
    wallet: str,
    amount_base: int,
    *,
    gross_amount_base: int,
    fee_amount_base: int = 0,
    fee_usd: float = 0.0,
    destination: str,
    rules_version: str = "v1",
    status: str = "requested",
) -> None:
    conn.execute(
        "INSERT INTO claims "
        "(wallet, amount, gross_amount, fee_amount, fee_usd, destination, rules_version, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            wallet,
            int(amount_base),
            int(gross_amount_base),
            int(fee_amount_base),
            float(fee_usd),
            destination,
            rules_version,
            status,
            int(time.time()),
        ),
    )
    conn.commit()


def list_pending_claims(conn, limit: int = 200):
    return conn.execute(
        "SELECT * FROM claims WHERE status='requested' ORDER BY created_at LIMIT ?", (limit,)
    ).fetchall()


def get_claim(conn, claim_id: int):
    return conn.execute("SELECT * FROM claims WHERE id=?", (int(claim_id),)).fetchone()


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
    eff = effective_staked_expr()
    # eff is an internal SQL constant, not user input.
    return conn.execute(
        f"SELECT * FROM stakers WHERE tg_id IS NOT NULL AND ({eff}) > 0"  # nosec B608
    ).fetchall()


def mark_claim_paid(conn, claim_id: int, tx_sig: str) -> int:
    """Idempotent: only a still-'requested' claim transitions to 'paid'."""
    if conn.execute("SELECT 1 FROM claims WHERE tx_sig=?", (tx_sig,)).fetchone() is not None:
        return 0
    cur = conn.execute(
        "UPDATE claims SET status='paid', tx_sig=?, paid_at=? WHERE id=? AND status='requested'",
        (tx_sig, int(time.time()), claim_id),
    )
    conn.commit()
    return cur.rowcount


_OPS_FLAGS = {
    "halt_all",
    "halt_staking",
    "halt_claims",
    "halt_burns",
    "halt_payout_setup",
    "halt_mm",
    "halt_bridge",
    "halt_escape_boost",
}


def set_flag(conn, key: str, value: str) -> None:
    if key not in _OPS_FLAGS:
        raise ValueError("unknown ops flag")
    conn.execute("DELETE FROM ops_flags WHERE key=?", (key,))
    conn.execute(
        "INSERT INTO ops_flags (key, value, updated_at) VALUES (?,?,?)",
        (key, str(value), int(time.time())),
    )
    conn.commit()


def get_flag(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM ops_flags WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def flag_enabled(conn, key: str) -> bool:
    v = get_flag(conn, key)
    return str(v).lower() in ("1", "true", "yes", "on")


def list_flags(conn):
    return conn.execute("SELECT key, value, updated_at FROM ops_flags ORDER BY key").fetchall()


# ---- multi-wallet portfolio links ----------------------------------------- #
def link_owner(conn, wallet: str) -> str:
    """The cluster anchor for any wallet (itself when unlinked)."""
    row = conn.execute("SELECT owner FROM wallet_links WHERE wallet=?", (wallet,)).fetchone()
    return row["owner"] if row else wallet


def cluster_wallets(conn, wallet: str) -> list[str]:
    """Anchor first, then linked wallets in link order."""
    o = link_owner(conn, wallet)
    rows = conn.execute("SELECT wallet FROM wallet_links WHERE owner=? ORDER BY ts, wallet", (o,)).fetchall()
    return [o] + [r["wallet"] for r in rows]


def link_wallet(conn, sess_wallet: str, new_wallet: str, limit: int = 10) -> str | None:
    """Attach new_wallet to sess_wallet's cluster. Returns an error string for
    the user, or None on success. Caller has already verified ownership of BOTH
    wallets (session for sess_wallet, fresh signature for new_wallet)."""
    o = link_owner(conn, sess_wallet)
    if new_wallet in cluster_wallets(conn, o):
        return "that wallet is already in your portfolio"
    if conn.execute("SELECT 1 FROM wallet_links WHERE owner=?", (new_wallet,)).fetchone():
        return "that wallet anchors its own portfolio — unlink its wallets there first"
    if len(cluster_wallets(conn, o)) >= limit:
        return f"portfolio limit reached ({limit} wallets)"
    # If the wallet is linked under some other portfolio, the FRESH signature we
    # just verified proves the caller controls it NOW — re-home it instead of
    # dead-ending (self-healing for re-tests, migrations, and shared devices).
    conn.execute("DELETE FROM wallet_links WHERE wallet=?", (new_wallet,))
    conn.execute(
        "INSERT INTO wallet_links (wallet, owner, ts) VALUES (?,?,?)",
        (new_wallet, o, int(time.time())),
    )
    conn.commit()
    return None


def unlink_wallet(conn, sess_wallet: str, wallet: str) -> str | None:
    """Detach `wallet` from the caller's cluster. Any cluster member's session
    may unlink; the anchor itself cannot be unlinked (it IS the cluster)."""
    o = link_owner(conn, sess_wallet)
    if wallet == o:
        return "the anchor wallet can't be unlinked"
    cur = conn.execute("DELETE FROM wallet_links WHERE wallet=? AND owner=?", (wallet, o))
    conn.commit()
    if cur.rowcount != 1:
        return "that wallet is not in your portfolio"
    return None
