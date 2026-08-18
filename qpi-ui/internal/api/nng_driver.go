package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"sync"
	"time"

	"github.com/pocketbase/dbx"
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
	registry.Register(EventCalibrationProgress, handleCalibrationProgress)
	registry.Register(EventCalibrationQueued, handleCalibrationQueued)
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

// MarkEveryDriverOffline resets every driver's status at startup.
//
// `online` is an observation made by the process that held the socket, so a row
// surviving a crash describes a server that no longer exists — and blocks the
// one-per-role check on a QPU nothing is connected to.
func MarkEveryDriverOffline(app core.App) error {
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return err
	}

	records, err := app.FindRecordsByFilter(
		cfg.CollectionDrivers, "status != 'offline'", "+created", 0, 0,
	)
	if err != nil {
		// The collection does not exist with the driver framework off.
		return nil
	}

	for _, record := range records {
		record.Set("status", "offline")
		if err := app.Save(record); err != nil {
			log.Printf("[QPi] could not mark driver %s offline at startup: %v", record.Id, err)
		}
	}
	if len(records) > 0 {
		log.Printf("[QPi] marked %d driver(s) offline at startup", len(records))
	}
	return nil
}

// ReleaseLeaseIfDriver releases record's lease when record is a driver. Bound to
// the delete hook, so goroutines and a listener do not outlive the record.
func ReleaseLeaseIfDriver(app core.App, record *core.Record) {
	if record == nil {
		return
	}
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return
	}
	if cfg.GetCollectionName(record.Collection().Name) == config.DefaultDriversCollection {
		StopDriverDistribution(record.Id)
	}
}

// sendQPUState tells one driver its QPU's state, reporting whether it went out.
// The caller records the state only on true, so a failed send is retried next tick
// rather than assumed delivered.
func sendQPUState(sock mangos.Socket, driverID, state string) bool {
	event, err := NewEvent(driverID, EventQPUState, QPUStatePayload{State: state})
	if err != nil {
		log.Printf("[DriverDispatcher %s] cannot build QPU state event: %v", driverID, err)
		return false
	}
	payload, err := json.Marshal(event)
	if err != nil {
		log.Printf("[DriverDispatcher %s] cannot marshal QPU state event: %v", driverID, err)
		return false
	}
	if err := sock.Send(payload); err != nil {
		log.Printf("[DriverDispatcher %s] cannot send QPU state %q: %v", driverID, state, err)
		return false
	}
	log.Printf("[DriverDispatcher %s] QPU is %s", driverID, state)
	return true
}

