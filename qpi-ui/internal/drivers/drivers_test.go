package drivers

import (
	"strings"
	"testing"
)

func TestKnownKind(t *testing.T) {
	known := []Kind{Mock, QiskitAer, Quantify, Qblox, Presto, BlueforsGen1, Custom}
	for _, kind := range known {
		if !Default.KnownKind(kind) {
			t.Errorf("expected %q to be a known kind", kind)
		}
	}
	if Default.KnownKind(Kind("rigetti")) {
		t.Errorf("expected unknown kind to be reported as unknown")
	}
}

func TestKnownLanguage(t *testing.T) {
	for _, language := range []Language{Python, TypeScript, Go} {
		if !KnownLanguage(language) {
			t.Errorf("expected %q to be a known language", language)
		}
	}
	if KnownLanguage(Language("rust")) {
		t.Errorf("expected unknown language to be reported as unknown")
	}
}

func TestExtraResolution(t *testing.T) {
	cases := map[Kind]string{
		Qblox:        "qpi-driver[cli,qblox]",
		Quantify:     "qpi-driver[cli,quantify]",
		QiskitAer:    "qpi-driver[cli,aer]",
		BlueforsGen1: "qpi-driver[cli,bluefors_gen1]",
		Mock:         baseCliExtra,
		Presto:       baseCliExtra,
	}
	for kind, want := range cases {
		spec, ok := Default.Lookup(kind)
		if !ok {
			t.Fatalf("expected %q to be registered", kind)
		}
		if got := spec.extra(); got != want {
			t.Errorf("extra for %q = %q, want %q", kind, got, want)
		}
	}
}

func TestEventsForKind(t *testing.T) {
	events := Default.Events(Qblox)
	if len(events) != 2 || events[0] != eventJobDispatch || events[1] != eventJobResult {
		t.Errorf("expected qblox events [JobDispatch, JobResult], got %v", events)
	}
	if events := Default.Events(Custom); events != nil {
		t.Errorf("expected custom kind to have no preset events, got %v", events)
	}
}

// TestEventsForBlueforsGen1 proves the monitor takes part in CryostatReading
// only — never the job flow (RFC 0001 §7).
func TestEventsForBlueforsGen1(t *testing.T) {
	events := Default.Events(BlueforsGen1)
	if len(events) != 1 || events[0] != eventCryostatReading {
		t.Errorf("expected bluefors_gen1 events [CryostatReading], got %v", events)
	}
}

func TestSnippetsExecutor(t *testing.T) {
	s := Default.Snippets(Qblox, Python, Params{
		Name: "qpu_1", Token: "tok_abc", QpiAddr: "https://qpi.example.com", CaFingerprint: "ca_hash",
	})

	if s.Install != "" || s.Stub != "" {
		t.Errorf("expected no bare-install/stub for an official build, got %+v", s)
	}
	for _, snippet := range []string{s.Systemd, s.ManualCLI} {
		if !strings.Contains(snippet, "tok_abc") {
			t.Errorf("expected snippet to carry the token, got %q", snippet)
		}
	}
	// The name reaches the systemd snippet, which uses it to name the unit, and not
	// the manual command, which no longer has anywhere to put it.
	if !strings.Contains(s.Systemd, "SERVICE_NAME='qpu_1'") {
		t.Errorf("expected SERVICE_NAME in the systemd snippet, got %q", s.Systemd)
	}
	if !strings.Contains(s.ManualCLI, "start --operation process --device qblox") {
		t.Errorf("expected the process start command, got %q", s.ManualCLI)
	}
	if !strings.Contains(s.ManualCLI, "qpi-driver[cli,qblox]") {
		t.Errorf("expected qblox extra, got %q", s.ManualCLI)
	}
	// This used to assert a process driver takes no `-o` options, which was only
	// true of the snippet: the driver reads two config paths whose defaults are
	// relative, so a pasted command that omitted them looked for them in whatever
	// directory it happened to run in.
	for _, key := range []string{"quantify_device_config", "quantify_hardware_config"} {
		if !strings.Contains(s.ManualCLI, "-o "+key+"=") {
			t.Errorf("expected -o %s in the manual CLI, got %q", key, s.ManualCLI)
		}
	}
	// The rest of the shared process list stays out: they default sensibly, and
	// nothing about a qblox chip is configured by them.
	for _, key := range []string{"data_dir", "job_timeout", "is_dummy", "spi_rack_address"} {
		if strings.Contains(s.ManualCLI, key) {
			t.Errorf("expected %q to be catalog-only, got %q", key, s.ManualCLI)
		}
	}
	if !strings.Contains(s.Systemd, "OPERATION=process DEVICE=qblox") {
		t.Errorf("expected OPERATION/DEVICE in the systemd snippet, got %q", s.Systemd)
	}
}

