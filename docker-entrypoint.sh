#!/bin/sh
# Start Xvfb in the background, wait briefly for it to be ready, then exec the
# Python app. Replaces `xvfb-run -a ...` wrapper, which hangs on the SIGUSR1
# ready-signal sync inside Docker and never execs the wrapped command.
set -e

: "${DISPLAY_NUM:=99}"
: "${SCREEN_GEOMETRY:=1280x900x24}"

# Clean any stale socket + lock file from a prior run so Xvfb can bind.
# `docker restart` preserves /tmp but a dead Xvfb leaves its socket behind,
# making the new Xvfb refuse to start with "already in use" silently.
rm -f "/tmp/.X11-unix/X${DISPLAY_NUM}" "/tmp/.X${DISPLAY_NUM}-lock"

# -ac disables access control (safe inside the container network boundary).
# setsid detaches Xvfb into its own session so it survives after this shell
# script does `exec python -m app.main`. Without setsid, tini-as-PID-1
# reparented Xvfb but something in the process-group handoff caused Xvfb to
# exit with <defunct>.
setsid -f Xvfb ":${DISPLAY_NUM}" -screen 0 "${SCREEN_GEOMETRY}" -nolisten tcp -ac

# Verify Xvfb actually came up before we continue (up to 5s).
for i in 1 2 3 4 5 6 7 8 9 10; do
    if [ -e "/tmp/.X11-unix/X${DISPLAY_NUM}" ] && pgrep -f "Xvfb :${DISPLAY_NUM}" >/dev/null 2>&1; then
        break
    fi
    sleep 0.5
done
if ! pgrep -f "Xvfb :${DISPLAY_NUM}" >/dev/null 2>&1; then
    echo "[docker-entrypoint] FATAL: Xvfb :${DISPLAY_NUM} failed to start" >&2
    exit 1
fi
XVFB_PID=$(pgrep -f "Xvfb :${DISPLAY_NUM}" | head -1)

# Wait (up to 5s) for Xvfb to create its socket.
for i in 1 2 3 4 5 6 7 8 9 10; do
    if [ -e "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; then
        break
    fi
    sleep 0.5
done

export DISPLAY=":${DISPLAY_NUM}"
echo "[docker-entrypoint] Xvfb :${DISPLAY_NUM} running (pid ${XVFB_PID}); launching app"
exec python -m app.main
