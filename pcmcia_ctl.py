#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пульт управления картой PCMCIA (сторона ноутбука).

Полная замена pcmcia_ctl.sh — весь функционал на Python:
info, read, write, erase, verify, canwrite, diag, report,
fwprobe, fwwrite, chip, selftest, menu.

Запуск на ноутбуке с картой:
    sudo python3 pcmcia_ctl.py              # меню
    sudo python3 pcmcia_ctl.py info         # паспорт и вердикт
    sudo python3 pcmcia_ctl.py read card.bin
    sudo python3 pcmcia_ctl.py write card.bin --yes

Требуется root (доступ к /dev/mem и /dev/mtdN).
Никакой сети на этой машине — соединение устанавливает Windows-пульт по ssh.
"""

from __future__ import annotations

import argparse
import fcntl
import locale
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Принудительная UTF-8 кодировка вывода и отключение буферизации,
# независимо от локали системы (C / POSIX / другие).
# Это заменяет bash-конструкцию `LC_ALL=C.UTF-8` и `PYTHONUNBUFFERED=1`.
os.environ.setdefault("PYTHONUNBUFFERED", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
try:
    locale.setlocale(locale.LC_ALL, "C.UTF-8")
except locale.Error:
    try:
        locale.setlocale(locale.LC_ALL, "C.utf8")
    except locale.Error:
        pass

# --------------------------------------------------------------------------- #
# Путь к набору (рядом с этим скриптом) и к pcmcia_direct.py                  #
# --------------------------------------------------------------------------- #
KIT = Path(__file__).resolve().parent
# скрипт может лежать в подпапке linux/ — тогда набор ищем на уровень выше
if KIT.name == "linux" and (KIT / "..").is_dir():
    KIT = (KIT / "..").resolve()

DIRECT = KIT / "windows" / "legacy" / "pcmcia_direct.py"
LOG_DEFAULT = KIT / "pcmcia_ctl.log"
LOCK_FILE = KIT / ".pcmcia_ctl.lock"
MTD = Path(os.environ.get("MTD", "/dev/mtd0"))

# --------------------------------------------------------------------------- #
# Цвета (только в терминале)                                                  #
# --------------------------------------------------------------------------- #
_IS_TTY = sys.stdout.isatty()
C_B = "\033[36m" if _IS_TTY else ""
C_0 = "\033[0m" if _IS_TTY else ""
C_R = "\033[31m" if _IS_TTY else ""
C_G = "\033[32m" if _IS_TTY else ""
C_Y = "\033[33m" if _IS_TTY else ""

JSON_MODE = False  # устанавливается в --json


# --------------------------------------------------------------------------- #
# Вспомогательный ввод-вывод                                                  #
# --------------------------------------------------------------------------- #
LOG_PATH: Path | None = None


def _log(text: str) -> None:
    """Записать строку в лог-файл, если он задан."""
    if LOG_PATH is not None:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except OSError:
            pass


def say(text: str = "") -> None:
    """Напечатать и продублировать в лог."""
    print(text)
    _log(text)


def ok(text: str = "") -> None:
    say(f"  {C_G}OK{C_0}  {text}")


def warn(text: str = "") -> None:
    say(f"  {C_Y}!{C_0}   {text}")


def err(text: str = "") -> None:
    say(f"  {C_R}ОШИБКА{C_0} {text}")


def sec(title: str) -> None:
    say("")
    say(f"{C_B}===== {title} ====={C_0}")


def have(name: str) -> bool:
    """Есть ли программа в PATH."""
    from shutil import which
    return which(name) is not None


def human(size_bytes: int | str) -> str:
    """Человекочитаемый размер (KiB, MiB)."""
    try:
        size_bytes = int(size_bytes)
    except (ValueError, TypeError):
        return str(size_bytes)
    for unit, div in (("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10), ("B", 1)):
        if size_bytes >= div:
            val = size_bytes / div
            return f"{val:.0f} {unit}" if val == int(val) else f"{val:.1f} {unit}"
    return "0 B"


# --------------------------------------------------------------------------- #
# Работа с MTD-устройством                                                    #
# --------------------------------------------------------------------------- #
def _sysfs_mtd_path(dev: str, fname: str) -> str | None:
    """Путь к файлу sysfs для MTD-устройства с учётом PCM_SYSFS (для тестов)."""
    sysfs_root = os.environ.get("PCM_SYSFS", "/sys")
    try:
        n = dev.split("/mtd")[-1]
        n = "".join(ch for ch in n if ch.isdigit())
        f = f"{sysfs_root}/class/mtd/mtd{n}/{fname}"
        if os.path.isfile(f):
            return f
    except (ValueError, OSError):
        pass
    return None


def mtd_size(dev: str) -> int:
    """Размер MTD-устройства (байт). Через sysfs, blockdev, stat или 0."""
    f = _sysfs_mtd_path(dev, "size")
    if f:
        try:
            with open(f) as fh:
                v = fh.read().strip()
            if v:
                return int(v, 0)
        except (ValueError, OSError):
            pass
    try:
        out = subprocess.run(
            ["blockdev", "--getsize64", dev],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if out:
            return int(out)
    except (subprocess.SubprocessError, ValueError, OSError):
        pass
    try:
        return os.path.getsize(dev)
    except OSError:
        return 0


def mtd_erase_size(dev: str) -> int:
    """Размер стираемого блока (сектора), байт. Через sysfs или 65536 по умолчанию."""
    f = _sysfs_mtd_path(dev, "erasesize")
    if f:
        try:
            with open(f) as fh:
                v = fh.read().strip()
            if v:
                return int(v, 0)
        except (ValueError, OSError):
            pass
    return 65536


# --------------------------------------------------------------------------- #
# Состояние карты                                                             #
# --------------------------------------------------------------------------- #
class CardState:
    """Кешированное состояние карты: mtd, паспорт, напряжение, вердикт."""

    def __init__(self, mtd: str, direct: Path) -> None:
        self.mtd = mtd
        self.direct = direct
        self.mtd_ok = False
        self.mtd_size_bytes = 0
        self.cis_ok = False
        self.cis_text = ""
        self.cis_rc = 1
        self.cis_error = ""
        self.product = ""
        self.vpp = ""          # "" = неизвестно, "5" = 5 В, "12" = 12 В
        self.vpp12 = False
        self.why = ""

    def probe(self) -> None:
        """Один опрос карты: mtd, CIS, напряжение."""
        self.mtd_ok = os.path.exists(self.mtd)
        if self.mtd_ok:
            self.mtd_size_bytes = mtd_size(self.mtd)

        if self.direct.is_file() and have("python3"):
            out, rc = self._run([sys.executable, str(self.direct), "--cis"])
            self.cis_text = out
            self.cis_rc = rc
            self.cis_ok = rc == 0
            for line in out.splitlines():
                low = line.lower()
                if any(m in low for m in ("ошибка", "нет —", "не найден")):
                    if not self.cis_error:
                        self.cis_error = line.strip()
                if "продукт (vers_1)" in low:
                    parts = line.split(":", 1)
                    if len(parts) == 2:
                        self.product = parts[1].strip()
                if "напряжение записи" in low:
                    parts = line.split(":", 1)
                    if len(parts) == 2:
                        val = parts[1].strip()
                        if val.startswith("12"):
                            self.vpp = "12"
                        elif val.startswith("5"):
                            self.vpp = "5"
            self.vpp12 = self.vpp == "12"
            if rc != 0 and self.vpp == "5":
                self.cis_ok = True
        else:
            self.cis_text = "(windows/legacy/pcmcia_direct.py не найден рядом — паспорт не прочитать)"

        if not self.cis_ok and self.cis_error:
            self.why = f"паспорт не удалось прочитать: {self.cis_error}"
        elif not self.cis_ok and not self.mtd_ok:
            self.why = "карта не отвечает ни по CIS, ни как mtd: контакты/слот/питание"
        elif self.cis_ok and not self.mtd_ok:
            self.why = "карта отвечает (паспорт прочитан), но ядро не дало окно памяти"

    def _run(self, cmd: list[str]) -> tuple[str, int]:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True)
            return (res.stdout or "") + (res.stderr or ""), res.returncode
        except OSError as exc:
            return str(exc), 1

    def print_state(self) -> None:
        sec("СОСТОЯНИЕ")
        say(f"  набор:        {KIT}")
        version = ""
        try:
            for line in self.direct.read_text(encoding="utf-8").splitlines():
                if line.startswith("KIT_VERSION"):
                    version = line.split("=", 1)[1].strip().strip('"')
                    break
        except OSError:
            pass
        say(f"  версия:       {version}")
        mtd_line = f"{self.mtd}, {human(self.mtd_size_bytes)}" if self.mtd_ok else f"нет {self.mtd}"
        say(f"  карта (mtd):  {mtd_line}")
        say(f"  паспорт:      {'прочитан' if self.cis_ok else 'НЕ прочитан'}")
        if self.product:
            say(f"  продукт:      {self.product}")
        if self.vpp12:
            say(f"  запись:       {C_R}НЕВОЗМОЖНА{C_0} — микросхемы требуют Vpp 12 В, слот даёт 5 В")
        elif self.cis_ok and not self.vpp:
            say(f"  запись:       {C_Y}неизвестно{C_0} — по паспорту тип микросхем не определить")
        elif self.mtd_ok:
            say(f"  запись:       возможна ({self.mtd} появился)")
        else:
            say(f"  запись:       {C_Y}невозможна{C_0} — нет устройства mtd")
        if self.why:
            say(f"  почему:       {self.why}")


# --------------------------------------------------------------------------- #
# Проверка прав                                                               #
# --------------------------------------------------------------------------- #
def need_root(action: str) -> bool:
    if os.geteuid() == 0:
        return True
    if os.environ.get("PCMCIA_SKIP_ROOT", "") == "1":
        # Для тестов: пропускаем проверку root, но предупреждаем.
        warn(f"пропускаю проверку root (PCMCIA_SKIP_ROOT=1)")
        return True
    err(f"нужен root: sudo python3 {sys.argv[0]} {action}")
    return False


# --------------------------------------------------------------------------- #
# Подтверждение                                                               #
# --------------------------------------------------------------------------- #
def confirm(prompt: str, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        say("  (подтверждения не спросить: запуск без терминала — по ssh)")
        say("  повторите с ключом --yes, если точно хотите выполнить")
        return False
    ans = input(f"{prompt} (YES): ").strip()
    return ans == "YES"


def pause() -> None:
    if os.environ.get("BATCH", "0") == "1":
        return
    print()
    try:
        input("Enter — продолжить...")
    except EOFError:
        pass


# --------------------------------------------------------------------------- #
# act_info — паспорт и вердикт                                                #
# --------------------------------------------------------------------------- #
def act_info(state: CardState, assume_yes: bool = False) -> int:
    if not need_root("info"):
        return 2
    state.probe()
    state.print_state()
    sec("ПАСПОРТ (attribute memory, мимо ядра)")
    for line in state.cis_text.splitlines():
        say(f"  {line}")
    return act_verdict(state)


def act_verdict(state: CardState) -> int:
    state.probe() if getattr(state, "_fresh", True) else None
    state._fresh = False

    rd = "да" if state.cis_ok else "нет"
    rd_why = "" if state.cis_ok else "паспорт не читается"
    wr = "нет"
    wr_why = ""

    if state.vpp12:
        wr = "нет"
        wr_why = "карта требует Vpp 12 В (в слоте 5 В)"
    elif state.mtd_ok and state.vpp:
        wr = "да"
    elif state.mtd_ok:
        wr = "?"
        wr_why = "напряжение записи по паспорту неизвестно — пробная запись сектора"
    else:
        wr = "нет"
        wr_why = "нет /dev/mtd (ядро не дало окно памяти)"

    sec("ВЕРДИКТ")
    rd_line = f"  чтение : {rd}"
    if rd_why:
        rd_line += f"  ({rd_why})"
    say(rd_line)
    wr_line = f"  запись : {wr}"
    if wr_why:
        wr_line += f"  ({wr_why})"
    say(wr_line)

    if JSON_MODE:
        import json
        verdict = {
            "read": rd,
            "write": wr,
            "mtd": "1" if state.mtd_ok else "0",
            "cis": "1" if state.cis_ok else "0",
            "vpp12_need": state.vpp12,
            "product": state.product.replace('"', ''),
        }
        print(f"JSON {json.dumps(verdict, ensure_ascii=False)}")

    if rd == "нет" and state.cis_error:
        say("  Итог: паспорт не прочитан не из-за карты — сначала устраните причину")
        say("  в строке «почему» выше, потом повторите этот пункт.")
        return 4
    if rd == "нет":
        say("  Итог: ни читать, ни писать нечего. Начните с контактов карты и слота")
        say("  (пункт 7 - диагностика), потом повторите пункт 1.")
        return 3
    if wr == "?":
        say("  Итог: карту можно читать и сохранить образ (пункт 2).")
        say("  Запись не подтверждена: сначала сделайте образ, потом пробную")
        say("  запись через пункт 3 (он пишет ОДИН сектор и сверяет).")
        return 0
    if wr == "нет":
        say("  Итог: карту можно ЧИТАТЬ и сохранить образ (пункт 2);")
        say("  запись на этой машине не сделать — причина выше.")
        return 3
    say("  Итог: доступны и чтение (пункт 2), и запись (пункты 3-4).")
    return 0


# --------------------------------------------------------------------------- #
# act_read — снятие образа (двойное чтение)                                   #
# --------------------------------------------------------------------------- #
def act_read(state: CardState, out_path: str | None, assume_yes: bool = False) -> int:
    if not need_root("read"):
        return 2
    if not out_path:
        out_path = f"/root/card-{time.strftime('%Y%m%d-%H%M')}.bin"
        print(f"Куда сохранить образ [{out_path}]: ", end="")
        try:
            out_path = input().strip() or out_path
        except EOFError:
            return 1

    state.probe()
    if not state.cis_ok:
        err("паспорт не читается — образ с такой карты снимать бессмысленно")
        say("  Сначала: паспорт/контакты (пункт 1), карта вставлена до конца?")
        return 2

    out_p = Path(out_path)
    need = 16 * 1024 * 1024
    dir_p = out_p.parent
    if not dir_p.is_dir():
        err(f"нет каталога {dir_p}")
        return 2

    # проверка свободного места (df -Pk, килобайты в 4-й колонке)
    # LC_ALL=C — чтобы df выводил точки вместо запятых («1,4G» -> «1.4G»)
    free = 0
    try:
        env_df = dict(os.environ)
        env_df["LC_ALL"] = "C"
        res = subprocess.run(
            ["df", "-Pk", str(dir_p)], capture_output=True, text=True,
            env=env_df,
        )
        lines = res.stdout.splitlines()
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 4:
                free = int(parts[3]) * 1024
    except (ValueError, OSError, subprocess.SubprocessError):
        free = 0
    if free and free < need:
        err(f"на {dir_p} свободно {human(free)}, нужно хотя бы {human(need)}")
        return 2

    sec("СНЯТИЕ ОБРАЗА (только чтение, на карту ничего не пишется)")
    say(f"  файл: {out_p}")
    say("  размер: по паспорту карты (и проверкой повтора адресов), чтение дважды + сверка")

    out, rc = _run_direct(["--read", str(out_p), "--double", "--size", "auto"])
    say(out, end="")
    if rc != 0 or not out_p.is_file():
        err("образ не создан (причина выше)")
        say("  Два чтения различаются = контакты/нестабильность; окно занято = см. пункт 7 (диагностика).")
        return 2

    sec("СВОДКА ПО ОБРАЗУ")
    out2, rc2 = _run_direct(["--image-report", str(out_p)])
    say(out2, end="")

    say(f"IMG {out_p}")
    state._fresh = False
    return 0


def _run_direct(args: list[str]) -> tuple[str, int]:
    """Запустить pcmcia_direct.py с аргументами."""
    cmd = [sys.executable, str(DIRECT)] + args
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
        return (res.stdout or "") + (res.stderr or ""), res.returncode
    except OSError as exc:
        return str(exc), 1


# --------------------------------------------------------------------------- #
# act_write — запись образа через /dev/mtdN                                   #
# --------------------------------------------------------------------------- #
def act_write(state: CardState, img_path: str | None, assume_yes: bool = False) -> int:
    if not need_root("write"):
        return 2
    if not img_path:
        print("Путь к файлу-образу: ", end="")
        try:
            img_path = input().strip()
        except EOFError:
            return 1
    img_p = Path(img_path)
    if not img_p.is_file():
        err(f"нет файла {img_p}")
        return 2

    state.probe()
    sec("ЗАПИСЬ НА КАРТУ")

    if not state.mtd_ok:
        err(f"писать нечем: нет {state.mtd}")
        say("  Ядро не дало карте окно памяти ('unable to map card memory!'),")
        say("  поэтому flashcp/flash_erase не с чем работать. Образ при этом")
        say("  снимается нормально (пункт 2) — карта читается мимо ядра.")
        if have("modinfo"):
            say("  Проверить другой драйвер: modinfo pcmciamtd   (в Debian это модуль")
            say("  ядра CONFIG_MTD_PCMCIA). Загрузка вручную:")
            say("    sudo modprobe pcmciamtd mem_type=0 bankwidth=2 force_size=4")
            say("  force_size — размер в МиБ; mem_type=0 — JEDEC+CFI; bankwidth=2 —")
            say("  два 8-битных чипа в паре (у этой карты 2 x Am29F016).")
            say("  Если модуль уже загружен: sudo modprobe -r pcmciamtd, затем снова.")
        act_verdict(state)
        return 3

    if state.vpp12:
        err("эта карта требует Vpp 12 В (28Fxxx / Am28Fxxx / часть Series 2)")
        say("  Слот ноутбука даёт 5 В. Стирание и запись не выполнятся,")
        say("  а попытки могут испортить содержимое. Нужен программатор.")
        say("  Набор сознательно НЕ пробует: 'стёрлось' на такой карте — неправда.")
        act_verdict(state)
        return 3

    if not have("flashcp"):
        err("нет flashcp (sudo apt install mtd-utils)")
        return 2
    if not have("flash_erase"):
        err("нет flash_erase (sudo apt install mtd-utils)")
        return 2

    # смонтирована?
    try:
        with open("/proc/mounts") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == state.mtd:
                    err(f"карта смонтирована в {parts[1]} — сначала: umount {parts[1]}")
                    return 2
    except OSError:
        pass

    isz = img_p.stat().st_size
    csz = state.mtd_size_bytes or mtd_size(state.mtd)
    say(f"  образ: {human(isz)}   карта: {human(csz)}")
    if isz > csz:
        err("образ больше карты — запись отменена")
        return 2
    if isz != csz:
        warn(f"образ меньше карты: остаток ({human(csz - isz)}) будет СТЁРТ")

    _md5_res = subprocess.run(["md5sum", str(img_p)], capture_output=True, text=True)
    for line in _md5_res.stdout.splitlines():
        say(f"  {line}")

    # ПРОБА ОДНОГО СЕКТОРА
    probe = os.environ.get("PROBE", "1")
    if probe == "1":
        sec("ПРОБА: пишу ОДИН сектор и сверяю (образ при этом не трогаю)")
        esz = mtd_erase_size(state.mtd) or 65536
        say(f"  сектор: {esz} байт — если карта не пишется, дальше не пойдём.")
        ptmp = Path(tempfile.mktemp())
        try:
            with open(img_p, "rb") as src, open(ptmp, "wb") as dst:
                dst.write(src.read(esz))
            r1 = subprocess.run(
                ["flash_erase", state.mtd, "0", "1"],
                capture_output=True, text=True,
            )
            if r1.returncode != 0:
                err("пробное стирание не удалось — карта не пишется в этом слоте")
                act_verdict(state)
                return 3
            r2 = subprocess.run(
                ["flashcp", "-v", str(ptmp), state.mtd],
                capture_output=True, text=True,
            )
            if r2.returncode != 0:
                err("пробная запись не удалась — карта не пишется в этом слоте")
                act_verdict(state)
                return 3
            ptmp_r = Path(str(ptmp) + ".r")
            subprocess.run(
                ["mtd_debug", "read", state.mtd, "0", str(esz), str(ptmp_r)],
                capture_output=True, text=True,
            )
            if ptmp.read_bytes() == ptmp_r.read_bytes():
                ok("проба сошлась — карта ПИШЕТСЯ (первый сектор уже содержит образ)")
            else:
                err("ПРОБА НЕ СОШЛАСЬ — на карте не то, что записали")
                say("  Возможные причины: микросхеме нужно Vpp 12 В (тогда запись в слоте")
                say("  невозможна), изношенный сектор, плохой контакт, или ядро не так")
                say("  считает размер сектора. Полную запись НЕ начинаю: образ цел,")
                say("  на карте испорчен максимум первый сектор.")
                try:
                    import filecmp
                    diffs = []
                    with open(ptmp, "rb") as a, open(ptmp_r, "rb") as b:
                        da = a.read()
                        db = b.read()
                        for i, (x, y) in enumerate(zip(da, db)):
                            if x != y:
                                diffs.append(i)
                                if len(diffs) >= 3:
                                    break
                    for i in diffs:
                        say(f"    байт {i}")
                except OSError:
                    pass
                act_verdict(state)
                return 3
        finally:
            try:
                ptmp.unlink(missing_ok=True)
                Path(str(ptmp) + ".r").unlink(missing_ok=True)
            except OSError:
                pass

    if not confirm("Стереть карту и записать этот образ", assume_yes):
        say("  отменено")
        return 1

    sec("СТИРАНИЕ И ЗАПИСЬ")
    r = subprocess.run(["flash_erase", state.mtd, "0", "0"], capture_output=True, text=True)
    if r.returncode != 0:
        err("стирание не удалось")
        return 2
    ok("стёрто")
    r = subprocess.run(["flashcp", "-v", str(img_p), state.mtd], capture_output=True, text=True)
    if r.returncode != 0:
        err("запись не удалась")
        return 2
    ok("записано")

    sec("СВЕРКА (читаю обратно и сравниваю побайтно)")
    tmp = Path(tempfile.mktemp())
    try:
        r = subprocess.run(
            ["mtd_debug", "read", state.mtd, "0", str(isz), str(tmp)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            err("чтение для сверки не удалось")
            return 2
        if img_p.read_bytes() == tmp.read_bytes():
            ok("сверка сошлась — карта содержит ровно этот образ")
            return 0
        err("СВЕРКА НЕ СОШЛАСЬ — на карте не то, что записали")
        say("  Бывает при Vpp не по паспорту, изношенных ячейках или плохом контакте.")
        try:
            da = img_p.read_bytes()
            db = tmp.read_bytes()
            cnt = 0
            for i, (x, y) in enumerate(zip(da, db)):
                if x != y:
                    say(f"    байт {i}")
                    cnt += 1
                    if cnt >= 5:
                        break
        except OSError:
            pass
        return 2
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# act_erase                                                                   #
# --------------------------------------------------------------------------- #
def act_erase(state: CardState, assume_yes: bool = False) -> int:
    if not need_root("erase"):
        return 2
    state.probe()
    if not state.mtd_ok:
        err(f"нет {state.mtd} — стирать нечего")
        act_verdict(state)
        return 3
    if state.vpp12:
        err("карта требует Vpp 12 В — стирание не выполнится")
        return 3
    if not state.vpp:
        warn("напряжение записи по паспорту неизвестно — стирание может не сработать, проверьте результат")
    if not have("flash_erase"):
        err("нет flash_erase (sudo apt install mtd-utils)")
        return 2
    say(f"{C_R}Полное стирание {state.mtd} ({human(state.mtd_size_bytes)}) — данные будут потеряны{C_0}")
    if not confirm("Точно стереть", assume_yes):
        say("  отменено")
        return 1
    r = subprocess.run(["flash_erase", state.mtd, "0", "0"], capture_output=True, text=True)
    if r.returncode == 0:
        ok("стёрто")
        return 0
    err("стирание не удалось")
    return 2


# --------------------------------------------------------------------------- #
# act_verify                                                                  #
# --------------------------------------------------------------------------- #
def act_verify(state: CardState, img_path: str | None) -> int:
    if not need_root("verify"):
        return 2
    if not img_path:
        print("Файл для сверки: ", end="")
        try:
            img_path = input().strip()
        except EOFError:
            return 1
    img_p = Path(img_path)
    if not img_p.is_file():
        err(f"нет файла {img_p}")
        return 2
    state.probe()
    if not state.mtd_ok:
        err(f"нет {state.mtd} — сверять не с чем")
        return 3

    isz = img_p.stat().st_size
    tmp = Path(tempfile.mktemp())
    try:
        r = subprocess.run(
            ["mtd_debug", "read", state.mtd, "0", str(isz), str(tmp)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            err("чтение не удалось")
            return 2
        if img_p.read_bytes() == tmp.read_bytes():
            ok("совпадает")
            return 0
        err("различается")
        try:
            da = img_p.read_bytes()
            db = tmp.read_bytes()
            cnt = 0
            for i, (x, y) in enumerate(zip(da, db)):
                if x != y:
                    say(f"    байт {i}")
                    cnt += 1
                    if cnt >= 5:
                        break
        except OSError:
            pass
        return 2
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# act_canwrite                                                                #
# --------------------------------------------------------------------------- #
def act_canwrite(state: CardState, assume_yes: bool = False) -> int:
    if not need_root("canwrite"):
        return 2
    state.probe()
    sec("МОЖНО ЛИ ПИСАТЬ")
    say("1) устройство записи:")
    if state.mtd_ok:
        say(f"   {C_G}есть{C_0} — {state.mtd}, {human(state.mtd_size_bytes)}")
    else:
        say(f"   {C_Y}нет{C_0} — /dev/mtd0 не появился")
        try:
            dmesg = subprocess.run(
                ["dmesg"], capture_output=True, text=True
            ).stdout
            cnt1 = dmesg.lower().count("unable to map card memory")
            cnt2 = dmesg.lower().count("excluding")
            say(f"   dmesg: {cnt1} строк 'unable to map card memory!'")
            say(f"   строки 'excluding' по окнам: {cnt2}")
        except (subprocess.SubprocessError, OSError):
            say("   dmesg недоступен")

    say("2) что требует карта:")
    for line in state.cis_text.splitlines():
        low = line.lower()
        if any(k in low for k in ("продукт", "jedec", "ёмкость", "чипы", "тип памяти")):
            say(f"   {line}")
    if state.vpp12:
        say(f"   {C_R}→ нужен Vpp 12 В: в слоте ноутбука (5 В) запись не выполнится{C_0}")
    elif not state.vpp:
        say(f"   {C_Y}→ напряжение записи неизвестно{C_0}: у 5-вольтовых микросхем")
        say("     (Am29F016 и подобные) запись в слоте работает, у 28Fxxx — нет.")
        say("     Единственная проверка — записать один сектор и сверить (пункт 3).")
    else:
        say(f"   {C_G}→ 5 В: 12 В не требуется, запись возможна{C_0}")
    say("3) что остаётся:")
    say("   * снять образ и хранить его (пункт 2) — безопасно;")
    say("   * записать образ программатором, поддерживающим эти микросхемы;")
    say("   * если /dev/mtdN появится (другая загрузка/другой слот) — пункты 3-5.")
    rc = act_verdict(state)
    if state.mtd_ok and not state.vpp12:
        return 0
    return 3


# --------------------------------------------------------------------------- #
# act_diag                                                                    #
# --------------------------------------------------------------------------- #
def act_diag() -> int:
    sec("ДИАГНОСТИКА: окна памяти и кто их держит")
    say("окна CardBus (/proc/iomem):")
    try:
        with open("/proc/iomem") as fh:
            found = False
            for line in fh:
                if "pci cardbus" in line.lower():
                    say(f"  {line.rstrip()}")
                    found = True
            if not found:
                say("  (строк нет)")
    except OSError:
        say("  (нельзя прочитать /proc/iomem)")
    say("")

    if have("lspci"):
        say("мост и драйвер:")
        try:
            out = subprocess.run(["lspci", "-nn"], capture_output=True, text=True).stdout
            for line in out.splitlines():
                if "cardbus" in line.lower():
                    say(f"  {line}")
        except (subprocess.SubprocessError, OSError):
            say("  lspci не удалось запустить")
        try:
            out = subprocess.run(["lspci", "-vv"], capture_output=True, text=True).stdout
            capture = False
            for line in out.splitlines():
                if "cardbus" in line.lower():
                    capture = True
                if capture and ("memory window" in line.lower() or "kernel driver" in line.lower()):
                    say(f"  {line}")
                    capture = False
        except (subprocess.SubprocessError, OSError):
            pass
    else:
        say("  lspci нет (sudo apt install pciutils)")
    say("")

    say("модули:")
    try:
        out = subprocess.run(["lsmod"], capture_output=True, text=True).stdout
        found = False
        for line in out.splitlines():
            low = line.lower()
            if any(k in low for k in ("yenta", "pcmcia", "mtd")):
                say(f"  {line}")
                found = True
        if not found:
            say("  (нет)")
    except (subprocess.SubprocessError, OSError):
        say("  lsmod недоступен")
    say("")

    say("ядро про карту:")
    try:
        out = subprocess.run(["dmesg"], capture_output=True, text=True).stdout
        lines = [l for l in out.splitlines() if any(k in l.lower() for k in ("pcmcia", "cardbus", "mtd"))]
        for line in lines[-12:]:
            say(f"  {line}")
    except (subprocess.SubprocessError, OSError):
        say("  dmesg недоступен")
    say("")

    say("проверка доступа:")
    if DIRECT.is_file():
        out, rc = _run_direct(["--probe"])
        for line in out.splitlines()[:20]:
            say(f"  {line}")
    say("")
    say("Если /dev/mem отказывает на страницу, набор читает тот же адрес через")
    say("файл BAR моста в sysfs. Если не выходит и так — окно держит драйвер.")
    return 0


# --------------------------------------------------------------------------- #
# act_report                                                                  #
# --------------------------------------------------------------------------- #
def act_report(out_path: str | None) -> int:
    collect = KIT / "collect_all.py"
    if not collect.is_file():
        err("нет collect_all.py рядом")
        return 2
    args = [sys.executable, str(collect)]
    if out_path:
        args += ["--out", out_path]
    env = dict(os.environ)
    env["BATCH"] = "1"
    r = subprocess.run(args, env=env)
    return r.returncode


# --------------------------------------------------------------------------- #
# act_selftest                                                                #
# --------------------------------------------------------------------------- #
def act_selftest(full: bool = False) -> int:
    test_scripts = KIT / "tests" / "test_scripts.py"
    if not test_scripts.is_file():
        err("в наборе нет tests/ - архив распакован не целиком")
        return 2
    sec("ПРОВЕРКА НАБОРА (карта не нужна)")
    mode = "полный" if full else "быстрый (главные проверки)"
    approx = "1-2 минут" if full else "30 секунд"
    say(f"  Режим: {mode}")
    say(f"  Это занимает около {approx}.")
    args = [sys.executable, str(test_scripts)]
    if not full:
        args.append("--fast")
    try:
        res = subprocess.run(args, cwd=str(KIT), capture_output=True, text=True)
        out = res.stdout + res.stderr
        rc = res.returncode
    except OSError as exc:
        err(str(exc))
        return 2
    for line in out.splitlines()[-25:]:
        say(f"  {line}")
    say("")
    if rc == 0:
        ok("набор цел: тесты прошли")
    else:
        err("тесты не прошли - набор повреждён при копировании или на системе")
        say("  чего-то не хватает. Перекачайте архив; подробности выше.")
    return rc


# --------------------------------------------------------------------------- #
# act_chip                                                                    #
# --------------------------------------------------------------------------- #
def act_chip() -> int:
    if not need_root("chip"):
        return 2
    if not DIRECT.is_file():
        err("нет windows/legacy/pcmcia_direct.py рядом")
        return 2
    state = CardState(str(MTD), DIRECT)
    state.probe()
    sec("ЧТО ЗА МИКРОСХЕМА В КАРТЕ (ничего не пишется)")
    out, rc = _run_direct(["--identify-chip"])
    for line in out.splitlines():
        say(f"  {line}")
    say("")
    if rc != 0:
        err(f"опрос не удался (код {rc}) — карта может быть не linear flash")
        return rc
    say("  Тип можно задать вручную: --chip ИМЯ (например Am29F016, 28F008SA).")
    say("  Если написано «12 В» — запись в слоте ноутбука невозможна, это")
    say("  ограничение железа, а не программы.")
    return 0


# --------------------------------------------------------------------------- #
# act_fwprobe / act_fwwrite                                                   #
# --------------------------------------------------------------------------- #
def act_fwprobe(state: CardState, assume_yes: bool = False) -> int:
    if not need_root("fwprobe"):
        return 2
    if not DIRECT.is_file():
        err("нет windows/legacy/pcmcia_direct.py рядом")
        return 2
    state.probe()
    sec("ПРОБА ЗАПИСИ БЕЗ ДРАЙВЕРА (один сектор, данные возвращаются)")
    say("  Что произойдёт: прочитаю один сектор (64 КиБ), сохраню его в файл,")
    say("  сотру, запишу узор, сверю и верну исходные байты на место.")
    say("  Остальное содержимое карты не трогается.")
    say("  Если карта не пишется - файл с исходными байтами останется у вас.")
    if not confirm("Пробовать запись в сектор 0", assume_yes):
        say("  ничего не сделано")
        return 1
    out, rc = _run_direct(["--write-probe", "--sector", "0"])
    for line in out.splitlines():
        say(f"  {line}")
    say("")
    if rc == 0:
        ok("проба прошла — карта пишется этим способом")
        say("  дальше: пункт «Записать образ без драйвера» (fwwrite <файл>)")
    else:
        err("проба не прошла — на карту через набор писать нельзя")
        say("  образ и остальные секторы целы; причина выше.")
    return rc


def act_fwwrite(state: CardState, img_path: str | None, assume_yes: bool = False) -> int:
    if not need_root("fwwrite"):
        return 2
    if not img_path:
        print("Путь к файлу-образу: ", end="")
        try:
            img_path = input().strip()
        except EOFError:
            return 1
    img_p = Path(img_path)
    if not img_p.is_file():
        err(f"нет файла {img_p}")
        return 2
    if not DIRECT.is_file():
        err("нет windows/legacy/pcmcia_direct.py рядом")
        return 2
    state.probe()
    sec(f"ЗАПИСЬ ОБРАЗА БЕЗ ДРАЙВЕРА ({img_p.name})")
    say("  Запись идёт командами JEDEC прямо в микросхему через окно моста.")
    say("  /dev/mtdN для этого не нужен.")
    say(f"  {C_Y}Сначала убедитесь, что проба (пункт 9 / fwprobe) прошла.{C_0}")
    try:
        md5 = subprocess.run(
            ["md5sum", str(img_p)], capture_output=True, text=True
        ).stdout.split()[0]
        say(f"  md5 файла: {md5}")
    except (subprocess.SubprocessError, OSError, IndexError):
        pass
    if not confirm("Стереть нужные секторы и записать этот образ", assume_yes):
        say("  ничего не сделано")
        return 1
    out, rc = _run_direct(["--write", str(img_p)])
    for line in out.splitlines():
        say(f"  {line}")
    say("")
    if rc == 0:
        ok("записано и сверено")
    else:
        err("не удалось (см. выше)")
    return rc


# --------------------------------------------------------------------------- #
# usage / menu                                                                #
# --------------------------------------------------------------------------- #
def usage() -> None:
    print("""Пульт управления картой PCMCIA — стороны ноутбука.

