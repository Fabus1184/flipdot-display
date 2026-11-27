//! The panel driver: flips one dot at a time.
//!
//! **Safety rule: never more than one dot at once.** The FP2800A drives one
//! column and carries the current of every row switch that is on. Two row
//! switches on during a pulse means two coils on one FP2800A output, which
//! overloads it. A high-side and a low-side switch on at the same time is worse:
//! current then flows from +HV to GND through the coils and diodes of every
//! column, without the FP2800A at all.
//!
//! So this driver keeps these invariants, and the tests below check them on
//! every pin change against a simulated 74HC595 chain:
//!
//! 1. The shift register chain is only latched while its outputs are off
//!    (`SR_OE#` high).
//! 2. The outputs are only switched on with exactly one of the 32 row bits
//!    latched.
//! 3. `FP_EN` only goes high while exactly one row switch is on, and only for
//!    [`PULSE_US`]; the address and `FP_DATA` do not change while it is high.
//! 4. The pulse runs with interrupts masked, so nothing can stretch it.
//! 5. At start-up the chain is cleared before its outputs are ever switched on.
//!
//! The hardware covers the time before the firmware runs: R14 pulls `SR_OE#`
//! high, so all row switches are off during reset and boot.

use crate::{HEIGHT, PanelFrame, WIDTH, fp2800};

/// Length of the coil pulse.
pub const PULSE_US: u32 = 500;
/// Pause after each pulse so the driver and the supply recover.
pub const PULSE_GAP_US: u32 = 500;
/// Setup/hold time for the shift register and the address lines.
pub const SETTLE_US: u32 = 1;
/// Row bits in the 74HC595 chain: 16 high-side, 16 low-side.
pub const RAILS: u8 = 32;

/// The GPIOs the driver needs. Implemented by the firmware for the ESP32 and
/// by the tests for a simulated board.
pub trait PanelPins {
    /// `SR_DATA`, serial input of the 74HC595 chain.
    fn sr_data(&mut self, high: bool);
    /// `SR_ICLK`, shift clock; shifts on the rising edge.
    fn sr_shift_clock(&mut self, high: bool);
    /// `SR_OCLK`, latch clock; copies the chain to the outputs on the rising edge.
    fn sr_latch_clock(&mut self, high: bool);
    /// `SR_OE#`: high switches all 32 outputs, so all row switches, off.
    fn sr_output_enable_n(&mut self, high: bool);
    /// `FP_EN`: the FP2800A drives the addressed column while high.
    fn fp_enable(&mut self, high: bool);
    /// `FP_DATA`: whether the column sources or sinks.
    fn fp_data(&mut self, high: bool);
    /// `FP_A0..A2`, `FP_B0..B1` as bits 0..4.
    fn fp_address(&mut self, address: u8);
    fn delay_us(&mut self, us: u32);
    /// Runs `f` with interrupts on this core masked, so the pulse cannot be
    /// stretched by an interrupt or a task switch.
    fn uninterrupted(&mut self, f: impl FnOnce(&mut Self));
}

/// One of the 32 row switches: `row` driven to the "bright" or the "dark"
/// side.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Rail(u8);

impl Rail {
    fn new(row: usize, bright: bool) -> Self {
        assert!(row < HEIGHT);
        let index = if bright { row } else { row + HEIGHT };
        Rail(index as u8)
    }
}

/// The whole panel. Remembers what it shows so updates only flip changed dots.
pub struct Panel<P: PanelPins> {
    pins: P,
    shown: PanelFrame,
}

impl<P: PanelPins> Panel<P> {
    /// Takes over the pins and puts the board into a safe state: column driver
    /// and row switches off, shift register chain cleared.
    ///
    /// The physical state of the dots is unknown at power-on: call
    /// [`Panel::force_fill`] before the first [`Panel::show`].
    pub fn new(mut pins: P) -> Self {
        pins.fp_enable(false);
        pins.sr_output_enable_n(true);
        pins.sr_shift_clock(false);
        pins.sr_latch_clock(false);
        let mut panel = Self {
            pins,
            shown: PanelFrame::new(),
        };
        panel.load_chain(None);
        panel
    }

