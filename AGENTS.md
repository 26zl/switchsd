# AGENTS.md

This repository ships `switchsd.py`, a standard-library Python tool for macOS, Linux and Windows. It
sets up, checks, backs up and migrates hekate/Atmosphère SD cards. The README says what each command
does, and `docs/homebrew.md` why each component is in the set.

## Commands

Run from the repository root. None of these touches a real card.

- `python3 -m unittest discover -s tests -v`:
  - The unit tests run everywhere.
  - The end-to-end tests (`tests/test_e2e.py`) attach image files as disks and run `new`, `format`,
    `image`, `restore` and `clone` against them: disk images on macOS, VHDs as administrator on
    Windows. On Linux they need root for loop devices:
    `sudo python3 -m unittest discover -s tests -p test_e2e.py`. With dosfstools they also run
    `fsck.fat` on what was written, and with `dmsetup` they test that a disk in use is refused.
  - Elsewhere they are skipped.
- On a Mac with Docker, the same on Linux, as root with loop devices (the README's Development
  section has the command). Run it after any change to `blockdev.py`, `disks.py` or `migrate.py`:
  macOS never runs the Linux half of those.
- `/usr/bin/python3 -m unittest discover -s tests` on macOS: the same on Python 3.9, which is what
  `sudo python3` runs. It must pass.
- `ruff check` and `mypy`, configured in `ruff.toml` and `mypy.ini`, should be clean; they are not
  in CI.
- `python3 switchsd.py updates` needs the network and nothing else: it lists the pins that are behind
  their project's newest release. `updates --verify` also compares every pinned file with GitHub's
  published SHA-256 and needs `GITHUB_TOKEN`.
- `python3 switchsd.py lock` needs the network. It moves every pin in
  `switchsd/data/components.lock.json` to the newest release, checked against GitHub's published
  SHA-256. Look at the releases before committing the result.

CI (`.github/workflows/ci.yml`) runs the tests on ubuntu-latest (end-to-end under sudo),
macos-latest (also with `/usr/bin/python3`) and windows-latest. `.github/workflows/pins.yml` runs
`updates --verify` every week and when the pins change; the weekly run keeps one issue, "Pinned
releases have updates", listing what is behind, and closes it once nothing is.

## Structure

- `switchsd.py`: the entry point. It adds its own folder to `sys.path`, because `python3 -I` does
  not.
- `switchsd/cli.py`: argparse, and the `list`, `setup`, `check`, `updates`, `lock` and `new`
  commands.
- `switchsd/common.py`: the errors and their exit codes, and helpers every command shares.
- `switchsd/components.py`: the manifest (`data/components.json`), selection, GitHub release
  lookup, and verified downloads into a per-OS cache.
- `switchsd/install.py`: the install plan, how it is applied, the record in
  `config/switchsd/installed.json`, settings templates (`data/sdroot/`), legacy clean-up
  (`data/legacy.json`), and the backups behind `undo`.
- `switchsd/doctor.py`: `check`.
- `switchsd/battery.py`: reads the battery's state from hekate's dump of the fuel gauge.
- `switchsd/games.py`: `games`, which copies game files into the card's `games/` folder and stores
  those FAT32 cannot hold as a folder of parts.
- `switchsd/abi.py`: reads the homebrew ABI revision from NRO files, overlays and sysmodule packages.
- `switchsd/remote.py`: `remote`, a sys-botbase client that only talks to local addresses. Its
  `check` mirrors the card's names and a few small files into a temporary folder and runs
  `doctor.check` on that.
- `switchsd/autocheck.py`: `autocheck`, which runs `check` on mount through launchd or systemd.
- The raw side:
  - `switchsd/migrate.py`: `disks`, `image`, `info`, `restore`, `clone` and `format`, and the part
    of `new` that writes the card.
  - `blockdev.py`: sector-aligned I/O.
  - `imagefile.py`: the compact image format, which holds only the parts of a card in use.
  - `disks.py`: the per-OS part, which finds, unmounts or locks a disk and re-reads its partition
    table.
  - `mbr.py`: hekate's layout, for a moved, restored or new card.
  - `fat32.py`: a reader, and a writer that builds a whole volume.
  - `emummc.py`: the sector references.
- `tests/support.py` builds synthetic cards with the FAT32 writer and attaches images per OS.

## Conventions

- Standard library only, Python 3.9 compatible: keep `from __future__ import annotations` and avoid
  3.10+ APIs outside the tests.
- Never commit anything copied off a card or console: images, keys, PRODINFO, BIS keys, serial
  numbers, eMMC IDs, fuse dumps. `.gitignore` catches the usual names; check `git status` anyway.
- Raw writes require `--expect-bytes`, a removable or virtual disk, and a typed confirmation. The
  source is checked whole before the target is touched; then the old partition table is wiped, the
  data follows, and the new table goes last. Keep it that way.
- The raw commands run under sudo, next to folders the user can write to. Downloads drop to the
  invoking user (`as_invoking_user`), and files handed back are chowned without following
  symlinks. Never open a path in such a folder for writing as root without `x` mode.
- Names and sizes that come from a card, an image or the console are untrusted: check a name with
  `safe_relpath` before it becomes a local path, and turn bad numbers into a `Failure`.
- What goes on a card comes from bytes that were compared with the pin after they were read
  (`components.verified`), never from a second read of the download cache.
- Errors are a `Failure` with a message for the user, exit code 1. `Usage` (2) is for a command
  line that no card or console could make right; `Mismatch` (3) for a check or a comparison that
  found a problem. A disk or file that is not there is a plain `Failure`.
- `image` only writes a compact image of a layout and a file system `restore` can rebuild, and reads
  the file back. A change to the format needs a new version number; images that a release wrote
  must stay readable.
- What `new` and `plan_layout` lay out has to match hekate's partition manager. The tests hold the
  partition table found on a 32 GB card set up for a 12 GiB emuMMC, which hekate's formulas give
  too; keep them passing to the sector.
- `setup` never writes or removes anything under `emuMMC/`, `Nintendo/` or `JKSV/`, and it never
  overwrites settings files.
- Before `setup` changes a card, every file it will replace or remove is copied to
  `config/switchsd/backup/<stamp>/` and the list of changes is saved next to it; only then does it
  write. `undo` checks every copy before it puts any back. Keep that order.
- A release that would write outside `install.TOP_LEVEL` is refused. When a project changes its
  layout, look at the new archive and adjust the component's rule (`unzip`, `exclude`), as for
  sphaira's Switch 2 build; widen `TOP_LEVEL` only for a folder the console really reads.
- A pinned file that GitHub no longer serves as pinned (replaced, or gone) raises
  `components.Changed`. `resolve_plan` leaves that component out, along with what `requires` it.
  `apply` leaves its files and its record entry as they are, even with `--prune`, and the run exits
  with 3. A `core` component stops the run instead, before anything changes. Never loosen the
  check: look at the release, then `lock <id>`.
- `lock` moves pins forward; look at what changed in each release, and at its archive's layout,
  before committing the result.
- Only components that start at boot cost memory every day; keep them optional unless the
  community treats them as essential.
- A component's source takes `"prerelease": true` only when the project publishes nothing but
  pre-releases, as Breeze does.
- Docs say what was verified and how, and mark everything else as not verified. They describe the
  tool, in a neutral voice: no notes about a particular console, purchase, seller or owner.
