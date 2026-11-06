#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""planb.py - снять мешающую проверку окон памяти, чтобы ядро accepté карту.

Две независимые настройки, обе обратимые:
  1) /etc/modprobe.d/pcmcia-noprobe.conf:  options pcmcia_rsrc probe_mem=0
     - окна памяти выдаются без проверки чтением CIS;
  2) iomem=relaxed в параметрах ядра - разрешает прямой доступ к /dev/mem,
     чтобы прочитать паспорт карты даже если ядро её не приняло.

Запуск:
    sudo python3 planb.py            применить
    sudo python3 planb.py --dry-run  только показать
    sudo python3 planb.py --revert   вернуть как было
    sudo python3 planb.py --check    ничего не менять, показать состояние
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

MODCONF = Path("/etc/modprobe.d/pcmcia-noprobe.conf")
OLDCONF = Path("/etc/default/grub.d/99-pcmcia-relaxed.cfg")
GRUBDEF = Path("/etc/default/grub")
GRUBBAK = Path("/etc/default/grub.planb.bak")
GRUBCFG = Path("/boot/grub/grub.cfg")

DRY = False
REVERT = False
CHECK = False


def say(text: str = "") -> None:
    print(text)


def run(cmd: str) -> None:
    """Выполнить команду (или показать, что было бы, в режиме --dry-run)."""
    if DRY:
        say(f"  [было бы] {cmd}")
    else:
        try:
            subprocess.run(cmd, shell=True, check=True)
        except subprocess.CalledProcessError as exc:
            say(f"  команда не удалась: {cmd} (код {exc.returncode})")


def state() -> None:
    cmdline_relaxed = False
    try:
        cmdline_relaxed = "iomem=relaxed" in Path("/proc/cmdline").read_text(
            encoding="utf-8"
        )
    except OSError:
        pass

    say(f"  iomem=relaxed в cmdline  : {'ДА' if cmdline_relaxed else 'НЕТ'}")
    say(
        f"  файл probe_mem=0         : {'есть' if MODCONF.is_file() else 'НЕТ'}  ({MODCONF})"
    )
    if GRUBDEF.is_file():
        grub_text = GRUBDEF.read_text(encoding="utf-8", errors="ignore")
        say(
            f"  /etc/default/grub        : "
            f"{'содержит iomem=relaxed' if 'iomem=relaxed' in grub_text else 'без iomem=relaxed'}"
        )
    else:
        say("  /etc/default/grub        : НЕТ (не Debian/Ubuntu? тогда этот путь не подходит)")
    if GRUBCFG.is_file():
        grub_cfg_text = GRUBCFG.read_text(encoding="utf-8", errors="ignore")
        say(
            f"  /boot/grub/grub.cfg      : "
            f"{'содержит iomem=relaxed (загрузка его получит)' if 'iomem=relaxed' in grub_cfg_text else 'БЕЗ iomem=relaxed (загрузка его НЕ получит)'}"
        )
    else:
        say("  /boot/grub/grub.cfg      : НЕТ файла")
    try:
        uptime = subprocess.run(
            ["uptime", "-p"], capture_output=True, text=True
        ).stdout.strip()
        say(f"  загрузка                 : {uptime}")
    except (subprocess.SubprocessError, OSError):
        say("  загрузка                 : ?")


def revert() -> None:
    say("=== возвращаю как было ===")
    run(f"rm -f {MODCONF}")
    run(f"rm -f {OLDCONF}")
    if GRUBBAK.is_file():
        run(f"cp -a {GRUBBAK} {GRUBDEF}")
        say(f"    /etc/default/grub восстановлен из {GRUBBAK}")
    else:
        # запасного нет - убираем параметр аккуратно через python3
        run(
            "python3 - '$GRUBDEF' <<'PY'\n"
            "import re, sys\n"
            "p = sys.argv[1]\n"
            "s = open(p, encoding='utf-8').read()\n"
            "s = re.sub(r'\\\\s*iomem=relaxed', '', s)\n"
            "open(p, 'w', encoding='utf-8').write(s)\n"
            "PY"
        )
    if shutil.which("update-initramfs"):
        run("update-initramfs -u >/dev/null 2>&1")
    if shutil.which("update-grub"):
        run("update-grub >/dev/null 2>&1")
    say("")
    say("Готово. Вступит в силу после перезагрузки.")
    say("Проверить:  sudo python3 planb.py --check")


