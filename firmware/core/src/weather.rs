//! Parsing of the wttr.in one-line temperature reply.
//!
//! The firmware requests `GET /<location>?format=%t&m`, which answers with a
//! body like `+12°C`, `-3°C` or `0°C`.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WeatherError {
    NotUtf8,
    /// No `\r\n\r\n` between headers and body (response truncated?).
    NoBody,
    /// Status line missing or not `200`.
    HttpStatus(u16),
    /// The body is not a temperature, e.g. "Unknown location".
    BadBody,
}

/// Parses a complete HTTP/1.x response and returns the temperature in whole
/// degrees Celsius.
pub fn parse_response(response: &[u8]) -> Result<i32, WeatherError> {
    let text = core::str::from_utf8(response).map_err(|_| WeatherError::NotUtf8)?;
    let (head, body) = text.split_once("\r\n\r\n").ok_or(WeatherError::NoBody)?;

    let status = head
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|code| code.parse::<u16>().ok())
        .unwrap_or(0);
    if status != 200 {
        return Err(WeatherError::HttpStatus(status));
    }

    parse_temperature(body)
}

/// Parses a body such as `+12°C`, `-3.5°C` or ` 7 °C\n` (rounded to whole
/// degrees).
pub fn parse_temperature(body: &str) -> Result<i32, WeatherError> {
    let number = body
        .trim()
        .strip_suffix("°C")
        .ok_or(WeatherError::BadBody)?
        .trim();
    let number = number.strip_prefix('+').unwrap_or(number);

    if let Ok(whole) = number.parse::<i32>() {
        return Ok(whole);
    }
    // Fractions are not expected from wttr.in, but round them instead of failing.
    let (int, frac) = number.split_once('.').ok_or(WeatherError::BadBody)?;
    let negative = int.starts_with('-');
    let int: i32 = int.parse().map_err(|_| WeatherError::BadBody)?;
    let first = frac
        .chars()
        .next()
        .and_then(|c| c.to_digit(10))
        .ok_or(WeatherError::BadBody)?;
    let round_away = first >= 5;
    Ok(match (round_away, negative) {
        (false, _) => int,
        (true, false) => int + 1,
        (true, true) => int - 1,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn http(status: &str, body: &str) -> std::string::String {
        std::format!(
            "HTTP/1.1 {status}\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n{body}"
        )
    }

    #[test]
    fn parses_temperatures() {
        assert_eq!(parse_temperature("+12°C"), Ok(12));
        assert_eq!(parse_temperature("-3°C\n"), Ok(-3));
        assert_eq!(parse_temperature("0°C"), Ok(0));
        assert_eq!(parse_temperature("-0°C"), Ok(0));
        assert_eq!(parse_temperature("+-0°C"), Ok(0));
        assert_eq!(parse_temperature(" +7 °C "), Ok(7));
        assert_eq!(parse_temperature("+2.5°C"), Ok(3));
        assert_eq!(parse_temperature("-2.5°C"), Ok(-3));
        assert_eq!(parse_temperature("-2.4°C"), Ok(-2));
    }

    #[test]
    fn rejects_garbage() {
        assert_eq!(
            parse_temperature("Unknown location"),
            Err(WeatherError::BadBody)
        );
        assert_eq!(parse_temperature("°C"), Err(WeatherError::BadBody));
        assert_eq!(parse_temperature("+12°F"), Err(WeatherError::BadBody));
    }

    #[test]
    fn parses_http() {
        assert_eq!(parse_response(http("200 OK", "+4°C").as_bytes()), Ok(4));
        assert_eq!(
            parse_response(http("503 Service Unavailable", "busy").as_bytes()),
            Err(WeatherError::HttpStatus(503))
        );
        assert_eq!(
            parse_response(b"HTTP/1.1 200 OK\r\nContent-Type: te"),
            Err(WeatherError::NoBody)
        );
    }
}
