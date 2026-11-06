#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Полный отчёт: все разделы диагностики одним файлом."""
import os

from .. import steps
from .. import remote as rctl


def run(target, kit_dir, work_dir=""):
    """Собрать отчёт и забрать его к себе."""
    remote_cmd = rctl.build("report")
    steps.describe(target, "report", remote_cmd, "")
    if not steps.connect(target):
        return 1
    if not steps.send_kit(target, kit_dir):
        return 1
    steps.skip_image("report")
    code, text = steps.execute(target, remote_cmd, "report")
    if code == 3:
        steps.needs_sudo_error(target, "report")
        return 3
    collect_code = steps.collect(target, "report", "", work_dir or os.getcwd())
    steps.finish(target)
    return collect_code or code
