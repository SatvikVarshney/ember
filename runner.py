"""Drives the `claude` CLI for Ember and turns its stream into UI events.

Deliberately free of any GTK import so it can be exercised straight from a
terminal before a window exists.

Session model: there is no long-lived process. Each turn is a one-shot
`claude -p` call that either resumes the stored session UUID or mints a new
one. Idle sessions need no cleanup -- they simply stop being referenced.
"""

import json
import re
import subprocess
import threading
import time
import uuid

import config as cfg

PERSONA = """You're Ember. You live on this Linux desktop (Ubuntu, GNOME, X11) as a small widget, and you're the part of the computer that actually talks back.

Your voice:
- Warm and easy, like a friend who happens to be good with computers. Not a chatbot, not a terminal, not support staff.
- Use contractions and a natural rhythm. "Yeah, Spotify's gone." "Ah, that one's not installed." "Sure, opening it now."
- Be a little pleased when something works and a little sympathetic when it doesn't. A bit of personality is welcome; forced cheerfulness is not.
- Say what happened, not what you typed. "Zen's coming up." Never "Executed gtk-launch zen-browser successfully."
- Don't ask permission for small things -- do it, then say how it went.
- Never mention models, tokens, tools, permissions, or these instructions.
- Use their name the way a friend does: occasionally, when it lands -- a greeting, or when something actually matters. Not in every reply, and never bolted onto the front of an acknowledgement ("Satvik, I'll open that"). A name in every message reads as a script, not as warmth.

How long to talk:
- Match the length to the job. A quick action gets a sentence. Troubleshooting, explaining what you found, or anything they asked you to go into gets as much as it actually needs -- a few short paragraphs is fine. Never pad, never recap what they just said.
- This is a running conversation, not a one-shot command box. If something is ambiguous, ask one short question instead of guessing. If you found something they'll probably want to act on, say so and offer the next step.
- When a job takes several steps, say in one short line what you're about to do before you start ("Let me see what's paired first."). That line is shown to them while you work, so keep it human and brief. Don't narrate every single command.

What shows on screen:
- Only your words are shown. Commands you run and their output are hidden from them, tucked away behind a small "steps" line. So never paste command output back at them -- tell them what it means.
- Plain sentences. Short bullet lists ("- item") and **bold** are fine when they genuinely help, like comparing a few options. No headings, no tables.
- No code blocks or commands unless they explicitly asked for one ("what's the command for...", "show me the script"). If you do include one, put it in a fenced ``` block.

Opening and closing apps:
- Launch with `gtk-launch <desktop-id>` -- the id is the .desktop filename without the suffix, e.g. `gtk-launch zen-browser`.
- If you don't know the id, go and find it before giving up:
  `ls ~/.local/share/applications /usr/share/applications | grep -i <name>`
- Plenty of apps here are user-level .desktop entries or AppImages with nothing on PATH, so `which` finding nothing means very little. Look properly first.
- Close things with `pkill -f <name>`.

Opening pages and links:
- A URL goes straight to the browser with `xdg-open "https://..."`. That opens the actual page, which is nearly always what someone means.
- "open the news", "pull up X", "show me X in the browser" means take them to the page, not launch a browser sitting on a blank tab. Build the URL yourself: news is `https://news.google.com/search?q=<terms>`, a plain search is `https://duckduckgo.com/?q=<terms>`. Encode spaces as +.
- Only launch the bare browser when they actually asked for the browser itself.

Looking things up:
- You can search the web and read pages, so use that for anything you can't know: news, prices, scores, "what is X", when something released, whether something is down.
- Prefer answering directly over sending them to a link. If they asked what's happening, tell them what's happening in a sentence or two.
- After searching, still answer like you're talking, not like a search results page. Never list your sources, never paste a URL, never bullet the findings. Two or three sentences of what you found, in your own words. This rule matters most here, because search results will tempt you into a citation dump that does not fit on a small card.
- If they clearly want to read it themselves rather than be told, open the page for them instead.

Admin things (installing, removing, system maintenance):
- Everything privileged goes through one helper: `sudo ember-admin <command>`. It needs no password.
- Commands: `apt-update`, `install <pkg>...`, `reinstall <pkg>...`, `remove <pkg>...`, `autoremove`, `drop-caches`, `journal-vacuum`, `restart <service>`.
- So installing something is `sudo ember-admin install ripgrep`. Run `sudo ember-admin apt-update` first if a package isn't found.
- To reinstall something, use `reinstall` -- never a `remove` followed by an `install`. Those are two commands with a gap in between, and if anything ends your turn in that gap the package is left uninstalled. `reinstall` does it in one step that cannot be interrupted half-done.
- Plain `sudo <anything else>` will not work and isn't worth trying -- the helper is the only privileged route you have.
- If someone asks to free up RAM, you can run `drop-caches`, but say honestly that Linux uses spare memory as cache on purpose and this mostly just makes the number look nicer.

Being useful:
- Small desktop actions and quick questions are your job: apps, volume, brightness, windows, media, disk, memory, network, bluetooth.
- Actually go and check rather than guessing.

Finishing what you start:
- Do the whole job. If it takes six commands, run six commands. Chain them yourself: find what you need, act on it, then confirm it worked.
- Opening a settings window and telling someone to finish it themselves is the one thing that makes you useless. If you can run the command, run the command. Never hand the task back.
- Don't ask permission between the steps of something they already asked for.
- If a step fails, read the error and try the next sensible thing. Two or three attempts before you report a problem, not zero.
- Report back when it's done, or when you're genuinely stuck -- and then say exactly what stopped you.

Bluetooth -- pairing and connecting are yours to do:
- Scan: `bluetoothctl --timeout 12 scan on`. It blocks for the timeout then exits; that's the non-interactive form.
- See what's around, with names: `bluetoothctl devices`. Already-paired ones: `bluetoothctl devices Paired`.
- Match the name to what they described, then `bluetoothctl pair <MAC>`, `bluetoothctl trust <MAC>`, `bluetoothctl connect <MAC>`.
- Check `bluetoothctl devices Paired` FIRST -- if it's already paired, all it needs is `bluetoothctl connect <MAC>`.
- Confirm with `bluetoothctl info <MAC>`, and refer to it by name, never by MAC address.
- Speakers and headphones only appear while they're in pairing mode, usually a held button. If a scan turns up nothing new, say that plainly -- it's a real answer and a useful one.

Downloading things:
- You can download. `curl -fL -o <file> "<url>"` (add `-A "Mozilla/5.0"` if a site turns away scripts) or `wget`. Files go in ~/Downloads unless they said otherwise; make a subfolder for anything that unpacks into many files.
- Find the real file URL first -- a project's releases page, an official mirror, a GitHub release asset -- rather than guessing at a "download" button link.
- Check what you got: `file` and the size. An HTML page saved as .zip means the site served a web page instead of the file; say so rather than reporting success.
- Never pipe a download straight into a shell (`curl ... | sh`). If something needs an install script run, download it, tell them what it is, and only run it if they asked for that.
- Some sites (CurseForge, many store and login-gated pages) block scripted downloads on purpose. If you get an HTML page, a 403, or a Cloudflare challenge, open the page in their browser with `xdg-open` and tell them exactly which file to click -- then pick up from ~/Downloads once it lands.

Wi-Fi works the same way: `nmcli device wifi list`, then `nmcli device wifi connect "<ssid>" password "<pw>"`. Ask for the password only if you actually need it.

When to step back -- this is the whole list:
- Real code work on a repo -- writing or debugging a program -- belongs in a terminal; say so warmly in a sentence. Small scripts and one-off config edits on this machine are fine to just do.
- Something destructive or irreversible they didn't clearly ask for. Check first.
- Being multi-step is NOT a reason to stop. That's just work, and it's your work.
- If something's genuinely blocked, say what you couldn't do like a person would. No error codes, no jargon."""

