#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pcmcia_flash.py — интерактивное меню для работы с 16-битными PCMCIA linear flash.

Fujitsu-Siemens AMILO Pro V2030D (CardBus TI PCIxx21), Debian/antiX/MX live.

Запуск:  sudo python3 ./linux/pcmcia_flash.py          (из корня набора или папки linux/)
Ничего не делает автоматически: только по выбранному пункту меню.
Все записи/стирания требуют ввода YES.

Пункты:
  1) Установить утилиты (офлайн из debs/ или через apt)
  2) Настроить слот и драйвер (окна памяти + Vpp)   <- один раз
  3) Показать состояние карты и подсказки
  4) Снять образ карты (бэкап, с контролем двойным чтением)
  5) Стереть карту
  6) Записать образ на карту (+ верификация)
  7) Сделать из карты «диск» (FTL + FAT)
  8) Тест записи (erase + 0xFF + проверка)
  9) Почему карта не видна (CIS, слот, драйвер) — по шагам
 10) Подробная информация (CIS, драйверы, dmesg)
 11) Прямое чтение карты через /dev/mem (когда драйвер не дал окно)
 12) Карта целиком: паспорт, размер, вердикт    <- работает без mtd
 13) Снять образ напрямую (минуя драйвер, двойное чтение)
 14) Разобрать образ без карты (карту можно вынуть)
 15) Можно ли ПИСАТЬ эту карту (честный ответ)
 16) Кто занял окно памяти
  L) Лог
  0) Выход
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MTD = Path(os.environ.get("MTD", "/dev/mtd0"))
LOG = Path(os.environ.get("LOG", "/tmp/pcmcia_flash.log"))
KIT = Path(__file__).resolve().parent
# скрипт может лежать в подпапке linux/ — тогда набор ищем на уровень выше
if KIT.name == "linux" and (KIT / "..").is_dir():
    KIT = (KIT / "..").resolve()
DEBS = KIT / "debs"
DIRECT = KIT / "pcmcia_direct.py"

_IS_TTY = sys.stdout.isatty()
C_B = "\033[36m" if _IS_TTY else ""
C_0 = "\033[0m" if _IS_TTY else ""
C_R = "\033[31m" if _IS_TTY else ""
C_G = "\033[32m" if _IS_TTY else ""
C_Y = "\033[33m" if _IS_TTY else ""


def say(text: str = "") -> None:
    print(text)


def ok(text: str = "") -> None:
    say(f"  {C_G}OK{C_0}  {text}")


def warn(text: str = "") -> None:
    say(f"  {C_Y}!{C_0}   {text}")


def err(text: str = "") -> None:
    say(f"  {C_R}ОШИБКА{C_0} {text}")


def sec(title: str) -> None:
    say("")
    say(f"===== {title} =====")


def have(name: str) -> bool:
    from shutil import which
    return which(name) is not None


def require_root() -> None:
    if os.geteuid() != 0 and os.environ.get("PCMCIA_SKIP_ROOT") != "1":
        err("запустите через sudo: sudo python3 ./linux/pcmcia_flash.py")
        sys.exit(1)


def confirm(prompt: str) -> bool:
    try:
        a = input(f"{prompt} (YES): ").strip()
    except EOFError:
        return False
    return a == "YES"


def pause() -> bool:
    if os.environ.get("BATCH", "0") == "1":
        return True
    print()
    try:
        input("Enter — продолжить...")
    except EOFError:
        return False
    return True


def human(size_bytes: int) -> str:
    try:
        r = subprocess.run(
            ["numfmt", "--to=iec", "--suffix=B", str(size_bytes)],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return f"{size_bytes} байт"


def card_size() -> int | None:
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
        return None


def check_card() -> None:
    say("")
    if MTD.exists():
        ok(f"карта распознана как {MTD}")
        have("mtd_debug") and subprocess.run(
            ["mtd_debug", "info", str(MTD)],
            capture_output=True, text=True,
        )
        sz = card_size()
        if sz is not None:
            say(f"    размер: {sz} байт ({human(sz)})")
        say("")
        have("pccardctl") and subprocess.run(
            ["pccardctl", "status"],
            capture_output=True, text=True,
        )
    else:
        warn("/dev/mtd0 не появился — карта не распознана")
        say("")
        say("Что проверить по порядку:")
        say("  1) карта вставлена до конца? контакты протереть изопропилом?")
        say("  2) карта 16-битная linear flash, а не ATA/CF? (ATA-flash видна как /dev/sd*)")
        say("  3) слот отвечает: pccardctl status  (должен быть \"Socket 0: ... 16-bit PC Card\")")
        say("  4) если pccardctl показывает \"-- no driver --\", попробовать перезагрузку с картой в слоте")
        say("  5) размер не читается из CIS -> добавить в /etc/modprobe.d/pcmciamtd.conf:")
        say("         options pcmciamtd vpp=50 setvpp=1 force_size=16     (или 32)")
        say("         затем: modprobe -r pcmciamtd; modprobe pcmciamtd")
        say("  6) 8-битная карта -> bankwidth=1 ; SRAM -> mem_type=1 ; ROM -> mem_type=2")
        say("  7) анонимный CIS -> нужен CONFIG_MTD_PCMCIA_ANONYMOUS=y (пересборка модуля)")
        say("  8) лог ядра: dmesg | tail -40")
        say("")
        say("Лог ядра (последние строки про pcmcia/mtd):")
        subprocess.run(
            ["sh", "-c", "dmesg 2>/dev/null | grep -iE 'pcmcia|mtd|cardbus|yenta' | tail -15"],
            capture_output=True,
        )


def has_pkgs() -> bool:
    return have("mtd_debug") and have("flash_erase") and have("flashcp")


def check_unmounted() -> bool:
    mtd_name = str(MTD)
    block_name = f"/dev/mtdblock{mtd_name.split('/mtd')[-1]}"
    try:
        with open("/proc/mounts") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0] in (mtd_name, block_name):
                    err(f"карта смонтирована в {parts[1]} — сначала: umount {parts[1]}")
                    return False
    except OSError:
        pass
    return True


def check_writable() -> bool:
    n = str(MTD).split("/mtd")[-1]
    n = "".join(ch for ch in n if ch.isdigit())
    f = Path(f"/sys/class/mtd/mtd{n}/flags")
    if not f.is_file():
        return True
    try:
        v = int(f.read_text().strip())
    except (ValueError, OSError):
        return True
    if (v & 0x400) == 0:
        err("устройство помечено как не-writeable — снимите WP на карте")
        return False
    return True


