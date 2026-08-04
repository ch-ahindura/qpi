// Package api manages HTTP REST endpoints and low-level NNG message channels
// for registering QPUs, dispatching jobs, and listening for job execution results.
package api

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"io/fs"
	"log"
	"net/http"
	"os"
	"sync"
	"time"

	"github.com/pocketbase/dbx"
	"github.com/pocketbase/pocketbase/apis"
	"github.com/pocketbase/pocketbase/core"

	"qpi/internal/config"
	"qpi/internal/db"
	"qpi/internal/drivers"
)

var (
	// activeQPUs stores active cancel functions for goroutines bound to registered QPUs.
	activeQPUs   = make(map[string]context.CancelFunc)
	activeQPUsMu sync.Mutex
)

// Version holds the version of the application, injected from main.go
var Version string

// SetupServer initializes the server with a few important configs
func SetupServer(e *core.ServeEvent) error {
	cfg, err := config.GetConfigFromApp(e.App)
	if err != nil {
		return err
	}

	if e.Server.TLSConfig == nil {
		e.Server.TLSConfig = &tls.Config{}
	}

	// Inject our manager's thread-safe handshake callback
	e.Server.TLSConfig.GetCertificate = cfg.GetCertificate
	e.Server.TLSConfig.MinVersion = tls.VersionTLS12

	// set the port
	e.Server.Addr = fmt.Sprintf(":%d", cfg.ServerPort)

	return nil
}

// RegisterRoutes sets up custom HTTP routes for QPU interactions.
func RegisterRoutes(e *core.ServeEvent, dashboardFS fs.FS) {
	// Dashboard
	if dashboardFS == nil {
		log.Panic("[RegisterRoutes] failed to load the dashboard file system")
	}

	subFS, err := fs.Sub(dashboardFS, "internal/dashboard/dist")
	if err != nil {
		log.Panicf("[RegisterRoutes] failed to sub dashboardFS: %v", err)
	}
	e.Router.GET("/{path...}", apis.Static(subFS, true))

	// general public data
	e.Router.GET("/api/pub/root-ca.pem", handleRootCaDownload)

	// ops routes
	e.Router.GET("/api/op/version", handleOpVersion)
	e.Router.POST("/api/op/qpus/create", handleQPUCreate)
	e.Router.POST("/api/op/qpu/toggle", handleQPUToggle)

	// Driver routes
	e.Router.POST("/api/op/drivers/create", handleDriverCreate)
	e.Router.POST("/api/op/drivers/connect", handleDriverConnect)
	e.Router.POST("/api/op/drivers/toggle", handleDriverToggle)
	e.Router.POST("/api/op/calibrate/dispatch", handleCalibrateDispatch)

	// Job CRUD routes
	e.Router.POST("/api/jobs", handleJobSubmit)
	e.Router.GET("/api/jobs", handleJobList)
	e.Router.GET("/api/jobs/{id}", handleJobGet)
	e.Router.POST("/api/jobs/{id}/cancel", handleJobCancel)

	// QPU discovery routes (public — no auth required)
	e.Router.GET("/api/qpus", handleQPUList)
	e.Router.GET("/api/qpus/{name}", handleQPUGet)

	// API token CRUD routes (owner-only)
	e.Router.POST("/api/tokens", handleTokenCreate)
	e.Router.GET("/api/tokens", handleTokenList)
	e.Router.GET("/api/tokens/{id}", handleTokenGet)
	e.Router.PATCH("/api/tokens/{id}", handleTokenUpdate)
	e.Router.DELETE("/api/tokens/{id}", handleTokenDelete)

	// Notification dismiss route (authenticated users only)
	e.Router.POST("/api/notifications/{id}/dismiss", handleNotificationDismiss)

	// Theme endpoints (public — dashboard needs these before login)
	e.Router.GET("/api/theme/active", handleThemeActive)
	e.Router.GET("/api/theme/defaults", handleThemeDefaults)
	e.Router.GET("/api/theme/css", handleThemeCSS)
	e.Router.GET("/api/theme/js", handleThemeJS)
}

