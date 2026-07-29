package cli

import (
	"bytes"
	"errors"
	"os"
	"strings"
	"testing"

	qpidriver "github.com/sopherapps/qpi/qpi-driver/go"

	"github.com/sopherapps/qpi/qpi-driver/go/devices"
	"github.com/sopherapps/qpi/qpi-driver/go/qpi-driver/bluefors"
)

// TestMain registers the devices the shipped CLI registers, since that is what
// these tests are about: the command tree over a populated registry. It is also
// the whole of what a downstream main does (RFC 0003 §6).
func TestMain(m *testing.M) {
	if err := devices.Register(bluefors.DeviceSpec); err != nil {
		panic(err)
	}
	os.Exit(m.Run())
}

// run executes the CLI with args and returns what it wrote plus the error it
// returned, without ever starting a driver — every case here fails, or only
// prints, before a driver would be run.
func run(t *testing.T, args ...string) (string, error) {
	t.Helper()
	root := NewRootCmd()
	out := &bytes.Buffer{}
	root.SetOut(out)
	root.SetErr(out)
	root.SetArgs(args)
	err := root.Execute()
	return out.String(), err
}

func TestOperationIsRequired(t *testing.T) {
	// `start` alone cannot know what to run: the operation is the one thing that
	// is not defaultable.
	_, err := run(t, "start", "--token", "t", "--ca-fingerprint", "fp")
	if err == nil {
		t.Fatal("expected start without --operation to fail")
	}
	for _, want := range []string{"--operation is required", "monitor", "process"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("expected the error to mention %q, got %q", want, err)
		}
	}
}

func TestUnknownOperationListsTheValidOnes(t *testing.T) {
	// Operations are a closed set — QPI-UI must have a handler for each — so the
	// error can name all of them (RFC 0003 §13.1).
	_, err := run(t, "start", "--operation", "procces", "--token", "t")
	if err == nil {
		t.Fatal("expected an unknown operation to fail")
	}
	if !strings.Contains(err.Error(), `unknown operation "procces"`) {
		t.Errorf("expected the operation named, got %q", err)
	}
	if !strings.Contains(err.Error(), "process, monitor") {
		t.Errorf("expected the valid operations listed, got %q", err)
	}
}

func TestOperationComesFromTheEnvironment(t *testing.T) {
	// Flags are registered with the environment already read, so a unit file can
	// be nothing but Environment= lines.
	t.Setenv("QPI_OPERATION", "monitor")
	t.Setenv("QPI_ACCESS_TOKEN", "t")

	_, err := run(t, "start", "--device", "nope")
	if err == nil || !strings.Contains(err.Error(), "unknown monitor device") {
		t.Fatalf("expected QPI_OPERATION to select monitor, got %v", err)
	}
}

func TestEmptyOperationSaysSoHonestly(t *testing.T) {
	// This build registers no process devices. With one verb, `--operation
	// process` has to say that plainly rather than reporting an unknown device
	// with an empty list of known ones (RFC 0003 §8).
	_, err := run(t, "start", "--operation", "process", "--token", "t")
	if err == nil {
		t.Fatal("expected an operation with no devices to fail")
	}
	message := err.Error()
	if !strings.Contains(message, "ships no process devices") {
		t.Errorf("expected an honest message, got %q", message)
	}
	if strings.Contains(message, "known devices") {
		t.Errorf("expected no empty device list, got %q", message)
	}
	if !strings.Contains(message, "qpi-driver[cli]") {
		t.Errorf("expected the message to say where to find one, got %q", message)
	}
}

func TestTokenIsRequired(t *testing.T) {
	_, err := run(t, "start", "--operation", "monitor", "--device", "bluefors_gen1")
	if err == nil || !strings.Contains(err.Error(), "access token is required") {
		t.Fatalf("expected a missing token to be reported, got %v", err)
	}
}

func TestUnknownOptionKeyIsRejected(t *testing.T) {
	// With no declared schema, the check is what the device read: the builder never
	// looks at base_urll, so the leftover key is the whole evidence. Silently
	// ignoring it is what this replaced — a typo in a unit file used to mean a
	// driver running with a default nobody chose.
	_, err := run(t, "start", "--operation", "monitor", "--device", "bluefors_gen1",
		"--token", "t", "-o", "channels=mapper.bf.tmc:K", "-o", "base_urll=http://x")
	if err == nil {
		t.Fatal("expected an unknown -o key to fail")
	}
	if !strings.Contains(err.Error(), `unknown option "base_urll"`) {
		t.Errorf("expected the bad key named, got %q", err)
	}
}

func TestMissingRequiredOptionIsReported(t *testing.T) {
	_, err := run(t, "start", "--operation", "monitor", "--device", "bluefors_gen1",
		"--token", "t")
	if err == nil || !strings.Contains(err.Error(), `missing required option "channels"`) {
		t.Fatalf("expected the required option to be named, got %v", err)
	}
}

