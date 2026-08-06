package db

import (
	"encoding/json"
	"errors"
	"fmt"
	"qpi/internal/config"

	"github.com/pocketbase/pocketbase/core"
)

type DbModel interface {
	// ToRecord converts this model into a pocketbase record
	ToRecord(app core.App) (*core.Record, error)

	// Refresh self using the values from a pocketbase record
	RefreshFromRecord(record *core.Record) error
}

// getOrCreateRecord loads an existing record by id from the database, or initializes a new one.
func getOrCreateRecord(app core.App, collectionName string, id string, col *core.Collection) (*core.Record, error) {
	if id != "" {
		record, err := app.FindRecordById(collectionName, id)
		if err == nil {
			return record, nil
		}
	}
	record := core.NewRecord(col)
	if id != "" {
		record.Set("id", id)
	}
	return record, nil
}

// User represents a user record in the database.
type User struct {
	ID         string  `json:"id" db:"id"`
	Email      string  `json:"email" db:"email"`
	Username   string  `json:"username" db:"username"`
	QPUSeconds float64 `json:"qpu_seconds" db:"qpu_seconds"`
}

// ToRecord converts this model into a pocketbase record
func (u *User) ToRecord(app core.App) (*core.Record, error) {
	if u == nil {
		return nil, nil
	}

	col_name := "users"
	col, err := app.FindCollectionByNameOrId(col_name)
	if err != nil {
		return nil, fmt.Errorf("error finding collection %s: %w", col_name, err)
	}

	record, err := getOrCreateRecord(app, col_name, u.ID, col)
	if err != nil {
		return nil, err
	}
	record.Set("email", u.Email)
	record.Set("username", u.Username)
	record.Set("qpu_seconds", u.QPUSeconds)

	return record, nil
}

// RefreshFromRecord updates this model using the values from a pocketbase record
func (u *User) RefreshFromRecord(record *core.Record) error {
	if u == nil || record == nil {
		return errors.New("cannot refresh from nil record")
	}

	u.ID = record.Id
	u.Email = record.Email()
	u.Username = record.GetString("username")
	u.QPUSeconds = record.GetFloat("qpu_seconds")

	return nil
}

// APIToken represents an API token record in the database.
type APIToken struct {
	ID        string `json:"id" db:"id"`
	Token     string `json:"token" db:"token" required:"true" maxSelect:"1"`
	User      string `json:"user" db:"user" type:"relation" required:"true" maxSelect:"1" collection:"users"`
	ExpiresAt string `json:"expires_at,omitempty" db:"expires_at" type:"date"`
	Created   string `json:"created" db:"created" type:"autodate" onCreate:"true"`
	Name      string `json:"name" db:"name"`
}

// ToRecord converts this model into a pocketbase record
func (at *APIToken) ToRecord(app core.App) (*core.Record, error) {
	if at == nil {
		return nil, nil
	}
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return nil, err
	}

	col_name := cfg.CollectionAPITokens
	col, err := app.FindCollectionByNameOrId(col_name)
	if err != nil {
		return nil, fmt.Errorf("error finding collection %s: %w", col_name, err)
	}

	record, err := getOrCreateRecord(app, col_name, at.ID, col)
	if err != nil {
		return nil, err
	}
	record.Set("token", at.Token)
	record.Set("user", at.User)
	record.Set("expires_at", at.ExpiresAt)
	record.Set("name", at.Name)

	return record, nil
}

// RefreshFromRecord updates this model using the values from a pocketbase record
func (at *APIToken) RefreshFromRecord(record *core.Record) error {
	if at == nil || record == nil {
		return errors.New("cannot refresh from nil record")
	}

	at.ID = record.Id
	at.Token = record.GetString("token")
	at.User = record.GetString("user")
	at.ExpiresAt = record.GetString("expires_at")
	at.Name = record.GetString("name")

	return nil
}

// QPU represents a QPU record in the database.
type QPU struct {
	ID        string `json:"id" db:"id"`
	Name      string `json:"name" db:"name" required:"true" primaryKey:"true"`
	Status    string `json:"status" db:"status" type:"select" required:"true" maxSelect:"1" values:"offline,online,maintenance"`
	NumQubits int    `json:"num_qubits" db:"num_qubits" min:"1.0"`
	Enabled   bool   `json:"enabled" db:"enabled"`
	Created   string `json:"created" db:"created"`
	Updated   string `json:"updated" db:"updated"`
	QpiAddr   string `json:"qpi_addr,omitempty" db:""`
}