Запуск на ноутбуке с картой:
    sudo python3 pcmcia_ctl.py              # меню
    sudo python3 pcmcia_ctl.py info         # паспорт и вердикт
    sudo python3 pcmcia_ctl.py read card.bin
    sudo python3 pcmcia_ctl.py write card.bin --yes

КОМАНДЫ (то же, что пункты меню):
  info                  паспорт и вердикт
  read [файл]           снять образ (по умолчанию /root/card-ДАТА.bin)
  write <файл>          стереть карту и записать образ, затем сверить
  erase                 стереть карту
  verify <файл>         сверить файл с картой, ничего не записывая
  canwrite              можно ли писать эту карту (подробно)
  diag                  окна памяти, драйвер, dmesg
  report [файл]         полный отчёт collect_all.py
  fwprobe               ПРОБА записи без драйвера: один сектор, данные возвращаются
  fwwrite <файл>        записать образ без драйвера (команды JEDEC через окно)
  chip                  что за микросхема в карте: паспорт, ID, сектор,
                        протокол, напряжение (ничего не пишет)
  selftest [--full]     проверить сам набор (тесты на модели, карта не нужна)
  menu                  меню (по умолчанию)

Чем fwwrite отличается от write: write пишет через /dev/mtdN (нужен драйвер),
fwwrite - командами микросхемы через окно моста, драйвер не нужен. Если
карта не пишется, fwwrite об этом скажет по DQ7/DQ5 и остановится.

