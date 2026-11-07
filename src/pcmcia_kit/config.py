#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Настройки пульта: значения по умолчанию и всё, что зависит от машины.

Отдельный модуль нужен потому, что одни и те же значения (адрес ноутбука,
порт, путь к ключу, папка набора) нужны половине команд, а задавать их в
каждой команде заново — значит рано или поздно разойтись. Здесь же собраны
сведения о раскладке набора, чтобы команды не искали файлы «на глаз».

Проверки: tests/test_win.py.
"""
import os

# --- умолчания -------------------------------------------------------------
DEFAULT_HOST = "192.168.0.94"
DEFAULT_USER = "root"
DEFAULT_PORT = 22

# Каталог на ноутбуке, куда копируется набор. /tmp — чтобы после перезагрузки
# ничего не оставалось.
REMOTE_DIR = "/tmp/pcmcia-ctl"

# Файлы набора, которые нужны на ноутбуке. Пульт, справочник и тесты остаются
# на рабочей машине: это части набора для Windows, а не рабочие файлы Debian.
# ВАЖНО: имена должны существовать в наборе. Здесь были planb.sh и
# pcmcia_flash.sh - таких файлов нет (есть planb.py и linux/pcmcia_flash.py),
# поэтому planb.py не уезжал на ноутбук, и run_remote.py --planb на нём падал
# с «planb.py не найден».
REMOTE_FILES = (
    "pcmcia_ctl.py",
    # 2.18.0: монолит разложен по модулям, поэтому на ноутбук уезжает точка
    # входа pcmcia_direct.py и весь пакет src/pcmcia_direct. Раньше здесь был
    # один файл windows/legacy/pcmcia_direct.py — с 2.18.0 это лаунчер
# pcmcia_direct.py и пакет src/pcmcia_direct.
    "pcmcia_direct.py",
    "src/pcmcia_direct/__init__.py",
    "src/pcmcia_direct/__main__.py",
    "src/pcmcia_direct/bridge.py",
    "src/pcmcia_direct/chips.py",
    "src/pcmcia_direct/chips_data.py",
    "src/pcmcia_direct/cis.py",
    "src/pcmcia_direct/cli.py",
    "src/pcmcia_direct/errors.py",
    "src/pcmcia_direct/flash.py",
    "src/pcmcia_direct/image.py",
    "src/pcmcia_direct/nor.py",
    "src/pcmcia_direct/nor_commands.py",
    "src/pcmcia_direct/power.py",
    "src/pcmcia_direct/version.py",
    "collect_all.py",
    "run_remote.py",
    "remote_collect.py",
    "planb.py",
)

# Без этих файлов пульт работать не должен: если их нет, распакован не тот
# архив или указана не та папка.
LOCAL_REQUIRED = ("pcmcia_ctl.py", "pcmcia_direct.py", "collect_all.py",
                  "run_remote.py", "src/pcmcia_direct/cli.py")

# Команды, которым нужен файл образа на этой машине.
NEEDS_IMAGE = ("write", "verify", "fwwrite")

# Команды, которые меняют содержимое карты: для них спрашивается
# подтверждение и проверяется размер образа.
DESTRUCTIVE = ("write", "erase", "fwwrite")

# Все команды пульта в порядке меню. Здесь только то, что выполняется на
# ноутбуке; настройка ключа (key) — команда этой машины и живёт отдельно.
ACTIONS = (
    "info", "read", "write", "erase", "verify", "canwrite", "diag",
    "report", "fwprobe", "fwwrite", "chip", "selftest",
)

# Команды, которые выполняются на этой машине и ноутбука не требуют.
LOCAL_ACTIONS = ("key",)

# Пункты меню: номер, команда, пояснение. Номера сохранены от прежнего пульта
# (PowerShell), чтобы привычные пункты оставались на месте.
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
    ("12", "selftest", "Проверить сам набор (тесты; карта не нужна, около 20 с)"),
    ("13", "key", "Настроить вход по ключу (без пароля; ноутбук не нужен)"),
)

# Имя файла памятки о карте (лежит рядом с набором, не в профиле).
STATE_FILE = ".card-state.txt"


def default_key():
    """Путь к ключу по умолчанию: ~/.ssh/pcmcia_key."""
    return os.path.join(os.path.expanduser("~"), ".ssh", "pcmcia_key")


def kit_search_paths(explicit=""):
    """Где пульт ищет файлы набора, в порядке предпочтения.

      1) папка, указанная пользователем (--kit-dir);
      2) корень дерева, если пакет запущен прямо из папки набора;
      3) текущий каталог — так работает установленный пакет (pip install .),
         когда код лежит в site-packages, а набор остаётся, где распакован.
    """
    if explicit:
        return [os.path.abspath(explicit)]
    here = os.path.dirname(os.path.abspath(__file__))        # src/pcmcia_kit
    tree_root = os.path.dirname(os.path.dirname(here))       # корень набора
    return [tree_root, os.path.abspath(os.getcwd())]


def kit_dir(explicit=""):
    """Папка набора: первая из путей поиска, где лежит pcmcia_direct.py.

    Если такого нет, возвращается первый кандидат — чтобы сообщение об ошибке
    называло осмысленный путь, а не пустую строку.
    """
    candidates = kit_search_paths(explicit)
    for candidate in candidates:
        if os.path.isfile(os.path.join(candidate, "pcmcia_direct.py")) \
                or os.path.isdir(os.path.join(candidate, "src", "pcmcia_direct")):
            return candidate
    return candidates[0]


def has_key(path):
    """Есть ли годный файл ключа."""
    return bool(path) and os.path.isfile(path)
