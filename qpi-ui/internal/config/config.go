// Package config manages application-wide configuration parameters for the QPI server,
// including database collection names, NNG connection ports, and job timeout intervals.
// Configuration is saved on the pocketbase app store for concurrent-safe thread access.
// It supports a strict configuration precedence: CLI Flag > Env Var > Config File > Default.
package config

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/go-playground/validator/v10"
	"github.com/pocketbase/pocketbase/core"
	"github.com/spf13/cobra"
	"gopkg.in/yaml.v3"
)

const (
	appStoreConfigKey                    = "custom_config"
	DefaultQpusCollection                = "qpus"
	DefaultTimeSlotsCollection           = "time_slots"
	DefaultQuantumJobsCollection         = "quantum_jobs"
	DefaultAPITokensCollection           = "api_tokens"
	DefaultQPUTimeRequestsCollection     = "qpu_time_requests"
	DefaultNotificationsCollection       = "notifications"
	DefaultDriversCollection             = "drivers"
	DefaultEventsCollection              = "events"
	DefaultThemesCollection              = "themes"
	DefaultCalibrationResultsCollection  = "calibration_results"
	DefaultCalibrationRequestsCollection = "calibration_requests"
	DefaultTLSCertFile                   = ".qpi.cert.pem"
	DefaultTLSKeyFile                    = ".qpi.key"
	DefaultTLSCaCertFile                 = ".qpi.ca.pem"
	DefaultTLSCaKeyFile                  = ".qpi.ca.key"
)

var DefaultThemeSchema = NewDefaultThemeSchema()

// Package local flags populated by Cobra flag bindings.
var (
	flagConfigFile                    string
	flagCollectionQPUs                string
	flagCollectionTimeSlots           string
	flagCollectionQuantumJobs         string
	flagCollectionAPITokens           string
	flagCollectionNotifications       string
	flagCollectionTimeRequests        string
	flagCollectionDrivers             string
	flagCollectionEvents              string
	flagCollectionThemes              string
	flagCollectionCalibrationResults  string
	flagCollectionCalibrationRequests string
	flagIdleThreshold                 time.Duration
	flagRecoveryInterval              time.Duration
	flagJobTimeout                    time.Duration
	flagDispatchPollInterval          time.Duration
	flagEventsRetention               time.Duration
	flagEventsPruneInterval           time.Duration
	flagCalibrationRequestRetention   time.Duration
	flagCalibrationResultRetention    time.Duration
	flagCalibrationFitRetention       time.Duration
	flagEventRateLimit                int
	flagPortRangeStart                int
	flagPortRangeEnd                  int
	flagDisableEmailPasswordAuth      bool
	flagOAuth2Providers               string // JSON array string
	flagTLSCertFile                   string
	flagTLSKeyFile                    string
	flagTLSCaCertFile                 string
	flagTLSCaKeyFile                  string
	flagDomainName                    string
	flagServerPort                    int
	flagIpAddr                        string
)

// AppConfig stores application-wide configuration parameters for the QPI server.
type AppConfig struct {
	CollectionQPUs                string
	CollectionTimeSlots           string
	CollectionQuantumJobs         string
	CollectionAPITokens           string
	CollectionQPUTimeRequests     string
	CollectionNotifications       string
	CollectionDrivers             string
	CollectionEvents              string
	CollectionThemes              string
	CollectionCalibrationResults  string
	CollectionCalibrationRequests string
	IdleThreshold                 time.Duration
	RecoveryInterval              time.Duration
	JobTimeout                    time.Duration
	DispatchPollInterval          time.Duration
	EventsRetention               time.Duration
	EventsPruneInterval           time.Duration
	// How long a finished calibration request is kept (RFC 0006 §9). Once its
	// report is stored the row holds nothing the report does not, so this is
	// bookkeeping and is pruned by default. A `running` row is never pruned however
	// old, or a hung calibration would lose its record while still running.
	CalibrationRequestRetention time.Duration
	// How long a calibration report is kept. Zero — the default — never prunes one:
	// a report is what the chip *was*, and RFC 0004 §9's whole argument for storing
	// them is that the history is the point. The lever exists for an operator with a
	// disk problem, and for nobody else.
	CalibrationResultRetention time.Duration
	// How long a report's fit summaries and benchmark raw data are kept, separately
	// from the report carrying them. They are most of a phase-3 row and the least
	// durable part of its value: nobody re-reads the Rabi trace from eight months
	// ago, but the fitted amp180 is the record of what the chip was.
	CalibrationFitRetention  time.Duration
	EventRateLimit           int
	PortRangeStart           int
	PortRangeEnd             int
	DisableEmailPasswordAuth bool
	OAuth2Providers          []core.OAuth2ProviderConfig
	Validator                *validator.Validate
	TlsCertFile              string
	TlsKeyFile               string
	TlsCaCertFile            string
	TlsCaKeyFile             string
	DomainName               string
	ServerPort               int
	IpAddr                   string
	tlsConfig                *certKeyPair
	parsedTlsConfig          *tls.Config
	tlsCaConfig              *certKeyPair
	tlsCaHash                string
	activeCert               *tls.Certificate
	activeTheme              *ThemeSchema
	mu                       sync.RWMutex
}

