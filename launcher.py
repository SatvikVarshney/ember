"""Local resolution for Ember -- everything that must not cost a model call.

Deliberately free of GTK imports, same as runner.py, so the whole matcher can
be exercised from a terminal:  python3 launcher.py brave

The rule that keeps this honest lives in `resolve()`. Local results are only
offered when they are a *confident* match, and anything shaped like a sentence
skips app matching entirely. A launcher that hijacks "why is my wifi dropping"
to open Wi-Fi Settings is worse than one that quietly does nothing and lets the
model answer.
"""

import ast
import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import time

import config as cfg

USAGE_PATH = cfg.CONFIG_DIR / "usage.json"

# Score floor for an app to be offered at all. Below this the match is a
# coincidence and showing it would make Enter unpredictable.
MIN_APP_SCORE = 0.30

# Beyond this many words the input reads as prose, not a launch, so app
# matching is skipped. Two or three words still covers "text editor",
# "system monitor", "disk usage".
MAX_LAUNCH_WORDS = 3

_QUESTION_OPENERS = {
    "why", "how", "what", "whats", "when", "where", "who", "which", "whose",
    "is", "are", "was", "were", "can", "could", "should", "would", "will",
    "do", "does", "did", "am", "tell", "explain", "summarise", "summarize",
    "write", "draft", "find", "search", "look", "check", "show", "give",
}


# --------------------------------------------------------------------------
# result model
# --------------------------------------------------------------------------

class Result:
    """One offered row. `kind` decides what activation does, and is also what
    the UI uses to pick an icon."""

    __slots__ = ("kind", "title", "subtitle", "score", "payload", "icon")

    def __init__(self, kind, title, subtitle="", score=0.0, payload=None, icon=None):
        self.kind = kind
        self.title = title
        self.subtitle = subtitle
        self.score = score
        self.payload = payload or {}
        self.icon = icon

    def __repr__(self):
        return f"<{self.kind} {self.title!r} {self.score:.2f}>"


# --------------------------------------------------------------------------
# desktop entry index
# --------------------------------------------------------------------------

def _app_dirs():
    """XDG search path plus the two places Ubuntu hides packaged apps.

    Order matters: the first entry to claim a desktop id wins, so a user
    override in ~/.local/share shadows the system copy.
    """
    dirs = []
    home_data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    dirs.append(pathlib.Path(home_data) / "applications")
    for base in (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":"):
        if base:
            dirs.append(pathlib.Path(base) / "applications")
    dirs.append(pathlib.Path(home_data) / "flatpak/exports/share/applications")
    dirs.append(pathlib.Path("/var/lib/flatpak/exports/share/applications"))
    dirs.append(pathlib.Path("/var/lib/snapd/desktop/applications"))

    seen, out = set(), []
    for d in dirs:
        key = str(d)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def _parse_desktop(path):
    """Minimal [Desktop Entry] reader.

    Hand-rolled rather than configparser on purpose: Exec lines are full of
    field codes like %U and %F, which configparser's interpolation treats as
    syntax errors, and duplicate keys in the wild make it raise outright.
    """
    data = {}
    in_main = False
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if line.startswith("["):
                    # [Desktop Action new-window] groups are separate launchers
                    # we deliberately don't index -- they'd double every entry.
                    in_main = line == "[Desktop Entry]"
                    continue
                if not in_main or not line or line.startswith("#"):
                    continue
                key, sep, value = line.partition("=")
                if not sep:
                    continue
                key = key.strip()
                # Name[de]=... -- localised variants are noise; the plain key
                # is the one to keep, and setdefault makes the first win.
                if "[" in key:
                    continue
                data.setdefault(key, value.strip())
    except OSError:
        return {}
    return data


_FIELD_CODES = re.compile(r"%[fFuUdDnNickvm]")


class AppEntry:
    __slots__ = ("app_id", "path", "name", "generic", "keywords", "exec_name", "icon")

    def __init__(self, app_id, path, name, generic, keywords, exec_name, icon):
        self.app_id = app_id
        self.path = path
        self.name = name
        self.generic = generic
        self.keywords = keywords
        self.exec_name = exec_name
        self.icon = icon


