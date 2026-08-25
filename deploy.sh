#!/bin/sh
set -eu

usage() {
    cat <<'EOF'
Usage: ./deploy.sh [--auto-source] [user@venus-host]

Synchronize this checkout to /data/dbus-rvc-renogy, preserve the remote
config file, restart the supervised service, and verify the running version.

  --auto-source  Replace an existing fixed source-address override with auto.

The target defaults to $RVC_RENOGY_TARGET or root@venus.local.
EOF
}

AUTO_SOURCE=0
TARGET=${RVC_RENOGY_TARGET:-root@venus.local}
TARGET_SET=0

while [ "$#" -gt 0 ]; do
    case $1 in
        --auto-source)
            AUTO_SOURCE=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        -*)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
        *)
            if [ "$TARGET_SET" -eq 1 ]; then
                echo "Only one deployment target may be specified" >&2
                exit 2
            fi
            TARGET=$1
            TARGET_SET=1
            ;;
    esac
    shift
done

ROOT=$(CDPATH= cd "$(dirname "$0")" && pwd)
EXPECTED_VERSION=$(sed 's/^v//' "$ROOT/version")
REMOTE_DIR=/data/dbus-rvc-renogy

for command_name in ssh rsync; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "Missing local command: $command_name" >&2
        exit 1
    fi
done

if ! ssh "$TARGET" 'command -v rsync >/dev/null 2>&1'; then
    echo "rsync is not installed on $TARGET" >&2
    exit 1
fi

echo "Deploying v$EXPECTED_VERSION to $TARGET:$REMOTE_DIR"
ssh "$TARGET" "mkdir -p $REMOTE_DIR"

# Do not use --delete: config and any operator-created diagnostic captures are
# remote state and must survive a deployment.
rsync -rlptz \
    --exclude='.git/' \
    --exclude='config' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    "$ROOT/" "$TARGET:$REMOTE_DIR/"

ssh "$TARGET" sh -s -- "$AUTO_SOURCE" "$EXPECTED_VERSION" <<'REMOTE'
set -eu

AUTO_SOURCE=$1
EXPECTED_VERSION=$2
PACKAGE_DIR=/data/dbus-rvc-renogy
CONFIG=$PACKAGE_DIR/config
SERVICE=/service/dbus-rvc-renogy
DBUS_SERVICE=com.victronenergy.battery.rvc_renogy_can0

if [ "$AUTO_SOURCE" -eq 1 ] && [ -f "$CONFIG" ]; then
    sed -i \
        's/^[#[:space:]]*export RVC_RENOGY_SOURCE_ADDRESS=.*/export RVC_RENOGY_SOURCE_ADDRESS=auto/' \
        "$CONFIG"
fi

chmod 755 "$PACKAGE_DIR/deploy.sh"
"$PACKAGE_DIR/install-service.sh"

attempt=0
while [ "$attempt" -lt 20 ]; do
    reported=$(
        dbus -y "$DBUS_SERVICE" /Mgmt/ProcessVersion GetValue 2>/dev/null |
            tr -d "'[:space:]" || true
    )
    if [ "$reported" = "$EXPECTED_VERSION" ]; then
        echo "Verified $DBUS_SERVICE v$reported"
        svstat "$SERVICE"
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 1
done

echo "Service did not publish expected version $EXPECTED_VERSION" >&2
svstat "$SERVICE" >&2 || true
tai64nlocal < /var/log/dbus-rvc-renogy/current | tail -n 30 >&2 || true
exit 1
REMOTE
