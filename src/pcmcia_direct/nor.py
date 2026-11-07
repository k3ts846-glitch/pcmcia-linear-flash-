# -*- coding: utf-8 -*-
"""Команды NOR-флеша на модели шины: чтение, запись, стирание, статус.

NorFlash собран из миксинов: IO, Status, Programming, Erase."""

from .bridge import PAGE, map_card_window, mem_for_addr
from .chips import chip_abs, chip_local, chip_of, sector_map
from .errors import DirectError
from .nor_commands import AMD_BUFFER_CONFIRM, AMD_BUFFER_SETUP, AMD_READ_ID, DQ5, DQ7, FLASH_POLLS, INTEL_BUFFER_CONFIRM, INTEL_BUFFER_SETUP, INTEL_BUFFER_SIZE, INTEL_CUI_CLEAR_SR, INTEL_CUI_ERASE_CONFIRM, INTEL_CUI_ERASE_SETUP, INTEL_CUI_READ_ID, INTEL_CUI_READ_SR, INTEL_CUI_RESET, INTEL_CUI_SUSPEND, INTEL_CUI_WRITE_SETUP, NOR_BYPASS, NOR_CMD_ADDR1, NOR_CMD_ADDR2, NOR_ERASE_CHIP, NOR_ERASE_RESUME, NOR_ERASE_SECTOR, NOR_ERASE_SETUP, NOR_ERASE_SUSPEND, NOR_PROGRAM, NOR_RESET, NOR_UNLOCK1, NOR_UNLOCK2, SECTOR, SR_ERASE_FAIL, SR_PROTECT, SR_READY, SR_SUSPEND, SR_VPP_LOW, SR_WRITE_FAIL


def _restore_sector(flash, off, was):
    """Вернуть в сектор прежние байты. Возвращает описание, что вышло.

    NOR-память умеет только «1 -> 0», стереть отдельный байт нельзя. Поэтому
    писать можно лишь туда, где сейчас 0xFF, и «восстановление» - это
    побайтовая запись прежних значений в только что стёртый сектор.
    Если что-то вернуть не удалось - говорим прямо, а не делаем вид.
    """
    lost = 0
    for i, b in enumerate(was):
        if b == 0xFF:
            continue
        cur = flash.read_byte(off + i)
        if cur == b:
            continue
        if cur != 0xFF:
            lost += 1
            continue
        ok, _why = flash.program_byte(off + i, b)
        if not ok:
            lost += 1
    if lost:
        return "вернуть удалось не всё: %d байт не восстановлены" % lost
    got = flash.read(off, len(was))
    if got != was:
        diff = sum(1 for a, b in zip(got, was) if a != b)
        return "после возврата не совпадает %d байт" % diff
    return "сектор возвращён как был"


