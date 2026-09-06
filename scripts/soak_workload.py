"""Run a bounded production soak workload against the public deployment."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import statistics
import sys
import time
import uuid
from pathlib import Path

import httpx

from smoke_test import run_smoke_tests


def _signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _write_record(output: Path, record: dict) -> None:
    line = json.dumps(record, separators=(",", ":"), sort_keys=True)
    print(line, flush=True)
    with output.open("a", encoding="utf-8") as handle:
        handle.write(f"{line}\n")


def _run_burst(
    client: httpx.Client,
    *,
    api_url: str,
    receiver_url: str,
    webhook_secret: str,
    burst_size: int,
    timeout: float,
) -> dict:
    event_ids: list[str] = []
    latencies_ms: list[float] = []
    accepted = 0

    for index in range(burst_size):
        event_id = f"soak-{uuid.uuid4().hex}"
        payload = {
            "id": event_id,
            "event_type": "soak.delivery",
            "payload": {"source": "oracle-soak", "index": index},
            "target_url": f"{receiver_url}/webhook",
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        started = time.monotonic()
        response = client.post(
            f"{api_url}/webhooks",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Signature": _signature(webhook_secret, body),
            },
        )
        latencies_ms.append((time.monotonic() - started) * 1000)
        if response.status_code == 202:
            accepted += 1
            event_ids.append(event_id)

    pending = set(event_ids)
    deadline = time.monotonic() + timeout
    while pending and time.monotonic() < deadline:
        for event_id in tuple(pending):
            try:
                response = client.get(f"{api_url}/deliveries/{event_id}")
            except httpx.HTTPError:
                continue
            if response.status_code == 200 and response.json().get("status") == "DELIVERED":
                pending.remove(event_id)
        if pending:
            time.sleep(0.5)

    ordered = sorted(latencies_ms)
    p95_index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95) - 1))
    return {
        "requested": burst_size,
        "accepted": accepted,
        "delivered": len(event_ids) - len(pending),
        "pending": len(pending),
        "ingest_ms_mean": round(statistics.fmean(latencies_ms), 2),
        "ingest_ms_p95": round(ordered[p95_index], 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--receiver-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=int, default=86_400)
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--burst-size", type=int, default=100)
    parser.add_argument("--cycle-timeout", type=float, default=180)
    parser.add_argument("--skip-smoke", action="store_true")
    args = parser.parse_args()

    webhook_secret = os.environ.get("WEBHOOK_SECRET", "")
    control_secret = os.environ.get("SMOKE_CONTROL_SECRET", "")
    if not webhook_secret or not control_secret:
        parser.error("WEBHOOK_SECRET and SMOKE_CONTROL_SECRET are required")
    if args.duration <= 0 or args.interval <= 0 or args.burst_size <= 0:
        parser.error("duration, interval, and burst-size must be positive")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    started_at = time.time()
    deadline = time.monotonic() + args.duration
    cycle = 0
    failed_cycles = 0

    with httpx.Client(timeout=15.0, follow_redirects=False) as client:
        while time.monotonic() < deadline:
            cycle_started = time.monotonic()
            cycle += 1
            record: dict = {
                "type": "cycle",
                "cycle": cycle,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            try:
                smoke_ok = args.skip_smoke or run_smoke_tests(
                    api_url=args.api_url,
                    receiver_url=args.receiver_url,
                    webhook_secret=webhook_secret,
                    control_secret=control_secret,
                    timeout=args.cycle_timeout,
                    client=client,
                )
                burst = _run_burst(
                    client,
                    api_url=args.api_url.rstrip("/"),
                    receiver_url=args.receiver_url.rstrip("/"),
                    webhook_secret=webhook_secret,
                    burst_size=args.burst_size,
                    timeout=args.cycle_timeout,
                )
                cycle_ok = smoke_ok and burst["accepted"] == args.burst_size and burst["pending"] == 0
                record.update({"ok": cycle_ok, "smoke_ok": smoke_ok, "burst": burst})
                if not cycle_ok:
                    failed_cycles += 1
            except Exception as exc:
                failed_cycles += 1
                record.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            record["cycle_seconds"] = round(time.monotonic() - cycle_started, 2)
            _write_record(args.output, record)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(max(0.0, args.interval - (time.monotonic() - cycle_started)), remaining))

    _write_record(
        args.output,
        {
            "type": "summary",
            "started_at_epoch": started_at,
            "ended_at_epoch": time.time(),
            "cycles": cycle,
            "failed_cycles": failed_cycles,
            "ok": failed_cycles == 0,
        },
    )
    return 0 if failed_cycles == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
