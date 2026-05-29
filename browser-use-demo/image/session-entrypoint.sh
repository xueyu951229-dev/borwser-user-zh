#!/bin/bash
set -e

# Session container entrypoint
# Starts an isolated desktop environment with Xvfb, VNC, noVNC, and Chromium (CDP-enabled)
# This is used for per-user-session sandbox isolation
#
# CDP exposure: Chrome listens on localhost:19222, socat forwards 0.0.0.0:9222 → 127.0.0.1:19222
# This allows the main backend to connect via Docker network while keeping Chrome secure.

export DISPLAY=:${DISPLAY_NUM:-1}
export CDP_PORT=${CDP_PORT:-9222}
export CHROME_INTERNAL_PORT=19222
export VNC_PORT=${VNC_PORT:-5900}
export NOVNC_PORT=${NOVNC_PORT:-6080}
export WIDTH=${WIDTH:-1920}
export HEIGHT=${HEIGHT:-1080}

echo "=== Session Container Starting ==="
echo "Display: ${DISPLAY} (${WIDTH}x${HEIGHT})"
echo "CDP: 0.0.0.0:${CDP_PORT} → 127.0.0.1:${CHROME_INTERNAL_PORT}"
echo "VNC Port: ${VNC_PORT} (internal)"
echo "noVNC Port: ${NOVNC_PORT}"

# 1. Start Xvfb
echo "[1/6] Starting Xvfb..."
./xvfb_startup.sh
echo "Xvfb started on ${DISPLAY}"

# 2. Start tint2 taskbar
echo "[2/6] Starting tint2 taskbar..."
./tint2_startup.sh
echo "tint2 started"

# 3. Start mutter window manager
echo "[3/6] Starting mutter window manager..."
./mutter_startup.sh
echo "mutter started"

# 4. Start x11vnc (internal only - accessed via noVNC websocket proxy)
echo "[4/6] Starting x11vnc on port ${VNC_PORT}..."
(x11vnc -display "${DISPLAY}" \
    -forever \
    -shared \
    -wait 50 \
    -rfbport "${VNC_PORT}" \
    -nopw \
    2>/tmp/x11vnc_stderr.log) &

# Wait for x11vnc to start
timeout=10
while [ $timeout -gt 0 ]; do
    if netstat -tuln 2>/dev/null | grep -q ":${VNC_PORT} "; then
        break
    fi
    sleep 1
    ((timeout--))
done
if [ $timeout -eq 0 ]; then
    echo "ERROR: x11vnc failed to start within 10s" >&2
    cat /tmp/x11vnc_stderr.log >&2
    exit 1
fi
echo "x11vnc started on port ${VNC_PORT}"

# 5. Start noVNC (maps to host port externally)
echo "[5/6] Starting noVNC on port ${NOVNC_PORT}..."
/opt/noVNC/utils/novnc_proxy \
    --vnc "localhost:${VNC_PORT}" \
    --listen "${NOVNC_PORT}" \
    --web /opt/noVNC \
    > /tmp/novnc.log 2>&1 &

# Wait for noVNC to start
timeout=10
while [ $timeout -gt 0 ]; do
    if netstat -tuln 2>/dev/null | grep -q ":${NOVNC_PORT} "; then
        break
    fi
    sleep 1
    ((timeout--))
done
if [ $timeout -eq 0 ]; then
    echo "ERROR: noVNC failed to start within 10s" >&2
    exit 1
fi
echo "noVNC started on port ${NOVNC_PORT}"

# 6. Start Chromium with remote debugging + socat proxy
echo "[6/6] Starting Chromium (CDP: 0.0.0.0:${CDP_PORT} → 127.0.0.1:${CHROME_INTERNAL_PORT})..."
CHROME_USER_DATA="/tmp/chrome-session-$(date +%s)"

