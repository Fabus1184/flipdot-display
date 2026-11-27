//! Keeps the time across resets (watchdog, panic), not across power cycles.
//!
//! The display loop saves "Unix time U at RTC time R" a few times a second
//! into RTC fast memory, which a reset does not clear. The RTC timer keeps
//! counting through the reset, so at boot the time is U plus the RTC time
//! since R.
//!
//! The RTC timer runs from an RC oscillator whose real rate can be several
//! percent off its nominal one. So the loop also measures that rate against
//! the crystal-driven timer and saves it; the reset gap is only a second or
//! two anyway. RTC fast memory is only reachable from core 0, where the
//! display loop runs.

use esp_hal::rtc_cntl::Rtc;

const MAGIC: u64 = 0x666c_6970_646f_7401; // "flipdot" + version
/// A saved time older than this is not trusted.
const MAX_GAP_US: u64 = 60 * 60 * 1_000_000;
/// The RTC rate is measured over at least this much crystal time.
const MIN_CALIBRATION_US: u64 = 10_000_000;

// Per slot: magic, sequence, rtc_us, unix_us, rate_ppb, rtc_scale_ppb, checksum.
const WORDS: usize = 7;

/// Two slots, written alternately, so a reset in the middle of a write still
/// leaves the other one intact.
#[esp_hal::ram(unstable(rtc_fast, persistent))]
static mut SLOTS: [[u64; WORDS]; 2] = [[0; WORDS]; 2];

fn checksum(words: &[u64]) -> u64 {
    words.iter().fold(0x9e37_79b9_7f4a_7c15, |h, w| {
        (h ^ w).rotate_left(23).wrapping_mul(0x0100_0000_01b3)
    })
}

fn read_slot(i: usize) -> [u64; WORDS] {
    // SAFETY: only the display loop on core 0 touches SLOTS.
    unsafe { core::ptr::read_volatile(&raw const SLOTS[i]) }
}

fn write_slot(i: usize, slot: [u64; WORDS]) {
    // SAFETY: as above.
    unsafe { core::ptr::write_volatile(&raw mut SLOTS[i], slot) }
}

/// The time restored at boot.
pub struct Restored {
    pub unix_us: u64,
    pub rate_ppb: i64,
    pub gap_us: u64,
}

pub struct Persist {
    rtc: Rtc<'static>,
    sequence: u64,
    /// RTC and crystal time at boot, to measure the RTC rate.
    start: (u64, u64),
    /// How much the RTC's real rate differs from its nominal one, in ppb.
    rtc_scale_ppb: i64,
}

impl Persist {
    /// Reads the saved time, if there is a valid one. Call early at boot.
    pub fn restore(rtc: Rtc<'static>) -> (Self, Option<Restored>) {
        let now_rtc = rtc.time_since_boot().as_micros();
        let local = crate::state::local_us();
        let best = (0..2)
            .map(read_slot)
            .filter(|s| s[0] == MAGIC && s[WORDS - 1] == checksum(&s[..WORDS - 1]))
            .max_by_key(|s| s[1]);
        let mut persist = Self {
            rtc,
            sequence: best.map_or(0, |s| s[1]),
            start: (now_rtc, local),
            rtc_scale_ppb: best.map_or(0, |s| s[5] as i64),
        };
        let restored = best.and_then(|s| {
            // After a power cycle the RTC counts from zero again: now < saved.
            let gap_nominal = now_rtc.checked_sub(s[2])?;
            let gap_us = (gap_nominal as i128 * (1_000_000_000 + persist.rtc_scale_ppb as i128)
                / 1_000_000_000) as u64;
            (gap_us <= MAX_GAP_US).then_some(Restored {
                unix_us: s[3] + gap_us,
                rate_ppb: s[4] as i64,
                gap_us,
            })
        });
        if restored.is_none() {
            persist.rtc_scale_ppb = 0;
        }
        (persist, restored)
    }

    /// Saves that it is `unix_us` now; `rate_ppb` is the clock's learned rate.
    pub fn save(&mut self, unix_us: u64, rate_ppb: i64) {
        let rtc_us = self.rtc.time_since_boot().as_micros();
        let local = crate::state::local_us();
        let (rtc0, local0) = self.start;
        let (d_rtc, d_local) = (rtc_us.saturating_sub(rtc0), local.saturating_sub(local0));
        if d_local >= MIN_CALIBRATION_US && d_rtc > 0 {
            self.rtc_scale_ppb = ((d_local as i128 - d_rtc as i128) * 1_000_000_000 / d_rtc as i128)
                .clamp(-200_000_000, 200_000_000) as i64;
        }
        self.sequence += 1;
        let mut slot = [
            MAGIC,
            self.sequence,
            rtc_us,
            unix_us,
            rate_ppb as u64,
            self.rtc_scale_ppb as u64,
            0,
        ];
        slot[WORDS - 1] = checksum(&slot[..WORDS - 1]);
        write_slot((self.sequence % 2) as usize, slot);
    }
}
