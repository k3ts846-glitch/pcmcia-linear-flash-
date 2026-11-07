# -*- coding: utf-8 -*-
"""Питание сокета и Vpp: EXCA/PCI-конфигурация, TPS2220B, состояние Vpp.

Отдельно от моста: это регистры сокета, а не окна памяти."""

import mmap
import os

from .errors import DirectError


# ------------------------------------------------ управление питанием сокета ---

# Смещения в PCI конфигурационном пространстве моста (функция 0):
PCI_CONFIG_GENERAL_CONTROL = 0x86   # General Control Register, бит 10 = 12V_SW_SEL


PCI_CONFIG_SERIAL_BUS_DATA = 0xB0   # Serial Bus Data


PCI_CONFIG_SERIAL_BUS_INDEX = 0xB1  # Serial Bus Index (адрес байта)


PCI_CONFIG_SERIAL_BUS_SLAVE_ADDR = 0xB2  # Serial Bus Slave Address + R/W бит


PCI_CONFIG_SERIAL_BUS_CONTROL = 0xB3   # Serial Bus Control / Status


# ExCA регистры питания (относительно ExCA_BASE = BAR0 + 0x800):
EXCA_POWER_CONTROL_A = 0x02   # Card A: ExCA +02h


EXCA_POWER_CONTROL_B = 0x42   # Card B: ExCA +42h


# Биты ExCA Power Control:
EXCA_COE = 0x80       # бит 7: Card Output Enable


EXCA_AUTOPWRSWEN = 0x20  # бит 5: Auto Power Switch Enable


EXCA_CAPWREN = 0x10    # бит 4: VCC Enable


EXCA_EXCAVPP_MASK = 0x03   # биты 1-0: VPP (00=no conn, 01=VCC, 10=12V)


# Биты Socket Control Register (CardBus Socket Base Address + 0x10h):
SOC_VPPCTRL_MASK = 0x07   # биты 2-0: VPP (000=off, 001=12V, 010=5V, 011=3.3V)


SOC_VPPCTRL_OFF = 0x00


SOC_VPPCTRL_12V = 0x01


SOC_VPPCTRL_5V = 0x02


SOC_VPPCTRL_3V3 = 0x03


SOC_VCCCTRL_MASK = 0x70   # биты 6-4: VCC (000=off, 010=5V, 011=3.3V)


SOC_VCCCTRL_OFF = 0x00


SOC_VCCCTRL_5V = 0x20


SOC_VCCCTRL_3V3 = 0x30


def pci_config_path(dev):
    """Путь к конфигурационному пространству PCI-устройства в sysfs."""
    return f"/sys/bus/pci/devices/{dev}/config"


def pci_config_read(dev, offset, length=1):
    """Прочитать length байт из PCI конфигурационного пространства.

    Требует root (чтение /sys/bus/pci/devices/*/config).
    """
    try:
        with open(pci_config_path(dev), "rb") as f:
            f.seek(offset)
            return f.read(length)
    except OSError as error:
        raise DirectError(
            f"не удалось прочитать PCI конфиг {dev} офсет {offset:#x}: {error}"
        ) from error


def pci_config_write(dev, offset, value, length=1):
    """Записать value (int) в PCI конфигурационное пространство.

    length байт, младший байт первым (little-endian). Требует root.
    """
    try:
        data = value.to_bytes(length, "little")
        with open(pci_config_path(dev), "r+b") as f:
            f.seek(offset)
            f.write(data)
    except OSError as error:
        raise DirectError(
            f"не удалось записать PCI конфиг {dev} офсет {offset:#x}: {error}"
        ) from error


def socket_base_address(dev):
    """Базовый адрес регистров сокета из PCI конфига (смещение 0x10).

    Для CardBus-мостов PCI7x21/PCI7x11 здесь адрес базы регистров сокета.
    """
    data = pci_config_read(dev, 0x10, 4)
    return int.from_bytes(data, "little") & ~0x3  # убрать биты типа региона


