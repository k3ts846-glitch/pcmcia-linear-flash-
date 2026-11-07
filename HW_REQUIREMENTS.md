# Hardware Requirements — PCMCIA FLASH KIT 2.18.4

Supported hardware configuration for the notebook side (Linux laptop with PCMCIA/CardBus slot).

---

## 1. Laptop

| Parameter | Requirement | Notes |
|-----------|-------------|-------|
| Architecture | x86 / x86_64 | ARM not tested |
| OS | Linux (kernel 4.15+) | Debian/Ubuntu recommended |
| RAM | ≥ 256 MiB | Image buffering |
| Storage | ≥ 100 MiB free | For card images |
| Network | Ethernet or Wi-Fi | SSH connection from Windows console |
| Python | ≥ 3.9 | stdlib only, no third-party packages |

---

## 2. PCMCIA / CardBus Bridge

| Parameter | Requirement | Verified |
|-----------|-------------|----------|
| Controller | UltraMedia PCI7411 | ✅ 0x5ca9 (photo on file) |
| Compatible | TI PCI7621, PCI7421, PCI7611, PCI7411 | Datasheet on file |
| Interface | CardBus (32-bit, 33 MHz) | Required for 16-bit flash pairs |
| Memory window | Configurable via ExCA registers | Socket Control +10h, Power Management +20h |
| Power control | ExCA Power Control +802h / +42h | COE, AUTOPWRSWEN, CAPWREN, EXCAVPP |

---

## 3. Socket Power Switch (VPP / VCC)

| Parameter | Requirement | Verified |
|-----------|-------------|----------|
| Component | TI TPS2220B (U26 on board) | ✅ Datasheet on file |
| VCC control | 3.3 V / 5 V | Break-Before-Make switching |
| VPP control | Up to 12 V | Required for some chips |
| Serial control | 11-bit word (D0–D10) | DATA(3), CLOCK(4), LATCH(5) |
| SHDN pin | Pin 21 | Shutdown control |
| RESET pin | Pin 12 | Reset control |
| Switching delay | ≤ 2.5 ms (5 V), ≤ 2 ms (12 V) | Must be accounted in timing |

---

## 4. Supported Flash Chips

The chip tables (`src/pcmcia_direct/chips_data.py` and
`src/pcmcia_flash/data/chips.json`) describe **121 rows**. The matrix below is
generated from those tables (not typed by hand) and `tests/test_flash_regressions.py`
compares it back with the data on every run, so the document cannot drift.

Summary:

* rows requiring 12 V on Vpp: 17 (`VPP126`), all other rows: 104 (`VPP5`);
* rows that the kit only reads (`ro`, full-chip erase is the only erase mode, or
  the part is not NOR at all): 14 — 28F010, 28F020, 28F256, 28F512, Am28F010, Am28F020, Am28F256, Am28F512, D28F010, D28F020, K9F1208U0A, K9F1G08U0A, K9F2G08U0A, M28F102;
* rows marked **NOT VERIFIED** (parameters are not confirmed by a datasheet —
  20h and other optional commands are switched off for them): 14 — Am29F032, Am29PL1280, Am29PL320, Am29PL640, HN28F010, HN28F020, HN28F100, LH28F010, LH28F020, M28F016, M5M29F010, M5M29F020, MBM29F033, MX29F3210;
* Unlock Bypass `20h` is enabled for 29 rows and only where a datasheet shows
  the command.

### AMD / Fujitsu, 5 V JEDEC (AAh/55h unlock)

