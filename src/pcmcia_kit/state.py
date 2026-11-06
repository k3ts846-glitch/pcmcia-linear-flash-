#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Последнее состояние карты: памятка в шапке пульта и карта износа секторов.

Один и тот же вопрос («что за карта») задаётся каждый раз заново, а ответ
печатается в консоли и теряется при закрытии окна. Здесь главные строки
последнего опроса сохраняются в файл рядом с набором и показываются в шапке
меню: человек видит, с чем работает, ещё до того как что-то нажал.

Кроме того, здесь фиксируются секторы, которые не поддались стиранию или
записи (карта износа). При следующей команде `canwrite` пульт предупреждает:
в этих секторах запись может не пройти ещё раз. Это главное отличие б/у PCMCIA-
карт от новых: bad-блоки — norma, а не исключение.

В файл попадают только строки из белого списка — никакие пароли и посторонний
шум ssh туда не попадут. Проверки — в tests/test_win.py.
"""
import json
import os
import re
import tempfile
import time

from .config import STATE_FILE

# Строки опроса, которые стоит помнить. Каждый шаблон — часть заголовка строки
# отчёта; сравнение без учёта регистра.
KEEP_MARKERS = (
    "микросхема",
    "напряжение программир",
    "сектор стирания",
    "протокол команд",
    "вид карты",
    "переключатель защиты",
    "паспорт",
)

# Маркер раздела карты износа внутри .card-state.txt.
WEAR_SECTION = "--- износ ---"

# Маркер раздела карты отказов (bad-блоки).
FAILURE_SECTION = "--- отказы ---"

# Строка износа: WORN <sector_offset> <failure_type> <byte_offset> <timestamp>
_WEAR_LINE = re.compile(
    r"^WORN\s+(0x[0-9a-fA-F]+)\s+(\w+)\s+(0x[0-9a-fA-F]+)\s+(.+)$"
)

# Строка отказа: FAIL <offset> <expected> <actual> <date> <image>
_FAILURE_LINE = re.compile(
    r"^FAIL\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+(.+?)\s+(\S+)$"
)


def state_path(base_dir):
    """Путь к файлу состояния. Каталог задаёт вызывающий код.

    В Python нет подвоха с $PSScriptRoot внутри функции, но правило остаётся:
    путь строится только от явно переданной папки набора, а не от текущего
    каталога — иначе файл состояния терялся бы там, откуда запущен пульт.
    """
    return os.path.join(base_dir, STATE_FILE)


def pick_lines(text):
    """Выбрать из вывода опроса строки, которые стоит запомнить."""
    kept = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if any(marker in low for marker in KEEP_MARKERS):
            kept.append(line)
    return kept


def _write_state_atomic(base_dir, lines):
    """Записать строки в .card-state.txt атомарно (temp + os.replace).

    Запись идёт во временный файл в той же папке, затем атомарно заменяет
    целевой. Это предотвращает повреждение файла при обрыве питания или
    одновременной записи из двух процессов.
    """
    path = state_path(base_dir)
    fd, tmp_path = tempfile.mkstemp(
        dir=base_dir, prefix=".card-state.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return False
    return True


def save(base_dir, text, host, when=None):
    """Сохранить состояние. Возвращает True, если что-то записано.

    Состояние — удобство, а не условие работы: ошибки записи не поднимаются,
    чтобы из-за занятого или доступного только для чтения файла не срывалась
    работа с картой.
    """
    lines = pick_lines(text)
    if not lines:
        return False
    stamp = (time.strftime("%Y-%m-%d %H:%M", time.localtime(when))
             if when else time.strftime("%Y-%m-%d %H:%M"))
    out = [f"{stamp}  {host}"]
    out.extend(lines)
    return _write_state_atomic(base_dir, out)


def load(base_dir):
    """Прочитать состояние. Возвращает (заголовок, строки) или (None, [])."""
    path = state_path(base_dir)
    if not os.path.isfile(path):
        return None, []
    try:
        with open(path, encoding="utf-8") as handle:
            rows = [line.rstrip("\n") for line in handle]
    except OSError:
        return None, []
    rows = [row for row in rows if row.strip()]
    if len(rows) < 2:
        return None, []
    return rows[0].strip(), rows[1:]


def show(base_dir, say=print):
    """Показать памятку в шапке меню. Ничего не печатает, если файла нет."""
    header, lines = load(base_dir)
    if not header:
        return False
    say(f"  карта (последний опрос {header}):")
    for line in lines:
        say("    " + line)
    return True


def clear(base_dir):
    """Забыть состояние (например, при смене ноутбука)."""
    try:
        os.remove(state_path(base_dir))
        return True
    except OSError:
        return False


# -------------------------------------------------------- карта износа -----

def _split_state_rows(rows):
    """Разбить строки .card-state.txt на информационные, износные и отказы.

    Возвращает (info_rows, wear_rows, failure_rows).
    wear_rows — после маркера `--- износ ---`.
    failure_rows — после маркера `--- отказы ---`.
    """
    info = []
    wear = []
    failures = []
    section = "info"
    for row in rows:
        stripped = row.strip()
        if stripped == WEAR_SECTION:
            section = "wear"
            continue
        if stripped == FAILURE_SECTION:
            section = "failure"
            continue
        if section == "wear":
            wear.append(row)
        elif section == "failure":
            failures.append(row)
        else:
            info.append(row)
    return info, wear, failures


def wear_entries(base_dir):
    """Загрузить карту износа из .card-state.txt.

    Возвращает список словарей:
        {"sector": int, "type": str, "byte": int, "when": str}
    Пустой список, если файла нет или в нём нет износных записей.
    """
    header, rows = load(base_dir)
    if header is None:
        return []
    _, wear_rows, _ = _split_state_rows(rows)
    entries = []
    for row in wear_rows:
        m = _WEAR_LINE.match(row)
        if not m:
            continue
        entries.append({
            "sector": int(m.group(1), 16),
            "type": m.group(2),
            "byte": int(m.group(3), 16),
            "when": m.group(4),
        })
    return entries


def wear_add(base_dir, sector, failure_type, byte_off, when=None, host=""):
    """Добавить запись о неисправном секторе в карту износа.

    sector   — смещение сектора (int);
    failure_type — "erase" | "write" | "blank";
    byte_off — смещение первого неисправного байта (int);
    when     — метка времени (str), по умолчанию «сейчас»;
    host     — имя хоста (для заголовка файла).
    """
    if when is None:
        when = time.strftime("%Y-%m-%d %H:%M")

    # Читаем текущее состояние, чтобы сохранить все секции.
    header, rows = load(base_dir)
    if header is not None:
        info_rows, existing_wear, existing_failures = _split_state_rows(rows)
    else:
        info_rows, existing_wear, existing_failures = [], [], []

    # Собираем существующие износные записи (по сектору+типу+байту, без времени).
    entry_line = f"WORN {sector:#x} {failure_type} {byte_off:#x} {when}"
    existing_keys = set()
    new_wear = []
    for row in existing_wear:
        m = _WEAR_LINE.match(row)
        if m:
            key = (int(m.group(1), 16), m.group(2), int(m.group(3), 16))
            existing_keys.add(key)
            new_wear.append(row)
    new_key = (sector, failure_type, byte_off)
    if new_key not in existing_keys:
        new_wear.append(entry_line)

    # Пишем файл заново: заголовок + информация + износ + отказы.
    stamp = (header.split("  ")[0] if header
             else time.strftime("%Y-%m-%d %H:%M"))
    host_part = (host or
                 (header.split("  ")[1] if header and "  " in header else ""))
    out = [f"{stamp}  {host_part}"]
    out.extend(info_rows)
    out.append(WEAR_SECTION)
    out.extend(new_wear)
    if existing_failures:
        out.append(FAILURE_SECTION)
        out.extend(existing_failures)
    return _write_state_atomic(base_dir, out)


def wear_summary(base_dir, say=print):
    """Показать карту износа в консоли.

    Возвращает True, если есть что показать, иначе False.
    """
    entries = wear_entries(base_dir)
    if not entries:
        return False
    say("  карта износа (сектора, где запись не удавалась):")
    for entry in entries:
        say(f"    сектор {entry['sector']:#x} "
            f"({entry['type']}, байт {entry['byte']:#x}, {entry['when']})")
    return True


# -------------------------------------------------------- карта отказов -----

def failure_entries(base_dir):
    """Загрузить карту отказов из .card-state.txt.

    Возвращает список словарей:
        {"offset": int, "expected": int, "actual": int,
         "date": str, "image": str}
    Пустой список, если файла нет или в нём нет записей об отказах.
    """
    header, rows = load(base_dir)
    if header is None:
        return []
    _, _, failure_rows = _split_state_rows(rows)
    entries = []
    for row in failure_rows:
        m = _FAILURE_LINE.match(row)
        if not m:
            continue
        entries.append({
            "offset": int(m.group(1), 16),
            "expected": int(m.group(2), 16),
            "actual": int(m.group(3), 16),
            "date": m.group(4),
            "image": m.group(5),
        })
    return entries


def failure_add(base_dir, offset, expected, actual, when=None, image="",
                host=""):
    """Добавить запись о неисправном байте (отказе) в карту отказов.

    offset   — смещение байта (int);
    expected — ожидаемое значение (int);
    actual   — фактически прочитанное значение (int);
    when     — дата/время отказа (str), по умолчанию «сейчас»;
    image    — имя файла образа (str);
    host     — имя хоста (для заголовка файла).
    """
    if when is None:
        when = time.strftime("%Y-%m-%d %H:%M")

    entry_line = (f"FAIL {offset:#x} {expected:#x} {actual:#x} "
                  f"{when} {image}")

    # Читаем текущее состояние.
    header, rows = load(base_dir)
    if header is not None:
        info_rows, existing_wear, existing_failures = _split_state_rows(rows)
    else:
        info_rows, existing_wear, existing_failures = [], [], []

    # Проверяем, нет ли уже такой же записи (по offset+expected+actual).
    new_key = (offset, expected, actual)
    existing_keys = set()
    new_failures = []
    for row in existing_failures:
        m = _FAILURE_LINE.match(row)
        if m:
            key = (int(m.group(1), 16), int(m.group(2), 16),
                   int(m.group(3), 16))
            existing_keys.add(key)
            new_failures.append(row)
    if new_key not in existing_keys:
        new_failures.append(entry_line)

    # Пишем файл заново.
    stamp = (header.split("  ")[0] if header
             else time.strftime("%Y-%m-%d %H:%M"))
    host_part = (host or
                 (header.split("  ")[1] if header and "  " in header else ""))
    out = [f"{stamp}  {host_part}"]
    out.extend(info_rows)
    if existing_wear:
        out.append(WEAR_SECTION)
        out.extend(existing_wear)
    if new_failures:
        out.append(FAILURE_SECTION)
        out.extend(new_failures)
    return _write_state_atomic(base_dir, out)


def failure_summary(base_dir, say=print):
    """Показать карту отказов в консоли.

    Возвращает True, если есть что показать, иначе False.
    """
    entries = failure_entries(base_dir)
    if not entries:
        return False
    say("  карта отказов (байты, которые не записались/не прочитались):")
    for entry in entries:
        say(f"    смещение {entry['offset']:#x}: "
            f"ожидалось {entry['expected']:#04x}, "
            f"прочитано {entry['actual']:#04x} "
            f"({entry['date']}, образ {entry['image'] or '—'})")
    return True