// TestSnippetsMonitor proves a monitor kind gets the same official tier as an
// executor, but launched via `monitor --device` with its config as `-o` options
// (RFC 0001 §7).
func TestSnippetsMonitor(t *testing.T) {
	s := Default.Snippets(BlueforsGen1, Python, Params{
		Name: "cryostat-1", Token: "tok123", QpiAddr: "http://localhost:8090", CaFingerprint: "aa:bb",
	})

	if s.Install != "" || s.Stub != "" {
		t.Errorf("expected no bare-install/stub for an official build, got %+v", s)
	}
	if !strings.Contains(s.ManualCLI, "start --operation monitor --device bluefors_gen1") {
		t.Errorf("expected the monitor start command, got %q", s.ManualCLI)
	}
	if strings.Contains(s.ManualCLI, "--operation process") {
		t.Errorf("expected monitor not to use the process operation, got %q", s.ManualCLI)
	}
	if !strings.Contains(s.ManualCLI, "-o base_url=") || !strings.Contains(s.ManualCLI, "-o channels=") {
		t.Errorf("expected -o options in the manual CLI, got %q", s.ManualCLI)
	}
	// channels first: it is the one option with no default, so it leads.
	if !strings.Contains(s.Systemd, "DRIVER_OPTIONS='channels=") ||
		!strings.Contains(s.Systemd, ";base_url=") {
		t.Errorf("expected DRIVER_OPTIONS env in the systemd snippet, got %q", s.Systemd)
	}
	// The options the driver defaults sensibly stay out of a copy-pasted command.
	for _, key := range []string{"api_key", "poll_interval", "timeout"} {
		if strings.Contains(s.Systemd, key) || strings.Contains(s.ManualCLI, key) {
			t.Errorf("expected %q to be catalog-only, not in a snippet: %q / %q",
				key, s.Systemd, s.ManualCLI)
		}
	}
	if !strings.Contains(s.Systemd, "OPERATION=monitor DEVICE=bluefors_gen1") {
		t.Errorf("expected OPERATION/DEVICE in the systemd snippet, got %q", s.Systemd)
	}
}

// TestSnippetsCalibrate proves a tuner's snippet carries the three configs it
// cannot start without: which routines to run, the chip to write back to, and the
// wiring. Their defaults are relative paths, so under systemd they resolve against
// a working directory the operator never chose.
func TestSnippetsCalibrate(t *testing.T) {
	for _, kind := range []Kind{QuantifyTuner, QbloxTuner} {
		s := Default.Snippets(kind, Python, Params{
			Name: "tuner-1", Token: "tok", QpiAddr: "https://qpi", CaFingerprint: "ca",
		})

		for _, key := range []string{
			"calibration_config", "quantify_device_config", "quantify_hardware_config",
		} {
			if !strings.Contains(s.ManualCLI, "-o "+key+"=") {
				t.Errorf("%s: expected -o %s in the manual CLI, got %q", kind, key, s.ManualCLI)
			}
			if !strings.Contains(s.Systemd, key+"=") {
				t.Errorf("%s: expected %s in DRIVER_OPTIONS, got %q", kind, key, s.Systemd)
			}
		}
		if !strings.Contains(s.Systemd, "DRIVER_OPTIONS='") {
			t.Errorf("%s: expected DRIVER_OPTIONS in the systemd snippet, got %q", kind, s.Systemd)
		}
		// Defaults that are actually defaults.
		for _, key := range []string{"drift_check_interval", "fidelity_threshold", "is_dummy"} {
			if strings.Contains(s.ManualCLI, key) {
				t.Errorf("%s: expected %q to be catalog-only, got %q", kind, key, s.ManualCLI)
			}
		}
	}
}

// A device that reads none of the quantify configs must not be told about them:
// the option list is shared across every process kind, so marking one for the
// snippet marks it for all of them unless it is done per kind.
func TestSnippetsDoNotOfferConfigsAKindIgnores(t *testing.T) {
	for _, kind := range []Kind{Mock, QiskitAer, Presto} {
		s := Default.Snippets(kind, Python, Params{
			Name: "q", Token: "tok", QpiAddr: "https://qpi", CaFingerprint: "ca",
		})
		if strings.Contains(s.ManualCLI, "-o ") || strings.Contains(s.Systemd, "DRIVER_OPTIONS") {
			t.Errorf("%s reads no config paths; expected a clean snippet, got %q / %q",
				kind, s.ManualCLI, s.Systemd)
		}
	}
}

