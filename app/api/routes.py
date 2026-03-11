import uuid
from typing import List, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from prometheus_client import Gauge

from app.models.queue import Queue, QueueConfig, QueueStats, QueuedCall
from app.models.routing import (
    AgentAvailability,
    AgentSkillUpdate,
    PriorityLevel,
    Priority,
    RoutingDecision,
    RoutingOverride,
    RoutingRequest,
    RoutingStrategy,
    RoutingStrategyConfig,
    Skill,
)
from app.services.kafka_service import KafkaService
from app.services.queue_service import QueueService
from app.services.routing_service import RoutingService
from app.services.strategy_service import StrategyService

log = structlog.get_logger()

router = APIRouter(prefix="/routing", tags=["routing"])

queue_length_gauge = Gauge(
    "queue_length",
    "Number of calls waiting in queue",
    ["queue_id"],
)


def get_routing_service(request: Request) -> RoutingService:
    return request.app.state.routing_service


def get_queue_service(request: Request) -> QueueService:
    return request.app.state.queue_service


def get_strategy_service(request: Request) -> StrategyService:
    return request.app.state.strategy_service


def get_kafka_service(request: Request) -> KafkaService:
    return request.app.state.kafka_service


# ---------------------------------------------------------------------------
# Routing Operations
# ---------------------------------------------------------------------------


@router.post("/route", response_model=RoutingDecision, status_code=status.HTTP_200_OK)
async def route_call(
    request: RoutingRequest,
    routing_svc: RoutingService = Depends(get_routing_service),
    kafka_svc: KafkaService = Depends(get_kafka_service),
):
    decision = await routing_svc.route_call(request)
    await kafka_svc.publish_routing_event(
        "call_routed" if decision.status == "routed" else "call_queued",
        decision.model_dump(mode="json"),
    )
    return decision


@router.get("/route/{call_id}", response_model=RoutingDecision)
async def get_routing_decision(
    call_id: str,
    routing_svc: RoutingService = Depends(get_routing_service),
):
    decision = await routing_svc.get_routing_decision(call_id)
    if not decision:
        raise HTTPException(status_code=404, detail=f"Routing decision for call {call_id} not found")
    return decision


@router.post("/route/{call_id}/override", response_model=RoutingDecision)
async def override_routing(
    call_id: str,
    override: RoutingOverride,
    routing_svc: RoutingService = Depends(get_routing_service),
    kafka_svc: KafkaService = Depends(get_kafka_service),
):
    decision = await routing_svc.override_routing(call_id, override.agent_id, override.reason)
    await kafka_svc.publish_routing_event("agent_assigned", decision.model_dump(mode="json"))
    return decision


@router.get("/next-agent", response_model=Optional[AgentAvailability])
async def get_next_agent(
    queue_id: Optional[str] = None,
    routing_svc: RoutingService = Depends(get_routing_service),
):
    agent = await routing_svc.get_next_agent(queue_id)
    if not agent:
        raise HTTPException(status_code=404, detail="No available agents")
    return agent


# ---------------------------------------------------------------------------
# Queue Management
# ---------------------------------------------------------------------------


@router.post("/queues", response_model=Queue, status_code=status.HTTP_201_CREATED)
async def create_queue(
    queue: Queue,
    queue_svc: QueueService = Depends(get_queue_service),
):
    return await queue_svc.create_queue(queue)


@router.get("/queues", response_model=List[Queue])
async def list_queues(queue_svc: QueueService = Depends(get_queue_service)):
    return await queue_svc.list_queues()


@router.get("/queues/{queue_id}", response_model=Queue)
async def get_queue(queue_id: str, queue_svc: QueueService = Depends(get_queue_service)):
    queue = await queue_svc.get_queue(queue_id)
    if not queue:
        raise HTTPException(status_code=404, detail=f"Queue {queue_id} not found")
    return queue


@router.put("/queues/{queue_id}", response_model=Queue)
async def update_queue(
    queue_id: str,
    config: QueueConfig,
    queue_svc: QueueService = Depends(get_queue_service),
):
    updated = await queue_svc.update_queue(queue_id, config)
    if not updated:
        raise HTTPException(status_code=404, detail=f"Queue {queue_id} not found")
    return updated


