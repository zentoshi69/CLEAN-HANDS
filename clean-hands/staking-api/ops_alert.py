#!/usr/bin/env python3
"""
ops_alert.py — DM the admins a one-line operational alert. Used by systemd
`OnFailure=` hooks (reconcile drift, backup failure) and safe to call by hand.

Targets (first that resolves):
  * TG_ALERTS_CHAT   — a channel/chat id or @handle, OR
  * TG_ADMIN_IDS     — comma-separated numeric user ids.
Token (first that resolves): TG_ALERTS_TOKEN, else TG_COMMUNITY_TOKEN.

Stdlib only (urllib) so it runs under the system python OR a venv with no extra
deps. Best-effort: prints the message and returns 0 when TG isn't configured, so
it never masks the failure that triggered it.

    python ops_alert.py "❌ something broke"
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request


def _targets() -> list[str]:
    chat = (os.environ.get("TG_ALERTS_CHAT") or "").strip()
    if chat:
        return [chat]
    ids = (os.environ.get("TG_ADMIN_IDS") or "").replace(" ", "")
    return [x for x in ids.split(",") if x]


def main() -> int:
    msg = " ".join(sys.argv[1:]).strip() or "⚠️ CLEAN ops alert"
    token = (os.environ.get("TG_ALERTS_TOKEN") or os.environ.get("TG_COMMUNITY_TOKEN") or "").strip()
    targets = _targets()
    if not token or not targets:
        print(f"ops_alert: TG not configured — message was:\n{msg}", file=sys.stderr)
        return 0  # don't mask the original failure
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok = True
    for chat_id in targets:
        data = json.dumps(
            {"chat_id": chat_id, "text": msg, "disable_web_page_preview": True}
        ).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 — fixed TG host
                ok = ok and (r.status == 200)
        except Exception as e:  # noqa: BLE001 — best-effort alert
            print(f"ops_alert: send failed for {chat_id}: {e}", file=sys.stderr)
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
