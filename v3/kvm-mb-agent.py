#!/usr/bin/env python3
# kvm-mb-agent — макбучная половина KVM-схемы. Живёт на макбуке под launchd (com.kvm.mb).
#
# ЗАЧЕМ. Раньше всю работу делал Mac mini по SSH: будил макбук, ставил ему вечное
# удержание дисплея (caffeinate -d -s, продление каждые 40 с) — макбук в доке не спал
# вообще и жёг энергию, а мышь не возвращалась на mini, потому что командовать ей
# может только та машина, на чьём канале она сейчас стоит.
#
# ЧТО ДЕЛАЕТ. Слушает приёмник Bolt (HID++ по vendor-интерфейсу, page 0xFF00):
#   клавиатура ПРИШЛА (нажали Easy-Switch 1) ->
#       разбудить себя (caffeinate -u), держать дисплей пока клавиатура здесь и есть
#       питание (caffeinate -d), перетянуть монитор на себя (m1ddc set input-alt 465,
#       с повторами — DDC у LG теряет команды, а после пробуждения линк поднимается 2-4 с);
#   клавиатура УШЛА (нажали Easy-Switch 2) ->
#       СРАЗУ послать мыши HID++ 0x1814 ChangeHost на хост mini (это чинит «мышь не
#       возвращается»), снять удержание дисплея, погасить дисплей (pmset displaysleepnow,
#       только при закрытой крышке) — через ~5 с макбук уходит в DarkWake.
#
# ЭНЕРГИЯ. Постоянно висит только caffeinate -s (привязан к жизни агента через -w):
# на питании он превращает clamshell-сон в DarkWake (SSH и этот агент живы, дисплей
# погашен, потребление минимально), на батарее по man ИГНОРИРУЕТСЯ — унесённый макбук
# спит как обычно. caffeinate -d живёт ТОЛЬКО пока клавиатура на макбуке и есть питание;
# сторож раз в 30 с убивает его, если питание пропало. Оба caffeinate с -w $$ — умирают
# вместе с агентом, сирот (как двухчасовая сирота 14.08) больше быть не может.
#
# ПРОТОКОЛ (выяснено probe-скриптами 15.08.2026, лог в хронике):
#   - vendor-интерфейс Bolt: usage_page 0xFF00, короткие HID++ репорты 0x10 работают,
#     длинные регистровые — нет (err 0x01); pairing info (0xB5) Bolt не отдаёт (err 0x02);
#   - карта слотов через fake device arrival (reg 0x02 <- 02): слот/тип/wpid/линк;
#     сейчас: слот 6 клавиатура B369, слот 2 мышь B034 — но карта строится динамически;
#   - нотификации: reg 0x00 <- 00 01 00 (флаг в RAM приёмника, включаем при каждом старте);
#   - хосты мыши 0-based: 0 = макбук (Bolt), 1 = mini (BT), 2 = мёртвый третий канал.
import ctypes, os, signal, subprocess, sys, threading, time
from collections import deque

LIB        = "/opt/homebrew/lib/libhidapi.dylib"
M1DDC      = "/opt/homebrew/bin/m1ddc"
INPUT_MB   = "465"          # USB-C (0x1D1) — вход монитора, на котором макбук
MOUSE_HOST_MINI = 1         # 0-based хост мыши: 1 = канал 2 = Mac mini
VID, PID   = 0x046D, 0xC548 # приёмник Logi Bolt
TYPE_KB, TYPE_MOUSE = 1, 2  # тип устройства в нотификации 0x41 (младший ниббл info)
SWID       = 0x0D           # наш software id в HID++2.0 — фильтр от трафика Options+
LOG        = "/tmp/kvm-mb.log"
RESYNC_SEC = 300            # период самопроверки состояния (fake arrival)
WATCH_SEC  = 30             # период сторожа питания
MOUSE_PUSH_COOLDOWN = 8     # конвергенция мыши: не чаще раза в N секунд

def log(msg):
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 5_000_000:
            os.rename(LOG, LOG + ".old")
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%F %T')} {msg}\n")
    except OSError:
        pass

def hx(b): return " ".join(f"{x:02x}" for x in b)

def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout

def on_ac():
    return "AC Power" in sh(["pmset", "-g", "batt"])

