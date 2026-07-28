// Command qpi-driver is the CLI that runs QPI's officially maintained Go
// built-in drivers, mirroring the Python `qpi-driver` CLI (RFC 0003 §4): one
// `start` verb, `--operation` saying what the driver does, `--device` selecting
// the backend within it, universal flags shared, and a device's own settings
// passed as repeatable `-o key=value`.
//
// Install it with:
//
//	go install github.com/sopherapps/qpi/qpi-driver/go/qpi-driver@latest
//
// then, e.g.:
//
//	qpi-driver start --operation monitor --device bluefors_gen1 \
//	  --qpi-addr https://qpi.example.com --token … --ca-fingerprint … \
//	  -o base_url=http://localhost:49099 -o channels=mapper.bf.tmc:K
package main

import (
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/spf13/cobra"

	qpidriver "github.com/sopherapps/qpi/qpi-driver/go"
	"github.com/sopherapps/qpi/qpi-driver/go/qpi-driver/bluefors"
)

// version is the CLI version; overridable at build time with
// -ldflags "-X main.version=…".
var version = "0.1.2"

// commonFlags are the universal options `start` shares across every operation,
// mirroring the Python CLI. A device's own settings go through -o instead.
type commonFlags struct {
	operation     string
	qpiAddr       string
	token         string
	name          string
	device        string
	caFile        string
	caFingerprint string
	options       []string
	recvTimeoutMs int
}

// deviceRunner runs one device of an operation from the shared flags and the
// parsed -o options; it blocks until the driver is stopped.
type deviceRunner func(cf *commonFlags, opts map[string]string) error

// operationSpec is what `start --operation <name>` needs to know: the devices it
// can run and what to fall back to when --device and --name are omitted. Go
// ships no process (QPU) built-in yet, so that operation's device table is
// empty and it says so rather than offering a default it cannot honour
// (RFC 0003 §8).
type operationSpec struct {
	summary       string
	defaultDevice string
	defaultName   string
	devices       map[string]deviceRunner
}

// operations is the whole set, closed because QPI-UI must have a handler for
// each (RFC 0003 §13.1). Adding a device is one entry in a devices map.
var operations = map[string]operationSpec{
	"process": {
		summary:     "Run quantum jobs pushed by QPI-UI and report their results.",
		defaultName: "qpu_sim_01",
		devices:     map[string]deviceRunner{},
	},
	"monitor": {
		summary:       "Report readings upward on a timer.",
		defaultDevice: "bluefors_gen1",
		defaultName:   "qpi-monitor",
		devices: map[string]deviceRunner{
			"bluefors_gen1": runBlueforsGen1,
		},
	},
}

func main() {
	if err := newRootCmd().Execute(); err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
}

