#!/usr/bin/env python3
"""Convert an MDS machine package to a MIDI Sysex file."""
import argparse
import hashlib
from pathlib import Path
import struct
import zlib


def convert(package: bytes, slot: int = 1) -> bytes:
    if type(slot) is not int or not 1 <= slot <= 16:
        raise ValueError("slot must be 1..16 (U01..U16)")
    if len(package) < 128 or package[:4] != b"MDS1":
        raise ValueError("not an MDS machine package")
    version, header, size = struct.unpack_from(">HHI", package, 4)
    if (version != 1 or header != 128 or size != len(package) or size % 4
            or package[12:16] != bytes(4) or package[16:22] != b"\x56\x03\x00\x01\x00\x00"
            or any(package[60:64] + package[72:76] + package[120:128])):
        raise ValueError("unsupported or invalid MDS header")
    if not int.from_bytes(package[70:72], "big"):
        raise ValueError("draft package: no installable cycle budget")
    offset, length, decoded = struct.unpack_from(">III", package, 32)
    if (offset != 128 or length != decoded or not length or
            length != 3 * int.from_bytes(package[22:24], "big") or offset + length > size):
        raise ValueError("invalid MDS program length")
    crc = int.from_bytes(package[84:88], "big")
    if zlib.crc32(package[:84] + bytes(4) + package[88:]) != crc:
        raise ValueError("MDS checksum mismatch")
    digest = hashlib.sha256(package[:84] + bytes(36) + package[120:] +
                            package[offset:offset + length]).digest()
    if digest != package[88:120]:
        raise ValueError("MDS content ID mismatch")
    count = (size + 419) // 420
    if count > 127:
        raise ValueError("MDS package is too large for this transfer format")

    def packet(command, sequence, body=b""):
        packed = bytearray()
        for at in range(0, len(body), 7):
            group = body[at:at + 7]
            packed.append(sum((value >> 7) << (6 - i) for i, value in enumerate(group)))
            packed.extend(value & 127 for value in group)
        checked = bytes((0, 1, slot - 1, sequence, 0)) + packed
        checksum, wire_length = sum(checked) & 0x3fff, len(checked) + 4
        return (bytes((0xf0, 0, 0x20, 0x3c, 2, 0, 0x74, 1, command)) + checked +
                bytes((checksum >> 7, checksum & 127, wire_length >> 7, wire_length & 127, 0xf7)))

    descriptor = struct.pack(">IBBI32s", size, count, 0, crc, digest)
    messages = [packet(1, 127, descriptor)]
    messages.extend(packet(2, sequence, package[sequence * 420:(sequence + 1) * 420])
                    for sequence in range(count))
    messages.append(packet(0x7b, count))
    return b"".join(messages)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="input .mds file")
    parser.add_argument("--slot", type=int, default=1, help="destination 1..16 (default: 1)")
    parser.add_argument("-o", "--output", type=Path, help="output .syx file (default: beside input)")
    args = parser.parse_args()
    output = args.output or args.package.with_suffix(".syx")
    try:
        if output.resolve() == args.package.resolve():
            raise ValueError("output must differ from the input package")
        sysex = convert(args.package.read_bytes(), args.slot)
        output.write_bytes(sysex)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"{output} (U{args.slot:02d})")


if __name__ == "__main__":
    main()
