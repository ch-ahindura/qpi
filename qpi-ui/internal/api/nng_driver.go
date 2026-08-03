package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"sync"
	"time"

	"github.com/pocketbase/pocketbase/core"
	"github.com/pocketbase/pocketbase/tools/types"
	"go.nanomsg.org/mangos/v3"
	"go.nanomsg.org/mangos/v3/protocol/pull"
	"go.nanomsg.org/mangos/v3/protocol/push"
	_ "go.nanomsg.org/mangos/v3/transport/tlstcp"

	"qpi/internal/config"
	"qpi/internal/db"
	"qpi/internal/lib"
	"qpi/internal/scheduler"
)

var (
	// activeDrivers stores active cancel functions for goroutines bound to
	// connected drivers, keyed by driver id (RFC 0001 Phase 2).
	activeDrivers   = make(map[string]context.CancelFunc)
	activeDriversMu sync.Mutex
)

// driverEventRegistry maps inbound driver→UI event types to their handlers. It
// is the production counterpart to the spike registry in events_test.go: the
// server holds one handler per type it receives (RFC 0001 §7).
var driverEventRegistry = func() *EventRegistry {
	registry := NewEventRegistry()
	registry.Register(EventJobResult, handleDriverJobResult)
	registry.Register(EventCryostatReading, handleCryostatReading)
	registry.Register(EventCalibrationResult, handleCalibrationResult)
	return registry
}()

// driverIDContextKey is how runDriverListener passes the calling driver's
// record id to handlers through ctx, without widening the EventHandler
// signature every existing handler and test would need to update (RFC 0001
// §7). A handler that needs it (e.g. to persist to the events log) reads it
// back with driverIDFromContext.
type driverIDContextKey struct{}

// driverIDFromContext extracts the driver id runDriverListener attached to
// ctx, or "" if it is missing (e.g. a handler invoked directly from a test).
func driverIDFromContext(ctx context.Context) string {
	id, _ := ctx.Value(driverIDContextKey{}).(string)
	return id
}

// StartDriverDistribution starts the dispatch/listen goroutines for a connected
// driver if not already running, mirroring StartQPUDistribution for QPUs.
func StartDriverDistribution(app core.App, cfg *config.AppConfig, driverID, qpuID string, inPort, outPort int) {
	activeDriversMu.Lock()
	defer activeDriversMu.Unlock()
	if _, running := activeDrivers[driverID]; !running {
		ctx, cancel := context.WithCancel(context.Background())
		activeDrivers[driverID] = cancel
		go runDriverDispatcher(ctx, app, driverID, qpuID, inPort)
		go runDriverListener(ctx, app, driverID, qpuID, outPort)
		log.Printf("[QPi] Driver goroutines started for %s (in:%d out:%d)", driverID, inPort, outPort)
	}
}

// StopDriverDistribution cancels the goroutines for a specific driver.
func StopDriverDistribution(driverID string) {
	activeDriversMu.Lock()
	defer activeDriversMu.Unlock()
	if cancel, exists := activeDrivers[driverID]; exists {
		cancel()
		delete(activeDrivers, driverID)
		log.Printf("[QPi] Driver goroutines stopped for %s", driverID)
	}
}

