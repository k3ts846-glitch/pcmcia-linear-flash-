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
import re as _re
import sys
import tempfile
import types as _types

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT)
sys.path.insert(0, os.path.join(KIT, "src"))
sys.path.insert(0, os.path.join(KIT, "tests"))

import nor_model as NM                                              # noqa: E402
from pcmcia_flash import image as _image                            # noqa: E402

# С 2.18.0 монолита windows/legacy/pcmcia_direct.py нет: код разложен по
# модулям пакета src/pcmcia_direct. Проверки, которым нужен ИСХОДНЫЙ ТЕКСТ
# (а не поведение), читают все модули подряд через direct_source().
DIRECT_PKG = pathlib.Path(KIT, "src", "pcmcia_direct")
DIRECT_FILES = sorted(DIRECT_PKG.glob("*.py"))


def direct_source():
    """Весь исходник пакета pcmcia_direct одним текстом."""
    return "\n".join(p.read_text(encoding="utf-8") for p in DIRECT_FILES)

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

# 2.18.0: _autodetect_intel создаёт второй объект шины (NorFlash словом).
# NorFlash живёт в nor.py, а nor.py берёт имена из chips.py — импорт внутри
# функции обязателен, иначе кольцо. Пропущенный импорт виден только вызовом:
# ruff (F821) нашёл его в разбитом монолите, а прежние тесты не звали функцию.
card, bus, flash = NM.make(kit=KIT, chips=1, chip_name="28F008SA", blank=False)
_silent = D._autodetect_intel(flash)
check("Intel ID: функция зовётся (NorFlash подключается внутри, без кольца)",
      isinstance(_silent, dict), repr(_silent))
check("Intel ID: молчащая микросхема -> пустой ответ, а не 0x00 в имени",
      _silent == {}, repr(_silent))

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
        # 2.17.24: у первых трёх причина своя («документа не нашли»), у
        # MBM29F002/004 - общая про ревизию. В обоих случаях отказ обязан
        # называть, что делать дальше.
        check("%s: отказ с подсказкой, а не умолчания" % name,
              ("документа на имя" in msg or "ревизи" in msg) and "Укажите" in msg,
              msg[:70])

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

# «28F320» без ревизии: у B3 boot-блоки (8 x 8 КиБ + до 63 x 64 КиБ), у J3 -
# 32 блока по 128 КиБ. Гадать нельзя, и 2.17.26 отказ называет ОБА семейства,
# а не просит «указать ревизию» вообще (B3 и J3 - разные даташиты).
try:
    D.check_sector_map(D.geometry_for("28F320"))
    no320 = ""
except D.DirectError as exc:
    no320 = str(exc)
