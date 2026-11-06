# PCMCIA FLASH KIT

Control linear flash PCMCIA cards from a Windows machine: card passport,
image dump, write, verify, erase. The card sits in a Linux laptop; the
console connects to it via ssh and installs nothing there.

* No PowerShell scripts and no `.cmd` files — everything is Python 3.
* Only changed files are pushed to the laptop.
* Writing is done via chip commands through the bridge window, no drivers.
* Supports different chips: sector 16/64/128 KiB, byte and word commands,
  32-byte page programming (AMD Am29F016/032), 5 V and 12 V on Vpp.

## Quick start (no installation)

```
python pcmcia-kit.pyw            GUI console (or double-click the file)
python pcmcia-kit.pyw --text     text menu
python pcmcia-kit.pyw --text info --dry-run
```

`pcmcia-kit.pyw` automatically adds the `src` folder to the path, so no
configuration is needed. `python -m pcmcia_kit` works after package install.

Requires Python 3.9 or newer. No third-party libraries.

## Installation (optional)

Needed if you want to call `pcmcia-kit` from any directory:

```
py -3 -m pip install .
pcmcia-kit info --host 192.168.0.71
```

Package files (`pcmcia_direct.py`, `pcmcia_ctl.sh`) remain in the unpacked
folder: run from there or specify `--kit-dir`.

## Usage

```
python pcmcia-kit.pyw --text                     menu
python pcmcia-kit.pyw --text info                card passport and verdict
python pcmcia-kit.pyw --text read                dump image and fetch it
python pcmcia-kit.pyw --text write -i card.bin   write image to card
python pcmcia-kit.pyw --text verify -i card.bin  verify file against card
python pcmcia-kit.pyw --text erase --yes         erase card
python pcmcia-kit.pyw --text chip                what chip is installed (read-only)
python pcmcia-kit.pyw --text selftest            check the kit (no card needed)
python pcmcia-kit.pyw --text key                 key-based login (no password)
```

After `pip install .` commands are shorter: `pcmcia-kit info`, `pcmcia-kit read`.

Double-click `pcmcia-kit.pyw` to open the GUI console: Windows associates
`.pyw` with `pythonw.exe`, so no console window appears.

Common options: `--host`, `--user`, `--port`, `--key`, `--yes`, `--dry-run`,
`--image`, `--remote-path`, `--kit-dir`, `--full`.

## What lives where

```
pcmcia-kit.pyw            double-click -> GUI console
pyproject.toml            package: pcmcia-kit command, version, lint settings
src/pcmcia_kit/           remote console (package)
    cli.py                argument parsing and command dispatch
    config.py             defaults: address, port, kit files, menu items
    console.py            messages and UTF-8 in Windows console
    transport.py          ssh/scp: run, copy, live output, errors
    sync.py               kit synchronization by md5
    state.py              card state note in menu header
    remote.py             commands executed on the laptop
    keyring.py            key-based login
    gui.py                GUI console
    commands/             one module per command
pcmcia_direct.py          card work on the laptop (windows, CIS, JEDEC)
pcmcia_ctl.sh             actions on the laptop (info, read, write, erase...)
tests/                    checks: test_scripts.py, test_win.py, test_gui...
PCMCIA_LINEAR_FLASH_СПРАВОЧНИК.md   chip reference
ИНСТРУКЦИЯ.md             step-by-step instructions
README_EN.md              this file (English)
```

## Checks

```
python tests/test_win.py         Windows part of the console (no network, no card)
python tests/test_scripts.py     kit scripts (~30 s; --fast twice as fast)
xvfb-run -a python tests/test_gui.py     interface (needs X or xvfb)
python tests/release.py --build  release build: archives and checksums
```

## What is verified on real hardware vs. model only

The kit is checked two ways: automatic tests on the chip model
(`tests/nor_model.py`) and, when a physical card is available, on the live
Centennial FL32M card (2 × Am29F016, 4 MiB). The matrix below shows which
parts are confirmed on real hardware and which are only simulated.

| Section | What is done | Live card (Centennial FL32M) | Model only (`nor_model`) | Files |
|---------|-------------|------------------------------|--------------------------|-------|
| CIS passport | attribute memory read, chip identification | ✅ | ✅ | `pcmcia_direct.py`, `pcmcia_ctl.sh` |
| Image read | full card image dump, md5 | ✅ | ✅ | `pcmcia_direct.py`, `pcmcia_ctl.sh` |
| Auto chip detection | JEDEC ID (90h), CFI (98h) | ✅ | ✅ | `pcmcia_direct.py` |
| Byte write | A0h, 4 cycles, DQ7 polling | ⏳ partial | ✅ | `pcmcia_direct.py` |
| Unlock Bypass | 20h, 2-cycle write, F0h | ⏳ partial | ✅ | `pcmcia_direct.py` |
| Sector erase | 30h, DQ7/DQ5 polling | ⏳ partial | ✅ | `pcmcia_direct.py` |
| Blank check | all 0xFF after erase | ⏳ partial | ✅ | `pcmcia_direct.py` |
| Sector protection | group read (A6h) | ❌ not tested | ✅ | `pcmcia_direct.py` |
| Erase Suspend/Resume | B0h / 30h | ❌ not tested | ✅ | `pcmcia_direct.py` |
| VPP/VCC slot | Socket Control, ExCA, TPS2220B | ❌ not tested | not modeled | `pcmcia_direct.py` |
| Wear map | `.card-state.txt`, canwrite warning | ❌ not tested | ✅ | `src/pcmcia_kit/state.py` |
| Failure map | bad blocks, offset, expected/actual | ❌ not tested | ✅ | `src/pcmcia_kit/state.py` |
| Journal / --resume / --backup | interrupted write, continuation | ❌ not tested | ✅ | `src/pcmcia_kit/commands/*.py` |
| JSON output | `--json` for info/canwrite/diag | ✅ | ✅ | `src/pcmcia_kit/cli.py` |
| Bridge detection | PCI vendor/device ID (UltraMedia 0x5ca9) | ✅ | ✅ | `pcmcia_direct.py` |

**Legend:** ✅ confirmed on live card · ⏳ partial (individual sectors) · ❌ not tested.

Writing to the live card is not fully confirmed yet: the main verification is
done on the chip model (`tests/nor_model.py`). The kit honestly reports this
in its output.

## Hardware requirements

| Component | Requirement |
|-----------|-------------|
| Laptop | Linux, x86, with CardBus / PCMCIA slot |
| Bridge | UltraMedia PCI7411 (0x5ca9) or compatible TI PCI7621/PCI7421/PCI7611 |
| Power switch | TI TPS2220B (U26 on board) |
| Flash chip | Linear flash: AMD Am29F016/032/040/080, Fujitsu MBM29F016, Intel 28F008SA/28F016SA, StrataFlash Am29PL/DL/GL, Intel 28F016S5 |
| Voltage | 5 V Vcc (12 V Vpp optional, chip-dependent) |
| Connection | Ethernet / Wi-Fi between Windows console and Linux laptop |
| SSH | OpenSSH server on laptop, root or sudo access |

## License

Proprietary.
