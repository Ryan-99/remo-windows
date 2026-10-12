//! Parser for the existing observe-v1 Gadget wire format. This is not a live
//! backend: host discovery, socket authentication, deadlines and injection must
//! still be implemented. Checking a frame's PID alone cannot authenticate it.

use remo_core::{Button, Edge, InputEvent};
use std::collections::HashSet;
use std::time::Instant;

fn button_for_usage(usage: u16) -> Option<Button> {
    Some(match usage {
        0x003E => Button::Mic,
        0x00F1 => Button::Back,
        0x0028 => Button::Ok,
        0x0035 => Button::Tv,
        0x004A => Button::Home,
        0x004F => Button::Right,
        0x0050 => Button::Left,
        0x0051 => Button::Down,
        0x0052 => Button::Up,
        0x0065 => Button::Menu,
        0x0066 => Button::Power,
        0x0080 | 0x00E9 => Button::VolumeUp,
        0x0081 | 0x00EA => Button::VolumeDown,
        _ => return None,
    })
}

fn button_rank(button: Button) -> u8 {
    match button {
        Button::Mic => 0,
        Button::Power => 1,
        Button::Up => 2,
        Button::Down => 3,
        Button::Left => 4,
        Button::Right => 5,
        Button::Ok => 6,
        Button::Back => 7,
        Button::VolumeUp => 8,
        Button::VolumeDown => 9,
        Button::Home => 10,
        Button::Menu => 11,
        Button::Tv => 12,
    }
}

enum Decoded {
    /// Audio and other non-keyboard IOCTLs share this socket. Dropping the
    /// session on them clears held keys and the next real report never
    /// emits the matching release.
    Ignore,
    Buttons(HashSet<Button>),
}

fn decode_report(raw: &str) -> Result<Decoded, String> {
    if raw.len() != 18 || !raw.is_ascii() {
        return Err("gatt report must be exactly 9 hex-encoded bytes".into());
    }
    let data = (0..raw.len())
        .step_by(2)
        .map(|index| u8::from_str_radix(&raw[index..index + 2], 16));
    let bytes: Result<Vec<_>, _> = data.collect();
    let bytes = bytes.map_err(|_| "gatt report is not valid hex".to_string())?;
    if bytes[..3] != [1, 0, 0] {
        return Ok(Decoded::Ignore);
    }
    let mut buttons = HashSet::new();
    for index in (3..9).step_by(2) {
        let usage = u16::from_le_bytes([bytes[index], bytes[index + 1]]);
        if usage == 0 {
            continue;
        }
        let Some(button) = button_for_usage(usage) else {
            return Ok(Decoded::Ignore);
        };
        buttons.insert(button);
    }
    Ok(Decoded::Buttons(buttons))
}

pub struct FridaSession {
    host_pid: u32,
    last_sequence: Option<u64>,
    active: HashSet<Button>,
    ready: bool,
    pub heartbeats: u64,
    pub reports: u64,
    last_report_sequence: Option<u64>,
}

impl FridaSession {
    pub fn new(host_pid: u32) -> Self {
        Self {
            host_pid,
            last_sequence: None,
            active: HashSet::new(),
            ready: false,
            heartbeats: 0,
            reports: 0,
            last_report_sequence: None,
        }
    }

    pub fn ready(&self) -> bool {
        self.ready
    }

    /// Sequence of the most recently accepted HID-over-GATT report. This is
    /// diagnostic context only; it lets the journal correlate down/up edges
    /// with the report that produced them without changing the core input API.
    pub fn last_report_sequence(&self) -> Option<u64> {
        self.last_report_sequence
    }

    pub fn process(
        &mut self,
        message: &serde_json::Value,
        at: Instant,
    ) -> Result<Vec<InputEvent>, String> {
        let result = self.process_frame(message, at);
        if result.is_err() {
            self.disconnected();
        }
        result
    }

