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
	if err := saveToDb(app, request); err != nil {
		return fmt.Errorf("cannot save self-triggered calibration: %w", err)
	}

	log.Printf("[DriverListener %s] %s calibration %s queued by %s", driverID, queued.Mode, queued.JobID, queued.Reason)
	return nil
}

// handleCalibrationProgress writes where a walk has got to onto the request it
// belongs to, which the dashboard is already subscribed to (RFC 0004 §6.8).
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
	record.Set("progress", progress.ToMap())
	_ = app.Save(record)
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
