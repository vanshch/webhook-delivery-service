# Oracle Cloud single-VM deployment runbook

This runbook documents the single-VM deployment architecture on Oracle Cloud, host bootstrapping, private TLS Redis configuration, operational verification, CI/CD deployment gating, and the completed 24-hour production soak evidence.

## Host and DNS

1. Provision an x86_64 Ubuntu LTS VM on Oracle Cloud (standard compute shape with 954 MiB usable RAM) with inbound TCP 22 restricted to authorized operator/CI IPs and public TCP 80/443 open.
2. Point the chosen domain or wildcard DNS hostname (e.g. `130-210-1-239.sslip.io`) to the VM's public IP address before starting Caddy.
3. Run `sudo scripts/bootstrap_host.sh` once. It installs Docker and Docker Compose, allocates a 2 GB swap file if none exists, configures `vm.swappiness=10`, and generates a private Redis CA and server certificate under `/etc/webhook-delivery/redis-tls`.
4. Clone this repository to `/opt/webhook_delivery`, owned by the unprivileged deployment user.

## Production configuration

Create `/opt/webhook_delivery/.env` with mode `600`. Start from `.env.example` and configure the following required production settings:

- `ENVIRONMENT=production`
- `DOMAIN` set to the public DNS hostname (e.g. `130-210-1-239.sslip.io`)
- `REDIS_TLS_DIR=/etc/webhook-delivery/redis-tls`
- Distinct cryptographically random secrets of at least 32 characters for `WEBHOOK_SECRET`, `OUTBOUND_WEBHOOK_SECRET`, `CLI_ADMIN_KEY`, and `SMOKE_CONTROL_SECRET` (inbound and outbound secrets must not match)
- `ALLOWED_TARGET_HOSTS` set to a JSON array containing the permitted webhook delivery target hostnames (e.g. `["130-210-1-239.sslip.io"]`)
- `DEFAULT_TARGET_URL` (optional HTTPS URL) if a fallback target is needed
- `WORKER_CONCURRENCY=8` (bounded worker concurrency suitable for 1 GB RAM hosts)

> **Security Invariant**: Never commit `.env`, SSH private keys, or Redis TLS private keys to source control.

## Deployment execution

Run `./scripts/deploy.sh` to deploy:

1. Validates `.env` file permissions (mode 600) and required production variables.
2. Builds the container images with non-root runtime users.
3. Starts the API, worker, private TLS Redis (no exposed host ports), and Caddy reverse proxy via Docker Compose.
4. Polls `https://${DOMAIN}/readyz` until Redis connectivity and worker heartbeat are verified healthy.

### Manual Smoke Test

To verify end-to-end webhook delivery in production, temporarily spin up the controlled receiver:

```bash
docker compose --profile smoke up -d --no-build receiver
```

From a trusted operator machine, export `API_URL`, `RECEIVER_URL`, `WEBHOOK_SECRET`, and `SMOKE_CONTROL_SECRET`, then run:

```bash
python scripts/smoke_test.py --timeout 120
```

After verification, stop and remove the receiver container:

```bash
docker compose --profile smoke stop receiver
docker compose --profile smoke rm -f receiver
```

## GitHub deployment automation

The repository includes continuous deployment automation in `.github/workflows/ci.yml` under the `deploy` job:

- **Gating**: Automated deployment runs only on pushes to `main` when the repository variable `DEPLOY_ENABLED` is explicitly set to `'true'`.
- **Environment**: Configured via the protected `production` environment with variables `DEPLOY_HOST`, `DEPLOY_USER`, and `DEPLOY_DOMAIN`.
- **Secrets**: Requires `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS`, `WEBHOOK_SECRET`, and `SMOKE_CONTROL_SECRET`. `DEPLOY_KNOWN_HOSTS` must contain the VM's verified host key; dynamic `ssh-keyscan` is not permitted in CI.
- **Workflow Steps**: Deploys the tested commit SHA, verifies readiness, starts the temporary smoke receiver, runs the full public smoke test, and removes the smoke receiver.
- **Status**: The production soak run was verified on a live host deployed at SHA `7b56b44a4d2e742df0c63341b80e02681f1ad69b`. Automated CD remains opt-in and disabled by default until repository variables and secrets are configured.

## Verified Soak Acceptance Evidence

A 24-hour continuous production soak test was executed on the Oracle Cloud VM to validate memory stability, queue draining, crash recovery, and sustained delivery.

### Run Summary

- **Run ID**: `oracle-postfix-20260906T213502Z`
- **Interval**: 2026-09-06T21:35:05Z to 2026-09-07T21:38:43Z (86,400 workload seconds)
- **Deployed SHA**: `7b56b44a4d2e742df0c63341b80e02681f1ad69b`
- **Host Specs**: Oracle Cloud x86 Ubuntu LTS (954 MiB usable RAM, 2 GB swap)
- **Base URL**: `https://130-210-1-239.sslip.io`

### Workload & Health Results

- **Cycles**: 288 consecutive 5-minute cycles (100 events/burst per cycle + end-to-end smoke test).
- **Events Processed**: 28,800 requested, 28,800 accepted (100%), 28,800 delivered (100%), 0 pending at completion.
- **Smoke Tests**: 288 / 288 passes (100% success rate).
- **Failures & Anomalies**: 0 failed cycles, 0 OOM kills, 0 unexpected container restarts, 0 readiness probe failures, 0 dead-letter queue (DLQ) events, 0 quarantine events.

### Ingestion Latency Metrics

- **Cycle Mean Ingest Latency Average**: 48.19 ms
- **Cycle p95 Ingest Latency Average**: 165.79 ms
- *Note*: These figures measure API burst-ingest latency (POST `/webhooks` response time) across cycles and do not represent end-to-end delivery percentiles.

### Resource & Storage Bounds

- **Host RAM**: Started at 516 MiB, peaked at 621 MiB, ended at 587 MiB (well within 954 MiB capacity).
- **Host Swap**: Started at 214 MiB, ranged 212–265 MiB, ended at 258 MiB.
- **Redis Memory**: Started at 1.81 MB, peaked at 25.07 MB during bursts, ended at 17.23 MB at completion.
- **Queue Draining**: Stream length peaked at 24 and drained to 1; pending entries peaked at 11 and drained to 0; delayed queue ended at 0.
- **Disk / Storage**: Redis AOF log ended at 61.68 MB; Redis volume ended at 68 MB; Docker container logs ended at 56 MB.

### Provenance and Artifacts

The machine-readable summary is committed at `docs/evidence/oracle-postfix-20260906T213502Z.json`. Raw per-cycle logs generated by `scripts/soak_workload.py` and `scripts/collect_soak_evidence.sh` are retained on the VM at `/opt/webhook-soak-runs/oracle-postfix-20260906T213502Z` but are not committed to source control.
