//! Virtual HID keyboard used to wake input methods.
//!
//! Doubao ignores `SendInput` because those events are marked injected.
//! WinUHid submits a real keyboard report instead. Right Alt is modifier bit 0x40.

use std::path::PathBuf;

pub fn together_reports(vks: &[u16]) -> Result<Vec<Vec<u8>>, String> {
    let modifiers: Vec<u16> = vks.iter().copied().filter(|vk| modifier_bit(*vk).is_some()).collect();
    let keys: Vec<u16> = vks.iter().copied().filter(|vk| modifier_bit(*vk).is_none()).collect();
    if modifiers.len() > 1 && keys.is_empty() {
        let mut reports = Vec::new();
        for step in 1..=modifiers.len() {
            reports.push(keyboard_report(&modifiers[..step])?);
        }
        return Ok(reports);
    }
    let mut reports = Vec::new();
    if !keys.is_empty() {
        reports.push(keyboard_report(&keys)?);
    }
    reports.push(keyboard_report(vks)?);
    Ok(reports)
}

pub fn keyboard_report(vks: &[u16]) -> Result<Vec<u8>, String> {
    let mut modifier = 0u8;
    let mut usages = Vec::new();
    for vk in vks {
        if let Some(bit) = modifier_bit(*vk) {
            modifier |= bit;
            continue;
        }
        let usage = key_usage(*vk).ok_or_else(|| format!("虚拟键盘不支持按键 {vk:#04x}"))?;
        if !usages.contains(&usage) {
            usages.push(usage);
        }
    }
    if usages.len() > 6 {
        return Err("虚拟键盘一次最多六个普通键".into());
    }
    let mut report = vec![0u8; 8];
    report[0] = modifier;
    report[2..2 + usages.len()].copy_from_slice(&usages);
    Ok(report)
}

pub fn menu_mask_report(vks: &[u16]) -> Result<Vec<u8>, String> {
    let mut report = keyboard_report(vks)?;
    if report[2] == 0 {
        // F24 has no shortcut of its own. Its presence makes the following
        // Right Alt release a chord release, so Windows does not open the menu.
        report[2] = 0x73;
    }
    Ok(report)
}

fn modifier_bit(vk: u16) -> Option<u8> {
    Some(match vk {
        0x11 | 0xA2 => 0x01,
        0x10 | 0xA0 => 0x02,
        0x12 | 0xA4 => 0x04,
        0x5B => 0x08,
        0xA3 => 0x10,
        0xA1 => 0x20,
        0xA5 => 0x40,
        0x5C => 0x80,
        _ => return None,
    })
}

fn key_usage(vk: u16) -> Option<u8> {
    if modifier_bit(vk).is_some() {
        return None;
    }
    let fixed = match vk {
        0x08 => 0x2A,
        0x09 => 0x2B,
        0x0D => 0x28,
        0x1B => 0x29,
        0x20 => 0x2C,
        0xBC => 0x36,
        0x25 => 0x50,
        0x26 => 0x52,
        0x27 => 0x4F,
        0x28 => 0x51,
        _ => 0,
    };
    if fixed != 0 {
        return Some(fixed);
    }
    if (0x41..=0x5A).contains(&vk) {
        return Some((vk - 0x41 + 0x04) as u8);
    }
    if vk == 0x30 {
        return Some(0x27);
    }
    if (0x31..=0x39).contains(&vk) {
        return Some((vk - 0x31 + 0x1E) as u8);
    }
    None
}

#[cfg(windows)]
struct ReportSlot {
    bytes: std::sync::atomic::AtomicU64,
    complete: CompleteReadFn,
}

#[cfg(windows)]
pub struct Keyboard {
    keyboard: Device,
    _consumer: Device,
    slot: Box<ReportSlot>,
    held: bool,
}

#[cfg(windows)]
struct Device {
    handle: *mut core::ffi::c_void,
    destroy: DestroyFn,
    submit: SubmitFn,
    _descriptor: Vec<u8>,
    _instance: Vec<u16>,
}

#[cfg(windows)]
type CreateFn = unsafe extern "system" fn(*const DeviceConfig) -> *mut core::ffi::c_void;
#[cfg(windows)]
type EventFn = unsafe extern "system" fn(*mut core::ffi::c_void, *mut core::ffi::c_void, *const u8);
type StartFn = unsafe extern "system" fn(*mut core::ffi::c_void, Option<EventFn>, *mut core::ffi::c_void) -> i32;
type CompleteReadFn = unsafe extern "system" fn(*mut core::ffi::c_void, *const u8, *const u8, u32);
#[cfg(windows)]
type SubmitFn = unsafe extern "system" fn(*mut core::ffi::c_void, *const u8, u32) -> i32;
#[cfg(windows)]
type DestroyFn = unsafe extern "system" fn(*mut core::ffi::c_void);

