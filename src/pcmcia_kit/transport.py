#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Связь с ноутбуком по ssh и scp: запуск, копирование, живой вывод, ошибки.

Модуль ничего не знает про карту и про набор — только про транспорт. Его
задача — превратить вызовы ssh/scp в понятные функции и ГОВОРИТЬ ПО-РУССКИ,
когда что-то не так: неверный пароль, ноутбук не отвечает, на сервере
выключен sftp, сменился ключ хоста. Молчаливое «код возврата 255» бесполезно.

Проверяется в тестах поддельными ssh/scp (tests/test_win.py): настоящая сеть
для этого не нужна.
"""
import os
import shlex
import shutil
import subprocess
import sys
import threading

from . import console

# Общие опции ssh: не ждать вечно, принимать новый ключ хоста, не пробрасывать
# агента и X11 (они не нужны, а лишний проброс — лишняя дверь).
BASE_OPTIONS = [
    "-o", "ConnectTimeout=8",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ServerAliveInterval=15",
    "-o", "NumberOfPasswordPrompts=1",
    "-o", "ForwardAgent=no",
    "-o", "ForwardX11=no",
]

# Скорость появления процентов: строки вида « 91%  58.60 МиБ» печатаем только
# при смене процента, иначе на 64 МиБ экран забивается сотнями одинаковых.
import re

_PERCENT = re.compile(r"^\s*(\d{1,3})%")


class SshError(Exception):
    """Ошибка транспорта с человеческим объяснением."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.message = message
        self.code = code


def find_program(name):
    """Найти ssh/scp. Возвращает путь или None."""
    return shutil.which(name)


def _program_options(key_file=""):
    """Опции для ssh. Порт добавляется отдельно: у ssh это -p, у scp -P."""
    opts = list(BASE_OPTIONS)
    if key_file:
        opts += ["-i", key_file, "-o", "IdentitiesOnly=yes"]
    return opts


