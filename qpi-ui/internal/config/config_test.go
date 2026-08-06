package config

import (
	"os"
	"testing"
	"time"

	"github.com/spf13/cobra"
)

func TestNewFromFlags_Yaml(t *testing.T) {
	// Create a temporary YAML config file
	tmpFile, err := os.CreateTemp("", "qpi_config_*.yaml")
	if err != nil {
		t.Fatalf("failed to create temp file: %v", err)
	}
	defer os.Remove(tmpFile.Name())

	tmpDir := t.TempDir()
	yamlContent := `
jobsCollection: "yaml_quantum_jobs"
portRangeStart: 6005
portRangeEnd: 7005
idleThreshold: "12s"
ipAddr: "127.0.0.1"
disableEmailPasswordAuth: true
tlsCertFile: "` + tmpDir + `/test.cert.pem"
tlsKeyFile: "` + tmpDir + `/test.key"
tlsCaCertFile: "` + tmpDir + `/test.ca.pem"
tlsCaKeyFile: "` + tmpDir + `/test.ca.key"
oauth2Providers:
  - name: "github"
    clientId: "test_client_id"
    clientSecret: "test_client_secret"
    displayName: "GitHub Custom"
    pkce: true
`
	if _, err := tmpFile.Write([]byte(yamlContent)); err != nil {
		t.Fatalf("failed to write to temp file: %v", err)
	}
	tmpFile.Close()

	// Bind flags to a dummy cobra command
	cmd := &cobra.Command{}
	BindFlags(cmd)

	// Set the config-file flag using the temp file
	if err := cmd.PersistentFlags().Set("config-file", tmpFile.Name()); err != nil {
		t.Fatalf("failed to set config-file flag: %v", err)
	}

	// Load configuration
	cfg, err := NewFromFlags(cmd)
	if err != nil {
		t.Fatalf("failed to load config from flags: %v", err)
	}

	// Verify loaded values
	if cfg.CollectionQuantumJobs != "yaml_quantum_jobs" {
		t.Errorf("expected CollectionQuantumJobs to be 'yaml_quantum_jobs', got '%s'", cfg.CollectionQuantumJobs)
	}
	if cfg.PortRangeStart != 6005 {
		t.Errorf("expected PortRangeStart to be 6005, got %d", cfg.PortRangeStart)
	}
	if cfg.PortRangeEnd != 7005 {
		t.Errorf("expected PortRangeEnd to be 7005, got %d", cfg.PortRangeEnd)
	}
	if cfg.IdleThreshold != 12*time.Second {
		t.Errorf("expected IdleThreshold to be 12s, got %v", cfg.IdleThreshold)
	}
	if !cfg.DisableEmailPasswordAuth {
		t.Errorf("expected DisableEmailPasswordAuth to be true, got false")
	}
	if len(cfg.OAuth2Providers) != 1 {
		t.Fatalf("expected 1 OAuth2 provider, got %d", len(cfg.OAuth2Providers))
	}

	p := cfg.OAuth2Providers[0]
	if p.Name != "github" {
		t.Errorf("expected provider name 'github', got '%s'", p.Name)
	}
	if p.ClientId != "test_client_id" {
		t.Errorf("expected ClientId 'test_client_id', got '%s'", p.ClientId)
	}
	if p.ClientSecret != "test_client_secret" {
		t.Errorf("expected ClientSecret 'test_client_secret', got '%s'", p.ClientSecret)
	}
	if p.DisplayName != "GitHub Custom" {
		t.Errorf("expected DisplayName 'GitHub Custom', got '%s'", p.DisplayName)
	}
	if p.PKCE == nil || !*p.PKCE {
		t.Errorf("expected PKCE to be true")
	}
}