// handleJobSubmit handles POST /api/jobs — creates a new quantum job.
func handleJobSubmit(re *core.RequestEvent) error {
	cfg, err := config.GetConfigFromApp(re.App)
	if err != nil {
		return re.Error(http.StatusInternalServerError, "failed to retrieve configuration", err)
	}

	user, err := getCurrentUser(re)
	if err != nil {
		return err
	}

	// Check QPU seconds balance
	if user.QPUSeconds <= 0 {
		return re.Error(http.StatusForbidden, "insufficient QPU seconds balance", nil)
	}

	// Parse and validate request body
	var req JobSubmitRequest
	err = parseBody(cfg, re, &req)
	if err != nil {
		return err
	}

	job := db.QuantumJob{
		UserID:    user.ID,
		QPUTarget: req.QPUTarget,
		Payload:   req,
		Status:    "pending",
		Duration:  0,
	}

	err = saveToDb(re.App, &job)
	if err != nil {
		return re.Error(http.StatusInternalServerError, "failed to save job", err)
	}

	return re.JSON(http.StatusCreated, &job)
}

// handleJobList handles GET /api/jobs — lists jobs for the authenticated user.
func handleJobList(re *core.RequestEvent) error {
	cfg, err := config.GetConfigFromApp(re.App)
	if err != nil {
		return re.Error(http.StatusInternalServerError, "failed to retrieve configuration", err)
	}

	user, err := getCurrentUser(re)
	if err != nil {
		return re.Error(http.StatusUnauthorized, "authentication required", err)
	}

	sort, skip, limit := getPaginationParams(re, "-created")
	var jobs []db.QuantumJob
	err = db.FindMany(
		re.App,
		cfg.CollectionQuantumJobs,
		&jobs,
		"user_id = {:userId}",
		sort,
		limit,
		skip,
		dbx.Params{"userId": user.ID},
	)
	if err != nil {
		return re.Error(http.StatusInternalServerError, "failed to query jobs", err)
	}

	return re.JSON(http.StatusOK, jobs)
}

// handleJobGet handles GET /api/jobs/{id} — retrieves a single job by ID.
func handleJobGet(re *core.RequestEvent) error {
	cfg, err := config.GetConfigFromApp(re.App)
	if err != nil {
		return re.Error(http.StatusInternalServerError, "failed to retrieve configuration", err)
	}

	user, err := getCurrentUser(re)
	if err != nil {
		return re.Error(http.StatusUnauthorized, "authentication required", err)
	}

	jobID := re.Request.PathValue("id")
	extraFilters := db.MapFilter(map[string]any{"user_id": user.ID})
	var job db.QuantumJob
	err = db.FindOne(re.App, cfg.CollectionQuantumJobs, jobID, &job, extraFilters)
	if err != nil {
		return re.Error(http.StatusNotFound, "job not found", err)
	}

	return re.JSON(http.StatusOK, &job)
}

// handleJobCancel handles POST /api/jobs/{id}/cancel — cancels a pending or running job.
func handleJobCancel(re *core.RequestEvent) error {
	cfg, err := config.GetConfigFromApp(re.App)
	if err != nil {
		return re.Error(http.StatusInternalServerError, "failed to retrieve configuration", err)
	}

	user, err := getCurrentUser(re)
	if err != nil {
		return re.Error(http.StatusUnauthorized, "authentication required", err)
	}

	jobID := re.Request.PathValue("id")
	extraFilters := func(q *dbx.SelectQuery) error {
		q.AndWhere(dbx.HashExp{"user_id": user.ID})
		q.AndWhere(dbx.In("status", "running", "pending"))
		return nil
	}
	updateData := map[string]any{"status": "cancelled"}
	var job db.QuantumJob
	err = db.FindAndUpdateOne(re.App, cfg.CollectionQuantumJobs, jobID, &job, updateData, extraFilters)
	if err != nil {
		if errors.Is(err, db.ErrNotFound) {
			return re.Error(http.StatusNotFound, "job not found, maybe it was already completed or cancelled?", err)
		}
		return re.Error(http.StatusInternalServerError, "failed to cancel job", err)
	}

	return re.JSON(http.StatusOK, map[string]string{"status": "cancelled"})
}

// handleTokenCreate handles POST /api/tokens — creates a new API token for the authenticated user.
// Returns the raw token exactly once; only the hash is stored in the database.
func handleTokenCreate(re *core.RequestEvent) error {
	user, err := getCurrentUser(re)
	if err != nil {
		return re.Error(http.StatusUnauthorized, "authentication required", err)
	}

	var req TokenCreateRequest
	if err := re.BindBody(&req); err != nil {
		return re.Error(http.StatusBadRequest, "invalid request body", err)
	}

	rawToken := generateAPIToken()
	tokenRecord := db.APIToken{
		Token:     db.HashToken(rawToken),
		User:      user.ID,
		ExpiresAt: req.ExpiresAt,
		Name:      req.Name,
	}

	if err := saveToDb(re.App, &tokenRecord); err != nil {
		return re.Error(http.StatusInternalServerError, "failed to create token", err)
	}

	var resp TokenCreateResponse
	err = resp.RefreshFromDbModel(&tokenRecord)
	resp.Token = rawToken
	if err != nil {
		return re.Error(http.StatusInternalServerError, "failed to refresh token", err)
	}

	return re.JSON(http.StatusCreated, resp)
}

