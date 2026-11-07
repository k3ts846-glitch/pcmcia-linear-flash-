# -*- coding: utf-8 -*-
"""Имена микросхем: JEDEC/CFI-определение, геометрия, карта секторов.

Правила отказа (2.17.x): имя из WRONG_NAMES -> подсказка, как называется
правильно; строка с why -> причина словами документа; ambiguous без why
-> просьба указать ревизию."""

from .chips_data import GEOMETRY, VENDOR_PARTS, VPP126, VPP5, WRONG_NAMES
from .errors import DirectError
from .nor_commands import AMD_READ_CFI, INTEL_READ_CFI


def vendor_part(vid, did):
    """(имя, ёмкость в Мбит) по паре JEDEC ID или None, если код не сверен."""
    table = VENDOR_PARTS.get(vid) or {}
    return table.get(did)


# Intel Series 2 - это 28F 5-вольтовая серия: запись без 12 В, но у части
# карт Vpp жёстко разведён на 12 В. Поэтому "неизвестно", а не "нельзя".


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
        part = vendor_part(vid, did)
        if part:
            name = part[0]
            if name in VPP126:
                return 12, f"по JEDEC: {name} (Vpp 12 В выведен наружу)"
            if name in VPP5:
                return 5, f"по JEDEC: {name} — семейство 5.0 V only, 12 В не нужно"
    prod = " ".join((info or {}).get("product") or [])
    up = prod.upper()
    for name in VPP126:
        if name.upper() in up:
            return 12, f"по строке паспорта: {name}"
    for name in VPP5:
        if name.upper() in up:
            return 5, f"по строке паспорта: {name}"
    if "SERIES 2" in up or "SERIES2" in up:
        return 0, ("Intel Series 2 (28F, 5 В): по документации 12 В не нужно, "
                   "но у части карт Vpp разведён на 12 В — проверить пробной "
                   "записью на копии нельзя, поэтому считаем неизвестным")
    return 0, "по паспорту тип микросхем не определить"


def chip_of(addr, chips=2):
    """В какой чип попадает байт с абсолютным смещением addr.

    Чипов в карте может быть не два! По документации C-ONE Series-II карта
    2/4/8 МБ собрана из 2/4/8 микросхем 28F008SA (по 1 МБ), а 4 МБ карта на
    Am29F016 - из двух. Поэтому байт раскладывается по кругу: i % chips
    (для двух это ровно «чётный/нечётный», как было раньше и как делает ядро
    в pcmciamtd с bankwidth=2).
    """
    n = max(int(chips or 1), 1)
    return 0 if n == 1 else addr % n


def chip_local(addr, chips=2):
    """Смещение внутри чипа (в байтах этого чипа) для абсолютного addr."""
    n = max(int(chips or 1), 1)
    return addr if n == 1 else addr // n


def chip_abs(local, chip, chips=2):
    """Обратное преобразование: смещение внутри чипа -> абсолютный адрес."""
    n = max(int(chips or 1), 1)
    return local if n == 1 else local * n + chip


