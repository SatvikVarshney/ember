"""TinyStories, run locally, for the little story lines Ember shows while it works.

The model is TinyStories-Instruct-33M: a four-layer GPT-Neo that only knows how
to write children's stories, and is small enough to do it on the CPU in a blink.
It runs here in plain numpy -- no torch, nothing to install -- in a separate
worker process, so the 300MB of weights never sit inside the widget and a slow
generation can never stall the card. The worker exits once it has been idle a
while and is started again on the next request.

Free of any GTK import, like runner.py, so it can be tried from a terminal:

    python3 storyteller.py "List paired Bluetooth devices"
"""

import json
import os
import pathlib
import re
import struct
import subprocess
import sys
import threading
from functools import lru_cache

import numpy as np

MODEL_DIR = pathlib.Path.home() / ".cache" / "ember" / "tinystories"
# The worker gives its memory back after this long without a request.
IDLE_EXIT_S = 300

LAYERS, HEADS, WIDTH, EOS = 4, 16, 768, 50256


# -- tokenizer: GPT-2 byte-level BPE -------------------------------------------

def _byte_alphabet():
    """GPT-2's reversible map from raw bytes to printable characters."""
    keep = list(range(ord("!"), ord("~") + 1)) + list(range(0xA1, 0xAD)) + list(range(0xAE, 0x100))
    chars = keep[:]
    extra = 0
    for b in range(256):
        if b not in keep:
            keep.append(b)
            chars.append(256 + extra)
            extra += 1
    return dict(zip(keep, map(chr, chars)))


_SPLIT = re.compile(r"""'s|'t|'re|'ve|'m|'ll|'d| ?[^\W\d_]+| ?\d+| ?[^\s\w]+|\s+(?!\S)|\s+""")


class Tokenizer:
    def __init__(self, folder):
        self.encoder = json.loads((folder / "vocab.json").read_text())
        self.decoder = {v: k for k, v in self.encoder.items()}
        merges = (folder / "merges.txt").read_text().split("\n")[1:]
        self.ranks = {tuple(m.split()): i for i, m in enumerate(merges) if m}
        self.to_char = _byte_alphabet()
        self.to_byte = {c: b for b, c in self.to_char.items()}
        self._bpe = lru_cache(maxsize=4096)(self._bpe_uncached)

    def _bpe_uncached(self, word):
        parts = list(word)
        while len(parts) > 1:
            rank, i = min((self.ranks.get(p, 1 << 30), i) for i, p in enumerate(zip(parts, parts[1:])))
            if rank == 1 << 30:
                break
            parts[i:i + 2] = [parts[i] + parts[i + 1]]
        return tuple(parts)

    def encode(self, text):
        ids = []
        for piece in _SPLIT.findall(text):
            word = "".join(self.to_char[b] for b in piece.encode("utf-8"))
            ids.extend(self.encoder[p] for p in self._bpe(word))
        return ids

    def decode(self, ids):
        raw = bytes(self.to_byte[c] for i in ids for c in self.decoder[i])
        return raw.decode("utf-8", errors="replace")


# -- model: GPT-Neo forward pass with a KV cache ------------------------------

