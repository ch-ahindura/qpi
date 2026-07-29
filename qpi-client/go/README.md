<pre align="center">
 ██████╗ ██████╗ ██╗
██╔═══██╗██╔══██╗██║
██║   ██║██████╔╝██║
██║▄▄ ██║██╔═══╝ ██║
╚██████╔╝██║     ██║
 ╚══▀▀═╝ ╚═╝     ╚═╝
</pre>

<h1 align="center">QPI Go Client</h1>

<p align="center">
  <a href="https://pkg.go.dev/github.com/sopherapps/qpi/qpi-client/go"><img src="https://pkg.go.dev/badge/github.com/sopherapps/qpi/qpi-client/go.svg" alt="Go Reference"></a>
  <a href="https://github.com/sopherapps/qpi/releases"><img src="https://img.shields.io/github/v/tag/sopherapps/qpi?label=version" alt="GitHub Tag"></a>
  <img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License">
</p>

<p align="center">
  Go client SDK for the <a href="https://github.com/sopherapps/qpi">QPI</a> quantum computing platform.
  Zero third-party dependencies — uses only the Go standard library.
</p>

<p align="center">
  <strong><a href="https://sopherapps.github.io/qpi/clients/go/">📚 Read the Documentation</a></strong>
</p>

---

## Install

```bash
go get github.com/sopherapps/qpi/qpi-client/go
```

Requires **Go ≥ 1.21**.

---

## Quick Start

<!-- docs-check: compile=go-client-quickstart -->
```go
package main

import (
    "context"
    "fmt"
    "log"
    "time"

    qpiclient "github.com/sopherapps/qpi/qpi-client/go"
)

func main() {
    client := qpiclient.NewClient("http://localhost:8090", "my-api-token")

    // Submit a job
    id, err := client.SubmitJob(context.Background(), qpiclient.JobSubmitRequest{
        Circuits: []qpiclient.CircuitPayload{
            {Circuit: "OPENQASM 3.0; include \"stdgates.inc\"; qubit[2] q; bit[2] c; h q[0]; cx q[0], q[1]; c = measure q;"},
        },
        Shots: 1024,
    })
    if err != nil {
        log.Fatal(err)
    }
    fmt.Println("Job ID:", id)

    // Wait for completion
    job, err := client.WaitForJob(context.Background(), id, 5*time.Second)
    if err != nil {
        log.Fatal(err)
    }
    fmt.Println("Status:", job.Status)
    fmt.Println("Results:", job.Results)
}
```

---

## API Overview

| Method | Description |
|--------|-------------|
| `NewClient(baseURL, apiToken)` | Create a new client |
| `SubmitJob(ctx, req)` | Submit a quantum job |
| `GetJob(ctx, jobID)` | Retrieve a job by ID |
| `ListJobs(ctx)` | List all jobs for the authenticated user |
| `CancelJob(ctx, jobID)` | Request job cancellation |
| `WaitForJob(ctx, jobID, pollInterval)` | Poll until the job reaches a terminal state |
| `ListQpus(ctx)` | List all online QPUs |
| `GetQpu(ctx, name)` | Retrieve a single QPU |
| `CreateQpu(ctx, req)` | Create a new QPU record (admin) |
| `ConnectQpu(ctx, req)` | Connect a QPU driver node |
| `ListTimeSlots(ctx)` | List booking slots |
| `ListNotifications(ctx)` | List visible notifications |

---

## Documentation

- [Main QPI Repository](https://github.com/sopherapps/qpi)
- [Go Package Reference](https://pkg.go.dev/github.com/sopherapps/qpi/qpi-client/go)

---

## License

MIT — see the [main repository](https://github.com/sopherapps/qpi/blob/main/LICENSE) for details.
