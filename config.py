"""Configuration and persisted state for Ember.

Config is plain JSON rather than GSettings on purpose: Ember is standalone, so
there is no schema to compile and no way to get into a code/schema mismatch.
"""

import json
import os
import pathlib

APP_DIR = pathlib.Path(__file__).resolve().parent
CONFIG_DIR = pathlib.Path(os.path.expanduser("~/.config/ember"))
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_PATH = CONFIG_DIR / "state.json"
# Appended to by tracker.py; read by `python3 tracker.py report`.
HISTORY_PATH = CONFIG_DIR / "history.jsonl"
# The visible conversation. Kept apart from state.json because the runner
# rewrites that file at the end of every turn from a copy it read at the start,
# which would silently drop anything the UI wrote in between.
TRANSCRIPT_PATH = CONFIG_DIR / "transcript.json"
WORKSPACE = APP_DIR / "workspace"
SETTINGS_PATH = APP_DIR / "settings.json"

# The models on offer, cheapest first. `alias` is what the CLI takes; the CLI
# resolves each to the newest model of that family. The UI never shows model
# names -- each one is a mood instead, which says more about when to pick it.
MODELS = [
    {"alias": "haiku", "label": "Off the cuff", "blurb": "first thought, best thought"},
    {"alias": "sonnet", "label": "Thinking cap", "blurb": "the usual"},
    {"alias": "opus", "label": "Sleeves rolled up", "blurb": "for the stubborn stuff"},
    {"alias": "fable", "label": "Slow burn", "blurb": "go put the kettle on"},
]


def model_label(alias):
    for model in MODELS:
        if model["alias"] == alias:
            return model["label"]
    return (alias or "").capitalize()


# Warm pastels. `dot` is the accent itself, `text` is the darker sibling used
# for body copy so it stays readable on the cream surface.
ACCENTS = {
    "peach": {"dot": "#E3A897", "text": "#7A4535"},
    "sage": {"dot": "#A9C0A0", "text": "#3E5636"},
    "blue": {"dot": "#A6BBD6", "text": "#3A5170"},
    "lilac": {"dot": "#D8B4D8", "text": "#6B3E6B"},
}

DEFAULTS = {
    # Used in greetings and handed to the model. Blank it and every greeting
    # that would have used a name quietly drops out of the pool.
    "user_name": "Satvik",

    "accent": "peach",
    "surface": "#EFE6D8",
    "font_family": "Quicksand",
    "font_size": 18,
    # The resting dot is the whole presence on the desktop, so it has to read as
    # a deliberate object rather than a speck.
    "dot_size": 150,

    # Lives on the desktop layer, under working windows. Floating above turned
    # out to just clutter apps that have no spare UI room.
    "keep_above": False,

    # Window type, measured against GNOME 46 / mutter / X11:
    #   normal  - iconified by Super+D (show-desktop), so it vanishes with
    #             everything else. Un-minimising it again cancels show-desktop
    #             and drags every other window back too.
    #   dock    - survives show-desktop, but mutter repositions dock windows and
    #             denies them keyboard focus, so text input is impossible.
    #   desktop - exempt from show-desktop by spec; it *is* the desktop.
    "window_type": "desktop",

    # How long an untouched empty input stays open before folding back.
    "listen_timeout_seconds": 20,

    # Local offers shown before the "Ask Ember" row. Five is about the most
    # that can be scanned without reading, which is the point of a launcher.
    "max_results": 5,

    # Where the resting dot sits on screen (its centre). None means "auto
    # place bottom-centre on first run". Stored as the dot rather than the
    # window corner so the canvas can change size without the dot moving.
    "anchor_x": None,
    "anchor_y": None,

    # Kept close to the dot: at rest the card is invisible, so a wide hit region
    # would swallow desktop clicks where the user can see nothing.
    "idle_width": 64,
    "idle_height": 64,
    "active_width": 560,
    "active_height": 124,
    "response_height": 96,
    # Once a conversation starts the card widens into a chat panel. It grows
    # with the transcript up to this height, then scrolls.
    "chat_width": 700,
    "chat_max_height": 760,

    # Session rotation. Conversations used to be capped at 12 turns, which cut
    # real troubleshooting off mid-thread; 0 means no cap, and Ctrl+N is the
    # deliberate way to start over.
    "session_idle_minutes": 90,
    "session_max_turns": 0,

    # Reopening within this window brings the conversation back on screen;
    # after it, Ember opens as a plain launcher (the session still resumes if
    # you ask something, and the old messages come back with it).
    "chat_resume_minutes": 30,
    # An open chat nobody has touched for this long folds back to the dot.
    "chat_fold_minutes": 15,

    # A reply that took longer than this, finishing while Ember isn't focused,
    # raises a desktop notification -- long jobs are exactly the ones you
    # wander off from.
    "notify_after_seconds": 15,

    # Dwell before auto-collapse for local results (calculator, errors in the
    # launcher). Model replies never auto-collapse any more.
    "dwell_base_seconds": 3.5,
    "dwell_per_char_seconds": 0.035,
    "dwell_max_seconds": 14.0,

    # Log every interaction to history.jsonl so usage can be reviewed and the
    # frequent, deterministic requests moved off the model. Off means no record
    # is written at all -- there is nothing to redact, because nothing is kept.
    "track": True,

    # Default model for a new conversation. Haiku proved too weak even for
    # "small" jobs like Bluetooth pairing, so Sonnet is the floor; Haiku stays
    # one keystroke away for the genuinely trivial.
    "model": "sonnet",
    # Per-model --effort. Haiku is fast, so spend the thinking budget -- low
    # effort made it give up rather than go and look things up. Models not
    # listed use the CLI default.
    "effort": {"haiku": "high"},

    # Responses longer than this get the terminal handoff instead of trying to
    # cram them into a 420px pill.
    "handoff_char_threshold": 220,
    "terminal": "gnome-terminal",
}