def _load_safetensors(path):
    with open(path, "rb") as f:
        size = struct.unpack("<Q", f.read(8))[0]
        header = json.loads(f.read(size))
        blob = f.read()
    weights = {}
    for name, info in header.items():
        if name == "__metadata__" or info["dtype"] != "F32":
            continue
        start, end = info["data_offsets"]
        weights[name] = np.frombuffer(blob, np.float32, (end - start) // 4, start).reshape(info["shape"])
    return weights


def _layer_norm(x, w, b):
    mean = x.mean(-1, keepdims=True)
    var = ((x - mean) ** 2).mean(-1, keepdims=True)
    return (x - mean) / np.sqrt(var + 1e-5) * w + b


def _gelu(x):
    return 0.5 * x * (1 + np.tanh(0.7978845608 * (x + 0.044715 * x ** 3)))


class Model:
    def __init__(self, folder):
        w = _load_safetensors(folder / "model.safetensors")
        self.wte = w["transformer.wte.weight"]
        self.wpe = w["transformer.wpe.weight"]
        self.lnf = (w["transformer.ln_f.weight"], w["transformer.ln_f.bias"])
        self.layers = []
        for i in range(LAYERS):
            p = f"transformer.h.{i}."
            a = p + "attn.attention."
            self.layers.append({
                "ln1": (w[p + "ln_1.weight"], w[p + "ln_1.bias"]),
                "ln2": (w[p + "ln_2.weight"], w[p + "ln_2.bias"]),
                # q, k and v in one matmul; torch stores Linear weights as [out, in].
                "qkv": np.concatenate([w[a + "q_proj.weight"], w[a + "k_proj.weight"], w[a + "v_proj.weight"]]).T.copy(),
                "out": (w[a + "out_proj.weight"].T.copy(), w[a + "out_proj.bias"]),
                "fc": (w[p + "mlp.c_fc.weight"].T.copy(), w[p + "mlp.c_fc.bias"]),
                "proj": (w[p + "mlp.c_proj.weight"].T.copy(), w[p + "mlp.c_proj.bias"]),
            })
        self.head = self.wte.T.copy()  # tied to the input embedding

    def forward(self, ids, cache):
        """Logits for the last of `ids`, extending `cache` (one (k, v) per layer).

        Prompts stay well under the 256-token window, so GPT-Neo's alternating
        local layers see exactly what the global ones do and need no mask of
        their own. GPT-Neo also famously skips the 1/sqrt(d) attention scale.
        """
        past = cache[0][0].shape[1] if cache else 0
        n = len(ids)
        x = self.wte[ids] + self.wpe[past:past + n]
        head_dim = WIDTH // HEADS
        causal = np.triu(np.full((n, past + n), -1e9, np.float32), past + 1)
        for i, layer in enumerate(self.layers):
            h = _layer_norm(x, *layer["ln1"])
            q, k, v = np.split(h @ layer["qkv"], 3, axis=-1)
            q, k, v = (t.reshape(n, HEADS, head_dim).transpose(1, 0, 2) for t in (q, k, v))
            if len(cache) > i:
                k = np.concatenate([cache[i][0], k], axis=1)
                v = np.concatenate([cache[i][1], v], axis=1)
                cache[i] = (k, v)
            else:
                cache.append((k, v))
            scores = q @ k.transpose(0, 2, 1) + causal
            scores = np.exp(scores - scores.max(-1, keepdims=True))
            scores /= scores.sum(-1, keepdims=True)
            attended = (scores @ v).transpose(1, 0, 2).reshape(n, WIDTH)
            x = x + attended @ layer["out"][0] + layer["out"][1]
            h = _layer_norm(x, *layer["ln2"])
            x = x + _gelu(h @ layer["fc"][0] + layer["fc"][1]) @ layer["proj"][0] + layer["proj"][1]
        return _layer_norm(x[-1], *self.lnf) @ self.head


class Teller:
    def __init__(self, folder=MODEL_DIR):
        self.tokenizer = Tokenizer(folder)
        self.model = Model(folder)
        self.rng = np.random.default_rng()

    def generate(self, prompt, max_tokens=40, temperature=0.7, top_k=40):
        """Sample until the first sentence ends."""
        cache = []
        logits = self.model.forward(self.tokenizer.encode(prompt), cache)
        out = []
        for _ in range(max_tokens):
            logits = logits / temperature
            top = np.argpartition(logits, -top_k)[-top_k:]
            p = np.exp(logits[top] - logits[top].max())
            token = int(self.rng.choice(top, p=p / p.sum()))
            if token == EOS:
                break
            out.append(token)
            text = self.tokenizer.decode(out)
            if "\n" in text or re.search(r"[.!?][\"']?\s*$", text):
                break
            logits = self.model.forward([token], cache)
        return self.tokenizer.decode(out)

    def line_for(self, task, attempts=4):
        """One story sentence about what Ember is doing right now.

        The Instruct model was trained on "Summary: ... Words: ... Story: ..."
        records, so the task goes in as the summary and its key words, and the
        story opens on Ember setting out to do it. The sentence the model adds
        next is the line. It is tiny and it rambles, so a few tries are allowed
        to find one that is short and doesn't trip over its own name.
        """
        task = task.strip().rstrip(".…")
        task = task[0].lower() + task[1:]
        prompt = (
            f"Summary: Ember the little spark wants to {task}.\n"
            f"Words: {_key_words(task)}\n"
            f"Story: Ember the little spark wanted to {task}. Ember"
        )
        line = ""
        for _ in range(attempts):
            line = _tidy("Ember" + self.generate(prompt, max_tokens=28, temperature=0.6))
            if len(line.split()) >= 4 and len(line) <= 80 and line.count("Ember") == 1:
                return line
        return line if len(line) <= 80 else ""


_STOP = set("a an the of to for in on and or with from by my your their is are this that it all any some".split())


def _key_words(task):
    """Up to three content words from the task, minus its leading verb."""
    words = re.findall(r"[a-zA-Z']+", task.lower())[1:]
    return ", ".join([w for w in words if w not in _STOP][:3])


def _tidy(text):
    text = " ".join(text.split())
    # A sentence cut off mid-way reads worse than none; end it on its last word.
    if not re.search(r"[.!?][\"']?$", text):
        text = text.rstrip(",;:- ") + "…"
    # The sentence often stops at the "?" inside someone's speech.
    if text.count("\u201c") > text.count("\u201d"):
        text += "\u201d"
    elif text.count('"') % 2:
        text += '"'
    return text


def available():
    return all((MODEL_DIR / f).exists() for f in ("model.safetensors", "vocab.json", "merges.txt"))


# -- worker process -------------------------------------------------------------

def _serve():
    """Read one JSON request per line, answer one per line. Exits when idle."""
    teller = Teller()
    timer = None

    def arm():
        nonlocal timer
        if timer:
            timer.cancel()
        timer = threading.Timer(IDLE_EXIT_S, lambda: os._exit(0))
        timer.daemon = True
        timer.start()

    arm()
    for raw in sys.stdin:
        arm()
        try:
            request = json.loads(raw)
            reply = {"id": request.get("id"), "line": teller.line_for(request["task"])}
        except Exception as exc:  # a bad request costs one line, never the worker
            reply = {"id": None, "error": str(exc)}
        print(json.dumps(reply), flush=True)


class Storyteller:
    """The widget's handle on the worker. `tell(task, callback)` calls back,
    from a background thread, with a story line -- or not at all if the model
    isn't there or something went wrong. Only the newest request matters: a
    line about a step that has already finished is just noise."""

    def __init__(self):
        self._lock = threading.Lock()
        self._process = None
        self._latest = 0
        self._callbacks = {}

    def tell(self, task, callback):
        if not task or not available():
            return
        with self._lock:
            self._latest += 1
            self._callbacks = {self._latest: callback}
            try:
                process = self._ensure_process()
                process.stdin.write(json.dumps({"id": self._latest, "task": task}) + "\n")
                process.stdin.flush()
            except (OSError, ValueError):
                self._process = None

    def cancel(self):
        with self._lock:
            self._callbacks = {}

    def _ensure_process(self):
        if self._process and self._process.poll() is None:
            return self._process
        self._process = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--serve"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        threading.Thread(target=self._read, args=(self._process,), daemon=True).start()
        return self._process

    def _read(self, process):
        for raw in process.stdout:
            try:
                reply = json.loads(raw)
            except ValueError:
                continue
            with self._lock:
                callback = self._callbacks.pop(reply.get("id"), None)
            if callback and reply.get("line"):
                callback(reply["line"])

    def stop(self):
        with self._lock:
            if self._process and self._process.poll() is None:
                self._process.terminate()
            self._process = None


if __name__ == "__main__":
    if sys.argv[1:] == ["--serve"]:
        _serve()
    else:
        teller = Teller()
        for task in sys.argv[1:] or ["List paired Bluetooth devices"]:
            for _ in range(3):
                print(f"{task}  ->  {teller.line_for(task)}")
