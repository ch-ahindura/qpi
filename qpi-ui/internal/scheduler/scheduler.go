// Package scheduler manages the job queue dispatcher algorithm and recovery loops,
// prioritizing booked session users and reverting hung quantum jobs.
package scheduler

import (
	"encoding/json"
	"log"
	"time"

	"qpi/internal/config"
	"qpi/internal/db"
	"qpi/internal/lib"

	"github.com/pocketbase/dbx"
	"github.com/pocketbase/pocketbase/core"
	"github.com/pocketbase/pocketbase/tools/types"
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

	if reason := UnavailableReason(app, qpuID); reason != "" {
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

// Service states a QPU can be in, as told to its drivers.
const (
	StateOnline      = "online"
	StateMaintenance = "maintenance"
	StateDisabled    = "disabled"
)

// UnavailableReason says why qpuID is not taking jobs, or "" when it is. Both the
// dispatcher and job submission show it to whoever is waiting.
func UnavailableReason(app core.App, qpuID string) string {
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return ""
	}

	qpu, err := app.FindRecordById(cfg.CollectionQPUs, qpuID)
	if err != nil {
		// A job naming a QPU that does not exist fails on its relation, not here.
		return ""
	}
	if !qpu.GetBool("enabled") {
		return "this QPU is switched off"
	}
	if qpu.GetString("status") == StateMaintenance {
		return "this QPU is under maintenance"
	}
	if CalibrationRunningOn(app, qpuID) {
		return "this QPU is being calibrated"
	}
	return ""
}

// ServiceStateOf is what a QPU's drivers are told it is in.
//
// Narrower than UnavailableReason: a running calibration stops jobs but is not a
// service state, or the tuner running it would be told to stop.
func ServiceStateOf(app core.App, qpuID string) string {
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return StateOnline
	}
	qpu, err := app.FindRecordById(cfg.CollectionQPUs, qpuID)
	if err != nil {
		return StateOnline
	}
	if !qpu.GetBool("enabled") {
		return StateDisabled
	}
	if qpu.GetString("status") == StateMaintenance {
		return StateMaintenance
	}
	return StateOnline
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

// PruneCalibrations applies the three calibration retention policies of RFC 0006 §9
// and returns how many rows each touched.
//
// They are three because what accumulates differs in how durable its value is. A
// finished request is bookkeeping — once the report is stored it holds nothing the
// report does not — so it is pruned by default. A report is what the chip *was* and is
// never pruned unless an operator asks. A report's fit summaries are ~90% of its size
// and the least durable part of its value, so they go separately from the report
// carrying them: nobody re-reads the Rabi trace from eight months ago, but the fitted
// amp180 is the record.
//
// A no-op when the driver framework is off, since neither collection exists then.
func PruneCalibrations(app core.App) (requests, results, stripped int, err error) {
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return 0, 0, 0, err
	}

	if cfg.CalibrationRequestRetention > 0 {
		// A `running` or `pending` row is never pruned however old: a hung
		// calibration would otherwise lose its record while still running, and the
		// per-QPU checks that read it would stop seeing the chip as busy.
		requests, err = pruneOlderThan(app, cfg.CollectionCalibrationRequests,
			"status != 'running' && status != 'pending' && created < {:cutoff}",
			cfg.CalibrationRequestRetention)
		if err != nil {
			return requests, 0, 0, err
		}
	}

	if cfg.CalibrationResultRetention > 0 {
		results, err = pruneOlderThan(app, cfg.CollectionCalibrationResults,
			"created < {:cutoff}", cfg.CalibrationResultRetention)
		if err != nil {
			return requests, results, 0, err
		}
	}

	if cfg.CalibrationFitRetention > 0 {
		stripped, err = stripAgedFits(app, cfg)
	}
	return requests, results, stripped, err
}

// pruneOlderThan deletes rows from *collection* matching *filter* — which must take a
// `cutoff` parameter — in the same bounded batches PruneEvents uses.
func pruneOlderThan(app core.App, collection, filter string, retention time.Duration) (int, error) {
	cutoff := time.Now().UTC().Add(-retention).Format("2006-01-02 15:04:05.000Z")

	pruned := 0
	for {
		stale, err := app.FindRecordsByFilter(
			collection, filter, "+created", eventsPruneBatchSize, 0,
			dbx.Params{"cutoff": cutoff},
		)
		if err != nil {
			return pruned, err
		}
		if len(stale) == 0 {
			return pruned, nil
		}
		deleted := 0
		for _, record := range stale {
			if err := app.Delete(record); err != nil {
				log.Printf("[Retention] failed to delete %s %s: %v", collection, record.Id, err)
				continue
			}
			deleted++
		}
		pruned += deleted
		if len(stale) < eventsPruneBatchSize || deleted == 0 {
			return pruned, nil
		}
	}
}