// ToRecord converts this model into a pocketbase record
func (q *QPU) ToRecord(app core.App) (*core.Record, error) {
	if q == nil {
		return nil, nil
	}
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return nil, err
	}

	col_name := cfg.CollectionQPUs
	col, err := app.FindCollectionByNameOrId(col_name)
	if err != nil {
		return nil, fmt.Errorf("error finding collection %s: %w", col_name, err)
	}

	record, err := getOrCreateRecord(app, col_name, q.ID, col)
	if err != nil {
		return nil, err
	}
	record.Set("name", q.Name)
	record.Set("status", q.Status)
	record.Set("num_qubits", q.NumQubits)
	record.Set("enabled", q.Enabled)
	record.Set("created", q.Created)
	record.Set("updated", q.Updated)

	return record, nil
}

// RefreshFromRecord updates this model using the values from a pocketbase record
func (q *QPU) RefreshFromRecord(record *core.Record) error {
	if q == nil || record == nil {
		return errors.New("cannot refresh from nil record")
	}

	q.ID = record.Id
	q.Name = record.GetString("name")
	q.Status = record.GetString("status")
	q.NumQubits = record.GetInt("num_qubits")
	q.Enabled = record.GetBool("enabled")
	q.Created = record.GetString("created")
	q.Updated = record.GetString("updated")

	return nil
}

// Driver represents a registered driver record in the database — an external
// process that exchanges typed events with QPI-UI (RFC 0001 §7). Every driver
// belongs to exactly one QPU; a QPU may have many drivers.
//
// `kind` must allow every kind in `drivers.Default` plus `custom`, and
// `language` every one of `drivers.Languages`; the schema tests pin both.
type Driver struct {
	ID         string   `json:"id" db:"id"`
	Name       string   `json:"name" db:"name" required:"true"`
	QPU        string   `json:"qpu" db:"qpu" type:"relation" required:"true" maxSelect:"1" collection:"qpus"`
	Kind       string   `json:"kind" db:"kind" type:"select" required:"true" maxSelect:"1" values:"mock,qiskit_aer,quantify,qblox,presto,bluefors_gen1,quantify_tuner,qblox_tuner,custom"`
	Language   string   `json:"language" db:"language" type:"select" required:"true" maxSelect:"1" values:"python,typescript,go"`
	Events     []string `json:"events" db:"events" type:"json"`
	Token      string   `json:"token" db:"token" required:"true" hidden:"true"`
	Status     string   `json:"status" db:"status" type:"select" required:"true" maxSelect:"1" values:"offline,online,maintenance"`
	NNGInPort  int      `json:"nng_in_port" db:"nng_in_port"`
	NNGOutPort int      `json:"nng_out_port" db:"nng_out_port"`
	Host       string   `json:"host" db:"host"`
	Version    string   `json:"version" db:"version"`
	LastSeen   string   `json:"last_seen,omitempty" db:"last_seen" type:"date"`
	Enabled    bool     `json:"enabled" db:"enabled"`
	Created    string   `json:"created" db:"created"`
	Updated    string   `json:"updated" db:"updated"`
}

// ToRecord converts this model into a pocketbase record
func (d *Driver) ToRecord(app core.App) (*core.Record, error) {
	if d == nil {
		return nil, nil
	}
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return nil, err
	}

	col_name := cfg.CollectionDrivers
	col, err := app.FindCollectionByNameOrId(col_name)
	if err != nil {
		return nil, fmt.Errorf("error finding collection %s: %w", col_name, err)
	}

	record, err := getOrCreateRecord(app, col_name, d.ID, col)
	if err != nil {
		return nil, err
	}
	record.Set("name", d.Name)
	record.Set("qpu", d.QPU)
	record.Set("kind", d.Kind)
	record.Set("language", d.Language)
	record.Set("token", d.Token)
	record.Set("status", d.Status)
	record.Set("nng_in_port", d.NNGInPort)
	record.Set("nng_out_port", d.NNGOutPort)
	record.Set("host", d.Host)
	record.Set("version", d.Version)
	record.Set("last_seen", d.LastSeen)
	record.Set("enabled", d.Enabled)
	record.Set("created", d.Created)
	record.Set("updated", d.Updated)

	if d.Events != nil {
		eventsJSON, err := json.Marshal(d.Events)
		if err != nil {
			return nil, fmt.Errorf("error marshaling events: %w", err)
		}
		record.Set("events", eventsJSON)
	}

	return record, nil
}

