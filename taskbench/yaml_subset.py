"""A minimal YAML reader and writer for the subset taskbench packets use.

This is not a full YAML implementation. It exists so taskbench can stay on
the Python standard library. Supported, and tested:

* mappings and nested mappings, indented with spaces (tabs are an error)
* block sequences of scalars (``- item``)
* block sequences of mappings (``- key: value`` plus indented continuation keys)
* flow sequences of scalars (``[a, b]``)
* folded (``>``, ``>-``, ``>+``) and literal (``|``, ``|-``, ``|+``) block scalars
* ``null`` / ``~``, ``true``, ``false``, integers, plain strings, single- and
  double-quoted strings
* comments that are a whole line, or an inline ``#`` preceded by whitespace
  and not inside quotes
* ``key:`` with an empty value, meaning a nested block or null

Not supported, and rejected: anchors (``&``), aliases (``*``), tags
(``!`` / ``!!``), multi-document markers (``---``, ``...``), flow mappings,
quoted keys, merge keys, and duplicate keys. A sequence scalar that contains
``": "`` is parsed as a mapping entry; quote it if that is not what you want.

Lines are split on ``\\n`` only, after ``\\r\\n`` is normalized to ``\\n``.
A lone ``\\r``, U+0085, U+2028, U+2029, or other control character is not a
line break. The writer emits any string that contains one of those as a
double-quoted JSON scalar (``ensure_ascii``) so it cannot become a new key
on the way back in.

The writer emits this same subset. Parse, emit, parse is stable for values
taskbench stores. Folded scalars come back as plain or quoted strings, so the
text bytes may differ while the parsed value does not.
"""

from __future__ import annotations

import json
import re
from typing import Any


class YamlSubsetError(ValueError):
    """The document uses YAML this parser does not implement, or is malformed."""


_BLOCK_STYLES = {">", "|", ">-", "|-", ">+", "|+"}


def load(text: str) -> Any:
    """Parse a YAML subset document. An empty document returns ``None``."""
    parser = _Parser(text)
    return parser.parse_document()


def dump(value: Any) -> str:
    """Serialize ``value`` as a YAML subset document ending in a newline."""
    rendered = _emit(value, 0)
    if rendered == "":
        return "null\n"
    if not rendered.endswith("\n"):
        rendered += "\n"
    return rendered


def _document_lines(text: str) -> list[str]:
    """Split on ``\\n`` only. ``\\r\\n`` is normalized first; lone ``\\r`` is not."""
    normalized = text.replace("\r\n", "\n")
    if normalized.endswith("\n"):
        normalized = normalized[:-1]
    if normalized == "":
        return []
    return normalized.split("\n")