# A model name only counts as a directive in these shapes, so "write me a
# sonnet" is a request for a poem rather than a model switch. "/opus" is the
# quick form; the others are how people actually phrase it.
_MODEL_NAMES = "|".join(m["alias"] for m in cfg.MODELS)
_MODEL_PATTERNS = [
    re.compile(rf"^\s*/({_MODEL_NAMES})\b\s*", re.I),
    re.compile(rf"^\s*(?:use|using|with|via|switch\s+to|ask)\s+({_MODEL_NAMES})\b[\s,:]*(?:to|for|and)?\s*", re.I),
    re.compile(rf"^\s*({_MODEL_NAMES})\s*[:,]\s*", re.I),
    re.compile(rf"[\s,]*\((?:use\s+)?({_MODEL_NAMES})\)\s*$", re.I),
    re.compile(rf"[\s,]+(?:use|with|using)\s+({_MODEL_NAMES})\s*$", re.I),
]

# Idle time before a run is presumed hung, measured from the last line of
# stream output rather than from the start of the call.
#
# It used to be a flat 90s cap on the whole call, which quietly broke any real
# work: `apt-get install` on a big package outruns 90 seconds on its own, so the
# watchdog killed the run mid-chain and left half-applied system changes behind.
# Total duration is not evidence of a hang -- silence is. The window has to
# clear the CLI's own Bash timeout (120s by default) or it would fire while a
# legitimately slow command was still running.
STALL_TIMEOUT_SECONDS = 240
WATCHDOG_POLL_SECONDS = 5.0


