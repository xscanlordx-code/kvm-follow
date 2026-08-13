#!/bin/bash
# kvm-follow — the monitor's input follows the keyboard.
#
# Runs on the always-on machine (the "host", e.g. a Mac mini). Watches whether a
# Logitech Easy-Switch keyboard is currently paired to this machine over Bluetooth.
# When the keyboard leaves, the monitor is switched to the other machine (the "peer",
# e.g. a laptop). When it comes back, the monitor is switched back. No KVM box,
# no USB switch, no cable pulling.
#
# HOW THE INPUT SWITCHING ACTUALLY WORKS
# --------------------------------------
# Recent LG monitors ignore the standard DDC/CI input register VCP 0x60. Every code
# advertised in the monitor's own capability string is accepted and does nothing but
# make the panel blink. Input is instead controlled by a proprietary register, 0xF4,
# sent to I2C address 0x50 instead of the usual 0x51 — that is what m1ddc calls
# `set input-alt`.
#
# TRAP: the Homebrew release of m1ddc (1.2.0) has the `input-alt` command but it is
# BROKEN — wrong checksum, and the high byte of the value is zeroed out. Since some
# input codes are two-byte (USB-C on this panel is 465 = 0x01D1), the broken build
# literally cannot express them. You need a HEAD build (PR #52, April 2025).
# See README for build instructions.
#
# DDC registers are global to the monitor, not per-input: the command works from
# either machine regardless of which input is currently displayed. That is the whole
# trick that makes this possible without extra hardware.
#
# DETECTION
# ---------
# Only the host can detect the keyboard: `blueutil --is-connected` by device NAME
# (querying by MAC address returns 0 incorrectly). On the peer side detection is
# impossible if the keyboard arrives through a Logitech Bolt receiver — the receiver
# holds three HID interfaces open permanently, so ioreg/hidutil never change.
# Hence the one-sided design: the host decides, the peer obeys over SSH.

# No `set -u` on purpose: macOS ships bash 3.2, where expanding an empty array
# under `set -u` is itself an error. Every setting below has a default instead.

CONFIG="${KVM_FOLLOW_CONFIG:-$HOME/.config/kvm-follow/config}"
# shellcheck source=/dev/null
[ -f "$CONFIG" ] && . "$CONFIG"

# ---- settings (override in the config file, see kvm-follow.conf.example) ----

: "${KEYBOARD:=MX Keys Mini}"      # exact Bluetooth name, see: blueutil --paired
: "${INPUT_HOST:=144}"             # input code for this machine   (144 = 0x90,  HDMI 1)
: "${INPUT_PEER:=465}"             # input code for the other one  (465 = 0x1D1, USB-C)

: "${PEER_USER:=$USER}"
: "${PEER_HOSTS:=}"                # space-separated, tried in order: "host.local 192.168.0.10"
: "${SSH_KEY:=$HOME/.ssh/kvm_peer}"

: "${DDC:=/opt/homebrew/bin/m1ddc}"       # HEAD build on this machine
: "${PEER_DDC:=/opt/homebrew/bin/m1ddc}"  # HEAD build on the peer
: "${BLUEUTIL:=/opt/homebrew/bin/blueutil}"

: "${POLL:=0.3}"                   # polling interval, seconds
: "${SETTLE:=0.3}"                 # debounce confirmation delay — Bluetooth flaps
: "${WAKE_HOLD:=1800}"             # seconds to keep the peer's display awake; 0 = forever
: "${LOG:=/tmp/kvm-follow.log}"

log() { echo "$(date '+%F %T') $*" >>"$LOG"; }

if [ -z "$PEER_HOSTS" ]; then
  log "FATAL: PEER_HOSTS is empty, see $CONFIG"; exit 1
fi
read -ra HOSTS <<<"$PEER_HOSTS"

# ControlMaster keeps one SSH connection warm — without it every switch pays the
# full handshake (~1 s), which is most of the perceived latency.
SSH_OPTS=(-i "$SSH_KEY" -o ConnectTimeout=5 -o BatchMode=yes
          -o StrictHostKeyChecking=accept-new
          -o ControlMaster=auto -o ControlPath=/tmp/kvm-ssh-%h -o ControlPersist=900)

PEER_ACTIVE=""

peer_ssh() {
  local h
  for h in "$PEER_ACTIVE" "${HOSTS[@]}"; do
    [ -z "$h" ] && continue
    if ssh "${SSH_OPTS[@]}" "$PEER_USER@$h" "$1" >/dev/null 2>&1; then
      PEER_ACTIVE="$h"; return 0
    fi
  done
  PEER_ACTIVE=""; return 1
}

# Wake the peer and hold its display on while we are using it, otherwise the monitor
# switches to a machine that is asleep and shows a black screen.
#   caffeinate -u -t 5  wakes it   (with -t 1 it does not wake at all)
#   caffeinate -d       holds it   (powerd otherwise releases the display after
#                                   30 SECONDS, not 30 minutes)
# The hold is NOT dropped when we switch back: leaving the peer awake saves ~1.5 s on
# the next switch, because there is no wake-up to wait for. The cost is that the peer
# never sleeps on its own. WAKE_HOLD bounds that — the hold expires by itself if you
# walk away with the laptop. Set WAKE_HOLD=0 for the original always-on behaviour.
peer_wake() {
  local hold="caffeinate -d"
  [ "$WAKE_HOLD" -gt 0 ] && hold="caffeinate -d -t $WAKE_HOLD"
  peer_ssh "nohup caffeinate -u -t 5 >/dev/null 2>&1 &
            pkill -f 'caffeinate -d' >/dev/null 2>&1
            nohup $hold >/dev/null 2>&1 &"
}

# LG panels occasionally swallow the first command — send it twice.
ddc_local()  { "$DDC" set input-alt "$1" >/dev/null 2>&1; sleep 0.15
               "$DDC" set input-alt "$1" >/dev/null 2>&1; }
ddc_peer()   { peer_ssh "$PEER_DDC set input-alt $1"; }

probe() { "$BLUEUTIL" --is-connected "$KEYBOARD" 2>/dev/null; }

for bin in "$DDC" "$BLUEUTIL"; do
  [ -x "$bin" ] || { log "FATAL: $bin not found or not executable"; exit 1; }
done

peer_ssh 'true' && log "ssh master up: $PEER_ACTIVE" || log "peer unreachable"

state=$(probe)
case "$state" in 0|1) ;; *) state=1 ;; esac
log "start, keyboard here=$state"

while true; do
  sleep "$POLL"
  now=$(probe)
  case "$now" in 0|1) ;; *) continue ;; esac
  [ "$now" = "$state" ] && continue

  t0=$(date +%s)
  sleep "$SETTLE"
  confirm=$(probe)
  [ "$confirm" != "$now" ] && { log "flap ($state->$now->$confirm), ignored"; continue; }
  state=$now

  if [ "$now" = "0" ]; then
    # keyboard left: wake the peer FIRST, or its input shows black
    if peer_wake; then
      ddc_local "$INPUT_PEER"
      log "[leave] monitor -> peer in $(( $(date +%s) - t0 ))s"
    else
      log "[leave] peer does not answer over SSH — monitor left alone"
    fi
  else
    # keyboard is back: this machine is awake by definition, nothing to wake.
    # Send from both sides — if this machine lost its DDC channel (it happens after
    # the monitor has been on another input for a while), the peer's copy gets through.
    ddc_local "$INPUT_HOST"
    ddc_peer  "$INPUT_HOST" &
    log "[return] monitor -> host in $(( $(date +%s) - t0 ))s"
  fi
done
