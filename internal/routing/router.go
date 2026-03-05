package routing

import (
	"context"
	"fmt"
	"sort"
	"time"

	"github.com/redis/go-redis/v9"
	"go.uber.org/zap"

	"telco-routing-service/internal/agents"
)

type RoutingRequest struct {
	CallID         string   `json:"call_id"`
	CallerID       string   `json:"caller_id"`
	Destination    string   `json:"destination"`
	QueueID        string   `json:"queue_id"`
	Priority       int      `json:"priority"`
	RequiredSkills []string `json:"required_skills,omitempty"`
	Strategy       string   `json:"strategy,omitempty"` 
}

type RoutingDecision struct {
	CallID    string    `json:"call_id"`
	AgentID   string    `json:"agent_id"`
	QueueID   string    `json:"queue_id"`
	Strategy  string    `json:"strategy"`
	EstWait   int       `json:"estimated_wait_seconds"`
	Timestamp time.Time `json:"timestamp"`
}

type Router struct {
	pool    *agents.Pool
	rdb     *redis.Client
	log     *zap.Logger
	rrIndex map[string]int 
}

func NewRouter(pool *agents.Pool, rdb *redis.Client, log *zap.Logger) *Router {
	return &Router{pool: pool, rdb: rdb, log: log, rrIndex: make(map[string]int)}
}

func (r *Router) Route(ctx context.Context, req RoutingRequest) (*RoutingDecision, error) {
	if req.QueueID == "" {
		req.QueueID = "general"
	}

	available, err := r.pool.GetAvailable(ctx, req.QueueID)
	if err != nil {
		return nil, fmt.Errorf("get available agents: %w", err)
	}

	if len(available) == 0 {
		
		wait := r.estimateWait(ctx, req.QueueID)
		r.log.Warn("no agents available, queuing call",
			zap.String("call_id", req.CallID),
			zap.String("queue", req.QueueID),
		)
		return &RoutingDecision{
			CallID:    req.CallID,
			AgentID:   "",
			QueueID:   req.QueueID,
			Strategy:  "queued",
			EstWait:   wait,
			Timestamp: time.Now(),
		}, nil
	}

	strategy := req.Strategy
	if strategy == "" {
		strategy = "skill_based"
	}

	var selected *agents.Agent
	switch strategy {
	case "round_robin":
		selected = r.roundRobin(req.QueueID, available)
	case "least_busy":
		selected = r.leastBusy(ctx, available)
	case "priority":
		selected = r.priorityBased(req, available)
	default: 
		selected = r.skillBased(req, available)
	}

	if selected == nil {
		selected = available[0]
	}

	r.log.Info("call.routed",
		zap.String("call_id", req.CallID),
		zap.String("agent_id", selected.ID),
		zap.String("strategy", strategy),
	)

	return &RoutingDecision{
		CallID:    req.CallID,
		AgentID:   selected.ID,
		QueueID:   req.QueueID,
		Strategy:  strategy,
		EstWait:   0,
		Timestamp: time.Now(),
	}, nil
}

func (r *Router) roundRobin(queueID string, available []*agents.Agent) *agents.Agent {
	idx := r.rrIndex[queueID] % len(available)
	r.rrIndex[queueID] = idx + 1
	return available[idx]
}

func (r *Router) skillBased(req RoutingRequest, available []*agents.Agent) *agents.Agent {
	type scored struct {
		agent *agents.Agent
		score int
	}
	var candidates []scored
	for _, a := range available {
		total := 0
		for _, skill := range req.RequiredSkills {
			total += a.SkillScore(skill)
		}
		candidates = append(candidates, scored{a, total})
	}
	sort.Slice(candidates, func(i, j int) bool {
		return candidates[i].score > candidates[j].score
	})
	if len(candidates) > 0 {
		return candidates[0].agent
	}
	return nil
}

func (r *Router) leastBusy(_ context.Context, available []*agents.Agent) *agents.Agent {
	var best *agents.Agent
	for _, a := range available {
		if best == nil || a.CallsHandled < best.CallsHandled {
			best = a
		}
	}
	return best
}

func (r *Router) priorityBased(req RoutingRequest, available []*agents.Agent) *agents.Agent {
	if req.Priority >= 8 {
		
		return r.skillBased(req, available)
	}
	return r.roundRobin(req.QueueID, available)
}

func (r *Router) estimateWait(ctx context.Context, queueID string) int {
	count, err := r.rdb.LLen(ctx, "queue:"+queueID).Result()
	if err != nil {
		return 60
	}
	return int(count) * 45 
}
