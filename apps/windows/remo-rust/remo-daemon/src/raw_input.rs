//! Device-scoped Raw Input for the paired RC003.
//!
//! Windows already owns the BLE HID service, so GATT open is denied. Raw
//! Input still delivers that remote's keys. Events are accepted only when the
//! source device path carries the RC003 vendor/product id or its Bluetooth
//! address, so the laptop keyboard is ignored.

use crate::Command;
use remo_core::{Button, Edge, InputEvent};
use std::sync::mpsc::SyncSender;
use std::time::Instant;

pub fn listen(tx: &SyncSender<Command>, address: u64) -> Result<(), String> {
    #[cfg(windows)]
    {
        listen_windows(tx, address)
    }
    #[cfg(not(windows))]
    {
        let _ = (tx, address);
        Err("系统按键通道仅支持 Windows".into())
    }
}

pub fn split_hid_reports(body: &[u8]) -> Vec<&[u8]> {
    if body.len() < 8 {
        return Vec::new();
    }
    let size = u32::from_le_bytes([body[0], body[1], body[2], body[3]]) as usize;
    let count = u32::from_le_bytes([body[4], body[5], body[6], body[7]]) as usize;
    let end = 8usize.saturating_add(size.saturating_mul(count));
    if size == 0 || count == 0 || body.len() < end {
        return vec![&body[8..]];
    }
    (0..count)
        .map(|index| &body[8 + index * size..8 + (index + 1) * size])
        .collect()
}

pub fn path_matches_remote(path: &str, address_hex: &str) -> bool {
    let low = path.to_ascii_lowercase();
    let classic = low.contains("vid_2717") && low.contains("pid_32b8");
    let ble = (low.contains("dev_vid&012717") || low.contains("dev_vid&002717"))
        && low.contains("pid&32b8");
    let address = !address_hex.is_empty()
        && address_hex != "000000000000"
        && low.contains(address_hex);
    classic || ble || address
}

pub fn button_from_keyboard(make_code: u16, flags: u16, vkey: u16) -> Option<(Button, bool)> {
    let pressed = flags & 0x0001 == 0;
    let button = match vkey {
        0x74 => Button::Mic,
        0x27 => Button::Right,
        0x25 => Button::Left,
        0x28 => Button::Down,
        0x26 => Button::Up,
        0x0D => Button::Ok,
        0x24 => Button::Home,
        0x5D => Button::Menu,
        0xC0 => Button::Tv,
        0x5F => Button::Power,
        0xAF => Button::VolumeUp,
        0xAE => Button::VolumeDown,
        0xA6 => Button::Back,
        0x7C => Button::VolumeUp,
        0x7D => Button::VolumeDown,
        0x7E => Button::Back,
        0x00 | 0xFF => match make_code {
            0x5E => Button::Power,
            0x6A => Button::Back,
            0x30 => Button::VolumeUp,
            0x2E => Button::VolumeDown,
            _ => return None,
        },
        _ => return None,
    };
    Some((button, pressed))
}