@router.delete("/queues/{queue_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_queue(queue_id: str, queue_svc: QueueService = Depends(get_queue_service)):
    deleted = await queue_svc.delete_queue(queue_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Queue {queue_id} not found")


@router.get("/queues/{queue_id}/stats", response_model=QueueStats)
async def get_queue_stats(queue_id: str, queue_svc: QueueService = Depends(get_queue_service)):
    stats = await queue_svc.get_queue_stats(queue_id)
    queue_length_gauge.labels(queue_id=queue_id).set(stats.calls_waiting)
    return stats


@router.get("/queues/{queue_id}/calls", response_model=List[QueuedCall])
async def get_queue_calls(queue_id: str, queue_svc: QueueService = Depends(get_queue_service)):
    return await queue_svc.get_calls_in_queue(queue_id)


@router.post("/queues/{queue_id}/calls/{call_id}", response_model=QueuedCall, status_code=status.HTTP_201_CREATED)
async def add_call_to_queue(
    queue_id: str,
    call_id: str,
    call: QueuedCall,
    queue_svc: QueueService = Depends(get_queue_service),
    kafka_svc: KafkaService = Depends(get_kafka_service),
):
    call.call_id = call_id
    call.queue_id = queue_id
    try:
        queued = await queue_svc.add_call_to_queue(queue_id, call)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await kafka_svc.publish_queue_event("call_queued", queued.model_dump(mode="json"))
    return queued


@router.delete("/queues/{queue_id}/calls/{call_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_call_from_queue(
    queue_id: str,
    call_id: str,
    queue_svc: QueueService = Depends(get_queue_service),
    kafka_svc: KafkaService = Depends(get_kafka_service),
):
    removed = await queue_svc.remove_call_from_queue(queue_id, call_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Call {call_id} not found in queue {queue_id}")
    await kafka_svc.publish_queue_event("call_dequeued", {"call_id": call_id, "queue_id": queue_id})


# ---------------------------------------------------------------------------
# Strategy Management
# ---------------------------------------------------------------------------

_strategies_store: dict = {}


@router.post("/strategies", response_model=RoutingStrategyConfig, status_code=status.HTTP_201_CREATED)
async def create_strategy(strategy: RoutingStrategyConfig):
    strategy.id = str(uuid.uuid4())
    _strategies_store[strategy.id] = strategy
    log.info("strategy.created", strategy_id=strategy.id, name=strategy.name)
    return strategy


@router.get("/strategies", response_model=List[RoutingStrategyConfig])
async def list_strategies():
    return list(_strategies_store.values())


@router.get("/strategies/{strategy_id}", response_model=RoutingStrategyConfig)
async def get_strategy(strategy_id: str):
    strategy = _strategies_store.get(strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")
    return strategy


@router.put("/strategies/{strategy_id}", response_model=RoutingStrategyConfig)
async def update_strategy(strategy_id: str, updated: RoutingStrategyConfig):
    if strategy_id not in _strategies_store:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")
    updated.id = strategy_id
    from datetime import datetime
    updated.updated_at = datetime.utcnow()
    _strategies_store[strategy_id] = updated
    return updated


@router.delete("/strategies/{strategy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_strategy(strategy_id: str):
    if strategy_id not in _strategies_store:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")
    del _strategies_store[strategy_id]


@router.post("/strategies/{strategy_id}/test")
async def test_strategy(
    strategy_id: str,
    sample_data: dict,
    strategy_svc: StrategyService = Depends(get_strategy_service),
):
    strategy = _strategies_store.get(strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")
    result = await strategy_svc.test_strategy(strategy.model_dump(), sample_data)
    return result


# ---------------------------------------------------------------------------
# Skills & Priority
# ---------------------------------------------------------------------------

_skills_store: dict = {}


@router.post("/skills", response_model=Skill, status_code=status.HTTP_201_CREATED)
async def create_skill(skill: Skill):
    skill.id = str(uuid.uuid4())
    _skills_store[skill.id] = skill
    log.info("skill.created", skill_id=skill.id, name=skill.name)
    return skill


@router.get("/skills", response_model=List[Skill])
async def list_skills():
    return list(_skills_store.values())


@router.get("/agents/{agent_id}/skills", response_model=AgentAvailability)
async def get_agent_skills(
    agent_id: str,
    routing_svc: RoutingService = Depends(get_routing_service),
):
    agent = await routing_svc.get_agent_skills(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    return agent


@router.put("/agents/{agent_id}/skills", response_model=AgentAvailability)
async def update_agent_skills(
    agent_id: str,
    update: AgentSkillUpdate,
    routing_svc: RoutingService = Depends(get_routing_service),
):
    agent = await routing_svc.update_agent_skills(agent_id, update.skills, update.skill_levels)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    return agent


@router.get("/priority-levels", response_model=List[PriorityLevel])
async def get_priority_levels():
    return [
        PriorityLevel(name=Priority.low, value=1, description="Low priority calls", sla_seconds=120),
        PriorityLevel(name=Priority.normal, value=2, description="Normal priority calls", sla_seconds=60),
        PriorityLevel(name=Priority.high, value=3, description="High priority calls", sla_seconds=30),
        PriorityLevel(name=Priority.urgent, value=4, description="Urgent calls - bypass queue", sla_seconds=10),
    ]
