import time
from typing import List, Optional

import httpx
import structlog

from app.models.routing import (
    AgentAvailability,
    RoutingStrategy,
    SkillMatch,
    Priority,
)

log = structlog.get_logger()

PRIORITY_VALUES = {
    Priority.urgent: 4,
    Priority.high: 3,
    Priority.normal: 2,
    Priority.low: 1,
}


class StrategyService:
    def __init__(self, cti_url: str, intelligence_url: str):
        self.cti_url = cti_url
        self.intelligence_url = intelligence_url
        self._rr_indices: dict = {}

    async def select_agent(
        self,
        strategy: RoutingStrategy,
        agents: List[AgentAvailability],
        required_skills: List[str],
        priority: Priority,
        call_id: str,
    ) -> Optional[AgentAvailability]:
        if not agents:
            return None

        if strategy == RoutingStrategy.round_robin:
            return self._round_robin(agents, call_id)
        elif strategy == RoutingStrategy.skill_based:
            return self._skill_based(agents, required_skills)
        elif strategy == RoutingStrategy.priority:
            return self._priority_based(agents, required_skills, priority)
        elif strategy == RoutingStrategy.longest_idle:
            return self._longest_idle(agents)
        elif strategy == RoutingStrategy.least_calls:
            return self._least_calls(agents)
        elif strategy == RoutingStrategy.predictive:
            return await self._predictive(agents, required_skills, call_id)
        else:
            return self._skill_based(agents, required_skills)

    def _round_robin(self, agents: List[AgentAvailability], queue_id: str) -> AgentAvailability:
        idx = self._rr_indices.get(queue_id, 0) % len(agents)
        self._rr_indices[queue_id] = idx + 1
        return agents[idx]

    def _skill_based(
        self,
        agents: List[AgentAvailability],
        required_skills: List[str],
    ) -> Optional[AgentAvailability]:
        if not required_skills:
            return agents[0] if agents else None

        best: Optional[AgentAvailability] = None
        best_score = -1

        for agent in agents:
            score = self._compute_skill_score(agent, required_skills)
            if score > best_score:
                best_score = score
                best = agent

        return best or (agents[0] if agents else None)

    def _compute_skill_score(self, agent: AgentAvailability, required_skills: List[str]) -> float:
        if not required_skills:
            return 1.0
        total = 0.0
        for skill in required_skills:
            level = agent.skill_levels.get(skill, 0)
            if skill in agent.skills:
                total += level if level > 0 else 1
        return total / len(required_skills)

    def _priority_based(
        self,
        agents: List[AgentAvailability],
        required_skills: List[str],
        priority: Priority,
    ) -> Optional[AgentAvailability]:
        priority_val = PRIORITY_VALUES.get(priority, 2)
        if priority_val >= 3:
            return self._skill_based(agents, required_skills)
        return self._longest_idle(agents)

    def _longest_idle(self, agents: List[AgentAvailability]) -> Optional[AgentAvailability]:
        best: Optional[AgentAvailability] = None
        oldest_idle: Optional[float] = None
        now = time.time()
        for agent in agents:
            if agent.idle_since:
                idle_ts = agent.idle_since.timestamp()
                idle_duration = now - idle_ts
                if oldest_idle is None or idle_duration > oldest_idle:
                    oldest_idle = idle_duration
                    best = agent
        return best or (agents[0] if agents else None)

    def _least_calls(self, agents: List[AgentAvailability]) -> Optional[AgentAvailability]:
        return min(agents, key=lambda a: a.calls_handled, default=None)

    async def _predictive(
        self,
        agents: List[AgentAvailability],
        required_skills: List[str],
        call_id: str,
    ) -> Optional[AgentAvailability]:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.post(
                    f"{self.intelligence_url}/routing/predict",
                    json={
                        "call_id": call_id,
                        "agents": [a.model_dump() for a in agents],
                        "required_skills": required_skills,
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    recommended_id = data.get("recommended_agent_id")
                    if recommended_id:
                        for agent in agents:
                            if agent.agent_id == recommended_id:
                                log.info("predictive.routing.used", agent_id=recommended_id)
                                return agent
        except Exception as exc:
            log.warning("predictive.routing.failed", error=str(exc))

        return self._skill_based(agents, required_skills)

    def compute_skill_matches(
        self,
        agent: AgentAvailability,
        required_skills: List[str],
    ) -> List[SkillMatch]:
        matches = []
        for skill in required_skills:
            agent_level = agent.skill_levels.get(skill, 0)
            if skill in agent.skills:
                agent_level = max(agent_level, 1)
            score = min(agent_level / 5.0, 1.0) if agent_level > 0 else 0.0
            matches.append(
                SkillMatch(
                    skill=skill,
                    required_level=1,
                    agent_level=agent_level,
                    match_score=score,
                )
            )
        return matches

    async def test_strategy(
        self,
        strategy_config: dict,
        sample_data: dict,
    ) -> dict:
        agents_data = sample_data.get("agents", [])
        required_skills = sample_data.get("required_skills", [])
        priority_str = sample_data.get("priority", "normal")
        call_id = sample_data.get("call_id", "test-call")

        agents = [AgentAvailability(**a) for a in agents_data]
        try:
            priority = Priority(priority_str)
        except ValueError:
            priority = Priority.normal

        strategy_str = strategy_config.get("strategy", "skill_based")
        try:
            strategy = RoutingStrategy(strategy_str)
        except ValueError:
            strategy = RoutingStrategy.skill_based

        selected = await self.select_agent(strategy, agents, required_skills, priority, call_id)
        return {
            "strategy": strategy.value,
            "selected_agent": selected.model_dump() if selected else None,
            "candidates": len(agents),
            "required_skills": required_skills,
        }
