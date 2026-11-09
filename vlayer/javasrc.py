"""Java source handling: find methods, replace them, normalise them (Module M1),
insert coverage probes, and read a model answer.

Everything works on the token stream of the `javalang` tokenizer, which skips
comments. Method spans are found from tokens alone, so the same code reads a
whole source file and a model answer that contains only methods.
"""

import hashlib
import re
from dataclasses import dataclass

import javalang

PROBE_PROPERTY = "vlayer.hit"


@dataclass
class Tok:
    kind: str
    value: str
    start: int
    end: int


@dataclass
class Method:
    key: str          # name(paramtypes), with #n added for a repeated signature
    name: str
    nparams: int
    start: int        # offset of the first token (annotation or modifier)
    end: int          # offset just after the closing brace
    body_open: int    # offset of the opening brace of the body
    is_ctor: bool


def tokens(text: str) -> list:
    """Tokens with character offsets. Raises javalang.tokenizer.LexerError."""
    line_starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            line_starts.append(i + 1)
    out = []
    for t in javalang.tokenizer.tokenize(text):
        line, col = t.position.line, t.position.column
        s = line_starts[line - 1] + col - 1
        out.append(Tok(type(t).__name__, t.value, s, s + len(t.value)))
    return out


def _match_forward(toks, i, open_v, close_v):
    depth = 0
    for j in range(i, len(toks)):
        if toks[j].value == open_v:
            depth += 1
        elif toks[j].value == close_v:
            depth -= 1
            if depth == 0:
                return j
    return -1


def _match_back(toks, i, open_v, close_v):
    depth = 0
    for j in range(i, -1, -1):
        if toks[j].value == close_v:
            depth += 1
        elif toks[j].value == open_v:
            depth -= 1
            if depth == 0:
                return j
    return -1


def _param_types(ptoks) -> list:
    """Split the tokens between the parentheses into parameter types."""
    params, cur, depth = [], [], 0
    for t in ptoks:
        v = t.value
        if v in ("<", "(", "["):
            depth += 1
        elif v in (">", ")", "]"):
            depth -= 1
        elif v == ">>":
            depth -= 2
        elif v == ">>>":
            depth -= 3
        if v == "," and depth == 0:
            params.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        params.append(cur)
    types = []
    for p in params:
        vals, skip = [], False
        for t in p[:-1]:                       # the last token is the name
            if t.value == "@":
                skip = True                    # skip the annotation name
                continue
            if skip:
                skip = False
                continue
            if t.value == "final":
                continue
            vals.append(t.value)
        types.append("".join(vals))
    return types


_TYPE_END = {"Identifier", "BasicType"}


def find_methods(text: str, toks=None) -> list:
    """All methods and constructors that have a body, in source order."""
    toks = toks if toks is not None else tokens(text)
    found, seen = [], {}
    for i in range(len(toks) - 1):
        t = toks[i]
        if t.kind != "Identifier" or toks[i + 1].value != "(":
            continue
        prev = toks[i - 1] if i > 0 else None
        if prev is not None and prev.value in ("new", ".", ","):
            continue
        close = _match_forward(toks, i + 1, "(", ")")
        if close < 0 or close + 1 >= len(toks) or toks[close + 1].value not in ("{", "throws"):
            continue
        k = close + 1
        while k < len(toks) and toks[k].value not in ("{", ";"):
            k += 1
        if k >= len(toks) or toks[k].value != "{":
            continue
        body_close = _match_forward(toks, k, "{", "}")
        if body_close < 0:
            continue
        # walk back to the start of the declaration (annotations, modifiers)
        s = i - 1
        while s >= 0:
            v = toks[s].value
            if v == ")":
                s = _match_back(toks, s, "(", ")") - 1
                continue
            if v in (";", "{", "}"):
                break
            s -= 1
        start_tok = s + 1
        is_ctor = prev is None or not (prev.kind in _TYPE_END or prev.value in ("void", "]", ">", ">>", ">>>"))
        types = _param_types(toks[i + 2:close])
        base = f"{t.value}({','.join(types)})"
        seen[base] = seen.get(base, 0) + 1
        key = base if seen[base] == 1 else f"{base}#{seen[base]}"
        found.append(Method(key, t.value, len(types), toks[start_tok].start,
                            toks[body_close].end, toks[k].start, is_ctor))
    # drop methods nested inside another found method (e.g. in anonymous classes)
    outer = []
    for m in found:
        if not any(o.start <= m.start and m.end <= o.end and o is not m for o in found):
            outer.append(m)
    return outer


def method_texts(text: str) -> dict:
    return {m.key: text[m.start:m.end] for m in find_methods(text)}


def replace_methods(text: str, new: dict) -> str:
    """Replace the methods named by key with new text (other code unchanged)."""
    methods = [m for m in find_methods(text) if m.key in new]
    for m in sorted(methods, key=lambda m: -m.start):
        text = text[:m.start] + new[m.key] + text[m.end:]
    return text


def token_values(text: str) -> list:
    return [t.value for t in tokens(text)]


