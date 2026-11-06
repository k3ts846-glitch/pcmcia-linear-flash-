# Hardware Requirements — PCMCIA FLASH KIT 2.16+

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

The kit supports 68 chip geometries. Key families:

### AMD / Fujitsu (JEDEC, AA/55 unlock)

| Chip | Size | Sector | Protocol | Page | Bypass | StrataFlash | Volts |
|------|------|--------|----------|------|--------|-------------|-------|
| Am29F016 | 2 MiB | 64 KiB | JEDEC | 1 byte | Yes | No | 5 V |
| Am29F032 | 4 MiB | 64 KiB | JEDEC | 1 byte | Yes | No | 5 V |
| Am29F040 | 512 KiB | 64 KiB | JEDEC | 1 byte | Yes | No | 5 V |
| Am29F080 | 1 MiB | 64 KiB | JEDEC | 1 byte | Yes | No | 5 V |
| Am29LV016 | 2 MiB | 64 KiB | JEDEC | 1 byte | No | No | 3 V |
| Am29PL320 | 4 MiB | 64 KiB | JEDEC | 32 B | Yes | Yes | 3 V |
| Am29PL640 | 8 MiB | 64 KiB | JEDEC | 32 B | Yes | Yes | 3 V |
| Am29PL1280 | 16 MiB | 64 KiB | JEDEC | 32 B | Yes | Yes | 3 V |
| Am29DL322 | 4 MiB | 64 KiB | JEDEC | 32 B | Yes | Yes | 3 V |
| Am29DL323 | 4 MiB | 64 KiB | JEDEC | 32 B | Yes | Yes | 3 V |
| Am29DL642 | 8 MiB | 64 KiB | JEDEC | 32 B | Yes | Yes | 3 V |
| Am29DL643 | 8 MiB | 64 KiB | JEDEC | 32 B | Yes | Yes | 3 V |
| Am29GL064 | 8 MiB | 64 KiB | JEDEC | 32 B | Yes | Yes | 3 V |
| Am29GL128 | 16 MiB | 64 KiB | JEDEC | 32 B | Yes | Yes | 3 V |
| Am29GL256 | 32 MiB | 64 KiB | JEDEC | 32 B | Yes | Yes | 3 V |
| Am29GL512 | 64 MiB | 64 KiB | JEDEC | 32 B | Yes | Yes | 3 V |
| Am29GL1024 | 128 MiB | 64 KiB | JEDEC | 32 B | Yes | Yes | 3 V |
| MBM29F016 | 2 MiB | 64 KiB | JEDEC | 1 byte | Yes | No | 5 V |
| MBM29DL322 | 4 MiB | 64 KiB | JEDEC | 32 B | Yes | Yes | 3 V |
| MBM29DL642 | 8 MiB | 64 KiB | JEDEC | 32 B | Yes | Yes | 3 V |
| MBM29GL064 | 8 MiB | 64 KiB | JEDEC | 32 B | Yes | Yes | 3 V |
| MBM29GL128 | 16 MiB | 64 KiB | JEDEC | 32 B | Yes | Yes | 3 V |

### Intel 28F Series II (CUI, word commands)

| Chip | Size | Sector | Protocol | Page | Bypass | Volts |
|------|------|--------|----------|------|--------|-------|
| 28F008SA | 1 MiB | 64 KiB | Intel CUI | 1 byte | No | 5 V |
| 28F016SA | 2 MiB | 64 KiB | Intel CUI | 1 byte | No | 5 V |

### Intel / ST StrataFlash (CUI, buffered 25h / D0h)

| Chip | Size | Sector | Protocol | Page | Bypass | Volts |
|------|------|--------|----------|------|--------|-------|
| 28F016S5 | 2 MiB | 64 KiB | Intel CUI | 32 B | No | 5 V |
| M28F016S5 | 2 MiB | 64 KiB | Intel CUI | 32 B | No | 5 V |

### Other manufacturers (JEDEC, byte commands)

Atmel AT29F016/040/080, SST 39VF/SF016/040, Macronix MX29F016/040,
Toshiba, Hitachi, Mitsubishi, NEC, Sharp, ST, Samsung — 512 KiB to 2 MiB,
64 KiB sectors, 5 V.

---

## 5. Voltage Requirements

| Chip type | Vcc | Vpp | Notes |
|-----------|-----|-----|-------|
| Am29F016/032/040/080 | 5 V | Not required | 5 V only |
| Am29LV016 | 3 V | Not required | Low voltage |
| StrataFlash (PL/DL/GL) | 3 V or 5 V | Not required | Buffered write |
| Intel 28F008SA/016SA | 5 V | Not required | CUI protocol |
| Intel StrataFlash 28F016S5 | 5 V | Not required | CUI + buffered |

**Warning:** Chips requiring 12 V Vpp (some Intel Series 2, Am28Fxxx) cannot be
programmed in this slot even if /dev/mtdN works, because the TPS2220B VPP
delivery has not been verified on this board revision.

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
| Chips requiring 12 V Vpp | VPP delivery via TPS2220B not verified on this board |
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
