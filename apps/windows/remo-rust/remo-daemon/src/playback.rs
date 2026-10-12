//! Play decoded 16 kHz mono PCM on one named output endpoint.
//!
//! Remote speech goes to the saved endpoint, or to VB-CABLE when that name
//! is present. It is never opened on the default speakers.

use std::sync::mpsc::{self, Receiver, SyncSender};
use std::thread;
use std::time::Duration;

pub struct Player {
    tx: SyncSender<Vec<i16>>,
}

impl Player {
    pub fn open(endpoint_name: String) -> Result<Self, String> {
        let (tx, rx) = mpsc::sync_channel(32);
        let (ready_tx, ready_rx) = mpsc::channel();
        thread::spawn(move || run(&endpoint_name, rx, ready_tx));
        match ready_rx.recv_timeout(Duration::from_secs(3)) {
            Ok(Ok(())) => Ok(Self { tx }),
            Ok(Err(error)) => Err(error),
            Err(_) => Err("播放端点打开超时".into()),
        }
    }

    /// Returns false when the playback thread is gone and the caller should
    /// open a new player. A full queue still counts as alive.
    pub fn write(&self, samples: Vec<i16>) -> bool {
        if samples.is_empty() {
            return true;
        }
        !matches!(self.tx.try_send(samples), Err(mpsc::TrySendError::Disconnected(_)))
    }
}