// isDispatching reports whether this server holds goroutines for driverID. Only
// handleDriverConnect adds to activeDrivers, so a restart starts empty rather than
// inheriting a stale claim from the database.
func isDispatching(driverID string) bool {
	activeDriversMu.Lock()
	defer activeDriversMu.Unlock()
	_, running := activeDrivers[driverID]
	return running
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
			// No grace period needed: the port pair stays reserved on the record,
			// so a reconnect rebinds the same two.
			StopDriverDistribution(driverID)
		}
	})

	addr := l.Address()
	if err := l.Listen(); err != nil {
		log.Printf("[DriverDispatcher %s] listen error on %s: %v", driverID, addr, err)
		// Connect already answered 200. Without releasing, StartDriverDistribution
		// treats this driver as served and never retries the bind.
		StopDriverDistribution(driverID)
		return
	}
	log.Printf("[DriverDispatcher %s] PUSH listening on %s", driverID, addr)

	go func() {
		<-ctx.Done()
		sock.Close()
	}()

	// Empty until the first pass, so a new dispatcher asserts the current state
	// once. That is also what a restart does, which is why it cannot wake a QPU
	// somebody switched off.
	lastStateSent := ""

	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		if state := scheduler.ServiceStateOf(app, qpuID); state != lastStateSent {
			if sendQPUState(sock, driverID, state) {
				lastStateSent = state
			}
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

// findCalibrationRequest resolves the job_id a tuner reports against to its queued
// row, or nil when there is none.
//
// A dispatched calibration's job_id is the row's own id, because that is what the
// dispatcher sent it. One the driver queued for itself has an id of the driver's
// making — `drift_check`, or `<id>_recalibrate` — which cannot be a record id: those
// are fifteen lowercase alphanumerics. So it is stored in `job_id` and found there.
func findCalibrationRequest(app core.App, cfg *config.AppConfig, jobID string) *core.Record {
	if record, err := app.FindRecordById(cfg.CollectionCalibrationRequests, jobID); err == nil {
		return record
	}
	record, err := app.FindFirstRecordByFilter(
		cfg.CollectionCalibrationRequests, "job_id = {:jobID}", dbx.Params{"jobID": jobID},
	)
	if err != nil {
		return nil
	}
	return record
}

// handleCalibrationQueued creates the row for a calibration nobody dispatched: a
// driver's periodic drift check, or the recalibration it queues on finding drift
// (RFC 0004 §6.5).
//
// Without it the tab shows a QPU busy for hours and no reason why, and the progress
// those runs report has no row to land on. Created `running` rather than `pending`
// because it is not queued here — the driver has already started it, and this is the
// record of that, not a request.
//
// It also lands the plan (RFC 0006 §5.1), which arrives on a second announcement
// made from the walk itself. That is why a re-announcement is not simply ignored:
// the second one is carrying the thing the first could not know. A dispatched
// calibration makes no announcement of its own and gets its plan by the same route.
func handleCalibrationQueued(ctx context.Context, app core.App, qpuID string, event *Event) error {
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return fmt.Errorf("cannot read config: %w", err)
	}

	var queued CalibrationQueuedPayload
	if err := json.Unmarshal(event.Payload, &queued); err != nil {
		return fmt.Errorf("cannot parse CalibrationQueued payload: %w", err)
	}
	if queued.JobID == "" || queued.Mode == "" {
		return fmt.Errorf("CalibrationQueued payload has no job_id or no mode")
	}
	if err := db.ValidateSelect(app, cfg.CollectionCalibrationRequests, "mode", queued.Mode); err != nil {
		return fmt.Errorf("CalibrationQueued: %w", err)
	}

	// A driver that reconnects mid-run, or one whose announcement is retried, must
	// not leave two rows for the same calibration.
	if existing := findCalibrationRequest(app, cfg, queued.JobID); existing != nil {
		if queued.Plan == nil {
			return nil
		}
		encoded, err := json.Marshal(queued.Plan)
		if err != nil {
			return fmt.Errorf("cannot marshal calibration plan: %w", err)
		}
		existing.Set("plan", encoded)
		if err := app.Save(existing); err != nil {
			return fmt.Errorf("cannot store calibration plan: %w", err)
		}
		return nil
	}

	driverID := driverIDFromContext(ctx)
	request := &db.CalibrationRequest{
		Driver:       driverID,
		QPU:          qpuID,
		Mode:         queued.Mode,
		TargetQubits: queued.TargetQubits,
		Status:       "running",
		JobID:        queued.JobID,
		Trigger:      "drift",
	}
	if queued.Plan != nil {
		request.Plan = queued.Plan
	}
	if err := saveToDb(app, request); err != nil {
		return fmt.Errorf("cannot save self-triggered calibration: %w", err)
	}

	log.Printf("[DriverListener %s] %s calibration %s queued by %s", driverID, queued.Mode, queued.JobID, queued.Reason)
	return nil
}

// handleCalibrationProgress writes where a walk has got to onto the request it
// belongs to, which the dashboard is already subscribed to (RFC 0004 §6.8).
//
// The position replaces the last one; the per-node tallies accumulate over it, so
// the graph can be coloured rather than only a bar drawn (RFC 0006 §5.3). That makes
// it a read-modify-write of the one node the event names, which at a few hundred
// events per calibration is not a load concern.
//
// A missing request is not an error worth reporting: a driver's own drift check
// runs on its clock and answers to no queued row, so it reports progress against
// a job_id nothing here has. The alternative is a log line per routine saying so.
func handleCalibrationProgress(ctx context.Context, app core.App, qpuID string, event *Event) error {
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return fmt.Errorf("cannot read config: %w", err)
	}

	var progress CalibrationProgressPayload
	if err := json.Unmarshal(event.Payload, &progress); err != nil {
		return fmt.Errorf("cannot parse CalibrationProgress payload: %w", err)
	}
	if progress.JobID == "" || progress.Total == 0 {
		return fmt.Errorf("CalibrationProgress payload names no job or no total")
	}

	// Silent on failure, a missing request included: progress is cosmetic, and the
	// result event is the one that has to land.
	record := findCalibrationRequest(app, cfg, progress.JobID)
	if record == nil {
		return nil
	}

	var plan CalibrationPlan
	_ = json.Unmarshal([]byte(record.GetString("plan")), &plan)
	var prior map[string]any
	_ = json.Unmarshal([]byte(record.GetString("progress")), &prior)

	stored := progress.ToMap()
	stored["nodes"] = advanceNodes(prior, &progress, &plan)
	record.Set("progress", stored)
	_ = app.Save(record)
	return nil
}

// CalibrationNodeState is one routine's state within a walk in flight, on the
// request's `progress.nodes` map (RFC 0006 §5.3).
//
// Done counts the targets that have finished, Failed how many of those failed and
// Skipped how many were never run for want of a prerequisite, so the drawing reads
// `3/5` from Done and Total and colours from the other two. Running names the targets
// being measured right now, which is what lets the drawing say *which* components are
// in flight rather than only that the node is (RFC 0009 §7.2).
//
// The states a walk produces are `running`, `done`, `partial`, `failed` and `blocked`;
// `pending`, `skipped` and `not_planned` are properties of the plan and are read from
// it directly.
type CalibrationNodeState struct {
	State   string   `json:"state"`
	Done    int      `json:"done"`
	Total   int      `json:"total"`
	Failed  int      `json:"failed"`
	Skipped int      `json:"skipped"`
	Running []string `json:"running,omitempty"`
}

