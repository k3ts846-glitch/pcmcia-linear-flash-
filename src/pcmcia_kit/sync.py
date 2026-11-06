#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Какие файлы набора уходят на ноутбук и какие уже там.

Раньше набор копировался целиком при каждом запуске: шесть файлов и полминуты
на каждой итерации, хотя меняется обычно один. Здесь сравниваются контрольные
суммы (md5) и в список попадают только изменившиеся файлы.

Проверки — в tests/test_win.py (на настоящих файлах, без сети).
"""
import hashlib
import os
import re

from .config import REMOTE_FILES

_MD5_LINE = re.compile(r"^([0-9a-fA-F]{32})\s+\*?(\S+)\s*$")


def md5_of(path, chunk=1 << 20):
    """Контрольная сумма файла. Читаем кусками: образы бывают по 64 МиБ."""
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for piece in iter(lambda: handle.read(chunk), b""):
            digest.update(piece)
    return digest.hexdigest()


def local_md5(kit_dir, files=None):
    """Суммы файлов набора на этой машине. Отсутствующие файлы пропускаются."""
    result = {}
    for name in (files if files is not None else REMOTE_FILES):
        path = os.path.join(kit_dir, name)
        if os.path.isfile(path):
            result[name] = md5_of(path)
    return result


def parse_md5sum(text):
    """Разобрать вывод `md5sum` с ноутбука.

    Формат строки: «сумма  имя» (пробелов два) или «сумма *имя». Регистр суммы
    приводим к нижнему: на Windows он бывает в верхнем.
    """
    result = {}
    for line in text.splitlines():
        match = _MD5_LINE.match(line.strip())
        if match:
            result[match.group(2)] = match.group(1).lower()
    return result


def changed_files(local, remote):
    """Какие файлы надо отправить.

    Файл отправляется, если его нет на ноутбуке, если сумма не совпала или
    если сумма неизвестна (например, состояние на ноутбуке не прочиталось).
    Порядок — как в REMOTE_FILES, чтобы вывод был предсказуемым.
    """
    return [name for name in local if remote.get(name) != local[name]]


def sync(kit_dir, target, remote_dir, reporter=None):
    """Синхронизировать набор с ноутбуком. Возвращает список отправленных файлов.

    Ошибки транспорта не глотаются: если копирование не удалось, вызывающий
    код обязан остановиться и не трогать карту.
    """
    say = reporter or (lambda text: None)
    local = local_md5(kit_dir)
    if not local:
        raise FileNotFoundError(f"в папке {kit_dir} нет файлов набора")

    code, text = target.run(f"cd '{remote_dir}' 2>/dev/null && md5sum "
                            f"{' '.join(REMOTE_FILES)} 2>/dev/null")
    if code == 0:
        remote = parse_md5sum(text)
    else:
        say("  (состояние на ноутбуке не прочиталось — отправлю все файлы)")
        remote = {}

    to_send = changed_files(local, remote)
    if not to_send:
        say("  набор на ноутбуке уже актуален — копировать нечего")
        return []

    paths = [os.path.join(kit_dir, name) for name in to_send]
    code, text = target.copy_to(paths, f"{target.address}:{remote_dir}/")
    if code != 0:
        raise OSError(text.strip() or f"scp завершился с кодом {code}")
    say(f"  отправлено файлов: {len(to_send)} из {len(local)}"
        f" (остальные уже были там и совпали по md5)")
    return to_send