#[cfg(windows)]
#[repr(C, packed)]
struct DeviceConfig {
    supported_events: i32,
    vendor_id: u16,
    product_id: u16,
    version_number: u16,
    report_descriptor_length: u16,
    report_descriptor: *const u8,
    container_id: [u8; 16],
    instance_id: *const u16,
    hardware_ids: *const u16,
    read_report_period_us: u32,
}

#[cfg(windows)]
const KEYBOARD_DESCRIPTOR: &[u8] = &[
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01,
    0x75, 0x01, 0x95, 0x08, 0x81, 0x02, 0x95, 0x01, 0x75, 0x08, 0x81, 0x01, 0x95, 0x05, 0x75, 0x01,
    0x05, 0x08, 0x19, 0x01, 0x29, 0x05, 0x91, 0x02, 0x95, 0x01, 0x75, 0x03, 0x91, 0x01, 0x95, 0x06,
    0x75, 0x08, 0x15, 0x00, 0x25, 0x73, 0x05, 0x07, 0x19, 0x00, 0x29, 0x73, 0x81, 0x00, 0xC0,
];

#[cfg(windows)]
impl Keyboard {
    pub fn open() -> Result<Self, String> {
        let path = dll_path().ok_or("没有找到 WinUHid.dll")?;
        let module = unsafe {
            windows_sys::Win32::System::LibraryLoader::LoadLibraryW(wide(path.as_os_str()).as_ptr())
        };
        if module.is_null() {
            return Err("WinUHid.dll 加载失败".into());
        }
        let get_version: unsafe extern "system" fn() -> u32 = unsafe { symbol(module, "WinUHidGetDriverInterfaceVersion")? };
        let create: CreateFn = unsafe { symbol(module, "WinUHidCreateDevice")? };
        let start: StartFn = unsafe { symbol(module, "WinUHidStartDevice")? };
        let submit: SubmitFn = unsafe { symbol(module, "WinUHidSubmitInputReport")? };
        let complete: CompleteReadFn = unsafe { symbol(module, "WinUHidCompleteReadEvent")? };
        let destroy: DestroyFn = unsafe { symbol(module, "WinUHidDestroyDevice")? };
        let version = unsafe { get_version() };
        if version < 1 {
            return Err(format!("WinUHid 驱动接口版本 {version} 不可用"));
        }
        let tag = std::process::id();
        let slot = Box::new(ReportSlot {
            bytes: std::sync::atomic::AtomicU64::new(0),
            complete,
        });
        let keyboard = create_device(
            create,
            start,
            submit,
            destroy,
            KEYBOARD_DESCRIPTOR,
            1,
            &format!("RemoKeyboard-{tag}"),
            8,
            Some(on_hid_event),
            &*slot as *const ReportSlot as *mut core::ffi::c_void,
            10_000,
        )?;
        let consumer = match create_device(
            create,
            start,
            submit,
            destroy,
            &[
                0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x15, 0x00, 0x26, 0xFF, 0x03, 0x19, 0x00, 0x2A,
                0xFF, 0x03, 0x75, 0x10, 0x95, 0x01, 0x81, 0x00, 0xC0,
            ],
            2,
            &format!("RemoConsumer-{tag}"),
            0,
            None,
            std::ptr::null_mut(),
            0,
        ) {
            Ok(device) => device,
            Err(error) => {
                destroy_device(&keyboard);
                return Err(error);
            }
        };
        let _ = submit_report(&consumer, &[0u8; 2]);
        let mut keyboard = Self {
            keyboard,
            _consumer: consumer,
            slot,
            held: false,
        };
        let _ = keyboard.publish(&[0u8; 8]);
        Ok(keyboard)
    }

    /// Left Alt goes down first, then Left Win joins it. Releasing both at
    /// once leaves WeChat's voice panel stuck.
    pub fn press_together(&mut self, vks: &[u16]) -> Result<(), String> {
        self.held = true;
        let reports = together_reports(vks)?;
        let gap = if vks.iter().all(|vk| modifier_bit(*vk).is_some()) { 80 } else { 10 };
        for (index, report) in reports.iter().enumerate() {
            if let Err(error) = self.publish(report) {
                let _ = self.release();
                return Err(error);
            }
            if index + 1 < reports.len() {
                std::thread::sleep(std::time::Duration::from_millis(gap));
            }
        }
        Ok(())
    }

