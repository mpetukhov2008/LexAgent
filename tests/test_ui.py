"""Browser regression test against a disposable local application instance."""

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


def settle_transitions(page):
    """Capture final colors and geometry while leaving live agent pulses running."""
    page.evaluate("""async () => {
        const finite = document.getAnimations().filter(animation =>
            animation.effect.getComputedTiming().iterations !== Infinity);
        await Promise.all(finite.map(animation => animation.finished.catch(() => {})));
    }""")


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
            assert page.locator("html").get_attribute("data-theme") == "dark"
            assert page.locator("#welcome-screen").is_visible()
            page.evaluate("document.fonts.ready")
            assert page.evaluate("document.fonts.check('16px Manrope')")
            assert page.evaluate("document.fonts.check('32px Unbounded')")
            assert page.locator(".welcome-marketplaces img").count() == 2
            assert page.locator(".welcome-marketplaces img").evaluate_all(
                "images => images.every(i => i.complete && i.naturalWidth > 0 && i.height <= 20)"
            )
            assert page.locator(".welcome-brand").inner_text().strip().endswith("ДОВОД")
            assert page.locator(".city-skyline").get_attribute("src").endswith("moscow-city.svg")
            assert page.locator(".welcome-footer .support-link").get_attribute("href") == (
                "https://t.me/m_petukhov_2008"
            )
            page.screenshot(path=str(ROOT / "reports/ui-welcome.jpg"), type="jpeg", quality=70)
            page.locator("#welcome-theme-toggle").click()
            assert page.locator("html").get_attribute("data-theme") == "light"
            page.reload()
            assert page.locator("html").get_attribute("data-theme") == "light"
            page.locator("#welcome-theme-toggle").click()
            page.locator("#enter-workspace").click()
            assert page.locator("#app-shell").is_visible()
            assert page.locator("#welcome-screen").is_hidden()
            page.locator("#open-connections").click()
            assert page.locator("#settings-details").get_attribute("open") is not None
            assert page.locator(".connection-card").count() == 3
            assert "Parser API" in page.locator(".connection-card").nth(1).inner_text()
            assert page.locator(".connection-card").nth(0).get_by_role("button").is_disabled()
            page.locator("#settings-details").evaluate("e => e.open = false")
            page.locator(".intro").scroll_into_view_if_needed()
            page.locator("#agent-steps [data-stage='search']").click()
            assert "запросы" in page.locator("#agent-description").inner_text()
            assert page.locator("#active-agent").inner_text() == "Шесть агентов. Одна задача."
            settle_transitions(page)
            page.screenshot(path=str(ROOT / "reports/ui-home.jpg"), type="jpeg", quality=65)
            page.locator("#theme-toggle").click()
            settle_transitions(page)
            assert page.locator("#agent-steps button").first.evaluate(
                "node => getComputedStyle(node).backgroundColor"
            ) == "rgb(255, 255, 255)"
            page.screenshot(path=str(ROOT / "reports/ui-light.jpg"), type="jpeg", quality=65)
            page.locator("#theme-toggle").click()
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
            page.locator(".report-conclusions .citation-link").click()
            assert page.locator(".case-card").count() == 1
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
            # Explicitly synthetic event stream checks live progress without paid external APIs.
            fixture = {**exported["run"], "id": "progress-ui-case", "status": "running",
                       "result": None, "created_at": time.time(), "updated_at": time.time()}
            activity = [{"kind": "stage_done", "created_at": time.time(),
                         "detail": {"stage": "intake"}},
                        {"kind": "stage_done", "created_at": time.time(),
                         "detail": {"stage": "search"}},
                        {"kind": "stage_start", "created_at": time.time(),
                         "detail": {"stage": "research", "message": "Учебная проверка этапа"}}]
            page.route("**/api/runs/progress-ui-case", lambda route: route.fulfill(json=fixture))
            page.route("**/api/runs/progress-ui-case/events",
                       lambda route: route.fulfill(json=activity))
            page.goto(base + "/#progress-ui-case")
            try:
                page.get_by_role("heading", name="Исследователь дела", exact=True).wait_for()
            except Exception as exc:
                raise AssertionError({
                    "browser_errors": errors,
                    "active_agent": page.locator("#active-agent").inner_text(),
                    "alert": page.locator("#error").inner_text(), "url": page.url,
                }) from exc
            assert page.locator("#agent-steps [data-stage='research']").get_attribute(
                "aria-current"
            ) == "step"
            assert page.locator("#completed-stages").inner_text() == "Завершено этапов: 2 из 6"
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.locator("#research-progress").scroll_into_view_if_needed()
            page.wait_for_function("""() => {
                const track = document.getElementById('stage-progress').getBoundingClientRect();
                const fill = document.getElementById('stage-progress-fill').getBoundingClientRect();
                return Math.abs(fill.width / track.width - 2 / 6) < 0.01;
            }""")
            settle_transitions(page)
            page.screenshot(path=str(ROOT / "reports/ui-processing.jpg"), type="jpeg", quality=70)
            fixture["status"] = "needs_clarification"
            fixture["updated_at"] += 1
            fixture["result"] = {**result, "questions": [
                {"field": "rights", "question": "Уточните права"}],
                                 "ready_for_search": False, "search": None}
            page.get_by_role("heading", name="Нужно ваше уточнение", exact=True).wait_for()
            assert page.locator("#agent-steps [data-stage='intake']").get_attribute(
                "data-state"
            ) == "attention"
            page.emulate_media(reduced_motion="reduce")
            page.locator("#theme-toggle").click()
            assert page.locator("#theme-toggle").evaluate(
                "node => getComputedStyle(node).animationName"
            ) == "none"
            assert not errors
            browser.close()
    finally:
        process.terminate()
        process.wait(timeout=10)
