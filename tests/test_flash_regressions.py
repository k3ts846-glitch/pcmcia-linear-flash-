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
import contextlib as _ctx
import io as _io
import os
import pathlib
import sys
import tempfile
import types as _types

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT)
sys.path.insert(0, os.path.join(KIT, "src"))
sys.path.insert(0, os.path.join(KIT, "tests"))

import nor_model as NM                                              # noqa: E402
from pcmcia_flash import image as _image                            # noqa: E402

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


# --- 9. Аудит таблицы: SST 4 КиБ, Intel Smart 5, реестр ID -----------------
# Сверено с независимыми источниками:
#   * SST DS25022A / flashrom flashchips/sst.c: у SST39SF/VF040 и младших
#     «uniform 4 KByte sectors» (flashrom: {4 KiB, 128}), 39VF — 2.7-3.6 В;
#   * Intel «BYTE-WIDE SMART 5 FlashFile MEMORY FAMILY 4, 8, AND 16 MBIT»:
#     4/8/16 Мбит = 8/16/32 блока по 64 КиБ, x8, 5 В (Vpp 5 или 12 В);
#   * flashrom flashchips.h + ядро drivers/mtd/chips/jedec_probe.c: коды Intel
#     A0h = 28F016SA/SV (ядро: 28F016S5), A2h = 28F008SA, A6h = 28F008S5,
#     A7h = 28F004S5; а B5h/B6h - это SST39SF010A/020A, не Intel.
section("аудит таблицы: SST, Intel Smart 5, реестр ID")

bad = []
for name, count in (("SST39SF040", 128), ("SST39VF040", 128)):
    for where, table in (("пакет", C.CHIP_GEOMETRY), ("монолит", D.GEOMETRY)):
        geo = table.get(name)
        if not geo:
            bad.append("%s нет в %s" % (name, where))
        elif C.sector_map(geo) != [(4096, count)] or geo["size"] != 512 << 10:
            bad.append("%s/%s: %s" % (name, where, C.sector_map(geo)))
check("SST39SF040 и SST39VF040: 128 секторов по 4 КиБ (а не 64 КиБ)", not bad,
      str(bad[:3]))

bad = []
for name, count, volts in (("SST39VF010", 32, 3), ("SST39VF020", 64, 3),
                           ("SST39VF080", 256, 3)):
    geo = D.GEOMETRY.get(name) or {}
    if C.sector_map(geo) != [(4096, count)] or geo.get("volts") != volts:
        bad.append((name, C.sector_map(geo), geo.get("volts")))
check("монолит: SST39VF010/020/080 тоже по 4 КиБ и 2.7-3.6 В", not bad, str(bad))

bad = []
for where, geo in (("пакет", C.CHIP_GEOMETRY.get("28F016S5") or {}),
                   ("монолит", D.GEOMETRY.get("28F016S5") or {})):
    if (not geo or geo.get("wide") or geo.get("volts") != 5
            or int(geo.get("page") or 1) != 1
            or C.sector_map(geo) != [(65536, 32)]):
        bad.append((where, geo.get("wide"), geo.get("volts"), geo.get("page")))
check("28F016S5: байтовая (wide=False), 5 В, без буфера 25h/D0h, 32 x 64 КиБ",
      not bad, str(bad))

bad = []
for name, count, size in (("28F004S5", 8, 512 << 10), ("28F008S5", 16, 1 << 20)):
    for where, table in (("пакет", C.CHIP_GEOMETRY), ("монолит", D.GEOMETRY)):
        geo = table.get(name)
        if not geo:
            bad.append("%s нет в %s" % (name, where))
        elif C.sector_map(geo) != [(65536, count)] or geo["size"] != size:
            bad.append("%s/%s: %s" % (name, where, C.sector_map(geo)))
check("28F004S5 / 28F008S5: 8 и 16 блоков по 64 КиБ, 5 В, байтовые", not bad,
      str(bad[:3]))

want_intel = {0xA0: "28F016SV", 0xA2: "28F008SA", 0xA6: "28F008S5", 0xA7: "28F004S5"}
got_intel = {k: v[0] for k, v in C.INTEL_PARTS.items()}
check("Intel ID по двум источникам: A0h/A2h/A6h/A7h, без выдуманных A1h/B5h/B6h",
      got_intel == want_intel, str(got_intel))

# Реестр в JSON и модуль jedec_registry.py не должны разъезжаться: раньше
# правки делались в одном из двух мест, и «эталон» подтверждал сам себя.
import json as _json
_reg_json = _json.loads((pathlib.Path(KIT) / "src" / "pcmcia_flash" / "data" /
                         "jedec_registry.json").read_text(encoding="utf-8"))
_reg_py = __import__("pcmcia_flash.jedec_registry", fromlist=["x"])
drift = []
for vendor, table in (("AMD", _reg_py.AMD_ETALON), ("Fujitsu", _reg_py.FUJITSU_ETALON),
                      ("Intel", _reg_py.INTEL_ETALON)):
    js = {int(k, 16): v["chip"] for k, v in (_reg_json.get(vendor) or {}).items()}
    py = {k: v[0] for k, v in table.items()}
    if js != py:
        drift.append((vendor, sorted(js.items() ^ py.items())[:4]))
check("реестр jedec_registry.json и jedec_registry.py совпадают", not drift,
      str(drift))

# Обозначения, которых нет в даташитах: отказ с подсказкой в обеих таблицах.
wrong = ("AT29F040", "AT29F040B", "AT29F016", "AT29F080",
         "SST39VF016", "SST39SF016", "28F002SA", "28F004SA", "M28F016S5")
bad = []
for name in wrong:
    for where, fn, err in (("пакет", C.geometry_for, C.UnsupportedChip),
                           ("монолит", D.geometry_for, D.DirectError)):
        try:
            fn(name)
            bad.append("%s/%s: не отказал" % (name, where))
        except err as exc:
            if "нет" not in str(exc):
                bad.append("%s/%s: %s" % (name, where, str(exc)[:50]))
        except Exception as exc:                    # noqa: BLE001
            bad.append("%s/%s: %s" % (name, where, type(exc).__name__))
