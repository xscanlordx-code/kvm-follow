/*
 * kvmmouse — нативный пуш MX Master на другой канал (HID++ 0x1814 ChangeHost).
 *
 * Зачем не python: TCC-разрешение kTCCServiceBluetoothAlways выдано бандлу
 * local.kvm.mouse, но цепочка bash->arch->python размывает идентичность процесса,
 * и IOKit всё равно отвечает kIOReturnNotPermitted. Нативный Mach-O внутри бандла
 * — однозначная идентичность, разрешение применяется к нему.
 *
 * Использование: kvmmouse [dry|push] [host 0..2] [подстрока имени, по умолчанию "MX Master"]
 *   host 0 = канал 1 (макбук/Bolt), host 1 = канал 2 (mini/BT).
 * Лог: /tmp/kvm-mouse-push.log (общий с python-версией).
 */
#include <stdio.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <wchar.h>
#include <hidapi/hidapi.h>
#include <objc/runtime.h>
#include <objc/message.h>
#include <CoreFoundation/CoreFoundation.h>

#define VID  0x046D
#define SWID 0x0E
#define LOGF "/tmp/kvm-mouse-push.log"

static void logline(const char *fmt, ...) {
    char ts[32];
    time_t now = time(NULL);
    strftime(ts, sizeof ts, "%F %T", localtime(&now));
    va_list ap;
    va_start(ap, fmt);
    char msg[512];
    vsnprintf(msg, sizeof msg, fmt, ap);
    va_end(ap);
    printf("%s %s\n", ts, msg);
    FILE *f = fopen(LOGF, "a");
    if (f) { fprintf(f, "%s %s\n", ts, msg); fclose(f); }
}

static int contains_w(const wchar_t *hay, const char *needle) {
    if (!hay) return 0;
    char buf[256] = {0};
    wcstombs(buf, hay, sizeof buf - 1);
    return strstr(buf, needle) != NULL;
}

/* Послать репорт и ждать подходящий ответ. match: r2ok — допустимые значения байта 2. */
static int rpc(hid_device *h, const unsigned char *req, int reqlen,
               unsigned char *resp, int fidx_or_zero, int timeout_ms) {
    if (hid_write(h, req, reqlen) < 0) return -2;
    int waited = 0;
    while (waited < timeout_ms) {
        unsigned char buf[64];
        int n = hid_read_timeout(h, buf, sizeof buf, 150);
        waited += 150;
        if (n < 5) continue;
        if (buf[0] != 0x11 || buf[1] != 0xFF) continue;
        if (fidx_or_zero == 0) {                       /* ответ root.getFeature */
            if (buf[2] == 0x00 && buf[3] == SWID) { memcpy(resp, buf, n); return n; }
        } else {                                       /* ответ/ошибка setHost */
            if (buf[2] == (unsigned char)fidx_or_zero || buf[2] == 0xFF) { memcpy(resp, buf, n); return n; }
        }
    }
    return -1;
}

/* Запрос Bluetooth-разрешения через CoreBluetooth: для CLI-бинаря macOS создаёт
 * path-запись в TCC (как у blueutil) и показывает системный диалог. Возвращает
 * CBManagerAuthorization: 0 не спрошено, 2 запрещено, 3 разрешено. */
static long bt_prompt(void) {
    typedef id (*msg_id)(id, SEL);
    typedef id (*msg_id2)(id, SEL, id, id);
    typedef long (*msg_long)(id, SEL);
    Class cb = objc_getClass("CBCentralManager");
    if (!cb) { logline("CoreBluetooth недоступен"); return -1; }
    long auth = ((msg_long)objc_msgSend)((id)cb, sel_registerName("authorization"));
    logline("CBManagerAuthorization до запроса: %ld", auth);
    if (auth == 3) return auth;
    id mgr = ((msg_id)objc_msgSend)((id)cb, sel_registerName("alloc"));
    mgr = ((msg_id2)objc_msgSend)(mgr, sel_registerName("initWithDelegate:queue:"), NULL, NULL);
    ((msg_id2)objc_msgSend)(mgr, sel_registerName("scanForPeripheralsWithServices:options:"), NULL, NULL);
    for (int i = 0; i < 120; i++) {   /* ждём решения пользователя до 60 с */
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.5, false);
        auth = ((msg_long)objc_msgSend)((id)cb, sel_registerName("authorization"));
        if (auth == 2 || auth == 3) break;
    }
    logline("CBManagerAuthorization после: %ld", auth);
    return auth;
}

int main(int argc, char **argv) {
    const char *mode = argc > 1 ? argv[1] : "push";
    if (strcmp(mode, "prompt") == 0) return bt_prompt() == 3 ? 0 : 1;
    int host = argc > 2 ? atoi(argv[2]) : 0;
    const char *substr = argc > 3 ? argv[3] : "MX Master";

    if (hid_init()) { logline("hid_init не удался"); return 1; }
    /* В момент ухода клавиатуры BT-стек занят её отключением и enumerate
     * не видит мышь несколько секунд (стабильно 3/3 утром 15.08) — из-за этого
     * следование мыши опаздывало на ~10 с до страховочного повтора.
     * Поэтому ищем с повторами до ~6 с. */
    char path[256] = {0};
    for (int att = 0; att < 9 && !path[0]; att++) {
        if (att) usleep(700000);
        struct hid_device_info *list = hid_enumerate(VID, 0), *d;
        for (d = list; d; d = d->next) {
            if (d->usage_page >= 0xFF00 && contains_w(d->product_string, substr)) {
                strncpy(path, d->path, sizeof path - 1);
                if (d->usage_page == 0xFF43) break;   /* основной HID++ канал BLE */
            }
        }
        hid_free_enumeration(list);
    }
    if (!path[0]) { logline("устройство '%s' не найдено за ~6 с — оно не на mini?", substr); return 2; }

    hid_device *h = NULL;
    for (int att = 0; att < 5 && !h; att++) {         /* open тоже может мигать при чурне стека */
        h = hid_open_path(path);
        if (!h) usleep(500000);
    }
    if (!h) { logline("open FAIL: %ls", hid_error(NULL)); return 3; }

    unsigned char req[20] = {0x11, 0xFF, 0x00, (0x0 << 4) | SWID, 0x18, 0x14};
    unsigned char resp[64];
    int n = rpc(h, req, sizeof req, resp, 0, 1200);
    if (n < 5 || resp[4] == 0) { logline("getFeature 0x1814: %s", n < 0 ? "нет ответа" : "фича не найдена"); hid_close(h); return 4; }
    int fidx = resp[4];
    logline("native: фича 0x1814 = индекс %d", fidx);

    if (strcmp(mode, "push") == 0) {
        unsigned char sreq[20] = {0x11, 0xFF, (unsigned char)fidx, (0x1 << 4) | SWID, (unsigned char)host};
        n = rpc(h, sreq, sizeof sreq, resp, fidx, 800);
        logline("native push '%s' -> хост %d: %s", substr, host,
                n > 0 ? "ack получен" : "команда ушла (без ack — норма)");
    }
    hid_close(h);
    return 0;
}
