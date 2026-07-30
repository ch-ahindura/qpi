package db

import (
	"fmt"
	"log"
	"reflect"
	"strconv"
	"strings"

	"qpi/internal/config"

	"github.com/pocketbase/pocketbase/core"
	"github.com/pocketbase/pocketbase/tools/types"
)

var (
	stringTypeStr    = reflect.String.String()
	float64TypeStr   = reflect.Float64.String()
	float32TypeStr   = reflect.Float32.String()
	intTypeStr       = reflect.Int.String()
	int64TypeStr     = reflect.Int64.String()
	int32TypeStr     = reflect.Int32.String()
	uintTypeStr      = reflect.Uint.String()
	uint64TypeStr    = reflect.Uint64.String()
	uint32TypeStr    = reflect.Uint32.String()
	boolTypeStr      = reflect.Bool.String()
	interfaceTypeStr = reflect.Interface.String()
)

// EnsureSchema bootstraps the database collections required by the QPI control stack.
// It creates the users, QPUs, Time Slots, and Quantum Jobs collections if they do not exist,
// configuring authentication options and properties based on the loaded AppConfig.
func EnsureSchema(app core.App) error {
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return err
	}

	if err := ensureUsersCollection(app, cfg); err != nil {
		return fmt.Errorf("users collection: %w", err)
	}
	if err := ensureAPITokensCollection(app, cfg); err != nil {
		return fmt.Errorf("api_tokens collection: %w", err)
	}
	if err := ensureQPUsCollection(app, cfg); err != nil {
		return fmt.Errorf("qpus collection: %w", err)
	}
	if err := ensureTimeSlotsCollection(app, cfg); err != nil {
		return fmt.Errorf("time_slots collection: %w", err)
	}
	if err := ensureQuantumJobsCollection(app, cfg); err != nil {
		return fmt.Errorf("quantum_jobs collection: %w", err)
	}
	if err := ensureQPUTimeRequestsCollection(app, cfg); err != nil {
		return fmt.Errorf("qpu_time_requests collection: %w", err)
	}
	if err := ensureNotificationsCollection(app, cfg); err != nil {
		return fmt.Errorf("notifications collection: %w", err)
	}
	if err := ensureDriversCollection(app, cfg); err != nil {
		return fmt.Errorf("drivers collection: %w", err)
	}
	if err := ensureEventsCollection(app, cfg); err != nil {
		return fmt.Errorf("events collection: %w", err)
	}
	if err := ensureThemesCollection(app, cfg); err != nil {
		return fmt.Errorf("themes collection: %w", err)
	}
	if err := ensureCalibrationResultsCollection(app, cfg); err != nil {
		return fmt.Errorf("calibration_results collection: %w", err)
	}

	log.Println("[QPI] Schema OK")
	return nil
}

// ensureUsersCollection modifies the default users collection to disable Email/Password auth if cfg.DisableEmailPasswordAuth
// and registers any specified OAuth2 providers.
func ensureUsersCollection(app core.App, cfg *config.AppConfig) error {
	log.Printf("Migrating users collection")

	collection, err := app.FindCollectionByNameOrId("users")
	if err != nil {
		// Create users collection if it doesn't exist
		collection = core.NewAuthCollection("users")

		// restrict the rules for record owners
		collection.ListRule = types.Pointer("id = @request.auth.id")
		collection.ViewRule = types.Pointer("id = @request.auth.id")
		collection.UpdateRule = types.Pointer("id = @request.auth.id")
		collection.DeleteRule = types.Pointer("id = @request.auth.id")
	}

	// Ensure extra fields exist (idempotent migration)
	hasQpuSeconds := false
	for _, f := range collection.Fields {
		if f.GetName() == "qpu_seconds" {
			hasQpuSeconds = true
		}
	}
	if !hasQpuSeconds {
		collection.Fields.Add(
			&core.NumberField{
				Name: "qpu_seconds",
				Min:  types.Pointer(0.0),
			},
		)
	}

	// Disable Email/Password authentication if configured
	if cfg.DisableEmailPasswordAuth {
		collection.PasswordAuth.Enabled = false
	} else {
		collection.PasswordAuth.Enabled = true
	}

	// Configure OAuth2 providers if specified
	if len(cfg.OAuth2Providers) > 0 {
		collection.OAuth2.Enabled = true

		for _, providerCfg := range cfg.OAuth2Providers {
			log.Printf("Configuring OAuth2 provider %s for users collection", providerCfg.Name)

			// Idempotent update/append
			found := false
			for i, existing := range collection.OAuth2.Providers {
				if existing.Name == providerCfg.Name {
					collection.OAuth2.Providers[i] = providerCfg
					found = true
					break
				}
			}
			if !found {
				collection.OAuth2.Providers = append(collection.OAuth2.Providers, providerCfg)
			}
		}
	}

	return app.Save(collection)
}

