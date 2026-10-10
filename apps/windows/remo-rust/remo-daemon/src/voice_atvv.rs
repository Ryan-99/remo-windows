//! RC003 voice service: ask the remote for audio and play the decoded PCM.

use crate::atvv::{self, Control, Session};
use crate::playback::Player;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, SyncSender, TrySendError};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use super::Command;

enum Incoming {
    Press,
    Release,
    Control(Vec<u8>),
    Audio(Vec<u8>),
}

enum KeyEdge {
    Press,
    Release,
}

pub struct VoiceLink {
    keys: SyncSender<KeyEdge>,
    held: Arc<AtomicBool>,
}

impl VoiceLink {
    pub fn press(&self) {
        self.held.store(true, Ordering::SeqCst);
        // Key edges have their own queue. Audio frames must not be able to
        // fill it and drop the press that opens the microphone.
        let _ = self.keys.try_send(KeyEdge::Press);
    }

    pub fn release(&self) {
        self.held.store(false, Ordering::SeqCst);
        let _ = self.keys.try_send(KeyEdge::Release);
    }
}

pub fn start(endpoint: String, gain_db: f32) -> (VoiceLink, Receiver<Command>) {
    let (key_tx, key_rx) = mpsc::sync_channel(16);
    let (diag_tx, diag_rx) = mpsc::sync_channel(64);
    let held = Arc::new(AtomicBool::new(false));
    let retry_held = Arc::clone(&held);
    thread::spawn(move || run(endpoint, gain_db, key_rx, retry_held, diag_tx));
    (VoiceLink { keys: key_tx, held }, diag_rx)
}

fn run(
    endpoint: String,
    gain_db: f32,
    keys: Receiver<KeyEdge>,
    held: Arc<AtomicBool>,
    diag: SyncSender<Command>,
) {
    loop {
        let error = match session(&endpoint, gain_db, &keys, &held, &diag) {
            Ok(()) => return,
            Err(error) => {
                let _ = diag.send(Command::Diagnostic(serde_json::json!({
                    "type": "voice_link",
                    "detail": error,
                })));
                error
            }
        };
        // Do not drain `keys` here. A press that arrives while the
        // session is being rebuilt has to stay in the channel so the next
        // session still opens the microphone.
        let pause = if error.contains("重新建立语音连接") {
            Duration::from_millis(400)
        } else {
            Duration::from_secs(3)
        };
        thread::sleep(pause);
    }
}

