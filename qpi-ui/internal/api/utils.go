package api

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"log"
	"net"
	"net/http"
	"qpi/internal/config"
	"qpi/internal/db"
	"qpi/internal/drivers"
	"strconv"
	"strings"
	"time"

	"github.com/pocketbase/pocketbase/core"
)

// getPaginationParams gets the pagination parameters from the request event, given a default sort
func getPaginationParams(re *core.RequestEvent, defaultSort string) (sort string, skip int, limit int) {
	skip = getQueryParamInt(re, "skip", 0)
	limit = getQueryParamInt(re, "limit", 0)
	sort = getQueryParam(re, "sort", defaultSort)
	return
}

// getQueryParam gets the query parameter from the request event, defaulting to a given value if not present
func getQueryParam(re *core.RequestEvent, key string, defaultValue string) string {
	value := re.Request.URL.Query().Get(key)
	if value == "" {
		return defaultValue
	}
	return value
}

// getQueryParamInt gets the query param as an integer, defaulting to a given value if not present or invalid
func getQueryParamInt(re *core.RequestEvent, key string, defaultValue int) int {
	value := re.Request.URL.Query().Get(key)
	if value == "" {
		return defaultValue
	}
	parsedValue, err := strconv.Atoi(value)
	if err != nil {
		return defaultValue
	}

	return parsedValue
}

// getCurrentUser gets the current logged in user
func getCurrentUser(re *core.RequestEvent) (*db.User, error) {
	if re.Auth != nil && re.Auth.Collection().Name == "users" {
		var user db.User
		err := user.RefreshFromRecord(re.Auth)
		if err != nil {
			return nil, re.Error(http.StatusUnauthorized, "authentication required", err)
		}

		return &user, nil
	}

	if re.HasSuperuserAuth() && re.Auth != nil {
		usersCol, err := re.App.FindCollectionByNameOrId("users")
		if err != nil {
			return nil, re.Error(http.StatusInternalServerError, "users collection not found", err)
		}

		record, err := re.App.FindFirstRecordByData("users", "email", re.Auth.Email())
		if err != nil {
			// Proxy user does not exist, so create one for superuser
			record = core.NewRecord(usersCol)
			// Let PocketBase auto-generate the ID
			record.Set("email", re.Auth.Email())
			username := re.Auth.Id
			if len(username) > 5 {
				username = username[:5]
			}
			record.Set("username", "admin_"+username)
			record.Set("qpu_seconds", 999999999.0)

			if err := re.App.SaveNoValidate(record); err != nil {
				log.Printf("Failed to create proxy user: %v", err)
				return nil, re.Error(http.StatusInternalServerError, "failed to provision proxy user", err)
			}
		}

		var user db.User
		err = user.RefreshFromRecord(record)
		if err != nil {
			return nil, err
		}

		// Ensure superuser has practically infinite QPU seconds
		if user.QPUSeconds < 999999999.0 {
			record.Set("qpu_seconds", 999999999.0)
			if err := re.App.SaveNoValidate(record); err != nil {
				log.Printf("Failed to update proxy user quota: %v", err)
				return nil, re.Error(http.StatusInternalServerError, "failed to update proxy user quota", err)
			}
			user.QPUSeconds = 999999999.0
		}

		return &user, nil
	}

	token := getToken(re)
	user, err := db.GetUserByToken(re.App, token)
	if err != nil {
		return nil, re.Error(http.StatusUnauthorized, "authentication required", err)
	}

	return user, nil
}

// getToken gets the token from the request
func getToken(re *core.RequestEvent) string {
	token := re.Request.Header.Get("X-API-Token")
	if token != "" {
		return token
	}

	authHeader := re.Request.Header.Get("Authorization")
	if strings.HasPrefix(authHeader, "Bearer ") {
		return strings.TrimPrefix(authHeader, "Bearer ")
	}

	return ""
}