// stripAgedFits removes `fit` from each routine result and `raw_data` from each
// benchmark on reports past cfg.CalibrationFitRetention, leaving the report itself and
// every fitted number in it untouched.
//
// The rows are found by filtering on age alone and then checking whether anything
// changed, rather than by asking the database whether a JSON column contains a key:
// that is what keeps this working across the two shapes a report can have — one from
// before phase 3, and one whose summaries the driver already dropped for size.
func stripAgedFits(app core.App, cfg *config.AppConfig) (int, error) {
	cutoff := time.Now().UTC().Add(-cfg.CalibrationFitRetention).Format("2006-01-02 15:04:05.000Z")

	aged, err := app.FindRecordsByFilter(
		cfg.CollectionCalibrationResults, "created < {:cutoff}",
		"+created", eventsPruneBatchSize, 0, dbx.Params{"cutoff": cutoff},
	)
	if err != nil {
		return 0, err
	}

	stripped := 0
	for _, record := range aged {
		results, resultsChanged := withoutKey(record.GetString("routine_results"), "fit")
		benchmarks, benchmarksChanged := withoutKey(record.GetString("benchmarks"), "raw_data")
		if !resultsChanged && !benchmarksChanged {
			continue
		}
		if resultsChanged {
			record.Set("routine_results", results)
		}
		if benchmarksChanged {
			record.Set("benchmarks", benchmarks)
		}
		if err := app.Save(record); err != nil {
			log.Printf("[Retention] failed to strip traces from report %s: %v", record.Id, err)
			continue
		}
		stripped++
	}
	return stripped, nil
}

// withoutKey removes *key* from every object in the stored JSON array, reporting
// whether it removed any. Unparseable or key-free JSON comes back untouched.
func withoutKey(stored, key string) (types.JSONRaw, bool) {
	var rows []map[string]any
	if err := json.Unmarshal([]byte(stored), &rows); err != nil {
		return nil, false
	}
	changed := false
	for _, row := range rows {
		if _, ok := row[key]; ok {
			delete(row, key)
			changed = true
		}
	}
	if !changed {
		return nil, false
	}
	encoded, err := json.Marshal(rows)
	if err != nil {
		return nil, false
	}
	return types.JSONRaw(encoded), true
}

// RunEventsRetentionEngine runs a background loop that periodically prunes the
// events log and the calibration path to keep their growth bounded, copying the
// shape of RunRecoveryEngine. It exits immediately when the driver framework is off
// or every retention is disabled, so a legacy deployment starts no extra goroutine.
func RunEventsRetentionEngine(app core.App) {
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		log.Printf("[Retention] failed to get config: %v", err)
		return
	}
	// Nothing in the calibration path was pruned before this; the gap was
	// pre-existing (RFC 0006 §9), so those durations keep the engine running even
	// where an operator has switched the events log off.
	if cfg.EventsRetention <= 0 && cfg.CalibrationRequestRetention <= 0 &&
		cfg.CalibrationResultRetention <= 0 && cfg.CalibrationFitRetention <= 0 {
		return
	}

	ticker := time.NewTicker(cfg.EventsPruneInterval)
	defer ticker.Stop()
	log.Printf("[Retention] Engine started (events=%s, calibration requests=%s, reports=%s, fits=%s, interval=%s)",
		cfg.EventsRetention, cfg.CalibrationRequestRetention,
		cfg.CalibrationResultRetention, cfg.CalibrationFitRetention, cfg.EventsPruneInterval)

	for range ticker.C {
		if pruned, err := PruneEvents(app); err != nil {
			log.Printf("[Retention] prune error: %v", err)
		} else if pruned > 0 {
			log.Printf("[Retention] pruned %d expired events", pruned)
		}

		requests, results, stripped, err := PruneCalibrations(app)
		if err != nil {
			log.Printf("[Retention] calibration prune error: %v", err)
		}
		if requests+results+stripped > 0 {
			log.Printf("[Retention] pruned %d finished calibration request(s) and %d report(s), stripped traces from %d",
				requests, results, stripped)
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
