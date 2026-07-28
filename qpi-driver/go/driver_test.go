package qpidriver

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"math/big"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

// recordingDriver is a minimal driver that captures the events it is handed,
// exactly how a real driver is written: embed Base, implement HandleEvent.
type recordingDriver struct {
	Base
	mu     sync.Mutex
	events []Event
}

func (d *recordingDriver) HandleEvent(event Event) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.events = append(d.events, event)
}

func TestDeliverDispatchesToHandler(t *testing.T) {
	d := &recordingDriver{}
	d.handler = d
	d.deliver(NewEvent(JobDispatch, "qpu_1", map[string]any{"job_id": "j1"}))

	if len(d.events) != 1 || d.events[0].Type != JobDispatch {
		t.Fatalf("expected one JobDispatch event, got %+v", d.events)
	}
}

func TestDeliverRecoversFromPanic(t *testing.T) {
	d := &panicDriver{}
	d.handler = d
	d.deliver(NewEvent(JobDispatch, "qpu_1", nil)) // must not panic the test
}

type panicDriver struct {
	Base
}

func (d *panicDriver) HandleEvent(event Event) { panic("boom") }

func TestEmitBeforeRunFails(t *testing.T) {
	d := &recordingDriver{}
	if err := d.Emit(NewEvent(JobResult, "qpu_1", nil)); err == nil {
		t.Error("expected Emit to fail before the driver is running")
	}
}

func TestEveryRunsCallbackUntilStopped(t *testing.T) {
	d := &recordingDriver{}
	d.stop = make(chan struct{})

	var calls int
	var mu sync.Mutex
	d.Every(5*time.Millisecond, func() {
		mu.Lock()
		calls++
		mu.Unlock()
	})
	d.startPeriodic()

	time.Sleep(40 * time.Millisecond)
	d.Stop()
	d.wg.Wait()

	mu.Lock()
	defer mu.Unlock()
	if calls == 0 {
		t.Error("expected the periodic callback to run at least once")
	}
}

func TestConnectResolvesPortsAndPinsCA(t *testing.T) {
	pemBytes, fingerprint := selfSignedCert(t)

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/op/drivers/connect":
			_ = json.NewEncoder(w).Encode(connectResponse{
				Name: "cryostat-1", NNGHost: "127.0.0.1", NNGInPort: 5001, NNGOutPort: 5002,
			})
		case "/api/pub/root-ca.pem":
			w.Header().Set("Content-Type", "application/x-pem-file")
			_, _ = w.Write(pemBytes)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	caPath := filepath.Join(t.TempDir(), "qpi.ca.pem")
	conn, err := connect(Config{
		QpiAddr:       server.URL,
		Token:         "tok_abc",
		CaFingerprint: fingerprint,
		CaFilePath:    caPath,
	})
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	if conn.host != "127.0.0.1" || conn.inPort != 5001 || conn.outPort != 5002 {
		t.Errorf("unexpected connection: %+v", conn)
	}
	// The display label is the server's to hand out: the driver asserts no name in
	// the connect body and learns it from the response.
	if conn.name != "cryostat-1" {
		t.Errorf("conn.name = %q, want the name the server returned", conn.name)
	}

	if _, err := buildTLSConfig(conn); err != nil {
		t.Errorf("buildTLSConfig: %v", err)
	}
}

func TestConnectRejectsFingerprintMismatch(t *testing.T) {
	pemBytes, _ := selfSignedCert(t)

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/op/drivers/connect":
			_ = json.NewEncoder(w).Encode(connectResponse{NNGHost: "127.0.0.1", NNGInPort: 1, NNGOutPort: 2})
		case "/api/pub/root-ca.pem":
			_, _ = w.Write(pemBytes)
		}
	}))
	defer server.Close()

	_, err := connect(Config{
		QpiAddr:       server.URL,
		Token:         "tok",
		CaFingerprint: "deadbeef",
		CaFilePath:    filepath.Join(t.TempDir(), "qpi.ca.pem"),
	})
	if err == nil {
		t.Fatal("expected a fingerprint mismatch error")
	}
}

func TestConnectSurfacesRejection(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "disabled driver", http.StatusForbidden)
	}))
	defer server.Close()

	_, err := connect(Config{QpiAddr: server.URL, Token: "tok"})
	if err == nil {
		t.Fatal("expected connect to surface a non-200 rejection")
	}
}

// selfSignedCert returns a PEM-encoded self-signed certificate and its pinned
// fingerprint (hex SHA-256 of the DER bytes), matching how the SDK verifies
// the downloaded root CA.
func selfSignedCert(t *testing.T) (pemBytes []byte, fingerprint string) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}
	template := x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: "qpi-test-ca"},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(time.Hour),
		IsCA:         true,
	}
	der, err := x509.CreateCertificate(rand.Reader, &template, &template, &key.PublicKey, key)
	if err != nil {
		t.Fatalf("create certificate: %v", err)
	}
	sum := sha256.Sum256(der)
	pemBytes = pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	return pemBytes, hex.EncodeToString(sum[:])
}
