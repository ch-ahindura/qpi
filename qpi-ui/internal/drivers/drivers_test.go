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
		if !strings.Contains(snippet, "tok_abc") || !strings.Contains(snippet, "qpu_1") {
			t.Errorf("expected snippet to carry token and name, got %q", snippet)
		}
	}
	if !strings.Contains(s.ManualCLI, "start --operation process --device qblox") {
		t.Errorf("expected the process start command, got %q", s.ManualCLI)
	}
	if !strings.Contains(s.ManualCLI, "qpi-driver[cli,qblox]") {
		t.Errorf("expected qblox extra, got %q", s.ManualCLI)
	}
	if strings.Contains(s.ManualCLI, "-o ") {
		t.Errorf("expected a process driver to take no -o options, got %q", s.ManualCLI)
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
	if !strings.Contains(s.Systemd, "DRIVER_OPTIONS='base_url=") {
		t.Errorf("expected DRIVER_OPTIONS env in the systemd snippet, got %q", s.Systemd)
	}
	if !strings.Contains(s.Systemd, "OPERATION=monitor DEVICE=bluefors_gen1") {
		t.Errorf("expected OPERATION/DEVICE in the systemd snippet, got %q", s.Systemd)
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

// TestSnippetsOfficialGoUsesGoInstall proves an official Go driver resolves the
// per-language official run snippets — installed via `go install` — rather than
// a bare SDK install + stub (RFC 0001 Phase 4).
func TestSnippetsOfficialGoUsesGoInstall(t *testing.T) {
	s := Default.Snippets(Qblox, Go, Params{Name: "x", Token: "t", QpiAddr: "u", CaFingerprint: "f"})
	if s.Install != "" || s.Stub != "" {
		t.Errorf("expected no bare-install/stub for an official Go build, got %+v", s)
	}
	if !strings.Contains(s.ManualCLI, "go install github.com/sopherapps/qpi/qpi-driver/go/qpi-driver") {
		t.Errorf("expected a `go install` manual CLI, got %q", s.ManualCLI)
	}
	if !strings.Contains(s.ManualCLI, "start --operation process --device qblox") {
		t.Errorf("expected the process start command, got %q", s.ManualCLI)
	}
	if !strings.Contains(s.Systemd, "/go/install-systemd.sh") {
		t.Errorf("expected the Go install-systemd.sh URL, got %q", s.Systemd)
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
// command an operator pastes and runs.
func TestNameIsShellQuoted(t *testing.T) {
	s := Default.Snippets(Qblox, Python, Params{
		Name: "a'; rm -rf /", Token: "t", QpiAddr: "u", CaFingerprint: "f",
	})
	if !strings.Contains(s.ManualCLI, `--name 'a'\''; rm -rf /'`) {
		t.Errorf("expected the name's single quote to be shell-escaped, got %q", s.ManualCLI)
	}
}