func TestNewFromFlags_Json(t *testing.T) {
	// Create a temporary JSON config file
	tmpFile, err := os.CreateTemp("", "qpi_config_*.json")
	if err != nil {
		t.Fatalf("failed to create temp file: %v", err)
	}
	defer os.Remove(tmpFile.Name())

	tmpDir := t.TempDir()
	jsonContent := `{
		"jobsCollection": "json_quantum_jobs",
		"portRangeStart": 8000,
		"ipAddr": "127.0.0.1",
		"tlsCertFile": "` + tmpDir + `/test.cert.pem",
		"tlsKeyFile": "` + tmpDir + `/test.key",
		"tlsCaCertFile": "` + tmpDir + `/test.ca.pem",
		"tlsCaKeyFile": "` + tmpDir + `/test.ca.key",
		"oauth2Providers": [
			{
				"name": "google",
				"clientId": "google_id",
				"clientSecret": "google_secret"
			}
		]
	}`
	if _, err := tmpFile.Write([]byte(jsonContent)); err != nil {
		t.Fatalf("failed to write to temp file: %v", err)
	}
	tmpFile.Close()

	// Bind flags to a dummy cobra command
	cmd := &cobra.Command{}
	BindFlags(cmd)

	// Set the config-file flag using the temp file
	if err := cmd.PersistentFlags().Set("config-file", tmpFile.Name()); err != nil {
		t.Fatalf("failed to set config-file flag: %v", err)
	}

	// Load configuration
	cfg, err := NewFromFlags(cmd)
	if err != nil {
		t.Fatalf("failed to load config from flags: %v", err)
	}

	// Verify loaded values
	if cfg.CollectionQuantumJobs != "json_quantum_jobs" {
		t.Errorf("expected CollectionQuantumJobs to be 'json_quantum_jobs', got '%s'", cfg.CollectionQuantumJobs)
	}
	if cfg.PortRangeStart != 8000 {
		t.Errorf("expected PortRangeStart to be 8000, got %d", cfg.PortRangeStart)
	}
	if len(cfg.OAuth2Providers) != 1 {
		t.Fatalf("expected 1 OAuth2 provider, got %d", len(cfg.OAuth2Providers))
	}

	p := cfg.OAuth2Providers[0]
	if p.Name != "google" {
		t.Errorf("expected provider name 'google', got '%s'", p.Name)
	}
	if p.ClientId != "google_id" {
		t.Errorf("expected ClientId 'google_id', got '%s'", p.ClientId)
	}
}

// TestNewFromFlags_TlsFromYaml verifies TLS settings loaded from YAML config file.
func TestNewFromFlags_TlsFromYaml(t *testing.T) {
	tmpDir := t.TempDir()
	configFile := tmpDir + "/qpi.config.yaml"

	// Use temp paths for TLS files so auto-generation works
	certFile := tmpDir + "/custom.cert.pem"
	keyFile := tmpDir + "/custom.key"
	caCertFile := tmpDir + "/custom.ca.pem"
	caKeyFile := tmpDir + "/custom.ca.key"

	yamlContent := `
tlsCertFile: "` + certFile + `"
tlsKeyFile: "` + keyFile + `"
tlsCaCertFile: "` + caCertFile + `"
tlsCaKeyFile: "` + caKeyFile + `"
ipAddr: "127.0.0.1"
serverPort: 8443
`
	if err := os.WriteFile(configFile, []byte(yamlContent), 0644); err != nil {
		t.Fatalf("failed to write config file: %v", err)
	}

	cmd := &cobra.Command{}
	BindFlags(cmd)
	if err := cmd.PersistentFlags().Set("config-file", configFile); err != nil {
		t.Fatalf("failed to set config-file flag: %v", err)
	}

	cfg, err := NewFromFlags(cmd)
	if err != nil {
		t.Fatalf("failed to load config from flags: %v", err)
	}

	if cfg.TlsCertFile != certFile {
		t.Errorf("expected TlsCertFile '%s', got '%s'", certFile, cfg.TlsCertFile)
	}
	if cfg.TlsKeyFile != keyFile {
		t.Errorf("expected TlsKeyFile '%s', got '%s'", keyFile, cfg.TlsKeyFile)
	}
	if cfg.TlsCaCertFile != caCertFile {
		t.Errorf("expected TlsCaCertFile '%s', got '%s'", caCertFile, cfg.TlsCaCertFile)
	}
	if cfg.TlsCaKeyFile != caKeyFile {
		t.Errorf("expected TlsCaKeyFile '%s', got '%s'", caKeyFile, cfg.TlsCaKeyFile)
	}
	if cfg.ServerPort != 8443 {
		t.Errorf("expected ServerPort 8443, got %d", cfg.ServerPort)
	}
}