// GetCollectionName returns the collection name for a given default collection name.
func (c *AppConfig) GetCollectionName(name string) string {
	if c == nil {
		return name
	}
	switch name {
	case DefaultQpusCollection:
		if c.CollectionQPUs != "" {
			return c.CollectionQPUs
		}
		return DefaultQpusCollection
	case DefaultTimeSlotsCollection:
		if c.CollectionTimeSlots != "" {
			return c.CollectionTimeSlots
		}
		return DefaultTimeSlotsCollection
	case DefaultQuantumJobsCollection:
		if c.CollectionQuantumJobs != "" {
			return c.CollectionQuantumJobs
		}
		return DefaultQuantumJobsCollection
	case DefaultAPITokensCollection:
		if c.CollectionAPITokens != "" {
			return c.CollectionAPITokens
		}
		return DefaultAPITokensCollection
	case DefaultQPUTimeRequestsCollection:
		if c.CollectionQPUTimeRequests != "" {
			return c.CollectionQPUTimeRequests
		}
		return DefaultQPUTimeRequestsCollection
	case DefaultNotificationsCollection:
		if c.CollectionNotifications != "" {
			return c.CollectionNotifications
		}
		return DefaultNotificationsCollection
	case DefaultDriversCollection:
		if c.CollectionDrivers != "" {
			return c.CollectionDrivers
		}
		return DefaultDriversCollection
	case DefaultEventsCollection:
		if c.CollectionEvents != "" {
			return c.CollectionEvents
		}
		return DefaultEventsCollection
	case DefaultThemesCollection:
		if c.CollectionThemes != "" {
			return c.CollectionThemes
		}
		return DefaultThemesCollection
	case DefaultCalibrationResultsCollection:
		if c.CollectionCalibrationResults != "" {
			return c.CollectionCalibrationResults
		}
		return DefaultCalibrationResultsCollection
	case DefaultCalibrationRequestsCollection:
		if c.CollectionCalibrationRequests != "" {
			return c.CollectionCalibrationRequests
		}
		return DefaultCalibrationRequestsCollection
	default:
		return name
	}
}

// GetDefaultCollectionName returns the default collection name for a given default collection name.
func (c *AppConfig) GetDefaultCollectionName(name string) string {
	switch name {
	case c.CollectionQPUs:
		return DefaultQpusCollection
	case c.CollectionTimeSlots:
		return DefaultTimeSlotsCollection
	case c.CollectionQuantumJobs:
		return DefaultQuantumJobsCollection
	case c.CollectionAPITokens:
		return DefaultAPITokensCollection
	case c.CollectionQPUTimeRequests:
		return DefaultQPUTimeRequestsCollection
	case c.CollectionNotifications:
		return DefaultNotificationsCollection
	case c.CollectionDrivers:
		return DefaultDriversCollection
	case c.CollectionEvents:
		return DefaultEventsCollection
	case c.CollectionThemes:
		return DefaultThemesCollection
	case c.CollectionCalibrationResults:
		return DefaultCalibrationResultsCollection
	case c.CollectionCalibrationRequests:
		return DefaultCalibrationRequestsCollection
	default:
		return name
	}
}

// GetTlsConfig gets this apps TLS configuration
func (c *AppConfig) GetTlsConfig() *tls.Config {
	return c.parsedTlsConfig
}

// GetTlsCaConfig gets this apps TLS CA configuration
func (c *AppConfig) GetTlsCaConfig() *certKeyPair {
	return c.tlsCaConfig
}

// GetTlsCaHash gets the hash of the TLS CA to be used by clients to verify
// the root CA TLS certificates from this server
func (c *AppConfig) GetTlsCaHash() string {
	return c.tlsCaHash
}

// GetCertificate the active for the server to use
func (cfg *AppConfig) GetCertificate(clientHello *tls.ClientHelloInfo) (*tls.Certificate, error) {
	cfg.mu.RLock()
	defer cfg.mu.RUnlock()

	if cfg.activeCert == nil {
		return nil, fmt.Errorf("no active leaf certificate loaded")
	}

	return cfg.activeCert, nil
}