def apply() -> None:
    say("=== план Б: снять проверку окон памяти ===")
    say("")

    say("1) параметр модуля: options pcmcia_rsrc probe_mem=0")
    if DRY:
        say(f"  [было бы] записать {MODCONF}")
    else:
        MODCONF.parent.mkdir(parents=True, exist_ok=True)
        MODCONF.write_text("options pcmcia_rsrc probe_mem=0\n", encoding="utf-8")
        say(f"    записано: {MODCONF}")

    say("")
    say(
        "2) параметр ядра iomem=relaxed -> /etc/default/grub (с резервной копией)"
    )
    if DRY:
        say(f"  [было бы] добавить iomem=relaxed в GRUB_CMDLINE_LINUX_DEFAULT ({GRUBDEF})")
        say(f"  [было бы] резервная копия: {GRUBBAK}")
    else:
        if not GRUBDEF.is_file():
            say(f"    ОШИБКА: нет {GRUBDEF} - это не Debian/Ubuntu или grub не установлен.")
            say(
                "    Дальше нужен ручной шаг: добавьте iomem=relaxed в параметры ядра вручную."
            )
            sys.exit(3)
        # резервную копию делаем ОДИН раз, чтобы повторный запуск её не затёр
        if not GRUBBAK.is_file():
            shutil.copy2(str(GRUBDEF), str(GRUBBAK))
            say(f"    резервная копия: {GRUBBAK}")
        # правка через python3: без кавычек, без sed, повторный запуск ничего не дублирует
        grub_text = GRUBDEF.read_text(encoding="utf-8")
        changed = []
        for key in ("GRUB_CMDLINE_LINUX_DEFAULT", "GRUB_CMDLINE_LINUX"):
            m = re.search(r"^" + key + r"=(.*)$", grub_text, re.M)
            if not m:
                if key.endswith("DEFAULT"):
                    line = f'{key}="quiet iomem=relaxed"'
                    grub_text = grub_text.rstrip("\n") + "\n" + line + "\n"
                    changed.append(f"добавлена строка {line}")
                continue
            val = m.group(1)
            if "iomem=relaxed" in val:
                continue
            if val[:1] in ('"', "'"):
                q = val[0]
                body = val.strip()[1:-1]
                new = f'{key}={q}{(body + " iomem=relaxed").strip()}{q}'
            else:
                new = f'{key}="{(val.strip() + " iomem=relaxed").strip()}"'
            grub_text = grub_text[: m.start()] + new + grub_text[m.end() :]
            changed.append(f"изменено: {new}")
        if not changed:
            say("    уже было - не дублирую")
        else:
            tmp = str(GRUBDEF) + ".planb.tmp"
            Path(tmp).write_text(grub_text, encoding="utf-8")
            os.replace(tmp, str(GRUBDEF))
            for c in changed:
                say(f"    {c}")
        # старый способ (grub.d) убираем, чтобы не было двух источников
        if OLDCONF.is_file():
            OLDCONF.unlink()
            say(f"    убран старый файл {OLDCONF}")

    say("")
    say("3) пересборка initramfs")
    if shutil.which("update-initramfs"):
        run("update-initramfs -u")
    else:
        say("    (update-initramfs не найден - пропускаю)")

    say("")
    say(
        "4) пересборка меню загрузки (это обязательный шаг: без него параметр не "
        "попадёт в загрузку)"
    )
    if shutil.which("update-grub"):
        run("update-grub")
    elif shutil.which("grub-mkconfig"):
        run(f"grub-mkconfig -o {GRUBCFG}")
    else:
        say("    ОШИБКА: нет ни update-grub, ни grub-mkconfig.")
        say(
            "    Меню загрузки не пересобрано, iomem=relaxed в загрузку НЕ попадёт."
        )

    say("")
    say("=== ПРОВЕРКА РЕЗУЛЬТАТА (до перезагрузки) ===")
    verdict = False
    if GRUBCFG.is_file():
        grub_cfg_text = GRUBCFG.read_text(encoding="utf-8", errors="ignore")
        if "iomem=relaxed" in grub_cfg_text:
            say("  в /boot/grub/grub.cfg: iomem=relaxed ЕСТЬ - после перезагрузки параметр будет")
            say(
                f"  в /etc/default/grub   : {'iomem=relaxed есть' if 'iomem=relaxed' in GRUBDEF.read_text(encoding='utf-8', errors='ignore') else 'НЕТ'}"
            )
            say(f"  файл probe_mem=0      : {'есть' if MODCONF.is_file() else 'НЕТ'}")
            verdict = True
        else:
            say("  в /boot/grub/grub.cfg: iomem=relaxed НЕ НАЙДЕН.")
            say("  Значит перезагрузка НЕ включит прямой доступ к /dev/mem.")
            say("  Пришлите этот вывод - разберём (возможно, грузит не grub).")
    say("")
    say("=== ЧТО ДАЛЬШЕ ===")
    if verdict:
        say("  перезагрузить ноутбук С КАРТОЙ В СЛОТЕ и снять отчёт:")
        say("    sudo reboot")
        say("    (с Windows это делает:  .\\RUN.ps1 -HostName <адрес> -PlanB -Reboot)")
    else:
        say("  НЕ перезагружайте: параметр не попал в загрузку. Пришлите вывод выше.")
    say("")
    say("  ВЕРНУТЬ КАК БЫЛО (когда закончим):  sudo python3 planb.py --revert")
    say("  ПОСМОТРЕТЬ СОСТОЯНИЕ:              sudo python3 planb.py --check")


def main(argv: list[str] | None = None) -> int:
    global DRY, REVERT, CHECK

    for a in sys.argv[1:]:
        if a == "--dry-run":
            DRY = True
        elif a == "--revert":
            REVERT = True
        elif a == "--check":
            CHECK = True
        elif a in ("-h", "--help"):
            print(
                """planb.py - снять мешающую проверку окон памяти, чтобы ядро accepté карту.

Запуск:
    sudo python3 planb.py            применить
    sudo python3 planb.py --dry-run  только показать
    sudo python3 planb.py --revert   вернуть как было
    sudo python3 planb.py --check    ничего не менять, показать состояние"""
            )
            return 0
        else:
            say(f"неизвестный ключ: {a}")
            return 2

    if CHECK or (DRY and not REVERT and len(sys.argv) > 1 and sys.argv[1] == "--check"):
        say("=== состояние плана Б ===")
        state()
        return 0

    if not DRY and os.geteuid() != 0:
        say("нужен root: sudo python3 planb.py")
        return 2

    if REVERT:
        revert()
        return 0

    apply()
    return 0


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    sys.exit(main())
