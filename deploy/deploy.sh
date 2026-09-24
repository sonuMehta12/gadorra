#!/usr/bin/env bash
# Pull the latest code, rebuild, restart, and wait until /health is ok.
#   bash deploy/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

# docker-compose.azure.yml when the database is Azure's managed Postgres,
# docker-compose.prod.yml when it runs in Docker on this VM
FILE="$(grep -E '^COMPOSE_FILE=' .env | cut -d= -f2- || true)"
FILE="${FILE:-docker-compose.prod.yml}"
echo "==> using $FILE"

REQUIRED="JWT_SECRET WHATSAPP_TOKEN RAZORPAY_KEY_ID RAZORPAY_KEY_SECRET"
case "$FILE" in
  *azure*) REQUIRED="$REQUIRED DATABASE_URL" ;;
  *)       REQUIRED="$REQUIRED POSTGRES_PASSWORD" ;;
esac

for key in $REQUIRED; do
  value="$(grep -E "^${key}=" .env | cut -d= -f2- || true)"
  if [ -z "$value" ] || [ "$value" = "change-me-in-production" ]; then
    echo "!! $key is empty in .env -- fill it in first"; exit 1
  fi
done

echo "==> pulling latest code"
git pull --ff-only

echo "==> building and starting"
docker compose -f "$FILE" up -d --build

echo "==> waiting for /health"
for i in $(seq 1 30); do
  body="$(curl -fsS http://localhost:8000/health 2>/dev/null || true)"
  if echo "$body" | grep -q '"status":"ok"'; then
    echo "$body"; echo; echo "API is up on :8000"; exit 0
  fi
  sleep 2
done
echo "!! /health never came up ok. Last response: ${body:-none}"
docker compose -f "$FILE" logs --tail 60 api
exit 1
