#!/usr/bin/env python3
"""Build a QuickTime Live Photo MOV: moov/meta mdta + mebx timed metadata track."""
from __future__ import annotations

import struct
from typing import List, Tuple


def u16(n: int) -> bytes:
    return struct.pack(">H", n & 0xFFFF)


def u32(n: int) -> bytes:
    return struct.pack(">I", n & 0xFFFFFFFF)


def i32(n: int) -> bytes:
    return struct.pack(">i", n)


def fourcc(s: str) -> bytes:
    b = s.encode("ascii")
    return (b + b"\x00\x00\x00\x00")[:4]


def box(typ: str, payload: bytes) -> bytes:
    return u32(8 + len(payload)) + fourcc(typ) + payload


def fullbox(typ: str, payload: bytes, version: int = 0, flags: int = 0) -> bytes:
    return box(typ, bytes([version, (flags >> 16) & 0xFF, (flags >> 8) & 0xFF, flags & 0xFF]) + payload)


def pascal_name(name: str) -> bytes:
    raw = name.encode("ascii")
    return bytes([len(raw)]) + raw


def parse_top_level(data: bytes) -> List[Tuple[int, int, str, bytes]]:
    boxes = []
    p = 0
    n = len(data)
    while p + 8 <= n:
        size = struct.unpack(">I", data[p : p + 4])[0]
        typ = data[p + 4 : p + 8].decode("latin1")
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", data[p + 8 : p + 16])[0]
            hdr = 16
        elif size == 0:
            size = n - p
        if size < hdr or p + size > n:
            break
        boxes.append((p, size, typ, data[p : p + size]))
        p += size
    return boxes


def iter_children(box_data: bytes, payload_off: int = 8) -> List[Tuple[str, bytes]]:
    children = []
    p = payload_off
    n = len(box_data)
    while p + 8 <= n:
        size = struct.unpack(">I", box_data[p : p + 4])[0]
        typ = box_data[p + 4 : p + 8].decode("latin1")
        if size < 8 or p + size > n:
            break
        children.append((typ, box_data[p : p + size]))
        p += size
    return children


def find_track_id(trak: bytes) -> int:
    for typ, raw in iter_children(trak):
        if typ == "tkhd" and len(raw) >= 32:
            ver = raw[8]
            off = 20 if ver == 0 else 28
            return struct.unpack(">I", raw[off : off + 4])[0]
    return 0


def read_mvhd(mvhd: bytes) -> Tuple[int, int, int]:
    ver = mvhd[8]
    if ver == 1:
        timescale = struct.unpack(">I", mvhd[28:32])[0]
        duration = struct.unpack(">Q", mvhd[32:40])[0]
        next_id = struct.unpack(">I", mvhd[-4:])[0]
    else:
        timescale = struct.unpack(">I", mvhd[20:24])[0]
        duration = struct.unpack(">I", mvhd[24:28])[0]
        next_id = struct.unpack(">I", mvhd[104:108])[0]
    return timescale, duration, next_id


def patch_mvhd_next_track_id(mvhd: bytes, next_id: int) -> bytes:
    b = bytearray(mvhd)
    ver = b[8]
    if ver == 1:
        b[-4:] = u32(next_id)
    else:
        b[104:108] = u32(next_id)
    return bytes(b)


def identity_matrix() -> bytes:
    return (
        u32(0x00010000) + u32(0) + u32(0)
        + u32(0) + u32(0x00010000) + u32(0)
        + u32(0) + u32(0) + u32(0x40000000)
    )


def build_tkhd(track_id: int, duration: int) -> bytes:
    payload = (
        u32(0)
        + u32(0)
        + u32(track_id)
        + u32(0)
        + u32(duration)
        + b"\x00" * 8
        + u16(0)
        + u16(0)
        + u16(0)
        + u16(0)
        + identity_matrix()
        + u32(0)
        + u32(0)
    )
    return fullbox("tkhd", payload, version=0, flags=0x00000F)


def build_mdhd(timescale: int, duration: int) -> bytes:
    payload = (
        u32(0)
        + u32(0)
        + u32(timescale)
        + u32(duration)
        + u16(0x55C4)
        + u16(0)
    )
    return fullbox("mdhd", payload)


