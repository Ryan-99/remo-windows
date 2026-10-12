//! RC003 HID report decoding shared by Frida and BLE.
//!
//! Report framing follows the remote's 6-byte little-endian usage list.
//! Usage ids match the production map already used by the Python bridge.

use remo_core::Button;
use std::collections::BTreeSet;

pub fn usage_to_button(usage: u16) -> Option<Button> {
    Some(match usage {
        0x0028 => Button::Ok,
        0x0035 => Button::Tv,
        0x003E => Button::Mic,
        0x004A => Button::Home,
        0x004F => Button::Right,
        0x0050 => Button::Left,
        0x0051 => Button::Down,
        0x0052 => Button::Up,
        0x0065 => Button::Menu,
        0x0066 => Button::Power,
        0x0080 | 0x00E9 => Button::VolumeUp,
        0x0081 | 0x00EA => Button::VolumeDown,
        0x00F1 => Button::Back,
        _ => return None,
    })
}

pub fn parse_hid_usages(payload: &[u8]) -> BTreeSet<u16> {
    let data: &[u8] = if payload.len() == 9 && payload.starts_with(&[0x01, 0x00, 0x00]) {
        &payload[3..]
    } else if payload.len() == 7 && payload.first() == Some(&1) {
        &payload[1..]
    } else if matches!(payload.len(), 3 | 5) && payload.first().copied().unwrap_or(0xFF) <= 0x0F {
        &payload[1..]
    } else if payload.len() >= 2 && payload.len() % 2 == 0 {
        payload
    } else if payload.len() > 6 && (payload.len() - 1) % 2 == 0 && payload.first().copied().unwrap_or(0xFF) <= 0x0F
    {
        &payload[1..]
    } else {
        payload
    };
    let mut usages = BTreeSet::new();
    if data.is_empty() || data.len() % 2 != 0 {
        return usages;
    }
    for chunk in data.chunks_exact(2) {
        let usage = u16::from_le_bytes([chunk[0], chunk[1]]);
        if usage != 0 {
            usages.insert(usage);
        }
    }
    usages
}

#[derive(Default)]
pub struct HidTracker {
    baseline: bool,
    active: BTreeSet<u16>,
}

impl HidTracker {
    /// The first report only records what is already held, so connecting does
    /// not look like a burst of new presses.
    pub fn push(&mut self, payload: &[u8]) -> Vec<(Button, bool)> {
        let usages = parse_hid_usages(payload);
        if !self.baseline {
            self.baseline = true;
            self.active = usages;
            return Vec::new();
        }
        let mut edges = Vec::new();
        for usage in usages.difference(&self.active) {
            if let Some(button) = usage_to_button(*usage) {
                edges.push((button, true));
            }
        }
        for usage in self.active.difference(&usages) {
            if let Some(button) = usage_to_button(*usage) {
                edges.push((button, false));
            }
        }
        self.active = usages;
        edges
    }

    /// Raw Input only arrives when a report changes, so the first packet is a
    /// real press. GATT can still use [`push`] and ignore the opening snapshot.
    pub fn start_live(&mut self) {
        self.baseline = true;
        self.active.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn six_byte_report_and_ioctl_prefix_decode_the_same_up_key() {
        assert_eq!(
            parse_hid_usages(&hex("520000000000")),
            BTreeSet::from([0x0052])
        );
        assert_eq!(
            parse_hid_usages(&hex("010000520000000000")),
            BTreeSet::from([0x0052])
        );
    }

    #[test]
    fn first_report_is_a_baseline_and_the_next_press_is_an_edge() {
        let mut tracker = HidTracker::default();
        assert!(tracker.push(&hex("000000000000")).is_empty());
        assert_eq!(
            tracker.push(&hex("3e0000000000")),
            vec![(Button::Mic, true)]
        );
        assert_eq!(
            tracker.push(&hex("000000000000")),
            vec![(Button::Mic, false)]
        );
    }

    #[test]
    fn short_consumer_reports_decode_back_and_volume() {
        assert_eq!(parse_hid_usages(&[0x01, 0xF1, 0x00]), BTreeSet::from([0x00F1]));
        assert_eq!(parse_hid_usages(&[0x01, 0xE9, 0x00]), BTreeSet::from([0x00E9]));
        assert_eq!(parse_hid_usages(&[0x80, 0x00]), BTreeSet::from([0x0080]));
        let mut tracker = HidTracker::default();
        tracker.start_live();
        assert_eq!(
            tracker.push(&[0x01, 0xF1, 0x00]),
            vec![(Button::Back, true)]
        );
        assert_eq!(tracker.push(&[0x01, 0x00, 0x00]), vec![(Button::Back, false)]);
    }

    fn hex(text: &str) -> Vec<u8> {
        (0..text.len())
            .step_by(2)
            .map(|index| u8::from_str_radix(&text[index..index + 2], 16).unwrap())
            .collect()
    }
}
