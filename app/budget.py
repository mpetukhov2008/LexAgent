"""A shared, checkpointed model-call budget across intake and subsequent roles."""

from app.agent import AgentError


class BudgetedProvider:
    def __init__(self, provider, state, limit, save):
        self.provider, self.state, self.limit, self.save = provider, state, limit, save

    async def choose(self, messages, tools=None):
        used = self.state.get("model_calls", 0)
        if used >= self.limit:
            raise AgentError("Исчерпан общий бюджет вызовов модели")
        self.state["model_calls"] = used + 1
        self.save()
        if tools is None:
            return await self.provider.choose(messages)
        return await self.provider.choose(messages, tools)