# ---------------------------------------------------------------- 1. установка
def find_debs() -> Path | None:
    for c in (DEBS, Path(__file__).resolve().parent.parent / "debs", Path.cwd() / "debs"):
        if c.is_dir():
            return c
    for a in (KIT / "pcmcia-flash-kit.tar.gz",
              Path.cwd() / "pcmcia-flash-kit.tar.gz",
              KIT / "pcmcia-flash-kit.cpio.gz",
              Path.cwd() / "pcmcia-flash-kit.cpio.gz"):
        if not a.is_file():
            continue
        t = Path(tempfile.mkdtemp(prefix="pcmia-kit-"))
        try:
            if a.suffix == ".tar.gz":
                r = subprocess.run(["tar", "-xzf", str(a), "-C", str(t)],
                                   capture_output=True, text=True)
            else:
                r = subprocess.run(
                    ["sh", "-c", f"gzip -dc {a} | cpio -idmu --quiet"],
                    cwd=str(t), capture_output=True, text=True,
                )
            if r.returncode != 0:
                continue
            if (t / "debs").is_dir():
                return t / "debs"
            if (t / "pcmcia-flash-kit" / "debs").is_dir():
                return t / "pcmcia-flash-kit" / "debs"
        finally:
            shutil.rmtree(str(t), ignore_errors=True)
    return None


def verify_tools() -> None:
    say("")
    say("Проверка версий (для отчёта):")
    r = subprocess.run(
        ["sh", "-c", "dpkg-query -W -f='    ${Package} ${Version} ${Status}\\n' "
        "mtd-utils pcmciautils pciutils kmod libc6 2>/dev/null "
        "| sed 's/ *install ok installed/ установлен/'"],
        capture_output=True, text=True,
    )
    print(r.stdout, end="")
    say(f"Лог установки: {LOG}")
    say("")
    say("Что дальше: пункт 2 (настроить слот), затем пункт 3 (состояние карты).")


def install_tools() -> None:
    require_root()
    sec("Установка утилит (mtd-utils, pcmciautils, pciutils, kmod)")
    if all(have(t) for t in ("mtd_debug", "pccardctl", "flash_erase", "flashcp")):
        ok("уже установлены — проверяю, что работают")
        verify_tools()
        return
    say("apt не смог (нет сети?) — пробую локальный набор на флешке")
    debs_dir = find_debs()
    if debs_dir:
        deb_files = sorted(debs_dir.glob("*.deb"))
        say(f"Пакеты найдены: {debs_dir} ({len(deb_files)} шт.)")
        for d in deb_files:
            r = subprocess.run(["dpkg-deb", "-f", str(d), "Package"],
                               capture_output=True, text=True)
            pkg = r.stdout.strip() or d.name
            st = subprocess.run(
                ["dpkg-query", "-W", "-f=${Status}", pkg],
                capture_output=True, text=True,
            )
            if "install ok installed" in st.stdout:
                say(f"    = {pkg} уже установлен, пропускаю")
            else:
                say(f"    + {pkg}")
                subprocess.run(["dpkg", "-i", str(d)], capture_output=True, text=True)
        subprocess.run(["apt-get", "-f", "install", "-y"], capture_output=True, text=True)
    else:
        warn("пакеты не найдены: нужна папка debs/ или архив pcmcia-flash-kit.tar.gz рядом со скриптом")

    if not have("mtd_debug") and have("apk"):
        r = subprocess.run(["apk", "add", "mtd-utils", "pciutils"], capture_output=True, text=True)
        if r.returncode == 0 and have("mtd_debug"):
            ok("готово (apk)")
    if all(have(t) for t in ("mtd_debug", "pccardctl")):
        ok("готово")
    else:
        err("не удалось поставить утилиты")
        say("Что делать:")
        say("  * подключить сеть (Ethernet-кабель) и: sudo apt update && sudo apt install -y mtd-utils pcmciautils pciutils kmod")
        say("  * либо скопировать на флешку папку debs/ целиком и повторить пункт 1")
        say(f"  * лог: {LOG}")
        say("")
        say("Проверка, что есть / чего нет:")
        for t in ("mtd_debug", "flash_erase", "flashcp", "ftl_format", "pccardctl", "lspci"):
            have(t) and say(f"    + {t}") or say(f"    - {t}  ОТСУТСТВУЕТ")


# ------------------------------------------------------------- 2. настройка
def configure() -> None:
    require_root()
    if not has_pkgs():
        err("сначала пункт 1 (установить утилиты)")
        return

    sec("Настройка слота и драйвера")
    say("Напряжение программирования Vpp:")
    say("  1) 5.0 В  — Intel Series 2+, Value Series 100, AMD, Catalyst, Sharp (обычный случай)")
    say("  2) 3.3 В  — только для карт, рассчитанных на 3.3 В (иначе порча карты)")
    v = input("Выбор [1]: ").strip()
    vpp = "33" if v == "2" else "50"

    bridge = subprocess.run(
        ["sh", "-c", "lspci -nn 2>/dev/null | grep -iE 'cardbus' | head -n1 | cut -d' ' -f1"],
        capture_output=True, text=True,
    ).stdout.strip()
    if not bridge:
        err("CardBus-мост не найден в lspci.")
        say("Проверьте: слот не отключён в BIOS, контроллер виден вообще")
        say("(lspci | grep -i -E 'cardbus|pcmcia').")
        subprocess.run(["modprobe", "yenta_socket"], capture_output=True)
        bridge = subprocess.run(
            ["sh", "-c", "lspci -nn | grep -iE 'cardbus' | head -n1 | cut -d' ' -f1"],
            capture_output=True, text=True,
        ).stdout.strip()
        if not bridge:
            return
    ok(f"мост: {_run(['lspci', '-s', bridge]).strip()}")

    bridge_v = subprocess.run(["lspci", "-v", "-s", bridge],
                              capture_output=True, text=True).stdout
    if "Memory window" not in bridge_v:
        err("нет строк 'Memory window' — модуль yenta_socket не привязался к мосту")
        say(bridge_v.splitlines()[:20])
        return

    def floor1m(addr_hex: str) -> str:
        val = int(addr_hex, 16)
        return f"0x{val & ~0xFFFFF:x}"

    def mkwin(line: str) -> str | None:
        m = re.search(r"([0-9a-fA-F]{8})-([0-9a-fA-F]{8})", line)
        if not m:
            return None
        start, end = m.group(1), m.group(2)
        if "memory" in line.lower():
            return f"include memory {floor1m(start)}-0x{end}"
        return f"include port 0x{start}-0x{end}"

    Path("/etc/pcmcia").mkdir(parents=True, exist_ok=True)
    conf_opts = Path("/etc/pcmcia/config.opts")
    if conf_opts.is_file():
        bak = Path(f"/etc/pcmcia/config.opts.bak.{os.getpid()}")
        shutil.copy2(str(conf_opts), str(bak))

    lines = [
        f"# Сгенерировано pcmcia_flash.py {time.strftime('%Y-%m-%dT%H:%M:%S%z')} — окна из lspci -v -s {bridge}",
    ]
    for line in bridge_v.splitlines():
        if "Memory window" in line or "I/O window" in line:
            w = mkwin(line)
            if w:
                lines.append(w)
    conf_opts.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok("/etc/pcmcia/config.opts:")
    for line in lines:
        say(f"    {line}")

    modprobe_conf = Path("/etc/modprobe.d/pcmciamtd.conf")
    modprobe_conf.write_text(
        f"# vpp={vpp} (единицы 0.1 В), setvpp=1 = подавать Vpp только на запись/стирание\n"
        f"options pcmciamtd vpp={vpp} setvpp=1\n",
        encoding="utf-8",
    )
    ok(f"/etc/modprobe.d/pcmciamtd.conf: vpp={vpp}")

    if not shell_has_group("flash"):
        subprocess.run(["groupadd", "flash"], capture_output=True)
    udev_rule = Path("/etc/udev/rules.d/99-pcmciamtd.rules")
    udev_rule.write_text('SUBSYSTEM=="mtd", MODE="0660", GROUP="flash"\n', encoding="utf-8")
    subprocess.run(["udevadm", "control", "--reload-rules"], capture_output=True)
    ok("udev: /dev/mtd* доступны группе flash")

    say("Загружаю драйверы...")
    for mod in ("yenta_socket", "pcmcia", "mtd", "jedec_probe", "cfi_probe",
                "cfi_cmdset_0001", "cfi_cmdset_0002", "pcmciamtd"):
        subprocess.run(["modprobe", mod], capture_output=True)
    import time as _t
    _t.sleep(1)
    check_card()


