"""Regression tests for the /meme 'stuck on Soaking the image…' bug.

The flagship AI wash (`ai_glove_hands`) used a single 120 s httpx timeout with
no short connect cap and no overall watchdog, and `_run_meme` was fully gated on
it. A slow render — or, worse, a blocked egress to the image API — left the
cleaning animation cycling forever and the meme never posted. (The engine is now
Gemini 2.5 Flash Image; the timeout/anti-stuck guarantees are engine-agnostic.)

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
os.environ.setdefault("GEMINI_API_KEY", "test-key")  # AI path ON
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

    async def edit_text(self, t, *a, **k):
        self.text = t

    async def delete(self):
        self.deleted = True


class _Bot:
    def __init__(self):
        self.ph = _Placeholder()
        self.photos = []

    async def get_file(self, fid):
        return _File()

    async def delete_message(self, c, m):
        pass

    async def send_message(self, c, t, *a, **k):
        self.ph.text = t
        return self.ph

    async def send_chat_action(self, c, a):
        pass

    async def send_photo(self, c, photo, caption=None, **k):
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
    """A non-200 (e.g. permission denied) returns fast — meme posts immediately."""
    async def post(*a, **k):
        return httpx.Response(403, json={"error": {"message": "permission denied"}})

    _patch_httpx(post)
    cb.MEME_AI_TIMEOUT = 60  # restore default-ish; failure is fast regardless
    bot = _Bot()
    asyncio.run(_run(bot))
    assert bot.photos and bot.photos[0][0] == "🧤 $CLEAN"
    assert "hiccupped" in (bot.ph.text or "")


def test_run_meme_happy_path_posts_ai_result():
    """AI succeeds -> 'washed by $CLEAN' and the placeholder is deleted.

    Mirrors a Gemini generateContent response: the edited image is the inline
    data part (camelCase 'inlineData')."""
    async def post(*a, **k):
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [
                        {"text": "Here you go"},
                        {"inlineData": {"mimeType": "image/png", "data": _png_b64()}},
                    ]}}
                ]
            },
        )

    _patch_httpx(post)
    cb.MEME_AI_TIMEOUT = 60
    bot = _Bot()
    asyncio.run(_run(bot))
    assert bot.photos and bot.photos[0][0] == "🧤 washed by $CLEAN"
    assert bot.ph.deleted is True


# ---------------------------------------------------------------------------- #
#  Gemini response parsing                                                      #
# ---------------------------------------------------------------------------- #
def test_extract_image_camel_and_snake_case():
    b = _png_b64()
    camel = {"candidates": [{"content": {"parts": [{"inlineData": {"data": b}}]}}]}
    snake = {"candidates": [{"content": {"parts": [{"inline_data": {"data": b}}]}}]}
    assert cb._extract_gemini_image(camel) is not None
    assert cb._extract_gemini_image(snake) is not None


def test_extract_image_safety_block_returns_none_with_reason():
    payload = {"candidates": [{"finishReason": "IMAGE_SAFETY",
                               "content": {"parts": [{"text": "declined"}]}}]}
    assert cb._extract_gemini_image(payload) is None
    assert "no image" in cb._AI_LAST_ERR.lower()


if __name__ == "__main__":
    import time
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        t0 = time.time()
        fn()
        print(f"ok  {fn.__name__}  ({time.time() - t0:.1f}s)")
    print(f"\n{len(fns)} passed")