func newRootCmd() *cobra.Command {
	root := &cobra.Command{
		Use:           "qpi-driver",
		Short:         "Quantum Processing Interface (QPI) Driver CLI",
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	root.AddCommand(newStartCmd(), newVersionCmd())
	return root
}

// newStartCmd builds the one verb that runs a driver. One command rather than a
// subcommand per operation, because everything about launching a driver is the
// same whichever operation it is (RFC 0003 §4).
func newStartCmd() *cobra.Command {
	cf := &commonFlags{}
	cmd := &cobra.Command{
		Use:   "start",
		Short: "Run a driver: one --operation, on one --device within it (RFC 0001 §4).",
		Long: "Run a driver: one --operation, on one --device within it (RFC 0001 §4).\n\n" +
			operationHelp(),
		RunE: func(_ *cobra.Command, _ []string) error {
			return runOperation(cf)
		},
	}
	addCommonFlags(cmd, cf)
	return cmd
}

// operationHelp lists the operations and their devices, generated rather than
// written down, so a new device shows up in --help with no change here.
func operationHelp() string {
	var b strings.Builder
	b.WriteString("Operations (--operation), and the devices each can run:\n")
	for _, operation := range sortedKeys(operations) {
		spec := operations[operation]
		b.WriteString(fmt.Sprintf("  %s — %s\n", operation, spec.summary))
		if len(spec.devices) == 0 {
			b.WriteString("    (no devices in the Go SDK; see the Python SDK, " +
				"`pip install \"qpi-driver[cli]\"`)\n")
			continue
		}
		b.WriteString(fmt.Sprintf("    %s\n", strings.Join(knownDevices(spec.devices), ", ")))
	}
	return b.String()
}

func addCommonFlags(cmd *cobra.Command, cf *commonFlags) {
	f := cmd.Flags()
	// No short form for --operation: -o is --option, and -O beside it would be a
	// hazard in a command usually written once into a unit file (RFC 0003 §13.7).
	f.StringVar(&cf.operation, "operation", os.Getenv("QPI_OPERATION"),
		"What this driver does: "+strings.Join(sortedKeys(operations), " | "))
	f.StringVarP(&cf.qpiAddr, "qpi-addr", "a", envOr("QPI_ADDR", "http://127.0.0.1:8090"),
		"Full URL of the QPI server")
	f.StringVarP(&cf.token, "token", "t", os.Getenv("QPI_ACCESS_TOKEN"),
		"Access token identifying this driver to the QPI server")
	f.StringVarP(&cf.name, "name", "n", os.Getenv("QPI_DRIVER_NAME"),
		"Human-readable name for this driver; defaults to the operation's own")
	f.StringVarP(&cf.device, "device", "d", os.Getenv("QPI_DEVICE"),
		"Which backend to run within the operation; defaults to the operation's own")
	f.StringVar(&cf.caFile, "ca-file", envOr("QPI_CA_FILE", "./bin/qpi.ca.pem"),
		"Where the downloaded server root CA certificate is written")
	f.StringVar(&cf.caFingerprint, "ca-fingerprint", os.Getenv("QPI_CA_FINGERPRINT"),
		"SHA-256 fingerprint pinning the downloaded root CA of the QPI server")
	f.StringArrayVarP(&cf.options, "option", "o", nil,
		"Operation-specific config as key=value, repeatable (e.g. -o channels=mapper.bf.tmc:K)")
	f.IntVar(&cf.recvTimeoutMs, "recv-timeout-ms",
		envIntOr("QPI_RECV_TIMEOUT_MS", int(qpidriver.DefaultRecvTimeout/time.Millisecond)),
		"How long the receive loop blocks per attempt before checking for shutdown, in ms")
}

// runOperation looks up the device's runner within the chosen operation and runs
// it, mirroring the Python CLI's shared handler. An operation this SDK registers
// no devices for says so plainly, rather than reporting an unknown device with an
// empty list of known ones (RFC 0003 §8).
func runOperation(cf *commonFlags) error {
	if cf.operation == "" {
		return fmt.Errorf("--operation is required; one of %s (or set QPI_OPERATION)",
			strings.Join(sortedKeys(operations), ", "))
	}
	spec, ok := operations[cf.operation]
	if !ok {
		return fmt.Errorf("unknown operation %q; valid operations: %s",
			cf.operation, strings.Join(sortedKeys(operations), ", "))
	}
	if cf.token == "" {
		return fmt.Errorf("access token is required; set --token/-t or the QPI_ACCESS_TOKEN environment variable")
	}
	if len(spec.devices) == 0 {
		return fmt.Errorf("the Go SDK ships no %s devices; run one from the Python SDK "+
			"instead (pip install \"qpi-driver[cli]\") or add one to this CLI",
			cf.operation)
	}

	if cf.device == "" {
		cf.device = spec.defaultDevice
	}
	if cf.name == "" {
		cf.name = spec.defaultName
	}

	runner, ok := spec.devices[cf.device]
	if !ok {
		return fmt.Errorf("unknown %s device %q; known devices: %s",
			cf.operation, cf.device, strings.Join(knownDevices(spec.devices), ", "))
	}
	opts, err := parseOptions(cf.options)
	if err != nil {
		return err
	}
	return runner(cf, opts)
}

// runBlueforsGen1 builds the Bluefors Gen. 1 monitor from the -o options and
// runs it. Recognised keys mirror the Python driver: channels (required),
// base_url, api_key, poll_interval (seconds), timeout (seconds).
func runBlueforsGen1(cf *commonFlags, opts map[string]string) error {
	channels := opts["channels"]
	if channels == "" {
		return fmt.Errorf("bluefors_gen1 needs a 'channels' option, e.g. " +
			"-o channels=mapper.bf.tmc:K,mapper.bf.pmc:mbar")
	}
	monitor := bluefors.New(bluefors.Options{
		BaseURL:      optOr(opts, "base_url", bluefors.DefaultBaseURL),
		Channels:     bluefors.ParseChannels(channels),
		APIKey:       opts["api_key"],
		PollInterval: secondsOr(opts, "poll_interval", bluefors.DefaultPollInterval),
		Timeout:      secondsOr(opts, "timeout", bluefors.DefaultTimeout),
	})
	return qpidriver.Run(monitor, qpidriver.Config{
		QpiAddr:       cf.qpiAddr,
		Token:         cf.token,
		Name:          cf.name,
		CaFingerprint: cf.caFingerprint,
		CaFilePath:    cf.caFile,
		RecvTimeout:   time.Duration(cf.recvTimeoutMs) * time.Millisecond,
	})
}

func newVersionCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "version",
		Short: "Show the version of the QPI driver CLI",
		RunE: func(cmd *cobra.Command, _ []string) error {
			fmt.Fprintln(cmd.OutOrStdout(), version)
			return nil
		},
	}
}

// parseOptions turns repeatable `-o key=value` flags into a dict; each device
// reads the keys it cares about, keeping the CLI generic.
func parseOptions(pairs []string) (map[string]string, error) {
	opts := make(map[string]string, len(pairs))
	for _, pair := range pairs {
		key, value, ok := strings.Cut(pair, "=")
		key = strings.TrimSpace(key)
		if !ok || key == "" {
			return nil, fmt.Errorf("invalid option %q; expected key=value", pair)
		}
		opts[key] = strings.TrimSpace(value)
	}
	return opts, nil
}

func knownDevices(registry map[string]deviceRunner) []string {
	devices := make([]string, 0, len(registry))
	for device := range registry {
		devices = append(devices, device)
	}
	sort.Strings(devices)
	return devices
}

// sortedKeys names the operations in a stable order, so help and error messages
// do not shuffle between runs the way Go map iteration would.
func sortedKeys(specs map[string]operationSpec) []string {
	names := make([]string, 0, len(specs))
	for name := range specs {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

func optOr(opts map[string]string, key, fallback string) string {
	if v, ok := opts[key]; ok && v != "" {
		return v
	}
	return fallback
}

func secondsOr(opts map[string]string, key string, fallback time.Duration) time.Duration {
	if v, ok := opts[key]; ok && v != "" {
		if secs, err := strconv.ParseFloat(v, 64); err == nil {
			return time.Duration(secs * float64(time.Second))
		}
	}
	return fallback
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func envIntOr(key string, fallback int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return fallback
}