// TestNewFromFlags_TlsFromEnv verifies TLS settings loaded from environment variables.
func TestNewFromFlags_TlsFromEnv(t *testing.T) {
	tmpDir := t.TempDir()
	emptyConfig := tmpDir + "/empty.yaml"
	if err := os.WriteFile(emptyConfig, []byte{}, 0644); err != nil {
		t.Fatalf("failed to create empty config: %v", err)
	}

	// Use temp paths so auto-generation works
	certFile := tmpDir + "/env.cert.pem"
	keyFile := tmpDir + "/env.key"
	caCertFile := tmpDir + "/env.ca.pem"
	caKeyFile := tmpDir + "/env.ca.key"

	t.Setenv("QPI_TLS_CERT_FILE", certFile)
	t.Setenv("QPI_TLS_KEY_FILE", keyFile)
	t.Setenv("QPI_TLS_CA_CERT_FILE", caCertFile)
	t.Setenv("QPI_TLS_CA_KEY_FILE", caKeyFile)
	t.Setenv("QPI_SERVER_PORT", "9443")
	t.Setenv("QPI_IP_ADDR", "127.0.0.1")
	t.Setenv("QPI_CONFIG_FILE", emptyConfig)

	cmd := &cobra.Command{}
	BindFlags(cmd)

	cfg, err := NewFromFlags(cmd)
	if err != nil {
		t.Fatalf("failed to load config from flags: %v", err)
	}

	if cfg.TlsCertFile != certFile {
		t.Errorf("expected TlsCertFile '%s', got '%s'", certFile, cfg.TlsCertFile)
	}
	if cfg.TlsKeyFile != keyFile {
		t.Errorf("expected TlsKeyFile '%s', got '%s'", keyFile, cfg.TlsKeyFile)
	}
	if cfg.TlsCaCertFile != caCertFile {
		t.Errorf("expected TlsCaCertFile '%s', got '%s'", caCertFile, cfg.TlsCaCertFile)
	}
	if cfg.TlsCaKeyFile != caKeyFile {
		t.Errorf("expected TlsCaKeyFile '%s', got '%s'", caKeyFile, cfg.TlsCaKeyFile)
	}
	if cfg.ServerPort != 9443 {
		t.Errorf("expected ServerPort 9443, got %d", cfg.ServerPort)
	}
}

// TestNewFromFlags_TlsFromFlags verifies TLS settings from CLI flags take highest precedence.
func TestNewFromFlags_TlsFromFlags(t *testing.T) {
	tmpDir := t.TempDir()
	emptyConfig := tmpDir + "/empty.yaml"
	if err := os.WriteFile(emptyConfig, []byte{}, 0644); err != nil {
		t.Fatalf("failed to create empty config: %v", err)
	}

	// Use temp paths so auto-generation works
	envCertFile := tmpDir + "/env.cert.pem"
	flagCertFile := tmpDir + "/flag.cert.pem"

	// Set env vars first
	t.Setenv("QPI_TLS_CERT_FILE", envCertFile)
	t.Setenv("QPI_SERVER_PORT", "9443")
	t.Setenv("QPI_CONFIG_FILE", emptyConfig)

	cmd := &cobra.Command{}
	BindFlags(cmd)

	// CLI flags should override env vars
	if err := cmd.PersistentFlags().Set("tls-cert-file", flagCertFile); err != nil {
		t.Fatalf("failed to set tls-cert-file flag: %v", err)
	}
	if err := cmd.PersistentFlags().Set("server-port", "10443"); err != nil {
		t.Fatalf("failed to set server-port flag: %v", err)
	}
	if err := cmd.PersistentFlags().Set("ip-addr", "127.0.0.1"); err != nil {
		t.Fatalf("failed to set ip-addr flag: %v", err)
	}

	cfg, err := NewFromFlags(cmd)
	if err != nil {
		t.Fatalf("failed to load config from flags: %v", err)
	}

	if cfg.TlsCertFile != flagCertFile {
		t.Errorf("expected TlsCertFile '%s', got '%s'", flagCertFile, cfg.TlsCertFile)
	}
	if cfg.ServerPort != 10443 {
		t.Errorf("expected ServerPort 10443, got %d", cfg.ServerPort)
	}
}