class _Parser:
    def __init__(self, text: str) -> None:
        self.lines = _document_lines(text)
        self.n = len(self.lines)
        self.i = 0

    def parse_document(self) -> Any:
        self._skip_ignorable()
        if self.i >= self.n:
            return None
        line = self.lines[self.i]
        indent = _indent(line, self.i)
        stripped = line.strip()
        _reject_yaml_feature(stripped, self.i)
        if stripped in {"{}", "[]"}:
            self.i += 1
            return {} if stripped == "{}" else []
        if stripped.startswith("- "):
            return self._parse_sequence(indent)
        return self._parse_mapping(indent)

    def _parse_mapping(self, indent: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        while True:
            self._skip_ignorable()
            if self.i >= self.n:
                break
            line = self.lines[self.i]
            current = _indent(line, self.i)
            if current < indent:
                break
            stripped = line.strip()
            _reject_yaml_feature(stripped, self.i)
            if stripped.startswith("- "):
                break
            if current > indent:
                raise YamlSubsetError(
                    f"line {self.i + 1}: unexpected indent {current}, expected {indent}"
                )
            key, rest = _split_key(stripped, self.i)
            if key in result:
                raise YamlSubsetError(f"line {self.i + 1}: duplicate key {key!r}")
            self.i += 1
            result[key] = self._parse_value(rest, indent)
        return result

    def _parse_sequence(self, indent: int) -> list[Any]:
        items: list[Any] = []
        while True:
            self._skip_ignorable()
            if self.i >= self.n:
                break
            line = self.lines[self.i]
            current = _indent(line, self.i)
            if current < indent:
                break
            stripped = line.strip()
            if not stripped.startswith("- "):
                break
            if current > indent:
                raise YamlSubsetError(
                    f"line {self.i + 1}: unexpected indent in sequence"
                )
            rest = stripped[2:]
            _reject_yaml_feature(rest.strip(), self.i)
            self.i += 1
            if rest.strip() == "":
                items.append(self._parse_value("", indent))
                continue
            comment_stripped = _strip_comment(rest).strip()
            if _looks_like_mapping_entry(comment_stripped):
                key, valpart = _split_key(comment_stripped, self.i - 1)
                item: dict[str, Any] = {}
                child_indent = indent + 2
                if valpart in _BLOCK_STYLES:
                    item[key] = self._parse_block_scalar(valpart, indent)
                elif valpart == "":
                    item[key] = self._parse_value("", indent)
                else:
                    item[key] = _parse_scalar(valpart)
                extra = self._parse_mapping(child_indent)
                for extra_key in extra:
                    if extra_key in item:
                        raise YamlSubsetError(
                            f"line {self.i + 1}: duplicate key {extra_key!r}"
                        )
                item.update(extra)
                items.append(item)
            else:
                items.append(_parse_scalar(rest))
        return items

    def _parse_value(self, rest: str, parent_indent: int) -> Any:
        rest = _strip_comment(rest).strip()
        if rest in _BLOCK_STYLES:
            return self._parse_block_scalar(rest, parent_indent)
        if rest == "":
            self._skip_ignorable()
            if self.i >= self.n:
                return None
            nxt = self.lines[self.i]
            current = _indent(nxt, self.i)
            if current <= parent_indent:
                return None
            if nxt.strip().startswith("- "):
                return self._parse_sequence(current)
            return self._parse_mapping(current)
        if rest.startswith("["):
            return _parse_flow_sequence(rest, self.i)
        return _parse_scalar(rest)

    def _parse_block_scalar(self, style: str, parent_indent: int) -> str:
        raw_lines: list[str] = []
        content_indent: int | None = None
        while self.i < self.n:
            line = self.lines[self.i]
            if line.strip() == "":
                if content_indent is None:
                    # A leading blank line is part of the scalar only after
                    # content has started. Keep it if more content follows
                    # at a deeper indent; otherwise the block is empty.
                    peek = self._peek_content(parent_indent)
                    if peek is None:
                        break
                    raw_lines.append("")
                    self.i += 1
                    continue
                raw_lines.append("")
                self.i += 1
                continue
            current = _indent(line, self.i)
            if current <= parent_indent:
                break
            if content_indent is None:
                content_indent = current
            if current < content_indent:
                break
            raw_lines.append(line[content_indent:])
            self.i += 1
        folded = style.startswith(">")
        if folded:
            text = _fold(raw_lines)
        else:
            text = "\n".join(raw_lines)
            if raw_lines:
                text += "\n"
        chomp = "clip"
        if style.endswith("-"):
            chomp = "strip"
        elif style.endswith("+"):
            chomp = "keep"
        if chomp == "strip":
            return text.rstrip("\n")
        if chomp == "clip":
            stripped = text.rstrip("\n")
            return stripped + ("\n" if stripped else "")
        return text

    def _peek_content(self, parent_indent: int) -> int | None:
        j = self.i
        while j < self.n:
            line = self.lines[j]
            if line.strip() == "":
                j += 1
                continue
            current = _indent(line, j)
            if current <= parent_indent:
                return None
            return j
        return None

    def _skip_ignorable(self) -> None:
        while self.i < self.n:
            stripped = self.lines[self.i].strip()
            if stripped == "" or stripped.startswith("#"):
                self.i += 1
                continue
            break


def _indent(line: str, index: int) -> int:
    count = 0
    for char in line:
        if char == " ":
            count += 1
            continue
        if char == "\t":
            raise YamlSubsetError(f"line {index + 1}: tabs are not supported")
        break
    return count


def _reject_yaml_feature(text: str, index: int) -> None:
    """Reject anchors, aliases, tags, and document markers as tokens."""
    stripped = text.strip()
    if not stripped:
        return
    if stripped in {"---", "..."} or stripped.startswith("--- ") or stripped.startswith("... "):
        raise YamlSubsetError(f"line {index + 1}: document markers are not supported")
    token = stripped
    if token[0] in "'\"":
        return
    if token.startswith(("!", "&", "*")):
        raise YamlSubsetError(f"line {index + 1}: anchors, aliases, and tags are not supported")


def _split_key(stripped: str, index: int) -> tuple[str, str]:
    if stripped.startswith('"') or stripped.startswith("'"):
        raise YamlSubsetError(f"line {index + 1}: quoted keys are not supported")
    colon = stripped.find(":")
    if colon <= 0:
        raise YamlSubsetError(f"line {index + 1}: expected 'key:'")
    key = stripped[:colon].strip()
    if not key:
        raise YamlSubsetError(f"line {index + 1}: empty key")
    if key.startswith(("!", "&", "*")):
        raise YamlSubsetError(f"line {index + 1}: anchors, aliases, and tags are not supported")
    rest = stripped[colon + 1 :]
    _reject_yaml_feature(rest, index)
    return key, rest


def _looks_like_mapping_entry(text: str) -> bool:
    if not text or text[0] in "'\"":
        return False
    colon = text.find(":")
    if colon <= 0:
        return False
    after = text[colon + 1 :]
    return after == "" or after[0].isspace()


def _strip_comment(text: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_double:
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            continue
        if char == "#" and not in_single and not in_double:
            if index == 0 or text[index - 1].isspace():
                return text[:index]
    return text


def _parse_scalar(text: str) -> Any:
    text = _strip_comment(text).strip()
    if text.startswith(("!", "&", "*")):
        raise YamlSubsetError("anchors, aliases, and tags are not supported")
    if text == "" or text in {"null", "Null", "NULL", "~"}:
        return None
    if text == "{}":
        return {}
    if text == "[]":
        return []
    if text in {"true", "True", "TRUE"}:
        return True
    if text in {"false", "False", "FALSE"}:
        return False
    if re.fullmatch(r"-?(0|[1-9][0-9]*)", text):
        try:
            return int(text)
        except ValueError:
            return text
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return _unquote(text)
    return text


def _unquote(text: str) -> str:
    quote = text[0]
    inner = text[1:-1]
    if quote == "'":
        return inner.replace("''", "'")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise YamlSubsetError(f"bad double-quoted string: {exc}") from exc


def _parse_flow_sequence(text: str, index: int) -> list[Any]:
    body = _strip_comment(text).strip()
    if not (body.startswith("[") and body.endswith("]")):
        raise YamlSubsetError(f"line {index + 1}: bad flow sequence")
    inner = body[1:-1].strip()
    if inner == "":
        return []
    parts = _split_commas(inner)
    return [_parse_scalar(part) for part in parts]


def _split_commas(text: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    escaped = False
    for char in text:
        if escaped:
            buf.append(char)
            escaped = False
            continue
        if char == "\\" and in_double:
            buf.append(char)
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            buf.append(char)
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            buf.append(char)
            continue
        if char == "," and not in_single and not in_double:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(char)
    parts.append("".join(buf))
    return parts


def _fold(lines: list[str]) -> str:
    paragraphs: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            paragraphs.append(" ".join(part.strip() for part in buf if part.strip()))
            buf.clear()

    for line in lines:
        if line.strip() == "":
            flush()
            if not paragraphs or paragraphs[-1] != "":
                paragraphs.append("")
            continue
        buf.append(line)
    flush()
    while paragraphs and paragraphs[-1] == "":
        paragraphs.pop()
    if not paragraphs:
        return ""
    return "\n".join(paragraphs) + "\n"


def _emit(value: Any, indent: int) -> str:
    pad = " " * indent
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return _emit_string(value)
    if isinstance(value, list):
        if not value:
            return "[]"
        lines: list[str] = []
        for item in value:
            if isinstance(item, dict):
                if not item:
                    lines.append(f"{pad}- {{}}")
                    continue
                first = True
                for key, item_value in item.items():
                    rendered = _emit_pair(key, item_value, indent + 2)
                    if first:
                        # "- " replaces two spaces of the child's indent.
                        child = _emit_pair(key, item_value, indent + 2)
                        lines.append(f"{pad}- {child[indent + 2:]}")
                        first = False
                    else:
                        lines.append(rendered)
                continue
            if isinstance(item, list):
                raise YamlSubsetError("nested sequences are not supported")
            scalar = _emit(item, 0)
            if "\n" in scalar:
                parts = scalar.split("\n")
                lines.append(f"{pad}- {parts[0]}")
                for extra in parts[1:]:
                    lines.append(f"{pad}  {extra}")
            else:
                lines.append(f"{pad}- {scalar}")
        return "\n".join(lines)
    if isinstance(value, dict):
        if not value:
            return "{}"
        return "\n".join(_emit_pair(key, item, indent) for key, item in value.items())
    raise YamlSubsetError(f"cannot emit {type(value).__name__}")


def _emit_pair(key: str, value: Any, indent: int) -> str:
    pad = " " * indent
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", key):
        key_text = json.dumps(key)
    else:
        key_text = key
    if isinstance(value, dict):
        if not value:
            return f"{pad}{key_text}: {{}}"
        nested = _emit(value, indent + 2)
        return f"{pad}{key_text}:\n{nested}"
    if isinstance(value, list):
        if not value:
            return f"{pad}{key_text}: []"
        nested = _emit(value, indent + 2)
        return f"{pad}{key_text}:\n{nested}"
    if isinstance(value, str) and (_must_quote(value) or "\n" in value):
        return f"{pad}{key_text}: {json.dumps(value, ensure_ascii=True)}"
    return f"{pad}{key_text}: {_emit(value, indent)}"


def _must_quote(value: str) -> bool:
    """True when a character could be read as a line break or a separator."""
    for char in value:
        code = ord(char)
        if char == "\n":
            continue
        if code < 32 or code == 127 or 0x80 <= code <= 0x9F or char in {"\u2028", "\u2029"}:
            return True
    return False


def _emit_string(value: str) -> str:
    if value == "":
        return '""'
    if _must_quote(value):
        return json.dumps(value, ensure_ascii=True)
    if value in {"null", "Null", "NULL", "~", "true", "false", "True", "False"}:
        return json.dumps(value)
    if re.fullmatch(r"-?(0|[1-9][0-9]*)", value):
        return json.dumps(value)
    if "\n" in value:
        return json.dumps(value, ensure_ascii=True)
    if _safe_plain(value):
        return value
    return json.dumps(value, ensure_ascii=True)


def _safe_plain(value: str) -> bool:
    if value.strip() != value:
        return False
    if value.startswith(("'", '"', "[", "{", "*", "&", "!", "|", ">", "%", "@", "`")):
        return False
    if ": " in value or value.endswith(":"):
        return False
    if "#" in value:
        return False
    if any(ch in value for ch in "{}[],&*!|>%@`"):
        return False
    if _must_quote(value):
        return False
    return True