// RefreshFromRecord updates this model using the values from a pocketbase record
func (d *Driver) RefreshFromRecord(record *core.Record) error {
	if d == nil || record == nil {
		return errors.New("cannot refresh from nil record")
	}

	d.ID = record.Id
	d.Name = record.GetString("name")
	d.QPU = record.GetString("qpu")
	d.Kind = record.GetString("kind")
	d.Language = record.GetString("language")
	d.Token = record.GetString("token")
	d.Status = record.GetString("status")
	d.NNGInPort = record.GetInt("nng_in_port")
	d.NNGOutPort = record.GetInt("nng_out_port")
	d.Host = record.GetString("host")
	d.Version = record.GetString("version")
	d.LastSeen = record.GetString("last_seen")
	d.Enabled = record.GetBool("enabled")
	d.Created = record.GetString("created")
	d.Updated = record.GetString("updated")
	d.Events = stringSliceFromJSONField(record.Get("events"))

	return nil
}

// stringSliceFromJSONField normalises the value returned by a JSON field
// holding a string array into a []string, regardless of whether pocketbase
// hands it back as raw JSON text, a decoded []interface{}, or a []string.
func stringSliceFromJSONField(v any) []string {
	switch val := v.(type) {
	case []string:
		return val
	case []interface{}:
		out := make([]string, 0, len(val))
		for _, item := range val {
			if s, ok := item.(string); ok {
				out = append(out, s)
			}
		}
		return out
	case string:
		var out []string
		if err := json.Unmarshal([]byte(val), &out); err == nil {
			return out
		}
	case []byte:
		var out []string
		if err := json.Unmarshal(val, &out); err == nil {
			return out
		}
	}
	return nil
}

// Event represents a single logged entry in the `events` trace collection
// (RFC 0001 §7) — the single log of typed messages exchanged with drivers.
// Phase 3 is its first writer: a driver→UI event whose handler chooses to
// persist it (e.g. CryostatReading), keyed by the driver that sent it and the
// QPU that driver belongs to.
type Event struct {
	ID      string `json:"id" db:"id"`
	Source  string `json:"source" db:"source" required:"true"`
	Driver  string `json:"driver" db:"driver" type:"relation" maxSelect:"1" collection:"drivers"`
	QPU     string `json:"qpu" db:"qpu" type:"relation" maxSelect:"1" collection:"qpus"`
	Type    string `json:"type" db:"type" required:"true"`
	Payload any    `json:"payload" db:"payload" type:"json"`
	Ts      string `json:"ts" db:"ts" required:"true"`
	Created string `json:"created" db:"created" type:"autodate" onCreate:"true"`
}

// ToRecord converts this model into a pocketbase record
func (ev *Event) ToRecord(app core.App) (*core.Record, error) {
	if ev == nil {
		return nil, nil
	}
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return nil, err
	}

	col_name := cfg.CollectionEvents
	col, err := app.FindCollectionByNameOrId(col_name)
	if err != nil {
		return nil, fmt.Errorf("error finding collection %s: %w", col_name, err)
	}

	record, err := getOrCreateRecord(app, col_name, ev.ID, col)
	if err != nil {
		return nil, err
	}
	record.Set("source", ev.Source)
	record.Set("driver", ev.Driver)
	record.Set("qpu", ev.QPU)
	record.Set("type", ev.Type)
	record.Set("ts", ev.Ts)
	record.Set("created", ev.Created)

	if ev.Payload != nil {
		payloadJSON, err := json.Marshal(ev.Payload)
		if err != nil {
			return nil, fmt.Errorf("error marshaling payload: %w", err)
		}
		record.Set("payload", payloadJSON)
	}

	return record, nil
}

// RefreshFromRecord updates this model using the values from a pocketbase record
func (ev *Event) RefreshFromRecord(record *core.Record) error {
	if ev == nil || record == nil {
		return errors.New("cannot refresh from nil record")
	}

	ev.ID = record.Id
	ev.Source = record.GetString("source")
	ev.Driver = record.GetString("driver")
	ev.QPU = record.GetString("qpu")
	ev.Type = record.GetString("type")
	ev.Ts = record.GetString("ts")
	ev.Created = record.GetString("created")
	ev.Payload = record.Get("payload")

	return nil
}