КЛЮЧИ:
  --yes                 не спрашивать YES (для запуска с Windows по ssh)
  --json                печатать вердикт машиночитаемо (строка JSON {...})
  MTD=/dev/mtdN         другое устройство карты

ПРИМЕРЫ:
  sudo python3 pcmcia_ctl.py info
  sudo python3 pcmcia_ctl.py read /root/card.bin
  sudo python3 pcmcia_ctl.py write /root/card.bin --yes
""")


def menu(state: CardState) -> None:
    while True:
        state.probe()
        clear_line = "\033[2J\033[H" if _IS_TTY else ""
        print(clear_line, end="")
        print(f"{C_B}================================================================{C_0}")
        print("  ПУЛЬТ УПРАВЛЕНИЯ PCMCIA FLASH — карта в слоте")
        print(f"{C_B}================================================================{C_0}")
        mtd_line = f"{state.mtd} {human(state.mtd_size_bytes)}" if state.mtd_ok else f"нет {state.mtd}"
        print(f"  карта:  {mtd_line}")
        cis_line = " прочитан" if state.cis_ok else " НЕ прочитан"
        print(f"  паспорт:{cis_line}")
        if state.vpp12:
            write_line = "невозможна (Vpp 12 В, пункт 6)"
        elif state.mtd_ok:
            if state.vpp:
                write_line = "возможна (через mtd)"
            else:
                write_line = "не проверена (пункты 9, 6)"
        else:
            write_line = "нет mtd, но есть пункт 9"
        print(f"  запись: {write_line}")
        print()
        print("   1) Паспорт и вердикт (что можно, что нельзя)")
        print("   2) Снять образ карты (двойное чтение + md5)")
        print("   3) Записать образ на карту (+ сверка)")
        print("   4) Стереть карту")
        print("   5) Сверить файл с картой (без записи)")
        print("   6) Можно ли писать именно эту карту — подробно")
        print("   7) Диагностика: окна памяти, драйвер, dmesg")
        print("   8) Полный отчёт (как с Windows, все разделы)")
        print("   9) ПРОБА записи БЕЗ драйвера (1 сектор, данные возвращаются)")
        print("  10) Записать образ БЕЗ драйвера (команды JEDEC через окно)")
        print("  11) Что за микросхема в карте (тип, сектор, протокол, напряжение)")
        print("  12) Проверить сам набор (тесты; карта не нужна, ~30 с)")
        print("   L) Лог")
        print("   0) Выход")
        print()
        try:
            c = input("Пункт: ").strip()
        except EOFError:
            break
        if c == "1":
            act_info(state)
        elif c == "2":
            act_read(state, None)
        elif c == "3":
            act_write(state, None)
        elif c == "4":
            act_erase(state)
        elif c == "5":
            act_verify(state, None)
        elif c == "6":
            act_canwrite(state)
        elif c == "7":
            act_diag()
        elif c == "8":
            act_report(None)
        elif c == "9":
            act_fwprobe(state)
        elif c == "10":
            act_fwwrite(state, None)
        elif c == "11":
            act_chip()
        elif c == "12":
            act_selftest(full=True)
        elif c.lower() == "l":
            log_p = LOG_PATH or LOG_DEFAULT
            try:
                lines = log_p.read_text(encoding="utf-8").splitlines()
                for line in lines[-40:]:
                    print(line)
            except OSError:
                pass
        elif c in ("0", "q", "Q"):
            break
        else:
            pass
        pause()


# --------------------------------------------------------------------------- #
# main                                                                        #
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pcmcia_ctl.py",
        description="Пульт управления картой PCMCIA (сторона ноутбука).",
    )
    p.add_argument("action", nargs="?", default="menu",
                   choices=["menu", "info", "read", "write", "erase", "verify",
                            "canwrite", "diag", "report", "fwprobe", "fwwrite",
                            "chip", "selftest"],
                   help="что сделать (по умолчанию: menu)")
    p.add_argument("arg", nargs="?", default="",
                   help="аргумент команды (путь к файлу и т.п.)")
    p.add_argument("--yes", "-y", action="store_true",
                   help="не спрашивать YES (для запуска с Windows по ssh)")
    p.add_argument("--json", action="store_true",
                   help="печатать вердикт машиночитаемо (строка JSON {...})")
    p.add_argument("--fast", action="store_true",
                   help="быстрый режим selftest")
    p.add_argument("--full", action="store_true",
                   help="полный режим selftest")
    return p


from contextlib import contextmanager


@contextmanager
def card_lock():
    """Сессионная блокировка карты: только один экземпляр работает с картой.

    Используется только для действий, которые модифицируют карту
    (write, erase, fwprobe, fwwrite, menu) — чтобы не блокировать
    selftest/diag/chip/read/verify, которые тесты запускают внутри себя.
    """
    lock_fd = None
    try:
        lock_fd = os.open(str(LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        err(f"не удалось создать файл блокировки {LOCK_FILE}: {exc}")
        yield False
        return
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
    except OSError:
        acquired = False
    if not acquired:
        err(f"ошибка: другой экземпляр pcmcia_ctl.py уже работает (блокировка {LOCK_FILE})")
        say("        карта защищена от одновременного доступа")
        try:
            os.close(lock_fd)
        except OSError:
            pass
        yield False
        return
    try:
        yield True
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(lock_fd)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    global JSON_MODE, LOG_PATH
    parser = build_parser()
    args = parser.parse_args(argv)

    JSON_MODE = args.json
    if JSON_MODE:
        LOG_PATH = None  # в JSON-режиме не пишем лишнего в stdout

    state = CardState(str(MTD), DIRECT)
    state._fresh = True

    # Действия, модифицирующие карту — под сессионной блокировкой.
    write_actions = {"write", "erase", "fwprobe", "fwwrite", "menu"}

    if args.action in write_actions:
        with card_lock() as locked:
            if not locked:
                return 3
            return _dispatch(args, state)
    else:
        return _dispatch(args, state)


def _dispatch(args: argparse.Namespace, state: CardState) -> int:
    if args.action == "menu":
        menu(state)
        return 0
    if args.action == "info":
        return act_info(state, assume_yes=args.yes)
    if args.action == "read":
        return act_read(state, args.arg or None, assume_yes=args.yes)
    if args.action == "write":
        return act_write(state, args.arg or None, assume_yes=args.yes)
    if args.action == "erase":
        return act_erase(state, assume_yes=args.yes)
    if args.action == "verify":
        return act_verify(state, args.arg or None)
    if args.action == "canwrite":
        return act_canwrite(state, assume_yes=args.yes)
    if args.action == "diag":
        return act_diag()
    if args.action == "report":
        return act_report(args.arg or None)
    if args.action == "fwprobe":
        return act_fwprobe(state, assume_yes=args.yes)
    if args.action == "fwwrite":
        return act_fwwrite(state, args.arg or None, assume_yes=args.yes)
    if args.action == "chip":
        return act_chip()
    if args.action == "selftest":
        return act_selftest(full=args.full)
    parser.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    sys.exit(main())
