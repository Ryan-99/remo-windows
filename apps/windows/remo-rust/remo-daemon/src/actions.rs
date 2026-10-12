use remo_core::Action;

pub trait ActionExecutor: Send {
    fn execute(&mut self, action: &Action) -> Result<(), String>;
    fn set_voice_held(&mut self, _held: bool) -> Result<(), String> {
        Ok(())
    }
}

pub struct SimulationExecutor;

impl ActionExecutor for SimulationExecutor {
    fn execute(&mut self, _action: &Action) -> Result<(), String> {
        Ok(())
    }
}

#[cfg(windows)]
pub struct WindowsExecutor {
    voice_hotkey: Vec<String>,
    voice_held: bool,
}

#[cfg(windows)]
impl WindowsExecutor {
    pub(crate) fn vk_for_name(name: &str) -> Option<u16> {
        Some(match name.trim().to_ascii_uppercase().as_str() {
            "CTRL" | "CONTROL" | "LCTRL" | "RCTRL" | "LEFT CTRL" | "RIGHT CTRL" => {
                if name.eq_ignore_ascii_case("rctrl") || name.eq_ignore_ascii_case("right ctrl") {
                    0xA3
                } else if name.eq_ignore_ascii_case("lctrl") || name.eq_ignore_ascii_case("left ctrl") {
                    0xA2
                } else {
                    0x11
                }
            }
            "ALT" | "LALT" | "LEFT ALT" => 0xA4,
            "RALT" | "RIGHT ALT" => 0xA5,
            "SHIFT" | "LSHIFT" | "RSHIFT" => 0x10,
            "WIN" | "META" | "CMD" | "LWIN" | "RWIN" => {
                if name.eq_ignore_ascii_case("rwin") { 0x5C } else { 0x5B }
            }
            "ENTER" | "RETURN" => 0x0D,
            "TAB" => 0x09,
            "ESC" | "ESCAPE" => 0x1B,
            "SPACE" => 0x20,
            "COMMA" | "OEM_COMMA" => 0xBC,
            "BACKSPACE" => 0x08,
            "VOLUME_MUTE" | "VOLUMEMUTE" => 0xAD,
            "PLAY_PAUSE" | "PLAYPAUSE" => 0xB3,
            "UP" => 0x26,
            "DOWN" => 0x28,
            "LEFT" => 0x25,
            "RIGHT" => 0x27,
            value if value.len() == 1 && value.as_bytes()[0].is_ascii_alphanumeric() => {
                value.as_bytes()[0] as u16
            }
            value if value.starts_with('F') => {
                let number = value[1..].parse::<u16>().ok()?;
                if !(1..=24).contains(&number) {
                    return None;
                }
                0x70 + number - 1
            }
            _ => return None,
        })
    }

    fn send_vk(vk: u16, key_up: bool) -> Result<(), String> {
        use windows_sys::Win32::UI::Input::KeyboardAndMouse::{
            SendInput, INPUT, INPUT_0, INPUT_KEYBOARD, KEYBDINPUT, KEYEVENTF_EXTENDEDKEY,
            KEYEVENTF_KEYUP,
        };
        let input = INPUT {
            r#type: INPUT_KEYBOARD,
            Anonymous: INPUT_0 {
                ki: KEYBDINPUT {
                    wVk: vk,
                    wScan: 0,
                    dwFlags: (if key_up { KEYEVENTF_KEYUP } else { 0 })
                        | if matches!(vk, 0x21..=0x28 | 0x2D..=0x2E | 0x5B..=0x5C | 0xA3 | 0xA5 | 0xAD..=0xAF)
                        {
                            KEYEVENTF_EXTENDEDKEY
                        } else {
                            0
                        },
                    time: 0,
                    dwExtraInfo: 0,
                },
            },
        };
        let sent = unsafe { SendInput(1, &input, std::mem::size_of::<INPUT>() as i32) };
        if sent == 1 {
            Ok(())
        } else {
            Err(format!("SendInput failed for vk=0x{vk:02X}: {}; UIPI may block injection into an elevated target", std::io::Error::last_os_error()))
        }
    }

    fn send_combo(keys: &[String]) -> Result<(), String> {
        let values: Vec<u16> = keys
            .iter()
            .filter_map(|key| Self::vk_for_name(key))
            .collect();
        if values.len() != keys.len() || values.is_empty() {
            return Err("unsupported or empty key combo".into());
        }
        send_balanced(
            &values,
            |vk| unsafe {
                windows_sys::Win32::UI::Input::KeyboardAndMouse::GetAsyncKeyState(vk as i32) < 0
            },
            Self::send_vk,
        )
    }
}

#[cfg(windows)]
impl ActionExecutor for WindowsExecutor {
    fn execute(&mut self, action: &Action) -> Result<(), String> {
        use windows_sys::Win32::UI::Input::KeyboardAndMouse::{VK_VOLUME_DOWN, VK_VOLUME_UP};
        match action {
            Action::Disabled => Ok(()),
            Action::KeyCombo { keys } => Self::send_combo(keys),
            Action::ArrowUp => Self::send_combo(&["UP".into()]),
            Action::ArrowDown => Self::send_combo(&["DOWN".into()]),
            Action::ArrowLeft => Self::send_combo(&["LEFT".into()]),
            Action::ArrowRight => Self::send_combo(&["RIGHT".into()]),
            Action::Return => Self::send_combo(&["ENTER".into()]),
            Action::DeleteBackward => Self::send_combo(&["BACKSPACE".into()]),
            Action::SystemVolumeUp => send_balanced(&[VK_VOLUME_UP], |_| false, Self::send_vk),
            Action::SystemVolumeDown => send_balanced(&[VK_VOLUME_DOWN], |_| false, Self::send_vk),
            Action::OpenApp { target } => open_app(target),
            Action::Voice => {
                if self.voice_hotkey.is_empty() {
                    Err("voice hotkey is empty".into())
                } else {
                    Self::send_combo(&self.voice_hotkey)
                }
            }
        }
    }

