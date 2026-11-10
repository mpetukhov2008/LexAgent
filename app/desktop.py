"""Local desktop entry point; reserves its port and opens only its own application."""

import argparse
import http.client
import json
import socket
import threading
import time
import webbrowser

import uvicorn

from app.config import runtime_instance_id
from app.main import create_app


def local_json(port, path="/api/health", timeout=1):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read(100_001)
        if len(body) > 100_000:
            return None
        return response.status, json.loads(body)
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def is_own_server(result):
    return bool(result and isinstance(result[1], dict)
                and result[1].get("application") == "dovod"
                and result[1].get("instance_id") == runtime_instance_id())


def reserve_port(start):
    for port in range(start, start + (1 if start == 0 else 11)):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", port))
            listener.listen(128)
            listener.setblocking(False)
            return listener, listener.getsockname()[1]
        except OSError:
            listener.close()
            existing = local_json(port)
            if is_own_server(existing):
                if existing[0] != 200:
                    raise RuntimeError("Довод уже запущен, но не готов. Перезапустите его окно.")
                return None, port
    raise RuntimeError("Порты заняты. Закройте прежний запуск или задайте другой порт.")


def open_browser(url):
    try:
        webbrowser.open(url)
    except (OSError, webbrowser.Error):
        print("Откройте адрес в браузере вручную:", url, flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--require-ocr", action="store_true")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65525:
        parser.error("Порт должен быть от 0 до 65525")
    listener, port = reserve_port(args.port)
    url = f"http://127.0.0.1:{port}/"
    if listener is None:
        print("Довод уже работает:", url, flush=True)
        if not args.no_browser:
            open_browser(url)
        return 0
    ready, stop = threading.Event(), threading.Event()
    server = None
    try:
        server = uvicorn.Server(uvicorn.Config(create_app(), log_level="warning", access_log=False))

        def wait_until_ready():
            deadline = time.monotonic() + 60
            while not stop.is_set() and time.monotonic() < deadline:
                result = local_json(port)
                if is_own_server(result) and result[0] == 200:
                    if args.require_ocr:
                        ocr = local_json(port, "/api/connections/ocr", timeout=15)
                        if not ocr or not ocr[1].get("ok"):
                            print("OCR не готов; проверка запуска не пройдена.", flush=True)
                            server.should_exit = True
                            return
                    ready.set()
                    print("Довод готов:", url, flush=True)
                    if not args.no_browser:
                        open_browser(url)
                    if args.smoke_test:
                        server.should_exit = True
                    return
                stop.wait(.2)
            if not stop.is_set():
                print("Сервер не готов за минуту. Проверьте сообщения в этом окне.", flush=True)
                server.should_exit = True

        threading.Thread(target=wait_until_ready, daemon=True).start()
        print("Оставьте это окно открытым. Для остановки нажмите Ctrl+C.", flush=True)
        server.run(sockets=[listener])
    finally:
        stop.set()
        listener.close()
    return 0 if ready.is_set() else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as error:
        print("Не удалось запустить Довод:", error, flush=True)
        raise SystemExit(1) from None
