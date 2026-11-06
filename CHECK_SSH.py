#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CHECK_SSH.py — запускается на ноутбуке, когда ssh-сервер уже есть.

Ничего не устанавливает и не меняет настройки: только проверяет, что сюда
можно подключиться с Windows-машины, и печатает готовую команду.

    python3 ./CHECK_SSH.py              # без root
    sudo python3 ./CHECK_SSH.py         # подробнее (пароли, файрвол)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PORT = 22


def say(text: str = "") -> None:
    print(text)


def head1(title: str) -> None:
    say("")
    say(f"=== {title} ===")


def ok(text: str) -> None:
    say(f"  ok   {text}")


def warn(text: str) -> None:
    say(f"  !    {text}")


def have(name: str) -> bool:
    from shutil import which
    return which(name) is not None


def shell(command: str) -> str:
    try:
        res = subprocess.run(command, shell=True, capture_output=True, text=True)
        return res.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def main(argv: list[str] | None = None) -> int:
    global PORT

    args = sys.argv[1:] if argv is None else argv
    if args:
        try:
            PORT = int(args[0])
        except ValueError:
            warn(f"некорректный порт: {args[0]}, использую 22")
            PORT = 22

    is_root = os.geteuid() == 0

    head1("1. SSH-СЕРВЕР ЕСТЬ?")
    sshd_path = have("sshd") or ("/usr/sbin/sshd" if Path("/usr/sbin/sshd").is_file() else None)
    if sshd_path:
        ok(f"sshd установлен: {sshd_path}")
    else:
        warn("sshd НЕ найден. Установите: sudo apt install -y openssh-server")
    if have("systemctl"):
        r1 = subprocess.run(["systemctl", "is-active", "ssh"],
                            capture_output=True, text=True)
        r2 = subprocess.run(["systemctl", "is-active", "ssh.socket"],
                            capture_output=True, text=True)
        say(f"  ssh.service: {r1.stdout.strip()}   ssh.socket: {r2.stdout.strip()}")
        if r1.stdout.strip() != "active" and r2.stdout.strip() != "active":
            warn("служба не активна: sudo systemctl enable --now ssh")

    head1("2. НА КАКОМ ПОРТУ СЛУШАЕТ")
    listen_lines = []
    if have("ss"):
        r = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True)
        for line in r.stdout.splitlines():
            if "sshd" in line or f":{PORT} " in line:
                listen_lines.append(f"      {line}")
    if listen_lines:
        for line in listen_lines:
            say(line)
    else:
        warn("не видно слушающего sshd (без root ss не показывает процессы)")
        say("       проверьте сами: sudo ss -ltnp | grep ssh")

    r = subprocess.run(["sh", "-c", f"ss -ltn 2>/dev/null | grep -q ':{PORT} '"],
                       capture_output=True)
    if r.returncode == 0:
        ok(f"порт {PORT} слушается")
    else:
        other = []
        if have("ss"):
            r2 = subprocess.run(["ss", "-ltn"], capture_output=True, text=True)
            for line in r2.stdout.splitlines():
                for part in line.split():
                    if part.startswith(":"):
                        try:
                            p = int(part[1:])
                            if p != PORT:
                                other.append(str(p))
                        except ValueError:
                            pass
        other = sorted(set(other), key=int)[:3]
        warn(f"порт {PORT} не слушается")
        if other:
            say(f"       слушаются порты: {' '.join(other)}")
            say(f"       если sshd на другом порту — запустите: python3 ./CHECK_SSH.py <порт>")

    head1("3. ВХОД ПО ПАРОЛЮ РАЗРЕШЁН?")
    conf_eff = ""
    for cmd in ["sshd -T", "/usr/sbin/sshd -T"]:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if r.returncode == 0:
            conf_eff = r.stdout
            break
    if conf_eff:
        relevant = []
        for line in conf_eff.splitlines():
            low = line.lower()
            if any(k in low for k in ("permitrootlogin", "passwordauthentication",
                                       "kbdinteractiveauthentication",
                                       "pubkeyauthentication", "port ")):
                relevant.append(f"      {line}")
        if relevant:
            for line in relevant:
                say(line)
        if "passwordauthentication no" in conf_eff.lower():
            warn("вход по паролю ВЫКЛЮЧЕН (passwordauthentication no) — "
                 "включите его или используйте ключ")
        if "permitrootlogin no" in conf_eff.lower():
            warn("вход root запрещён (permitrootlogin no): "
                 "подключайтесь обычным пользователем (-User логин)")
    else:
        warn("не удалось получить действующие настройки "
             "(запустите с sudo: sudo python3 ./CHECK_SSH.py)")

    if is_root:
        st = shell("passwd -S root 2>/dev/null | awk '{print $2}'")
        if st == "P":
            ok("у root есть пароль")
        elif st == "L":
            warn("пароль root заблокирован: sudo passwd root")
        elif st == "NP":
            warn("у root нет пароля: sudo passwd root")
        else:
            say("  состояние пароля root неизвестно")

        if have("ufw"):
            r = subprocess.run(["ufw", "status"], capture_output=True, text=True)
            if "active" in r.stdout.lower():
                r2 = subprocess.run(["sh", "-c",
                                     f"ufw status 2>/dev/null | grep -qE '^{PORT}(/tcp)? '"],
                                    capture_output=True)
                if r2.returncode == 0:
                    ok(f"в ufw порт {PORT} открыт")
                else:
                    warn(f"ufw активен, порт {PORT} не открыт: sudo ufw allow {PORT}/tcp")

    head1("4. АДРЕС ДЛЯ WINDOWS-МАШИНЫ")
    ips_raw = shell("hostname -I 2>/dev/null | tr ' ' '\\n' | grep -E '^[0-9]+\\.' | grep -v '^127\\.' | head -4")
    ips = [x for x in ips_raw.splitlines() if x]
    if ips:
        for ip in ips:
            say(f"      {ip}")
    else:
        warn("адрес не определён: ip -4 addr | grep inet")
    first_ip = ips[0] if ips else "192.168.0.94"

    head1("5. КОМАНДА ДЛЯ WINDOWS-МАШИНЫ")
    say("  Распакуйте архив на Windows-машине и запустите в PowerShell:")
    say("")
    say(f"      .\\\\RUN.ps1 -HostName {first_ip} -Port {PORT}")
    say("")
    say("  Если входите не root (а в настройках permitrootlogin no — только так):")
    say(f"      .\\\\RUN.ps1 -HostName {first_ip} -User ВАШ_ЛОГИН")
    say("")
    say("  Сначала можно проверить, ничего не подключая:  -DryRun")
    return 0


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    sys.exit(main(sys.argv[1:]))