    fn set_voice_held(&mut self, held: bool) -> Result<(), String> {
        if self.voice_held == held {
            return Ok(());
        }
        let values: Vec<u16> = self
            .voice_hotkey
            .iter()
            .filter_map(|key| Self::vk_for_name(key))
            .collect();
        if values.len() != self.voice_hotkey.len() || values.is_empty() {
            return Err("voice hotkey is empty".into());
        }
        let result = if held {
            values.iter().try_for_each(|vk| Self::send_vk(*vk, false))
        } else {
            values.iter().rev().try_for_each(|vk| Self::send_vk(*vk, true))
        };
        if result.is_ok() {
            self.voice_held = held;
        }
        result
    }
}

/// Reject already-held keys; never release a key merely because the user is
/// holding it. On failure attempt *all* releases for our successful downs.
/// This is best effort: SendInput/UIPI can also reject cleanup, which is logged.
fn send_balanced(
    keys: &[u16],
    mut is_down: impl FnMut(u16) -> bool,
    mut send: impl FnMut(u16, bool) -> Result<(), String>,
) -> Result<(), String> {
    let mut unique = Vec::new();
    for &key in keys {
        if !unique.contains(&key) {
            unique.push(key);
        }
    }
    if unique.iter().any(|&key| is_down(key)) {
        return Err("shortcut conflicts with a currently held key; no input injected".into());
    }
    let mut owned = Vec::new();
    let mut errors = Vec::new();
    for key in unique {
        match send(key, false) {
            Ok(()) => owned.push(key),
            Err(error) => {
                errors.push(error);
                break;
            }
        }
    }
    for key in owned.into_iter().rev() {
        if let Err(error) = send(key, true) {
            errors.push(error);
        }
    }
    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors.join("; "))
    }
}

pub fn create_executor(execute: bool, voice_hotkey: Vec<String>) -> Box<dyn ActionExecutor> {
    #[cfg(windows)]
    if execute {
        return Box::new(WindowsExecutor {
            voice_hotkey,
            voice_held: false,
        });
    }
    let _ = (execute, voice_hotkey);
    Box::new(SimulationExecutor)
}

fn open_app(target: &str) -> Result<(), String> {
    let command = match target {
        "open_wechat" => "WeChat",
        "open_chrome" => "chrome",
        "open_edge" => "msedge",
        "open_cursor" => "Cursor",
        "open_codex" => "codex",
        "open_slack" => "slack",
        "open_zed" => "zed",
        other => other,
    };
    std::process::Command::new("cmd")
        .args(["/C", "start", "", command])
        .spawn()
        .map(|_| ())
        .map_err(|error| format!("open app failed: {error}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn failed_down_releases_previous_modifiers() {
        let mut events = Vec::new();
        let result = send_balanced(
            &[0x11, 0x5B, 0x41],
            |_| false,
            |vk, up| {
                events.push((vk, up));
                if vk == 0x41 && !up {
                    Err("failed".into())
                } else {
                    Ok(())
                }
            },
        );
        assert!(result.is_err());
        assert_eq!(
            events,
            vec![
                (0x11, false),
                (0x5B, false),
                (0x41, false),
                (0x5B, true),
                (0x11, true)
            ]
        );
    }
    #[test]
    fn failed_release_does_not_skip_other_releases() {
        let mut events = Vec::new();
        assert!(send_balanced(
            &[0x11, 0x41],
            |_| false,
            |vk, up| {
                events.push((vk, up));
                if vk == 0x41 && up {
                    Err("release failed".into())
                } else {
                    Ok(())
                }
            }
        )
        .is_err());
        assert_eq!(events.last(), Some(&(0x11, true)));
    }
    #[test]
    fn user_held_key_is_neither_injected_nor_released() {
        assert!(send_balanced(
            &[0x11, 0x41],
            |vk| vk == 0x11,
            |_, _| panic!("must not inject")
        )
        .is_err());
    }
    #[test]
    fn duplicate_modifiers_are_only_pressed_once() {
        let mut events = Vec::new();
        send_balanced(
            &[0x11, 0x11],
            |_| false,
            |vk, up| {
                events.push((vk, up));
                Ok(())
            },
        )
        .unwrap();
        assert_eq!(events, vec![(0x11, false), (0x11, true)]);
    }
    #[cfg(windows)]
    #[test]
    fn key_names_are_validated_without_overflow() {
        assert_eq!(WindowsExecutor::vk_for_name("Left Alt"), Some(0xA4));
        assert_eq!(WindowsExecutor::vk_for_name("Right Alt"), Some(0xA5));
        assert_eq!(WindowsExecutor::vk_for_name("F24"), Some(0x87));
        for value in ["F0", "F25", "F65535", "!", ""] {
            assert_eq!(WindowsExecutor::vk_for_name(value), None);
        }
    }
}
