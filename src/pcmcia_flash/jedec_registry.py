#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Эталонный реестр JEDEC Device ID, сверенный с даташитами производителей.

Каждая запись содержит:
    * JEDEC Device ID (байт, возвращаемый командой 90h / Autoselect)
    * имя микросхемы (как в CHIP_GEOMETRY)
    * источник — ссылку на даташит или документ, откуда взят ID

Источники:
    * flashrom flashchips.h (Chromium OS / coreboot project)
    * Linux kernel drivers/mtd/chips/jedec_probe.c
    * AMD datasheets pub#16736 (Am29F010), pub#17113 (Am29F040), pub#20375B (Am29F800),
      pub#20637A (Am29F200), AN943 (Am29F400)
    * Fujitsu datasheet DS05-20807-3E (MBM29F016)
    * Intel datasheets 28F008SA / 28F016SA
    * CNC-LAB ID code table (03.05.2016)

При изменении любого ID в chips.py должен обновиться и этот реестр.
Функция validate_jedec_ids() проверяет соответствие при импорте модуля chips.
"""

from .errors import DirectError

# (Device ID, имя в CHIP_GEOMETRY, источник)
AMD_ETALON = {
    0x20: ("Am29F010",   "AMD pub#16736 Rev.G, Device ID = 20H"),
    0x22: ("Am29DL322",  "AMD datasheet, Device ID = 22H"),
    0x23: ("Am29F400AT", "AMD AN943, Device ID = 23H"),
    0x3D: ("Am29F017D",  "Linux kernel jedec_probe.c, AM29F017D 0x003D"),
    0x41: ("Am29F032B",  "Linux kernel jedec_probe.c, AM29F032B 0x0041"),
    0x4F: ("Am29LV040B", "Linux kernel jedec_probe.c, AM29LV040B 0x004F"),
    0x51: ("Am29F200AT", "AMD pub#20637 Rev.A, Device ID = 51H"),
    0x57: ("Am29F200AB", "AMD pub#20637 Rev.A, Device ID = 57H"),
    0x58: ("Am29F800BB", "AMD pub#20375B, Device ID = 58H"),
    0x6D: ("Am29LV001BB","flashrom flashchips.h, AMD_AM29LV001BB 0x6D"),
    0xA4: ("Am29F040",   "AMD pub#17113 Rev.D, Device ID = A4H"),
    0xAB: ("Am29F400AB", "AMD AN943, Device ID = ABH"),
    0xAD: ("Am29F016",   "AMD datasheet, Device ID = ADH"),
    0xBA: ("Am29F080B",  "пользовательский даташот AM29F080B.PDF, Device ID = BAH"),
    0xD5: ("Am29F080",   "flashrom flashchips.h AMD_AM29F080, Device ID = D5H"),
    0xD6: ("Am29F800BT", "AMD pub#20375B, Device ID = D6H"),
    0xED: ("Am29LV001BT","flashrom flashchips.h, AMD_AM29LV001BT 0xED"),
}

# Fujitsu: Manufacturer ID = 04h
FUJITSU_ETALON = {
    0xAD: ("MBM29F016",  "Fujitsu DS05-20807-3E, Device ID = ADH (vendor 04h)"),
    0xA4: ("MBM29F040C", "Fujitsu MBM29F040C, Device ID = A4H (vendor 04h)"),
}

# Intel: Device ID читается через CUI Read ID (90h)
INTEL_ETALON = {
    0xA1: ("28F016SA", "Intel datasheet, Device ID = A1H"),
    0xA2: ("28F008SA", "Intel datasheet, Device ID = A2H"),
    0xA6: ("28F016SV", "Intel datasheet, Device ID = A6H"),
    0xB5: ("28F002SA", "Intel datasheet, Device ID = B5H"),
    0xB6: ("28F004SA", "Intel datasheet, Device ID = B6H"),
}

# Производители JEDEC (Manufacturer ID)
MANUFACTURER_IDS = {
    0x01: "AMD",
    0x04: "Fujitsu",
    0x89: "Intel",
}


def validate_jedec_ids(chip_geometry, amd_parts, fujitsu_parts, intel_parts):
    """Проверить, что все JEDEC ID в чипах совпадают с эталонным реестром.

    Выбрасывает DirectError при любом несовпадении — лучше упасть при импорте,
    чем silently использовать неверный ID и испортить карту.
    """
    errors = []

    # Проверяем AMD
    for dev_id, (name, source) in AMD_ETALON.items():
        if dev_id not in amd_parts:
            errors.append(
                f"AMD: Device ID 0x{dev_id:02X} отсутствует в AMD_PARTS ( source: {source})"
            )
            continue
        part_name = amd_parts[dev_id][0]
        if part_name != name:
            errors.append(
                f"AMD: Device ID 0x{dev_id:02X} -> '{part_name}' в chips.py, "
                f"но в реестре '{name}' ({source})"
            )
        if part_name not in chip_geometry:
            errors.append(
                f"AMD: '{part_name}' (ID 0x{dev_id:02X}) отсутствует в CHIP_GEOMETRY"
            )

    # Проверяем Fujitsu
    for dev_id, (name, source) in FUJITSU_ETALON.items():
        if dev_id not in fujitsu_parts:
            errors.append(
                f"Fujitsu: Device ID 0x{dev_id:02X} отсутствует в FUJITSU_PARTS ( source: {source})"
            )
            continue
        part_name = fujitsu_parts[dev_id][0]
        if part_name != name:
            errors.append(
                f"Fujitsu: Device ID 0x{dev_id:02X} -> '{part_name}' в chips.py, "
                f"но в реестре '{name}' ({source})"
            )
        if part_name not in chip_geometry:
            errors.append(
                f"Fujitsu: '{part_name}' (ID 0x{dev_id:02X}) отсутствует в CHIP_GEOMETRY"
            )

    # Проверяем Intel
    for dev_id, (name, source) in INTEL_ETALON.items():
        if dev_id not in intel_parts:
            errors.append(
                f"Intel: Device ID 0x{dev_id:02X} отсутствует в INTEL_PARTS ( source: {source})"
            )
            continue
        part_name = intel_parts[dev_id][0]
        if part_name != name:
            errors.append(
                f"Intel: Device ID 0x{dev_id:02X} -> '{part_name}' в chips.py, "
                f"но в реестре '{name}' ({source})"
            )
        if part_name not in chip_geometry:
            errors.append(
                f"Intel: '{part_name}' (ID 0x{dev_id:02X}) отсутствует в CHIP_GEOMETRY"
            )

    # Проверяем обратное: все ID в chips.py должны быть в реестре
    for dev_id, part_info in amd_parts.items():
        name = part_info[0]
        if dev_id not in AMD_ETALON:
            errors.append(
                f"AMD: Device ID 0x{dev_id:02X} ('{name}') в chips.py отсутствует в эталонном реестре"
            )

    for dev_id, part_info in fujitsu_parts.items():
        name = part_info[0]
        if dev_id not in FUJITSU_ETALON:
            errors.append(
                f"Fujitsu: Device ID 0x{dev_id:02X} ('{name}') в chips.py отсутствует в эталонном реестре"
            )

    for dev_id, part_info in intel_parts.items():
        name = part_info[0]
        if dev_id not in INTEL_ETALON:
            errors.append(
                f"Intel: Device ID 0x{dev_id:02X} ('{name}') в chips.py отсутствует в эталонном реестре"
            )

    if errors:
        msg = "JEDEC ID не совпадают с эталонным реестром (jedec_registry.py):\n" + "\n".join(
            f"  * {e}" for e in errors
        )
        raise DirectError(msg)

    return True


__all__ = [
    "AMD_ETALON", "FUJITSU_ETALON", "INTEL_ETALON", "MANUFACTURER_IDS",
    "validate_jedec_ids",
]