// TestNewFromFlags_TlsAutoGenerate verifies that TLS certificates are auto-generated
// when no existing files are present.
func TestNewFromFlags_TlsAutoGenerate(t *testing.T) {
	tmpDir := t.TempDir()

	certFile := tmpDir + "/test.cert.pem"
	keyFile := tmpDir + "/test.key"
	caCertFile := tmpDir + "/test.ca.pem"
	caKeyFile := tmpDir + "/test.ca.key"
	emptyConfig := tmpDir + "/empty.yaml"
	if err := os.WriteFile(emptyConfig, []byte{}, 0644); err != nil {
		t.Fatalf("failed to create empty config: %v", err)
	}

	cmd := &cobra.Command{}
	BindFlags(cmd)

	if err := cmd.PersistentFlags().Set("config-file", emptyConfig); err != nil {
		t.Fatalf("failed to set config-file flag: %v", err)
	}
	if err := cmd.PersistentFlags().Set("tls-cert-file", certFile); err != nil {
		t.Fatalf("failed to set tls-cert-file flag: %v", err)
	}
	if err := cmd.PersistentFlags().Set("tls-key-file", keyFile); err != nil {
		t.Fatalf("failed to set tls-key-file flag: %v", err)
	}
	if err := cmd.PersistentFlags().Set("tls-ca-cert-file", caCertFile); err != nil {
		t.Fatalf("failed to set tls-ca-cert-file flag: %v", err)
	}
	if err := cmd.PersistentFlags().Set("tls-ca-key-file", caKeyFile); err != nil {
		t.Fatalf("failed to set tls-ca-key-file flag: %v", err)
	}
	if err := cmd.PersistentFlags().Set("ip-addr", "127.0.0.1"); err != nil {
		t.Fatalf("failed to set ip-addr flag: %v", err)
	}
	if err := cmd.PersistentFlags().Set("domain", "test.local"); err != nil {
		t.Fatalf("failed to set domain flag: %v", err)
	}

	cfg, err := NewFromFlags(cmd)
	if err != nil {
		t.Fatalf("failed to load config from flags: %v", err)
	}

	// Verify files were auto-generated
	if _, err := os.Stat(certFile); err != nil {
		t.Errorf("expected cert file to be auto-generated: %v", err)
	}
	if _, err := os.Stat(keyFile); err != nil {
		t.Errorf("expected key file to be auto-generated: %v", err)
	}
	if _, err := os.Stat(caCertFile); err != nil {
		t.Errorf("expected CA cert file to be auto-generated: %v", err)
	}
	if _, err := os.Stat(caKeyFile); err != nil {
		t.Errorf("expected CA key file to be auto-generated: %v", err)
	}

	// Verify TLS config is loaded
	if cfg.GetTlsConfig() == nil {
		t.Fatal("expected GetTlsConfig() to return non-nil")
	}

	// Verify CA hash is set
	if cfg.GetTlsCaHash() == "" {
		t.Fatal("expected GetTlsCaHash() to return non-empty string")
	}

	// Verify CA config is loaded
	if cfg.GetTlsCaConfig() == nil {
		t.Fatal("expected GetTlsCaConfig() to return non-nil")
	}
}

