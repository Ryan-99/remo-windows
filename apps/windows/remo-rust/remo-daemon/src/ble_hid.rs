//! Paired-device BLE HID subscription for the RC003.
//!
//! This is the testable input path: Windows already has the remote paired,
//! so the daemon subscribes to HID reports and turns usage edges into the
//! same button events the settings window polls.

use crate::hid_usage::HidTracker;
use remo_core::{Edge, InputEvent};
use std::sync::mpsc::{self, Receiver, SyncSender};
use std::thread;
use std::time::{Duration, Instant};

use super::Command;

pub fn spawn() -> Receiver<Command> {
    let (tx, rx) = mpsc::sync_channel(256);
    thread::spawn(move || run(tx));
    rx
}

fn run(tx: SyncSender<Command>) {
    loop {
        match session(&tx) {
            Ok(()) => {}
            Err(error) => {
                let _ = tx.send(Command::Diagnostic(serde_json::json!({
                    "type": "ble_transport_waiting",
                    "error": error,
                })));
            }
        }
        if tx.send(Command::Diagnostic(serde_json::json!({
            "type": "ble_transport_waiting",
            "error": "正在重新查找遥控器",
        }))).is_err() {
            return;
        }
        thread::sleep(Duration::from_secs(3));
    }
}

#[cfg(windows)]
fn wait_async<T>(
    op: windows::Foundation::IAsyncOperation<T>,
    timeout: Duration,
) -> Result<T, String>
where
    T: windows::core::RuntimeType + 'static,
{
    use windows::Foundation::AsyncStatus;
    let started = Instant::now();
    loop {
        match op.Status() {
            Ok(AsyncStatus::Started) if started.elapsed() < timeout => {
                thread::sleep(Duration::from_millis(40));
            }
            Ok(AsyncStatus::Started) => {
                let _ = op.Cancel();
                return Err("蓝牙操作超时".into());
            }
            Ok(_) => return op.GetResults().map_err(|error| error.to_string()),
            Err(error) => return Err(error.to_string()),
        }
    }
}

#[cfg(windows)]
fn hid_services(
    device: &windows::Devices::Bluetooth::BluetoothLEDevice,
    hid_uuid: windows::core::GUID,
) -> Result<
    windows::Devices::Bluetooth::GenericAttributeProfile::GattDeviceServicesResult,
    String,
> {
    use windows::Devices::Bluetooth::BluetoothCacheMode;
    use windows::Devices::Bluetooth::GenericAttributeProfile::GattCommunicationStatus;
    let mut last_error = "没有 HID 服务".to_string();
    for mode in [BluetoothCacheMode::Cached, BluetoothCacheMode::Uncached] {
        match device
            .GetGattServicesForUuidWithCacheModeAsync(hid_uuid, mode)
            .map_err(|error| error.to_string())
            .and_then(|op| wait_async(op, Duration::from_secs(6)))
        {
            Ok(services) => {
                let count = services
                    .Services()
                    .ok()
                    .and_then(|list| list.Size().ok())
                    .unwrap_or(0);
                if services.Status().ok() == Some(GattCommunicationStatus::Success) && count > 0 {
                    return Ok(services);
                }
                last_error = format!("没有 HID 服务 ({mode:?})");
            }
            Err(error) => last_error = error,
        }
    }
    Err(last_error)
}

