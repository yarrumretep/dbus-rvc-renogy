#!/bin/sh
set -eu

SERVICE_DIR=/data/dbus-rvc-renogy/services/dbus-rvc-renogy
ACTIVE_SERVICE=/service/dbus-rvc-renogy

if [ -L "$ACTIVE_SERVICE" ]; then
    if [ "$(readlink "$ACTIVE_SERVICE")" != "$SERVICE_DIR" ]; then
        echo "$ACTIVE_SERVICE points somewhere else; refusing to remove it" >&2
        exit 1
    fi
    svc -d "$ACTIVE_SERVICE" 2>/dev/null || true
    rm "$ACTIVE_SERVICE"
    echo "Removed $ACTIVE_SERVICE; package files remain in /data/dbus-rvc-renogy"
elif [ -e "$ACTIVE_SERVICE" ]; then
    echo "$ACTIVE_SERVICE is not the package symlink; refusing to remove it" >&2
    exit 1
else
    echo "$ACTIVE_SERVICE is not installed"
fi