// TestNewFromFlags_TlsReuseExisting verifies that existing valid TLS files are reused.
func TestNewFromFlags_TlsReuseExisting(t *testing.T) {
	tmpDir := t.TempDir()

	certFile := tmpDir + "/test.cert.pem"
	keyFile := tmpDir + "/test.key"
	caCertFile := tmpDir + "/test.ca.pem"
	caKeyFile := tmpDir + "/test.ca.key"

	// Pre-generate CA and cert
	caPair, err := generateCA(caCertFile, caKeyFile)
	if err != nil {
		t.Fatalf("generateCA failed: %v", err)
	}
	err = generateCertAndKeyFiles("test.local", certFile, keyFile, caPair, "127.0.0.1")
	if err != nil {
		t.Fatalf("generateCertAndKeyFiles failed: %v", err)
	}

	cmd := &cobra.Command{}
	BindFlags(cmd)

	// Disable default config file
	if err := cmd.PersistentFlags().Set("config-file", ""); err != nil {
		t.Fatalf("failed to set config-file flag: %v", err)
	}
	if err := cmd.PersistentFlags().Set("tls-cert-file", certFile); err != nil {
		t.Fatalf("failed to set tls-cert-file flag: %v", err)
	}
	if err := cmd.PersistentFlags().Set("tls-key-file", keyFile); err != nil {
		t.Fatalf("failed to set tls-key-file flag: %v", err)
	}
	if err := cmd.PersistentFlags().Set("tls-ca-cert-file", caCertFile); err != nil {
		t.Fatalf("failed to set tls-ca-cert-file flag: %v", err)
	}
	if err := cmd.PersistentFlags().Set("tls-ca-key-file", caKeyFile); err != nil {
		t.Fatalf("failed to set tls-ca-key-file flag: %v", err)
	}
	if err := cmd.PersistentFlags().Set("ip-addr", "127.0.0.1"); err != nil {
		t.Fatalf("failed to set ip-addr flag: %v", err)
	}

	cfg, err := NewFromFlags(cmd)
	if err != nil {
		t.Fatalf("failed to load config from flags: %v", err)
	}

	if cfg.GetTlsConfig() == nil {
		t.Fatal("expected GetTlsConfig() to return non-nil")
	}
	if cfg.GetTlsCaHash() == "" {
		t.Fatal("expected GetTlsCaHash() to return non-empty string")
	}
}

// TestAppConfig_GetCollectionName verifies collection name mapping.
func TestAppConfig_GetCollectionName(t *testing.T) {
	cfg := &AppConfig{
		CollectionQPUs:            "custom_qpus",
		CollectionTimeSlots:       "custom_timeslots",
		CollectionQuantumJobs:     "custom_jobs",
		CollectionAPITokens:       "custom_tokens",
		CollectionQPUTimeRequests: "custom_requests",
		CollectionNotifications:   "custom_notifications",
		CollectionDrivers:         "custom_drivers",
	}

	tests := []struct {
		input    string
		expected string
	}{
		{DefaultQpusCollection, "custom_qpus"},
		{DefaultTimeSlotsCollection, "custom_timeslots"},
		{DefaultQuantumJobsCollection, "custom_jobs"},
		{DefaultAPITokensCollection, "custom_tokens"},
		{DefaultQPUTimeRequestsCollection, "custom_requests"},
		{DefaultNotificationsCollection, "custom_notifications"},
		{DefaultDriversCollection, "custom_drivers"},
		{"unknown", "unknown"},
	}

	for _, tt := range tests {
		got := cfg.GetCollectionName(tt.input)
		if got != tt.expected {
			t.Errorf("GetCollectionName(%q) = %q, want %q", tt.input, got, tt.expected)
		}
	}
}

// TestAppConfig_GetDefaultCollectionName verifies reverse collection name mapping.
func TestAppConfig_GetDefaultCollectionName(t *testing.T) {
	cfg := &AppConfig{
		CollectionQPUs:            "custom_qpus",
		CollectionTimeSlots:       "custom_timeslots",
		CollectionQuantumJobs:     "custom_jobs",
		CollectionAPITokens:       "custom_tokens",
		CollectionQPUTimeRequests: "custom_requests",
		CollectionNotifications:   "custom_notifications",
		CollectionDrivers:         "custom_drivers",
	}

	tests := []struct {
		input    string
		expected string
	}{
		{"custom_qpus", DefaultQpusCollection},
		{"custom_timeslots", DefaultTimeSlotsCollection},
		{"custom_jobs", DefaultQuantumJobsCollection},
		{"custom_tokens", DefaultAPITokensCollection},
		{"custom_requests", DefaultQPUTimeRequestsCollection},
		{"custom_notifications", DefaultNotificationsCollection},
		{"custom_drivers", DefaultDriversCollection},
		{"unknown", "unknown"},
	}

	for _, tt := range tests {
		got := cfg.GetDefaultCollectionName(tt.input)
		if got != tt.expected {
			t.Errorf("GetDefaultCollectionName(%q) = %q, want %q", tt.input, got, tt.expected)
		}
	}
}