// handleTokenList handles GET /api/tokens — lists metadata for tokens belonging to the authenticated user.
// Never exposes the raw token hash.
func handleTokenList(re *core.RequestEvent) error {
	cfg, err := config.GetConfigFromApp(re.App)
	if err != nil {
		return re.Error(http.StatusInternalServerError, "failed to retrieve configuration", err)
	}

	user, err := getCurrentUser(re)
	if err != nil {
		return re.Error(http.StatusUnauthorized, "authentication required", err)
	}

	sort, skip, limit := getPaginationParams(re, "-created")
	var records []db.APIToken
	err = db.FindMany(re.App, cfg.CollectionAPITokens, &records, "user = {:userId}", sort, limit, skip, dbx.Params{"userId": user.ID})
	if err != nil {
		return re.Error(http.StatusInternalServerError, "failed to query tokens", err)
	}

	tokens := make([]TokenCreateResponse, len(records))
	for i := range records {
		err = (&tokens[i]).RefreshFromDbModel(&records[i])
		if err != nil {
			return re.Error(http.StatusInternalServerError, "failed to refresh token response from db", err)
		}
	}
	return re.JSON(http.StatusOK, tokens)
}

// handleTokenGet handles GET /api/tokens/{id} — retrieves metadata for a single token.
// Only the owner (or a superuser) may access it.
func handleTokenGet(re *core.RequestEvent) error {
	cfg, err := config.GetConfigFromApp(re.App)
	if err != nil {
		return re.Error(http.StatusInternalServerError, "failed to retrieve configuration", err)
	}

	user, err := getCurrentUser(re)
	if err != nil {
		return re.Error(http.StatusUnauthorized, "authentication required", err)
	}

	tokenID := re.Request.PathValue("id")
	ownOrAdminFilter := func(q *dbx.SelectQuery) error {
		if !re.HasSuperuserAuth() {
			q.AndWhere(dbx.HashExp{"user": user.ID})
		}
		return nil
	}
	var token db.APIToken
	err = db.FindOne(re.App, cfg.CollectionAPITokens, tokenID, &token, ownOrAdminFilter)
	if err != nil {
		return re.Error(http.StatusNotFound, "token not found", err)
	}

	var resp TokenCreateResponse
	err = resp.RefreshFromDbModel(&token)
	if err != nil {
		return re.Error(http.StatusInternalServerError, "failed to refresh token response from db", err)
	}

	return re.JSON(http.StatusOK, resp)
}

// handleTokenUpdate handles PATCH /api/tokens/{id} — updates name or expiry for a token.
// Only the owner (or a superuser) may update it.
func handleTokenUpdate(re *core.RequestEvent) error {
	cfg, err := config.GetConfigFromApp(re.App)
	if err != nil {
		return re.Error(http.StatusInternalServerError, "failed to retrieve configuration", err)
	}

	user, err := getCurrentUser(re)
	if err != nil {
		return re.Error(http.StatusUnauthorized, "authentication required", err)
	}

	tokenID := re.Request.PathValue("id")
	var req TokenUpdateRequest
	if err := parseBody(cfg, re, &req); err != nil {
		return re.Error(http.StatusBadRequest, "invalid request body", err)
	}
	updateData := req.ToMap()

	ownOrAdminFilter := func(q *dbx.SelectQuery) error {
		if !re.HasSuperuserAuth() {
			q.AndWhere(dbx.HashExp{"user": user.ID})
		}
		return nil
	}
	var token db.APIToken
	err = db.FindAndUpdateOne(re.App, cfg.CollectionAPITokens, tokenID, &token, updateData, ownOrAdminFilter)
	if err != nil {
		return re.Error(http.StatusNotFound, "token not found", err)
	}

	var resp TokenCreateResponse
	if err := resp.RefreshFromDbModel(&token); err != nil {
		return re.Error(http.StatusInternalServerError, "failed to refresh token response from db", err)
	}

	return re.JSON(http.StatusOK, resp)
}

