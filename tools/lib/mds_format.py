"""MDS1 package and MDM1 machine-description encoder.

Implements the package format in docs/MDX_MachineDumpStandard.txt.
"""

from __future__ import annotations

import hashlib
import re
import struct
import zlib
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


PACKAGE_HEADER_BYTES = 128
MANIFEST_BYTES = 128
VALUE_TEXT_ENTRY_BYTES = 8
MAX_VALUE_TEXT_ENTRIES = 32
MANIFEST_MAX_BYTES = MANIFEST_BYTES + MAX_VALUE_TEXT_ENTRIES * VALUE_TEXT_ENTRY_BYTES
MACHINE_BITMAP_WIDTH = 32
MACHINE_BITMAP_HEIGHT = 24
MACHINE_BITMAP_SCAN_WORDS = 1
MACHINE_BITMAP_BYTES = MACHINE_BITMAP_WIDTH * MACHINE_BITMAP_SCAN_WORDS * 4
PROGRAM_MAX_WORDS = 14_052
MACHINE_ABI_VERSION = 1
WORKSET_WORDS = 64
MAX_IMPORTS = 64
MAX_PARAMETERS = 8
# Largest program with 64 imports and 32 value-text entries.
PACKAGE_MAX_BYTES = 44_812
TARGET_DSP56303 = 0x5603
CODEC_RAW24 = 0
IMPORT_X_READ_BASE = 1
IMPORT_SINE_TABLE = 1
IMPORT_XY_WRITE_BASE = 2
IMPORT_X_WRITE_BASE = 3
IMPORT_Y_WRITE_BASE = 4
IMPORT_P_CALL = 5
IMPORT_AUDIO_INPUT = 2
IMPORT_AUDIO_INPUT_CURSOR = 3
IMPORT_MAIN_OUTPUT = 4
IMPORT_MAIN_OUTPUT_CURSOR = 5
IMPORT_TRACK_BUFFERS = 6
IMPORT_TEMP_X = 7
IMPORT_TEMP_Y = 8
IMPORT_SAMPLE_INFO = 9
IMPORT_SAMPLE_READ = 10
IMPORT_RAM_WORKSPACE = 11
IMPORT_RAM_RECORD = 12
# (kind, symbol): resource size in DSP words; a service call operand must be zero.
IMPORT_RESOURCES = {
    (IMPORT_X_READ_BASE, IMPORT_SINE_TABLE): 0x8000,
    (IMPORT_X_READ_BASE, IMPORT_AUDIO_INPUT): 0x100,
    (IMPORT_X_READ_BASE, IMPORT_AUDIO_INPUT_CURSOR): 1,
    (IMPORT_X_READ_BASE, IMPORT_MAIN_OUTPUT): 0x100,
    (IMPORT_X_READ_BASE, IMPORT_MAIN_OUTPUT_CURSOR): 1,
    (IMPORT_XY_WRITE_BASE, IMPORT_TRACK_BUFFERS): 0x600,
    (IMPORT_X_WRITE_BASE, IMPORT_TEMP_X): 32,
    (IMPORT_Y_WRITE_BASE, IMPORT_TEMP_Y): 32,
    (IMPORT_P_CALL, IMPORT_SAMPLE_INFO): 1,
    (IMPORT_P_CALL, IMPORT_SAMPLE_READ): 1,
    (IMPORT_P_CALL, IMPORT_RAM_WORKSPACE): 1,
    (IMPORT_P_CALL, IMPORT_RAM_RECORD): 1,
}


def check_import(kind: int, symbol: int, offset: int) -> None:
    words = IMPORT_RESOURCES.get((kind, symbol))
    if words is None:
        raise FormatError("unsupported resource or service import")
    if not 0 <= offset < words:
        raise FormatError("import offset is outside its memory resource")


MANIFEST_TONAL = 0x01
MANIFEST_CONTINUOUS_RETRIGGER = 0x04
MANIFEST_NEIGHBOUR = 0x08
MANIFEST_STEREO_PAIR = 0x10
MANIFEST_FLAGS_MASK = MANIFEST_TONAL | MANIFEST_NEIGHBOUR | MANIFEST_CONTINUOUS_RETRIGGER | MANIFEST_STEREO_PAIR
RETRIGGER_MODES = ("restart", "continuous")
PARAMETER_LINK_MASK = 0x77

