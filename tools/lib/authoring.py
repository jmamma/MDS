"""Expand an MDS machine JSON definition into packaging inputs."""
from __future__ import annotations

from collections.abc import Mapping

from .mds_format import IMPORT_RESOURCES, FormatError, build_manifest, normalize_manifest

ENTRY_DEFAULTS = {"init_symbol": "INIT", "trig_symbol": "TRIG", "render_symbol": "RENDER"}


def import_kind(symbol: int) -> int:
    kinds = [kind for kind, resource in IMPORT_RESOURCES if resource == symbol]
    if len(kinds) != 1:
        raise FormatError(f"ABI symbol {symbol!r} has no unique import kind")
    return kinds[0]


def normalize_program(program: Mapping) -> dict:
    if not isinstance(program, Mapping):
        raise FormatError("program must be an object")
    p = dict(program)
    allowed = set(ENTRY_DEFAULTS) | {"abi_version", "x_workset_words", "y_workset_words",
                                    "relocations", "imports"}
    if p.keys() - allowed:
        raise FormatError("unknown program fields: " + ", ".join(sorted(p.keys() - allowed)))
    if type(p.get("abi_version", 1)) is not int or p.get("abi_version", 1) != 1:
        raise FormatError("program ABI version must be 1")
    for key, value in ENTRY_DEFAULTS.items():
        p.setdefault(key, value)
        if not isinstance(p[key], str) or not p[key]:
            raise FormatError(f"{key} must name an assembly label")
    for key in ("x_workset_words", "y_workset_words"):
        p.setdefault(key, 0)
        if type(p[key]) is not int or not 0 <= p[key] <= 64:
            raise FormatError(f"{key} must be an integer in 0..64")
    for key in ("relocations", "imports"):
        rows = p.get(key, [])
        if not isinstance(rows, list):
            raise FormatError(f"program {key} must be a list")
        result = []
        for row in rows:
            if isinstance(row, str) and key == "relocations":
                row = {"symbol": row}
            if not isinstance(row, Mapping) or not isinstance(row.get("symbol"), str) or not row["symbol"]:
                raise FormatError(f"program {key} must name assembly labels")
            item = dict(row)
            allowed = {"symbol", "word_delta"} | ({"kind", "abi_symbol"} if key == "imports" else set())
            if item.keys() - allowed:
                raise FormatError("unknown patch fields: " + ", ".join(sorted(item.keys() - allowed)))
            item.setdefault("word_delta", 1)
            if type(item["word_delta"]) is not int:
                raise FormatError("word_delta must be an integer")
            if key == "imports":
                resource = item.get("abi_symbol")
                if type(resource) is not int:
                    raise FormatError("abi_symbol must be an integer")
                expected = import_kind(resource)
                item.setdefault("kind", expected)
                if type(item["kind"]) is not int or item["kind"] != expected:
                    raise FormatError(f"wrong import kind for ABI symbol {resource}")
            result.append(item)
        p[key] = result
    p["abi_version"] = 1
    return p


def normalize_document(document: Mapping) -> dict:
    """Expand a machine definition and check it against the package format."""
    result = normalize_manifest(document)
    missing = {"id", "display_name", "category", "short_label"} - document.keys()
    if missing:
        raise FormatError("missing machine fields: " + ", ".join(sorted(missing)))
    if type(result["abi_version"]) is not int or result["abi_version"] != 1:
        raise FormatError("machine ABI version must be 1")
    if type(result["max_track_cycles"]) is not int or not 0 <= result["max_track_cycles"] <= 65535:
        raise FormatError("max_track_cycles must be an integer in 0..65535")
    if "program" not in document:
        raise FormatError("machine definition needs a program section")
    result["program"] = normalize_program(document["program"])
    build_manifest(result)
    required_y = max(1 + len(result["params"]) if result["params"] else 0,
                     10 if result.get("stereo_pair", False) else 0)
    if result["program"]["y_workset_words"] < required_y:
        raise FormatError(f"program needs at least {required_y} Y workset words")
    return result