def verify_geometry(flash, geo, progress=None, backup_path=None):
    """Проверка «а та ли это микросхема и тот ли сектор» ДО стирания.

    Пассивно этого не увидеть: ошибка «взяли 64 КиБ вместо 16 КиБ» или
    «микросхема не стирается без Vpp» проявляется только стиранием. Поэтому
    проверка активная - но НЕ РАЗРУШАЮЩАЯ:

      1. берём последний сектор карты (там обычно меньше всего данных);
      2. снимаем его в память (и в файл, если задан backup_path) ДО стирания;
      3. стираем и убеждаемся, что в секторе только 0xFF;
      4. возвращаем снимок побайтово и сверяем.

    Прежняя версия стирала последний сектор и НЕ возвращала данные: проверка
    перед записью молча уничтожала сектор карты. Возврат был только при
    --snapshot-dir, да и тот с ошибкой (проверялся не тот признак).

    Возвращает (ok, почему). Сектор не остаётся стёртым молча ни при каком
    исходе: если стирание сорвалось - всё равно пытаемся вернуть данные.
    """
    if progress:
        progress("проверяю размер сектора и отклик микросхемы")
    sector = int(geo.get("sector") or 0)
    limit = int(geo.get("size") or 0)
    if sector <= 0:
        return True, "размер сектора неизвестен - проверка пропущена"
    if limit < sector:
        return True, ("ёмкость карты неизвестна или меньше сектора (%d < %d) - "
                      "проверка пропущена" % (limit, sector))
    # Последний сектор берём по карте: у boot-микросхем он может быть и 64 КиБ
    # (bottom-boot), и 16 КиБ (top-boot), а «limit - sector» попадёт в
    # середину сектора и стирание заденет два соседних.
    off, sector = limit - sector, sector
    spans = None
    if getattr(flash, "sector_map", None):
        spans = flash.spans(limit)
    if spans:
        off, sector = spans[-1]
    else:
        off -= off % sector

    was = flash.read(off, sector)
    if len(was) != sector:
        return False, ("по адресу %#x прочиталось %d байт вместо %d: карта "
                       "меньше, чем мы думаем" % (off, len(was), sector))
    if backup_path:
        try:
            with open(backup_path, "wb") as f:
                f.write(was)
        except OSError as exc:
            return False, "не смог сохранить копию сектора: %s" % exc

    blank = all(b == 0xFF for b in was)
    ok, why = flash.erase_sector(off)
    if not ok:
        back = "" if blank else _restore_sector(flash, off, was)
        return False, ("сектор %d КиБ по адресу %#x не стёрся: %s%s"
                       % (sector // 1024, off, why,
                          ("; " + back) if back else ""))

    data = flash.read(off, sector)
    bad = next((i for i, b in enumerate(data) if b != 0xFF), None)
    if bad is not None:
        back = "" if blank else _restore_sector(flash, off, was)
        return False, ("после стирания байт %#x читается %#04x вместо 0xFF: "
                       "сектор не %d КиБ, стирание не идёт (Vpp?) или это не "
                       "та микросхема%s"
                       % (off + bad, data[bad], sector // 1024,
                          ("; " + back) if back else ""))

    if blank:
        return True, ("сектор %d КиБ по адресу %#x стирается (был пуст, "
                      "возвращать нечего)" % (sector // 1024, off))
    back = _restore_sector(flash, off, was)
    if back != "сектор возвращён как был":
        return False, ("сектор стирается, но данные вернуть не удалось: %s. "
                       "Копия сектора %s" % (back, ("в " + backup_path)
                                             if backup_path else "потеряна"))
    return True, ("сектор %d КиБ стирается по адресу %#x, данные возвращены"
                  % (sector // 1024, off))


def sector_table(geo, total, chips=1):
    """Таблица секторов: список (начало, размер). Размер сектора - из geo.

    У boot-микросхем карта неоднородная (16K + 8K + 8K + 32K + N x 64K, у
    top-boot - в обратном порядке), поэтому размеры берутся из sector_map(),
    а не из geo["sector"]. Размеры умножаются на число чипов: у пары сектор
    8 КиБ одного чипа занимает 16 КиБ карты. Последний сектор обрезается по
    total.
    """
    out = []
    off = 0
    total = total or 0
    scale = max(int(chips or 1), 1)
    pairs = sector_map(geo)
    if not pairs:
        return out
    while off < total:
        step = 0
        for size, count in pairs:
            size *= scale
            for _ in range(count):
                if off >= total:
                    return out
                n = min(size, total - off)
                out.append((off, n))
                off += n
                step += n
        if not step:
            break
    return out


def flash_plan(image, sector=SECTOR, spans=None):
    """Что делать с каждым сектором: стирать или нет и какие байты писать.

    sector - РАЗМЕР СЕКТОРА КОНКРЕТНОЙ МИКРОСХЕМЫ (не всегда 64 КиБ).
    spans - готовая карта секторов [(начало, размер)] в адресах карты
    (обычно flash.spans(len(image))). Нужна для boot-микросхем, где секторы
    разного размера: шаг «по 64 КиБ» там промахнулся бы мимо границ.
    """
    plan = []
    areas = spans if spans else [(off, min(sector, len(image) - off))
                                 for off in range(0, len(image), sector)]
    for off, size in areas:
        chunk = image[off:off + size]
        need_erase = any(b != 0xFF for b in chunk)
        writes = [off + i for i, b in enumerate(chunk) if b != 0xFF]
        plan.append((off, need_erase, writes))
    return plan


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
        self.span = max(PAGE, min(span or win["size"], win["size"]))
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
        base = card_off - (card_off % PAGE)
        length = min(self.span, self.win["size"])
        if self.limit:
            # не залезать за конец карты: там не данные, а пустая шина
            room = max(PAGE, self.limit - base)
            length = min(length, room)
        if length < n + (card_off - base):
            length = ((card_off - base) + n + PAGE - 1) // PAGE * PAGE
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
                 sector_map=None, ro=False, ro_why=""):
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
        # ro - микросхема в этом наборе ТОЛЬКО ЧИТАЕТСЯ: её команда стирания
        # либо отсутствует, либо требует 12 В на Vpp, либо набор её не знает.
        # Пишем отказ с причиной, а не «пробуем A0h и смотрим».
        self.ro = bool(ro)
        self.ro_why = str(ro_why or "")

    # --- отказ от записи ---------------------------------------------------
    def _readonly(self, action):
        """(False, почему) для микросхем, которые набор только читает.

        Возвращает None, если запись разрешена. Причина берётся из таблицы
        геометрии (поле why) - это даташитная формулировка, а не догадка.
        """
        if not self.ro:
            return None
        why = self.ro_why or "её команды не подтверждены даташитом"
        return False, (f"{self.name or 'микросхема'}: {action} в этом наборе "
                       f"не выполняется - {why}")

    # --- адреса команд -----------------------------------------------------
    def cmd_addr(self, local):
        """Абсолютный адрес для адреса команды local внутри чипа."""
        return chip_abs(local, 0, self.chips)

    def _send(self, local, value):
        """Послать команду value по адресу команды local - КАЖДОМУ чипу карты.

        У каждого чипа СВОЙ командный регистр, поэтому на карте из двух x8
        команда должна дойти до обоих. Делается это одним словом: байт
        команды дублируется в оба байта (Infineon KBA203626 «Connecting
        2x8-bit Parallel NOR Flash Devices to MCU»: «commands must be written
        in parallel to each 1x8 flash device ... flash command cycles must be
        duplicated within the 2B word that is sent to a word address, with one
        copy of the command cycle data in the low byte and one copy ... in the
        high byte»; в Linux MTD то же самое делает cfi_build_cmd из
        drivers/mtd/chips/cfi_util.c - размножает байт команды по всем чипам
        слова, а адрес считает как cmd_ofs * interleave).

        Было до 2.17.14: bytes([value, value]) только при wide=True. У пары
        x8-чипов wide=False, поэтому ВСЕ команды (AAh/55h/80h/A0h/30h/20h)
        уходили в чип 0, а данные - в оба. Чип 1 команд не видел, набор при
        этом отчитывался об успехе: на реальной карте не программировались
        нечётные байты, а сектор в чипе 1 не стирался.
        """
        n = self.chips if self.chips > 1 else (2 if self.wide else 1)
        self.bus.wr_many(self.cmd_addr(local), bytes([value] * n))
    # --- чтение массива ----------------------------------------------------
    def read(self, card_off, n=1):
        return self.bus.rd_block(card_off, n)

    def read_byte(self, card_off):
        return self.bus.rd(card_off)

    # --- карта секторов ----------------------------------------------------
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
            # Через _intel_send: ровно по байту на чип. Прежние два байта
            # подряд на карте с ОДНИМ чипом означали «команда + данные».
            for chip in range(self.chips):
                try:
                    self._intel_send(chip, INTEL_CUI_RESET)
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
        """Выйти из Unlock Bypass: 90h + 00h, затем F0h.

        Даташит AMD (Am29LV320D, S29AS016J): «During the unlock bypass mode, only
        the Unlock Bypass Program and Unlock Bypass Reset commands are valid», а
        Unlock Bypass Reset - это ДВА цикла: 90h, затем 00h (у части Spansion
        второй цикл F0h). Прежний код посылал только F0h: если чип его в bypass
        игнорирует, карта остаётся в bypass, и следующая разблокировка AA/55
        уходит в никуда (в bypass эти циклы уже не разблокировка).
        """
        if self.protocol == "jedec" and self._bypass_active:
            for chip in range(self.chips):
                try:
                    base = chip_abs(0, chip, self.chips)
                    self.bus.wr(base, AMD_READ_ID)     # 90h: цикл 1
                    self.bus.wr(base, 0x00)            # цикл 2
                except (DirectError, OSError, ValueError):
                    pass
        self.reset()
        self._bypass_active = False


class NorFlashStatus:
    """Статус, идентификатор, защита секторов, приостановка стирания."""

    def read_sector_protection(self):
        """Состояние защиты групп секторов: autoselect 90h + адрес (SGA+02h).

        Даташит Am29F016B/D (21444E): защита проверяется не отдельной командой,
        а обычным autoselect: AAh@0x555, 55h@0x2AA, 90h@0x555, затем чтение по
        адресу (SGA + 02h), где группу секторов выбирают биты A20-A18, а данные
        01h = защищена, 00h = нет.

        Прежний код посылал команду A6h и «выбирал группу битом A6» (адрес
        grp << 6). Такой команды у AMD нет: чип её игнорирует, чтение попадает
        в массив, и функция выдавала случайные «защищено/не защищено».

        Возвращает dict: номер группы -> True (защищена) / False / None.
        """
        if self.protocol != "jedec":
            return None
        sector_chip = max(self.sector // max(self.chips, 1), 1)
        group_size = sector_chip * 4          # в группе 4 сектора (A20-A18)
        chip_size = (int(getattr(self.bus, "limit", 0) or 0)
                     // max(self.chips, 1))
        groups_n = (chip_size // group_size) if chip_size else 8
        groups_n = max(min(groups_n, 8), 1)
        self.unlock()
        self._send(self.cmd1, AMD_READ_ID)     # 90h: режим autoselect
        groups = {}
        for grp in range(groups_n):
            # SGA + 02h: A20-A18 = номер группы, A1 = 1, A0 = 0
            addr = grp * group_size + 0x02
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
        if self.protocol == "intel":
            # У Intel приостановка ЕСТЬ: Erase Suspend B0h (таблица 3
            # даташита 290429-008). Прежний комментарий «у Intel 28F
            # приостановки нет» был неверен, а сама команда B0h лежала в
            # константах неиспользованной.
            for chip in range(self.chips):
                self._intel_send(chip, INTEL_CUI_SUSPEND)
            self._erase_suspended = True
            return True
        if self.protocol != "jedec":
            return False
        self._send(self.cmd1, NOR_ERASE_SUSPEND)
        self._erase_suspended = True
        return True

    def erase_resume(self):
        """Продолжение стирания после приостановки (команда 30h).

        Возвращает True если команда принята.
        """
        if self.protocol == "intel":
            # Erase Resume - D0h, та же команда, что Erase Confirm.
            for chip in range(self.chips):
                self._intel_send(chip, INTEL_CUI_ERASE_CONFIRM)
            self._erase_suspended = False
            return True
        if self.protocol != "jedec":
            return False
        self._send(self.cmd1, NOR_ERASE_RESUME)
        self._erase_suspended = False
        return True

    def clear_status(self):
        """Intel: сброс статусного регистра (биты 3,4,5 ставятся навсегда)."""
        if self.protocol != "intel":
            return
        # По байту на чип (см. _intel_send): прежняя запись «50h дважды
        # в один чип» на карте с одним чипом давала команду + данные.
        for chip in range(self.chips):
            try:
                self._intel_send(chip, INTEL_CUI_CLEAR_SR)
            except (DirectError, OSError, ValueError):
                pass

    def read_status(self, card_off=0):
        """Статус: у JEDEC - DQ7/DQ5 из ячейки, у Intel - команда 70h + чтение."""
        if self.protocol != "intel":
            return self.read_byte(card_off)
        chip = chip_of(card_off, self.chips)
        self._intel_send(chip, INTEL_CUI_READ_SR)
        return self.read_byte(card_off)

    def _clear_status_and_reset(self, card_off):
        """Очистить статус (50h) и вернуть чип в режим массива (FFh).

        Даташит 28F008SA 290429-008, §4.4: биты SR.3/4/5 ставятся автоматом
        (WSM) и снимаются ТОЛЬКО командой Clear Status Register (50h). FFh -
        это Read Array/Reset, он их не трогает. Без 50h первая же ошибка Vpp
        остаётся в регистре, и каждая следующая запись/стирание сразу получает
        тот же отказ: карта выглядит мёртвой до передёргивания питания.
        """
        chip = chip_of(card_off, self.chips)
        try:
            self._intel_send(chip, INTEL_CUI_CLEAR_SR)
        except (DirectError, OSError, ValueError):
            pass
        self._intel_send(chip, INTEL_CUI_RESET)

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
            # вернуло бы байт статуса. Чистим статус (50h) и возвращаем чип в
            # режим массива (FFh).
            self._clear_status_and_reset(card_off)
            return st, ("блок защищён (WPPS=1, block lock). У 28F004S5/008S5/016S5 снять "
                        "программную защиту нельзя: Intel прошивает master "
                        "lock-bit на заводе (erratum #2, 297796-007), нужны "
                        "12 В на RP#, а слот их не даёт")
        if st & SR_VPP_LOW:
            self._clear_status_and_reset(card_off)
            return st, ("нет напряжения программирования (VPPS=1): этой "
                        "микросхеме нужны 12 В на Vpp, а слот их не даёт; "
                        "статус очищен (50h), чтобы следующая попытка не "
                        "получила тот же застарелый бит")
        if st & SR_ERASE_FAIL:
            self._clear_status_and_reset(card_off)
            return st, ("микросхема сообщает: стирание не удалось (ESS=1); "
                        "статус очищен (50h)")
        if st & SR_WRITE_FAIL:
            self._clear_status_and_reset(card_off)
            return st, ("микросхема сообщает: запись не удалась (BWS=1); "
                        "статус очищен (50h)")
        return st, ""

    def read_id(self):
        """JEDEC ID: (производитель, устройство) или (None, None)."""
        if self.protocol == "intel":
            # 2.18.4: команда - ПО БАЙТУ НА ЧИП (см. _intel_send). Здесь до
            # этого раунда стояли два байта 90h подряд: на карте с парой
            # чипов это было верно, а на карте с ОДНИМ чипом второй байт
            # уходил уже в следующий адрес, и ID не читался (модель такие
            # команды отвергает: "команда + данные").
            self._intel_send(0, INTEL_CUI_READ_ID)
            man = self.read_byte(chip_abs(0, 0, self.chips))
            dev = self.read_byte(chip_abs(1, 0, self.chips))
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
        refuse = self._readonly("запись")
        if refuse:
            return refuse
        if self.protocol == "intel":
            return self._intel_program(card_off, value)
        if self.wide:
            return self.program_word(card_off, value)
        if self.chips > 1:
            # 16-битная карта из двух x8-чипов: одиночный байт писать нельзя.
            # После A0h микросхема ЖДЁТ данные, и чип, которому байт не
            # достался, остался бы в этом состоянии (следующая AAh ушла бы в
            # него как данные). Пишем слово: свой байт и соседний - как есть.
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
        """Записать СЛОВО, в котором лежит байт card_off.

        Так пишет Intel (команда и данные у него - слово) и так приходится
        писать на карте из двух x8-чипов: после A0h микросхема ЖДЁТ данные, и
        чип, которому байт не достался, остался бы в этом состоянии - а
        следующая AAh разблокировки попала бы в него как ДАННЫЕ (Infineon
        KBA203626: команда и данные должны идти парой в одном слове).
        Второй байт слова не портится: он пишется таким, каким был (0xFF после
        стирания, а запись в NOR умеет только 1 -> 0, поэтому уже записанный
        байт не меняется).
        """
        refuse = self._readonly("запись")
        if refuse:
            return refuse
        pair_at = card_off - (card_off % 2)
        before = self.read_byte(card_off)
        value = before if value is None else value
        if before == value:
            return True, ""
        if before != 0xFF and before != value:
            return False, f"байт не стёрт ({before:#04x}) — нужна чистка сектора"
        if self.chips > 1:
            data = bytearray(self.read_byte(pair_at + c) for c in range(self.chips))
        else:
            # Одиночная 16-битная микросхема: «слово» - два байта ОДНОЙ ячейки.
            data = bytearray([self.read_byte(pair_at), self.read_byte(pair_at + 1)])
        data[card_off - pair_at] = value
        if self.bypass and not self._bypass_broken:
            # Автовход в Unlock Bypass: первая запись открывает сеанс, дальше
            # A0h + слово за два цикла. Если микросхема 20h не знает (JEDEC ID
            # у семейства один, а команда есть только у поздних ревизий),
            # откатываемся на обычную запись - один раз, с предупреждением.
            if not self._bypass_active:
                self.enter_bypass()
                self._bypass_active = True
            ok_b, why_b = self._word_write(pair_at, data, card_off, value)
            if ok_b:
                return True, ""
            self.exit_bypass()
            self._bypass_active = False
            self._bypass_broken = True
            print("  ВНИМАНИЕ: Unlock Bypass (20h) не сработал: " + why_b
                  + ". Дальше пишу обычной командой A0h - медленнее, но верно.")
        return self._word_write(pair_at, data, card_off, value)

    def _word_write(self, pair_at, data, card_off, value):
        """Один цикл записи слова: (разблокировка) -> A0h -> слово -> опрос.

        В bypass циклы AAh/55h не разблокировка, а ДАННЫЕ: посылать их там
        нельзя, иначе в буфер чипа уедет мусор. Поэтому разблокировка - только
        когда bypass не активен.
        """
        if not self._bypass_active:
            self.unlock()
        self._send(self.cmd1, NOR_PROGRAM)
        self.bus.wr_many(pair_at, bytes(data))
        ok, why = self._wait_program(card_off, value)
        if not ok:
            return False, why
        if self.chips > 1:
            # Статус у чипов пары НЕ синхронизирован (Infineon KBA203626):
            # каждый чип надо опросить отдельно.
            for c in range(self.chips):
                if pair_at + c == card_off:
                    continue
                ok2, why2 = self._wait_program(pair_at + c, data[c])
                if not ok2:
                    return False, f"чип {c}, слово {pair_at:#x}: {why2}"
        for c in range(len(data)):
            got = self.read_byte(pair_at + c)
            if got != data[c]:
                return False, (f"после записи читается {got:#04x} вместо "
                               f"{data[c]:#04x} по адресу {pair_at + c:#x}")
        return True, ""
    def program_page(self, card_off, data):
        """Записать страницу программирования (до page байт) одной командой.

        data - то, что должно оказаться в карте; 0xFF означает «не трогать»
        (в NOR запись умеет только 1 -> 0, а стёртое состояние и есть 0xFF).
        Если page=1, работает обычная побайтовая запись.
        У Intel 28F/StrataFlash с page > 1 используется буферная запись
        E8h/N/D0h (Intel CUI), у AMD StrataFlash — 25h/D0h (JEDEC).
        """
        refuse = self._readonly("запись страницы")
        if refuse:
            return refuse
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
          2. N = число байт минус один (N=0 -> один байт) — тоже слово
          3. N+1 слово данных; младший байт слова - чип 0, старший - чип 1
          4. D0h (Write Confirm) — после него начинается программирование
          5. Опросить статусный регистр (70h) каждого чипа до WSRS=1
          6. Сбросить каждый чип (FFh)

        Раньше здесь были 25h и побайтовые данные: 25h - это команда AMD
        StrataFlash, а не Intel CUI, счётчика не было вовсе, а байтовые записи
        чип в CUI не принимает (он ждёт слово). Путь не был виден только
        потому, что у всех Intel-строк таблицы page=1.
        """
        if self.page > INTEL_BUFFER_SIZE:
            return False, (f"буфер записи Intel не больше {INTEL_BUFFER_SIZE} байт, "
                           f"а в таблице page={self.page}")
        if not self.wide:
            # Счётчик N и данные в E8h идут СЛОВОМ (младший байт - чип 0,
            # старший - чип 1). На байтовой (x8) микросхеме тот же код означал
            # бы «команда + данные» и буфер не набрался бы, а отчитался бы он
            # об успехе. Честнее отказаться, чем писать наугад.
            return False, ("буферная запись Intel E8h: счётчик и данные идут "
                           "словом, поэтому нужна 16-битная (x16) микросхема; "
                           f"{self.name or 'чип'} байтовый (x8)")
        start = card_off - (card_off % self.page_card)
        if card_off != start or len(data) != self.page_card:
            return self.program_page(start, self._pad_page(start, data, card_off))

        # Проверяем, что все байты страницы стёрты (0xFF) или уже нужного значения
        for i, b in enumerate(data):
            if b != 0xFF and self.read_byte(start + i) != 0xFF:
                return False, f"байт {start + i:#x} не стёрт"

        # Адрес БЛОКА (BA), а не 0: даташит Intel 28F320J5/J3 велит писать E8h,
        # счётчик N и D0h по адресу блока; иначе операция прерывается («The
        # device aborts the Write to Buffer command if the current address is
        # outside of the original block address»).
        block_card = self.span(start)[0]
        local_block = chip_local(block_card, self.chips)

        # 1. E8h Write-to-Buffer — в каждый чип по адресу буфера
        # Команда Intel - СЛОВО, и оно одно на всю пару чипов: писать её
        # «каждому чипу» нельзя - второй такой же байт чип прочтёт как
        # счётчик N (проверено моделью).
        self._intel_send(0, INTEL_BUFFER_SETUP, local_block)

        # 2. Счётчик N = число байт минус один. Пишем СЛОВОМ: младший байт -
        #    N, старший - 0 (в словесном режиме счётчик занимает DQ3-DQ0).
        count = self.page - 1
        self.bus.wr_many(chip_abs(local_block, 0, self.chips),
                         bytes([count, 0])[:max(self.chips, 1)])

        # 3. Данные — тоже СЛОВОМ: чип 0 в младшем байте, чип 1 в старшем.
        #    data лежит в порядке карты: start+i*chips+chip.
        for i in range(self.page):
            word = bytearray(2)
            for chip in range(self.chips):
                byte_idx = i * self.chips + chip
                word[chip] = data[byte_idx] if byte_idx < len(data) else 0xFF
            self.bus.wr_many(chip_abs(i, 0, self.chips),
                             bytes(word[:max(self.chips, 1)]))

        # 4. D0h Confirm — посылаем в каждый чип
        self._intel_send(0, INTEL_BUFFER_CONFIRM, local_block)

        # 5. Ожидание готовности через статусный регистр каждого чипа
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

        # 6. Проверяем записанные данные
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
        Последовательность (AMD MirrorBit 25539a; Am29DL640G; S29GL-P;
        W29GL256S - таблица «Write to Buffer» / «Program Buffer to Flash»):
          1. Разблокировка: AAh@0x555, 55h@0x2AA
          2. 25h @ SA (Write-to-Buffer Setup; SA - адрес СЕКТОРА)
          3. WC = N-1 @ SA (сколько байт загрузим минус один)
          4. N байт данных по адресам буфера
          5. 29h @ SA (Program Buffer to Flash - подтверждение)
          6. Опрос DQ7/DQ5 по последнему загруженному адресу
          7. Сброс F0h

        Что было не так до 2.17.13: цикл счётчика отсутствовал, а подтверждением
        служила команда D0h. Даташит: «The CONFIRM command (29h) is expected
        after exactly n+1 WRITE cycles; any other command at that point aborts
        the WRITE-to-BUFFER operation» и «The block address in this command must
        match the block address in the fourth write cycle». То есть без WC и с
        D0h микросхема отменяла операцию, а набор этого не видел: модель
        повторяла ту же ошибку.
        """
        start = card_off - (card_off % self.page_card)
        if card_off != start or len(data) != self.page_card:
            return self.program_page(start, self._pad_page(start, data, card_off))

        # Проверяем, что все байты страницы стёрты (0xFF)
        for i, b in enumerate(data):
            if b != 0xFF and self.read_byte(start + i) != 0xFF:
                return False, f"байт {start + i:#x} не стёрт"

        # SA - начало СЕКТОРА: 25h, счётчик и 29h идут именно по нему.
        sector_base, _size = self.span(start)

        # Если активен Unlock Bypass - выйти: в bypass последовательность
        # AA/55 не переводит чип в u1/u2, и 25h не будет принят.
        if self._bypass_active:
            self.reset()

        # 1. Разблокировка: AAh@cmd1, 55h@cmd2
        self.unlock()

        count = max(self.page - 1, 0)      # WC: число байт минус один, на чип

        for chip in range(self.chips):
            sa_addr = sector_base + chip
            # 2. 25h по адресу сектора
            self.bus.wr(sa_addr, AMD_BUFFER_SETUP)
            # 3. Счётчик WC по тому же адресу. Без него чип примет за счётчик
            #    первый байт данных - и буфер уедет (это и было в 2.17.12).
            self.bus.wr(sa_addr, count)

        # 4. Данные: чип chip получает data[i*chips+chip]
        for i in range(self.page):
            for chip in range(self.chips):
                byte_idx = i * self.chips + chip
                val = data[byte_idx] if byte_idx < len(data) else 0xFF
                self.bus.wr(start + i * self.chips + chip, val)

        # 5. 29h (Program Buffer to Flash) по адресу сектора
        for chip in range(self.chips):
            self.bus.wr(sector_base + chip, AMD_BUFFER_CONFIRM)

        # 6. Готовность: DQ7 опрашивается по ПОСЛЕДНЕМУ загруженному адресу
        for chip in range(self.chips):
            last_idx = (self.page - 1) * self.chips + chip
            last_val = data[last_idx] if last_idx < len(data) else 0xFF
            addr = start + last_idx
            want = last_val & DQ7
            for _ in range(self.polls):
                st = self.read_byte(addr)
                if st & DQ5:
                    st2 = self.read_byte(addr)
                    if (st2 & DQ7) == want:
                        break
                    return False, (f"чип {chip}, буфер {start:#x}: микросхема "
                                   "не приняла данные (DQ5=1, DQ7 не тот)")
                if (st & DQ7) == want:
                    break
            else:
                return False, (f"чип {chip}, буфер {start:#x}: не дождались "
                               "готовности за отведённое время")

        # 7. Сброс F0h - возвращаем чипы в чтение массива
        self.reset()

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
        """Послать команду CUI. Команда действует на КАРТУ, а не на один чип.

        Обе микросхемы пары должны быть в одном режиме (иначе чтение статуса
        одной переводит вторую в режим статуса), поэтому команда пишется
        словом - по байту на чип, - и всегда по адресу чипа 0.

        Было до 2.17.14: base = chip_abs(addr_local, chip). Для chip=1 это
        два байта со СДВИГОМ (адреса 1 и 2): первый - команда чипу 1, второй -
        та же команда чипу 0, но по ДРУГОМУ адресу. Для 70h это переводило
        чип 0 в режим статуса на смещённом адресе, и чтение массива из чипа 0
        начинало отдавать байты статуса.
        """
        base = chip_abs(addr_local or 0, 0, self.chips)
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
        """Стирание блока по командам CUI: 20h (setup), затем D0h (confirm).

        Оба цикла идут ПО АДРЕСУ БЛОКА (BA) - таблица 3 даташита 290429-008.
        Команда Intel пишется по байту на чип пары, поэтому ОДНА пара циклов
        доходит до обоих чипов. Прежний цикл «для каждого чипа» посылал вторую
        пару 20h/D0h уже во время стирания: чип её игнорирует (во время erase
        он слушает только 70h и B0h), но это лишние циклы и путаница в логах.
        """
        base, _size = self.span(card_off)
        local = chip_local(base, self.chips)
        self._intel_send(0, INTEL_CUI_ERASE_SETUP, local)
        self._intel_send(0, INTEL_CUI_ERASE_CONFIRM, local)
        for chip in range(self.chips):
            addr = chip_abs(0, chip, self.chips)
            for _ in range(self.polls):
                st, why = self._status_ok(addr)
                if why:
                    return False, f"чип {chip}, блок {base:#x}: {why}"
                if st & SR_SUSPEND:
                    # WSMS=1 при приостановке тоже, но блок не стёрт: без
                    # этой проверки «пауза» выглядела бы успешным стиранием.
                    return False, (f"чип {chip}, блок {base:#x}: стирание "
                                   "приостановлено (SR.6=1) — сначала Erase "
                                   "Resume (D0h), потом повторить")
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

        ВАЖНО: команды надо послать КАЖДОМУ чипу пары - у каждого чипа свой
        командный регистр (Infineon KBA203626; Linux MTD cfi_build_cmd
        размножает байт команды по чипам слова). Поэтому вся
        последовательность идёт через _send(), а 30h пишется словом: один
        цикл стирает сектор в ОБОИХ чипах. Статус потом опрашивается по
        каждому чипу отдельно - он у пары не синхронизирован.

        Раньше здесь был цикл «на каждый чип», но unlock()/80h всё равно
        уходили только в чип 0: чип 1 получал один 30h без 80h/AAh/55h и
        стирание игнорировал.
        """
        refuse = self._readonly("стирание сектора")
        if refuse:
            return refuse
        base, _size = self.span(card_off)
        if self.protocol == "intel":
            return self._intel_erase(base)
        # base - АБСОЛЮТНОЕ смещение; чипу нужен адрес ВНУТРИ него, поэтому
        # сначала переводим в байты чипа, и только потом раскладываем по чипам.
        local = chip_local(base, self.chips)
        self.unlock()
        self._send(self.cmd1, NOR_ERASE_SETUP)
        self.unlock()
        # Шестой цикл - 30h ПО АДРЕСУ СЕКТОРА (даташит, «Sector Erase Command
        # Sequence»; так же делают flashrom: issuecmd(0x30, page) и ядро
        # Linux: map_write(map, cfi->sector_erase_cmd, adr)). Раньше здесь
        # стояло _send(self.cmd1, ...) + wr(addr, 0xFF): команда уходила в
        # 0x555, то есть ВСЕГДА стирала сектор 0.
        if self.chips > 1:
            self.bus.wr_many(chip_abs(local, 0, self.chips),
                             bytes([NOR_ERASE_SECTOR] * self.chips))
        else:
            self.bus.wr(chip_abs(local, 0, self.chips), NOR_ERASE_SECTOR)
        for chip in range(self.chips):
            ok, why = self._wait_erase(chip_abs(local, chip, self.chips))
            if not ok:
                self.reset()
                return False, f"чип {chip}, сектор {base:#x}: {why}"
        return True, ""
    def erase_chip(self):
        """Стереть чипы целиком. У Intel 28F команды «стереть всё» НЕТ.

        В таблице 9 документа её нет: только пошаговое стирание блоков.
        Поэтому здесь честный отказ, а не «попробуем 10h и посмотрим».

        У JEDEC последовательность одна на карту (команды через _send() идут
        каждому чипу, 10h - словом), а готовность опрашивается по каждому
        чипу: у пары она не синхронизирована.
        """
        refuse = self._readonly("стирание чипа")
        if refuse:
            return refuse
        if self.protocol == "intel":
            return False, ("у Intel 28F008SA нет команды «стереть весь чип»: "
                           "стирать нужно поблочно (это и делает обычная запись)")
        self.unlock()
        self._send(self.cmd1, NOR_ERASE_SETUP)
        self.unlock()
        self._send(self.cmd1, NOR_ERASE_CHIP)
        for chip in range(self.chips):
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