#[cfg(windows)]
fn session(
    endpoint: &str,
    gain_db: f32,
    keys: &Receiver<KeyEdge>,
    held: &AtomicBool,
    diag: &SyncSender<Command>,
) -> Result<(), String> {
    use windows::core::GUID;
    use windows::Devices::Bluetooth::GenericAttributeProfile::{
        GattOpenStatus, GattSharingMode,
    };
    use windows::Devices::Bluetooth::{BluetoothCacheMode, BluetoothLEDevice};
    use windows::Devices::Enumeration::DeviceInformation;
    use windows::Win32::System::Com::{CoInitializeEx, COINIT_MULTITHREADED};

    unsafe {
        let _ = CoInitializeEx(None, COINIT_MULTITHREADED);
    }
    let selector = BluetoothLEDevice::GetDeviceSelectorFromPairingState(true)
        .map_err(|error| error.to_string())?;
    let devices = wait(
        DeviceInformation::FindAllAsyncAqsFilter(&selector).map_err(|error| error.to_string())?,
    )?;
    let mut chosen = None;
    for index in 0..devices.Size().unwrap_or(0) {
        let Ok(info) = devices.GetAt(index) else { continue };
        let name = info.Name().map(|value| value.to_string()).unwrap_or_default();
        if preferred(&name) {
            chosen = Some((info, name));
            break;
        }
    }
    let (info, name) = chosen.ok_or("已配对设备里没有遥控器语音服务")?;
    let id = info.Id().map_err(|error| error.to_string())?;
    let device = wait(BluetoothLEDevice::FromIdAsync(&id).map_err(|error| error.to_string())?)?;
    let gatt_session = prepare_voice_session(&device);
    let service_uuid = GUID::from_u128(0xAB5E0001_5A21_4F05_BC7D_AF01F617B664);
    let Some(service) = find_voice_service(&device, service_uuid)? else {
        let connection = device.ConnectionStatus().ok();
        let note = gatt_session.as_ref().err().map(|error| error.as_str()).unwrap_or("gatt=ok");
        return Err(format!("{name} 没有 ATVV 语音服务 {note} connection={connection:?}"));
    };
    let _keep_gatt_session = gatt_session.ok();
    match wait(service.OpenAsync(GattSharingMode::SharedReadAndWrite).map_err(|error| error.to_string())?)? {
        GattOpenStatus::Success | GattOpenStatus::AlreadyOpened => {}
        GattOpenStatus::AccessDenied => return Err("遥控器语音服务被系统占用".into()),
        status => return Err(format!("{name} 语音服务打开失败: {status:?}")),
    }
    let characteristics = wait(
        service
            .GetCharacteristicsWithCacheModeAsync(BluetoothCacheMode::Uncached)
            .map_err(|error| error.to_string())?,
    )?;
    let list = characteristics.Characteristics().map_err(|error| error.to_string())?;
    let tx_uuid = GUID::from_u128(0xAB5E0002_5A21_4F05_BC7D_AF01F617B664);
    let audio_uuid = GUID::from_u128(0xAB5E0003_5A21_4F05_BC7D_AF01F617B664);
    let control_uuid = GUID::from_u128(0xAB5E0004_5A21_4F05_BC7D_AF01F617B664);
    let mut tx_char = None;
    let mut audio_char = None;
    let mut control_char = None;
    for index in 0..list.Size().unwrap_or(0) {
        let Ok(ch) = list.GetAt(index) else { continue };
        let Ok(uuid) = ch.Uuid() else { continue };
        if uuid == tx_uuid {
            tx_char = Some(ch);
        } else if uuid == audio_uuid {
            audio_char = Some(ch);
        } else if uuid == control_uuid {
            control_char = Some(ch);
        }
    }
    let tx_char = tx_char.ok_or("没有 ATVV 写入特征")?;
    let audio_char = audio_char.ok_or("没有 ATVV 音频特征")?;
    let control_char = control_char.ok_or("没有 ATVV 控制特征")?;
    let (control_tx, control_rx) = mpsc::sync_channel(32);
    let (audio_tx, audio_rx) = mpsc::sync_channel(256);
    let _audio_token = subscribe(&audio_char, audio_tx)?;
    subscribe(&control_char, control_tx)?;
    write_bytes(&tx_char, &atvv::GET_CAPABILITIES)?;
    let _ = diag.send(Command::Diagnostic(serde_json::json!({
        "type": "voice_link",
        "detail": format!("{name} 语音服务已连接"),
    })));

    let mut session = Session::new(gain_db);
    let mut player: Option<Player> = None;
    let mut opening = false;
    let mut opened_at = Instant::now();
    let mut last_extend = Instant::now();
    let mut pending_press = false;
    let mut opened_this_press = false;
    let mut audio_packets = 0u32;
    let mut audio_samples = 0u32;
    let mut nudged_open = false;
    let mut refreshed_notify = false;
    loop {
        let event = match poll_voice(keys, &control_rx, &audio_rx) {
            Ok(event) => event,
            Err(()) => return Ok(()),
        };
        match event {
            Some(Incoming::Press) => {
                audio_packets = 0;
                audio_samples = 0;
                opened_this_press = false;
                nudged_open = false;
                refreshed_notify = false;
                while audio_rx.try_recv().is_ok() {}
                // A lost AUDIO_STOP leaves the local flag open and the next
                // hold never sends MIC_OPEN. Clear that flag only. Closing
                // the remote from here makes it answer with AUDIO_STOP while
                // the key is still down, and rebuilding the link then lets
                // the mic key through as F5.
                if session.active() || opening {
                    session.force_idle();
                    opening = false;
                }
                pending_press = true;
            }
            Some(Incoming::Release) => {
                // Measure the hold before the grace wait. The wait is only
                // there to catch frames already in flight, and must not make
                // a short tap look like a long silent hold.
                let held = opened_at.elapsed();
                let grace = Instant::now() + Duration::from_millis(280);
                loop {
                    while let Ok(payload) = control_rx.try_recv() {
                        if let Control::AudioStarted(_) = session.handle_control(&payload) {
                            last_extend = Instant::now();
                        }
                    }
                    while let Ok(payload) = audio_rx.try_recv() {
                        audio_packets = audio_packets.saturating_add(1);
                        let samples = session.handle_audio(&payload);
                        audio_samples = audio_samples.saturating_add(samples.len() as u32);
                        if !samples.is_empty() {
                            if let Some(player) = &player {
                                player.write(samples);
                            }
                        }
                    }
                    if audio_packets > 0 || Instant::now() >= grace {
                        break;
                    }
                    thread::sleep(Duration::from_millis(20));
                }
                pending_press = false;
                opening = false;
                if session.version() != 0 {
                    let _ = write_bytes(&tx_char, &atvv::mic_close_command(session.version(), session.stream().unwrap_or(0)));
                }
                session.force_idle();
                let detail = if !opened_this_press {
                    "语音键已松开".to_string()
                } else if audio_packets == 0 && held < Duration::from_millis(1500) {
                    "按键时间较短，语音尚未传出".to_string()
                } else if audio_packets == 0 {
                    "遥控器没有传来音频".to_string()
                } else if audio_samples == 0 {
                    format!("收到 {audio_packets} 个音频包，但没有解出采样")
                } else {
                    format!("已播放遥控器音频，采样 {audio_samples}")
                };
                let stale = opened_this_press
                    && audio_packets == 0
                    && held >= Duration::from_millis(1500);
                opened_this_press = false;
                let _ = diag.send(Command::Diagnostic(serde_json::json!({
                    "type": "voice_link",
                    "detail": detail,
                })));
                // A tap that ends before frames arrive is normal. Only a real
                // hold with no packets means this audio subscription is dead.
                // The session is opened again; the notify is not rewritten.
                if stale {
                    return Err("音频通知已失效，正在重新建立语音连接".into());
                }
            }
            Some(Incoming::Control(payload)) => match session.handle_control(&payload) {
                Control::Capabilities(version) => {
                    let _ = diag.send(Command::Diagnostic(serde_json::json!({
                        "type": "voice_link",
                        "detail": format!("语音能力已就绪 version={version:#06x} frame={}", session.frame_size()),
                    })));
                    if player.is_none() {
                        match Player::open(endpoint.to_string()) {
                            Ok(opened) => {
                                player = Some(opened);
                                let _ = diag.send(Command::Diagnostic(serde_json::json!({
                                    "type": "voice_link",
                                    "detail": format!("播放端点已打开：{endpoint}"),
                                })));
                            }
                            Err(error) => {
                                let _ = diag.send(Command::Diagnostic(serde_json::json!({
                                    "type": "voice_link",
                                    "detail": error,
                                })));
                            }
                        }
                    }
                }
                Control::AudioStarted(stream) => {
                    opening = false;
                    last_extend = Instant::now();
                    let _ = diag.send(Command::Diagnostic(serde_json::json!({
                        "type": "voice_audio_started",
                        "stream": stream,
                    })));
                }
                Control::AudioStopped | Control::MicClosed => {
                    let _ = diag.send(Command::Diagnostic(serde_json::json!({
                        "type": "voice_audio_stopped",
                    })));
                }
                Control::MicButton if !session.active() && !opening => pending_press = true,
                _ => {}
            },
            Some(Incoming::Audio(payload)) => {
                audio_packets = audio_packets.saturating_add(1);
                let samples = session.handle_audio(&payload);
                audio_samples = audio_samples.saturating_add(samples.len() as u32);
                if !samples.is_empty() {
                    if let Some(player) = &player {
                        player.write(samples);
                    }
                }
            }
            None => {}
        }
        if pending_press && session.version() != 0 && !session.active() && !opening {
            write_bytes(&tx_char, &atvv::mic_open_command(session.version()))?;
            opening = true;
            pending_press = false;
            opened_this_press = true;
            opened_at = Instant::now();
            let _ = diag.send(Command::Diagnostic(serde_json::json!({
                "type": "voice_link",
                "detail": "已发送 MIC_OPEN",
            })));
        }
        if opening && opened_at.elapsed() > Duration::from_secs(5) {
            opening = false;
            let _ = diag.send(Command::Diagnostic(serde_json::json!({
                "type": "voice_link",
                "detail": "遥控器没有开始传送语音",
            })));
        }
        // A hold that only gets stream 0 has not started a real recording.
        // Ask again on the same connection. Dropping the link here is what
        // lets the mic key reach Windows as F5.
        let quiet = opened_this_press
            && audio_packets == 0
            && session.stream().unwrap_or(0) == 0
            && held.load(Ordering::SeqCst);
        if quiet && !nudged_open && opened_at.elapsed() > Duration::from_millis(350) {
            if write_bytes(&tx_char, &atvv::mic_open_command(session.version())).is_ok() {
                nudged_open = true;
                opening = true;
                opened_at = Instant::now();
                let _ = diag.send(Command::Diagnostic(serde_json::json!({
                    "type": "voice_link",
                    "detail": "遥控器还没传来音频，正在再次打开麦克风",
                })));
            }
        }
        if quiet && nudged_open && !refreshed_notify && opened_at.elapsed() > Duration::from_millis(450) {
            refreshed_notify = true;
            if held.load(Ordering::SeqCst)
                && refresh_audio_notify(&audio_char).is_ok()
                && held.load(Ordering::SeqCst)
            {
                session.force_idle();
                opening = false;
                if write_bytes(&tx_char, &atvv::mic_open_command(session.version())).is_ok() {
                    opening = true;
                    opened_this_press = true;
                    opened_at = Instant::now();
                    let _ = diag.send(Command::Diagnostic(serde_json::json!({
                        "type": "voice_link",
                        "detail": "正在重新订阅遥控器音频",
                    })));
                }
            }
        }
        if session.active() && session.version() >= 0x0100 && last_extend.elapsed() >= Duration::from_millis(2500) {
            if let Some(stream) = session.stream().filter(|stream| *stream != 0) {
                let _ = write_bytes(&tx_char, &atvv::mic_extend_command(stream));
                last_extend = Instant::now();
            }
        }
    }
}

