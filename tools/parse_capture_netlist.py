#!/usr/bin/env python3
"""
Local parser for OrCAD Capture PST netlist exports.

Reads three files produced by PSTWRITER 17.2:
    pstchip.dat   FILE_TYPE=LIBRARY_PARTS
    pstxprt.dat   FILE_TYPE=EXPANDEDPARTLIST
    pstxnet.dat   FILE_TYPE=EXPANDEDNETLIST

Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from typing import Any, Optional


SCHEMA_VERSION = "1.0"
TOOL_NAME = "parse_capture_netlist.py"
DEFAULT_ENCODING = "utf-8-sig"

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

SERVICE_PROP_NAMES = frozenset({"PRIM_FILE"})

EXPECTED_FILE_TYPES = {
    "chip": "LIBRARY_PARTS",
    "part": "EXPANDEDPARTLIST",
    "net": "EXPANDEDNETLIST",
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class NetlistError(Exception):
    def __init__(self, message: str, filename: Optional[str] = None,
                 line: Optional[int] = None, col: Optional[int] = None) -> None:
        super().__init__(message)
        self.message = message
        self.filename = filename
        self.line = line
        self.col = col

    def __str__(self) -> str:
        parts = []
        if self.filename:
            parts.append(self.filename)
        if self.line is not None:
            parts.append(str(self.line))
            if self.col is not None:
                parts.append(str(self.col))
        loc = ":".join(parts)
        return f"{loc}: {self.message}" if loc else self.message


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------

class Token:
    __slots__ = ("kind", "value", "line", "col")

    def __init__(self, kind: str, value: str, line: int, col: int) -> None:
        self.kind = kind
        self.value = value
        self.line = line
        self.col = col

    def __repr__(self) -> str:
        return f"Token({self.kind!r}, {self.value!r}, {self.line}:{self.col})"


_WORD_EXTRA = set("_")
_PUNCT = set(";:,=.")


class Lexer:
    def __init__(self, text: str, filename: str) -> None:
        self.text = text
        self.filename = filename
        self.pos = 0
        self.line = 1
        self.col = 1
        self.n = len(text)

    def _advance(self) -> str:
        ch = self.text[self.pos]
        self.pos += 1
        if ch == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return ch

    def _peek(self) -> str:
        if self.pos >= self.n:
            return ""
        return self.text[self.pos]

    def _error(self, message: str, line: Optional[int] = None,
               col: Optional[int] = None) -> None:
        raise NetlistError(
            message, self.filename,
            line if line is not None else self.line,
            col if col is not None else self.col,
        )

    def tokenize(self) -> list[Token]:
        tokens: list[Token] = []
        while self.pos < self.n:
            ch = self._peek()
            if ch in " \t\r\n":
                self._advance()
                continue
            if ch == "{":
                self._skip_brace_comment()
                continue
            if ch == "'":
                tokens.append(self._read_string())
                continue
            if ch in _PUNCT:
                line, col = self.line, self.col
                self._advance()
                tokens.append(Token("punct", ch, line, col))
                continue
            if ch.isalnum() or ch in _WORD_EXTRA:
                tokens.append(self._read_word())
                continue
            self._error(f"unexpected character {ch!r}")

        tokens.append(Token("eof", "", self.line, self.col))
        return tokens

    def _read_word(self) -> Token:
        line, col = self.line, self.col
        start = self.pos
        while self.pos < self.n:
            ch = self.text[self.pos]
            if ch.isalnum() or ch in _WORD_EXTRA:
                self._advance()
            else:
                break
        return Token("word", self.text[start:self.pos], line, col)

    def _read_string(self) -> Token:
        line, col = self.line, self.col
        self._advance()  # opening quote
        start = self.pos
        while self.pos < self.n and self.text[self.pos] != "'":
            self._advance()
        if self.pos >= self.n:
            self._error("unterminated string literal", line, col)
        value = self.text[start:self.pos]
        self._advance()  # closing quote
        # Guard against two indistinguishable situations:
        #   (a) the string actually was not closed and what we took as the
        #       closing quote is the opening quote of the *next* string;
        #   (b) the source uses an escaping syntax for the apostrophe that
        #       this parser does not support.
        # We refuse to guess and emit a single, explicit, actionable error.
        if self.pos < self.n:
            nxt = self.text[self.pos]
            if nxt.isalnum() or nxt in _WORD_EXTRA:
                self._error(
                    f"unexpected word character {nxt!r} immediately after "
                    f"quoted string; the string may be unterminated, or an "
                    f"apostrophe inside it may use an escaping syntax that "
                    f"this parser does not support. Please provide a "
                    f"minimal example of the offending record.",
                    self.line, self.col,
                )
        return Token("string", value, line, col)

    def _skip_brace_comment(self) -> None:
        line, col = self.line, self.col
        depth = 0
        while self.pos < self.n:
            ch = self._peek()
            if ch == "{":
                depth += 1
                self._advance()
            elif ch == "}":
                depth -= 1
                self._advance()
                if depth == 0:
                    return
            else:
                self._advance()
        self._error("unterminated brace comment", line, col)


# ---------------------------------------------------------------------------
# Token stream
# ---------------------------------------------------------------------------

class TokenStream:
    def __init__(self, tokens: list[Token], filename: str) -> None:
        self.tokens = tokens
        self.pos = 0
        self.filename = filename

    def peek(self, offset: int = 0) -> Token:
        idx = self.pos + offset
        if idx >= len(self.tokens):
            return self.tokens[-1]
        return self.tokens[idx]

    def next(self) -> Token:
        tok = self.peek()
        if tok.kind != "eof":
            self.pos += 1
        return tok

    def at_eof(self) -> bool:
        return self.peek().kind == "eof"

    def error(self, message: str, tok: Optional[Token] = None) -> None:
        if tok is None:
            tok = self.peek()
        raise NetlistError(message, self.filename, tok.line, tok.col)

    def expect_punct(self, value: str) -> Token:
        tok = self.next()
        if tok.kind != "punct" or tok.value != value:
            self.error(f"expected {value!r}, got {tok.value!r}", tok)
        return tok

    def expect_word(self, value: Optional[str] = None) -> Token:
        tok = self.next()
        if tok.kind != "word":
            self.error(f"expected word, got {tok.value!r}", tok)
        if value is not None and tok.value != value:
            self.error(f"expected word {value!r}, got {tok.value!r}", tok)
        return tok

    def expect_string(self) -> Token:
        tok = self.next()
        if tok.kind != "string":
            self.error(f"expected string, got {tok.value!r}", tok)
        return tok


# ---------------------------------------------------------------------------
# Shared parsers
# ---------------------------------------------------------------------------

def parse_file_type(ts: TokenStream) -> str:
    name_tok = ts.expect_word()
    if name_tok.value != "FILE_TYPE":
        ts.error(
            f"expected FILE_TYPE declaration, got {name_tok.value!r}", name_tok)
    ts.expect_punct("=")
    val_tok = ts.next()
    if val_tok.kind not in ("word", "string"):
        ts.error(f"expected value for FILE_TYPE, got {val_tok.value!r}", val_tok)
    ts.expect_punct(";")
    return val_tok.value


def parse_prop_statements(ts: TokenStream,
                          terminators: tuple[str, ...] = (";",)) -> list[dict]:
    props: list[dict] = []
    while True:
        tok = ts.peek()
        if tok.kind != "word":
            break
        nxt = ts.peek(1)
        if nxt.kind != "punct" or nxt.value != "=":
            break
        name_tok = ts.next()
        ts.expect_punct("=")
        val_tok = ts.expect_string()
        term = ts.next()
        if term.kind != "punct" or term.value not in terminators:
            ts.error(
                f"expected one of {terminators} after property value, "
                f"got {term.value!r}", term)
        props.append({
            "name": name_tok.value,
            "value": val_tok.value,
            "line": name_tok.line,
            "col": name_tok.col,
        })
    return props


def consume_optional_end(ts: TokenStream) -> bool:
    """Consume an optional top-level 'END .' terminator."""
    tok = ts.peek()
    if tok.kind != "word" or tok.value != "END":
        return False
    ts.next()
    ts.expect_punct(".")
    nxt = ts.peek()
    if nxt.kind == "punct" and nxt.value == ";":
        ts.next()
    return True


# ---------------------------------------------------------------------------
# pstchip.dat
# ---------------------------------------------------------------------------

def parse_pstchip(ts: TokenStream) -> list[dict]:
    primitives: list[dict] = []
    while not ts.at_eof():
        if consume_optional_end(ts):
            break
        tok = ts.peek()
        if tok.kind == "word" and tok.value == "primitive":
            primitives.append(_parse_primitive(ts))
        else:
            ts.error(f"unexpected token {tok.value!r} in pstchip.dat", tok)
    return primitives


def _parse_primitive(ts: TokenStream) -> dict:
    kw = ts.expect_word("primitive")
    name_tok = ts.expect_string()
    ts.expect_punct(";")
    pins: list[dict] = []
    body: list[dict] = []
    saw_pin = False
    saw_body = False
    while True:
        tok = ts.peek()
        if tok.kind == "word" and tok.value == "pin":
            if saw_pin:
                ts.error("duplicate pin section", tok)
            saw_pin = True
            pins = _parse_pin_section(ts)
        elif tok.kind == "word" and tok.value == "body":
            if saw_body:
                ts.error("duplicate body section", tok)
            saw_body = True
            body = _parse_body_section(ts)
        elif tok.kind == "word" and tok.value == "end_primitive":
            ts.next()
            ts.expect_punct(";")
            break
        else:
            ts.error(
                f"unexpected token {tok.value!r} in primitive "
                f"{name_tok.value!r}", tok)
    return {
        "name": name_tok.value,
        "pins": pins,
        "body": body,
        "line": kw.line,
    }


def _parse_pin_section(ts: TokenStream) -> list[dict]:
    ts.expect_word("pin")
    pins: list[dict] = []
    while True:
        tok = ts.peek()
        if tok.kind == "word" and tok.value == "end_pin":
            ts.next()
            ts.expect_punct(";")
            return pins
        if tok.kind == "string":
            name_tok = ts.next()
            ts.expect_punct(":")
            props = parse_prop_statements(ts, terminators=(";",))
            pins.append({
                "name": name_tok.value,
                "properties": props,
                "line": name_tok.line,
            })
        else:
            ts.error(
                f"unexpected token {tok.value!r} in pin section", tok)


def _parse_body_section(ts: TokenStream) -> list[dict]:
    ts.expect_word("body")
    props = parse_prop_statements(ts, terminators=(";",))
    ts.expect_word("end_body")
    ts.expect_punct(";")
    return props


# ---------------------------------------------------------------------------
# pstxprt.dat
# ---------------------------------------------------------------------------

def parse_pstxprt(ts: TokenStream) -> tuple[list[dict], list[dict]]:
    directives: list[dict] = []
    components: list[dict] = []
    tok = ts.peek()
    if tok.kind == "word" and tok.value == "DIRECTIVES":
        ts.next()
        directives = parse_prop_statements(ts, terminators=(";",))
        ts.expect_word("END_DIRECTIVES")
        ts.expect_punct(";")
    while not ts.at_eof():
        if consume_optional_end(ts):
            break
        tok = ts.peek()
        if tok.kind == "word" and tok.value == "PART_NAME":
            components.append(_parse_component(ts))
        else:
            ts.error(f"unexpected token {tok.value!r} in pstxprt.dat", tok)
    return directives, components


def _parse_component(ts: TokenStream) -> dict:
    ts.expect_word("PART_NAME")
    refdes_tok = ts.expect_word()
    prim_tok = ts.expect_string()
    ts.expect_punct(":")
    ts.expect_punct(";")
    sections: list[dict] = []
    while True:
        tok = ts.peek()
        if tok.kind == "word" and tok.value == "SECTION_NUMBER":
            ts.next()
            num_tok = ts.expect_word()
            try:
                sec_num = int(num_tok.value)
            except ValueError:
                ts.error(f"invalid SECTION_NUMBER {num_tok.value!r}", num_tok)
            path_tok = ts.expect_string()
            ts.expect_punct(":")
            props = parse_prop_statements(ts, terminators=(";", ","))
            sections.append({
                "section_number": sec_num,
                "path": path_tok.value,
                "properties": props,
                "line": path_tok.line,
            })
        else:
            break
    return {
        "refdes": refdes_tok.value,
        "primitive_name": prim_tok.value,
        "sections": sections,
        "line": refdes_tok.line,
    }


# ---------------------------------------------------------------------------
# pstxnet.dat
# ---------------------------------------------------------------------------

def parse_pstxnet(ts: TokenStream) -> list[dict]:
    nets: list[dict] = []
    while not ts.at_eof():
        if consume_optional_end(ts):
            break
        tok = ts.peek()
        if tok.kind == "word" and tok.value == "NET_NAME":
            nets.append(_parse_net(ts))
        else:
            ts.error(f"unexpected token {tok.value!r} in pstxnet.dat", tok)
    return nets


def _parse_net(ts: TokenStream) -> dict:
    ts.expect_word("NET_NAME")
    name_tok = ts.expect_string()
    path_tok = ts.expect_string()
    ts.expect_punct(":")
    props = parse_prop_statements(ts, terminators=(";",))
    nodes: list[dict] = []
    while True:
        tok = ts.peek()
        if tok.kind == "word" and tok.value == "NODE_NAME":
            nodes.append(_parse_node(ts))
        else:
            break
    return {
        "name": name_tok.value,
        "path": path_tok.value,
        "properties": props,
        "nodes": nodes,
        "line": name_tok.line,
    }


def _parse_node(ts: TokenStream) -> dict:
    ts.expect_word("NODE_NAME")
    refdes_tok = ts.expect_word()
    pin_tok = ts.expect_word()
    path_tok = ts.expect_string()
    ts.expect_punct(":")
    pin_name_tok = ts.expect_string()
    ts.expect_punct(":")
    props = parse_prop_statements(ts, terminators=(";",))
    return {
        "refdes": refdes_tok.value,
        "pin_number": pin_tok.value,
        "path": path_tok.value,
        "pin_name": pin_name_tok.value,
        "properties": props,
        "line": refdes_tok.line,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_pin_number(raw: str) -> tuple[Optional[list[str]], list[str],
                                        Optional[str]]:
    """Parse PIN_NUMBER forms confirmed by the export.

    Confirmed forms (from real fragments):
        (1)
        (H11,0)
        (0,C3)
        (0,0,W7,0,0,...,0)

    Rules:
      * The value must be wrapped in parentheses.
      * Elements are split on ','; empty elements are an error, not a
        silently dropped token.
      * Elements must consist of word characters.
      * '0' is a section placeholder: it is dropped, keeping the non-zero
        tokens as physical pin identifiers.
      * Grouped forms with several non-zero tokens are returned as-is;
        whether such groups are legitimate for the electrical model is
        decided by the callers.

    Returns (tokens, physical_pins, error_message).
      * tokens is None if the input is not parenthesised (unsupported
        overall shape).
      * error_message is non-None if the input is invalid.
    """
    if raw is None:
        return None, [], None
    s = raw.strip()
    if not (s.startswith("(") and s.endswith(")")):
        return None, [], None
    inner = s[1:-1]
    raw_parts = inner.split(",")
    parts: list[str] = []
    for p in raw_parts:
        p = p.strip()
        if not p:
            return None, [], f"empty element in PIN_NUMBER {raw!r}"
        for ch in p:
            if not (ch.isalnum() or ch == "_"):
                return None, [], (
                    f"invalid character {ch!r} in PIN_NUMBER {raw!r}")
        parts.append(p)
    if not parts:
        return None, [], f"empty PIN_NUMBER {raw!r}"
    physical = [p for p in parts if p != "0"]
    return parts, physical, None


def normalize_properties(props: list[dict]) -> tuple[list[dict], list[dict]]:
    """Fold identical (name, value) pairs preserving first-seen order."""
    order: list[dict] = []
    index: dict[tuple[str, str], int] = {}
    by_name: dict[str, set[str]] = {}
    for p in props:
        name = p["name"]
        value = p["value"]
        key = (name, value)
        if key in index:
            order[index[key]]["occurrences"] += 1
        else:
            index[key] = len(order)
            order.append({"name": name, "value": value, "occurrences": 1})
        by_name.setdefault(name, set()).add(value)
    conflicts = []
    for name in sorted(by_name):
        vals = by_name[name]
        if len(vals) > 1:
            conflicts.append({"name": name, "values": sorted(vals)})
    return order, conflicts


def _warn(diag: dict, code: str, message: str,
          filename: Optional[str] = None,
          line: Optional[int] = None,
          col: Optional[int] = None) -> None:
    entry: dict[str, Any] = {"code": code, "message": message}
    if filename:
        entry["file"] = filename
    if line is not None:
        entry["line"] = line
    if col is not None:
        entry["col"] = col
    diag["warnings"].append(entry)


def _error(diag: dict, code: str, message: str,
           filename: Optional[str] = None,
           line: Optional[int] = None,
           col: Optional[int] = None) -> None:
    entry: dict[str, Any] = {"code": code, "message": message}
    if filename:
        entry["file"] = filename
    if line is not None:
        entry["line"] = line
    if col is not None:
        entry["col"] = col
    diag["errors"].append(entry)


# ---------------------------------------------------------------------------
# Model building
# ---------------------------------------------------------------------------

def _resolve_pin_numbers(pin: dict, prim_name: str, diag: dict,
                         f_chip: str) -> tuple[Optional[str], list[str]]:
    """Resolve PIN_NUMBER entries of one primitive pin.

    Returns (pin_number_raw_first, physical_pins). Reports errors and
    warnings on diag using f_chip as source file.
    """
    values = [pp["value"] for pp in pin["properties"]
              if pp["name"] == "PIN_NUMBER"]
    raw_first = values[0] if values else None

    if not values:
        _warn(diag, "PIN_MISSING_NUMBER",
              f"pin {pin['name']!r} of primitive {prim_name!r} has no "
              f"PIN_NUMBER", f_chip, pin["line"])
        return raw_first, []

    resolved: list[Optional[list[str]]] = []
    for pn in values:
        tokens, phys, err = parse_pin_number(pn)
        if err is not None:
            _error(diag, "PIN_INVALID_NUMBER",
                   f"pin {pin['name']!r} of primitive {prim_name!r}: {err}",
                   f_chip, pin["line"])
            resolved.append(None)
        elif tokens is None:
            _warn(diag, "PIN_UNSUPPORTED_NUMBER",
                  f"pin {pin['name']!r} of primitive {prim_name!r}: "
                  f"unsupported PIN_NUMBER {pn!r}",
                  f_chip, pin["line"])
            resolved.append(None)
        else:
            resolved.append(phys)

    valid = [phys for phys in resolved if phys is not None]
    if not valid:
        return raw_first, []

    distinct = {frozenset(p) for p in valid}
    if len(distinct) > 1:
        _error(diag, "PIN_NUMBER_CONFLICT",
               f"pin {pin['name']!r} of primitive {prim_name!r}: "
               f"conflicting PIN_NUMBER values {values!r}",
               f_chip, pin["line"])
        return raw_first, []

    physical = sorted(valid[0])
    if not physical:
        _warn(diag, "PIN_UNSUPPORTED_NUMBER",
              f"pin {pin['name']!r} of primitive {prim_name!r}: "
              f"PIN_NUMBER {raw_first!r} has no physical pin after "
              f"removing section placeholders",
              f_chip, pin["line"])
    return raw_first, physical


def build_json(chip_primitives: list[dict],
               part_directives: list[dict],
               part_components: list[dict],
               net_nets: list[dict],
               context: dict) -> dict:

    diag: dict[str, list[dict]] = {"warnings": [], "errors": []}
    f_chip = context["chip_filename"]
    f_part = context["part_filename"]
    f_net = context["net_filename"]

    # ---- primitives ------------------------------------------------------
    primitives_out: list[dict] = []
    primitives_by_name: dict[str, dict] = {}
    primitive_first_line: dict[str, int] = {}

    for p in chip_primitives:
        name = p["name"]
        if name in primitives_by_name:
            _error(diag, "DUPLICATE_PRIMITIVE",
                   f"duplicate primitive {name!r}; first defined at line "
                   f"{primitive_first_line[name]}",
                   f_chip, p["line"])
            continue
        primitive_first_line[name] = p["line"]

        pins_norm: list[dict] = []
        for pin in p["pins"]:
            raw_first, physical = _resolve_pin_numbers(
                pin, name, diag, f_chip)
            norm_props, conflicts = normalize_properties(pin["properties"])
            for c in conflicts:
                _warn(diag, "PROPERTY_CONFLICT",
                      f"pin {pin['name']!r} of primitive {name!r}: property "
                      f"{c['name']!r} has multiple values {c['values']!r}",
                      f_chip, pin["line"])
            pins_norm.append({
                "name": pin["name"],
                "pin_number_raw": raw_first,
                "physical_pins": physical,
                "properties": norm_props,
            })

        # Pin order within a primitive does not affect the electrical
        # model, so we sort for determinism. Source order is preserved
        # only inside each pin's properties (normalize_properties keeps
        # first-seen order).
        pins_norm.sort(key=lambda x: (x["name"], x["pin_number_raw"] or ""))

        body_props, body_conflicts = normalize_properties(p["body"])
        for c in body_conflicts:
            _warn(diag, "PROPERTY_CONFLICT",
                  f"primitive {name!r}: property {c['name']!r} has multiple "
                  f"values {c['values']!r}", f_chip, p["line"])

        prim = {
            "name": name,
            "pins": pins_norm,
            "properties": body_props,
        }
        primitives_by_name[name] = prim
        primitives_out.append(prim)

    primitives_out.sort(key=lambda x: x["name"])

    # ---- components ------------------------------------------------------
    components_out: list[dict] = []
    components_by_refdes: dict[str, dict] = {}
    component_first_line: dict[str, int] = {}

    for c in part_components:
        refdes = c["refdes"]
        if refdes in components_by_refdes:
            _error(diag, "DUPLICATE_REFDES",
                   f"duplicate component {refdes!r}; first defined at line "
                   f"{component_first_line[refdes]}",
                   f_part, c["line"])
            continue
        component_first_line[refdes] = c["line"]

        sections_norm: list[dict] = []
        for sec in c["sections"]:
            props, conflicts = normalize_properties(sec["properties"])
            for cf in conflicts:
                _warn(diag, "PROPERTY_CONFLICT",
                      f"section {sec['section_number']} of {refdes!r}: "
                      f"property {cf['name']!r} has multiple values "
                      f"{cf['values']!r}", f_part, sec["line"])
            regular: list[dict] = []
            service: list[dict] = []
            for pr in props:
                if pr["name"] in SERVICE_PROP_NAMES:
                    service.append(pr)
                else:
                    regular.append(pr)
            sections_norm.append({
                "section_number": sec["section_number"],
                "path": sec["path"],
                "properties": regular,
                "service_properties": service,
            })
        sections_norm.sort(key=lambda x: x["section_number"])

        comp = {
            "refdes": refdes,
            "primitive_name": c["primitive_name"],
            "sections": sections_norm,
        }
        if c["primitive_name"] not in primitives_by_name:
            _error(diag, "MISSING_PRIMITIVE",
                   f"component {refdes!r} references unknown primitive "
                   f"{c['primitive_name']!r}", f_part, c["line"])
        components_by_refdes[refdes] = comp
        components_out.append(comp)

    components_out.sort(key=lambda x: x["refdes"])

    # (refdes, section path) -> section_number, used to validate node paths.
    section_path_index: dict[tuple[str, str], int] = {}
    for c in components_out:
        for s in c["sections"]:
            section_path_index[(c["refdes"], s["path"])] = s["section_number"]

    # ---- nets ------------------------------------------------------------
    nets_out: list[dict] = []
    seen_nets: set[tuple[str, str]] = set()
    # (refdes, pin_number) -> (net_path, net_name). Physical identity of a
    # pin, independent of which section path the net record came from.
    pin_owner: dict[tuple[str, str], tuple[str, str]] = {}

    for net in net_nets:
        net_key = (net["path"], net["name"])
        if net_key in seen_nets:
            _error(diag, "DUPLICATE_NET",
                   f"net {net['name']!r} at path {net['path']!r} is defined "
                   f"more than once", f_net, net["line"])
            continue
        seen_nets.add(net_key)

        net_props, net_conflicts = normalize_properties(net["properties"])
        for cf in net_conflicts:
            _warn(diag, "PROPERTY_CONFLICT",
                  f"net {net['name']!r}: property {cf['name']!r} has "
                  f"multiple values {cf['values']!r}", f_net, net["line"])

        nodes_norm: list[dict] = []
        seen_nodes: set[tuple[str, str]] = set()

        for node in net["nodes"]:
            comp = components_by_refdes.get(node["refdes"])
            if comp is None:
                _error(diag, "MISSING_COMPONENT",
                       f"net {net['name']!r} references unknown component "
                       f"{node['refdes']!r}", f_net, node["line"])
                continue

            sec_num = section_path_index.get((node["refdes"], node["path"]))
            if sec_num is None:
                _error(diag, "NODE_PATH_MISMATCH",
                       f"net {net['name']!r}: node {node['refdes']}."
                       f"{node['pin_number']} references path "
                       f"{node['path']!r} which is not among sections of "
                       f"component {node['refdes']!r}",
                       f_net, node["line"])
                continue

            # Pin validation against the referenced primitive.
            prim = primitives_by_name.get(comp["primitive_name"])
            if prim is not None:
                matches = [p for p in prim["pins"]
                           if node["pin_number"] in p["physical_pins"]]
                if not matches:
                    _error(diag, "UNKNOWN_PIN",
                           f"net {net['name']!r}: pin {node['refdes']}."
                           f"{node['pin_number']} not found in primitive "
                           f"{comp['primitive_name']!r}",
                           f_net, node["line"])
                elif len(matches) > 1:
                    _error(diag, "AMBIGUOUS_PIN",
                           f"net {net['name']!r}: pin {node['refdes']}."
                           f"{node['pin_number']} matches multiple pins in "
                           f"primitive {comp['primitive_name']!r}; "
                           f"disambiguation rules are not confirmed",
                           f_net, node["line"])

            node_key = (node["refdes"], node["pin_number"])
            if node_key in seen_nodes:
                _warn(diag, "DUPLICATE_NODE_IN_NET",
                      f"net {net['name']!r}: duplicate node "
                      f"{node['refdes']}.{node['pin_number']}",
                      f_net, node["line"])
                continue
            seen_nodes.add(node_key)

            if node_key in pin_owner:
                other = pin_owner[node_key]
                if other != net_key:
                    _error(diag, "PIN_IN_MULTIPLE_NETS",
                           f"pin {node['refdes']}.{node['pin_number']} "
                           f"appears in multiple nets: {other[1]!r} and "
                           f"{net['name']!r}", f_net, node["line"])
            else:
                pin_owner[node_key] = net_key

            node_props, node_conflicts = normalize_properties(
                node["properties"])
            for cf in node_conflicts:
                _warn(diag, "PROPERTY_CONFLICT",
                      f"net {net['name']!r}, node {node['refdes']}."
                      f"{node['pin_number']}: property {cf['name']!r} has "
                      f"multiple values {cf['values']!r}",
                      f_net, node["line"])
            nodes_norm.append({
                "refdes": node["refdes"],
                "pin_number": node["pin_number"],
                "pin_name": node["pin_name"],
                "path": node["path"],
                "properties": node_props,
            })

        nodes_norm.sort(
            key=lambda x: (x["refdes"], x["pin_number"], x["path"]))

        nets_out.append({
            "name": net["name"],
            "path": net["path"],
            "properties": net_props,
            "nodes": nodes_norm,
        })

    nets_out.sort(key=lambda x: (x["path"], x["name"]))

    # ---- directives / meta ----------------------------------------------
    directives_simple: dict[str, str] = {}
    for d in part_directives:
        if d["name"] not in directives_simple:
            directives_simple[d["name"]] = d["value"]

    sections_count = sum(len(c["sections"]) for c in components_out)
    nodes_count = sum(len(n["nodes"]) for n in nets_out)

    stats = {
        "primitives": len(primitives_out),
        "components": len(components_out),
        "sections": sections_count,
        "nets": len(nets_out),
        "nodes": nodes_count,
        "warnings": len(diag["warnings"]),
        "errors": len(diag["errors"]),
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "meta": {
            "tool": TOOL_NAME,
            "encoding": context["encoding"],
            "files": {
                "chip": f_chip,
                "part": f_part,
                "net": f_net,
            },
            "file_types": {
                "chip": context["chip_file_type"],
                "part": context["part_file_type"],
                "net": context["net_file_type"],
            },
            "directives": directives_simple,
        },
        "primitives": primitives_out,
        "components": components_out,
        "nets": nets_out,
        "diagnostics": diag,
        "stats": stats,
    }


# ---------------------------------------------------------------------------
# Top-level run
# ---------------------------------------------------------------------------

def _read_text(path: str, encoding: str) -> str:
    with open(path, "rb") as f:
        raw = f.read()
    return raw.decode(encoding)


def _parse_file(text: str, filename: str, parser,
                expected_file_type: str):
    lexer = Lexer(text, filename)
    tokens = lexer.tokenize()
    ts = TokenStream(tokens, filename)
    ft = parse_file_type(ts)
    if ft != expected_file_type:
        raise NetlistError(
            f"unexpected FILE_TYPE {ft!r}, expected {expected_file_type!r}",
            filename, 1, 1)
    payload = parser(ts)
    if not ts.at_eof():
        tok = ts.peek()
        raise NetlistError(
            f"unexpected trailing token {tok.value!r} after end of file",
            filename, tok.line, tok.col)
    return ft, payload


def run(chip_path: str, part_path: str, net_path: str,
        encoding: str) -> dict:
    chip_text = _read_text(chip_path, encoding)
    part_text = _read_text(part_path, encoding)
    net_text = _read_text(net_path, encoding)

    chip_ft, chip_prims = _parse_file(
        chip_text, os.path.basename(chip_path),
        parse_pstchip, EXPECTED_FILE_TYPES["chip"])
    part_ft, part_payload = _parse_file(
        part_text, os.path.basename(part_path),
        parse_pstxprt, EXPECTED_FILE_TYPES["part"])
    net_ft, net_payload = _parse_file(
        net_text, os.path.basename(net_path),
        parse_pstxnet, EXPECTED_FILE_TYPES["net"])

    part_directives, part_components = part_payload

    context = {
        "encoding": encoding,
        "chip_filename": os.path.basename(chip_path),
        "part_filename": os.path.basename(part_path),
        "net_filename": os.path.basename(net_path),
        "chip_file_type": chip_ft,
        "part_file_type": part_ft,
        "net_file_type": net_ft,
    }
    return build_json(chip_prims, part_directives, part_components,
                      net_payload, context)


def _atomic_write(path: str, text: str) -> None:
    dirname = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(
        prefix=".parse_capture_", suffix=".tmp", dir=dirname)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Parse OrCAD Capture PST netlist export into normalized JSON.")
    p.add_argument("--input-dir", default=_SCRIPT_DIR,
                   help="Directory containing pstchip.dat, pstxprt.dat, "
                        "pstxnet.dat. Defaults to the directory of this "
                        f"script: {_SCRIPT_DIR}")
    p.add_argument("--output", default=None,
                   help="Output JSON file. If omitted, JSON is written to stdout.")
    p.add_argument("--encoding", default=DEFAULT_ENCODING,
                   help=f"Input file encoding (default: {DEFAULT_ENCODING})")
    p.add_argument("--validate-only", action="store_true",
                   help="Do not emit JSON; only validate and print diagnostics.")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    input_dir = args.input_dir
    chip_path = os.path.join(input_dir, "pstchip.dat")
    part_path = os.path.join(input_dir, "pstxprt.dat")
    net_path = os.path.join(input_dir, "pstxnet.dat")

    for p in (chip_path, part_path, net_path):
        if not os.path.isfile(p):
            print(f"error: file not found: {p}", file=sys.stderr)
            return 2

    try:
        result = run(chip_path, part_path, net_path, args.encoding)
    except NetlistError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except UnicodeDecodeError as e:
        print(f"error: decoding failed with encoding {args.encoding!r}: {e}",
              file=sys.stderr)
        return 1
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    for w in result["diagnostics"]["warnings"]:
        loc = _loc_str(w)
        print(f"warning [{w['code']}]{loc}: {w['message']}", file=sys.stderr)
    for e in result["diagnostics"]["errors"]:
        loc = _loc_str(e)
        print(f"error [{e['code']}]{loc}: {e['message']}", file=sys.stderr)

    stats = result["stats"]
    print(
        f"stats: primitives={stats['primitives']} "
        f"components={stats['components']} sections={stats['sections']} "
        f"nets={stats['nets']} nodes={stats['nodes']} "
        f"warnings={stats['warnings']} errors={stats['errors']}",
        file=sys.stderr)

    if result["diagnostics"]["errors"]:
        print("validation failed", file=sys.stderr)
        return 1

    if args.validate_only:
        return 0

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        try:
            _atomic_write(args.output, text + "\n")
        except OSError as e:
            print(f"error: cannot write output: {e}", file=sys.stderr)
            return 2
    else:
        sys.stdout.write(text)
        sys.stdout.write("\n")
    return 0


def _loc_str(entry: dict) -> str:
    parts = []
    if "file" in entry:
        parts.append(entry["file"])
    if "line" in entry:
        parts.append(str(entry["line"]))
        if "col" in entry:
            parts.append(str(entry["col"]))
    return " " + ":".join(parts) if parts else ""


if __name__ == "__main__":
    sys.exit(main())
