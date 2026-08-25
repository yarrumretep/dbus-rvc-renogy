#!/bin/sh
set -eu

PACKAGE_DIR=${RVC_RENOGY_PACKAGE_DIR:-/data/dbus-rvc-renogy}
SERVICE_ROOT=${RVC_RENOGY_SERVICE_ROOT:-/service}
LOG_ROOT=${RVC_RENOGY_LOG_ROOT:-/var/log}
RC_LOCAL=${RVC_RENOGY_RC_LOCAL:-/data/rc.local}

SERVICE_DIR=$PACKAGE_DIR/services/dbus-rvc-renogy
ACTIVE_SERVICE=$SERVICE_ROOT/dbus-rvc-renogy
LOG_DIR=$LOG_ROOT/dbus-rvc-renogy

HOOK_BEGIN="# BEGIN dbus-rvc-renogy"
HOOK_COMMAND="$PACKAGE_DIR/install-service.sh --boot"
HOOK_END="# END dbus-rvc-renogy"

BOOT_MODE=0
case ${1:-} in
    "") ;;
    --boot) BOOT_MODE=1 ;;
    *)
        echo "Usage: $0 [--boot]" >&2
        exit 2
        ;;
esac

install_boot_hook() {
    if [ -L "$RC_LOCAL" ]; then
        echo "$RC_LOCAL is a symlink; refusing to replace it" >&2
        return 1
    fi
    if [ -e "$RC_LOCAL" ] && [ ! -f "$RC_LOCAL" ]; then
        echo "$RC_LOCAL exists and is not a regular file" >&2
        return 1
    fi

    if [ -f "$RC_LOCAL" ] && grep -Fqx "$HOOK_COMMAND" "$RC_LOCAL"; then
        chmod 755 "$RC_LOCAL"
        return 0
    fi

    rc_dir=$(dirname "$RC_LOCAL")
    mkdir -p "$rc_dir"
    rc_temp=$(mktemp "$rc_dir/.rc.local.dbus-rvc-renogy.XXXXXX")
    trap 'if [ -n "${rc_temp:-}" ]; then rm -f "$rc_temp"; fi' 0 1 2 15

    if [ -f "$RC_LOCAL" ]; then
        awk -v hook_begin="$HOOK_BEGIN" \
            -v hook_command="$HOOK_COMMAND" \
            -v hook_end="$HOOK_END" '
            BEGIN { inserted = 0 }
            !inserted && $0 ~ /^[[:space:]]*exit[[:space:]]+0[[:space:]]*$/ {
                print hook_begin
                print hook_command
                print hook_end
                inserted = 1
            }
            { print }
            END {
                if (!inserted) {
                    if (NR > 0) print ""
                    print hook_begin
                    print hook_command
                    print hook_end
                }
            }
        ' "$RC_LOCAL" > "$rc_temp"
    else
        printf '%s\n%s\n%s\n%s\n' \
            '#!/bin/sh' "$HOOK_BEGIN" "$HOOK_COMMAND" "$HOOK_END" \
            > "$rc_temp"
    fi

    chmod 755 "$rc_temp"
    mv "$rc_temp" "$RC_LOCAL"
    rc_temp=
    trap - 0 1 2 15
    echo "Installed boot hook in $RC_LOCAL"
}

if [ ! -f "$PACKAGE_DIR/dbus-rvc-renogy.py" ]; then
    echo "Missing $PACKAGE_DIR/dbus-rvc-renogy.py" >&2
    exit 1
fi
if [ ! -f "$SERVICE_DIR/run" ] || [ ! -f "$SERVICE_DIR/log/run" ]; then
    echo "Missing runit files under $SERVICE_DIR" >&2
    exit 1
fi
if [ ! -d "$SERVICE_ROOT" ]; then
    echo "Missing Venus service directory $SERVICE_ROOT" >&2
    exit 1
fi

if [ "$BOOT_MODE" -eq 0 ]; then
    install_boot_hook
fi

chmod 755 "$PACKAGE_DIR/dbus-rvc-renogy.py"
chmod 755 "$PACKAGE_DIR/install-service.sh"
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

# A manual down/up cycle reloads updated code. During boot the service is new,
# so only request up after the supervisor discovers the recreated link.
if [ "$BOOT_MODE" -eq 0 ]; then
    svc -d "$ACTIVE_SERVICE" 2>/dev/null || true
fi

attempt=0
while ! svc -u "$ACTIVE_SERVICE" 2>/dev/null; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 10 ]; then
        echo "Service link exists, but supervision did not start" >&2
        exit 1
    fi
    sleep 1
done

echo "Installed $ACTIVE_SERVICE -> $SERVICE_DIR"
echo "Rollback: $PACKAGE_DIR/uninstall-service.sh"
