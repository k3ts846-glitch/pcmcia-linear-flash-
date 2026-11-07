#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Перегенерировать §4 («Supported Flash Chips») в HW_REQUIREMENTS.md.

Зачем: §4 — это матрица поддержки микросхем, и её нельзя держать руками.
До 2.18.3 документ обещал «68 chip geometries», числил AT29F016/040/080 (таких
микросхем не существует) и писал Intel 28F008SA «Vpp not required», тогда как
таблица требует 12 В. Теперь §4 собирается из таблицы набора, а
tests/test_flash_regressions.py читает его обратно и сверяет построчно — так
документ и данные не могут разойтись молча.

Запуск: python3 tests/gen_hw_requirements.py
        (после любой правки размеров/секторов/вольт в chips_data.py или chips.json)
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(KIT, "src"))

import pcmcia_direct as D                                            # noqa: E402

GROUPS = [
    ("AMD / Fujitsu, 5 V JEDEC (AAh/55h unlock)",
     ("Am29F", "Am29LV", "MBM29F0", "MBM29F01", "MBM29F03")),
    ("AMD / Fujitsu StrataFlash and page-mode, 3 V (buffer 32 B)",
     ("Am29DL", "Am29GL", "Am29PL", "MBM29DL", "MBM29GL")),
    ("AMD Am28F, 12 V, chip erase only",
     ("Am28F",)),
    ("Intel CUI (commands by byte or word)",
     ("28F", "M28F", "D28F")),
    ("SST (uniform 4 KiB sectors)",
     ("SST",)),
    ("Macronix", ("MX",)),
    ("Toshiba", ("TC",)),
    ("Hitachi / Sharp / Mitsubishi (names not confirmed by a datasheet)",
     ("HN28F", "LH28F", "M5M29F")),
    ("Samsung NAND (refused: not NOR)",
     ("K9F",)),
    ("Atmel", ("AT",)),
]


def human(n):
    """Байты -> «2 MiB» / «512 KiB» (как в документе)."""
    return ("%d MiB" % (n >> 20)) if n % (1 << 20) == 0 else ("%d KiB" % (n >> 10))


def table_for(names):
    """Таблица одного семейства: ровно те столбцы, которые читает тест."""
    out = ["| Chip | Size | Sector | Protocol | Page | Bypass | Volts | Read-only | Datasheet |",
           "|------|------|--------|----------|------|--------|-------|-----------|-----------|"]
    for n in names:
        g = D.GEOMETRY[n]
        out.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            n, human(g["size"]), human(g["sector"]),
            "Intel CUI" if g.get("protocol") == "intel" else "JEDEC",
            "1 byte" if int(g.get("page") or 1) <= 1 else "%d B" % int(g["page"]),
            "Yes" if g.get("bypass") else "No",
            {12: "12 V", 5: "5 V", 3: "3 V"}.get(g.get("volts"),
                                                 "%s V" % g.get("volts")),
            "read-only" if g.get("ro") else "—",
            "NOT VERIFIED" if g.get("unverified") else "yes"))
    return "\n".join(out)


def group_of(name):
    """Семейство по префиксу имени."""
    for title, prefixes in GROUPS:
        for p in prefixes:
            if name.startswith(p):
                return title
    return "Other"


def build_section():
    """Собрать текст §4 целиком."""
    buckets = {}
    for name in sorted(D.GEOMETRY):
        buckets.setdefault(group_of(name), []).append(name)

    parts = []
    for title, _ in GROUPS:
        names = buckets.get(title) or []
        if names:
            parts.append("### %s\n\n%s\n" % (title, table_for(names)))
    if buckets.get("Other"):
        parts.append("### Other\n\n%s\n" % table_for(buckets["Other"]))

    unv = sorted(n for n, g in D.GEOMETRY.items() if g.get("unverified"))
    ro = sorted(n for n, g in D.GEOMETRY.items() if g.get("ro"))
    byp = sorted(n for n, g in D.GEOMETRY.items() if g.get("bypass"))

    head = """## 4. Supported Flash Chips

The chip tables (`src/pcmcia_direct/chips_data.py` and
`src/pcmcia_flash/data/chips.json`) describe **%d rows**. The matrix below is
generated from those tables (not typed by hand) and `tests/test_flash_regressions.py`
compares it back with the data on every run, so the document cannot drift.

Summary:

* rows requiring 12 V on Vpp: %d (`VPP126`), all other rows: %d (`VPP5`);
* rows that the kit only reads (`ro`, full-chip erase is the only erase mode, or
  the part is not NOR at all): %d — %s;
* rows marked **NOT VERIFIED** (parameters are not confirmed by a datasheet —
  20h and other optional commands are switched off for them): %d — %s;
* Unlock Bypass `20h` is enabled for %d rows and only where a datasheet shows
  the command.

""" % (len(D.GEOMETRY), len(D.VPP126), len(D.VPP5), len(ro), ", ".join(ro),
       len(unv), ", ".join(unv), len(byp))
    return head + "\n".join(parts)


def main():
    path = os.path.join(KIT, "HW_REQUIREMENTS.md")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    start = text.index("## 4. Supported Flash Chips")
    end = text.index("## 5. Voltage Requirements")
    new = text[:start] + build_section() + "\n---\n\n" + text[end:]
    if new == text:
        print("§4 уже совпадает с таблицами — менять нечего")
        return 0
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    print("§4 перегенерирован: строк %d" % len(D.GEOMETRY))
    return 0


if __name__ == "__main__":
    sys.exit(main())
