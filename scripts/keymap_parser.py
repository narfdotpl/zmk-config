#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# ///
"""Parse a ZMK keymap into per-key tap / hold / tap+mod behaviours.

Supports what this keymap uses: kp, mt, lt, mo, sk, trans, none, hold-tap,
mod-morph and macro behaviours, plus the `MAKE_*` helper macros.

Usage: scripts/keymap_parser.py [layer ...]   (default: default_layer alpha_layer)

Disclosure: Throughout renderer and parser, I used probabilistic automation in the form of Anthropic Claude Sonnet 5.5 used via the Pi harness. I reviewed, edited, and approved all changes.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

KEYMAP = Path(__file__).resolve().parent.parent / "config" / "corne.keymap"

# Manual overrides for generated labels (tap, hold and tap+mod). Keys are matched
# case-insensitively against the parser's label, e.g. "ALT+X" or "SELFDOT".
LABEL_OVERRIDES = {
    "ALT+X": "Ź",
    "SELFDOT": "self.",
    "THISDOT": "this.",
}

# --- Key labels ---------------------------------------------------------------

KEY_NAMES = {
    "SPACE": "SPC", "BACKSPACE": "BS", "BSPC": "BS", "DELETE": "DEL", "DEL": "DEL",
    "ESC": "ESC", "ESCAPE": "ESC", "TAB": "TAB", "ENTER": "ENTER", "RET": "ENTER",
    "RETURN": "ENTER", "CAPSLOCK": "CAPS", "K_MUTE": "MUTE",
    "UP": "↑", "DOWN": "↓", "LEFT": "←", "RIGHT": "→",
    "LSHIFT": "SHIFT", "RSHIFT": "SHIFT", "LCMD": "CMD", "RCMD": "CMD",
    "LGUI": "CMD", "RGUI": "CMD", "LALT": "ALT", "RALT": "ALT",
    "LCTRL": "CTRL", "RCTRL": "CTRL",
    "DOT": ".", "COMMA": ",", "QUESTION": "?", "EXCLAMATION": "!", "COLON": ":",
    "SEMICOLON": ";", "SINGLE_QUOTE": "'", "SQT": "'", "DOUBLE_QUOTES": '"',
    "GRAVE": "`", "TILDE": "~", "LBRC": "{", "RBRC": "}", "LPAR": "(", "RPAR": ")",
    "LBKT": "[", "RBKT": "]", "LT": "<", "GT": ">", "SLASH": "/", "BACKSLASH": "\\",
    "MINUS": "-", "EQUAL": "=", "PLUS": "+", "ASTERISK": "*", "UNDER": "_",
    "AT": "@", "HASH": "#", "PERCENT": "%", "AMPS": "&", "PIPE": "|", "CARET": "^",
    "DOLLAR": "$",
}  # fmt: skip

MOD_WRAPPERS = {
    "LS": "SHIFT", "RS": "SHIFT", "LG": "CMD", "RG": "CMD",
    "LA": "ALT", "RA": "ALT", "LC": "CTRL", "RC": "CTRL", "HY": "HYPER",
}  # fmt: skip

MOD_FLAGS = {"SFT": "SHIFT", "CTL": "CTRL", "ALT": "ALT", "GUI": "CMD"}

# Number of parameters of ZMK built-in behaviours.
BUILTIN_CELLS = {"kp": 1, "mt": 2, "lt": 2, "mo": 1, "sk": 1, "trans": 0, "none": 0}

# --- Preprocessing --------------------------------------------------------------


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def split_args(text: str, start: int) -> tuple[list[str], int]:
    """Split macro call arguments; `start` is just after the opening paren."""
    depth, args, cur = 1, [], []
    for i in range(start, len(text)):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                args.append("".join(cur).strip())
                return args, i + 1
        if c == "," and depth == 1:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
    raise ValueError("unbalanced parentheses in macro call")


def substitute(body: str, mapping: dict[str, str]) -> str:
    def repl(m: re.Match) -> str:
        if m.group(1) is not None:  # #param -> "value"
            return f'"{mapping[m.group(1)]}"' if m.group(1) in mapping else m.group(0)
        return mapping.get(m.group(2), m.group(0))

    out = []
    # Don't touch string literals (e.g. "zmk,behavior-hold-tap").
    for part in re.split(r'("[^"]*")', body):
        out.append(part if part.startswith('"') else re.sub(r"#(\w+)|\b(\w+)\b", repl, part))
    return "".join(out)


def preprocess(text: str) -> tuple[str, dict[str, str]]:
    """Returns (text with MAKE_* macros expanded, object-like defines)."""
    text = strip_comments(text).replace("\\\n", " ")
    defines: dict[str, str] = {}
    funcs: dict[str, tuple[list[str], str]] = {}
    lines = []
    for line in text.split("\n"):
        if not line.lstrip().startswith("#"):
            lines.append(line)
            continue
        m = re.match(r"\s*#define\s+(\w+)(\(([^)]*)\))?\s*(.*)", line)
        if not m:
            continue  # #include etc.
        name, _, params, body = m.groups()
        if params is None:
            defines[name] = body.strip()
        else:
            funcs[name] = ([p.strip() for p in params.split(",")], body)
    text = "\n".join(lines)

    # Only expand the node-generating helpers; keycode macros like HY(X) are
    # understood directly when building labels.
    expandable = [n for n in funcs if n.startswith("MAKE_")]
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, expandable)) + r")\s*\(")
    while m := pattern.search(text):
        args, end = split_args(text, m.end())
        params, body = funcs[m.group(1)]
        mapping = {}
        for i, p in enumerate(params):
            if p == "...":
                mapping["__VA_ARGS__"] = ", ".join(args[i:])
            else:
                mapping[p] = args[i]
        text = text[: m.start()] + substitute(body, mapping) + text[end:]
    return text, defines


# --- Devicetree-ish parsing -------------------------------------------------------


@dataclass
class Binding:
    ref: str
    params: list[str]


@dataclass
class Behavior:
    name: str
    compatible: str
    cells: int
    bindings: list[Binding]
    mods: list[str]


def match_brace(text: str, open_idx: int) -> int:
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("unbalanced braces")


def parse_bindings(s: str) -> list[Binding]:
    out = []
    for piece in s.replace("<", " ").replace(">", " ").split("&")[1:]:
        tokens = piece.replace(",", " ").split()
        out.append(Binding(tokens[0], tokens[1:]))
    return out


def parse_behaviors(text: str) -> dict[str, Behavior]:
    behaviors = {}
    for m in re.finditer(r"(\w+)\s*:\s*([\w-]+)\s*\{", text):
        body = text[m.end() : match_brace(text, m.end() - 1)]
        compat = re.search(r'compatible\s*=\s*"([^"]+)"', body)
        cells = re.search(r"#binding-cells\s*=\s*<(\d+)>", body)
        mods = re.search(r"\bmods\s*=\s*<(.*?)>\s*;", body, re.S)
        bindings = re.search(r"\bbindings\s*=\s*(.*?);", body, re.S)
        behaviors[m.group(1)] = Behavior(
            name=m.group(1),
            compatible=compat.group(1) if compat else "",
            cells=int(cells.group(1)) if cells else 0,
            bindings=parse_bindings(bindings.group(1)) if bindings else [],
            mods=sorted({MOD_FLAGS[f] for f in re.findall(r"MOD_[LR](\w+)", mods.group(1))})
            if mods
            else [],
        )
    return behaviors


def parse_layers(text: str) -> dict[str, list[Binding]]:
    m = re.search(r"\bkeymap\s*\{", text)
    block = text[m.end() : match_brace(text, m.end() - 1)]
    return {
        name: parse_bindings(body)
        for name, body in re.findall(r"(\w+)\s*\{\s*bindings\s*=\s*<(.*?)>\s*;", block, re.S)
    }


# --- Semantics ----------------------------------------------------------------------


@dataclass
class ParsedKey:
    tap: str = ""
    hold: str = ""
    tap_mod: str = ""  # what the key does when `mods` are held while tapping
    mods: str = ""  # e.g. "SHIFT"
    raw: str = ""
    hold_layer: str = ""  # layer node name (e.g. "numbers_layer") activated by hold
    kind: str = ""  # "trans" | "none" | "" (regular key)

    @property
    def label(self) -> str:
        """Main label: tap, plus tap+mod if it differs, e.g. "SPC BS"."""
        if self.tap_mod and self.tap_mod != self.tap:
            return f"{self.tap} {self.tap_mod}"
        return self.tap


class Keymap:
    def __init__(self, path: Path = KEYMAP):
        text, self.defines = preprocess(path.read_text())
        self.behaviors = parse_behaviors(text)
        self.layers = parse_layers(text)

    # keycodes
    def code(self, code: str) -> str:
        while code in self.defines:
            code = self.defines[code]
        if m := re.fullmatch(r"(\w+)\((.*)\)", code):
            return f"{MOD_WRAPPERS.get(m.group(1), m.group(1))}+{self.code(m.group(2))}"
        if code in KEY_NAMES:
            return KEY_NAMES[code]
        if re.fullmatch(r"N\d", code):
            return code[1:]
        if re.fullmatch(r"[A-Z]", code) or re.fullmatch(r"F\d+", code):
            return code
        return code.replace("_", " ")

    @staticmethod
    def override(label: str) -> str:
        return LABEL_OVERRIDES.get(label.upper(), label)

    def layer_name(self, name: str) -> str:
        return name.replace("_", " ")

    def layer_node(self, param: str) -> str:
        """Layer define (e.g. NUMBERS) -> layer node name (e.g. numbers_layer)."""
        return list(self.layers)[int(self.defines.get(param, param))]

    def activating_key(self, target: str) -> tuple[str, int] | None:
        """(layer, index) of the key that, when held, brings up `target`
        (searching layers in keymap order, so the default layer wins)."""
        for name in self.layers:
            if name == target:
                continue
            for i, k in enumerate(self.layer(name)):
                if k.hold_layer == target:
                    return name, i
        return None

    def activating_chain(self, target: str) -> list[tuple[str, int]]:
        """Every held key leading to `target`, as (layer activated, index):
        the key activating `target`, then the key activating the layer that
        key lives on, and so on down to the default layer."""
        chain: list[tuple[str, int]] = []
        seen = {target}
        while (found := self.activating_key(target)) is not None:
            source, i = found
            chain.append((target, i))
            if source in seen:
                break
            seen.add(source)
            target = source
        return chain

    def cells(self, ref: str) -> int:
        if ref in self.behaviors:
            return self.behaviors[ref].cells
        return BUILTIN_CELLS.get(ref, 0)

    def resolve(self, b: Binding) -> ParsedKey:
        p = b.params
        match b.ref:
            case "kp":
                return ParsedKey(tap=self.code(p[0]))
            case "mt":
                return ParsedKey(tap=self.code(p[1]), hold=self.code(p[0]))
            case "lt":
                return ParsedKey(tap=self.code(p[1]), hold=self.layer_name(p[0]),
                                 hold_layer=self.layer_node(p[0]))
            case "mo":
                return ParsedKey(hold=self.layer_name(p[0]), hold_layer=self.layer_node(p[0]))
            case "sk":
                return ParsedKey(tap=f"sticky {self.code(p[0])}")
            case "trans":
                return ParsedKey(kind="trans")
            case "none":
                return ParsedKey(kind="none")
            case "bootloader":
                return ParsedKey(tap="BOOT")
            case "bt":
                return ParsedKey(tap=" ".join(["BT", *p]).replace("_", " "))

        beh = self.behaviors.get(b.ref)
        if beh is None:
            return ParsedKey(tap=f"&{b.ref}")
        if "macro" in beh.compatible:
            # Macros that press `&mo X` bring up a layer while held.
            layer = next((self.layer_node(x.params[0]) for x in beh.bindings if x.ref == "mo"), "")
            return ParsedKey(tap=beh.name, hold_layer=layer)
        if "mod-morph" in beh.compatible:
            normal, morphed = (self.resolve(self.fill(x, [])) for x in beh.bindings[:2])
            return ParsedKey(
                tap=normal.tap, hold=normal.hold, hold_layer=normal.hold_layer,
                tap_mod=morphed.tap, mods="+".join(beh.mods),
            )  # fmt: skip
        if "hold-tap" in beh.compatible:
            hold_b, tap_b = beh.bindings[:2]
            h = self.resolve(self.fill(hold_b, p[0:1]))
            t = self.resolve(self.fill(tap_b, p[1:2]))
            return ParsedKey(
                tap=t.tap, hold=h.tap or h.hold, hold_layer=h.hold_layer,
                tap_mod=t.tap_mod, mods=t.mods,
            )  # fmt: skip
        return ParsedKey(tap=f"&{b.ref}")

    def fill(self, b: Binding, params: list[str]) -> Binding:
        """Behaviours nested in hold-tap may take their parameters from it."""
        if b.params or not self.cells(b.ref):
            return b
        return Binding(b.ref, params[: self.cells(b.ref)])

    def layer(self, name: str) -> list[ParsedKey]:
        keys = []
        for b in self.layers[name]:
            k = self.resolve(b)
            k.tap, k.hold, k.tap_mod = (self.override(t) for t in (k.tap, k.hold, k.tap_mod))
            k.raw = " ".join(["&" + b.ref, *b.params])
            keys.append(k)
        return keys


# --- CLI ------------------------------------------------------------------------------


def position(i: int) -> str:
    if i < 30:
        row, c = divmod(i, 10)
        return f"{'L' if c < 5 else 'R'} row{row} col{c % 5}"
    t = i - 30
    return f"{'L' if t < 3 else 'R'} thumb{t % 3}"


def key_to_dict(i: int, k: ParsedKey) -> dict[str, str]:
    """Key as a dict, omitting empty fields."""
    d = {
        "pos": position(i),
        "binding": k.raw,
        "kind": k.kind,
        "tap": k.tap,
        "hold": k.hold,
        "hold_layer": k.hold_layer,
        "tap_mod": k.tap_mod if k.tap_mod != k.tap else "",
        "mods": k.mods if k.tap_mod != k.tap else "",
        "label": k.label,
    }
    return {name: v for name, v in d.items() if v}


def main() -> None:
    km = Keymap()
    names = sys.argv[1:] or ["default_layer", "alpha_layer"]
    # One key per line: readable and diff-friendly.
    print("{")
    for n, name in enumerate(names):
        print(f"  {json.dumps(name)}: [")
        keys = [key_to_dict(i, k) for i, k in enumerate(km.layer(name))]
        print(",\n".join("    " + json.dumps(k, ensure_ascii=False) for k in keys))
        print("  ]" + ("," if n < len(names) - 1 else ""))
    print("}")


if __name__ == "__main__":
    main()
