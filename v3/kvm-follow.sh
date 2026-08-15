#!/bin/bash
# kvm-follow v3 — монитор следует за клавиатурой MX Keys Mini. Половина Mac mini.
#
# МЕХАНИЗМ ПЕРЕКЛЮЧЕНИЯ ВХОДА. LG 34WQ650 НЕ слушает стандартный VCP 0x60: вход
# переключается проприетарным регистром 0xF4 на I2C-адрес 0x50 — в m1ddc это
# `set input-alt`. Нужна HEAD-сборка m1ddc (brew-релиз 1.2.0 шлёт битую команду).
# Коды проверены вживую: 144 (0x90) = HDMI (mini), 465 (0x1D1) = USB-C (макбук).
# Код возврата m1ddc НИЧЕГО не значит (всегда 0), чтение регистров даёт мусор,
# поэтому надёжность — тройной посылкой (потери DDC у этого LG ~25-30% на пакет).
#
# АРХИТЕКТУРА v3 (15.08.2026). Каждая машина ТЯНЕТ монитор НА СЕБЯ, когда к ней
# приходит клавиатура, и НИКОГДА не переключает его «от себя» — гонки типа
# «протухшая команда со спящего макбука отменила уже сделанное переключение»
# исключены по построению.
#   - mini (этот скрипт): опрос blueutil; клавиатура пришла -> DDC на HDMI;
#     клавиатура ушла -> DDC на USB-C сразу (чтобы картинка не отставала от
#     клавиатуры) + SSH-побудка макбука в фоне (без каких-либо удержаний!).
#   - макбук (kvm-mb-agent.py, launchd com.kvm.mb): слушает приёмник Bolt;
#     клавиатура пришла -> будит себя, держит дисплей, дотягивает монитор
#     повторами m1ddc, когда его картинка реально поднялась; клавиатура ушла ->
#     возвращает мышь на mini (HID++ ChangeHost) и гасит свой дисплей.
#
# ЧТО УБРАНО ОТНОСИТЕЛЬНО v2 И ПОЧЕМУ. Вечное удержание дисплея макбука
# (caffeinate -d -s -t 90 с продлением по SSH каждые 40 с) удалено целиком:
# из-за него макбук в доке не спал вообще и жёг энергию как включённый.
# Теперь сон макбука — забота самого макбука: его агент держит дисплей только
# пока клавиатура у него, а постоянный caffeinate -s (действует только на
# питании) превращает clamshell-сон в DarkWake — SSH и агент живы, экран погашен.
# Цена: переключение на макбук из DarkWake занимает ~4-6 с вместо 2-3 с.
#
# ДЕТЕКТ. Только blueutil по ИМЕНИ устройства (по MAC blueutil ошибочно отдаёт 0).

KB="MX Keys Mini"
IN_MINI=144            # HDMI  (0x90)  — Mac mini
IN_MB=465              # USB-C (0x1D1) — макбук

MB_USER="semenmoshkin"
MB_HOSTS=("MacBook-Pro-2.local" "192.168.0.118")
SSH_KEY="/Users/semenmoshkin/.ssh/kvm_macbook"

POLL=0.3               # период опроса
SETTLE=0.3             # подтверждение против дребезга BT

DDC="/Users/semenmoshkin/bin/m1ddc"   # HEAD-сборка, с рабочим input-alt
BLUEUTIL=/opt/homebrew/bin/blueutil
LOG=/tmp/kvm-follow.log
STATEFILE=/tmp/kvm-follow.state       # последнее выставленное состояние, против флапа при рестарте

log() { echo "$(date '+%F %T') $*" >>"$LOG"; }

# ServerAlive* обязательны: сеанс через живой мастер-сокет к уснувшему хосту без них
# висит неограниченно. ControlPersist 120 — меньше шансов на протухший сокет.
SSH_OPTS=(-i "$SSH_KEY" -o ConnectTimeout=5 -o BatchMode=yes
          -o ServerAliveInterval=3 -o ServerAliveCountMax=2
          -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
          -o ControlMaster=auto -o ControlPath=/tmp/kvm-ssh-%h -o ControlPersist=120)