// runDriverDispatcher pushes pending jobs for the driver's QPU as JobDispatch
// events over an NNG PUSH socket on inPort. It copies runDispatcher, differing
// only in that each job travels inside the event envelope (RFC 0001 §6) and the
// pipe hook flips both the driver's and its QPU's online/offline status.
func runDriverDispatcher(ctx context.Context, app core.App, driverID, qpuID string, inPort int) {
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		log.Printf("[DriverDispatcher %s] failed to get config: %v", driverID, err)
		return
	}

	sock, err := push.NewSocket()
	if err != nil {
		log.Printf("[DriverDispatcher %s] socket error: %v", driverID, err)
		return
	}
	defer sock.Close()

	l, err := getListener(sock, inPort, cfg.GetTlsConfig())
	if err != nil {
		log.Printf("[DriverDispatcher %s] %v", driverID, err)
		return
	}

	sock.SetPipeEventHook(func(event mangos.PipeEvent, pipe mangos.Pipe) {
		switch event {
		case mangos.PipeEventAttached:
			log.Printf("[DriverDispatcher %s] driver attached: %s", driverID, pipe.Address())
			markDriverStatus(app, cfg, driverID, qpuID, "online")
		case mangos.PipeEventDetached:
			log.Printf("[DriverDispatcher %s] driver disconnected: %s", driverID, pipe.Address())
			markDriverStatus(app, cfg, driverID, qpuID, "offline")
		}
	})

	addr := l.Address()
	if err := l.Listen(); err != nil {
		log.Printf("[DriverDispatcher %s] listen error on %s: %v", driverID, addr, err)
		return
	}
	log.Printf("[DriverDispatcher %s] PUSH listening on %s", driverID, addr)

	go func() {
		<-ctx.Done()
		sock.Close()
	}()

	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		// A calibration takes the QPU out of service for hours, so it is
		// offered before the job queue: dispatching jobs first would mean a
		// busy QPU never calibrates, which is the state calibration exists to
		// get it out of (RFC 0004 §6.8).
		if request := scheduler.FetchNextCalibration(app, driverID); request != nil {
			dispatchCalibration(app, cfg, sock, driverID, request)
			continue
		}

		job := scheduler.FetchNextJob(app, qpuID)
		if job == nil {
			select {
			case <-ctx.Done():
				return
			case <-time.After(cfg.DispatchPollInterval):
			}
			continue
		}

		event, err := NewEvent(driverID, EventJobDispatch, DispatchPayload{JobID: job.ID, Payload: job.Payload})
		if err != nil {
			log.Printf("[DriverDispatcher %s] cannot build dispatch for job %s: %v", driverID, job.ID, err)
			continue
		}
		payload, err := json.Marshal(event)
		if err != nil {
			log.Printf("[DriverDispatcher %s] cannot marshal dispatch for job %s: %v", driverID, job.ID, err)
			continue
		}

		if err := sock.Send(payload); err != nil {
			select {
			case <-ctx.Done():
				return
			default:
			}
			log.Printf("[DriverDispatcher %s] send error: %v — requeueing", driverID, err)

			updateData := map[string]any{"status": "pending"}
			var requeuedJob db.QuantumJob
			if updateErr := db.FindAndUpdateOne(app, cfg.CollectionQuantumJobs, job.ID, &requeuedJob, updateData); updateErr != nil {
				log.Printf("[DriverDispatcher %s] failed to requeue job %s: %v", driverID, job.ID, updateErr)
			}

			select {
			case <-ctx.Done():
				return
			case <-time.After(cfg.DispatchPollInterval):
			}
			continue
		}

		updateData := map[string]any{"status": "running"}
		var runningJob db.QuantumJob
		if err := db.FindAndUpdateOne(app, cfg.CollectionQuantumJobs, job.ID, &runningJob, updateData); err != nil {
			log.Printf("[DriverDispatcher %s] DB update error: %v", driverID, err)
		} else {
			log.Printf("[DriverDispatcher %s] dispatched job %s", driverID, job.ID)
		}
	}
}