// advanceNodes folds one progress event into the tallies a walk has accumulated.
//
// prior is the `progress` object already on the row — the previous event's payload,
// node map included — and is nil before the first one.
//
// Two shapes arrive, told apart by whether Running is set. A start event names the
// targets about to be measured and advances no tally; a finish event names the one
// target that is done and advances exactly one of the totals.
//
// Two things make this less obvious than a counter. A finish event carries the walk's
// running totals rather than the outcome of the target it names, so whether that
// target failed is the difference from the last event's total — which is why a start
// event has to carry the same totals rather than zeroes. And a routine stays the
// running one until an event names a different routine, so the walk moving on is what
// settles whatever it was on before.
func advanceNodes(prior map[string]any, event *CalibrationProgressPayload, plan *CalibrationPlan) map[string]CalibrationNodeState {
	nodes := priorNodes(prior)

	node := nodes[event.Routine]
	node.Total = plan.targetCount(event.Routine, node.Total)
	if len(event.Running) > 0 {
		node.Running = event.Running
		node.State = "running"
	} else {
		node.Done++
		if float64(event.Failed) > numberOf(prior["failed"]) {
			node.Failed++
		}
		if float64(event.Skipped) > numberOf(prior["skipped"]) {
			node.Skipped++
		}
		node.Running = without(node.Running, event.Target)
		if node.Total > 0 && node.Done >= node.Total {
			node.State = settledState(node)
		} else {
			node.State = "running"
		}
	}
	nodes[event.Routine] = node

	// The walk has moved on, so whatever it was on before is finished — however
	// short of its total the tally looks, which is the case for a node whose plan
	// the row never received.
	if previous, ok := prior["routine"].(string); ok && previous != event.Routine {
		if done, seen := nodes[previous]; seen && done.State == "running" {
			done.State = settledState(done)
			done.Running = nil
			nodes[previous] = done
		}
	}
	return nodes
}

// settledState is what a node that has finished its targets looks like.
//
// Failure outranks a skip: a node with one of each has something to investigate, and
// reporting it as merely blocked would bury that. `blocked` is every target skipped —
// nothing ran, so neither `done` nor `failed` is true of it (RFC 0007 §11).
func settledState(node CalibrationNodeState) string {
	switch {
	case node.Failed > 0 && node.Failed >= node.Done:
		return "failed"
	case node.Failed > 0:
		return "partial"
	case node.Skipped > 0 && node.Skipped >= node.Done:
		return "blocked"
	case node.Skipped > 0:
		return "partial"
	default:
		return "done"
	}
}

// without is *targets* less *done*, for shrinking the in-flight set as a group's
// targets report back one at a time.
func without(targets []string, done string) []string {
	kept := make([]string, 0, len(targets))
	for _, target := range targets {
		if target != done {
			kept = append(kept, target)
		}
	}
	if len(kept) == 0 {
		return nil
	}
	return kept
}

// priorNodes recovers the accumulated node map from the stored progress object,
// which round-trips through JSON and so arrives as floats in maps.
func priorNodes(prior map[string]any) map[string]CalibrationNodeState {
	nodes := map[string]CalibrationNodeState{}
	stored, ok := prior["nodes"].(map[string]any)
	if !ok {
		return nodes
	}
	for name, value := range stored {
		fields, ok := value.(map[string]any)
		if !ok {
			continue
		}
		state, _ := fields["state"].(string)
		nodes[name] = CalibrationNodeState{
			State:   state,
			Done:    int(numberOf(fields["done"])),
			Total:   int(numberOf(fields["total"])),
			Failed:  int(numberOf(fields["failed"])),
			Skipped: int(numberOf(fields["skipped"])),
			Running: stringsOf(fields["running"]),
		}
	}
	return nodes
}

// stringsOf reads a JSON string array back out of an `any`. Nil for anything that is
// not one, absent included.
func stringsOf(value any) []string {
	items, ok := value.([]any)
	if !ok {
		return nil
	}
	strings := make([]string, 0, len(items))
	for _, item := range items {
		if text, ok := item.(string); ok {
			strings = append(strings, text)
		}
	}
	if len(strings) == 0 {
		return nil
	}
	return strings
}

// numberOf reads a JSON number back out of an `any`, whatever numeric shape the
// decoder chose. Zero for anything that is not one, absent included.
func numberOf(value any) float64 {
	switch typed := value.(type) {
	case float64:
		return typed
	case int:
		return float64(typed)
	}
	return 0
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
