//! Voice shortcut plus the ATVV microphone.
//!
//! The remote only holds a shortcut while the mic key is down. A tap that
//! opens an input method and then lets go is not used.

use std::sync::mpsc::{self, Receiver, Sender};
use std::thread;
use std::time::{Duration, Instant};

use super::Command;

enum KeyCommand {
    Down,
    Up,
    /// Another remote key arrived after the mic was released. End the chord
    /// tail so that key is not modified by the held shortcut.
    Yield,
    SetChord(Vec<u16>),
}

pub struct VoiceHub {
    keys: Option<Sender<KeyCommand>>,
    link: crate::voice_atvv::VoiceLink,
    chord: Vec<u16>,
    chord_label: String,
    preset: String,
    key_error: String,
    sendinput_hold: bool,
    held: bool,
    last_ime: String,
    last_chord: String,
}

pub fn start(
    backend: &str,
    mode: &str,
    hotkey: &[String],
    preset: &str,
    sustain: &[String],
    endpoint: String,
    gain_db: f32,
) -> (VoiceHub, Receiver<Command>) {
    crate::f5_guard::start();
    let (link, events) = crate::voice_atvv::start(endpoint, gain_db);
    let _mode = mode;
    let _sustain = sustain;
    let opener = vks(hotkey);
    let chord = opener.clone();
    let chord_label = hotkey.join("+");
    let preset = preset.to_string();
    let (keys, key_rx) = mpsc::channel();
    let (ready_tx, ready_rx) = mpsc::channel();
    let allow_sendinput = backend != "winuhid";
    thread::spawn(move || key_thread(opener, key_rx, ready_tx));
    match ready_rx.recv_timeout(Duration::from_secs(5)) {
        Ok(Ok(())) => finished(
            Some(keys),
            chord,
            chord_label,
            preset,
            link,
            events,
            String::new(),
            false,
        ),
        Ok(Err(error)) => finished(
            None,
            chord,
            chord_label,
            preset,
            link,
            events,
            error,
            allow_sendinput,
        ),
        Err(_) => finished(
            None,
            chord,
            chord_label,
            preset,
            link,
            events,
            "虚拟键盘打开超时".into(),
            allow_sendinput,
        ),
    }
}

fn finished(
    keys: Option<Sender<KeyCommand>>,
    chord: Vec<u16>,
    chord_label: String,
    preset: String,
    link: crate::voice_atvv::VoiceLink,
    events: Receiver<Command>,
    key_error: String,
    sendinput_hold: bool,
) -> (VoiceHub, Receiver<Command>) {
    (
        VoiceHub {
            keys,
            link,
            chord,
            chord_label,
            preset,
            key_error: if sendinput_hold { String::new() } else { key_error },
            sendinput_hold,
            held: false,
            last_ime: String::new(),
            last_chord: String::new(),
        },
        events,
    )
}

impl VoiceHub {
    pub fn keyboard_ready(&self) -> bool {
        self.keys.is_some()
    }

    pub fn keyboard_error(&self) -> &str {
        &self.key_error
    }

    pub fn last_ime(&self) -> &str {
        &self.last_ime
    }

    pub fn last_chord(&self) -> &str {
        &self.last_chord
    }

