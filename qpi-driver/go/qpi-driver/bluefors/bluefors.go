// Package bluefors is the officially maintained Bluefors Gen. 1 cryostat
// monitor driver (RFC 0001 §7), the Go counterpart of the Python
// qpi_driver.builtins.bluefors_gen1 driver.
//
// Note the Gen. 1: Bluefors also ships a Gen. 2 Control Software with its own
// API, which is out of scope here and would need its own driver.
//
// It is a report-only driver: it never handles JobDispatch (it is not a QPU),
// it polls the Bluefors Remote Access Control API Gen. 1 "values" endpoint on
// a timer and emits a CryostatReading event with whatever it read.
//
// This package lives outside the base SDK and imports it, so a consumer only
// pulls it into their build if they actually use it — the Go equivalent of the
// Python `qpi-driver[bluefors_gen1]` extra.
package bluefors

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	qpidriver "github.com/sopherapps/qpi/qpi-driver/go"
)

const (
	// DefaultBaseURL is the Bluefors Control API endpoint on the cryostat host.
	DefaultBaseURL = "http://127.0.0.1:49099"
	// DefaultPollInterval is how often channels are read.
	DefaultPollInterval = 5 * time.Second
	// DefaultTimeout is the per-channel HTTP read timeout.
	DefaultTimeout = 5 * time.Second
)

// Options configures the Bluefors monitor. Channels maps a value-tree channel
// path (e.g. "mapper.bf.tmc") to a display unit (e.g. "K"), which the Bluefors
// basic read response does not itself report; an empty unit is fine.
type Options struct {
	// BaseURL is the Bluefors Control API base URL. Defaults to DefaultBaseURL.
	BaseURL string
	// Channels maps a value-tree channel path to its display unit.
	Channels map[string]string
	// APIKey is the optional Bluefors access key, sent as the "key" query
	// parameter (Bluefors reference §3.5.1).
	APIKey string
	// PollInterval is the time between polls. Defaults to DefaultPollInterval.
	PollInterval time.Duration
	// Timeout is the per-channel HTTP timeout. Defaults to DefaultTimeout.
	Timeout time.Duration
	// ReadChannel, when set, replaces how one channel is read.
	//
	// Go has no inheritance, so this is the seam a monitor for different control
	// software reuses this driver through — Bluefors Gen. 2, say, which has its own
	// API. Everything around the read stays: the timer, one bad channel not losing
	// the rest of the tick, and the CryostatReading event. Only the call to the
	// instrument changes. Nil means the built-in Gen. 1 read.
	//
	// It must not panic: a channel it cannot read is a nil Value with an "ERROR"
	// Status, which is how the caller knows to keep going.
	ReadChannel func(channel, unit string) Reading
}

// Driver polls Bluefors Gen. 1 Control API channels and emits readings on a
// timer. Embed nothing; construct it with New and pass it to qpidriver.Run.
type Driver struct {
	qpidriver.Base
	baseURL      string
	channels     map[string]string
	apiKey       string
	pollInterval time.Duration
	timeout      time.Duration
	client       *http.Client
	read         func(channel, unit string) Reading
}

// Reading is one channel's latest value in the emitted payload.
type Reading struct {
	Value  *float64 `json:"value"`
	Unit   string   `json:"unit"`
	Status string   `json:"status"`
}

// New builds a Bluefors monitor from its options. Zero-valued option fields
// fall back to sensible defaults. The returned driver is passed to
// qpidriver.Run to connect and start polling.
func New(opts Options) *Driver {
	if opts.BaseURL == "" {
		opts.BaseURL = DefaultBaseURL
	}
	if opts.PollInterval <= 0 {
		opts.PollInterval = DefaultPollInterval
	}
	if opts.Timeout <= 0 {
		opts.Timeout = DefaultTimeout
	}
	channels := make(map[string]string, len(opts.Channels))
	for path, unit := range opts.Channels {
		channels[path] = unit
	}

	d := &Driver{
		baseURL:      strings.TrimRight(opts.BaseURL, "/"),
		channels:     channels,
		apiKey:       opts.APIKey,
		pollInterval: opts.PollInterval,
		timeout:      opts.Timeout,
		client:       &http.Client{Timeout: opts.Timeout},
	}
	d.read = opts.ReadChannel
	if d.read == nil {
		d.read = d.readChannel
	}
	d.Every(d.pollInterval, d.poll)
	return d
}

