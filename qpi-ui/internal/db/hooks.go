package db

import (
	"fmt"
	"qpi/internal/config"
	"qpi/internal/lib"

	"github.com/pocketbase/dbx"
	"github.com/pocketbase/pocketbase/core"
)

// CollectionHookMap is a map of collection names to hook functions.
type CollectionHookMap map[string]func(e *core.RecordEvent) error

// RegisterCollectionHooks returns a hook function for each default collection name in the hooks map
func RegisterCollectionHooks(app core.App, hooks CollectionHookMap) func(e *core.RecordEvent) error {
	return func(e *core.RecordEvent) error {
		cfg := config.MustGetConfigFromApp(app)
		col := e.Record.Collection().Name

		defaultColName := cfg.GetCollectionName(col)
		if fn, ok := hooks[defaultColName]; ok {
			return fn(e)
		}

		return e.Next()
	}
}

// OnTimeSlotUpsert runs on time slot creation or update
func OnTimeSlotUpsert(e *core.RecordEvent) error {
	if err := ValidateTimeSlot(e.App, e.Record); err != nil {
		return err
	}
	return e.Next()
}

// OnQpuCreate runs on QPU creation
func OnQpuCreate(e *core.RecordEvent) error {
	// Hash access_token if it exists
	token := e.Record.GetString("access_token")
	if token != "" {
		e.Record.Set("access_token", HashToken(token))
	}

	if !e.Record.GetBool("enabled") {
		e.Record.Set("status", "offline")
	}
	return e.Next()
}

// OnQpuUpdate runs on QPU update
func OnQpuUpdate(e *core.RecordEvent) error {
	// Hash access_token if it changed
	originalToken := e.Record.Original().GetString("access_token")
	newToken := e.Record.GetString("access_token")
	if newToken != "" && newToken != originalToken {
		e.Record.Set("access_token", HashToken(newToken))
	}

	originalEnabled := e.Record.Original().GetBool("enabled")
	newEnabled := e.Record.GetBool("enabled")

	if originalEnabled != newEnabled {
		if !newEnabled {
			e.Record.Set("status", "offline")
		} else {
			e.Record.Set("status", ConnectedQpuStatus(e.Record))
		}
	}
	return e.Next()
}

// ConnectedQpuStatus is what a QPU's status should be when nothing is overriding
// it: online if it holds a port pair on this server, offline otherwise.
//
// Shared with the maintenance endpoint, which needs the same answer when clearing
// maintenance — writing `online` there instead would claim a connection that may
// not exist.
func ConnectedQpuStatus(record *core.Record) string {
	cmdPort := record.GetInt("nng_command_port")
	resPort := record.GetInt("nng_result_port")
	if cmdPort > 0 && resPort > 0 {
		return "online"
	}
	return "offline"
}

// OnDriverCreate runs on driver creation, hashing its token the same way
// OnQpuCreate hashes a QPU's access_token (RFC 0001 §7, §9).
func OnDriverCreate(e *core.RecordEvent) error {
	token := e.Record.GetString("token")
	if token != "" {
		e.Record.Set("token", HashToken(token))
	}

	if !e.Record.GetBool("enabled") {
		e.Record.Set("status", "offline")
	}
	return e.Next()
}

// OnDriverUpdate runs on driver update, hashing its token if it changed.
func OnDriverUpdate(e *core.RecordEvent) error {
	originalToken := e.Record.Original().GetString("token")
	newToken := e.Record.GetString("token")
	if newToken != "" && newToken != originalToken {
		e.Record.Set("token", HashToken(newToken))
	}

	originalEnabled := e.Record.Original().GetBool("enabled")
	newEnabled := e.Record.GetBool("enabled")
	if originalEnabled != newEnabled && !newEnabled {
		e.Record.Set("status", "offline")
	}
	return e.Next()
}

// OnThemeUpsert enforces that at most one theme is active at any time.
// When a theme is saved with is_active=true, all other active themes are
// deactivated (RFC 0002 §3.5). It also updates the in-memory active theme cache.
func OnThemeUpsert(e *core.RecordEvent) error {
	cfg, err := config.GetConfigFromApp(e.App)
	if err != nil {
		return err
	}

	rec := e.Record
	activeTheme := cfg.GetActiveTheme()
	isActivating := rec.GetBool("is_active")
	isDeactivating := !isActivating && activeTheme != nil && activeTheme.ID == rec.Id

	if isActivating {
		err := activateTheme(e.App, cfg, rec)
		if err != nil {
			return err
		}
	}

	if isDeactivating {
		cfg.UpdateActiveTheme(nil)
	}

	return e.Next()
}

// OnThemeDelete reverts the active theme cache to the default theme if the deleted theme was active.
func OnThemeDelete(e *core.RecordEvent) error {
	cfg, err := config.GetConfigFromApp(e.App)
	if err != nil {
		return err
	}

	rec := e.Record
	activeTheme := cfg.GetActiveTheme()
	isDeactivating := activeTheme != nil && activeTheme.ID == rec.Id
	if isDeactivating {
		cfg.UpdateActiveTheme(nil)
	}

	return e.Next()
}

// activateTheme activates the given theme as seen by the app's config
func activateTheme(app core.App, cfg *config.AppConfig, rec *core.Record) error {
	// Deactivate all other active themes, without running their hooks
	// (to avoid infinite recursion).
	col := cfg.CollectionThemes
	filter := dbx.NewExp("is_active = true AND id != {:id}", dbx.Params{"id": rec.Id})
	update := dbx.Params{
		"is_active": false,
		"updated":   lib.GetUtcNow(),
	}

	_, err := app.DB().Update(col, update, filter).Execute()
	if err != nil {
		return fmt.Errorf("failed to deactivate other active themes: %w", err)
	}

	var theme Theme
	if err := theme.RefreshFromRecord(rec); err != nil {
		return err
	}

	cfg.UpdateActiveTheme(&theme.ThemeSchema)
	return nil
}