// handleNotificationDismiss handles POST /api/notifications/{id}/dismiss —
// adds the authenticated user to the notification's dismissed_by relation.
func handleNotificationDismiss(re *core.RequestEvent) error {
	cfg, err := config.GetConfigFromApp(re.App)
	if err != nil {
		return re.Error(http.StatusInternalServerError, "failed to retrieve configuration", err)
	}

	user, err := getCurrentUser(re)
	if err != nil {
		return re.Error(http.StatusUnauthorized, "authentication required", err)
	}

	notifID := re.Request.PathValue("id")
	var notif db.Notification
	err = db.FindOne(re.App, cfg.CollectionNotifications, notifID, &notif)
	if err != nil {
		return re.Error(http.StatusNotFound, "notification not found", err)
	}

	// Verify the user is allowed to see this notification (target_users empty or includes user)
	if len(notif.TargetUsers) > 0 {
		found := false
		for _, uid := range notif.TargetUsers {
			if uid == user.ID {
				found = true
				break
			}
		}
		if !found {
			return re.Error(http.StatusForbidden, "access denied", nil)
		}
	}

	// Add user to dismissed_by if not already present
	alreadyDismissed := false
	for _, uid := range notif.DismissedBy {
		if uid == user.ID {
			alreadyDismissed = true
			break
		}
	}
	if !alreadyDismissed {
		notif.DismissedBy = append(notif.DismissedBy, user.ID)
		if err := saveToDb(re.App, &notif); err != nil {
			return re.Error(http.StatusInternalServerError, "failed to dismiss notification", err)
		}
	}

	return re.JSON(http.StatusOK, map[string]string{"status": "dismissed"})
}

// handleTokenDelete handles DELETE /api/tokens/{id} — removes a token.
// Only the owner (or a superuser) may delete it.
func handleTokenDelete(re *core.RequestEvent) error {
	cfg, err := config.GetConfigFromApp(re.App)
	if err != nil {
		return re.Error(http.StatusInternalServerError, "failed to retrieve configuration", err)
	}

	user, err := getCurrentUser(re)
	if err != nil {
		return re.Error(http.StatusUnauthorized, "authentication required", err)
	}

	tokenID := re.Request.PathValue("id")
	ownOrAdminFilter := func(q *dbx.SelectQuery) error {
		if !re.HasSuperuserAuth() {
			q.AndWhere(dbx.HashExp{"user": user.ID})
		}
		return nil
	}
	var token db.APIToken
	err = db.FindAndDeleteOne(re.App, cfg.CollectionAPITokens, tokenID, &token, ownOrAdminFilter)
	if err != nil {
		if errors.Is(err, db.ErrNotFound) {
			return re.Error(http.StatusNotFound, "token not found", err)
		}
		return re.Error(http.StatusInternalServerError, "failed to delete token", err)
	}

	return re.JSON(http.StatusOK, map[string]string{"status": "deleted"})
}

// handleOpVersion handles GET /api/op/version — returns the current
// application version, plus whether the driver framework (RFC 0001) is
// enabled so the dashboard can gate the Drivers page without probing a
// drivers/* route directly.
func handleOpVersion(re *core.RequestEvent) error {
	if !re.HasSuperuserAuth() {
		return re.Error(http.StatusForbidden, "admin access required", nil)
	}

	return re.JSON(http.StatusOK, map[string]any{
		"version": Version,
	})
}

// handleQPUCreate creates a new QPU record (admin-only), generating a random
// access token and returning it in the response.  The hashed token is stored.
// POST /api/op/qpus/create
func handleQPUCreate(re *core.RequestEvent) error {
	cfg, err := config.GetConfigFromApp(re.App)
	if err != nil {
		return re.Error(http.StatusInternalServerError, "failed to retrieve configuration", err)
	}

	if !re.HasSuperuserAuth() {
		return re.Error(http.StatusForbidden, "admin access required", nil)
	}

	var req QPUCreateRequest
	if err := parseBody(cfg, re, &req); err != nil {
		return err
	}

	enabled := true
	if req.Enabled != nil {
		enabled = *req.Enabled
	}

	qpu := db.QPU{
		Name:      req.Name,
		Status:    "offline",
		NumQubits: req.NumQubits,
		Enabled:   enabled,
	}

	if err := saveToDb(re.App, &qpu); err != nil {
		return re.Error(http.StatusInternalServerError, "failed to create QPU", err)
	}

	resp := QPUCreateResponse{}
	_ = resp.RefreshFromDbModel(&qpu)

	return re.JSON(http.StatusCreated, resp)
}

