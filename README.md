# Telco Routing Service

Intelligent call routing microservice for the telco platform. Handles call routing decisions, queue management, skill-based routing, and multiple routing strategies.

## Features

- **Multiple Routing Strategies**: round-robin, skill-based, priority, longest-idle, least-calls, predictive
- **Queue Management**: Full queue lifecycle with priority queuing, overflow, and callbacks
- **Skill-Based Routing**: Match caller needs with agent skills and proficiency levels
- **Priority Handling**: Priority-based routing with SLA targets per priority level
- **Real-time Statistics**: Live queue and routing metrics via Prometheus
- **Overflow Logic**: Automatic overflow to backup queues when primary is full
- **Service Level Monitoring**: Track and report SLA achievement
- **Agent Load Balancing**: Distribute calls fairly across available agents
- **Predictive Routing**: Integration with intelligence service for ML-based routing hints
- **Kafka Integration**: Async event publishing for routing and queue events

## Routing Strategies

| Strategy | Description |
|---|---|
| `round_robin` | Distribute calls evenly across available agents |
| `skill_based` | Match required skills with agent proficiency levels |
| `priority` | Route high-priority calls to best-matched agents first |
| `longest_idle` | Route to agent who has been idle the longest |
| `least_calls` | Route to agent with the fewest calls handled today |
| `predictive` | Use intelligence service ML hints for optimal routing |

## API Endpoints

### Routing Operations
| Method | Path | Description |
|---|---|---|
| `POST` | `/routing/route` | Route a call to best available agent |
| `GET` | `/routing/route/{call_id}` | Get routing decision for a call |
| `POST` | `/routing/route/{call_id}/override` | Manual routing override |
| `GET` | `/routing/next-agent` | Get next available agent for queue |

### Queue Management
| Method | Path | Description |
|---|---|---|
| `POST` | `/routing/queues` | Create queue |
| `GET` | `/routing/queues` | List all queues |
| `GET` | `/routing/queues/{queue_id}` | Get queue details |
| `PUT` | `/routing/queues/{queue_id}` | Update queue configuration |
| `DELETE` | `/routing/queues/{queue_id}` | Delete queue |
| `GET` | `/routing/queues/{queue_id}/stats` | Get queue statistics |
| `GET` | `/routing/queues/{queue_id}/calls` | Get calls in queue |
| `POST` | `/routing/queues/{queue_id}/calls/{call_id}` | Add call to queue |
| `DELETE` | `/routing/queues/{queue_id}/calls/{call_id}` | Remove call from queue |

### Strategy Management
| Method | Path | Description |
|---|---|---|
| `POST` | `/routing/strategies` | Create routing strategy |
| `GET` | `/routing/strategies` | List routing strategies |
| `GET` | `/routing/strategies/{strategy_id}` | Get strategy details |
| `PUT` | `/routing/strategies/{strategy_id}` | Update strategy |
| `DELETE` | `/routing/strategies/{strategy_id}` | Delete strategy |
| `POST` | `/routing/strategies/{strategy_id}/test` | Test strategy with sample data |

### Skills & Priority
| Method | Path | Description |
|---|---|---|
| `POST` | `/routing/skills` | Create skill |
| `GET` | `/routing/skills` | List skills |
| `GET` | `/routing/agents/{agent_id}/skills` | Get agent skills |
| `PUT` | `/routing/agents/{agent_id}/skills` | Update agent skills |
| `GET` | `/routing/priority-levels` | Get priority levels |

## Skill-Based Routing Setup

1. Create skills:
```bash
curl -X POST http://localhost:8007/routing/skills \
  -H "Content-Type: application/json" \
  -d '{"name": "spanish", "description": "Spanish language support", "category": "language"}'
```

2. Assign skills to agents (with proficiency levels 1-5):
```bash
curl -X PUT http://localhost:8007/routing/agents/agent-001/skills \
  -H "Content-Type: application/json" \
  -d '{"skills": ["spanish", "billing"], "skill_levels": {"spanish": 5, "billing": 3}}'
```

3. Route a call requiring skills:
```bash
curl -X POST http://localhost:8007/routing/route \
  -H "Content-Type: application/json" \
  -d '{
    "call_id": "call-001",
    "caller_id": "+1234567890",
    "required_skills": ["spanish"],
    "strategy": "skill_based",
    "priority": "high"
  }'
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `REDIS_URL` | `redis://redis:6379` | Redis connection string |
| `KAFKA_BROKERS` | `kafka:9092` | Kafka brokers |
| `CTI_SERVICE_URL` | `http://cti-service:8006` | CTI service URL for agent availability |
| `INTELLIGENCE_SERVICE_URL` | `http://intelligence-service:8003` | Intelligence service for predictive routing |
| `DEFAULT_ROUTING_STRATEGY` | `skill_based` | Default routing strategy |
| `MAX_QUEUE_SIZE` | `100` | Maximum queue size |
| `MAX_WAIT_TIME` | `300` | Maximum wait time in seconds |
| `SERVICE_LEVEL_TARGET` | `20` | Target answer time for SLA (seconds) |

## Running

### Local Development
```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8007 --reload
```

### Docker
```bash
docker build -t telco-routing-service .
docker run -p 8007:8007 \
  -e REDIS_URL=redis://localhost:6379 \
  -e KAFKA_BROKERS=localhost:9092 \
  telco-routing-service
```

## Integration Points

- **Redis** (`redis:6379`): Queue state, agent availability cache, routing decision cache, statistics
- **Kafka** (`kafka:9092`):
  - Produces to: `routing-events`, `queue-events`
  - Consumes from: `call-events`, `agent-state-events`, `cti-events`
- **CTI Service** (`http://cti-service:8006`): Real-time agent availability
- **Call Orchestrator** (`http://call-orchestrator:8005`): Call lifecycle management
- **Intelligence Service** (`http://intelligence-service:8003`): ML-based predictive routing hints

## Prometheus Metrics

| Metric | Type | Description |
|---|---|---|
| `routing_decisions_total` | Counter | Total routing decisions by strategy and status |
| `routing_confidence_score` | Histogram | Confidence scores for routing decisions |
| `routing_duration_seconds` | Histogram | Time spent on routing decisions |
| `agent_utilization_ratio` | Gauge | Ratio of busy to total agents |
| `queue_length` | Gauge | Number of calls waiting per queue |

## Redis Data Structures

- **Queues**: Sorted sets by priority score + timestamp (`queue:calls:{queue_id}`)
- **Queue config**: JSON hash per queue (`queue:config:{queue_id}`)
- **Agent availability**: JSON per agent (`agent:{agent_id}`)
- **Routing cache**: TTL-based JSON per call (`routing:decision:{call_id}`)
- **Statistics**: Hash counters per queue (`queue:stats:{queue_id}`)

## Routing Algorithm: Skill-Based

1. Extract required skills from the call request
2. Query available agents from CTI service (or Redis fallback)
3. Score each agent based on skill proficiency levels
4. Select agent with highest aggregate skill score
5. In case of tie, prefer agent idle longest
6. If no skill match, return best available agent
7. Cache decision in Redis with 1-hour TTL

## Priority Levels

| Level | SLA Target | Description |
|---|---|---|
| `low` | 120s | Standard low-priority calls |
| `normal` | 60s | Regular calls |
| `high` | 30s | Elevated priority, skill-matched routing |
| `urgent` | 10s | Bypass queue, immediate routing |

