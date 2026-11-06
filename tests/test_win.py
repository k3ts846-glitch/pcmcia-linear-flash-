#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверки Windows-части пульта: пакет src/pcmcia_kit.

Windows-часть раньше была сценариями PowerShell, и её можно было проверить
только на настоящем PowerShell. Теперь это Python-пакет, и проверять можно на
любой машине: ssh и scp подменяются поддельными программами во временной
папке, сеть не нужна, карта не нужна.

    python3 tests/test_win.py            все проверки
    python3 tests/test_win.py -v         то же, с подробностями

Коды возврата: 0 — всё прошло, 1 — есть провалы.
"""
import ast
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(HERE)
SRC = os.path.join(KIT, "src")
sys.path.insert(0, SRC)

from pcmcia_kit import config, remote, state, sync            # noqa: E402
from pcmcia_kit.transport import Target, explain              # noqa: E402

PASS = []
FAIL = []
WORK = tempfile.mkdtemp(prefix="win-test-")


def check(name, condition, detail=""):
    """Отметить одну проверку."""
    if condition:
        PASS.append(name)
        print(f"  ok   {name}")
    else:
        FAIL.append((name, detail))
        text = f"   {detail}" if detail else ""
        print(f"  FAIL {name}{text}")


def section(title):
    print(f"\n[{title}]")


# ---------------------------------------------------------------------------
# Поддельные ssh и scp: пишут журнал вызовов и отвечают по заданному режиму.
# ---------------------------------------------------------------------------
FAKE_SSH = r"""#!{python}
import sys, pathlib
work = pathlib.Path({work!r})
args = sys.argv[1:]
with open(work / "calls.txt", "a", encoding="utf-8") as log:
    log.write("ssh " + " ".join(args) + "\n")
cmd = args[-1] if args else ""
mode_file = work / "mode.txt"
mode = mode_file.read_text(encoding="utf-8").strip() if mode_file.exists() else "ok"
if mode == "denied":
    sys.stderr.write("root@192.168.0.94: Permission denied (publickey,password).\n")
    sys.exit(255)
if mode == "down":
    sys.stderr.write("ssh: connect to host 192.168.0.94 port 22: Connection timed out\n")
    sys.exit(255)
if "md5sum" in cmd:
    answers = work / "md5.txt"
    if answers.exists():
        sys.stdout.write(answers.read_text(encoding="utf-8"))
    sys.exit(0)
if "echo KEY_OK" in cmd:
    sys.stdout.write("KEY_OK\nroot\n")
    sys.exit(0)
out = work / "remote-out.txt"
if out.exists():
    sys.stdout.write(out.read_text(encoding="utf-8"))
sys.exit(0)
"""

FAKE_SCP = r"""#!{python}
import sys, pathlib
work = pathlib.Path({work!r})
args = sys.argv[1:]
with open(work / "calls.txt", "a", encoding="utf-8") as log:
    log.write("scp " + " ".join(args) + "\n")
mode_file = work / "mode.txt"
mode = mode_file.read_text(encoding="utf-8").strip() if mode_file.exists() else "ok"
if mode == "sftp":
    sys.stderr.write("subsystem request failed on channel 0\n")
    sys.exit(255)
if mode == "denied":
    sys.stderr.write("Permission denied, please try again.\n")
    sys.exit(255)
dest = args[-1] if args else ""
if ":" not in dest and not dest.endswith("/"):
    pathlib.Path(dest).write_bytes(b"image-from-notebook")