// ensureAPITokensCollection creates the collection storing API tokens with
// user relation, optional expiry, and metadata.
func ensureAPITokensCollection(app core.App, cfg *config.AppConfig) error {
	col, err := initCollection(app, cfg.CollectionAPITokens, &APIToken{})
	if err != nil {
		return err
	}

	// Set API rules: owner-only access
	col.ListRule = types.Pointer("user = @request.auth.id")
	col.ViewRule = types.Pointer("user = @request.auth.id")
	col.CreateRule = types.Pointer("@request.auth.id != \"\" && user = @request.auth.id")
	col.UpdateRule = types.Pointer("user = @request.auth.id")
	col.DeleteRule = types.Pointer("user = @request.auth.id")

	return app.Save(col)
}

// ensureQPUsCollection creates the collection storing QPU hardware properties, statuses, and ports.
func ensureQPUsCollection(app core.App, cfg *config.AppConfig) error {
	col, err := initCollection(app, cfg.CollectionQPUs, &QPU{})
	if err != nil {
		return err
	}

	// Public read, superuser-only CUD
	col.ListRule = types.Pointer("")
	col.ViewRule = types.Pointer("")
	col.CreateRule = nil
	col.UpdateRule = nil
	col.DeleteRule = nil

	return app.Save(col)
}

// ensureTimeSlotsCollection creates/updates the collection storing calendar slot reservations for users.
func ensureTimeSlotsCollection(app core.App, cfg *config.AppConfig) error {
	col, err := initCollection(app, cfg.CollectionTimeSlots, &TimeSlot{})
	if err != nil {
		return err
	}

	// Set API Rules for user-level CRUD and administration
	col.ListRule = types.Pointer("@request.auth.id != \"\"")
	col.ViewRule = types.Pointer("@request.auth.id != \"\"")
	col.CreateRule = types.Pointer("@request.auth.id != \"\" && booked_by = @request.auth.id")
	col.UpdateRule = types.Pointer("booked_by = @request.auth.id")
	col.DeleteRule = types.Pointer("booked_by = @request.auth.id")

	return app.Save(col)
}

// ensureQuantumJobsCollection creates the collection storing jobs pending execution or containing results.
func ensureQuantumJobsCollection(app core.App, cfg *config.AppConfig) error {
	col, err := initCollection(app, cfg.CollectionQuantumJobs, &QuantumJob{})
	if err != nil {
		return err
	}

	// Owner-only read; authenticated create for self; no update/delete for regular users
	col.ListRule = types.Pointer("user_id = @request.auth.id")
	col.ViewRule = types.Pointer("user_id = @request.auth.id")
	col.CreateRule = types.Pointer("@request.auth.id != \"\" && user_id = @request.auth.id")
	col.UpdateRule = nil
	col.DeleteRule = nil

	return app.Save(col)
}

// ensureQPUTimeRequestsCollection creates/updates the collection storing QPU time requests by users.
func ensureQPUTimeRequestsCollection(app core.App, cfg *config.AppConfig) error {
	col, err := initCollection(app, cfg.CollectionQPUTimeRequests, &QPUTimeRequest{})
	if err != nil {
		return err
	}

	// API Authorization Rules
	col.ListRule = types.Pointer("@request.auth.id != \"\" && user = @request.auth.id")
	col.ViewRule = types.Pointer("@request.auth.id != \"\" && user = @request.auth.id")
	col.CreateRule = types.Pointer("@request.auth.id != \"\" && user = @request.auth.id && status = \"pending\"")
	col.UpdateRule = nil // Disallowed for regular users; superusers bypass
	col.DeleteRule = types.Pointer("@request.auth.id != \"\" && user = @request.auth.id && status = \"pending\"")

	return app.Save(col)
}

