//! Flipdot clock firmware for the ESP32.
//!
//! * Core 0 drives the panel: it renders the clock screen once per second and
//!   flips the dots that changed.
//! * Core 1 runs the network on an embassy executor: one task each for WiFi,
//!   the IP stack, NTP, the weather from wttr.in, a status page on port 80,
//!   and the watchdog.

#![no_std]
#![no_main]

extern crate alloc;

mod config;
mod logger;
mod net;
mod panel;
mod persist;
mod state;

use core::fmt::Write as _;

use embassy_executor::Spawner;
use embassy_futures::select::{Either, select};
use embassy_net::{Runner, Stack, StackResources};
use embassy_sync::blocking_mutex::raw::CriticalSectionRawMutex;
use embassy_sync::signal::Signal;
use embassy_time::Timer;
use esp_hal::clock::CpuClock;
use esp_hal::gpio::Pin as _;
use esp_hal::main;
use esp_hal::peripherals;
use esp_hal::time::{Duration, Instant};
use esp_hal::timer::timg::{MwdtStage, MwdtStageAction, TimerGroup, Wdt};
use esp_radio::wifi::{WifiController, WifiDevice, WifiEvent};
use flipdot_core::clock::{self, ClockState};
use static_cell::StaticCell;

esp_bootloader_esp_idf::esp_app_desc!();

/// How often to sync the clock. Also the internet check for the watchdog, so
/// well below [`INTERNET_MAX_OUTAGE`]. NTP servers accept polls every 64 s.
const NTP_INTERVAL: Duration = Duration::from_secs(2 * 60);
const NTP_RETRY: Duration = Duration::from_secs(30);
/// wttr.in asks clients not to poll too often.
const WEATHER_INTERVAL: Duration = Duration::from_secs(10 * 60);
const WEATHER_RETRY: Duration = Duration::from_secs(60);
/// Reset if no NTP or weather request has succeeded for this long (WiFi or
/// the network stack stuck even after reconnecting), counted from boot until
/// the first success.
const INTERNET_MAX_OUTAGE: Duration = Duration::from_secs(10 * 60);
/// Reset if the display loop has not run for this long.
const DISPLAY_MAX_STALL: Duration = Duration::from_secs(60);
/// The watchdog fires if it is not fed for this long.
/// Short, so a stalled network core also resets the board quickly.
const WATCHDOG_TIMEOUT: Duration = Duration::from_secs(30);

#[panic_handler]
fn panic(info: &core::panic::PanicInfo) -> ! {
    // First make sure no coil stays powered, whatever was running.
    panel::emergency_off();
    esp_println::println!("PANIC: {info}");
    let bt = esp_backtrace::Backtrace::capture();
    for frame in bt.frames() {
        esp_println::println!("  0x{:08x}", frame.program_counter());
    }
    // Give the serial console a moment, then start over.
    esp_hal::delay::Delay::new().delay_millis(2000);
    esp_hal::system::software_reset()
}

static mut APP_CORE_STACK: esp_hal::system::Stack<16384> = esp_hal::system::Stack::new();

#[esp_hal::ram(reclaimed)]
static mut HEAP1: core::mem::MaybeUninit<[u8; 64_000]> = core::mem::MaybeUninit::uninit();
static mut HEAP2: core::mem::MaybeUninit<[u8; 64_000]> = core::mem::MaybeUninit::uninit();

