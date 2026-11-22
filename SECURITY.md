# Security policy

## Reporting a vulnerability

Report it privately on GitHub: **Security → Report a vulnerability** in this repository. Please do
not open a public issue for it.

Include the version (`python3 switchsd.py --version`, or the commit) and how to reproduce it.
Leave out serial numbers, keys, PRODINFO and anything else read from a console or its card.

## Scope

In scope: `switchsd.py` and the `switchsd/` package, above all anything that could make it write to
the wrong disk or outside the planned partitions, install something other than the pinned files, or
touch the emuMMC or saves; and the docs where they could lead someone to lose data. Bugs in hekate,
Atmosphère or the homebrew named in the docs belong with those projects.

## Supported versions

`main`. This is a one-person project maintained on a best-effort basis.
