# -*- coding: utf-8 -*-
"""Таблицы микросхем: ёмкость, сектор, напряжения, команды.

Каждая строка сопровождается src/notes со ссылкой на документ
(даташит, селектор вендора, flashchips.h). Строка без ссылки несёт
unverified=True и печатается с предупреждением «не сверено»."""


VENDORS = {0x01: "AMD", 0x04: "Fujitsu", 0x1C: "Atmel", 0x20: "STMicroelectronics",
           0x89: "Intel", 0x98: "Toshiba", 0xC2: "Macronix", 0xEC: "Samsung",
           0x1F: "Hitachi", 0x0B: "Mitsubishi", 0xBF: "SST"}


# JEDEC-идентификаторы AMD, которые встречаются на платах linear flash.
# Значение: (имя микросхемы, ёмкость в Мбит). Коды Device ID из даташитов
# AMD Am29F016/016B/016D (18805 rev. D, 21444 rev. E5) и Fujitsu MBM29F016A.
# ёмкость нужна cis_capacity() для подсчёта числа чипов: всего_Мбит / на_чип_Мбит.
# Сверено с реестром src/pcmcia_flash/data/jedec_registry.json (источники:
# flashrom flashchips.h, ядро Linux jedec_probe.c, даташиты AMD).
# ВАЖНО, что было не так до 2.17.2: ёмкости стояли с ошибкой в 2-8 раз
# (0xAB назывался «32 Мбит» вместо 4, 0xD5 - «16 Мбит» вместо 8, 0x34 -
# «256 Мбит»), а имена были без ревизии. Ёмкость идёт в cis_capacity() и
# делит ёмкость карты на число чипов, поэтому такая ошибка давала неверное
# число микросхем, то есть неверную геометрию и порчу соседних секторов.
AMD_PARTS = {
    0x20: ("Am29F010", 1),    # 128 КиБ = 1 Мбит, 8 x 16 КиБ
    0x22: ("Am29DL322", 32),  # 4 МиБ = 32 Мбит, StrataFlash
    0x23: ("Am29F400AT", 4),  # 512 КиБ = 4 Мбит, top-boot
    0x3D: ("Am29F017D", 16),  # 2 МиБ = 16 Мбит
    0x41: ("Am29F032B", 32),  # 4 МиБ = 32 Мбит
    0x4F: ("Am29LV040B", 4),  # 512 КиБ = 4 Мбит, 3 В
    0x51: ("Am29F200AT", 2),  # 256 КиБ = 2 Мбит, top-boot
    0x57: ("Am29F200AB", 2),  # 256 КиБ = 2 Мбит, bottom-boot
    0x58: ("Am29F800BB", 8),  # 1 МиБ = 8 Мбит, bottom-boot
    0x6D: ("Am29LV001BB", 1), # 128 КиБ = 1 Мбит, 3 В, boot-сектора
    0xA4: ("Am29F040", 4),    # 512 КиБ = 4 Мбит, 64 КиБ секторы
    0xAB: ("Am29F400AB", 4),  # 512 КиБ = 4 Мбит, bottom-boot
    0xAD: ("Am29F016", 16),   # 2 МиБ = 16 Мбит, 32 x 64 КиБ; CFI у D-ревизии
    0xBA: ("Am29F080B", 8),   # 1 МиБ = 8 Мбит, B-ревизия
    0xD5: ("Am29F080", 8),    # 1 МиБ = 8 Мбит, 16 x 64 КиБ
    0xD6: ("Am29F800BT", 8),  # 1 МиБ = 8 Мбит, top-boot
    0xED: ("Am29LV001BT", 1), # 128 КиБ = 1 Мбит, 3 В, top-boot
}


# Fujitsu отвечает своим вендор-кодом 04h, коды устройств те же AMD.
FUJITSU_PARTS = {
    0xAD: ("MBM29F016A", 16), # 2 МиБ = 16 Мбит, аналог Am29F016
    0xA4: ("MBM29F040C", 4),  # 512 КиБ = 4 Мбит
}


# STMicroelectronics: вендор 20h; у M29F400B коды 00D5h (top-boot) и 00D6h
# (bottom-boot) - даташит «M29F400BT/M29F400BB», раздел Electronic Signature.
# Это ДРУГИЕ коды, чем у AMD при её вендоре 01h (там D5h - Am29F080).
STMICRO_PARTS = {
    0xD5: ("M29F400BT", 4),   # 512 КиБ = 4 Мбит, top-boot
    0xD6: ("M29F400BB", 4),   # 512 КиБ = 4 Мбит, bottom-boot
}


# Intel: вендор 89h. До 2.18.2 таблицы не было вовсе, а в chips.py лежали
# ТРИ РАЗНЫХ встроенных словаря кодов Intel, и они противоречили друг другу:
#   chip_name_from_info: A0=28F016SV, A2=28F008SA, A6=28F008S5, A7=28F004S5,
#                        B4=28F010;
#   identify_chip:       A1=28F016SA, A2=28F008SA, A3=28F016S5;
#   _autodetect_intel:   A1=28F016SA, A2=28F008SA, A3=28F016S5, A4=28F320.
# Кодов A1h/A3h/A4h нет ни в flashrom (flashchips.h), ни в ядре
# (drivers/mtd/chips/jedec_probe.c) - имена для них были выдуманы, а
# задокументированные A0h/A6h/A7h в двух таблицах отсутствовали. Теперь
# код Intel один и сверен с реестром (jedec_registry.json).
# B4h - 28F010: даташит Intel 290207-010, Intelligent Identifier
# (mfr 89h / device B4h), читается командой 90h.
#
# 2.18.4: остальные коды 12-вольтового первого поколения тоже взяты из
# даташитов, а не по аналогии:
#   B8h - 28F512 (order 290204-008; A28F512 - 290265-004);
#   B9h - 28F256A (order 290243-006), строка таблицы названа без «A»;
#   BDh - 28F020 (order 290245-009).
# Кода B5h (которым раньше «награждали» 28F020) в документах нет: у 28F020 ID
# BDh, поэтому B5h по-прежнему отсутствует.
INTEL_PARTS = {
    0xA0: ("28F016SV", 16),   # 2 МиБ = 16 Мбит, CUI (flashrom: «28F016SA/SV»)
    0xA2: ("28F008SA", 8),    # 1 МиБ = 8 Мбит, CUI, 16 x 64 КиБ
    0xA6: ("28F008S5", 8),    # 1 МиБ = 8 Мбит, Smart 5
    0xA7: ("28F004S5", 4),    # 512 КиБ = 4 Мбит, Smart 5
    0xB4: ("28F010", 1),      # 128 КиБ = 1 Мбит, 12 В, только 90h
    0xB8: ("28F512", 0),      # 64 КиБ = 512 Кбит, 12 В, только 90h
    0xB9: ("28F256", 0),      # 32 КиБ = 256 Кбит, 12 В, только 90h
    0xBD: ("28F020", 2),      # 256 КиБ = 2 Мбит, 12 В, только 90h
}


VENDOR_PARTS = {0x01: AMD_PARTS, 0x04: FUJITSU_PARTS, 0x20: STMICRO_PARTS,
                0x89: INTEL_PARTS}


#
# ПРО Vpp: сколько вольт нужно микросхеме для стирания и записи.
#
# Семейство Am29Fxxx — это «5.0 Volt-only» флеш: по документации AMD
# «A 12.0 volt VPP is not required for program or erase operations», внутренние
# напряжения программатора вырабатываются на кристалле. 12 В нужны СТАРЫМ
# Am28Fxxx (Am28F256/512/010/020) и Intel 28F-серии - у них Vpp выведен наружу.
#
# До 2.13.0 набор определял «нужен Vpp 12 В» по подстроке AMD|Am29|Series 2|Intel
# в тексте паспорта, то есть ВСЕГДА для Am29F016. Это давало ложный запрет
# записи на 5-вольтовой карте.
VPP126 = {"Am28F256", "Am28F512", "Am28F010", "Am28F020", "28F256", "28F512",
          "28F010", "28F020",
          "D28F010", "D28F020", "LH28F010", "LH28F020",
          "M28F008", "M28F016", "M28F102",
          # 2.17.22: строки с volts=12 в GEOMETRY, которых здесь не было -
          # detect_vpp по паспорту карты отвечал «не определить» вместо 12 В.
          "28F008SA", "28F016SA"}


VPP5 = {"Am29F016", "Am29F800", "Am29F400", "Am29F040", "Am29F200", "Am29F010",
        "Am29F080", "Am29LV016", "MBM29F016",
        # имена с ревизией (boot-чипы и B-ревизии) - тоже 5.0 V only
        "Am29F200AB", "Am29F200AT", "Am29F400AB", "Am29F400AT",
        "Am29F800BB", "Am29F800BT", "Am29F080B", "Am29F017D", "Am29F032B",
        "Am29LV001BB", "Am29LV001BT", "Am29LV040B", "Am29LV400BB",
        "Am29LV400BT", "Am29LV800BB", "Am29LV800BT", "Am29LV160DB",
        "Am29LV160DT", "MBM29F016A", "MBM29F033C", "MBM29F040C",
        # Fujitsu/Spansion и ST: 2 и 4 Мбит boot-block, 5.0 V only
        "MBM29F002TC", "MBM29F002BC", "MBM29F004TC", "MBM29F004BC",
        "M29F002BT", "M29F002BB", "M29F400BT", "M29F400BB",
        "HN28F010", "HN28F020", "HN28F100",
        "M5M29F010", "M5M29F020",
        "MX29F016", "MX29F040", "MX29F1610", "MX29F3210",
        "Am29F016D", "28F016SV",
        # 2.17.22: строки геометрии с volts=5, которых здесь не было, -
        # по паспорту карты Vpp не определялся, хотя 12 В им не нужны.
        "28F004S5", "28F008S5", "28F016S5", "AT29C010", "AT29C020",
        "AT29C040", "AT29C512", "Am29F032", "Am29F033", "M28F002",
        "M28F004", "M5M29F400", "MBM29F002", "MBM29F004",
        "MBM29F033", "SST39SF040",
        "SST39VF010", "SST39VF020", "SST39VF040", "SST39VF080",
        # 2.18.2: было TC58FVT320 - такого имени у Toshiba нет (см. WRONG_NAMES:
        # 32-Мбит часть называется TC58FVT321), а в таблице геометрии строка
        # есть только у правильного имени.
        "TC58FVT160", "TC58FVT321",
        # AMD/Fujitsu StrataFlash — 5 В или 3 В, VPP не нужен
        "Am29PL320", "Am29PL640", "Am29PL1280",
        "Am29DL322", "Am29DL323", "Am29DL642", "Am29DL643",
        "Am29DL322DB", "Am29DL322DT", "Am29DL323DB", "Am29DL323DT",
        "Am29DL324DB", "Am29DL324DT", "MBM29DL322BE", "MBM29DL322TE",
        "Am29LV033C",
        "Am29GL064", "Am29GL128", "Am29GL256", "Am29GL512", "Am29GL1024",
        "MBM29DL322", "MBM29DL642", "MBM29GL064", "MBM29GL128",
        # 2.18.2: эти строки были в GEOMETRY, но ни в одном списке Vpp - то
        # есть vpp_need() по паспорту отвечал «не определить» вместо
        # «12 В не нужно». Сверка списков двух источников (набор и база
        # пакета) показала ровно эти девять имён.
        "28F320", "Am29LV001", "Am29LV160", "Am29LV400", "Am29LV800",
        "TC58FVT321", "K9F1208U0A", "K9F1G08U0A", "K9F2G08U0A"}


