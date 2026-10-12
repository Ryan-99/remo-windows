//! Start the same RC003 Frida HID tap the Python bridge uses.
//!
//! Windows' keyboard class drops Back and the volume keys. The verified
//! gadget reads the 6-byte usage report inside WUDFHost, which is the
//! production source for every button.

use std::path::PathBuf;
use std::process::Command;
use std::thread;

fn python_exe() -> Result<PathBuf, String> {
    let bundled = PathBuf::from(
        r"D:\Claude Code\codex\projects\remote-mic-windows\apps\windows\rc003\.venv-build-312\Scripts\python.exe",
    );
    if bundled.is_file() {
        return Ok(bundled);
    }
    Err("找不到用于注入遥控器 HID 通道的 Python".into())
}

fn python_path() -> &'static str {
    r"D:\code\remo\apps\windows\rc003\src"
}

pub fn host_pid() -> Result<u32, String> {
    let output = Command::new(python_exe()?)
        .env("PYTHONPATH", python_path())
        .args([
            "-c",
            "from ovb_rc003.frida_hid_tap_runtime import find_rc003_hidogatt_host_pid; print(find_rc003_hidogatt_host_pid() or 0)",
        ])
        .output()
        .map_err(|error| error.to_string())?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).trim().to_string());
    }
    let text = String::from_utf8_lossy(&output.stdout);
    let pid = text
        .trim()
        .lines()
        .last()
        .unwrap_or("0")
        .trim()
        .parse::<u32>()
        .map_err(|error| error.to_string())?;
    if pid == 0 {
        return Err("没有找到 RC003 的 WUDFHost".into());
    }
    Ok(pid)
}

pub fn spawn_inject(pid: u32) {
    thread::spawn(move || {
        let python = match python_exe() {
            Ok(python) => python,
            Err(error) => {
                eprintln!("frida inject skipped: {error}");
                return;
            }
        };
        match Command::new(&python)
            .env("PYTHONPATH", python_path())
            .args([
                "-m",
                "ovb_rc003.frida_hid_tap_injector",
                "--pid",
                &pid.to_string(),
            ])
            .output()
        {
            Ok(output) if output.status.success() => {}
            Ok(output) => {
                let stderr = String::from_utf8_lossy(&output.stderr);
                let text = stderr.trim();
                if needs_elevation(text) {
                    eprintln!("frida inject needs administrator, requesting elevation");
                    if let Err(error) = inject_elevated(&python, pid) {
                        eprintln!("frida inject failed: {error}");
                    }
                } else {
                    eprintln!("frida inject failed: {text}");
                }
            }
            Err(error) => eprintln!("frida inject failed: {error}"),
        }
    });
}

fn needs_elevation(stderr: &str) -> bool {
    let text = stderr.to_ascii_lowercase();
    text.contains("sedebugprivilege")
        || text.contains("permissionerror")
        || text.contains("administrator")
        || text.contains("access is denied")
}

fn inject_elevated(python: &std::path::Path, pid: u32) -> Result<(), String> {
    let script = inject_script(pid)?;
    let file = wide(python.to_string_lossy().as_ref());
    let parameters = wide(&format!("\"{}\"", script.display()));
    let operation = wide("runas");
    let code = unsafe {
        windows_sys::Win32::UI::Shell::ShellExecuteW(
            std::ptr::null_mut(),
            operation.as_ptr(),
            file.as_ptr(),
            parameters.as_ptr(),
            std::ptr::null(),
            0,
        )
    };
    if (code as isize) <= 32 {
        return Err(format!("elevation request was not accepted ({code:?})"));
    }
    Ok(())
}

fn inject_script(pid: u32) -> Result<PathBuf, String> {
    let directory = PathBuf::from(r"D:\code\remo\artifacts\ble-probe");
    std::fs::create_dir_all(&directory).map_err(|error| error.to_string())?;
    let script = directory.join("frida-inject.py");
    let body = format!(
        r#"import sys, traceback
sys.path.insert(0, r"{source}")
log_path = r"{directory}\frida-inject.log"
try:
    from ovb_rc003.frida_hid_tap_injector import main
    code = main(["--pid", "{pid}"])
    open(log_path, "w", encoding="utf-8").write("exit %s\n" % code)
    raise SystemExit(code)
except SystemExit:
    raise
except Exception:
    open(log_path, "w", encoding="utf-8").write(traceback.format_exc())
    raise SystemExit(1)
"#,
        source = python_path(),
        directory = directory.display(),
        pid = pid,
    );
    std::fs::write(&script, body).map_err(|error| error.to_string())?;
    Ok(script)
}

fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(std::iter::once(0)).collect()
}
