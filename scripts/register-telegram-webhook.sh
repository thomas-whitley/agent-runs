#!/usr/bin/env bash
# Points the Telegram bot at the api's /telegram endpoint, with the secret
# Telegram will send back on every call. Reads TELEGRAM_BOT_TOKEN and
# TELEGRAM_WEBHOOK_SECRET from the environment or from .env in the repo root,
# and prints Telegram's answer, never either value.
#
#   scripts/register-telegram-webhook.sh https://<api host>
#
# The same two values must be in the private repo's secrets, or the deployed
# api refuses Telegram's calls with 401. Once a webhook is set, getUpdates
# returns nothing, so read the chat id before running this.
set -euo pipefail

api_url="${1:?usage: $0 https://<api host>}"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"

read_env() {
  local name="$1"
  if [ -n "${!name:-}" ]; then
    printf '%s' "${!name}"
  elif [ -f "$repo_root/.env" ]; then
    { grep -E "^${name}=" "$repo_root/.env" || true; } | tail -n 1 | cut -d= -f2-
  fi
}

token="$(read_env TELEGRAM_BOT_TOKEN)"
secret="$(read_env TELEGRAM_WEBHOOK_SECRET)"
[ -n "$token" ] || { echo "TELEGRAM_BOT_TOKEN is not set" >&2; exit 1; }
[ -n "$secret" ] || { echo "TELEGRAM_WEBHOOK_SECRET is not set" >&2; exit 1; }

body="$(python3 -c '
import json, sys
print(json.dumps({
    "url": sys.argv[1].rstrip("/") + "/telegram",
    "secret_token": sys.argv[2],
    "allowed_updates": ["message"],
    "drop_pending_updates": True,
}))' "$api_url" "$secret")"

# The token is in the URL, so curl reads it from stdin config rather than argv,
# where any other user on the machine could see it in the process list.
answer() {
  local method="$1" data="${2:-}"
  printf 'url = "https://api.telegram.org/bot%s/%s"\n' "$token" "$method" |
    if [ -n "$data" ]; then
      curl -sS --max-time 20 -K - -H 'Content-Type: application/json' -d "$data"
    else
      curl -sS --max-time 20 -K -
    fi
}

answer setWebhook "$body" | python3 -c 'import json,sys; r=json.load(sys.stdin); print("setWebhook:", r.get("description") or r.get("ok"))'
answer getWebhookInfo | python3 -c '
import json, sys
info = json.load(sys.stdin).get("result", {})
print("url:", info.get("url"))
print("pending updates:", info.get("pending_update_count"))
if info.get("last_error_message"):
    print("last error:", info["last_error_message"])'
