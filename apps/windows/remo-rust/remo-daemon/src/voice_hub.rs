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
    key_error: String,
    sendinput_hold: bool,
    held: bool,
}

pub fn start(
    backend: &str,
    mode: &str,
    hotkey: &[String],
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
    let (keys, key_rx) = mpsc::channel();
    let (ready_tx, ready_rx) = mpsc::channel();
    let allow_sendinput = backend != "winuhid";
    thread::spawn(move || key_thread(opener, key_rx, ready_tx));
    match ready_rx.recv_timeout(Duration::from_secs(5)) {
        Ok(Ok(())) => finished(Some(keys), chord, link, events, String::new(), false),
        Ok(Err(error)) => finished(None, chord, link, events, error, allow_sendinput),
        Err(_) => finished(
            None,
            chord,
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
            key_error: if sendinput_hold { String::new() } else { key_error },
            sendinput_hold,
            held: false,
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

    pub fn set_mic(&mut self, down: bool) -> Result<&'static str, String> {
        if self.held == down {
            return Ok(if self.keys.is_some() { "winuhid" } else { "sendinput" });
        }
        if down {
            crate::f5_guard::arm_down();
            self.link.press();
            if let Some(keys) = &self.keys {
                let chosen = active_ime_chord().unwrap_or_else(|| self.chord.clone());
                let _ = keys.send(KeyCommand::SetChord(chosen));
            }
        } else {
            crate::f5_guard::arm_up();
            self.link.release();
        }
        if let Some(keys) = &self.keys {
            let command = if down { KeyCommand::Down } else { KeyCommand::Up };
            keys.send(command).map_err(|_| "虚拟键盘线程已退出".to_string())?;
            self.held = down;
            return Ok("winuhid");
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

    pub fn set_chord(&mut self, keys: &[String]) {
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

fn active_ime_chord() -> Option<Vec<u16>> {
    let name = foreground_ime_name();
    if name.is_empty() {
        return None;
    }
    let folded = name.to_lowercase();
    // Doubao's own hook consumes a bare Right Alt even when WeChat is the
    // active input method, and then it cannot commit text. WeChat gets a
    // different physical chord.
    if name.contains("微信") || folded.contains("wetype") {
        return Some(vks(&[String::from("rctrl"), String::from("space")]));
    }
    if name.contains("豆包") || folded.contains("doubao") {
        return Some(vks(&[String::from("ralt")]));
    }
    None
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
    let mut down = false;
    let mut virtual_down = false;
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
                        let _ = keyboard.release();
                        virtual_down = false;
                        masking = false;
                        crate::f5_guard::set_chord(false);
                    }
                    opener = next;
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {}
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    let _ = keyboard.release();
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
            if saw_down && !virtual_down && !masking && !opener.is_empty() && keyboard.press(&opener).is_ok()
            {
                virtual_down = true;
                virtual_since = Instant::now();
                saw_down = false;
                crate::f5_guard::set_chord(true);
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
            let release_after = if yield_tail {
                Duration::ZERO
            } else {
                Duration::from_millis(100)
            };
            if !down && virtual_down && !masking && virtual_since.elapsed() >= release_after {
                if keyboard.hold_menu_mask(&opener).is_ok() {
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
                    masking = false;
                    crate::f5_guard::set_chord(false);
                }
            }
        }
    }
}