func TestTheOldSubcommandsAreGone(t *testing.T) {
	// The grammar broke once, deliberately (RFC 0003 §11). A subcommand that
	// still worked would make the migration optional and the docs wrong.
	for _, operation := range []string{"process", "monitor"} {
		if _, err := run(t, operation, "--token", "t"); err == nil {
			t.Errorf("expected %q to no longer be a subcommand", operation)
		}
	}
}

func TestStartHelpPointsAtTheDashboard(t *testing.T) {
	// --help used to render every device and every -o key from a declared schema,
	// which was a second catalog beside QPI-UI's (RFC 0003 §9).
	out, err := run(t, "start", "--help")
	if err != nil {
		t.Fatalf("expected --help to succeed, got %v", err)
	}
	for _, want := range []string{"QPI-UI", "qpi-driver devices"} {
		if !strings.Contains(out, want) {
			t.Errorf("expected --help to mention %q, got:\n%s", want, out)
		}
	}
}

func TestDevicesCommandListsNamesByOperation(t *testing.T) {
	out, err := run(t, "devices")
	if err != nil {
		t.Fatalf("expected devices to succeed, got %v", err)
	}
	if !strings.Contains(out, "monitor: bluefors_gen1") {
		t.Errorf("expected the monitor device listed, got:\n%s", out)
	}
	// This build ships no process device, and must say so rather than print nothing.
	if !strings.Contains(out, "process: none") {
		t.Errorf("expected the empty operation stated, got:\n%s", out)
	}
}

func TestDevicesCommandNarrowsToOneOperation(t *testing.T) {
	out, err := run(t, "devices", "--operation", "monitor")
	if err != nil {
		t.Fatalf("expected devices to succeed, got %v", err)
	}
	if !strings.Contains(out, "bluefors_gen1") {
		t.Errorf("expected the monitor device listed, got:\n%s", out)
	}
	if strings.Contains(out, "process:") {
		t.Errorf("expected process to be left out, got:\n%s", out)
	}

	if _, err := run(t, "devices", "--operation", "nope"); err == nil {
		t.Error("expected an unknown operation to fail")
	}
}

func TestThereIsNoCatalogCommand(t *testing.T) {
	// The machine-readable catalog is gone; nothing consumed it but a sync script.
	if _, err := run(t, "catalog", "--json"); err == nil {
		t.Error("expected `catalog` to no longer be a subcommand")
	}
}

func TestParseOptionsReadsTheSyntaxOnly(t *testing.T) {
	// Splitting is all this does; an unknown key passes through for the unread-option
	// check to catch after the build.
	opts, err := parseOptions([]string{"channels=a:K", " base_url = http://x ", "nonsense=1"})
	if err != nil {
		t.Fatalf("expected valid pairs to parse, got %v", err)
	}
	if opts["channels"] != "a:K" || opts["base_url"] != "http://x" || opts["nonsense"] != "1" {
		t.Errorf("expected trimmed key/value pairs, got %v", opts)
	}
	if _, err := parseOptions([]string{"not-a-pair"}); err == nil {
		t.Error("expected a pair without '=' to be rejected")
	}
}

func TestEnvFallbacks(t *testing.T) {
	t.Setenv("QPI_ADDR", "")
	if got := envOr("QPI_ADDR", "fallback"); got != "fallback" {
		t.Errorf("expected the fallback for an empty env var, got %q", got)
	}
	t.Setenv("QPI_ADDR", "https://qpi.example.com")
	if got := envOr("QPI_ADDR", "fallback"); got != "https://qpi.example.com" {
		t.Errorf("expected the env var to win, got %q", got)
	}

	t.Setenv("QPI_RECV_TIMEOUT_MS", "not-a-number")
	if got := envIntOr("QPI_RECV_TIMEOUT_MS", 200); got != 200 {
		t.Errorf("expected an unparseable value to fall back, got %d", got)
	}
	t.Setenv("QPI_RECV_TIMEOUT_MS", "350")
	if got := envIntOr("QPI_RECV_TIMEOUT_MS", 200); got != 350 {
		t.Errorf("expected the env var to win, got %d", got)
	}
}

