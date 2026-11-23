#!/usr/bin/env bash
set -Eeuo pipefail

REPO=""
REF="main"
DOMAIN=""
INSTALL_DIR="/opt/dara-finance"
usage() { echo "Usage: install.sh --repo OWNER/REPOSITORY --domain finance.example.com [--ref main] [--dir /opt/dara-finance]"; }
while (($#)); do
  case "$1" in
    --repo) REPO="${2:-}"; shift 2 ;;
    --ref) REF="${2:-}"; shift 2 ;;
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --dir) INSTALL_DIR="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then echo "Run this installer through sudo." >&2; exit 1; fi
if [[ ! "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ || "$REPO" == *"/../"* ]]; then echo "Pass a valid GitHub OWNER/REPOSITORY with --repo." >&2; exit 2; fi
if [[ ! "$REF" =~ ^[A-Za-z0-9._/-]+$ || "$REF" == *..* ]]; then echo "Invalid Git ref." >&2; exit 2; fi
if [[ ! "$DOMAIN" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$ ]]; then echo "Pass a public DNS name such as finance.example.com." >&2; exit 2; fi
if [[ "$DOMAIN" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then echo "Use a DNS name; public certificates are not issued for this server setup using an IP address." >&2; exit 2; fi
case "${DOMAIN,,}" in localhost|*.localhost|*.local|*.internal|*.test) echo "Use a public DNS domain for Let’s Encrypt." >&2; exit 2 ;; esac

. /etc/os-release
version_ge() { [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" == "$2" ]]; }
case "$ID" in
  debian) version_ge "$VERSION_ID" "12" || { echo "Debian 12 or newer is required." >&2; exit 1; } ;;
  ubuntu) version_ge "$VERSION_ID" "22.04" || { echo "Ubuntu 22.04 or newer is required." >&2; exit 1; } ;;
  *) echo "Supported hosts are Debian 12+ and Ubuntu 22.04+." >&2; exit 1 ;;
esac

install_docker_repository() {
  apt-get update
  apt-get install -y ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' \
    "$(dpkg --print-architecture)" "$ID" "$VERSION_CODENAME" > /etc/apt/sources.list.d/docker.list
  apt-get update
}
if ! command -v docker >/dev/null 2>&1; then
  install_docker_repository
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin git openssl
  systemctl enable --now docker
else
  if ! docker compose version >/dev/null 2>&1; then
    install_docker_repository
    apt-get install -y docker-compose-plugin
  fi
  systemctl start docker 2>/dev/null || true
  docker info >/dev/null 2>&1 || { echo "Docker is installed but its daemon is not available. Start Docker and rerun." >&2; exit 1; }
fi
command -v git >/dev/null 2>&1 || { apt-get update && apt-get install -y git; }
command -v openssl >/dev/null 2>&1 || { apt-get update && apt-get install -y openssl; }
if [[ -e "$INSTALL_DIR" ]]; then echo "Install directory already exists: $INSTALL_DIR (choose another --dir or back it up first)." >&2; exit 1; fi
install -d -m 0750 "$(dirname "$INSTALL_DIR")"
git clone --depth 1 --branch "$REF" "https://github.com/$REPO.git" "$INSTALL_DIR"
cd "$INSTALL_DIR"
if [[ ! -f compose.yaml || ! -f Dockerfile || ! -f Caddyfile ]]; then echo "The selected GitHub ref does not contain the server deployment files at its root." >&2; exit 1; fi
umask 077
cat > .env <<ENV
DOMAIN=$DOMAIN
ALLOW_SIGNUP=1
MAX_ACCOUNTS=500
ENV
chmod 600 .env
install -d -m 0700 .secrets
openssl rand -hex 32 > .secrets/session_secret
chmod 0444 .secrets/session_secret
docker compose up -d --build
printf '\nDara Finance is starting at https://%s\n' "$DOMAIN"
printf 'DNS must point to this server and inbound ports 80 and 443 must be reachable.\n'
printf 'Check status with: cd %q && docker compose ps\n' "$INSTALL_DIR"
