#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Команды только для чтения: паспорт, микросхема, вердикт, диагностика.

info, chip, canwrite, diag ничего не меняют на карте — это важно и в коде, и
в тексте подсказок: на карте могут лежать единственные данные. Общая часть у
них одна, поэтому все четыре собраны в одном модуле.
"""
from .. import console, steps, state
from .. import remote as rctl

# Пояснения, которые печатаются до работы: что именно будет прочитано.
HINTS = {
    "info": "читаю паспорт карты и отвечаю, что с ней можно делать",
    "chip": "опрашиваю микросхему (команды 90h/98h — только чтение)",
    "canwrite": "проверяю, можно ли писать именно эту карту",
    "diag": "собираю диагностику: окна памяти, драйвер, dmesg",
}

# После этих команд состояние карты запоминается для шапки меню.
REMEMBER = ("info", "chip")


def run(target, kit_dir, action, remote_path="", assume_yes=False, full=False,
        json_out=False):
    """Выполнить команду чтения. Возвращает код возврата."""
    hint = HINTS.get(action)
    if hint and not json_out:
        console.say(f"  {hint}")
    remote_cmd = rctl.build(action, remote_path, assume_yes, full,
                            json_out=json_out)
    steps.describe(target, action, remote_cmd, remote_path)

    if not steps.connect(target):
        return 1
    if not steps.send_kit(target, kit_dir):
        return 1
    steps.skip_image(action)

    if json_out:
        # Машинный вывод: печатаем JSON как есть, без обёртки шагов.
        code, text = target.run(remote_cmd)
        if code == 3:
            steps.needs_sudo_error(target, action)
            return 3
        # Выводим JSON как есть (pcmcia_ctl.py уже отформатировал).
        print(text, end="")
        steps.finish(target)
        return code

    code, text = steps.execute(target, remote_cmd, action)
    if code == 3:
        steps.needs_sudo_error(target, action)
        return 3
    if action in REMEMBER:
        steps.remember_card(kit_dir, text, target.host)

    # Карта износа: при canwrite предупреждаем о секторах, которые ранее
    # не поддавались стиранию или записи.
    if action == "canwrite":
        entries = state.wear_entries(kit_dir)
        if entries:
            console.say("")
            console.say("  ---- карта износа ----")
            for entry in entries:
                console.say(
                    f"  сектор {entry['sector']:#x}: ранее не "
                    f"пройден ({entry['type']}, "
                    f"байт {entry['byte']:#x}, {entry['when']}) — "
                    f"запись туда может не повториться."
                )
            console.say("  ---------------------")
        # Карта отказов: конкретные байты, которые не записались.
        failures = state.failure_entries(kit_dir)
        if failures:
            console.say("")
            console.say("  ---- карта отказов ----")
            for entry in failures:
                console.say(
                    f"  смещение {entry['offset']:#x}: "
                    f"ожидалось {entry['expected']:#04x}, "
                    f"прочитано {entry['actual']:#04x} "
                    f"({entry['date']}, образ {entry['image'] or '—'})"
                )
            console.say("  ---------------------")

    steps.collect(target, action, remote_path, "/")
    steps.finish(target)
    return code
