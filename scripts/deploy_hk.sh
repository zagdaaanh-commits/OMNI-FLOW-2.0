#!/usr/bin/env bash
# OmniFlow one-click deploy for an Alibaba Cloud HK / Tencent Cloud HK Ubuntu 22.04/24.04 instance.
#
#   chmod +x scripts/deploy_hk.sh
#   ./scripts/deploy_hk.sh [--skip-pull] [--skip-build] [--no-cert] [--dry-run]
#
# What it does: preflight -> install Docker if missing -> git pull -> build -> migrate ->
# certificate bootstrap -> zero-downtime rolling restart of `app` (auto-rollback on failure)
# -> restart `worker` -> smoke tests. Safe to re-run at any time.
set -Eeuo pipefail

COMPOSE_FILE="docker-compose.prod.yml"
PROJECT="omniflow"
CERT_NAME="omniflow"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"

SKIP_PULL=0; SKIP_BUILD=0; NO_CERT=0; DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --skip-pull)  SKIP_PULL=1 ;;
    --skip-build) SKIP_BUILD=1 ;;
    --no-cert)    NO_CERT=1 ;;
    --dry-run)    DRY_RUN=1 ;;
    -h|--help)    sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

log()  { printf '%s [deploy] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die()  { log "ERROR: $*" >&2; exit 1; }
trap 'log "ERROR: command failed (line $LINENO): $BASH_COMMAND" >&2' ERR

run() {  # print, and execute unless --dry-run
  if [[ $DRY_RUN -eq 1 ]]; then log "DRY-RUN: $*"; return 0; fi
  "$@"
}

cd "$(dirname "${BASH_SOURCE[0]}")/.."
ROOT="$(pwd)"

# ----------------------------------------------------------------- preflight --
log "Preflight checks in $ROOT"
[[ -f "$COMPOSE_FILE" ]] || die "$COMPOSE_FILE not found; run from the repository."
[[ -f .env ]] || die ".env is missing. Copy .env.example to .env and fill it in (DOMAIN, LETSENCRYPT_EMAIL, APP_SECRET_KEY, REDIS_PASSWORD, DATABASE_URL, API keys)."

env_val() { grep -E "^$1=" .env | tail -n1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//'; }
DOMAIN="$(env_val DOMAIN)"; EMAIL="$(env_val LETSENCRYPT_EMAIL)"
for var in DOMAIN LETSENCRYPT_EMAIL APP_SECRET_KEY REDIS_PASSWORD DATABASE_URL; do
  [[ -n "$(env_val "$var")" ]] || die "$var is empty in .env"
done
SECRET="$(env_val APP_SECRET_KEY)"
[[ ${#SECRET} -ge 32 ]] || die "APP_SECRET_KEY must be at least 32 characters (try: openssl rand -hex 32)"

if [[ -r /etc/os-release ]]; then
  . /etc/os-release
  [[ "${ID:-}" == "ubuntu" ]] || log "WARNING: tested on Ubuntu, found ${PRETTY_NAME:-unknown}"
fi

SUDO=""; [[ $EUID -ne 0 ]] && SUDO="sudo"
if [[ -n "$SUDO" ]] && ! command -v sudo >/dev/null; then die "Run as root or install sudo."; fi

# --------------------------------------------------------------------- docker --
if ! command -v docker >/dev/null 2>&1; then
  log "Installing Docker Engine + compose plugin (official apt repository)"
  run $SUDO apt-get update -y
  run $SUDO apt-get install -y ca-certificates curl gnupg git
  run $SUDO install -m 0755 -d /etc/apt/keyrings
  run bash -c "curl -fsSL https://download.docker.com/linux/ubuntu/gpg | $SUDO gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg"
  run bash -c "echo \"deb [arch=\$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \$(. /etc/os-release && echo \$VERSION_CODENAME) stable\" | $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null"
  run $SUDO apt-get update -y
  run $SUDO apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  run $SUDO systemctl enable --now docker
fi
DOCKER="docker"
if [[ $DRY_RUN -eq 0 ]] && ! docker info >/dev/null 2>&1; then DOCKER="$SUDO docker"; fi
COMPOSE="$DOCKER compose -p $PROJECT -f $COMPOSE_FILE"
[[ $DRY_RUN -eq 1 ]] || $COMPOSE version >/dev/null || die "docker compose plugin not available"

# ------------------------------------------------------------------ git update --
if [[ $SKIP_PULL -eq 0 ]]; then
  BRANCH="${DEPLOY_BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
  log "Updating code from origin/$BRANCH"
  run git fetch --prune origin
  run git checkout "$BRANCH"
  run git pull --ff-only origin "$BRANCH"
else
  log "Skipping git pull"
fi

# ---------------------------------------------------------------------- build --
if [[ $SKIP_BUILD -eq 0 ]]; then
  log "Building image (dependencies are compiled to wheels inside the build)"
  run $COMPOSE build app
fi

log "Starting redis"
run $COMPOSE up -d redis

# ------------------------------------------------------------------- migrations --
log "Running database migrations"
run $COMPOSE run --rm --no-deps -e SCHEDULER_MODE=off app python scripts/migrate.py

# ------------------------------------------------------------------ certificates --
LE_VOLUME="${PROJECT}_letsencrypt"
cert_present() {
  $DOCKER run --rm -v "$LE_VOLUME:/le:ro" alpine sh -c "test -f /le/live/$CERT_NAME/fullchain.pem"
}
cert_is_real() {
  $DOCKER run --rm -v "$LE_VOLUME:/le:ro" alpine sh -c "test -d /le/archive/$CERT_NAME"
}

if [[ $NO_CERT -eq 0 ]]; then
  if [[ $DRY_RUN -eq 1 ]] || ! cert_present; then
    log "No certificate yet: creating a temporary self-signed one so nginx can start"
    run $DOCKER volume create "$LE_VOLUME"
    run $DOCKER run --rm -v "$LE_VOLUME:/etc/letsencrypt" --entrypoint sh alpine/openssl -c \
      "mkdir -p /etc/letsencrypt/live/$CERT_NAME && openssl req -x509 -nodes -newkey rsa:2048 -days 2 -keyout /etc/letsencrypt/live/$CERT_NAME/privkey.pem -out /etc/letsencrypt/live/$CERT_NAME/fullchain.pem -subj /CN=$DOMAIN"
    BOOTSTRAP_CERT=1
  fi
fi

# ------------------------------------------------- zero-downtime rolling restart --
container_ids() { $COMPOSE ps -q app 2>/dev/null | sort || true; }
health_of()     { $DOCKER inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$1" 2>/dev/null || echo "gone"; }

rolling_restart_app() {
  local old_ids count target new_ids deadline healthy id
  old_ids="$(container_ids)"
  count=$(printf '%s\n' "$old_ids" | grep -c . || true)
  if [[ "$count" -eq 0 ]]; then
    log "No running app containers: plain start"
    run $COMPOSE up -d --no-deps app
    return 0
  fi
  target=$((count * 2))
  log "Rolling restart: $count running replica(s) -> starting $count new ones alongside"
  run $COMPOSE up -d --no-deps --no-recreate --scale app="$target" app
  [[ $DRY_RUN -eq 1 ]] && return 0

  new_ids="$(comm -13 <(printf '%s\n' "$old_ids") <(container_ids))"
  [[ -n "$new_ids" ]] || die "Scaling up did not create new containers"

  deadline=$((SECONDS + HEALTH_TIMEOUT))
  while :; do
    healthy=1
    for id in $new_ids; do
      case "$(health_of "$id")" in
        healthy) ;;
        unhealthy|exited|dead|gone) healthy=-1; break ;;
        *) healthy=0 ;;
      esac
    done
    [[ $healthy -eq 1 ]] && break
    if [[ $healthy -eq -1 || $SECONDS -ge $deadline ]]; then
      log "New containers did not become healthy; ROLLING BACK (old replicas keep serving)"
      for id in $new_ids; do $DOCKER logs --tail 40 "$id" 2>&1 | sed 's/^/    /' || true; done
      # shellcheck disable=SC2086
      $DOCKER rm -f $new_ids >/dev/null
      die "Deploy aborted: new version unhealthy. The previous version is still running."
    fi
    sleep 3
  done
  log "New replicas healthy; retiring old ones"
  # shellcheck disable=SC2086
  $DOCKER stop -t 30 $old_ids >/dev/null
  # shellcheck disable=SC2086
  $DOCKER rm $old_ids >/dev/null
  $COMPOSE up -d --no-deps --no-recreate --scale app="$count" app
}

rolling_restart_app

log "Starting nginx"
run $COMPOSE up -d nginx certbot

if [[ ${BOOTSTRAP_CERT:-0} -eq 1 ]]; then
  log "Requesting a real Let's Encrypt certificate for $DOMAIN"
  # certbot refuses to run while a non-lineage live/ directory exists, so remove the dummy first
  # (nginx keeps serving the cert it already loaded in memory).
  run $DOCKER run --rm -v "$LE_VOLUME:/etc/letsencrypt" alpine sh -c "rm -rf /etc/letsencrypt/live/$CERT_NAME /etc/letsencrypt/archive/$CERT_NAME /etc/letsencrypt/renewal/$CERT_NAME.conf"
  run $COMPOSE run --rm --entrypoint certbot certbot certonly --webroot -w /var/www/certbot \
      --cert-name "$CERT_NAME" -d "$DOMAIN" --email "$EMAIL" --agree-tos --no-eff-email --non-interactive
  run $COMPOSE exec -T nginx nginx -s reload
fi

log "Reloading nginx so it re-resolves the app replicas"
run $COMPOSE exec -T nginx nginx -s reload

# The scheduler is DB-backed, so a short worker restart loses nothing: due posts run on the next poll.
log "Restarting scheduler worker"
run $COMPOSE up -d --no-deps --force-recreate worker

# ------------------------------------------------------------------ smoke tests --
if [[ $DRY_RUN -eq 0 ]]; then
  log "Smoke test: https://$DOMAIN/health"
  ok=0
  for _ in $(seq 1 20); do
    if curl -fsS --max-time 8 "https://$DOMAIN/health" >/dev/null 2>&1 || curl -fsSk --max-time 8 "https://127.0.0.1/health" -H "Host: $DOMAIN" >/dev/null 2>&1; then ok=1; break; fi
    sleep 3
  done
  [[ $ok -eq 1 ]] || die "Health check failed after deploy. Inspect: $COMPOSE logs --tail=100 app nginx"
  log "Network diagnostics (Meta Graph API / Zhipu reachability from this server):"
  curl -fsS --max-time 20 "https://$DOMAIN/health/network" 2>/dev/null || curl -fsSk --max-time 20 "https://127.0.0.1/health/network" -H "Host: $DOMAIN" || log "WARNING: /health/network unavailable"
  echo
  run $DOCKER image prune -f >/dev/null
fi

log "Deploy finished successfully."
