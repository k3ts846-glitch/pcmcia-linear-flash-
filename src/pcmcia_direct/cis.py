# -*- coding: utf-8 -*-
"""CIS карты: разбор цепочек, паспорт, отчёт о том, что карта о себе говорит."""

from .bridge import BRIDGE_IDS, CIS_MAX, _bridge_ctx, _read_page, _read_via_sysfs, choose_window, looks_blank, mem_for_addr, open_best_mem, read_mem, restore_cis_window, setup_cis_window
from .chips import vendor_part
from .chips_data import VENDORS
from .errors import DirectError
from .nor_commands import SECTOR


CIS_TAG = b"CIS"                 # маркер цепочки CIS: CISTPL_LINKTARGET "CIS"


CIS_KNOWN = {0x01, 0x03, 0x04, 0x05, 0x13, 0x15, 0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1C,
             0x20, 0x21, 0x22}              # 4 КиБ — как карта CIS у ядра (map_size)


TUPLE_NAMES = {0x01: "CISTPL_DEVICE", 0x15: "CISTPL_VERS_1", 0x17: "CISTPL_DEVICE_A",
               0x18: "CISTPL_JEDEC_C", 0x19: "CISTPL_JEDEC_A", 0x1A: "CISTPL_CONFIG",
               0x1B: "CISTPL_CFTABLE", 0x20: "CISTPL_MANFID", 0x21: "CISTPL_FUNCID",
               0x22: "CISTPL_FUNCE", 0x1C: "CISTPL_CFTABLE_ENTRY",
               0x1E: "CISTPL_DEVICEGEO", 0x1F: "CISTPL_DEVICEGEO_A",
               0x40: "CISTPL_VERS_2", 0x41: "CISTPL_FORMAT", 0x42: "CISTPL_GEOMETRY",
               0x43: "CISTPL_BYTEORDER", 0x44: "CISTPL_DATE", 0x45: "CISTPL_BATTERY",
               0x46: "CISTPL_ORG", 0x47: "CISTPL_FORMAT_A",
               0x04: "CISTPL_LONGLINK_A", 0x03: "CISTPL_LONGLINK_C",
               0x05: "CISTPL_LINKTARGET", 0x13: "CISTPL_LINKTARGET"}


# Коды типов памяти в CISTPL_DEVICE/DEVICE_A (PC Card Standard, таблица 3-11).
CIS_DEV_TYPES = {0: "нет устройства", 1: "ROM", 2: "OTPROM", 3: "EPROM",
                 4: "EEPROM", 5: "FLASH", 6: "SRAM", 7: "DRAM"}


CIS_DEV_SPEED = {0: "нет данных", 1: "250 нс", 2: "200 нс", 3: "150 нс",
                 4: "100 нс", 5: "70 нс", 6: "60 нс", 7: "расширенная скорость"}


def cis_device_entries(body):
    """Записи Device Info из кортежа 01h/17h: тип, скорость, размер, WP.

    Формат (PC Card Standard, 3.2.2): байт типа/скорости, затем байт размера.
    Размер = ((size >> 3) + 1) * (512 << (2 * (size & 7))) байт, единицы -
    блоки по 512 байт. Бит 3 байта типа - устройство имеет свой write-protect
    (переключатель на корпусе), и это видно ещё до всякой записи.
    """
    devs, j = [], 0
    while j + 1 < len(body) and body[j] != 0xFF and len(devs) < 4:
        dt, sz = body[j], body[j + 1]
        scale = sz & 7
        if scale == 7:                     # расширенный размер: не разбираем
            break
        devs.append({"type": dt >> 4,
                     "type_name": CIS_DEV_TYPES.get(dt >> 4, f"тип {dt >> 4}"),
                     "speed": CIS_DEV_SPEED.get((dt >> 1) & 7, "?"),
                     "wp": bool(dt & 0x08),
                     "size": ((sz >> 3) + 1) * (512 << (scale * 2))})
        j += 2
    return devs


