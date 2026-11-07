# -*- coding: utf-8 -*-
"""Полные сценарии: проба, чтение образа, запись, стирание, журнал.

Сюда же отчёт о микросхеме и охрана стирания сектора."""

import os
import sys
import time

from .bridge import PAGE, _bridge_ctx, choose_window, open_best_mem, restore_window, snapshot_window, window_span
from .chips import (check_sector_map, chip_name_from_info, geometry_for,
                    identify_chip, sector_map, vpp_need)
from .cis import cis_location, read_cis
from .errors import DirectError
from .image import card_geometry
from .nor import NorBus, NorFlash, flash_plan, verify_geometry
from .nor_commands import NOR_CMD_ADDR1, NOR_CMD_ADDR2, SECTOR
from .power import check_vpp_state, set_vpp_for_write
from .version import KIT_VERSION


# ------------------------------------------------------------ ход записи ---

def flash_probe(flash, offset=0, payload=None, progress=None):
    """ПРОБА: стереть и записать ОДИН сектор, прочитать обратно, сравнить.

    Это единственный способ узнать, пишется ли карта в этом слоте, не
    трогая остальное содержимое. payload - что записать (по умолчанию
    узор: 0x00,0xFF,0x55,0xAA, 0..255 - ловит залипшие линии данных).
    """
    # границы сектора - по карте: у boot-чипа первый сектор 16 КиБ, и проба
    # «на 64 КиБ» вышла бы за него в соседние секторы
    offset, sector = flash.span(offset)
    if payload is None:
        pattern = bytearray([0x00, 0xFF, 0x55, 0xAA] * 64)
        pattern += bytes(range(256)) * 2
        payload = bytes(pattern)
    # сколько байт пишем в пробе: не весь сектор, а 2 КиБ (больше не нужно -
    # узор длиной 1024 байта повторяется), но у очень маленьких секторов
    # (16 КиБ и меньше) границы соблюдаем.
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
    # Программирование через program_byte могло открыть Unlock Bypass -
    # сбрасываем чипы, чтобы следующая операция (стирание, запись, чтение)
    # начиналась из чистого состояния.
    flash.reset()
    return True, f"стёрт и записан 1 сектор ({written} байт), прочитанное совпало"


def flash_probe_safe(flash, offset=0, backup_dir=None, progress=None):
    """Проба записи, которая не теряет данные: сектор сохраняется и возвращается.

    Порядок: прочитать сектор -> сохранить в файл -> стереть и записать узор ->
    сверить -> стереть -> вернуть исходные байты -> сверить. Если карта не
    пишется, вернуть исходное уже нечем - тогда файл остаётся на диске, и об
    этом говорится прямо (а не «всё хорошо»).
    """
    import os as _os
    base, sector = flash.span(offset)
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
    # Восстановление через program_byte могло оставить Unlock Bypass активным.
    flash.reset()
    return {"ok": True, "why": "проба прошла, сектор возвращён байт в байт",
            "backup": path, "restored": True}


def journal_path(remote_dir):
    """Путь к файлу журнала записи на ноутбуке."""
    import os as _os
    return _os.path.join(remote_dir, ".journal")


