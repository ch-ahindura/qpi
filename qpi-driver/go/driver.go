package qpidriver

import (
	"bytes"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	"go.nanomsg.org/mangos/v3"
	"go.nanomsg.org/mangos/v3/protocol/pull"
	"go.nanomsg.org/mangos/v3/protocol/push"

	_ "go.nanomsg.org/mangos/v3/transport/tlstcp"
)

// DefaultRecvTimeout is how long the inbound receive loop blocks per attempt
// before checking whether the driver has been asked to stop.
const DefaultRecvTimeout = 200 * time.Millisecond

const defaultCAFilePath = "./bin/qpi.ca.pem"

// Config holds the connection settings a driver needs to reach QPI-UI. The
// zero value is not usable; at minimum QpiAddr and Token are required.
type Config struct {
	// QpiAddr is the full URL of the QPI-UI server, e.g. "https://qpi.example.com".
	QpiAddr string
	// Token is the driver's access token; it identifies the driver (and its
	// QPU) to QPI-UI. There is no name field: a driver's display label belongs to
	// the admin who typed it into the dashboard, and the handshake returns it.
	Token string
	// CaFingerprint is the expected SHA-256 (hex) of the server root CA,
	// pinned over TLS. When empty, the fingerprint check is skipped.
	CaFingerprint string
	// CaFilePath is where the downloaded root CA certificate is written.
	// Defaults to "./bin/qpi.ca.pem".
	CaFilePath string
	// RecvTimeout is how long the receive loop blocks per attempt. Defaults to
	// DefaultRecvTimeout.
	RecvTimeout time.Duration
}

// Handler acts on a single inbound event, switching on event.Type. A driver
// implements it by embedding [Base] and defining this method.
type Handler interface {
	// HandleEvent acts on one inbound event. An event a driver does not care
	// about is simply ignored; there is no application-level ACK/NACK
	// (RFC 0001 §4).
	HandleEvent(event Event)
}

// Driver is what [Run] accepts: a [Handler] that also embeds [Base]. The
// unexported base() method means only types embedding Base in this package can
// satisfy it, so Run can always reach the transport a driver carries.
type Driver interface {
	Handler
	base() *Base
}

// Base owns the transport shared by every driver: the outbound NNG PUSH socket
// it emits on, the inbound NNG PULL socket QPI-UI pushes to, TLS with the
// pinned root CA, and the receive loop. Embed it in a driver struct and
// implement [Handler.HandleEvent]; Emit and Every are then available on the
// driver directly.
type Base struct {
	cfg      Config
	name     string
	handler  Handler
	out      mangos.Socket
	emitMu   sync.Mutex
	periodic []periodicTask
	stop     chan struct{}
	stopOnce sync.Once
	wg       sync.WaitGroup
}

type periodicTask struct {
	interval time.Duration
	fn       func()
}

func (b *Base) base() *Base { return b }

// DriverName is the display label QPI-UI has this driver registered under, used
// to tag emitted events. It comes from the drivers/connect response, so it is
// empty until [Run] has connected.
func (b *Base) DriverName() string { return b.name }

// Emit sends an event upward to QPI-UI over the outbound NNG channel. Delivery
// is best-effort: if nothing is listening the event is dropped rather than
// buffered (RFC 0001 §5). It returns an error if called before the driver has
// connected.
func (b *Base) Emit(event Event) error {
	if b.out == nil {
		return errors.New("qpidriver: cannot emit before the driver is running")
	}
	raw, err := json.Marshal(event)
	if err != nil {
		return fmt.Errorf("qpidriver: encoding event: %w", err)
	}
	b.emitMu.Lock()
	defer b.emitMu.Unlock()
	return b.out.Send(raw)
}

// Every registers fn to run every interval while the driver runs. It is used
// by drivers that report on their own schedule — e.g. a monitor that emits a
// reading on a timer — independently of any inbound event. Register callbacks
// before calling [Run].
func (b *Base) Every(interval time.Duration, fn func()) {
	b.periodic = append(b.periodic, periodicTask{interval: interval, fn: fn})
}

// Stop signals a running driver to shut down. It is safe to call more than
// once and from any goroutine.
func (b *Base) Stop() {
	b.stopOnce.Do(func() { close(b.stop) })
}