def _tuples_from(data):
    """Разбирает поток как обычную цепочку CIS (код, длина, данные)."""
    names = TUPLE_NAMES
    out = []
    i = 0
    while i + 1 < len(data) and len(out) < 64:
        code = data[i]
        if code in (0x00, 0xFF):        # 0xFF - конец цепочки, 0x00 - мусор
            break
        length = data[i + 1]
        body = data[i + 2:i + 2 + length]
        if len(body) < length:
            out.append({"code": code, "name": names.get(code, f"0x{code:02x}"),
                        "len": length, "truncated": True, "body": body})
            break
        out.append({"code": code, "name": names.get(code, f"0x{code:02x}"),
                    "len": length, "body": body})
        i += 2 + length
    return out


def scan_cis(data, max_start=96):
    """Ищет настоящую цепочку CIS внутри сырого чтения attribute memory.

    Зачем: 16-битная карта отдаёт паспорт, но в потоке бывает "лишний" байт
    между байтами CIS (0x00/0xFF), а перед началом цепочки - неинициализированные
    байты. Прямой разбор с нулевого смещения на этом ломается (видели в отчёте:
    "00 00 03 00 52 00 7e 00 ff 00 18 00 ..." - кортежей 0).

    Перебираются: ширина (1 байт / каждый 2-й с чётного / с нечётного) и
    смещение начала. Лучший вариант - где больше осмысленных кортежей
    (MANFID, VERS_1, FUNCID, JEDEC и т.п.) и цепочка длиннее.

    Возвращает (tuples, info) где info = {"stride": 1|2, "parity": 0|1,
    "offset": смещение в исходных байтах, "score": ...}.
    """
    best = None
    for stride, parity in ((1, 0), (2, 0), (2, 1)):
        buf = data if stride == 1 else data[parity::stride]
        for start in range(0, min(max_start, len(buf) - 2)):
            tuples = []
            i = start
            while i + 1 < len(buf) and len(tuples) < 64:
                code = buf[i]
                length = buf[i + 1]
                if code in (0x00, 0xFF):
                    break
                if i + 2 + length > len(buf):
                    break
                body = buf[i + 2:i + 2 + length]
                # кортеж с паршивым телом не принимаем: так отсекаются ложные
                # начала, где "кодом" оказывается случайный байт вроде 0x7e
                if code == 0x20 and not _valid_manfid(body):
                    break
                if code == 0x15 and length < 4:
                    break
                if code in TUPLE_NAMES and length == 0 and code not in (0x13,):
                    break
                tuples.append({"code": code,
                               "name": TUPLE_NAMES.get(code, f"0x{code:02x}"),
                               "len": length, "body": body})
                i += 2 + length
            if not tuples:
                continue
            score = _cis_score(tuples)
            if score <= 0:
                continue
            # предпочитаем настоящие кортежи PC Card (0x01..0x22) мусорным
            # кодам вроде 0x1e/0x52, которые тоже "разбираются"
            known = sum(1 for t in tuples if t["code"] in CIS_KNOWN)
            total_len = sum(t["len"] for t in tuples)
            key = (known, score, total_len, len(tuples), -start)
            if best is None or key > best[0]:
                best = (key, tuples, {"stride": stride, "parity": parity,
                                      "offset": start * stride + parity,
                                      "score": score})
    if best is None:
        return [], {"stride": 0, "parity": 0, "offset": -1, "score": 0}
    return best[1], best[2]


def _valid_manfid(body):
    """MANFID: 4 байта, и это не текст (0x6962/0x2074 = "bi"/"t ")."""
    if len(body) < 4:
        return False
    return not _mostly_ascii(body[:4])


def passport_ok(info):
    """Есть ли РЕАЛЬНЫЙ паспорт: название, MANFID или DEVICE."""
    return bool(info.get("product") or info.get("manfid") or info.get("devices"))


