"""Planner: turn a goal into an ordered, reviewable step plan.

The planner asks a model for strict JSON and degrades gracefully: if the output
cannot be parsed, the plan collapses to a single step containing the original
goal — planning failure must never block execution.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field, ValidationError

from hoursx.providers.router import ModelRouter
from hoursx.providers.types import ChatMessage, ChatRequest, ChatRole

_PLAN_PROMPT = """\
Break the goal below into a short ordered plan (2-6 steps). Reply with ONLY a
JSON object of the form {{"steps": [{{"description": "..."}}]}} — no prose.

Goal: {goal}\
"""


class PlanStep(BaseModel):
    description: str = Field(min_length=3)


class Plan(BaseModel):
    steps: list[PlanStep] = Field(min_length=1)

    def as_checklist(self) -> str:
        return "\n".join(f"{i + 1}. {step.description}" for i, step in enumerate(self.steps))


class Planner:
    def __init__(self, router: ModelRouter, model_alias: str = "fast") -> None:
        self._router = router
        self._alias = model_alias

    async def plan(self, goal: str) -> Plan:
        result = await self._router.complete(
            self._alias,
            ChatRequest(
                model="unset",
                messages=[ChatMessage(role=ChatRole.USER, content=_PLAN_PROMPT.format(goal=goal))],
                temperature=0.2,
                max_tokens=800,
            ),
        )
        parsed = self._parse(result.message.content)
        return parsed if parsed is not None else Plan(steps=[PlanStep(description=goal)])

    @staticmethod
    def _parse(text: str) -> Plan | None:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match is None:
            return None
        try:
            return Plan.model_validate(json.loads(match.group(0)))
        except (json.JSONDecodeError, ValidationError):
            return None