def _entry_from(path):
    data = _parse_desktop(path)
    if data.get("Type", "Application") != "Application":
        return None
    if data.get("NoDisplay", "").lower() == "true":
        return None
    if data.get("Hidden", "").lower() == "true":
        return None
    name = data.get("Name")
    execline = data.get("Exec")
    if not name or not execline:
        return None

    # TryExec is the entry's own statement that it needs a binary that isn't
    # there. Honour it only for absolute paths -- a bare name can legitimately
    # be missing from PATH for AppImages and wrappers.
    tryexec = data.get("TryExec")
    if tryexec and tryexec.startswith("/") and not os.path.exists(tryexec):
        return None

    first = _FIELD_CODES.sub("", execline).strip().split()
    exec_name = os.path.basename(first[0]) if first else ""

    keywords = [k for k in re.split(r"[;,]", data.get("Keywords", "")) if k]
    return AppEntry(
        app_id=os.path.basename(path),
        path=str(path),
        name=name,
        generic=data.get("GenericName", ""),
        keywords=keywords,
        exec_name=exec_name,
        icon=data.get("Icon", ""),
    )


class AppIndex:
    """In-memory index, rebuilt only when one of the search dirs changes.

    Ember is a long-running daemon, so the scan cost is paid once. The mtime
    fingerprint means installing an app makes it findable without a restart,
    without re-reading 190 files on every keystroke.
    """

    def __init__(self):
        self.entries = []
        self._fingerprint = None

    @staticmethod
    def _fingerprint_dirs():
        marks = []
        for d in _app_dirs():
            try:
                marks.append((str(d), d.stat().st_mtime_ns))
            except OSError:
                marks.append((str(d), 0))
        return tuple(marks)

    def refresh(self, force=False):
        fingerprint = self._fingerprint_dirs()
        if not force and fingerprint == self._fingerprint:
            return False

        entries, claimed = [], set()
        for directory in _app_dirs():
            try:
                paths = sorted(directory.glob("*.desktop"))
            except OSError:
                continue
            for path in paths:
                app_id = path.name
                if app_id in claimed:
                    continue
                entry = _entry_from(path)
                if entry is not None:
                    claimed.add(app_id)
                    entries.append(entry)

        self.entries = entries
        self._fingerprint = fingerprint
        return True


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------

_WORD_START = re.compile(r"(?:^|[\s\-_.])(\w)")


def _subsequence_from(needle, haystack, start):
    it = iter(range(start, len(haystack)))
    last = start
    for char in needle:
        for i in it:
            if haystack[i] == char:
                last = i
                break
        else:
            return 0.0
    span = (last - start) + 1
    return len(needle) / span


def _is_subsequence(needle, haystack):
    """Fuzzy 'brwsr' -> 'browser'. Returns a density score, or 0.0.

    Two constraints keep this from generating noise. The match must *start* on
    a word boundary, which is how people actually abbreviate and is what stops
    "mute" matching the m-u-t-e buried inside "Firmware Updater". And density
    (matched length over the span it took) stops a short query scoring well
    against a long name it happens to be scattered through.
    """
    best = 0.0
    for match in _WORD_START.finditer(haystack):
        start = match.start(1)
        if haystack[start] != needle[0]:
            continue
        best = max(best, _subsequence_from(needle, haystack, start))
    return best


def _score_text(query, text, allow_fuzzy):
    """Tiered match quality in 0..1, or 0.0 for no match."""
    if not text:
        return 0.0
    text = text.lower()
    if text == query:
        return 1.0
    if text.startswith(query):
        # Longer names are weaker prefix matches: "bra" should prefer "Brave"
        # over "Brave Browser Beta Channel Nightly".
        return 0.90 - min(0.15, (len(text) - len(query)) / 200.0)

    words = re.split(r"[\s\-_.]+", text)
    if any(w.startswith(query) for w in words if w):
        return 0.75

    query_words = query.split()
    if len(query_words) > 1:
        # "text editor" -> "Text Editor". Every query word must lead a word in
        # the target, which is what keeps multi-word input from matching noise.
        if all(any(w.startswith(qw) for w in words if w) for qw in query_words):
            return 0.72
        if query in text:
            return 0.55
        return 0.0

    if query in text:
        return 0.55
    if allow_fuzzy and len(query) >= 3:
        density = _is_subsequence(query, text)
        if density:
            return 0.30 + 0.20 * density
    return 0.0