// HandleEvent ignores every inbound event: the monitor only reports upward. It
// never handles JobDispatch; it is a separate driver from the QPU, not part of
// it (RFC 0001 §4).
func (d *Driver) HandleEvent(event qpidriver.Event) {
	log.Printf("[bluefors] dropping event %s: monitor does not handle %s", event.ID, event.Type)
}

// poll reads every configured channel and emits whatever succeeded. A channel
// that fails to read is recorded with a nil value and an ERROR status rather
// than aborting the tick, so one bad channel does not lose the rest. If every
// channel fails, nothing is emitted this tick.
func (d *Driver) poll() {
	readings := make(map[string]Reading, len(d.channels))
	anyOK := false
	for channel, unit := range d.channels {
		r := d.read(channel, unit)
		readings[channel] = r
		if r.Status != "ERROR" {
			anyOK = true
		}
	}

	if !anyOK {
		log.Printf("[bluefors] all %d channel(s) failed this tick; skipping emit", len(readings))
		return
	}

	payload := map[string]any{"readings": readings}
	if err := d.Emit(qpidriver.NewEvent(qpidriver.CryostatReading, d.DriverName(), payload)); err != nil {
		log.Printf("[bluefors] emit failed: %v", err)
	}
}

// bfResponse is the slice of the Bluefors "values" response the monitor reads.
type bfResponse struct {
	Data struct {
		Content struct {
			LatestValidValue *bfSample `json:"latest_valid_value"`
			LatestValue      *bfSample `json:"latest_value"`
		} `json:"content"`
	} `json:"data"`
}

type bfSample struct {
	Value  json.RawMessage `json:"value"`
	Status string          `json:"status"`
}

// readChannel reads one value-tree channel from the Bluefors Control API,
// mirroring the "values" endpoint example in the reference: GET the endpoint
// path (channel with dots replaced by slashes) and read
// content.latest_valid_value, falling back to latest_value.
//
// This is what Options.ReadChannel replaces.
func (d *Driver) readChannel(channel, unit string) Reading {
	endpoint := fmt.Sprintf("%s/values/%s", d.baseURL, strings.ReplaceAll(channel, ".", "/"))
	if d.apiKey != "" {
		endpoint += "?" + url.Values{"key": {d.apiKey}}.Encode()
	}

	value, status, err := d.fetchSample(endpoint)
	if err != nil {
		log.Printf("[bluefors] failed to read channel %s: %v", channel, err)
		return Reading{Value: nil, Unit: unit, Status: "ERROR"}
	}
	return Reading{Value: value, Unit: unit, Status: status}
}

func (d *Driver) fetchSample(endpoint string) (*float64, string, error) {
	resp, err := d.client.Get(endpoint)
	if err != nil {
		return nil, "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, "", fmt.Errorf("status %d", resp.StatusCode)
	}

	var parsed bfResponse
	if err := json.NewDecoder(resp.Body).Decode(&parsed); err != nil {
		return nil, "", err
	}
	sample := parsed.Data.Content.LatestValidValue
	if sample == nil {
		sample = parsed.Data.Content.LatestValue
	}
	if sample == nil {
		return nil, "UNKNOWN", nil
	}
	return parseValue(sample.Value), statusOr(sample.Status, "UNKNOWN"), nil
}

// parseValue coerces the raw JSON value (which Bluefors may send as a number
// or a numeric string) to a float, or nil when it is absent or non-numeric.
func parseValue(raw json.RawMessage) *float64 {
	if len(raw) == 0 || string(raw) == "null" {
		return nil
	}
	var asNumber float64
	if err := json.Unmarshal(raw, &asNumber); err == nil {
		return &asNumber
	}
	var asString string
	if err := json.Unmarshal(raw, &asString); err == nil && asString != "" {
		if parsed, err := strconv.ParseFloat(asString, 64); err == nil {
			return &parsed
		}
	}
	return nil
}

func statusOr(status, fallback string) string {
	if status == "" {
		return fallback
	}
	return status
}

// ParseChannels parses "path[:unit],path[:unit],..." into a channel->unit map,
// the same string form the CLI and setup snippets use.
func ParseChannels(raw string) map[string]string {
	channels := map[string]string{}
	for _, part := range strings.Split(raw, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		path, unit, _ := strings.Cut(part, ":")
		channels[strings.TrimSpace(path)] = strings.TrimSpace(unit)
	}
	return channels
}
