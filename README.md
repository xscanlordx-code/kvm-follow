# kvm-follow

Your monitor's input follows your keyboard. No KVM box, no USB switch, no reaching
behind the desk.

*[Русская версия](README.ru.md)*

Two Macs share one monitor and one Logitech Easy-Switch keyboard. You press the
Easy-Switch key to move the keyboard to the other machine — and the monitor follows
it, by itself, in about a second. That is the whole feature.

```
2026-08-13 16:10:11 [leave]  monitor -> peer in 2s
2026-08-13 16:10:24 [return] monitor -> host in 1s
2026-08-13 16:10:49 [leave]  monitor -> peer in 1s
2026-08-13 16:11:01 [return] monitor -> host in 1s
```

Hardware this was built and verified on:

| | |
|---|---|
| Monitor | LG UltraWide 2560×1080 (WQ family), no built-in KVM |
| Host | Mac mini M4 Pro on HDMI, always on |
| Peer | MacBook Pro M1 Pro on USB-C, lid closed |
| Keyboard | MX Keys Mini — Bluetooth to the host, Bolt receiver to the peer |
| Mouse | MX Master 3S, same Easy-Switch pairing |

Everything here is Apple-silicon-only, because it stands on
[m1ddc](https://github.com/waydabber/m1ddc).

## The part that took the longest

If you only read one section, read this one. Three findings, none of them documented
anywhere obvious.

**1. Recent LG monitors ignore the standard DDC input register.** VCP `0x60` is *the*
input-source register, and this panel advertises codes 15/17/18 in its own capability
string. Every one of them is accepted and does exactly nothing — the panel blinks and
stays where it was. Reading `0x60` back always returns `0`, a firmware bug, so you
cannot even tell which input is live.

**2. The real register is proprietary: `0xF4`, sent to I2C address `0x50` instead of
the usual `0x51`.** In m1ddc that is `set input-alt`. Verified codes on this panel:

| Input | Code | Hex |
|---|---|---|
| HDMI 1 | 144 | `0x90` |
| HDMI 2 | 145 | `0x91` |
| DisplayPort 1 | 208 | `0xD0` |
| USB-C | **465** | **`0x1D1`** |

**3. The Homebrew build of m1ddc cannot send that USB-C code — at all.** Release
1.2.0 has the `input-alt` command, but the implementation is broken: wrong checksum,
and the high byte of the value is zeroed. `0x01D1` gets truncated to `0xD1`, so the
command that switches the monitor to the laptop is not merely unreliable, it is
inexpressible. You need a HEAD build (the fix landed in
[PR #52](https://github.com/waydabber/m1ddc/pull/52), April 2025). This one detail
costs an afternoon if you don't know it: everything *looks* right, the tool exits 0,
and nothing happens.

And the property that makes the whole design possible:

**DDC registers are global to the monitor, not scoped to the active input.** Either
machine can send the switch command at any time, whether or not its picture is
currently on screen. So the machine that *loses* the display can be the one that
tells the monitor to go, and the machine that wants it back can ask for it — no
handshake, no shared state.

## How it works

The host runs a small polling loop. Once every 0.3 s it asks
`blueutil --is-connected "MX Keys Mini"`:

- **Keyboard disappears** → SSH to the peer, wake it, then send `input-alt 465`.
  The monitor lands on USB-C, the peer is already awake, no black screen.
- **Keyboard comes back** → send `input-alt 144` locally *and* from the peer.
  Belt and braces: after the monitor has spent time on another input, the host's DDC
  channel sometimes goes deaf, and the peer's copy of the command gets through.

Three details that are not obvious but matter:

- **Only the host can detect the keyboard.** `blueutil --is-connected` works by device
  *name*; passing the MAC address returns `0` even when connected. On the peer the
  keyboard arrives through a Logitech Bolt receiver, which keeps three HID interfaces
  open permanently — `ioreg` and `hidutil` never change, so there is nothing to watch.
  Hence the one-sided design: the host decides, the peer obeys over SSH.
- **The peer must be woken before the switch, not after.** `caffeinate -u -t 5` wakes
  it; `caffeinate -u -t 1` does not wake it at all. Then `caffeinate -d` holds the
  display, because `powerd` releases it again after **30 seconds** if nothing asserts.
- **Send every DDC command twice.** LG panels swallow the first one often enough to be
  annoying, and the second one costs 150 ms.

## Requirements

Both machines:

- Apple silicon (m1ddc has no Intel support)
- [m1ddc](https://github.com/waydabber/m1ddc) built from HEAD — see below
- Homebrew

Host only:

- `blueutil` — `brew install blueutil`
- SSH key access to the peer

Peer only:

- Remote Login enabled (System Settings → General → Sharing → Remote Login)

A Logitech Easy-Switch keyboard paired to the host over Bluetooth and to the peer
however you like. Any keyboard whose Bluetooth connection actually drops when it
switches channel will do.

## Install

### 1. m1ddc from HEAD, on both machines

```sh
brew uninstall m1ddc            # if you have the release; it will not work
git clone https://github.com/waydabber/m1ddc
cd m1ddc && make
sudo cp m1ddc /opt/homebrew/bin/m1ddc
```

If `make` fails with missing SDK headers, your Command Line Tools are too old
(`xcode-select --install`). It is perfectly fine to build the binary on one machine
and copy it to the other — it is a single static-ish executable and both machines are
arm64.

Verify that the fixed `input-alt` is present:

```sh
m1ddc set input-alt 465 && echo "if the monitor switched, you are done"
```

### 2. SSH from host to peer

```sh
ssh-keygen -t ed25519 -f ~/.ssh/kvm_peer -N ''
ssh-copy-id -i ~/.ssh/kvm_peer.pub youruser@peer.local
ssh -i ~/.ssh/kvm_peer youruser@peer.local true && echo ok
```

### 3. The agent, on the host

```sh
git clone https://github.com/xscanlordx-code/kvm-follow
cd kvm-follow
./install.sh
$EDITOR ~/.config/kvm-follow/config     # keyboard name, input codes, peer host
launchctl kickstart -k gui/$(id -u)/com.kvm.follow
tail -f /tmp/kvm-follow.log
```

### Finding your own input codes

The codes above are for one LG UltraWide. If your panel is different:

```sh
./tools/probe-inputs.sh          # cycles the common codes, 5 s apart
./tools/probe-inputs.sh 144 465  # or specific ones
```

Run it from the machine that will keep the picture, and remember that DDC is global —
if you lose the screen, send the return code from the other machine.

## Trade-off you should know about

Waking the peer takes about 1.5 s, so the script does not release its `caffeinate`
hold when you switch away — the peer stays awake and the next switch is instant. The
price is that the peer never sleeps on its own while the agent is running.

`WAKE_HOLD` bounds this: the default of 1800 s means the hold expires half an hour
after the last switch, so a laptop you carry off the desk will sleep normally. Set
`WAKE_HOLD=0` for a hold that never expires (fastest, but the peer stays awake until
it reboots).

Note that this affects display sleep and idle system sleep only. A laptop set to sleep
on lid close still sleeps on lid close.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Nothing happens, exit code 0 | Release build of m1ddc. Check `input-alt` against a two-byte code. |
| Monitor blinks but stays put | You are sending `set input`, not `set input-alt`. |
| Black screen after switching to the peer | Peer was asleep and SSH failed — check `/tmp/kvm-follow.log`. |
| Switch happens twice, or bounces | Raise `SETTLE`; the Bluetooth stack is flapping. |
| `blueutil --is-connected` always 0 | You passed a MAC address. Use the device name. |
| Works one way only | The host's DDC channel went deaf; that is exactly why the return path also fires from the peer. |
| Agent dies at boot | `blueutil`/`m1ddc` path wrong in the config, see `/tmp/kvm-follow.err`. |

## Dead ends, so you don't repeat them

- **Switching by killing the video signal** (`pmset displaysleepnow` + letting the
  monitor auto-search for a live input). This *works*, and it was the first version
  here, but it takes 12–20 s per switch and produces HPD races where macOS rearranges
  windows. The DDC path is 10× faster and does not touch the video link at all.
- **LG OnScreen Control / LG Dual Controller** — no input switching. Dual Controller
  moves the *pointer* between machines over the network, which is a different feature.
- **MonitorControl** — brightness and volume only, input switching is not implemented.
- **ddcctl** — Intel-only.
- **The monitor's own KVM** — this panel does not have one. Panels that do have one
  usually expose it at `0xE7`/`0xE8` and m1ddc has Dell/ASUS-specific commands for it.
- **Detecting the keyboard on the laptop side** — impossible through a Bolt receiver,
  see above.

## Credits

All the hard DDC work belongs to [waydabber/m1ddc](https://github.com/waydabber/m1ddc)
and to whoever reverse-engineered the `0xF4`-at-`0x50` register for LG panels. This
repository is a hundred lines of shell on top of that, plus the specific codes and
failure modes for one very common monitor family.

## License

MIT — see [LICENSE](LICENSE).
