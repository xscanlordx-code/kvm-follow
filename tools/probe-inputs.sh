#!/bin/bash
# probe-inputs.sh — find the input-alt code your monitor answers to.
#
# Sends a series of candidate codes through the proprietary LG register (0xF4 at
# I2C 0x50) with a pause between them, printing each one before it is sent. Watch
# the monitor: when the input changes, the code printed last is the one you want.
#
#   ./probe-inputs.sh                 # try the common codes
#   ./probe-inputs.sh 144 145 465     # try specific ones
#   DDC=~/bin/m1ddc ./probe-inputs.sh # use a specific m1ddc binary
#
# WARNING: this switches the monitor away from the machine you are typing on. Run it
# from the machine that will KEEP the picture, or over SSH, or be ready to switch the
# input back with the monitor's own buttons. Remember that DDC is global — you can
# always send the "come back" code from the other machine.

DDC="${DDC:-/opt/homebrew/bin/m1ddc}"
PAUSE="${PAUSE:-5}"

# Codes seen in the wild. The first four are documented by m1ddc; 465 (0x1D1) is the
# USB-C value on LG UltraWide panels and is two-byte — a broken m1ddc build silently
# truncates it to 0xD1 and nothing happens.
DEFAULT_CODES=(144 145 208 209 210 465 466 27 17 18 15 16)

[ -x "$DDC" ] || { echo "m1ddc not found at $DDC" >&2; exit 1; }

if [ $# -eq 0 ]; then codes=("${DEFAULT_CODES[@]}"); else codes=("$@"); fi

echo "monitor(s) visible to this machine:"
"$DDC" display list
echo
echo "sending ${#codes[@]} codes, ${PAUSE}s apart — watch the screen"
echo

for c in "${codes[@]}"; do
  printf 'input-alt %-5s (0x%X)\n' "$c" "$c"
  "$DDC" set input-alt "$c" >/dev/null 2>&1
  sleep 0.15
  "$DDC" set input-alt "$c" >/dev/null 2>&1   # LG swallows the first one sometimes
  sleep "$PAUSE"
done

echo
echo "done. Note that 'get input' / VCP 0x60 reads return 0 on these panels —"
echo "a firmware bug — so there is no way to confirm the current input in software."