# Shown in the same style as a real reply, not as grey placeholder text, so
# opening the widget feels like someone already there rather than an empty form.
#
# Two rules learned by reading these on the actual desktop. They must not all be
# questions -- a greeting that always ends in "what do you need?" puts the work
# back on the person the moment they open it, which is what made the first set
# read like a service desk. And they must vary in *shape*, not just wording;
# five near-identical lines feel more scripted than three varied ones. Some just
# signal presence and leave the silence open, which is the warmer thing to do.
# Roughly a third of each pool uses the name. Every line carrying it would be
# worse than none at all -- a name in every single greeting stops reading as
# recognition and starts reading as a mail merge.
GREETINGS = {
    "morning": [
        "Morning. What're we up to?",
        "Morning — take your time.",
        "Hey, morning. Where shall we start?",
        "Morning. I'm here whenever.",
        "Morning, {name}.",
        "Morning, {name}. Good to see you.",
    ],
    "afternoon": [
        "Hey. How's it going?",
        "Afternoon. What's on your mind?",
        "Hey there. What're we up to?",
        "Afternoon — I'm around.",
        "Hey, {name}. Good to see you.",
        "Afternoon, {name}. How's it going?",
    ],
    "evening": [
        "Evening. How was today?",
        "Evening — what's on your mind?",
        "Hey. Winding down, or still going?",
        "Evening. I'm around.",
        "Evening, {name}. How was today?",
        "Hey {name}. No rush.",
    ],
    "night": [
        "Still up? I'm around.",
        "Late one. What's on your mind?",
        "Hey. No rush.",
        "Still going? I'm here.",
        "Still up, {name}?",
        "Late one, {name}. What's keeping you up?",
    ],
}


def pick_greeting(now=None, name=None):
    import datetime
    import random

    hour = (now or datetime.datetime.now()).hour
    if hour < 5:
        slot = "night"
    elif hour < 12:
        slot = "morning"
    elif hour < 17:
        slot = "afternoon"
    elif hour < 22:
        slot = "evening"
    else:
        slot = "night"

    name = (name or "").strip()
    pool = GREETINGS[slot]
    if not name:
        # No name configured, so drop the lines that need one rather than
        # rendering "Morning, ." at somebody.
        pool = [line for line in pool if "{name}" not in line]
    return random.choice(pool).format(name=name)


def _read_json(path, fallback):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return dict(fallback)


def _write_json(path, data):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp, path)


# Bumped when a saved value needs rewriting rather than just a new default.
CONFIG_VERSION = 2


def _migrate(saved):
    """Bring an older config.json forward. Returns True when it changed."""
    if saved.get("config_version", 1) >= CONFIG_VERSION:
        return False
    # v1 stored the canvas corner; the canvas was 620x560 then.
    if saved.get("x") is not None and saved.get("y") is not None:
        saved.setdefault("anchor_x", saved["x"] + 310)
        saved.setdefault("anchor_y", saved["y"] + 280)
    for key in ("x", "y", "escalation_model"):
        saved.pop(key, None)
    # v1 pinned Haiku and a 12-turn cap explicitly; both are what made longer
    # conversations unusable, so they are reset rather than respected.
    if saved.get("model") == "haiku":
        saved["model"] = "sonnet"
    if isinstance(saved.get("effort"), str):
        saved["effort"] = {"haiku": saved["effort"]}
    for key in ("session_max_turns", "session_idle_minutes"):
        saved.pop(key, None)
    saved["config_version"] = CONFIG_VERSION
    return True


def load_config():
    """Defaults merged with whatever the user has saved, so new keys in a
    future version don't break an existing config file."""
    saved = _read_json(CONFIG_PATH, {})
    if saved and _migrate(saved):
        try:
            _write_json(CONFIG_PATH, saved)
        except OSError:
            pass
    config = dict(DEFAULTS)
    config.update(saved)
    return config


def save_config(config):
    _write_json(CONFIG_PATH, config)


def load_state():
    return _read_json(STATE_PATH, {"session_id": None, "last_used_at": 0, "turns": 0})


def save_state(state):
    _write_json(STATE_PATH, state)


def load_transcript():
    return _read_json(TRANSCRIPT_PATH, {"session_id": None, "messages": [], "model": None,
                                        "updated_at": 0})


def save_transcript(data):
    _write_json(TRANSCRIPT_PATH, data)


def accent_colors(config):
    return ACCENTS.get(config.get("accent"), ACCENTS["peach"])