# ============================================================================
# ЧИПЫ БЫВАЮТ РАЗНЫЕ  (2.15.0)
# ============================================================================
#
# До 2.14.0 запись была написана «под Am29F016»: сектор 64 КиБ, адреса команд
# 0x555/0x2AA, два чипа в паре. На вашей карте это верно, но карт на свете
# много, и у других микросхем другие размеры сектора, другие адреса команд и
# даже другая ширина команды (у Intel 28F008SA команда занимает СЛОВО 16 бит
# по 0x5555, а не байт по 0x555 - это не одно и то же).
#
# Поэтому теперь всё это определяется по паспорту карты (CIS) и, если нужно,
# уточняется у самой микросхемы (автоопределение через F0h/90h/CFI), и
# проверяется на КАЖДОМ шаге - до записи, а не после.

GEOMETRY = {
    # имя: (сектор ЧИПА, страница программирования, адреса команд, ширина
    #       команды, байт на чип, volts, источник сведений).
    #
    # ВАЖНО: sector здесь - размер стираемого блока ОДНОГО чипа. В адресном
    # пространстве карты, где байты разложены по чипам, один блок занимает
    # sector * (число чипов) байт. Это не мелочь: у карты из двух Am29F016
    # стирание «одного сектора 64 КиБ» на самом деле стирает 128 КиБ карты,
    # а у Intel 28F008SA блок вообще 128 КиБ (64K слов), то есть 256 КиБ
    # карты при двух чипах.
    # page - сколько байт программируется ОДНОЙ командой A0h. У JEDEC-микросхем
    # этой эпохи (Am29F016/032/040/080, MBM29F016) page=1: команда A0h
    # программирует ровно один байт за четыре шинных цикла. Буферной записи
    # (page > 1) в даташитах этих микросхем нет - она есть у поздних семейств
    # (Am29PL, Am29DL/GL, Intel StrataFlash) с командами 25h/D0h. page > 1
    # устанавливается только из CFI (поле multibyte-write), а не «по имени».
    # "Писать 4 МиБ побайтно на живой карте - часы" - неверно: внутреннее
    # программирование ~7 мкс на байт, шина - миллисекунды на отклик окна;
    # весь образ укладывается в несколько минут. Ускорение даёт только
    # Unlock Bypass (20h) - 2 цикла на байт вместо 4.
    "Am29F200AB": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=256 << 10, volts=5, sectors=[(16384, 1), (8192, 2), (32768, 1), (65536, 3)],
                     src="AMD Am29F200AB: 2 Мбит, bottom-boot: 1 x 16K + 2 x 8K + 1 x 32K + 3 x 64K"),
    "Am29F200AT": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=256 << 10, volts=5, sectors=[(65536, 3), (32768, 1), (8192, 2), (16384, 1)],
                     src="AMD Am29F200AT: 2 Мбит, top-boot: 3 x 64K + 1 x 32K + 2 x 8K + 1 x 16K"),
    "Am29F400AB": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=512 << 10, volts=5, sectors=[(16384, 1), (8192, 2), (32768, 1), (65536, 7)],
                     src="AMD Am29F400AB: 4 Мбит, bottom-boot: 1 x 16K + 2 x 8K + 1 x 32K + 7 x 64K"),
    "Am29F400AT": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=512 << 10, volts=5, sectors=[(65536, 7), (32768, 1), (8192, 2), (16384, 1)],
                     src="AMD Am29F400AT: 4 Мбит, top-boot: 7 x 64K + 1 x 32K + 2 x 8K + 1 x 16K"),
    "Am29F800BB": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=1024 << 10, volts=5, sectors=[(16384, 1), (8192, 2), (32768, 1), (65536, 15)],
                     src="AMD Am29F800BB: 8 Мбит, bottom-boot: 1 x 16K + 2 x 8K + 1 x 32K + 15 x 64K"),
    "Am29F800BT": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=1024 << 10, volts=5, sectors=[(65536, 15), (32768, 1), (8192, 2), (16384, 1)],
                     src="AMD Am29F800BT: 8 Мбит, top-boot: 15 x 64K + 1 x 32K + 2 x 8K + 1 x 16K"),
    "Am29LV400BB": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=512 << 10, volts=3, sectors=[(16384, 1), (8192, 2), (32768, 1), (65536, 7)],
                     src="AMD Am29LV400BB: 4 Мбит, 3 В, bottom-boot: 1 x 16K + 2 x 8K + 1 x 32K + 7 x 64K"),
    "Am29LV400BT": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=512 << 10, volts=3, sectors=[(65536, 7), (32768, 1), (8192, 2), (16384, 1)],
                     src="AMD Am29LV400BT: 4 Мбит, 3 В, top-boot: 7 x 64K + 1 x 32K + 2 x 8K + 1 x 16K"),
    "Am29LV800BB": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=1024 << 10, volts=3, sectors=[(16384, 1), (8192, 2), (32768, 1), (65536, 15)],
                     src="AMD Am29LV800BB: 8 Мбит, 3 В, bottom-boot: 1 x 16K + 2 x 8K + 1 x 32K + 15 x 64K"),
    "Am29LV800BT": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=1024 << 10, volts=3, sectors=[(65536, 15), (32768, 1), (8192, 2), (16384, 1)],
                     src="AMD Am29LV800BT: 8 Мбит, 3 В, top-boot: 15 x 64K + 1 x 32K + 2 x 8K + 1 x 16K"),
    "Am29LV160DB": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=2048 << 10, volts=3, sectors=[(16384, 1), (8192, 2), (32768, 1), (65536, 31)],
                     src="AMD Am29LV160DB: 16 Мбит, 3 В, bottom-boot: 1 x 16K + 2 x 8K + 1 x 32K + 31 x 64K"),
    "Am29LV160DT": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=2048 << 10, volts=3, sectors=[(65536, 31), (32768, 1), (8192, 2), (16384, 1)],
                     src="AMD Am29LV160DT: 16 Мбит, 3 В, top-boot: 31 x 64K + 1 x 32K + 2 x 8K + 1 x 16K"),
    "Am29LV001BB": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=128 << 10, volts=3, sectors=[(8192, 1), (4096, 2), (16384, 7)],
                     src="AMD Am29LV001BB: 1 Мбит, 3 В, bottom-boot: 1 x 8K + 2 x 4K + 7 x 16K"),
    "Am29LV001BT": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=128 << 10, volts=3, sectors=[(16384, 7), (4096, 2), (8192, 1)],
                     src="AMD Am29LV001BT: 1 Мбит, 3 В, top-boot: 7 x 16K + 2 x 4K + 1 x 8K"),
    "Am29F400": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     ambiguous=True,
                     size=512 << 10, volts=5,
                     why="ревизия не указана: у boot-чипов от неё зависит "
                         "раскладка секторов (16K + 2x8K + 32K + 64K ...), а при "
                         "неверной раскладке первое же стирание заденет соседние "
                         "данные. Укажите --chip Am29F400AB или Am29F400AT",
                     src="AMD Am29F400AB/Am29F400AT (даташиты AMD, boot-block: "
                         "bottom/top)"),
    "Am29LV400": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     ambiguous=True,
                     size=512 << 10, volts=3,
                     why="ревизия не указана: у boot-чипов от неё зависит "
                         "раскладка секторов (16K + 2x8K + 32K + 64K ...), а при "
                         "неверной раскладке первое же стирание заденет соседние "
                         "данные. Укажите --chip Am29LV400BB или Am29LV400BT",
                     src="AMD Am29LV400BB/Am29LV400BT (даташиты AMD, 3 В, boot- "
                         "block)"),
    "Am29LV160": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     ambiguous=True,
                     size=2 << 20, volts=3,
                     why="ревизия не указана: у boot-чипов от неё зависит "
                         "раскладка секторов (16K + 2x8K + 32K + 64K ...), а при "
                         "неверной раскладке первое же стирание заденет соседние "
                         "данные. Укажите --chip Am29LV160DB или Am29LV160DT",
                     src="AMD Am29LV160DB/Am29LV160DT (даташиты AMD, 3 В, boot- "
                         "block)"),
    "Am29LV001": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     ambiguous=True,
                     size=128 << 10, volts=3,
                     why="ревизия не указана: у boot-чипов от неё зависит "
                         "раскладка секторов (16K + 2x8K + 32K + 64K ...), а при "
                         "неверной раскладке первое же стирание заденет соседние "
                         "данные. Укажите --chip Am29LV001BB или Am29LV001BT",
                     src="AMD Am29LV001BB/Am29LV001BT (даташиты AMD, 3 В, boot- "
                         "block)"),
    "Am29F016": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=2 << 20, volts=5, bypass=False,
                     src="AMD Am29F016: 32 x 64 КиБ, 5 В, побайтно A0h. Unlock "
                         "Bypass (20h) появился только в ревизии D (21444E5: "
                         "«The device now has the Unlock Bypass Program "
                         "feature»), поэтому по умолчанию не шлём"),
    "Am29F016D": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                      size=2 << 20, volts=5, bypass=True,
                      src="AMD Am29F016D (21444E, ред. E, июль 2006): 16 Мбит "
                          "x8, «32 uniform sectors of 64 Kbytes each», "
                          "5.0 V only; в ревизии D появился «Unlock Bypass "
                          "Program Command», поэтому bypass=True. JEDEC ID "
                          "тот же 01AD, что у Am29F016, - отличать по CFI"),
    "Am29F032": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=4 << 20, volts=5, bypass=False, unverified=True,
                     src="AMD Am29F032: в селекторе AMD 32-Мбит 5-вольтовая часть называется Am29F032B, имени без B там нет; параметры (64 x 64 КиБ, 5 В) взяты как у Am29F032B - не сверены; 20h не включаем"),
    "Am29F080": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=1 << 20, volts=5, bypass=False,
                     src="AMD Am29F080: 16 x 64 КиБ, 5 В, побайтно A0h; Unlock "
                         "Bypass 20h есть не у всех ревизий - по умолчанию не шлём"),
    "Am29F040": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=512 << 10, volts=5, bypass=False,
                     src="AMD Am29F040: 8 x 64 КиБ, 5 В, побайтно A0h; Unlock "
                         "Bypass 20h есть не у всех ревизий - по умолчанию не шлём"),
    "Am29LV016": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                      size=2 << 20, volts=3, src="AMD Am29LV016: 3 В, 32 x 64 КиБ"),
    "Am29LV800": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     ambiguous=True,
                     size=1 << 20, volts=3,
                     why="ревизия не указана: у boot-чипов от неё зависит "
                         "раскладка секторов (16K + 2x8K + 32K + 64K ...), а при "
                         "неверной раскладке первое же стирание заденет соседние "
                         "данные. Укажите --chip Am29LV800BB или Am29LV800BT",
                     src="AMD Am29LV800BB/Am29LV800BT (даташиты AMD, 3 В, boot- "
                         "block)"),
    # То же семейство, что MBM29F016A ниже (иногда пишут без «A»). Unlock
    # Bypass 20h у Fujitsu нашими документами не подтверждён - см. пояснение
    # к MBM29F016A; раньше здесь стояло bypass=True без ссылки на документ.
    "MBM29F016": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                      size=2 << 20, volts=5, bypass=False,
                      src="Fujitsu MBM29F016A (16M (2M x 8) BIT): 32 сектора по "
                          "64 КиБ, 5.0 V-Only; unlock bypass 20h не подтверждён"),
    # AMD Am28F: 12 вольт на Vpp и ТОЛЬКО стирание всего чипа (Bulk Erase).
    # Публикации AMD: Am28F256 - 11560 Rev G ("256 Kilobit (32 K x 8-Bit)
    # CMOS 12.0 Volt, Bulk Erase"), Am28F512 - 11561 Rev G ("512 Kilobit
    # (64 K x 8-Bit)"), Am28F010 - 11559 Rev H ("1 Megabit (128 K x 8-Bit)"),
    # Am28F020 - "2 Megabit (256 K x 8-Bit) CMOS 12.0 Volt, Bulk Erase".
    # Секторного стирания у семейства нет: пишем ro=True и объясняем причину,
    # иначе набор пытался бы стирать «сектор» командой 30h, которой нет.
    "Am28F256": dict(sector=32 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=32 << 10, volts=12, ro=True,
                     why="Am28F256: только Bulk Erase (всё сразу), секторного "
                         "стирания нет; программирование при 12 В на Vpp "
                         "(AMD 11560 Rev G)",
                     src="AMD Am28F256: 256 Кбит = 32 КиБ, 32K x 8, "
                         "12.0 V ± 5%"),
    "Am28F512": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=64 << 10, volts=12, ro=True,
                     why="Am28F512: только Bulk Erase, секторного стирания нет; "
                         "12 В на Vpp (AMD 11561 Rev G)",
                     src="AMD Am28F512: 512 Кбит = 64 КиБ, 64K x 8, 12.0 V"),
    "Am28F010": dict(sector=128 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=128 << 10, volts=12, ro=True,
                     why="Am28F010: только Bulk Erase (Flasherase), секторного "
                         "стирания нет; 12 В на Vpp (AMD 11559 Rev H)",
                     src="AMD Am28F010: 1 Мбит = 128 КиБ, 128K x 8, 12.0 V ± 5%"),
    "Am28F020": dict(sector=256 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=256 << 10, volts=12, ro=True,
                     why="Am28F020: только Bulk Erase, секторного стирания нет; "
                         "12 В на Vpp (AMD: 2 Megabit (256 K x 8-Bit) CMOS "
                         "12.0 Volt, Bulk Erase)",
                     src="AMD Am28F020: 2 Мбит = 256 КиБ, 256K x 8, 12.0 V"),
    # 28F008SA: команда - СЛОВО по 0x5555, сектор 64 КиБ, вся карта стирается
    # только по секторам (chip erase у неё есть, но 10h у Intel - другое)
    # ВНИМАНИЕ про Intel: у 28F команда подаётся по адресу 0x5555 в СЛОВАХ.
    # Наша модель адресов - байтовая, а слово = два байта, поэтому в таблице
    # стоят байтовые смещения 0xAAAA и 0x5554 (0x5555*2 и 0x2AAA*2). Это не
    # опечатка: именно из-за такой путаницы «один 0x555 на все чипы» неверен.
    # Intel 28F008SA: НЕ JEDEC-команды, а CUI (свой набор команд со статусным
    # регистром). По datasheet: 28F008SA — sixteen 64-Kbyte blocks (1 MByte),
    # 28F016SA — thirty-two 64-KB blocks (2 MByte). Сектор здесь — у одного чипа;
    # в адресах карты с двумя чипами логический блок удваивается.
    # Для записи/стирания нужен Vpp 12 В (VPPH 11.4...12.6 В).
    # --- Fujitsu MBM29F002/004 и ST M29F002/M29F400 (boot-блоки) ----------
    # Fujitsu DS05-20868-3E (MBM29F002TC/BC): «One 16K byte, two 8K bytes, one
    # 32K byte, and three 64K bytes», «T = Top sector, B = Bottom sector».
    # 4-Мбит ревизии - тот же ряд блоков, что у AMD Am29F004B (даташит: «One
    # 16 Kbyte, two 8 Kbyte, one 32 Kbyte, and seven 64 Kbyte sectors»).
    "MBM29F002TC": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA),
                        wide=False, size=256 << 10, volts=5, bypass=False,
                        sectors=[(65536, 3), (32768, 1), (8192, 2), (16384, 1)],
                        src="Fujitsu MBM29F002TC: 2 Мбит, top-boot: 3 x 64K + "
                            "32K + 2 x 8K + 16K"),
    "MBM29F002BC": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA),
                        wide=False, size=256 << 10, volts=5, bypass=False,
                        sectors=[(16384, 1), (8192, 2), (32768, 1), (65536, 3)],
                        src="Fujitsu MBM29F002BC: 2 Мбит, bottom-boot: 16K + "
                            "2 x 8K + 32K + 3 x 64K"),
    "MBM29F004TC": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA),
                        wide=False, size=512 << 10, volts=5, bypass=False,
                        sectors=[(65536, 7), (32768, 1), (8192, 2), (16384, 1)],
                        src="Fujitsu MBM29F004TC: 4 Мбит, top-boot: 7 x 64K + "
                            "32K + 2 x 8K + 16K"),
    "MBM29F004BC": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA),
                        wide=False, size=512 << 10, volts=5, bypass=False,
                        sectors=[(16384, 1), (8192, 2), (32768, 1), (65536, 7)],
                        src="Fujitsu MBM29F004BC: 4 Мбит, bottom-boot: 16K + "
                            "2 x 8K + 32K + 7 x 64K"),
    # ST: «11 memory blocks - 1 Boot Block, 2 Parameter and 8 Main Blocks»
    # (M29F400B) и «7 blocks - 1 Boot, 2 Parameter, 4 Main» (M29F002B).
    "M29F002BT": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA),
                      wide=False, size=256 << 10, volts=5, bypass=False,
                      sectors=[(65536, 3), (32768, 1), (8192, 2), (16384, 1)],
                      src="ST M29F002BT: 2 Мбит, top-boot: 3 x 64K + 32K + "
                          "2 x 8K + 16K"),
    "M29F002BB": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA),
                      wide=False, size=256 << 10, volts=5, bypass=False,
                      sectors=[(16384, 1), (8192, 2), (32768, 1), (65536, 3)],
                      src="ST M29F002BB: 2 Мбит, bottom-boot: 16K + 2 x 8K + "
                          "32K + 3 x 64K"),
    "M29F400BT": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA),
                      wide=False, size=512 << 10, volts=5, bypass=False,
                      sectors=[(65536, 7), (32768, 1), (8192, 2), (16384, 1)],
                      src="ST M29F400BT: 4 Мбит, top-boot, код 00D5h при "
                          "вендоре 20h"),
    "M29F400BB": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA),
                      wide=False, size=512 << 10, volts=5, bypass=False,
                      sectors=[(16384, 1), (8192, 2), (32768, 1), (65536, 7)],
                      src="ST M29F400BB: 4 Мбит, bottom-boot, код 00D6h при "
                          "вендоре 20h"),
    # --- микросхемы из сверенного реестра JEDEC ID (см. AMD_PARTS) ---
    "Am29F017D": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                      size=2 << 20, volts=5, bypass=True,
                      src="AMD Am29F017D: 16 Мбит, 32 x 64 КиБ, CFI"),
    "Am29F032B": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                      size=4 << 20, volts=5, bypass=True,
                      src="AMD Am29F032B: 32 Мбит, 64 x 64 КиБ, CFI"),
    "Am29LV040B": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA),
                       wide=False, size=512 << 10, volts=3, bypass=False,
                       src="AMD Am29LV040B: 4 Мбит, 3 В, 8 x 64 КиБ"),
    "Am29F080B": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                      size=1 << 20, volts=5, bypass=False,
                      src="AMD Am29F080B: 8 Мбит, 16 x 64 КиБ, CFI"),
    "MBM29F040C": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA),
                       wide=False, size=512 << 10, volts=5, bypass=False,
                       src="Fujitsu MBM29F040C: 4 Мбит, 8 x 64 КиБ"),
    # --- JEDEC-чипы из AMD_PARTS, которых не хватало в таблице геометрии ---
    "Am29F800": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     ambiguous=True,
                     size=1 << 20, volts=5, bypass=True,
                     why="ревизия не указана: у boot-чипов от неё зависит "
                         "раскладка секторов (16K + 2x8K + 32K + 64K ...), а при "
                         "неверной раскладке первое же стирание заденет соседние "
                         "данные. Укажите --chip Am29F800BB или Am29F800BT",
                     src="AMD Am29F800BB/Am29F800BT (даташиты AMD, boot-block: "
                         "bottom/top)"),
    "Am29F200": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     ambiguous=True,
                     size=256 << 10, volts=5, bypass=True,
                     why="ревизия не указана: у boot-чипов от неё зависит "
                         "раскладка секторов (16K + 2x8K + 32K + 64K ...), а при "
                         "неверной раскладке первое же стирание заденет соседние "
                         "данные. Укажите --chip Am29F200AB или Am29F200AT",
                     src="AMD Am29F200AB/Am29F200AT (даташиты AMD, boot-block: "
                         "bottom/top)"),
    "Am29F010": dict(sector=16 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=128 << 10, volts=5, bypass=False,
                     src="AMD Am29F010: 8 x 16 КиБ = 128 КиБ, 5 В (таблица "
                         "секторов SA0-SA7 в даташите 21116). Unlock Bypass у "
                         "исходного Am29F010 нет - он появился в поздних "
                         "ревизиях, поэтому пишем обычной командой A0h"),
    # Селектор AMD: в ряду 32 Мбит 5 В есть ТОЛЬКО Am29F032B (4M x 8);
    # «Am29F033» в нём нет, а 32-Мбитная x16 часть у AMD трёхвольтовая -
    # Am29LV033C. Поэтому не догадка, а отказ с двумя настоящими именами.
    "Am29F033": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=4 << 20, volts=5, bypass=True, ambiguous=True,
                     why="у AMD нет 5-вольтовой 32-Мбитной части Am29F033: "
                         "5-вольтовая называется Am29F032B (4M x 8), а 32-Мбитная "
                         "x16 - трёхвольтовая Am29LV033C",
                     src="AMD FLASH MEMORY PRODUCT SELECTOR GUIDE: 32 Mbit - Am29F032B"),
    "Am29LV033C": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                       size=4 << 20, volts=3, bypass=False,
                       src="AMD Am29LV033C: 32 Мбит (4M x 8), 3,0 В, однородные "
                           "секторы; Unlock Bypass даташитом не подтверждён - "
                           "работает штатная A0h"),
    # ====================================================================
    # Дополнительные NOR-флеш микросхемы, встречающиеся на PCMCIA-картах
    # (все параметры из даташитов производителей; для чипов без точных
    # сведений указаны безопасные значения по умолчанию — как Am29F016:
    # сектор 64 КиБ, побайтовая запись A0h, JEDEC-протокол, 5.0 В).
    # ====================================================================
    # --- Fujitsu ---
    # Даташит Fujitsu «MBM29F016A» (16M (2M x 8) BIT, 43 стр.): «The 2 M bytes
    # of data is divided into 32 sectors of 64 K bytes», 5.0 V-Only, ID 04h/ADh.
    # Unlock Bypass 20h в наших записях по этому документу не подтверждён,
    # поэтому bypass=False: стандартная запись A0h верна в любом случае, а
    # выдуманная команда 20h - нет (раньше здесь стояло True «по аналогии с
    # Am29F016»; у Am29F016 20h подтверждён даташитом AMD 18805, у Fujitsu -
    # нет).
    "MBM29F016A": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                       size=2 << 20, volts=5, bypass=False,
                       src="Fujitsu MBM29F016A (16M (2M x 8) BIT): 32 сектора "
                           "по 64 КиБ, 5.0 V-Only, ID 04h/ADh; unlock bypass "
                           "20h даташитом не подтверждён - пишем A0h"),
    # 2.18.3: документ нашёлся, но на имя с буквой ревизии - MBM29F033C
    # (Fujitsu, 46 стр.): «32M-bit, 5.0 V-Only ... divided into 64 sectors of
    # 64K bytes», JEDEC-команды, «A 12.0 V VPP is not required». Параметры
    # строки с ним совпадают, поэтому у ревизии отдельная сверенная строка, а
    # имя без «C» остаётся «не сверено» - так же, как Am29F032 без «B».
    # Unlock Bypass (20h) в документе не упоминается - bypass выключен.
    "MBM29F033C": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                       size=4 << 20, volts=5,
                       src="Fujitsu MBM29F033C (46 стр.): 32M-bit, 4M x 8, "
                           "64 сектора по 64 КиБ, 5.0 V-only, JEDEC"),
    "MBM29F033": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                      size=4 << 20, volts=5, bypass=False, unverified=True,
                      src="Fujitsu MBM29F033: даташит нашёлся только на имя с ревизией - MBM29F033C (64 сектора по 64 КиБ, 5 В); само имя без «C» документом не подтверждено, 20h не включаем"),
    "MBM29F002": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     ambiguous=True,
                     size=256 << 10, volts=5,
                     why="ревизия не указана: у boot-чипов от неё зависит "
                         "раскладка секторов (16K + 2x8K + 32K + 64K ...), а при "
                         "неверной раскладке первое же стирание заденет соседние "
                         "данные. Укажите --chip MBM29F002TC или MBM29F002BC",
                     src="Fujitsu MBM29F002TC/MBM29F002BC (даташиты Fujitsu, "
                         "top/bottom boot)"),
    "MBM29F004": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     ambiguous=True,
                     size=512 << 10, volts=5,
                     why="ревизия не указана: у boot-чипов от неё зависит "
                         "раскладка секторов (16K + 2x8K + 32K + 64K ...), а при "
                         "неверной раскладке первое же стирание заденет соседние "
                         "данные. Укажите --chip MBM29F004TC или MBM29F004BC",
                     src="Fujitsu MBM29F004TC/MBM29F004BC (даташиты Fujitsu, "
                         "top/bottom boot)"),
    # 2.18.2: сектор = вся ёмкость. Поблочного стирания у M28F102 нет
    # («Electrical CHIP ERASE in 1s» в даташите ST), поэтому «сектор 64 КиБ»
    # в строке был вымыслом: чип стирается только целиком, как 28F010.
    "M28F102": dict(sector=128 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                    size=128 << 10, volts=12, ambiguous=True,
                    # 2.17.17: как у 28F010/28F020 - страховка в самом
                    # NorFlash, а не только в check_sector_map: поблочного
                    # стирания у чипа нет (даташит ST, август 1998).
                    ro=True,
                    why="стирание только целиком (bulk/chip erase, 12 В при "
                        "программировании), команды Sector Erase нет",
                    src="ST M28F102 (август 1998): 1 Мбит = 64K x 16 = 128 КиБ, "
                        "5 В питания и 12 В программирования, «Electrical CHIP "
                        "ERASE in 1s»; поблочного стирания нет"),
    # M28F016: точного имени в перечнях Intel НЕТ (искали отдельно), поэтому
    # пометка «не сверено» остаётся. Но команды JEDEC 555h/2AAh у 12-вольтовой
    # части этой семьи невозможны: 16-Мбит байтовые Intel называются
    # MS28F016SV/VS28F016SV и 28F016SA (2 МиБ, блоки 64 КиБ, CUI), а документ
    # MS28F016SV прямо говорит «Backwards-Compatible with VE28F008, M28F008
    # and 28F016SA Command Set». Отсюда CUI с честной пометкой, а не догадка
    # о том, что это ST и что команды AMD.
    "M28F016": dict(sector=64 << 10, page=1, cmd=(0, 0), wide=False,
                    size=2 << 20, volts=12, protocol="intel", unverified=True,
                    src="16-Мбит x8 Intel этой семьи (28F016SA/MS28F016SV, "
                        "2 МиБ, блоки 64 КиБ, CUI): имя M28F016 в перечнях "
                        "Intel не найдено - параметры не сверены даташитом на "
                        "точное имя, команды CUI по семейству"),
    # Имена, которых НЕТ в даташитах: ST выпускала 2/4-Мбит boot-block как
    # M29F002BB/BT и M29F400BB/BT, а серия M28F - старые 12-вольтовые
    # (M28F512/010/020/102). Mitsubishi 4-Мбит boot-block (M5M29F400) тоже не
    # подтверждается ни одним найденным даташитом. Держим отказ с объяснением,
    # а не «значения по умолчанию как у Am29F016»: те дали бы неверную
    # ёмкость и однородную карту.
    # 2.17.24: раньше у этих трёх строк не было why, и отказ был общим -
    # «ревизия не указана, укажите Am29F800BB...», что для ST и Mitsubishi
    # просто неверно. Документа на сами имена найти не удалось, поэтому так и
    # написано: «документа не нашли», а не «такого имени нет» (это разные
    # утверждения, и второе мы доказать не можем).
    "M28F002": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                    ambiguous=True, size=256 << 10, volts=5,
                    why="документа на имя M28F002 не нашли: у ST 2-Мбит "
                        "boot-block с ревизиями T/B называется M29F002BT или "
                        "M29F002BB. Укажите --chip M29F002BT или M29F002BB",
                    src="ST M29F002BT/M29F002BB (даташит ST, 2 Мбит boot-block)"),
    "M28F004": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                    ambiguous=True, size=512 << 10, volts=5,
                    why="документа на имя M28F004 не нашли: у ST 4-Мбит "
                        "boot-block с ревизиями T/B называется M29F400BT или "
                        "M29F400BB. Укажите --chip M29F400BT или M29F400BB",
                    src="ST M29F400BT/M29F400BB (даташит ST, 4 Мбит boot-block)"),
    "M5M29F400": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                      ambiguous=True, size=512 << 10, volts=5,
                      why="документа на имя M5M29F400 не нашли: у Mitsubishi "
                          "подтверждены только M5M29F010/M5M29F020, ревизий "
                          "T/B в найденных документах нет. Укажите точное имя "
                          "с маркировки микросхемы ключом --chip",
                      src="Mitsubishi M5M29F010/M5M29F020 (единственные "
                          "найденные даташиты этой серии)"),
    # --- Atmel ---
    "AT29C010": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=128 << 10, volts=5, bypass=False, ambiguous=True,
                     why="страничная запись с автостиранием страницы: команды "
                         "Sector Erase 30h у AT29C нет вовсе, стирание - только "
                         "всей микросхемы 6-байтовым кодом, а запись идёт целой "
                         "страницей (1024 страницы по 128 байт, Device ID D5h у "
                         "AT29C010A). Этим набором карта не стирается, "
                         "побайтовая запись A0h здесь не действует",
                     src="Atmel AT29C010A (128K x 8): 1024 sectors x 128 bytes, "
                         "Device ID D5h - App Note DOC0518"),
    "AT29C020": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=256 << 10, volts=5, bypass=False, ambiguous=True,
                     why="страничная запись с автостиранием страницы: команды "
                         "Sector Erase 30h у AT29C нет вовсе, стирание - только "
                         "всей микросхемы 6-байтовым кодом, а запись идёт целой "
                         "страницей (1024 страницы по 256 байт). Этим набором "
                         "карта не стирается, побайтовая запись A0h здесь не "
                         "действует",
                     src="Atmel AT29C020 (256K x 8), даташит 0291P-FLASH-2/05: "
                         "«1024 Sectors (256 Bytes/Sector)», «the device will "
                         "automatically erase the sector and then program the "
                         "latched data»"),
    "AT29C040": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=512 << 10, volts=5, bypass=False, ambiguous=True,
                     why="страничная запись с автостиранием: у AT29C040A 2048 "
                         "страниц по 256 байт (Device ID A4h), а у AT29C040 "
                         "размер страницы и Device ID другие - Atmel: «AT29C040A "
                         "is identical to AT29C040 except for the sector size "
                         "and the Device ID Code». Команды Sector Erase 30h нет: "
                         "стирание только всей микросхемы 6-байтовым кодом",
                     src="Atmel AT29C040A, даташит «4-Megabit (512K x 8) 5-volt "
                         "Only 256-Byte Sector Flash Memory»; различие с AT29C040 "
                         "- DOC0518"),
    "AT29C512": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=64 << 10, volts=5, bypass=False, ambiguous=True,
                     why="страничная запись с автостиранием страницы: команды "
                         "Sector Erase 30h у AT29C нет вовсе, стирание - только "
                         "всей микросхемы 6-байтовым кодом, а запись идёт целой "
                         "страницей (512 страниц по 128 байт, Device ID 5Dh). "
                         "Этим набором карта не стирается, побайтовая запись A0h "
                         "здесь не действует",
                     src="Atmel AT29C512 (64K x 8): 512 sectors x 128 bytes, "
                         "Device ID 5Dh - App Note DOC0518 «Programming Atmel's "
                         "AT29 Flash Family»"),
    # --- Toshiba ---
    # Даташит Toshiba TC58FVT160/B160A (16-MBIT, 2Mx8/1Mx16): «Block erase
    # architecture 1 x 16 Kbytes / 2 x 8 Kbytes / 1 x 32 Kbytes / 31 x 64
    # Kbytes», «TC58FVT160AFT: top boot block», VDD 2.7-3.6 В, BYTE/Word,
    # коды 98h/00C2h. В байтовом режиме адреса команд - AAAh/555h (в даташите
    # прямо: «Byte Mode: AAAH or 555H on address pins A10~A-1»), поэтому
    # cmd=(0xAAA, 0x555), а не привычные 0x555/0x2AA.
    # Порядок блоков в верхних 64 КиБ - из таблицы регионов ядра Linux
    # (jedec_probe.c, TC58FVT160: 31 x 64K + 1 x 32K + 2 x 8K + 1 x 16K).
    "TC58FVT160": dict(sector=64 << 10, page=1, cmd=(0xAAA, 0x555), wide=False,
                       size=2 << 20, volts=3, bypass=True,
                       sectors=[(64 << 10, 31), (32 << 10, 1), (8 << 10, 2), (16 << 10, 1)],
                       src="Toshiba TC58FVT160: top boot, 31x64K + 32K + 2x8K + 16K, "
                           "3.0 В, команды AAA/555 (даташит + ядро jedec_probe.c)"),
    # TC58FVT321 (4 MiB): ядро jedec_probe.c - 63 x 64K + 8 x 8K; пресс-релиз
    # Toshiba про TC58FVT/B321 - «one 64-Kbyte bank, one 448-Kbyte bank and
    # seven 512-Kbyte banks», блоки boot 8 КБ x8. Имени TC58FVT320 в даташитах
    # нет - оно осталось в WRONG_NAMES с подсказкой.
    "TC58FVT321": dict(sector=64 << 10, page=1, cmd=(0xAAA, 0x555), wide=False,
                       size=4 << 20, volts=3, bypass=True,
                       sectors=[(64 << 10, 63), (8 << 10, 8)],
                       src="Toshiba TC58FVT321: 63x64K + 8x8K, 3.0 В, команды AAA/555 "
                           "(ядро jedec_probe.c + описание Toshiba TC58FVT/B321)"),
    # --- Samsung ---
    "K9F1G08U0A": dict(sector=128 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                       size=128 << 20, volts=3.3, bypass=False, ro=True,
                       why="NAND, а не NOR: чтение и запись идут страницами (2K + "
                           "64 байт), стирание - блоком командой 60h с "
                           "подтверждением D0h, адрес, команды и данные передаются "
                           "по одним линиям I/O0-I/O7. Линейно, как NOR-карта, эта "
                           "микросхема не читается, и стирать её командами "
                           "20h/30h/D0h нельзя - набор её не трогает",
                       src="Samsung K9F1G08U0A: «128M x 8 Bit NAND Flash Memory», "
                         "массив (128M + 4,096K)bit x 8, страница (2K + 64) байт, "
                         "блок (128K + 4K) байт, 1024 блока, «the bit by bit erase "
                         "operation is prohibited»"),
    # --- Macronix ---
    # Даташит Macronix MX29F1610 (16M-BIT [2Mx8/1Mx16], 5 В +-10%):
    # «Sector erase architecture - 16 equal sectors of 128k bytes each»,
    # страничная запись 128 байт/64 слова. Прежние «64 КиБ сектор» были
    # опасны: стирание «64 КиБ» стёрло бы блок 128 КиБ вместе с соседом.
    "MX29F016": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=2 << 20, volts=5, bypass=False,
                     src="Macronix MX29F016 (P/N PM0590, ред. 1.4, "
                         "21.11.2002): 16 Мбит x8, «Sector Erase: 32 equal "
                         "sector with of 64KByte each», питание 5 В"),
    "MX29F040": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=512 << 10, volts=5, bypass=False,
                     src="Macronix MX29F040: 4 Мбит (512K x 8), «Sector Erase "
                         "8 equal sectors of 64K-Byte each», 5.0 V only"),
    "MX29F1610": dict(sector=128 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                      size=2 << 20, volts=5, bypass=False,
                      src="Macronix MX29F1610: 16 секторов по 128 КиБ, 5 В "
                          "(даташит: «16 equal sectors of 128k bytes each», "
                          "5V +-10%); в таблице команд Unlock Bypass нет - "
                          "стирание 80h/30h, запись A0h; страничная запись "
                          "128 байт не используется - пишем по байту"),
    # 32-Мбит версия того же семейства; даташита под рукой нет, поэтому
    # раскладка взята по аналогии (32 x 128 КиБ) и строка помечена unverified.
    "MX29F3210": dict(sector=128 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                      size=4 << 20, volts=5, bypass=False, unverified=True,
                      src="Macronix MX29F3210: 4 МиБ, 5 В - по аналогии с MX29F1610, даташитом не сверено; в линейке Macronix 5-вольтовый MX29F - только 2-8 Мбит (страница Parallel NOR Flash), 32 Мбит у них 3-вольтовые (MX29LV/MX29GL); 20h не включаем"),
    # --- SST ---
    # SST39VF010/020 (даташит Microchip/SST DS20005023: «Sector-Erase
    # Capability - Uniform 4 KByte sectors», 2.7-3.6 В, Software Data
    # Protection). Unlock Bypass 20h у этого семейства нет: запись - три
    # такта AAh/55h/A0h, стирание - шесть, SDP. Раньше у 010/020/080 стояло
    # bypass=True (у SST39VF040 при этом False) - команда 20h уходила в никуда,
    # запись откатывалась на A0h с предупреждением. Теперь False у всех.
    "SST39VF010": dict(sector=4 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                       size=128 << 10, volts=3, bypass=False,
                       src="SST SST39VF010 (DS20005023): 1 Mbit, 128 KiB, 32 сектора по 4 КиБ, 2.7-3.6 В, Software Data Protection; unlock bypass 20h нет"),
    "SST39VF020": dict(sector=4 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                       size=256 << 10, volts=3, bypass=False,
                       src="SST SST39VF020 (DS20005023): 2 Mbit, 256 KiB, 64 сектора по 4 КиБ, 2.7-3.6 В, Software Data Protection; unlock bypass 20h нет"),
    "SST39VF040": dict(sector=4 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                       size=512 << 10, volts=3, bypass=False,
                       src="SST SST39VF040: 512 KiB, 128 секторов по 4 КиБ, 2.7-3.6 В (flashrom sst.c: {4 KiB, 128}; SST DS25022A: uniform 4 KByte sectors)"),
    # 5-вольтовый близнец SST39VF040 (SST DS25022A: uniform 4 KByte sectors).
    "SST39SF040": dict(sector=4 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                       size=512 << 10, volts=5, bypass=False,
                       src="SST SST39SF040: 512 KiB, 128 секторов по 4 КиБ, 5 В "
                           "(SST DS25022A; flashrom sst.c: {4 KiB, 128})"),
    "SST39VF080": dict(sector=4 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                       size=1 << 20, volts=3, bypass=False,
                       src="SST SST39VF080 (S71146 EOL): 8 Mbit, 1M x8, 256 секторов по 4 КиБ (плюс стирание блоком 64 КиБ), 2.7-3.6 В; unlock bypass 20h нет"),
    # --- Intel 28F-серия (CUI-протокол) ---
    # По datasheet: 28F008SA — sixteen 64-Kbyte blocks (1 MByte),
    # 28F016SA — thirty-two 64-KB blocks (2 MByte).
    # Байтовое семейство Smart 5 FlashFile 4/8/16 Мбит (даташит Intel):
    # «4 Mbit: Eight Blocks - 8 Mbit: Sixteen Blocks - 16 Mbit: Thirty-Two
    # Blocks», все блоки по 64 КиБ, 5 В, x8. ID: A7h/A6h (flashrom: 28F004S3/
    # S5/SC, ядро Linux jedec_probe.c: I28F008S5 = A6h).
    "28F004S5": dict(sector=64 << 10, page=1, cmd=(0, 0), wide=False,
                     size=512 << 10, volts=5, protocol="intel",
                     src="Intel 28F004S5 (4 Мбит, x8, 5 В, даташит 290597-005 + spec update 297796-007): 512 КиБ, однородные секторы 64 КиБ, командой байт"),
    "28F008S5": dict(sector=64 << 10, page=1, cmd=(0, 0), wide=False,
                     size=1 << 20, volts=5, protocol="intel",
                     src="Intel 28F008S5 (8 Мбит, x8, 5 В, даташит 290597-005 + spec update 297796-007): 1024 КиБ, однородные секторы 64 КиБ, командой байт"),
    # Даташит 290429-008 (28F008SA, 8 Мбит, декабрь 1998): 16 блоков по 64 КиБ,
    # CUI + статусный регистр, RY/BY#, пауза стирания, 12 В на Vpp при
    # стирании и записи. Микросхема БАЙТОВАЯ (x8, «Byte Write»): команды
    # пишутся одним байтом, поэтому wide=False. Статус: SR.7 готовность,
    # SR.6 пауза стирания, SR.5 ошибка стирания, SR.4 ошибка записи,
    # SR.3 нет Vpp; SR.2-SR.0 reserved, бита WPPS (как у S5) здесь НЕТ.
    "28F008SA": dict(sector=64 << 10, page=1, cmd=(0, 0), wide=False,
                     size=1 << 20, volts=12, protocol="intel",
                     src="Intel 28F008SA (даташит 290429-008): CUI, 16 блоков "
                         "по 64 КиБ, x8 - команда байтом, 12 В на Vpp, "
                         "статус 70h (SR.7/6/5/4/3), ID 89h/A2h"),
    "28F016SA": dict(sector=64 << 10, page=1, cmd=(0, 0), wide=True,
                     size=2 << 20, volts=12, protocol="intel",
                     src="Intel 28F016SA: CUI, 32 блока по 64 КиБ, Vpp 12 В"),
    # 28F016SV (2.17.22): имя и параметры взяты из базы пакета, где строка
    # стоит без пометки, но цитаты по карте блоков найти не удалось. В
    # даташите Intel «16-MBIT (1 MBIT x 16, 2 MBIT x 8) FlashFile MEMORY»
    # (E28F016SV-070, 63 стр.) есть ёмкость, CUI и «user-selectable VPP
    # voltage»; про «32 блока по 64 КиБ» цитаты нет, поэтому пометка остаётся
    # в ОБЕИХ таблицах, а не выдаётся за проверку.
    # 2.18.3: «не сверено» снято - документ нашёлся. Intel, «16-Mbit
    # FlashFile Memory» (product brief 297349-003, 28F016SA/28F016SV):
    # «They contain 32 64-Kbyte (65,536 byte) blocks, each block separately
    # erasable»; SmartVoltage - «the option of 5V Vpp operation» (или 12 В);
    # x8/x16; набор команд - надмножество 28F008SA. Всё сходится со строкой.
    # В документе есть две 256-байтовые буферные страницы, но код умеет буфер
    # не больше INTEL_BUFFER_SIZE (32 байта), а «половину буфера» писать
    # нельзя - поэтому page=1 и побайтная запись командой 40h.
    "28F016SV": dict(sector=64 << 10, page=1, cmd=(0, 0), wide=True,
                     size=2 << 20, volts=5, protocol="intel",
                     src="Intel 28F016SV: 16 Мбит (1 Мбит x16 / 2 Мбит x8), CUI, SmartVoltage (Vpp 5 или 12 В), 32 блока по 64 КиБ - Intel «16-Mbit FlashFile Memory», 297349-003 (28F016SA/28F016SV)"),
    # Даташит Intel 28F016S5 («Byte-Wide Smart 5 FlashFile Memory»): «64-Kbyte
    # Symmetrical Erase Block Architecture ... 16 Mbit: Thirty-Two Blocks».
    # Прежние 128 КиБ давали ложный отказ blank-check и запись через границу
    # блока: стирался один 64-КиБ блок, а писалось два.
    # Даташит Intel «BYTE-WIDE SMART 5 FlashFile MEMORY FAMILY 4, 8, AND 16 MBIT»:
    # «16 Mbit: Thirty-Two Blocks», 5 В (Vpp 5 или 12 В). Микросхема БАЙТОВАЯ -
    # команда CUI пишется одним байтом, поэтому wide=False. Буфер 25h/D0h не
    # используем: это команда AMD StrataFlash, в CUI Intel её нет, а писать
    # неизвестную команду в микросхему с паспортом карты внутри нельзя.
    # Пишем по байту командой 40h - медленнее, зато точно по даташиту.
    "28F016S5": dict(sector=64 << 10, page=1, cmd=(0, 0), wide=False,
                     size=2 << 20, volts=5, protocol="intel",
                     src="Intel 28F016S5 (16 Мбит, x8, 5 В, даташит 290597-005 + spec update 297796-007): 2048 КиБ, однородные секторы 64 КиБ, командой байт"
                         "32 x 64 КиБ, 5 В; буфер не используется"),
    # Ревизии 28F320 раскладываются по-разному: у B3T/B - boot-блоки, у J3/J5
    # блоки крупнее 64 КиБ. Общее имя «28F320» ничего не говорит о границах
    # стирания, а ошибка здесь стирает соседний блок (в 2.17.4 её ловит
    # _erase_sector_guarded, но лучше не доводить).
    "28F320": dict(sector=64 << 10, page=1, cmd=(0, 0), wide=True,
                   size=4 << 20, volts=3, protocol="intel", ambiguous=True,
                   why="у Intel 28F320 два разных семейства с разной "
                       "раскладкой: 28F320B3 (3-Volt Advanced Boot Block: "
                       "восемь 8-КиБ блоков данных и до 63 блоков по 64 КиБ) и "
                       "28F320J3 (StrataFlash J3: 32 блока по 128 КиБ, "
                       "x8/x16). Без ревизии раскладка не подтверждена - "
                       "укажите точное имя с маркировки",
                   src="Intel 3 Volt Advanced Boot Block 28F004/400B3, "
                         "28F008/800B3, 28F016/160B3, 28F320B3, 28F640B3; Intel "
                         "StrataFlash J3 28F320J3"),
    # Обозначения M28F016S5 у ST не найдено: байтовое семейство FlashFile
    # выпускала Intel (28F004S5/008S5/016S5), у ST были M28F008/M28F016 (12 В)
    # и M28F102. Пока даташита нет - отказ, а не «как у Intel».
    # M28F016S5 убран из GEOMETRY в 2.17.24: имя уже есть в WRONG_NAMES,
    # а подсказка оттуда проверяется РАНЬШЕ таблицы, поэтому строка была
    # недостижима - но попадала в --list и в сверку таблиц.
    # --- AMD/Fujitsu StrataFlash (Am29PL/DL/GL, MBM29DL/GL) ---
    # Буферная запись 25h/D0h, JEDEC-протокол, DQ7/DQ5 опрос, 5 В или 3 В.
    # Имён Am29PL… нет ни в flashchips.h flashrom, ни в jedec_probe.c ядра:
    # похоже на «PL» из Spansion S29PL, но это уже другой ряд микросхем.
    # Данные ниже - не сверенные, и это написано прямо в src.
    "Am29PL320": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                      size=4 << 20, volts=3, bypass=False, strata=True, unverified=True,
                      src="Am29PL320: обозначение даташитом не подтверждено (нет ни в flashchips.h, ни в jedec_probe.c); ёмкость и буфер 32 байта - по аналогии с DL/GL, не сверено; 20h не включаем"),
    "Am29PL640": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                      size=8 << 20, volts=3, bypass=False, strata=True, unverified=True,
                      src="Am29PL640: обозначение даташитом не подтверждено (нет ни в flashchips.h, ни в jedec_probe.c); ёмкость и буфер 32 байта - по аналогии с DL/GL, не сверено; 20h не включаем"),
    "Am29PL1280": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                       size=16 << 20, volts=3, bypass=False, strata=True, unverified=True,
                       src="Am29PL1280: обозначение даташитом не подтверждено (нет ни в flashchips.h, ни в jedec_probe.c); ёмкость и буфер 32 байта - по аналогии с DL/GL, не сверено; 20h не включаем"),
    # AMD Am29DL322D/323D/324D (даташит 21534d, декабрь 2005): «32 Megabit
    # (4 M x 8-Bit/2 M x 16-Bit) CMOS **3.0 Volt-only**, Simultaneous Operation».
    # Table 2: Bank 1 = восемь секторов по 8 КиБ + семь по 64 КиБ, Bank 2 = 56 по
    # 64 КиБ. Table 5 (Bottom Boot): SA0..SA7 по 8 КиБ, дальше 63 по 64 КиБ.
    # Прежняя строка «5 В и однородные 64 КиБ» была неверна дважды: у чипа есть
    # 8-КиБ boot-секторы, и стирание «первого сектора 64 КиБ» стёрло бы только
    # 8 КиБ (blank-check упал бы), а напряжение написано 3 В прямо в заголовке.
    "Am29DL322": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                      size=4 << 20, volts=3, bypass=True, strata=True, ambiguous=True,
                      why="AMD Am29DL322 без суффикса: раскладка зависит от T/B "
                          "(bottom 8x8K+63x64K, top наоборот, плюс сектор SecSi "
                          "64 КиБ); укажите Am29DL322DB/DT - они в таблице",
                      src="AMD Am29DL322D (21534d), Table 2/5: 3,0 В, boot 8 КиБ"),
    "Am29DL323": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                      size=4 << 20, volts=3, bypass=True, strata=True, ambiguous=True,
                      why="AMD Am29DL323 без суффикса: нужен T/B (раскладка одна и "
                          "та же с 322D, различаются банки); укажите Am29DL323DB/DT",
                      src="AMD Am29DL323D (21534d), Table 2: 3,0 В, boot 8 КиБ"),
    "Am29DL642": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                      size=8 << 20, volts=3, bypass=True, strata=True, ambiguous=True,
                      why="AMD Am29DL642: 64-Мбит семейство Am29DL640D/G - четыре "
                          "банка, восемь boot-секторов по 8 КиБ снизу И восемь "
                          "сверху («Both Top and Bottom Boot»), между ними 126 по "
                          "64 КиБ; однородных 64 КиБ нет. Укажите ревизию D или G",
                      src="AMD Am29DL640D (23695C3) / Am29DL640G (25693B): 3,0 В, "
                          "8 КиБ + 126 x 64 КиБ + 8 КиБ"),
    "Am29DL643": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                      size=8 << 20, volts=3, bypass=True, strata=True, ambiguous=True,
                      why="AMD Am29DL643: то же семейство Am29DL640D/G (x16): восемь "
                          "8-КиБ boot-секторов снизу и восемь сверху, 126 x 64 КиБ; "
                          "укажите ревизию D или G",
                      src="AMD Am29DL640D (23695C3) / Am29DL640G (25693B): 3,0 В"),
    # Конкретные ревизии: карта из Table 5 (bottom) и Table 3 (top) даташита
    # 21534d. Сумма 8x8K + 63x64K = 4 МиБ, сходится с Table 2.
    "Am29DL322DB": dict(sector=8 << 10, size=4 << 20, page=32, cmd=(0x555, 0x2AA),
                        wide=False, volts=3, bypass=True, strata=True,
                        sectors=[(8 << 10, 8), (64 << 10, 63)],
                        src="AMD Am29DL322DB (21534d, Table 5): 8x8 КиБ + 63x64 КиБ, 3,0 В"),
    "Am29DL322DT": dict(sector=64 << 10, size=4 << 20, page=32, cmd=(0x555, 0x2AA),
                        wide=False, volts=3, bypass=True, strata=True,
                        sectors=[(64 << 10, 63), (8 << 10, 8)],
                        src="AMD Am29DL322DT (21534d, Table 3): 63x64 КиБ + 8x8 КиБ, 3,0 В"),
    "Am29DL323DB": dict(sector=8 << 10, size=4 << 20, page=32, cmd=(0x555, 0x2AA),
                        wide=False, volts=3, bypass=True, strata=True,
                        sectors=[(8 << 10, 8), (64 << 10, 63)],
                        src="AMD Am29DL323DB (21534d, Table 5): раскладка как у 322DB, "
                            "иное деление на банки"),
    "Am29DL323DT": dict(sector=64 << 10, size=4 << 20, page=32, cmd=(0x555, 0x2AA),
                        wide=False, volts=3, bypass=True, strata=True,
                        sectors=[(64 << 10, 63), (8 << 10, 8)],
                        src="AMD Am29DL323DT (21534d, Table 3): раскладка как у 322DT"),
    "Am29DL324DB": dict(sector=8 << 10, size=4 << 20, page=32, cmd=(0x555, 0x2AA),
                        wide=False, volts=3, bypass=True, strata=True,
                        sectors=[(8 << 10, 8), (64 << 10, 63)],
                        src="AMD Am29DL324DB (21534d, Table 5): раскладка как у 322DB"),
    "Am29DL324DT": dict(sector=64 << 10, size=4 << 20, page=32, cmd=(0x555, 0x2AA),
                        wide=False, volts=3, bypass=True, strata=True,
                        sectors=[(64 << 10, 63), (8 << 10, 8)],
                        src="AMD Am29DL324DT (21534d, Table 3): раскладка как у 322DT"),
    "Am29GL064": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                      size=8 << 20, volts=3, bypass=True, strata=True,
                      src="AMD Am29GL064: StrataFlash, буфер 32 байта, 25h/D0h, 3 В"),
    "Am29GL128": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                      size=16 << 20, volts=3, bypass=True, strata=True,
                      src="AMD Am29GL128: StrataFlash, буфер 32 байта, 25h/D0h, 3 В"),
    "Am29GL256": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                      size=32 << 20, volts=3, bypass=True, strata=True,
                      src="AMD Am29GL256: StrataFlash, буфер 32 байта, 25h/D0h, 3 В"),
    "Am29GL512": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                      size=64 << 20, volts=3, bypass=True, strata=True,
                      src="AMD Am29GL512: StrataFlash, буфер 32 байта, 25h/D0h, 3 В"),
    "Am29GL1024": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                       size=128 << 20, volts=3, bypass=True, strata=True,
                       src="AMD Am29GL1024: StrataFlash, буфер 32 байта, 25h/D0h, 3 В"),
    # Fujitsu MBM29DL32xTE/BE — «32 M-bit, 3.0 volt-only», копия AMD 21534d;
    # раскладка та же (8 boot-секторов по 8 КиБ), суффикс BE/TE её и задаёт.
    "MBM29DL322": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                       size=4 << 20, volts=3, bypass=True, strata=True, ambiguous=True,
                       why="Fujitsu MBM29DL322 без суффикса: нужен BE (bottom) или "
                           "TE (top); укажите MBM29DL322BE/TE",
                       src="Fujitsu MBM29DL32xTE/BE: 3,0 В, boot 8 КиБ"),
    "MBM29DL642": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                       size=8 << 20, volts=3, bypass=True, strata=True, ambiguous=True,
                       why="Fujitsu MBM29DL642: 64-Мбит двухбанковое семейство, "
                           "8-КиБ boot-секторы; раскладку ревизии не сверяли",
                       src="Fujitsu MBM29DL64x: 3,0 В (по аналогии с MBM29DL32x)"),
    "MBM29DL322BE": dict(sector=8 << 10, size=4 << 20, page=32, cmd=(0x555, 0x2AA),
                         wide=False, volts=3, bypass=True, strata=True,
                         sectors=[(8 << 10, 8), (64 << 10, 63)],
                         src="Fujitsu MBM29DL322BE (bottom boot): 8x8 КиБ + 63x64 КиБ, 3,0 В"),
    "MBM29DL322TE": dict(sector=64 << 10, size=4 << 20, page=32, cmd=(0x555, 0x2AA),
                         wide=False, volts=3, bypass=True, strata=True,
                         sectors=[(64 << 10, 63), (8 << 10, 8)],
                         src="Fujitsu MBM29DL322TE (top boot): 63x64 КиБ + 8x8 КиБ, 3,0 В"),
    "MBM29GL064": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                       size=8 << 20, volts=3, bypass=True, strata=True,
                       src="Fujitsu MBM29GL064: StrataFlash, буфер 32 байта, 25h/D0h, 3 В"),
    "MBM29GL128": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                       size=16 << 20, volts=3, bypass=True, strata=True,
                       src="Fujitsu MBM29GL128: StrataFlash, буфер 32 байта, 25h/D0h, 3 В"),
    # Первое поколение Intel (12 В): стирание только ВСЕГО чипа
    # («Bulk Erase» в каталоге Intel; у 28F020 в описании - «adds
    # electrical chip-erasure»). Команды Sector Erase у этих
    # микросхем нет, поэтому цикл «стереть сектор - записать» к ним
    # неприменим.
    # 2.17.15: 28F256/28F512 сверили по даташитам - у них ТОТ ЖЕ регистр
    # команд, что у 28F010/28F020 (00h/90h/20h+20h/40h/A0h/C0h/FFh), и
    # никаких 555h/2AAh. Раньше стояли AMD-адреса и не было ro: набор
    # послал бы AA/55/A0 «в никуда» и отчитался бы об успехе.
    "28F256": dict(sector=32 << 10, page=1, cmd=(0, 0), wide=False,
                   size=32 << 10, volts=12, ambiguous=True, protocol="intel",
                   ro=True,
                   why="стирание только ВСЕГО чипа (20h+20h, нужны 12 В на Vpp) "
                       "и нет статусного регистра 70h - набор её не пишет",
                   src="Intel 28F256A (32K x 8, даташит 290243-006): команды "
                       "00h/90h/20h+20h/40h/A0h/C0h/FFh, байт, 12 В +-5% на "
                       "Vpp, блочного стирания нет, ID 89h/B9h; то же у AMD "
                       "Am28F256 (11560) и ST M28F256"),
    "28F512": dict(sector=64 << 10, page=1, cmd=(0, 0), wide=False,
                   size=64 << 10, volts=12, ambiguous=True, protocol="intel",
                   ro=True,
                   why="стирание только ВСЕГО чипа (20h+20h, нужны 12 В на Vpp) "
                       "и нет статусного регистра 70h - набор её не пишет",
                   src="Intel 28F512 (64K x 8): команды 00h/90h/20h+20h/40h/"
                       "A0h/C0h/FFh, байт, 12 В +-5% на Vpp, блочного стирания "
                       "нет, ID 89h/B8h; то же у TI TMS28F512A и CAT28F512"),
    # Сверено по даташиту Intel 290207-010 (28F010, ноябрь 1995). Важное:
    # это НЕ JEDEC-микросхема - никаких 555h/2AAh: команды пишутся в регистр
    # команд одним байтом (Read 00h, ID 90h, Erase 20h+20h, Program 40h+PA/PD,
    # Erase Verify A0h, Program Verify C0h, Reset FFh+FFh). Стирание ТОЛЬКО
    # всего чипа (20h/20h), блочного нет; стирание и запись возможны только
    # при 12 В на Vpp (при Vpp < 6.5 В чип read-only); статусного регистра
    # (70h) нет - готовность проверяется командами A0h/C0h, а не опросом бит.
    # Поэтому строка помечена ro: набор такие микросхемы только читает, иначе
    # он послал бы AMD-команды и «успешно» дождался DQ7 у нетронутого массива.
    "28F010": dict(sector=128 << 10, page=1, cmd=(0, 0), wide=False,
                   size=128 << 10, volts=12, ambiguous=True, protocol="intel",
                   ro=True,
                   why="стирание только ВСЕГО чипа (20h+20h, нужны 12 В на Vpp) "
                       "и нет статусного регистра 70h - набор её не пишет",
                   src="Intel 28F010 (128K x 8, даташит 290207-010): команды "
                       "00h/90h/20h/40h/A0h/C0h/FFh, блочного стирания нет, "
                       "12 В +-5% на Vpp, чип-стирание 1 с тип., байт 10 мкс тип., "
                       "ID 89h/B4h"),
    "28F020": dict(sector=256 << 10, page=1, cmd=(0, 0), wide=False,
                   size=256 << 10, volts=12, ambiguous=True, protocol="intel",
                   ro=True,
                   why="стирание только ВСЕГО чипа (20h+20h, нужны 12 В на Vpp) "
                       "и нет статусного регистра 70h - набор её не пишет",
                   src="Intel 28F020: 256 KiB, то же первое поколение (12 В, "
                       "bulk erase); отдельного даташита в наборе нет - "
                       "поведение как у 28F010 (290207-010)"),
    # ====================================================================
    # Производители Hitachi, Mitsubishi, NEC, Sharp и др.
    # Параметры указаны по даташитам, где известны; иначе — безопасные
    # значения по умолчанию (сектор 64 КиБ, побайтовая запись A0h,
    # JEDEC-протокол, 5.0 В). Запись не реализуется без Vpp 12 В для чипов
    # с volts=12.
    # ====================================================================
    # --- Hitachi ---
    "HN28F010": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=128 << 10, volts=5, bypass=False,
                     unverified=True,
                     src="HN28F010: параметры даташитом не сверены; по схеме обозначений Hitachi (перечень программатора elnec) 5-вольтовый HN28F101 - это 128K x 8, а имён HN28F010/HN28F100 в перечнях нет; 20h не включаем"),
    "HN28F020": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=256 << 10, volts=5, bypass=False,
                     unverified=True,
                     src="HN28F020: параметры даташитом не сверены; по схеме обозначений Hitachi (перечень программатора elnec) 5-вольтовый HN28F101 - это 128K x 8, а имён HN28F010/HN28F100 в перечнях нет; 20h не включаем"),
    "HN28F100": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=1 << 20, volts=5, bypass=False,
                     unverified=True,
                     src="HN28F100: параметры даташитом не сверены; по схеме обозначений Hitachi (перечень программатора elnec) 5-вольтовый HN28F101 - это 128K x 8, а имён HN28F010/HN28F100 в перечнях нет; 20h не включаем"),
    # --- Mitsubishi ---
    "M5M29F010": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                      size=128 << 10, volts=5, bypass=False,
                      unverified=True,
                     src="M5M29F010: параметры даташитом не сверены; документа на это имя не нашли (искали 2026-11), 20h не включаем"),
    "M5M29F020": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                      size=256 << 10, volts=5, bypass=False,
                      unverified=True,
                     src="M5M29F020: параметры даташитом не сверены; документа на это имя не нашли (искали 2026-11), 20h не включаем"),
    # --- Intel в керамическом корпусе (D = CDIP), НЕ NEC ------------------
    # Аудит 2.17.17. В таблице эти две строки стояли как «NEC D28F010/020» с
    # JEDEC-адресами 555h/2AAh. Даташита NEC с таким именем найти не удалось,
    # зато три независимых источника говорят одно и то же: D28F... - это
    # ИСПОЛНЕНИЕ Intel 28F010/28F020 в керамическом корпусе (военная серия
    # MD28F...): в справочниках D28F010-120P1C4 числится за Intel (CERAMIC
    # DIP32, 128K x 8, «Command User Interface: YES», «Data Polling: NO»), в
    # перечне заказных комбинаций самого Intel 28F010 (290207-010) керамики
    # нет - она идёт отдельной военной серией MD28F..., а в таблице
    # соответствий AMD 1992 года Intel D28F010-150P1C4 сопоставлен
    # Am28F010-150DC. Значит и команды те же, что у 28F010/28F020: 00h/90h/
    # 20h+20h/40h/A0h/C0h/FFh (регистр команд, без 555h/2AAh), 12 В на Vpp,
    # стирание только ВСЕГО чипа, статусного регистра 70h нет.
    "D28F010": dict(sector=128 << 10, page=1, cmd=(0, 0), wide=False,
                    size=128 << 10, volts=12, ambiguous=True, protocol="intel",
                    ro=True,
                    why="стирание только ВСЕГО чипа (20h+20h, нужны 12 В на Vpp) "
                        "и нет статусного регистра 70h - набор её не пишет",
                    src="Intel 28F010 в керамическом корпусе (D = CDIP): команды "
                        "00h/90h/20h+20h/40h/A0h/C0h/FFh по даташиту 290207-010, "
                        "12 В +-5% на Vpp, ID 89h/B4h; в справочниках "
                        "D28F010-120P1C4 - Intel, CERAMIC DIP32, 128K x 8, "
                        "CUI есть, data polling нет"),
    "D28F020": dict(sector=256 << 10, page=1, cmd=(0, 0), wide=False,
                    size=256 << 10, volts=12, ambiguous=True, protocol="intel",
                    ro=True,
                    why="стирание только ВСЕГО чипа (20h+20h, нужны 12 В на Vpp) "
                        "и нет статусного регистра 70h - набор её не пишет",
                    src="Intel 28F020 в керамическом корпусе (D = CDIP): та же "
                        "семья, что 28F010 (290207-010) - 256 КиБ, 12 В, "
                        "стирание только целиком; MD28F020 у Rochester описан "
                        "как военный 28F020 (2048K, 256K x 8)"),
    # --- Sharp ---
    "LH28F010": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=128 << 10, volts=12, unverified=True,
                     src="LH28F010: параметры даташитом не сверены; документа на это имя не нашли (искали 2026-11), 20h не включаем"),
    "LH28F020": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=256 << 10, volts=12, unverified=True,
                     src="LH28F020: параметры даташитом не сверены; документа на это имя не нашли (искали 2026-11), 20h не включаем"),
    # --- Intel, военная серия (M28F...), НЕ ST --------------------------
    # Аудит 2.17.18. Строка числилась за ST и стояла с JEDEC-адресами
    # 555h/2AAh. Источники говорят другое: M28F008 - это ВОЕННОЕ исполнение
    # Intel 8 Мбит (в справочниках прямо «INTEL — 8 MBIT (1 MBIT x 8) FLASH
    # MEMORY», 28 страниц; у Rochester то же описание с «Extended Cycling
    # Capability»). Архитектура та же, что у 28F008SA: 16 блоков по 64 КиБ,
    # CUI + статусный регистр, 12 В на Vpp, команда байтом. Документ
    # MS28F016SV перечисляет M28F008 среди совместимых по набору команд.
    "M28F008": dict(sector=64 << 10, page=1, cmd=(0, 0), wide=False,
                    size=1 << 20, volts=12, protocol="intel",
                    src="Intel M28F008 (военная серия Intel, 8 Мбит = 1 МиБ, "
                        "x8): 16 блоков по 64 КиБ, CUI + статусный регистр, "
                        "12 В на Vpp - то же, что 28F008SA (290429-008); "
                        "в справочниках M28F008 числится за Intel "
                        "(«8 MBIT (1 MBIT x 8) FLASH MEMORY»)"),
    # --- Samsung (дополнительно к K9*) ---
    "K9F1208U0A": dict(sector=16 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                       size=64 << 20, volts=3.3, bypass=False, ro=True,
                       why="NAND, а не NOR: чтение и запись идут страницами (512 + "
                           "16 байт), стирание - блоком командой 60h с "
                           "подтверждением D0h, адрес, команды и данные передаются "
                           "по одним линиям I/O0-I/O7. Линейно, как NOR-карта, эта "
                           "микросхема не читается, и стирать её командами "
                           "20h/30h/D0h нельзя - набор её не трогает",
                       src="Samsung K9F1208U0A (Rev 1.6): «64M x 8 Bit / 32M x 16 "
                         "Bit NAND Flash Memory», массив (64M + 2048K)bit x 8, "
                         "страница (512 + 16) байт, блок (16K + 512) байт, 4096 "
                         "блоков, Erase Setup 60h + Erase Confirm D0h"),
    "K9F2G08U0A": dict(sector=128 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                       size=256 << 20, volts=3.3, bypass=False, ro=True,
                       why="NAND, а не NOR: чтение и запись идут страницами (2K + "
                           "64 байт), стирание - блоком командой 60h с "
                           "подтверждением D0h, адрес, команды и данные передаются "
                           "по одним линиям I/O0-I/O7. Линейно, как NOR-карта, эта "
                           "микросхема не читается, и стирать её командами "
                           "20h/30h/D0h нельзя - набор её не трогает",
                       src="Samsung K9F2G08U0A: «256M x 8 Bit NAND Flash Memory», "
                         "массив (256M + 8,192K)bit x 8, страница (2K + 64) байт, "
                         "блок (128K + 4K) байт, 2048 блоков, Erase Setup 60h + "
                         "Erase Confirm D0h"),
}