def faulty_methods(buggy: str, fixed: str):
    """Methods changed by the developer's fix.

    Returns (keys, reason). keys is None when the fix is not confined to the
    bodies of existing methods, which excludes the bug.
    """
    try:
        mb, mf = method_texts(buggy), method_texts(fixed)
    except Exception as e:  # noqa: BLE001
        return None, f"cannot tokenise: {e}"
    if set(mb) != set(mf):
        return None, "fix adds, removes or renames a method"
    changed = [k for k in mb if token_values(mb[k]) != token_values(mf[k])]
    if not changed:
        return None, "fix changes no method"
    rebuilt = replace_methods(buggy, {k: mf[k] for k in changed})
    if token_values(rebuilt) != token_values(fixed):
        return None, "fix changes code outside methods"
    return changed, "ok"


# ---------------------------------------------------------------- Module M1

_LOCAL_NODES = (
    javalang.tree.VariableDeclaration,       # includes LocalVariableDeclaration
    javalang.tree.FormalParameter,
    javalang.tree.CatchClauseParameter,
    javalang.tree.InferredFormalParameter,
    javalang.tree.TryResource,
)


def _local_names(method_text: str) -> set:
    tree = javalang.parse.parse("class V__ {\n" + method_text + "\n}")
    names = set()
    for _, node in tree.filter(javalang.tree.Node):
        if isinstance(node, _LOCAL_NODES):
            if hasattr(node, "declarators") and node.declarators:
                names.update(d.name for d in node.declarators)
            elif getattr(node, "name", None):
                names.add(node.name)
    return names


def normalise(method_text: str) -> str:
    """Layout, comments and local variable names removed (Module M1).

    Locals (parameters included) are renamed v0, v1, ... in order of first
    appearance. Nothing that can change behaviour is touched, so two patches
    with the same normal form behave identically on deterministic tests.
    Falls back to tokens only, then to whitespace only, if parsing fails.
    """
    try:
        toks = tokens(method_text)
    except Exception:  # noqa: BLE001
        no_comments = re.sub(r"//[^\n]*|/\*.*?\*/", " ", method_text, flags=re.S)
        return " ".join(no_comments.split())
    try:
        local = _local_names(method_text)
    except Exception:  # noqa: BLE001
        local = set()
    rename, out = {}, []
    for i, t in enumerate(toks):
        v = t.value
        if (t.kind == "Identifier" and v in local
                and (i == 0 or toks[i - 1].value != ".")
                and (i + 1 >= len(toks) or toks[i + 1].value != "(")):
            v = rename.setdefault(v, f"v{len(rename)}")
        out.append(v)
    return " ".join(out)


def candidate_key(methods: dict) -> str:
    """Key of a candidate: hash of its normalised methods, in key order."""
    norm = "\n".join(f"{k}:{normalise(methods[k])}" for k in sorted(methods))
    return hashlib.sha256(norm.encode()).hexdigest()[:20]


# ---------------------------------------------------------------- coverage probe

def insert_probes(text: str, keys: list) -> str:
    """Insert a statement that sets a system property at the start of each
    listed method; the runner reads it to tell which test classes reach them."""
    toks = tokens(text)
    methods = [m for m in find_methods(text, toks) if m.key in keys]
    stmt = f' System.setProperty("{PROBE_PROPERTY}", "1"); '
    inserts = []
    for m in methods:
        pos = m.body_open + 1
        if m.is_ctor:   # must follow an explicit this(...) / super(...) call
            idx = next(i for i, t in enumerate(toks) if t.start == m.body_open)
            if idx + 2 < len(toks) and toks[idx + 1].value in ("this", "super") and toks[idx + 2].value == "(":
                close = _match_forward(toks, idx + 2, "(", ")")
                if close + 1 < len(toks) and toks[close + 1].value == ";":
                    pos = toks[close + 1].end
        inserts.append(pos)
    for pos in sorted(inserts, reverse=True):
        text = text[:pos] + stmt + text[pos:]
    return text


# ---------------------------------------------------------------- model answers

_FENCE = re.compile(r"```[ \t]*([A-Za-z]*)[ \t]*\n(.*?)```", re.S)


def extract_code(answer: str) -> str:
    blocks = _FENCE.findall(answer or "")
    for lang, body in blocks:
        if lang.lower() in ("java", ""):
            return body
    return blocks[0][1] if blocks else (answer or "")


def parse_answer(answer: str, faulty: list):
    """Map the model's answer onto the given methods.

    faulty: list of (key, name, nparams). Returns {key: new text} for the
    methods found in the answer, or None if none can be read.
    """
    code = extract_code(answer)
    try:
        found = find_methods(code)
    except Exception:  # noqa: BLE001
        return None
    by_key = {m.key: m for m in found}
    out = {}
    for key, name, nparams in faulty:
        m = by_key.get(key)
        if m is None:
            same = [x for x in found if x.name == name and x.nparams == nparams]
            m = same[0] if same else None
        if m is not None:
            out[key] = code[m.start:m.end]
    return out or None
