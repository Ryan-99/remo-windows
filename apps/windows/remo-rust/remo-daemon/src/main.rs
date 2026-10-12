#![windows_subsystem = "windows"]

mod actions;
mod atvv;
mod ble_hid;
mod bridge_status;
mod f5_guard;
mod frida_launch;
mod hid_usage;
mod ipc;
mod playback;
mod raw_input;
mod user_config;
mod voice_atvv;
mod voice_hub;
mod winuhid;

use actions::{create_executor, ActionExecutor};
use remo_core::{dispatch, Action, Button, Edge, GestureEngine, InputEvent, Mapping, Trigger};
use serde_json::Value;
use std::collections::HashMap;
use std::env;
use std::fs::{create_dir_all, File, OpenOptions};
use std::io::{self, BufRead, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const FRIDA_READ_TIMEOUT: Duration = Duration::from_secs(1);
const FRIDA_STALE_TIMEOUT: Duration = Duration::from_secs(15);

fn button(value: &str) -> Option<Button> {
    Some(match value {
        "mic" => Button::Mic,
        "power" => Button::Power,
        "up" => Button::Up,
        "down" => Button::Down,
        "left" => Button::Left,
        "right" => Button::Right,
        "ok" => Button::Ok,
        "back" => Button::Back,
        "volume_up" => Button::VolumeUp,
        "volume_down" => Button::VolumeDown,
        "home" => Button::Home,
        "menu" => Button::Menu,
        "tv" => Button::Tv,
        _ => return None,
    })
}

enum Command {
    Input(InputEvent),
    FridaInput {
        input: InputEvent,
        report_seq: Option<u64>,
    },
    Diagnostic(Value),
    Quit,
}

fn button_name(button: Button) -> &'static str {
    match button {
        Button::Mic => "mic",
        Button::Power => "power",
        Button::Up => "up",
        Button::Down => "down",
        Button::Left => "left",
        Button::Right => "right",
        Button::Ok => "ok",
        Button::Back => "back",
        Button::VolumeUp => "volume_up",
        Button::VolumeDown => "volume_down",
        Button::Home => "home",
        Button::Menu => "menu",
        Button::Tv => "tv",
    }
}

fn trigger_name(trigger: Trigger) -> &'static str {
    match trigger {
        Trigger::Single => "single",
        Trigger::Double => "double",
        Trigger::Long => "long",
    }
}

fn unix_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}

struct Journal {
    file: File,
    events: ipc::SharedEvents,
    sequence: u64,
    session: String,
    execute: bool,
    source: String,
}

impl Journal {
    fn open(execute: bool, source: &str) -> io::Result<Self> {
        let dir = env::var_os("REMO_LOG_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                env::var_os("LOCALAPPDATA")
                    .map(PathBuf::from)
                    .unwrap_or_else(env::temp_dir)
                    .join("Remo/rust/logs")
            });
        create_dir_all(&dir)?;
        let session = format!("{}-{}", std::process::id(), unix_ms());
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(dir.join(format!("events-{session}.jsonl")))?;
        Ok(Self {
            file,
            events: Arc::new(Mutex::new(ipc::EventStore::default())),
            sequence: 0,
            session,
            execute,
            source: source.to_string(),
        })
    }

    fn emit(&mut self, mut value: serde_json::Value) {
        self.sequence += 1;
        value["event_id"] = serde_json::json!(self.sequence);
        value["session_id"] = serde_json::json!(self.session);
        value["source"] = serde_json::json!(&self.source);
        value["execution_mode"] = serde_json::json!(if self.execute {
            "windows"
        } else {
            "simulation"
        });
        let line = value.to_string();
        self.events.lock().unwrap().push(value);
        if let Err(error) = writeln!(self.file, "{line}").and_then(|_| self.file.flush()) {
            eprintln!("event journal write failed: {error}");
        }
        // The UI may close its stdout reader; journal/IPC must remain alive.
        let _ = writeln!(io::stdout().lock(), "{line}");
    }
}