// ensureNotificationsCollection creates/updates the collection storing admin notifications
// that can target specific users or all users (empty target_users = broadcast).
func ensureNotificationsCollection(app core.App, cfg *config.AppConfig) error {
	col, err := initCollection(app, cfg.CollectionNotifications, &Notification{})
	if err != nil {
		return err
	}

	// Visibility rules:
	// - authenticated users only
	// - target_users empty (broadcast) OR current user is in target_users
	// - within start_time / end_time window (if set)
	// - not dismissed by current user
	visibilityRule := "@request.auth.id != \"\" && " +
		"(@request.auth.id ?= target_users.id || target_users:length = 0) && " +
		"(start_time = '' || start_time <= @now) && " +
		"(end_time = '' || end_time >= @now) && " +
		"(dismissed_by:length = 0 || dismissed_by.id ?!= @request.auth.id)"

	col.ListRule = types.Pointer(visibilityRule)
	col.ViewRule = types.Pointer(visibilityRule)
	// nil = disabled for regular users; superusers bypass API rules
	col.CreateRule = nil
	col.UpdateRule = nil
	col.DeleteRule = nil

	return app.Save(col)
}

// ensureDriversCollection creates the `drivers` collection (required `qpu`
// relation + `kind` / `language`) and its rules (admin-only manage, read-only
// list for authenticated users) if it does not exist (RFC 0001 §3).
//
// QPU collections/rules are untouched.
func ensureDriversCollection(app core.App, cfg *config.AppConfig) error {
	col, err := initCollection(app, cfg.CollectionDrivers, &Driver{})
	if err != nil {
		return err
	}

	// Public read, superuser-only CUD — mirrors qpus (RFC 0001 §9).
	col.ListRule = types.Pointer("")
	col.ViewRule = types.Pointer("")
	col.CreateRule = nil
	col.UpdateRule = nil
	col.DeleteRule = nil

	return app.Save(col)
}

// ensureEventsCollection creates the collection storing the `events` trace
// log — every driver→UI event a handler chooses to persist, e.g. a cryostat
// monitor's readings (RFC 0001 §7, Phase 3). Only reached when
// EnableDriverFramework is on.
func ensureEventsCollection(app core.App, cfg *config.AppConfig) error {
	col, err := initCollection(app, cfg.CollectionEvents, &Event{})
	if err != nil {
		return err
	}

	// Public read, superuser-only CUD — mirrors drivers/qpus (RFC 0001 §9).
	// The server itself writes rows through the admin app instance, which
	// bypasses these API rules entirely.
	col.ListRule = types.Pointer("")
	col.ViewRule = types.Pointer("")
	col.CreateRule = nil
	col.UpdateRule = nil
	col.DeleteRule = nil

	// A composite index on (type, ts) keeps the dashboard's per-type,
	// time-ordered reads and the retention prune fast as the log grows
	// (RFC 0001 §7, §11; Phase 5).
	indexName := fmt.Sprintf("idx_%s_type_ts", cfg.CollectionEvents)
	if !hasIndex(col, indexName) {
		col.Indexes = append(col.Indexes, fmt.Sprintf(
			"CREATE INDEX `%s` ON `%s` (`type`, `ts`)", indexName, cfg.CollectionEvents))
	}

	return app.Save(col)
}

// ensureThemesCollection creates the `themes` collection storing dashboard custom
// design tokens, branding, and optional custom CSS/JS (RFC 0002 §3.2, §3.4).
func ensureThemesCollection(app core.App, cfg *config.AppConfig) error {
	colName := cfg.GetCollectionName(config.DefaultThemesCollection)
	col, err := initCollection(app, colName, &config.ThemeSchema{})
	if err != nil {
		return err
	}

	// Public read (dashboard needs theme before login), superuser-only CUD (RFC 0002 §3.4).
	col.ListRule = types.Pointer("")
	col.ViewRule = types.Pointer("")
	col.CreateRule = nil
	col.UpdateRule = nil
	col.DeleteRule = nil

	return app.Save(col)
}

// ensureCalibrationResultsCollection creates the `calibration_results` collection storing
// calibration results returned by a calibration tuner.
func ensureCalibrationResultsCollection(app core.App, cfg *config.AppConfig) error {
	col, err := initCollection(app, cfg.CollectionCalibrationResults, &CalibrationResult{})
	if err != nil {
		return err
	}

	// Public read (maybe auth only?), superuser-only CUD (written by server)
	col.ListRule = types.Pointer("@request.auth.id != \"\"")
	col.ViewRule = types.Pointer("@request.auth.id != \"\"")
	col.CreateRule = nil
	col.UpdateRule = nil
	col.DeleteRule = nil

	return app.Save(col)
}

