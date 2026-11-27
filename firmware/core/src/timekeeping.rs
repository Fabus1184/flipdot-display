//! Wall-clock time between NTP syncs.
//!
//! The board only has a local timer, which runs at its own rate. [`Clock`]
//! learns that rate from successive NTP samples. It applies corrections by
//! running slightly faster or slower for a while (slewing) instead of jumping,
//! so the time it reports never goes backwards and the display never shows a
//! minute twice. It only jumps (steps) on errors above [`STEP_THRESHOLD_US`].
//!
//! All times are in microseconds: `local` counts from boot, `unix` from the
//! Unix epoch.

/// Errors larger than this are corrected at once instead of slewed.
pub const STEP_THRESHOLD_US: i64 = 30_000_000;
/// Speed of a correction: 2 % of elapsed time, so 1 s takes 50 s.
pub const SLEW_PPB: i64 = 20_000_000;
/// Largest rate error that is believed (5 %).
pub const MAX_RATE_PPB: i64 = 50_000_000;
/// Samples closer together than this are not used to measure the rate.
pub const MIN_RATE_INTERVAL_US: u64 = 60_000_000;

const PPB: i128 = 1_000_000_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Clock {
    /// The estimate is `anchor_unix` at `anchor_local` …
    anchor_local: u64,
    anchor_unix: u64,
    /// … then advances by (1 + rate) per local microsecond …
    rate_ppb: i64,
    /// … plus this correction, applied at [`SLEW_PPB`].
    pending_us: i64,
    /// The previous NTP sample, to measure the rate. `None` after
    /// [`Clock::resume`]: a time carried over a reset is not exact enough.
    last_sample: Option<(u64, u64)>,
    rate_known: bool,
}

/// What a sync did.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Sync {
    /// NTP time minus the clock's estimate before the sync.
    pub error_us: i64,
    /// The error was too large to slew, the clock jumped.
    pub stepped: bool,
    /// Current estimate of how much faster the local timer runs than real
    /// time, in parts per billion (negative: slower).
    pub rate_ppb: i64,
}

impl Clock {
    /// A clock set from its first NTP sample.
    pub fn new(local: u64, unix: u64) -> Self {
        Self {
            anchor_local: local,
            anchor_unix: unix,
            rate_ppb: 0,
            pending_us: 0,
            last_sample: Some((local, unix)),
            rate_known: false,
        }
    }

    /// A clock continuing from a time kept across a reset, with the rate
    /// learned before it.
    pub fn resume(local: u64, unix: u64, rate_ppb: i64) -> Self {
        Self {
            anchor_local: local,
            anchor_unix: unix,
            rate_ppb: rate_ppb.clamp(-MAX_RATE_PPB, MAX_RATE_PPB),
            pending_us: 0,
            last_sample: None,
            rate_known: rate_ppb != 0,
        }
    }

    /// How much faster the local timer runs than real time, in parts per
    /// billion, as far as known.
    pub fn rate_ppb(&self) -> i64 {
        self.rate_ppb
    }

    /// Unix time at local time `local`. Never decreases as `local` grows.
    pub fn now(&self, local: u64) -> u64 {
        let dt = local.saturating_sub(self.anchor_local) as i128;
        let drift = dt * self.rate_ppb as i128 / PPB;
        let slew_max = dt * SLEW_PPB as i128 / PPB;
        let slew = (self.pending_us as i128).clamp(-slew_max, slew_max);
        (self.anchor_unix as i128 + dt + drift + slew).max(0) as u64
    }

