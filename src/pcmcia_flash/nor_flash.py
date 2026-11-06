#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Классы NOR-флеша: шина, чтение/запись, стирание, статус.

Вынесены из pcmcia_direct.py. Каждый класс — своя зона ответственности:

* NorBus — окно моста, отображённое целиком, байтовый доступ к карте;
* NorFlashIO — шина, разблокировка, сброс, bypass, чтение;
* NorFlashStatus — статус, идентификатор, защита секторов, приостановка стирания;
* NorFlashProgramming — побайтовая, слово, страница, буферная запись;
* NorFlashErase — стирание сектора/чипа, ожидание готовности;
* NorFlash — композиция всех выше.
"""

from .addressing import chip_abs, chip_local, chip_of
from .constants import (
    AMD_BUFFER_CONFIRM, AMD_BUFFER_SETUP, AMD_READ_CFI, AMD_READ_ID, AMD_RESET,
    DQ5, DQ7, FLASH_POLLS, INTEL_BUFFER_CONFIRM, INTEL_BUFFER_SETUP,
    INTEL_BUFFER_SIZE,
    INTEL_CUI_CLEAR_SR, INTEL_CUI_ERASE_CONFIRM, INTEL_CUI_ERASE_SETUP,
    INTEL_CUI_READ_ID, INTEL_CUI_READ_SR, INTEL_CUI_RESET, INTEL_CUI_SUSPEND,
    INTEL_CUI_WRITE_SETUP, INTEL_READ_CFI, INTEL_READ_ID, INTEL_RESET,
    NOR_BYPASS, NOR_CMD_ADDR1, NOR_CMD_ADDR2, NOR_ERASE_CHIP, NOR_ERASE_RESUME,
    NOR_ERASE_SECTOR, NOR_ERASE_SETUP, NOR_ERASE_SUSPEND, NOR_PROGRAM, NOR_RESET,
    NOR_SECTOR_PROTECT,
    NOR_UNLOCK1, NOR_UNLOCK2, SR_ERASE_FAIL, SR_PROTECT, SR_READY, SR_VPP_LOW,
    SR_WRITE_FAIL,
    SECTOR,
)
from .errors import DirectError
from .mem import mem_for_addr, map_card_window

from typing import Optional


class NorBus:
    """Окно моста, отображённое целиком, и доступ к карте по байтам.

    Почему не через Mem.read/write на каждый байт: там mmap+munmap на каждый
    вызов, а программирование байта - это 3 записи и 2-3 чтения статуса.
    На 700 тысяч байт это миллионы системных вызовов. Здесь отображение
    делается один раз на участок, и дальше это обычные обращения к памяти
    (область некэшируемая, поэтому каждое чтение реально уходит в карту -
    иначе опрос DQ7 не работал бы).
    """

    def __init__(self, mem, exca, win, span=None, dev_id=None, writable=True,
                 limit=0):
        """limit - ёмкость карты в байтах (0 = неизвестна).

        Зачем: окно ставят на размер захода, и оно легко выходит за конец
        карты. За концом карты шина отдаёт не данные, а 0x00/0xFF - и опрос
        DQ7 «готово» на такой ячейке не наступит никогда. Нашлось на модели
        при записи в последний сектор.
        """
        self.mem = mem
        self.exca = exca
        self.win = win
        self.dev_id = dev_id
        self.writable = writable
        self.limit = int(limit or 0)
        self.span = max(4096, min(span or win["size"], win["size"]))
        self._m = None
        self._base = 0            # смещение на карте, с которого отображено
        self._len = 0
        self._off = 0             # сдвиг внутри mmap (адрес окна не всегда
        self._maps = 0            # кратен странице - на всякий случай держим)

    def _close_map(self):
        if self._m is not None:
            try:
                self._m.close()
            except (OSError, ValueError, BufferError):
                pass
        self._m = None
        self._len = 0

    def ensure(self, card_off, n=1):
        """Гарантирует, что байты [card_off, card_off+n) отображены."""
        if self._m is not None and self._base <= card_off and \
                card_off + n <= self._base + self._len:
            return
        self._close_map()
        base = card_off - (card_off % 4096)
        length = min(self.span, self.win["size"])
        if self.limit:
            # не залезать за конец карты: там не данные, а пустая шина
            room = max(4096, self.limit - base)
            length = min(length, room)
        if length < n + (card_off - base):
            length = ((card_off - base) + n + 4096 - 1) // 4096 * 4096
        regs = mem_for_addr(self.mem, self.exca, writable=True, dev_id=self.dev_id)
        map_card_window(regs, self.exca, self.win, card_offset=base,
                        attrib=False, size=length)
        m, off = self.mem._map(self.win["start"], length, write=self.writable)
        self._m, self._off, self._base, self._len = m, off, base, length
        self._maps += 1

    def rd(self, card_off):
        self.ensure(card_off, 1)
        return self._m[self._off + card_off - self._base]

    def wr(self, card_off, value):
        self.ensure(card_off, 1)
        self._m[self._off + card_off - self._base] = value & 0xff

    def wr_many(self, card_off, data):
        """Записать несколько соседних байт одной командой (Intel: слово)."""
        self.ensure(card_off, len(data))
        start = self._off + card_off - self._base
        self._m[start:start + len(data)] = bytes(data)

    def rd_block(self, card_off, n):
        out = bytearray()
        got = 0
        while got < n:
            self.ensure(card_off + got, 1)
            take = min(n - got, self._base + self._len - (card_off + got))
            if take <= 0:
                # Окно не покрывает адрес (он за концом карты). Прежде здесь
                # стоял continue - то есть бесконечный цикл вместо ошибки.
                raise DirectError(
                    f"адрес {card_off + got:#x} за пределами карты "
                    f"(окно {self._base:#x}+{self._len:#x})")
            start = self._off + card_off + got - self._base
            out += self._m[start:start + take]
            got += take
        return bytes(out)

    def close(self):
        self._close_map()


class NorFlashIO:
    """Низуровневая работа с шиной: чтение, запись, разблокировка, сброс, bypass."""

    def __init__(self, bus, chips=2, sector=SECTOR, verbose=False, polls=None,
                 cmd=(NOR_CMD_ADDR1, NOR_CMD_ADDR2), wide=False, page=1,
                 bypass=False, strata=False, name="", protocol="jedec",
                 sector_map=None):
        self.bus = bus
        self.chips = max(int(chips or 1), 1)
        # sector здесь - В АДРЕСАХ КАРТЫ (уже умножен на число чипов).
        self.sector = int(sector or SECTOR)
        # sector_map - карта секторов ОДНОГО чипа: [(размер, число), ...] от
        # младших адресов. Пусто - карта однородная (см. span()).
        self.sector_map = [(int(size), int(count))
                           for size, count in (sector_map or [])]
        self.protocol = protocol or "jedec"
        self.strata = bool(strata)                  # AMD/Fujitsu StrataFlash 25h/D0h
        self.verbose = verbose
        self.polls = polls or FLASH_POLLS
        self.cmd1, self.cmd2 = cmd
        self.wide = bool(wide)          # команда словом (Intel 28F): байт -> 2 байта
        self.page = max(int(page or 1), 1)          # байт на чип
        # В адресах карты страница занимает page * chips байт: 32 байта буфера
        # микросхемы - это 32 байта В КАЖДОМ чипе, то есть 64 байта карты.
        self.page_card = self.page * self.chips
        self.bypass = bool(bypass)                  # Unlock Bypass 20h есть?
        self._bypass_active = False                 # сеанс bypass открыт?
        self._bypass_broken = False                 # bypass не работает?
        self._erase_suspended = False               # стирание приостановлено?
        self.name = name

    # --- адреса команд -----------------------------------------------------

    def cmd_addr(self, local):
        """Абсолютный адрес для адреса команды local внутри чипа."""
        return chip_abs(local, 0, self.chips)

    def _send(self, local, value):
        """Послать команду value по адресу команды local (внутри чипа 0).

        У Intel 28F команда - слово: те же два байта пишутся ДВАЖДЫ (в оба
        байта 16-битной шины), иначе микросхема примет это за данные. Это
        тонкость, на которой легко ошибиться: 0x5555 и 0x555 - разные адреса.
        """
        data = bytes([value, value]) if self.wide else bytes([value])
        self.bus.wr_many(self.cmd_addr(local), data)

    # --- чтение массива ----------------------------------------------------

    def read(self, card_off, n=1):
        return self.bus.rd_block(card_off, n)

    def read_byte(self, card_off):
        return self.bus.rd(card_off)

    def span(self, card_off):
        """Границы сектора карты, содержащего адрес: (начало, размер).

        Для boot-микросхем это единственный правильный способ: секторы там
        разного размера (16K + 8K + 8K + 32K + N x 64K), и «округление по
        64 КиБ» стёрло бы не то.
        """
        if not self.sector_map:
            base = card_off - (card_off % self.sector)
            return base, self.sector
        off = 0
        for size, count in self.sector_map:
            size *= self.chips                     # в адресах карты
            if card_off < off + size * count:
                return off + ((card_off - off) // size) * size, size
            off += size * count
        size = self.sector_map[-1][0] * self.chips
        return max(off - size, 0), size

    def spans(self, length):
        """Секторы карты, покрывающие первые length байт: [(начало, размер)]."""
        if not self.sector_map:
            return [(off, min(self.sector, length - off))
                    for off in range(0, length, self.sector)]
        out = []
        off = 0
        for size, count in self.sector_map:
            size *= self.chips
            for _ in range(count):
                if off >= length:
                    return out
                out.append((off, min(size, length - off)))
                off += size
        return out

    # --- команды -----------------------------------------------------------

    def unlock(self):
        """JEDEC: AA/55. Intel 28F: разблокировки нет, команда одна."""
        if self.protocol != "jedec":
            return
        self._send(self.cmd1, NOR_UNLOCK1)
        self._send(self.cmd2, NOR_UNLOCK2)

    def reset(self):
        """Вернуть чипы в режим чтения массива."""
        if self.protocol == "intel":
            for chip in range(self.chips):
                try:
                    self.bus.wr_many(chip_abs(0, chip, self.chips),
                                     bytes([INTEL_CUI_RESET, INTEL_CUI_RESET]))
                except (DirectError, OSError, ValueError):
                    pass
            return
        cmd = NOR_RESET
        for chip in range(self.chips):
            try:
                self.bus.wr_many(chip_abs(0, chip, self.chips), bytes([cmd]))
            except (DirectError, OSError, ValueError):
                pass
        self._bypass_active = False
        self._erase_suspended = False

    def enter_bypass(self):
        """Войти в режим Unlock Bypass (команда 20h).

        После трёх циклов AA/555h, 55/2AAh, 20h/555h микросхема принимает
        программирование за два цикла (A0h → PA/PD) вместо четырёх. Режим
        действует до сброса (exit_bypass() или F0h). Работает только у чипов,
        у которых в GEOMETRY bypass=True (Am29F016/032/040/080, MBM29F016).
        """
        if self.protocol != "jedec" or not self.bypass:
            return
        self.unlock()
        self._send(self.cmd1, NOR_BYPASS)
        self._bypass_active = True

    def exit_bypass(self):
        """Выйти из режима Unlock Bypass: сброс чипов в чтение массива.

        Вызывается безусловно в конце записи, чтобы следующая операция
        (чтение, стирание, другая запись) начиналась из чистого состояния.
        """
        self.reset()
        self._bypass_active = False


class NorFlashStatus:
    """Статус, идентификатор, защита секторов, приостановка стирания."""

    def read_sector_protection(self):
        """Чтение состояния защиты групп секторов (команда A6h).

        Am29F016D и новее: 8 групп по 4 сектора (32 сектора всего).
        После разблокировки и команды A6h чтение по адресу группы возвращает
        DQ0=1, если группа защищена.

        Возвращает dict: номер группы -> True (защищена) / False (не защищена),
        либо None если команда не поддерживается микросхемой.
        """
        if self.protocol != "jedec":
            return None
        # Sector Group Protect работает только у микросхем с CFI (D-ревизия и новее)
        # У старых Am29F016/016B команды A6h нет — возвращаем None.
        self.unlock()
        self._send(self.cmd1, NOR_SECTOR_PROTECT)
        # Читаем состояние всех 8 групп: адрес группы смещён на 6 бит
        # (A6 кодирует номер группы: группа 0 = адрес 0x000, группа 1 = 0x040, ...)
        groups = {}
        for grp in range(8):
            addr = grp << 6   # бит A6 = номер группы
            try:
                val = self.read_byte(chip_abs(addr, 0, self.chips))
                groups[grp] = bool(val & 0x01)
            except (DirectError, OSError, ValueError):
                groups[grp] = None
        self.reset()
        return groups

    def erase_suspend(self):
        """Приостановка стирания (команда B0h).

        Позволяет приостановить стирание сектора, чтобы прочитать или записать
        данные в ДРУГОЙ сектор, а затем продолжить (erase_resume,
        команда 30h). Пауза не ограничена 20 мкс - ограничен только отклик на
        саму команду B0h.

        Возвращает True если команда принята, False если не поддерживается.
        """
        if self.protocol != "jedec":
            # B0h/30h - команды JEDEC-набора AMD/Fujitsu; у Intel 28F
            # приостановки стирания нет. К Unlock Bypass (20h) это отношения
            # не имеет: прежнее условие «только при bypass» было неверным.
            return False
        self._send(self.cmd1, NOR_ERASE_SUSPEND)
        self._erase_suspended = True
        return True

    def erase_resume(self):
        """Продолжение стирания после приостановки (команда 30h).

        Возвращает True если команда принята.
        """
        if self.protocol != "jedec":
            return False
        self._send(self.cmd1, NOR_ERASE_RESUME)
        self._erase_suspended = False
        return True

    def clear_status(self):
        """Intel: сброс статусного регистра (биты 3,4,5 ставятся навсегда)."""
        if self.protocol != "intel":
            return
        for chip in range(self.chips):
            try:
                self.bus.wr_many(chip_abs(0, chip, self.chips),
                                 bytes([INTEL_CUI_CLEAR_SR, INTEL_CUI_CLEAR_SR]))
            except (DirectError, OSError, ValueError):
                pass

    def read_status(self, card_off=0):
        """Статус: у JEDEC - DQ7/DQ5 из ячейки, у Intel - команда 70h + чтение."""
        if self.protocol != "intel":
            return self.read_byte(card_off)
        chip = chip_of(card_off, self.chips)
        self.bus.wr_many(chip_abs(0, chip, self.chips),
                         bytes([INTEL_CUI_READ_SR, INTEL_CUI_READ_SR]))
        return self.read_byte(card_off)

    def _status_ok(self, card_off, want_value=None):
        """(готово, объяснение отказа) для текущей операции.

        У Intel важен порядок: сначала WSMS (бит 7) - операция закончена?,
        затем VPPS (бит 3) - «Vpp вне допуска», то есть нет 12 В, затем
        ESS/BWS (биты 5/4) - сама операция не удалась.
        """
        st = self.read_status(card_off)
        if self.protocol != "intel":
            return st, ""
        if not (st & SR_READY):
            return st, ""                      # ещё идёт
        if st & SR_PROTECT:
            # Отказ оставляет CUI в режиме статуса: дальше чтение массива
            # вернуло бы байт статуса. Возвращаем чип в режим массива (FFh).
            self._intel_send(chip_of(card_off, self.chips), INTEL_CUI_RESET)
            return st, ("блок защищён (WPPS=1, block lock). У 28F004S5/008S5/016S5 снять "
                        "программную защиту нельзя: Intel прошивает master "
                        "lock-bit на заводе (erratum #2, 297796-007), нужны "
                        "12 В на RP#, а слот их не даёт")
        if st & SR_VPP_LOW:
            self._intel_send(chip_of(card_off, self.chips), INTEL_CUI_RESET)
            return st, ("нет напряжения программирования (VPPS=1): этой "
                        "микросхеме нужны 12 В на Vpp, а слот их не даёт")
        if st & SR_ERASE_FAIL:
            self._intel_send(chip_of(card_off, self.chips), INTEL_CUI_RESET)
            return st, "микросхема сообщает: стирание не удалось (ESS=1)"
        if st & SR_WRITE_FAIL:
            self._intel_send(chip_of(card_off, self.chips), INTEL_CUI_RESET)
            return st, "микросхема сообщает: запись не удалась (BWS=1)"
        return st, ""

    def read_id(self):
        """JEDEC ID: (производитель, устройство) или (None, None)."""
        if self.protocol == "intel":
            chip = 0
            self.bus.wr_many(chip_abs(0, chip, self.chips),
                             bytes([INTEL_CUI_READ_ID, INTEL_CUI_READ_ID]))
            man = self.read_byte(chip_abs(0, chip, self.chips))
            dev = self.read_byte(chip_abs(1, chip, self.chips))
            self.reset()
            return man, dev
        self.unlock()
        self._send(self.cmd1, AMD_READ_ID)
        man = self.read_byte(chip_abs(0, 0, self.chips))
        dev = self.read_byte(chip_abs(1, 0, self.chips))
        self.reset()
        return man, dev


class NorFlashProgramming:
    """Программирование: побайтовое, слово, страница, буферная запись (25h/D0h)."""

    def program_byte(self, card_off, value):
        """Записать один байт. Возвращает (ok, почему).

        У Intel 28F команда и данные - СЛОВО (2 байта). Поэтому байт там
        пишется парой: нужный байт и 0xFF во втором байте слова (0xFF ничего
        не меняет: NOR умеет только 1 -> 0). См. program_word().

        Если у микросхемы есть Unlock Bypass (bypass=True), запись идёт через
        program_byte_bypass(): два цикла A0h/PA на байт вместо четырёх. Режим
        bypass должен быть включён enter_bypass() перед пакетной записью и
        выключен exit_bypass() после.
        """
        if self.protocol == "intel":
            return self._intel_program(card_off, value)
        if self.wide:
            return self.program_word(card_off, value)
        before = self.read_byte(card_off)
        if before == value:
            return True, ""
        if before != 0xFF:
            return False, f"байт не стёрт ({before:#04x}) — нужна чистка сектора"
        if self.bypass and not self._bypass_broken:
            # Автовход в Unlock Bypass: первая запись открывает сеанс,
            # последующие пишут за 2 цикла без повторной разблокировки.
            # Завершить сеанс нужно exit_bypass() (или reset()).
            if not self._bypass_active:
                self.enter_bypass()
                self._bypass_active = True
            ok, why = self.program_byte_bypass(card_off, value)
            if ok:
                return ok, why
            # Микросхема не приняла Unlock Bypass. Так бывает: JEDEC ID у
            # семейства один (01ADh у Am29F016, B и D), а команда 20h есть
            # только у поздних ревизий. Откатываемся на обычную запись A0h -
            # один раз, с предупреждением, и больше bypass не пробуем.
            self.exit_bypass()
            self._bypass_active = False
            self._bypass_broken = True
            print("  ВНИМАНИЕ: Unlock Bypass (20h) не сработал: " + why
                  + ". Дальше пишу обычной командой A0h - медленнее, но верно.")
        self.unlock()
        self._send(self.cmd1, NOR_PROGRAM)
        self.bus.wr(card_off, value)
        ok, why = self._wait_program(card_off, value)
        if not ok:
            return False, why
        got = self.read_byte(card_off)
        if got != value:
            return False, f"после записи читается {got:#04x} вместо {value:#04x}"
        return True, ""

    def program_byte_bypass(self, card_off, value):
        """Записать один байт через Unlock Bypass (A0h → PA/PD, 2 цикла).

        Предполагается, что enter_bypass() уже вызван - иначе команда A0h
        без предварительных разблокирующих циклов не будет принята.
        Разблокировка внутри не делается, чтобы не тратить лишние циклы
        при пакетной записи: вход один раз, много байт, выход один раз.
        """
        self._send(self.cmd1, NOR_PROGRAM)
        self.bus.wr(card_off, value)
        ok, why = self._wait_program(card_off, value)
        if not ok:
            return False, why
        got = self.read_byte(card_off)
        if got != value:
            return False, f"после записи читается {got:#04x} вместо {value:#04x}"
        return True, ""

    def program_word(self, card_off, value=None):
        """Записать СЛОВО, в котором лежит байт card_off (у Intel - так).

        Второй байт слова не трогаем: он остаётся таким, каким был (обычно
        0xFF после стирания). Нужен ровно один сценарий - «записать этот
        байт», поэтому значение берётся из card_off, а пара читается заранее.
        """
        pair_at = card_off - (card_off % 2)
        before = self.read_byte(card_off)
        value = before if value is None else value
        if before != 0xFF and before != value:
            return False, f"байт не стёрт ({before:#04x}) — нужна чистка сектора"
        data = bytearray([self.read_byte(pair_at), self.read_byte(pair_at + 1)])
        data[card_off - pair_at] = value
        self.unlock()
        self._send(self.cmd1, NOR_PROGRAM)
        self.bus.wr_many(pair_at, bytes(data))
        ok, why = self._wait_program(card_off, value)
        if not ok:
            return False, why
        got = self.read_byte(card_off)
        if got != value:
            return False, f"после записи читается {got:#04x} вместо {value:#04x}"
        return True, ""

    def program_page(self, card_off, data):
        """Записать страницу программирования (до page байт) одной командой.

        data - то, что должно оказаться в карте; 0xFF означает «не трогать»
        (в NOR запись умеет только 1 -> 0, а стёртое состояние и есть 0xFF).
        Если page=1, работает обычная побайтовая запись.
        У Intel 28F/StrataFlash с page > 1 используется буферная запись
        E8h/N/D0h (Intel CUI), у AMD StrataFlash — 25h/D0h (JEDEC).
        """
        if len(data) > self.page_card:
            return False, f"за один раз не больше {self.page_card} байт"
        if self.page <= 1 or len(data) <= 1:
            for i, b in enumerate(data):
                if b == 0xFF:
                    continue
                ok, why = self.program_byte(card_off + i, b)
                if not ok:
                    return False, why
            return True, ""
        if self.protocol == "intel":
            return self._program_page_intel(card_off, data)
        if self.strata:
            return self._program_page_strata_amd(card_off, data)
        return self._program_page_amd(card_off, data)

    def _program_page_amd(self, card_off, data):
        """Страница AMD (Am29F016/032): A0h, данные подряд, последний - пуск.

        Последовательность взята из даташита: после A0h по адресу команды
        записывается первый байт, затем остальные байты страницы идут подряд;
        программирование запускает запись ПОСЛЕДНЕГО байта. Опрос DQ7/DQ5
        начинается только после него, поэтому на страницу выходит одна
        команда и один опрос вместо 32 команд.

        ВАЖНО: последний байт страницы пишется ВСЕГДА, даже если он 0xFF -
        иначе программирование не запустится. 0xFF в NOR ничего не меняет
        (запись умеет только 1 -> 0), поэтому это безопасно.
        """
        start = card_off - (card_off % self.page_card)
        if card_off != start or len(data) != self.page_card:
            # Всегда пишем страницу ЦЕЛИКОМ от её начала: программирование
            # запускает заполнение буфера (последний байт). Иначе пришлось бы
            # надеяться на таймаут, а его длительность у разных партий разная.
            return self.program_page(start, self._pad_page(start, data, card_off))
        for i, b in enumerate(data):
            if b != 0xFF and self.read_byte(start + i) != 0xFF:
                return False, f"байт {start + i:#x} не стёрт"
        self.unlock()
        self._send(self.cmd1, NOR_PROGRAM)
        # Данные уходят ОДНОЙ операцией записи в окно: именно так работает
        # буфер микросхемы, и именно так пуск происходит по последнему байту.
        self.bus.wr_many(start, bytes(data))
        last = start + len(data) - 1
        return self._wait_program(last, data[-1])

    def _pad_page(self, start, data, card_off):
        """Собрать полную страницу: данные образа плюс 0xFF на остальном.

        Нужна потому, что образ может задевать только часть страницы, а
        команда A0h у страничных микросхем ожидает её целиком.
        """
        page = bytearray(b"\xff" * self.page_card)
        for i, b in enumerate(data):
            page[card_off - start + i] = b
        return bytes(page)

    def _program_page_intel(self, card_off, data):
        """Буферная запись Intel CUI: E8h, счётчик N, данные, D0h, опрос SR.

        Последовательность (даташиты Intel 28F/StrataFlash, «Write to Buffer»):
          1. E8h по адресу буфера каждого чипа (Write-to-Buffer, слово)
          2. N = число байт минус один — тоже слово
          3. N+1 слово данных; младший байт - чип 0, старший - чип 1
          4. D0h (Write Confirm) — после него начинается программирование
          5. Опросить статусный регистр (70h) каждого чипа до WSRS=1
          6. Сбросить каждый чип (FFh)

        Раньше здесь были 25h и побайтовые данные: 25h - команда AMD, счётчика
        не было, а байтовые записи CUI не принимает (он ждёт слово).
        """
        if self.page > INTEL_BUFFER_SIZE:
            return False, (f"буфер записи Intel не больше {INTEL_BUFFER_SIZE} байт, "
                           f"а в таблице page={self.page}")
        start = card_off - (card_off % self.page_card)
        if card_off != start or len(data) != self.page_card:
            return self.program_page(start, self._pad_page(start, data, card_off))

        # Проверяем, что все байты страницы стёрты (0xFF) или уже нужного значения
        for i, b in enumerate(data):
            if b != 0xFF and self.read_byte(start + i) != 0xFF:
                return False, f"байт {start + i:#x} не стёрт"

        # 1. E8h Write-to-Buffer — в каждый чип по адресу буфера
        # Команда Intel - СЛОВО, и оно одно на всю пару чипов: писать её
        # «каждому чипу» нельзя - второй такой же байт чип прочтёт как
        # счётчик N (проверено моделью).
        self._intel_send(0, INTEL_BUFFER_SETUP, 0)

        # 2. Счётчик N = число байт минус один, словом (младший байт - N)
        count = self.page - 1
        self.bus.wr_many(chip_abs(0, 0, self.chips), bytes([count, 0])[:max(self.chips, 1)])

        # 3. Данные — словом: чип 0 в младшем байте, чип 1 в старшем
        for i in range(self.page):
            word = bytearray(2)
            for chip in range(self.chips):
                byte_idx = i * self.chips + chip
                word[chip] = data[byte_idx] if byte_idx < len(data) else 0xFF
            self.bus.wr_many(chip_abs(i, 0, self.chips),
                             bytes(word[:max(self.chips, 1)]))

        # 4. D0h Confirm — посылаем в каждый чип
        self._intel_send(0, INTEL_BUFFER_CONFIRM, 0)   # тоже одно слово

        # 4. Ожидание готовности через статусный регистр каждого чипа
        for chip in range(self.chips):
            addr = chip_abs(0, chip, self.chips)
            for _ in range(self.polls):
                st, why = self._status_ok(addr)
                if why:
                    return False, f"чип {chip}, буфер {start:#x}: {why}"
                if st & SR_READY:
                    self._intel_send(chip, INTEL_CUI_RESET)
                    break
            else:
                return False, f"чип {chip}, буфер {start:#x}: не дождались готовности за отведённое время"

        # 5. Проверяем записанные данные
        for i, b in enumerate(data):
            if b == 0xFF:
                continue
            got = self.read_byte(start + i)
            if got != b:
                return False, (f"после буферной записи читается {got:#04x} "
                               f"вместо {b:#04x} по адресу {start + i:#x}")

        return True, ""

    def _program_page_strata_amd(self, card_off, data):
        """AMD/Fujitsu StrataFlash буферная запись (Am29PL/DL/GL, MBM29DL/GL).

        Протокол JEDEC (разблокировка AA/55, опрос DQ7/DQ5), не Intel CUI.
        Последовательность (по даташиту Am29PL/DL/GL):
          1. Разблокировка: AAh@0x555, 55h@0x2AA
          2. 25h@адрес_буфера (Write-to-Buffer Setup)
          3. Данные по байту в адреса буфера каждого чипа
          4. D0h@адрес_буфера (Buffer Write Confirm)
          5. Опрос DQ7/DQ5 до готовности
          6. Сброс F0h

        Каждый чип имеет свой буфер. В адресах карты с 2 чипами:
        карт-байт start+i*chips+chip → чип chip, смещение i внутри чипа.
        page_card = page * chips байт на карте, page байт на чип.
        """
        start = card_off - (card_off % self.page_card)
        if card_off != start or len(data) != self.page_card:
            return self.program_page(start, self._pad_page(start, data, card_off))

        # Проверяем, что все байты страницы стёрты (0xFF) или уже нужного значения
        for i, b in enumerate(data):
            if b != 0xFF and self.read_byte(start + i) != 0xFF:
                return False, f"байт {start + i:#x} не стёрт"

        # Если активно Unlock Bypass — выйти из него перед буферной записью:
        # из состояния bypass последовательность AA/55 не переводит чип в u1/u2,
        # поэтому unlock() не сработает и 25h Setup не будет принят.
        if self._bypass_active:
            self.reset()

        # 1. Разблокировка: AAh@cmd1, 55h@cmd2
        self.unlock()

        # 2. 25h Setup — посылаем в каждый чип по адресу буфера (смещение 0 внутри чипа)
        # Адреса карты: start + chip (byte 0 страницы → чип 0, byte 1 → чип 1, ...)
        # Используем bus.wr напрямую, а не _send, т.к. _send пересчитывает адрес
        # через chip_abs(local, 0, chips) — неверно для произвольных адресов буфера.
        for chip in range(self.chips):
            buf_addr = start + chip
            self.bus.wr(buf_addr, AMD_BUFFER_SETUP)

        # 3. Пишем данные в буфер каждого чипа
        # data лежит в порядке карты: start+0, start+1, start+2, ...
        # chip 0 получает data[0], data[2], data[4]... (чётные)
        # chip 1 получает data[1], data[3], data[5]... (нечётные)
        for i in range(self.page):
            for chip in range(self.chips):
                byte_idx = i * self.chips + chip
                val = data[byte_idx] if byte_idx < len(data) else 0xFF
                addr = start + i * self.chips + chip
                self.bus.wr(addr, val)

        # 4. D0h Confirm — посылаем в каждый чип
        for chip in range(self.chips):
            buf_addr = start + chip
            self.bus.wr(buf_addr, AMD_BUFFER_CONFIRM)

        # 5. Ожидание готовности через DQ7/DQ5 каждого чипа
        for chip in range(self.chips):
            addr = start + chip
            last_val = data[(self.page - 1) * self.chips + chip] if self.page * self.chips <= len(data) else 0xFF
            want = last_val & DQ7
            for _ in range(self.polls):
                st = self.read_byte(addr)
                if st & DQ5:
                    st2 = self.read_byte(addr)
                    if (st2 & DQ7) == want:
                        break
                    return False, f"чип {chip}, буфер {start:#x}: микросхема не приняла данные (DQ5=1, DQ7 не тот)"
                if (st & DQ7) == want:
                    break
            else:
                return False, f"чип {chip}, буфер {start:#x}: не дождались готовности за отведённое время"

        # 6. Сброс F0h — возвращаем чипы в чтение массива
        self.reset()

        # 7. Проверяем записанные данные
        for i, b in enumerate(data):
            if b == 0xFF:
                continue
            got = self.read_byte(start + i)
            if got != b:
                return False, (f"после буферной записи читается {got:#04x} "
                               f"вместо {b:#04x} по адресу {start + i:#x}")

        return True, ""

    # --- Intel 28F: CUI (свой набор команд со статусным регистром) ---------

    def _intel_send(self, chip, op, addr_local=None):
        """Послать команду CUI в конкретный чип: (адрес, данные).

        Адрес и данные у Intel - СЛОВО: 0x2020 при стирании, 0x7070 при
        чтении статуса. Поэтому байтовое смещение удваивается, а данные
        дублируются в оба байта.
        """
        base = chip_abs(addr_local or 0, chip, self.chips)
        # Ровно по байту на чип пары: у карты с ДВУМЯ чипами это два байта
        # (команда доходит до обоих), у карты с ОДНИМ - один байт. Прежнее
        # bytes([op, op]) на одном чипе давало команду, а следом уже данные
        # (40h как данные) - запись уезжала в мусор.
        self.bus.wr_many(base, bytes([op] * max(self.chips, 1)))
        return base

    def _intel_word_base(self, card_off):
        """(адрес начала СЛОВА, номер чипа внутри слова) для байта card_off.

        У Intel всё - слова: один байт слова лежит в чипе 0, второй в чипе 1
        по ТОМУ ЖЕ адресу внутри чипа. В адресах карты это соседние байты:
        2n (чип 0) и 2n+1 (чип 1).
        """
        chip = chip_of(card_off, self.chips)
        return card_off - chip, chip

    def _intel_program(self, card_off, value):
        before = self.read_byte(card_off)
        if before == value:
            return True, ""
        if before != 0xFF:
            return False, f"байт не стёрт ({before:#04x}) — нужна чистка блока"
        pair_at, chip = self._intel_word_base(card_off)
        data = bytearray(self.read_byte(pair_at + c) for c in range(self.chips))
        data[chip] = value
        self._intel_send(chip, INTEL_CUI_WRITE_SETUP)
        self.bus.wr_many(pair_at, bytes(data))
        word_base = chip_abs(0, chip, self.chips)
        for _ in range(self.polls):
            st, why = self._status_ok(word_base)
            if why:
                return False, why
            if st & SR_READY:
                # пока не послана команда FF, чип отдаёт СТАТУС, а не данные
                self._intel_send(chip, INTEL_CUI_RESET)
                got = self.read_byte(card_off)
                if got != value:
                    return False, (f"после записи читается {got:#04x} вместо "
                                   f"{value:#04x} (статус {st:#04x})")
                return True, ""
        return False, "микросхема не сообщила о готовности за отведённое время"

    def _intel_erase(self, card_off):
        """Стирание блока по командам CUI: 20h (setup), затем D0h (confirm)."""
        base, _size = self.span(card_off)
        for chip in range(self.chips):
            local = chip_local(base, self.chips)
            self._intel_send(chip, INTEL_CUI_ERASE_SETUP, local)
            self._intel_send(chip, INTEL_CUI_ERASE_CONFIRM, local)
            addr = chip_abs(0, chip, self.chips)
            for _ in range(self.polls):
                st, why = self._status_ok(addr)
                if why:
                    return False, f"чип {chip}, блок {base:#x}: {why}"
                if st & SR_READY:
                    break
            else:
                return False, (f"чип {chip}, блок {base:#x}: микросхема не "
                               "сообщила о готовности за отведённое время")
            self._intel_send(chip, INTEL_CUI_RESET)
        return True, ""

    def _wait_program(self, card_off, value):
        """Ожидание записи байта по DQ7/DQ5.

        Порядок важен: DQ5 (бит 5) означает «вышли за пределы времени», и пока
        он поднят, значение DQ7 недостоверно. Поэтому сначала проверяем DQ5:
        если он есть - читаем ещё раз и смотрим DQ7 уже без оглядки на DQ5:
        совпал - это была задержка, не совпал - настоящий отказ.
        """
        want = value & DQ7
        for _ in range(self.polls):
            st = self.read_byte(card_off)
            if st & DQ5:
                st2 = self.read_byte(card_off)
                if (st2 & DQ7) == want:
                    return True, ""
                return False, ("микросхема не приняла байт (DQ5=1, DQ7 не тот, "
                               "DQ7 недостоверен): ячейка изношена или запись "
                               "запрещена")
            if (st & DQ7) == want:
                return True, ""
        return False, "микросхема не ответила за отведённое число опросов"


class NorFlashErase:
    """Стирание: сектор, чип, приостановка/возобновление."""

    def erase_sector(self, card_off):
        """Стереть сектор, содержащий card_off. Возвращает (ok, почему).

        ВАЖНО (нашлось на модели, а не на карте): стирать надо В КАЖДОМ чипе
        пары отдельно. Команда 30h действует на тот чип, к которому попал её
        адрес: байт с чётным смещением уходит в чип 0, с нечётным - в чип 1.
        Если послать одну команду по чётному адресу, второй чип не сотрётся,
        и запись в нечётные байты провалится с «байт не стёрт».
        """
        base, _size = self.span(card_off)
        if self.protocol == "intel":
            return self._intel_erase(base)
        # base - АБСОЛЮТНОЕ смещение; чипу нужен адрес ВНУТРИ него, поэтому
        # сначала переводим в байты чипа, и только потом раскладываем по чипам.
        local = chip_local(base, self.chips)
        for chip in range(self.chips):
            addr = chip_abs(local, chip, self.chips)
            self.unlock()
            self._send(self.cmd1, NOR_ERASE_SETUP)
            self.unlock()
            # Шестой цикл - 30h ПО АДРЕСУ СЕКТОРА (даташит, «Sector Erase
            # Command Sequence»; так же делают flashrom: issuecmd(0x30, page)
            # и ядро Linux: map_write(map, cfi->sector_erase_cmd, adr)).
            # Раньше здесь стояло _send(self.cmd1, ...) + wr(addr, 0xFF):
            # команда уходила в 0x555, то есть ВСЕГДА стирала сектор 0.
            self.bus.wr(addr, NOR_ERASE_SECTOR)
            ok, why = self._wait_erase(addr)
            if not ok:
                self.reset()
                return False, f"чип {chip}, сектор {base:#x}: {why}"
        return True, ""

    def erase_chip(self):
        """Стереть чипы целиком. У Intel 28F команды «стереть всё» НЕТ.

        В таблице 9 документа её нет: только пошаговое стирание блоков.
        Поэтому здесь честный отказ, а не «попробуем 10h и посмотрим».
        """
        if self.protocol == "intel":
            return False, ("у Intel 28F008SA нет команды «стереть весь чип»: "
                           "стирать нужно поблочно (это и делает обычная запись)")
        for chip in range(self.chips):
            self.unlock()
            self._send(self.cmd1, NOR_ERASE_SETUP)
            self.unlock()
            self._send(self.cmd1, NOR_ERASE_CHIP)
            ok, why = self._wait_erase(chip_abs(0, chip, self.chips))
            if not ok:
                self.reset()
                return False, f"чип {chip}: {why}"
        return True, ""

    def _wait_erase(self, card_off):
        """Ожидание стирания по DQ7/DQ5.

        Тонкость, на которой легко ошибиться (и я ошибся): у стёртого байта
        значение 0xFF, то есть DQ5 (бит 5) ВСЕГДА поднят. Поэтому требовать
        «DQ7=1 и DQ5=0» на первом же чтении нельзя - у стёртой ячейки это
        никогда не выполнится, и стирание будет объявлено неудачным. По
        даташиту порядок такой: DQ7=1 - стирание закончено, но надо проверить
        DQ5; если DQ5=1, читаем ЕЩЁ РАЗ и смотрим ТОЛЬКО DQ7: он остался 1 -
        всё в порядке, стал 0 - отказ.
        """
        st = 0
        for _ in range(self.polls):
            st = self.read_byte(card_off)
            if not (st & DQ7):
                continue                       # ещё стирается
            if not (st & DQ5):
                return True, ""
            st2 = self.read_byte(card_off)      # DQ5 поднят - проверяем DQ7
            if st2 & DQ7:
                return True, ""
            return False, ("стирание не выполнилось (DQ5=1, при повторном "
                           "чтении DQ7=0): сектор защищён, изношен или карте "
                           "нужно Vpp 12 В")
        return False, (f"стирание не закончилось за отведённое число опросов "
                       f"(последний статус {st:#04x}; DQ7=1 и DQ5=0 означает "
                       "«готово» - значит карта отвечает не так, как ждёт "
                       "даташит: возможно, включён режим CFI или другой чип)")


class NorFlash(NorFlashIO, NorFlashProgramming, NorFlashErase, NorFlashStatus):
    """Команды микросхемы через окно: программирование и стирание.

    Параметры у разных микросхем разные, поэтому всё берётся из geo (см.
    identify_chip): размер сектора, адреса команд 0x555/0x2AA (AMD, Fujitsu)
    или 0x5555/0x2AAA (Intel 28F, где команда - СЛОВО), ширина команды и
    страница программирования. Жёстко зашитых 64 КиБ и 0x555 больше нет,
    кроме значений по умолчанию.

    Сколько чипов в паре (chips) тоже важно: у 8-битной карты он один, и
    тогда адреса команд идут подряд, без «через один байт».

    Состав (каждый класс — своя зона ответственности):

    * NorFlashIO — шина, разблокировка, сброс, bypass, чтение;
    * NorFlashProgramming — побайтовая, слово, страница, буферная запись;
    * NorFlashErase — стирание сектора/чипа, ожидание готовности;
    * NorFlashStatus — статус, ID, защита секторов, приостановка стирания.
    """
    pass


__all__ = [
    "NorBus",
    "NorFlashIO",
    "NorFlashStatus",
    "NorFlashProgramming",
    "NorFlashErase",
    "NorFlash",
]
