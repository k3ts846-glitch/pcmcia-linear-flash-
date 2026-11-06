#!/usr/bin/env python3
"""
pcmcia_direct.py — прямой доступ к 16-битной PCMCIA linear flash карте через /dev/mem.

Зачем: когда драйвер PCMCIA не может отдать карте окно памяти, ядро пишет
    cs: warning: no high memory space available!
и карта не появляется ни как /dev/mtd0, ни как устройство в /sys/bus/pcmcia.
mtd-utils, force_size, vpp в этом случае бесполезны — до них дело не доходит.

Но окна CardBus-моста при этом уже назначены ядром:
    pci 0000:00:0c.0: CardBus bridge window 1 [mem 0x64000000-0x67ffffff pref]: assigned
Эти адреса физические. Если открыть /dev/mem и отобразить их через mmap,
карта читается напрямую, без участия подсистемы PCMCIA. Именно так читают CIS
на таких ноутбуках под DOS/Linux-утилитами.

Ограничения (важно понимать до запуска):
  * только ЧТЕНИЕ. Запись сюда сделана бы не будет: без драйвера некому
    выставить Vpp (напряжение программирования) и соблюсти тайминги команд
    микросхемы. Запись «на глазок» портит карту.
  * нужен root и /dev/mem с разрешением на эти адреса: CONFIG_STRICT_DEVMEM=N
    (или ядро с iomem=relaxed). Иначе mmap вернёт EPERM/EACCES.
  * если карта читает 8-битные данные на 16-битной шине, байты будут
    дублироваться (0xAA 0xAA); нормализация есть ниже.

Запуск:
    sudo python3 pcmcia_direct.py --probe             # доступно ли вообще
    sudo python3 pcmcia_direct.py                     # окна + CIS + первые 256 Б
    sudo python3 pcmcia_direct.py --read card.bin --double   # образ + сверка
    sudo python3 pcmcia_direct.py --image-report card.bin    # сводка по образу
    sudo python3 pcmcia_direct.py --addr 0x64000000 --len 4096 --hex
    sudo python3 pcmcia_direct.py --probe --json      # машинный вывод

Коды возврата: 0 — успех, 2 — нет доступа (не root / STRICT_DEVMEM / нет окон),
3 — карта не отвечает (все байты 0xFF или все 0x00).
"""

import argparse
import json
import mmap
import os
import sys
import time

CIS_TAG = b"CIS"                 # маркер цепочки CIS: CISTPL_LINKTARGET "CIS"
KIT_VERSION = "2.17.1"          # версия набора: печатается в отчёте и в --selftest
PAGE = 4096                     # страница памяти: mmap требует кратного смещения
WIN_CHUNK = 1024 * 1024          # сколько отображаем за один mmap
#
# Почему 1 МиБ, а не 64 КиБ: на каждый mmap приходится close, и на медленной
# 16-битной карте затраты на это заметны - образ 4 МиБ из 64 кусков шёл
# десятки секунд. При куске 512 КиБ (конец окна окна моста) их 8.
PROGRESS_EVERY = 256 * 1024      # как часто писать проценты (не на каждый кусок)
FF = 0xFF


class DirectError(Exception):
    """Понятная пользователю ошибка с подсказкой, что делать."""


# ------------------------------------------------------------------ окна

def cardbus_windows(lspci_text=None):
    """Окна памяти моста CardBus из lspci -v (или из переданного текста).

    Возвращает список словарей: start, end, size, prefetch.
    Берутся только строки «Memory window N: AAAAAAAA-BBBBBBBB (prefetchable)».
    """
    if lspci_text is not None and os.path.isfile(lspci_text):
        with open(lspci_text, encoding="utf-8", errors="replace") as fh:
            lspci_text = fh.read()
    if lspci_text is None:
        import subprocess

        def lspci(*args):
            try:
                return subprocess.run(["lspci", *args], capture_output=True, text=True,
                                      errors="replace").stdout
            except FileNotFoundError as e:
                raise DirectError(
                    "утилиты lspci нет — поставьте pciutils "
                    "(sudo apt install pciutils, или пункт 1 меню)") from e

        nn = lspci("-nn")
        if not nn.strip():
            raise DirectError("lspci ничего не ответил — запускали без прав? "
                              "Проверьте: lspci -nn | head")
        dev = ""
        for line in nn.splitlines():
            if "cardbus" in line.lower():
                dev = line.split()[0]
                break
        if not dev:
            raise DirectError("мост CardBus не найден в lspci — слот выключен в BIOS "
                              "или контроллер не инициализируется")
        lspci_text = lspci("-v", "-s", dev)
    wins = []
    for line in lspci_text.splitlines():
        low = line.strip()
        if "memory window" not in low.lower():
            continue
        try:
            rng = low.split(":", 1)[1].split()[0]          # AAAAAAAA-BBBBBBBB
            a, b = rng.split("-")
            start, end = int(a, 16), int(b, 16)
        except (IndexError, ValueError):
            continue
        if end <= start:
            continue
        wins.append({"start": start, "end": end, "size": end - start + 1,
                     "prefetch": "prefetch" in low.lower()})
    if not wins:
        raise DirectError("в lspci нет строк «Memory window» — драйвер yenta_socket "
                          "не привязан к мосту (modprobe yenta_socket)")
    return wins


# ------------------------------------------------- окна моста через sysfs
#
# CONFIG_STRICT_DEVMEM не даёт открыть /dev/mem на страницу, которую ядро
# считает RAM. Но у каждого PCI-устройства есть resource0 (BAR0) и resource2..N
# (окна моста) — это отдельные файлы, и под ту же проверку они не попадают.
# Работать с ними проще: у файла своё смещение, физический адрес не нужен.

def sysfs_bridge_dir(dev_id=None):
    """Каталог моста в sysfs: /sys/bus/pci/devices/0000:00:0c.0."""
    import glob
    candidates = []
    for path in glob.glob("/sys/bus/pci/devices/*"):
        try:
            with open(path + "/class") as f:
                cls = f.read().strip()
        except OSError:
            continue
        if cls.startswith("0x0607"):
            candidates.append(path)
    if dev_id:
        for path in candidates:
            if os.path.basename(path) == dev_id:
                return path
    return candidates[0] if candidates else None


def sysfs_window_files(dev_id=None):
    """Список (имя, путь, начало, размер) для BAR0 и окон моста из resource.

    /sys/.../resource: по строке на BAR — начало, конец, флаги.
    Файл resourceN соответствует строке N того же файла.
    """
    d = sysfs_bridge_dir(dev_id)
    if not d:
        return []
    try:
        with open(os.path.join(d, "resource")) as f:
            lines = [line.split() for line in f if line.strip()]
    except OSError:
        return []
    out = []
    for i, parts in enumerate(lines):
        if len(parts) < 3:
            continue
        try:
            start, end, flags = int(parts[0], 16), int(parts[1], 16), int(parts[2], 16)
        except ValueError:
            continue
        if start == 0 or end < start:
            continue
        f = os.path.join(d, f"resource{i}")
        if not os.path.exists(f):
            continue
        out.append({"idx": i, "path": f, "start": start,
                    "size": end - start + 1, "flags": flags})
    return out


def sysfs_bar_for(addr, dev_id=None, verbose=False):
    """Файл resourceN моста, внутри которого лежит физический адрес addr.

    Нужен, когда /dev/mem открывается, но mmap конкретной страницы ядро
    запрещает (STRICT_DEVMEM на ioremap-областях). Тот же физический диапазон
    читается через sysfs-файл BAR, без /dev/mem.
    """
    for w in sysfs_window_files(dev_id):
        if w["idx"] == 0 and not (w["flags"] & 0x200):
            continue
        if w["start"] <= addr < w["start"] + w["size"]:
            if verbose:
                print(f"  адрес {addr:#x} попадает в {w['path']} "
                      f"({w['start']:#x}+{w['size']:#x})")
            return w["path"], w["start"]
    return None, 0


def _mem_for_addr(mem, addr, writable, dev_id=None, verbose=False):
    """Память для страницы с адресом addr: тот же mem либо файл BAR из sysfs.

    Один Mem показывает только один файл (или /dev/mem целиком). Когда /dev/mem
    открылся, но страницу не отдал, или когда ExCA и окно лежат в разных BAR,
    для очередного адреса берётся тот файл, в который этот адрес входит.
    """
    path0 = getattr(mem, "path", "/dev/mem")
    base = getattr(mem, "base", 0)
    covered = (path0 == "/dev/mem") or (base == 0) or (addr >= base and
              addr < base + getattr(mem, "span", 1 << 40))
    if covered:
        return mem
    path, start = sysfs_bar_for(addr, dev_id, verbose=verbose)
    if path and path != path0:
        try:
            return Mem(path=path, writable=writable, base=start, name=path)
        except DirectError as e:
            if verbose:
                print(f"  {path}: {e}")
    return mem


_MEM_CACHE = {}


def _sysfs_mem(path, start, writable=False):
    """Один Mem на файл sysfs (кэш: при чтении образа файл открывается однажды)."""
    key = (path, bool(writable))
    if key not in _MEM_CACHE:
        _MEM_CACHE[key] = Mem(path=path, writable=writable, base=start, name=path)
    return _MEM_CACHE[key]


def mem_for_addr(mem, addr, writable=False, dev_id=None, verbose=False):
    """Память, которой можно достать адрес addr: mem, а если нельзя - файл BAR.

    /dev/mem может открываться, но не отдавать конкретную страницу
    (STRICT_DEVMEM на ioremap-областях - ровно то, что видно в отчёте:
    mmap(0x60000806) = EACCES при активном iomem=relaxed). Тот же физический
    диапазон лежит в /sys/.../resourceN моста и читается без /dev/mem.
    """
    path, start = sysfs_bar_for(addr, dev_id, verbose=verbose)
    if not path:
        return mem
    if getattr(mem, "path", None) == path:
        return mem
    try:
        return _sysfs_mem(path, start, writable)
    except DirectError as e:
        if verbose:
            print(f"  {path}: {e}")
        return mem


_NOTICE = set()


def say_once(text):
    """Печатает сообщение один раз за прогон (чтение образа идёт страницами)."""
    if text not in _NOTICE:
        _NOTICE.add(text)
        print(text)


def read_mem(mem, addr, length, writable=False, dev_id=None, verbose=False):
    """Читает addr..addr+length через mem; при отказе - через sysfs-файл BAR."""
    try:
        return mem.read(addr, length)
    except DirectError as e:
        alt = mem_for_addr(mem, addr, writable=writable, dev_id=dev_id,
                          verbose=verbose)
        if alt is mem:
            raise
        if verbose:
            say_once(f"  {mem.name} не отдал {addr:#x} ({e}); читаю через "
                     f"{alt.name}")
        try:
            return alt.read(addr, length)
        except DirectError as e2:
            raise DirectError(
                f"адрес {addr:#x} не читается ни через {mem.name} ({e}), "
                f"ни через {alt.name} ({e2}). Если это окно памяти, его мог "
                "занять драйвер моста (yenta_socket): тогда карта снимается "
                "только после выгрузки слота или на другой загрузке.") from e2


def open_best_mem(dev_id=None, verbose=False):
    """Открывает память моста: сначала /dev/mem, при отказе — sysfs.

    Возвращает (mem, описания для настройки окна). У /dev/mem mem.base = 0,
    у sysfs-файла mem.base = начало соответствующего BAR.
    """
    errors = []
    try:
        m = Mem(writable=True)
        if verbose:
            print("  источник памяти: /dev/mem")
        return m, "devmem", errors
    except DirectError as e:
        errors.append(f"/dev/mem: {e}")
    wins = list(sysfs_window_files(dev_id))
    bar0 = None
    for w in wins:
        if w["idx"] == 0:
            bar0 = w
            break
    if not bar0:
        raise DirectError("нет ни /dev/mem, ни resource0 моста в sysfs:\n  "
                          + "\n  ".join(errors))
    try:
        m = Mem(path=bar0["path"], writable=True, base=bar0["start"], name=bar0["path"])
    except (DirectError, PermissionError) as e:
        errors.append(f"{bar0['path']}: {e}")
        raise DirectError("не удалось открыть память моста:\n  " + "\n  ".join(errors)) from e
    if verbose:
        print(f"  источник памяти: {bar0['path']} (BAR0 = {bar0['start']:#x})")
    return m, "sysfs", errors


# ------------------------------------------------------------------ /dev/mem

class Mem:
    """mmap окон /dev/mem (или файлов sysfs) с понятными ошибками."""

    base = 0            # физ. адрес, соответствующий смещению 0 (для sysfs — BAR)
    name = "/dev/mem"

    def __init__(self, path="/dev/mem", writable=False, base=0, name=None):
        self.path = path
        self.base = base          # физ. адрес, соответствующий смещению 0 в файле
        self.name = name or path
        if not os.path.exists(path):
            raise DirectError(f"{path} нет — ядро без CONFIG_DEVMEM?")
        if os.geteuid() != 0 and path == "/dev/mem":
            raise DirectError("нужен root: sudo python3 pcmcia_direct.py ...")
        flags = (os.O_RDWR if writable else os.O_RDONLY) | os.O_SYNC
        try:
            self.fd = os.open(path, flags)
        except PermissionError as e:
            raise DirectError(f"{path} не открывается: {e}") from e
        # сколько адресов покрывает этот файл (у /dev/mem - «все»)
        self.span = 1 << 40
        if base:
            try:
                self.span = os.fstat(self.fd).st_size
            except OSError:
                pass

    def _map(self, addr, length, write=False):
        """Отображает физический участок [addr, addr+length).

        mmap для /dev/mem требует, чтобы смещение было кратно странице, а нужный
        адрес часто нет: регистры ExCA моста лежат, например, по 0x60000800.
        Поэтому отображается выровненный вниз участок, а из него берётся кусок.
        """
        # переводим физический адрес в смещение внутри файла
        rel = addr - getattr(self, "base", 0)
        if rel < 0:
            raise DirectError(f"адрес {addr:#x} ниже начала окна {self.base:#x} "
                              f"({getattr(self, 'name', self.path)})")
        base = rel - (rel % PAGE)
        off = rel - base
        size = off + length
        prot = mmap.PROT_READ | (mmap.PROT_WRITE if write else 0)
        try:
            m = mmap.mmap(self.fd, size, mmap.MAP_SHARED, prot, offset=base)
        except OSError as e:
            raise DirectError(
                f"mmap({addr:#x}, {length}) не удался: {e}. Причины по порядку: "
                "CONFIG_STRICT_DEVMEM=y (тогда нужен iomem=relaxed или "
                "окна через sysfs), адрес не назначен мосту, "
                f"или {getattr(self, 'name', self.path)} уже занят.") from e
        return m, off

    def read(self, addr, length):
        """Читает length байт с физического адреса addr (по кускам)."""
        out = bytearray()
        got = 0
        while got < length:
            n = min(WIN_CHUNK, length - got)
            m, off = self._map(addr + got, n)
            try:
                out += m[off:off + n]
            finally:
                m.close()
            got += n
        return bytes(out)

    def write(self, addr, data):
        """Пишет байты по физическому адресу (только при writable=True)."""
        done = 0
        while done < len(data):
            n = min(WIN_CHUNK, len(data) - done)
            m, off = self._map(addr + done, n, write=True)
            try:
                m[off:off + n] = data[done:done + n]
            finally:
                m.close()
            done += n

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass



# --------------------------------------------------- CIS через attribute memory
#
# Common memory (то, что читается по адресу окна) у стёртой карты — 0xFF, и это
# ничего не доказывает: так же выглядит неотвечающая шина. Отличается она
# attribute memory: там лежит CIS (паспорт карты). У живой карты в attribute
# memory всегда есть данные, даже если common memory стёрта под ноль.
#
# Чтобы добраться до неё, нужно настроить окно памяти моста так же, как это
# делает ядро (drivers/pcmcia/yenta_socket.c: yenta_set_mem_map + cistpl.c):
#   окно 0: 4 КиБ, PCI-адрес = первый MemBase окна моста
#   CB_MEM_PAGE(0)          = 0x40  <- старший байт PCI-адреса
#   I365_MEM(0)+START  (0x10) = (PCI-адрес >> 12) & 0x0fff | I365_MEM_16BIT
#   I365_MEM(0)+STOP   (0x12) = (PCI-адрес + 0xfff) >> 12 & 0x0fff
#   I365_MEM(0)+OFF    (0x14) = (смещение на карте) >> 12  | I365_MEM_REG (attr)
#   I365_ADDRWIN       (0x06) |= I365_ENA_MEM(0) = 0x01     <- включить окно
# ExCA-регистры моста живут в PCI-памяти по смещению 0x800 от его BAR0.

EXCA_BASE = 0x800
CB_MEM_PAGE0 = 0x40
I365_ADDRWIN = 0x06
I365_MEM0_START = 0x10
I365_MEM0_STOP = 0x12
I365_MEM0_OFF = 0x14
I365_ENA_MEM0 = 0x01
I365_MEM_16BIT = 0x8000
I365_MEM_REG = 0x4000          # attribute memory вместо common
CIS_WINDOW = 4096
CIS_KNOWN = {0x01, 0x03, 0x04, 0x05, 0x13, 0x15, 0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1C,
             0x20, 0x21, 0x22}              # 4 КиБ — как карта CIS у ядра (map_size)
CIS_MAX = 4096
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
    import glob
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


def setup_cis_window(mem, exca, pci_addr, card_offset=0, attrib=True, win_idx=0):
    """Настраивает окно моста на чтение. Возвращает старое состояние.

    Окно — это «форточка» моста: PCI-адрес (куда смотрит процессор) и адрес на
    карте (что видно в форточку) независимы. Поэтому образ снимается так же,
    как читается CIS: окно 4 КиБ переставляется по карте (card_offset), а
    чтение идёт с одного и того же PCI-адреса. attrib=True — attribute memory
    (паспорт), False — common memory (сама флеш).
    """
    old = {
        "addrwin": mem.read(exca + I365_ADDRWIN, 1),
        "page0": mem.read(exca + CB_MEM_PAGE0, 1),
        "start": mem.read(exca + I365_MEM0_START, 2),
        "stop": mem.read(exca + I365_MEM0_STOP, 2),
        "off": mem.read(exca + I365_MEM0_OFF, 2),
    }
    # 1) выключить окно, прежде чем менять регистры (как в ядре)
    aw = old["addrwin"][0] & ~I365_ENA_MEM0 & 0xff
    mem.write(exca + I365_ADDRWIN, bytes([aw]))
    # 2) старший байт PCI-адреса окна
    mem.write(exca + CB_MEM_PAGE0, bytes([_pci_addr_hi(pci_addr)]))
    # 3) начало: бит 16-битной шины обязателен
    start = ((pci_addr >> 12) & 0x0fff) | I365_MEM_16BIT
    mem.write(exca + I365_MEM0_START, start.to_bytes(2, "little"))
    # 4) конец окна (4 КиБ)
    stop = ((pci_addr + CIS_WINDOW - 1) >> 12) & 0x0fff
    mem.write(exca + I365_MEM0_STOP, stop.to_bytes(2, "little"))
    # 5) смещение на карте + attribute memory
    off = ((card_offset - pci_addr) >> 12) & 0x3fff
    if attrib:
        off |= I365_MEM_REG
    mem.write(exca + I365_MEM0_OFF, off.to_bytes(2, "little"))
    # 6) включить окно
    mem.write(exca + I365_ADDRWIN, bytes([aw | I365_ENA_MEM0]))
    return old


def restore_cis_window(mem, exca, old):
    """Возвращает регистры окна как было (чтобы не оставить мост настроенным)."""
    for reg, val in ((I365_ADDRWIN, old["addrwin"]),
                     (CB_MEM_PAGE0, old["page0"]),
                     (I365_MEM0_START, old["start"]),
                     (I365_MEM0_STOP, old["stop"]),
                     (I365_MEM0_OFF, old["off"])):
        mem.write(exca + reg, val)


def choose_window(win_list, card_size=0):
    """Какое окно моста отдать карте, чтобы покрыть весь её объём.

    Ядро может назначить окна «не по порядку» (первое окажется меньше карты),
    поэтому берём ПЕРВОЕ окно, которое не меньше карты, иначе самое большое.
    """
    if not win_list:
        raise DirectError("нет окон памяти CardBus")
    fitting = [w for w in win_list if w["size"] >= card_size]
    return fitting[0] if fitting else max(win_list, key=lambda w: w["size"])


def map_card_window(mem, exca, win, card_offset=0, attrib=False, size=CIS_WINDOW):
    """Открывает «форточку» размером size на карту со смещения card_offset."""
    pci = win["start"]
    old = setup_cis_window(mem, exca, pci, card_offset=card_offset,
                           attrib=attrib, win_idx=0)
    if size > CIS_WINDOW:
        # окно больше 4 КиБ: начало/конец уже заданы в setup, здесь только
        # досчитываем конец под нужный размер
        stop = ((pci + size - 1) >> 12) & 0x0fff
        mem.write(exca + I365_MEM0_STOP, stop.to_bytes(2, "little"))
    return old


# Сколько байт карты видно в окне за один заход.
#
# До 2.11.0 было 4 КиБ (CIS_WINDOW): на 4 МиБ это 1024 mmap+close на каждый
# проход, 2048 при двойном чтении. Живой прогон так и шёл - 0.40 МиБ/с, 20.9 c
# на 4 МиБ. Регистры START/STOP хранят номер страницы в младших 12 битах,
# поэтому окно до 16 МиБ кодируется без проблем. 64 КиБ - это 16 страниц:
# в 16 раз меньше системных вызовов на тот же объём, и никакого риска для
# моста (окно моста 64 МиБ). Если на каком-то мосте большое окно не сработает,
# есть --span 4096.
MAX_SPAN = 4 << 20          # верхний предел захода: больше не нужно и рискованно
READ_SPAN = MAX_SPAN        # по умолчанию - сразу максимальный заход
#
# Почему сразу максимум: для 4 МиБ карты это ОДИН заход, то есть НИ ОДНОЙ
# перестановки окна внутри прохода. Раньше (2.11.0) заход был 64 КиБ - это уже
# в 16 раз меньше системных вызовов, чем 4 КиБ, но всё равно 64 перестановки
# на проход. Регистры START/STOP хранят номер страницы, окно моста 64 МиБ -
# 4 МиБ кодируется штатно. Если на каком-то мосте большой заход не сработает,
# есть --span 4096: откат без пересборки.
STALL_TIMEOUT = 60          # секунд без нового байта - и чтение останавливается


def window_span(win, span=None):
    """Сколько байт карты читать за заход: span, но не больше окна моста.

    Верхний предел 4 МиБ - не потому что формат регистров не даст больше, а
    потому что заход больше карты всё равно бессмыслен, а ошибка в STOP на
    некоторых мостах обходится дорого. Для 4 МиБ карты это ОДИН заход.
    """
    want = min(span or READ_SPAN, MAX_SPAN)
    return max(CIS_WINDOW, min(want, win["size"]))


def parse_size(text):
    """'16m' / '16M' / '16777216' / 'auto' -> число байт или None для auto."""
    if text is None or str(text).lower() in ("auto", "авто"):
        return None
    t = str(text).strip().lower()
    mult = 1
    if t.endswith("m"):
        t, mult = t[:-1], 1024 * 1024
    elif t.endswith("k"):
        t, mult = t[:-1], 1024
    try:
        return int(t, 0) * mult
    except ValueError as e:
        raise DirectError(f"не понимаю размер {text!r}: пишите 16m, 32m, 1m "
                          "или число байт") from e


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


VENDORS = {0x01: "AMD", 0x04: "Fujitsu", 0x1C: "Atmel", 0x20: "STMicroelectronics",
           0x89: "Intel", 0x98: "Toshiba", 0xC2: "Macronix", 0xEC: "Samsung",
           0x1F: "Hitachi", 0x0B: "Mitsubishi", 0xBF: "SST"}
