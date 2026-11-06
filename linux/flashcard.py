#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""flashcard.py — безопасная работа с linear flash картой (MTD) на AMILO Pro V2030D.

    python3 flashcard.py info                    # тип, размер, блоки
    python3 flashcard.py ident                   # паспорт карты из CIS
    python3 flashcard.py backup out.bin          # снять полный образ (с проверкой двойным чтением)
    python3 flashcard.py erase                   # стереть всю карту
    python3 flashcard.py write image.bin         # стереть + записать + верифицировать
    python3 flashcard.py verify image.bin        # только верификация
    python3 flashcard.py fill 0xFF               # залить «пустой» образ (тест записи)
    python3 flashcard.py ftl                     # разметить FTL + FAT (карта станет «диском»)
    python3 flashcard.py list                    # краткая информация
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MTD = Path(os.environ.get("MTD", "/dev/mtd0"))


def die(msg: str) -> None:
    print(f"ОШИБКА: {msg}", file=sys.stderr)
    sys.exit(1)


def need(name: str) -> None:
    from shutil import which
    if which(name) is None:
        die(f"нет утилиты {name} (apt install mtd-utils)")


def require_root() -> None:
    if os.geteuid() != 0 and os.environ.get("PCMCIA_SKIP_ROOT") != "1":
        die("нужен root: sudo python3 flashcard.py ...")


def mtd_size() -> int:
    try:
        n = str(MTD).split("/mtd")[-1]
        n = "".join(ch for ch in n if ch.isdigit())
        f = f"/sys/class/mtd/mtd{n}/size"
        if Path(f).is_file():
            return int(Path(f).read_text().strip(), 0)
    except (ValueError, OSError):
        pass
    r = subprocess.run(["blockdev", "--getsize64", str(MTD)],
                       capture_output=True, text=True)
    try:
        return int(r.stdout.strip())
    except ValueError:
        die(f"не читается {MTD}")


def human(size_bytes: int) -> str:
    try:
        r = subprocess.run(["numfmt", "--to=iec", "--suffix=B", str(size_bytes)],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return f"{size_bytes} байт"


def check_card() -> None:
    if not MTD.exists():
        die(f"нет {MTD} — карта не распознана. Запустите setup_linear_flash.py и посмотрите dmesg.")


def check_unmounted() -> None:
    mtd_name = str(MTD)
    block_name = f"/dev/mtdblock{mtd_name.split('/mtd')[-1]}"
    try:
        with open("/proc/mounts") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0] in (mtd_name, block_name):
                    die(f"карта смонтирована в {parts[1]} — сначала: umount {parts[1]}")
    except OSError:
        pass


def check_writable() -> None:
    n = str(MTD).split("/mtd")[-1]
    n = "".join(ch for ch in n if ch.isdigit())
    f = Path(f"/sys/class/mtd/mtd{n}/flags")
    if not f.is_file():
        return
    try:
        v = int(f.read_text().strip())
    except (ValueError, OSError):
        return
    if (v & 0x400) == 0:
        die(f"устройство помечено как не-writeable (flags={v}). "
            f"Проверьте переключатель WP на карте.")


def check_space(need_bytes: int, out_path: str) -> None:
    dir_p = Path(out_path).parent
    try:
        r = subprocess.run(["df", "-B1", "--output=avail", str(dir_p)],
                           capture_output=True, text=True)
        lines = r.stdout.splitlines()
        if len(lines) >= 2:
            free = int(lines[1].strip())
            if free < need_bytes * 11 // 10:
                die(f"мало места в {dir_p}: свободно {human(free)}, "
                    f"нужно {human(need_bytes * 11 // 10)}")
    except (ValueError, OSError, subprocess.SubprocessError):
        pass


def cmd_info() -> None:
    need("mtd_debug")
    check_card()
    subprocess.run(["mtd_debug", "info", str(MTD)])
    say("-----")
    subprocess.run(["pccardctl", "status"], capture_output=True)


def cmd_ident() -> None:
    r = subprocess.run(["pccardctl", "ident"], capture_output=True, text=True)
    if r.returncode != 0:
        die("pccardctl недоступен или слот пуст")
    print(r.stdout, end="")
    say("-----")
    subprocess.run(["pccardctl", "status"], capture_output=True)


def cmd_list() -> None:
    if not MTD.exists():
        die(f"нет {MTD}")
    subprocess.run(["mtd_debug", "info", str(MTD)],
                   capture_output=True) or subprocess.run(["ls", "-l", "/dev/mtd*"])