    /// Lift the last key while the first modifier stays down, mask that
    /// remaining Alt so Windows does not open the menu, then clear.
    pub fn release_together(&mut self, vks: &[u16]) -> Result<(), String> {
        let modifiers: Vec<u16> = vks.iter().copied().filter(|vk| modifier_bit(*vk).is_some()).collect();
        let keys: Vec<u16> = vks.iter().copied().filter(|vk| modifier_bit(*vk).is_none()).collect();
        if modifiers.len() > 1 && keys.is_empty() {
            self.publish(&menu_mask_report(&modifiers[..modifiers.len() - 1])?)?;
            std::thread::sleep(std::time::Duration::from_millis(20));
            return self.release();
        }
        if !modifiers.is_empty() && !keys.is_empty() {
            self.publish(&keyboard_report(&modifiers)?)?;
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        self.release()
    }

    pub fn press(&mut self, vks: &[u16]) -> Result<(), String> {
        let modifiers: Vec<u16> = vks.iter().copied().filter(|vk| modifier_bit(*vk).is_some()).collect();
        let keys: Vec<u16> = vks.iter().copied().filter(|vk| modifier_bit(*vk).is_none()).collect();
        if modifiers.len() <= 1 {
            self.publish(&keyboard_report(vks)?)?;
        } else {
            for step in 1..=modifiers.len() {
                let mut partial = modifiers[..step].to_vec();
                partial.extend_from_slice(&keys);
                self.publish(&keyboard_report(&partial)?)?;
                if step < modifiers.len() {
                    // Two modifiers in one report arrive as a single event.
                    // WeChat matches a real keyboard: Right Alt is already
                    // down, then Right Ctrl follows about 120 ms later.
                    let gap = if keys.is_empty() { 120 } else { 80 };
                    std::thread::sleep(std::time::Duration::from_millis(gap));
                }
            }
        }
        self.held = true;
        Ok(())
    }

    pub fn release(&mut self) -> Result<(), String> {
        self.publish(&[0u8; 8])?;
        self.held = false;
        Ok(())
    }

    /// Lift a two-modifier chord one key at a time. Releasing both at once,
    /// or adding F24 while they are still down, leaves WeChat's voice panel
    /// stuck so the next press does not wake it.
    pub fn release_chord(&mut self, vks: &[u16]) -> Result<(), String> {
        let modifiers: Vec<u16> = vks.iter().copied().filter(|vk| modifier_bit(*vk).is_some()).collect();
        if modifiers.len() <= 1 {
            return self.release();
        }
        for end in (1..modifiers.len()).rev() {
            self.publish(&keyboard_report(&modifiers[..end])?)?;
            std::thread::sleep(std::time::Duration::from_millis(40));
        }
        self.release()
    }

    pub fn modifier_count(vks: &[u16]) -> usize {
        vks.iter().filter(|vk| modifier_bit(**vk).is_some()).count()
    }

    pub fn hold_menu_mask(&mut self, vks: &[u16]) -> Result<(), String> {
        self.publish(&menu_mask_report(vks)?)?;
        self.held = true;
        Ok(())
    }

    fn publish(&mut self, report: &[u8]) -> Result<(), String> {
        let mut bytes = [0u8; 8];
        let count = report.len().min(8);
        bytes[..count].copy_from_slice(&report[..count]);
        self.slot.bytes.store(u64::from_le_bytes(bytes), std::sync::atomic::Ordering::SeqCst);
        let _ = submit_report(&self.keyboard, &bytes);
        Ok(())
    }
}

#[cfg(windows)]
unsafe extern "system" fn on_hid_event(ctx: *mut core::ffi::c_void, device: *mut core::ffi::c_void, event: *const u8) {
    if ctx.is_null() || event.is_null() {
        return;
    }
    let kind = unsafe { event.cast::<i32>().read_unaligned() };
    if kind != 8 {
        return;
    }
    let slot = unsafe { &*ctx.cast::<ReportSlot>() };
    let report = slot.bytes.load(std::sync::atomic::Ordering::SeqCst).to_le_bytes();
    unsafe { (slot.complete)(device, event, report.as_ptr(), report.len() as u32) };
}

#[cfg(windows)]
impl Drop for Keyboard {
    fn drop(&mut self) {
        let _ = self.release();
        destroy_device(&self.keyboard);
        destroy_device(&self._consumer);
    }
}

#[cfg(windows)]
fn create_device(
    create: CreateFn,
    start: StartFn,
    submit: SubmitFn,
    destroy: DestroyFn,
    descriptor: &[u8],
    product_id: u16,
    instance: &str,
    events: i32,
    callback: Option<EventFn>,
    context: *mut core::ffi::c_void,
    period_us: u32,
) -> Result<Device, String> {
    let descriptor = descriptor.to_vec();
    let mut instance_wide: Vec<u16> = instance.encode_utf16().collect();
    instance_wide.push(0);
    let config = DeviceConfig {
        supported_events: events,
        vendor_id: 0x1234,
        product_id,
        version_number: 1,
        report_descriptor_length: descriptor.len() as u16,
        report_descriptor: descriptor.as_ptr(),
        container_id: [0; 16],
        instance_id: instance_wide.as_ptr(),
        hardware_ids: std::ptr::null(),
        read_report_period_us: period_us,
    };
    let handle = unsafe { create(&config) };
    if handle.is_null() {
        return Err(format!("WinUHidCreateDevice 失败：{instance}"));
    }
    let device = Device {
        handle,
        destroy,
        submit,
        _descriptor: descriptor,
        _instance: instance_wide,
    };
    if unsafe { start(handle, callback, context) } == 0 {
        destroy_device(&device);
        return Err(format!("WinUHidStartDevice 失败：{instance}"));
    }
    let _ = submit;
    Ok(device)
}

#[cfg(windows)]
fn submit_report(device: &Device, report: &[u8]) -> Result<(), String> {
    let ok = unsafe { (device.submit)(device.handle, report.as_ptr(), report.len() as u32) };
    if ok == 0 {
        Err("WinUHidSubmitInputReport 失败".into())
    } else {
        Ok(())
    }
}

#[cfg(windows)]
fn destroy_device(device: &Device) {
    unsafe { (device.destroy)(device.handle) };
}

#[cfg(windows)]
unsafe fn symbol<T>(module: *mut core::ffi::c_void, name: &str) -> Result<T, String> {
    let mut bytes = name.as_bytes().to_vec();
    bytes.push(0);
    let address = windows_sys::Win32::System::LibraryLoader::GetProcAddress(module, bytes.as_ptr());
    address
        .map(|address| std::mem::transmute_copy(&address))
        .ok_or_else(|| format!("WinUHid.dll 缺少 {name}"))
}

#[cfg(windows)]
fn dll_path() -> Option<PathBuf> {
    if let Some(path) = std::env::var_os("REMOTE_MIC_WINUHID_DLL") {
        let path = PathBuf::from(path);
        if path.is_file() {
            return Some(path);
        }
    }
    let mut candidates = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            candidates.push(dir.join("WinUHid.dll"));
            candidates.push(dir.join("winuhid").join("WinUHid.dll"));
            candidates.push(dir.join("_internal").join("winuhid").join("WinUHid.dll"));
        }
    }
    candidates.push(PathBuf::from(
        r"D:\code\remo\apps\windows\rc003\build\third_party\winuhid\WinUHid.dll",
    ));
    candidates.push(PathBuf::from(
        r"D:\Claude Code\codex\projects\remote-mic-windows\apps\windows\rc003\build\third_party\winuhid\WinUHid.dll",
    ));
    candidates.into_iter().find(|path| path.is_file())
}

