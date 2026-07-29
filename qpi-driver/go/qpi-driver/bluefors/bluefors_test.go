package bluefors

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	qpidriver "github.com/sopherapps/qpi/qpi-driver/go"
)

func TestReadChannelParsesLatestValidValue(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/values/mapper/bf/tmc" {
			t.Errorf("unexpected path %q (dots should become slashes)", r.URL.Path)
		}
		_, _ = w.Write([]byte(`{"data":{"content":{"latest_valid_value":{"value":0.0123,"status":"OK"}}}}`))
	}))
	defer server.Close()

	d := New(Options{BaseURL: server.URL, Channels: map[string]string{"mapper.bf.tmc": "K"}})
	r := d.readChannel("mapper.bf.tmc", "K")

	if r.Status != "OK" || r.Unit != "K" || r.Value == nil || *r.Value != 0.0123 {
		t.Fatalf("unexpected reading: %+v", r)
	}
}

func TestReadChannelFallsBackToLatestValue(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"data":{"content":{"latest_value":{"value":"1.5","status":"STALE"}}}}`))
	}))
	defer server.Close()

	d := New(Options{BaseURL: server.URL, Channels: map[string]string{"mapper.bf.pmc": "mbar"}})
	r := d.readChannel("mapper.bf.pmc", "mbar")

	if r.Status != "STALE" || r.Value == nil || *r.Value != 1.5 {
		t.Fatalf("expected fallback to latest_value with string coercion, got %+v", r)
	}
}

func TestReadChannelErrorStatusOnFailure(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "boom", http.StatusInternalServerError)
	}))
	defer server.Close()

	d := New(Options{BaseURL: server.URL, Channels: map[string]string{"x": ""}})
	r := d.readChannel("x", "")

	if r.Status != "ERROR" || r.Value != nil {
		t.Fatalf("expected an ERROR reading with nil value, got %+v", r)
	}
}

func TestParseValueVariants(t *testing.T) {
	if v := parseValue([]byte("null")); v != nil {
		t.Errorf("null should parse to nil, got %v", *v)
	}
	if v := parseValue(nil); v != nil {
		t.Errorf("empty should parse to nil, got %v", *v)
	}
	if v := parseValue([]byte(`"not-a-number"`)); v != nil {
		t.Errorf("non-numeric string should parse to nil, got %v", *v)
	}
	if v := parseValue([]byte("42")); v == nil || *v != 42 {
		t.Errorf("expected 42, got %v", v)
	}
}

func TestParseChannels(t *testing.T) {
	got := ParseChannels("mapper.bf.tmc:K, mapper.bf.pmc:mbar ,bare")
	want := map[string]string{"mapper.bf.tmc": "K", "mapper.bf.pmc": "mbar", "bare": ""}
	if len(got) != len(want) {
		t.Fatalf("got %v, want %v", got, want)
	}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("channel %q = %q, want %q", k, got[k], v)
		}
	}
}

func TestHandleEventIgnoresInbound(t *testing.T) {
	d := New(Options{Channels: map[string]string{"x": "K"}})
	d.HandleEvent(qpidriver.NewEvent(qpidriver.JobDispatch, "qpu_1", nil)) // must not panic
}

func TestOptionsReadChannelReplacesTheRead(t *testing.T) {
	// The seam a monitor for different control software reuses this driver
	// through: Go has no inheritance, so a Gen. 2 supplies its own read and keeps
	// the timer, the per-channel error handling and the CryostatReading emit.
	value := 42.0
	var asked []string

	d := New(Options{
		Channels:     map[string]string{"gen2.temperature": "K"},
		PollInterval: time.Hour, // long enough that only the direct call below runs
		ReadChannel: func(channel, unit string) Reading {
			asked = append(asked, channel)
			return Reading{Value: &value, Unit: unit, Status: "OK"}
		},
	})
	r := d.read("gen2.temperature", "K")

	if len(asked) != 1 || asked[0] != "gen2.temperature" {
		t.Fatalf("expected the supplied reader to be asked, got %v", asked)
	}
	if r.Status != "OK" || r.Value == nil || *r.Value != 42.0 {
		t.Fatalf("expected the supplied reader's reading, got %+v", r)
	}
	// And the built-in read is untouched for a driver that does not replace it.
	if New(Options{Channels: map[string]string{"x": ""}}).read == nil {
		t.Error("expected a default read when ReadChannel is nil")
	}
}
