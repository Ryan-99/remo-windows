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
            Action::OpenApp { target, path } => open_app(target, path),
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

fn open_app(target: &str, path: &str) -> Result<(), String> {
    if let Some(existing) = existing_file(path) {
        if is_executable(&existing) && focus_running(&existing) {
            return Ok(());
        }
        return shell_open(&existing);
    }
    let names = search_names(target);
    if let Some(shortcut) = find_named_shortcut(&start_menu_roots(), &names) {
        return shell_open(&shortcut);
    }
    Err("没有找到这个应用".into())
}

fn search_names(target: &str) -> Vec<&str> {
    match target {
        "open_wechat" => vec!["微信", "WeChat", "Weixin"],
        "open_wecom" => vec!["企业微信", "WeCom", "WXWork"],
        "open_chrome" => vec!["Google Chrome", "Chrome"],
        "open_edge" => vec!["Microsoft Edge", "Edge"],
        "open_netease_music" => vec!["网易云音乐", "NetEase Cloud Music"],
        "open_cursor" => vec!["Cursor"],
        "open_slack" => vec!["Slack"],
        "open_codex" => vec!["Codex"],
        "open_claude" => vec!["Claude"],
        "open_zed" => vec!["Zed"],
        "open_cmux" => vec!["cmux"],
        "open_remote_mic" => vec!["Remo"],
        other => vec![other],
    }
}

fn existing_file(path: &str) -> Option<std::path::PathBuf> {
    let trimmed = path.trim();
    if trimmed.is_empty() {
        return None;
    }
    let path = std::path::PathBuf::from(trimmed);
    path.is_file().then_some(path)
}

fn is_executable(path: &std::path::Path) -> bool {
    path.extension()
        .and_then(|ext| ext.to_str())
        .is_some_and(|ext| ext.eq_ignore_ascii_case("exe"))
}

pub fn find_named_shortcut(roots: &[std::path::PathBuf], names: &[&str]) -> Option<std::path::PathBuf> {
    let wanted: Vec<String> = names
        .iter()
        .map(|name| name.trim().to_lowercase())
        .filter(|name| !name.is_empty())
        .collect();
    if wanted.is_empty() {
        return None;
    }
    let mut best: Option<(usize, std::path::PathBuf)> = None;
    for root in roots {
        visit_shortcuts(root, 0, &wanted, &mut best);
    }
    best.map(|(_, path)| path)
}

fn visit_shortcuts(
    dir: &std::path::Path,
    depth: usize,
    wanted: &[String],
    best: &mut Option<(usize, std::path::PathBuf)>,
) {
    if depth > 6 {
        return;
    }
    let entries = match std::fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(_) => return,
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            visit_shortcuts(&path, depth + 1, wanted, best);
            continue;
        }
        if !path
            .extension()
            .and_then(|ext| ext.to_str())
            .is_some_and(|ext| ext.eq_ignore_ascii_case("lnk"))
        {
            continue;
        }
        let Some(stem) = path.file_stem().and_then(|stem| stem.to_str()) else {
            continue;
        };
        let folded = stem.to_lowercase();
        let Some(rank) = wanted.iter().position(|name| name == &folded) else {
            continue;
        };
        if best.as_ref().is_none_or(|(current, _)| rank < *current) {
            *best = Some((rank, path));
        }
    }
}

fn start_menu_roots() -> Vec<std::path::PathBuf> {
    ["APPDATA", "PROGRAMDATA"]
        .into_iter()
        .filter_map(|key| std::env::var_os(key))
        .map(|value| {
            std::path::PathBuf::from(value)
                .join("Microsoft")
                .join("Windows")
                .join("Start Menu")
                .join("Programs")
        })
        .filter(|path| path.is_dir())
        .collect()
}

fn shell_open(path: &std::path::Path) -> Result<(), String> {
    #[cfg(windows)]
    {
        let file = wide_path(path);
        let operation = wide("open");
        let result = unsafe {
            windows_sys::Win32::UI::Shell::ShellExecuteW(
                std::ptr::null_mut(),
                operation.as_ptr(),
                file.as_ptr(),
                std::ptr::null(),
                std::ptr::null(),
                1,
            )
        };
        if (result as isize) > 32 {
            Ok(())
        } else {
            Err("打开应用失败".into())
        }
    }
    #[cfg(not(windows))]
    {
        let _ = path;
        Err("打开应用失败".into())
    }
}