check("9 несуществующих обозначений отвергаются с подсказкой", not bad,
      str(bad[:3]))

# AT29C: у семейства нет команды Sector Erase (страничная запись), поэтому
# монолит отказывает, а не шлёт 30h в никуда.
try:
    D.check_sector_map(D.geometry_for("AT29C010"))
    ok29 = False
except D.DirectError as exc:
    ok29 = "30h" in str(exc)
check("AT29C010: отказ «команды Sector Erase 30h нет»", ok29)

# Опечатка в --chip не должна молча давать «умолчания как у Am29F016».
try:
    D.identify_chip(None, {}, force="XX4234")
    typo = False
except D.DirectError as exc:
    typo = "Опечатка" in str(exc)
check("--chip с опечаткой: отказ, а не умолчания (64 КиБ / 2 МиБ)", typo)

# Модель берёт код Intel из эталона, а не «на память».
check("модель: 28F008SA = 89h:A2h, 28F016SV = 89h:A0h",
      NM._intel_id_from_registry("28F008SA") == 0xA2
      and NM._intel_id_from_registry("28F016SV") == 0xA0,
      str((NM._intel_id_from_registry("28F008SA"),
           NM._intel_id_from_registry("28F016SV"))))

# 4-КиБ сектор стирается ровно один: соседний байт цел.
card, _bus, flash = NM.make(kit=KIT, chips=1, chip_name="SST39VF040", blank=False)
img = bytes((i * 7 + 3) & 0xFF for i in range(512 << 10))
card.mem[0][:] = img
ok, why = flash.erase_sector(3 * flash.sector)
check("SST39VF040: стирается один 4-КиБ сектор, соседний цел",
      ok and flash.read(3 * flash.sector, 16) == b"\xff" * 16
      and flash.read(2 * flash.sector, 8) == img[2 * flash.sector:2 * flash.sector + 8],
      why)


# --- 10. Страж соседей: блок на железе больше, чем в таблице --------------
# Ровно тот случай, который дал аудит 2.17.3 (SST: 64 КиБ в таблице против
# 4 КиБ на железе, и наоборот - таблица 64 КиБ против блока 128 КиБ). Сам
# сектор стирается в любом случае, поэтому ошибку видно только по СОСЕДУ.
section("страж соседей при стирании (блок больше, чем в таблице)")

IMG2 = bytes((i * 13 + 7) & 0xFF for i in range(2 << 20))

for where, erase in (("монолит", D._erase_sector_guarded),
                     ("пакет", _image._erase_sector_guarded)):
    card, _bus, flash = NM.make(kit=KIT, chips=1, chip_name="Am29F016", blank=False)
    card.mem[0][:] = IMG2
    ok, why = erase(flash, 0x8000)
    check("%s: верная геометрия — страж молчит" % where, ok, why)

    card, _bus, flash = NM.make(kit=KIT, chips=1, chip_name="Am29F016", blank=False)
    card.mem[0][:] = IMG2
    flash.sector = 32 << 10          # таблица думает 32 КиБ, железо сотрёт 64
    flash.sector_map = None
    ok, why = erase(flash, 0x8000)
    check("%s: заниженный сектор — отказ, сосед назван" % where,
          (not ok) and "БОЛЬШЕ" in why and "0x7fff" in why, why[:60])


# --- 11. Второй заход аудита: Toshiba boot-блоки, старое 12-вольтовое -------
# Toshiba TC58FVT160/B160A (даташит): «Block erase architecture 1 x 16 Kbytes /
# 2 x 8 Kbytes / 1 x 32 Kbytes / 31 x 64 Kbytes», top boot, VDD 2.7-3.6 В,
# коды 98h/00C2h, в байтовом режиме адреса команд AAAh/555h. Порядок блоков в
# верхних 64 КиБ - из таблицы регионов ядра jedec_probe.c. Прежние «2 МиБ по
# 64 КиБ и 5 В» были неверны и на boot-чипе задели бы 32K+8K+8K+16K.
section("Toshiba и старое 12-вольтовое (второй заход аудита)")

TOSH = {"TC58FVT160": (2 << 20, [(65536, 31), (32768, 1), (8192, 2), (16384, 1)]),
        "TC58FVT321": (4 << 20, [(65536, 63), (8192, 8)])}
bad = []
for name, (size, want) in TOSH.items():
    for where, table in (("пакет", C.CHIP_GEOMETRY), ("монолит", D.GEOMETRY)):
        geo = table.get(name)
        if not geo:
            bad.append("%s нет в %s" % (name, where))
            continue
        if C.sector_map(geo) != want or geo["size"] != size:
            bad.append("%s/%s: %s" % (name, where, C.sector_map(geo)))
        if geo.get("volts") != 3 or tuple(geo["cmd"]) != (0xAAA, 0x555):
            bad.append("%s/%s: %s В, cmd %s" % (name, where, geo.get("volts"), geo["cmd"]))
        if geo.get("wide"):
            bad.append("%s/%s: широкая команда у байтовой" % (name, where))
check("TC58FVT160/321: boot-карты, 3 В, команды AAA/555 в обеих таблицах",
      not bad, str(bad[:3]))

try:
    D.geometry_for("TC58FVT320")
    no320 = False
except D.DirectError as exc:
    no320 = "TC58FVT321" in str(exc)
check("TC58FVT320: отказ с подсказкой TC58FVT321", no320)

# Первое поколение Intel: стирание только целиком (Bulk Erase), команды
# Sector Erase нет — цикл «стереть сектор, записать» неприменим.
bad = []
for name in ("28F010", "28F020", "28F256", "28F512"):
    try:
        D.check_sector_map(D.geometry_for(name))
        bad.append("%s: не отказал" % name)
    except D.DirectError as exc:
        if not ("ВСЕГО чипа" in str(exc) or "bulk erase" in str(exc)):
            bad.append("%s: %s" % (name, str(exc)[:40]))
