#!/usr/bin/env python3
"""
pcmcia_direct.py — прямой доступ к 16-битной PCMCIA linear flash карте через /dev/mem.

Зачем: когда драйвер PCMCIA не может отдать карте окно памяти, ядро пишет
    cs: warning: no high memory space available!
и карта не появляется ни как /dev/mtd0, ни как устройство в /sys/bus/pcmcia.
mtd-utils, force_size, vpp в этом случае бесполезны — до них дело не доходит.

Но окна CardBus-моста при этом уже назначены ядром:
    pci 0000:00:0c.0: CardBus bridge window 1 [mem 0x64000000-0x67ffffff pref]: assigned
Эти адреса физические. Если открыть /dev/mem и отобразить их через mmap,
карта читается напрямую, без участия подсистемы PCMCIA. Именно так читают CIS
на таких ноутбуках под DOS/Linux-утилитами.

Ограничения (важно понимать до запуска):
  * только ЧТЕНИЕ. Запись сюда сделана бы не будет: без драйвера некому
    выставить Vpp (напряжение программирования) и соблюсти тайминги команд
    микросхемы. Запись «на глазок» портит карту.
  * нужен root и /dev/mem с разрешением на эти адреса: CONFIG_STRICT_DEVMEM=N
    (или ядро с iomem=relaxed). Иначе mmap вернёт EPERM/EACCES.
  * если карта читает 8-битные данные на 16-битной шине, байты будут
    дублироваться (0xAA 0xAA); нормализация есть в пакете.

Запуск:
    sudo python3 pcmcia_direct.py --probe             # доступно ли вообще
    sudo python3 pcmcia_direct.py                     # окна + CIS + первые 256 Б
    sudo python3 pcmcia_direct.py --read card.bin --double   # образ + сверка
    sudo python3 pcmcia_direct.py --image-report card.bin    # сводка по образу
    sudo python3 pcmcia_direct.py --addr 0x64000000 --len 4096 --hex
    sudo python3 pcmcia_direct.py --probe --json      # машинный вывод

Коды возврата: 0 — успех, 2 — нет доступа (не root / STRICT_DEVMEM / нет окон),
3 — карта не отвечает (все байты 0xFF или все 0x00).

Этот файл — только точка входа. Логика разложена по модулям пакета
src/pcmcia_direct/ (с 2.18.0 монолита windows/legacy/pcmcia_direct.py нет):

    version.py        версия набора
    errors.py         исключение DirectError
    nor_commands.py   коды команд NOR: JEDEC/AMD, Intel CUI, статусный регистр
    bridge.py         окна CardBus через sysfs, /dev/mem, mmap, чтение памяти
    chips_data.py     таблицы микросхем с ссылками на даташиты
    chips.py          имена, JEDEC/CFI-определение, карта секторов
    cis.py            паспорт карты (CIS)
    power.py          питание сокета и Vpp (EXCA, TPS2220B)
    image.py          образ карты: чтение, размер, обрезка, сводка
    nor.py            команды микросхемы на модели шины
    flash.py          проба, чтение, запись, стирание, журнал
    cli.py            командная строка

Плоский API сохранён: collect_all.py и pcmcia_ctl.py запускают этот файл так же,
как раньше, а python3 -m pcmcia_direct делает то же самое из каталога src.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _bootstrap():
    """Поставить рядом лежащий src/ впереди sys.path и найти пакет."""
    for cand in (os.path.join(HERE, "src"), HERE):
        if os.path.isdir(os.path.join(cand, "pcmcia_direct")):
            if cand not in sys.path:
                sys.path.insert(0, cand)
            return cand
    sys.exit("pcmcia_direct: рядом нет каталога src/pcmcia_direct — "
             "набор скопирован не целиком")


def main(argv=None):
    _bootstrap()
    from pcmcia_direct.cli import main as _main
    return _main(argv)


if __name__ == "__main__":
    sys.exit(main())