MB_ACTIVE=""
mb_ssh() {
  local h
  for h in "${MB_ACTIVE}" "${MB_HOSTS[@]}"; do
    [ -z "$h" ] && continue
    if ssh "${SSH_OPTS[@]}" "$MB_USER@$h" "$1" >/dev/null 2>&1; then
      MB_ACTIVE="$h"; return 0
    fi
  done
  MB_ACTIVE=""; return 1
}

# Побудка макбука в фоне, с повторами, отменяемая. ТОЛЬКО caffeinate -u — никаких
# удержаний: ими управляет агент на самом макбуке. Побудка — дублёр на случай
# полного сна (агент на макбуке в DarkWake будит себя сам по приходу клавиатуры).
# Спящий по-настоящему макбук по сети не просыпается — его будит первое нажатие
# клавиши через приёмник Bolt (0.4 с), тогда агент макбука дотянет монитор сам.
MB_BG=""
DDC_BG=""
mb_wake_bg() {
  [ -n "$MB_BG" ] && kill "$MB_BG" 2>/dev/null
  ( for i in 1 2 3 4; do
      [ "$(probe)" = "1" ] && exit 0        # вернулись на mini, будить больше некого
      mb_ssh "nohup caffeinate -u -t 5 >/dev/null 2>&1 & exit 0" && exit 0
      sleep 3
    done
    # СПАСЕНИЕ ОТ ЧЁРНОГО ЭКРАНА (15.08 12:20): макбук может быть в глубокой
    # гибернации (обесточили монитор -> батарея -> standby, USB мёртв, клавиша
    # не будит — только крышка). Держать пользователя на чёрном USB-C вечно —
    # хуже, чем показать mini при уехавшей клавиатуре: хоть видно, что случилось.
    if [ "$(probe)" = "0" ]; then
      log "[спасение] макбук молчит по SSH — возвращаю монитор на mini (разбудите макбук крышкой)"
      ddc_local "$IN_MINI"
    fi ) &
  MB_BG=$!
}

# ── МЫШЬ ─────────────────────────────────────────────────────────────────────
# Уход мыши mini->макбук больше не доверен Options+ (при быстрых циклах он
# перестаёт слать ChangeHost — 15.08, циклы 4-5). Пуш делаем сами по BLE:
# kvm-mouse-push.py шлёт мыши HID++ SetCurrentHost(0) = канал 1 = макбук.
# КОНВЕРГЕНТНОЕ ПРАВИЛО (страховка, а не событие): пока клавиатура НЕ у нас,
# а мышь ещё у нас — отталкиваем её, не чаще раза в 8 с. Обратное правило
# симметрично живёт в агенте макбука. Пинг-понг невозможен: каждый толкает
# только СВОЮ засидевшуюся мышь при чужой клавиатуре.
MOUSE="MX Master 3S"
MOUSE_CHECK_EVERY=8            # период правила, сек ($SECONDS — встроенный счётчик bash)
mouse_check_ts=0
mouse_push_bg() {
  ( [ "$(probe)" = "0" ] || exit 0     # клавиатура уже вернулась — мышь не трогаем
    # Через постоянный помощник (kvm-mouse-helper.sh, запущен из сессии с
    # Bluetooth-правами): напрямую из launchd BLE-мышь не открывается
    # (kIOReturnNotPermitted), и выбить промпт для фонового контекста нельзя.
    # Пишем только при ЖИВОМ помощнике: без читателя printf повис бы навечно.
    hp=$(cat /tmp/kvm-mouse-helper.pid 2>/dev/null)
    [ -n "$hp" ] && kill -0 "$hp" 2>/dev/null && [ -p /tmp/kvm-mouse.fifo ] &&
      printf '0\n' > /tmp/kvm-mouse.fifo ) &
}

# Тройная посылка: код возврата m1ddc фиктивен, проверить результат нечем,
# при потерях ~25-30% на пакет три посылки дают ~2% шанс полного промаха.
ddc_local() {
  local want="$1" i
  for i in 1 2 3; do
    [ "$i" -gt 1 ] && sleep 0.15
    "$DDC" set input-alt "$want" >/dev/null 2>&1
  done
  return 0
}

