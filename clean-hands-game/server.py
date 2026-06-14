"""Clean Hands, Dirty Money — standalone game backend.

Foundational, intentionally self-contained so it can be reviewed and run on its
own. It serves the static game and persists per-player progress + a leaderboard
in SQLite. Player identity here is an anonymous client id (localStorage UUID);
when we integrate into the staking-api we swap that for the wallet auth token —
the table shape (state JSON + score) is already what staking-api db.py expects
(see README "Integration").

Run:
    pip install -r requirements.txt
    uvicorn server:app --reload --port 8001
    # open http://localhost:8001
"""

from __future__ import annotations

import json
import os
import sqlite3
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

app = FastAPI(title="Clean Hands, Dirty Money")

WEB = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("GAME_DB", os.path.join(WEB, "game.db"))

MAX_STATE_BYTES = 16_000  # a save blob is tiny; reject anything suspicious
MAX_SCORE = 10**18


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS game_state (
                player     TEXT PRIMARY KEY,
                name       TEXT NOT NULL DEFAULT '',
                state      TEXT NOT NULL,
                score      INTEGER NOT NULL DEFAULT 0,
                updated_ts INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_game_score ON game_state(score DESC);
            """
        )
        conn.commit()


init_db()


# --------------------------------------------------------------------- models
class LoadBody(BaseModel):
    player: str


class SaveBody(BaseModel):
    player: str
    state: str
    score: int = 0
    name: str = ""


def _clean_player(p: str) -> str:
    p = (p or "").strip()
    if not (1 <= len(p) <= 64):
        raise HTTPException(400, "bad player id")
    return p


# ----------------------------------------------------------------------- api
@app.post("/api/load")
def api_load(body: LoadBody):
    player = _clean_player(body.player)
    with db() as conn:
        row = conn.execute(
            "SELECT state, score, updated_ts FROM game_state WHERE player=?", (player,)
        ).fetchone()
    if not row:
        return {"state": None, "score": 0}
    return {"state": row["state"], "score": row["score"], "updated_ts": row["updated_ts"]}


@app.post("/api/save")
def api_save(body: SaveBody):
    player = _clean_player(body.player)
    state = body.state or ""
    if len(state.encode("utf-8")) > MAX_STATE_BYTES:
        raise HTTPException(413, "save too large")
    try:
        json.loads(state)  # must be valid JSON; we store it opaquely
    except Exception:
        raise HTTPException(400, "state must be JSON")
    score = max(0, min(int(body.score or 0), MAX_SCORE))
    name = (body.name or "")[:24]
    with db() as conn:
        conn.execute(
            "INSERT INTO game_state (player, name, state, score, updated_ts) VALUES (?,?,?,?,?) "
            "ON CONFLICT(player) DO UPDATE SET name=excluded.name, state=excluded.state, "
            "score=excluded.score, updated_ts=excluded.updated_ts",
            (player, name, state, score, int(time.time())),
        )
        conn.commit()
    return {"ok": True}


@app.get("/api/leaderboard")
def api_leaderboard(limit: int = 20):
    limit = max(1, min(int(limit), 100))
    with db() as conn:
        rows = conn.execute(
            "SELECT player, name, score FROM game_state WHERE score > 0 "
            "ORDER BY score DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        label = r["name"] or (r["player"][:4] + "…" + r["player"][-4:])
        out.append({"name": label, "score": r["score"]})
    return {"top": out}


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ------------------------------------------------------------------- static
_NO_CACHE = {"Cache-Control": "no-cache, max-age=0, must-revalidate"}


@app.get("/")
def index():
    return FileResponse(os.path.join(WEB, "index.html"), headers=_NO_CACHE)


@app.get("/game.css")
def game_css():
    return FileResponse(os.path.join(WEB, "game.css"), media_type="text/css", headers=_NO_CACHE)


@app.get("/game.js")
def game_js():
    return FileResponse(
        os.path.join(WEB, "game.js"), media_type="application/javascript", headers=_NO_CACHE
    )


@app.exception_handler(404)
async def not_found(_request: Request, _exc):
    return JSONResponse({"error": "not found"}, status_code=404)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8001")))
