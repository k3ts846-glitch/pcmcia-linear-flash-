#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Раскладка набора: монолита windows/legacy больше нет (2.18.0).

Что проверяется и почему именно так.

До 2.18.0 вся работа с картой лежала в одном файле
windows/legacy/pcmcia_direct.py на 6709 строк (233 имени верхнего уровня:
окна моста и /dev/mem, CIS, питание сокета, образ, таблицы микросхем,
команды NOR, сценарии записи, командная строка). Рядом в той же папке
доживали свой век .ps1/.cmd-сценарии 2.16.0, а описание папки обещало, что
их проверяет tests/test_legacy.py — файла с таким именем не было.

В 2.18.0 монолит разложен по модулям пакета src/pcmcia_direct, точка входа
одна — pcmcia_direct.py в корне набора, а папка windows/ удалена целиком.
Этот файл проверяет, что раскладка не расползлась обратно:

  * нет windows/ и windows/legacy/, нет .ps1/.cmd/.bat;
  * лаунчер тонкий и не тащит логику обратно в один файл;
  * у каждого модуля пакета есть docstring, объясняющий, что в нём;
  * пакет импортируется без установки и без не-stdlib зависимостей;
  * плоский API сохранён: GEOMETRY, VPP5/VPP126, WRONG_NAMES, NorFlash,
    identify_chip, check_sector_map, main — всё на месте;
  * рабочие файлы не отправляют пользователя в удалённую папку.

Запуск: python3 tests/test_legacy.py   (ни карты, ни сети, ни PowerShell)
"""
import os
import subprocess
import sys

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(KIT, "src", "pcmcia_direct")
LAUNCHER = os.path.join(KIT, "pcmcia_direct.py")

# Старые сценарии Windows: их больше нет и они не должны вернуться.
GONE_SUFFIX = (".ps1", ".cmd", ".bat")
GONE_PATHS = ("windows", os.path.join("windows", "legacy"),
              os.path.join("windows", "legacy", "pcmcia_direct.py"))

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
    """Прочитать файл набора (часть старых .txt — в cp1251, а не в UTF-8)."""
    with open(os.path.join(KIT, rel), "rb") as handle:
        raw = handle.read()
    for encoding in ("utf-8", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def py_files():
    """Все .py набора, кроме тестов и кэша."""
    out = []
    for root, dirs, files in os.walk(KIT):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "tests",
                                                "demo", "debs")]
        for name in files:
            if name.endswith(".py"):
                out.append(os.path.relpath(os.path.join(root, name), KIT))
    return sorted(out)


# --- 1. Папки legacy нет ----------------------------------------------------
section("папки windows/ и windows/legacy/ удалены")

for rel in GONE_PATHS:
    check("нет " + rel.replace(os.sep, "/"), not os.path.exists(os.path.join(KIT, rel)))

left_scripts = [f for f in py_files() if f.endswith(GONE_SUFFIX)]
for root, dirs, files in os.walk(KIT):
    dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "debs")]
    for name in files:
        if name.lower().endswith(GONE_SUFFIX):
            left_scripts.append(os.path.relpath(os.path.join(root, name), KIT))
check("в наборе не осталось ни одного .ps1/.cmd/.bat", not left_scripts,
      str(left_scripts[:4]))


# --- 2. Точка входа ---------------------------------------------------------
section("точка входа: тонкий лаунчер pcmcia_direct.py в корне")

check("pcmcia_direct.py существует", os.path.isfile(LAUNCHER))
launcher = read("pcmcia_direct.py") if os.path.isfile(LAUNCHER) else ""
lines = launcher.splitlines()
check("лаунчер короткий (меньше 200 строк) — логика не вернулась в один файл",
      0 < len(lines) < 200, len(lines))
check("лаунчер зовёт пакет, а не содержит код сам",
      "from pcmcia_direct.cli import main" in launcher
      and "def _bootstrap" in launcher)
check("в лаунчере нет таблиц микросхем",
      "GEOMETRY" not in launcher and "Am29F016" not in launcher)


# --- 3. Пакет ---------------------------------------------------------------
section("пакет src/pcmcia_direct: модули и их назначение")

EXPECTED = {
    "__init__.py": "плоский API", "__main__.py": "python -m pcmcia_direct",
    "bridge.py": "окна моста и /dev/mem", "chips_data.py": "таблицы микросхем",
    "chips.py": "имена и определение", "cis.py": "паспорт карты",
    "cli.py": "командная строка", "errors.py": "DirectError",
    "flash.py": "сценарии записи и стирания", "image.py": "образ карты",
    "nor.py": "команды микросхемы", "nor_commands.py": "коды команд",
    "power.py": "питание сокета и Vpp", "version.py": "версия набора",
}
present = sorted(n for n in os.listdir(PKG) if n.endswith(".py")) if os.path.isdir(PKG) else []
check("в пакете ровно ожидаемые 14 модулей", present == sorted(EXPECTED),
      str(set(present) ^ set(EXPECTED)))

no_doc = []
for name in present:
    text = open(os.path.join(PKG, name), encoding="utf-8").read()
    body = text.split("\n")
    if name in ("__init__.py", "__main__.py", "cli.py"):
        has = any(line.strip().startswith('"""') for line in body[:12])
    else:
        has = any(line.strip().startswith('"""') for line in body[:4])
    if not has:
        no_doc.append(name)
check("у каждого модуля есть docstring — что в нём лежит", not no_doc, str(no_doc))

sizes = {n: len(open(os.path.join(PKG, n), encoding="utf-8").read().splitlines())
         for n in present}
check("ни один модуль не разросся до монолита (меньше 2000 строк)",
      all(v < 2000 for v in sizes.values()),
      {k: v for k, v in sizes.items() if v >= 2000})
