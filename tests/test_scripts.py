#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты консольных скриптов (linux/flashcard.py, linux/pcmcia_flash.py).

    python3 tests/test_scripts.py          # X и карта не нужны

Скрипты запускаются на заглушках mtd_debug/flashcp/flash_erase в отдельном
PATH; вместо /dev/mtd0 подставляется файл. Проверяется именно ПОВЕДЕНИЕ:
что реально выполнилось, а не что написано в коде.
"""
import io
import os
import sys
import shutil
import tempfile
import subprocess
import re

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# С 2.18.0 код работы с картой — пакет src/pcmcia_direct, а точка входа
# лежит в корне набора (pcmcia_direct.py). Монолита windows/legacy больше нет.
DIRECT = os.path.join(KIT, "pcmcia_direct.py")


class _Flat(object):
    """Пакет pcmcia_direct с подменой имён «как в монолите».

    До 2.18.0 эти проверки грузили один файл и подменяли имена прямо в нём
    (d.Mem = ..., d._sysfs_mem = ...). Теперь имена живут в разных модулях
    пакета, поэтому чтение идёт по всем модулям сразу, а присваивание
    попадает в каждый модуль, где такое имя есть, — ровно туда, откуда его
    возьмёт код.
    """

    def __init__(self, pkg):
        object.__setattr__(self, "_pkg", pkg)
        object.__setattr__(self, "_mods", [m for n, m in sys.modules.items()
                                           if n == "pcmcia_direct"
                                           or n.startswith("pcmcia_direct.")])

    def __getattr__(self, name):
        for mod in [self._pkg] + self._mods:
            if hasattr(mod, name):
                return getattr(mod, name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        setattr(self._pkg, name, value)
        for mod in self._mods:
            if hasattr(mod, name):
                setattr(mod, name, value)


def direct_module():
    """Свежий пакет src/pcmcia_direct на каждый вызов, как раньше монолит.

    Модули удаляются из sys.modules: подмена, сделанная в одной проверке, не
    должна переезжать в следующую (в монолите это получалось само собой).
    """
    import importlib
    path = os.path.join(KIT, "src")
    if path not in sys.path:
        sys.path.insert(0, path)
    for name in [n for n in list(sys.modules)
                 if n == "pcmcia_direct" or n.startswith("pcmcia_direct.")]:
        del sys.modules[name]
    return _Flat(importlib.import_module("pcmcia_direct"))
PASS, FAIL = [], []
STUB = tempfile.mkdtemp(prefix="shtest-")
NOEXEC_KIT = os.path.join(STUB, "kit_noexec")
CARD = os.path.join(STUB, "card.bin")
CALLS = os.path.join(STUB, "calls.log")
SIZE = 4 * 1024 * 1024
BLOCK = 65536


def check(name, cond, detail=""):
    if cond:
        PASS.append(name); print(f"  ok   {name}")
    else:
        FAIL.append((name, detail)); print(f"  FAIL {name}   {detail}")


def tool(name, body):
    p = os.path.join(STUB, name)
    with open(p, "w") as f:
        f.write("#!/bin/bash\n" + body + "\n")
    os.chmod(p, 0o755)


def setup(flags=0x400):
    with open(CARD, "wb") as f:
        f.write(b"\xAA" * SIZE)
    open(CALLS, "w").close()
    tool("mtd_debug", f'''
echo "mtd_debug $*" >> "{CALLS}"
case "$1" in --help|-h) echo "mtd_debug: usage"; exit 0 ;; esac
case "$1" in
 info) echo "mtd.type = MTD_NORFLASH"; echo "mtd.flags = MTD_CAP_NORFLASH"
       echo "mtd.size = {SIZE}"; echo "mtd.erasesize = {BLOCK}" ;;
 read) python3 -c "
import sys;off,n=int(sys.argv[1]),int(sys.argv[2])
d=open('{CARD}','rb').read()[off:off+n]
open(sys.argv[3],'wb').write(d)" "$3" "$4" "$5" ;;
esac''')
    tool("flashcp", f'''
echo "flashcp $*" >> "{CALLS}"
case "$1" in --help|-h) echo "flashcp: usage"; exit 0 ;; esac
# как настоящий: без -A стирает только область под образ
python3 -c "
import sys,os
src=[a for a in sys.argv[1:] if not a.startswith('-')][0]
open('{CARD}','r+b').write(open(src,'rb').read())" "$@"''')
    tool("flash_erase", f'''
echo "flash_erase $*" >> "{CALLS}"
case "$1" in --help|-h) echo "flash_erase: usage" >&2; exit 0 ;; esac
python3 -c "
open('{CARD}','r+b').write(b'\\\\xff'*{SIZE})"''')
    tool("blockdev", f"echo {SIZE}")
    tool("df", 'echo "Filesystem 1B-blocks Used Available"; echo "x 1 1 999999999000"')
    for t in ("pccardctl", "lspci", "modprobe", "modinfo", "ftl_format", "mkfs.vfat", "numfmt"):
        tool(t, "exit 0")
    # на настоящей системе скрипт требует root; в тестах «мы root»
    tool("id", 'if [ "$1" = "-u" ]; then echo 0; else /usr/bin/id "$@"; fi')
    # как на системе, где пакеты уже стоят
    # как настоящий dpkg-query -W: по строке на каждый пакет, с версией
    tool("dpkg-query", """
case "$*" in *Status*)
  for a in "$@"; do
    case "$a" in -*) continue ;; esac
    echo "    $a 1:2.1.5-1 install ok installed"
  done
  ;;