// runDriverListener receives events emitted by a driver over an NNG PULL socket
// on outPort and routes each through driverEventRegistry. It copies
// runResultListener, differing only in that it parses the event envelope and
// dispatches by type instead of assuming a bare result (RFC 0001 §4, §6).
func runDriverListener(ctx context.Context, app core.App, driverID, qpuID string, outPort int) {
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		log.Printf("[DriverListener %s] failed to get config: %v", driverID, err)
		return
	}

	sock, err := pull.NewSocket()
	if err != nil {
		log.Printf("[DriverListener %s] socket error: %v", driverID, err)
		return
	}
	defer sock.Close()

	l, err := getListener(sock, outPort, cfg.GetTlsConfig())
	if err != nil {
		log.Printf("[DriverListener %s] %v", driverID, err)
		return
	}

	addr := l.Address()
	if err := l.Listen(); err != nil {
		log.Printf("[DriverListener %s] listen error on %s: %v", driverID, addr, err)
		return
	}
	log.Printf("[DriverListener %s] PULL listening on %s", driverID, addr)

	go func() {
		<-ctx.Done()
		sock.Close()
	}()

	// One limiter per driver caps how fast this driver can push events at us;
	// over-rate events are logged and dropped, like any other rejected event
	// (RFC 0001 §7, Phase 5).
	limiter := newRateLimiter(cfg.EventRateLimit)

	for {
		msg, err := sock.Recv()
		if err != nil {
			if err == mangos.ErrClosed {
				return
			}
			select {
			case <-ctx.Done():
				return
			default:
			}
			log.Printf("[DriverListener %s] recv error: %v", driverID, err)
			select {
			case <-ctx.Done():
				return
			case <-time.After(cfg.DispatchPollInterval):
			}
			continue
		}

		if !limiter.Allow() {
			log.Printf("[DriverListener %s] rate limit exceeded, dropping event", driverID)
			continue
		}

		var event Event
		if err := json.Unmarshal(msg, &event); err != nil {
			log.Printf("[DriverListener %s] envelope parse error: %v", driverID, err)
			continue
		}

		// A handler that rejects an event just logs and drops it; the loop
		// keeps listening (RFC 0001 §4).
		dispatchCtx := context.WithValue(ctx, driverIDContextKey{}, driverID)
		_ = driverEventRegistry.Dispatch(dispatchCtx, app, qpuID, &event)
	}
}

// markDriverStatus flips a driver's online/offline status and mirrors it onto
// the driver's QPU. A QPU driver's connection is what makes the QPU available,
// so the QPU status tracks the driver's pipe events the way the legacy
// dispatcher tracked the QPU's own connection (RFC 0001 §5).
func markDriverStatus(app core.App, cfg *config.AppConfig, driverID, qpuID, status string) {
	driverUpdate := map[string]any{
		"status":    status,
		"last_seen": lib.GetUtcNow(),
	}
	var driver db.Driver
	if err := db.FindAndUpdateOne(app, cfg.CollectionDrivers, driverID, &driver, driverUpdate); err != nil {
		log.Printf("[DriverDispatcher %s] failed to mark driver %s: %v", driverID, status, err)
	}

	var qpu db.QPU
	if err := db.FindAndUpdateOne(app, cfg.CollectionQPUs, qpuID, &qpu, map[string]any{"status": status}); err != nil {
		log.Printf("[DriverDispatcher %s] failed to mark QPU %s %s: %v", driverID, qpuID, status, err)
	}
}

// handleDriverJobResult applies a JobResult event to the calling driver's QPU,
// the event-framework counterpart of the body of runResultListener.
func handleDriverJobResult(ctx context.Context, app core.App, qpuID string, event *Event) error {
	var result ResultPayload
	if err := json.Unmarshal(event.Payload, &result); err != nil {
		return fmt.Errorf("cannot parse JobResult payload: %w", err)
	}
	return applyJobResult(app, qpuID, result)
}

