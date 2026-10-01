#!/usr/bin/env python3
"""Usage tracking for Ember -- what was asked, what happened, and how.

The point is not analytics for its own sake. Every model turn costs a `claude -p`
round trip of several seconds, and a lot of what gets asked is the same handful
of things over and over. This records enough about each interaction to answer one
question: *which requests are frequent enough, and deterministic enough, that
they should be handled locally instead of by a model?* The `report` command ranks
exactly that.

The thing that makes an answer possible is `how.commands` -- the actual shell
commands the model ran. Knowing "he asks about wifi a lot" is not enough to write
a local handler; knowing that every one of those turns ends in `nmcli device wifi
list` is.

GTK-free on purpose, like runner.py and launcher.py, so the whole thing can be
exercised from a terminal:

    python3 tracker.py report
    python3 tracker.py tail 20

Storage is JSON Lines rather than one JSON document. A single array would mean
read-parse-rewrite on every interaction -- wasteful on the UI thread, and a crash
mid-rewrite loses the entire history rather than one line. Appending is one
write, and a truncated final line costs exactly that line.
"""

import json
import os
import pathlib
import re
import threading
import time

CONFIG_DIR = pathlib.Path(os.path.expanduser("~/.config/ember"))
HISTORY_PATH = CONFIG_DIR / "history.jsonl"

# Rotation keeps the file scannable. At roughly 400 bytes a record this is on the
# order of 10k interactions per file, which is far more than the report window
# ever looks at.
MAX_BYTES = 4 * 1024 * 1024
KEEP_ROTATIONS = 3

# Responses are stored truncated: the report never needs the whole thing, and a
# long web-search answer would dwarf every other field in the file.
RESPONSE_CAP = 400
PROMPT_CAP = 500

_lock = threading.Lock()


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

def _rotate_if_needed():
    try:
        if HISTORY_PATH.stat().st_size < MAX_BYTES:
            return
    except OSError:
        return
    for n in range(KEEP_ROTATIONS - 1, 0, -1):
        older = HISTORY_PATH.with_suffix(f".{n}.jsonl")
        newer = HISTORY_PATH.with_suffix(f".{n - 1}.jsonl") if n > 1 else HISTORY_PATH
        try:
            if newer.exists():
                os.replace(newer, older)
        except OSError:
            pass


def log(record):
    """Append one interaction. Never raises.

    Tracking is strictly a bystander: if the disk is full or the directory is
    gone, Ember carries on and the interaction simply goes unrecorded. A widget
    that stops answering because its usage log failed would be a bad trade.
    """
    try:
        record = dict(record)
        record.setdefault("ts", time.time())
        record.setdefault("at", time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record["ts"])))
        if "prompt" in record and record["prompt"]:
            record["prompt"] = record["prompt"][:PROMPT_CAP]
            record.setdefault("norm", normalize(record["prompt"]))
        if record.get("response"):
            record["response"] = record["response"][:RESPONSE_CAP]
        record.setdefault("intent", classify(record.get("norm", ""),
                                            (record.get("how") or {}).get("commands"),
                                            record.get("route")))

        with _lock:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            _rotate_if_needed()
            with open(HISTORY_PATH, "a") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - deliberately total; see docstring
        pass


def load(days=None, path=None):
    """Every record, oldest first, optionally limited to the last `days`.

    Rotated files are read too, so a report window longer than one file still
    sees the whole period.
    """
    cutoff = time.time() - days * 86400 if days else 0
    paths = [path] if path else (
        [HISTORY_PATH.with_suffix(f".{n}.jsonl") for n in range(KEEP_ROTATIONS - 1, 0, -1)]
        + [HISTORY_PATH]
    )
    out = []
    for candidate in paths:
        try:
            with open(candidate) as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue  # A torn last line from a crash; skip it.
                    if record.get("ts", 0) >= cutoff:
                        out.append(record)
        except OSError:
            continue
    out.sort(key=lambda r: r.get("ts", 0))
    return out


# --------------------------------------------------------------------------
# normalisation and intent
# --------------------------------------------------------------------------

# Stripped so "can you open brave" and "open brave please" land in the same
# bucket. Frequency counting is useless if politeness splits a cluster in three.
_FILLER = re.compile(
    r"^\s*(?:hey|hi|ok|okay|please|pls|can you|could you|would you|will you|"
    r"i want to|i need to|i'd like to|let's|lets|go|just)\s+",
    re.I,
)
_TRAILING = re.compile(r"\s*(?:please|pls|thanks|thank you|for me|now)\s*[.!?]*\s*$", re.I)
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?%?\b")
_PUNCT = re.compile(r"[^\w\s<>%+\-*/]")


