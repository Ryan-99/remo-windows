//! Platform independent core for the Remo Windows backend.
//!
//! This crate deliberately has no Windows, BLE, Frida, or GUI dependency.
//! Hardware adapters emit `InputEvent`; this layer owns mapping, gesture
//! classification, configuration validation, and deterministic action events.

use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::time::{Duration, Instant};
use thiserror::Error;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Button {
    Mic,
    Power,
    Up,
    Down,
    Left,
    Right,
    Ok,
    Back,
    VolumeUp,
    VolumeDown,
    Home,
    Menu,
    Tv,
}

impl Button {
    pub const ALL: &'static [Button] = &[
        Button::Mic,
        Button::Power,
        Button::Up,
        Button::Down,
        Button::Left,
        Button::Right,
        Button::Ok,
        Button::Back,
        Button::VolumeUp,
        Button::VolumeDown,
        Button::Home,
        Button::Menu,
        Button::Tv,
    ];
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Edge {
    Down,
    Up,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InputEvent {
    pub button: Button,
    pub edge: Edge,
    pub at: Instant,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Trigger {
    Single,
    Double,
    Long,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Action {
    Disabled,
    Voice,
    KeyCombo { keys: Vec<String> },
    OpenApp {
        target: String,
        #[serde(default)]
        path: String,
    },
    SystemVolumeUp,
    SystemVolumeDown,
    ArrowUp,
    ArrowDown,
    ArrowLeft,
    ArrowRight,
    Return,
    DeleteBackward,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ActionEvent {
    pub button: Button,
    pub trigger: Trigger,
    pub action: Action,
}

#[derive(Debug, Error)]
pub enum MappingError {
    #[error("key combo cannot be empty")]
    EmptyCombo,
    #[error("unknown button mapping: {0}")]
    UnknownButton(String),
}

#[derive(Debug, Clone)]
struct Pending {
    down_at: Instant,
    released_at: Option<Instant>,
    clicks: u8,
    long_emitted: bool,
}

/// Deterministic gesture classifier. Single and double are delayed until the
/// double-click window; long is emitted by `tick` at the threshold and is not
/// repeated on release. Time-dependent behavior is therefore independent from
/// the platform input adapter's event cadence.
pub struct GestureEngine {
    long_press: Duration,
    double_window: Duration,
    pending: HashMap<Button, Pending>,
    /// When set, only buttons whose saved mapping has that secondary action
    /// wait for it. Other buttons emit a single click as soon as they release,
    /// matching the Python recognizer.
    selective: bool,
    doubles: HashSet<Button>,
    longs: HashSet<Button>,
}

impl GestureEngine {
    pub fn new(long_press: Duration, double_window: Duration) -> Self {
        Self {
            long_press,
            double_window,
            pending: HashMap::new(),
            selective: false,
            doubles: HashSet::new(),
            longs: HashSet::new(),
        }
    }

    pub fn follow_mapping(&mut self, mapping: &Mapping) {
        self.selective = true;
        self.doubles.clear();
        self.longs.clear();
        for button in Button::ALL {
            if mapping.resolve(*button, Trigger::Double) != Action::Disabled {
                self.doubles.insert(*button);
            }
            if mapping.resolve(*button, Trigger::Long) != Action::Disabled {
                self.longs.insert(*button);
            }
        }
    }

    pub fn input(&mut self, event: InputEvent) -> Vec<(Button, Trigger)> {
        if self
            .pending
            .get(&event.button)
            .is_some_and(|p| event.at < p.released_at.unwrap_or(p.down_at))
        {
            return Vec::new();
        }
        let mut out = self.tick(event.at);
        match event.edge {
            Edge::Down => {
                if let Some(p) = self.pending.get_mut(&event.button) {
                    if p.released_at.take().is_some() {
                        p.down_at = event.at;
                    }
                    return out;
                }
                self.pending.entry(event.button).or_insert(Pending {
                    down_at: event.at,
                    released_at: None,
                    clicks: 0,
                    long_emitted: false,
                });
            }
            Edge::Up => {
                if let Some(p) = self.pending.get_mut(&event.button) {
                    if p.released_at.is_some() {
                        return out;
                    }
                    if p.long_emitted {
                        self.pending.remove(&event.button);
                        return out;
                    }
                    if self.selective && !self.doubles.contains(&event.button) {
                        self.pending.remove(&event.button);
                        out.push((event.button, Trigger::Single));
                        return out;
                    }
                    p.released_at = Some(event.at);
                    p.clicks = p.clicks.saturating_add(1);
                    if p.clicks >= 2 {
                        self.pending.remove(&event.button);
                        out.push((event.button, Trigger::Double));
                    }
                }
            }
        }
        out
    }

    pub fn flush(&mut self, now: Instant) -> Vec<(Button, Trigger)> {
        let mut out = Vec::new();
        let expired: Vec<Button> = self
            .pending
            .iter()
            .filter_map(|(button, p)| {
                p.released_at
                    .filter(|released| {
                        now.saturating_duration_since(*released) >= self.double_window
                    })
                    .map(|_| *button)
            })
            .collect();
        for button in expired {
            self.pending.remove(&button);
            out.push((button, Trigger::Single));
        }
        out
    }

    /// Resolve time-dependent gestures without requiring another physical
    /// input event.  This is intentionally separate from `input` so platform
    /// adapters can call it from a small monotonic timer.
    pub fn tick(&mut self, now: Instant) -> Vec<(Button, Trigger)> {
        let mut out = Vec::new();
        for (button, pending) in self.pending.iter_mut() {
            if pending.released_at.is_none()
                && !pending.long_emitted
                && now.saturating_duration_since(pending.down_at) >= self.long_press
                && (!self.selective || self.longs.contains(button))
            {
                pending.long_emitted = true;
                // A short first tap followed by a hold is two gestures, not
                // a double click or a silently lost first tap.
                if pending.clicks == 1 {
                    out.push((*button, Trigger::Single));
                }
                out.push((*button, Trigger::Long));
            }
        }
        out.extend(self.flush(now));
        out
    }

    pub fn pending_count(&self) -> usize {
        self.pending.len()
    }

    /// Disconnect/shutdown must discard pending clicks, not execute them.
    pub fn cancel(&mut self) {
        self.pending.clear();
    }
}

#[derive(Debug, Clone, Default)]
pub struct Mapping {
    actions: HashMap<(Button, Trigger), Action>,
}

impl Mapping {
    pub fn set(&mut self, button: Button, trigger: Trigger, action: Action) {
        self.actions.insert((button, trigger), action);
    }
    pub fn resolve(&self, button: Button, trigger: Trigger) -> Action {
        self.actions
            .get(&(button, trigger))
            .cloned()
            .unwrap_or(Action::Disabled)
    }
}

pub fn dispatch(
    mapping: &Mapping,
    gestures: impl IntoIterator<Item = (Button, Trigger)>,
) -> Vec<ActionEvent> {
    gestures
        .into_iter()
        .map(|(button, trigger)| ActionEvent {
            button,
            trigger,
            action: mapping.resolve(button, trigger),
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    fn feed(g: &mut GestureEngine, t: Instant, ms: u64, edge: Edge) -> Vec<(Button, Trigger)> {
        g.input(InputEvent {
            button: Button::Ok,
            edge,
            at: t + Duration::from_millis(ms),
        })
    }
    #[test]
    fn duplicates_and_orphan_release_do_not_make_a_double() {
        let t = Instant::now();
        let mut g = GestureEngine::new(Duration::from_millis(500), Duration::from_millis(250));
        assert!(feed(&mut g, t, 0, Edge::Up).is_empty());
        feed(&mut g, t, 10, Edge::Down);
        feed(&mut g, t, 20, Edge::Down);
        feed(&mut g, t, 30, Edge::Up);
        assert!(feed(&mut g, t, 40, Edge::Up).is_empty());
        assert_eq!(
            g.tick(t + Duration::from_millis(280)),
            vec![(Button::Ok, Trigger::Single)]
        );
    }
    #[test]
    fn second_press_has_its_own_clock_and_survives_first_click_deadline() {
        let t = Instant::now();
        let mut g = GestureEngine::new(Duration::from_millis(500), Duration::from_millis(250));
        feed(&mut g, t, 0, Edge::Down);
        feed(&mut g, t, 200, Edge::Up);
        feed(&mut g, t, 440, Edge::Down);
        assert!(g.tick(t + Duration::from_millis(550)).is_empty());
        assert_eq!(
            feed(&mut g, t, 600, Edge::Up),
            vec![(Button::Ok, Trigger::Double)]
        );
    }
    #[test]
    fn expired_first_click_is_not_merged_with_second() {
        let t = Instant::now();
        let mut g = GestureEngine::new(Duration::from_millis(500), Duration::from_millis(250));
        feed(&mut g, t, 0, Edge::Down);
        feed(&mut g, t, 10, Edge::Up);
        assert_eq!(
            feed(&mut g, t, 260, Edge::Down),
            vec![(Button::Ok, Trigger::Single)]
        );
        feed(&mut g, t, 270, Edge::Up);
        assert_eq!(
            g.tick(t + Duration::from_millis(520)),
            vec![(Button::Ok, Trigger::Single)]
        );
    }
    #[test]
    fn long_once_and_cancel_does_not_dispatch() {
        let t = Instant::now();
        let mut g = GestureEngine::new(Duration::from_millis(500), Duration::from_millis(250));
        feed(&mut g, t, 0, Edge::Down);
        assert!(g.tick(t + Duration::from_millis(499)).is_empty());
        assert_eq!(
            g.tick(t + Duration::from_millis(500)),
            vec![(Button::Ok, Trigger::Long)]
        );
        assert!(g.tick(t + Duration::from_millis(900)).is_empty());
        assert!(feed(&mut g, t, 1000, Edge::Up).is_empty());
        feed(&mut g, t, 1100, Edge::Down);
        feed(&mut g, t, 1110, Edge::Up);
        g.cancel();
        assert!(g.tick(t + Duration::from_secs(2)).is_empty());
    }
    #[test]
    fn short_then_hold_retains_both_gestures() {
        let t = Instant::now();
        let mut g = GestureEngine::new(Duration::from_millis(500), Duration::from_millis(250));
        feed(&mut g, t, 0, Edge::Down);
        feed(&mut g, t, 10, Edge::Up);
        feed(&mut g, t, 50, Edge::Down);
        assert_eq!(
            g.tick(t + Duration::from_millis(550)),
            vec![(Button::Ok, Trigger::Single), (Button::Ok, Trigger::Long)]
        );
        assert!(feed(&mut g, t, 600, Edge::Up).is_empty());
    }
    #[test]
    fn out_of_order_release_does_not_clear_a_new_press() {
        let t = Instant::now();
        let mut g = GestureEngine::new(Duration::from_millis(500), Duration::from_millis(250));
        feed(&mut g, t, 100, Edge::Down);
        assert!(feed(&mut g, t, 50, Edge::Up).is_empty());
        assert_eq!(
            g.tick(t + Duration::from_millis(600)),
            vec![(Button::Ok, Trigger::Long)]
        );
    }
    #[test]
    fn single_is_delayed_until_double_window() {
        let t = Instant::now();
        let mut g = GestureEngine::new(Duration::from_millis(500), Duration::from_millis(250));
        assert!(g
            .input(InputEvent {
                button: Button::Ok,
                edge: Edge::Down,
                at: t
            })
            .is_empty());
        assert!(g
            .input(InputEvent {
                button: Button::Ok,
                edge: Edge::Up,
                at: t + Duration::from_millis(30)
            })
            .is_empty());
        assert_eq!(
            g.flush(t + Duration::from_millis(280)),
            vec![(Button::Ok, Trigger::Single)]
        );
    }
    #[test]
    fn second_release_is_double_and_long_is_immediate() {
        let t = Instant::now();
        let mut g = GestureEngine::new(Duration::from_millis(500), Duration::from_millis(250));
        g.input(InputEvent {
            button: Button::Mic,
            edge: Edge::Down,
            at: t,
        });
        g.input(InputEvent {
            button: Button::Mic,
            edge: Edge::Up,
            at: t + Duration::from_millis(20),
        });
        g.input(InputEvent {
            button: Button::Mic,
            edge: Edge::Down,
            at: t + Duration::from_millis(50),
        });
        assert_eq!(
            g.input(InputEvent {
                button: Button::Mic,
                edge: Edge::Up,
                at: t + Duration::from_millis(70)
            }),
            vec![(Button::Mic, Trigger::Double)]
        );
        g.input(InputEvent {
            button: Button::Mic,
            edge: Edge::Down,
            at: t + Duration::from_millis(100),
        });
        assert_eq!(
            g.tick(t + Duration::from_millis(600)),
            vec![(Button::Mic, Trigger::Long)]
        );
        assert!(g
            .input(InputEvent {
                button: Button::Mic,
                edge: Edge::Up,
                at: t + Duration::from_millis(700)
            })
            .is_empty());
    }

    #[test]
    fn tick_resolves_single_without_a_follow_up_input() {
        let t = Instant::now();
        let mut g = GestureEngine::new(Duration::from_millis(500), Duration::from_millis(100));
        g.input(InputEvent {
            button: Button::Home,
            edge: Edge::Down,
            at: t,
        });
        g.input(InputEvent {
            button: Button::Home,
            edge: Edge::Up,
            at: t + Duration::from_millis(10),
        });
        assert_eq!(
            g.tick(t + Duration::from_millis(109)),
            Vec::<(Button, Trigger)>::new()
        );
        assert_eq!(
            g.tick(t + Duration::from_millis(110)),
            vec![(Button::Home, Trigger::Single)]
        );
        assert_eq!(g.pending_count(), 0);
    }

    #[test]
    fn mapped_button_without_a_double_click_fires_on_release() {
        let t = Instant::now();
        let mut mapping = Mapping::default();
        mapping.set(Button::Left, Trigger::Single, Action::ArrowLeft);
        let mut g = GestureEngine::new(Duration::from_millis(500), Duration::from_millis(280));
        g.follow_mapping(&mapping);
        g.input(InputEvent {
            button: Button::Left,
            edge: Edge::Down,
            at: t,
        });
        assert_eq!(
            g.input(InputEvent {
                button: Button::Left,
                edge: Edge::Up,
                at: t + Duration::from_millis(40),
            }),
            vec![(Button::Left, Trigger::Single)]
        );
    }
}
