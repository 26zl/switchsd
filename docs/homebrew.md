# Which homebrew, and why

`switchsd/data/components.json` lists what `setup` and `new` install. The choice comes from GitHub
(stars, maintenance, latest release) and from what four sources recommended in November 2026:

- the NH Switch Guide, now at switch.hacks.guide;
- the HATS pack, build 2026-11-12;
- the Kefir pack;
- the PYXIS pack, build 46.

The "Sources" column below counts how many of the four include a component.

`components.lock.json` pins every component to a release and a SHA-256. `setup` refuses a
download that does not match. `switchsd.py updates` lists the components with a newer release,
which needs no account or token, and `switchsd.py lock` moves the pins forward after a release has
been looked at. A weekly GitHub Actions job is set up to run `updates --verify`: a newer release
shows as a warning, a pinned file that no longer matches the checksum GitHub publishes fails the
run, and one issue lists what is behind until the pins catch up.

## Installed by default

| Component | Why | Sources | Notes |
| --- | --- | --- | --- |
| Atmosphère, hekate | CFW and bootloader | 4 | Upstream hekate rather than HATS's Hekatos fork |
| sys-patch (impeeza) | Runs unsigned titles | 3 | The borntohonk and ITotalJustice repos are gone; impeeza is the maintained one |
| DBI (English) | Installing, MTP, title management | 2 | English comes from rashevskyv/DBIPatcher; DBI itself is Russian-only and closed source. Tried on a Switch Lite on firmware 22.5.0: it starts from sphaira, with a notice that it lacks the console's keys; nothing was installed with it. |
| Goldleaf | Installer, file browser | 3 | |
| sphaira | Homebrew menu with store, Themezer and FTP | 2 | `setup` turns on its installing to the emuMMC, not to the console's own system, so that it can put tico, or itself, on the home menu as a forwarder. Homebrew started from a sphaira forwarder runs with a game's full memory: tried on a Switch Lite on firmware 22.5.0, where tico started from it. The Switch 2 build in its archive (`nx2/`) is left out. |
| JKSV | Save backups | 3 | |
| Homebrew App Store | Homebrew downloads | 1 | The official 2.3.2 dates from 2023, has the old ABI and crashes on exit on firmware 21+. This is DefenderOfHyrule's rebuild, which the store's maintainers point to until 2.4 is out. sphaira uses the same store. |
| EdiZon overlay | Cheats in the overlay menu | 4 | WerWolv's original overlay crashes on firmware 16+ |
| Breeze | Cheats, memory search | 1 | Every release is marked as a pre-release, so the newest one is taken. Its two bundled copies of the homebrew menu still have the old ABI; they are only used by Breeze's optional menu-swapping profile. Tried on a Switch Lite on firmware 22.5.0: it starts from sphaira. |
| Ultrahand | Overlay menu, includes nx-ovlloader | 3 | Replaces Tesla; install only one of them |
| sys-clk + Ultrahand overlay | Clocks per game | 3 | Sysmodule from retronx-team, overlay from ppkantorski. Firmware 23 support is unconfirmed. |
| Status Monitor Deux | Clocks, temperatures, FPS | 3 | The old Status Monitor overlay is archived |
| QuickNTP | Sets the clock | 3 | |
| Sysmodules overlay | Turns sysmodules on and off | 3 | |
| MissionControl | Other consoles' Bluetooth controllers | 4 | Must match the Atmosphère version |
| Disable remap dialog | Drops the dialog shown while button remapping is active | 1 | Home menu patches from MissionControl's author, now published in `ndeadly/mc_exefs_patches` |
| ldn_mitm (DefenderOfHyrule) | LAN play | 3 | Must match the Atmosphère version. Upstream's v1.25.1 predates Atmosphère 1.12 despite the higher number. |
| Switch 90DNS tester | Checks the Nintendo block | 1 | |
| NXThemes Installer, theme patches | Themes | 4 | Patches go in `themes/systemPatches/` |
| Themezer-NX | Theme browser | 1 | |
| emuiibo | Virtual amiibo | 3 | |
| Linkalho | Offline account link | 1 | |
| Moonlight | Streams games from a PC | – | 1,700 GitHub stars, one of the most-starred Switch apps |
| green-nx | Xbox Cloud Gaming | – | Game Pass cloud games over Wi-Fi |
| Autorun | Your own Windows PC games (Wine with FEX/Box64, DXVK) | – | Experimental. It updates itself, so `setup` only installs it when missing. Its Quick setup can add an "Autorun" hekate entry with its own kernel, built for one Atmosphère version; `check` points that out. |
| tico | Games for older consoles: one menu, with emulators from the NES to the GameCube, Wii, PS2 and 3DS | – | 1,460 GitHub stars. The emulators come from tico's own Store, and tico updates itself, so `setup` only installs it when missing. It refuses to run from the Album; sphaira can put it on the home menu as a forwarder, installed to the emuMMC only. Tried on a Switch Lite on firmware 22.5.0: it starts from that forwarder and installs its 20 emulators; no game was tried. |
| sys-botbase (masagrator's build) | Remote control for `switchsd.py remote` | 1 | Upstream 2.5 keeps a 4.5 MB heap, which crashes firmware 21 and newer with error 0x10801 (upstream issues #97 and #103, pull request #111). masagrator's build has the fix and adds SD card file commands. Installed switched off (no `boot2.flag`) and listed in the Sysmodules overlay as startable on demand. The build's own `toolbox.json` says it needs a reboot, but it starts on demand: tried on a Switch Lite on firmware 22.5.0. |
| TegraExplorer-Ext | Payload; copies the console's own firmware for Daybreak ([how](guide.md#updating-the-emummcs-firmware)) | 2 | Upstream TegraExplorer has been idle since 2024 |

Six sysmodules start at boot. Since firmware 21 there is about 10 MB less memory for them, so the
ones that cost memory without being needed every day are optional.

## Optional (`--with ...`)

| Component | Why it is not a default |
| --- | --- |
| `cyberfoil` | Installer with network libraries; the default installers cover installing from the card and over USB |
| `kitnx` | The HATS author's maintenance payload. It carries a modified `package3`, one per Atmosphère version, so it has to be updated together with Atmosphère. |
| `checkpoint` | A second save manager |
| `awoo` | Installer; TinWoo is an archived fork of it |
| `ftpd` | Its last release predates firmware 21's ABI change and may crash; sphaira has FTP |
| `aio-switch-updater` | Dormant, and it fetches firmware from third parties; switchsd does the updating |
| `saltynx` + `fpslocker` | SaltyNX loads into every game and upsets some. It is installed switched off; set Auto Start in the Sysmodules overlay to use FPSLocker. |
| `sys-con` | USB controllers; starts at boot |
| `sysdvr` | Streams the screen to a PC; starts at boot |
| `fizeau` | Screen colour; starts at boot |
| `battery-desync-fix` | CTCaer's fix for a battery gauge that lost track |
| `nx-shell`, `nx-activity-log` | File manager, play time |

## Left out on purpose

- **Tinfoil and TinWoo**, installers built around shops.
- **Firmware downloaders** (HATS-Tools, the AIO firmware feature): firmware should come from your
  own console.
- **Key dumpers** (Lockpick_RCM and forks): switchsd needs no keys. DBI starts without them, with a
  notice that some of its functions are unavailable.
- **Kefirosphere**, an Atmosphère fork with signature patches built in.
- **nxdumptool**: archived, "project discontinued".
