//! Read-only diagnostic IPC. No UI command may inject input into an elevated
//! daemon. Mutating configuration/control IPC needs a separate authenticated
//! contract before hardware execution can be enabled there.
use serde_json::{json, Value};
use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

#[derive(Default)]
pub struct EventStore {
    events: VecDeque<Value>,
    latest: u64,
    state: Value,
}
pub type SharedEvents = Arc<Mutex<EventStore>>;

impl EventStore {
    pub fn push(&mut self, event: Value) {
        self.latest = event["event_id"].as_u64().unwrap_or(self.latest);
        if event["type"] == "daemon_started" || event["type"] == "daemon_stopped" {
            self.state = event.clone();
        }
        if self.events.len() == 256 {
            self.events.pop_front();
        }
        self.events.push_back(event);
    }
    fn reply(&self, line: &[u8]) -> Value {
        let Ok(request) = serde_json::from_slice::<Value>(line) else {
            return json!({"protocol":"remo.v1","type":"error","error":"invalid_json"});
        };
        if request["protocol"] != "remo.v1" {
            return json!({"protocol":"remo.v1","type":"error","error":"unsupported_protocol"});
        }
        match request["type"].as_str() {
            Some("get_state") => json!({"protocol":"remo.v1","type":"state","payload":self.state}),
            Some("get_events") => {
                let after = request["after_id"].as_u64().unwrap_or(0);
                let oldest = self
                    .events
                    .front()
                    .and_then(|e| e["event_id"].as_u64())
                    .unwrap_or(0);
                json!({"protocol":"remo.v1","type":"events","last_event_id":self.latest,
                    "gap":after.saturating_add(1)<oldest,
                    "payload": self.events.iter().filter(|e| e["event_id"].as_u64().unwrap_or(0)>after).collect::<Vec<_>>()})
            }
            _ => json!({"protocol":"remo.v1","type":"error","error":"read_only_endpoint"}),
        }
    }
}

#[cfg(windows)]
pub struct PipeServer {
    stop: Arc<std::sync::atomic::AtomicBool>,
    thread: Option<std::thread::JoinHandle<()>>,
}

#[cfg(windows)]
impl PipeServer {
    pub fn start(name: &str, events: SharedEvents) -> std::io::Result<Self> {
        use std::{
            ptr::{null, null_mut},
            sync::atomic::{AtomicBool, Ordering},
            thread,
            time::{Duration, Instant},
        };
        use windows_sys::Win32::{Foundation::*, Storage::FileSystem::*, System::Pipes::*};
        if name.is_empty()
            || name.len() > 128
            || !name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
        {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "pipe name must be ASCII alphanumeric or hyphen",
            ));
        }
        let wide: Vec<_> = format!(r"\\.\pipe\{name}")
            .encode_utf16()
            .chain(Some(0))
            .collect();
        // One exclusive, local-only diagnostic endpoint. Nonblocking mode lets
        // shutdown join even when a client connects but never sends a newline.
        let handle = unsafe {
            CreateNamedPipeW(
                wide.as_ptr(),
                PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE,
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_NOWAIT | PIPE_REJECT_REMOTE_CLIENTS,
                1,
                262144,
                8192,
                0,
                null(),
            )
        };
        if handle == INVALID_HANDLE_VALUE {
            return Err(std::io::Error::last_os_error());
        }
        let stop = Arc::new(AtomicBool::new(false));
        let thread_stop = stop.clone();
        let raw = handle as usize;
        let thread = thread::spawn(move || {
            let handle = raw as HANDLE;
            let mut connected = false;
            let mut input = Vec::new();
            let mut pending = Vec::new();
            let mut last_activity = Instant::now();
            while !thread_stop.load(Ordering::Relaxed) {
                if !connected {
                    let ok = unsafe { ConnectNamedPipe(handle, null_mut()) } != 0;
                    connected = ok || unsafe { GetLastError() } == ERROR_PIPE_CONNECTED;
                    if connected {
                        last_activity = Instant::now();
                    }
                } else {
                    let mut drop_client = last_activity.elapsed() > Duration::from_secs(5);
                    if !pending.is_empty() {
                        let mut written = 0;
                        let ok = unsafe {
                            WriteFile(
                                handle,
                                pending.as_ptr(),
                                pending.len() as u32,
                                &mut written,
                                null_mut(),
                            )
                        } != 0;
                        if written > 0 {
                            pending.drain(..written as usize);
                            last_activity = Instant::now();
                        }
                        if !ok && unsafe { GetLastError() } != ERROR_NO_DATA {
                            drop_client = true;
                        }
                    } else {
                        let mut buffer = [0u8; 1024];
                        let mut read = 0;
                        let ok = unsafe {
                            ReadFile(
                                handle,
                                buffer.as_mut_ptr(),
                                buffer.len() as u32,
                                &mut read,
                                null_mut(),
                            )
                        } != 0;
                        if ok && read > 0 {
                            last_activity = Instant::now();
                            input.extend_from_slice(&buffer[..read as usize]);
                        } else if !ok && unsafe { GetLastError() } != ERROR_NO_DATA {
                            drop_client = true;
                        }
                        if input.len() > 8192 {
                            drop_client = true;
                        }
                        if !drop_client {
                            if let Some(end) = input.iter().position(|b| *b == b'\n') {
                                let line: Vec<_> = input.drain(..=end).collect();
                                let response = events.lock().unwrap().reply(&line);
                                pending = response.to_string().into_bytes();
                                pending.push(b'\n');
                            }
                        }
                    }
                    if drop_client {
                        unsafe {
                            DisconnectNamedPipe(handle);
                        }
                        connected = false;
                        input.clear();
                        pending.clear();
                    }
                }
                thread::sleep(Duration::from_millis(10));
            }
            unsafe {
                DisconnectNamedPipe(handle);
                CloseHandle(handle);
            }
        });
        Ok(Self {
            stop,
            thread: Some(thread),
        })
    }
}

#[cfg(windows)]
impl Drop for PipeServer {
    fn drop(&mut self) {
        self.stop.store(true, std::sync::atomic::Ordering::Relaxed);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cursor_and_gap_report_dropped_history() {
        let mut store = EventStore::default();
        for id in 1..=300 {
            store.push(json!({"event_id":id,"type":"test"}));
        }
        let reply = store.reply(br#"{"protocol":"remo.v1","type":"get_events","after_id":0}"#);
        assert_eq!(reply["gap"], true);
        assert_eq!(reply["payload"].as_array().unwrap().len(), 256);
        let reply = store.reply(br#"{"protocol":"remo.v1","type":"get_events","after_id":299}"#);
        assert_eq!(reply["payload"].as_array().unwrap().len(), 1);
        assert_eq!(reply["gap"], false);
    }
    #[test]
    fn endpoint_rejects_mutations_and_invalid_protocols() {
        let store = EventStore::default();
        assert_eq!(store.reply(b"{")["error"], "invalid_json");
        assert_eq!(
            store.reply(br#"{"type":"get_state"}"#)["error"],
            "unsupported_protocol"
        );
        assert_eq!(
            store.reply(br#"{"protocol":"remo.v1","type":"test_action"}"#)["error"],
            "read_only_endpoint"
        );
    }
}