#[cfg(windows)]
fn wide(value: &std::ffi::OsStr) -> Vec<u16> {
    use std::os::windows::ffi::OsStrExt;
    value.encode_wide().chain(std::iter::once(0)).collect()
}

#[cfg(test)]
mod tests {
    use super::keyboard_report;

    #[test]
    fn right_alt_is_the_doubao_modifier_bit() {
        assert_eq!(keyboard_report(&[0xA5]).unwrap(), vec![0x40, 0, 0, 0, 0, 0, 0, 0]);
    }

    #[test]
    fn left_ctrl_and_win_are_modifier_bits_only() {
        assert_eq!(keyboard_report(&[0xA2, 0x5B]).unwrap(), vec![0x09, 0, 0, 0, 0, 0, 0, 0]);
    }

    #[test]
    fn right_alt_and_comma_share_one_report() {
        assert_eq!(keyboard_report(&[0xA5, 0xBC]).unwrap(), vec![0x40, 0, 0x36, 0, 0, 0, 0, 0]);
    }

    #[test]
    fn wechat_chord_presses_left_alt_then_left_win() {
        let reports = super::together_reports(&[0xA4, 0x5B]).unwrap();
        assert_eq!(reports[0], vec![0x04, 0, 0, 0, 0, 0, 0, 0]);
        assert_eq!(reports[1], vec![0x0C, 0, 0, 0, 0, 0, 0, 0]);
    }

    #[test]
    fn right_alt_release_is_masked_with_f24() {
        assert_eq!(super::menu_mask_report(&[0xA5]).unwrap(), vec![0x40, 0, 0x73, 0, 0, 0, 0, 0]);
    }
}