#[cfg(windows)]
fn refresh_audio_notify(
    characteristic: &windows::Devices::Bluetooth::GenericAttributeProfile::GattCharacteristic,
) -> Result<(), String> {
    use windows::Devices::Bluetooth::GenericAttributeProfile::{
        GattCharacteristicProperties, GattClientCharacteristicConfigurationDescriptorValue,
    };
    let props = characteristic
        .CharacteristicProperties()
        .unwrap_or(GattCharacteristicProperties(0));
    let enabled = if props.contains(GattCharacteristicProperties::Indicate)
        && !props.contains(GattCharacteristicProperties::Notify)
    {
        GattClientCharacteristicConfigurationDescriptorValue::Indicate
    } else {
        GattClientCharacteristicConfigurationDescriptorValue::Notify
    };
    write_cccd(characteristic, GattClientCharacteristicConfigurationDescriptorValue::None)?;
    thread::sleep(Duration::from_millis(120));
    write_cccd(characteristic, enabled)
}

#[cfg(windows)]
fn write_cccd(
    characteristic: &windows::Devices::Bluetooth::GenericAttributeProfile::GattCharacteristic,
    value: windows::Devices::Bluetooth::GenericAttributeProfile::GattClientCharacteristicConfigurationDescriptorValue,
) -> Result<(), String> {
    use windows::Devices::Bluetooth::GenericAttributeProfile::GattCommunicationStatus;
    let status = wait(
        characteristic
            .WriteClientCharacteristicConfigurationDescriptorAsync(value)
            .map_err(|error| error.to_string())?,
    )?;
    if status == GattCommunicationStatus::Success {
        Ok(())
    } else {
        Err(format!("重新订阅音频失败: {status:?}"))
    }
}

