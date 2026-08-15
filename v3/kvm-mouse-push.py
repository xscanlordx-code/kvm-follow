#!/usr/bin/env python3
# kvm-mouse-push — половина mini: пуш мыши MX Master 3S на канал макбука.
#
# ЗАЧЕМ. Уход мыши mini->макбук раньше делал Logi Options+ на mini (наблюдалось
# 17/17), но при быстрых циклах переключения он перестаёт слать ChangeHost
# (15.08, циклы 4-5: мышь так и не пришла на Bolt). Этот скрипт делает то же
# самое сам: mini связан с мышью напрямую по BLE, HID++ ей шлётся с device
# index 0xFF (прямое подключение, без ресивера).
#
# Вызовы: dry  — только найти мышь и спросить индекс фичи 0x1814 (ничего не переключает);
#         push — послать SetCurrentHost(0) = канал 1 = макбук (Bolt).
# Карта каналов мыши: 1 = макбук (Bolt), 2 = mini (BT), 3 = мёртв. Хосты 0-based.
import ctypes, sys, time

LIB = "/opt/homebrew/lib/libhidapi.dylib"
VID = 0x046D
SWID = 0x0E                 # отличается от 0x0D агента макбука — фильтр ответов
HOST_MB = 0                 # 0-based: канал 1 = макбук
LOG = "/tmp/kvm-mouse-push.log"

def log(msg):
    line = f"{time.strftime('%F %T')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass

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
hid.hid_open_path.restype = ctypes.c_void_p
hid.hid_open_path.argtypes = [ctypes.c_char_p]
hid.hid_close.argtypes = [ctypes.c_void_p]
hid.hid_write.restype = ctypes.c_int
hid.hid_write.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
hid.hid_read_timeout.restype = ctypes.c_int
hid.hid_read_timeout.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int]
hid.hid_error.restype = ctypes.c_wchar_p
hid.hid_error.argtypes = [ctypes.c_void_p]

def hx(b): return " ".join(f"{x:02x}" for x in b)

def candidates(substr):
    out = []
    p = hid.hid_enumerate(VID, 0)
    while p:
        d = p.contents
        name = d.product_string or ""
        if d.usage_page >= 0xFF00 and substr in name:
            out.append((bytes(d.path), d.usage_page, d.usage, name))
        p = d.next
    return out

def rpc(h, data, match, timeout=1.2):
    buf = bytes(data)
    if hid.hid_write(h, buf, len(buf)) < 0:
        return False
    t_end = time.time() + timeout
    while time.time() < t_end:
        rb = ctypes.create_string_buffer(64)
        n = hid.hid_read_timeout(h, rb, 64, 150)
        if n > 0 and match(rb.raw[:n]):
            return rb.raw[:n]
    return None

def bt_prompt():
    """Запросить Bluetooth-разрешение штатно: создание CBCentralManager вызывает
    системный диалог (нужен NSBluetoothAlwaysUsageDescription в Info.plist обёртки).
    PyObjC на CLT-питоне нет — работаем через libobjc/ctypes."""
    c = ctypes
    libobjc = c.CDLL("/usr/lib/libobjc.dylib")
    c.CDLL("/System/Library/Frameworks/CoreBluetooth.framework/CoreBluetooth")
    CF = c.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    libobjc.objc_getClass.restype = c.c_void_p
    libobjc.objc_getClass.argtypes = [c.c_char_p]
    libobjc.sel_registerName.restype = c.c_void_p
    libobjc.sel_registerName.argtypes = [c.c_char_p]
    def send(restype, receiver, selname, *args):
        argtypes = [c.c_void_p, c.c_void_p] + [type(a) if isinstance(a, c.c_void_p) else c.c_void_p for a in args]
        fn = c.cast(libobjc.objc_msgSend, c.CFUNCTYPE(restype, *argtypes))
        return fn(receiver, libobjc.sel_registerName(selname), *args)
    cls = libobjc.objc_getClass(b"CBCentralManager")
    auth = send(c.c_long, cls, b"authorization")
    log(f"CBManagerAuthorization до запроса: {auth} (0=не спрошено, 2=запрещено, 3=разрешено)")
    if auth == 3:
        return 0
    mgr = send(c.c_void_p, cls, b"alloc")
    # Голый init диалог НЕ вызывает (проверено 02:07) — нужен initWithDelegate:queue:,
    # а надёжнее всего — попытка сканирования: она обязана спросить разрешение.
    mgr = send(c.c_void_p, mgr, b"initWithDelegate:queue:", c.c_void_p(0), c.c_void_p(0))
    send(c.c_void_p, mgr, b"scanForPeripheralsWithServices:options:", c.c_void_p(0), c.c_void_p(0))
    CF.CFRunLoopRunInMode.restype = c.c_int
    CF.CFRunLoopRunInMode.argtypes = [c.c_void_p, c.c_double, c.c_bool]
    mode_ref = c.c_void_p.in_dll(CF, "kCFRunLoopDefaultMode")
    for _ in range(120):                          # ждём решения пользователя до 60 с
        CF.CFRunLoopRunInMode(mode_ref, 0.5, False)
        auth = send(c.c_long, cls, b"authorization")
        if auth in (2, 3):
            break
    log(f"CBManagerAuthorization после: {auth}")
    return 0 if auth == 3 else 1

def main():
    # kvm-mouse-push.py [dry|push|prompt] [host_idx] [подстрока имени устройства]
    # По умолчанию: push 0 "MX Master" (мышь -> канал 1 = макбук).
    mode = sys.argv[1] if len(sys.argv) > 1 else "dry"
    if mode == "prompt":
        sys.exit(bt_prompt())
    host = int(sys.argv[2]) if len(sys.argv) > 2 else HOST_MB
    substr = sys.argv[3] if len(sys.argv) > 3 else "MX Master"
    hid.hid_init()
    cands = candidates(substr)
    if not cands:
        log(f"устройство '{substr}' (vendor-интерфейс BLE) не найдено — оно не на mini?")
        sys.exit(2)
    for path, page, usage, name in cands:
        h = hid.hid_open_path(path)
        if not h:
            log(f"open FAIL page={page:04x} usage={usage:04x}: {hid.hid_error(None)!r}")
            continue
        # getFeature(0x1814): device index 0xFF — прямое BLE-подключение
        r = rpc(h, [0x11, 0xFF, 0x00, (0x0 << 4) | SWID, 0x18, 0x14] + [0] * 14,
                lambda r: len(r) >= 5 and r[0] == 0x11 and r[1] == 0xFF and r[2] == 0x00 and r[3] == SWID)
        if not r or r[4] == 0:
            log(f"page={page:04x}: getFeature 0x1814 не ответил ({hx(r) if r else 'тишина'})")
            hid.hid_close(h)
            continue
        fidx = r[4]
        log(f"page={page:04x} usage={usage:04x}: фича 0x1814 = индекс {fidx}")
        if mode == "push":
            ok = rpc(h, [0x11, 0xFF, fidx, (0x1 << 4) | SWID, host] + [0] * 15,
                     lambda r: len(r) >= 4 and r[0] == 0x11 and r[1] == 0xFF and r[2] in (fidx, 0xFF),
                     timeout=0.8)
            # Ответа обычно нет: устройство прыгает на другой канал мгновенно
            log(f"push '{substr}' -> хост {host}: {'ack ' + hx(bytes(ok)) if ok else 'команда ушла (без ack — норма)'}")
        hid.hid_close(h)
        sys.exit(0)
    log("ни один интерфейс не отработал")
    sys.exit(1)

main()