fn focus_running(path: &std::path::Path) -> bool {
    #[cfg(windows)]
    {
        focus_executable(path)
    }
    #[cfg(not(windows))]
    {
        let _ = path;
        false
    }
}

#[cfg(windows)]
fn focus_executable(path: &std::path::Path) -> bool {
    use windows_sys::Win32::Foundation::{FALSE, TRUE};
    use windows_sys::Win32::System::Threading::{
        AttachThreadInput, GetCurrentThreadId, OpenProcess, QueryFullProcessImageNameW,
        PROCESS_QUERY_LIMITED_INFORMATION,
    };
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        EnumWindows, GetForegroundWindow, GetWindowTextLengthW, GetWindowThreadProcessId,
        IsIconic, IsWindowVisible, SetForegroundWindow, ShowWindow, SW_RESTORE,
    };
    let wanted = normalize_path(path);
    if wanted.is_empty() {
        return false;
    }
    struct Search {
        wanted: String,
        hwnd: *mut core::ffi::c_void,
    }
    unsafe extern "system" fn visit(hwnd: *mut core::ffi::c_void, param: isize) -> i32 {
        let search = &mut *(param as *mut Search);
        if unsafe { IsWindowVisible(hwnd) } == FALSE || unsafe { GetWindowTextLengthW(hwnd) } == 0 {
            return TRUE;
        }
        let mut pid = 0u32;
        unsafe { GetWindowThreadProcessId(hwnd, &mut pid) };
        if pid == 0 {
            return TRUE;
        }
        let process = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid) };
        if process.is_null() {
            return TRUE;
        }
        let mut buffer = [0u16; 1024];
        let mut length = buffer.len() as u32;
        let ok = unsafe {
            QueryFullProcessImageNameW(process, 0, buffer.as_mut_ptr(), &mut length)
        };
        unsafe { windows_sys::Win32::Foundation::CloseHandle(process) };
        if ok == FALSE {
            return TRUE;
        }
        let image = String::from_utf16_lossy(&buffer[..length as usize]);
        if normalize_text(&image) == search.wanted {
            search.hwnd = hwnd;
            return FALSE;
        }
        TRUE
    }
    let mut search = Search {
        wanted,
        hwnd: std::ptr::null_mut(),
    };
    unsafe { EnumWindows(Some(visit), &mut search as *mut Search as isize) };
    if search.hwnd.is_null() {
        return false;
    }
    unsafe {
        if IsIconic(search.hwnd) == TRUE {
            ShowWindow(search.hwnd, SW_RESTORE);
        }
        let foreground = GetForegroundWindow();
        let foreground_thread = GetWindowThreadProcessId(foreground, std::ptr::null_mut());
        let current = GetCurrentThreadId();
        let attached = foreground_thread != 0
            && foreground_thread != current
            && AttachThreadInput(current, foreground_thread, TRUE) == TRUE;
        let raised = SetForegroundWindow(search.hwnd) == TRUE;
        if attached {
            AttachThreadInput(current, foreground_thread, FALSE);
        }
        raised
    }
}

#[cfg(windows)]
fn normalize_path(path: &std::path::Path) -> String {
    let text = path.to_string_lossy();
    normalize_text(&text)
}

#[cfg(windows)]
fn normalize_text(text: &str) -> String {
    text.trim()
        .trim_start_matches(r"\\?\")
        .replace('/', "\\")
        .to_lowercase()
}

#[cfg(windows)]
fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(std::iter::once(0)).collect()
}

#[cfg(windows)]
fn wide_path(path: &std::path::Path) -> Vec<u16> {
    wide(&path.to_string_lossy())
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
    #[test]
    fn an_exact_shortcut_name_beats_a_longer_lookalike() {
        let dir = std::env::temp_dir().join(format!("remo-apps-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("nested")).unwrap();
        std::fs::write(dir.join("微信输入法.lnk"), b"x").unwrap();
        std::fs::write(dir.join("nested").join("微信.lnk"), b"x").unwrap();
        let found = find_named_shortcut(&[dir.clone()], &["微信"]).unwrap();
        assert_eq!(found.file_stem().unwrap(), "微信");
        let _ = std::fs::remove_dir_all(dir);
    }
}
