from enum import Enum
from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel, Field


class RoutingStrategy(str, Enum):
    round_robin = "round_robin"
    skill_based = "skill_based"
    priority = "priority"
    longest_idle = "longest_idle"
    least_calls = "least_calls"
    predictive = "predictive"


class Priority(str, Enum):
    low = "low"
    normal = "normal"
    high = "high"
    urgent = "urgent"


class RoutingRequest(BaseModel):
    call_id: str
    caller_id: str
    queue_id: Optional[str] = None
    priority: Priority = Priority.normal
    required_skills: List[str] = Field(default_factory=list)
    strategy: Optional[RoutingStrategy] = None
    metadata: dict = Field(default_factory=dict)


class RoutingDecision(BaseModel):
    call_id: str
    agent_id: Optional[str] = None
    queue_id: Optional[str] = None
    strategy_used: RoutingStrategy
    wait_time: int = 0
    confidence_score: float = Field(default=1.0, ge=0.0, le=1.0)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    status: str = "routed"
    metadata: dict = Field(default_factory=dict)


class RoutingOverride(BaseModel):
    agent_id: str
    reason: Optional[str] = None


class SkillMatch(BaseModel):
    skill: str
    required_level: int = Field(default=1, ge=1, le=5)
    agent_level: int = Field(default=1, ge=1, le=5)
    match_score: float = Field(default=0.0, ge=0.0, le=1.0)


class AgentAvailability(BaseModel):
    agent_id: str
    status: str
    skills: List[str] = Field(default_factory=list)
    skill_levels: dict = Field(default_factory=dict)
    calls_handled: int = 0
    idle_since: Optional[datetime] = None
    queue_ids: List[str] = Field(default_factory=list)


class RoutingStrategyConfig(BaseModel):
    id: Optional[str] = None
    name: str
    strategy: RoutingStrategy
    description: Optional[str] = None
    config: dict = Field(default_factory=dict)
    priority_weight: float = Field(default=1.0, ge=0.0)
    skill_weight: float = Field(default=1.0, ge=0.0)
    idle_weight: float = Field(default=1.0, ge=0.0)
    active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Skill(BaseModel):
    id: Optional[str] = None
    name: str
    description: Optional[str] = None
    category: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AgentSkillUpdate(BaseModel):
    skills: List[str] = Field(default_factory=list)
    skill_levels: dict = Field(default_factory=dict)


class PriorityLevel(BaseModel):
    name: Priority
    value: int
    description: str
    sla_seconds: int