    fn process_frame(
        &mut self,
        message: &serde_json::Value,
        at: Instant,
    ) -> Result<Vec<InputEvent>, String> {
        if message["pid"].as_u64() != Some(self.host_pid as u64) || self.host_pid == 0 {
            return Err("Frida frame has a missing or different WUDFHost PID".into());
        }
        match message["kind"].as_str() {
            Some("ready") => {
                if self.ready
                    || message["hook_installed"] != true
                    || message["protocol"] != "observe-v1"
                {
                    return Err("invalid Frida ready handshake".into());
                }
                self.ready = true;
                Ok(Vec::new())
            }
            Some("heartbeat") => {
                if !self.ready || message["hook_installed"] != true {
                    return Err("heartbeat before verified ready".into());
                }
                self.heartbeats += 1;
                Ok(Vec::new())
            }
            Some("gatt_read") => {
                if !self.ready {
                    return Err("gatt report before verified ready".into());
                }
                let sequence = message["seq"]
                    .as_u64()
                    .ok_or("gatt report sequence is missing")?;
                if sequence == 0
                    || self
                        .last_sequence
                        .is_some_and(|last| last.checked_add(1) != Some(sequence))
                {
                    return Err("Frida sequence gap".into());
                }
                let current = decode_report(
                    message["raw"]
                        .as_str()
                        .ok_or("gatt report raw is missing")?,
                )?;
                let Decoded::Buttons(current) = current else {
                    self.last_sequence = Some(sequence);
                    return Ok(Vec::new());
                };
                let mut events = Vec::new();
                for button in current.difference(&self.active) {
                    events.push(InputEvent {
                        button: *button,
                        edge: Edge::Down,
                        at,
                    });
                }
                for button in self.active.difference(&current) {
                    events.push(InputEvent {
                        button: *button,
                        edge: Edge::Up,
                        at,
                    });
                }
                events.sort_by_key(|event| button_rank(event.button));
                self.active = current;
                self.last_sequence = Some(sequence);
                self.last_report_sequence = Some(sequence);
                self.reports += 1;
                Ok(events)
            }
            Some("error") => Err(message["message"]
                .as_str()
                .unwrap_or("Frida reported an error")
                .to_string()),
            Some(other) => Err(format!("unsupported Frida frame kind: {other}")),
            None => Err("Frida frame kind is missing".into()),
        }
    }