#[cfg(windows)]
fn prepare_voice_session(
    device: &windows::Devices::Bluetooth::BluetoothLEDevice,
) -> Result<windows::Devices::Bluetooth::GenericAttributeProfile::GattSession, String> {
    use windows::Devices::Bluetooth::BluetoothConnectionStatus;
    use windows::Devices::Bluetooth::GenericAttributeProfile::GattSession;
    let device_id = device.BluetoothDeviceId().map_err(|error| error.to_string())?;
    let session = wait(GattSession::FromDeviceIdAsync(&device_id).map_err(|error| error.to_string())?)?;
    if !session.CanMaintainConnection().unwrap_or(false) {
        return Err("gatt=设备不允许保持连接".into());
    }
    session.SetMaintainConnection(true).map_err(|error| error.to_string())?;
    let started = Instant::now();
    while started.elapsed() < Duration::from_secs(8) {
        if device.ConnectionStatus().ok() == Some(BluetoothConnectionStatus::Connected) {
            break;
        }
        thread::sleep(Duration::from_millis(200));
    }
    Ok(session)
}

#[cfg(windows)]
fn find_voice_service(
    device: &windows::Devices::Bluetooth::BluetoothLEDevice,
    uuid: windows::core::GUID,
) -> Result<Option<windows::Devices::Bluetooth::GenericAttributeProfile::GattDeviceService>, String> {
    use windows::Devices::Bluetooth::BluetoothCacheMode;
    use windows::Devices::Bluetooth::GenericAttributeProfile::GattCommunicationStatus;
    let services = wait(
        device
            .GetGattServicesForUuidWithCacheModeAsync(uuid, BluetoothCacheMode::Uncached)
            .map_err(|error| error.to_string())?,
    )?;
    if services.Status().ok() == Some(GattCommunicationStatus::Success) {
        if let Ok(list) = services.Services() {
            if list.Size().unwrap_or(0) > 0 {
                return Ok(Some(list.GetAt(0).map_err(|error| error.to_string())?));
            }
        }
    }
    let all = wait(
        device
            .GetGattServicesWithCacheModeAsync(BluetoothCacheMode::Uncached)
            .map_err(|error| error.to_string())?,
    )?;
    if all.Status().ok() != Some(GattCommunicationStatus::Success) {
        return Ok(None);
    }
    let list = all.Services().map_err(|error| error.to_string())?;
    for index in 0..list.Size().unwrap_or(0) {
        let Ok(service) = list.GetAt(index) else { continue };
        if service.Uuid().ok() == Some(uuid) {
            return Ok(Some(service));
        }
    }
    Ok(None)
}