#[cfg(windows)]
#[allow(dead_code)]
fn os_hid_session(tx: &SyncSender<Command>, address: u64) -> Result<(), String> {
    use windows::core::HSTRING;
    use windows::Devices::Enumeration::DeviceInformation;
    use windows::Devices::HumanInterfaceDevice::{HidDevice, HidInputReportReceivedEventArgs};
    use windows::Foundation::TypedEventHandler;
    use windows::Storage::FileAccessMode;
    use windows::Storage::Streams::DataReader;

    let address_hex = format!("{address:012X}").to_ascii_lowercase();
    let mut selectors = Vec::new();
    for (page, usage) in [(0x000Cu16, 0x0001u16), (0x0001u16, 0x0006u16)] {
        if let Ok(selector) = HidDevice::GetDeviceSelector(page, usage) {
            selectors.push(selector.to_string());
        }
    }
    selectors.push(
        "System.Devices.InterfaceClassGuid:=\"{4d1e55b2-f16f-11cf-88cb-001111000030}\" AND System.Devices.InterfaceEnabled:=System.StructuredQueryType.Boolean#True"
            .to_string(),
    );
    let mut opened: Vec<(HidDevice, windows::Foundation::EventRegistrationToken)> = Vec::new();
    let mut subscribed_ids = std::collections::BTreeSet::new();
    let mut seen = Vec::new();
    let mut scanned = 0u32;
    let mut samples = Vec::new();
    for selector in selectors {
        let devices = match DeviceInformation::FindAllAsyncAqsFilter(&HSTRING::from(&selector))
            .map_err(|error| error.to_string())
            .and_then(|op| wait_async(op, Duration::from_secs(8)))
        {
            Ok(devices) => devices,
            Err(error) => {
                seen.push(format!("枚举失败: {error}"));
                continue;
            }
        };
        let count = devices.Size().unwrap_or(0);
        for index in 0..count {
            let Ok(info) = devices.GetAt(index) else {
                continue;
            };
            let name = info.Name().map(|value| value.to_string()).unwrap_or_default();
            let id = info.Id().map(|value| value.to_string()).unwrap_or_default();
            let folded = id.to_ascii_lowercase();
            scanned += 1;
            if samples.len() < 8
                && (index == 0
                    || folded.contains("bth")
                    || folded.contains("2717")
                    || folded.contains("1812"))
            {
                samples.push(format!("{name} :: {id}"));
            }
            let matched = (address != 0 && folded.contains(&address_hex))
                || folded.contains("vid&012717")
                || folded.contains("vid_2717")
                || name.contains("小米")
                || name.contains('遥');
            if !matched || !subscribed_ids.insert(folded.clone()) {
                continue;
            }
            seen.push(format!("{name} [{id}]"));
            let id_h = match info.Id() {
                Ok(id) => id,
                Err(error) => return Err(error.to_string()),
            };
            let device = match HidDevice::FromIdAsync(&id_h, FileAccessMode::Read)
                .map_err(|error| error.to_string())
                .and_then(|op| wait_async(op, Duration::from_secs(8)))
            {
                Ok(device) => device,
                Err(error) => {
                    seen.push(format!("{name} 打开失败: {error}"));
                    continue;
                }
            };
            let tx = tx.clone();
            let tracker = std::sync::Arc::new(std::sync::Mutex::new(crate::hid_usage::HidTracker::default()));
            let handler = TypedEventHandler::new(
                move |_sender: &Option<HidDevice>, args: &Option<HidInputReportReceivedEventArgs>| {
                    let Some(args) = args else {
                        return Ok(());
                    };
                    let Ok(report) = args.Report() else {
                        return Ok(());
                    };
                    let mut edges = Vec::new();
                    if let Ok(buffer) = report.Data() {
                        if let Ok(reader) = DataReader::FromBuffer(&buffer) {
                            let len = reader.UnconsumedBufferLength().unwrap_or(0) as usize;
                            let mut data = vec![0u8; len];
                            let _ = reader.ReadBytes(&mut data);
                            if let Ok(mut tracker) = tracker.lock() {
                                edges.extend(tracker.push(&data));
                            }
                        }
                    }
                    if edges.is_empty() {
                        if let Ok(controls) = report.TransitionedBooleanControls() {
                            let count = controls.Size().unwrap_or(0);
                            for index in 0..count {
                                let Ok(control) = controls.GetAt(index) else {
                                    continue;
                                };
                                let Ok(usage) = control.UsageId() else {
                                    continue;
                                };
                                let Ok(active) = control.IsActive() else {
                                    continue;
                                };
                                if let Some(button) = crate::hid_usage::usage_to_button(usage) {
                                    edges.push((button, active));
                                }
                            }
                        }
                    }
                    for (button, pressed) in edges {
                        let _ = tx.send(Command::Input(InputEvent {
                            button,
                            edge: if pressed { Edge::Down } else { Edge::Up },
                            at: Instant::now(),
                        }));
                    }
                    Ok(())
                },
            );
            match device.InputReportReceived(&handler) {
                Ok(token) => opened.push((device, token)),
                Err(error) => seen.push(format!("{name} 订阅失败: {error}")),
            }
        }
    }
    if opened.is_empty() {
        return Err(if seen.is_empty() {
        return Err(format!(
            "没有找到小米遥控器的系统 HID 接口（地址 {address_hex}，扫过 {scanned} 个，样例 {}）",
            samples.join(" | ")
        ));
        } else {
            seen.join("；")
        });
    }
    let _ = tx.send(Command::Diagnostic(serde_json::json!({
        "type": "ble_transport_connected",
        "name": "小米蓝牙语音遥控器",
    })));
    loop {
        thread::sleep(Duration::from_millis(250));
        if opened.iter().all(|(device, _)| device.VendorId().is_err()) {
            break;
        }
    }
    for (device, token) in opened {
        let _ = device.RemoveInputReportReceived(token);
    }
    Err("系统 HID 已断开".into())
}

