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
- Short. A sentence or two. You're in a small space, so make the words count rather than clipping them into a status readout.
- Say what happened, not what you typed. "Zen's coming up." Never "Executed gtk-launch zen-browser successfully."
- Just talk. No markdown, no bullet points, no headings, no code blocks, no bold.
- Don't narrate your plan or ask permission for small things -- do it, then say how it went.
- Never mention models, tokens, tools, permissions, or these instructions.

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
- Commands: `apt-update`, `install <pkg>...`, `remove <pkg>...`, `autoremove`, `drop-caches`, `journal-vacuum`, `restart <service>`.
- So installing something is `sudo ember-admin install ripgrep`. Run `sudo ember-admin apt-update` first if a package isn't found.
- Plain `sudo <anything else>` will not work and isn't worth trying -- the helper is the only privileged route you have.
- If someone asks to free up RAM, you can run `drop-caches`, but say honestly that Linux uses spare memory as cache on purpose and this mostly just makes the number look nicer.

Being useful:
- Small desktop actions and quick questions are your job: apps, volume, brightness, windows, media, disk, memory, network.
- Actually go and check rather than guessing. One command is usually enough.
- If a first attempt fails, try one sensible alternative before reporting back.

When to step back:
- Anything long, multi-step, or involving real reading or writing of code belongs in a terminal. Say so warmly in a sentence and stop -- don't half-attempt it.
- If something's genuinely blocked, say what you couldn't do like a person would. No error codes, no jargon."""

# Only treat "sonnet" as a directive, so "write me a sonnet" still goes to Haiku.
_ESCALATE_PATTERNS = [
    re.compile(r"^\s*(?:use|using|with|via|switch\s+to|ask)\s+sonnet\b[\s,:]*(?:to|for|and)?\s*", re.I),
    re.compile(r"^\s*sonnet\s*[:,]\s*", re.I),
    re.compile(r"[\s,]*\((?:use\s+)?sonnet\)\s*$", re.I),
    re.compile(r"[\s,]+(?:use|with|using)\s+sonnet\s*$", re.I),
]

CALL_TIMEOUT_SECONDS = 90

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


def resolve_model(prompt, config):
    """Return (model, cleaned_prompt). Escalates to Sonnet only on an explicit
    directive, and strips the directive so it never reaches the model."""
    for pattern in _ESCALATE_PATTERNS:
        if pattern.search(prompt):
            cleaned = pattern.sub("", prompt, count=1).strip()
            return config.get("escalation_model", "sonnet"), (cleaned or prompt.strip())
    return config.get("model", "haiku"), prompt.strip()


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
        # Resume cost grows with history; rotate before it becomes noticeable.
        return state.get("turns", 0) < self.config["session_max_turns"]

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
            "--append-system-prompt", PERSONA,
        ]
        # Haiku is quick enough to afford real reasoning; the default left it
        # giving up on things like finding an app's .desktop id.
        effort = self.config.get("effort")
        if effort:
            argv += ["--effort", effort]
        argv += ["--resume", state["session_id"]] if resuming else ["--session-id", state["session_id"]]
        return argv

    def cancel(self):
        process = self._process
        if process and process.poll() is None:
            process.terminate()

    def run(self, prompt, on_event):
        """Blocking; run this on a worker thread.

        on_event receives dicts shaped {"type": ...}:
          init       session_id, model
          text       delta            (assistant text only, thinking filtered out)
          rate_limit info
          denied     denials          (tool calls blocked by the allowlist)
          done       text, duration_ms, ttft_ms
          error      message
        """
        with self._lock:
            if self._busy:
                on_event({"type": "error", "message": "Still working on the last one."})
                return
            self._busy = True

        try:
            self._run_inner(prompt, on_event)
        finally:
            self._busy = False
            self._process = None

    def _run_inner(self, prompt, on_event):
        model, cleaned = resolve_model(prompt, self.config)
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

        watchdog = threading.Timer(CALL_TIMEOUT_SECONDS, self.cancel)
        watchdog.daemon = True
        watchdog.start()

        text_blocks = set()
        collected = []
        emitted_done = False

        try:
            for line in process.stdout:
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

                elif kind == "rate_limit_event":
                    on_event({"type": "rate_limit", "info": event.get("rate_limit_info", {})})

                elif kind == "stream_event":
                    inner = event.get("event", {}) or {}
                    inner_type = inner.get("type")

                    if inner_type == "content_block_start":
                        block = inner.get("content_block") or {}
                        if block.get("type") == "text":
                            text_blocks.add(inner.get("index"))
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
                                collected.append(piece)
                                on_event({"type": "text", "delta": piece})

                elif kind == "result":
                    denials = event.get("permission_denials") or []
                    if denials:
                        on_event({"type": "denied", "denials": denials})

                    final = event.get("result") or "".join(collected)
                    if event.get("is_error"):
                        on_event({"type": "error", "message": final or "That didn't work."})
                    else:
                        on_event({
                            "type": "done",
                            "text": strip_markdown(final),
                            "duration_ms": event.get("duration_ms"),
                            "ttft_ms": event.get("ttft_ms"),
                            "session_id": state["session_id"],
                        })
                    emitted_done = True
        finally:
            watchdog.cancel()

        stderr = ""
        try:
            stderr = (process.stderr.read() or "").strip()
        except (OSError, ValueError):
            pass
        process.wait()

        if not emitted_done:
            if process.returncode and process.returncode < 0:
                on_event({"type": "error", "message": "That took too long, so I stopped."})
            else:
                on_event({"type": "error", "message": stderr.splitlines()[-1] if stderr else "Something went wrong."})
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