// Run connects a driver to QPI-UI and processes events until the process is
// interrupted (SIGINT/SIGTERM) or [Base.Stop] is called. It performs the
// handshake, opens the outbound channel, starts any periodic callbacks, then
// blocks on the inbound receive loop.
func Run(d Driver, cfg Config) error {
	b := d.base()
	b.cfg = cfg
	b.handler = d
	b.stop = make(chan struct{})

	conn, err := connect(cfg)
	if err != nil {
		return err
	}
	// The server owns the label, so the driver only knows it from here on.
	b.name = conn.name

	tlsConfig, err := buildTLSConfig(conn)
	if err != nil {
		return err
	}

	out, err := push.NewSocket()
	if err != nil {
		return fmt.Errorf("qpidriver: creating PUSH socket: %w", err)
	}
	outAddr := fmt.Sprintf("tls+tcp://%s:%d", conn.host, conn.outPort)
	if err := out.DialOptions(outAddr, tlsDialOptions(tlsConfig)); err != nil {
		return fmt.Errorf("qpidriver: dialing %s: %w", outAddr, err)
	}
	b.out = out
	log.Printf("[qpidriver] NNG PUSH connected to %s", outAddr)

	b.startPeriodic()
	b.installSignalHandler()

	err = b.recvLoop(conn, tlsConfig)
	b.shutdown()
	return err
}

func (b *Base) recvLoop(conn *connection, tlsConfig *tls.Config) error {
	in, err := pull.NewSocket()
	if err != nil {
		return fmt.Errorf("qpidriver: creating PULL socket: %w", err)
	}
	defer in.Close()

	recvTimeout := b.cfg.RecvTimeout
	if recvTimeout <= 0 {
		recvTimeout = DefaultRecvTimeout
	}
	if err := in.SetOption(mangos.OptionRecvDeadline, recvTimeout); err != nil {
		return fmt.Errorf("qpidriver: setting PULL recv deadline: %w", err)
	}
	inAddr := fmt.Sprintf("tls+tcp://%s:%d", conn.host, conn.inPort)
	if err := in.DialOptions(inAddr, tlsDialOptions(tlsConfig)); err != nil {
		return fmt.Errorf("qpidriver: dialing %s: %w", inAddr, err)
	}
	log.Printf("[qpidriver] NNG PULL connected to %s", inAddr)

	for {
		select {
		case <-b.stop:
			return nil
		default:
		}

		raw, err := in.Recv()
		if errors.Is(err, mangos.ErrRecvTimeout) {
			continue
		}
		if errors.Is(err, mangos.ErrClosed) {
			return nil
		}
		if err != nil {
			log.Printf("[qpidriver] receive error: %v", err)
			continue
		}

		event, err := decodeEvent(raw)
		if err != nil {
			log.Printf("[qpidriver] dropping malformed inbound message: %v", err)
			continue
		}
		b.deliver(event)
	}
}

// deliver passes an event to the driver's HandleEvent, recovering from a panic
// so one bad event does not take the driver down. There is no ACK/NACK: a
// dropped event is only logged (RFC 0001 §4).
func (b *Base) deliver(event Event) {
	defer func() {
		if r := recover(); r != nil {
			log.Printf("[qpidriver] dropping event %s of type %s: handler panicked: %v",
				event.ID, event.Type, r)
		}
	}()
	b.handler.HandleEvent(event)
}

func (b *Base) startPeriodic() {
	for _, task := range b.periodic {
		b.wg.Add(1)
		go func(task periodicTask) {
			defer b.wg.Done()
			ticker := time.NewTicker(task.interval)
			defer ticker.Stop()
			for {
				select {
				case <-b.stop:
					return
				case <-ticker.C:
					b.runPeriodic(task.fn)
				}
			}
		}(task)
	}
}

func (b *Base) runPeriodic(fn func()) {
	defer func() {
		if r := recover(); r != nil {
			log.Printf("[qpidriver] periodic callback panicked: %v", r)
		}
	}()
	fn()
}

func (b *Base) installSignalHandler() {
	sigs := make(chan os.Signal, 1)
	signal.Notify(sigs, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		select {
		case <-sigs:
			log.Print("[qpidriver] shutdown signal received")
			b.Stop()
		case <-b.stop:
		}
		signal.Stop(sigs)
	}()
}

func (b *Base) shutdown() {
	log.Print("[qpidriver] shutting down driver...")
	b.Stop()
	b.wg.Wait()
	if b.out != nil {
		b.out.Close()
		b.out = nil
	}
	log.Print("[qpidriver] shutdown complete.")
}

// connection holds the transport coordinates resolved during the handshake.
type connection struct {
	name    string
	host    string
	inPort  int
	outPort int
	caFile  string
}

type connectRequest struct {
	Token string `json:"token"`
}