    pub fn set_mic(&mut self, down: bool) -> Result<&'static str, String> {
        if self.held == down {
            return Ok(if self.keys.is_some() { "winuhid" } else { "sendinput" });
        }
        if down {
            crate::f5_guard::arm_down();
            self.link.press();
            let ime = foreground_ime_name();
            let (label, chosen) = choose_chord(&ime, &self.chord, &self.chord_label, &self.preset);
            self.last_ime = ime;
            self.last_chord = label;
            if let Some(keys) = &self.keys {
                let _ = keys.send(KeyCommand::SetChord(chosen.clone()));
            }
            if let Some(keys) = &self.keys {
                keys.send(KeyCommand::Down)
                    .map_err(|_| "虚拟键盘线程已退出".to_string())?;
                self.held = true;
                return Ok(chord_backend(&self.last_chord));
            }
        } else {
            crate::f5_guard::arm_up();
            self.link.release();
            if let Some(keys) = &self.keys {
                keys.send(KeyCommand::Up)
                    .map_err(|_| "虚拟键盘线程已退出".to_string())?;
                self.held = false;
                return Ok(chord_backend(&self.last_chord));
            }
        }
        if self.sendinput_hold {
            self.held = down;
            return Ok("sendinput");
        }
        Err(if self.key_error.is_empty() {
            "WinUHid 虚拟键盘不可用".into()
        } else {
            self.key_error.clone()
        })
    }

    pub fn set_route(&mut self, keys: &[String], preset: &str) {
        self.chord_label = keys.join("+");
        self.preset = preset.to_string();
        self.set_chord(keys);
    }

    pub fn set_chord(&mut self, keys: &[String]) {
        self.chord_label = keys.join("+");
        self.chord = vks(keys);
        if let Some(sender) = &self.keys {
            let _ = sender.send(KeyCommand::SetChord(self.chord.clone()));
        }
    }

    pub fn other_key(&self) {
        if self.held {
            return;
        }
        if let Some(keys) = &self.keys {
            let _ = keys.send(KeyCommand::Yield);
        }
    }
}

fn wechat_chord() -> Vec<u16> {
    // Left Alt then Left Win. Right Alt is Doubao's own voice key.
    vks(&[String::from("lalt"), String::from("lwin")])
}

fn choose_chord(
    ime: &str,
    configured: &[u16],
    configured_label: &str,
    preset: &str,
) -> (String, Vec<u16>) {
    // 网易叭哥说 is a global hold on Left Alt, not a keyboard TIP.
    // 千问's default is Right Alt, which Doubao's hook consumes, so its
    // preset uses Right Ctrl instead.
    if preset == "bage" {
        return ("lalt".to_string(), vks(&[String::from("lalt")]));
    }
    if preset == "qianwen_ralt" {
        return ("rctrl".to_string(), vks(&[String::from("rctrl")]));
    }
    let folded = ime.to_lowercase();
    let wechat = folded.contains("wetype") || ime.contains("微信");
    let doubao = folded.contains("doubao") || ime.contains("豆包");
    let follow = preset == "wechat" || preset == "doubao" || preset.is_empty();
    if follow && doubao {
        return ("ralt".to_string(), vks(&[String::from("ralt")]));
    }
    if (follow && wechat) || preset == "wechat" {
        return ("lalt+lwin".to_string(), wechat_chord());
    }
    if preset == "doubao" {
        return ("ralt".to_string(), vks(&[String::from("ralt")]));
    }
    let label = if configured_label.is_empty() {
        "ralt".to_string()
    } else {
        configured_label.to_string()
    };
    (label, configured.to_vec())
}

fn duplicates_wechat_voice_on_paste(file_name: &str) -> bool {
    let name = file_name
        .rsplit(['\\', '/'])
        .next()
        .unwrap_or(file_name)
        .to_ascii_lowercase();
    matches!(name.as_str(), "wxwork.exe" | "wecom.exe")
}

#[cfg(windows)]
fn wecom_is_foreground() -> bool {
    duplicates_wechat_voice_on_paste(&foreground_executable_name())
}

#[cfg(not(windows))]
fn wecom_is_foreground() -> bool {
    false
}

#[cfg(windows)]
fn foreground_executable_name() -> String {
    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::Threading::{
        OpenProcess, QueryFullProcessImageNameW, PROCESS_QUERY_LIMITED_INFORMATION,
    };
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        GetForegroundWindow, GetWindowThreadProcessId,
    };
    unsafe {
        let hwnd = GetForegroundWindow();
        if hwnd.is_null() {
            return String::new();
        }
        let mut pid = 0u32;
        GetWindowThreadProcessId(hwnd, &mut pid);
        if pid == 0 {
            return String::new();
        }
        let process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if process.is_null() {
            return String::new();
        }
        let mut buffer = [0u16; 520];
        let mut length = buffer.len() as u32;
        let ok = QueryFullProcessImageNameW(process, 0, buffer.as_mut_ptr(), &mut length);
        CloseHandle(process);
        if ok == 0 {
            return String::new();
        }
        let end = (length as usize).min(buffer.len());
        String::from_utf16_lossy(&buffer[..end])
    }
}