def sector_map(geo):
    """Карта секторов ОДНОГО чипа: [(размер, число), ...] от младших адресов.

    У большинства микросхем она однородная и собирается из sector/size. У
    boot-микросхем задана явно в GEOMETRY (16K + 2x8K + 32K + N x 64K, а у
    top-boot - то же в обратном порядке) или приходит из CFI, где erase-
    регионы описаны именно так.
    """
    pairs = geo.get("sectors") or []
    if pairs:
        return [(int(size), int(count)) for size, count in pairs]
    sector = int(geo.get("sector") or 0)
    size = int(geo.get("size") or 0)
    if sector > 0 and size > 0 and size % sector == 0:
        return [(sector, size // sector)]
    return []


def check_sector_map(geo, strict=False):
    """Карта секторов должна сходиться с ёмкостью чипа - иначе работать нельзя.

    Границы сектора считаются по карте; если её нет, «округление по 64 КиБ» на
    boot-чипе стёрло бы 16K+8K+8K+32K одним махом, вместе с паспортом карты.

    strict=False - мягкая проверка (во время опроса карты, когда ёмкость ещё
    не известна): проверяем только то, что можно проверить. strict=True -
    перед стиранием и записью: без карты секторов не работаем.
    """
    pairs = sector_map(geo)
    size = int(geo.get("size") or 0)
    name = geo.get("name") or "микросхема"
    if geo.get("sectors"):
        total = sum(sz * cnt for sz, cnt in pairs)
        if size and total != size:
            raise DirectError(
                "микросхема " + str(name) + ": карта секторов даёт " +
                str(total) + " байт, а ёмкость " + str(size) + " - данные "
                "противоречат друг другу, работать нельзя.")
        return geo
    if geo.get("why"):
        # Отказ не из-за ревизии, а из-за того, что микросхема вообще не умеет
        # то, на чём построен наш цикл (стирание только целиком, страничная
        # запись без 30h, NAND вместо NOR). 2.17.27: why - самостоятельная
        # причина, без обязательного ambiguous; раньше строка Samsung K9F*
        # (ro=True, why=«NAND», без ambiguous) молча проходила проверку и
        # получала однородную карту секторов.
        hint = geo.get("src") or geo.get("notes") or ""
        raise DirectError("микросхема " + str(name) + ": " + str(geo["why"])
                          + ((" Подробнее: " + hint.rstrip(".") + ".") if hint else ""))
    if geo.get("ambiguous"):
        hint = geo.get("src") or geo.get("notes") or ""
        raise DirectError(
            "микросхема " + str(name) + ": ревизия не указана, а у boot-чипов "
            "от неё зависит раскладка секторов (16K + 2x8K + 32K + 64K...). "
            "Укажите точное имя ключом --chip (например Am29F800BB для "
            "bottom-boot или Am29F800BT для top-boot): иначе первое же "
            "стирание заденет соседние данные."
            + ((" Что известно про это имя: " + hint.rstrip(".") + ".")
               if hint else ""))
    if strict and (not pairs or not size):
        raise DirectError(
            "микросхема " + str(name) + ": неизвестна карта секторов (нет ни "
            "размера сектора, ни списка секторов). Укажите модель ключом "
            "--chip или размер сектора ключом --sector.")
    return geo


def sector_at(geo, chip_off):
    """Границы сектора, содержащего адрес ВНУТРИ чипа: (начало, размер)."""
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


def name_known(name):
    """Есть ли имя в таблице (точно или как самое длинное совпадение)."""
    key = str(name or "").strip()
    if not key:
        return False
    if key in GEOMETRY:
        return True
    up = key.upper()
    return any(len(n) >= 5 and n.upper() in up for n in GEOMETRY)


def wrong_name_hint(name):
    """Подсказка для обозначения, которого нет в даташитах (или '')."""
    return WRONG_NAMES.get(str(name or "").strip().upper(), "")


def geometry_for(name, size=None, volts=None):
    """Параметры микросхемы по имени: сектор, адреса/ширина команды, вольты.

    Если имя неизвестно - безопасные значения по умолчанию (как у Am29F016:
    64 КиБ, байтовые команды 0x555/0x2AA). Почему безопасные: эти адреса и
    сектор совпадают у большинства 5-вольтовых NOR этой эпохи; а после
    запуска их всё равно проверяет автоопределение - см. verify_geometry().
    """
    base = dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                protocol="jedec",
                size=size or 0, volts=volts or 0,
                src="по умолчанию (как Am29F016)")
    def done(g):
        """Домешать имя и проверить карту секторов."""
        g.setdefault("name", str(g.get("name") or ""))
        if g.get("sectors"):
            check_sector_map(g)          # сумма карты должна сходиться
        return g

    if not name:
        return base
    hint = wrong_name_hint(name)
    if hint:
        raise DirectError("микросхема " + str(name) + ": " + hint + ".")
    key = str(name).strip()
    if key in GEOMETRY:
        g = dict(base)
        g.update(GEOMETRY[key])
        g["name"] = key
        if size:
            g["size"] = size
        if volts:
            g["volts"] = volts
        return done(g)
    up = key.upper()
    # Самое длинное совпадение: «Am29LV800B» не должно подходить под «Am29LV800»
    # по первому найденному ключу словаря - у ревизий различаются сектора.
    hits = [(len(name2), name2) for name2 in GEOMETRY
            if len(name2) >= 5 and name2.upper() in up]
    if hits:
        best = max(hits)[1]
        out = dict(base)
        out.update(GEOMETRY[best])
        out["name"] = best
        if size:
            out["size"] = size
        return done(out)
    return base


def chip_name_from_info(info):
    """Имя микросхемы из паспорта: JEDEC id либо строка продукта."""
    j = (info or {}).get("jedec")
    vid = did = None
    if j and len(j) >= 4:
        try:
            vid, did = int(j[0:2], 16), int(j[2:4], 16)
        except ValueError:
            vid = did = None
        part = vendor_part(vid, did)
        if part:
            return part[0]
    # Коды Intel: 0xA2 = 28F008SA (flashrom и ядро), 0xA0 = 28F016SV
    # («28F016SA/SV» по flashrom; ядро читает тот же A0h как 28F016S5 - у них
    # одна раскладка), 0xA6 = 28F008S5, 0xA7 = 28F004S5. Кода A1h в этих
    # таблицах нет, поэтому прежнее «A1h = 28F016SA» убрано: угадывать нельзя.
    # 0xB4 - 28F010 по даташиту 290207-010 (Intelligent Identifier: mfr 89h,
    # device B4h, читается и командой 90h, и поднятием A9 - второй способ в
    # слоте недоступен). Кода 28F020 в приложенном документе нет.
    intel_ids = {0xA0: "28F016SV", 0xA2: "28F008SA", 0xA6: "28F008S5",
                 0xA7: "28F004S5", 0xB4: "28F010"}
    if vid == 0x89 and did in intel_ids:
        return intel_ids[did]
    prod = " ".join((info or {}).get("product") or []).upper()
    for name in list(GEOMETRY) + ["28F008SA", "28F016SA"]:
        if name.upper() in prod:
            return name
    return ""


def identify_chip(flash, info=None, force=None, progress=None):
    """Что за микросхема: паспорт -> автоопределение -> значения по умолчанию.

    Возвращает dict: name, sector, cmd, wide, size, chips, volts, how.
    Автоопределение (JEDEC ID) важнее паспорта: паспорт пишет производитель
    карты и иногда ошибается, а микросхема отвечает сама. Но автоопределение
    может и не сработать (не все карты разрешают команды в обход контроллера),
    поэтому оба источника остаются в отчёте.
    """
    if progress:
        progress("определяю микросхему: паспорт")
    name = (force or chip_name_from_info(info or {}) or "").strip()
    if force and not name_known(force):
        hint = wrong_name_hint(force)
        raise DirectError(
            "микросхема " + str(force) + ": в таблице такого имени нет"
            + (" — " + hint if hint else "")
            + ". Опечатка в --chip опаснее отказа: умолчания (64 КиБ, 2 МиБ) на "
              "boot-чипе задели бы 16K+8K+8K+32K. Список имён: --chip ? или "
              "--list-chips.")
    how = "паспорт карты" if name else "имя не определено"
    size = 0
    devs = (info or {}).get("devices") or []
    if devs:
        size = devs[0].get("size") or 0
    chips = max(len(devs), 1) if devs else 0
    geo = geometry_for(name, size=size)
    if not name:
        # Честное сообщение: микросхема не ответила, имя неизвестно.
        res_name = "неизвестная"
        how = "имя не определено: параметры по умолчанию (Am29F016), "
        "проверьте запись на одном секторе"
    else:
        res_name = name
    res = dict(name=res_name, sector=geo["sector"], cmd=geo["cmd"],
               wide=geo["wide"], size=geo["size"], chips=chips or 0,
               volts=geo["volts"], how=how, autodetect="",
               protocol=geo.get("protocol", "jedec"),
               # карта секторов ОДНОГО чипа (в байтах), как её видит таблица
               sectors=list(geo.get("sectors") or []),
               ambiguous=bool(geo.get("ambiguous")),
               # строка есть в таблице, но её параметры не сверены с даташитом
               unverified=bool(geo.get("unverified")),
               # микросхема в наборе только читается (нет команды стирания,
               # нужны 12 В и т.п.) - причина в ro_why
               ro=bool(geo.get("ro")), ro_why=str(geo.get("why") or ""),
               src=str(geo.get("src") or ""))
    if progress:
        progress("определяю микросхему: опрос самой микросхемы")
    auto = autodetect_chip(flash)
    if auto:
        res["autodetect"] = auto
        if auto.get("name"):
            res["name"] = auto["name"]
            res["how"] = "автоопределение (ответила микросхема)"
            g2 = geometry_for(auto["name"], size=res["size"] or None)
            res.update(sector=g2["sector"], cmd=g2["cmd"], wide=g2["wide"],
                       volts=g2["volts"] or res["volts"],
                       protocol=g2.get("protocol", "jedec"),
                       sectors=list(g2.get("sectors") or res["sectors"]),
                       ambiguous=bool(g2.get("ambiguous")),
                       ro=bool(g2.get("ro")),
                       ro_why=str(g2.get("why") or res.get("ro_why") or ""))
        elif auto.get("manuf") is not None and not auto.get("id_documented"):
            # Производитель отвечает, но код устройства недокументирован:
            # не подменяем имя Am29F016 — пишем честно, что определено по CFI.
            res["how"] = ("автоопределение (производитель отвечает, "
                          "код устройства недокументирован)")
        if auto.get("sectors"):
            # CFI описывает erase-регионы именно так: (блоков, размер блока).
            # Это карта ЧИПА и самый достоверный источник: снимает вопрос о
            # ревизии (bottom-boot / top-boot) без всяких догадок.
            res["sectors"] = [(int(sz), int(cnt))
                              for sz, cnt in auto["sectors"]]
            res["ambiguous"] = False
            res["how"] += " + карта секторов из CFI"
        if auto.get("sector"):
            # сектор из CFI - это сектор ЧИПА; ниже он умножится на число чипов
            res["sector"] = auto["sector"]
            if not auto.get("name"):
                res["how"] += " + размер сектора из CFI"
        # page из CFI: 0 = буфера нет (побайтовая запись A0h), >0 = размер буфера.
        # CFI есть только у новых ревизий (Am29F016D и новее); у старых — page=1.
        if auto.get("page") is not None:
            res["page"] = auto["page"]
            res["has_write_buffer"] = bool(auto.get("has_write_buffer"))
        else:
            res["page"] = 1
            res["has_write_buffer"] = False
        # Напряжения и тайм-ауты из CFI — для отчёта и корректных пределов поллинга.
        for key in ("vcc_min_write", "vcc_max_write",
                    "vpp_min_write", "vpp_max_write",
                    "timeout_program_us", "timeout_buffer_write_us",
                    "timeout_erase_sector_us", "timeout_mode_switch_us",
                    "timeout_chip_erase_us", "max_erase_cycles"):
            if auto.get(key) is not None:
                res[key] = auto[key]
        if auto.get("cfi_version"):
            res["cfi_version"] = auto["cfi_version"]
        if auto.get("cfi_command") is not None:
            res["cfi_command"] = auto["cfi_command"]
        if auto.get("cfi_manuf") is not None:
            res["cfi_manuf"] = auto["cfi_manuf"]
        if auto.get("cfi_device") is not None:
            res["cfi_device"] = auto["cfi_device"]
        if auto.get("size_chip") and not size:
            res["size"] = auto["size_chip"] * max(int(res["chips"]), 1)
        if auto.get("chips"):
            res["chips"] = auto["chips"]
        if auto.get("size"):
            res["size"] = auto["size"]
    if not res["chips"]:
        # ни паспорт, ни автоопределение не сказали: берём то, что известно
        # вызывающему коду (--chips), и только в крайнем случае «два»
        res["chips"] = int(getattr(flash, "chips", 0) or 2)
    # ВАЖНО: sector в таблице - у ОДНОГО чипа. В адресах карты, где байты
    # разложены по чипам, блок занимает sector * chips байт. Иначе стирание
    # «одного сектора» задело бы половину соседнего.
    res["sector_chip"] = res["sector"]
    res["sector"] = res["sector"] * max(int(res["chips"]), 1)
    # Карта секторов проверяется здесь, а не в geometry_for: если ревизия не
    # указана (Am29F800 - это и BB, и BT), CFI выше мог дать точные регионы,
    # и тогда отказываться незачем.
    check_sector_map(res)
    return res


def autodetect_chip(flash, progress=None):
    """Опрос микросхемы: JEDEC ID (90h) и CFI (98h). Ничего не пишет.

    Возвращает dict или None. Ошибки не поднимаются: карта вправе не отвечать
    на эти команды (контроллер может их не пропускать), и это не поломка.
    """
    out = {}
    try:
        try:
            out.update(_autodetect_amd(flash))
        except (DirectError, OSError, IndexError, ValueError):
            pass
        if not out.get("name"):
            # Intel 28F отвечает только СЛОВОМ: байтовый опрос его не берёт
            try:
                out.update(_autodetect_intel(flash))
            except (DirectError, OSError, IndexError, ValueError):
                pass
        if not out.get("sector"):
            try:
                out.update(_autodetect_cfi(flash))
            except (DirectError, OSError, IndexError, ValueError):
                pass
    finally:
        try:
            flash.reset()
        except (DirectError, OSError):
            pass
    return out or None


def _autodetect_amd(flash):
    """JEDEC ID: AA/55, затем 90h (Intel 28F - словом, AMD/Fujitsu - байтом).

    Проверяет чётность DQ7: оба байта ID не должны быть 0xFF (стёртое состояние
    шины) и не должны быть 0x00 (провод замыкания на землю). Если код устройства
    неdocumented, возвращаем manuf/device без имени — вызывающий код сам решит,
    откатываться ли к CFI или к значениям по умолчанию.
    """
    out = {}
    man, dev = flash.read_id()
    # Оба байта 0xFF или 0x00 — шина молчит, а не отвечает.
    if man in (0xFF, 0x00) and dev in (0xFF, 0x00):
        return out
    out["manuf"] = man
    out["device"] = dev
    out["id_documented"] = False
    part = vendor_part(man, dev)
    if part:
        # Fujitsu (04h/ADh) — MBM29F016A, JEDEC-совместимый с Am29F016
        out["name"] = part[0]
        out["id_documented"] = True
    elif man == 0x89:
        out["name"] = {0xA1: "28F016SA", 0xA2: "28F008SA",
                       0xA3: "28F016S5"}.get(dev, "")
        if out["name"]:
            out["id_documented"] = True
    elif man in (0x04, 0x1F, 0x20, 0xC2, 0xEC, 0xBF):
        # Производитель известен, код устройства — нет: честно сообщаем,
        # чтобы не подменять имя параметрами Am29F016.
        out["name"] = ""
    return out


def _autodetect_intel(flash):
    """Intel 28F: Intelligent Identifier (90h) СЛОВОМ, как в таблице 9.

    Отдельный «двойник» шины нужен потому, что у Intel другая ширина команд
    (адрес 0xAAAA, данные словом): посылать их через объект с байтовыми
    командами нельзя - микросхема такое не примет.
    """
    # Ленивый импорт по той же причине, что и в _autodetect_cfi: nor.py сам
    # берёт имена из chips.py, импорт на уровне модуля дал бы кольцо.
    from .nor import NorFlash
    out = {}
    twin = NorFlash(flash.bus, chips=flash.chips, sector=flash.sector,
                    cmd=(0, 0), wide=True, name=flash.name or "",
                    protocol="intel",
                    sector_map=getattr(flash, "sector_map", None))
    man, dev = twin.read_id()
    if man in (0x89, 0x01, 0x1F, 0x20) and dev not in (0xFF, 0x00, None):
        out["manuf"] = man
        out["device"] = dev
        names = {0xA1: "28F016SA", 0xA2: "28F008SA", 0xA3: "28F016S5",
                 0xA4: "28F320"}
        if man == 0x89:
            out["name"] = names.get(dev, "")
        else:
            part = vendor_part(man, dev)
            if part:
                out["name"] = part[0]
    return out


def _autodetect_cfi(flash):
    """Таблица CFI (команда 98h): ёмкость, блоки, напряжения, тайм-ауты.

    Раскладка полей - CFI Publication 100, таблица 5; сверено по ядру Linux
    (include/linux/mtd/cfi.h - struct cfi_ident; drivers/mtd/chips/cfi_probe.c -
    таблица читается с адреса 0x10):

        0x10-0x12  "QRY" - признак CFI;
        0x13-0x14  основной набор команд (01h = Intel, 02h = AMD/Fujitsu);
        0x15-0x16  адрес расширенной таблицы (там же версия CFI);
        0x17-0x1A  альтернативный набор команд и его адрес;
        0x1B..0x1E Vcc min/max и Vpp min/max при записи, единица 100 мВ;
        0x1F..0x22  типовые тайм-ауты (2^n мкс): слово, буфер, сектор, чип;
        0x23..0x26  максимальные тайм-ауты тех же операций;
        0x27       ёмкость ОДНОГО чипа: 2^n байт;
        0x28-0x29  разрядность шины (1 = x8, 2 = x16, 3 = x8/x16);
        0x2A-0x2B  буферная запись: 2^n байт (0 - буфера нет);
        0x2C       число erase-регионов;
        0x2D..     регионы по 4 байта: младшие 16 бит = блоков - 1,
                   старшие 16 бит = размер блока в единицах 256 байт.

    ЧТО БЫЛО НЕ ТАК: прежняя версия читала поля со сдвигом на байт - номер
    региона (0x2C) принимала за младший байт размера сектора, а 0x2E-0x2F за
    число секторов. На Am29F016D (0x2C=01, регион = 1F 00 00 01) это давало
    «сектор 2 031 872 байта, секторов 0», а на других чипах - мусорную
    ёмкость, которая перекрывала настоящую (см. ниже res["size"]). Проверять
    такие числа надо по таблице выше.

    CFI есть не у всех: у Am29F016/B его нет, у Am29F016D и новее - есть.
    Отсутствие ответа не ошибка.
    """
    # Ленивый импорт: NorFlash живёт в nor.py, а nor.py берёт из chips.py
    # карту секторов и адреса - импорт на уровне модуля дал бы кольцо.
    # Функция нужна редко (только CFI-определение), цена - один import.
    from .nor import NorFlash
    out = {}
    # CFI Query - ОДИН цикл по адресу 55h (даташит, «Command Definitions»),
    # без разблокировки AA/55 и не по адресу 0x555. Прежняя версия слала 98h
    # после unlock в 0x555: на части карт это не входило в режим CFI вообще.
    flash._send(0x55, INTEL_READ_CFI if flash.wide else AMD_READ_CFI)

    def rb(addr):
        return flash.read_byte(chip_abs(addr, 0, flash.chips))

    if rb(0x10) != 0x51 or rb(0x11) != 0x52 or rb(0x12) != 0x59:   # "QRY"
        flash.reset()
        return out

    cmdset = rb(0x13) | (rb(0x14) << 8)
    out["cmdset"] = cmdset
    out["cmdset_name"] = {1: "Intel", 2: "AMD/Fujitsu"}.get(cmdset, "")
    p_adr = rb(0x15) | (rb(0x16) << 8)
    # Версия CFI лежит в расширенной таблице: 'P','R','I', major, minor.
    if p_adr and rb(p_adr) == 0x50 and rb(p_adr + 1) == 0x52 and rb(p_adr + 2) == 0x49:
        out["cfi_version"] = "%d.%d" % (rb(p_adr + 3), rb(p_adr + 4))

    out["vcc_min_write"] = rb(0x1B) * 100        # мВ
    out["vcc_max_write"] = rb(0x1C) * 100
    out["vpp_min_write"] = rb(0x1D) * 100
    out["vpp_max_write"] = rb(0x1E) * 100
    out["timeout_program_us"] = 1 << min(rb(0x1F), 20)
    out["timeout_buffer_write_us"] = 1 << min(rb(0x20), 20)
    out["timeout_erase_sector_us"] = 1 << min(rb(0x21), 24)
    out["timeout_chip_erase_us"] = 1 << min(rb(0x22), 26)
    out["timeout_max_program_us"] = 1 << min(rb(0x23), 20)
    out["timeout_max_erase_sector_us"] = 1 << min(rb(0x25), 24)

    if rb(0x27):
        out["size_chip"] = 1 << rb(0x27)
    out["bus_width"] = {1: "x8", 2: "x16", 3: "x8/x16"}.get(
        rb(0x28) | (rb(0x29) << 8), "")
    # Буферная запись: 2^n байт, 0 - буфера нет (только побайтовая A0h).
    exp = rb(0x2A) | (rb(0x2B) << 8)
    has_buf = 0 < exp <= 9
    out["page"] = (1 << exp) if has_buf else 1
    out["has_write_buffer"] = has_buf

    regions = []
    base = 0x2D
    for _ in range(min(rb(0x2C), 4)):
        val = (rb(base) | (rb(base + 1) << 8) |
               (rb(base + 2) << 16) | (rb(base + 3) << 24))
        regions.append({"blocks": (val & 0xFFFF) + 1,
                        "size": (val >> 16) * 256})
        base += 4
    if regions:
        out["regions"] = regions
        if len(regions) == 1:
            out["sector"] = regions[0]["size"]
        # CFI описывает карту секторов именно так: (блоков, размер блока).
        # Раньше многoregion-ответ считался поводом отказаться от чипа; теперь
        # это готовая карта, по которой считаются границы секторов.
        out["sectors"] = [(r["size"], r["blocks"]) for r in regions]
    flash.reset()
    return out