// TimeSlot represents a calendar booking slot in the database.
type TimeSlot struct {
	ID        string `json:"id" db:"id"`
	StartTime string `json:"start_time" type:"date" db:"start_time" required:"true"`
	EndTime   string `json:"end_time" type:"date" db:"end_time" required:"true"`
	BookedBy  string `json:"booked_by" db:"booked_by" type:"relation" required:"true" maxSelect:"1" collection:"users"`
}

// ToRecord converts this model into a pocketbase record
func (ts *TimeSlot) ToRecord(app core.App) (*core.Record, error) {
	if ts == nil {
		return nil, nil
	}
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return nil, err
	}

	col_name := cfg.CollectionTimeSlots
	col, err := app.FindCollectionByNameOrId(col_name)
	if err != nil {
		return nil, fmt.Errorf("error finding collection %s: %w", col_name, err)
	}

	record, err := getOrCreateRecord(app, col_name, ts.ID, col)
	if err != nil {
		return nil, err
	}
	record.Set("start_time", ts.StartTime)
	record.Set("end_time", ts.EndTime)
	record.Set("booked_by", ts.BookedBy)

	return record, nil
}

// RefreshFromRecord updates this model using the values from a pocketbase record
func (ts *TimeSlot) RefreshFromRecord(record *core.Record) error {
	if ts == nil || record == nil {
		return errors.New("cannot refresh from nil record")
	}

	ts.ID = record.Id
	ts.StartTime = record.GetString("start_time")
	ts.EndTime = record.GetString("end_time")
	ts.BookedBy = record.GetString("booked_by")

	return nil
}

// QuantumJob represents a job record in the database.
type QuantumJob struct {
	ID         string  `json:"id" db:"id"`
	UserID     string  `json:"user_id" db:"user_id" type:"relation" maxSelect:"1" collection:"users"`
	QPUTarget  string  `json:"qpu_target" db:"qpu_target" type:"relation" maxSelect:"1" collection:"qpus"`
	Payload    any     `json:"payload" db:"payload" type:"json"`
	Status     string  `json:"status" db:"status" type:"select" maxSelect:"1" values:"pending,running,completed,failed,cancelled" required:"true"`
	FinishedAt string  `json:"finished_at,omitempty" db:"finished_at" type:"date"`
	Duration   float64 `json:"duration,omitempty" db:"duration" min:"0.0"`
	Results    any     `json:"results,omitempty" db:"results" type:"json"`
	Created    string  `json:"created" db:"created" type:"autodate" onCreate:"true"`
	Updated    string  `json:"updated" db:"updated" type:"autodate" onCreate:"true" onUpdate:"true"`
}

// ToRecord converts this model into a pocketbase record
func (qj *QuantumJob) ToRecord(app core.App) (*core.Record, error) {
	if qj == nil {
		return nil, nil
	}
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return nil, err
	}

	col_name := cfg.CollectionQuantumJobs
	col, err := app.FindCollectionByNameOrId(col_name)
	if err != nil {
		return nil, fmt.Errorf("error finding collection %s: %w", col_name, err)
	}

	record, err := getOrCreateRecord(app, col_name, qj.ID, col)
	if err != nil {
		return nil, err
	}
	record.Set("user_id", qj.UserID)
	record.Set("qpu_target", qj.QPUTarget)
	record.Set("status", qj.Status)
	record.Set("finished_at", qj.FinishedAt)
	record.Set("duration", qj.Duration)
	record.Set("created", qj.Created)
	record.Set("updated", qj.Updated)

	if qj.Payload != nil {
		payloadJSON, err := json.Marshal(qj.Payload)
		if err != nil {
			return nil, fmt.Errorf("error marshaling payload: %w", err)
		}
		record.Set("payload", payloadJSON)
	}

	if qj.Results != nil {
		resultsJSON, err := json.Marshal(qj.Results)
		if err != nil {
			return nil, fmt.Errorf("error marshaling results: %w", err)
		}
		record.Set("results", resultsJSON)
	}

	return record, nil
}

