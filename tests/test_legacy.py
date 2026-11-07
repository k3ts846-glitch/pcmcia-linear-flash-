#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Старые сценарии: то, что лежит в windows/legacy, и то, что о них написано.

Зачем отдельный файл: в windows/legacy лежал «запасной вариант» 2.16.0 —
pcmcia_win.py, add_key.py и ПУСК.cmd. Все три начинались с

    from kitlib import console, kstate, ksync

а модуля kitlib в наборе не было ни в одной ревизии git, поэтому вместо
запасного варианта получался ModuleNotFoundError на первом же импорте.
Описание папки обещало при этом, что набор проверяет tests/test_legacy.py —
файла с таким именем тоже не было. Два обещания, ни одно не выполнялось.

В 2.17.19 мёртвые файлы убраны, подсказки набора переписаны на существующие
команды, а это и есть тот самый tests/test_legacy.py — теперь он проверяет,
что описание папки и содержимое папки не расходятся.

Запуск: python3 tests/test_legacy.py   (ни карты, ни сети, ни PowerShell)
"""
import ast
import os
import sys

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEGACY = os.path.join(KIT, "windows", "legacy")
README = os.path.join(LEGACY, "README_УСТАРЕЛО.txt")

# Единственный .py, которому здесь место: главный движок набора. Он переехал
# в windows/legacy в 2.16.0 и оттуда его читают пакет, пульт и тесты.
PY_ALLOWED = {"pcmcia_direct.py"}

# Файлы, которых больше нет и которые не должны вернуться или упоминаться
# как рабочие.
GONE = ("pcmcia_win.py", "add_key.py", "ПУСК.cmd")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  ok   " + name)
    else:
        FAIL.append((name, detail))
        print("  FAIL " + name + ("   " + str(detail) if detail else ""))


def section(title):
    print("\n[" + title + "]")


def read(rel):
    """Прочитать файл набора. Часть старых .txt — в cp1251, а не в UTF-8."""
    with open(os.path.join(KIT, rel), "rb") as handle:
        raw = handle.read()
    for encoding in ("utf-8", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def files_in_legacy():
    return sorted(os.listdir(LEGACY))


def scripts_in_legacy():
    """.py/.ps1/.cmd/.bat из папки (без описания)."""
    return [n for n in files_in_legacy()
            if n.rsplit(".", 1)[-1].lower() in ("py", "ps1", "cmd", "bat")]


# --- 1. Мёртвого «запасного варианта» больше нет ------------------------------
section("в windows/legacy нет файлов, которые не запускаются")

left = [n for n in files_in_legacy() if n.endswith(".py") and n not in PY_ALLOWED]
check("единственный .py в windows/legacy — pcmcia_direct.py", not left, str(left))

back = [n for n in GONE if os.path.exists(os.path.join(LEGACY, n))]
check("pcmcia_win.py, add_key.py и ПУСК.cmd убраны (kitlib в наборе нет)",
      not back, str(back))

# Причина удаления должна быть проверяемой, а не «так решили»: если в наборе
# снова появится импорт kitlib, тест обязан это заметить.
importers = []
for root, dirs, names in os.walk(KIT):
    dirs[:] = [d for d in dirs
               if d not in (".git", "__pycache__", ".pytest_cache", "build", "dist")]
    for name in names:
        if not name.endswith(".py"):
            continue
        path = os.path.join(root, name)
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for mod in mods:
                if mod == "kitlib" or mod.startswith("kitlib."):
                    importers.append(os.path.relpath(path, KIT))
check("ни один файл набора не импортирует kitlib", not importers,
      str(sorted(set(importers))))

# --- 2. Описание папки совпадает с папкой -------------------------------------
section("README_УСТАРЕЛО.txt описывает то, что действительно лежит")

check("описание папки на месте", os.path.isfile(README), README)
text = open(README, encoding="utf-8").read() if os.path.isfile(README) else ""

missing = [n for n in scripts_in_legacy() if n not in text]
check("каждый файл папки назван в описании", not missing, str(missing))

named = []
for name in os.listdir(LEGACY):
    base = name.rsplit(".", 1)[-1].lower()
    if base in ("ps1", "cmd", "bat"):
        named.append(name)
absent = [n for n in named if n not in files_in_legacy()]
check("описание не отправляет читать несуществующие сценарии", not absent,
      str(absent))

check("описание прямо говорит, что сценарии не используются",
      "НЕ ИСПОЛЬЗУЮТСЯ" in text and "архив" in text, text[:60])
check("описание называет причину удаления Python-версии (kitlib)",
      "kitlib" in text and "ModuleNotFoundError" in text, "")

# Прошлое описание обещало проверку tests/test_legacy.py, которой не было.
check("tests/test_legacy.py, на который ссылается описание, существует",
      "tests/test_legacy.py" in text
      and os.path.isfile(os.path.join(KIT, "tests", "test_legacy.py")), "")

# --- 3. Рабочий путь на месте -------------------------------------------------
section("рабочий путь — Python, и он существует")

for rel, what in (("pcmcia-kit.pyw", "двойной щелчок в Windows"),
                  ("src/pcmcia_kit/__main__.py", "python -m pcmcia_kit"),
                  ("src/pcmcia_kit/keyring.py", "вход по ключу")):
    check("рабочий путь: %s (%s)" % (rel, what),
          os.path.isfile(os.path.join(KIT, rel)), rel)

# --- 4. Подсказки набора не отправляют к удалённым файлам ---------------------
section("подсказки в коде называют существующие команды")

transport = read("src/pcmcia_kit/transport.py")
dead_hints = [name for name in GONE if name in transport]
check("в подсказках ssh нет удалённых программ", not dead_hints, str(dead_hints))
check("подсказка про другого пользователя называет работающий ключ --user",
      "--user" in transport and "pcmcia_kit info" in transport, "")
check("подсказка про вход без пароля называет pcmcia_kit key",
      "pcmcia_kit key" in transport, "")

release = read("tests/release.py")
check("текст выпуска не советует ПУСК.cmd и PowerShell-сценарии",
      "ПУСК.cmd" not in release and "kit_sync.ps1" not in release
      and "kit_state.ps1" not in release, "")

# Инструкции, которые читает человек перед работой (не история выпусков:
# README.md и ПЛАН_РАЗВИТИЯ_2.17.md рассказывают, как было, - им можно).
GUIDES = ("ИНСТРУКЦИЯ.md", "README_FIRST.txt", "ПОРЯДОК_ЗАПУСКА.txt",
          "КАК_УПРАВЛЯТЬ_С_WINDOWS.txt", "00_READ_ME_RU.txt")
stale_guides = []
for name in GUIDES:
    if not os.path.isfile(os.path.join(KIT, name)):
        stale_guides.append(name + " (нет файла)")
        continue
    body = read(name)
    for gone in GONE:
        if gone in body:
            stale_guides.append("%s: %s" % (name, gone))
check("инструкции не советуют запускать удалённые файлы", not stale_guides,
      str(stale_guides))

# --- итог ---------------------------------------------------------------------
print("\n" + "=" * 70)
print("ИТОГО: прошло %d, провалено %d" % (len(PASS), len(FAIL)))
if FAIL:
    for name, detail in FAIL:
        print("  FAIL: " + name + ("   " + str(detail) if detail else ""))
    sys.exit(1)
print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
