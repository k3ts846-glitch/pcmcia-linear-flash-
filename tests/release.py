#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сборка выпуска набора: архивы и контрольные суммы.

    python3 tests/release.py            # проверить, что можно собирать
    python3 tests/release.py --build    # собрать ZIP, tar.gz и суммы

Зачем отдельный файл: раньше сборка делалась вручную, по памяти, и один раз
из неё выпали windows/, debs/ и demo/ — архив собрался «успешно», но неполным.
Здесь состав архива описан явно и проверяется ДО упаковки:

  * версия одна на все файлы (KIT_VERSION, RUN.ps1, collect_all.py,
    remote_collect.ps1);
  * в исходниках нет __pycache__ и временных файлов;
  * for-windows/ в ZIP - точная копия корня набора (иначе на ноутбук
    уезжает старый код);
  * в ZIP есть всё, что обещано в README: тесты, linux/, debs/, windows/,
    demo/ и справочник.

Коды возврата: 0 - всё в порядке (или собрано), 1 - есть замечания,
2 - собирать нельзя (расхождение версий или отсутствует нужный файл).
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPORT = "/home/user/one-archive/pcmcia-ssh-kit"
ZIP_PATH = "/home/user/pcmcia-ssh-kit.zip"
TAR_PATH = "/home/user/pcmcia-flash-kit.tar.gz"
MD5_PATH = "/home/user/MD5_АРХИВА.txt"

# Что обязательно должно попасть в ZIP: без этого набор неполон.
REQUIRED = [
    "windows/legacy/pcmcia_direct.py", "pcmcia_ctl.py", "collect_all.py", "run_remote.py",
    "remote_collect.py",
    # Технический долг (ещё не перенесены на Python):
    "linux/pcmcia_flash.py", "pcmcia_gui.py", "gui.py",
    # Windows-часть: пакет по стандарту, без .cmd и PowerShell
    "pcmcia-kit.pyw", "pyproject.toml", "README.md",
    "src/pcmcia_kit/cli.py", "src/pcmcia_kit/gui.py", "src/pcmcia_kit/config.py",
    "src/pcmcia_kit/commands/menu.py", "src/pcmcia_kit/commands/image.py",
    "README_FIRST.txt", "ИНСТРУКЦИЯ.md", "00_READ_ME_RU.txt",
    "КАК_УПРАВЛЯТЬ_С_WINDOWS.txt", "ПОРЯДОК_ЗАПУСКА.txt",
    "PCMCIA_LINEAR_FLASH_СПРАВОЧНИК.md",
    "tests/test_scripts.py", "tests/nor_model.py", "tests/test_v2.py",
    "tests/test_gui.py", "tests/test_ui_pro.py", "tests/test_win.py",
    "tests/test_legacy.py",
    # .bat вне windows/legacy в наборе нет с 2.16.0 (Windows-часть на Python);
    # диагностический сценарий переехал в windows/legacy вместе с остальным.
    "windows/legacy/collect_pcmcia_info.bat",
    "demo/01_карта.png",
]

# Служебное: не должно попадать ни в исходники, ни в архив.
JUNK_NAMES = {"__pycache__", ".card-state.txt", ".DS_Store"}
JUNK_SUFFIX = (".pyc", ".pyo", ".orig", ".rej", ".bak", "~")

# А вот ДЛЯ ПРОВЕРКИ дерева (check_tree) это разные вещи. Кэш Python и следы
# работы набора появляются от любого запуска и убираются clean_junk() при
# сборке - это не дефект (см. её docstring). Остатки правок в архив попасть не
# должны - это дефект. До 2.17.16 check_tree валила сборку за __pycache__ и
# .card-state.txt, то есть «собирать нельзя» было всегда.
JUNK_CACHE_NAMES = {"__pycache__", ".pytest_cache", "build", "dist",
                    ".card-state.txt", ".DS_Store"}
JUNK_LEFTOVER_SUFFIX = (".orig", ".rej", ".bak", "~")

# Где записана версия пакетов. В __init__.py литерала нет - там
# `from ._version import __version__`.
PACKAGES = ("src/pcmcia_kit/_version.py", "src/pcmcia_flash/_version.py")


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def kit_version():
    """Версия набора из KIT_VERSION (pcmcia_direct.py) - единственный источник.

    Совпадать с ней обязаны: src/pcmcia_kit/_version.py, src/pcmcia_flash/
    _version.py (у пакетов своя запись) и подсказка в collect_all.py.
    pyproject.toml версию не хранит, а берёт из пакета (attr)."""
    text = open(os.path.join(KIT, "windows", "legacy", "pcmcia_direct.py"), encoding="utf-8").read()
    m = re.search(r'^KIT_VERSION = "([^"]+)"', text, re.M)
    return m.group(1) if m else None


