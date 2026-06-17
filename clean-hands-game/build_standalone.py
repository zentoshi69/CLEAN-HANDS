"""Refresh standalone.html from the canonical, deployed game.

The single source of truth for the game is the file served in production at /play:
    clean-hands/staking-api/webapp/play.html
It is already a fully self-contained, offline-first single file (CSS + JS + assets
inlined), so the "standalone" build is simply a verified copy of it. Keeping the two
identical is enforced in CI (.github/workflows/ci.yml: "game single-source guard").

Run: python3 build_standalone.py  ->  standalone.html

NOTE: the older index.html / game.css / game.js in this folder are the original
BACKBONE engine, kept only as a design reference. They are no longer the source of
standalone.html — do not rebuild from them.
"""
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
CANONICAL = os.path.normpath(
    os.path.join(HERE, "..", "clean-hands", "staking-api", "webapp", "play.html")
)
OUT = os.path.join(HERE, "standalone.html")

shutil.copyfile(CANONICAL, OUT)
print(f"wrote standalone.html from {CANONICAL}: {os.path.getsize(OUT)} bytes")
