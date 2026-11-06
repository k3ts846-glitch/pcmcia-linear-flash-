#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PCMCIA Flash Tool 2.1 — работа с 16-битными PCMCIA linear flash картами (MTD).

2.1 — перепроверка 2.0: проверка фактически прочитанных/записанных байт,
устранение дедлока на большом выводе утилит, чистая уборка после сбоя,
проверка монтирования, безопасное Vpp. Подробно — AUDIT_2.1.txt.
2.2 — профильный разбор интерфейса: мгновенный запуск (полный опрос карты
в фоне), неблокирующая отмена, живучая очередь событий, безопасные
временные файлы, корректный выход при незавершённой операции.
Подробно — AUDIT_2.2.txt.
2.3 — скорость и удобство: карта блоков читается кусками по 1 МиБ (в 8 раз
меньше запусков утилит), режим без интерфейса --blocks / --map-file,
запоминание введённых значений и последней вкладки.
2.4 — защита от записи не того файла: отказ писать файл из 0x00, предупреждение
«образ уже совпадает с картой», показ первых байт и типа содержимого;
горячие клавиши (F1/F5/Ctrl+R/W/V/M/S/L/Esc) и автоперерисовка карты блоков
после записи.

Реализованные требования (аудит v1.0 -> 2.0):
  P0-1  Потокобезопасность: рабочие потоки НЕ трогают Tk, обмен через очередь.
  P0-2  Запись стирает всю карту (flashcp -A / flash_erase 0 0) — «хвост» не остаётся.
  P0-3  Отмена любой длительной операции (terminate процесса утилиты).
  P0-4  Проверка свободного места перед сохранением образа.
  P0-5  Защита от второго экземпляра (flock).
  P0-6  Проверка write-protect до операции.
  P1-7  Учёт нескольких регионов стирания.
  P1-8  Определение формата на карте (FTL100 / BNFTL / пусто).
  P1-9  Потоковая верификация (память O(1), прогресс по чанкам).
  P1-10 Отчёт об операции (TXT + JSON).
  P1-11 Проверка, что устройство действительно MTD.
  P1-12 Контекст ошибки (dmesg, pccardctl, lsmod, mtd info) в отчёте.
  P2-13 Прогресс по чанкам + индикатор неопределённой длительности.
  P2-14 Карта блоков (стёрт / занят / сбойный), счётчики.
  P2-15 Профиль настроек (Vpp, erase-all, папки) в ~/.config.
  P2-16 Компактная раскладка под 1024x768.
  P2-17 Ротация журнала.
  P2-18 Проверка PATH (защита от подмены утилит).
  P2-19 Подтверждение вводом слова ERASE для стирания.
  P2-20 Чтение/запись произвольного диапазона адресов.

Запуск GUI:  sudo python3 pcmcia_gui.py
Проверки:   python3 pcmcia_gui.py --selftest | --doctor
Без графики: python3 pcmcia_gui.py --blocks            (состояние блоков карты)
             python3 pcmcia_gui.py --map-file card.bin (то же по локальному образу)
"""

import os
import sys
import re
import json
import shutil
import subprocess
import threading
import queue
import time
import hashlib
import tempfile
import fcntl
from datetime import datetime

APP_NAME = "PCMCIA Flash Tool"
APP_VER = "2.4"
MTD = os.environ.get("MTD", "/dev/mtd0")
LOG_DIR = "/var/log/pcmcia-flash-tool"
CFG_DIR = os.path.expanduser("~/.config/pcmcia-flash-tool")
LOCK_PATH = "/var/lock/pcmcia-flash-tool.lock"
BLOCK_DEFAULT = 128 * 1024     # 128 КиБ: реальный блок 16-МБ linear flash
BLOCK_DEFAULT_FILE = 64 * 1024  # блок для файла-тренировки (совпадает с mtd_debug-заглушкой)
CFG_OPTS = "/etc/pcmcia/config.opts"
MODPROBE_CONF = "/etc/modprobe.d/pcmciamtd.conf"
UDEV_RULES = "/etc/udev/rules.d/99-pcmciamtd.rules"
CHUNK = 1024 * 1024
PROBE_TIMEOUT = 15          # опрос карты: 15 с хватает любому живому драйверу
OP_TIMEOUT = 3600           # длинные операции (стирание/запись 32 МБ)
SIZE_16M = 16 * 1024 * 1024
SIZE_32M = 32 * 1024 * 1024
MTD_WRITEABLE = 0x400
FFS_HINT = "возможно FFS/данные прибора"


# ============================================================ базовые утилиты

class Cancelled(Exception):
    """Пользователь отменил операцию."""


def have(prog):
    return shutil.which(prog) is not None


def human(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    for unit in ("Б", "КиБ", "МиБ", "ГиБ"):
        if n < 1024 or unit == "ГиБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.2f} {unit}"
        n /= 1024.0
    return str(n)


def md5_of(path, limit=None):
    h = hashlib.md5()
    read = 0
    with open(path, "rb") as f:
        while limit is None or read < limit:
            b = f.read(min(CHUNK, (limit - read) if limit else CHUNK))
            if not b:
                break
            h.update(b)
            read += len(b)
    return h.hexdigest()


def private_tmpdir(prefix="pcmcia-flash-"):
    """Приватный каталог 0700 для временных файлов.

    tempfile.mktemp() возвращает ПРЕДСКАЗУЕМОЕ имя и не создаёт файл: на
    многопользовательской машине это окно для подмены/симлинка. Плюс при
    аварийном выходе файлы не убираются. Здесь каталог создаётся атомарно
    с правами 0700 и удаляется целиком.
    """
    return tempfile.mkdtemp(prefix=prefix, dir=os.environ.get("PCM_TMP") or None)


def dir_free(path):
    """Свободно байт в каталоге, куда пишем файл."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    try:
        st = os.statvfs(d)
        return st.f_bavail * st.f_frsize
    except OSError:
        return None


def path_is_sane():
    """Проверка PATH: не стоит ли раньше /usr/sbin каталог, доступный на запись."""
    bad = []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or not os.path.isdir(d):
            continue
        try:
            st = os.stat(d)
            if st.st_mode & 0o002 and not st.st_mode & 0o1000 and d not in ("/tmp", "/var/tmp"):
                bad.append(d)          # world-writable без sticky bit
        except OSError:
            pass
        if d in ("/usr/sbin", "/sbin", "/usr/bin", "/bin"):
            break
    return bad


def log_rotate(path, limit=1024 * 1024):
    try:
        if os.path.exists(path) and os.path.getsize(path) > limit:
            os.replace(path, path + ".1")
    except OSError:
        pass


def sh(ops, cmd, timeout=600):
    """Команда с отменой, если есть рабочий runner; иначе обычный запуск.

    apt-get/dpkg/modprobe/udevadm идут минуты. Пока они вызывались через
    run(), кнопка «Отмена» не действовала: процесс доигрывал до конца,
    а окно потом «дёргалось».
    """
    runner = getattr(ops, "runner", None)
    if runner is not None:
        return runner.exec(cmd, timeout=timeout)
    return run(cmd, timeout=timeout)


def run(cmd, timeout=120, input_text=None):
    """Простой запуск без отмены (для быстрых команд)."""
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), timeout=timeout,
                           input=input_text, capture_output=True, text=True,
                           errors="replace")
        return p.returncode, p.stdout or "", p.stderr or ""
    except FileNotFoundError as e:
        return 127, "", f"не найдено: {e}"
    except subprocess.TimeoutExpired:
        return 124, "", "таймаут выполнения"
    except Exception as e:                                        # noqa: BLE001
        return 1, "", str(e)


# ============================================================ отменяемый запуск

class Runner:
    """Выполняет внешние команды с возможностью отмены."""

    def __init__(self):
        self.cancel_ev = threading.Event()
        self.proc = None
        self._lock = threading.Lock()

    def cancel(self, wait=True):
        """Просит отменить операцию.

        wait=True — дождаться фактической остановки (до ~2 с, плюс до 5 с на
        SIGKILL). Из UI-потока так делать нельзя: окно замирает. Поэтому
        кнопка «Отмена» вызывает cancel(wait=False) и ставит флаг, а
        завершением занимается рабочий поток в exec().
        """
        self.cancel_ev.set()
        if not wait:
            return
        with self._lock:
            p = self.proc
        if p and p.poll() is None:
            try:
                p.terminate()
                for _ in range(20):
                    if p.poll() is not None:
                        break
                    time.sleep(0.1)
                if p.poll() is None:
                    p.kill()
            except OSError:
                pass

    def check(self):
        if self.cancel_ev.is_set():
            raise Cancelled()

    def exec(self, cmd, timeout=3600):
        """Запуск с поддержкой отмены. Возвращает (rc, stdout, stderr)."""
        self.check()
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, errors="replace")
        except FileNotFoundError as e:
            return 127, "", f"не найдено: {e}"
        with self._lock:
            self.proc = p
        # ВАЖНО: пайпы вычитываются отдельными потоками. Если читать их только
        # после завершения процесса, утилита, напечатавшая больше 64 КиБ
        # (размер буфера пайпа), навсегда блокируется на записи — был реальный
        # дедлок: процесс не завершается, а мы ждём его завершения.
        bufs = {"out": [], "err": []}
        sizes = {"out": 0, "err": 0}
        LIMIT = 4 * 1024 * 1024          # не растим память бесконечно

        def drain(stream, key):
            try:
                for line in iter(stream.readline, ""):
                    if sizes[key] < LIMIT:
                        bufs[key].append(line)
                        sizes[key] += len(line)
            except (ValueError, OSError):
                pass
            finally:
                try:
                    stream.close()
                except OSError:
                    pass

        readers = [threading.Thread(target=drain, args=(p.stdout, "out"), daemon=True),
                   threading.Thread(target=drain, args=(p.stderr, "err"), daemon=True)]
        for t in readers:
            t.start()
        t0 = time.time()
        try:
            while True:
                if p.poll() is not None:
                    break
                if self.cancel_ev.is_set():
                    try:
                        p.terminate()
                    except OSError:
                        pass
                    try:
                        p.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        p.kill()
                    raise Cancelled()
                if time.time() - t0 > timeout:
                    p.kill()
                    try:
                        p.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    return 124, "", "таймаут выполнения"
                time.sleep(0.05)
            for t in readers:
                t.join(timeout=5)
            # Гонка отмены: сигнал мог прийти, когда цикл выше уже увидел
            # завершение процесса, а процесс в этот момент был убит по нашей
            # же просьбе — код возврата не 0, а stderr пуст. Пользователю
            # показывалось «не удалось: без пояснений» вместо «отменено».
            if self.cancel_ev.is_set() and p.returncode != 0:
                raise Cancelled()
            return p.returncode, "".join(bufs["out"]), "".join(bufs["err"])
        finally:
            with self._lock:
                self.proc = None


# ============================================================ карта