def _cis_score(tuples):
    """Насколько содержимое похоже на настоящий CIS."""
    info = cis_summary(tuples)
    return (10 * bool(info["product"]) + 8 * bool(info["manfid"])
            + 6 * bool(info["devices"]) + 4 * bool(info["func"])
            + 2 * bool(info["jedec"])
            + sum(1 for t in tuples if t["code"] in CIS_KNOWN and t["len"] > 0))


def parse_cis(data, verbose=False):
    """Разбирает CIS, подбирая правильную ширину шины.

    16-битная карта отдаёт байт CIS на линиях D0..D7, но при чтении 8-битным
    окном соседний байт (D8..D15) попадает в поток как 0x00 (или 0xFF).
    Поэтому нужно ПРОПУСКАТЬ лишний байт, иначе кортежи "разъезжаются":
    в отчёте это выглядело как "00 00 03 00 52 00 7e 00 ff 00 18 00 ...",
    где настоящий CIS - каждый второй байт (03 52 7e ff 18 03 01 ad ff 1e 07
    02 11 01 01 01 01 ff 15 54 04 01 43 65 6e 74 65 6e 6e 69 61 ...).

    Пробуем: как есть, чётные байты, нечётные байты - и берём вариант с
    наибольшим числом осмысленных кортежей.
    """
    cands = [("как есть", data), ("чётные байты", data[0::2]), ("нечётные байты", data[1::2])]
    best = None
    for how, buf in cands:
        t = _tuples_from(buf)
        sc = _cis_score(t)
        if best is None or sc > best[0]:
            best = (sc, how, t)
    sc, how, tuples = best
    if verbose and how != "как есть":
        print(f"  ширина шины: поток читается {how} (лишний байт между байтами CIS)")
    return tuples


def _dump_rest(data, frm, info=None):
    """Хвост дампа: длинные пустые участки не печатаем."""
    out = []
    rest = data[frm:]
    interesting = [b for b in rest if b not in (0x00, 0xFF)]
    if not rest:
        return out
    if not interesting:
        out.append(f"... далее {len(rest)} байт пусто (00/FF) - не печатаю")
    else:
        uniq = sorted(set(interesting))[:8]
        out.append(f"... далее {len(rest)} байт, непустых {len(interesting)} "
                   f"(значения: {', '.join(f'{b:02x}' for b in uniq)}) - "
                   "см. раздел 7.1, если нужно")
    return out


def cis_extra_report(data, info, limit=12):
    """Сырые факты об attribute memory: где цепочка, что после неё.

    Нужно, когда разбор спорный. Печатает МАЛО и по делу: смещение маркера
    "CIS", первые строки паспорта и всё непустое после цепочки.
    """
    out = []
    i = data.find(b"\x43\x49\x53")          # "CIS" - CISTPL_LINKTARGET
    if i >= 0:
        out.append(f"маркер 'CIS' (LINKTARGET) со смещения {i}")
    else:
        out.append("маркера 'CIS' нет - цепочка без LINKTARGET (бывает)")
    out.append(f"ширина шины: каждый {info.get('stride', 1)}-й байт, "
               f"начало цепочки {info.get('offset', -1)}")
    # строки из первой части (паспорт всегда в начале attribute memory)
    head = data[:512]
    strs = []
    for stride, par in ((1, 0), (2, 0)):
        buf = head if stride == 1 else head[par::2]
        for x in _printable_runs(buf, 4):
            if x not in strs and not x.startswith("CIS"):
                strs.append(x)
    if strs:
        out.append("строки паспорта:")
        for x in strs[:limit]:
            out.append(f"  {x}")
    # что после цепочки: перечисляем непустые байты (hex), их немного
    tail_from = min(len(data), 512)
    tail = [(k, b) for k, b in enumerate(data[tail_from:], start=tail_from)
            if b not in (0x00, 0xFF)]
    out.append(f"после 512-го байта: непустых {len(tail)} байт")
    if tail:
        for k, b in tail[:24]:
            out.append(f"  {k:04x}: {b:02x}")
        if len(tail) > 24:
            out.append(f"  ... и ещё {len(tail) - 24}")
    return out


