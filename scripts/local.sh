#!/usr/bin/env bash
# Run the bot on this machine, in the background, with every feature.
#
#   ./scripts/local.sh start          use .env exactly as it is
#   ./scripts/local.sh start demo     TESTNET ONLY (fake money). Tuned so the whole cycle happens
#                                     in minutes: signals are frequent, targets are tiny, trades small
#   ./scripts/local.sh status         is it running, and the last log lines
#   ./scripts/local.sh logs           follow the log (Ctrl+C stops following, not the bot)
#   ./scripts/local.sh stop           clean shutdown (flushes alerts, closes the Binance stream)
#   ./scripts/local.sh restart [demo]
#
# It runs the checks first, keeps the Mac awake while the bot runs (a sleeping bot is a stopped
# bot), and writes to logs/bot.log (gitignored). Needs the virtualenv: `uv sync --python 3.12`.

set -u
cd "$(dirname "$0")/.." || exit 1

PY=.venv/bin/python
PID_FILE=logs/bot.pid
LOG_FILE=logs/bot.log
MAX_LOG_BYTES=10485760

# What "demo" changes. BINANCE_TESTNET=true is forced, so demo mode can never reach the real exchange.
DEMO_ENV="BINANCE_TESTNET=true DRY_RUN=false SCAN_INTERVAL=1m RSI_OVERSOLD=50 RSI_LOOKBACK=6 \
SCAN_EVERY_SECONDS=15 TAKE_PROFIT_PCT=0.003 STOP_LOSS_PCT=0.0025 ALLOCATION_PCT=0.002 \
ENGINE_TICK_SECONDS=5"

is_running() {
  [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

file_size() { stat -f %z "$1" 2>/dev/null || stat -c %s "$1" 2>/dev/null || echo 0; }

start() {
  local mode="${1:-}"
  if is_running; then
    echo "Already running (PID $(cat "$PID_FILE")). Use: ./scripts/local.sh status"
    return 0
  fi
  if [ ! -x "$PY" ]; then
    echo "No virtualenv found. Run:  uv sync --python 3.12"
    return 1
  fi
  if [ ! -f .env ]; then
    echo "No .env. Run:  cp .env.example .env   and fill it in."
    return 1
  fi

  local extra=""
  if [ "$mode" = "demo" ]; then
    extra="$DEMO_ENV"
    echo "DEMO MODE: Binance TESTNET only (fake money), fast signals, tiny targets."
  elif [ -n "$mode" ]; then
    echo "Unknown option '$mode'. Use:  start   or   start demo"
    return 1
  fi

  echo "Running the startup checks first..."
  # shellcheck disable=SC2086  # $extra is a list of VAR=value words on purpose
  out=$(env $extra "$PY" -m bot.main --check-only 2>&1)
  code=$?
  printf '%s\n' "$out" | grep -vE 'Warning|warnings.warn|WebSocketClientProtocol' | tail -n 12 | cut -c1-170
  if [ "$code" -ne 0 ]; then
    echo "The startup checks failed (see above). Not starting."
    return 1
  fi

  mkdir -p logs
  if [ -f "$LOG_FILE" ] && [ "$(file_size "$LOG_FILE")" -gt "$MAX_LOG_BYTES" ]; then
    mv "$LOG_FILE" "$LOG_FILE.1"
  fi
  # shellcheck disable=SC2086
  nohup env $extra "$PY" -m bot.main >>"$LOG_FILE" 2>&1 &
  local pid=$!
  echo "$pid" >"$PID_FILE"
  # Keep the Mac from idle-sleeping for as long as the bot lives (macOS only).
  if command -v caffeinate >/dev/null 2>&1; then
    nohup caffeinate -i -w "$pid" >/dev/null 2>&1 &
  fi
  sleep 2
  if is_running; then
    echo "Started (PID $pid). Log: $LOG_FILE"
    echo "Follow it with:  ./scripts/local.sh logs     Stop with:  ./scripts/local.sh stop"
  else
    echo "It exited right away. Last log lines:"
    tail -n 15 "$LOG_FILE"
    rm -f "$PID_FILE"
    return 1
  fi
}

stop() {
  if ! is_running; then
    echo "Not running."
    rm -f "$PID_FILE"
    return 0
  fi
  local pid
  pid=$(cat "$PID_FILE")
  echo "Stopping PID $pid (a clean shutdown can take up to about 20 seconds)..."
  kill -TERM "$pid" 2>/dev/null
  local waited=0
  while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt 40 ]; do
    sleep 1
    waited=$((waited + 1))
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "Still running after 40 s; sending SIGKILL. (Open positions keep their exits on Binance.)"
    kill -KILL "$pid" 2>/dev/null
  else
    echo "Stopped cleanly after ${waited}s."
  fi
  rm -f "$PID_FILE"
}

status() {
  if is_running; then
    local pid
    pid=$(cat "$PID_FILE")
    echo "Running: PID $pid, up $(ps -o etime= -p "$pid" | tr -d ' ')"
  else
    echo "Not running."
  fi
  if [ -f "$LOG_FILE" ]; then
    echo "--- the mode it started in:"
    grep "bot: starting:" "$LOG_FILE" | tail -1 | cut -c1-200
    echo "--- last log lines:"
    tail -n 8 "$LOG_FILE" | cut -c1-200
  fi
}

case "${1:-}" in
  start) start "${2:-}" ;;
  stop) stop ;;
  restart) stop; start "${2:-}" ;;
  status) status ;;
  logs) tail -n 40 -f "$LOG_FILE" ;;
  *) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
