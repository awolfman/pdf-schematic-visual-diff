#!/usr/bin/env python3
"""
Compare two normalized netlist JSON files produced by
parse_capture_netlist.py.

Exit codes
----------
0  proven electrically identical, no uncertainty
1  differences, or uncertainty preventing a definite answer
2  input / validation / I/O error
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from typing import Any, Iterable, Optional


SCHEMA_VERSION = "1.5"
TOOL_NAME = "compare_netlists.py"
PARSER_SCHEMA_VERSION = "1.0"

EXIT_OK = 0
EXIT_DIFFERENCES = 1
EXIT_ERROR = 2

# Service reference properties of a component section.
SERVICE_REFERENCE_PROP_NAMES = frozenset(
    {"C_PATH", "P_PATH", "PRIM_FILE", "SECTION"}
)
# Service reference properties of a net (references to the net itself).
NET_REFERENCE_PROP_NAMES = frozenset({"C_SIGNAL"})
# Service references relevant to physical placement.
PLACEMENT_PROP_NAMES = frozenset({"C_PATH", "P_PATH"})

REQUIRED_SECTIONS = (
    "schema_version", "primitives", "components", "nets", "diagnostics",
)

_SECTION_PATH_RE = re.compile(
    r"^(?P<context>[^:]*):INS(?P<ins>[^@]*)@(?P<ref>.*)$")
_PPATH_PAGE_RE = re.compile(r"\\page(?P<page>\d+)_ins", re.IGNORECASE)


# ---------------------------------------------------------------------------
# JSON loading with duplicate-field detection
# ---------------------------------------------------------------------------

def _no_dup_object_pairs(pairs):
    seen = set()
    for k, _v in pairs:
        if k in seen:
            raise ValueError(f"duplicate JSON key {k!r}")
        seen.add(k)
    return dict(pairs)


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f, object_pairs_hook=_no_dup_object_pairs)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _v_str(v) -> bool:
    return isinstance(v, str)


def _v_int_ge1(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 1


def _validate_property_list(props, label, issues, ctx):
    if not isinstance(props, list):
        issues.append(f"{label}: {ctx} must be a list")
        return False
    ok = True
    for j, pr in enumerate(props):
        if not isinstance(pr, dict):
            issues.append(f"{label}: {ctx}[{j}] is not an object")
            ok = False
            continue
        if not _v_str(pr.get("name")):
            issues.append(f"{label}: {ctx}[{j}].name must be a string")
            ok = False
        if not _v_str(pr.get("value")):
            issues.append(f"{label}: {ctx}[{j}].value must be a string")
            ok = False
        if not _v_int_ge1(pr.get("occurrences")):
            issues.append(
                f"{label}: {ctx}[{j}].occurrences must be int >= 1")
            ok = False
    return ok


# ---------------------------------------------------------------------------
# Validation (two-phase: structure, then references)
# ---------------------------------------------------------------------------

def validate_model(model: Any, label: str) -> list[str]:
    issues: list[str] = []

    if not isinstance(model, dict):
        return [f"{label}: top-level JSON value is not an object"]

    for key in REQUIRED_SECTIONS:
        if key not in model:
            issues.append(f"{label}: missing required section {key!r}")
    if issues:
        return issues

    sv = model.get("schema_version")
    if sv != PARSER_SCHEMA_VERSION:
        issues.append(
            f"{label}: schema_version {sv!r} not supported by this tool "
            f"(expected {PARSER_SCHEMA_VERSION!r})")

    diag = model.get("diagnostics")
    if not isinstance(diag, dict):
        issues.append(f"{label}: diagnostics must be an object")
    else:
        errs = diag.get("errors")
        if not isinstance(errs, list):
            issues.append(f"{label}: diagnostics.errors must be a list")
        elif errs:
            issues.append(
                f"{label}: input reports {len(errs)} error(s) in "
                f"diagnostics; refusing to compare")

    prims_raw = model.get("primitives")
    if not isinstance(prims_raw, list):
        issues.append(f"{label}: primitives must be a list")
        return issues
    comps_raw = model.get("components")
    if not isinstance(comps_raw, list):
        issues.append(f"{label}: components must be a list")
        return issues
    nets_raw = model.get("nets")
    if not isinstance(nets_raw, list):
        issues.append(f"{label}: nets must be a list")
        return issues

    # --- phase 1: structural validation; build valid indexes
    valid_prim_pins: dict[str, set[str]] = {}
    prim_seen: set[str] = set()

    for i, p in enumerate(prims_raw):
        if not isinstance(p, dict):
            issues.append(f"{label}: primitives[{i}] is not an object")
            continue
        nm = p.get("name")
        if not _v_str(nm) or not nm:
            issues.append(f"{label}: primitives[{i}].name missing")
            continue
        if nm in prim_seen:
            issues.append(f"{label}: duplicate primitive {nm!r}")
            continue
        prim_seen.add(nm)

        ok = _validate_property_list(
            p.get("properties"), label, issues,
            f"primitive {nm!r}.properties")

        pins = p.get("pins")
        physical: set[str] = set()
        if not isinstance(pins, list):
            issues.append(
                f"{label}: primitive {nm!r}.pins must be a list")
            ok = False
        else:
            seen_pin: set[str] = set()
            for j, pin in enumerate(pins):
                if not isinstance(pin, dict):
                    issues.append(
                        f"{label}: primitive {nm!r}.pins[{j}] "
                        f"is not an object")
                    ok = False
                    continue
                pname = pin.get("name")
                if not _v_str(pname) or not pname:
                    issues.append(
                        f"{label}: primitive {nm!r}.pins[{j}].name missing")
                    ok = False
                    continue
                if pname in seen_pin:
                    issues.append(
                        f"{label}: primitive {nm!r}: duplicate pin "
                        f"{pname!r}")
                    ok = False
                else:
                    seen_pin.add(pname)
                phys = pin.get("physical_pins")
                if not isinstance(phys, list):
                    issues.append(
                        f"{label}: primitive {nm!r}.pin {pname!r}: "
                        f"physical_pins must be a list")
                    ok = False
                else:
                    for x in phys:
                        if _v_str(x):
                            physical.add(x)
                        else:
                            issues.append(
                                f"{label}: primitive {nm!r}.pin "
                                f"{pname!r}: physical_pins elements "
                                f"must be strings")
                            ok = False
                _validate_property_list(
                    pin.get("properties"), label, issues,
                    f"primitive {nm!r}.pin {pname!r}.properties")
        if ok:
            valid_prim_pins[nm] = physical
        else:
            # Still register name so later refs know it exists, but no
            # physical pin table.
            valid_prim_pins[nm] = physical

    # Components: index only structurally valid ones.
    comp_by_refdes: dict[str, dict] = {}
    comp_seen: set[str] = set()

    for i, c in enumerate(comps_raw):
        if not isinstance(c, dict):
            issues.append(f"{label}: components[{i}] is not an object")
            continue
        refdes = c.get("refdes")
        if not _v_str(refdes) or not refdes:
            issues.append(f"{label}: components[{i}].refdes missing")
            continue
        if refdes in comp_seen:
            issues.append(f"{label}: duplicate component {refdes!r}")
            continue
        comp_seen.add(refdes)

        prim_name = c.get("primitive_name")
        prim_name_ok = _v_str(prim_name)
        if not prim_name_ok:
            issues.append(
                f"{label}: component {refdes!r}.primitive_name must be "
                f"a string")
        elif prim_name not in prim_seen:
            issues.append(
                f"{label}: component {refdes!r} references missing "
                f"primitive {prim_name!r}")

        secs = c.get("sections")
        sections_ok = isinstance(secs, list)
        if not sections_ok:
            issues.append(
                f"{label}: component {refdes!r}.sections must be a list")

        if sections_ok:
            seen_sec: set[int] = set()
            for j, s in enumerate(secs):
                if not isinstance(s, dict):
                    issues.append(
                        f"{label}: component {refdes!r}.sections[{j}] "
                        f"is not an object")
                    sections_ok = False
                    continue
                sn = s.get("section_number")
                if not isinstance(sn, int) or isinstance(sn, bool):
                    issues.append(
                        f"{label}: component {refdes!r}.sections[{j}]."
                        f"section_number must be an integer")
                    sections_ok = False
                    continue
                if sn in seen_sec:
                    issues.append(
                        f"{label}: component {refdes!r}: duplicate "
                        f"section number {sn}")
                    sections_ok = False
                else:
                    seen_sec.add(sn)
                if not _v_str(s.get("path")):
                    issues.append(
                        f"{label}: component {refdes!r}.sections[{j}]."
                        f"path must be a string")
                    sections_ok = False
                if not _validate_property_list(
                        s.get("properties"), label, issues,
                        f"component {refdes!r}.section[{sn}].properties"):
                    sections_ok = False
                if not _validate_property_list(
                        s.get("service_properties"), label, issues,
                        f"component {refdes!r}.section[{sn}]."
                        f"service_properties"):
                    sections_ok = False

        if prim_name_ok and sections_ok:
            comp_by_refdes[refdes] = c

    # --- phase 2: reference validation for nets
    net_keys: set[tuple[str, str]] = set()
    pin_owner: dict[tuple[str, str], tuple[str, str]] = {}

    for i, n in enumerate(nets_raw):
        if not isinstance(n, dict):
            issues.append(f"{label}: nets[{i}] is not an object")
            continue
        name = n.get("name")
        path = n.get("path")
        if not _v_str(name) or not _v_str(path):
            issues.append(
                f"{label}: nets[{i}].name/.path must be strings")
            continue
        key = (path, name)
        if key in net_keys:
            issues.append(f"{label}: duplicate net {key!r}")
            continue
        net_keys.add(key)

        _validate_property_list(
            n.get("properties"), label, issues, f"net {name!r}.properties")

        nodes = n.get("nodes")
        if not isinstance(nodes, list):
            issues.append(f"{label}: net {name!r}.nodes must be a list")
            continue
        local_seen: set[tuple[str, str]] = set()
        for j, nd in enumerate(nodes):
            if not isinstance(nd, dict):
                issues.append(
                    f"{label}: net {name!r}.nodes[{j}] is not an object")
                continue
            r = nd.get("refdes")
            p = nd.get("pin_number")
            if not _v_str(r) or not _v_str(p):
                issues.append(
                    f"{label}: net {name!r}.nodes[{j}].refdes/"
                    f"pin_number must be strings")
                continue
            if "path" in nd and not _v_str(nd.get("path")):
                issues.append(
                    f"{label}: net {name!r}.nodes[{j}].path must be a "
                    f"string when present")
            if "pin_name" in nd and not _v_str(nd.get("pin_name")):
                issues.append(
                    f"{label}: net {name!r}.nodes[{j}].pin_name must be "
                    f"a string when present")
            _validate_property_list(
                nd.get("properties"), label, issues,
                f"net {name!r}.node[{r}.{p}].properties")

            if r not in comp_by_refdes:
                issues.append(
                    f"{label}: net {name!r} references unknown component "
                    f"{r!r}")
                continue
            comp = comp_by_refdes[r]
            prim_name = comp.get("primitive_name")
            if _v_str(prim_name) and prim_name in valid_prim_pins:
                valid = valid_prim_pins[prim_name]
                if p not in valid:
                    issues.append(
                        f"{label}: net {name!r}: pin {r}.{p} is not a "
                        f"valid physical pin of primitive {prim_name!r}")

            node_path = nd.get("path")
            if _v_str(node_path):
                section_paths = {
                    s.get("path") for s in comp.get("sections") or []
                    if _v_str(s.get("path"))
                }
                if node_path not in section_paths:
                    issues.append(
                        f"{label}: net {name!r}: node {r}.{p} path "
                        f"{node_path!r} not among the component's "
                        f"section paths")

            k = (r, p)
            if k in local_seen:
                issues.append(
                    f"{label}: net {name!r}: pin {r}.{p} appears twice "
                    f"within the same net")
            else:
                local_seen.add(k)
            if k in pin_owner and pin_owner[k] != key:
                issues.append(
                    f"{label}: pin {r}.{p} appears in two different "
                    f"nets: {pin_owner[k]!r} and {key!r}")
            else:
                pin_owner[k] = key

    return issues


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _prop_full_signature(props: Iterable[dict]) -> list[tuple[str, str, int]]:
    return sorted(
        (str(p.get("name", "")), str(p.get("value", "")),
         int(p.get("occurrences", 1)))
        for p in props or []
    )


def _prop_electrical_signature(props: Iterable[dict]) -> list[tuple[str, str]]:
    return sorted(
        set((str(p.get("name", "")), str(p.get("value", "")))
            for p in props or [])
    )


def _multiset_diff(old: list, new: list) -> tuple[list, list]:
    so, sn = sorted(old), sorted(new)
    only_old: list = []
    only_new: list = list(sn)
    for e in so:
        if e in only_new:
            only_new.remove(e)
        else:
            only_old.append(e)
    return only_old, only_new


def _split_section_props(props: Iterable[dict]) -> tuple[list, list]:
    elec: list = []
    svc: list = []
    for p in props or []:
        if str(p.get("name", "")) in SERVICE_REFERENCE_PROP_NAMES:
            svc.append(p)
        else:
            elec.append(p)
    return elec, svc


def _split_net_props(props: Iterable[dict]) -> tuple[list, list]:
    elec: list = []
    ref: list = []
    for p in props or []:
        if str(p.get("name", "")) in NET_REFERENCE_PROP_NAMES:
            ref.append(p)
        else:
            elec.append(p)
    return elec, ref


def _node_elec_key(node: dict) -> tuple[str, str]:
    return (str(node.get("refdes", "")), str(node.get("pin_number", "")))


def _mapped_node_key(node: dict, comp_map: dict) -> tuple[str, str]:
    r = str(node.get("refdes", ""))
    return (str(comp_map.get(r, r)), str(node.get("pin_number", "")))


def _node_summary(node: dict) -> dict:
    return {
        "refdes": node.get("refdes", ""),
        "pin_number": node.get("pin_number", ""),
        "pin_name": node.get("pin_name", ""),
        "path": node.get("path", ""),
    }


def _sort_nodes(nodes: list[dict]) -> list[dict]:
    return sorted(
        nodes,
        key=lambda nd: (str(nd.get("refdes", "")),
                        str(nd.get("pin_number", "")),
                        str(nd.get("path", ""))),
    )


def _section_paths(comp: dict) -> set[str]:
    return {str(s.get("path", "")) for s in comp.get("sections") or []
            if s.get("path")}


def _prim_pin_names(prim: Optional[dict]) -> Optional[frozenset]:
    if prim is None:
        return None
    return frozenset(str(p.get("name", "")) for p in prim.get("pins") or [])


def _mapped_node_frozenset(net: dict, comp_map: dict) -> frozenset:
    return frozenset(_mapped_node_key(nd, comp_map)
                     for nd in net.get("nodes") or [])


def _frozenset_sort_key(s: frozenset) -> tuple:
    return tuple(sorted(s))


def _candidate_key(c: dict) -> tuple[str, str]:
    return (str(c.get("path", "")), str(c.get("name", "")))


def _make_candidates(tuples: list[tuple[str, str]]) -> list[dict]:
    """Convert list of (path, name) into sorted list of dicts."""
    return [
        {"name": name, "path": path}
        for (path, name) in sorted(tuples)
    ]


def _parse_section_path(path: str) -> Optional[dict]:
    m = _SECTION_PATH_RE.match(path or "")
    if not m:
        return None
    return {
        "context": m.group("context"),
        "ins": m.group("ins"),
        "ref": m.group("ref"),
    }


def _ppath_page(value: str) -> Optional[str]:
    m = _PPATH_PAGE_RE.search(value or "")
    if m:
        return m.group("page")
    return None


def _classify_path_change(old_path: str, new_path: str) -> dict:
    o = _parse_section_path(old_path)
    n = _parse_section_path(new_path)
    if o is None or n is None:
        return {
            "context_changed": None,
            "ins_changed": None,
            "library_ref_changed": None,
        }
    return {
        "context_changed": o["context"] != n["context"],
        "ins_changed": o["ins"] != n["ins"],
        "library_ref_changed": o["ref"] != n["ref"],
    }


# ---------------------------------------------------------------------------
# Primitive definition comparison
# ---------------------------------------------------------------------------

def _compare_primitive_definitions(old_prim: dict,
                                   new_prim: dict) -> tuple[list, list]:
    elec: list[dict] = []
    tech: list[dict] = []

    old_elec = _prop_electrical_signature(old_prim.get("properties") or [])
    new_elec = _prop_electrical_signature(new_prim.get("properties") or [])
    if old_elec != new_elec:
        oo, on = _multiset_diff(old_elec, new_elec)
        elec.append({
            "field": "body.properties",
            "only_in_old": oo,
            "only_in_new": on,
        })

    old_full = _prop_full_signature(old_prim.get("properties") or [])
    new_full = _prop_full_signature(new_prim.get("properties") or [])
    if old_full != new_full and old_elec == new_elec:
        oo, on = _multiset_diff(old_full, new_full)
        tech.append({
            "field": "body.properties.occurrences",
            "only_in_old": oo,
            "only_in_new": on,
        })

    old_pins = {p["name"]: p for p in old_prim.get("pins") or []}
    new_pins = {p["name"]: p for p in new_prim.get("pins") or []}

    pins_added = sorted(set(new_pins) - set(old_pins))
    pins_removed = sorted(set(old_pins) - set(new_pins))

    elec_pin_changes: list[dict] = []
    tech_pin_changes: list[dict] = []

    for pn in sorted(set(old_pins) & set(new_pins)):
        op, npp = old_pins[pn], new_pins[pn]
        op_elec = _prop_electrical_signature(op.get("properties") or [])
        np_elec = _prop_electrical_signature(npp.get("properties") or [])
        op_full = _prop_full_signature(op.get("properties") or [])
        np_full = _prop_full_signature(npp.get("properties") or [])
        ophys = sorted(str(x) for x in op.get("physical_pins") or [])
        nphys = sorted(str(x) for x in npp.get("physical_pins") or [])

        entry_elec: dict = {}
        if op_elec != np_elec:
            oo, on = _multiset_diff(op_elec, np_elec)
            entry_elec["properties_only_in_old"] = oo
            entry_elec["properties_only_in_new"] = on
        if ophys != nphys:
            entry_elec["physical_pins_old"] = ophys
            entry_elec["physical_pins_new"] = nphys
        if entry_elec:
            entry_elec["pin"] = pn
            elec_pin_changes.append(entry_elec)

        if op_full != np_full and op_elec == np_elec:
            oo, on = _multiset_diff(op_full, np_full)
            tech_pin_changes.append({
                "pin": pn,
                "occurrences_only_in_old": oo,
                "occurrences_only_in_new": on,
            })

    if pins_added or pins_removed or elec_pin_changes:
        elec.append({
            "field": "pins",
            "added": pins_added,
            "removed": pins_removed,
            "changed": elec_pin_changes,
        })
    if tech_pin_changes:
        tech.append({
            "field": "pins.occurrences",
            "changed": tech_pin_changes,
        })

    return elec, tech


# ---------------------------------------------------------------------------
# Primitives section (informational)
# ---------------------------------------------------------------------------

def _primitive_signature(prim: dict) -> tuple:
    props = tuple(_prop_full_signature(prim.get("properties") or []))
    pins = tuple(sorted(
        (str(p.get("name", "")),
         tuple(sorted(str(x) for x in p.get("physical_pins") or [])),
         tuple(_prop_full_signature(p.get("properties") or [])))
        for p in prim.get("pins") or []
    ))
    return (props, pins)


def compare_primitives(old: dict, new: dict) -> dict:
    old_by = {p["name"]: p for p in old.get("primitives") or []}
    new_by = {p["name"]: p for p in new.get("primitives") or []}

    added = sorted(set(new_by) - set(old_by))
    removed = sorted(set(old_by) - set(new_by))

    changed: list[dict] = []
    for name in sorted(set(old_by) & set(new_by)):
        o, n = old_by[name], new_by[name]
        elec, tech = _compare_primitive_definitions(o, n)
        if elec or tech:
            changed.append({
                "name": name,
                "electrical_changes": elec,
                "technical_changes": tech,
            })

    def _group(names, table):
        out: dict = {}
        for nm in names:
            out.setdefault(_primitive_signature(table[nm]), []).append(nm)
        return out

    rem_by_sig = _group(removed, old_by)
    add_by_sig = _group(added, new_by)
    possible_renames: list[dict] = []
    ambiguous: list[dict] = []
    for sig, olds in rem_by_sig.items():
        news = add_by_sig.get(sig)
        if not news:
            continue
        if len(olds) == 1 and len(news) == 1:
            possible_renames.append({
                "old": olds[0],
                "new": news[0],
                "note": "unique content match (same properties and pins)",
            })
        else:
            ambiguous.append({
                "old_candidates": sorted(olds),
                "new_candidates": sorted(news),
                "note": "multiple primitives share the same content",
            })

    possible_renames.sort(key=lambda e: (e["old"], e["new"]))
    ambiguous.sort(key=lambda e: (
        tuple(e["old_candidates"]), tuple(e["new_candidates"])))

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "possible_renames": possible_renames,
        "ambiguous": ambiguous,
    }


# ---------------------------------------------------------------------------
# Component matching
# ---------------------------------------------------------------------------

def build_component_matches(old: dict, new: dict) -> dict:
    old_prims = {p["name"]: p for p in old.get("primitives") or []}
    new_prims = {p["name"]: p for p in new.get("primitives") or []}
    old_by = {c["refdes"]: c for c in old.get("components") or []}
    new_by = {c["refdes"]: c for c in new.get("components") or []}

    direct: list[dict] = []
    matched_old: set[str] = set()
    matched_new: set[str] = set()
    for refdes in sorted(set(old_by) & set(new_by)):
        direct.append({"old": refdes, "new": refdes})
        matched_old.add(refdes)
        matched_new.add(refdes)

    old_left = {r: old_by[r] for r in old_by if r not in matched_old}
    new_left = {r: new_by[r] for r in new_by if r not in matched_new}

    candidates: dict = {}
    for r_ref in sorted(old_left):
        r_comp = old_left[r_ref]
        r_paths = _section_paths(r_comp)
        if not r_paths:
            continue
        r_pins = _prim_pin_names(old_prims.get(r_comp.get("primitive_name")))
        for a_ref in sorted(new_left):
            a_comp = new_left[a_ref]
            a_paths = _section_paths(a_comp)
            if not a_paths or r_paths != a_paths:
                continue
            a_pins = _prim_pin_names(
                new_prims.get(a_comp.get("primitive_name")))
            if r_pins is None or a_pins is None or r_pins != a_pins:
                continue
            candidates[(r_ref, a_ref)] = tuple(sorted(r_paths))

    old_to_new: dict = {}
    new_to_old: dict = {}
    for (r, a) in candidates:
        old_to_new.setdefault(r, set()).add(a)
        new_to_old.setdefault(a, set()).add(r)

    confirmed: list[dict] = []
    confirmed_old: set[str] = set()
    confirmed_new: set[str] = set()
    for (r, a) in sorted(candidates):
        if len(old_to_new[r]) == 1 and len(new_to_old[a]) == 1:
            confirmed.append({
                "old": r,
                "new": a,
                "shared_paths": list(candidates[(r, a)]),
            })
            confirmed_old.add(r)
            confirmed_new.add(a)

    ambiguous: list[dict] = []
    seen_keys: set = set()
    for (r, a) in sorted(candidates):
        if r in confirmed_old and a in confirmed_new:
            continue
        olds = sorted(new_to_old[a])
        news: set[str] = set()
        for r2 in olds:
            news |= old_to_new[r2]
        key = (tuple(olds), tuple(sorted(news)))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        ambiguous.append({
            "old_candidates": olds,
            "new_candidates": sorted(news),
            "note": "full section path set and pin names shared with "
                    "multiple components; pairing not unique",
        })

    partial_seen: set = set()
    for r_ref in sorted(old_left):
        r_comp = old_left[r_ref]
        r_paths = _section_paths(r_comp)
        if not r_paths:
            continue
        r_pins = _prim_pin_names(old_prims.get(r_comp.get("primitive_name")))
        for a_ref in sorted(new_left):
            a_comp = new_left[a_ref]
            a_paths = _section_paths(a_comp)
            if not a_paths or not (r_paths & a_paths):
                continue
            if (r_ref, a_ref) in candidates:
                continue
            a_pins = _prim_pin_names(
                new_prims.get(a_comp.get("primitive_name")))
            if r_pins is None or a_pins is None or r_pins != a_pins:
                continue
            if (r_ref, a_ref) in partial_seen:
                continue
            partial_seen.add((r_ref, a_ref))
            ambiguous.append({
                "old_candidates": [r_ref],
                "new_candidates": [a_ref],
                "note": "partial section path overlap; not enough to "
                        "confirm a rename",
            })

    ambiguous.sort(key=lambda e: (
        tuple(e["old_candidates"]), tuple(e["new_candidates"])))

    comp_map: dict = {}
    for m in direct:
        comp_map[m["old"]] = m["new"]
    for m in confirmed:
        comp_map[m["old"]] = m["new"]

    unmatched_old = sorted(r for r in old_by if r not in comp_map)
    unmatched_new = sorted(r for r in new_by if r not in comp_map.values())

    return {
        "direct": direct,
        "confirmed_renames": confirmed,
        "ambiguous": ambiguous,
        "map": comp_map,
        "unmatched_old": unmatched_old,
        "unmatched_new": unmatched_new,
    }


# ---------------------------------------------------------------------------
# Components comparison
# ---------------------------------------------------------------------------

def compare_components(old: dict, new: dict, matches: dict) -> dict:
    old_prims = {p["name"]: p for p in old.get("primitives") or []}
    new_prims = {p["name"]: p for p in new.get("primitives") or []}
    old_by = {c["refdes"]: c for c in old.get("components") or []}
    new_by = {c["refdes"]: c for c in new.get("components") or []}

    added = list(matches["unmatched_new"])
    removed = list(matches["unmatched_old"])

    changed: list[dict] = []
    renamed: list[dict] = []
    placement: list[dict] = []

    pairs: list[tuple[str, str, Optional[dict]]] = []
    for m in matches["direct"]:
        pairs.append((m["old"], m["new"], None))
    for m in matches["confirmed_renames"]:
        pairs.append((m["old"], m["new"], m))

    for old_ref, new_ref, rename_info in sorted(pairs):
        o, n = old_by[old_ref], new_by[new_ref]
        elec: list[dict] = []
        tech: list[dict] = []
        svc: list[dict] = []

        old_pn = o.get("primitive_name")
        new_pn = n.get("primitive_name")
        old_prim = old_prims.get(old_pn)
        new_prim = new_prims.get(new_pn)

        if old_pn != new_pn:
            svc.append({
                "field": "primitive_name_link",
                "old": old_pn,
                "new": new_pn,
            })
        if old_prim is not None and new_prim is not None:
            def_elec, def_tech = _compare_primitive_definitions(
                old_prim, new_prim)
            if old_pn != new_pn and not def_elec and not def_tech:
                svc[-1]["note"] = (
                    "effective definition is identical; only the link "
                    "changed")
            if def_elec:
                elec.append({
                    "field": "primitive_definition",
                    "old_primitive": old_pn,
                    "new_primitive": new_pn,
                    "changes": def_elec,
                })
            if def_tech:
                tech.append({
                    "field": "primitive_definition",
                    "old_primitive": old_pn,
                    "new_primitive": new_pn,
                    "changes": def_tech,
                })
        elif old_prim is None or new_prim is None:
            svc.append({
                "field": "primitive_definition",
                "old": old_pn,
                "new": new_pn,
                "note": "one primitive definition is missing",
            })

        old_sec = {s.get("section_number"): s
                   for s in o.get("sections") or []}
        new_sec = {s.get("section_number"): s
                   for s in n.get("sections") or []}

        sec_added = sorted(set(new_sec) - set(old_sec))
        sec_removed = sorted(set(old_sec) - set(new_sec))
        if sec_added or sec_removed:
            elec.append({
                "field": "sections",
                "added": sec_added,
                "removed": sec_removed,
            })

        for sn in sorted(set(old_sec) & set(new_sec)):
            os_, ns_ = old_sec[sn], new_sec[sn]

            path_changed = os_.get("path") != ns_.get("path")
            op_all = (list(os_.get("properties") or [])
                      + list(os_.get("service_properties") or []))
            np_all = (list(ns_.get("properties") or [])
                      + list(ns_.get("service_properties") or []))
            op_elec, op_svc = _split_section_props(op_all)
            np_elec, np_svc = _split_section_props(np_all)

            po_old = _prop_electrical_signature(op_elec)
            po_new = _prop_electrical_signature(np_elec)
            if po_old != po_new:
                oo, on = _multiset_diff(po_old, po_new)
                elec.append({
                    "field": f"section[{sn}].properties",
                    "only_in_old": oo,
                    "only_in_new": on,
                })
            op_full = _prop_full_signature(op_elec)
            np_full = _prop_full_signature(np_elec)
            if op_full != np_full and po_old == po_new:
                oo, on = _multiset_diff(op_full, np_full)
                tech.append({
                    "field": f"section[{sn}].properties.occurrences",
                    "only_in_old": oo,
                    "only_in_new": on,
                })

            so_old = _prop_full_signature(op_svc)
            so_new = _prop_full_signature(np_svc)
            if so_old != so_new:
                oo, on = _multiset_diff(so_old, so_new)
                svc.append({
                    "field": f"section[{sn}].service_properties",
                    "only_in_old": oo,
                    "only_in_new": on,
                })

            # Placement: grouped per section, values (not occurrences).
            placement_details: dict = {}
            if path_changed:
                cls = _classify_path_change(
                    str(os_.get("path", "")), str(ns_.get("path", "")))
                placement_details["path_old"] = os_.get("path")
                placement_details["path_new"] = ns_.get("path")
                placement_details["context_changed"] = cls["context_changed"]
                placement_details["ins_changed"] = cls["ins_changed"]
                placement_details["library_ref_changed"] = cls[
                    "library_ref_changed"]

            for prop_name in sorted(PLACEMENT_PROP_NAMES):
                old_vals = [p for p in op_svc
                            if str(p.get("name")) == prop_name]
                new_vals = [p for p in np_svc
                            if str(p.get("name")) == prop_name]
                old_values = sorted(str(p.get("value", ""))
                                    for p in old_vals)
                new_values = sorted(str(p.get("value", ""))
                                    for p in new_vals)
                if old_values != new_values:
                    placement_details[f"{prop_name}_old"] = old_values
                    placement_details[f"{prop_name}_new"] = new_values

            if placement_details:
                kinds: list[str] = []

                # Page change only when the page segment is parseable
                # on both sides and differs.
                if ("P_PATH_old" in placement_details
                        or "P_PATH_new" in placement_details):
                    pages_old: set[str] = set()
                    pages_new: set[str] = set()
                    for v in placement_details.get("P_PATH_old", []):
                        p = _ppath_page(v)
                        if p is not None:
                            pages_old.add(p)
                    for v in placement_details.get("P_PATH_new", []):
                        p = _ppath_page(v)
                        if p is not None:
                            pages_new.add(p)
                    if pages_old and pages_new and pages_old != pages_new:
                        kinds.append("page_change")
                    else:
                        kinds.append("placement_reference_change")

                if placement_details.get("ins_changed") is True:
                    kinds.append("instance_reference_change")
                if placement_details.get("library_ref_changed") is True:
                    kinds.append("library_reference_change")
                if not kinds:
                    kinds.append("other_change")

                placement.append({
                    "old_refdes": old_ref,
                    "new_refdes": new_ref,
                    "section_number": sn,
                    "kinds": kinds,
                    "label": f"{old_ref}, секция {sn}",
                    "details": placement_details,
                })

        if rename_info is not None:
            svc.insert(0, {
                "field": "refdes_rename",
                "old": old_ref,
                "new": new_ref,
                "shared_paths": rename_info.get("shared_paths", []),
                "note": "auto-matched by section paths and pin names",
            })
            renamed.append({
                "old_refdes": old_ref,
                "new_refdes": new_ref,
                "shared_paths": rename_info.get("shared_paths", []),
            })

        if elec or svc or tech:
            changed.append({
                "old_refdes": old_ref,
                "new_refdes": new_ref,
                "electrical_changes": elec,
                "service_reference_changes": svc,
                "technical_changes": tech,
            })

    changed.sort(key=lambda c: (c["old_refdes"], c["new_refdes"]))
    renamed.sort(key=lambda r: (r["old_refdes"], r["new_refdes"]))
    placement.sort(key=lambda p: (p["old_refdes"], p["section_number"],
                                  tuple(p["kinds"])))

    return {
        "added": added,
        "removed": removed,
        "renamed": renamed,
        "changed": changed,
        "placement_changes": placement,
    }


# ---------------------------------------------------------------------------
# Net matching
# ---------------------------------------------------------------------------

def build_net_matches(old: dict, new: dict, comp_map: dict) -> dict:
    old_by = {(n["path"], n["name"]): n for n in old.get("nets") or []}
    new_by = {(n["path"], n["name"]): n for n in new.get("nets") or []}

    direct: list[dict] = []
    matched_old: set = set()
    matched_new: set = set()

    # 1) exact (path, name)
    for key in sorted(set(old_by) & set(new_by)):
        direct.append({
            "old_name": key[1], "new_name": key[1],
            "old_path": key[0], "new_path": key[0],
            "match": "path_and_name",
        })
        matched_old.add(key)
        matched_new.add(key)

    old_left = {k: v for k, v in old_by.items() if k not in matched_old}
    new_left = {k: v for k, v in new_by.items() if k not in matched_new}

    # 2) same path, name changed
    old_by_path: dict = {}
    for k in old_left:
        old_by_path.setdefault(k[0], []).append(k)
    new_by_path: dict = {}
    for k in new_left:
        new_by_path.setdefault(k[0], []).append(k)

    ambiguous: list[dict] = []
    for p in sorted(set(old_by_path) & set(new_by_path)):
        olds = sorted(old_by_path[p])   # tuple (path, name)
        news = sorted(new_by_path[p])
        if len(olds) == 1 and len(news) == 1:
            ok, nk = olds[0], news[0]
            direct.append({
                "old_name": ok[1], "new_name": nk[1],
                "old_path": p, "new_path": p,
                "match": "path",
            })
            matched_old.add(ok)
            matched_new.add(nk)
        else:
            ambiguous.append({
                "old_candidates": _make_candidates(olds),
                "new_candidates": _make_candidates(news),
                "note": "same path, different names on both sides",
            })

    old_left = {k: v for k, v in old_left.items() if k not in matched_old}
    new_left = {k: v for k, v in new_left.items() if k not in matched_new}

    # 3) exact mapped node set
    def group_by_nodes(nets: dict, cmap: dict) -> dict:
        out: dict = {}
        for k, n in nets.items():
            sig = _mapped_node_frozenset(n, cmap)
            if not sig:
                continue
            out.setdefault(sig, []).append(k)
        return out

    old_by_nodes = group_by_nodes(old_left, comp_map)
    new_by_nodes = group_by_nodes(new_left, {})
    confirmed_renames: list[dict] = []
    for sig in sorted(old_by_nodes, key=_frozenset_sort_key):
        olds = sorted(old_by_nodes[sig])
        news = sorted(new_by_nodes.get(sig, []))
        if not news:
            continue
        if len(olds) == 1 and len(news) == 1:
            ok, nk = olds[0], news[0]
            confirmed_renames.append({
                "old_name": ok[1], "new_name": nk[1],
                "old_path": ok[0], "new_path": nk[0],
                "match": "node_set_exact",
            })
            matched_old.add(ok)
            matched_new.add(nk)
        else:
            ambiguous.append({
                "old_candidates": _make_candidates(olds),
                "new_candidates": _make_candidates(news),
                "note": "multiple nets share the same mapped node set",
            })

    # 4) strict subset -- tentative
    old_left2 = {k: v for k, v in old_left.items() if k not in matched_old}
    new_left2 = {k: v for k, v in new_left.items() if k not in matched_new}
    old_sigs = {k: _mapped_node_frozenset(v, comp_map)
                for k, v in old_left2.items()}
    new_sigs = {k: _mapped_node_frozenset(v, {})
                for k, v in new_left2.items()}

    old_cand: dict = {}
    new_cand: dict = {}
    for ok in sorted(old_sigs):
        osig = old_sigs[ok]
        if not osig:
            continue
        for nk in sorted(new_sigs):
            nsig = new_sigs[nk]
            if not nsig:
                continue
            if osig < nsig or nsig < osig:
                old_cand.setdefault(ok, set()).add(nk)
                new_cand.setdefault(nk, set()).add(ok)

    tentative_renames: list[dict] = []
    for ok in sorted(old_cand):
        news_set = old_cand[ok]
        if len(news_set) != 1:
            ambiguous.append({
                "old_candidates": _make_candidates([ok]),
                "new_candidates": _make_candidates(sorted(news_set)),
                "note": "old net's node set is a strict subset of "
                        "several new nets; pairing not unique",
            })
            continue
        nk = next(iter(news_set))
        olds_set = new_cand.get(nk, set())
        if len(olds_set) != 1:
            ambiguous.append({
                "old_candidates": _make_candidates(sorted(olds_set)),
                "new_candidates": _make_candidates([nk]),
                "note": "new net's node set is a superset of several "
                        "old nets; pairing not unique",
            })
            continue
        tentative_renames.append({
            "old_name": ok[1], "new_name": nk[1],
            "old_path": ok[0], "new_path": nk[0],
            "match": "node_set_subset",
            "note": "node sets overlap but are not identical; tentative",
        })
        matched_old.add(ok)
        matched_new.add(nk)

    ambiguous.sort(key=lambda e: (
        tuple((c["path"], c["name"]) for c in e["old_candidates"]),
        tuple((c["path"], c["name"]) for c in e["new_candidates"]),
    ))

    net_map: dict = {}
    tentative_keys: set = set()
    for entry in direct + confirmed_renames:
        net_map[(entry["old_path"], entry["old_name"])] = (
            entry["new_path"], entry["new_name"])
    for entry in tentative_renames:
        net_map[(entry["old_path"], entry["old_name"])] = (
            entry["new_path"], entry["new_name"])
        tentative_keys.add((entry["old_path"], entry["old_name"]))

    return {
        "direct": direct,
        "confirmed_renames": confirmed_renames,
        "tentative_renames": tentative_renames,
        "ambiguous": ambiguous,
        "map": net_map,
        "tentative_old_keys": tentative_keys,
        "unmatched_old": sorted(k for k in old_by if k not in matched_old),
        "unmatched_new": sorted(k for k in new_by if k not in matched_new),
    }


# ---------------------------------------------------------------------------
# Nets comparison
# ---------------------------------------------------------------------------

def _net_change(o: dict, n: dict, comp_map: dict,
                kind: str) -> Optional[dict]:
    old_nodes: dict = {}
    for nd in o.get("nodes") or []:
        old_nodes.setdefault(_mapped_node_key(nd, comp_map), nd)
    new_nodes: dict = {}
    for nd in n.get("nodes") or []:
        new_nodes.setdefault(_node_elec_key(nd), nd)

    added = sorted(set(new_nodes) - set(old_nodes))
    removed = sorted(set(old_nodes) - set(new_nodes))

    changed_nodes: list[dict] = []
    for k in sorted(set(old_nodes) & set(new_nodes)):
        on, nn = old_nodes[k], new_nodes[k]
        po = _prop_electrical_signature(on.get("properties") or [])
        pn = _prop_electrical_signature(nn.get("properties") or [])
        po_full = _prop_full_signature(on.get("properties") or [])
        pn_full = _prop_full_signature(nn.get("properties") or [])

        entry: dict = {"refdes": k[0], "pin_number": k[1]}
        has_change = False

        if po != pn or on.get("pin_name") != nn.get("pin_name"):
            oo, on_ = _multiset_diff(po, pn) if po != pn else ([], [])
            entry["pin_name_old"] = on.get("pin_name")
            entry["pin_name_new"] = nn.get("pin_name")
            entry["properties_only_in_old"] = oo
            entry["properties_only_in_new"] = on_
            has_change = True

        if po_full != pn_full and po == pn:
            oo, on_ = _multiset_diff(po_full, pn_full)
            entry["properties_occurrences_only_in_old"] = oo
            entry["properties_occurrences_only_in_new"] = on_
            has_change = True

        if has_change:
            changed_nodes.append(entry)

    # Net properties: split into electrical and reference.
    old_elec_props, old_ref_props = _split_net_props(o.get("properties") or [])
    new_elec_props, new_ref_props = _split_net_props(n.get("properties") or [])

    net_elec_old = _prop_electrical_signature(old_elec_props)
    net_elec_new = _prop_electrical_signature(new_elec_props)
    if net_elec_old != net_elec_new:
        npo, npn = _multiset_diff(net_elec_old, net_elec_new)
    else:
        npo, npn = [], []

    net_elec_old_full = _prop_full_signature(old_elec_props)
    net_elec_new_full = _prop_full_signature(new_elec_props)
    if (net_elec_old_full != net_elec_new_full
            and net_elec_old == net_elec_new):
        tpo, tpn = _multiset_diff(net_elec_old_full, net_elec_new_full)
    else:
        tpo, tpn = [], []

    ref_old = _prop_full_signature(old_ref_props)
    ref_new = _prop_full_signature(new_ref_props)
    if ref_old != ref_new:
        rpo, rpn = _multiset_diff(ref_old, ref_new)
    else:
        rpo, rpn = [], []

    name_changed = o.get("name") != n.get("name")
    path_changed = o.get("path") != n.get("path")

    if not (added or removed or changed_nodes
            or npo or npn or tpo or tpn or rpo or rpn
            or name_changed or path_changed):
        return None

    return {
        "old_name": o.get("name"),
        "new_name": n.get("name"),
        "old_path": o.get("path"),
        "new_path": n.get("path"),
        "name_changed": name_changed,
        "path_changed": path_changed,
        "match_kind": kind,
        "nodes_added": [
            {"refdes": k[0], "pin_number": k[1]} for k in added
        ],
        "nodes_removed": [
            {"refdes": k[0], "pin_number": k[1]} for k in removed
        ],
        "nodes_changed": changed_nodes,
        "properties_only_in_old": npo,
        "properties_only_in_new": npn,
        "properties_occurrences_only_in_old": tpo,
        "properties_occurrences_only_in_new": tpn,
        "reference_properties_only_in_old": rpo,
        "reference_properties_only_in_new": rpn,
    }


def compare_nets(old: dict, new: dict, matches: dict,
                 comp_map: dict) -> dict:
    old_by = {(n["path"], n["name"]): n for n in old.get("nets") or []}
    new_by = {(n["path"], n["name"]): n for n in new.get("nets") or []}

    changed: list[dict] = []
    renamed: list[dict] = []
    path_relocations: list[dict] = []

    def _do_pair(entry: dict, tentative: bool) -> None:
        ok = (entry["old_path"], entry["old_name"])
        nk = (entry["new_path"], entry["new_name"])
        o = old_by.get(ok)
        n = new_by.get(nk)
        if o is None or n is None:
            return
        ch = _net_change(o, n, comp_map, entry["match"])
        if ch is None:
            return
        if tentative:
            ch["tentative"] = True
            ch["note"] = entry.get("note", "tentative match")
        changed.append(ch)
        # Only confirmed renames enter the renamed list.
        if ch["name_changed"] and not tentative:
            renamed.append({
                "old_name": ch["old_name"], "new_name": ch["new_name"],
                "old_path": ch["old_path"], "new_path": ch["new_path"],
                "match": entry["match"],
            })
        if ch["path_changed"] and not ch["name_changed"]:
            path_relocations.append({
                "name": ch["old_name"],
                "old_path": ch["old_path"],
                "new_path": ch["new_path"],
                "match": entry["match"],
                "tentative": tentative,
            })

    for entry in matches["direct"]:
        _do_pair(entry, tentative=False)
    for entry in matches["confirmed_renames"]:
        _do_pair(entry, tentative=False)
    for entry in matches["tentative_renames"]:
        _do_pair(entry, tentative=True)

    added: list[dict] = []
    for k in matches["unmatched_new"]:
        n = new_by.get(k)
        if n is None:
            continue
        added.append({
            "name": n.get("name"),
            "path": n.get("path"),
            "nodes": _sort_nodes(
                [_node_summary(nd) for nd in n.get("nodes") or []]),
        })
    removed: list[dict] = []
    for k in matches["unmatched_old"]:
        n = old_by.get(k)
        if n is None:
            continue
        removed.append({
            "name": n.get("name"),
            "path": n.get("path"),
            "nodes": _sort_nodes(
                [_node_summary(nd) for nd in n.get("nodes") or []]),
        })

    changed.sort(key=lambda c: (c["old_path"], c["old_name"]))
    renamed.sort(key=lambda c: (c["old_path"], c["new_path"]))
    path_relocations.sort(key=lambda c: (c["old_path"], c["new_path"]))

    return {
        "added": added,
        "removed": removed,
        "renamed": renamed,
        "path_relocations": path_relocations,
        "changed": changed,
        "tentative_renames": list(matches["tentative_renames"]),
        "ambiguous": list(matches["ambiguous"]),
    }


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------

def compare_pins(old: dict, new: dict,
                 comp_matches: dict, net_matches: dict) -> dict:
    comp_map = comp_matches["map"]
    net_map = net_matches["map"]
    tentative_old_keys: set = net_matches.get("tentative_old_keys", set())

    old_pin_to_net: dict = {}
    for net in old.get("nets") or []:
        key = (net["path"], net["name"])
        for nd in net.get("nodes") or []:
            k = _node_elec_key(nd)
            old_pin_to_net.setdefault(k, key)
    new_pin_to_net: dict = {}
    for net in new.get("nets") or []:
        key = (net["path"], net["name"])
        for nd in net.get("nodes") or []:
            k = _node_elec_key(nd)
            new_pin_to_net.setdefault(k, key)

    old_mapped: dict = {}
    for k, net_key in old_pin_to_net.items():
        new_r = comp_map.get(k[0], k[0])
        old_mapped[(new_r, k[1])] = (net_key, k[0])

    appeared: list[dict] = []
    for k in sorted(set(new_pin_to_net) - set(old_mapped)):
        net_key = new_pin_to_net[k]
        appeared.append({
            "refdes": k[0], "pin_number": k[1],
            "net_name": net_key[1], "net_path": net_key[0],
        })

    disappeared: list[dict] = []
    for k in sorted(set(old_mapped) - set(new_pin_to_net)):
        old_net_key, old_r = old_mapped[k]
        disappeared.append({
            "refdes": old_r, "pin_number": k[1],
            "net_name": old_net_key[1], "net_path": old_net_key[0],
        })

    moved: list[dict] = []
    for k in sorted(set(old_mapped) & set(new_pin_to_net)):
        old_net_key, _old_r = old_mapped[k]
        new_net_key = new_pin_to_net[k]
        target = net_map.get(old_net_key)
        if target is None:
            target = (None, None)
        if target != new_net_key:
            cond = old_net_key in tentative_old_keys
            entry = {
                "refdes": k[0], "pin_number": k[1],
                "from_net_name": old_net_key[1],
                "from_net_path": old_net_key[0],
                "to_net_name": new_net_key[1],
                "to_net_path": new_net_key[0],
            }
            if cond:
                entry["conditional"] = True
            moved.append(entry)

    # Pins whose classification ("no move") depends on a tentative net match.
    relying_on_tentative: list[dict] = []
    for tr in net_matches.get("tentative_renames", []):
        old_key = (tr["old_path"], tr["old_name"])
        new_key = (tr["new_path"], tr["new_name"])
        old_net_obj = next(
            (nn for nn in old.get("nets") or []
             if (nn["path"], nn["name"]) == old_key), None)
        new_net_obj = next(
            (nn for nn in new.get("nets") or []
             if (nn["path"], nn["name"]) == new_key), None)
        if old_net_obj is None or new_net_obj is None:
            continue
        old_pins = {_node_elec_key(nd)
                    for nd in old_net_obj.get("nodes") or []}
        new_pins = {_node_elec_key(nd)
                    for nd in new_net_obj.get("nodes") or []}
        old_pins_mapped = {(comp_map.get(r, r), p) for (r, p) in old_pins}
        shared = sorted(old_pins_mapped & new_pins)
        for (r, p) in shared:
            relying_on_tentative.append({
                "refdes": r,
                "pin_number": p,
                "old_net_name": tr["old_name"],
                "new_net_name": tr["new_name"],
                "note": "verdict 'no move' depends on tentative net match",
            })

    relying_on_tentative.sort(key=lambda e: (e["refdes"], e["pin_number"],
                                             e["old_net_name"],
                                             e["new_net_name"]))

    return {
        "appeared": appeared,
        "disappeared": disappeared,
        "moved": moved,
        "relying_on_tentative": relying_on_tentative,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _is_net_change_electrical(entry: dict) -> bool:
    if entry.get("nodes_added") or entry.get("nodes_removed"):
        return True
    for nc in entry.get("nodes_changed") or []:
        if (nc.get("properties_only_in_old")
                or nc.get("properties_only_in_new")
                or nc.get("pin_name_old") != nc.get("pin_name_new")):
            return True
    if entry.get("properties_only_in_old") or entry.get(
            "properties_only_in_new"):
        return True
    return False


def _is_comp_change_electrical(entry: dict) -> bool:
    return bool(entry.get("electrical_changes"))


def _has_any_net_change(entry: dict) -> bool:
    return bool(
        entry.get("nodes_added") or entry.get("nodes_removed")
        or entry.get("nodes_changed")
        or entry.get("properties_only_in_old")
        or entry.get("properties_only_in_new")
        or entry.get("properties_occurrences_only_in_old")
        or entry.get("properties_occurrences_only_in_new")
        or entry.get("reference_properties_only_in_old")
        or entry.get("reference_properties_only_in_new")
        or entry.get("name_changed") or entry.get("path_changed")
    )


def build_report(old: dict, new: dict,
                 old_path: str, new_path: str) -> dict:
    prim = compare_primitives(old, new)
    comp_matches = build_component_matches(old, new)
    comp = compare_components(old, new, comp_matches)
    net_matches = build_net_matches(old, new, comp_matches["map"])
    nets = compare_nets(old, new, net_matches, comp_matches["map"])
    pins = compare_pins(old, new, comp_matches, net_matches)

    comp_changed_elec = [c for c in comp["changed"]
                         if _is_comp_change_electrical(c)]
    net_changed_elec = [c for c in nets["changed"]
                        if _is_net_change_electrical(c)]

    confirmed_renames_count = sum(
        1 for r in nets["renamed"] if not r.get("tentative"))
    tentative_renames_count = len(nets["tentative_renames"])

    has_uncertainty = bool(
        comp_matches["ambiguous"]
        or net_matches["ambiguous"]
        or net_matches["tentative_renames"]
    )

    electrically_identical = (
        not comp["added"]
        and not comp["removed"]
        and not comp_changed_elec
        and not nets["added"]
        and not nets["removed"]
        and not net_changed_elec
        and not pins["appeared"]
        and not pins["disappeared"]
        and not pins["moved"]
        and not has_uncertainty
    )

    fully_identical = (
        electrically_identical
        and not prim["added"] and not prim["removed"] and not prim["changed"]
        and not comp["renamed"]
        and not comp["placement_changes"]
        and not nets["renamed"]
        and not nets["path_relocations"]
        and all(not c.get("electrical_changes")
                and not c.get("service_reference_changes")
                and not c.get("technical_changes")
                for c in comp["changed"])
        and all(not _has_any_net_change(c) for c in nets["changed"])
        and not pins.get("relying_on_tentative")
    )

    summary = {
        "electrically_identical": electrically_identical,
        "fully_identical": fully_identical,
        "has_uncertainty": has_uncertainty,
        "primitives": {
            "added": len(prim["added"]),
            "removed": len(prim["removed"]),
            "changed": len(prim["changed"]),
        },
        "components": {
            "added": len(comp["added"]),
            "removed": len(comp["removed"]),
            "renamed": len(comp["renamed"]),
            "changed_electrically": len(comp_changed_elec),
            "placement": len(comp["placement_changes"]),
        },
        "nets": {
            "added": len(nets["added"]),
            "removed": len(nets["removed"]),
            "renamed_confirmed": confirmed_renames_count,
            "renamed_tentative": tentative_renames_count,
            "path_relocated": len(nets["path_relocations"]),
            "changed_electrically": len(net_changed_elec),
            "ambiguous": len(nets["ambiguous"]),
        },
        "pins": {
            "appeared": len(pins["appeared"]),
            "disappeared": len(pins["disappeared"]),
            "moved": len(pins["moved"]),
            "conditional": sum(1 for m in pins["moved"]
                               if m.get("conditional")),
            "relying_on_tentative": len(pins["relying_on_tentative"]),
        },
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "inputs": {"old": old_path, "new": new_path},
        "component_matches": {
            "direct": comp_matches["direct"],
            "confirmed_renames": comp_matches["confirmed_renames"],
            "ambiguous": comp_matches["ambiguous"],
            "unmatched_old": comp_matches["unmatched_old"],
            "unmatched_new": comp_matches["unmatched_new"],
        },
        "net_matches": {
            "direct": net_matches["direct"],
            "confirmed_renames": net_matches["confirmed_renames"],
            "tentative_renames": net_matches["tentative_renames"],
            "ambiguous": net_matches["ambiguous"],
            "unmatched_old": net_matches["unmatched_old"],
            "unmatched_new": net_matches["unmatched_new"],
        },
        "primitives": prim,
        "components": comp,
        "nets": nets,
        "pins": pins,
        "summary": summary,
    }


def format_report(rep: dict) -> str:
    lines: list[str] = []
    s = rep["summary"]

    flags = []
    if s["electrically_identical"]:
        flags.append("electrically-identical")
    if s["fully_identical"]:
        flags.append("fully-identical")
    if s["has_uncertainty"]:
        flags.append("uncertain")
    lines.append(
        "summary: "
        f"components +{s['components']['added']}"
        f" -{s['components']['removed']}"
        f" renamed={s['components']['renamed']}"
        f" elec={s['components']['changed_electrically']}"
        f" placement={s['components']['placement']} | "
        f"nets +{s['nets']['added']} -{s['nets']['removed']}"
        f" renamed={s['nets']['renamed_confirmed']}"
        f" tentative={s['nets']['renamed_tentative']}"
        f" path_relocated={s['nets']['path_relocated']}"
        f" elec={s['nets']['changed_electrically']}"
        f" amb={s['nets']['ambiguous']} | "
        f"pins +{s['pins']['appeared']}"
        f" -{s['pins']['disappeared']}"
        f" moved={s['pins']['moved']}"
        f" cond={s['pins']['conditional']}"
        f" rely_tent={s['pins']['relying_on_tentative']} | "
        f"primitives +{s['primitives']['added']}"
        f" -{s['primitives']['removed']}"
        f" ~{s['primitives']['changed']}"
        + (f" [{','.join(flags)}]" if flags else "")
    )

    p = rep["primitives"]
    if p["added"] or p["removed"] or p["changed"] or p["possible_renames"]:
        lines.append("")
        lines.append("primitives (library, informational):")
        for name in p["added"]:
            lines.append(f"  + {name}")
        for name in p["removed"]:
            lines.append(f"  - {name}")
        for ch in p["changed"]:
            lines.append(f"  ~ {ch['name']}")
            for entry in ch["electrical_changes"]:
                lines.append(f"      [electrical] {entry}")
            for entry in ch["technical_changes"]:
                lines.append(f"      [technical]  {entry}")
        for rn in p["possible_renames"]:
            lines.append(f"  > possible rename {rn['old']} -> "
                         f"{rn['new']} ({rn['note']})")
        for am in p["ambiguous"]:
            lines.append(f"  ? ambiguous primitive pairing: "
                         f"old={am['old_candidates']} "
                         f"new={am['new_candidates']} ({am['note']})")

    c = rep["components"]
    if (c["added"] or c["removed"] or c["renamed"] or c["changed"]
            or c["placement_changes"]):
        lines.append("")
        lines.append("components:")
        for refdes in c["added"]:
            lines.append(f"  + {refdes}")
        for refdes in c["removed"]:
            lines.append(f"  - {refdes}")
        for rn in c["renamed"]:
            lines.append(f"  > renamed {rn['old_refdes']} -> "
                         f"{rn['new_refdes']}")
        for ch in c["changed"]:
            tag = ch["old_refdes"]
            if ch["old_refdes"] != ch["new_refdes"]:
                tag += f" -> {ch['new_refdes']}"
            lines.append(f"  ~ {tag}")
            for entry in ch["electrical_changes"]:
                lines.append(f"      [electrical] {entry}")
            for entry in ch["service_reference_changes"]:
                lines.append(f"      [service]    {entry}")
            for entry in ch.get("technical_changes", []):
                lines.append(f"      [technical]  {entry}")
        for pl in c["placement_changes"]:
            lines.append(f"  > placement {pl['label']}: "
                         f"{','.join(pl['kinds'])}")
            for k, v in sorted(pl["details"].items()):
                lines.append(f"      {k}: {v}")
    if rep["component_matches"]["ambiguous"]:
        lines.append("")
        lines.append("component matching -- ambiguous:")
        for am in rep["component_matches"]["ambiguous"]:
            lines.append(f"  ? old={am['old_candidates']} "
                         f"new={am['new_candidates']} ({am['note']})")

    n = rep["nets"]
    if (n["added"] or n["removed"] or n["renamed"]
            or n["path_relocations"] or n["changed"]
            or n["tentative_renames"] or n["ambiguous"]):
        lines.append("")
        lines.append("nets:")
        for entry in n["added"]:
            lines.append(f"  + {entry['name']}  path={entry['path']}")
            for nd in entry["nodes"]:
                lines.append(f"      {nd['refdes']}.{nd['pin_number']}")
        for entry in n["removed"]:
            lines.append(f"  - {entry['name']}  path={entry['path']}")
            for nd in entry["nodes"]:
                lines.append(f"      {nd['refdes']}.{nd['pin_number']}")
        for rn in n["renamed"]:
            lines.append(f"  > renamed {rn['old_name']} -> "
                         f"{rn['new_name']}  ({rn.get('match', '')})")
        for pr in n["path_relocations"]:
            mark = " (tentative)" if pr.get("tentative") else ""
            lines.append(f"  > path relocated {pr['name']}: "
                         f"{pr['old_path']} -> {pr['new_path']}{mark}")
        for ch in n["changed"]:
            tag = ch["old_name"]
            if ch["name_changed"]:
                tag += f" -> {ch['new_name']}"
            mark = " (tentative)" if ch.get("tentative") else ""
            lines.append(f"  ~ {tag}  path={ch['old_path']}{mark}")
            for k in ch["nodes_added"]:
                lines.append(f"      + {k['refdes']}.{k['pin_number']}")
            for k in ch["nodes_removed"]:
                lines.append(f"      - {k['refdes']}.{k['pin_number']}")
            for nc in ch["nodes_changed"]:
                lines.append(f"      ~ {nc['refdes']}.{nc['pin_number']}")
                for pr in nc.get("properties_only_in_old", []):
                    lines.append(f"          [prop old] {pr}")
                for pr in nc.get("properties_only_in_new", []):
                    lines.append(f"          [prop new] {pr}")
                for pr in nc.get(
                        "properties_occurrences_only_in_old", []):
                    lines.append(f"          [occ old] {pr}")
                for pr in nc.get(
                        "properties_occurrences_only_in_new", []):
                    lines.append(f"          [occ new] {pr}")
            for pr in ch["properties_only_in_old"]:
                lines.append(f"      [prop old] {pr}")
            for pr in ch["properties_only_in_new"]:
                lines.append(f"      [prop new] {pr}")
            for pr in ch["properties_occurrences_only_in_old"]:
                lines.append(f"      [occ old] {pr}")
            for pr in ch["properties_occurrences_only_in_new"]:
                lines.append(f"      [occ new] {pr}")
            for pr in ch["reference_properties_only_in_old"]:
                lines.append(f"      [ref old] {pr}")
            for pr in ch["reference_properties_only_in_new"]:
                lines.append(f"      [ref new] {pr}")
        if n["tentative_renames"]:
            lines.append("  tentative renames:")
            for tr in n["tentative_renames"]:
                lines.append(f"    ? {tr['old_name']} -> {tr['new_name']}"
                             f"  ({tr['note']})")
        if n["ambiguous"]:
            lines.append("  ambiguous pairs:")
            for am in n["ambiguous"]:
                lines.append(f"    ? old={am['old_candidates']} "
                             f"new={am['new_candidates']} ({am['note']})")

    pi = rep["pins"]
    if (pi["appeared"] or pi["disappeared"] or pi["moved"]
            or pi["relying_on_tentative"]):
        lines.append("")
        lines.append("pins:")
        for entry in pi["appeared"]:
            lines.append(f"  + {entry['refdes']}.{entry['pin_number']} -> "
                         f"{entry['net_name']}")
        for entry in pi["disappeared"]:
            lines.append(f"  - {entry['refdes']}.{entry['pin_number']} "
                         f"(was in {entry['net_name']})")
        for entry in pi["moved"]:
            mark = " (conditional)" if entry.get("conditional") else ""
            lines.append(f"  > {entry['refdes']}.{entry['pin_number']} "
                         f"{entry['from_net_name']} -> "
                         f"{entry['to_net_name']}{mark}")
        if pi["relying_on_tentative"]:
            lines.append("  relying on tentative net matches:")
            for entry in pi["relying_on_tentative"]:
                lines.append(
                    f"    ? {entry['refdes']}.{entry['pin_number']} "
                    f"({entry['old_net_name']} -> "
                    f"{entry['new_net_name']}): {entry['note']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def _atomic_write(path: str, text: str) -> None:
    dirname = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(
        prefix=".compare_netlists_", suffix=".tmp", dir=dirname)
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
        description="Compare two normalized netlist JSON files produced "
                    "by parse_capture_netlist.py.")
    p.add_argument("old", help="older normalized JSON")
    p.add_argument("new", help="newer normalized JSON")
    p.add_argument("--json", dest="json_stdout", action="store_true",
                   help="emit JSON report to stdout (in addition to "
                        "--output, if given)")
    p.add_argument("--output", default=None,
                   help="write JSON report to this file (atomic)")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    for path in (args.old, args.new):
        if not os.path.isfile(path):
            print(f"error: file not found: {path}", file=sys.stderr)
            return EXIT_ERROR

    try:
        old = _load_json(args.old)
        new = _load_json(args.new)
    except ValueError as e:
        print(f"error: cannot parse JSON: {e}", file=sys.stderr)
        return EXIT_ERROR
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON: {e}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR

    issues = validate_model(old, "old") + validate_model(new, "new")
    if issues:
        for issue in issues:
            print(f"validation: {issue}", file=sys.stderr)
        return EXIT_ERROR

    rep = build_report(old, new, args.old, args.new)
    text = json.dumps(rep, ensure_ascii=False, indent=2, sort_keys=False)

    if args.output:
        try:
            _atomic_write(args.output, text + "\n")
        except OSError as e:
            print(f"error: cannot write output: {e}", file=sys.stderr)
            return EXIT_ERROR

    if args.json_stdout:
        sys.stdout.write(text)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(format_report(rep))
        sys.stdout.write("\n")

    s = rep["summary"]
    if s["electrically_identical"]:
        return EXIT_OK
    return EXIT_DIFFERENCES


if __name__ == "__main__":
    sys.exit(main())
