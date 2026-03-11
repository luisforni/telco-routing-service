import json
from typing import Optional

import structlog
from aiokafka import AIOKafkaProducer, AIOKafkaConsumer
from aiokafka.errors import KafkaError

log = structlog.get_logger()


class KafkaService:
    def __init__(self, brokers: str):
        self.brokers = brokers
        self._producer: Optional[AIOKafkaProducer] = None
        self._consumers: list = []

    async def start_producer(self):
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self.brokers,
            value_serializer=lambda v: json.dumps(v).encode(),
            key_serializer=lambda k: k.encode() if k else None,
            enable_idempotence=True,
            compression_type="gzip",
            acks="all",
        )
        await self._producer.start()
        log.info("kafka.producer.started", brokers=self.brokers)

    async def stop_producer(self):
        if self._producer:
            await self._producer.stop()
            log.info("kafka.producer.stopped")

    async def publish(self, topic: str, key: str, value: dict):
        if not self._producer:
            log.warning("kafka.producer.not_started", topic=topic)
            return
        try:
            await self._producer.send_and_wait(topic, key=key, value=value)
            log.debug("kafka.message.sent", topic=topic, key=key)
        except KafkaError as exc:
            log.error("kafka.publish.error", topic=topic, error=str(exc))

    async def publish_routing_event(self, event_type: str, data: dict):
        event = {"event_type": event_type, "data": data}
        await self.publish("routing-events", data.get("call_id", ""), event)

    async def publish_queue_event(self, event_type: str, data: dict):
        event = {"event_type": event_type, "data": data}
        await self.publish("queue-events", data.get("call_id", ""), event)

    def create_consumer(
        self,
        topics: list,
        group_id: str,
        handler,
    ) -> "KafkaConsumerRunner":
        return KafkaConsumerRunner(
            brokers=self.brokers,
            topics=topics,
            group_id=group_id,
            handler=handler,
        )


class KafkaConsumerRunner:
    def __init__(self, brokers: str, topics: list, group_id: str, handler):
        self.brokers = brokers
        self.topics = topics
        self.group_id = group_id
        self.handler = handler
        self._consumer: Optional[AIOKafkaConsumer] = None
        self._running = False

    async def start(self):
        self._consumer = AIOKafkaConsumer(
            *self.topics,
            bootstrap_servers=self.brokers,
            group_id=self.group_id,
            auto_offset_reset="earliest",
            enable_auto_commit=True,
            value_deserializer=lambda v: json.loads(v.decode()),
        )
        await self._consumer.start()
        self._running = True
        log.info("kafka.consumer.started", topics=self.topics, group=self.group_id)
        await self._consume()

    async def stop(self):
        self._running = False
        if self._consumer:
            await self._consumer.stop()
            log.info("kafka.consumer.stopped")

    async def _consume(self):
        try:
            async for msg in self._consumer:
                if not self._running:
                    break
                try:
                    await self.handler(msg.topic, msg.key, msg.value)
                except Exception as exc:
                    log.error(
                        "kafka.handler.error",
                        topic=msg.topic,
                        error=str(exc),
                    )
        except Exception as exc:
            log.error("kafka.consumer.error", error=str(exc))
