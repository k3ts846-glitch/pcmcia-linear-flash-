#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Операции с образом линейной флеш-карты: проба, запись, чтение, журнал.

Вынесен из pcmcia_direct.py. Зависит от nor_flash (классы NorFlash),
chips (план записи), mem (Mem, окна), cis (парсинг CIS).
"""

import hashlib
import os
import time

from .chips import flash_plan
from .errors import DirectError
from .mem import (
    PAGE, PROGRESS_EVERY, STALL_TIMEOUT, WIN_CHUNK,
    Mem, mem_for_addr, read_mem, restore_cis_window,
    setup_cis_window, map_card_window,
    _NOTICE, say_once,
)
from .nor_flash import NorFlash

SIZE_STEPS = (512 << 10, 1 << 20, 2 << 20, 4 << 20, 8 << 20, 16 << 20, 32 << 20)
PROBE_LEN = 4096
TRUST_CIS_MAX = 64 << 20


# --- чтение карты -------------------------------------------------------------

def _read_page(mem, exca, win, card_off, length=PROBE_LEN, dev_id=None,
               verbose=False):
    """Читает length байт common memory начиная со смещения card_off."""
    regs = mem_for_addr(mem, exca, writable=True, dev_id=dev_id,
                        verbose=verbose)
    old = map_card_window(regs, exca, win, card_offset=card_off,
                          attrib=False, size=length)
    try:
        page_off = card_off & (PAGE - 1)
        data = mem_for_addr(mem, win["start"], writable=False, dev_id=dev_id,
                            verbose=verbose)
        return read_mem(data, win["start"] + page_off, length,
                        dev_id=dev_id, verbose=verbose)
    finally:
        restore_cis_window(regs, exca, old)


def _read_card(mem, exca, win, off, length, dev_id=None, tries=3):
    """Читает участок карты с проверкой стабильности (правило двух из трёх)."""
    first = _read_page(mem, exca, win, off, length, dev_id=dev_id)
    second = (_read_page(mem, exca, win, off, length, dev_id=dev_id)
              if tries > 1 else first)
    if second == first:
        return first
    if tries > 2:
        third = _read_page(mem, exca, win, off, length, dev_id=dev_id)
        if third == first:
            return first
        if third == second:
            return second
    raise DirectError(
        f"карта отвечает НЕСТАБИЛЬНО: участок со смещения {off} "
        f"({length} байт) читается по-разному за {tries} попытки. "
        "Обычные причины: контакты карты или слота, питание. "
        "Образ не сохранён, на карту ничего не записано - повторите "
        "чтение после чистки контактов или с меньшим заходом "
        "(--span 4096).")


# --- размер карты по паспорту --------------------------------------------------

def cis_card_size(info):
    """Ёмкость карты по паспорту: (байт, откуда) или (None, почему нет)."""
    devs = (info or {}).get("devices") or []
    total = sum(d.get("size") or 0 for d in devs)
    if total and total <= TRUST_CIS_MAX:
        return total, f"по кортежу CISTPL_DEVICE ({len(devs)} устройств)"
    mbits = (info or {}).get("mbits")
    if mbits:
        return mbits * 1024 * 1024 // 8, f'по паспорту "{mbits} Мбит"'
    return None, "в паспорте нет ёмкости"


def card_geometry(chips_hint=0):
    """(сколько чипов, сколько байт всего) по паспорту карты."""
    chips, size = (chips_hint or 2), 0
    try:
        from .cis import read_cis
        _raw, _tuples, info = read_cis(mem=None, verbose=False)
    except DirectError:
        return chips, size
    devs = (info or {}).get("devices") or []
    total = sum((d.get("size") or 0) for d in devs)
    if devs:
        chips = max(len(devs), 1)
    if total:
        size = total
    return chips, size


# --- тип карты -----------------------------------------------------------------

CIS_IN_COMMON = (0x200, 0x400)
CIS_LOOK = 512


def cis_in_common_mem(mem, exca, win, dev_id=None):
    """Ищет цепочку CIS в common memory (так устроены ATA-карты)."""
    from .cis import scan_cis, cis_summary
    for off in CIS_IN_COMMON:
        try:
            raw = _read_page(mem, exca, win, off, CIS_LOOK, dev_id=dev_id)
        except DirectError:
            continue
        from .mem import looks_blank
        if looks_blank(raw, 0xFF) or looks_blank(raw, 0x00):
            continue
        tuples, _how = scan_cis(raw)
        if not tuples:
            continue
        info = cis_summary(tuples)
        if info.get("manfid") or info.get("product"):
            return off, tuples, info
    return None, [], {}


def card_kind(info, tuples, at_off=None):
    """Что это за карта: (вид, что с ней можно делать)."""
    func = (info or {}).get("func")
    jedec = (info or {}).get("jedec")
    types = {(d or {}).get("type_name", "").upper()
             for d in (info or {}).get("devices") or []}
    if at_off is not None:
        return "ata", ("паспорт лежит в common memory (смещение "
                       f"{at_off:#x}) - так устроены ATA-карты: "
                       "CompactFlash в переходнике, Microdrive, диски; "
                       "образ с такой карты - это дамп всего диска")
    if func == 1 and ("SRAM" in types or "DRAM" in types):
        return "sram", ("SRAM/DRAM с батареей: образ снимается, но данные "
                        "держатся только пока жива батарея; стирания "
                        "секторами нет, запись - обычная, без команд")
    if func == 1 and ("ROM" in types or "EPROM" in types or "OTPROM" in types):
        return "rom", ("ROM/EPROM/OTPROM: только чтение, записывать нечего")
    if func == 1 and ("FLASH" in types or jedec):
        return "linear-flash", "линейная флеш: образ снимается, запись - по Vpp"
    if func == 1:
        return "memory", "карта памяти: образ снимается как есть"
    if func in (2,):
        return "serial", ("последовательный порт: это НЕ носитель, образ не "
                          "имеет смысла (только регистры)")
    if func in (3,):
        return "parallel", "параллельный порт: не носитель, образ не нужен"
    if func in (6,):
        return "network", ("сетевая карта: не носитель, образ не нужен "
                           "(можно читать CIS и регистры)")
    return "unknown", "вид карты по паспорту не определён"


# --- диагностика окна ----------------------------------------------------------

def window_access_diag(dev, exca, win):
    """Почему attribute memory читается нулями: разбор ДО выводов о карте."""
    out = []
    suspect = False
    try:
        aw_reg = mem_for_addr(dev, exca, writable=True, dev_id=dev)
    except DirectError as e:
        return [f"регистры ExCA недоступны: {e}"], True
    try:
        aw = aw_reg.read(exca + 0x06, 1)[0]
        page = aw_reg.read(exca + 0x40, 1)[0]
        start = int.from_bytes(aw_reg.read(exca + 0x10, 2), "little")
        stop = int.from_bytes(aw_reg.read(exca + 0x12, 2), "little")
        off = int.from_bytes(aw_reg.read(exca + 0x14, 2), "little")
    except DirectError as e:
        return [f"регистры ExCA не читаются: {e}"], True
    out.append(f"  ExCA: ADDRWIN={aw:#04x} (окно 0 {'включено' if aw & 1 else 'ВЫКЛЮЧЕНО'}), "
               f"PAGE0={page:#04x}, START={start:#06x}, STOP={stop:#06x}, OFF={off:#06x}")
    if not (aw & 1):
        out.append("  -> до чтения окно 0 было ВЫКЛЮЧЕНО: значит его переставил")
        out.append("     драйвер моста (yenta_socket) или оно не настраивалось.")
        suspect = True
    return out, suspect


def card_window_check(mem, exca, win, dev_id=None):
    """Здоровье окна: (ok, текст)."""
    try:
        a = _read_page(mem, exca, win, 0, PROBE_LEN, dev_id=dev_id)
        b = _read_page(mem, exca, win, 0, PROBE_LEN, dev_id=dev_id)
    except DirectError as e:
        return False, f"окно не читается: {e}"
    if a != b:
        return False, ("окно читается НЕСТАБИЛЬНО: два чтения одного и того "
                       "же места различаются - контакты или питание")
    if a == b"\x00" * len(a):
        return True, ("в окне сплошные нули: шина прижата, карта не отвечает "
                      "(пустой слот или карта не контачит)")
    if a == b"\xff" * len(a):
        return True, "в окне сплошные 0xFF: карта не отвечает (пустой слот?)"
    return True, "окно читается, содержимое неоднородное"


def find_card_size(mem, exca, win, steps=SIZE_STEPS, probe=PROBE_LEN,
                   dev_id=None, verbose=False, expect=None):
    """Размер карты: (размер, как определено)."""
    from .mem import looks_blank

    head = _read_page(mem, exca, win, 0, PROBE_LEN, dev_id=dev_id)
    blank_head = looks_blank(head, 0xFF) or looks_blank(head, 0x00)
    for period in steps:
        if period < probe:
            continue
        if period >= win["size"]:
            break
        try:
            a = _read_page(mem, exca, win, period, probe, dev_id=dev_id)
            if blank_head and period != (expect or -1):
                continue
            if a != head:
                continue
            b = _read_page(mem, exca, win, period, probe, dev_id=dev_id)
            if b == head:
                return period, (f"адрес повторяется через {period / 1024 / 1024:.1f} МиБ")
        except DirectError:
            continue

    if expect:
        try:
            tail = _read_page(mem, exca, win, expect - PROBE_LEN, PROBE_LEN,
                              dev_id=dev_id)
        except DirectError:
            tail = None
        if tail is not None and tail == head and not blank_head:
            return expect, ("по паспорту; внутри карты содержимое повторяется, "
                            "то есть карта СВОРАЧИВАЕТ адреса")
        note = ""
        if tail is not None:
            if looks_blank(tail, 0x00):
                note = ("; за концом карты читаются нули - линии адреса не "
                        "подключены, повторения не будет")
            elif looks_blank(tail, 0xFF):
                note = "; за концом карты 0xFF (пустая шина)"
            else:
                note = "; за концом карты другие данные"
        return expect, f"по паспорту ({expect / 1024 / 1024:.0f} МиБ)" + note

    if not blank_head:
        prev = head
        for period in steps:
            if period <= probe or period >= win["size"]:
                continue
            try:
                pg = _read_page(mem, exca, win, period, PROBE_LEN, dev_id=dev_id)
            except DirectError:
                break
            if ((looks_blank(pg, 0x00) or looks_blank(pg, 0xFF)) and
                    not looks_blank(prev, 0x00)):
                return period, (f"по первому пустому участку ({period / 1024 / 1024:.0f} МиБ): "
                                "дальше за картой ничего нет")
            prev = pg

    return win["size"], (f"повторения не нашли и ёмкости в паспорте нет, "
                         f"беру окно моста {win['size']:#x} - это МОЖЕТ БЫТЬ "
                         f"больше карты")


# --- чтение образа -------------------------------------------------------------

def read_card_image(mem, exca, win, path, size, double=False, progress=None,
                    dev_id=None, verbose=False, span=None):
    """Снимает образ карты: окно переставляется по карте, читается common memory."""
    tmp = path + ".part"
    from .mem import window_span
    step = window_span(win, span)

    last = [0]

    def one_pass(out_path, pass_no=1):
        h = hashlib.md5()
        done = 0
        last[0] = 0
        last_move = time.time()
        with open(out_path, "wb") as f:
            while done < size:
                n = min(step, size - done)
                if done == 0:
                    blk = _read_card(mem, exca, win, done, n, dev_id=dev_id)
                else:
                    blk = _read_page(mem, exca, win, done, n, dev_id=dev_id)
                f.write(blk)
                h.update(blk)
                done += n
                if time.time() - last_move > STALL_TIMEOUT:
                    raise DirectError(
                        f"чтение встало: {STALL_TIMEOUT} с без нового байта "
                        f"(остановились на {done} из {size}). Причины: карту "
                        "вынули, контакты, зависший мост. Образ не сохранён.")
                last_move = time.time()
                if progress and (done >= size or done - last[0] >= PROGRESS_EVERY):
                    last[0] = done
                    progress(done, size, pass_no)
            f.flush()
            os.fsync(f.fileno())
        return h.hexdigest()

    lock_path = path + ".lock"
    lock_fh = None
    try:
        import fcntl
        lock_fh = open(lock_path, "w")
        import fcntl as _fcntl
        _fcntl.flock(lock_fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except OSError as e:
        if lock_fh:
            lock_fh.close()
        raise DirectError(
            f"образ {path} уже снимается другим запуском ({lock_path} занят). "
            "Дождитесь его конца или возьмите другое имя файла.") from e
    except ImportError:
        lock_fh = None
    try:
        md5 = one_pass(tmp, 1)
        if double:
            chk = path + ".chk"
            try:
                md5b = one_pass(chk, 2)
                same = filecmp_equal(tmp, chk)
            finally:
                try:
                    os.remove(chk)
                except OSError:
                    pass
            if not same or md5 != md5b:
                raise DirectError(
                    "два чтения РАЗЛИЧАЮТСЯ — карта или контакты нестабильны, "
                    f"образ не сохранён (md5 {md5} и {md5b})")
        os.replace(tmp, path)
    except BaseException:
        for junk in (tmp, path + ".chk"):
            try:
                os.remove(junk)
            except OSError:
                pass
        raise
    finally:
        if lock_fh:
            try:
                import fcntl as _fcntl2
                _fcntl2.flock(lock_fh.fileno(), _fcntl2.LOCK_UN)
            except OSError:
                pass
            lock_fh.close()
            try:
                os.remove(lock_path)
            except OSError:
                pass
    return {"path": path, "size": size, "md5": md5,
            "sha256": file_sha256(path)}


# --- работа с образом (файл) --------------------------------------------------

def image_regions(path, page=4096):
    """Карта занятости: одинаковые соседние страницы объединяются в участки."""
    out = []
    prev = None
    size = os.path.getsize(path)
    off = 0

    def kind_of(blk):
        if not blk:
            return "пусто"
        ff = blk.count(0xFF)
        z = blk.count(0x00)
        if ff == len(blk):
            return "0xFF (стёрто)"
        if z == len(blk):
            return "0x00 (нули)"
        if z > len(blk) * 0.9:
            return "почти нули"
        if ff > len(blk) * 0.9:
            return "почти 0xFF"
        return "данные"

    with open(path, "rb") as f:
        while off < size:
            blk = f.read(page)
            if not blk:
                break
            k = kind_of(blk)
            if prev and prev[0] == k:
                prev[1] += len(blk)
            else:
                prev = [k, len(blk), off]
                out.append(prev)
            off += len(blk)
    return out


def image_stats(path):
    """Короткая сводка по образу: сколько 0xFF, 0x00, прочего."""
    ff = z = n = 0
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            n += len(blk)
            ff += blk.count(b"\xff")
            z += blk.count(b"\x00")
    n = n or 1
    return {"bytes": n, "ff": ff, "zero": z, "other": n - ff - z,
            "ff_pct": 100.0 * ff / n, "zero_pct": 100.0 * z / n,
            "other_pct": 100.0 * (n - ff - z) / n}


def file_sha256(path, chunk=1 << 20):
    """sha256 файла."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