_FIELD_WEIGHTS = (("name", 1.00), ("exec_name", 0.72), ("generic", 0.62))


def _score_entry(query, entry, allow_fuzzy):
    best = 0.0
    for field, weight in _FIELD_WEIGHTS:
        value = getattr(entry, field)
        if value:
            best = max(best, _score_text(query, value, allow_fuzzy) * weight)
    for keyword in entry.keywords:
        best = max(best, _score_text(query, keyword, False) * 0.55)
    return best


def looks_like_prose(query):
    """True when the input should go straight to the model.

    Two cheap signals do nearly all the work: length, and an opening word that
    only ever starts a question or an instruction.
    """
    stripped = query.strip()
    if not stripped:
        return False
    if stripped.endswith("?"):
        return True
    words = stripped.split()
    if len(words) > MAX_LAUNCH_WORDS:
        return True
    if len(words) > 1 and words[0].lower().strip(",'") in _QUESTION_OPENERS:
        return True
    return False


# --------------------------------------------------------------------------
# frecency
# --------------------------------------------------------------------------

def load_usage():
    try:
        with open(USAGE_PATH) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_usage(usage):
    cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = USAGE_PATH.with_suffix(".tmp")
    with open(tmp, "w") as handle:
        json.dump(usage, handle, indent=2)
    os.replace(tmp, USAGE_PATH)


def record_launch(app_id, usage=None):
    usage = load_usage() if usage is None else usage
    record = usage.setdefault(app_id, {"count": 0, "last": 0})
    record["count"] = record.get("count", 0) + 1
    record["last"] = int(time.time())
    save_usage(usage)
    return usage


def _frecency_boost(record, now):
    """Capped additive bonus. Capped on purpose: frequency should break ties
    and promote a good match, never drag an unrelated app to the top."""
    if not record:
        return 0.0
    count = record.get("count", 0)
    if count <= 0:
        return 0.0
    last = record.get("last", 0)
    recency = 1.0
    if last:
        days = max(0.0, (now - last) / 86400.0)
        recency = 0.5 + 0.5 * math.exp(-days / 21.0)
    return min(0.30, 0.11 * math.log1p(count)) * recency


# --------------------------------------------------------------------------
# calculator
# --------------------------------------------------------------------------

_MATH_FUNCS = {
    "sqrt": math.sqrt, "abs": abs, "round": round, "min": min, "max": max,
    "log": math.log, "log2": math.log2, "log10": math.log10, "ln": math.log,
    "sin": math.sin, "cos": math.cos, "tan": math.tan, "floor": math.floor,
    "ceil": math.ceil, "exp": math.exp,
}
_MATH_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}

_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Call, ast.Name,
    ast.Load, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
    ast.Pow, ast.USub, ast.UAdd,
)

_PERCENT_OF = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*of\s+", re.I)
_TRAILING_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*$")


def _prepare_expression(text):
    """Rewrite the two percent forms people actually type.

    '%' stays modulo everywhere else, which keeps the behaviour predictable
    rather than guessing at intent mid-expression.
    """
    text = text.replace("×", "*").replace("÷", "/").replace("^", "**")
    text = _PERCENT_OF.sub(r"(\1/100)*", text)
    text = _TRAILING_PERCENT.sub(r"(\1/100)", text)
    return text.strip()


def _has_operation(tree):
    """A bare number is not a calculation, so '42' must not produce a row."""
    return any(isinstance(n, (ast.BinOp, ast.UnaryOp, ast.Call)) for n in ast.walk(tree))