#[cfg(windows)]
fn session(tx: &SyncSender<Command>) -> Result<(), String> {
    use windows::core::GUID;
    use windows::Devices::Bluetooth::GenericAttributeProfile::{
        GattCharacteristic, GattCharacteristicProperties,
        GattClientCharacteristicConfigurationDescriptorValue, GattCommunicationStatus,
        GattOpenStatus, GattSharingMode,
    };
    use windows::Devices::Bluetooth::{
        BluetoothCacheMode, BluetoothLEDevice,
    };
    use windows::Devices::Enumeration::DeviceInformation;
    use windows::Foundation::TypedEventHandler;
    use windows::Storage::Streams::DataReader;
    use windows::Win32::System::Com::{CoInitializeEx, COINIT_MULTITHREADED};

    unsafe {
        let _ = CoInitializeEx(None, COINIT_MULTITHREADED);
    }

    let selector = BluetoothLEDevice::GetDeviceSelectorFromPairingState(true)
        .map_err(|error| error.to_string())?;
    let devices = wait_async(
        DeviceInformation::FindAllAsyncAqsFilter(&selector).map_err(|error| error.to_string())?,
        Duration::from_secs(8),
    )?;
    let count = devices.Size().map_err(|error| error.to_string())?;
    let hid_uuid = GUID::from_u128(0x0000_1812_0000_1000_8000_0080_5f9b_34fb);
    let report_uuid = GUID::from_u128(0x0000_2a4d_0000_1000_8000_0080_5f9b_34fb);

    let mut last_error = "没有找到带 HID 服务的已配对遥控器".to_string();
    let mut infos = Vec::new();
    for index in 0..count {
        match devices.GetAt(index) {
            Ok(info) => infos.push(info),
            Err(error) => last_error = error.to_string(),
        }
    }
    infos.retain(|info| {
        let name = info.Name().map(|value| value.to_string()).unwrap_or_default();
        preferred_remote_name(&name)
    });
    if infos.is_empty() {
        return Err(format!("已配对蓝牙设备里没有遥控器（共 {count} 台）"));
    }
    let _ = tx.send(Command::Diagnostic(serde_json::json!({
        "type": "ble_transport_progress",
        "detail": format!("正在连接 {}", infos.iter().filter_map(|info| info.Name().ok()).map(|value| value.to_string()).collect::<Vec<_>>().join("、")),
    })));
    for info in infos {
        let name = info.Name().map(|value| value.to_string()).unwrap_or_default();
        let id = match info.Id() {
            Ok(id) => id,
            Err(error) => {
                last_error = error.to_string();
                continue;
            }
        };
        let _ = tx.send(Command::Diagnostic(serde_json::json!({
            "type": "ble_transport_progress",
            "detail": format!("正在检查 {name}"),
        })));
        let device = match BluetoothLEDevice::FromIdAsync(&id)
            .map_err(|error| error.to_string())
            .and_then(|op| wait_async(op, Duration::from_secs(8)))
        {
            Ok(device) => device,
            Err(error) => {
                last_error = format!("{name}: {error}");
                continue;
            }
        };
        let services = match hid_services(&device, hid_uuid) {
            Ok(services) => services,
            Err(error) => {
                last_error = format!("{name}: {error}");
                continue;
            }
        };
        if services.Status().ok() != Some(GattCommunicationStatus::Success) {
            continue;
        }
        let list = match services.Services() {
            Ok(list) => list,
            Err(error) => {
                last_error = error.to_string();
                continue;
            }
        };
        if list.Size().unwrap_or(0) == 0 {
            last_error = format!("{name} 没有 HID 服务");
            continue;
        }
        let service = match list.GetAt(0) {
            Ok(service) => service,
            Err(error) => {
                last_error = error.to_string();
                continue;
            }
        };
        match service
            .OpenAsync(GattSharingMode::SharedReadOnly)
            .map_err(|error| error.to_string())
            .and_then(|op| wait_async(op, Duration::from_secs(8)))
        {
            Ok(GattOpenStatus::Success | GattOpenStatus::AlreadyOpened) => {}
            Ok(GattOpenStatus::AccessDenied) => {
                let address = device.BluetoothAddress().unwrap_or(0);
                let radio_up = radio_connected(&device);
                publish_link(tx, &name, radio_up);
                let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
                let stop_flag = stop.clone();
                let watch_tx = tx.clone();
                let watch_name = name.clone();
                thread::spawn(move || {
                    watch_bluetooth_link(device, watch_name, watch_tx, radio_up, stop_flag)
                });
                let listened = crate::raw_input::listen(tx, address);
                stop.store(true, std::sync::atomic::Ordering::Relaxed);
                match listened {
                    Ok(()) => return Ok(()),
                    Err(error) => {
                        last_error = format!(
                            "{name} 的 HID 已被系统占用，系统按键通道也没能启动: {error}"
                        );
                        continue;
                    }
                }
            }
            Ok(status) => {
                last_error = format!("{name} HID 打开失败: {status:?}");
                continue;
            }
            Err(error) => {
                last_error = format!("{name} HID 打开失败: {error}");
                continue;
            }
        }
        let chars = match service
            .GetCharacteristicsForUuidWithCacheModeAsync(report_uuid, BluetoothCacheMode::Uncached)
            .map_err(|error| error.to_string())
            .and_then(|op| wait_async(op, Duration::from_secs(8)))
        {
            Ok(chars) => chars,
            Err(error) => {
                last_error = format!("{name}: {error}");
                continue;
            }
        };
        if chars.Status().ok() != Some(GattCommunicationStatus::Success) {
            continue;
        }
        let reports = match chars.Characteristics() {
            Ok(reports) => reports,
            Err(error) => {
                last_error = error.to_string();
                continue;
            }
        };
        let tracker = std::sync::Arc::new(std::sync::Mutex::new(HidTracker::default()));
        let mut subscribed = false;
        let report_count = reports.Size().unwrap_or(0);
        let mut kept: Vec<(GattCharacteristic, windows::Foundation::EventRegistrationToken)> =
            Vec::new();
        for report_index in 0..report_count {
            let Ok(ch) = reports.GetAt(report_index) else {
                continue;
            };
            let props = ch
                .CharacteristicProperties()
                .unwrap_or(GattCharacteristicProperties(0));
            let can_notify = props.contains(GattCharacteristicProperties::Notify)
                || props.contains(GattCharacteristicProperties::Indicate);
            if !can_notify {
                continue;
            }
            let tracker = tracker.clone();
            let tx = tx.clone();
            let handler = TypedEventHandler::new(
                move |_sender: &Option<GattCharacteristic>,
                      args: &Option<windows::Devices::Bluetooth::GenericAttributeProfile::GattValueChangedEventArgs>| {
                    let Some(args) = args else {
                        return Ok(());
                    };
                    let Ok(buffer) = args.CharacteristicValue() else {
                        return Ok(());
                    };
                    let Ok(reader) = DataReader::FromBuffer(&buffer) else {
                        return Ok(());
                    };
                    let len = reader.UnconsumedBufferLength().unwrap_or(0) as usize;
                    let mut data = vec![0u8; len];
                    let _ = reader.ReadBytes(&mut data);
                    let edges = tracker.lock().map(|mut tracker| tracker.push(&data)).unwrap_or_default();
                    for (button, pressed) in edges {
                        let _ = tx.send(Command::Input(InputEvent {
                            button,
                            edge: if pressed { Edge::Down } else { Edge::Up },
                            at: Instant::now(),
                        }));
                    }
                    Ok(())
                },
            );
            let Ok(token) = ch.ValueChanged(&handler) else {
                continue;
            };
            let cccd = if props.contains(GattCharacteristicProperties::Notify) {
                GattClientCharacteristicConfigurationDescriptorValue::Notify
            } else {
                GattClientCharacteristicConfigurationDescriptorValue::Indicate
            };
            match ch
                .WriteClientCharacteristicConfigurationDescriptorAsync(cccd)
                .map_err(|error| error.to_string())
                .and_then(|op| wait_async(op, Duration::from_secs(8)))
            {
                Ok(status) if status == GattCommunicationStatus::Success => {
                    kept.push((ch, token));
                    subscribed = true;
                }
                _ => {
                    let _ = ch.RemoveValueChanged(token);
                }
            }
        }
        if !subscribed {
            last_error = format!("{name} 没有可订阅的 HID 报告");
            continue;
        }
        // A paired remote can open a cached GATT session while the radio is
        // still down. Only the live connection status may say "connected".
        let mut radio_up = radio_connected(&device);
        publish_link(tx, &name, radio_up);
        let mut disconnected_since: Option<Instant> = None;
        loop {
            let up = radio_connected(&device);
            if up != radio_up {
                radio_up = up;
                publish_link(tx, &name, radio_up);
            }
            if up {
                disconnected_since = None;
            } else {
                let since = disconnected_since.get_or_insert_with(Instant::now);
                if since.elapsed() >= Duration::from_secs(3) {
                    break;
                }
            }
            thread::sleep(Duration::from_millis(250));
        }
        for (ch, token) in kept {
            let _ = ch.RemoveValueChanged(token);
        }
        return Err(format!("{name} 已断开"));
    }
    Err(last_error)
}

