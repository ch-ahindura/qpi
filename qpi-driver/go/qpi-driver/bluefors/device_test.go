package bluefors

import (
	"testing"
	"time"

	qpidriver "github.com/sopherapps/qpi/qpi-driver/go"
	"github.com/sopherapps/qpi/qpi-driver/go/devices"
)

// The device spec is in-package here so the built driver's unexported fields can
// be asserted on — which is the point of a builder that returns rather than runs:
// every option can be checked with no server, no socket and no polling.

func TestSpecDescribesItself(t *testing.T) {
	if DeviceSpec.Name != "bluefors_gen1" || DeviceSpec.Operation != devices.Monitor {
		t.Fatalf("expected a monitor device named bluefors_gen1, got %+v", DeviceSpec)
	}
	if DeviceSpec.Summary == "" || DeviceSpec.Build == nil {
		t.Error("expected a summary and a builder")
	}

	// The keys, types and defaults must match the Python SDK's bluefors_gen1
	// device: QPI-UI renders setup snippets from one catalog and an operator may
	// well run the other (RFC 0003 §9).
	want := map[string]string{
		"channels": "channels", "base_url": "str", "api_key": "str",
		"poll_interval": "float", "timeout": "float",
	}
	got := map[string]string{}
	for _, option := range DeviceSpec.Options {
		got[option.Key] = option.Type
		if option.Help == "" {
			t.Errorf("expected %q to have help text", option.Key)
		}
	}
	if len(got) != len(want) {
		t.Fatalf("expected options %v, got %v", want, got)
	}
	for key, kind := range want {
		if got[key] != kind {
			t.Errorf("expected %q to be %q, got %q", key, kind, got[key])
		}
	}
}

func TestBuildFromOptionsReadsEveryKey(t *testing.T) {
	opts, err := DeviceSpec.ParseOptions(map[string]string{
		"channels":      "mapper.bf.tmc:K,mapper.bf.pmc:mbar",
		"base_url":      "http://cryo:49099/",
		"api_key":       "secret",
		"poll_interval": "2.5",
		"timeout":       "7",
	})
	if err != nil {
		t.Fatalf("expected the options to parse, got %v", err)
	}

	driver, err := DeviceSpec.Build(qpidriver.Config{Token: "tok"}, opts)
	if err != nil {
		t.Fatalf("expected the builder to succeed, got %v", err)
	}
	built, ok := driver.(*Driver)
	if !ok {
		t.Fatalf("expected a bluefors driver, got %T", driver)
	}

	if built.baseURL != "http://cryo:49099" {
		t.Errorf("expected the trailing slash trimmed, got %q", built.baseURL)
	}
	if built.apiKey != "secret" {
		t.Errorf("expected the api key to land, got %q", built.apiKey)
	}
	if built.pollInterval != 2500*time.Millisecond {
		t.Errorf("expected 2.5s between polls, got %v", built.pollInterval)
	}
	if built.timeout != 7*time.Second {
		t.Errorf("expected a 7s timeout, got %v", built.timeout)
	}
	if built.channels["mapper.bf.tmc"] != "K" || built.channels["mapper.bf.pmc"] != "mbar" {
		t.Errorf("expected the channels parsed with units, got %v", built.channels)
	}
}

func TestBuildFromOptionsDefaultsEveryKeyButChannels(t *testing.T) {
	// Only the channels are unknowable in advance — which mappers a given system
	// has is configuration — so only they are required.
	opts, err := DeviceSpec.ParseOptions(map[string]string{"channels": "mapper.bf.tmc"})
	if err != nil {
		t.Fatalf("expected the options to parse, got %v", err)
	}

	driver, err := DeviceSpec.Build(qpidriver.Config{}, opts)
	if err != nil {
		t.Fatalf("expected the builder to succeed, got %v", err)
	}
	built := driver.(*Driver)

	if built.baseURL != DefaultBaseURL {
		t.Errorf("expected the default base URL, got %q", built.baseURL)
	}
	if built.pollInterval != DefaultPollInterval || built.timeout != DefaultTimeout {
		t.Errorf("expected the default timings, got %v and %v", built.pollInterval, built.timeout)
	}
	if built.apiKey != "" {
		t.Errorf("expected no api key, got %q", built.apiKey)
	}
	if built.channels["mapper.bf.tmc"] != "" {
		t.Errorf("expected a channel with no unit, got %v", built.channels)
	}
}

func TestChannelsAreRequired(t *testing.T) {
	if _, err := DeviceSpec.ParseOptions(map[string]string{"base_url": "http://x"}); err == nil {
		t.Fatal("expected a missing channels option to fail")
	}
}

func TestBuildingStartsNothing(t *testing.T) {
	// The builder connects to nothing: qpidriver.Run is what starts a driver, so a
	// device can be built and asserted on in a test like this one (RFC 0003 §7).
	opts, err := DeviceSpec.ParseOptions(map[string]string{
		"channels":      "mapper.bf.tmc:K",
		"base_url":      "http://127.0.0.1:1",
		"poll_interval": "0.01",
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := DeviceSpec.Build(qpidriver.Config{}, opts); err != nil {
		t.Fatalf("expected building against an unreachable host to succeed, got %v", err)
	}
	// Nothing to stop, nothing to wait for: had Build connected or polled, this
	// test would hang or fail against port 1.
}