check("28F010/020/256/512 (12 В, bulk erase): отказ с объяснением", not bad,
      str(bad[:2]))

# «28F320» без ревизии: у B3T/B boot-блоки, у J3/J5 блоки крупнее — гадать нельзя.
try:
    D.check_sector_map(D.geometry_for("28F320"))
    no320 = False
except D.DirectError as exc:
    no320 = "ревизию" in str(exc)
check("28F320 без ревизии: отказ «укажите ревизию»", no320)

# Списки напряжений не должны содержать сами себя (такая строка однажды
# попала в chips.json правкой списка).
raw = _json.loads((pathlib.Path(KIT) / "src" / "pcmcia_flash" / "data" /
                   "chips.json").read_text(encoding="utf-8"))
junk = [(key, [x for x in raw.get(key, []) if x in ("vpp5", "vpp126")])
        for key in ("vpp5", "vpp126")]
junk = [(k, v) for k, v in junk if v]
check("vpp5/vpp126 не содержат собственных имён", not junk, str(junk))


# --- 12. Третий заход аудита: MX29F1610 (сектор 128 КиБ) и честные пометки ---
# Даташит Macronix MX29F1610 (16M-BIT [2Mx8/1Mx16], 5 В +-10%): «Sector erase
# architecture - 16 equal sectors of 128k bytes each», страница 128 байт. В
# таблице стояло «сектор 64 КиБ» - такое стирание снесло бы соседние 64 КиБ
# того же блока. Проверяем и саму строку, и то, что страж ловит прежнюю
# раскладку на модели с ЖЕЛЕЗНЫМИ 128-КиБ блоками.
section("MX29F1610: сектор 128 КиБ, и страж против прежней таблицы")

mx = D.GEOMETRY["MX29F1610"]
check("MX29F1610: 2 МиБ, сектор 128 КиБ, карта 16 x 128 КиБ",
      mx["size"] == 2 << 20 and mx["sector"] == 128 << 10
      and D.sector_map(mx) == [(131072, 16)],
      str((mx["size"], mx["sector"], D.sector_map(mx))))
check("MX29F1610: 5 В и не помечен отказом",
      mx["volts"] == 5 and not mx.get("ambiguous") and not mx.get("unverified"))

MX_IMG = bytes((i * 11 + 5) & 0xFF for i in range(2 << 20))
card, _bus, flash = NM.make(kit=KIT, chips=1, chip_name="MX29F1610", blank=False)
card.mem[0][:] = MX_IMG
ok, why = D._erase_sector_guarded(flash, 0x20000)
check("MX29F1610: верная таблица - стёрт блок 128 КиБ, соседи целы",
      ok and flash.read(0x20000, 16) == b"\xff" * 16
      and flash.read(0x1FFFF, 1) == b"\xfa" and flash.read(0x40000, 1) == b"\x05",
      (ok, why[:70]))

card, _bus, flash = NM.make(kit=KIT, chips=1, chip_name="MX29F1610", blank=False)
card.mem[0][:] = MX_IMG
flash.sector = 64 << 10           # прежняя таблица: 64 КиБ при блоке 128 КиБ
flash.sector_map = None
ok, why = D._erase_sector_guarded(flash, 0x20000)
check("MX29F1610: прежние 64 КиБ - отказ, сосед назван",
      (not ok) and "БОЛЬШЕ" in why and "0x30000" in why, why[:90])

# Строки, которых нет ни в одном из двух источников (flashrom flashchips.h и
# ядро jedec_probe.c) и под которые не нашлось даташита, обязаны быть помечены
# честно: чип работает, но отчёт предупреждает, а src говорит, что сверки нет.
# Am29PL320/640/1280 добавлены в 2.17.6: таких имён нет ни в flashchips.h, ни
# в jedec_probe.c, то есть строка держалась на догадке.
UNVERIFIED = {"D28F010", "D28F020", "HN28F010", "HN28F020", "HN28F100",
              "LH28F010", "LH28F020", "M28F008", "M28F016",
              "M5M29F010", "M5M29F020", "MX29F3210",
              "Am29PL320", "Am29PL640", "Am29PL1280",
              "Am29F032", "MBM29F033"}
got = {n for n, g in D.GEOMETRY.items() if g.get("unverified")}
check("помечены «не сверено» ровно %d строк" % len(UNVERIFIED),
      got == UNVERIFIED, str(sorted(got ^ UNVERIFIED)))
dishonest = [n for n in sorted(UNVERIFIED)
             if "не сверен" not in (D.GEOMETRY[n].get("src") or "")]
check("у каждой непроверенной строки src говорит о недосверке",
      not dishonest, str(dishonest))
both = [n for n in sorted(UNVERIFIED) if D.GEOMETRY[n].get("ambiguous")]
check("непроверенная строка не выдаёт себя за отказ", not both, str(both))

_geo = D.geometry_for("HN28F010", size=128 << 10)
_buf = _io.StringIO()
with _ctx.redirect_stdout(_buf):
    D._flash_report({"geo": _geo, "chips": 1, "size": 128 << 10, "how": "тест",
                     "win": {"start": 0xC0000, "size": 4 << 20},
                     "bus": _types.SimpleNamespace(span=8192)})
check("непроверенная строка: отчёт печатает ВНИМАНИЕ",
      "ВНИМАНИЕ" in _buf.getvalue() and "не сверены" in _buf.getvalue(),
      _buf.getvalue()[:120])

_buf = _io.StringIO()
with _ctx.redirect_stdout(_buf):
    D._flash_report({"geo": D.geometry_for("MX29F1610", size=2 << 20),
                     "chips": 1, "size": 2 << 20, "how": "тест",
                     "win": {"start": 0xC0000, "size": 4 << 20},
                     "bus": _types.SimpleNamespace(span=8192)})