// applyJobResult updates a finished job and deducts the QPU-seconds it used,
// mirroring the persistence the legacy result listener performs (RFC 0001 §8).
func applyJobResult(app core.App, qpuID string, result ResultPayload) error {
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return err
	}

	var job db.QuantumJob
	if err := db.FindOne(app, cfg.CollectionQuantumJobs, result.JobID, &job); err != nil {
		return fmt.Errorf("job %s not found: %w", result.JobID, err)
	}

	var executionDuration time.Duration
	if job.Updated != "" {
		if updatedTime, parseErr := time.Parse("2006-01-02 15:04:05.000Z", job.Updated); parseErr == nil {
			executionDuration = time.Since(updatedTime)
		}
	}
	durationSeconds := executionDuration.Seconds()

	if job.UserID != "" {
		deductData := map[string]any{"qpu_seconds-": durationSeconds}
		var user db.User
		if updateErr := db.FindAndUpdateOne(app, "users", job.UserID, &user, deductData); updateErr != nil {
			if errors.Is(updateErr, db.ErrNotFound) {
				log.Printf("[DriverListener %s] user %s not found for QPU seconds deduction", qpuID, job.UserID)
			} else {
				log.Printf("[DriverListener %s] failed to deduct QPU seconds for user %s: %v", qpuID, job.UserID, updateErr)
			}
		}
	}

	finalStatus := "completed"
	if _, hasError := result.Results["error"]; hasError {
		finalStatus = "failed"
	}

	resultsJSON, _ := json.Marshal(result.Results)
	jobUpdate := &JobResultUpdate{
		Status:     finalStatus,
		FinishedAt: lib.GetUtcNow(),
		Results:    string(resultsJSON),
		Duration:   durationSeconds,
	}

	var updatedJob db.QuantumJob
	if err := db.FindAndUpdateOne(app, cfg.CollectionQuantumJobs, result.JobID, &updatedJob, jobUpdate.ToMap()); err != nil {
		return fmt.Errorf("cannot save result for job %s: %w", result.JobID, err)
	}
	log.Printf("[DriverListener %s] job %s %s", qpuID, result.JobID, finalStatus)
	return nil
}

// handleCryostatReading validates a monitoring driver's reading snapshot and
// appends it to the `events` trace log for the dashboard to chart. Unlike
// JobResult it updates no domain record — the events log is its only
// destination (RFC 0001 §7, Phase 3). A payload with no readings is rejected,
// which the registry logs and drops rather than crashing the listener loop.
func handleCryostatReading(ctx context.Context, app core.App, qpuID string, event *Event) error {
	var reading CryostatReadingPayload
	if err := json.Unmarshal(event.Payload, &reading); err != nil {
		return fmt.Errorf("cannot parse CryostatReading payload: %w", err)
	}
	if len(reading.Readings) == 0 {
		return fmt.Errorf("CryostatReading payload has no readings")
	}

	return appendEvent(app, driverIDFromContext(ctx), qpuID, event)
}

// appendEvent persists an inbound event to the `events` trace log, keyed by
// the driver that sent it and the QPU that driver belongs to (RFC 0001 §7).
func appendEvent(app core.App, driverID, qpuID string, event *Event) error {
	record := &db.Event{
		Source:  driverID,
		Driver:  driverID,
		QPU:     qpuID,
		Type:    string(event.Type),
		Payload: event.Payload,
		Ts:      event.Ts,
	}
	if err := saveToDb(app, record); err != nil {
		return fmt.Errorf("cannot persist %s event: %w", event.Type, err)
	}
	return nil
}

// dispatchCalibration pushes one queued calibration to its driver and marks it
// running, requeueing it if the send fails (RFC 0004 §6.8).
//
// Mirrors the job path deliberately, including the requeue: a calibration that
// vanished on a transient socket error would cost hours to notice and hours
// more to redo.
func dispatchCalibration(
	app core.App,
	cfg *config.AppConfig,
	sock mangos.Socket,
	driverID string,
	request *db.CalibrationRequest,
) {
	event, err := NewEvent(driverID, EventCalibrateDispatch, CalibrateDispatchPayload{
		JobID:        request.ID,
		Mode:         request.Mode,
		TargetQubits: toStringSlice(request.TargetQubits),
		TargetEdges:  toStringSlice(request.TargetEdges),
	})
	if err != nil {
		log.Printf("[DriverDispatcher %s] cannot build calibration dispatch %s: %v", driverID, request.ID, err)
		setCalibrationStatus(app, cfg, request.ID, "failed")
		return
	}

	payload, err := json.Marshal(event)
	if err != nil {
		log.Printf("[DriverDispatcher %s] cannot marshal calibration dispatch %s: %v", driverID, request.ID, err)
		setCalibrationStatus(app, cfg, request.ID, "failed")
		return
	}

	if err := sock.Send(payload); err != nil {
		log.Printf("[DriverDispatcher %s] calibration send error: %v — leaving %s pending", driverID, err, request.ID)
		return
	}

	setCalibrationStatus(app, cfg, request.ID, "running")
	log.Printf("[DriverDispatcher %s] dispatched calibration %s (mode %s)", driverID, request.ID, request.Mode)
}