def _eval_node(node):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("non-numeric constant")
    if isinstance(node, ast.Name):
        if node.id in _MATH_CONSTS:
            return _MATH_CONSTS[node.id]
        raise ValueError(f"unknown name {node.id}")
    if isinstance(node, ast.UnaryOp):
        value = _eval_node(node.operand)
        return -value if isinstance(node.op, ast.USub) else +value
    if isinstance(node, ast.BinOp):
        left, right = _eval_node(node.left), _eval_node(node.right)
        if isinstance(node.op, ast.Pow):
            # 9**9**9 would hang the UI thread solid; cap it well below that.
            if abs(right) > 512 or (abs(left) > 1e6 and abs(right) > 8):
                raise ValueError("exponent too large")
            return left ** right
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if isinstance(node.op, ast.FloorDiv):
            return left // right
        if isinstance(node.op, ast.Mod):
            return left % right
        raise ValueError("unsupported operator")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _MATH_FUNCS:
            raise ValueError("unsupported call")
        if node.keywords:
            raise ValueError("no keyword arguments")
        return _MATH_FUNCS[node.func.id](*[_eval_node(a) for a in node.args])
    raise ValueError("unsupported expression")


def _format_number(value):
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return str(value)
        if value.is_integer() and abs(value) < 1e15:
            value = int(value)
    if isinstance(value, int):
        return f"{value:,}"
    text = f"{value:,.10g}"
    return text


def calculate(query):
    """Return the formatted result, or None if this isn't a calculation."""
    if not any(char.isdigit() for char in query):
        return None
    prepared = _prepare_expression(query)
    if not prepared:
        return None
    try:
        tree = ast.parse(prepared, mode="eval")
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            return None
    if not _has_operation(tree):
        return None
    try:
        value = _eval_node(tree)
    except (ValueError, TypeError, ZeroDivisionError, OverflowError, ArithmeticError):
        return None
    if not isinstance(value, (int, float)):
        return None
    return _format_number(value)


# --------------------------------------------------------------------------
# system actions
# --------------------------------------------------------------------------

def _sink_volume(delta):
    return ["pactl", "set-sink-volume", "@DEFAULT_SINK@", delta]


