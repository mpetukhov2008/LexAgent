"""Trusted project modules, not user-provided plugin paths."""

import importlib
import importlib.util


def load_team_agent(module_name, class_name, settings):
    if importlib.util.find_spec(module_name) is None:
        return None
    module = importlib.import_module(module_name)
    return getattr(module, class_name)(settings)


def team_agents(settings):
    return (
        load_team_agent("app.agents.research", "CaseResearchAgent", settings),
        load_team_agent("app.agents.critic", "CriticAgent", settings)
        if settings.enable_rule_critic else None,
    )
