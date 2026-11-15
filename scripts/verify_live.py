# SPDX-License-Identifier: AGPL-3.0-only
"""Check live ingress without sending a chat message or exposing credentials."""

import argparse
import hashlib
import json
import os
import time
from base64 import b64decode
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from rp_core.providers import post_json


def wait_receipt(event_id):
    repository = os.environ["GH_REPO"]
    credential = (
        os.environ.get("TENSHI_BOT_ACTION_TOKEN")
        or os.environ.get("RP_INSTANCE_TOKEN")
        or os.environ.get("GH_TOKEN")
    )
    receipt = hashlib.sha256(event_id.encode()).hexdigest()
    endpoint = f"https://api.github.com/repos/{repository}/contents/.rp/full-receipts/{receipt}.json?ref=main"
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        try:
            request = Request(
                endpoint,
                headers={
                    "Authorization": "Bearer " + credential,
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "RP-Core-cutover",
                },
            )
            with urlopen(request, timeout=30) as response:  # noqa: S310
                file = json.load(response)
            state = json.loads(b64decode(file["content"])).get("status")
            if state == "completed":
                return True
            if state == "uncertain":
                raise ValueError("Probe requires reconciliation")
        except HTTPError as error:
            if error.code != 404:
                raise
        time.sleep(5)
    raise ValueError("Private receipt was not completed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--control", choices=["probe", "drain"], default="probe")
    args = parser.parse_args()
    sync = urlsplit(os.environ.get("PERCEPTION_SCHEDULE_SYNC_URL", ""))
    if sync.scheme != "https" or not sync.netloc or sync.username or sync.password:
        raise SystemExit("A valid HTTPS schedule synchronization URL is required.")
    origin = f"https://{sync.netloc}"
    secret = os.environ.get("PERCEPTION_SCHEDULE_SYNC_SECRET", "")
    token = os.environ.get("TG_TOKEN", "")
    if not secret or not token:
        raise SystemExit("Live verification credentials are missing.")
    try:
        nonce = "cutover-" + os.environ.get("GITHUB_RUN_ID", "manual")
        api_headers = {"User-Agent": "RP-Core-live-check/1", "Cache-Control": "no-cache"}
        with urlopen(
            Request(origin + "/health?check=" + nonce, headers=api_headers), timeout=30
        ) as response:  # noqa: S310
            health = json.load(response)
        if health.get("service") != "rp-core":
            raise ValueError("Unexpected ingress")
        bot = post_json("https://api.telegram.org/bot" + token + "/getMe", {}, timeout=30)
        info = post_json("https://api.telegram.org/bot" + token + "/getWebhookInfo", {}, timeout=30)
        url = urlsplit(info.get("result", {}).get("url", ""))
        if bot.get("ok") is not True or info.get("ok") is not True or url.netloc != sync.netloc:
            raise ValueError("Bot or webhook mismatch")
        endpoint = "/internal/probe" if args.control == "probe" else "/internal/drain-inbox"
        payload = {"nonce": nonce}
        result = post_json(
            origin + endpoint,
            payload,
            headers={**api_headers, "Authorization": "Bearer " + secret},
            timeout=60,
        )
        should_wait = health.get("enabled") is True or os.environ.get("RP_EXPECT_RECEIPT") == "true"
        completed = (
            wait_receipt(result["event_id"]) if args.control == "probe" and should_wait else None
        )
    except Exception:
        raise SystemExit(
            "Live ingress verification failed; credentials and private responses were withheld."
        ) from None
    print(
        json.dumps(
            {
                "service": "rp-core",
                "enabled": health.get("enabled") is True,
                "bot_authenticated": True,
                "webhook_preserved": True,
                "pending_updates": info["result"].get("pending_update_count", 0),
                "control": args.control,
                "receipt_completed": completed,
                "event_id": result.get("event_id"),
                "queued": result.get("queued"),
                "scheduled_queued": result.get("scheduled_queued"),
                "chat_messages_sent": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