def clamshell_closed():
    return "\"AppleClamshellState\" = Yes" in sh(["ioreg", "-r", "-k", "AppleClamshellState", "-d", "1"])

# ── hidapi через ctypes ──────────────────────────────────────────────────────
class HidDeviceInfo(ctypes.Structure): pass
HidDeviceInfo._fields_ = [
    ("path", ctypes.c_char_p), ("vendor_id", ctypes.c_ushort), ("product_id", ctypes.c_ushort),
    ("serial_number", ctypes.c_wchar_p), ("release_number", ctypes.c_ushort),
    ("manufacturer_string", ctypes.c_wchar_p), ("product_string", ctypes.c_wchar_p),
    ("usage_page", ctypes.c_ushort), ("usage", ctypes.c_ushort),
    ("interface_number", ctypes.c_int), ("next", ctypes.POINTER(HidDeviceInfo)),
]
hid = ctypes.CDLL(LIB)
hid.hid_init.restype = ctypes.c_int
hid.hid_enumerate.restype = ctypes.POINTER(HidDeviceInfo)
hid.hid_enumerate.argtypes = [ctypes.c_ushort, ctypes.c_ushort]
hid.hid_free_enumeration.argtypes = [ctypes.POINTER(HidDeviceInfo)]
hid.hid_open_path.restype = ctypes.c_void_p
hid.hid_open_path.argtypes = [ctypes.c_char_p]
hid.hid_close.argtypes = [ctypes.c_void_p]
hid.hid_write.restype = ctypes.c_int
hid.hid_write.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
hid.hid_read_timeout.restype = ctypes.c_int
hid.hid_read_timeout.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int]

