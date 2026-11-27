//! Minimal SNTP (RFC 4330) client packet handling.

/// Seconds between the NTP epoch (1900-01-01) and the Unix epoch (1970-01-01).
pub const NTP_UNIX_OFFSET_SECS: u64 = 2_208_988_800;

/// Size of an NTP packet without extensions.
pub const PACKET_LEN: usize = 48;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NtpError {
    TooShort(usize),
    /// The reply is not a server reply (mode 4).
    NotServerMode(u8),
    /// Stratum 0 is a "kiss-o'-death" packet, the server wants us to back off.
    KissOfDeath,
    /// Leap indicator 3: the server clock is not synchronised.
    Unsynchronised,
    /// The reply does not answer our request (stale or spoofed packet).
    WrongOriginate,
    /// The transmit timestamp is zero or before 2020.
    BadTimestamp,
}

/// A decoded server reply.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Reply {
    /// Server time when the reply was sent, microseconds since the Unix epoch.
    pub transmit_unix_us: u64,
    /// Time the server held our request (transmit − receive), microseconds.
    pub server_hold_us: u64,
}

/// Builds a client request. `nonce` goes into the transmit timestamp and
/// must come back as the originate timestamp of the reply.
pub fn request(nonce: u64) -> [u8; PACKET_LEN] {
    let mut p = [0u8; PACKET_LEN];
    p[0] = 0b00_100_011; // LI 0, version 4, mode 3 (client)
    p[40..48].copy_from_slice(&nonce.to_be_bytes());
    p
}

/// Parses and validates the reply to the request built with `nonce`.
pub fn parse_reply(packet: &[u8], nonce: u64) -> Result<Reply, NtpError> {
    if packet.len() < PACKET_LEN {
        return Err(NtpError::TooShort(packet.len()));
    }
    let leap = packet[0] >> 6;
    let mode = packet[0] & 0b111;
    let stratum = packet[1];
    let ts = |at: usize| u64::from_be_bytes(packet[at..at + 8].try_into().unwrap());

    if mode != 4 {
        return Err(NtpError::NotServerMode(mode));
    }
    if stratum == 0 {
        return Err(NtpError::KissOfDeath);
    }
    if leap == 3 {
        return Err(NtpError::Unsynchronised);
    }
    if ts(24) != nonce {
        return Err(NtpError::WrongOriginate);
    }

    let receive = ts(32);
    let transmit = ts(40);
    if transmit == 0 {
        return Err(NtpError::BadTimestamp);
    }
    let transmit_unix_us = ntp_to_unix_us(transmit).ok_or(NtpError::BadTimestamp)?;
    let server_hold_us = ntp_to_us(transmit.saturating_sub(receive));

    Ok(Reply {
        transmit_unix_us,
        server_hold_us,
    })
}

/// Converts a 32.32 fixed-point NTP duration to microseconds.
fn ntp_to_us(ts: u64) -> u64 {
    let secs = ts >> 32;
    let frac = ts & 0xFFFF_FFFF;
    secs * 1_000_000 + ((frac * 1_000_000) >> 32)
}

/// Converts an NTP timestamp to microseconds since the Unix epoch. Handles
/// the NTP era rollover in 2036. Returns `None` for timestamps before 2020,
/// which no working server sends.
pub fn ntp_to_unix_us(ts: u64) -> Option<u64> {
    const Y2020_UNIX_SECS: u64 = 1_577_836_800;
    let mut secs = ts >> 32;
    // Era 1 starts in February 2036; timestamps that small must be era 1.
    if secs < NTP_UNIX_OFFSET_SECS {
        secs += 1 << 32;
    }
    let unix_secs = secs - NTP_UNIX_OFFSET_SECS;
    if unix_secs < Y2020_UNIX_SECS {
        return None;
    }
    let frac_us = ((ts & 0xFFFF_FFFF) * 1_000_000) >> 32;
    Some(unix_secs * 1_000_000 + frac_us)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn reply(nonce: u64, receive: u64, transmit: u64) -> [u8; PACKET_LEN] {
        let mut p = [0u8; PACKET_LEN];
        p[0] = 0b00_100_100; // LI 0, v4, server
        p[1] = 2;
        p[24..32].copy_from_slice(&nonce.to_be_bytes());
        p[32..40].copy_from_slice(&receive.to_be_bytes());
        p[40..48].copy_from_slice(&transmit.to_be_bytes());
        p
    }

    // 2026-11-25 12:00:00 UTC
    const UNIX: u64 = 1_795_608_000;
    const NTP: u64 = (UNIX + NTP_UNIX_OFFSET_SECS) << 32;

    #[test]
    fn request_layout() {
        let r = request(0x1122_3344_5566_7788);
        assert_eq!(r[0], 0x23);
        assert_eq!(&r[40..], &0x1122_3344_5566_7788u64.to_be_bytes());
        assert!(r[1..40].iter().all(|&b| b == 0));
    }

    #[test]
    fn converts_to_unix_epoch() {
        // The old firmware forgot this offset and ran 70 years in the future.
        let half_second = 1u64 << 31;
        let r = parse_reply(&reply(7, NTP, NTP + half_second), 7).unwrap();
        assert_eq!(r.transmit_unix_us, UNIX * 1_000_000 + 500_000);
        assert_eq!(r.server_hold_us, 500_000);
    }

    #[test]
    fn era_rollover() {
        // 2036-02-07 06:28:16 UTC is NTP seconds 2^32, which wraps to 0.
        let unix_2036 = (1u64 << 32) - NTP_UNIX_OFFSET_SECS;
        assert_eq!(
            ntp_to_unix_us(0x10 << 32),
            Some((unix_2036 + 0x10) * 1_000_000)
        );
    }

    #[test]
    fn rejects_bad_replies() {
        assert_eq!(parse_reply(&[0; 10], 1), Err(NtpError::TooShort(10)));
        assert_eq!(
            parse_reply(&reply(1, NTP, NTP), 2),
            Err(NtpError::WrongOriginate)
        );

        let mut p = reply(1, NTP, NTP);
        p[1] = 0;
        assert_eq!(parse_reply(&p, 1), Err(NtpError::KissOfDeath));

        let mut p = reply(1, NTP, NTP);
        p[0] |= 0b11 << 6;
        assert_eq!(parse_reply(&p, 1), Err(NtpError::Unsynchronised));

        let mut p = reply(1, NTP, NTP);
        p[0] = 0x23;
        assert_eq!(parse_reply(&p, 1), Err(NtpError::NotServerMode(3)));

        assert_eq!(parse_reply(&reply(1, 0, 0), 1), Err(NtpError::BadTimestamp));
        // 1999 is before any plausible server time
        let y1999 = (946_684_800 + NTP_UNIX_OFFSET_SECS) << 32;
        assert_eq!(
            parse_reply(&reply(1, y1999, y1999), 1),
            Err(NtpError::BadTimestamp)
        );
    }
}