def trim_image(path, size_wanted, keep_original=True):
    """Обрезает образ до size_wanted байт."""
    size = os.path.getsize(path)
    if size <= size_wanted:
        return path, size
    if path.lower().endswith(".bin"):
        out = path[:-4] + f"-{size_wanted}.bin"
    else:
        out = path + ".trim"
    with open(path, "rb") as src, open(out, "wb") as dst:
        left = size_wanted
        while left > 0:
            blk = src.read(min(1 << 20, left))
            if not blk:
                break
            dst.write(blk)
            left -= len(blk)
    if not keep_original:
        try:
            os.remove(path)
        except OSError:
            pass
    return out, os.path.getsize(out)


def trim_hint(path, size_wanted):
    """Что делать, если образ больше карты."""
    size = os.path.getsize(path)
    if size <= size_wanted:
        return []
    return [f"  ВНИМАНИЕ: образ {size} байт, а карта по паспорту {size_wanted} байт.",
            f"  Первые {size_wanted} байт - это карта, остальное - пустая шина.",
            f"  Обрезать: head -c {size_wanted} {os.path.basename(path)} > card-карта.bin",
            "  Чище переснять: в наборе 2.9.0+ размер берётся из паспорта."]


def image_report(res, stats, win, how):
    """Строки отчёта о снятом образе."""
    out = ["", "Образ карты:"]
    from .cis import _window_text
    out.append(_window_text(win))
    out.append(f"  файл   : {res['path']}")
    out.append(f"  размер : {res['size']} байт "
               f"({res['size'] / 1024 / 1024:.1f} МиБ) — {how}")
    out.append(f"  md5    : {res['md5']}")
    if res.get("sha256"):
        out.append(f"  sha256 : {res['sha256']}")
    out.append(f"  состав : 0xFF {stats['ff_pct']:.1f}%, "
               f"0x00 {stats['zero_pct']:.1f}%, "
               f"данные {stats['other_pct']:.1f}%")
    if stats["other_pct"] < 0.1:
        out.append("  ВНИМАНИЕ: в образе почти нет данных (всё 0xFF/0x00).")
        out.append("    Так выглядит стёртая карта ИЛИ чтение не туда: проверьте,")
        out.append("    что в разделе 7 паспорт читается (карта отвечает).")
    try:
        parts = image_regions(res["path"])
    except OSError:
        parts = []
    if parts:
        out.append("  где что лежит (участки одинаковых страниц склеены):")
        for k, n, off in parts[:12]:
            if n >= 4096:
                out.append(f"    {off:#09x}  {k:<16} {n / 1024:8.1f} КиБ")
        if len(parts) > 12:
            out.append(f"    ... всего участков: {len(parts)}")
    return out