#[cfg(windows)]
fn listen_windows(tx: &SyncSender<Command>, address: u64) -> Result<(), String> {
    use std::sync::Mutex;
    use windows_sys::Win32::Foundation::{HWND, LPARAM, LRESULT, WPARAM};
    use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
    use windows_sys::Win32::UI::Input::{
        GetRawInputData, GetRawInputDeviceInfoW, RegisterRawInputDevices, HRAWINPUT,
        RAWINPUTDEVICE, RAWINPUTHEADER, RIDEV_INPUTSINK, RIDI_DEVICENAME, RID_INPUT, RIM_TYPEHID,
        RIM_TYPEKEYBOARD,
    };
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        CreateWindowExW, DefWindowProcW, DispatchMessageW, GetMessageW, RegisterClassW,
        TranslateMessage, HWND_MESSAGE, MSG, WM_INPUT, WNDCLASSW,
    };

    struct Slot {
        tx: SyncSender<Command>,
        address_hex: String,
        tracker: crate::hid_usage::HidTracker,
    }
    static SLOT: Mutex<Option<Slot>> = Mutex::new(None);

    let address_hex = format!("{address:012X}").to_ascii_lowercase();
    {
        let mut slot = SLOT.lock().map_err(|error| error.to_string())?;
        *slot = Some(Slot {
            tx: tx.clone(),
            address_hex,
            tracker: {
            let mut tracker = crate::hid_usage::HidTracker::default();
            tracker.start_live();
            tracker
        },
        });
    }

    unsafe extern "system" fn wnd_proc(
        hwnd: HWND,
        msg: u32,
        wparam: WPARAM,
        lparam: LPARAM,
    ) -> LRESULT {
        if msg == WM_INPUT {
            handle_input(lparam as HRAWINPUT);
            return 0;
        }
        DefWindowProcW(hwnd, msg, wparam, lparam)
    }

    unsafe fn handle_input(input: HRAWINPUT) {
        let mut size = 0u32;
        GetRawInputData(
            input,
            RID_INPUT,
            std::ptr::null_mut(),
            &mut size,
            std::mem::size_of::<RAWINPUTHEADER>() as u32,
        );
        if size == 0 {
            return;
        }
        let mut buffer = vec![0u8; size as usize];
        let written = GetRawInputData(
            input,
            RID_INPUT,
            buffer.as_mut_ptr().cast(),
            &mut size,
            std::mem::size_of::<RAWINPUTHEADER>() as u32,
        );
        if written == u32::MAX || written == 0 {
            return;
        }
        let header = std::ptr::read_unaligned(buffer.as_ptr().cast::<RAWINPUTHEADER>());
        let path = device_path(header.hDevice as isize);
        let Ok(mut slot) = SLOT.lock() else {
            return;
        };
        let Some(slot) = slot.as_mut() else {
            return;
        };
        if !path_matches_remote(&path, &slot.address_hex) {
            return;
        }
        let body = &buffer[std::mem::size_of::<RAWINPUTHEADER>()..];
        let mut edges = Vec::new();
        if header.dwType == RIM_TYPEKEYBOARD && body.len() >= 8 {
            let make_code = u16::from_le_bytes([body[0], body[1]]);
            let flags = u16::from_le_bytes([body[2], body[3]]);
            let vkey = u16::from_le_bytes([body[6], body[7]]);
            if let Some(edge) = button_from_keyboard(make_code, flags, vkey) {
                edges.push(edge);
            }
        } else if header.dwType == RIM_TYPEHID && body.len() >= 8 {
            for report in split_hid_reports(body) {
                edges.extend(slot.tracker.push(report));
            }
        }
        for (button, pressed) in edges {
            let _ = slot.tx.try_send(Command::Input(InputEvent {
                button,
                edge: if pressed { Edge::Down } else { Edge::Up },
                at: Instant::now(),
            }));
        }
    }

    unsafe fn device_path(device: isize) -> String {
        let mut chars = 0u32;
        GetRawInputDeviceInfoW(device as _, RIDI_DEVICENAME, std::ptr::null_mut(), &mut chars);
        if chars == 0 {
            return String::new();
        }
        let mut wide = vec![0u16; chars as usize];
        let written = GetRawInputDeviceInfoW(
            device as _,
            RIDI_DEVICENAME,
            wide.as_mut_ptr().cast(),
            &mut chars,
        );
        if written == u32::MAX || written == 0 {
            return String::new();
        }
        let end = wide.iter().position(|unit| *unit == 0).unwrap_or(wide.len());
        String::from_utf16_lossy(&wide[..end])
    }

    unsafe {
        let class_name: Vec<u16> = "RemoRawInput\0".encode_utf16().collect();
        let instance = GetModuleHandleW(std::ptr::null());
        let class = WNDCLASSW {
            lpfnWndProc: Some(wnd_proc),
            hInstance: instance,
            lpszClassName: class_name.as_ptr(),
            ..std::mem::zeroed()
        };
        if RegisterClassW(&class) == 0 {
            return Err("无法注册按键监听窗口".into());
        }
        let hwnd = CreateWindowExW(
            0,
            class_name.as_ptr(),
            class_name.as_ptr(),
            0,
            0,
            0,
            0,
            0,
            HWND_MESSAGE,
            std::ptr::null_mut(),
            instance,
            std::ptr::null_mut(),
        );
        if hwnd.is_null() {
            return Err("无法创建按键监听窗口".into());
        }
        let devices = [
            RAWINPUTDEVICE {
                usUsagePage: 0x01,
                usUsage: 0x06,
                dwFlags: RIDEV_INPUTSINK,
                hwndTarget: hwnd,
            },
            RAWINPUTDEVICE {
                usUsagePage: 0x0C,
                usUsage: 0x01,
                dwFlags: RIDEV_INPUTSINK,
                hwndTarget: hwnd,
            },
        ];
        if RegisterRawInputDevices(devices.as_ptr(), devices.len() as u32, std::mem::size_of::<RAWINPUTDEVICE>() as u32) == 0
        {
            return Err("无法注册系统按键通道".into());
        }
        let _ = tx.send(Command::Diagnostic(serde_json::json!({
            "type": "ble_transport_connected",
            "name": "小米蓝牙语音遥控器",
        })));
        let mut message: MSG = std::mem::zeroed();
        while GetMessageW(&mut message, std::ptr::null_mut(), 0, 0) > 0 {
            TranslateMessage(&message);
            DispatchMessageW(&message);
        }
    }
    Err("系统按键通道已停止".into())
}

#[cfg(test)]
mod tests {
    use super::{button_from_keyboard, path_matches_remote};
    use remo_core::Button;

    #[test]
    fn only_the_rc003_path_is_accepted() {
        assert!(path_matches_remote(
            r"\\?\HID#VID_2717&PID_32B8&MI_00#x",
            "c05d39c2c8fb"
        ));
        assert!(path_matches_remote(
            r"\\?\HID#{00001812}_DEV_VID&012717_PID&32B8_C05D39C2C8FB",
            "c05d39c2c8fb"
        ));
        assert!(!path_matches_remote(
            r"\\?\HID#VID_046D&PID_C53F&MI_00#keyboard",
            "c05d39c2c8fb"
        ));
    }

    #[test]
    fn observed_remote_virtual_keys_map_to_buttons() {
        assert_eq!(
            button_from_keyboard(0, 0, 0x74),
            Some((Button::Mic, true))
        );
        assert_eq!(
            button_from_keyboard(0, 1, 0x0D),
            Some((Button::Ok, false))
        );
        assert_eq!(
            button_from_keyboard(0x6A, 0, 0xFF),
            Some((Button::Back, true))
        );
        assert_eq!(button_from_keyboard(0, 0, 0x41), None);
        assert_eq!(
            button_from_keyboard(0x30, 2, 0x00),
            Some((Button::VolumeUp, true))
        );
        assert_eq!(
            button_from_keyboard(0x6A, 2, 0xFF),
            Some((Button::Back, true))
        );
    }

    #[test]
    fn rawhid_short_report_is_split_from_the_header() {
        let body = [3, 0, 0, 0, 1, 0, 0, 0, 0x01, 0xF1, 0x00];
        assert_eq!(super::split_hid_reports(&body), vec![&[0x01, 0xF1, 0x00][..]]);
    }
}