class Target:
    """Куда подключаемся и чем.

    Хранит адрес, пользователя, порт, ключ и общие опции. Отдельный класс,
    потому что порт у ssh и scp обозначается по-разному, и это единственное
    место, где об этом надо помнить.
    """

    def __init__(self, host, user="root", port=22, key_file="", dry_run=False):
        self.host = host
        self.user = user
        self.port = int(port)
        self.key_file = key_file
        self.dry_run = dry_run
        self.ssh = find_program("ssh")
        self.scp = find_program("scp")

    # --- представление ----------------------------------------------------
    @property
    def address(self):
        return f"{self.user}@{self.host}"

    def ssh_options(self):
        return ["-p", str(self.port)] + _program_options(self.key_file)

    def scp_options(self):
        return ["-P", str(self.port)] + _program_options(self.key_file)

    def has_key(self):
        return bool(self.key_file and os.path.isfile(self.key_file))

    # --- проверка ---------------------------------------------------------
    def require_programs(self):
        if self.ssh and self.scp:
            return
        missing = " и ".join(n for n, p in (("ssh", self.ssh), ("scp", self.scp)) if not p)
        raise SshError(
            "нет программы {0}. В Windows 10/11 клиент OpenSSH уже есть; если его "
            "нет, установите от администратора:\n"
            "     Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0"
            .format(missing), 2)

    # --- действия ---------------------------------------------------------
    def run(self, command, timeout=900):
        """Выполнить команду на ноутбуке. Возвращает (код, текст).

        Таймаут по умолчанию 2 часа: запись образа 4 МиБ на медленной карте
        может занять десятки минут, и 10 минут — недостаточно.
        """
        argv = [self.ssh] + self.ssh_options() + [self.address, command]
        if self.dry_run:
            console.say("  (пробный режим) " + " ".join(argv))
            return 0, ""
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            # Раньше timeout принимался, но в subprocess не передавался:
            # зависший ssh (например, сессия ждёт пароль) висел вечно.
            return 124, (f"команда не завершилась за {timeout} с и была "
                         f"прервана: {command}")
        text = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, text

    def run_live(self, command, timeout=900):
        """Выполнить команду и печатать вывод по мере поступления.

        Чтение идёт секунды и десятки секунд, а обычный вызов ждёт конца и не
        печатает ничего — со стороны это неотличимо от зависания. Здесь и
        stdout, и stderr читаются построчно и сразу показываются.

        Возвращает (код, весь текст).
        """
        argv = [self.ssh] + self.ssh_options() + [self.address, command]
        if self.dry_run:
            console.say("  (пробный режим) " + " ".join(argv))
            return 0, ""
        collected = []
        last_percent = -1
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                bufsize=1)
        expired = []

        def _stop():
            """Сторож: убить процесс, если он не уложился в таймаут."""
            expired.append(True)
            try:
                proc.kill()
            except OSError:
                pass

        guard = threading.Timer(timeout, _stop)
        guard.daemon = True
        guard.start()
        try:
            for raw in proc.stdout:
                collected.append(raw)
                # Возврат каретки — тоже граница строки: так перерисовываются
                # проценты у pcmcia_direct.py.
                for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                    if not line.strip():
                        continue
                    m = _PERCENT.match(line)
                    if m:
                        pct = int(m.group(1))
                        if pct == last_percent:
                            continue
                        last_percent = pct
                    console.say(line)
        finally:
            guard.cancel()
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=30)
        if expired:
            console.say(f"  прервано по таймауту {timeout} с")
            return 124, "".join(collected)
        return proc.returncode, "".join(collected)

    def copy_to(self, files, destination):
        """Скопировать файлы на ноутбук (scp)."""
        argv = [self.scp] + self.scp_options() + list(files) + [destination]
        return self._scp(argv)

    def copy_from(self, remote, local):
        """Забрать файл с ноутбука (scp)."""
        argv = [self.scp] + self.scp_options() + [remote, local]
        return self._scp(argv)

    def _scp(self, argv):
        if self.dry_run:
            console.say("  (пробный режим) " + " ".join(argv))
            return 0, ""
        proc = subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        text = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0 and _looks_like_sftp_problem(text):
            # Новые scp работают через подсистему sftp. Если на ноутбуке её
            # нет, достаточно попросить старый протокол (-O) — править
            # настройки чужой машины не нужно.
            console.say("  scp не смог через sftp — пробую старый протокол (-O)")
            argv2 = argv[:1] + ["-O"] + argv[1:]
            proc2 = subprocess.run(argv2, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace")
            text2 = (proc2.stdout or "") + (proc2.stderr or "")
            if proc2.returncode == 0:
                console.say("  получилось по старому протоколу (sftp на ноутбуке не нужен)")
            return proc2.returncode, text2
        return proc.returncode, text

    def close(self):
        """Закрыть общее соединение, если оно использовалось."""
        # Соединения не переиспользуем (в Python это не нужно: пароль
        # спрашивает сам ssh, а BatchMode-настройки не меняются), поэтому
        # закрывать нечего. Метод оставлен, чтобы вызывающий код не зависел
        # от способа транспорта.
        return None


def _looks_like_sftp_problem(text):
    low = text.lower()
    return ("subsystem request failed" in low or "sftp" in low
            or "not a valid command" in low)


def explain(text):
    """Объяснить по-русски частые отказы ssh. True — если объяснение дано."""
    if "Permission denied" in text:
        console.say("")
        console.say("  ВХОД НЕ ВЫПОЛНЕН: неверный пароль или вход запрещён.")
        console.say("  1) проверьте раскладку и Caps Lock;")
        console.say("  2) вход root: на ноутбуке  grep -i permitrootlogin /etc/ssh/sshd_config")
        console.say("  3) свой пользователь: python -m pcmcia_kit info --user имя --host <адрес>")
        console.say("  4) без пароля вообще: python -m pcmcia_kit key --host <адрес>")
        console.say("  Ничего не изменено: файлы не копировались, на карту не писали.")
        return True
    if any(word in text for word in ("Connection refused", "No route to host",
                                     "timed out", "Could not resolve")):
        console.say("")
        console.say("  НОУТБУК НЕ ОТВЕЧАЕТ.")
        console.say("  Проверьте: включён ли ноутбук, тот ли адрес (на ноутбуке: hostname -I),")
        console.say("  работает ли sshd (sudo systemctl status ssh), не блокирует ли файрвол.")
        return True
    if "Host key verification failed" in text:
        console.say("")
        console.say("  КЛЮЧ ХОСТА ИЗМЕНИЛСЯ (переустановка системы на ноутбуке).")
        console.say("  Удалите старую запись: ssh-keygen -R <адрес>")
        return True
    return False


def quote_for_remote(text):
    """Заключить строку в кавычки для удалённой оболочки."""
    return shlex.quote(text)


if __name__ == "__main__":       # pragma: no cover - ручная проверка
    console.enable_utf8()
    console.say("ssh: " + str(find_program("ssh")))
    console.say("scp: " + str(find_program("scp")))
    sys.exit(0)
