#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_cpio.py — собрать «мини-initramfs» (cpio.gz) только из этого набора.

Удобно, когда не хочется распаковывать образ флешки на Linux-машине:
    mkdir /mnt/usb && mount /dev/sdb1 /mnt/usb      # флешка с установщиком
    cp pcmcia-flash-kit.cpio.gz /mnt/usb/

При загрузке Debian-инсталлятора в шелле: см. README_FIRST.txt, раздел «initrd».

Запуск: python3 make_cpio.py      -> на выходе pcmcia-flash-kit.cpio.gz
"""

import gzip
import os
import subprocess
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "pcmcia-flash-kit.cpio.gz"


def main() -> int:
    os.chdir(Path(__file__).resolve().parent)

    if subprocess.run(["sh", "-c", "command -v cpio"], capture_output=True).returncode != 0:
        print("Нет cpio, пробую поставить...")
        r = subprocess.run("apt-get install -y cpio", shell=True,
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("Не удалось поставить cpio. Установите вручную: sudo apt install cpio")
            print("Либо просто скопируйте папку на флешку — cpio нужен только для initrd-варианта.")
            return 1

    here = Path(".")
    files = sorted(p for p in here.rglob("*")
                   if p.is_file() and p.name != "make_cpio.py")

    # Собираем cpio в памяти, затем gzip
    import io
    cpio_buf = io.BytesIO()
    for p in files:
        rel = p.relative_to(here).as_posix()
        data = p.read_bytes()
        # cpio newc header: 110 байт
        name = rel.encode()
        name_len = len(name) + 1  # с завершающим нулём
        # выравнивание имени до 4-байтной границе
        padded_name_len = (name_len + 3) & ~3
        # в stat-поле newc: ino=0, mode=0o100644, uid=0, gid=0, nlink=1, mtime=0, size, devmajor=0, devminor=0, rdevmajor=0, rdevminor=0, namesize, chksum
        mode = 0o100644
        size = len(data)
        # newc формат: все поля 16-ричные, фиксированная ширина
        hdr = (
            f"{0:06o}"        # magic "070701" handled below
            f"{0:06o}"        # ino
            f"{mode:06o}"     # mode
            f"{0:06o}"        # uid
            f"{0:06o}"        # gid
            f"{1:06o}"        # nlink
            f"{0:011o}"       # mtime
            f"{size:011o}"    # size
            f"{0:06o}"        # devmajor
            f"{0:06o}"        # devminor
            f"{0:06o}"        # rdevmajor
            f"{0:06o}"        # rdevminor
            f"{padded_name_len:06o}"  # namesize
            f"{0:06o}"        # chksum (посчитаем ниже)
        )
        magic = b"070701"
        hdr_bytes = magic + hdr[6:].encode()  # заменили первые 6 байт на магию
        # чексумма: сумма всех байт заголовка, где поле chksum = пробелы
        hdr_for_sum = hdr_bytes[:128] + b"                " + hdr_bytes[136:]
        checksum = sum(hdr_for_sum) & 0xFFFF
        hdr_bytes = hdr_bytes[:136] + f"{checksum:06o}".encode() + hdr_bytes[142:]
        cpio_buf.write(hdr_bytes)
        cpio_buf.write(name + b"\x00")
        # выравнивание данных
        pad = (-len(name) - 1) & 3
        cpio_buf.write(b"\x00" * pad)
        cpio_buf.write(data)
        pad = (-size) & 3
        cpio_buf.write(b"\x00" * pad)
    # TRAILER!!!
    trailer = b"TRAILER!!!" + b"\x00"
    trailer_len = len(trailer)
    padded_trailer_len = (trailer_len + 3) & ~3
    hdr = (
        f"{0:06o}" * 11 +
        f"{padded_trailer_len:06o}" +
        f"{0:06o}"
    )
    hdr_bytes = magic + hdr[6:].encode()
    hdr_for_sum = hdr_bytes[:128] + b"                " + hdr_bytes[136:]
    checksum = sum(hdr_for_sum) & 0xFFFF
    hdr_bytes = hdr_bytes[:136] + f"{checksum:06o}".encode() + hdr_bytes[142:]
    cpio_buf.write(hdr_bytes)
    cpio_buf.write(trailer)
    pad = (-trailer_len) & 3
    cpio_buf.write(b"\x00" * pad)
    # end of archive: два нулевых блока по 512 байт
    cpio_buf.write(b"\x00" * 1024)

    with gzip.open(str(OUT), "wb", compresslevel=9) as gz:
        gz.write(cpio_buf.getvalue())

    size = OUT.stat().st_size
    print(f"Готово: {OUT} ({size} байт)")
    print("Содержимое (первые 40):")
    with gzip.open(str(OUT), "rb") as gz:
        data = gz.read()
    # просто покажем имена файлов из cpio
    import cpio  # может не быть — fallback
    del cpio
    print(f"  (всего файлов: {len(files)})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
