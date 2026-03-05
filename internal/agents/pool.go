package agents

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"
	"go.uber.org/zap"
)

type AgentStatus string

const (
	StatusAvailable AgentStatus = "available"
	StatusBusy      AgentStatus = "busy"
	StatusOffline   AgentStatus = "offline"
)

type Agent struct {
	ID           string         `json:"id"`
	Name         string         `json:"name"`
	Skills       []string       `json:"skills"`
	SkillLevels  map[string]int `json:"skill_levels,omitempty"`
	Status       AgentStatus    `json:"status"`
	QueueID      string         `json:"queue_id,omitempty"`
	CallsHandled int            `json:"calls_handled"`
	LastUpdateAt time.Time      `json:"last_update_at"`
}

func (a *Agent) AvailableForQueue(queueID string) bool {
	if a.Status != StatusAvailable {
		return false
	}
	for _, s := range a.Skills {
		if s == queueID {
			return true
		}
	}
	return len(a.Skills) == 0 
}

func (a *Agent) SkillScore(skill string) int {
	if a.SkillLevels == nil {
		return 1
	}
	return a.SkillLevels[skill]
}

type Pool struct {
	rdb *redis.Client
	log *zap.Logger
}

func NewPool(rdb *redis.Client, log *zap.Logger) *Pool {
	return &Pool{rdb: rdb, log: log}
}

func (p *Pool) agentKey(id string) string { return "agent:" + id }

func (p *Pool) Register(ctx context.Context, agent *Agent) error {
	agent.LastUpdateAt = time.Now()
	data, err := json.Marshal(agent)
	if err != nil {
		return err
	}
	return p.rdb.Set(ctx, p.agentKey(agent.ID), data, 8*time.Hour).Err()
}

func (p *Pool) SetStatus(ctx context.Context, agentID string, status AgentStatus) error {
	agent, err := p.get(ctx, agentID)
	if err != nil {
		return fmt.Errorf("agent %s not found: %w", agentID, err)
	}
	agent.Status = status
	agent.LastUpdateAt = time.Now()
	data, _ := json.Marshal(agent)
	return p.rdb.Set(ctx, p.agentKey(agentID), data, 8*time.Hour).Err()
}

func (p *Pool) get(ctx context.Context, agentID string) (*Agent, error) {
	raw, err := p.rdb.Get(ctx, p.agentKey(agentID)).Bytes()
	if err != nil {
		return nil, err
	}
	var a Agent
	if err := json.Unmarshal(raw, &a); err != nil {
		return nil, err
	}
	return &a, nil
}

func (p *Pool) GetAvailable(ctx context.Context, queueID string) ([]*Agent, error) {
	keys, err := p.rdb.Keys(ctx, "agent:*").Result()
	if err != nil {
		return nil, err
	}
	var result []*Agent
	for _, k := range keys {
		raw, err := p.rdb.Get(ctx, k).Bytes()
		if err != nil {
			continue
		}
		var a Agent
		if err := json.Unmarshal(raw, &a); err != nil {
			continue
		}
		if a.AvailableForQueue(queueID) {
			result = append(result, &a)
		}
	}
	return result, nil
}

func (p *Pool) TotalAvailable() int {
	ctx := context.Background()
	agents, err := p.GetAvailable(ctx, "")
	if err != nil {
		return 0
	}
	count := 0
	for _, a := range agents {
		if a.Status == StatusAvailable {
			count++
		}
	}
	return count
}
