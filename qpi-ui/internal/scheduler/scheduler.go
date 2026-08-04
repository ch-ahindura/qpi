// Package scheduler manages the job queue dispatcher algorithm and recovery loops,
// prioritizing booked session users and reverting hung quantum jobs.
package scheduler

import (
	"log"
	"time"

	"qpi/internal/config"
	"qpi/internal/db"
	"qpi/internal/lib"

	"github.com/pocketbase/dbx"
	"github.com/pocketbase/pocketbase/core"
)

// FetchNextJob implements the session-based booking + opportunistic FIFO algorithm.
// It prioritizes the booked user's oldest pending job. If no slot is active, or if
// the booked user remains idle beyond cfg.IdleThreshold, it falls back to the oldest
// pending job from any user targetted at this QPU.
func FetchNextJob(app core.App, qpuID string) *db.QuantumJob {
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		log.Printf("[Scheduler] failed to get config: %v", err)
		return nil
	}

	if reason := QPUUnavailable(app, qpuID); reason != "" {
		return nil
	}

	now := lib.GetUtcNow()

	// Is there an active time slot right now?
	slots, _ := app.FindRecordsByFilter(
		cfg.CollectionTimeSlots,
		"start_time <= {:now} && end_time >= {:now}",
		"-start_time", 1, 0,
		dbx.Params{"now": now},
	)

	var bookerID string
	if len(slots) > 0 {
		bookerID = slots[0].GetString("booked_by")
	}

	if bookerID != "" {
		// Priority 1: booked user's oldest pending job for this QPU
		jobs, _ := app.FindRecordsByFilter(
			cfg.CollectionQuantumJobs,
			"status = 'pending' && qpu_target = {:qpu} && user_id = {:user}",
			"+created", 1, 0,
			dbx.Params{"qpu": qpuID, "user": bookerID},
		)
		if len(jobs) > 0 {
			return recordToQuantumJob(jobs[0])
		}

		// Priority 2: idle fallback — check booker's last completed job time
		lastJobs, _ := app.FindRecordsByFilter(
			cfg.CollectionQuantumJobs,
			"status = 'completed' && qpu_target = {:qpu} && user_id = {:user}",
			"-finished_at", 1, 0,
			dbx.Params{"qpu": qpuID, "user": bookerID},
		)
		if len(lastJobs) > 0 {
			finishedAt := lastJobs[0].GetDateTime("finished_at").Time()
			if time.Since(finishedAt) < cfg.IdleThreshold {
				// Booker is still active; wait for their next job
				return nil
			}
		}
	}

	// Priority 3: general drop-in — oldest pending job for this QPU
	jobs, _ := app.FindRecordsByFilter(
		cfg.CollectionQuantumJobs,
		"status = 'pending' && qpu_target = {:qpu}",
		"+created", 1, 0,
		dbx.Params{"qpu": qpuID},
	)
	if len(jobs) > 0 {
		return recordToQuantumJob(jobs[0])
	}
	return nil
}

// recordToQuantumJob converts a pocketbase record into a QuantumJob model.
func recordToQuantumJob(record *core.Record) *db.QuantumJob {
	var job db.QuantumJob
	if err := job.RefreshFromRecord(record); err != nil {
		log.Printf("[Scheduler] failed to convert record to QuantumJob: %v", err)
		return nil
	}
	return &job
}

// QPUUnavailable says why qpuID is not taking jobs, or "" when it is.
//
// Three reasons, and the message is the reason: a user told only that their job is
// queued learns nothing, and an operator looking at an idle queue has to guess
// between a switched-off QPU, one under maintenance, and a calibration in progress.
//
// Read by both the dispatcher and job submission. The dispatcher is the one that
// must not be skipped: a job accepted while the QPU was online must not run once it
// goes into maintenance, or once a calibration starts, a second later.
func QPUUnavailable(app core.App, qpuID string) string {
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return ""
	}

	qpu, err := app.FindRecordById(cfg.CollectionQPUs, qpuID)
	if err != nil {
		// No QPU to be unavailable. A job naming one that does not exist fails
		// elsewhere, on its relation.
		return ""
	}
	if !qpu.GetBool("enabled") {
		return "this QPU is switched off"
	}
	if qpu.GetString("status") == "maintenance" {
		return "this QPU is under maintenance"
	}
	if CalibrationRunningOn(app, qpuID) {
		return "this QPU is being calibrated"
	}
	return ""
}

// CalibrationRunningOn reports whether a calibration is in flight on qpuID.
//
// Read from the request's own `qpu` rather than by traversing `driver.qpu`: this is
// asked on every dispatcher tick, by both queues.
func CalibrationRunningOn(app core.App, qpuID string) bool {
	if qpuID == "" {
		return false
	}
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return false
	}
	running, _ := app.FindRecordsByFilter(
		cfg.CollectionCalibrationRequests,
		"status = 'running' && qpu = {:qpu}",
		"+created", 1, 0,
		dbx.Params{"qpu": qpuID},
	)
	return len(running) > 0
}