fn foreground_ime_name() -> String {
    #[cfg(windows)]
    {
        let legacy = legacy_ime_name();
        let tip = active_tip_name();
        if legacy.is_empty() {
            return tip;
        }
        if tip.is_empty() {
            return legacy;
        }
        return format!("{legacy} {tip}");
    }
    #[cfg(not(windows))]
    String::new()
}

#[cfg(windows)]
fn legacy_ime_name() -> String {
    use windows_sys::Win32::UI::Input::Ime::ImmGetDescriptionW;
    use windows_sys::Win32::UI::Input::KeyboardAndMouse::GetKeyboardLayout;
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        GetForegroundWindow, GetWindowThreadProcessId,
    };
    unsafe {
        let hwnd = GetForegroundWindow();
        if hwnd.is_null() {
            return String::new();
        }
        let thread = GetWindowThreadProcessId(hwnd, std::ptr::null_mut());
        let layout = GetKeyboardLayout(thread);
        let mut buffer = [0u16; 128];
        let count = ImmGetDescriptionW(layout, buffer.as_mut_ptr(), buffer.len() as u32);
        if count == 0 {
            return String::new();
        }
        let end = (count as usize).min(buffer.len());
        String::from_utf16_lossy(&buffer[..end])
    }
}

#[cfg(windows)]
fn active_tip_name() -> String {
    use windows::core::Interface;
    use windows::Win32::System::Com::{
        CoCreateInstance, CoInitializeEx, CLSCTX_INPROC_SERVER, COINIT_MULTITHREADED,
    };
    use windows::Win32::UI::TextServices::{
        CLSID_TF_InputProcessorProfiles, GUID_TFCAT_TIP_KEYBOARD, ITfInputProcessorProfileMgr,
        ITfInputProcessorProfiles, TF_INPUTPROCESSORPROFILE,
    };
    unsafe {
        let _ = CoInitializeEx(None, COINIT_MULTITHREADED);
        let manager: ITfInputProcessorProfileMgr = match CoCreateInstance(
            &CLSID_TF_InputProcessorProfiles,
            None,
            CLSCTX_INPROC_SERVER,
        ) {
            Ok(manager) => manager,
            Err(_) => return String::new(),
        };
        let mut profile = TF_INPUTPROCESSORPROFILE::default();
        if manager
            .GetActiveProfile(&GUID_TFCAT_TIP_KEYBOARD, &mut profile)
            .is_err()
        {
            return String::new();
        }
        let profiles: ITfInputProcessorProfiles = match manager.cast() {
            Ok(profiles) => profiles,
            Err(_) => return String::new(),
        };
        let mut described = profiles
            .GetLanguageProfileDescription(&profile.clsid, profile.langid, &profile.guidProfile)
            .map(|text| text.to_string())
            .unwrap_or_default();
        // ImmGetDescription is empty for these two. The text-service id is
        // what Windows actually reports as the active keyboard.
        const WETYPE: windows::core::GUID =
            windows::core::GUID::from_u128(0x86598fb9_66a2_463e_b9c2_aeb906d477ad);
        const DOUBAO_IME: windows::core::GUID =
            windows::core::GUID::from_u128(0x9d2b2e2b_3c93_4d2f_9d35_6eeb85f0d2b0);
        if profile.clsid == WETYPE {
            described.push_str(" wetype");
        } else if profile.clsid == DOUBAO_IME {
            described.push_str(" doubao");
        }
        described
    }
}