// RefreshFromRecord updates this model using the values from a pocketbase record
func (qj *QuantumJob) RefreshFromRecord(record *core.Record) error {
	if qj == nil || record == nil {
		return errors.New("cannot refresh from nil record")
	}

	qj.ID = record.Id
	qj.UserID = record.GetString("user_id")
	qj.QPUTarget = record.GetString("qpu_target")
	qj.Status = record.GetString("status")
	qj.FinishedAt = record.GetString("finished_at")
	qj.Duration = record.GetFloat("duration")
	qj.Created = record.GetString("created")
	qj.Updated = record.GetString("updated")
	qj.Payload = record.Get("payload")
	qj.Results = record.Get("payload")

	return nil
}

// QPUTimeRequest represents a user request for QPU seconds in the database.
type QPUTimeRequest struct {
	ID              string  `json:"id" db:"id"`
	User            string  `json:"user" db:"user" type:"relation" maxSelect:"1" collection:"users"`
	Seconds         float64 `json:"seconds" db:"seconds" min:"0.0" required:"true"`
	Status          string  `json:"status" db:"status" type:"select" maxSelect:"1" values:"pending,approved,rejected" required:"true"`
	RequestedReason string  `json:"requested_reason,omitempty" db:"requested_reason"`
	RejectionReason string  `json:"rejection_reason,omitempty" db:"rejection_reason"`
	HandledBy       string  `json:"handled_by,omitempty" db:"handled_by"`
}

// ToRecord converts this model into a pocketbase record
func (qtr *QPUTimeRequest) ToRecord(app core.App) (*core.Record, error) {
	if qtr == nil {
		return nil, nil
	}
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return nil, err
	}

	col_name := cfg.CollectionQPUTimeRequests
	col, err := app.FindCollectionByNameOrId(col_name)
	if err != nil {
		return nil, fmt.Errorf("error finding collection %s: %w", col_name, err)
	}

	record, err := getOrCreateRecord(app, col_name, qtr.ID, col)
	if err != nil {
		return nil, err
	}
	record.Set("user", qtr.User)
	record.Set("seconds", qtr.Seconds)
	record.Set("status", qtr.Status)
	record.Set("requested_reason", qtr.RequestedReason)
	record.Set("rejection_reason", qtr.RejectionReason)
	record.Set("handled_by", qtr.HandledBy)

	return record, nil
}

// RefreshFromRecord updates this model using the values from a pocketbase record
func (qtr *QPUTimeRequest) RefreshFromRecord(record *core.Record) error {
	if qtr == nil || record == nil {
		return errors.New("cannot refresh from nil record")
	}

	qtr.ID = record.Id
	qtr.User = record.GetString("user")
	qtr.Seconds = record.GetFloat("seconds")
	qtr.Status = record.GetString("status")
	qtr.RequestedReason = record.GetString("requested_reason")
	qtr.RejectionReason = record.GetString("rejection_reason")
	qtr.HandledBy = record.GetString("handled_by")

	return nil
}

// Notification represents an admin notification in the database.
type Notification struct {
	ID          string   `json:"id" db:"id"`
	Title       string   `json:"title" db:"title" required:"true"`
	Description string   `json:"description" db:"description"`
	TargetUsers []string `json:"target_users,omitempty" db:"target_users" type:"relation" maxSelect:"0" collection:"users"`
	DismissedBy []string `json:"dismissed_by,omitempty" db:"dismissed_by" type:"relation" maxSelect:"0" collection:"users"`
	StartTime   string   `json:"start_time,omitempty" db:"start_time" type:"date"`
	EndTime     string   `json:"end_time,omitempty" db:"end_time" type:"date"`
	Created     string   `json:"created" db:"created" type:"autodate" onCreate:"true"`
	Updated     string   `json:"updated" db:"updated" type:"autodate" onCreate:"true" onUpdate:"true"`
}

// ToRecord converts this model into a pocketbase record
func (n *Notification) ToRecord(app core.App) (*core.Record, error) {
	if n == nil {
		return nil, nil
	}
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return nil, err
	}

	col_name := cfg.CollectionNotifications
	col, err := app.FindCollectionByNameOrId(col_name)
	if err != nil {
		return nil, fmt.Errorf("error finding collection %s: %w", col_name, err)
	}

	record, err := getOrCreateRecord(app, col_name, n.ID, col)
	if err != nil {
		return nil, err
	}
	record.Set("title", n.Title)
	record.Set("description", n.Description)
	record.Set("target_users", n.TargetUsers)
	record.Set("dismissed_by", n.DismissedBy)
	record.Set("start_time", n.StartTime)
	record.Set("end_time", n.EndTime)
	record.Set("created", n.Created)
	record.Set("updated", n.Updated)

	return record, nil
}