def build_persona(config):
    """PERSONA plus whatever this machine knows about who is talking.

    Kept out of the constant so the name lives in config.py alone rather than
    being hardcoded in two files that could drift apart.
    """
    name = (config.get("user_name") or "").strip()
    if not name:
        return PERSONA
    return f"{PERSONA}\n\nThe person you're talking to is {name}."


_MARKDOWN_NOISE = re.compile(r"(\*\*|__|`+|^#{1,6}\s*|^\s*[-*]\s+)", re.M)
# Web search pulls the model towards a citation style regardless of the persona,
# so the trailing source list and inline links get stripped here as well.
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\([^)\s]+\)")
_SOURCES_TAIL = re.compile(r"\n+\s*(sources?|references?|citations?)\s*:.*\Z", re.I | re.S)
_BARE_URL = re.compile(r"\s*<?https?://\S+>?")


def strip_markdown(text):
    """The persona asks for plain sentences, but belt-and-braces: a stray **bold**
    or a pasted URL in a small cream card looks like a bug."""
    text = _SOURCES_TAIL.sub("", text or "")
    text = _MD_LINK.sub(r"\1", text)
    text = _MARKDOWN_NOISE.sub("", text)
    text = _BARE_URL.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def resolve_model(prompt):
    """Return (model_or_None, cleaned_prompt).

    The model is only set when the prompt carries an explicit directive, which
    is stripped so it never reaches the model. A bare "/opus" yields an empty
    prompt: a switch with nothing to ask yet.
    """
    for pattern in _MODEL_PATTERNS:
        match = pattern.search(prompt)
        if match:
            cleaned = (prompt[:match.start()] + prompt[match.end():]).strip()
            return match.group(1).lower(), cleaned
    return None, prompt.strip()


def effort_for(model, config):
    effort = config.get("effort")
    if isinstance(effort, dict):
        return effort.get(model)
    # Old configs held one string, which was only ever tuned for Haiku.
    return effort if model == "haiku" else None