#[cfg(windows)]
fn activate_wechat_for_session() {
    use windows::Win32::System::Com::{
        CoCreateInstance, CoInitializeEx, CLSCTX_INPROC_SERVER, COINIT_MULTITHREADED,
    };
    use windows::Win32::UI::TextServices::{
        CLSID_TF_InputProcessorProfiles, GUID_TFCAT_TIP_KEYBOARD, ITfInputProcessorProfileMgr,
        TF_INPUTPROCESSORPROFILE, TF_IPPMF_DONTCARECURRENTINPUTLANGUAGE, TF_IPPMF_FORSESSION,
        TF_PROFILETYPE_INPUTPROCESSOR,
    };
    const WETYPE: windows::core::GUID =
        windows::core::GUID::from_u128(0x86598fb9_66a2_463e_b9c2_aeb906d477ad);
    unsafe {
        let _ = CoInitializeEx(None, COINIT_MULTITHREADED);
        let manager: ITfInputProcessorProfileMgr = match CoCreateInstance(
            &CLSID_TF_InputProcessorProfiles,
            None,
            CLSCTX_INPROC_SERVER,
        ) {
            Ok(manager) => manager,
            Err(_) => return,
        };
        let mut profile = TF_INPUTPROCESSORPROFILE::default();
        if manager
            .GetActiveProfile(&GUID_TFCAT_TIP_KEYBOARD, &mut profile)
            .is_err()
            || profile.clsid != WETYPE
        {
            return;
        }
        // Thread-local activation still reports WeType, but its voice sink
        // stays quiet until the profile is active for the whole desktop.
        let _ = manager.ActivateProfile(
            TF_PROFILETYPE_INPUTPROCESSOR,
            profile.langid,
            &profile.clsid,
            &profile.guidProfile,
            profile.hkl,
            TF_IPPMF_FORSESSION | TF_IPPMF_DONTCARECURRENTINPUTLANGUAGE,
        );
    }
}

#[cfg(not(windows))]
fn activate_wechat_for_session() {}

fn chord_backend(label: &str) -> &'static str {
    if label.contains("lalt") && label.contains("lwin") {
        "scancode"
    } else if label.contains("lalt") || label.contains("comma") || !label.contains('+') {
        "winuhid"
    } else {
        "scancode"
    }
}

fn is_wechat_chord(vks: &[u16]) -> bool {
    vks.contains(&0xA4) && vks.contains(&0x5B)
}

fn uses_scancode(vks: &[u16]) -> bool {
    !is_wechat_chord(vks) && crate::winuhid::Keyboard::modifier_count(vks) > 1
}

/// WeChat stores left and right modifiers as different scan codes. A HID
/// modifier bit is not that event, so Doubao can see it and WeChat cannot.
/// RemoteMapper wakes WeType by sending each key as an extended scan code,
/// with SendInput and keybd_event, and a short gap between them.
fn wechat_send(vk: u16, down: bool) {
    use windows_sys::Win32::UI::Input::KeyboardAndMouse::{
        keybd_event, MapVirtualKeyW, SendInput, INPUT, INPUT_0, INPUT_KEYBOARD, KEYBDINPUT,
        KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE, MAPVK_VK_TO_VSC,
    };
    let scan = unsafe { MapVirtualKeyW(vk as u32, MAPVK_VK_TO_VSC) } as u16;
    let extended = matches!(vk, 0x21..=0x28 | 0x2D..=0x2E | 0x5B..=0x5C | 0xA3 | 0xA5);
    let mut flags = KEYEVENTF_SCANCODE;
    if extended {
        flags |= KEYEVENTF_EXTENDEDKEY;
    }
    if !down {
        flags |= KEYEVENTF_KEYUP;
    }
    let input = INPUT {
        r#type: INPUT_KEYBOARD,
        Anonymous: INPUT_0 {
            ki: KEYBDINPUT {
                wVk: vk,
                wScan: scan,
                dwFlags: flags,
                time: 0,
                dwExtraInfo: 0,
            },
        },
    };
    unsafe {
        let _ = SendInput(1, &input, std::mem::size_of::<INPUT>() as i32);
        keybd_event(vk as u8, scan as u8, flags, 0);
    }
}

fn press_wechat_scan(vks: &[u16]) -> bool {
    if vks.is_empty() {
        return false;
    }
    activate_wechat_for_session();
    // RemoteMapper: a live WH_KEYBOARD_LL hook serializes the remote's F5
    // repeat with the injected chord, and WeType never sees a clean pair.
    crate::f5_guard::suspend();
    wechat_send(0x74, false);
    for vk in vks.iter().rev() {
        wechat_send(*vk, false);
    }
    thread::sleep(Duration::from_millis(50));
    for (index, vk) in vks.iter().enumerate() {
        wechat_send(*vk, true);
        if index + 1 < vks.len() {
            thread::sleep(Duration::from_millis(40));
        }
    }
    crate::f5_guard::resume();
    true
}