#[cfg(windows)]
fn poll_voice(
    keys: &Receiver<KeyEdge>,
    control: &Receiver<Vec<u8>>,
    audio: &Receiver<Vec<u8>>,
) -> Result<Option<Incoming>, ()> {
    match keys.try_recv() {
        Ok(KeyEdge::Press) => return Ok(Some(Incoming::Press)),
        Ok(KeyEdge::Release) => return Ok(Some(Incoming::Release)),
        Err(mpsc::TryRecvError::Disconnected) => return Err(()),
        Err(mpsc::TryRecvError::Empty) => {}
    }
    match control.try_recv() {
        Ok(payload) => return Ok(Some(Incoming::Control(payload))),
        Err(mpsc::TryRecvError::Disconnected) => return Err(()),
        Err(mpsc::TryRecvError::Empty) => {}
    }
    match audio.try_recv() {
        Ok(payload) => Ok(Some(Incoming::Audio(payload))),
        Err(mpsc::TryRecvError::Disconnected) => Err(()),
        Err(mpsc::TryRecvError::Empty) => {
            thread::sleep(Duration::from_millis(20));
            Ok(None)
        }
    }
}

#[cfg(windows)]
fn subscribe(
    characteristic: &windows::Devices::Bluetooth::GenericAttributeProfile::GattCharacteristic,
    tx: SyncSender<Vec<u8>>,
) -> Result<windows::Foundation::EventRegistrationToken, String> {
    use windows::Devices::Bluetooth::GenericAttributeProfile::{
        GattCharacteristic, GattCharacteristicProperties,
        GattClientCharacteristicConfigurationDescriptorValue, GattCommunicationStatus,
    };
    use windows::Foundation::TypedEventHandler;
    use windows::Storage::Streams::DataReader;
    let props = characteristic
        .CharacteristicProperties()
        .unwrap_or(GattCharacteristicProperties(0));
    let handler = TypedEventHandler::<GattCharacteristic, windows::Devices::Bluetooth::GenericAttributeProfile::GattValueChangedEventArgs>::new(
        move |_sender, args| {
            let Some(args) = args else { return Ok(()) };
            let Ok(buffer) = args.CharacteristicValue() else { return Ok(()) };
            let Ok(reader) = DataReader::FromBuffer(&buffer) else { return Ok(()) };
            let len = reader.UnconsumedBufferLength().unwrap_or(0) as usize;
            let mut data = vec![0u8; len];
            let _ = reader.ReadBytes(&mut data);
            if let Err(TrySendError::Disconnected(_)) = tx.try_send(data) {}
            Ok(())
        },
    );
    let token = characteristic.ValueChanged(&handler).map_err(|error| error.to_string())?;
    let cccd = if props.contains(GattCharacteristicProperties::Indicate)
        && !props.contains(GattCharacteristicProperties::Notify)
    {
        GattClientCharacteristicConfigurationDescriptorValue::Indicate
    } else {
        GattClientCharacteristicConfigurationDescriptorValue::Notify
    };
    let status = wait(
        characteristic
            .WriteClientCharacteristicConfigurationDescriptorAsync(cccd)
            .map_err(|error| error.to_string())?,
    )?;
    if status == GattCommunicationStatus::Success {
        Ok(token)
    } else {
        Err(format!("订阅语音通知失败: {status:?}"))
    }
}