#[main]
fn main() -> ! {
    // SAFETY: runs once before anything allocates; the regions are not used otherwise.
    unsafe {
        for heap in [
            core::ptr::addr_of_mut!(HEAP1),
            core::ptr::addr_of_mut!(HEAP2),
        ] {
            esp_alloc::HEAP.add_region(esp_alloc::HeapRegion::new(
                heap.cast::<u8>(),
                core::mem::size_of_val(&*heap),
                esp_alloc::MemoryCapability::Internal.into(),
            ));
        }
    }

    let peripherals = esp_hal::init(esp_hal::Config::default().with_cpu_clock(CpuClock::_80MHz));

    logger::init(log::LevelFilter::Info);
    log::info!(
        "flipdot {} starting, wifi {:?}, tz {}, weather {}",
        env!("CARGO_PKG_VERSION"),
        config::WIFI_SSID,
        config::TIMEZONE,
        config::WEATHER_LOCATION
    );

    let timg0 = TimerGroup::new(peripherals.TIMG0);
    let software_interrupt =
        esp_hal::interrupt::software::SoftwareInterruptControl::new(peripherals.SW_INTERRUPT);
    esp_rtos::start(timg0.timer0);

    let watchdog = timg0.wdt;
    let wifi = peripherals.WIFI;
    esp_rtos::start_second_core_with_stack_guard_offset(
        peripherals.CPU_CTRL,
        software_interrupt.software_interrupt0,
        software_interrupt.software_interrupt1,
        // SAFETY: the stack is handed to the second core exactly once.
        unsafe { &mut *core::ptr::addr_of_mut!(APP_CORE_STACK) },
        None,
        move || network_core(wifi, watchdog),
    );

    // Pin assignment, see hardware/README.md.
    let mut panel = panel::Panel::new(panel::EspPins::new(panel::Pins {
        sr_shift_clock: peripherals.GPIO15.degrade(),
        sr_latch_clock: peripherals.GPIO2.degrade(),
        sr_data: peripherals.GPIO4.degrade(),
        sr_output_enable_n: peripherals.GPIO16.degrade(),
        fp_enable: peripherals.GPIO13.degrade(),
        fp_data: peripherals.GPIO12.degrade(),
        fp_address: [
            peripherals.GPIO14.degrade(), // FP_A0
            peripherals.GPIO27.degrade(), // FP_A1
            peripherals.GPIO26.degrade(), // FP_A2
            peripherals.GPIO25.degrade(), // FP_B0
            peripherals.GPIO33.degrade(), // FP_B1
        ],
    }));

    display_core(&mut panel, esp_hal::rtc_cntl::Rtc::new(peripherals.LPWR))
}

fn display_core(
    panel: &mut panel::Panel<panel::EspPins>,
    rtc: esp_hal::rtc_cntl::Rtc<'static>,
) -> ! {
    // First thing, before the self-test: take over the time from before a
    // reset, if there is one.
    let (mut persist, restored) = persist::Persist::restore(rtc);
    if let Some(r) = restored {
        state::resume_time(r.unix_us, r.rate_ppb);
        log::info!(
            "time kept across reset (gap {} ms), waiting for NTP",
            r.gap_us / 1000
        );
    }

    // The dots keep whatever they showed before power-off. Flip all of them
    // through both states (also a visible self-test).
    panel.force_fill(false);
    panel.force_fill(true);
    panel.force_fill(false);

    let tz = config::TZ;
    loop {
        let state = ClockState {
            time: state::unix_now_us().map(|us| clock::local_hour_minute(us, tz)),
            time_synced: state::time_synced(),
            temperature: state::temperature(),
        };
        panel.show(&clock::render(&state));
        state::display_alive();
        if let Some((unix_us, rate_ppb)) = state::clock_snapshot() {
            persist.save(unix_us, rate_ppb);
        }

        // A short period so the minute changes promptly. Sleeps the thread,
        // so the core idles instead of spinning.
        esp_rtos::CurrentThreadHandle::get().delay(Duration::from_millis(250));
    }
}

fn network_core(wifi: peripherals::WIFI<'static>, watchdog: Wdt<peripherals::TIMG0<'static>>) {
    static EXECUTOR: StaticCell<esp_rtos::embassy::Executor> = StaticCell::new();
    EXECUTOR
        .init(esp_rtos::embassy::Executor::new())
        .run(move |spawner| {
            spawner.must_spawn(watchdog_task(watchdog));
            spawner.must_spawn(network_main(spawner, wifi));
        })
}

/// Time of the last successful NTP or weather request, read by the watchdog.
static LAST_INTERNET_OK: spin::Mutex<Option<Instant>> = spin::Mutex::new(None);
/// Signalled by the jobs when the link looks dead although WiFi says it is
/// connected; the WiFi task then reconnects.
static RECONNECT: Signal<CriticalSectionRawMutex, ()> = Signal::new();

#[embassy_executor::task]
async fn watchdog_task(mut watchdog: Wdt<peripherals::TIMG0<'static>>) {
    watchdog.set_timeout(MwdtStage::Stage0, WATCHDOG_TIMEOUT);
    watchdog.set_stage_action(MwdtStage::Stage0, MwdtStageAction::ResetSystem);
    for stage in [MwdtStage::Stage1, MwdtStage::Stage2, MwdtStage::Stage3] {
        watchdog.set_stage_action(stage, MwdtStageAction::Off);
    }
    watchdog.enable();

    let boot = Instant::now();
    let mut heartbeat = (state::display_heartbeat(), boot);
    loop {
        // Feed the watchdog only while both cores do their job.
        let beat = state::display_heartbeat();
        if beat != heartbeat.0 {
            heartbeat = (beat, Instant::now());
        }
        let display_ok = heartbeat.1.elapsed() < DISPLAY_MAX_STALL;
        let internet_ok = LAST_INTERNET_OK.lock().unwrap_or(boot).elapsed() < INTERNET_MAX_OUTAGE;
        if display_ok && internet_ok {
            watchdog.feed();
        }
        Timer::after(embassy_time::Duration::from_secs(1)).await;
    }
}

