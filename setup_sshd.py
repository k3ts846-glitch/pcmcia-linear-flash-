#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""setup_sshd.py — запускается на ноутбуке (втором компьютере).

Делает из «пустой» машины готовый приёмник для remote_collect.py:
  1) ставит ssh-сервер (openssh-server), если его нет;
  2) включает вход по паролю (и по ключу), при желании — для root;
  3) запускает и включает автозапуск;
  4) открывает порт в ufw, если файрвол включён;
  5) печатает адрес ноутбука и готовую команду для Windows-машины.

Карта и PCMCIA не затрагиваются вообще: скрипт работает только с SSH.

Использование:
    sudo python3 ./setup_sshd.py                    # порт 22, root может входить
    sudo python3 ./setup_sshd.py --port 2222
    sudo python3 ./setup_sshd.py --no-root           # вход только обычным пользователям
    sudo python3 ./setup_sshd.py --set-password      # сразу задать пароль root
    sudo python3 ./setup_sshd.py --dry-run           # только показать, что будет сделано
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

PORT = 22
ALLOW_ROOT = True
SET_PASS = False
DRY = False


def die(msg: str) -> None:
    print(f"ОШИБКА: {msg}", file=sys.stderr)
    sys.exit(2)


def say(text: str = "") -> None:
    print(text)


def head1(title: str) -> None:
    say("")
    say(f"=== {title} ===")


def ok(text: str) -> None:
    say(f"  ok   {text}")


def warn(text: str) -> None:
    say(f"  !    {text}")


def bad(text: str) -> None:
    say(f"  !!!  {text}")


def run_cmd(*args: str) -> bool:
    """Выполнить команду (или показать, что было бы, в режиме --dry-run)."""
    if DRY:
        say(f"  [dry-run] {' '.join(args)}")
        return True
    try:
        res = subprocess.run(args, capture_output=True, text=True)
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def have(name: str) -> bool:
    from shutil import which
    return which(name) is not None


def is_root() -> bool:
    return os.geteuid() == 0


def shell(command: str) -> str:
    try:
        res = subprocess.run(command, shell=True, capture_output=True, text=True)
        return res.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


import argparse


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="setup_sshd.py",
        description="Настроить SSH-сервер на ноутбуке для приёма диагностики.",
    )
    p.add_argument("--port", type=int, default=22, help="порт SSH (по умолчанию 22)")
    p.add_argument("--no-root", action="store_true",
                   help="вход только обычным пользователям (root запрещён)")
    p.add_argument("--set-password", action="store_true",
                   help="сразу задать пароль root")
    p.add_argument("--dry-run", action="store_true",
                   help="только показать, что будет сделано")
    return p


