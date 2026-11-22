"""Finding disks: which ones the raw commands may touch on Linux and Windows, from made-up system answers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from switchsd import disks, install
from switchsd.common import Failure, linux_mounts

GIB = 1 << 30


@unittest.skipIf(sys.platform == "win32", "builds a sysfs look-alike out of symbolic links")
class LinuxDiskTests(unittest.TestCase):
    """`_linux_disk` against a made-up /sys and /proc. The disk names are ones no computer has."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.blocks = self.root / "sys/class/block"
        self.blocks.mkdir(parents=True)
        (self.root / "proc/self").mkdir(parents=True)
        self.mounts: list[str] = ["proc /proc proc rw 0 0"]
        self.swaps: list[str] = []
        for name, value in (("SYS", str(self.root / "sys")), ("PROC", str(self.root / "proc"))):
            patch = mock.patch.object(disks, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def disk(self, name: str, bus: str, sectors: int = 64 * GIB // 512, removable: str = "0",
             kind: str | None = None) -> None:
        device = self.root / "devices" / bus / name
        device.mkdir(parents=True)
        (device / "model").write_text("Made-up Disk\n")
        if kind:
            (device / "type").write_text(kind + "\n")
        folder = self.blocks / name
        folder.mkdir()
        os.symlink(device, folder / "device")
        self.fill(folder, sectors, removable=removable, ro="0")

    def partition(self, disk: str, name: str, start: int, sectors: int) -> None:
        folder = self.blocks / disk / name
        folder.mkdir()
        self.fill(folder, sectors, partition="1", start=str(start))
        os.symlink(folder, self.blocks / name)

    def mapped(self, name: str, on: str) -> None:
        """A device-mapper volume (LUKS, LVM) built on the block device `on`."""
        folder = self.blocks / name
        folder.mkdir()
        self.fill(folder, 1)
        holders = self.blocks / on / "holders"
        holders.mkdir(exist_ok=True)
        (holders / name).mkdir()

    def fill(self, folder: Path, sectors: int, **files: str) -> None:
        for key, value in {"size": str(sectors), **files}.items():
            (folder / key).write_text(value + "\n")

    def look(self, name: str) -> disks.Disk:
        (self.root / "proc/self/mounts").write_text("".join(line + "\n" for line in self.mounts))
        (self.root / "proc/swaps").write_text("Filename Type Size Used Priority\n"
                                              + "".join(f"{dev} partition 1 0 -2\n" for dev in self.swaps))
        return disks._linux_disk(f"/dev/{name}")

    def test_a_card_in_a_usb_reader(self):
        self.disk("sdzz", "pci0/usb1/1-1/host9", removable="1")
        self.partition("sdzz", "sdzz1", 32768, 1000)
        self.mounts.append("/dev/sdzz1 /run/media/u/SWITCH\\040SD vfat rw 0 0")
        disk = self.look("sdzz")
        self.assertEqual((disk.removable, disk.system, disk.virtual, disk.readonly), (True, False, False, False))
        self.assertEqual((disk.size, disk.name), (64 * GIB, "Made-up Disk"))
        self.assertEqual([(v.offset, v.size, v.mount) for v in disk.volumes],
                         [(32768 * 512, 1000 * 512, "/run/media/u/SWITCH SD")])
        with self.assertRaisesRegex(Failure, "is a partition"):
            self.look("sdzz1")
        with self.assertRaisesRegex(Failure, "is not a disk"):
            self.look("sdzq")

    def test_a_disk_the_system_runs_from_through_luks_and_lvm(self):
        self.disk("sdzy", "pci0/usb2/2-1/host8", removable="1")
        self.partition("sdzy", "sdzy1", 2048, 1000)
        self.partition("sdzy", "sdzy2", 4096, 1000)
        self.mapped("dm-97", on="sdzy2")  # LUKS on the partition
        self.mapped("dm-98", on="dm-97")  # LVM on LUKS
        self.assertFalse(self.look("sdzy").system)
        self.mounts.append("/dev/dm-98 /run/media/u/secret ext4 rw 0 0")
        self.assertFalse(self.look("sdzy").system, "an encrypted stick is no system disk")
        self.mounts.append("/dev/dm-98 / ext4 rw 0 0")
        self.assertTrue(self.look("sdzy").system)
        with mock.patch.object(disks, "get_disk", lambda ident: self.look("sdzy")), \
                self.assertRaisesRegex(Failure, "holds the running system"):
            disks.check_disk("/dev/sdzy", 64 * GIB, writing=True)

    def test_swap_and_plain_system_partitions(self):
        self.disk("sdzx", "pci0/usb3/3-1/host7", removable="1")
        self.partition("sdzx", "sdzx1", 2048, 1000)
        self.partition("sdzx", "sdzx2", 4096, 1000)
        self.swaps.append("/dev/sdzx2")
        self.assertTrue(self.look("sdzx").system)
        self.swaps.clear()
        for point in ("/boot/efi", "/efi", "/home", "/"):
            self.mounts = [f"/dev/sdzx1 {point} vfat rw 0 0"]
            self.assertTrue(self.look("sdzx").system, point)
        self.mounts = ["/dev/sdzx /var ext4 rw 0 0"]  # a file system on the whole device
        self.assertTrue(self.look("sdzx").system)
        self.mounts = ["/dev/sdzx1 /home/u/card vfat rw 0 0"]
        self.assertFalse(self.look("sdzx").system, "a card mounted in a home folder is still a card")

    def test_an_sd_card_is_removable_and_the_built_in_emmc_is_not(self):
        self.disk("mmcblk8", "platform/mmc_host/mmc8", kind="SD")
        self.disk("mmcblk9", "platform/mmc_host/mmc9", kind="MMC")
        self.disk("sdzw", "pci0/ata1/host0")
        self.assertTrue(self.look("mmcblk8").removable)
        self.assertFalse(self.look("mmcblk9").removable)
        self.assertFalse(self.look("sdzw").removable)
        with mock.patch.object(disks, "get_disk", lambda ident: self.look("mmcblk9")), \
                self.assertRaisesRegex(Failure, "not a removable disk"):
            disks.check_disk("/dev/mmcblk9", None)

    def test_everything_mounted_from_the_card_is_unmounted_deepest_first(self):
        self.disk("sdzv", "pci0/usb4/4-1/host6", removable="1")
        self.partition("sdzv", "sdzv1", 2048, 1000)
        self.partition("sdzv", "sdzv2", 4096, 1000)
        self.mounts += ["/dev/sdzv1 /mnt/card vfat rw 0 0", "/dev/sdzv2 /mnt/card/inner ext4 rw 0 0",
                        "/dev/sdzv1 /mnt/tab\\011name vfat rw 0 0", "/dev/sdzv /mnt/whole vfat rw 0 0",
                        "/dev/sdzu1 /mnt/other vfat rw 0 0"]
        disk = self.look("sdzv")
        ran: list[list[str]] = []
        with mock.patch.object(disks, "run", lambda cmd, timeout=120: ran.append(cmd) or ""), \
                mock.patch.object(sys, "platform", "linux"), disks.exclusive(disk):
            pass
        self.assertEqual(ran, [["umount", "/mnt/card/inner"], ["umount", "/mnt/card"], ["umount", "/mnt/tab\tname"],
                               ["umount", "/mnt/whole"]])

    def test_the_mount_table_is_read_as_the_kernel_writes_it(self):
        table = self.root / "mounts"
        table.write_text("/dev/sdzz1 /media/u/SWITCH\\040SD vfat rw 0 0\n"
                         "/dev/sdzz2 /media/u/back\\134slash exfat rw 0 0\n"
                         "broken line\n")
        self.assertEqual(linux_mounts(str(table)), [("/dev/sdzz1", "/media/u/SWITCH SD", "vfat"),
                                                    ("/dev/sdzz2", "/media/u/back\\slash", "exfat")])

    def test_a_folders_file_system_is_that_of_the_mount_it_lies_in(self):
        mounts = [("/dev/root", "/", "ext4"), ("/dev/sdzz1", "/mnt/sd", "vfat"), ("tmpfs", "/mnt/sd/tmp", "tmpfs")]
        with mock.patch.object(install, "linux_mounts", lambda: mounts), mock.patch.object(sys, "platform", "linux"):
            for folder, expected in (("/mnt/sd", "vfat"), ("/mnt/sd/switch", "vfat"), ("/mnt/sd2/switch", "ext4"),
                                     ("/mnt/sd/tmp/x", "tmpfs"), ("/srv", "ext4")):
                self.assertEqual(install.filesystem(Path(folder)), expected, folder)


class WindowsDiskTests(unittest.TestCase):
    """The Windows side with PowerShell's and kernel32's answers made up; the scripts it runs are parsed
    by a real PowerShell where one is installed."""

    ANSWERS = (
        ("@(Get-Disk |", [0, 2]),
        ("Get-Disk -Number 0", {"Number": 0, "FriendlyName": "System SSD", "Size": 512 * GIB, "IsBoot": True,
                                "IsSystem": True, "IsReadOnly": False, "Bus": "NVMe"}),
        ("Get-Disk -Number 2", {"Number": 2, "FriendlyName": "Card Reader", "Size": 250139901952, "IsBoot": False,
                                "IsSystem": False, "IsReadOnly": False, "Bus": "USB"}),
        ("$_.AccessPaths }", "\\\\?\\Volume{0a1b2c3d-0000-0000-0000-100000000000}\\"),
        ("Get-Partition -DiskNumber 0", [{"Offset": 1048576, "Size": 104857600, "DriveLetter": "\0", "Paths": []},
                                         {"Offset": 122683392, "Size": 500 * GIB, "DriveLetter": "C", "Paths": []}]),
        ("Get-Partition -DiskNumber 2", {"Offset": 16777216, "Size": 237 * GIB, "DriveLetter": "E", "Paths": []}),
        ("Update-Disk", None),
    )

    def setUp(self):
        self.scripts: list[str] = []
        self.calls: list[tuple] = []

        def powershell(cmd: list[str], timeout: float = 120) -> str:
            self.assertEqual(cmd[1:4], ["-NoProfile", "-NonInteractive", "-Command"])
            self.assertTrue(cmd[0].startswith("C:\\Windows"), "PowerShell is started by its full path")
            script = cmd[4].split("; ", 1)[-1]
            self.scripts.append(script)
            answer = next(value for needle, value in self.ANSWERS if needle in script)
            return "" if answer is None else json.dumps(answer)

        kernel32 = mock.Mock()
        kernel32.CloseHandle.side_effect = lambda handle: self.calls.append(("close", handle))
        for patch in (
                mock.patch.object(disks, "run", powershell),
                mock.patch.dict(os.environ, {"SYSTEMROOT": "C:\\Windows"}),
                mock.patch.object(disks, "_kernel32", lambda: (None, None, kernel32)),
                mock.patch.object(disks, "_win_open", lambda path: self.calls.append(("open", path)) or path),
                mock.patch.object(disks, "_win_ioctl",
                                  lambda handle, code, what: self.calls.append(("ioctl", handle, code)))):
            patch.start()
            self.addCleanup(patch.stop)

    def windows(self):  # type: ignore[no-untyped-def]
        return mock.patch.object(sys, "platform", "win32")

    def test_only_the_card_is_offered_and_the_system_disk_is_refused(self):
        with self.windows():
            found = disks.list_disks()
            system = disks.get_disk("0")
            for ident in ("0", r"\\.\PhysicalDrive0"):
                with self.assertRaisesRegex(Failure, "holds the running system"):
                    disks.check_disk(ident, None, writing=True)
            with self.assertRaisesRegex(Failure, "--expect-bytes"):
                disks.check_disk("2", 1, writing=True)
            with self.assertRaisesRegex(Failure, "is not a disk number"):
                disks.get_disk("E:")
        self.assertEqual([(d.id, d.raw, d.size, d.name) for d in found],
                         [("2", r"\\.\PhysicalDrive2", 250139901952, "Card Reader")])
        self.assertEqual([(v.offset, v.mount) for v in found[0].volumes], [(16777216, "E:\\")])
        self.assertEqual((system.system, system.removable), (True, False))
        self.assertEqual([v.mount for v in system.volumes], [None, "C:\\"])

    def test_volumes_are_locked_and_dismounted_for_a_write_and_let_go_after(self):
        volume = "\\\\?\\Volume{0a1b2c3d-0000-0000-0000-100000000000}"
        with self.windows():
            disk = disks.get_disk("2")
            with disks.exclusive(disk):
                self.assertEqual(self.calls, [("open", volume), ("ioctl", volume, 0x00090018),
                                              ("ioctl", volume, 0x00090020)])
            self.assertEqual(self.calls[-1], ("close", volume))
            self.calls.clear()
            disks.reread(disk)
        self.assertEqual(self.calls, [("open", r"\\.\PhysicalDrive2"), ("ioctl", r"\\.\PhysicalDrive2", 0x00070140),
                                      ("close", r"\\.\PhysicalDrive2")])
        self.assertEqual(self.scripts[-1], "Update-Disk -Number 2")

    def test_the_powershell_it_runs_is_valid_powershell(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("needs a PowerShell to parse the scripts with")
        with self.windows():
            disks.list_disks()
            disk = disks.get_disk("2")
            with disks.exclusive(disk):
                pass
            disks.reread(disk)
        scripts = sorted(set(self.scripts))
        self.assertEqual(len(scripts), 7, scripts)
        parse = ("$errors = $null; $tokens = $null; [void][System.Management.Automation.Language.Parser]::ParseInput("
                 "$env:SWITCHSD_SCRIPT, [ref]$tokens, [ref]$errors); "
                 "if ($errors.Count) { $errors | ForEach-Object { $_.Message }; exit 1 }")
        for script in scripts:
            result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", parse], capture_output=True,
                                    text=True, timeout=120, check=False, env={**os.environ, "SWITCHSD_SCRIPT": script})
            self.assertEqual(result.returncode, 0, f"{script}\n{result.stdout}{result.stderr}")


if __name__ == "__main__":
    unittest.main()
