#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""card_dump_linux.py — снять сырой дамп PCMCIA linear flash карты.

Запускать в Linux (live-флешка) на машине, где карта видна как /dev/mtd0.
НИЧЕГО не стирает и не пишет на карту — только читает.

Использование:
    python3 card_dump_linux.py [куда_сохранить]
Пример:
    python3 card_dump_linux.py /media/usb/card32.bin
"""

import os
import subprocess
import sys
import time
from pathlib import Path

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
    f"./card_dump_{time.strftime('%Y%m%d_%H%M')}.bin")
MTD = Path(os.environ.get("MTD", "/dev/mtd0"))


def main() -> int:
    if os.geteuid() != 0:
        print("Нужен root: sudo python3 card_dump_linux.py", file=sys.stderr)
        return 1

    if not MTD.exists():
        print(f"Нет {MTD} — карта не распознана.")
        print("Проверьте: ls /dev/mtd* ; pccardctl status ; pccardctl ident ; dmesg | tail -30")
        print("Если карта вообще не видна в Linux — дамп снять не получится,")
        print("Windows тут не помощник (там нет драйвера 16-битных карт памяти).")
        return 2

    if subprocess.run(["sh", "-c", "command -v mtd_debug"],
                      capture_output=True).returncode != 0:
        print("Нет mtd_debug. Поставьте: apt install mtd-utils", file=sys.stderr)
        return 3

    # Размер карты
    r = subprocess.run(["blockdev", "--getsize64", str(MTD)],
                       capture_output=True, text=True)
    try:
        sz = int(r.stdout.strip())
    except ValueError:
        sz = 0
    if sz <= 0:
        print("Не читается размер карты", file=sys.stderr)
        return 4

    print(f"Карта: {MTD}, размер: {sz} байт")

    r = subprocess.run(["mtd_debug", "info", str(MTD)], capture_output=True, text=True)
    print(r.stdout, end="")
    if r.stderr:
        print(r.stderr, end="", file=sys.stderr)

    if OUT.exists():
        print(f"{OUT} уже существует — не перезаписываю.", file=sys.stderr)
        return 5

    print(f"Чтение #1 -> {OUT}")
    r = subprocess.run(["mtd_debug", "read", str(MTD), "0", str(sz), str(OUT)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout, end="")
        print(r.stderr, end="", file=sys.stderr)
        return 6
    print(f"Контрольное чтение #2 -> {OUT.chk}")
    r = subprocess.run(["mtd_debug", "read", str(MTD), "0", str(sz), str(OUT) + ".chk"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout, end="")
        print(r.stderr, end="", file=sys.stderr)
        return 7

    if OUT.read_bytes() == (Path(str(OUT) + ".chk")).read_bytes():
        (Path(str(OUT) + ".chk")).unlink()
        print("OK: чтения совпали, образ стабилен.")
        r = subprocess.run(["md5sum", str(OUT)], capture_output=True, text=True)
        print(r.stdout.strip())
        open(str(OUT) + ".md5", "w").write(r.stdout)
    else:
        print("ВНИМАНИЕ: чтения различаются — карта/контакты нестабильны.")
        print(f"Файл {OUT.chk} оставлен для сравнения. Карту лучше не использовать для записи.")
        return 8

    print()
    print("Готово. Копию образа держите минимум в двух местах.")
    return 0


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    sys.exit(main())
