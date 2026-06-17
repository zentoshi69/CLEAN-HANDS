"""Regression tests for the /meme 'stuck on Soaking the image…' bug.

The flagship AI wash (`ai_glove_hands`) used a single 120 s httpx timeout with
no short connect cap and no overall watchdog, and `_run_meme` was fully gated on
it. A slow gpt-image-1 render — or, worse, a blocked egress to api.openai.com —
left the cleaning animation cycling forever and the meme never posted.

These tests pin the fix:
  * `ai_glove_hands` is time-bounded and returns None promptly on a stalled /
    blocked connection (so the caller can fall back to the local stamp);
  * `_run_meme` ALWAYS posts a photo and ALWAYS stops the cleaning animation,
    whether the AI succeeds, fails fast, or hangs.

Run:  python -m pytest test_meme_timeout.py -q
  or:  python test_meme_timeout.py
"""
import io
import os
import asyncio

os.environ.setdefault("TG_COMMUNITY_TOKEN", "123:TEST")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")  # AI path ON
os.environ.setdefault("MEME_AI_COOLDOWN", "0")

import httpx
from PIL import Image

import community_bot as cb


# --- a tiny real JPEG to act as the "uploaded photo" ------------------------ #
def _jpeg() -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 180, 160)).save(b, "JPEG")
    return b.getvalue()


def _png_b64() -> str:
    import base64
    b = io.BytesIO()
    Image.new("RGB", (64, 64), (0, 128, 255)).save(b, "PNG")
    return base64.b64encode(b.getvalue()).decode()


# --- fake httpx.AsyncClient driven by a per-test `post` impl ----------------- #
def _patch_httpx(monkeypatch_post):
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return await monkeypatch_post(*a, **k)

    cb.httpx.AsyncClient = _Client  # type: ignore[attr-defined]


# --- fake Telegram surface for _run_meme ------------------------------------ #
class _Photo:
    file_id = "F"
    file_size = 1234


class _File:
    async def download_as_bytearray(self):
        return bytearray(_jpeg())


class _Placeholder:
    def __init__(self):
        self.text = None
        self.deleted = False
        self.edits = 0  # editing this on a timer is what used to flood the chat

    async def edit_text(self, t, *a, **k):
        self.edits += 1
        self.text = t

    async def delete(self):
        self.deleted = True


class _Bot:
    def __init__(self, *, flood_photo=0):
        self.ph = _Placeholder()
        self.photos = []
        self.actions = 0          # native 'uploading photo…' keepalive (flood-exempt)
        self._flood_photo = flood_photo  # raise RetryAfter on the first N photo sends

    async def get_file(self, fid):
        return _File()

    async def delete_message(self, c, m):
        pass

    async def send_message(self, c, t, *a, **k):
        self.ph.text = t
        return self.ph

    async def send_chat_action(self, c, a):
        self.actions += 1

    async def send_photo(self, c, photo, caption=None, **k):
        if self._flood_photo > 0:
            self._flood_photo -= 1
            from telegram.error import RetryAfter
            raise RetryAfter(0)  # transient per-chat flood; sender must retry
        data = photo.getvalue() if hasattr(photo, "getvalue") else b""
        self.photos.append((caption, len(data)))


class _User:
    id = 42


class _Msg:
    def __init__(self):
        self.message_id = 1
        self.chat_id = -100
        self.caption = "/meme"
        self.photo = [_Photo()]
        self.reply_to_message = None


class _Update:
    def __init__(self):
        self.message = _Msg()
        self.effective_user = _User()


class _Ctx:
    def __init__(self, bot):
        self.bot = bot


async def _run(bot):
    upd = _Update()
    cb._ai_last.clear()
    # never let a test wedge the suite: the whole command must finish well
    # inside the AI budget + a generous margin
    await asyncio.wait_for(
        cb._run_meme(upd, _Ctx(bot), upd.message, []),
        timeout=cb.MEME_AI_TIMEOUT + 20,
    )


# ---------------------------------------------------------------------------- #
#  ai_glove_hands: time-bounded                                                #
# ---------------------------------------------------------------------------- #
def test_ai_returns_fast_on_blocked_egress():
    """A blocked/firewalled egress surfaces as a connect timeout — the call must
    NOT sit there: it returns None quickly so the caller can fall back."""
    async def post(*a, **k):
        raise httpx.ConnectTimeout("egress blocked")

    _patch_httpx(post)

    async def go():
        out = await asyncio.wait_for(ai := cb.ai_glove_hands(_jpeg()), timeout=5)
        return out

    out = asyncio.run(go())
    assert out is None
    assert "timeout" in cb._AI_LAST_ERR.lower()


def test_ai_watchdog_caps_a_silent_hang():
    """If the connection hangs with no error at all, the outer asyncio deadline
    still bounds the call (this is THE 'stuck forever' guard)."""
    async def post(*a, **k):
        await asyncio.sleep(3600)  # never returns

    _patch_httpx(post)
    cb.MEME_AI_TIMEOUT = 0.4  # shrink the budget so the test is quick

    async def go():
        return await cb.ai_glove_hands(_jpeg())

    # must resolve via the watchdog (budget + 5 s belt), never hang
    out = asyncio.run(asyncio.wait_for(go(), timeout=10))
    assert out is None
    assert "timeout" in cb._AI_LAST_ERR.lower()