// handleRootCaDownload handles requests to download the root CA of the server
func handleRootCaDownload(re *core.RequestEvent) error {
	cfg, err := config.GetConfigFromApp(re.App)
	if err != nil {
		return re.InternalServerError("failed to retrieve configuration", err)
	}

	data, err := os.ReadFile(cfg.TlsCaCertFile)
	if err != nil {
		return re.NotFoundError("Root CA certificate file not found on server", err)
	}

	return re.Blob(200, "application/x-pem-file", data)
}

// handleQPUToggle handles POST /api/op/qpu/toggle — toggles a QPU's enabled status (admin-only).
func handleQPUToggle(re *core.RequestEvent) error {
	cfg, err := config.GetConfigFromApp(re.App)
	if err != nil {
		return re.Error(http.StatusInternalServerError, "failed to retrieve configuration", err)
	}

	// Only superusers (admins) can access this endpoint
	if !re.HasSuperuserAuth() {
		return re.Error(http.StatusForbidden, "admin access required", nil)
	}

	var req QPUToggleRequest
	if err := parseBody(cfg, re, &req); err != nil {
		return err
	}

	updateData := map[string]any{"enabled": req.Enabled}
	var qpu db.QPU
	err = db.FindAndUpdateOne(re.App, cfg.CollectionQPUs, req.ID, &qpu, updateData)
	if err != nil {
		if errors.Is(err, db.ErrNotFound) {
			return re.Error(http.StatusNotFound, "QPU not found", err)
		}
		return re.Error(http.StatusInternalServerError, "failed to update QPU status", err)
	}

	var resp QPUToggleResponse
	_ = resp.RefreshFromDbModel(&qpu)
	return re.JSON(http.StatusOK, resp)
}

// handleDriverCreate creates a new driver record (admin-only), generating a
// random token and returning it, plus the kind×language setup snippets, once
// (RFC 0001 §3). The hashed token is stored. POST /api/op/drivers/create
func handleDriverCreate(re *core.RequestEvent) error {
	cfg, err := config.GetConfigFromApp(re.App)
	if err != nil {
		return re.Error(http.StatusInternalServerError, "failed to retrieve configuration", err)
	}

	if !re.HasSuperuserAuth() {
		return re.Error(http.StatusForbidden, "admin access required", nil)
	}

	var req DriverCreateRequest
	if err := parseBody(cfg, re, &req); err != nil {
		return err
	}

	kind := drivers.Kind(req.Kind)
	language := drivers.Language(req.Language)
	if !drivers.Default.KnownKind(kind) {
		return re.Error(http.StatusBadRequest, fmt.Sprintf("unknown kind %q", req.Kind), nil)
	}
	if !drivers.KnownLanguage(language) {
		return re.Error(http.StatusBadRequest, fmt.Sprintf("unknown language %q", req.Language), nil)
	}
	// Which devices exist is the SDK's business and it differs between them: only
	// the Python SDK has a process device. Registering, say, kind=mock with
	// language=go used to succeed and hand the operator setup commands for a
	// `--device mock` the Go binary has never heard of.
	if !drivers.Default.ShipsIn(kind, language) {
		return re.Error(http.StatusBadRequest, fmt.Sprintf(
			"the %s SDK ships no %q device; it ships %s. Write it yourself against the SDK with kind=custom, or use a language whose SDK has it",
			req.Language, req.Kind, joinKinds(drivers.Default.KindsIn(language))), nil)
	}

	events := drivers.Default.Events(kind)
	if kind == drivers.Custom {
		events = req.Events
	}
	if len(events) == 0 {
		return re.Error(http.StatusBadRequest, "events is required for a custom driver", nil)
	}
	for _, e := range events {
		if !isKnownEventType(EventType(e)) {
			return re.Error(http.StatusBadRequest, fmt.Sprintf("unknown event type %q", e), nil)
		}
	}

	// Generate a random token (raw token is returned once, hash is stored by db hook)
	rawToken := generateAPIToken()

	enabled := true
	if req.Enabled != nil {
		enabled = *req.Enabled
	}

	driver := db.Driver{
		Name:     req.Name,
		QPU:      req.QPU,
		Kind:     string(kind),
		Language: string(language),
		Events:   events,
		Token:    rawToken,
		Status:   "offline",
		Enabled:  enabled,
	}

	if err := saveToDb(re.App, &driver); err != nil {
		return re.Error(http.StatusInternalServerError, "failed to create driver", err)
	}

	resp := DriverCreateResponse{
		CaFingerprint: cfg.GetTlsCaHash(),
		DriverVersion: Version,
	}
	_ = resp.RefreshFromDbModel(&driver)
	resp.Token = rawToken
	resp.QpiAddr = getAddrFromReq(re)
	resp.Snippets = drivers.Default.Snippets(kind, language, drivers.Params{
		Name:          driver.Name,
		Token:         rawToken,
		QpiAddr:       resp.QpiAddr,
		CaFingerprint: resp.CaFingerprint,
	})

	return re.JSON(http.StatusCreated, resp)
}

