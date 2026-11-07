#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Работа с памятью: /dev/mem, файлы sysfs, mmap-окна моста CardBus.

Вынесен из pcmcia_direct.py в отдельный модуль.
"""

import glob
import mmap
import os
import subprocess

from .errors import DirectError

# --- константы памяти ---------------------------------------------------------
PAGE = 4096                     # страница памяти: mmap требует кратного смещения
WIN_CHUNK = 1024 * 1024         # сколько отображаем за один mmap
PROGRESS_EVERY = 256 * 1024     # как часто писать проценты (не на каждый кусок)
FF = 0xFF

# --- регистры ExCA моста (PCMCIA) ---------------------------------------------
CB_MEM_PAGE0 = 0x40             # старший байт PCI-адреса окна
I365_ADDRWIN = 0x06             # регистр управления окном памяти
I365_MEM0_START = 0x10          # начало окна (страница)
I365_MEM0_STOP = 0x12           # конец окна (страница)
I365_MEM0_OFF = 0x14            # смещение на карте + атрибут/общая память
I365_ENA_MEM0 = 0x01            # включить окно 0
I365_MEM_16BIT = 0x8000         # 16-битная шина
I365_MEM_REG = 0x4000           # attribute memory вместо common
CIS_WINDOW = 4096               # базовый размер окна CIS (4 КиБ)

# Сколько байт карты видно в окне за один заход.
MAX_SPAN = 4 << 20              # верхний предел захода
READ_SPAN = MAX_SPAN            # по умолчанию - сразу максимальный заход
STALL_TIMEOUT = 60              # сек без нового байта - остановка чтения



def _pci_addr_hi(addr):
    """Старший байт PCI-адреса окна (регистр CB_MEM_PAGE0)."""
    return (addr >> 24) & 0xff


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


_NOTICE = set()
_MEM_CACHE = {}


# --------------------------------------------------------------------------- окна моста CardBus (lspci)

def cardbus_windows(lspci_text=None):
    """Окна памяти моста CardBus из lspci -v (или из переданного текста).

    Возвращает список словарей: start, end, size, prefetch.
    Берутся только строки «Memory window N: AAAAAAAA-BBBBBBBB (prefetchable)».
    """
    if lspci_text is not None and os.path.isfile(lspci_text):
        with open(lspci_text, encoding="utf-8", errors="replace") as fh:
            lspci_text = fh.read()
    if lspci_text is None:

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


# --------------------------------------------------- окна моста через sysfs

def sysfs_bridge_dir(dev_id=None):
    """Каталог моста в sysfs: /sys/bus/pci/devices/0000:00:0c.0."""
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




def window_span(win, span=None):
    """Сколько байт карты читать за заход: span, но не больше окна моста."""
    want = min(span or READ_SPAN, MAX_SPAN)
    return max(CIS_WINDOW, min(want, win["size"]))



def choose_window(win_list, card_size=0):
    """Какое окно моста отдать карте, чтобы покрыть весь её объём."""
    if not win_list:
        raise DirectError("нет окон памяти CardBus")
    fitting = [w for w in win_list if w["size"] >= card_size]
    return fitting[0] if fitting else max(win_list, key=lambda w: w["size"])


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

__all__ = [
    "PAGE", "WIN_CHUNK", "PROGRESS_EVERY", "FF",
    "CB_MEM_PAGE0", "I365_ADDRWIN", "I365_MEM0_START", "I365_MEM0_STOP",
    "I365_MEM0_OFF", "I365_ENA_MEM0", "I365_MEM_16BIT", "I365_MEM_REG",
    "CIS_WINDOW", "MAX_SPAN", "READ_SPAN", "STALL_TIMEOUT",
    "cardbus_windows", "sysfs_bridge_dir", "sysfs_window_files",
    "sysfs_bar_for", "mem_for_addr", "say_once", "read_mem",
    "open_best_mem", "Mem",
    "_pci_addr_hi", "setup_cis_window", "restore_cis_window", "map_card_window",
]
