# -*- coding: utf-8 -*-
"""Мостовое окно CardBus и /dev/mem: sysfs-окна, mmap, чтение памяти.

Нижний слой пакета: здесь нет ни одной команды микросхемы, только
доступ к физической памяти и разбор окон моста."""

import mmap
import os
import sys

from .errors import DirectError


PAGE = 4096                     # страница памяти: mmap требует кратного смещения


WIN_CHUNK = 1024 * 1024          # сколько отображаем за один mmap


#
# Почему 1 МиБ, а не 64 КиБ: на каждый mmap приходится close, и на медленной
# 16-битной карте затраты на это заметны - образ 4 МиБ из 64 кусков шёл
# десятки секунд. При куске 512 КиБ (конец окна окна моста) их 8.
PROGRESS_EVERY = 256 * 1024      # как часто писать проценты (не на каждый кусок)


FF = 0xFF


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


CIS_MAX = 4096


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


def looks_blank(data, byte):
    """Всё ли содержимое равно byte (0xFF у стёртой флешки, 0x00 у некоторых SRAM)."""
    return bool(data) and data.count(byte) == len(data)


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
