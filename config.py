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
WORKSPACE = APP_DIR / "workspace"
SETTINGS_PATH = APP_DIR / "settings.json"

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
    "dot_size": 76,

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

    # None means "auto place bottom-center on first run".
    "x": None,
    "y": None,

    # Kept close to the dot: at rest the card is invisible, so a wide hit region
    # would swallow desktop clicks where the user can see nothing.
    "idle_width": 64,
    "idle_height": 64,
    "active_width": 560,
    "active_height": 124,
    "response_height": 96,

    # Session rotation. The turn cap exists because resume latency grows with
    # history -- without it the widget gets slower as the day goes on.
    "session_idle_minutes": 30,
    "session_max_turns": 12,

    # Dwell before auto-collapse, scaled by response length so long answers
    # aren't yanked away mid-read.
    "dwell_base_seconds": 3.5,
    "dwell_per_char_seconds": 0.035,
    "dwell_max_seconds": 14.0,

    "model": "haiku",
    "escalation_model": "sonnet",
    # Haiku is fast, so spend the thinking budget -- low effort made it give up
    # rather than go and look things up.
    "effort": "high",

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


def load_config():
    """Defaults merged with whatever the user has saved, so new keys in a
    future version don't break an existing config file."""
    config = dict(DEFAULTS)
    config.update(_read_json(CONFIG_PATH, {}))
    return config


def save_config(config):
    _write_json(CONFIG_PATH, config)


def load_state():
    return _read_json(STATE_PATH, {"session_id": None, "last_used_at": 0, "turns": 0})


def save_state(state):
    _write_json(STATE_PATH, state)


def accent_colors(config):
    return ACCENTS.get(config.get("accent"), ACCENTS["peach"])