// StartTlsRenewalWorker runs in the background and checks the certificate every 24 hours,
// renewing it if it is about to expire
func (cfg *AppConfig) StartTlsRenewalWorker(ctx context.Context) {
	go func() {
		interval := 24 * time.Hour       // 1 day
		certBuffer := 2 * 24 * time.Hour // 2 days
		caBuffer := 30 * 24 * time.Hour  // 30 days

		ticker := time.NewTicker(interval)
		defer ticker.Stop()

		for range ticker.C {
			select {
			case <-ctx.Done():
				return
			default:
			}

			// Check if CA is up for renewal
			if isCertUpForRenewal(cfg.tlsCaConfig, caBuffer) {
				log.Println("[Config] CA (certificate authority) regenerating...")

				var err error
				cfg.tlsCaConfig, err = generateCA(cfg.TlsCaCertFile, cfg.TlsCaKeyFile)
				if err != nil {
					log.Printf("[Config] Error regenerating root CA: %v\n", err)
					continue
				}

				// regenerate certificate with new CA
				err = generateCertAndKeyFiles(cfg.DomainName, cfg.TlsCertFile, cfg.TlsKeyFile, cfg.tlsCaConfig, cfg.IpAddr)
				if err != nil {
					log.Printf("[Config] Error regenerating TLS cert and key: %v\n", err)
					continue
				}

				cfg.tlsConfig, err = getTlsCertKeyPair(cfg.TlsCertFile, cfg.TlsKeyFile, cfg.DomainName, cfg.tlsCaConfig, cfg.IpAddr)
				if err != nil {
					log.Printf("[Config] Error getting TLS config: %v\n", err)
					continue
				}

				// Refresh active TLS certicate
				err = cfg.refreshActiveTls()
				if err != nil {
					log.Printf("[Config] Error refreshing active certificate: %v\n", err)
					continue
				}

				// Save the hash
				cfg.tlsCaHash, err = getCaCertHash(cfg.tlsCaConfig)
				if err != nil {
					log.Printf("[Config] Error hashing the CA certificate: %v\n", err)
					continue
				}

				log.Printf("Root CA successfully renewed! Fingerprint: %s\n", cfg.tlsCaHash)
			}

			// Check if certificate is up for renewal
			if isCertUpForRenewal(cfg.tlsConfig, certBuffer) {
				log.Println("[Config] TLS certificate regenerating...")

				err := generateCertAndKeyFiles(cfg.DomainName, cfg.TlsCertFile, cfg.TlsKeyFile, cfg.tlsCaConfig, cfg.IpAddr)
				if err != nil {
					log.Printf("[Config] Error regenerating TLS cert and key: %v\n", err)
					continue
				}

				cfg.tlsCaConfig, err = getCA(cfg.TlsCaCertFile, cfg.TlsCaKeyFile)
				if err != nil {
					log.Printf("[Config] Error loading TLS config: %v\n", err)
					continue
				}

				cfg.tlsConfig, err = getTlsCertKeyPair(cfg.TlsCertFile, cfg.TlsKeyFile, cfg.DomainName, cfg.tlsCaConfig, cfg.IpAddr)
				if err != nil {
					log.Printf("[Config] Error loading TLS config: %v\n", err)
					continue
				}

				// Refresh active TLS certicate
				err = cfg.refreshActiveTls()
				if err != nil {
					log.Printf("[Config] Error refreshing active certificate: %v\n", err)
					continue
				}

				log.Printf("Certificate successfully renewed! Fingerprint: %s\n", cfg.tlsCaHash)
			}
		}
	}()
}

// UpdateActiveTheme sets the currently active theme in the config.
// if theme is nil, it falls back to the default theme schema.
func (cfg *AppConfig) UpdateActiveTheme(theme *ThemeSchema) {
	cfg.mu.Lock()
	defer cfg.mu.Unlock()
	if theme == nil {
		theme = DefaultThemeSchema
	}

	cfg.activeTheme = theme
}

// GetActiveTheme returns the currently active theme from the config.
func (cfg *AppConfig) GetActiveTheme() *ThemeSchema {
	return cfg.activeTheme
}

// refreshActiveTls refreshes the active certificate from the current values
// of the certificate and key
func (cfg *AppConfig) refreshActiveTls() error {
	tlsConf, err := loadTLS(cfg.TlsCertFile, cfg.TlsKeyFile)
	if err != nil {
		return fmt.Errorf("error loading TLS config: %w\n", err)
	}

	// update the parsedTlsConfig
	cfg.parsedTlsConfig = tlsConf

	tlsCert := tlsConf.Certificates[0]

	// Pre-populate the parsed Leaf structure to save CPU cycles during client handshakes
	tlsCert.Leaf, err = x509.ParseCertificate(tlsCert.Certificate[0])
	if err != nil {
		return fmt.Errorf("error parsing certificate: %v\n", err)
	}

	// Safely update the active certificate without interrupting connections
	cfg.mu.Lock()
	defer cfg.mu.Unlock()
	cfg.activeCert = &tlsCert

	return nil
}

// SaveConfigOnApp saves the config on the app instance store.
func SaveConfigOnApp(app core.App, config *AppConfig) {
	app.Store().Set(appStoreConfigKey, config)
}

// GetConfigFromApp retrieves the config from the app instance store.
func GetConfigFromApp(app core.App) (*AppConfig, error) {
	value := app.Store().Get(appStoreConfigKey)
	config, ok := value.(*AppConfig)
	if !ok {
		return nil, fmt.Errorf("failed to retrieve AppConfig from app store")
	}
	return config, nil
}

// MustGetConfigFromApp retrieves the config from the app instance store, panicking if it fails.
func MustGetConfigFromApp(app core.App) *AppConfig {
	cfg, err := GetConfigFromApp(app)
	if err != nil {
		panic(err)
	}
	return cfg
}

// GetActiveThemeFromApp retrieves the active theme schema from the app instance store.
// It is just a convenience function to get the theme from the app
func GetActiveThemeFromApp(app core.App) (*ThemeSchema, error) {
	cfg, err := GetConfigFromApp(app)
	if err != nil {
		return nil, err
	}
	return cfg.activeTheme, nil
}