def package_version(rel=PACKAGES[0]):
    """Версия пакета из его _version.py (по умолчанию - пульт pcmcia_kit).

    Литерал лежит именно там: в __init__.py его нет (там
    `from ._version import __version__`). Прежний поиск по __init__.py всегда
    возвращал None, и сборка сообщала «нет __version__», хотя версия была.
    """
    path = os.path.join(KIT, rel)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    m = re.search(r'^__version__ = "([^"]+)"', text, re.M)
    return m.group(1) if m else None


def clean_junk():
    """Убрать из исходников кэш Python и следы работы перед сборкой.

    Кэш появляется от любого запуска набора, и это не дефект: сборка его
    выбрасывает сама, а не заставляет чистить руками. Возвращает число
    удалённых объектов (для отчёта).
    """
    removed = 0
    for root, dirs, files in os.walk(KIT):
        for name in list(dirs):
            if name in ("__pycache__", ".pytest_cache", "build", "dist"):
                shutil.rmtree(os.path.join(root, name), ignore_errors=True)
                dirs.remove(name)
                removed += 1
        for name in files:
            if name in JUNK_NAMES or name.endswith(JUNK_SUFFIX) or name.endswith(".pyc"):
                try:
                    os.remove(os.path.join(root, name))
                    removed += 1
                except OSError:
                    pass
    for egg in os.listdir(KIT):
        if egg.endswith(".egg-info"):
            shutil.rmtree(os.path.join(KIT, egg), ignore_errors=True)
            removed += 1
    return removed


def check_tree():
    """Проверки до сборки. Возвращает список замечаний (пусто - всё хорошо)."""
    problems = []
    ver = kit_version()
    if not ver:
        problems.append("KIT_VERSION не найден в windows/legacy/pcmcia_direct.py")
        return problems

    # 1. одна версия на все файлы, где она упоминается
    for name, pattern in (
        ("collect_all.py", r"должна быть ([0-9]+\.[0-9]+\.[0-9]+)"),
    ):
        path = os.path.join(KIT, name)
        if not os.path.exists(path):
            problems.append("нет файла " + name)
            continue
        m = re.search(pattern, open(path, encoding="utf-8-sig").read())
        if not m:
            problems.append(f"{name}: не нашёл ожидаемую версию")
        elif m.group(1) != ver:
            problems.append(f"{name}: версия {m.group(1)}, а набор {ver}")

    # 1б. версия КАЖДОГО пакета совпадает с версией набора
    for rel in PACKAGES:
        pkg = package_version(rel)
        if not pkg:
            problems.append("нет __version__ в " + rel)
        elif pkg != ver:
            problems.append(f"{rel}: версия {pkg}, а набора {ver}")

    # 1в. в рабочем пути нет .cmd/.bat/.ps1: Windows-часть на Python
    for name in sorted(os.listdir(KIT)):
        if name.lower().endswith((".cmd", ".bat", ".ps1")):
            problems.append("в корне набора командный файл: " + name)

    # 2. обязательные файлы на месте
    for rel in REQUIRED:
        if not os.path.exists(os.path.join(KIT, rel)):
            problems.append("нет обязательного файла " + rel)

    # 3. мусор в исходниках. Кэш Python и следы работы набора - не дефект:
    # их убирает clean_junk() при сборке. Дефект - остатки правок (.orig,
    # .rej, .bak, ~): они уехали бы в архив. И в .git не ходим: там своё.
    for root, dirs, files in os.walk(KIT):
        dirs[:] = [d for d in dirs
                   if d not in JUNK_CACHE_NAMES and d != ".git"]
        for f in files:
            if f.endswith(JUNK_LEFTOVER_SUFFIX):
                problems.append("остаток правки в исходниках: "
                                + os.path.join(root, f))
    return problems


