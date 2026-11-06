#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Загрузчик базы микросхем из JSON с fail-fast валидацией.

База устройств хранится в:
    * src/pcmcia_flash/data/chips.json      — геометрия, JEDEC ID, напряжения
    * src/pcmcia_flash/data/jedec_registry.json — эталонный реестр JEDEC ID с источниками

При загрузке проверяется соответствие чипов из chips.json эталону в jedec_registry.json.
При любом несовпадении выбрасывается DirectError — лучше упасть при старте,
чем 조용히 использовать неверный ID и испортить карту.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .errors import DirectError

PAGE_NONE = 1  # одностраничная запись (чипы без буфера)

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def _hex(value: str | int | None) -> int | None:
    """Преобразовать строку '0x20' или число в int. None остаётся None."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    return int(value, 16)


def _load_json(name: str) -> dict[str, Any]:
    """Загрузить JSON-файл из директории данных пакета."""
    path = os.path.join(_DATA_DIR, name)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _build_part_tables(chips: dict[str, Any]) -> tuple[dict, dict, dict]:
    """Построить AMD_PARTS, FUJITSU_PARTS, INTEL_PARTS из chips.json.

    Формат: {JEDEC_ID: (имя, ёмкость_Мбит, ревизия/примечание)}
    """
    amd: dict[int, tuple[str, int, str]] = {}
    fujitsu: dict[int, tuple[str, int, str]] = {}
    intel: dict[int, tuple[str, int, str]] = {}

    for name, chip in chips.items():
        jedec = _hex(chip.get("jedec_id"))
        if jedec is None:
            continue
        size_kib = chip["size_kib"]
        size_mbit = size_kib * 8 // 1024
        notes = chip.get("notes", "")
        manuf = chip["manuf"]

        entry = (name, size_mbit, notes)
        if manuf == "AMD":
            amd[jedec] = entry
        elif manuf == "Fujitsu":
            fujitsu[jedec] = entry
        elif manuf == "Intel":
            intel[jedec] = entry

    return amd, fujitsu, intel


def _build_geometry(chips: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Построить CHIP_GEOMETRY из chips.json."""
    geometry: dict[str, dict[str, Any]] = {}
    for name, chip in chips.items():
        cmd1 = _hex(chip.get("cmd_addr1"))
        cmd2 = _hex(chip.get("cmd_addr2"))
        geometry[name] = {
            "size": chip["size_kib"] * 1024,
            "sector": chip["sector_kib"] * 1024,
            "cmd": (cmd1, cmd2),
            "wide": chip.get("wide", False),
            "page": chip.get("page") or PAGE_NONE,
            "bypass": chip.get("bypass", False),
            "strata": chip.get("strata", False),
            "protocol": chip.get("protocol", "jedec"),
            "volts": chip.get("volts", 5),
            "manuf": chip.get("manuf", ""),
            # sectors - карта секторов ОДНОГО чипа: [(размер, число), ...]
            # от младших адресов. Нужна для boot-микросхем, где первый сектор
            # 16 КиБ, дальше 8+8+32 КиБ, и только потом 64 КиБ: стирать их
            # «сектором 64 КиБ» нельзя. Пустой список - карта однородная,
            # её собирает chips.sector_map из sector и size.
            "sectors": [(int(size) * 1024, int(count))
                        for size, count in (chip.get("sectors") or [])],
            "notes": chip.get("notes", ""),
        }
    return geometry


def _build_vpp_lists(chips: dict[str, Any], vpp5: list[str], vpp126: list[str]) -> tuple[set[str], set[str]]:
    """Построить множества VPP5 и VPP126 из чипов и списков из JSON."""
    vpp5_set: set[str] = set()
    vpp126_set: set[str] = set()

    for name, chip in chips.items():
        volts = chip.get("volts", 5)
        if volts == 12:
            vpp126_set.add(name)
        else:
            vpp5_set.add(name)

    # Дополняем из явно заданных списков (для старых чипов без записи в chips.json)
    vpp5_set.update(vpp5)
    vpp126_set.update(vpp126)

    return vpp5_set, vpp126_set


def validate(chip_geometry: dict[str, Any],
             amd_parts: dict[int, Any],
             fujitsu_parts: dict[int, Any],
             intel_parts: dict[int, Any],
             registry: dict[str, Any]) -> None:
    """Fail-fast валидация: все JEDEC ID в чипах должны совпадать с эталоном.

    Выбрасывает DirectError при любом несовпадении.
    """
    errors: list[str] = []

    def check_vendor(vendor_key: str, parts: dict[int, Any], vendor_name: str) -> None:
        etalon = registry.get(vendor_key, {})
        for dev_id, (name, _mbit, _rev) in parts.items():
            key = f"0x{dev_id:02X}"
            if key not in etalon:
                errors.append(f"{vendor_name}: Device ID {key} ('{name}') отсутствует в эталонном реестре")
                continue
            etalon_name = etalon[key]["chip"]
            if etalon_name != name:
                errors.append(
                    f"{vendor_name}: Device ID {key} -> '{name}' в chips.json, "
                    f"но в реестре '{etalon_name}' ({etalon[key].get('source', '')})"
                )
            if name not in chip_geometry:
                errors.append(f"{vendor_name}: '{name}' (ID {key}) отсутствует в CHIP_GEOMETRY")

        # Обратная проверка: все из реестра должны быть в чипах
        for key, info in etalon.items():
            dev_id = int(key, 16)
            if dev_id not in parts:
                errors.append(f"{vendor_name}: Device ID {key} ('{info['chip']}') отсутствует в таблице чипов")

    check_vendor("AMD", amd_parts, "AMD")
    check_vendor("Fujitsu", fujitsu_parts, "Fujitsu")
    check_vendor("Intel", intel_parts, "Intel")

    if errors:
        msg = "JEDEC ID не совпадают с эталонным реестром (jedec_registry.json):\n" + "\n".join(
            f"  * {e}" for e in errors
        )
        raise DirectError(msg)


# --- загрузка данных при импорте модуля --------------------------------
_raw_chips = _load_json("chips.json")
_registry = _load_json("jedec_registry.json")

AMD_PARTS, FUJITSU_PARTS, INTEL_PARTS = _build_part_tables(_raw_chips["chips"])
CHIP_GEOMETRY = _build_geometry(_raw_chips["chips"])
VPP5, VPP126 = _build_vpp_lists(
    _raw_chips["chips"],
    _raw_chips.get("vpp5", []),
    _raw_chips.get("vpp126", []),
)

# Fail-fast при импорта модуля
validate(CHIP_GEOMETRY, AMD_PARTS, FUJITSU_PARTS, INTEL_PARTS, _registry)

__all__ = [
    "CHIP_GEOMETRY", "AMD_PARTS", "FUJITSU_PARTS", "INTEL_PARTS",
    "VPP5", "VPP126", "PAGE_NONE",
]
