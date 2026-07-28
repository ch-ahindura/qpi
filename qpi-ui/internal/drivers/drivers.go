// Package drivers is QPI-UI's catalog of registerable driver backends: which
// kinds exist, how each is installed and launched, and which events it takes
// part in (RFC 0001 §3, §7).
//
// A backend is described by a data-only Spec, and the whole catalog is a
// Registry of Specs. Adding a new backend — a new executor or a new monitor —
// means registering one Spec in catalog.go; no other server code changes. A
// backend's operation (`qpi-driver start --operation process --device <kind>`,
// or `--operation monitor`) and its options are just
// Spec fields, so a differently-shaped backend is more data rather than a new
// branch.
package drivers

// Language is an SDK language a driver can be written in (RFC 0001 §2).
type Language string

const (
	Python     Language = "python"
	TypeScript Language = "typescript"
	Go         Language = "go"
)

// Kind identifies a known official driver backend, or Custom for a driver
// QPI-UI has no built-in knowledge of (RFC 0001 §3).
type Kind string

const (
	Mock         Kind = "mock"
	QiskitAer    Kind = "qiskit_aer"
	Quantify     Kind = "quantify"
	Qblox        Kind = "qblox"
	Presto       Kind = "presto"
	BlueforsGen1 Kind = "bluefors_gen1"
	Custom       Kind = "custom"
)

// Operation is what a driver does — the category it belongs to — and doubles as
// what `qpi-driver start --operation` is given: a Process driver runs jobs pushed
// to it (a QPU), a Monitor driver reports upward on its own schedule (a
// cryostat monitor). New operations are new constants here (RFC 0001 §4, §7).
type Operation string

const (
	Process Operation = "process"
	Monitor Operation = "monitor"
)

// Option is one `-o key=value` setting a monitor kind needs, carrying an
// example value so the setup snippets show a ready-to-edit command.
type Option struct {
	Key     string
	Example string
}

// Spec is the data-only description of one official driver backend: how it is
// installed, how the qpi-driver CLI launches it, and which events it takes part
// in. Custom drivers have no Spec — their events are chosen at registration and
// they run code the operator writes.
type Spec struct {
	Kind Kind
	// Operation is what this backend does, and what the CLI is told to run:
	// `qpi-driver start --operation <Operation> --device <kind> …`.
	Operation Operation
	// Extra is the qpi-driver Python extra that ships this backend, e.g.
	// "qpi-driver[cli,qblox]". Empty means the base CLI extra (mock, presto).
	Extra string
	// Events are the event-type names this kind participates in. They must be
	// values QPI-UI has a handler for; a test guards that invariant.
	Events []string
	// Options are the per-kind `-o key=value` settings the operation needs,
	// shown pre-filled in the snippets. Nil when the operation's defaults are
	// enough (e.g. an executor).
	Options []Option
}

// Registry is the set of official driver backends QPI-UI knows about, keyed by
// kind. Custom is deliberately absent — it is handled as the catch-all.
type Registry struct {
	specs map[Kind]Spec
}

// NewRegistry builds a registry from the given specs. Tests use it to assemble
// a catalog in isolation; the server uses the package-level Default.
func NewRegistry(specs ...Spec) *Registry {
	r := &Registry{specs: make(map[Kind]Spec, len(specs))}
	for _, spec := range specs {
		r.specs[spec.Kind] = spec
	}
	return r
}

// Lookup returns the spec for an official kind, and whether it is registered.
func (r *Registry) Lookup(kind Kind) (Spec, bool) {
	spec, ok := r.specs[kind]
	return spec, ok
}

// KnownKind reports whether kind is one this QPI-UI version recognises, which
// includes Custom.
func (r *Registry) KnownKind(kind Kind) bool {
	if kind == Custom {
		return true
	}
	_, ok := r.specs[kind]
	return ok
}

// Kinds returns every official kind registered in the catalog, in no
// particular order.
func (r *Registry) Kinds() []Kind {
	kinds := make([]Kind, 0, len(r.specs))
	for kind := range r.specs {
		kinds = append(kinds, kind)
	}
	return kinds
}

// Events returns the fixed event set an official kind participates in, or nil
// for Custom (whose events are chosen at registration instead).
func (r *Registry) Events(kind Kind) []string {
	spec, ok := r.specs[kind]
	if !ok {
		return nil
	}
	events := make([]string, len(spec.Events))
	copy(events, spec.Events)
	return events
}

// KnownLanguage reports whether language is one of the SDK languages.
func KnownLanguage(language Language) bool {
	switch language {
	case Python, TypeScript, Go:
		return true
	default:
		return false
	}
}