fn spawn_reader() -> Receiver<Command> {
    let (tx, rx) = mpsc::sync_channel(256);
    thread::spawn(move || {
        let stdin = io::stdin();
        for line in stdin.lock().lines().map_while(Result::ok) {
            let fields: Vec<_> = line.split_whitespace().collect();
            if fields.first() == Some(&"quit") {
                let _ = tx.send(Command::Quit);
                break;
            }
            if fields.len() != 2 {
                eprintln!("usage: <button> <down|up>");
                continue;
            }
            let Some(key) = button(fields[0]) else {
                eprintln!("unknown button: {}", fields[0]);
                continue;
            };
            let Some(edge) = (match fields[1] {
                "down" => Some(Edge::Down),
                "up" => Some(Edge::Up),
                _ => None,
            }) else {
                eprintln!("unknown edge: {}", fields[1]);
                continue;
            };
            if tx
                .send(Command::Input(InputEvent {
                    button: key,
                    edge,
                    at: Instant::now(),
                }))
                .is_err()
            {
                break;
            }
        }
    });
    rx
}

fn spawn_frida_listener(port: u16, host_pid: u32) -> io::Result<Receiver<Command>> {
    let listener = TcpListener::bind(("127.0.0.1", port))?;
    listener.set_nonblocking(true)?;
    let (tx, rx) = mpsc::sync_channel(256);
    let generation = Arc::new(AtomicU64::new(0));
    thread::spawn(move || loop {
        match listener.accept() {
            Ok((stream, address)) => {
                let _ = stream.set_nonblocking(false);
                // Gadget heartbeats arrive every 5 seconds. A short socket
                // timeout must only wake the loop; it is not a disconnect.
                let _ = stream.set_read_timeout(Some(FRIDA_READ_TIMEOUT));
                let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));
                let tx = tx.clone();
                let generation_id = generation.fetch_add(1, Ordering::Relaxed) + 1;
                let generation = generation.clone();
                thread::spawn(move || {
                    frida_connection(
                        stream,
                        address.to_string(),
                        host_pid,
                        generation_id,
                        generation,
                        tx,
                    )
                });
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(20))
            }
            Err(error) => {
                let _ = tx.send(Command::Diagnostic(
                    serde_json::json!({"type":"frida_listener_failed","error":error.to_string()}),
                ));
                break;
            }
        }
    });
    Ok(rx)
}

fn merge_input_channels(
    primary: Receiver<Command>,
    secondary: Receiver<Command>,
) -> Receiver<Command> {
    let (tx, rx) = mpsc::sync_channel(256);
    thread::spawn(move || {
        let mut primary_open = true;
        let mut secondary_open = true;
        while primary_open || secondary_open {
            match primary.try_recv() {
                Ok(command) => {
                    if tx.send(command).is_err() {
                        return;
                    }
                }
                Err(mpsc::TryRecvError::Disconnected) => primary_open = false,
                Err(mpsc::TryRecvError::Empty) => {}
            }
            match secondary.try_recv() {
                Ok(command) => {
                    if tx.send(command).is_err() {
                        return;
                    }
                }
                Err(mpsc::TryRecvError::Disconnected) => secondary_open = false,
                Err(mpsc::TryRecvError::Empty) => {}
            }
            if primary_open || secondary_open {
                thread::sleep(Duration::from_millis(5));
            }
        }
    });
    rx
}