def export():
    """Разложить набор по местам: архивная копия, for-windows, полные-файлы."""
    subprocess.run([sys.executable, os.path.join(KIT, "tests", "sync_kit.py")],
                   check=True, cwd=KIT)
    skip = {"tests", "linux", "__pycache__", "for-windows"}
    for name in sorted(os.listdir(KIT)):
        src = os.path.join(KIT, name)
        if not os.path.isfile(src) or name.startswith("."):
            continue
        for dst_dir in (EXPORT, os.path.join(EXPORT, "for-windows")):
            shutil.copy2(src, os.path.join(dst_dir, name))
    for sub in ("src", "windows", "demo"):
        dst = os.path.join(EXPORT, sub)
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        shutil.copytree(os.path.join(KIT, sub), dst)
    for sub in ("tests", "linux"):
        dst = os.path.join(EXPORT, sub)
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        shutil.copytree(os.path.join(KIT, sub), dst,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # for-windows: только то, что нужно на ноутбуке и на Windows при пульте
    for junk in ("tests", "linux", "debs", "demo", "windows", "src"):
        p = os.path.join(EXPORT, "for-windows", junk)
        if os.path.isdir(p):
            shutil.rmtree(p)
    return skip


def zip_file_list():
    """Что положить в ZIP: папка архива, затем её содержимое по списку."""
    items = [(EXPORT, "pcmcia-ssh-kit")]
    for root, dirs, files in os.walk(EXPORT):
        dirs[:] = sorted(d for d in dirs if d not in JUNK_NAMES)
        rel_root = os.path.relpath(root, EXPORT)
        for f in sorted(files):
            if f in JUNK_NAMES or f.endswith(JUNK_SUFFIX):
                continue
            rel = f if rel_root == "." else os.path.join(rel_root, f)
            items.append((os.path.join(root, f), "pcmcia-ssh-kit/" + rel))
    return items


def build_zip():
    if os.path.exists(ZIP_PATH):
        os.remove(ZIP_PATH)
    # Имена берём из файловой системы и передаём явно (а не «zip -r по
    # папке»): так состав архива совпадает с проверенным списком, а имена
    # записываются в UTF-8 (-UN=UTF8) и читаются на Windows 10/11 и 7-Zip.
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as z:
        for path, arc in zip_file_list():
            name = arc if path == EXPORT else arc
            if os.path.isdir(path):
                z.writestr(name + "/", b"")
                continue
            # каталоги тоже нужны в архиве, если внутри есть файлы
            parent = os.path.dirname(arc)
            if parent and parent + "/" not in z.namelist():
                z.writestr(parent + "/", b"")
            z.write(path, name)
    return md5(ZIP_PATH)


def build_tar():
    with tarfile.open(TAR_PATH, "w:gz") as tf:
        for name in sorted(os.listdir(KIT)):
            if name in ("__pycache__", ".card-state.txt"):
                continue
            tf.add(os.path.join(KIT, name), arcname="pcmcia-flash-kit/" + name,
                   filter=_skip_junk)
    return md5(TAR_PATH)


def _skip_junk(tarinfo):
    """Не включать в tar.gz кэш Python и следы работы."""
    parts = tarinfo.name.split("/")
    if any(part in ("__pycache__", "build", "dist") for part in parts):
        return None
    if any(part.endswith((".pyc", ".pyo")) for part in parts):
        return None
    if os.path.basename(tarinfo.name) in (".card-state.txt", ".DS_Store"):
        return None
    return tarinfo


def verify_tar():
    """В tar.gz кладём набор целиком: там должны быть и пакеты debs/."""
    problems = []
    with tarfile.open(TAR_PATH) as tf:
        names = tf.getnames()
    for rel in ("debs/INSTALL_OFFLINE.txt",
                "windows/legacy/collect_pcmcia_info.bat",
                "demo/01_карта.png", "tests/test_scripts.py", "tests/test_win.py",
                "src/pcmcia_kit/cli.py", "pyproject.toml", "pcmcia-kit.pyw",
                "PCMCIA_LINEAR_FLASH_СПРАВОЧНИК.md", "README.md"):
        if "pcmcia-flash-kit/" + rel not in names:
            problems.append("в tar.gz нет " + rel)
    for n in names:
        if "__pycache__" in n or n.endswith(".pyc"):
            problems.append("в tar.gz мусор: " + n)
            break
    return problems


def verify_zip():
    problems = []
    with zipfile.ZipFile(ZIP_PATH) as z:
        names = z.namelist()
        for rel in REQUIRED:
            if "pcmcia-ssh-kit/" + rel not in names:
                problems.append("в ZIP нет " + rel)
        for n in names:
            base = os.path.basename(n.rstrip("/"))
            if base in JUNK_NAMES or base.endswith(JUNK_SUFFIX):
                problems.append("в ZIP мусор: " + n)
            # в корне архива не должно быть командных файлов: пульт на Python
            if n.count("/") == 1 and base.lower().endswith((".cmd", ".bat", ".ps1")):
                problems.append("в корне ZIP командный файл: " + base)
        # for-windows не должен отставать от корня
        for n in names:
            if not n.startswith("pcmcia-ssh-kit/for-windows/"):
                continue
            rel = n[len("pcmcia-ssh-kit/"):]
            src = os.path.join(EXPORT, rel)
            core = os.path.join(EXPORT, rel.split("for-windows/", 1)[1])
            if not os.path.isfile(src) or not os.path.isfile(core):
                continue
            if md5(src) != md5(core):
                problems.append("for-windows отстаёт: " + rel.split("for-windows/")[1])
    return problems


def write_full_sums():
    """полные-файлы/MD5SUMS.txt: контрольные суммы каждого файла, что уезжает."""
    full = "/home/user/полные-файлы"
    for junk in os.listdir(full):        # следы работы (.card-state.txt и пр.)
        if junk.startswith("."):
            os.remove(os.path.join(full, junk))
    names = sorted(n for n in os.listdir(full)
                   if os.path.isfile(os.path.join(full, n)) and n != "MD5SUMS.txt")
    lines = [f"{md5(os.path.join(full, n))}  {n}" for n in names]
    open(os.path.join(full, "MD5SUMS.txt"), "w", encoding="utf-8").write(
        "\n".join(lines) + "\n")
    return len(lines)


def main():
    build = "--build" in sys.argv[1:]
    if build:
        cleaned = clean_junk()
        if cleaned:
            print(f"убрано мусора перед сборкой: {cleaned}")
    problems = check_tree()
    if problems:
        print("СОБИРАТЬ НЕЛЬЗЯ, пока не исправлено:")
        for p in problems:
            print("  - " + p)
        return 2
    ver = kit_version()
    print(f"версия {ver}: проверки до сборки пройдены")
    if not build:
        print("сборка не запускалась (добавьте --build)")
        return 0

    export()
    zip_md5 = build_zip()
    tar_md5 = build_tar()
    direct_md5 = md5(os.path.join(KIT, "windows", "legacy", "pcmcia_direct.py"))
    full_count = write_full_sums()
    problems = verify_zip() + verify_tar()

    counts = ""
    for name in ("test_scripts.py", "test_win.py", "test_v2.py", "test_gui.py",
                 "test_ui_pro.py"):
        t = open(os.path.join(KIT, "tests", name), encoding="utf-8").read()
        n = len(re.findall(r"\n\s+check\(", t))
        counts += f"  {name}: около {n} проверок\n"

    with zipfile.ZipFile(ZIP_PATH) as z:
        entries = len(z.namelist())
    text = f"""КОНТРОЛЬНЫЕ СУММЫ НАБОРА PCMCIA FLASH KIT — версия {ver}
(пересчитаны после сборки архивов; запись на железе НЕ подтверждена)

{zip_md5}  pcmcia-ssh-kit.zip
{tar_md5}  pcmcia-flash-kit.tar.gz
{direct_md5}  windows/legacy/pcmcia_direct.py  (внутри набора)

Что в {ver} (коротко):
  * справочник PCMCIA_LINEAR_FLASH_СПРАВОЧНИК.md: что за карты бывают, как
    устроены, где паспорт (CIS), переключатель защиты от записи, микросхемы,
    последовательность команд, типовые отказы;
  * защита от опасных действий: отказ писать при отмеченном переключателе
    защиты (код 4, --force-write) и остановка перед стиранием блока с
    паспортом (код 5, --allow-cis-erase);
  * страничная запись AMD Am29F016/032 (команда A0h, страница 32 байта на
    чип); при отказе называется первый несошедшийся байт страницы;
  * паспорт разбирается подробнее: 17h/19h/1E/40h/45h/46h, тип носителя
    (флеш, SRAM, ROM), батарея, размер, защита от записи;
  * Windows: двойной щелчок по pcmcia-kit.pyw и меню; пункт 12 - проверка
    набора; на ноутбук уходят только изменившиеся файлы; шапка меню
    помнит последний опрос карты;
  * Windows-часть переведена на Python целиком: пакет src/pcmcia_kit,
    запуск python -m pcmcia_kit или двойной щелчок по pcmcia-kit.pyw;
    ни .cmd, ни PowerShell в рабочем пути нет;
  * графический пульт (окно с кнопками и журналом) и текстовое меню;
  * оформительный минимум: pyproject.toml, console-скрипт pcmcia-kit,
    проверка стиля ruff; старые сценарии - в windows/legacy/ (не используются);
  * тесты ускорены: около 32 с полный прогон и около 17 с быстрый
    (tests/test_scripts.py --fast);
  * сборка выпуска одним действием: python3 tests/release.py --build
    (проверяет версии и состав, собирает ZIP и tar.gz, считает суммы).

Проверки (перед сборкой):
{counts}
Сверка:
  md5sum pcmcia-ssh-kit.zip        (Windows: certutil -hashfile <файл> MD5)
  md5sum pcmcia-flash-kit.tar.gz

Важно: суммы верны только для ЭТИХ файлов. Если архив пересобран — они другие.
"""
    open(MD5_PATH, "w", encoding="utf-8").write(text)
    print(f"\nZIP  {zip_md5}  ({entries} записей)")
    print(f"TAR  {tar_md5}")
    print(f"MD5_АРХИВА.txt обновлён: {MD5_PATH}")
    print(f"MD5SUMS.txt обновлён: {full_count} файлов в полные-файлы/")
    if problems:
        print("\nзамечания по архиву:")
        for p in problems:
            print("  - " + p)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
