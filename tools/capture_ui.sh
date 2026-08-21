#!/usr/bin/env bash
# Regenerate `ui.png`, the README's viewer screenshot.
#
#   ./tools/capture_ui.sh          # writes ui.png next to the repo root
#
# The companion to `demo.tape`, and separate from it for one reason: vhs
# records a *terminal*, and this frame is a browser. So the capture is headless
# Chrome instead, driven to a URL rather than through a browser-automation
# dependency -- the viewer keeps its current view in the location hash
# (`#diff/1/0`), which is what makes a single URL enough.
#
# Like the demo, it costs nothing and comes out the same for everyone:
# `examples/truncation_bug.py` embeds a mock provider, so no API key and no
# network. The frame is the context diff, because the truncation gutter is the
# thing no other tool renders.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/ui.png}"
PORT="${UI_CAPTURE_PORT:-8767}"

CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
if [ ! -x "$CHROME" ]; then
  CHROME="$(command -v chromium || command -v google-chrome || true)"
fi
if [ -z "$CHROME" ] || [ ! -x "$CHROME" ]; then
  echo "capture_ui: no Chrome or Chromium found; set CHROME=/path/to/chrome" >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'kill "${SERVER_PID:-}" 2>/dev/null || true; rm -rf "$WORK"' EXIT

# A server left over from an earlier run would answer on this port with *its*
# tape dir, and the screenshot would silently be of the wrong thing -- which is
# exactly what happened the first time this script was written.
if lsof -ti ":$PORT" >/dev/null 2>&1; then
  echo "capture_ui: something is already listening on $PORT" >&2
  echo "  kill it, or set UI_CAPTURE_PORT to a free port" >&2
  exit 1
fi

PY="${PYTHON:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"

# One recording with a truncated message in it. Same script as the demo GIF.
# Copied in and run by bare name so the recorded argv -- which the viewer shows
# in its header -- is `truncation_bug.py` rather than somebody's home directory.
cp "$ROOT/examples/truncation_bug.py" "$WORK/truncation_bug.py"
( cd "$WORK" && REELTIME_DEMO_PORT="$((PORT + 1))" \
    "$PY" -m reeltime.cli run "$PY" truncation_bug.py >/dev/null 2>&1 )

RUN="$("$PY" - "$WORK" <<'PYEOF'
import sys, pathlib
runs = sorted(pathlib.Path(sys.argv[1], ".tape", "runs").glob("*.jsonl"))
print(runs[-1].stem)
PYEOF
)"

( cd "$WORK" && "$PY" -m reeltime.cli ui "$RUN" --port "$PORT" --no-open >/dev/null 2>&1 ) &
SERVER_PID=$!

for _ in $(seq 1 50); do
  if curl -fsS "http://127.0.0.1:$PORT/api/boot" >/dev/null 2>&1; then break; fi
  sleep 0.2
done

BOOT="$(curl -fsS "http://127.0.0.1:$PORT/api/boot" || true)"
case "$BOOT" in
  *"$RUN"*) ;;
  *) echo "capture_ui: the server on $PORT is not serving $RUN -- got: $BOOT" >&2
     exit 1 ;;
esac

# `#diff/1/0` is the context diff of event 1 against event 0 -- the frame where
# the TRUNCATED gutter appears.
"$CHROME" --headless --disable-gpu --hide-scrollbars \
  --force-device-scale-factor=2 \
  --window-size=1360,395 \
  --virtual-time-budget=6000 \
  --screenshot="$OUT" \
  "http://127.0.0.1:$PORT/run/$RUN#diff/1/0" >/dev/null 2>&1

echo "wrote $OUT"