sys.exit(0)
"""


def make_fakes():
    """Создать поддельные ssh/scp и вернуть путь к папке с ними."""
    bindir = os.path.join(WORK, "bin")
    os.makedirs(bindir, exist_ok=True)
    for name, template in (("ssh", FAKE_SSH), ("scp", FAKE_SCP)):
        path = os.path.join(bindir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(template.format(python=sys.executable, work=WORK))
        os.chmod(path, 0o755)
    return bindir


def fake_env(bindir):
    env = dict(os.environ)
    env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    env["PYTHONPATH"] = SRC
    return env


def reset_fakes(mode="ok", md5_text=None, remote_out=None):
    """Сбросить управляющие файлы поддельных программ."""
    for name in ("calls.txt", "md5.txt", "remote-out.txt"):
        path = os.path.join(WORK, name)
        if os.path.exists(path):
            os.remove(path)
    with open(os.path.join(WORK, "mode.txt"), "w", encoding="utf-8") as handle:
        handle.write(mode)
    if md5_text is not None:
        with open(os.path.join(WORK, "md5.txt"), "w", encoding="utf-8") as handle:
            handle.write(md5_text)
    if remote_out is not None:
        with open(os.path.join(WORK, "remote-out.txt"), "w", encoding="utf-8") as handle:
            handle.write(remote_out)


def calls():
    """Журнал вызовов поддельных ssh/scp."""
    path = os.path.join(WORK, "calls.txt")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [line.strip() for line in handle if line.strip()]


def run_cli(args, env):
    """Запустить пульт (как программу) с поддельными ssh/scp.

    Рабочий каталог — временная папка, а папка набора указывается явно: иначе
    отчёты и образы, которые пульт забирает «к себе», падали бы в сам набор и
    попадали в архив.
    """
    return subprocess.run([sys.executable, "-m", "pcmcia_kit"] + args,
                          capture_output=True, text=True, env=env, cwd=WORK)


# ---------------------------------------------------------------------------
def t_layout():
    section("W1 раскладка набора: пакет по стандарту, без .cmd и PowerShell")
    check("есть pyproject.toml (сборка и метаданные в одном месте)",
          os.path.isfile(os.path.join(KIT, "pyproject.toml")), "")
    text = open(os.path.join(KIT, "pyproject.toml"), encoding="utf-8").read()
    check("пакет описан по src-layout", 'package-dir = { "" = "src" }' in text, "")
    check("есть точка входа pcmcia-kit ([project.scripts])",
          "pcmcia-kit = \"pcmcia_kit.cli:main\"" in text, "")
    check("есть графическая точка входа ([project.gui-scripts])",
          "[project.gui-scripts]" in text and "pcmcia_kit.gui:main" in text, "")
    check("версия берётся из пакета, а не дублируется",
          'version = { attr = "pcmcia_kit.__version__" }' in text, "")
    check("сторонних зависимостей нет (dependencies = [])",
          "dependencies = []" in text, "")

    for name in ("cli.py", "config.py", "console.py", "transport.py", "sync.py",
                 "state.py", "remote.py", "entry.py", "gui.py", "keyring.py",
                 "__main__.py", "__init__.py"):
        check(f"src/pcmcia_kit/{name} на месте",
              os.path.isfile(os.path.join(SRC, "pcmcia_kit", name)), "")

    for name in ("__main__.py", "cli.py"):
        check(f"src/pcmcia_kit/{name} не пустой",
              os.path.getsize(os.path.join(SRC, "pcmcia_kit", name)) > 200, "")

    # ни одного .cmd в рабочем пути
    top = [n for n in os.listdir(KIT) if n.lower().endswith((".cmd", ".ps1", ".bat"))]
    check("в корне набора нет .cmd, .bat и .ps1", top == [], str(top))
    check("файл двойного щелчка — .pyw (Windows запускает без окна консоли)",
          os.path.isfile(os.path.join(KIT, "pcmcia-kit.pyw")), "")
    launch = open(os.path.join(KIT, "pcmcia-kit.pyw"), encoding="utf-8").read()
    # В пояснении слово «PowerShell» допустимо («он больше не нужен»).
    # Проверяем не текст, а настоящий код: разбираем файл и смотрим, какие
    # модули он импортирует и какие функции вызывает.
    launch_tree = ast.parse(launch)
    imported = set()
    for node in ast.walk(launch_tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    check("запуск .pyw не импортирует subprocess (ничего не запускает)",
          "subprocess" not in imported, str(sorted(imported)))
    called = set()
    for node in ast.walk(launch_tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            called.add(name)
    check("запуск .pyw не вызывает os.system/popen",
          not ({"system", "popen", "startfile"} & called), str(sorted(called)))
    check(".pyw импортирует пакет и вызывает main()",
          "from pcmcia_kit.entry import main" in launch and "main()" in launch, "")
    check(".pyw добавляет src в путь (работает без установки пакета)",
          '"src"' in launch and "sys.path.insert" in launch, "")


def t_commands():
    section("W2 команды для ноутбука: пункт меню -> команда")
    check("info опрашивает карту и ничего не пишет",
          remote.build("info") == f"python3 {config.REMOTE_DIR}/pcmcia_ctl.py info --json",
          remote.build("info"))
    check("chip — только опрос микросхемы", " chip" in remote.build("chip"), "")
    check("selftest по умолчанию быстрый",
          remote.build("selftest").endswith("selftest --fast"), remote.build("selftest"))
    check("selftest --full гоняет полный прогон",
          remote.build("selftest", full=True).endswith("selftest"), "")
    check("erase без --yes спросит на ноутбуке",
          remote.build("erase").endswith("erase"), "")
    check("erase --yes подтверждение уже дано",
          remote.build("erase", assume_yes=True).endswith("erase --yes"), "")
    check("путь с пробелами ставится в кавычки",
          "'/root/образ с пробелом.bin'" in remote.build("write", "/root/образ с пробелом.bin"),
          "")
    check("write/fwwrite/erase отмечены как изменяющие карту",
          all(remote.is_destructive(a) for a in config.DESTRUCTIVE)
          and not remote.is_destructive("info"), "")
    check("файл образа нужен только write/verify/fwwrite",
          all(remote.needs_image(a) for a in config.NEEDS_IMAGE)
          and not remote.needs_image("read"), "")
    check("неизвестная команда даёт пустую строку", remote.build("выдумка") == "", "")
    check("путь образа для чтения содержит дату и время",
          remote.default_remote_image("read", stamp="20260101-1200")
          == "/root/card-20260101-1200.bin", "")
    check("путь образа для записи постоянный",
          remote.default_remote_image("write") == "/root/card-write.bin", "")
    for _number, action, _title in config.MENU_ITEMS:
        if action in config.LOCAL_ACTIONS:
            check(f"пункт меню {action}: выполняется на этой машине, без ноутбука",
                  remote.build(action) == "", "")
        else:
            check(f"пункт меню {action}: команда для ноутбука есть",
                  bool(remote.build(action)), "")
    check("номера меню — как у прежнего пульта (1..12 и 13 — ключ)",
          [n for n, _a, _t in config.MENU_ITEMS] == [str(i) for i in range(1, 14)], "")


def t_sync():
    section("W3 синхронизация набора: уходят только изменившиеся файлы")
    kit = os.path.join(WORK, "kitcopy")
    os.makedirs(kit, exist_ok=True)
    names = ["pcmcia_ctl.py", "windows/legacy/pcmcia_direct.py", "collect_all.py"]
    for index, name in enumerate(names):
        full = os.path.join(kit, name)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(f"файл {index}\n")
    local = sync.local_md5(kit, names)
    check("суммы считаются для всех файлов набора", len(local) == 3, str(local))
    check("сумма совпадает с md5 файла",
          local["pcmcia_ctl.py"] == sync.md5_of(os.path.join(kit, "pcmcia_ctl.py")), "")
    check("когда всё совпало — отправлять нечего",
          sync.changed_files(local, dict(local)) == [], "")
    broken = dict(local)
    broken["collect_all.py"] = "0" * 32
    check("изменившийся файл попадает в список",
          sync.changed_files(local, broken) == ["collect_all.py"], "")
    partial = dict(local)
    del partial["windows/legacy/pcmcia_direct.py"]
    check("файл, которого нет на ноутбуке, отправляется",
          "windows/legacy/pcmcia_direct.py" in sync.changed_files(local, partial), "")
    check("порядок отправки предсказуем (как в списке набора)",
          sync.changed_files(local, {}) == names, "")
    parsed = sync.parse_md5sum("0123456789abcdef0123456789abcdef  pcmcia_ctl.py\n"
                               "ABCDEF0123456789ABCDEF0123456789 *windows/legacy/pcmcia_direct.py\n"
                               "мусор\n")
    check("вывод md5sum разбирается", len(parsed) == 2, str(parsed))
    check("сумма приводится к нижнему регистру",
          parsed.get("windows/legacy/pcmcia_direct.py") == "abcdef0123456789abcdef0123456789", str(parsed))


def t_transport(bindir):
    section("W4 транспорт: ssh/scp, живые проценты, понятные отказы")
    target = Target("192.168.0.94", "root", 22, "", dry_run=False)
    target.ssh = os.path.join(bindir, "ssh")
    target.scp = os.path.join(bindir, "scp")

    reset_fakes("ok")
    code, _text = target.run("echo привет")
    check("команда уходит по ssh с портом -p", code == 0 and any(
        call.startswith("ssh -p 22") for call in calls()), str(calls()))
    check("в опциях ssh нет проброса агента и X11",
          all("ForwardAgent=no" in call and "ForwardX11=no" in call
              for call in calls() if call.startswith("ssh -p")), "")

    reset_fakes("ok", remote_out="  10%  1 МиБ\n  10%  1 МиБ\n  20%  2 МиБ\nготово\n")
    code, text = target.run_live("python3 pcmcia_ctl.py read")
    check("весь вывод команды накапливается для проверок",
          "20%" in text and "готово" in text and code == 0, text[:200])

    reset_fakes("denied")
    code, text = target.run("echo привет")
    check("отказ входа даёт код 255", code == 255, str(code))
    import io
    from contextlib import redirect_stdout
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        explained = explain(text)
    explanation = buffer.getvalue()
    check("отказ входа объясняется по-русски",
          explained and "ВХОД НЕ ВЫПОЛНЕН" in explanation, explanation[:200])
    check("объяснение говорит, что карта не тронута",
          "на карту не писали" in explanation, "")

    reset_fakes("down")
    code, text = target.run("echo привет")
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        explain(text)
    check("недоступный ноутбук объясняется отдельно",
          "НЕ ОТВЕЧАЕТ" in buffer.getvalue(), "")

    reset_fakes("sftp")
    target.copy_to(["файл.txt"], "root@192.168.0.94:/tmp/")
    scp_calls = [c for c in calls() if c.startswith("scp")]
    check("при выключенном sftp пробуется старый протокол (-O)",
          any(" -O " in c or c.startswith("scp -O") for c in scp_calls), str(scp_calls))

    reset_fakes("denied")
    code, text = target.copy_from("root@192.168.0.94:/root/a.bin",
                                  os.path.join(WORK, "a.bin"))
    check("отказ scp возвращает код и текст", code == 255 and text.strip() != "", "")


def t_cli(bindir, env):
    section("W5 пульт из командной строки: пути, образы, пробный режим")
    reset_fakes("ok")
    result = run_cli(["info", "--dry-run", "--kit-dir", KIT], env)
    check("пробный режим ничего не подключает", "ничего не подключаю" in result.stdout,
          result.stdout[-300:])
    check("пробный режим показывает, что было бы выполнено",
          "mkdir -p" in result.stdout and "pcmcia_ctl.py info" in result.stdout, "")
    check("пробный режим не вызвал ни ssh, ни scp", calls() == [], str(calls()))

    check("--version печатает версию",
          run_cli(["--version"], env).stdout.strip().startswith("pcmcia-kit"), "")
    help_text = run_cli(["--help"], env).stdout
    for key in ("--host", "--image", "--yes", "--dry-run", "--kit-dir"):
        check(f"в справке есть {key}", key in help_text, "")

    result = run_cli(["write", "--image", os.path.join(WORK, "нет-такого.bin"), "--kit-dir", KIT], env)
    check("несуществующий образ не отправляется", result.returncode == 2,
          str(result.returncode))
    check("о несуществующем образе сказано прямо", "не найден" in result.stdout.lower(), "")

    small = os.path.join(WORK, "small.bin")
    with open(small, "wb") as handle:
        handle.write(b"x" * 100)
    result = run_cli(["write", "--image", small, "--kit-dir", KIT], env)
    check("слишком маленький файл не пишется на карту", result.returncode == 2,
          str(result.returncode))
    check("причина названа: файл слишком мал", "слишком мал" in result.stdout, "")

    big = os.path.join(WORK, "card.bin")
    with open(big, "wb") as handle:
        handle.write(b"\xff" * 4096)
    reset_fakes("ok", md5_text=f"{sync.md5_of(big)}  /root/card.bin\n")
    result = run_cli(["write", "--image", big, "--kit-dir", KIT], env)
    check("правильный образ отправляется и записывается", result.returncode == 0,
          result.stdout[-400:])
    check("сумма образа сверяется на ноутбуке до записи",
          "md5 на ноутбуке совпал" in result.stdout, result.stdout[-400:])

    reset_fakes("ok", md5_text="0" * 32 + "  /root/card.bin\n")
    result = run_cli(["write", "--image", big, "--kit-dir", KIT], env)
    check("испорченный при копировании образ не пишется", result.returncode == 1,
          str(result.returncode))
    check("причина названа: md5 не совпал", "md5 на ноутбуке ДРУГОЙ" in result.stdout, "")

    reset_fakes("denied")
    result = run_cli(["info", "--kit-dir", KIT], env)
    check("при отказе входа пульт останавливается", result.returncode == 1,
          str(result.returncode))
    check("подсказка про вход по ключу на месте",
          "add_key" in result.stdout or "ВХОД НЕ ВЫПОЛНЕН" in result.stdout, "")

    reset_fakes("ok", remote_out="  микросхема : Am29F016\n  протокол команд : JEDEC\n")
    work_before = os.path.getsize(os.path.join(KIT, "windows", "legacy", "pcmcia_direct.py"))
    result = run_cli(["chip", "--host", "192.168.0.255", "--kit-dir", KIT], env)
    check("chip не трогает файлы набора на этой машине",
          os.path.getsize(os.path.join(KIT, "windows", "legacy", "pcmcia_direct.py")) == work_before, "")


def t_state():
    section("W6 памятка о карте в шапке меню")
    base = os.path.join(WORK, "state")
    os.makedirs(base, exist_ok=True)
    text = ("  микросхема            : Am29F016 (автоопределение)\n"
            "  напряжение программир.: 5 В (12 В не нужно)\n"
            "  сектор стирания       : 128 КиБ в адресах карты\n"
            "  протокол команд       : JEDEC\n"
            "  вид карты             : linear-flash\n"
            "  пароль                : НЕ-ЗАПОМИНАТЬ\n")
    saved = state.save(base, text, "192.168.0.94")
    header, lines = state.load(base)
    check("состояние сохраняется и читается", saved and header and lines, str(header))
    check("в памятке есть адрес ноутбука", "192.168.0.94" in header, header)
    joined = "\n".join(lines)
    check("в памятку попадает напряжение программирования", "12 В не нужно" in joined, "")
    check("в памятку попадает протокол и сектор",
          "JEDEC" in joined and "сектор стирания" in joined, "")
    check("посторонние строки (пароль) не запоминаются",
          "НЕ-ЗАПОМИНАТЬ" not in joined, "")
    check("файл состояния лежит в папке набора",
          os.path.isfile(os.path.join(base, config.STATE_FILE)), "")
    check("пустой вывод не портит памятку", state.pick_lines("") == [], "")
    check("памятку можно забыть", state.clear(base) and not os.path.isfile(
        os.path.join(base, config.STATE_FILE)), "")


def t_state_wear(env):
    """W1 карта износа в .card-state.txt."""
    section("W1 карта износа секторов в .card-state.txt")
    base = os.path.join(WORK, "state_wear")
    os.makedirs(base, exist_ok=True)

    # Пустая карта износа.
    entries = state.wear_entries(base)
    check("пустая карта износа — пустой список", entries == [], str(entries))

    # Добавляем запись о неудачном стирании.
    ok = state.wear_add(base, 0x10000, "erase", 0x10000, "2026-11-05 18:00")
    check("запись износа сохранена", ok, "")

    # Читаем обратно.
    entries = state.wear_entries(base)
    check("одна запись износа", len(entries) == 1, str(entries))
    check("смещение сектора", entries[0]["sector"] == 0x10000, str(entries[0]))
    check("тип отказа", entries[0]["type"] == "erase", str(entries[0]))
    check("байт отказа", entries[0]["byte"] == 0x10000, str(entries[0]))
    check("время записи", entries[0]["when"] == "2026-11-05 18:00",
          str(entries[0]))

    # Добавляем запись о неудачной записи.
    ok = state.wear_add(base, 0x20000, "write", 0x20010, "2026-11-05 18:05")
    check("вторая запись износа сохранена", ok, "")
    entries = state.wear_entries(base)
    check("две записи износа", len(entries) == 2, str(entries))

    # Дубликат не дублируется.
    ok = state.wear_add(base, 0x10000, "erase", 0x10000, "2026-11-05 19:00")
    check("дубликат не добавляется", len(state.wear_entries(base)) == 2,
          str(state.wear_entries(base)))

    # Проверяем формат файла: информационные строки и износная секция вместе.
    state.save(base, "  микросхема : Am29F016\n", "192.168.0.94")
    state.wear_add(base, 0x30000, "blank", 0x30005, "2026-11-05 18:10")
    with open(os.path.join(base, config.STATE_FILE), encoding="utf-8") as f:
        content = f.read()
    check("маркер износа в файле", state.WEAR_SECTION in content, "")
    check("WORN-строка в файле", "WORN 0x30000 blank 0x30005" in content, "")
    check("информационная строка сохранена", "микросхема" in content, "")

    # Показ карты износа не падает.
    try:
        state.wear_summary(base)
        check("wear_summary не падает", True, "")
    except Exception as error:
        check("wear_summary не падает", False, str(error))

    state.clear(base)


def t_gui(env):
    section("W7 графический пульт: окно, потокобезопасность, переход на текст")
    gui_path = os.path.join(SRC, "pcmcia_kit", "gui.py")
    tree = ast.parse(open(gui_path, encoding="utf-8").read())
    check("gui.py разбирается как Python-модуль", tree is not None, "")

    # Без экрана пульт обязан перейти на текстовое меню, а не упасть.
    result = run_cli(["info", "--dry-run", "--kit-dir", KIT], {**env, "DISPLAY": ""})
    check("без графической среды работа продолжается в текстовом режиме",
          "графической среды нет" in result.stdout or "ничего не подключаю" in result.stdout,
          result.stdout[:300])

    has_xvfb = shutil.which("xvfb-run") is not None
    if not has_xvfb:
        check("окно проверяется только при наличии xvfb (не дефект)", True,
              "xvfb-run не найден")
        return
    probe = r"""
