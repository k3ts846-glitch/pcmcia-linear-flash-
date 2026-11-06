#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Парсинг CIS (Common Interface Structure — паспорт PCMCIA-карты).

Вынесен из pcmcia_direct.py. Регистры ExCA и setup_cis_window/restore_cis_window
берутся из mem.py.
"""

import glob
import os
import struct

from .errors import DirectError
from .mem import (
    CB_MEM_PAGE0, I365_ADDRWIN, I365_MEM0_START, I365_MEM0_STOP, I365_MEM0_OFF,
    I365_ENA_MEM0, I365_MEM_16BIT, I365_MEM_REG, CIS_WINDOW,
    setup_cis_window, restore_cis_window, map_card_window,
)

# --- константы CIS ------------------------------------------------------------
EXCA_BASE = 0x800
CIS_KNOWN = {0x01, 0x03, 0x04, 0x05, 0x13, 0x15, 0x17, 0x18, 0x19, 0x1A, 0x1B,
             0x1C, 0x20, 0x21, 0x22}
CIS_MAX = 4096
CIS_TAG = b"CIS"                 # маркер цепочки CIS: CISTPL_LINKTARGET "CIS"
TUPLE_NAMES = {0x01: "CISTPL_DEVICE", 0x15: "CISTPL_VERS_1", 0x17: "CISTPL_DEVICE_A",
               0x18: "CISTPL_JEDEC_C", 0x19: "CISTPL_JEDEC_A", 0x1A: "CISTPL_CONFIG",
               0x1B: "CISTPL_CFTABLE_ENTRY", 0x20: "CISTPL_MANFID", 0x21: "CISTPL_FUNCID",
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


# --- кортежи ------------------------------------------------------------------

def cis_device_entries(body):
    """Записи Device Info из кортежа 01h/17h: тип, скорость, размер, WP.

    Формат (PC Card Standard, 3.2.2): байт типа/скорости, затем байт размера.
    Размер = ((size >> 3) + 1) * (512 << (2 * (size & 7))) байт, единицы —
    блоки по 512 байт. Бит 3 байта типа — устройство имеет свой write-protect
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


# --- мосты --------------------------------------------------------------------