// setupTLSEnv points the TLS env vars at a temp dir so NewFromFlags can
// auto-generate certs, and returns an empty config file path.
func setupTLSEnv(t *testing.T, prefix string) string {
	t.Helper()
	tmpDir := t.TempDir()
	emptyConfig := tmpDir + "/empty.yaml"
	if err := os.WriteFile(emptyConfig, []byte{}, 0644); err != nil {
		t.Fatalf("failed to create empty config: %v", err)
	}
	t.Setenv("QPI_TLS_CERT_FILE", tmpDir+"/"+prefix+".cert.pem")
	t.Setenv("QPI_TLS_KEY_FILE", tmpDir+"/"+prefix+".key")
	t.Setenv("QPI_TLS_CA_CERT_FILE", tmpDir+"/"+prefix+".ca.pem")
	t.Setenv("QPI_TLS_CA_KEY_FILE", tmpDir+"/"+prefix+".ca.key")
	t.Setenv("QPI_CONFIG_FILE", emptyConfig)
	return emptyConfig
}

// TestNewFromFlags_EventsOpsDefaults verifies the Phase 5 ops settings default
// to their sensible values (RFC 0001 §11).
func TestNewFromFlags_EventsOpsDefaults(t *testing.T) {
	setupTLSEnv(t, "eo")

	cmd := &cobra.Command{}
	BindFlags(cmd)

	cfg, err := NewFromFlags(cmd)
	if err != nil {
		t.Fatalf("failed to load config from flags: %v", err)
	}

	if cfg.EventsRetention != 720*time.Hour {
		t.Errorf("expected EventsRetention default 720h, got %s", cfg.EventsRetention)
	}
	if cfg.EventsPruneInterval != time.Hour {
		t.Errorf("expected EventsPruneInterval default 1h, got %s", cfg.EventsPruneInterval)
	}
	if cfg.EventRateLimit != 100 {
		t.Errorf("expected EventRateLimit default 100, got %d", cfg.EventRateLimit)
	}
	// The calibration path (RFC 0006 §9). Requests and traces are bookkeeping and
	// go by default; a report is the chip's history and never goes unless asked.
	if cfg.CalibrationRequestRetention != 720*time.Hour {
		t.Errorf("expected CalibrationRequestRetention default 720h, got %s", cfg.CalibrationRequestRetention)
	}
	if cfg.CalibrationFitRetention != 720*time.Hour {
		t.Errorf("expected CalibrationFitRetention default 720h, got %s", cfg.CalibrationFitRetention)
	}
	if cfg.CalibrationResultRetention != 0 {
		t.Errorf("expected CalibrationResultRetention disabled by default, got %s", cfg.CalibrationResultRetention)
	}
}

