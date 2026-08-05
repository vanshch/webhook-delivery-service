"""Protected CLI for Dead Letter Queue (DLQ) inspection, listing, and replay."""

import sys
import getpass
import hmac
import json
import asyncio
import argparse
from typing import List, Optional

from app.storage import redis_client
from app.config import settings
from app.core.dlq import (
    list_dlq_events,
    inspect_dlq_event,
    replay_dlq_event,
    replay_selected_events,
)


def verify_admin_key(admin_key: Optional[str]) -> bool:
    """Verify the configured CLI key without allowing an unprotected mode."""
    expected_key = settings.cli_admin_key.strip()
    if not expected_key or not admin_key:
        return False
    return hmac.compare_digest(admin_key, expected_key)


async def async_main(args: argparse.Namespace) -> int:
    if not verify_admin_key(args.admin_key):
        print("Error: Unauthorized. Invalid or missing admin key.", file=sys.stderr)
        return 1

    redis_conn = redis_client.get_async_redis()

    if args.command == "list":
        events = await list_dlq_events(redis_conn)
        print(f"Total DLQ events: {len(events)}")
        for idx, item in enumerate(events, 1):
            webhook = item.get("webhook", {})
            eid = webhook.get("id", "unknown")
            event_type = webhook.get("event_type", "unknown")
            last_attempt = item.get("last_attempt", "?")
            failed_at = item.get("failed_at", "?")
            print(f"[{idx}] ID: {eid} | Type: {event_type} | Attempts: {last_attempt} | FailedAt: {failed_at}")
        return 0

    elif args.command == "inspect":
        if not args.event_id:
            print("Error: --event-id is required for inspect command", file=sys.stderr)
            return 1
        item = await inspect_dlq_event(redis_conn, args.event_id)
        if not item:
            print(f"DLQ event '{args.event_id}' not found.", file=sys.stderr)
            return 1
        print(json.dumps(item, indent=2))
        return 0

    elif args.command == "replay":
        if not args.event_id:
            print("Error: --event-id is required for replay command", file=sys.stderr)
            return 1
        success = await replay_dlq_event(redis_conn, args.event_id)
        if success:
            print(f"Successfully replayed DLQ event '{args.event_id}'.")
            return 0
        else:
            print(f"Failed to replay DLQ event '{args.event_id}'.", file=sys.stderr)
            return 1

    elif args.command == "replay-selected":
        if not args.event_ids:
            print("Error: --event-ids list is required for replay-selected command", file=sys.stderr)
            return 1
        res = await replay_selected_events(redis_conn, args.event_ids)
        print(f"Replay batch results: Replayed={len(res['replayed'])}, Failed={len(res['failed'])}")
        print(json.dumps(res, indent=2))
        return 0 if not res["failed"] else 1

    else:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser(description="Protected DLQ Management CLI")
    parser.add_argument("command", choices=["list", "inspect", "replay", "replay-selected"], help="DLQ operation")
    parser.add_argument("--event-id", type=str, help="Single event ID for inspect or replay")
    parser.add_argument("--event-ids", nargs="+", help="Multiple event IDs for replay-selected")
    args = parser.parse_args()
    args.admin_key = getpass.getpass("DLQ admin key: ")
    code = asyncio.run(async_main(args))
    sys.exit(code)


if __name__ == "__main__":
    main()
