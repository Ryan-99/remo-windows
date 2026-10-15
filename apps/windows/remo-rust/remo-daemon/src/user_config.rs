//! Read the settings app's `config.json` and `key_bindings.json`.

use remo_core::{Action, Button, Mapping, Trigger};
use serde_json::Value;
use std::fs;
use std::path::Path;
use std::time::SystemTime;

use crate::bridge_status::config_root;

pub struct UserConfig {
    pub mapping: Mapping,
    pub voice_hotkey: Vec<String>,
    pub voice_preset: String,
    #[allow(dead_code)]
    pub voice_hold: bool,
    pub voice_mode: String,
    pub voice_sustain: Vec<String>,
    pub voice_backend: String,
    pub output_endpoint_name: String,
    pub gain_db: f32,
}

pub fn load_mapping_file() -> Option<Mapping> {
    read_mapping(&config_root().join("key_bindings.json"))
}

pub fn config_stamp() -> Option<SystemTime> {
    fs::metadata(config_root().join("config.json"))
        .and_then(|meta| meta.modified())
        .ok()
}

pub fn mapping_stamp() -> Option<SystemTime> {
    fs::metadata(config_root().join("key_bindings.json"))
        .and_then(|meta| meta.modified())
        .ok()
}

pub fn load() -> UserConfig {
    let root = config_root();
    let config_path = root.join("config.json");
    let voice_hotkey = hold_chord(&config_path);
    let voice_preset = read_string(&config_path, "voice_input_preset");
    let voice_mode = "hold".to_string();
    let voice_hold = true;
    let voice_sustain = Vec::<String>::new();
    let voice_backend = read_voice_backend(&config_path);
    let output_endpoint_name = read_string(&config_path, "output_endpoint_name");
    let gain_db = read_gain(&config_path);
    let mapping = read_mapping(&root.join("key_bindings.json")).unwrap_or_else(default_mapping);
    UserConfig {
        mapping,
        voice_hotkey,
        voice_preset,
        voice_hold,
        voice_mode,
        voice_sustain,
        voice_backend,
        output_endpoint_name,
        gain_db,
    }
}

fn read_config(path: &Path) -> Option<Value> {
    let text = fs::read_to_string(path).ok()?;
    serde_json::from_str(&text).ok()
}

fn read_voice_backend(path: &Path) -> String {
    read_config(path)
        .and_then(|value| value["voice_output_backend"].as_str().map(str::to_string))
        .filter(|value| matches!(value.as_str(), "winuhid" | "auto" | "sendinput"))
        .unwrap_or_else(|| "winuhid".into())
}

fn read_string(path: &Path, key: &str) -> String {
    read_config(path)
        .and_then(|value| value[key].as_str().map(str::to_string))
        .unwrap_or_default()
}

fn read_gain(path: &Path) -> f32 {
    read_config(path)
        .and_then(|value| value["gain_db"].as_f64())
        .map(|value| value as f32)
        .filter(|value| value.is_finite())
        .unwrap_or(10.0)
}

fn default_mapping() -> Mapping {
    let mut mapping = Mapping::default();
    mapping.set(Button::Ok, Trigger::Single, Action::Return);
    mapping.set(Button::Mic, Trigger::Long, Action::Voice);
    mapping.set(Button::VolumeUp, Trigger::Single, Action::SystemVolumeUp);
    mapping.set(
        Button::VolumeDown,
        Trigger::Single,
        Action::SystemVolumeDown,
    );
    mapping
}

fn hold_chord(path: &Path) -> Vec<String> {
    // Tap-once voice mode is retired. Whatever chord is saved is held for
    // the whole utterance; a leftover toggle mode does not select another key.
    read_voice_hotkey(path)
}

fn read_voice_hotkey(path: &Path) -> Vec<String> {
    let Ok(text) = fs::read_to_string(path) else {
        return vec!["lctrl".into(), "win".into()];
    };
    let Ok(value) = serde_json::from_str::<Value>(&text) else {
        return vec!["lctrl".into(), "win".into()];
    };
    split_hotkey(value["voice_hotkey"].as_str().unwrap_or("lctrl+win"))
}

fn split_hotkey(text: &str) -> Vec<String> {
    text.split('+')
        .map(str::trim)
        .filter(|part| !part.is_empty())
        .map(str::to_string)
        .collect()
}