// hasIndex reports whether the collection already declares an index with the
// given name, keeping the migration idempotent across restarts.
func hasIndex(col *core.Collection, name string) bool {
	for _, idx := range col.Indexes {
		if strings.Contains(idx, name) {
			return true
		}
	}
	return false
}

// initCollection initializes a collection given a model
func initCollection(app core.App, name string, model interface{}) (*core.Collection, error) {
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return nil, err
	}

	col, err := app.FindCollectionByNameOrId(name)
	if err != nil {
		col = core.NewBaseCollection(name)
	}

	existingFields := make(map[string]bool)
	for _, f := range col.Fields {
		existingFields[f.GetName()] = true
	}

	t := reflect.TypeOf(model)
	if t.Kind() == reflect.Ptr {
		t = t.Elem()
	}
	for field := range t.Fields() {
		fieldName := field.Tag.Get("db")
		if fieldName == "" || field.Name == "ID" {
			continue
		}

		if !existingFields[fieldName] {
			fieldType := field.Tag.Get("type")
			if fieldType == "" {
				fieldType = field.Type.String()
			}

			switch fieldType {
			case "text", stringTypeStr:
				col.Fields.Add(&core.TextField{
					Name:                fieldName,
					System:              field.Tag.Get("system") == "true",
					Hidden:              field.Tag.Get("hidden") == "true",
					Presentable:         field.Tag.Get("presentable") == "true",
					Help:                field.Tag.Get("help"),
					Min:                 tryParseInt(field.Tag.Get("min"), 0),
					Max:                 tryParseInt(field.Tag.Get("max"), 0),
					Pattern:             field.Tag.Get("pattern"),
					AutogeneratePattern: field.Tag.Get("autogeneratePattern"),
					Required:            field.Tag.Get("required") == "true",
					// PrimaryKey:          field.Tag.Get("primaryKey") == "true",
				})
				if field.Tag.Get("primaryKey") == "true" {
					col.Id = fieldName
				}
			case "number", intTypeStr, int64TypeStr, int32TypeStr, uintTypeStr, uint64TypeStr, uint32TypeStr, float64TypeStr, float32TypeStr:
				col.Fields.Add(&core.NumberField{
					Name:        fieldName,
					System:      field.Tag.Get("system") == "true",
					Hidden:      field.Tag.Get("hidden") == "true",
					Presentable: field.Tag.Get("presentable") == "true",
					Help:        field.Tag.Get("help"),
					Min:         tryParseFloat64Ptr(field.Tag.Get("min"), nil),
					Max:         tryParseFloat64Ptr(field.Tag.Get("max"), nil),
					OnlyInt:     field.Tag.Get("onlyInt") == "true",
					Required:    field.Tag.Get("required") == "true",
				})
			case "bool", boolTypeStr:
				col.Fields.Add(&core.BoolField{Name: fieldName,
					System:      field.Tag.Get("system") == "true",
					Hidden:      field.Tag.Get("hidden") == "true",
					Presentable: field.Tag.Get("presentable") == "true",
					Help:        field.Tag.Get("help"),
					Required:    field.Tag.Get("required") == "true",
				})
			case "date":
				col.Fields.Add(&core.DateField{Name: fieldName,
					System:      field.Tag.Get("system") == "true",
					Hidden:      field.Tag.Get("hidden") == "true",
					Presentable: field.Tag.Get("presentable") == "true",
					Help:        field.Tag.Get("help"),
					Min:         tryParseDateTime(field.Tag.Get("min"), types.DateTime{}),
					Max:         tryParseDateTime(field.Tag.Get("max"), types.DateTime{}),
					Required:    field.Tag.Get("required") == "true",
				})
			case "autodate":
				col.Fields.Add(&core.AutodateField{Name: fieldName,
					System:      field.Tag.Get("system") == "true",
					Hidden:      field.Tag.Get("hidden") == "true",
					Presentable: field.Tag.Get("presentable") == "true",
					OnCreate:    field.Tag.Get("onCreate") == "true",
					OnUpdate:    field.Tag.Get("onUpdate") == "true",
				})
			case "json":
				col.Fields.Add(&core.JSONField{Name: fieldName,
					System:      field.Tag.Get("system") == "true",
					Hidden:      field.Tag.Get("hidden") == "true",
					Presentable: field.Tag.Get("presentable") == "true",
					Help:        field.Tag.Get("help"),
					MaxSize:     tryParseInt64(field.Tag.Get("maxSize"), 0),
					Required:    field.Tag.Get("required") == "true",
				})
			case "relation":
				col.Fields.Add(&core.RelationField{Name: fieldName,
					System:        field.Tag.Get("system") == "true",
					Hidden:        field.Tag.Get("hidden") == "true",
					Presentable:   field.Tag.Get("presentable") == "true",
					Help:          field.Tag.Get("help"),
					Required:      field.Tag.Get("required") == "true",
					CollectionId:  tryGetCollectionId(app, cfg, field.Tag.Get("collection")),
					MaxSelect:     tryParseInt(field.Tag.Get("maxSelect"), 0),
					MinSelect:     tryParseInt(field.Tag.Get("minSelect"), 0),
					CascadeDelete: field.Tag.Get("cascadeDelete") == "true",
				})
			case "file":
				col.Fields.Add(&core.FileField{Name: fieldName,
					System:      field.Tag.Get("system") == "true",
					Hidden:      field.Tag.Get("hidden") == "true",
					Presentable: field.Tag.Get("presentable") == "true",
					Help:        field.Tag.Get("help"),
					Required:    field.Tag.Get("required") == "true",
					MaxSelect:   tryParseInt(field.Tag.Get("maxSelect"), 0),
					MaxSize:     tryParseInt64(field.Tag.Get("maxSize"), 0),
					Protected:   field.Tag.Get("protected") == "true",
					MimeTypes:   tryParseStrSlice(field.Tag.Get("mimeTypes")),
					Thumbs:      tryParseStrSlice(field.Tag.Get("thumbs")),
				})
			case "select":
				col.Fields.Add(&core.SelectField{Name: fieldName,
					System:      field.Tag.Get("system") == "true",
					Hidden:      field.Tag.Get("hidden") == "true",
					Presentable: field.Tag.Get("presentable") == "true",
					Help:        field.Tag.Get("help"),
					Required:    field.Tag.Get("required") == "true",
					MaxSelect:   tryParseInt(field.Tag.Get("maxSelect"), 0),
					Values:      tryParseStrSlice(field.Tag.Get("values")),
				})
			default:
				return nil, fmt.Errorf("unknown field type: %s\n", fieldType)
			}
		}
	}

	return col, nil
}