def journal_append(path, entry):
    """Дозаписать одну строку JSON в файл журнала.

    Каждая строка — отдельный JSON-объект. Пишем «открыть — дописать — закрыть»,
    чтобы при обрыве питания или ssh осталось то, что успели записать.
    """
    import json as _json
    line = _json.dumps(entry, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def journal_read(path):
    """Прочитать все записи из файла журнала. Список словарей (может быть пуст)."""
    import json as _json
    import os as _os
    if not _os.path.isfile(path):
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
                # битая строка в конце (обрыв записи) — игнорируем
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
    """Есть ли в плане хотя бы один сектор с записями (не пустой writes)."""
    return any(bool(writes) for _, _, writes in plan)


def _erase_sector_guarded(flash, off):
    """Стереть сектор и убедиться, что байты ЗА ЕГО ГРАНИЦАМИ не изменились.

    Зачем: ошибка в таблице микросхем («сектор 64 КиБ» там, где на железе
    128 КиБ) по самому сектору не видна — он-то сотрётся. Видна она по соседу:
    если байт за границей сектора был не 0xFF и стал 0xFF, значит стёрся блок
    БОЛЬШЕ заявленного, и соседние данные потеряны. Проверять это надо ДО
    записи: тогда отказ честный, а не «записалось, но сосед испорчен».

    Соседа два: последний байт слева и первый справа. Соседей, которые уже
    0xFF, не проверяем — по ним ничего не видно.
    """
    start, size = flash.span(off)
    probes = {}
    for probe in (start - 1, start + size):
        if probe < 0 or probe == off:
            continue
        try:
            b = flash.read(probe, 1)[0]
        except (DirectError, OSError, ValueError):
            continue                      # за пределами карты — проверять нечего
        if b != 0xFF:
            probes[probe] = b
    ok, why = flash.erase_sector(off)
    if not ok:
        return ok, why
    for probe, was in probes.items():
        try:
            now = flash.read(probe, 1)[0]
        except (DirectError, OSError, ValueError):
            continue
        if now != was:
            return False, (
                f"стёрся блок БОЛЬШЕ, чем записано в таблице: байт {probe:#x} "
                f"за границей сектора {start:#x}+{size:#x} был {was:#04x}, "
                f"стал {now:#04x}. Модель микросхемы неверна (у настоящей блок "
                f"длиннее) — стирание остановлено, чтобы не потерять остальное. "
                f"Проверьте имя в --chip и размер сектора в --sector")
    return True, ""


def flash_write_image(flash, image, progress=None, verify=True,
                      erase_first=False, journal=None, resume=False,
                      remote_dir=None, backup_dir=None):
    """Записать образ на карту. Возвращает dict с честным итогом.

    Порядок для каждого сектора: стирание (только если в секторе есть
    не-0xFF байты), затем построчная запись отличных байт. После всего -
    чтение обратно и сравнение. Ключ 'ok' в результате - только если
    сравнение сошлось; иначе 'failed_at' говорит, где остановились.

    backup_dir — папка для резервных копий секторов перед стиранием.
    Если задана, каждый сектор, который собираются стереть, сначала
    читается и сохраняется в файл <backup_dir>/sector-<offset>.bin.
    Это позволяет восстановить сектор, если запись прервётся или карта
    окажется изношенной.
    """
    plan = flash_plan(image, flash.sector, flash.spans(len(image)))
    total = len(plan)
    res = {"sectors": total, "erased": 0, "bytes": 0, "ok": False,
           "failed_at": None, "why": "", "backups": []}

    # Если journal не задан, но есть remote_dir — определяем путь по умолчанию.
    if journal is None and remote_dir:
        journal = journal_path(remote_dir)

    # Если backup_dir задан — создаём папку.
    if backup_dir:
        import os as _os
        try:
            _os.makedirs(backup_dir, exist_ok=True)
        except OSError as error:
            return {"ok": False, "why": f"не удалось создать папку бэкапа "
                    f"{backup_dir}: {error}", "backups": []}

    # Если resume — определяем, какие секторы уже записаны по журналу,
    # и пропускаем их.
    completed = set()
    if resume and journal:
        completed = journal_completed_sectors(journal)
        if completed:
            progress and progress(f"восстанавливаюсь после обрыва: "
                                  f"{len(completed)} сектор(ов) уже записано",
                                  0.0)

    # Unlock Bypass: входим в режим только на время записи байт, выходим
    # сразу после. Стирание и чтение делаются с полной разблокировкой, чтобы
    # не зависеть от состояния bypass. Выход гарантирован finally даже при
    # ошибке записи.
    bypass_active = bool(getattr(flash, "bypass", False)) and bool(
        any(writes for _, _, writes in plan))
    for no, (off, need_erase, writes) in enumerate(plan, 1):
        if off in completed:
            # Этот сектор уже записан в прошлом запуске — пропускаем.
            progress and progress(f"сектор {no} из {total}: {off:#x} (уже записан)",
                                  (no - 1) / max(total, 1))
            res["erased"] += 1 if need_erase else 0
            res["bytes"] += len(writes)
            continue

        if progress:
            progress(f"сектор {no} из {total}: {off:#x}",
                     (no - 1) / max(total, 1))
        # Бэкап сектора перед стиранием: читаем текущее содержимое и
        # сохраняем в файл. Если потом запись не удастся, можно вернуть
        # сектор как был.
        if backup_dir and need_erase:
            import os as _os
            backup_path = _os.path.join(backup_dir,
                                        f"sector-{off:08x}.bin")
            try:
                sector_data = flash.read(off, flash.span(off)[1])
                with open(backup_path, "wb") as bf:
                    bf.write(sector_data)
                res["backups"].append(backup_path)
            except (DirectError, OSError) as error:
                res["failed_at"] = off
                res["why"] = (f"не удалось сделать бэкап сектора "
                              f"{off:#x}: {error}")
                return res
        if need_erase:
            ok, why = _erase_sector_guarded(flash, off)
            if not ok:
                res["failed_at"] = off
                res["why"] = f"стирание сектора {off:#x}: {why}"
                return res
            res["erased"] += 1
            # Blank check: после стирания сектор должен быть целиком 0xFF.
            # Если нет — сектор изношен, и запись в него ненадёжна.
            blank = flash.read(off, flash.span(off)[1])
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
            ok, why = _erase_sector_guarded(flash, off)
            if not ok:
                res["failed_at"] = off
                res["why"] = f"стирание сектора {off:#x}: {why}"
                return res
            res["erased"] += 1
            blank = flash.read(off, flash.span(off)[1])
            bad = [i for i, b in enumerate(blank) if b != 0xFF]
            if bad:
                res["failed_at"] = off + bad[0]
                res["why"] = (f"сектор {off:#x} не стёрся полностью: "
                              f"первый не-0xFF байт по адресу "
                              f"{off + bad[0]:#x} "
                              f"(читается {blank[bad[0]]:#04x}). "
                              f"Сектор изношен или защищён.")
                return res
        # запись: если микросхема умеет страницу (geo.page > 1) - страницами,
        # иначе по байту. Логика одна и та же, различается только размер куска.
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
                        # какой именно байт не записался: читаем страницу назад.
                        # Для человека это разница между «страница 0x0 не прошла»
                        # и «байт 0x10 изношен, остальные записались».
                        res["failed_at"] = chunk[0]
                        try:
                            # два чтения: первое после отказа может показать
                            # недостоверный DQ7 (это и есть признак отказа)
                            back = flash.read(page_start, step)
                            diff = [k for k in range(step)
                                    if back[k] != image[page_start + k]]
                            if diff:
                                # на железе недостоверный DQ7 держится до сброса
                                # (FF), поэтому «не сошёлся» может быть и байт,
                                # который на самом деле записался: берём ПЕРВЫЙ
                                # несошедшийся и говорим об этом прямо
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
        # Сектор записан полностью — фиксируем в журнале (если ведётся).
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
        # Перед любой сверкой сбрасываем чипы: после записи (особенно после
        # неудачной) они могут находиться в статусе, а не в чтении массива.
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
    """Прочитать образ целиком тем же окном (для сверки и для проб)."""
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


def _flash_open(args, want_write=True, probe=True):
    """Открывает окно и готовит NorFlash. Возвращает (card_parts) или бросает.

    probe=False - не подавать в карту НИ ОДНОЙ команды: только паспорт и
    значения из таблицы. Это нужно ключу --identify-chip --chip ИМЯ.
    """
    dev, exca, wins, _bv, _bd = _bridge_ctx()
    chips_hint = int(args.chips or 0)
    chips, size = card_geometry(chips_hint)
    if chips_hint:
        chips = chips_hint
    win = choose_window(wins, size or 0)
    mem, src, _err = open_best_mem(dev)
    span = window_span(win, None)
    limit = 0
    if size:
        limit = size
    elif chips:
        limit = chips * (2 << 20)          # 2 МиБ на чип этой семьи
    bus = NorBus(mem, exca, win, span=span, dev_id=dev, writable=want_write,
                 limit=limit)
    # сначала «как по паспорту», потом уточняем у самой микросхемы
    try:
        _raw, _tup, info = read_cis(mem=None, verbose=False)
    except DirectError:
        info = {}
    pre = NorFlash(bus, chips=chips, sector=SECTOR)
    if probe:
        geo = identify_chip(pre, info=info, force=args.chip or None)
    else:
        name = (args.chip or chip_name_from_info(info or {}) or "").strip()
        g0 = geometry_for(name, size=size or None)
        geo = dict(name=name or "неизвестная", sector=g0["sector"],
                   sector_chip=g0["sector"], cmd=g0["cmd"], wide=g0["wide"],
                   size=g0["size"], chips=chips or 0, volts=g0["volts"],
                   protocol=g0.get("protocol", "jedec"),
                   page=g0.get("page", 1),
                   has_write_buffer=bool(g0.get("page", 1) > 1),
                   how="взято из таблицы по --chip (карта не опрашивалась)",
                   autodetect="")
    if geo.get("chips"):
        chips = geo["chips"]
    if geo.get("size") and not size:
        size = geo["size"]
        bus.limit = size
    if geo.get("size"):
        bus.limit = geo["size"]
    protocol = geo.get("protocol", "jedec")
    # geo["sector"] у identify_chip уже умножен на число чипов (адреса карты)
    sector_card = geo.get("sector") or SECTOR
    # Перед выдачей объекта, который умеет стирать, карта секторов должна быть
    # согласована: без неё границы сектора считались бы «по 64 КиБ» и первое
    # же стирание boot-чипа задело бы соседние данные.
    check_sector_map(geo, strict=True)
    flash = NorFlash(bus, chips=chips, sector=sector_card, cmd=tuple(geo["cmd"]),
                     wide=geo["wide"], page=geo.get("page") or 1,
                     bypass=bool(geo.get("bypass")),
                     name=geo.get("name", ""), protocol=protocol,
                     sector_map=geo.get("sectors") or None,
                     ro=bool(geo.get("ro")),
                     ro_why=str(geo.get("why") or ""))
    return {"dev": dev, "exca": exca, "wins": wins, "win": win, "mem": mem,
            "bus": bus, "flash": flash, "chips": chips, "size": size,
            "src": src, "geo": geo, "info": info}


def _flash_report(parts, extra_lines=()):
    """Что известно о карте перед записью - чтобы человек видел, куда лезет."""
    chips, size = parts["chips"], parts["size"]
    geo = parts.get("geo") or {}
    cmd = geo.get("cmd") or (NOR_CMD_ADDR1, NOR_CMD_ADDR2)
    # Версия набора - первой строкой отчёта: по ней видно, каким выпуском
    # снят отчёт (его просят присылать вместе с выводом).
    print(f"  версия набора         : {KIT_VERSION}")
    print(f"  микросхема            : {geo.get('name', 'неизвестная')} "
          f"({geo.get('how', '?')})")
    vol = geo.get("volts")
    volt_text = {5: "5 В (12 В не нужно)",
                 12: "12 В (Vpp) — слот столько не даёт",
                 3: "3 В"}.get(vol, "неизвестно")
    print("  напряжение программир.: " + volt_text)
    print(f"  микросхем в карте     : {chips} (байт i лежит в чипе i % {chips}"
          + (", то есть чётный/нечётный)" if chips == 2 else ")"))
    print(f"  ёмкость по паспорту   : "
          f"{size if size else 'неизвестна'}"
          + (f" ({size / 1024 / 1024:.1f} МиБ)" if size else ""))
    card_sec = geo.get("sector") or (SECTOR * max(int(chips), 1))
    chip_sec = geo.get("sector_chip") or -(-card_sec // max(int(chips), 1))
    smap = sector_map(geo)
    if len(smap) > 1:
        # boot-микросхема: одной цифрой «размер сектора» её не описать
        layout = " + ".join(f"{cnt} x {sz // 1024} КиБ" for sz, cnt in smap)
        print(f"  карта секторов        : {layout} (неоднородная, "
              f"на чип; в адресах карты x {chips})")
    else:
        print(f"  сектор стирания       : {card_sec // 1024} КиБ в адресах карты "
              f"({chip_sec // 1024} КиБ на чип x {chips} чип.)"
              + ("" if chip_sec == 64 << 10 else "  — НЕ 64 КиБ, учтено"))
    print("  протокол команд       : "
          + ("CUI Intel 28F (20h/D0h, статусный регистр 70h)"
             if geo.get("protocol") == "intel"
             else "JEDEC (AA/55, A0h, 80h/30h, DQ7/DQ5)"))
    print(f"  адреса команд         : {cmd[0]:#x} / {cmd[1]:#x}"
          + (", команда СЛОВОМ (Intel 28F)" if geo.get("wide") else ", командой байт")
          + (f", страница {geo.get('page')} байт" if (geo.get("page") or 1) > 1 else ""))
    if geo.get("unverified"):
        print("  ВНИМАНИЕ: параметры не сверены с даташитом: "
              + (geo.get("src") or "источника нет")[:110])
        print("            первый сектор проверьте на копии карты "
              "(или укажите --chip с проверенной моделью)")
    if geo.get("autodetect"):
        ad = geo["autodetect"]
        bits = []
        if ad.get("manuf") is not None:
            bits.append(f"ID {ad['manuf']:#04x}:{ad['device']:#04x}")
        if ad.get("cmdset_name"):
            bits.append("CFI, набор команд " + ad["cmdset_name"])
        if ad.get("cfi_version"):
            bits.append("CFI v" + str(ad["cfi_version"]))
        for reg in ad.get("regions") or []:
            bits.append("CFI: %d x %d КиБ" % (reg["blocks"], reg["size"] // 1024))
        if ad.get("size_chip"):
            bits.append("CFI: ёмкость чипа %.1f МиБ"
                        % (ad["size_chip"] / 1024 / 1024))
        if ad.get("has_write_buffer") and ad.get("page") and ad["page"] > 1:
            bits.append(f"CFI: буфер записи {ad['page']} байт")
        if ad.get("timeout_erase_sector_us"):
            bits.append("CFI: стирание сектора до %.0f мс"
                        % (ad["timeout_erase_sector_us"] / 1000))
        if bits:
            print("  ответ микросхемы      : " + "; ".join(bits))
    print(f"  окно моста            : {parts['win']['start']:#x}, "
          f"{parts['win']['size'] / 1024 / 1024:.0f} МиБ, заход "
          f"{parts['bus'].span // 1024} КиБ")
    for line in extra_lines:
        print(line)


def _identify_chip_main(args):
    """--identify-chip: рассказать, что за микросхема, и НИЧЕГО НЕ ПИСАТЬ.

    Опрос без --chip подаёт в карту команды 90h/98h (это чтение, данные не
    меняются). С --chip карта не опрашивается вовсе: набор просто пересказывает
    таблицу - так можно посмотреть, что он будет делать, не трогая карту.
    """
    probe = not (args.chip and args.no_geometry_check)
    print("Опрос микросхемы (ничего не пишется и не стирается)")
    if not probe:
        print("  режим: по --chip, карта не опрашивается")
    parts = _flash_open(args, want_write=False, probe=probe)
    try:
        geo = parts["geo"]
        _flash_report(parts)
        ad = geo.get("autodetect") or {}
        man, dev = ad.get("manuf"), ad.get("device")
        if (man is None or dev is None) and probe:
            print("  ответ микросхемы      : молчит (карта не пропускает "
                  "команды 90h/98h) — параметры взяты по паспорту")
        sector = geo.get("sector") or 0
        size = parts.get("size") or 0
        print(f"  секторов на карте     : {size // sector if sector and size else 'неизвестно'}")
        warn = []
        if geo.get("volts") == 12:
            warn.append("микросхеме нужны 12 В на Vpp: в слоте ноутбука их нет, "
                        "запись с неё невозможна (для Kontron PC/104-PCMCIA-1 - "
                        "только если 12 В подведены по PC/104)")
        if geo.get("ro"):
            warn.append("микросхема только ЧИТАЕТСЯ: "
                        + str(geo.get("why") or "команды не подтверждены"))
        elif geo.get("protocol") == "intel":
            warn.append("протокол Intel CUI: стирание только поблочно, "
                        "команды словом; статус читается регистром (70h)")
        if geo.get("name") in ("неизвестная", ""):
            warn.append("тип не определён: запись пойдёт по значениям по "
                        "умолчанию - сначала проба (--write-probe), при "
                        "сомнении укажите --chip")
        for w in warn:
            print(f"  ВНИМАНИЕ              : {w}")
        return 0
    finally:
        try:
            parts["bus"].close()
        except (DirectError, OSError, KeyError):
            pass
        try:
            parts["mem"].close()
        except (DirectError, OSError, KeyError):
            pass


def _flash_main(args):
    """--write-probe и --write: запись без драйвера."""
    parts = _flash_open(args)
    flash = parts["flash"]
    mem, exca, dev = parts["mem"], parts["exca"], parts["dev"]
    print("Прямая запись без драйвера (команды JEDEC через окно моста)")
    _flash_report(parts)
    snap = snapshot_window(mem, exca, dev)
    t0 = time.time()
    rc = 0
    info = parts.get("info") or {}
    where, why_where = cis_location(info)
    print(f"  паспорт карты         : {why_where}")
    # защита от записи по паспорту: если переключатель включён, стирание и
    # запись всё равно не пройдут - говорим сразу, а не после первой ошибки
    if any(d.get("wp") for d in info.get("devices") or []) and not getattr(
            args, "force_write", False):
        print("\nИТОГ: в паспорте отмечен переключатель защиты от записи (WPS).")
        print("  Проверьте переключатель на корпусе карты. Если он включён,")
        print("  стирание и запись физически не пройдут. Обойти это ключом")
        print("  --force-write можно, только если переключатель точно выключен,")
        print("  а паспорт просто старый.")
        return 4
    try:
        # ДО любой записи: та ли микросхема и тот ли сектор? Проверка активная
        # (стиранием последнего сектора), но не разрушающая: снимок сектора
        # снимается ДО стирания и возвращается после - внутри verify_geometry.
        if not getattr(args, "no_geometry_check", False) and parts["size"]:
            backup = None
            if getattr(args, "snapshot_dir", None):
                os.makedirs(args.snapshot_dir, exist_ok=True)
                backup = os.path.join(
                    args.snapshot_dir,
                    "geometry_probe_%#x.bin" % parts["size"])
            ok_g, why_g = verify_geometry(flash, {"sector": flash.sector,
                                                  "size": parts["size"]},
                                          backup_path=backup)
            print("  проверка геометрии    : " + ("ок - " if ok_g else "НЕ ТАК - ")
                  + why_g)
            if not ok_g:
                print("\nИТОГ: не начинаю стирание/запись - не сходится тип "
                      "микросхемы.")
                print("  Проверьте вручную: --chip <ИМЯ> или --sector <размер>. ")
                return 3

        if where == "array" and not getattr(args, "allow_cis_erase", False):
            img_path = getattr(args, "write", None)
            # образ задевает блок 0, если в начале образа есть не-0xFF байты
            touches = bool(img_path)
            if img_path:
                try:
                    with open(img_path, "rb") as f:
                        head = f.read(flash.span(0)[1])
                    touches = any(b != 0xFF for b in head)
                except OSError:
                    touches = True
            probe0 = (args.write_probe and int(args.sector or 0, 0) == 0) or touches
            if probe0:
                back = flash.read(0, flash.span(0)[1])
                has_cis = (b"CIS" in back or info.get("offset") is not None or
                           any(b != 0xFF for b in back))
                if has_cis:
                    print("\nВНИМАНИЕ: паспорт этой карты лежит в блоке 0 и "
                          "будет стёрт.")
                    print("  Набор сохранит копию блока 0 в файл, но вернуть "
                          "паспорт сможет только")
                    print("  если запись в карту работает. Если не сработает - "
                          "паспорт потерян.")
                    print("  Если это нежелательно: --sector <другой сектор> "
                          "или --allow-cis-erase (согласие заведомо).")
                    if not args.allow_cis_erase:
                        return 5

        if args.write_probe:
            off = int(args.sector, 0) if str(args.sector) else 0
            print(f"\nПРОБА: сектор {off:#x}. Данные сектора сохраняются и "
                  "возвращаются на место.")
            print("  (единственный способ узнать, пишется ли карта, не трогая "
                  "остальное)")
            res = flash_probe_safe(flash, off, backup_dir=args.snapshot_dir or ".",
                                   progress=lambda t, *a: print(f"  ... {t}"))
            print()
            if res["ok"]:
                print(f"ИТОГ: карта ПИШЕТСЯ. {res['why']}")
                print("  Теперь можно писать образ: --write <файл>")
            else:
                print(f"ИТОГ: карта НЕ пишется так, как ожидалось. {res['why']}")
                print("  Образ и остальные секторы не тронуты.")
                if res.get("backup"):
                    print(f"  Исходные байты сектора сохранены: {res['backup']}")
                if not res.get("restored"):
                    print("  ВЕРНУТЬ ИСХОДНЫЕ БАЙТЫ НЕ УДАЛОСЬ - на карте "
                          "испорчен один сектор, файл выше это копия.")
                    print("  Вероятные причины: карте нужно Vpp 12 В, сектор "
                          "защищён или изношен.")
            rc = 0 if res["ok"] else 3
            return rc

        img = args.write
        if not os.path.exists(img):
            print(f"ОШИБКА: нет файла {img}", file=sys.stderr)
            return 2
        data = open(img, "rb").read()
        if not data:
            print("ОШИБКА: файл пуст", file=sys.stderr)
            return 2
        if parts["size"] and len(data) > parts["size"]:
            print(f"ОШИБКА: образ {len(data)} байт больше карты "
                  f"({parts['size']} байт) - запись отменена", file=sys.stderr)
            return 2
        # Границы секторов - по карте микросхемы: у boot-чипов они разного
        # размера, и «кратно 64 КиБ» ничего не гарантирует. Запрещаем образ,
        # который обрезает сектор посередине: хвост остался бы стёртым.
        spans = flash.spans(len(data))
        _off, _sz = spans[-1]
        _full = flash.span(_off)[1]
        if _sz != _full:
            print(f"ОШИБКА: размер образа ({len(data)}) обрезает сектор "
                  f"{_off:#x} ({_sz} из {_full} байт): так писать нельзя - "
                  "хвост сектора остался бы стёртым", file=sys.stderr)
            return 2
        if parts["size"] and len(data) < parts["size"]:
            left = parts["size"] - len(data)
            print(f"ВНИМАНИЕ: образ меньше карты на {left} байт; эти секторы "
                  "останутся как есть (набор их не стирает).")
        plan = flash_plan(data, flash.sector, spans)
        need = sum(1 for _o, e, _w in plan if e)
        nbytes = sum(len(w) for _o, _e, w in plan)
        print(f"\nПлан: секторов всего {len(plan)}, из них стирать {need}, "
              f"записать непустых байт {nbytes} "
              f"(байты 0xFF не пишутся - после стирания они и так 0xFF)")
        import hashlib
        print(f"  md5 образа: {hashlib.md5(data).hexdigest()}")
        print("\nНачинаю. Это может занять минуты: стирание сектора - до "
              "нескольких секунд, байт пишется отдельной командой.")
        last = [0.0]

        def prog(text, frac=None):
            now = time.time()
            if frac is None or now - last[0] > 0.5:
                last[0] = now
                tail = f" ({frac * 100:.0f}%)" if frac is not None else ""
                print(f"  {text}{tail}", flush=True)

        # --- VPP: проверяем и включаем программирующее напряжение перед записью ---
        # 2.18.2: вольты берём из vpp_need(), как это делает пакет. До этого
        # здесь было жёстко «5 В» (аргумент функции включения Vpp), и даже
        # 12-вольтовой микросхеме (Intel
        # 28F008SA/28F016SA, AMD Am28F*, то есть именно 12-вольтовым картам)
        # подавалось 5 В: стирание и запись не проходили, а «не тот Vpp»
        # выглядел как сбойная карта или сбитый CIS.
        volts, why_v = vpp_need(info)
        vpp = check_vpp_state(dev, mem, exca)
        if vpp["ok"]:
            print(f"  VPP: включён ({vpp['vpp']}, через {vpp['source']})")
            if volts in (5, 12) and vpp.get("vpp") and volts != vpp.get("vpp"):
                print(f"  ВНИМАНИЕ: по документации нужно {volts} В, а сейчас "
                      f"{vpp.get('vpp')} В ({why_v}).")
        elif volts in (5, 12):
            ok_vpp, msg_vpp = set_vpp_for_write(dev, voltage=volts,
                                                exca_mem=mem,
                                                exca_addr=exca)
            print(f"  VPP: {msg_vpp} ({why_v})")
            if not ok_vpp:
                print(f"  ВНИМАНИЕ: VPP не включён. Запись может не пройти — "
                      f"микросхеме нужно {volts} В на VPP ({why_v}).")
        else:
            # 3 - микросхеме Vpp не нужен (3-вольтовая), 0 - не определить.
            print(f"  VPP: не включаю - {why_v}. Если запись не пойдёт, "
                  "укажите модель ключом --chip.")

        res = flash_write_image(flash, data, progress=prog,
                               verify=not args.no_verify,
                               resume=args.resume,
                               journal=args.journal or None,
                               remote_dir=args.kit_dir or "",
                               backup_dir=args.backup or None)
        print()
        if res["ok"]:
            print(f"ИТОГ: записано и СВЕРЕНО. Секторов стёрто {res['erased']}, "
                  f"байт записано {res['bytes']}.")
            if not args.no_verify:
                print("  Карта прочитана обратно целиком и совпала с образом "
                      "байт в байт.")
            print(f"  Время: {time.time() - t0:.1f} с")
            return 0
        print(f"ИТОГ: НЕ УДАЛОСЬ. {res['why']}")
        if res["failed_at"] is not None:
            print(f"  Остановились на смещении {res['failed_at']:#x}. "
                  "Образ (файл) цел; на карте изменены только секторы, "
                  "которые успели стереть и записать.")
        print("  Что это обычно значит: карте нужно Vpp 12 В, сектор защищён "
              "или изношен, либо контакт карты плохой.")
        return 3
    finally:
        try:
            flash.reset()
        except (DirectError, OSError):
            pass
        parts["bus"].close()
        if not restore_window(mem, exca, snap, dev):
            print("  ВНИМАНИЕ: окно моста вернуть не удалось. Если карта "
                  "перестала читаться - выключите и включите слот.")
        mem.close()