// BindFlags registers custom flags on the Cobra command.
func BindFlags(cmd *cobra.Command) {
	cmd.PersistentFlags().StringVar(&flagConfigFile, "config-file", getEnvString("QPI_CONFIG_FILE", "qpi.config.yml"), "Path to QPI JSON configuration file")
	cmd.PersistentFlags().StringVar(&flagTLSCertFile, "tls-cert-file", DefaultTLSCertFile, "Path to QPI TLS certificate file")
	cmd.PersistentFlags().StringVar(&flagTLSKeyFile, "tls-key-file", DefaultTLSKeyFile, "Path to QPI TLS key file")
	cmd.PersistentFlags().StringVar(&flagTLSCaCertFile, "tls-ca-cert-file", DefaultTLSCaCertFile, "Path to QPI TLS certificate authority CA cert file")
	cmd.PersistentFlags().StringVar(&flagTLSCaKeyFile, "tls-ca-key-file", DefaultTLSCaKeyFile, "Path to QPI TLS certificate authority CA key file")
	cmd.PersistentFlags().StringVar(&flagDomainName, "domain", "", "The domain name this server is running on")
	cmd.PersistentFlags().StringVar(&flagIpAddr, "ip-addr", "127.0.0.1", "The public IP address to include in the generated TLS certificates")
	cmd.PersistentFlags().IntVar(&flagServerPort, "server-port", 8090, "The port this server should run on")
	cmd.PersistentFlags().StringVar(&flagCollectionQPUs, "qpus-collection", DefaultQpusCollection, "Collection name for QPUs")
	cmd.PersistentFlags().StringVar(&flagCollectionTimeSlots, "timeslots-collection", DefaultTimeSlotsCollection, "Collection name for Time Slots")
	cmd.PersistentFlags().StringVar(&flagCollectionQuantumJobs, "jobs-collection", DefaultQuantumJobsCollection, "Collection name for Quantum Jobs")
	cmd.PersistentFlags().StringVar(&flagCollectionAPITokens, "api-tokens-collection", DefaultAPITokensCollection, "Collection name for API Tokens")
	cmd.PersistentFlags().StringVar(&flagCollectionNotifications, "notifications-collection", DefaultNotificationsCollection, "Collection name for Notifications")
	cmd.PersistentFlags().StringVar(&flagCollectionTimeRequests, "qpu-time-requests-collection", DefaultQPUTimeRequestsCollection, "Collection name for QPU Time Requests")
	cmd.PersistentFlags().StringVar(&flagCollectionDrivers, "drivers-collection", DefaultDriversCollection, "Collection name for Drivers")
	cmd.PersistentFlags().StringVar(&flagCollectionEvents, "events-collection", DefaultEventsCollection, "Collection name for Events")
	cmd.PersistentFlags().StringVar(&flagCollectionThemes, "themes-collection", DefaultThemesCollection, "Collection name for Themes")
	cmd.PersistentFlags().StringVar(&flagCollectionCalibrationResults, "calibration-results-collection", DefaultCalibrationResultsCollection, "Collection name for Calibration Results")
	cmd.PersistentFlags().StringVar(&flagCollectionCalibrationRequests, "calibration-requests-collection", DefaultCalibrationRequestsCollection, "Collection name for queued Calibration Requests")
	cmd.PersistentFlags().DurationVar(&flagIdleThreshold, "idle-threshold", 5*time.Second, "Idle fallback threshold")
	cmd.PersistentFlags().DurationVar(&flagRecoveryInterval, "recovery-interval", 10*time.Second, "Stale job recovery check interval")
	cmd.PersistentFlags().DurationVar(&flagJobTimeout, "job-timeout", 20*time.Second, "Stale job execution timeout")
	cmd.PersistentFlags().DurationVar(&flagDispatchPollInterval, "dispatch-poll-interval", 1*time.Second, "Dispatch poll interval")
	cmd.PersistentFlags().DurationVar(&flagEventsRetention, "events-retention", 720*time.Hour, "How long to keep events log entries before pruning (driver framework); 0 disables pruning")
	cmd.PersistentFlags().DurationVar(&flagEventsPruneInterval, "events-prune-interval", 1*time.Hour, "How often the events log retention prune runs (driver framework)")
	cmd.PersistentFlags().DurationVar(&flagCalibrationRequestRetention, "calibration-request-retention", 720*time.Hour, "How long to keep finished calibration requests; 0 disables pruning. A running one is never pruned")
	cmd.PersistentFlags().DurationVar(&flagCalibrationResultRetention, "calibration-result-retention", 0, "How long to keep calibration reports; 0 (the default) never prunes one — a report is the chip's history")
	cmd.PersistentFlags().DurationVar(&flagCalibrationFitRetention, "calibration-fit-retention", 720*time.Hour, "How long to keep a report's fit summaries and benchmark raw data, stripped without touching the rest; 0 disables")
	cmd.PersistentFlags().IntVar(&flagEventRateLimit, "event-rate-limit", 100, "Max inbound events per second accepted from each driver; 0 disables the limit")
	cmd.PersistentFlags().IntVar(&flagPortRangeStart, "port-range-start", 6000, "NNG port range start")
	cmd.PersistentFlags().IntVar(&flagPortRangeEnd, "port-range-end", 7000, "NNG port range end")
	cmd.PersistentFlags().BoolVar(&flagDisableEmailPasswordAuth, "disable-email-password-auth", false, "Disable email/password auth on users collection")
	cmd.PersistentFlags().StringVar(&flagOAuth2Providers, "oauth2-providers", "", "JSON array of OAuth2 providers: '[{\"name\":\"github\",\"clientId\":\"...\",\"clientSecret\":\"...\",\"authURL\":\"...\",\"tokenURL\":\"...\",\"userInfoURL\":\"...\",\"displayName\":\"...\",\"pkce\":true}]'")
}