def normalize(text):
    """A clustering key, not a display string.

    Numbers collapse to <n> so "volume 40" and "volume 70" are recognised as one
    recurring request with an argument -- which is precisely the shape that wants
    a local handler rather than a model.
    """
    text = (text or "").strip().lower()
    for _ in range(3):  # "hey can you please ..." stacks
        stripped = _FILLER.sub("", text)
        if stripped == text:
            break
        text = stripped
    text = _TRAILING.sub("", text)
    text = _NUMBER.sub("<n>", text)
    text = _PUNCT.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


# Ordered: the first match wins, so the specific patterns sit above the general
# ones. Matched against the normalised prompt.
_INTENT_PROMPT = [
    ("open_app",    r"^(?:open|launch|start|run|fire up|bring up)\b"),
    ("close_app",   r"^(?:close|quit|kill|stop|exit)\b"),
    ("volume",      r"\b(?:volume|vol|mute|unmute|louder|quieter|sound)\b"),
    ("brightness",  r"\b(?:brightness|dim|brighter|screen light)\b"),
    ("media",       r"\b(?:play|pause|next track|skip|previous|spotify|song|music)\b"),
    ("bluetooth",   r"\b(?:bluetooth|pair|headphones|earbuds|speaker)\b"),
    ("wifi",        r"\b(?:wifi|wi-fi|network|internet|hotspot|vpn|ssid)\b"),
    ("install",     r"\b(?:install|uninstall|reinstall|remove package|apt|update packages)\b"),
    ("system_info", r"\b(?:battery|disk|space|memory|ram|cpu|temperature|uptime|processes)\b"),
    ("files",       r"\b(?:file|folder|directory|downloads|screenshot|rename|move|copy)\b"),
    ("display",     r"\b(?:monitor|display|resolution|night light|dark mode)\b"),
    ("web_lookup",  r"^(?:what|who|when|where|why|how|is|are|does|did)\b|\b(?:news|weather|search|look up|score|price)\b"),
    ("clipboard",   r"\b(?:clipboard|copy that|paste)\b"),
]
_INTENT_PROMPT = [(name, re.compile(pattern)) for name, pattern in _INTENT_PROMPT]

# Fallback when the wording is unusual but the commands are not. What a turn
# actually *ran* is often a cleaner label than what it was asked.
_INTENT_COMMAND = [
    ("open_app",    r"^(?:gtk-launch|xdg-open|gio launch)\b"),
    ("close_app",   r"^(?:pkill|killall|kill)\b"),
    ("volume",      r"^(?:pactl|amixer|wpctl)\b"),
    ("brightness",  r"^brightnessctl\b"),
    ("bluetooth",   r"^bluetoothctl\b"),
    ("wifi",        r"^(?:nmcli|iwctl|ping)\b"),
    ("install",     r"^sudo ember-admin\b"),
    ("system_info", r"^(?:free|df|du|uptime|sensors|top|ps|upower|acpi)\b"),
    ("media",       r"^(?:playerctl|spotify)\b"),
]
_INTENT_COMMAND = [(name, re.compile(pattern)) for name, pattern in _INTENT_COMMAND]


# How the request was resolved settles the intent more reliably than its
# wording does: a bare "brave" typed at the launcher says nothing on its own, but
# the fact that it activated an app row says everything.
_INTENT_ROUTE = {"app": "open_app", "calc": "calc", "handoff": "handoff"}


def classify(norm, commands=None, route=None):
    if route in _INTENT_ROUTE:
        return _INTENT_ROUTE[route]
    for name, pattern in _INTENT_PROMPT:
        if pattern.search(norm or ""):
            return name
    for command in commands or []:
        for name, pattern in _INTENT_COMMAND:
            if pattern.search(command.strip()):
                return name
    return "system_action" if route == "action" else "other"


# --------------------------------------------------------------------------
# command shapes -- the scriptability signal
# --------------------------------------------------------------------------

# Tools where the first word alone says nothing useful: `nmcli` covers wifi,
# devices and connections, so the subcommand belongs in the shape.
_MULTIPLEXERS = {
    "nmcli", "bluetoothctl", "systemctl", "pactl", "wpctl", "playerctl", "git",
    "apt", "apt-get", "snap", "flatpak", "gsettings", "brightnessctl", "gio",
    "journalctl", "amixer", "xdotool", "wmctrl", "ember-admin",
}


