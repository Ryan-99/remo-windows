//! Status file shared with the existing PySide6 settings window.
//!
//! The QML app polls `%LOCALAPPDATA%\RemoteMic\RC003\bridge-status.json`.
//! Field names and types match `ovb_rc003.bridge_status.BridgeStatus`.

use serde_json::{json, Value};
use std::collections::VecDeque;
use std::env;
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

const SCHEMA_VERSION: i64 = 1;
const MAX_EVENTS: usize = 32;
const MAX_DEBUG: usize = 40;

pub struct Publisher {
    path: PathBuf,
    pid: u32,
    state: String,
    detail: String,
    audio_active: bool,
    attempt: u32,
    button_seq: u64,
    last_button: String,
    last_button_state: String,
    button_events: VecDeque<(u64, String, String)>,
    last_action: String,
    last_action_state: String,
    last_action_error: String,
    debug_events: VecDeque<String>,
}

impl Publisher {
    pub fn open() -> io::Result<Self> {
        Self::open_in(config_root())
    }

    pub fn open_in(root: PathBuf) -> io::Result<Self> {
        fs::create_dir_all(&root)?;
        Ok(Self {
            path: root.join("bridge-status.json"),
            pid: std::process::id(),
            state: "stopped".into(),
            detail: String::new(),
            audio_active: false,
            attempt: 0,
            button_seq: 0,
            last_button: String::new(),
            last_button_state: String::new(),
            button_events: VecDeque::new(),
            last_action: String::new(),
            last_action_state: String::new(),
            last_action_error: String::new(),
            debug_events: VecDeque::new(),
        })
    }

    pub fn set_state(&mut self, state: &str, detail: &str) {
        self.state = state.to_string();
        self.detail = detail.to_string();
    }

    pub fn note_button(&mut self, button: &str, edge: &str) {
        self.button_seq += 1;
        self.last_button = button.to_string();
        self.last_button_state = edge.to_string();
        self.button_events
            .push_back((self.button_seq, self.last_button.clone(), self.last_button_state.clone()));
        while self.button_events.len() > MAX_EVENTS {
            self.button_events.pop_front();
        }
        self.push_debug(format!("{button} · {edge}"));
    }

    pub fn note_action(&mut self, action: &str, state: &str, error: &str) {
        self.last_action = action.to_string();
        self.last_action_state = state.to_string();
        self.last_action_error = error.to_string();
        let mut line = format!("{action} · {state}");
        if !error.is_empty() {
            line.push_str(" · ");
            line.push_str(error);
        }
        self.push_debug(line);
    }

    pub fn write(&self) -> io::Result<()> {
        let payload = serde_json::to_vec_pretty(&self.snapshot()).map_err(io::Error::other)?;
        let parent = self
            .path
            .parent()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "status path has no parent"))?;
        fs::create_dir_all(parent)?;
        let temporary = parent.join(format!(
            ".bridge-status.{}.{}.tmp",
            self.pid,
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos()
        ));
        {
            let mut file = fs::File::create(&temporary)?;
            file.write_all(&payload)?;
            file.write_all(b"\n")?;
            file.sync_all()?;
        }
        replace_file(&temporary, &self.path)
    }

    fn push_debug(&mut self, line: String) {
        self.debug_events.push_back(line);
        while self.debug_events.len() > MAX_DEBUG {
            self.debug_events.pop_front();
        }
    }

    fn snapshot(&self) -> Value {
        json!({
            "schema_version": SCHEMA_VERSION,
            "state": self.state,
            "detail": self.detail,
            "pid": self.pid,
            "audio_active": self.audio_active,
            "attempt": self.attempt,
            "updated_at": utc_now(),
            "last_button": self.last_button,
            "last_button_state": self.last_button_state,
            "button_seq": self.button_seq,
            "button_events": self.button_events.iter().map(|(seq, button, edge)| json!([seq, button, edge])).collect::<Vec<_>>(),
            "last_action": self.last_action,
            "last_action_state": self.last_action_state,
            "last_action_error": self.last_action_error,
            "debug_events": self.debug_events.iter().cloned().collect::<Vec<_>>(),
        })
    }
}

pub fn config_root() -> PathBuf {
    if let Some(root) = env::var_os("REMO_CONFIG_ROOT") {
        return PathBuf::from(root);
    }
    env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(env::temp_dir)
        .join("RemoteMic")
        .join("RC003")
}

fn utc_now() -> String {
    let elapsed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    let secs = elapsed.as_secs();
    let micros = elapsed.subsec_micros();
    let days = secs / 86_400;
    let tod = secs % 86_400;
    let (year, month, day) = civil_from_days(days as i64);
    format!(
        "{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}.{micros:06}+00:00",
        hour = tod / 3600,
        minute = (tod % 3600) / 60,
        second = tod % 60,
    )
}

fn civil_from_days(days: i64) -> (i32, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = if month <= 2 { y + 1 } else { y };
    (year as i32, month as u32, day as u32)
}

fn replace_file(from: &Path, to: &Path) -> io::Result<()> {
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Storage::FileSystem::{
            MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
        };
        let from_wide: Vec<u16> = from.as_os_str().encode_wide().chain(Some(0)).collect();
        let to_wide: Vec<u16> = to.as_os_str().encode_wide().chain(Some(0)).collect();
        let moved = unsafe {
            MoveFileExW(
                from_wide.as_ptr(),
                to_wide.as_ptr(),
                MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
            )
        };
        if moved == 0 {
            return Err(io::Error::last_os_error());
        }
        return Ok(());
    }
    #[cfg(not(windows))]
    {
        fs::rename(from, to)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn button_history_keeps_the_latest_thirty_two_and_round_trips() {
        let dir = env::temp_dir().join(format!(
            "remo-status-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let mut publisher = Publisher::open_in(dir.clone()).unwrap();
        publisher.set_state("connected", "Rust 后台已启动");
        for index in 0..40 {
            publisher.note_button(if index % 2 == 0 { "ok" } else { "up" }, "down");
        }
        publisher.note_action("return", "simulated", "");
        publisher.write().unwrap();
        let raw = fs::read_to_string(dir.join("bridge-status.json")).unwrap();
        let value: Value = serde_json::from_str(&raw).unwrap();
        assert_eq!(value["schema_version"], 1);
        assert_eq!(value["state"], "connected");
        assert_eq!(value["pid"], std::process::id());
        assert_eq!(value["button_seq"], 40);
        assert_eq!(value["button_events"].as_array().unwrap().len(), 32);
        assert_eq!(value["button_events"][0][0], 9);
        assert_eq!(value["last_button"], "up");
        assert_eq!(value["last_button_state"], "down");
        assert_eq!(value["last_action"], "return");
        assert!(value["updated_at"].as_str().unwrap().contains('T'));
        let _ = fs::remove_dir_all(dir);
    }
}
