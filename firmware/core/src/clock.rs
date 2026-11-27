//! The clock screen: time on top, outside temperature below.

use core::fmt::Write as _;

use chrono::{TimeZone as _, Timelike as _};

use crate::{PanelFrame, font::FONT_3X5};

/// Top row of the time line.
pub const TIME_Y: i32 = 3;
/// Top row of the temperature line.
pub const TEMP_Y: i32 = 9;

/// What the clock screen shows.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ClockState {
    /// Local wall-clock time as (hour, minute), `None` until the first NTP sync.
    pub time: Option<(u8, u8)>,
    /// The time comes from NTP since this boot. If not (kept across a reset),
    /// a dot after the time says so.
    pub time_synced: bool,
    /// Outside temperature in °C, `None` if unknown or stale.
    pub temperature: Option<i32>,
}

/// Renders the clock screen.
pub fn render(state: &ClockState) -> PanelFrame {
    let mut frame = PanelFrame::new();
    let mut text = StrBuf::<16>::new();

    match state.time {
        Some((h, m)) => write!(text, "{h:02}:{m:02}"),
        None => write!(text, "--:--"),
    }
    .ok();
    let end = frame.draw_text_centered(TIME_Y, &FONT_3X5, text.as_str());
    if state.time.is_some() && !state.time_synced {
        // After the time, outside the centring, so the time does not move.
        frame.draw_text(end + FONT_3X5.spacing as i32, TIME_Y, &FONT_3X5, "*");
    }

    text.clear();
    match state.temperature {
        // Clamp so the line always fits the 28 columns.
        Some(t) => write!(text, "{}°C", t.clamp(-99, 99)),
        None => write!(text, "--°C"),
    }
    .ok();
    frame.draw_text_centered(TEMP_Y, &FONT_3X5, text.as_str());

    frame
}

/// Local (hour, minute) for `unix_us` microseconds since the Unix epoch.
pub fn local_hour_minute(unix_us: u64, tz: chrono_tz::Tz) -> (u8, u8) {
    let secs = (unix_us / 1_000_000) as i64;
    let local = tz.timestamp_opt(secs, 0).single().unwrap_or_else(|| {
        // timestamp_opt on a UTC instant is never ambiguous; keep a sane fallback.
        tz.timestamp_opt(0, 0).unwrap()
    });
    (local.hour() as u8, local.minute() as u8)
}

/// A fixed-capacity string for `write!` without an allocator. Text past the
/// capacity is silently dropped.
pub struct StrBuf<const N: usize> {
    buf: [u8; N],
    len: usize,
}

impl<const N: usize> StrBuf<N> {
    pub const fn new() -> Self {
        Self {
            buf: [0; N],
            len: 0,
        }
    }

    pub fn clear(&mut self) {
        self.len = 0;
    }

    pub fn as_str(&self) -> &str {
        // Only whole `&str`s are ever copied in, so this is always valid UTF-8.
        core::str::from_utf8(&self.buf[..self.len]).unwrap_or("")
    }
}

impl<const N: usize> Default for StrBuf<N> {
    fn default() -> Self {
        Self::new()
    }
}

impl<const N: usize> core::fmt::Write for StrBuf<N> {
    fn write_str(&mut self, s: &str) -> core::fmt::Result {
        if self.len + s.len() > N {
            return Err(core::fmt::Error);
        }
        self.buf[self.len..self.len + s.len()].copy_from_slice(s.as_bytes());
        self.len += s.len();
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::format;

    const BERLIN: chrono_tz::Tz = chrono_tz::Europe::Berlin;

    fn unix_us(y: i32, mo: u32, d: u32, h: u32, mi: u32) -> u64 {
        chrono::NaiveDate::from_ymd_opt(y, mo, d)
            .unwrap()
            .and_hms_opt(h, mi, 0)
            .unwrap()
            .and_utc()
            .timestamp_micros() as u64
    }

    #[test]
    fn berlin_daylight_saving() {
        // Winter: UTC+1
        assert_eq!(
            local_hour_minute(unix_us(2026, 11, 25, 18, 5), BERLIN),
            (19, 5)
        );
        // Summer: UTC+2
        assert_eq!(
            local_hour_minute(unix_us(2026, 7, 1, 10, 30), BERLIN),
            (12, 30)
        );
        // Switch to summer time 2026-03-29 01:00 UTC
        assert_eq!(
            local_hour_minute(unix_us(2026, 3, 29, 0, 59), BERLIN),
            (1, 59)
        );
        assert_eq!(
            local_hour_minute(unix_us(2026, 3, 29, 1, 0), BERLIN),
            (3, 0)
        );
        // Back to winter time 2026-10-25 01:00 UTC
        assert_eq!(
            local_hour_minute(unix_us(2026, 10, 25, 0, 59), BERLIN),
            (2, 59)
        );
        assert_eq!(
            local_hour_minute(unix_us(2026, 10, 25, 1, 0), BERLIN),
            (2, 0)
        );
    }

    #[test]
    fn renders_time_and_temperature() {
        let f = render(&ClockState {
            time: Some((12, 34)),
            time_synced: true,
            temperature: Some(-5),
        });
        assert_eq!(format!("{f:?}"), GOLDEN_12_34_MINUS_5);
    }

    #[test]
    fn unsynced_time_gets_a_dot_and_does_not_move() {
        let f = render(&ClockState {
            time: Some((12, 34)),
            time_synced: false,
            temperature: Some(-5),
        });
        let text = format!("{f:?}");
        let golden: std::vec::Vec<&str> = GOLDEN_12_34_MINUS_5.lines().collect();
        for (y, row) in text.lines().enumerate() {
            if y == TIME_Y as usize {
                assert_eq!(row, "......#..###...###.#.#.#....");
            } else {
                assert_eq!(row, golden[y]);
            }
        }
    }

    const GOLDEN_12_34_MINUS_5: &str = "\
............................
............................
............................
......#..###...###.#.#......
.....##....#.#...#.#.#......
......#..###...###.###......
......#..#...#...#...#......
.....###.###...###...#......
............................
..........###..##.###.......
..........#....##.#.........
......###.###.....#.........
............#.....#.........
..........###.....###.......
............................
............................
";

    #[test]
    fn unknown_values_show_dashes() {
        let f = render(&ClockState {
            time: None,
            time_synced: false,
            temperature: None,
        });
        let text = format!("{f:?}");
        let rows: std::vec::Vec<&str> = text.lines().collect();
        // "--:--": dashes on the middle row, colon dots above and below it
        assert_eq!(rows[TIME_Y as usize + 1], ".............#..............");
        assert_eq!(rows[TIME_Y as usize + 2], ".....###.###...###.###......");
        assert_eq!(rows[TEMP_Y as usize + 2], "......###.###.....#.........");
    }

    #[test]
    fn strbuf_truncates() {
        let mut b = StrBuf::<4>::new();
        assert!(write!(b, "abcdef").is_err());
        assert_eq!(b.as_str(), "");
        write!(b, "ab").unwrap();
        assert_eq!(b.as_str(), "ab");
    }
}
