#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""collect_all.py — ВСЁ в один отчёт. Запускается на ноутбуке с картой.

Только чтение: на карту ничего не пишется (кроме режима --image, где
снимается образ в файл на диске ноутбука).

    sudo python3 collect_all.py                  # полная диагностика
    sudo python3 collect_all.py --image          # + снять образ в /root/card.bin
    sudo python3 collect_all.py --kit /путь/к/набору --out /root/otchet.txt
    python3 collect_all.py --no-slow             # без --selftest и --diag (быстро)

На выходе: файл report-<дата>.txt (рядом с набором или в /var/tmp).
Его и нужно прислать в переписку.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# --------------------------------------------------------------------------- #
# Путь к набору                                                                 #
# --------------------------------------------------------------------------- #
KIT = Path(__file__).resolve().parent
# скрипт может лежать в подпапке linux/ — тогда набор ищем на уровень выше
if KIT.name == "linux" and (KIT / "..").is_dir():
    KIT = (KIT / "..").resolve()

PCMCIA_CTL = KIT / "pcmcia_ctl.py"

SELF_DIR = KIT
IMAGE_DEFAULT = Path("/root/card.bin")
OUT_DEFAULT: Path | None = None

# --------------------------------------------------------------------------- #
# Цвета только в терминале                                                    #
# --------------------------------------------------------------------------- #
_IS_TTY = sys.stdout.isatty()
C_0 = "\033[0m" if _IS_TTY else ""

# --------------------------------------------------------------------------- #
# Вспомогательный ввод-вывод                                                  #
# --------------------------------------------------------------------------- #
REPORT_PATH: Path | None = None


def say(text: str = "") -> None:
    """Напечатать и продублировать в отчёт."""
    print(text)
    if REPORT_PATH is not None:
        try:
            with open(REPORT_PATH, "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except OSError:
            pass


def sec(title: str) -> None:
    say("")
    say(f"===== {title} =====")


def have(name: str) -> bool:
    """Есть ли программа в PATH."""
    from shutil import which
    return which(name) is not None


def is_root() -> bool:
    return os.geteuid() == 0


def read_text_quiet(path) -> str:
    """Прочитать файл, если он есть и доступен, иначе пустая строка.

    /boot/grub/grub.cfg обычному пользователю не читается (Permission
    denied) — без этого отчёт обрывался на разделе 1.1 с трейсбеком.
    """
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def run_to(n: int, *args: str) -> int:
    """Выполнить команду, оставив в отчёте последние n строк вывода."""
    cmd = list(args)
    try:
        if have("timeout"):
            res = subprocess.run(
                ["timeout", "30"] + cmd,
                capture_output=True, text=True,
            )
        else:
            res = subprocess.run(cmd, capture_output=True, text=True)
        out = (res.stdout or "") + (res.stderr or "")
        lines = out.splitlines()
        tail = lines[-n:] if len(lines) > n else lines
        for line in tail:
            say(f"    {line}")
        return res.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        say(f"    (не удалось запустить: {exc})")
        return 1


def run_sh(n: int, shell_code: str) -> int:
    """Выполнить bash-строку (нужна для конвейеров)."""
    try:
        if have("timeout"):
            res = subprocess.run(
                ["timeout", "30", "bash", "-c", shell_code],
                capture_output=True, text=True,
            )
        else:
            res = subprocess.run(
                ["bash", "-c", shell_code], capture_output=True, text=True
            )
        out = (res.stdout or "") + (res.stderr or "")
        lines = out.splitlines()
        tail = lines[-n:] if len(lines) > n else lines
        for line in tail:
            say(f"    {line}")
        return res.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        say(f"    (не удалось запустить bash: {exc})")
        return 1


def run_py(n: int, *args: str) -> int:
    """Запустить python-скрипт набора, оставив в отчёте последние n строк."""
    global LAST_RC
    rc = 0
    try:
        cmd = [sys.executable] + list(args)
        if have("timeout"):
            res = subprocess.run(
                ["timeout", "120"] + cmd,
                cwd=str(KIT), capture_output=True, text=True,
            )
        else:
            res = subprocess.run(cmd, cwd=str(KIT), capture_output=True, text=True)
        out = (res.stdout or "") + (res.stderr or "")
        lines = out.splitlines()
        tail = lines[-n:] if len(lines) > n else lines
        for line in tail:
            say(f"    {line}")
        rc = res.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        say(f"    (не удалось запустить python: {exc})")
        rc = 1
    LAST_RC = rc
    say(f"    -> код возврата: {rc}")
    return rc


LAST_RC = ""

# --------------------------------------------------------------------------- #
# Поиск набора                                                                 #
# --------------------------------------------------------------------------- #
def find_kit(explicit: str | None) -> Path | None:
    """Найти папку набора (там лежит pcmcia_direct.py)."""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(SELF_DIR)
    candidates.append(Path.cwd())
    candidates.append(Path("/root/pcmcia-flash-kit"))
    # /home/*/pcmcia-flash-kit
    for home in Path("/home").iterdir() if Path("/home").is_dir() else []:
        candidates.append(home / "pcmcia-flash-kit")
    # /media/*/*/pcmcia-flash-kit, /run/media/*/*/pcmcia-flash-kit
    for base in (Path("/media"), Path("/run/media")):
        if base.is_dir():
            for level1 in base.iterdir():
                if level1.is_dir():
                    for level2 in level1.iterdir():
                        if level2.is_dir():
                            candidates.append(level2 / "pcmcia-flash-kit")
    for mnt in Path("/mnt").iterdir() if Path("/mnt").is_dir() else []:
        candidates.append(mnt / "pcmcia-flash-kit")
    candidates.append(Path("/mnt/pcmcia-flash-kit"))

    for c in candidates:
        if c.is_dir() and (c / "pcmcia_direct.py").is_file():
            return c
    return None


# --------------------------------------------------------------------------- #
# main                                                                        #
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="collect_all.py",
        description="Полный отчёт по карте PCMCIA (ноутбук).",
    )
    p.add_argument("--kit", default="", help="путь к папке набора")
    p.add_argument("--image", action="store_true",
                   help="снять образ в файл на диске ноутбука")
    p.add_argument("--img", default="", help="путь к файлу образа (по умолчанию /root/card.bin)")
    p.add_argument("--no-slow", action="store_true",
                   help="без --selftest и --diag (быстро)")
    p.add_argument("--out", default="", help="путь к файлу отчёта")
    return p