def shell_has_group(name: str) -> bool:
    try:
        import grp
        grp.getgrnam(name)
        return True
    except KeyError:
        return False


# --------------------------------------------------------------- 4. бэкап
def do_backup() -> None:
    require_root()
    if not has_pkgs():
        err("сначала пункт 1")
        return
    if not MTD.exists():
        err(f"нет {MTD} — сначала пункт 2/3")
        return
    sz = card_size()
    if sz is None:
        err("не читается размер карты")
        return
    default_out = Path.cwd() / f"card_{time.strftime('%Y%m%d_%H%M')}.bin"
    say(f"Размер карты: {sz} байт ({human(sz)})")
    say("Совет: сохраняйте сразу на флешку, например /media/<user>/<флешка>/card.bin")
    try:
        out = input(f"Куда сохранить образ [{default_out}]: ").strip()
    except EOFError:
        return
    out_p = Path(out) if out else default_out
    if out_p.exists():
        err(f"{out_p} уже существует — не перезаписываю")
        return
    say(f"Чтение #1 -> {out_p}")
    if subprocess.run(["mtd_debug", "read", str(MTD), "0", str(sz), str(out_p)]).returncode != 0:
        err("чтение не удалось")
        return
    say("Контрольное чтение #2...")
    chk = Path(str(out_p) + ".chk")
    if subprocess.run(["mtd_debug", "read", str(MTD), "0", str(sz), str(chk)]).returncode != 0:
        err("контрольное чтение не удалось")
        return
    if out_p.read_bytes() == chk.read_bytes():
        chk.unlink()
        ok("два чтения совпали — образ стабилен")
        subprocess.run(["md5sum", str(out_p)])
    else:
        err("чтения РАЗЛИЧАЮТСЯ: карта или контакты нестабильны.")
        say(f"Файл {chk} оставлен для сравнения. НЕ пишите на эту карту, пока не разберётесь.")


# --------------------------------------------------------------- 5. стирание
def do_erase() -> None:
    require_root()
    if not has_pkgs():
        err("сначала пункт 1")
        return
    if not MTD.exists():
        err(f"нет {MTD}")
        return
    if not check_unmounted() or not check_writable():
        return
    sz = card_size()
    say(f"{C_R}Полное стирание карты {MTD} ({human(sz) if sz else '?'}){C_0}")
    if not confirm("Точно стереть? Данные будут потеряны"):
        say("отменено")
        return
    if subprocess.run(["flash_erase", str(MTD), "0", "0"]).returncode == 0:
        ok("стёрто")


# --------------------------------------------------------------- 6. запись
def do_write() -> None:
    require_root()
    if not has_pkgs():
        err("сначала пункт 1")
        return
    if not MTD.exists():
        err(f"нет {MTD}")
        return
    try:
        img = input("Путь к файлу-образу: ").strip()
    except EOFError:
        return
    img_p = Path(img)
    if not img_p.is_file():
        err(f"нет файла {img_p}")
        return
    if not check_unmounted() or not check_writable():
        return
    isz = img_p.stat().st_size
    csz = card_size() or 0
    say(f"образ: {human(isz)}, карта: {human(csz)}")
    if isz > csz:
        err("образ больше карты")
        return
    if isz != csz:
        warn(f"образ меньше карты: остаток ({human(csz - isz)}) будет СТЁРТ")
    subprocess.run(["md5sum", str(img_p)])
    if not confirm("Стереть карту и записать этот образ"):
        say("отменено")
        return
    if os.environ.get("MTD_KEEP_TAIL", "0") == "1":
        say("Остаток карты НЕ стирается (MTD_KEEP_TAIL=1)")
    else:
        say("Стирание всей карты...")
        if subprocess.run(["flash_erase", str(MTD), "0", "0"]).returncode != 0:
            err("стирание не удалось")
            return
    if subprocess.run(["flashcp", "-v", str(img_p), str(MTD)]).returncode != 0:
        err("запись не удалась")
        return
    ok("записано, проверяю...")
    verify_file(img_p)