// FetchNextCalibration returns the oldest pending calibration queued for
// driverID, or nil when there is none or one is already running on its QPU
// (RFC 0004 §6.8).
//
// One at a time, deliberately: one chip cannot be calibrated twice at once. The
// wait is scoped to the QPU rather than the driver because nothing stops two
// tuners being registered against one QPU, and each gets its own dispatcher — so
// serializing per driver would let both sweep the same qubits and both write the
// device YAML, leaving it a mix of two runs.
func FetchNextCalibration(app core.App, driverID string) *db.CalibrationRequest {
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		log.Printf("[Scheduler] failed to get config: %v", err)
		return nil
	}

	driver, err := app.FindRecordById(cfg.CollectionDrivers, driverID)
	if err != nil {
		return nil
	}

	if CalibrationRunningOn(app, driver.GetString("qpu")) {
		return nil
	}

	pending, _ := app.FindRecordsByFilter(
		cfg.CollectionCalibrationRequests,
		"status = 'pending' && driver = {:driver}",
		"+created", 1, 0,
		dbx.Params{"driver": driverID},
	)
	if len(pending) == 0 {
		return nil
	}

	var request db.CalibrationRequest
	if err := request.RefreshFromRecord(pending[0]); err != nil {
		log.Printf("[Scheduler] failed to convert record to CalibrationRequest: %v", err)
		return nil
	}
	return &request
}

// eventsPruneBatchSize bounds how many expired events a single prune query
// pulls, mirroring the recovery engine's batched scan.
const eventsPruneBatchSize = 500

// PruneEvents deletes events log entries older than cfg.EventsRetention and
// returns how many it removed. It is a no-op when the driver framework is off
// (the events collection does not exist then) or when retention is disabled
// (EventsRetention <= 0). It deletes in bounded batches so a large backlog
// cannot block on one huge transaction (RFC 0001 §7, §11).
func PruneEvents(app core.App) (int, error) {
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return 0, err
	}
	if cfg.EventsRetention <= 0 {
		return 0, nil
	}

	cutoff := time.Now().UTC().Add(-cfg.EventsRetention).Format("2006-01-02T15:04:05.000Z")

	pruned := 0
	for {
		stale, err := app.FindRecordsByFilter(
			cfg.CollectionEvents,
			"ts < {:cutoff}",
			"+ts", eventsPruneBatchSize, 0,
			dbx.Params{"cutoff": cutoff},
		)
		if err != nil {
			return pruned, err
		}
		if len(stale) == 0 {
			break
		}
		deleted := 0
		for _, record := range stale {
			if err := app.Delete(record); err != nil {
				log.Printf("[Retention] failed to delete event %s: %v", record.Id, err)
				continue
			}
			deleted++
		}
		pruned += deleted
		// Stop when the window is exhausted, or when a full batch made no
		// progress (every delete failed) so we never spin on the same rows.
		if len(stale) < eventsPruneBatchSize || deleted == 0 {
			break
		}
	}
	return pruned, nil
}

// RunEventsRetentionEngine runs a background loop that periodically prunes the
// events log to keep its growth bounded, copying the shape of
// RunRecoveryEngine. It exits immediately when the driver framework is off or
// retention is disabled, so a legacy deployment starts no extra goroutine.
func RunEventsRetentionEngine(app core.App) {
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		log.Printf("[Retention] failed to get config: %v", err)
		return
	}
	if cfg.EventsRetention <= 0 {
		return
	}

	ticker := time.NewTicker(cfg.EventsPruneInterval)
	defer ticker.Stop()
	log.Printf("[Retention] Engine started (retention=%s, interval=%s)", cfg.EventsRetention, cfg.EventsPruneInterval)

	for range ticker.C {
		pruned, err := PruneEvents(app)
		if err != nil {
			log.Printf("[Retention] prune error: %v", err)
			continue
		}
		if pruned > 0 {
			log.Printf("[Retention] pruned %d expired events", pruned)
		}
	}
}

// RunRecoveryEngine runs a background loop that identifies 'running' jobs
// that have exceeded cfg.JobTimeout and resets their status to 'pending'
// (e.g. if the QPU driver crashed or lost connection during simulation).
func RunRecoveryEngine(app core.App) {
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		log.Printf("[Recovery] failed to get config: %v", err)
		return
	}

	ticker := time.NewTicker(cfg.RecoveryInterval)
	defer ticker.Stop()
	log.Println("[Recovery] Engine started")

	for range ticker.C {
		cutoff := time.Now().UTC().Add(-cfg.JobTimeout).Format("2006-01-02 15:04:05.000Z")
		staleJobs, err := app.FindRecordsByFilter(
			cfg.CollectionQuantumJobs,
			"status = 'running' && updated <= {:cutoff}",
			"+updated", 100, 0,
			dbx.Params{"cutoff": cutoff},
		)
		if err != nil {
			log.Printf("[Recovery] query error: %v", err)
			continue
		}
		for _, job := range staleJobs {
			job.Set("status", "pending")
			if err := app.Save(job); err != nil {
				log.Printf("[Recovery] failed to reset job %s: %v", job.Id, err)
			} else {
				log.Printf("[Recovery] reset stale job %s to pending", job.Id)
			}
		}
	}
}