def build_hdlr(component_type: str, subtype: str, manufacturer: str, name: str) -> bytes:
    payload = (
        fourcc(component_type)
        + fourcc(subtype)
        + fourcc(manufacturer)
        + u32(0)
        + u32(0)
        + pascal_name(name)
    )
    return fullbox("hdlr", payload)


def build_dinf() -> bytes:
    url = fullbox("url ", b"", version=0, flags=1)
    dref = fullbox("dref", u32(1) + url)
    return box("dinf", dref)


def build_gmhd() -> bytes:
    gmin = fullbox(
        "gmin",
        u16(0x0040) + u16(0x8000) + u16(0x8000) + u16(0x8000) + u16(0) + u16(0),
    )
    return box("gmhd", gmin)


def build_elst(movie_delta: int) -> bytes:
    payload = u32(1) + u32(max(1, movie_delta)) + i32(0) + u32(0x00010000)
    return box("edts", fullbox("elst", payload))


COVER_TIMESCALE = 600

# mebx keys: only still-image-time, dtyp=65 (int8s).
# Apple/ExifTool: this tag ALWAYS has value -1; the still time is SampleTime.
STILL_KEYS_PAYLOAD = bytes.fromhex(
    "0000004800000001000000306B6579646D647461636F6D2E6170706C652E717569636B74696D652E7374696C6C2D696D6167652D74696D65"
    "00000010647479700000000000000041"
)

# mebx sample item: size=9, local key=1, payload=int8s -1 (0xFF)
STILL_IMAGE_TIME_SAMPLE = bytes.fromhex("0000000900000001FF")


def build_mebx_stsd() -> bytes:
    keys_box = box("keys", STILL_KEYS_PAYLOAD)
    entry_payload = b"\x00" * 6 + u16(1) + keys_box
    entry = box("mebx", entry_payload)
    return fullbox("stsd", u32(1) + entry)


def build_stbl(sample_size: int, sample_offset: int) -> bytes:
    stsd = build_mebx_stsd()
    stts = fullbox("stts", u32(1) + u32(1) + u32(1))
    stsc = fullbox("stsc", u32(1) + u32(1) + u32(1) + u32(1))
    stsz = fullbox("stsz", u32(sample_size) + u32(1))
    stco = fullbox("stco", u32(1) + u32(sample_offset))
    return box("stbl", stsd + stts + stsc + stsz + stco)


def build_cover_trak(track_id: int, movie_timescale: int, movie_duration: int, sample_offset: int) -> bytes:
    movie_delta = max(1, round(movie_timescale / COVER_TIMESCALE))
    tkhd = build_tkhd(track_id, movie_duration)
    edts = build_elst(movie_delta)
    mdhd = build_mdhd(COVER_TIMESCALE, 1)
    hdlr = build_hdlr("mhlr", "meta", "appl", "Core Media Metadata")
    minf = box(
        "minf",
        build_gmhd()
        + build_hdlr("dhlr", "alis", "appl", "Core Media Data Handler")
        + build_dinf()
        + build_stbl(len(STILL_IMAGE_TIME_SAMPLE), sample_offset),
    )
    mdia = box("mdia", mdhd + hdlr + minf)
    return box("trak", tkhd + edts + mdia)


def build_qt_meta(asset_id: str) -> bytes:
    """QuickTime moov/meta (NOT FullBox, NOT under udta) with mdta content.identifier."""
    hdlr = build_hdlr("", "mdta", "", "")
    key_name = b"com.apple.quicktime.content.identifier"
    key_entry = u32(8 + len(key_name)) + b"mdta" + key_name
    keys_box = fullbox("keys", u32(1) + key_entry)
    data_payload = u32(1) + u32(0) + asset_id.encode("ascii")
    data_box = box("data", data_payload)
    item = u32(8 + len(data_box)) + bytes([0, 0, 0, 1]) + data_box
    ilst = box("ilst", item)
    return box("meta", hdlr + keys_box + ilst)


