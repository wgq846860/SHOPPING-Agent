"""启动器图标包装测试：ICO 容器结构（不依赖 Pillow）。"""

from __future__ import annotations

import struct

import pytest

from scripts.launcher.make_icon import wrap_png_as_ico

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _png(width: int, height: int, payload: bytes = b"payload") -> bytes:
    return _PNG_MAGIC + b"----" + struct.pack(">II", width, height) + payload


def test_wrap_png_as_ico_structure():
    png = _png(128, 128, b"abc")
    ico = wrap_png_as_ico(png, 128, 128)
    assert struct.unpack_from("<HHH", ico, 0) == (0, 1, 1)  # reserved/type/count
    entry = struct.unpack_from("<BBBBHHII", ico, 6)
    assert entry[0] == 128  # width
    assert entry[1] == 128  # height
    assert entry[2] == 0 and entry[3] == 0
    assert entry[4] == 1  # planes
    assert entry[5] == 32  # bpp
    assert entry[6] == len(png)
    assert entry[7] == 22  # 数据偏移 = 6 + 16
    assert ico[22:] == png


def test_wrap_png_as_ico_256_uses_zero_dimension():
    ico = wrap_png_as_ico(_png(256, 256), 256, 256)
    entry = struct.unpack_from("<BBBBHHII", ico, 6)
    assert entry[0] == 0 and entry[1] == 0


def test_wrap_png_as_ico_rejects_bad_input():
    with pytest.raises(ValueError):
        wrap_png_as_ico(b"notpng", 10, 10)
    with pytest.raises(ValueError):
        wrap_png_as_ico(_png(512, 512), 512, 512)