// connectedPeer returns another driver already connected to the same QPU for the
// same operation, with the operation they share, or "" for neither.
//
// One chip takes one driver of each role. Two QPU drivers would hand the same
// hardware two schedules, and two tuners would sweep the same qubits and each
// write the device YAML (RFC 0004 §6.8). Registering a second is allowed — a
// standby, or a replacement prepared before the running one is retired — but
// connecting it while the first is live is not.
//
// A driver counts as connected only when its socket is attached *and* this server
// is dispatching to it, so neither a stale `online` left by a crash nor a restart
// of the server can wedge a QPU no driver is actually on.
func connectedPeer(app core.App, cfg *config.AppConfig, driver *db.Driver) (string, drivers.Operation) {
	operation := drivers.Default.OperationOf(drivers.Kind(driver.Kind))
	if operation == "" {
		// A custom driver's operation is whatever its author wrote, so the server
		// has no grounds to say two of them are the same role.
		return "", ""
	}

	peers, err := app.FindRecordsByFilter(
		cfg.CollectionDrivers,
		"qpu = {:qpu} && status = 'online' && enabled = true && id != {:self}",
		"+created", 0, 0,
		dbx.Params{"qpu": driver.QPU, "self": driver.ID},
	)
	if err != nil {
		return "", ""
	}

	for _, peer := range peers {
		if drivers.Default.OperationOf(drivers.Kind(peer.GetString("kind"))) != operation {
			continue
		}
		if !isDispatching(peer.Id) {
			continue
		}
		return peer.GetString("name"), operation
	}
	return "", ""
}

// handleDriverConnect connects a driver process, allocating dynamic in/out
// NNG ports the same way handleQPUConnect does for QPUs (RFC 0001 §8).
// POST /api/op/drivers/connect
func handleDriverConnect(re *core.RequestEvent) error {
	cfg, err := config.GetConfigFromApp(re.App)
	if err != nil {
		return re.Error(http.StatusInternalServerError, "failed to retrieve custom configuration", err)
	}

	var req DriverConnectRequest
	if err := parseBody(cfg, re, &req); err != nil {
		return err
	}

	// Find driver by token (hashed) — the token identifies the driver and,
	// transitively, its QPU (RFC 0001 §8).
	hashedToken := db.HashToken(req.AccessToken)
	var driver db.Driver
	err = db.FindOneByFilter(re.App, cfg.CollectionDrivers, &driver, "token = {:token}", dbx.Params{"token": hashedToken})
	if err != nil {
		return re.Error(http.StatusUnauthorized, "invalid access token", err)
	}

	if !driver.Enabled {
		return re.Error(http.StatusForbidden, "driver is currently disabled by administrator", nil)
	}

	if peer, operation := connectedPeer(re.App, cfg, &driver); peer != "" {
		return re.Error(http.StatusConflict, fmt.Sprintf(
			"%q is already connected as this QPU's %s driver; one per QPU", peer, operation), nil)
	}

	// The driver does not get to rename itself. Name is a display label an admin
	// typed in the dashboard; writing it from the connect body meant every restart
	// of a driver launched with a --name silently overwrote it, and nothing looks a
	// driver up by name anyway — the token is the identity.
	if req.Host != "" {
		driver.Host = req.Host
	}
	if req.Version != "" {
		driver.Version = req.Version
	}

	// Allocate in/out ports if not already done
	if driver.NNGInPort == 0 || driver.NNGOutPort == 0 {
		ports, err := findFreePorts(re.App, 2)
		if err != nil {
			return re.Error(http.StatusInternalServerError, "cannot allocate NNG ports", err)
		}
		driver.NNGInPort = ports[0]
		driver.NNGOutPort = ports[1]
	}

	driver.LastSeen = time.Now().UTC().Format("2006-01-02T15:04:05.000Z")

	if err := saveToDb(re.App, &driver); err != nil {
		return re.Error(http.StatusInternalServerError, "cannot save driver record", err)
	}

	// Generate auth token for the driver record
	record, err := driver.ToRecord(re.App)
	var token string
	if err == nil {
		token, err = record.NewStaticAuthToken(0) // 0 = use app default duration
	}
	if err != nil {
		// Non-fatal: fall back to the access token as a simple identifier
		token = req.AccessToken
	}

	StartDriverDistribution(re.App, cfg, driver.ID, driver.QPU, driver.NNGInPort, driver.NNGOutPort)

	resp := DriverConnectResponse{
		Status:     "success",
		Name:       driver.Name,
		NNGInPort:  driver.NNGInPort,
		NNGOutPort: driver.NNGOutPort,
		TLSHash:    cfg.GetTlsCaHash(),
		AuthToken:  token,
		NNGHost:    cfg.IpAddr,
	}
	return re.JSON(http.StatusOK, resp)
}

