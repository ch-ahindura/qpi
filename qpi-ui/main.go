// Package main is the entry point for the QPI server service, bootstrapping PocketBase,
// binding custom command line flags, and registering dbs, HTTP handlers, and recovery background tasks.
package main

import (
	"context"
	"embed"
	"log"

	"github.com/pocketbase/pocketbase"
	"github.com/pocketbase/pocketbase/core"
	"github.com/pocketbase/pocketbase/tools/hook"

	"qpi/internal/api"
	"qpi/internal/config"
	"qpi/internal/db"
	"qpi/internal/scheduler"
)

//go:embed all:internal/dashboard/dist
var dashboardFS embed.FS

var Version = "v0.3.1"

func main() {
	app := pocketbase.New()

	// Bind custom persistent CLI flags to configuration variables
	config.BindFlags(app.RootCmd)

	// set the current version of the application
	app.RootCmd.Version = Version
	api.Version = Version

	appCtx, cancelAppCtx := context.WithCancel(context.Background())
	defer cancelAppCtx()

	// Bootstrap: create collections on first boot
	app.OnBootstrap().Bind(&hook.Handler[*core.BootstrapEvent]{
		Func: func(e *core.BootstrapEvent) error {
			// Populate and save AppConfig to the App store
			cfg, err := config.NewFromFlags(app.RootCmd)
			if err != nil {
				return err
			}

			cfg.StartTlsRenewalWorker(appCtx)

			config.SaveConfigOnApp(e.App, cfg)

			if err := e.Next(); err != nil {
				return err
			}
			if err := db.EnsureSchema(e.App); err != nil {
				return err
			}

			theme, err := db.GetActiveThemeSchema(e.App)
			if err != nil {
				return err
			}

			cfg.UpdateActiveTheme(theme)

			if err := api.MarkEveryDriverOffline(e.App); err != nil {
				return err
			}
			return nil
		},
	})

	// Register custom HTTP routes & background tasks
	app.OnServe().Bind(&hook.Handler[*core.ServeEvent]{
		Func: func(e *core.ServeEvent) error {
			// Initialize the TLS
			err := api.SetupServer(e)
			if err != nil {
				return err
			}

			// Register api register handler routes
			api.RegisterRoutes(e, dashboardFS)

			// Start the global recovery engine
			go scheduler.RunRecoveryEngine(e.App)

			// Start the events log retention prune (no-op with the flag off)
			go scheduler.RunEventsRetentionEngine(e.App)

			return e.Next()
		},
	})

	// for database
	app.OnRecordCreate().Bind(&hook.Handler[*core.RecordEvent]{
		Func: db.RegisterCollectionHooks(app, db.CollectionHookMap{
			config.DefaultTimeSlotsCollection: db.OnTimeSlotUpsert,
			config.DefaultQpusCollection:      db.OnQpuCreate,
			config.DefaultDriversCollection:   db.OnDriverCreate,
			config.DefaultThemesCollection:    db.OnThemeUpsert,
		}),
	})

	app.OnRecordUpdate().Bind(&hook.Handler[*core.RecordEvent]{
		Func: db.RegisterCollectionHooks(app, db.CollectionHookMap{
			config.DefaultTimeSlotsCollection: db.OnTimeSlotUpsert,
			config.DefaultQpusCollection:      db.OnQpuUpdate,
			config.DefaultDriversCollection:   db.OnDriverUpdate,
			config.DefaultThemesCollection:    db.OnThemeUpsert,
		}),
	})

	app.OnRecordDelete().Bind(&hook.Handler[*core.RecordEvent]{
		Func: db.RegisterCollectionHooks(app, db.CollectionHookMap{
			config.DefaultThemesCollection: db.OnThemeDelete,
		}),
	})

	// A deleted driver's goroutines and listener would otherwise run forever
	// against an id that no longer exists, holding a port pair no record claims
	// any more: findFreePorts skips it, because its probe finds the zombie still
	// bound, so the pair is simply lost from the range. Bound here rather than in
	// db's hook map because releasing the lease lives in api, and db must not
	// import it.
	app.OnRecordAfterDeleteSuccess().Bind(&hook.Handler[*core.RecordEvent]{
		Func: func(e *core.RecordEvent) error {
			api.ReleaseLeaseIfDriver(e.App, e.Record)
			return e.Next()
		},
	})

	// For requests
	app.OnRecordCreateRequest().Bind(&hook.Handler[*core.RecordRequestEvent]{
		Func: api.RegisterRequestHooks(app, api.RequestHookMap{
			config.DefaultTimeSlotsCollection:       api.OnTimeSlotCreateRequest,
			config.DefaultQPUTimeRequestsCollection: api.OnQPUTimeRequestCreateRequest,
			config.DefaultQuantumJobsCollection:     api.OnQuantumJobCreateRequest,
		}),
	})

	app.OnRecordUpdateRequest().Bind(&hook.Handler[*core.RecordRequestEvent]{
		Func: api.RegisterRequestHooks(app, api.RequestHookMap{
			config.DefaultTimeSlotsCollection:       api.OnTimeSlotUpdateRequest,
			config.DefaultQPUTimeRequestsCollection: api.OnQPUTimeRequestUpdateRequest,
			config.DefaultQpusCollection:            api.OnQpuUpdateRequest,
		}),
	})

	app.OnRecordDeleteRequest().Bind(&hook.Handler[*core.RecordRequestEvent]{
		Func: api.RegisterRequestHooks(app, api.RequestHookMap{
			config.DefaultTimeSlotsCollection: api.OnTimeSlotDeleteRequest,
		}),
	})

	if err := app.Start(); err != nil {
		log.Fatal(err)
	}
}