#[embassy_executor::task]
async fn network_main(spawner: Spawner, wifi: peripherals::WIFI<'static>) {
    static RADIO: StaticCell<esp_radio::Controller<'static>> = StaticCell::new();
    static RESOURCES: StaticCell<StackResources<4>> = StaticCell::new();

    let radio = RADIO.init(esp_radio::init().expect("radio init"));
    let wifi_config = esp_radio::wifi::Config::default().with_country_code(
        esp_radio::wifi::CountryInfo::from(*b"DE")
            .with_operating_class(esp_radio::wifi::OperatingClass::AllEnvironments),
    );
    let (controller, interfaces) =
        esp_radio::wifi::new(radio, wifi, wifi_config).expect("WiFi init");

    let config = net::static_config(
        config::ip(),
        config::prefix_len(),
        config::gateway(),
        config::dns(),
    );
    let seed = state::local_us() ^ 0x9e37_79b9_7f4a_7c15;
    let (stack, runner) = embassy_net::new(
        interfaces.sta,
        config,
        RESOURCES.init(StackResources::new()),
        seed,
    );
    log::info!("IP {}/{}", config::ip(), config::prefix_len());

    spawner.must_spawn(wifi_task(controller));
    spawner.must_spawn(stack_task(runner));
    spawner.must_spawn(ntp_task(stack));
    spawner.must_spawn(weather_task(stack));
    spawner.must_spawn(status_task(stack));
}

/// Keeps WiFi connected: connects, waits for a disconnect (or a request from
/// a job that sees a dead link), and starts over.
#[embassy_executor::task]
async fn wifi_task(mut controller: WifiController<'static>) {
    let client = esp_radio::wifi::ModeConfig::Client(
        esp_radio::wifi::ClientConfig::default()
            .with_ssid(config::WIFI_SSID.into())
            .with_auth_method(esp_radio::wifi::AuthMethod::Wpa2Personal)
            .with_password(config::WIFI_PASSWORD.into()),
    );
    controller.set_config(&client).expect("WiFi config");
    loop {
        if !matches!(controller.is_started(), Ok(true))
            && let Err(e) = controller.start_async().await
        {
            log::error!("WiFi start: {e:?}");
            Timer::after(embassy_time::Duration::from_secs(5)).await;
            continue;
        }
        // The radio sleeps between the access point's beacons instead of
        // listening all the time. Costs a little latency on incoming requests.
        if let Err(e) = controller.set_power_saving(esp_radio::wifi::PowerSaveMode::Minimum) {
            log::warn!("WiFi power saving: {e:?}");
        }
        log::info!("WiFi connecting to {:?}", config::WIFI_SSID);
        match controller.connect_async().await {
            Ok(()) => log::info!("WiFi connected"),
            Err(e) => {
                log::error!("WiFi connect: {e:?}");
                Timer::after(embassy_time::Duration::from_secs(5)).await;
                continue;
            }
        }
        RECONNECT.reset();
        match select(
            controller.wait_for_event(WifiEvent::StaDisconnected),
            RECONNECT.wait(),
        )
        .await
        {
            Either::First(()) => log::warn!("WiFi disconnected"),
            Either::Second(()) => {
                log::warn!("network unreachable, reconnecting WiFi");
                let _ = controller.disconnect_async().await;
            }
        }
        Timer::after(embassy_time::Duration::from_secs(2)).await;
    }
}

#[embassy_executor::task]
async fn stack_task(mut runner: Runner<'static, WifiDevice<'static>>) {
    runner.run().await
}

