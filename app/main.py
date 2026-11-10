import asyncio
import contextlib
import json
import re
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.agent import AgentError
from app.config import Settings, runtime_instance_id
from app.contracts import CONTRACT_VERSION
from app.db import Conflict, Store
from app.documents import OriginalStore
from app.gigachat import GigaChatProvider
from app.integrations import team_agents
from app.library import ActImport, Library
from app.postgres import PostgresStore
from app.schemas import Clarifications, Intake
from app.sources import ParserClient
from app.upload_processing import decode_upload, extract_image, validate_upload
from app.worker import Worker

STATIC = Path(__file__).parent / "static"
SAMPLES = Path(__file__).parent.parent / "samples"


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            upload = scope.get("path") == "/api/documents/extract"
            if len(body) > (10_000_000 if upload else 300_000):
                detail = "Запрос превышает 10 МБ" if upload else "Запрос превышает 300 КБ"
                return await JSONResponse({"detail": detail}, status_code=413)(
                    scope, receive, send
                )
            if not message.get("more_body"):
                break
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(settings=None, research_agent=None, critic_agent=None):
    settings = settings or Settings.from_env()
    store = (
        PostgresStore(settings.database_url) if settings.database_url else Store(settings.database)
    )
    library = Library(store)
    if research_agent is None and critic_agent is None:
        research_agent, critic_agent = team_agents(settings)
    worker = Worker(store, settings, research_agent=research_agent, critic_agent=critic_agent)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(worker.loop()) if settings.worker_enabled else None
        app.state.worker_task = task
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(
        title="Довод — исследование судебной практики", version="0.6.1", lifespan=lifespan
    )
    app.state.store = store
    app.state.settings = settings
    app.add_middleware(BodyLimit)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"]
    )

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and origin:
            if origin != f"{request.url.scheme}://{request.headers.get('host')}":
                return JSONResponse({"detail": "Запрос с другого сайта запрещен"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if request.url.path == "/":
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
            )
        return response

    @app.exception_handler(Conflict)
    async def conflict_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    def get_run(run_id):
        run = store.get(run_id)
        if run is None:
            raise HTTPException(404, "Задание не найдено")
        return run

    def check_mode(payload):
        if payload.mode == "gigachat":
            if not settings.gigachat_auth_key:
                raise HTTPException(503, "Настройте GIGACHAT_AUTH_KEY в локальном .env")
            if not payload.consent_to_cloud:
                raise HTTPException(422, "Подтвердите отправку текстов в GigaChat")
        if payload.mode == "yandex":
            if not settings.yandex_ready:
                raise HTTPException(503, "Настройте Yandex AI Studio в локальном .env")
            if not payload.consent_to_yandex:
                raise HTTPException(
                    422, "Подтвердите отправку введенных текстов в Yandex AI Studio"
                )

    def check_key(key):
        if not key or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", key):
            raise HTTPException(422, "Передайте Idempotency-Key: 8–100 букв, цифр, - или _")

    def public_run(run):
        return {
            k: run[k]
            for k in (
                "id",
                "parent_id",
                "payload",
                "status",
                "result",
                "error",
                "attempts",
                "created_at",
                "updated_at",
            )
        }

    @app.get("/api/health")
    def health():
        task = getattr(app.state, "worker_task", None)
        worker_ok = not settings.worker_enabled or (task is not None and not task.done())
        with store.connect() as db:
            db.execute("SELECT 1")
        return JSONResponse(
            {
                "status": "ok" if worker_ok else "degraded",
                "worker": worker_ok,
                "database": "PostgreSQL" if settings.database_url else "SQLite",
                "schema_version": 1,
                "application": "dovod",
                "instance_id": runtime_instance_id(),
            },
            status_code=200 if worker_ok else 503,
        )

    @app.get("/api/config")
    def config():
        return {
            "yandex_ready": settings.yandex_ready,
            "gigachat_ready": bool(settings.gigachat_auth_key),
            "gigachat_model": settings.gigachat_model,
            "model_framework": "LangChain",
            "parser_configured": bool(settings.parser_api_key),
            "database": "PostgreSQL" if settings.database_url else "SQLite",
            "model": settings.model if settings.yandex_ready else "Alice AI LLM",
            "implemented_agents": ["intake", "search", "comparison", "report"] +
                                  (["research"] if research_agent else []) +
                                  (["critique"] if critic_agent else []),
            "baseline_agents": ([] if research_agent else ["research"]) +
                               ([] if critic_agent else ["critique"]),
            "contract_version": CONTRACT_VERSION,
            "source_integration": "parser_api_kad_ras",
            "tool_research_enabled": settings.enable_tool_research,
            "semantic_critic_enabled": settings.enable_semantic_critic,
            "ocr_enabled": settings.enable_ocr,
            "originals_retained": True,
            "limits": {
                "source_calls": settings.source_max_calls,
                "research_acts": settings.research_acts,
                "model_calls": settings.total_model_calls,
            },
            "research_extension": research_agent is not None,
            "critic_extension": critic_agent is not None,
            "max_documents": 6,
            "demo_notice": "Локальный сценарий по правилам; языковая модель не вызывается",
        }

    @app.get("/api/samples/{name}")
    def sample(name: str):
        if name not in {"complete", "missing", "conflict"}:
            raise HTTPException(404, "Пример не найден")
        return json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8"))

    @app.post("/api/runs", status_code=201)
    def create_run(
        payload: Intake, response: Response, idempotency_key: str | None = Header(default=None)
    ):
        check_key(idempotency_key)
        check_mode(payload)
        run_id, created = store.create(payload.model_dump(), idempotency_key)
        response.status_code = 201 if created else 200
        return {"id": run_id, "created": created, "status": get_run(run_id)["status"]}

    @app.get("/api/runs")
    def list_runs():
        return store.recent()

    @app.get("/api/runs/{run_id}")
    def read_run(run_id: str):
        return public_run(get_run(run_id))

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str):
        get_run(run_id)
        return store.events(run_id)

    @app.get("/api/runs/{run_id}/export")
    def export(run_id: str):
        run = get_run(run_id)
        return JSONResponse(
            {"run": public_run(run), "events": store.events(run_id)},
            headers={"Content-Disposition": f'attachment; filename="intake-{run["id"]}.json"'},
        )

    @app.get("/api/runs/{run_id}/materials")
    def materials(run_id: str):
        get_run(run_id)
        return worker.materials.snapshot(run_id)

    @app.post("/api/runs/{run_id}/clarifications", status_code=201)
    def clarify(
        run_id: str,
        answers: Clarifications,
        response: Response,
        idempotency_key: str | None = Header(default=None),
    ):
        check_key(idempotency_key)
        run = get_run(run_id)
        if run["status"] != "needs_clarification":
            raise HTTPException(409, "Задание не ожидает уточнений")
        payload = Intake.model_validate(
            {
                **run["payload"],
                "answers": {**run["payload"]["answers"], **answers.answers},
                "consent_to_yandex": answers.consent_to_yandex,
                "consent_to_cloud": answers.consent_to_cloud,
            }
        )
        check_mode(payload)
        child, created = store.create(payload.model_dump(), idempotency_key, parent_id=run_id)
        response.status_code = 201 if created else 200
        return {"id": child, "created": created}

    @app.post("/api/runs/{run_id}/retry")
    def retry(run_id: str):
        run = get_run(run_id)
        check_mode(Intake.model_validate(run["payload"]))
        store.retry(run_id)
        return {"id": run_id, "status": "queued"}

    @app.post("/api/runs/{run_id}/revisions", status_code=201)
    def revise(run_id: str, payload: Intake, response: Response,
               idempotency_key: str | None = Header(default=None)):
        check_key(idempotency_key)
        get_run(run_id)
        check_mode(payload)
        child, created = store.create(payload.model_dump(), idempotency_key, parent_id=run_id)
        response.status_code = 201 if created else 200
        return {"id": child, "created": created}

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str):
        get_run(run_id)
        store.cancel(run_id)
        return {"id": run_id, "status": "cancelled"}

    @app.post("/api/runs/{run_id}/resume")
    def resume(run_id: str):
        run = get_run(run_id)
        check_mode(Intake.model_validate(run["payload"]))
        store.resume(run_id)
        return {"id": run_id, "status": "queued"}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/library")
    def list_acts():
        return library.recent()

    @app.post("/api/documents/extract")
    async def extract_uploaded(request: Request):
        """Local raw byte upload: no multipart dependency or cloud transmission."""
        from app.pdf_processing import extract_document

        filename = request.headers.get("x-document-name", "document")
        if not filename.isascii() or len(filename) > 180:
            raise HTTPException(422, "Некорректное имя документа")
        raw = await request.body()
        try:
            extension = validate_upload(raw, filename)
        except AgentError as exc:
            raise HTTPException(422, str(exc)) from None
        with tempfile.TemporaryDirectory(prefix="dovod-upload-") as directory:
            path = Path(directory) / ("document" + extension)
            path.write_bytes(raw)
            try:
                result = await (extract_document(path, settings) if extension == ".pdf"
                                else extract_image(path, settings))
            except AgentError as exc:
                raise HTTPException(422, str(exc)) from None
        try:
            outcome = decode_upload(result)
        except AgentError as exc:
            raise HTTPException(422, str(exc)) from None
        if extension == ".pdf":
            digest = OriginalStore(settings.document_dir).put(raw)
            outcome["original_url"] = f"/api/documents/{digest}/original"
        return outcome

    @app.get("/api/documents/{digest}/original")
    def original_document(digest: str):
        try:
            target = OriginalStore(settings.document_dir).get(digest)
        except AgentError as exc:
            raise HTTPException(404, str(exc)) from None
        return FileResponse(target, media_type="application/pdf", headers={
            "Content-Disposition": f'inline; filename="{digest}.pdf"',
            "Content-Security-Policy": "sandbox; default-src 'none'",
        })

    @app.get("/api/connections/ocr")
    async def ocr_status():
        from app.pdf_worker import ocr_ready

        ready = await asyncio.to_thread(ocr_ready, settings.tesseract_cmd, settings.ocr_languages)
        return {"ok": ready and settings.enable_ocr, "engine_ready": ready,
                "enabled": settings.enable_ocr,
                "languages": settings.ocr_languages,
                "message": "OCR выключен; оригиналы PDF сохраняются" if not settings.enable_ocr else
                "OCR доступен; результат требует сверки" if ready else
                "Установите Tesseract и языки rus/eng; оригиналы сохраняются и без OCR"}

    @app.post("/api/connections/gigachat/check")
    async def check_gigachat():
        try:
            await GigaChatProvider(settings).access_token()
            return {
                "ok": True,
                "message": "Авторизация GigaChat успешна; доступ к модели проверяется при запуске.",
            }
        except AgentError as exc:
            return {"ok": False, "message": str(exc)}

    @app.post("/api/library", status_code=201)
    def import_act(value: ActImport):
        return {"id": library.import_act(value)}

    @app.post("/api/connections/parser/check")
    async def check_parser():
        return await ParserClient(settings).diagnose()

    @app.post("/api/library/kad")
    async def import_kad(number: str):
        source = ParserClient(settings)
        try:
            cases = await source.case_by_number(number)
        except AgentError as exc:
            raise HTTPException(422, str(exc)) from None
        imported, warnings = [], []
        if len(cases) > settings.research_acts:
            warnings.append(
                f"Загружены первые {settings.research_acts} актов; "
                "остальные доступны в карточке дела."
            )
        for case in cases[:settings.research_acts]:
            try:
                case["pages"] = await source.read_act(case)
                library.save(case)
                imported.append({"number": case["number"], "pages": len(case["pages"])})
            except AgentError as exc:
                warnings.append(str(exc))
        return {
            "imported": imported,
            "warnings": warnings,
            "message": "Акты сохранены" if imported else "Доступные тексты актов не получены",
        }

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
