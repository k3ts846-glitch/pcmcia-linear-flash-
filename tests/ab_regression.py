#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A/B-проверка: одни и те же 7 сценариев на двух версиях файла.

    python3 tests/ab_regression.py pcmcia_gui.py             # текущая версия
    python3 tests/ab_regression.py /путь/к/старой/версии.py   # для сравнения

Смысл: доказать, что тесты ловят именно те ошибки, из-за которых написаны.
Сценарии: усечённое чтение с rc=0, сверка пустого файла, мусор после сбоя,
место под двойное чтение, дедлок на большом выводе утилиты, обрывок файла
при записи диапазона, недоступный /var/lock.
"""
import os, sys, tempfile, shutil, time, importlib.util

MOD = sys.argv[1]
spec = importlib.util.spec_from_file_location("guimod", MOD)
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
g.os.geteuid = lambda: 0

STUB = tempfile.mkdtemp(prefix="ab-")
CARD = os.path.join(STUB, "card.bin")
SIZE, BLOCK = 4*1024*1024, 65536
open(CARD,'wb').write(b'\x11'*SIZE)
os.environ["PATH"] = STUB + os.pathsep + os.environ["PATH"]
g.TMP = STUB

def tool(name, body):
    p=os.path.join(STUB,name); open(p,'w').write("#!/bin/bash\n"+body+"\n"); os.chmod(p,0o755)

def normal_tools():
    tool("mtd_debug", f'''
case "$1" in
 info) echo "mtd.size = {SIZE}"; echo "mtd.erasesize = {BLOCK}"; echo "mtd.type = MTD_NORFLASH" ;;
 read) python3 -c "
import sys;off,n=int(sys.argv[1]),int(sys.argv[2])
open(sys.argv[3],'wb').write(open('{CARD}','rb').read()[off:off+n])" "$3" "$4" "$5" ;;
esac''')
    tool("flashcp","exit 0"); tool("flash_erase","exit 0")

def short_tools():
    tool("mtd_debug", f'''
case "$1" in
 info) echo "mtd.size = {SIZE}"; echo "mtd.erasesize = {BLOCK}"; echo "mtd.type = MTD_NORFLASH" ;;
 read) python3 -c "
import sys;off,n=int(sys.argv[1]),int(sys.argv[2])
d=open('{CARD}','rb').read()[off:off+n]
open(sys.argv[3],'wb').write(d[:max(0,n//2)])" "$3" "$4" "$5" ;;
esac''')

res=[]
def case(name, fn):
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
    res.append((name, ok, detail))

def card():
    return g.Card(CARD)

# 1 усечённое чтение
def c1():
    short_tools()
    out=os.path.join(STUB,"t1.bin")
    try:
        card()  # noqa
        r = g.Ops(card(), g.Runner()).read_image(out, double_check=False)
        return (os.path.getsize(out)==SIZE, f"файл {os.path.getsize(out)} из {SIZE}, «{r['verdict'][:30]}»")
    except Exception as e:
        return (True, f"отказ: {str(e)[:50]}")
case("усечённое чтение замечено", c1)

# 2 пустой файл при сверке
def c2():
    normal_tools()
    e=os.path.join(STUB,"empty.bin"); open(e,'wb').close()
    try:
        r = g.Ops(card(), g.Runner()).verify(e)
        return (not r["verified"], f"вернула verified={r['verified']}")
    except Exception as ex:
        return (True, f"отказ: {str(ex)[:40]}")
case("сверка пустого файла отвергнута", c2)

# 3 мусор после сбоя
def c3():
    tool("mtd_debug", f'''
case "$1" in
 info) echo "mtd.size = {SIZE}"; echo "mtd.erasesize = {BLOCK}" ;;
 read) python3 -c "
import sys
if int(sys.argv[1])>0: sys.exit(1)
open(sys.argv[3],'wb').write(b'\\x11'*int(sys.argv[2]))" "$3" "$4" "$5" ;;
esac''')
    out=os.path.join(STUB,"t3.bin")
    try:
        g.Ops(card(), g.Runner()).read_image(out, double_check=False)
    except Exception:
        pass
    left=[f for f in os.listdir(STUB) if f.startswith("t3.bin")]
    return (not left, f"осталось: {left}")
case("нет мусора после сбоя", c3)

# 4 место под двойное чтение
def c4():
    normal_tools()
    real=g.dir_free; g.dir_free=lambda p: SIZE
    try:
        try:
            g.Ops(card(), g.Runner()).read_image(os.path.join(STUB,"t4.bin"), double_check=True)
            return (False, "прошло при свободном месте = 1 образ")
        except Exception as ex:
            return (True, f"отказ: {str(ex)[:50]}")
    finally:
        g.dir_free=real
case("место считается под двойное чтение", c4)

# 5 дедлок на большом выводе
def c5():
    p=os.path.join(STUB,"noisy")
    open(p,'w').write("#!/bin/bash\npython3 -c \"import sys;sys.stdout.write('x'*700000)\"\n")
    os.chmod(p,0o755)
    t0=time.time(); rc,out,err=g.Runner().exec([p], timeout=12); dt=time.time()-t0
    return (rc==0 and len(out)==700000, f"rc={rc}, {len(out)} байт, {dt:.1f} c")
case("большой вывод не вешает программу", c5)

# 6 write_range с обрывком
def c6():
    normal_tools()
    s=os.path.join(STUB,"s.bin"); open(s,'wb').write(b"\x00"*16)
    try:
        g.Ops(card(), g.Runner()).write_range(0, 1000, s)
        return (False, "записала 16 байт вместо 1000")
    except Exception as ex:
        return (True, f"отказ: {str(ex)[:45]}")
case("обрывок файла не уходит на карту", c6)

# 7 блокировка: недоступный путь
def c7():
    g.LOCK_PATH="/proc/self/нет/lock"
    try:
        r = g.acquire_single_instance()
        if isinstance(r, tuple):
            f, why = r
            if f: f.close()
            return ("запущен" not in why, f"{why[:45]}")
        return (r is not None, "вернула файл" if r else "None = «уже запущено»")
    finally:
        g.LOCK_PATH=os.path.join(STUB,"l.lock")
case("недоступный /var/lock не выдаётся за «уже запущено»", c7)

print(f"\n=== {os.path.basename(MOD)} ===")
fails=0
for n, ok, d in res:
    print(f"  {'OK  ' if ok else 'БАГ '} {n:52} {d}")
    if not ok: fails+=1
print(f"  итог: дефектов {fails} из {len(res)}")
shutil.rmtree(STUB, ignore_errors=True)
