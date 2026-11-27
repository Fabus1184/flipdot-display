//! State shared between the network core and the display core.

use core::sync::atomic::{AtomicBool, AtomicU32, Ordering};

use esp_hal::time::{Duration, Instant};
use flipdot_core::timekeeping::{Clock, Sync};

/// Weather older than this is not shown.
pub const WEATHER_MAX_AGE: Duration = Duration::from_secs(2 * 60 * 60);

static CLOCK: spin::Mutex<Option<Clock>> = spin::Mutex::new(None);
static WEATHER: spin::Mutex<Option<(i32, Instant)>> = spin::Mutex::new(None);
static DISPLAY_HEARTBEAT: AtomicU32 = AtomicU32::new(0);
/// The clock has had an NTP sync since this boot.
static SYNCED: AtomicBool = AtomicBool::new(false);

/// Microseconds since boot, the clock's local time base.
pub fn local_us() -> u64 {
    Instant::now().duration_since_epoch().as_micros()
}

/// Feeds an NTP sample: it was `unix_us` at local time `local_us`. Returns
/// `None` for the first sample, which sets the clock.
pub fn sync_time(local_us: u64, unix_us: u64) -> Option<Sync> {
    let mut clock = CLOCK.lock();
    SYNCED.store(true, Ordering::Relaxed);
    match clock.as_mut() {
        Some(c) => Some(c.sync(local_us, unix_us)),
        None => {
            *clock = Some(Clock::new(local_us, unix_us));
            None
        }
    }
}

/// Sets the clock from a time kept across a reset, until NTP confirms it.
pub fn resume_time(unix_us: u64, rate_ppb: i64) {
    let mut clock = CLOCK.lock();
    if clock.is_none() {
        *clock = Some(Clock::resume(local_us(), unix_us, rate_ppb));
    }
}

/// Whether the time comes from NTP since this boot.
pub fn time_synced() -> bool {
    SYNCED.load(Ordering::Relaxed)
}

/// Current Unix time and the clock's learned rate, for saving.
pub fn clock_snapshot() -> Option<(u64, i64)> {
    let clock = CLOCK.lock();
    clock.as_ref().map(|c| (c.now(local_us()), c.rate_ppb()))
}

/// Current Unix time in microseconds, `None` before the first NTP sync.
/// Never goes backwards.
pub fn unix_now_us() -> Option<u64> {
    let clock = CLOCK.lock();
    clock.as_ref().map(|c| c.now(local_us()))
}

pub fn set_temperature(celsius: i32) {
    *WEATHER.lock() = Some((celsius, Instant::now()));
}

/// The last temperature, unless it is older than [`WEATHER_MAX_AGE`].
pub fn temperature() -> Option<i32> {
    let (celsius, at) = (*WEATHER.lock())?;
    (at.elapsed() <= WEATHER_MAX_AGE).then_some(celsius)
}

/// Called by the display loop on every iteration.
pub fn display_alive() {
    DISPLAY_HEARTBEAT.fetch_add(1, Ordering::Relaxed);
}

pub fn display_heartbeat() -> u32 {
    DISPLAY_HEARTBEAT.load(Ordering::Relaxed)
}