// TestNewFromFlags_EventsOpsPrecedence verifies the strict resolution order
// CLI flag > env var > config file for the Phase 5 ops settings.
func TestNewFromFlags_EventsOpsPrecedence(t *testing.T) {
	t.Run("from config file", func(t *testing.T) {
		tmpDir := t.TempDir()
		configFile := tmpDir + "/ops.yaml"
		yamlContent := `
eventsRetention: "48h"
eventsPruneInterval: "30m"
calibrationRequestRetention: "24h"
calibrationResultRetention: "8760h"
calibrationFitRetention: "1h"
eventRateLimit: 42
tlsCertFile: "` + tmpDir + `/c.cert.pem"
tlsKeyFile: "` + tmpDir + `/c.key"
tlsCaCertFile: "` + tmpDir + `/c.ca.pem"
tlsCaKeyFile: "` + tmpDir + `/c.ca.key"
ipAddr: "127.0.0.1"
`
		if err := os.WriteFile(configFile, []byte(yamlContent), 0644); err != nil {
			t.Fatalf("failed to write config file: %v", err)
		}

		cmd := &cobra.Command{}
		BindFlags(cmd)
		if err := cmd.PersistentFlags().Set("config-file", configFile); err != nil {
			t.Fatalf("failed to set config-file flag: %v", err)
		}

		cfg, err := NewFromFlags(cmd)
		if err != nil {
			t.Fatalf("failed to load config from flags: %v", err)
		}
		if cfg.EventsRetention != 48*time.Hour {
			t.Errorf("expected EventsRetention 48h from file, got %s", cfg.EventsRetention)
		}
		if cfg.EventsPruneInterval != 30*time.Minute {
			t.Errorf("expected EventsPruneInterval 30m from file, got %s", cfg.EventsPruneInterval)
		}
		if cfg.EventRateLimit != 42 {
			t.Errorf("expected EventRateLimit 42 from file, got %d", cfg.EventRateLimit)
		}
		if cfg.CalibrationRequestRetention != 24*time.Hour {
			t.Errorf("expected CalibrationRequestRetention 24h from file, got %s", cfg.CalibrationRequestRetention)
		}
		if cfg.CalibrationResultRetention != 8760*time.Hour {
			t.Errorf("expected CalibrationResultRetention 8760h from file, got %s", cfg.CalibrationResultRetention)
		}
		if cfg.CalibrationFitRetention != time.Hour {
			t.Errorf("expected CalibrationFitRetention 1h from file, got %s", cfg.CalibrationFitRetention)
		}
	})

	t.Run("env overrides config file", func(t *testing.T) {
		tmpDir := t.TempDir()
		configFile := tmpDir + "/ops_env.yaml"
		yamlContent := `
eventsRetention: "48h"
eventRateLimit: 42
tlsCertFile: "` + tmpDir + `/e.cert.pem"
tlsKeyFile: "` + tmpDir + `/e.key"
tlsCaCertFile: "` + tmpDir + `/e.ca.pem"
tlsCaKeyFile: "` + tmpDir + `/e.ca.key"
ipAddr: "127.0.0.1"
`
		if err := os.WriteFile(configFile, []byte(yamlContent), 0644); err != nil {
			t.Fatalf("failed to write config file: %v", err)
		}
		t.Setenv("QPI_CONFIG_FILE", configFile)
		t.Setenv("QPI_EVENTS_RETENTION", "12h")
		t.Setenv("QPI_EVENT_RATE_LIMIT", "7")
		t.Setenv("QPI_CALIBRATION_FIT_RETENTION", "2h")

		cmd := &cobra.Command{}
		BindFlags(cmd)

		cfg, err := NewFromFlags(cmd)
		if err != nil {
			t.Fatalf("failed to load config from flags: %v", err)
		}
		if cfg.EventsRetention != 12*time.Hour {
			t.Errorf("expected EventsRetention 12h from env, got %s", cfg.EventsRetention)
		}
		if cfg.EventRateLimit != 7 {
			t.Errorf("expected EventRateLimit 7 from env, got %d", cfg.EventRateLimit)
		}
		if cfg.CalibrationFitRetention != 2*time.Hour {
			t.Errorf("expected CalibrationFitRetention 2h from env, got %s", cfg.CalibrationFitRetention)
		}
	})

	t.Run("flag overrides env", func(t *testing.T) {
		setupTLSEnv(t, "fo")
		t.Setenv("QPI_EVENTS_RETENTION", "12h")
		t.Setenv("QPI_EVENT_RATE_LIMIT", "7")
		t.Setenv("QPI_CALIBRATION_REQUEST_RETENTION", "2h")

		cmd := &cobra.Command{}
		BindFlags(cmd)
		if err := cmd.PersistentFlags().Set("calibration-request-retention", "3h"); err != nil {
			t.Fatalf("failed to set calibration-request-retention flag: %v", err)
		}
		if err := cmd.PersistentFlags().Set("events-retention", "6h"); err != nil {
			t.Fatalf("failed to set events-retention flag: %v", err)
		}
		if err := cmd.PersistentFlags().Set("event-rate-limit", "3"); err != nil {
			t.Fatalf("failed to set event-rate-limit flag: %v", err)
		}

		cfg, err := NewFromFlags(cmd)
		if err != nil {
			t.Fatalf("failed to load config from flags: %v", err)
		}
		if cfg.EventsRetention != 6*time.Hour {
			t.Errorf("expected EventsRetention 6h from flag, got %s", cfg.EventsRetention)
		}
		if cfg.EventRateLimit != 3 {
			t.Errorf("expected EventRateLimit 3 from flag, got %d", cfg.EventRateLimit)
		}
		if cfg.CalibrationRequestRetention != 3*time.Hour {
			t.Errorf("expected CalibrationRequestRetention 3h from flag, got %s", cfg.CalibrationRequestRetention)
		}
	})
}