class Receiver:
    def __init__(self):
        self.h = None
        self.backlog = deque()      # репорты, пришедшие во время rpc-ожиданий

    def open(self):
        self.close()
        path = None
        head = hid.hid_enumerate(VID, PID)
        p = head
        while p:
            d = p.contents
            if d.usage_page == 0xFF00:
                path = bytes(d.path)
                break
            p = d.next
        if head: hid.hid_free_enumeration(head)
        if not path:
            return False
        self.h = hid.hid_open_path(path)
        return bool(self.h)

    def close(self):
        if self.h:
            hid.hid_close(self.h)
            self.h = None

    def write(self, data):
        buf = bytes(data)
        r = hid.hid_write(self.h, buf, len(buf))
        if r < 0:
            log(f"!! hid_write не прошёл: {hx(buf)}")
        return r >= 0

    def read(self, timeout_ms):
        """Один репорт: сперва из backlog, потом с устройства. None если тихо."""
        if self.backlog:
            return self.backlog.popleft()
        rb = ctypes.create_string_buffer(64)
        n = hid.hid_read_timeout(self.h, rb, 64, timeout_ms)
        if n < 0:
            raise IOError("hid_read: приёмник отвалился")
        if n > 0:
            # Диагностика «второго командира»: пишем ВЕСЬ эфир. Ответы устройств
            # на чужие команды (Options+) приходят и нам — swid в ниббле байта 3
            # выдаёт отправителя (наши: 0x0D агент, 0x0E пуш с mini).
            log(f"[эфир] {hx(rb.raw[:n])}")
            return rb.raw[:n]
        return None

    def rpc(self, data, match, timeout=1.0):
        """Послать и ждать подходящий ответ; чужое — в backlog (обработает основной цикл).
        False = запись не прошла, None = тишина (для ChangeHost это разные исходы)."""
        if not self.write(data):
            return False
        stash = []
        t_end = time.time() + timeout
        while time.time() < t_end:
            r = self.read(150)
            if r is None: continue
            if match(r):
                self.backlog.extend(stash)
                return r
            stash.append(r)
        self.backlog.extend(stash)
        return None

    # — операции —
    def enable_notifications(self):
        ok = self.rpc([0x10, 0xFF, 0x80, 0x00, 0x00, 0x01, 0x00],
                      lambda r: r[0] == 0x10 and r[1] == 0xFF and r[2] in (0x80, 0x8F) and r[3] == 0x00)
        log(f"нотификации: {'вкл' if ok and ok[2] == 0x80 else 'ОШИБКА ' + (hx(ok) if ok else 'нет ответа')}")

    def fake_arrival(self, collect=1.2):
        """Просим приёмник переобъявить слоты; возвращаем {slot: (type, wpid, linked)}."""
        seen = {}
        if not self.write([0x10, 0xFF, 0x80, 0x02, 0x02, 0x00, 0x00]):
            return seen
        t_end = time.time() + collect
        while time.time() < t_end:
            r = self.read(150)
            if r is None: continue
            if len(r) >= 7 and r[0] == 0x10 and r[2] == 0x41:
                seen[r[1]] = (r[4] & 0x0F, (r[6] << 8) | r[5], not (r[4] & 0x40))
            elif not (len(r) >= 4 and r[0] == 0x10 and r[1] == 0xFF and r[3] == 0x02):
                self.backlog.append(r)      # ack на нашу команду глотаем, чужое сохраняем
        return seen

    def get_feature_index(self, slot, feat):
        r = self.rpc([0x11, slot, 0x00, (0x0 << 4) | SWID, (feat >> 8) & 0xFF, feat & 0xFF] + [0] * 14,
                     lambda r: (r[0] == 0x11 and r[1] == slot and r[2] == 0x00 and r[3] == SWID) or
                               (r[0] == 0x10 and r[1] == slot and r[2] == 0x8F) or
                               (r[0] == 0x11 and r[1] == slot and r[2] == 0xFF))
        if r and r[0] == 0x11 and r[2] == 0x00 and r[4] != 0:
            return r[4]
        log(f"getFeature 0x{feat:04x} слот {slot}: {'err ' + hx(r) if r else 'нет ответа'}")
        return None

    def change_host(self, slot, host):
        """ChangeHost (0x1814). Успех = ack ИЛИ уход устройства с канала (delink)."""
        fidx = self.get_feature_index(slot, 0x1814)
        if not fidx:
            return False
        r = self.rpc([0x11, slot, fidx, (0x1 << 4) | SWID, host] + [0] * 15,
                     lambda r: (r[0] == 0x11 and r[1] == slot and r[2] in (fidx, 0xFF)) or
                               (r[0] == 0x10 and r[1] == slot and r[2] in (0x8F, 0x40)) or
                               (r[0] == 0x10 and r[1] == slot and r[2] == 0x41 and (r[4] & 0x40)),
                     timeout=1.2)
        if r is False:
            return False  # запись не прошла — приёмник нездоров
        if r is None:
            return True   # команда часто уходит без ack — устройство уже прыгнуло
        if r[2] in (0x40, 0x41):
            self.backlog.append(r)   # delink нужен и основному циклу
            return True
        if r[2] == fidx:
            return True
        log(f"ChangeHost слот {slot} отверг: {hx(r)}")
        return False

