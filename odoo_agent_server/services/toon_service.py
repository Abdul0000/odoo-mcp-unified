"""TOON (Token-Oriented Object Notation) encoder/decoder.

Stdlib only. Used to render tool results for the model: uniform arrays of
objects (file indexes, grep hits, lint findings, job lists, search_read rows)
collapse to a header plus rows, which costs far fewer tokens than repeating
every key in JSON.

Rules implemented (comma delimiter, 2-space indent):
  * object            ->  ``key: value`` lines, nested objects indented
  * primitive array   ->  ``key[N]: a,b,c``
  * uniform objects   ->  ``key[N]{f1,f2}:`` then one indented row per item
  * anything else     ->  ``key[N]:`` then ``- item`` lines
Strings are quoted only when needed. ``render_tool_text`` returns TOON only
when it is actually shorter than compact JSON, otherwise compact JSON, so it
can never make a response bigger.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

INDENT = "  "
_UNQUOTED_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_NUMBERISH = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|0\d+")
_RESERVED = {"true", "false", "null"}
_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


class ToonError(ValueError):
    pass


# --------------------------------------------------------------------------- encode
def _needs_quotes(text: str) -> bool:
    if text == "" or text != text.strip():
        return True
    if text in _RESERVED or _NUMBERISH.fullmatch(text):
        return True
    if text.startswith("-"):
        return True
    for ch in text:
        if ch in ',:"\\[]{}' or ord(ch) < 32:
            return True
    return False


def _quote(text: str) -> str:
    return '"' + "".join(_ESCAPES.get(ch, ch) for ch in text) + '"'


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return "null"
        if value == int(value) and abs(value) < 1e15:
            return str(int(value))
        return repr(value)
    text = str(value)
    return _quote(text) if _needs_quotes(text) else text


def _key(key: Any) -> str:
    text = str(key)
    return text if _UNQUOTED_KEY.fullmatch(text) else _quote(text)


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _tabular_fields(items: list[Any]) -> list[str] | None:
    if not items or not all(isinstance(i, dict) and i for i in items):
        return None
    fields = list(items[0].keys())
    for item in items:
        if list(item.keys()) != fields:
            if set(item.keys()) != set(fields):
                return None
        if not all(_is_scalar(v) for v in item.values()):
            return None
    return fields


def _encode_array(key: str | None, items: list[Any], depth: int, out: list[str]) -> None:
    pad = INDENT * depth
    head = (_key(key) if key is not None else "") + f"[{len(items)}]"
    if not items:
        out.append(f"{pad}{head}:")
        return
    if all(_is_scalar(i) for i in items):
        out.append(f"{pad}{head}: " + ",".join(_scalar(i) for i in items))
        return
    fields = _tabular_fields(items)
    if fields is not None:
        out.append(f"{pad}{head}{{{','.join(_key(f) for f in fields)}}}:")
        for item in items:
            out.append(f"{pad}{INDENT}" + ",".join(_scalar(item[f]) for f in fields))
        return
    out.append(f"{pad}{head}:")
    for item in items:
        _encode_list_item(item, depth + 1, out)


def _encode_list_item(item: Any, depth: int, out: list[str]) -> None:
    pad = INDENT * depth
    if _is_scalar(item):
        out.append(f"{pad}- {_scalar(item)}")
    elif isinstance(item, list):
        sub: list[str] = []
        _encode_array(None, item, 0, sub)
        out.append(f"{pad}- {sub[0]}")
        out.extend(f"{pad}{INDENT}{line}" for line in sub[1:])
    elif isinstance(item, dict):
        if not item:
            out.append(f"{pad}-")
            return
        sub = []
        _encode_object(item, 0, sub)
        out.append(f"{pad}- {sub[0]}")
        out.extend(f"{pad}{INDENT}{line}" for line in sub[1:])


def _encode_object(obj: dict[str, Any], depth: int, out: list[str]) -> None:
    pad = INDENT * depth
    for key, value in obj.items():
        if _is_scalar(value):
            out.append(f"{pad}{_key(key)}: {_scalar(value)}")
        elif isinstance(value, list):
            _encode_array(key, value, depth, out)
        elif isinstance(value, dict):
            if not value:
                out.append(f"{pad}{_key(key)}:")
            else:
                out.append(f"{pad}{_key(key)}:")
                _encode_object(value, depth + 1, out)
        else:
            out.append(f"{pad}{_key(key)}: {_scalar(str(value))}")


def encode(value: Any) -> str:
    """Encode JSON-compatible data as TOON."""
    out: list[str] = []
    if _is_scalar(value):
        return _scalar(value)
    if isinstance(value, list):
        _encode_array(None, value, 0, out)
    elif isinstance(value, dict):
        if not value:
            return ""
        _encode_object(value, 0, out)
    else:
        return _scalar(str(value))
    return "\n".join(out)


# --------------------------------------------------------------------------- decode
def _split_csv(text: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    in_q = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_q:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(text):
                i += 1
                buf.append(text[i])
            elif ch == '"':
                in_q = False
        elif ch == '"':
            in_q = True
            buf.append(ch)
        elif ch == ",":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def _parse_scalar(raw: str) -> Any:
    token = raw.strip()
    if token.startswith('"'):
        if not token.endswith('"') or len(token) < 2:
            raise ToonError(f"unterminated string: {raw!r}")
        body = token[1:-1]
        out: list[str] = []
        i = 0
        rev = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
        while i < len(body):
            if body[i] == "\\" and i + 1 < len(body):
                out.append(rev.get(body[i + 1], body[i + 1]))
                i += 2
            else:
                out.append(body[i])
                i += 1
        return "".join(out)
    if token == "true":
        return True
    if token == "false":
        return False
    if token == "null":
        return None
    if re.fullmatch(r"-?\d+", token):
        return int(token)
    if re.fullmatch(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", token):
        return float(token)
    return token


_HEADER = re.compile(r'^(?P<key>"(?:[^"\\]|\\.)*"|[^\s:\[\]{}"][^:\[\]{}]*?)?\[(?P<n>\d+)\](?:\{(?P<fields>[^}]*)\})?:(?P<rest>.*)$')


def decode(text: str) -> Any:
    lines = [(len(l) - len(l.lstrip(" ")), l.strip()) for l in text.split("\n") if l.strip()]
    if not lines:
        return {}
    value, pos = _decode_block(lines, 0, lines[0][0])
    if pos != len(lines):
        raise ToonError("trailing content")
    return value


def _key_text(raw: str) -> str:
    raw = raw.strip()
    return _parse_scalar(raw) if raw.startswith('"') else raw


def _decode_array(header: re.Match, lines, pos: int, indent: int):
    count = int(header.group("n"))
    rest = header.group("rest").strip()
    fields = header.group("fields")
    if fields is not None:
        names = [_key_text(f) for f in _split_csv(fields)]
        rows = []
        while len(rows) < count and pos < len(lines) and lines[pos][0] > indent:
            cells = [_parse_scalar(c) for c in _split_csv(lines[pos][1])]
            if len(cells) != len(names):
                raise ToonError("row width mismatch")
            rows.append(dict(zip(names, cells)))
            pos += 1
        if len(rows) != count:
            raise ToonError("row count mismatch")
        return rows, pos
    if rest:
        items = [_parse_scalar(c) for c in _split_csv(rest)]
        if len(items) != count:
            raise ToonError("array length mismatch")
        return items, pos
    items = []
    while len(items) < count and pos < len(lines) and lines[pos][0] > indent and lines[pos][1].startswith("-"):
        item_indent, content = lines[pos]
        body = content[1:].strip()
        if not body:
            items.append({})
            pos += 1
            continue
        sub_header = _HEADER.match(body)
        if sub_header and not sub_header.group("key"):
            val, pos = _decode_array(sub_header, lines, pos + 1, item_indent)
            items.append(val)
            continue
        if _looks_like_field(body):
            synthetic = [(item_indent + 2, body)]
            j = pos + 1
            while j < len(lines) and lines[j][0] > item_indent:
                synthetic.append((lines[j][0], lines[j][1]))
                j += 1
            val, _ = _decode_block(synthetic, 0, item_indent + 2)
            items.append(val)
            pos = j
        else:
            items.append(_parse_scalar(body))
            pos += 1
    if len(items) != count:
        raise ToonError("list length mismatch")
    return items, pos


def _looks_like_field(body: str) -> bool:
    if _HEADER.match(body):
        return True
    if body.startswith('"'):
        m = re.match(r'^"(?:[^"\\]|\\.)*":', body)
        return bool(m)
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_.]*:(\s|$)", body))


def _decode_block(lines, pos: int, indent: int):
    first = lines[pos][1]
    header = _HEADER.match(first)
    if header and not header.group("key") and lines[pos][0] == indent:
        return _decode_array(header, lines, pos + 1, indent)
    if not _looks_like_field(first) and len(lines) == 1:
        return _parse_scalar(first), pos + 1
    obj: dict[str, Any] = {}
    while pos < len(lines) and lines[pos][0] == indent:
        content = lines[pos][1]
        header = _HEADER.match(content)
        if header and header.group("key"):
            key = _key_text(header.group("key"))
            obj[key], pos = _decode_array(header, lines, pos + 1, indent)
            continue
        m = re.match(r'^(?P<k>"(?:[^"\\]|\\.)*"|[^:]+?):(?P<v>.*)$', content)
        if not m:
            raise ToonError(f"cannot parse line: {content!r}")
        key = _key_text(m.group("k"))
        rest = m.group("v").strip()
        if rest:
            obj[key] = _parse_scalar(rest)
            pos += 1
        else:
            pos += 1
            if pos < len(lines) and lines[pos][0] > indent:
                obj[key], pos = _decode_block(lines, pos, lines[pos][0])
            else:
                obj[key] = {}
    return obj, pos


# --------------------------------------------------------------------------- rendering
def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


BLOCK_KEYS = frozenset({"text", "diff", "ctx", "content"})


def _extract_blocks(value: Any, keys: frozenset[str], blocks: list[str]) -> Any:
    """Replace multi-line string fields (code, diffs) with a block marker; raw text is appended later."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in keys and isinstance(v, str) and "\n" in v:
                blocks.append(v)
                out[k] = f"<<block {len(blocks)}>>"
            else:
                out[k] = _extract_blocks(v, keys, blocks)
        return out
    if isinstance(value, list):
        return [_extract_blocks(v, keys, blocks) for v in value]
    return value


def render_tool_text(value: Any, *, enabled: bool = True, block_keys: frozenset[str] | None = None) -> str:
    """Return the shortest faithful text form of a tool result.

    Multi-line code/diff strings under ``block_keys`` are emitted raw after the TOON body
    (no escaping, far fewer tokens, easier for the model to read). Falls back to compact
    JSON when TOON is disabled, fails, does not round-trip, or is not smaller.
    """
    as_json = compact_json(value)
    if not enabled:
        return as_json
    try:
        plain = json.loads(as_json)
        blocks: list[str] = []
        body_source = _extract_blocks(plain, block_keys, blocks) if block_keys else plain
        candidate = encode(body_source)
        if decode(candidate) != body_source:
            return as_json
        if blocks:
            candidate += "".join(f"\n\n<<block {i}>>\n{text}\n<</block {i}>>" for i, text in enumerate(blocks, 1))
        return candidate if len(candidate) < len(as_json) else as_json
    except Exception:  # noqa: BLE001 - rendering must never break a tool call
        return as_json