| Chip | Size | Sector | Protocol | Page | Bypass | Volts | Read-only | Datasheet |
|------|------|--------|----------|------|--------|-------|-----------|-----------|
| Am29F010 | 128 KiB | 16 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| Am29F016 | 2 MiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| Am29F016D | 2 MiB | 64 KiB | JEDEC | 1 byte | Yes | 5 V | — | yes |
| Am29F017D | 2 MiB | 64 KiB | JEDEC | 1 byte | Yes | 5 V | — | yes |
| Am29F032 | 4 MiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | NOT VERIFIED |
| Am29F032B | 4 MiB | 64 KiB | JEDEC | 1 byte | Yes | 5 V | — | yes |
| Am29F033 | 4 MiB | 64 KiB | JEDEC | 1 byte | Yes | 5 V | — | yes |
| Am29F040 | 512 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| Am29F080 | 1 MiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| Am29F080B | 1 MiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| Am29F200 | 256 KiB | 64 KiB | JEDEC | 1 byte | Yes | 5 V | — | yes |
| Am29F200AB | 256 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| Am29F200AT | 256 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| Am29F400 | 512 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| Am29F400AB | 512 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| Am29F400AT | 512 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| Am29F800 | 1 MiB | 64 KiB | JEDEC | 1 byte | Yes | 5 V | — | yes |
| Am29F800BB | 1 MiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| Am29F800BT | 1 MiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| Am29LV001 | 128 KiB | 64 KiB | JEDEC | 1 byte | No | 3 V | — | yes |
| Am29LV001BB | 128 KiB | 64 KiB | JEDEC | 1 byte | No | 3 V | — | yes |
| Am29LV001BT | 128 KiB | 64 KiB | JEDEC | 1 byte | No | 3 V | — | yes |
| Am29LV016 | 2 MiB | 64 KiB | JEDEC | 1 byte | No | 3 V | — | yes |
| Am29LV033C | 4 MiB | 64 KiB | JEDEC | 1 byte | No | 3 V | — | yes |
| Am29LV040B | 512 KiB | 64 KiB | JEDEC | 1 byte | No | 3 V | — | yes |
| Am29LV160 | 2 MiB | 64 KiB | JEDEC | 1 byte | No | 3 V | — | yes |
| Am29LV160DB | 2 MiB | 64 KiB | JEDEC | 1 byte | No | 3 V | — | yes |
| Am29LV160DT | 2 MiB | 64 KiB | JEDEC | 1 byte | No | 3 V | — | yes |
| Am29LV400 | 512 KiB | 64 KiB | JEDEC | 1 byte | No | 3 V | — | yes |
| Am29LV400BB | 512 KiB | 64 KiB | JEDEC | 1 byte | No | 3 V | — | yes |
| Am29LV400BT | 512 KiB | 64 KiB | JEDEC | 1 byte | No | 3 V | — | yes |
| Am29LV800 | 1 MiB | 64 KiB | JEDEC | 1 byte | No | 3 V | — | yes |
| Am29LV800BB | 1 MiB | 64 KiB | JEDEC | 1 byte | No | 3 V | — | yes |
| Am29LV800BT | 1 MiB | 64 KiB | JEDEC | 1 byte | No | 3 V | — | yes |
| MBM29F002 | 256 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| MBM29F002BC | 256 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| MBM29F002TC | 256 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| MBM29F004 | 512 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| MBM29F004BC | 512 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| MBM29F004TC | 512 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| MBM29F016 | 2 MiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| MBM29F016A | 2 MiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| MBM29F033 | 4 MiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | NOT VERIFIED |
| MBM29F033C | 4 MiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| MBM29F040C | 512 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |

### AMD / Fujitsu StrataFlash and page-mode, 3 V (buffer 32 B)

