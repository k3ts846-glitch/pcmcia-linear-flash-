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
  * у boot-микросхем (16K+8K+8K+32K+64K) работа запрещена, а не «примерно»;
  * если микросхема не знает Unlock Bypass (20h), запись уходит на A0h.

Запуск: python3 tests/test_flash_regressions.py   (карта и /dev/mem не нужны)
"""
import os
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

# --- 5. Пакет src/pcmcia_flash: те же правила, чистая логика -------------------
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

refused = False
try:
    C.geometry_for("Am29F400AT")
except C.UnsupportedChip:
    refused = True
check("boot-чип (16K+8K+8K+32K+7x64K): отказ, а не стирание «по 64 КиБ»",
      refused)
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
bad = [n for n, g in C.CHIP_GEOMETRY.items()
       if g["uniform"] and g["size"] % g["sector"]]
check("у всех однородных чипов ёмкость кратна сектору", not bad, str(bad))

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

# --- итог ---------------------------------------------------------------------
print("\n" + "=" * 70)
print("ИТОГО: прошло %d, провалено %d" % (len(PASS), len(FAIL)))
if FAIL:
    for name, detail in FAIL:
        print("  FAIL: " + name + ("   " + str(detail) if detail else ""))
    sys.exit(1)
print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
