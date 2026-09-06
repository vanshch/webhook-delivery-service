# Azure single-VM deployment runbook

This runbook prepares the repository for roadmap commit 6. Do not mark the
deployment complete until the public smoke test and 24-hour soak criteria in
`COMMIT_ROADMAP.md` pass.

## Host and DNS

1. Provision an x86 Ubuntu LTS Azure VM with inbound TCP 22 restricted to the
   operator/CI source and public TCP 80/443 enabled.
2. Point the chosen DNS A/AAAA record at the VM before starting Caddy.
3. Run `sudo scripts/bootstrap_host.sh` once. It installs Docker, creates 2 GB
   swap when the host has none, and creates a private Redis CA/server certificate
   under `/etc/webhook-delivery/redis-tls`.
4. Clone this public repository to `/opt/webhook_delivery`, owned by the
   unprivileged deployment user.

## Production configuration

Create `/opt/webhook_delivery/.env` with mode `600`. Start from `.env.example`
and set at least:

- `ENVIRONMENT=production`
- `DOMAIN` to the public DNS name
- `REDIS_TLS_DIR=/etc/webhook-delivery/redis-tls`
- distinct random values of at least 32 characters for `WEBHOOK_SECRET`,
  `OUTBOUND_WEBHOOK_SECRET`, `CLI_ADMIN_KEY`, and `SMOKE_CONTROL_SECRET`
- `ALLOWED_TARGET_HOSTS` to a JSON array containing the controlled receiver
  host (the deployment domain when using the bundled receiver)
- an HTTPS `DEFAULT_TARGET_URL`, if a default is used
- `WORKER_CONCURRENCY=8` for the initial 1 GB VM soak

Never commit `.env`, the SSH key, or the Redis private keys.

## First deployment

Run `./scripts/deploy.sh`. It validates the production settings, builds the
images, starts API/worker/private TLS Redis/Caddy, and waits for the public HTTPS
readiness endpoint.

For a manual smoke test, temporarily start the receiver:

```bash
docker compose --profile smoke up -d --no-build receiver
```

From a trusted machine, export `API_URL`, `RECEIVER_URL`, `WEBHOOK_SECRET`, and
`SMOKE_CONTROL_SECRET`, then run `python scripts/smoke_test.py --timeout 120`.
Stop and remove the receiver afterward:

```bash
docker compose --profile smoke stop receiver
docker compose --profile smoke rm -f receiver
```

## GitHub deployment environment

Create the repository variable `DEPLOY_ENABLED=true`, then create a protected
`production` environment with variables `DEPLOY_HOST`, `DEPLOY_USER`, and
`DEPLOY_DOMAIN`. Set environment secrets
`DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS`, `WEBHOOK_SECRET`, and
`SMOKE_CONTROL_SECRET`. `DEPLOY_KNOWN_HOSTS` must contain the VM's verified SSH
host-key line; do not populate it with an unverified `ssh-keyscan` during CI.

When `DEPLOY_ENABLED=true`, a push to `main` deploys only after tests and the
image build pass, runs the public smoke test, and removes the temporary receiver.

## Soak acceptance

Run the roadmap's exact 24-hour soak on this VM/configuration. Capture
`docker stats`, `free -m`, `swapon --show`, container restart/OOM state, Redis
memory/AOF growth, disk usage, queue drain, readiness, delivery state, and crash
recovery. A repository commit is not evidence that deployment or soak passed.