probe() { "$BLUEUTIL" --is-connected "$KB" 2>/dev/null; }

# Прогрев SSH-мастера в фоне: блокирующая проверка глушила бы опрос на 10 с.
( mb_ssh 'true' && log "SSH-мастер поднят: $MB_ACTIVE" || log "макбук недоступен" ) &

state=$(probe)
case "$state" in 0|1) ;; *) state=1 ;; esac
log "старт v3, состояние=$state"

# Выравнивание входа при старте — только если состояние разошлось с последним
# выставленным или отметка протухла (KeepAlive+ThrottleInterval=10: безусловное
# выравнивание при аварийном цикле рестартов дёргало бы экран каждые 10 с).
prev=$(cat "$STATEFILE" 2>/dev/null)
prev_mtime=$(stat -f %m "$STATEFILE" 2>/dev/null || echo 0)
prev_age=$(( $(date +%s) - prev_mtime ))
if [ "$prev" = "$state" ] && [ "$prev_age" -lt 300 ]; then
  log "  вход уже выровнен ($state), DDC не трогаю"
else
  if [ "$state" = "1" ]; then ddc_local "$IN_MINI"; else ddc_local "$IN_MB"; fi
fi
printf '%s' "$state" > "$STATEFILE"

while true; do
  sleep "$POLL"

  # Конвергенция мыши: клавиатура у макбука, а мышь всё ещё липнет к mini.
  # Проверка не чаще раза в MOUSE_CHECK_EVERY — чтобы не плодить процессы в опросе.
  if [ "$state" = "0" ] && [ $(( SECONDS - mouse_check_ts )) -ge "$MOUSE_CHECK_EVERY" ]; then
    mouse_check_ts=$SECONDS
    if [ "$("$BLUEUTIL" --is-connected "$MOUSE" 2>/dev/null)" = "1" ]; then
      log "[мышь] осталась на mini при клавиатуре на макбуке — пуш"
      mouse_push_bg
    fi
  fi

  now=$(probe)
  case "$now" in 0|1) ;; *) continue ;; esac
  [ "$now" = "$state" ] && continue

  t0=$(date +%s)
  sleep "$SETTLE"
  confirm=$(probe)
  [ "$confirm" != "$now" ] && { log "дребезг ($state->$now->$confirm), игнор"; continue; }
  state=$now
  printf '%s' "$state" > "$STATEFILE"

  if [ "$now" = "0" ]; then
    # Ушли на макбук: картинку уводим сразу (клавиатура уехала — печать вслепую
    # хуже пары секунд черноты), побудка в фоне. DDC-мастер ОДИН — mini:
    # посылки с макбука убраны (0xF4 умер после первых же команд со второго
    # порта, 15.08 01:00), вместо них mini досылает через 3 и 6.5 с — это
    # накрывает окно ренегоциации линка макбука (2.6-4.4 с) и его откаты.
    [ -n "$DDC_BG" ] && kill "$DDC_BG" 2>/dev/null
    mb_wake_bg
    mouse_push_bg
    ddc_local "$IN_MB"
    ( sleep 3;   [ "$(probe)" = "0" ] && "$DDC" set input-alt "$IN_MB" >/dev/null 2>&1
      sleep 3.5; [ "$(probe)" = "0" ] && "$DDC" set input-alt "$IN_MB" >/dev/null 2>&1 ) &
    DDC_BG=$!
    log "[уход] монитор -> макбук (USB-C) за $(( $(date +%s) - t0 ))с"
  else
    # Вернулись: mini всегда готов рисовать. Мышь возвращает агент макбука
    # (HID++ ChangeHost по уходу клавиатуры с Bolt) — здесь делать ничего не надо.
    [ -n "$MB_BG" ] && kill "$MB_BG" 2>/dev/null; MB_BG=""
    [ -n "$DDC_BG" ] && kill "$DDC_BG" 2>/dev/null; DDC_BG=""
    ddc_local "$IN_MINI"
    log "[возврат] монитор -> mini (HDMI) за $(( $(date +%s) - t0 ))с"
  fi
done