// setCalibrationStatus moves a queued calibration to a new status.
func setCalibrationStatus(app core.App, cfg *config.AppConfig, requestID, status string) {
	var updated db.CalibrationRequest
	data := map[string]any{"status": status}
	if err := db.FindAndUpdateOne(app, cfg.CollectionCalibrationRequests, requestID, &updated, data); err != nil {
		log.Printf("[DriverDispatcher] failed to mark calibration %s as %s: %v", requestID, status, err)
	}
}

// toStringSlice narrows the JSON a queued request stores into the string list
// the dispatch payload declares.
func toStringSlice(value any) []string {
	switch typed := value.(type) {
	case []string:
		return typed
	case []any:
		out := make([]string, 0, len(typed))
		for _, item := range typed {
			if s, ok := item.(string); ok {
				out = append(out, s)
			}
		}
		return out
	case types.JSONRaw:
		var out []string
		if err := json.Unmarshal(typed, &out); err == nil {
			return out
		}
	}
	return nil
}

// handleCalibrationResult stores a tuner's report and closes out the request it
// answers (RFC 0004 §6.8).
//
// The payload is flat — the driver emits the report's fields at the top level
// beside job_id — because this unmarshals it directly. Nested under a "results"
// key it would parse without error and leave every field at its zero value,
// saving a blank record for a real calibration.
func handleCalibrationResult(ctx context.Context, app core.App, qpuID string, event *Event) error {
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return fmt.Errorf("cannot read config: %w", err)
	}

	var result CalibrationResultPayload
	if err := json.Unmarshal(event.Payload, &result); err != nil {
		return fmt.Errorf("cannot parse CalibrationResult payload: %w", err)
	}
	// Validated as CryostatReading validates its readings: a report with no
	// mode or status is not a report, and storing it would put a blank row in
	// front of whoever is trying to work out what the chip is doing.
	if result.Mode == "" || result.Status == "" {
		return fmt.Errorf("CalibrationResult payload has no mode or status")
	}
	// Caught here rather than at the insert, where a listener error is all that is
	// left of the report.
	for _, field := range []struct{ name, value string }{
		{"mode", result.Mode},
		{"status", result.Status},
	} {
		if err := db.ValidateSelect(app, cfg.CollectionCalibrationResults, field.name, field.value); err != nil {
			return fmt.Errorf("CalibrationResult: %w", err)
		}
	}

	driverID := driverIDFromContext(ctx)
	record := &db.CalibrationResult{
		Driver:         driverID,
		QPU:            qpuID,
		Timestamp:      result.Timestamp,
		DurationS:      result.DurationS,
		Mode:           result.Mode,
		Backend:        result.Backend,
		RoutineResults: result.RoutineResults,
		Benchmarks:     result.Benchmarks,
		Errors:         result.Errors,
		Status:         result.Status,
	}

	if err := saveToDb(app, record); err != nil {
		return fmt.Errorf("cannot save calibration result: %w", err)
	}

	// Close out the queued request this answers, so the driver's dispatcher
	// stops treating it as in flight and can offer the next one.
	if result.JobID != "" {
		status := "done"
		if result.Status == "failed" {
			status = "failed"
		}
		setCalibrationStatus(app, cfg, result.JobID, status)
	}

	log.Printf("[DriverListener %s] calibration %s %s", driverID, result.Mode, result.Status)
	return nil
}
