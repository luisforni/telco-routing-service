import asyncio
import os
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import make_asgi_app

from app.api.routes import router
from app.services.kafka_service import KafkaService
from app.services.queue_service import QueueService
from app.services.routing_service import RoutingService
from app.services.strategy_service import StrategyService

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ]
)

log = structlog.get_logger()


def get_env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


@asynccontextmanager
async def lifespan(app: FastAPI):
    redis_url = get_env("REDIS_URL", "redis://redis:6379")
    kafka_brokers = get_env("KAFKA_BROKERS", "kafka:9092")
    cti_url = get_env("CTI_SERVICE_URL", "http://cti-service:8006")
    intelligence_url = get_env("INTELLIGENCE_SERVICE_URL", "http://intelligence-service:8003")
    default_strategy = get_env("DEFAULT_ROUTING_STRATEGY", "skill_based")

    from app.models.routing import RoutingStrategy

    try:
        strategy_enum = RoutingStrategy(default_strategy)
    except ValueError:
        strategy_enum = RoutingStrategy.skill_based

    redis_client = aioredis.from_url(redis_url, encoding="utf-8", decode_responses=False)

    strategy_svc = StrategyService(cti_url=cti_url, intelligence_url=intelligence_url)
    routing_svc = RoutingService(
        redis_client=redis_client,
        strategy_service=strategy_svc,
        cti_url=cti_url,
        default_strategy=strategy_enum,
    )
    queue_svc = QueueService(redis_client=redis_client)
    kafka_svc = KafkaService(brokers=kafka_brokers)

    app.state.redis = redis_client
    app.state.routing_service = routing_svc
    app.state.queue_service = queue_svc
    app.state.strategy_service = strategy_svc
    app.state.kafka_service = kafka_svc

    try:
        await kafka_svc.start_producer()
        log.info("kafka.producer.ready")
    except Exception as exc:
        log.warning("kafka.producer.unavailable", error=str(exc))

    consumer = kafka_svc.create_consumer(
        topics=["call-events", "agent-state-events", "cti-events"],
        group_id="routing-service",
        handler=_handle_kafka_event,
    )
    consumer_task = asyncio.create_task(_start_consumer_safe(consumer))

    log.info("telco_routing_service.started", port=8007)

    yield

    consumer_task.cancel()
    try:
        await consumer_task
    except (asyncio.CancelledError, Exception):
        pass

    await kafka_svc.stop_producer()
    await redis_client.aclose()
    log.info("telco_routing_service.stopped")


async def _start_consumer_safe(consumer):
    try:
        await consumer.start()
    except Exception as exc:
        log.warning("kafka.consumer.unavailable", error=str(exc))


async def _handle_kafka_event(topic: str, key, value: dict):
    log.info("kafka.event.received", topic=topic, event_type=value.get("event_type"))


app = FastAPI(
    title="Telco Routing Service",
    description="Intelligent call routing, queue management, and skill-based routing for the telco platform",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

app.include_router(router)


@app.get("/healthz")
async def health_check():
    return {"status": "ok", "service": "telco-routing-service", "version": "1.0.0"}
