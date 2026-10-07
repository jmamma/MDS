#!/usr/bin/env python3
"""Create an MDS machine package from its JSON definition and assembled program."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from lib.authoring import normalize_document
from lib.mds_format import FormatError, build_package, decode_machine_bitmap_asset, package_parts, parse_package


def parse_lod(text: str) -> tuple[bytes, dict[str, int]]:
    """Extract a contiguous origin-zero P image and its symbols from CLDLOD text."""
    memory: dict[int, int] = {}
    symbols: dict[str, int] = {}
    space = None
    address = 0
    in_symbols = False
    for line in text.splitlines():
        line = line.strip()
        data = re.fullmatch(r"_DATA\s+([A-Za-z][A-Za-z0-9]*)\s+([0-9A-Fa-f]+)", line)
        symbol_header = re.fullmatch(r"_SYMBOL\s+([PXY])", line)
        if data:
            space, address, in_symbols = data[1], int(data[2], 16), False
            continue
        if symbol_header:
            space, in_symbols = symbol_header[1], True
            continue
        if line.startswith(("_DATA", "_BLOCKDATA")):
            raise FormatError("unsupported LOD data record; use explicit contiguous _DATA P words")
        if line.startswith("_"):
            space, in_symbols = None, False
            continue
        if not line or space is None:
            continue
        if in_symbols:
            symbol = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s+\S\s+([0-9A-Fa-f]+)", line)
            if symbol and space == "P":
                value = int(symbol[2], 16)
                if symbol[1] in symbols and symbols[symbol[1]] != value:
                    raise FormatError(f"conflicting P symbol {symbol[1]!r}")
                symbols[symbol[1]] = value
            continue
        if space != "P":
            raise FormatError("MDS programs cannot contain initialized X/Y memory")
        for token in line.split():
            if not re.fullmatch(r"[0-9A-Fa-f]{6}", token):
                raise FormatError(f"invalid 24-bit LOD word: {token!r}")
            if address in memory:
                raise FormatError(f"duplicate P-memory word at {address:#x}")
            memory[address] = int(token, 16)
            address += 1
    if not memory or min(memory) != 0 or max(memory) + 1 != len(memory):
        raise FormatError("program must be one contiguous P-memory section starting at zero")
    return b"".join(memory[i].to_bytes(3, "big") for i in range(len(memory))), symbols


def read_symbols(path: Path) -> dict[str, int]:
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, dict):
        raise FormatError("symbol map must be a JSON object mapping labels to word offsets")
    result = {}
    for name, value in values.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise FormatError(f"invalid symbol name: {name!r}")
        if isinstance(value, str):
            try:
                value = int(value, 16 if value.lower().startswith("0x") else 10)
            except ValueError as error:
                raise FormatError(f"invalid word offset for {name}") from error
        if type(value) is not int or value < 0:
            raise FormatError(f"symbol {name} requires a nonnegative program-word offset")
        result[name] = value
    return result


def read_cld(path: Path, dsp_tools: Path | None) -> tuple[bytes, dict[str, int]]:
    directory = dsp_tools or os.environ.get("DSP56300_TOOLS")
    if directory:
        directory = Path(directory).expanduser()
        converter = next((directory / name for name in ("cldlod.exe", "cldlod")
                          if (directory / name).is_file()), None)
    else:
        found = shutil.which("cldlod") or shutil.which("cldlod.exe")
        converter = Path(found) if found else None
    if converter is None:
        raise FormatError("CLD input needs cldlod; specify --dsp-tools or supply a .lod file")
    command = [str(converter.resolve()), str(path.resolve())]
    if converter.suffix.lower() == ".exe" and os.name != "nt":
        wine = shutil.which("wine")
        if not wine:
            raise FormatError("Wine is required to run cldlod.exe on this platform")
        command.insert(0, wine)
    try:
        result = subprocess.run(command, cwd=path.parent, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FormatError(f"could not convert CLD: {error}") from error
    if result.returncode:
        raise FormatError("cldlod failed: " + (result.stderr or result.stdout).strip()[-2000:])
    return parse_lod(result.stdout)


def make_package(document: dict, program: bytes, symbols: dict[str, int], bitmap: bytes | None = None) -> bytes:
    manifest = normalize_document(document)
    if not program or len(program) % 3:
        raise FormatError("raw program must contain three big-endian bytes per 24-bit word")
    words = len(program) // 3
    spec = manifest["program"]

    def address(name: str, delta: int = 0) -> int:
        if name not in symbols:
            raise FormatError(f"missing program symbol {name!r}; raw .bin input needs --symbols")
        value = symbols[name]
        if type(value) is not int:
            raise FormatError(f"symbol {name!r} must have an integer word offset")
        value += delta
        if not 0 <= value < words:
            raise FormatError(f"symbol/patch {name!r} is outside the program")
        return value

    link = {
        "abi_version": spec["abi_version"],
        "init_word": address(spec["init_symbol"]),
        "mutate_word": address(spec["trig_symbol"]),
        "execute_word": address(spec["render_symbol"]),
        "x_workset_words": spec["x_workset_words"],
        "y_workset_words": spec["y_workset_words"],
        "relocations": [address(row["symbol"], row["word_delta"]) for row in spec["relocations"]],
        "imports": [{"patch_word": address(row["symbol"], row["word_delta"]),
                     "kind": row["kind"], "symbol": row["abi_symbol"]}
                    for row in spec["imports"]],
    }
    package = build_package(package_parts(program, manifest, link, bitmap))
    parse_package(package)
    return package


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("definition", type=Path, help="machine JSON definition")
    parser.add_argument("program", type=Path, help="absolute .cld, CLDLOD .lod, or packed raw .bin")
    parser.add_argument("--format", choices=("auto", "cld", "lod", "bin"), default="auto")
    parser.add_argument("--symbols", type=Path, help="JSON label-to-word-offset map for raw .bin input")
    parser.add_argument("--dsp-tools", type=Path, help="directory containing cldlod.exe (for CLD input)")
    parser.add_argument("--bitmap", type=Path, help="optional 32x24 P1 PBM or 128-byte native sprite")
    parser.add_argument("--max-track-cycles", type=int, help="measured worst-case cycle declaration; otherwise use JSON")
    parser.add_argument("-o", "--output", type=Path, help="output .mds file (default: beside JSON)")
    args = parser.parse_args(argv)
    output = args.output or args.definition.with_suffix(".mds")
    try:
        inputs = [args.definition, args.program, args.symbols, args.bitmap]
        if any(path and path.resolve() == output.resolve() for path in inputs):
            raise FormatError("output must differ from all input files")
        document = json.loads(args.definition.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise FormatError("machine definition must be a JSON object")
        if args.max_track_cycles is not None:
            document["max_track_cycles"] = args.max_track_cycles
        kind = args.format if args.format != "auto" else args.program.suffix.lower().lstrip(".")
        if kind == "cld":
            program, symbols = read_cld(args.program, args.dsp_tools)
        elif kind == "lod":
            program, symbols = parse_lod(args.program.read_text(encoding="ascii"))
        elif kind == "bin":
            if args.symbols is None:
                raise FormatError("raw .bin input needs --symbols: binary words do not retain assembly labels")
            program, symbols = args.program.read_bytes(), {}
        else:
            raise FormatError("use an absolute .cld, .lod or packed .bin; relocatable .cln objects need linking first")
        if args.symbols is not None:
            supplied = read_symbols(args.symbols)
            for name, value in supplied.items():
                if name in symbols and symbols[name] != value:
                    raise FormatError(f"symbol map conflicts with assembled symbol {name!r}")
            symbols.update(supplied)
        bitmap = decode_machine_bitmap_asset(args.bitmap.read_bytes()) if args.bitmap else None
        package = make_package(document, program, symbols, bitmap)
        output.write_bytes(package)
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.error(str(error))
    parsed = parse_package(package)
    cycles = parsed["max_track_cycles"]
    status = f"declared cycles={cycles}" if cycles else "DRAFT: no cycle declaration; cannot be installed"
    print(f"{output} ({parsed['program_words']} program words; {status})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
