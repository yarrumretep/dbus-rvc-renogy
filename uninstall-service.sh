#!/bin/sh
set -eu

PACKAGE_DIR=${RVC_RENOGY_PACKAGE_DIR:-/data/dbus-rvc-renogy}
SERVICE_ROOT=${RVC_RENOGY_SERVICE_ROOT:-/service}
RC_LOCAL=${RVC_RENOGY_RC_LOCAL:-/data/rc.local}

SERVICE_DIR=$PACKAGE_DIR/services/dbus-rvc-renogy
ACTIVE_SERVICE=$SERVICE_ROOT/dbus-rvc-renogy

HOOK_BEGIN="# BEGIN dbus-rvc-renogy"
HOOK_COMMAND="$PACKAGE_DIR/install-service.sh --boot"
HOOK_END="# END dbus-rvc-renogy"

remove_boot_hook() {
    if [ -L "$RC_LOCAL" ]; then
        echo "$RC_LOCAL is a symlink; refusing to edit it" >&2
        return 1
    fi
    if [ ! -e "$RC_LOCAL" ]; then
        return 0
    fi
    if [ ! -f "$RC_LOCAL" ]; then
        echo "$RC_LOCAL exists and is not a regular file" >&2
        return 1
    fi
    if ! grep -Fqx "$HOOK_COMMAND" "$RC_LOCAL"; then
        return 0
    fi

    rc_dir=$(dirname "$RC_LOCAL")
    rc_temp=$(mktemp "$rc_dir/.rc.local.dbus-rvc-renogy.XXXXXX")
    trap 'if [ -n "${rc_temp:-}" ]; then rm -f "$rc_temp"; fi' 0 1 2 15

    awk -v hook_begin="$HOOK_BEGIN" \
        -v hook_command="$HOOK_COMMAND" \
        -v hook_end="$HOOK_END" '
        $0 == hook_begin || $0 == hook_command || $0 == hook_end { next }
        { print }
    ' "$RC_LOCAL" > "$rc_temp"

    chmod 755 "$rc_temp"
    mv "$rc_temp" "$RC_LOCAL"
    rc_temp=
    trap - 0 1 2 15
    echo "Removed boot hook from $RC_LOCAL"
}

remove_boot_hook

if [ -L "$ACTIVE_SERVICE" ]; then
    if [ "$(readlink "$ACTIVE_SERVICE")" != "$SERVICE_DIR" ]; then
        echo "$ACTIVE_SERVICE points somewhere else; refusing to remove it" >&2
        exit 1
    fi
    svc -dx "$ACTIVE_SERVICE" "$ACTIVE_SERVICE/log" 2>/dev/null || true
    rm "$ACTIVE_SERVICE"
    echo "Removed $ACTIVE_SERVICE; package files remain in $PACKAGE_DIR"
elif [ -e "$ACTIVE_SERVICE" ]; then
    echo "$ACTIVE_SERVICE is not the package symlink; refusing to remove it" >&2
    exit 1
else
    echo "$ACTIVE_SERVICE is not installed"
fi
