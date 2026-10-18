#!/usr/bin/env bash
# Pre-flight checks. Run on the server before the first start, and after any config change:
#
#     ./scripts/preflight.sh
#
# It only reads and checks: it builds the image and runs the bot's own `--check-only`, and it
# never sends an order. (If Telegram is configured, --check-only sends one test message.)
# Exit code 0 = no failures (warnings are advice); 1 = fix the FAIL lines first.
#
# PREFLIGHT_PING_URL overrides the Binance URL that is checked. It exists for testing this script.

set -u
cd "$(dirname "$0")/.." || exit 1

failures=0
warnings=0
ok()   { printf '  ok    %s\n' "$1"; }
warn() { printf '  WARN  %s\n' "$1"; warnings=$((warnings + 1)); }
bad()  { printf '  FAIL  %s\n' "$1"; failures=$((failures + 1)); }

# Value of KEY in .env, without surrounding quotes (the last assignment wins).
env_value() {
  grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | sed -e "s/^[\"']//" -e "s/[\"']\$//"
}

http_status() { curl -m 10 -s -o /dev/null -w '%{http_code}' "$1" 2>/dev/null || true; }

check_binance() { # name url
  local status
  status=$(http_status "$2")
  case "$status" in
    200) ok "$1 is reachable ($2)" ;;
    451 | 403) bad "$1 refuses this server (HTTP $status). Binance blocks restricted locations, including US IP addresses: use a server in another region" ;;
    000 | "") bad "cannot connect to $1 ($2): check the server's internet access and firewall" ;;
    *) warn "$1 answered HTTP $status ($2)" ;;
  esac
}

echo "Pre-flight checks"
echo

echo "Tools"
if command -v docker >/dev/null 2>&1; then ok "docker is installed"; else bad "docker is not installed"; fi
if docker compose version >/dev/null 2>&1; then ok "docker compose is available"; else bad "the docker compose plugin is missing"; fi
if command -v curl >/dev/null 2>&1; then ok "curl is installed"; else bad "curl is not installed (needed for these checks)"; fi

echo
echo "Configuration (.env)"
if [ ! -f .env ]; then
  bad ".env does not exist: run  cp .env.example .env  and fill it in"
else
  ok ".env exists"
  perm=$(stat -c %a .env 2>/dev/null || stat -f %Lp .env 2>/dev/null || echo "")
  if [ -n "$perm" ] && [ "${perm: -2}" != "00" ]; then
    warn ".env is readable by other users (mode $perm): run  chmod 600 .env"
  fi

  testnet=$(env_value BINANCE_TESTNET)
  dry_run=$(env_value DRY_RUN)
  : "${testnet:=true}"
  : "${dry_run:=true}"
  if [ "$dry_run" = "true" ]; then
    mode="DRY RUN (no orders are ever sent)"
  elif [ "$testnet" = "true" ]; then
    mode="TESTNET (orders go to the Binance testnet, fake money)"
  else
    mode="LIVE (REAL ORDERS WITH REAL MONEY)"
  fi
  echo "  mode  $mode"
  if [ "$dry_run" != "true" ] && [ "$testnet" != "true" ]; then
    warn "this configuration trades real money: make sure that is what you want (see SETUP_CHECKLIST.md, section 8)"
  fi

  db_url=$(env_value DATABASE_URL)
  if [ -z "$db_url" ]; then
    bad "DATABASE_URL is not set"
  elif printf '%s' "$db_url" | grep -qE '@(localhost|127\.0\.0\.1)([:/]|$)'; then
    bad "DATABASE_URL points at localhost, which inside Docker is the container itself. Use the database's real host (or host.docker.internal for Postgres on this same machine)"
  else
    ok "DATABASE_URL is set"
  fi
  if printf '%s' "$db_url" | grep -q ':changeme@'; then
    warn "DATABASE_URL still uses the example password 'changeme'"
  fi

  if [ "$dry_run" != "true" ] && { [ -z "$(env_value BINANCE_API_KEY)" ] || { [ -z "$(env_value BINANCE_API_SECRET)" ] && [ -z "$(env_value BINANCE_PRIVATE_KEY_PATH)" ]; }; }; then
    bad "DRY_RUN is false but the Binance API key and secret are not both set"
  fi

  if [ -n "$(env_value TELEGRAM_BOT_TOKEN)" ] && [ -n "$(env_value TELEGRAM_CHAT_ID)" ]; then
    ok "Telegram is configured"
  else
    warn "Telegram is not configured: alerts will only appear in the log (SETUP_CHECKLIST.md, section 3)"
  fi
fi

echo
echo "Network"
if command -v curl >/dev/null 2>&1; then
  if [ "${testnet:-true}" = "true" ] && [ -z "${PREFLIGHT_PING_URL:-}" ]; then
    check_binance "Binance testnet" "https://testnet.binance.vision/api/v3/ping"
  fi
  check_binance "Binance" "${PREFLIGHT_PING_URL:-https://api.binance.com/api/v3/ping}"
  ip=$(curl -m 8 -s https://api.ipify.org 2>/dev/null || true)
  if [ -n "$ip" ]; then
    ok "this server's public IP is $ip (the address to allow on the Binance API key)"
  else
    warn "could not determine this server's public IP (tried api.ipify.org)"
  fi
fi

echo
echo "The bot's own checks"
if [ "$failures" -gt 0 ]; then
  warn "skipped: fix the FAIL lines above first"
elif command -v docker >/dev/null 2>&1 && [ -f .env ]; then
  if docker compose build -q >/dev/null 2>&1; then
    ok "the image builds"
    output=$(docker compose run --rm -T app python -m bot.main --check-only 2>&1)
    code=$?
    if [ "$code" -eq 0 ]; then
      ok "python -m bot.main --check-only passed"
    else
      bad "python -m bot.main --check-only failed (exit $code). Last lines:"
      printf '%s\n' "$output" | grep -vE 'Warning|warnings.warn|WebSocketClientProtocol' | tail -12 | sed 's/^/        /'
    fi
  else
    bad "the image did not build: run  docker compose build  to see why"
  fi
fi

echo
if [ "$failures" -gt 0 ]; then
  echo "$failures failure(s), $warnings warning(s). Not ready."
  exit 1
fi
echo "No failures, $warnings warning(s). Start with:  docker compose up -d --build   then   docker compose logs -f app"