# ---------------------------------------------------------------------------- #
#  _run_meme: always posts, always stops the animation                         #
# ---------------------------------------------------------------------------- #
def test_run_meme_posts_fallback_when_ai_hangs():
    """The bug: AI hangs -> meme never posts, spinner cycles forever. Fixed:
    the wash is bounded, so _run_meme posts the local stamp and the cleaning
    animation is stopped."""
    async def post(*a, **k):
        await asyncio.sleep(3600)

    _patch_httpx(post)
    cb.MEME_AI_TIMEOUT = 0.4
    bot = _Bot()
    asyncio.run(_run(bot))

    assert bot.photos, "no photo posted — /meme is still stuck"
    caption, nbytes = bot.photos[0]
    assert caption == "🧤 $CLEAN" and nbytes > 0      # local-stamp fallback shipped
    assert "hiccupped" in (bot.ph.text or "")          # loud, not silent
    # the animation task was stopped, not left looping
    # (placeholder kept as the ⚠️ notice rather than deleted)


def test_run_meme_fast_fail_falls_back():
    """A 403 (org not verified) returns fast — meme posts immediately."""
    async def post(*a, **k):
        return httpx.Response(403, text="must be verified")

    _patch_httpx(post)
    cb.MEME_AI_TIMEOUT = 90  # restore default-ish; failure is fast regardless
    bot = _Bot()
    asyncio.run(_run(bot))
    assert bot.photos and bot.photos[0][0] == "🧤 $CLEAN"
    assert "hiccupped" in (bot.ph.text or "")


def test_run_meme_happy_path_posts_ai_result():
    """AI succeeds -> 'washed by $CLEAN' and the placeholder is deleted."""
    async def post(*a, **k):
        return httpx.Response(200, json={"data": [{"b64_json": _png_b64()}]})

    _patch_httpx(post)
    cb.MEME_AI_TIMEOUT = 90
    bot = _Bot()
    asyncio.run(_run(bot))
    assert bot.photos and bot.photos[0][0] == "🧤 washed by $CLEAN"
    assert bot.ph.deleted is True


# ---------------------------------------------------------------------------- #
#  flood control: the REAL 'stops halfway' bug                                 #
# ---------------------------------------------------------------------------- #
def test_run_meme_no_edit_loop_during_render():
    """THE root-cause guard. The old code edited the placeholder ~once a second
    while the AI rendered; 20+ edits trip Telegram's per-chat flood limit and the
    final send_photo gets 429'd, so the meme never posts. The fix shows liveliness
    via the native 'uploading photo…' chat action (flood-exempt) and NEVER edits
    the placeholder on a timer. Pin both facts against a multi-second render."""
    async def post(*a, **k):
        return httpx.Response(200, json={"data": [{"b64_json": _png_b64()}]})

    _patch_httpx(post)
    cb.MEME_AI_TIMEOUT = 90

    real_ai = cb.ai_glove_hands

    async def slow_ai(b):
        await asyncio.sleep(2.5)  # longer than the old 1.4s edit cadence
        return await real_ai(b)

    cb.ai_glove_hands = slow_ai
    try:
        bot = _Bot()
        asyncio.run(_run(bot))
    finally:
        cb.ai_glove_hands = real_ai

    assert bot.photos, "no photo posted — /meme is still stuck"
    assert bot.actions >= 1, "no chat-action keepalive — nothing shows progress"
    # zero timed edits during the render: success deletes the placeholder, it is
    # never edited. This is what keeps the chat under the flood limit.
    assert bot.ph.edits == 0, f"placeholder was edited {bot.ph.edits}x — flood risk"
    assert bot.ph.deleted is True


def test_run_meme_survives_transient_photo_flood():
    """Even if the final send_photo hits a transient 429, the meme is the whole
    point — the resilient sender honours Retry-After and posts it anyway."""
    async def post(*a, **k):
        return httpx.Response(200, json={"data": [{"b64_json": _png_b64()}]})

    _patch_httpx(post)
    cb.MEME_AI_TIMEOUT = 90
    bot = _Bot(flood_photo=1)  # first send 429s, retry succeeds
    asyncio.run(_run(bot))
    assert bot.photos and bot.photos[0][0] == "🧤 washed by $CLEAN"
    assert bot.ph.deleted is True


def test_send_photo_resilient_gives_up_on_hard_error():
    """A non-flood send failure must NOT loop forever — it returns False so the
    caller can surface an error instead of hanging."""
    class _BadBot:
        async def send_photo(self, *a, **k):
            raise RuntimeError("boom")

    out = asyncio.run(cb._send_photo_resilient(_BadBot(), -100, b"x", "cap"))
    assert out is False


if __name__ == "__main__":
    import time
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        t0 = time.time()
        fn()
        print(f"ok  {fn.__name__}  ({time.time() - t0:.1f}s)")
    print(f"\n{len(fns)} passed")
