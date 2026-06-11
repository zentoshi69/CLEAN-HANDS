#!/usr/bin/env python3
"""
configure.py — one-shot push of every @BotFather setting the Bot API *can* set,
so you don't click through the BotFather menus by hand.

Run it once (re-run any time) after putting your FRESH tokens in .env:

    set -a; source .env; set +a
    python configure.py

It sets, per bot (only for tokens that are present):
  • bot name / short description / description
  • the slash-command menu (same as the bots set on startup)
  • default group Admin Rights  (Guardian: delete+ban+restrict, Scanner: delete)
  • the chat Menu Button → your Mini App (Community bot, needs MINIAPP_URL)

What the Bot API CANNOT do — these stay manual in @BotFather (the script prints
a reminder):
  • /setprivacy → Disable        (Group Privacy)
  • Allow Groups / Inline Mode
  • Configure Mini App (the web-app URL via /newapp)
  • Payments, Login Domain

No secrets are printed. Tokens are read from the environment only.
"""

import os
import sys
import httpx

API = "https://api.telegram.org/bot{token}/{method}"


def call(token: str, method: str, payload: dict) -> dict:
    r = httpx.post(API.format(token=token, method=method), json=payload, timeout=20)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"{method}: {data.get('description')}")
    return data["result"]


def configure(label: str, token: str, *, name, short, desc, commands, admin_rights=None, menu_url=None):
    if not token:
        print(f"— {label}: no token set, skipping")
        return
    try:
        me = call(token, "getMe", {})
        print(f"==> {label}: @{me['username']}")
        call(token, "setMyName", {"name": name})
        call(token, "setMyShortDescription", {"short_description": short})
        call(token, "setMyDescription", {"description": desc})
        call(token, "setMyCommands", {"commands": commands})
        print("    ✓ name, descriptions, commands")
        if admin_rights is not None:
            call(token, "setMyDefaultAdministratorRights", {"rights": admin_rights, "for_channels": False})
            print("    ✓ default group admin rights")
        if menu_url:
            call(
                token,
                "setChatMenuButton",
                {"menu_button": {"type": "web_app", "text": "🧤 App", "web_app": {"url": menu_url}}},
            )
            print(f"    ✓ menu button → {menu_url}")
    except Exception as e:  # noqa: BLE001
        # httpx errors embed the request URL, which contains the bot token
        print(f"    ✗ {label}: {str(e).replace(token, '***')}", file=sys.stderr)


def cmds(*pairs):
    return [{"command": c, "description": d} for c, d in pairs]


def main():
    guardian = os.environ.get("TG_BOT_TOKEN", "")
    scanner = os.environ.get("TG_SCANNER_TOKEN", "")
    community = os.environ.get("TG_COMMUNITY_TOKEN", "")
    miniapp_url = os.environ.get("MINIAPP_URL", "").strip()

    # Guardian: needs delete + ban + restrict to moderate.
    configure(
        "Guardian", guardian,
        name="Degen Guardian",
        short="CAPTCHA gate, anti-scam, impersonation guard.",
        desc="Protects the group: join CAPTCHA, deletes drainer/phishing links, flags admin impersonators.",
        commands=cmds(
            ("setup", "One-shot: lock down + pin rules + invite link"),
            ("lockdown", "Panic: mute everyone (raid control)"),
            ("unlock", "Lift a lockdown"),
            ("refreshadmins", "Reload the admin list"),
            ("rules", "Show the safety rules"),
        ),
        admin_rights={
            "is_anonymous": False,
            "can_manage_chat": True,
            "can_delete_messages": True,
            "can_restrict_members": True,
            "can_promote_members": False,
            "can_change_info": False,
            "can_invite_users": True,
            "can_pin_messages": True,
        },
    )

    # Scanner: needs delete only.
    configure(
        "Scanner", scanner,
        name="Degen Scanner",
        short="Auto-checks every Solana contract posted.",
        desc="Scans posted Solana mints (RugCheck) and removes high-risk contracts from non-admins.",
        commands=cmds(("scan", "Safety-check a Solana mint: /scan <address>")),
        admin_rights={
            "is_anonymous": False,
            "can_manage_chat": True,
            "can_delete_messages": True,
            "can_restrict_members": False,
            "can_promote_members": False,
            "can_change_info": False,
            "can_invite_users": False,
            "can_pin_messages": False,
        },
    )

    # Community: no admin rights; gets the Mini App menu button.
    configure(
        "Community", community,
        name="CLEAN",
        short="Price, trade, memes, and the $CLEAN staking app.",
        desc="/price /stats /chart /trade, /meme /glove /sticker, and /app for staking, leaderboard & referrals.",
        commands=cmds(
            ("app", "Open the $CLEAN App (stake, leaderboard, invite)"),
            ("stake", "Stake points for yield"),
            ("invite", "Get your referral link"),
            ("price", "Quick price + 24h change"),
            ("stats", "Full token readout"),
            ("chart", "Live DexScreener chart"),
            ("trade", "Buy/sell links (Jupiter, non-custodial)"),
            ("meme", "Reply to a photo: /meme top | bottom"),
            ("glove", "Reply to a photo to slap the $CLEAN glove on it"),
            ("sticker", "Reply to a photo to make a sticker"),
            ("help", "Show commands"),
        ),
        menu_url=miniapp_url if miniapp_url.startswith("https://") else None,
    )

    print("\nDone with everything the Bot API can set.")
    print("Still do these by hand in @BotFather (no API for them):")
    print("  • /setprivacy → Disable   (each bot, so they can read group messages)")
    print("  • Allow Groups (on), Inline Mode (as desired)")
    print("  • Configure Mini App → set the web-app URL (or /newapp) for the Community bot")
    print("  • Payments / Login Domain if you use them")


if __name__ == "__main__":
    main()
