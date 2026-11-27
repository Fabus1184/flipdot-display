# Firmware

Rust firmware for the ESP32 on the driver board, bare metal on `esp-hal`. How
to build and flash it is in the [top-level README](../README.md#firmware).

| Part | What |
| --- | --- |
| [`core/src/panel.rs`](core/src/panel.rs) | Panel driver: shift registers, FP2800A, one pulse per changed dot, with its safety rules tested against a simulated board |
| [`src/panel.rs`](src/panel.rs) | The driver's ESP32 pins, and switching the panel off on a panic |
| [`src/persist.rs`](src/persist.rs) | Keeps the time across resets in RTC memory |
| [`src/main.rs`](src/main.rs) | Start-up, the two cores, the network tasks (WiFi, NTP, weather, status page), watchdog |
| [`src/net.rs`](src/net.rs) | NTP query, HTTP GET and status page on embassy-net, each with a deadline |
| [`core/`](core/) | Frame buffer, font, screen layout, clock discipline and reply parsing; tested on the PC |

## Power

The CPU runs at 80 MHz, the display loop sleeps between its checks instead of
spinning, and WiFi uses modem power saving (the radio wakes for the access
point's beacons). The status page answers a few hundred milliseconds slower
for it.

## One dot at a time

Two coils powered at once overload the FP2800A, and a high-side and a
low-side row switch on together short +HV to GND through the coil diodes. The
driver in [`core/src/panel.rs`](core/src/panel.rs) therefore keeps these rules,
and its tests check them on every pin change against a simulated 74HC595
chain:

- the chain is cleared at start-up and only latched while its outputs are off;
- its outputs are only switched on with exactly one of the 32 row bits set;
- `FP_EN` only goes high with that one row switch on, for 500 µs, with
  interrupts masked so nothing can stretch the pulse;
- address and `FP_DATA` never change during a pulse.

A panic first drives `FP_EN` low and `SR_OE#` high. Before the firmware runs,
R14 holds `SR_OE#` high, so all row switches are off during reset and boot.

## The included application

The firmware currently shows a clock with the outside temperature. Replace
`clock::render` and the network jobs in `main.rs` to show something else.

- **Time.** Synced via NTP every 2 minutes. The clock learns how fast the
  local timer runs from these samples and corrects errors by running up to 2 %
  faster or slower instead of jumping, so the shown time never goes backwards
  ([`core/src/timekeeping.rs`](core/src/timekeeping.rs)). Errors over 30 s are
  corrected at once. The serial log prints each sync's error and the measured
  timer rate.
- **Across resets.** The time survives a watchdog or panic reset
  ([`src/persist.rs`](src/persist.rs)): it is saved a few times a second in RTC
  memory, and the RTC timer bridges the reset. Until the next NTP sync, a
  single dot after the time marks it as not synced. After a power cycle the
  clock shows `--:--` until the first sync.
- **Temperature.** Fetched from [wttr.in](https://wttr.in) every 10 minutes:
  the actual air temperature (`%t`), rounded to whole degrees and clamped to
  −99…99. It shows `--°C` until the first reply, and again when the last reply
  is older than two hours.
- **WiFi.** A dedicated task reconnects after every disconnect. If NTP fails
  three times in a row while WiFi claims to be connected, it forces a
  reconnect.
- **Watchdog.** The hardware watchdog (30 s) is only fed while the display
  loop runs and an NTP or weather request has succeeded within the last
  10 minutes. So the board resets after 10 minutes without internet, after a
  minute of stalled display, or 30 s after the network core hangs. The time
  is kept across these resets (see above).

Settings in `config.toml` (copy [`config.example.toml`](config.example.toml)):

- WiFi and static IP address;
- time zone and NTP server;
- weather location.