def main(argv: list[str] | None = None) -> int:
    global REPORT_PATH, LAST_RC, OUT_DEFAULT
    parser = build_parser()
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        print(f"неизвестный ключ: {unknown[0]} (см. --help)", file=sys.stderr)
        return 2

    kit = find_kit(args.kit or None)
    if kit is None:
        print("ОШИБКА: не нашёл набор (pcmcia_direct.py в корне). Укажите путь:")
        print("  python3 collect_all.py --kit /media/<флешка>/pcmcia-flash-kit")
        return 2

    image_flag = args.image
    img_path = Path(args.img) if args.img else IMAGE_DEFAULT
    slow = not args.no_slow
    out_path = Path(args.out) if args.out else None

    stamp = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    stamp_file = time.strftime("%Y%m%d-%H%M%S")

    if out_path is None:
        # пробуем рядом с набором, иначе /var/tmp
        test_file = kit / f".wtest_{stamp_file}"
        try:
            test_file.touch()
            test_file.unlink()
            out_path = kit / f"report-{stamp_file}.txt"
        except OSError:
            out_path = Path("/var/tmp") / f"report-{stamp_file}.txt"

    try:
        out_path.touch(exist_ok=True)
    except OSError:
        print(f"не могу писать в {out_path} — укажите --out /var/tmp/otchet.txt")
        return 2

    REPORT_PATH = out_path

    say(f"ОТЧЁТ ПО КАРТЕ PCMCIA — {stamp}")
    say(f"Файл отчёта: {out_path}")
    try:
        md5 = subprocess.run(
            ["md5sum", str(__file__)], capture_output=True, text=True
        ).stdout.split()[0]
        say(f"Версия сборщика отчёта (md5): {md5}")
    except (subprocess.SubprocessError, OSError, IndexError):
        say("Версия сборщика отчёта (md5): -")

    # ---------- 1. ОКРУЖЕНИЕ ----------
    sec("1. ОКРУЖЕНИЕ")
    say(f"  дата:        {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    say(f"  хост:        {os.uname().nodename}")
    say(f"  ядро:        {os.uname().sysname} {os.uname().release}")
    say(f"  архитектура: {os.uname().machine}   (нужна i386/i686 — 32-битная система)")
    say(f"  root:        {'да' if is_root() else 'НЕТ'}")
    say(f"  набор:       {kit}")
    try:
        cmdline = Path("/proc/cmdline").read_text(encoding="utf-8").strip()
        say(f"  cmdline:     {cmdline}")
    except OSError:
        say("  cmdline:     -")

    pcmcia_direct = kit / "pcmcia_direct.py"
    version_py = kit / "src" / "pcmcia_direct" / "version.py"
    kit_version = "?"
    try:
        for line in version_py.read_text(encoding="utf-8").splitlines():
            if line.startswith("KIT_VERSION"):
                # отрезать хвостовой комментарий, иначе версия уедет
                # вместе с ним (было до 2.17.16).
                kit_version = line.split("=", 1)[1].split("#", 1)[0].strip().strip('"')
                break
    except OSError:
        pass
    say(f"  версия набора: {kit_version}   (в переписке должна быть 2.18.1)")
    try:
        md5_direct = subprocess.run(
            ["md5sum", str(pcmcia_direct)], capture_output=True, text=True
        ).stdout.split()[0]
        say(f"  md5 pcmcia_direct.py: {md5_direct}")
    except (subprocess.SubprocessError, OSError, IndexError):
        say("  md5 pcmcia_direct.py: -")

    say("  Файлы набора (md5 — сверить с ответом в переписке):")
    md5_files = ["pcmcia_direct.py", "src/pcmcia_direct/cli.py",
                 "src/pcmcia_direct/flash.py", "src/pcmcia_direct/chips_data.py",
                 "pcmcia_ctl.py", "collect_all.py",
                 "run_remote.py", "remote_collect.py"]
    existing = [f for f in md5_files if (kit / f).is_file()]
    if existing:
        try:
            res = subprocess.run(
                ["md5sum"] + existing,
                cwd=str(kit), capture_output=True, text=True,
            )
            for line in res.stdout.splitlines():
                say(f"    {line}")
        except (subprocess.SubprocessError, OSError):
            pass

    say("  Чего не хватает в наборе:")
    for f in ("pcmcia_direct.py", "src/pcmcia_direct/cli.py", "pcmcia_ctl.py",
              "collect_all.py"):
        if not (kit / f).is_file():
            say(f"    НЕТ: {f}")

    # ---------- 1.1 ПЛАН Б ----------
    sec("1.1 ПЛАН Б (снятие проверки окон памяти)")
    modconf = Path("/etc/modprobe.d/pcmcia-noprobe.conf")
    grubdef = Path("/etc/default/grub")
    grubcfg = Path("/boot/grub/grub.cfg")

    cmdline_has = False
    try:
        cmdline_has = "iomem=relaxed" in Path("/proc/cmdline").read_text(encoding="utf-8")
    except OSError:
        pass

    grub_has = "iomem=relaxed" in read_text_quiet(grubdef)
    run_has = "iomem=relaxed" in read_text_quiet(grubcfg)

    say(f"  iomem=relaxed в cmdline : {'ДА (действует сейчас)' if cmdline_has else 'НЕТ'}")
    if grubdef.is_file():
        say(f"  /etc/default/grub       : {'содержит iomem=relaxed' if grub_has else 'БЕЗ iomem=relaxed'}")
    else:
        say("  /etc/default/grub       : НЕТ файла")
    if grubcfg.is_file():
        say(f"  /boot/grub/grub.cfg     : {'содержит iomem=relaxed' if run_has else 'БЕЗ iomem=relaxed'}")
    else:
        say("  /boot/grub/grub.cfg     : НЕТ файла")

    if modconf.is_file():
        say("  probe_mem=0 (файл)      : есть")
        try:
            for line in modconf.read_text(encoding="utf-8").splitlines():
                say(f"                            {line}")
        except OSError:
            pass
    else:
        say(f"  probe_mem=0 (файл)      : НЕТ ({modconf})")

    if cmdline_has:
        say("  ВЫВОД: план Б АКТИВЕН - прямой доступ к attribute memory разрешён.")
    elif run_has:
        say("  ВЫВОД: план Б записан и попал в меню загрузки - нужна ПЕРЕЗАГРУЗКА.")
    elif grub_has:
        say("  ВЫВОД: параметр записан, но меню загрузки НЕ пересобрано (update-grub).")
        say("         Перезагрузка НЕ включит iomem=relaxed. Запустите planb.sh 2.6.3+")
    else:
        say("  ВЫВОД: план Б НЕ применён (или применён старой версией набора).")
        say("         С Windows: python run_remote.py --planb --host <адрес>")

    try:
        uptime = Path("/proc/uptime").read_text(encoding="utf-8").split()[0]
        say(f"  загрузка (uptime): {uptime} с")
    except (OSError, IndexError, ValueError):
        try:
            up = subprocess.run(["uptime", "-p"], capture_output=True, text=True).stdout.strip()
            say(f"  загрузка (uptime): {up}")
        except (subprocess.SubprocessError, OSError):
            say("  загрузка (uptime): -")

    # ---------- 2. МОДУЛИ, МОСТ, СЛОТ ----------
    sec("2. МОДУЛИ, МОСТ, СЛОТ")
    say("  модули PCMCIA:")
    if have("lsmod"):
        run_sh(20, "lsmod | grep -iE 'pcmcia|yenta' || echo '(модулей pcmcia не видно)'")
    else:
        say("    (нет lsmod)")
    say("")

    say("  мост CardBus:")
    if have("lspci"):
        run_sh(10, "lspci -nn | grep -iE 'cardbus|pcmcia' || echo '(мост CardBus не найден)'")
        try:
            brg_out = subprocess.run(
                ["lspci"], capture_output=True, text=True
            ).stdout
            brg = ""
            for line in brg_out.splitlines():
                if "cardbus" in line.lower():
                    brg = line.split()[0]
                    break
            if brg:
                say(f"  окна и драйвер моста {brg}:")
                run_sh(
                    25,
                    f"lspci -vv -s {brg} 2>/dev/null | "
                    f"grep -iE 'Memory window|I/O window|Kernel driver|Region 0|Capabilities' | head -20",
                )
                res_path = Path(f"/sys/bus/pci/devices/0000:{brg}/resource")
                if res_path.is_file():
                    say("  BAR моста (из sysfs):")
                    run_to(6, "head", "-3", str(res_path))
        except (subprocess.SubprocessError, OSError):
            pass
    else:
        say("    (нет lspci — sudo apt install pciutils)")
    say("")

    say("  слот:")
    if have("pccardctl"):
        run_to(8, "pccardctl", "status")
        run_to(8, "pccardctl", "ident")
    else:
        say("    (нет pccardctl — sudo apt install pcmciautils)")
    socket_dir = Path("/sys/class/pcmcia_socket/pcmcia_socket0")
    for fname in ("card_type", "card_voltage", "card_vpp", "available_resources_setup_done"):
        f = socket_dir / fname
        if f.is_file():
            try:
                val = f.read_text(encoding="utf-8").strip()
                say(f"    {fname:<32} {val}")
            except OSError:
                pass
    say("  устройства PCMCIA (пусто = ядро карту не приняло):")
    pcmcia_dev = Path("/sys/bus/pcmcia/devices")
    if pcmcia_dev.is_dir():
        try:
            entries = os.listdir(pcmcia_dev)
            for e in entries:
                say(f"    {e}")
        except OSError:
            say("    (нет каталога)")
    else:
        say("    (нет каталога)")

    say("  /dev/mtd*:")
    try:
        mtds = subprocess.run(
            ["ls", "-l", "/dev/mtd*"], capture_output=True, text=True
        )
        for line in mtds.stdout.splitlines():
            say(f"    {line}")
        if mtds.stderr.strip():
            say(f"    (нет /dev/mtd*)")
    except (subprocess.SubprocessError, OSError):
        say("    (нет /dev/mtd*)")

    say("  окна в /proc/iomem:")
    try:
        iomem = subprocess.run(
            ["grep", "-iE", "CardBus", "/proc/iomem"],
            capture_output=True, text=True,
        )
        for line in iomem.stdout.splitlines():
            say(f"    {line}")
        if not iomem.stdout.strip():
            say("    (строк CardBus нет)")
    except (subprocess.SubprocessError, OSError):
        say("    (строк CardBus нет)")

    # ---------- 3. ЯДРО ----------
    sec("3. ЧТО ГОВОРИТ ЯДРО")
    if have("dmesg"):
        run_sh(60, "dmesg 2>/dev/null | grep -iE 'pcmcia|yenta|pccard|pcmciamtd|high memory|CIS' | tail -40 || true")
    else:
        say("    (нет dmesg — запускайте через sudo)")

    # ---------- 4-5. СЕЛФТЕСТ / ДИАГ (только в медленном режиме) ----------
    if slow:
        sec("4. САМОПРОВЕРКА НАБОРА (--selftest)")
        run_py(30, str(PCMCIA_CTL), "selftest", "--fast")

        sec("5. РАЗБОР ЯДРА СРЕДСТВАМИ НАБОРА (--diag)")
        run_py(60, str(PCMCIA_CTL), "diag")

    # ---------- 6. ЧТЕНИЕ COMMON MEMORY ----------
    sec("6. ЧТЕНИЕ COMMON MEMORY КАРТЫ (--probe, настроенное окно)")
    run_py(40, str(pcmcia_direct), "--probe")

    # ---------- 7. ПАСПОРТ ----------
    sec("7. ПАСПОРТ КАРТЫ ИЗ ATTRIBUTE MEMORY (--cis)  <-- главный раздел")
    say("  Кортежи/«производитель карты» здесь = карта жива и отвечает.")
    say("  Сплошные 0xFF/0x00 = карта (или контакт слота) не отвечает.")
    say("")
    run_py(60, str(pcmcia_direct), "--cis")

    # ---------- 7.1 СЫРЫЕ ФАКТЫ ----------
    sec("7.1 ПРОВЕРКА ATTRIBUTE MEMORY (сырые факты)")
    run_py(40, str(pcmcia_direct), "--extra")
    say("")

    cis_ok = 0
    cis_seen = 0
    cis_rc = LAST_RC
    report_text = ""
    try:
        report_text = out_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        report_text = ""

    if cis_rc == 0 and (
        "продукт (VERS_1)" in report_text or
        "производитель (MANFID)" in report_text or
        "устройство из DEVICE" in report_text
    ):
        cis_ok = 1
    if cis_rc == 4 or "Паспорт ВИДЕН" in report_text:
        cis_seen = 1

    say("")
    if cis_ok == 1:
        say(f"  ИТОГ РАЗДЕЛА 7: паспорт прочитан: ДА (код {cis_rc})")
    elif cis_seen == 1:
        say(f"  ИТОГ РАЗДЕЛА 7: паспорт прочитан: ЧАСТИЧНО (код {cis_rc})")
        say("    карта ОТВЕЧАЕТ и отдаёт данные, но цепочку кортежей собрать не удалось;")
        say("    строки паспорта - в разделе 7 выше.")
    else:
        say(f"  ИТОГ РАЗДЕЛА 7: паспорт прочитан: НЕТ (код {cis_rc})")

    # ---------- 8. СНЯТИЕ ОБРАЗА ----------
    if image_flag:
        sec("8. СНЯТИЕ ОБРАЗА КАРТЫ")
        if cis_ok != 1:
            say("  ПРОПУЩЕНО: в разделе 7 паспорт карты не прочитан.")
            say("  Снимать образ с неотвечающей карты бессмысленно — сначала раздел 7.")
        else:
            say(f"  Пишу {img_path} (карта только читается, на карту ничего не пишется)")
            say("  Размер определяет сама карта: адрес повторяется через её объём.")
            run_py(900, str(pcmcia_direct), "--read", str(img_path), "--double", "--size", "auto")
            if img_path.is_file():
                run_py(60, str(pcmcia_direct), "--image-report", str(img_path))
            else:
                say("  ОБРАЗ НЕ СОЗДАН — причина в выводе выше (код возврата строкой раньше).")
                say("  Частые причины: два чтения различаются (контакты/нестабильность),")
                say("  нет места на диске, окно моста меньше карты.")

    # ---------- КОНЕЦ ----------
    sec("КОНЕЦ ОТЧЁТА")
    try:
        line_count = len(out_path.read_text(encoding="utf-8", errors="ignore").splitlines())
        say(f"  строк: {line_count}")
    except OSError:
        say("  строк: -")
    say(f"  файл:  {out_path}")
    say("")
    say("  КАК ПРИСЛАТЬ: приложите этот файл к сообщению (или вставьте текстом).")
    if not is_root():
        say("")
        say("  ВНИМАНИЕ: запуск был БЕЗ root — разделы 6-8 недостоверны. Повторите:")
        say("    sudo python3 collect_all.py")
    return 0


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    sys.exit(main())
