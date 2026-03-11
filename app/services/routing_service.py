import json
import time
import uuid
from typing import List, Optional

import httpx
import redis.asyncio as aioredis
import structlog
from prometheus_client import Counter, Histogram, Gauge

from app.models.routing import (
    AgentAvailability,
    RoutingDecision,
    RoutingRequest,
    RoutingStrategy,
    Priority,
)
from app.services.strategy_service import StrategyService

log = structlog.get_logger()

routing_decisions_total = Counter(
    "routing_decisions_total",
    "Total routing decisions by strategy",
    ["strategy", "status"],
)
routing_confidence = Histogram(
    "routing_confidence_score",
    "Confidence score of routing decisions",
    buckets=[0.1, 0.2, 0.3, 0.5, 0.7, 0.9, 1.0],
)
routing_duration = Histogram(
    "routing_duration_seconds",
    "Time spent on routing decisions",
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0],
)
agent_utilization = Gauge(
    "agent_utilization_ratio",
    "Ratio of busy agents to total agents",
)


class RoutingService:
    def __init__(
        self,
        redis_client: aioredis.Redis,
        strategy_service: StrategyService,
        cti_url: str,
        default_strategy: RoutingStrategy = RoutingStrategy.skill_based,
    ):
        self.redis = redis_client
        self.strategy_service = strategy_service
        self.cti_url = cti_url
        self.default_strategy = default_strategy

    def _decision_key(self, call_id: str) -> str:
        return f"routing:decision:{call_id}"

    def _agent_key(self, agent_id: str) -> str:
        return f"agent:{agent_id}"

    async def route_call(self, request: RoutingRequest) -> RoutingDecision:
        start = time.time()
        strategy = request.strategy or self.default_strategy

        available_agents = await self._get_available_agents(request.queue_id)

        if not available_agents:
            decision = RoutingDecision(
                call_id=request.call_id,
                queue_id=request.queue_id,
                strategy_used=strategy,
                wait_time=await self._estimate_wait(request.queue_id),
                confidence_score=0.0,
                status="queued",
            )
            routing_decisions_total.labels(strategy=strategy.value, status="queued").inc()
            log.info("call.no_agents", call_id=request.call_id, queue_id=request.queue_id)
            return decision

        selected = await self.strategy_service.select_agent(
            strategy=strategy,
            agents=available_agents,
            required_skills=request.required_skills,
            priority=request.priority,
            call_id=request.call_id,
        )

        if not selected:
            decision = RoutingDecision(
                call_id=request.call_id,
                queue_id=request.queue_id,
                strategy_used=strategy,
                wait_time=await self._estimate_wait(request.queue_id),
                confidence_score=0.0,
                status="queued",
            )
            routing_decisions_total.labels(strategy=strategy.value, status="queued").inc()
            return decision

        confidence = self._compute_confidence(selected, request.required_skills)
        decision = RoutingDecision(
            call_id=request.call_id,
            agent_id=selected.agent_id,
            queue_id=request.queue_id,
            strategy_used=strategy,
            wait_time=0,
            confidence_score=confidence,
            status="routed",
        )

        await self._cache_decision(decision)
        elapsed = time.time() - start
        routing_decisions_total.labels(strategy=strategy.value, status="routed").inc()
        routing_confidence.observe(confidence)
        routing_duration.observe(elapsed)

        log.info(
            "call.routed",
            call_id=request.call_id,
            agent_id=selected.agent_id,
            strategy=strategy.value,
            confidence=confidence,
        )
        return decision

    async def get_routing_decision(self, call_id: str) -> Optional[RoutingDecision]:
        data = await self.redis.get(self._decision_key(call_id))
        if not data:
            return None
        return RoutingDecision.model_validate_json(data)

    async def override_routing(self, call_id: str, agent_id: str, reason: Optional[str] = None) -> RoutingDecision:
        existing = await self.get_routing_decision(call_id)
        strategy = existing.strategy_used if existing else RoutingStrategy.round_robin

        decision = RoutingDecision(
            call_id=call_id,
            agent_id=agent_id,
            queue_id=existing.queue_id if existing else None,
            strategy_used=strategy,
            confidence_score=1.0,
            status="overridden",
            metadata={"reason": reason, "overridden": True},
        )
        await self._cache_decision(decision)
        routing_decisions_total.labels(strategy="override", status="overridden").inc()
        log.info("call.routed.override", call_id=call_id, agent_id=agent_id, reason=reason)
        return decision

    async def get_next_agent(self, queue_id: Optional[str] = None) -> Optional[AgentAvailability]:
        agents = await self._get_available_agents(queue_id)
        if not agents:
            return None
        return await self.strategy_service.select_agent(
            strategy=self.default_strategy,
            agents=agents,
            required_skills=[],
            priority=Priority.normal,
            call_id="next-agent-query",
        )

    async def _get_available_agents(self, queue_id: Optional[str] = None) -> List[AgentAvailability]:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                params = {}
                if queue_id:
                    params["queue_id"] = queue_id
                resp = await client.get(f"{self.cti_url}/agents/available", params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    agents = [AgentAvailability(**a) for a in data.get("agents", data if isinstance(data, list) else [])]
                    log.debug("agents.fetched_from_cti", count=len(agents))
                    return agents
        except Exception as exc:
            log.warning("cti.unavailable", error=str(exc))

        return await self._get_agents_from_redis(queue_id)

    async def _get_agents_from_redis(self, queue_id: Optional[str] = None) -> List[AgentAvailability]:
        keys = await self.redis.keys("agent:*")
        agents = []
        for key in keys:
            data = await self.redis.get(key)
            if not data:
                continue
            try:
                agent_data = json.loads(data)
                if agent_data.get("status") != "available":
                    continue
                if queue_id and queue_id not in agent_data.get("queue_ids", []):
                    continue
                agents.append(AgentAvailability(**agent_data))
            except Exception:
                continue
        return agents

    def _compute_confidence(self, agent: AgentAvailability, required_skills: List[str]) -> float:
        if not required_skills:
            return 1.0
        matches = sum(1 for s in required_skills if s in agent.skills)
        return matches / len(required_skills)

    async def _estimate_wait(self, queue_id: Optional[str]) -> int:
        if not queue_id:
            return 60
        try:
            count = await self.redis.zcard(f"queue:calls:{queue_id}")
            return int(count) * 45
        except Exception:
            return 60

    async def _cache_decision(self, decision: RoutingDecision):
        await self.redis.set(
            self._decision_key(decision.call_id),
            decision.model_dump_json(),
            ex=3600,
        )

    async def register_agent(self, agent: AgentAvailability):
        await self.redis.set(
            self._agent_key(agent.agent_id),
            agent.model_dump_json(),
            ex=28800,
        )
        log.info("agent.registered", agent_id=agent.agent_id)

    async def get_agent_skills(self, agent_id: str) -> Optional[AgentAvailability]:
        data = await self.redis.get(self._agent_key(agent_id))
        if not data:
            return None
        return AgentAvailability.model_validate_json(data)

    async def update_agent_skills(self, agent_id: str, skills: List[str], skill_levels: dict) -> Optional[AgentAvailability]:
        agent = await self.get_agent_skills(agent_id)
        if not agent:
            agent = AgentAvailability(agent_id=agent_id, status="available")
        agent.skills = skills
        agent.skill_levels = skill_levels
        await self.register_agent(agent)
        return agent