def patch_stco(stco: bytes, delta: int) -> bytes:
    b = bytearray(stco)
    count = struct.unpack(">I", b[12:16])[0]
    for i in range(count):
        off = 16 + i * 4
        val = struct.unpack(">I", b[off : off + 4])[0]
        b[off : off + 4] = u32(val + delta)
    return bytes(b)


def patch_co64(co64: bytes, delta: int) -> bytes:
    b = bytearray(co64)
    count = struct.unpack(">I", b[12:16])[0]
    for i in range(count):
        off = 16 + i * 8
        val = struct.unpack(">Q", b[off : off + 8])[0]
        b[off : off + 8] = struct.pack(">Q", val + delta)
    return bytes(b)


_OFFSET_CONTAINERS = {"trak", "mdia", "minf", "stbl"}


def patch_chunk_offsets(raw: bytes, delta: int) -> bytes:
    if delta == 0:
        return raw
    typ = raw[4:8].decode("latin1")
    if typ == "stco":
        return patch_stco(raw, delta)
    if typ == "co64":
        return patch_co64(raw, delta)
    if typ in _OFFSET_CONTAINERS:
        payload = b"".join(patch_chunk_offsets(child, delta) for _t, child in iter_children(raw))
        return u32(8 + len(payload)) + raw[4:8] + payload
    return raw


def rewrite_ftyp(raw: bytes) -> bytes:
    if len(raw) < 20 or raw[4:8] != b"ftyp":
        return raw
    b = bytearray(raw)
    b[8:12] = b"qt  "
    b[12:16] = b"\x00\x00\x00\x00"
    if len(b) >= 24:
        b[16:20] = b"qt  "
    return bytes(b)


def inject_live_photo_metadata(src_mov: bytes, asset_id: str) -> bytes:
    top = parse_top_level(src_mov)
    mdat_start = None
    mdat = None
    moov = None
    prefix_parts = []
    for start, size, typ, raw in top:
        if typ == "mdat" and mdat is None:
            mdat_start = start
            mdat = raw
        elif typ == "moov" and moov is None:
            moov = raw
        elif typ == "mdat":
            continue
        elif mdat is None and typ not in ("moov", "free", "skip"):
            prefix_parts.append(rewrite_ftyp(raw) if typ == "ftyp" else raw)

    if any(typ == "moof" for _s, _n, typ, _r in top):
        raise ValueError("fragmented MOV (moof) is not supported")
    if mdat is None or moov is None or mdat_start is None:
        raise ValueError("MOV missing mdat/moov")

    prefix = b"".join(prefix_parts)
    delta = len(prefix) - mdat_start
    head = prefix + mdat

    mvhd = None
    traks = []
    max_tid = 0
    for typ, raw in iter_children(moov):
        if typ == "mvhd":
            mvhd = raw
        elif typ == "trak":
            patched = patch_chunk_offsets(raw, delta)
            traks.append(patched)
            max_tid = max(max_tid, find_track_id(patched))

    if mvhd is None:
        raise ValueError("MOV missing mvhd")

    timescale, duration, _next = read_mvhd(mvhd)
    track_id = max(max_tid + 1, 1)
    mvhd = patch_mvhd_next_track_id(mvhd, track_id + 1)
    meta = build_qt_meta(asset_id)

    dummy = build_cover_trak(track_id, timescale, duration, 0)
    moov1 = box("moov", mvhd + b"".join(traks) + dummy + meta)
    sample_offset = len(head) + len(moov1) + 8
    cover = build_cover_trak(track_id, timescale, duration, sample_offset)
    if len(cover) != len(dummy):
        raise RuntimeError("cover trak size changed between passes")
    moov2 = box("moov", mvhd + b"".join(traks) + cover + meta)
    return head + moov2 + box("mdat", STILL_IMAGE_TIME_SAMPLE)


if __name__ == "__main__":
    import sys

    src, dst, aid = sys.argv[1], sys.argv[2], sys.argv[3]
    out = inject_live_photo_metadata(open(src, "rb").read(), aid)
    open(dst, "wb").write(out)
    print(f"wrote {dst} ({len(out)} bytes) asset_id={aid}")