# ── удержания питания ────────────────────────────────────────────────────────
class Holds:
    def __init__(self):
        self.sleep_guard = None    # caffeinate -s: clamshell-сон -> DarkWake (только на AC)
        self.display_hold = None   # caffeinate -d: пока клавиатура здесь и есть питание

    def _spawn(self, args):
        return subprocess.Popen(args + ["-w", str(os.getpid())],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def ensure_sleep_guard(self):
        if self.sleep_guard is None or self.sleep_guard.poll() is not None:
            self.sleep_guard = self._spawn(["caffeinate", "-s"])
            log("страж сна поднят (caffeinate -s, на батарее не действует)")

    def hold_display(self):
        if self.display_hold is None or self.display_hold.poll() is not None:
            self.display_hold = self._spawn(["caffeinate", "-d"])
            log("дисплей удерживаю (caffeinate -d)")

    def release_display(self, why=""):
        if self.display_hold and self.display_hold.poll() is None:
            self.display_hold.kill()
            log(f"удержание дисплея снято{why}")
        self.display_hold = None

# ── реакции на события ───────────────────────────────────────────────────────
class Agent:
    def __init__(self):
        self.rx = Receiver()
        self.holds = Holds()
        self.kb_slot = None
        self.mouse_slot = None
        self.kb_here = False
        self.mouse_here = False
        self.pull_gen = 0          # поколение pull-потока: старые прекращаются сами
        self.kb_change_ts = 0.0    # когда клавиатура последний раз пришла/ушла
        self.mouse_push_ts = 0.0   # когда последний раз толкали мышь

    # Тянуть монитор на себя — сквозь окно нестабильности сигнала, но БЕРЕЖНО.
    # Два факта 15.08: (1) ранние посылки попадают в окно, когда наш видеолинк ещё
    # не стабилен — LG переключается, видит «нет сигнала» и откатывается на живой
    # вход; лечится поздними повторами. (2) DDC-контроллер этой LG ЗАВЯЗАЕТ от
    # плотного потока команд (ночью чтения деградировали до мусора, записи молча
    # игнорировались) и отходит только после ~35-40 минут тишины. Поэтому посылок
    # 6 с шагом 1,4 с (~8,5 с окна) — достаточно для окна ренегоциации (2,6-4,4 с)
    # и отката, но без шквала. Повторная посылка на уже выбранный вход безвредна.
    def pull_monitor(self):
        self.pull_gen += 1
        gen = self.pull_gen
        def run():
            sent, last_err = 0, ""
            for i in range(6):
                if gen != self.pull_gen or not self.kb_here:
                    log("pull отменён (клавиатура ушла)")
                    return
                p = subprocess.run([M1DDC, "set", "input-alt", INPUT_MB],
                                   capture_output=True, text=True)
                if p.returncode == 0 and not p.stderr.strip():
                    sent += 1
                else:
                    last_err = (p.stderr.strip() or p.stdout.strip() or f"rc={p.returncode}")[:120]
                time.sleep(1.4)
            log(f"pull завершён: {sent}/6 посылок ушло" +
                (f", последняя ошибка m1ddc: {last_err}" if last_err else ""))
        threading.Thread(target=run, daemon=True).start()

    def on_kb_arrive(self):
        log("клавиатура ПРИШЛА -> зажигаю дисплей циклом (DDC не трогаю)")
        if on_ac():
            self.holds.hold_display()
        # pull_monitor ОТКЛЮЧЁН 15.08 09:55: DDC-мастер должен быть ОДИН (mini).
        # 0xF4 умер ровно после первых посылок с макбука (циклы 1-2 работали,
        # с 3-го регистр мёртв). Досылку делает mini (+3 и +6.5 с после ухода).
        #
        # ЗАЖИГАНИЕ — ЦИКЛОМ, а не одной побудкой (разбор 12:24): побудка при
        # приходе выигрывается гонкой гашения — clamshell от закрытия крышки или
        # 2.6-4.4-секундный провал линка при переключении входа LG гасят дисплей
        # ПОСЛЕ неё, и экран остаётся чёрным навсегда. caffeinate -d гашение по
        # clamshell не предотвращает и погасшее не зажигает. Только СВЕЖАЯ
        # декларация активности после каждого гашения зажигает снова — поэтому
        # повторяем ~15 секунд. -t 5 проверенное (с -t 1 не будит вообще).
        self.pull_gen += 1
        gen = self.pull_gen
        def relight():
            for i in range(6):
                if gen != self.pull_gen or not self.kb_here:
                    return
                subprocess.Popen(["caffeinate", "-u", "-t", "5"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(2.5)
        threading.Thread(target=relight, daemon=True).start()

    def push_mouse_to_mini(self, why):
        """Одна попытка ChangeHost; настойчивость обеспечивает конвергентное
        правило в основном цикле (раз в MOUSE_PUSH_COOLDOWN), а не ретраи
        на месте — inline-ретраи блокировали цикл событий до 11 с."""
        self.mouse_push_ts = time.time()
        ok = self.rx.change_host(self.mouse_slot, MOUSE_HOST_MINI)
        log(f"мышь -> mini ({why}): {'команда принята' if ok else 'не прошло, повторит правило конвергенции'}")

    def on_kb_leave(self):
        log("клавиатура УШЛА -> возвращаю мышь на mini, гашу дисплей")
        self.pull_gen += 1                       # отменить недобежавший pull
        if self.mouse_here and self.mouse_slot:
            self.push_mouse_to_mini("уход клавиатуры")
        else:
            log("мышь уже не на макбуке, команда не нужна")
        self.holds.release_display(" (клавиатура ушла)")
        if clamshell_closed():
            subprocess.run(["pmset", "displaysleepnow"], capture_output=True)
            log("дисплей погашен (крышка закрыта) — дальше DarkWake")

    def apply_slots(self, seen, first=False):
        for slot, (typ, wpid, linked) in seen.items():
            if typ == TYPE_KB:
                self.kb_slot = slot
                if linked != self.kb_here:
                    self.kb_here = linked
                    self.kb_change_ts = time.time()
                    if not first:
                        (self.on_kb_arrive if linked else self.on_kb_leave)()
            elif typ == TYPE_MOUSE:
                self.mouse_slot = slot
                if linked != self.mouse_here and not first:
                    log(f"мышь {'на Bolt' if linked else 'не на Bolt'} (ресинк)")
                self.mouse_here = linked

    def handle_report(self, r):
        if len(r) >= 7 and r[0] == 0x10 and r[2] == 0x41:
            slot, info = r[1], r[4]
            linked = not (info & 0x40)
            typ = info & 0x0F
            if typ == TYPE_KB or slot == self.kb_slot:
                self.kb_slot = slot
                if linked != self.kb_here:
                    self.kb_here = linked
                    self.kb_change_ts = time.time()
                    (self.on_kb_arrive if linked else self.on_kb_leave)()
            elif typ == TYPE_MOUSE or slot == self.mouse_slot:
                self.mouse_slot = slot
                if linked != self.mouse_here:
                    log(f"мышь {'ПРИШЛА на Bolt' if linked else 'ушла с Bolt'}")
                self.mouse_here = linked
        elif len(r) >= 3 and r[0] == 0x10 and r[2] == 0x40:   # disconnection notification
            slot = r[1]
            if slot == self.kb_slot and self.kb_here:
                self.kb_here = False
                self.kb_change_ts = time.time()
                self.on_kb_leave()
            elif slot == self.mouse_slot:
                if self.mouse_here:
                    log("мышь ушла с Bolt (disconnect)")
                self.mouse_here = False

    def run(self):
        log(f"=== старт kvm-mb-agent, pid {os.getpid()} ===")
        while True:
            if not self.rx.open():
                log("приёмник Bolt не найден, повтор через 5 с")
                time.sleep(5)
                continue
            log("приёмник открыт")
            try:
                self.rx.enable_notifications()
                seen = self.rx.fake_arrival()
                self.apply_slots(seen, first=True)
                log(f"слоты: клавиатура={self.kb_slot} ({'здесь' if self.kb_here else 'нет'}), "
                    f"мышь={self.mouse_slot} ({'здесь' if self.mouse_here else 'нет'})")
                self.holds.ensure_sleep_guard()
                if self.kb_here and on_ac():
                    self.holds.hold_display()
                last_resync = last_watch = last_tick = time.time()
                while True:
                    r = self.rx.read(500)
                    now = time.time()
                    # Разрыв в цикле = мы спали (полный сон на батарее и т.п.): события
                    # приёмника за это время потеряны — переспрашиваем состояние слотов.
                    if now - last_tick > 5:
                        log(f"разрыв цикла {now - last_tick:.0f} с (сон?) — ресинк слотов")
                        self.apply_slots(self.rx.fake_arrival())
                        last_resync = now
                    last_tick = now
                    if r is not None:
                        self.handle_report(r)
                    # Конвергенция мыши: клавиатуры давно нет, а мышь ещё на Bolt —
                    # значит команда потерялась или мышь вернуло болтанкой. Толкаем.
                    # Симметричное правило на mini толкает её сюда. Пинг-понга нет:
                    # каждый толкает только свою мышь при ЧУЖОЙ клавиатуре.
                    if (self.mouse_here and not self.kb_here and self.mouse_slot
                            and now - self.kb_change_ts > 3
                            and now - self.mouse_push_ts > MOUSE_PUSH_COOLDOWN):
                        self.push_mouse_to_mini("конвергенция")
                    if now - last_watch >= WATCH_SEC:
                        last_watch = now
                        self.holds.ensure_sleep_guard()
                        if self.holds.display_hold and not on_ac():
                            self.holds.release_display(" (питание пропало)")
                    if now - last_resync >= RESYNC_SEC:
                        last_resync = now
                        self.apply_slots(self.rx.fake_arrival())
            except IOError as e:
                log(f"!! {e} — переоткрываю")
                self.rx.close()
                time.sleep(2)

if __name__ == "__main__":
    hid.hid_init()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    Agent().run()
