import asyncio
import contextlib
import time
import uuid

from app.agent import AgentError, IntakeAgent
from app.agents.semantic_critic import SemanticCritic
from app.agents.tool_research import ResearchAgent
from app.budget import BudgetedProvider
from app.db import LostLease
from app.library import Library
from app.materials import Materials
from app.pipeline import SearchPipeline


class Worker:
    def __init__(self, store, settings, research_agent=None, critic_agent=None):
        self.store = store
        self.settings = settings
        self.owner = str(uuid.uuid4())
        self.research_agent, self.critic_agent = research_agent, critic_agent
        self.materials = Materials(store)

    async def pulse(self, run_id):
        while True:
            await asyncio.sleep(max(1, self.settings.lease_seconds / 3))
            self.store.heartbeat(run_id, self.owner, self.settings.lease_seconds)

    async def process(self, run):
        run_id = run["id"]
        pulse = asyncio.create_task(self.pulse(run_id))
        try:
            started = time.monotonic()
            saved = run["checkpoint"] or {}
            intake_state = saved.get("intake", saved if saved.get("version") == 1 else None)
            state = {"intake": intake_state, "pipeline": saved.get("pipeline", {}),
                     "budget": saved.get("budget", {})}

            def save_intake(value, kind, detail):
                state["intake"] = value
                self.store.checkpoint(run_id, self.owner, state, kind, detail)

            def emit(kind, detail):
                self.store.checkpoint(run_id, self.owner, state, kind, detail)

            agent = IntakeAgent(run["payload"], self.settings, intake_state)
            if run["payload"]["mode"] != "demo":
                agent.provider = BudgetedProvider(
                    agent.provider, state["budget"], self.settings.total_model_calls,
                    lambda: emit("budget", {"message": "Зарезервирован вызов модели"}),
                )
            result = await asyncio.wait_for(
                agent.run(save_intake),
                timeout=self.settings.run_timeout,
            )
            if run["payload"].get("search_enabled"):
                if result["questions"]:
                    result["search_notice"] = "Поиск начнётся после уточнения исходных сведений."
                else:
                    pipeline = SearchPipeline(
                        self.settings, library=Library(self.store),
                        research_agent=self.research_agent, critic_agent=self.critic_agent,
                    )
                    if run["payload"]["mode"] == "gigachat":
                        pipeline.provider = BudgetedProvider(
                            pipeline.provider, state["budget"], self.settings.total_model_calls,
                            lambda: emit("budget", {"message": "Зарезервирован вызов модели"}),
                        )
                        if self.settings.enable_tool_research and self.research_agent is None:
                            pipeline.research_agent = ResearchAgent(pipeline.provider, emit)
                        if self.settings.enable_semantic_critic and self.critic_agent is None:
                            pipeline.critic_agent = SemanticCritic(pipeline.provider, emit)
                    if hasattr(getattr(pipeline, "source", None), "set_budget"):
                        pipeline.source.set_budget(
                            state["budget"], lambda: emit("budget", {
                                "message": "Зарезервирован запрос к судебному источнику"}),
                        )

                    def save_pipeline(value):
                        state["pipeline"] = value
                        emit("checkpoint", {"message": "Сохранён этап поиска"})

                    try:
                        result["search"] = await asyncio.wait_for(
                            pipeline.run(
                                run["payload"], result, emit, state["pipeline"], save_pipeline
                            ),
                            timeout=max(0.01, self.settings.run_timeout -
                                        (time.monotonic() - started)),
                        )
                    except (AgentError, asyncio.TimeoutError) as exc:
                        result["search"] = {
                            **state["pipeline"],
                            "status": "partial",
                            "warnings": [str(exc) or "Превышен лимит времени поиска"],
                            "report": {
                                "summary": "Поиск завершён частично. Исходная карточка сохранена.",
                                "next_steps": ["Проверьте доступ к API и повторите исследование."],
                            },
                        }
                    result["handoff"] = {
                        "next_agent": "search_planner",
                        "implemented": True,
                        "level": "baseline",
                    }
                    self.materials.save(run_id, result["search"])
            result["execution_budget"] = state["budget"]
            self.store.finish(run_id, self.owner, result=result)
        except asyncio.CancelledError:
            self.store.release(run_id, self.owner)
            raise
        except LostLease:
            pass
        except asyncio.TimeoutError:
            self.store.finish(run_id, self.owner, error="Превышен лимит времени запуска")
        except AgentError as exc:
            self.store.finish(run_id, self.owner, error=str(exc))
        except Exception:
            # Never expose vendor response bodies, credentials or document contents in errors.
            self.store.finish(run_id, self.owner, error="Внутренняя ошибка обработки задания")
        finally:
            pulse.cancel()
            with contextlib.suppress(asyncio.CancelledError, LostLease):
                await pulse

    async def loop(self):
        while True:
            run = self.store.claim(
                self.owner, self.settings.lease_seconds, self.settings.max_attempts
            )
            if run:
                await self.process(run)
            await asyncio.sleep(self.settings.poll_seconds)
