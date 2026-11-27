//! Real wttr.in replies, captured with the request the firmware sends.

use flipdot_core::weather::parse_response;

#[test]
fn minus_zero() {
    // wttr.in writes "+-0°C" for temperatures just below zero; the old
    // firmware failed to parse it.
    let reply = include_bytes!("fixtures/wttr-minus-zero.http");
    assert_eq!(parse_response(reply), Ok(0));
}