// RefreshFromRecord updates this model using the values from a pocketbase record
func (n *Notification) RefreshFromRecord(record *core.Record) error {
	if n == nil || record == nil {
		return errors.New("cannot refresh from nil record")
	}

	n.ID = record.Id
	n.Title = record.GetString("title")
	n.Description = record.GetString("description")
	n.TargetUsers = record.GetStringSlice("target_users")
	n.DismissedBy = record.GetStringSlice("dismissed_by")
	n.StartTime = record.GetString("start_time")
	n.EndTime = record.GetString("end_time")
	n.Created = record.GetString("created")
	n.Updated = record.GetString("updated")

	return nil
}

// Theme represents a dashboard theme record containing both light and dark
// colour palettes, shared design tokens, branding, and optional custom CSS/JS
// (RFC 0002 §3.2).
type Theme struct {
	config.ThemeSchema
	// ID        string `json:"id"         db:"id"`
	// Name      string `json:"name"       db:"name"       required:"true"`
	// IsActive  bool   `json:"is_active"  db:"is_active"`
	// SiteName  string `json:"site_name"  db:"site_name"`
	// Tagline   string `json:"tagline"    db:"tagline"`
	// Logo      string `json:"logo"       db:"logo"       type:"file" maxSelect:"1" maxSize:"2097152" mimeTypes:"image/png,image/svg+xml,image/webp"`
	// Favicon   string `json:"favicon"    db:"favicon"     type:"file" maxSelect:"1" maxSize:"524288"  mimeTypes:"image/png,image/svg+xml,image/x-icon,image/webp"`
	// Tokens    any    `json:"tokens"     db:"tokens"      type:"json"`
	// CustomCSS string `json:"custom_css" db:"custom_css"`
	// CustomJS  string `json:"custom_js"  db:"custom_js"`
	// Created   string `json:"created"    db:"created"     type:"autodate" onCreate:"true"`
	// Updated   string `json:"updated"    db:"updated"     type:"autodate" onCreate:"true" onUpdate:"true"`
}

// ToRecord converts this model into a pocketbase record
func (t *Theme) ToRecord(app core.App) (*core.Record, error) {
	if t == nil {
		return nil, nil
	}
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return nil, err
	}

	col_name := cfg.CollectionThemes
	col, err := app.FindCollectionByNameOrId(col_name)
	if err != nil {
		return nil, fmt.Errorf("error finding collection %s: %w", col_name, err)
	}

	record, err := getOrCreateRecord(app, col_name, t.ID, col)
	if err != nil {
		return nil, err
	}
	record.Set("name", t.Name)
	record.Set("is_active", t.IsActive)
	record.Set("site_name", t.SiteName)
	record.Set("tagline", t.Tagline)
	record.Set("logo", t.Logo)
	record.Set("favicon", t.Favicon)
	record.Set("custom_css", t.CustomCSS)
	record.Set("custom_js", t.CustomJS)
	record.Set("created", t.Created)
	record.Set("updated", t.Updated)

	if t.Tokens != nil {
		tokensJSON, err := json.Marshal(t.Tokens)
		if err != nil {
			return nil, fmt.Errorf("error marshaling tokens: %w", err)
		}
		record.Set("tokens", tokensJSON)
	}

	return record, nil
}

// RefreshFromRecord updates this model using the values from a pocketbase record
func (t *Theme) RefreshFromRecord(record *core.Record) error {
	if t == nil || record == nil {
		return errors.New("cannot refresh from nil record")
	}

	t.ID = record.Id
	t.Name = record.GetString("name")
	t.IsActive = record.GetBool("is_active")
	t.SiteName = record.GetString("site_name")
	t.Tagline = record.GetString("tagline")
	t.Logo = record.GetString("logo")
	t.Favicon = record.GetString("favicon")
	if tokensJSON, ok := record.Get("tokens").([]byte); ok {
		if err := json.Unmarshal(tokensJSON, t.Tokens); err != nil {
			return fmt.Errorf("error unmarshaling tokens: %w", err)
		}
	}
	t.CustomCSS = record.GetString("custom_css")
	t.CustomJS = record.GetString("custom_js")
	t.Created = record.GetString("created")
	t.Updated = record.GetString("updated")

	return nil
}

