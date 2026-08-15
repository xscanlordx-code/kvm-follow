# kvm-follow

> **⚠️ Current version lives in [`v3/`](v3/)** (2026-08-15): a two-agent design
> (mini + a MacBook-side agent speaking HID++ to the Bolt receiver), the mouse
> follows the keyboard both ways, and the MacBook naps in DarkWake instead of
> being held awake forever. The script below is v1/v2 — it works, but tells only
> half the story. The full map of rakes stepped on: [`v3/JOURNAL.md`](v3/JOURNAL.md) (Russian).


Your monitor's input follows your keyboard. No KVM box, no USB switch, no reaching
behind the desk.

*[Русская версия](README.ru.md)*

Two Macs share one monitor and one Logitech Easy-Switch keyboard. You press the
Easy-Switch key to move the keyboard to the other machine — and the monitor follows
it, by itself. That is the whole feature.

Hardware this was built and verified on:

| | |
|---|---|
| Monitor | **LG UltraWide 34WQ650** — 2560×1080 @ 100 Hz, 2023, no built-in KVM |
| Host | Mac mini M4 Pro on HDMI, always on |
| Peer | MacBook Pro M1 Pro on USB-C, lid closed |
| Keyboard | MX Keys Mini — Bluetooth to the host, Bolt receiver to the peer |
| Mouse | MX Master 3S, same Easy-Switch pairing |

