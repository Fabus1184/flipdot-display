//! Hardware-independent logic for the flipdot display.
//!
//! Everything in here is `no_std`, has no ESP32 dependencies and is unit-tested
//! on the host with a plain `cargo test`. The firmware crate only glues this to
//! GPIOs and the network stack.

#![no_std]

#[cfg(test)]
extern crate std;

pub mod clock;
pub mod font;
pub mod fp2800;
pub mod frame;
pub mod ntp;
pub mod panel;
pub mod timekeeping;
pub mod weather;

pub use frame::Frame;

/// Columns of the panel.
pub const WIDTH: usize = 28;
/// Rows of the panel.
pub const HEIGHT: usize = 16;

/// A frame the size of the panel.
pub type PanelFrame = Frame<WIDTH, HEIGHT>;
