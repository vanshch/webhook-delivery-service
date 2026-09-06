#!/usr/bin/env bash
set -uo pipefail

OUTPUT_DIR=${1:?output directory is required}
DURATION_SECONDS=${2:-86400}
INTERVAL_SECONDS=${3:-300}
READY_URL=${4:?readiness URL is required}

mkdir -p "$OUTPUT_DIR"
OUTPUT_FILE="$OUTPUT_DIR/host-samples.log"
START_EPOCH=$(date +%s)
END_EPOCH=$((START_EPOCH + DURATION_SECONDS))

exec >>"$OUTPUT_FILE" 2>&1

echo "SOAK_START_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "HOST=$(hostname)"
echo "REVISION=$(git rev-parse HEAD 2>/dev/null || echo unknown)"
docker images --format 'IMAGE {{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}'

while [ "$(date +%s)" -lt "$END_EPOCH" ]; do
    echo "SAMPLE_START_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    uptime
    free -m
    swapon --show
    df -h /
    docker ps --format 'CONTAINER {{.Names}}|{{.Status}}|{{.Image}}'
    docker stats --no-stream --format 'STATS {{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}|{{.NetIO}}|{{.BlockIO}}'
    for container in webhook_delivery-api-1 webhook_delivery-worker-1 webhook_delivery-redis-1 webhook_delivery-caddy-1 webhook_delivery-receiver-1; do
        docker inspect --format 'STATE {{.Name}}|status={{.State.Status}}|started={{.State.StartedAt}}|restart={{.RestartCount}}|oom={{.State.OOMKilled}}|exit={{.State.ExitCode}}' "$container" 2>&1 || true
    done
    docker exec webhook_delivery-redis-1 redis-cli --tls --cacert /tls/ca.crt -h redis INFO memory | grep -E '^(used_memory:|used_memory_peak:|used_memory_rss:|maxmemory:|mem_fragmentation_ratio:)'
    docker exec webhook_delivery-redis-1 redis-cli --tls --cacert /tls/ca.crt -h redis INFO persistence | grep -E '^(aof_enabled:|aof_current_size:|aof_base_size:|aof_last_bgrewrite_status:|rdb_last_bgsave_status:)'
    docker exec webhook_delivery-redis-1 redis-cli --tls --cacert /tls/ca.crt -h redis XLEN webhook_stream | sed 's/^/QUEUE stream_length=/'
    docker exec webhook_delivery-redis-1 redis-cli --tls --cacert /tls/ca.crt -h redis XPENDING webhook_stream webhook_workers | head -1 | sed 's/^/QUEUE pending=/'
    docker exec webhook_delivery-redis-1 redis-cli --tls --cacert /tls/ca.crt -h redis ZCARD webhook_delay_queue | sed 's/^/QUEUE delayed=/'
    docker exec webhook_delivery-redis-1 redis-cli --tls --cacert /tls/ca.crt -h redis LLEN webhook_dlq | sed 's/^/QUEUE dlq=/'
    docker exec webhook_delivery-redis-1 redis-cli --tls --cacert /tls/ca.crt -h redis LLEN webhook_quarantine | sed 's/^/QUEUE quarantine=/'
    du -sh /var/lib/docker/containers /var/lib/docker/volumes/webhook_delivery_redis_data 2>&1 | sed 's/^/STORAGE /'
    readiness=$(curl --fail --silent --show-error --max-time 10 "$READY_URL" 2>&1) && ready_status=ok || ready_status=failed
    echo "READINESS status=$ready_status response=$readiness"
    echo "SAMPLE_END"
    sleep "$INTERVAL_SECONDS"
done

echo "SOAK_END_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