#[cfg(windows)]
fn run(endpoint_name: &str, rx: Receiver<Vec<i16>>, ready: mpsc::Sender<Result<(), String>>) {
    use std::ptr::null_mut;
    use windows_sys::Win32::Media::Audio::{
        waveOutClose, waveOutOpen, waveOutPrepareHeader, waveOutReset, waveOutUnprepareHeader,
        waveOutWrite, HWAVEOUT, WAVEFORMATEX, WAVEHDR, WHDR_DONE,
    };
    unsafe {
        let device = match find_device(endpoint_name) {
            Ok(device) => device,
            Err(error) => {
                let _ = ready.send(Err(error));
                return;
            }
        };
        let mut format = |rate: u32, channels: u16| WAVEFORMATEX {
            wFormatTag: 1,
            nChannels: channels,
            nSamplesPerSec: rate,
            nAvgBytesPerSec: rate * u32::from(channels) * 2,
            nBlockAlign: channels * 2,
            wBitsPerSample: 16,
            cbSize: 0,
        };
        let mut handle: HWAVEOUT = null_mut();
        let mut rate = 16_000u32;
        let mut channels = 1u16;
        let mut opened = false;
        for candidate in [(16_000u32, 2u16), (16_000, 1), (48_000, 2), (48_000, 1), (44_100, 2)] {
            let spec = format(candidate.0, candidate.1);
            if waveOutOpen(
                &mut handle,
                device,
                &spec,
                0,
                0,
                0,
            ) == 0
            {
                rate = candidate.0;
                channels = candidate.1;
                opened = true;
                break;
            }
        }
        if !opened {
            let _ = ready.send(Err(format!("播放端点打不开 16-bit PCM：{endpoint_name}")));
            return;
        }
        let _ = ready.send(Ok(()));
        let mut live: Vec<*mut Slot> = Vec::new();
        let header_size = std::mem::size_of::<WAVEHDR>() as u32;
        loop {
            live.retain(|slot| unsafe {
                let slot = *slot;
                let done = std::ptr::addr_of!((*slot).header.dwFlags).read_unaligned() & WHDR_DONE != 0;
                if done {
                    waveOutUnprepareHeader(handle, std::ptr::addr_of_mut!((*slot).header), header_size);
                    drop(Box::from_raw(slot));
                }
                !done
            });
            match rx.recv_timeout(Duration::from_millis(40)) {
                Ok(samples) => {
                    if live.len() >= 8 {
                        continue;
                    }
                    let bytes = pcm_bytes(&samples, rate, channels);
                    let slot = Box::into_raw(Box::new(Slot {
                        bytes,
                        header: std::mem::zeroed(),
                    }));
                    let data = (*slot).bytes.as_mut_ptr();
                    let len = (*slot).bytes.len() as u32;
                    std::ptr::addr_of_mut!((*slot).header.lpData).write_unaligned(data);
                    std::ptr::addr_of_mut!((*slot).header.dwBufferLength).write_unaligned(len);
                    let header = std::ptr::addr_of_mut!((*slot).header);
                    if waveOutPrepareHeader(handle, header, header_size) != 0
                        || waveOutWrite(handle, header, header_size) != 0
                    {
                        drop(Box::from_raw(slot));
                        continue;
                    }
                    live.push(slot);
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {}
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
            }
        }
        waveOutReset(handle);
        for slot in live {
            waveOutUnprepareHeader(handle, std::ptr::addr_of_mut!((*slot).header), header_size);
            drop(Box::from_raw(slot));
        }
        waveOutClose(handle);
    }
}

#[cfg(windows)]
struct Slot {
    bytes: Vec<u8>,
    header: windows_sys::Win32::Media::Audio::WAVEHDR,
}

#[cfg(windows)]
fn find_device(endpoint_name: &str) -> Result<u32, String> {
    use windows_sys::Win32::Media::Audio::{waveOutGetDevCapsW, waveOutGetNumDevs, WAVEOUTCAPSW};
    let wanted = endpoint_name.trim();
    let count = unsafe { waveOutGetNumDevs() };
    let mut cable = None;
    let mut named = None;
    for id in 0..count {
        let mut caps = unsafe { std::mem::zeroed::<WAVEOUTCAPSW>() };
        if unsafe {
            waveOutGetDevCapsW(
                id as usize,
                &mut caps,
                std::mem::size_of::<WAVEOUTCAPSW>() as u32,
            )
        } != 0
        {
            continue;
        }
        let mut name_units = [0u16; 32];
        unsafe {
            std::ptr::copy_nonoverlapping(std::ptr::addr_of!(caps.szPname).cast::<u16>(), name_units.as_mut_ptr(), 32);
        }
        let name = device_name(&name_units);
        if !wanted.is_empty() && names_match(wanted, &name) {
            named = Some(id);
            break;
        }
        if name.to_ascii_lowercase().contains("cable input") {
            cable = Some(id);
        }
    }
    named.or(cable).ok_or_else(|| {
        if wanted.is_empty() {
            "没有找到 CABLE Input 播放端点".into()
        } else {
            format!("没有找到播放端点：{wanted}")
        }
    })
}

#[cfg(windows)]
fn device_name(raw: &[u16]) -> String {
    let end = raw.iter().position(|unit| *unit == 0).unwrap_or(raw.len());
    String::from_utf16_lossy(&raw[..end])
}

fn names_match(wanted: &str, actual: &str) -> bool {
    let wanted = wanted.to_ascii_lowercase();
    let actual = actual.to_ascii_lowercase();
    wanted.starts_with(actual.trim()) || actual.starts_with(wanted.trim()) || wanted.contains(&actual)
}

fn pcm_bytes(samples: &[i16], rate: u32, channels: u16) -> Vec<u8> {
    let expanded = if rate == 16_000 {
        samples.to_vec()
    } else {
        let ratio = rate as f32 / 16_000.0;
        let count = ((samples.len() as f32) * ratio).round() as usize;
        (0..count)
            .map(|index| {
                let source = index as f32 / ratio;
                let left = source.floor() as usize;
                let right = (left + 1).min(samples.len().saturating_sub(1));
                let mix = source - left as f32;
                let value = f32::from(samples[left]) * (1.0 - mix) + f32::from(samples[right]) * mix;
                value.round() as i16
            })
            .collect()
    };
    let mut bytes = Vec::with_capacity(expanded.len() * usize::from(channels) * 2);
    for sample in expanded {
        let encoded = sample.to_le_bytes();
        for _ in 0..channels {
            bytes.extend_from_slice(&encoded);
        }
    }
    bytes
}

#[cfg(not(windows))]
fn run(_endpoint_name: &str, _rx: Receiver<Vec<i16>>, ready: mpsc::Sender<Result<(), String>>) {
    let _ = ready.send(Err("语音播放仅支持 Windows".into()));
}

#[cfg(test)]
mod tests {
    use super::{names_match, pcm_bytes};

    #[test]
    fn truncated_cable_name_matches_the_saved_endpoint() {
        assert!(names_match(
            "CABLE Input (VB-Audio Virtual Cable)",
            "CABLE Input (VB-Audio Virtual C"
        ));
    }

    #[test]
    fn stereo_duplicates_each_sample() {
        assert_eq!(pcm_bytes(&[1, -2], 16_000, 2), vec![1, 0, 1, 0, 254, 255, 254, 255]);
    }
}