#[cfg(not(windows))]
fn session(_tx: &SyncSender<Command>) -> Result<(), String> {
    Err("BLE HID 仅支持 Windows".into())
}

#[cfg(windows)]
fn radio_connected(device: &windows::Devices::Bluetooth::BluetoothLEDevice) -> bool {
    use windows::Devices::Bluetooth::BluetoothConnectionStatus;
    device.ConnectionStatus().ok() == Some(BluetoothConnectionStatus::Connected)
}

#[cfg(windows)]
fn watch_bluetooth_link(
    device: windows::Devices::Bluetooth::BluetoothLEDevice,
    name: String,
    tx: SyncSender<Command>,
    mut announced: bool,
    stop: std::sync::Arc<std::sync::atomic::AtomicBool>,
) {
    use std::sync::atomic::Ordering;
    while !stop.load(Ordering::Relaxed) {
        let up = radio_connected(&device);
        if up != announced {
            announced = up;
            publish_link(&tx, &name, up);
        }
        thread::sleep(Duration::from_millis(400));
    }
}

fn publish_link(tx: &SyncSender<Command>, name: &str, radio_up: bool) {
    let (kind, text) = link_notice(radio_up);
    let event = if radio_up {
        serde_json::json!({
            "type": kind,
            "name": name,
            "detail": text,
        })
    } else {
        serde_json::json!({
            "type": kind,
            "error": text,
        })
    };
    let _ = tx.send(Command::Diagnostic(event));
}

fn link_notice(radio_up: bool) -> (&'static str, &'static str) {
    if radio_up {
        ("ble_transport_connected", "遥控器蓝牙已连接")
    } else {
        ("ble_transport_waiting", "已配对，遥控器未连接")
    }
}

fn preferred_remote_name(name: &str) -> bool {
    let folded = name.to_lowercase();
    folded.contains("rc")
        || folded.contains("xiaomi")
        || folded.contains("remote")
        || name.contains('遥')
}

#[cfg(test)]
mod tests {
    use super::preferred_remote_name;

    #[test]
    fn remote_names_are_preferred_over_generic_hid_devices() {
        assert!(preferred_remote_name("Xiaomi RC"));
        assert!(preferred_remote_name("小米遥控器"));
        assert!(!preferred_remote_name("Headphones"));
    }

    #[test]
    fn a_paired_remote_stays_disconnected_until_the_radio_is_up() {
        assert_eq!(
            super::link_notice(false),
            ("ble_transport_waiting", "已配对，遥控器未连接")
        );
        assert_eq!(
            super::link_notice(true),
            ("ble_transport_connected", "遥控器蓝牙已连接")
        );
    }
}
