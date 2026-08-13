#!/bin/bash
# probe-inputs.sh — find the input code your monitor answers to.
#
# Sends candidate codes with a pause between them, printing each one before it goes
# out. Watch the monitor: when the input changes, the code printed last is yours.
#
#   ./probe-inputs.sh                     # LG sidechannel, documented codes only
#   DDC_CMD=input ./probe-inputs.sh       # standard VCP 0x60 codes (non-LG monitors)
#   ./probe-inputs.sh 144 465             # only these codes
#   DDC=~/bin/m1ddc PAUSE=8 ./probe-inputs.sh
#
# WARNING 1 — you will lose the picture. This switches the monitor away from the
# machine you are typing on. Run it from the machine that will KEEP the picture, or
# over SSH, or be ready to switch back with the monitor's own buttons. DDC is global,
# so the "come back" code can always be sent from the other machine.
#
# WARNING 2 — do not brute-force the sidechannel. With DDC_CMD=input-alt these
# commands go to LG's service channel (DDC2AB, source address 0x50), which is a
# manufacturer facility, not a documented user-facing register. The default list below
# is limited to values the ddcutil wiki documents plus one verified in the wild. Do
# not feed it long sweeps of arbitrary numbers: there are unresolved reports of LG
# panels ending up unresponsive after DDC experiments
# (https://github.com/rockowitz/ddcutil/issues/419 — that one involved plain VCP 0x60,
# but a factory channel is not the place to find out whether it can happen there too).
# If your monitor is not a recent LG, use DDC_CMD=input instead: standard, safe, dull.

DDC="${DDC:-/opt/homebrew/bin/m1ddc}"
DDC_CMD="${DDC_CMD:-input-alt}"
PAUSE="${PAUSE:-5}"

# Documented LG sidechannel codes, plus 465 (0x1D1) — the USB-C value verified on an
# LG UltraWide, where the documented 210 is not what the panel uses.
ALT_CODES=(144 145 208 209 210 465)

# Standard VCP 0x60 codes, per the MCCS specification.
STD_CODES=(15 16 17 18 27)

case "$DDC_CMD" in
  input-alt) DEFAULT_CODES=("${ALT_CODES[@]}") ;;
  input)     DEFAULT_CODES=("${STD_CODES[@]}") ;;
  *) echo "DDC_CMD must be 'input' or 'input-alt', got '$DDC_CMD'" >&2; exit 1 ;;
esac

[ -x "$DDC" ] || { echo "m1ddc not found at $DDC" >&2; exit 1; }

if [ $# -eq 0 ]; then codes=("${DEFAULT_CODES[@]}"); else codes=("$@"); fi

echo "monitor(s) visible to this machine:"
"$DDC" display list
echo
echo "command: m1ddc set $DDC_CMD <code>"
echo "sending ${#codes[@]} codes, ${PAUSE}s apart — watch the screen"
echo

for c in "${codes[@]}"; do
  printf 'set %-9s %-5s (0x%X)\n' "$DDC_CMD" "$c" "$c"
  "$DDC" set "$DDC_CMD" "$c" >/dev/null 2>&1
  sleep 0.15
  "$DDC" set "$DDC_CMD" "$c" >/dev/null 2>&1   # LG swallows the first one sometimes
  sleep "$PAUSE"
done

echo
echo "done. Note that reading the input back (VCP 0x60) returns 0 on these panels —"
echo "a firmware bug — so there is no way to confirm the current input in software."