def cis_location(info):
    """Где лежит паспорт карты: в отдельной памяти или в блоке 0 массива.

    Это не мелочь: у части карт (Series 5 на 28F016S5, часть Smart/MagicRAM)
    отдельной паспортной EEPROM нет, и CIS записан в ПЕРВЫЙ блок флеша. Такая
    карта неотличима от обычной, пока не сотрёшь блок 0 - и тогда паспорт
    теряется навсегда. Поэтому перед записью набор говорит об этом прямо и
    требует явного подтверждения для блока 0.
    """
    attr = (info or {}).get("attr_devices") or []
    if attr:
        d = attr[0]
        return ("attr", f"в отдельной паспортной памяти ({d.get('type_name')}, "
                        f"{d.get('size', 0)} байт)")
    if (info or {}).get("product") or (info or {}).get("manfid"):
        return ("array", "в ПЕРВОМ БЛОКЕ массива флеша (отдельной памяти в "
                         "паспорте нет): стирание блока 0 сотрёт паспорт")
    return ("unknown", "неизвестно (паспорт не разобран)")


def cis_erased_by_block0(info, flash, offset, sector=None):
    """Попадёт ли стирание сектора offset на блок с паспортом."""
    where, _why = cis_location(info)
    sec = sector or getattr(flash, "sector", SECTOR)
    return where == "array" and offset < sec


def cis_notes(info, tuples=None):
    """Что паспорт говорит о носителе — словами, а не кодами.

    Возвращает список (уровень, текст), уровень: "info" или "warn". Нужно для
    двух вещей: человек не должен сам складывать DEVICE + BATTERY + JEDEC_A,
    и набор не должен молча писать на карту, у которой по паспорту есть
    переключатель защиты от записи или паспорт в ROM.
    """
    out = []
    kinds = {(d or {}).get("type_name") for d in (info or {}).get("devices") or []}
    wp = any((d or {}).get("wp") for d in (info or {}).get("devices") or [])
    batt = (info or {}).get("battery") or {}
    attr = (info or {}).get("attr_devices") or []
    has_cis = bool((info or {}).get("product") or (info or {}).get("manfid"))

    if "FLASH" in kinds:
        out.append(("info", "носитель: линейная флеш (в паспорте CISTPL_DEVICE "
                            "тип FLASH). Энергонезависима, батарея не нужна"))
    elif "SRAM" in kinds or "DRAM" in kinds:
        out.append(("warn", "носитель: SRAM/DRAM с батареей. Данные держатся "
                            "только пока жива батарея; при её замене содержимое "
                            "теряется. Это не флеш: стирания секторами нет"))
    elif "ROM" in kinds or "EPROM" in kinds or "OTPROM" in kinds:
        out.append(("warn", "носитель: ROM/EPROM/OTPROM - только чтение. "
                            "Записывать нечего и нечем"))
    elif "EEPROM" in kinds:
        out.append(("info", "носитель: EEPROM (перезаписывается побайтно)"))
    elif kinds:
        out.append(("info", "носитель по паспорту: " + ", ".join(sorted(kinds))))
    else:
        out.append(("warn", "тип носителя в паспорте не указан (нет CISTPL_DEVICE)"))

    if wp:
        out.append(("warn", "в паспорте отмечен переключатель защиты от записи "
                            "(WPS). Проверьте его на корпусе карты: при защите "
                            "запись и стирание не пройдут, и это не дефект"))
    if batt:
        out.append(("warn" if batt.get("present") else "info",
                    "батарея: " + str(batt.get("kind"))))
    for d in attr:
        if d.get("type") == 1:
            out.append(("warn", "паспортная память (CIS) - ROM: сам паспорт "
                                "переписать нельзя, даже если массив пишется"))
        else:
            out.append(("info", f"паспортная память: {d.get('type_name')} "
                                f"{d.get('size', 0)} байт (перезаписываемая)"))
    if not attr and has_cis:
        out.append(("info", "отдельной паспортной памяти в CIS нет: у таких карт "
                            "(например, Series 5 на 28F016S5) паспорт лежит в "
                            "первом блоке массива, и стирание блока 0 сотрёт его"))
    geo = (info or {}).get("geometry") or {}
    if geo.get("sector_shift"):
        out.append(("info", f"геометрия из паспорта: сектор "
                            f"{1 << geo['sector_shift']} байт, микросхема "
                            f"{1 << geo['chip_shift']} байт"))
    return out