| Chip | Size | Sector | Protocol | Page | Bypass | Volts | Read-only | Datasheet |
|------|------|--------|----------|------|--------|-------|-----------|-----------|
| Am29DL322 | 4 MiB | 64 KiB | JEDEC | 32 B | Yes | 3 V | — | yes |
| Am29DL322DB | 4 MiB | 8 KiB | JEDEC | 32 B | Yes | 3 V | — | yes |
| Am29DL322DT | 4 MiB | 64 KiB | JEDEC | 32 B | Yes | 3 V | — | yes |
| Am29DL323 | 4 MiB | 64 KiB | JEDEC | 32 B | Yes | 3 V | — | yes |
| Am29DL323DB | 4 MiB | 8 KiB | JEDEC | 32 B | Yes | 3 V | — | yes |
| Am29DL323DT | 4 MiB | 64 KiB | JEDEC | 32 B | Yes | 3 V | — | yes |
| Am29DL324DB | 4 MiB | 8 KiB | JEDEC | 32 B | Yes | 3 V | — | yes |
| Am29DL324DT | 4 MiB | 64 KiB | JEDEC | 32 B | Yes | 3 V | — | yes |
| Am29DL642 | 8 MiB | 64 KiB | JEDEC | 32 B | Yes | 3 V | — | yes |
| Am29DL643 | 8 MiB | 64 KiB | JEDEC | 32 B | Yes | 3 V | — | yes |
| Am29GL064 | 8 MiB | 64 KiB | JEDEC | 32 B | Yes | 3 V | — | yes |
| Am29GL1024 | 128 MiB | 64 KiB | JEDEC | 32 B | Yes | 3 V | — | yes |
| Am29GL128 | 16 MiB | 64 KiB | JEDEC | 32 B | Yes | 3 V | — | yes |
| Am29GL256 | 32 MiB | 64 KiB | JEDEC | 32 B | Yes | 3 V | — | yes |
| Am29GL512 | 64 MiB | 64 KiB | JEDEC | 32 B | Yes | 3 V | — | yes |
| Am29PL1280 | 16 MiB | 64 KiB | JEDEC | 32 B | No | 3 V | — | NOT VERIFIED |
| Am29PL320 | 4 MiB | 64 KiB | JEDEC | 32 B | No | 3 V | — | NOT VERIFIED |
| Am29PL640 | 8 MiB | 64 KiB | JEDEC | 32 B | No | 3 V | — | NOT VERIFIED |
| MBM29DL322 | 4 MiB | 64 KiB | JEDEC | 32 B | Yes | 3 V | — | yes |
| MBM29DL322BE | 4 MiB | 8 KiB | JEDEC | 32 B | Yes | 3 V | — | yes |
| MBM29DL322TE | 4 MiB | 64 KiB | JEDEC | 32 B | Yes | 3 V | — | yes |
| MBM29DL642 | 8 MiB | 64 KiB | JEDEC | 32 B | Yes | 3 V | — | yes |
| MBM29GL064 | 8 MiB | 64 KiB | JEDEC | 32 B | Yes | 3 V | — | yes |
| MBM29GL128 | 16 MiB | 64 KiB | JEDEC | 32 B | Yes | 3 V | — | yes |

### AMD Am28F, 12 V, chip erase only

| Chip | Size | Sector | Protocol | Page | Bypass | Volts | Read-only | Datasheet |
|------|------|--------|----------|------|--------|-------|-----------|-----------|
| Am28F010 | 128 KiB | 128 KiB | JEDEC | 1 byte | No | 12 V | read-only | yes |
| Am28F020 | 256 KiB | 256 KiB | JEDEC | 1 byte | No | 12 V | read-only | yes |
| Am28F256 | 32 KiB | 32 KiB | JEDEC | 1 byte | No | 12 V | read-only | yes |
| Am28F512 | 64 KiB | 64 KiB | JEDEC | 1 byte | No | 12 V | read-only | yes |

### Intel CUI (commands by byte or word)

| Chip | Size | Sector | Protocol | Page | Bypass | Volts | Read-only | Datasheet |
|------|------|--------|----------|------|--------|-------|-----------|-----------|
| 28F004S5 | 512 KiB | 64 KiB | Intel CUI | 1 byte | No | 5 V | — | yes |
| 28F008S5 | 1 MiB | 64 KiB | Intel CUI | 1 byte | No | 5 V | — | yes |
| 28F008SA | 1 MiB | 64 KiB | Intel CUI | 1 byte | No | 12 V | — | yes |
| 28F010 | 128 KiB | 128 KiB | Intel CUI | 1 byte | No | 12 V | read-only | yes |
| 28F016S5 | 2 MiB | 64 KiB | Intel CUI | 1 byte | No | 5 V | — | yes |
| 28F016SA | 2 MiB | 64 KiB | Intel CUI | 1 byte | No | 12 V | — | yes |
| 28F016SV | 2 MiB | 64 KiB | Intel CUI | 1 byte | No | 5 V | — | yes |
| 28F020 | 256 KiB | 256 KiB | Intel CUI | 1 byte | No | 12 V | read-only | yes |
| 28F256 | 32 KiB | 32 KiB | Intel CUI | 1 byte | No | 12 V | read-only | yes |
| 28F320 | 4 MiB | 64 KiB | Intel CUI | 1 byte | No | 3 V | — | yes |
| 28F512 | 64 KiB | 64 KiB | Intel CUI | 1 byte | No | 12 V | read-only | yes |
| D28F010 | 128 KiB | 128 KiB | Intel CUI | 1 byte | No | 12 V | read-only | yes |
| D28F020 | 256 KiB | 256 KiB | Intel CUI | 1 byte | No | 12 V | read-only | yes |
| M28F002 | 256 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| M28F004 | 512 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| M28F008 | 1 MiB | 64 KiB | Intel CUI | 1 byte | No | 12 V | — | yes |
| M28F016 | 2 MiB | 64 KiB | Intel CUI | 1 byte | No | 12 V | — | NOT VERIFIED |
| M28F102 | 128 KiB | 128 KiB | JEDEC | 1 byte | No | 12 V | read-only | yes |

