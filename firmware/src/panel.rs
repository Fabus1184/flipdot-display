//! The panel's GPIOs. The flip sequence and its safety rules live in
//! [`flipdot_core::panel`]; this file only connects them to the ESP32 pins.

use esp_hal::gpio::{AnyPin, Level, Output, OutputConfig, Pin as _};
use esp_hal::peripherals;
use esp_hal::time::{Duration, Instant};
pub use flipdot_core::panel::Panel;
use flipdot_core::panel::PanelPins;

/// Pin assignment, see hardware/README.md.
pub struct Pins {
    pub sr_shift_clock: AnyPin<'static>,     // SR_ICLK, GPIO15
    pub sr_latch_clock: AnyPin<'static>,     // SR_OCLK, GPIO2
    pub sr_data: AnyPin<'static>,            // SR_DATA, GPIO4
    pub sr_output_enable_n: AnyPin<'static>, // SR_OE#, GPIO16
    pub fp_enable: AnyPin<'static>,          // FP_EN, GPIO13
    pub fp_data: AnyPin<'static>,            // FP_DATA, GPIO12
    pub fp_address: [AnyPin<'static>; 5],    // FP_A0..A2, FP_B0..B1
}

pub struct EspPins {
    sr_shift_clock: Output<'static>,
    sr_latch_clock: Output<'static>,
    sr_data: Output<'static>,
    sr_output_enable_n: Output<'static>,
    fp_enable: Output<'static>,
    fp_data: Output<'static>,
    fp_address: [Output<'static>; 5],
}

fn output(pin: AnyPin<'static>, level: Level) -> Output<'static> {
    Output::new(pin, level, OutputConfig::default())
}

impl EspPins {
    /// Configures the pins. `SR_OE#` starts high and `FP_EN` low, so nothing
    /// is switched on between reset and [`Panel::new`].
    pub fn new(p: Pins) -> Self {
        Self {
            sr_output_enable_n: output(p.sr_output_enable_n, Level::High),
            fp_enable: output(p.fp_enable, Level::Low),
            sr_shift_clock: output(p.sr_shift_clock, Level::Low),
            sr_latch_clock: output(p.sr_latch_clock, Level::Low),
            sr_data: output(p.sr_data, Level::Low),
            fp_data: output(p.fp_data, Level::Low),
            fp_address: p.fp_address.map(|pin| output(pin, Level::Low)),
        }
    }
}

fn busy_wait(us: u32) {
    let start = Instant::now();
    let duration = Duration::from_micros(us as u64);
    while start.elapsed() < duration {}
}

impl PanelPins for EspPins {
    fn sr_data(&mut self, high: bool) {
        self.sr_data.set_level(Level::from(high));
    }
    fn sr_shift_clock(&mut self, high: bool) {
        self.sr_shift_clock.set_level(Level::from(high));
    }
    fn sr_latch_clock(&mut self, high: bool) {
        self.sr_latch_clock.set_level(Level::from(high));
    }
    fn sr_output_enable_n(&mut self, high: bool) {
        self.sr_output_enable_n.set_level(Level::from(high));
    }
    fn fp_enable(&mut self, high: bool) {
        self.fp_enable.set_level(Level::from(high));
    }
    fn fp_data(&mut self, high: bool) {
        self.fp_data.set_level(Level::from(high));
    }
    fn fp_address(&mut self, address: u8) {
        for (bit, pin) in self.fp_address.iter_mut().enumerate() {
            pin.set_level(Level::from(address >> bit & 1 == 1));
        }
    }
    fn delay_us(&mut self, us: u32) {
        busy_wait(us);
    }
    fn uninterrupted(&mut self, f: impl FnOnce(&mut Self)) {
        // Masks interrupts on this core only. A critical section would also
        // take the lock the other core needs, stalling the network for the
        // whole pulse.
        esp_hal::xtensa_lx::interrupt::free(|| f(self));
    }
}

/// Switches the column driver and all row switches off, whatever owns the
/// pins. For the panic handler: a panic in the middle of a pulse must not
/// leave a coil powered.
pub fn emergency_off() {
    // SAFETY: only called on the way to a reset. The pins are reconfigured as
    // outputs driving their safe levels. If the other core is still running
    // the display, it keeps driving them too; each of its pulses is still a
    // single, checked dot, and a cut pulse does no harm.
    // (Output has no Drop: the pins keep driving these levels.)
    unsafe {
        Output::new(
            peripherals::GPIO13::steal().degrade(),
            Level::Low,
            OutputConfig::default(),
        );
        Output::new(
            peripherals::GPIO16::steal().degrade(),
            Level::High,
            OutputConfig::default(),
        );
    }
}
