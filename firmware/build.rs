//! Reads `config.toml` (or `$FLIPDOT_CONFIG`), validates it and passes the
//! values to the firmware as `FLIPDOT_*` compile-time environment variables.

use std::{net::Ipv4Addr, path::PathBuf, str::FromStr};

fn main() {
    linker_be_nice();
    // make sure linkall.x is the last linker script (otherwise might cause problems with flip-link)
    println!("cargo:rustc-link-arg=-Tlinkall.x");

    load_config();
}

fn load_config() {
    println!("cargo:rerun-if-env-changed=FLIPDOT_CONFIG");
    let manifest_dir = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let path = match std::env::var("FLIPDOT_CONFIG") {
        Ok(p) => manifest_dir.join(p),
        Err(_) => manifest_dir.join("config.toml"),
    };
    println!("cargo:rerun-if-changed={}", path.display());

    let text = std::fs::read_to_string(&path).unwrap_or_else(|e| {
        fail(&format!(
            "cannot read {}: {e}\n\n    Copy config.example.toml to config.toml and fill it in,\n    or point FLIPDOT_CONFIG at a config file.",
            path.display()
        ))
    });
    let config: toml::Table = text
        .parse()
        .unwrap_or_else(|e| fail(&format!("{} is not valid TOML: {e}", path.display())));

    let get = |section: &str, key: &str| -> String {
        config
            .get(section)
            .and_then(|s| s.get(key))
            .and_then(|v| v.as_str())
            .unwrap_or_else(|| {
                fail(&format!(
                    "{}: missing string [{section}] {key}",
                    path.display()
                ))
            })
            .to_owned()
    };

    let ssid = get("wifi", "ssid");
    let password = get("wifi", "password");
    if ssid.is_empty() || ssid.len() > 32 {
        fail("[wifi] ssid must be 1..=32 bytes");
    }
    if !(8..=64).contains(&password.len()) {
        fail("[wifi] password must be 8..=64 bytes for WPA2");
    }

    let address = get("network", "address");
    let (ip, prefix) = address
        .split_once('/')
        .unwrap_or_else(|| fail("[network] address must look like 192.168.1.50/24"));
    let ip = parse_ip("[network] address", ip);
    let prefix: u8 = prefix
        .parse()
        .ok()
        .filter(|p| (1..=32).contains(p))
        .unwrap_or_else(|| fail("[network] address prefix must be 1..=32"));
    let gateway = parse_ip("[network] gateway", &get("network", "gateway"));
    let dns = parse_ip("[network] dns", &get("network", "dns"));

    let timezone = get("clock", "timezone");
    let tz = chrono_tz::Tz::from_str(&timezone).unwrap_or_else(|_| {
        fail(&format!(
            "[clock] timezone {timezone:?} is not an IANA time zone name"
        ))
    });
    // Name the zone as a constant so only its rules get linked; parsing the
    // name at runtime would pull in the whole tz database (~850 KB).
    let out = PathBuf::from(std::env::var("OUT_DIR").unwrap()).join("timezone.rs");
    std::fs::write(
        &out,
        format!(
            "/// {name}\npub const TZ: chrono_tz::Tz = chrono_tz::Tz::{variant};\n",
            name = tz.name(),
            variant = tz_variant(tz.name())
        ),
    )
    .unwrap();
    let ntp_server = get("clock", "ntp_server");
    let location = get("weather", "location");
    if location.is_empty() || location.contains(|c: char| c.is_whitespace() || c == '?' || c == '#')
    {
        fail(
            "[weather] location must not be empty or contain spaces, '?' or '#' (use '+' for spaces)",
        );
    }

    let env = |name: &str, value: &str| println!("cargo:rustc-env=FLIPDOT_{name}={value}");
    env("WIFI_SSID", &ssid);
    env("WIFI_PASSWORD", &password);
    env("IP", &ip.to_string());
    env("PREFIX", &prefix.to_string());
    env("GATEWAY", &gateway.to_string());
    env("DNS", &dns.to_string());
    env("TIMEZONE", &timezone);
    env("NTP_SERVER", &ntp_server);
    env("WEATHER_LOCATION", &location);
}

/// The `chrono_tz::Tz` variant for a zone name, e.g. `Europe/Berlin` →
/// `Europe__Berlin`, `Etc/GMT-1` → `Etc__GMTMinus1`, `America/Port-au-Prince`
/// → `America__PortauPrince`.
fn tz_variant(name: &str) -> String {
    let mut out = String::new();
    let mut chars = name.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '/' => out.push_str("__"),
            '+' => out.push_str("Plus"),
            '-' if chars.peek().is_some_and(|c| c.is_ascii_digit()) => out.push_str("Minus"),
            '-' => {}
            c => out.push(c),
        }
    }
    out
}

fn parse_ip(what: &str, s: &str) -> Ipv4Addr {
    s.parse()
        .unwrap_or_else(|_| fail(&format!("{what}: {s:?} is not an IPv4 address")))
}

fn fail(msg: &str) -> ! {
    eprintln!("\nflipdot config error: {msg}\n");
    std::process::exit(1);
}

fn linker_be_nice() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() > 1 {
        let kind = &args[1];
        let what = &args[2];

        match kind.as_str() {
            "undefined-symbol" => match what.as_str() {
                "_stack_start" => {
                    eprintln!();
                    eprintln!("💡 Is the linker script `linkall.x` missing?");
                    eprintln!();
                }
                "esp_rtos_initialized" | "esp_rtos_yield_task" | "esp_rtos_task_create" => {
                    eprintln!();
                    eprintln!(
                        "💡 `esp-radio` has no scheduler enabled. Make sure you have initialized `esp-rtos` or provided an external scheduler."
                    );
                    eprintln!();
                }
                _ => (),
            },
            // we don't have anything helpful for "missing-lib" yet
            _ => {
                std::process::exit(1);
            }
        }

        std::process::exit(0);
    }

    println!(
        "cargo:rustc-link-arg=-Wl,--error-handling-script={}",
        std::env::current_exe().unwrap().display()
    );
}