// CalibrationResult represents a calibration result in the database.
//
// QPU is carried alongside Driver for the same reason the events log carries
// both: a driver belongs to a QPU, and a calibration report that cannot be
// attributed to the chip it describes is not much of a record (RFC 0004 §6.8).
type CalibrationResult struct {
	ID             string  `json:"id" db:"id"`
	Driver         string  `json:"driver" db:"driver" type:"relation" required:"true" maxSelect:"1" collection:"drivers"`
	QPU            string  `json:"qpu" db:"qpu" type:"relation" maxSelect:"1" collection:"qpus"`
	Timestamp      string  `json:"timestamp" db:"timestamp" type:"date" required:"true"`
	DurationS      float64 `json:"duration_s" db:"duration_s" min:"0.0"`
	Mode           string  `json:"mode" db:"mode" type:"select" required:"true" maxSelect:"1" values:"full,partial,fidelity_check"`
	Backend        string  `json:"backend,omitempty" db:"backend"`
	RoutineResults any     `json:"routine_results" db:"routine_results" type:"json"`
	Benchmarks     any     `json:"benchmarks" db:"benchmarks" type:"json"`
	Errors         any     `json:"errors" db:"errors" type:"json"`
	Status         string  `json:"status" db:"status" type:"select" required:"true" maxSelect:"1" values:"success,partial_failure,failed"`
	Created        string  `json:"created" db:"created" type:"autodate" onCreate:"true"`
}

// CalibrationRequest is one queued calibration, waiting for its driver's
// dispatcher to pick it up (RFC 0004 §6.8).
//
// It is a collection rather than an in-memory channel because a driver's PUSH
// socket lives inside its dispatcher goroutine and no HTTP handler can reach
// it — and because a calibration is the slowest thing in the system, so losing
// one silently is the worst failure available. Queueing it here means it
// survives a restart, can be requeued on a send error exactly as a job is, and
// runs when the driver reconnects rather than being dropped.
// CalibrationRequest is one queued calibration (RFC 0004 §6.8).
//
// `qpu` is the driver's QPU, copied here at create. It is what lets the job
// scheduler ask "is this chip being calibrated" in one filter — the alternative,
// traversing `driver.qpu`, is a join on the dispatcher's per-second poll.
type CalibrationRequest struct {
	ID           string `json:"id" db:"id"`
	Driver       string `json:"driver" db:"driver" type:"relation" required:"true" maxSelect:"1" collection:"drivers"`
	QPU          string `json:"qpu" db:"qpu" type:"relation" maxSelect:"1" collection:"qpus"`
	Mode         string `json:"mode" db:"mode" type:"select" required:"true" maxSelect:"1" values:"full,partial,fidelity_check"`
	TargetQubits any    `json:"target_qubits" db:"target_qubits" type:"json"`
	TargetEdges  any    `json:"target_edges" db:"target_edges" type:"json"`
	Status       string `json:"status" db:"status" type:"select" required:"true" maxSelect:"1" values:"pending,running,done,failed"`
	// JobID is what the driver calls this calibration when it reports progress and
	// results. For a dispatched one it is this row's own id, which is what the
	// dispatcher sent; for one the driver queued itself it is an id of the driver's
	// making — `drift_check`, or `<id>_recalibrate` — which cannot be a record id.
	JobID string `json:"job_id,omitempty" db:"job_id" type:"text"`
	// Trigger says who wanted this. Empty on rows created before the field, which
	// were all dispatched.
	Trigger string `json:"trigger,omitempty" db:"trigger" type:"select" maxSelect:"1" values:"dispatched,drift"`
	// Progress is where the walk has got to, replaced on each CalibrationProgress
	// event. On the request rather than in its own collection because it is a
	// current position, not history: the report is the history.
	Progress    any    `json:"progress,omitempty" db:"progress" type:"json"`
	RequestedBy string `json:"requested_by,omitempty" db:"requested_by" type:"relation" maxSelect:"1" collection:"users"`
	Created     string `json:"created" db:"created" type:"autodate" onCreate:"true"`
}