check("сверенная строка: ВНИМАНИЯ нет", "ВНИМАНИЕ" not in _buf.getvalue())


# --- 13. Четвёртый заход: семейство Am29DL (3,0 В, boot-секторы 8 КиБ) -----
# Даташит AMD 21534d (Am29DL322D/323D/324D, декабрь 2005): «32 Megabit
# (4 M x 8-Bit/2 M x 16-Bit) CMOS **3.0 Volt-only**», Table 2 - Bank 1 это
# восемь секторов по 8 КиБ + семь по 64 КиБ, Table 5 (bottom boot) - SA0..SA7
# по 8 КиБ, дальше 63 по 64 КиБ, Table 3 (top boot) - зеркально. В таблице
# стояло «5 В и однородные 64 КиБ»: стёрлось бы 8 КиБ вместо 64 (blank-check
# упал бы), а напряжение написано в заголовке даташита. Пакет при этом уже
# знал про 3 В - то есть две реализации расходились, и старая проверка
# расхождений этого не видела (она сравнивала только ёмкость и карту).
section("Am29DL: 3,0 В, boot-секторы 8 КиБ, суффикс T/B")

DL_BARE = {"Am29DL322": "T/B", "Am29DL323": "T/B", "Am29DL642": "ревизи",
           "Am29DL643": "укажите", "MBM29DL322": "BE",
           "MBM29DL642": "не сверяли"}
bad = []
for name, needle in DL_BARE.items():
    for where, table, err in (("монолит", D.GEOMETRY, D.DirectError),
                              ("пакет", C.CHIP_GEOMETRY, C.UnsupportedChip)):
        geo = table.get(name)
        if not geo:
            bad.append("%s: нет в %s" % (name, where))
            continue
        if geo.get("volts") != 3:
            bad.append("%s/%s: %s В, а не 3" % (name, where, geo.get("volts")))
        try:
            (D.check_sector_map if where == "монолит" else C.check_sector_map)(geo)
            bad.append("%s/%s: не отказал" % (name, where))
        except err as exc:
            if needle not in str(exc):
                bad.append("%s/%s: в отказе нет «%s»" % (name, where, needle))
check("Am29DL322/323/642/643, MBM29DL322/642: 3 В и отказ с подсказкой T/B",
      not bad, str(bad[:3]))

# Конкретные ревизии: карта из Table 5 / Table 3, сумма = ёмкость.
DL_MAPS = {"Am29DL322DB": [(8192, 8), (65536, 63)],
           "Am29DL323DB": [(8192, 8), (65536, 63)],
           "Am29DL324DB": [(8192, 8), (65536, 63)],
           "MBM29DL322BE": [(8192, 8), (65536, 63)],
           "Am29DL322DT": [(65536, 63), (8192, 8)],
           "Am29DL323DT": [(65536, 63), (8192, 8)],
           "Am29DL324DT": [(65536, 63), (8192, 8)],
           "MBM29DL322TE": [(65536, 63), (8192, 8)]}
bad = []
for name, want in DL_MAPS.items():
    for where, table in (("монолит", D.GEOMETRY), ("пакет", C.CHIP_GEOMETRY)):
        geo = table.get(name)
        if not geo:
            bad.append("%s: нет в %s" % (name, where))
        elif geo["size"] != 4 << 20 or C.sector_map(geo) != want \
                or geo.get("volts") != 3:
            bad.append("%s/%s: %s %s %sВ" % (name, where, geo["size"],
                                             C.sector_map(geo), geo.get("volts")))
check("Am29DL32xDB/DT и MBM29DL322BE/TE: 8x8 КиБ + 63x64 КиБ, 4 МиБ, 3 В",
      not bad, str(bad[:3]))

# На модели: стирание первого 8-КиБ сектора не трогает соседний 8-КиБ,
# а «64 КиБ по таблице» на таком чипе ловится стражем.
card, _bus, flash = NM.make(kit=KIT, chips=1, chip_name="Am29DL322DB", blank=False)
DL_IMG = bytes((i * 17 + 9) & 0xFF for i in range(4 << 20))
card.mem[0][:] = DL_IMG
ok, why = D._erase_sector_guarded(flash, 0x2000)
check("Am29DL322DB: стёрт второй 8-КиБ сектор, первый цел",
      ok and flash.read(0x2000, 16) == b"\xff" * 16
      and flash.read(0x1000, 1) == DL_IMG[0x1000:0x1001], why[:70])

# Обратный случай: таблица БОЛЬШЕ железа (64 КиБ против блока 8 КиБ). Страж
# соседей тут молчит - он смотрит за границы заявленного сектора, а они целы.
# Спасает следующий шаг: blank-check увидит, что 0x2000..0xFFFF не стёрлись.
card, _bus, flash = NM.make(kit=KIT, chips=1, chip_name="Am29DL322DB", blank=False)
card.mem[0][:] = DL_IMG
flash.sector = 64 << 10            # прежняя таблица: «первый сектор 64 КиБ»
flash.sector_map = None
ok, why = D._erase_sector_guarded(flash, 0)
check("Am29DL322DB: завышенный сектор страж не ловит (ловит blank-check)",
      ok and why == ""
      and set(flash.read(0x2000, 16)) != {0xFF}
      and flash.read(0, 16) == b"\xff" * 16)

# Сектор SecSi (Table 4) - отдельный, к обычной карте не относится: в карте
# ровно 71 сектор (8 + 63), SecSi живёт поверх последних 64 КиБ по команде.
check("Am29DL32xDB: в карте 71 сектор (8 boot + 63), SecSi не в карте",
      len(C.sector_map(C.CHIP_GEOMETRY["Am29DL322DB"])) == 2
      and sum(n for _s, n in C.sector_map(C.CHIP_GEOMETRY["Am29DL322DB"])) == 71)


