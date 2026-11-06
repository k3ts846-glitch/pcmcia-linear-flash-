#!/usr/bin/env bash
# Запускает команду, показывает хвост вывода, а при провале делает из него
# аннотацию к проверке GitHub Actions.
#
# Зачем: 1) в журнале видно последние строки даже когда полный лог недоступен;
#        2) причина провала попадает в аннотацию (её читают через API,
#           а сами логи Actions лежат на отдельном хосте и бывают недоступны).
#
#   .github/run-and-annotate.sh python tests/test_scripts.py
set +e

log=$(mktemp)
"$@" > "$log" 2>&1
rc=$?

tail -n 40 "$log"

if [ "$rc" -ne 0 ]; then
  # %…%0A — экранирование для workflow-команды ::error::
  msg=$(tail -c 1400 "$log" | sed -e 's/%/%25/g' -e 's/\r//g' | awk '{printf "%s%%0A", $0}')
  echo "::error title=$*::$msg"
fi

rm -f "$log"
exit "$rc"