fn release_wechat_scan(vks: &[u16]) {
    // WeCom already shows the live sentence. WeType then injects Ctrl+V
    // with the same text. Arm the swallow before the hook comes back.
    if wecom_is_foreground() {
        crate::f5_guard::arm_swallow_injected_paste(4_000);
    }
    crate::f5_guard::suspend();
    let keys: Vec<u16> = vks.iter().rev().copied().collect();
    for (index, vk) in keys.iter().enumerate() {
        wechat_send(*vk, false);
        if index + 1 < keys.len() {
            thread::sleep(Duration::from_millis(20));
        }
    }
    crate::f5_guard::resume();
}

fn wechat_hold(vks: &[u16]) -> bool {
    if vks.is_empty() {
        return false;
    }
    // The remote's mic key arrives as F5 and would sit inside the chord.
    wechat_send(0x74, false);
    for vk in vks.iter().rev() {
        wechat_send(*vk, false);
    }
    thread::sleep(Duration::from_millis(50));
    for (index, vk) in vks.iter().enumerate() {
        wechat_send(*vk, true);
        if index + 1 < vks.len() {
            thread::sleep(Duration::from_millis(40));
        }
    }
    true
}

fn release_latched_keys() {
    for vk in [0xA5u16, 0xA4, 0xA3, 0xA2, 0x12, 0x11, 0x10, 0xBC, 0x5B, 0x5C] {
        wechat_send(vk, false);
    }
}

fn wechat_release(vks: &[u16]) {
    let keys: Vec<u16> = vks.iter().rev().copied().collect();
    for (index, vk) in keys.iter().enumerate() {
        wechat_send(*vk, false);
        if index + 1 < keys.len() {
            thread::sleep(Duration::from_millis(20));
        }
    }
}

fn vks(names: &[String]) -> Vec<u16> {
    names
        .iter()
        .filter_map(|name| crate::actions::WindowsExecutor::vk_for_name(name))
        .collect()
}

