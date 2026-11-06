#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Меню пульта: то, что видит человек при двойном щелчке по файлу запуска.

Меню не пытается угадать, чего хочет пользователь: оно показывает состояние
(адрес ноутбука, версию набора, последний опрос карты) и список действий, а
выбор передаёт нужной команде. Номера пунктов сохранены от прежнего пульта.
"""
from .. import console, state
from ..config import MENU_ITEMS


def show(target, version, kit_dir):
    """Показать меню и вернуть выбранную команду ('' — выход)."""
    console.say("")
    console.say("================================================================")
    console.say("  ПУЛЬТ УПРАВЛЕНИЯ КАРТОЙ (Windows -> ноутбук, по ssh/scp)")
    console.say("================================================================")
    console.say(f"  ноутбук: {target.address} порт {target.port}   набор: {version}")
    if target.has_key():
        console.say(f"  вход:   по ключу {target.key_file}")
    else:
        console.say("  вход:   по паролю (спросит ssh); без пароля — пункт 13")
    state.show(kit_dir, console.say)
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
    console.say("  такого пункта нет — выхожу")
    return ""


def confirm(action, title):
    """Спросить подтверждение для действия, которое меняет карту."""
    if action in ("write", "erase", "fwwrite"):
        console.say("")
        console.say(f"  Выбрано: {title}")
        return console.ask_yes("Продолжить?")
    return True