// A rendered command must never end on a backslash: pasted into a shell, a dangling
// continuation swallows the next line the operator types.
func TestSnippetsNeverEndOnAContinuation(t *testing.T) {
	for _, kind := range append(Default.Kinds(), Custom) {
		s := Default.Snippets(kind, Python, Params{
			Name: "q", Token: "tok", QpiAddr: "https://qpi", CaFingerprint: "ca",
		})
		for label, snippet := range map[string]string{
			"systemd": s.Systemd, "manual": s.ManualCLI, "install": s.Install,
		} {
			if strings.HasSuffix(strings.TrimRight(snippet, "\n"), "\\") {
				t.Errorf("%s %s snippet ends on a continuation: %q", kind, label, snippet)
			}
		}
	}
}

func TestSnippetsCustomHasNoOfficialBuild(t *testing.T) {
	s := Default.Snippets(Custom, Python, Params{Name: "x", Token: "t", QpiAddr: "u", CaFingerprint: "f"})
	if s.Systemd != "" || s.ManualCLI != "" {
		t.Errorf("expected no official-build snippets for a custom driver, got %+v", s)
	}
	if s.Install == "" || s.Stub == "" {
		t.Errorf("expected an install command and a stub for a custom driver, got %+v", s)
	}
	if !strings.Contains(s.Stub, "handle_event") {
		t.Errorf("expected the python stub to use handle_event, got %q", s.Stub)
	}
}

// TestSnippetsRenderNoDeviceTheLanguageLacks is the case that used to render a
// working-looking command that could never work: qblox is a process device and only
// the Python SDK has one, so `--device qblox` against a `go install`-ed binary exits
// 1 the first time it is pasted. handleDriverCreate rejects the combination now, and
// nothing here renders it either.
func TestSnippetsRenderNoDeviceTheLanguageLacks(t *testing.T) {
	p := Params{Name: "x", Token: "t", QpiAddr: "u", CaFingerprint: "f"}

	for _, language := range []Language{Go, TypeScript} {
		s := Default.Snippets(Qblox, language, p)
		if s.Systemd != "" || s.ManualCLI != "" {
			t.Errorf("%s: expected no run snippets for a device this SDK has not got, got %+v",
				language, s)
		}
		if s.Install == "" {
			t.Errorf("%s: expected the SDK install command, got %+v", language, s)
		}
		if strings.Contains(s.Install, "qblox") {
			t.Errorf("%s: expected nothing about qblox in %q", language, s.Install)
		}
	}

	// Python does have it, and is unaffected.
	s := Default.Snippets(Qblox, Python, p)
	if !strings.Contains(s.ManualCLI, "start --operation process --device qblox") {
		t.Errorf("expected the process start command for Python, got %q", s.ManualCLI)
	}
}

// TestShipsInKnowsWhichSdkHasWhat covers the lookup handleDriverCreate gates on.
// Custom is registerable in every language by definition: it is code the operator
// writes against the SDK, so the SDK having no such device is the point.
func TestShipsInKnowsWhichSdkHasWhat(t *testing.T) {
	cases := []struct {
		kind     Kind
		language Language
		want     bool
	}{
		{Qblox, Python, true},
		{Qblox, Go, false},
		{Qblox, TypeScript, false},
		{Mock, Go, false},
		{BlueforsGen1, Python, true},
		{BlueforsGen1, Go, true},
		{BlueforsGen1, TypeScript, true},
		{Custom, Go, true},
		{Custom, TypeScript, true},
		{Kind("no_such_device"), Python, false},
	}
	for _, c := range cases {
		if got := Default.ShipsIn(c.kind, c.language); got != c.want {
			t.Errorf("ShipsIn(%q, %q) = %v, want %v", c.kind, c.language, got, c.want)
		}
	}

	if kinds := Default.KindsIn(Go); len(kinds) != 1 || kinds[0] != BlueforsGen1 {
		t.Errorf("KindsIn(go) = %v, want just [bluefors_gen1]", kinds)
	}
	if kinds := Default.KindsIn(Python); len(kinds) != len(Default.Kinds()) {
		t.Errorf("KindsIn(python) = %v, want every registered kind", kinds)
	}
}