fn key_thread(
    mut opener: Vec<u16>,
    rx: Receiver<KeyCommand>,
    ready: Sender<Result<(), String>>,
) {
    let mut keyboard = match crate::winuhid::Keyboard::open() {
        Ok(keyboard) => keyboard,
        Err(error) => {
            let _ = ready.send(Err(error));
            return;
        }
    };
    let _ = ready.send(Ok(()));
    release_latched_keys();
    let _ = keyboard.release();
    let mut down = false;
    let mut virtual_down = false;
    let mut scan_held = false;
    let mut virtual_since = Instant::now();
    let mut saw_down = false;
    let mut yield_tail = false;
    let mut masking = false;
    let mut mask_since = Instant::now();
    loop {
        let incoming = rx.recv_timeout(Duration::from_millis(20));
        {
            match incoming {
                Ok(KeyCommand::Down) => {
                    down = true;
                    saw_down = true;
                    yield_tail = false;
                }
                Ok(KeyCommand::Up) => down = false,
                Ok(KeyCommand::Yield) => yield_tail = true,
                Ok(KeyCommand::SetChord(next)) => {
                    if virtual_down || masking {
                        if scan_held {
                            wechat_release(&opener);
                            scan_held = false;
                        } else if is_wechat_chord(&opener) {
                            release_wechat_scan(&opener);
                            let _ = keyboard.release();
                        } else {
                            let _ = keyboard.release();
                        }
                        virtual_down = false;
                        masking = false;
                        crate::f5_guard::set_chord(false);
                    }
                    opener = next;
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {}
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    if scan_held {
                        wechat_release(&opener);
                    } else if is_wechat_chord(&opener) {
                        release_wechat_scan(&opener);
                        let _ = keyboard.release();
                    } else {
                        let _ = keyboard.release();
                    }
                    crate::f5_guard::set_chord(false);
                    break;
                }
            }
            while let Ok(command) = rx.try_recv() {
                match command {
                    KeyCommand::Down => {
                        down = true;
                        saw_down = true;
                        yield_tail = false;
                    }
                    KeyCommand::Up => down = false,
                    KeyCommand::Yield => yield_tail = true,
                    KeyCommand::SetChord(next) => opener = next,
                }
            }
            // Keep one Right Alt hold across a short mic bounce so Doubao sees
            // a single edge. A later remote key cancels that tail immediately.
            let scan = uses_scancode(&opener);
            let wechat = is_wechat_chord(&opener);
            if saw_down && !virtual_down && !masking && !opener.is_empty() {
                // WeChat 2.1 drops injected scan codes. One real HID report
                // carries Right Alt and Comma together, without that mark.
                let pressed = if wechat {
                    press_wechat_scan(&opener)
                } else if scan {
                    wechat_hold(&opener)
                } else {
                    keyboard.press(&opener).is_ok()
                };
                if pressed {
                    virtual_down = true;
                    scan_held = scan;
                    virtual_since = Instant::now();
                    saw_down = false;
                    crate::f5_guard::set_chord(true);
                }
            } else if virtual_down || opener.is_empty() {
                saw_down = false;
            }
            if down {
                yield_tail = false;
                if masking {
                    if keyboard.press(&opener).is_ok() {
                        masking = false;
                        virtual_down = true;
                        crate::f5_guard::set_chord(true);
                    }
                }
            }
            let multi = crate::winuhid::Keyboard::modifier_count(&opener) > 1;
            let release_after = if yield_tail || wechat || multi {
                Duration::ZERO
            } else {
                Duration::from_millis(100)
            };
            if !down && virtual_down && !masking && virtual_since.elapsed() >= release_after {
                if wechat {
                    release_wechat_scan(&opener);
                    let _ = keyboard.release();
                    scan_held = false;
                    virtual_down = false;
                    crate::f5_guard::set_chord(false);
                } else if scan_held {
                    wechat_release(&opener);
                    scan_held = false;
                    virtual_down = false;
                    crate::f5_guard::set_chord(false);
                } else if multi {
                    let _ = keyboard.release_chord(&opener);
                    virtual_down = false;
                    crate::f5_guard::set_chord(false);
                } else if keyboard.hold_menu_mask(&opener).is_ok() {
                    masking = true;
                    mask_since = Instant::now();
                    virtual_down = false;
                } else {
                    let _ = keyboard.release();
                    virtual_down = false;
                    crate::f5_guard::set_chord(false);
                }
                yield_tail = false;
            }
            if masking && (down || mask_since.elapsed() >= Duration::from_millis(20)) {
                if down && keyboard.press(&opener).is_ok() {
                    masking = false;
                    virtual_down = true;
                    virtual_since = Instant::now();
                    crate::f5_guard::set_chord(true);
                } else if !down {
                    let _ = keyboard.release();
                    release_latched_keys();
                    masking = false;
                    crate::f5_guard::set_chord(false);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{choose_chord, duplicates_wechat_voice_on_paste};

    #[test]
    fn bage_keeps_left_alt_while_wechat_is_the_keyboard() {
        let (label, keys) = choose_chord("微信输入法 wetype", &[0xA4, 0x5B], "lalt+lwin", "bage");
        assert_eq!(label, "lalt");
        assert_eq!(keys, vec![0xA4]);
    }

    #[test]
    fn qianwen_uses_right_ctrl_while_doubao_is_the_keyboard() {
        let (label, keys) = choose_chord("豆包输入法 doubao", &[0xA5], "ralt", "qianwen_ralt");
        assert_eq!(label, "rctrl");
        assert_eq!(keys, vec![0xA3]);
    }

    #[test]
    fn wecom_is_the_only_host_whose_second_paste_is_swallowed() {
        assert!(duplicates_wechat_voice_on_paste(
            r"C:\Program Files\WXWork\WXWork.exe"
        ));
        assert!(duplicates_wechat_voice_on_paste("WeCom.exe"));
        assert!(!duplicates_wechat_voice_on_paste("Weixin.exe"));
        assert!(!duplicates_wechat_voice_on_paste("notepad.exe"));
    }

    #[test]
    fn wechat_preset_still_follows_doubao() {
        let (label, keys) = choose_chord("豆包输入法 doubao", &[0xA4, 0x5B], "lalt+lwin", "wechat");
        assert_eq!(label, "ralt");
        assert_eq!(keys, vec![0xA5]);
    }

    #[test]
    fn saved_left_alt_is_not_replaced_by_right_alt() {
        let (label, keys) = choose_chord("微软拼音", &[0xA4], "lalt", "custom");
        assert_eq!(label, "lalt");
        assert_eq!(keys, vec![0xA4]);
    }
}
