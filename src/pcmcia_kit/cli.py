#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Командная строка пульта: разбор аргументов и вызов нужной команды.

Точка входа одна — `main()`. Она же вызывается из `python -m pcmcia_kit` и из
консольного скрипта `pcmcia-kit`, который ставится при установке пакета
(`[project.scripts]` в pyproject.toml). Поэтому вести себя по-разному в
зависимости от способа запуска набор не может и не должен.

    python -m pcmcia_kit                    меню
    python -m pcmcia_kit info               паспорт карты и вердикт
    python -m pcmcia_kit read               снять образ
    python -m pcmcia_kit write -i card.bin  записать образ
    python -m pcmcia_kit selftest           проверить набор (карта не нужна)
"""
import argparse
import os
import re
import sys


def _running_from_tree():
    """Запущен ли пакет прямо из папки набора, без установки.

    От этого зависит одна вещь: добавлять ли корень набора в sys.path, чтобы
    при двойном щелчке пакет импортировался без настройки PYTHONPATH. После
    установки (`pip install .`) этого делать не нужно и не следует.
    """
    here = os.path.dirname(os.path.abspath(__file__))          # .../src/pcmcia_kit
    root = os.path.dirname(os.path.dirname(here))              # корень набора
    return (os.path.basename(os.path.dirname(here)) == "src"
            and os.path.isfile(os.path.join(root, "pcmcia_kit.pyw")))


if _running_from_tree():
    _root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    if _root not in sys.path:
        sys.path.insert(0, _root)

from . import __version__, console, keyring, remote, state      # noqa: E402
from .commands import erase as cmd_erase      # noqa: E402
from .commands import image as cmd_image      # noqa: E402
from .commands import menu as cmd_menu        # noqa: E402
from .commands import probe as cmd_probe      # noqa: E402
from .commands import report as cmd_report    # noqa: E402
from .commands import selftest as cmd_selftest  # noqa: E402
from .config import (ACTIONS, DEFAULT_HOST, DEFAULT_PORT, DEFAULT_USER,  # noqa: E402
                     LOCAL_ACTIONS, LOCAL_REQUIRED, default_key, has_key,
                     kit_dir as resolve_kit_dir, kit_search_paths, STATE_FILE)
from .transport import SshError, Target                       # noqa: E402

# Смещение неисправного сектора в выводе pcmcia_direct.py / pcmcia_ctl.py.
# Ищет и английский ("offset 0x10000") и русский ("смещении 0x10000") варианты,
# чтобы не зависеть от языка вывода удалённой стороны.
_WEAR_OFFSET = re.compile(
    r"(?:смещении|offset)\s+(0x[0-9a-fA-F]+)", re.IGNORECASE
)

EPILOG = """\
Примеры:
  python -m pcmcia_kit                          меню (самый простой путь)
  python -m pcmcia_kit info --host 192.168.0.71 паспорт карты
  python -m pcmcia_kit read                     снять образ и забрать к себе
  python -m pcmcia_kit write -i card.bin        записать образ на карту
  python -m pcmcia_kit selftest                 проверить набор (карта не нужна)
