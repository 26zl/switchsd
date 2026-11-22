"""Which homebrew ABI a program was built for.

Firmware 21 started writing to thread-local space that homebrew used, so programs built with a libnx
older than 4.10 may crash or corrupt memory there. Newer libnx marks its output with "LNY2" and an ABI
revision 0x34 bytes into the MOD0 header; hbmenu 3.6.1 reads the same field to show its warning.
"""

from __future__ import annotations

import struct
from pathlib import Path

MAX_NSP = 64 << 20  # sysmodule packages are a few MiB; anything far beyond is not read


def lz4_prefix(src: bytes, limit: int) -> bytes:
    """The first `limit` bytes (or a few more) of an LZ4 block."""
    out = bytearray()
    i = 0
    while i < len(src) and len(out) < limit:
        token = src[i]
        i += 1
        literals = token >> 4
        if literals == 15:
            while i < len(src):
                extra = src[i]
                i += 1
                literals += extra
                if extra != 255:
                    break
        out += src[i:i + literals]
        i += literals
        if i + 2 > len(src):
            break
        offset = src[i] | src[i + 1] << 8
        i += 2
        length = (token & 15) + 4
        if length == 19:
            while i < len(src):
                extra = src[i]
                i += 1
                length += extra
                if extra != 255:
                    break
        start = len(out) - offset
        if offset == 0 or start < 0:
            raise ValueError("corrupt LZ4 data")
        for k in range(min(length, limit - len(out))):  # byte by byte: a match may overlap what it is producing
            out.append(out[start + k])
    return bytes(out)


def image_revision(head: bytes) -> int | None:
    """ABI revision of a code image, given its first bytes: 0 for libnx without the marker, None if not libnx."""
    if len(head) < 8:
        return None
    mod = struct.unpack_from("<I", head, 4)[0]
    block = head[mod:mod + 0x3C]
    if len(block) < 0x3C or block[:4] != b"MOD0" or block[0x1C:0x20] != b"LNY0":
        return None
    if block[0x34:0x38] != b"LNY2":
        return 0
    return struct.unpack_from("<I", block, 0x38)[0]


def nro_revision(path: Path) -> int | None:
    """For an .nro or .ovl file."""
    with open(path, "rb") as f:
        head = f.read(0x20)
        if len(head) < 0x20 or head[0x10:0x14] != b"NRO0":
            return None
        mod = struct.unpack_from("<I", head, 4)[0]
        if mod > 0x10000:
            return None
        f.seek(0)
        return image_revision(f.read(mod + 0x3C))


def nsp_revision(path: Path) -> int | None:
    """For a sysmodule's exefs.nsp: a PFS0 archive whose `main` is an NSO, usually LZ4-compressed."""
    if path.stat().st_size > MAX_NSP:
        return None
    data = path.read_bytes()
    if len(data) < 0x10 or data[:4] != b"PFS0":
        return None
    count, names_size = struct.unpack_from("<II", data, 4)
    names = 0x10 + count * 0x18
    for n in range(count):
        offset, size, name_at = struct.unpack_from("<QQI", data, 0x10 + n * 0x18)
        name = data[names + name_at:names + names_size].split(b"\0", 1)[0]
        if name != b"main":
            continue
        nso = data[names + names_size + offset:names + names_size + offset + size]
        if len(nso) < 0x70 or nso[:4] != b"NSO0":
            return None
        flags = struct.unpack_from("<I", nso, 0xC)[0]
        text_at, _, text_size = struct.unpack_from("<III", nso, 0x10)
        packed = nso[text_at:text_at + struct.unpack_from("<I", nso, 0x60)[0]]
        if not flags & 1:
            return image_revision(packed[:text_size])
        try:
            start = lz4_prefix(packed, 8)
            if len(start) < 8:
                return None
            mod = struct.unpack_from("<I", start, 4)[0]
            if mod > 0x10000:
                return None
            return image_revision(lz4_prefix(packed, mod + 0x3C))
        except (ValueError, IndexError):
            return None
    return None


def revision(path: Path) -> int | None:
    """ABI revision of a program on a card; None for anything that is not a readable libnx program."""
    name = path.name.lower()
    try:
        if name.endswith((".nro", ".ovl")):
            return nro_revision(path)
        if name == "exefs.nsp":
            return nsp_revision(path)
    except (struct.error, ValueError, IndexError):  # a damaged or made-up file
        return None
    return None
