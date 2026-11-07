# -*- coding: utf-8 -*-
"""Виртуальные карты с РАЗНЫМИ микросхемами и модель окна моста.

Зачем модель, а не «мок, который всё принимает»: проверка должна ловить
ошибку в порядке команд, в адресах 0x555/0x2AA (или 0xAAAA/0x5554 у Intel,
где команда - слово), в размере сектора, в стирании «в обоих чипах пары» и
в чтении статуса DQ7/DQ5. Поэтому модель придирчива:

  * команды принимаются только в правильной последовательности, любой
    посторонний байт сбрасывает её (как в даташите);
  * NOR чистится только 1 -> 0;
  * стирание действует на ТОТ чип, в который попал адрес команды;
  * отказ записи виден как «DQ7 не тот + DQ5 = 1»; отказ стирания - DQ5 = 1;
  * адреса команд у второго чипа - со сдвигом, как в 16-битной паре;
  * у Intel-режима (wide) команда - СЛОВО: один байт вместо слова команды
    не принимается, и это отдельная проверка.

Параметры чипа (sector, cmd, wide, chip_size) берутся из карты чипов самого
pcmcia_direct.py (GEOMETRY) - то есть проверяется ровно то, что уедет на
ноутбук, а не отдельная копия таблицы.
"""
import os
import pathlib
import sys

MIB = 1 << 20
CHIP = 2 * MIB          # Am29F016 = 16 Мбит = 2 МиБ
SECTOR = 64 * 1024


