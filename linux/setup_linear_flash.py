#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""setup_linear_flash.py

Разовая настройка Linux (Debian 12 i386 / antiX / MX и совместимые)
для работы с 16-битными PCMCIA linear flash картами (Intel/AMD/Catalyst/Sharp)
на ноутбуке Fujitsu-Siemens AMILO Pro V2030D (CardBus TI PCIxx21).

Запуск:  sudo python3 ./setup_linear_flash.py [vpp]      vpp в 0.1 В, по умолчанию 50 (=5.0 В)
         sudo python3 ./setup_linear_flash.py 33         для 3.3-вольтовых карт

Скрипт: ставит пакеты, сам находит окна памяти слота через lspci,
пишет /etc/pcmcia/config.opts, настраивает опции модуля pcmciamtd,
udev-права и загружает драйверы. Ничего не стирает на картах.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

VPP = "50"
CONF_OPTS = Path("/etc/pcmcia/config.opts")
MODPROBE_CONF = Path("/etc/modprobe.d/pcmciamtd.conf")
UDEV_RULE = Path("/etc/udev/rules.d/99-pcmciamtd.rules")


def die(msg: str) -> None:
    print(f"ОШИБКА: {msg}", file=sys.stderr)
    sys.exit(1)


def info(title: str) -> None:
    print(f"\n=== {title} ===")


def have(name: str) -> bool:
    from shutil import which
    return which(name) is not None


def shell(cmd: str) -> str:
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return res.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def run(cmd: list[str]) -> int:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
        print(res.stdout, end="")
        if res.stderr.strip():
            print(res.stderr, end="", file=sys.stderr)
        return res.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"не удалось запустить: {exc}", file=sys.stderr)
        return 1


def floor1m(addr_hex: str) -> str:
    """start окна -> вниз до границы 1 МиБ (проверенный приём для TI-мостов)."""
    val = int(addr_hex, 16)
    return f"0x{val & ~0xFFFFF:x}"


def mkwindow(line: str) -> str | None:
    """$1 = строка \"Memory window 0: a2000000-a3ffffff (prefetchable)\" -> config.opts строка."""
    m = re.search(r"([0-9a-fA-F]{8})-([0-9a-fA-F]{8})", line)
    if not m:
        return None
    start, end = m.group(1), m.group(2)
    ismem = "memory" in line.lower()
    if ismem:
        return f"include memory {floor1m(start)}-0x{end}"
    else:
        return f"include port 0x{start}-0x{end}"