// TestSnippetsBlueforsPerLanguage proves the bluefors_gen1 monitor resolves
// official per-language run snippets — `go install` / `npm install -g` in the
// manual CLI, the language's install-systemd.sh, and the monitor start
// command with its -o options — for Go and TypeScript (RFC 0001 §7, Phase 4).
func TestSnippetsBlueforsPerLanguage(t *testing.T) {
	p := Params{Name: "cryostat-1", Token: "tok", QpiAddr: "https://qpi.example.com", CaFingerprint: "f"}

	goSnips := Default.Snippets(BlueforsGen1, Go, p)
	if goSnips.Install != "" || goSnips.Stub != "" {
		t.Errorf("expected no bare-install/stub for an official Go build, got %+v", goSnips)
	}
	if !strings.Contains(goSnips.ManualCLI, "go install github.com/sopherapps/qpi/qpi-driver/go/qpi-driver") {
		t.Errorf("expected a `go install` manual CLI, got %q", goSnips.ManualCLI)
	}
	if !strings.Contains(goSnips.ManualCLI, "start --operation monitor --device bluefors_gen1") {
		t.Errorf("expected the monitor start command, got %q", goSnips.ManualCLI)
	}
	if !strings.Contains(goSnips.Systemd, "/go/install-systemd.sh") {
		t.Errorf("expected the Go install-systemd.sh URL, got %q", goSnips.Systemd)
	}

	tsSnips := Default.Snippets(BlueforsGen1, TypeScript, p)
	if !strings.Contains(tsSnips.ManualCLI, "npm install -g qpi-driver") {
		t.Errorf("expected an `npm install -g` manual CLI, got %q", tsSnips.ManualCLI)
	}
	if !strings.Contains(tsSnips.ManualCLI, "start --operation monitor --device bluefors_gen1") {
		t.Errorf("expected the monitor start command, got %q", tsSnips.ManualCLI)
	}
	if !strings.Contains(tsSnips.Systemd, "/js/install-systemd.sh") {
		t.Errorf("expected the TS install-systemd.sh URL, got %q", tsSnips.Systemd)
	}
}

// TestNameIsShellQuoted guards against a driver name breaking out of the shell
// command an operator pastes and runs. The name no longer reaches the driver at
// all — it names the systemd unit — but it still reaches a shell, as SERVICE_NAME.
func TestNameIsShellQuoted(t *testing.T) {
	s := Default.Snippets(Qblox, Python, Params{
		Name: "a'; rm -rf /", Token: "t", QpiAddr: "u", CaFingerprint: "f",
	})
	if !strings.Contains(s.Systemd, `SERVICE_NAME='a'\''; rm -rf /'`) {
		t.Errorf("expected the name's single quote to be shell-escaped, got %q", s.Systemd)
	}
	// And nothing renders a --name any more.
	if strings.Contains(s.ManualCLI, "--name") || strings.Contains(s.Systemd, "--name") {
		t.Errorf("expected no --name in either snippet, got %q / %q", s.ManualCLI, s.Systemd)
	}
}

// TestCalibrateOptionsMatchTheDriver pins the catalog's `-o` keys to the ones
// the Python builder reads (RFC 0004 §6.5).
//
// Reading an option is what declares it, and an option nothing reads is a
// startup error (RFC 0003 §13.6) — so a key here that the driver does not read
// turns a generated setup snippet into a driver that refuses to launch. The
// list is duplicated rather than derived because the SDK publishes no catalog
// (RFC 0003 §9); this test is what keeps the two in step.
func TestCalibrateOptionsMatchTheDriver(t *testing.T) {
	// Exactly the keys read by build_from_options in
	// qpi_driver/builtins/calibrate.py.
	want := map[string]bool{
		"data_dir":                 true,
		"save_raw_data":            true,
		"calibration_config":       true,
		"quantify_hardware_config": true,
		"quantify_device_config":   true,
		"is_dummy":                 true,
		"is_simulated":             true,
		"spi_rack_address":         true,
		"drift_check_interval":     true,
		"fidelity_threshold":       true,
		"fidelity_2q_threshold":    true,
	}

	spec, ok := Default.Lookup(QuantifyTuner)
	if !ok {
		t.Fatal("expected quantify_tuner in the catalog")
	}

	got := map[string]bool{}
	for _, option := range spec.Options {
		got[option.Key] = true
		if !want[option.Key] {
			t.Errorf("catalog offers -o %s, which the calibrate driver does not read", option.Key)
		}
	}
	for key := range want {
		if !got[key] {
			t.Errorf("the calibrate driver reads -o %s, which the catalog does not offer", key)
		}
	}
}

// TestBothTunersAreRegisteredForCalibrate proves the two tuner devices are in
// the catalog, Python-only — no other SDK ships a tuner (RFC 0004 §6.1).
func TestBothTunersAreRegisteredForCalibrate(t *testing.T) {
	for _, kind := range []Kind{QuantifyTuner, QbloxTuner} {
		spec, ok := Default.Lookup(kind)
		if !ok {
			t.Fatalf("expected %s in the catalog", kind)
		}
		if spec.Operation != Calibrate {
			t.Errorf("expected %s to be a calibrate device, got %s", kind, spec.Operation)
		}
		if !spec.ShipsIn(Python) || spec.ShipsIn(Go) || spec.ShipsIn(TypeScript) {
			t.Errorf("expected %s to ship in Python only, got %v", kind, spec.Languages)
		}
		if len(spec.Events) != 3 || spec.Events[0] != eventCalibrateDispatch ||
			spec.Events[1] != eventCalibrationResult || spec.Events[2] != eventCalibrationProgress {
			t.Errorf("expected %s events [CalibrateDispatch, CalibrationResult, CalibrationProgress], got %v", kind, spec.Events)
		}
	}
}
