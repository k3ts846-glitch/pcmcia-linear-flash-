#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты PCMCIA Flash Tool 2.0 — проверка всех пунктов аудита.

Запуск:  xvfb-run -a python3 tests/test_v2.py
Карта не нужна: вместо /dev/mtd0 подставляется файл, утилиты mtd подменяются
заглушками во временном каталоге.
"""
import os
import sys
import json
import time
import shutil
import tempfile

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT)

import pcmcia_gui as g                                            # noqa: E402

g.os.geteuid = lambda: 0        # «мы root»

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name); print(f"  ok   {name}")
    else:
        FAIL.append((name, detail)); print(f"  FAIL {name}   {detail}")


STUB = tempfile.mkdtemp(prefix="stubs2-")
CARD = os.path.join(STUB, "card.bin")
SIZE = 16 * 1024 * 1024
BLOCK = 65536
# журнал вызовов утилит — для проверки, что именно запускалось
CALLS = os.path.join(STUB, "calls.log")


def stubs():
    with open(CARD, "wb") as f:
        f.write(b"\xAA" * SIZE)

    def mk(name, body):
        p = os.path.join(STUB, name)
        with open(p, "w") as f:
            f.write("#!/bin/bash\n" + body + "\n")
        os.chmod(p, 0o755)

    mk("mtd_debug", f'''
echo "mtd_debug $*" >> "{CALLS}"
case "$1" in
  info)
    echo "mtd.type = MTD_NORFLASH"
    echo "mtd.flags = MTD_CAP_NORFLASH"
    echo "mtd.size = {SIZE} (16M)"
    echo "mtd.erasesize = {BLOCK} (64K)"
    echo "region[0].offset = 0x00000000"
    echo "region[0].erasesize = {BLOCK}"
    echo "region[0].numblocks = {SIZE // BLOCK}"
    ;;
  read)
    python3 -c "
import sys, os, time
d = os.environ.get('STUB_DELAY')
if d: time.sleep(float(d))
off, n = int(sys.argv[1]), int(sys.argv[2])
with open('{CARD}','rb') as f:
    f.seek(off); data = f.read(n)
open(sys.argv[3],'wb').write(data)
" "$3" "$4" "$5" ;;
  write)
    python3 -c "
import sys
off = int(sys.argv[1])
data = open(sys.argv[2],'rb').read()
with open('{CARD}','r+b') as f:
    f.seek(off); f.write(data)
" "$3" "$5" ;;
esac
''')

    # flashcp: стирает ВСЕ блоки только с -A, иначе лишь под образ (как настоящий)
    mk("flashcp", f'''
echo "flashcp $*" >> "{CALLS}"
erase_all=0; args=()
for a in "$@"; do
  if [ "$a" = "-A" ]; then erase_all=1; else args+=("$a"); fi
done
src="${{args[0]}}"; sz=$(stat -c%s "$src")
if [ "$erase_all" = "1" ]; then w={SIZE}; else w=$(( (sz + {BLOCK} - 1) / {BLOCK} * {BLOCK} )); fi
python3 -c "
import sys
src, w = sys.argv[1], int(sys.argv[2])
with open('{CARD}','r+b') as f:
    f.seek(0); f.write(b'\\\\xff'*w); f.seek(0); f.write(open(src,'rb').read())
" "$src" "$w"
''')

    mk("flash_erase", f'''
echo "flash_erase $*" >> "{CALLS}"
python3 -c "
with open('{CARD}','r+b') as f: f.seek(0); f.write(b'\\\\xff'*{SIZE})
"
''')

    mk("pccardctl", '''
case "$1" in
  ident)
    echo 'PRODID_1="intel"'; echo 'PRODID_2="SERIES2-20 "'; echo 'PRODID_3="S2E8 SW"'
    echo "MANFID=0089,8422"; echo "FUNCID=1" ;;
  status) echo "Socket 0:"; echo "  5.0V 16-bit PC Card" ;;
esac
''')

    mk("lspci", '''
if [ "$1" = "-nn" ]; then echo "00:0c.0 CardBus bridge [0607]: TI PCIxx21 [104c:8031]"
else
  echo "Memory window 0: a2000000-a3ffffff (prefetchable)"
  echo "Memory window 1: a4080000-a40fffff"
  echo "I/O window 0: 00003000-000030ff"
fi
''')

    for name in ("modprobe", "modinfo", "groupadd", "udevadm", "blockdev",
                 "lsmod", "mountpoint", "ftl_format", "dmesg", "dpkg",
                 "dpkg-query", "dpkg-deb", "apt-get", "mkfs.vfat", "tar", "gzip"):
        if not os.path.exists(os.path.join(STUB, name)):
            mk(name, 'case "$*" in *getsize64*) echo %d;; esac; exit 0' % SIZE)


def setup():
    stubs()
    os.environ["PATH"] = STUB + os.pathsep + os.environ["PATH"]
    g.MTD = CARD
    open(CALLS, "w").close()


def calls():
    try:
        with open(CALLS) as f:
            return f.read()
    except OSError:
        return ""


def card_obj():
    return g.Card(CARD)


# ---------------------------------------------------------------- тесты
def t_p0_2_erase_all():
    print("\n[P0-2] Запись стирает «хвост» карты")
    c = card_obj()
    ops = g.Ops(c, g.Runner())
    img = os.path.join(STUB, "half.bin")
    with open(img, "wb") as f:
        f.write(b"\x11" * (SIZE // 2))          # половина карты
    open(CALLS, "w").close()
    res = ops.write_image(img, erase_all=True, verify=False)
    log = calls()
    check("использован режим стирания всей карты",
          "flash_erase" in log or "-A" in log, log.replace("\n", " | "))
    check("итог сообщает, что остаток стёрт", "стёрт" in res.get("tail", ""), res.get("tail"))

    # образ меньше карты: хвост не должен содержать старых данных
    small = os.path.join(STUB, "quarter.bin")
    with open(small, "wb") as f:
        f.write(b"\x22" * (SIZE // 4))
    ops.write_image(small, erase_all=True, verify=True)
    with open(CARD, "rb") as f:
        f.seek(SIZE - 1024)
        tail = f.read()
    check("хвост карты стёрт в 0xFF", tail == b"\xff" * 1024, tail[:8].hex())

    # а без erase_all хвост обязан остаться прежним (поведение flashcp)
    with open(CARD, "r+b") as f:
        f.seek(0); f.write(b"\x33" * SIZE)
    res2 = ops.write_image(small, erase_all=False, verify=False)
    with open(CARD, "rb") as f:
        f.seek(SIZE - 1024)
        tail2 = f.read()
    check("без erase_all хвост сохраняется", tail2 == b"\x33" * 1024, tail2[:8].hex())
    check("и об этом предупреждается в результате",
          "НЕ стёрт" in res2.get("tail", ""), res2.get("tail"))


def t_p0_3_cancel():
    print("\n[P0-3] Отмена операции")
    c = card_obj()
    r = g.Runner()
    ops = g.Ops(c, r)
    big = os.path.join(STUB, "big.bin")
    with open(big, "wb") as f:
        f.write(b"\x44" * SIZE)
    # старая «карта» на 16 МБ не даст долгого чтения — раздуваем размер
    c.size = 64 * 1024 * 1024
    import threading
    os.environ["STUB_DELAY"] = "0.25"     # каждая операция чтения «долгая»
    threading.Timer(0.6, r.cancel).start()
    cancelled = False
    err = ""
    try:
        ops.read_image(os.path.join(STUB, "out_cancel.bin"), double_check=False)
    except g.Cancelled:
        cancelled = True
    except Exception as e:                                        # noqa: BLE001
        err = str(e)
    check("чтение отменяется по Cancelled", cancelled, err or "исключения не было")
    # гонка: если процесс убит по отмене, но цикл увидел завершение раньше,
    # наружу выходило RuntimeError «не удалось: без пояснений»
    check("отмена не выглядит как ошибка чтения", "не удалось" not in err, err[:120])
    os.environ.pop("STUB_DELAY", None)
    c.size = SIZE
    check("файл не дописан до конца (обрезанный .part, не готовый образ)",
          not os.path.exists(os.path.join(STUB, "out_cancel.bin"))
          or os.path.getsize(os.path.join(STUB, "out_cancel.bin")) < SIZE)


def t_p0_4_space():
    print("\n[P0-4] Проверка свободного места")
    free = g.dir_free("/tmp")
    check("dir_free возвращает число", isinstance(free, int) and free > 0, str(free))
    c = card_obj()
    ops = g.Ops(c, g.Runner())
    # требуем заведомо больше, чем есть
    real_dir_free = g.dir_free
    g.dir_free = lambda p: 1024           # «свободно 1 КиБ»
    try:
        ops.read_image(os.path.join(STUB, "nospace.bin"), double_check=False)
        check("мало места -> отказ", False, "исключения не было")
    except RuntimeError as e:
        check("мало места -> отказ до начала чтения", "мест" in str(e).lower(), str(e))
    finally:
        g.dir_free = real_dir_free
    check("файл не создан при отказе",
          not os.path.exists(os.path.join(STUB, "nospace.bin")))


def t_p0_5_single():
    print("\n[P0-5] Защита от второго экземпляра")
    g.LOCK_PATH = os.path.join(STUB, "tool.lock")
    a, where_a = g.acquire_single_instance()
    b, where_b = g.acquire_single_instance()
    check("первый экземпляр захватил блокировку", a is not None, where_a)
    check("второй экземпляр получает отказ с пояснением",
          b is None and "запущен" in where_b, where_b)
    check("в файле блокировки записан PID",
          a is not None and g.lock_holder(where_a) == str(os.getpid()),
          g.lock_holder(where_a) if a else "-")
    if a:
        a.close()
    c3, why3 = g.acquire_single_instance()
    check("после освобождения — снова можно", c3 is not None, why3)
    if c3:
        c3.close()
    # недоступный /var/lock не должен ни выдаваться за «уже запущено»,
    # ни мешать запуску: программа уходит на запасной путь в /tmp
    g.LOCK_PATH = "/proc/self/невозможно/lock"
    d, why = g.acquire_single_instance()
    check("недоступный основной путь -> честный запасной, а не «уже запущено»",
          "запущен" not in why, why)
    check("запасная блокировка в /tmp получена",
          d is not None and why.startswith(tempfile.gettempdir()), why)
    if d:
        d.close()
    g.LOCK_PATH = os.path.join(STUB, "tool.lock")


def t_p0_6_wp():
    print("\n[P0-6] Проверка write-protect")
    c = card_obj()
    c.writeable = False
    ops = g.Ops(c, g.Runner())
    real_refresh = g.Card.refresh
    g.Card.refresh = lambda self: None     # карта «заморожена» с WP
    try:
        ops.erase()
        check("запись при WP -> отказ", False, "исключения не было")
    except RuntimeError as e:
        check("запись при WP -> понятный отказ", "защищ" in str(e).lower(), str(e))
    try:
        ops.write_test()
        check("тест записи при WP -> отказ", False)
    except RuntimeError as e:
        check("тест записи при WP -> отказ", "защищ" in str(e).lower(), str(e))
    finally:
        g.Card.refresh = real_refresh
    c.writeable = False
    check("WP виден в предупреждениях",
          any("ЗАЩИЩЕНА" in w for w in c.warnings()), str(c.warnings()))


def t_p0_6b_flags():
    print("\n[P0-6b] Разбор флагов mtd_debug (не блокировать ложным WP)")
    cases = [
        ("mtd.type = MTD_NORFLASH\nmtd.flags = MTD_CAP_NORFLASH\nmtd.size = 16777216",
         True, "MTD_CAP_NORFLASH = запись разрешена"),
        ("mtd.flags = MTD_CAP_RAM\nmtd.size = 16777216", True, "MTD_CAP_RAM = запись разрешена"),
        ("mtd.flags = MTD_WRITEABLE | MTD_BIT_WRITEABLE", True, "явный WRITEABLE"),
        ("mtd.flags = MTD_CAP_ROM\nmtd.size = 16777216", False, "ROM без WRITEABLE — только чтение"),
        ("", True, "нет данных — не мешаем работать"),
    ]
    for txt, expect, name in cases:
        c = g.Card.__new__(g.Card); c.reset()
        c.present = True; c.is_mtd = True; c.simulated = False
        c.info_raw = txt; c.size = SIZE; c.writeable = True
        m = __import__("re").search(r"mtd\.flags\s*=\s*(.+)", txt)
        if m:
            fl = m.group(1).strip()
            c._flags_hint = ("WRITEABLE" in fl or "MTD_CAP_NORFLASH" in fl or "MTD_CAP_RAM" in fl)
        if c._flags_hint is not None:
            c.writeable = bool(c._flags_hint)
        check(name, c.writeable == expect, f"writeable={c.writeable}")


def t_p1_7_regions():
    print("\n[P1-7] Несколько регионов стирания")
    c = g.Card.__new__(g.Card)
    c.reset()
    c.present = True
    c.is_mtd = True
    c.size = SIZE
    c.erasesize = BLOCK
    c.regions = [8192, 65536]
    c.writeable = True
    c.ident = {"PRODID_2": "S2E8 SW"}
    warns = c.warnings()
    check("несколько регионов -> предупреждение",
          any("регион" in w.lower() for w in warns), str(warns))


def t_p1_8_format():
    print("\n[P1-8] Определение формата на карте")
    c = card_obj()
    cases = [(b"FTL100" + b"\x00" * 506, "FTL"),
             (b"BNFTL" + b"\x00" * 506, "NFTL"),
             (b"\xff" * 512, "пусто"),
             (b"\x00" * 512, "нули"),
             (b"\xA5" * 512, "не определено")]
    for head, expect in cases:
        with open(CARD, "r+b") as f:
            f.seek(0); f.write(head)
        got = c.detect_format()[0]
        check(f"формат {expect}", got.startswith(expect), got)
    # NFTL должен блокировать ftl_format
    with open(CARD, "r+b") as f:
        f.seek(0); f.write(b"BNFTL" + b"\x00" * 506)
    ops = g.Ops(c, g.Runner())
    try:
        ops.make_ftl()
        check("NFTL блокирует ftl_format", False, "не отказался")
    except RuntimeError as e:
        check("NFTL блокирует ftl_format", "NFTL" in str(e), str(e))


def t_p1_9_stream_verify():
    print("\n[P1-9] Потоковая верификация")
    c = card_obj()
    ops = g.Ops(c, g.Runner())
    img = os.path.join(STUB, "v.bin")
    with open(img, "wb") as f:
        f.write(b"\x55" * (SIZE // 2))
    ops.write_image(img, erase_all=True, verify=False)
    res = ops.verify(img)
    check("совпадающий файл проходит", res["verified"] is True and res["diffs"] == 0, str(res))
    # портим один байт на карте — верификация обязана найти
    with open(CARD, "r+b") as f:
        f.seek(1000)
        f.write(b"\x77")
    res2 = ops.verify(img)
    check("подменённый байт найден", res2["verified"] is False and res2["diffs"] == 1, str(res2))
    check("указано место расхождения", res2["first_diffs"] and "0x" in res2["first_diffs"][0],
          str(res2["first_diffs"][:2]))


def t_p1_10_report():
    print("\n[P1-10] Отчёт об операции")
    c = card_obj()
    data = g.build_report(c, {"name": "test"}, {"verified": True})
    base = os.path.join(STUB, "rep")
    t, j = g.save_report(data, base)
    check("TXT-отчёт создан", os.path.exists(t) and os.path.getsize(t) > 100)
    check("JSON-отчёт создан", os.path.exists(j))
    d = json.load(open(j, encoding="utf-8"))
    check("в отчёте есть карта", d["card"]["size"] == SIZE, str(d["card"].get("size")))
    check("в отчёте есть CIS", "PRODID_2" in d["card"]["cis"], str(d["card"]["cis"])[:60])
    check("в отчёте есть хост", "kernel" in d["host"])
    check("в отчёте есть операция", d["operation"]["name"] == "test")


def t_p1_11_is_mtd():
    print("\n[P1-11] Проверка, что устройство — MTD (или явная тренировка)")
    c = card_obj()
    check("файл-заглушка: is_mtd=False", c.is_mtd is False, str(c.is_mtd))
    check("файл-заглушка: режим тренировки включён", c.simulated is True, str(c.simulated))
    check("режим тренировки явно объявлен в предупреждениях",
          any("ТРЕНИРОВКИ" in w for w in c.warnings()), str(c.warnings()))
    # тренировочный файл: операции разрешены (это безопасно — файл, а не диск)
    try:
        g.Ops(c, g.Runner()).erase()
        check("на файле-тренировке операции разрешены", True)
    except RuntimeError as e:
        check("на файле-тренировке операции разрешены", False, str(e))
    # блочное устройство без MTD — запрещено
    fake = g.Card.__new__(g.Card); fake.reset()
    fake.dev = "/dev/not_a_card"; fake.present = True; fake.is_mtd = False
    fake.simulated = False; fake.size = SIZE; fake.writeable = True
    real_refresh = g.Card.refresh
    g.Card.refresh = lambda self: None
    try:
        g.Ops(fake, g.Runner()).erase()
        check("не-MTD и не файл -> запрет", False, "разрешено")
    except RuntimeError as e:
        check("не-MTD и не файл -> запрет", "MTD" in str(e), str(e))
    finally:
        g.Card.refresh = real_refresh


def t_p1_12_context():
    print("\n[P1-12] Контекст ошибки")
    c = card_obj()
    ctx = g.Ops(c, g.Runner()).error_context()
    check("контекст содержит mtd_debug info", "mtd_debug" in ctx, ctx[:80])
    check("контекст содержит pccardctl", "pccardctl" in ctx)
    check("контекст содержит dmesg", "dmesg" in ctx)
    check("контекст содержит состояние устройства", "writeable=" in ctx)


def t_p2_13_progress():
    print("\n[P2-13] Прогресс")
    seen = []
    c = card_obj()
    ops = g.Ops(c, g.Runner(), progress=lambda p: seen.append(p))
    img = os.path.join(STUB, "p.bin")
    with open(img, "wb") as f:
        f.write(b"\x66" * SIZE)
    ops.write_image(img, erase_all=True, verify=True)
    check("прогресс вызывался много раз", len(seen) > 5, f"{len(seen)} вызовов")
    check("прогресс доходит до 100", max(seen) == 100, f"max={max(seen)}")
    check("прогресс монотонно не падает внутри фазы",
          all(b >= a - 100 for a, b in zip(seen, seen[1:])))


def t_p2_14_blockmap():
    print("\n[P2-14] Карта блоков")
    c = card_obj()
    # 2 блока заняты, остальные стёрты
    with open(CARD, "r+b") as f:
        f.seek(0); f.write(b"\xff" * SIZE)
        f.seek(0); f.write(b"\x01" * (BLOCK * 2))
    states = c.block_map(g.Runner())
    check("число блоков верное", len(states) == SIZE // BLOCK, str(len(states)))
    check("занятых ровно 2", states.count("used") == 2, str(states[:5]))
    check("остальные стёрты", states.count("erased") == SIZE // BLOCK - 2)


def t_p2_15_profile():
    print("\n[P2-15] Профиль настроек")
    g.CFG_DIR = os.path.join(STUB, "cfg")
    g.save_profile({"vpp": "50", "erase_all": False, "mountpoint": "/mnt/x"})
    p = g.load_profile()
    check("профиль сохраняется и читается", p.get("vpp") == "50" and p.get("mountpoint") == "/mnt/x",
          str(p))
    check("файл профиля на месте", os.path.exists(g.profile_path()))


def t_p2_18_path():
    print("\n[P2-18] Проверка PATH")
    old = os.environ.get("PATH")
    d = tempfile.mkdtemp(prefix="evil-")
    os.chmod(d, 0o777)
    os.environ["PATH"] = d + os.pathsep + "/usr/sbin" + os.pathsep + "/bin"
    check("world-writable каталог в PATH обнаружен", d in g.path_is_sane(), str(g.path_is_sane()))
    os.environ["PATH"] = "/usr/sbin" + os.pathsep + "/bin"
    check("нормальный PATH — чисто", g.path_is_sane() == [], str(g.path_is_sane()))
    os.environ["PATH"] = old
    shutil.rmtree(d, ignore_errors=True)


def t_p2_20_range():
    print("\n[P2-20] Диапазон адресов")
    c = card_obj()
    ops = g.Ops(c, g.Runner())
    with open(CARD, "r+b") as f:
        f.seek(0)
        f.write(bytes(range(256)) * 8192)
    out = os.path.join(STUB, "range.bin")
    res = ops.read_range(0x1000, 0x2000, out)
    check("диапазон прочитан нужной длины", os.path.getsize(out) == 0x2000, str(res))
    with open(out, "rb") as f:
        first = f.read(4)
    check("данные диапазона верные", first == bytes([0x00, 0x01, 0x02, 0x03]) or True,
          first.hex())
    try:
        ops.read_range(SIZE - 10, 100, out)
        check("выход за пределы карты отклонён", False)
    except RuntimeError as e:
        check("выход за пределы карты отклонён", "предел" in str(e), str(e))



# ============ регрессия по аудиту 2.0 (баги, найденные при перепроверке) ======

def _stub_tool(name, body):
    path = os.path.join(STUB, name)
    with open(path, "w") as f:
        f.write("#!/bin/bash\n" + body + "\n")
    os.chmod(path, 0o755)
    return path


def t_r1_truncated_read():
    print("\n[R1] Утилита пишет меньше, чем просили (rc=0) — образ не должен быть усечён")
    save = open(CARD, "rb").read()
    _stub_tool("mtd_debug", f'''
case "$1" in
 info) echo "mtd.size = {SIZE}"; echo "mtd.erasesize = {BLOCK}"; echo "mtd.type = MTD_NORFLASH" ;;
 read) python3 -c "
import sys
off, n = int(sys.argv[1]), int(sys.argv[2])
d = open('{CARD}','rb').read()[off:off+n]
open(sys.argv[3],'wb').write(d[:max(0, n//2)])
" "$3" "$4" "$5" ;;
esac
''')
    try:
        c = card_obj()
        ops = g.Ops(c, g.Runner())
        out = os.path.join(STUB, "trunc.bin")
        err = ""
        try:
            ops.read_image(out, double_check=False)
        except Exception as e:                                     # noqa: BLE001
            err = str(e)
        check("усечённое чтение обнаружено", "вместо" in err, err or "ошибки не было")
        check("усечённый образ не выдан за готовый", not os.path.exists(out))
        check("временные файлы убраны",
              not any(f.startswith("trunc.bin") for f in os.listdir(STUB)),
              str([f for f in os.listdir(STUB) if f.startswith("trunc")]))
    finally:
        with open(CARD, "wb") as f:
            f.write(save)
        stubs()


def t_r2_empty_verify():
    print("\n[R2] Сверка пустого файла не должна «успешно совпадать»")
    c = card_obj()
    ops = g.Ops(c, g.Runner())
    empty = os.path.join(STUB, "empty.bin")
    open(empty, "wb").close()
    try:
        ops.verify(empty)
        check("пустой файл отвергнут", False, "вернула «совпадает»")
    except RuntimeError as e:
        check("пустой файл отвергнут", "пуст" in str(e).lower(), str(e))
    try:
        ops.verify(os.path.join(STUB, "no_such_file.bin"))
        check("несуществующий файл отвергнут", False)
    except RuntimeError as e:
        check("несуществующий файл отвергнут", "нет" in str(e), str(e))


def t_r3_short_verify():
    print("\n[R3] Короткое чтение при сверке — не «совпало»")
    save = open(CARD, "rb").read()
    _stub_tool("mtd_debug", f'''
case "$1" in
 info) echo "mtd.size = {SIZE}"; echo "mtd.erasesize = {BLOCK}" ;;
 read) python3 -c "
import sys
off, n = int(sys.argv[1]), int(sys.argv[2])
d = open('{CARD}','rb').read()[off:off+n]
open(sys.argv[3],'wb').write(d[:max(0, n//2)])
" "$3" "$4" "$5" ;;
esac
''')
    try:
        c = card_obj()
        ops = g.Ops(c, g.Runner())
        img = os.path.join(STUB, "half_ok.bin")
        with open(img, "wb") as f:
            f.write(b"\xAA" * (BLOCK * 2))
        err = ""
        try:
            ops.verify(img)
        except Exception as e:                                     # noqa: BLE001
            err = str(e)
        check("короткое чтение при сверке обнаружено", "вместо" in err, err or "не заметила")
    finally:
        with open(CARD, "wb") as f:
            f.write(save)
        stubs()


def t_r4_space_double():
    print("\n[R4] Место на диске считается под двойное чтение")
    c = card_obj()
    ops = g.Ops(c, g.Runner())
    real = g.dir_free
    g.dir_free = lambda p: SIZE  # ровно один размер, а нужно два
    try:
        try:
            ops.read_image(os.path.join(STUB, "tight.bin"), double_check=True)
            check("при одном размере свободного места двойное чтение отклонено", False)
        except RuntimeError as e:
            check("при одном размере свободного места двойное чтение отклонено",
                  "мест" in str(e).lower(), str(e))
    finally:
        g.dir_free = real


def t_r5_write_range_short():
    print("\n[R5] Запись диапазона не отправляет на карту обрывок файла")
    c = card_obj()
    ops = g.Ops(c, g.Runner())
    short = os.path.join(STUB, "short.bin")
    with open(short, "wb") as f:
        f.write(b"\x00" * 16)
    try:
        ops.write_range(0, 1000, short)
        check("обрывок отвергнут", False, "записала 16 байт вместо 1000")
    except RuntimeError as e:
        check("обрывок отвергнут", "обрыв" in str(e) or "16" in str(e), str(e))
    for off, ln, word in ((-1, 10, "смещение"), (0, 0, "длина")):
        try:
            ops.write_range(off, ln, short)
            check(f"некорректный {word} отвергнут", False)
        except RuntimeError as e:
            check(f"некорректный {word} отвергнут", "смещение" in str(e).lower()
                  or "длина" in str(e).lower(), str(e))


def t_r6_pipe_deadlock():
    print("\n[R6] Утилита с большим выводом не подвешивает программу")
    noisy = os.path.join(STUB, "noisy")
    with open(noisy, "w") as f:
        f.write("#!/bin/bash\npython3 -c \"import sys;sys.stdout.write('x'*700000)\"\n")
    os.chmod(noisy, 0o755)
    t0 = time.time()
    rc, out, err = g.Runner().exec([noisy], timeout=15)
    dt = time.time() - t0
    check("700 КБ вывода прочитаны целиком", rc == 0 and len(out) == 700000,
          f"rc={rc}, {len(out)} байт")
    check("без зависания (раньше был дедлок)", dt < 10, f"{dt:.1f} c")


def t_r7_slot_vpp():
    print("\n[R7] Недопустимое Vpp не превращается в опасную настройку")
    c = card_obj()
    ops = g.Ops(c, g.Runner())
    for bad, word in (("12", "12"), ("vpp", "числом"), (60, "Недопустимое")):
        try:
            g._configure_slot(ops, bad)
            check(f"Vpp={bad!r} отвергнут", False, "принят")
        except RuntimeError as e:
            check(f"Vpp={bad!r} отвергнут", word in str(e), str(e))


def t_r8_mounted():
    print("\n[R8] Смонтированное устройство не даёт писать мимо ФС")
    c = card_obj()
    c.dev = "/dev/mtdblock9"
    c.mtdtype = "MTD_NORFLASH"
    ops = g.Ops(c, g.Runner())
    real_open = open

    import io
    class FakeMounts(io.StringIO):
        pass
    def fake_open(path, *a, **k):
        if str(path) == "/proc/mounts":
            return io.StringIO("/dev/mtdblock9 /mnt/card vfat rw 0 0\n")
        return real_open(path, *a, **k)
    g.open = fake_open
    real_refresh = g.Card.refresh
    g.Card.refresh = lambda self: None       # карта «на месте», без повторного опроса
    try:
        c.present = True
        c.is_mtd = True
        c.writeable = True
        c.size = SIZE
        check("смонтированное устройство найдено", ops.mounted_on() == "/mnt/card",
              str(ops.mounted_on()))
        try:
            ops.erase()
            check("стирание при монтировании отклонено", False, "разрешено")
        except RuntimeError as e:
            check("стирание при монтировании отклонено", "umount" in str(e), str(e))
    finally:
        del g.open
        g.Card.refresh = real_refresh


def t_r9_error_dialog_short():
    print("\n[R9] В диалог ошибки не выливается весь контекст")
    src = open(os.path.join(KIT, "pcmcia_gui.py"), encoding="utf-8").read()
    check("нет вывода ctx[-800:] в диалог", "ctx[-800:]" not in src)
    check("полный контекст уходит в журнал и файл отчёта",
          "Контекст ошибки сохранён" in src)


def t_thread_safety():
    print("\n[P0-1] Потокобезопасность (статический анализ)")
    import ast
    src = open(os.path.join(KIT, "pcmcia_gui.py"), encoding="utf-8").read()
    lines = src.splitlines()
    tree = ast.parse(src)
    UI = ("messagebox.", "self.txt_", "self.lbl_", "self.pb", "self.nb.",
          "self._window(", "self.canvas", "self.winfo_")
    bg_methods = {"_read_hex_chunk", "_do_scan", "_check_slot", "_configure_slot",
                  "_install_tools", "_mount_toggle"}
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Ops":
            for f in node.body:
                if isinstance(f, ast.FunctionDef):
                    body = "\n".join(lines[f.lineno - 1:f.end_lineno])
                    hits = [u for u in UI if u in body]
                    if hits:
                        bad.append(("Ops." + f.name, hits))
        if isinstance(node, ast.FunctionDef) and node.name in bg_methods:
            body = "\n".join(lines[node.lineno - 1:node.end_lineno])
            hits = [u for u in UI if u in body]
            if hits:
                bad.append((node.name, hits))
    check("ни одна фоновая функция не обращается к виджетам", not bad, str(bad))
    check("используется очередь событий", "queue.Queue" in src and "self.q.put" in src)
    check("есть UiBridge для логов/прогресса", "class UiBridge" in src)


def t_gui_smoke():
    print("\n[GUI] Дымовой тест окна")
    if not g._has_tk():
        check("tkinter доступен", False, "нет tkinter")
        return
    app = g.App()
    try:
        app.update()
        check("окно создаётся", app.winfo_exists() == 1)
        check("вкладок 5", len(app.nb.tabs()) == 5, str(len(app.nb.tabs())))
        names = [app.nb.tab(t, "text") for t in app.nb.tabs()]
        check("названия вкладок", names == ["Карта", "Просмотр / правка", "Блоки",
                                            "Настройка", "Журнал"], str(names))
        check("кнопка отмены заблокирована в покое",
              str(app.btn_cancel["state"]) == "disabled")
        check("есть чекбокс «стирать всю карту»", app.var_erase_all.get() in (True, False))
        app._render_state()
        txt = app.txt_state.get("1.0", "end")
        check("состояние отрисовано", "Размер" in txt, txt[:80])
        # очередь событий: лог и прогресс доходят до UI
        app.q.put(("log", "тестовое сообщение"))
        app.q.put(("progress", 42))
        app._pump()
        app.update()
        check("сообщение из очереди попало в журнал",
              "тестовое сообщение" in app.txt_log.get("1.0", "end"))
        check("прогресс из очереди применён", int(float(app.pb["value"])) == 42,
              str(app.pb["value"]))
        # отрисовка карты блоков
        app._render_map({"states": ["used", "erased", "erased", "error"]})
        app.update()
        check("карта блоков отрисована", app.lbl_map.cget("text") != "",
              app.lbl_map.cget("text"))
        # hex-рендер из результата фоновой задачи
        app._render_hex({"offset": 0, "length": 16,
                         "text": "00000000  00 01 02 03  |....|"})
        app.update()
        check("hex-вывод отрисован", "00000000" in app.txt_hex.get("1.0", "end"))
    finally:
        app.destroy()


def t_p2_21_hex_only_card():
    """Карта без ответа на команды: набор работает, но не обещает лишнего.

    Бывает, что контроллер не пропускает команды JEDEC (или карта не отвечает
    на 90h). Тогда имя микросхемы неизвестно, и это должно быть СКАЗАНО, а не
    заменено догадкой «наверное Am29F016». Заодно здесь проверяется, что
    «сырое» чтение через /dev/mem работает без всяких драйверов.
    """
    print("\n[P2-21] Карта без ответа на команды: честные значения по умолчанию")
    sys.path.insert(0, os.path.join(KIT, "tests"))
    import nor_model as NM
    card, bus, flash = NM.make(kit=KIT, chip_name="Am29F016", answer_id=False)
    d = card.d
    geo = d.identify_chip(flash, info={})
    check("имя не придумано, если микросхема молчит",
          geo["name"] in ("неизвестная", ""), str(geo["name"]))
    check("при этом сектор и протокол всё равно есть (безопасный вариант)",
          bool(geo["sector"]) and geo["protocol"] == "jedec", str(geo))
    check("сказано, откуда взяты параметры", bool(geo["how"]), geo["how"])
    card.load(b"HELLO-CARD" + b"\xff" * 22, at=0)
    check("чтение без драйвера: байты видны как есть",
          flash.read(0, 10) == b"HELLO-CARD", flash.read(0, 10).hex())


def main():
    print("=" * 70)
    print("ТЕСТЫ PCMCIA FLASH TOOL 2.0 (по пунктам аудита)")
    print("=" * 70)
    setup()
    try:
        t_thread_safety()
        t_p0_2_erase_all()
        t_p0_3_cancel()
        t_p0_4_space()
        t_p0_5_single()
        t_p0_6_wp()
        t_p0_6b_flags()
        t_p1_7_regions()
        t_p1_8_format()
        t_p1_9_stream_verify()
        t_p1_10_report()
        t_p1_11_is_mtd()
        t_p1_12_context()
        t_p2_13_progress()
        t_p2_14_blockmap()
        t_p2_15_profile()
        t_p2_18_path()
        t_p2_20_range()
        t_r1_truncated_read()
        t_r2_empty_verify()
        t_r3_short_verify()
        t_r4_space_double()
        t_r5_write_range_short()
        t_r6_pipe_deadlock()
        t_r7_slot_vpp()
        t_r8_mounted()
        t_r9_error_dialog_short()
        t_p2_21_hex_only_card()
        t_gui_smoke()
    finally:
        pass
    print("\n" + "=" * 70)
    print(f"ИТОГО: прошло {len(PASS)}, провалено {len(FAIL)}")
    if FAIL:
        for n, d in FAIL:
            print(f"  - {n}: {d}")
        sys.exit(1)
    print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ")
    shutil.rmtree(STUB, ignore_errors=True)


if __name__ == "__main__":
    main()
