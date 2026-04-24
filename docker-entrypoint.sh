#!/bin/sh
# Start Xvfb in the background, wait briefly for it to be ready, then exec the
# Python app. Replaces `xvfb-run -a ...` wrapper, which hangs on the SIGUSR1
# ready-signal sync inside Docker and never execs the wrapped command.
set -e

: "${DISPLAY_NUM:=99}"
: "${SCREEN_GEOMETRY:=1280x900x24}"

# -ac disables access control (safe inside the container network boundary);
# without it, clients spawned outside Xvfb's auth handshake can't connect.
#
# setsid detaches Xvfb into its own session so it survives after this shell
# script does `exec python -m app.main`. Without setsid, tini-as-PID-1
# reparented Xvfb but something in the process-group handoff caused Xvfb to
# exit with <defunct> — patchright Chrome then failed with "Missing X server
# or $DISPLAY" even though /tmp/.X11-unix/X99 existed as a stale socket.
setsid -f Xvfb ":${DISPLAY_NUM}" -screen 0 "${SCREEN_GEOMETRY}" -nolisten tcp -ac
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
