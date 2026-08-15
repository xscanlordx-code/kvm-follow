#!/bin/bash
# kvm-mouse-helper — постоянный помощник пуша мыши.
#
# ЗАЧЕМ. Пуш мыши требует Bluetooth-доступа к BLE HID. TCC-права процесса
# фиксируются при его СТАРТЕ: помощник, запущенный из сессии с правами
# (Terminal/Claude), сохраняет их навсегда, даже когда родитель умер.
# launchd-агенты такого доступа не имеют (все попытки выбить промпт для
# фонового контекста провалились — хроника ночи 15.08).
#
# ПРОТОКОЛ. Читает строки из /tmp/kvm-mouse.fifo: "<host> [подстрока имени]".
#   "0"            -> мышь на канал 1 (макбук)
#   "1"            -> мышь на канал 2 (mini)
# Пишет свой pid в /tmp/kvm-mouse-helper.pid. Лог общий: /tmp/kvm-mouse-push.log.
#
# ЗАПУСК: вручную из терминала (или сессии Claude):
#   nohup ~/bin/kvm-mouse-helper.sh >/dev/null 2>&1 &
# После перезагрузки mini запустить заново тем же способом.

FIFO=/tmp/kvm-mouse.fifo
PIDFILE=/tmp/kvm-mouse-helper.pid
BIN=/Users/semenmoshkin/bin/kvmmouse-bin
LOG=/tmp/kvm-mouse-push.log

old=$(cat "$PIDFILE" 2>/dev/null)
[ -n "$old" ] && kill "$old" 2>/dev/null
echo $$ > "$PIDFILE"

[ -p "$FIFO" ] || { rm -f "$FIFO"; mkfifo "$FIFO"; }
echo "$(date '+%F %T') helper запущен, pid $$" >> "$LOG"

# Держим fifo открытым на чтение И запись: читатель существует всегда,
# писатели (printf из kvm-follow.sh) никогда не блокируются, EOF не бывает.
exec 3<>"$FIFO"
while read -r -u 3 host name; do
  [ -z "$host" ] && continue
  "$BIN" push "$host" "${name:-MX Master}" >/dev/null 2>&1
done
