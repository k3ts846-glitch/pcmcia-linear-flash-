#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_remote.py — выполняется на ноутбуке. Вызывается по SSH одной строкой:

    python3 /tmp/pcmcia-ctl/run_remote.py [--image] [--no-slow]

Смысл: вся логика (sudo, отчёт, уборка) — здесь, а не в командной строке ssh.
Так на стороне Windows не нужны кавычки, >, || и $ — меньше поводов
для ошибок cmd.exe.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPORT = HERE / "otchet.txt"
LOG = HERE / ".console.log"

PLANB = False
EXTRA: list[str] = []

for a in sys.argv[1:]:
    if a == "--planb":
        PLANB = True
    else:
        EXTRA.append(a)


def say(text: str = "") -> None:
    print(text)


def is_root() -> bool:
    return os.geteuid() == 0


def sudo_available_nopasswd() -> bool:
    try:
        res = subprocess.run(
            ["sudo", "-n", "true"], capture_output=True, text=True
        )
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def run_collect(extra: list[str]) -> int:
    """Запускает collect_all.py с --out REPORT и extra-аргументами."""
    args = [sys.executable, str(HERE / "collect_all.py"), "--out", str(REPORT)]
    args.extend(extra)
    try:
        with open(LOG, "w", encoding="utf-8") as lf:
            res = subprocess.run(args, cwd=str(HERE), stdout=lf, stderr=lf)
        return res.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        say(f"(не удалось запустить collect_all.py: {exc})")
        return 1


def main() -> int:
    os.environ["DEBIAN_FRONTEND"] = "noninteractive"

    # ---------- вариант "план Б": настройка окон памяти ----------
    if PLANB:
        say("===== ПЛАН Б: снимаю проверку окон памяти =====")
        planb = HERE / "planb.py"
        if not planb.is_file():
            say("  planb.py не найден — план Б невозможен без него.")
            say("  Запустите planb.py напрямую на ноутбуке.")
            return 1
        try:
            if is_root():
                res = subprocess.run([sys.executable, str(planb)])
            else:
                res = subprocess.run(["sudo", sys.executable, str(planb)])
            rc = res.returncode
        except (OSError, subprocess.SubprocessError) as exc:
            say(f"  не удалось запустить planb.py: {exc}")
            return 1
        say("")
        say("")
        say("--- вердикт по загрузке (это главное) ---")
        grubcfg = Path("/boot/grub/grub.cfg")
        if grubcfg.is_file():
            try:
                text = grubcfg.read_text(encoding="utf-8", errors="ignore")
                if "iomem=relaxed" in text:
                    say("  ЗАГРУЗКА ГОТОВА: iomem=relaxed есть в /boot/grub/grub.cfg - можно перезагружать")
                    say("  (то же должно быть в самом отчёте: строка 'в /boot/grub/grub.cfg: iomem=relaxed ЕСТЬ')")
                else:
                    say("  НЕ ГОТОВО: iomem=relaxed НЕТ в /boot/grub/grub.cfg - перезагружаться рано")
            except OSError:
                say("  НЕ ГОТОВО: не удалось прочитать /boot/grub/grub.cfg")
        else:
            say("  НЕ ГОТОВО: /boot/grub/grub.cfg не найден")
        say("")
        say("--- состояние сейчас (до перезагрузки) ---")
        try:
            cmdline = Path("/proc/cmdline").read_text(encoding="utf-8")
            has_relaxed = "iomem=relaxed" in cmdline
        except OSError:
            has_relaxed = False
        say(f"  iomem=relaxed в cmdline: {'ДА' if has_relaxed else 'НЕТ'}")
        modconf = Path("/etc/modprobe.d/pcmcia-noprobe.conf")
        say(f"  файл probe_mem: {'есть' if modconf.is_file() else 'нет'}")
        grubd = Path("/etc/default/grub.d/99-pcmcia-relaxed.cfg")
        say(f"  файл grub.d:    {'есть' if grubd.is_file() else 'нет'}")
        say("")
        say("===== ПОСЛЕ ПЕРЕЗАГРУЗКИ =====")
        say("  1) перезагрузите ноутбук С КАРТОЙ В СЛОТЕ (это единственный")
        say("     оставшийся шаг, без него iomem=relaxed не включится):")
        say("       sudo reboot")
        say("  2) после загрузки запустите обычный отчёт (без -PlanB);")
        say("     в разделе 1.1 должно быть 'iomem=relaxed в cmdline : ДА'")
        say("  Вернуть как было:  sudo python3 planb.py --revert")
        try:
            import shutil
            shutil.rmtree(str(HERE), ignore_errors=True)
        except OSError:
            pass
        return rc

    # ---------- обычный отчёт ----------
    try:
        REPORT.unlink(missing_ok=True)
    except OSError:
        pass

    if is_root():
        rc = run_collect(EXTRA)
    elif sudo_available_nopasswd():
        # sudo без пароля разрешён - работаем молча
        rc = run_collect(EXTRA)
    else:
        say("===== НУЖЕН ПАРОЛЬ SUDO =====")
        say("  Вы вошли как обычный пользователь, а разделы 6-8 (память, паспорт,")
        say("  образ) требуют root. Здесь пароль ввести нельзя: ssh запущен без")
        say("  терминала, чтобы не портить передачу файлов.")
        say("")
        say("  Три способа, любой на выбор:")
        say("   1) войти на ноутбук под root:  python run_remote.py --host <адрес> --user root")
        say("   2) разрешить sudo без пароля для этого пользователя (на ноутбуке):")
        say("        echo '$USER ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/pcmcia")
        say("   3) запустить набор прямо на ноутбуке: sudo python3 ./collect_all.py")
        say("")
        say("  Ничего не изменено: на карту не писали, настройки не трогали.")
        return 3

    say("")
    say("===== ОТЧЁТ ЦЕЛИКОМ =====")
    if REPORT.is_file():
        try:
            print(REPORT.read_text(encoding="utf-8", errors="replace"), end="")
        except OSError as exc:
            say(f"(не удалось прочитать отчёт: {exc})")
        card_bin = Path("/root/card.bin")
        if card_bin.is_file():
            say("")
            say("===== ОБРАЗ НА НОУТБУКЕ =====")
            try:
                res = subprocess.run(["ls", "-l", str(card_bin)], capture_output=True, text=True)
                for line in res.stdout.splitlines():
                    say(f"  {line}")
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                res = subprocess.run(["md5sum", str(card_bin)], capture_output=True, text=True)
                for line in res.stdout.splitlines():
                    say(f"  {line}")
            except (OSError, subprocess.SubprocessError):
                pass
    else:
        say(f"(отчёт не создан, код {rc}) - что напечатала программа:")
        try:
            tail = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in tail[-40:]:
                say(line)
        except OSError:
            pass

    # Отчёт остаётся на месте: его забирает к себе клиент (scp),
    # а папку с набором убирает он же после копирования.
    try:
        for entry in HERE.iterdir():
            if entry.name == "otchet.txt":
                continue
            if entry.is_dir():
                import shutil
                shutil.rmtree(str(entry), ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
    except OSError:
        pass

    return rc


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    sys.exit(main())
