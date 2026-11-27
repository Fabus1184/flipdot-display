//! `log` backend: prints to the serial console and keeps the last few KiB in
//! RAM for the status page.

use core::fmt::Write as _;

const CAPACITY: usize = 4096;

struct Ring {
    buf: [u8; CAPACITY],
    /// Index of the next byte to write.
    head: usize,
    full: bool,
}

impl Ring {
    fn push(&mut self, bytes: &[u8]) {
        for &b in bytes {
            self.buf[self.head] = b;
            self.head = (self.head + 1) % CAPACITY;
            self.full |= self.head == 0;
        }
    }

    fn for_each_chunk(&self, mut f: impl FnMut(&[u8])) {
        if self.full {
            f(&self.buf[self.head..]);
        }
        f(&self.buf[..self.head]);
    }
}

impl core::fmt::Write for Ring {
    fn write_str(&mut self, s: &str) -> core::fmt::Result {
        self.push(s.as_bytes());
        Ok(())
    }
}

static RING: spin::Mutex<Ring> = spin::Mutex::new(Ring {
    buf: [0; CAPACITY],
    head: 0,
    full: false,
});

struct Logger;

static LOGGER: Logger = Logger;

impl log::Log for Logger {
    fn enabled(&self, metadata: &log::Metadata) -> bool {
        metadata.level() <= log::max_level()
    }

    fn log(&self, record: &log::Record) {
        if !self.enabled(record.metadata()) {
            return;
        }
        let ms = esp_hal::time::Instant::now()
            .duration_since_epoch()
            .as_millis();
        let (s, ms) = (ms / 1000, ms % 1000);
        esp_println::println!(
            "[{s:6}.{ms:03} {:5} {}] {}",
            record.level(),
            record.target(),
            record.args()
        );
        let mut ring = RING.lock();
        let _ = writeln!(
            ring,
            "[{s:6}.{ms:03} {:5}] {}",
            record.level(),
            record.args()
        );
    }

    fn flush(&self) {}
}

pub fn init(level: log::LevelFilter) {
    // Only fails if a logger is already set, which is fine.
    let _ = log::set_logger(&LOGGER);
    log::set_max_level(level);
}

/// Appends the buffered log to `out`. Bytes cut in the middle of a UTF-8
/// sequence at the start of the buffer are dropped.
pub fn dump(out: &mut alloc::vec::Vec<u8>) {
    let ring = RING.lock();
    let start = out.len();
    ring.for_each_chunk(|chunk| out.extend_from_slice(chunk));
    // skip a partial first line when the ring has wrapped
    if ring.full
        && let Some(nl) = out[start..].iter().position(|&b| b == b'\n')
    {
        out.drain(start..=start + nl);
    }
}