### SST (uniform 4 KiB sectors)

| Chip | Size | Sector | Protocol | Page | Bypass | Volts | Read-only | Datasheet |
|------|------|--------|----------|------|--------|-------|-----------|-----------|
| SST39SF040 | 512 KiB | 4 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| SST39VF010 | 128 KiB | 4 KiB | JEDEC | 1 byte | No | 3 V | — | yes |
| SST39VF020 | 256 KiB | 4 KiB | JEDEC | 1 byte | No | 3 V | — | yes |
| SST39VF040 | 512 KiB | 4 KiB | JEDEC | 1 byte | No | 3 V | — | yes |
| SST39VF080 | 1 MiB | 4 KiB | JEDEC | 1 byte | No | 3 V | — | yes |

### Macronix

| Chip | Size | Sector | Protocol | Page | Bypass | Volts | Read-only | Datasheet |
|------|------|--------|----------|------|--------|-------|-----------|-----------|
| MX29F016 | 2 MiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| MX29F040 | 512 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| MX29F1610 | 2 MiB | 128 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| MX29F3210 | 4 MiB | 128 KiB | JEDEC | 1 byte | No | 5 V | — | NOT VERIFIED |

### Toshiba

| Chip | Size | Sector | Protocol | Page | Bypass | Volts | Read-only | Datasheet |
|------|------|--------|----------|------|--------|-------|-----------|-----------|
| TC58FVT160 | 2 MiB | 64 KiB | JEDEC | 1 byte | Yes | 3 V | — | yes |
| TC58FVT321 | 4 MiB | 64 KiB | JEDEC | 1 byte | Yes | 3 V | — | yes |

### Hitachi / Sharp / Mitsubishi (names not confirmed by a datasheet)

| Chip | Size | Sector | Protocol | Page | Bypass | Volts | Read-only | Datasheet |
|------|------|--------|----------|------|--------|-------|-----------|-----------|
| HN28F010 | 128 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | NOT VERIFIED |
| HN28F020 | 256 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | NOT VERIFIED |
| HN28F100 | 1 MiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | NOT VERIFIED |
| LH28F010 | 128 KiB | 64 KiB | JEDEC | 1 byte | No | 12 V | — | NOT VERIFIED |
| LH28F020 | 256 KiB | 64 KiB | JEDEC | 1 byte | No | 12 V | — | NOT VERIFIED |
| M5M29F010 | 128 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | NOT VERIFIED |
| M5M29F020 | 256 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | NOT VERIFIED |
| M5M29F400 | 512 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |

### Samsung NAND (refused: not NOR)

| Chip | Size | Sector | Protocol | Page | Bypass | Volts | Read-only | Datasheet |
|------|------|--------|----------|------|--------|-------|-----------|-----------|
| K9F1208U0A | 64 MiB | 16 KiB | JEDEC | 1 byte | No | 3.3 V | read-only | yes |
| K9F1G08U0A | 128 MiB | 128 KiB | JEDEC | 1 byte | No | 3.3 V | read-only | yes |
| K9F2G08U0A | 256 MiB | 128 KiB | JEDEC | 1 byte | No | 3.3 V | read-only | yes |

