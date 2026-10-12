//! Android TV Voice protocol used by the RC003 microphone.
//!
//! The opcodes, UUIDs and IMA ADPCM tables are the values the remote speaks.
//! Decoding matches the Python session: 16 kHz only, high nibble first.

pub const GET_CAPABILITIES: [u8; 6] = [0x0A, 0x01, 0x00, 0x00, 0x03, 0x03];
const AUDIO_STOP: u8 = 0x00;
const AUDIO_START: u8 = 0x04;
const MIC_BUTTON: u8 = 0x08;
const AUDIO_SYNC: u8 = 0x0A;
const CAPS: u8 = 0x0B;
const MIC_OPEN: u8 = 0x0C;
const MIC_CLOSE: u8 = 0x0D;
const MIC_EXTEND: u8 = 0x0E;
const START_SEARCH_ALT: u8 = 0x10;
const DEFAULT_FRAME: usize = 120;

const STEP_TABLE: [i32; 89] = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60, 66,
    73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449,
    494, 544, 598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272,
    2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493,
    10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
];
const INDEX_TABLE: [i32; 8] = [-1, -1, -1, -1, 2, 4, 6, 8];

#[derive(Debug, Clone, Copy)]
pub struct Capabilities {
    pub version: u16,
    pub frame_size: usize,
}

pub fn mic_open_command(version: u16) -> Vec<u8> {
    if version >= 0x0100 {
        vec![MIC_OPEN, 0x00]
    } else {
        vec![MIC_OPEN, 0x00, 0x00]
    }
}

pub fn mic_close_command(version: u16, stream: u8) -> Vec<u8> {
    if version >= 0x0100 {
        vec![MIC_CLOSE, stream]
    } else {
        vec![MIC_CLOSE]
    }
}

pub fn mic_extend_command(stream: u8) -> Vec<u8> {
    vec![MIC_EXTEND, stream]
}