# JEDEC-идентификаторы AMD, которые встречаются на платах linear flash.
# Значение: (имя микросхемы, ёмкость в Мбит). Коды Device ID из даташитов
# AMD Am29F016/016B/016D (18805 rev. D, 21444 rev. E5) и Fujitsu MBM29F016A.
# ёмкость нужна cis_capacity() для подсчёта числа чипов: всего_Мбит / на_чип_Мбит.
AMD_PARTS = {
    0xAD: ("Am29F016", 16),   # 2 MiB = 16 Mbit, 32 x 64 КиБ, 5 В, CFI только у D-ревизии
    0x58: ("Am29F800", 8),    # 1 MiB = 8 Mbit
    0xAB: ("Am29F400", 32),   # 4 MiB = 32 Mbit
    0xA4: ("Am29F040", 4),    # 512 KiB = 4 Mbit
    0xD5: ("Am29F200", 16),   # 2 MiB = 16 Mbit
    0x01: ("Am29F010", 8),    # 1 MiB = 8 Mbit
    0x34: ("Am29F032/Am29F033", 256),  # 32 MiB = 256 Mbit
    0xBA: ("Am29F080", 64),   # 8 MiB = 64 Mbit (по даташиту серии)
    0xB4: ("Am29LV016", 16),  # 2 MiB = 16 Mbit, 3 В
    0x44: ("MBM29F016", 16),  # Fujitsu, аналог Am29F016, 2 MiB = 16 Mbit
}
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
          "M28F008", "M28F002", "M28F004", "M28F016", "M28F102"}
VPP5 = {"Am29F016", "Am29F800", "Am29F400", "Am29F040", "Am29F200", "Am29F010",
        "Am29F032/Am29F033", "Am29F080", "Am29LV016", "MBM29F016",
        "HN28F010", "HN28F020", "HN28F100",
        "M5M29F010", "M5M29F020", "M5M29F400",
        "MX29F1610", "MX29F3210",
        "SST39VF010", "SST39VF020", "SST39VF040", "SST39VF080",
        "TC58FVT160", "TC58FVT320",
        # AMD/Fujitsu StrataFlash — 5 В или 3 В, VPP не нужен
        "Am29PL320", "Am29PL640", "Am29PL1280",
        "Am29DL322", "Am29DL323", "Am29DL642", "Am29DL643",
        "Am29GL064", "Am29GL128", "Am29GL256", "Am29GL512", "Am29GL1024",
        "MBM29DL322", "MBM29DL642", "MBM29GL064", "MBM29GL128"}
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
        if vid == 0x01 and did in AMD_PARTS:
            name = AMD_PARTS[did][0]
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
            if vid == 0x01 and did in AMD_PARTS:
                part, mbit = AMD_PARTS[did]
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


def pick_mem_win(by_idx, pci_addr):
    """Выбирает файл окна sysfs, в которое попадает адрес окна CardBus.

    Нельзя брать «самый большой MEM-бар»: у моста их несколько (BAR0 с ExCA,
    окна, ROM), и порядок не гарантирован.
    """
    for idx in sorted(by_idx):
        w = by_idx[idx]
        if idx == 0 or not (w["flags"] & 0x200):   # 0x200 = IORESOURCE_MEM
            continue
        if w["start"] <= pci_addr < w["start"] + w["size"]:
            return w
    return None


def _read_via_sysfs(dev_id, verbose=False):
    """Читает CIS, настраивая окно и читая его через файлы sysfs.

    ExCA-регистры лежат в BAR0 (resource0), окно памяти — в BAR с окнами
    CardBus (resource2 и т.д.). Всё это mmap обычных файлов, без /dev/mem.
    """
    files = sysfs_window_files(dev_id)
    by_idx = {w["idx"]: w for w in files}
    bar0 = by_idx.get(0)
    if not bar0:
        raise DirectError("нет resource0 моста в sysfs")
    wins = cardbus_windows()
    pci_addr = wins[0]["start"]
    # окно памяти выбираем по ВХОЖДЕНИЮ адреса окна CardBus: у моста бывает
    # несколько MEM-областей (BAR0 с ExCA, окна, ROM), и «самая большая» -
    # не обязательно нужная.
    mem_win = pick_mem_win(by_idx, pci_addr)
    if not mem_win:
        have = ", ".join(f"{by_idx[i]['path'].rsplit('/', 1)[-1]}="
                         f"{by_idx[i]['start']:#x}+{by_idx[i]['size']:#x}"
                         for i in sorted(by_idx))
        raise DirectError(
            "в sysfs нет файла окна, содержащего адрес "
            f"{pci_addr:#x}. Есть: {have}.\n"
            "  Значит окно памяти моста не отдано файлом (ядро 6.1 создаёт "
            "resourceN не для всех окон моста).\n"
            "  Тогда остаётся один штатный путь - разрешить /dev/mem на "
            "время диагностики:\n"
            "    sudo sed -i 's/GRUB_CMDLINE_LINUX_DEFAULT=\"/"
            "GRUB_CMDLINE_LINUX_DEFAULT=\"iomem=relaxed /' /etc/default/grub\n"
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
            "  Проверьте, что запущено под root (sudo). Если и под root "
            "отказ -\n  остаётся iomem=relaxed для /dev/mem (см. подсказку "
            "в конце отчёта).") from e
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


def bridge_windows(lspci_text=None):
    """Окна моста: lspci, а если pciutils нет - /proc/iomem. None = ошибка.

    Так набор работает и на голой системе: на живом ноутбуке lspci был, но
    в минимальной установке его может не быть, а окна CardBus видны в
    /proc/iomem всегда.
    """
    try:
        wins = cardbus_windows(lspci_text)
    except DirectError as e:
        if lspci_text is not None:          # явно переданный файл: ошибку не глотаем
            print(f"ОШИБКА: {e}", file=sys.stderr)
            return None
        wins = cardbus_windows_iomem()
        if not wins:
            print(f"ОШИБКА: {e}\n  и в /proc/iomem строк 'PCI CardBus' тоже нет",
                  file=sys.stderr)
            return None
    return wins


def _bridge_ctx():
    """Мост, ExCA-регистры и окна памяти: общее для чтения CIS и образа.

    Возвращает (имя_устройства, exca_addr, wins, vendor_id, device_id).
    """
    dev, bar0, vendor, device = find_bridge_ids()
    if not bar0:
        raise DirectError("у моста нет назначенного региона памяти (BAR0) - "
                          "окна CardBus не назначены, читать ExCA-регистры негде")
    wins = bridge_windows()
    if not wins:
        raise DirectError("окна памяти CardBus не найдены ни через lspci, "
                          "ни в /proc/iomem")
    return dev, bar0 + EXCA_BASE, wins, vendor, device


# ------------------------------------------------ управление питанием сокета ---

# Смещения в PCI конфигурационном пространстве моста (функция 0):
PCI_CONFIG_GENERAL_CONTROL = 0x86   # General Control Register, бит 10 = 12V_SW_SEL
PCI_CONFIG_SERIAL_BUS_DATA = 0xB0   # Serial Bus Data
PCI_CONFIG_SERIAL_BUS_INDEX = 0xB1  # Serial Bus Index (адрес байта)
PCI_CONFIG_SERIAL_BUS_SLAVE_ADDR = 0xB2  # Serial Bus Slave Address + R/W бит
PCI_CONFIG_SERIAL_BUS_CONTROL = 0xB3   # Serial Bus Control / Status

# ExCA регистры питания (относительно ExCA_BASE = BAR0 + 0x800):
EXCA_POWER_CONTROL_A = 0x02   # Card A: ExCA +02h
EXCA_POWER_CONTROL_B = 0x42   # Card B: ExCA +42h

# Биты ExCA Power Control:
EXCA_COE = 0x80       # бит 7: Card Output Enable
EXCA_AUTOPWRSWEN = 0x20  # бит 5: Auto Power Switch Enable
EXCA_CAPWREN = 0x10    # бит 4: VCC Enable
EXCA_EXCAVPP_MASK = 0x03   # биты 1-0: VPP (00=no conn, 01=VCC, 10=12V)

# Биты Socket Control Register (CardBus Socket Base Address + 0x10h):
SOC_VPPCTRL_MASK = 0x07   # биты 2-0: VPP (000=off, 001=12V, 010=5V, 011=3.3V)
SOC_VPPCTRL_OFF = 0x00
SOC_VPPCTRL_12V = 0x01
SOC_VPPCTRL_5V = 0x02
SOC_VPPCTRL_3V3 = 0x03
SOC_VCCCTRL_MASK = 0x70   # биты 6-4: VCC (000=off, 010=5V, 011=3.3V)
SOC_VCCCTRL_OFF = 0x00
SOC_VCCCTRL_5V = 0x20
SOC_VCCCTRL_3V3 = 0x30


def pci_config_path(dev):
    """Путь к конфигурационному пространству PCI-устройства в sysfs."""
    return f"/sys/bus/pci/devices/{dev}/config"


def pci_config_read(dev, offset, length=1):
    """Прочитать length байт из PCI конфигурационного пространства.

    Требует root (чтение /sys/bus/pci/devices/*/config).
    """
    try:
        with open(pci_config_path(dev), "rb") as f:
            f.seek(offset)
            return f.read(length)
    except OSError as error:
        raise DirectError(
            f"не удалось прочитать PCI конфиг {dev} офсет {offset:#x}: {error}"
        ) from error


def pci_config_write(dev, offset, value, length=1):
    """Записать value (int) в PCI конфигурационное пространство.

    length байт, младший байт первым (little-endian). Требует root.
    """
    try:
        data = value.to_bytes(length, "little")
        with open(pci_config_path(dev), "r+b") as f:
            f.seek(offset)
            f.write(data)
    except OSError as error:
        raise DirectError(
            f"не удалось записать PCI конфиг {dev} офсет {offset:#x}: {error}"
        ) from error


def socket_base_address(dev):
    """Базовый адрес регистров сокета из PCI конфига (смещение 0x10).

    Для CardBus-мостов PCI7x21/PCI7x11 здесь адрес базы регистров сокета.
    """
    data = pci_config_read(dev, 0x10, 4)
    return int.from_bytes(data, "little") & ~0x3  # убрать биты типа региона


def socket_control_read(dev):
    """Прочитать Socket Control Register (Socket Base + 0x10h).

    Читает через /dev/mem (физический адрес). Возвращает (vpp_bits, vcc_bits)
    или None при ошибке.
    """
    try:
        base = socket_base_address(dev)
        if not base:
            return None
        page = base & ~0xFFF
        offset = base & 0xFFF
        mem_fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        try:
            mm = mmap.mmap(mem_fd, 4096, mmap.MAP_SHARED,
                           mmap.PROT_READ | mmap.PROT_WRITE,
                           offset=page)
            reg_val = int.from_bytes(mm[offset:offset + 4], "little")
        finally:
            mm.close()
            os.close(mem_fd)
        vpp = reg_val & SOC_VPPCTRL_MASK
        vcc = (reg_val & SOC_VCCCTRL_MASK) >> 4
        return vpp, vcc
    except (OSError, ValueError, mmap.error, OverflowError):
        return None


def socket_control_write(dev, vpp=None, vcc=None):
    """Записать Socket Control Register.

    vpp: 0=off, 1=12V, 2=5V, 3=3.3V
    vcc: 0=off, 2=5V, 3=3.3V
    Возвращает True при успехе.
    """
    try:
        base = socket_base_address(dev)
        if not base:
            return False
        current = socket_control_read(dev)
        if current is None:
            return False
        vpp_cur, vcc_cur = current
        if vpp is not None:
            vpp_cur = vpp & SOC_VPPCTRL_MASK
        if vcc is not None:
            vcc_cur = vcc & 0x07
        reg_val = (vcc_cur << 4) | vpp_cur
        page = base & ~0xFFF
        offset = base & 0xFFF
        mem_fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        try:
            mm = mmap.mmap(mem_fd, 4096, mmap.MAP_SHARED,
                           mmap.PROT_READ | mmap.PROT_WRITE,
                           offset=page)
            mm[offset:offset + 4] = reg_val.to_bytes(4, "little")
            mm.flush()
        finally:
            mm.close()
            os.close(mem_fd)
        return True
    except (OSError, ValueError, mmap.error, OverflowError):
        return False


def exca_power_control_read(exca_mem, exca_addr, card_a=True):
    """Прочитать ExCA Power Control Register.

    card_a=True — смещение 02h (Card A), False — 42h (Card B).
    Возвращает байт регистра или None.
    """
    offset = EXCA_POWER_CONTROL_A if card_a else EXCA_POWER_CONTROL_B
    try:
        data = exca_mem.read(exca_addr + offset, 1)
        return data[0]
    except (DirectError, OSError, IndexError, TypeError):
        return None


def exca_power_control_write(exca_mem, exca_addr, value, card_a=True):
    """Записать ExCA Power Control Register.

    value: новые биты (только биты 7,5,4,1,0 изменяются, остальные сохраняются).
    """
    offset = EXCA_POWER_CONTROL_A if card_a else EXCA_POWER_CONTROL_B
    try:
        current = exca_mem.read(exca_addr + offset, 1)[0]
        new_val = (current & 0x04) | (value & 0xFB)  # бит 2 не трогаем
        exca_mem.write(exca_addr + offset, bytes([new_val]))
        return True
    except (DirectError, OSError, IndexError, TypeError):
        return False


def tps2220b_serial_write(dev, index, data_byte, slave_addr=0x00):
    """Записать через последовательную шину питания TPS2220B.

    Использует регистры Serial Bus (PCI конфиг B0h-B3h):
    - B0h: Serial Bus Data (данные)
    - B1h: Serial Bus Index (адрес байта в устройстве)
    - B2h: Serial Bus Slave Address (адрес устройства + R/W бит, младший бит=0 — запись)
    - B3h: Serial Bus Control/Status (запуск передачи, статус занятости)

    Протокол TPS2220B: данные передаются старшим битом вперёд по CLOCK,
    защёлка по LATCH. Точная структура серийного слова (какой бит за что
    отвечает) требует сверки с даташитом TPS2220B (TI SLVS554).
    """
    try:
        pci_config_write(dev, PCI_CONFIG_SERIAL_BUS_DATA, data_byte & 0xFF, 1)
        pci_config_write(dev, PCI_CONFIG_SERIAL_BUS_INDEX, index & 0xFF, 1)
        pci_config_write(dev, PCI_CONFIG_SERIAL_BUS_SLAVE_ADDR, slave_addr & 0xFE, 1)
        pci_config_write(dev, PCI_CONFIG_SERIAL_BUS_CONTROL, 0x01, 1)
        return True
    except DirectError:
        return False


def tps2220b_set_vpp(dev, voltage):
    """Установить VPP на TPS2220B через последовательную шину.

    voltage: 0 (off), 3.3, 5, 12 (В).
    Возвращает True при успехе, False при ошибке.

    Структура серийного слова TPS2220B (предположительная, требует проверки
    на железе по даташиту SLVS554):
      D0-D1: VPP (00=0V, 01=3.3V, 10=5V, 11=12V)
      D2-D3: VCC (00=0V, 01=3.3V, 10=5V, 11=Hi-Z)
    """
    vpp_bits = {0: 0b00, 3.3: 0b01, 5: 0b10, 12: 0b11}.get(voltage, 0b00)
    return tps2220b_serial_write(dev, index=0x00, data_byte=vpp_bits)


def vpp_state_names(vpp_bits):
    """Человеческое имя VPP по битам Socket Control Register."""
    return {0: "выключен", 1: "12 В", 2: "5 В", 3: "3,3 В"}.get(vpp_bits, "неизвестно")


def check_vpp_state(dev, exca_mem=None, exca_addr=None, card_a=True):
    """Проверить состояние VPP перед записью.

    Возвращает dict:
        {"vpp": "off"|"12V"|"5V"|"3.3V"|"unknown",
         "vcc": "off"|"5V"|"3.3V"|"unknown",
         "source": "socket_control"|"exca"|"unknown",
         "ok": bool}  — True если VPP включён (любое ненулевое напряжение)
    """
    result = {"vpp": "unknown", "vcc": "unknown",
              "source": "unknown", "ok": False}

    # 1. Через Socket Control Register (PCI7411 +10h)
    soc = socket_control_read(dev)
    if soc is not None:
        vpp_bits, vcc_bits = soc
        result["vpp"] = vpp_state_names(vpp_bits)
        result["vcc"] = vpp_state_names(vcc_bits)
        result["source"] = "socket_control"
        result["ok"] = vpp_bits != 0
        return result

    # 2. Через ExCA Power Control Register (fallback)
    if exca_mem is not None and exca_addr is not None:
        val = exca_power_control_read(exca_mem, exca_addr, card_a=card_a)
        if val is not None:
            exp_vpp = val & EXCA_EXCAVPP_MASK
            vpp_map = {0: "off", 1: "VCC (проброс)", 2: "12V"}
            result["vpp"] = vpp_map.get(exp_vpp, "unknown")
            result["vcc"] = "on" if (val & EXCA_CAPWREN) else "off"
            result["source"] = "exca"
            result["ok"] = exp_vpp != 0
            return result

    return result


def set_vpp_for_write(dev, voltage=5, exca_mem=None, exca_addr=None, card_a=True):
    """Включить VPP перед записью на карту.

    Пытается через Socket Control Register, затем ExCA Power Control,
    затем через последовательную шину TPS2220B.
    Возвращает (ok, сообщение).
    """
    vpp_bits = {3.3: SOC_VPPCTRL_3V3, 5: SOC_VPPCTRL_5V,
                12: SOC_VPPCTRL_12V}.get(voltage, SOC_VPPCTRL_5V)

    # 1. Socket Control Register
    if socket_control_write(dev, vpp=vpp_bits):
        return True, f"VPP {voltage} В включён через Socket Control Register"

    # 2. ExCA Power Control Register
    if exca_mem is not None and exca_addr is not None:
        exp_bits = {3.3: 1, 5: 1, 12: 2}.get(voltage, 1)  # 01=VCC, 10=12V
        exca_val = (exp_bits & 0x03) | EXCA_CAPWREN
        if exca_power_control_write(exca_mem, exca_addr, exca_val, card_a=card_a):
            return True, f"VPP {voltage} В включён через ExCA Power Control"

    # 3. Последовательная шина TPS2220B
    if tps2220b_set_vpp(dev, voltage):
        return True, f"VPP {voltage} В включён через TPS2220B (serial bus)"

    return False, f"не удалось включить VPP {voltage} В"


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


# --------------------------------------------------------- образ карты
#
# ЧТО БЫЛО НЕ ТАК ДО 2.8.0 (нашлось по коду, а не на карте):
# образ читался по PCI-адресу окна напрямую, БЕЗ настройки ExCA. Но CIS
# читается через то же окно, настроенное на attribute memory (I365_MEM_REG).
# Значит «чтение по адресу окна» попадало в attribute memory, а не в саму
# флеш: вместо 4 МиБ данных получилось бы 4 МиБ паспорта, с зеркалами.
# Правильно - переставлять окно по карте, как ядро (yenta_set_mem_map):
# на карте N страниц, в форточку видна одна, чтение идёт с одного адреса.
#
# Сколько читать, решает САМА карта: лишние адресные линии не подключены, или
# карта отражает их зеркально, поэтому адрес повторяется с периодом, равным
# её размеру. Пробуем 512 КиБ, 1, 2, 4, 8, 16, 32 МиБ и берём первый период
# со СТАБИЛЬНЫМ совпадением (два чтения подряд), а не «на глаз».
SIZE_STEPS = (512 << 10, 1 << 20, 2 << 20, 4 << 20, 8 << 20, 16 << 20, 32 << 20)
PROBE_LEN = 4096


def _read_page(mem, exca, win, card_off, length=PROBE_LEN, dev_id=None,
               verbose=False):
    """Читает length байт common memory начиная со смещения card_off.

    Регистры ExCA (BAR0) и само окно памяти - разные области: каждая берётся
    из того файла, куда она попадает. Иначе на STRICT_DEVMEM первое же
    обращение к ExCA даёт EACCES, и «окно не читается» без причины.
    """
    regs = mem_for_addr(mem, exca, writable=True, dev_id=dev_id, verbose=verbose)
    old = map_card_window(regs, exca, win, card_offset=card_off,
                          attrib=False, size=length)
    try:
        # ВАЖНО: окно всегда ставится на границу страницы (card_off & ~0xfff),
        # а внутри страницы читаем со смещением card_off % 4096. Раньше смещение
        # внутри страницы не учитывалось, и чтение по 0x200 (так лежит паспорт
        # у ATA-карт) попадало в НАЧАЛО страницы: вместо паспорта приходили
        # нули. Нашлось при поддержке ATA-карт.
        page_off = card_off & (PAGE - 1)
        data = mem_for_addr(mem, win["start"], writable=False, dev_id=dev_id,
                            verbose=verbose)
        return read_mem(data, win["start"] + page_off, length, dev_id=dev_id,
                        verbose=verbose)
    finally:
        restore_cis_window(regs, exca, old)


# ПАСПОРТНАЯ ЁМКОСТЬ (CISTPL_DEVICE + "32 MEG" в строках) — главная подсказка.
# Зачем: 4 МиБ флеша повторяют адреса через 4 МиБ ТОЛЬКО если карта отражает
# неотключённые линии адреса. На живой Centennial FL32M этого не происходит:
# за концом карты читались нули (шина прижата), повторения не нашлось, и набор
# честно, но бесполезно снял 64 МиБ окна моста за 340 секунд.
TRUST_CIS_MAX = 64 << 20


def cis_card_size(info):
    """Ёмкость карты по паспорту: (байт, откуда) или (None, почему нет).

    Два независимых источника, оба уже разобраны выше:
      * CISTPL_DEVICE: размер каждого устройства считается как в ядре;
      * строка VERS_1 вида "32 MEG FLASH" и JEDEC "всего N шт.".
    """
    devs = (info or {}).get("devices") or []
    total = sum(d.get("size") or 0 for d in devs)
    if total and total <= TRUST_CIS_MAX:
        return total, f"по кортежу CISTPL_DEVICE ({len(devs)} устройств)"
    mbits = (info or {}).get("mbits")
    if mbits:
        return mbits * 1024 * 1024 // 8, f'по паспорту "{mbits} Мбит"'
    return None, "в паспорте нет ёмкости"


def _read_card(mem, exca, win, off, length, dev_id=None, tries=3):
    """Читает участок карты и убеждается, что прочитанное не «плывёт».

    Зачем: в живом логе два прохода двойного чтения шли с разной скоростью -
    0.40 МиБ/с и 0.19 МиБ/с. Это признак нестабильного ответа карты, и
    одиночное чтение такой случай не ловит: ошибка видна только на повторе.
    Здесь участок читается до трёх раз, при расхождении - понятная ошибка.
    """
    first = _read_page(mem, exca, win, off, length, dev_id=dev_id)
    second = _read_page(mem, exca, win, off, length, dev_id=dev_id) if tries > 1 else first
    if second == first:
        return first
    # два чтения разошлись - третье решает, кто прав (правило двух из трёх)
    if tries > 2:
        third = _read_page(mem, exca, win, off, length, dev_id=dev_id)
        if third == first:
            return first
        if third == second:
            return second
    raise DirectError(
        f"карта отвечает НЕСТАБИЛЬНО: участок со смещения {off} "
        f"({length} байт) читается по-разному за {tries} попытки. "
        "Обычные причины: контакты карты или слота, питание. "
        "Образ не сохранён, на карту ничего не записано - повторите "
        "чтение после чистки контактов или с меньшим заходом "
        "(--span 4096).")


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


def card_kind(info, tuples, at_off=None):
    """Что это за карта: (вид, что с ней можно делать).

    Виды: linear-flash, ata, sram, serial, network, unknown. От вида зависит,
    имеет ли смысл снимать образ: у ATA-диска это дамп всего диска, а у
    serial/network - набор регистров, а не данные.
    """
    func = (info or {}).get("func")
    jedec = (info or {}).get("jedec")
    types = {(d or {}).get("type_name", "").upper() for d in (info or {}).get("devices") or []}
    if at_off is not None:
        return "ata", ("паспорт лежит в common memory (смещение "
                       f"{at_off:#x}) - так устроены ATA-карты: "
                       "CompactFlash в переходнике, Microdrive, диски; "
                       "образ с такой карты - это дамп всего диска")
    if func == 1 and ("SRAM" in types or "DRAM" in types):
        return "sram", ("SRAM/DRAM с батареей: образ снимается, но данные "
                        "держатся только пока жива батарея; стирания "
                        "секторами нет, запись - обычная, без команд")
    if func == 1 and ("ROM" in types or "EPROM" in types or "OTPROM" in types):
        return "rom", ("ROM/EPROM/OTPROM: только чтение, записывать нечего")
    if func == 1 and ("FLASH" in types or jedec):
        return "linear-flash", "линейная флеш: образ снимается, запись - по Vpp"
    if func == 1:
        return "memory", "карта памяти: образ снимается как есть"
    if func in (2,):
        return "serial", ("последовательный порт: это НЕ носитель, образ не "
                          "имеет смысла (только регистры)")
    if func in (3,):
        return "parallel", "параллельный порт: не носитель, образ не нужен"
    if func in (6,):
        return "network", ("сетевая карта: не носитель, образ не нужен "
                           "(можно читать CIS и регистры)")
    return "unknown", "вид карты по паспорту не определён"


