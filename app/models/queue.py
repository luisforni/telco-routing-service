from enum import Enum
from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel, Field

from app.models.routing import RoutingStrategy, Priority


class QueueStatus(str, Enum):
    active = "active"
    paused = "paused"
    closed = "closed"


class Queue(BaseModel):
    id: Optional[str] = None
    name: str
    description: Optional[str] = None
    strategy: RoutingStrategy = RoutingStrategy.skill_based
    max_size: int = Field(default=100, ge=1)
    timeout: int = Field(default=300, ge=1, description="Max wait time in seconds")
    overflow_queue_id: Optional[str] = None
    required_skills: List[str] = Field(default_factory=list)
    status: QueueStatus = QueueStatus.active
    service_level_target: int = Field(default=20, description="Target answer time in seconds")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class QueueConfig(BaseModel):
    strategy: Optional[RoutingStrategy] = None
    max_size: Optional[int] = Field(default=None, ge=1)
    timeout: Optional[int] = Field(default=None, ge=1)
    overflow_queue_id: Optional[str] = None
    required_skills: Optional[List[str]] = None
    status: Optional[QueueStatus] = None
    service_level_target: Optional[int] = None


class QueueStats(BaseModel):
    queue_id: str
    calls_waiting: int = 0
    calls_handled: int = 0
    avg_wait_time: float = 0.0
    max_wait_time: float = 0.0
    abandoned_calls: int = 0
    service_level: float = Field(default=0.0, description="Percentage of calls answered within SLA")
    agents_available: int = 0
    agents_busy: int = 0
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class QueuedCall(BaseModel):
    call_id: str
    caller_id: str
    queue_id: str
    priority: Priority = Priority.normal
    position: int = 0
    wait_time: int = 0
    queued_at: datetime = Field(default_factory=datetime.utcnow)
    required_skills: List[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class QueueMember(BaseModel):
    agent_id: str
    queue_id: str
    priority: int = Field(default=1, ge=1)
    penalty: int = Field(default=0, ge=0)
    added_at: datetime = Field(default_factory=datetime.utcnow)