// handleDriverToggle handles POST /api/op/drivers/toggle — toggles a
// driver's enabled status (admin-only).
func handleDriverToggle(re *core.RequestEvent) error {
	cfg, err := config.GetConfigFromApp(re.App)
	if err != nil {
		return re.Error(http.StatusInternalServerError, "failed to retrieve configuration", err)
	}

	if !re.HasSuperuserAuth() {
		return re.Error(http.StatusForbidden, "admin access required", nil)
	}

	var req DriverToggleRequest
	if err := parseBody(cfg, re, &req); err != nil {
		return err
	}

	updateData := map[string]any{"enabled": req.Enabled}
	var driver db.Driver
	err = db.FindAndUpdateOne(re.App, cfg.CollectionDrivers, req.ID, &driver, updateData)
	if err != nil {
		if errors.Is(err, db.ErrNotFound) {
			return re.Error(http.StatusNotFound, "driver not found", err)
		}
		return re.Error(http.StatusInternalServerError, "failed to update driver status", err)
	}

	if !req.Enabled {
		StopDriverDistribution(req.ID)
	}

	var resp DriverToggleResponse
	_ = resp.RefreshFromDbModel(&driver)
	return re.JSON(http.StatusOK, resp)
}

// handleCalibrateDispatch handles POST /api/op/calibrate/dispatch — queues a
// calibration for a tuner driver (RFC 0004 §6.8).
//
// Admin-only, like the /api/op/* routes around it. A calibration takes a QPU out
// of service for hours and rewrites the parameters every subsequent job runs
// against; at user level it would be a denial of service with a plausible cover
// story (RFC 0004 §10).
//
// This queues rather than sends. A driver's PUSH socket lives inside its
// dispatcher goroutine and nothing here can reach it — and queueing is what makes
// a calibration requested while the driver is offline run on reconnect instead of
// being dropped.
func handleCalibrateDispatch(re *core.RequestEvent) error {
	cfg, err := config.GetConfigFromApp(re.App)
	if err != nil {
		return re.Error(http.StatusInternalServerError, "failed to retrieve configuration", err)
	}

	if !re.HasSuperuserAuth() {
		return re.Error(http.StatusForbidden, "admin access required", nil)
	}

	var req CalibrateDispatchRequest
	if err := parseBody(cfg, re, &req); err != nil {
		return err
	}

	if req.Mode == "" {
		req.Mode = "full"
	}
	if err := db.ValidateSelect(re.App, cfg.CollectionCalibrationRequests, "mode", req.Mode); err != nil {
		return re.Error(http.StatusBadRequest, err.Error(), nil)
	}
	// A partial run with no targets would widen to a full one on the driver,
	// taking the QPU out for hours nobody asked for.
	if req.Mode == "partial" && len(req.TargetQubits) == 0 {
		return re.Error(http.StatusBadRequest,
			"a partial calibration needs target_qubits", nil)
	}

	var driver db.Driver
	if err := db.FindOne(re.App, cfg.CollectionDrivers, req.DriverID, &driver); err != nil {
		if errors.Is(err, db.ErrNotFound) {
			return re.Error(http.StatusNotFound, "driver not found", err)
		}
		return re.Error(http.StatusInternalServerError, "failed to look up driver", err)
	}

	// Maintenance is the state a chip is in *while* someone works on it, so a
	// calibration is exactly what it should still accept. Switched off is not: that
	// QPU is out of service, and a calibration would put it back on the air.
	var qpu db.QPU
	if err := db.FindOne(re.App, cfg.CollectionQPUs, driver.QPU, &qpu); err == nil && !qpu.Enabled {
		return re.Error(http.StatusConflict, "this QPU is switched off", nil)
	}

	request := &db.CalibrationRequest{
		Driver:       req.DriverID,
		QPU:          driver.QPU,
		Mode:         req.Mode,
		TargetQubits: req.TargetQubits,
		TargetEdges:  req.TargetEdges,
		Status:       "pending",
	}
	if err := saveToDb(re.App, request); err != nil {
		return re.Error(http.StatusInternalServerError, "failed to queue calibration", err)
	}

	return re.JSON(http.StatusAccepted, CalibrateDispatchResponse{
		ID:     request.ID,
		Driver: request.Driver,
		Mode:   request.Mode,
		Status: request.Status,
	})
}

