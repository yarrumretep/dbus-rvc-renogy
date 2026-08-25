#!/bin/sh
set -eu

PACKAGE_DIR=/data/dbus-rvc-renogy
SERVICE_DIR=/data/dbus-rvc-renogy/services/dbus-rvc-renogy
ACTIVE_SERVICE=/service/dbus-rvc-renogy
LOG_DIR=/var/log/dbus-rvc-renogy

if [ ! -f "$PACKAGE_DIR/dbus-rvc-renogy.py" ]; then
    echo "Missing $PACKAGE_DIR/dbus-rvc-renogy.py" >&2
    exit 1
fi
if [ ! -f "$SERVICE_DIR/run" ] || [ ! -f "$SERVICE_DIR/log/run" ]; then
    echo "Missing runit files under $SERVICE_DIR" >&2
    exit 1
fi

chmod 755 "$PACKAGE_DIR/dbus-rvc-renogy.py"
chmod 755 "$SERVICE_DIR/run" "$SERVICE_DIR/log/run"
mkdir -p "$LOG_DIR"

if [ -L "$ACTIVE_SERVICE" ]; then
    if [ "$(readlink "$ACTIVE_SERVICE")" != "$SERVICE_DIR" ]; then
        echo "$ACTIVE_SERVICE points somewhere else; refusing to replace it" >&2
        exit 1
    fi
elif [ -e "$ACTIVE_SERVICE" ]; then
    echo "$ACTIVE_SERVICE already exists and is not a symlink; refusing to replace it" >&2
    exit 1
else
    ln -s "$SERVICE_DIR" "$ACTIVE_SERVICE"
fi

# A down/up cycle also reloads an updated bridge script when the service was
# already installed and running.
svc -d "$ACTIVE_SERVICE" 2>/dev/null || true
svc -u "$ACTIVE_SERVICE" 2>/dev/null || true
echo "Installed $ACTIVE_SERVICE -> $SERVICE_DIR"
echo "Rollback: $PACKAGE_DIR/uninstall-service.sh"
