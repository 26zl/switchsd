# Contributing

## Reporting a problem

Open an issue; the forms ask for what is needed. The most useful report holds the exact command,
everything it printed, the operating system, and what `python3 switchsd.py --version` says.

Leave out everything read from a console or its card: serial numbers, keys, PRODINFO, card images.
None of it is needed to find a bug, and an issue is public.

A problem that could make the tool write to the wrong disk, or install something other than the
pinned files, goes the private way that [SECURITY.md](SECURITY.md) describes.

## What helps most

The tool has run with one Switch Lite, from one Mac. The README's
[What has been tested](README.md#what-has-been-tested) lists what has never run. A report that a
command worked, or did not, on a console model, an operating system or a card size that is not in
that table is as welcome as code. Say what you ran and what the console did.

## Changing the code

There is nothing to install: the tool uses the standard library only and has to run on Python 3.9,
which is what `sudo python3` gets on macOS.

```sh
python3 -m unittest discover -s tests      # everything that runs without rights
ruff check && mypy                          # if you have them; configured in ruff.toml and mypy.ini
```

The end-to-end tests write to disk images, never to a real disk. They run by themselves on macOS,
and need root on Linux and an administrator terminal on Windows; the README's
[Development](README.md#development) section has the commands, including one that runs the Linux
side in Docker.

[AGENTS.md](AGENTS.md) holds the conventions. The ones that matter most:

- A raw write needs the card's exact size, a removable or virtual disk and a typed confirmation.
  The source is checked whole before the target is touched, and what was written is read back.
- `setup` never writes or removes anything under `emuMMC/`, `Nintendo/` or `JKSV/`, and never
  overwrites a settings file.
- Names and sizes that come from a card, an image or the console are untrusted.
- A change comes with a test, and the docs say what was tried on real hardware and what was not.

## Adding or updating a component

1. Describe it in `switchsd/data/components.json`: where its releases are, and which files go
   where on the card.
2. `python3 switchsd.py lock ID` pins its newest release with a SHA-256. Look at the release
   before you commit the pin.
3. `python3 switchsd.py setup SOME_FOLDER --only ID` shows what lands on a card. A release whose
   files would land outside the folders components use is refused: adjust the rule (`unzip`,
   `exclude`) to the archive's layout.
4. Add it to [docs/homebrew.md](docs/homebrew.md), with the reason it is a default or an option.

A component that starts at boot costs memory on every boot, so it is optional unless nearly
everyone needs it. [docs/homebrew.md](docs/homebrew.md#left-out-on-purpose) lists what is left out
on purpose.

## Pull requests

Keep one change to a pull request, and say in it what you tested and on what: a disk image, a real
card, which console, which system.