def command_shape(command):
    """A command reduced to the part that stays the same across repeats.

    `gtk-launch brave-browser` and `gtk-launch zen-browser` share the shape
    `gtk-launch <arg>`; if that shape shows up thirty times it is a script
    waiting to be written, whatever the argument was.
    """
    command = (command or "").strip()
    if not command:
        return ""
    # Only the first command in a chain: it is what the turn was really doing.
    head = re.split(r"\s*(?:&&|\|\||;|\|)\s*", command)[0].strip()
    parts = head.split()
    if not parts:
        return ""
    verb = parts[0]
    if verb == "sudo" and len(parts) > 1:
        verb = f"sudo {parts[1]}"
        parts = parts[1:]
    base = os.path.basename(parts[0])
    if base in _MULTIPLEXERS:
        # Step over flags and their values, so `bluetoothctl --timeout 12 scan on`
        # is recognised as the same operation as a bare `bluetoothctl scan`.
        rest = parts[1:]
        while rest and (rest[0].startswith("-") or rest[0].isdigit()):
            rest = rest[1:]
        if rest:
            return f"{verb} {rest[0]}"
    return verb


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def _median(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return 0
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def summarize(records):
    """Everything the report prints, as plain data, so it can also feed a UI."""
    local = [r for r in records if r.get("route") != "model"]
    model = [r for r in records if r.get("route") == "model"]

    intents = {}
    clusters = {}
    shapes = {}
    for record in records:
        intent = record.get("intent", "other")
        bucket = intents.setdefault(intent, {"n": 0, "model": 0, "ok": 0, "ms": [], "cost": 0.0})
        bucket["n"] += 1
        bucket["ok"] += record.get("outcome") == "ok"
        if record.get("route") == "model":
            bucket["model"] += 1
            bucket["ms"].append(record.get("duration_ms"))
            bucket["cost"] += record.get("cost_usd") or 0.0

        key = record.get("norm") or "(blank)"
        cluster = clusters.setdefault(key, {"n": 0, "model": 0, "ms": [], "ok": 0, "cost": 0.0,
                                            "commands": {}, "sigs": {}, "with_commands": 0,
                                            "sample": record.get("prompt", "")})
        cluster["n"] += 1
        cluster["ok"] += record.get("outcome") == "ok"
        if record.get("route") == "model":
            cluster["model"] += 1
            cluster["ms"].append(record.get("duration_ms"))
            cluster["cost"] += record.get("cost_usd") or 0.0

        commands = (record.get("how") or {}).get("commands") or []
        # The signature is the whole sequence of shapes this one turn ran, which
        # is what has to repeat before the turn can be replaced by a script.
        # Comparing individual commands instead would punish any turn that takes
        # four steps, however reliably it takes the same four.
        signature = tuple(s for s in (command_shape(c) for c in commands) if s)
        if signature:
            cluster["with_commands"] += 1
            cluster["sigs"][signature] = cluster["sigs"].get(signature, 0) + 1
        for command, shape in zip(commands, (command_shape(c) for c in commands)):
            if not shape:
                continue
            cluster["commands"][shape] = cluster["commands"].get(shape, 0) + 1
            entry = shapes.setdefault(shape, {"n": 0, "example": command, "intents": {}})
            entry["n"] += 1
            entry["intents"][intent] = entry["intents"].get(intent, 0) + 1

    return {
        "total": len(records),
        "local": len(local),
        "model": len(model),
        "model_ms_median": _median([r.get("duration_ms") for r in model]),
        "model_ttft_median": _median([r.get("ttft_ms") for r in model]),
        "cost": sum(r.get("cost_usd") or 0.0 for r in records),
        "outcomes": _tally(records, "outcome"),
        "routes": _tally(records, "route"),
        "intents": intents,
        "clusters": clusters,
        "shapes": shapes,
        "span_days": _span_days(records),
    }


def _tally(records, field):
    out = {}
    for record in records:
        key = record.get(field) or "unknown"
        out[key] = out.get(key, 0) + 1
    return out


def _span_days(records):
    if len(records) < 2:
        return 0.0
    return (records[-1].get("ts", 0) - records[0].get("ts", 0)) / 86400


def candidates(summary, min_count=3):
    """Model-handled clusters worth turning into local handlers.

    Ranked by time spent, not by count: five wifi checks at nine seconds each
    matter more than twenty instant ones.

    `stability` is the share of the cluster's turns that ran the *same sequence*
    of commands. At 1.0 the model reaches the same answer the same way every
    time, which is a straight port to a local handler however many steps it
    takes; below about 0.6 it is genuinely deciding something per-request and
    should keep the job.
    """
    out = []
    for key, cluster in summary["clusters"].items():
        if cluster["model"] < min_count:
            continue
        median_ms = _median(cluster["ms"])
        top_shape, stability = "", 0.0
        if cluster["sigs"]:
            signature, top_n = max(cluster["sigs"].items(), key=lambda kv: kv[1])
            stability = top_n / cluster["with_commands"]
            top_shape = " -> ".join(signature)
        out.append({
            "query": key,
            "sample": cluster["sample"],
            "count": cluster["n"],
            "model_count": cluster["model"],
            "median_ms": median_ms,
            "success": cluster["ok"] / cluster["n"] if cluster["n"] else 0.0,
            "cost": cluster["cost"],
            "shape": top_shape,
            "stability": stability,
            # Local resolution is ~2ms, so essentially the whole latency is saved.
            "seconds_saved": cluster["model"] * median_ms / 1000.0,
        })
    out.sort(key=lambda c: c["seconds_saved"], reverse=True)
    return out


def _bar(value, total, width=18):
    filled = int(round(width * value / total)) if total else 0
    return "#" * filled + "." * (width - filled)


def report(days=None, min_count=3):
    records = load(days=days)
    if not records:
        print("No interactions recorded yet.")
        print(f"(expected at {HISTORY_PATH})")
        return

    summary = summarize(records)
    window = f"last {days} days" if days else f"all time ({summary['span_days']:.1f} days)"
    print(f"\nEmber usage -- {window}, {summary['total']} interactions\n")

    local_pct = 100 * summary["local"] / summary["total"]
    print(f"  handled locally   {summary['local']:5}  ({local_pct:.0f}%)   ~2ms")
    print(f"  handled by model  {summary['model']:5}  ({100 - local_pct:.0f}%)   "
          f"median {summary['model_ms_median'] / 1000:.1f}s, first token {summary['model_ttft_median'] / 1000:.1f}s")
    if summary["cost"]:
        print(f"  model spend       ${summary['cost']:.2f}")
    outcomes = ", ".join(f"{k} {v}" for k, v in sorted(summary["outcomes"].items(), key=lambda kv: -kv[1]))
    print(f"  outcomes          {outcomes}")

    print("\n  by intent" + " " * 12 + "total   model   median   success")
    ranked = sorted(summary["intents"].items(), key=lambda kv: -kv[1]["n"])
    top = ranked[0][1]["n"] if ranked else 1
    for name, bucket in ranked:
        median_ms = _median(bucket["ms"])
        success = 100 * bucket["ok"] / bucket["n"]
        print(f"  {name:<16} {_bar(bucket['n'], top)} {bucket['n']:5} {bucket['model']:7}"
              f" {median_ms / 1000:7.1f}s {success:8.0f}%")

    print("\n  most repeated requests")
    ranked = sorted(summary["clusters"].items(), key=lambda kv: -kv[1]["n"])[:12]
    for key, cluster in ranked:
        route = "model" if cluster["model"] == cluster["n"] else ("local" if not cluster["model"] else "mixed")
        print(f"  {cluster['n']:4}x  [{route:5}]  {key[:60]}")

    print(f"\n  SCRIPT THESE  (model-handled, seen {min_count}+ times)")
    picks = candidates(summary, min_count=min_count)
    if not picks:
        print(f"  Nothing yet -- no request has gone to the model {min_count}+ times.")
    else:
        print(f"  {'request':<34} {'runs':>5} {'median':>8} {'saved':>8}  what it ran")
        for pick in picks[:12]:
            verdict = "port it" if pick["stability"] >= 0.8 else (
                "mostly stable" if pick["stability"] >= 0.6 else "model decides -- leave it")
            shape = (f"{pick['shape'][:46]} ({pick['stability'] * 100:.0f}% same) -- {verdict}"
                     if pick["shape"] else "no commands recorded")
            print(f"  {pick['query'][:33]:<34} {pick['model_count']:5} {pick['median_ms'] / 1000:7.1f}s"
                  f" {pick['seconds_saved']:7.0f}s  {shape}")

    print("\n  commands the model runs most")
    ranked = sorted(summary["shapes"].items(), key=lambda kv: -kv[1]["n"])[:10]
    for shape, entry in ranked:
        print(f"  {entry['n']:4}x  {shape:<26} e.g. {entry['example'][:44]}")
    print()


def tail(count=20):
    for record in load()[-count:]:
        route = record.get("route", "?")
        outcome = record.get("outcome", "?")
        mark = {"ok": "+", "error": "!", "denied": "x", "cancelled": "-"}.get(outcome, "?")
        ms = record.get("duration_ms") or 0
        print(f"{record.get('at', '')}  {mark} {route:<7} {ms / 1000:5.1f}s  "
              f"{record.get('intent', ''):<12} {(record.get('prompt') or '')[:52]}")
        commands = (record.get("how") or {}).get("commands") or []
        for command in commands[:3]:
            print(f"{'':22}    $ {command[:80]}")


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    command = args[0] if args else "report"
    if command == "tail":
        tail(int(args[1]) if len(args) > 1 else 20)
    elif command == "report":
        days = None
        min_count = 3
        for arg in args[1:]:
            if arg.startswith("--days="):
                days = int(arg.split("=", 1)[1])
            elif arg.startswith("--min="):
                min_count = int(arg.split("=", 1)[1])
        report(days=days, min_count=min_count)
    elif command == "path":
        print(HISTORY_PATH)
    else:
        print(__doc__)