// tryParseInt64 converts a string to an int64, defaulting to defaultValue if the string is not a valid int64
func tryParseInt64(v string, defaultValue int64) int64 {
	value, err := strconv.ParseInt(v, 10, 64)
	if err != nil {
		return defaultValue
	}
	return value
}

// tryParseInt converts a string to an int, defaulting to defaultValue if the string is not a valid int
func tryParseInt(v string, defaultValue int) int {
	value, err := strconv.Atoi(v)
	if err != nil {
		return defaultValue
	}
	return value
}

// tryParseFloat64 converts a string to a float64, defaulting to defaultValue if the string is not a valid float
func tryParseFloat64(v string, defaultValue float64) float64 {
	value, err := strconv.ParseFloat(v, 64)
	if err != nil {
		return defaultValue
	}
	return value
}

// tryParseFloat64Ptr converts a string to a float64, defaulting to defaultValue if the string is not a valid float
func tryParseFloat64Ptr(v string, defaultValue *float64) *float64 {
	value, err := strconv.ParseFloat(v, 64)
	if err != nil {
		return defaultValue
	}
	return &value
}

// tryParseDateTime converts a string to a DateTime, defaulting to defaultValue if the string is not a valid DateTime
func tryParseDateTime(v string, defaultValue types.DateTime) types.DateTime {
	value, err := types.ParseDateTime(v)
	if err != nil {
		return defaultValue
	}
	return value
}

// tryParseStrSlice converts a comma-separated string to a slice of strings, defaulting to []string{}
func tryParseStrSlice(v string) []string {
	parts := strings.Split(v, ",")
	result := make([]string, 0, len(parts))

	for _, part := range parts {
		trimmed := strings.TrimSpace(part)
		if trimmed != "" {
			result = append(result, trimmed)
		}
	}

	return result
}

// tryGetCollectionId gets a collection's ID, defaulting to "" if the string is not a valid collection
func tryGetCollectionId(app core.App, cfg *config.AppConfig, v string) string {
	name := cfg.GetCollectionName(v)
	collection, err := app.FindCollectionByNameOrId(name)
	if err != nil {
		return ""
	}
	return collection.Id
}