# (aliases, title, subtitle, argv)
_ACTIONS = [
    (("volume up", "vol up", "louder", "audio up"),
     "Volume up", "+5%", _sink_volume("+5%")),
    (("volume down", "vol down", "quieter", "audio down"),
     "Volume down", "-5%", _sink_volume("-5%")),
    (("mute", "volume mute", "silence"),
     "Mute", "toggle audio", ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"]),
    (("mic mute", "microphone mute"),
     "Mute microphone", "toggle input", ["pactl", "set-source-mute", "@DEFAULT_SOURCE@", "toggle"]),
    (("brightness up", "screen up", "brighter"),
     "Brightness up", "+10%", ["brightnessctl", "set", "+10%"]),
    (("brightness down", "screen down", "dimmer"),
     "Brightness down", "10%-", ["brightnessctl", "set", "10%-"]),
    (("lock", "lock screen", "lock session"),
     "Lock screen", "", ["loginctl", "lock-session"]),
    (("suspend", "sleep"),
     "Suspend", "", ["systemctl", "suspend"]),
    (("wifi on", "wi-fi on"),
     "Wi-Fi on", "", ["nmcli", "radio", "wifi", "on"]),
    (("wifi off", "wi-fi off"),
     "Wi-Fi off", "", ["nmcli", "radio", "wifi", "off"]),
    (("bluetooth on",),
     "Bluetooth on", "", ["rfkill", "unblock", "bluetooth"]),
    (("bluetooth off",),
     "Bluetooth off", "", ["rfkill", "block", "bluetooth"]),
    (("play", "pause", "play pause"),
     "Play / pause", "", ["playerctl", "play-pause"]),
    (("next", "next track", "skip"),
     "Next track", "", ["playerctl", "next"]),
    (("previous", "previous track", "prev"),
     "Previous track", "", ["playerctl", "previous"]),
]

_VOLUME_SET = re.compile(r"^(?:volume|vol)\s+(\d{1,3})$", re.I)
_BRIGHT_SET = re.compile(r"^(?:brightness|bright)\s+(\d{1,3})$", re.I)


def _action_results(query):
    out = []
    lowered = query.lower().strip()

    match = _VOLUME_SET.match(lowered)
    if match:
        level = min(150, int(match.group(1)))
        return [Result("action", f"Volume {level}%", "set output volume", 0.99,
                       {"argv": _sink_volume(f"{level}%")}, "audio-volume-high-symbolic")]

    match = _BRIGHT_SET.match(lowered)
    if match:
        level = min(100, int(match.group(1)))
        return [Result("action", f"Brightness {level}%", "set screen brightness", 0.99,
                       {"argv": ["brightnessctl", "set", f"{level}%"]}, "display-brightness-symbolic")]

    for aliases, title, subtitle, argv in _ACTIONS:
        if not shutil.which(argv[0]):
            continue
        best = 0.0
        for alias in aliases:
            if lowered == alias:
                best = max(best, 0.99)
            elif alias.startswith(lowered) and len(lowered) >= 2:
                best = max(best, 0.80)
        if best:
            out.append(Result("action", title, subtitle, best, {"argv": argv}, "emblem-system-symbolic"))
    return out


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------

def resolve(query, index, usage=None, limit=5):
    """Rank every local answer for `query`, best first.

    Calculator and system actions are evaluated regardless of shape -- they
    have exact triggers, so they cannot fire by accident. Only app matching is
    gated behind the prose check.
    """
    query = (query or "").strip()
    if not query:
        return []

    usage = load_usage() if usage is None else usage
    now = time.time()
    results = []

    answer = calculate(query)
    if answer is not None:
        results.append(Result("calc", answer, query.strip(), 1.0,
                              {"value": answer}, "accessories-calculator-symbolic"))

    results.extend(_action_results(query))

    if not looks_like_prose(query):
        lowered = query.lower()
        allow_fuzzy = " " not in lowered
        for entry in index.entries:
            score = _score_entry(lowered, entry, allow_fuzzy)
            if score < MIN_APP_SCORE:
                continue
            score += _frecency_boost(usage.get(entry.app_id), now)
            results.append(Result(
                "app", entry.name, entry.generic or entry.exec_name, score,
                {"app_id": entry.app_id, "path": entry.path}, entry.icon or "application-x-executable",
            ))

    results.sort(key=lambda r: -r.score)
    return results[:limit]


# --------------------------------------------------------------------------
# activation
# --------------------------------------------------------------------------

def launch_app(result):
    """Gio rather than gtk-launch: it applies Exec field codes, Terminal=true
    and DBusActivatable correctly, and it works for entries in the snap and
    flatpak dirs that gtk-launch cannot always resolve by id."""
    from gi.repository import Gio

    path = result.payload.get("path")
    info = Gio.DesktopAppInfo.new_from_filename(path) if path else None
    if info is None:
        raise RuntimeError(f"no launchable entry at {path}")
    info.launch([], None)
    record_launch(result.payload.get("app_id") or os.path.basename(path or ""))


def run_action(result):
    argv = result.payload.get("argv")
    if not argv:
        return
    # start_new_session so the child outlives Ember and never inherits its
    # controlling terminal.
    subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def activate(result):
    if result.kind == "app":
        launch_app(result)
    elif result.kind == "action":
        run_action(result)


if __name__ == "__main__":
    import sys

    index = AppIndex()
    started = time.time()
    index.refresh()
    scan_ms = (time.time() - started) * 1000

    query = " ".join(sys.argv[1:])
    print(f"[index] {len(index.entries)} apps in {scan_ms:.1f}ms")
    if not query:
        sys.exit(0)

    print(f"[prose] {looks_like_prose(query)}  ->  "
          f"{'model' if looks_like_prose(query) else 'local matching allowed'}")
    started = time.time()
    hits = resolve(query, index)
    print(f"[resolve] {(time.time() - started) * 1000:.2f}ms")
    if not hits:
        print("  (no local match -- would go to Haiku)")
    for hit in hits:
        print(f"  {hit.score:5.2f}  {hit.kind:6}  {hit.title}"
              + (f"   -- {hit.subtitle}" if hit.subtitle else ""))
