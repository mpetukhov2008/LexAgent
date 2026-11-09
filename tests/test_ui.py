"""Browser regression test against a disposable local application instance."""

import base64
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

from app.db import Store

ROOT = Path(__file__).resolve().parents[1]


def test_browser_intake_library_search_and_mobile(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "DATABASE_URL": "", "DATABASE_PATH": str(tmp_path / "ui.sqlite3")}
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(base + "/api/health").status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base)
            page.screenshot(path=str(ROOT / "reports/ui-home.jpg"), type="jpeg", quality=65)
            page.get_by_text(
                "Библиотека судебных актов · добавить текст из источника", exact=True
            ).click()
            page.locator("#act-number").fill("УЧЕБНЫЙ ПРИМЕР")
            page.locator("#act-court").fill("Синтетический источник для теста")
            page.locator("#act-url").fill("https://kad.arbitr.ru/Card/example")
            page.locator("#act-text").fill(
                "Это синтетический текст для проверки интерфейса. Фотографии Ozon, "
                "инфографика Ozon. Не является судебным актом и не подтверждает судебную практику."
            )
            page.get_by_role("button", name="Добавить в библиотеку", exact=True).click()
            page.get_by_text(
                "Акт сохранён. Он доступен для локального поиска.", exact=True
            ).wait_for()
            page.get_by_role("button", name="Полные сведения", exact=True).click()
            page.locator("#search-enabled").check()
            page.locator("#submit").click()
            page.locator("#result .search-results").wait_for(timeout=20000)
            assert page.locator(".case-card").count() == 1
            assert "УЧЕБНЫЙ ПРИМЕР" in page.locator(".case-card").inner_text()
            run_hash = page.url.split("#")[1]
            exported = httpx.get(base + "/api/runs/" + run_hash + "/export").json()
            assert exported["run"]["result"]["search"]["status"] == "partial"
            # Labelled synthetic fixture: citation navigation without a paid model.
            result = exported["run"]["result"]
            act = result["search"]["cases"][0]
            result["search"]["evidence"] = [{
                "id": "ev_ui_fixture", "case_id": act["id"], "act_id": act["id"],
                "page": 1, "quote": act["pages"][0]["text"],
                "observation": "Синтетическая проверка ссылок", "text_verified": True,
            }]
            result["search"]["report"]["conclusions"] = [{
                "text": "Учебный вывод для проверки интерфейса, не юридическое заключение.",
                "evidence_ids": ["ev_ui_fixture"],
            }]
            with Store(tmp_path / "ui.sqlite3").connect() as db:
                db.execute("UPDATE runs SET result=? WHERE id=?", (json.dumps(result), run_hash))
            page.reload()
            page.locator(".case-card").wait_for()
            page.locator(".report-conclusions .citation-link").click()
            assert page.locator("#evidence-ev_ui_fixture").get_attribute("open") is not None
            assert "#page=1" in page.get_by_role(
                "link", name="Открыть страницу оригинала ↗"
            ).get_attribute("href")
            page.get_by_label("Фильтр судебной практики").fill("несуществующий номер")
            assert page.locator(".case-card").count() == 0
            page.get_by_label("Фильтр судебной практики").fill("")
            (ROOT / "reports").mkdir(exist_ok=True)
            page.screenshot(path=str(ROOT / "reports/ui-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(ROOT / "reports/ui-mobile.png"), full_page=True)
            page.locator("#menu-toggle").click()
            page.locator("#nav-settings").click()
            assert page.locator("#settings-details").get_attribute("open") is not None
            page.locator("#mode").select_option("gigachat")
            assert page.locator("#consent-wrap").is_visible()
            assert not errors
            browser.close()
            if os.getenv("CI"):
                encoded = base64.b64encode((ROOT / "reports/ui-home.jpg").read_bytes()).decode()
                for index in range(0, len(encoded), 4000):
                    print(f"UI_PREVIEW_{index:06d}:{encoded[index:index + 4000]}")
    finally:
        process.terminate()
        process.wait(timeout=10)