    /// Transport loss never becomes a physical release. The caller must
    /// separately cancel gestures and release any output keys it owns.
    pub fn disconnected(&mut self) {
        self.ready = false;
        self.last_sequence = None;
        self.last_report_sequence = None;
        self.active.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    fn report(seq: u64, raw: &str) -> serde_json::Value {
        json!({"kind":"gatt_read","pid":7,"seq":seq,"raw":raw})
    }
    fn connected() -> FridaSession {
        let mut session = FridaSession::new(7);
        session
            .process(
                &json!({"kind":"ready","pid":7,"hook_installed":true,"protocol":"observe-v1"}),
                Instant::now(),
            )
            .unwrap();
        session
    }
    #[test]
    fn malformed_frames_never_panic_or_leave_a_session_ready() {
        for raw in ["好好好好好好", "zz00003e0000000000", ""] {
            let mut session = connected();
            assert!(session.process(&report(1, raw), Instant::now()).is_err());
            assert!(!session.ready());
        }
    }
    #[test]
    fn non_keyboard_reports_do_not_drop_a_held_key() {
        let t = Instant::now();
        let mut session = connected();
        let down = session
            .process(&report(1, "0100003e0000000000"), t)
            .unwrap();
        assert_eq!(down.len(), 1);
        for (seq, raw) in [
            (2u64, "0200003e0000000000"),
            (3, "010000990000000000"),
            (4, "010000160000000000"),
        ] {
            assert!(session.process(&report(seq, raw), t).unwrap().is_empty());
            assert!(session.ready());
        }
        assert_eq!(session.reports, 1);
        assert!(session
            .process(&report(5, "0100003e0000000000"), t)
            .unwrap()
            .is_empty());
        let up = session
            .process(&report(6, "010000000000000000"), t)
            .unwrap();
        assert_eq!(up.len(), 1);
        assert_eq!(up[0].edge, Edge::Up);
    }
    #[test]
    fn missing_pid_and_duplicate_ready_invalidate_the_session() {
        let mut session = connected();
        assert!(session
            .process(
                &json!({"kind":"heartbeat","hook_installed":true}),
                Instant::now()
            )
            .is_err());
        assert!(!session.ready());
        let mut session = connected();
        assert!(session
            .process(
                &json!({"kind":"ready","pid":7,"hook_installed":true,"protocol":"observe-v1"}),
                Instant::now()
            )
            .is_err());
    }
    #[test]
    fn all_thirteen_usages_pair_and_repeated_reports_have_no_edges() {
        for usage in [
            0x3E, 0xF1, 0x28, 0x35, 0x4A, 0x4F, 0x50, 0x51, 0x52, 0x65, 0x66, 0x80, 0x81,
        ] {
            let mut session = connected();
            let raw = format!("010000{usage:02x}0000000000");
            let down = session.process(&report(1, &raw), Instant::now()).unwrap();
            assert_eq!(down.len(), 1);
            assert_eq!(down[0].button, button_for_usage(usage).unwrap());
            assert_eq!(down[0].edge, Edge::Down);
            assert!(session
                .process(&report(2, &raw), Instant::now())
                .unwrap()
                .is_empty());
            let up = session
                .process(&report(3, "010000000000000000"), Instant::now())
                .unwrap();
            assert_eq!(up.len(), 1);
            assert_eq!(up[0].edge, Edge::Up);
        }
    }
    #[test]
    fn sequence_overflow_is_rejected_without_panicking() {
        let mut session = connected();
        session
            .process(&report(u64::MAX, "010000000000000000"), Instant::now())
            .unwrap();
        assert!(session
            .process(&report(1, "010000000000000000"), Instant::now())
            .is_err());
    }
    #[test]
    fn handshake_heartbeat_and_edges_are_strictly_session_bound() {
        let t = Instant::now();
        let mut session = FridaSession::new(7);
        assert!(session
            .process(
                &json!({"kind":"heartbeat","pid":7,"hook_installed":true}),
                t
            )
            .is_err());
        session
            .process(
                &json!({"kind":"ready","pid":7,"hook_installed":true,"protocol":"observe-v1"}),
                t,
            )
            .unwrap();
        session
            .process(
                &json!({"kind":"heartbeat","pid":7,"hook_installed":true}),
                t,
            )
            .unwrap();
        let down = session
            .process(&report(1, "0100003e0000000000"), t)
            .unwrap();
        assert_eq!(down.len(), 1);
        assert_eq!(down[0].button, Button::Mic);
        assert_eq!(down[0].edge, Edge::Down);
        let up = session
            .process(&report(2, "010000000000000000"), t)
            .unwrap();
        assert_eq!(up.len(), 1);
        assert_eq!(up[0].edge, Edge::Up);
        assert_eq!(session.last_report_sequence(), Some(2));
    }
    #[test]
    fn gaps_unknown_usage_and_old_host_are_rejected_without_state_corruption() {
        let t = Instant::now();
        let mut session = FridaSession::new(7);
        session
            .process(
                &json!({"kind":"ready","pid":7,"hook_installed":true,"protocol":"observe-v1"}),
                t,
            )
            .unwrap();
        session
            .process(&report(1, "010000000000000000"), t)
            .unwrap();
        assert!(session
            .process(&report(3, "0100003e0000000000"), t)
            .is_err());
        assert_eq!(session.reports, 1);
        assert!(session
            .process(
                &json!({"kind":"gatt_read","pid":9,"seq":1,"raw":"0100003e0000000000"}),
                t
            )
            .is_err());
        assert!(session
            .process(&report(1, "010000990000000000"), t)
            .is_err());
        assert_eq!(session.reports, 1);
    }
    #[test]
    fn disconnect_requires_a_new_handshake() {
        let t = Instant::now();
        let mut session = FridaSession::new(7);
        session
            .process(
                &json!({"kind":"ready","pid":7,"hook_installed":true,"protocol":"observe-v1"}),
                t,
            )
            .unwrap();
        session.disconnected();
        assert!(!session.ready());
        assert!(session
            .process(&report(1, "0100003e0000000000"), t)
            .is_err());
    }
}