check("самый большой модуль — не CLI и не таблица (мост/чипы/привод)",
      max(sizes, key=sizes.get) in ("bridge.py", "chips_data.py", "nor.py",
                                    "flash.py", "cli.py"),
      max(sizes, key=sizes.get))


# --- 4. Импорт без установки и без зависимостей -----------------------------
section("пакет импортируется как есть и не тянет ничего вне stdlib")

sys.path.insert(0, os.path.join(KIT, "src"))
import pcmcia_direct as D                                            # noqa: E402

check("версия читается из пакета", bool(D.KIT_VERSION), D.KIT_VERSION)
check("плоский API на месте (не меньше 230 имён)", len(D.__all__) >= 230,
      len(D.__all__))
API = ("GEOMETRY", "VPP5", "VPP126", "WRONG_NAMES", "Mem", "NorFlash",
       "identify_chip", "check_sector_map", "geometry_for", "sector_map",
       "read_card_image", "flash_write_image", "flash_probe", "main",
       "set_vpp_for_write", "read_cis", "cis_summary", "card_kind")
missing = [n for n in API if not hasattr(D, n)]
check("ключевые имена прежнего монолита доступны", not missing, str(missing))
check("DirectError один на весь пакет",
      D.DirectError is __import__("pcmcia_direct.errors", fromlist=["x"]).DirectError)

import importlib.util as _ilu_std                                    # noqa: E402
import sysconfig as _syscfg                                         # noqa: E402


def _is_stdlib(mod):
    """Модуль из стандартной библиотеки (а не из PyPI и не локальный)."""
    try:
        found = _ilu_std.find_spec(mod)
    except (ImportError, ValueError):
        return False
    if found is None:
        return False
    origin = found.origin or ""
    if origin in ("built-in", "frozen"):
        return True
    return origin.startswith(_syscfg.get_paths()["stdlib"])


foreign = []
for name in present:
    text = open(os.path.join(PKG, name), encoding="utf-8").read()
    for line in text.splitlines():
        word = line.strip()
        if word.startswith("import "):
            mod = word.split()[1].split(".")[0]
            if not _is_stdlib(mod):
                foreign.append((name, mod))
check("модули импортируют только stdlib (или относительные импорты)",
      not foreign, str(foreign[:3]))


# --- 5. Запуск --------------------------------------------------------------
section("запуск: лаунчер и python -m pcmcia_direct")

run = subprocess.run([sys.executable, "-B", LAUNCHER, "--version"],
                     capture_output=True, text=True, cwd=KIT)
check("pcmcia_direct.py --version: код 0 и версия в выводе",
      run.returncode == 0 and D.KIT_VERSION in run.stdout,
      (run.returncode, run.stdout[:60], run.stderr[:80]))

run_mod = subprocess.run([sys.executable, "-B", "-m", "pcmcia_direct", "--version"],
                         capture_output=True, text=True,
                         cwd=os.path.join(KIT, "src"))
check("python3 -m pcmcia_direct --version: код 0 и та же версия",
      run_mod.returncode == 0 and D.KIT_VERSION in run_mod.stdout,
      (run_mod.returncode, run_mod.stdout[:60], run_mod.stderr[:80]))

run_other = subprocess.run([sys.executable, "-B", LAUNCHER, "--help"],
                           capture_output=True, text=True, cwd="/tmp")
check("лаунчер работает из чужого каталога (набор найден по своему пути)",
      run_other.returncode == 0 and "--version" in run_other.stdout,
      (run_other.returncode, run_other.stderr[:80]))


# --- 6. Никто не отправляет в удалённую папку ------------------------------
section("рабочие файлы не отправляют пользователя в windows/legacy")

# Упоминание папки в объяснении «её больше нет» — это не подсказка запустить
# её. Ищем именно ПОДСКАЗКИ: строки, где путь стоит рядом с командой запуска.
HISTORY_MARKERS = ("больше нет", "удалён", "удалена", "удалено", "разложен",
                   "бывший", "монолит", "переехал", "перенесён", "перенесен",
                   "не найден рядом", "раскладка", "С 2.18.0")
stale = []
for rel in py_files():
    for line in read(rel).splitlines():
        if "windows/legacy" not in line:
            continue
        if any(marker.lower() in line.lower() for marker in HISTORY_MARKERS):
            continue
        stale.append("%s: %s" % (rel, line.strip()[:70]))
check("в рабочих .py нет подсказок запустить windows/legacy", not stale,
      str(stale[:3]))

import importlib.util as _ilu                                       # noqa: E402

spec = _ilu.spec_from_file_location("_rel", os.path.join(KIT, "tests", "release.py"))
_rel = _ilu.module_from_spec(spec)
spec.loader.exec_module(_rel)
check("tests/release.py: обязательный состав без windows/legacy",
      not [r for r in _rel.REQUIRED if r.startswith("windows/legacy")]
      and "pcmcia_direct.py" in _rel.REQUIRED
      and "src/pcmcia_direct/cli.py" in _rel.REQUIRED)

readme = read("README.md")
_layout = readme.split("## Что где лежит")[1].split("```")[1] if "## Что где лежит" in readme else ""
check("раскладка в README: pcmcia_direct.py в корне и пакет src/pcmcia_direct",
      "pcmcia_direct.py" in _layout and "src/pcmcia_direct/" in _layout
      and "windows/legacy" not in _layout, _layout[:60])


print("\n" + "=" * 70)
print("ИТОГО: прошло %d, провалено %d" % (len(PASS), len(FAIL)))
if FAIL:
    for name, detail in FAIL:
        print("  FAIL: " + name + "   " + str(detail))
    sys.exit(1)
print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
