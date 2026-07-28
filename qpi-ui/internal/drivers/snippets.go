package drivers

import (
	"fmt"
	"strings"
	"text/template"
)

const (
	// baseCliExtra is installed for an official kind with no dedicated extra.
	baseCliExtra = "qpi-driver[cli]"
	// baseSdkPackage is installed for a custom driver, which depends on the SDK
	// alone and supplies its own handlers (RFC 0001 §5).
	baseSdkPackage = "qpi-driver"
	// optionFlag is the CLI flag an operation's per-kind config is passed
	// under, once per option.
	optionFlag = "-o"
	// gitRawBaseUrl is the base URL where the raw content of the git repo can be got
	gitRawBaseUrl = "https://raw.githubusercontent.com/sopherapps/qpi/main/qpi-driver"
)

// scriptURLs maps language to where its install-systemd.sh is fetched from in the systemd
// snippet for its drivers.
var scriptUrls = map[Language]string{
	Python:     gitRawBaseUrl + "/py/install-systemd.sh",
	Go:         gitRawBaseUrl + "/go/install-systemd.sh",
	TypeScript: gitRawBaseUrl + "/js/install-systemd.sh",
}

// manualCLITmpls is the mapping for languages and the manual CLI templates
var manualCLITmpls = map[Language]*template.Template{
	Python:     manualPyCLITmpl,
	Go:         manualGoCLITmpl,
	TypeScript: manualTsCLITmpl,
}

// Params carries the per-registration values spliced into the setup snippets.
type Params struct {
	Name          string
	Token         string
	QpiAddr       string
	CaFingerprint string
}

// Snippets are the ready-to-use setup commands shown once at registration
// (RFC 0001 §3). A custom driver — code the operator writes — fills Install and
// Stub. An official Python backend, which is run rather than written, fills
// Systemd and ManualCLI. An official backend in another language fills Install
// alone: its SDK ships the driver, so there is nothing to write and no stub.
type Snippets struct {
	Systemd   string `json:"systemd,omitempty"`
	ManualCLI string `json:"manual_cli,omitempty"`
	Install   string `json:"install,omitempty"`
	Stub      string `json:"stub,omitempty"`
}

// Snippets resolves the kind×language setup snippets for a registration:
//
//   - A custom driver is code the operator writes, so it gets the SDK install
//     command and a stub to extend, in the chosen language (RFC 0001 §5).
//   - An official driver is run, not written, so it gets the prefilled
//     systemd + manual-CLI commands.
func (r *Registry) Snippets(kind Kind, language Language, p Params) Snippets {
	if kind == Custom {
		return Snippets{
			Install: installCommand(language),
			Stub:    stub(language),
		}
	}
	if spec, ok := r.Lookup(kind); ok {
		return officialSnippets(spec, p, language)
	}
	return Snippets{Install: installCommand(language)}
}

// officialSnippets renders the systemd + manual-CLI commands for an official
// drivers, from the spec's operation, device, and install extra.
func officialSnippets(spec Spec, p Params, l Language) Snippets {
	data := snippetData{
		ScriptURL:     scriptUrls[l],
		Token:         p.Token,
		QpiAddr:       p.QpiAddr,
		CaFingerprint: p.CaFingerprint,
		Name:          shellQuote(p.Name),
		Device:        string(spec.Kind),
		Operation:     string(spec.Operation),
		Extra:         shellQuote(spec.extra()),
		OptionsEnv:    optionsEnv(spec.Options),
		Options:       cliOptions(spec.Options),
	}
	return Snippets{
		Systemd:   mustRender(systemdTmpl, data),
		ManualCLI: mustRender(manualCLITmpls[l], data),
	}
}

// extra resolves the pip extra to install for this backend.
func (s Spec) extra() string {
	if s.Extra != "" {
		return s.Extra
	}
	return baseCliExtra
}

// snippetOptions are the options a rendered command should pre-fill: the ones the
// driver cannot default for the operator. The rest are catalog-only — see
// [Option.InSnippet].
func snippetOptions(opts []Option) []Option {
	shown := make([]Option, 0, len(opts))
	for _, opt := range opts {
		if opt.InSnippet {
			shown = append(shown, opt)
		}
	}
	return shown
}

// cliOptions renders a kind's config as trailing `-o key=value` CLI flags, or ""
// when none of them belong in a snippet.
func cliOptions(opts []Option) string {
	var b strings.Builder
	for _, opt := range snippetOptions(opts) {
		fmt.Fprintf(&b, " %s %s=%s", optionFlag, opt.Key, opt.Example)
	}
	return b.String()
}

// optionsEnv renders a kind's config as the DRIVER_OPTIONS environment variable
// install-systemd.sh reads, or "" when none of them belong in a snippet.
func optionsEnv(opts []Option) string {
	shown := snippetOptions(opts)
	if len(shown) == 0 {
		return ""
	}
	pairs := make([]string, len(shown))
	for i, opt := range shown {
		pairs[i] = opt.Key + "=" + opt.Example
	}
	return fmt.Sprintf(" DRIVER_OPTIONS='%s'", strings.Join(pairs, ";"))
}

// installCommand is the bare-SDK install for a language. Only the Python SDK
// ships today; the others are named ahead of their Phase 4 release, following
// qpi-client's per-language convention.
func installCommand(language Language) string {
	switch language {
	case TypeScript:
		return "npm install qpi-driver"
	case Go:
		return "go get github.com/sopherapps/qpi/qpi-driver/go"
	default:
		return "pip install " + baseSdkPackage
	}
}

// stub sketches a custom driver — subclass the SDK base, dispatch on the event type, emit
// results.
func stub(language Language) string {
	switch language {
	case TypeScript:
		return `import { QpiDriver, Event, EventType } from "qpi-driver";

class MyDriver extends QpiDriver {
  handleEvent(event: Event): void {
    if (event.type === EventType.JobDispatch) {
      const results = runMyBackend(event.payload);
      this.emit(new Event(EventType.JobResult, {
        job_id: event.payload.job_id, status: "completed", results,
      }));
    }
  }
}
`
	case Go:
		return `package main

import qpidriver "github.com/sopherapps/qpi/qpi-driver/go"

type MyDriver struct{ qpidriver.Base }

func (d *MyDriver) HandleEvent(event qpidriver.Event) {
	if event.Type == qpidriver.JobDispatch {
		results := runMyBackend(event.Payload)
		_ = d.Emit(qpidriver.NewEvent(qpidriver.JobResult, d.DriverName(), map[string]any{
			"job_id": event.Payload["job_id"], "status": "completed", "results": results,
		}))
	}
}
`
	default:
		return `from qpi_driver import QpiDriver, Event, EventType

class MyDriver(QpiDriver):
    def handle_event(self, event: Event) -> None:
        if event.type == EventType.JOB_DISPATCH:
            results = run_my_backend(event.payload)
            self.emit(Event(
                type=EventType.JOB_RESULT,
                driver=self.name,
                payload={"job_id": event.payload["job_id"], "status": "completed", "results": results},
            ))
`
	}
}