### Atmel

| Chip | Size | Sector | Protocol | Page | Bypass | Volts | Read-only | Datasheet |
|------|------|--------|----------|------|--------|-------|-----------|-----------|
| AT29C010 | 128 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| AT29C020 | 256 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| AT29C040 | 512 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| AT29C512 | 64 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |

### Other

| Chip | Size | Sector | Protocol | Page | Bypass | Volts | Read-only | Datasheet |
|------|------|--------|----------|------|--------|-------|-----------|-----------|
| M29F002BB | 256 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| M29F002BT | 256 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| M29F400BB | 512 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |
| M29F400BT | 512 KiB | 64 KiB | JEDEC | 1 byte | No | 5 V | — | yes |

---

## 5. Voltage Requirements

The kit takes the voltage from the chip tables (`vpp_need()`), not from
«5 V for everything»: until 2.18.2 the writer in `pcmcia_direct` always asked
the socket for 5 V, so a 12 V part (Intel 28F008SA/28F016SA, AMD Am28F*) was
programmed at the wrong Vpp and the erase silently failed.

| Chip group | Vcc | Vpp | Notes |
|-----------|-----|-----|-------|
| AMD Am29F*/MBM29F*, SST39SF, Macronix MX29F | 5 V | 5 V | Chip is 5.0 V-only; Vpp is not used internally |
| AMD Am29LV*, SST39VF/LF, Am29DL/GL/PL, Intel 28F320 | 3 V | **not needed at all** | Program/erase run from Vcc 2.7-3.6 V (vpp_need() returns 3 and the kit does not touch Vpp) |
| Intel 28F010/28F020, 28F008SA, 28F016SA, M28F* | 5 V | **12 V** | Program and erase require 12 V on Vpp |
| AMD Am28F256/512/010/020, ST M28F102 | 5 V | **12 V** | Full-chip erase only — the kit treats them as read-only |
| Intel 28F016SV (SmartVoltage) | 5 V | 5 V or 12 V | Datasheet allows both; the table asks for 5 V |

**Hardware caveat:** the socket switch (TPS2220B, U26) does deliver 12 V on
Vpp, and the kit asks for it when `vpp_need()` says 12, but 12 V has not been
measured on this board revision yet — check `vpp` state before writing to a
12 V part.

---

## 6. Known Tested Hardware

| Item | Value | Source |
|------|-------|--------|
| Laptop board photo | UltraMedia PCI7411 | `uploads/IMG_20261105_232535.jpg` |
| Power switch photo | TPS2220B U26 | `uploads/IMG_20261105_233457.jpg` |
| Bridge datasheet | TI PCI7621/PCI7421/PCI7611/PCI7411 | `uploads/PCI7621.PDF` |
| Switch datasheet | TI TPS2220B | `uploads/TPS2220B.PDF` |
| Live card | Centennial FL32M (2 × Am29F016, 4 MiB) | Read verified: 4 194 304 bytes in 20.9 s, md5 `e9b567a7d4faeb0a127e473980a5a63b` |

---

## 7. What Is NOT Supported

| Feature | Reason |
|---------|--------|
| NAND flash | Different command protocol (not linear NOR) |
| SPI flash | Different interface (not PCMCIA memory window) |
| Chips requiring 12 V Vpp | Supported by the code (Vpp 12 V via ExCA/TPS2220B), but 12 V delivery is not measured on this board revision yet |
| ARM laptops | Not tested, CardBus bridge may differ |
| Windows on the laptop | The notebook side must run Linux |

---

## 8. Pre-Deployment Checklist

- [ ] Laptop runs Linux with kernel ≥ 4.15
- [ ] CardBus bridge is PCI7411 or compatible (check `lspci`)
- [ ] TPS2220B present at U26 (check board photo)
- [ ] Python 3.9+ installed on laptop
- [ ] OpenSSH server running on laptop
- [ ] SSH key or password access from Windows console
- [ ] `/dev/mem` accessible (root or appropriate group)
- [ ] Flash chip in supported list (check `chip` command)
- [ ] Write not yet verified on live hardware — test on one sector first