PARAMETER_TRANSFORMS = {
    "raw14": 0,
    "unipolar_q23": 1,
    "unipolar_square_q23": 2,
    "bipolar_q23": 3,
    "pitch": 4,
    "boolean": 5,
    "index": 6,
    "decay": 7,
}
PARAMETER_CONTROLS = {
    "continuous": 0,
    "bipolar": 1,
    "percent": 2,
    "switch": 3,
    "rotary": 4,
    "pitch": 5,
}
PARAMETER_TRANSFORM_NAMES = {
    value: name for name, value in PARAMETER_TRANSFORMS.items()
}
PARAMETER_CONTROL_NAMES = {
    value: name for name, value in PARAMETER_CONTROLS.items()
}


class FormatError(ValueError):
    """Raised when input is not the one canonical v1 encoding."""


def _align4(value: int) -> int:
    return (value + 3) & ~3


def _crc32(data: bytes | bytearray | memoryview) -> int:
    return zlib.crc32(data) & 0xFFFF_FFFF


def _u16(data: bytes | bytearray | memoryview, offset: int) -> int:
    return struct.unpack_from(">H", data, offset)[0]


def _u32(data: bytes | bytearray | memoryview, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def _put_u16(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into(">H", data, offset, value)


def _put_u32(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into(">I", data, offset, value)


def _padded_ascii(value: str, size: int, field: str, *, uppercase: bool = False) -> bytes:
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise FormatError(f"{field} must be ASCII") from exc
    if not encoded or len(encoded) > size or any(byte < 0x20 or byte > 0x7E for byte in encoded):
        raise FormatError(f"{field} must contain 1..{size} printable ASCII bytes")
    if uppercase and any(0x61 <= byte <= 0x7A for byte in encoded):
        raise FormatError(f"{field} must be uppercase")
    return encoded.ljust(size, b"\0")


def _decode_padded_ascii(data: bytes, field: str, *, uppercase: bool = False) -> str:
    head, separator, tail = data.partition(b"\0")
    if separator and any(tail):
        raise FormatError(f"{field} has nonzero bytes after its terminator")
    try:
        value = head.decode("ascii")
    except UnicodeDecodeError as exc:
        raise FormatError(f"{field} is not ASCII") from exc
    canonical = _padded_ascii(value, len(data), field, uppercase=uppercase)
    if canonical != data:
        raise FormatError(f"{field} is not canonically padded")
    return value


def _machine_id(value: str) -> bytes:
    if not value or len(value) > 32 or not value[0].isalnum():
        raise FormatError("manifest id must be 1..32 bytes and start with a letter or digit")
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789.-_" for character in value):
        raise FormatError("manifest id uses characters outside [a-z0-9._-]")
    return value.encode("ascii").ljust(32, b"\0")


def _parameter_semantics(
    transform: str, control: str, minimum: int, maximum: int
) -> int:
    try:
        transform_id = PARAMETER_TRANSFORMS[transform]
    except KeyError as exc:
        raise FormatError(f"unknown MDS parameter transform {transform!r}") from exc
    try:
        control_id = PARAMETER_CONTROLS[control]
    except KeyError as exc:
        raise FormatError(f"unknown MDS parameter control {control!r}") from exc
    if minimum >= maximum:
        raise FormatError("MDS parameter range must contain at least two values")
    valid_control = {
        "raw14": {"continuous", "bipolar", "percent"},
        "unipolar_q23": {"continuous", "percent"},
        "unipolar_square_q23": {"continuous", "percent"},
        "bipolar_q23": {"bipolar"},
        "pitch": {"pitch"},
        "boolean": {"switch"},
        "index": {"rotary"},
        "decay": {"continuous"},
    }[transform]
    if control not in valid_control:
        raise FormatError(
            f"control {control!r} is not valid for transform {transform!r}"
        )
    if transform == "pitch" and (minimum != 0 or maximum != 127):
        raise FormatError("MDS pitch parameters must use range 0..127")
    if transform == "boolean" and (minimum != 0 or maximum != 1):
        raise FormatError("MDS boolean must use range 0..1")
    return (control_id << 4) | transform_id


DEFAULT_CONTROLS = {
    "raw14": "continuous", "unipolar_q23": "percent",
    "unipolar_square_q23": "percent", "bipolar_q23": "bipolar",
    "pitch": "pitch", "boolean": "switch", "index": "rotary",
    "decay": "continuous",
}


def normalize_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Expand authoring defaults without changing the supplied document."""
    if not isinstance(manifest, Mapping):
        raise FormatError("machine definition must be an object")
    result = dict(manifest)
    for key, value in (("schema", "spsx.mds.machine/1"), ("abi_version", 1),
                       ("max_track_cycles", 0), ("machine_version", "0.0"),
                       ("category_order", 0),
                       ("tonal", False), ("neighbour", False), ("stereo_pair", False),
                       ("retrigger", "restart"), ("params", [])):
        result.setdefault(key, value)
    if not isinstance(result["params"], list):
        raise FormatError("manifest params must be a list")
    params = []
    for parameter in result["params"]:
        if not isinstance(parameter, Mapping):
            raise FormatError("parameter must be an object")
        p = dict(parameter)
        transform = p.get("transform")
        if not isinstance(transform, str) or transform not in DEFAULT_CONTROLS:
            raise FormatError(f"unknown MDS parameter transform {transform!r}")
        p.setdefault("default", 0)
        p.setdefault("min", 0)
        p.setdefault("max", 1 if transform == "boolean" else 127)
        p.setdefault("control", DEFAULT_CONTROLS.get(transform, ""))
        params.append(p)
    result["params"] = params
    return result


def build_manifest(manifest: Mapping[str, Any]) -> bytes:
    manifest = normalize_manifest(manifest)
    if manifest.get("schema") != "spsx.mds.machine/1":
        raise FormatError("manifest schema must be spsx.mds.machine/1")
    machine_id = str(manifest.get("id", ""))
    display_name = str(manifest.get("display_name", ""))
    short_label = str(manifest.get("short_label", ""))
    category = str(manifest.get("category", "USR"))
    if not re.fullmatch(r"(?:[A-Z0-9]{1,3}|P-I)", category):
        raise FormatError("category must be 1..3 uppercase letters/digits, or P-I")
    if category in {"MID", "CTR", "CTL"}:
        raise FormatError("MIDI/control categories are reserved")
    if not re.fullmatch(r"[A-Z0-9]{2}", short_label):
        raise FormatError("short_label must be exactly two uppercase letters/digits")
    tonal = manifest.get("tonal")
    if not isinstance(tonal, bool):
        raise FormatError("manifest tonal must be true or false")
    neighbour = manifest.get("neighbour", False)
    if not isinstance(neighbour, bool):
        raise FormatError("manifest neighbour must be true or false")
    retrigger = manifest.get("retrigger", "restart")
    if retrigger not in RETRIGGER_MODES:
        raise FormatError("manifest retrigger must be \"restart\" or \"continuous\"")
    parameters = manifest.get("params", [])
    if not isinstance(parameters, list) or len(parameters) > MAX_PARAMETERS:
        raise FormatError("manifest params must be a list of at most 8 entries")
    result = bytearray(MANIFEST_BYTES)
    result[0:4] = b"MDM1"
    result[4] = 1
    result[5] = len(parameters)
    stereo_pair = manifest.get("stereo_pair", False)
    if not isinstance(stereo_pair, bool) or (stereo_pair and not neighbour):
        raise FormatError("stereo_pair must be boolean and requires neighbour")
    result[6] = ((MANIFEST_STEREO_PAIR if stereo_pair else 0) | (MANIFEST_TONAL if tonal else 0) | (MANIFEST_NEIGHBOUR if neighbour else 0) |
                 (MANIFEST_CONTINUOUS_RETRIGGER if retrigger == "continuous" else 0))
    result[8:12] = _padded_ascii(category, 4, "category", uppercase=True)
    result[12:16] = _padded_ascii(short_label, 4, "short_label", uppercase=True)
    category_order = manifest["category_order"]
    if type(category_order) is not int or not 0 <= category_order <= 254:
        raise FormatError("category_order must be an integer in 0..254")
    result[15] = category_order
    result[16:32] = _padded_ascii(display_name, 16, "display_name")
    result[32:64] = _machine_id(machine_id)
    for index, parameter in enumerate(parameters):
        if not isinstance(parameter, Mapping):
            raise FormatError(f"parameter {index} is not an object")
        offset = 64 + index * 8
        name = str(parameter.get("name", ""))
        default = int(parameter.get("default", 0))
        minimum = int(parameter.get("min", 0))
        maximum = int(parameter.get("max", 127))
        transform = str(parameter.get("transform", ""))
        control = str(parameter.get("control", ""))
        linked = parameter.get("linked", False)
        if not isinstance(linked, bool):
            raise FormatError(f"parameter {index} linked must be true or false")
        if linked:
            if not (PARAMETER_LINK_MASK & (1 << index)):
                raise FormatError(
                    f"parameter {index} cannot link across a four-parameter row"
                )
            if index + 1 >= len(parameters):
                raise FormatError(
                    f"parameter {index} cannot link without a following parameter"
                )
            result[7] |= 1 << index
        if not (0 <= minimum <= default <= maximum <= 127):
            raise FormatError(f"parameter {index} requires 0 <= min <= default <= max <= 127")
        semantics = _parameter_semantics(transform, control, minimum, maximum)
        result[offset : offset + 4] = _padded_ascii(
            name, 4, f"parameter {index} name", uppercase=True
        )
        result[offset + 4 : offset + 8] = bytes((default, minimum, maximum, semantics))
    if tonal and not any(
        parameter.get("transform") == "pitch" for parameter in parameters
    ):
        raise FormatError("a tonal MDS machine requires a pitch parameter")
    for index, parameter in enumerate(parameters):
        mappings = parameter.get("value_text", [])
        if not isinstance(mappings, list):
            raise FormatError("value_text must be a list")
        previous = -1
        for mapping in mappings:
            if not isinstance(mapping, Mapping) or set(mapping) not in (
                {"value", "text"}, {"min", "max", "text"}
            ):
                raise FormatError("value_text requires value/text or min/max/text")
            lo = mapping.get("value", mapping.get("min"))
            hi = mapping.get("value", mapping.get("max"))
            if (type(lo) is not int or type(hi) is not int or
                    not parameter.get("min", 0) <= lo <= hi <= parameter.get("max", 127)
                    or lo <= previous):
                raise FormatError("value_text ranges must be ordered, disjoint and within parameter bounds")
            label = mapping["text"]
            if (not isinstance(label, str) or not 1 <= len(label) <= 4 or
                    any(not 0x21 <= ord(c) <= 0x7e for c in label)):
                raise FormatError("value_text text must be 1..4 visible ASCII characters")
            result.extend(bytes((index, lo, hi, 0)) + label.encode("ascii").ljust(4, b"\0"))
            previous = hi
    if len(result) > MANIFEST_MAX_BYTES:
        raise FormatError("at most 32 value_text entries are allowed per machine")
    return bytes(result)


def parse_manifest(data: bytes) -> dict[str, Any]:
    if (not MANIFEST_BYTES <= len(data) <= MANIFEST_MAX_BYTES or
            (len(data) - MANIFEST_BYTES) % VALUE_TEXT_ENTRY_BYTES) or data[:4] != b"MDM1" or data[4] != 1:
        raise FormatError("invalid MDM1 header")
    count = data[5]
    flags = data[6]
    parameter_links = data[7]
    if (count > MAX_PARAMETERS or
            flags & ~MANIFEST_FLAGS_MASK or
            (flags & MANIFEST_STEREO_PAIR and not flags & MANIFEST_NEIGHBOUR) or
            parameter_links & ~PARAMETER_LINK_MASK):
        raise FormatError("invalid MDM1 count or flags")
    if any(
        parameter_links & (1 << index) and index + 1 >= count
        for index in range(MAX_PARAMETERS)
    ):
        raise FormatError("MDM1 parameter link has no following parameter")
    machine_id = _decode_padded_ascii(data[32:64], "manifest id")
    if _machine_id(machine_id) != data[32:64]:
        raise FormatError("invalid manifest id")
    parameters: list[dict[str, Any]] = []
    for index in range(MAX_PARAMETERS):
        descriptor = data[64 + index * 8 : 72 + index * 8]
        if index >= count:
            if any(descriptor):
                raise FormatError("unused parameter descriptor is not zero")
            continue
        name = _decode_padded_ascii(descriptor[:4], f"parameter {index} name", uppercase=True)
        default, minimum, maximum, semantics = descriptor[4:8]
        if not 0 <= minimum <= default <= maximum <= 127:
            raise FormatError(f"parameter {index} has invalid bounds")
        transform_id = semantics & 0x0F
        control_id = semantics >> 4
        if transform_id not in PARAMETER_TRANSFORM_NAMES or control_id not in PARAMETER_CONTROL_NAMES:
            raise FormatError(f"parameter {index} has unknown semantics")
        transform = PARAMETER_TRANSFORM_NAMES[transform_id]
        control = PARAMETER_CONTROL_NAMES[control_id]
        if _parameter_semantics(
            transform, control, minimum, maximum
        ) != semantics:
            raise FormatError(f"parameter {index} has noncanonical semantics")
        parameters.append(
            {
                "name": name,
                "default": default,
                "min": minimum,
                "max": maximum,
                "transform": transform,
                "control": control,
                "linked": bool(parameter_links & (1 << index)),
            }
        )
    previous_parameter, previous_end = -1, -1
    for offset in range(MANIFEST_BYTES, len(data), VALUE_TEXT_ENTRY_BYTES):
        parameter, lo, hi, reserved = data[offset:offset + 4]
        if parameter >= count or parameter < previous_parameter or reserved:
            raise FormatError("invalid value_text parameter/order/reserved byte")
        descriptor = parameters[parameter]
        if (not descriptor["min"] <= lo <= hi <= descriptor["max"] or
                (parameter == previous_parameter and lo <= previous_end)):
            raise FormatError("invalid or overlapping value_text range")
        raw = data[offset + 4:offset + 8]
        label = raw.rstrip(b"\0")
        if not label or any(not 0x21 <= c <= 0x7e for c in label):
            raise FormatError("invalid value_text label")
        mapping = {"value": lo} if lo == hi else {"min": lo, "max": hi}
        mapping["text"] = label.decode("ascii")
        descriptor.setdefault("value_text", []).append(mapping)
        previous_parameter, previous_end = parameter, hi
    tonal = bool(flags & MANIFEST_TONAL)
    if tonal and not any(
        parameter["transform"] == "pitch" for parameter in parameters
    ):
        raise FormatError("a tonal MDS machine requires a pitch parameter")
    if data[11] != 0 or data[12] == 0:
        raise FormatError("invalid category or two-character machine name")
    category = _decode_padded_ascii(data[8:12], "category", uppercase=True)
    label = _decode_padded_ascii(data[12:15], "short_label", uppercase=True)
    category_order = data[15]
    if category_order == 255:
        raise FormatError("category_order 255 is reserved")
    if not re.fullmatch(r"(?:[A-Z0-9]{1,3}|P-I)", category) or not re.fullmatch(r"[A-Z0-9]{2}", label):
        raise FormatError("invalid category or two-character machine name")
    if category in {"MID", "CTR", "CTL"}:
        raise FormatError("MIDI/control categories are reserved")
    return {
        "schema": "spsx.mds.machine/1",
        "id": machine_id,
        "category": category,
        "category_order": category_order,
        "short_label": label,
        "display_name": _decode_padded_ascii(data[16:32], "display_name"),
        "tonal": tonal,
        "neighbour": bool(flags & MANIFEST_NEIGHBOUR),
        "stereo_pair": bool(flags & MANIFEST_STEREO_PAIR),
        "retrigger": "continuous" if flags & MANIFEST_CONTINUOUS_RETRIGGER else "restart",
        "params": parameters,
    }


@dataclass(frozen=True)
class Import:
    patch_word: int
    kind: int
    symbol: int


@dataclass(frozen=True)
class PackageParts:
    program: bytes
    manifest: bytes
    bitmap: bytes
    relocations: tuple[int, ...]
    imports: tuple[Import, ...]
    init_word: int
    mutate_word: int
    execute_word: int
    abi_version: int
    x_workset_words: int
    y_workset_words: int
    machine_version: int = 0
    max_track_cycles: int = 0


def _machine_version(value: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})", value):
        raise FormatError("machine_version must be major.minor, for example 1.2")
    major, minor = map(int, value.split('.'))
    if major > 255 or minor > 255:
        raise FormatError("machine_version components must be 0..255")
    return (major << 8) | minor


def _canonical_relocations(words: int, values: Iterable[int]) -> tuple[int, ...]:
    relocations = tuple(sorted(int(value) for value in values))
    if len(set(relocations)) != len(relocations) or any(value < 0 or value >= words for value in relocations):
        raise FormatError("relocation sites must be unique program-word offsets")
    return relocations


def _canonical_imports(words: int, values: Iterable[Mapping[str, Any] | Import]) -> tuple[Import, ...]:
    imports = tuple(
        value
        if isinstance(value, Import)
        else Import(int(value["patch_word"]), int(value["kind"]), int(value["symbol"]))
        for value in values
    )
    if len(imports) > MAX_IMPORTS:
        raise FormatError("package has more than 64 imports")
    if tuple(sorted(imports, key=lambda item: item.patch_word)) != imports:
        raise FormatError("imports must be ordered by patch_word")
    previous = -1
    for item in imports:
        if not 0 <= item.patch_word < words or item.patch_word <= previous:
            raise FormatError("import patch words must be unique and strictly increasing")
        if not 0 <= item.kind <= 255 or not 0 <= item.symbol <= 255:
            raise FormatError("import kind and symbol must fit one byte")
        if (item.kind, item.symbol) not in IMPORT_RESOURCES:
            raise FormatError("import is outside the MDS ABI-v1 allowlist")
        previous = item.patch_word
    return imports


def canonical_machine_bitmap(bitmap: bytes | bytearray | memoryview | None) -> bytes:
    encoded = bytes(MACHINE_BITMAP_BYTES) if bitmap is None else bytes(bitmap)
    if len(encoded) != MACHINE_BITMAP_BYTES:
        raise FormatError(
            f"machine bitmap must contain exactly {MACHINE_BITMAP_BYTES} bytes"
        )
    if any(encoded[offset] for offset in range(3, len(encoded), 4)):
        raise FormatError("machine bitmap has nonzero pixels below row 23")
    return encoded


def encode_machine_bitmap(rows: Sequence[str]) -> bytes:
    if len(rows) != MACHINE_BITMAP_HEIGHT or any(
        len(row) != MACHINE_BITMAP_WIDTH or any(pixel not in "01" for pixel in row)
        for row in rows
    ):
        raise FormatError("machine bitmap must be 24 rows of 32 binary pixels")
    encoded = bytearray(MACHINE_BITMAP_BYTES)
    for y, row in enumerate(rows):
        for x, pixel in enumerate(row):
            if pixel == "1":
                word = int.from_bytes(encoded[x * 4 : x * 4 + 4], "big")
                word |= 1 << (8 + y)
                encoded[x * 4 : x * 4 + 4] = word.to_bytes(4, "big")
    return bytes(encoded)


def decode_machine_bitmap_asset(data: bytes) -> bytes:
    if len(data) == MACHINE_BITMAP_BYTES:
        return canonical_machine_bitmap(data)
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise FormatError(
            "machine bitmap must be a 128-byte native image or ASCII P1 PBM"
        ) from exc
    tokens: list[str] = []
    for line in text.splitlines():
        tokens.extend(line.split("#", 1)[0].split())
    if tokens[:3] != [
        "P1", str(MACHINE_BITMAP_WIDTH), str(MACHINE_BITMAP_HEIGHT)
    ]:
        raise FormatError("machine bitmap PBM must be ASCII P1 32x24")
    pixels = tokens[3:]
    if len(pixels) != MACHINE_BITMAP_WIDTH * MACHINE_BITMAP_HEIGHT or any(
        pixel not in ("0", "1") for pixel in pixels
    ):
        raise FormatError("machine bitmap PBM has invalid pixel data")
    return encode_machine_bitmap(
        tuple(
            "".join(pixels[offset : offset + MACHINE_BITMAP_WIDTH])
            for offset in range(0, len(pixels), MACHINE_BITMAP_WIDTH)
        )
    )


def package_parts(
    program: bytes,
    manifest: Mapping[str, Any] | bytes,
    link: Mapping[str, Any],
    bitmap: bytes | bytearray | memoryview | None = None,
) -> PackageParts:
    if isinstance(manifest, Mapping):
        manifest = normalize_manifest(manifest)
    if not program or len(program) % 3:
        raise FormatError("program must contain packed 24-bit words")
    words = len(program) // 3
    if words > PROGRAM_MAX_WORDS:
        raise FormatError(f"program exceeds {PROGRAM_MAX_WORDS:,} words")
    encoded_manifest = build_manifest(manifest) if isinstance(manifest, Mapping) else bytes(manifest)
    parse_manifest(encoded_manifest)
    relocations = _canonical_relocations(words, link.get("relocations", []))
    imports = _canonical_imports(words, link.get("imports", []))
    for item in imports:
        offset = int.from_bytes(program[item.patch_word * 3:item.patch_word * 3 + 3], "big", signed=True)
        check_import(item.kind, item.symbol, offset)
    if set(relocations).intersection(item.patch_word for item in imports):
        raise FormatError("a word cannot be both a relocation and an import")
    init_word = int(link["init_word"])
    mutate_word = int(link["mutate_word"])
    execute_word = int(link["execute_word"])
    if any(value < 0 or value >= words for value in (init_word, mutate_word, execute_word)):
        raise FormatError("INIT, TRIG and RENDER must be program-word offsets")
    abi_version = int(link.get("abi_version", 1))
    x_words = int(link.get("x_workset_words", 0))
    y_words = int(link.get("y_workset_words", 0))
    if parse_manifest(encoded_manifest)["stereo_pair"] and y_words < 10:
        raise FormatError("stereo_pair requires Y workset words 0..9")
    if abi_version != MACHINE_ABI_VERSION:
        raise FormatError("machine ABI version must be 1")
    if not 0 <= x_words <= WORKSET_WORDS or not 0 <= y_words <= WORKSET_WORDS:
        raise FormatError("X/Y workset use must be between 0 and 64 words")
    return PackageParts(
        bytes(program), encoded_manifest, canonical_machine_bitmap(bitmap),
        relocations, imports,
        init_word, mutate_word, execute_word, abi_version, x_words, y_words,
        _machine_version(manifest.get("machine_version", "0.0")) if isinstance(manifest, Mapping) else 0,
        manifest["max_track_cycles"] if isinstance(manifest, Mapping) else 0,
    )


def _relocation_bitmap(words: int, relocations: Sequence[int]) -> bytes:
    bitmap = bytearray((words + 7) // 8)
    for word in relocations:
        bitmap[word // 8] |= 1 << (word & 7)
    return bytes(bitmap)


def _package_digest(package: bytes | bytearray, program: bytes) -> bytes:
    canonical = bytearray(package)
    canonical[84:120] = b"\0" * 36
    return hashlib.sha256(canonical + program).digest()


def build_package(parts: PackageParts) -> bytes:
    words = len(parts.program) // 3
    if type(parts.machine_version) is not int or not 0 <= parts.machine_version <= 0xffff:
        raise FormatError("machine_version must fit two bytes")
    if type(parts.max_track_cycles) is not int or not 0 <= parts.max_track_cycles <= 0xffff:
        raise FormatError("max_track_cycles must fit two bytes")
    relocations = _relocation_bitmap(words, parts.relocations)
    imports = b"".join(
        struct.pack(">HBB", item.patch_word, item.kind, item.symbol)
        for item in parts.imports
    )
    code_offset = PACKAGE_HEADER_BYTES
    relocation_offset = _align4(code_offset + len(parts.program))
    imports_offset = _align4(relocation_offset + len(relocations))
    manifest_offset = _align4(imports_offset + len(imports))
    bitmap_offset = _align4(manifest_offset + len(parts.manifest))
    package_bytes = _align4(bitmap_offset + len(parts.bitmap))
    if package_bytes > PACKAGE_MAX_BYTES:
        raise FormatError(f"package exceeds the {PACKAGE_MAX_BYTES:,}-byte canonical limit")
    package = bytearray(package_bytes)
    package[0:4] = b"MDS1"
    _put_u16(package, 4, 1)
    _put_u16(package, 6, PACKAGE_HEADER_BYTES)
    _put_u32(package, 8, package_bytes)
    _put_u16(package, 16, TARGET_DSP56303)
    _put_u16(package, 18, parts.abi_version)
    package[20] = CODEC_RAW24
    _put_u16(package, 22, words)
    _put_u16(package, 24, len(parts.imports))
    _put_u16(package, 26, parts.x_workset_words)
    _put_u16(package, 28, parts.y_workset_words)
    _put_u16(package, 30, parts.machine_version)
    _put_u32(package, 32, code_offset)
    _put_u32(package, 36, len(parts.program))
    _put_u32(package, 40, len(parts.program))
    _put_u32(package, 44, relocation_offset)
    _put_u32(package, 48, imports_offset)
    _put_u16(package, 52, len(parts.manifest))
    _put_u32(package, 56, manifest_offset)
    _put_u16(package, 64, parts.init_word)
    _put_u16(package, 66, parts.mutate_word)
    _put_u16(package, 68, parts.execute_word)
    _put_u16(package, 70, parts.max_track_cycles)
    _put_u32(package, 76, _crc32(parts.program))
    _put_u32(package, 80, _crc32(parts.manifest))
    package[code_offset : code_offset + len(parts.program)] = parts.program
    package[relocation_offset : relocation_offset + len(relocations)] = relocations
    package[imports_offset : imports_offset + len(imports)] = imports
    package[manifest_offset : manifest_offset + len(parts.manifest)] = parts.manifest
    package[bitmap_offset : bitmap_offset + len(parts.bitmap)] = parts.bitmap
    package[88:120] = _package_digest(package, parts.program)
    _put_u32(package, 84, _crc32(package))
    return bytes(package)


def parse_package(data: bytes) -> dict[str, Any]:
    if len(data) < PACKAGE_HEADER_BYTES or data[:4] != b"MDS1":
        raise FormatError("invalid MDS1 header")
    if _u16(data, 4) != 1 or _u16(data, 6) != PACKAGE_HEADER_BYTES:
        raise FormatError("unsupported MDS1 version or header size")
    package_bytes = _u32(data, 8)
    if package_bytes != len(data) or package_bytes > PACKAGE_MAX_BYTES:
        raise FormatError("MDS1 complete-byte field is invalid")
    if _u32(data, 12):
        raise FormatError("MDS1 flags are not zero")
    if _u16(data, 16) != TARGET_DSP56303 or data[20] != CODEC_RAW24:
        raise FormatError("unsupported architecture or codec")
    if data[21] or _u16(data, 54) or _u32(data, 60) or _u32(data, 72) or any(data[120:128]):
        raise FormatError("MDS1 reserved fields are not zero")
    words = _u16(data, 22)
    imports_count = _u16(data, 24)
    if not 1 <= words <= PROGRAM_MAX_WORDS or imports_count > MAX_IMPORTS:
        raise FormatError("program/import count exceeds v1 limits")
    if _u16(data, 18) != MACHINE_ABI_VERSION:
        raise FormatError("unsupported MDS1 machine ABI")
    if _u16(data, 26) > WORKSET_WORDS or _u16(data, 28) > WORKSET_WORDS:
        raise FormatError("MDS1 workset use exceeds the physical track workset")
    code_offset, code_bytes, decoded_bytes = (_u32(data, offset) for offset in (32, 36, 40))
    relocation_offset = _u32(data, 44)
    imports_offset = _u32(data, 48)
    manifest_bytes = _u16(data, 52)
    manifest_offset = _u32(data, 56)
    if code_offset != PACKAGE_HEADER_BYTES or code_bytes != words * 3 or decoded_bytes != code_bytes:
        raise FormatError("invalid raw program section")
    relocation_bytes = (words + 7) // 8
    imports_bytes = imports_count * 4
    expected_relocation = _align4(code_offset + code_bytes)
    expected_imports = _align4(expected_relocation + relocation_bytes)
    expected_manifest = _align4(expected_imports + imports_bytes)
    if ((relocation_offset, imports_offset, manifest_offset) != (
        expected_relocation, expected_imports, expected_manifest
    ) or not MANIFEST_BYTES <= manifest_bytes <= MANIFEST_MAX_BYTES or
            (manifest_bytes - MANIFEST_BYTES) % VALUE_TEXT_ENTRY_BYTES):
        raise FormatError("noncanonical MDS1 section geometry")
    bitmap_offset = _align4(manifest_offset + manifest_bytes)
    if _align4(bitmap_offset + MACHINE_BITMAP_BYTES) != package_bytes:
        raise FormatError("package has trailing or missing bytes")
    canonical_crc = bytearray(data)
    canonical_crc[84:88] = b"\0" * 4
    if _crc32(canonical_crc) != _u32(data, 84):
        raise FormatError("package CRC mismatch")
    program = data[code_offset : code_offset + code_bytes]
    manifest_data = data[manifest_offset : manifest_offset + manifest_bytes]
    if parse_manifest(manifest_data)["stereo_pair"] and _u16(data, 28) < 10:
        raise FormatError("stereo_pair requires Y workset words 0..9")
    bitmap_data = data[bitmap_offset : bitmap_offset + MACHINE_BITMAP_BYTES]
    if _crc32(program) != _u32(data, 76) or _crc32(manifest_data) != _u32(data, 80):
        raise FormatError("program or manifest CRC mismatch")
    for begin, end in (
        (code_offset + code_bytes, relocation_offset),
        (relocation_offset + relocation_bytes, imports_offset),
        (imports_offset + imports_bytes, manifest_offset),
        (bitmap_offset + MACHINE_BITMAP_BYTES, package_bytes),
    ):
        if any(data[begin:end]):
            raise FormatError("section alignment padding is not zero")
    bitmap = data[relocation_offset : relocation_offset + relocation_bytes]
    if words & 7 and bitmap[-1] & ~((1 << (words & 7)) - 1):
        raise FormatError("relocation bitmap has nonzero trailing bits")
    relocations = tuple(
        word for word in range(words) if bitmap[word // 8] & (1 << (word & 7))
    )
    canonical_machine_bitmap(bitmap_data)
    imports: list[Import] = []
    previous = -1
    for index in range(imports_count):
        patch, kind, symbol = struct.unpack_from(">HBB", data, imports_offset + index * 4)
        if patch >= words or patch <= previous or patch in relocations:
            raise FormatError("invalid or overlapping import site")
        imports.append(Import(patch, kind, symbol))
        check_import(kind, symbol, int.from_bytes(program[patch * 3:patch * 3 + 3], "big", signed=True))
        previous = patch
    for entry_offset in (64, 66, 68):
        if _u16(data, entry_offset) >= words:
            raise FormatError("entry point lies outside the program")
    digest = _package_digest(data, program)
    if digest != data[88:120]:
        raise FormatError("package content digest mismatch")
    return {
        "package_bytes": package_bytes,
        "program_words": words,
        "abi_version": _u16(data, 18),
        "machine_version": f"{data[30]}.{data[31]}",
        "x_workset_words": _u16(data, 26),
        "y_workset_words": _u16(data, 28),
        "program": program,
        "manifest_bytes": manifest_data,
        "manifest": parse_manifest(manifest_data),
        "bitmap_offset": bitmap_offset,
        "bitmap": bitmap_data,
        "relocations": relocations,
        "imports": tuple(imports),
        "init_word": _u16(data, 64),
        "mutate_word": _u16(data, 66),
        "execute_word": _u16(data, 68),
        "max_track_cycles": _u16(data, 70),
        "package_crc32": _u32(data, 84),
        "content_digest": digest,
    }