# Find the Chromium binary installed by Playwright
CHROMIUM_BIN=""
if [ -f "/home/browseruse/.cache/ms-playwright/chromium-"*/"chrome-linux64"/chrome ]; then
    CHROMIUM_BIN=$(ls -d /home/browseruse/.cache/ms-playwright/chromium-*/chrome-linux64/chrome 2>/dev/null | head -1)
fi
if [ -z "${CHROMIUM_BIN}" ] && [ -f "/home/browseruse/.cache/ms-playwright/chromium-"*/"chrome-linux"/chrome ]; then
    CHROMIUM_BIN=$(ls -d /home/browseruse/.cache/ms-playwright/chromium-*/chrome-linux/chrome 2>/dev/null | head -1)
fi
if [ -z "${CHROMIUM_BIN}" ]; then
    CHROMIUM_BIN=$(which google-chrome 2>/dev/null || which chromium-browser 2>/dev/null || echo "")
fi
if [ -z "${CHROMIUM_BIN}" ]; then
    echo "ERROR: Could not find Chromium binary" >&2
    exit 1
fi
echo "Using Chromium: ${CHROMIUM_BIN}"

# Start Chrome on localhost only
"${CHROMIUM_BIN}" \
    --no-sandbox \
    --disable-gpu \
    --disable-dev-shm-usage \
    --disable-setuid-sandbox \
    --remote-debugging-port="${CHROME_INTERNAL_PORT}" \
    --user-data-dir="${CHROME_USER_DATA}" \
    --window-size="${WIDTH}","${HEIGHT}" \
    --window-position=0,0 \
    --no-first-run \
    --disable-session-crashed-bubble \
    --disable-infobars \
    --disable-features=TranslateUI \
    --disable-component-extensions-with-background-pages \
    --disable-blink-features=AutomationControlled \
    --display="${DISPLAY}" \
    about:blank &

CHROME_PID=$!

# Wait for Chrome CDP to be ready on internal port
echo "Waiting for Chrome CDP on 127.0.0.1:${CHROME_INTERNAL_PORT}..."
timeout=30
while [ $timeout -gt 0 ]; do
    if curl -s "http://127.0.0.1:${CHROME_INTERNAL_PORT}/json/version" > /dev/null 2>&1; then
        break
    fi
    if ! kill -0 ${CHROME_PID} 2>/dev/null; then
        echo "ERROR: Chromium process died unexpectedly" >&2
        exit 1
    fi
    sleep 1
    ((timeout--))
done
if [ $timeout -eq 0 ]; then
    echo "ERROR: Chrome CDP not ready within 30s" >&2
    exit 1
fi
echo "Chrome CDP ready on 127.0.0.1:${CHROME_INTERNAL_PORT}"

# Expose CDP to Docker network via socat proxy
echo "Exposing CDP on 0.0.0.0:${CDP_PORT} via socat..."
socat TCP-LISTEN:${CDP_PORT},fork,reuseaddr TCP:127.0.0.1:${CHROME_INTERNAL_PORT} &
SOCAT_PID=$!

# Verify the exposed endpoint
sleep 1
if curl -s "http://127.0.0.1:${CDP_PORT}/json/version" > /dev/null 2>&1; then
    echo "CDP proxy verified on 0.0.0.0:${CDP_PORT}"
else
    echo "WARNING: CDP proxy check failed — continuing anyway..."
fi

echo "=== Session Container Ready ==="
echo "CDP available at: http://0.0.0.0:${CDP_PORT}"
echo "noVNC available at: http://localhost:${NOVNC_PORT}/vnc.html"

# Keep the container running; monitor Chrome and socat health
while true; do
    if ! kill -0 ${CHROME_PID} 2>/dev/null; then
        echo "ERROR: Chromium process died, exiting container" >&2
        exit 1
    fi
    if ! kill -0 ${SOCAT_PID} 2>/dev/null; then
        echo "ERROR: socat process died, exiting container" >&2
        exit 1
    fi
    sleep 5
done
