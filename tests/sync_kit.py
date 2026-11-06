#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Синхронизация: pcmcia-flash-kit -> one-archive/pcmcia-ssh-kit (+ for-windows).

    python3 tests/sync_kit.py

Копирует по md5 только изменённые файлы: набор, windows-вариант, tests/,
linux/ и вспомогательные скрипты, затем обновляет полные-файлы/.
Без tests/ и linux/ архив будет неполным: README обещает 843 проверки,
а прогнать их в распакованном архиве было бы нечем (так и случилось).
"""
import hashlib
import os
import shutil

KIT = "/home/user/pcmcia-flash-kit"
DST = "/home/user/one-archive/pcmcia-ssh-kit"
FULL = "/home/user/полные-файлы"
SKIP_FULL = {"tests", "linux", "for-windows"}


def md5(path):
    return hashlib.md5(open(path, "rb").read()).hexdigest()


def copy_tree(src_dir, dst_dir, changed, prefix=""):
    os.makedirs(dst_dir, exist_ok=True)
    for name in sorted(os.listdir(src_dir)):
        src = os.path.join(src_dir, name)
        dst = os.path.join(dst_dir, name)
        if not os.path.isfile(src) or name == "__pycache__":
            continue
        if not os.path.exists(dst) or md5(src) != md5(dst):
            shutil.copy2(src, dst)
            changed.append(prefix + name)


changed = []
for name in sorted(os.listdir(KIT)):
    src = os.path.join(KIT, name)
    dst = os.path.join(DST, name)
    if name.startswith(".") or name == "__pycache__":
        continue
    if os.path.isfile(src) and (not os.path.exists(dst) or md5(src) != md5(dst)):
        shutil.copy2(src, dst)
        changed.append(name)

for win_dir in (os.path.join(DST, "for-windows"),):
    os.makedirs(win_dir, exist_ok=True)
    # for-windows - то же содержимое, что и корень набора: берём список из него
    for name in sorted(set(os.listdir(win_dir)) | set(os.listdir(DST))):
        # скрытые файлы (.card-state.txt) и кэш Python не переносим: это не
        # часть набора, а следы работы на конкретной машине
        if name.startswith(".") or name == "__pycache__":
            continue
        src = os.path.join(KIT, name)
        dst = os.path.join(win_dir, name)
        if not os.path.isfile(src):
            continue
        if os.path.isfile(src) and (not os.path.exists(dst) or md5(src) != md5(dst)):
            shutil.copy2(src, dst)
            changed.append("fw/" + name)

copy_tree(os.path.join(KIT, "tests"), os.path.join(DST, "tests"), changed, "tests/")
copy_tree(os.path.join(KIT, "linux"), os.path.join(DST, "linux"), changed, "linux/")

# полные-файлы/ - плоское зеркало того, что уезжает пользователю
os.makedirs(FULL, exist_ok=True)
for name in sorted(os.listdir(DST)):
    if name in SKIP_FULL:
        continue
    src = os.path.join(DST, name)
    dst = os.path.join(FULL, name)
    if os.path.isfile(src) and (not os.path.exists(dst) or md5(src) != md5(dst)):
        shutil.copy2(src, dst)
        changed.append("полные/" + name)

print("обновлено:", len(changed))