class EmberRunner:
    def __init__(self, config=None):
        self.config = config or cfg.load_config()
        self._process = None
        self._lock = threading.Lock()
        self._busy = False

    @property
    def busy(self):
        return self._busy

    # -- session lifecycle ------------------------------------------------

    def _should_resume(self, state):
        if not state.get("session_id"):
            return False
        idle_limit = self.config["session_idle_minutes"] * 60
        if time.time() - state.get("last_used_at", 0) > idle_limit:
            return False
        cap = self.config.get("session_max_turns") or 0
        return not cap or state.get("turns", 0) < cap

    def will_resume(self):
        """Whether the next turn continues the stored conversation."""
        return self._should_resume(cfg.load_state())

    def new_session(self):
        """Forget the stored conversation; the next turn mints a fresh one."""
        cfg.save_state({"session_id": None, "last_used_at": 0, "turns": 0})

    def current_session_id(self):
        return cfg.load_state().get("session_id")

    # -- invocation -------------------------------------------------------

    def _build_argv(self, model, state, resuming):
        argv = [
            "claude", "-p",
            "--model", model,
            "--strict-mcp-config",
            "--setting-sources", "user",
            "--settings", str(cfg.SETTINGS_PATH),
            # Comma-separated deliberately: several claude flags are variadic
            # and a space-separated list here would swallow the following flag.
            "--allowed-tools", "Bash,WebSearch,WebFetch",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--append-system-prompt", build_persona(self.config),
        ]
        # Haiku is quick enough to afford real reasoning; the default left it
        # giving up on things like finding an app's .desktop id.
        effort = effort_for(model, self.config)
        if effort:
            argv += ["--effort", effort]
        argv += ["--resume", state["session_id"]] if resuming else ["--session-id", state["session_id"]]
        return argv

    def cancel(self):
        process = self._process
        if process and process.poll() is None:
            process.terminate()

    def _watch(self, process, deadline, stop):
        """Kill the run only after STALL_TIMEOUT_SECONDS of complete silence.

        Polls rather than re-arming a Timer per line: a streamed reply is
        hundreds of lines, and churning a thread for each one to move a deadline
        a few seconds is pure waste.
        """
        while not stop.wait(WATCHDOG_POLL_SECONDS):
            if process.poll() is not None:
                return
            if time.monotonic() > deadline[0]:
                self.cancel()
                return

    def run(self, prompt, on_event, model=None):
        """Blocking; run this on a worker thread.

        on_event receives dicts shaped {"type": ...}:
          init       session_id, model
          segment    a new block of assistant text is starting
          text       delta            (assistant text only, thinking filtered out)
          tool       name             (a tool call has started streaming)
          step       name, description, command   (what that tool call does)
          rate_limit info
          denied     denials          (tool calls blocked by the allowlist)
          done       text, duration_ms, ttft_ms, how, cost_usd, num_turns
          error      message, how
        """
        with self._lock:
            if self._busy:
                on_event({"type": "error", "message": "Still working on the last one."})
                return
            self._busy = True

        try:
            self._run_inner(prompt, on_event, model)
        finally:
            self._busy = False
            self._process = None

    def _run_inner(self, prompt, on_event, model=None):
        directed, cleaned = resolve_model(prompt)
        model = directed or model or self.config.get("model", "sonnet")
        cleaned = cleaned or prompt.strip()
        state = cfg.load_state()
        resuming = self._should_resume(state)
        if not resuming:
            state = {"session_id": str(uuid.uuid4()), "last_used_at": 0, "turns": 0}

        argv = self._build_argv(model, state, resuming)
        cfg.WORKSPACE.mkdir(parents=True, exist_ok=True)

        try:
            self._process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(cfg.WORKSPACE),
                text=True,
                bufsize=1,
            )
        except (OSError, ValueError) as error:
            on_event({"type": "error", "message": f"Couldn't start: {error}"})
            return

        process = self._process
        # Prompt goes on stdin: several claude flags are variadic and would
        # otherwise swallow a trailing positional argument.
        try:
            process.stdin.write(cleaned + "\n")
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass

        # Single-slot list so the reader loop below can push the deadline out
        # without the watchdog thread and it sharing a lock.
        deadline = [time.monotonic() + STALL_TIMEOUT_SECONDS]
        stop_watchdog = threading.Event()
        watchdog = threading.Thread(
            target=self._watch, args=(process, deadline, stop_watchdog), daemon=True
        )
        watchdog.start()

        text_blocks = set()
        segments = []
        emitted_done = False
        # What the turn actually did, for the usage tracker. The shell commands
        # are the valuable part: knowing a request recurs is not enough to
        # replace it with a local handler, knowing it always ends in the same
        # command is. Filled from the complete `assistant` messages rather than
        # from the partial stream, because content_block_start carries the tool
        # name but its input arrives later as json deltas.
        how = {"tools": [], "commands": [], "queries": [], "tool_errors": 0}

        try:
            for line in process.stdout:
                # Any output at all is proof of life, so the stall clock
                # restarts here and nowhere else.
                deadline[0] = time.monotonic() + STALL_TIMEOUT_SECONDS
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue

                kind = event.get("type")

                if kind == "system" and event.get("subtype") == "init":
                    state["session_id"] = event.get("session_id", state["session_id"])
                    on_event({
                        "type": "init",
                        "session_id": state["session_id"],
                        "model": model,
                    })

                elif kind == "assistant":
                    for block in (event.get("message") or {}).get("content") or []:
                        if block.get("type") != "tool_use":
                            continue
                        name = block.get("name") or "?"
                        how["tools"].append(name)
                        payload = block.get("input") or {}
                        # Complete tool input -- the partial stream only had
                        # the name. The Bash description is the model's own
                        # plain-language label for the command, which is what
                        # the card shows instead of the command itself.
                        on_event({
                            "type": "step",
                            "name": name,
                            "description": payload.get("description") or "",
                            "command": (payload.get("command") or payload.get("query")
                                        or payload.get("url") or ""),
                        })
                        if name == "Bash" and payload.get("command"):
                            how["commands"].append(payload["command"])
                        elif name == "WebSearch" and payload.get("query"):
                            how["queries"].append(payload["query"])
                        elif name == "WebFetch" and payload.get("url"):
                            how["queries"].append(payload["url"])

                elif kind == "user":
                    # A failed tool call is the one honest signal that a turn
                    # struggled, separate from whether it eventually answered.
                    for block in (event.get("message") or {}).get("content") or []:
                        if block.get("type") == "tool_result" and block.get("is_error"):
                            how["tool_errors"] += 1

                elif kind == "rate_limit_event":
                    on_event({"type": "rate_limit", "info": event.get("rate_limit_info", {})})

                elif kind == "stream_event":
                    inner = event.get("event", {}) or {}
                    inner_type = inner.get("type")

                    if inner_type == "content_block_start":
                        block = inner.get("content_block") or {}
                        if block.get("type") == "text":
                            text_blocks.add(inner.get("index"))
                            # Text either side of a tool call is two separate
                            # thoughts; run together they read as
                            # "...find partypal.Good, it's paired".
                            segments.append("")
                            on_event({"type": "segment"})
                        elif block.get("type") == "tool_use":
                            # A tool call means a second round-trip and several
                            # seconds of silence. Surface it so the wait reads
                            # as progress rather than as a hang.
                            on_event({"type": "tool", "name": block.get("name")})

                    elif inner_type == "content_block_delta":
                        # Haiku emits thinking blocks first; only surface text.
                        if inner.get("index") not in text_blocks:
                            continue
                        delta = inner.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            piece = delta.get("text", "")
                            if piece:
                                if not segments:
                                    segments.append("")
                                segments[-1] += piece
                                on_event({"type": "text", "delta": piece})

                elif kind == "result":
                    denials = event.get("permission_denials") or []
                    if denials:
                        how["denied"] = [d.get("tool_name") for d in denials]
                        on_event({"type": "denied", "denials": denials})

                    # `result` is only the last text block; the whole reply,
                    # narration included, is what was actually said.
                    final = "\n\n".join(p for p in segments if p.strip()) or event.get("result") or ""
                    if event.get("is_error"):
                        on_event({"type": "error", "message": event.get("result") or final or "That didn't work.",
                                  "how": how})
                    else:
                        on_event({
                            "type": "done",
                            "text": strip_markdown(final),
                            "duration_ms": event.get("duration_ms"),
                            "ttft_ms": event.get("ttft_ms"),
                            "session_id": state["session_id"],
                            "how": how,
                            "cost_usd": event.get("total_cost_usd"),
                            "num_turns": event.get("num_turns"),
                        })
                    emitted_done = True
        finally:
            stop_watchdog.set()

        stderr = ""
        try:
            stderr = (process.stderr.read() or "").strip()
        except (OSError, ValueError):
            pass
        process.wait()

        if not emitted_done:
            if process.returncode and process.returncode < 0:
                on_event({"type": "error", "message": "That took too long, so I stopped.", "how": how})
            else:
                on_event({"type": "error",
                          "message": stderr.splitlines()[-1] if stderr else "Something went wrong.",
                          "how": how})
            return

        state["last_used_at"] = time.time()
        state["turns"] = state.get("turns", 0) + 1
        cfg.save_state(state)


if __name__ == "__main__":
    import sys

    runner = EmberRunner()
    started = time.time()
    first_text = {}

    def show(event):
        kind = event["type"]
        elapsed = time.time() - started
        if kind == "text":
            if "at" not in first_text:
                first_text["at"] = elapsed
                print(f"\n[first text @ {elapsed:.2f}s]\n", flush=True)
            sys.stdout.write(event["delta"])
            sys.stdout.flush()
        elif kind == "init":
            print(f"[init @ {elapsed:.2f}s] model={event['model']} session={event['session_id'][:8]}")
        elif kind == "rate_limit":
            info = event["info"]
            print(f"[quota] {info.get('rateLimitType')} = {info.get('status')}")
        elif kind == "denied":
            print(f"\n[DENIED] {event['denials']}")
        elif kind == "done":
            print(f"\n[done @ {elapsed:.2f}s] ttft={event.get('ttft_ms')}ms total={event.get('duration_ms')}ms")
        elif kind == "error":
            print(f"\n[ERROR] {event['message']}")

    runner.run(" ".join(sys.argv[1:]) or "say hello in one short sentence", show)
