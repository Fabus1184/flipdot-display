//! Network jobs on top of embassy-net: one NTP query, one HTTP GET, and a
//! tiny status page on port 80. Every exchange has a deadline, so a dead
//! link makes a job fail instead of hanging.

use alloc::vec::Vec;
use core::net::Ipv4Addr;

use embassy_net::dns::DnsQueryType;
use embassy_net::tcp::TcpSocket;
use embassy_net::udp::{PacketMetadata, UdpSocket};
use embassy_net::{IpAddress, Stack};
use embassy_time::{Duration, with_timeout};

/// How long one DNS lookup, NTP query or HTTP request may take in total.
const DEADLINE: Duration = Duration::from_secs(15);
/// Largest HTTP response we accept.
const MAX_RESPONSE: usize = 4096;
/// Port of the status page.
const STATUS_PORT: u16 = 80;

#[derive(Debug)]
pub enum NetError {
    Dns,
    Timeout,
    Connect,
    Io,
    BadReply,
    ResponseTooLarge,
}

impl core::fmt::Display for NetError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.write_str(match self {
            NetError::Dns => "DNS lookup failed",
            NetError::Timeout => "timed out",
            NetError::Connect => "connection refused or reset",
            NetError::Io => "socket error",
            NetError::BadReply => "unusable reply",
            NetError::ResponseTooLarge => "response too large",
        })
    }
}

async fn resolve(stack: Stack<'_>, host: &str) -> Result<IpAddress, NetError> {
    let addrs = stack
        .dns_query(host, DnsQueryType::A)
        .await
        .map_err(|_| NetError::Dns)?;
    addrs.first().copied().ok_or(NetError::Dns)
}

/// An NTP sample: it was `unix_us` at local time `local_us`.
pub struct TimeSample {
    pub local_us: u64,
    pub unix_us: u64,
    pub round_trip_us: u64,
}

/// Asks `server` for the time.
pub async fn ntp_time(stack: Stack<'_>, server: &str) -> Result<TimeSample, NetError> {
    with_timeout(DEADLINE, ntp_query(stack, server))
        .await
        .map_err(|_| NetError::Timeout)?
}

async fn ntp_query(stack: Stack<'_>, server: &str) -> Result<TimeSample, NetError> {
    use flipdot_core::ntp;

    let ip = resolve(stack, server).await?;
    let mut rx_meta = [PacketMetadata::EMPTY; 2];
    let mut tx_meta = [PacketMetadata::EMPTY; 2];
    let mut rx_buf = [0u8; 128];
    let mut tx_buf = [0u8; 128];
    let mut socket = UdpSocket::new(stack, &mut rx_meta, &mut rx_buf, &mut tx_meta, &mut tx_buf);
    socket.bind(0).map_err(|_| NetError::Io)?;

    let sent_at = crate::state::local_us();
    let nonce = sent_at | 1;
    socket
        .send_to(&ntp::request(nonce), (ip, 123))
        .await
        .map_err(|_| NetError::Io)?;
    loop {
        let mut packet = [0u8; 128];
        let (len, _) = socket
            .recv_from(&mut packet)
            .await
            .map_err(|_| NetError::Io)?;
        let received_at = crate::state::local_us();
        match ntp::parse_reply(&packet[..len], nonce) {
            Ok(reply) => {
                // Half the network round trip has passed since the server
                // stamped the reply.
                let round_trip_us = (received_at - sent_at).saturating_sub(reply.server_hold_us);
                return Ok(TimeSample {
                    local_us: received_at,
                    unix_us: reply.transmit_unix_us + round_trip_us / 2,
                    round_trip_us,
                });
            }
            // A late reply to an earlier query; keep waiting.
            Err(ntp::NtpError::WrongOriginate) => {}
            Err(e) => {
                log::warn!("NTP reply rejected: {e:?}");
                return Err(NetError::BadReply);
            }
        }
    }
}

/// Sends `request` to `host:port` over TCP and returns everything the server
/// sends until it closes the connection.
pub async fn tcp_request(
    stack: Stack<'_>,
    host: &str,
    port: u16,
    request: &[u8],
) -> Result<Vec<u8>, NetError> {
    with_timeout(DEADLINE, tcp_exchange(stack, host, port, request))
        .await
        .map_err(|_| NetError::Timeout)?
}

async fn tcp_exchange(
    stack: Stack<'_>,
    host: &str,
    port: u16,
    request: &[u8],
) -> Result<Vec<u8>, NetError> {
    let ip = resolve(stack, host).await?;
    let mut rx = [0u8; 1024];
    let mut tx = [0u8; 512];
    let mut socket = TcpSocket::new(stack, &mut rx, &mut tx);
    socket
        .connect((ip, port))
        .await
        .map_err(|_| NetError::Connect)?;
    let result = async {
        let mut sent = 0;
        while sent < request.len() {
            sent += socket
                .write(&request[sent..])
                .await
                .map_err(|_| NetError::Io)?;
        }
        let mut response = Vec::new();
        let mut chunk = [0u8; 512];
        loop {
            // 0 bytes: the server closed after the response (Connection: close).
            match socket.read(&mut chunk).await.map_err(|_| NetError::Io)? {
                0 => return Ok(response),
                n => response.extend_from_slice(&chunk[..n]),
            }
            if response.len() > MAX_RESPONSE {
                return Err(NetError::ResponseTooLarge);
            }
        }
    }
    .await;
    // Reset instead of a FIN handshake so the socket is gone right away.
    socket.abort();
    let _ = with_timeout(Duration::from_millis(200), socket.flush()).await;
    result
}

/// Produces the body of the status page.
pub type StatusFn = fn(&mut Vec<u8>);

/// Serves the status page forever, one client at a time.
pub async fn status_server(stack: Stack<'static>, status: StatusFn) -> ! {
    let mut rx = [0u8; 512];
    let mut tx = [0u8; 6144];
    loop {
        let mut socket = TcpSocket::new(stack, &mut rx, &mut tx);
        socket.set_timeout(Some(Duration::from_secs(5)));
        if socket.accept(STATUS_PORT).await.is_err() {
            continue;
        }
        // Answer once something arrived; the request itself is ignored.
        let mut discard = [0u8; 512];
        if matches!(socket.read(&mut discard).await, Ok(n) if n > 0) {
            let mut body = Vec::with_capacity(5000);
            status(&mut body);
            body.truncate(6144 - 128);
            let mut head = flipdot_core::clock::StrBuf::<128>::new();
            let _ = core::fmt::Write::write_fmt(
                &mut head,
                format_args!(
                    "HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                ),
            );
            for part in [head.as_str().as_bytes(), &body[..]] {
                let mut sent = 0;
                while sent < part.len() {
                    match socket.write(&part[sent..]).await {
                        Ok(n) if n > 0 => sent += n,
                        _ => break,
                    }
                }
            }
            let _ = socket.flush().await;
        }
        socket.close();
        let _ = with_timeout(Duration::from_secs(1), socket.flush()).await;
        socket.abort();
    }
}

/// The static IPv4 configuration from `config.toml`.
pub fn static_config(
    ip: Ipv4Addr,
    prefix: u8,
    gateway: Ipv4Addr,
    dns: Ipv4Addr,
) -> embassy_net::Config {
    let mut config = embassy_net::StaticConfigV4 {
        address: embassy_net::Ipv4Cidr::new(ip, prefix),
        gateway: Some(gateway),
        dns_servers: Default::default(),
    };
    let _ = config.dns_servers.push(dns);
    embassy_net::Config::ipv4_static(config)
}
