# -*- coding: utf-8 -*-
"""Командная строка pcmcia_direct: разбор аргументов и команды.

Запуск: python3 pcmcia_direct.py --probe | --cis | --read ... | --flash ..."""

import argparse
import json
import os
import sys
import time

from .bridge import EXCA_BASE, Mem, PROBE_LEN, _bridge_ctx, _read_page, bridge_windows, choose_window, devmem_hint, find_bridge_ids, looks_blank, open_best_mem, parse_size, read_mem, window_span
from .chips import vpp_need
from .cis import cis_capacity, cis_dump, cis_extra_report, cis_in_common_mem, cis_notes, passport_ok, read_cis
from .errors import DirectError
from .flash import _flash_main, _identify_chip_main
from .image import card_kind, card_window_check, cis_card_size, find_card_size, image_report, image_stats, normalize_bankwidth, read_card_image, strings, summarize, trim_hint, trim_image, window_access_diag
from .version import KIT_VERSION


def probe(win_list, verbose=True):
    """Можно ли читать карту: /dev/mem (или sysfs) + первые байты каждого окна.

    Окно настраивается на common memory карты (attrib=False) - то есть читается
    САМА флеш, а не паспорт. Сплошные 0xFF здесь - нормальный вид стёртой
    флеш-карты, поэтому вывод говорит об этом прямо.
    """
    report = {"windows": win_list, "readable": None, "reason": "", "samples": [],
              "common_memory": True}
    try:
        mem = Mem()
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
        dev, bar0, _bv, _bd = find_bridge_ids()
        if bar0:
            exca = bar0 + EXCA_BASE
    except DirectError:
        dev = None
    report["common_memory"] = exca is not None
    try:
        ok_any = False
        for w in win_list:
            try:
                if exca is not None:
                    sample = _read_page(mem, exca, w, 0, 256, dev_id=dev,
                                        verbose=verbose)
                else:
                    sample = read_mem(mem, w["start"], 256, dev_id=dev,
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
        mem.close()
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
                print("  НИ ОДНО ОКНО НЕ ПРОЧИТАНО — про содержимое карты здесь "
                      "ничего не сказано,")
                print("  и слова про 0xFF к этому случаю не относятся: выше "
                      "причина отказа по каждому окну.")
                print(f"  Подсказка: {report.get('hint') or devmem_hint()}")

        else:
            print(f"Доступ к /dev/mem: нет — {report['reason']}")
    return report


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
    # --version: версия набора. До 2.17.16 ключа не было ни здесь, ни в отчёте,
    # хотя комментарий у KIT_VERSION обещал печать «в отчёте и в --selftest»:
    # --selftest есть у linux/pcmcia_flash.py и подкомандой у пульта, а у самого
    # pcmcia_direct.py его нет.
    ap.add_argument("--version", action="version",
                    version=f"pcmcia_direct.py {KIT_VERSION}"
                            " (набор pcmcia-linear-flash)")
    ap.add_argument("--probe", action="store_true", help="только проверить доступ")
    ap.add_argument("--cis", action="store_true",
                    help="прочитать CIS через attribute memory (паспорт карты)")
    ap.add_argument("--read", metavar="ФАЙЛ", help="снять образ карты в файл")
    ap.add_argument("--double", action="store_true", help="при --read: два чтения + сверка")
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
                    help="в CIS-дампе печатать окно цепочки целиком (без --full "
                         "окно 160 байт; всю attribute memory не печатаем никогда)")
    ap.add_argument("--windows", metavar="ФАЙЛ",
                    help="файл с выводом lspci -v (для проверки без слота)")
    # --- запись без драйвера (2.14.0)
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

    # сводка по готовому образу не требует ни слота, ни root: делаем сразу
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
            # пустая attribute memory: возможно, это ATA-карта
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
            print("  карта не опознана: пришлите вывод pcmcia_direct.py --cis")
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
        # источник памяти выбирается внутри read_cis: /dev/mem, а если ядро
        # запрещает - окна моста через sysfs.
        # при --json печатаем ТОЛЬКО json (его разбирает раздел 7.1)
        try:
            attr, tuples, info = read_cis(None, verbose=not args.json)
            if args.json:
                print(json.dumps({"cis_raw": attr.hex(), "summary": info,
                                  "tuples": [{"code": t["code"], "name": t["name"],
                                              "len": t["len"]} for t in tuples]},
                                 ensure_ascii=False, indent=2))
                return 0
            if not passport_ok(info) and tuples:
                print("\nЦепочка найдена, но в ней нет ни названия (VERS_1),")
                print("  ни MANFID, ни DEVICE - это ещё не паспорт.")
                print("  Скорее всего цепочка начинается раньше/позже и её надо")
                print("  искать с другого смещения. Пришлите первые 64 байта.")
                return 4
            show = cis_dump(attr, info)
            print("\nAttribute memory:")
            for line in show:
                print("  " + line)
            if not tuples:
                blank = info.get("blank")
                # Прежде чем говорить «карта не отвечает», показать факты:
                # включено ли окно, кто его мог занять. Иначе совет «протрите
                # контакты» даётся в случае, когда карта тут вообще ни при чем.
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
                # Если окно выключено или его явно занял драйвер моста, то
                # «карта не отвечает» - НЕВЕРНЫЙ вывод: мы до карты не дошли.
                # Раньше suspect вычислялся и просто печатался. Теперь он
                # останавливает вывод советов про контакты: пусть сначала
                # выгрузят драйвер, иначе человек будет чистить контакты
                # карты, которая в этот момент ни при чем.
                if suspect:
                    print("\nОСТАНОВ: похоже, окно памяти занято драйвером моста,")
                    print("  поэтому вывод «карта не отвечает» делать нельзя -")
                    print("  набор до карты не дошёл. Что сделать (одна строка):")
                    print("    sudo modprobe -r yenta_socket")
                    print("  и сразу повторить эту команду. Если после выгрузки")
                    print("  паспорт прочитался - вопрос был в драйвере, а не в карте.")
                    print("  Вернуть драйвер обратно: sudo modprobe yenta_socket")
                    return 3
                # attribute memory пуста: у ATA-карт паспорт лежит в common
                # memory (0x200/0x400) - проверить это ДО выводов о контактах
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
                        print("    sudo python3 pcmcia_direct.py --read card.bin --size auto")
                        print("  ВНИМАНИЕ: для ATA-карты образ - это дамп всего")
                        print("  диска; файловая система внутри (FAT) этим набором")
                        print("  НЕ разбирается.")
                        return 0
                if blank == "ff":
                    print("\nattribute memory = сплошные 0xFF: карта НЕ отвечает.")
                    print("  Это не «стёртая карта» (у стёртой паспорт читается,")
                    print("  стирается только common memory). Ноль ответов там, где")
                    print("  должен быть паспорт, — это шина без карты на конце.")
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
                    print("  Для пустого слота PC Card это ненормально: без карты")
                    print("  линии обычно отдают 0xFF. Нули значат, что чтение не")
                    print("  доходит до карты. Порядок проверки - от частого к редкому:")
                    print("    1) драйвер моста занял окно (yenta_socket): выгрузить")
                    print("       его и повторить - modprobe -r yenta_socket;")
                    print("    2) карта не дожата в слоте: вынуть и вставить до щелчка;")
                    print("       паспорт (CIS) есть у ЛЮБОЙ живой карты, даже стёртой;")
                    print("    3) контакты карты протереть ластиком, без спирта;")
                    print("    4) вставить другую 16-битную карту: если и она даёт 0x00 -")
                    print("       вопрос к слоту/мосту, а не к карте.")
                elif info.get("stride") == 2 or info.get("jedec"):
                    print("\nПаспорт ВИДЕН: цепочка читается, но обрывается рано.")
                    print("  Признак 16-битной раскладки: байты CIS идут через один,")
                    print("  и в потоке найдены настоящие кортежи (JEDEC_C и др.),")
                    print("  но длина следующего кортежа выходит за прочитанное.")
                    print("  Это НЕ молчащая карта: обмен идёт и данные правильные.")
                    print("  Что делать: запустить с большим окном чтения (--len),")
                    print("  либо прислать первые 64 байта - подберём сдвиг точно.")
                    return 4
                elif info.get("strings"):
                    print("\nПаспорт ВИДЕН, но цепочка кортежей не собралась.")
                    print("  В attribute memory найдены строки:")
                    for x in info["strings"]:
                        print(f"    {x}")
                    print("  Значит карта отвечает и данные читаются - вопрос только")
                    print("  в поиске начала цепочки. Пришлите этот блок и первые")
                    print("  64 байта выше: подберём сдвиг/ширину точно.")
                    return 4
                else:
                    print("\nВ attribute memory нет цепочки кортежей, и содержимое не")
                    print("  однородное — карта что-то отдаёт, но не паспорт. Скорее")
                    print("  всего это не linear flash (модем/сетевая/ATA-карта), либо")
                    print("  окно настроено не туда. Пришлите первые 64 байта выше.")
                print("\nВажно: common memory (режим --probe) тут ничего не решает —")
                print("  у стёртой карты и у молчащей шины она выглядит одинаково.")
                return 3
            print(f"\nКортежи CIS ({len(tuples)}):")
            for t in tuples:
                body = t["body"][:8].hex()
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
                         4: "fixed disk", 5: "video", 6: "network",
                         7: "AIMS"}.get(info["func"], "?")
                print(f"  функция (FUNCID)      : {info['func']} ({fname})")
            if info["jedec"]:
                print(f"  JEDEC ID              : {info['jedec']}")
            for d in info["devices"]:
                mb = d["size"] / 1024 / 1024
                print(f"  устройство из DEVICE  : {d['type_name']}, "
                      f"{d['size']} байт ({mb:.1f} МиБ), доступ {d.get('speed', '?')}"
                      + (", есть переключатель защиты от записи" if d["wp"] else ""))
            for d in info.get("attr_devices") or []:
                # attribute memory - это EEPROM с паспортом (CIS). Если она
                # только для чтения, CIS переписать нельзя даже при 12 В.
                print(f"  паспортная память     : {d['type_name']}, "
                      f"{d['size']} байт"
                      + (", только чтение (ROM)" if d["type"] == 1
                         else ", перезаписываемая (EEPROM)"))
            if info.get("jedec_a"):
                print(f"  JEDEC паспортной EEPROM: {info['jedec_a']}")
            if info.get("geometry"):
                g2 = info["geometry"]
                print(f"  геометрия (DEVICEGEO) : сектор 2^{g2['sector_shift']} "
                      f"= {1 << g2['sector_shift']} байт, карта 2^{g2['card_shift']}, "
                      f"чип 2^{g2['chip_shift']}")
            if info.get("battery"):
                print(f"  батарея               : {info['battery']['kind']}")
            if info.get("vers2"):
                print(f"  версия CIS (VERS_2)   : {info['vers2'][0]}.{info['vers2'][1]}")
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
            elif v == 3:
                # 2.18.2: 3-вольтовой микросхеме Vpp не нужен вовсе, и
                # «неизвестно» здесь было бы неправдой.
                print(f"  напряжение записи     : Vpp не нужна (3 В) — {vwhy}")
            else:
                print(f"  напряжение записи     : неизвестно — {vwhy}")
            if kind in ("serial", "parallel", "network"):
                print("  (это не носитель: паспорт читается, образ не нужен -")
                print("   флеш-образ с такой карты не снимается)")
            for line in cis_capacity(info, info.get("strings")):
                print(f"  {line}")
            prod = " ".join(info["product"] or []).upper()
            if "SERIES2-02" in prod and v != 5:
                print("\n  ВНИМАНИЕ: SERIES2-02 — Intel Series 2 (28F008SA). По"
                      " документации\n  это 5-вольтовая серия, но часть карт разводит"
                      " Vpp на 12 В.\n  Заранее не определить: проверяется пробной"
                      " записью одного сектора.")
            return 0
        except DirectError as e:
            print(f"ОШИБКА: {e}", file=sys.stderr)
            return 2

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
                    # ёмкость из паспорта: она важнее «повторения адресов»,
                    # иначе пустая шина за концом карты даёт 64 МиБ
                    expect, why = (None, "")
                    try:
                        _raw, _tp, _inf = read_cis(mem=None, verbose=False)
                        expect, why = cis_card_size(_inf)
                        if expect:
                            print(f"Ёмкость по паспорту: {expect} байт "
                                  f"({expect / 1024 / 1024:.0f} МиБ) — {why}")
                    except Exception:
                        # паспорт не прочитался - не беда: размер определится
                        # повторением адресов или первым пустым участком
                        expect, why = None, ""
                    size, how = find_card_size(mem2, exca, win, dev_id=dev,
                                               verbose=True, expect=expect)
                    print(f"Размер карты определён: {size} байт "
                          f"({size / 1024 / 1024:.1f} МиБ) — {how}")
                    if expect and size > expect:
                        print(f"  ВНИМАНИЕ: это больше паспортной ёмкости "
                              f"({expect} байт). Так бывает, когда в паспорте "
                              f"ошибка; тогда укажите размер вручную: "
                              f"--size {expect}")
                else:
                    how = f"размер задан вручную ({args.size})"
                # Печатаем ДО первой секунды работы: иначе на медленной карте
                # экран пустой и не отличить «работает» от «висит».
                passes = 2 if args.double else 1
                _span = parse_size(args.span) if args.span else None
                print(f"Чтение образа: {size} байт через окно {win['start']:#x}, "
                      f"кусками {window_span(win, _span) // 1024} КиБ, "
                      f"заходов {passes} (двойное чтение: "
                      f"{'да' if args.double else 'нет'})")
                print("  первая секунда уходит на настройку окна - это нормально")
                t0 = time.time()
                pace = {"pass": 0, "t": time.time(), "bytes": 0}

                def show(done, allb, pass_no=1):
                    # каждый заход считается отдельно: иначе на втором проходе
                    # скорость и остаток показываются от начала первого
                    if pass_no != pace["pass"]:
                        pace.update({"pass": pass_no, "t": time.time(),
                                     "bytes": 0})
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

                # тот же стоп-кран, что и в --cis: если окно выключено или его
                # занял драйвер, чтение даст мусор, а не карту
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
                # что в начале карты: если там не 0xFF/0x00, значит на карте
                # есть данные, и образ снимать было смысл
                try:
                    head4k = _read_page(mem2, exca, win, 0, PROBE_LEN, dev_id=dev)
                    if looks_blank(head4k, 0xFF):
                        print("  в начале карты 0xFF (стёртая флеш)")
                    elif looks_blank(head4k, 0x00):
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

        # по умолчанию: окна + первые байты каждого окна
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
              "sudo python3 pcmcia_direct.py --read card.bin --double")
        return 0 if any_data else 3
    except DirectError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 2
    finally:
        mem.close()


def hashlib_md5(path):
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(blk)
    return h.hexdigest()