"""


def build_parser():
    """Собрать разбор аргументов. Один на все команды — так проще запомнить."""
    parser = argparse.ArgumentParser(
        prog="pcmcia_kit",
        description="Пульт управления линейной флеш-картой PCMCIA с Windows-машины.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", nargs="?", default="menu",
                        choices=list(ACTIONS) + list(LOCAL_ACTIONS) + ["menu"],
                        help="что сделать (по умолчанию: меню)")
    parser.add_argument("--version", action="version",
                        version=f"pcmcia-kit {__version__}")

    group = parser.add_argument_group("подключение")
    group.add_argument("--host", "-H", default=DEFAULT_HOST,
                       help=f"адрес ноутбука (по умолчанию {DEFAULT_HOST})")
    group.add_argument("--user", "-u", default=DEFAULT_USER, help="пользователь ssh")
    group.add_argument("--port", "-p", type=int, default=DEFAULT_PORT, help="порт ssh")
    group.add_argument("--key", "-k", default="",
                       help="ключ вместо пароля (по умолчанию ~/.ssh/pcmcia_key)")

    group = parser.add_argument_group("файлы")
    group.add_argument("--image", "-i", default="",
                       help="файл образа на этой машине (для write/verify/fwwrite)")
    group.add_argument("--remote-path", default="",
                       help="путь к образу на ноутбуке")
    group.add_argument("--kit-dir", default="", help="папка набора")

    group = parser.add_argument_group("поведение")
    group.add_argument("--yes", "-y", action="store_true",
                       help="не спрашивать подтверждение (стирание, запись)")
    group.add_argument("--full", action="store_true",
                       help="полный прогон тестов (для selftest)")
    group.add_argument("--dry-run", "-n", action="store_true",
                       help="показать, что было бы выполнено, и ничего не делать")
    group.add_argument("--local-tests", action="store_true",
                       help="прогнать тесты на этой машине, без ноутбука")
    group.add_argument("--resume", action="store_true",
                       help="продолжить прерванную запись с последнего сектора")
    group.add_argument("--journal", default="",
                       help="путь к файлу журнала записи на ноутбуке "
                            "(по умолчанию <папка набора>/.journal)")
    group.add_argument("--backup", default="",
                       help="папка для резервных копий секторов перед "
                            "стиранием (на этой машине). Каждый сектор, "
                            "который собираются стереть, сначала читается и "
                            "сохраняется здесь.")
    group.add_argument("--follow", action="store_true",
                       help="после запуска записи читать журнал на ноутбуке "
                            "и показывать прогресс в реальном времени "
                            "(полезно, если сессия ssh может оборваться)")
    group.add_argument("--json", action="store_true",
                       help="машинный вывод: ответ команды в формате JSON "
                            "(для info, chip, canwrite, diag)")
    return parser


def resolve_key(explicit):
    """Выбрать файл ключа: указанный, обычный или никакого."""
    if explicit:
        if has_key(explicit):
            return explicit
        console.warn(f"указанный ключ не найден: {explicit}")
        fallback = default_key()
        if has_key(fallback):
            console.say(f"  беру обычный: {fallback}")
            return fallback
        console.say("  работаю с паролем")
        return ""
    return default_key() if has_key(default_key()) else ""


def kit_version(directory):
    """Версия набора из pcmcia_direct.py: по ней видно, тот ли архив распакован."""
    path = os.path.join(directory, "windows", "legacy", "pcmcia_direct.py")
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("KIT_VERSION"):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "?"


def check_kit(directory, explicit=""):
    """Проверить, что в папке лежит набор."""
    missing = [name for name in LOCAL_REQUIRED
               if not os.path.isfile(os.path.join(directory, name))]
    if missing:
        console.bad("файлы набора не найдены: " + ", ".join(missing))
        console.say("")
        console.say("  Пульт ищет их здесь:")
        for path in kit_search_paths(explicit):
            console.say("    " + path)
        console.say("")
        console.say("  Что делать: перейдите в папку набора (там лежат pcmcia_direct.py")
        console.say("  и pcmcia_ctl.py) или укажите её явно:")
        console.say("    python -m pcmcia_kit info --kit-dir D:\\pcmcia-flash-kit")
        return False
    return True


def _follow_journal(target, directory, journal=""):
    """Прочитать журнал записи с ноутбука и показать прогресс.

    Читает файл .journal через ssh и выводит, сколько секторов записано,
    сколько байт, на каком секторе остановились (если запись прервалась).
    """
    from . import remote as rctl
    import json

    journal_path = journal or f"{rctl.REMOTE_DIR}/.journal"
    code, text = target.run(f"cat '{journal_path}' 2>/dev/null || true")
    if code != 0 or not text.strip():
        console.say("  журнал не найден или пуст — записи не было.")
        return

    done = 0
    total = 0
    bytes_written = 0
    erased = 0
    last_sector = None
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("stage") == "sector_done":
            done += 1
            total = entry.get("total", total)
            bytes_written = entry.get("bytes", bytes_written)
            erased = entry.get("erased", erased)
            last_sector = entry.get("sector", last_sector)

    console.say("\n  ---- прогресс по журналу ----")
    if total:
        pct = done * 100 // total
        console.say(f"  записано секторов: {done} из {total} ({pct}%)")
    if bytes_written:
        console.say(f"  записано байт: {bytes_written}")
    if erased:
        console.say(f"  стёрто секторов: {erased}")
    if last_sector is not None:
        console.say(f"  последний сектор: {last_sector:#x}")
    console.say("  -----------------------------\n")


def run_remote(args, action, directory, target, confirmed_in_menu=False):
    """Выполнить команду, которой нужен ноутбук."""
    image = args.image
    remote_path = args.remote_path
    if not remote_path and action == "read":
        remote_path = remote.default_remote_image("read")

    if action in ("info", "chip", "canwrite", "diag"):
        return cmd_probe.run(target, directory, action, remote_path,
                             args.yes, args.full, json_out=args.json)
    if action in ("read", "write", "verify", "fwwrite"):
        rc, text = cmd_image.run(target, directory, action, image, remote_path,
                                 args.yes, os.getcwd(),
                                 resume=args.resume, journal=args.journal,
                                 backup=args.backup)
        if args.follow and action == "write":
            _follow_journal(target, directory, args.journal)
        # Карта износа и карта отказов: при неудачной записи/стирании
        # фиксируем сектор и байт, чтобы canwrite мог предупредить
        # при следующем запуске.
        if action in ("write", "erase", "fwwrite") and rc != 0:
            _record_failure(directory, text, action, image)
        return rc
    if action == "erase":
        rc, text = cmd_erase.run(target, directory, "erase", args.yes,
                                 remote_path, confirmed_in_menu)
        if rc != 0:
            _record_failure(directory, text, action, "")
        return rc
    if action == "report":
        return cmd_report.run(target, directory, os.getcwd())
    if action == "fwprobe":
        return cmd_probe.run(target, directory, action, remote_path, args.yes, args.full)
    if action == "selftest":
        return cmd_selftest.run(target, directory, args.full)
    console.bad(f"неизвестная команда: {action}")
    return 2


def _record_failure(directory, text, action, image=""):
    """Записать сбой в карту износа и карту отказов.

    Ищет смещение сектора в выводе pcmcia_direct.py по patterns
    «смещении 0x...» / «offset 0x...» и фиксирует его.
    """
    m = _WEAR_OFFSET.search(text)
    if not m:
        return
    sector = int(m.group(1), 16)
    ftype = "erase" if action == "erase" else "write"
    state.wear_add(directory, sector, ftype, sector)
    # Карта отказов: точное ожидаемое/фактическое значение требует
    # разбора детального вывода pcmcia_direct.py — пока 0x00/0x00.
    state.failure_add(
        directory, sector, 0x00, 0x00,
        image=os.path.basename(image) if image else "",
    )


def run(args):
    """Выполнить выбранное действие. Возвращает код возврата."""
    directory = resolve_kit_dir(args.kit_dir)
    if not check_kit(directory, args.kit_dir):
        return 2

    if args.action in LOCAL_ACTIONS:
        # Настройка ключа выполняется здесь же и ssh/scp проверяет сама.
        return keyring.setup(args.host, args.user, args.port, args.key)

    key = resolve_key(args.key)
    target = Target(args.host, args.user, args.port, key, dry_run=args.dry_run)
    try:
        target.require_programs()
    except SshError as error:
        console.bad(error.message)
        return error.code

    action = args.action
    confirmed_in_menu = False
    if action == "menu":
        action = cmd_menu.show(target, kit_version(directory), directory)
        if not action:
            return 0
        if not cmd_menu.confirm(action, menu_title(action)):
            console.say("  отменено: карта не тронута")
            return 0
        confirmed_in_menu = True

    if args.dry_run:
        _show_plan(args, action, target)

    return run_remote(args, action, directory, target, confirmed_in_menu)


def menu_title(action):
    """Название пункта меню по имени команды (для подтверждения)."""
    from .config import MENU_ITEMS
    for _number, name, title in MENU_ITEMS:
        if name == action:
            return title
    return action


def _show_plan(args, action, target):
    """Печать плана для --dry-run: что и где было бы выполнено."""
    remote_path = args.remote_path or remote.default_remote_image(
        "read" if action == "read" else "write")
    command = remote.build(action, remote_path, args.yes, args.full,
                           resume=args.resume, journal=args.journal)
    console.say("")
    console.say("  --dry-run: ничего не подключаю. Было бы выполнено:")
    console.say(f"    ssh {' '.join(target.ssh_options())} {target.address} "
                f"'mkdir -p {remote.REMOTE_DIR}'")
    console.say(f"    scp {' '.join(target.scp_options())} <файлы набора> "
                f"{target.address}:{remote.REMOTE_DIR}/")
    console.say(f"    ssh {' '.join(target.ssh_options())} {target.address} '{command}'")
    console.say("")
    console.say(f"  проверка: ssh={target.ssh}  scp={target.scp}  "
                f"ключ={'есть' if target.has_key() else 'нет'}")


def run_local_tests():
    """Прогнать тесты набора на этой машине (без ноутбука)."""
    directory = resolve_kit_dir("")
    script = os.path.join(directory, "tests", "test_scripts.py")
    if not os.path.isfile(script):
        console.bad("тесты не входят в набор: нет tests/test_scripts.py")
        return 2
    import subprocess
    return subprocess.call([sys.executable, script, "--fast"])


def main(argv=None):
    """Точка входа. Возвращает код возврата для оболочки."""
    console.enable_utf8()
    console.enable_color()
    args = build_parser().parse_args(argv)
    try:
        if args.local_tests:
            return run_local_tests()
        return run(args)
    except SshError as error:
        console.bad(error.message)
        return error.code
    except KeyboardInterrupt:
        console.say("")
        console.say("  прервано пользователем")
        return 130


if __name__ == "__main__":            # pragma: no cover - ручной запуск
    sys.exit(main())