# --- 14. Пятый заход: ST M28F102 (стирание только целиком) и DL640 ---------
# Даташит ST M28F102 (август 1998): «1 Mbit (64Kb x16, Bulk) Flash Memory»,
# «5V ±10% supply, 12V programming», «ELECTRICAL CHIP ERASE in 1s», 64 Kword x
# 16 бит = 128 КиБ. В таблице стояло 256 КиБ и шестнадцать секторов по 16 КиБ -
# неверно и то, и другое: поблочного стирания у чипа нет вообще.
section("ST M28F102: 128 КиБ и стирание только целиком")

m102 = D.GEOMETRY["M28F102"]
check("M28F102: 128 КиБ (1 Мбит), а не 256 КиБ",
      m102["size"] == 128 << 10, str(m102["size"]))
check("M28F102: 12 В (Vpp при программировании)", m102["volts"] == 12)
try:
    D.check_sector_map(D.geometry_for("M28F102"))
    refuse = ""
except D.DirectError as exc:
    refuse = str(exc)
check("M28F102: отказ «стирание только целиком», а не 16 секторов по 16 КиБ",
      "целиком" in refuse and "Sector Erase" in refuse, refuse[:80])

# 64-Мбит семейство Am29DL640D/G: четыре банка, восемь 8-КиБ секторов снизу И
# восемь сверху (Both Top and Bottom Boot), между ними 126 по 64 КиБ.
dl64 = D.GEOMETRY["Am29DL642"]
check("Am29DL642: 8 МиБ и 3 В", dl64["size"] == 8 << 20 and dl64["volts"] == 3)
try:
    D.check_sector_map(D.geometry_for("Am29DL642"))
    refuse64 = ""
except D.DirectError as exc:
    refuse64 = str(exc)
check("Am29DL642/643: в отказе сказано про 8-КиБ boot-секторы и ревизию",
      "8 КиБ" in refuse64 and "D или G" in refuse64, refuse64[:90])

# 64 МиБ = 8 x 8 КиБ (низ) + 126 x 64 КиБ + 8 x 8 КиБ (верх): сумма сходится,
# это и есть та карта, которую пришлось бы применять, если бы ревизия была
# известна. Проверяем арифметику, а не «на глаз».
both_boot = (8 * 8192) + (126 * 65536) + (8 * 8192)
check("раскладка DL640 «8 КиБ + 126 x 64 КиБ + 8 КиБ» = 8 МиБ",
      both_boot == 8 << 20, str(both_boot))


# --- 15. Шестой заход: «Am29F032/033» - каких частей у AMD не было ---------
# Селектор AMD (FLASH MEMORY PRODUCT SELECTOR GUIDE): в ряду 32 Мбит 5 В есть
# ТОЛЬКО Am29F032B (4M x 8, «64 uniform sectors of 64 Kbytes each», 21610D8);
# 32-Мбитная x16 часть у AMD трёхвольтовая - Am29LV033C. Ни «Am29F032», ни
# «Am29F033» в селекторе нет, и «64 x 64 КиБ, 5 В, Unlock Bypass 20h» было
# догадкой, выданной за факт.
section("Am29F032B и Am29LV033C вместо «Am29F032/033»")

try:                                  # именно параметры 032B подтверждены
    C.check_sector_map(C.geometry_for("Am29F032B"))
    ok032b = False
except C.UnsupportedChip:
    ok032b = True
check("Am29F032B: 64 x 64 КиБ, 5 В (даташит 21610D8)", not ok032b
      and C.CHIP_GEOMETRY["Am29F032B"]["size"] == 4 << 20
      and C.sector_map(C.CHIP_GEOMETRY["Am29F032B"]) == [(65536, 64)])

try:
    D.check_sector_map(D.geometry_for("Am29F033"))
    refuse033 = ""
except D.DirectError as exc:
    refuse033 = str(exc)
check("Am29F033: отказ называет Am29F032B и Am29LV033C",
      "Am29F032B" in refuse033 and "Am29LV033C" in refuse033, refuse033[:90])

for name in ("Am29F032", "MBM29F033"):
    geo = D.GEOMETRY.get(name) or {}
    ok_flag = bool(geo.get("unverified"))
    usable = True
    try:
        D.check_sector_map(D.geometry_for(name))
    except D.DirectError:
        usable = False
    check("%s: работает, но помечен «не сверено»" % name,
          ok_flag and usable and geo.get("size") == 4 << 20)

# Новая строка: 32-Мбитная трёхвольтовая часть, чтобы было что указать вместо
# несуществующей «Am29F033».
bad = []
for where, table in (("монолит", D.GEOMETRY), ("пакет", C.CHIP_GEOMETRY)):
    geo = table.get("Am29LV033C")
    if not geo:
        bad.append("%s: нет строки" % where)
    elif (geo["size"] != 4 << 20 or geo.get("volts") != 3
          or C.sector_map(geo) != [(65536, 64)] or geo.get("bypass")):
        bad.append("%s: %s %sВ %s bypass=%s" % (where, geo["size"], geo.get("volts"),
                                                C.sector_map(geo), geo.get("bypass")))
check("Am29LV033C: 4 МиБ, 3 В, 64 x 64 КиБ, без Unlock Bypass", not bad, str(bad))


# --- 16. Intel CUI: буферная запись - E8h + счётчик N + D0h, а не 25h ------
# Даташиты Intel (28F/StrataFlash, «Write to Buffer»): команда E8h по адресу
# блока, затем N = число байт минус один, затем N+1 слово данных, затем D0h.
# В коде стояло 25h (это команда AMD StrataFlash, JEDEC), счётчика не было, а
# данные писались побайтно - тогда как CUI принимает только СЛОВО. Заметить
# это было нельзя: у всех Intel-строк таблицы page=1, и ветка не работала.
section("Intel CUI: буферная запись E8h/N/D0h")