/// Waits until the link is up, then runs `job` every `interval` (every
/// `retry` after a failure). With `reconnect`, three failures in a row ask
/// for a WiFi reconnect.
async fn periodic<F, Fut>(
    stack: Stack<'static>,
    interval: Duration,
    retry: Duration,
    reconnect: bool,
    mut job: F,
) -> !
where
    F: FnMut(Stack<'static>) -> Fut,
    Fut: core::future::Future<Output = bool>,
{
    let mut failures = 0u32;
    loop {
        stack.wait_link_up().await;
        let ok = job(stack).await;
        failures = if ok { 0 } else { failures + 1 };
        if reconnect && failures >= 3 {
            RECONNECT.signal(());
            failures = 0;
        }
        let wait = if ok { interval } else { retry };
        Timer::after(embassy_time::Duration::from_micros(wait.as_micros())).await;
    }
}

#[embassy_executor::task]
async fn ntp_task(stack: Stack<'static>) {
    periodic(stack, NTP_INTERVAL, NTP_RETRY, true, |stack| async move {
        match net::ntp_time(stack, config::NTP_SERVER).await {
            Ok(sample) => {
                *LAST_INTERNET_OK.lock() = Some(Instant::now());
                match state::sync_time(sample.local_us, sample.unix_us) {
                    None => log::info!("NTP: clock set (round trip {} ms)", sample.round_trip_us / 1000),
                    Some(s) => log::info!(
                        "NTP: clock was off by {} ms, {}; local timer rate {:+} ppm (round trip {} ms)",
                        s.error_us / 1000,
                        if s.stepped { "stepped" } else { "slewing" },
                        -s.rate_ppb / 1000,
                        sample.round_trip_us / 1000
                    ),
                }
                true
            }
            Err(e) => {
                log::error!("NTP: {e}");
                false
            }
        }
    })
    .await
}

#[embassy_executor::task]
async fn weather_task(stack: Stack<'static>) {
    periodic(
        stack,
        WEATHER_INTERVAL,
        WEATHER_RETRY,
        false,
        |stack| async move {
            match fetch_temperature(stack).await {
                Ok(celsius) => {
                    *LAST_INTERNET_OK.lock() = Some(Instant::now());
                    log::info!("temperature {celsius} °C");
                    state::set_temperature(celsius);
                    true
                }
                Err(e) => {
                    log::error!("weather: {e}");
                    false
                }
            }
        },
    )
    .await
}

#[embassy_executor::task]
async fn status_task(stack: Stack<'static>) {
    net::status_server(stack, status_page).await
}

async fn fetch_temperature(stack: Stack<'static>) -> Result<i32, alloc::string::String> {
    use alloc::string::ToString as _;

    let request = alloc::format!(
        "GET /{}?format=%t&m HTTP/1.1\r\nHost: wttr.in\r\nUser-Agent: curl/8 (flipdot)\r\nAccept: text/plain\r\nConnection: close\r\n\r\n",
        config::WEATHER_LOCATION
    );
    let response = net::tcp_request(stack, "wttr.in", 80, request.as_bytes())
        .await
        .map_err(|e| e.to_string())?;
    flipdot_core::weather::parse_response(&response).map_err(|e| alloc::format!("{e:?}"))
}

/// Body of the status page on port 80.
fn status_page(out: &mut alloc::vec::Vec<u8>) {
    let mut text = alloc::string::String::new();
    let uptime = Instant::now().duration_since_epoch().as_secs();
    let _ = writeln!(text, "flipdot {}", env!("CARGO_PKG_VERSION"));
    let _ = writeln!(
        text,
        "uptime {}d {:02}:{:02}:{:02}",
        uptime / 86400,
        uptime / 3600 % 24,
        uptime / 60 % 60,
        uptime % 60
    );
    match state::unix_now_us() {
        Some(us) => {
            let (h, m) = clock::local_hour_minute(us, config::TZ);
            let _ = writeln!(
                text,
                "time {h:02}:{m:02} {} (unix {}){}",
                config::TIMEZONE,
                us / 1_000_000,
                if state::time_synced() {
                    ""
                } else {
                    ", kept across reset, not NTP-synced yet"
                }
            );
        }
        None => {
            let _ = writeln!(text, "time not synced yet");
        }
    }
    match state::temperature() {
        Some(t) => {
            let _ = writeln!(text, "temperature {t} °C ({})", config::WEATHER_LOCATION);
        }
        None => {
            let _ = writeln!(text, "temperature unknown");
        }
    }
    let _ = writeln!(
        text,
        "heap used {} free {}",
        esp_alloc::HEAP.used(),
        esp_alloc::HEAP.free()
    );
    let _ = writeln!(text, "\nlog:");
    out.extend_from_slice(text.as_bytes());
    logger::dump(out);
}
