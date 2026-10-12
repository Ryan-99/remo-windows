//! Swallow the RC003 microphone key's translated F5, and the few remote keys
//! that become a different command while the voice chord is held.
//!
//! The F5 guard is armed by a Frida mic edge. The low-level hook runs before
//! that edge, so an F5 that is not armed yet waits briefly for it. An ordinary
//! keyboard F5 is left alone when the guard does not arm. Arrows and Enter are
//! never swallowed: those are the remote's normal navigation keys.

use std::sync::atomic::{AtomicBool, AtomicPtr, AtomicU32, AtomicU64, Ordering};
use std::sync::OnceLock;
use std::time::Instant;

static START: OnceLock<Instant> = OnceLock::new();
static GUARD_UNTIL_MS: AtomicU64 = AtomicU64::new(0);
static CHORD_DOWN: AtomicBool = AtomicBool::new(false);
static HOOK: AtomicPtr<std::ffi::c_void> = AtomicPtr::new(std::ptr::null_mut());
static PUMP_TID: AtomicU32 = AtomicU32::new(0);
const WM_APP_REHOOK: u32 = 0x8000;

fn mono_ms() -> u64 {
    START
        .get_or_init(Instant::now)
        .elapsed()
        .as_millis() as u64
}

pub fn arm_down() {
    GUARD_UNTIL_MS.store(mono_ms().saturating_add(30_000), Ordering::SeqCst);
}

pub fn arm_up() {
    GUARD_UNTIL_MS.store(mono_ms().saturating_add(300), Ordering::SeqCst);
}

fn armed() -> bool {
    mono_ms() < GUARD_UNTIL_MS.load(Ordering::SeqCst)
}

pub fn set_chord(down: bool) {
    CHORD_DOWN.store(down, Ordering::SeqCst);
}

/// Drop the low-level hook before a voice chord is injected. While it is
/// installed, every remote F5 is marshaled onto this thread and the chord
/// no longer arrives as one clean pair.
pub fn suspend() {
    let hook = HOOK.swap(std::ptr::null_mut(), Ordering::SeqCst);
    if hook.is_null() {
        return;
    }
    unsafe {
        use windows_sys::Win32::UI::WindowsAndMessaging::UnhookWindowsHookEx;
        let _ = UnhookWindowsHookEx(hook);
    }
}

/// Ask the message-pump thread to install the hook again. SetWindowsHookEx
/// only stays alive on the thread that pumps messages.
pub fn resume() {
    let tid = PUMP_TID.load(Ordering::SeqCst);
    if tid == 0 {
        return;
    }
    unsafe {
        use windows_sys::Win32::UI::WindowsAndMessaging::PostThreadMessageW;
        let _ = PostThreadMessageW(tid, WM_APP_REHOOK, 0, 0);
    }
}

fn chord_down() -> bool {
    CHORD_DOWN.load(Ordering::SeqCst)
}

pub(crate) fn should_swallow(vk: u32, injected: bool, f5_armed: bool, chord: bool) -> bool {
    if injected {
        return false;
    }
    if vk == 0x74 && (f5_armed || chord) {
        return true;
    }
    // These translations belong to the remote, not a normal typing keyboard.
    // Leaving them through while Right Alt is down turns the mic key into
    // Sleep, the menu key, or a backtick.
    chord && matches!(vk, 0x5D | 0x5F | 0xC0)
}

#[cfg(windows)]
pub fn start() {
    std::thread::spawn(|| unsafe {
        use windows_sys::Win32::System::Threading::GetCurrentThreadId;
        use windows_sys::Win32::UI::WindowsAndMessaging::{
            CallNextHookEx, GetMessageW, HHOOK, KBDLLHOOKSTRUCT, MSG,
        };
        unsafe extern "system" fn proc(
            code: i32,
            wparam: usize,
            lparam: isize,
        ) -> isize {
            if code >= 0 {
                let event = &*(lparam as *const KBDLLHOOKSTRUCT);
                let injected = event.flags & 0x10 != 0;
                if event.vkCode == 0x74 && !injected && !armed() && !chord_down() {
                    let started = mono_ms();
                    while mono_ms().saturating_sub(started) < 35 && !armed() && !chord_down() {
                        std::thread::sleep(std::time::Duration::from_millis(1));
                    }
                }
                if should_swallow(event.vkCode, injected, armed(), chord_down()) {
                    return 1;
                }
            }
            CallNextHookEx(HHOOK::default(), code, wparam, lparam)
        }
        unsafe fn install() {
            use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
            use windows_sys::Win32::UI::WindowsAndMessaging::{
                SetWindowsHookExW, WH_KEYBOARD_LL,
            };
            if !HOOK.load(Ordering::SeqCst).is_null() {
                return;
            }
            let hook = SetWindowsHookExW(
                WH_KEYBOARD_LL,
                Some(proc),
                GetModuleHandleW(std::ptr::null()),
                0,
            );
            if hook.is_null() {
                eprintln!("f5 guard hook failed");
                return;
            }
            HOOK.store(hook, Ordering::SeqCst);
        }
        PUMP_TID.store(GetCurrentThreadId(), Ordering::SeqCst);
        install();
        let mut message = std::mem::zeroed::<MSG>();
        while GetMessageW(&mut message, std::ptr::null_mut(), 0, 0) > 0 {
            if message.message == WM_APP_REHOOK {
                install();
            }
        }
    });
}

#[cfg(not(windows))]
pub fn start() {}

#[cfg(test)]
mod tests {
    use super::should_swallow;

    #[test]
    fn remote_f5_is_swallowed_only_while_the_mic_guard_or_chord_is_active() {
        assert!(should_swallow(0x74, false, true, false));
        assert!(should_swallow(0x74, false, false, true));
        assert!(!should_swallow(0x74, false, false, false));
        assert!(!should_swallow(0x74, true, true, true));
    }

    #[test]
    fn voice_chord_swallows_remote_only_keys_and_leaves_navigation_alone() {
        assert!(should_swallow(0x5F, false, false, true));
        assert!(should_swallow(0x5D, false, false, true));
        assert!(should_swallow(0xC0, false, false, true));
        assert!(!should_swallow(0x25, false, false, true));
        assert!(!should_swallow(0x0D, false, false, true));
        assert!(!should_swallow(0x5F, false, false, false));
    }
}