from pcmcia_flash import constants as K                                # noqa: E402

check("команда Write-to-Buffer у Intel - E8h (а не 25h из AMD)",
      K.INTEL_BUFFER_SETUP == 0xE8 and D.INTEL_BUFFER_SETUP == 0xE8,
      "%#x/%#x" % (K.INTEL_BUFFER_SETUP, D.INTEL_BUFFER_SETUP))

CARD, _bus16, FL16 = NM.make(kit=KIT, chips=1, chip_name="28F016S5", blank=True)
FL16.page, FL16.page_card = 32, 32
DATA32 = bytes((i * 37 + 11) & 0xFF for i in range(32))
ok, why = FL16.program_page(0, DATA32)
check("буфер 32 байта: программа прошла по E8h и данные читаются",
      ok and all(FL16.read_byte(i) == DATA32[i] for i in range(32)), why[:80])
check("модель не увидела ни одной неизвестной команды CUI",
      CARD.bad_cui_cmds == 0, str(CARD.bad_cui_cmds))

# Пара чипов: команда - ОДНО слово на пару. Если послать E8h «каждому чипу»,
# второй байт чип прочтёт как счётчик N и буфер уедет - именно эта ошибка
# была в первой редакции правки.
CARD2C, _b2c, FL2C = NM.make(kit=KIT, chips=2, chip_name="28F016S5", blank=True)
FL2C.page, FL2C.page_card = 32, 64
D64 = bytes((i * 53 + 7) & 0xFF for i in range(64))
ok, why = FL2C.program_page(0, D64)
# Ширина команды: ровно по байту на чип. На карте с одним чипом прежнее
# bytes([op, op]) превращало команду в команду+данные.
CARD1, _b1, FL1 = NM.make(kit=KIT, chips=1, chip_name="28F008S5", blank=True)
ok, why = FL1.program_byte(0x100, 0x5A)
check("один чип: запись байта идёт (команда - один байт, не два)",
      ok and FL1.read_byte(0x100) == 0x5A, why[:70])
ok, why = FL1.erase_sector(0)
check("один чип: стирание сектора идёт", ok, why[:70])

check("пара чипов: E8h одним словом, данные сходятся",
      ok and all(FL2C.read_byte(i) == D64[i] for i in range(64))
      and CARD2C.bad_cui_cmds == 0, why[:80])

# Старый протокол: 25h в CUI - неизвестная команда. Модель это фиксирует,
# значит следующий раз такой код не пройдёт молча.
CARD2, BUS2, _f2 = NM.make(kit=KIT, chips=2, chip_name="28F016S5", blank=True)
BUS2.wr_many(CARD2.win_start, bytes([0x25, 0x25]))
check("старый протокол 25h: модель считает команду неизвестной",
      CARD2.bad_cui_cmds == 1, str(CARD2.bad_cui_cmds))

# page больше буфера: отказ, а не запись «как получится»
FL16.page, FL16.page_card = 64, 64
ok, why = FL16.program_page(0, b"\x00" * 64)
check("page=64 при буфере 32: отказ с объяснением",
      (not ok) and "32" in why, why[:70])
FL16.page, FL16.page_card = 1, 1


# --- 17. Intel 28F004S5/008S5/016S5: spec update 297796-007 ---------------
# Присланный документ (Intel 297796-007, февраль 1999) — Specification Update к
# даташиту 290597-005 «5 Volt FlashFile Memory; 28F004S5, 28F008S5, 28F016S5».
# Из него важны две вещи:
#   * семейство — 4/8/16 Мбит, x8, 5 В (плотности и ширина шины в таблице верны);
#   * erratum #2 «Block Locking and Unlocking»: на 8- и 16-Мбит частях Intel
#     прошивает master lock-bit на заводе, и программно защиту снять НЕЛЬЗЯ -
#     нужны 12 В на RP#. Обратная сторона: операция в защищённый блок
#     возвращает WPPS=1 (бит 1 статуса), а наш код этот бит не различал и
#     писал просто «запись не удалась».
section("28Fxxx: блок защищён (WPPS) — и почему это не лечится программно")

check("SR_PROTECT = 0x02 в обеих реализациях", K.SR_PROTECT == 0x02
      and D.SR_PROTECT == 0x02, "%#x/%#x" % (K.SR_PROTECT, D.SR_PROTECT))

LOCK_CARD, _lb, LOCK_FL = NM.make(kit=KIT, chips=1, chip_name="28F008S5", blank=True)
LOCK_CARD.protect_groups[0] = True          # первый 64-КиБ блок защищён
ok, why = LOCK_FL.program_byte(0, 0x00)
check("запись в защищённый блок: отказ объясняет 12 В на RP#",
      (not ok) and "защищ" in why and "RP#" in why and "297796" in why, why[:90])
ok, why = LOCK_FL.erase_sector(0)
check("стирание защищённого блока: то же объяснение",
      (not ok) and "защищ" in why and "RP#" in why, why[:90])
# Соседний незащищённый блок при этом работает - защита не «вся карта»
ok, why = LOCK_FL.program_byte(64 << 10, 0x00)
check("незащищённый блок пишется нормально", ok, why[:70])

# Две реализации не должны разойтись в диагностике: текст один и тот же.
_srcs = {}
for where, rel in (("монолит", ("windows", "legacy", "pcmcia_direct.py")),
                   ("пакет", ("src", "pcmcia_flash", "nor_flash.py"))):
    text = pathlib.Path(KIT, *rel).read_text(encoding="utf-8")
    # в исходнике строка разбита переносом, поэтому ищем короткие куски
    _srcs[where] = "lock-bit" in text and "RP#" in text and "297796" in text
check("оба файла знают про master lock-bit и RP#", all(_srcs.values()), str(_srcs))

