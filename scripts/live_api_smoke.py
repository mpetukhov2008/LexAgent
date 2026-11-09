"""Explicit live API smoke test: localhost -> PostgreSQL -> LangChain -> GigaChat.

Consumes model/source quota on synthetic client input. No synthetic court acts
are inserted into the working library. Source failure remains a partial result.
"""

import json
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8010"


def main():
    payload = json.loads((ROOT / "samples/complete.json").read_text(encoding="utf-8"))
    payload.update(
        mode="gigachat",
        consent_to_cloud=True,
        search_enabled=True,
        documents=[],
        title="LangChain + GigaChat Pro — учебная проверка",
    )
    response = httpx.post(
        BASE + "/api/runs", json=payload, headers={"Idempotency-Key": str(uuid.uuid4())}
    )
    if response.status_code != 201:
        raise SystemExit(
            f"Local API returned HTTP {response.status_code}; check startup and configuration"
        )
    run_id = response.json()["id"]
    print("Live run:", run_id, flush=True)
    (ROOT / "data/live-run-id.txt").write_text(run_id, encoding="utf-8")
    for index in range(150):
        run = httpx.get(BASE + "/api/runs/" + run_id).json()
        if run["status"] not in {"queued", "running"}:
            report = httpx.get(BASE + "/api/runs/" + run_id + "/export").json()
            (ROOT / "reports/postgres-langchain-api-live.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            result = run.get("result") or {}
            print(
                "Final:",
                run["status"],
                "facts:",
                len(result.get("facts", [])),
                "questions:",
                len(result.get("questions", [])),
                flush=True,
            )
            return
        if index % 6 == 0:
            print("Processing", flush=True)
        time.sleep(5)
    raise SystemExit("Polling deadline reached; the run remains in application history")


if __name__ == "__main__":
    main()