#[cfg(windows)]
fn write_bytes(
    characteristic: &windows::Devices::Bluetooth::GenericAttributeProfile::GattCharacteristic,
    bytes: &[u8],
) -> Result<(), String> {
    use windows::Devices::Bluetooth::GenericAttributeProfile::GattCommunicationStatus;
    use windows::Storage::Streams::DataWriter;
    let writer = DataWriter::new().map_err(|error| error.to_string())?;
    writer.WriteBytes(bytes).map_err(|error| error.to_string())?;
    let buffer = writer.DetachBuffer().map_err(|error| error.to_string())?;
    let result = wait(
        characteristic
            .WriteValueWithResultAsync(&buffer)
            .map_err(|error| error.to_string())?,
    )?;
    if result.Status().ok() == Some(GattCommunicationStatus::Success) {
        Ok(())
    } else {
        Err("写入遥控器语音特征失败".into())
    }
}

#[cfg(windows)]
fn wait<T>(op: windows::Foundation::IAsyncOperation<T>) -> Result<T, String>
where
    T: windows::core::RuntimeType + 'static,
{
    use windows::Foundation::AsyncStatus;
    let started = Instant::now();
    loop {
        match op.Status() {
            Ok(AsyncStatus::Started) if started.elapsed() < Duration::from_secs(8) => {
                thread::sleep(Duration::from_millis(40));
            }
            Ok(AsyncStatus::Started) => {
                let _ = op.Cancel();
                return Err("蓝牙语音操作超时".into());
            }
            Ok(_) => return op.GetResults().map_err(|error| error.to_string()),
            Err(error) => return Err(error.to_string()),
        }
    }
}

#[cfg(windows)]
fn preferred(name: &str) -> bool {
    let folded = name.to_lowercase();
    folded.contains("rc")
        || folded.contains("xiaomi")
        || folded.contains("remote")
        || name.contains('遥')
}

#[cfg(not(windows))]
fn session(
    _endpoint: &str,
    _gain_db: f32,
    _keys: &Receiver<KeyEdge>,
    _held: &AtomicBool,
    _diag: &SyncSender<Command>,
) -> Result<(), String> {
    Err("ATVV 语音仅支持 Windows".into())
}
