"""Focused checks for the production deployment contract."""

import re
from pathlib import Path

from app.config import Settings


PROJECT_ROOT = Path(__file__).parent.parent


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def _service_block(compose: str, service: str) -> str:
    match = re.search(
        rf"^  {re.escape(service)}:\n(?P<body>.*?)(?=^  [a-z][a-z0-9_-]*:\n|^volumes:\n)",
        compose,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match, f"missing Compose service: {service}"
    return match.group("body")


def test_dockerfile_runs_non_root_and_propagates_signals():
    dockerfile = _read("Dockerfile")
    assert "USER appuser" in dockerfile
    assert "${PORT:-8000}" in dockerfile
    assert "exec uvicorn" in dockerfile


def test_compose_keeps_app_and_tls_redis_private_and_bounded():
    compose = _read("docker-compose.yml")
    for service in ("caddy", "api", "worker", "redis", "receiver"):
        block = _service_block(compose, service)
        assert "mem_limit:" in block
        assert "cpus:" in block

    api = _service_block(compose, "api")
    worker = _service_block(compose, "worker")
    redis = _service_block(compose, "redis")
    caddy = _service_block(compose, "caddy")
    assert "ports:" not in api
    assert "ports:" not in worker
    assert "ports:" not in redis
    assert "rediss://redis:6379" in api
    assert "rediss://redis:6379" in worker
    assert "--tls-port" in redis
    assert "--port\n      - \"0\"" in redis
    assert "redis_data:/data" in redis
    assert "--appendonly" in redis
    assert "--maxmemory" in redis
    assert "image: redis:7.4.10-alpine" in redis
    assert 'user: "999"' in redis
    assert "image: caddy:2.11.4-alpine" in caddy
    assert 'user: "65534:65534"' in caddy
    assert "NET_BIND_SERVICE" in caddy


def test_compose_bounds_worker_and_smoke_receiver():
    compose = _read("docker-compose.yml")
    worker = _service_block(compose, "worker")
    receiver = _service_block(compose, "receiver")
    assert "WORKER_CONCURRENCY: ${WORKER_CONCURRENCY:-8}" in worker
    assert 'profiles: ["smoke"]' in receiver
    assert "SMOKE_CONTROL_SECRET" in receiver
    assert "OUTBOUND_WEBHOOK_SECRET" in receiver


def test_caddy_uses_domain_for_automatic_https_and_routes_smoke_receiver():
    caddyfile = _read("Caddyfile")
    assert "{$DOMAIN}" in caddyfile
    assert not re.search(r"^\s*:80\s*\{", caddyfile, flags=re.MULTILINE)
    assert "reverse_proxy api:8000" in caddyfile
    assert "handle_path /_smoke-receiver/*" in caddyfile
    assert "reverse_proxy receiver:9090" in caddyfile


def test_scripts_configure_swap_tls_readiness_and_real_deployment():
    bootstrap = _read("scripts/bootstrap_host.sh")
    deploy = _read("scripts/deploy.sh")
    assert "swapon" in bootstrap
    assert "vm.swappiness=10" in bootstrap
    assert "server.crt" in bootstrap and "ca.crt" in bootstrap
    assert "docker compose" in deploy
    assert 'READY_URL="https://${DOMAIN}/readyz"' in deploy
    assert "settings.environment == \"production\"" in deploy


def test_ci_deployment_is_gated_and_executes_public_smoke_test():
    workflow = _read(".github/workflows/ci.yml")
    deploy = workflow.split("  deploy:\n", 1)[1]
    assert "needs: [test, build]" in deploy
    assert "vars.DEPLOY_ENABLED == 'true'" in deploy
    assert "environment:\n      name: production" in deploy
    assert "git checkout --detach '$GITHUB_SHA'" in deploy
    assert "python scripts/smoke_test.py" in deploy
    assert "docker compose --profile smoke rm -f receiver" in deploy


def test_secret_examples_are_blank_and_smoke_receiver_is_in_image():
    example = _read(".env.example")
    for name in (
        "WEBHOOK_SECRET",
        "OUTBOUND_WEBHOOK_SECRET",
        "CLI_ADMIN_KEY",
        "SMOKE_CONTROL_SECRET",
    ):
        assert re.search(rf"^{name}=\s*$", example, flags=re.MULTILINE)
    dockerignore = _read(".dockerignore")
    assert "!scripts/controlled_receiver.py" in dockerignore


def test_worker_concurrency_is_bounded_by_configuration():
    configured = Settings(worker_concurrency=4)
    assert configured.worker_concurrency == 4
    worker = _read("app/workers/delivery_worker.py")
    assert "DeliveryWorker(concurrency=settings.worker_concurrency)" in worker