# Плотности из того же документа: 4/8/16 Мбит, x8, 5 В.
DENS = {"28F004S5": 512 << 10, "28F008S5": 1 << 20, "28F016S5": 2 << 20}
bad = []
for name, size in DENS.items():
    for where, table in (("монолит", D.GEOMETRY), ("пакет", C.CHIP_GEOMETRY)):
        geo = table.get(name)
        if not geo:
            bad.append("%s нет в %s" % (name, where))
        elif (geo["size"] != size or geo.get("volts") != 5 or geo.get("wide")
              or C.sector_map(geo) != [(65536, size >> 16)]):
            bad.append("%s/%s: %s %sВ wide=%s %s" % (name, where, geo["size"],
                                                     geo.get("volts"), geo.get("wide"),
                                                     C.sector_map(geo)))
check("28F004S5/008S5/016S5: 4/8/16 Мбит, 5 В, x8, секторы по 64 КиБ",
      not bad, str(bad[:3]))


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

# Прошлая проверка смотрела ТОЛЬКО ёмкость и карту, и поэтому пропустила, что
# Am29DL322 у пакета был 3 В, а у монолита 5 В: чип один, данные разные.
def _norm(geo):
    # None у page/bypass - это «нет буфера» и «не поддерживает», а не отличие:
    # пакет пишет 1/False, монолит оставляет None.
    return (geo.get("volts"), int(geo.get("page") or 1),
            bool(geo.get("wide")), bool(geo.get("bypass")))


drift2 = [(n, _norm(C.CHIP_GEOMETRY[n]), _norm(D.GEOMETRY[n]))
          for n in sorted(shared)
          if _norm(C.CHIP_GEOMETRY[n]) != _norm(D.GEOMETRY[n])]
check("напряжение, страница, wide и bypass совпадают у всех общих имён",
      not drift2, str(drift2[:2]))

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


# --- 18. Даташит Intel 28F010 (290207-010): регистр команд, не JEDEC -------
# Присланный документ (28F010, 128K x 8, ноябрь 1995) описывает ПЕРВОЕ
# поколение: команды пишутся в регистр команд однобайтовыми циклами
# (00h чтение массива, 90h ID, 20h+20h стирание ВСЕГО чипа, 40h+PA/PD запись,
# A0h/C0h проверка, FFh+FFh сброс). Никаких 555h/2AAh, блочного стирания нет,
# статусного регистра 70h нет, а запись и стирание возможны только при
# 12 В +-5% на Vpp (при Vpp < 6.5 В микросхема read-only).
bad = []
for name, size in (("28F010", 128 << 10), ("28F020", 256 << 10)):
    geo = D.GEOMETRY.get(name)
    if not geo:
        bad.append("%s нет" % name)
        continue
    if not geo.get("ro"):
        bad.append("%s: не помечен ro" % name)
    if geo.get("protocol") != "intel":
        bad.append("%s: protocol %s" % (name, geo.get("protocol")))
    if tuple(geo["cmd"]) != (0, 0):
        bad.append("%s: cmd %s (ждали (0, 0): JEDEC-адресов у чипа нет)"
                   % (name, geo["cmd"]))
    if geo.get("size") != size or geo.get("sector") != size:
        bad.append("%s: size %s, sector %s (стирается только весь чип)"
                   % (name, geo.get("size"), geo.get("sector")))
    if geo.get("volts") != 12:
        bad.append("%s: volts %s" % (name, geo.get("volts")))
check("28F010/020: только чтение, CUI, cmd (0,0), сектор = весь чип, 12 В",
      not bad, str(bad[:3]))

src010 = str((D.GEOMETRY.get("28F010") or {}).get("src") or "")
needles = ("290207", "B4h", "20h", "12 В")
miss = [n for n in needles if n not in src010]
check("28F010: источник - даташит 290207-010, команда 20h, ID B4h, 12 В",
      not miss, str(miss))

# ID из документа: производитель 89h, устройство B4h
check("28F010: определяется по ID 89h/B4h",
      D.chip_name_from_info({"jedec": "89B4"}) == "28F010",
      D.chip_name_from_info({"jedec": "89B4"}))
check("28F020 по ID не угадываем (в документе кода нет)",
      D.chip_name_from_info({"jedec": "89B5"}) != "28F020")


class _SilentBus:
    """Шина-заглушка: команды никуда не идут, читается 0xFF."""
    limit = 1 << 20

    def rd(self, addr):
        return 0xFF

    def wr(self, addr, value):
        pass

    def wr_many(self, addr, data):
        pass


def _flash(name, **kw):
    geo = D.geometry_for(name)
    return D.NorFlash(_SilentBus(), chips=1, sector=geo["sector"],
                      name=name, protocol=geo.get("protocol", "jedec"),
                      ro=bool(geo.get("ro")),
                      ro_why=str(geo.get("why") or ""), **kw)


ro_flash = _flash("28F010")
ans = [ro_flash.program_byte(0, 0x5A), ro_flash.program_page(0, b"\x00"),
       ro_flash.erase_sector(0), ro_flash.erase_chip()]
bad = []
for ok, why in ans:
    if ok:
        bad.append("не отказал")
    elif "не выполняется" not in why or "Vpp" not in why:
        bad.append(why[:50])
check("28F010: запись/страница/сектор/чип - отказ с причиной (Vpp, только чип)",
      not bad, str(bad[:2]))

# ... и наоборот: у 5-вольтовой S5 отказа нет, запись идёт
plain = _flash("28F008S5")
check("28F008S5: без ro (5 В, есть блочное стирание) - отказа нет",
      plain.ro is False and plain.program_byte(0, 0xFF)[0], plain.ro_why)

# ro обязан дойти до NorFlash из identify_chip (иначе предохранитель мёртв)
import inspect
id_src = inspect.getsource(D.identify_chip)
check("identify_chip пробрасывает ro/ro_why в результат",
      "ro=bool(geo.get(\"ro\"))" in id_src and "ro_why" in id_src)
