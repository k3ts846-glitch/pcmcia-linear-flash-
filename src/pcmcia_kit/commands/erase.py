#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Стирание карты: единственная команда, которая уничтожает данные целиком.

Поэтому здесь два предохранителя, которых нет у остальных команд:
  * без --yes подтверждение спрашивается ещё раз уже на этой машине;
  * в подсказке сказано, что стирается массив, а паспорт (CIS) остаётся.
"""
from .. import console, steps
from .. import remote as rctl


def run(target, kit_dir, action="erase", assume_yes=False, remote_path="",
        confirmed_in_menu=False):
    """Стереть карту. Возвращает код возврата.

    confirmed_in_menu=True означает, что человек уже подтвердил выбор пункта
    меню: дважды спрашивать «стереть всё?» — не забота, а раздражение.
    """
    if not (assume_yes or confirmed_in_menu) and not console.ask_yes("Стереть ВСЁ содержимое карты?"):
        console.say("  отменено: карта не тронута. Для стирания добавьте --yes")
        return 0, ""

    remote_cmd = rctl.build(action, remote_path, assume_yes=True)
    steps.describe(target, action, remote_cmd, remote_path)
    console.say("  стирается массив карты; паспорт карты остаётся на месте")

    if not steps.connect(target):
        return 1, ""
    if not steps.send_kit(target, kit_dir):
        return 1, ""
    steps.skip_image(action)

    code, text = steps.execute(target, remote_cmd, action)
    if code == 3:
        steps.needs_sudo_error(target, action)
        return 3, text
    steps.finish(target)
    return code, text
