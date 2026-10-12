use serde_json::Value;
use std::{
    io::{BufRead, BufReader, Write},
    net::TcpStream,
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicU64, Ordering},
        mpsc,
    },
    time::{Duration, Instant},
};

static NEXT: AtomicU64 = AtomicU64::new(0);
struct Daemon {
    child: Child,
    events: mpsc::Receiver<Value>,
    name: String,
    logs: std::path::PathBuf,
}
impl Daemon {
    fn start(pipe: bool) -> Self {
        let id = NEXT.fetch_add(1, Ordering::Relaxed);
        let name = format!("remo-test-{}-{id}", std::process::id());
        let logs = std::env::temp_dir().join(format!(
            "{name}-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_remo-daemon"));
        if pipe {
            cmd.args(["--pipe", &name]);
        }
        let mut child = cmd
            .env("REMO_LOG_DIR", &logs)
            .env("REMO_CONFIG_ROOT", logs.with_extension("config"))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap();
        let stdout = child.stdout.take().unwrap();
        let (tx, events) = mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                let Ok(line) = line else { break };
                if tx.send(serde_json::from_str(&line).unwrap()).is_err() {
                    break;
                }
            }
        });
        let daemon = Self {
            child,
            events,
            name,
            logs,
        };
        assert_eq!(daemon.next()["type"], "daemon_started");
        assert_eq!(daemon.next()["type"], "input_backend_selected");
        daemon
    }

    fn start_named(name: String, logs: std::path::PathBuf, args: Vec<String>) -> Self {
        let mut child = Command::new(env!("CARGO_BIN_EXE_remo-daemon"))
            .args(args)
            .env("REMO_LOG_DIR", &logs)
            .env("REMO_CONFIG_ROOT", logs.with_extension("config"))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap();
        let stdout = child.stdout.take().unwrap();
        let (tx, events) = mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                let Ok(line) = line else { break };
                if tx.send(serde_json::from_str(&line).unwrap()).is_err() {
                    break;
                }
            }
        });
        let daemon = Self {
            child,
            events,
            name,
            logs,
        };
        assert_eq!(daemon.next()["type"], "daemon_started");
        assert_eq!(daemon.next()["type"], "input_backend_selected");
        daemon
    }
    fn next(&self) -> Value {
        self.events
            .recv_timeout(Duration::from_secs(3))
            .expect("daemon event timeout")
    }
    fn send(&mut self, line: &str) {
        writeln!(self.child.stdin.as_mut().unwrap(), "{line}").unwrap();
    }
    fn until(&self, kind: &str) -> Vec<Value> {
        let mut events = Vec::new();
        for _ in 0..32 {
            let event = self.next();
            let found = event["type"] == kind;
            events.push(event);
            if found {
                return events;
            }
        }
        panic!("event not found: {kind}");
    }
    fn stop(&mut self) {
        self.send("quit");
        let started = Instant::now();
        loop {
            if let Some(status) = self.child.try_wait().unwrap() {
                assert!(status.success());
                break;
            }
            assert!(
                started.elapsed() < Duration::from_secs(3),
                "shutdown timeout"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
    }
}
impl Drop for Daemon {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[test]
fn single_fires_while_stdin_is_idle_and_never_claims_physical_execution() {
    let mut d = Daemon::start(false);
    d.send("ok down");
    d.send("ok up");
    let events = d.until("action_simulated");
    assert_eq!(
        events
            .iter()
            .filter(|e| e["type"] == "simulated_button")
            .count(),
        2
    );
    assert!(!events
        .iter()
        .any(|e| e["type"] == "physical_button" || e["type"] == "action_completed"));
    for pair in events.windows(2) {
        assert!(pair[0]["event_id"].as_u64() < pair[1]["event_id"].as_u64());
    }
    assert!(events.iter().all(|e| e["source"] == "simulation"));
    d.stop();
    let path = std::fs::read_dir(&d.logs)
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    let journal = std::fs::read_to_string(path).unwrap();
    assert!(journal.contains("daemon_stopped"));
}

#[test]
fn simulated_press_is_visible_in_the_settings_status_file() {
    let mut d = Daemon::start(false);
    d.send("power down");
    d.send("power up");
    let mut edges = 0;
    for _ in 0..8 {
        let event = d.next();
        if event["type"] == "simulated_button" {
            edges += 1;
            if edges == 2 {
                break;
            }
        }
    }
    assert_eq!(edges, 2);
    let status_path = d.logs.with_extension("config").join("bridge-status.json");
    let raw = std::fs::read_to_string(&status_path).expect("settings status file");
    let status: Value = serde_json::from_str(&raw).unwrap();
    assert_eq!(status["schema_version"], 1);
    assert_eq!(status["state"], "connected");
    assert!(status["pid"].as_u64().unwrap() > 0);
    assert_eq!(status["last_button"], "power");
    assert_eq!(status["last_button_state"], "up");
    assert_eq!(status["button_seq"], 2);
    assert_eq!(
        status["button_events"],
        serde_json::json!([[1, "power", "down"], [2, "power", "up"]])
    );
    d.stop();
    let stopped: Value =
        serde_json::from_str(&std::fs::read_to_string(&status_path).unwrap()).unwrap();
    assert_eq!(stopped["state"], "stopped");
    assert_eq!(stopped["button_seq"], 2);
}

#[test]
fn long_fires_before_release_without_a_second_trigger() {
    let mut d = Daemon::start(false);
    d.send("mic down");
    let events = d.until("action_simulated");
    assert_eq!(events.last().unwrap()["trigger"], "long");
    d.send("mic up");
    assert_eq!(d.next()["type"], "simulated_button");
    assert!(matches!(
        d.events.recv_timeout(Duration::from_millis(350)),
        Err(mpsc::RecvTimeoutError::Timeout)
    ));
    d.stop();
}

#[test]
fn quit_does_not_execute_pending_clicks() {
    let mut d = Daemon::start(false);
    d.send("ok down");
    d.stop();
    let events = d.until("daemon_stopped");
    assert!(!events
        .iter()
        .any(|e| e["type"] == "gesture_resolved" || e["type"] == "action_simulated"));
}

#[test]
fn frida_transport_converts_verified_mock_gadget_reports_to_physical_events() {
    let id = NEXT.fetch_add(1, Ordering::Relaxed);
    let port = 31684u16 + id as u16;
    let name = format!("remo-frida-test-{id}");
    let logs = std::env::temp_dir().join(format!(
        "{name}-{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    let args = vec![
        "--backend".into(),
        "frida".into(),
        "--frida-pid".into(),
        "777".into(),
        "--frida-port".into(),
        port.to_string(),
    ];
    let mut d = Daemon::start_named(name, logs, args);
    let deadline = Instant::now() + Duration::from_secs(3);
    let mut stream = loop {
        match TcpStream::connect(("127.0.0.1", port)) {
            Ok(stream) => break stream,
            Err(_) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(20)),
            Err(error) => panic!("frida mock connect: {error}"),
        }
    };
    stream
        .write_all(
            br#"{"kind":"ready","pid":777,"hook_installed":true,"protocol":"observe-v1"}
"#,
        )
        .unwrap();
    stream
        .write_all(
            br#"{"kind":"heartbeat","pid":777,"hook_installed":true}
"#,
        )
        .unwrap();
    // The Gadget heartbeat cadence is five seconds. The Rust transport must
    // tolerate several one-second socket read timeouts without reconnecting.
    std::thread::sleep(Duration::from_secs(3));
    stream
        .write_all(
            br#"{"kind":"heartbeat","pid":777,"hook_installed":true}
"#,
        )
        .unwrap();
    stream
        .write_all(
            br#"{"kind":"gatt_read","pid":777,"seq":1,"raw":"0100003e0000000000"}
"#,
        )
        .unwrap();
    let down = d.until("physical_button");
    assert!(!down.iter().any(|event| matches!(
        event["type"].as_str(),
        Some("frida_transport_error" | "frida_transport_disconnected" | "frida_transport_stale")
    )));
    assert_eq!(down.last().unwrap()["button"], "mic");
    assert_eq!(down.last().unwrap()["edge"], "down");
    assert_eq!(down.last().unwrap()["report_seq"], 1);
    assert_eq!(down.last().unwrap()["source"], "frida");
    stream
        .write_all(
            br#"{"kind":"gatt_read","pid":777,"seq":2,"raw":"010000000000000000"}
"#,
        )
        .unwrap();
    // The single action is intentionally delayed by the double-click window;
    // avoid making this transport test depend on stdout scheduling.
    d.stop();
}

#[cfg(windows)]
fn pipe_exchange(name: &str, line: &str) -> Value {
    let name = name.to_owned();
    let line = line.to_owned();
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(2);
        let mut file = loop {
            match std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .open(format!(r"\\.\pipe\{name}"))
            {
                Ok(file) => break file,
                Err(_) if Instant::now() < deadline => {
                    std::thread::sleep(Duration::from_millis(20))
                }
                Err(error) => panic!("pipe open: {error}"),
            }
        };
        writeln!(file, "{line}").unwrap();
        let mut response = String::new();
        BufReader::new(file).read_line(&mut response).unwrap();
        let _ = tx.send(serde_json::from_str(&response).unwrap());
    });
    rx.recv_timeout(Duration::from_secs(4))
        .expect("pipe request timeout")
}

#[cfg(windows)]
#[test]
fn real_named_pipe_reconnect_state_and_read_only_contract() {
    let mut d = Daemon::start(true);
    let state = pipe_exchange(&d.name, r#"{"protocol":"remo.v1","type":"get_state"}"#);
    assert_eq!(state["payload"]["device_connected"], false);
    assert_eq!(state["payload"]["status"], "simulation_only");
    assert_eq!(
        pipe_exchange(&d.name, r#"{"protocol":"remo.v1","type":"test_action"}"#)["error"],
        "read_only_endpoint"
    );
    d.send("volume_up down");
    d.send("volume_up up");
    d.until("action_simulated");
    let events = pipe_exchange(
        &d.name,
        r#"{"protocol":"remo.v1","type":"get_events","after_id":2}"#,
    );
    assert_eq!(events["gap"], false);
    assert!(events["payload"]
        .as_array()
        .unwrap()
        .iter()
        .any(|e| e["type"] == "action_simulated"));
    d.stop();
}