fn frida_connection(
    stream: TcpStream,
    peer: String,
    host_pid: u32,
    generation_id: u64,
    generation: Arc<AtomicU64>,
    tx: mpsc::SyncSender<Command>,
) {
    let mut session = remo_daemon::frida::FridaSession::new(host_pid);
    let _ = tx.send(Command::Diagnostic(
        serde_json::json!({"type":"frida_transport_connected","peer":peer,"host_pid":host_pid}),
    ));
    let mut reader = io::BufReader::new(stream.try_clone().unwrap());
    let mut last_activity = Instant::now();
    loop {
        if generation.load(Ordering::Relaxed) != generation_id {
            return;
        }
        let mut line = String::new();
        match reader.read_line(&mut line) {
            Ok(0) => break,
            Ok(_) if line.len() > 65536 => {
                let _ = tx.send(Command::Diagnostic(
                    serde_json::json!({"type":"frida_frame_rejected","reason":"frame_too_large"}),
                ));
                break;
            }
            Ok(_) => {
                last_activity = Instant::now();
                let value: Value = match serde_json::from_str(&line) {
                    Ok(value) => value,
                    Err(error) => {
                        let _ = tx.send(Command::Diagnostic(serde_json::json!({"type":"frida_frame_rejected","reason":"invalid_json","error":error.to_string()})));
                        break;
                    }
                };
                match session.process(&value, Instant::now()) {
                    Ok(events) => {
                        if generation.load(Ordering::Relaxed) != generation_id {
                            return;
                        }
                        let report_seq = session.last_report_sequence();
                        for input in events {
                            if tx.send(Command::FridaInput { input, report_seq }).is_err() {
                                return;
                            }
                        }
                    }
                    Err(error) => {
                        let _ = tx.send(Command::Diagnostic(
                            serde_json::json!({"type":"frida_frame_rejected","reason":error}),
                        ));
                        break;
                    }
                }
            }
            Err(error) => {
                if matches!(
                    error.kind(),
                    io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock
                ) {
                    if last_activity.elapsed() < FRIDA_STALE_TIMEOUT {
                        continue;
                    }
                    let _ = tx.send(Command::Diagnostic(serde_json::json!({
                        "type":"frida_transport_stale",
                        "reason":"heartbeat_timeout",
                        "timeout_seconds":FRIDA_STALE_TIMEOUT.as_secs()
                    })));
                } else {
                    let _ = tx.send(Command::Diagnostic(
                        serde_json::json!({"type":"frida_transport_error","error":error.to_string()}),
                    ));
                }
                break;
            }
        }
    }
    session.disconnected();
    let _ = stream.shutdown(std::net::Shutdown::Both);
    if generation.load(Ordering::Relaxed) == generation_id {
        let _ = tx.send(Command::Diagnostic(serde_json::json!({"type":"frida_transport_disconnected","peer":peer,"host_pid":host_pid,"ready":session.ready()})));
    }
}

fn action_label(action: &Action) -> String {
    match action {
        Action::Disabled => "disabled".to_string(),
        Action::Voice => "voice".to_string(),
        Action::KeyCombo { keys } => format!("key_combo:{}", keys.join("+")),
        Action::OpenApp { target } => format!("open_app:{target}"),
        Action::SystemVolumeUp => "volume_up".to_string(),
        Action::SystemVolumeDown => "volume_down".to_string(),
        Action::ArrowUp => "arrow_up".to_string(),
        Action::ArrowDown => "arrow_down".to_string(),
        Action::ArrowLeft => "arrow_left".to_string(),
        Action::ArrowRight => "arrow_right".to_string(),
        Action::Return => "return".to_string(),
        Action::DeleteBackward => "delete_backward".to_string(),
    }
}

/// Windows already posts these translations to the focused window. Injecting
/// them again makes arrows and OK jump twice. Back and the volume keys are
/// not in this set: the keyboard class drops them, so they still need a
/// synthesized key.
fn delivered_by_windows(button: Button, action: &Action) -> bool {
    matches!(
        (button, action),
        (Button::Up, Action::ArrowUp)
            | (Button::Down, Action::ArrowDown)
            | (Button::Left, Action::ArrowLeft)
            | (Button::Right, Action::ArrowRight)
            | (Button::Ok, Action::Return)
    )
}

