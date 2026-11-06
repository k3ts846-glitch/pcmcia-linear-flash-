#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI точка входа для прямого доступа к PCMCIA linear flash.

Вынесен из pcmcia_direct.py. Аргументы, отчёт, main() — здесь.
"""

import argparse
import hashlib
import json
import os
import sys
import time

from .chips import (
    SECTOR, geometry_for, identify_chip, verify_geometry,
    chip_name_from_info, flash_plan, vpp_need,
)
from .cis import (
    read_cis, cis_extra_report, cis_dump, cis_notes, scan_cis, parse_cis,
    CIS_TAG, _window_text, find_bridge_ids,
    bridge_windows, passport_ok, cis_summary,
)
from .errors import DirectError
from .image import (
    flash_probe, flash_probe_safe, flash_write_image, flash_read_image,
    card_geometry, card_window_check, window_access_diag, find_card_size,
    read_card_image, image_report, image_stats, trim_hint,
    normalize_bankwidth, filecmp_equal, card_kind, cis_in_common_mem,
    _read_page, probe, summarize, strings, looks_blank as _lb,
    cis_card_size, PROBE_LEN,
)
from .mem import (
    Mem, mem_for_addr, open_best_mem, read_mem, choose_window,
    window_span, parse_size, setup_cis_window, restore_cis_window,
    map_card_window, cardbus_windows,
)
from .nor_flash import NorFlash, NorBus
from .power import check_vpp_state, set_vpp_for_write

# --- мост / окно --------------------------------------------------------------

def _bridge_ctx():
    """PCI-адрес моста, BAR0, ExCA, vendor/device."""
    try:
        dev, bar0, vendor, device = find_bridge_ids()
    except DirectError:
        return None, None, None, None, None
    if bar0 is None:
        return None, None, None, None, None
    exca = bar0 + 0x800
    return dev, exca, None, vendor, device


# --- окно моста (снимок/восстановление) --------------------------------------

def snapshot_window(mem, exca, dev_id=None):
    """Запомнить регистры окна 0, чтобы в конце вернуть их как было."""
    regs = mem_for_addr(mem, exca, writable=True, dev_id=dev_id)
    from .mem import I365_ADDRWIN, CB_MEM_PAGE0, I365_MEM0_START, I365_MEM0_STOP, I365_MEM0_OFF
    return {
        "addrwin": regs.read(exca + I365_ADDRWIN, 1),
        "page0": regs.read(exca + CB_MEM_PAGE0, 1),
        "start": regs.read(exca + I365_MEM0_START, 2),
        "stop": regs.read(exca + I365_MEM0_STOP, 2),
        "off": regs.read(exca + I365_MEM0_OFF, 2),
    }


def restore_window(mem, exca, old, dev_id=None):
    """Вернуть регистры окна. Ошибки глотаются: это уборка, а не работа."""
    from .mem import I365_ADDRWIN, CB_MEM_PAGE0, I365_MEM0_START, I365_MEM0_STOP, I365_MEM0_OFF
    try:
        regs = mem_for_addr(mem, exca, writable=True, dev_id=dev_id)
        restore_cis_window(regs, exca, old)
        return True
    except (DirectError, OSError, KeyError, TypeError):
        return False


# --- открытие карты и подготовка NorFlash ------------------------------------

def _flash_open(args, want_write=True, probe=True):
    """Открывает окно и готовит NorFlash. Возвращает dict с частями или бросает.

    probe=False - не подавать в карту НИ ОДНОЙ команды: только паспорт и
    значения из таблицы. Это нужно ключу --identify-chip --chip ИМЯ.
    """
    dev, exca, _wins, _vendor, _device = _bridge_ctx()
    if dev is None:
        raise DirectError("мост CardBus не найден")
    wins = cardbus_windows(args.windows) if hasattr(args, 'windows') and args.windows else []
    chips_hint = int(getattr(args, "chips", 0) or 0)
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
        limit = chips * (2 << 20)
    bus = NorBus(mem, exca, win, span=span, dev_id=dev, writable=want_write,
                 limit=limit)
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
    sector_card = geo.get("sector") or SECTOR
    flash = NorFlash(bus, chips=chips, sector=sector_card,
                     cmd=tuple(geo["cmd"]), wide=geo["wide"],
                     page=geo.get("page") or 1,
                     bypass=bool(geo.get("bypass")),
                     name=geo.get("name", ""), protocol=protocol,
                     sector_map=geo.get("sectors") or None,
                     ro=bool(geo.get("ro")),
                     ro_why=str(geo.get("why") or ""))
    return {"dev": dev, "exca": exca, "wins": wins, "win": win, "mem": mem,
            "bus": bus, "flash": flash, "chips": chips, "size": size,
            "src": src, "geo": geo, "info": info}


def _flash_report(parts, extra_lines=()):
    """Что известно о карте перед записью."""
    from .constants import NOR_CMD_ADDR1, NOR_CMD_ADDR2
    chips, size = parts["chips"], parts["size"]
    geo = parts.get("geo") or {}
    cmd = geo.get("cmd") or (NOR_CMD_ADDR1, NOR_CMD_ADDR2)
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
    print(f"  сектор стирания       : {card_sec // 1024} КиБ в адресах карты "
          f"({chip_sec // 1024} КиБ на чип x {chips} чип.)"
          + ("" if chip_sec == 64 << 10 else "  — НЕ 64 КиБ, учтено"))
    print("  протокол команд       : "
          + ("CUI Intel 28F (20h/D0h, статусный регистр 70h)"
             if geo.get("protocol") == "intel"
             else "JEDEC (AA/55, A0h, 80h/30h, DQ7/DQ5)"))
    sectors = geo.get("sectors") or []
    if len(sectors) > 1:
        layout = " + ".join(f"{count} x {size // 1024} КиБ"
                            for size, count in sectors)
        print(f"  карта секторов        : {layout} (неоднородная)")
    print(f"  адреса команд         : {cmd[0]:#x} / {cmd[1]:#x}"
          + (", команда СЛОВОМ (Intel 28F)" if geo.get("wide")
             else ", командой байт")
          + (f", страница {geo.get('page')} байт"
             if (geo.get("page") or 1) > 1 else ""))
    if geo.get("autodetect"):
        ad = geo["autodetect"]
        bits = []
        if ad.get("manuf") is not None:
            bits.append(f"JEDEC ID {ad['manuf']:#04x}:{ad['device']:#04x}")
        cfi = ad.get("cfi") or {}
        if cfi:
            if cfi.get("cmdset_name"):
                bits.append(f"CFI, набор команд {cfi['cmdset_name']}")
            if cfi.get("size"):
                bits.append(f"CFI: ёмкость чипа {cfi['size'] / 1024 / 1024:.1f} МиБ")
            for reg in cfi.get("regions") or []:
                bits.append(f"CFI: {reg['blocks']} x "
                            f"{reg['size'] // 1024} КиБ")
            if cfi.get("buf_write"):
                bits.append(f"CFI: буфер записи {cfi['buf_write']} байт")
            if cfi.get("t_sector_erase_us"):
                bits.append(f"CFI: стирание сектора до "
                            f"{cfi['t_sector_erase_us'] / 1000:.0f} мс")
            vcc = cfi.get("vcc_write_mv") or (0, 0)
            if vcc[1]:
                bits.append(f"CFI: Vcc при записи {vcc[0] / 1000:.1f}-"
                            f"{vcc[1] / 1000:.1f} В")
        elif ad.get("manuf") is not None:
            bits.append("CFI не отвечает (у ранних ревизий его нет)")
        if bits:
            print("  ответ микросхемы      : " + "; ".join(bits))
    print(f"  окно моста            : {parts['win']['start']:#x}, "
          f"{parts['win']['size'] / 1024 / 1024:.0f} МиБ, заход "
          f"{parts['bus'].span // 1024} КиБ")
    for line in extra_lines:
        print(line)


def _identify_chip_main(args):
    """--identify-chip: рассказать, что за микросхема, и НИЧЕГО НЕ ПИСАТЬ."""
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
        if geo.get("protocol") == "intel":
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
    where = "array"
    devs = info.get("devices") or []
    print("  паспорт карты         : "
          + (f"{len(devs)} устройство(а) в CIS" if devs
             else "CIS не разобран - параметры берём из автоопределения"))
    if any(d.get("wp") for d in info.get("devices") or []) and not getattr(
            args, "force_write", False):
        print("\nИТОГ: в паспорте отмечен переключатель защиты от записи (WPS).")
        print("  Проверьте переключатель на корпусе карты. Если он включён,")
        print("  стирание и запись физически не пройдут. Обойти это ключом")
        print("  --force-write можно, только если переключатель точно выключен,")
        print("  а паспорт просто старый.")
        return 4
    try:
        if not getattr(args, "no_geometry_check", False) and parts["size"]:
            # Копию сектора проверки кладём в файл, если задан --snapshot-dir:
            # тогда даже обрыв питания посреди проверки не потеряет данные.
            backup = None
            if getattr(args, "snapshot_dir", None):
                os.makedirs(args.snapshot_dir, exist_ok=True)
                backup = os.path.join(args.snapshot_dir,
                                      f"geometry_probe_{parts['size']:#x}.bin")
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
            touches = bool(img_path)
            if img_path:
                try:
                    with open(img_path, "rb") as f:
                        head = f.read(flash.sector)
                    touches = any(b != 0xFF for b in head)
                except OSError:
                    touches = True
            probe0 = (args.write_probe and int(args.sector, 0) == 0) or touches
            if probe0:
                back = flash.read(0, flash.sector)
                has_cis = (b"CIS" in back or info.get("offset") is not None or
                           any(b != 0xFF for b in back))
                if has_cis:
                    print("\nВНИМАНИЕ: паспорт этой карты лежит в блоке 0 и "
                          "будет стёрт.")
                    print("  Набор сохранит копию блока 0 в файл, но вернуть ")
                    print("  паспорт сможет только если запись в карту работает.")
                    print("  Если не сработает - паспорт потерян.")
                    print("  Если это нежелательно: --sector <другой сектор> ")
                    print("  или --allow-cis-erase (согласие заведомо).")
                    if not args.allow_cis_erase:
                        return 5

        if args.write_probe:
            off = int(args.sector, 0) if str(args.sector) else 0
            print(f"\nПРОБА: сектор {off:#x}. Данные сектора сохраняются и "
                  "возвращаются на место.")
            print("  (единственный способ узнать, пишется ли карта, не трогая ")
            print("  остальное)")
            res = flash_probe_safe(flash, off,
                                   backup_dir=args.snapshot_dir or ".",
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
                    print("  ВЕРНУТЬ ИСХОДНЫЕ БАЙТЫ НЕ УДАЛОСЬ - на карте ")
                    print("  испорчен один сектор, файл выше это копия.")
                    print("  Вероятные причины: карте нужно Vpp 12 В, сектор ")
                    print("  защищён или изношен.")
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
        spans = (flash.spans(len(data))
                 if callable(getattr(flash, "spans", None)) else None)
        if spans:
            tail_off, tail_have = spans[-1]
            tail_full = flash.span(tail_off)[1]
        else:
            tail_off, tail_full = 0, flash.sector
            tail_have = len(data) % flash.sector or tail_full
        if tail_have != tail_full:
            print(f"ОШИБКА: размер образа ({len(data)}) обрезает сектор "
                  f"{tail_off:#x} ({tail_have} из {tail_full} байт): так "
                  f"писать нельзя - хвост сектора остался бы стёртым",
                  file=sys.stderr)
            return 2
        if parts["size"] and len(data) < parts["size"]:
            left = parts["size"] - len(data)
            print(f"ВНИМАНИЕ: образ меньше карты на {left} байт; эти секторы ")
            print("  останутся как есть (набор их не стирает).")
        plan = flash_plan(data, flash.sector, spans=spans)
        need = sum(1 for _o, e, _w in plan if e)
        nbytes = sum(len(w) for _o, _e, w in plan)
        print(f"\nПлан: секторов всего {len(plan)}, из них стирать {need}, "
              f"записать непустых байт {nbytes} "
              f"(байты 0xFF не пишутся - после стирания они и так 0xFF)")
        print(f"  md5 образа: {hashlib.md5(data).hexdigest()}")
        print("\nНачинаю. Это может занять минуты: стирание сектора - до ")
        print("  нескольких секунд, байт пишется отдельной командой.")
        last = [0.0]

        def prog(text, frac=None):
            now = time.time()
            if frac is None or now - last[0] > 0.5:
                last[0] = now
                tail = f" ({frac * 100:.0f}%)" if frac is not None else ""
                print(f"  {text}{tail}", flush=True)

        # Напряжение VPP - ИЗ ТАБЛИЦЫ/ПАСПОРТА, а не всегда 5 В: у Intel
        # 28Fxxx (SA/SV) программирование идёт при 12 В, и подача 5 В вместо
        # 12 В означает «стирание не проходит» без внятной причины.
        volts, why_v = vpp_need(info)
        vpp = check_vpp_state(dev, mem, exca)
        if vpp["ok"]:
            print(f"  VPP: включён ({vpp['vpp']}, через {vpp['source']})")
            if volts and vpp.get("vpp") and volts != vpp.get("vpp"):
                print(f"  ВНИМАНИЕ: по документации нужно {volts} В, "
                      f"а сейчас {vpp.get('vpp')} В ({why_v}).")
        elif volts:
            ok_vpp, msg_vpp = set_vpp_for_write(dev, voltage=volts,
                                                exca_mem=mem,
                                                exca_addr=exca)
            print(f"  VPP: {msg_vpp} ({why_v})")
            if not ok_vpp:
                print("  ВНИМАНИЕ: VPP не включён. Стирание и запись, скорее "
                      "всего, не пройдут.")
        else:
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
                print("  Карта прочитана обратно целиком и совпала с образом ")
                print("  байт в байт.")
            print(f"  Время: {time.time() - t0:.1f} с")
            return 0
        print(f"ИТОГ: НЕ УДАЛОСЬ. {res['why']}")
        if res["failed_at"] is not None:
            print(f"  Остановились на смещении {res['failed_at']:#x}. "
                  "Образ (файл) цел; на карте изменены только секторы, ")
            print("  которые успели стереть и записать.")
        print("  Что это обычно значит: карте нужно Vpp 12 В, сектор защищён ")
        print("  или изношен, либо контакт карты плохой.")
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


# --- main ---------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Прямое чтение 16-битной PCMCIA linear flash через /dev/mem "
                    "(работает, когда ядро не дало карте окно памяти).",
        epilog="Чтение - всегда. С 2.14.0 есть и запись без драйвера: "
               "--write-probe (один сектор, со возвратом данных) и "
               "--write <образ>. Микросхемы бывают разные (JEDEC и Intel CUI, "
               "сектор 16/32/64/128 КиБ, 1..8 чипов): тип определяется по "
               "паспорту и ответу самой микросхемы, при сомнении - --chip. "
               "Напряжение программирования (Vpp) не выставляется никогда.")
    ap.add_argument("--probe", action="store_true",
                    help="только проверить доступ")
    ap.add_argument("--cis", action="store_true",
                    help="прочитать CIS через attribute memory (паспорт карты)")
    ap.add_argument("--read", metavar="ФАЙЛ",
                    help="снять образ карты в файл")
    ap.add_argument("--double", action="store_true",
                    help="при --read: два чтения + сверка")
    ap.add_argument("--span", metavar="N", default="",
                    help="сколько байт читать за заход (по умолчанию 4 МиБ; "
                         "4096 - если большой заход не работает)")
    ap.add_argument("--trim", metavar="N", default="",
                    help="обрезать готовый образ до N байт (остальное - шина за картой)")
    ap.add_argument("--identify", action="store_true",
                    help="что за карта: linear flash, ATA, порт/сеть (по паспорту)")
    ap.add_argument("--image-report", metavar="ФАЙЛ",
                    help="сводка по уже снятому образу: размер, md5, доля 0xFF/0x00")
    ap.add_argument("--size", default="auto",
                    help="сколько читать при --read: auto (по повторению адресов, "
                         "по умолчанию) либо 1m/2m/4m/8m/16m/32m")
    ap.add_argument("--addr", help="адрес для точечного чтения, например 0x64000000")
    ap.add_argument("--len", default="256", help="сколько байт читать (по умолчанию 256)")
    ap.add_argument("--hex", action="store_true", help="печатать hex-дамп")
    ap.add_argument("--json", action="store_true", help="машинный вывод")
    ap.add_argument("--extra", action="store_true",
                    help="сырые факты об attribute memory (маркер CIS, строки, хвост)")
    ap.add_argument("--full", action="store_true",
                    help="в CIS-дампе печатать окно цепочки целиком")
    ap.add_argument("--windows", metavar="ФАЙЛ",
                    help="файл с выводом lspci -v (для проверки без слота)")
    ap.add_argument("--write-probe", action="store_true",
                    help="ПРОБА: стереть и записать ОДИН сектор, сверить, вернуть "
                         "исходные байты. Ничего другого не трогает")
    ap.add_argument("--sector", metavar="N", default="0",
                    help="какой сектор пробовать при --write-probe (по умолчанию 0)")
    ap.add_argument("--write", metavar="ФАЙЛ",
                    help="записать образ на карту без драйвера (команды JEDEC "
                         "через окно моста)")
    ap.add_argument("--chip", metavar="ИМЯ", default="",
                    help="тип микросхемы вручную (Am29F016, 28F008SA, ...), "
                         "если паспорт врёт и автоопределение не сработало")
    ap.add_argument("--chips", metavar="N", default="0",
                    help="сколько микросхем в паре (0 = взять из паспорта)")
    ap.add_argument("--no-verify", action="store_true",
                    help="при --write не читать карту обратно для сверки "
                         "(по умолчанию сверка обязательна)")
    ap.add_argument("--identify-chip", action="store_true",
                    help="только опрос микросхемы: паспорт, ID (90h), CFI (98h), "
                         "сектор, протокол, напряжение. НИЧЕГО НЕ ПИШЕТ")
    ap.add_argument("--allow-cis-erase", action="store_true",
                    help="разрешить стирание блока с паспортом карты "
                         "(паспорт лежит в блоке 0 массива)")
    ap.add_argument("--force-write", action="store_true",
                    help="писать, даже если в паспорте отмечен переключатель "
                         "защиты от записи (только если он заведомо выключен)")
    ap.add_argument("--kit-dir", default="",
                    help="папка набора на ноутбуке (для --journal по умолчанию "
                         "<папка>/.journal)")
    ap.add_argument("--no-geometry-check", action="store_true",
                    help="не проверять тип микросхемы стиранием крайнего сектора "
                         "(по умолчанию проверка делается всегда)")
    ap.add_argument("--resume", action="store_true",
                    help="продолжить прерванную запись с последнего сектора "
                         "(нужен --journal или <папка набора>/.journal)")
    ap.add_argument("--journal", metavar="ФАЙЛ", default="",
                    help="файл журнала записи на ноутбуке (по умолчанию "
                         "<папка набора>/.journal). Каждый завершённый сектор "
                         "фиксируется здесь, чтобы при обрыве можно было "
                         "продолжить с того же места.")
    ap.add_argument("--snapshot-dir", metavar="КАТАЛОГ", default="",
                    help="куда сохранять снимок сектора перед записью "
                         "(по умолчанию рядом с образом)")
    ap.add_argument("--backup", metavar="КАТАЛОГ", default="",
                    help="папка для резервных копий секторов перед стиранием. "
                    "Каждый сектор, который собираются стереть, сначала "
                    "читается и сохраняется в файл sector-<offset>.bin. "
                    "Позволяет восстановить сектор, если запись "
                    "прервётся или карта окажется изношенной.")
    args = ap.parse_args(argv)

    # сводка по готовому образу не требует ни слота, ни root
    if args.trim:
        try:
            n = parse_size(args.trim)
        except DirectError as e:
            print(f"ОШИБКА: {e}", file=sys.stderr)
            return 2
        if not n:
            print("ОШИБКА: укажите размер числом, например --trim 4194304",
                  file=sys.stderr)
            return 2
        src = args.image_report or args.read or ""
        if not src or not os.path.exists(src):
            print("ОШИБКА: обрезать нечего - укажите файл: --trim 4194304 --image-report card.bin",
                  file=sys.stderr)
            return 2
        if os.path.getsize(src) <= n:
            print(f"Обрезать нечего: {src} уже {os.path.getsize(src)} байт "
                  f"(просили {n})")
            return 0
        from .image import trim_image
        out, got = trim_image(src, n)
        print(f"Обрезано: {src} -> {out} ({got} байт)")
        print("Исходный файл оставлен как есть.")
        return 0

    if args.identify:
        try:
            _raw, tuples, info = read_cis(None, verbose=False)
        except DirectError as e:
            print(f"ОШИБКА: {e}", file=sys.stderr)
            return 2
        kind, ktext = card_kind(info, tuples)
        if kind == "unknown" and not tuples:
            try:
                dev_i, exca_i, wins_i, _bvi, _bdi = _bridge_ctx()
                win_i = choose_window(wins_i, 0)
                mem_i, _s, _e = open_best_mem(dev_i)
                try:
                    off_i, tp_i, inf_i = cis_in_common_mem(mem_i, exca_i, win_i,
                                                          dev_id=dev_i)
                finally:
                    mem_i.close()
                if tp_i:
                    kind, ktext = card_kind(inf_i, tp_i, at_off=off_i)
                    info = inf_i
            except DirectError:
                pass
        prod = " / ".join((info or {}).get("product") or []) or "нет строк"
        print(f"вид карты: {kind}")
        print(f"  {ktext}")
        print(f"  паспорт: {prod}")
        if (info or {}).get("manfid"):
            print(f"  MANFID : 0x{info['manfid'][0]:04x}, карта 0x{info['manfid'][1]:04x}")
        if kind in ("linear-flash", "memory", "ata"):
            print("  образ снимается: --read <файл> [--double]")
        elif kind == "unknown":
            print("  карта не опознана: пришлите вывод windows/legacy/pcmcia_direct.py --cis")
        else:
            print("  это не носитель: читать можно паспорт, образ не нужен")
        return 0 if kind != "unknown" else 4

    if args.image_report:
        if not os.path.exists(args.image_report):
            print(f"ОШИБКА: файла {args.image_report} нет", file=sys.stderr)
            return 2
        st = image_stats(args.image_report)
        print("\n".join(image_report(
            {"path": args.image_report, "size": st["bytes"],
             "md5": hashlib_md5(args.image_report)},
            st, {}, "размер файла на диске")))
        return 0

    win_list = bridge_windows(args.windows)
    if win_list is None:
        return 2

    if not args.json:
        print("Окна памяти CardBus:")
        for i, w in enumerate(win_list):
            print(f"  окно {i}: {w['start']:#010x}-{w['end']:#010x} "
                  f"({w['size'] / 1024 / 1024:.1f} МиБ"
                  f"{', prefetchable' if w['prefetch'] else ''})")

    if args.probe:
        rep = probe(win_list, verbose=not args.json)
        if args.json:
            print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep["readable"] else 2

    if args.extra:
        try:
            attr, tuples, info = read_cis(None, verbose=False)
        except DirectError as e:
            print(f"ОШИБКА: {e}", file=sys.stderr)
            return 2
        print("\n".join(cis_extra_report(attr, info)))
        return 0

    if args.cis:
        try:
            attr, tuples, info = read_cis(None, verbose=not args.json)
        except DirectError as e:
            print(f"ОШИБКА: {e}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps({"cis_raw": attr.hex(), "summary": info,
                              "tuples": [{"code": t["code"], "name": t["name"],
                                          "len": t["len"]} for t in tuples]},
                             ensure_ascii=False, indent=2))
            return 0
        if not passport_ok(info) and tuples:
            print("\nЦепочка найдена, но в ней нет ни названия (VERS_1),")
            print("  ни MANFID, ни DEVICE - это ещё не паспорт.")
            return 4
        show = cis_dump(attr, info)
        print("\nAttribute memory:")
        for line in show:
            print("  " + line)
        if not tuples:
            blank = info.get("blank")
            try:
                dev2, exca2, wins2, _bv2, _bd2 = _bridge_ctx()
                diag_lines, suspect = window_access_diag(dev2, exca2,
                                                         choose_window(wins2, 0))
                if diag_lines:
                    print("\nДоступ к окну (факты, до выводов о карте):")
                    for ln in diag_lines:
                        print(ln)
            except DirectError:
                suspect = False
            if suspect:
                print("\nОСТАНОВ: похоже, окно памяти занято драйвером моста,")
                print("  поэтому вывод «карта не отвечает» делать нельзя -")
                print("  набор до карты не дошёл. Что сделать (одна строка):")
                print("    sudo modprobe -r yenta_socket")
                print("  и сразу повторить эту команду. Если после выгрузки")
                print("  паспорт прочитался - вопрос был в драйвере, а не в карте.")
                print("  Вернуть драйвер обратно: sudo modprobe yenta_socket")
                return 3
            if blank in ("ff", "00"):
                off_c, tp_c, inf_c = None, [], {}
                try:
                    dev_c, exca_c, wins_c, _bvc, _bdc = _bridge_ctx()
                    win_c = choose_window(wins_c, 0)
                    mem_c, _src_c, _err_c = open_best_mem(dev_c)
                    try:
                        off_c, tp_c, inf_c = cis_in_common_mem(
                            mem_c, exca_c, win_c, dev_id=dev_c)
                    finally:
                        mem_c.close()
                except DirectError:
                    tp_c = []
                if tp_c:
                    kind, ktext = card_kind(inf_c, tp_c, at_off=off_c)
                    print(f"\nНайдена карта типа «{kind}»: {ktext}")
                    if inf_c.get("product"):
                        print("  продукт (VERS_1)      : "
                              + " / ".join(inf_c["product"][:4]))
                    if inf_c.get("manfid"):
                        print(f"  производитель (MANFID): "
                              f"0x{inf_c['manfid'][0]:04x}, "
                              f"карта 0x{inf_c['manfid'][1]:04x}")
                    print("  Это не linear flash: attribute memory у неё пуста,")
                    print("  и это нормально. Образ снимается тем же чтением:")
                    print("    sudo python3 windows/legacy/pcmcia_direct.py --read card.bin --size auto")
                    print("  ВНИМАНИЕ: для ATA-карты образ - это дамп всего")
                    print("  диска; файловая система внутри (FAT) этим набором")
                    print("  НЕ разбирается.")
                    return 0
            if blank == "ff":
                print("\nattribute memory = сплошные 0xFF: карта НЕ отвечает.")
                print("  Что делать по порядку:")
                print("    1) вынуть и вставить карту до щелчка (контакт не дожат);")
                print("    2) протереть контакты ластиком, без спирта и абразива;")
                print("    3) проверить, что карта сидит в верхнем слоте PC Card,")
                print("       а не вставлена на половину;")
                print("    4) вставить ДРУГУЮ 16-битную карту: если и она даёт 0xFF —")
                print("       вопрос к слоту/мосту, а не к карте.")
            elif blank == "00":
                print("\nattribute memory = сплошные 0x00: ответа нет,")
                print("  но шина при этом «прижата» к нулю, а НЕ к 0xFF.")
                print("    1) драйвер моста занял окно (yenta_socket): выгрузить")
                print("       его и повторить - modprobe -r yenta_socket;")
                print("    2) карта не дожата в слоте: вынуть и вставить до щелчка;")
                print("       паспорт (CIS) есть у ЛЮБОЙ живой карты, даже стёртой;")
                print("    3) контакты карты протереть ластиком, без спирта;")
                print("    4) вставить другую 16-битную карту: если и она даёт 0x00 -")
                print("       вопрос к слоту/мосту, а не к карте.")
            else:
                print("\nВ attribute memory нет цепочки кортежей, и содержимое не")
                print("  однородное — карта что-то отдаёт, но не паспорт.")
            return 3
        print(f"\nКортежи CIS ({len(tuples)}):")
        for t in tuples:
            body = t["body"][:8].hex() if "body" in t else ""
            extra = " (обрезан)" if t.get("truncated") else ""
            print(f"  {t['name']:<20} код 0x{t['code']:02x}, "
                  f"длина {t['len']:2d}, данные: {body}{extra}")
        print("\nПаспорт карты:")
        if info["manfid"]:
            print(f"  производитель (MANFID): 0x{info['manfid'][0]:04x}, "
                  f"карта 0x{info['manfid'][1]:04x}")
        if info["product"]:
            print(f"  продукт (VERS_1)      : {' / '.join(info['product'][:4])}")
        if info["func"]:
            fname = {0: "multi", 1: "memory", 2: "serial", 3: "parallel",
                     4: "fixed disk", 5: "video", 6: "network", 7: "AIMS"}.get(
                info["func"], "?")
            print(f"  функция (FUNCID)      : {info['func']} ({fname})")
        if info["jedec"]:
            print(f"  JEDEC ID              : {info['jedec']}")
        for d in info["devices"]:
            mb = d["size"] / 1024 / 1024
            print(f"  устройство из DEVICE  : {d['type_name']}, "
                  f"{d['size']} байт ({mb:.1f} МиБ), доступ {d.get('speed', '?')}"
                  + (", есть переключатель защиты от записи" if d["wp"] else ""))
        kind, ktext = card_kind(info, tuples)
        print(f"  вид карты             : {kind} — {ktext}")
        for lvl, txt in cis_notes(info, tuples):
            mark = "ВНИМАНИЕ" if lvl == "warn" else "примечание"
            print(f"  {mark:<21}: {txt}")
        v, vwhy = vpp_need(info)
        if v == 5:
            print(f"  напряжение записи     : 5 В (12 В не нужно) — {vwhy}")
        elif v == 12:
            print(f"  напряжение записи     : 12 В (Vpp) — {vwhy}")
        else:
            print(f"  напряжение записи     : неизвестно — {vwhy}")
        if kind in ("serial", "parallel", "network"):
            print("  (это не носитель: паспорт читается, образ не нужен -")
            print("   флеш-образ с такой карты не снимается)")
        return 0

    try:
        mem = Mem()
    except DirectError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 2

    try:
        if args.addr:
            addr = int(args.addr, 0)
            length = int(args.len, 0)
            if length <= 0 or length > 64 * 1024 * 1024:
                print("ОШИБКА: --len должен быть от 1 до 64 МиБ", file=sys.stderr)
                return 2
            data = mem.read(addr, length)
            data, width = normalize_bankwidth(data)
            info = summarize(data)
            if args.json:
                info.update({"addr": addr, "width": width})
                print(json.dumps(info, ensure_ascii=False, indent=2))
                return 0
            print(f"Прочитано {info['bytes']} байт с {addr:#x} "
                  f"(ширина шины {width}):")
            print(f"  разных значений : {info['unique_values']}")
            print(f"  маркеры CIS     : "
                  f"{', '.join(hex(x) for x in info['cis_marks']) or 'нет'}")
            print(f"  строки          : {', '.join(info['strings'][:8]) or 'нет'}")
            print(f"  вывод           : {info['verdict']}")
            if args.hex:
                for off in range(0, min(len(data), 4096), 16):
                    row = data[off:off + 16]
                    txt = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
                    print(f"  {addr + off:08x}  {' '.join(f'{b:02x}' for b in row)}"
                          f"  |{txt}|")
            return 0 if not (info["empty_ff"] and info["unique_values"] == 1) else 3

        if args.identify_chip:
            return _identify_chip_main(args)

        if args.write_probe or args.write:
            return _flash_main(args)

        if args.read:
            dev, exca, wins, _bv, _bd = _bridge_ctx()
            size = parse_size(args.size)
            win = choose_window(wins, size or 0)
            if size is not None and size > win["size"]:
                raise DirectError(f"просили {size / 1024 / 1024:.0f} МиБ, а окно "
                                  f"моста всего {win['size'] / 1024 / 1024:.0f} МиБ")
            mem2, src, _err = open_best_mem(dev)
            try:
                if size is None:
                    expect, why = (None, "")
                    try:
                        _raw, _tp, _inf = read_cis(mem=None, verbose=False)
                        expect, why = cis_card_size(_inf)
                        if expect:
                            print(f"Ёмкость по паспорту: {expect} байт "
                                  f"({expect / 1024 / 1024:.0f} МиБ) — {why}")
                    except Exception:
                        expect, why = None, ""
                    size, how = find_card_size(mem2, exca, win, dev_id=dev,
                                               verbose=True, expect=expect)
                    print(f"Размер карты определён: {size} байт "
                          f"({size / 1024 / 1024:.1f} МиБ) — {how}")
                    if expect and size > expect:
                        print(f"  ВНИМАНИЕ: это больше паспортной ёмкости "
                              f"({expect} байт). Так бывает, когда в паспорте ")
                        print(f"  ошибка; тогда укажите размер вручную: "
                              f"--size {expect}")
                else:
                    how = f"размер задан вручную ({args.size})"
                passes = 2 if args.double else 1
                _span = parse_size(args.span) if args.span else None
                print(f"Чтение образа: {size} байт через окно {win['start']:#x}, "
                      f"кусками {window_span(win, _span) // 1024} КиБ, "
                      f"заходов {passes} (двойное чтение: ")
                print(f"{'да' if args.double else 'нет'})")
                print("  первая секунда уходит на настройку окна - это нормально")
                t0 = time.time()
                pace = {"pass": 0, "t": time.time(), "bytes": 0}

                def show(done, allb, pass_no=1):
                    if pass_no != pace["pass"]:
                        pace.update({"pass": pass_no, "t": time.time(), "bytes": 0})
                        if pass_no > 1:
                            sys.stderr.write(f"\r  заход {pass_no} из 2"
                                             f" (сверка)                  \n")
                    el = max(time.time() - pace["t"], 1e-6)
                    sp = (done - pace["bytes"]) / el / 1024 / 1024
                    sp = max(sp, 0.001)
                    pace["bytes"] = done
                    pct = int(done * 100 / allb) if allb else 100
                    rest = (allb - done) / (sp * 1024 * 1024)
                    sys.stderr.write(f"\r  {pct:3d}%  {done / 1024 / 1024:6.2f} из "
                                     f"{allb / 1024 / 1024:.0f} МиБ  {sp:5.2f} МиБ/с  "
                                     f"осталось ~{rest:4.0f} с ")
                    sys.stderr.flush()

                try:
                    _dl, suspect_read = window_access_diag(dev, exca, win)
                    for _ln in _dl:
                        print(_ln)
                except DirectError:
                    suspect_read = False
                if suspect_read:
                    raise DirectError(
                        "окно памяти занято драйвером моста (или выключено): "
                        "сначала sudo modprobe -r yenta_socket, потом чтение. "
                        "Образ НЕ снимался, файл не создавался.")
                ok_win, why_win = card_window_check(mem2, exca, win, dev_id=dev)
                print(f"  окно: {why_win}")
                if not ok_win:
                    raise DirectError(
                        "окно памяти не даёт стабильного ответа: " + why_win +
                        ". Сначала контакты карты и слота (diag), потом чтение.")
                try:
                    head4k = _read_page(mem2, exca, win, 0, PROBE_LEN, dev_id=dev)
                    if _lb(head4k, 0xFF):
                        print("  в начале карты 0xFF (стёртая флеш)")
                    elif _lb(head4k, 0x00):
                        print("  в начале карты 0x00 (шина прижата)")
                    else:
                        print("  в начале карты есть данные (не 0xFF и не 0x00)")
                except DirectError:
                    pass
                res = read_card_image(mem2, exca, win, args.read, size,
                                      double=args.double, progress=show,
                                      dev_id=dev, verbose=True,
                                      span=parse_size(args.span) if args.span else None)
                sys.stderr.write("\n")
                dt = time.time() - t0
                print(f"Готово: {args.read} — {res['size']} байт, "
                      f"md5 {res['md5']}, {dt:.1f} c")
                rep = image_report(res, image_stats(args.read), win, how)
                if expect:
                    rep += trim_hint(args.read, expect)
                print("\n".join(rep))
                print("Дальше: посмотреть образ без карты — "
                      f"python3 pcmcia_gui.py --map-file {args.read}")
                return 0
            finally:
                mem2.close()

        mem_rep = probe(win_list, verbose=True)
        if not mem_rep["readable"]:
            return 2
        any_data = any(not s["blank_ff"] for s in mem_rep["samples"] if s.get("width"))
        for smp in mem_rep["samples"]:
            if smp.get("error"):
                continue
            data = bytes.fromhex(smp["hex"]) if smp["hex"] else b""
            found = strings(data) if data else []
            if found:
                print(f"  окно {smp['start']:#010x}, строки: {', '.join(found[:6])}")
        print("\nСледующий шаг: снять образ целиком — "
              "sudo python3 windows/legacy/pcmcia_direct.py --read card.bin --double")
        return 0 if any_data else 3
    except DirectError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 2
    finally:
        mem.close()


def hashlib_md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(blk)
    return h.hexdigest()


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main"]