def load_direct(kit):
    """Модуль работы с картой.

    С 2.18.0 это пакет src/pcmcia_direct (монолит windows/legacy/pcmcia_direct.py
    разложен по модулям), а не отдельный файл. Плоский API сохранён, поэтому
    весь код ниже работает с ним как раньше.
    """
    src = os.path.join(kit, "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    import pcmcia_direct
    return pcmcia_direct


class ModelCardIO:
    """Ввод-вывод модели: чтение, запись, отображение окна, ExCA."""

    def raw(self, off):
        self.reads += 1
        chip, local = off % self.chips, off // self.chips
        if chip >= self.chips or local >= self.chip_size:
            return 0x00
        v = self.mem[chip][local]
        if self.stuck:
            v = (v & ~self.stuck[0]) | (self.stuck[1] & self.stuck[0])
        return v

    def read(self, addr, n):
        if n > 1 and self._fast_path_ok(addr, n):
            return self._read_fast(addr, n)
        d = self.d
        out = bytearray()
        for i in range(n):
            a = addr + i
            if self.exca <= a < self.exca + 0x100:
                out.append(self.regs.get(a - self.exca, 0))
                continue
            off = self.win_card + (a - self.win_start)
            v = self._chip_read(off)
            if self.fail_off == off:
                if self.fail_reads.get(off, 0) == 0:
                    self.fail_reads[off] = 1
                    v = (v & d.DQ7) ^ d.DQ7 | d.DQ5
                else:
                    self.fail_reads[off] = self.fail_reads.get(off, 0) + 1
            elif self.erase_fail and self.protocol != "intel":
                v = d.DQ5
            out.append(v)
        return bytes(out)

    def _read_fast(self, addr, n):
        base = self.win_card + (addr - self.win_start)
        k = self.chips
        start, end = base // k, (base + n + k - 1) // k
        parts = [memoryview(self.mem[c])[start:min(end, self.chip_size)]
                 for c in range(k)]
        inter = bytearray(n * k)
        view = memoryview(inter)
        for c, part in enumerate(parts):
            view[c:len(part) * k:k] = part
        begun = base - start * k
        return bytes(view[begun:begun + n])

    def _chip_read(self, off):
        chip, local = off % self.chips, off // self.chips
        if chip >= self.chips or local >= self.chip_size:
            return 0x00
        mode = getattr(self, "_mode", "array")
        if self.protocol == "intel":
            st = getattr(self, "_cui_state", None) or {}
            mode = st.get("mode", "array")
            if mode == "sr":
                if not self.answer_id:
                    return self.mem[chip][local]
                return st.get("sr", 0) & 0xFF
        if mode == "id":
            if not self.answer_id:
                return self.mem[chip][local]
            manuf, dev = self._id_values()
            if local == 0:
                return manuf
            if local == 1:
                return dev
            if local & 0x03 == 0x02:
                # Sector Group Protect Verify: A1=1, A0=0, группу выбирают
                # биты A20-A18, данные 01h = защищена, 00h = нет (даташит
                # Am29F016B/D, таблица autoselect).
                return 0x01 if self.protect_groups[(local >> 18) & 0x07] else 0x00
            return 0x00
        if mode == "cfi":
            cfi = self._cfi_bytes()
            return cfi[local] if local < len(cfi) else 0x00
        if mode == "erase_suspended":
            return self.raw(off)
        if getattr(self, "_dq7_polls_left", 0) > 0:
            self._dq7_polls_left -= 1
            return self.raw(off) | self.d.DQ7
        return self.raw(off)

    def write(self, addr, data):
        self._wide_write = getattr(self, "_wide_write", False)
        for i, v in enumerate(data):
            a = addr + i
            if self.exca <= a < self.exca + 0x100:
                self._exca(a - self.exca, v)
            else:
                off = self.win_card + (a - self.win_start)
                self.store(off, v, wide=self._wide_write)
        self._wide_write = False

    def write_many(self, addr, data):
        if self.protocol == "intel":
            # Команда идёт ПО БАЙТУ НА ЧИП: у пары - два байта (слово), у
            # одиночного чипа - один. Всё остальное - неверная ширина.
            if len(data) == self.chips:
                word = data[0] | ((data[1] if len(data) > 1 else data[0]) << 8)
                return self._cui_step(addr // self.chips, word)
            self.bad_wide_cmds += 1
            return
        if self._st.get(addr % self.chips, "idle") == "prog" and len(data) > 1:
            return self._program_page(addr, bytes(data))
        if not self.wide or len(data) != 2:
            for i, v in enumerate(data):
                self.store(addr + i, v)
            return
        local = addr // self.chips
        for chip, v in ((0, data[0]), (1, data[1])):
            self.store(chip_abs_dummy(addr, chip), v, wide=True, chip=chip,
                       local=local)

    def _exca(self, reg, val):
        d = self.d
        self.regs[reg] = val
        if reg == d.CB_MEM_PAGE0:
            self._page = val
        def word(lo):
            return self.regs.get(lo, 0) | (self.regs.get(lo + 1, 0) << 8)
        start_reg = d.I365_MEM0_START
        stop_reg = d.I365_MEM0_STOP
        off_reg = d.I365_MEM0_OFF
        if reg in (start_reg, start_reg + 1, stop_reg, stop_reg + 1,
                   off_reg, off_reg + 1, d.I365_ADDRWIN):
            start = word(start_reg) & 0xFFF
            stop = word(stop_reg) & 0xFFF
            self.win_card = (word(off_reg) & 0x3FFF) << 12
            pages = ((stop - start) & 0xFFF) + 1 if stop >= start else 1
            self.win_size = pages * 4096
            self.win_start = (getattr(self, "_page", 0) << 24) | (start << 12)

    def _map(self, addr, length, write=False):
        return _Win(self, addr), 0

    def close(self):
        pass

    def image(self):
        return bytes(self.raw(i) for i in range(self.chips * self.chip_size))

    def load(self, data, at=0):
        for i, b in enumerate(data):
            off = at + i
            self.mem[off & 1][off >> 1] = b


class ModelCardState:
    """Конечный автомат модели: обработка команд шины (store, CUI, режимы)."""

    def _sector_at(self, local):
        """Границы сектора ЧИПА, содержащего local: (начало, размер)."""
        if not self.sector_map:
            sector = self.sector // max(self.chips, 1)
            return local - (local % sector), sector
        off = 0
        for size, count in self.sector_map:
            if local < off + size * count:
                return off + ((local - off) // size) * size, size
            off += size * count
        size = self.sector_map[-1][0]
        return max(off - size, 0), size

    @property
    def state(self):
        """Состояние последнего затронутого чипа (автомат теперь по чипам)."""
        return self._st.get(getattr(self, "_last_chip", 0), "idle")

    @state.setter
    def state(self, value):
        self._st[self._last_chip] = value

    def store(self, off, val, wide=False, chip=None, local=None):
        """Один байт в шину. chip/local можно задать напрямую (см. write_many).

        Состояние командного автомата ведётся ПО ЧИПАМ: у каждого чипа свой
        командный регистр, и команда, посланная одному, второго не трогает
        (Infineon KBA203626; Linux MTD cfi_build_cmd). Общее на карту
        состояние скрывало целый класс ошибок: данные, ушедшие в чип без
        команды, «программировались», а 30h без 80h/AAh/55h «стирал».
        """
        d = self.d
        self.writes += 1
        if chip is None:
            chip, local = off % self.chips, off // self.chips
        if chip >= self.chips or local >= self.chip_size:
            return
        self._last_chip = chip
        st = self._st.get(chip, "idle")
        if self.protocol == "intel":
            self.bad_wide_cmds += 1
            return
        if st == "prog":
            if off in self.refuse_program:
                self.fail_off = off
            else:
                self.mem[chip][local] &= val & 0xFF
            left = self._prog_left.get(chip, 1) - 1
            self._prog_left[chip] = left
            if left <= 0:
                self._st[chip] = "bypass" if self.bypass else "idle"
            return
        if st == "erase_cmd":
            # Даташит (AMD/MX «Sector Erase Command Sequence», 6 циклов):
            # шестой цикл — 30h ПО АДРЕСУ СТИРАЕМОГО СЕКТОРА. Сектор выбирают
            # старшие биты этого адреса (A19-A12), а не командный 0x555.
            # Поэтому модель стирает тот сектор, куда реально попал адрес:
            # если код пошлёт 30h в 0x555, сотрётся сектор 0 - ровно как на
            # настоящей карте. Так ошибка в адресе становится видимой в тесте.
            self._st[chip] = "idle"
            if val == d.NOR_ERASE_SECTOR:
                self.erase_targets.append((chip, local))
                if self.refuse_erase:
                    self.erase_fail = True
                    return
                if self.quiet_erase:
                    return
                base, chip_sector = self._sector_at(local)
                for i in range(base, min(base + chip_sector, self.chip_size)):
                    self.mem[chip][i] = 0xFF
                self.erases += 1
            elif val == d.NOR_ERASE_CHIP and local == self.cmd[0]:
                # Chip Erase - те же 6 циклов, но шестой не 30h по адресу
                # сектора, а 10h по командному адресу (Am29F016 и др.).
                # До 2.17.14 модель этого не знала и «chip erase» в ней
                # ничего не стирал, поэтому и проверять его было нечем.
                if self.refuse_erase:
                    self.erase_fail = True
                elif not self.quiet_erase:
                    for i in range(self.chip_size):
                        self.mem[chip][i] = 0xFF
                    self.erases += 1
            return
        self.cmds.append((local, val))
        if val in (d.NOR_RESET, d.INTEL_RESET) and st != "buffer":
            self._mode = "array"
            self.bypass = False
            self._st.clear()
            self._prog_left.clear()
            self.fail_off, self.erase_fail = None, False
            self._dq7_polls_left = 0
            return
        if st == "idle" and local == self.cmd[0] and val == d.NOR_UNLOCK1:
            self._st[chip] = "u1"
            return
        if st == "u1" and local == self.cmd[1] and val == d.NOR_UNLOCK2:
            self._st[chip] = "u2"
            return
        # CFI Query: ОДИН цикл, адрес 55h, данные 98h (без разблокировки).
        if st == "idle" and local == 0x55 and val == d.AMD_READ_CFI:
            if not self.no_cfi:
                self._mode = "cfi"
            return
        if st == "u2" and local == self.cmd[0]:
            self._mode_hook(local, val)
            if val == d.NOR_BYPASS:
                if not self.supports_bypass:
                    # Микросхема не знает команды 20h: игнорирует её, как
                    # настоящая. Именно так выглядит карта с Am29F016 (без D).
                    self._st[chip] = "idle"
                    return
                self.bypass = True
                self._st[chip] = "bypass"
                return
            if val == d.NOR_PROGRAM:
                self._st[chip] = "prog"
                self._prog_left[chip] = 2 if self.wide else 1
                return
            if val == d.NOR_ERASE_SETUP:
                self._st[chip] = "erase_u1"
                return
            if val == d.NOR_ERASE_CHIP:
                self._st[chip] = "idle"
                if self.refuse_erase:
                    self.erase_fail = True
                elif not self.quiet_erase:
                    for i in range(self.chip_size):
                        self.mem[chip][i] = 0xFF
                    self.erases += 1
                return
            self._st[chip] = "idle"
            return
        if st == "u2" and val == d.AMD_BUFFER_SETUP:
            # 25h @ SA. Следующий цикл К ЭТОМУ ЖЕ ЧИПУ - счётчик WC = N-1
            # (даташит AMD: «the number of locations minus one»); буфер у
            # каждого чипа свой, поэтому состояние ведётся по чипам.
            st_now = getattr(self, "_buffer", None)
            if st_now is None:
                st_now = self._buffer = {"phase": {}, "left": {}, "data": {}}
            st_now["phase"][chip] = ("count", local)
            st_now["left"][chip] = 0
            st_now["data"][chip] = []
            self._st[chip] = "buffer"
            return
        if st == "buffer":
            buf = self._buffer
            if val == d.AMD_BUFFER_SETUP:
                # Следующий чип пары открывает СВОЙ буфер (у каждого чипа свой
                # CUI и свой загрузчик буфера) - это не нарушение порядка.
                buf["phase"][chip] = ("count", local)
                buf["left"][chip] = 0
                buf["data"][chip] = []
                return
            ph = buf["phase"].get(chip)
            if ph is None:
                # Запись чипу, который буфер не открывал: даташит - abort.
                self.bad_buffer += 1
                return
            kind, sa_local = ph
            if kind == "count":
                buf["left"][chip] = (val & 0xFF) + 1
                buf["phase"][chip] = ("data", sa_local)
                return
            if val == d.AMD_BUFFER_CONFIRM:
                # Program Buffer to Flash: только 29h, только по адресу
                # сектора и ровно после N+1 циклов данных.
                if local != sa_local or buf["left"].get(chip, 0) != 0:
                    self.bad_buffer += 1
                else:
                    for off_i, local_i, v_i in buf["data"][chip]:
                        if off_i in self.refuse_program:
                            self.fail_off = off_i
                            continue
                        self.mem[chip][local_i] &= v_i & 0xFF
                buf["phase"].pop(chip, None)
                self._st[chip] = "idle"
                if not buf["phase"]:
                    self._buffer = None
                    self._dq7_polls_left = self.chips * getattr(self, "polls", 100)
                return
            if buf["left"].get(chip, 0) <= 0:
                # Данных больше, чем обещал счётчик: abort.
                self.bad_buffer += 1
                return
            buf["data"][chip].append((off, local, val))
            buf["left"][chip] -= 1
            return
        if st == "bypass" and local == self.cmd[0] and val == d.NOR_PROGRAM:
            self._st[chip] = "prog"
            self._prog_left[chip] = 2 if self.wide else 1
            return
        if st == "erase_u1" and local == self.cmd[0] and val == d.NOR_UNLOCK1:
            self._st[chip] = "erase_u2"
            return
        if st == "erase_u2" and local == self.cmd[1] and val == d.NOR_UNLOCK2:
            self._st[chip] = "erase_cmd"
            return
        self._st[chip] = "idle"

    def _cui_step(self, local, word):
        d = self.d
        st = self._cui_state = getattr(self, "_cui_state", None) or {
            "cmd": None, "mode": "array", "sr": 0}
        cmd = st.get("cmd")

        def W(v):
            return (v & 0xFF) | ((v & 0xFF) << 8)
        def locked(off):
            """Блок защищён? У Intel это WPPS=1 (бит 1 статуса)."""
            return bool(self.protect_groups[(off >> 16) & 0x07])

        if cmd == "write":
            st["cmd"] = None
            if locked(local):
                st["sr"] = d.SR_READY | d.SR_PROTECT
                st["mode"] = "sr"
                return
            if local in self.refuse_program:
                self.fail_off = local
                st["sr"] = d.SR_READY | d.SR_WRITE_FAIL
                st["mode"] = "sr"
                return
            self.mem[0][local] &= word & 0xFF
            if self.chips > 1:
                self.mem[1][local] &= (word >> 8) & 0xFF
            st["sr"] = d.SR_READY
            st["mode"] = "sr"
            return
        if cmd == "buffer_cmd":
            # Второй такт: N - число записываемых байт минус один (N=0 -> 1)
            st["buf_left"] = (word & 0xFF) + 1
            st["buf_data"] = []
            st["cmd"] = "buffer_data"
            return
        if cmd == "buffer_data":
            if st["buf_left"] > 0:
                st["buf_data"].append((local, word))
                st["buf_left"] -= 1
                if st["buf_left"] == 0:
                    st["cmd"] = "buffer_confirm"
                return
        if cmd == "buffer_confirm":
            st["cmd"] = None
            if word != W(d.INTEL_BUFFER_CONFIRM):
                # Даташит: любой другой байт вместо D0h отменяет буфер
                st["sr"] = d.SR_READY | d.SR_WRITE_FAIL
                st["mode"] = "sr"
                return
            for off, w in st["buf_data"]:
                if locked(off):
                    st["sr"] = d.SR_READY | d.SR_PROTECT
                    st["mode"] = "sr"
                    return
            for off, w in st["buf_data"]:
                if off in self.refuse_program:
                    self.fail_off = off
                    continue
                self.mem[0][off] &= w & 0xFF
                if self.chips > 1:
                    self.mem[1][off] &= (w >> 8) & 0xFF
            st["sr"] = d.SR_READY
            st["mode"] = "sr"
            return
        if cmd == "erase":
            st["cmd"] = None
            if locked(local):
                st["sr"] = d.SR_READY | d.SR_PROTECT
                st["mode"] = "sr"
                self.erase_fail = True
                return
            if word != W(d.INTEL_CUI_ERASE_CONFIRM):
                st["sr"] = d.SR_READY | d.SR_WRITE_FAIL
                st["mode"] = "sr"
                return
            if self.no_vpp:
                st["sr"] = d.SR_READY | d.SR_VPP_LOW
                st["mode"] = "sr"
                self.erase_fail = True
                return
            if self.refuse_erase:
                st["sr"] = d.SR_READY | d.SR_ERASE_FAIL
                st["mode"] = "sr"
                self.erase_fail = True
                return
            if not self.quiet_erase:
                base = local - (local % self.sector_chip)
                for ch in range(self.chips):
                    for i in range(base, min(base + self.sector_chip,
                                             self.chip_size)):
                        self.mem[ch][i] = 0xFF
                self.erases += 1
            st["sr"] = d.SR_READY
            st["mode"] = "sr"
            return
        if word == W(d.INTEL_CUI_RESET):
            st["cmd"], st["mode"] = None, "array"
            self._st.clear()
            self.fail_off, self.erase_fail = None, False
            return
        if word == W(d.INTEL_CUI_READ_ID):
            st["mode"] = "id"
            return
        if word == W(d.INTEL_CUI_READ_SR):
            st["mode"] = "sr"
            return
        if word == W(d.INTEL_CUI_CLEAR_SR):
            st["sr"] = 0
            st["mode"] = "sr"
            return
        if word == W(d.INTEL_CUI_ERASE_SETUP):
            st["cmd"] = "erase"
            return
        if word in (W(d.INTEL_CUI_WRITE_SETUP), W(d.INTEL_CUI_ALT_WRITE)):
            st["cmd"] = "write"
            return
        if word == W(d.INTEL_BUFFER_SETUP):
            # E8h: Write to Buffer (даташит Intel, «Write to Buffer»)
            st["cmd"] = "buffer_cmd"
            return
        # Неизвестная команда: настоящая микросхема её игнорирует, а модель
        # считает - так старый протокол 25h (AMD) становится видимым в тесте.
        self.bad_cui_cmds += 1
        st["cmd"] = None

    def _mode_hook(self, local, val):
        d = self.d
        if local != self.cmd[0]:
            return
        if val in (d.AMD_READ_ID, d.INTEL_READ_ID):
            self._mode = "id"
        elif val in (d.AMD_READ_CFI, d.INTEL_READ_CFI):
            self._mode = "cfi"
        elif val in (d.NOR_RESET, d.INTEL_RESET):
            self._mode = "array"
        # 0xA6h команды не существует: у AMD защита группы читается через
        # autoselect (90h и адрес SGA+02h), а «метод A6» - это уровень
        # V(ih) на ВЫВОДЕ A6 при высоковольтном программировании.
        elif val == d.NOR_ERASE_SUSPEND:
            self._mode = "erase_suspended"
            self._erase_suspended = True
        elif val == d.NOR_ERASE_RESUME and self._mode == "erase_suspended":
            self._mode = "idle"
            self._erase_suspended = False

    def _fast_path_ok(self, addr, n):
        if getattr(self, "_mode", "array") != "array" and self.protocol != "intel":
            return False
        if self.protocol == "intel":
            st = getattr(self, "_cui_state", None) or {}
            if st.get("mode", "array") != "array":
                return False
        if self.fail_off is not None or self.fail_page or self.erase_fail \
                or self.stuck:
            return False
        if self.exca and addr <= self.exca < addr + n:
            return False
        off = self.win_card + (addr - self.win_start)
        return 0 <= off and off + n <= self.chips * self.chip_size


_INTEL_IDS = None


def _intel_id_from_registry(chip_name):
    """Код устройства Intel из эталонного реестра (data/jedec_registry.json)."""
    global _INTEL_IDS
    if _INTEL_IDS is None:
        import json
        path = pathlib.Path(__file__).resolve().parent.parent / "src" / \
            "pcmcia_flash" / "data" / "jedec_registry.json"
        try:
            reg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _INTEL_IDS = {}
        else:
            _INTEL_IDS = {info["chip"].upper(): int(key, 16)
                          for key, info in (reg.get("Intel") or {}).items()}
    return _INTEL_IDS.get(str(chip_name).upper())


class ModelCardProgramming:
    """Симуляция программирования страницы (AMD-режим A0h + данные)."""

    def _program_page(self, addr, data):
        # Цикл записи закончен. Чип возвращается туда, откуда пришёл: в режим
        # Unlock Bypass (он НЕ заканчивается на записи - только по 90h/00h или
        # сбросу) либо в обычное чтение массива.
        for ch in list(self._st):
            if self._st[ch] == "prog":
                self._st[ch] = "bypass" if self.bypass else "idle"
        for i, v in enumerate(data):
            a = addr + i
            if a in self.refuse_program:
                self.fail_off = a
                self.fail_page = (addr, addr + len(data))
                return
            chip, local = a % self.chips, a // self.chips
            if chip >= self.chips or local >= self.chip_size:
                continue
            self.mem[chip][local] &= v & 0xFF
        self.page_writes = getattr(self, "page_writes", 0) + 1
        self.writes += len(data)

class ModelCard(ModelCardIO, ModelCardState, ModelCardProgramming):
    """Два (или один) чипа в паре, регистры ExCA и окно - в одном объекте.

    Composes three single-responsibility classes:

    * ModelCardIO   — чтение, запись, отображение окна, ExCA, удобства;
    * ModelCardState — конечный автомат: store, CUI, режимы;
    * ModelCardProgramming — симуляция программирования страницы.

    Параметры у разных микросхем разные, поэтому всё берётся из geo
    (см. identify_chip): размер сектора, адреса команд, ширина команды,
    страница программирования.
    """

    name = path = "/dev/mem"
    base = 0

    def __init__(self, kit=None, chips=2, chip_name="Am29F016", chip_size=None,
                 blank=True, refuse_program=(), refuse_erase=False,
                 quiet_erase=False, stuck=None, answer_id=True,
                 sector_override=None, card_mib=4, no_vpp=False,
                 page_size_cfi=0, supports_bypass=True, no_cfi=False):
        self.d = load_direct(kit or os.environ.get("KIT_DIR", "."))
        g = self.d.geometry_for(chip_name)
        self.chip_name = chip_name
        self.protocol = g.get("protocol", "jedec")
        self.sector_chip = int(sector_override or g["sector"])
        # Карта секторов ОДНОГО чипа: у boot-микросхем она неоднородная
        # (16K + 8K + 8K + 32K + N x 64K, у top-boot - наоборот). Модель
        # стирает именно тот сектор, в который попал адрес 30h, поэтому
        # «округление по sector» тут повторяло бы ошибку кода, а не ловило её.
        self.sector_map = [(int(sz), int(cnt))
                           for sz, cnt in (g.get("sectors") or [])]
        self.cmd = tuple(g["cmd"])
        self.wide = bool(g["wide"])
        self.chip_size = int(chip_size or g["size"] or CHIP)
        self.chips = chips
        self.sector = self.sector_chip * max(chips, 1)   # в адресах карты
        self.page = int(g.get("page") or 1)              # байт на команду A0h
        self.answer_id = answer_id
        self.mem = [bytearray([0xFF if blank else 0x00]) * self.chip_size
                    for _ in range(chips)]
        self.exca = 0x60000800
        self.pci_base = 0x64000000
        self.win_start = self.pci_base
        self.win_size = card_mib * MIB
        self.win_card = 0
        self.regs = {}
        self.refuse_program = set(refuse_program)
        self.refuse_erase = refuse_erase
        self.quiet_erase = quiet_erase
        self.no_vpp = no_vpp       # нет 12 В на Vpp: статус скажет VPPS=1
        self.stuck = stuck
        self.page_size_cfi = page_size_cfi
        self.supports_bypass = supports_bypass   # чип знает команду 20h?
        self.no_cfi = no_cfi                     # чип не отвечает на 98h
        self._st = {}                # состояние команд КАЖДОГО чипа
        self._prog_left = {}         # сколько байт данных ждёт каждый чип
        self._last_chip = 0
        self.fail_page = None
        self.protect_groups = [False] * 8   # состояние защиты 8 групп секторов
        self._erase_suspended = False       # стирание приостановлено (B0h)
        self.page_writes = 0
        self.prog_left = 1
        self.erase_left = 1
        self.erase_targets = []          # (чип, адрес), по которым пришёл 30h
        self._mode = "array"
        self.bypass = False          # Unlock Bypass активен: A0h принимается без разблокировки
        self.fail_off = None
        self.fail_reads = {}
        self.erase_fail = False
        self.reads = self.writes = self.erases = 0
        self.cmds = []
        self.bad_wide_cmds = 0       # команды, поданные не словом
        self.bad_cui_cmds = 0        # неизвестные команды в Intel CUI (например 25h)
        # Нарушения протокола буферной записи AMD: нет цикла счётчика WC,
        # подтверждение не 29h или не по адресу сектора, данных больше/меньше
        # обещанного. Даташит: любое из этого отменяет операцию (abort).
        self.bad_buffer = 0
        self._buffer = None          # состояние буферной записи AMD (по чипам)
        self._dq7_polls_left = 0     # счётчик отложенных чтений с DQ7=1 после D0h

    def _id_values(self):
        # Intel: код берём из эталонного реестра data/jedec_registry.json, а не
        # из таблиц кода - иначе модель повторит ошибку кода (раньше здесь
        # стояло 0xA0 для 28F008SA, хотя это код 28F016SV, и выдуманный A1h).
        if self.chip_name.upper().startswith("28F"):
            got = _intel_id_from_registry(self.chip_name)
            if got is not None:
                return (0x89, got)
        table = {"Am29F016": (0x01, 0xAD), "Am29F032": (0x01, 0x34),
                 "Am29F080": (0x01, 0x58), "Am29F040": (0x01, 0xA4),
                 "Am29LV016": (0x01, 0x49), "28F008SA": (0x89, 0xA0),
                 # коды с ревизией - по include/flashchips.h (flashrom):
                 # 58h = Am29F800BB, D6h = Am29F800BT, ABh = Am29F400BB,
                 # 23h = Am29F400BT, 57h = Am29F200BB, 51h = Am29F200BT
                 "Am29F800BB": (0x01, 0x58), "Am29F800BT": (0x01, 0xD6),
                 "Am29F400AB": (0x01, 0xAB), "Am29F400AT": (0x01, 0x23),
                 "Am29F200AB": (0x01, 0x57), "Am29F200AT": (0x01, 0x51),
                 "Am29F080B": (0x01, 0xBA), "Am29LV001BB": (0x01, 0x6D),
                 "Am29LV001BT": (0x01, 0xED),
                 "MBM29F016": (0x04, 0xAD)}
        return table.get(self.chip_name, (0x01, 0xFF))

    def _cfi_bytes(self):
        """Мини-таблица CFI ровно по раскладке CFI Publication 100 / cfi.h.

        Смещения (сверено с include/linux/mtd/cfi.h, struct cfi_ident):

          0x10-0x12  "QRY";
          0x13-0x14  набор команд (02h = AMD/Fujitsu);
          0x15-0x16  адрес расширенной таблицы (0x40, там "PRI"+версия);
          0x1B-0x1E  Vcc min/max, Vpp min/max при записи (100 мВ);
          0x1F-0x22  типовые тайм-ауты (2^n мкс);
          0x27       ёмкость чипа: 2^n байт;
          0x28-0x29  разрядность шины;
          0x2A-0x2B  буферная запись: 2^n байт (0 = нет);
          0x2C       число erase-регионов;
          0x2D-0x30  регион: блоков-1 (16 бит), размер/256 (16 бит).

        Модель обязана отдавать таблицу именно в этой раскладке - раньше она
        повторяла ошибку кода (поля на байт раньше), и тест ничего не ловил.
        page_size_cfi=0 означает отсутствие буфера записи, как у Am29F016.
        """
        cfi = bytearray(0x48)
        cfi[0x10:0x13] = b"QRY"
        cfi[0x13] = 0x02                      # набор команд: AMD/Fujitsu
        cfi[0x14] = 0x00
        cfi[0x15] = 0x40                      # P_ADR -> расширенная таблица
        cfi[0x16] = 0x00
        cfi[0x1B] = 30                        # Vcc min 3.0 В (100 мВ)
        cfi[0x1C] = 36                        # Vcc max 3.6 В
        cfi[0x1D] = 0                         # Vpp нет
        cfi[0x1E] = 0
        cfi[0x1F] = 0x07                      # запись слова: 128 мкс
        cfi[0x20] = 0x09                      # буфер: 512 мкс
        cfi[0x21] = 0x0A                      # сектор: 1 мс (типовая)
        cfi[0x22] = 0x0C                      # чип: 4 мс (типовая)
        cfi[0x23] = 0x0B                      # запись слова: 2048 мкс (макс)
        cfi[0x25] = 0x0E                      # сектор: 16 мс (макс)
        size = self.chip_size
        cfi[0x27] = size.bit_length() - 1 if size else 0     # 2^n байт
        cfi[0x28] = 0x01                      # x8
        cfi[0x29] = 0x00
        cfi[0x2A] = self.page_size_cfi        # 2^n байт буфера, 0 = нет
        cfi[0x2B] = 0x00
        # Erase-регионы. У boot-микросхем их ровно 4 (16K, 2x8K, 32K,
        # N x 64K), и CFI отдаёт их именно так - в таблице 4 слота по 4 байта.
        if self.sector_map:
            regions = list(self.sector_map)[:4]
        else:
            sector = self.sector_chip
            regions = [(sector, max(size // sector, 1))]
        cfi[0x2C] = len(regions)              # число erase-регионов
        base = 0x2D
        for region_size, region_count in regions:
            cfi[base] = (region_count - 1) & 0xFF        # блоков - 1 (мл.)
            cfi[base + 1] = ((region_count - 1) >> 8) & 0xFF
            cfi[base + 2] = (region_size // 256) & 0xFF  # размер / 256 (мл.)
            cfi[base + 3] = ((region_size // 256) >> 8) & 0xFF
            base += 4
        cfi[0x40:0x43] = b"PRI"               # расширенная таблица
        cfi[0x43] = 0x01                      # версия CFI 1.2
        cfi[0x44] = 0x02
        return bytes(cfi)


def chip_abs_dummy(addr, chip):
    """Адрес нужен только для refuse_program: (local << 1) | chip."""
    return ((addr >> 1) << 1) | chip


class _Win:
    def __init__(self, card, pci_base):
        self.card, self.pci_base = card, pci_base

    def __getitem__(self, k):
        if isinstance(k, slice):
            start, stop = k.start or 0, k.stop
            step = k.step or 1
            if step == 1:
                # весь срез - одним чтением: иначе на каждый байт приходится
                # вызов с разбором, и тесты записи 8 МиБ шли по 30 секунд
                return self.card.read(self.card.win_start + start, stop - start)
            return self.card.read(self.card.win_start + start,
                                  (stop - start + step - 1) // step)[::step]
        return self.card.read(self.card.win_start + k, 1)[0]

    def __setitem__(self, k, v):
        if isinstance(k, slice):
            step = k.step or 1
            if step == 1:
                # срез подряд - это запись НЕСКОЛЬКИХ байт одной командой
                # (так NorBus пишет слово Intel): модель должна это видеть.
                # Индекс окна переводим в смещение НА КАРТЕ (win_card + i) -
                # в модели всё считается в смещениях карты, не в адресах PCI.
                self.card.write_many(self.card.win_card + (k.start or 0), bytes(v))
                return
            for n, i in enumerate(range(k.start or 0, k.stop, step)):
                self.card.write(self.card.win_start + i, bytes([v[n]]))
        else:
            self.card.write(self.card.win_start + k, bytes([v]))

    def close(self):
        pass


def patch_iron(d):
    """Отвязать pcmcia_direct.py от /dev/mem: всё идёт через модель."""
    d.mem_for_addr = lambda mem, addr, writable=False, dev_id=None, verbose=False: mem


def make(kit=None, span=MIB, **kw):
    """(card, bus, flash) на модели. Карта-чип задаётся kw (chip_name и др.)."""
    card = ModelCard(kit=kit, **kw)
    d = card.d
    patch_iron(d)
    win = {"start": card.pci_base, "size": card.win_size}
    bus = d.NorBus(card, card.exca, win, span=span,
                   limit=card.chips * card.chip_size)
    geo = d.geometry_for(card.chip_name)
    flash = d.NorFlash(bus, chips=card.chips, sector=card.sector,
                       cmd=card.cmd, wide=card.wide,
                       page=geo.get("page") or 1,
                       bypass=bool(geo.get("bypass")),
                       # strata: у AMD/Fujitsu StrataFlash буферная запись идёт
                       # командами 25h/WC/29h. Без этого флага модель гоняла
                       # обычную страничную запись A0h и путь буфера не
                       # проверялся вообще (так ошибка 2.17.12 и прожила).
                       strata=bool(geo.get("strata")),
                       name=card.chip_name, protocol=card.protocol,
                       sector_map=geo.get("sectors") or None,
                       ro=bool(geo.get("ro")),
                       ro_why=str(geo.get("why") or ""))
    return card, bus, flash
