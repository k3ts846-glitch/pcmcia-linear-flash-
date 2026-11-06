#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Константы команд и битов статуса для линейной флеш-памяти (JEDEC / Intel CUI).

Вынесены из pcmcia_direct.py в отдельный модуль, чтобы не дублировать
значения между ноутбуком (pcmcia_direct.py) и моделью (tests/nor_model.py).
"""

# --- адреса команд внутри чипа (JEDEC / AMD / Fujitsu) ----------------------
NOR_CMD_ADDR1 = 0x555      # адрес первой команды внутри чипа
NOR_CMD_ADDR2 = 0x2AA      # адрес второй команды внутри чипа

NOR_RESET = 0xF0
NOR_PROGRAM = 0xA0
NOR_BYPASS = 0x20          # Unlock Bypass: вход в режим быстрой записи
NOR_ERASE_SETUP = 0x80
NOR_ERASE_SECTOR = 0x30
NOR_ERASE_CHIP = 0x10
NOR_UNLOCK1 = 0xAA
NOR_UNLOCK2 = 0x55
NOR_SECTOR_PROTECT = 0xA6  # чтение состояния защиты групп секторов (Am29F016D)
NOR_ERASE_SUSPEND = 0xB0   # приостановка стирания (Am29F016D)
NOR_ERASE_RESUME = 0x30    # продолжение стирания (совместимо с командой сектора)

# --- биты состояния DQ -------------------------------------------------------
DQ7 = 0x80                 # готовность операции (toggle / остаётся 1)
DQ5 = 0x20                 # выход за пределы времени операции

# --- статусный регистр Intel CUI ---------------------------------------------
SR_READY = 0x80            # WSMS: операция завершена
SR_ERASE_FAIL = 0x20       # ESS: стирание не удалось
SR_WRITE_FAIL = 0x10       # BWS: запись не удалось
SR_VPP_LOW = 0x08          # VPPS: нет напряжения программирования
# SR.6: стирание приостановлено (B0h). WSMS при этом = 1, поэтому проверять
# отдельно, иначе приостановленное стирание выглядит завершённым (290429-008).
SR_SUSPEND = 0x40
SR_PROTECT = 0x02          # WPPS: операция попала в защищённый блок
                           # (у 28Fxxx снимается только 12 В на RP#,
                           # erratum #2 в 297796-007)

# --- команды Intel CUI -------------------------------------------------------
INTEL_CUI_RESET = 0xFF
INTEL_CUI_READ_ID = 0x90
INTEL_CUI_READ_SR = 0x70
INTEL_CUI_CLEAR_SR = 0x50
INTEL_CUI_ERASE_SETUP = 0x20
INTEL_CUI_ERASE_CONFIRM = 0xD0
INTEL_CUI_WRITE_SETUP = 0x40
INTEL_CUI_ALT_WRITE = 0x10
INTEL_CUI_SUSPEND = 0xB0

# --- буферная запись Intel StrataFlash (25h / D0h) --------------------------
# Intel CUI: Write to Buffer = E8h + счётчик N + D0h (25h - это AMD)
INTEL_BUFFER_SETUP = 0xE8
INTEL_BUFFER_CONFIRM = 0xD0
INTEL_BUFFER_SIZE = 32     # слов на чип = 32 байта на чип

# --- буферная запись AMD / Fujitsu StrataFlash (25h / D0h) -------------------
AMD_BUFFER_SETUP = 0x25
AMD_BUFFER_CONFIRM = 0xD0
AMD_BUFFER_SIZE = 32       # слов на чип = 32 байта на чип

# --- общие параметры флеш-памяти ----------------------------------------------
SECTOR = 64 * 1024         # блок стирания этой семьи микросхем (байт)
FLASH_POLLS = 300000       # сколько раз опрашивать статус, прежде чем сдаться

# Intel использует 0xFF как сброс (отличается от JEDEC 0xF0)
INTEL_RESET = 0xFF
INTEL_READ_ID = 0x90
INTEL_READ_CFI = 0x98
AMD_RESET = 0xF0
AMD_READ_ID = 0x90
AMD_READ_CFI = 0x98

__all__ = [
    "NOR_CMD_ADDR1", "NOR_CMD_ADDR2",
    "NOR_RESET", "NOR_PROGRAM", "NOR_BYPASS",
    "NOR_ERASE_SETUP", "NOR_ERASE_SECTOR", "NOR_ERASE_CHIP",
    "NOR_UNLOCK1", "NOR_UNLOCK2",
    "NOR_SECTOR_PROTECT", "NOR_ERASE_SUSPEND", "NOR_ERASE_RESUME",
    "DQ7", "DQ5",
    "SR_READY", "SR_ERASE_FAIL", "SR_WRITE_FAIL", "SR_VPP_LOW", "SR_SUSPEND",
    "INTEL_CUI_RESET", "INTEL_CUI_READ_ID", "INTEL_CUI_READ_SR",
    "INTEL_CUI_CLEAR_SR", "INTEL_CUI_ERASE_SETUP", "INTEL_CUI_ERASE_CONFIRM",
    "INTEL_CUI_WRITE_SETUP", "INTEL_CUI_ALT_WRITE", "INTEL_CUI_SUSPEND",
    "INTEL_BUFFER_SETUP", "INTEL_BUFFER_CONFIRM", "INTEL_BUFFER_SIZE",
    "AMD_BUFFER_SETUP", "AMD_BUFFER_CONFIRM", "AMD_BUFFER_SIZE",
    "SECTOR", "FLASH_POLLS",
    "INTEL_RESET", "INTEL_READ_ID", "INTEL_READ_CFI",
    "AMD_RESET", "AMD_READ_ID", "AMD_READ_CFI",
]