fn read_mapping(path: &Path) -> Option<Mapping> {
    let text = fs::read_to_string(path).ok()?;
    let value: Value = serde_json::from_str(&text).ok()?;
    let bindings = value.get("bindings")?.as_object()?;
    let mut mapping = Mapping::default();
    for (name, action) in bindings {
        let Some(button) = button_from_name(name) else {
            continue;
        };
        mapping.set(button, Trigger::Single, action_from(action));
    }
    if let Some(secondary) = value.get("secondary_bindings").and_then(Value::as_object) {
        for (name, gestures) in secondary {
            let Some(button) = button_from_name(name) else {
                continue;
            };
            let Some(gestures) = gestures.as_object() else {
                continue;
            };
            if let Some(action) = gestures.get("double_click") {
                mapping.set(button, Trigger::Double, action_from(action));
            }
            if let Some(action) = gestures.get("long_press") {
                mapping.set(button, Trigger::Long, action_from(action));
            }
        }
    }
    Some(mapping)
}

fn button_from_name(name: &str) -> Option<Button> {
    Some(match name {
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

fn action_from(value: &Value) -> Action {
    let kind = value["kind"].as_str().unwrap_or("disabled");
    let keys = value["keys"]
        .as_array()
        .map(|items| {
            items
                .iter()
                .filter_map(|item| item.as_str().map(str::to_string))
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    match kind {
        "disabled" => Action::Disabled,
        "voice" => Action::Voice,
        "key_combo" => Action::KeyCombo { keys },
        "return" => Action::Return,
        "arrow_up" => Action::ArrowUp,
        "arrow_down" => Action::ArrowDown,
        "arrow_left" => Action::ArrowLeft,
        "arrow_right" => Action::ArrowRight,
        "delete_backward" => Action::DeleteBackward,
        "system_volume_up" => Action::SystemVolumeUp,
        "system_volume_down" => Action::SystemVolumeDown,
        "escape" => Action::KeyCombo {
            keys: vec!["escape".into()],
        },
        "show_desktop" => Action::KeyCombo {
            keys: vec!["win".into(), "d".into()],
        },
        "context_menu" => Action::KeyCombo {
            keys: vec!["shift".into(), "f10".into()],
        },
        "app_switcher" => Action::KeyCombo {
            keys: vec!["alt".into(), "tab".into()],
        },
        "system_volume_mute" => Action::KeyCombo {
            keys: vec!["volume_mute".into()],
        },
        "play_pause" => Action::KeyCombo {
            keys: vec!["play_pause".into()],
        },
        other if other.starts_with("open_") => Action::OpenApp {
            target: other.to_string(),
        },
        _ if !keys.is_empty() => Action::KeyCombo { keys },
        _ => Action::Disabled,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use remo_core::Trigger;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn saved_single_double_and_voice_hotkey_round_trip() {
        let dir = std::env::temp_dir().join(format!(
            "remo-config-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("config.json"), r#"{"voice_hotkey":"lctrl+win"}"#).unwrap();
        fs::write(
            dir.join("key_bindings.json"),
            r#"{"bindings":{"power":{"kind":"key_combo","keys":["lctrl","win"]},"ok":{"kind":"return","keys":[]}},"secondary_bindings":{"up":{"double_click":{"kind":"arrow_up","keys":[]},"long_press":{"kind":"disabled","keys":[]}}}}"#,
        )
        .unwrap();
        let hotkey = read_voice_hotkey(&dir.join("config.json"));
        let mapping = read_mapping(&dir.join("key_bindings.json")).unwrap();
        assert_eq!(hotkey, vec!["lctrl", "win"]);
        assert_eq!(
            mapping.resolve(Button::Power, Trigger::Single),
            Action::KeyCombo {
                keys: vec!["lctrl".into(), "win".into()]
            }
        );
        assert_eq!(mapping.resolve(Button::Ok, Trigger::Single), Action::Return);
        assert_eq!(
            mapping.resolve(Button::Up, Trigger::Double),
            Action::ArrowUp
        );
        assert_eq!(
            mapping.resolve(Button::Up, Trigger::Long),
            Action::Disabled
        );
        let _ = fs::remove_dir_all(dir);
    }
}