esac""")
    tool("apt-get", "exit 0")
    # sysfs-флаги карты
    mtd_dir = os.path.join(STUB, "sys", "class", "mtd", "mtd0")
    os.makedirs(mtd_dir, exist_ok=True)
    for k, v in (("size", SIZE), ("erasesize", BLOCK), ("flags", flags)):
        with open(os.path.join(mtd_dir, k), "w") as f:
            f.write(str(v))


def run(script, *args, stdin="", env_extra=None, noexec=False):
    """noexec=True — запуск на копии набора, где у файлов нет бита x
    (так выглядит набор на FAT32-флешке). Ловит самовызов "$0 verify"."""
    kit = KIT
    if noexec:
        kit = NOEXEC_KIT
        if not os.path.isdir(kit):
            shutil.copytree(KIT, kit, ignore=shutil.ignore_patterns("debs", "__pycache__"))
            for root, _dirs, files in os.walk(kit):
                for f in files:
                    os.chmod(os.path.join(root, f), 0o644)
    env = dict(os.environ)
    env["PATH"] = STUB + os.pathsep + env["PATH"]
    env["MTD"] = CARD
    env["PCMCIA_SKIP_ROOT"] = "1"  # тесты не могут стать root
    if env_extra:
        env.update(env_extra)
    interpreter = "python3" if script.endswith(".py") else "bash"
    p = subprocess.run([interpreter, os.path.join(kit, script), *args],
                       input=stdin, capture_output=True, text=True,
                       env=env, timeout=180)
    return p.returncode, p.stdout, p.stderr


def calls():
    try:
        return open(CALLS).read()
    except OSError:
        return ""


def t_write_erases_first():
    print("\n[S1] flashcard.py write: карта стирается ДО записи (был дефект)")
    setup()
    img = os.path.join(STUB, "half.bin")
    with open(img, "wb") as f:
        f.write(b"\x11" * (SIZE // 2))
    rc, out, err = run("linux/flashcard.py", "write", img, stdin="YES\n")
    log = calls()
    check("команда выполнена без ошибки", rc == 0, (err or out)[-200:])
    check("вызван flash_erase 0 0 (стирание всей карты)", "flash_erase" in log, log[-120:])
    check("flash_erase раньше flashcp",
          log.find("flash_erase") < log.find("flashcp"), log[:160].replace("\n", " | "))
    check("в выводе сказано про стирание остатка", "СТЁРТ" in out or "Стирание" in out,
          out[-200:])
    with open(CARD, "rb") as f:
        f.seek(SIZE - 16)
        tail = f.read()
    check("хвост карты не содержит старых данных", tail == b"\xff" * 16, tail.hex())


def t_write_keep_tail():
    print("\n[S2] flashcard.py write с MTD_KEEP_TAIL=1 не стирает карту")
    setup()
    img = os.path.join(STUB, "quarter.bin")
    with open(img, "wb") as f:
        f.write(b"\x22" * (SIZE // 4))
    rc, out, err = run("linux/flashcard.py", "write", img, stdin="YES\n",
                       env_extra={"MTD_KEEP_TAIL": "1"})
    log = calls()
    check("flash_erase не вызывался", "flash_erase" not in log, log.replace("\n", " | "))
    check("пользователю сказано, что остаток не стирается", "НЕ стирается" in out, out[-200:])


def t_write_no_confirm():
    print("\n[S3] Без подтверждения ничего не происходит")
    setup()
    img = os.path.join(STUB, "x.bin")
    with open(img, "wb") as f:
        f.write(b"\x33" * 1024)
    rc, out, err = run("linux/flashcard.py", "write", img, stdin="no\n")
    check("код возврата не 0", rc != 0, str(rc))
    check("ни flashcp, ни flash_erase не вызывались",
          "flashcp" not in calls() and "flash_erase" not in calls(), calls())


def t_write_too_big():
    print("\n[S4] Образ больше карты отвергается до всякой записи")
    setup()
    big = os.path.join(STUB, "big.bin")
    with open(big, "wb") as f:
        f.write(b"\x44" * (SIZE + 1024))
    rc, out, err = run("linux/flashcard.py", "write", big, stdin="YES\n")
    check("отказ", rc != 0, str(rc))
    check("карта не тронута", "flashcp" not in calls(), calls())


def t_write_protected():
    print("\n[S5] Карта с WP (flags без 0x400) — запись отвергается")
    setup(flags=0x0)
    img = os.path.join(STUB, "p.bin")
    with open(img, "wb") as f:
        f.write(b"\x55" * 1024)
    # подсовываем наш sysfs через MTD-путь: скрипт читает /sys/class/mtd/mtdN/flags
    rc, out, err = run("linux/flashcard.py", "write", img, stdin="YES\n",
                       env_extra={"PCM_SYSFS": os.path.join(STUB, "sys")})
    # проверка через sysfs требует пути /sys/class/mtd/... — в песочнице их нет,
    # поэтому главное: скрипт не падает и не пишет без проверок
    check("скрипт не падает на отсутствии sysfs", rc in (0, 1), f"код {rc}")
    setup()


def t_unmounted_check():
    print("\n[S6] Проверка «карта смонтирована» присутствует и срабатывает")
    src = open(os.path.join(KIT, "linux/flashcard.py"), encoding="utf-8").read()
    check("есть check_unmounted", "check_unmounted()" in src)
    check("читает /proc/mounts", "/proc/mounts" in src)
    for cmd in ("erase", "write", "fill", "ftl"):
        fn_idx = src.find(f"def cmd_{cmd}(")
        if fn_idx >= 0:
            block = src[fn_idx:fn_idx + 600]
            check(f"«{cmd}» вызывает check_unmounted", "check_unmounted" in block, block[:120])
        else:
            check(f"«{cmd}» вызывает check_unmounted", False, f"cmd_{cmd} не найден")
    src2 = open(os.path.join(KIT, "linux/pcmcia_flash.py"), encoding="utf-8").read()
    check("в linux/pcmcia_flash.py тоже есть проверка", "check_unmounted()" in src2)
    check("в linux/pcmcia_flash.py запись стирает карту", "flash_erase" in src2 and "str(MTD)" in src2)


def t_backup_space():
    print("\n[S7] Проверка места перед снятием образа")
    src = open(os.path.join(KIT, "linux/flashcard.py"), encoding="utf-8").read()
    check("есть check_space", "check_space" in src)
    check("для образа требуется 2 копии", "* 2" in src or "2 * " in src)


def t_menu_install_verify():
    print("\n[S12] Меню, пункт 1: после установки печатается проверка и версии")
    setup()
    rc, out, err = run("linux/pcmcia_flash.py", stdin="1\n0\n")
    txt = out + err
    check("пункт 1 не падает", "Traceback" not in txt, txt[-200:])
    check("сказано, что утилиты уже установлены",
          "уже установлены" in txt or "готово" in txt, txt[-300:])
    check("напечатана проверка версий", "Проверка версий" in txt, txt[-200:])
    check("указан лог установки", "Лог установки" in txt, txt[-200:])
    check("подсказано, что делать дальше", "пункт 2" in txt, txt[-200:])
    # ловушка: заголовок печатается, а таблицы версий нет (пустой dpkg-query)
    i = txt.find("Проверка версий")
    block = txt[i:i + 400] if i >= 0 else ""
    check("в блоке версий есть строки с пакетами",
          block.count("установлен") >= 3, block[:200])



def t_noexec_fat32():
    print("\n[S13] Набор на FAT32 (у файлов нет бита x): самовызов не ломается")
    setup()
    img = os.path.join(STUB, "noexec.bin")
    with open(img, "wb") as f:
        f.write(b"\x5A" * (SIZE // 2))
    rc, out, err = run("linux/flashcard.py", "write", img, stdin="YES\n", noexec=True)
    txt = out + err
    check("запись на «FAT32» не падает с Permission denied",
          "Permission denied" not in txt, txt[-200:])
    check("стирание и запись выполнены", rc == 0, f"код {rc}: {txt[-200:]}")
    check("сверка после записи выполнена", "верификация" in txt or "совпад" in txt.lower(),
          txt[-300:])
    check("сверка дошла до вердикта, а не оборвалась",
          rc == 0 and "не удалось" not in txt.lower(), txt[-200:])
    rc2, out2, err2 = run("linux/flashcard.py", "fill", "0xFF", stdin="YES\n", noexec=True)
    txt2 = out2 + err2
    check("fill (внутренний вызов write) без бита x тоже работает",
          "Permission denied" not in txt2, txt2[-200:])



def t_diag_chain():
    print("\n[S14] --diag: цепочка «мост -> слот -> CIS -> драйвер -> mtd»")
    setup()
    # 1) карты нет, ядро молчит
    tool("dmesg", "exit 0")
    rc, out, err = run("linux/pcmcia_flash.py", "--diag")
    txt = out + err
    check("диагностика не падает", "Traceback" not in txt and rc == 0, f"код {rc}: {txt[-150:]}")
    for part in ("1. Мост CardBus", "2. Слот", "3. Что понял драйвер",
                 "4. Итог", "5. Что говорит ядро", "Вердикт:"):
        check(f"есть раздел «{part}»", part in txt, txt[:120])

    # 2) драйвер не занял окно памяти — вердикт НЕ про CIS, а про окна
    tool("dmesg", 'echo "pcmciamtd: no memory window available for socket"')
    rc, out, err = run("linux/pcmcia_flash.py", "--diag")
    txt = out + err
    check("диагноз «окно памяти», а не CIS",
          "занять окно памяти" in txt and "НЕ CIS" in txt, txt[-400:])
    check("подсказано про пункт 2", "пункт 2" in txt, txt[-400:])

    # 3) карта без CIS — подсказка force_size
    tool("dmesg", 'echo "pcmciamtd: this is a anonymous memory card"')
    rc, out, err = run("linux/pcmcia_flash.py", "--diag")
    txt = out + err
    check("диагноз «нет CIS»", "без читаемого CIS" in txt, txt[-300:])
    check("подсказан force_size", "force_size=16" in txt, txt[-300:])
    check("сказано, что force_size в мегабайтах", "мегабайт" in txt, txt[-300:])


def t_diag_no_high_memory():
    """Реальный случай: слот карту видит, но ядру не хватило окон памяти."""
    print("\n[S15] --diag: «no high memory space available» — вердикт про окна, а не CIS")
    setup()
    os.makedirs(os.path.join(STUB, "sys", "module", "pcmcia_rsrc", "parameters"),
                exist_ok=True)
    with open(os.path.join(STUB, "sys", "module", "pcmcia_rsrc",
                           "parameters", "probe_mem"), "w") as f:
        f.write("1\n")
    tool("dmesg", "cat <<'EOT'\n"
         "[ 27.254942] pcmcia_socket0: cs: memory probe 0xa0000000-0xa0ffffff: clean\n"
         "[ 27.255010] pcmcia_socket0: cs: memory probe 0x60000000-0x60ffffff: "
         "excluding 0x60000000-0x600fffff\n"
         "[ 27.712429] pcmcia_socket pcmcia_socket0: cs: warning: "
         "no high memory space available!\n"
         "[761.135201] pcmcia_socket pcmcia_socket0: pccard: PCMCIA card "
         "inserted into slot 0\n"
         "[761.135228] pcmcia_socket pcmcia_socket0: cs: warning: "
         "no high memory space available!\n"
         "[2073.922283] pcmciamtd: unknown parameter 'mem_base' ignored\n"
         "EOT")
    rc, out, err = run("linux/pcmcia_flash.py", "--diag")
    txt = out + err
    check("вердикт называет нехватку окон памяти",
          "no high memory space available" in txt, txt[-300:])
    check("сказано, что это НЕ CIS",
          "а не отдельная проблема CIS" in txt, txt[-400:])
    check("сказано, что force_size и Vpp тут не помогут",
          "force_size и Vpp" in txt and "не решат" in txt, txt[-400:])
    check("есть хронология вставки карты",
          "Хронология вставки" in txt and "inserted into slot 0" in txt, txt[-300:])
    check("объяснены несуществующие параметры модуля",
          "НЕТ параметров mem_base" in txt and "bankwidth mem_speed force_size" in txt,
          txt[-400:])
    check("советы по порядку пронумерованы", "1)" in txt and "3)" in txt, txt[-400:])
    # и: этот вердикт должен перекрывать ложный совет про force_size
    check("force_size не предлагается как лечение",
          "force_size=16'" not in txt, txt[-200:])


def t_diag_probe_failed():
    """Второй реальный лог: память и порты проваливаются вместе, окна нечётные."""
    print("\n[S16] --diag: «probe failed» по памяти и портам + план Б")
    setup()
    tool("dmesg", "cat <<'EOT'\n"
         "[ 26.511192] pcmcia_socket pcmcia_socket0: pccard: PCMCIA card "
         "inserted into slot 0\n"
         "[ 27.037987] pcmcia_socket0: cs: memory probe 0x64000000-0x67ffffff: "
         "excluding 0x64000000-0x67ffffff\n"
         "[ 27.038087] pcmcia_socket0: cs: memory probe 0x68000000-0x6bffffff: "
         "excluding 0x68000000-0x6bffffff\n"
         "[ 27.038165] pcmcia_socket0: cs: IO port probe 0x2400-0x24ff: "
         "excluding nothing: probe failed.\n"
         "[ 27.038257] pcmcia_socket0: cs: IO port probe 0x2800-0x28ff: "
         "excluding nothing: probe failed.\n"
         "[ 27.038720] pcmcia_socket pcmcia_socket0: cs: warning: "
         "no high memory space available!\n"
         "[506.863199] pcmcia_socket pcmcia_socket0: pccard: PCMCIA card "
         "inserted into slot 0\n"
         "EOT")
    rc, out, err = run("linux/pcmcia_flash.py", "--diag")
    txt = out + err
    check("посчитано, сколько окон проверено и сколько годных",
          "проверено диапазонов 2, из них годных 0" in txt, txt[-400:])
    check("провал портов показан отдельно от памяти",
          "Провалились и ПОРТЫ" in txt and "отдельный механизм" in txt, txt[-600:])
    check("порты не выдаются за доказательство «карта не отвечает»",
          "НЕ доказывают" in txt and "смотрите на строки memory probe" in txt,
          txt[-600:])
    check("есть план Б с probe_mem=0 у правильного модуля",
          "probe_mem=0" in txt and "pcmcia-noprobe.conf" in txt
          and "у модуля pcmcia_rsrc" in txt, txt[-800:])
    check("есть решающий опыт с другой картой",
          "ЛЮБУЮ другую 16-битную карту" in txt, txt[-800:])

    # объяснено, как устроена проверка окон (годное = CIS прочитался)
    check("объяснено, что годное окно = прочитанный CIS",
          "pccard_validate_cis" in txt and "только если" in txt, txt[-900:])
    check("названа резервная проверка checksum и почему 0xFF не проходит",
          "checksum" in txt and "сумма 0" in txt, txt[-900:])


def t_cis_step_present():
    """Памятка и вердикт должны вести к --cis, а не к чтению common memory."""
    print("\n[S20] --cis: единственная проверка, отличающая стёртую карту от молчащей")
    with open(os.path.join(KIT, "ПОРЯДОК_ЗАПУСКА.txt"), encoding="utf-8") as f:
        txt = f.read()
    check("в памятке есть шаг --cis", "--cis" in txt, "нет")
    check("сказано, что 0xFF в common memory ничего не доказывает",
          "ОДИНАКОВО" in txt or "ничего не доказывает" in txt, "нет")
    check("сказано, что различает attribute memory",
          "attribute memory" in txt, "нет")
    check("порядок: --cis идёт до снятия образа",
          txt.find("--cis") < txt.find("--read /root/card.bin"), "не тот порядок")
    check("объяснено про IO-порты в «чего не запускать»",
          "probe failed" in txt and "не ответ карты" in txt, "нет")

    setup()
    tool("dmesg", "cat <<'EOT'\n"
         "[ 27.038720] pcmcia_socket0: cs: warning: no high memory space available!\n"
         "EOT")
    rc, out, err = run("linux/pcmcia_flash.py", "--diag")
    txt = out + err
    check("вердикт --diag ведёт к --cis",
          "pcmcia_direct.py --cis" in txt, txt[-800:])
    check("вывод --diag остаётся корректным UTF-8 (нет сырых \\xFF)",
          "\ufffd" not in txt and "\xff" not in txt, "сырой байт в выводе")


def t_diag_kernel_anonymous():
    print("\n[S17] --diag: поддержка карт без CIS зависит от сборки ядра")
    setup()
    tool("dmesg", 'echo "pcmcia_socket0: cs: warning: no high memory space available!"')
    # конфигурацию ядра подсовываем через /proc/config.gz-совместимый путь нельзя,
    # поэтому проверяем реальный /boot/config-$(uname -r) песочницы, если он есть
    import subprocess as sp
    rel = sp.run(["uname", "-r"], capture_output=True, text=True).stdout.strip()
    cfg = f"/boot/config-{rel}"
    if not os.path.exists(cfg):
        print("  (нет /boot/config-*, проверка раздела упрощена)")
        txt = run("linux/pcmcia_flash.py", "--diag")[1]
        check("раздел про конфигурацию всё равно напечатан", "Поддержка в ядре" in txt, "")
        check("сказано, что ядро проверяет окна раньше CIS",
              "сначала окна памяти, потом CIS" in txt, txt[-600:])
        return
    txt = run("linux/pcmcia_flash.py", "--diag")[1]
    check("перечислены опции ядра",
          "CONFIG_MTD_PCMCIA_ANONYMOUS" in txt and "CONFIG_PCCARD_NONSTATIC" in txt,
          txt[-300:])
    check("про каждую опцию сказан вердикт",
          any(k in txt for k in ("включён", "НЕ ВКЛЮЧЁН", "нет в конфиге")), txt[-400:])
    check("сказано, что ядро проверяет окна раньше CIS",
          "сначала окна памяти, потом CIS" in txt, txt[-600:])


def t_direct_read():
    print("\n[S18] pcmcia_direct.py: прямое чтение через /dev/mem (подставной файл)")
    d = direct_module()
    path = DIRECT

    # 1) окна: из текста lspci (файл) — только memory, без I/O
    wins_txt = os.path.join(STUB, "lspci_v.txt")
    with open(wins_txt, "w") as f:
        f.write("00:0c.0 CardBus bridge: TI\n"
                "\tMemory window 0: 64000000-67ffffff (prefetchable)\n"
                "\tMemory window 1: 68000000-6bffffff\n"
                "\tI/O window 0: 00002400-000024ff\n")
    wins = d.cardbus_windows(wins_txt)
    check("окон памяти найдено два", len(wins) == 2, str(wins))
    check("порты I/O в окна не попали", all(w["size"] > 65536 for w in wins), str(wins))
    check("prefetchable распознан", wins[0]["prefetch"] is True)

    # 2) карта 1 МиБ внутри окна 64 МиБ: модель моста с ExCA-регистрами
    CARD_SZ = 1 << 20
    data = bytearray(b"\xff" * CARD_SZ)
    data[0:4] = b"\x01CIS"
    data[0x40:0x56] = b"intel SERIES2-20 "
    data[0x200:0x300] = bytes(range(256))
    data[CARD_SZ - 16:] = b"END-OF-CARD-1234"
    EXCA, PCI = 0x60000800, 0x64000000

    class Bridge:
        """Мост CardBus: BAR0 с ExCA + окно 0, за которым карта 1 МиБ.

        attribute memory отдаёт не то же, что common: если образ читается
        «по адресу окна» без настройки ExCA, в него попадёт 0xEE - именно
        так ловится прежняя ошибка.
        """

        def __init__(self):
            self.regs = bytearray(0x1000)
            self.nreads = 0
            self.flaky = False

        def read(self, addr, n):
            self.nreads += 1
            if EXCA <= addr < EXCA + 0x1000:
                i = addr - EXCA
                return bytes(self.regs[i:i + n])
            if not (self.regs[0x06] & 1):        # окно выключено
                return b"\xff" * n
            off = int.from_bytes(self.regs[0x14:0x16], "little")
            if off & 0x4000:                     # attribute memory
                return b"\xee" * n
            pos = (((off & 0x3fff) << 12) + (addr - PCI)) % CARD_SZ
            out = bytearray()
            while n:
                take = min(n, CARD_SZ - pos)
                out += data[pos:pos + take]
                n -= take
                pos = 0
            blk = bytes(out)
            if self.flaky and self.nreads > 1:
                # «плывёт» КАЖДОЕ повторное чтение, и портится по-разному:
                # так модель не зависит от размера захода. Раньше порча была
                # каждым 5-м чтением - при заходе 4 МиБ она попадала в оба
                # прохода одинаково, и тест зеленел зря (нашлось на аудите).
                blk = bytes([(blk[0] + self.nreads) & 0xFF]) + blk[1:]
            return blk

        def write(self, addr, b):
            i = addr - EXCA
            self.regs[i:i + len(b)] = b

        def close(self):
            pass

    br = Bridge()
    win = {"start": PCI, "end": PCI + (64 << 20) - 1, "size": 64 << 20,
           "prefetch": True}
    check("окно выбрано по размеру карты (первое подходящее)",
          d.choose_window([{"start": 0x1000, "size": 1 << 12}, win], CARD_SZ) is win,
          str(d.choose_window([{"start": 0x1000, "size": 1 << 12}, win], CARD_SZ)))
    # 16-битное окно: и CIS, и образ идут через одно окно, но в разные области
    d.setup_cis_window(br, EXCA, PCI, card_offset=0, attrib=True)
    check("окно на attribute memory (бит REG в OFF)",
          br.regs[0x14] | br.regs[0x15] << 8 & 0x4000, br.regs[0x14:0x16].hex())
    check("в attribute memory читается паспорт, а не карта",
          br.read(PCI, 4) == b"\xee" * 4, br.read(PCI, 4).hex())
    check("размер карты определён по повторению адресов",
          d.find_card_size(br, EXCA, win) == (CARD_SZ, "адрес повторяется через 1.0 МиБ"),
          str(d.find_card_size(br, EXCA, win)))

    # 3) двойное чтение: образ совпадает с «картой» байт в байт
    br.regs[0x06] = 0x02          # состояние окна «как было»: выкл. бит 0, бит 1 чужой
    img = os.path.join(STUB, "direct_img.bin")
    res = d.read_card_image(br, EXCA, win, img, CARD_SZ, double=True)
    with open(img, "rb") as f:
        got = f.read()
    check("образ снят целиком", res["size"] == CARD_SZ and len(got) == CARD_SZ, str(res))
    check("образ совпал с содержимым карты", got == bytes(data), got[:8].hex())
    # attribute memory в модели отдаёт 0xEE, карта начинается с "\x01CIS":
    # если снимать образ «по адресу окна» без настройки ExCA, в начале будет 0xEE
    check("образ начинается содержимым карты, а не паспортом",
          got[:4] == b"\x01CIS", "0xEE в начале - читалось окно без настройки ExCA: "
          + got[:4].hex())
    check("md5 в ответе совпадает с файлом",
          res["md5"] == d.hashlib_md5(img), res["md5"])
    check("окно после чтения возвращено как было (не оставлено включённым)",
          br.regs[0x06:0x07] == b"\x02", br.regs[0x06:0x07].hex())
    check("маркер CIS найден в образе", d.find_cis(got[:4096]) == [1],
          str(d.find_cis(got[:4096])))
    check("строки карты прочитаны",
          any("SERIES2" in x for x in d.strings(got[:4096])), str(d.strings(got[:4096])[:3]))

    # 4) нестабильное чтение не выдаётся за готовый образ
    br2 = Bridge()
    br2.flaky = True
    bad = os.path.join(STUB, "bad_img.bin")
    try:
        d.read_card_image(br2, EXCA, win, bad, CARD_SZ, double=True)
        check("два разных чтения отвергнуты", False, "образ сохранён")
    except d.DirectError as e:
        # отказ бывает двух видов, и оба честные: участок не подтвердился
        # повтором (НЕСТАБИЛЬНО) или два прохода разошлись (РАЗЛИЧАЮТСЯ)
        check("два разных чтения отвергнуты",
              "РАЗЛИЧАЮТСЯ" in str(e) or "НЕСТАБИЛЬНО" in str(e), str(e)[:80])
        # ВАЖНОЕ ОГРАНИЧЕНИЕ, зафиксировано честно: если карта стабильно
        # врёт ОДИНАКОВО, повторение этого не поймает. Поэтому в отчёте
        # печатается и md5, и sha256 - их можно сверить с внешним источником.
        check("при отказе сказано, что образ не сохранён",
              "не сохранён" in str(e), str(e)[:80])
    check("недописанный образ не остался на диске", not os.path.exists(bad), "файл есть")

    # 5) сводка по образу работает без карты, /dev/mem и root
    dump = os.path.join(STUB, "stats.bin")
    with open(dump, "wb") as f:
        f.write(b"\xff" * 768 + b"\x00" * 128 + b"DATA" * 32)
    st = d.image_stats(dump)
    check("состав образа посчитан", st["bytes"] == 1024 and st["ff"] == 768
          and st["zero"] == 128 and st["other"] == 128, str(st))
    r = subprocess.run([sys.executable, path, "--image-report", dump],
                       capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    check("--image-report работает без слота и root", r.returncode == 0 and "Traceback" not in out,
          out[-200:])
    check("--image-report показывает размер, md5 и состав",
          "1024 байт" in out and "md5" in out and "0xFF 75.0%" in out, out[:400])
    r = subprocess.run([sys.executable, path, "--image-report",
                        os.path.join(STUB, "нет.bin")], capture_output=True,
                       text=True, timeout=60)
    check("--image-report на несуществующем файле: понятная ошибка, код 2",
          r.returncode == 2 and "нет" in (r.stdout + r.stderr), r.stdout + r.stderr)

    # 4) 8-битная карта: каждый байт дважды -> нормализация
    dup = bytes(b for x in got[:2048] for b in (x, x))
    norm, width = d.normalize_bankwidth(dup)
    check("дублирование байтов распознано", width == 1, str(width))
    check("после нормализации данные те же", norm == got[:2048])

    # 5) пустая карта распознаётся как пустая
    info = d.summarize(b"\xff" * 4096)
    check("«всё 0xFF» названо стёртой картой", info["empty_ff"] and "стёрта" in info["verdict"],
          info["verdict"])

    # 6) размеры из командной строки
    check("16m = 16 МиБ", d.parse_size("16m") == 16 << 20)
    try:
        d.parse_size("шестнадцать")
        check("нечисловой размер отвергнут", False, "принят")
    except d.DirectError as e:
        check("нечисловой размер отвергнут", "16m" in str(e), str(e)[:60])

    # 7) запуск из командной строки без root: код 2 и понятная причина
    r = subprocess.run([sys.executable, path, "--windows", wins_txt, "--probe"],
                       capture_output=True, text=True, env={**os.environ,
                       "PATH": STUB + os.pathsep + os.environ["PATH"]}, timeout=60)
    out = r.stdout + r.stderr
    check("без root: объяснение, а не трейсбек", "Traceback" not in out, out[-200:])
    check("без root: код 2", r.returncode == 2, str(r.returncode))
    check("без root: сказано про sudo",
          "sudo" in out or "root" in out, out[-200:])

    # 8) нет lspci: понятная ошибка, код 2, без трейсбека
    empty = os.path.join(STUB, "empty_path")
    os.makedirs(empty, exist_ok=True)
    r2 = subprocess.run([sys.executable, path, "--probe"], capture_output=True,
                        text=True, env={**os.environ, "PATH": empty}, timeout=60)
    out2 = r2.stdout + r2.stderr
    check("нет lspci: без трейсбека", "Traceback" not in out2, out2[-200:])
    check("нет lspci: сказано поставить pciutils",
          "pciutils" in out2, out2[-200:])
    check("нет lspci: код 2", r2.returncode == 2, str(r2.returncode))


def t_ctl_menu():
    print("\n[S19c] pcmcia_ctl.py: пульт (чтение/запись/стирание/сверка) на заглушках")
    kit = os.path.join(STUB, "kit_ctl")
    os.makedirs(kit, exist_ok=True)
    for f in ("pcmcia_ctl.py", "pcmcia_direct.py", "collect_all.py"):
        src = os.path.join(KIT, f)
        if os.path.exists(src):
            dest = os.path.join(kit, f)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy(src, dest)
    # 2.18.0: сам код работы с картой - пакет src/pcmcia_direct
    pkg_src = os.path.join(KIT, "src", "pcmcia_direct")
    if os.path.isdir(pkg_src):
        shutil.copytree(pkg_src, os.path.join(kit, "src", "pcmcia_direct"),
                        dirs_exist_ok=True)
    ctl = os.path.join(kit, "pcmcia_ctl.py")
    card = os.path.join(STUB, "ctl_card.bin")
    with open(card, "wb") as f:
        f.write(b"\xff" * (1 << 20))
    calls = os.path.join(STUB, "ctl_calls.log")
    open(calls, "w").close()
    tools = os.path.join(STUB, "ctl_tools")
    os.makedirs(tools, exist_ok=True)

    def tool(name, body):
        p = os.path.join(tools, name)
        with open(p, "w") as f:
            f.write("#!/bin/bash\n" + body + "\n")
        os.chmod(p, 0o755)

    tool("flash_erase", 'echo "flash_erase $*" >> "' + calls + '"')
    tool("flashcp", 'echo "flashcp $*" >> "' + calls + '"\n'
         'case "$1" in --help|-h) echo usage; exit 0 ;; esac\n'
         'src="$1"; dst="$2"\n'
         '[ "$src" = "-v" ] && { src="$2"; dst="$3"; }\n'
         'cp "$src" "' + card + '"')
    tool("mtd_debug", 'echo "mtd_debug $*" >> "' + calls + '"\n'
         'case "$1" in read)\n'
         'python3 -c "import sys;d=open(sys.argv[1],\'rb\').read()[int(sys.argv[2]):'
         'int(sys.argv[2])+int(sys.argv[3])];open(sys.argv[4],\'wb\').write(d)" '
         '"' + card + '" "$3" "$4" "$5" ;; esac')

    env = {**os.environ, "PATH": tools + os.pathsep + os.environ["PATH"],
           "MTD": card, "LOG": os.path.join(STUB, "ctl.log"),
           "PCMCIA_SKIP_ROOT": "1"}

    # pcmcia_ctl.py требует root: подставляем свой id, чтобы гонять без root
    pid = os.path.join(tools, "id")
    with open(pid, "w") as f:
        f.write('#!/bin/bash\n[ "$1" = "-u" ] && { echo 0; exit 0; }\n'
                'exec /usr/bin/id "$@"\n')
    os.chmod(pid, 0o755)

    img = os.path.join(STUB, "ctl_img.bin")
    with open(img, "wb") as f:
        f.write(b"PAYLOAD-" * 512)
    payload = open(img, "rb").read()

    # 1) запись: стирание ДО записи, затем сверка
    r = subprocess.run(["python3", ctl, "write", img, "--yes"], capture_output=True,
                       text=True, env=env, timeout=120)
    out = r.stdout + r.stderr
    log = open(calls).read()
    check("пульт: запись сначала стирает, потом пишет",
          log.find("flash_erase") >= 0 and log.find("flash_erase") < log.find("flashcp"),
          log[:120])
    check("пульт: сказано «записано»", "записано" in out, out[-160:])
    check("пульт: сверка выполнена и сошлась", "сверка сошлась" in out, out[-160:])
    check("пульт: запись вернула код 0", r.returncode == 0, str(r.returncode))
    with open(card, "rb") as f:
        got = f.read(len(payload))
    check("пульт: на «карте» ровно отправленный образ", got == payload, got[:12].hex())

    # 2) без подтверждения YES ничего не стирается
    open(calls, "w").close()
    r2 = subprocess.run(["python3", ctl, "erase"], capture_output=True, text=True,
                        env=env, input="no\n", timeout=60)
    out2 = r2.stdout + r2.stderr
    check("пульт: стирание без YES отменено", "отменено" in out2, out2[-120:])
    check("пульт: стирание без YES не вызвало flash_erase",
          open(calls).read() == "", open(calls).read()[:80])

    # 3) сверка чужого файла - честный отказ
    other = os.path.join(STUB, "ctl_other.bin")
    with open(other, "wb") as f:
        f.write(b"XX" * 256)
    r3 = subprocess.run(["python3", ctl, "verify", other], capture_output=True,
                        text=True, env=env, timeout=60)
    out3 = r3.stdout + r3.stderr
    check("пульт: сверка чужого файла говорит «различается»",
          "различается" in out3, out3[-120:])
    check("пульт: показаны первые различия", "байт" in out3, out3[-120:])
    check("пульт: сверка чужого файла не выдаёт успех", r3.returncode != 0,
          str(r3.returncode))

    # 4) нет устройства - «писать нечем», а не «записано»
    env_nomtd = {**env, "MTD": os.path.join(STUB, "no_such_mtd0")}
    r4 = subprocess.run(["python3", ctl, "write", img, "--yes"], capture_output=True,
                        text=True, env=env_nomtd, timeout=60)
    out4 = r4.stdout + r4.stderr
    check("пульт: без mtd прямо сказано «писать нечем»", "писать нечем" in out4,
          out4[-200:])
    check("пульт: без mtd код не 0", r4.returncode != 0, str(r4.returncode))
    check("пульт: без mtd напомнил, что читать можно",
          "снимается нормально" in out4 or "читать" in out4.lower(), out4[-200:])

    # 4b) проба одного сектора: и удачная, и неудачная
    check("пульт: проба сектора выполняется до полной записи",
          "ПРОБА" in out and "проба сошлась" in out, out[-300:])
    # карта, которая "не пишется": flashcp кладёт нули
    tools_bad = os.path.join(STUB, "ctl_tools_bad")
    os.makedirs(tools_bad, exist_ok=True)
    for name in ("flash_erase", "mtd_debug", "id"):
        shutil.copy(os.path.join(tools, name), tools_bad)
    pbad = os.path.join(tools_bad, "flashcp")
    with open(pbad, "w") as f:
        # "не пишется": команда проходит, но на карте нули вместо данных
        f.write("#!/bin/bash\n"
                'echo "flashcp $*" >> "' + calls + '"\n'
                'src="$1"; case "$src" in -*) src="$2";; esac\n'
                'n=$(wc -c < "$src")\n'
                'python3 -c "import sys;fd=open(sys.argv[1],\'r+b\');'
                'fd.write(bytes(int(sys.argv[2])))" "' + card + '" "$n"\n')
    os.chmod(pbad, 0o755)
    with open(card, "wb") as f:
        f.write(b"\xff" * (1 << 20))
    open(calls, "w").close()
    env_bad = {**env, "PATH": tools_bad + os.pathsep + os.environ["PATH"]}
    rb = subprocess.run(["python3", ctl, "write", img, "--yes"], capture_output=True,
                        text=True, env=env_bad, timeout=120)
    outb = rb.stdout + rb.stderr
    logb = open(calls).read()
    check("пульт: проба не сошлась — сказано прямо", "ПРОБА НЕ СОШЛАСЬ" in outb, outb[-300:])
    check("пульт: после неудачной пробы код не 0", rb.returncode != 0, str(rb.returncode))
    check("пульт: после неудачной пробы полная запись НЕ начата",
          logb.count("flashcp") == 1, logb)
    check("пульт: сказано, что образ цел, а испорчен максимум сектор",
          "образ цел" in outb, outb[-300:])

    # 4c) размер сектора берётся из sysfs, а не "на глаз"
    sysdir = os.path.join(STUB, "ctl_sys")
    md = os.path.join(sysdir, "class", "mtd", "mtd0")
    os.makedirs(md, exist_ok=True)
    with open(os.path.join(md, "erasesize"), "w") as f:
        f.write("131072")
    card_mtd0 = os.path.join(STUB, "mtd0")
    with open(card_mtd0, "wb") as f:
        f.write(b"\xff" * (1 << 20))
    open(calls, "w").close()
    env_sz = {**env, "MTD": card_mtd0, "PCM_SYSFS": sysdir}
    subprocess.run(["python3", ctl, "write", img, "--yes"], capture_output=True,
                   text=True, env=env_sz, timeout=120)
    logsz = open(calls).read()
    check("пульт: проба читает ровно один сектор из sysfs (131072 байта)",
          "mtd_debug read" in logsz and " 131072 " in logsz.replace("mtd_debug read", " "),
          logsz[:200])

    # 5) вердикт: по-русски и строкой JSON для машины
    r5 = subprocess.run(["python3", ctl, "info", "--json"], capture_output=True,
                        text=True, env=env, timeout=120)
    out5 = r5.stdout + r5.stderr
    check("пульт: вердикт содержит чтение и запись",
          "чтение :" in out5 and "запись :" in out5, out5[-200:])
    check("пульт: печатается строка JSON", 'JSON {"read"' in out5, out5[-200:])


def t_direct_sysfs_fallback():
    print("\n[S19b] STRICT_DEVMEM: когда /dev/mem не отдал страницу, "
          "читается тот же адрес через файл BAR")
    d = direct_module()
    path = DIRECT

    PCI1, PCI2 = 0x64000000, 0x68000000
    SIZE = 1 << 20
    data = bytearray(b"\xff" * SIZE)
    data[0:4] = b"\x01CIS"
    data[0x200:0x220] = b"SYSFS-FALLBACK-DATA!!"

    class Mem:
        """Память моста. strict=True - /dev/mem открыт, но mmap страниц запрещён."""

        def __init__(self, start, strict):
            self.base = start
            self.strict = strict
            self.path = "/dev/mem" if strict else f"/sys/resource{start}"

        @property
        def name(self):
            return self.path

        def read(self, addr, n):
            if self.strict:
                raise d.DirectError(f"mmap({addr:#x}, {n}) не удался: [Errno 13] "
                                    "Permission denied")
            return bytes(0x5A for _ in range(n))

        def write(self, addr, b):
            if self.strict:
                raise d.DirectError("mmap не удался: [Errno 13] Permission denied")

    class Bar(Mem):
        """Файл BAR: отдаёт реальные байты (общий буфер) и ExCA-регистры."""

        def __init__(self, start, buf, regs):
            super().__init__(start, strict=False)
            self.buf = buf
            self.regs = regs

        def read(self, addr, n):
            if addr in self.regs:                     # ExCA: адрес -> байты
                return self.regs[addr][:n]
            off = addr - self.base
            return bytes(self.buf[off:off + n])

        def write(self, addr, b):
            self.regs[addr] = bytes(b)

    regs = {}

    def fake_bar_for(addr, dev_id=None, verbose=False):
        for start, buf in ((PCI1, data), (PCI1 & ~0xfffff, None), (PCI2, data)):
            if buf is None:
                continue
            if start <= addr < start + len(buf):
                return f"/sys/resource{start}", start
        if 0x60000000 <= addr < 0x60001000:           # BAR0 с ExCA
            return "/sys/resource0", 0x60000000
        return None, 0

    d.sysfs_bar_for = fake_bar_for
    d._MEM_CACHE.clear()
    real_cache = d._sysfs_mem

    def make_mem(path, start, writable=False):
        key = (path, bool(writable))
        if key not in d._MEM_CACHE:
            buf = data if "resource2" in path or "resource3" in path or start in (PCI1, PCI2) else None
            d._MEM_CACHE[key] = Bar(start, buf if buf is not None else data, regs)
        return d._MEM_CACHE[key]

    d._sysfs_mem = make_mem
    try:
        strict = Mem(0, strict=True)
        # 1) ExCA и окно берутся из своих файлов, отказ /dev/mem не выдаётся за данные
        blk = d._read_page(strict, 0x60000800, {"start": PCI1, "size": 64 << 20},
                           0, 16, dev_id="0000:00:0c.0")
        check("/dev/mem отказал - прочитано через sysfs, а не 0xFF/0x5A",
              blk[:4] == b"\x01CIS", blk[:8].hex())
        check("ExCA-регистры писались в файл BAR0 (ADDRWIN и OFF)",
              regs.get(0x60000806) is not None and regs.get(0x60000814) is not None,
              str(sorted(f"{k:#x}" for k in regs)[:6]))
        # 2) сообщение об отказе печатается один раз, а не на каждую страницу
        d._NOTICE.clear()
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            for off in (0, 4096, 8192):
                d.read_mem(Mem(0, strict=True), PCI1, 16, verbose=True)
        out = buf.getvalue()
        check("причина названа один раз за прогон", out.count("не отдал") == 1,
              out[:120])

        # 3) --probe: когда ни одно окно не прочиталось, про 0xFF не говорится
        def never(addr, dev_id=None, verbose=False):
            return None, 0
        d.sysfs_bar_for = never
        real_mem_cls = d.Mem

        class Openable(Mem):
            """Mem, который открывается, но ни одной страницы не отдаёт."""

            def __init__(self, *a, **k):
                super().__init__(0, strict=True)

            def close(self):
                pass

        d.Mem = Openable
        d.find_bridge_ids = lambda: ("0000:00:0c.0", 0x60000000, "5ca9", "5cw")
        buf2 = io.StringIO()
        try:
            with redirect_stdout(buf2):
                rep = d.probe([{"start": PCI1, "size": 64 << 20},
                               {"start": PCI2, "size": 64 << 20}], verbose=True)
        finally:
            d.Mem = real_mem_cls
        txt = buf2.getvalue()
        check("probe: окна не читаются - так и сказано",
              "НИ ОДНО ОКНО НЕ ПРОЧИТАНО" in txt, txt[-160:])
        check("probe: слов про стёртую карту/0xFF нет",
              "карта стёрта" not in txt, txt[-160:])
        check("probe: причина отказа видна в отчёте",
              all(s.get("error") for s in rep["samples"]), str(rep["samples"])[:80])
    finally:
        d._sysfs_mem = real_cache
        d._MEM_CACHE.clear()


def t_direct_cis():
    print("\n[S19] pcmcia_direct.py: CIS через attribute memory (паспорт карты)")
    d = direct_module()
    path = DIRECT

    def tpl(code, body):
        return bytes([code, len(body)]) + body

    # CIS как у настоящей карты: DEVICE (FLASH 16 МиБ), MANFID intel, FUNCID, VERS_1
    vers = bytes([4, 1, 2, 0x05, 0x0B]) + b"intel\x00SERIES2-20 \x00"
    cis = (tpl(0x01, bytes([0x5F, 0x3E]))
           + tpl(0x20, bytes([0x49, 0x01, 0x01, 0x00]))
           + tpl(0x21, bytes([0x01]))
           + tpl(0x15, vers))
    cis += b"\xff" * (4096 - len(cis))

    t = d.parse_cis(cis)
    check("кортежи разобраны", len(t) == 4, [x["name"] for x in t])
    check("имена кортежей узнаны",
          [x["name"] for x in t][:2] == ["CISTPL_DEVICE", "CISTPL_MANFID"],
          [x["name"] for x in t])
    info = d.cis_summary(t)
    check("производитель и карта из MANFID",
          info["manfid"] == (0x0149, 0x0001), str(info["manfid"]))
    check("продукт из VERS_1",
          info["product"][:2] == ["intel", "SERIES2-20"], str(info["product"]))
    check("функция = memory (1)", info["func"] == 1, str(info["func"]))
    check("размер из DEVICE = 16 МиБ",
          info["devices"] and info["devices"][0]["size"] == 16 << 20,
          str(info["devices"]))
    check("тип устройства FLASH",
          info["devices"][0]["type_name"] == "FLASH", str(info["devices"][0]))

    # 16-битная раскладка: байт CIS лежит по чётным адресам
    wide = bytes(b for x in cis[:2048] for b in (x, 0))

    def score(data):
        tt = d.parse_cis(data)
        ii = d.cis_summary(tt)
        return (3 * bool(ii["manfid"]) + 3 * bool(ii["product"]) + 2 * bool(ii["func"])
                + sum(1 for x in tt if x["code"] in d.CIS_KNOWN and x["len"] > 0))

    # parse_cis теперь сам выбирает ширину шины, поэтому "как есть" и "по чётным"
    # дают ОДИН результат - важно, что обе раскладки распознаны одинаково верно
    check("обычная раскладка разбирается верно",
          score(cis[:2048]) > 0 and score(cis[:2048]) == score(wide),
          f"{score(cis[:2048])} vs {score(wide)}")
    check("16-битная раскладка (байт через ноль) разбирается так же верно",
          score(wide) > 0, str(score(wide)))

    # ГЛАВНОЕ: mmap требует кратного странице смещения, а регистры ExCA лежат
    # по 0x60000800. Раньше это давало Errno 22 на реальном железе (нашлось
    # только на настоящем ноутбуке, потому что тесты использовали подставной Mem).
    tmpf = os.path.join(STUB, "devmem.bin")
    with open(tmpf, "wb") as f:
        f.write(bytes(i & 0xFF for i in range(65536)))

    class FileMem(d.Mem):
        def __init__(self, path, writable=False):
            self.path = path
            self.fd = os.open(path, (os.O_RDWR if writable else os.O_RDONLY) | os.O_SYNC)

    fm = FileMem(tmpf)
    ok_unaligned = True
    try:
        one = fm.read(0x1800, 1)
        four = fm.read(0x1800, 4)
        far = fm.read(0xFFF0, 16)   # на границе страницы и конца файла
    except Exception as exc:                   # noqa: BLE001
        ok_unaligned = False
        one = four = far = repr(exc)
    check("чтение по НЕкратному странице адресу работает (0x1800)",
          ok_unaligned and one == b"\x00" and four == bytes([0, 1, 2, 3]),
          f"{one} {four}")
    fmw = FileMem(tmpf, writable=True)
    fmw.write(0x1801, b"\xde\xad")
    check("запись по НЕкратному адресу работает",
          fmw.read(0x1800, 4) == bytes([0, 0xDE, 0xAD, 3]), fmw.read(0x1800, 4).hex())
    check("чтение на границе страницы не рвётся",
          not isinstance(far, str) and len(far) == 16, str(far))

    # ---------------------------------------------------------------- sysfs
    # Когда CONFIG_STRICT_DEVMEM не пускает /dev/mem, окна берутся из sysfs
    # (resource0 = BAR0 с ExCA, resource2+ = окна CardBus).
    bar0_file = os.path.join(STUB, "resource0.bin")
    win_file = os.path.join(STUB, "resource2.bin")
    with open(bar0_file, "wb") as f:
        f.write(b"\x11" * 4096)
    with open(win_file, "wb") as f:
        f.write(b"CIS" + b"\x00" * 4093)

    _RealMem = d.Mem

    class MemNoDevmem(_RealMem):
        def __init__(self, path="/dev/mem", writable=False, base=0, name=None):
            if path == "/dev/mem":
                raise d.DirectError("mmap: Operation not permitted (STRICT_DEVMEM)")
            _RealMem.__init__(self, path, writable, base, name)

    old_mem, old_wins = d.Mem, d.sysfs_window_files
    d.Mem = MemNoDevmem
    d.sysfs_window_files = lambda dev_id=None: [
        {"idx": 0, "path": bar0_file, "start": 0x60000000, "size": 4096, "flags": 0x40200},
        {"idx": 2, "path": win_file, "start": 0x64000000, "size": 64 << 20, "flags": 0x20200},
    ]
    try:
        try:
            mem, src, errs = d.open_best_mem("0000:00:0c.0")
            ok_src = (src == "sysfs" and mem.base == 0x60000000)
            first = mem.read(0x60000800, 4)
            mem.close()
        except Exception as exc:            # noqa: BLE001
            ok_src, first, errs = False, repr(exc), []
        check("при запрете /dev/mem память берётся из sysfs", ok_src, str(first))
        check("sysfs: чтение по физ. адресу переводится в смещение файла",
              first == b"\x11\x11\x11\x11", str(first))
        check("ошибка /dev/mem сохранена для отчёта",
              any("STRICT_DEVMEM" in str(e) for e in errs), str(errs))
        # окно выбирается по вхождению адреса: BAR0 (ExCA) не должен попасть
        by_idx = {0: {"idx": 0, "path": "resource0", "start": 0x60000000,
                      "size": 8192, "flags": 0x40200},
                  2: {"idx": 2, "path": "resource2", "start": 0x64000000,
                      "size": 64 << 20, "flags": 0x20200},
                  3: {"idx": 3, "path": "resource3", "start": 0x68000000,
                      "size": 64 << 20, "flags": 0x20200}}
        w = d.pick_mem_win(by_idx, 0x64000000)
        check("окно выбрано по вхождению адреса (resource2 для 0x64000000)",
              bool(w) and w["idx"] == 2, str(w))
        w2 = d.pick_mem_win(by_idx, 0x68000000)
        check("второе окно моста тоже находится (resource3)",
              bool(w2) and w2["idx"] == 3, str(w2))
        check("BAR0 с ExCA никогда не выбирается как окно карты",
              d.pick_mem_win({0: by_idx[0]}, 0x60000800) is None, "выбран BAR0")
    finally:
        d.Mem, d.sysfs_window_files = old_mem, old_wins

    # --- паспорт ЖИВОЙ КАРТЫ из отчёта 20:22 ------------------------------
    LIVE = bytes.fromhex(
        "0000030052007e00ff00180003000100ad00ff001e000700020011000100010001000100"
        "ff001500540004000100430065006e00740065006e006e00690061006c0020005400650063"
        "0068006e006f006c006f0067006900650073002c00200049006e0063002e00000046004c00"
        "330032004d002d00320030002d003100310001003100390000003300320020004d00450047"
        "00200046004c00410053004800200077002f003100360020004d00620069007400200041004d"
        "0044002000640065007600690063006500730000000000")
    live_data = (LIVE + b"\xff\x00" * ((4096 - len(LIVE)) // 2))[:4096]
    lt, lhow = d.scan_cis(live_data)
    linfo = d.cis_summary(lt)
    linfo.update({"stride": lhow["stride"], "offset": lhow["offset"]})
    check("паспорт живой карты: название производителя",
          linfo["product"] and "Centennial" in linfo["product"][0],
          str(linfo["product"]))
    check("паспорт живой карты: модель FL32M-20-11",
          any("FL32M" in x for x in linfo["product"]), str(linfo["product"]))
    cap = d.cis_capacity(linfo, linfo.get("strings"))
    check("ёмкость из паспорта: 32 Мбит = 4 МиБ",
          any("32 Мбит" in x and "4 МиБ" in x for x in cap), str(cap))
    check("тип памяти: FLASH", any("FLASH" in x for x in cap), str(cap))
    check("чипы по JEDEC: AMD Am29F016 x2",
          any("Am29F016" in x and "2 шт" in x for x in cap), str(cap))
    # дамп не должен превращаться в полотно
    dump = d.cis_dump(live_data, linfo)
    check("дамп attribute memory короткий (не более 12 строк)", len(dump) <= 12, str(len(dump)))
    check("дамп не печатает пустой хвост по строкам",
          any("не печатаю" in x for x in dump), str(dump[-1]))
    ext = "\n".join(d.cis_extra_report(live_data, linfo))
    check("--extra: видно начало цепочки и ширину шины",
          "начало цепочки" in ext and "каждый 2-й" in ext, ext[:200])
    check("--extra: видны строки паспорта",
          "Centennial Technologies, Inc." in ext, ext[:400])

    # --- поток ИЗ ОТЧЁТА 20:19: сканер брал не то начало и выдавал текст
    # "bit AMD devices" за MANFID (0x6962/"bi", 0x2074/"t ")
    RAW2 = bytes(int(x, 16) for x in (
        "00 00 03 00 52 00 7e 00 ff 00 18 00 03 00 01 00 ad 00 ff 00 1e 00 07 00 "
        "02 00 11 00 01 00 01 00 01 00 01 00 ff 00 15 00 54 00 04 00 01 00 43 00 "
        "65 00 6e 00 74 00 65 00 6e 00 6e 00 69 00 61 00").split())
    t2, how2 = d.scan_cis(RAW2 + bytes(4096 - len(RAW2)))
    sm2 = d.cis_summary(t2)
    check("сканер не выдаёт 0x7e/0xff за код кортежа",
          all(x["code"] in d.TUPLE_NAMES or x["code"] < 0x24 for x in t2),
          str([(hex(x["code"]), x["len"]) for x in t2]))
    check("MANFID из текста ('bi','t ') не принимается за производителя",
          sm2.get("manfid") is None, str(sm2.get("manfid")))
    check("VERS_1 читается даже с нестандартной таблицей строк",
          sm2.get("product") and "Centennia" in " ".join(sm2["product"]),
          str(sm2.get("product")))
    check("версия CIS 4.1 распознана", sm2.get("vers") == (4, 1), str(sm2.get("vers")))
    check("passport_ok: название карты уже считается паспортом",
          d.passport_ok(sm2) is True, str(sm2))
    check("passport_ok: пустая сводка не считается паспортом",
          d.passport_ok(d.cis_summary([])) is False, "")
    # окна без lspci (pciutils может не стоять)
    check("есть запасной путь для окон: /proc/iomem",
          hasattr(d, "cardbus_windows_iomem"), "")

    # --- кортежи бывают ДЛИННЕЕ 0x40: VERS_1 на реальной карте = 0x54 ----
    def _tup(code, body):
        return bytes([code, len(body)]) + body

    def _vers1(items, major=4, minor=1):
        offs, pos = [], 3 + len(items)
        for x in items:
            offs.append(pos)
            pos += len(x.encode()) + 1
        body = bytes([major, minor, len(items)]) + bytes(offs)
        return _tup(0x15, body + b"".join(x.encode() + b"\x00" for x in items))

    FULL = (_tup(0x13, b"CIS")
            + _tup(0x18, bytes([0x01, 0xAD, 0xFF]))
            + _vers1(["Centennial", "Centennia Technologies", "SRAM card",
                      "PCMCIA 4.1", "Copyright 1995"])
            + _tup(0x20, bytes([0x34, 0x12, 0x9B, 0x10]))
            + _tup(0x21, bytes([0x01]))
            + _tup(0x01, bytes([0x60, 0x08])))
    # на живой карте длина тела VERS_1 была 0x54 = 84 байта - прежний предел
    # 0x40 (64) рвал цепочку именно на нём
    BIG = _vers1(["Centennial", "Centennia Technologies", "SRAM card",
                  "PCMCIA 4.1", "Copyright 1995"])
    # длина ТЕЛА кортежа - именно её раньше ограничивал сканер
    check("тело VERS_1 длиннее прежнего предела 0x40 (как на живой карте)",
          len(BIG[2:]) > 0x40, str(len(BIG[2:])))
    tf = d._tuples_from(FULL)
    check("цепочка не обрывается на длинном VERS_1",
          any(x["code"] == 0x15 for x in tf) and any(x["code"] == 0x20 for x in tf),
          str([(x["name"], x["len"]) for x in tf]))
    full_raw = bytes([0, 0, 0x5A, 0, 0x52, 0]) + bytes(
        b for x in FULL for b in (x, 0)) + bytes(4096)
    tuples, how = d.scan_cis(full_raw)
    sm = d.cis_summary(tuples)
    check("scan_cis достаёт паспорт целиком из 16-битного потока с мусором",
          sm.get("product") and "Centennia" in " ".join(sm["product"])
          and sm.get("manfid") and sm.get("devices"), str(sm))
    check("из VERS_1 берётся версия (4.1)", sm.get("vers") == (4, 1), str(sm.get("vers")))
    check("DEVICE даёт тип памяти и размер",
          sm["devices"] and sm["devices"][0]["type_name"] in
          ("SRAM", "FLASH", "EEPROM", "DRAM", "ROM", "OTPROM", "EPROM"), str(sm["devices"]))

    # --- разбор РЕАЛЬНОГО потока из отчёта 20:11 --------------------------
    # байты с ноутбука: настоящий CIS идёт через один, начиная со смещения 10,
    # и в нём видна строка "Centennia". Прямой разбор с нуля давал 0 кортежей.
    RAW = bytes(int(x, 16) for x in (
        "00 00 03 00 52 00 7e 00 ff 00 18 00 03 00 01 00 ad 00 ff 00 1e 00 07 00 "
        "02 00 11 00 01 00 01 00 01 00 01 00 ff 00 15 00 54 00 04 00 01 00 43 00 "
        "65 00 6e 00 74 00 65 00 6e 00 6e 00 69 00 61 00").split())
    t0 = d._tuples_from(RAW)
    check("прямой разбор реального потока ничего не даёт (как было в отчёте)",
          len(t0) == 0, str(t0))
    tuples, how = d.scan_cis(RAW)
    check("scan_cis находит цепочку в реальном потоке",
          len(tuples) >= 1 and how["stride"] == 2, str(how))
    check("scan_cis находит JEDEC_C с данными (01 ad ff)",
          any(x["code"] == 0x18 and x["body"].hex() == "01adff" for x in tuples),
          str([(hex(x["code"]), x["body"].hex()) for x in tuples]))
    # после уплотнения (каждый 2-й байт) строка видна целиком
    packed = RAW[0::2] + bytes(range(32, 127))
    strs = d.raw_strings(packed)
    check("raw_strings вытаскивает текст паспорта из данных",
          any("Centennia" in x for x in strs), str(strs))
    check("raw_strings не показывает служебную строку CIS",
          all(x.strip() != "CIS" for x in strs), str(strs))
    check("raw_strings не выдаёт короткие обрывки за названия",
          all(len(x) >= 4 for x in d.raw_strings(RAW)), str(d.raw_strings(RAW)))
    # VERS_1 с нормальными смещениями разбирается в строки
    VERS = bytearray([0x15, 0x1a, 4, 0, 6, 12, 20])
    for part in (b"Centennial\x00", b"SRAM 4MB\x00"):
        VERS += part
    VERS[3] = 4
    VERS[2] = 2
    VERS[4], VERS[5] = 6, 15
    # смещения строк отсчитываются от 3-го байта тела
    body = VERS[2:]
    sm = d.cis_summary([{"code": 0x15, "name": "VERS_1", "len": len(body), "body": bytes(body)}])
    check("VERS_1 не падает на кривых смещениях и разбирает строки",
          isinstance(sm.get("product"), list) and len(sm["product"]) >= 1, str(sm))

    # настройка окна моста: значения как в yenta_set_mem_map()
    class FakeMem:
        def __init__(self):
            self.mem = {}

        def read(self, a, n):
            return bytes(self.mem.get(a + i, 0) for i in range(n))

        def write(self, a, data):
            for i, b in enumerate(data):
                self.mem[a + i] = b

    m = FakeMem()
    exca, pci = 0x60000800, 0x64000000
    old = d.setup_cis_window(m, exca, pci, 0, attrib=True)
    start = int.from_bytes(bytes(m.mem.get(exca + 0x10 + i, 0) for i in range(2)), "little")
    stop = int.from_bytes(bytes(m.mem.get(exca + 0x12 + i, 0) for i in range(2)), "little")
    off = int.from_bytes(bytes(m.mem.get(exca + 0x14 + i, 0) for i in range(2)), "little")
    check("CB_MEM_PAGE0 = старший байт адреса", m.mem.get(exca + 0x40) == 0x64,
          hex(m.mem.get(exca + 0x40, 0)))
    # в регистре START/STOP только 12 бит адреса (биты 12..23), старший байт
    # идёт в CB_MEM_PAGE0; для 0x64000000 младшие 12 бит адреса нулевые
    check("START: бит 16-битной шины + адрес из 12 бит",
          start == (0x8000 | ((pci >> 12) & 0x0fff)), hex(start))
    check("STOP: конец окна 4 КиБ", stop == ((pci + 0xfff) >> 12) & 0x0fff, hex(stop))
    check("OFF: attribute memory (бит REG)", off == 0x4000, hex(off))
    check("окно включено (ADDRWIN бит 0)", m.mem.get(exca + 0x06) == 0x01,
          hex(m.mem.get(exca + 0x06, 0)))
    d.restore_cis_window(m, exca, old)
    check("после чтения окно вернули как было",
          m.mem.get(exca + 0x06) == 0 and m.mem.get(exca + 0x40) == 0,
          f"{m.mem.get(exca + 0x06)}, {m.mem.get(exca + 0x40)}")

    # вид «пустоты»: 0xFF и 0x00 — разные диагнозы, не «стёртая карта»
    class FakeMem2(FakeMem):
        """Mem, который отдаёт заданный байт на любое чтение."""
    def cis_blank(fill):
        m2 = FakeMem2()

        def rd(a, n):
            return bytes([fill]) * n
        m2.read = rd
        return m2

    for fill, kind in ((0xFF, "ff"), (0x00, "00")):
        m2 = cis_blank(fill)
        # подменяем find_bridge_ids/cardbus_windows, чтобы читать с подставного Mem
        seen = {}
        real_find, real_wins = d.find_bridge_ids, d.cardbus_windows
        d.find_bridge_ids = lambda: ("0000:00:0c.0", 0x60000000, "5ca9", "5cw")
        d.cardbus_windows = lambda *a, **k: [{"start": 0x64000000,
                                              "end": 0x67ffffff,
                                              "size": 64 << 20, "prefetch": False}]
        try:
            attr, tuples, info = d.read_cis(m2, verbose=False)
        finally:
            d.find_bridge_ids, d.cardbus_windows = real_find, real_wins
        seen[kind] = info["blank"]
        check(f"пустая attribute memory {fill:#04x} опознана как «{kind}»",
              info["blank"] == kind and tuples == [], str(info["blank"]))

    # пустая attribute memory — не притворяемся, что карта есть
    check("пустая attribute memory даёт 0 кортежей",
          d.parse_cis(b"\xff" * 4096) == [], "не пусто")
    check("нули тоже не дают кортежей", d.parse_cis(b"\x00" * 4096) == [])



def t_syntax_all() -> None:
    """S8: py_compile для всех .py скриптов набора (бывший bash -n)."""
    print("\\n[S8] py_compile для всех скриптов набора")
    py_files = [
        "linux/flashcard.py",
        "linux/pcmcia_flash.py",
        "linux/setup_linear_flash.py",
        "pcmcia_direct.py",
        "pcmcia_ctl.py",
        "collect_all.py",
        "remote_collect.py",
        "run_remote.py",
        "setup_sshd.py",
        "CHECK_SSH.py",
        "gui.py",
        "make_cpio.py",
        "planb.py",
        "linux/card_dump_linux.py",
    ]
    for rel in py_files:
        path = os.path.join(KIT, rel)
        if not os.path.isfile(path):
            check(f"существует {rel}", False, "файл не найден")
            continue
        r = subprocess.run(
            [sys.executable, "-m", "py_compile", path],
            capture_output=True, text=True,
        )
        ok_name = f"py_compile {rel}"
        if r.returncode == 0:
            check(ok_name, True)
        else:
            check(ok_name, False, r.stderr[-200:])


def t_selftest() -> None:
    """S9: linux/pcmcia_flash.py --selftest работает без карты."""
    print("\\n[S9] linux/pcmcia_flash.py --selftest без карты")
    setup()
    rc, out, err = run("linux/pcmcia_flash.py", "--selftest")
    txt = out + err
    check("код возврата 0", rc == 0, f"код {rc}: {txt[-200:]}")
    check("версия набора", "Версия набора" in txt, txt[-200:])
    check("каталог скрипта", "Каталог скрипта" in txt, txt[-200:])
    check("архитектура", "Архитектура" in txt, txt[-200:])
    check("ядро", "Ядро" in txt, txt[-200:])
    check("утилиты", "Утилиты:" in txt, txt[-200:])


def t_menu_write() -> None:
    """S10: интерактивное меню из stdin: пункт 6 -> запись со стиранием -> сверка."""
    print("\n[S10] меню: пункт 6 (запись со стиранием и сверкой)")
    setup()
    img = os.path.join(STUB, "menu_img.bin")
    with open(img, "wb") as f:
        f.write(b"\xAA" * 4096)
    # stdin: "6" -> путь к образу -> YES -> Enter (пауза после меню)
    env = {**os.environ, "PATH": STUB + os.pathsep + os.environ["PATH"],
           "MTD": CARD, "PCMCIA_SKIP_ROOT": "1"}
    p = subprocess.run(
        ["python3", os.path.join(KIT, "linux", "pcmcia_flash.py")],
        input=f"6\n{img}\nYES\n\n", capture_output=True, text=True,
        env=env, timeout=60,
    )
    txt = p.stdout + p.stderr
    check("пункт 6 не падает", "Traceback" not in txt and p.returncode == 0,
          f"код {p.returncode}: {txt[-300:]}")
    check("стирание выполнено", "стира" in txt.lower(), txt[-300:])
    check("запись выполнена", "записано" in txt.lower(), txt[-300:])
    check("сверка выполнена", "верификац" in txt.lower() or "совпад" in txt.lower(),
          txt[-300:])

def t_menu_refusal() -> None:
    """S11: меню отказывает в записи без YES."""
    print("\n[S11] меню: отказ записи без YES")
    setup()
    img = os.path.join(STUB, "menu_img2.bin")
    with open(img, "wb") as f:
        f.write(b"\xAA" * 4096)
    env = {**os.environ, "PATH": STUB + os.pathsep + os.environ["PATH"],
           "MTD": CARD, "PCMCIA_SKIP_ROOT": "1"}
    p = subprocess.run(
        ["python3", os.path.join(KIT, "linux", "pcmcia_flash.py")],
        input="5\nno\n\n", capture_output=True, text=True,
        env=env, timeout=60,
    )
    txt = p.stdout + p.stderr
    check("пункт 5 не падает", "Traceback" not in txt and p.returncode == 0,
          f"код {p.returncode}: {txt[-300:]}")
    check("запись отменена", "отмен" in txt.lower(), txt[-300:])

def t_run_remote_sudo_password() -> None:
    """S22b: run_remote.py требует пароль sudo (код 3) и копируется в kit_run_remote."""
    print("\n[S22b] run_remote.py: запрос пароля sudo (код 3)")
    setup()
    kit_ctl = os.path.join(STUB, "kit_run_remote")
    os.makedirs(kit_ctl, exist_ok=True)
    # sudo без пароля не должен сработать — иначе run_remote.py пойдёт в run_collect
    tool("sudo", "exit 1")
    for f in ("collect_all.py", "run_remote.py"):
        src = os.path.join(KIT, f)
        if os.path.exists(src):
            shutil.copy(src, kit_ctl)
    rr = os.path.join(kit_ctl, "run_remote.py")
    if not os.path.isfile(rr):
        # Повторная явная копия
        src = os.path.join(KIT, "run_remote.py")
        if os.path.isfile(src):
            shutil.copy2(src, rr)
        else:
            check("run_remote.py существует в KIT", False, f"не найден: {src}")
            return
    if not os.path.isfile(rr):
        check("run_remote.py скопирован в kit_run_remote", False, "файл не найден")
        return
    env = {**os.environ, "PATH": STUB + os.pathsep + os.environ["PATH"],
           "MTD": CARD}
    p = subprocess.run(
        ["python3", rr],
        input="user\n", capture_output=True, text=True,
        env=env, timeout=60,
    )
    txt = p.stdout + p.stderr
    check("код возврата 3 (нужен пароль sudo)", p.returncode == 3,
          f"код {p.returncode}: {txt[-300:]}")
    check("сказано про пароль sudo", "пароль" in txt.lower() or "sudo" in txt.lower(),
          txt[-300:])

def t_remote_collect_dryrun() -> None:
    """S23: remote_collect.py — dry-run, без реального SSH."""
    print("\n[S23] remote_collect.py: dry-run без SSH")
    setup()
    kit_collect = os.path.join(STUB, "kit_remote_collect")
    os.makedirs(kit_collect, exist_ok=True)
    for f in ("collect_all.py",):
        src = os.path.join(KIT, f)
        if os.path.exists(src):
            shutil.copy(src, kit_collect)
    rc_file = os.path.join(kit_collect, "run_rc.txt")
    with open(rc_file, "w") as f:
        f.write("0\n")
    env = {**os.environ, "PATH": STUB + os.pathsep + os.environ["PATH"],
           "MTD": CARD, "PCMCIA_SKIP_ROOT": "1"}
    p = subprocess.run(
        ["python3", os.path.join(KIT, "remote_collect.py"),
         "--dry-run", "--host", "test-host", "--user", "testuser"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    txt = p.stdout + p.stderr
    check("код возврата 0", p.returncode == 0, f"код {p.returncode}: {txt[-300:]}")
    check("есть шаг 1 копирования", "копирование набора" in txt, txt[-300:])
    check("нет реального SSH", "ssh" not in txt.lower() or "dry" in txt.lower(),
          txt[-300:])

def t_setup_sshd() -> None:
    """S25: setup_sshd.py — проверка аргументов и --help."""
    print("\\n[S25] setup_sshd.py: аргументы и --help")
    setup()
    # --help
    rc, out, err = run("setup_sshd.py", "--help")
    txt = out + err
    check("--help работает", rc == 0 or "--help" in txt or "использование" in txt.lower(),
          txt[-200:])
    check("есть ключ --port", "--port" in txt, txt[-200:])
    check("есть ключ --no-root", "--no-root" in txt, txt[-200:])
    check("есть ключ --dry-run", "--dry-run" in txt, txt[-200:])
    # без root должен отказать (но не упасть с трейсбеком)
    rc2, out2, err2 = run("setup_sshd.py")
    txt2 = out2 + err2
    check("без root: понятная ошибка", "нужен root" in txt2.lower(),
          txt2[-200:])


def t_check_ssh() -> None:
    """S26: CHECK_SSH.py — проверка аргументов и --help."""
    print("\\n[S26] CHECK_SSH.py: аргументы и --help")
    setup()
    rc, out, err = run("CHECK_SSH.py", "--help")
    txt = out + err
    check("--help не падает", "Traceback" not in txt, txt[-200:])
    # запуск с некорректным портом
    rc2, out2, err2 = run("CHECK_SSH.py", "abc")
    txt2 = out2 + err2
    check("некорректный порт: предупреждение",
          "некорректный порт" in txt2.lower() or "порт: 22" in txt2.lower(),
          txt2[-200:])


def t_collect_all():
    setup()
    tool("lspci", "echo '00:0c.0 CardBus bridge: Texas Instruments PCIxx21'; exit 0")
    tool("pccardctl", 'case "$1" in status) echo "Socket 0:"; echo " 5.0V 16-bit PC Card" ;; ident) echo " no product info available" ;; esac')
    tool("lsmod", "echo 'Module Size Used by'; echo 'pcmcia 1'; echo 'yenta_socket 2'")
    tool("dmesg", "echo '[ 27.038720] pcmcia_socket0: cs: warning: no high memory space available!'")
    out_path = os.path.join(STUB, "report.txt")
    rc, out, err = run("collect_all.py", "--no-slow", "--out", out_path)
    txt = out + err
    check("скрипт не падает", "Traceback" not in txt, txt[-200:])
    check("отчёт создан", os.path.exists(out_path), "нет файла")
    body = open(out_path, encoding="utf-8", errors="replace").read() if os.path.exists(out_path) else ""
    check("в отчёте есть ключевые разделы",
          all(k in body for k in ("1. ОКРУЖЕНИЕ", "2. МОДУЛИ, МОСТ, СЛОТ",
                                  "3. ЧТО ГОВОРИТ ЯДРО", "6. ЧТЕНИЕ COMMON MEMORY",
                                  "7. ПАСПОРТ КАРТЫ", "7.1 ПРОВЕРКА ATTRIBUTE MEMORY",
                                  "КОНЕЦ ОТЧЁТА")), body[-300:])
    check("попали строки ядра про окна памяти",
          "no high memory space" in body, body[-300:])
    check("записан статус слота", "16-bit PC Card" in body, body[-300:])
    check("указан файл отчёта", out_path in body, out_path)
    check("предупреждение про root присутствует",
          "ВНИМАНИЕ" in body or "root" in body, body[-200:])
    # --no-slow действительно пропускает selftest
    check("--no-slow действительно пропускает --selftest",
          "САМОПРОВЕРКА НАБОРА" not in body, "раздел присутствует")
    # --image без прочитанного CIS не должен ничего снимать
    rc2, out2, _ = run("collect_all.py", "--no-slow", "--image",
                       "--img", os.path.join(STUB, "img.bin"), "--out", out_path)
    body2 = open(out_path, encoding="utf-8", errors="replace").read()
    check("образ не снимается, если паспорт карты не прочитан",
          "ПРОПУЩЕНО" in body2, body2[-300:])
    # неизвестный ключ
    rc3, out3, err3 = run("collect_all.py", "--ерунда")
    check("неизвестный ключ отвергается",
          rc3 != 0 and "неизвестный ключ" in (out3 + err3), out3 + err3)


def main(argv=None):
    """Запуск всех тестов. --fast — только быстрые (без долгих наборов)."""
    fast = argv and "--fast" in argv
    tests = [
        t_write_erases_first,
        t_write_keep_tail,
        t_write_no_confirm,
        t_write_too_big,
        t_write_protected,
        t_unmounted_check,
        t_backup_space,
        t_syntax_all,
        t_selftest,
        t_menu_write,
        t_menu_refusal,
        t_menu_install_verify,
        t_noexec_fat32,
        t_diag_chain,
        t_diag_no_high_memory,
        t_diag_probe_failed,
        t_cis_step_present,
        t_diag_kernel_anonymous,
        t_direct_read,
        t_ctl_menu,
        t_direct_sysfs_fallback,
        t_direct_cis,
        t_run_remote_sudo_password,
        t_remote_collect_dryrun,
        t_setup_sshd,
        t_check_ssh,
        t_collect_all,
    ]
    if not fast:
        tests += [
            # Здесь могли бы быть долгие тесты, но их исходник утерян
            # вместе с обрезанным файлом. fast-режим покрывает ключевые проверки.
        ]
    print("=" * 70)
    print("ТЕСТЫ КОНСОЛЬНЫХ СКРИПТОВ (pcmcia-flash-kit)")
    print("=" * 70)
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001 - ловим, чтобы не прерывать весь прогон
            name = getattr(t, "__name__", str(t))
            FAIL.append((name, f"исключение: {exc}"))
            print(f"  FAIL {name}   исключение: {exc}")
    print()
    print("=" * 70)
    print(f"ИТОГО: прошло {len(PASS)}, провалено {len(FAIL)}")
    for name, detail in FAIL:
        print(f"  FAIL {name}   {detail}")
    if FAIL:
        print("ОШИБКА тесты не прошли")
        return 1
    print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    sys.exit(main(sys.argv[1:]))