def socket_control_read(dev):
    """Прочитать Socket Control Register (Socket Base + 0x10h).

    Читает через /dev/mem (физический адрес). Возвращает (vpp_bits, vcc_bits)
    или None при ошибке.
    """
    try:
        base = socket_base_address(dev)
        if not base:
            return None
        page = base & ~0xFFF
        offset = base & 0xFFF
        mem_fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        try:
            mm = mmap.mmap(mem_fd, 4096, mmap.MAP_SHARED,
                           mmap.PROT_READ | mmap.PROT_WRITE,
                           offset=page)
            reg_val = int.from_bytes(mm[offset:offset + 4], "little")
        finally:
            mm.close()
            os.close(mem_fd)
        vpp = reg_val & SOC_VPPCTRL_MASK
        vcc = (reg_val & SOC_VCCCTRL_MASK) >> 4
        return vpp, vcc
    except (OSError, ValueError, mmap.error, OverflowError):
        return None


def socket_control_write(dev, vpp=None, vcc=None):
    """Записать Socket Control Register.

    vpp: 0=off, 1=12V, 2=5V, 3=3.3V
    vcc: 0=off, 2=5V, 3=3.3V
    Возвращает True при успехе.
    """
    try:
        base = socket_base_address(dev)
        if not base:
            return False
        current = socket_control_read(dev)
        if current is None:
            return False
        vpp_cur, vcc_cur = current
        if vpp is not None:
            vpp_cur = vpp & SOC_VPPCTRL_MASK
        if vcc is not None:
            vcc_cur = vcc & 0x07
        reg_val = (vcc_cur << 4) | vpp_cur
        page = base & ~0xFFF
        offset = base & 0xFFF
        mem_fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        try:
            mm = mmap.mmap(mem_fd, 4096, mmap.MAP_SHARED,
                           mmap.PROT_READ | mmap.PROT_WRITE,
                           offset=page)
            mm[offset:offset + 4] = reg_val.to_bytes(4, "little")
            mm.flush()
        finally:
            mm.close()
            os.close(mem_fd)
        return True
    except (OSError, ValueError, mmap.error, OverflowError):
        return False


def exca_power_control_read(exca_mem, exca_addr, card_a=True):
    """Прочитать ExCA Power Control Register.

    card_a=True — смещение 02h (Card A), False — 42h (Card B).
    Возвращает байт регистра или None.
    """
    offset = EXCA_POWER_CONTROL_A if card_a else EXCA_POWER_CONTROL_B
    try:
        data = exca_mem.read(exca_addr + offset, 1)
        return data[0]
    except (DirectError, OSError, IndexError, TypeError):
        return None


def exca_power_control_write(exca_mem, exca_addr, value, card_a=True):
    """Записать ExCA Power Control Register.

    value: новые биты (только биты 7,5,4,1,0 изменяются, остальные сохраняются).
    """
    offset = EXCA_POWER_CONTROL_A if card_a else EXCA_POWER_CONTROL_B
    try:
        current = exca_mem.read(exca_addr + offset, 1)[0]
        new_val = (current & 0x04) | (value & 0xFB)  # бит 2 не трогаем
        exca_mem.write(exca_addr + offset, bytes([new_val]))
        return True
    except (DirectError, OSError, IndexError, TypeError):
        return False


def tps2220b_serial_write(dev, index, data_byte, slave_addr=0x00):
    """Записать через последовательную шину питания TPS2220B.

    Использует регистры Serial Bus (PCI конфиг B0h-B3h):
    - B0h: Serial Bus Data (данные)
    - B1h: Serial Bus Index (адрес байта в устройстве)
    - B2h: Serial Bus Slave Address (адрес устройства + R/W бит, младший бит=0 — запись)
    - B3h: Serial Bus Control/Status (запуск передачи, статус занятости)

    Протокол TPS2220B: данные передаются старшим битом вперёд по CLOCK,
    защёлка по LATCH. Точная структура серийного слова (какой бит за что
    отвечает) требует сверки с даташитом TPS2220B (TI SLVS554).
    """
    try:
        pci_config_write(dev, PCI_CONFIG_SERIAL_BUS_DATA, data_byte & 0xFF, 1)
        pci_config_write(dev, PCI_CONFIG_SERIAL_BUS_INDEX, index & 0xFF, 1)
        pci_config_write(dev, PCI_CONFIG_SERIAL_BUS_SLAVE_ADDR, slave_addr & 0xFE, 1)
        pci_config_write(dev, PCI_CONFIG_SERIAL_BUS_CONTROL, 0x01, 1)
        return True
    except DirectError:
        return False