def window_access_diag(dev, exca, win):
    """Почему attribute memory читается нулями: разбор ДО выводов о карте.

    Живой лог показал важное: при вынутой карте attribute memory читалась
    нулями, а не 0xFF. Для пустого слота это ненормально - шина PC Card без
    карты обычно отдаёт 0xFF (ничем не притянута). Нули означают, что чтение
    не доходит до карты: чаще всего ядро заняло окно своим драйвером
    (yenta_socket) и переставило регистры ExCA, либо /dev/mem отдал не ту
    страницу. Поэтому сначала - факты, потом вывод «карта не отвечает».

    Возвращает (список строк, подозрение: bool).
    """
    out = []
    suspect = False
    try:
        aw_reg = mem_for_addr(dev, exca, writable=True, dev_id=dev)
    except DirectError as e:
        return [f"регистры ExCA недоступны: {e}"], True
    try:
        aw = aw_reg.read(exca + I365_ADDRWIN, 1)[0]
        page = aw_reg.read(exca + CB_MEM_PAGE0, 1)[0]
        start = int.from_bytes(aw_reg.read(exca + I365_MEM0_START, 2), "little")
        stop = int.from_bytes(aw_reg.read(exca + I365_MEM0_STOP, 2), "little")
        off = int.from_bytes(aw_reg.read(exca + I365_MEM0_OFF, 2), "little")
    except DirectError as e:
        return [f"регистры ExCA не читаются: {e}"], True
    out.append(f"  ExCA: ADDRWIN={aw:#04x} (окно 0 {'включено' if aw & 1 else 'ВЫКЛЮЧЕНО'}), "
               f"PAGE0={page:#04x}, START={start:#06x}, STOP={stop:#06x}, OFF={off:#06x}")
    if not (aw & 1):
        out.append("  -> до чтения окно 0 было ВЫКЛЮЧЕНО: значит его переставил")
        out.append("     драйвер моста (yenta_socket) или оно не настраивалось.")
        suspect = True
    # кто ещё держит память моста
    try:
        import glob
        for p in glob.glob("/sys/bus/pci/devices/*/driver"):
            name = os.path.basename(os.path.realpath(p))
            if name in ("yenta_socket", "pcmcia_socket", "pcmcia"):
                out.append(f"  драйвер моста загружен: {name} - он мог занять окно;")
                out.append("     для прямого чтения его выгружают: modprobe -r yenta_socket")
                suspect = True
                break
    except OSError:
        pass
    return out, suspect


def card_window_check(mem, exca, win, dev_id=None):
    """Здоровье окна: (ok, текст). Ловит «пустой слот» и «занятое окно».

    Смысл отдельной проверки: нельзя снимать 4 МиБ, если окно не читается
    вообще или читается мусор - иначе пустой слот выглядит как карта,
    забитая нулями.
    """
    try:
        a = _read_page(mem, exca, win, 0, PROBE_LEN, dev_id=dev_id)
        b = _read_page(mem, exca, win, 0, PROBE_LEN, dev_id=dev_id)
    except DirectError as e:
        return False, f"окно не читается: {e}"
    if a != b:
        return False, ("окно читается НЕСТАБИЛЬНО: два чтения одного и того "
                       "же места различаются - контакты или питание")
    if a == b"\x00" * len(a):
        return True, ("в окне сплошные нули: шина прижата, карта не отвечает "
                      "(пустой слот или карта не контачит)")
    if a == b"\xff" * len(a):
        return True, "в окне сплошные 0xFF: карта не отвечает (пустой слот?)"
    return True, "окно читается, содержимое неоднородное"


def _page_signature(mem, exca, win, dev_id=None):
    """Содержимое первой страницы карты как «подпись» для поиска повторов."""
    return _read_page(mem, exca, win, 0, PROBE_LEN, dev_id=dev_id)


def find_card_size(mem, exca, win, steps=SIZE_STEPS, probe=PROBE_LEN,
                   dev_id=None, verbose=False, expect=None):
    """Размер карты: (размер, как определено).

    Порядок, от самого надёжного к самому грубому:
      1) повторение адресов (работает, если карта зеркалит лишние линии);
      2) если повторения нет, а паспорт говорит ёмкость - берём ПАСПОРТ;
      3) если в начале карты непустые данные, размер не больше первого
         появления нулей/0xFF на страничной границе (конец карты);
      4) иначе - окно моста (и об этом громко предупреждаем).
    Паспорт важнее «на глаз»: 64 МиБ окна - это не карта.
    """
    head = _page_signature(mem, exca, win, dev_id=dev_id)
    blank_head = looks_blank(head, 0xFF) or looks_blank(head, 0x00)
    for period in steps:
        if period < probe:
            continue
        if period >= win["size"]:
            break
        try:
            a = _read_page(mem, exca, win, period, probe, dev_id=dev_id)
            # Если в начале всё 0xFF/0x00, совпадение ничего не доказывает:
            # у стёртой карты и у «пустоты за концом карты» одинаковый вид.
            if blank_head and period != (expect or -1):
                continue
            if a != head:
                continue
            b = _read_page(mem, exca, win, period, probe, dev_id=dev_id)
            if b == head:
                return period, f"адрес повторяется через {period / 1024 / 1024:.1f} МиБ"
        except DirectError:
            continue

    if expect:
        # 2) паспорт: читаем последнюю страницу карты, чтобы убедиться, что
        #    это действительно карта, а не дыра за её концом (0x00/0xFF)
        try:
            tail = _read_page(mem, exca, win, expect - PROBE_LEN, PROBE_LEN,
                              dev_id=dev_id)
        except DirectError:
            tail = None
        if tail is not None and tail == head and not blank_head:
            return expect, ("по паспорту; внутри карты содержимое повторяется, "
                            "то есть карта СВОРАЧИВАЕТ адреса")
        note = ""
        if tail is not None:
            if looks_blank(tail, 0x00):
                note = ("; за концом карты читаются нули - линии адреса не "
                        "подключены, повторения не будет")
            elif looks_blank(tail, 0xFF):
                note = "; за концом карты 0xFF (пустая шина)"
            else:
                note = "; за концом карты другие данные"
        return expect, f"по паспорту ({expect / 1024 / 1024:.0f} МиБ)" + note

    # 3) конец карты по первому «пустому» участку, если начало непустое
    if not blank_head:
        prev = head
        for period in steps:
            if period <= probe or period >= win["size"]:
                continue
            try:
                pg = _read_page(mem, exca, win, period, PROBE_LEN, dev_id=dev_id)
            except DirectError:
                break
            if (looks_blank(pg, 0x00) or looks_blank(pg, 0xFF)) and \
                    looks_blank(prev, 0x00) is False:
                return period, (f"по первому пустому участку ({period / 1024 / 1024:.0f} МиБ): "
                                "дальше за картой ничего нет")
            prev = pg

    # 4) последний случай - окно моста, с честным предупреждением
    return win["size"], (f"повторения не нашли и ёмкости в паспорте нет, "
                         f"беру окно моста {win['size']:#x} - это МОЖЕТ БЫТЬ "
                         f"больше карты")


def read_card_image(mem, exca, win, path, size, double=False, progress=None,
                    dev_id=None, verbose=False, span=None):
    """Снимает образ карты: окно переставляется по карте, читается common memory.

    Возвращает dict: сколько байт, md5, путь. При double=True образ читается
    дважды и сравнивается — нестабильное чтение не выдаётся за готовый образ.
    """
    import hashlib
    tmp = path + ".part"
    step = window_span(win, span)

    last = [0]

    def one_pass(out_path, pass_no=1):
        h = hashlib.md5()
        done = 0
        last[0] = 0
        last_move = time.time()
        with open(out_path, "wb") as f:
            while done < size:
                n = min(step, size - done)
                # первый блок каждого захода читаем со сверкой повтором:
                # если карта поплыла, узнаем это в первые секунды, а не в конце
                if done == 0:
                    blk = _read_card(mem, exca, win, done, n, dev_id=dev_id)
                else:
                    blk = _read_page(mem, exca, win, done, n, dev_id=dev_id)
                f.write(blk)
                h.update(blk)
                done += n
                # стоп по застою: если карта залипла (или мост перестал
                # отвечать), процесс иначе висит навсегда. 60 с без единого
                # нового байта - это уже не «медленно», а поломка.
                if time.time() - last_move > STALL_TIMEOUT:
                    raise DirectError(
                        f"чтение встало: {STALL_TIMEOUT} с без нового байта "
                        f"(остановились на {done} из {size}). Причины: карту "
                        "вынули, контакты, зависший мост. Образ не сохранён.")
                last_move = time.time()
                # проценты - не чаще PROGRESS_EVERY байт: иначе на быстром
                # чтении они печатаются тысячи раз и сами тормозят вывод
                if progress and (done >= size or done - last[0] >= PROGRESS_EVERY):
                    last[0] = done
                    progress(done, size, pass_no)
            f.flush()
            os.fsync(f.fileno())
        return h.hexdigest()

    # защита от второго экземпляра по тому же файлу: два чтения сразу
    # переставляют одно и то же окно моста, и оба получают мусор
    lock_path = path + ".lock"
    lock_fh = None
    try:
        import fcntl
        lock_fh = open(lock_path, "w")
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if lock_fh:
            lock_fh.close()
        raise DirectError(
            f"образ {path} уже снимается другим запуском ({lock_path} занят). "
            "Дождитесь его конца или возьмите другое имя файла.") from e
    except ImportError:
        lock_fh = None
    try:
        md5 = one_pass(tmp, 1)
        if double:
            chk = path + ".chk"
            try:
                md5b = one_pass(chk, 2)
                same = filecmp_equal(tmp, chk)
            finally:
                try:
                    os.remove(chk)
                except OSError:
                    pass
            if not same or md5 != md5b:
                raise DirectError(
                    "два чтения РАЗЛИЧАЮТСЯ — карта или контакты нестабильны, "
                    f"образ не сохранён (md5 {md5} и {md5b})")
        os.replace(tmp, path)
    except BaseException:
        for junk in (tmp, path + ".chk"):
            try:
                os.remove(junk)
            except OSError:
                pass
        raise
    finally:
        if lock_fh:
            try:
                import fcntl
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            lock_fh.close()
            try:
                os.remove(lock_path)
            except OSError:
                pass
    return {"path": path, "size": size, "md5": md5,
            "sha256": file_sha256(path)}


def image_regions(path, page=4096):
    """Карта занятости: одинаковые соседние страницы объединяются в участки.

    Зачем: по процентам «0x00 78.8%, данные 18.4%» нельзя понять, ГДЕ данные.
    Из живого лога карта Centennial дала сплошные нули в начале при данных в
    середине - по одним процентам этого не увидеть. Здесь возвращается список
    участков (начало, длина, вид), а вид считается по содержимому страницы.
    """
    out = []
    prev = None
    size = os.path.getsize(path)
    off = 0

    def kind_of(blk):
        if not blk:
            return "пусто"
        ff = blk.count(0xFF)
        z = blk.count(0x00)
        if ff == len(blk):
            return "0xFF (стёрто)"
        if z == len(blk):
            return "0x00 (нули)"
        if z > len(blk) * 0.9:
            return "почти нули"
        if ff > len(blk) * 0.9:
            return "почти 0xFF"
        return "данные"

    with open(path, "rb") as f:
        while off < size:
            blk = f.read(page)
            if not blk:
                break
            k = kind_of(blk)
            if prev and prev[0] == k:
                prev[1] += len(blk)
            else:
                prev = [k, len(blk), off]
                out.append(prev)
            off += len(blk)
    return out


def image_stats(path):
    """Короткая сводка по образу: сколько 0xFF, 0x00, прочего."""
    ff = z = n = 0
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            n += len(blk)
            ff += blk.count(b"\xff")
            z += blk.count(b"\x00")
    n = n or 1
    return {"bytes": n, "ff": ff, "zero": z, "other": n - ff - z,
            "ff_pct": 100.0 * ff / n, "zero_pct": 100.0 * z / n,
            "other_pct": 100.0 * (n - ff - z) / n}


def file_sha256(path, chunk=1 << 20):
    """sha256 файла. Нужен для настоящих данных, где md5 слабоват."""
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


def trim_image(path, size_wanted, keep_original=True):
    """Обрезает образ до size_wanted байт (остальное - пустая шина за картой).

    Возвращает (путь результата, сколько байт). Исходник не удаляется, если
    keep_original: пересъёмка на новом наборе дешевле, чем потеря старого файла.
    """
    size = os.path.getsize(path)
    if size <= size_wanted:
        return path, size
    if path.lower().endswith(".bin"):
        out = path[:-4] + f"-{size_wanted}.bin"
    else:
        out = path + ".trim"
    with open(path, "rb") as src, open(out, "wb") as dst:
        left = size_wanted
        while left > 0:
            blk = src.read(min(1 << 20, left))
            if not blk:
                break
            dst.write(blk)
            left -= len(blk)
    if not keep_original:
        try:
            os.remove(path)
        except OSError:
            pass
    return out, os.path.getsize(out)


def trim_hint(path, size_wanted):
    """Что делать, если образ больше карты (ошибка 2.8.9 с окном моста)."""
    size = os.path.getsize(path)
    if size <= size_wanted:
        return []
    return [f"  ВНИМАНИЕ: образ {size} байт, а карта по паспорту {size_wanted} байт.",
            f"  Первые {size_wanted} байт - это карта, остальное - пустая шина.",
            f"  Обрезать: head -c {size_wanted} {os.path.basename(path)} > card-карта.bin",
            "  Чище переснять: в наборе 2.9.0+ размер берётся из паспорта."]


def image_report(res, stats, win, how):
    """Строки отчёта о снятом образе (без карты их нечем восстановить)."""
    out = ["", "Образ карты:", _window_text(win)]
    out.append(f"  файл   : {res['path']}")
    out.append(f"  размер : {res['size']} байт "
               f"({res['size'] / 1024 / 1024:.1f} МиБ) — {how}")
    out.append(f"  md5    : {res['md5']}")
    if res.get("sha256"):
        out.append(f"  sha256 : {res['sha256']}")
    out.append(f"  состав : 0xFF {stats['ff_pct']:.1f}%, "
               f"0x00 {stats['zero_pct']:.1f}%, "
               f"данные {stats['other_pct']:.1f}%")
    if stats["other_pct"] < 0.1:
        out.append("  ВНИМАНИЕ: в образе почти нет данных (всё 0xFF/0x00).")
        out.append("    Так выглядит стёртая карта ИЛИ чтение не туда: проверьте,")
        out.append("    что в разделе 7 паспорт читается (карта отвечает).")
    try:
        parts = image_regions(res["path"])
    except OSError:
        parts = []
    if parts:
        out.append("  где что лежит (участки одинаковых страниц склеены):")
        for k, n, off in parts[:12]:
            if n >= 4096:
                out.append(f"    {off:#09x}  {k:<16} {n / 1024:8.1f} КиБ")
        if len(parts) > 12:
            out.append(f"    ... всего участков: {len(parts)}")
    return out

def normalize_bankwidth(data):
    """8-битная карта на 16-битной шине даёт каждый байт дважды.

    Возвращает (данные, ширина): 2 — как есть, 1 — если байты явно дублируются.
    Признак дублирования: чётные байты равны следующим нечётным на всём куске.
    """
    if len(data) < 8:
        return data, 2
    even = data[0::2]
    odd = data[1::2]
    if even[:4096] == odd[:4096] and len(set(even[:1024])) > 1:
        return even, 1
    return data, 2


def looks_blank(data, byte):
    """Всё ли содержимое равно byte (0xFF у стёртой флешки, 0x00 у некоторых SRAM)."""
    return bool(data) and data.count(byte) == len(data)


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


def strings(data, minlen=4, limit=16):
    """Печатаемые строки — по ним видно PRODID/MANFID карты."""
    res, cur = [], bytearray()
    for b in data:
        if 32 <= b < 127:
            cur.append(b)
        else:
            if len(cur) >= minlen:
                res.append(cur.decode("ascii"))
                if len(res) >= limit:
                    return res
            cur = bytearray()
    if len(cur) >= minlen:
        res.append(cur.decode("ascii"))
    return res


def summarize(data):
    """Короткая сводка содержимого окна: пусто / данные / что похоже."""
    n = len(data)
    kinds = set(data)
    res = {"bytes": n,
           "unique_values": len(kinds),
           "empty_ff": looks_blank(data, 0xFF),
           "empty_00": looks_blank(data, 0x00),
           "cis_marks": find_cis(data),
           "strings": strings(data)}
    if res["empty_ff"]:
        res["verdict"] = "всё 0xFF — карта стёрта (или это не карта, а пустое окно)"
    elif res["empty_00"]:
        res["verdict"] = "всё 0x00 — типично для неотвечающей шины или SRAM без питания"
    elif len(kinds) < 8:
        res["verdict"] = f"подозрительно однородное содержимое ({len(kinds)} разных байт)"
    else:
        res["verdict"] = "похоже на настоящие данные"
    return res


# ------------------------------------------------------------------ команды

def devmem_hint():
    """Одна строка: что мешает /dev/mem и что с этим делать."""
    try:
        with open("/proc/cmdline", encoding="utf-8", errors="replace") as f:
            cmdline = f.read()
    except OSError:
        cmdline = ""
    relaxed = "iomem=relaxed" in cmdline
    return ("в cmdline уже есть iomem=relaxed" if relaxed else
            "если mmap запрещён (Operation not permitted), добавьте в параметры "
            "ядра iomem=relaxed (в /etc/default/grub: GRUB_CMDLINE_LINUX, "
            "затем update-grub и перезагрузка)")


def probe(win_list, verbose=True):
    """Можно ли читать карту: /dev/mem (или sysfs) + первые байты каждого окна.

    Окно настраивается на common memory карты (attrib=False) - то есть читается
    САМА флеш, а не паспорт. Сплошные 0xFF здесь - нормальный вид стёртой
    флеш-карты, поэтому вывод говорит об этом прямо.
    """
    report = {"windows": win_list, "readable": None, "reason": "", "samples": [],
              "common_memory": True}
    try:
        mem = Mem()
    except DirectError as e:
        report["reason"] = str(e)
        report["common_memory"] = False
        report["hint"] = devmem_hint()
        if verbose:
            print(f"НЕТ ДОСТУПА: {e}")
            print(f"Подсказка: {report['hint']}")
        return report
    exca = None
    dev = None
    try:
        dev, bar0, _bv, _bd = find_bridge_ids()
        if bar0:
            exca = bar0 + EXCA_BASE
    except DirectError:
        dev = None
    report["common_memory"] = exca is not None
    try:
        ok_any = False
        for w in win_list:
            try:
                if exca is not None:
                    sample = _read_page(mem, exca, w, 0, 256, dev_id=dev,
                                        verbose=verbose)
                else:
                    sample = read_mem(mem, w["start"], 256, dev_id=dev,
                                      verbose=verbose)
            except DirectError as e:
                report["samples"].append({"start": w["start"], "hex": "",
                                          "width": 0, "blank_ff": False,
                                          "error": str(e)})
                continue
            data, width = normalize_bankwidth(sample)
            report["samples"].append({"start": w["start"], "hex": sample[:16].hex(),
                                      "width": width, "blank_ff": looks_blank(data, 0xFF)})
            if not looks_blank(sample, 0xFF):
                ok_any = True
        report["readable"] = True
        report["reason"] = "" if ok_any else ("окна читаются, но везде 0xFF - "
                                              "у стёртой флеш-карты это норма")
    except DirectError as e:
        report["readable"] = False
        report["reason"] = str(e)
    finally:
        mem.close()
    if verbose:
        if report["readable"]:
            print("Доступ к /dev/mem: есть")
            what = ("common memory карты (сама флеш)"
                    if report["common_memory"] else "окно по адресу (без настройки ExCA)")
            print(f"  читается: {what}")
            for smp in report["samples"]:
                if smp.get("error"):
                    print(f"  окно {smp['start']:#010x}: ошибка - {smp['error']}")
                    continue
                print(f"  окно {smp['start']:#010x}: {smp['hex']} "
                      f"(ширина шины {smp['width']})"
                      + ("  [0xFF]" if smp["blank_ff"] else ""))
            good = [s for s in report["samples"] if s.get("width")]
            if good and all(s["blank_ff"] for s in good):
                print("  0xFF по всему окну = карта стёрта или её нет; паспорт при")
                print("  этом читается отдельно (раздел --cis) - решает он, а не это.")
            elif not good and any(s.get("error") for s in report["samples"]):
                print("  НИ ОДНО ОКНО НЕ ПРОЧИТАНО — про содержимое карты здесь "
                      "ничего не сказано,")
                print("  и слова про 0xFF к этому случаю не относятся: выше "
                      "причина отказа по каждому окну.")
                print(f"  Подсказка: {report.get('hint') or devmem_hint()}")

        else:
            print(f"Доступ к /dev/mem: нет — {report['reason']}")
    return report


def filecmp_equal(a, b):
    import filecmp
    return filecmp.cmp(a, b, shallow=False)


# ------------------------------------------------------------------ CLI

# ============================================================================
# ПРЯМАЯ ЗАПИСЬ В LINEAR FLASH БЕЗ ДРАЙВЕРА  (2.14.0)
# ============================================================================
#
# Зачем это здесь. Ядро не даёт этой карте /dev/mtdN («unable to map card
# memory!»), поэтому flashcp/flash_erase писать некуда. Но окно памяти набор
# уже настраивает сам - им же и читается образ. Значит команды микросхемы
# можно выполнять прямо из программы.
#
# Командная последовательность JEDEC (AMD Am29F016 rev. B/D, Intel 28F016SA,
# Fujitsu MBM29F016A - у всех одинаковая):
#
#   сброс/чтение:  F0h по любому адресу внутри чипа; после этого чип снова
#                  отдаёт массив;
#   unlock:        AAh -> 0x555, 55h -> 0x2AA (адреса ВНУТРИ чипа);
#   program:       A0h -> 0x555, затем данные по целевому адресу; ждём по
#                  DQ7 (инверсия записанного бита) и DQ5 (выход за время);
#   erase sector:  80h -> 0x555, AAh/55h -> 0x555/0x2AA, 30h -> адрес сектора;
#   erase chip:    80h -> 0x555, AAh/55h, 10h -> 0x555;
#   готовность:    стирание - DQ7=1 и DQ5=0; при DQ5=1 читаем DQ7 ещё раз:
#                  если и он не тот - это реальный отказ, а не таймаут.
#
# Сектор стирания у этой семьи - 64 КиБ (у 2 МиБ чипа их 32).
#
# Два чипа в паре (у карты 2 x Am29F016 по 2 МиБ): в 16-битном окне байт с
# чётным смещением идёт в один чип, с нечётным - в другой. Адрес команды для
# каждого чипа считается В БАЙТАХ ЭТОГО ЧИПА, то есть для второго чипа - с
# прибавкой 1 к абсолютному адресу. Ровно это делает и ядро (pcmciamtd с
# bankwidth=2: две подкарты по 2 МиБ).
#
# Чего этот код НЕ делает - и говорит об этом прямо:
#   * не выставляет Vpp. Am29F016 его не требует (5.0 V only); если карте
#     12 В всё-таки нужны, стирание/запись провалятся на проверке DQ7/DQ5 и
#     так и будет сказано;
#   * не работает с 8-битными картами (там другая адресация, а проверить
#     нечем - такой карты у нас нет). При bankwidth=1 отказывается заранее;
#   * не трогает байты, которые уже равны записываемым: NOR чистится только
#     1 -> 0, и лишняя запись только изнашивает ячейку;
#   * не пишет в attribute memory и не меняет паспорт (CIS).

