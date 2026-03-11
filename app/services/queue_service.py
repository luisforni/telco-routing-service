import json
import time
import uuid
from typing import List, Optional

import redis.asyncio as aioredis
import structlog

from app.models.queue import Queue, QueueConfig, QueueStats, QueuedCall, QueueMember, QueueStatus
from app.models.routing import Priority

log = structlog.get_logger()

PRIORITY_SCORES = {
    Priority.urgent: 100,
    Priority.high: 75,
    Priority.normal: 50,
    Priority.low: 25,
}


class QueueService:
    def __init__(self, redis_client: aioredis.Redis):
        self.redis = redis_client

    def _queue_key(self, queue_id: str) -> str:
        return f"queue:config:{queue_id}"

    def _queue_calls_key(self, queue_id: str) -> str:
        return f"queue:calls:{queue_id}"

    def _call_key(self, call_id: str) -> str:
        return f"queue:call:{call_id}"

    def _stats_key(self, queue_id: str) -> str:
        return f"queue:stats:{queue_id}"

    def _all_queues_key(self) -> str:
        return "queue:all"

    async def create_queue(self, queue: Queue) -> Queue:
        if not queue.id:
            queue.id = str(uuid.uuid4())
        await self.redis.set(self._queue_key(queue.id), queue.model_dump_json())
        await self.redis.sadd(self._all_queues_key(), queue.id)
        log.info("queue.created", queue_id=queue.id, name=queue.name)
        return queue

    async def get_queue(self, queue_id: str) -> Optional[Queue]:
        data = await self.redis.get(self._queue_key(queue_id))
        if not data:
            return None
        return Queue.model_validate_json(data)

    async def list_queues(self) -> List[Queue]:
        queue_ids = await self.redis.smembers(self._all_queues_key())
        queues = []
        for qid in queue_ids:
            q = await self.get_queue(qid.decode() if isinstance(qid, bytes) else qid)
            if q:
                queues.append(q)
        return queues

    async def update_queue(self, queue_id: str, config: QueueConfig) -> Optional[Queue]:
        queue = await self.get_queue(queue_id)
        if not queue:
            return None
        update_data = config.model_dump(exclude_none=True)
        updated = queue.model_copy(update=update_data)
        from datetime import datetime
        updated.updated_at = datetime.utcnow()
        await self.redis.set(self._queue_key(queue_id), updated.model_dump_json())
        log.info("queue.updated", queue_id=queue_id)
        return updated

    async def delete_queue(self, queue_id: str) -> bool:
        queue = await self.get_queue(queue_id)
        if not queue:
            return False
        await self.redis.delete(self._queue_key(queue_id))
        await self.redis.srem(self._all_queues_key(), queue_id)
        await self.redis.delete(self._queue_calls_key(queue_id))
        log.info("queue.deleted", queue_id=queue_id)
        return True

    async def add_call_to_queue(self, queue_id: str, call: QueuedCall) -> QueuedCall:
        queue = await self.get_queue(queue_id)
        if not queue:
            raise ValueError(f"Queue {queue_id} not found")

        queue_size = await self.redis.zcard(self._queue_calls_key(queue_id))
        if queue_size >= queue.max_size:
            if queue.overflow_queue_id:
                call.queue_id = queue.overflow_queue_id
                return await self.add_call_to_queue(queue.overflow_queue_id, call)
            raise ValueError(f"Queue {queue_id} is full")

        call.queue_id = queue_id
        call.queued_at = __import__("datetime").datetime.utcnow()
        score = self._compute_score(call)
        await self.redis.zadd(self._queue_calls_key(queue_id), {call.call_id: score})
        await self.redis.set(self._call_key(call.call_id), call.model_dump_json(), ex=queue.timeout + 60)

        position = await self.get_call_position(queue_id, call.call_id)
        call.position = position
        log.info("call.queued", call_id=call.call_id, queue_id=queue_id, position=position)
        return call

    def _compute_score(self, call: QueuedCall) -> float:
        priority_boost = PRIORITY_SCORES.get(call.priority, 50)
        timestamp = time.time()
        return timestamp - priority_boost * 1000

    async def remove_call_from_queue(self, queue_id: str, call_id: str) -> bool:
        removed = await self.redis.zrem(self._queue_calls_key(queue_id), call_id)
        await self.redis.delete(self._call_key(call_id))
        if removed:
            log.info("call.dequeued", call_id=call_id, queue_id=queue_id)
        return bool(removed)

    async def get_calls_in_queue(self, queue_id: str) -> List[QueuedCall]:
        call_ids = await self.redis.zrange(self._queue_calls_key(queue_id), 0, -1)
        calls = []
        for i, cid in enumerate(call_ids):
            cid_str = cid.decode() if isinstance(cid, bytes) else cid
            data = await self.redis.get(self._call_key(cid_str))
            if data:
                call = QueuedCall.model_validate_json(data)
                call.position = i + 1
                call.wait_time = int(time.time() - call.queued_at.timestamp())
                calls.append(call)
        return calls

    async def get_call_position(self, queue_id: str, call_id: str) -> int:
        rank = await self.redis.zrank(self._queue_calls_key(queue_id), call_id)
        return (rank + 1) if rank is not None else -1

    async def get_next_call(self, queue_id: str) -> Optional[QueuedCall]:
        call_ids = await self.redis.zrange(self._queue_calls_key(queue_id), 0, 0)
        if not call_ids:
            return None
        cid = call_ids[0].decode() if isinstance(call_ids[0], bytes) else call_ids[0]
        data = await self.redis.get(self._call_key(cid))
        if not data:
            await self.redis.zrem(self._queue_calls_key(queue_id), cid)
            return None
        return QueuedCall.model_validate_json(data)

    async def get_queue_stats(self, queue_id: str) -> QueueStats:
        calls_waiting = await self.redis.zcard(self._queue_calls_key(queue_id))
        stats_data = await self.redis.hgetall(self._stats_key(queue_id))

        calls_handled = int(stats_data.get(b"calls_handled", 0) if stats_data else 0)
        abandoned_calls = int(stats_data.get(b"abandoned_calls", 0) if stats_data else 0)
        total_wait = float(stats_data.get(b"total_wait_time", 0) if stats_data else 0)
        within_sla = int(stats_data.get(b"within_sla", 0) if stats_data else 0)

        avg_wait = total_wait / calls_handled if calls_handled > 0 else 0.0
        service_level = (within_sla / calls_handled * 100) if calls_handled > 0 else 0.0

        calls_list = await self.get_calls_in_queue(queue_id)
        max_wait = max((c.wait_time for c in calls_list), default=0)

        return QueueStats(
            queue_id=queue_id,
            calls_waiting=calls_waiting,
            calls_handled=calls_handled,
            avg_wait_time=avg_wait,
            max_wait_time=float(max_wait),
            abandoned_calls=abandoned_calls,
            service_level=service_level,
        )

    async def record_answered(self, queue_id: str, wait_time: float, sla_target: int):
        pipe = self.redis.pipeline()
        pipe.hincrby(self._stats_key(queue_id), "calls_handled", 1)
        pipe.hincrbyfloat(self._stats_key(queue_id), "total_wait_time", wait_time)
        if wait_time <= sla_target:
            pipe.hincrby(self._stats_key(queue_id), "within_sla", 1)
        await pipe.execute()

    async def record_abandoned(self, queue_id: str):
        await self.redis.hincrby(self._stats_key(queue_id), "abandoned_calls", 1)
