"""The ABI marker reader: NRO files, sysmodule packages and the LZ4 prefix decoder."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from support import code_image, lz4_literals, sysmodule_nsp

from switchsd import abi


class AbiTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def write(self, name: str, data: bytes) -> Path:
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_nro_and_overlay(self):
        self.assertEqual(abi.revision(self.write("new.nro", code_image(1))), 1)
        self.assertEqual(abi.revision(self.write("newer.ovl", code_image(2))), 2)
        self.assertEqual(abi.revision(self.write("old.nro", code_image(None))), 0)
        self.assertIsNone(abi.revision(self.write("rust.nro", code_image(None, libnx=False))))
        self.assertIsNone(abi.revision(self.write("junk.nro", b"not a program")))
        self.assertIsNone(abi.revision(self.write("empty.nro", b"")))
        self.assertIsNone(abi.revision(self.write("notes.txt", code_image(1))))

    def test_made_up_files_cost_little_and_crash_nothing(self):
        # One LZ4 match can ask for 255 times its own size; the reader stops at what it needs.
        bomb = bytes([0x1F, 0x41, 0x01, 0x00]) + b"\xff" * 4000 + b"\x00"
        self.assertLessEqual(len(abi.lz4_prefix(bomb, 64)), 64)
        self.assertEqual(abi.lz4_prefix(lz4_literals(b"x" * 100), 8)[:8], b"x" * 8)
        self.assertIsNone(abi.revision(self.write("short/exefs.nsp", b"PFS0" + bytes(8) + b"\x05")))
        self.assertIsNone(abi.revision(self.write("cut/exefs.nsp", sysmodule_nsp(code_image(1), True)[:0x30])))

    def test_sysmodule_package(self):
        for compressed in (False, True):
            new = self.write(f"a{compressed}/exefs.nsp", sysmodule_nsp(code_image(1), compressed))
            old = self.write(f"b{compressed}/exefs.nsp", sysmodule_nsp(code_image(None), compressed))
            self.assertEqual(abi.revision(new), 1, compressed)
            self.assertEqual(abi.revision(old), 0, compressed)
        self.assertIsNone(abi.revision(self.write("c/exefs.nsp", b"PFS0" + bytes(12))))
        self.assertIsNone(abi.revision(self.write("d/exefs.nsp", b"garbage")))

    def test_lz4_prefix(self):
        block = bytes([0x35]) + b"abc" + bytes([3, 0]) + bytes([0x10]) + b"z"  # "abc", 9 more from 3 back, "z"
        self.assertEqual(abi.lz4_prefix(block, 100), b"abcabcabcabcz")
        self.assertEqual(abi.lz4_prefix(block, 5)[:5], b"abcab")
        long = bytes(range(256)) * 3
        self.assertEqual(abi.lz4_prefix(lz4_literals(long), len(long)), long)
        with self.assertRaises(ValueError):
            abi.lz4_prefix(bytes([0x05, 9, 0]), 10)  # a match reaching back before the start


if __name__ == "__main__":
    unittest.main()