    /// Shifts 32 bits into the chain and latches them: all zero, or only bit
    /// `rail` set (bit 0 is shifted in first). The outputs must be off.
    fn load_chain(&mut self, rail: Option<Rail>) {
        self.pins.sr_output_enable_n(true);
        let mut ones = 0;
        for i in 0..RAILS {
            let bit = rail == Some(Rail(i));
            ones += bit as u8;
            self.pins.sr_data(bit);
            self.pins.delay_us(SETTLE_US);
            self.pins.sr_shift_clock(true);
            self.pins.delay_us(SETTLE_US);
            self.pins.sr_shift_clock(false);
        }
        assert!(ones == rail.is_some() as u8);
        self.pins.sr_data(false);
        self.pins.sr_latch_clock(true);
        self.pins.delay_us(SETTLE_US);
        self.pins.sr_latch_clock(false);
    }

    /// Pulses the dot at column `x` (from the left) and row `y` (from the top).
    fn flip(&mut self, x: usize, y: usize, bright: bool) {
        assert!(x < WIDTH && y < HEIGHT);
        let rail = Rail::new(y, bright);
        // The FP2800A outputs run right to left on this panel and need the
        // opposite polarity of the row rail.
        let column = (WIDTH - 1 - x) as u8;
        let address = fp2800::column_address(column);

        self.pins.fp_enable(false);
        self.load_chain(Some(rail));
        self.pins.fp_data(!bright);
        self.pins.fp_address(address);
        self.pins.delay_us(SETTLE_US);

        self.pins.uninterrupted(|pins| {
            pins.sr_output_enable_n(false);
            pins.delay_us(SETTLE_US);
            pins.fp_enable(true);
            pins.delay_us(PULSE_US);
            pins.fp_enable(false);
            pins.delay_us(SETTLE_US);
            pins.sr_output_enable_n(true);
        });
        self.pins.delay_us(PULSE_GAP_US);
    }

    /// Pulses every dot, whatever it shows, to bring the panel into a known
    /// state.
    pub fn force_fill(&mut self, bright: bool) {
        for y in 0..HEIGHT {
            for x in 0..WIDTH {
                self.flip(x, y, bright);
            }
        }
        self.shown = PanelFrame::filled(bright);
    }