// NewDefaultAppConfig returns an AppConfig populated with default collection names and settings.
func NewDefaultAppConfig() *AppConfig {
	return &AppConfig{
		CollectionQPUs:                DefaultQpusCollection,
		CollectionTimeSlots:           DefaultTimeSlotsCollection,
		CollectionQuantumJobs:         DefaultQuantumJobsCollection,
		CollectionAPITokens:           DefaultAPITokensCollection,
		CollectionQPUTimeRequests:     DefaultQPUTimeRequestsCollection,
		CollectionNotifications:       DefaultNotificationsCollection,
		CollectionDrivers:             DefaultDriversCollection,
		CollectionEvents:              DefaultEventsCollection,
		CollectionThemes:              DefaultThemesCollection,
		CollectionCalibrationResults:  DefaultCalibrationResultsCollection,
		CollectionCalibrationRequests: DefaultCalibrationRequestsCollection,
		IdleThreshold:                 5 * time.Second,
		RecoveryInterval:              10 * time.Second,
		JobTimeout:                    20 * time.Second,
		DispatchPollInterval:          1 * time.Second,
		EventsRetention:               720 * time.Hour,
		EventsPruneInterval:           1 * time.Hour,
		CalibrationRequestRetention:   720 * time.Hour,
		CalibrationResultRetention:    0,
		CalibrationFitRetention:       720 * time.Hour,
		EventRateLimit:                100,
		PortRangeStart:                6000,
		PortRangeEnd:                  7000,
		ServerPort:                    8090,
		DisableEmailPasswordAuth:      false,
		Validator:                     validator.New(validator.WithRequiredStructEnabled()),
		TlsCertFile:                   DefaultTLSCertFile,
		TlsKeyFile:                    DefaultTLSKeyFile,
		TlsCaCertFile:                 DefaultTLSCaCertFile,
		TlsCaKeyFile:                  DefaultTLSCaKeyFile,
		IpAddr:                        "127.0.0.1",
		activeTheme:                   DefaultThemeSchema,
	}
}

