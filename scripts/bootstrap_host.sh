#!/usr/bin/env bash
set -euo pipefail

# Bootstrap an x86 Ubuntu LTS VM for the single-host Docker deployment.
if [ "$(id -u)" -ne 0 ]; then
    echo "Run this script as root." >&2
    exit 1
fi
if [ "$(uname -m)" != "x86_64" ]; then
    echo "This deployment is validated only for x86_64 hosts." >&2
    exit 1
fi

SWAP_SIZE_MB=${SWAP_SIZE_MB:-2048}
REDIS_TLS_DIR=${REDIS_TLS_DIR:-/etc/webhook-delivery/redis-tls}

if [ -z "$(swapon --noheadings --show=NAME)" ]; then
    fallocate -l "${SWAP_SIZE_MB}M" /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
printf 'vm.swappiness=10\n' > /etc/sysctl.d/99-webhook-delivery.conf
sysctl --system >/dev/null

apt-get update
apt-get install -y ca-certificates curl git openssl
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    . /etc/os-release
    printf '%s\n' \
        "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker

install -d -m 0755 "$REDIS_TLS_DIR"
if [ ! -s "$REDIS_TLS_DIR/ca.crt" ] \
    || [ ! -s "$REDIS_TLS_DIR/ca.key" ] \
    || [ ! -s "$REDIS_TLS_DIR/server.crt" ] \
    || [ ! -s "$REDIS_TLS_DIR/server.key" ]; then
    openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 3650 \
        -subj '/CN=webhook-delivery-redis-ca' \
        -keyout "$REDIS_TLS_DIR/ca.key" \
        -out "$REDIS_TLS_DIR/ca.crt"
    openssl req -new -newkey rsa:2048 -sha256 -nodes \
        -subj '/CN=redis' \
        -addext 'subjectAltName=DNS:redis' \
        -keyout "$REDIS_TLS_DIR/server.key" \
        -out "$REDIS_TLS_DIR/server.csr"
    openssl x509 -req -sha256 -days 825 \
        -in "$REDIS_TLS_DIR/server.csr" \
        -CA "$REDIS_TLS_DIR/ca.crt" \
        -CAkey "$REDIS_TLS_DIR/ca.key" \
        -CAcreateserial \
        -copy_extensions copy \
        -out "$REDIS_TLS_DIR/server.crt"
    rm -f "$REDIS_TLS_DIR/server.csr" "$REDIS_TLS_DIR/ca.srl"
fi
chmod 0644 "$REDIS_TLS_DIR/ca.crt" "$REDIS_TLS_DIR/server.crt"
chmod 0600 "$REDIS_TLS_DIR/ca.key" "$REDIS_TLS_DIR/server.key"
chown 999:999 "$REDIS_TLS_DIR/server.key"

if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    usermod -aG docker "$SUDO_USER"
fi

docker version >/dev/null
docker compose version
echo "Host ready. Log in again before using Docker without sudo."