pub fn parse_capabilities(data: &[u8]) -> Option<Capabilities> {
    if data.len() < 7 || data[0] != CAPS {
        return None;
    }
    let version = u16::from_be_bytes([data[1], data[2]]);
    let codecs = if version >= 0x0100 {
        let mut codecs = data[3];
        if codecs == 0 && data.len() >= 9 && data[4] & 0x03 != 0 {
            codecs = data[4];
        }
        codecs
    } else if data.len() >= 9 {
        data[4]
    } else {
        return None;
    };
    if codecs & 0x02 == 0 {
        return None;
    }
    let frame = usize::from(u16::from_be_bytes([data[5], data[6]]));
    Some(Capabilities {
        version,
        frame_size: if frame == 0 { DEFAULT_FRAME } else { frame },
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Control {
    Capabilities(u16),
    MicButton,
    AudioStarted(Option<u8>),
    AudioStopped,
    MicClosed,
    AudioSynced,
    Ignored,
}

struct Decoder {
    predictor: i32,
    step_index: i32,
}

impl Decoder {
    fn new() -> Self {
        Self {
            predictor: 0,
            step_index: 0,
        }
    }

    fn reset(&mut self, predictor: i32, step_index: i32) {
        self.predictor = predictor.clamp(-32768, 32767);
        self.step_index = step_index.clamp(0, 88);
    }

    fn nibble(&mut self, nibble: u8) -> i16 {
        let step = STEP_TABLE[self.step_index as usize];
        let mut difference = step >> 3;
        if nibble & 1 != 0 {
            difference += step >> 2;
        }
        if nibble & 2 != 0 {
            difference += step >> 1;
        }
        if nibble & 4 != 0 {
            difference += step;
        }
        if nibble & 8 != 0 {
            self.predictor -= difference;
        } else {
            self.predictor += difference;
        }
        self.predictor = self.predictor.clamp(-32768, 32767);
        self.step_index = (self.step_index + INDEX_TABLE[(nibble & 7) as usize]).clamp(0, 88);
        self.predictor as i16
    }

    fn decode(&mut self, frame: &[u8]) -> Vec<i16> {
        let mut samples = Vec::with_capacity(frame.len() * 2);
        for byte in frame {
            samples.push(self.nibble(byte >> 4));
            samples.push(self.nibble(byte & 0x0F));
        }
        samples
    }
}

struct HighPass {
    alpha: f32,
    previous_input: f32,
    previous_output: f32,
    ready: bool,
}

impl HighPass {
    fn new() -> Self {
        Self {
            alpha: (-2.0 * std::f32::consts::PI * 20.0 / 16000.0).exp(),
            previous_input: 0.0,
            previous_output: 0.0,
            ready: false,
        }
    }

    fn reset(&mut self) {
        self.previous_input = 0.0;
        self.previous_output = 0.0;
        self.ready = false;
    }

    fn process(&mut self, samples: &[i16]) -> Vec<i16> {
        if samples.is_empty() {
            return Vec::new();
        }
        if !self.ready {
            self.previous_input = samples[0] as f32;
            self.ready = true;
        }
        samples
            .iter()
            .map(|sample| {
                let current = *sample as f32;
                let output =
                    current - self.previous_input + self.alpha * self.previous_output;
                self.previous_input = current;
                self.previous_output = output;
                output.round().clamp(-32768.0, 32767.0) as i16
            })
            .collect()
    }
}

fn postprocess(samples: &[i16], gain_db: f32) -> Vec<i16> {
    if samples.is_empty() {
        return Vec::new();
    }
    let mut filtered = samples.to_vec();
    if samples.len() >= 3 {
        for index in 1..samples.len() - 1 {
            filtered[index] = ((i32::from(samples[index - 1])
                + 2 * i32::from(samples[index])
                + i32::from(samples[index + 1]))
                >> 2) as i16;
        }
    }
    let gain_db = gain_db.clamp(-24.0, 24.0);
    let gain = 10f32.powf(gain_db / 20.0);
    filtered
        .into_iter()
        .map(|value| ((f32::from(value) * gain).round().clamp(-32768.0, 32767.0)) as i16)
        .collect()
}

pub struct Session {
    decoder: Decoder,
    filter: HighPass,
    pending: Vec<u8>,
    pending_sync: Option<(i32, i32)>,
    frame_size: usize,
    version: u16,
    stream: Option<u8>,
    mic_open: bool,
    stopped_at: Option<std::time::Instant>,
    gain_db: f32,
}

impl Session {
    pub fn new(gain_db: f32) -> Self {
        Self {
            decoder: Decoder::new(),
            filter: HighPass::new(),
            pending: Vec::new(),
            pending_sync: None,
            frame_size: DEFAULT_FRAME,
            version: 0,
            stream: None,
            mic_open: false,
            stopped_at: None,
            gain_db,
        }
    }

    pub fn version(&self) -> u16 {
        self.version
    }

    pub fn stream(&self) -> Option<u8> {
        self.stream
    }

    pub fn frame_size(&self) -> usize {
        self.frame_size
    }

    pub fn active(&self) -> bool {
        self.mic_open
    }

    /// Drop the local open flag without waiting for an AUDIO_STOP.
    /// A lost stop notification must not block the next MIC_OPEN.
    pub fn force_idle(&mut self) {
        self.mic_open = false;
        self.stopped_at = None;
        self.stream = None;
        self.pending.clear();
        self.pending_sync = None;
    }

    pub fn handle_control(&mut self, payload: &[u8]) -> Control {
        if payload.is_empty() {
            return Control::Ignored;
        }
        match payload[0] {
            CAPS => {
                let Some(caps) = parse_capabilities(payload) else {
                    return Control::Ignored;
                };
                self.version = caps.version;
                self.frame_size = caps.frame_size;
                Control::Capabilities(caps.version)
            }
            MIC_BUTTON | START_SEARCH_ALT => Control::MicButton,
            AUDIO_START => {
                let stream = payload.get(3).copied();
                // The remote repeats AUDIO_START. Stream 0 is that repeat, not
                // a new recording. A real id that arrives after the repeat
                // still has to become the owner; the other way around must
                // not let 0 replace it.
                if self.mic_open {
                    let current = self.stream.unwrap_or(0);
                    let incoming = stream.unwrap_or(0);
                    if incoming == 0 || incoming == current || current != 0 {
                        return Control::Ignored;
                    }
                }
                self.decoder.reset(0, 0);
                self.filter.reset();
                self.pending.clear();
                self.pending_sync = None;
                self.mic_open = true;
                self.stopped_at = None;
                self.stream = stream;
                Control::AudioStarted(stream)
            }
            _ if payload == [0x00, 0x00] => Control::MicClosed,
            AUDIO_STOP => self.stop(),
            MIC_CLOSE if payload.len() >= 2 && payload[1] == 0 => self.stop(),
            AUDIO_SYNC if payload.len() >= 7 => {
                let predictor = i16::from_be_bytes([payload[4], payload[5]]) as i32;
                self.pending_sync = Some((predictor, i32::from(payload[6])));
                Control::AudioSynced
            }
            _ => Control::Ignored,
        }
    }

    fn stop(&mut self) -> Control {
        self.mic_open = false;
        self.stopped_at = Some(std::time::Instant::now());
        self.pending.clear();
        Control::AudioStopped
    }

    pub fn handle_audio(&mut self, payload: &[u8]) -> Vec<i16> {
        if !self.mic_open {
            if self
                .stopped_at
                .is_some_and(|stopped| stopped.elapsed() < std::time::Duration::from_millis(300))
            {
                return Vec::new();
            }
        }
        self.pending.extend_from_slice(payload);
        let mut samples = Vec::new();
        while self.pending.len() >= self.frame_size {
            let frame: Vec<u8> = self.pending.drain(..self.frame_size).collect();
            if let Some((predictor, index)) = self.pending_sync.take() {
                self.decoder.reset(predictor, index);
                self.filter.reset();
            }
            let decoded = self.decoder.decode(&frame);
            let centered = self.filter.process(&decoded);
            samples.extend(postprocess(&centered, self.gain_db));
        }
        samples
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sixteen_khz_capabilities_and_open_command() {
        let caps = parse_capabilities(&[0x0B, 0x01, 0x00, 0x02, 0x03, 0x00, 120]).unwrap();
        assert_eq!(caps.version, 0x0100);
        assert_eq!(caps.frame_size, 120);
        assert_eq!(mic_open_command(caps.version), vec![0x0C, 0x00]);
        assert!(parse_capabilities(&[0x0B, 0x00, 0x01, 0x00, 0x01, 0x00, 120, 0, 0]).is_none());
    }

    #[test]
    fn silence_frame_decodes_after_audio_start() {
        let mut session = Session::new(10.0);
        assert_eq!(
            session.handle_control(&[0x04, 0x00, 0x00, 0x01]),
            Control::AudioStarted(Some(1))
        );
        let samples = session.handle_audio(&[0u8; 120]);
        assert_eq!(samples.len(), 240);
        assert!(samples.iter().all(|sample| *sample == 0));
        assert_eq!(session.handle_control(&[0x00]), Control::AudioStopped);
        assert!(session.handle_audio(&[0u8; 120]).is_empty());
    }

    #[test]
    fn a_second_audio_start_does_not_replace_the_live_stream() {
        let mut session = Session::new(10.0);
        assert_eq!(
            session.handle_control(&[0x04, 0x00, 0x00, 0x12]),
            Control::AudioStarted(Some(0x12))
        );
        assert_eq!(
            session.handle_control(&[0x04, 0x00, 0x00, 0x00]),
            Control::Ignored
        );
        assert_eq!(session.stream(), Some(0x12));
        assert!(session.active());
        session.force_idle();
        assert!(!session.active());
        assert_eq!(session.stream(), None);
    }

    #[test]
    fn a_real_stream_replaces_the_zero_placeholder() {
        let mut session = Session::new(10.0);
        assert_eq!(
            session.handle_control(&[0x04, 0x00, 0x00, 0x00]),
            Control::AudioStarted(Some(0))
        );
        assert_eq!(
            session.handle_control(&[0x04, 0x00, 0x00, 0x15]),
            Control::AudioStarted(Some(0x15))
        );
        assert_eq!(session.stream(), Some(0x15));
    }

    #[test]
    fn first_nibble_of_seven_moves_the_predictor() {
        let mut decoder = Decoder::new();
        assert_eq!(decoder.nibble(0), 0);
        assert_eq!(decoder.nibble(7), 11);
    }
}