def tps2220b_set_vpp(dev, voltage):
    """Установить VPP на TPS2220B через последовательную шину.

    voltage: 0 (off), 3.3, 5, 12 (В).
    Возвращает True при успехе, False при ошибке.

    Структура серийного слова TPS2220B (предположительная, требует проверки
    на железе по даташиту SLVS554):
      D0-D1: VPP (00=0V, 01=3.3V, 10=5V, 11=12V)
      D2-D3: VCC (00=0V, 01=3.3V, 10=5V, 11=Hi-Z)
    """
    vpp_bits = {0: 0b00, 3.3: 0b01, 5: 0b10, 12: 0b11}.get(voltage, 0b00)
    return tps2220b_serial_write(dev, index=0x00, data_byte=vpp_bits)


def vpp_state_names(vpp_bits):
    """Человеческое имя VPP по битам Socket Control Register."""
    return {0: "выключен", 1: "12 В", 2: "5 В", 3: "3,3 В"}.get(vpp_bits, "неизвестно")


def check_vpp_state(dev, exca_mem=None, exca_addr=None, card_a=True):
    """Проверить состояние VPP перед записью.

    Возвращает dict:
        {"vpp": "off"|"12V"|"5V"|"3.3V"|"unknown",
         "vcc": "off"|"5V"|"3.3V"|"unknown",
         "source": "socket_control"|"exca"|"unknown",
         "ok": bool}  — True если VPP включён (любое ненулевое напряжение)
    """
    result = {"vpp": "unknown", "vcc": "unknown",
              "source": "unknown", "ok": False}

    # 1. Через Socket Control Register (PCI7411 +10h)
    soc = socket_control_read(dev)
    if soc is not None:
        vpp_bits, vcc_bits = soc
        result["vpp"] = vpp_state_names(vpp_bits)
        result["vcc"] = vpp_state_names(vcc_bits)
        result["source"] = "socket_control"
        result["ok"] = vpp_bits != 0
        return result

    # 2. Через ExCA Power Control Register (fallback)
    if exca_mem is not None and exca_addr is not None:
        val = exca_power_control_read(exca_mem, exca_addr, card_a=card_a)
        if val is not None:
            exp_vpp = val & EXCA_EXCAVPP_MASK
            vpp_map = {0: "off", 1: "VCC (проброс)", 2: "12V"}
            result["vpp"] = vpp_map.get(exp_vpp, "unknown")
            result["vcc"] = "on" if (val & EXCA_CAPWREN) else "off"
            result["source"] = "exca"
            result["ok"] = exp_vpp != 0
            return result

    return result


def set_vpp_for_write(dev, voltage=5, exca_mem=None, exca_addr=None, card_a=True):
    """Включить VPP перед записью на карту.

    Пытается через Socket Control Register, затем ExCA Power Control,
    затем через последовательную шину TPS2220B.
    Возвращает (ok, сообщение).
    """
    vpp_bits = {3.3: SOC_VPPCTRL_3V3, 5: SOC_VPPCTRL_5V,
                12: SOC_VPPCTRL_12V}.get(voltage, SOC_VPPCTRL_5V)

    # 1. Socket Control Register
    if socket_control_write(dev, vpp=vpp_bits):
        return True, f"VPP {voltage} В включён через Socket Control Register"

    # 2. ExCA Power Control Register
    if exca_mem is not None and exca_addr is not None:
        exp_bits = {3.3: 1, 5: 1, 12: 2}.get(voltage, 1)  # 01=VCC, 10=12V
        exca_val = (exp_bits & 0x03) | EXCA_CAPWREN
        if exca_power_control_write(exca_mem, exca_addr, exca_val, card_a=card_a):
            return True, f"VPP {voltage} В включён через ExCA Power Control"

    # 3. Последовательная шина TPS2220B
    if tps2220b_set_vpp(dev, voltage):
        return True, f"VPP {voltage} В включён через TPS2220B (serial bus)"

    return False, f"не удалось включить VPP {voltage} В"