def cis_capacity(info, strings):
    """Оценка типа и ёмкости по паспорту (строки VERS_1 + JEDEC ID).

    Возвращает список строк для отчёта. Не гадаем сверх того, что написано в
    паспорте: берём числа из строк и производителя из JEDEC.
    """
    out = []
    text = " ".join(list(info.get("product") or []) + list(strings or []))
    low = text.lower()
    kinds = [("flash", "FLASH (линейная флеш)"), ("sram", "SRAM"),
             ("eeprom", "EEPROM"), ("dram", "DRAM"), ("rom", "ROM")]
    for key, name in kinds:
        if key in low:
            out.append(f"тип памяти по паспорту : {name}")
            break
    import re as _re
    mbits = []
    for num, unit in _re.findall(r"(\d+)\s*(meg|mbit|мбит|мег|m\b)", low):
        if unit in ("meg", "mbit", "мбит", "мег", "m"):
            mbits.append(int(num))
    if mbits:
        cap = max(mbits)
        out.append(f"ёмкость (из паспорта)  : {cap} Мбит = {cap / 8:g} МиБ")
    j = info.get("jedec")
    if j and len(j) >= 4:
        try:
            vid, did = int(j[0:2], 16), int(j[2:4], 16)
        except ValueError:
            vid = did = None
        if vid in VENDORS:
            line = f"чипы (JEDEC 0x{vid:02x})    : {VENDORS[vid]}"
            info_part = vendor_part(vid, did)
            if info_part:
                part, mbit = info_part
                line += f" {part} ({mbit} Мбит)"
                if mbits and mbit:
                    n = max(1, round(max(mbits) / mbit))
                    line += f", всего {n} шт."
            out.append(line)
    return out


def cis_dump(data, info=None, head=64, window=160):
    """Компактный дамп attribute memory: начало + окно с цепочкой.

    Полная печать 4 КиБ превращала отчёт в полотно "ff 00 ff 00 ...": там нет
    информации. Показываем 64 байта с начала и 160 байт от начала цепочки,
    а длинные однородные участки заменяем строкой "далее ...".
    """
    lines = []
    for off in range(0, min(head, len(data)), 32):
        row = data[off:off + 32]
        txt = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        lines.append(f"{off:04x}  {row.hex(' '):<96}  |{txt}|")
    start = 0
    if info:
        start = info.get("offset", 0) or 0
    beg = max(0, start)
    if start > head:
        lines.append(f"... (до начала цепочки {start} байт)")
    if beg and beg < head:
        # окно уже показано выше - не дублируем, продолжаем с head+window
        return lines + _dump_rest(data, head + window, info)
    if beg < head:
        beg = head          # уже напечатано выше
    for off in range(beg, min(beg + window, len(data)), 32):
        row = data[off:off + 32]
        txt = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        lines.append(f"{off:04x}  {row.hex(' '):<96}  |{txt}|")
    return lines + _dump_rest(data, beg + window, info)