NOR_CMD_ADDR1 = 0x555          # адрес первой команды внутри чипа
NOR_CMD_ADDR2 = 0x2AA          # адрес второй команды внутри чипа
NOR_RESET = 0xF0
NOR_PROGRAM = 0xA0
NOR_BYPASS = 0x20          # Unlock Bypass: вход в режим быстрой записи
NOR_ERASE_SETUP = 0x80
NOR_ERASE_SECTOR = 0x30
NOR_ERASE_CHIP = 0x10
NOR_UNLOCK1 = 0xAA
NOR_UNLOCK2 = 0x55
NOR_SECTOR_PROTECT = 0xA6  # чтение состояния защиты групп секторов (Am29F016D)
NOR_ERASE_SUSPEND = 0xB0   # приостановка стирания (Am29F016D)
NOR_ERASE_RESUME = 0x30    # продолжение стирания (共享 с командой сектора)
DQ7 = 0x80
DQ5 = 0x20
# Intel 28F008SA/28F016SA: не DQ7/DQ5, а статусный регистр (документация
# C-ONE Series-II, раздел «Device Status Register Definition»):
#   бит 7 WSMS 1 = готов, 0 = идёт запись/стирание
#   бит 5 ESS  1 = стирание не удалось
#   бит 4 BWS  1 = запись не удалась
#   бит 3 VPPS 1 = Vpp вне допуска (то есть «нет 12 В»)
#   бит 6 / 2..0 - пауза стирания и резерв
SR_READY = 0x80
SR_ERASE_FAIL = 0x20
SR_WRITE_FAIL = 0x10
SR_VPP_LOW = 0x08
# Команды CUI Intel 28F008SA (таблица 9 документа)
INTEL_CUI_RESET = 0xFF
INTEL_CUI_READ_ID = 0x90
INTEL_CUI_READ_SR = 0x70
INTEL_CUI_CLEAR_SR = 0x50
INTEL_CUI_ERASE_SETUP = 0x20
INTEL_CUI_ERASE_CONFIRM = 0xD0
INTEL_CUI_WRITE_SETUP = 0x40
INTEL_CUI_ALT_WRITE = 0x10
INTEL_CUI_SUSPEND = 0xB0
# StrataFlash буферная запись (28F016S5, M28F016S5):
# 25h — Write-to-Buffer Setup, D0h — Buffer Write Confirm.
# Буфер 32 слова (64 байта на чип в 16-битном режиме, 32 байта в 8-битном).
INTEL_BUFFER_SETUP = 0x25
INTEL_BUFFER_CONFIRM = 0xD0
INTEL_BUFFER_SIZE = 32  # слов на чип = 32 байта на чип
# AMD/Fujitsu StrataFlash буферная запись (Am29PL/DL/GL, MBM29DL/GL):
# 25h — Write-to-Buffer Setup, D0h — Buffer Write Confirm.
# Протокол JEDEC (разблокировка AA/55, опрос DQ7/DQ5), не Intel CUI.
AMD_BUFFER_SETUP = 0x25
AMD_BUFFER_CONFIRM = 0xD0
AMD_BUFFER_SIZE = 32  # слов на чип = 32 байта на чип
SECTOR = 64 * 1024             # блок стирания этой семьи микросхем
FLASH_POLLS = 300000           # сколько раз опрашивать статус, прежде чем сдаться


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


def snapshot_window(mem, exca, dev_id=None):
    """Запомнить регистры окна 0, чтобы в конце вернуть их как было."""
    regs = mem_for_addr(mem, exca, writable=True, dev_id=dev_id)
    return {"addrwin": regs.read(exca + I365_ADDRWIN, 1),
            "page0": regs.read(exca + CB_MEM_PAGE0, 1),
            "start": regs.read(exca + I365_MEM0_START, 2),
            "stop": regs.read(exca + I365_MEM0_STOP, 2),
            "off": regs.read(exca + I365_MEM0_OFF, 2)}


def restore_window(mem, exca, old, dev_id=None):
    """Вернуть регистры окна. Ошибки глотаются: это уборка, а не работа."""
    try:
        regs = mem_for_addr(mem, exca, writable=True, dev_id=dev_id)
        restore_cis_window(regs, exca, old)
        return True
    except (DirectError, OSError, KeyError, TypeError):
        return False


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
    "Am29F016": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=2 << 20, volts=5, bypass=True,
                     src="AMD Am29F016 (B/D): 32 x 64 КиБ, 5 В. Программи-"
                         "рование побайтно (A0h, 4 цикла); ускорение - "
                         "Unlock Bypass 20h (2 цикла на байт)"),
    "Am29F032": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=4 << 20, volts=5, bypass=True,
                     src="AMD Am29F032: 64 x 64 КиБ, 5 В. Программи-"
                         "рование побайтно (A0h, 4 цикла); ускорение - "
                         "Unlock Bypass 20h (2 цикла на байт)"),
    "Am29F080": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=1 << 20, volts=5, bypass=True,
                     src="AMD Am29F080: 128 x 64 КиБ, 5 В. Unlock Bypass 20h"),
    "Am29F040": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=512 << 10, volts=5, bypass=True,
                     src="AMD Am29F040: 64 x 64 КиБ, 5 В. Unlock Bypass 20h"),
    "Am29LV016": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                      size=2 << 20, volts=3, src="AMD Am29LV016: 3 В, 32 x 64 КиБ"),
    "Am29LV800": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                      size=1 << 20, volts=3, uniform=False,
                      src="AMD Am29LV800B: 8 Мбит = 1 МиБ, 3 В, boot-сектора "
                          "16K+8K+8K+32K+15x64K (неuniform)"),
    "MBM29F016": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                      size=2 << 20, volts=5, bypass=True,
                      src="Fujitsu MBM29F016A: 5 В. Unlock Bypass 20h"),
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
    "28F008SA": dict(sector=64 << 10, page=1, cmd=(0, 0), wide=True,
                     size=1 << 20, volts=12, protocol="intel",
                     src="Intel 28F008SA: CUI, 16 блоков по 64 КиБ, Vpp 12 В"),
    "28F016SA": dict(sector=64 << 10, page=1, cmd=(0, 0), wide=True,
                     size=2 << 20, volts=12, protocol="intel",
                     src="Intel 28F016SA: CUI, 32 блока по 64 КиБ, Vpp 12 В"),
    "28F016S5": dict(sector=128 << 10, page=1, cmd=(0, 0), wide=True,
                     size=2 << 20, volts=12, protocol="intel",
                     src="Intel 28F016S5: CUI, Vpp 12 В"),
    "28F320": dict(sector=64 << 10, page=1, cmd=(0, 0), wide=True,
                   size=4 << 20, volts=3, protocol="intel",
                   src="Intel 28F320: CUI, 3 В"),
    "M28F102": dict(sector=16 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                    size=256 << 10, volts=12, src="ST M28F102: сектор 16 КиБ"),
    # --- JEDEC-чипы из AMD_PARTS, которых не хватало в таблице геометрии ---
    "Am29F800": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=1 << 20, volts=5, bypass=True, uniform=False,
                     src="AMD Am29F800B: 8 Мбит = 1 МиБ, boot-сектора "
                         "16K+8K+8K+32K+15x64K (неuniform)"),
    "Am29F200": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=256 << 10, volts=5, bypass=True, uniform=False,
                     src="AMD Am29F200A/B: 2 Мбит = 256 КиБ, 5 В, boot-сектора "
                         "16K+8K+8K+32K+3x64K (неuniform)"),
    "Am29F010": dict(sector=16 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=128 << 10, volts=5, bypass=False,
                     src="AMD Am29F010: 8 x 16 КиБ = 128 КиБ, 5 В (таблица "
                         "секторов SA0-SA7 в даташите 21116). Unlock Bypass у "
                         "исходного Am29F010 нет - он появился в поздних "
                         "ревизиях, поэтому пишем обычной командой A0h"),
    "Am29F033": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=4 << 20, volts=5, bypass=True,
                     src="AMD Am29F033: 64 x 64 КиБ, 5 В. Unlock Bypass 20h"),
    # ====================================================================
    # Дополнительные NOR-флеш микросхемы, встречающиеся на PCMCIA-картах
    # (все параметры из даташитов производителей; для чипов без точных
    # сведений указаны безопасные значения по умолчанию — как Am29F016:
    # сектор 64 КиБ, побайтовая запись A0h, JEDEC-протокол, 5.0 В).
    # ====================================================================
    # --- Fujitsu ---
    "MBM29F016A": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                       size=2 << 20, volts=5, bypass=True,
                       src="Fujitsu MBM29F016A: 32 x 64 КиБ, 5 В, аналог Am29F016"),
    "MBM29F033": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                      size=4 << 20, volts=5, bypass=True,
                      src="Fujitsu MBM29F033: 64 x 64 КиБ, 5 В (данные по умолчанию)"),
    "MBM29F002": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=256 << 10, volts=5, uniform=False,
                     src="2 Мбит, 5 В, boot-сектора (неuniform)"),
    "MBM29F004": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=512 << 10, volts=5, uniform=False,
                     src="4 Мбит, 5 В, boot-сектора (неuniform)"),
    # --- ST Microelectronics ---
    "M28F102": dict(sector=16 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                    size=256 << 10, volts=12, src="ST M28F102: сектор 16 КиБ, Vpp 12 В"),
    "M28F002": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=256 << 10, volts=5, uniform=False,
                     src="2 Мбит, 5 В, boot-сектора (неuniform)"),
    "M28F004": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=512 << 10, volts=5, uniform=False,
                     src="4 Мбит, 5 В, boot-сектора (неuniform)"),
    "M28F016": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                    size=2 << 20, volts=12, src="ST M28F016: 2 MiB, Vpp 12 В (данные по умолчанию)"),
    "M28F016S5": dict(sector=128 << 10, page=1, cmd=(0, 0), wide=True,
                      size=2 << 20, volts=12, protocol="intel",
                      src="ST M28F016S5: StrataFlash, сектор 128 КиБ, Vpp 12 В, буферная запись 25h/D0h"),
    # --- Atmel ---
    "AT29C010": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=128 << 10, volts=5, bypass=True,
                     src="Atmel AT29C010: 128 KiB, 5 В (данные по умолчанию)"),
    "AT29C020": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=256 << 10, volts=5, bypass=True,
                     src="Atmel AT29C020: 256 KiB, 5 В (данные по умолчанию)"),
    "AT29C040": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=512 << 10, volts=5, bypass=True,
                     src="Atmel AT29C040: 512 KiB, 5 В (данные по умолчанию)"),
    "AT29C512": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=64 << 10, volts=5, bypass=True,
                     src="Atmel AT29C512: 64 KiB, 5 В (данные по умолчанию)"),
    # --- Toshiba ---
    "TC58FVT160": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                       size=2 << 20, volts=5, bypass=True,
                       src="Toshiba TC58FVT160: 2 MiB, 5 В (данные по умолчанию)"),
    "TC58FVT320": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                       size=4 << 20, volts=5, bypass=True,
                       src="Toshiba TC58FVT320: 4 MiB, 5 В (данные по умолчанию)"),
    # --- Samsung ---
    "K9F1G08U0A": dict(sector=128 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                       size=128 << 20, volts=3.3, bypass=False,
                       src="Samsung K9F1G08U0A: NAND, не поддерживается (доступ только по паспорту)"),
    # --- Macronix ---
    "MX29F1610": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                      size=2 << 20, volts=5, bypass=True,
                      src="Macronix MX29F1610: 2 MiB, 5 В (аналог Am29F016)"),
    "MX29F3210": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                      size=4 << 20, volts=5, bypass=True,
                      src="Macronix MX29F3210: 4 MiB, 5 В (данные по умолчанию)"),
    # --- SST ---
    "SST39VF010": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                       size=128 << 10, volts=5, bypass=True,
                       src="SST SST39VF010: 128 KiB, 5 В (данные по умолчанию)"),
    "SST39VF020": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                       size=256 << 10, volts=5, bypass=True,
                       src="SST SST39VF020: 256 KiB, 5 В (данные по умолчанию)"),
    "SST39VF040": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                       size=512 << 10, volts=5, bypass=True,
                       src="SST SST39VF040: 512 KiB, 5 В (данные по умолчанию)"),
    "SST39VF080": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                       size=1 << 20, volts=5, bypass=True,
                       src="SST SST39VF080: 1 MiB, 5 В (данные по умолчанию)"),
    # --- Intel 28F-серия (CUI-протокол) ---
    # По datasheet: 28F008SA — sixteen 64-Kbyte blocks (1 MByte),
    # 28F016SA — thirty-two 64-KB blocks (2 MByte).
    "28F008SA": dict(sector=64 << 10, page=1, cmd=(0, 0), wide=True,
                     size=1 << 20, volts=12, protocol="intel",
                     src="Intel 28F008SA: CUI, 16 блоков по 64 КиБ, Vpp 12 В"),
    "28F016SA": dict(sector=64 << 10, page=1, cmd=(0, 0), wide=True,
                     size=2 << 20, volts=12, protocol="intel",
                     src="Intel 28F016SA: CUI, 32 блока по 64 КиБ, Vpp 12 В"),
    "28F016S5": dict(sector=128 << 10, page=32, cmd=(0, 0), wide=True,
                     size=2 << 20, volts=12, protocol="intel",
                     src="Intel 28F016S5: CUI StrataFlash, блок 128 КиБ, Vpp 12 В, буферная запись 25h/D0h (32 байта/чип)"),
    "28F320": dict(sector=64 << 10, page=1, cmd=(0, 0), wide=True,
                   size=4 << 20, volts=3, protocol="intel",
                   src="Intel 28F320: CUI, 3 В"),
    "M28F016S5": dict(sector=128 << 10, page=32, cmd=(0, 0), wide=True,
                      size=2 << 20, volts=12, protocol="intel",
                      src="ST M28F016S5: StrataFlash, сектор 128 КиБ, Vpp 12 В, буферная запись 25h/D0h (32 байта/чип)"),
    # --- AMD/Fujitsu StrataFlash (Am29PL/DL/GL, MBM29DL/GL) ---
    # Буферная запись 25h/D0h, JEDEC-протокол, DQ7/DQ5 опрос, 5 В или 3 В.
    "Am29PL320": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                      size=4 << 20, volts=5, bypass=True, strata=True,
                      src="AMD Am29PL320: StrataFlash, буфер 32 байта, 25h/D0h, 5 В"),
    "Am29PL640": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                      size=8 << 20, volts=5, bypass=True, strata=True,
                      src="AMD Am29PL640: StrataFlash, буфер 32 байта, 25h/D0h, 5 В"),
    "Am29PL1280": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                       size=16 << 20, volts=5, bypass=True, strata=True,
                       src="AMD Am29PL1280: StrataFlash, буфер 32 байта, 25h/D0h, 5 В"),
    "Am29DL322": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                      size=4 << 20, volts=5, bypass=True, strata=True,
                      src="AMD Am29DL322: StrataFlash, буфер 32 байта, 25h/D0h, 5 В"),
    "Am29DL323": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                      size=4 << 20, volts=5, bypass=True, strata=True,
                      src="AMD Am29DL323: StrataFlash, буфер 32 байта, 25h/D0h, 5 В"),
    "Am29DL642": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                      size=8 << 20, volts=5, bypass=True, strata=True,
                      src="AMD Am29DL642: StrataFlash, буфер 32 байта, 25h/D0h, 5 В"),
    "Am29DL643": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                      size=8 << 20, volts=5, bypass=True, strata=True,
                      src="AMD Am29DL643: StrataFlash, буфер 32 байта, 25h/D0h, 5 В"),
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
    "MBM29DL322": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                       size=4 << 20, volts=5, bypass=True, strata=True,
                       src="Fujitsu MBM29DL322: StrataFlash, буфер 32 байта, 25h/D0h, 5 В"),
    "MBM29DL642": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                       size=8 << 20, volts=5, bypass=True, strata=True,
                       src="Fujitsu MBM29DL642: StrataFlash, буфер 32 байта, 25h/D0h, 5 В"),
    "MBM29GL064": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                       size=8 << 20, volts=3, bypass=True, strata=True,
                       src="Fujitsu MBM29GL064: StrataFlash, буфер 32 байта, 25h/D0h, 3 В"),
    "MBM29GL128": dict(sector=64 << 10, page=32, cmd=(0x555, 0x2AA), wide=False,
                       size=16 << 20, volts=3, bypass=True, strata=True,
                       src="Fujitsu MBM29GL128: StrataFlash, буфер 32 байта, 25h/D0h, 3 В"),
    "28F256": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                   size=32 << 10, volts=12, src="Intel 28F256: 32 KiB, Vpp 12 В (устаревший)"),
    "28F512": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                   size=64 << 10, volts=12, src="Intel 28F512: 64 KiB, Vpp 12 В (устаревший)"),
    "28F010": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                   size=128 << 10, volts=12, src="Intel 28F010: 128 KiB, Vpp 12 В (устаревший)"),
    "28F020": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                   size=256 << 10, volts=12, src="Intel 28F020: 256 KiB, Vpp 12 В (устаревший)"),
    # ====================================================================
    # Производители Hitachi, Mitsubishi, NEC, Sharp и др.
    # Параметры указаны по даташитам, где известны; иначе — безопасные
    # значения по умолчанию (сектор 64 КиБ, побайтовая запись A0h,
    # JEDEC-протокол, 5.0 В). Запись не реализуется без Vpp 12 В для чипов
    # с volts=12.
    # ====================================================================
    # --- Hitachi ---
    "HN28F010": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=128 << 10, volts=5, bypass=True,
                     src="Hitachi HN28F010: 128 KiB, 5 В, JEDEC (по умолчанию)"),
    "HN28F020": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=256 << 10, volts=5, bypass=True,
                     src="Hitachi HN28F020: 256 KiB, 5 В, JEDEC (по умолчанию)"),
    "HN28F100": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=1 << 20, volts=5, bypass=True,
                     src="Hitachi HN28F100: 1 MiB, 5 В, JEDEC (по умолчанию)"),
    # --- Mitsubishi ---
    "M5M29F010": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                      size=128 << 10, volts=5, bypass=True,
                      src="Mitsubishi M5M29F010: 128 KiB, 5 В, аналог Am29F010"),
    "M5M29F020": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                      size=256 << 10, volts=5, bypass=True,
                      src="Mitsubishi M5M29F020: 256 KiB, 5 В, аналог Am29F020"),
    "M5M29F400": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=512 << 10, volts=5, uniform=False,
                     src="4 Мбит, 5 В, boot-сектора (неuniform)"),
    # --- NEC ---
    "D28F010": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                    size=128 << 10, volts=12, src="NEC D28F010: 128 KiB, Vpp 12 В (по умолчанию)"),
    "D28F020": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                    size=256 << 10, volts=12, src="NEC D28F020: 256 KiB, Vpp 12 В (по умолчанию)"),
    # --- Sharp ---
    "LH28F010": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=128 << 10, volts=12, src="Sharp LH28F010: 128 KiB, Vpp 12 В (по умолчанию)"),
    "LH28F020": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                     size=256 << 10, volts=12, src="Sharp LH28F020: 256 KiB, Vpp 12 В (по умолчанию)"),
    # --- СGS-Thomson / ST (дополнительно к M28F*) ---
    "M28F008": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                    size=1 << 20, volts=12, src="ST M28F008: 1 MiB, Vpp 12 В (по умолчанию)"),
    # --- Samsung (дополнительно к K9*) ---
    "K9F1208U0A": dict(sector=64 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                       size=128 << 20, volts=3.3, bypass=False,
                       src="Samsung K9F1208U0A: NAND, не поддерживается (доступ только по паспорту)"),
    "K9F2G08U0A": dict(sector=128 << 10, page=1, cmd=(0x555, 0x2AA), wide=False,
                       size=256 << 20, volts=3.3, bypass=False,
                       src="Samsung K9F2G08U0A: NAND, не поддерживается (доступ только по паспорту)"),
}

# Intel 28F с командой словом: сброс, автоопределение, CFI у них другие.
INTEL_RESET = 0xFF
INTEL_READ_ID = 0x90
INTEL_READ_CFI = 0x98
AMD_RESET = 0xF0
AMD_READ_ID = 0x90
AMD_READ_CFI = 0x98


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
        if not g.get("uniform", True):
            # Неоднородная карта секторов (boot-блок): 16 КиБ, затем 8+8+32,
            # и только потом 64 КиБ. Стирание «сектором 64 КиБ» по адресу
            # 0x8000 сотрёт сектор 0x0000-0x7FFF вместе с паспортом карты,
            # поэтому здесь честный отказ, а не порча данных.
            raise DirectError(
                "микросхема " + str(g.get("name") or "?") + " имеет "
                "НЕОДНОРОДНУЮ карту секторов (boot-блок: 16K+8K+8K+32K+...+64K). "
                "Набор стирает однородными секторами, поэтому работа с ней "
                "запрещена: первое же стирание задело бы соседние данные. "
                "Нужна поддержка карты секторов (в планах). Укажите --chip с "
                "однородной микросхемой.")
        g.setdefault("name", str(g.get("name") or ""))
        return g

    if not name:
        return base
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
        if vid == 0x01 and did in AMD_PARTS:
            return AMD_PARTS[did][0]
    if vid == 0x89 and did == 0xA2:
        return "28F008SA"
    if vid == 0x89 and did == 0xA1:
        return "28F016SA"
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
               protocol=geo.get("protocol", "jedec"))
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
                       protocol=g2.get("protocol", "jedec"))
        elif auto.get("manuf") is not None and not auto.get("id_documented"):
            # Производитель отвечает, но код устройства недокументирован:
            # не подменяем имя Am29F016 — пишем честно, что определено по CFI.
            res["how"] = ("автоопределение (производитель отвечает, "
                          "код устройства недокументирован)")
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
    if man == 0x01 and dev in AMD_PARTS:
        out["name"] = AMD_PARTS[dev][0]
        out["id_documented"] = True
    elif man == 0x04 and dev == 0xAD:
        # Fujitsu MBM29F016A — JEDEC-совместим с Am29F016,
        # вендор ID Fujitsu (04h), код устройства ADh.
        out["name"] = "MBM29F016A"
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
    out = {}
    twin = NorFlash(flash.bus, chips=flash.chips, sector=flash.sector,
                    cmd=(0, 0), wide=True, name=flash.name or "",
                    protocol="intel")
    man, dev = twin.read_id()
    if man in (0x89, 0x01, 0x1F, 0x20) and dev not in (0xFF, 0x00, None):
        out["manuf"] = man
        out["device"] = dev
        names = {0xA1: "28F016SA", 0xA2: "28F008SA", 0xA3: "28F016S5",
                 0xA4: "28F320"}
        if man == 0x89:
            out["name"] = names.get(dev, "")
        elif man == 0x01 and dev in AMD_PARTS:
            out["name"] = AMD_PARTS[dev][0]
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
        else:
            # Неоднородная карта (boot-блок): стирать «сектором» нельзя.
            out["uniform"] = False
    flash.reset()
    return out


