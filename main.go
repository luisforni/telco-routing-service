package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/redis/go-redis/v9"
	"go.uber.org/zap"

	"telco-routing-service/internal/agents"
	appkafka "telco-routing-service/internal/kafka"
	"telco-routing-service/internal/routing"
)

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func main() {
	log, _ := zap.NewProduction()
	defer log.Sync()

	rdb := redis.NewClient(&redis.Options{
		Addr:     getEnv("REDIS_ADDR", "redis:6379"),
		Password: getEnv("REDIS_PASSWORD", ""),
		PoolSize: 20,
	})

	agentPool := agents.NewPool(rdb, log)
	router := routing.NewRouter(agentPool, rdb, log)

	
	producer, err := appkafka.NewProducer(getEnv("KAFKA_BROKERS", "kafka:9092"), log)
	if err != nil {
		log.Fatal("kafka producer", zap.Error(err))
	}
	defer producer.Close()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	
	consumer, err := appkafka.NewConsumer(
		getEnv("KAFKA_BROKERS", "kafka:9092"),
		getEnv("CONSUMER_GROUP", "routing-service"),
		[]string{getEnv("TOPIC_ROUTING_REQ", "routing-requests")},
		func(ctx context.Context, evt appkafka.RawEvent) error {
			var req routing.RoutingRequest
			if err := json.Unmarshal(evt.Value, &req); err != nil {
				return fmt.Errorf("unmarshal routing req: %w", err)
			}
			decision, err := router.Route(ctx, req)
			if err != nil {
				log.Error("routing error", zap.Error(err))
				return err
			}
			topic := getEnv("TOPIC_ROUTING_DECISIONS", "routing-decisions")
			return producer.Publish(topic, decision.CallID, decision)
		},
		log,
	)
	if err != nil {
		log.Fatal("kafka consumer", zap.Error(err))
	}
	go consumer.Start(ctx)

	
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	r.Use(gin.Recovery())
	r.GET("/healthz", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"status": "ok", "available_agents": agentPool.TotalAvailable()})
	})
	r.GET("/metrics", gin.WrapH(promhttp.Handler()))

	
	v1 := r.Group("/api/v1")
	{
		v1.POST("/agents", func(c *gin.Context) {
			var agent agents.Agent
			if err := c.ShouldBindJSON(&agent); err != nil {
				c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
				return
			}
			if err := agentPool.Register(c.Request.Context(), &agent); err != nil {
				c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
				return
			}
			c.JSON(http.StatusCreated, agent)
		})
		v1.PUT("/agents/:id/status", func(c *gin.Context) {
			var body struct{ Status agents.AgentStatus `json:"status"` }
			if err := c.ShouldBindJSON(&body); err != nil {
				c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
				return
			}
			if err := agentPool.SetStatus(c.Request.Context(), c.Param("id"), body.Status); err != nil {
				c.JSON(http.StatusNotFound, gin.H{"error": err.Error()})
				return
			}
			c.JSON(http.StatusOK, gin.H{"status": "updated"})
		})
		v1.POST("/route", func(c *gin.Context) {
			var req routing.RoutingRequest
			if err := c.ShouldBindJSON(&req); err != nil {
				c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
				return
			}
			decision, err := router.Route(c.Request.Context(), req)
			if err != nil {
				c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
				return
			}
			c.JSON(http.StatusOK, decision)
		})
	}

	srv := &http.Server{Addr: ":8080", Handler: r}
	go srv.ListenAndServe()

	log.Info("routing service started")
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit
	cancel()
	ctx2, cancel2 := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel2()
	srv.Shutdown(ctx2)
}