fn emit_gestures(
    journal: &mut Journal,
    status: &mut bridge_status::Publisher,
    mapping: &Mapping,
    executor: &mut dyn ActionExecutor,
    gestures: Vec<(Button, Trigger)>,
    report_seq: Option<u64>,
    voice_hold: bool,
    physical: bool,
) {
    for (button, trigger) in gestures {
        journal.emit(serde_json::json!({"protocol":"remo.v1","type":"gesture_resolved","timestamp_ms":unix_ms(),"button":button_name(button),"trigger":trigger_name(trigger),"report_seq":report_seq}));
        for event in dispatch(mapping, [(button, trigger)]) {
            if event.action == Action::Disabled {
                journal.emit(serde_json::json!({"protocol":"remo.v1","type":"action_skipped","timestamp_ms":unix_ms(),"button":button_name(event.button),"reason":"disabled","report_seq":report_seq}));
                continue;
            }
            if physical
                && event.trigger == Trigger::Single
                && delivered_by_windows(event.button, &event.action)
            {
                journal.emit(serde_json::json!({"protocol":"remo.v1","type":"action_skipped","timestamp_ms":unix_ms(),"button":button_name(event.button),"reason":"already_delivered","report_seq":report_seq}));
                continue;
            }
            if voice_hold && event.action == Action::Voice {
                journal.emit(serde_json::json!({"protocol":"remo.v1","type":"action_skipped","timestamp_ms":unix_ms(),"button":button_name(event.button),"reason":"voice_hold","report_seq":report_seq}));
                continue;
            }
            let label = action_label(&event.action);
            if !journal.execute {
                status.note_action(&label, "simulated", "");
                if let Err(error) = status.write() {
                    eprintln!("bridge status write failed: {error}");
                }
                journal.emit(serde_json::json!({"protocol":"remo.v1","type":"action_simulated","timestamp_ms":unix_ms(),"button":button_name(event.button),"trigger":trigger_name(event.trigger),"action":event.action,"report_seq":report_seq}));
                continue;
            }
            journal.emit(serde_json::json!({"protocol":"remo.v1","type":"action_started","timestamp_ms":unix_ms(),"button":button_name(event.button),"trigger":trigger_name(event.trigger),"action":event.action,"report_seq":report_seq}));
            match executor.execute(&event.action) {
                Ok(()) => {
                    status.note_action(&label, "completed", "");
                    journal.emit(serde_json::json!({"protocol":"remo.v1","type":"action_completed","timestamp_ms":unix_ms(),"button":button_name(event.button),"trigger":trigger_name(event.trigger),"action":event.action,"report_seq":report_seq}));
                }
                Err(error) => {
                    status.note_action(&label, "failed", &error);
                    journal.emit(serde_json::json!({"protocol":"remo.v1","type":"action_failed","timestamp_ms":unix_ms(),"button":button_name(event.button),"trigger":trigger_name(event.trigger),"action":event.action,"error":error,"report_seq":report_seq}));
                }
            }
            if let Err(error) = status.write() {
                eprintln!("bridge status write failed: {error}");
            }
        }
    }
}

fn hold_voice(
    hub: Option<&mut voice_hub::VoiceHub>,
    executor: &mut dyn ActionExecutor,
    status: &mut bridge_status::Publisher,
    journal: &mut Journal,
    held: bool,
) {
    let state = if held { "held" } else { "released" };
    let (result, ime, chord) = if let Some(hub) = hub {
        let result = match hub.set_mic(held) {
            Ok("sendinput") => executor
                .set_voice_held(held)
                .map(|()| "sendinput".to_string()),
            Ok(backend) => Ok(backend.to_string()),
            Err(error) => Err(error),
        };
        (
            result,
            hub.last_ime().to_string(),
            hub.last_chord().to_string(),
        )
    } else {
        (
            executor
                .set_voice_held(held)
                .map(|()| "sendinput".to_string()),
            String::new(),
            String::new(),
        )
    };
    match result {
        Ok(backend) => {
            status.note_action("voice", state, "");
            journal.emit(serde_json::json!({"protocol":"remo.v1","type":"voice_shortcut","timestamp_ms":unix_ms(),"state":state,"backend":backend,"ime":ime,"chord":chord}));
        }
        Err(error) => status.note_action("voice", "failed", &error),
    }
    if let Err(error) = status.write() {
        eprintln!("bridge status write failed: {error}");
    }
}