    /// Feeds an NTP sample: it was `unix` at local time `local`.
    pub fn sync(&mut self, local: u64, unix: u64) -> Sync {
        let estimate = self.now(local);
        let error_us =
            (unix as i128 - estimate as i128).clamp(i64::MIN as i128, i64::MAX as i128) as i64;

        // Rate of the local timer from the previous sample to this one. The
        // samples are raw NTP times, so earlier corrections do not matter.
        if let Some((l0, u0)) = self.last_sample
            && local.saturating_sub(l0) >= MIN_RATE_INTERVAL_US
        {
            let dl = local - l0;
            let du = unix as i128 - u0 as i128;
            // local runs (dl - du) / du too fast; as a correction factor for
            // local time that is (du - dl) / dl.
            let measured = ((du - dl as i128) * PPB / dl as i128)
                .clamp(-(MAX_RATE_PPB as i128), MAX_RATE_PPB as i128)
                as i64;
            self.rate_ppb = if self.rate_known {
                self.rate_ppb + (measured - self.rate_ppb) / 4
            } else {
                measured
            };
            self.rate_known = true;
        }
        self.last_sample = Some((local, unix));

        let stepped = error_us.unsigned_abs() > STEP_THRESHOLD_US as u64;
        self.anchor_local = local;
        if stepped {
            self.anchor_unix = unix;
            self.pending_us = 0;
        } else {
            // Continue from the current estimate, slew the rest.
            self.anchor_unix = estimate;
            self.pending_us = error_us;
        }
        Sync {
            error_us,
            stepped,
            rate_ppb: self.rate_ppb,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const S: u64 = 1_000_000;
    const T0: u64 = 1_790_000_000 * S;

    /// A local timer that runs `ppm` too fast: real time for local time `l`.
    fn real(l: u64, ppm: i64) -> u64 {
        T0 + (l as i128 * 1_000_000 / (1_000_000 + ppm as i128)) as u64
    }

    #[test]
    fn first_sample_is_exact() {
        let c = Clock::new(5 * S, T0);
        assert_eq!(c.now(5 * S), T0);
        assert_eq!(c.now(6 * S), T0 + S);
    }

    #[test]
    fn never_goes_back_and_converges() {
        // 0.5 % fast: 3 s per 10 minutes, as seen on the board.
        let ppm = 5000;
        let mut c = Clock::new(0, real(0, ppm));
        let mut last = 0;
        let mut l = 0;
        for sync in 1..=6 {
            let next = sync * 600 * S;
            while l < next {
                l += 100_000;
                let t = c.now(l);
                assert!(t >= last, "time went back at local {l}");
                last = t;
            }
            let r = c.sync(l, real(l, ppm));
            assert!(!r.stepped);
        }
        // After a few syncs the rate is learned and the error is small.
        let r = c.sync(l + 600 * S, real(l + 600 * S, ppm));
        assert!(r.error_us.abs() < 50_000, "error {} µs", r.error_us);
        assert!(
            (r.rate_ppb + 4_975_000).abs() < 50_000,
            "rate {}",
            r.rate_ppb
        );
    }

    #[test]
    fn minute_is_not_shown_twice() {
        // The bug this fixes: the clock showed hh:01, the sync set it back to
        // hh:00:58, the display went back to hh:00.
        let mut c = Clock::new(0, T0);
        let l = 600 * S;
        let before = c.now(l);
        c.sync(l, before - 3 * S);
        for k in 0..2000 {
            assert!(c.now(l + k * 100_000) >= before);
        }
        // The 3 s are worked off within 150 s at 2 %; from now on the clock
        // also runs 0.5 % slower, the rate the sample implies.
        assert_eq!(c.now(l + 200 * S), before - 3 * S + 199 * S);
    }

    #[test]
    fn resumed_clock_keeps_its_rate_and_slews_the_reset_error() {
        // Before the reset the rate was learned; the time carried over the
        // reset is 300 ms late.
        let ppm = 5000;
        let rate = -4_975_124;
        let mut c = Clock::resume(1_000_000, real(0, ppm) - 300_000, rate);
        let l = 120 * S;
        let truth = real(l - 1_000_000, ppm);
        let r = c.sync(l, truth);
        assert!(!r.stepped);
        assert!((r.error_us - 300_000).abs() < 5_000, "error {}", r.error_us);
        // The bridged sample was not used to measure the rate.
        assert_eq!(r.rate_ppb, rate);
    }

    #[test]
    fn large_errors_step() {
        let mut c = Clock::new(0, T0);
        let r = c.sync(60 * S, T0 + 3600 * S);
        assert!(r.stepped);
        assert_eq!(c.now(60 * S), T0 + 3600 * S);
    }
}