def _printable_runs(buf, minlen=3, limit=16):
    """Печатные строки в буфере (для нестандартных VERS_1 и сырых дампов)."""
    out, cur = [], []
    for b in buf:
        if 32 <= b < 127:
            cur.append(chr(b))
        else:
            if len(cur) >= minlen:
                out.append("".join(cur).strip())
            cur = []
    if len(cur) >= minlen:
        out.append("".join(cur).strip())
    return [x for x in out if len(x) >= minlen][:limit]


def _mostly_ascii(buf, minlen=4):
    """Похоже ли содержимое на текст (а не на двоичные идентификаторы)."""
    if not buf:
        return False
    printable = sum(1 for b in buf if 32 <= b < 127)
    return printable >= max(minlen, int(len(buf) * 0.7))


def cis_summary(tuples):
    """Человеческое описание: производитель, продукт, тип, размеры."""
    res = {"manfid": None, "product": None, "func": None, "jedec": None,
           "jedec_a": None, "devices": [], "attr_devices": [], "blank": None,
           "vers": None, "strings": [], "mbits": None, "geometry": None,
           "battery": None, "org": None, "vers2": None}
    for t in tuples:
        b, code = t["body"], t["code"]
        if code == 0x20 and len(b) >= 4:                       # MANFID
            manf, card = b[0] | (b[1] << 8), b[2] | (b[3] << 8)
            # 0x6962 = "bi", 0x2074 = "t " - так выглядит ТЕКСТ, ошибочно
            # принятый за MANFID. Такие значения не принимаем.
            if not (_mostly_ascii(b[:4]) and (manf > 0x0FFF or card > 0x0FFF)):
                res["manfid"] = (manf, card)
        elif code == 0x15 and len(b) >= 3:                     # VERS_1
            # формат: major, minor, затем смещения строк внутри кортежа
            n = b[2]
            strs = []
            for k in range(min(n, 4)):
                pos = 3 + k
                if pos >= len(b):
                    break
                ofs = b[pos]
                if ofs >= len(b):          # смещение за пределами кортежа
                    continue
                end = b.find(b"\x00", ofs)
                if end < 0:
                    end = len(b)
                txt = b[ofs:end].decode("ascii", "replace").strip()
                if txt:
                    strs.append(txt)
            if not strs:
                # на живой карте таблица строк VERS_1 оказалась нестандартной
                # (после major/minor сразу текст), поэтому берём печатные
                # последовательности из тела кортежа
                strs = [x for x in _printable_runs(b, 3) if x.upper() != "CIS"]
            if strs:
                res["product"] = strs
                if 0 < b[0] < 16 and b[1] < 16:
                    res["vers"] = (b[0], b[1])
        elif code == 0x21 and b:                               # FUNCID
            res["func"] = b[0]
        elif code == 0x18 and len(b) >= 2:                     # JEDEC_C (ID)
            res["jedec"] = b[:4].hex()
        elif code == 0x19 and len(b) >= 2:                     # JEDEC_A (память CIS)
            # пара байт = идентификатор микросхемы attribute memory: по ней
            # видно, EEPROM там (перезаписывается) или ROM (только чтение)
            res["jedec_a"] = b[:4].hex()
        elif code == 0x1E and len(b) >= 3:                     # DEVICEGEO
            # геометрия массива: 2^x байт на сектор/карту/чип и число чипов
            res["geometry"] = {"sector_shift": b[0], "card_shift": b[1],
                               "chip_shift": b[2]}
        elif code == 0x45 and b:                               # BATTERY
            # карта с батареей (SRAM/DRAM) или без (флеш). Важно: у SRAM
            # батарея - единственное, что держит данные.
            res["battery"] = {"present": b[0] != 0,
                              "kind": {0: "нет батареи (энергонезависимая "
                                          "память: флеш/ROM)",
                                       1: "батарея есть"}.get(b[0], "?")}
        elif code == 0x46 and b:                               # ORG
            res["org"] = b[0]
        elif code == 0x40 and len(b) >= 3:                     # VERS_2
            res["vers2"] = (b[0], b[1])
        elif code in (0x01, 0x17) and len(b) >= 2:             # DEVICE / DEVICE_A
            entries = cis_device_entries(b)
            if code == 0x01:
                res["devices"].extend(entries)
            else:
                # attribute memory: 8 КиБ EEPROM (перезаписываемая) или ROM
                res["attr_devices"] = entries
    # ёмкость словами: "32 MEG FLASH w/16 Mbit AMD devices" -> 32 Мбит
    if res.get("product"):
        import re as _re
        for txt in res["product"]:
            m = _re.search(r"(\d+)\s*(?:meg|mbit|Мбит|мег)", txt, _re.I)
            if m:
                res["mbits"] = int(m.group(1))
                break
    return res


