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

# Аннотации в этом модуле используют запись `int | None`, которая работает
# только с Python 3.10. Набор поддерживает 3.9 (см. матрицу CI), поэтому
# аннотации не вычисляются: иначе импорт модуля падал бы TypeError.
from __future__ import annotations

from .chip_db import (
    CHIP_GEOMETRY, AMD_PARTS, FUJITSU_PARTS, INTEL_PARTS,
    VPP5, VPP126, PAGE_NONE,
)
from .addressing import chip_abs
from .constants import (
    NOR_CMD_ADDR1, NOR_CMD_ADDR2, SECTOR,
)
from .errors import DirectError


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


class UnsupportedChip(ValueError):
    """Микросхема известна, но этим набором работать с ней нельзя."""


def sector_map(geo: dict) -> list[tuple[int, int]]:
    """Карта секторов ОДНОГО чипа: [(размер, число), ...] от младших адресов.

    У большинства микросхем карта однородная, и она собирается из sector/size.
    У boot-микросхем карта задана в chips.json (16K + 2x8K + 32K + N x 64K, а
    у top-boot то же самое в обратном порядке) либо приходит из CFI, где
    erase-регионы описаны именно так.
    """
    pairs = geo.get("sectors") or []
    if pairs:
        return [(int(size), int(count)) for size, count in pairs]
    sector = int(geo.get("sector") or 0)
    size = int(geo.get("size") or 0)
    if sector > 0 and size > 0 and size % sector == 0:
        return [(sector, size // sector)]
    return []


def check_sector_map(geo: dict) -> dict:
    """Убедиться, что карта секторов сходится с ёмкостью чипа.

    Без карты стирать нельзя: границы сектора вычислялись бы как «ёмкость /
    размер», а у boot-чипа первый сектор 16 КиБ. Стирание «сектора 64 КиБ» по
    адресу 0x8000 задело бы 16K+8K+8K+32K, то есть паспорт карты и соседние
    данные.
    """
    pairs = sector_map(geo)
    size = int(geo.get("size") or 0)
    name = geo.get("name") or "микросхема"
    if not pairs or not size:
        raise UnsupportedChip(
            f"{name}: неизвестна карта секторов (нет ни размера сектора, "
            f"ни списка секторов). Укажите модель ключом --chip."
        )
    total = sum(s * c for s, c in pairs)
    if total != size:
        raise UnsupportedChip(
            f"{name}: карта секторов даёт {total} байт, а ёмкость чипа "
            f"{size} - данные противоречат друг другу, работать нельзя."
        )
    return geo


def sector_at(geo: dict, chip_off: int) -> tuple[int, int]:
    """Границы сектора, содержащего адрес ВНУТРИ чипа: (начало, размер).

    Адрес за концом чипа относится к последнему сектору - что с этим делать,
    решает вызывающий код.
    """
    pairs = sector_map(geo)
    off = 0
    for size, count in pairs:
        if chip_off < off + size * count:
            return off + ((chip_off - off) // size) * size, size
        off += size * count
    if pairs:
        size = pairs[-1][0]
        return off - size, size
    return chip_off, 0


def geometry_for(name: str, size: int | None = None, volts: int | None = None) -> dict:
    """Параметры микросхемы по имени (или по самому длинному совпадению).

    Длинное совпадение важно: «Am29F016D» не должно подходить под «Am29F016»
    (у ревизий отличается поддержка Unlock Bypass), а «Am29LV160DB» - под
    «Am29LV160».
    """
    def finish(geo: dict, exact_name: str) -> dict:
        out = dict(geo)
        if size is not None:
            out["size"] = size
        if volts is not None:
            out["volts"] = volts
        out["name"] = exact_name
        return check_sector_map(out)

    if name in CHIP_GEOMETRY:
        return finish(CHIP_GEOMETRY[name], name)

    up = str(name).strip().upper()
    hits = [(len(chip_name), chip_name)
            for chip_name in CHIP_GEOMETRY
            if len(chip_name) >= 5 and chip_name.upper() in up]
    if hits:
        return finish(CHIP_GEOMETRY[max(hits)[1]], max(hits)[1])

    raise ValueError(f"неизвестная микросхема: {name}")


# --- автоопределение ---------------------------------------------------------
#
# Источников три, и доверять им надо по-разному:
#   1. JEDEC ID (команда 90h) - отвечает сама микросхема. У семейства один код
#      на все ёмкости (Am29F016/Am29F016B/Am29F016D = 01h:ADh), поэтому имя из
#      него выводится, а ёмкость приходится брать из CFI или паспорта;
#   2. CFI (команда 98h) - таблица, которую микросхема отдаёт сама: ёмкость,
#      размер блока, напряжение, тайм-ауты. Отвечают не все (у Am29F016 нет);
#   3. паспорт карты (CIS) - строки, которые пишет производитель карты; они
#      бывают устаревшими, поэтому паспорт - последний источник.
#
# В прежней версии здесь сравнивалось hex-представление ID со строкой "AMD":
# сравнение не выполнялось ни разу, автоопределение всегда падало с ValueError,
# и работал только --chip. Теперь сравниваются числа.

# Код производителя -> (человеческое имя, таблица «код устройства -> имя»).
_VENDORS = {
    0x01: ("AMD", AMD_PARTS),
    0x04: ("Fujitsu", FUJITSU_PARTS),
    0x89: ("Intel", INTEL_PARTS),
}

CFI_QUERY = 0x98
CFI_ADDR = 0x55


def _as_int(value):
    """'0xAD' / 'AD' / 173 -> 173. Что не число - None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text.startswith("0x"):
        text, base = text[2:], 16
    elif any(c in "abcdef" for c in text):
        text, base = text, 16
    else:
        text, base = text, 10
    try:
        return int(text, base)
    except ValueError:
        return None


def _passport_text(info: dict) -> str:
    """Все строки паспорта одной строкой в верхнем регистре."""
    parts = []
    for key in ("product", "manufacturer", "device", "name", "jedec"):
        value = (info or {}).get(key)
        if isinstance(value, (list, tuple)):
            parts.extend(str(v) for v in value)
        elif value:
            parts.append(str(value))
    return " ".join(parts).upper()


def chip_name_from_info(info: dict) -> str:
    """Имя микросхемы по паспорту карты (CIS).

    Берётся САМОЕ ДЛИННОЕ совпадение: «AM29F016D» должно победить «AM29F016»,
    иначе ёмкость и число секторов будут от другой ревизии.
    """
    text = _passport_text(info)
    if not text:
        return ""
    best = ""
    for name in CHIP_GEOMETRY:
        if len(name) >= 5 and name.upper() in text and len(name) > len(best):
            best = name
    return best


def read_cfi(flash) -> dict:
    """Прочитать общую таблицу CFI (команда 98h). Пустой dict - CFI нет.

    Раскладка полей - CFI Publication 100 (таблица 5). Проверено по ядру
    Linux: include/linux/mtd/cfi.h (struct cfi_ident) и
    drivers/mtd/chips/cfi_probe.c, где CFI читается с адреса 0x10:

        0x10-0x12  "QRY" - признак CFI;
        0x13-0x14  основной набор команд (01h = Intel, 02h = AMD/Fujitsu);
        0x15-0x16  адрес расширенной таблицы этого набора;
        0x1B-0x1E  Vcc/Vpp min/max при записи, единица 100 мВ;
        0x1F-0x26  тайм-ауты (тип./макс.) для записи слова, буфера,
                   стирания сектора и чипа - степень двойки в микросекундах;
        0x27       ёмкость ОДНОГО чипа: 2^n байт;
        0x28-0x29  разрядность шины: 1 = x8, 2 = x16, 3 = x8/x16;
        0x2A-0x2B  буферная запись: 2^n байт (0 - буфера нет);
        0x2C       число erase-регионов;
        0x2D-0x30  регион 1: младшие 16 бит = число блоков - 1,
                   старшие 16 бит = размер блока в единицах 256 байт.

    ВНИМАНИЕ: в монолите windows/legacy/pcmcia_direct.py те же поля читаются
    со сдвигом на байт (номер региона принимается за размер сектора), из-за
    чего ёмкость из CFI там получается мусорной. Здесь - по таблице выше.
    """
    out: dict = {}
    flash._send(CFI_ADDR, CFI_QUERY)

    def q(off):
        return flash.read_byte(chip_abs(off, 0, flash.chips))

    try:
        if q(0x10) != 0x51 or q(0x11) != 0x52 or q(0x12) != 0x59:   # "QRY"
            return {}
        cmdset = q(0x13) | (q(0x14) << 8)
        out["cmdset"] = cmdset
        out["cmdset_name"] = {0x0001: "Intel", 0x0002: "AMD/Fujitsu"}.get(
            cmdset, f"неизвестный ({cmdset:#06x})")
        if q(0x27):
            out["size"] = 1 << q(0x27)             # байт в ОДНОМ чипе
        iface = q(0x28) | (q(0x29) << 8)
        out["bus_width"] = {0x01: "x8", 0x02: "x16", 0x03: "x8/x16"}.get(
            iface, f"неизвестно ({iface:#06x})")
        exponent = q(0x2A) | (q(0x2B) << 8)
        out["buf_write"] = (1 << exponent) if 0 < exponent <= 9 else 0
        out["vcc_write_mv"] = (q(0x1B) * 100, q(0x1C) * 100)
        out["vpp_write_mv"] = (q(0x1D) * 100, q(0x1E) * 100)
        out["t_word_us"] = 1 << min(q(0x1F), 20)
        out["t_sector_erase_us"] = 1 << min(q(0x21), 24)
        out["t_chip_erase_us"] = 1 << min(q(0x22), 26)
        regions = []
        base = 0x2D
        for _ in range(min(q(0x2C), 4)):
            info32 = (q(base) | (q(base + 1) << 8) |
                      (q(base + 2) << 16) | (q(base + 3) << 24))
            regions.append({"blocks": (info32 & 0xFFFF) + 1,
                            "size": (info32 >> 16) * 256})
            base += 4
        out["regions"] = regions
        if len(regions) == 1:
            out["sector"] = regions[0]["size"]      # у однородных карт - он же
        elif regions:
            # CFI сам описывает неоднородную карту: (размер, число блоков).
            out["sectors"] = [(reg["size"], reg["blocks"]) for reg in regions]
    finally:
        flash.reset()
    return out


def _geometry_from_cfi(cfi: dict, size: int = 0) -> dict | None:
    """Геометрия по данным CFI: по ёмкости и размеру блока.

    Если таблица не знает такую микросхему - строим геометрию из того, что
    сказала она сама (протокол JEDEC, байтовые команды 0x555/0x2AA - это
    адреса команд AMD/Fujitsu, других в CFI с набором 02h не бывает).
    """
    chip_size = int(cfi.get("size") or 0)
    sector = int(cfi.get("sector") or 0)
    if not chip_size:
        return None
    hits = []
    for name, geo in CHIP_GEOMETRY.items():
        if int(geo.get("size") or 0) != chip_size:
            continue
        if sector and int(geo.get("sector") or 0) != sector:
            continue
        hits.append((name, geo))
    # Однородные 2-МБ 64-КиБ чипы есть у десятка производителей; берём первый,
    # но помечаем в отчёте, что имя - семейное, а не точное.
    for name, geo in hits:
        out = dict(geo)
        out["name"] = name
        out["name_exact"] = False
        return out
    if not sector:
        return None
    out = {
        "name": f"CFI {chip_size // 1024} КиБ",
        "name_exact": False,
        "size": chip_size,
        "sector": sector,
        "cmd": (NOR_CMD_ADDR1, NOR_CMD_ADDR2),
        "wide": False,
        "page": int(cfi.get("buf_write") or 1) or 1,
        "bypass": False,
        "strata": False,
        "protocol": "jedec",
        "volts": (cfi.get("vcc_write_mv") or (0, 0))[1] // 1000 or None,
        "manuf": "",
    }
    if cfi.get("sectors"):
        out["sectors"] = list(cfi["sectors"])       # карта из CFI, в байтах
    return out


def _read_ids(flash) -> dict:
    """Сырые ответы микросхемы: JEDEC ID и таблица CFI."""
    out: dict = {}
    try:
        man, dev = flash.read_id()
        if man is not None:
            out["manuf"], out["device"] = man, dev
    except (DirectError, OSError, ValueError, IndexError):
        pass
    try:
        cfi = read_cfi(flash)
        if cfi:
            out["cfi"] = cfi
    except (DirectError, OSError, ValueError, IndexError):
        pass
    return out


def identify_chip(flash, info: dict | None = None, force: str | None = None,
                  progress=None) -> dict:
    """Что за микросхема. Порядок: --chip, паспорт, JEDEC ID, CFI.

    Возвращает геометрию из CHIP_GEOMETRY, дополненную ключами:
    "name" (имя), "how" (откуда взято) и "autodetect" (сырые ID и CFI).
    Если не ответил никто - ValueError с объяснением, что делать.
    """
    info = info or {}
    size = 0
    devices = info.get("devices") or []
    if devices:
        size = devices[0].get("size") or 0

    if force:
        geo = geometry_for(force, size=size or None)
        geo["name"] = force
        geo["how"] = "указано ключом --chip"
        geo.setdefault("autodetect", {})
        return geo

    name = chip_name_from_info(info)
    if name:
        geo = geometry_for(name, size=size or None)
        geo["name"] = name
        geo["how"] = "строка паспорта карты"
        geo.setdefault("autodetect", {})
        return geo

    if progress:
        progress("спрашиваю микросхему: JEDEC ID (90h) и CFI (98h)")
    auto = _read_ids(flash)
    man, dev = auto.get("manuf"), auto.get("device")
    if man is None:
        # Микросхема не ответила на 90h (бывает, когда команды в обход
        # контроллера не проходят), но JEDEC ID может быть в паспорте карты.
        man, dev = _as_int(info.get("manufacturer")), _as_int(info.get("device"))
        if man is not None:
            auto["manuf"], auto["device"] = man, dev
            auto["id_source"] = "паспорт карты (микросхема на 90h не ответила)"
    vendor = _VENDORS.get(man if man is not None else -1)
    if vendor and dev in vendor[1]:
        name = vendor[1][dev][0]
        geo = geometry_for(name, size=size or None)
        geo["name"] = name
        geo["how"] = f"JEDEC ID {man:#04x}:{dev:#04x} ({vendor[0]})"
        geo["autodetect"] = auto
        return geo

    cfi = auto.get("cfi") or {}
    geo = _geometry_from_cfi(cfi, size=size)
    if geo:
        geo["how"] = "таблица CFI (98h): ёмкость и размер блока"
        geo["autodetect"] = auto
        return geo

    answered = "нет ответа" if man is None else f"{man:#04x}:{dev:#04x}"
    raise ValueError(
        f"микросхема не ответила: JEDEC ID {answered}, CFI "
        f"{'есть, но размеров не даёт' if cfi else 'не отвечает'}. "
        f"Укажите модель ключом --chip (список: pcmcia-flash chips)"
    )


def autodetect_chip(flash, progress=None) -> dict:
    """Автоопределение микросхемы: JEDEC ID и CFI, без паспорта карты."""
    if progress:
        progress("определяю микросхему: JEDEC ID и CFI")
    geo = identify_chip(flash, info={})
    return geo


def _program_restore(flash, off: int, was: bytes) -> str:
    """Вернуть в сектор прежние байты. Возвращает описание, что вышло.

    Писать можно только туда, где сейчас 0xFF: стереть байт в 0 нельзя, а
    «записать поверх» NOR-память не умеет - запись только сбрасывает единицы
    в нули. Поэтому восстанавливаем побайтово и честно сообщаем, если
    что-то вернуть не удалось.
    """
    lost = 0
    for i, b in enumerate(was):
        if b == 0xFF:
            continue
        cur = flash.read_byte(off + i)
        if cur == b:
            continue
        if cur != 0xFF:
            lost += 1
            continue
        ok, why = flash.program_byte(off + i, b)
        if not ok:
            lost += 1
    if lost:
        return f"вернуть удалось не всё: {lost} байт не восстановлены"
    got = flash.read(off, len(was))
    if got != was:
        diff = sum(1 for a, b in zip(got, was) if a != b)
        return f"после возврата не совпадает {diff} байт"
    return "сектор возвращён как был"


def verify_geometry(flash, geo: dict, progress=None,
                    backup_path=None) -> tuple[bool, str]:
    """Проверить ДО записи, что микросхема и её геометрия те, что мы думаем.

    Проверка активная: пассивно этого не увидеть. Ошибка «взяли 64 КиБ вместо
    16 КиБ» проявляется только стиранием - и, если её не заметить, стирание
    задевает соседние данные.

    Порядок и почему так:
      1. пробуем ПОСЛЕДНИЙ сектор карты - там обычно меньше всего данных;
      2. СНИМАЕМ его в память (и, если задан backup_path, в файл) ДО стирания:
         проверка не должна ничего уничтожать;
      3. стираем и проверяем, что в секторе только 0xFF;
      4. возвращаем снимок обратно и сверяем побайтово;
      5. если сектор не стёрся - тоже пытаемся вернуть данные и говорим об
         этом прямо, а не «проверка не прошла».

    Прежняя версия стирала последний сектор и не возвращала его (данные
    пропадали), либо возвращала только при --snapshot-dir, да и то с ошибкой.

    Возвращает (ok, почему). Никогда не оставляет сектор стёртым молча.
    """
    sector = int(geo.get("sector") or 0)
    limit = int(geo.get("size") or 0)
    if sector <= 0:
        return True, "размер сектора неизвестен - проверка пропущена"
    if limit < sector:
        return True, (f"ёмкость карты неизвестна или меньше сектора "
                      f"({limit} < {sector}) - проверка пропущена")
    off = (limit - sector)
    off -= off % sector
    if progress:
        progress(f"проверяю геометрию: сектор по адресу {off:#x}")

    was = flash.read(off, sector)
    if len(was) != sector:
        return False, (f"по адресу {off:#x} прочиталось {len(was)} байт "
                       f"вместо {sector}: карта меньше, чем мы думаем")
    if backup_path:
        try:
            with open(backup_path, "wb") as f:
                f.write(was)
        except OSError as exc:
            return False, f"не смог сохранить копию сектора: {exc}"

    blank = all(b == 0xFF for b in was)
    ok, why = flash.erase_sector(off)
    if not ok:
        back = "" if blank else _program_restore(flash, off, was)
        return False, (f"сектор {sector // 1024} КиБ по адресу {off:#x} "
                       f"не стёрся: {why}" + (f"; {back}" if back else ""))

    data = flash.read(off, sector)
    bad = next((i for i, b in enumerate(data) if b != 0xFF), None)
    if bad is not None:
        back = "" if blank else _program_restore(flash, off, was)
        return False, (f"после стирания байт {off + bad:#x} читается "
                       f"{data[bad]:#04x} вместо 0xFF: сектор не "
                       f"{sector // 1024} КиБ, стирание не идёт (Vpp?) или "
                       f"это не та микросхема"
                       + (f"; {back}" if back else ""))

    if blank:
        return True, (f"сектор {sector // 1024} КиБ по адресу {off:#x} "
                      f"стирается (был пуст, возвращать нечего)")
    back = _program_restore(flash, off, was)
    if back != "сектор возвращён как был":
        return False, (f"сектор стирается, но данные вернуть не удалось: {back}. "
                       f"Копия сектора "
                       + (f"в {backup_path}" if backup_path else "потеряна"))
    return True, (f"сектор {sector // 1024} КиБ стирается по адресу {off:#x}, "
                  f"данные возвращены")


def sector_table(geo: dict, total: int, chips: int = 1) -> list[dict]:
    """Таблица секторов карты: список {"offset": int, "size": int}.

    total - ёмкость КАРТЫ в байтах (geo["size"] - ёмкость одного чипа).
    Раньше total игнорировался и таблица всегда строилась по ёмкости чипа,
    то есть вдвое короче для пары - по такой таблице последние секторы карты
    не попали бы в план записи. Последний сектор урезается по total.

    Границы берутся из карты секторов (sector_map), а размеры умножаются на
    число чипов: байты карты разложены по чипам, поэтому сектор 8 КиБ одного
    чипа занимает 16 КиБ карты у пары. Для однородной микросхемы при chips=1
    результат прежний.
    """
    limit = int(total or geo.get("size") or 0)
    pairs = sector_map(geo)
    if not pairs or limit <= 0:
        return []
    scale = max(int(chips or 1), 1)
    table: list[dict] = []
    offset = 0
    while offset < limit:
        # карта повторяется, если total больше одной микросхемы: так же вёл
        # себя прежний «однородный» вариант, когда звали без chips
        step = 0
        for size, count in pairs:
            size *= scale
            for _ in range(count):
                if offset >= limit:
                    return table
                table.append({"offset": offset,
                              "size": min(size, limit - offset)})
                offset += size
                step += size
        if not step:                     # карта нулевая - не зацикливаемся
            break
    return table


def flash_plan(image: bytes, sector: int = SECTOR,
               spans=None) -> list[tuple[int, bool, list[int]]]:
    """Разбить образ на план записи по секторам.

    Возвращает список (смещение_сектора, нужно_стирание, список_байт_для_записи).
    spans - готовый список границ [(начало, размер)] для карт с секторами
    разного размера (см. NorFlash.spans); если не задан, сектор один на всех.
    """
    chunks = spans if spans is not None else [
        (off, min(sector, len(image) - off))
        for off in range(0, len(image), sector)]
    plan: list[tuple[int, bool, list[int]]] = []
    for off, size in chunks:
        if off >= len(image):
            break
        chunk = image[off:off + size]
        need_erase = any(b != 0xFF for b in chunk)
        writes = [i for i, b in enumerate(chunk) if b != 0xFF]
        plan.append((off, need_erase, writes))
    return plan


__all__ = [
    "CHIP_GEOMETRY", "geometry_for", "sector_map", "sector_at",
    "check_sector_map", "UnsupportedChip",
    "chip_name_from_info", "read_cfi",
    "identify_chip", "autodetect_chip", "verify_geometry",
    "sector_table", "flash_plan", "PAGE_NONE",
    "AMD_PARTS", "FUJITSU_PARTS", "INTEL_PARTS", "VPP5", "VPP126", "vpp_need",
]