def say(text: str = "") -> None:
    print(text)


def cmd_backup(out_path: str = "") -> None:
    if not out_path:
        die("укажите имя файла: backup /root/card.bin")
    need("mtd_debug")
    require_root()
    check_card()
    sz = mtd_size()
    out_p = Path(out_path)
    if out_p.exists():
        die(f"{out_p} уже существует — не перезаписываю")
    check_space(sz * 2, out_path)
    say(f"Размер карты: {sz} ({human(sz)})")
    say(f"Чтение #1 -> {out_p}")
    if subprocess.run(["mtd_debug", "read", str(MTD), "0", str(sz), str(out_p)]).returncode != 0:
        die("чтение не удалось")
    chk = Path(str(out_p) + ".check")
    say(f"Чтение #2 (контроль) -> {chk}")
    if subprocess.run(["mtd_debug", "read", str(MTD), "0", str(sz), str(chk)]).returncode != 0:
        die("контрольное чтение не удалось")
    if out_p.read_bytes() == chk.read_bytes():
        chk.unlink()
        say("OK: два чтения совпали, образ стабилен.")
        subprocess.run(["md5sum", str(out_p)])
    else:
        say("ВНИМАНИЕ: чтения различаются — карта или контакты нестабильны!")
        say(f"Файл {chk} оставлен для сравнения. Не записывайте на эту карту, пока не разберётесь.")
        sys.exit(2)


def cmd_erase() -> None:
    need("flash_erase")
    require_root()
    check_card()
    check_unmounted()
    check_writable()
    sz = mtd_size()
    say(f"СТИРАНИЕ ВСЕЙ КАРТЫ {MTD} ({human(sz)})")
    a = input("Введите YES для подтверждения: ").strip()
    if a != "YES":
        die("отменено")
    if subprocess.run(["flash_erase", str(MTD), "0", "0"]).returncode == 0:
        say("Стёрто.")


def cmd_write(img_path: str) -> None:
    need("flashcp")
    need("flash_erase")
    require_root()
    check_card()
    check_unmounted()
    check_writable()
    img_p = Path(img_path)
    if not img_p.is_file():
        die(f"нет файла {img_p}")
    isz = img_p.stat().st_size
    csz = mtd_size()
    say(f"Образ: {human(isz)}, карта: {human(csz)}")
    if isz > csz:
        die("образ больше карты")
    if isz == csz:
        say("Размер совпадает с картой.")
    else:
        say(f"Образ меньше карты на {human(csz - isz)}.")
        say("Остаток карты будет СТЁРТ (flash_erase 0 0), чтобы там не остались старые данные.")
        say("Если это не нужно: MTD_KEEP_TAIL=1 python3 flashcard.py write " + img_path)
    a = input(f"Стереть карту и записать {img_p.name}? YES: ").strip()
    if a != "YES":
        die("отменено")
    # Ключевой момент: flashcp без -A стирает только область под образ,
    # и «хвост» карты остаётся со старыми данными. Поэтому стираем явно.
    if os.environ.get("MTD_KEEP_TAIL", "0") == "1":
        say("Остаток карты НЕ стирается (MTD_KEEP_TAIL=1)")
    else:
        say("Стирание всей карты...")
        if subprocess.run(["flash_erase", str(MTD), "0", "0"]).returncode != 0:
            die("стирание не удалось")
    if subprocess.run(["flashcp", "-v", str(img_p), str(MTD)]).returncode != 0:
        die("запись не удалась")
    # обратное чтение: сколько реально записалось
    r = subprocess.run(["mtd_debug", "read", str(MTD), "0", str(isz), "/dev/stdout"],
                       capture_output=True)
    got = len(r.stdout) if isinstance(r.stdout, bytes) else len(r.stdout.encode())
    if got < isz:
        say(f"ВНИМАНИЕ: обратное чтение дало {got} байт из {isz}")
    say("----- верификация -----")
    # запускаем через bash: на FAT32 у файла нет бита x, и "$0" не исполнится
    subprocess.run(["bash", str(Path(__file__)), "verify", img_path])


