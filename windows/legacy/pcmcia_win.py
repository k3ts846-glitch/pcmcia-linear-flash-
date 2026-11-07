#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пульт управления картой с Windows-машины.

Запускается на рабочей машине, а работает с картой на ноутбуке: файлы набора
копируются туда по scp, команда запускается по ssh, результат и образ
возвращаются обратно тоже scp. На ноутбуке ничего устанавливать не нужно.

Заменяет прежние RUN_CTL.ps1, RUN.ps1 и remote_collect.ps1: PowerShell на
рабочей машине больше не требуется.

    python pcmcia_win.py                      меню (самый простой путь)
    python pcmcia_win.py info                 паспорт карты и вердикт
    python pcmcia_win.py read                 снять образ и забрать к себе
    python pcmcia_win.py write --image card.bin    отправить образ и записать
    python pcmcia_win.py erase --yes          стереть карту
    python pcmcia_win.py verify --image card.bin   сверить файл с картой
    python pcmcia_win.py chip                 что за микросхема (не пишет)
    python pcmcia_win.py selftest             проверить набор (карта не нужна)

Общие ключи подходят к любой команде: --host, --user, --port, --key,
--yes, --dry-run.
"""
import argparse
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from kitlib import console, kstate, ksync                      # noqa: E402
from kitlib import remote as rctl                              # noqa: E402
from kitlib.transport import SshError, Target, explain         # noqa: E402

# Пункты меню: номер, действие, пояснение. Порядок и номера сохранены от
# прежнего пульта, чтобы привычные пункты остались на месте.
MENU_ITEMS = (
    ("1", "info", "Паспорт и вердикт (что можно, что нельзя)"),
    ("2", "read", "Снять образ карты и забрать к себе"),
    ("3", "write", "Записать образ на карту (файл с этой машины)"),
    ("4", "erase", "Стереть карту"),
    ("5", "verify", "Сверить файл с картой (без записи)"),
    ("6", "canwrite", "Можно ли писать именно эту карту"),
    ("7", "diag", "Диагностика: окна памяти, драйвер, dmesg"),
    ("8", "report", "Полный отчёт (все разделы)"),
    ("9", "fwprobe", "ПРОБА записи БЕЗ драйвера: один сектор, данные возвращаются"),
    ("10", "fwwrite", "Записать образ БЕЗ драйвера (файл с этой машины)"),
    ("11", "chip", "Что за микросхема (тип, сектор, протокол, напряжение)"),
    ("12", "selftest", "Проверить сам набор (тесты; карта не нужна, ~20 с)"),
)

DEFAULT_KEY = os.path.join(os.path.expanduser("~"), ".ssh", "pcmcia_key")

# Файлы, без которых пульт работать не должен: если их нет, значит распакован
# не тот архив или указана не та папка.
REQUIRED_KIT_FILES = ("pcmcia_ctl.sh", "windows/legacy/pcmcia_direct.py", "collect_all.sh",
                      "run_remote.sh")


def build_parser():
    """Разобрать аргументы командной строки."""
    parser = argparse.ArgumentParser(
        prog="pcmcia_win.py",
        description="Пульт управления PCMCIA-картой с Windows-машины (по ssh/scp).",
        epilog="Пример: python pcmcia_win.py info --host 192.168.0.71")
    parser.add_argument("action", nargs="?", default="menu", choices=list(rctl.ACTIONS) + ["menu"],
                        help="что сделать (по умолчанию меню)")
    parser.add_argument("--host", "-H", default="192.168.0.94",
                        help="адрес ноутбука (по умолчанию 192.168.0.94)")
    parser.add_argument("--user", "-u", default="root", help="пользователь ssh")
    parser.add_argument("--port", "-p", type=int, default=22, help="порт ssh")
    parser.add_argument("--key", "-k", default="",
                        help="ключ вместо пароля (по умолчанию ~/.ssh/pcmcia_key)")
    parser.add_argument("--image", "-i", default="",
                        help="файл образа на этой машине (для write/verify)")
    parser.add_argument("--remote-path", default="",
                        help="путь к образу на ноутбуке")
    parser.add_argument("--kit-dir", default="", help="папка набора")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="не спрашивать подтверждение на ноутбуке")
    parser.add_argument("--full", action="store_true",
                        help="полный прогон тестов (для selftest)")
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="показать, что было бы выполнено, и ничего не делать")
    return parser


def resolve_key(explicit):
    """Выбрать файл ключа: указанный, обычный или никакого."""
    if explicit:
        if os.path.isfile(explicit):
            return explicit
        console.warn(f"указанный ключ не найден: {explicit}")
        if os.path.isfile(DEFAULT_KEY):
            console.say(f"  беру обычный: {DEFAULT_KEY}")
            return DEFAULT_KEY
        console.say("  работаю с паролем")
        return ""
    if os.path.isfile(DEFAULT_KEY):
        return DEFAULT_KEY
    return ""


def kit_version(kit_dir):
    """Версия набора из pcmcia_direct.py. Нужна, чтобы отличить старый архив."""
    path = os.path.join(kit_dir, "windows", "legacy", "pcmcia_direct.py")
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("KIT_VERSION"):
                    # без отрезания комментария версия уезжает вместе
                    # с ним: было «2.17.16"  # версия набора: ...».
                    value = line.split("=", 1)[1].split("#", 1)[0]
                    return value.strip().strip('"')
    except OSError:
        pass
    return "?"


def check_kit_dir(kit_dir):
    """Проверить, что в папке лежит набор."""
    missing = [name for name in REQUIRED_KIT_FILES
               if not os.path.isfile(os.path.join(kit_dir, name))]
    if missing:
        console.bad(f"в папке {kit_dir} нет: {', '.join(missing)}")
        console.say("  Укажите папку набора: python pcmcia_win.py info --kit-dir D:\\pcmcia-flash-kit")
        return False
    return True


def show_menu(target, version, kit_dir):
    """Показать меню и вернуть выбранное действие ('' — выход)."""
    console.say("")
    console.say("================================================================")
    console.say("  ПУЛЬТ УПРАВЛЕНИЯ КАРТОЙ (Windows -> ноутбук, по ssh/scp)")
    console.say("================================================================")
    console.say(f"  ноутбук: {target.address} порт {target.port}   набор: {version}")
    if target.has_key():
        console.say(f"  вход:   по ключу {target.key_file}")
    else:
        console.say("  вход:   по паролю (спросит ssh); без пароля: python add_key.py")
    kstate.show(kit_dir, console.say)
    console.say("")
    for number, _action, title in MENU_ITEMS:
        console.say(f"  {number:>2}) {title}")
    console.say("   0) Выход")
    console.say("")
    choice = console.ask("Пункт")
    if not choice:
        return ""
    for number, action, _title in MENU_ITEMS:
        if choice == number:
            return action
    console.say("  ничего не выбрано — выхожу")
    return ""


def prepare_image(args, action):
    """Проверить локальный файл образа и вернуть путь к нему.

    Ошибка записи не того файла — самая дорогая из возможных, поэтому файл
    проверяется до подключения к ноутбуку.
    """
    image = args.image
    if not image:
        image = console.ask("Путь к файлу-образу на этой машине")
    if not image:
        console.bad("файл образа не указан")
        return ""
    image = os.path.abspath(image)
    if not os.path.isfile(image):
        console.bad(f"файл образа не найден: {image}")
        return ""
    size = os.path.getsize(image)
    if size == 0:
        console.bad(f"файл образа пустой: {image}")
        return ""
    if action in ("write", "fwwrite") and size < 1024:
        # Меньше килобайта — это почти наверняка не образ карты, а текст или
        # недокачанный файл. Запись такого файла испортит карту.
        console.bad(f"файл слишком мал для образа карты: {size} байт")
        return ""
    return image


def step_connect(target):
    """Шаг 1: убедиться, что папка на ноутбуке существует."""
    console.head("ШАГ 1/5: папка на ноутбуке (ssh)")
    code, text = target.run(f"mkdir -p {rctl.REMOTE_DIR}")
    if code != 0:
        if not explain(text):
            console.say(text)
        return False
    console.say(f"  готово: {rctl.REMOTE_DIR}")
    return True


def step_sync(target, kit_dir):
    """Шаг 2: отправить изменившиеся файлы набора."""
    console.head("ШАГ 2/5: синхронизирую набор (scp, только изменившееся)")
    try:
        ksync.sync(kit_dir, target, rctl.REMOTE_DIR, reporter=console.say)
    except (OSError, FileNotFoundError) as error:
        console.bad(str(error))
        console.say("  файлы не скопированы — дальше идти нельзя, на карту ничего не писали.")
        return False
    return True


def step_upload(target, image, remote_path):
    """Шаг 3: отправить образ на ноутбук и сверить сумму."""
    console.head("ШАГ 3/5: отправляю образ на ноутбук (scp)")
    code, text = target.copy_to([image], f"{target.address}:{remote_path}")
    if code != 0:
        if not explain(text):
            console.say(text)
        console.say("  образ не отправлен — на карту ничего не писали.")
        return False
    local_sum = ksync.md5_of(image)
    console.say(f"  отправлено: {os.path.basename(image)}  md5 {local_sum}")
    code, text = target.run(f"md5sum '{remote_path}'")
    if code == 0:
        remote_sum = ksync.parse_md5sum(text)
        got = next(iter(remote_sum.values()), "")
        if got and got != local_sum:
            console.bad(f"md5 на ноутбуке ДРУГОЙ: {got} вместо {local_sum}")
            console.say("  Запись отменена, чтобы не залить испорченный файл.")
            return False
        if got:
            console.say("  md5 на ноутбуке совпал — файл тот самый")
    else:
        console.warn("md5 на ноутбуке проверить не удалось — продолжаю, но это стоит знать")
    return True


def step_execute(target, remote_cmd, action):
    """Шаг 4: выполнить команду на ноутбуке с показом хода работы."""
    console.head("ШАГ 4/5: работаю с картой (ssh)")
    if action == "read":
        console.say("  образ читается ДВАЖДЫ и сверяется: время будет примерно вдвое больше")
        console.say("  проценты ниже означают, что набор работает, а не висит")
    code, text = target.run_live(remote_cmd)
    if "НУЖЕН ПАРОЛЬ SUDO" in text or "sudo: a password is required" in text:
        console.bad("работа требует root, а sudo просит пароль (его нельзя ввести без терминала)")
        console.say("  Выберите одно:")
        console.say("   1) python pcmcia_win.py ... --user root")
        console.say("   2) на ноутбуке разрешить sudo без пароля:")
        console.say("        echo '$USER ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/pcmcia")
        console.say("   3) сделать это прямо на ноутбуке: sudo bash ./pcmcia_ctl.sh")
        return 3, text
    return code, text


def step_collect(target, action, remote_path, work_dir):
    """Шаг 5: забрать образ или отчёт. Возвращает код возврата."""
    if action == "read":
        console.head("ШАГ 5/5: забираю образ к себе (scp)")
        local = os.path.join(work_dir,
                             f"card-{target.host}-{os.path.basename(remote_path)}")
        code, text = target.copy_from(f"{target.address}:{remote_path}", local)
        if code != 0:
            if not explain(text):
                console.say(text)
            console.bad(f"образ остался на ноутбуке: {remote_path} (забрать вручную по scp)")
            return 1
        console.say(f"  образ у вас: {local}")
        if os.path.isfile(local):
            console.say(f"  размер: {os.path.getsize(local)} байт   "
                        f"md5: {ksync.md5_of(local)}")
        return 0
    if action == "report":
        console.head("ШАГ 5/5: забираю отчёт к себе (scp)")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        local = os.path.join(work_dir, f"otchet-{target.host}-{stamp}.txt")
        code, text = target.copy_from(f"{target.address}:{rctl.REMOTE_DIR}/otchet.txt", local)
        if code != 0:
            console.bad("отчёт не забрался")
            return 1
        console.say(f"  отчёт получен: {local}")
        show_report_digest(local)
        return 0
    console.head("ШАГ 5/5: забирать файл не нужно")
    console.say("  пропущено")
    return 0


def show_report_digest(path):
    """Показать главные строки отчёта: версию, паспорт, вердикт."""
    markers = ("ИТОГ РАЗДЕЛА 7", "версия набора", "паспорт", "ВЕРДИКТ")
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = [line.strip() for line in handle]
    except OSError:
        return
    picked = [line for line in lines if any(m in line for m in markers)]
    if not picked:
        return
    console.say("")
    console.say("---- главное из отчёта ----")
    for line in picked:
        console.say("  " + line)


def cleanup(target, had_error):
    """Убрать набор с ноутбука. Образ в /root не трогаем: его могли не забрать."""
    console.say("")
    console.say(f"  убрано с ноутбука: {rctl.REMOTE_DIR} (образ в /root не тронут)")


def run(args):
    """Выполнить одно действие пульта. Возвращает код возврата."""
    kit_dir = os.path.abspath(args.kit_dir or _HERE)
    if not check_kit_dir(kit_dir):
        return 2

    key = resolve_key(args.key)
    target = Target(args.host, args.user, args.port, key, dry_run=args.dry_run)
    try:
        target.require_programs()
    except SshError as error:
        console.bad(error.message)
        return error.code

    version = kit_version(kit_dir)
    action = args.action
    if action == "menu":
        action = show_menu(target, version, kit_dir)
        if not action:
            return 0

    image = ""
    if rctl.needs_image(action):
        image = prepare_image(args, action)
        if not image:
            return 2

    remote_path = args.remote_path or rctl.default_remote_image(
        "read" if action == "read" else "write")
    if action == "read" and not args.remote_path:
        remote_path = rctl.default_remote_image("read")
    if image and not args.remote_path:
        remote_path = "/root/" + os.path.basename(image)

    remote_cmd = rctl.build(action, remote_path, args.yes, args.full)
    if not remote_cmd:
        console.bad(f"неизвестное действие: {action}")
        return 2

    console.head("ЧТО БУДЕТ СДЕЛАНО")
    console.say(f"  команда: {action}")
    console.say(f"  ноутбук: {target.address} (порт {target.port})")
    console.say(f"  папка:   {rctl.REMOTE_DIR}")
    console.say(f"  запуск:  {remote_cmd}")
    if rctl.needs_image(action) or action == "read":
        console.say(f"  файл на ноутбуке: {remote_path}")
    if args.dry_run:
        console.say("")
        console.say("  --dry-run: ничего не подключаю. Было бы выполнено:")
        console.say(f"    ssh {' '.join(target.ssh_options())} {target.address} "
                    f"'mkdir -p {rctl.REMOTE_DIR}'")
        console.say(f"    scp {' '.join(target.scp_options())} <файлы набора> "
                    f"{target.address}:{rctl.REMOTE_DIR}/")
        console.say(f"    ssh {' '.join(target.ssh_options())} {target.address} '{remote_cmd}'")
        console.say("")
        console.say(f"  проверка: ssh={target.ssh}  scp={target.scp}  "
                    f"ключ={'есть' if target.has_key() else 'нет'}")
        return 0

    if not target.has_key():
        console.say("")
        console.say("  Пароль спрашивает ssh на каждом шаге — это нормально.")
        console.say("  Чтобы вводить один раз: python add_key.py")

    if not step_connect(target):
        return 1
    if not step_sync(target, kit_dir):
        return 1
    if rctl.needs_image(action):
        if not step_upload(target, image, remote_path):
            return 1
    else:
        console.head("ШАГ 3/5: файл образа не нужен для этой команды")
        console.say("  пропущено")

    code, text = step_execute(target, remote_cmd, action)
    if code == 3:
        return 3
    if action in ("info", "chip"):
        if kstate.save(kit_dir, text, args.host):
            console.say("")
            console.say("  (состояние карты запомнено — увидите его в шапке меню)")
    if action == "read" and rctl.report_was_not_created(text):
        console.bad("образ не создан на ноутбуке — забирать нечего")
        console.say("  Причина в выводе выше. Чаще всего карта отвечает нестабильно")
        console.say("  (два чтения различаются) или паспорт не читается.")
        return 2

    collect_code = step_collect(target, action, remote_path, os.getcwd())
    cleanup(target, code != 0)
    return collect_code or code


def main(argv=None):
    """Точка входа. Возвращает код возврата для оболочки."""
    console.enable_utf8()
    console.enable_color()
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except SshError as error:
        console.bad(error.message)
        return error.code
    except KeyboardInterrupt:
        console.say("")
        console.say("  прервано пользователем")
        return 130


if __name__ == "__main__":
    sys.exit(main())
