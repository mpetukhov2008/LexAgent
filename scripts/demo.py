"""Generate repeatable synthetic defense artifacts without network access."""

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent import IntakeAgent  # noqa: E402
from app.config import Settings  # noqa: E402
from app.schemas import Intake  # noqa: E402


async def main():
    target = ROOT / "reports/demo"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("complete", "missing", "conflict"):
        payload = Intake.model_validate_json(
            (ROOT / f"samples/{name}.json").read_text(encoding="utf-8")
        ).model_dump()
        events = []
        agent = IntakeAgent(payload, Settings())
        result = await agent.run(
            lambda state, kind, detail: events.append({"kind": kind, **detail})
        )
        (target / f"{name}.json").write_text(
            json.dumps(
                {
                    "synthetic": True,
                    "mode": "demo",
                    "input": payload,
                    "result": result,
                    "events": events,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(
            f"{name}: facts={len(result['facts'])}, questions={len(result['questions'])}, "
            f"conflicts={len(result['conflicts'])}, steps={result['steps']}"
        )


if __name__ == "__main__":
    asyncio.run(main())