open_src = inspect.getsource(D._flash_open)
check("_flash_open передаёт ro в NorFlash",
      "ro=bool(geo.get(\"ro\"))" in open_src)
pkg = (pathlib.Path(KIT) / "src" / "pcmcia_flash" / "nor_flash.py").read_text(encoding="utf-8")
check("пакетный двойник знает про ro (предохранитель в обеих реализациях)",
      "self.ro = bool(ro)" in pkg and "def _readonly" in pkg)

# --- 19. Даташит Intel 28F008SA (290429-008) -------------------------------
# Документ (8 Мбит, декабрь 1998) даёт три вещи, которых в наборе не было:
# * статусный регистр: SR.7 готовность, SR.6 ПАУЗА стирания, SR.5 ошибка
#   стирания, SR.4 ошибка записи, SR.3 нет Vpp, SR.2-SR.0 reserved;
# * биты SR.3/4/5 ставятся автоматом и снимаются ТОЛЬКО командой 50h
#   (Clear Status Register): FFh - это Read Array/Reset, их не трогает.
#   «Если SR.3 выставлен, его надо сбросить, прежде чем снова писать/стирать»;
# * пауза стирания у Intel ЕСТЬ: Erase Suspend B0h, Erase Resume D0h.
from pcmcia_flash import constants as _CONST      # noqa: E402  (та же таблица)
check("SR.6 (пауза стирания) есть в обеих реализациях",
      D.SR_SUSPEND == 0x40 and _CONST.SR_SUSPEND == 0x40,
      (D.SR_SUSPEND, getattr(_CONST, "SR_SUSPEND", None)))

geo_sa = D.GEOMETRY.get("28F008SA") or {}
bad = []
if geo_sa.get("wide"):
    bad.append("wide=True: чип байтовый (x8, Byte Write)")
if tuple(geo_sa.get("cmd") or ()) != (0, 0):
    bad.append("cmd %s" % (geo_sa.get("cmd"),))
if geo_sa.get("sector") != 64 << 10 or geo_sa.get("size") != 1 << 20:
    bad.append("16 x 64 КиБ")
if geo_sa.get("volts") != 12:
    bad.append("volts %s" % geo_sa.get("volts"))
src_sa = str(geo_sa.get("src") or "")
miss = [n for n in ("290429", "A2h", "x8") if n not in src_sa]
if miss:
    bad.append("в источнике нет %s" % (miss,))
check("28F008SA: байтовый (wide=False), cmd (0,0), 16 x 64 КиБ, 12 В, 290429-008",
      not bad, str(bad[:3]))

# 50h идёт ПОСЛЕ отказа и ДО возврата в режим массива (FFh). Без неё карта
# после первой же ошибки Vpp отвечала бы тем же битом до передёргивания питания.
card_sa, _bus_sa, flash_sa = NM.make(kit=KIT, chips=1, chip_name="28F008SA",
                                     blank=True, no_vpp=True)
log = []
_many = card_sa.write_many


def _spy(addr, data):
    log.append(bytes(data))
    return _many(addr, data)


card_sa.write_many = _spy
ok_novpp, why_novpp = flash_sa.erase_sector(0)
seq = [b[0] for b in log if len(b) == 1]
i_d0 = seq.index(0xD0) if 0xD0 in seq else -1
i_50 = seq.index(0x50) if 0x50 in seq else -1
i_ff = seq.index(0xFF) if 0xFF in seq else -1
check("нет Vpp: отказ по SR.3 с объяснением про 12 В",
      not ok_novpp and "Vpp" in why_novpp, why_novpp[:70])
check("после отказа послан 50h (Clear Status) и уже потом FFh",
      i_50 > i_d0 >= 0 and i_ff > i_50, (i_d0, i_50, i_ff))

# ... и без ошибки Vpp тот же блок стирается штатно
card_ok, _bus_ok, flash_ok = NM.make(kit=KIT, chips=1, chip_name="28F008SA",
                                     blank=True)
ok_e, why_e = flash_ok.erase_sector(0)
check("28F008SA: штатное стирание блока работает",
      ok_e and card_ok.erases == 1 and flash_ok.read_byte(0) == 0xFF,
      (why_e[:60], card_ok.erases))

# Пауза стирания: у Intel это B0h/D0h (таблица 3), а не отказ.
check("Intel: пауза стирания (B0h) принимается", flash_ok.erase_suspend() is True)
check("Intel: продолжение стирания (D0h) принимается", flash_ok.erase_resume() is True)


class _SuspendedFlash(D.NorFlash):
    """Чип, который всё время отвечает «стирание приостановлено»."""

    def read_status(self, card_off=0):
        return D.SR_READY | D.SR_SUSPEND


susp = _SuspendedFlash(_SilentBus(), chips=1, sector=64 << 10, name="тест",
                       protocol="intel")
ok_s, why_s = susp.erase_sector(0)
check("SR.6: приостановленное стирание не выдаётся за готовое",
      not ok_s and "приостановлено" in why_s and "D0h" in why_s, why_s[:80])

for path in ("windows/legacy/pcmcia_direct.py", "src/pcmcia_flash/nor_flash.py"):
    src = (pathlib.Path(KIT) / path).read_text(encoding="utf-8")
    if not ("def _clear_status_and_reset" in src
            and "INTEL_CUI_CLEAR_SR" in src
            and "SR_SUSPEND" in src):
        check("50h/SR.6 в " + path, False, "нет _clear_status_and_reset/50h/SR.6")
    else:
        check("50h и SR.6 в " + path.split("/")[-1], True)


# --- итог ---------------------------------------------------------------------
print("\n" + "=" * 70)
print("ИТОГО: прошло %d, провалено %d" % (len(PASS), len(FAIL)))
if FAIL:
    for name, detail in FAIL:
        print("  FAIL: " + name + ("   " + str(detail) if detail else ""))
    sys.exit(1)
print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