def raw_strings(data, minlen=4, limit=8):
    """Печатает строки из attribute memory - работает даже без разбора CIS.

    В отчёте это уже спасало: "Centennia" было видно прямо в байтах, хотя
    кортежи не собирались.
    """
    out = []
    cur = []
    for b in data:
        if 32 <= b < 127:
            cur.append(chr(b))
        else:
            if len(cur) >= minlen:
                out.append("".join(cur))
            cur = []
    if len(cur) >= minlen:
        out.append("".join(cur))
    # первая строка CIS - "CIS", её в отчёт как название не берём
    out = [x for x in out if x.strip() not in ("CIS",)]
    return out[:limit]


def read_cis(mem=None, verbose=True):
    """Читает CIS через attribute memory, вернув кортежи и сводку.

    mem=None — выбрать источник самому: /dev/mem, а если ядро запрещает
    (STRICT_DEVMEM, "Operation not permitted") — sysfs (resource0/resource2).
    """
    dev, exca, wins, bv, bd = _bridge_ctx()
    win = choose_window(wins, 0)
    pci_addr = win["start"]
    if verbose:
        bname = BRIDGE_IDS.get(bv, "неизвестный")
        print(f"Мост {dev} ({bname} {bv}:{bd}): ExCA по {exca:#x}, "
              f"окно памяти {pci_addr:#x} (окно моста {win['size'] / 1024 / 1024:.1f} МиБ)")
    if mem is None:
        mem2, src, _err = open_best_mem(dev, verbose=verbose)
        try:
            regs = mem_for_addr(mem2, exca, writable=True, dev_id=dev,
                               verbose=verbose)
            data = mem_for_addr(mem2, pci_addr, writable=False, dev_id=dev,
                               verbose=verbose)
            old = setup_cis_window(regs, exca, pci_addr, card_offset=0, attrib=True)
            try:
                raw = read_mem(data, pci_addr, CIS_MAX, dev_id=dev, verbose=verbose)
            finally:
                restore_cis_window(regs, exca, old)
        except DirectError:
            mem2.close()
            if src == "sysfs":
                raise
            # /dev/mem отказал на этой странице — пробуем sysfs
            if verbose:
                print("  /dev/mem не дал доступ к этой странице, "
                      "пробую окна через sysfs")
            raw = _read_via_sysfs(dev, verbose=verbose)
        else:
            mem2.close()
    else:
        regs = mem_for_addr(mem, exca, writable=True, dev_id=dev, verbose=verbose)
        data = mem_for_addr(mem, pci_addr, writable=False, dev_id=dev,
                           verbose=verbose)
        old = setup_cis_window(regs, exca, pci_addr, card_offset=0, attrib=True)
        try:
            raw = read_mem(data, pci_addr, CIS_MAX, dev_id=dev, verbose=verbose)
        finally:
            restore_cis_window(regs, exca, old)
    # 16-битная карта отдаёт CIS не подряд: лишний байт может стоять между
    # байтами цепочки, а перед началом - неинициализированные байты. Поэтому
    # цепочка ИЩЕТСЯ (scan_cis), а не разбирается с нулевого смещения.
    tuples, how = scan_cis(raw)
    info = cis_summary(tuples)
    info["stride"] = how["stride"]
    info["offset"] = how["offset"]
    info["window"] = {"start": pci_addr, "size": win["size"],
                      "attrib": True, "card": 0, "pci": pci_addr}
    # строки из сырых байт - работают даже когда кортежи не собрались
    if not info.get("product"):
        found = raw_strings(raw)
        if found:
            info["strings"] = found
    # чем именно "пусто": 0xFF - карта не отвечает, 0x00 - шина "прижата"
    if not tuples:
        if all(b == 0xFF for b in raw):
            info["blank"] = "ff"
        elif all(b == 0x00 for b in raw):
            info["blank"] = "00"
        else:
            info["blank"] = "mixed"
    if verbose:
        print(f"attribute memory: {len(raw)} байт, кортежей: {len(tuples)}")
        if tuples:
            if how["stride"] == 2:
                print(f"  ширина шины: каждый 2-й байт (смещение {how['offset']} в сыром потоке)")
            else:
                print(f"  цепочка найдена со смещения {how['offset']}")
            for t in tuples[:12]:
                print(f"    {t['name']:<20} длина {t['len']:<3} {t['body'][:16].hex(' ')}")
        elif info["strings"]:
            print("  строки в attribute memory (паспорт читается, но не разобрался):")
            for x in info["strings"]:
                print(f"    {x}")
    return raw, tuples, info