def _restore_sector(flash, off, was):
    """Вернуть в сектор прежние байты. Возвращает описание, что вышло.

    NOR-память умеет только «1 -> 0», стереть отдельный байт нельзя. Поэтому
    писать можно лишь туда, где сейчас 0xFF, и «восстановление» - это
    побайтовая запись прежних значений в только что стёртый сектор.
    Если что-то вернуть не удалось - говорим прямо, а не делаем вид.
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
        ok, _why = flash.program_byte(off + i, b)
        if not ok:
            lost += 1
    if lost:
        return "вернуть удалось не всё: %d байт не восстановлены" % lost
    got = flash.read(off, len(was))
    if got != was:
        diff = sum(1 for a, b in zip(got, was) if a != b)
        return "после возврата не совпадает %d байт" % diff
    return "сектор возвращён как был"


def verify_geometry(flash, geo, progress=None, backup_path=None):
    """Проверка «а та ли это микросхема и тот ли сектор» ДО стирания.

    Пассивно этого не увидеть: ошибка «взяли 64 КиБ вместо 16 КиБ» или
    «микросхема не стирается без Vpp» проявляется только стиранием. Поэтому
    проверка активная - но НЕ РАЗРУШАЮЩАЯ:

      1. берём последний сектор карты (там обычно меньше всего данных);
      2. снимаем его в память (и в файл, если задан backup_path) ДО стирания;
      3. стираем и убеждаемся, что в секторе только 0xFF;
      4. возвращаем снимок побайтово и сверяем.

    Прежняя версия стирала последний сектор и НЕ возвращала данные: проверка
    перед записью молча уничтожала сектор карты. Возврат был только при
    --snapshot-dir, да и тот с ошибкой (проверялся не тот признак).

    Возвращает (ok, почему). Сектор не остаётся стёртым молча ни при каком
    исходе: если стирание сорвалось - всё равно пытаемся вернуть данные.
    """
    if progress:
        progress("проверяю размер сектора и отклик микросхемы")
    sector = int(geo.get("sector") or 0)
    limit = int(geo.get("size") or 0)
    if sector <= 0:
        return True, "размер сектора неизвестен - проверка пропущена"
    if limit < sector:
        return True, ("ёмкость карты неизвестна или меньше сектора (%d < %d) - "
                      "проверка пропущена" % (limit, sector))
    off = limit - sector
    off -= off % sector

    was = flash.read(off, sector)
    if len(was) != sector:
        return False, ("по адресу %#x прочиталось %d байт вместо %d: карта "
                       "меньше, чем мы думаем" % (off, len(was), sector))
    if backup_path:
        try:
            with open(backup_path, "wb") as f:
                f.write(was)
        except OSError as exc:
            return False, "не смог сохранить копию сектора: %s" % exc

    blank = all(b == 0xFF for b in was)
    ok, why = flash.erase_sector(off)
    if not ok:
        back = "" if blank else _restore_sector(flash, off, was)
        return False, ("сектор %d КиБ по адресу %#x не стёрся: %s%s"
                       % (sector // 1024, off, why,
                          ("; " + back) if back else ""))

    data = flash.read(off, sector)
    bad = next((i for i, b in enumerate(data) if b != 0xFF), None)
    if bad is not None:
        back = "" if blank else _restore_sector(flash, off, was)
        return False, ("после стирания байт %#x читается %#04x вместо 0xFF: "
                       "сектор не %d КиБ, стирание не идёт (Vpp?) или это не "
                       "та микросхема%s"
                       % (off + bad, data[bad], sector // 1024,
                          ("; " + back) if back else ""))

    if blank:
        return True, ("сектор %d КиБ по адресу %#x стирается (был пуст, "
                      "возвращать нечего)" % (sector // 1024, off))
    back = _restore_sector(flash, off, was)
    if back != "сектор возвращён как был":
        return False, ("сектор стирается, но данные вернуть не удалось: %s. "
                       "Копия сектора %s" % (back, ("в " + backup_path)
                                             if backup_path else "потеряна"))
    return True, ("сектор %d КиБ стирается по адресу %#x, данные возвращены"
                  % (sector // 1024, off))


def sector_table(geo, total):
    """Таблица секторов: список (начало, размер). Размер сектора - из geo.

    У реальных микросхем бывает неоднородная карта (несколько маленьких
    секторов в начале/конце). Пока набор работает с однородной, но функция
    отдельная: когда появится карта с неоднородной, менять придётся только её.
    """
    sector = geo["sector"]
    out = []
    off = 0
    total = total or 0
    while off < total:
        n = min(sector, total - off)
        out.append((off, n))
        off += n
    return out


def flash_plan(image, sector=SECTOR):
    """Что делать с каждым сектором: стирать или нет и какие байты писать.

    sector - РАЗМЕР СЕКТОРА КОНКРЕТНОЙ МИКРОСХЕМЫ (не всегда 64 КиБ).
    """
    plan = []
    for off in range(0, len(image), sector):
        chunk = image[off:off + sector]
        need_erase = any(b != 0xFF for b in chunk)
        writes = [off + i for i, b in enumerate(chunk) if b != 0xFF]
        plan.append((off, need_erase, writes))
    return plan


class NorBus:
    """Окно моста, отображённое целиком, и доступ к карте по байтам.

    Почему не через Mem.read/write на каждый байт: там mmap+munmap на каждый
    вызов, а программирование байта - это 3 записи и 2-3 чтения статуса.
    На 700 тысяч байт это миллионы системных вызовов. Здесь отображение
    делается один раз на участок, и дальше это обычные обращения к памяти
    (область некэшируемая, поэтому каждое чтение реально уходит в карту -
    иначе опрос DQ7 не работал бы).
    """

    def __init__(self, mem, exca, win, span=None, dev_id=None, writable=True,
                 limit=0):
        """limit - ёмкость карты в байтах (0 = неизвестна).

        Зачем: окно ставят на размер захода, и оно легко выходит за конец
        карты. За концом карты шина отдаёт не данные, а 0x00/0xFF - и опрос
        DQ7 «готово» на такой ячейке не наступит никогда. Нашлось на модели
        при записи в последний сектор.
        """
        self.mem = mem
        self.exca = exca
        self.win = win
        self.dev_id = dev_id
        self.writable = writable
        self.limit = int(limit or 0)
        self.span = max(PAGE, min(span or win["size"], win["size"]))
        self._m = None
        self._base = 0            # смещение на карте, с которого отображено
        self._len = 0
        self._off = 0             # сдвиг внутри mmap (адрес окна не всегда
        self._maps = 0            # кратен странице - на всякий случай держим)

    def _close_map(self):
        if self._m is not None:
            try:
                self._m.close()
            except (OSError, ValueError, BufferError):
                pass
        self._m = None
        self._len = 0

    def ensure(self, card_off, n=1):
        """Гарантирует, что байты [card_off, card_off+n) отображены."""
        if self._m is not None and self._base <= card_off and \
                card_off + n <= self._base + self._len:
            return
        self._close_map()
        base = card_off - (card_off % PAGE)
        length = min(self.span, self.win["size"])
        if self.limit:
            # не залезать за конец карты: там не данные, а пустая шина
            room = max(PAGE, self.limit - base)
            length = min(length, room)
        if length < n + (card_off - base):
            length = ((card_off - base) + n + PAGE - 1) // PAGE * PAGE
        regs = mem_for_addr(self.mem, self.exca, writable=True, dev_id=self.dev_id)
        map_card_window(regs, self.exca, self.win, card_offset=base,
                        attrib=False, size=length)
        m, off = self.mem._map(self.win["start"], length, write=self.writable)
        self._m, self._off, self._base, self._len = m, off, base, length
        self._maps += 1

    def rd(self, card_off):
        self.ensure(card_off, 1)
        return self._m[self._off + card_off - self._base]

    def wr(self, card_off, value):
        self.ensure(card_off, 1)
        self._m[self._off + card_off - self._base] = value & 0xff

    def wr_many(self, card_off, data):
        """Записать несколько соседних байт одной командой (Intel: слово)."""
        self.ensure(card_off, len(data))
        start = self._off + card_off - self._base
        self._m[start:start + len(data)] = bytes(data)

    def rd_block(self, card_off, n):
        out = bytearray()
        got = 0
        while got < n:
            self.ensure(card_off + got, 1)
            take = min(n - got, self._base + self._len - (card_off + got))
            if take <= 0:
                # Окно не покрывает адрес (он за концом карты). Прежде здесь
                # стоял continue - то есть бесконечный цикл вместо ошибки.
                raise DirectError(
                    f"адрес {card_off + got:#x} за пределами карты "
                    f"(окно {self._base:#x}+{self._len:#x})")
            start = self._off + card_off + got - self._base
            out += self._m[start:start + take]
            got += take
        return bytes(out)

    def close(self):
        self._close_map()


class NorFlashIO:
    """Низуровневая работа с шиной: чтение, запись, разблокировка, сброс, bypass."""

    def __init__(self, bus, chips=2, sector=SECTOR, verbose=False, polls=None,
                 cmd=(NOR_CMD_ADDR1, NOR_CMD_ADDR2), wide=False, page=1,
                 bypass=False, strata=False, name="", protocol="jedec"):
        self.bus = bus
        self.chips = max(int(chips or 1), 1)
        # sector здесь - В АДРЕСАХ КАРТЫ (уже умножен на число чипов).
        self.sector = int(sector or SECTOR)
        self.protocol = protocol or "jedec"
        self.strata = bool(strata)                  # AMD/Fujitsu StrataFlash 25h/D0h
        self.verbose = verbose
        self.polls = polls or FLASH_POLLS
        self.cmd1, self.cmd2 = cmd
        self.wide = bool(wide)          # команда словом (Intel 28F): байт -> 2 байта
        self.page = max(int(page or 1), 1)          # байт на чип
        # В адресах карты страница занимает page * chips байт: 32 байта буфера
        # микросхемы - это 32 байта В КАЖДОМ чипе, то есть 64 байта карты.
        self.page_card = self.page * self.chips
        self.bypass = bool(bypass)                  # Unlock Bypass 20h есть?
        self._bypass_active = False                 # сеанс bypass открыт?
        self._bypass_broken = False                 # bypass не работает?
        self._erase_suspended = False               # стирание приостановлено?
        self.name = name

    # --- адреса команд -----------------------------------------------------
    def cmd_addr(self, local):
        """Абсолютный адрес для адреса команды local внутри чипа."""
        return chip_abs(local, 0, self.chips)

    def _send(self, local, value):
        """Послать команду value по адресу команды local (внутри чипа 0).

        У Intel 28F команда - слово: те же два байта пишутся ДВАЖДЫ (в оба
        байта 16-битной шины), иначе микросхема примет это за данные. Это
        тонкость, на которой легко ошибиться: 0x5555 и 0x555 - разные адреса.
        """
        data = bytes([value, value]) if self.wide else bytes([value])
        self.bus.wr_many(self.cmd_addr(local), data)

    # --- чтение массива ----------------------------------------------------
    def read(self, card_off, n=1):
        return self.bus.rd_block(card_off, n)

    def read_byte(self, card_off):
        return self.bus.rd(card_off)

    # --- команды -----------------------------------------------------------
    def unlock(self):
        """JEDEC: AA/55. Intel 28F: разблокировки нет, команда одна."""
        if self.protocol != "jedec":
            return
        self._send(self.cmd1, NOR_UNLOCK1)
        self._send(self.cmd2, NOR_UNLOCK2)

    def reset(self):
        """Вернуть чипы в режим чтения массива."""
        if self.protocol == "intel":
            for chip in range(self.chips):
                try:
                    self.bus.wr_many(chip_abs(0, chip, self.chips),
                                     bytes([INTEL_CUI_RESET, INTEL_CUI_RESET]))
                except (DirectError, OSError, ValueError):
                    pass
            return
        cmd = NOR_RESET
        for chip in range(self.chips):
            try:
                self.bus.wr_many(chip_abs(0, chip, self.chips), bytes([cmd]))
            except (DirectError, OSError, ValueError):
                pass
        self._bypass_active = False
        self._erase_suspended = False

    def enter_bypass(self):
        """Войти в режим Unlock Bypass (команда 20h).

        После трёх циклов AA/555h, 55/2AAh, 20h/555h микросхема принимает
        программирование за два цикла (A0h → PA/PD) вместо четырёх. Режим
        действует до сброса (exit_bypass() или F0h). Работает только у чипов,
        у которых в GEOMETRY bypass=True (Am29F016/032/040/080, MBM29F016).
        """
        if self.protocol != "jedec" or not self.bypass:
            return
        self.unlock()
        self._send(self.cmd1, NOR_BYPASS)
        self._bypass_active = True

    def exit_bypass(self):
        """Выйти из режима Unlock Bypass: сброс чипов в чтение массива.

        Вызывается безусловно в конце записи, чтобы следующая операция
        (чтение, стирание, другая запись) начиналась из чистого состояния.
        """
        self.reset()
        self._bypass_active = False


class NorFlashStatus:
    """Статус, идентификатор, защита секторов, приостановка стирания."""

    def read_sector_protection(self):
        """Чтение состояния защиты групп секторов (команда A6h).

        Am29F016D и новее: 8 групп по 4 сектора (32 сектора всего).
        После разблокировки и команды A6h чтение по адресу группы возвращает
        DQ0=1, если группа защищена.

        Возвращает dict: номер группы -> True (защищена) / False (не защищена),
        либо None если команда не поддерживается микросхемой.
        """
        if self.protocol != "jedec":
            return None
        # Sector Group Protect работает только у микросхем с CFI (D-ревизия и новее)
        # У старых Am29F016/016B команды A6h нет — возвращаем None.
        self.unlock()
        self._send(self.cmd1, NOR_SECTOR_PROTECT)
        # Читаем состояние всех 8 групп: адрес группы смещён на 6 бит
        # (A6 кодирует номер группы: группа 0 = адрес 0x000, группа 1 = 0x040, ...)
        groups = {}
        for grp in range(8):
            addr = grp << 6   # бит A6 = номер группы
            try:
                val = self.read_byte(chip_abs(addr, 0, self.chips))
                groups[grp] = bool(val & 0x01)
            except (DirectError, OSError, ValueError):
                groups[grp] = None
        self.reset()
        return groups

    def erase_suspend(self):
        """Приостановка стирания (команда B0h).

        Позволяет приостановить стирание сектора, чтобы прочитать или записать
        данные в ДРУГОЙ сектор, а затем продолжить (erase_resume,
        команда 30h). Пауза не ограничена 20 мкс - ограничен только отклик на
        саму команду B0h.

        Возвращает True если команда принята, False если не поддерживается.
        """
        if self.protocol != "jedec":
            # B0h/30h - команды JEDEC-набора AMD/Fujitsu; у Intel 28F
            # приостановки стирания нет. К Unlock Bypass (20h) это отношения
            # не имеет: прежнее условие «только при bypass» было неверным.
            return False
        self._send(self.cmd1, NOR_ERASE_SUSPEND)
        self._erase_suspended = True
        return True

    def erase_resume(self):
        """Продолжение стирания после приостановки (команда 30h).

        Возвращает True если команда принята.
        """
        if self.protocol != "jedec":
            return False
        self._send(self.cmd1, NOR_ERASE_RESUME)
        self._erase_suspended = False
        return True

    def clear_status(self):
        """Intel: сброс статусного регистра (биты 3,4,5 ставятся навсегда)."""
        if self.protocol != "intel":
            return
        for chip in range(self.chips):
            try:
                self.bus.wr_many(chip_abs(0, chip, self.chips),
                                 bytes([INTEL_CUI_CLEAR_SR, INTEL_CUI_CLEAR_SR]))
            except (DirectError, OSError, ValueError):
                pass

    def read_status(self, card_off=0):
        """Статус: у JEDEC - DQ7/DQ5 из ячейки, у Intel - команда 70h + чтение."""
        if self.protocol != "intel":
            return self.read_byte(card_off)
        chip = chip_of(card_off, self.chips)
        self.bus.wr_many(chip_abs(0, chip, self.chips),
                         bytes([INTEL_CUI_READ_SR, INTEL_CUI_READ_SR]))
        return self.read_byte(card_off)

    def _status_ok(self, card_off, want_value=None):
        """(готово, объяснение отказа) для текущей операции.

        У Intel важен порядок: сначала WSMS (бит 7) - операция закончена?,
        затем VPPS (бит 3) - «Vpp вне допуска», то есть нет 12 В, затем
        ESS/BWS (биты 5/4) - сама операция не удалась.
        """
        st = self.read_status(card_off)
        if self.protocol != "intel":
            return st, ""
        if not (st & SR_READY):
            return st, ""                      # ещё идёт
        if st & SR_VPP_LOW:
            return st, ("нет напряжения программирования (VPPS=1): этой "
                        "микросхеме нужны 12 В на Vpp, а слот их не даёт")
        if st & SR_ERASE_FAIL:
            return st, "микросхема сообщает: стирание не удалось (ESS=1)"
        if st & SR_WRITE_FAIL:
            return st, "микросхема сообщает: запись не удалась (BWS=1)"
        return st, ""

    def read_id(self):
        """JEDEC ID: (производитель, устройство) или (None, None)."""
        if self.protocol == "intel":
            chip = 0
            self.bus.wr_many(chip_abs(0, chip, self.chips),
                             bytes([INTEL_CUI_READ_ID, INTEL_CUI_READ_ID]))
            man = self.read_byte(chip_abs(0, chip, self.chips))
            dev = self.read_byte(chip_abs(1, chip, self.chips))
            self.reset()
            return man, dev
        self.unlock()
        self._send(self.cmd1, AMD_READ_ID)
        man = self.read_byte(chip_abs(0, 0, self.chips))
        dev = self.read_byte(chip_abs(1, 0, self.chips))
        self.reset()
        return man, dev


class NorFlashProgramming:
    """Программирование: побайтовое, слово, страница, буферная запись (25h/D0h)."""

    def program_byte(self, card_off, value):
        """Записать один байт. Возвращает (ok, почему).

        У Intel 28F команда и данные - СЛОВО (2 байта). Поэтому байт там
        пишется парой: нужный байт и 0xFF во втором байте слова (0xFF ничего
        не меняет: NOR умеет только 1 -> 0). См. program_word().

        Если у микросхемы есть Unlock Bypass (bypass=True), запись идёт через
        program_byte_bypass(): два цикла A0h/PA на байт вместо четырёх. Режим
        bypass должен быть включён enter_bypass() перед пакетной записью и
        выключен exit_bypass() после.
        """
        if self.protocol == "intel":
            return self._intel_program(card_off, value)
        if self.wide:
            return self.program_word(card_off, value)
        before = self.read_byte(card_off)
        if before == value:
            return True, ""
        if before != 0xFF:
            return False, f"байт не стёрт ({before:#04x}) — нужна чистка сектора"
        if self.bypass and not self._bypass_broken:
            # Автовход в Unlock Bypass: первая запись открывает сеанс,
            # последующие пишут за 2 цикла без повторной разблокировки.
            # Завершить сеанс нужно exit_bypass() (или reset()).
            if not self._bypass_active:
                self.enter_bypass()
                self._bypass_active = True
            ok, why = self.program_byte_bypass(card_off, value)
            if ok:
                return ok, why
            # Микросхема не приняла Unlock Bypass. Так бывает: JEDEC ID у
            # семейства один (01ADh у Am29F016, B и D), а команда 20h есть
            # только у поздних ревизий. Откатываемся на обычную запись A0h -
            # один раз, с предупреждением, и больше bypass не пробуем.
            self.exit_bypass()
            self._bypass_active = False
            self._bypass_broken = True
            print("  ВНИМАНИЕ: Unlock Bypass (20h) не сработал: " + why
                  + ". Дальше пишу обычной командой A0h - медленнее, но верно.")
        self.unlock()
        self._send(self.cmd1, NOR_PROGRAM)
        self.bus.wr(card_off, value)
        ok, why = self._wait_program(card_off, value)
        if not ok:
            return False, why
        got = self.read_byte(card_off)
        if got != value:
            return False, f"после записи читается {got:#04x} вместо {value:#04x}"
        return True, ""

    def program_byte_bypass(self, card_off, value):
        """Записать один байт через Unlock Bypass (A0h → PA/PD, 2 цикла).

        Предполагается, что enter_bypass() уже вызван - иначе команда A0h
        без предварительных разблокирующих циклов не будет принята.
        Разблокировка внутри не делается, чтобы не тратить лишние циклы
        при пакетной записи: вход один раз, много байт, выход один раз.
        """
        self._send(self.cmd1, NOR_PROGRAM)
        self.bus.wr(card_off, value)
        ok, why = self._wait_program(card_off, value)
        if not ok:
            return False, why
        got = self.read_byte(card_off)
        if got != value:
            return False, f"после записи читается {got:#04x} вместо {value:#04x}"
        return True, ""

    def program_word(self, card_off, value=None):
        """Записать СЛОВО, в котором лежит байт card_off (у Intel - так).

        Второй байт слова не трогаем: он остаётся таким, каким был (обычно
        0xFF после стирания). Нужен ровно один сценарий - «записать этот
        байт», поэтому значение берётся из card_off, а пара читается заранее.
        """
        pair_at = card_off - (card_off % 2)
        before = self.read_byte(card_off)
        value = before if value is None else value
        if before != 0xFF and before != value:
            return False, f"байт не стёрт ({before:#04x}) — нужна чистка сектора"
        data = bytearray([self.read_byte(pair_at), self.read_byte(pair_at + 1)])
        data[card_off - pair_at] = value
        self.unlock()
        self._send(self.cmd1, NOR_PROGRAM)
        self.bus.wr_many(pair_at, bytes(data))
        ok, why = self._wait_program(card_off, value)
        if not ok:
            return False, why
        got = self.read_byte(card_off)
        if got != value:
            return False, f"после записи читается {got:#04x} вместо {value:#04x}"
        return True, ""

    def program_page(self, card_off, data):
        """Записать страницу программирования (до page байт) одной командой.

        data - то, что должно оказаться в карте; 0xFF означает «не трогать»
        (в NOR запись умеет только 1 -> 0, а стёртое состояние и есть 0xFF).
        Если page=1, работает обычная побайтовая запись.
        У Intel 28F/StrataFlash с page > 1 используется буферная запись 25h/D0h.
        """
        if len(data) > self.page_card:
            return False, f"за один раз не больше {self.page_card} байт"
        if self.page <= 1 or len(data) <= 1:
            for i, b in enumerate(data):
                if b == 0xFF:
                    continue
                ok, why = self.program_byte(card_off + i, b)
                if not ok:
                    return False, why
            return True, ""
        if self.protocol == "intel":
            return self._program_page_intel(card_off, data)
        if self.strata:
            return self._program_page_strata_amd(card_off, data)
        return self._program_page_amd(card_off, data)

    def _program_page_amd(self, card_off, data):
        """Страница AMD (Am29F016/032): A0h, данные подряд, последний - пуск.

        Последовательность взята из даташита: после A0h по адресу команды
        записывается первый байт, затем остальные байты страницы идут подряд;
        программирование запускает запись ПОСЛЕДНЕГО байта. Опрос DQ7/DQ5
        начинается только после него, поэтому на страницу выходит одна
        команда и один опрос вместо 32 команд.

        ВАЖНО: последний байт страницы пишется ВСЕГДА, даже если он 0xFF -
        иначе программирование не запустится. 0xFF в NOR ничего не меняет
        (запись умеет только 1 -> 0), поэтому это безопасно.
        """
        start = card_off - (card_off % self.page_card)
        if card_off != start or len(data) != self.page_card:
            # Всегда пишем страницу ЦЕЛИКОМ от её начала: программирование
            # запускает заполнение буфера (последний байт). Иначе пришлось бы
            # надеяться на таймаут, а его длительность у разных партий разная.
            return self.program_page(start, self._pad_page(start, data, card_off))
        for i, b in enumerate(data):
            if b != 0xFF and self.read_byte(start + i) != 0xFF:
                return False, f"байт {start + i:#x} не стёрт"
        self.unlock()
        self._send(self.cmd1, NOR_PROGRAM)
        # Данные уходят ОДНОЙ операцией записи в окно: именно так работает
        # буфер микросхемы, и именно так пуск происходит по последнему байту.
        self.bus.wr_many(start, bytes(data))
        last = start + len(data) - 1
        return self._wait_program(last, data[-1])

    def _pad_page(self, start, data, card_off):
        """Собрать полную страницу: данные образа плюс 0xFF на остальном.

        Нужна потому, что образ может задевать только часть страницы, а
        команда A0h у страничных микросхем ожидает её целиком.
        """
        page = bytearray(b"\xff" * self.page_card)
        for i, b in enumerate(data):
            page[card_off - start + i] = b
        return bytes(page)

    def _program_page_intel(self, card_off, data):
        """StrataFlash буферная запись: 25h setup, данные, D0h confirm, опрос SR.

        Последовательность (по даташиту 28F016S5 / M28F016S5):
          1. Записать 25h в адрес буфера каждого чипа (Write-to-Buffer Setup)
          2. Записать данные в буфер каждого чипа (по байту на чип за такт)
          3. Записать D0h в адрес буфера каждого чипа (Buffer Write Confirm)
          4. Опросить статусный регистр (70h) каждого чипа до WSMS=1
          5. Сбросить каждый чип (FFh)

        Каждый чип имеет свой буфер. В 16-битном режиме с 2 чипами:
        карт-байт start+i*chips+chip → чип chip, смещение i внутри чипа.
        page_card = page * chips байт на карте, page байт на чип.
        """
        start = card_off - (card_off % self.page_card)
        if card_off != start or len(data) != self.page_card:
            return self.program_page(start, self._pad_page(start, data, card_off))

        # Проверяем, что все байты страницы стёрты (0xFF) или уже нужного значения
        for i, b in enumerate(data):
            if b != 0xFF and self.read_byte(start + i) != 0xFF:
                return False, f"байт {start + i:#x} не стёрт"

        # 1. 25h Setup — посылаем в каждый чип по адресу буфера (смещение 0 внутри чипа)
        for chip in range(self.chips):
            self._intel_send(chip, INTEL_BUFFER_SETUP, 0)

        # 2. Пишем данные в буфер каждого чипа
        # data лежит в порядке карты: start+0, start+1, start+2, ...
        # chip 0 получает data[0], data[2], data[4]... (чётные)
        # chip 1 получает data[1], data[3], data[5]... (нечётные)
        for i in range(self.page):
            for chip in range(self.chips):
                byte_idx = i * self.chips + chip
                val = data[byte_idx] if byte_idx < len(data) else 0xFF
                addr = chip_abs(i, chip, self.chips)
                self.bus.wr(addr, val)

        # 3. D0h Confirm — посылаем в каждый чип
        for chip in range(self.chips):
            self._intel_send(chip, INTEL_BUFFER_CONFIRM, 0)

        # 4. Ожидание готовности через статусный регистр каждого чипа
        for chip in range(self.chips):
            addr = chip_abs(0, chip, self.chips)
            for _ in range(self.polls):
                st, why = self._status_ok(addr)
                if why:
                    return False, f"чип {chip}, буфер {start:#x}: {why}"
                if st & SR_READY:
                    self._intel_send(chip, INTEL_CUI_RESET)
                    break
            else:
                return False, f"чип {chip}, буфер {start:#x}: не дождались готовности за отведённое время"

        # 5. Проверяем записанные данные
        for i, b in enumerate(data):
            if b == 0xFF:
                continue
            got = self.read_byte(start + i)
            if got != b:
                return False, (f"после буферной записи читается {got:#04x} "
                               f"вместо {b:#04x} по адресу {start + i:#x}")

        return True, ""

    def _program_page_strata_amd(self, card_off, data):
        """AMD/Fujitsu StrataFlash буферная запись (Am29PL/DL/GL, MBM29DL/GL).

        Протокол JEDEC (разблокировка AA/55, опрос DQ7/DQ5), не Intel CUI.
        Последовательность (по даташиту Am29PL/DL/GL):
          1. Разблокировка: AAh@0x555, 55h@0x2AA
          2. 25h@адрес_буфера (Write-to-Buffer Setup)
          3. Данные по байту в адреса буфера каждого чипа
          4. D0h@адрес_буфера (Buffer Write Confirm)
          5. Опрос DQ7/DQ5 до готовности
          6. Сброс F0h

        Каждый чип имеет свой буфер. В адресах карты с 2 чипами:
        карт-байт start+i*chips+chip → чип chip, смещение i внутри чипа.
        page_card = page * chips байт на карте, page байт на чип.
        """
        start = card_off - (card_off % self.page_card)
        if card_off != start or len(data) != self.page_card:
            return self.program_page(start, self._pad_page(start, data, card_off))

        # Проверяем, что все байты страницы стёрты (0xFF) или уже нужного значения
        for i, b in enumerate(data):
            if b != 0xFF and self.read_byte(start + i) != 0xFF:
                return False, f"байт {start + i:#x} не стёрт"

        # Если активно Unlock Bypass — выйти из него перед буферной записью:
        # из состояния bypass последовательность AA/55 не переводит чип в u1/u2,
        # поэтому unlock() не сработает и 25h Setup не будет принят.
        if self._bypass_active:
            self.reset()

        # 1. Разблокировка: AAh@cmd1, 55h@cmd2
        self.unlock()

        # 2. 25h Setup — посылаем в каждый чип по адресу буфера (смещение 0 внутри чипа)
        # Адреса карты: start + chip (byte 0 страницы → чип 0, byte 1 → чип 1, ...)
        # Используем bus.wr напрямую, а не _send, т.к. _send пересчитывает адрес
        # через chip_abs(local, 0, chips) — неверно для произвольных адресов буфера.
        for chip in range(self.chips):
            buf_addr = start + chip
            self.bus.wr(buf_addr, AMD_BUFFER_SETUP)

        # 3. Пишем данные в буфер каждого чипа
        # data лежит в порядке карты: start+0, start+1, start+2, ...
        # chip 0 получает data[0], data[2], data[4]... (чётные)
        # chip 1 получает data[1], data[3], data[5]... (нечётные)
        for i in range(self.page):
            for chip in range(self.chips):
                byte_idx = i * self.chips + chip
                val = data[byte_idx] if byte_idx < len(data) else 0xFF
                addr = start + i * self.chips + chip
                self.bus.wr(addr, val)

        # 4. D0h Confirm — посылаем в каждый чип
        for chip in range(self.chips):
            buf_addr = start + chip
            self.bus.wr(buf_addr, AMD_BUFFER_CONFIRM)

        # 5. Ожидание готовности через DQ7/DQ5 каждого чипа
        for chip in range(self.chips):
            addr = start + chip
            last_val = data[(self.page - 1) * self.chips + chip] if self.page * self.chips <= len(data) else 0xFF
            want = last_val & DQ7
            for _ in range(self.polls):
                st = self.read_byte(addr)
                if st & DQ5:
                    st2 = self.read_byte(addr)
                    if (st2 & DQ7) == want:
                        break
                    return False, f"чип {chip}, буфер {start:#x}: микросхема не приняла данные (DQ5=1, DQ7 не тот)"
                if (st & DQ7) == want:
                    break
            else:
                return False, f"чип {chip}, буфер {start:#x}: не дождались готовности за отведённое время"

        # 6. Сброс F0h — возвращаем чипы в чтение массива
        self.reset()

        # 7. Проверяем записанные данные
        for i, b in enumerate(data):
            if b == 0xFF:
                continue
            got = self.read_byte(start + i)
            if got != b:
                return False, (f"после буферной записи читается {got:#04x} "
                               f"вместо {b:#04x} по адресу {start + i:#x}")

        return True, ""

    # --- Intel 28F: CUI (свой набор команд со статусным регистром) ---------
    def _intel_send(self, chip, op, addr_local=None):
        """Послать команду CUI в конкретный чип: (адрес, данные).

        Адрес и данные у Intel - СЛОВО: 0x2020 при стирании, 0x7070 при
        чтении статуса. Поэтому байтовое смещение удваивается, а данные
        дублируются в оба байта.
        """
        base = chip_abs(addr_local or 0, chip, self.chips)
        self.bus.wr_many(base, bytes([op, op]))
        return base

    def _intel_word_base(self, card_off):
        """(адрес начала СЛОВА, номер чипа внутри слова) для байта card_off.

        У Intel всё - слова: один байт слова лежит в чипе 0, второй в чипе 1
        по ТОМУ ЖЕ адресу внутри чипа. В адресах карты это соседние байты:
        2n (чип 0) и 2n+1 (чип 1).
        """
        chip = chip_of(card_off, self.chips)
        return card_off - chip, chip

    def _intel_program(self, card_off, value):
        before = self.read_byte(card_off)
        if before == value:
            return True, ""
        if before != 0xFF:
            return False, f"байт не стёрт ({before:#04x}) — нужна чистка блока"
        pair_at, chip = self._intel_word_base(card_off)
        data = bytearray(self.read_byte(pair_at + c) for c in range(self.chips))
        data[chip] = value
        self._intel_send(chip, INTEL_CUI_WRITE_SETUP)
        self.bus.wr_many(pair_at, bytes(data))
        word_base = chip_abs(0, chip, self.chips)
        for _ in range(self.polls):
            st, why = self._status_ok(word_base)
            if why:
                return False, why
            if st & SR_READY:
                # пока не послана команда FF, чип отдаёт СТАТУС, а не данные
                self._intel_send(chip, INTEL_CUI_RESET)
                got = self.read_byte(card_off)
                if got != value:
                    return False, (f"после записи читается {got:#04x} вместо "
                                   f"{value:#04x} (статус {st:#04x})")
                return True, ""
        return False, "микросхема не сообщила о готовности за отведённое время"

    def _intel_erase(self, card_off):
        """Стирание блока по командам CUI: 20h (setup), затем D0h (confirm)."""
        base = card_off - (card_off % self.sector)
        for chip in range(self.chips):
            local = chip_local(base, self.chips)
            self._intel_send(chip, INTEL_CUI_ERASE_SETUP, local)
            self._intel_send(chip, INTEL_CUI_ERASE_CONFIRM, local)
            addr = chip_abs(0, chip, self.chips)
            for _ in range(self.polls):
                st, why = self._status_ok(addr)
                if why:
                    return False, f"чип {chip}, блок {base:#x}: {why}"
                if st & SR_READY:
                    break
            else:
                return False, (f"чип {chip}, блок {base:#x}: микросхема не "
                               "сообщила о готовности за отведённое время")
            self._intel_send(chip, INTEL_CUI_RESET)
        return True, ""

    def _wait_program(self, card_off, value):
        """Ожидание записи байта по DQ7/DQ5.

        Порядок важен: DQ5 (бит 5) означает «вышли за пределы времени», и пока
        он поднят, значение DQ7 недостоверно. Поэтому сначала проверяем DQ5:
        если он есть - читаем ещё раз и смотрим DQ7 уже без оглядки на DQ5:
        совпал - это была задержка, не совпал - настоящий отказ.
        """
        want = value & DQ7
        for _ in range(self.polls):
            st = self.read_byte(card_off)
            if st & DQ5:
                st2 = self.read_byte(card_off)
                if (st2 & DQ7) == want:
                    return True, ""
                return False, ("микросхема не приняла байт (DQ5=1, DQ7 не тот, "
                               "DQ7 недостоверен): ячейка изношена или запись "
                               "запрещена")
            if (st & DQ7) == want:
                return True, ""
        return False, "микросхема не ответила за отведённое число опросов"



class NorFlashErase:
    """Стирание: сектор, чип, приостановка/возобновление."""

    def erase_sector(self, card_off):
        """Стереть сектор, содержащий card_off. Возвращает (ok, почему).

        ВАЖНО (нашлось на модели, а не на карте): стирать надо В КАЖДОМ чипе
        пары отдельно. Команда 30h действует на тот чип, к которому попал её
        адрес: байт с чётным смещением уходит в чип 0, с нечётным - в чип 1.
        Если послать одну команду по чётному адресу, второй чип не сотрётся,
        и запись в нечётные байты провалится с «байт не стёрт».
        """
        base = card_off - (card_off % self.sector)
        if self.protocol == "intel":
            return self._intel_erase(base)
        # base - АБСОЛЮТНОЕ смещение; чипу нужен адрес ВНУТРИ него, поэтому
        # сначала переводим в байты чипа, и только потом раскладываем по чипам.
        local = chip_local(base, self.chips)
        for chip in range(self.chips):
            addr = chip_abs(local, chip, self.chips)
            self.unlock()
            self._send(self.cmd1, NOR_ERASE_SETUP)
            self.unlock()
            # Шестой цикл - 30h ПО АДРЕСУ СЕКТОРА (даташит, «Sector Erase
            # Command Sequence»; так же делают flashrom: issuecmd(0x30, page)
            # и ядро Linux: map_write(map, cfi->sector_erase_cmd, adr)).
            # Раньше здесь стояло _send(self.cmd1, ...) + wr(addr, 0xFF):
            # команда уходила в 0x555, то есть ВСЕГДА стирала сектор 0.
            self.bus.wr(addr, NOR_ERASE_SECTOR)
            ok, why = self._wait_erase(addr)
            if not ok:
                self.reset()
                return False, f"чип {chip}, сектор {base:#x}: {why}"
        return True, ""

    def erase_chip(self):
        """Стереть чипы целиком. У Intel 28F команды «стереть всё» НЕТ.

        В таблице 9 документа её нет: только пошаговое стирание блоков.
        Поэтому здесь честный отказ, а не «попробуем 10h и посмотрим».
        """
        if self.protocol == "intel":
            return False, ("у Intel 28F008SA нет команды «стереть весь чип»: "
                           "стирать нужно поблочно (это и делает обычная запись)")
        for chip in range(self.chips):
            self.unlock()
            self._send(self.cmd1, NOR_ERASE_SETUP)
            self.unlock()
            self._send(self.cmd1, NOR_ERASE_CHIP)
            ok, why = self._wait_erase(chip_abs(0, chip, self.chips))
            if not ok:
                self.reset()
                return False, f"чип {chip}: {why}"
        return True, ""

    def _wait_erase(self, card_off):
        """Ожидание стирания по DQ7/DQ5.

        Тонкость, на которой легко ошибиться (и я ошибся): у стёртого байта
        значение 0xFF, то есть DQ5 (бит 5) ВСЕГДА поднят. Поэтому требовать
        «DQ7=1 и DQ5=0» на первом же чтении нельзя - у стёртой ячейки это
        никогда не выполнится, и стирание будет объявлено неудачным. По
        даташиту порядок такой: DQ7=1 - стирание закончено, но надо проверить
        DQ5; если DQ5=1, читаем ЕЩЁ РАЗ и смотрим ТОЛЬКО DQ7: он остался 1 -
        всё в порядке, стал 0 - отказ.
        """
        st = 0
        for _ in range(self.polls):
            st = self.read_byte(card_off)
            if not (st & DQ7):
                continue                       # ещё стирается
            if not (st & DQ5):
                return True, ""
            st2 = self.read_byte(card_off)      # DQ5 поднят - проверяем DQ7
            if st2 & DQ7:
                return True, ""
            return False, ("стирание не выполнилось (DQ5=1, при повторном "
                           "чтении DQ7=0): сектор защищён, изношен или карте "
                           "нужно Vpp 12 В")
        return False, (f"стирание не закончилось за отведённое число опросов "
                       f"(последний статус {st:#04x}; DQ7=1 и DQ5=0 означает "
                       "«готово» - значит карта отвечает не так, как ждёт "
                       "даташит: возможно, включён режим CFI или другой чип)")


class NorFlash(NorFlashIO, NorFlashProgramming, NorFlashErase, NorFlashStatus):
    """Команды микросхемы через окно: программирование и стирание.

    Параметры у разных микросхем разные, поэтому всё берётся из geo (см.
    identify_chip): размер сектора, адреса команд 0x555/0x2AA (AMD, Fujitsu)
    или 0x5555/0x2AAA (Intel 28F, где команда - СЛОВО), ширина команды и
    страница программирования. Жёстко зашитых 64 КиБ и 0x555 больше нет,
    кроме значений по умолчанию.

    Сколько чипов в паре (chips) тоже важно: у 8-битной карты он один, и
    тогда адреса команд идут подряд, без «через один байт».

    Состав (каждый класс — своя зона ответственности):

    * NorFlashIO — шина, разблокировка, сброс, bypass, чтение;
    * NorFlashProgramming — побайтовая, слово, страница, буферная запись;
    * NorFlashErase — стирание сектора/чипа, ожидание готовности;
    * NorFlashStatus — статус, ID, защита секторов, приостановка стирания.
    """
    pass


# ---------------------------------------------------------------- паспорт ---

def card_geometry(chips_hint=0):
    """(сколько чипов, сколько байт всего) по паспорту карты.

    Читается без окна: read_cis сам его настраивает. Если паспорт пуст -
    остаётся только догадка «два чипа» (так у 16-битных карт в паре).
    """
    chips, size = (chips_hint or 2), 0
    try:
        _raw, _tuples, info = read_cis(mem=None, verbose=False)
    except DirectError:
        return chips, size
    devs = (info or {}).get("devices") or []
    total = sum((d.get("size") or 0) for d in devs)
    if devs:
        chips = max(len(devs), 1)
    if total:
        size = total
    else:
        size, _why = cis_card_size(info or {})
    return chips, (size or 0)


# ------------------------------------------------------------ ход записи ---

def flash_probe(flash, offset=0, payload=None, progress=None):
    """ПРОБА: стереть и записать ОДИН сектор, прочитать обратно, сравнить.

    Это единственный способ узнать, пишется ли карта в этом слоте, не
    трогая остальное содержимое. payload - что записать (по умолчанию
    узор: 0x00,0xFF,0x55,0xAA, 0..255 - ловит залипшие линии данных).
    """
    sector = flash.sector
    if payload is None:
        pattern = bytearray([0x00, 0xFF, 0x55, 0xAA] * 64)
        pattern += bytes(range(256)) * 2
        payload = bytes(pattern)
    # сколько байт пишем в пробе: не весь сектор, а 2 КиБ (больше не нужно -
    # узор длиной 1024 байта повторяется), но у очень маленьких секторов
    # (16 КиБ и меньше) границы соблюдаем.
    span = min(len(payload), sector, 2048)
    payload = payload[:span]
    if progress:
        progress("стираю сектор")
    ok, why = flash.erase_sector(offset)
    if not ok:
        return False, f"пробное стирание не удалось: {why}"
    if progress:
        progress("проверяю, что стёрлось 0xFF")
    empty = flash.read(offset, len(payload))
    if any(b != 0xFF for b in empty):
        bad = next(i for i, b in enumerate(empty) if b != 0xFF)
        return False, (f"после стирания байт {offset + bad} читается "
                       f"{empty[bad]:#04x}, а должен быть 0xFF: сектор не "
                       "стирается (Vpp, защита или износ)")
    written = 0
    for i, b in enumerate(payload):
        if b == 0xFF:
            continue
        ok, why = flash.program_byte(offset + i, b)
        if not ok:
            return False, (f"запись байта {offset + i}="
                           f"{b:#04x} не удалась: {why}")
        written += 1
    if progress:
        progress("читаю обратно")
    got = flash.read(offset, len(payload))
    if got != payload:
        bad = [i for i in range(len(payload)) if got[i] != payload[i]]
        i = bad[0]
        flash.reset()
        return False, (f"прочитано {got[i]:#04x} вместо {payload[i]:#04x} в байте "
                       f"{offset + i} (различий {len(bad)}): карта не пишет так, "
                       "как ожидалось")
    # Программирование через program_byte могло открыть Unlock Bypass -
    # сбрасываем чипы, чтобы следующая операция (стирание, запись, чтение)
    # начиналась из чистого состояния.
    flash.reset()
    return True, f"стёрт и записан 1 сектор ({written} байт), прочитанное совпало"


def flash_probe_safe(flash, offset=0, backup_dir=None, progress=None):
    """Проба записи, которая не теряет данные: сектор сохраняется и возвращается.

    Порядок: прочитать сектор -> сохранить в файл -> стереть и записать узор ->
    сверить -> стереть -> вернуть исходные байты -> сверить. Если карта не
    пишется, вернуть исходное уже нечем - тогда файл остаётся на диске, и об
    этом говорится прямо (а не «всё хорошо»).
    """
    import os as _os
    sector = flash.sector
    base = offset - (offset % sector)
    if progress:
        progress("читаю сектор, чтобы его не потерять")
    original = flash.read(base, sector)
    path = None
    if backup_dir is not None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = _os.path.join(backup_dir, f"probe-sector-{base:#08x}-{stamp}.bin")
        try:
            _os.makedirs(backup_dir, exist_ok=True)
            with open(path, "wb") as f:
                f.write(original)
        except OSError:
            path = None
    ok, why = flash_probe(flash, base, progress=progress)
    if not ok:
        return {"ok": False, "why": why, "backup": path, "restored": False}
    if progress:
        progress("возвращаю исходные байты сектора")
    flash.erase_sector(base)
    bad = 0
    for i, b in enumerate(original):
        if b == 0xFF:
            continue
        ok2, why2 = flash.program_byte(base + i, b)
        if not ok2:
            bad += 1
            if bad == 1:
                why = f"не удалось вернуть байт {base + i:#x}: {why2}"
    if bad:
        return {"ok": False, "why": why, "backup": path, "restored": False,
                "not_restored_bytes": bad}
    got = flash.read(base, sector)
    if got != original:
        diff = sum(1 for i in range(sector) if got[i] != original[i])
        return {"ok": False, "restored": False, "backup": path,
                "why": f"после возврата сектор отличается в {diff} байтах"}
    # Восстановление через program_byte могло оставить Unlock Bypass активным.
    flash.reset()
    return {"ok": True, "why": "проба прошла, сектор возвращён байт в байт",
            "backup": path, "restored": True}


def journal_path(remote_dir):
    """Путь к файлу журнала записи на ноутбуке."""
    import os as _os
    return _os.path.join(remote_dir, ".journal")


def journal_append(path, entry):
    """Дозаписать одну строку JSON в файл журнала.

    Каждая строка — отдельный JSON-объект. Пишем «открыть — дописать — закрыть»,
    чтобы при обрыве питания или ssh осталось то, что успели записать.
    """
    import json as _json
    line = _json.dumps(entry, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def journal_read(path):
    """Прочитать все записи из файла журнала. Список словарей (может быть пуст)."""
    import json as _json
    import os as _os
    if not _os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(_json.loads(line))
            except _json.JSONDecodeError:
                # битая строка в конце (обрыв записи) — игнорируем
                continue
    return out


def journal_completed_sectors(path):
    """Множество номеров секторов, которые уже успешно записаны."""
    done = set()
    for entry in journal_read(path):
        if entry.get("stage") == "sector_done":
            done.add(entry["sector"])
    return done


def writes_for_bypass(plan):
    """Есть ли в плане хотя бы один сектор с записями (не пустой writes)."""
    return any(bool(writes) for _, _, writes in plan)


def flash_write_image(flash, image, progress=None, verify=True,
                      erase_first=False, journal=None, resume=False,
                      remote_dir=None, backup_dir=None):
    """Записать образ на карту. Возвращает dict с честным итогом.

    Порядок для каждого сектора: стирание (только если в секторе есть
    не-0xFF байты), затем построчная запись отличных байт. После всего -
    чтение обратно и сравнение. Ключ 'ok' в результате - только если
    сравнение сошлось; иначе 'failed_at' говорит, где остановились.

    backup_dir — папка для резервных копий секторов перед стиранием.
    Если задана, каждый сектор, который собираются стереть, сначала
    читается и сохраняется в файл <backup_dir>/sector-<offset>.bin.
    Это позволяет восстановить сектор, если запись прервётся или карта
    окажется изношенной.
    """
    plan = flash_plan(image, flash.sector)
    total = len(plan)
    res = {"sectors": total, "erased": 0, "bytes": 0, "ok": False,
           "failed_at": None, "why": "", "backups": []}

    # Если journal не задан, но есть remote_dir — определяем путь по умолчанию.
    if journal is None and remote_dir:
        journal = journal_path(remote_dir)

    # Если backup_dir задан — создаём папку.
    if backup_dir:
        import os as _os
        try:
            _os.makedirs(backup_dir, exist_ok=True)
        except OSError as error:
            return {"ok": False, "why": f"не удалось создать папку бэкапа "
                    f"{backup_dir}: {error}", "backups": []}

    # Если resume — определяем, какие секторы уже записаны по журналу,
    # и пропускаем их.
    completed = set()
    if resume and journal:
        completed = journal_completed_sectors(journal)
        if completed:
            progress and progress(f"восстанавливаюсь после обрыва: "
                                  f"{len(completed)} сектор(ов) уже записано",
                                  0.0)

    # Unlock Bypass: входим в режим только на время записи байт, выходим
    # сразу после. Стирание и чтение делаются с полной разблокировкой, чтобы
    # не зависеть от состояния bypass. Выход гарантирован finally даже при
    # ошибке записи.
    bypass_active = bool(getattr(flash, "bypass", False)) and bool(
        any(writes for _, _, writes in plan))
    for no, (off, need_erase, writes) in enumerate(plan, 1):
        if off in completed:
            # Этот сектор уже записан в прошлом запуске — пропускаем.
            progress and progress(f"сектор {no} из {total}: {off:#x} (уже записан)",
                                  (no - 1) / max(total, 1))
            res["erased"] += 1 if need_erase else 0
            res["bytes"] += len(writes)
            continue

        if progress:
            progress(f"сектор {no} из {total}: {off:#x}",
                     (no - 1) / max(total, 1))
        # Бэкап сектора перед стиранием: читаем текущее содержимое и
        # сохраняем в файл. Если потом запись не удастся, можно вернуть
        # сектор как был.
        if backup_dir and need_erase:
            import os as _os
            backup_path = _os.path.join(backup_dir,
                                        f"sector-{off:08x}.bin")
            try:
                sector_data = flash.read(off, flash.sector)
                with open(backup_path, "wb") as bf:
                    bf.write(sector_data)
                res["backups"].append(backup_path)
            except (DirectError, OSError) as error:
                res["failed_at"] = off
                res["why"] = (f"не удалось сделать бэкап сектора "
                              f"{off:#x}: {error}")
                return res
        if need_erase:
            ok, why = flash.erase_sector(off)
            if not ok:
                res["failed_at"] = off
                res["why"] = f"стирание сектора {off:#x}: {why}"
                return res
            res["erased"] += 1
            # Blank check: после стирания сектор должен быть целиком 0xFF.
            # Если нет — сектор изношен, и запись в него ненадёжна.
            blank = flash.read(off, flash.sector)
            bad = [i for i, b in enumerate(blank) if b != 0xFF]
            if bad:
                res["failed_at"] = off + bad[0]
                res["why"] = (f"сектор {off:#x} не стёрся полностью: "
                              f"первый не-0xFF байт по адресу "
                              f"{off + bad[0]:#x} "
                              f"(читается {blank[bad[0]]:#04x}). "
                              f"Сектор изношен или защищён.")
                return res
        elif erase_first:
            ok, why = flash.erase_sector(off)
            if not ok:
                res["failed_at"] = off
                res["why"] = f"стирание сектора {off:#x}: {why}"
                return res
            res["erased"] += 1
            blank = flash.read(off, flash.sector)
            bad = [i for i, b in enumerate(blank) if b != 0xFF]
            if bad:
                res["failed_at"] = off + bad[0]
                res["why"] = (f"сектор {off:#x} не стёрся полностью: "
                              f"первый не-0xFF байт по адресу "
                              f"{off + bad[0]:#x} "
                              f"(читается {blank[bad[0]]:#04x}). "
                              f"Сектор изношен или защищён.")
                return res
        # запись: если микросхема умеет страницу (geo.page > 1) - страницами,
        # иначе по байту. Логика одна и та же, различается только размер куска.
        step = max(int(getattr(flash, "page_card", 1) or 1), 1)
        i = 0
        if bypass_active and writes:
            flash.enter_bypass()
        try:
            while i < len(writes):
                a = writes[i]
                if step == 1:
                    chunk = [a]
                else:
                    page_start = a - (a % step)
                    chunk = []
                    while i < len(writes) and writes[i] < page_start + step \
                            and len(chunk) < step:
                        chunk.append(writes[i])
                        i += 1
                    ok, why = flash.program_page(page_start, bytes(
                        image[k] if k in chunk else 0xFF
                        for k in range(page_start, page_start + step)))
                    if not ok:
                        # какой именно байт не записался: читаем страницу назад.
                        # Для человека это разница между «страница 0x0 не прошла»
                        # и «байт 0x10 изношен, остальные записались».
                        res["failed_at"] = chunk[0]
                        try:
                            # два чтения: первое после отказа может показать
                            # недостоверный DQ7 (это и есть признак отказа)
                            back = flash.read(page_start, step)
                            diff = [k for k in range(step)
                                    if back[k] != image[page_start + k]]
                            if diff:
                                # на железе недостоверный DQ7 держится до сброса
                                # (FF), поэтому «не сошёлся» может быть и байт,
                                # который на самом деле записался: берём ПЕРВЫЙ
                                # несошедшийся и говорим об этом прямо
                                res["failed_at"] = page_start + diff[0]
                                res["fail_span"] = (page_start + diff[0],
                                                   page_start + diff[-1])
                                res["fail_count"] = len(diff)
                        except (DirectError, OSError):
                            pass
                        res["why"] = (f"запись страницы {page_start:#x} "
                                      f"(байт {res['failed_at']:#x}): {why}")
                        return res
                    res["bytes"] += len(chunk)
                    continue
                ok, why = flash.program_byte(chunk[0], image[chunk[0]])
                if not ok:
                    res["failed_at"] = chunk[0]
                    res["why"] = f"запись байта {chunk[0]:#x}: {why}"
                    return res
                res["bytes"] += 1
                i += 1
        finally:
            if bypass_active and writes:
                flash.exit_bypass()
        # Сектор записан полностью — фиксируем в журнале (если ведётся).
        if journal:
            journal_append(journal, {
                "stage": "sector_done",
                "sector": off,
                "no": no,
                "total": total,
                "bytes": res["bytes"],
                "erased": res["erased"],
            })
    if verify:
        if progress:
            progress("сверка: читаю карту обратно", 0.98)
        # Перед любой сверкой сбрасываем чипы: после записи (особенно после
        # неудачной) они могут находиться в статусе, а не в чтении массива.
        flash.reset()
        got = flash.read(0, len(image))
        if got != image:
            diff = [i for i in range(len(image)) if got[i] != image[i]]
            res["failed_at"] = diff[0]
            res["why"] = (f"после записи не совпало {len(diff)} байт, первый - "
                          f"{diff[0]:#x} ({got[diff[0]]:#04x} вместо "
                          f"{image[diff[0]]:#04x})")
            res["fail_count"] = len(diff)
            if flash.protocol == "jedec":
                res["why"] += ("; при отказе записи DQ7 недостоверен, пока не "
                               "послан сброс, поэтому число байт может быть "
                               "завышено")
            return res
    res["ok"] = True
    return res


def flash_read_image(flash, size, progress=None):
    """Прочитать образ целиком тем же окном (для сверки и для проб)."""
    out = bytearray()
    step = max(flash.bus.span, PAGE)
    done = 0
    while done < size:
        n = min(step, size - done)
        out += flash.bus.rd_block(done, n)
        done += n
        if progress:
            progress(done, size)
    return bytes(out)


def _flash_open(args, want_write=True, probe=True):
    """Открывает окно и готовит NorFlash. Возвращает (card_parts) или бросает.

    probe=False - не подавать в карту НИ ОДНОЙ команды: только паспорт и
    значения из таблицы. Это нужно ключу --identify-chip --chip ИМЯ.
    """
    dev, exca, wins, _bv, _bd = _bridge_ctx()
    chips_hint = int(args.chips or 0)
    chips, size = card_geometry(chips_hint)
    if chips_hint:
        chips = chips_hint
    win = choose_window(wins, size or 0)
    mem, src, _err = open_best_mem(dev)
    span = window_span(win, None)
    limit = 0
    if size:
        limit = size
    elif chips:
        limit = chips * (2 << 20)          # 2 МиБ на чип этой семьи
    bus = NorBus(mem, exca, win, span=span, dev_id=dev, writable=want_write,
                 limit=limit)
    # сначала «как по паспорту», потом уточняем у самой микросхемы
    try:
        _raw, _tup, info = read_cis(mem=None, verbose=False)
    except DirectError:
        info = {}
    pre = NorFlash(bus, chips=chips, sector=SECTOR)
    if probe:
        geo = identify_chip(pre, info=info, force=args.chip or None)
    else:
        name = (args.chip or chip_name_from_info(info or {}) or "").strip()
        g0 = geometry_for(name, size=size or None)
        geo = dict(name=name or "неизвестная", sector=g0["sector"],
                   sector_chip=g0["sector"], cmd=g0["cmd"], wide=g0["wide"],
                   size=g0["size"], chips=chips or 0, volts=g0["volts"],
                   protocol=g0.get("protocol", "jedec"),
                   page=g0.get("page", 1),
                   has_write_buffer=bool(g0.get("page", 1) > 1),
                   how="взято из таблицы по --chip (карта не опрашивалась)",
                   autodetect="")
    if geo.get("chips"):
        chips = geo["chips"]
    if geo.get("size") and not size:
        size = geo["size"]
        bus.limit = size
    if geo.get("size"):
        bus.limit = geo["size"]
    protocol = geo.get("protocol", "jedec")
    # geo["sector"] у identify_chip уже умножен на число чипов (адреса карты)
    sector_card = geo.get("sector") or SECTOR
    flash = NorFlash(bus, chips=chips, sector=sector_card, cmd=tuple(geo["cmd"]),
                     wide=geo["wide"], page=geo.get("page") or 1,
                     bypass=bool(geo.get("bypass")),
                     name=geo.get("name", ""), protocol=protocol)
    return {"dev": dev, "exca": exca, "wins": wins, "win": win, "mem": mem,
            "bus": bus, "flash": flash, "chips": chips, "size": size,
            "src": src, "geo": geo, "info": info}


def _flash_report(parts, extra_lines=()):
    """Что известно о карте перед записью - чтобы человек видел, куда лезет."""
    chips, size = parts["chips"], parts["size"]
    geo = parts.get("geo") or {}
    cmd = geo.get("cmd") or (NOR_CMD_ADDR1, NOR_CMD_ADDR2)
    print(f"  микросхема            : {geo.get('name', 'неизвестная')} "
          f"({geo.get('how', '?')})")
    vol = geo.get("volts")
    volt_text = {5: "5 В (12 В не нужно)",
                 12: "12 В (Vpp) — слот столько не даёт",
                 3: "3 В"}.get(vol, "неизвестно")
    print("  напряжение программир.: " + volt_text)
    print(f"  микросхем в карте     : {chips} (байт i лежит в чипе i % {chips}"
          + (", то есть чётный/нечётный)" if chips == 2 else ")"))
    print(f"  ёмкость по паспорту   : "
          f"{size if size else 'неизвестна'}"
          + (f" ({size / 1024 / 1024:.1f} МиБ)" if size else ""))
    card_sec = geo.get("sector") or (SECTOR * max(int(chips), 1))
    chip_sec = geo.get("sector_chip") or -(-card_sec // max(int(chips), 1))
    print(f"  сектор стирания       : {card_sec // 1024} КиБ в адресах карты "
          f"({chip_sec // 1024} КиБ на чип x {chips} чип.)"
          + ("" if chip_sec == 64 << 10 else "  — НЕ 64 КиБ, учтено"))
    print("  протокол команд       : "
          + ("CUI Intel 28F (20h/D0h, статусный регистр 70h)"
             if geo.get("protocol") == "intel"
             else "JEDEC (AA/55, A0h, 80h/30h, DQ7/DQ5)"))
    print(f"  адреса команд         : {cmd[0]:#x} / {cmd[1]:#x}"
          + (", команда СЛОВОМ (Intel 28F)" if geo.get("wide") else ", командой байт")
          + (f", страница {geo.get('page')} байт" if (geo.get("page") or 1) > 1 else ""))
    if geo.get("autodetect"):
        ad = geo["autodetect"]
        bits = []
        if ad.get("manuf") is not None:
            bits.append(f"ID {ad['manuf']:#04x}:{ad['device']:#04x}")
        if ad.get("cmdset_name"):
            bits.append("CFI, набор команд " + ad["cmdset_name"])
        if ad.get("cfi_version"):
            bits.append("CFI v" + str(ad["cfi_version"]))
        for reg in ad.get("regions") or []:
            bits.append("CFI: %d x %d КиБ" % (reg["blocks"], reg["size"] // 1024))
        if ad.get("size_chip"):
            bits.append("CFI: ёмкость чипа %.1f МиБ"
                        % (ad["size_chip"] / 1024 / 1024))
        if ad.get("has_write_buffer") and ad.get("page") and ad["page"] > 1:
            bits.append(f"CFI: буфер записи {ad['page']} байт")
        if ad.get("timeout_erase_sector_us"):
            bits.append("CFI: стирание сектора до %.0f мс"
                        % (ad["timeout_erase_sector_us"] / 1000))
        if bits:
            print("  ответ микросхемы      : " + "; ".join(bits))
    print(f"  окно моста            : {parts['win']['start']:#x}, "
          f"{parts['win']['size'] / 1024 / 1024:.0f} МиБ, заход "
          f"{parts['bus'].span // 1024} КиБ")
    for line in extra_lines:
        print(line)


def _identify_chip_main(args):
    """--identify-chip: рассказать, что за микросхема, и НИЧЕГО НЕ ПИСАТЬ.

    Опрос без --chip подаёт в карту команды 90h/98h (это чтение, данные не
    меняются). С --chip карта не опрашивается вовсе: набор просто пересказывает
    таблицу - так можно посмотреть, что он будет делать, не трогая карту.
    """
    probe = not (args.chip and args.no_geometry_check)
    print("Опрос микросхемы (ничего не пишется и не стирается)")
    if not probe:
        print("  режим: по --chip, карта не опрашивается")
    parts = _flash_open(args, want_write=False, probe=probe)
    try:
        geo = parts["geo"]
        _flash_report(parts)
        ad = geo.get("autodetect") or {}
        man, dev = ad.get("manuf"), ad.get("device")
        if (man is None or dev is None) and probe:
            print("  ответ микросхемы      : молчит (карта не пропускает "
                  "команды 90h/98h) — параметры взяты по паспорту")
        sector = geo.get("sector") or 0
        size = parts.get("size") or 0
        print(f"  секторов на карте     : {size // sector if sector and size else 'неизвестно'}")
        warn = []
        if geo.get("volts") == 12:
            warn.append("микросхеме нужны 12 В на Vpp: в слоте ноутбука их нет, "
                        "запись с неё невозможна (для Kontron PC/104-PCMCIA-1 - "
                        "только если 12 В подведены по PC/104)")
        if geo.get("protocol") == "intel":
            warn.append("протокол Intel CUI: стирание только поблочно, "
                        "команды словом; статус читается регистром (70h)")
        if geo.get("name") in ("неизвестная", ""):
            warn.append("тип не определён: запись пойдёт по значениям по "
                        "умолчанию - сначала проба (--write-probe), при "
                        "сомнении укажите --chip")
        for w in warn:
            print(f"  ВНИМАНИЕ              : {w}")
        return 0
    finally:
        try:
            parts["bus"].close()
        except (DirectError, OSError, KeyError):
            pass
        try:
            parts["mem"].close()
        except (DirectError, OSError, KeyError):
            pass


def _flash_main(args):
    """--write-probe и --write: запись без драйвера."""
    parts = _flash_open(args)
    flash = parts["flash"]
    mem, exca, dev = parts["mem"], parts["exca"], parts["dev"]
    print("Прямая запись без драйвера (команды JEDEC через окно моста)")
    _flash_report(parts)
    snap = snapshot_window(mem, exca, dev)
    t0 = time.time()
    rc = 0
    info = parts.get("info") or {}
    where, why_where = cis_location(info)
    print(f"  паспорт карты         : {why_where}")
    # защита от записи по паспорту: если переключатель включён, стирание и
    # запись всё равно не пройдут - говорим сразу, а не после первой ошибки
    if any(d.get("wp") for d in info.get("devices") or []) and not getattr(
            args, "force_write", False):
        print("\nИТОГ: в паспорте отмечен переключатель защиты от записи (WPS).")
        print("  Проверьте переключатель на корпусе карты. Если он включён,")
        print("  стирание и запись физически не пройдут. Обойти это ключом")
        print("  --force-write можно, только если переключатель точно выключен,")
        print("  а паспорт просто старый.")
        return 4
    try:
        # ДО любой записи: та ли микросхема и тот ли сектор? Проверка активная
        # (стиранием последнего сектора), но не разрушающая: снимок сектора
        # снимается ДО стирания и возвращается после - внутри verify_geometry.
        if not getattr(args, "no_geometry_check", False) and parts["size"]:
            backup = None
            if getattr(args, "snapshot_dir", None):
                os.makedirs(args.snapshot_dir, exist_ok=True)
                backup = os.path.join(
                    args.snapshot_dir,
                    "geometry_probe_%#x.bin" % parts["size"])
            ok_g, why_g = verify_geometry(flash, {"sector": flash.sector,
                                                  "size": parts["size"]},
                                          backup_path=backup)
            print("  проверка геометрии    : " + ("ок - " if ok_g else "НЕ ТАК - ")
                  + why_g)
            if not ok_g:
                print("\nИТОГ: не начинаю стирание/запись - не сходится тип "
                      "микросхемы.")
                print("  Проверьте вручную: --chip <ИМЯ> или --sector <размер>. ")
                return 3

        if where == "array" and not getattr(args, "allow_cis_erase", False):
            img_path = getattr(args, "write", None)
            # образ задевает блок 0, если в начале образа есть не-0xFF байты
            touches = bool(img_path)
            if img_path:
                try:
                    with open(img_path, "rb") as f:
                        head = f.read(flash.sector)
                    touches = any(b != 0xFF for b in head)
                except OSError:
                    touches = True
            probe0 = (args.write_probe and int(args.sector or 0, 0) == 0) or touches
            if probe0:
                back = flash.read(0, flash.sector)
                has_cis = (b"CIS" in back or info.get("offset") is not None or
                           any(b != 0xFF for b in back))
                if has_cis:
                    print("\nВНИМАНИЕ: паспорт этой карты лежит в блоке 0 и "
                          "будет стёрт.")
                    print("  Набор сохранит копию блока 0 в файл, но вернуть "
                          "паспорт сможет только")
                    print("  если запись в карту работает. Если не сработает - "
                          "паспорт потерян.")
                    print("  Если это нежелательно: --sector <другой сектор> "
                          "или --allow-cis-erase (согласие заведомо).")
                    if not args.allow_cis_erase:
                        return 5

        if args.write_probe:
            off = int(args.sector, 0) if str(args.sector) else 0
            print(f"\nПРОБА: сектор {off:#x}. Данные сектора сохраняются и "
                  "возвращаются на место.")
            print("  (единственный способ узнать, пишется ли карта, не трогая "
                  "остальное)")
            res = flash_probe_safe(flash, off, backup_dir=args.snapshot_dir or ".",
                                   progress=lambda t, *a: print(f"  ... {t}"))
            print()
            if res["ok"]:
                print(f"ИТОГ: карта ПИШЕТСЯ. {res['why']}")
                print("  Теперь можно писать образ: --write <файл>")
            else:
                print(f"ИТОГ: карта НЕ пишется так, как ожидалось. {res['why']}")
                print("  Образ и остальные секторы не тронуты.")
                if res.get("backup"):
                    print(f"  Исходные байты сектора сохранены: {res['backup']}")
                if not res.get("restored"):
                    print("  ВЕРНУТЬ ИСХОДНЫЕ БАЙТЫ НЕ УДАЛОСЬ - на карте "
                          "испорчен один сектор, файл выше это копия.")
                    print("  Вероятные причины: карте нужно Vpp 12 В, сектор "
                          "защищён или изношен.")
            rc = 0 if res["ok"] else 3
            return rc

        img = args.write
        if not os.path.exists(img):
            print(f"ОШИБКА: нет файла {img}", file=sys.stderr)
            return 2
        data = open(img, "rb").read()
        if not data:
            print("ОШИБКА: файл пуст", file=sys.stderr)
            return 2
        if parts["size"] and len(data) > parts["size"]:
            print(f"ОШИБКА: образ {len(data)} байт больше карты "
                  f"({parts['size']} байт) - запись отменена", file=sys.stderr)
            return 2
        if len(data) % SECTOR:
            print(f"ОШИБКА: размер образа ({len(data)}) не кратен сектору "
                  f"{SECTOR}: так писать нельзя - неясно, что делать с "
                  "хвостом", file=sys.stderr)
            return 2
        if parts["size"] and len(data) < parts["size"]:
            left = parts["size"] - len(data)
            print(f"ВНИМАНИЕ: образ меньше карты на {left} байт; эти секторы "
                  "останутся как есть (набор их не стирает).")
        plan = flash_plan(data)
        need = sum(1 for _o, e, _w in plan if e)
        nbytes = sum(len(w) for _o, _e, w in plan)
        print(f"\nПлан: секторов всего {len(plan)}, из них стирать {need}, "
              f"записать непустых байт {nbytes} "
              f"(байты 0xFF не пишутся - после стирания они и так 0xFF)")
        import hashlib
        print(f"  md5 образа: {hashlib.md5(data).hexdigest()}")
        print("\nНачинаю. Это может занять минуты: стирание сектора - до "
              "нескольких секунд, байт пишется отдельной командой.")
        last = [0.0]

        def prog(text, frac=None):
            now = time.time()
            if frac is None or now - last[0] > 0.5:
                last[0] = now
                tail = f" ({frac * 100:.0f}%)" if frac is not None else ""
                print(f"  {text}{tail}", flush=True)

        # --- VPP: проверяем и включаем программирующее напряжение перед записью ---
        vpp = check_vpp_state(dev, mem, exca)
        if not vpp["ok"]:
            ok_vpp, msg_vpp = set_vpp_for_write(dev, voltage=5,
                                                exca_mem=mem,
                                                exca_addr=exca)
            print(f"  VPP: {msg_vpp}")
            if not ok_vpp:
                print("  ВНИМАНИЕ: VPP не включён. Запись может не пройти — "
                      "микросхеме нужно 5 В или 12 В на VPP.")
        else:
            print(f"  VPP: включён ({vpp['vpp']}, через {vpp['source']})")

        res = flash_write_image(flash, data, progress=prog,
                               verify=not args.no_verify,
                               resume=args.resume,
                               journal=args.journal or None,
                               remote_dir=args.kit_dir or "",
                               backup_dir=args.backup or None)
        print()
        if res["ok"]:
            print(f"ИТОГ: записано и СВЕРЕНО. Секторов стёрто {res['erased']}, "
                  f"байт записано {res['bytes']}.")
            if not args.no_verify:
                print("  Карта прочитана обратно целиком и совпала с образом "
                      "байт в байт.")
            print(f"  Время: {time.time() - t0:.1f} с")
            return 0
        print(f"ИТОГ: НЕ УДАЛОСЬ. {res['why']}")
        if res["failed_at"] is not None:
            print(f"  Остановились на смещении {res['failed_at']:#x}. "
                  "Образ (файл) цел; на карте изменены только секторы, "
                  "которые успели стереть и записать.")
        print("  Что это обычно значит: карте нужно Vpp 12 В, сектор защищён "
              "или изношен, либо контакт карты плохой.")
        return 3
    finally:
        try:
            flash.reset()
        except (DirectError, OSError):
            pass
        parts["bus"].close()
        if not restore_window(mem, exca, snap, dev):
            print("  ВНИМАНИЕ: окно моста вернуть не удалось. Если карта "
                  "перестала читаться - выключите и включите слот.")
        mem.close()


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Прямое чтение 16-битной PCMCIA linear flash через /dev/mem "
                    "(работает, когда ядро не дало карте окно памяти).",
        epilog="Чтение - всегда. С 2.14.0 есть и запись без драйвера: "
               "--write-probe (один сектор, со возвратом данных) и "
               "--write <образ>. Микросхемы бывают разные (JEDEC и Intel CUI, "
               "сектор 16/32/64/128 КиБ, 1..8 чипов): тип определяется по "
               "паспорту и ответу самой микросхемы, при сомнении - --chip. "
               "Напряжение программирования (Vpp) не выставляется никогда.")
    ap.add_argument("--probe", action="store_true", help="только проверить доступ")
    ap.add_argument("--cis", action="store_true",
                    help="прочитать CIS через attribute memory (паспорт карты)")
    ap.add_argument("--read", metavar="ФАЙЛ", help="снять образ карты в файл")
    ap.add_argument("--double", action="store_true", help="при --read: два чтения + сверка")
    ap.add_argument("--span", metavar="N", default="",
                    help="сколько байт читать за заход (по умолчанию 4 МиБ; "
                         "4096 - если большой заход не работает)")
    ap.add_argument("--trim", metavar="N", default="",
                    help="обрезать готовый образ до N байт (остальное - шина за картой)")
    ap.add_argument("--identify", action="store_true",
                    help="что за карта: linear flash, ATA, порт/сеть (по паспорту)")
    ap.add_argument("--image-report", metavar="ФАЙЛ",
                    help="сводка по уже снятому образу: размер, md5, доля 0xFF/0x00")
    ap.add_argument("--size", default="auto",
                    help="сколько читать при --read: auto (по повторению адресов, "
                         "по умолчанию) либо 1m/2m/4m/8m/16m/32m")
    ap.add_argument("--addr", help="адрес для точечного чтения, например 0x64000000")
    ap.add_argument("--len", default="256", help="сколько байт читать (по умолчанию 256)")
    ap.add_argument("--hex", action="store_true", help="печатать hex-дамп")
    ap.add_argument("--json", action="store_true", help="машинный вывод")
    ap.add_argument("--extra", action="store_true",
                    help="сырые факты об attribute memory (маркер CIS, строки, хвост)")
    ap.add_argument("--full", action="store_true",
                    help="в CIS-дампе печатать окно цепочки целиком (без --full "
                         "окно 160 байт; всю attribute memory не печатаем никогда)")
    ap.add_argument("--windows", metavar="ФАЙЛ",
                    help="файл с выводом lspci -v (для проверки без слота)")
    # --- запись без драйвера (2.14.0)
    ap.add_argument("--write-probe", action="store_true",
                    help="ПРОБА: стереть и записать ОДИН сектор, сверить, вернуть "
                         "исходные байты. Ничего другого не трогает")
    ap.add_argument("--sector", metavar="N", default="0",
                    help="какой сектор пробовать при --write-probe (по умолчанию 0)")
    ap.add_argument("--write", metavar="ФАЙЛ",
                    help="записать образ на карту без драйвера (команды JEDEC "
                         "через окно моста)")
    ap.add_argument("--chip", metavar="ИМЯ", default="",
                    help="тип микросхемы вручную (Am29F016, 28F008SA, ...), "
                         "если паспорт врёт и автоопределение не сработало")
    ap.add_argument("--chips", metavar="N", default="0",
                    help="сколько микросхем в паре (0 = взять из паспорта)")
    ap.add_argument("--no-verify", action="store_true",
                    help="при --write не читать карту обратно для сверки "
                         "(по умолчанию сверка обязательна)")
    ap.add_argument("--identify-chip", action="store_true",
                    help="только опрос микросхемы: паспорт, ID (90h), CFI (98h), "
                         "сектор, протокол, напряжение. НИЧЕГО НЕ ПИШЕТ")
    ap.add_argument("--allow-cis-erase", action="store_true",
                    help="разрешить стирание блока с паспортом карты "
                         "(паспорт лежит в блоке 0 массива)")
    ap.add_argument("--force-write", action="store_true",
                    help="писать, даже если в паспорте отмечен переключатель "
                         "защиты от записи (только если он заведомо выключен)")
    ap.add_argument("--kit-dir", default="",
                    help="папка набора на ноутбуке (для --journal по умолчанию "
                         "<папка>/.journal)")
    ap.add_argument("--no-geometry-check", action="store_true",
                    help="не проверять тип микросхемы стиранием крайнего сектора "
                         "(по умолчанию проверка делается всегда)")
    ap.add_argument("--resume", action="store_true",
                    help="продолжить прерванную запись с последнего сектора "
                         "(нужен --journal или <папка набора>/.journal)")
    ap.add_argument("--journal", metavar="ФАЙЛ", default="",
                    help="файл журнала записи на ноутбуке (по умолчанию "
                         "<папка набора>/.journal). Каждый завершённый сектор "
                         "фиксируется здесь, чтобы при обрыве можно было "
                         "продолжить с того же места.")
    ap.add_argument("--snapshot-dir", metavar="КАТАЛОГ", default="",
                    help="куда сохранять снимок сектора перед записью "
                         "(по умолчанию рядом с образом)")
    ap.add_argument("--backup", metavar="КАТАЛОГ", default="",
                    help="папка для резервных копий секторов перед стиранием. "
                         "Каждый сектор, который собираются стереть, сначала "
                         "читается и сохраняется в файл sector-<offset>.bin. "
                         "Позволяет восстановить сектор, если запись "
                         "прервётся или карта окажется изношенной.")
    args = ap.parse_args(argv)

    # сводка по готовому образу не требует ни слота, ни root: делаем сразу
    if args.trim:
        try:
            n = parse_size(args.trim)
        except DirectError as e:
            print(f"ОШИБКА: {e}", file=sys.stderr)
            return 2
        if not n:
            print("ОШИБКА: укажите размер числом, например --trim 4194304",
                  file=sys.stderr)
            return 2
        src = args.image_report or args.read or ""
        if not src or not os.path.exists(src):
            print("ОШИБКА: обрезать нечего - укажите файл: --trim 4194304 --image-report card.bin",
                  file=sys.stderr)
            return 2
        if os.path.getsize(src) <= n:
            print(f"Обрезать нечего: {src} уже {os.path.getsize(src)} байт "
                  f"(просили {n})")
            return 0
        out, got = trim_image(src, n)
        print(f"Обрезано: {src} -> {out} ({got} байт)")
        print("Исходный файл оставлен как есть.")
        return 0

    if args.identify:
        try:
            _raw, tuples, info = read_cis(None, verbose=False)
        except DirectError as e:
            print(f"ОШИБКА: {e}", file=sys.stderr)
            return 2
        kind, ktext = card_kind(info, tuples)
        if kind == "unknown" and not tuples:
            # пустая attribute memory: возможно, это ATA-карта
            try:
                dev_i, exca_i, wins_i, _bvi, _bdi = _bridge_ctx()
                win_i = choose_window(wins_i, 0)
                mem_i, _s, _e = open_best_mem(dev_i)
                try:
                    off_i, tp_i, inf_i = cis_in_common_mem(mem_i, exca_i, win_i,
                                                          dev_id=dev_i)
                finally:
                    mem_i.close()
                if tp_i:
                    kind, ktext = card_kind(inf_i, tp_i, at_off=off_i)
                    info = inf_i
            except DirectError:
                pass
        prod = " / ".join((info or {}).get("product") or []) or "нет строк"
        print(f"вид карты: {kind}")
        print(f"  {ktext}")
        print(f"  паспорт: {prod}")
        if (info or {}).get("manfid"):
            print(f"  MANFID : 0x{info['manfid'][0]:04x}, карта 0x{info['manfid'][1]:04x}")
        if kind in ("linear-flash", "memory", "ata"):
            print("  образ снимается: --read <файл> [--double]")
        elif kind == "unknown":
            print("  карта не опознана: пришлите вывод pcmcia_direct.py --cis")
        else:
            print("  это не носитель: читать можно паспорт, образ не нужен")
        return 0 if kind != "unknown" else 4

    if args.image_report:
        if not os.path.exists(args.image_report):
            print(f"ОШИБКА: файла {args.image_report} нет", file=sys.stderr)
            return 2
        st = image_stats(args.image_report)
        print("\n".join(image_report(
            {"path": args.image_report, "size": st["bytes"],
             "md5": hashlib_md5(args.image_report)},
            st, {}, "размер файла на диске")))
        return 0

    win_list = bridge_windows(args.windows)
    if win_list is None:
        return 2

    if not args.json:
        print("Окна памяти CardBus:")
        for i, w in enumerate(win_list):
            print(f"  окно {i}: {w['start']:#010x}-{w['end']:#010x} "
                  f"({w['size'] / 1024 / 1024:.1f} МиБ"
                  f"{', prefetchable' if w['prefetch'] else ''})")

    if args.probe:
        rep = probe(win_list, verbose=not args.json)
        if args.json:
            print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep["readable"] else 2

    if args.extra:
        try:
            attr, tuples, info = read_cis(None, verbose=False)
        except DirectError as e:
            print(f"ОШИБКА: {e}", file=sys.stderr)
            return 2
        print("\n".join(cis_extra_report(attr, info)))
        return 0

    if args.cis:
        # источник памяти выбирается внутри read_cis: /dev/mem, а если ядро
        # запрещает - окна моста через sysfs.
        # при --json печатаем ТОЛЬКО json (его разбирает раздел 7.1)
        try:
            attr, tuples, info = read_cis(None, verbose=not args.json)
            if args.json:
                print(json.dumps({"cis_raw": attr.hex(), "summary": info,
                                  "tuples": [{"code": t["code"], "name": t["name"],
                                              "len": t["len"]} for t in tuples]},
                                 ensure_ascii=False, indent=2))
                return 0
            if not passport_ok(info) and tuples:
                print("\nЦепочка найдена, но в ней нет ни названия (VERS_1),")
                print("  ни MANFID, ни DEVICE - это ещё не паспорт.")
                print("  Скорее всего цепочка начинается раньше/позже и её надо")
                print("  искать с другого смещения. Пришлите первые 64 байта.")
                return 4
            show = cis_dump(attr, info)
            print("\nAttribute memory:")
            for line in show:
                print("  " + line)
            if not tuples:
                blank = info.get("blank")
                # Прежде чем говорить «карта не отвечает», показать факты:
                # включено ли окно, кто его мог занять. Иначе совет «протрите
                # контакты» даётся в случае, когда карта тут вообще ни при чем.
                try:
                    dev2, exca2, wins2, _bv2, _bd2 = _bridge_ctx()
                    diag_lines, suspect = window_access_diag(dev2, exca2,
                                                             choose_window(wins2, 0))
                    if diag_lines:
                        print("\nДоступ к окну (факты, до выводов о карте):")
                        for ln in diag_lines:
                            print(ln)
                except DirectError:
                    suspect = False
                # Если окно выключено или его явно занял драйвер моста, то
                # «карта не отвечает» - НЕВЕРНЫЙ вывод: мы до карты не дошли.
                # Раньше suspect вычислялся и просто печатался. Теперь он
                # останавливает вывод советов про контакты: пусть сначала
                # выгрузят драйвер, иначе человек будет чистить контакты
                # карты, которая в этот момент ни при чем.
                if suspect:
                    print("\nОСТАНОВ: похоже, окно памяти занято драйвером моста,")
                    print("  поэтому вывод «карта не отвечает» делать нельзя -")
                    print("  набор до карты не дошёл. Что сделать (одна строка):")
                    print("    sudo modprobe -r yenta_socket")
                    print("  и сразу повторить эту команду. Если после выгрузки")
                    print("  паспорт прочитался - вопрос был в драйвере, а не в карте.")
                    print("  Вернуть драйвер обратно: sudo modprobe yenta_socket")
                    return 3
                # attribute memory пуста: у ATA-карт паспорт лежит в common
                # memory (0x200/0x400) - проверить это ДО выводов о контактах
                if blank in ("ff", "00"):
                    off_c, tp_c, inf_c = None, [], {}
                    try:
                        dev_c, exca_c, wins_c, _bvc, _bdc = _bridge_ctx()
                        win_c = choose_window(wins_c, 0)
                        mem_c, _src_c, _err_c = open_best_mem(dev_c)
                        try:
                            off_c, tp_c, inf_c = cis_in_common_mem(
                                mem_c, exca_c, win_c, dev_id=dev_c)
                        finally:
                            mem_c.close()
                    except DirectError:
                        tp_c = []
                    if tp_c:
                        kind, ktext = card_kind(inf_c, tp_c, at_off=off_c)
                        print(f"\nНайдена карта типа «{kind}»: {ktext}")
                        if inf_c.get("product"):
                            print("  продукт (VERS_1)      : "
                                  + " / ".join(inf_c["product"][:4]))
                        if inf_c.get("manfid"):
                            print(f"  производитель (MANFID): "
                                  f"0x{inf_c['manfid'][0]:04x}, "
                                  f"карта 0x{inf_c['manfid'][1]:04x}")
                        print("  Это не linear flash: attribute memory у неё пуста,")
                        print("  и это нормально. Образ снимается тем же чтением:")
                        print("    sudo python3 pcmcia_direct.py --read card.bin --size auto")
                        print("  ВНИМАНИЕ: для ATA-карты образ - это дамп всего")
                        print("  диска; файловая система внутри (FAT) этим набором")
                        print("  НЕ разбирается.")
                        return 0
                if blank == "ff":
                    print("\nattribute memory = сплошные 0xFF: карта НЕ отвечает.")
                    print("  Это не «стёртая карта» (у стёртой паспорт читается,")
                    print("  стирается только common memory). Ноль ответов там, где")
                    print("  должен быть паспорт, — это шина без карты на конце.")
                    print("  Что делать по порядку:")
                    print("    1) вынуть и вставить карту до щелчка (контакт не дожат);")
                    print("    2) протереть контакты ластиком, без спирта и абразива;")
                    print("    3) проверить, что карта сидит в верхнем слоте PC Card,")
                    print("       а не вставлена на половину;")
                    print("    4) вставить ДРУГУЮ 16-битную карту: если и она даёт 0xFF —")
                    print("       вопрос к слоту/мосту, а не к карте.")
                elif blank == "00":
                    print("\nattribute memory = сплошные 0x00: ответа нет,")
                    print("  но шина при этом «прижата» к нулю, а НЕ к 0xFF.")
                    print("  Для пустого слота PC Card это ненормально: без карты")
                    print("  линии обычно отдают 0xFF. Нули значат, что чтение не")
                    print("  доходит до карты. Порядок проверки - от частого к редкому:")
                    print("    1) драйвер моста занял окно (yenta_socket): выгрузить")
                    print("       его и повторить - modprobe -r yenta_socket;")
                    print("    2) карта не дожата в слоте: вынуть и вставить до щелчка;")
                    print("       паспорт (CIS) есть у ЛЮБОЙ живой карты, даже стёртой;")
                    print("    3) контакты карты протереть ластиком, без спирта;")
                    print("    4) вставить другую 16-битную карту: если и она даёт 0x00 -")
                    print("       вопрос к слоту/мосту, а не к карте.")
                elif info.get("stride") == 2 or info.get("jedec"):
                    print("\nПаспорт ВИДЕН: цепочка читается, но обрывается рано.")
                    print("  Признак 16-битной раскладки: байты CIS идут через один,")
                    print("  и в потоке найдены настоящие кортежи (JEDEC_C и др.),")
                    print("  но длина следующего кортежа выходит за прочитанное.")
                    print("  Это НЕ молчащая карта: обмен идёт и данные правильные.")
                    print("  Что делать: запустить с большим окном чтения (--len),")
                    print("  либо прислать первые 64 байта - подберём сдвиг точно.")
                    return 4
                elif info.get("strings"):
                    print("\nПаспорт ВИДЕН, но цепочка кортежей не собралась.")
                    print("  В attribute memory найдены строки:")
                    for x in info["strings"]:
                        print(f"    {x}")
                    print("  Значит карта отвечает и данные читаются - вопрос только")
                    print("  в поиске начала цепочки. Пришлите этот блок и первые")
                    print("  64 байта выше: подберём сдвиг/ширину точно.")
                    return 4
                else:
                    print("\nВ attribute memory нет цепочки кортежей, и содержимое не")
                    print("  однородное — карта что-то отдаёт, но не паспорт. Скорее")
                    print("  всего это не linear flash (модем/сетевая/ATA-карта), либо")
                    print("  окно настроено не туда. Пришлите первые 64 байта выше.")
                print("\nВажно: common memory (режим --probe) тут ничего не решает —")
                print("  у стёртой карты и у молчащей шины она выглядит одинаково.")
                return 3
            print(f"\nКортежи CIS ({len(tuples)}):")
            for t in tuples:
                body = t["body"][:8].hex()
                extra = " (обрезан)" if t.get("truncated") else ""
                print(f"  {t['name']:<20} код 0x{t['code']:02x}, "
                      f"длина {t['len']:2d}, данные: {body}{extra}")
            print("\nПаспорт карты:")
            if info["manfid"]:
                print(f"  производитель (MANFID): 0x{info['manfid'][0]:04x}, "
                      f"карта 0x{info['manfid'][1]:04x}")
            if info["product"]:
                print(f"  продукт (VERS_1)      : {' / '.join(info['product'][:4])}")
            if info["func"]:
                fname = {0: "multi", 1: "memory", 2: "serial", 3: "parallel",
                         4: "fixed disk", 5: "video", 6: "network",
                         7: "AIMS"}.get(info["func"], "?")
                print(f"  функция (FUNCID)      : {info['func']} ({fname})")
            if info["jedec"]:
                print(f"  JEDEC ID              : {info['jedec']}")
            for d in info["devices"]:
                mb = d["size"] / 1024 / 1024
                print(f"  устройство из DEVICE  : {d['type_name']}, "
                      f"{d['size']} байт ({mb:.1f} МиБ), доступ {d.get('speed', '?')}"
                      + (", есть переключатель защиты от записи" if d["wp"] else ""))
            for d in info.get("attr_devices") or []:
                # attribute memory - это EEPROM с паспортом (CIS). Если она
                # только для чтения, CIS переписать нельзя даже при 12 В.
                print(f"  паспортная память     : {d['type_name']}, "
                      f"{d['size']} байт"
                      + (", только чтение (ROM)" if d["type"] == 1
                         else ", перезаписываемая (EEPROM)"))
            if info.get("jedec_a"):
                print(f"  JEDEC паспортной EEPROM: {info['jedec_a']}")
            if info.get("geometry"):
                g2 = info["geometry"]
                print(f"  геометрия (DEVICEGEO) : сектор 2^{g2['sector_shift']} "
                      f"= {1 << g2['sector_shift']} байт, карта 2^{g2['card_shift']}, "
                      f"чип 2^{g2['chip_shift']}")
            if info.get("battery"):
                print(f"  батарея               : {info['battery']['kind']}")
            if info.get("vers2"):
                print(f"  версия CIS (VERS_2)   : {info['vers2'][0]}.{info['vers2'][1]}")
            kind, ktext = card_kind(info, tuples)
            print(f"  вид карты             : {kind} — {ktext}")
            for lvl, txt in cis_notes(info, tuples):
                mark = "ВНИМАНИЕ" if lvl == "warn" else "примечание"
                print(f"  {mark:<21}: {txt}")
            v, vwhy = vpp_need(info)
            if v == 5:
                print(f"  напряжение записи     : 5 В (12 В не нужно) — {vwhy}")
            elif v == 12:
                print(f"  напряжение записи     : 12 В (Vpp) — {vwhy}")
            else:
                print(f"  напряжение записи     : неизвестно — {vwhy}")
            if kind in ("serial", "parallel", "network"):
                print("  (это не носитель: паспорт читается, образ не нужен -")
                print("   флеш-образ с такой карты не снимается)")
            for line in cis_capacity(info, info.get("strings")):
                print(f"  {line}")
            prod = " ".join(info["product"] or []).upper()
            if "SERIES2-02" in prod and v != 5:
                print("\n  ВНИМАНИЕ: SERIES2-02 — Intel Series 2 (28F008SA). По"
                      " документации\n  это 5-вольтовая серия, но часть карт разводит"
                      " Vpp на 12 В.\n  Заранее не определить: проверяется пробной"
                      " записью одного сектора.")
            return 0
        except DirectError as e:
            print(f"ОШИБКА: {e}", file=sys.stderr)
            return 2

    try:
        mem = Mem()
    except DirectError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 2

    try:
        if args.addr:
            addr = int(args.addr, 0)
            length = int(args.len, 0)
            if length <= 0 or length > 64 * 1024 * 1024:
                print("ОШИБКА: --len должен быть от 1 до 64 МиБ", file=sys.stderr)
                return 2
            data = mem.read(addr, length)
            data, width = normalize_bankwidth(data)
            info = summarize(data)
            if args.json:
                info.update({"addr": addr, "width": width})
                print(json.dumps(info, ensure_ascii=False, indent=2))
                return 0
            print(f"Прочитано {info['bytes']} байт с {addr:#x} "
                  f"(ширина шины {width}):")
            print(f"  разных значений : {info['unique_values']}")
            print(f"  маркеры CIS     : "
                  f"{', '.join(hex(x) for x in info['cis_marks']) or 'нет'}")
            print(f"  строки          : {', '.join(info['strings'][:8]) or 'нет'}")
            print(f"  вывод           : {info['verdict']}")
            if args.hex:
                for off in range(0, min(len(data), 4096), 16):
                    row = data[off:off + 16]
                    txt = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
                    print(f"  {addr + off:08x}  {' '.join(f'{b:02x}' for b in row)}"
                          f"  |{txt}|")
            return 0 if not (info["empty_ff"] and info["unique_values"] == 1) else 3

        if args.identify_chip:
            return _identify_chip_main(args)

        if args.write_probe or args.write:
            return _flash_main(args)

        if args.read:
            dev, exca, wins, _bv, _bd = _bridge_ctx()
            size = parse_size(args.size)
            win = choose_window(wins, size or 0)
            if size is not None and size > win["size"]:
                raise DirectError(f"просили {size / 1024 / 1024:.0f} МиБ, а окно "
                                  f"моста всего {win['size'] / 1024 / 1024:.0f} МиБ")
            mem2, src, _err = open_best_mem(dev)
            try:
                if size is None:
                    # ёмкость из паспорта: она важнее «повторения адресов»,
                    # иначе пустая шина за концом карты даёт 64 МиБ
                    expect, why = (None, "")
                    try:
                        _raw, _tp, _inf = read_cis(mem=None, verbose=False)
                        expect, why = cis_card_size(_inf)
                        if expect:
                            print(f"Ёмкость по паспорту: {expect} байт "
                                  f"({expect / 1024 / 1024:.0f} МиБ) — {why}")
                    except Exception:
                        # паспорт не прочитался - не беда: размер определится
                        # повторением адресов или первым пустым участком
                        expect, why = None, ""
                    size, how = find_card_size(mem2, exca, win, dev_id=dev,
                                               verbose=True, expect=expect)
                    print(f"Размер карты определён: {size} байт "
                          f"({size / 1024 / 1024:.1f} МиБ) — {how}")
                    if expect and size > expect:
                        print(f"  ВНИМАНИЕ: это больше паспортной ёмкости "
                              f"({expect} байт). Так бывает, когда в паспорте "
                              f"ошибка; тогда укажите размер вручную: "
                              f"--size {expect}")
                else:
                    how = f"размер задан вручную ({args.size})"
                # Печатаем ДО первой секунды работы: иначе на медленной карте
                # экран пустой и не отличить «работает» от «висит».
                passes = 2 if args.double else 1
                _span = parse_size(args.span) if args.span else None
                print(f"Чтение образа: {size} байт через окно {win['start']:#x}, "
                      f"кусками {window_span(win, _span) // 1024} КиБ, "
                      f"заходов {passes} (двойное чтение: "
                      f"{'да' if args.double else 'нет'})")
                print("  первая секунда уходит на настройку окна - это нормально")
                t0 = time.time()
                pace = {"pass": 0, "t": time.time(), "bytes": 0}

                def show(done, allb, pass_no=1):
                    # каждый заход считается отдельно: иначе на втором проходе
                    # скорость и остаток показываются от начала первого
                    if pass_no != pace["pass"]:
                        pace.update({"pass": pass_no, "t": time.time(),
                                     "bytes": 0})
                        if pass_no > 1:
                            sys.stderr.write(f"\r  заход {pass_no} из 2"
                                             f" (сверка)                  \n")
                    el = max(time.time() - pace["t"], 1e-6)
                    sp = (done - pace["bytes"]) / el / 1024 / 1024
                    sp = max(sp, 0.001)
                    pace["bytes"] = done
                    pct = int(done * 100 / allb) if allb else 100
                    rest = (allb - done) / (sp * 1024 * 1024)
                    sys.stderr.write(f"\r  {pct:3d}%  {done / 1024 / 1024:6.2f} из "
                                     f"{allb / 1024 / 1024:.0f} МиБ  {sp:5.2f} МиБ/с  "
                                     f"осталось ~{rest:4.0f} с ")
                    sys.stderr.flush()

                # тот же стоп-кран, что и в --cis: если окно выключено или его
                # занял драйвер, чтение даст мусор, а не карту
                try:
                    _dl, suspect_read = window_access_diag(dev, exca, win)
                    for _ln in _dl:
                        print(_ln)
                except DirectError:
                    suspect_read = False
                if suspect_read:
                    raise DirectError(
                        "окно памяти занято драйвером моста (или выключено): "
                        "сначала sudo modprobe -r yenta_socket, потом чтение. "
                        "Образ НЕ снимался, файл не создавался.")
                ok_win, why_win = card_window_check(mem2, exca, win, dev_id=dev)
                print(f"  окно: {why_win}")
                if not ok_win:
                    raise DirectError(
                        "окно памяти не даёт стабильного ответа: " + why_win +
                        ". Сначала контакты карты и слота (diag), потом чтение.")
                # что в начале карты: если там не 0xFF/0x00, значит на карте
                # есть данные, и образ снимать было смысл
                try:
                    head4k = _read_page(mem2, exca, win, 0, PROBE_LEN, dev_id=dev)
                    if looks_blank(head4k, 0xFF):
                        print("  в начале карты 0xFF (стёртая флеш)")
                    elif looks_blank(head4k, 0x00):
                        print("  в начале карты 0x00 (шина прижата)")
                    else:
                        print("  в начале карты есть данные (не 0xFF и не 0x00)")
                except DirectError:
                    pass
                res = read_card_image(mem2, exca, win, args.read, size,
                                      double=args.double, progress=show,
                                      dev_id=dev, verbose=True,
                                      span=parse_size(args.span) if args.span else None)
                sys.stderr.write("\n")
                dt = time.time() - t0
                print(f"Готово: {args.read} — {res['size']} байт, "
                      f"md5 {res['md5']}, {dt:.1f} c")
                rep = image_report(res, image_stats(args.read), win, how)
                if expect:
                    rep += trim_hint(args.read, expect)
                print("\n".join(rep))
                print("Дальше: посмотреть образ без карты — "
                      f"python3 pcmcia_gui.py --map-file {args.read}")
                return 0
            finally:
                mem2.close()

        # по умолчанию: окна + первые байты каждого окна
        mem_rep = probe(win_list, verbose=True)
        if not mem_rep["readable"]:
            return 2
        any_data = any(not s["blank_ff"] for s in mem_rep["samples"] if s.get("width"))
        for smp in mem_rep["samples"]:
            if smp.get("error"):
                continue
            data = bytes.fromhex(smp["hex"]) if smp["hex"] else b""
            found = strings(data) if data else []
            if found:
                print(f"  окно {smp['start']:#010x}, строки: {', '.join(found[:6])}")
        print("\nСледующий шаг: снять образ целиком — "
              "sudo python3 pcmcia_direct.py --read card.bin --double")
        return 0 if any_data else 3
    except DirectError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 2
    finally:
        mem.close()


def hashlib_md5(path):
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(blk)
    return h.hexdigest()


if __name__ == "__main__":
    sys.exit(main())