def normalize_bankwidth(data):
    """8-битная карта на 16-битной шине даёт каждый байт дважды."""
    if len(data) < 8:
        return data, 2
    even = data[0::2]
    odd = data[1::2]
    if even[:4096] == odd[:4096] and len(set(even[:1024])) > 1:
        return even, 1
    return data, 2


def looks_blank(data, byte):
    """Всё ли содержимое равно byte."""
    return bool(data) and data.count(byte) == len(data)


def find_cis(data):
    """Ищет маркер цепочки CIS."""
    out = []
    pos = 0
    from .cis import CIS_TAG
    while True:
        i = data.find(CIS_TAG, pos)
        if i < 0:
            break
        out.append(i)
        pos = i + 1
        if len(out) >= 8:
            break
    return out


def strings(data, minlen=4, limit=16):
    """Печатаемые строки — по ним видно PRODID/MANFID карты."""
    res, cur = [], bytearray()
    for b in data:
        if 32 <= b < 127:
            cur.append(b)
        else:
            if len(cur) >= minlen:
                res.append(cur.decode("ascii"))
                if len(res) >= limit:
                    return res
            cur = bytearray()
    if len(cur) >= minlen:
        res.append(cur.decode("ascii"))
    return res


def summarize(data):
    """Короткая сводка содержимого окна."""
    n = len(data)
    kinds = set(data)
    res = {"bytes": n,
           "unique_values": len(kinds),
           "empty_ff": looks_blank(data, 0xFF),
           "empty_00": looks_blank(data, 0x00),
           "cis_marks": find_cis(data),
           "strings": strings(data)}
    if res["empty_ff"]:
        res["verdict"] = "всё 0xFF — карта стёрта (или это не карта, а пустое окно)"
    elif res["empty_00"]:
        res["verdict"] = "всё 0x00 — типично для неотвечающей шины или SRAM без питания"
    elif len(kinds) < 8:
        res["verdict"] = f"подозрительно однородное содержимое ({len(kinds)} разных байт)"
    else:
        res["verdict"] = "содержимое неоднородное, есть данные"
    return res