# Обозначения, которых в даташитах нет (или которые принадлежат другой
# микросхеме). Раньше они молча получали «умолчания как у Am29F016»: 64 КиБ
# сектор, 2 МиБ - то есть на boot-чипе первое же стирание задело бы 16K+8K+8K+
# 32K. Теперь это отказ с подсказкой, какое имя указать.
WRONG_NAMES = {
    "AT29F040": "у Atmel нет AT29F040: 5-вольтовый флеш с командами JEDEC - это "
                "AT49F040A (512 КиБ, 16K+2x8K+32K+7x64K); AT29C040A - страничная "
                "запись без команды стирания 30h. В наборе ни та, ни другая не "
                "заведена",
    "AT29F040B": "у Atmel нет AT29F040B: см. AT49F040A (512 КиБ, boot-блоки) или "
                 "AT29C040A (страничная запись, без 30h). В наборе ни та, ни "
                 "другая не заведена",
    "AT29F016": "у Atmel нет AT29F016: AT29-серия - страничная запись (512/010/"
                "020/040), AT49F - до 1 МиБ; 16-Мбит часть у Atmel - x16 "
                "(AT49BV16xx), для байтовой карты не подходит. В наборе таких "
                "частей нет",
    "AT29F080": "у Atmel нет AT29F080: 1 МиБ байтовая - AT49F080 (стирание только "
                "чипом, команды Sector Erase нет); AT29C-серия - страничная "
                "запись. В наборе эта часть не заведена",
    "SST39VF016": "у SST нет 39VF016: 16-Мбит часть называется SST39VF160, и она "
                  "x16 (3 В), а не байтовая; байтовые 5-вольтовые - до SST39SF040",
    "SST39SF016": "у SST нет 39SF016: 5-вольтовая байтовая серия кончается на "
                  "SST39SF040 (512 КиБ)",
    "28F002SA": "у Intel нет 28F002SA: 2-Мбит boot-block - 28F002BV/BX (x8/x16), "
                "байтовое семейство Smart 5 - 28F004S5/28F008S5/28F016S5",
    "28F004SA": "у Intel нет 28F004SA: 4-Мбит байтовая - 28F004S5 (8 блоков по "
                "64 КиБ), она же 28F004S3/SC",
    "TC58FVT320": "у Toshiba нет TC58FVT320: 32-Мбит часть называется TC58FVT321 "
                  "(4 МиБ, 63x64 КиБ + 8x8 КиБ, 3 В, команды AAAh/555h)",
    "M28F016S5": "у ST нет M28F016S5: байтовое семейство Smart 5 выпускала Intel "
                 "(28F016S5, 5 В, 32 x 64 КиБ)",
}