// handleQPUList handles GET /api/qpus — lists all online QPUs.
func handleQPUList(re *core.RequestEvent) error {
	cfg, err := config.GetConfigFromApp(re.App)
	if err != nil {
		return re.Error(http.StatusInternalServerError, "failed to retrieve configuration", err)
	}

	sort, skip, limit := getPaginationParams(re, "+created")
	var qpus []db.QPU
	err = db.FindMany(re.App, cfg.CollectionQPUs, &qpus, "status = 'online'", sort, limit, skip)
	if err != nil {
		return re.Error(http.StatusInternalServerError, "failed to query QPUs", err)
	}

	return re.JSON(http.StatusOK, qpus)
}

// handleQPUGet handles GET /api/qpus/{name} — retrieves a single QPU by name.
// Since the QPUs collection uses "name" as its primary key, we can look up
// directly by record ID.
func handleQPUGet(re *core.RequestEvent) error {
	cfg, err := config.GetConfigFromApp(re.App)
	if err != nil {
		return re.Error(http.StatusInternalServerError, "failed to retrieve configuration", err)
	}

	name := re.Request.PathValue("name")
	var qpu db.QPU
	err = db.FindOne(re.App, cfg.CollectionQPUs, name, &qpu)
	if err != nil {
		return re.Error(http.StatusNotFound, "QPU not found", err)
	}

	return re.JSON(http.StatusOK, &qpu)
}

// handleThemeActive handles GET /api/theme/active
// — returns the currently active theme for the dashboard.
func handleThemeActive(re *core.RequestEvent) error {
	theme, err := config.GetActiveThemeFromApp(re.App)
	if err != nil {
		return re.Error(http.StatusInternalServerError, "failed to retrieve active theme", err)
	}

	return re.JSON(http.StatusOK, theme)
}

// handleThemeDefaults handles GET /api/theme/defaults
// — returns the default theme.
func handleThemeDefaults(re *core.RequestEvent) error {
	re.Response.Header().Set("Cache-Control", "public, max-age=300")
	return re.JSON(http.StatusOK, config.DefaultThemeSchema)
}

// handleThemeCSS handles GET /api/theme/css, returning the custom CSS of the active theme
// as a text/css response. If no custom CSS is set, it returns 204 No Content.
func handleThemeCSS(re *core.RequestEvent) error {
	re.Response.Header().Set("Cache-Control", "public, max-age=300")
	theme, err := config.GetActiveThemeFromApp(re.App)
	if err != nil {
		return re.Error(http.StatusInternalServerError, "failed to retrieve active theme", err)
	}

	if theme == nil || theme.CustomCSS == "" {
		return re.NoContent(http.StatusNoContent)
	}

	return re.Blob(http.StatusOK, "text/css", []byte(theme.CustomCSS))
}

// handleThemeJS handles GET /api/theme/js, returning the custom JavaScript of the active theme
// as a text/javascript response. If no custom JS is set, it returns 204 No Content.
func handleThemeJS(re *core.RequestEvent) error {
	re.Response.Header().Set("Cache-Control", "public, max-age=300")
	theme, err := config.GetActiveThemeFromApp(re.App)
	if err != nil {
		return re.Error(http.StatusInternalServerError, "failed to retrieve active theme", err)
	}

	if theme == nil || theme.CustomJS == "" {
		return re.NoContent(http.StatusNoContent)
	}

	return re.Blob(http.StatusOK, "text/javascript", []byte(theme.CustomJS))
}