// parseBody parses the request body and validates it
func parseBody[T GeneralDTO](cfg *config.AppConfig, re *core.RequestEvent, v T) error {
	err := re.BindBody(v)
	if err != nil {
		return re.Error(http.StatusBadRequest, "invalid request body", err)
	}

	v.SetDefaults()
	err = cfg.Validator.Struct(v)
	if err != nil {
		return re.Error(http.StatusBadRequest, "invalid request body", err)
	}

	return nil
}

// Saves a given model into the database and updates itself based on the db value
func saveToDb[T db.DbModel](app core.App, item T) error {
	record, err := item.ToRecord(app)
	if err != nil {
		return err
	}

	err = app.Save(record)
	if err != nil {
		return err
	}

	err = item.RefreshFromRecord(record)
	if err != nil {
		return err
	}

	return nil
}

// joinKinds renders driver kinds for an error message an operator reads, so an
// SDK with no built-in devices says so rather than trailing off after "it ships".
func joinKinds(kinds []drivers.Kind) string {
	if len(kinds) == 0 {
		return "no built-in devices"
	}
	names := make([]string, len(kinds))
	for i, kind := range kinds {
		names[i] = strconv.Quote(string(kind))
	}
	return strings.Join(names, ", ")
}

// getAddrFromReq extracts the scheme and host from a request to build the full QPI address
func getAddrFromReq(re *core.RequestEvent) string {
	scheme := "http"
	if re.Request.TLS != nil {
		scheme = "https"
	}
	if proto := re.Request.Header.Get("X-Forwarded-Proto"); proto != "" {
		scheme = proto
	}
	return fmt.Sprintf("%s://%s", scheme, re.Request.Host)
}

// findFreePorts searches for free TCP ports within the configuration range,
// excluding ports currently reserved/allocated by QPUs or, once the driver
// framework is on, by drivers — both bind on the same host, so neither may
// steal the other's port (RFC 0001 §7).
func findFreePorts(app core.App, count int) ([]int, error) {
	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return nil, err
	}

	allocated := make(map[int]bool)
	collectAllocatedPorts(app, cfg.CollectionQPUs, allocated, "nng_command_port", "nng_result_port")
	collectAllocatedPorts(app, cfg.CollectionDrivers, allocated, "nng_in_port", "nng_out_port")

	var ports []int
	for port := cfg.PortRangeStart; port < cfg.PortRangeEnd; port++ {
		if allocated[port] {
			continue
		}
		addr := fmt.Sprintf(":%d", port)
		ln, err := net.Listen("tcp", addr)
		if err == nil {
			ln.Close()
			ports = append(ports, port)
			allocated[port] = true
			if len(ports) == count {
				return ports, nil
			}
		}
	}
	return nil, fmt.Errorf("could not find %d free ports in range %d-%d", count, cfg.PortRangeStart, cfg.PortRangeEnd)
}

// collectAllocatedPorts records the ports already stored in the given fields
// of a collection's records into allocated. A collection that doesn't exist
// yet (e.g. drivers with the flag off) is silently skipped, so this stays a
// no-op for QPU port allocation until the driver framework is enabled.
func collectAllocatedPorts(app core.App, collectionName string, allocated map[int]bool, fields ...string) {
	if collectionName == "" {
		return
	}

	filterParts := make([]string, len(fields))
	for i, field := range fields {
		filterParts[i] = fmt.Sprintf("%s > 0", field)
	}
	filter := strings.Join(filterParts, " || ")

	records, err := app.FindRecordsByFilter(collectionName, filter, "", 0, 0)
	if err != nil {
		return
	}
	for _, r := range records {
		for _, field := range fields {
			if port := r.GetInt(field); port > 0 {
				allocated[port] = true
			}
		}
	}
}

// generateAPIToken creates a new random API token string.
func generateAPIToken() string {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		// Fallback to timestamp-based entropy if crypto/rand fails
		for i := range b {
			b[i] = byte(time.Now().UnixNano() % 256)
			time.Sleep(1 * time.Nanosecond)
		}
	}
	return "qpi_" + hex.EncodeToString(b)
}
