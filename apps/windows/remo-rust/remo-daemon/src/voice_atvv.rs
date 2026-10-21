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
    let mut ignore_stop_until = Instant::now();
    let mut reopened = false;
    let mut priming = false;
    let mut prime_saw_start = false;
    let mut repair = Repair::new();
    let mut last_player_try: Option<Instant> = None;
    loop {
        let event = match poll_voice(keys, &control_rx, &audio_rx) {
            Ok(event) => event,
            Err(()) => return Ok(()),
        };
        match event {
            Some(Incoming::Press) => {
                let was_priming = priming;
                priming = false;
                repair.on_press();
                audio_packets = 0;
                audio_samples = 0;
                opened_this_press = false;
                reopened = false;
                ignore_stop_until = Instant::now();
                while audio_rx.try_recv().is_ok() {}
                // A lost AUDIO_STOP leaves the local flag open and the next
                // hold never sends MIC_OPEN. Clear that flag only. Closing
                // the remote from here makes it answer with AUDIO_STOP while
                // the key is still down, and rebuilding the link then lets
                // the mic key through as F5. The idle warm-up is the exception:
                // it is already an empty open, and the real press has to close
                // it before asking again.
                if was_priming {
                    ignore_stop_until = Instant::now() + Duration::from_millis(500);
                    let version = session.version();
                    let stream = session.stream().unwrap_or(0);
                    session.force_idle();
                    opening = false;
                    if version != 0 {
                        let _ = write_bytes(&tx_char, &atvv::mic_close_command(version, stream));
                    }
                } else if session.active() || opening {
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
                            let mut playback_dead = false;
                            if let Some(live) = &player {
                                if !live.write(samples) {
                                    playback_dead = true;
                                }
                            }
                            if playback_dead {
                                player = None;
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
                opened_this_press = false;
                repair.on_release(Instant::now(), audio_samples > 0);
                let _ = diag.send(Command::Diagnostic(serde_json::json!({
                    "type": "voice_link",
                    "detail": detail,
                })));
                // Leave the connection up. Dropping it after one silent hold
                // makes the following presses arrive while the link is down,
                // so the input method never wakes. The repair below reopens
                // the microphone while the key is up instead.
            }
            Some(Incoming::Control(payload)) => {
                // A recovery MIC_CLOSE is answered with AUDIO_STOP. That stop
                // belongs to the empty stream, and must not cancel the open
                // that follows it.
                if payload.first() == Some(&0x00) && Instant::now() < ignore_stop_until {
                    continue;
                }
                match session.handle_control(&payload) {
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
                    if priming {
                        prime_saw_start = true;
                    }
                    let bytes: String = payload.iter().map(|byte| format!("{byte:02x}")).collect();
                    let _ = diag.send(Command::Diagnostic(serde_json::json!({
                        "type": "voice_audio_started",
                        "stream": stream,
                        "bytes": bytes,
                        "prime": priming,
                    })));
                }
                Control::AudioStopped | Control::MicClosed => {
                    let _ = diag.send(Command::Diagnostic(serde_json::json!({
                        "type": "voice_audio_stopped",
                        "prime": priming,
                    })));
                }
                Control::MicButton if !session.active() && !opening => pending_press = true,
                _ => {}
                }
            }
            Some(Incoming::Audio(payload)) => {
                if priming {
                    continue;
                }
                audio_packets = audio_packets.saturating_add(1);
                let samples = session.handle_audio(&payload);
                audio_samples = audio_samples.saturating_add(samples.len() as u32);
                if !samples.is_empty() {
                    if let Some(live) = &player {
                        if !live.write(samples) {
                            player = None;
                        }
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
        // A stuck remote answers MIC_OPEN with stream 0 and then sends no
        // frames. Close that empty session and open again on the same
        // connection. Leaving the GATT session up keeps the input method
        // chord held.
        let empty = opened_this_press
            && !reopened
            && audio_packets == 0
            && session.stream().unwrap_or(0) == 0
            && opened_at.elapsed() > Duration::from_millis(400)
            && held.load(Ordering::SeqCst);
        if empty {
            reopened = true;
            ignore_stop_until = Instant::now() + Duration::from_millis(500);
            let version = session.version();
            session.force_idle();
            opening = false;
            if version != 0 {
                let _ = write_bytes(&tx_char, &atvv::mic_close_command(version, 0));
                let _ = write_bytes(&tx_char, &atvv::GET_CAPABILITIES);
                if write_bytes(&tx_char, &atvv::mic_open_command(version)).is_ok() {
                    opening = true;
                    pending_press = false;
                    opened_at = Instant::now();
                    let _ = diag.send(Command::Diagnostic(serde_json::json!({
                        "type": "voice_link",
                        "detail": "麦克风没有出声，已重新打开",
                    })));
                }
            }
        }
        // The remote only emits its own audio start (04 03 ..) on a physical
        // press after the host has already opened and closed once. That first
        // cycle carries no samples, so do it while the key is up.
        if priming && !held.load(Ordering::SeqCst) {
            let stream = session.stream().unwrap_or(0);
            let saw = prime_saw_start || session.active() || session.stream().is_some();
            let ready = saw || opened_at.elapsed() > Duration::from_millis(350);
            if ready {
                let version = session.version();
                ignore_stop_until = Instant::now() + Duration::from_millis(400);
                session.force_idle();
                opening = false;
                priming = false;
                prime_saw_start = false;
                repair.on_prime_finished(saw);
                if version != 0 {
                    let _ = write_bytes(&tx_char, &atvv::mic_close_command(version, stream));
                    let _ = write_bytes(&tx_char, &atvv::GET_CAPABILITIES);
                }
                let _ = diag.send(Command::Diagnostic(serde_json::json!({
                    "type": "voice_link",
                    "detail": if saw { "空闲预热已完成" } else { "麦克风没有应答" },
                })));
            }
        }
        let idle = session.version() != 0
            && !priming
            && !opening
            && !pending_press
            && !session.active()
            && !held.load(Ordering::SeqCst);
        if player.is_none() && idle {
            let due = last_player_try
                .map(|at| at.elapsed() > Duration::from_secs(3))
                .unwrap_or(true);
            if due {
                last_player_try = Some(Instant::now());
                match Player::open(endpoint.to_string()) {
                    Ok(opened) => {
                        player = Some(opened);
                        let _ = diag.send(Command::Diagnostic(serde_json::json!({
                            "type": "voice_link",
                            "detail": format!("播放端点已重新打开：{endpoint}"),
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
        match repair.poll(Instant::now(), idle) {
            RepairStep::Prime => {
                let quiet = repair.take_quiet_press();
                write_bytes(&tx_char, &atvv::mic_open_command(session.version()))?;
                repair.on_prime_started(Instant::now());
                priming = true;
                prime_saw_start = false;
                opening = true;
                opened_at = Instant::now();
                let _ = diag.send(Command::Diagnostic(serde_json::json!({
                    "type": "voice_link",
                    "detail": if quiet { "这次没有收到音频，正在重新准备麦克风" } else { "空闲时预先打开麦克风" },
                })));
            }
            RepairStep::Rebuild => {
                return Err("重新建立语音连接：麦克风没有应答".into());
            }
            RepairStep::Wait => {}
        }
        if session.active() && !priming && session.version() >= 0x0100 && last_extend.elapsed() >= Duration::from_millis(2500) {
            if let Some(stream) = session.stream().filter(|stream| *stream != 0) {
                let _ = write_bytes(&tx_char, &atvv::mic_extend_command(stream));
                last_extend = Instant::now();
            }
        }
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

/// Decides when an idle microphone should be opened and closed again.
///
/// The remote only starts a real recording after the host has already
/// opened and closed once. That warm-up used to wait 15 seconds, so the
/// next press in that window came back empty. A press that heard nothing
/// schedules the warm-up immediately. Two warm-ups with no answer at all
/// rebuild the Bluetooth session.
struct Repair {
    armed: bool,
    prime_not_before: Option<Instant>,
    last_prime: Option<Instant>,
    failed_primes: u8,
    quiet_press: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RepairStep {
    Wait,
    Prime,
    Rebuild,
}

impl Repair {
    fn new() -> Self {
        Self {
            armed: false,
            prime_not_before: None,
            last_prime: None,
            failed_primes: 0,
            quiet_press: false,
        }
    }

    fn on_press(&mut self) {
        self.armed = false;
    }

    fn on_release(&mut self, now: Instant, heard_audio: bool) {
        self.armed = false;
        self.prime_not_before = Some(now + Duration::from_millis(250));
        self.quiet_press = !heard_audio;
    }

    fn on_prime_started(&mut self, now: Instant) {
        self.last_prime = Some(now);
        self.prime_not_before = None;
    }

    fn on_prime_finished(&mut self, saw_audio_start: bool) {
        if saw_audio_start {
            self.armed = true;
            self.failed_primes = 0;
        } else {
            self.armed = false;
            self.failed_primes = self.failed_primes.saturating_add(1);
        }
    }

    fn take_quiet_press(&mut self) -> bool {
        let quiet = self.quiet_press;
        self.quiet_press = false;
        quiet
    }

    fn poll(&self, now: Instant, idle: bool) -> RepairStep {
        if !idle {
            return RepairStep::Wait;
        }
        if self.failed_primes >= 2 {
            return RepairStep::Rebuild;
        }
        if self.prime_not_before.is_some_and(|at| now < at) {
            return RepairStep::Wait;
        }
        let since_prime = self.last_prime.map(|at| now.saturating_duration_since(at));
        if self.armed {
            if since_prime.is_none() || since_prime.is_some_and(|age| age > Duration::from_secs(15)) {
                return RepairStep::Prime;
            }
            return RepairStep::Wait;
        }
        if since_prime.is_some_and(|age| age < Duration::from_millis(400)) {
            return RepairStep::Wait;
        }
        RepairStep::Prime
    }
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

#[cfg(test)]
mod tests {
    use super::{Repair, RepairStep};
    use std::time::{Duration, Instant};

    #[test]
    fn a_silent_press_is_repaired_without_waiting_out_the_idle_gap() {
        let mut repair = Repair::new();
        let start = Instant::now();
        repair.on_prime_started(start);
        repair.on_prime_finished(true);
        assert_eq!(repair.poll(start + Duration::from_secs(1), true), RepairStep::Wait);
        repair.on_press();
        repair.on_release(start + Duration::from_secs(2), false);
        assert_eq!(repair.poll(start + Duration::from_secs(2), true), RepairStep::Wait);
        assert_eq!(
            repair.poll(start + Duration::from_millis(2300), true),
            RepairStep::Prime
        );
        assert!(repair.take_quiet_press());
    }

    #[test]
    fn two_unanswered_warmups_rebuild_the_session() {
        let mut repair = Repair::new();
        let start = Instant::now();
        repair.on_prime_started(start);
        repair.on_prime_finished(false);
        assert_eq!(repair.poll(start + Duration::from_millis(500), true), RepairStep::Prime);
        repair.on_prime_started(start + Duration::from_millis(500));
        repair.on_prime_finished(false);
        assert_eq!(repair.poll(start + Duration::from_secs(1), true), RepairStep::Rebuild);
    }

    #[test]
    fn a_press_in_progress_does_not_start_another_warmup() {
        let mut repair = Repair::new();
        repair.on_release(Instant::now(), false);
        assert_eq!(repair.poll(Instant::now(), false), RepairStep::Wait);
    }
}