# ПАСПОРТНАЯ ЁМКОСТЬ (CISTPL_DEVICE + "32 MEG" в строках) — главная подсказка.
# Зачем: 4 МиБ флеша повторяют адреса через 4 МиБ ТОЛЬКО если карта отражает
# неотключённые линии адреса. На живой Centennial FL32M этого не происходит:
# за концом карты читались нули (шина прижата), повторения не нашлось, и набор
# честно, но бесполезно снял 64 МиБ окна моста за 340 секунд.
TRUST_CIS_MAX = 64 << 20


# ------------------------------------------------------------- типы карт
#
# До 2.10.0 набор понимал только linear flash и честно молчал на остальных:
# карта видна, а «паспорта нет». Это неверно для ATA-карт (CompactFlash в
# переходнике, IBM Microdrive, диски SanDisk): у них НЕТ attribute memory,
# паспорт (CIS) лежит в common memory по смещениям 0x200 и 0x400. Поэтому при
# пустой attribute memory надо посмотреть туда, прежде чем говорить «карта не
# отвечает».
CIS_IN_COMMON = (0x200, 0x400)


CIS_LOOK = 512


def cis_in_common_mem(mem, exca, win, dev_id=None):
    """Ищет цепочку CIS в common memory (так устроены ATA-карты).

    Возвращает (смещение, кортежи, сводка) или (None, [], {}).
    """
    for off in CIS_IN_COMMON:
        try:
            raw = _read_page(mem, exca, win, off, CIS_LOOK, dev_id=dev_id)
        except DirectError:
            continue
        if looks_blank(raw, 0xFF) or looks_blank(raw, 0x00):
            continue
        tuples, _how = scan_cis(raw)
        if not tuples:
            continue
        info = cis_summary(tuples)
        # у ATA-карты в паспорте есть MANFID/строка модели, а JEDEC-флеша нет
        if info.get("manfid") or info.get("product"):
            return off, tuples, info
    return None, [], {}


def find_cis(data):
    """Ищет маркер цепочки CIS («CIS» в начале блока атрибутов).

    Формально CIS живёт в attribute memory по чётным адресам, но первое
    приближение — поиск байтов «CIS» в прочитанном окне: у настоящих карт
    там же рядом лежат строки продукта (PRODID).
    """
    out = []
    pos = 0
    while True:
        i = data.find(CIS_TAG, pos)
        if i < 0:
            break
        out.append(i)
        pos = i + 1
        if len(out) >= 8:
            break
    return out