class Card:
    """Состояние карты, слота и предупреждения. Без Tk — можно тестировать."""

    def __init__(self, dev=None, quick=False):
        self.dev = dev or MTD
        self.reset()
        self.refresh(full=not quick)

    def reset(self):
        self.present = False
        self.is_mtd = False
        self.simulated = False
        self.mtdtype = ""
        self.size = 0
        self.erasesize = 0
        self.regions = []
        self.flags = 0
        self.writeable = True
        self._flags_hint = None
        self.ident = {}
        self.status = ""
        self.info_raw = ""
        self.error = ""

    def _file_size(self):
        try:
            return os.path.getsize(self.dev)
        except OSError:
            return 0

    # ---------- чтение состояния
    def refresh(self, full=True):
        """full=False — только то, что можно узнать без запуска утилит.

        Опрос через mtd_debug/pccardctl занимает секунды, а на залипшем слоте
        упирается в таймаут. Из UI-потока такой вызов недопустим: окно
        перестаёт перерисовываться. Поэтому интерфейс всегда делает быстрый
        опрос, а полный — в фоновом потоке.
        """
        self.reset()
        self.present = os.path.exists(self.dev)
        name = os.path.basename(self.dev)
        sysdir = f"/sys/class/mtd/{name}"
        # обычный файл (не блочное устройство) — законный режим тренировки
        if self.present and not os.path.isdir(sysdir) and os.path.isfile(self.dev):
            try:
                import stat as _stat
                self.simulated = not _stat.S_ISBLK(os.stat(self.dev).st_mode)
            except OSError:
                self.simulated = False
        if os.path.isdir(sysdir):
            self.is_mtd = True
            for attr, cast in (("size", int), ("erasesize", int), ("flags", lambda v: int(v, 0))):
                try:
                    with open(os.path.join(sysdir, attr)) as fh:
                        setattr(self, attr, cast(fh.read().strip()))
                except (OSError, ValueError):
                    pass
            try:
                with open(os.path.join(sysdir, "type")) as fh:
                    self.mtdtype = fh.read().strip().upper()
            except OSError:
                pass
        if not full:
            if self.simulated and not self.size:
                self.size = self._file_size()
                self.erasesize = self.erasesize or BLOCK_DEFAULT_FILE
                self.mtdtype = self.mtdtype or "ФАЙЛ (тренировка)"
            return self
        if have("mtd_debug") and self.present:
            rc, out, err = run(["mtd_debug", "info", self.dev], timeout=PROBE_TIMEOUT)
            self.info_raw = (out + err).strip()
            if rc != 0:
                self.error = (err or out).strip()
            m = re.search(r"mtd\.size\s*=\s*(\d+)", self.info_raw)
            if m and not self.size:
                self.size = int(m.group(1))
            m = re.search(r"mtd\.erasesize\s*=\s*(\d+)", self.info_raw)
            if m and not self.erasesize:
                self.erasesize = int(m.group(1))
            m = re.search(r"mtd\.type\s*=\s*(\S+)", self.info_raw)
            if m and not self.mtdtype:
                self.mtdtype = m.group(1)
            m = re.search(r"mtd\.flags\s*=\s*(.+)", self.info_raw)
            if m:
                fl = m.group(1).strip()
                # mtd_debug печатает символьные имена: "MTD_CAP_NORFLASH" =
                # MTD_WRITEABLE|MTD_BIT_WRITEABLE. Подстроки "WRITEABLE" там нет!
                self._flags_hint = ("WRITEABLE" in fl or "MTD_CAP_NORFLASH" in fl
                                    or "MTD_CAP_RAM" in fl)
            for rm in re.finditer(r"region\[(\d+)\]\.erasesize\s*=\s*(\d+)", self.info_raw):
                self.regions.append(int(rm.group(2)))
        if self.simulated and not self.size:
            # тренировка на файле: размер = размер файла, блок = 64 КиБ
            self.size = self._file_size()
            self.erasesize = self.erasesize or BLOCK_DEFAULT_FILE
            if not self.mtdtype:
                self.mtdtype = "ФАЙЛ (тренировка)"
        if not self.size and self.present:
            rc, out, _ = run(["blockdev", "--getsize64", self.dev], timeout=PROBE_TIMEOUT)
            if rc == 0 and out.strip().isdigit():
                self.size = int(out.strip())
        # Приоритет: числовые флаги из sysfs (точные). Иначе — эвристика по
        # символьному выводу mtd_debug, иначе считаем карту доступной для записи
        # (нельзя блокировать работу ложным срабатыванием: WP-переключатель
        #  MTD всё равно не отражает, отказ придёт от flash_erase).
        if self.flags:
            self.writeable = bool(self.flags & MTD_WRITEABLE)
        elif getattr(self, "_flags_hint", None) is not None:
            self.writeable = bool(self._flags_hint)
        else:
            self.writeable = True
        if have("pccardctl"):
            _, out, _ = run(["pccardctl", "ident"])
            for line in out.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    self.ident[k.strip()] = v.strip().strip('"')
            _, out, _ = run(["pccardctl", "status"])
            self.status = out.strip()

    # ---------- паспорт
    def card_kind(self):
        blob = " ".join((self.ident.get("PRODID_1", ""), self.ident.get("PRODID_2", ""),
                         self.ident.get("PRODID_3", ""))).lower()
        if not blob.strip():
            return ("неизвестно", "CIS не прочитан — карта может быть «анонимной»")
        if "s2e" in blob or "series2+" in blob or "series 2+" in blob:
            return ("Intel Series 2+ / Value", "Запись от 5 В — должна работать.")
        if "series2-" in blob or "series 2-" in blob:
            # Intel Series 2 - это 28F 5-вольтовая серия. По документации 12 В
            # ей не нужны, но у части карт Vpp разведён на 12 В, поэтому
            # обещать запись нельзя - только проверка пробным сектором.
            return ("Intel Series 2 (Vpp под вопросом)",
                    "Intel Series 2 (28F008SA): по документации 5 В, 12 В не нужны. "
                    "Но часть карт разводит Vpp на 12 В, поэтому заранее неизвестно — "
                    "проверяется пробной записью одного сектора.")
        if "value" in blob and "100" in blob:
            return ("Intel Value Series 100", "Запись от 5 В — должна работать.")
        if "amd" in blob:
            return ("AMD linear flash", "Обычно пишется от 5 В.")
        if "catalyst" in blob or "sharp" in blob or "mitsubishi" in blob:
            return ("Catalyst/Sharp linear flash", "Обычно пишется от 5 В.")
        if "sram" in blob:
            return ("SRAM-карта", "Не flash: данные держит батарейка (mem_type=1).")
        return ("linear flash (предположительно)", "Точную модель по CIS определить не удалось.")

    def warnings(self):
        w = []
        if not self.present:
            w.append(f"Карта не распознана: {self.dev} отсутствует. Вставьте карту (лучше до загрузки).")
            return w
        if not self.is_mtd and self.simulated:
            w.append(f"РЕЖИМ ТРЕНИРОВКИ: {self.dev} — обычный файл, не карта. "
                     "Операции выполняются на файле, реальная карта не затрагивается.")
        elif not self.is_mtd:
            w.append(f"{self.dev} не является MTD-устройством — операции заблокированы.")
        kind, note = self.card_kind()
        if kind.startswith("Intel Series 2 ("):
            w.append("Карта, вероятно, 12-вольтовая: чтение работает, запись/стирание — нет. "
                     "Это ограничение железа ноутбука.")
        if not self.writeable:
            w.append("Карта ЗАЩИЩЕНА от записи (переключатель на торце или флаг MTD). "
                     "Снимите блокировку перед записью.")
        if len(set(self.regions)) > 1:
            w.append("У карты несколько регионов стирания (" +
                     ", ".join(str(r) for r in sorted(set(self.regions))) +
                     " байт) — стирание выполняется целиком (flash_erase 0 0).")
        if self.size and self.size not in (SIZE_16M, SIZE_32M):
            w.append(f"Размер {human(self.size)} не похож на 16 или 32 МБ — "
                     "проверьте CIS (иначе force_size в опциях pcmciamtd).")
        return w

    # ---------- формат на карте
    def detect_format(self, runner=None):
        """Читает первые 512 байт и определяет разметку."""
        if not (self.present and self.size):
            return ("нет карты", "")
        d = private_tmpdir()
        tmp = os.path.join(d, "head.bin")
        try:
            if runner:
                rc, _, _ = runner.exec(["mtd_debug", "read", self.dev, "0", "512", tmp])
            else:
                rc, _, _ = run(["mtd_debug", "read", self.dev, "0", "512", tmp],
                               timeout=PROBE_TIMEOUT)
            if rc != 0 or not os.path.exists(tmp):
                return ("не прочитано", "")
            with open(tmp, "rb") as f:
                head = f.read(512)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        if head.startswith(b"FTL100"):
            return ("FTL (PCMCIA Flash Translation Layer)", "")
        if head.startswith(b"BNFTL"):
            return ("NFTL / TrueFFS (M-Systems)", "форматировать как FTL нельзя — нужен nftl_format")
        if head.startswith(b"\xff" * 512):
            return ("пусто (всё 0xFF)", "")
        if head.startswith(b"\x00" * 512):
            return ("нули", "")
        return ("не определено (" + FFS_HINT + ")", "вероятно данные прибора/прошивка")

    # ---------- карта блоков
    @staticmethod
    def _classify(data, bs):
        out = []
        for i in range(0, len(data), bs):
            blk = data[i:i + bs]
            out.append("erased" if blk == b"\xff" * len(blk) else "used")
        return out

    def block_map(self, runner, progress=None):
        """Состояния блоков: 'erased' | 'used' | 'error'.

        Читаем карту крупными кусками (по 1 МиБ) и нарезаем в памяти:
        по одному запуску утилиты на блок получалось 128 запусков на 16-МБ
        карте — это лишние секунды на Celeron M при том же объёме чтения.
        """
        bs = self.erasesize or BLOCK_DEFAULT
        nblocks = self.size // bs if self.size else 0
        if not nblocks:
            return []
        step = max(bs, (1024 * 1024 // bs) * bs)      # кратно блоку
        d = private_tmpdir()
        tmp = os.path.join(d, "chunk.bin")
        res = []
        try:
            for start in range(0, self.size, step):
                runner.check()
                n = min(step, self.size - start)
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                rc, _, _ = runner.exec(["mtd_debug", "read", self.dev,
                                        str(start), str(n), tmp])
                if rc != 0 or not os.path.exists(tmp):
                    res.extend(["error"] * (n // bs))
                else:
                    with open(tmp, "rb") as f:
                        data = f.read()
                    if len(data) != n:
                        res.extend(["error"] * (n // bs))
                    else:
                        res.extend(self._classify(data, bs))
                if progress:
                    progress(min(100, int((start + n) * 100 / self.size)))
        finally:
            shutil.rmtree(d, ignore_errors=True)
        return res

    def block_map_from_file(self, path, progress=None):
        """Карта блоков по локальному образу — без обращения к карте.

        Только ОБЫЧНЫЙ файл: на /dev/zero, /dev/urandom, FIFO или каталоге
        чтение «до конца файла» не заканчивается никогда. Проверка размера
        тоже обязательна — образ может быть больше карты.
        """
        import stat as _stat
        try:
            st = os.stat(path)
        except OSError:
            return []
        if not _stat.S_ISREG(st.st_mode):
            return []
        bs = self.erasesize or BLOCK_DEFAULT
        res = []
        try:
            with open(path, "rb") as f:
                step = max(bs, (1024 * 1024 // bs) * bs)
                while True:
                    data = f.read(step)
                    if not data:
                        break
                    res.extend(self._classify(data, bs))
                    if progress:
                        total = max(1, self.size or os.path.getsize(path))
                        progress(min(100, int(f.tell() * 100 / total)))
        except OSError:
            return []
        return res


# ============================================================ проверка образа

FAT_MAGIC = (b"\xeb", b"\xe9")          # начало загрузочного сектора FAT/NTFS


def image_report(path, sample=64 * 1024):
    """Что за файл нам дают записывать — до того, как тронем карту."""
    import stat as _stat
    st = os.stat(path)
    if not _stat.S_ISREG(st.st_mode):
        raise RuntimeError(f"{path} — не обычный файл (устройство или FIFO), "
                           "записывать его как образ нельзя")
    rep = {"path": path, "size": st.st_size, "md5": md5_of(path),
           "head": "", "notes": [], "kind": "", "all_zero": False,
           "all_ff": False, "same_as_card": False}
    with open(path, "rb") as f:
        head = f.read(512)
        rep["head"] = " ".join(f"{b:02X}" for b in head[:16])

    # целиком ли файл из 0x00 / 0xFF — читаем кусками, память O(1)
    zeros = ones = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            zeros += b.count(0)
            ones += b.count(0xFF)
    rep["all_zero"] = zeros == rep["size"]
    rep["all_ff"] = ones == rep["size"]

    if rep["all_zero"]:
        rep["kind"] = "ПУСТОЙ (все байты 0x00)"
        rep["notes"].append("Такой файл почти всегда означает ошибку: выбран не тот "
                           "образ. Запись затрёт карту нулями.")
    elif rep["all_ff"]:
        rep["kind"] = "все 0xFF (чистая карта)"
        rep["notes"].append("Это равносильно стиранию: на карте не будет данных.")
    elif head.startswith(b"FTL100"):
        rep["kind"] = "образ с разметкой FTL"
    elif head.startswith(b"BNFTL"):
        rep["kind"] = "образ с разметкой NFTL/TrueFFS"
        rep["notes"].append("NFTL-образ на карту пишется, но проверять его потом "
                           "нужно через nftl, а не как сырые байты.")
    elif head[:1] in FAT_MAGIC or head[3:11] == b"MSDOS5.0":
        rep["kind"] = "похоже на FAT/с файловой системой"
    elif head.startswith(b"\xff" * 512):
        rep["kind"] = "начало стёрто (0xFF)"
    else:
        rep["kind"] = "двоичные данные (прошивка/дамп?)"
    return rep


def compare_with_card(card, path, runner=None, sample=64 * 1024):
    """Совпадает ли образ с тем, что уже на карте (по началу и концу).

    Полное побайтовое сравнение 16 МБ — это ещё одно чтение карты; для
    предупреждения «записывать нечего» достаточно краёв: если отличается
    начало или конец, файл заведомо другой.
    """
    size = min(os.path.getsize(path), card.size or 0)
    if size <= 0:
        return None
    d = private_tmpdir()
    tmp = os.path.join(d, "cmp.bin")
    same = True
    try:
        spans = [(0, min(sample, size))]
        if size > sample * 2:
            spans.append((size - sample, sample))
        with open(path, "rb") as f:
            for off, n in spans:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                cmd = ["mtd_debug", "read", card.dev, str(off), str(n), tmp]
                rc, _, _ = (runner.exec(cmd) if runner else run(cmd, timeout=PROBE_TIMEOUT))
                if rc != 0 or not os.path.exists(tmp):
                    return None
                with open(tmp, "rb") as t:
                    card_bytes = t.read(n)
                f.seek(off)
                if f.read(n) != card_bytes:
                    same = False
                    break
        return same
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================ операции

class Ops:
    """Все операции с картой. Не знают про Tk: пишут через log()/progress()."""

    def __init__(self, card, runner, log=lambda m: None, progress=lambda p: None,
                 slow=lambda text: None):
        self.card = card
        self.runner = runner
        self.log = log
        self.progress = progress
        self.slow = slow          # индикатор «идёт, длительность неизвестна»

    # ---------- служебное
    def _precheck(self, need_write=False):
        c = self.card
        c.refresh()
        if not c.present:
            raise RuntimeError(f"Карта не найдена ({c.dev})")
        if not c.is_mtd and not c.simulated:
            raise RuntimeError(f"{c.dev} не является MTD-устройством — операция отменена")
        if c.simulated:
            self.log(f"Тренировка на файле {c.dev} (реальная карта не затрагивается)")
        if need_write and not c.writeable:
            raise RuntimeError("Карта защищена от записи: снимите переключатель WP на торце карты")

    def mounted_on(self):
        """Где смонтировано устройство карты (или её mtdblock), если смонтировано."""
        devs = {self.card.dev}
        base = os.path.basename(self.card.dev)
        if base.startswith("mtd"):
            num = base[3:]
            devs |= {f"/dev/mtdblock{num}", f"/dev/ftl{num}"}
        try:
            with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    src = parts[0].split("/")[-1]
                    if parts[0] in devs or src in {d.split("/")[-1] for d in devs}:
                        return parts[1]
        except OSError:
            pass
        return None

    def _not_mounted(self):
        mnt = self.mounted_on()
        if mnt:
            raise RuntimeError(
                f"Устройство карты смонтировано в {mnt}. Запись «мимо» файловой "
                "системы её разрушит, а данные останутся в кэше. Сначала: "
                f"umount {mnt}")

    def _space(self, path, need):
        free = dir_free(path)
        if free is not None and free < need * 1.1:
            raise RuntimeError(f"Мало места в {os.path.dirname(path) or '.'}: "
                               f"свободно {human(free)}, нужно {human(int(need*1.1))}")

    # ---------- чтение образа с прогрессом и контролем
    def _read_chunk(self, offset, length, dest):
        """Читает ровно length байт с карты в файл dest.

        Ключевая проверка: mtd_debug (и любая утилита) может завершиться с
        кодом 0, но записать МЕНЬШЕ, чем просили. Без проверки размера образ
        молча получался усечённым, а программа рапортовала успех.
        """
        try:
            os.remove(dest)
        except OSError:
            pass
        rc, _, err = self.runner.exec(["mtd_debug", "read", self.card.dev,
                                       str(offset), str(length), dest])
        if rc != 0:
            raise RuntimeError(f"чтение на смещении {offset:#x} не удалось: "
                               f"{(err or '').strip() or 'без пояснений'}")
        got = os.path.getsize(dest) if os.path.exists(dest) else 0
        if got != length:
            raise RuntimeError(f"утилита прочитала {got} байт вместо {length} "
                               f"на смещении {offset:#x} — образ неполный, "
                               "чтение прервано (возможно, сбойный блок карты)")
        return dest

    def _region_offset(self, b):
        """Смещение и размер региона стирания, в который попадает блок b."""
        if not self.card.regions:
            bs = self.card.erasesize
            return b * bs, bs
        off = 0
        for size in self.card.regions:
            nb = max(1, size // (self.card.erasesize or size))
            if b < nb:
                return off + b * (self.card.erasesize or size), (self.card.erasesize or size)
            off += size
            b -= nb
        return self.card.size - self.card.erasesize, self.card.erasesize

    def read_image(self, path, double_check=True, progress_from=0, progress_to=100):
        self._precheck()
        c = self.card
        size = c.size
        need = size * (2 if double_check else 1)
        self._space(path, need)
        tmp = path + ".part"
        chk = path + ".chk"
        self.log(f"Чтение {c.dev} ({human(size)}) → {path}"
                 f"{' с контрольным чтением' if double_check else ''}")
        self.progress(progress_from)
        try:
            with open(tmp, "wb") as out:
                done = 0
                while done < size:
                    self.runner.check()
                    n = min(CHUNK, size - done)
                    self._read_chunk(done, n, tmp + ".chk")
                    with open(tmp + ".chk", "rb") as f:
                        data = f.read()
                    out.write(data)
                    out.flush()
                    done += n
                    self.progress(progress_from + int(done / size *
                                                      (progress_to - progress_from) *
                                                      (0.5 if double_check else 1.0)))
                out.flush()
                os.fsync(out.fileno())
            got = os.path.getsize(tmp)
            if got != size:
                raise RuntimeError(f"образ получился {human(got)} вместо {human(size)}")
            os.replace(tmp, path)
        except BaseException:
            # не оставляем мусор и недописанный файл, который можно принять за образ
            for junk in (tmp, tmp + ".chk"):
                try:
                    os.remove(junk)
                except OSError:
                    pass
            raise
        finally:
            try:
                os.remove(tmp + ".chk")
            except OSError:
                pass

        md5 = md5_of(path)
        self.log(f"Прочитано, MD5 {md5}")
        with open(path + ".md5", "w", encoding="utf-8") as fh:
            fh.write(f"{md5}  {os.path.basename(path)}\n")

        verdict = "одно чтение (без контроля)"
        if double_check:
            self.log("Контрольное чтение…")
            bad = None
            try:
                with open(chk, "wb") as out:
                    done = 0
                    while done < size:
                        self.runner.check()
                        n = min(CHUNK, size - done)
                        self._read_chunk(done, n, chk + ".part")
                        with open(chk + ".part", "rb") as f:
                            out.write(f.read())
                        done += n
                        self.progress(progress_from +
                                      int((0.5 + 0.5 * done / size) *
                                          (progress_to - progress_from)))
                for junk in (chk + ".part",):
                    try:
                        os.remove(junk)
                    except OSError:
                        pass
                with open(path, "rb") as a, open(chk, "rb") as b:
                    off = 0
                    while True:
                        x, y = a.read(CHUNK), b.read(CHUNK)
                        if x != y:
                            for i in range(min(len(x), len(y))):
                                if x[i] != y[i]:
                                    bad = off + i
                                    break
                            else:
                                bad = off
                            break
                        if not x:
                            break
                        off += len(x)
            except BaseException:
                bad = None
                raise
            if bad is None:
                verdict = "два чтения совпали — образ стабилен"
                try:
                    os.remove(chk)
                except OSError:
                    pass
            else:
                verdict = (f"ВНИМАНИЕ: чтения различаются с {bad:#x} — карта или "
                           f"контакты нестабильны, файл {os.path.basename(chk)} оставлен")
            self.log(verdict)
        self.progress(progress_to)
        return {"path": path, "size": size, "md5": md5, "verdict": verdict}

    # ---------- запись
    def write_image(self, path, erase_all=True, verify=True):
        self._precheck(need_write=True)
        self._not_mounted()
        c = self.card
        isz = os.path.getsize(path)
        if isz > c.size:
            raise RuntimeError(f"Образ больше карты: {human(isz)} > {human(c.size)}")
        self.log(f"Запись {path} ({human(isz)}) → {c.dev}"
                 f"{' с полным стиранием' if erase_all else ''}")
        self.progress(0)

        # 1) стирание
        if erase_all:
            self.slow("стирание всей карты")
            rc, out, err = self.runner.exec(["flash_erase", c.dev, "0", "0"])
            if rc != 0:
                raise RuntimeError(f"стирание не удалось: {(err or out).strip()}")
            self.log("Карта стёрта полностью")
        else:
            self.log("Стирание областей под образ (flashcp сделает сам)")

        # 2) запись
        self.slow("запись образа")
        cmd = ["flashcp"] + (["-A"] if erase_all else []) + [path, c.dev]
        rc, out, err = self.runner.exec(cmd)
        if rc != 0:
            raise RuntimeError(f"запись не удалась: {(err or out).strip()}")
        self.log("Запись завершена")
        self.progress(60)

        # 3) верификация
        result = {"path": path, "size": isz, "md5": md5_of(path), "verified": False}
        if verify:
            res = self.verify(path)
            result.update(res)
        tail = ""
        if erase_all and isz < c.size:
            tail = f"остаток карты ({human(isz)}…{human(c.size)}) стёрт"
        elif isz < c.size:
            tail = f"ВНИМАНИЕ: остаток карты ({human(isz)}…{human(c.size)}) НЕ стёрт"
            self.log(tail)
        result["tail"] = tail
        return result

    # ---------- верификация потоком
    def verify(self, path):
        self._precheck()
        c = self.card
        if not os.path.exists(path):
            raise RuntimeError(f"файла {path} нет")
        isz = os.path.getsize(path)
        if isz == 0:
            # раньше пустой файл «успешно совпадал» с картой: цикл не выполнялся
            raise RuntimeError("файл пустой — сверять нечего")
        if isz > c.size:
            raise RuntimeError(f"файл больше карты: {human(isz)} > {human(c.size)}")
        self.log(f"Сверка {path} ({human(isz)}) с {c.dev}")
        diffs = 0
        first = []
        d = private_tmpdir()
        tmp = os.path.join(d, "chunk.bin")
        try:
            with open(path, "rb") as f:
                done = 0
                while done < isz:
                    self.runner.check()
                    n = min(CHUNK, isz - done)
                    rc, _, err = self.runner.exec(["mtd_debug", "read", c.dev,
                                                   str(done), str(n), tmp])
                    if rc != 0:
                        raise RuntimeError(f"чтение при сверке не удалось: {err.strip()}")
                    a = f.read(n)
                    with open(tmp, "rb") as t:
                        b = t.read(n)
                    if len(b) != n:
                        # карта прочиталась короче, чем просили: это не «совпало»
                        raise RuntimeError(
                            f"при сверке прочитано {len(b)} байт вместо {n} "
                            f"на смещении {done:#x} — карта читается нестабильно")
                    if a != b:
                        for i in range(min(len(a), len(b))):
                            if a[i] != b[i]:
                                diffs += 1
                                if len(first) < 20:
                                    first.append(f"{done + i:#010x}: файл {a[i]:02X} / карта {b[i]:02X}")
                    done += n
                    self.progress(int(done * 100 / isz))
        finally:
            shutil.rmtree(d, ignore_errors=True)
        ok = diffs == 0
        if ok:
            self.log(f"Сверка: совпадает ({human(isz)} проверено)")
        else:
            self.log(f"Сверка: {diffs} различий, первое на {first[0] if first else '?'}")
        return {"verified": ok, "diffs": diffs, "first_diffs": first}

    # ---------- запись МИМО ядра (нет /dev/mtdN)
    def direct_script(self):
        """Путь к windows/legacy/pcmcia_direct.py рядом с программой."""
        here = os.path.dirname(os.path.abspath(__file__))
        for cand in (os.path.join(here, "windows", "legacy", "pcmcia_direct.py"),
                     os.path.join(here, "linux", "pcmcia_direct.py")):
            if os.path.exists(cand):
                return cand
        return ""

    def direct_probe(self):
        """Проба записи без драйвера: один сектор, данные возвращаются.

        Зачем: ядро не дало карте /dev/mtdN, поэтому flashcp писать некуда.
        Проба отвечает на единственный правильный вопрос - «пишется ли эта
        карта в этом слоте» - и не трогает остальное содержимое.
        """
        script = self.direct_script()
        if not script:
            raise RuntimeError("рядом нет windows/legacy/pcmcia_direct.py - проба невозможна")
        if self.card.simulated:
            raise RuntimeError("проба без драйвера на файле-тренажёре не имеет смысла")
        self.slow("проба записи: один сектор")
        self.progress(0)
        rc, out, err = self.runner.exec(
            [sys.executable, script, "--write-probe", "--sector", "0"])
        text = (out or "") + (err or "")
        self.log(text.strip()[-2000:] or "(нет вывода)")
        ok = rc == 0
        self.progress(100)
        self.log("Проба записи БЕЗ драйвера: " + ("карта пишется" if ok else "не пишется"))
        return {"ok": ok, "text": text}

    def direct_write(self, path):
        """Записать образ без драйвера (команды JEDEC через окно моста)."""
        script = self.direct_script()
        if not script:
            raise RuntimeError("рядом нет windows/legacy/pcmcia_direct.py - запись невозможна")
        if self.card.simulated:
            raise RuntimeError("запись без драйвера на файле-тренажёре не имеет смысла")
        if not os.path.exists(path):
            raise RuntimeError(f"нет файла {path}")
        self.slow("запись без драйвера")
        self.progress(0)
        rc, out, err = self.runner.exec(
            [sys.executable, script, "--write", path])
        text = (out or "") + (err or "")
        self.log(text.strip()[-4000:] or "(нет вывода)")
        self.progress(100)
        ok = rc == 0
        self.log("Запись БЕЗ драйвера: " + ("записано и сверено" if ok else "не удалось"))
        return {"ok": ok, "text": text}

    # ---------- стирание
    def erase(self):
        self._precheck(need_write=True)
        self._not_mounted()
        c = self.card
        self.slow("стирание всей карты")
        rc, out, err = self.runner.exec(["flash_erase", c.dev, "0", "0"])
        if rc != 0:
            raise RuntimeError(f"стирание не удалось: {(err or out).strip()}")
        self.progress(100)
        self.log("Карта стёрта (все блоки → 0xFF)")
        return True

    # ---------- тест записи
    def write_test(self, byte=0xFF):
        self._precheck(need_write=True)
        c = self.card
        size = c.size
        # файл размером с карту (16/32 МБ): раньше писался в /tmp без всякой
        # проверки места, а на live-системе /tmp бывает в памяти
        d = private_tmpdir()
        tmp = os.path.join(d, "fill.bin")
        self._space(tmp, size)
        self.log("Тест записи: карта будет стёрта и заполнена 0x%02X" % byte)
        try:
            with open(tmp, "wb") as f:
                left = size
                block = bytes([byte]) * CHUNK
                while left > 0:
                    n = min(CHUNK, left)
                    f.write(block[:n])
                    left -= n
            res = self.write_image(tmp, erase_all=True, verify=True)
            ok = res.get("verified", False)
            self.log("Тест записи пройден" if ok else "Тест записи НЕ пройден")
            return {"ok": ok, "diffs": res.get("diffs", 0)}
        finally:
            shutil.rmtree(d, ignore_errors=True)

    # ---------- диапазон
    def read_range(self, offset, length, path):
        self._precheck()
        c = self.card
        if offset < 0 or length <= 0:
            raise RuntimeError("Нужны неотрицательное смещение и длина больше нуля")
        if offset + length > c.size:
            raise RuntimeError(f"Диапазон выходит за пределы карты: "
                               f"{offset:#x}+{length:#x} > {c.size:#x}")
        self._space(path, length)
        self._read_chunk(offset, length, path)
        self.log(f"Прочитано {length} байт с {offset:#x} → {path}")
        return {"path": path, "offset": offset, "length": length, "md5": md5_of(path)}

    def write_range(self, offset, length, path):
        self._precheck(need_write=True)
        self._not_mounted()
        c = self.card
        if offset < 0 or length <= 0:
            raise RuntimeError("Нужны неотрицательное смещение и длина больше нуля")
        if offset + length > c.size:
            raise RuntimeError(f"Диапазон выходит за пределы карты: "
                               f"{offset:#x}+{length:#x} > {c.size:#x}")
        if not os.path.exists(path):
            raise RuntimeError(f"файла {path} нет")
        have_len = os.path.getsize(path)
        if have_len < length:
            raise RuntimeError(f"в файле {human(have_len)}, а записать надо "
                               f"{human(length)} — иначе на карту уйдёт обрывок")
        rc, _, err = self.runner.exec(["mtd_debug", "write", c.dev,
                                       str(offset), str(length), path])
        if rc != 0:
            raise RuntimeError(f"запись диапазона не удалась: {err.strip()} "
                               "(внутри блока flash нужно сначала стереть блок)")
        self.log(f"Записано {length} байт по смещению {offset:#x}")
        return {"offset": offset, "length": length}

    # ---------- FTL / FAT
    def make_ftl(self):
        self._precheck(need_write=True)
        self._not_mounted()
        c = self.card
        fmt, note = c.detect_format(self.runner)
        self.log(f"Формат на карте: {fmt} {note}")
        if fmt.startswith("NFTL"):
            raise RuntimeError("На карте NFTL/TrueFFS. Разметка FTL затрёт данные, "
                               "которые читаются через nftl. Сделайте дамп и используйте nftl_format.")
        rc, out, err = self.runner.exec(["ftl_format", "-i", c.dev])
        if rc != 0:
            raise RuntimeError(f"ftl_format не удался: {(err or out).strip()}")
        run(["modprobe", "ftl"])
        time.sleep(1)
        dev = "/dev/ftl0"
        if not os.path.exists(dev):
            raise RuntimeError("после разметки не появился /dev/ftl0")
        rc, out, err = run(["mkfs.vfat", dev])
        if rc != 0:
            raise RuntimeError(f"mkfs.vfat не удался: {(err or out).strip()}")
        self.log(f"FAT создан на {dev}")
        return {"dev": dev}

    # ---------- контекст ошибки
    def error_context(self):
        ctx = []
        c = self.card
        ctx.append(f"устройство: {c.dev}; present={c.present}; is_mtd={c.is_mtd}; "
                   f"size={c.size}; erasesize={c.erasesize}; regions={c.regions}")
        ctx.append(f"writeable={c.writeable}; flags={c.flags:#x}")
        for cmd in (["mtd_debug", "info", c.dev], ["pccardctl", "status"],
                    ["pccardctl", "ident"], ["lsmod"], "dmesg | tail -30"):
            if isinstance(cmd, str):
                rc, out, err = run(cmd)
            else:
                rc, out, err = run(cmd)
            txt = (out or err).strip()
            ctx.append(f"$ {' '.join(cmd) if isinstance(cmd, list) else cmd}\n{txt[:2000]}")
        return "\n\n".join(ctx)


# ============================================================ отчёт

def build_report(card, operation, result, error=None):
    kind, note = card.card_kind()
    data = {
        "tool": f"{APP_NAME} {APP_VER}",
        "when": datetime.now().isoformat(timespec="seconds"),
        "host": {
            "kernel": os.uname().release,
            "arch": run(["dpkg", "--print-architecture"])[1].strip(),
            "mtd_utils": run(["mtd_debug", "--version"])[1].strip().splitlines()[:1],
            "pcmciautils": run(["pccardctl", "--version"])[1].strip(),
        },
        "card": {
            "dev": card.dev, "is_mtd": card.is_mtd, "size": card.size,
            "erasesize": card.erasesize, "regions": card.regions,
            "mtd_type": card.mtdtype, "writeable": card.writeable,
            "kind": kind, "note": note,
            "cis": card.ident, "status": card.status,
        },
        "operation": operation,
        "result": result,
        "error": error,
    }
    return data


def save_report(data, base_path):
    txt_path = base_path + ".txt"
    json_path = base_path + ".json"
    lines = [f"{data['tool']}   {data['when']}", ""]
    lines.append("=== ХОСТ ===")
    for k, v in data["host"].items():
        lines.append(f"  {k}: {v}")
    lines.append("=== КАРТА ===")
    for k, v in data["card"].items():
        if k in ("cis", "status"):
            continue
        lines.append(f"  {k}: {v}")
    if data["card"]["cis"]:
        lines.append("  CIS:")
        for k, v in data["card"]["cis"].items():
            lines.append(f"    {k} = {v}")
    if data["card"]["status"]:
        lines.append("  слот: " + data["card"]["status"].replace("\n", " | "))
    lines.append("=== ОПЕРАЦИЯ ===")
    lines.append(f"  {json.dumps(data['operation'], ensure_ascii=False)}")
    lines.append("=== РЕЗУЛЬТАТ ===")
    lines.append(f"  {json.dumps(data['result'], ensure_ascii=False, default=str)}")
    if data.get("error"):
        lines.append("=== ОШИБКА ===")
        lines.append(data["error"])
    with open(txt_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, default=str)
    return txt_path, json_path


# ============================================================ профиль

def profile_path():
    return os.path.join(CFG_DIR, "profile.json")


def load_profile():
    try:
        with open(profile_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_profile(d):
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(profile_path(), "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ============================================================ GUI

def _has_tk():
    try:
        import importlib
        importlib.import_module("tkinter")
        return True
    except Exception:                                             # noqa: BLE001
        return False


if _has_tk():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, simpledialog, scrolledtext

    class UiBridge:
        """Объект, через который рабочий поток общается с окном. Только очередь."""

        def __init__(self, q):
            self.q = q

        def log(self, msg):
            self.q.put(("log", msg))

        def progress(self, pct):
            self.q.put(("progress", pct))

        def slow(self, text):
            self.q.put(("slow", text))

        def done(self, res):
            self.q.put(("done", res))

        def failed(self, err):
            self.q.put(("error", err))

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title(f"{APP_NAME} {APP_VER}")
            self.geometry("980x660")
            self.minsize(820, 560)
            self.q = queue.Queue()
            self.runner = None
            self.worker = None          # поток текущей ДЛИТЕЛЬНОЙ операции
            self.busy_op = False        # True только пока идёт операция с картой
            self.probe_thread = None    # отдельный поток опроса карты
            self._on_done = None
            self.profile = load_profile()
            # быстрый опрос: окно должно появиться мгновенно, полный опрос
            # карты уходит в фон (см. refresh_all -> _full_refresh)
            self.card = Card(quick=True)
            self._build()
            self.protocol("WM_DELETE_WINDOW", self.on_close)
            self._bind_keys()
            self.restore_ui_state()
            self.after(80, self._safe(self._pump))
            if getattr(self, "_closing", False):
                self._force_close()
                return
            self.refresh_all()

        # ---------------- сборка окна
        def _build(self):
            style = ttk.Style(self)
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
            style.configure("Ok.TLabel", foreground="#0a6b0a")
            style.configure("Bad.TLabel", foreground="#a00000")
            style.configure("Warn.TLabel", foreground="#8a5a00")

            bar = ttk.Frame(self, padding=(6, 4)); bar.pack(fill="x")
            self.lbl_card = ttk.Label(bar, text="Карта: —",
                                      font=("TkDefaultFont", 10, "bold"))
            self.lbl_card.pack(side="left")
            self.lbl_root = ttk.Label(bar, text="")
            self.lbl_root.pack(side="right")
            ttk.Button(bar, text="Обновить", command=self.refresh_all).pack(side="right", padx=6)

            self.nb = ttk.Notebook(self)
            self.nb.pack(fill="both", expand=True, padx=6)
            self.t_main = ttk.Frame(self.nb, padding=6)
            self.t_view = ttk.Frame(self.nb, padding=6)
            self.t_map = ttk.Frame(self.nb, padding=6)
            self.t_setup = ttk.Frame(self.nb, padding=6)
            self.t_log = ttk.Frame(self.nb, padding=6)
            self.nb.add(self.t_main, text="Карта")
            self.nb.add(self.t_view, text="Просмотр / правка")
            self.nb.add(self.t_map, text="Блоки")
            self.nb.add(self.t_setup, text="Настройка")
            self.nb.add(self.t_log, text="Журнал")

            self._build_main()
            self._build_view()
            self._build_map()
            self._build_setup()
            self._build_log()

            bottom = ttk.Frame(self, padding=(6, 2)); bottom.pack(fill="x", side="bottom")
            self.pb = ttk.Progressbar(bottom, mode="determinate", maximum=100)
            self.pb.pack(side="left", fill="x", expand=True)
            self.btn_cancel = ttk.Button(bottom, text="Отмена", width=12,
                                         command=self.cancel_op, state="disabled")
            self.btn_cancel.pack(side="left", padx=6)
            self.lbl_state = ttk.Label(bottom, text="Готово", width=44, anchor="w")
            self.lbl_state.pack(side="left")

        def _build_main(self):
            f = self.t_main
            box = ttk.LabelFrame(f, text="Состояние", padding=6)
            box.pack(fill="x")
            self.txt_state = tk.Text(box, height=8, wrap="word", relief="flat",
                                     background=self.cget("background"))
            self.txt_state.pack(fill="x")
            self.txt_state.configure(state="disabled")

            ops = ttk.LabelFrame(f, text="Операции", padding=6)
            ops.pack(fill="both", expand=True, pady=6)

            grid = ttk.Frame(ops); grid.pack(fill="x")
            buttons = [
                ("Идентификатор (CIS)", self.show_ident, 0, 0),
                ("Проверить слот", self.check_slot, 0, 1),
                ("Тест записи (0xFF)", self.test_write, 0, 2),
                ("Проба записи БЕЗ драйвера (1 сектор, данные возвращаются)",
                 self.direct_probe_dialog, 1, 0),
                ("Прочитать карту в файл…", self.read_card, 1, 0),
                ("Записать образ на карту…", self.write_card, 1, 1),
                ("Сверить файл с картой…", self.verify_card, 1, 2),
                ("Стереть карту…", self.erase_card, 2, 0),
                ("Сделать FAT-диск (FTL)…", self.make_ftl, 2, 1),
                ("Монтировать / отмонтировать", self.mount_toggle, 2, 2),
                ("Диапазон: прочитать…", self.range_read, 3, 0),
                ("Диапазон: записать…", self.range_write, 3, 1),
                ("Сохранить отчёт…", self.save_report_ui, 3, 2),
            ]
            for text, cmd, r, c in buttons:
                b = ttk.Button(grid, text=text, command=cmd)
                b.grid(row=r, column=c, sticky="ew", padx=2, pady=2)
                grid.columnconfigure(c, weight=1)

            opts = ttk.Frame(ops); opts.pack(fill="x", pady=(6, 0))
            self.var_erase_all = tk.BooleanVar(
                value=self.profile.get("erase_all", True))
            ttk.Checkbutton(opts, text="Стирать всю карту перед записью (рекомендуется)",
                            variable=self.var_erase_all).pack(side="left")
            self.var_verify = tk.BooleanVar(value=self.profile.get("verify", True))
            ttk.Checkbutton(opts, text="Проверять после записи",
                            variable=self.var_verify).pack(side="left", padx=10)

            self.lbl_ops = ttk.Label(ops, text="", style="Warn.TLabel")
            self.lbl_ops.pack(fill="x", pady=(4, 0))

        def _build_view(self):
            f = self.t_view
            bar = ttk.Frame(f); bar.pack(fill="x")
            ttk.Label(bar, text="Смещение (hex):").pack(side="left")
            self.var_off = tk.StringVar(value="00000000")
            ttk.Entry(bar, textvariable=self.var_off, width=10).pack(side="left", padx=3)
            ttk.Label(bar, text="Длина:").pack(side="left")
            self.var_len = tk.StringVar(value="256")
            ttk.Combobox(bar, textvariable=self.var_len, width=6, state="readonly",
                         values=["64", "128", "256", "512", "1024"]).pack(side="left", padx=3)
            ttk.Button(bar, text="Читать", command=self.view_read).pack(side="left", padx=3)
            ttk.Button(bar, text="‹", width=3, command=lambda: self.view_move(-1)).pack(side="left")
            ttk.Button(bar, text="›", width=3, command=lambda: self.view_move(1)).pack(side="left")

            self.txt_hex = tk.Text(f, font=("Courier", 9), wrap="none")
            self.txt_hex.pack(fill="both", expand=True, pady=4)

            eb = ttk.LabelFrame(f, text="Записать байты по смещению (hex: DE AD BE EF)", padding=4)
            eb.pack(fill="x")
            self.var_poke = tk.StringVar()
            ttk.Entry(eb, textvariable=self.var_poke).pack(side="left", fill="x", expand=True)
            ttk.Button(eb, text="Записать", command=self.poke).pack(side="left", padx=4)

        def _build_map(self):
            f = self.t_map
            bar = ttk.Frame(f); bar.pack(fill="x")
            ttk.Button(bar, text="Просканировать карту", command=self.scan_map).pack(side="left")
            self.lbl_map = ttk.Label(bar, text="")
            self.lbl_map.pack(side="left", padx=10)
            wrap = ttk.Frame(f)
            wrap.pack(fill="both", expand=True, pady=6)
            self.canvas = tk.Canvas(wrap, background="#202020", height=260,
                                    highlightthickness=0)
            vs = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
            self.canvas.configure(yscrollcommand=vs.set)
            vs.pack(side="right", fill="y")
            self.canvas.pack(side="left", fill="both", expand=True)
            self.canvas.bind("<Configure>", lambda e: self._safe(self._draw_map)())
            # подсказка: клик по блоку показывает его адрес и состояние
            self.canvas.bind("<Button-1>", self._map_click)

        def _build_setup(self):
            f = self.t_setup
            pk = ttk.LabelFrame(f, text="1. Утилиты", padding=6); pk.pack(fill="x")
            self.lbl_tools = ttk.Label(pk, text=""); self.lbl_tools.pack(anchor="w")
            r = ttk.Frame(pk); r.pack(fill="x", pady=3)
            ttk.Button(r, text="Установить утилиты", command=self.install_tools).pack(side="left")
            ttk.Button(r, text="Проверить снова", command=self.refresh_all).pack(side="left", padx=6)

            cf = ttk.LabelFrame(f, text="2. Слот и Vpp", padding=6); cf.pack(fill="x", pady=6)
            ttk.Label(cf, text="Vpp:").grid(row=0, column=0, sticky="w")
            self.var_vpp = tk.StringVar(
                value="3.3 В" if str(self.profile.get("vpp", "50")) == "33" else "5.0 В")
            ttk.Combobox(cf, textvariable=self.var_vpp, width=10, state="readonly",
                         values=["5.0 В", "3.3 В"]).grid(row=0, column=1, sticky="w", padx=4)
            ttk.Label(cf, text="только 5.0 или 3.3 В; 12 В (Vpp) слот не даёт",
                      style="Warn.TLabel").grid(row=0, column=2, sticky="w", padx=6)
            ttk.Button(cf, text="Настроить слот и загрузить драйверы",
                       command=self.configure_slot).grid(row=1, column=0, columnspan=3,
                                                         sticky="w", pady=4)
            self.txt_cfg = tk.Text(cf, height=6, wrap="word", relief="flat",
                                   background=self.cget("background"))
            self.txt_cfg.grid(row=2, column=0, columnspan=3, sticky="ew")

            ff = ttk.LabelFrame(f, text="3. Монтирование", padding=6); ff.pack(fill="x")
            self.var_mount = tk.StringVar(
                value=self.profile.get("mountpoint", "/mnt/card"))
            ttk.Label(ff, text="Точка:").pack(side="left")
            ttk.Entry(ff, textvariable=self.var_mount, width=18).pack(side="left", padx=4)

        def _build_log(self):
            bar = ttk.Frame(self.t_log); bar.pack(fill="x")
            ttk.Button(bar, text="Очистить", command=lambda: self.txt_log.delete("1.0", "end")).pack(side="right")
            ttk.Button(bar, text="Сохранить…", command=self.save_log).pack(side="right", padx=6)
            self.txt_log = scrolledtext.ScrolledText(self.t_log, font=("Courier", 9), wrap="word")
            self.txt_log.pack(fill="both", expand=True, pady=4)

        def _bind_keys(self):
            """Горячие клавиши: на этом ноутбуке тачпад неудобный, а мышь не всегда рядом."""
            keys = {
                "<F5>": self.refresh_all,
                "<Control-r>": self.read_card,
                "<Control-w>": self.write_card,
                "<Control-v>": self.verify_card,
                "<Control-s>": self.save_report_ui,
                "<Control-l>": lambda: self.nb.select(self.t_log),
                "<Control-m>": self.scan_map,
                "<Escape>": self.cancel_op,
                "<F1>": self.show_help,
            }
            for seq, fn in keys.items():
                try:
                    self.bind_all(seq, lambda e, f=fn: (self._safe(f)(), "break")[1])
                except Exception:                                 # noqa: BLE001
                    pass

        def show_help(self):
            self._window("Горячие клавиши", "\n".join([
                "F5            обновить состояние карты",
                "Ctrl+R        прочитать карту в файл",
                "Ctrl+W        записать образ на карту",
                "Ctrl+V        сверить файл с картой",
                "Ctrl+M        просканировать блоки",
                "Ctrl+S        сохранить отчёт",
                "Ctrl+L        перейти к журналу",
                "Esc           отмена текущей операции",
                "F1            эта справка",
                "",
                "Без интерфейса:",
                "  python3 pcmcia_gui.py --blocks",
                "  python3 pcmcia_gui.py --map-file card.bin",
                "  python3 pcmcia_gui.py --doctor",
            ]))

        # ---------------- живучесть UI
        def _safe(self, fn, *a):
            """Обёртка для after(): исключение не должно убивать очередь событий.

            Раньше любое исключение в обработчике (например, в отрисовке карты
            блоков) всплывало из _pump и, если Tk не перехватывал его сам,
            останавливало разбор очереди: программа «жива», но больше не
            обновляется и кнопки не реагируют.
            """
            def wrapped(*extra):
                try:
                    fn(*a, *extra)
                except Exception as e:                            # noqa: BLE001
                    try:
                        self._log_line(f"внутренняя ошибка интерфейса: {e}")
                        self.lbl_state.configure(text="Внутренняя ошибка — см. журнал")
                    except Exception:                             # noqa: BLE001
                        print(f"UI error: {e}", file=sys.stderr)
            return wrapped

        # ---------------- очередь событий (единственный путь в UI)
        def _pump(self):
            try:
                for _ in range(200):          # не съедаем UI-поток целиком
                    try:
                        kind, payload = self.q.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        if kind == "log":
                            self._log_line(payload)
                        elif kind == "progress":
                            self.pb.stop(); self.pb.configure(mode="determinate")
                            self.pb["value"] = max(0, min(100, payload))
                        elif kind == "slow":
                            self.pb.configure(mode="indeterminate"); self.pb.start(60)
                            self.lbl_state.configure(text=payload)
                        elif kind == "done":
                            self._op_finished(payload, None)
                        elif kind == "error":
                            self._op_finished(None, payload)
                        elif kind == "full":
                            self._apply_full(payload)
                    except Exception as e:                        # noqa: BLE001
                        # одно кривое сообщение не должно останавливать очередь
                        try:
                            self._log_line(f"ошибка обработки события «{kind}»: {e}")
                        except Exception:                         # noqa: BLE001
                            pass
            finally:
                self.after(80, self._safe(self._pump))

        def _log_line(self, msg):
            ts = datetime.now().strftime("%H:%M:%S")
            self.txt_log.insert("end", f"[{ts}] {msg}\n")
            self.txt_log.see("end")
            self.lbl_state.configure(text=msg[:50])
            try:
                os.makedirs(LOG_DIR, exist_ok=True)
                p = os.path.join(LOG_DIR, "pcmcia-flash-tool.log")
                log_rotate(p)
                with open(p, "a", encoding="utf-8") as fh:
                    fh.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
            except OSError:
                pass

        def _maybe_rescan(self, result):
            """После записи/стирания карта блоков устарела — обновляем сами.

            Раньше пользователь видел старую карту блоков, пока не нажмёт
            «Просканировать» вручную, и мог принять её за состояние после записи.
            """
            if not isinstance(result, dict):
                return
            changed = any(k in result for k in ("tail", "verified", "diffs")) \
                or result.get("action") == "erase"
            if changed and getattr(self, "_map_states", []):
                self.nb.select(self.t_map)
                self.scan_map()

        def _op_finished(self, result, error):
            self.busy_op = False
            self.pb.stop(); self.pb.configure(mode="determinate")
            if error:
                self.pb["value"] = 0
                self.lbl_state.configure(text="Ошибка: " + str(error)[:60])
                self._log_line("ОШИБКА: " + str(error))
                messagebox.showerror(APP_NAME, str(error))
            else:
                self.pb["value"] = 100
                self.lbl_state.configure(text="Готово")
            self.btn_cancel.configure(state="disabled")
            if not error and isinstance(result, dict) and result.get("warning") \
                    and not result.get("cancelled"):
                messagebox.showwarning(APP_NAME, result["warning"])
            if not error and isinstance(result, dict) and result.get("warning") \
                    and result.get("cancelled"):
                messagebox.showwarning(APP_NAME, result["warning"])
            if not error and result is not None:
                self._safe(self._maybe_rescan, result)()
            cb, self._on_done = self._on_done, None
            if cb and not error and result is not None:
                try:
                    cb(result)
                except Exception as e:                            # noqa: BLE001
                    self._log_line(f"ошибка обработки результата: {e}")
            self.refresh_all()

        # ---------------- запуск операций
        def _run(self, fn, opname, *args, on_done=None, **kwargs):
            if self.busy_op:
                messagebox.showwarning(APP_NAME, "Другая операция ещё выполняется.")
                return
            if os.geteuid() != 0:
                messagebox.showerror(APP_NAME, "Нужны права root:\n    sudo python3 pcmcia_gui.py")
                return
            self.runner = Runner()
            ui = UiBridge(self.q)
            card = self.card
            ops = Ops(card, self.runner, ui.log, ui.progress, ui.slow)

            def job():
                try:
                    res = fn(ops, *args, **kwargs)
                    rep = build_report(card, {"name": opname, "args": str(args)}, res or {})
                    base = os.path.join(tempfile.gettempdir(),
                                        f"pcmcia-report-{datetime.now():%Y%m%d-%H%M%S}")
                    try:
                        save_report(rep, base)
                        ui.log(f"Отчёт: {base}.txt")
                    except OSError:
                        pass
                    ui.done(res)
                except Cancelled:
                    ops.log("Операция ОТМЕНЕНА пользователем.")
                    ui.done({"cancelled": True,
                             "warning": "Карта может остаться в промежуточном состоянии: "
                                        "часть блоков записана, часть нет. "
                                        "Повторите запись образа целиком перед использованием."})
                except Exception as e:                            # noqa: BLE001
                    ctx = ""
                    try:
                        ctx = ops.error_context()
                        rep = build_report(card, {"name": opname}, {}, error=f"{e}\n\n{ctx}")
                        base = os.path.join(tempfile.gettempdir(),
                                            f"pcmcia-error-{datetime.now():%Y%m%d-%H%M%S}")
                        save_report(rep, base)
                        ui.log(f"Контекст ошибки сохранён: {base}.txt")
                    except Exception:                             # noqa: BLE001
                        pass
                    head = str(e).strip().splitlines()[0][:300] if str(e).strip() else "ошибка"
                    ui.failed(f"{head}\n\nПодробности — в журнале и в файле отчёта.")

            self.pb["value"] = 0
            self.btn_cancel.configure(state="normal")
            self.lbl_state.configure(text=opname)
            self._on_done = on_done
            self.busy_op = True
            self.worker = threading.Thread(target=job, daemon=True)
            self.worker.start()

        def cancel_op(self):
            if self.runner:
                self.runner.cancel(wait=False)   # не морозим окно
                self._log_line("Запрошена отмена…")

        # ---------------- состояние
        def refresh_all(self):
            # 1) мгновенно: то, что лежит в sysfs / легко считается
            quick = Card(self.card.dev if getattr(self, "card", None) else None, quick=True)
            self.card = quick
            self._render_state()
            self._render_tools()
            # 2) полный опрос — в фоне, иначе окно замирает на секунды (а при
            #    залипшем слоте — на десятки секунд), не успев даже нарисоваться
            if self.busy_op:
                return
            self._full_refresh()

        def _full_refresh(self):
            q = self.q

            def job():
                try:
                    c = Card()
                    q.put(("full", c))
                except Exception as e:                            # noqa: BLE001
                    q.put(("log", f"полный опрос карты не удался: {e}"))

            # отдельный поток: опрос карты не должен занимать «замок» операций,
            # иначе сразу после запуска окна кнопки на несколько секунд
            # отвечали «другая операция ещё выполняется»
            self.probe_thread = threading.Thread(target=job, daemon=True)
            self.probe_thread.start()

        def _apply_full(self, c):
            self.card = c
            self._render_state()
            self._render_tools()
            root_ok = os.geteuid() == 0
            self.lbl_root.configure(text="root" if root_ok else "НЕТ root (нужен sudo)",
                                    style="Ok.TLabel" if root_ok else "Bad.TLabel")

        def _render_state(self):
            c = self.card
            self.txt_state.configure(state="normal")
            self.txt_state.delete("1.0", "end")
            warns = c.warnings()
            if not c.present:
                self.lbl_card.configure(text="Карта: не найдена", style="Bad.TLabel")
                self.txt_state.insert("end", f"{c.dev} отсутствует.\n"
                                            "Вставьте карту и нажмите «Обновить».\n"
                                            "Если не помогает — вкладка «Настройка», пункт 2.\n")
            else:
                kind, note = c.card_kind()
                self.lbl_card.configure(text=f"Карта: {c.dev} — {human(c.size)}",
                                        style="Ok.TLabel")
                self.txt_state.insert("end", f"Устройство    : {c.dev}"
                                             f"{'' if c.is_mtd else '  (не MTD!)'}\n")
                self.txt_state.insert("end", f"Тип MTD       : {c.mtdtype or '?'}\n")
                self.txt_state.insert("end", f"Размер        : {c.size} байт ({human(c.size)})\n")
                self.txt_state.insert("end", f"Блок стирания : {c.erasesize} байт, "
                                             f"блоков: {c.size // c.erasesize if c.erasesize else '?'}"
                                             f"{', регионов: ' + str(len(set(c.regions))) if c.regions else ''}\n")
                self.txt_state.insert("end", f"Запись       : "
                                             f"{'разрешена' if c.writeable else 'ЗАПРЕЩЕНА (WP)'}\n")
                self.txt_state.insert("end", f"Модель (CIS)  : {kind}\n")
                self.txt_state.insert("end", f"Комментарий   : {note}\n")
                if c.ident:
                    ids = " | ".join(c.ident.get(f"PRODID_{i}", "") for i in range(1, 5)
                                     if c.ident.get(f"PRODID_{i}"))
                    if ids:
                        self.txt_state.insert("end", f"PRODID        : {ids}\n")
            if warns:
                self.txt_state.insert("end", "\n")
                for w in warns:
                    self.txt_state.insert("end", f"• {w}\n")
            self.txt_state.configure(state="disabled")

        def _render_tools(self):
            need = ["mtd_debug", "flash_erase", "flashcp", "ftl_format",
                    "pccardctl", "lspci", "modprobe"]
            miss = [t for t in need if not have(t)]
            bad_path = path_is_sane()
            msg = ""
            if miss:
                self.lbl_tools.configure(text="Не хватает: " + ", ".join(miss), style="Bad.TLabel")
            else:
                msg = "Все утилиты на месте"
                self.lbl_tools.configure(text=msg, style="Ok.TLabel")
            if bad_path:
                self.lbl_tools.configure(
                    text=f"{msg}; ⚠ в PATH есть каталог с правом записи ({bad_path[0]}) — "
                         "программа работает от root, это риск подмены утилит",
                    style="Warn.TLabel")

        # ---------------- действия
        def show_ident(self):
            if not self.card.present:
                messagebox.showwarning(APP_NAME, "Карта не найдена.")
                return
            c = self.card
            kind, note = c.card_kind()
            fmt = c.detect_format()
            txt = [f"Модель (CIS): {kind}", note, "",
                   f"Формат на карте: {fmt[0]} {fmt[1]}", "",
                   f"MANFID: {c.ident.get('MANFID', '?')}",
                   f"FUNCID: {c.ident.get('FUNCID', '?')}"]
            for i in range(1, 5):
                if c.ident.get(f"PRODID_{i}"):
                    txt.append(f"PRODID_{i}: {c.ident[f'PRODID_{i}']}")
            txt += ["", "--- mtd_debug info ---", c.info_raw or "(нет)",
                    "", "--- слот ---", c.status or "(нет)"]
            self._window("Идентификатор карты", "\n".join(txt))

        def check_slot(self):
            self._run(lambda ops: _check_slot(ops), "Проверка слота",
                      on_done=lambda res: self._window("Проверка слота", res.get("report", "")))

        def read_card(self):
            if not self.card.present:
                messagebox.showwarning(APP_NAME, "Карта не найдена.")
                return
            name = f"card_{datetime.now():%Y%m%d_%H%M}.bin"
            path = filedialog.asksaveasfilename(title="Куда сохранить образ",
                                                initialfile=name,
                                                defaultextension=".bin",
                                                filetypes=[("Образ", "*.bin"), ("Все", "*")])
            if not path:
                return
            if os.path.exists(path) and not messagebox.askyesno(
                    APP_NAME, f"{path} существует. Перезаписать?"):
                return
            free = dir_free(path)
            if free is not None and free < self.card.size * 1.1:
                messagebox.showerror(APP_NAME,
                                     f"Мало места: свободно {human(free)}, "
                                     f"нужно {human(int(self.card.size * 1.1))}")
                return
            self._run(lambda ops: ops.read_image(path), "Чтение карты")

        def write_card(self):
            if not self.card.present:
                messagebox.showwarning(APP_NAME, "Карта не найдена.")
                return
            path = filedialog.askopenfilename(title="Образ для записи",
                                              filetypes=[("Образ", "*.bin"), ("Все", "*")])
            if not path:
                return
            isz = os.path.getsize(path)
            c = self.card
            if isz > c.size:
                messagebox.showerror(APP_NAME, f"Образ больше карты: {human(isz)} > {human(c.size)}")
                return
            if not c.writeable:
                messagebox.showerror(APP_NAME, "Карта защищена от записи (переключатель WP).")
                return
            # --- проверки образа ДО того, как тронем карту
            rep = image_report(path)
            self._log_line(f"Образ: {rep['kind']}, {human(rep['size'])}, MD5 {rep['md5']}")
            if rep["all_zero"]:
                messagebox.showerror(
                    APP_NAME,
                    "Файл целиком из нулей (0x00).\n\n"
                    "Запись такого файла затрёт карту нулями — это почти всегда "
                    "означает, что выбран не тот файл.\n\n"
                    "Запись отменена. Если это действительно нужно: "
                    "«Диапазон: записать» или стереть карту вручную.")
                return
            if rep["all_ff"]:
                if not messagebox.askyesno(
                        APP_NAME,
                        "Файл целиком из 0xFF — это равносильно стиранию карты, "
                        "данных на ней не будет.\n\nПродолжить?"):
                    return
            kind, _ = c.card_kind()
            extra = ""
            if kind.startswith("Intel Series 2 ("):
                extra = ("\n\n⚠ Похоже на Intel Series 2: по документации 5 В и 12 В не "
                         "нужны, но часть таких карт всё же требует Vpp 12 В. "
                         "Заранее не узнать — набор сначала пробует один сектор.")
            tail = ""
            if isz < c.size:
                tail = (f"\n\nОбраз меньше карты на {human(c.size - isz)}."
                        + ("\nОстаток будет СТЁРТ." if self.var_erase_all.get()
                           else "\n⚠ Остаток НЕ будет стёрт — там останутся прежние данные!"))
            # «записывать нечего» — типичная потеря времени: 2-3 минуты и лишний цикл стирания
            try:
                same = compare_with_card(c, path)
            except Exception as e:                                # noqa: BLE001
                same = None
                self._log_line(f"сравнение с картой не удалось: {e}")
            if same:
                if not messagebox.askyesno(
                        APP_NAME,
                        "Судя по началу и концу, образ УЖЕ совпадает с содержимым карты.\n\n"
                        "Запись ничего не изменит, но займёт время и добавит один цикл "
                        "стирания (ресурс карты не бесконечный).\n\nЗаписывать всё равно?"):
                    self._log_line("Запись отменена: образ совпадает с картой")
                    return
            if not messagebox.askyesno(
                    APP_NAME,
                    f"Записать образ?\n\n{path}\n"
                    f"Содержимое: {rep['kind']}\n"
                    f"Размер: {human(isz)}\nMD5: {rep['md5']}\n"
                    f"Первые байты: {rep['head']}"
                    f"{tail}{extra}\n\nСодержимое карты будет изменено."):
                return
            self._run(lambda ops: ops.write_image(path,
                                                 erase_all=self.var_erase_all.get(),
                                                 verify=self.var_verify.get()),
                      "Запись образа")

        def verify_card(self):
            path = filedialog.askopenfilename(title="Что сверить с картой?",
                                              filetypes=[("Образ", "*.bin"), ("Все", "*")])
            if not path:
                return
            self._run(lambda ops: ops.verify(path), "Сверка")

        def erase_card(self):
            if not self.card.present:
                messagebox.showwarning(APP_NAME, "Карта не найдена.")
                return
            ans = simpledialog.askstring(
                APP_NAME,
                f"Стирание ВСЕЙ карты ({human(self.card.size)}).\n"
                "Данные будут потеряны безвозвратно.\n\n"
                "Введите слово ERASE для подтверждения:", parent=self)
            if (ans or "").strip().upper() != "ERASE":
                self._log_line("Стирание отменено (слово не введено)")
                return
            self._run(lambda ops: ops.erase(), "Стирание карты")

        def direct_probe_dialog(self):
            """ПРОБА без драйвера - единственный честный ответ «пишется ли»."""
            script_ok = bool(self.ops.direct_script()) if getattr(self, "ops", None) else False
            if not script_ok:
                messagebox.showerror(
                    APP_NAME,
                    "Рядом с программой нет pcmcia_direct.py, поэтому проба "
                    "невозможна. Проба работает, только если утилита лежит рядом.")
                return
            if not messagebox.askyesno(
                    APP_NAME,
                    "Проба записи БЕЗ драйвера.\n\n"
                    "Будет прочитан ОДИН сектор (64 КиБ), сохранён в файл, "
                    "стёрт, заполнен узором, сверен и возвращён на место.\n"
                    "Остальное содержимое карты не трогается.\n\n"
                    "Если карта не пишется, вернуть байты не удастся - файл "
                    "с исходными данными останется у вас.\n\nПродолжить?"):
                return
            self._run(lambda ops: ops.direct_probe(), "Проба записи без драйвера")

        def test_write(self):
            if not self.card.present:
                return
            if not self.card.writeable:
                messagebox.showerror(APP_NAME, "Карта защищена от записи.")
                return
            if not messagebox.askyesno(APP_NAME,
                                       f"Тест записи: карта ({human(self.card.size)}) будет стёрта "
                                       "и заполнена 0xFF, затем проверена.\nПродолжить?"):
                return
            self._run(lambda ops: ops.write_test(0xFF), "Тест записи")

        def make_ftl(self):
            if not messagebox.askyesno(APP_NAME,
                                       "Разметить FTL и создать FAT?\nСодержимое карты будет потеряно."):
                return
            self._run(lambda ops: ops.make_ftl(), "FTL + FAT")

        def range_read(self):
            off = simpledialog.askstring(APP_NAME, "Смещение (hex), например 100000:", parent=self)
            if off is None:
                return
            ln = simpledialog.askstring(APP_NAME, "Длина (hex), например 10000:", parent=self)
            if ln is None:
                return
            try:
                o, n = int(off, 16), int(ln, 16)
            except ValueError:
                messagebox.showerror(APP_NAME, "Нужны hex-числа")
                return
            path = filedialog.asksaveasfilename(title="Куда сохранить диапазон",
                                                defaultextension=".bin")
            if not path:
                return
            self._run(lambda ops: ops.read_range(o, n, path), "Чтение диапазона")

        def range_write(self):
            off = simpledialog.askstring(APP_NAME, "Смещение (hex):", parent=self)
            if off is None:
                return
            try:
                o = int(off, 16)
            except ValueError:
                messagebox.showerror(APP_NAME, "Нужно hex-число")
                return
            path = filedialog.askopenfilename(title="Файл для записи в диапазон")
            if not path:
                return
            n = os.path.getsize(path)
            if not messagebox.askyesno(APP_NAME,
                                       f"Записать {human(n)} по смещению {o:#x}?\n"
                                       "⚠ Внутри блока flash сначала нужно стереть блок (кнопка «Стереть»)."):
                return
            self._run(lambda ops: ops.write_range(o, n, path), "Запись диапазона")

        def view_read(self):
            try:
                off = int(self.var_off.get().strip() or "0", 16)
                ln = int(self.var_len.get())
            except ValueError:
                messagebox.showerror(APP_NAME, "Смещение — hex, длина — число")
                return
            if not self.card.present:
                return
            if off + ln > self.card.size:
                messagebox.showerror(APP_NAME, "За пределами карты")
                return
            self._run(lambda ops: self._read_hex_chunk(ops, off, ln), "Чтение участка",
                      on_done=self._render_hex)

        def _read_hex_chunk(self, ops, off, ln):
            """Работает в фоновом потоке: только чтение и форматирование."""
            d = private_tmpdir()
            tmp = os.path.join(d, "hex.bin")
            try:
                rc, _, err = ops.runner.exec(["mtd_debug", "read", self.card.dev,
                                              str(off), str(ln), tmp])
                if rc != 0:
                    raise RuntimeError(f"чтение не удалось: "
                                       f"{(err or '').strip() or 'без пояснений'}")
                with open(tmp, "rb") as f:
                    data = f.read()
                if len(data) != ln:
                    raise RuntimeError(f"прочитано {len(data)} байт вместо {ln}")
            finally:
                shutil.rmtree(d, ignore_errors=True)
            lines = []
            for i in range(0, len(data), 16):
                ch = data[i:i + 16]
                hx = " ".join(f"{b:02X}" for b in ch)
                hx = (hx[:23] + " " + hx[23:]) if len(ch) > 8 else hx
                asc = "".join(chr(b) if 32 <= b < 127 else "." for b in ch)
                lines.append(f"{off + i:08X}  {hx:<48} |{asc}|")
            ops.log(f"прочитано {len(data)} байт @ {off:#x}")
            return {"offset": off, "length": len(data) or 256, "text": "\n".join(lines)}

        def _render_hex(self, res):
            self._safe(self._render_hex_inner)(res)

        def _render_hex_inner(self, res):
            """Вызывается уже в UI-потоке."""
            self.txt_hex.delete("1.0", "end")
            self.txt_hex.insert("1.0", res.get("text", ""))
            self._view_off = res.get("offset", 0)
            self._view_len = res.get("length", 256)

        def view_move(self, d):
            off = getattr(self, "_view_off", 0)
            ln = getattr(self, "_view_len", 256) or 256
            new = max(0, off + d * ln)
            if self.card.size and new + ln > self.card.size:
                new = max(0, self.card.size - ln)
            self.var_off.set(f"{new:08X}")
            self.view_read()

        def poke(self):
            raw = self.var_poke.get().replace(",", " ").split()
            try:
                data = bytes(int(x, 16) for x in raw)
            except ValueError:
                messagebox.showerror(APP_NAME, "Ожидаются hex-байты: DE AD BE EF")
                return
            try:
                off = int(self.var_off.get().strip(), 16)
            except ValueError:
                messagebox.showerror(APP_NAME, "Неверное смещение")
                return
            size = self.card.size or 0
            if off < 0:
                messagebox.showerror(APP_NAME, "Смещение не может быть отрицательным")
                return
            if off + len(data) > size:
                messagebox.showerror(APP_NAME,
                                     f"{len(data)} байт по {off:#x} не помещаются "
                                     f"в карту ({human(size)})")
                return
            d = private_tmpdir()
            tmp = os.path.join(d, "poke.bin")
            with open(tmp, "wb") as f:
                f.write(data)
            if not messagebox.askyesno(APP_NAME,
                                       f"Записать {len(data)} байт по {off:#x}?\n"
                                       "Внутри блока flash сначала нужно стереть блок."):
                shutil.rmtree(d, ignore_errors=True)
                return
            self._run(lambda ops: ops.write_range(off, len(data), tmp), "Запись байтов",
                      on_done=lambda res: shutil.rmtree(d, ignore_errors=True))

        def scan_map(self):
            self._run(lambda ops: self._do_scan(ops), "Сканирование блоков",
                      on_done=self._render_map)

        def _do_scan(self, ops):
            ops.log("Сканирую блоки (чтение всей карты)…")
            states = self.card.block_map(ops.runner, ops.progress)
            ops.log(f"Блоков: {len(states)}, стёртых: {states.count('erased')}, "
                    f"занятых: {states.count('used')}, ошибок: {states.count('error')}")
            return {"states": states}

        def _render_map(self, res):
            self._map_states = res.get("states", [])
            self._safe(self._draw_map)()

        def _draw_map(self):
            states = getattr(self, "_map_states", [])
            cv = self.canvas
            cv.delete("all")
            if not states:
                cv.create_text(10, 10, anchor="nw", fill="#bbb",
                               text="Нажмите «Просканировать карту»")
                return
            w = max(cv.winfo_width(), 100)
            h = max(cv.winfo_height(), 100)
            n = len(states)
            # сетка заполняет ширину: ячейка 20 px, слева колонка адресов
            cell = 20 if w >= 620 else 14
            cols = max(1, min(n, (w - 52) // cell))
            rows = (n + cols - 1) // cols
            if 4 + rows * cell > h:                     # не влезло по высоте — мельче
                cell = max(8, (h - 8) // max(rows, 1))
            colors = {"erased": "#2e7d32", "used": "#c62828", "error": "#ff9800"}
            self._map_cell, self._map_cols = cell, cols
            x0 = 44
            for i, st in enumerate(states):
                r, c = divmod(i, cols)
                x, y = x0 + c * cell, 4 + r * cell
                cv.create_rectangle(x, y, x + cell - 2, y + cell - 2,
                                    fill=colors.get(st, "#555"), outline="#111")
            # адрес начала каждой строки блоков
            for r in range(rows):
                first = states and self.card.erasesize * r * cols
                cv.create_text(x0 - 6, 4 + r * cell + cell // 2, anchor="e",
                               fill="#9aa0a6", font=("Courier", 8),
                               text=(f"{first // (1024 * 1024)}М" if first >= 1024 * 1024
                                     else f"{first // 1024}К"))
            cv.configure(scrollregion=(0, 0, x0 + cols * cell, 8 + rows * cell))
            legend = (f"всего {n} блоков по {human(self.card.erasesize)}: "
                      f"стёрто {states.count('erased')} (зелёный), "
                      f"занято {states.count('used')} (красный), "
                      f"ошибок {states.count('error')} (оранжевый)")
            self.lbl_map.configure(text=legend)

        def _map_click(self, event):
            states = getattr(self, "_map_states", [])
            cell = getattr(self, "_map_cell", 16)
            cols = getattr(self, "_map_cols", 1)
            c = int((event.x - 44) // cell)
            r = int((event.y - 4) // cell)
            i = r * cols + c
            if 0 <= i < len(states):
                start = self.card.erasesize * i
                name = {"erased": "стёрт", "used": "занят", "error": "ошибка чтения"}
                self.lbl_map.configure(
                    text=f"блок {i}: {start:#x}–{start + self.card.erasesize - 1:#x} — "
                         f"{name.get(states[i], states[i])}")
                self._log_line(f"блок {i} @ {start:#010x}: {states[i]}")

        def configure_slot(self):
            vpp = "33" if self.var_vpp.get().startswith("3.3") else "50"
            save_profile({**self.profile, "vpp": vpp,
                          "erase_all": self.var_erase_all.get(),
                          "verify": self.var_verify.get(),
                          "mountpoint": self.var_mount.get()})
            self._run(lambda ops: _configure_slot(ops, vpp), "Настройка слота",
                      on_done=self._show_cfg)

        def _show_cfg(self, res):
            lines = res.get("windows", [])
            txt = ["Vpp: " + str(res.get("vpp")),
                   "Записано в /etc/pcmcia/config.opts:"] + ["  " + l for l in lines]
            if res.get("card"):
                txt.append(f"\nКарта распознана, размер {human(res.get('size', 0))}")
            else:
                txt.append("\nКарта не появилась — смотрите журнал и dmesg")
            self.txt_cfg.delete("1.0", "end")
            self.txt_cfg.insert("1.0", "\n".join(txt))

        def install_tools(self):
            self._run(lambda ops: _install_tools(ops), "Установка утилит")

        def mount_toggle(self):
            self._run(lambda ops: _mount_toggle(ops, self.var_mount.get().strip() or "/mnt/card"),
                      "Монтирование")

        def save_report_ui(self):
            base = filedialog.asksaveasfilename(title="Сохранить отчёт",
                                                defaultextension="")
            if not base:
                return
            data = build_report(self.card, {"name": "manual"}, {"note": "отчёт по требованию"})
            t, j = save_report(data, base)
            self._log_line(f"Отчёт: {t}, {j}")
            messagebox.showinfo(APP_NAME, f"Сохранено:\n{t}\n{j}")

        # ---------------- прочее
        def _window(self, title, text):
            win = tk.Toplevel(self)
            win.title(title)
            win.geometry("760x520")
            t = scrolledtext.ScrolledText(win, font=("Courier", 9), wrap="word")
            t.pack(fill="both", expand=True, padx=6, pady=6)
            t.insert("1.0", text)
            t.configure(state="disabled")
            ttk.Button(win, text="Закрыть", command=win.destroy).pack(pady=(0, 6))

        def save_log(self):
            p = filedialog.asksaveasfilename(defaultextension=".log",
                                             initialfile="pcmcia-flash-tool.log")
            if p:
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(self.txt_log.get("1.0", "end"))
                self._log_line(f"Журнал сохранён: {p}")

        def save_ui_state(self):
            """Запоминает, что пользователь вводил, чтобы не набирать заново."""
            try:
                d = load_profile()
                d.update({
                    "offset": self.var_off.get(),
                    "length": self.var_len.get(),
                    "erase_all": bool(self.var_erase_all.get()),
                    "verify": bool(self.var_verify.get()),
                    "mountpoint": self.var_mount.get(),
                    "last_tab": self.nb.index("current") if self.nb.tabs() else 0,
                })
                save_profile(d)
            except Exception:                                     # noqa: BLE001
                pass

        def restore_ui_state(self):
            p = self.profile or {}
            for var, key in ((getattr(self, "var_off", None), "offset"),
                             (getattr(self, "var_len", None), "length")):
                if var is not None and p.get(key):
                    try:
                        var.set(str(p[key]))
                    except Exception:                             # noqa: BLE001
                        pass
            for var, key in ((getattr(self, "var_erase_all", None), "erase_all"),
                             (getattr(self, "var_verify", None), "verify")):
                if var is not None and key in p:
                    try:
                        var.set(bool(p[key]))
                    except Exception:                             # noqa: BLE001
                        pass
            tab = p.get("last_tab")
            if isinstance(tab, int) and 0 <= tab < len(self.nb.tabs()):
                try:
                    self.nb.select(tab)
                except Exception:                                 # noqa: BLE001
                    pass

        def on_close(self):
            if self.busy_op:
                if not messagebox.askyesno(
                        APP_NAME,
                        "Операция с картой ещё идёт (карта может остаться в "
                        "промежуточном состоянии).\n\nПрервать и выйти?"):
                    return
                self.cancel_op()
                # даём рабочему потоку шанс штатно завершиться, но не держим
                # пользователя: через 3 с закрываемся в любом случае
                self._closing = True
                self.after(3000, self._force_close)
                return
            self._force_close()

        def _force_close(self):
            self.save_ui_state()
            try:
                if self.busy_op:
                    self._log_line("ВНИМАНИЕ: выход при незавершённой операции. "
                                   "Проверьте карту перед следующим использованием.")
            except Exception:                                     # noqa: BLE001
                pass
            try:
                self.destroy()
            except Exception:                                     # noqa: BLE001
                pass

    # -------- операции, требующие диалога из UI (возвращают текст в UI)
    def _check_slot(ops):
        lines = []
        rc, out, _ = run(["lspci", "-nn"])
        cb = [l for l in out.splitlines() if re.search(r"cardbus|pcmcia", l, re.I)]
        lines.append("CardBus: " + (cb[0] if cb else "НЕ НАЙДЕН"))
        if cb:
            dev = cb[0].split()[0]
            rc, out, _ = run(["lspci", "-v", "-s", dev])
            for l in out.splitlines():
                if re.search(r"Memory window|I/O window|Kernel driver", l):
                    lines.append("  " + l.strip())
        rc, out, err = run(["pccardctl", "status"])
        lines.append("\npccardctl status:\n" + (out or err or "(нет ответа)"))
        rc, out, _ = run(["lsmod"])
        mods = [l.split()[0] for l in out.splitlines() if l.startswith(("yenta", "pcmcia", "mtd"))]
        lines.append("модули: " + (", ".join(mods) or "—"))
        rc, out, err = run("dmesg | tail -20")
        lines.append("\ndmesg:\n" + out.strip())
        return {"report": "\n".join(lines)}

    def _configure_slot(ops, vpp):
        try:
            vpp = int(vpp)
        except (TypeError, ValueError):
            raise RuntimeError("Vpp должен быть числом в десятых вольта (50 = 5.0 В)")
        # 12 В в этом ноутбуке нет; 33 и 50 — рабочие варианты, остальное запрещаем
        if vpp not in (33, 50):
            raise RuntimeError(f"Недопустимое Vpp={vpp}: разрешено 50 (5.0 В) "
                               "или 33 (3.3 В). 12 В на этом слоте нет — "
                               "такая настройка карту не оживит, а повредить может.")
        rc, out, _ = sh(ops, ["lspci", "-nn"])
        cb = [l for l in out.splitlines() if re.search(r"cardbus", l, re.I)]
        if not cb:
            sh(ops, ["modprobe", "yenta_socket"]); time.sleep(1)
            rc, out, _ = sh(ops, ["lspci", "-nn"])
            cb = [l for l in out.splitlines() if re.search(r"cardbus", l, re.I)]
        if not cb:
            raise RuntimeError("CardBus-мост не найден — включён ли слот в BIOS?")
        dev = cb[0].split()[0]
        rc, out, _ = sh(ops, ["lspci", "-v", "-s", dev])
        wins = [l.strip() for l in out.splitlines() if re.search(r"Memory window|I/O window", l)]
        if not wins:
            raise RuntimeError("нет строк «Memory window» — yenta_socket не привязан к мосту")
        lines = []
        for w in wins:
            m = re.search(r"([0-9a-fA-F]{8})-([0-9a-fA-F]{8})", w)
            if not m:
                continue
            s, e = int(m.group(1), 16), int(m.group(2), 16)
            if "memory" in w.lower():
                lines.append(f"include memory 0x{s & ~0xFFFFF:x}-0x{e:x}")
            else:
                lines.append(f"include port 0x{s:x}-0x{e:x}")
        try:
            for path in (CFG_OPTS, MODPROBE_CONF):
                os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.exists(CFG_OPTS):
                shutil.copy2(CFG_OPTS, CFG_OPTS + f".bak.{int(time.time())}")
            with open(CFG_OPTS, "w", encoding="utf-8") as fh:
                fh.write(f"# {APP_NAME} {datetime.now():%Y-%m-%d %H:%M}\n"
                         + "\n".join(lines) + "\n")
            with open(MODPROBE_CONF, "w", encoding="utf-8") as fh:
                fh.write(f"# {APP_NAME}: Vpp={vpp} (0.1 В), только на запись\n"
                         f"options pcmciamtd vpp={vpp} setvpp=1\n")
        except OSError as e:
            raise RuntimeError(f"не удалось записать настройки слота: {e} "
                               "(нужен запуск от root)") from e
        try:
            os.makedirs(os.path.dirname(UDEV_RULES), exist_ok=True)
            with open(UDEV_RULES, "w", encoding="utf-8") as fh:
                fh.write('SUBSYSTEM=="mtd", MODE="0660", GROUP="flash"\n')
            sh(ops, ["groupadd", "-f", "flash"])
            sh(ops, ["udevadm", "control", "--reload-rules"])
        except OSError:
            ops.log("правила udev записать не удалось — не критично")
        for m in ("yenta_socket", "pcmcia", "mtd", "jedec_probe", "cfi_probe",
                  "cfi_cmdset_0001", "cfi_cmdset_0002", "pcmciamtd"):
            sh(ops, ["modprobe", m])
        time.sleep(1)
        ops.card.refresh()
        return {"windows": lines, "vpp": vpp,
                "card": ops.card.present, "size": ops.card.size}

    def _install_tools(ops):
        need = ["mtd_debug", "flash_erase", "flashcp", "pccardctl", "lspci", "modprobe"]
        miss = [t for t in need if not have(t)]
        if not miss:
            return {"installed": 0, "note": "всё уже есть"}
        ops.log("Ставлю через apt: " + ", ".join(miss))
        rc, out, err = sh(ops, ["apt-get", "install", "-y", "mtd-utils", "pcmciautils",
                            "pciutils", "kmod", "dosfstools", "python3-tk"], timeout=1800)
        if rc != 0:
            ops.log("apt не смог — офлайн-набор debs/")
            debs = _find_debs()
            if debs:
                for f in sorted(os.listdir(debs)):
                    if not f.endswith(".deb"):
                        continue
                    p = os.path.join(debs, f)
                    pkg = sh(ops, ["dpkg-deb", "-f", p, "Package"])[1].strip()
                    if not pkg:
                        continue
                    st = sh(ops, ["dpkg-query", "-W", "-f=${Status}", pkg])[1]
                    if "install ok installed" in st:
                        continue
                    ops.log(f"+ {pkg}")
                    sh(ops, ["dpkg", "-i", p], timeout=600)
                sh(ops, ["apt-get", "-f", "install", "-y"], timeout=900)
        left = [t for t in need if not have(t)]
        if left:
            raise RuntimeError("не установилось: " + ", ".join(left))
        return {"installed": len(miss)}

    def _find_debs():
        here = os.path.dirname(os.path.abspath(__file__))
        for c in (os.path.join(here, "debs"), os.path.join(here, "..", "debs"),
                  os.path.join(os.getcwd(), "debs")):
            if os.path.isdir(c):
                return os.path.abspath(c)
        for arc in ("pcmcia-flash-kit.tar.gz", "pcmcia-flash-kit.cpio.gz"):
            for base in (here, os.path.dirname(here), os.getcwd()):
                p = os.path.join(base, arc)
                if os.path.exists(p):
                    t = tempfile.mkdtemp(prefix="pcmcia-kit-")
                    if arc.endswith(".tar.gz"):
                        run(["tar", "-xzf", p, "-C", t], timeout=600)
                    elif have("cpio"):
                        run(["sh", "-c", f"cd '{t}' && gzip -dc '{p}' | cpio -idmu --quiet"],
                            timeout=600)
                    for cand in (os.path.join(t, "debs"),
                                 os.path.join(t, "pcmcia-flash-kit", "debs")):
                        if os.path.isdir(cand):
                            return cand
        return None

    def _mount_toggle(ops, mnt):
        rc, _, _ = sh(ops, ["mountpoint", "-q", mnt])
        if rc == 0:
            rc, out, err = sh(ops, ["umount", mnt])
            if rc != 0:
                raise RuntimeError(f"umount: {(err or out).strip()}")
            return {"action": "umount", "path": mnt}
        dev = "/dev/ftl0" if os.path.exists("/dev/ftl0") else "/dev/mtdblock0"
        if dev.endswith("mtdblock0"):
            sh(ops, ["modprobe", "mtdblock"]); time.sleep(1)
        os.makedirs(mnt, exist_ok=True)
        rc, out, err = sh(ops, ["mount", dev, mnt])
        if rc != 0:
            raise RuntimeError(f"mount: {(err or out).strip()}. "
                               "Сначала «Сделать FAT-диск (FTL)».")
        return {"action": "mount", "dev": dev, "path": mnt}


# ============================================================ блокировка

def lock_paths():
    """Куда пробовать писать блокировку. /var/lock доступен только root."""
    return [LOCK_PATH, os.path.join(tempfile.gettempdir(),
                                    f"pcmcia-flash-tool-{os.getuid()}.lock")]


def acquire_single_instance():
    """Захват блокировки. Возвращает (файл, путь) либо (None, причина).

    Раньше любая ошибка открытия файла (например, /var/lock недоступен
    обычному пользователю) трактовалась как «программа уже запущена» —
    и второй запуск не диагностировался, а первый отказывался стартовать.
    """
    last = "неизвестная ошибка"
    for path in lock_paths():
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fh = open(path, "a+")
        except OSError as e:
            last = f"{path}: {e}"
            continue
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return None, f"уже запущено другим экземпляром (блокировка {path})"
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()))
            fh.flush()
        except OSError:
            pass
        return fh, path
    return None, f"не удалось создать файл блокировки ({last})"


def lock_holder(path):
    """PID из файла блокировки — чтобы сказать, кто именно держит."""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip() or "?"
    except OSError:
        return "?"


# ============================================================ CLI

def selftest():
    print(f"{APP_NAME} {APP_VER} — самопроверка\n")
    print(f"Архитектура   : {run(['dpkg','--print-architecture'])[1].strip() or '?'} (нужно i386)")
    print(f"Ядро          : {os.uname().release}")
    print(f"root          : {'да' if os.geteuid() == 0 else 'НЕТ'}")
    print(f"tkinter       : {'есть' if _has_tk() else 'НЕТ (python3-tk)'}")
    bad = path_is_sane()
    print(f"PATH          : {'ок' if not bad else 'риск: ' + ', '.join(bad)}")
    c = Card()
    print(f"Устройство    : {c.dev} — {'найдено' if c.present else 'не найдено'}"
          f"{' (MTD)' if c.is_mtd else ''}")
    if c.present:
        print(f"  размер      : {c.size} ({human(c.size)}), блок {c.erasesize}, "
              f"регионов {len(set(c.regions)) or 1}")
        print(f"  запись      : {'разрешена' if c.writeable else 'ЗАПРЕЩЕНА'}")
        print(f"  модель      : {c.card_kind()[0]}")
        print(f"  формат      : {c.detect_format()[0]}")
    for t in ("mtd_debug", "flash_erase", "flashcp", "ftl_format", "pccardctl",
              "lspci", "modprobe", "mkfs.vfat"):
        print(f"  {'+' if have(t) else '-'} {t}")
    for w in c.warnings():
        print(f"  • {w}")


def _run_quiet(cmd, timeout=10):
    """Вывод команды или пустая строка — для диагностики не критично."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           errors="replace")
        return (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return ""


def doctor_slot():
    """Цепочка «мост → слот → CIS → драйвер → /dev/mtd» короткими строками.

    Нужна, чтобы по одному выводу было видно, где обрыв: «программа не видит
    карту» — это ещё не «CIS не работает».
    """
    lspci = _run_quiet(["lspci", "-nn"])
    bridge = next((ln.split()[0] for ln in lspci.splitlines()
                   if "cardbus" in ln.lower()), "")
    print("\nСлот и CIS:")
    if not bridge:
        print("  мост CardBus  : НЕ НАЙДЕН — слот выключен в BIOS или контроллер "
              "не инициализируется (это не CIS)")
    else:
        verbose = _run_quiet(["lspci", "-v", "-s", bridge])
        drv = next((ln.split(":", 1)[1].strip() for ln in verbose.splitlines()
                    if "Kernel driver in use" in ln), "")
        print(f"  мост CardBus  : {bridge}" + (f", драйвер {drv}" if drv else
                                              ", драйвер НЕ привязан (modprobe yenta_socket)"))
    st = _run_quiet(["pccardctl", "status"])
    if not st.strip():
        print("  pccardctl     : не ответил (нет pcmciautils или слот не отвечает)")
    else:
        print("  pccardctl     : " + " ".join(st.split())[:100])
    ident = _run_quiet(["pccardctl", "ident"])
    print("  CIS (ident)   : " + (" ".join(ident.split())[:120] if ident.strip()
                                  else "не прочитан"))
    dm = _run_quiet(["dmesg"])
    if not dm.strip():
        dm = _run_quiet(["journalctl", "-k", "-n", "400", "--no-pager"])
    # отбираем только строки ядра: в audit/пути бывает слово pcmcia
    hits = [ln for ln in dm.splitlines()
            if re.search(r"\b(pcmciamtd|yenta\w*|pcmcia_core|pccard\w*)\b", ln, re.I)
            and "audit" not in ln.lower()]
    if hits:
        print("  ядро          : " + hits[-1][:120])
    low = dm.lower()
    if "no high memory space available" in low:
        print("  ВЕРДИКТ       : у слота НЕТ годного окна памяти (no high memory space "
              "available). Из-за этого карта не становится устройством PCMCIA, и "
              "«no product info available» у pccardctl ident — следствие, а не "
              "причина. force_size/Vpp тут не помогут. Разбор и обход: "
              "sudo bash pcmcia_flash.sh --diag")
    elif re.search(r"pcmciamtd.*(no memory window|get_mem_map|allocate)", low):
        print("  ВЕРДИКТ       : драйвер не занял окно памяти — не CIS. "
              "Повторите пункт 2 (окна памяти), затем modprobe -r pcmciamtd; "
              "modprobe pcmciamtd")
    elif re.search(r"anonymous|no cis|cis.*(not|fail)|unknown card", low):
        print("  ВЕРДИКТ       : карта без читаемого CIS. Обход — force_size=16 "
              "(МБ) в /etc/modprobe.d/pcmciamtd.conf, затем перезагрузить модуль; "
              "подробно: sudo bash pcmcia_flash.sh --diag")
    elif not os.path.exists(MTD):
        print("  ВЕРДИКТ       : /dev/mtd0 нет, ядро молчит — модуль pcmciamtd не "
              "загружался либо карта не подключена. Полный разбор: "
              "sudo bash pcmcia_flash.sh --diag")
    else:
        print("  ВЕРДИКТ       : карта на месте, цепочка пройдена")


def doctor():
    """Полная диагностика: то же, что selftest + ошибки и рекомендации."""
    selftest()
    doctor_slot()
    c = Card()
    if not c.present:
        print("\nРекомендации:")
        print("  1. Вставьте карту в слот (лучше до загрузки) и повторите.")
        print("  2. Вкладка «Настройка» → «Настроить слот и загрузить драйверы».")
        print("  3. Проверьте dmesg | tail -30 и pccardctl status.")
        print("  4. Пошаговый разбор «почему не видна»: sudo bash pcmcia_flash.sh --diag")
    elif c.simulated:
        print("\nЭто обычный файл: работает режим тренировки. Для настоящей карты "
              "запустите без MTD=... (по умолчанию /dev/mtd0).")
    elif not c.is_mtd:
        print("\nРекомендация: устройство не MTD — возможно это ATA/CF карта "
              "(она видна как /dev/sdX и монтируется обычным mount).")


def cli_blocks(path=None):
    """Состояние блоков без графики: удобно для скриптов и проверки «на бегу»."""
    c = Card(quick=True) if path else Card()
    if not c.present and not path:
        print("Карта не найдена: " + MTD, file=sys.stderr)
        return 2
    if path:
        import stat as _stat
        if not os.path.exists(path):
            print(f"файла {path} нет", file=sys.stderr)
            return 2
        if not _stat.S_ISREG(os.stat(path).st_mode):
            print(f"{path} — не обычный файл (устройство, каталог или FIFO). "
                  "Укажите файл образа.", file=sys.stderr)
            return 2
        if os.path.getsize(path) == 0:
            print(f"{path} — пустой файл", file=sys.stderr)
            return 2
        if not c.erasesize:
            c.erasesize = BLOCK_DEFAULT
        states = c.block_map_from_file(path)
        src = path
    else:
        states = c.block_map(Runner())
        src = c.dev
    if not states:
        print("не удалось прочитать карту", file=sys.stderr)
        return 1
    bs = c.erasesize or BLOCK_DEFAULT
    print(f"{src}: {human(len(states) * bs)}, блок {human(bs)}, "
          f"стёрто {states.count('erased')}, занято {states.count('used')}, "
          f"ошибок {states.count('error')}")

    def run_of(st):
        """Непрерывные участки одинакового состояния — так читается глазом."""
        out, i = [], 0
        while i < len(states):
            j = i
            while j + 1 < len(states) and states[j + 1] == st:
                j += 1
            out.append((i, j, states[i]))
            i = j + 1
        return out

    for st, label in (("used", "занято"), ("error", "ОШИБКИ")):
        segs = [s for s in run_of(st) if s[2] == st]
        for a, b, _ in segs[:6]:
            rng = f"{a * bs:#09x}–{(b + 1) * bs - 1:#09x}"
            print(f"  {label}: блоки {a}–{b}  ({rng})")
        if len(segs) > 6:
            print(f"  {label}: … и ещё {len(segs) - 6} участков")
    return 0


def main():
    if "--blocks" in sys.argv:
        sys.exit(cli_blocks())
    if "--map-file" in sys.argv:
        i = sys.argv.index("--map-file")
        if i + 1 >= len(sys.argv):
            print("--map-file требует путь к образу", file=sys.stderr)
            sys.exit(2)
        sys.exit(cli_blocks(sys.argv[i + 1]))
    if "--selftest" in sys.argv and "--doctor" in sys.argv:
        print("Выберите что-то одно: --selftest или --doctor", file=sys.stderr)
        sys.exit(2)
    if "--selftest" in sys.argv:
        selftest(); return
    if "--doctor" in sys.argv:
        doctor(); return
    known = ("--selftest", "--doctor", "--blocks", "--map-file")
    unknown = []
    skip = False
    for a in sys.argv[1:]:
        if skip:
            skip = False
            continue
        if a == "--map-file":
            skip = True
            continue
        if a.startswith("-") and a not in known:
            unknown.append(a)
    if unknown:
        print(f"Неизвестный параметр: {' '.join(unknown)}\n"
              "Допустимо: --selftest, --doctor, --blocks, --map-file <образ>",
              file=sys.stderr)
        sys.exit(2)
    if not _has_tk():
        print("Нет tkinter. Установите: sudo apt install python3-tk", file=sys.stderr)
        print("Либо работайте без графики:  sudo bash ./pcmcia_flash.sh", file=sys.stderr)
        sys.exit(2)
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        # без этого здесь был трейсбек Tk на пустом месте — например, при
        # запуске по ssh или из консоли без X
        print("Графическое окружение не найдено (переменная DISPLAY пуста).\n"
              "Варианты:\n"
              "  * запустить из сеанса с рабочим столом (не по ssh);\n"
              "  * работать без графики:  sudo bash ./pcmcia_flash.sh\n"
              "  * проверить состояние:   python3 pcmcia_gui.py --doctor",
              file=sys.stderr)
        sys.exit(2)
    lock, why = acquire_single_instance()
    if lock is None:
        print(f"Запуск невозможен: {why}", file=sys.stderr)
        if os.environ.get("DISPLAY"):
            try:
                import tkinter as _tk
                r = _tk.Tk(); r.withdraw()
                messagebox.showerror(APP_NAME, "Программа уже запущена.")
            except Exception:                                     # noqa: BLE001
                pass
        sys.exit(3)
    if os.geteuid() != 0:
        print("ВНИМАНИЕ: без root операции с картой недоступны.", file=sys.stderr)
    App().mainloop()


if __name__ == "__main__":
    main()
