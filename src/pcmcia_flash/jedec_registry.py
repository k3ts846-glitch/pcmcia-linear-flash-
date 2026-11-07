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
    * Fujitsu datasheet DS05-20807-3E (MBM29F016A; 2.18.2: имя сведено к этому)
    * Intel datasheets 28F008SA / 28F016SA
    * CNC-LAB ID code table (03.05.2016)

При изменении любого ID в chips.json должен обновиться и реестр
(data/jedec_registry.json) - это единственный источник: словари этого модуля
строятся из него при импорте (2.18.2).
Функция validate_jedec_ids() проверяет соответствие при импорте модуля chips.
"""

from .chip_db import load_registry
from .errors import DirectError

# 2.18.2: реестр был ДВАЖДЫ - этот модуль с литеральными словарями и
# data/jedec_registry.json, который читает chip_db. Правки делали в одном
# месте из двух, и «эталон» подтверждал сам себя (проверка в тестах так и
# называлась). Теперь источник один - JSON; словари ниже строятся из него при
# импорте, поэтому разъехаться они уже не могут. Источники по каждому коду
# (даташиты, flashrom, jedec_probe.c) лежат в самом JSON, в поле "source".
REGISTRY = load_registry()


def _etalon(vendor: str) -> dict:
    """{Device ID: (имя, источник)} для раздела реестра."""
    return {int(code, 16): (info["chip"], info.get("source", ""))
            for code, info in (REGISTRY.get(vendor) or {}).items()}


AMD_ETALON = _etalon("AMD")
FUJITSU_ETALON = _etalon("Fujitsu")
INTEL_ETALON = _etalon("Intel")
ST_ETALON = _etalon("STMicroelectronics")

# Производители JEDEC (Manufacturer ID)
MANUFACTURER_IDS = {int(code, 16): name
                   for code, name in (REGISTRY.get("manufacturer_ids") or {}).items()}


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
    "AMD_ETALON", "FUJITSU_ETALON", "INTEL_ETALON", "ST_ETALON",
    "MANUFACTURER_IDS", "REGISTRY", "validate_jedec_ids",
]