    /// Shows `frame`, flipping only the dots that differ from what is shown.
    /// Returns the number of flipped dots.
    pub fn show(&mut self, frame: &PanelFrame) -> usize {
        let old = self.shown;
        let mut flipped = 0;
        for (x, y, bright) in old.diff(frame) {
            self.flip(x, y, bright);
            flipped += 1;
        }
        self.shown = *frame;
        flipped
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::vec::Vec;

    /// A board with a 32-bit 74HC595 chain that checks the safety rules on
    /// every pin change and panics on the first violation.
    #[derive(Default)]
    struct Board {
        chain: u32,
        latched: u32,
        oe_n: bool,
        fp_en: bool,
        fp_data: bool,
        address: u8,
        data: bool,
        shift_clock: bool,
        latch_clock: bool,
        masked: bool,
        now_us: u64,
        pulse_start: u64,
        /// (latched rows, FP2800A address, FP_DATA) of every pulse.
        pulses: Vec<(u32, u8, bool)>,
    }

    impl Board {
        fn new() -> Self {
            // Power-on: random chain and outputs, OE# pulled high by R14.
            Board {
                chain: 0xdead_beef,
                latched: 0x0f0f_1234,
                oe_n: true,
                ..Default::default()
            }
        }
        fn rows_on(&self) -> u32 {
            if self.oe_n { 0 } else { self.latched }
        }
    }

    impl PanelPins for Board {
        fn sr_data(&mut self, high: bool) {
            self.data = high;
        }
        fn sr_shift_clock(&mut self, high: bool) {
            if high && !self.shift_clock {
                self.chain = (self.chain << 1) | self.data as u32;
            }
            self.shift_clock = high;
        }
        fn sr_latch_clock(&mut self, high: bool) {
            if high && !self.latch_clock {
                assert!(self.oe_n, "rule 1: latched while the outputs are on");
                self.latched = self.chain;
            }
            self.latch_clock = high;
        }
        fn sr_output_enable_n(&mut self, high: bool) {
            if !high && self.oe_n {
                assert_eq!(
                    self.latched.count_ones(),
                    1,
                    "rule 2: outputs on with {:032b}",
                    self.latched
                );
            }
            if high && !self.oe_n {
                assert!(!self.fp_en, "row switches cut while FP_EN is high");
            }
            self.oe_n = high;
        }
        fn fp_enable(&mut self, high: bool) {
            if high && !self.fp_en {
                assert_eq!(
                    self.rows_on().count_ones(),
                    1,
                    "rule 3: FP_EN without exactly one row"
                );
                assert!(self.masked, "rule 4: pulse with interrupts enabled");
                self.pulse_start = self.now_us;
                self.pulses.push((self.latched, self.address, self.fp_data));
            }
            if !high && self.fp_en {
                assert!(
                    self.now_us - self.pulse_start <= PULSE_US as u64,
                    "rule 3: pulse too long"
                );
            }
            self.fp_en = high;
        }
        fn fp_data(&mut self, high: bool) {
            assert!(!self.fp_en, "rule 3: FP_DATA changed during a pulse");
            self.fp_data = high;
        }
        fn fp_address(&mut self, address: u8) {
            assert!(!self.fp_en, "rule 3: address changed during a pulse");
            self.address = address;
        }
        fn delay_us(&mut self, us: u32) {
            self.now_us += us as u64;
        }
        fn uninterrupted(&mut self, f: impl FnOnce(&mut Self)) {
            self.masked = true;
            f(self);
            self.masked = false;
        }
    }

    #[test]
    fn start_up_clears_the_chain_before_anything_is_switched_on() {
        let panel = Panel::new(Board::new());
        assert_eq!(panel.pins.latched, 0);
        assert!(panel.pins.oe_n && !panel.pins.fp_en);
        assert!(panel.pins.pulses.is_empty());
    }

    #[test]
    fn every_dot_is_one_pulse_with_one_row() {
        let mut panel = Panel::new(Board::new());
        panel.force_fill(false);
        panel.force_fill(true);
        assert_eq!(panel.pins.pulses.len(), 2 * WIDTH * HEIGHT);
        for &(rows, address, _) in &panel.pins.pulses {
            assert_eq!(rows.count_ones(), 1);
            assert!(address % 8 != 0 && address < 32);
        }
        // Ends with everything off.
        assert!(panel.pins.oe_n && !panel.pins.fp_en);
    }

    #[test]
    fn each_row_and_colour_has_its_own_switch() {
        let mut panel = Panel::new(Board::new());
        let mut seen = std::collections::BTreeSet::new();
        for y in 0..HEIGHT {
            for bright in [false, true] {
                panel.flip(0, y, bright);
                seen.insert(panel.pins.pulses.last().unwrap().0);
            }
        }
        assert_eq!(seen.len(), 2 * HEIGHT);
    }

    #[test]
    fn updates_flip_only_changed_dots_one_by_one() {
        let mut panel = Panel::new(Board::new());
        panel.force_fill(false);
        let before = panel.pins.pulses.len();
        let mut frame = PanelFrame::new();
        // A pseudo-random pattern.
        let mut seed = 12345u32;
        let mut lit = 0;
        for y in 0..HEIGHT as i32 {
            for x in 0..WIDTH as i32 {
                seed = seed.wrapping_mul(1_103_515_245).wrapping_add(12345);
                let on = seed >> 30 == 0;
                frame.set(x, y, on);
                lit += on as usize;
            }
        }
        assert_eq!(panel.show(&frame), lit);
        assert_eq!(panel.pins.pulses.len() - before, lit);
        assert_eq!(panel.show(&frame), 0);
    }

    #[test]
    #[should_panic(expected = "rule 2")]
    fn the_simulator_catches_two_rows() {
        // Check the checker: latch two bits and switch the outputs on.
        let mut board = Board::new();
        board.sr_output_enable_n(true);
        board.chain = 0b11;
        board.sr_latch_clock(true);
        board.sr_output_enable_n(false);
    }
}