type connectResponse struct {
	Name       string `json:"name"`
	NNGHost    string `json:"nng_host"`
	NNGInPort  int    `json:"nng_in_port"`
	NNGOutPort int    `json:"nng_out_port"`
}

// connect handshakes with QPI-UI over the shared drivers/connect endpoint. The
// token identifies the driver (and, transitively, its QPU), and QPI-UI returns
// the NNG host and ports. Every driver connects the same way; what differs is
// only which events it handles and emits (RFC 0001 §3, §8).
func connect(cfg Config) (*connection, error) {
	addr := normalizeQpiAddr(cfg.QpiAddr)
	body, err := json.Marshal(connectRequest{Token: cfg.Token})
	if err != nil {
		return nil, fmt.Errorf("qpidriver: encoding connect request: %w", err)
	}

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Post(addr+"/api/op/drivers/connect", "application/json", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("qpidriver: connect request failed: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		msg, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("qpidriver: connect rejected (%d): %s", resp.StatusCode, strings.TrimSpace(string(msg)))
	}

	var data connectResponse
	if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
		return nil, fmt.Errorf("qpidriver: decoding connect response: %w", err)
	}

	caFilePath := cfg.CaFilePath
	if caFilePath == "" {
		caFilePath = defaultCAFilePath
	}
	caFile, err := downloadRootCACert(addr, cfg.CaFingerprint, caFilePath, 10*time.Second)
	if err != nil {
		return nil, err
	}

	return &connection{
		name:    data.Name,
		host:    data.NNGHost,
		inPort:  data.NNGInPort,
		outPort: data.NNGOutPort,
		caFile:  caFile,
	}, nil
}

// downloadRootCACert fetches the server root CA, verifies its SHA-256
// fingerprint against the pinned value, and writes it to dst. The fingerprint
// is the hex SHA-256 of the certificate's DER bytes, matching the Python SDK.
// An empty expected fingerprint skips the check.
func downloadRootCACert(qpiAddr, expected, dst string, timeout time.Duration) (string, error) {
	client := &http.Client{Timeout: timeout}
	resp, err := client.Get(qpiAddr + "/api/pub/root-ca.pem")
	if err != nil {
		return "", fmt.Errorf("qpidriver: downloading root CA: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("qpidriver: downloading root CA: status %d", resp.StatusCode)
	}
	pemBytes, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", fmt.Errorf("qpidriver: reading root CA: %w", err)
	}

	if expected != "" {
		block, _ := pem.Decode(pemBytes)
		if block == nil {
			return "", errors.New("qpidriver: root CA is not valid PEM")
		}
		sum := sha256.Sum256(block.Bytes)
		got := hex.EncodeToString(sum[:])
		if !strings.EqualFold(got, expected) {
			return "", fmt.Errorf(
				"qpidriver: CRITICAL SECURITY ERROR: downloaded CA fingerprint (%s) "+
					"does not match the expected value (%s); the download channel may be compromised",
				got, expected)
		}
	}

	if dir := filepath.Dir(dst); dir != "" {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return "", fmt.Errorf("qpidriver: creating CA directory: %w", err)
		}
	}
	if err := os.WriteFile(dst, pemBytes, 0o644); err != nil {
		return "", fmt.Errorf("qpidriver: writing CA file: %w", err)
	}
	return dst, nil
}

// tlsDialOptions carries the TLS config as a mangos dial option. OptionTLSConfig
// is a transport option applied to the dialer (as the server applies it to its
// listener), not a socket option — setting it via Socket.SetOption is rejected
// as "invalid or unsupported option".
func tlsDialOptions(tlsConfig *tls.Config) map[string]any {
	return map[string]any{mangos.OptionTLSConfig: tlsConfig}
}

func buildTLSConfig(conn *connection) (*tls.Config, error) {
	pemBytes, err := os.ReadFile(conn.caFile)
	if err != nil {
		return nil, fmt.Errorf("qpidriver: reading CA file: %w", err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(pemBytes) {
		return nil, errors.New("qpidriver: no certificates found in CA file")
	}
	return &tls.Config{
		RootCAs:    pool,
		ServerName: conn.host,
		MinVersion: tls.VersionTLS12,
	}, nil
}

// normalizeQpiAddr ensures the address has a scheme and no trailing slash, so
// callers can pass a bare host:port pair.
func normalizeQpiAddr(addr string) string {
	if !strings.Contains(addr, "://") {
		addr = "http://" + addr
	}
	return strings.TrimRight(addr, "/")
}
