#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Функциональные тесты PCMCIA Flash Tool 2.0 (набор «утилиты + интерфейс»).

Запуск:  xvfb-run -a python3 tests/test_gui.py

Дополняет tests/test_v2.py: там проверяются пункты аудита (отмена, WP,
отчёты, карта блоков…), здесь — классификация CIS, разбор окон слота,
форматирование, виджеты и автопоиск офлайн-пакетов.

Карта не нужна: вместо /dev/mtd0 подставляется файл на 16 МБ, утилиты
mtd_debug/flashcp/flash_erase/pccardctl/lspci — заглушки во временном PATH.
"""
import os
import sys
import shutil
import tempfile

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KIT)

import pcmcia_gui as g                                            # noqa: E402

g.os.geteuid = lambda: 0        # тесты идут от «root», иначе GUI справедливо требует sudo

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ok   {name}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL {name}  {detail}")


STUB_DIR = tempfile.mkdtemp(prefix="stubs-gui-")
CARD = os.path.join(STUB_DIR, "card.bin")
CARD_SIZE = 16 * 1024 * 1024


def install_stubs():
    with open(CARD, "wb") as f:
        f.write(b"\xAA" * CARD_SIZE)

    def script(name, body):
        p = os.path.join(STUB_DIR, name)
        # Прямая перезапись исполняемого файла даёт ETXTBSY («Text file busy»),
        # если прошлый поддельный процесс ещё жив: так упал extras в CI
        # 2026-11-07. Пишем свежий файл и заменяем им старый - работающий
        # процесс остаётся на прежнем inode и не мешает.
        fresh = p + ".new"
        with open(fresh, "w") as f:
            f.write("#!/bin/bash\n" + body + "\n")
        os.chmod(fresh, 0o755)
        os.replace(fresh, p)

    script("mtd_debug", f'''
case "$1" in
  info)
    echo "mtd.type = MTD_NORFLASH"
    echo "mtd.flags = MTD_CAP_NORFLASH"
    echo "mtd.size = {CARD_SIZE} (16M)"
    echo "mtd.erasesize = 65536 (64K)"
    echo "mtd.writesize = 1"
    echo "region[0].offset = 0x00000000"
    echo "region[0].erasesize = 65536"
    echo "region[0].numblocks = 256"
    ;;
  read)
    python3 -c "
import sys
off, n = int(sys.argv[1]), int(sys.argv[2])
with open('{CARD}','rb') as f:
    f.seek(off); d = f.read(n)
open(sys.argv[3],'wb').write(d)
" "$3" "$4" "$5" ;;
  write)
    python3 -c "
import sys
with open('{CARD}','r+b') as f:
    f.seek(int(sys.argv[1])); f.write(open(sys.argv[2],'rb').read())
" "$3" "$5" ;;
esac
''')
    script("flashcp", f'''
if [ "$1" = "-A" ]; then shift; fi
python3 -c "
import sys
src = sys.argv[1]
d = open(src,'rb').read()
with open('{CARD}','r+b') as f:
    f.seek(0); f.write(d)
" "$1"
''')
    script("flash_erase", f'''
python3 -c "
with open('{CARD}','r+b') as f: f.seek(0); f.write(b'\\\\xff'*{CARD_SIZE})
"
''')
    script("pccardctl", '''
case "$1" in
  ident)
    echo 'PRODID_1="intel"'; echo 'PRODID_2="SERIES2-20 "'; echo 'PRODID_3="S2E8 SW"'
    echo "MANFID=0089,8422"; echo "FUNCID=1" ;;
  status) echo "Socket 0:"; echo "  5.0V 16-bit PC Card" ;;
esac
''')
    script("lspci", '''
if [ "$1" = "-nn" ]; then
  echo "00:0c.0 CardBus bridge [0607]: Texas Instruments PCIxx21 [104c:8031]"
else
  echo "Memory window 0: a2000000-a3ffffff (prefetchable)"
  echo "Memory window 1: a4080000-a40fffff"
  echo "I/O window 0: 00003000-000030ff"
  echo "Bus: primary=00, secondary=01, subordinate=01, sec-latency=64"
fi
''')
    script("modprobe", "exit 0")
    script("dpkg", "echo amd64")
    os.environ["PATH"] = STUB_DIR + os.pathsep + os.environ["PATH"]
    g.MTD = CARD


# ---------------------------------------------------------------- тесты
def test_classify():
    print("\n[1] Классификация карт по CIS")
    cases = [
        (("intel", "SERIES2-02", "2H REGBASE"), "Intel Series 2 (Vpp под вопросом)"),
        (("intel", "SERIES2-20 ", "S2E8 SW"), "Intel Series 2+ / Value"),
        (("intel", "S2E8 SW", "COPYRIGHT"), "Intel Series 2+ / Value"),
        (("intel", "VALUE 100", ""), "Intel Value Series 100"),
        (("AMD", "Am29F", ""), "AMD linear flash"),
        (("Centennial", "SRAM 2MB", ""), "SRAM-карта"),
        (("", "", ""), "неизвестно"),
    ]
    for ids, expected in cases:
        c = g.Card.__new__(g.Card)
        c.ident = {"PRODID_1": ids[0], "PRODID_2": ids[1], "PRODID_3": ids[2]}
        got = c.card_kind()[0]
        check(f"CIS {ids[1]!r:22} -> {expected}", expected in got, f"получено: {got}")

    c = g.Card.__new__(g.Card)
    c.ident = {"PRODID_2": "SERIES2-20 ", "PRODID_3": "S2E8 SW"}
    check("S2E8 SW не считается 12-вольтовой",
          c.card_kind()[0] != "Intel Series 2 (Vpp под вопросом)", c.card_kind()[0])


def test_human():
    print("\n[2] Форматирование размеров")
    for v, exp in [(0, "0 Б"), (512, "512 Б"), (1024, "1.00 КиБ"),
                   (16777216, "16.00 МиБ"), (33554432, "32.00 МиБ")]:
        check(f"human({v}) = {exp}", g.human(v) == exp, f"получено {g.human(v)}")
    check("human(None) не падает", isinstance(g.human(None), str))


def test_md5():
    print("\n[3] MD5")
    p = os.path.join(STUB_DIR, "h.bin")
    with open(p, "wb") as f:
        f.write(b"abc")
    import hashlib
    check("md5_of('abc')", g.md5_of(p) == hashlib.md5(b"abc").hexdigest())
    check("md5_of с limit", g.md5_of(p, limit=2) == hashlib.md5(b"ab").hexdigest())


def test_card_state():
    print("\n[4] Состояние карты (виртуальная карта 16 МБ)")
    c = g.Card(CARD)
    check("карта найдена", c.present, f"dev={c.dev}")
    check("размер 16 МиБ", c.size == CARD_SIZE, f"получено {c.size}")
    check("блок 64 КиБ", c.erasesize == 65536, f"получено {c.erasesize}")
    check("тип MTD_NORFLASH", c.mtdtype == "MTD_NORFLASH", c.mtdtype)
    check("модель определена", "Series 2+" in c.card_kind()[0], c.card_kind()[0])
    check("MANFID прочитан", c.ident.get("MANFID") == "0089,8422", str(c.ident))
    check("режим тренировки на файле распознан", c.simulated is True, str(c.simulated))
    check("предупреждения только про тренировку",
          all("ТРЕНИРОВКИ" in w for w in c.warnings()), str(c.warnings()))


def test_no_card_window():
    print("\n[5] Поведение без карты")
    old = g.MTD
    g.MTD = "/dev/mtd_does_not_exist"
    c = g.Card()
    check("present=False", c.present is False)
    check("warnings сообщает о карте", any("не распознана" in w for w in c.warnings()),
          str(c.warnings()))
    check("size=0", c.size == 0)
    g.MTD = old


def test_ops_read_write():
    print("\n[6] Чтение / запись / сверка / стирание (ядро Ops)")
    c = g.Card(CARD)
    ops = g.Ops(c, g.Runner())
    out = os.path.join(STUB_DIR, "img.bin")
    r = ops.read_image(out)
    check("образ прочитан целиком", os.path.getsize(out) == CARD_SIZE, str(r))
    check("md5 образа совпадает с картой", r["md5"] == g.md5_of(CARD), r.get("md5"))
    check("создан .md5-файл", os.path.exists(out + ".md5"))
    r2 = ops.verify(out)
    check("сверка образа с картой проходит", r2["verified"] is True, str(r2))
    # пишем другой образ, стирая всю карту
    src = os.path.join(STUB_DIR, "new.bin")
    with open(src, "wb") as f:
        f.write(b"\x12" * (CARD_SIZE // 2))
    ops.write_image(src, erase_all=True, verify=True)
    with open(CARD, "rb") as f:
        head = f.read(16)
        f.seek(CARD_SIZE - 16)
        tail = f.read(16)
    check("образ записан в начало", head == b"\x12" * 16, head.hex())
    check("хвост карты стёрт", tail == b"\xff" * 16, tail.hex())
    ops.erase()
    with open(CARD, "rb") as f:
        f.seek(CARD_SIZE // 2)
        mid = f.read(64)
    check("стирание заполняет карту 0xFF", mid == b"\xff" * 64, mid.hex())


def test_config_windows():
    print("\n[7] Разбор окон слота из lspci и запись config.opts")
    c = g.Card(CARD)
    ops = g.Ops(c, g.Runner())
    g.CFG_OPTS = os.path.join(STUB_DIR, "etc/pcmcia/config.opts")
    g.MODPROBE_CONF = os.path.join(STUB_DIR, "etc/modprobe.d/pcmciamtd.conf")
    g.UDEV_RULES = os.path.join(STUB_DIR, "etc/udev/rules.d/99-pcmciamtd.rules")
    res = g._configure_slot(ops, "50")
    wins = res.get("windows", [])
    check("окно памяти 0 округлено вниз до 1 МиБ",
          "include memory 0xa2000000-0xa3ffffff" in wins, str(wins))
    check("окно памяти 1 округлено (a4080000 -> a4000000)",
          "include memory 0xa4000000-0xa40fffff" in wins, str(wins))
    check("порты не округляются (0x3000-0x30ff)",
          "include port 0x3000-0x30ff" in wins, str(wins))
    check("все 3 окна из lspci разобраны", len(wins) == 3, str(wins))
    body = open(g.CFG_OPTS, encoding="utf-8").read()
    check("в config.opts записан include memory", "include memory 0xa2000000" in body)
    check("опции Vpp записаны в modprobe.d",
          "options pcmciamtd vpp=50 setvpp=1" in open(g.MODPROBE_CONF, encoding="utf-8").read())
    check("udev-правило записано",
          "GROUP=\"flash\"" in open(g.UDEV_RULES, encoding="utf-8").read())
    check("карта после настройки распознана", res.get("card") is True, str(res.get("card")))
    check("Vpp отражён в результате", str(res.get("vpp")) == "50", str(res.get("vpp")))


def test_gui_widgets():
    print("\n[8] Целостность GUI")
    if not g._has_tk():
        check("tkinter доступен", False)
        return
    app = g.App()
    try:
        app.update()
        check("5 вкладок", len(app.nb.tabs()) == 5, str(len(app.nb.tabs())))
        names = [app.nb.tab(t, "text") for t in app.nb.tabs()]
        check("названия вкладок", names == ["Карта", "Просмотр / правка", "Блоки",
                                            "Настройка", "Журнал"], str(names))
        app.card = g.Card(CARD)
        app._render_state()
        state = app.txt_state.get("1.0", "end")
        check("состояние показывает размер", "16.00 МиБ" in state, state[:200])
        check("состояние показывает модель", "Series 2+" in state, state[:300])
        check("в состоянии есть режим тренировки", "ТРЕНИРОВКИ" in state, state[:400])
        check("в состоянии нет «не найдена»", "не найдена" not in state, state[:120])
        app._render_tools()
        check("утилиты определены", app.lbl_tools.cget("text") != "",
              app.lbl_tools.cget("text"))
        check("кнопка отмены в покое заблокирована",
              str(app.btn_cancel["state"]) == "disabled")
        check("есть поле для профиля/монтирования", hasattr(app, "var_mount"))
    finally:
        app.destroy()


def test_find_debs():
    print("\n[9] Автопоиск офлайн-пакетов (папка / tar.gz / cpio.gz)")
    old_cwd = os.getcwd()
    orig_file = g.__file__
    os.chdir(tempfile.mkdtemp(prefix="nowhere-"))
    try:
        # 9.1 обычная папка debs рядом
        d = tempfile.mkdtemp(prefix="kitdir-")
        os.makedirs(os.path.join(d, "debs"))
        open(os.path.join(d, "debs", "x.deb"), "w").close()
        g.__file__ = os.path.join(d, "pcmcia_gui.py")
        got = g._find_debs()
        check("папка debs/ рядом найдена", got == os.path.join(d, "debs"), str(got))

        # 9.2 архив tar.gz рядом (как на флешке)
        d2 = tempfile.mkdtemp(prefix="kitar-")
        kit = os.path.join(d2, "pcmcia-flash-kit")
        os.makedirs(os.path.join(kit, "debs"))
        open(os.path.join(kit, "debs", "y.deb"), "w").close()
        g.run(["tar", "-czf", os.path.join(d2, "pcmcia-flash-kit.tar.gz"),
               "-C", d2, "pcmcia-flash-kit"])
        g.__file__ = os.path.join(d2, "pcmcia_gui.py")
        got = g._find_debs()
        check("архив tar.gz распознан", bool(got) and got.endswith("debs"), str(got))
        check("в распакованном архиве есть .deb",
              bool(got) and os.path.exists(os.path.join(got, "y.deb")), str(got))

        # 9.3 ничего нет
        d3 = tempfile.mkdtemp(prefix="kitempty-")
        g.__file__ = os.path.join(d3, "pcmcia_gui.py")
        check("без пакетов возвращает None", g._find_debs() is None, str(g._find_debs()))
    finally:
        g.__file__ = orig_file
        os.chdir(old_cwd)
        for x in ("d", "d2", "d3"):
            shutil.rmtree(locals().get(x, "/nonexistent"), ignore_errors=True)


def test_path_and_lock():
    print("\n[10] PATH и единственный экземпляр")
    check("PATH без world-writable каталогов",
          isinstance(g.path_is_sane(), list), str(g.path_is_sane()))
    g.LOCK_PATH = os.path.join(STUB_DIR, "l.lock")
    h1, _ = g.acquire_single_instance()
    h2, why = g.acquire_single_instance()
    check("первый экземпляр блокировку получил", h1 is not None)
    check("второй — нет, с пояснением", h2 is None and "запущен" in why, why)
    if h1:
        h1.close()


def t_direct_write():
    """Проба и запись БЕЗ драйвера из программы (кнопка и Ops)."""
    print("\n[G7] Проба записи без драйвера: кнопка, предупреждение, вызов утилиты")
    src = open(os.path.join(KIT, "pcmcia_gui.py"), encoding="utf-8").read()
    for frag in ("def direct_probe(", "def direct_write(", "def direct_script(",
                 "def direct_probe_dialog(", '"--write-probe"', '"--write"'):
        check(f"в программе есть {frag}", frag in src, "")
    check("кнопка пробы объясняет, что данные сектора вернутся",
          "возвращён на место" in src, "")
    check("кнопка пробы предупреждает, что вернуть может не удаться",
          "вернуть байты не удастся" in src, "")
    check("кнопка пробы честно отказывает без windows/legacy/pcmcia_direct.py",
          "нет windows/legacy/pcmcia_direct.py" in src, "")

    class FakeRunner:
        def __init__(self):
            self.calls = []

        def exec(self, cmd, timeout=3600):
            self.calls.append(cmd)
            return 0, "ИТОГ: карта ПИШЕТСЯ.", ""

    class FakeCard:
        present = True
        dev = "/dev/mtd0"
        size = 4 << 20
        writeable = True
        is_mtd = True
        simulated = False

        def refresh(self):
            return None

    logs = []
    r = FakeRunner()
    ops = g.Ops(FakeCard(), r, log=logs.append, progress=lambda p: None,
                slow=lambda t: None)
    ops.direct_script = lambda: "/tmp/fake-direct.py"
    res = ops.direct_probe()
    check("Ops: проба вызывает pcmcia_direct.py --write-probe",
          bool(r.calls) and "--write-probe" in r.calls[-1], str(r.calls[-1:]))
    check("Ops: успех пробы возвращается как ok", res["ok"], str(res))
    check("Ops: в журнал попало «карта пишется»",
          any("карта пишется" in m for m in logs), str(logs[-2:]))
    img = os.path.join(STUB_DIR, "gui-direct.bin")
    with open(img, "wb") as f:
        f.write(b"\x00" * 4096)
    res2 = ops.direct_write(img)
    check("Ops: запись вызывает pcmcia_direct.py --write <файл>",
          "--write" in r.calls[-1] and img in r.calls[-1], str(r.calls[-1:]))
    check("Ops: успех записи возвращается как ok", res2["ok"], str(res2))
    ops2 = g.Ops(FakeCard(), FakeRunner(), log=logs.append,
                 progress=lambda p: None, slow=lambda t: None)
    ops2.direct_script = lambda: ""
    try:
        ops2.direct_probe()
        check("Ops: без pcmcia_direct.py проба падает с понятной ошибкой", False, "")
    except RuntimeError as e:
        check("Ops: без windows/legacy/pcmcia_direct.py проба падает с понятной ошибкой",
              "windows/legacy/pcmcia_direct.py" in str(e), str(e))


def main():
    print("=" * 70)
    print("ФУНКЦИОНАЛЬНЫЕ ТЕСТЫ: PCMCIA Flash Tool 2.0 (утилиты + интерфейс)")
    print("=" * 70)
    install_stubs()
    test_classify()
    test_human()
    test_md5()
    test_no_card_window()
    test_card_state()
    test_ops_read_write()
    test_config_windows()
    test_gui_widgets()
    test_find_debs()
    test_path_and_lock()
    t_direct_write()

    print("\n" + "=" * 70)
    print(f"ИТОГО: прошло {len(PASS)}, провалено {len(FAIL)}")
    if FAIL:
        print("\nПровалы:")
        for n, d in FAIL:
            print(f"  - {n}: {d}")
        sys.exit(1)
    print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ")
    shutil.rmtree(STUB_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()
