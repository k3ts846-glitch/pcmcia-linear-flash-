#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Регрессии, найденные при разборе кода (стирание, CFI, геометрия, запись).

Зачем отдельный файл: остальные проверки набора зелёные И ПРИ ЭТОМ не ловили
ни одну из ошибок ниже. Модель чипа повторяла ошибку кода (30h в 0x555, поля
CFI со сдвигом на байт), поэтому «231 проверка пройдена» ничего не говорила
о правильности стирания. Здесь каждая проверка сформулирована по даташиту:

  * 30h стирает сектор ПО СВОЕМУ АДРЕСУ, а не сектор 0;
  * проверка геометрии не уничтожает данные (снимок и возврат);
  * CFI читается по раскладке cfi.h: 0x2C - номер региона, 0x2D.. - регионы;
  * автоопределение по JEDEC ID сравнивает числа, а не строку «AMD»;
  * у boot-микросхем (16K+8K+8K+32K+64K) стирание идёт по КАРТЕ секторов,
    а не «округлением по 64 КиБ» (раньше такие чипы просто отвергались);
  * если микросхема не знает Unlock Bypass (20h), запись уходит на A0h.

Запуск: python3 tests/test_flash_regressions.py   (карта и /dev/mem не нужны)
"""
import os
import pathlib
import sys
import tempfile

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT)
sys.path.insert(0, os.path.join(KIT, "src"))
sys.path.insert(0, os.path.join(KIT, "tests"))

import nor_model as NM                                              # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  ok   " + name)
    else:
        FAIL.append((name, detail))
        print("  FAIL " + name + ("   " + str(detail) if detail else ""))


def section(title):
    print("\n[" + title + "]")


# --- 1. Стирание: 30h должен попасть по адресу сектора -------------------------
# Даташит AMD/MX «Sector Erase Command Sequence»: 6 циклов, шестой - 30h по
# адресу SA. Прежний код слал 30h в командный адрес 0x555, то есть ВСЕГДА
# стирал сектор 0 (у карты - это паспорт CIS).
section("стирание сектора по адресу (даташит: 555/AA, 2AA/55, 555/80, "
        "555/AA, 2AA/55, SA/30)")

card, bus, flash = NM.make(kit=KIT, chips=2, chip_name="Am29F016", blank=False)
D = card.d                     # сам модуль windows/legacy/pcmcia_direct.py
sec = flash.sector                       # сектор в адресах КАРТЫ (2 чипа)
target = 4 * sec                         # пятый сектор карты
sector0_before = flash.read(0, sec)      # там лежит CIS - трогать нельзя
target_before = flash.read(target, sec)
ok, why = flash.erase_sector(target + 1234)   # адрес внутри целевого сектора
check("стирание вернуло «готово»", ok, why)
chip_local = target // card.chips
check("30h ушёл по адресу целевого сектора в ОБОИХ чипах",
      sorted(card.erase_targets) == [(0, chip_local), (1, chip_local)],
      str(card.erase_targets))
after0 = flash.read(0, sec)
after_t = flash.read(target, sec)
check("сектор 0 (CIS) не тронут", after0 == sector0_before,
      "первые байты: %s" % after0[:8].hex())
check("целевой сектор стёрт (только 0xFF)", set(after_t) == {0xFF},
      "осталось не-0xFF байт: %d" % sum(1 for b in after_t if b != 0xFF))
check("стирался именно запрошенный сектор, а не соседний",
      target_before != after_t)

# --- 2. Проверка геометрии не должна уничтожать данные -------------------------
# Прежняя версия стирала последний сектор карты и не возвращала его.
section("проверка геометрии: снимок -> стирание -> возврат")

card, bus, flash = NM.make(kit=KIT, chips=2, chip_name="Am29F016", blank=False)
geo = {"sector": flash.sector, "size": card.chips * card.chip_size}
off = geo["size"] - geo["sector"]
before = flash.read(off, geo["sector"])
backup = os.path.join(tempfile.mkdtemp(prefix="geo-"), "probe.bin")
ok, why = D.verify_geometry(flash, geo, backup_path=backup)
check("проверка прошла на исправной модели", ok, why)
check("сектор возвращён побайтово", flash.read(off, geo["sector"]) == before)
with open(backup, "rb") as f:
    check("копия сектора сохранена в файл", f.read() == before)

card, bus, flash = NM.make(kit=KIT, chips=2, chip_name="Am29F016", blank=False,
                           refuse_erase=True)
geo = {"sector": flash.sector, "size": card.chips * card.chip_size}
off = geo["size"] - geo["sector"]
before = flash.read(off, geo["sector"])
ok, why = D.verify_geometry(flash, geo)
check("отказ стирания -> честный False", not ok, why)
check("при отказе стирания данные не потеряны",
      flash.read(off, geo["sector"]) == before)

# --- 3. Таблица CFI: раскладка cfi.h (а не «на байт раньше») -------------------
section("CFI (98h): ёмкость, регионы, версия")

card, bus, flash = NM.make(kit=KIT, chips=2, chip_name="Am29F016", blank=False)
cfi = D._autodetect_cfi(flash)
check("CFI отвечает («QRY»)", bool(cfi), "пусто - значит 98h не дошла")
check("CFI: ёмкость чипа 2 МиБ", cfi.get("size_chip") == 2 << 20,
      str(cfi.get("size_chip")))
check("CFI: один регион 32 x 64 КиБ",
      cfi.get("regions") == [{"blocks": 32, "size": 65536}],
      str(cfi.get("regions")))
check("CFI: сектор 64 КиБ", cfi.get("sector") == 65536, str(cfi.get("sector")))
check("CFI: буфера записи нет (page=1, A0h побайтно)",
      cfi.get("page") == 1 and not cfi.get("has_write_buffer"))
check("CFI: версия из расширенной таблицы = 1.2",
      cfi.get("cfi_version") == "1.2", str(cfi.get("cfi_version")))
check("CFI: Vcc при записи 3.0-3.6 В",
      cfi.get("vcc_min_write") == 3000 and cfi.get("vcc_max_write") == 3600,
      str((cfi.get("vcc_min_write"), cfi.get("vcc_max_write"))))

card, bus, flash = NM.make(kit=KIT, chips=2, chip_name="Am29F016", blank=False,
                           no_cfi=True)
check("микросхема без CFI -> пустой ответ, а не мусор",
      D._autodetect_cfi(flash) == {})

# --- 4. Автоопределение по JEDEC ID и работа без Unlock Bypass -----------------
section("JEDEC ID и Unlock Bypass")

card, bus, flash = NM.make(kit=KIT, chips=2, chip_name="Am29F016", blank=False)
cfi = D._autodetect_cfi(flash)
man, dev = flash.read_id()
check("JEDEC ID микросхемы читается", (man, dev) == (0x01, 0xAD),
      "%r" % ((man, dev),))

card, bus, flash = NM.make(kit=KIT, chips=2, chip_name="Am29F016", blank=True,
                           supports_bypass=False)
flash.bypass = True            # таблица говорит «bypass есть», а чип его не знает
flash._bypass_active = False
ok, why = flash.program_byte(0x1000, 0x5A)
check("запись без Unlock Bypass не падает, а уходит на A0h", ok, why)
check("байт записан правильно", flash.read(0x1000, 1) == b"\x5a",
      flash.read(0x1000, 1).hex())
check("после отказа bypass больше не пробуется", flash._bypass_broken)

# --- 5. Карта секторов: стирание boot-чипа по её границам ---------------------
# Даташит AMD Am29F800B (22358) / Am29LV160D: у boot-микросхем секторы разного
# размера - 16K + 2x8K + 32K + N x 64K (bottom-boot) или то же в обратном
# порядке (top-boot). Команда 30h стирает ТОЛЬКО тот сектор, в который попал
# её адрес, поэтому «округление по 64 КиБ» на таком чипе стирает сразу четыре
# сектора - вместе с паспортом карты. Проверяем, что границы берутся по карте.
section("boot-чип: границы секторов по карте, а не по 64 КиБ")

boot_card, _boot_bus, _boot_flash = NM.make(kit=KIT, chips=1,
                                            chip_name="Am29F800BB", blank=False)
D2 = boot_card.d

gBB = D2.geometry_for("Am29F800BB")
check("Am29F800BB: карта 16K + 2x8K + 32K + 15x64K",
      D2.sector_map(gBB) == [(16384, 1), (8192, 2), (32768, 1), (65536, 15)],
      str(D2.sector_map(gBB)))
check("Am29F800BB: адрес 0x6000 - это 8-КиБ сектор 0x6000",
      D2.sector_at(gBB, 0x6000) == (0x6000, 0x2000),
      str(D2.sector_at(gBB, 0x6000)))
check("Am29F800BB: адрес 0x8000 - это 32-КиБ сектор 0x8000",
      D2.sector_at(gBB, 0x8000) == (0x8000, 0x8000),
      str(D2.sector_at(gBB, 0x8000)))
try:
    D2.check_sector_map(D2.geometry_for("Am29F800"))
    check("Am29F800 без ревизии отвергнут (BB и BT стирают по-разному)", False)
except D2.DirectError as exc:
    check("Am29F800 без ревизии отвергнут (BB и BT стирают по-разному)",
          "ревизи" in str(exc), str(exc)[:80])

# Стирание на модели: стирается РОВНО один сектор карты, соседние целы.
img_bb = bytes((i * 7 + 3) & 0xFF for i in range(1 << 20))
template = bytes(img_bb)
boot_card.mem[0][:] = template
ok, why = _boot_flash.erase_sector(0x6000 + 7)          # внутри 8-КиБ сектора
check("стирание 8-КиБ сектора: стёрт только 0x6000-0x7FFF",
      ok and _boot_flash.read(0x6000, 0x2000) == b"\xff" * 0x2000
      and _boot_flash.read(0x5FFF, 1) == template[0x5FFF:0x6000]
      and _boot_flash.read(0x8000, 1) == template[0x8000:0x8001], why)
boot_card.mem[0][:] = template
ok, why = _boot_flash.erase_sector(3)                   # внутри 16-КиБ сектора
check("стирание 16-КиБ сектора: стёрт только 0x0000-0x3FFF",
      ok and _boot_flash.read(0, 0x4000) == b"\xff" * 0x4000
      and _boot_flash.read(0x4000, 1) == template[0x4000:0x4001], why)
boot_card.mem[0][:] = template
ok, why = _boot_flash.erase_sector(0x8001)              # внутри 32-КиБ сектора
check("стирание 32-КиБ сектора: стёрт только 0x8000-0xFFFF",
      ok and _boot_flash.read(0x8000, 0x8000) == b"\xff" * 0x8000
      and _boot_flash.read(0x10000, 4) == template[0x10000:0x10004], why)
check("boot-чип 1 МиБ = 19 секторов в адресах карты",
      len(_boot_flash.spans(1 << 20)) == 19
      and _boot_flash.spans(1 << 20)[-1] == (0xF0000, 0x10000),
      str(_boot_flash.spans(1 << 20)[-1]))

# top-boot: мелкие секторы в КОНЦЕ, и проверка геометрии должна брать именно
# последний (самый маленький) - иначе она «проверяет» не тот сектор.
bt_card, _bt_bus, bt_flash = NM.make(kit=KIT, chips=1, chip_name="Am29F800BT",
                                     blank=False)
bt_card.mem[0][:] = template
check("top-boot: последний сектор - 16 КиБ по адресу 0xFC000",
      bt_flash.spans(1 << 20)[-1] == (0xFC000, 0x4000),
      str(bt_flash.spans(1 << 20)[-1]))
ok, why = bt_flash.erase_sector(0xFC005)
check("top-boot: стирание 16-КиБ сектора не трогает 0xFBFFF",
      ok and bt_flash.read(0xFC000, 0x4000) == b"\xff" * 0x4000
      and bt_flash.read(0xFBFFF, 1) == template[0xFBFFF:0xFC000], why)
bt_card.mem[0][:] = template
seen = []
_orig_erase = bt_flash.erase_sector
bt_flash.erase_sector = lambda off: (seen.append(off), _orig_erase(off))[1]
ok_g, why_g = D2.verify_geometry(bt_flash, {"sector": bt_flash.sector,
                                            "size": 1 << 20})
check("проверка геометрии берёт последний сектор по карте (0xFC000), "
      "а данные возвращает",
      ok_g and seen == [0xFC000] and bytes(bt_card.mem[0]) == template,
      "%s %s" % (why_g, [hex(x) for x in seen]))
bt_flash.erase_sector = _orig_erase

# План записи обязан идти по границам карты: иначе chunk[i] соберётся из двух
# разных секторов, и стирание одного из них уничтожит данные другого.
zeros = bytes(1 << 20)
plan_bb = D2.flash_plan(zeros, _boot_flash.sector, _boot_flash.spans(len(zeros)))
check("flash_plan по карте: 19 шагов, каждый начинается с границы сектора",
      len(plan_bb) == 19
      and [p[0] for p in plan_bb] == [s[0] for s in _boot_flash.spans(1 << 20)],
      str([p[0] for p in plan_bb][:5]))
check("flash_plan по карте: размер шага равен размеру сектора",
      [len(p[2]) for p in plan_bb] == [s[1] for s in _boot_flash.spans(1 << 20)],
      str([s[1] for s in _boot_flash.spans(1 << 20)]))


# --- 6. CFI снимает вопрос о ревизии -----------------------------------------
# У Am29F800 код устройства 58h одинаков у BB и BT, то есть JEDEC ID ревизию не
# различает. Но CFI отдаёт erase-регионы - и по ним карта восстанавливается.
section("CFI: erase-регионы = карта секторов (снимает вопрос о ревизии)")

cfi_card, _cfi_bus, cfi_flash = NM.make(kit=KIT, chips=1,
                                        chip_name="Am29F800BB")
geo_cfi = D2.identify_chip(cfi_flash)
check("CFI дал 4 региона -> sectors = 16K+2x8K+32K+15x64K",
      geo_cfi["sectors"] == [(16384, 1), (8192, 2), (32768, 1), (65536, 15)],
      str(geo_cfi["sectors"]))
check("неоднозначность снята: ambiguous=False", not geo_cfi["ambiguous"])
check("в отчёте видно, откуда карта",
      "карта секторов из CFI" in (geo_cfi.get("how") or ""),
      str(geo_cfi.get("how")))
# Без CFI ревизию различает только код устройства - и он РАЗНЫЙ:
# include/flashrom include/flashchips.h: AMD_AM29F800BB 0x58, AMD_AM29F800BT
# 0xD6 (AMD pub#20375B). Прежняя таблица набора этого не знала и называла
# 0x58 «Am29F800» без ревизии, то есть выбирала геометрию наугад.
check("коды с ревизией: 0x58 -> Am29F800BB, 0xD6 -> Am29F800BT",
      D2.vendor_part(0x01, 0x58) == ("Am29F800BB", 8)
      and D2.vendor_part(0x01, 0xD6) == ("Am29F800BT", 8),
      str((D2.vendor_part(0x01, 0x58), D2.vendor_part(0x01, 0xD6))))
check("Fujitsu (вендор 04h): 0xAD -> MBM29F016A",
      D2.vendor_part(0x04, 0xAD) == ("MBM29F016A", 16),
      str(D2.vendor_part(0x04, 0xAD)))
bad_cap = [hex(did) for did, (name, mbit) in D2.AMD_PARTS.items()
           if name not in D2.GEOMETRY
           or D2.GEOMETRY[name]["size"] * 8 // (1 << 20) != mbit]
check("ёмкость в таблице ID сходится с ёмкостью микросхемы "
      "(было: 0xAB «32 Мбит», 0xD5 «16 Мбит», 0x34 «256 Мбит»)",
      not bad_cap, str(bad_cap))

bt_id_card, _bt_id_bus, bt_id_flash = NM.make(kit=KIT, chips=1,
                                              chip_name="Am29F800BT",
                                              no_cfi=True)
geo_bt_id = D2.identify_chip(bt_id_flash)
check("без CFI код 0xD6 даёт top-boot карту (мелкие секторы в конце)",
      geo_bt_id["name"] == "Am29F800BT"
      and D2.sector_map(geo_bt_id)[-1] == (16384, 1)
      and D2.sector_at(geo_bt_id, 0xFC000) == (0xFC000, 0x4000),
      str((geo_bt_id["name"], D2.sector_map(geo_bt_id)[-1:])))


# --- 8. Пакет src/pcmcia_flash: те же правила, чистая логика -------------------
section("src/pcmcia_flash: CFI, автоопределение, таблица микросхем")

from pcmcia_flash import chips as C                                 # noqa: E402


class FakeFlash:
    """Микросхема без шины: только то, что нужно read_cfi/identify_chip."""

    def __init__(self, table, ids=(0x01, 0xAD), cfi_ok=True):
        self.chips = 2
        self.table = table
        self.ids = ids
        self.cfi_ok = cfi_ok
        self.in_cfi = False
        self.sent = []

    def _send(self, local, value):
        self.sent.append((local, value))
        if value == 0x98 and local == 0x55 and self.cfi_ok:
            self.in_cfi = True

    def read_byte(self, card_off):
        _chip, local = card_off % self.chips, card_off // self.chips
        if self.in_cfi and local < len(self.table):
            return self.table[local]
        return 0xFF

    def read_id(self):
        return self.ids

    def reset(self):
        self.in_cfi = False


table = NM.ModelCard(kit=KIT, chip_name="Am29F016")._cfi_bytes()
ff = FakeFlash(table)
cfi = C.read_cfi(ff)
check("read_cfi: 98h послан ОДИН раз по адресу 55h (даташит)",
      ff.sent == [(0x55, 0x98)], str(ff.sent))
check("read_cfi: ёмкость 2 МиБ", cfi.get("size") == 2 << 20, str(cfi.get("size")))
check("read_cfi: сектор 64 КиБ", cfi.get("sector") == 65536)
check("read_cfi: набор команд AMD/Fujitsu",
      "AMD" in (cfi.get("cmdset_name") or ""), str(cfi.get("cmdset_name")))
check("read_cfi: без ответа на 98h - пустой dict",
      C.read_cfi(FakeFlash(table, cfi_ok=False)) == {})

geo = C.identify_chip(ff, info={})
check("identify_chip: JEDEC 01AD -> Am29F016 по числу, а не по строке",
      geo.get("name") == "Am29F016", str(geo.get("name")))
check("identify_chip: сектор 64 КиБ", geo.get("sector") == 65536)
geo = C.identify_chip(ff, info={"product": ["FLASH", "AM29F016D"]})
check("паспорт: AM29F016D побеждает AM29F016 (длинное совпадение)",
      geo.get("name") == "Am29F016D", str(geo.get("name")))
geo = C.identify_chip(FakeFlash(table, ids=(None, None)),
                      info={"manufacturer": "0x04", "device": "0xad"})
check("микросхема молчит -> JEDEC ID берётся из паспорта ('0x04'/'0xad')",
      geo.get("name") == "MBM29F016", str(geo.get("name")))

# boot-чип: карта секторов должна сходиться с ёмкостью и давать правильные
# границы - 16K, 2x8K, 32K в начале, дальше по 64K
boot = C.geometry_for("Am29F400AB")
check("boot-чип: карта секторов 16K+2x8K+32K+7x64K",
      C.sector_map(boot) == [(16384, 1), (8192, 2), (32768, 1), (65536, 7)],
      str(C.sector_map(boot)))
check("boot-чип: адрес 0x8000 попадает в 32-КиБ сектор 0x8000",
      C.sector_at(boot, 0x8000) == (0x8000, 0x8000),
      str(C.sector_at(boot, 0x8000)))
check("boot-чип: адрес 0x7FFF попадает в 8-КиБ сектор 0x6000",
      C.sector_at(boot, 0x7FFF) == (0x6000, 0x2000),
      str(C.sector_at(boot, 0x7FFF)))
top = C.geometry_for("Am29F400AT")
# 512 КиБ, top-boot: 0x00000..0x6FFFF по 64K, далее 32K, 8K, 8K и 16K последним
check("top-boot: мелкие секторы в КОНЦЕ (последний - 16 КиБ)",
      C.sector_map(top) == [(65536, 7), (32768, 1), (8192, 2), (16384, 1)]
      and C.sector_at(top, 0x7FFFF) == (0x7C000, 0x4000)
      and C.sector_at(top, 0x70000) == (0x70000, 0x8000),
      str((C.sector_map(top), C.sector_at(top, 0x7FFFF))))
bad_maps = [n for n, g in C.CHIP_GEOMETRY.items()
            if sum(sz * cnt for sz, cnt in C.sector_map(g)) != g["size"]]
check("у всех чипов карта секторов сходится с ёмкостью", not bad_maps,
      str(bad_maps[:3]))
check("Am29LV400BB = 4 Мбит = 512 КиБ (было 4 МиБ)",
      C.CHIP_GEOMETRY["Am29LV400BB"]["size"] == 512 << 10,
      str(C.CHIP_GEOMETRY["Am29LV400BB"]["size"]))
check("Am29LV800BT = 8 Мбит = 1 МиБ (было 8 МиБ)",
      C.CHIP_GEOMETRY["Am29LV800BT"]["size"] == 1 << 20)
check("Am29F016 без Unlock Bypass, Am29F016D с ним",
      not C.CHIP_GEOMETRY["Am29F016"]["bypass"]
      and C.CHIP_GEOMETRY["Am29F016D"]["bypass"])
check("в таблице нет выдуманных «производителей вместо моделей»",
      not any(n in C.CHIP_GEOMETRY for n in
              ("ToshibaTC58", "SamsungNAND", "HitachiHN28F", "NECuPD",
               "SharpLH", "STMicro", "MitsubishiM5M")))


# --- 7. Fujitsu MBM29F002/004 и ST M29F002/M29F400: раскладки из даташитов ---
# Fujitsu DS05-20868-3E: «One 16K byte, two 8K bytes, one 32K byte, and three
# 64K bytes», «T = Top sector, B = Bottom sector». 4-Мбит ревизии - тот же ряд
# блоков, что у AMD Am29F004B («...and seven 64 Kbyte sectors»). ST M29F400B:
# «11 memory blocks - 1 Boot Block, 2 Parameter and 8 Main Blocks», коды
# 00D5h (top) и 00D6h (bottom) при вендоре 0020h.
section("Fujitsu и ST: карты boot-блоков по даташитам")

NEW_MAPS = {
    "MBM29F002BC": (256 << 10, [(16384, 1), (8192, 2), (32768, 1), (65536, 3)]),
    "MBM29F002TC": (256 << 10, [(65536, 3), (32768, 1), (8192, 2), (16384, 1)]),
    "MBM29F004BC": (512 << 10, [(16384, 1), (8192, 2), (32768, 1), (65536, 7)]),
    "MBM29F004TC": (512 << 10, [(65536, 7), (32768, 1), (8192, 2), (16384, 1)]),
    "M29F002BB": (256 << 10, [(16384, 1), (8192, 2), (32768, 1), (65536, 3)]),
    "M29F002BT": (256 << 10, [(65536, 3), (32768, 1), (8192, 2), (16384, 1)]),
    "M29F400BB": (512 << 10, [(16384, 1), (8192, 2), (32768, 1), (65536, 7)]),
    "M29F400BT": (512 << 10, [(65536, 7), (32768, 1), (8192, 2), (16384, 1)]),
}
bad = []
for name, (size, want) in NEW_MAPS.items():
    geo_both = []
    for where, table in (("монолит", D.GEOMETRY), ("пакет", C.CHIP_GEOMETRY)):
        geo = table.get(name)
        if not geo:
            bad.append("%s: нет в %s" % (name, where))
            continue
        if geo["size"] != size or C.sector_map(geo) != want:
            bad.append("%s: %s size=%s map=%s" % (name, where, geo["size"],
                                                  C.sector_map(geo)))
        geo_both.append(geo)
    if len(geo_both) == 2:
        C.check_sector_map(geo_both[1])          # карта сходится с ёмкостью
check("8 новых boot-ревизий (Fujitsu + ST) есть в обеих таблицах и совпадают",
      not bad, str(bad[:3]))

check("ST: вендор 20h, коды 00D5h/00D6h -> M29F400BT/BB "
      "(не путать с AMD 01h:D5h = Am29F080)",
      D.vendor_part(0x20, 0xD5) == ("M29F400BT", 4)
      and D.vendor_part(0x20, 0xD6) == ("M29F400BB", 4)
      and D.vendor_part(0x01, 0xD5) == ("Am29F080", 8),
      str((D.vendor_part(0x20, 0xD5), D.vendor_part(0x01, 0xD5))))

# Имена, которых нет в даташитах, должны ОТКАЗЫВАТЬ, а не подставлять
# умолчания Am29F016 (те дали бы 2 МиБ и однородную карту).
for name in ("M28F002", "M28F004", "M5M29F400", "MBM29F002", "MBM29F004"):
    try:
        D.check_sector_map(D.geometry_for(name))
        check("%s: отказ с подсказкой, а не умолчания" % name, False)
    except D.DirectError as exc:
        msg = str(exc)
        check("%s: отказ с подсказкой, а не умолчания" % name,
              "ревизи" in msg and "Укажите" in msg, msg[:70])

# Стирание на модели: у MBM29F002BC первый сектор 16 КиБ, а у TC - 16 КиБ
# ПОСЛЕДНИЙ; проверяем, что стирается именно он.
for name, off, size in (("MBM29F002BC", 0x00000, 16 << 10),
                         ("MBM29F002TC", 0x3C000, 16 << 10)):
    card, _bus, flash = NM.make(kit=KIT, chips=1, chip_name=name, blank=False)
    img = bytes((i * 11 + 5) & 0xFF for i in range(256 << 10))
    card.mem[0][:] = img
    ok, why = flash.erase_sector(off + 3)
    check("%s: 16-КиБ boot-сектор стирается по адресу %#x" % (name, off),
          ok and flash.read(off, size) == b"\xff" * size, why)


# Две реализации должны говорить об одном и том же: пакет и монолит -
# независимые копии, и именно так они разошлись (28F016S5: 128 КиБ против
# 64 КиБ, дело дошло до ложного отказа blank-check).
section("две реализации не расходятся в геометрии")

shared = set(C.CHIP_GEOMETRY) & set(D.GEOMETRY)
drift = [(n, C.CHIP_GEOMETRY[n]["size"], D.GEOMETRY[n]["size"],
          C.sector_map(C.CHIP_GEOMETRY[n]), D.sector_map(D.GEOMETRY[n]))
         for n in sorted(shared)
         if C.CHIP_GEOMETRY[n]["size"] != D.GEOMETRY[n]["size"]
         or C.sector_map(C.CHIP_GEOMETRY[n]) != D.sector_map(D.GEOMETRY[n])]
check("ёмкость и карта секторов совпадают у всех %d общих имён" % len(shared),
      bool(shared) and not drift, str(drift[:2]))

# Повторяющийся ключ в литерале молча перекрывает предыдущий: 6 таких пар
# было в GEOMETRY (28F008SA, 28F016SA, 28F016S5, 28F320, M28F102, M28F016S5).
import ast                                                          # noqa: E402

dup_keys = []
for node in ast.walk(ast.parse(pathlib.Path(KIT, "windows/legacy",
                                            "pcmcia_direct.py")
                              .read_text(encoding="utf-8"))):
    if isinstance(node, ast.Dict):
        seen = {}
        for k in node.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                seen.setdefault(k.value, []).append(k.lineno)
        dup_keys += ["%s:%s" % (k, v) for k, v in seen.items() if len(v) > 1]
check("в GEOMETRY нет повторяющихся ключей (они молча перекрывают друг друга)",
      not dup_keys, str(dup_keys[:3]))
# Таблица секторов карты для boot-пары: карта чипа масштабируется на число
# чипов (8 КиБ чипа = 16 КиБ карты), поэтому у пары Am29F800BB первый сектор
# 32 КиБ, а не 16, и вся карта 2 МиБ укладывается в 19 секторов.
bb_geo = C.geometry_for("Am29F800BB")
bb_tbl = C.sector_table(bb_geo, 2 << 20, chips=2)
check("sector_table boot-пары: 19 секторов, первый 32 КиБ, сумма = ёмкость",
      len(bb_tbl) == 19 and bb_tbl[0]["size"] == 32 << 10
      and bb_tbl[-1]["size"] == 128 << 10
      and sum(s["size"] for s in bb_tbl) == 2 << 20,
      str((len(bb_tbl), bb_tbl[0], bb_tbl[-1])))
mb_tbl = D.sector_table(bb_geo, 2 << 20, chips=2)
check("sector_table в монолите даёт то же самое",
      [(s["offset"], s["size"]) for s in bb_tbl] == mb_tbl,
      str(mb_tbl[:2]))

check("28F016S5: 32 x 64 КиБ (даташит Intel «16 Mbit: Thirty-Two Blocks»), "
      "а не 16 x 128 КиБ",
      D.GEOMETRY["28F016S5"]["sector"] == 64 << 10
      and D.sector_map(D.GEOMETRY["28F016S5"]) == [(65536, 32)],
      str(D.sector_map(D.GEOMETRY["28F016S5"])))


# verify_geometry в пакете: отдельная копия логики, проверяем и её
class FakeNor:
    def __init__(self, data, fail_erase=False, fail_program=False):
        self.mem = bytearray(data)
        self.sector = 4096
        self.fail_erase = fail_erase
        self.fail_program = fail_program

    def read(self, off, n):
        return bytes(self.mem[off:off + n])

    def read_byte(self, off):
        return self.mem[off]

    def erase_sector(self, off):
        if self.fail_erase:
            return False, "стирание не прошло (модель)"
        self.mem[off:off + self.sector] = b"\xff" * self.sector
        return True, ""

    def program_byte(self, off, value):
        if self.fail_program:
            return False, "запись не прошла (модель)"
        self.mem[off] &= value & 0xFF
        return True, ""


data = bytes(range(256)) * 32
fake = FakeNor(data)
ok, why = C.verify_geometry(fake, {"sector": 4096, "size": len(data)})
check("src verify_geometry: ок на исправной модели", ok, why)
check("src verify_geometry: данные возвращены", bytes(fake.mem) == data)

fake = FakeNor(data, fail_erase=True)
ok, why = C.verify_geometry(fake, {"sector": 4096, "size": len(data)})
check("src verify_geometry: отказ стирания -> False", not ok, why)

fake = FakeNor(data, fail_program=True)
ok, why = C.verify_geometry(fake, {"sector": 4096, "size": len(data)})
check("src verify_geometry: не смог вернуть данные -> False и об этом сказано",
      not ok and "данные" in why, why)

fake = FakeNor(data)
check("src verify_geometry: без известной ёмкости проверка пропускается",
      C.verify_geometry(fake, {"sector": 4096, "size": 0})[0])

# --- 6. Пульт: имена файлов, таймауты, JSON-режим ----------------------------
section("пульт: файлы для ноутбука, таймауты ssh, машинный вывод")

from pcmcia_kit import config as KCFG                               # noqa: E402

missing = [name for name in KCFG.REMOTE_FILES
           if not os.path.exists(os.path.join(KIT, name))]
check("REMOTE_FILES: все перечисленные файлы существуют", not missing,
      str(missing))
check("REMOTE_FILES: planb.py уезжает на ноутбук (его запускает run_remote.py)",
      "planb.py" in KCFG.REMOTE_FILES)

from pcmcia_kit import transport as KTR                             # noqa: E402

calls = {}


class _FakeProc:
    returncode = 0
    stdout = "ok\n"
    stderr = ""


def _fake_run(argv, **kw):
    calls.update(kw)
    return _FakeProc()


real_run = KTR.subprocess.run
KTR.subprocess.run = _fake_run
try:
    target = KTR.Target("example.invalid")
    target.run("echo hi", timeout=42)
finally:
    KTR.subprocess.run = real_run
check("transport.run действительно передаёт timeout в subprocess",
      calls.get("timeout") == 42, str(calls.get("timeout")))

# JSON-режим: в stdout не должно быть человеческого текста
import subprocess                                                   # noqa: E402
res = subprocess.run([sys.executable, os.path.join(KIT, "pcmcia_ctl.py"),
                      "info", "--json"], capture_output=True, text=True,
                     timeout=120)
# Режим --json по замыслу набора печатает и отчёт для человека, и строку
# "JSON {...}". Ломаться он не должен только в одном случае - когда работать
# не с чем: тогда без этой проверки в stdout была одна проза про sudo, и
# разбирать было нечего.
json_lines = [ln for ln in res.stdout.splitlines() if ln.startswith("JSON ")]
check("pcmcia_ctl --json: строка JSON есть даже когда нет прав/карты",
      bool(json_lines), res.stdout.strip()[:120])

# --- 7. Мелочи, которые легко превращаются в зависание ------------------------
section("циклы и границы")

check("sector_table считает по ёмкости КАРТЫ (4 МиБ пара, а не 2 МиБ чип)",
      len(C.sector_table({"sector": 64 << 10, "size": 2 << 20}, 4 << 20)) == 64)


class TinyBus(NM.load_direct(KIT).NorBus):
    """Шина, у которой окно не двигается: так выглядит «адрес вне карты»."""

    def ensure(self, card_off, n=1):
        if self._len == 0:
            self._base, self._len, self._off = 0, 1, 0
            self._m = bytearray(b"\xff" * 8)


card, bus, flash = NM.make(kit=KIT, chips=1, chip_name="Am29F016")
tiny = TinyBus(card, card.exca, {"start": card.pci_base,
                                 "size": card.win_size}, span=4096, limit=1 << 20)
try:
    tiny.rd_block(0, 4)
    hung = False
except Exception as exc:                                  # noqa: BLE001
    hung = "за пределами карты" in str(exc)
check("rd_block: оконная нестыковка даёт ошибку, а не бесконечный цикл", hung)

# --- итог ---------------------------------------------------------------------
print("\n" + "=" * 70)
print("ИТОГО: прошло %d, провалено %d" % (len(PASS), len(FAIL)))
if FAIL:
    for name, detail in FAIL:
        print("  FAIL: " + name + ("   " + str(detail) if detail else ""))
    sys.exit(1)
print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