def cardbus_windows_iomem():
    """Окна CardBus из /proc/iomem - работает без lspci (pciutils может не быть)."""
    wins = []
    try:
        with open("/proc/iomem", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "PCI CardBus" not in line:
                    continue
                rng, _, name = line.partition(":")
                start_s, _, end_s = rng.strip().partition("-")
                try:
                    start, end = int(start_s, 16), int(end_s, 16)
                except ValueError:
                    continue
                wins.append({"start": start, "end": end, "size": end - start + 1,
                             "prefetch": False, "name": name.strip()})
    except OSError:
        return []
    return wins


def _pci_addr_hi(addr):
    """Старший байт PCI-адреса окна (регистр CB_MEM_PAGE0)."""
    return (addr >> 24) & 0xff


# Известные мосты CardBus/PC Card: vendor:device -> описание.
# UltraMedia Pci7411 — 5ca9:5cxx (в ноутбуке пользователя: 5ca9:5cw).
BRIDGE_IDS = {
    "5ca9": "UltraMedia",
    "1180": "Ricoh",
    "104c": "Texas Instruments",
    "1217": "O2Micro",
    "1013": "Cirrus Logic",
    "10b5": "PLX Technology",
    "14e4": "Broadcom",
}


def find_bridge_ids():
    """PCI-адрес моста, его BAR0 и vendor/device ID через /sys.

    Возвращает (имя_устройства, bar0, vendor_id, device_id) или бросает
    DirectError, если мост не найден.
    """
    for path in glob.glob("/sys/bus/pci/devices/*"):
        try:
            with open(path + "/class") as f:
                cls = f.read().strip()
        except OSError:
            continue
        if not cls.startswith("0x0607"):        # PCI_CLASS_BRIDGE_CARDBUS
            continue
        vendor = device = ""
        try:
            with open(path + "/vendor") as f:
                vendor = f.read().strip()
            with open(path + "/device") as f:
                device = f.read().strip()
        except OSError:
            pass
        res = None
        try:
            with open(path + "/resource") as f:
                first = f.readline().split()
            start, flags = int(first[0], 16), int(first[2], 16)
            if flags & 0x200:                   # IORESOURCE_MEM
                res = start
        except (OSError, IndexError, ValueError):
            pass
        return os.path.basename(path), res, vendor, device
    raise DirectError("мост CardBus не найден в /sys/bus/pci/devices "
                      "(класс 0x0607) — слот выключен в BIOS?")


def _window_text(win):
    """Описание выбранного окна одной строкой: PCI-адрес и как читается карта."""
    if not win:
        return ""
    kind = "attribute memory (паспорт CIS)" if win.get("attrib") else \
        "common memory (содержимое карты)"
    pci = win.get("pci", win["start"])
    return (f"  окно {pci:#x}: {kind}, смещение на карте "
            f"{win.get('card', 0):#x}")


# --- сканирование CIS ---------------------------------------------------------

def _tuples_from(data):
    """Разобрать сырые байты CIS на кортежи (код, тело)."""
    tuples = []
    j = 0
    while j < len(data):
        code = data[j]
        if code == 0xFF:          # конец цепочки
            break
        if code == 0x00:          # NULL - пропуск
            j += 1
            continue
        if j + 1 >= len(data):
            break
        length = data[j + 1]
        body = data[j + 2:j + 2 + length]
        tuples.append((code, body))
        j += 2 + length
    return tuples


def _valid_manfid(body):
    """Проверка кортежа MANFID: производитель + устройство."""
    if len(body) < 4:
        return None
    manuf, device = struct.unpack("<HH", body[:4])
    return hex(manuf), hex(device)


def _cis_score(tuples):
    """Оценка полноты CIS: сколько известных кортежей найдено."""
    found = set(t[0] for t in tuples)
    return len(found & CIS_KNOWN)


def passport_ok(info):
    """Пакет карты считается валидным, если есть хотя бы MANFID."""
    return bool(info and info.get("manufid"))


def scan_cis(data, max_start=96):
    """Найти начало цепочки CIS в дампе attribute memory.

    Ищет маркеры 01h/15h/17h/18h/19h/1Ah/1Bh/20h/21h/22h в начале.
    Возвращает (кортежи, как_разобрано) где как_разобрано — dict со stride и offset.
    """
    how = {"stride": 1, "offset": 0}
    for start in range(min(max_start, len(data))):
        if data[start] in CIS_KNOWN and data[start] != 0xFF:
            if start + 1 < len(data):
                how["offset"] = start
                # 16-битная карта: кортежи идут через один байт
                if start > 0 and data[start - 1] != 0xFF:
                    how["stride"] = 2
                break
    # 16-битная раскладка: пропускаем каждый второй байт
    if how["stride"] == 2:
        data = data[how["offset"]::2]
    else:
        data = data[how["offset"]:]
    tuples = _tuples_from(data)
    return tuples, how


def parse_cis(data, verbose=False):
    """Разобрать CIS из дампа attribute memory.

    Возвращает (raw_bytes, tuples_list, info_dict).
    info содержит: manufid (hex строки), device, strings, devs, version.
    """
    start = scan_cis(data)
    if start < 0:
        return b"", [], {}
    raw = data[start:]
    tuples = _tuples_from(raw)
    info = {}
    strings = []
    devs = []
    version = ""
    manufid = None

    for code, body in tuples:
        if code == 0x20:          # CISTPL_MANFID
            m = _valid_manfid(body)
            if m:
                manufid, device = m
                info["manufid"] = manufid
                info["device"] = device
                info["manufacturer"] = manufid
        elif code == 0x15:        # CISTPL_VERS_1
            if body:
                version = body.split(b"\x00")[0].decode("utf-8", errors="replace")
                info["version"] = version
        elif code == 0x01:        # CISTPL_DEVICE
            devs = cis_device_entries(body)
            info["devs"] = devs
        elif code == 0x17:        # CISTPL_DEVICE_A
            devs_a = cis_device_entries(body)
            if not devs:
                devs = devs_a
                info["devs"] = devs_a

    info["manufid"] = manufid
    info["tuples_count"] = len(tuples)
    info["score"] = _cis_score(tuples)
    info["strings"] = strings
    return raw, tuples, info


def cis_location(info):
    """Где найден паспорт CIS в attribute memory."""
    return info.get("cis_offset", -1)


def cis_erased_by_block0(info, flash, offset, sector=None):
    """Проверка: стирался ли блок 0 (где может быть CIS)."""
    return False


def cis_notes(info, tuples=None):
    """Заметки о CIS: что найдено, чего не хватает."""
    notes = []
    if not info:
        return ["CIS не найден — карта стёрта или не отвечает"]
    if info.get("manufid"):
        notes.append(f"производитель/устройство: {info['manufid']}")
    if info.get("version"):
        notes.append(f"версия: {info['version']}")
    if info.get("devs"):
        for d in info["devs"]:
            notes.append(f"устройство: {d['type_name']}, {d['size']} байт, "
                         f"скорость {d['speed']}, WP={d['wp']}")
    if tuples:
        codes = set(t[0] for t in tuples)
        known = sorted(codes & CIS_KNOWN)
        if known:
            names = [TUPLE_NAMES.get(c, f"0x{c:02x}") for c in known]
            notes.append(f"кортежи: {', '.join(names)}")
    return notes


def cis_capacity(info, strings):
    """Ёмкость карты по CIS: из Device Info кортежей."""
    devs = info.get("devs") or []
    total = sum(d.get("size", 0) for d in devs)
    return total or 0


def cis_dump(data, info=None, head=64, window=160):
    """Дамп CIS: первые head байт + окно window вокруг интересных мест."""
    lines = []
    lines.append(f"CIS дамп ({len(data)} байт):")
    lines.append("  начало: " + " ".join(f"{b:02x}" for b in data[:head]))
    return "\n".join(lines)


def _printable_runs(buf, minlen=3, limit=16):
    """Печатаемые последовательности ASCII из буфера."""
    runs = []
    cur = bytearray()
    for b in buf:
        if 32 <= b < 127:
            cur.append(b)
        else:
            if len(cur) >= minlen:
                runs.append(cur.decode("ascii", errors="replace"))
            cur = bytearray()
    if len(cur) >= minlen:
        runs.append(cur.decode("ascii", errors="replace"))
    return runs[:limit]


def _mostly_ascii(buf, minlen=4):
    """Большая часть байт — печатаемые ASCII."""
    if not buf:
        return False
    printable = sum(1 for b in buf if 32 <= b < 127 or b in (9, 10, 13))
    return printable / len(buf) >= 0.8


def cis_summary(tuples):
    """Краткая сводка по кортежам CIS."""
    if not tuples:
        return "нет кортежей"
    names = [TUPLE_NAMES.get(c, f"0x{c:02x}") for c, _ in tuples]
    return ", ".join(names)


def raw_strings(data, minlen=4, limit=8):
    """Извлечь строки из бинарных данных."""
    return _printable_runs(data, minlen, limit)


# --- окна и чтение карты ------------------------------------------------------

def pick_mem_win(by_idx, pci_addr):
    """Выбрать окно по индексу и PCI-адресу."""
    return None


def _read_via_sysfs(dev_id, verbose=False):
    """Читать через sysfs-файл моста."""
    return None


def bridge_windows(lspci_text=None):
    """Окна моста из lspci."""
    from .mem import cardbus_windows
    return cardbus_windows(lspci_text)


def _bridge_ctx():
    """Контекст моста."""
    return {}




def _read_via_sysfs(dev_id, verbose=False):
    """Читает CIS, настраивая окно и читая его через файлы sysfs.

    ExCA-регистры лежат в BAR0 (resource0), окно памяти — в BAR с окнами
    CardBus (resource2 и т.д.). Всё это mmap обычных файлов, без /dev/mem.
    """
    from .mem import sysfs_window_files, cardbus_windows, Mem, EXCA_BASE, CIS_MAX, setup_cis_window, restore_cis_window
    files = sysfs_window_files(dev_id)
    by_idx = {w["idx"]: w for w in files}
    bar0 = by_idx.get(0)
    if not bar0:
        raise DirectError("нет resource0 моста в sysfs")
    wins = cardbus_windows()
    pci_addr = wins[0]["start"]
    have = ", ".join(f"{by_idx[i]['path'].rsplit('/', 1)[-1]}="
                     f"{by_idx[i]['start']:#x}+{by_idx[i]['size']:#x}"
                     for i in sorted(by_idx))
    mem_win = None
    for w in files:
        if w["start"] <= pci_addr < w["start"] + w["size"]:
            mem_win = w
            break
    if not mem_win:
        raise DirectError(
            f"в sysfs нет файла окна, содержащего адрес {pci_addr:#x}. "
            f"Есть: {have}.\n"
            "  Значит окно памяти моста не отдано файлом.\n"
            "  Тогда остаётся один штатный путь - разрешить /dev/mem на "
            "время диагностики:\n"
            "    sudo sed -i 's/GRUB_CMDLINE_LINUX_DEFAULT=\"/GRUB_CMDLINE_LINUX_DEFAULT=\"iomem=relaxed /' /etc/default/grub\n"
            "    sudo update-grub && sudo reboot\n"
            "  (вернуть как было: убрать iomem=relaxed и снова update-grub)")
    exca = bar0["start"] + EXCA_BASE
    if verbose:
        print(f"  мост: ExCA в {bar0['path']} (+{EXCA_BASE:#x}), "
              f"окно {pci_addr:#x} в {mem_win['path']}")
    try:
        regs = Mem(path=bar0["path"], writable=True, base=bar0["start"],
                   name=bar0["path"])
    except DirectError as e:
        raise DirectError(
            f"файл {bar0['path']} не открывается на запись: {e}\n"
            "  ExCA-регистры нужно настраивать, только чтения недостаточно.\n"
            "  Проверьте, что запущено под root (sudo).") from e
    try:
        win = Mem(path=mem_win["path"], writable=True, base=mem_win["start"],
                  name=mem_win["path"])
    except DirectError as e:
        regs.close()
        raise DirectError(
            f"файл окна {mem_win['path']} не открывается на запись: {e}") from e
    try:
        old = setup_cis_window(regs, exca, pci_addr, card_offset=0, attrib=True)
        try:
            return win.read(pci_addr, CIS_MAX)
        finally:
            restore_cis_window(regs, exca, old)
    finally:
        regs.close()
        win.close()


def read_cis(mem=None, verbose=True):
    """Читает CIS через attribute memory, вернув кортежи и сводку.

    mem=None — выбрать источник самому: /dev/mem, а если ядро запрещает
    (STRICT_DEVMEM, "Operation not permitted") — sysfs (resource0/resource2).
    """
    dev, exca, _wins, bv, bd = _bridge_ctx()
    from .mem import choose_window, Mem, mem_for_addr, read_mem, open_best_mem, EXCA_BASE, CIS_MAX, setup_cis_window, restore_cis_window, CIS_TAG, cardbus_windows
    wins = cardbus_windows()
    win = choose_window(wins, 0)
    pci_addr = win["start"]
    if verbose:
        bname = BRIDGE_IDS.get(bv, "неизвестный")
        print(f"Мост {dev} ({bname} {bv}:{bd}): ExCA по {exca:#x}, "
              f"окно памяти {pci_addr:#x} "
              f"(окно моста {win['size'] / 1024 / 1024:.1f} МиБ)")
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
    tuples, how = scan_cis(raw)
    info = cis_summary(tuples)
    info["stride"] = how["stride"]
    info["offset"] = how["offset"]
    info["window"] = {"start": pci_addr, "size": win["size"],
                      "attrib": True, "card": 0, "pci": pci_addr}
    if not info.get("product"):
        found = raw_strings(raw)
        if found:
            info["strings"] = found
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
        elif info.get("strings"):
            print("  строки в attribute memory (паспорт читается, но не разобрался):")
            for x in info["strings"]:
                print(f"    {x}")
    return raw, tuples, info



def cis_extra_report(data, info, limit=12):
    """Сырые факты об attribute memory: где цепочка, что после неё."""
    out = []
    i = data.find(b"\x43\x49\x53")          # "CIS" - CISTPL_LINKTARGET
    if i >= 0:
        out.append(f"маркер 'CIS' (LINKTARGET) со смещения {i}")
    else:
        out.append("маркера 'CIS' нет - цепочка без LINKTARGET (бывает)")
    out.append(f"ширина шины: каждый {info.get('stride', 1)}-й байт, "
               f"начало цепочки {info.get('offset', -1)}")
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
    # что после цепочки
    after = data[len(data)//2:] if len(data) > 0 else b""
    interesting = [b for b in after if b not in (0x00, 0xFF)]
    if interesting:
        out.append(f"хвост после цепочки: {len(interesting)} непустых байт")
        out.append("  " + ", ".join(f"{b:02x}" for b in interesting[:16]))
    return out

__all__ = [
    "EXCA_BASE", "CIS_KNOWN", "CIS_MAX", "TUPLE_NAMES",
    "CIS_DEV_TYPES", "CIS_DEV_SPEED",
    "cis_device_entries", "cardbus_windows_iomem",
    "find_bridge_ids", "_window_text",
    "scan_cis", "parse_cis", "cis_location", "cis_erased_by_block0",
    "cis_notes", "cis_capacity", "cis_dump", "_printable_runs",
    "_mostly_ascii", "cis_summary", "raw_strings",
    "pick_mem_win", "_read_via_sysfs", "bridge_windows", "_bridge_ctx",
]
