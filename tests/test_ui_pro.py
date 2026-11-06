#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты отзывчивости и живучести интерфейса (профессиональный слой).

    xvfb-run -a python3 tests/test_ui_pro.py

Проверяют то, что не видно на «быстрой» карте: медленный драйвер, залипший
слот, неправильный вызов из потока, закрытие окна во время операции.
"""
import os
import sys
import io
import contextlib
import time
import tempfile
import threading

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT)

import pcmcia_gui as g                                            # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name); print(f"  ok   {name}")
    else:
        FAIL.append((name, detail)); print(f"  FAIL {name}   {detail}")


STUB = tempfile.mkdtemp(prefix="uipro-")
CARD = os.path.join(STUB, "card.bin")
SIZE = 2 * 1024 * 1024
DELAY_FILE = os.path.join(STUB, "delay")


def setup(delay=0):
    with open(CARD, "wb") as f:
        f.write(b"\xAA" * SIZE)
    with open(DELAY_FILE, "w") as f:
        f.write(str(delay))

    def tool(name, body):
        p = os.path.join(STUB, name)
        with open(p, "w") as f:
            f.write("#!/bin/bash\n" + body + "\n")
        os.chmod(p, 0o755)

    tool("mtd_debug", f'''
d=$(cat "{DELAY_FILE}")
[ "$d" != "0" ] && sleep "$d"
case "$1" in
 info) echo "mtd.size = {SIZE}"; echo "mtd.erasesize = 65536"; echo "mtd.type = MTD_NORFLASH" ;;
 read) python3 -c "
import sys
dev, off, n = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
open(sys.argv[4],'wb').write(open(dev,'rb').read()[off:off+n])" "$2" "$3" "$4" "$5" ;;
 write) python3 -c "
import sys;off=int(sys.argv[1])
open('{CARD}','r+b').write(open(sys.argv[2],'rb').read())" "$3" "$5" ;;
esac''')
    for t in ("lspci", "pccardctl", "blockdev", "flashcp", "flash_erase"):
        tool(t, "exit 0")
    os.environ["PATH"] = STUB + os.pathsep + os.environ["PATH"]
    os.environ["PCM_TMP"] = os.path.join(STUB, "tmpwork")
    os.makedirs(os.environ["PCM_TMP"], exist_ok=True)
    g.MTD = CARD
    g.LOCK_PATH = os.path.join(STUB, "l.lock")
    g.LOG_DIR = os.path.join(STUB, "log")
    g.os.geteuid = lambda: 0


def pump(app, seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.update()
        time.sleep(0.02)


def test_fast_start():
    print("\n[U1] Окно появляется мгновенно даже при медленной карте (3 с на вызов)")
    setup(delay=3)
    try:
        t0 = time.time()
        app = g.App()
        app.update()
        dt = time.time() - t0
        check("окно нарисовано меньше чем за 1 с", dt < 1.0, f"{dt:.2f} c")
        check("размер карты виден сразу (без утилит)", app.card.size == SIZE,
              str(app.card.size))
        # ждём полный опрос в фоне
        pump(app, 5)
        check("фоновый опрос доехал и обновил тип MTD",
              app.card.mtdtype == "MTD_NORFLASH", app.card.mtdtype)
        app.destroy()
    finally:
        setup(delay=0)


def test_buttons_not_blocked():
    print("\n[U2] Пока идёт фоновый опрос, кнопки не отвечают «занято»")
    setup(delay=2)
    try:
        app = g.App()
        app.update()
        check("флаг занятости при старте не поднят", app.busy_op is False, str(app.busy_op))
        check("опрос идёт в отдельном потоке",
              app.probe_thread is not None and app.probe_thread.daemon,
              str(app.probe_thread))
        # имитируем нажатие операции во время опроса
        warnings = []
        g.messagebox.showwarning = lambda *a, **k: warnings.append(a)
        app._run(lambda ops: {"ok": True}, "тест")
        check("операция не отклонена из-за фонового опроса", not warnings, str(warnings))
        pump(app, 0.6)
        app.destroy()
    finally:
        setup(delay=0)


def test_cancel_does_not_freeze():
    print("\n[U3] Кнопка «Отмена» не морозит окно")
    setup(delay=0)
    app = g.App()
    app.update()

    # процесс, который не реагирует на SIGTERM сразу
    p = os.path.join(STUB, "stubborn")
    with open(p, "w") as f:
        f.write("#!/bin/bash\ntrap '' TERM\nsleep 30\n")
    os.chmod(p, 0o755)
    r = g.Runner()
    threading.Thread(target=lambda: r.exec([p], timeout=60), daemon=True).start()
    time.sleep(0.4)
    app.runner = r
    t0 = time.time()
    app.cancel_op()
    dt = time.time() - t0
    check("cancel_op вернулся быстрее 0.2 с", dt < 0.2, f"{dt:.3f} c")
    check("окно продолжает перерисовываться", app.winfo_exists() == 1)
    for _ in range(3):
        r.cancel(wait=False)
    app.destroy()


def test_pump_survives_error():
    print("\n[U4] Ошибка в обработчике не останавливает очередь событий")
    setup(delay=0)
    app = g.App()
    app.update()
    app.q.put(("unknown_kind", None))
    app.q.put(("log", "после мусора"))
    pump(app, 0.5)
    check("журнал жив после неизвестного события",
          "после мусора" in app.txt_log.get("1.0", "end"))
    # ломаем отрисовку карты и проверяем, что очередь продолжает работать
    orig = app._draw_map
    def boom():
        raise RuntimeError("искусственный сбой отрисовки")
    app._draw_map = boom
    app._render_map({"states": ["used"] * 4})
    app.q.put(("log", "после сбоя отрисовки"))
    pump(app, 0.6)
    check("журнал жив после сбоя внутри отрисовки",
          "после сбоя отрисовки" in app.txt_log.get("1.0", "end"))
    app._draw_map = orig
    app.destroy()


def test_close_during_op():
    print("\n[U5] Закрытие во время операции")
    setup(delay=0)
    app = g.App()
    app.update()
    asked = []
    g.messagebox.askyesno = lambda *a, **k: (asked.append(1), True)[1]
    app.busy_op = True
    app.runner = g.Runner()
    app.on_close()
    check("спросили подтверждение", len(asked) == 1, str(asked))
    check("окно не закрылось мгновенно (даём операции завершиться)",
          app.winfo_exists() == 1)
    app.busy_op = False
    app._force_close()
    check("после завершения окно закрывается без ошибок", True)
    setup(delay=0)


def test_cancel_warning():
    print("\n[U6] После отмены пользователю сказано про промежуточное состояние")
    setup(delay=0)
    app = g.App()
    app.update()
    shown = []
    g.messagebox.showwarning = lambda *a, **k: shown.append(a)
    app._op_finished({"cancelled": True,
                      "warning": "Карта может остаться в промежуточном состоянии"}, None)
    check("предупреждение показано", any("промежуточном" in str(a) for a in shown), str(shown[:1]))
    app.destroy()


def test_cli():
    print("\n[U7] Командная строка")
    rc = os.system(f'"{sys.executable}" "{os.path.join(KIT, "pcmcia_gui.py")}" '
                   f'--selftest --doctor >/dev/null 2>&1')
    check("--selftest вместе с --doctor отвергаются", rc != 0, f"код {rc}")
    rc2 = os.system(f'"{sys.executable}" "{os.path.join(KIT, "pcmcia_gui.py")}" '
                    f'--ерунда >/dev/null 2>&1')
    check("неизвестный параметр отвергается", rc2 != 0, f"код {rc2}")


def test_no_display():
    print("\n[U13] Запуск без графики объясняется, а не падает трейсбеком")
    import subprocess as sp
    kit = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    r = sp.run([sys.executable, os.path.join(kit, "pcmcia_gui.py")],
               capture_output=True, text=True, env=env, timeout=60)
    out = r.stdout + r.stderr
    check("нет трейсбека", "Traceback" not in out, out[-200:])
    check("сказано про отсутствие графики", "Графическое окружение" in out, out[:120])
    check("предложен консольный вариант", "linux/pcmcia_flash.py" in out, out[:200])
    check("код возврата 2", r.returncode == 2, str(r.returncode))


def test_tmp_isolation():
    print("\n[U8] Временные файлы в приватном каталоге 0700")
    d = g.private_tmpdir()
    try:
        st = os.stat(d)
        check("права каталога 0700", (st.st_mode & 0o777) == 0o700, oct(st.st_mode))
        check("каталог свой у каждого вызова", g.private_tmpdir() != d)
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)
    src = open(os.path.join(KIT, "pcmcia_gui.py"), encoding="utf-8").read()
    code = [line for line in src.splitlines()
            if "tempfile.mktemp(" in line and not line.strip().startswith("#")
            and "tempfile.mktemp() возвращает" not in line]
    check("tempfile.mktemp() в коде не осталось", not code, str(code))


def test_speed_and_cli():
    print("\n[U9] Скорость карты блоков и работа без интерфейса")
    setup(delay=0)
    # карта из 128 блоков по 128 КиБ (как настоящая 16-МБ)
    big = os.path.join(STUB, "big.bin")
    with open(big, "wb") as f:
        f.write(b"\xff" * (16 * 1024 * 1024))
    c = g.Card(big, quick=True)
    c.erasesize = 131072
    c.size = 16 * 1024 * 1024
    calls = []
    real_exec = g.Runner.exec

    def counting(self, cmd, timeout=3600):
        calls.append(cmd)
        return real_exec(self, cmd, timeout)

    g.Runner.exec = counting
    try:
        import time as _t
        t0 = _t.time()
        states = c.block_map(g.Runner())
        dt = _t.time() - t0
    finally:
        g.Runner.exec = real_exec
    check("карта блоков собрана", len(states) == 128, str(len(states)))
    check("все блоки определены как стёртые", states.count("erased") == 128)
    check("запусков утилиты не больше 20 (было 128)", len(calls) <= 20, f"{len(calls)}")
    check("по времени быстрее 3 с", dt < 3.0, f"{dt:.2f} c")

    # карта блоков по локальному образу — без единого запуска утилиты
    calls.clear()
    g.Runner.exec = counting
    try:
        st2 = c.block_map_from_file(big)
    finally:
        g.Runner.exec = real_exec
    check("по файлу блоков столько же", len(st2) == 128, str(len(st2)))
    check("по файлу утилиты не запускались", len(calls) == 0, str(len(calls)))

    # состояние интерфейса сохраняется и восстанавливается
    g.CFG_DIR = os.path.join(STUB, "cfgstate")
    app = g.App()
    app.var_off.set("0040")
    app.var_len.set("512")
    app.var_verify.set(False)
    app.save_ui_state()
    app.destroy()
    saved = g.load_profile()
    check("состояние сохранено", saved.get("offset") == "0040" and saved.get("verify") is False,
          str(saved))
    app2 = g.App()
    check("состояние восстановлено", app2.var_off.get() == "0040",
          app2.var_off.get())
    app2.destroy()
    setup(delay=0)


def test_image_safety():
    print("\n[U10] Защита от записи не того файла")
    setup(delay=0)
    # 1) анализ образа
    cases = [("zeros.bin", b"\x00" * 200000, "ПУСТОЙ", True, False),
             ("ffs.bin", b"\xff" * 200000, "0xFF", False, True),
             ("fat.bin", b"\xeb\x3c\x90MSDOS5.0" + b"\x00" * 500, "FAT", False, False),
             ("raw.bin", bytes(range(256)) * 800, "двоичные", False, False)]
    for name, data, kind, is_zero, is_ff in cases:
        pth = os.path.join(STUB, name)
        with open(pth, "wb") as f:
            f.write(data)
        r = g.image_report(pth)
        check(f"{name}: определён как «{kind}»", kind.lower() in r["kind"].lower(), r["kind"])
        check(f"{name}: all_zero={is_zero}, all_ff={is_ff}",
              r["all_zero"] is is_zero and r["all_ff"] is is_ff,
              f"zero={r['all_zero']} ff={r['all_ff']}")
        check(f"{name}: первые байты показаны", len(r["head"]) > 10, r["head"])

    # 2) сравнение с картой
    c = g.Card(CARD, quick=True)
    c.size = SIZE
    same = os.path.join(STUB, "same.bin")
    with open(same, "wb") as f:
        f.write(b"\xAA" * SIZE)
    check("образ, равный карте, распознан",
          g.compare_with_card(c, same, g.Runner()) is True)
    tail = os.path.join(STUB, "taildiff.bin")
    with open(tail, "wb") as f:
        f.write(b"\xAA" * (SIZE - 1024) + b"\x22" * 1024)
    check("различие в конце замечено", g.compare_with_card(c, tail, g.Runner()) is False)
    head = os.path.join(STUB, "headdiff.bin")
    with open(head, "wb") as f:
        f.write(b"\x99" * 1024 + b"\xAA" * (SIZE - 1024))
    check("различие в начале замечено", g.compare_with_card(c, head, g.Runner()) is False)
    setup(delay=0)


def test_not_a_file():
    print("\n[U12] Бесконечные «файлы» не вешают программу (/dev/zero и подобные)")
    import subprocess as sp
    kit = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for dev in ("/dev/zero", "/dev/null", "/dev/urandom"):
        if not os.path.exists(dev):
            continue
        try:
            r = sp.run([sys.executable, os.path.join(kit, "pcmcia_gui.py"),
                        "--map-file", dev], capture_output=True, text=True, timeout=20)
            check(f"{dev} отвергнут без зависания", r.returncode == 2, f"код {r.returncode}")
            check(f"{dev}: понятное сообщение", "не обычный файл" in (r.stdout + r.stderr),
                  (r.stdout + r.stderr)[:80])
        except sp.TimeoutExpired:
            check(f"{dev} отвергнут без зависания", False, "ЗАВИС")
    # и в библиотеке
    check("block_map_from_file на устройстве возвращает пусто",
          g.Card(CARD, quick=True).block_map_from_file("/dev/zero") == [])
    try:
        g.image_report("/dev/zero")
        check("image_report отвергает устройство", False, "не отказался")
    except RuntimeError as e:
        check("image_report отвергает устройство", "не обычный файл" in str(e), str(e))


def test_hotkeys_and_rescan():
    print("\n[U11] Горячие клавиши и автоперерисовка карты блоков")
    setup(delay=0)
    app = g.App()
    app.update()
    warns = []
    g.messagebox.showwarning = lambda *a, **k: warns.append(a)
    g.messagebox.showerror = lambda *a, **k: warns.append(a)
    # F1 открывает справку
    before = len(app.winfo_children())
    app.show_help()
    app.update()
    check("справка открывается отдельным окном", len(app.winfo_children()) > before)
    texts = []

    def walk(w):
        try:
            texts.append(w.get("1.0", "end"))
        except Exception:                                         # noqa: BLE001
            pass
        for ch in w.winfo_children():
            walk(ch)

    for ch in app.winfo_children():
        walk(ch)
    check("в справке перечислены клавиши",
          any("Ctrl+R" in t and "F5" in t for t in texts),
          f"найдено текстов: {len(texts)}")
    # привязки клавиш существуют
    check("F5 привязана к обновлению", bool(app.bind_all("<F5>")))
    check("Ctrl+R привязана к чтению", bool(app.bind_all("<Control-r>")))
    check("Esc привязан к отмене", bool(app.bind_all("<Escape>")))
    # карта блоков перерисовывается после записи
    app._map_states = ["used"] * 4
    scanned = []
    app.scan_map = lambda: scanned.append(1)
    app._op_finished({"tail": "остаток стёрт", "verified": True}, None)
    pump(app, 0.3)
    check("после записи карта блоков обновляется сама", len(scanned) == 1, str(scanned))
    scanned.clear()
    app._map_states = ["used"] * 4
    app._op_finished({"path": "/tmp/x"}, None)
    pump(app, 0.3)
    check("после операций без изменения карты не перерисовывается",
          len(scanned) == 0, str(scanned))
    app.destroy()
    setup(delay=0)


def test_doctor_slot():
    print("\n[U14] --doctor: цепочка «мост/слот/CIS/драйвер» и вердикт")
    import subprocess as sp
    kit = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = sp.run([sys.executable, os.path.join(kit, "pcmcia_gui.py"), "--doctor"],
               capture_output=True, text=True, timeout=90)
    out = r.stdout + r.stderr
    check("доктор не падает", "Traceback" not in out, out[-200:])
    check("есть блок «Слот и CIS»", "Слот и CIS:" in out, out[-300:])
    for key in ("мост CardBus", "pccardctl", "CIS (ident)", "ВЕРДИКТ"):
        check(f"строка «{key}»", key in out, out[-300:])
    check("результат — одно из осмысленных состояний",
          any(k in out for k in ("НЕ НАЙДЕН", "драйвер", "не ответил",
                                 "карта на месте", "модуль pcmciamtd")), out[-300:])


def test_doctor_high_memory():
    print("\n[U15] --doctor: «no high memory space available» не выдаётся за проблему CIS")
    # подменяем вывод команд: слот карту видит, ядру не хватило окон памяти
    fake = {
        ("lspci", "-nn"): "00:0c.0 CardBus bridge [0607]: Texas Instruments "
                          "PCIxx21/PCIxx11/PCIx515 PC Card Controller [104c:8031]\n",
        ("lspci", "-v", "00:0c.0"): "\tKernel driver in use: yenta_cardbus\n",
        ("pccardctl", "status"): "Socket 0:\n 5.0V\n 16-bit\n PC Card\n",
        ("pccardctl", "ident"): "Socket 0:\n  no product info available\n",
        ("dmesg",): "pcmcia_socket0: cs: warning: no high memory space available!\n",
        ("journalctl", "-k", "-n", "400", "--no-pager"): "",
    }
    orig_sub, orig_mtd = g.subprocess.run, g.MTD

    class R:
        def __init__(self, out):
            self.stdout, self.stderr, self.returncode = out, "", 0

    g.subprocess.run = lambda cmd, **kw: R(fake.get(tuple(cmd), ""))
    g.MTD = os.path.join(STUB, "нет-такого-mtd")
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            g.doctor_slot()
    finally:
        g.subprocess.run, g.MTD = orig_sub, orig_mtd
    out = buf.getvalue()
    check("вердикт про окна памяти", "НЕТ годного окна памяти" in out, out[-300:])
    check("сказано, что ident — следствие, а не причина", "следствие" in out, out[-300:])
    check("force_size не предлагается", "force_size=16" not in out, out[-200:])
    check("отправлено к --diag", "--diag" in out, out[-200:])


def main():
    print("=" * 70)
    print("ТЕСТЫ ОТЗЫВЧИВОСТИ И ЖИВУЧЕСТИ ИНТЕРФЕЙСА")
    print("=" * 70)
    if not g._has_tk():
        print("нужен tkinter"); sys.exit(2)
    for fn in (test_fast_start, test_buttons_not_blocked, test_cancel_does_not_freeze,
               test_pump_survives_error, test_close_during_op, test_cancel_warning,
               test_cli, test_tmp_isolation, test_no_display, test_speed_and_cli,
               test_image_safety, test_hotkeys_and_rescan, test_not_a_file,
               test_doctor_slot, test_doctor_high_memory):
        fn()
    print("\n" + "=" * 70)
    print(f"ИТОГО: прошло {len(PASS)}, провалено {len(FAIL)}")
    if FAIL:
        for n, d in FAIL:
            print(f"  - {n}: {d}")
        sys.exit(1)
    print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ")


if __name__ == "__main__":
    main()