func TestFlagDefaults(t *testing.T) {
	// --device is blank by default and has no fallback: QPI-UI generates the command
	// that launches a driver, and it always names a device.
	cmd := newStartCmd()
	for _, flag := range []string{"device", "operation"} {
		if got := cmd.Flags().Lookup(flag).DefValue; got != "" {
			t.Errorf("expected --%s to default to empty, got %q", flag, got)
		}
	}
	// A driver does not name itself: the display label belongs to the admin who
	// registered it, and the drivers/connect response hands it over.
	for _, flag := range []string{"name"} {
		if cmd.Flags().Lookup(flag) != nil {
			t.Errorf("expected no --%s flag", flag)
		}
	}
	if cmd.Flags().ShorthandLookup("n") != nil {
		t.Error("expected no -n shorthand")
	}
	if cmd.Flags().ShorthandLookup("O") != nil {
		t.Error("expected no -O shorthand beside -o (RFC 0003 §13.7)")
	}
	if cmd.Flags().Lookup("operation").Shorthand != "" {
		t.Error("expected --operation to have no short form")
	}
}

func TestConfigOfCarriesTheTransportFlags(t *testing.T) {
	cfg := configOf(&commonFlags{
		qpiAddr:       "https://qpi.example.com",
		token:         "tok",
		caFingerprint: "fp",
		caFile:        "./bin/qpi.ca.pem",
		recvTimeoutMs: 350,
	})

	if cfg.QpiAddr != "https://qpi.example.com" || cfg.Token != "tok" {
		t.Errorf("expected the transport flags to land, got %+v", cfg)
	}
	if cfg.RecvTimeout.Milliseconds() != 350 {
		t.Errorf("expected 350ms, got %v", cfg.RecvTimeout)
	}
}

func TestVersionPrintsTheVersion(t *testing.T) {
	out, err := run(t, "version")
	if err != nil {
		t.Fatalf("expected version to succeed, got %v", err)
	}
	if strings.TrimSpace(out) != Version {
		t.Errorf("expected %q, got %q", Version, out)
	}
}

func TestStartRunsTheResolvedDevice(t *testing.T) {
	// The happy path as far as it goes without a server: resolve, parse, build, and
	// hand the driver to qpidriver.Run — which is where it stops, since Run needs
	// somewhere to connect to. The e2e suite covers the rest.
	var built *commonFlags
	spec := devices.Default
	original, err := spec.Resolve(devices.Monitor, "bluefors_gen1")
	if err != nil {
		t.Fatal(err)
	}
	replacement := original
	replacement.Build = func(cfg qpidriver.Config, opts *devices.Options) (qpidriver.Driver, error) {
		built = &commonFlags{token: cfg.Token, qpiAddr: cfg.QpiAddr}
		return nil, errors.New("built, and deliberately not run")
	}
	*spec = *devices.NewRegistry(replacement)
	t.Cleanup(func() { *spec = *devices.NewRegistry(original) })

	_, err = run(t, "start", "--operation", "monitor", "--device", "bluefors_gen1",
		"--token", "t", "--qpi-addr", "https://qpi.example.com",
		"-o", "channels=mapper.bf.tmc:K")

	if err == nil || !strings.Contains(err.Error(), "deliberately not run") {
		t.Fatalf("expected the builder's error to surface, got %v", err)
	}
	if built == nil || built.token != "t" || built.qpiAddr != "https://qpi.example.com" {
		t.Errorf("expected the transport config to reach the builder, got %+v", built)
	}
}

func TestStartRejectsAMalformedOption(t *testing.T) {
	// `-o` with no `=` is a syntax error, caught before the device sees it.
	_, err := run(t, "start", "--operation", "monitor", "--device", "bluefors_gen1",
		"--token", "t", "-o", "not-a-pair")

	if err == nil || !strings.Contains(err.Error(), "expected key=value") {
		t.Fatalf("expected a malformed option to be reported, got %v", err)
	}
}

func TestStartRejectsABadOptionValue(t *testing.T) {
	_, err := run(t, "start", "--operation", "monitor", "--device", "bluefors_gen1",
		"--token", "t", "-o", "channels=mapper.bf.tmc:K", "-o", "poll_interval=soon")

	if err == nil || !strings.Contains(err.Error(), "bad value for -o poll_interval") {
		t.Fatalf("expected the option named, got %v", err)
	}
}

func TestStartAsksForADeviceAndNamesTheOnesThisBuildHas(t *testing.T) {
	// Which devices a Go binary has is settled at compile time, so the message has
	// to come from the registry rather than from a documented default.
	_, err := run(t, "start", "--operation", "monitor", "--token", "t")

	if err == nil || !strings.Contains(err.Error(), "--device is required") {
		t.Fatalf("expected a request for --device, got %v", err)
	}
	if !strings.Contains(err.Error(), "bluefors_gen1") {
		t.Errorf("expected the available devices named, got %v", err)
	}
}

func TestStartSaysWhenThisBuildHasNoDeviceForTheOperation(t *testing.T) {
	// No devices at all is a different message from "pick one of these".
	_, err := run(t, "start", "--operation", "process", "--token", "t")

	if err == nil || !strings.Contains(err.Error(), "ships no process devices") {
		t.Fatalf("expected the empty case stated, got %v", err)
	}
}