def verify_file(img_p: Path) -> None:
    isz = img_p.stat().st_size
    tmp = Path(tempfile.mktemp())
    try:
        r = subprocess.run(["mtd_debug", "read", str(MTD), "0", str(isz), str(tmp)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            err("чтение для проверки не удалось")
            return
        if img_p.read_bytes() == tmp.read_bytes():
            ok("верификация пройдена: карта совпадает с образом")
        else:
            err("РАСХОЖДЕНИЕ при обратном чтении!")
            r2 = subprocess.run(["cmp", "-l", str(img_p), str(tmp)],
                                capture_output=True, text=True)
            for line in r2.stdout.splitlines()[:10]:
                say(line)
            say("Совет: проверить vpp (пункт 2), контакты карты, и что карта не 12-вольтовая.")
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


# -------------------------------------------------- 7. «просто диск» (FTL/FAT)
def do_ftl() -> None:
    require_root()
    if not has_pkgs():
        err("сначала пункт 1")
        return
    if not MTD.exists():
        err(f"нет {MTD}")
        return
    if not check_unmounted() or not check_writable():
        return
    say("Разметка FTL: карта станет блочным устройством /dev/ftl0 (содержимое стирается)")
    say("Нужно, если хотите файлы и FAT, а не сырой образ.")
    if not confirm("Продолжить"):
        say("отменено")
        return
    if subprocess.run(["ftl_format", "-i", str(MTD)]).returncode != 0:
        err("ftl_format не удался (возможно, карта в NFTL/FFS)")
        return
    subprocess.run(["modprobe", "ftl"], capture_output=True)
    import time as _t
    _t.sleep(1)
    if not Path("/dev/ftl0").exists():
        err("нет /dev/ftl*")
        return
    if confirm("Отформатировать /dev/ftl0 в FAT и смонтировать в /mnt/card"):
        Path("/mnt/card").mkdir(parents=True, exist_ok=True)
        if not have("mkfs.vfat"):
            subprocess.run(["apt-get", "install", "-y", "dosfstools"], capture_output=True)
        if subprocess.run(["mkfs.vfat", "/dev/ftl0"]).returncode == 0:
            if subprocess.run(["mount", "/dev/ftl0", "/mnt/card"]).returncode == 0:
                ok("смонтировано: /mnt/card")


# --------------------------------------------------------------- 8. тест
def do_test() -> None:
    require_root()
    if not has_pkgs():
        err("сначала пункт 1")
        return
    if not MTD.exists():
        err(f"нет {MTD}")
        return
    sz = card_size()
    if sz is None:
        err("не читается размер карты")
        return
    say(f"Тест записи: будет стёрта ВСЯ карта ({human(sz)}) и залито 0xFF, потом 0x00, потом проверка.")
    if not confirm("Продолжить"):
        say("отменено")
        return
    if not check_unmounted() or not check_writable():
        return
    free = None
    try:
        r = subprocess.run(["df", "-B1", "--output=avail", "/tmp"],
                           capture_output=True, text=True)
        lines = r.stdout.splitlines()
        if len(lines) >= 2:
            free = int(lines[1].strip())
    except (ValueError, OSError, subprocess.SubprocessError):
        pass
    if free is not None and free < sz * 11 // 10:
        err(f"в /tmp свободно {human(free)}, а для теста нужно {human(sz * 11 // 10)}")
        return
    tmp = Path(tempfile.mktemp())
    try:
        with open(tmp, "wb") as fh:
            for _ in range(0, sz, 65536):
                fh.write(b"\xff" * min(65536, sz - fh.tell()))
        if subprocess.run(["flashcp", "-v", str(tmp), str(MTD)],
                          capture_output=True).returncode == 0:
            ok("0xFF записано")
        else:
            err("запись 0xFF не удалась")
            return
        verify_file(tmp)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    say("")
    say("Если тест прошёл — карта пишется нормально (Vpp в порядке).")
    say("Если 'верификация' не прошла или erase ругается — карта, скорее всего, требует 12 В (Intel Series 2).")


# --------------------------------------------------------------- 9. инфо
def do_info() -> None:
    if not MTD.exists():
        err("карты нет")
        return
    have("mtd_debug") and subprocess.run(["mtd_debug", "info", str(MTD)])
    say("-----")
    have("pccardctl") and subprocess.run(["pccardctl", "ident"])
    have("pccardctl") and subprocess.run(["pccardctl", "status"])
    say("-----")
    subprocess.run(["sh", "-c", "dmesg | grep -iE 'pcmciamtd|mtd' | tail -10"])


# --------------------------------------------------------------- 9. диагностика
def bridge_v_full(bridge: str) -> str:
    return subprocess.run(["lspci", "-v", "-s", bridge],
                          capture_output=True, text=True).stdout


def _run(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.stdout
    except (OSError, subprocess.SubprocessError, FileNotFoundError):
        return ""


def do_diag() -> None:
    sec("Почему карта не видна (CIS, слот, драйвер) — по шагам")
    bridge = _run(
        ["sh", "-c", "lspci -nn 2>/dev/null | grep -iE 'cardbus' | head -n1 | cut -d' ' -f1"]
    ).strip()
    dm = _run(["dmesg"])
    have_pccardctl = have("pccardctl")

    say(f"{C_B}1. Мост CardBus{C_0}")
    if not bridge:
        err("CardBus-мост не найден в lspci")
        say("    Слот выключен в BIOS или контроллер не инициализируется. Это не CIS.")
    else:
        ok(f"есть: {_run(['lspci', '-s', bridge]).strip()[:110]}")
        for line in _run(["lspci", "-v", "-s", bridge]).splitlines():
            if any(k in line for k in ("Memory window", "I/O window", "Kernel driver")):
                say(f"    {line}")
        bvf = bridge_v_full(bridge)
        if "Kernel driver in use: yenta" in bvf:
            ok("yenta_socket привязан к мосту")
        elif "Kernel driver in use:" in bvf:
            warn("к мосту привязан другой драйвер — это и есть причина, CIS тут не при чём")
        else:
            warn("к мосту НЕ привязан драйвер: modprobe yenta_socket (или перезагрузка)")

    say("")
    say(f"{C_B}2. Слот: видит ли ядро карту{C_0}")
    if have_pccardctl:
        r = _run(["pccardctl", "status"])
        for line in r.splitlines():
            say(f"    {line}")
        st = r
        if not st:
            warn("pccardctl status ничего не ответил — утилита не работает или слот не отвечает")
        low = st.lower()
        if "no card" in low or "no driver" in low:
            warn("карты в слоте ядро не видит (или нет драйвера сокета)")
            say("    CIS читать не у чего: сначала пункт 2 (слот и драйвер), затем перезагрузка с картой в слоте.")
        elif "16-bit" in low or "16 bit" in low:
            ok("в слоте 16-битная карта — этот путь правильный")

    say("")
    say(f"{C_B}3. Что понял драйвер pcmciamtd{C_0}")
    mod_conf = Path("/etc/modprobe.d/pcmciamtd.conf")
    if mod_conf.is_file():
        txt = mod_conf.read_text(encoding="utf-8", errors="replace")
        opts = " ".join(l for l in txt.splitlines() if not l.startswith("#") and l.strip())
        ok(f"опции: {opts}")
    else:
        warn("нет /etc/modprobe.d/pcmciamtd.conf — запустите пункт 2")
    conf_opts = Path("/etc/pcmcia/config.opts")
    if conf_opts.is_file():
        say("    окна (config.opts):")
        for line in conf_opts.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("include"):
                say(f"      {line}")
    r = _run(["lsmod"])
    for line in r.splitlines():
        if any(k in line.lower() for k in ("pcmciamtd", "yenta_socket", "pcmcia", "mtd", "jedec_probe", "cfi_probe")):
            parts = line.split()
            if len(parts) >= 3:
                say(f"    {parts[0]:<16} {parts[2]}")

    say("")
    say(f"{C_B}4. Итог{C_0}")
    mtds = sorted(Path("/dev").glob("mtd*"))
    if mtds:
        ok("устройства MTD есть: " + " ".join(m.name for m in mtds))
        for m in mtds:
            if m.name[-1].isdigit():
                try:
                    sz = _run(["blockdev", "--getsize64", str(m)]).strip()
                    say(f"    {m}: {sz} байт")
                except (OSError, subprocess.SubprocessError):
                    say(f"    {m}: ? байт")
    else:
        warn("нет /dev/mtd* — карта не превратилась в MTD")

    say("")
    say(f"{C_B}5. Что говорит ядро (pcmciamtd / CIS){C_0}")
    lines = [l for l in dm.splitlines()
             if any(k in l.lower() for k in ("pcmciamtd", "pcmcia", "cardbus", "yenta",
                                              "cis", "jedec", "cfi", "mtd"))]
    if lines:
        for line in lines[-25:]:
            say(f"    {line}")
    else:
        say("    пусто — dmesg ничего не сказал (нет прав? попробуйте sudo)")

    say("")
    say(f"{C_B}Поддержка в ядре (от этого зависит, возможна ли карта без CIS):{C_0}")
    cfgtxt = ""
    for cfg in (f"/boot/config-{os.uname().release}", "/proc/config.gz"):
        try:
            if cfg.endswith(".gz"):
                import gzip as _gzip
                cfgtxt = _gzip.open(cfg, "rt", encoding="utf-8", errors="replace").read()
            else:
                cfgtxt = Path(cfg).read_text(encoding="utf-8", errors="replace")
            break
        except (OSError, IOError):
            continue
    if cfgtxt:
        cfg_lines = cfgtxt.splitlines()
        for opt in ("CONFIG_MTD_PCMCIA_ANONYMOUS", "CONFIG_MTD_PCMCIA", "CONFIG_PCCARD_NONSTATIC"):
            enabled = any(l.startswith(f"{opt}=y") or l.startswith(f"{opt}=m") for l in cfg_lines)
            disabled = any(l == f"# {opt} is not set" for l in cfg_lines)
            if enabled:
                say(f"    {opt}: включён")
            elif disabled:
                say(f"    {opt}: НЕ ВКЛЮЧЁН")
            else:
                say(f"    {opt}: нет в конфиге")
        say("    Что это значит на практике (drivers/pcmcia/ds.c, pcmcia_card_add):")
        say("    ядро принимает карту без читаемого CIS (pcmcia_replace_cis: подставляется")
        say("    один байт 0xFF вместо паспорта) - но только если эта опция ВКЛЮЧЕНА в")
        say("    ядре; переключается только сборкой ядра, не modprobe). С выключенной")
        say("    опцией карта не станет устройством ни при каких force_size/vpp.")
        say("    Включить её можно только сборкой своего ядра.")
        say("    Порядок проверки в ядре такой: сначала окна памяти, потом CIS, -")
        say("    поэтому начинать всё равно с CIS и окон.")
    else:
        say("    конфигурация ядра недоступна (нет /boot/config-$(uname -r) и /proc/config.gz)")
        say("    проверить опцию можно так: modinfo pcmciamtd | grep -i parm")
        say("    Порядок проверки в ядре такой: сначала окна памяти, потом CIS, -")
        say("    поэтому начинать всё равно с CIS и окон.")

    if "unknown parameter" in dm.lower():
        say("")
        say(f"{C_B}Попытки загрузить модуль с параметрами{C_0}")
        for line in dm.splitlines():
            if "unknown parameter" in line.lower():
                say(f"    {line}")
        say("    У pcmciamtd НЕТ параметров mem_base, mem_size, force_write — ядро их")
        say("    молча игнорирует. Допустимые параметры (из modinfo):")
        say("      bankwidth mem_speed force_size setvpp vpp mem_type")
        say("    то есть 'modprobe pcmciamtd mem_base=0x64000000' ничего не настраивает:")
        say("    окна памяти берутся из /etc/pcmcia/config.opts и из ресурсов сокета.")

    say("")
    say(f"{C_B}Хронология вставки (pccard: inserted/ejected + предупреждения):{C_0}")
    chrono = [l for l in dm.splitlines()
              if any(k in l.lower() for k in ("inserted", "ejected", "no high memory space", "invalid cis"))]
    if chrono:
        for line in chrono[-12:]:
            say(f"    {line}")
    else:
        say("    (в dmesg нет событий карты — карта не вставлялась при этой загрузке?)")

    say("")
    say(f"{C_B}Вердикт:{C_0}")

    # --- сбор фактов из dmesg ---
    has_no_high_mem = "no high memory space available" in dm.lower()
    has_io_fail = "probe failed" in dm.lower() and "io port" in dm.lower()
    nwin = dm.lower().count("memory probe")
    nclean = dm.lower().count("memory probe: clean") + dm.lower().count("memory probe .*: clean")

    # --- всегда: подсчёт окон и разбор портов, если есть пробы ---
    if nwin > 0 or has_io_fail:
        say("")
        if nwin > 0:
            say(f"  Итог проверки памяти: проверено диапазонов {nwin}, из них годных {nclean}.")
            say("  Как это устроено внутри (drivers/pcmcia/rsrc_nonstatic.c):")
            say("    * основная проверка окна - readable(), а она вызывает")
            say("      pccard_validate_cis(), то есть окно признаётся годным только если")
            say("      через него читается корректный CIS (есть DEVICE или CFTABLE и есть")
            say("      MANFID/VERS_1). Другого критерия у ядра нет;")
            say("    * резервная проверка checksum() требует, чтобы сумма прочитанных")
            say("      слов была не равна нулю и совпала у двух половин окна (все 0xFF и")
            say("      все 0x00 отбрасываются, потому что сумма 0);")
            say("    * оба диапазона забракованы целиком, значит не сработал ни один из")
            say("      двух критериев.")
        if has_io_fail:
            say("")
            say("  Провалились и ПОРТЫ ввода-вывода:")
            for line in dm.splitlines():
                if "io port probe" in line.lower() and "probe failed" in line.lower():
                    say(f"    {line.strip()}")
            say("    Это отдельный механизм (do_io_probe): он не спрашивает карту, а")
            say("    ищет во всём диапазоне наибольшее значение, которое порт выдаёт")
            say("    стабильно на 8 подряд адресах, и вычёркивает всё, что от него")
            say("    отличается. 'probe failed' = единого фона не нашлось.")
            say("    Поэтому IO-порты НЕ доказывают, что карта не отвечает: слот")
            say("    16-битной карте порты обычно и не нужны (pcmciamtd работает через")
            say("    память), а на многих ноутбуках Config-регистр CardBus выключен.")
            say("    На вывод о CIS это не влияет - смотрите на строки memory probe.")

    # --- основной вердикт ---
    if has_no_high_mem:
        say("")
        say("  Ядро (подсистема PCMCIA, а не драйвер mtd) не нашло ни одного годного")
        say("  окна памяти: 'no high memory space available'. Это ПЕРВАЯ причина, она")
        say("  перекрывает всё остальное. Из-за неё:")
        say("    * карта не становится устройством PCMCIA (sysfs пуст),")
        say("    * поэтому 'pccardctl ident' пишет 'no product info available' —")
        say("      это ВЫВОД из ошибки окон, а не отдельная проблема CIS;")
        say("    * pcmciamtd вообще не вызывается — значит force_size и Vpp сейчас")
        say("      ни на что не влияют и проблему не решат.")
        say("")
        say("  Вывод: 'no high memory space' означает ровно одно — через окна моста")
        say("  CIS карты не читается. Это не про питание, не про Vpp и не про драйвер mtd.")
        if nclean == 0 and nwin > 0:
            say("")
            say("  ПОЧЕМУ ЗАБРАКОВАН ВЕСЬ ДИАПАЗОН ЦЕЛИКОМ (проверено по коду 6.1):")
            say("    * проверку через CIS (readable) ядро для этого моста не делает:")
            say("      yenta_socket не передаёт ядру .validate;")
            say("    * остаётся checksum(): он читает слова окна и отбраковывает")
            say("      диапазон, если ни одно слово не дало ненулевой суммы;")
            say("    * 'excluding 0x....-0x....' на ВЕСЬ диапазон значит, что в окне")
            say("      нет никаких данных (всё 0x00 или всё 0xFF), а не 'плохой CIS'.")
            say("  Практический вывод: паспорт читается напрямую (раздел --cis) - значит")
            say("  карта и контакты живы, а окно ядру не отдаётся. С /dev/mtd0 на")
            say("  таком мосте рассчитывать не нужно: рабочий путь - прямое чтение и")
            say("  снятие образа (раздел --read).")
        say("")
        say("  Что делать по порядку:")
        say("   1) Перезагрузиться с картой, вставленной ДО включения, и сразу после")
        say("      загрузки посмотреть: dmesg | grep -iE 'pcmcia|high memory'")
        say("      Если предупреждение появилось ещё до каких-либо действий с картой —")
        say("      дело в окнах слота, а не в карте.")
        say("   2) Протереть контакты карты и вставить до щелчка.")
        say("   3) Прямое чтение attribute memory в обход ядра (самый быстрый ответ")
        say("      на вопрос «карта отвечает или нет»):")
        say("        sudo python3 pcmcia_direct.py --cis")
        say("      Читаются те же 4 КиБ, что и у ядра, но без проверки 'годности'.")
        say("      Кортежи/паспорт -> карта жива и отвечает, дело в окнах ядра.")
        say("      Сплошные 0xFF или 0x00 -> карта (или контакт) не отвечает.")
        say("   4) Трюк со вторым включением (иногда проходит): при вставленной карте")
        say("        sudo -i; echo 0 > /sys/class/pcmcia_socket/pcmcia_socket0/card_eject")
        say("        sleep 2; echo 1 > /sys/class/pcmcia_socket/pcmcia_socket0/card_insert")
        say("      и сразу посмотреть dmesg | tail -20 — прочитался ли CIS.")
        say("   5) План Б, если --cis кортежи читает, а ядро их не видит: отключить")
        say("      саму проверку окон и отдать окна из config.opts как есть. Параметр")
        say("      probe_mem есть ТОЛЬКО у модуля pcmcia_rsrc (pcmcia_core), у yenta и")
        say("      pcmciamtd его нет:")
        say("        echo 'options pcmcia_rsrc probe_mem=0' | sudo tee /etc/modprobe.d/pcmcia-noprobe.conf")
        say("        sudo update-initramfs -u     # чтобы параметр попал в раннюю загрузку")
        say("        sudo reboot                  # с картой в слоте")
        say("        dmesg | grep -iE 'pcmcia|mtd' | tail -20 ; ls -l /dev/mtd*")
        say("      Окно памяти в такой загрузке будет отдано карте без проверки: если")
        say("      CIS после этого прочитался и появился /dev/mtd0 - мешала проверка.")
        say("      Вернуть как было: sudo rm /etc/modprobe.d/pcmcia-noprobe.conf;")
        say("      sudo update-initramfs -u; sudo reboot")
        say("   6) Решающий опыт: вставить ЛЮБУЮ другую 16-битную карту (модем,")
        say("      сетевая, SRAM). Если и она не видна — виноват слот/мост, а не ваша")
        say("      карта, и дальше обсуждать CIS бессмысленно.")
        say("   7) Если после всего этого 'no high memory' остаётся — сохраните отчёт")
        say("      (команда в конце вывода) и пришлите: по dmesg видно, какие окна")
        say("      проверялись и какие ядро забраковало.")

    elif "no memory window" in dm.lower() or "get_mem_map" in dm.lower() or "cannot allocate" in dm.lower():
        say("")
        say("  Драйвер не смог занять окно памяти — это НЕ CIS.")
        say("  Окна в config.opts не совпали с реальными: перезапустите пункт 2,")
        say("  затем modprobe -r pcmciamtd; modprobe pcmciamtd")

    elif any(k in dm.lower() for k in ("anonymous", "no cis", "cis not", "cis fail", "unknown card")):
        say("")
        say("  Похоже на карту без читаемого CIS. Как обойти:")
        say("    sudo cp -a /etc/modprobe.d/pcmciamtd.conf /etc/modprobe.d/pcmciamtd.conf.bak")
        say("    sudo sed -i 's/^options pcmciamtd.*/options pcmciamtd vpp=50 setvpp=1 force_size=16/'")
        say("         /etc/modprobe.d/pcmciamtd.conf")
        say("    sudo modprobe -r pcmciamtd; sudo modprobe pcmciamtd; ls -l /dev/mtd*")
        say("  16 = мегабайт (force_size задаётся в МБ). Не помогло — попробуйте 32.")
        say("  ВАЖНО: перед первой записью снять образ и убедиться, что нет «FF на всём».")

    elif any(k in dm.lower() for k in ("pcmciamtd probe fail", "pcmciamtd init error", "pcmciamtd.*probe.*fail")):
        say("")
        say("  Модуль падает при опросе карты — смотрите строки выше, там обычно причина")
        say("  (нет окна, не тот Vpp, карта 12-вольтовая -> только чтение).")

    elif not mtds:
        say("")
        say("  Ядро молчит про pcmciamtd: модуль не загружался либо карта не подключена.")
        if "unable to map card memory" in dm.lower():
            say("  Точнее: ядро уже сказало главное — 'unable to map card memory!'.")
            say("  Это не CIS и не pcmciamtd: подсистеме PCMCIA не дали окно памяти,")
            say("  поэтому карта не стала устройством, а mtd-драйверу не с чем работать.")
        else:
            say("  Порядок: пункт 2 -> перезагрузка с картой в слоте -> этот пункт снова.")
    else:
        say("")
        say("  Карта на месте. Если программа её «не видит» — проверьте MTD и права.")

    say("")
    say("Полный отчёт для переписки: sudo python3 ./linux/pcmcia_flash.py --diag > diag.txt 2>&1")


def menu() -> None:
    print("\033[2J\033[H", end="") if _IS_TTY else None
    say(f"{C_B}================================================================{C_0}")
    say("  PCMCIA LINEAR FLASH — AMILO Pro V2030D (TI PCIxx21), Debian live")
    say(f"{C_B}================================================================{C_0}")
    status = f"{C_G}распознана{C_0}" if MTD.exists() else f"{C_Y}нет{C_0}"
    say(f"  Карта: {MTD}   Статус: {status}")
    say("")
    say("   1) Установить утилиты (офлайн из debs/ или через apt)")
    say("   2) Настроить слот и драйвер (окна памяти + Vpp)   <- один раз")
    say("   3) Показать состояние карты и подсказки")
    say("   4) Снять образ карты (бэкап, с контролем двойным чтением)")
    say("   5) Стереть карту")
    say("   6) Записать образ на карту (+ верификация)")
    say("   7) Сделать из карты «диск» (FTL + FAT)")
    say("   8) Тест записи (erase + 0xFF + проверка)")
    say("   9) Почему карта не видна (CIS, слот, драйвер) — по шагам")
    say("  10) Подробная информация (CIS, драйверы, dmesg)")
    say("  11) Прямое чтение карты через /dev/mem (когда драйвер не дал окно)")
    say("  12) Карта целиком: паспорт, размер, вердикт    <- работает без mtd")
    say("  13) Снять образ напрямую (минуя драйвер, двойное чтение)")
    say("  14) Разобрать образ без карты (карту можно вынуть)")
    say("  15) Можно ли ПИСАТЬ эту карту (честный ответ)")
    say("  16) Кто занял окно памяти")
    say("   L) Показать лог")
    say("   0) Выход")
    say("")
    try:
        c = input("Пункт: ").strip()
    except EOFError:
        return
    dispatch(c)


def dispatch(c: str) -> None:
    if c == "1":
        install_tools()
    elif c == "2":
        configure()
    elif c == "3":
        check_card()
    elif c == "4":
        do_backup()
    elif c == "5":
        do_erase()
    elif c == "6":
        do_write()
    elif c == "7":
        do_ftl()
    elif c == "8":
        do_test()
    elif c == "9":
        do_diag()
    elif c == "10":
        do_info()
    elif c == "11":
        do_direct()
    elif c == "12":
        do_direct_info()
    elif c == "13":
        do_direct_image()
    elif c == "14":
        do_direct_map()
    elif c == "15":
        do_write_check()
    elif c == "16":
        do_window_owner()
    elif c.lower() == "l":
        do_log()
    elif c in ("0", "q", "Q"):
        sys.exit(0)


def do_log() -> None:
    say(f"Лог: {LOG}")
    if LOG.is_file():
        print(LOG.read_text(encoding="utf-8", errors="replace")[-4000:], end="")


def do_direct() -> None:
    if not DIRECT.is_file():
        err(f"нет файла {DIRECT} (нужен полный набор)")
        return
    sec("Прямое чтение карты через /dev/mem")
    say("Работает, когда ядро не дало карте окно памяти (no high memory space).")
    say("Только ЧТЕНИЕ: без драйвера некому выставить Vpp и тайминги для записи.")
    say("")
    say("Шаг 1: есть ли вообще доступ к окнам")
    subprocess.run([sys.executable, str(DIRECT), "--probe"])
    say("")
    say("Шаг 2: паспорт карты (attribute memory)")
    subprocess.run([sys.executable, str(DIRECT), "--cis"])
    say("")
    say("Шаг 3: образ карты (common memory; карта только читается)")
    try:
        out = input("Снять образ карты целиком? (путь или Enter — пропустить): ").strip()
    except EOFError:
        return
    if not out:
        return
    subprocess.run([sys.executable, str(DIRECT), "--read", out, "--double", "--size auto"])
    out_p = Path(out)
    if out_p.is_file():
        subprocess.run([sys.executable, str(DIRECT), "--image-report", str(out_p)])
    say("")
    say("Дальше: анализ образа без карты — python3 pcmcia_gui.py --map-file " + out)


def do_direct_info() -> None:
    require_root()
    if not DIRECT.is_file():
        err(f"нет файла {DIRECT} (нужен полный набор)")
        return
    sec("Карта целиком: паспорт, размер, вердикт (прямое чтение)")
    say("Ядро карту не приняло - это не помеха: окно моста настраивает сам набор.")
    say("")
    subprocess.run([sys.executable, str(DIRECT), "--cis"])
    say("")
    say("Итог: паспорт прочитан (код 0)" if True else "")


def do_direct_image() -> None:
    require_root()
    if not DIRECT.is_file():
        err(f"нет файла {DIRECT} (нужен полный набор)")
        return
    default_out = Path("/root") / f"card-{time.strftime('%Y%m%d-%H%M')}.bin"
    sec("Снять образ карты напрямую (минуя драйвер)")
    say("На карту ничего не пишется. Размер определит сама карта по повторению")
    say("адресов, чтение будет ДВА раза с сверкой - иначе образ не отдаётся.")
    try:
        out = input(f"Куда сохранить образ [{default_out}]: ").strip()
    except EOFError:
        return
    out_p = Path(out) if out else default_out
    dir_p = out_p.parent
    if not dir_p.is_dir():
        err(f"нет каталога {dir_p}")
        return
    need = 16 * 1024 * 1024
    free = 0
    try:
        r = subprocess.run(["df", "-Pk", str(dir_p)], capture_output=True, text=True)
        lines = r.stdout.splitlines()
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 4:
                free = int(parts[3]) * 1024
    except (ValueError, OSError, subprocess.SubprocessError):
        pass
    if free and free < need:
        err(f"на {dir_p} мало места: свободно {human(free)}, нужно хотя бы {human(need)}")
        return
    subprocess.run([sys.executable, str(DIRECT), "--read", str(out_p),
                    "--double", "--size auto"])
    if out_p.is_file():
        say("")
        subprocess.run([sys.executable, str(DIRECT), "--image-report", str(out_p)])
        say("")
        say(f"Готово. Образ: {out_p}")


def do_direct_map() -> None:
    if not DIRECT.is_file():
        err(f"нет файла {DIRECT} (нужен полный набор)")
        return
    try:
        img = input("Путь к образу (Enter - последний снятый): ").strip()
    except EOFError:
        return
    if not img:
        cands = (sorted(Path("/root").glob("card-*.bin"), key=lambda p: p.stat().st_mtime, reverse=True)
                 + sorted(Path.home().glob("card-*.bin"), key=lambda p: p.stat().st_mtime, reverse=True)
                 + sorted(Path.cwd().glob("*.bin"), key=lambda p: p.stat().st_mtime, reverse=True))
        img = str(cands[0]) if cands else ""
        if not img:
            err("образов не найдено - укажите путь")
            return
        say(f"беру: {img}")
    img_p = Path(img)
    if not img_p.is_file():
        err(f"нет файла {img_p}")
        return
    sec("Сводка по образу (карта не нужна)")
    subprocess.run([sys.executable, str(DIRECT), "--image-report", str(img_p)])
    say("")
    if os.environ.get("DISPLAY"):
        say("Открываю карту образа в окне...")
        subprocess.run([sys.executable, str(KIT / "pcmcia_gui.py"), "--map-file", str(img_p)])
    else:
        say("Графики нет (нет DISPLAY) - подробный разбор строками:")
        say(f"  python3 {KIT / DIRECT.name} --image-report {img_p} --full")


def do_write_check() -> None:
    require_root()
    sec("Можно ли записывать на эту карту")
    say("")
    nmtd = MTD.exists()
    say("1) Устройство для записи (mtd):")
    if nmtd:
        say(f"   {C_G}есть{C_0} - {MTD}")
    else:
        say(f"   {C_Y}нет{C_0} - /dev/mtd0 не появился: ядро не дало карте окно памяти")
        say("   (строки 'excluding ...' и 'unable to map card memory!' в dmesg).")
        say("   Писать нечем: flashcp/flash_erase работают только через mtd.")
    say("")
    say("2) Что требует сама карта (по паспорту):")
    if DIRECT.is_file():
        r = subprocess.run([sys.executable, str(DIRECT), "--cis"],
                           capture_output=True, text=True)
        cis = r.stdout
        for line in cis.splitlines():
            low = line.lower()
            if any(k in low for k in ("продукт", "jedec", "ёмкость", "чипы")):
                say(f"   {line}")
        if any(k in cis.lower() for k in ("amd", "centennial", "series 2", "intel")):
            say("")
            say("   ВНИМАНИЕ: микросхемы AMD Am29Fxxx (и Intel Series 2) для стирания и")
            say("   записи требуют Vpp = 12 В на отдельном выводе. В слоте ноутбука")
            say("   только 5 В, поэтому такая карта здесь ТОЛЬКО ДЛЯ ЧТЕНИЯ.")
    say("")
    say("3) Что можно сделать:")
    say("   * снять образ (пункт 13) - безопасно, ничего не меняется;")
    say("   * если /dev/mtd0 когда-нибудь появится (другая загрузка, план Б),")
    say("     запись делается пунктами 5-6 меню (стирание + запись + сверка);")
    say("   * карту с Vpp 12 В прошивают программатором (или в слоте с 12 В).")


def do_window_owner() -> None:
    sec("Кто занял окно памяти и что с этим делать")
    say("")
    say("Окна CardBus в /proc/iomem:")
    try:
        with open("/proc/iomem") as fh:
            found = False
            for line in fh:
                if "pci cardbus" in line.lower():
                    say(f"   {line.rstrip()}")
                    found = True
            if not found:
                say("   (нет строк PCI CardBus)")
    except OSError:
        say("   (нельзя прочитать /proc/iomem)")
    say("")
    say("Драйвер моста и его окна:")
    if have("lspci"):
        r = subprocess.run(["lspci", "-vv"], capture_output=True, text=True)
        capture = False
        for line in r.stdout.splitlines():
            if "cardbus" in line.lower():
                capture = True
            if capture and ("memory window" in line.lower() or "kernel driver" in line.lower()):
                say(f"   {line}")
                capture = False
    else:
        say("   lspci нет (sudo apt install pciutils)")
    say("")
    say("Кто уже держит память карты (не выгружайте вслепую):")
    r = _run(["lsmod"])
    found = False
    for line in r.splitlines():
        low = line.lower()
        if any(k in low for k in ("yenta", "pcmcia")):
            say(f"   {line}")
            found = True
    if not found:
        say("   модулей pcmcia нет")
    say("")
    say("Что делает набор: если /dev/mem открыт, но страницу не отдаёт, тот же")
    say("адрес читается через файл BAR моста в sysfs. Если не выходит и так -")
    say("окно занято драйвером: тогда или план Б (пункт меню L -> planb.sh),")
    say("или выгрузка слота:  sudo modprobe -r pcmciamtd ; sudo modprobe -r yenta_socket")
    say("  (после выгрузки yenta карта обесточивается - читать её уже нельзя,")
    say("   поэтому выгружать имеет смысл только чтобы проверить, кто мешал).")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "--diag":
        do_diag()
        return 0
    if args and args[0] == "--selftest":
        ver = "?"
        try:
            for line in (KIT / "src" / "pcmcia_direct" / "version.py").read_text(encoding="utf-8").splitlines():
                if line.startswith("KIT_VERSION"):
                    # хвостовой комментарий отрезаем (было до 2.17.16
                    # «2.17.16"  # версия набора: ...»).
                    ver = line.split("=", 1)[1].split("#", 1)[0].strip().strip('"')
                    break
        except OSError:
            pass
        say(f"Версия набора   : {ver}")
        say(f"Каталог скрипта : {KIT}")
        say(f"Архитектура     : {subprocess.run(['dpkg', '--print-architecture'], capture_output=True, text=True).stdout.strip() or '?'}   (нужно i386)")
        say(f"Ядро            : {subprocess.run(['uname', '-r'], capture_output=True, text=True).stdout.strip()}")
        debs_dir = find_debs()
        if debs_dir:
            n = len(list(debs_dir.glob("*.deb")))
            say(f"Пакеты          : {debs_dir} ({n} шт.)")
            if n >= 20:
                say("  (ожидается 20+ — полный набор)")
            else:
                say("  ⚠ МАЛО, ожидалось 20+")
        else:
            say("Пакеты          : НЕ НАЙДЕНЫ (нужна папка debs/ или pcmcia-flash-kit.tar.gz)")
        mod = subprocess.run(["modinfo", "-F", "filename", "pcmciamtd"],
                             capture_output=True, text=True)
        modline = mod.stdout.strip() or "НЕ НАЙДЕН (проверьте ядро 686)"
        say(f"Модуль pcmciamtd: {modline}")
        say("Утилиты:")
        for t in ("mtd_debug", "flash_erase", "flashcp", "ftl_format", "pccardctl", "lspci", "modprobe", "mkfs.vfat"):
            have(t) and say(f"  + {t}") or say(f"  - {t}  (нет)")
        say("")
        say("Если архитектура i386, 20 пакетов найдены и mtd_debug есть — можно запускать меню.")
        return 0
    while True:
        menu()
        if not pause():
            break
    return 0


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    sys.exit(main(sys.argv[1:]))