# --- журнал записи ------------------------------------------------------------

def journal_path(remote_dir):
    """Путь к файлу журнала записи на ноутбуке."""
    return os.path.join(remote_dir, ".journal")


def journal_append(path, entry):
    """Дозаписать одну строку JSON в файл журнала."""
    import json as _json
    line = _json.dumps(entry, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def journal_read(path):
    """Прочитать все записи из файла журнала."""
    import json as _json
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(_json.loads(line))
            except _json.JSONDecodeError:
                continue
    return out


def journal_completed_sectors(path):
    """Множество номеров секторов, которые уже успешно записаны."""
    done = set()
    for entry in journal_read(path):
        if entry.get("stage") == "sector_done":
            done.add(entry["sector"])
    return done


def writes_for_bypass(plan):
    """Есть ли в плане хотя бы один сектор с записями."""
    return any(bool(writes) for _, _, writes in plan)


# --- запись образа ------------------------------------------------------------

def _span_size(flash, offset):
    """Размер сектора карты, в который попадает адрес (для boot-карт - разный)."""
    span = getattr(flash, "span", None)
    if callable(span):
        return span(offset)[1]
    return flash.sector


def flash_probe(flash, offset=0, payload=None, progress=None):
    """ПРОБА: стереть и записать ОДИН сектор, прочитать обратно, сравнить."""
    offset, sector = (flash.span(offset) if callable(getattr(flash, "span", None))
                      else (offset - (offset % flash.sector), flash.sector))
    if payload is None:
        pattern = bytearray([0x00, 0xFF, 0x55, 0xAA] * 64)
        pattern += bytes(range(256)) * 2
        payload = bytes(pattern)
    span = min(len(payload), sector, 2048)
    payload = payload[:span]
    if progress:
        progress("стираю сектор")
    ok, why = flash.erase_sector(offset)
    if not ok:
        return False, f"пробное стирание не удалось: {why}"
    if progress:
        progress("проверяю, что стёрлось 0xFF")
    empty = flash.read(offset, len(payload))
    if any(b != 0xFF for b in empty):
        bad = next(i for i, b in enumerate(empty) if b != 0xFF)
        return False, (f"после стирания байт {offset + bad} читается "
                       f"{empty[bad]:#04x}, а должен быть 0xFF: сектор не "
                       "стирается (Vpp, защита или износ)")
    written = 0
    for i, b in enumerate(payload):
        if b == 0xFF:
            continue
        ok, why = flash.program_byte(offset + i, b)
        if not ok:
            return False, (f"запись байта {offset + i}="
                           f"{b:#04x} не удалась: {why}")
        written += 1
    if progress:
        progress("читаю обратно")
    got = flash.read(offset, len(payload))
    if got != payload:
        bad = [i for i in range(len(payload)) if got[i] != payload[i]]
        i = bad[0]
        flash.reset()
        return False, (f"прочитано {got[i]:#04x} вместо {payload[i]:#04x} в байте "
                       f"{offset + i} (различий {len(bad)}): карта не пишет так, "
                       "как ожидалось")
    flash.reset()
    return True, f"стёрт и записан 1 сектор ({written} байт), прочитанное совпало"


def flash_probe_safe(flash, offset=0, backup_dir=None, progress=None):
    """Проба записи, которая не теряет данные."""
    import os as _os
    if callable(getattr(flash, "span", None)):
        base, sector = flash.span(offset)
    else:
        sector = flash.sector
        base = offset - (offset % sector)
    if progress:
        progress("читаю сектор, чтобы его не потерять")
    original = flash.read(base, sector)
    path = None
    if backup_dir is not None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = _os.path.join(backup_dir, f"probe-sector-{base:#08x}-{stamp}.bin")
        try:
            _os.makedirs(backup_dir, exist_ok=True)
            with open(path, "wb") as f:
                f.write(original)
        except OSError:
            path = None
    ok, why = flash_probe(flash, base, progress=progress)
    if not ok:
        return {"ok": False, "why": why, "backup": path, "restored": False}
    if progress:
        progress("возвращаю исходные байты сектора")
    flash.erase_sector(base)
    bad = 0
    why = ""
    for i, b in enumerate(original):
        if b == 0xFF:
            continue
        ok2, why2 = flash.program_byte(base + i, b)
        if not ok2:
            bad += 1
            if bad == 1:
                why = f"не удалось вернуть байт {base + i:#x}: {why2}"
    if bad:
        return {"ok": False, "why": why, "backup": path, "restored": False,
                "not_restored_bytes": bad}
    got = flash.read(base, sector)
    if got != original:
        diff = sum(1 for i in range(sector) if got[i] != original[i])
        return {"ok": False, "restored": False, "backup": path,
                "why": f"после возврата сектор отличается в {diff} байтах"}
    flash.reset()
    return {"ok": True, "why": "проба прошла, сектор возвращён байт в байт",
            "backup": path, "restored": True}


def flash_write_image(flash, image, progress=None, verify=True,
                      erase_first=False, journal=None, resume=False,
                      remote_dir=None, backup_dir=None):
    """Записать образ на карту. Возвращает dict с честным итогом."""
    # План строится по КАРТЕ СЕКТОРОВ карты: у boot-микросхем секторы разного
    # размера, и «шаг 64 КиБ» стёр бы не то. Для однородных карт spans даёт
    # ровно то же, что и раньше.
    spans = getattr(flash, "spans", None)
    plan = flash_plan(image, flash.sector,
                      spans=spans(len(image)) if callable(spans) else None)
    total = len(plan)
    res = {"sectors": total, "erased": 0, "bytes": 0, "ok": False,
           "failed_at": None, "why": "", "backups": []}

    if journal is None and remote_dir:
        journal = journal_path(remote_dir)

    if backup_dir:
        try:
            os.makedirs(backup_dir, exist_ok=True)
        except OSError as error:
            return {"ok": False,
                    "why": f"не удалось создать папку бэкапа {backup_dir}: {error}",
                    "backups": []}

    completed = set()
    if resume and journal:
        completed = journal_completed_sectors(journal)

    bypass_active = bool(getattr(flash, "bypass", False)) and bool(
        any(writes for _, _, writes in plan))
    for no, (off, need_erase, writes) in enumerate(plan, 1):
        if off in completed:
            progress and progress(f"сектор {no} из {total}: {off:#x} (уже записан)",
                                  (no - 1) / max(total, 1))
            res["erased"] += 1 if need_erase else 0
            res["bytes"] += len(writes)
            continue

        if progress:
            progress(f"сектор {no} из {total}: {off:#x}",
                     (no - 1) / max(total, 1))
        span_size = _span_size(flash, off)
        if backup_dir and need_erase:
            backup_path = os.path.join(backup_dir, f"sector-{off:08x}.bin")
            try:
                sector_data = flash.read(off, span_size)
                with open(backup_path, "wb") as bf:
                    bf.write(sector_data)
                res["backups"].append(backup_path)
            except (DirectError, OSError) as error:
                res["failed_at"] = off
                res["why"] = (f"не удалось сделать бэкап сектора "
                              f"{off:#x}: {error}")
                return res
        if need_erase:
            ok, why = flash.erase_sector(off)
            if not ok:
                res["failed_at"] = off
                res["why"] = f"стирание сектора {off:#x}: {why}"
                return res
            res["erased"] += 1
            blank = flash.read(off, span_size)
            bad = [i for i, b in enumerate(blank) if b != 0xFF]
            if bad:
                res["failed_at"] = off + bad[0]
                res["why"] = (f"сектор {off:#x} не стёрся полностью: "
                              f"первый не-0xFF байт по адресу "
                              f"{off + bad[0]:#x} "
                              f"(читается {blank[bad[0]]:#04x}). "
                              f"Сектор изношен или защищён.")
                return res
        elif erase_first:
            ok, why = flash.erase_sector(off)
            if not ok:
                res["failed_at"] = off
                res["why"] = f"стирание сектора {off:#x}: {why}"
                return res
            res["erased"] += 1
            blank = flash.read(off, span_size)
            bad = [i for i, b in enumerate(blank) if b != 0xFF]
            if bad:
                res["failed_at"] = off + bad[0]
                res["why"] = (f"сектор {off:#x} не стёрся полностью: "
                              f"первый не-0xFF байт по адресу "
                              f"{off + bad[0]:#x} "
                              f"(читается {blank[bad[0]]:#04x}). "
                              f"Сектор изношен или защищён.")
                return res
        step = max(int(getattr(flash, "page_card", 1) or 1), 1)
        i = 0
        if bypass_active and writes:
            flash.enter_bypass()
        try:
            while i < len(writes):
                a = writes[i]
                if step == 1:
                    chunk = [a]
                else:
                    page_start = a - (a % step)
                    chunk = []
                    while i < len(writes) and writes[i] < page_start + step \
                            and len(chunk) < step:
                        chunk.append(writes[i])
                        i += 1
                    ok, why = flash.program_page(page_start, bytes(
                        image[k] if k in chunk else 0xFF
                        for k in range(page_start, page_start + step)))
                    if not ok:
                        res["failed_at"] = chunk[0]
                        try:
                            back = flash.read(page_start, step)
                            diff = [k for k in range(step)
                                    if back[k] != image[page_start + k]]
                            if diff:
                                res["failed_at"] = page_start + diff[0]
                                res["fail_span"] = (page_start + diff[0],
                                                   page_start + diff[-1])
                                res["fail_count"] = len(diff)
                        except (DirectError, OSError):
                            pass
                        res["why"] = (f"запись страницы {page_start:#x} "
                                      f"(байт {res['failed_at']:#x}): {why}")
                        return res
                    res["bytes"] += len(chunk)
                    continue
                ok, why = flash.program_byte(chunk[0], image[chunk[0]])
                if not ok:
                    res["failed_at"] = chunk[0]
                    res["why"] = f"запись байта {chunk[0]:#x}: {why}"
                    return res
                res["bytes"] += 1
                i += 1
        finally:
            if bypass_active and writes:
                flash.exit_bypass()
        if journal:
            journal_append(journal, {
                "stage": "sector_done",
                "sector": off,
                "no": no,
                "total": total,
                "bytes": res["bytes"],
                "erased": res["erased"],
            })
    if verify:
        if progress:
            progress("сверка: читаю карту обратно", 0.98)
        flash.reset()
        got = flash.read(0, len(image))
        if got != image:
            diff = [i for i in range(len(image)) if got[i] != image[i]]
            res["failed_at"] = diff[0]
            res["why"] = (f"после записи не совпало {len(diff)} байт, первый - "
                          f"{diff[0]:#x} ({got[diff[0]]:#04x} вместо "
                          f"{image[diff[0]]:#04x})")
            res["fail_count"] = len(diff)
            if flash.protocol == "jedec":
                res["why"] += ("; при отказе записи DQ7 недостоверен, пока не "
                               "послан сброс, поэтому число байт может быть "
                               "завышено")
            return res
    res["ok"] = True
    return res


def flash_read_image(flash, size, progress=None):
    """Прочитать образ целиком тем же окном."""
    out = bytearray()
    step = max(flash.bus.span, PAGE)
    done = 0
    while done < size:
        n = min(step, size - done)
        out += flash.bus.rd_block(done, n)
        done += n
        if progress:
            progress(done, size)
    return bytes(out)


def filecmp_equal(a, b):
    import filecmp
    return filecmp.cmp(a, b, shallow=False)




def devmem_hint():
    """Одна строка: что мешает /dev/mem и что с этим делать."""
    try:
        with open("/proc/cmdline", encoding="utf-8", errors="replace") as f:
            cmdline = f.read()
    except OSError:
        cmdline = ""
    relaxed = "iomem=relaxed" in cmdline
    return ("в cmdline уже есть iomem=relaxed" if relaxed else
            "если mmap запрещён (Operation not permitted), добавьте в параметры "
            "ядра iomem=relaxed (в /etc/default/grub: GRUB_CMDLINE_LINUX, "
            "затем update-grub и перезагрузка)")


def probe(win_list, verbose=True):
    """Можно ли читать карту: /dev/mem (или sysfs) + первые байты каждого окна."""
    report = {"windows": win_list, "readable": None, "reason": "", "samples": [],
              "common_memory": True}
    try:
        m = Mem()
    except DirectError as e:
        report["reason"] = str(e)
        report["common_memory"] = False
        report["hint"] = devmem_hint()
        if verbose:
            print(f"НЕТ ДОСТУПА: {e}")
            print(f"Подсказка: {report['hint']}")
        return report
    exca = None
    dev = None
    try:
        from .cis import find_bridge_ids
        dev, bar0, _bv, _bd = find_bridge_ids()
        if bar0:
            exca = bar0 + 0x800
    except DirectError:
        dev = None
    report["common_memory"] = exca is not None
    try:
        ok_any = False
        for w in win_list:
            try:
                if exca is not None:
                    sample = _read_page(m, exca, w, 0, 256, dev_id=dev,
                                        verbose=verbose)
                else:
                    sample = read_mem(m, w["start"], 256, dev_id=dev,
                                      verbose=verbose)
            except DirectError as e:
                report["samples"].append({"start": w["start"], "hex": "",
                                          "width": 0, "blank_ff": False,
                                          "error": str(e)})
                continue
            data, width = normalize_bankwidth(sample)
            report["samples"].append({"start": w["start"], "hex": sample[:16].hex(),
                                      "width": width, "blank_ff": looks_blank(data, 0xFF)})
            if not looks_blank(sample, 0xFF):
                ok_any = True
        report["readable"] = True
        report["reason"] = "" if ok_any else ("окна читаются, но везде 0xFF - "
                                              "у стёртой флеш-карты это норма")
    except DirectError as e:
        report["readable"] = False
        report["reason"] = str(e)
    finally:
        m.close()
    if verbose:
        if report["readable"]:
            print("Доступ к /dev/mem: есть")
            what = ("common memory карты (сама флеш)"
                    if report["common_memory"] else "окно по адресу (без настройки ExCA)")
            print(f"  читается: {what}")
            for smp in report["samples"]:
                if smp.get("error"):
                    print(f"  окно {smp['start']:#010x}: ошибка - {smp['error']}")
                    continue
                print(f"  окно {smp['start']:#010x}: {smp['hex']} "
                      f"(ширина шины {smp['width']})"
                      + ("  [0xFF]" if smp["blank_ff"] else ""))
            good = [s for s in report["samples"] if s.get("width")]
            if good and all(s["blank_ff"] for s in good):
                print("  0xFF по всему окну = карта стёрта или её нет; паспорт при")
                print("  этом читается отдельно (раздел --cis) - решает он, а не это.")
            elif not good and any(s.get("error") for s in report["samples"]):
                print("  НИ ОДНО ОКНО НЕ ПРОЧИТАНО — про содержимое карты здесь ")
                print("  ничего не сказано,")
                print("  и слова про 0xFF к этому случаю не относятся: выше ")
                print("  причина отказа по каждому окну.")
                print(f"  Подсказка: {report.get('hint') or devmem_hint()}")
        else:
            print(f"Доступ к /dev/mem: нет — {report['reason']}")
    return report


__all__ = [
    "SIZE_STEPS", "PROBE_LEN", "TRUST_CIS_MAX",
    "_read_page", "_read_card",
    "cis_card_size", "card_geometry",
    "CIS_IN_COMMON", "CIS_LOOK",
    "cis_in_common_mem", "card_kind",
    "window_access_diag", "card_window_check",
    "find_card_size",
    "read_card_image",
    "image_regions", "image_stats", "file_sha256",
    "trim_image", "trim_hint", "image_report",
    "normalize_bankwidth", "looks_blank", "find_cis",
    "strings", "summarize",
    "journal_path", "journal_append", "journal_read",
    "journal_completed_sectors", "writes_for_bypass",
    "flash_probe", "flash_probe_safe",
    "flash_write_image", "flash_read_image",
    "filecmp_equal",
]