// NewFromFlags builds an AppConfig from the parsed CLI flags, optional config file, and environment variables.
// It enforces the precedence: CLI Flag (explicit) > Env Var > Config File > Default.
func NewFromFlags(cmd *cobra.Command) (*AppConfig, error) {
	if cmd != nil {
		// PocketBase's OnBootstrap hook runs before the cobra command parses flags.
		// So we need to manually parse them first ignoring unknown flags to get the CLI supplied values.
		cmd.PersistentFlags().ParseErrorsAllowlist.UnknownFlags = true
		_ = cmd.PersistentFlags().Parse(os.Args[1:])
	}

	cfg := NewDefaultAppConfig()

	// Overlay Config File (if specified via env or flag)
	configFile := flagConfigFile
	if envVal := os.Getenv("QPI_CONFIG_FILE"); envVal != "" {
		configFile = envVal
	}
	if configFile != "" && fileExists(configFile) {
		data, err := os.ReadFile(configFile)
		if err != nil {
			return nil, fmt.Errorf("error reading config file %s: %w", configFile, err)

		}
		type oauth2ProviderConfigLocal struct {
			Name         string         `json:"name" yaml:"name"`
			ClientId     string         `json:"clientId" yaml:"clientId"`
			ClientSecret string         `json:"clientSecret" yaml:"clientSecret"`
			AuthURL      string         `json:"authURL" yaml:"authURL"`
			TokenURL     string         `json:"tokenURL" yaml:"tokenURL"`
			UserInfoURL  string         `json:"userInfoURL" yaml:"userInfoURL"`
			DisplayName  string         `json:"displayName" yaml:"displayName"`
			PKCE         *bool          `json:"pkce" yaml:"pkce"`
			Extra        map[string]any `json:"extra" yaml:"extra"`
		}
		var fileCfg struct {
			CollectionQPUs                *string                     `json:"qpusCollection" yaml:"qpusCollection"`
			TlsCertFile                   *string                     `json:"tlsCertFile" yaml:"tlsCertFile"`
			TlsKeyFile                    *string                     `json:"tlsKeyFile" yaml:"tlsKeyFile"`
			TlsCaCertFile                 *string                     `json:"tlsCaCertFile" yaml:"tlsCaCertFile"`
			TlsCaKeyFile                  *string                     `json:"tlsCaKeyFile" yaml:"tlsCaKeyFile"`
			ServerPort                    *int                        `json:"serverPort" yaml:"serverPort"`
			IpAddr                        *string                     `json:"ipAddr" yaml:"ipAddr"`
			CollectionTimeSlots           *string                     `json:"timeslotsCollection" yaml:"timeslotsCollection"`
			CollectionQuantumJobs         *string                     `json:"jobsCollection" yaml:"jobsCollection"`
			CollectionAPITokens           *string                     `json:"apiTokensCollection" yaml:"apiTokensCollection"`
			CollectionNotifications       *string                     `json:"notificationsCollection" yaml:"notificationsCollection"`
			CollectionDrivers             *string                     `json:"driversCollection" yaml:"driversCollection"`
			CollectionEvents              *string                     `json:"eventsCollection" yaml:"eventsCollection"`
			CollectionThemes              *string                     `json:"themesCollection" yaml:"themesCollection"`
			CollectionCalibrationResults  *string                     `json:"calibrationResultsCollection" yaml:"calibrationResultsCollection"`
			CollectionCalibrationRequests *string                     `json:"calibrationRequestsCollection" yaml:"calibrationRequestsCollection"`
			IdleThreshold                 *string                     `json:"idleThreshold" yaml:"idleThreshold"`
			RecoveryInterval              *string                     `json:"recoveryInterval" yaml:"recoveryInterval"`
			JobTimeout                    *string                     `json:"jobTimeout" yaml:"jobTimeout"`
			DispatchPollInterval          *string                     `json:"dispatchPollInterval" yaml:"dispatchPollInterval"`
			EventsRetention               *string                     `json:"eventsRetention" yaml:"eventsRetention"`
			EventsPruneInterval           *string                     `json:"eventsPruneInterval" yaml:"eventsPruneInterval"`
			CalibrationRequestRetention   *string                     `json:"calibrationRequestRetention" yaml:"calibrationRequestRetention"`
			CalibrationResultRetention    *string                     `json:"calibrationResultRetention" yaml:"calibrationResultRetention"`
			CalibrationFitRetention       *string                     `json:"calibrationFitRetention" yaml:"calibrationFitRetention"`
			EventRateLimit                *int                        `json:"eventRateLimit" yaml:"eventRateLimit"`
			PortRangeStart                *int                        `json:"portRangeStart" yaml:"portRangeStart"`
			PortRangeEnd                  *int                        `json:"portRangeEnd" yaml:"portRangeEnd"`
			DisableEmailPasswordAuth      *bool                       `json:"disableEmailPasswordAuth" yaml:"disableEmailPasswordAuth"`
			OAuth2Providers               []oauth2ProviderConfigLocal `json:"oauth2Providers" yaml:"oauth2Providers"`
		}

		var parseErr error
		isYaml := strings.HasSuffix(configFile, ".yaml") || strings.HasSuffix(configFile, ".yml")
		if isYaml {
			parseErr = yaml.Unmarshal(data, &fileCfg)
		} else {
			parseErr = json.Unmarshal(data, &fileCfg)
		}

		if parseErr != nil {
			return nil, fmt.Errorf("error parsing file %s: %w", configFile, parseErr)
		}

		if fileCfg.TlsCertFile != nil {
			cfg.TlsCertFile = *fileCfg.TlsCertFile
		}
		if fileCfg.TlsKeyFile != nil {
			cfg.TlsKeyFile = *fileCfg.TlsKeyFile
		}
		if fileCfg.TlsCaCertFile != nil {
			cfg.TlsCaCertFile = *fileCfg.TlsCaCertFile
		}
		if fileCfg.TlsCaKeyFile != nil {
			cfg.TlsCaKeyFile = *fileCfg.TlsCaKeyFile
		}
		if fileCfg.ServerPort != nil {
			cfg.ServerPort = *fileCfg.ServerPort
		}
		if fileCfg.IpAddr != nil {
			if _, err := parseIpOrErr(*fileCfg.IpAddr); err == nil {
				cfg.IpAddr = *fileCfg.IpAddr
			}
		}
		if fileCfg.CollectionQPUs != nil {
			cfg.CollectionQPUs = *fileCfg.CollectionQPUs
		}
		if fileCfg.CollectionTimeSlots != nil {
			cfg.CollectionTimeSlots = *fileCfg.CollectionTimeSlots
		}
		if fileCfg.CollectionQuantumJobs != nil {
			cfg.CollectionQuantumJobs = *fileCfg.CollectionQuantumJobs
		}
		if fileCfg.CollectionAPITokens != nil {
			cfg.CollectionAPITokens = *fileCfg.CollectionAPITokens
		}
		if fileCfg.CollectionNotifications != nil {
			cfg.CollectionNotifications = *fileCfg.CollectionNotifications
		}
		if fileCfg.CollectionDrivers != nil {
			cfg.CollectionDrivers = *fileCfg.CollectionDrivers
		}
		if fileCfg.CollectionEvents != nil {
			cfg.CollectionEvents = *fileCfg.CollectionEvents
		}
		if fileCfg.CollectionThemes != nil {
			cfg.CollectionThemes = *fileCfg.CollectionThemes
		}
		if fileCfg.CollectionCalibrationResults != nil {
			cfg.CollectionCalibrationResults = *fileCfg.CollectionCalibrationResults
		}
		if fileCfg.CollectionCalibrationRequests != nil {
			cfg.CollectionCalibrationRequests = *fileCfg.CollectionCalibrationRequests
		}
		if fileCfg.IdleThreshold != nil {
			if d, err := time.ParseDuration(*fileCfg.IdleThreshold); err == nil {
				cfg.IdleThreshold = d
			}
		}
		if fileCfg.RecoveryInterval != nil {
			if d, err := time.ParseDuration(*fileCfg.RecoveryInterval); err == nil {
				cfg.RecoveryInterval = d
			}
		}
		if fileCfg.JobTimeout != nil {
			if d, err := time.ParseDuration(*fileCfg.JobTimeout); err == nil {
				cfg.JobTimeout = d
			}
		}
		if fileCfg.DispatchPollInterval != nil {
			if d, err := time.ParseDuration(*fileCfg.DispatchPollInterval); err == nil {
				cfg.DispatchPollInterval = d
			}
		}
		if fileCfg.EventsRetention != nil {
			if d, err := time.ParseDuration(*fileCfg.EventsRetention); err == nil {
				cfg.EventsRetention = d
			}
		}
		if fileCfg.EventsPruneInterval != nil {
			if d, err := time.ParseDuration(*fileCfg.EventsPruneInterval); err == nil {
				cfg.EventsPruneInterval = d
			}
		}
		if fileCfg.CalibrationRequestRetention != nil {
			if d, err := time.ParseDuration(*fileCfg.CalibrationRequestRetention); err == nil {
				cfg.CalibrationRequestRetention = d
			}
		}
		if fileCfg.CalibrationResultRetention != nil {
			if d, err := time.ParseDuration(*fileCfg.CalibrationResultRetention); err == nil {
				cfg.CalibrationResultRetention = d
			}
		}
		if fileCfg.CalibrationFitRetention != nil {
			if d, err := time.ParseDuration(*fileCfg.CalibrationFitRetention); err == nil {
				cfg.CalibrationFitRetention = d
			}
		}
		if fileCfg.EventRateLimit != nil {
			cfg.EventRateLimit = *fileCfg.EventRateLimit
		}
		if fileCfg.PortRangeStart != nil {
			cfg.PortRangeStart = *fileCfg.PortRangeStart
		}
		if fileCfg.PortRangeEnd != nil {
			cfg.PortRangeEnd = *fileCfg.PortRangeEnd
		}
		if fileCfg.DisableEmailPasswordAuth != nil {
			cfg.DisableEmailPasswordAuth = *fileCfg.DisableEmailPasswordAuth
		}
		if len(fileCfg.OAuth2Providers) > 0 {
			cfg.OAuth2Providers = make([]core.OAuth2ProviderConfig, len(fileCfg.OAuth2Providers))
			for i, p := range fileCfg.OAuth2Providers {
				cfg.OAuth2Providers[i] = core.OAuth2ProviderConfig{
					Name:         p.Name,
					ClientId:     p.ClientId,
					ClientSecret: p.ClientSecret,
					AuthURL:      p.AuthURL,
					TokenURL:     p.TokenURL,
					UserInfoURL:  p.UserInfoURL,
					DisplayName:  p.DisplayName,
					PKCE:         p.PKCE,
					Extra:        p.Extra,
				}
			}
		}

	}

	// Helper resolution functions to check precedence: CLI Changed > Env Set > Config File / Default
	resolveString := func(flagName, envName, current string) string {
		if cmd != nil && cmd.PersistentFlags().Changed(flagName) {
			val, _ := cmd.PersistentFlags().GetString(flagName)
			return val
		}
		if envVal := os.Getenv(envName); envVal != "" {
			return envVal
		}
		return current
	}

	resolveDuration := func(flagName, envName string, current time.Duration) time.Duration {
		if cmd != nil && cmd.PersistentFlags().Changed(flagName) {
			val, _ := cmd.PersistentFlags().GetDuration(flagName)
			return val
		}
		if envVal := os.Getenv(envName); envVal != "" {
			if d, err := time.ParseDuration(envVal); err == nil {
				return d
			}
		}
		return current
	}

	resolveInt := func(flagName, envName string, current int) int {
		if cmd != nil && cmd.PersistentFlags().Changed(flagName) {
			val, _ := cmd.PersistentFlags().GetInt(flagName)
			return val
		}
		if envVal := os.Getenv(envName); envVal != "" {
			if val, err := strconv.Atoi(envVal); err == nil {
				return val
			}
		}
		return current
	}

	resolveBool := func(flagName, envName string, current bool) bool {
		if cmd != nil && cmd.PersistentFlags().Changed(flagName) {
			val, _ := cmd.PersistentFlags().GetBool(flagName)
			return val
		}
		if envVal := os.Getenv(envName); envVal != "" {
			if val, err := strconv.ParseBool(envVal); err == nil {
				return val
			}
		}
		return current
	}

	// Overlay Env and CLI precedence
	cfg.TlsCertFile = resolveString("tls-cert-file", "QPI_TLS_CERT_FILE", cfg.TlsCertFile)
	cfg.TlsKeyFile = resolveString("tls-key-file", "QPI_TLS_KEY_FILE", cfg.TlsKeyFile)
	cfg.TlsCaCertFile = resolveString("tls-ca-cert-file", "QPI_TLS_CA_CERT_FILE", cfg.TlsCaCertFile)
	cfg.TlsCaKeyFile = resolveString("tls-ca-key-file", "QPI_TLS_CA_KEY_FILE", cfg.TlsCaKeyFile)
	cfg.ServerPort = resolveInt("server-port", "QPI_SERVER_PORT", cfg.ServerPort)
	cfg.IpAddr = resolveString("ip-addr", "QPI_IP_ADDR", cfg.IpAddr)
	cfg.CollectionQPUs = resolveString("qpus-collection", "QPI_QPUS_COLLECTION", cfg.CollectionQPUs)
	cfg.CollectionTimeSlots = resolveString("timeslots-collection", "QPI_TIMESLOTS_COLLECTION", cfg.CollectionTimeSlots)
	cfg.CollectionQuantumJobs = resolveString("jobs-collection", "QPI_JOBS_COLLECTION", cfg.CollectionQuantumJobs)
	cfg.CollectionAPITokens = resolveString("api-tokens-collection", "QPI_API_TOKENS_COLLECTION", cfg.CollectionAPITokens)
	cfg.CollectionNotifications = resolveString("notifications-collection", "QPI_NOTIFICATIONS_COLLECTION", cfg.CollectionNotifications)
	cfg.CollectionQPUTimeRequests = resolveString("qpu-time-requests-collection", "QPI_QPU_TIME_REQUESTS_COLLECTION", cfg.CollectionQPUTimeRequests)
	cfg.CollectionDrivers = resolveString("drivers-collection", "QPI_DRIVERS_COLLECTION", cfg.CollectionDrivers)
	cfg.CollectionEvents = resolveString("events-collection", "QPI_EVENTS_COLLECTION", cfg.CollectionEvents)
	cfg.CollectionThemes = resolveString("themes-collection", "QPI_THEMES_COLLECTION", cfg.CollectionThemes)
	cfg.CollectionCalibrationResults = resolveString("calibration-results-collection", "QPI_CALIBRATION_RESULTS_COLLECTION", cfg.CollectionCalibrationResults)
	cfg.CollectionCalibrationRequests = resolveString("calibration-requests-collection", "QPI_CALIBRATION_REQUESTS_COLLECTION", cfg.CollectionCalibrationRequests)
	cfg.IdleThreshold = resolveDuration("idle-threshold", "QPI_IDLE_THRESHOLD", cfg.IdleThreshold)
	cfg.RecoveryInterval = resolveDuration("recovery-interval", "QPI_RECOVERY_INTERVAL", cfg.RecoveryInterval)
	cfg.JobTimeout = resolveDuration("job-timeout", "QPI_JOB_TIMEOUT", cfg.JobTimeout)
	cfg.DispatchPollInterval = resolveDuration("dispatch-poll-interval", "QPI_DISPATCH_POLL_INTERVAL", cfg.DispatchPollInterval)
	cfg.EventsRetention = resolveDuration("events-retention", "QPI_EVENTS_RETENTION", cfg.EventsRetention)
	cfg.EventsPruneInterval = resolveDuration("events-prune-interval", "QPI_EVENTS_PRUNE_INTERVAL", cfg.EventsPruneInterval)
	cfg.CalibrationRequestRetention = resolveDuration("calibration-request-retention", "QPI_CALIBRATION_REQUEST_RETENTION", cfg.CalibrationRequestRetention)
	cfg.CalibrationResultRetention = resolveDuration("calibration-result-retention", "QPI_CALIBRATION_RESULT_RETENTION", cfg.CalibrationResultRetention)
	cfg.CalibrationFitRetention = resolveDuration("calibration-fit-retention", "QPI_CALIBRATION_FIT_RETENTION", cfg.CalibrationFitRetention)
	cfg.EventRateLimit = resolveInt("event-rate-limit", "QPI_EVENT_RATE_LIMIT", cfg.EventRateLimit)
	cfg.PortRangeStart = resolveInt("port-range-start", "QPI_PORT_RANGE_START", cfg.PortRangeStart)
	cfg.PortRangeEnd = resolveInt("port-range-end", "QPI_PORT_RANGE_END", cfg.PortRangeEnd)
	cfg.DisableEmailPasswordAuth = resolveBool("disable-email-password-auth", "QPI_DISABLE_EMAIL_PASSWORD_AUTH", cfg.DisableEmailPasswordAuth)

	// Merge OAuth2 providers JSON from CLI or Env
	oauth2ProvidersRaw := ""
	if cmd != nil && cmd.Flags().Changed("oauth2-providers") {
		oauth2ProvidersRaw, _ = cmd.Flags().GetString("oauth2-providers")
	} else if envVal := os.Getenv("QPI_OAUTH2_PROVIDERS"); envVal != "" {
		oauth2ProvidersRaw = envVal
	}
	if oauth2ProvidersRaw != "" {
		var providers []core.OAuth2ProviderConfig
		err := json.Unmarshal([]byte(oauth2ProvidersRaw), &providers)
		if err != nil {
			return nil, fmt.Errorf("failed to parse OAuth2 providers: %w", err)
		}

		for _, p := range providers {
			found := false
			for i, existing := range cfg.OAuth2Providers {
				if existing.Name == p.Name {
					cfg.OAuth2Providers[i] = p
					found = true
					break
				}
			}
			if !found {
				cfg.OAuth2Providers = append(cfg.OAuth2Providers, p)
			}
		}

	}

	// Load the TLS CA
	var err error
	cfg.tlsCaConfig, err = getCA(cfg.TlsCaCertFile, cfg.TlsCaKeyFile)
	if err != nil {
		return nil, fmt.Errorf("error loading CA config: %w", err)
	}

	// Load the TLS
	cfg.tlsConfig, err = getTlsCertKeyPair(cfg.TlsCertFile, cfg.TlsKeyFile, cfg.DomainName, cfg.tlsCaConfig, cfg.IpAddr)
	if err != nil {
		return nil, fmt.Errorf("[Config] NewFromFlags error: %w", err)
	}

	// Refresh active TLS certificate
	err = cfg.refreshActiveTls()
	if err != nil {
		return nil, fmt.Errorf("[Config] NewFromFlags error: %w", err)
	}

	// Save the hash
	cfg.tlsCaHash, err = getCaCertHash(cfg.tlsCaConfig)
	if err != nil {
		return nil, fmt.Errorf("[Config] NewFromFlags error: %w", err)
	}
	log.Printf("[Config] Active CA Fingerprint: %s\n", cfg.tlsCaHash)

	return cfg, nil
}

// getEnvString reads the given env var or returns the fallback
func getEnvString(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// fileExists checks whether a file exists
func fileExists(path string) bool {
	if path == "" {
		return false
	}

	_, err := os.Stat(path)
	if err != nil {
		return !os.IsNotExist(err)
	}

	return true
}

// parseIp parsed the IP string, returning an error if invalid
func parseIpOrErr(value string) (net.IP, error) {
	parsedIP := net.ParseIP(value)
	if parsedIP == nil {
		return nil, fmt.Errorf("invalid IP address: %s", value)
	}

	return parsedIP, nil
}
