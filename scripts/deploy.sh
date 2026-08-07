#!/usr/bin/env bash
set -euo pipefail

ENV_FILE=${ENV_FILE:-.env}
MAX_RETRIES=${MAX_RETRIES:-60}
RETRY_INTERVAL=${RETRY_INTERVAL:-5}

if [ ! -f "$ENV_FILE" ]; then
    echo "Missing deployment environment file: $ENV_FILE" >&2
    exit 1
fi
if [ "$(stat -c '%a' "$ENV_FILE")" != 600 ]; then
    echo "$ENV_FILE must have mode 600." >&2
    exit 1
fi

# Compose validates required interpolation without printing resolved secrets.
docker compose --env-file "$ENV_FILE" config --quiet
DOMAIN=$(sed -n 's/^DOMAIN=//p' "$ENV_FILE" | tail -n 1)
ENVIRONMENT=$(sed -n 's/^ENVIRONMENT=//p' "$ENV_FILE" | tail -n 1)
if [ "$ENVIRONMENT" != production ] || ! [[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]]; then
    echo "ENVIRONMENT=production and DOMAIN are required." >&2
    exit 1
fi
READY_URL="https://${DOMAIN}/readyz"

docker compose --env-file "$ENV_FILE" build
docker compose --env-file "$ENV_FILE" run --rm --no-deps api \
    python -c 'from app.config import settings; assert settings.environment == "production"'
docker compose --env-file "$ENV_FILE" up -d --remove-orphans

for ((attempt = 1; attempt <= MAX_RETRIES; attempt++)); do
    if curl --fail --silent --show-error --max-time 5 "$READY_URL" >/dev/null; then
        echo "Deployment ready at $READY_URL"
        exit 0
    fi
    sleep "$RETRY_INTERVAL"
done

echo "Deployment failed readiness; recent service logs follow." >&2
docker compose --env-file "$ENV_FILE" logs --tail 50 api worker caddy redis >&2
exit 1