import sys
sys.path.insert(0, {src!r})
from pcmcia_kit import gui
window = gui.PumpGUI([])
window.root.update()
print("TITLE=" + window.root.title())
print("BUTTONS=" + str(sum(1 for child in window.root.winfo_children())))
print("KEY=" + window.lbl_key.cget("text"))
window.root.destroy()
""".format(src=SRC)
    result = subprocess.run(["xvfb-run", "-a", sys.executable, "-c", probe],
                            capture_output=True, text=True, timeout=180)
    out = result.stdout
    check("окно создаётся", "TITLE=" in out and "пульт" in out, out[-300:] + result.stderr[-300:])
    check("в окне есть журнал и кнопки",
          "BUTTONS=" in out and int(out.split("BUTTONS=")[1].split()[0]) >= 3, out[-200:])
    check("в окне видно состояние входа по ключу", "KEY=" in out, out[-200:])

    text = open(gui_path, encoding="utf-8").read()
    check("работа идёт в отдельном потоке (окно не «зависает»)",
          "threading.Thread" in text, "")
    check("сообщения из потока идут через очередь, а не прямо в виджет",
          "queue.Queue" in text and "_drain" in text, "")
    check("опасные действия спрашиваются отдельно",
          "askyesno" in text and "DANGEROUS" in text, "")
    check("окно вызывает те же команды, что и текстовый режим",
          "from .cli import main" in text, "")


def t_local_selftest(env):
    section("W8 проверка набора без ноутбука")
    result = run_cli(["--local-tests", "--kit-dir", KIT], env)
    check("--local-tests запускает тесты на этой машине",
          "ИТОГО" in (result.stdout + result.stderr), result.stdout[-300:])


def main():
    print("=" * 70)
    print("ПРОВЕРКИ WINDOWS-ЧАСТИ (пакет src/pcmcia_kit)")
    print("=" * 70)
    bindir = make_fakes()
    env = fake_env(bindir)
    try:
        t_layout()
        t_commands()
        t_sync()
        t_transport(bindir)
        t_cli(bindir, env)
        t_state()
        t_state_wear(env)
        t_gui(env)
        t_local_selftest(env)
    finally:
        print("\n" + "=" * 70)
        print(f"ИТОГО: прошло {len(PASS)}, провалено {len(FAIL)}")
        for name, detail in FAIL:
            print(f"  ПРОВАЛ: {name}  {detail}")
        print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ" if not FAIL else "ЕСТЬ ПРОВАЛЫ")
        shutil.rmtree(WORK, ignore_errors=True)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