Everything here is Apple-silicon-only, because it stands on
[m1ddc](https://github.com/waydabber/m1ddc). See
[Will this work on my setup?](#will-this-work-on-my-setup) before you start.

## Status: one verified configuration, not a product

Read this before you file an issue asking why it does not work on your desk.

This is a write-up of a setup that works, published because the three findings behind
it are documented nowhere obvious. It is **not** a tool that has been tested across
hardware. Specifically:

**Verified on the hardware above, in daily use:** the DDC sidechannel, the input codes
144 and 465, the wake-before-switch sequence, the double-send, the Bluetooth detection,
the ~7 second end-to-end timing.

**Not verified, and you should assume it needs work:**

- **The `DDC_CMD=input` path** (standard VCP `0x60`, for non-LG monitors). Logically
  it is a one-word substitution and the code path is identical, but there was no
  monitor at hand that speaks the standard register. Nobody has run it.
- **`WAKE_HOLD` expiry.** The author's own machine runs an earlier variant with an
  unbounded `caffeinate` hold; the 1800-second default here is the better default, but
  it has not been left running for weeks.
- **Every monitor that is not a 34WQ650.** Input codes differ per model — see below.
- **Anything but this exact pair of Macs**, this keyboard, and these ports.

Worth knowing: **the 34WQ650 appears in neither list on the
[ddcutil wiki](https://github.com/rockowitz/ddcutil/wiki/Switching-input-source-on-LG-monitors)** —
not among the confirmed models, not among the theoretically supported ones (the WQ
entries there are 60C, 75C and 95C). The sidechannel works on it anyway. Which is the
whole point: absence from the list means nothing, and presence in it does not tell you
your codes.

If you adapt this to other hardware, the codes and quirks you find are worth sending
to that wiki rather than only here — it is the closest thing to a central registry.

## How fast is it, honestly

**About 7 seconds** from pressing the Easy-Switch key to a picture on the other
machine. The breakdown matters, because most of it is not something software can fix:

| | |
|---|---|
| Bluetooth reports the keyboard is gone | ~1–2 s |
| Script debounce, SSH, wake, DDC command | < 1 s |
| **The panel physically changes input** | **~5 s** |

That last row is the monitor's own firmware and it is the same 5 seconds you get from
pressing the input button on the monitor itself. There is nothing to optimise there.

The log only shows the software part, so it prints numbers like this:

```
2026-08-13 16:10:11 [leave]  monitor -> peer in 2s
2026-08-13 16:10:24 [return] monitor -> host in 1s
```

Add ~5 s of panel time to each line for what you actually experience. For comparison,
the first version of this — which switched inputs by cutting the video signal and
letting the monitor auto-search — took 12–20 s end to end.

## The part that took the longest

If you only read one section, read this one. Three findings, none of them documented
anywhere obvious.

**1. Recent LG monitors ignore the standard DDC input register.** VCP `0x60` is *the*
input-source register, and this panel advertises codes 15/17/18 in its own capability
string. Every one of them is accepted and does exactly nothing — the panel blinks and
stays where it was. Reading `0x60` back always returns `0`, a firmware bug, so you
cannot even tell which input is live.

**2. The real register is `0xF4`, reached over LG's service sidechannel "DDC2AB".**
The DDC packet is sent with source address `0x50` instead of the standard `0x51`. In
m1ddc that is `set input-alt`; on Linux, ddcutil exposes the same thing as
`--i2c-source-addr=x50`. The
[ddcutil wiki page on LG input switching](https://github.com/rockowitz/ddcutil/wiki/Switching-input-source-on-LG-monitors)
is the best documentation that exists for this, including a list of confirmed models.

Codes per that wiki — and what this panel actually does:

| Input | Documented | This panel |
|---|---|---|
| HDMI 1 | 144 = `0x90` | 144 ✓ |
| HDMI 2 | 145 = `0x91` | — |
| DisplayPort 1 | 208 = `0xD0` | — |
| DisplayPort 2 | 209 = `0xD1` | — |
| USB-C | 210 = `0xD2` | **465 = `0x1D1`** |

Do not trust the table, verify your own codes. See
[finding your codes](#finding-your-own-input-codes).

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

## Will this work on my setup?

Be honest with yourself about all five rows before spending an evening on this.

| Layer | Requirement | If not |
|---|---|---|
| OS | macOS | Nothing here ports. See [other platforms](#other-platforms) |
| CPU | Apple silicon | m1ddc has no Intel support — use [BetterDisplay](https://github.com/waydabber/BetterDisplay) (DDC is free there) |
| Port | USB-C/DP Alt Mode, or a supported built-in HDMI | DDC frequently does not survive docks and hubs. Test with `m1ddc display list` first |
| Monitor | Speaks DDC input switching | Set `DDC_CMD=input` for the standard register — see below. Monitors with a built-in KVM don't need any of this |
| Keyboard | Bluetooth link that actually drops on channel switch | If both machines use receivers, there is nothing to detect and this design does not apply |

**Non-LG monitors are the common case, and they are easier.** Dell, Samsung, BenQ,
ASUS and older LGs generally honour the standard `0x60` register. Set `DDC_CMD=input`
in the config, use the standard codes (17 = HDMI 1, 15 = DisplayPort 1, 27 = USB-C),
and skip the entire sidechannel story. Try that path first — it is documented, it is
verifiable, and it does not poke a manufacturer's service channel.

**The peer does not have to be a Mac.** SSH is used for exactly two things: waking the
peer, and sending a duplicate switch command. If your second machine is a Windows or
Linux box that stays awake, drop the SSH parts and let the Mac send both commands
itself — the monitor does not care who tells it.

### Repeating this on your hardware: the order of operations

Do it in this order and you will know within twenty minutes whether it is possible at
all, before installing anything:

1. **`m1ddc display list`.** Nothing listed? DDC is not reaching the panel — wrong
   port, a dock, or a hub in the way. Stop here, no software fixes that.
2. **`m1ddc set luminance 50`.** Brightness changes? Then the channel works and the
   monitor is talking to you. This is the cheap, harmless test — do it before touching
   inputs.
3. **`m1ddc set input 17`** (HDMI 1) or `27` (USB-C). If the input switches, you are
   done with the hard part: set `DDC_CMD=input`, use standard codes, and skip the rest
   of this section entirely.
4. **Only if step 3 does nothing:** you are on a recent LG. Run
   `./tools/probe-inputs.sh` for the documented sidechannel codes. Expect ~5 s per
   switch — do not conclude a code failed until you have waited.
5. **Codes found?** Then everything else here is plumbing: SSH keys, the config file,
   `install.sh`. Codes not found? Your panel is probably not in this family, and
   nothing in this repository will help.

Test each direction from the *other* machine first, by hand, before wiring up the
agent. A daemon that switches your display away is a bad place to discover a typo.

### Other platforms

The *approach* travels even though this script does not:

- **Linux** — ddcutil supports the LG sidechannel natively:
  `ddcutil setvcp xF4 x0090 --i2c-source-addr=x50 --noverify`. `--noverify` is
  required: the panel does not report back on this channel. Keyboard detection would
  come from `bluetoothctl` or udev instead of blueutil.
- **Windows** — the DDC APIs do not let you change the source address, so people go
  around them through the GPU's raw I2C:
  [lg-input-switch](https://github.com/meer-cha/lg-input-switch) via NVAPI for NVIDIA,
  [LGInputSwitch](https://github.com/phillip9933/LGInputSwitch) for AMD.
- **Intel Macs** — BetterDisplay, which does DDC on all Macs and all ports.

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
and copy it to the other — it is a single executable and both machines are arm64.

Verify that the fixed `input-alt` is present:

```sh
m1ddc set input-alt 465 && echo "if the monitor switched, you are done"
```

### 2. blueutil on the host

```sh
brew install blueutil
blueutil --paired          # copy the exact keyboard name from here
```

### 3. SSH from host to peer

Enable Remote Login on the peer (System Settings → General → Sharing), then:

```sh
ssh-keygen -t ed25519 -f ~/.ssh/kvm_peer -N ''
ssh-copy-id -i ~/.ssh/kvm_peer.pub youruser@peer.local
ssh -i ~/.ssh/kvm_peer youruser@peer.local true && echo ok
```

### 4. The agent, on the host

```sh
git clone https://github.com/xscanlordx-code/kvm-follow
cd kvm-follow
./install.sh
$EDITOR ~/.config/kvm-follow/config     # keyboard name, DDC_CMD, input codes, peer host
launchctl kickstart -k gui/$(id -u)/com.kvm.follow
tail -f /tmp/kvm-follow.log
```

`install.sh` is safe to re-run: it never overwrites an existing config and unloads the
old agent before loading the new one.

### Finding your own input codes

```sh
./tools/probe-inputs.sh                  # LG sidechannel, documented codes only
DDC_CMD=input ./tools/probe-inputs.sh    # standard VCP 0x60, for everything else
./tools/probe-inputs.sh 144 465          # just these two
```

Run it from the machine that will keep the picture, and remember that DDC is global —
if you lose the screen, send the return code from the other machine.

**Do not brute-force the sidechannel.** With `input-alt` these writes land on LG's
factory service channel. The probe ships a short list of documented values on purpose.
There is an unresolved report of an
[LG panel going unresponsive](https://github.com/rockowitz/ddcutil/issues/419) after
DDC experiments — that case involved plain `0x60`, but a manufacturer's service
channel is not where you want to find out whether it generalises. If your monitor is
not a recent LG, use `DDC_CMD=input` and stay on the documented register.

## Trade-off you should know about

Waking the peer takes about 1.5 s, so the script does not release its `caffeinate`
hold when you switch away — the peer stays awake and the next switch skips that step.
The price is that the peer never sleeps on its own while the agent is running.

`WAKE_HOLD` bounds this: the default of 1800 s means the hold expires half an hour
after the last switch, so a laptop you carry off the desk will sleep normally. Set
`WAKE_HOLD=0` for a hold that never expires (fastest, but the peer stays awake until
it reboots).

This affects display sleep and idle system sleep only. A laptop set to sleep on lid
close still sleeps on lid close.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Nothing happens, exit code 0 | Release build of m1ddc. Check `input-alt` against a two-byte code. |
| Monitor blinks but stays put | You are sending `set input` to a panel that needs `input-alt`, or the reverse. |
| Black screen after switching to the peer | Peer was asleep and SSH failed — check `/tmp/kvm-follow.log`. |
| Switch happens twice, or bounces | Raise `SETTLE`; the Bluetooth stack is flapping. |
| `blueutil --is-connected` always 0 | You passed a MAC address. Use the device name. |
| Works one way only | The host's DDC channel went deaf; that is exactly why the return path also fires from the peer. |
| `m1ddc display list` shows nothing | DDC is not reaching the monitor — dock, hub, or unsupported port. |
| Agent dies at boot | `blueutil`/`m1ddc` path wrong in the config, see `/tmp/kvm-follow.err`. |

## Dead ends, so you don't repeat them

- **Switching by killing the video signal** (`pmset displaysleepnow` + letting the
  monitor auto-search for a live input). This *works*, and it was the first version
  here, but it takes 12–20 s per switch and produces HPD races where macOS rearranges
  windows. The DDC path is faster and does not touch the video link at all.
- **LG OnScreen Control / LG Dual Controller** — no input switching. Dual Controller
  moves the *pointer* between machines over the network, which is a different feature.
- **MonitorControl** — brightness and volume only, input switching is not implemented.
- **ddcctl** — Intel-only.
- **The monitor's own KVM** — this panel does not have one. Panels that do usually
  expose it at `0xE7`/`0xE8`, and m1ddc has Dell/ASUS-specific commands for it.
- **Detecting the keyboard on the laptop side** — impossible through a Bolt receiver,
  see above.

## Credits

All the hard DDC work belongs to [waydabber/m1ddc](https://github.com/waydabber/m1ddc),
and the LG sidechannel was reverse-engineered by the
[ddcutil](https://github.com/rockowitz/ddcutil) community. This repository is a
hundred lines of shell on top of that, plus the specific codes, timings and failure
modes for one very common monitor family.

## License

MIT — see [LICENSE](LICENSE).