check("28F320 без ревизии: отказ называет 28F320B3 и 28F320J3",
      "28F320B3" in no320 and "28F320J3" in no320, no320[:80])

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
UNVERIFIED = {"HN28F010", "HN28F020", "HN28F100",
              "LH28F010", "LH28F020", "M28F016", "28F016SV",
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

# Счётчик N и данные в E8h идут СЛОВОМ, поэтому буфер имеет смысл только на
# 16-битной карте: на байтовой (x8) микросхеме код честно отказывает, а не
# пишет «команду + данные» (подробнее - §20, даташиты 28F320J5 / AP-646).
CARD, _bus16, FL16 = NM.make(kit=KIT, chips=1, chip_name="28F016S5", blank=True)
FL16.page, FL16.page_card = 32, 32
DATA32 = bytes((i * 37 + 11) & 0xFF for i in range(32))
ok, why = FL16.program_page(0, DATA32)
check("x8: буфер E8h отказывает (счётчик и данные - слово, нужна x16)",
      not ok and "x16" in why, why[:80])

# Пара чипов: карта 16-битная (wide), команда - ОДНО слово на пару. Если послать
# E8h «каждому чипу», второй байт чип прочтёт как счётчик N и буфер уедет -
# именно эта ошибка была в первой редакции правки.
CARD2C, _b2c, FL2C = NM.make(kit=KIT, chips=2, chip_name="28F016S5", blank=True)
FL2C.wide = True
FL2C.page, FL2C.page_card = 32, 64
D64 = bytes((i * 53 + 7) & 0xFF for i in range(64))
ok2c, why2c = FL2C.program_page(0, D64)
# Ширина команды: ровно по байту на чип. На карте с одним чипом прежнее
# bytes([op, op]) превращало команду в команду+данные.
CARD1, _b1, FL1 = NM.make(kit=KIT, chips=1, chip_name="28F008S5", blank=True)
ok, why = FL1.program_byte(0x100, 0x5A)
check("один чип: запись байта идёт (команда - один байт, не два)",
      ok and FL1.read_byte(0x100) == 0x5A, why[:70])
ok, why = FL1.erase_sector(0)
check("один чип: стирание сектора идёт", ok, why[:70])

check("пара чипов: E8h одним словом, данные сходятся",
      ok2c and all(FL2C.read_byte(i) == D64[i] for i in range(64))
      and CARD2C.bad_cui_cmds == 0, why2c[:80])

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
for where, text in (("pcmcia_direct", direct_source()),
                    ("pcmcia_flash", pathlib.Path(KIT, "src", "pcmcia_flash",
                                                  "nor_flash.py").read_text(encoding="utf-8"))):
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
for _file in DIRECT_FILES:
    for node in ast.walk(ast.parse(_file.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Dict):
            seen = {}
            for k in node.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    seen.setdefault(k.value, []).append(k.lineno)
            dup_keys += ["%s:%s:%s" % (_file.name, k, v)
                         for k, v in seen.items() if len(v) > 1]
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

for path, src in (("pcmcia_direct", direct_source()),
                  ("nor_flash.py", pathlib.Path(KIT, "src", "pcmcia_flash",
                                                "nor_flash.py")
                   .read_text(encoding="utf-8"))):
    if not ("def _clear_status_and_reset" in src
            and "INTEL_CUI_CLEAR_SR" in src
            and "SR_SUSPEND" in src):
        check("50h/SR.6 в " + path, False, "нет _clear_status_and_reset/50h/SR.6")
    else:
        check("50h и SR.6 в " + path, True)


# --- 20. Сверка с даташитами: буфер AMD, защита групп, E8h, bypass -----------
# Раунд 2.17.13. Каждый пункт проверялся по 3-4 документам, потому что ошибки
# были именно в КОДАХ КОМАНД, а не в тексте:
# * буфер AMD: 25h -> WC = N-1 -> данные -> 29h (Am29DL640G, S29GL-P,
#   W29GL256S, AMD MirrorBit 25539a, IS29GL064). В наборе был D0h и не было
#   счётчика - и модель повторяла ту же ошибку, поэтому проверки были зелёные;
# * защита групп секторов читается ОБЫЧНЫМ autoselect: 90h и адрес SGA+02h
#   (Am29F016B/D 21444E, Am29F016 18805-D, MX29F016 PM0590). Команды A6h нет:
#   «метод A6» в даташитах - это уровень V(ih) на ВЫВОДЕ A6;
# * Intel E8h/счётчик/D0h адресуются БЛОКОМ (28F320J5, 28F128J3A, AP-646).
section("буфер AMD: 25h -> WC -> данные -> 29h (4 даташита), а не D0h")


def _trace(bus):
    """Записать все циклы записи шины: [("wr"|"wm", адрес, значение/список)]."""
    log = []
    _wr, _wm = bus.wr, bus.wr_many
    bus.wr = lambda a, v: (log.append(("wr", a, v)), _wr(a, v))[1]
    bus.wr_many = lambda a, d: (log.append(("wm", a, list(d))), _wm(a, d))[1]
    return log


check("AMD_BUFFER_CONFIRM = 29h в обеих реализациях",
      D.AMD_BUFFER_CONFIRM == 0x29 and _CONST.AMD_BUFFER_CONFIRM == 0x29,
      (D.AMD_BUFFER_CONFIRM, _CONST.AMD_BUFFER_CONFIRM))

card_bf, bus_bf, flash_bf = NM.make(kit=KIT, chips=2, chip_name="Am29GL064",
                                    blank=True)
log_bf = _trace(bus_bf)
# Данные специально без байтов 25h/29h/D0h, иначе их не отличить от команд.
page_bf = bytes(0x80 + (i % 0x40) for i in range(flash_bf.page_card))
ok_bf, why_bf = flash_bf.program_page(0, page_bf)
wr_bf = [(a, v) for k, a, v in log_bf if k == "wr"]
vals_bf = [v for _a, v in wr_bf]
i25 = next((i for i, (_a, v) in enumerate(wr_bf) if v == 0x25), -1)
wc_ok = i25 >= 0 and wr_bf[i25 + 1][1] == flash_bf.page - 1 and \
    wr_bf[i25 + 1][0] == wr_bf[i25][0]
check("25h, затем счётчик WC = page-1 по тому же адресу",
      wc_ok, wr_bf[max(i25, 0):i25 + 3])
i29 = next((i for i, (_a, v) in enumerate(wr_bf) if v == 0x29), -1)
check("подтверждение - 29h по адресу сектора (SA), а не D0h",
      i29 > i25 >= 0 and wr_bf[i29][0] % max(flash_bf.chips, 1) == 0
      and 0xD0 not in vals_bf, (i25, i29))
check("модель включает strata для Am29GL064 (иначе путь буфера не проверяется)",
      flash_bf.strata is True, flash_bf.strata)
check("буферная запись AMD: данные легли, нарушений протокола нет",
      ok_bf and card_bf.bad_buffer == 0
      and all(flash_bf.read_byte(i) == page_bf[i] for i in range(len(page_bf))),
      (why_bf[:70], card_bf.bad_buffer))

# Протокол 2.17.12 (25h -> данные -> D0h, без счётчика) обязан ломаться в
# модели: без этого следующая такая ошибка снова пройдёт незамеченной.
card_old, bus_old, flash_old = NM.make(kit=KIT, chips=2, chip_name="Am29GL064",
                                       blank=True)
flash_old.unlock()
for chip in range(flash_old.chips):
    bus_old.wr(chip, D.AMD_BUFFER_SETUP)              # 25h
for i in range(flash_old.page):
    for chip in range(flash_old.chips):
        bus_old.wr(i * flash_old.chips + chip, 0x11)  # данные без WC
for chip in range(flash_old.chips):
    bus_old.wr(chip, 0xD0)                            # «подтверждение» D0h
check("модель отвергает старый протокол (25h -> данные -> D0h)",
      card_old.bad_buffer > 0 and flash_old.read_byte(0) == 0xFF,
      card_old.bad_buffer)

section("защита групп секторов: autoselect 90h + адрес SGA+02h (Am29F016B/D)")
card_pg, bus_pg, flash_pg = NM.make(kit=KIT, chips=1, chip_name="Am29F016",
                                    blank=True)
card_pg.protect_groups[3] = True                      # третья группа защищена
log_pg = _trace(bus_pg)
groups_pg = flash_pg.read_sector_protection()
cmds_pg = [v for e in log_pg if e[0] == "wm" for v in e[2]]
check("защита читается командой 90h, команды A6h нет",
      0x90 in cmds_pg and 0xA6 not in cmds_pg, cmds_pg[:4])
check("группа 3 защищена, остальные нет",
      groups_pg == {0: False, 1: False, 2: False, 3: True,
                    4: False, 5: False, 6: False, 7: False}, groups_pg)

section("Intel E8h: адрес БЛОКА (BA), а не 0 (28F320J5 / 28F128J3A / AP-646)")
bad_src = []
for path, src in (("pcmcia_direct", direct_source()),
                  ("nor_flash.py", pathlib.Path(KIT, "src", "pcmcia_flash",
                                                "nor_flash.py")
                   .read_text(encoding="utf-8"))):
    if not ("INTEL_BUFFER_SETUP, local_block" in src
            and "INTEL_BUFFER_CONFIRM, local_block" in src
            and "local_block" in src.split("def _program_page_intel")[1]
            .split("def ")[0]):
        bad_src.append(path)
check("E8h, счётчик и D0h идут по адресу блока в обеих реализациях",
      not bad_src, bad_src)
card_x8, _bus_x8, flash_x8 = NM.make(kit=KIT, chips=1, chip_name="28F008SA",
                                     blank=True)
flash_x8.page, flash_x8.page_card = 4, 4
ok_x8, why_x8 = flash_x8.program_page(0, b"\x01\x02\x03\x04")
check("x8 Intel: буфер E8h честно отказывает (слово = только x16)",
      not ok_x8 and "x16" in why_x8, why_x8[:70])

section("выход из Unlock Bypass: 90h + 00h, затем F0h (Am29LV320D / S29AS016J)")
card_by, bus_by, flash_by = NM.make(kit=KIT, chips=2, chip_name="Am29GL064",
                                    blank=True)
flash_by.enter_bypass()
log_by = _trace(bus_by)
flash_by.exit_bypass()
seq_by = [e[2] for e in log_by if e[0] == "wr"]
all_by = [v for e in log_by for v in (e[2] if isinstance(e[2], list) else [e[2]])]
check("выход: 90h и 00h по каждому чипу, F0h в конце",
      seq_by[:4] == [0x90, 0x00, 0x90, 0x00] and 0xF0 in all_by,
      (seq_by[:6], all_by[-4:]))
_buf_by = _io.StringIO()
with _ctx.redirect_stdout(_buf_by):
    ok_after_by, _why_by = flash_by.program_byte(0, 0x5A)
check("после выхода обычная запись проходит (карта не застряла в bypass)",
      ok_after_by and flash_by.read_byte(0) == 0x5A, _why_by[:60])

section("Intel CUI: сброс одним байтом FFh и одна пара 20h/D0h на стирание")
card_i, _bus_i, flash_i = NM.make(kit=KIT, chips=1, chip_name="28F008SA",
                                  blank=True)
log_i = []
_wm_i = card_i.write_many


def _spy_i(addr, data):
    log_i.append(list(data))
    return _wm_i(addr, data)


card_i.write_many = _spy_i
flash_i.reset()
check("reset(): ровно один байт FFh на чип (не два подряд)", log_i == [[0xFF]], log_i)
log_i.clear()
ok_er, why_er = flash_i.erase_sector(0)
n20 = sum(1 for d in log_i if d and d[0] == 0x20)
n_d0 = sum(1 for d in log_i if d and d[0] == 0xD0)
check("стирание Intel: ОДНА пара 20h/D0h", ok_er and n20 == 1 and n_d0 == 1,
      (n20, n_d0, why_er[:50]))

card_i2, _bus_i2, flash_i2 = NM.make(kit=KIT, chips=2, chip_name="28F008SA",
                                     blank=True)
log_i2 = []
_wm_i2 = card_i2.write_many
card_i2.write_many = lambda a, d: (log_i2.append(list(d)), _wm_i2(a, d))[1]
ok_e2, why_e2 = flash_i2.erase_sector(0)
n20b = sum(1 for d in log_i2 if d and d[0] == 0x20)
n_d0b = sum(1 for d in log_i2 if d and d[0] == 0xD0)
check("стирание Intel на паре чипов: снова одна пара (слово 20h/D0h)",
      ok_e2 and n20b == 1 and n_d0b == 1, (n20b, n_d0b, why_e2[:50]))

model_src = (pathlib.Path(KIT) / "tests" / "nor_model.py").read_text(encoding="utf-8")
check("модель чипа требует WC и 29h (иначе проверки выше ничего не доказывают)",
      "bad_buffer" in model_src and 'buf["left"]' in model_src
      and "AMD_BUFFER_CONFIRM" in model_src)


# --- 21. Карта из ДВУХ x8-чипов: команда - каждому чипу ----------------------
# У каждого чипа свой командный регистр, поэтому команда должна дойти до
# каждого. Как это делается - три независимых источника:
#   * Infineon KBA203626 «Connecting 2x8-bit Parallel NOR Flash Devices to MCU»:
#     «commands must be written in parallel to each 1x8 flash device ... flash
#     command cycles must be duplicated within the 2B word ... one copy in the
#     low byte and one copy ... in the high byte», и там же: «status reads ...
#     require special logic ... one device will become ready before the other»;
#   * Linux MTD drivers/mtd/chips/cfi_util.c, cfi_build_cmd(): байт команды
#     размножается по всем чипам слова, адрес - cmd_ofs * interleave;
#   * OpenOCD (dual flash mode): «command byte is sent to both chips but data
#     bytes are sent alternatingly».
# Было до 2.17.14: _send() писал два байта только при wide=True, поэтому на
# паре x8 (wide=False) ВСЕ команды уходили в чип 0, а данные - в оба.
section("два x8-чипа: команды - каждому чипу, данные - словом")


def _trace2(bus):
    log = []
    _wr, _wm = bus.wr, bus.wr_many
    bus.wr = lambda a, v: (log.append((a, [v])), _wr(a, v))[1]
    bus.wr_many = lambda a, d: (log.append((a, list(d))), _wm(a, d))[1]
    return log


card_p2, bus_p2, flash_p2 = NM.make(kit=KIT, chips=2, chip_name="Am29F016",
                                    blank=True)
log_p2 = _trace2(bus_p2)
ok_p2, why_p2 = flash_p2.program_byte(0x1000, 0x5A)
cmds_p2 = [(a, d) for a, d in log_p2 if a in (0xAAA, 0x554)]
check("команды AA/55/A0h уходят ОБОИМ чипам (словом по байту на чип)",
      ok_p2 and cmds_p2 and all(len(d) == 2 and d[0] == d[1]
                                for _a, d in cmds_p2),
      cmds_p2)
check("данные пишутся словом: свой байт + соседний как есть",
      flash_p2.read(0x1000, 2) == b"\x5a\xff", flash_p2.read(0x1000, 2).hex())

ok_p2b, why_p2b = flash_p2.program_byte(0x1001, 0xA5)      # НЕЧЁТНЫЙ адрес
check("нечётный байт (чип 1) программируется, соседний не портится",
      ok_p2b and flash_p2.read(0x1000, 2) == b"\x5a\xa5",
      (why_p2b[:60], flash_p2.read(0x1000, 2).hex()))

card_e2, bus_e2, flash_e2 = NM.make(kit=KIT, chips=2, chip_name="Am29F016",
                                    blank=False)
log_e2 = _trace2(bus_e2)
ok_e2, why_e2 = flash_e2.erase_sector(0)
tgt = sorted(card_e2.erase_targets)
check("стирание: один цикл 30h словом - сектор стёрт в ОБОИХ чипах",
      ok_e2 and tgt == [(0, 0), (1, 0)] and card_e2.erases == 2,
      (why_e2[:50], tgt, card_e2.erases))
n30 = [d for a, d in log_e2 if d and d[0] == D.NOR_ERASE_SECTOR]
check("30h послан ОДИН раз (словом), а не по циклу на чип",
      len(n30) == 1 and len(n30[0]) == 2, n30)

card_ce, _bus_ce, flash_ce = NM.make(kit=KIT, chips=2, chip_name="Am29F016",
                                     blank=False)
ok_ce, why_ce = flash_ce.erase_chip()
check("стирание чипа: стёрты оба чипа", ok_ce and card_ce.erases == 2,
      (why_ce[:60], card_ce.erases))

# Старый протокол (команды только чипу 0, данные - чипу 1) обязан ЛОМАТЬСЯ в
# модели: иначе следующая такая ошибка снова пройдёт незамеченной.
card_old2, bus_old2, flash_old2 = NM.make(kit=KIT, chips=2,
                                          chip_name="Am29F016", blank=True)
bus_old2.wr_many(0xAAA, bytes([0xAA]))
bus_old2.wr_many(0x554, bytes([0x55]))
bus_old2.wr_many(0xAAA, bytes([0xA0]))
bus_old2.wr(0x1001, 0x77)
check("модель: данные без команды чипу НЕ программируются",
      flash_old2.read(0x1001, 1) == b"\xff", flash_old2.read(0x1001, 1).hex())
card_old3, bus_old3, flash_old3 = NM.make(kit=KIT, chips=2,
                                          chip_name="Am29F016", blank=False)
bus_old3.wr(1, D.NOR_ERASE_SECTOR)         # один 30h, без 80h/AAh/55h
check("модель: один 30h без 80h/AAh/55h не стирает",
      card_old3.erases == 0 and flash_old3.read(0, 1) != b"\xff",
      card_old3.erases)

# Intel: команда CUI действует на карту, поэтому её адрес всегда от чипа 0.
# Прежний chip_abs(..., chip) для chip=1 писал два байта СО СДВИГОМ: команда
# чипу 1 и та же команда чипу 0 по другому адресу.
card_i3, bus_i3, flash_i3 = NM.make(kit=KIT, chips=2, chip_name="28F008SA",
                                    blank=True)
log_i3 = _trace2(bus_i3)
flash_i3._intel_send(1, 0x70)
addrs_i3 = [a for a, _d in log_i3]
check("_intel_send для чипа 1 пишет по чётному адресу (без сдвига на байт)",
      addrs_i3 and all(a % 2 == 0 for a in addrs_i3), addrs_i3)

# Unlock Bypass на паре чипов: сеанс НЕ заканчивается на записи (даташит:
# выход - 90h/00h или сброс), поэтому второй и третий байты должны пройти без
# повторной разблокировки.
card_b2, _bus_b2, flash_b2 = NM.make(kit=KIT, chips=2, chip_name="Am29GL064",
                                     blank=True)
_buf_b2 = _io.StringIO()
with _ctx.redirect_stdout(_buf_b2):
    b_1 = flash_b2.program_byte(0x1000, 0x11)
    b_2 = flash_b2.program_byte(0x1001, 0x22)
    b_3 = flash_b2.program_byte(0x1002, 0x33)
    flash_b2.exit_bypass()
check("bypass на паре чипов: сеанс держится между байтами, данные верны",
      b_1[0] and b_2[0] and b_3[0] and not flash_b2._bypass_broken
      and flash_b2.read(0x1000, 4) == b"\x11\x22\x33\xff",
      (b_2[1][:40], flash_b2.read(0x1000, 4).hex()))

st_left = {c: s for c, s in getattr(card_p2, "_st", {}).items() if s != "idle"}
check("после записи оба чипа вернулись в состояние idle", not st_left, st_left)


# --- 22. 28F256/28F512: регистр команд, а не AMD-циклы 555h/2AAh -----------
# Обе строки держались на догадке «12-вольтовая, значит как Am29F016»: в них
# стояли JEDEC-адреса команд (555h/2AAh) и НЕ было флага ro. Сверили по пяти
# независимым документам - и все они совпадают:
#  * Intel 28F256A (290243-006): 00h чтение, 90h ID (0000h=89h, 0001h=B9h),
#    20h+20h стирание ВСЕГО чипа, A0h проверка стирания, 40h+PA/PD запись,
#    C0h проверка записи, FFh сброс; блочного стирания нет, 12 В +-5% на Vpp;
#  * AMD Am28F256 (публикация 11560): та же таблица, «the entire chip is bulk
#    erased»;
#  * ST M28F256: 90h, 20h+20h, A0h, 40h, C0h - та же семья;
#  * Intel 28F512 (64K x 8): те же команды, ID 89h/B8h, «Flash Electrical
#    Chip-Erase» (только чип), 12 В;
#  * TI TMS28F512A и Catalyst CAT28F512: те же коды (B8h).
# Значит, как и у 28F010/28F020 (раздел 18), цикла «стереть сектор - записать»
# здесь нет, и единственное честное поведение - только чтение.
section("28F256/28F512: CUI Intel, стирание только целиком, только чтение")
bad = []
for name, size, did in (("28F256", 32 << 10, "B9h"), ("28F512", 64 << 10, "B8h")):
    geo = D.GEOMETRY.get(name)
    if not geo:
        bad.append("%s нет в GEOMETRY" % name)
        continue
    if not geo.get("ro"):
        bad.append("%s: не помечен ro" % name)
    if geo.get("protocol") != "intel":
        bad.append("%s: protocol %s" % (name, geo.get("protocol")))
    if tuple(geo["cmd"]) != (0, 0):
        bad.append("%s: cmd %s вместо (0, 0)" % (name, geo["cmd"]))
    if geo.get("size") != size or geo.get("sector") != size:
        bad.append("%s: size %s, sector %s (стирается только весь чип)"
                   % (name, geo.get("size"), geo.get("sector")))
    if geo.get("volts") != 12:
        bad.append("%s: volts %s" % (name, geo.get("volts")))
    if did not in str(geo.get("src") or ""):
        bad.append("%s: в src нет ID %s" % (name, did))
check("28F256/512: ro, CUI, cmd (0,0), сектор = весь чип, 12 В, ID в src",
      not bad, str(bad[:3]))

bad = [(n, g.get("size"), g.get("sector")) for n, g in D.GEOMETRY.items()
       if (g.get("sector") or 0) > (g.get("size") or 0)]
check("GEOMETRY: сектор ни у одной строки не больше ёмкости чипа", not bad,
      str(bad[:3]))

# Две таблицы (монолит и пакет) описывают одни и те же имена - значит обязаны
# совпадать: иначе одна реализация сотрёт блок, а другая возьмёт иной размер.
# protocol=None и "jedec" - одно и то же; адреса команд значимы только у JEDEC
# (у Intel-семьи команда идёт в нулевой адрес, cmd остаётся справочным полем).
bad = []
common = sorted(set(C.CHIP_GEOMETRY) & set(D.GEOMETRY))
for name in common:
    g, p = D.GEOMETRY[name], C.CHIP_GEOMETRY[name]
    gp, pp = g.get("protocol") or "jedec", p.get("protocol") or "jedec"
    if gp != pp:
        bad.append("%s: protocol %s / %s" % (name, gp, pp))
        continue
    trio_g = (g.get("size"), g.get("sector"), g.get("volts"))
    trio_p = (p.get("size"), p.get("sector"), p.get("volts"))
    if trio_g != trio_p:
        bad.append("%s: size/sector/volts %s / %s" % (name, trio_g, trio_p))
    flags_g = (bool(g.get("wide")), bool(g.get("bypass")), bool(g.get("ro")),
               g.get("page") or 1)
    flags_p = (bool(p.get("wide")), bool(p.get("bypass")), bool(p.get("ro")),
               p.get("page") or 1)
    if flags_g != flags_p:
        bad.append("%s: wide/bypass/ro/page %s / %s" % (name, flags_g, flags_p))
    if gp == "jedec" and tuple(g.get("cmd") or ()) != tuple(p.get("cmd") or ()):
        bad.append("%s: cmd %s / %s" % (name, tuple(g.get("cmd") or ()),
                                        tuple(p.get("cmd") or ())))
check("GEOMETRY и chips.json: %d общих имён совпадают по геометрии" % len(common),
      not bad and len(common) > 60, str(bad[:3]))

# --- 2.18.1: пять проверенных по даташитам имён перенесены в базу пакета ----
section("chips.json: SST39VF010/020/080, MBM29F016A и MX29F1610")

# Ёмкость, сектор (КиБ) и напряжение - из даташитов (см. notes в chips.json):
#   SST DS20005023 - SST39VF010/020: uniform 4 KByte sectors, 2.7-3.6 В;
#   SST S71146 EOL - SST39VF080: 8 Mbit (1M x8), 256 секторов по 4 КиБ;
#   Fujitsu MBM29F016A (43 стр.) - 2 M байт = 32 сектора по 64 КБ, 5.0 V-Only;
#   Macronix MX29F1610 - «16 equal sectors of 128k bytes each», 5 В +-10%.
MOVED = {
    "SST39VF010": (128, 4, 3),
    "SST39VF020": (256, 4, 3),
    "SST39VF080": (1024, 4, 3),
    "MBM29F016A": (2048, 64, 5),
    "MX29F1610": (2048, 128, 5),
}
absent = [n for n in MOVED if n not in C.CHIP_GEOMETRY]
check("в базе пакета есть все 5 перенесённых имён", not absent, str(absent))
wrong = []
for name, (size_kib, sector_kib, volts) in MOVED.items():
    geo_p = C.CHIP_GEOMETRY.get(name) or {}
    if (geo_p.get("size") != size_kib << 10
            or geo_p.get("sector") != sector_kib << 10
            or geo_p.get("volts") != volts):
        wrong.append("%s: %s" % (name, (geo_p.get("size"), geo_p.get("sector"),
                                        geo_p.get("volts"))))
check("перенесённые строки совпадают с даташитами (ёмкость/сектор/напряжение)",
      not wrong, str(wrong[:3]))

# Unlock Bypass 20h: у SST39VF и MX29F1610 такой команды в таблицах команд нет,
# у Fujitsu MBM29F016A документом не подтверждена. bypass=True заставил бы
# набор посылать 20h, которой нет: запись откатывалась бы на A0h с
# предупреждением. У AMD Am29F016 20h подтверждён (18805) - там True и остаётся.
bad_bypass = []
for name, geo_p in C.CHIP_GEOMETRY.items():
    if geo_p.get("manuf") in ("SST", "Macronix") and geo_p.get("bypass"):
        bad_bypass.append(name)
check("SST и Macronix: unlock bypass не обещан без даташита", not bad_bypass,
      str(bad_bypass))
no_bypass = [n for n in ("MBM29F016", "MBM29F016A") 
             if C.CHIP_GEOMETRY.get(n, {}).get("bypass")
             or D.GEOMETRY.get(n, {}).get("bypass")]
check("Fujitsu MBM29F016/016A: выдуманного bypass нет ни в одной таблице",
      not no_bypass, str(no_bypass))
# И обратная сторона: там, где 20h подтверждён документом, bypass обязан быть.
# Am29F016 - False осознанно (даташит 18805: «Unlock Bypass появился только в
# ревизии D»), Am29F016D и Am29F032B - True (21444E5).
no_flag = [n for n in ("Am29F016D", "Am29F032B")
           if not (C.CHIP_GEOMETRY.get(n, {}).get("bypass")
                   and D.GEOMETRY.get(n, {}).get("bypass"))]
check("AMD Am29F016D/Am29F032B: bypass остался (даташит 21444E5)", not no_flag,
      str(no_flag))
check("AMD Am29F016: bypass не обещан (20h только с ревизии D)",
      not C.CHIP_GEOMETRY.get("Am29F016", {}).get("bypass")
      and not D.GEOMETRY.get("Am29F016", {}).get("bypass"))

# Одна и та же микросхема под двумя именами: геометрия обязана совпадать.
g_a = C.CHIP_GEOMETRY.get("MBM29F016") or {}
g_b = C.CHIP_GEOMETRY.get("MBM29F016A") or {}
check("MBM29F016 и MBM29F016A описаны одинаково (одна микросхема, два имени)",
      (g_a.get("size"), g_a.get("sector"), g_a.get("volts"))
      == (g_b.get("size"), g_b.get("sector"), g_b.get("volts")))

# Остаток расхождения: 12 имён только в наборе, и все - заведомо не перенесённые
# (unverified) или отказ по документу (Am29F033: у AMD нет такой 5-вольтовой).
only_direct = sorted(set(D.GEOMETRY) - set(C.CHIP_GEOMETRY))
loose = [n for n in only_direct
         if not (D.GEOMETRY[n].get("unverified") or D.GEOMETRY[n].get("why"))]
check("имён только в наборе ровно 12, и все помечены (не сверены или отказ)",
      len(only_direct) == 12 and not loose,
      "%d: %s" % (len(only_direct), str(loose[:3]) or str(only_direct)))

# Поведение на модели: ro обязан дойти до NorFlash, а массив - остаться целым.
card_r, _bus_r, flash_r = NM.make(kit=KIT, chips=1, chip_name="28F512",
                                  blank=False)
before_r = bytes(card_r.mem[0][:64])
ans_r = [flash_r.program_byte(0, 0x5A), flash_r.erase_sector(0),
         flash_r.erase_chip()]
check("28F512 (модель): запись/сектор/чип - отказ, массив не тронут",
      all(not ok for ok, _w in ans_r) and bytes(card_r.mem[0][:64]) == before_r,
      (ans_r[0][1][:60], bytes(card_r.mem[0][:4]).hex()))
why_r = ans_r[0][1]
check("28F512 (модель): причина - «не выполняется», 12 В на Vpp, стирание целиком",
      "не выполняется" in why_r and "Vpp" in why_r and "ВСЕГО чипа" in why_r,
      why_r[:90])



# --- 23. Версия набора: --version, отчёт, читатели, tests/release.py -------
# Аудит 2.17.16 (всё проверено запуском, а не чтением):
#  * у монолита не было --version, хотя комментарий у KIT_VERSION обещал печать
#    версии «в отчёте и в --selftest»;
#  * в отчёте о карте версии не было, хотя её просят присылать вместе с выводом;
#  * пять мест читают KIT_VERSION из текста и брали строку ЦЕЛИКОМ вместе с
#    хвостовым комментарием: пульт показывал «2.17.15"   # версия набора: ...»;
#  * tests/release.py не мог пройти никогда: искал __version__ в __init__.py
#    (там литерала нет, только `from ._version import __version__`), требовал
#    windows/collect_pcmcia_info.bat (переехал в windows/legacy в 2.16.0) и
#    считал дефектом кэш Python, который сам же убирает при сборке.
section("версия набора: --version, отчёт, читатели, tests/release.py")

import importlib.util as _ilu                                       # noqa: E402
import shutil as _sh                                                # noqa: E402
import subprocess as _sp                                            # noqa: E402

ver = D.KIT_VERSION
direct = os.path.join(KIT, "pcmcia_direct.py")

_run = _sp.run([sys.executable, "-B", direct, "--version"],
               capture_output=True, text=True)
check("pcmcia_direct.py --version: код 0, в выводе имя и версия",
      _run.returncode == 0 and ver in _run.stdout
      and "pcmcia_direct.py" in _run.stdout,
      (_run.returncode, _run.stdout[:60], _run.stderr[:60]))

_run_help = _sp.run([sys.executable, "-B", direct, "--help"],
                    capture_output=True, text=True)
check("pcmcia_direct.py --help показывает ключ --version",
      "--version" in _run_help.stdout, _run_help.stdout[:80])

_run_pkg = _sp.run([sys.executable, "-B", "-c",
                    "import sys; sys.path.insert(0, 'src'); "
                    "from pcmcia_flash.cli import main; main(['--version'])"],
                   capture_output=True, text=True, cwd=KIT)
check("пакетный двойник pcmcia_flash --version: код 0 и та же версия",
      _run_pkg.returncode == 0 and ver in _run_pkg.stdout,
      (_run_pkg.returncode, _run_pkg.stdout[:60], _run_pkg.stderr[:80]))

_parts = {"geo": D.geometry_for("MX29F1610", size=2 << 20), "chips": 1,
          "size": 2 << 20, "how": "тест",
          "win": {"start": 0xC0000, "size": 4 << 20},
          "bus": _types.SimpleNamespace(span=8192)}
_buf_ver = _io.StringIO()
with _ctx.redirect_stdout(_buf_ver):
    D._flash_report(_parts)
_first = _buf_ver.getvalue().splitlines()[0]
check("отчёт о карте начинается строкой с версией набора",
      ver in _first and "версия" in _first, _first[:70])

from pcmcia_flash import _version as _pver                          # noqa: E402
from pcmcia_flash import cli as _pcli                               # noqa: E402
from pcmcia_kit import _version as _kver                            # noqa: E402
from pcmcia_kit import cli as _kcli                                 # noqa: E402

_buf_pkg = _io.StringIO()
with _ctx.redirect_stdout(_buf_pkg):
    _pcli._flash_report(_parts)
check("пакетный отчёт тоже называет версию",
      ver in _buf_pkg.getvalue().splitlines()[0], _buf_pkg.getvalue()[:70])

check("версия одна: KIT_VERSION, оба _version.py и читатель пульта",
      _pver.__version__ == ver and _kver.__version__ == ver
      and _kcli.kit_version(KIT) == ver,
      (_pver.__version__, _kver.__version__, _kcli.kit_version(KIT)))

_greedy = '.split("=", 1)[1].strip()'
_readers = ["collect_all.py", "pcmcia_ctl.py", "linux/pcmcia_flash.py",
            "src/pcmcia_kit/cli.py"]
_left = [rel for rel in _readers
         if _greedy in (pathlib.Path(KIT) / rel).read_text(encoding="utf-8")]
check("читатели KIT_VERSION отрезают хвостовой комментарий (%d файлов)"
      % len(_readers), not _left, str(_left))

_spec = _ilu.spec_from_file_location("_rel",
                                     os.path.join(KIT, "tests", "release.py"))
_rel = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_rel)
check("tests/release.py: package_version находит версию (было None)",
      _rel.package_version() == ver
      and _rel.package_version(_rel.PACKAGES[1]) == ver,
      (_rel.package_version(), _rel.package_version(_rel.PACKAGES[1])))

_probs = _rel.check_tree()
check("tests/release.py: check_tree на рабочем дереве чист", _probs == [],
      str(_probs[:3]))

_rel_text = (pathlib.Path(KIT) / "tests" / "release.py").read_text(encoding="utf-8")
# 2.18.0: монолит разложен по модулям, папки windows/legacy больше нет, поэтому
# в обязательном составе - лаунчер и модули пакета, а путей в legacy нет вовсе.
check("tests/release.py: в обязательном составе лаунчер и пакет, legacy нет",
      "pcmcia_direct.py" in _rel.REQUIRED
      and "src/pcmcia_direct/cli.py" in _rel.REQUIRED
      and "src/pcmcia_direct/chips_data.py" in _rel.REQUIRED
      and not [r for r in _rel.REQUIRED if r.startswith("windows/legacy")],
      str([r for r in _rel.REQUIRED if "legacy" in r]))

_run_rel = _sp.run([sys.executable, "-B", "tests/release.py"],
                   capture_output=True, text=True, cwd=KIT)
check("tests/release.py как программа: код 0 и «проверки до сборки пройдены»",
      _run_rel.returncode == 0 and "проверки до сборки пройдены" in _run_rel.stdout,
      (_run_rel.returncode, _run_rel.stdout[-80:], _run_rel.stderr[-80:]))

_fake = tempfile.mkdtemp(prefix="relkit-")
try:
    for rel, text in (("src/pcmcia_direct/version.py",
                       'KIT_VERSION = "%s"\n' % ver),
                      ("collect_all.py",
                       "x = '(в переписке должна быть %s)'\n" % ver),
                      ("src/pcmcia_kit/_version.py", '__version__ = "%s"\n' % ver),
                      ("src/pcmcia_flash/_version.py", '__version__ = "%s"\n' % ver)):
        target = os.path.join(_fake, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(text)
    os.makedirs(os.path.join(_fake, "__pycache__"))
    with open(os.path.join(_fake, "__pycache__", "x.pyc"), "w") as handle:
        handle.write("x")
    with open(os.path.join(_fake, ".card-state.txt"), "w") as handle:
        handle.write("state")
    with open(os.path.join(_fake, "left.orig"), "w") as handle:
        handle.write("old")
    _old_kit = _rel.KIT
    _rel.KIT = _fake
    try:
        _fake_probs = _rel.check_tree()
    finally:
        _rel.KIT = _old_kit
finally:
    _sh.rmtree(_fake, ignore_errors=True)
_junk = [p for p in _fake_probs if "__pycache__" in p or ".card-state.txt" in p]
_leftover = [p for p in _fake_probs if p.endswith("left.orig")]
check("кэш Python - не дефект, остаток правки (.orig) - дефект",
      not _junk and bool(_leftover), (_junk[:2], _leftover[:2]))



# --- 24. D28F010/D28F020 - это Intel в керамике, а не NEC с JEDEC-циклами --
# Аудит 2.17.17. Строки стояли как «NEC D28F010/020» с адресами 555h/2AAh и
# пометкой «не сверено». Даташита NEC с таким именем найти не удалось, а три
# независимых источника сходятся на другом: D28F... - керамическое (CDIP)
# исполнение Intel 28F010/28F020 военной серии MD28F...:
#  * справочники: D28F010-120P1C4 - Intel, CERAMIC DIP32, 128K x 8,
#    «Command User Interface: YES», «Data Polling: NO» (то есть CUI, не JEDEC);
#  * перечень заказных комбинаций самого Intel 28F010 (290207-010) знает
#    P28F010/N28F010/E28F010/F28F010 и T-варианты; керамика идёт отдельной
#    военной серией MD28F010/MD28F020 (Rochester: «28F020 - 2048K (256K x 8),
#    Mil Temp»);
#  * таблица соответствий AMD (1992): Intel D28F010-150P1C4 = Am28F010-150DC;
#  * перечень D28-серии: D28F020-150 (DIP), D28F256-200P1C2 (INT, CDIP).
# Значит команды у них те же, что у 28F010/28F020: регистр команд (00h/90h/
# 20h+20h/40h/A0h/C0h/FFh), 12 В на Vpp, стирание только ВСЕГО чипа, без
# статусного регистра 70h - и такие строки обязаны быть ro.
section("D28F010/D28F020: Intel в керамике (CUI), как 28F010/28F020")

bad = []
for name, size in (("D28F010", 128 << 10), ("D28F020", 256 << 10)):
    geo = D.GEOMETRY.get(name) or {}
    if geo.get("unverified"):
        bad.append("%s: всё ещё «не сверено»" % name)
    if geo.get("protocol") != "intel" or not geo.get("ro"):
        bad.append("%s: protocol %s, ro %s"
                   % (name, geo.get("protocol"), geo.get("ro")))
    if tuple(geo.get("cmd") or ()) != (0, 0):
        bad.append("%s: cmd %s вместо (0, 0)" % (name, geo.get("cmd")))
    if geo.get("size") != size or geo.get("sector") != size:
        bad.append("%s: size %s, sector %s (стирается только весь чип)"
                   % (name, geo.get("size"), geo.get("sector")))
    if geo.get("volts") != 12:
        bad.append("%s: volts %s" % (name, geo.get("volts")))
    src = str(geo.get("src") or "")
    if "керамич" not in src or "290207" not in src:
        bad.append("%s: в src нет источника" % name)
check("D28F010/020: Intel CUI, ro, cmd (0,0), сектор = весь чип, 12 В, источник",
      not bad, str(bad[:3]))

# M28F102: по даташиту ST (август 1998) стирание только целиком, поэтому и у
# него предохранитель должен стоять в самом NorFlash, а не только в
# check_sector_map (как у 28F010/28F020 с 2.17.11).
m102 = D.GEOMETRY["M28F102"]
check("M28F102: ro в NorFlash, причина - стирание только целиком",
      bool(m102.get("ro")) and "целиком" in str(m102.get("why") or ""),
      (m102.get("ro"), (m102.get("why") or "")[:60]))

# Инвариант: 12-вольтовая строка не может выглядеть обычной пишущейся
# JEDEC-микросхемой. У первых поколений либо регистр команд (Intel CUI), либо
# честная пометка «не сверено», либо отказ с объяснением.
susp = []
for name, geo in sorted(D.GEOMETRY.items()):
    if geo.get("volts") != 12:
        continue
    if (geo.get("protocol") == "intel" or bool(geo.get("unverified"))
            or (bool(geo.get("ambiguous")) and bool(geo.get("why")))):
        continue
    susp.append(name)
check("ни одна 12-вольтовая строка не притворяется обычной JEDEC-микросхемой",
      not susp, str(susp[:4]))

# Поведение на модели: ro доходит до NorFlash, данные не портятся.
card_d, _bus_d, flash_d = NM.make(kit=KIT, chips=1, chip_name="D28F010",
                                  blank=False)
before_d = bytes(card_d.mem[0][:64])
ans_d = [flash_d.program_byte(0, 0x5A), flash_d.erase_sector(0),
         flash_d.erase_chip()]
check("D28F010 (модель): запись/сектор/чип - отказ, массив не тронут",
      all(not ok for ok, _w in ans_d) and bytes(card_d.mem[0][:64]) == before_d,
      (ans_d[0][1][:60], bytes(card_d.mem[0][:4]).hex()))
try:
    D.check_sector_map(D.geometry_for("D28F010"))
    refuse_d = ""
except D.DirectError as exc:
    refuse_d = str(exc)
check("D28F010: открытие по имени отвергается с объяснением (Vpp, весь чип)",
      "ВСЕГО чипа" in refuse_d and "Vpp" in refuse_d, refuse_d[:80])



# --- 25. M28F008/M28F016: Intel военной серии (CUI), а не ST с 555h/2AAh ---
# Аудит 2.17.18. Обе строки числились за ST и стояли с адресами 555h/2AAh при
# 12 В. Справочники относят M28F008 к Intel («INTEL - 8 MBIT (1 MBIT x 8) FLASH
# MEMORY», 28 страниц; у Rochester то же описание с «Extended Cycling
# Capability»), а архитектура у него как у 28F008SA: 16 блоков по 64 КиБ,
# CUI + статусный регистр, 12 В на Vpp, команда байтом. Документ MS28F016SV
# перечисляет M28F008 среди совместимых по набору команд («Backwards-Compatible
# with VE28F008, M28F008 and 28F016SA Command Set»). Для M28F016 точного имени
# в перечнях Intel не нашлось, поэтому пометка «не сверено» осталась; но 16-
# Мбит x8 части этой семьи (28F016SA/MS28F016SV) - CUI, и адреса JEDEC у
# 12-вольтовой Intel-строки были бы заведомо ложью.
section("M28F008/M28F016: Intel военной серии (CUI), а не ST с 555h/2AAh")

geo_sa25 = D.GEOMETRY["28F008SA"]
geo_m8 = D.GEOMETRY["M28F008"]
bad_m8 = []
if geo_m8.get("protocol") != "intel":
    bad_m8.append("protocol=%s" % geo_m8.get("protocol"))
if tuple(geo_m8.get("cmd") or ()) != (0, 0):
    bad_m8.append("cmd=%s" % (geo_m8.get("cmd"),))
if geo_m8.get("unverified"):
    bad_m8.append("unverified")
diff_m8 = [(k, geo_m8.get(k), geo_sa25.get(k)) for k in
           ("size", "sector", "volts", "page", "protocol")
           if geo_m8.get(k) != geo_sa25.get(k)]
if diff_m8 or bool(geo_m8.get("wide")) != bool(geo_sa25.get("wide")):
    bad_m8.append("расходится с 28F008SA: %s wide=%s/%s"
                  % (diff_m8, geo_m8.get("wide"), geo_sa25.get("wide")))
check("M28F008: CUI, cmd (0,0), геометрия как у 28F008SA, «не сверено» снято",
      not bad_m8, str(bad_m8[:3]))

card_m8, _bus_m8, flash_m8 = NM.make(kit=KIT, chips=1, chip_name="M28F008",
                                     blank=True)
ok_w8 = flash_m8.program_byte(0x100, 0x5A)
ok_r8 = flash_m8.read(0x100, 2) == b"\x5a\xff"
ok_e8 = flash_m8.erase_sector(0)
ok_z8 = flash_m8.read(0x100, 2) == b"\xff\xff"
check("M28F008 (модель): запись байта и стирание блока идут путём Intel CUI",
      flash_m8.protocol == "intel" and ok_w8[0] and ok_r8 and ok_e8[0] and ok_z8,
      (flash_m8.protocol, ok_w8, ok_e8, flash_m8.read(0x100, 2).hex()))

geo_m16 = D.GEOMETRY["M28F016"]
src_m16 = str(geo_m16.get("src") or "")
check("M28F016: CUI и cmd (0,0), пометка «не сверено» осталась (имя не найдено)",
      geo_m16.get("protocol") == "intel"
      and tuple(geo_m16.get("cmd") or ()) == (0, 0)
      and bool(geo_m16.get("unverified")) and "28F016SA" in src_m16,
      (geo_m16.get("protocol"), geo_m16.get("cmd"),
       geo_m16.get("unverified"), src_m16[:60]))

bad_st25 = [n for n in ("M28F008", "M28F016")
            if "ST M28F0" in str(D.GEOMETRY[n].get("src") or "")]
check("M28F008/M28F016: ни одна строка больше не выдаёт себя за ST",
      not bad_st25, str(bad_st25))

# --- 26. chips.json: Intel CUI без адресов и сверенные 12-вольтовые строки ---
# 2.17.21. В базе пакета у пяти строк с protocol="intel" стояли адреса
# 5555h/2aaah - ровно та картина «обычная JEDEC-микросхема», из-за которой
# в 2.17.15-2.17.18 пришлось переписывать 28F256/512, D28F010/020 и
# M28F008/016. У Intel CUI адрес команды не декодируется вовсе: в монолите
# все строки CUI стоят с cmd=(0,0), так же должно быть и в chips.json.
# Там же появились три строки, сверенные по даташиту в 2.17.17/2.17.18:
# до этого пакет их не знал, хотя vpp126 их перечислял, - то есть в одной
# реализации карта с такой микросхемой открывалась, а в другой нет.
section("chips.json: у Intel CUI команда по адресу (0,0), сверенные 12-вольтовые строки на месте")

bad_intel = []
for where, table in (("монолит", D.GEOMETRY), ("пакет", C.CHIP_GEOMETRY)):
    for name, geo_row in table.items():
        if (geo_row.get("protocol") or "jedec") != "intel":
            continue
        if tuple(geo_row.get("cmd") or ()) != (0, 0):
            bad_intel.append("%s/%s: %s"
                             % (where, name, tuple(geo_row.get("cmd") or ())))
check("у всех строк Intel CUI команда по адресу (0,0)", not bad_intel,
      str(bad_intel[:3]))

bad_rows = []
for name in ("D28F010", "D28F020", "M28F008"):
    mono_geo = D.GEOMETRY[name]
    pkg_geo = C.CHIP_GEOMETRY.get(name) or {}
    if not pkg_geo:
        bad_rows.append(name + ": нет в chips.json")
        continue
    for key in ("size", "sector", "volts", "protocol"):
        if mono_geo.get(key) != pkg_geo.get(key):
            bad_rows.append("%s: %s %r/%r"
                            % (name, key, mono_geo.get(key), pkg_geo.get(key)))
    if bool(mono_geo.get("ro")) != bool(pkg_geo.get("ro")):
        bad_rows.append("%s: ro %s/%s"
                        % (name, mono_geo.get("ro"), pkg_geo.get("ro")))
    if (mono_geo.get("why") or "") != (pkg_geo.get("why") or ""):
        bad_rows.append("%s: why %r/%r" % (name, mono_geo.get("why"),
                                           pkg_geo.get("why")))
check("D28F010/020 и M28F008 есть в обеих таблицах и совпадают (ro и why тоже)",
      not bad_rows, str(bad_rows[:3]))

from pcmcia_flash import chip_db as _cdb                            # noqa: E402
from pcmcia_flash import nor_flash as _pkg_nor                      # noqa: E402

geo_pkg_ro = C.CHIP_GEOMETRY["D28F010"]
pkg_ro_flash = _pkg_nor.NorFlash(_SilentBus(), chips=1,
                                 sector=geo_pkg_ro["sector"], name="D28F010",
                                 protocol=geo_pkg_ro["protocol"],
                                 ro=bool(geo_pkg_ro.get("ro")),
                                 ro_why=str(geo_pkg_ro.get("why") or ""))
ans_pkg = [pkg_ro_flash.program_byte(0, 0x5A), pkg_ro_flash.erase_sector(0),
           pkg_ro_flash.erase_chip()]
check("D28F010 (пакет): запись/сектор/чип - отказ со словами даташита",
      all(not ok and "не выполняется" in why and "12 В" in why
          for ok, why in ans_pkg), str(ans_pkg[0][1][:70]))

try:
    _cdb._build_geometry({"X": {"size_kib": 128, "sector_kib": 128,
                                "cmd_addr1": "0x0", "cmd_addr2": "0x0",
                                "volts": 12, "manuf": "Intel", "ro": True}})
    no_why = ""
except _cdb.DirectError as exc:
    no_why = str(exc)
check("chips.json: ro без why не загружается (отказ обязан быть объяснён)",
      "why" in no_why, no_why[:70])

# --- 27. Обратная синхронизация: имена только из пакета ----------------------
# 2.17.22. До этой правки четыре имени знал только пакет: 28F016SV,
# Am29F016D, MX29F016, MX29F040. То есть карту с такой микросхемой можно было
# открыть из пакета и нельзя из монолита - та же рассинхронизация, что в
# 2.17.21 в другую сторону. Три строки добавлены по цитатам:
#   Am29F016D - AMD 21444E: «32 uniform sectors of 64 Kbytes each», 5.0 V
#               only, в ревизии D есть Unlock Bypass (поэтому bypass=True);
#   MX29F016  - Macronix P/N PM0590 ред. 1.4: «Sector Erase: 32 equal sector
#               with of 64KByte each», питание 5 В;
#   MX29F040  - Macronix MX29F040: «8 equal sectors of 64K-Byte each», 5.0 V.
# 28F016SV добавлен с пометкой «не сверено»: ёмкость и CUI есть в даташите
# E28F016SV-070, а цитаты про «32 блока по 64 КиБ» найти не удалось.
section("обратная синхронизация: в пакете не осталось имён, которых нет в монолите")

only_pkg = sorted(set(C.CHIP_GEOMETRY) - set(D.GEOMETRY))
check("ни одно имя не встречается только в пакете", not only_pkg,
      str(only_pkg[:5]))

cited = {"Am29F016D": "21444E", "MX29F016": "PM0590", "MX29F040": "MX29F040"}
bad_src = [n for n, doc in cited.items()
           if n not in D.GEOMETRY or doc not in str(D.GEOMETRY[n].get("src") or "")]
check("Am29F016D, MX29F016, MX29F040 в монолите называют документ",
      not bad_src, str(bad_src))

bad_new = []
new_geo = {"Am29F016D": (2 << 20, 64 << 10, 5, True),
           "MX29F016": (2 << 20, 64 << 10, 5, False),
           "MX29F040": (512 << 10, 64 << 10, 5, False)}
for name, (size, sector, volts, bypass) in new_geo.items():
    g = D.GEOMETRY.get(name) or {}
    p = C.CHIP_GEOMETRY.get(name) or {}
    got = (g.get("size"), g.get("sector"), g.get("volts"), bool(g.get("bypass")))
    got_p = (p.get("size"), p.get("sector"), p.get("volts"), bool(p.get("bypass")))
    if got != (size, sector, volts, bypass) or got_p != got:
        bad_new.append("%s: %s / %s" % (name, got, got_p))
check("три новые строки: ёмкость, сектор 64 КиБ, 5 В, bypass - как в даташитах",
      not bad_new, str(bad_new))

sv_mono = D.GEOMETRY.get("28F016SV") or {}
sv_pkg = C.CHIP_GEOMETRY.get("28F016SV") or {}
check("28F016SV: в ОБЕИХ таблицах пометка «не сверено», числа совпадают",
      bool(sv_mono.get("unverified")) and bool(sv_pkg.get("unverified"))
      and sv_mono.get("size") == sv_pkg.get("size")
      and sv_mono.get("sector") == sv_pkg.get("sector"),
      (sv_mono.get("unverified"), sv_pkg.get("unverified")))

# Списки Vpp нужны, чтобы по паспорту карты решить, подавать ли 12 В.
# 3-вольтовым строкам Vpp не нужен вовсе, поэтому проверяем ровно те, кому он
# важен: 12 В обязаны быть в VPP126, 5 В - в VPP5. До 2.17.22 пять строк
# (28F008SA, 28F016SA и три Intel S5) не были ни в одном списке.
missing_12 = sorted(n for n, g in D.GEOMETRY.items()
                    if g.get("volts") == 12 and n not in D.VPP126)
missing_5 = sorted(n for n, g in D.GEOMETRY.items()
                   if g.get("volts") == 5 and n not in D.VPP5)
check("каждая 12-вольтовая строка есть в VPP126", not missing_12,
      str(missing_12[:4]))
check("каждая 5-вольтовая строка есть в VPP5", not missing_5, str(missing_5[:4]))

# --- 28. Пакет: отказы и предупреждения как в монолите -----------------------
# 2.17.23. Пять 12-вольтовых строк (28F010, 28F020, 28F256, 28F512, M28F102)
# знал только монолит: в пакете их не было, и карта с такой микросхемой там
# просто не открывалась. Перенесены вместе с ro и той же формулировкой why.
# Заодно закрыты две дырки пакета:
#   * `ambiguous` не переносилось в геометрию, поэтому отказ «ревизия не
#     указана» (boot-чипы с раскладкой 16K+2x8K+32K+64K) в пакете не работал;
#   * `unverified` переносилось, но нигде не выводилось - пакет молчал там,
#     где монолит печатает «параметры не сверены с даташитом».
section("пакет: 12-вольтовые ro-строки, отказ по ревизии и предупреждение о несверенных данных")

ro_names = ("28F010", "28F020", "28F256", "28F512", "M28F102")
bad_ro = []
for name in ro_names:
    mono_geo = D.GEOMETRY[name]
    pkg_geo = C.CHIP_GEOMETRY.get(name) or {}
    if not pkg_geo:
        bad_ro.append(name + ": нет в chips.json")
        continue
    for key in ("size", "sector", "volts"):
        if mono_geo.get(key) != pkg_geo.get(key):
            bad_ro.append("%s: %s %r/%r" % (name, key, mono_geo.get(key),
                                            pkg_geo.get(key)))
    # protocol: пустое значение в монолите означает JEDEC (так же, как в
    # общей сверке двух таблиц).
    if (mono_geo.get("protocol") or "jedec") != (pkg_geo.get("protocol") or "jedec"):
        bad_ro.append("%s: protocol %r/%r" % (name, mono_geo.get("protocol"),
                                              pkg_geo.get("protocol")))
    if not pkg_geo.get("ro") or bool(pkg_geo.get("ro")) != bool(mono_geo.get("ro")):
        bad_ro.append(name + ": ro")
    if (pkg_geo.get("why") or "") != (mono_geo.get("why") or ""):
        bad_ro.append(name + ": why")
    if not pkg_geo.get("ambiguous"):
        bad_ro.append(name + ": потеряно ambiguous")
check("пять 12-вольтовых ro-строк есть в обеих таблицах и совпадают",
      not bad_ro, str(bad_ro[:3]))

bad_refuse = []
for name in ro_names:
    try:
        C.geometry_for(name)
        bad_refuse.append(name + ": открылся")
    except C.UnsupportedChip as exc:
        if (D.GEOMETRY[name].get("why") or "") not in str(exc):
            bad_refuse.append("%s: другая причина: %s" % (name, str(exc)[:40]))
    except Exception as exc:                                  # noqa: BLE001
        bad_refuse.append("%s: %r" % (name, exc))
check("пакет отказывает по имени и цитирует ту же причину, что монолит",
      not bad_refuse, str(bad_refuse[:3]))

# ambiguous без why: раньше пакет пропускал такую строку молча.
try:
    C.check_sector_map({"name": "ТестовыйBoot", "size": 1 << 20,
                        "sector": 64 << 10, "ambiguous": True})
    rev_text = ""
except C.UnsupportedChip as exc:
    rev_text = str(exc)
check("пакет: строка с ambiguous без why отвергается словами про ревизию",
      "ревизия не указана" in rev_text and "--chip" in rev_text, rev_text[:70])

# unverified обязан попасть в отчёт о карте.
_geo_unv = C.geometry_for("Am29PL320")
_parts_unv = {"geo": _geo_unv, "chips": 1, "size": _geo_unv["size"],
              "how": "тест",
              "win": {"start": 0xC0000, "size": 4 << 20},
              "bus": _types.SimpleNamespace(span=8192)}
_buf_unv = _io.StringIO()
with _ctx.redirect_stdout(_buf_unv):
    _pcli._flash_report(_parts_unv)
check("пакет: в отчёте есть предупреждение «параметры не сверены»",
      "не сверены с даташитом" in _buf_unv.getvalue(),
      _buf_unv.getvalue()[-200:])

# --- 29. «Документа не нашли» - не то же, что «такого имени нет» -------------
# 2.17.24. У трёх строк (M28F002, M28F004, M5M29F400) не было своего why, и
# отказ был общим: «ревизия не указана, укажите Am29F800BB для bottom-boot» -
# для ST и Mitsubishi это просто неверно. Документа на сами имена найти не
# удалось, поэтому причина теперь так и звучит: «документа на имя ... не
# нашли» + как эта ёмкость называется у вендора. Утверждать «такого имени
# нет» мы не можем - отсутствие документа не доказывает отсутствие детали.
# Заодно убран мертвый дубль: M28F016S5 лежал И в GEOMETRY, И в WRONG_NAMES,
# а подсказка проверяется раньше таблицы - строка была недостижима, но
# попадала в --list и в сверку таблиц.
section("пакет и монолит: «документа не нашли», а не «такого имени нет»")

dup_mono = sorted(set(D.GEOMETRY) & set(D.WRONG_NAMES))
dup_pkg = sorted(set(C.CHIP_GEOMETRY) & set(C.WRONG_NAMES))
check("ни одно имя не лежит одновременно в таблице и в списке «нет в даташитах»",
      not dup_mono and not dup_pkg, (dup_mono[:3], dup_pkg[:3]))

# Подсказка обязана либо называть строку, которая в таблице есть (тогда
# человек может сразу указать её в --chip), либо честно говорить, что такой
# части в наборе нет. Обещать «вот правильное имя» и падать на нём нельзя.
NOT_MODELLED = "в наборе"
bad_hint = []
for name, hint in sorted(D.WRONG_NAMES.items()):
    tokens = {tok.upper() for tok in _re.findall(r"[A-Za-z0-9]+", hint)}
    known = {c.upper() for c in D.GEOMETRY}
    if not (tokens & known) and NOT_MODELLED not in hint.lower():
        bad_hint.append(name)
check("подсказка называет строку таблицы или говорит, что части в наборе нет",
      not bad_hint, str(bad_hint))

bad_msg = []
for name in ("M28F002", "M28F004", "M5M29F400"):
    for where, fn, err in (("монолит", lambda n: D.check_sector_map(
                                D.geometry_for(n)), D.DirectError),
                           ("пакет", C.geometry_for, C.UnsupportedChip)):
        try:
            fn(name)
            bad_msg.append("%s/%s: открылся" % (name, where))
        except Exception as exc:                            # noqa: BLE001
            text = str(exc)
            if "документа на имя" not in text or "Укажите" not in text:
                bad_msg.append("%s/%s: %s" % (name, where, text[:60]))
check("три имени без документа: отказ обеих реализаций говорит «документа не нашли»",
      not bad_msg, str(bad_msg[:2]))

check("M28F016S5 больше не строка таблицы, а подсказка",
      "M28F016S5" not in D.GEOMETRY and "M28F016S5" in D.WRONG_NAMES
      and "M28F016S5" not in C.CHIP_GEOMETRY)

# --- 30. «Ревизия не указана»: отказ называет СВОИ ревизии ------------------
# 2.17.25. Девять строк (Am29F200/400/800, Am29LV001/160/400/800, MBM29F002/004)
# отказывали общей фразой с примером «например, Am29F800BB для bottom-boot» -
# для Am29F200 или M5M29F020 это чужое имя. Теперь у каждой строки свой why с
# её собственными ревизиями, и строки перенесены в базу пакета, поэтому отказ
# обеих реализаций дословно одинаков.
section("«ревизия не указана»: отказ называет ревизии этой микросхемы")

REV_PAIRS = {
    "Am29F200": ("Am29F200AB", "Am29F200AT"),
    "Am29F400": ("Am29F400AB", "Am29F400AT"),
    "Am29F800": ("Am29F800BB", "Am29F800BT"),
    "Am29LV001": ("Am29LV001BB", "Am29LV001BT"),
    "Am29LV160": ("Am29LV160DB", "Am29LV160DT"),
    "Am29LV400": ("Am29LV400BB", "Am29LV400BT"),
    "Am29LV800": ("Am29LV800BB", "Am29LV800BT"),
    "MBM29F002": ("MBM29F002TC", "MBM29F002BC"),
    "MBM29F004": ("MBM29F004TC", "MBM29F004BC"),
}

bad_rev = []
for name, (a, b) in sorted(REV_PAIRS.items()):
    for where, fn in (("монолит", lambda n: D.check_sector_map(
                          D.geometry_for(n))), ("пакет", C.geometry_for)):
        try:
            fn(name)
            bad_rev.append("%s/%s: открылся" % (name, where))
            continue
        except Exception as exc:                            # noqa: BLE001
            text = str(exc)
        if a not in text or b not in text:
            bad_rev.append("%s/%s: не названы свои ревизии" % (name, where))
        # чужое имя в отказе сбивает с толку: у Am29F200 не должно быть Am29F800
        foreign = [o for o in REV_PAIRS if o != name
                   and not name.startswith(o) and o in text]
        if foreign:
            bad_rev.append("%s/%s: упомянуто чужое %s" % (name, where, foreign[:1]))
check("девять boot-имён: отказ обеих реализаций называет свои T/B-ревизии",
      not bad_rev, str(bad_rev[:3]))

# Обещание «Укажите --chip X» должно быть выполнимым: X есть в таблице.
bad_promise = []
for name, geo in sorted(D.GEOMETRY.items()):
    why = (geo.get("why") or "")
    if not why.startswith("ревизия не указана"):
        continue
    named = {t for t in _re.findall(r"[A-Za-z0-9]+", why)
             if t in D.GEOMETRY and t != name}
    if len(named) < 2:
        bad_promise.append((name, sorted(named)))
check("«ревизия не указана» называет не меньше двух имён, которые есть в таблице",
      not bad_promise, str(bad_promise[:3]))

# и те же девять строк теперь есть в базе пакета - с тем же why
bad_sync = [n for n in REV_PAIRS
            if (C.CHIP_GEOMETRY.get(n) or {}).get("why") != D.GEOMETRY[n]["why"]
            or not (C.CHIP_GEOMETRY.get(n) or {}).get("ambiguous")]
check("девять строк перенесены в пакет с тем же why и ambiguous",
      not bad_sync, str(bad_sync))

# --- 31. AT29C: причина не в ревизии, а в отсутствии команды 30h ------------
# 2.17.26. Четыре строки AT29C (страничная запись с автостиранием) и 28F320
# отказывали текстом «ревизия не указана... 16K + 2x8K + 32K + 64K» - то есть
# про boot-блоки, которых у AT29C нет вовсе. Настоящая причина другая, и она
# теперь сказана словами даташита: команды Sector Erase 30h у AT29C нет,
# стирание - только всей микросхемы 6-байтовым кодом, запись идёт целой
# страницей (AT29C512/AT29C010A - 128 байт, AT29C020/AT29C040A - 256).
# Заодно: bypass=True у AT29C означал бы Unlock Bypass 20h, которого там нет.
section("AT29C и 28F320: причина названа по даташиту, а не «ревизия»")

AT29C_PAGE = {"AT29C512": "128 байт", "AT29C010": "128 байт",
              "AT29C020": "256 байт", "AT29C040": "256 байт"}
bad_at = []
for name, page in sorted(AT29C_PAGE.items()):
    for where, fn in (("монолит", lambda n: D.check_sector_map(
                          D.geometry_for(n))), ("пакет", C.geometry_for)):
        try:
            fn(name)
            bad_at.append("%s/%s: открылся" % (name, where))
            continue
        except Exception as exc:                            # noqa: BLE001
            text = str(exc)
        if "30h" not in text or page not in text:
            bad_at.append("%s/%s: нет 30h или размера страницы" % (name, where))
        if "16K + 2x8K" in text:
            bad_at.append("%s/%s: всё ещё текст про boot-блоки" % (name, where))
    mono_bp = D.GEOMETRY[name].get("bypass")
    pkg_bp = (C.CHIP_GEOMETRY.get(name) or {}).get("bypass")
    if mono_bp or pkg_bp:
        bad_at.append("%s: bypass=%s/%s (Unlock Bypass 20h у AT29C нет)"
                      % (name, mono_bp, pkg_bp))
check("AT29C*: отказ про 30h и размер страницы, bypass выключен в обеих таблицах",
      not bad_at, str(bad_at[:3]))

bad_320 = []
for where, fn in (("монолит", lambda: D.check_sector_map(D.geometry_for("28F320"))),
                  ("пакет", lambda: C.geometry_for("28F320"))):
    try:
        fn()
        bad_320.append("%s: открылся" % where)
    except Exception as exc:                                # noqa: BLE001
        text = str(exc)
        if "28F320B3" not in text or "28F320J3" not in text:
            bad_320.append("%s: не названы оба семейства" % where)
check("28F320: обе реализации называют 28F320B3 и 28F320J3", not bad_320,
      str(bad_320))

# Структурно: после 2.17.26 ни одна строка не отказывает ОБЩИМ текстом про
# ревизию - он остался только страховкой в коде.
no_why_mono = sorted(n for n, g in D.GEOMETRY.items()
                     if g.get("ambiguous") and not (g.get("why") or "").strip())
no_why_pkg = sorted(n for n, g in C.CHIP_GEOMETRY.items()
                    if g.get("ambiguous") and not (g.get("why") or "").strip())
check("у каждой ambiguous-строки в обеих таблицах есть свой why",
      not no_why_mono and not no_why_pkg, (no_why_mono[:3], no_why_pkg[:3]))

bad_sync31 = [n for n in list(AT29C_PAGE) + ["28F320"]
              if (C.CHIP_GEOMETRY.get(n) or {}).get("why")
              != (D.GEOMETRY.get(n) or {}).get("why")]
check("AT29C* и 28F320 перенесены в пакет с тем же why", not bad_sync31,
      str(bad_sync31))

# --- 32. Samsung K9F*: NAND - не NOR, и «why» отказывает сам по себе ---------
# 2.17.27. Три строки NAND (K9F1208U0A, K9F1G08U0A, K9F2G08U0A) стояли с ro=True
# и why=«NAND, не поддерживается», но проверка карты срабатывала только на
# ambiguous+why, а ambiguous у них не было - строка МОЛЧА получала однородную
# карту секторов. Плюс у K9F1208U0A были неверны ёмкость и блок: по даташиту
# Samsung (Rev 1.6) это 64M x 8 (64 МиБ), страница (512 + 16) байт, блок
# (16K + 512) байт = 16 КиБ и 4096 блоков, а стояло 128 МиБ и 64 КиБ.
section("Samsung K9F*: NAND не NOR, отказ и верная геометрия по даташиту")

K9F = {"K9F1208U0A": (64, 16), "K9F1G08U0A": (128, 128), "K9F2G08U0A": (256, 128)}
bad_k9 = []
for name, (mib, kib) in sorted(K9F.items()):
    for where, geo in (("монолит", D.GEOMETRY.get(name)),
                       ("пакет", C.CHIP_GEOMETRY.get(name))):
        if not geo:
            bad_k9.append("%s/%s: строки нет" % (name, where))
            continue
        if geo.get("size") != mib << 20 or geo.get("sector") != kib << 10:
            bad_k9.append("%s/%s: %s/%s" % (name, where, geo.get("size"),
                                            geo.get("sector")))
check("K9F1208U0A 64 МиБ/16 КиБ, K9F1G08U0A 128/128, K9F2G08U0A 256/128",
      not bad_k9, str(bad_k9[:3]))

bad_nand = []
for name in sorted(K9F):
    for where, fn in (("монолит", lambda n: D.check_sector_map(
                          D.geometry_for(n))), ("пакет", C.geometry_for)):
        try:
            fn(name)
            bad_nand.append("%s/%s: открылся" % (name, where))
        except Exception as exc:                            # noqa: BLE001
            text = str(exc)
            if "NAND" not in text or "60h" not in text:
                bad_nand.append("%s/%s: без NAND/60h" % (name, where))
check("три NAND-строки отказывают в обеих реализациях словами про NAND и 60h",
      not bad_nand, str(bad_nand[:3]))

# Структурно: «why» - самостоятельная причина отказа, ambiguous для неё не нужен.
opens_with_why = []
for where, table, fn in (("монолит", D.GEOMETRY,
                          lambda g: D.check_sector_map(D.geometry_for(g["name"]))),
                         ("пакет", C.CHIP_GEOMETRY,
                          lambda g: C.check_sector_map(dict(g, name=g["name"])))):
    for name, geo in sorted(table.items()):
        if not (geo.get("why") or "").strip():
            continue
        if geo.get("sectors"):
            opens_with_why.append("%s/%s: есть и sectors, и why" % (name, where))
            continue
        try:
            fn(dict(geo, name=name))
            opens_with_why.append("%s/%s: открылся" % (name, where))
        except Exception:                                   # noqa: BLE001
            pass
check("строка с why не открывается ни в одной из таблиц, даже без ambiguous",
      not opens_with_why, str(opens_with_why[:3]))

bad_sync32 = [n for n in K9F
              if (C.CHIP_GEOMETRY.get(n) or {}).get("why")
              != (D.GEOMETRY.get(n) or {}).get("why")]
check("три NAND-строки перенесены в пакет с тем же why", not bad_sync32,
      str(bad_sync32))

# --- итог ---------------------------------------------------------------------
print("\n" + "=" * 70)
print("ИТОГО: прошло %d, провалено %d" % (len(PASS), len(FAIL)))
if FAIL:
    for name, detail in FAIL:
        print("  FAIL: " + name + ("   " + str(detail) if detail else ""))
    sys.exit(1)
print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
