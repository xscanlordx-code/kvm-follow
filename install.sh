#!/bin/bash
# install.sh — install kvm-follow as a launchd agent on the host machine.
#
# Installs the script to ~/bin, seeds a config file if there is none, generates the
# LaunchAgent plist with correct absolute paths and loads it. Safe to re-run: it
# unloads the old agent first and never overwrites an existing config.

set -eu

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/bin"
CONF_DIR="$HOME/.config/kvm-follow"
LABEL="com.kvm.follow"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$BIN_DIR" "$CONF_DIR" "$HOME/Library/LaunchAgents"

install -m 0755 "$SRC_DIR/kvm-follow.sh" "$BIN_DIR/kvm-follow.sh"
echo "installed $BIN_DIR/kvm-follow.sh"

if [ -f "$CONF_DIR/config" ]; then
  echo "kept existing $CONF_DIR/config"
else
  cp "$SRC_DIR/kvm-follow.conf.example" "$CONF_DIR/config"
  echo "seeded $CONF_DIR/config — EDIT IT before the agent will do anything useful"
fi

cat >"$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$BIN_DIR/kvm-follow.sh</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>StandardErrorPath</key><string>/tmp/kvm-follow.err</string>
</dict>
</plist>
PLIST_EOF
echo "wrote $PLIST"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "agent loaded"
echo
echo "watch it work:  tail -f /tmp/kvm-follow.log"
echo "stop it:        launchctl bootout gui/$(id -u)/$LABEL"