def main(argv: list[str] | None = None) -> int:
    global PORT, ALLOW_ROOT, SET_PASS, DRY

    parser = build_parser()
    args = parser.parse_args(argv)
    PORT = args.port
    ALLOW_ROOT = not args.no_root
    SET_PASS = args.set_password
    DRY = args.dry_run

    if not is_root():
        die("нужен root: sudo python3 ./setup_sshd.py")

    CONF = Path("/etc/ssh/sshd_config.d/99-pcmcia.conf")
    SVC = "ssh"

    head1("1. СИСТЕМА")
    pretty = shell('. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME"') or "неизвестно"
    say(f"  дистрибутив: {pretty}")
    say(f"  ядро:        {shell('uname -sr')}")
    say(f"  архитектура: {shell('uname -m')}   (набор рассчитан на i386/i686)")
    arch = shell("uname -m")
    if arch.startswith(("i", "i?86")):
        ok("32-битная система — как нужно")
    elif arch == "x86_64":
        warn("64-битная система: набор собран под 32-битную, часть проверок не сработает")
    else:
        warn("не x86: набор для этой архитектуры не предназначен")

    head1("2. SSH-СЕРВЕР")
    have_sshd = have("sshd") or Path("/usr/sbin/sshd").is_file()
    if have_sshd:
        ok("sshd уже установлен")
    else:
        warn("sshd не установлен — поставлю")

    if not have("apt-get"):
        bad("нет apt-get: этот скрипт рассчитан на Debian/Ubuntu.")
        say("  На другой системе поставьте ssh-сервер вручную и включите вход по паролю.")
    elif not have_sshd:
        if DRY:
            say("  [dry-run] apt-get update && apt-get install -y openssh-server")
        else:
            say("  ставлю openssh-server (нужен интернет)...")
            r1 = subprocess.run("apt-get update -qq", shell=True, capture_output=True, text=True)
            r2 = subprocess.run("apt-get install -y -qq openssh-server", shell=True,
                                capture_output=True, text=True)
            if r1.returncode == 0 and r2.returncode == 0:
                ok("openssh-server установлен")
            else:
                bad("не удалось поставить openssh-server.")
                say("  Проверьте интернет:  ping -c2 deb.debian.org")
                say("  И повторите:         sudo apt-get install -y openssh-server")
                return 3

    head1("3. НАСТРОЙКА ВХОДА")
    permit_root = "yes" if ALLOW_ROOT else "no"
    conf_lines = [
        "# создано setup_sshd.py из набора pcmcia-ssh-kit",
        f"Port {PORT}",
        f"PermitRootLogin {permit_root}",
        "PasswordAuthentication yes",
        "KbdInteractiveAuthentication yes",
        "PubkeyAuthentication yes",
        "UsePAM yes",
    ]
    if DRY:
        say(f"  [dry-run] записал бы в {CONF}:")
        for line in conf_lines:
            say(f"      {line}")
    else:
        CONF.parent.mkdir(parents=True, exist_ok=True)
        CONF.write_text("\n".join(conf_lines) + "\n", encoding="utf-8")
        ok(f"настройки записаны: {CONF}")

    head1("4. ЗАПУСК СЛУЖБЫ")
    socket_active = False
    if have("systemctl"):
        r = subprocess.run(["systemctl", "is-active", "ssh.socket"],
                           capture_output=True, text=True)
        socket_active = r.returncode == 0

    if PORT != 22 and socket_active:
        warn(f"sshd запускается через ssh.socket: порт {PORT} без правки сокета не заработает")
        say("       отключаю socket-активацию и включаю обычную службу ssh.service")
        run_cmd("systemctl", "disable", "--now", "ssh.socket")
        say("       (вернуть как было: systemctl enable --now ssh.socket)")

    if have("systemctl"):
        run_cmd("systemctl", "enable", SVC)
        run_cmd("systemctl", "restart", SVC)
        if not DRY:
            ok(f"служба {SVC} включена в автозапуск и перезапущена")
    else:
        run_cmd("service", SVC, "restart")
        if not DRY:
            ok("служба перезапущена (service)")
        if DRY == 0:
            r = subprocess.run(["sh", "-c", f"ss -ltn 2>/dev/null | grep -q ':{PORT} '"],
                               capture_output=True)
            if r.returncode != 0:
                run_cmd("/usr/sbin/sshd", "-f", "/etc/ssh/sshd_config")
                ok("sshd запущен напрямую")

    head1("5. ФАЙРВОЛ")
    if have("ufw"):
        r = subprocess.run(["ufw", "status"], capture_output=True, text=True)
        if "active" in r.stdout.lower():
            if DRY:
                say(f"  [dry-run] ufw allow {PORT}/tcp")
            else:
                r2 = subprocess.run(["ufw", "allow", f"{PORT}/tcp"],
                                    capture_output=True, text=True)
                if r2.returncode == 0:
                    ok(f"в ufw открыт порт {PORT}/tcp")
        else:
            ok("ufw не активен (или не установлен) — порт открывать не нужно")
    else:
        ok("ufw не активен (или не установлен) — порт открывать не нужно")

    head1("6. ПРОВЕРКА")
    listen = False
    if not DRY:
        for _ in range(5):
            r = subprocess.run(["sh", "-c", f"ss -ltn 2>/dev/null | grep -q ':{PORT} '"],
                               capture_output=True)
            if r.returncode == 0:
                listen = True
                break
            import time
            time.sleep(1)

    if DRY:
        say("  [dry-run] проверка не выполняется")
    elif listen:
        ok(f"порт {PORT} слушается")
        banner = ""
        if have("timeout"):
            r = subprocess.run(
                ["timeout", "4", "bash", "-c",
                 f"exec 3<>/dev/tcp/127.0.0.1/{PORT}; head -c 40 <&3"],
                capture_output=True, text=True,
            )
            banner = r.stdout.strip()
        if banner.startswith("SSH-2.0-"):
            ok("сервер отвечает строкой SSH-2.0")
        elif not banner:
            warn("порт открыт, но баннер не прочитан (это не всегда ошибка)")
        else:
            warn(f"неожиданный ответ: {banner}")
    else:
        bad(f"порт {PORT} не слушается")
        say("  Что реально слушается сейчас:")
        r = subprocess.run(
            ["sh", "-c", f"ss -ltn 2>/dev/null | grep -E ':(22|{PORT}) '"],
            capture_output=True, text=True,
        )
        for line in r.stdout.splitlines():
            say(f"      {line}")
        if not r.stdout.strip():
            say("      (ничего похожего)")
        sock_active = subprocess.run(["systemctl", "is-active", "ssh.socket"],
                                     capture_output=True, text=True).stdout.strip()
        say(f"  Сокет-активация: {sock_active or 'нет'}")
        say("  Посмотрите почему:    journalctl -u ssh -n 30 --no-pager")
        say(f"  Или запустите вручную: /usr/sbin/sshd -d -p {PORT}")

    head1("7. ПАРОЛЬ")
    if SET_PASS:
        if DRY:
            say("  [dry-run] passwd root")
        else:
            say("  Сейчас задайте пароль root (он понадобится для входа по SSH):")
            r = subprocess.run(["passwd", "root"])
            if r.returncode != 0:
                warn("пароль не изменён")
    elif not ALLOW_ROOT:
        ok("вход root запрещён (--no-root): пароль root для SSH не нужен")
    else:
        st = shell("passwd -S root 2>/dev/null | awk '{print $2}'")
        if st == "P":
            ok("у root есть пароль")
        elif st == "L":
            warn("пароль root заблокирован. Задайте его:  sudo passwd root")
            say("       (или запустите этот скрипт с ключом --set-password)")
        elif st == "NP":
            warn("у root нет пароля. Задайте его:  sudo passwd root")
        else:
            warn("состояние пароля root неизвестно — проверьте: sudo passwd root")

    head1("8. ЧТО ДАЛЬШЕ (НА WINDOWS-МАШИНЕ)")
    ips_raw = shell("hostname -I 2>/dev/null | tr ' ' '\\n' | grep -E '^[0-9]+\\.' | grep -v '^127\\.' | head -4")
    ips = [x for x in ips_raw.splitlines() if x]
    say("  адрес(а) этого ноутбука:")
    if ips:
        for ip in ips:
            say(f"      {ip}")
    else:
        say("      (не удалось определить; посмотрите: ip -4 addr | grep inet)")
    say("")
    say("  В папке «для-windows» лежат три файла. Скопируйте их на Windows-машину")
    say("  в одну папку (например D:\\\\pcmcia-flash-kit) и запустите там:")
    say("")
    first_ip = ips[0] if ips else "192.168.0.94"
    say(f"      .\\remote_collect.py --host {first_ip} --port {PORT}")
    say("")
    say("  Если входите не root — добавьте --user ВАШ_ЛОГИН (на ноутбуке нужен sudo).")
    say("")
    say("  Можно и без Windows: запустите отчёт прямо здесь, с картой в слоте:")
    say("      sudo python3 ./collect_all.py")
    say("")
    say("  Подробности: 00_ПРОЧТИ_МЕНЯ.txt")
    return 0


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    sys.exit(main(sys.argv[1:]))