def main(argv: list[str] | None = None) -> int:
    global VPP
    say = print

    args = sys.argv[1:] if argv is None else argv
    if args:
        VPP = args[0]
    if VPP not in ("33", "50", "120"):
        die("vpp должен быть 33 или 50 (единицы 0.1 В)")
    if VPP == "120":
        die("12 В (vpp=120) на этом ноутбуке не подаётся и опасен. "
            "Используйте 50 (5 В) или 33 (3.3 В).")

    if os.geteuid() != 0:
        die("нужен root: sudo python3 ./setup_linear_flash.py")

    info("1/7 Проверка наличия инструментов")
    pkgs = []
    if not have("lspci"):
        pkgs.append("pciutils")
    if not have("mtd_debug"):
        pkgs.append("mtd-utils")
    if not have("pccardctl"):
        pkgs.append("pcmciautils")
    if pkgs:
        say = print
        say(f"Ставлю:{' '.join(pkgs)}")
        r = run(["sh", "-c", f"apt-get update -qq && apt-get install -y {' '.join(pkgs)}"])
        if r != 0:
            die("не удалось поставить пакеты")
    else:
        say = print
        say("pciutils, mtd-utils, pcmciautils уже есть")

    info("2/7 Поиск CardBus-контроллера")
    bridge = shell("lspci -nn | grep -iE 'cardbus' | head -n1 | cut -d' ' -f1")
    if not bridge:
        die("CardBus-мост не найден. Слот выключен в BIOS или контроллер не инициализируется.")
    r = run(["lspci", "-s", bridge])

    info("3/7 Чтение окон памяти/ввода")
    bridge_v = shell(f"lspci -v -s {bridge}")
    win_lines = [l for l in bridge_v.splitlines()
                 if "Memory window" in l or "I/O window" in l]
    if not win_lines:
        die("в lspci нет строк окон — драйвер yenta не привязан к мосту")

    opts_lines = [
        f"# Сгенерировано setup_linear_flash.py {time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
        f"# Окна взяты из: lspci -v -s {bridge}",
        "# Не объединяйте соседние окна в одну строку include.",
    ]
    for line in win_lines:
        w = mkwindow(line)
        if w:
            opts_lines.append(w)

    new_opts = CONF_OPTS.with_suffix(".opts.new")
    try:
        new_opts.write_text("\n".join(opts_lines) + "\n", encoding="utf-8")
    except OSError as exc:
        die(f"не удалось сформировать config.opts: {exc}")

    if new_opts.stat().st_size == 0:
        die("не удалось разобрать окна из lspci")

    if CONF_OPTS.is_file():
        bak = CONF_OPTS.with_suffix(f".opts.bak.{int(time.time())}")
        Path(CONF_OPTS).replace(bak)
    new_opts.replace(CONF_OPTS)
    say = print
    say(f"Записано в {CONF_OPTS}:")
    print(CONF_OPTS.read_text(encoding="utf-8"), end="")

    info(f"4/7 Опции модуля pcmciamtd (vpp={VPP} = {int(VPP)/10:.1f} В)")
    modprobe_text = (
        "# Vpp подаётся только на время записи/стирания (setvpp=1).\n"
        "# 50 = 5.0 В (карты Intel Series 2+, Value 100, AMD, Catalyst, Sharp)\n"
        "# 33 = 3.3 В (только если карта точно 3.3-вольтовая!)\n"
        f"options pcmciamtd vpp={VPP} setvpp=1\n"
    )
    MODPROBE_CONF.write_text(modprobe_text, encoding="utf-8")
    print(modprobe_text, end="")

    info("5/7 Права на MTD-устройства для группы flash")
    if not shell("getent group flash"):
        run(["groupadd", "flash"])
    udev_text = 'SUBSYSTEM=="mtd", MODE="0660", GROUP="flash"\n'
    UDEV_RULE.write_text(udev_text, encoding="utf-8")
    say(f"Правило: {UDEV_RULE} (добавьте себя: sudo usermod -aG flash $USER)")

    info("6/7 Загрузка драйверов")
    for mod in ("yenta_socket", "pcmcia", "mtd", "jedec_probe", "cfi_probe"):
        run(["modprobe", mod])  # модуль может быть уже загружен или встроен
    if run(["modprobe", "pcmciamtd"]) != 0:
        say("ВНИМАНИЕ: pcmciamtd не загрузился.")
        say("Проверьте: modinfo pcmciamtd ; apt install linux-image-686-pae")
    else:
        say("pcmciamtd загружен")

    info("7/7 Состояние слота и карты")
    run(["pccardctl", "status"])
    say()
    if Path("/dev/mtd0").exists():
        run(["mtd_debug", "info", "/dev/mtd0"])
        say()
        run(["mtdinfo", "-a", "/dev/mtd0"])
    else:
        say("Устройство /dev/mtd0 не появилось.")
        say("Возможные причины и что делать:")
        say("  * карта не вставлена / слот не увидел карту -> вставить до загрузки, протереть контакты;")
        say("  * карта без внятного CIS (анонимная) -> нужен CONFIG_MTD_PCMCIA_ANONYMOUS=y (пересборка модуля);")
        say("  * размер не читается из CIS -> добавить force_size=16 (или 32) в " + str(MODPROBE_CONF) + ";")
        say("  * 8-битная карта -> bankwidth=1 ; SRAM -> mem_type=1 ; ROM -> mem_type=2;")
        say("  * посмотреть лог: dmesg | tail -40")

    say()
    say("Готово. Дальше:")
    say("  sudo python3 ./linux/flashcard.py info")
    say("  sudo python3 ./linux/flashcard.py backup /root/card.bin     # ВСЕГДА сначала бэкап")
    say("  sudo python3 ./linux/flashcard.py erase")
    say("  sudo python3 ./linux/flashcard.py write new.bin")
    say("  sudo python3 ./linux/flashcard.py verify new.bin")
    return 0


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    sys.exit(main(sys.argv[1:]))
