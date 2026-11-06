#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Общая обвязка команд: что делается до и после работы с картой.

Каждая команда пульта выглядит одинаково: «подключиться, синхронизировать
набор, отправить образ (если нужен), выполнить, забрать результат». Повторять
это в двенадцати модулях — верный способ сделать их по-разному. Поэтому шаги
собраны здесь, а команда задаёт только то, чем отличается от других.

Порядок шагов и их номера («ШАГ 2/5») сохранены от прежнего пульта, чтобы
привычный вид отчёта не менялся.
"""
from . import console, state, sync
from . import remote as rctl
from .config import REMOTE_DIR, REMOTE_FILES
from .transport import explain

# Сколько шагов в полном цикле — печатается в заголовках.
STEPS = 5


def connect(target):
    """Шаг 1: убедиться, что папка на ноутбуке существует."""
    console.head(f"ШАГ 1/{STEPS}: папка на ноутбуке (ssh)")
    code, text = target.run(f"mkdir -p {REMOTE_DIR}")
    if code != 0:
        if not explain(text):
            console.say(text)
        return False
    console.say(f"  готово: {REMOTE_DIR}")
    return True


def send_kit(target, kit_dir):
    """Шаг 2: отправить на ноутбук только изменившиеся файлы набора."""
    console.head(f"ШАГ 2/{STEPS}: синхронизирую набор (scp, только изменившееся)")
    try:
        sync.sync(kit_dir, target, REMOTE_DIR, reporter=console.say)
    except (OSError, FileNotFoundError) as error:
        console.bad(str(error))
        console.say("  файлы не скопированы — дальше идти нельзя, на карту ничего не писали.")
        return False
    return True


def send_image(target, image, remote_path):
    """Шаг 3: отправить образ и сверить контрольную сумму до записи."""
    console.head(f"ШАГ 3/{STEPS}: отправляю образ на ноутбук (scp)")
    code, text = target.copy_to([image], f"{target.address}:{remote_path}")
    if code != 0:
        if not explain(text):
            console.say(text)
        console.say("  образ не отправлен — на карту ничего не писали.")
        return False
    local_sum = sync.md5_of(image)
    console.say(f"  отправлено: {image_name(image)}  md5 {local_sum}")
    code, text = target.run(f"md5sum '{remote_path}'")
    if code == 0:
        remote_sum = sync.parse_md5sum(text)
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


def skip_image(action):
    """Шаг 3 для команд, которым образ не нужен."""
    console.head(f"ШАГ 3/{STEPS}: файл образа не нужен для этой команды")
    console.say("  пропущено")


def execute(target, remote_cmd, action):
    """Шаг 4: выполнить команду на ноутбуке с показом хода работы."""
    console.head(f"ШАГ 4/{STEPS}: работаю с картой (ssh)")
    if action == "read":
        console.say("  образ читается ДВАЖДЫ и сверяется: время будет примерно вдвое больше")
        console.say("  проценты ниже означают, что набор работает, а не висит")
    return target.run_live(remote_cmd)


def remember_card(kit_dir, text, host):
    """Запомнить главные строки опроса для шапки меню."""
    if state.save(kit_dir, text, host):
        console.say("")
        console.say("  (состояние карты запомнено — увидите его в шапке меню)")


def collect(target, action, remote_path, work_dir):
    """Шаг 5: забрать образ или отчёт. Возвращает код возврата."""
    if action == "read":
        return _pull_image(target, remote_path, work_dir)
    if action == "report":
        return _pull_report(target, work_dir)
    console.head(f"ШАГ 5/{STEPS}: забирать файл не нужно")
    console.say("  пропущено")
    return 0


def _pull_image(target, remote_path, work_dir):
    console.head(f"ШАГ 5/{STEPS}: забираю образ к себе (scp)")
    import os
    local = os.path.join(work_dir, f"card-{target.host}-{os.path.basename(remote_path)}")
    code, text = target.copy_from(f"{target.address}:{remote_path}", local)
    if code != 0:
        if not explain(text):
            console.say(text)
        console.bad(f"образ остался на ноутбуке: {remote_path} (забрать вручную по scp)")
        return 1
    console.say(f"  образ у вас: {local}")
    if os.path.isfile(local):
        console.say(f"  размер: {os.path.getsize(local)} байт   md5: {sync.md5_of(local)}")
    return 0


def _pull_report(target, work_dir):
    console.head(f"ШАГ 5/{STEPS}: забираю отчёт к себе (scp)")
    import os
    import time
    stamp = time.strftime("%Y%m%d-%H%M%S")
    local = os.path.join(work_dir, f"otchet-{target.host}-{stamp}.txt")
    code, text = target.copy_from(f"{target.address}:{REMOTE_DIR}/otchet.txt", local)
    if code != 0:
        console.bad("отчёт не забрался")
        return 1
    console.say(f"  отчёт получен: {local}")
    show_digest(local)
    return 0


def show_digest(path):
    """Показать главные строки отчёта: версию набора, паспорт, вердикт."""
    markers = ("ИТОГ РАЗДЕЛА 7", "версия набора", "паспорт", "ВЕРДИКТ")
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = [line.strip() for line in handle]
    except OSError:
        return
    picked = [line for line in lines if any(marker in line for marker in markers)]
    if not picked:
        return
    console.say("")
    console.say("---- главное из отчёта ----")
    for line in picked:
        console.say("  " + line)


def finish(target):
    """Убрать набор с ноутбука. Образ в /root не трогаем: его могли не забрать.

    Удаляем только файлы набора (pcmcia_direct.py, pcmcia_ctl.py и т.д.),
    но не образ: пользователь мог не успеть его забрать.
    """
    import shlex
    quoted = " ".join(shlex.quote(name) for name in REMOTE_FILES)
    code, text = target.run(
        f"cd '{REMOTE_DIR}' && rm -f {quoted} 2>/dev/null; true"
    )
    console.say("")
    console.say(f"  набор убран с ноутбука: {REMOTE_DIR}")
    console.say(f"  образ в /root не тронут — забирайте вручную, если нужно")


def image_name(path):
    """Имя файла образа без папки (для сообщений)."""
    import os
    return os.path.basename(path)


def needs_sudo_error(target, action):
    """Подсказка, если на ноутбуке не хватает прав root."""
    console.bad("работа требует root, а sudo просит пароль (его нельзя ввести без терминала)")
    console.say("  Выберите одно:")
    console.say("   1) python -m pcmcia_kit %s --user root" % action)
    console.say("   2) на ноутбуке разрешить sudo без пароля:")
    console.say("        echo '$USER ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/pcmcia")
    console.say("   3) сделать это прямо на ноутбуке: sudo python3 ./pcmcia_ctl.py %s" % action)


def describe(target, action, remote_cmd, remote_path):
    """Печать плана: что и где будет выполняться."""
    console.head("ЧТО БУДЕТ СДЕЛАНО")
    console.say(f"  команда: {action}")
    console.say(f"  ноутбук: {target.address} (порт {target.port})")
    console.say(f"  папка:   {REMOTE_DIR}")
    console.say(f"  запуск:  {remote_cmd}")
    if rctl.needs_image(action) or action == "read":
        console.say(f"  файл на ноутбуке: {remote_path}")
