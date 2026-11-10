#!/usr/bin/env python3
"""Локальный сервер тестирования. Работает без интернета.

Только стандартная библиотека Python 3.6+. Установка пакетов не нужна.

Запуск:  python server.py [--port 8080] [--devices 20] [--pin 1234]
"""
import argparse
import csv
import io
import json
import os
import socket
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
DATA_DIR = os.path.join(BASE, "data")
RESULTS_FILE = os.path.join(DATA_DIR, "results.json")

OFFLINE_AFTER = 10  # секунд без сигнала = «не в сети»
MAX_BODY = 512 * 1024

LOCK = threading.Lock()
DEVICES = {}   # id -> dict
RESULTS = []   # список результатов
CONFIG = {"show_key": False, "heartbeat_sec": 3, "pin": ""}


def now():
    return time.time()


def load_results():
    global RESULTS
    try:
        with open(RESULTS_FILE, encoding="utf-8") as f:
            RESULTS = json.load(f)
    except (OSError, ValueError):
        RESULTS = []


def save_results():
    """Вызывать только под LOCK. Запись через временный файл."""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = RESULTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(RESULTS, f, ensure_ascii=False)
    os.replace(tmp, RESULTS_FILE)


def new_device(dev_id):
    return {"id": dev_id, "last_seen": 0, "ip": "", "state": "idle",
            "fio": "", "group": "", "answered": 0, "total": 0, "seconds_left": 0}


def clean_id(value):
    value = str(value or "").strip().upper()[:16]
    return value if value and all(c.isalnum() or c in "-_" for c in value) else ""


def local_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:  # узнать основной адрес без интернета: пакет не отправляется
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