// ToRecord converts this model into a pocketbase record
func (cr *CalibrationResult) ToRecord(app core.App) (*core.Record, error) {
	if cr == nil {
		return nil, nil
	}
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return nil, err
	}

	col_name := cfg.CollectionCalibrationResults
	col, err := app.FindCollectionByNameOrId(col_name)
	if err != nil {
		return nil, fmt.Errorf("error finding collection %s: %w", col_name, err)
	}

	record, err := getOrCreateRecord(app, col_name, cr.ID, col)
	if err != nil {
		return nil, err
	}
	record.Set("driver", cr.Driver)
	record.Set("qpu", cr.QPU)
	record.Set("timestamp", cr.Timestamp)
	record.Set("duration_s", cr.DurationS)
	record.Set("mode", cr.Mode)
	record.Set("backend", cr.Backend)
	record.Set("status", cr.Status)
	record.Set("created", cr.Created)

	if cr.RoutineResults != nil {
		resultsJSON, err := json.Marshal(cr.RoutineResults)
		if err != nil {
			return nil, fmt.Errorf("error marshaling routine_results: %w", err)
		}
		record.Set("routine_results", resultsJSON)
	}

	if cr.Benchmarks != nil {
		benchmarksJSON, err := json.Marshal(cr.Benchmarks)
		if err != nil {
			return nil, fmt.Errorf("error marshaling benchmarks: %w", err)
		}
		record.Set("benchmarks", benchmarksJSON)
	}

	if cr.Errors != nil {
		errorsJSON, err := json.Marshal(cr.Errors)
		if err != nil {
			return nil, fmt.Errorf("error marshaling errors: %w", err)
		}
		record.Set("errors", errorsJSON)
	}

	return record, nil
}

// ToRecord converts this model into a pocketbase record
func (cr *CalibrationRequest) ToRecord(app core.App) (*core.Record, error) {
	if cr == nil {
		return nil, nil
	}
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return nil, err
	}

	colName := cfg.CollectionCalibrationRequests
	col, err := app.FindCollectionByNameOrId(colName)
	if err != nil {
		return nil, fmt.Errorf("error finding collection %s: %w", colName, err)
	}

	record, err := getOrCreateRecord(app, colName, cr.ID, col)
	if err != nil {
		return nil, err
	}
	record.Set("driver", cr.Driver)
	record.Set("qpu", cr.QPU)
	record.Set("mode", cr.Mode)
	record.Set("status", cr.Status)
	record.Set("job_id", cr.JobID)
	record.Set("trigger", cr.Trigger)
	record.Set("requested_by", cr.RequestedBy)
	record.Set("created", cr.Created)

	for field, value := range map[string]any{
		"target_qubits": cr.TargetQubits,
		"target_edges":  cr.TargetEdges,
	} {
		if value == nil {
			continue
		}
		encoded, err := json.Marshal(value)
		if err != nil {
			return nil, fmt.Errorf("error marshaling %s: %w", field, err)
		}
		record.Set(field, encoded)
	}

	return record, nil
}

// RefreshFromRecord updates this model using the values from a pocketbase record
func (cr *CalibrationRequest) RefreshFromRecord(record *core.Record) error {
	if cr == nil || record == nil {
		return errors.New("cannot refresh from nil record")
	}
	cr.ID = record.Id
	cr.Driver = record.GetString("driver")
	cr.QPU = record.GetString("qpu")
	cr.Mode = record.GetString("mode")
	cr.Status = record.GetString("status")
	cr.JobID = record.GetString("job_id")
	cr.Trigger = record.GetString("trigger")
	cr.RequestedBy = record.GetString("requested_by")
	cr.Created = record.GetString("created")

	cr.TargetQubits = record.Get("target_qubits")
	cr.TargetEdges = record.Get("target_edges")
	return nil
}

// RefreshFromRecord updates this model using the values from a pocketbase record
func (cr *CalibrationResult) RefreshFromRecord(record *core.Record) error {
	if cr == nil || record == nil {
		return errors.New("cannot refresh from nil record")
	}

	cr.ID = record.Id
	cr.Driver = record.GetString("driver")
	cr.Timestamp = record.GetString("timestamp")
	cr.DurationS = record.GetFloat("duration_s")
	cr.Mode = record.GetString("mode")
	cr.QPU = record.GetString("qpu")
	cr.Backend = record.GetString("backend")
	cr.RoutineResults = record.Get("routine_results")
	cr.Benchmarks = record.Get("benchmarks")
	cr.Errors = record.Get("errors")
	cr.Status = record.GetString("status")
	cr.Created = record.GetString("created")

	return nil
}
