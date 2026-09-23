"""桌面品牌 ICO 契约：标题栏必须使用完整 favicon 标记，而不是透明洞简化版。"""
from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ICO = ROOT / "packaging" / "assets" / "BioDataAgent.ico"
GENERATOR = ROOT / "packaging" / "assets" / "make_ico.py"
EXPECTED_SHA256 = "245d4566db55a418d32f99cdd3dad05d259d530f82db40599d0b724ac36e6af4"


def _ico_payloads(raw: bytes) -> dict[tuple[int, int], bytes]:
    reserved, kind, count = struct.unpack_from("<HHH", raw, 0)
    assert (reserved, kind, count) == (0, 1, 8)
    out: dict[tuple[int, int], bytes] = {}
    for index in range(count):
        width, height, _colors, _reserved, planes, bits, size, offset = struct.unpack_from(
            "<BBBBHHII", raw, 6 + index * 16,
        )
        dims = (width or 256, height or 256)
        assert planes == 0 or planes == 1
        assert bits in (0, 32)
        out[dims] = raw[offset:offset + size]
    return out


def _decode_rgba_png(payload: bytes) -> tuple[int, int, bytes]:
    """只解本项目 256px ICO 内的非交错 RGBA8 PNG；纯 stdlib，避免给 CI 增 Pillow。"""
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    pos = 8
    width = height = 0
    compressed = bytearray()
    while pos < len(payload):
        length = struct.unpack_from(">I", payload, pos)[0]
        name = payload[pos + 4:pos + 8]
        data = payload[pos + 8:pos + 8 + length]
        pos += 12 + length
        if name == b"IHDR":
            width, height, depth, color_type, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
            assert (depth, color_type, compression, filtering, interlace) == (8, 6, 0, 0, 0)
        elif name == b"IDAT":
            compressed.extend(data)
        elif name == b"IEND":
            break

    raw = zlib.decompress(bytes(compressed))
    stride = width * 4
    rows: list[bytearray] = []

    def paeth(a: int, b: int, c: int) -> int:
        p = a + b - c
        pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
        return a if pa <= pb and pa <= pc else b if pb <= pc else c

    cursor = 0
    for _ in range(height):
        filter_type = raw[cursor]
        cursor += 1
        row = bytearray(raw[cursor:cursor + stride])
        cursor += stride
        prior = rows[-1] if rows else bytearray(stride)
        for i, value in enumerate(row):
            left = row[i - 4] if i >= 4 else 0
            up = prior[i]
            upper_left = prior[i - 4] if i >= 4 else 0
            if filter_type == 1:
                row[i] = (value + left) & 0xFF
            elif filter_type == 2:
                row[i] = (value + up) & 0xFF
            elif filter_type == 3:
                row[i] = (value + ((left + up) // 2)) & 0xFF
            elif filter_type == 4:
                row[i] = (value + paeth(left, up, upper_left)) & 0xFF
            else:
                assert filter_type == 0
        rows.append(row)
    return width, height, b"".join(rows)


def _pixel(rgba: bytes, width: int, x: int, y: int) -> tuple[int, int, int, int]:
    offset = (y * width + x) * 4
    return tuple(rgba[offset:offset + 4])  # type: ignore[return-value]


def test_brand_ico_is_locked_and_contains_all_windows_layers() -> None:
    raw = ICO.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == EXPECTED_SHA256
    assert set(_ico_payloads(raw)) == {
        (16, 16), (20, 20), (24, 24), (32, 32),
        (48, 48), (64, 64), (128, 128), (256, 256),
    }


def test_brand_icon_keeps_teal_inside_ring_and_composites_the_swoosh() -> None:
    payload = _ico_payloads(ICO.read_bytes())[(256, 256)]
    width, height, rgba = _decode_rgba_png(payload)
    assert (width, height) == (256, 256)
    assert _pixel(rgba, width, 0, 0)[3] == 0              # 圆角矩形外透明
    inner = _pixel(rgba, width, 128, 92)                  # 白环内部、避开轨道弧
    assert inner == (13, 148, 136, 255)                   # 透出 SVG rect 青底，不挖透明洞
    swoosh = _pixel(rgba, width, 128, 128)
    assert swoosh[3] == 255 and swoosh[:3] != inner[:3]   # 轨道弧已 source-over 合成、仍完全不透明


def test_generator_encodes_svg_fill_none_semantics_instead_of_transparent_hole() -> None:
    source = GENERATOR.read_text(encoding="utf-8")
    assert "fill=_TEAL" in source
    assert "return Image.alpha_composite(img, overlay)" in source
    assert "fill=(0, 0, 0, 0))\n\n    # ③" not in source