fn track_repeat(
    repeats: &mut HashMap<Button, Instant>,
    mapping: &Mapping,
    button: Button,
    edge: Edge,
    at: Instant,
    physical: bool,
) {
    if physical && delivered_by_windows(button, &mapping.resolve(button, Trigger::Single)) {
        repeats.remove(&button);
        return;
    }
    if !repeatable(button, mapping) {
        return;
    }
    match edge {
        Edge::Down => {
            repeats
                .entry(button)
                .or_insert(at + Duration::from_millis(350));
        }
        Edge::Up => {
            repeats.remove(&button);
        }
    }
}

fn repeatable(button: Button, mapping: &Mapping) -> bool {
    if mapping.resolve(button, Trigger::Double) != Action::Disabled
        || mapping.resolve(button, Trigger::Long) != Action::Disabled
    {
        return false;
    }
    matches!(
        button,
        Button::Up
            | Button::Down
            | Button::Left
            | Button::Right
            | Button::Back
            | Button::VolumeUp
            | Button::VolumeDown
    ) && !matches!(
        mapping.resolve(button, Trigger::Single),
        Action::Disabled | Action::Voice | Action::OpenApp { .. }
    )
}

fn main() -> io::Result<()> {
    if env::args().any(|arg| arg == "--version") {
        println!("Remo Rust daemon 0.1.0");
        return Ok(());
    }
    let args: Vec<_> = env::args().skip(1).collect();
    let mut execute = false;
    let mut pipe = None;
    let mut backend = "simulation".to_string();
    let mut frida_pid = None;
    let mut frida_port = 30684u16;
    let mut iter = args.iter();
    while let Some(arg) = iter.next() {
        match arg.as_str() {
            "--execute" => execute = true,
            "--backend" => {
                backend = iter
                    .next()
                    .ok_or_else(|| {
                        io::Error::new(
                            io::ErrorKind::InvalidInput,
                            "--backend requires simulation|frida|ble|esp32",
                        )
                    })?
                    .clone();
                if !matches!(backend.as_str(), "simulation" | "frida" | "esp32" | "ble") {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidInput,
                        "unsupported backend",
                    ));
                }
            }
            "--frida-pid" => {
                frida_pid = Some(
                    iter.next()
                        .ok_or_else(|| {
                            io::Error::new(
                                io::ErrorKind::InvalidInput,
                                "--frida-pid requires a WUDFHost PID",
                            )
                        })?
                        .parse::<u32>()
                        .map_err(|_| {
                            io::Error::new(io::ErrorKind::InvalidInput, "invalid --frida-pid")
                        })?,
                );
            }
            "--frida-port" => {
                frida_port = iter
                    .next()
                    .ok_or_else(|| {
                        io::Error::new(
                            io::ErrorKind::InvalidInput,
                            "--frida-port requires a TCP port",
                        )
                    })?
                    .parse::<u16>()
                    .map_err(|_| {
                        io::Error::new(io::ErrorKind::InvalidInput, "invalid --frida-port")
                    })?;
            }
            "--pipe" => {
                pipe = Some(
                    iter.next()
                        .ok_or_else(|| {
                            io::Error::new(io::ErrorKind::InvalidInput, "--pipe requires a name")
                        })?
                        .clone(),
                )
            }
            _ => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!("unknown option: {arg}"),
                ))
            }
        }
    }
    if backend == "frida" && frida_pid.is_none() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "--backend frida requires --frida-pid",
        ));
    }
    let pipe_mode = pipe.is_some();
    let mut journal = Journal::open(execute, &backend)?;
    let mut status = bridge_status::Publisher::open()?;
    let status_detail = if backend == "simulation" {
        status.set_state("connected", "Rust 后台已启动");
        "simulation_only"
    } else {
        status.set_state("connecting", "正在等待遥控器按键通道");
        "transport_listening"
    };
    if let Err(error) = status.write() {
        eprintln!("bridge status write failed: {error}");
    }
    journal.emit(serde_json::json!({"protocol":"remo.v1","type":"daemon_started","timestamp_ms":unix_ms(),"backend":backend,"status":status_detail,"device_connected":false,"capabilities":{"keys":13,"hardware":false}}));
    journal.emit(serde_json::json!({"protocol":"remo.v1","type":"input_backend_selected","timestamp_ms":unix_ms(),"frida":if backend=="frida" { "selected_not_started" } else { "not_selected" },"ble":if backend=="ble" { "selected" } else { "not_selected" },"esp32":if backend=="esp32" { "selected_not_started" } else { "not_selected" },"fallback":"simulation"}));
    #[cfg(windows)]
    let _pipe = pipe
        .as_deref()
        .map(|name| ipc::PipeServer::start(name, journal.events.clone()))
        .transpose()?;
    let user = user_config::load();
    let mut mapping = user.mapping;
    let mut mapping_stamp = user_config::mapping_stamp();
    let mut mapping_checked = Instant::now();
    let voice_mode = user.voice_mode.clone();
    let voice_sustain = user.voice_sustain.clone();
    let voice_backend = user.voice_backend.clone();
    let voice_endpoint = user.output_endpoint_name.clone();
    let voice_gain = user.gain_db;
    let mut voice_hotkey = user.voice_hotkey.clone();
    let mut voice_preset = user.voice_preset.clone();
    let mut config_stamp = user_config::config_stamp();
    let mut executor = create_executor(execute, user.voice_hotkey);
    let mut engine = GestureEngine::new(Duration::from_millis(500), Duration::from_millis(280));
    engine.follow_mapping(&mapping);
    // Single-click actions are resolved after the double-click window. Keep
    // the originating Frida report with the pending button so delayed action
    // events remain traceable to the physical HID report that released it.
    let mut pending_report_seq: HashMap<Button, u64> = HashMap::new();
    let mut repeats: HashMap<Button, Instant> = HashMap::new();
    let frida_pid = if frida_pid.is_some() {
        frida_pid
    } else if backend == "ble" || backend == "frida" {
        frida_launch::host_pid().ok()
    } else {
        None
    };
    let rx = if backend == "frida" || (backend == "ble" && frida_pid.is_some()) {
        let host = frida_pid.expect("frida pid");
        let frida_rx = spawn_frida_listener(frida_port, host)?;
        if backend == "ble" {
            frida_launch::spawn_inject(host);
            let with_raw = merge_input_channels(spawn_reader(), ble_hid::spawn());
            merge_input_channels(with_raw, frida_rx)
        } else {
            merge_input_channels(spawn_reader(), frida_rx)
        }
    } else if backend == "ble" {
        merge_input_channels(spawn_reader(), ble_hid::spawn())
    } else {
        spawn_reader()
    };
    let mut frida_live = false;
    let physical = backend != "simulation";
    let (mut voice_hub, rx) = if execute && physical {
        let (hub, voice_rx) = voice_hub::start(
            &voice_backend,
            &voice_mode,
            &voice_hotkey,
            &voice_preset,
            &voice_sustain,
            voice_endpoint,
            voice_gain,
        );
        journal.emit(serde_json::json!({"protocol":"remo.v1","type":"voice_output_selected","timestamp_ms":unix_ms(),"mode":voice_mode,"backend":voice_backend,"hotkey":voice_hotkey.join("+"),"keyboard":if hub.keyboard_ready(){"winuhid"}else{"unavailable"},"error":hub.keyboard_error()}));
        (Some(hub), merge_input_channels(rx, voice_rx))
    } else {
        (None, rx)
    };
    let voice_owned = voice_hub.is_some();
    let mut stopping = false;
    while !stopping {
        match rx.recv_timeout(Duration::from_millis(20)) {
            Ok(Command::Quit) => stopping = true,
            Ok(Command::Input(input)) if frida_live => {
                let _ = input;
            }
            Ok(Command::Input(input)) => {
                let edge = match input.edge {
                    Edge::Down => "down",
                    Edge::Up => "up",
                };
                status.note_button(button_name(input.button), edge);
                if let Err(error) = status.write() {
                    eprintln!("bridge status write failed: {error}");
                }
                journal.emit(serde_json::json!({"protocol":"remo.v1","type":"simulated_button","timestamp_ms":unix_ms(),"button":button_name(input.button),"edge":input.edge}));
                track_repeat(
                    &mut repeats,
                    &mapping,
                    input.button,
                    input.edge,
                    input.at,
                    physical,
                );
                if voice_owned && input.button == Button::Mic {
                    hold_voice(
                        voice_hub.as_mut(),
                        executor.as_mut(),
                        &mut status,
                        &mut journal,
                        matches!(input.edge, Edge::Down),
                    );
                } else if matches!(input.edge, Edge::Down) {
                    if let Some(hub) = voice_hub.as_ref() {
                        hub.other_key();
                    }
                }
                emit_gestures(
                    &mut journal,
                    &mut status,
                    &mapping,
                    executor.as_mut(),
                    engine.input(input),
                    None,
                    voice_owned,
                    physical,
                );
            }
            Ok(Command::FridaInput { input, report_seq }) => {
                let edge = match input.edge {
                    Edge::Down => "down",
                    Edge::Up => "up",
                };
                status.note_button(button_name(input.button), edge);
                if let Err(error) = status.write() {
                    eprintln!("bridge status write failed: {error}");
                }
                journal.emit(serde_json::json!({"protocol":"remo.v1","type":"physical_button","timestamp_ms":unix_ms(),"button":button_name(input.button),"edge":input.edge,"report_seq":report_seq}));
                track_repeat(
                    &mut repeats,
                    &mapping,
                    input.button,
                    input.edge,
                    input.at,
                    true,
                );
                if let Some(sequence) = report_seq {
                    pending_report_seq.insert(input.button, sequence);
                }
                if voice_owned && input.button == Button::Mic {
                    hold_voice(
                        voice_hub.as_mut(),
                        executor.as_mut(),
                        &mut status,
                        &mut journal,
                        matches!(input.edge, Edge::Down),
                    );
                } else if matches!(input.edge, Edge::Down) {
                    if let Some(hub) = voice_hub.as_ref() {
                        hub.other_key();
                    }
                }
                let gestures = engine.input(input);
                emit_gestures(
                    &mut journal,
                    &mut status,
                    &mapping,
                    executor.as_mut(),
                    gestures.clone(),
                    report_seq,
                    voice_owned,
                    true,
                );
                if !gestures.is_empty() {
                    pending_report_seq.remove(&input.button);
                }
            }
            Ok(Command::Diagnostic(event)) => {
                match event["type"].as_str() {
                    Some("voice_audio_started") => {
                        status.set_state("voice_active", "正在接收遥控器语音");
                        if let Err(error) = status.write() {
                            eprintln!("bridge status write failed: {error}");
                        }
                    }
                    Some("voice_audio_stopped") => {
                        status.set_state("connected", "遥控器按键通道已连接");
                        if let Err(error) = status.write() {
                            eprintln!("bridge status write failed: {error}");
                        }
                    }
                    Some("frida_transport_connected") => {
                        frida_live = true;
                        status.set_state("connected", "遥控器按键通道已连接");
                        if let Err(error) = status.write() {
                            eprintln!("bridge status write failed: {error}");
                        }
                    }
                    Some("ble_transport_connected") => {
                        let detail = match event["name"].as_str() {
                            Some(name) if !name.is_empty() => {
                                format!("遥控器按键通道已连接：{name}")
                            }
                            _ => "遥控器按键通道已连接".to_string(),
                        };
                        status.set_state("connected", &detail);
                        if let Err(error) = status.write() {
                            eprintln!("bridge status write failed: {error}");
                        }
                    }
                    Some("ble_transport_progress") => {
                        let detail = event["detail"].as_str().unwrap_or("正在查找遥控器");
                        status.set_state("connecting", detail);
                        if let Err(error) = status.write() {
                            eprintln!("bridge status write failed: {error}");
                        }
                    }
                    Some(
                        "frida_transport_disconnected"
                        | "frida_transport_stale"
                        | "ble_transport_waiting",
                    ) => {
                        if event["type"] != "ble_transport_waiting" {
                            frida_live = false;
                            engine.cancel();
                            repeats.clear();
                            if let Some(hub) = voice_hub.as_mut() {
                                let _ = hub.set_mic(false);
                            }
                        }
                        let detail = event["error"].as_str().unwrap_or("正在重连遥控器");
                        status.set_state("reconnecting", detail);
                        if let Err(error) = status.write() {
                            eprintln!("bridge status write failed: {error}");
                        }
                    }
                    _ => {}
                }
                journal.emit(event);
            }
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) if !pipe_mode => stopping = true,
            Err(RecvTimeoutError::Disconnected) => thread::sleep(Duration::from_millis(20)),
        }
        if mapping_checked.elapsed() >= Duration::from_millis(400) {
            mapping_checked = Instant::now();
            let stamp = user_config::mapping_stamp();
            if stamp != mapping_stamp {
                if let Some(loaded) = user_config::load_mapping_file() {
                    mapping = loaded;
                    engine.follow_mapping(&mapping);
                    engine.cancel();
                    mapping_stamp = stamp;
                    journal.emit(serde_json::json!({"protocol":"remo.v1","type":"mapping_reloaded","timestamp_ms":unix_ms()}));
                }
            }
            let next_config = user_config::config_stamp();
            if next_config != config_stamp {
                let loaded = user_config::load();
                if loaded.voice_hotkey != voice_hotkey || loaded.voice_preset != voice_preset {
                    voice_hotkey = loaded.voice_hotkey;
                    voice_preset = loaded.voice_preset;
                    if let Some(hub) = voice_hub.as_mut() {
                        hub.set_route(&voice_hotkey, &voice_preset);
                    }
                    journal.emit(serde_json::json!({
                        "protocol": "remo.v1",
                        "type": "voice_hotkey_reloaded",
                        "timestamp_ms": unix_ms(),
                        "hotkey": voice_hotkey.join("+"),
                    }));
                }
                config_stamp = next_config;
            }
        }
        for (button, trigger) in engine.tick(Instant::now()) {
            let report_seq = pending_report_seq.get(&button).copied();
            emit_gestures(
                &mut journal,
                &mut status,
                &mapping,
                executor.as_mut(),
                vec![(button, trigger)],
                report_seq,
                voice_owned,
                physical,
            );
            pending_report_seq.remove(&button);
        }
        let now = Instant::now();
        let due: Vec<Button> = repeats
            .iter()
            .filter(|(_, next)| now >= **next)
            .map(|(button, _)| *button)
            .collect();
        for button in due {
            let interval = if button == Button::Back { 50 } else { 100 };
            if let Some(next) = repeats.get_mut(&button) {
                *next = now + Duration::from_millis(interval);
            }
            emit_gestures(
                &mut journal,
                &mut status,
                &mapping,
                executor.as_mut(),
                vec![(button, Trigger::Single)],
                None,
                voice_owned,
                physical,
            );
        }
    }
    // Do not fabricate a future timestamp and execute pending shortcuts while
    // shutting down or after the input source has disconnected.
    engine.cancel();
    if let Some(hub) = voice_hub.as_mut() {
        let _ = hub.set_mic(false);
    }
    let _ = executor.set_voice_held(false);
    status.set_state("stopped", "Rust 后台已停止");
    if let Err(error) = status.write() {
        eprintln!("bridge status write failed: {error}");
    }
    journal.emit(
        serde_json::json!({"protocol":"remo.v1","type":"daemon_stopped","timestamp_ms":unix_ms()}),
    );
    Ok(())
}