def cmd_verify(img_path: str) -> None:
    need("mtd_debug")
    require_root()
    check_card()
    img_p = Path(img_path)
    if not img_p.is_file():
        die(f"нет файла {img_p}")
    isz = img_p.stat().st_size
    tmp = Path(tempfile.mktemp())
    try:
        if subprocess.run(["mtd_debug", "read", str(MTD), "0", str(isz), str(tmp)]).returncode != 0:
            die("чтение не удалось")
        if img_p.read_bytes() == tmp.read_bytes():
            say(f"OK: карта совпадает с {img_p}")
        else:
            say("РАСХОЖДЕНИЕ! Различия:")
            r1 = subprocess.run(["cmp", "-l", str(img_p), str(tmp)],
                                capture_output=True, text=True)
            for line in r1.stdout.splitlines()[:20]:
                say(line)
            total = subprocess.run(
                ["sh", "-c", f"cmp -l {img_p} {tmp} | wc -l"],
                capture_output=True, text=True
            )
            say(f"Всего различающихся байт: {total.stdout.strip()}")
            sys.exit(2)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def cmd_fill(byte_str: str) -> None:
    need("flashcp")
    require_root()
    check_card()
    check_unmounted()
    check_writable()
    byte_val = byte_str
    if byte_val.startswith("0x") or byte_val.startswith("0X"):
        byte_val = byte_val[2:]
    byte_val = byte_val.upper()
    if byte_val not in ("FF", "00"):
        die("поддерживаются только FF или 00")
    sz = mtd_size()
    tmp = Path(tempfile.mktemp())
    try:
        print(f"Готовлю образ {sz} байт из 0x{byte_val}...", end="")
        filler = b"\xff" if byte_val == "FF" else b"\x00"
        with open(tmp, "wb") as fh:
            for _ in range(0, sz, 65536):
                fh.write(filler * min(65536, sz - fh.tell()))
        print(" ок")
        # через bash: см. комментарий в ветке write (FAT32, нет бита x)
        subprocess.run(["bash", str(Path(__file__)), "write", str(tmp)])
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def cmd_ftl() -> None:
    need("ftl_format")
    require_root()
    check_card()
    check_unmounted()
    check_writable()
    say("Разметка FTL (стирает содержимое карты!)")
    a = input("Введите YES для подтверждения: ").strip()
    if a != "YES":
        die("отменено")
    if subprocess.run(["ftl_format", "-i", str(MTD)]).returncode != 0:
        die("ftl_format не удался (карта может быть в NFTL/FFS — тогда nftl_format)")
    subprocess.run(["sync"])
    import time
    time.sleep(1)
    r = subprocess.run(["ls", "-l", "/dev/ftl*"], capture_output=True, text=True)
    if r.stdout.strip():
        print(r.stdout, end="")
    else:
        print("нет /dev/ftl* — попробуйте: modprobe ftl")
    say("Дальше: sudo mkfs.vfat /dev/ftl0 && sudo mount /dev/ftl0 /mnt")


def usage() -> None:
    say("flashcard.py — безопасная работа с linear flash картой (MTD) на AMILO Pro V2030D")
    say("")
    say("  python3 flashcard.py info             тип, размер, блоки, состояние слота")
    say("  python3 flashcard.py ident            паспорт карты из CIS (PRODID/MANFID/FUNCID)")
    say("  python3 flashcard.py backup out.bin   снять полный образ + контроль двойным чтением")
    say("  python3 flashcard.py erase            стереть всю карту (подтверждение YES)")
    say("  python3 flashcard.py write image.bin  стереть + записать + верифицировать")
    say("  python3 flashcard.py verify image.bin только верификация")
    say("  python3 flashcard.py fill 0xFF        записать «пустой» образ (тест записи)")
    say("  python3 flashcard.py ftl              разметить FTL + подсказка про mkfs.vfat")
    say("  python3 flashcard.py list             краткая информация (если вы в группе flash)")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else "help"
    if cmd == "info":
        cmd_info()
    elif cmd == "ident":
        cmd_ident()
    elif cmd == "list":
        cmd_list()
    elif cmd == "backup":
        cmd_backup(args[1] if len(args) > 1 else "")
    elif cmd == "erase":
        cmd_erase()
    elif cmd == "write":
        cmd_write(args[1] if len(args) > 1 else "")
    elif cmd == "verify":
        cmd_verify(args[1] if len(args) > 1 else "")
    elif cmd == "fill":
        cmd_fill(args[1] if len(args) > 1 else "0xFF")
    elif cmd == "ftl":
        cmd_ftl()
    else:
        usage()
    return 0


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    sys.exit(main(sys.argv[1:]))