class Handler(BaseHTTPRequestHandler):
    server_version = "ClassTest/1.0"

    def log_message(self, fmt, *args):  # тихий режим: heartbeat не засоряет консоль
        pass

    # ---------- вспомогательные ----------
    def client_ip(self):
        return self.client_address[0]

    def is_teacher(self):
        ip = self.client_ip()
        if ip in ("127.0.0.1", "::1") or ip == self.request.getsockname()[0]:
            return True  # запрос с самого компьютера учителя
        pin = CONFIG["pin"]
        if not pin:
            return False
        q = parse_qs(urlparse(self.path).query)
        if q.get("pin", [""])[0] == pin:
            return True
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return "pin" in cookie and cookie["pin"].value == pin

    def send_bytes(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, code=200):
        self.send_bytes(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                        "application/json; charset=utf-8")

    def read_json(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if n <= 0 or n > MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return None

    def serve_file(self, name, ctype, inject=None):
        try:
            with open(os.path.join(STATIC, name), "rb") as f:
                body = f.read()
        except OSError:
            return self.send_bytes(404, b"not found", "text/plain")
        if inject:
            body = body.replace(b"</body>", inject + b"</body>", 1)
        self.send_bytes(200, body, ctype)

    # ---------- GET ----------
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/test", "/index.html"):
            return self.serve_file("test.html", "text/html; charset=utf-8",
                                   inject=b'<script src="/net.js"></script>\n')
        if path == "/net.js":
            return self.serve_file("net.js", "application/javascript; charset=utf-8")
        if path == "/api/config":
            return self.send_json({"show_key": CONFIG["show_key"],
                                   "heartbeat_sec": CONFIG["heartbeat_sec"]})
        if path in ("/teacher", "/api/teacher/state", "/api/teacher/export.csv"):
            if not self.is_teacher():
                return self.send_bytes(403, "Доступ только для учителя".encode("utf-8"),
                                       "text/plain; charset=utf-8")
            if path == "/teacher":
                extra = {}
                pin = parse_qs(urlparse(self.path).query).get("pin", [""])[0]
                if pin and pin == CONFIG["pin"]:
                    extra["Set-Cookie"] = "pin=%s; Path=/; HttpOnly" % pin
                try:
                    with open(os.path.join(STATIC, "teacher.html"), "rb") as f:
                        return self.send_bytes(200, f.read(), "text/html; charset=utf-8", extra)
                except OSError:
                    return self.send_bytes(404, b"not found", "text/plain")
            if path == "/api/teacher/state":
                return self.send_json(self.snapshot())
            return self.send_csv()
        self.send_bytes(404, b"not found", "text/plain")

    def snapshot(self):
        t = now()
        with LOCK:
            devices = []
            for d in DEVICES.values():
                item = dict(d)
                item["online"] = (t - d["last_seen"]) <= OFFLINE_AFTER if d["last_seen"] else False
                item["age"] = round(t - d["last_seen"], 1) if d["last_seen"] else None
                devices.append(item)
            devices.sort(key=lambda x: x["id"])
            return {"server_time": t, "offline_after": OFFLINE_AFTER,
                    "devices": devices, "results": list(RESULTS)}

    def send_csv(self):
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["№", "Получено", "Ноутбук", "ФИО", "Возраст", "Школа", "Рег. №",
                    "Режим", "Время", "Баллы", "Всего", "Оценка"])
        with LOCK:
            rows = list(RESULTS)
        for i, r in enumerate(rows, 1):
            rec = r.get("record", {})
            w.writerow([i, time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(r["received_at"])),
                        r.get("device", ""), rec.get("fio", ""), rec.get("age", ""),
                        rec.get("group", ""), rec.get("regNum", ""), rec.get("modeLabel", ""),
                        rec.get("duration", ""), rec.get("score", ""), rec.get("total", ""),
                        "%s (%s)" % (rec.get("gradeNum", ""), rec.get("gradeText", ""))])
        body = ("﻿" + buf.getvalue()).encode("utf-8")
        self.send_bytes(200, body, "text/csv; charset=utf-8",
                        {"Content-Disposition": 'attachment; filename="results.csv"'})

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path
        data = self.read_json()
        if path == "/api/heartbeat":
            return self.on_heartbeat(data)
        if path == "/api/result":
            return self.on_result(data)
        if path in ("/api/teacher/clear", "/api/teacher/forget"):
            if not self.is_teacher():
                return self.send_json({"ok": False}, 403)
            return self.on_teacher_action(path, data or {})
        self.send_json({"ok": False}, 404)

    def on_heartbeat(self, data):
        if not isinstance(data, dict):
            return self.send_json({"ok": False}, 400)
        dev_id = clean_id(data.get("device"))
        if not dev_id:
            return self.send_json({"ok": False, "error": "device"}, 400)
        state = data.get("state") if data.get("state") in ("idle", "testing", "done") else "idle"

        def num(key):
            try:
                return max(0, int(data.get(key, 0)))
            except (TypeError, ValueError):
                return 0

        with LOCK:
            d = DEVICES.setdefault(dev_id, new_device(dev_id))
            d.update(last_seen=now(), ip=self.client_ip(), state=state,
                     fio=str(data.get("fio", ""))[:120], group=str(data.get("group", ""))[:120],
                     answered=num("answered"), total=num("total"),
                     seconds_left=num("seconds_left"))
        self.send_json({"ok": True})

    def on_result(self, data):
        if not isinstance(data, dict) or not isinstance(data.get("record"), dict):
            return self.send_json({"ok": False}, 400)
        dev_id = clean_id(data.get("device"))
        uid = str(data.get("uid", ""))[:64]
        if not dev_id or not uid:
            return self.send_json({"ok": False}, 400)
        with LOCK:
            if not any(r["uid"] == uid for r in RESULTS):  # повторная отправка не дублируется
                RESULTS.append({"uid": uid, "device": dev_id, "received_at": now(),
                                "record": data["record"],
                                "details": data.get("details", [])})
                save_results()
            d = DEVICES.setdefault(dev_id, new_device(dev_id))
            d.update(last_seen=now(), ip=self.client_ip(), state="done")
        self.send_json({"ok": True})

    def on_teacher_action(self, path, data):
        with LOCK:
            if path == "/api/teacher/clear":
                RESULTS.clear()
                save_results()
            else:
                DEVICES.pop(clean_id(data.get("device")), None)
        self.send_json({"ok": True})


def main():
    ap = argparse.ArgumentParser(description="Сервер школьного тестирования")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--devices", type=int, default=20,
                    help="сколько ноутбуков показывать в списке заранее (NB-01..NB-N)")
    ap.add_argument("--pin", default="", help="PIN для доступа к панели учителя с других ПК")
    ap.add_argument("--show-key", action="store_true",
                    help="показывать ученику разбор ответов после теста")
    args = ap.parse_args()

    CONFIG["pin"] = args.pin
    CONFIG["show_key"] = args.show_key
    load_results()
    for i in range(1, args.devices + 1):
        dev_id = "NB-%02d" % i
        DEVICES[dev_id] = new_device(dev_id)
    for r in RESULTS:
        DEVICES.setdefault(r["device"], new_device(r["device"]))

    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    except OSError as e:
        raise SystemExit("Не удалось открыть порт %d: %s\nЗакройте другую программу или "
                         "задайте другой порт: --port 8081" % (args.port, e))
    httpd.daemon_threads = True
    print("=" * 60)
    print(" Сервер запущен. Не закрывайте это окно.")
    print(" Панель учителя :  http://localhost:%d/teacher" % args.port)
    for ip in local_ips():
        print(" Для ноутбуков  :  http://%s:%d/" % (ip, args.port))
    print("=" * 60)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")


if __name__ == "__main__":
    main()
