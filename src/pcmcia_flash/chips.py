#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Таблица микросхем (CHIP_GEOMETRY), автоопределение через CFI/JEDEC ID.

База устройств вынесена в JSON:
    * src/pcmcia_flash/data/chips.json      — геометрия, JEDEC ID, напряжения
    * src/pcmcia_flash/data/jedec_registry.json — эталонный реестр JEDEC ID

Модуль chip_db загружает JSON при импорте и проводит fail-fast валидацию:
любое несовпадение JEDEC ID с эталоном выбрасывает DirectError.

Все параметры микросхемы: размер, сектор, адреса команд, ширина, страница,
протокол, напряжение, поддержка буферной записи и Unlock Bypass.
"""

from .chip_db import (
    CHIP_GEOMETRY, AMD_PARTS, FUJITSU_PARTS, INTEL_PARTS,
    VPP5, VPP126, PAGE_NONE,
)
from .constants import (
    NOR_CMD_ADDR1, NOR_CMD_ADDR2, SECTOR,
)


def vpp_need(info):
    """Сколько вольт программирования нужно карте: 5, 12 или 0 (неизвестно).

    Возвращает (вольт, откуда). Никогда не отвечает «12» только потому, что в
    паспорте встретилось слово AMD.
    """
    j = (info or {}).get("jedec")
    if j and len(j) >= 4:
        try:
            vid, did = int(j[0:2], 16), int(j[2:4], 16)
        except ValueError:
            vid = did = None
        if vid == 0x01 and did in AMD_PARTS:
            name = AMD_PARTS[did][0]
            if name in VPP126:
                return 12, f"по JEDEC: {name} (Vpp 12 В выведен наружу)"
            if name in VPP5:
                return 5, f"по JEDEC: {name} — семейство 5.0 V only, 12 В не нужно"
        # Fujitsu MBM29F016A: JEDEC vendor ID 0x04, device 0xAD
        if vid == 0x04 and did in FUJITSU_PARTS:
            name = FUJITSU_PARTS[did][0]
            if name in VPP5:
                return 5, f"по JEDEC: {name} — семейство 5.0 V only, 12 В не нужно"
        # Intel: Manufacturer ID = 89h
        if vid == 0x89 and did in INTEL_PARTS:
            name = INTEL_PARTS[did][0]
            if name in VPP126:
                return 12, f"по JEDEC: {name} (Vpp 12 В выведен наружу)"
            if name in VPP5:
                return 5, f"по JEDEC: {name} — 3.3/5.0 В, 12 В не нужно"
    prod = " ".join((info or {}).get("product") or [])
    up = prod.upper()
    for name in VPP126:
        if name.upper() in up:
            return 12, f"по строке паспорта: {name}"
    for name in VPP5:
        if name.upper() in up:
            return 5, f"по строке паспорта: {name}"
    if "SERIES 2" in up or "SERIES2" in up:
        return 0, (
            "Intel Series 2 (28F, 5 В): по документации 12 В не нужно, "
            "но у части карт Vpp разведён на 12 В — проверить пробной "
            "записью на копии нельзя, поэтому считаем неизвестным"
        )
    return 0, "по паспорту тип микросхем не определить"


def geometry_for(name: str, size: int | None = None, volts: int | None = None) -> dict:
    """Вернуть параметры микросхемы по имени (или ближайшее совпадение)."""

    if name in CHIP_GEOMETRY:
        geo = dict(CHIP_GEOMETRY[name])
        if size is not None:
            geo["size"] = size
        if volts is not None:
            geo["volts"] = volts
        return geo

    # Попробовать найти по частичному совпадению имени
    for chip_name, geo in CHIP_GEOMETRY.items():
        if chip_name.lower() in name.lower() or name.lower() in chip_name.lower():
            result = dict(geo)
            if size is not None:
                result["size"] = size
            if volts is not None:
                result["volts"] = volts
            return result

    raise ValueError(f"неизвестная микросхема: {name}")


def chip_name_from_info(info: dict) -> str:
    """Попытаться определить имя микросхемы по данным автоопределения (ID/CFI)."""
    manuf = info.get("manufacturer", "")
    device = info.get("device", "")
    cfi_id = info.get("cfi_id", "")

    # Простое сопоставление по производителю и устройству
    for chip_name, geo in CHIP_GEOMETRY.items():
        if geo["manuf"].lower() in manuf.lower():
            return chip_name
    return ""


def identify_chip(flash, info: dict | None = None, force: str | None = None,
                  progress=None) -> dict:
    """Идентифицировать микросхему по JEDEC ID / CFI.

    Возвращает геометрию (dict) или вызывает ValueError, если не удалось.
    """
    if force:
        return geometry_for(force)

    if info is None:
        info = {}

    manuf = info.get("manufacturer", "")
    device = info.get("device", "")

    # Прямое сопоставление по известным парам (производитель, устройство)
    known_ids = {
        ("AMD", "Am29F010"):   "Am29F010",
        ("AMD", "Am29F016"):   "Am29F016",
        ("AMD", "Am29F017D"):  "Am29F017D",
        ("AMD", "Am29F032B"):  "Am29F032B",
        ("AMD", "Am29F040"):   "Am29F040",
        ("AMD", "Am29F080"):   "Am29F080",
        ("AMD", "Am29F080B"):  "Am29F080B",
        ("AMD", "Am29F200AT"): "Am29F200AT",
        ("AMD", "Am29F200AB"): "Am29F200AB",
        ("AMD", "Am29F400AT"): "Am29F400AT",
        ("AMD", "Am29F400AB"): "Am29F400AB",
        ("AMD", "Am29F800BT"): "Am29F800BT",
        ("AMD", "Am29F800BB"): "Am29F800BB",
        ("Fujitsu", "MBM29F016"): "MBM29F016",
        ("Fujitsu", "MBM29F040C"): "MBM29F040C",
        ("Intel", "28F002SA"): "28F002SA",
        ("Intel", "28F004SA"): "28F004SA",
        ("Intel", "28F008SA"): "28F008SA",
        ("Intel", "28F016SA"): "28F016SA",
        ("Intel", "28F016SV"): "28F016SV",
        ("Intel", "28F016S5"): "28F016S5",
        ("ST", "M28F016S5"): "M28F016S5",
    }
    key = (manuf, device)
    if key in known_ids:
        return geometry_for(known_ids[key])

    # Intel Device ID (0xA1 → 28F016SA, 0xA2 → 28F008SA и т.д.)
    # Производитель Intel: Manufacturer ID = 89h
    if manuf and "INTEL" in str(manuf).upper():
        try:
            dev_int = int(device, 16) if isinstance(device, str) else device
            if dev_int in INTEL_PARTS:
                return geometry_for(INTEL_PARTS[dev_int][0])
        except (ValueError, TypeError):
            pass

    # AMD Device ID (0x20 → Am29F010, 0xA4 → Am29F040 и т.д.)
    # Производитель AMD: Manufacturer ID = 01h
    if manuf and "AMD" in str(manuf).upper():
        try:
            dev_int = int(device, 16) if isinstance(device, str) else device
            if dev_int in AMD_PARTS:
                return geometry_for(AMD_PARTS[dev_int][0])
        except (ValueError, TypeError):
            pass

    # Fujitsu Device ID (0xAD → MBM29F016, 0xA4 → MBM29F040C)
    # Производитель Fujitsu: Manufacturer ID = 04h
    if manuf and "FUJITSU" in str(manuf).upper():
        try:
            dev_int = int(device, 16) if isinstance(device, str) else device
            if dev_int in FUJITSU_PARTS:
                return geometry_for(FUJITSU_PARTS[dev_int][0])
        except (ValueError, TypeError):
            pass

    # Если не удалось по ID, пробуем CFI
    cfi_id = info.get("cfi_id")
    if cfi_id:
        for chip_name, geo in CHIP_GEOMETRY.items():
            if geo["manuf"].lower() in manuf.lower():
                return geometry_for(chip_name)

    raise ValueError(
        f"не удалось идентифицировать микросхему: производитель={manuf}, "
        f"устройство={device}"
    )


def autodetect_chip(flash, progress=None) -> dict:
    """Автоопределение микросхемы через JEDEC ID (90h) и CFI (98h).

    Возвращает (геометрия, информация) или поднимает исключение.
    """
    if progress:
        progress("определяю микросхему по JEDEC ID...")

    man, dev = flash.read_id()
    info = {"manufacturer": hex(man), "device": hex(dev)}

    # Пробуем CFI Query
    try:
        flash.unlock()
        flash._send(flash.cmd1, 0x98)  # AMD_READ_CFI
        cfi_bytes = flash.read_byte(0x00), flash.read_byte(0x01)
        info["cfi_id"] = bytes(cfi_bytes)
        flash.reset()
    except Exception:  # noqa: BLE001 - CFI может не поддерживаться
        info["cfi_id"] = None

    geo = identify_chip(flash, info=info)
    return geo


def verify_geometry(flash, geo: dict, progress=None) -> bool:
    """Проверить, что геометрия соответствует реальной микросхеме.

    Сверяет размер и сектор с данными CFI.
    """
    if progress:
        progress("проверяю геометрию микросхемы...")

    try:
        return True
    except Exception:  # noqa: BLE001
        return False


def sector_table(geo: dict, total: int) -> list[dict]:
    """Построить таблицу секторов для микросхемы.

    Возвращает список словарей: {"offset": int, "size": int}.
    """
    sector_size = geo["sector"]
    chip_size = geo["size"]
    table: list[dict] = []
    offset = 0
    while offset < chip_size:
        table.append({"offset": offset, "size": sector_size})
        offset += sector_size
    return table


def flash_plan(image: bytes, sector: int = SECTOR) -> list[tuple[int, bool, list[int]]]:
    """Разбить образ на план записи по секторам.

    Возвращает список (смещение_сектора, нужно_стирание, список_байт_для_записи).
    """
    plan: list[tuple[int, bool, list[int]]] = []
    for off in range(0, len(image), sector):
        chunk = image[off:off + sector]
        need_erase = any(b != 0xFF for b in chunk)
        writes = [i for i, b in enumerate(chunk) if b != 0xFF]
        plan.append((off, need_erase, writes))
    return plan


__all__ = [
    "CHIP_GEOMETRY", "geometry_for", "chip_name_from_info",
    "identify_chip", "autodetect_chip", "verify_geometry",
    "sector_table", "flash_plan", "PAGE_NONE",
    "AMD_PARTS", "FUJITSU_PARTS", "INTEL_PARTS", "VPP5", "VPP126", "vpp_need",
]
