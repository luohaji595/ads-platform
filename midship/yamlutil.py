"""极小 YAML 读取器，只覆盖本目录 rules.yaml 这种结构。不引入 PyYAML 依赖。"""

from __future__ import annotations

from typing import Any


def load(text: str) -> Any:
    lines = [ln.rstrip("\n") for ln in text.splitlines()]
    value, _ = _parse_block(lines, 0, 0)
    return value


def load_path(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return load(f.read())


def _parse_block(lines: list[str], i: int, indent: int) -> tuple[Any, int]:
    while i < len(lines):
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        break
    if i >= len(lines):
        return {}, i
    stripped = lines[i][indent:] if lines[i].startswith(" " * indent) else lines[i].lstrip()
    if stripped.startswith("- "):
        return _parse_list(lines, i, indent)
    return _parse_map(lines, i, indent)


def _parse_map(lines: list[str], i: int, indent: int) -> tuple[dict, int]:
    out: dict[str, Any] = {}
    while i < len(lines):
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        cur = _indent(raw)
        if cur < indent:
            break
        if cur > indent:
            raise ValueError(f"YAML 缩进异常，第 {i + 1} 行")
        body = raw[indent:]
        if body.startswith("- "):
            break
        if ":" not in body:
            raise ValueError(f"YAML 缺冒号，第 {i + 1} 行：{raw}")
        key, rest = body.split(":", 1)
        key = key.strip()
        rest = rest.strip()
        i += 1
        if rest == "" or rest == "|":
            nxt = _next_content(lines, i)
            if nxt is None:
                out[key] = None if rest == "" else ""
            else:
                ni = _indent(lines[nxt])
                if ni > indent:
                    val, i = _parse_block(lines, i, ni)
                    out[key] = val
                else:
                    out[key] = None
        else:
            out[key] = _parse_scalar(rest)
    return out, i


def _parse_list(lines: list[str], i: int, indent: int) -> tuple[list, int]:
    out: list[Any] = []
    while i < len(lines):
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        cur = _indent(raw)
        if cur < indent:
            break
        if cur > indent:
            raise ValueError(f"YAML 列表缩进异常，第 {i + 1} 行")
        body = raw[indent:]
        if not body.startswith("- "):
            break
        item = body[2:].strip()
        i += 1
        if item == "":
            nxt = _next_content(lines, i)
            if nxt is None:
                out.append(None)
            else:
                ni = _indent(lines[nxt])
                val, i = _parse_block(lines, i, ni)
                out.append(val)
        elif item.startswith("{") and item.endswith("}"):
            out.append(_parse_flow_map(item))
        elif ":" in item and not item.startswith("[") and not (item.startswith('"') or item.startswith("'")):
            # compact "- key: value" plus following indented keys
            key, rest = item.split(":", 1)
            base = {key.strip(): _parse_scalar(rest.strip()) if rest.strip() else None}
            nxt = _next_content(lines, i)
            if nxt is not None and _indent(lines[nxt]) > indent:
                more, i = _parse_map(lines, i, _indent(lines[nxt]))
                base.update(more)
            elif rest.strip() == "":
                # value on following lines
                pass
            out.append(base)
        else:
            out.append(_parse_scalar(item))
    return out, i


def _parse_flow_map(text: str) -> dict:
    inner = text.strip()
    if inner.startswith("{") and inner.endswith("}"):
        inner = inner[1:-1].strip()
    out: dict[str, Any] = {}
    if not inner:
        return out
    # type may be a flow list: type: [a, b]
    parts = _split_flow(inner)
    for part in parts:
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        out[k.strip()] = _parse_scalar(v.strip())
    return out


def _split_flow(text: str) -> list[str]:
    parts = []
    buf = []
    depth = 0
    for ch in text:
        if ch == "[":
            depth += 1
            buf.append(ch)
        elif ch == "]":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf).strip())
    return parts


def _parse_scalar(text: str) -> Any:
    if text == "" or text == "null" or text == "~":
        return None
    if text in ("true", "True"):
        return True
    if text in ("false", "False"):
        return False
    if text.startswith("{") and text.endswith("}"):
        return _parse_flow_map(text)
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(p.strip()) for p in _split_flow(inner)]
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        return text[1:-1]
    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        return text


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _next_content(lines: list[str], i: int) -> int | None:
    while i < len(lines):
        if lines[i].strip() and not lines[i].lstrip().startswith("#"):
            return i
        i += 1
    return None
