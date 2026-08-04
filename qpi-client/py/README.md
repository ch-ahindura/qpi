<pre align="center">
 ██████╗ ██████╗ ██╗
██╔═══██╗██╔══██╗██║
██║   ██║██████╔╝██║
██║▄▄ ██║██╔═══╝ ██║
╚██████╔╝██║     ██║
 ╚══▀▀═╝ ╚═╝     ╚═╝
</pre>

<h1 align="center">QPI Python Client</h1>

<p align="center">
  <a href="https://badge.fury.io/py/qpi-client"><img src="https://badge.fury.io/py/qpi-client.svg" alt="PyPI version"></a>
  <a href="https://github.com/sopherapps/qpi/actions/workflows/ci.yml"><img src="https://github.com/sopherapps/qpi/actions/workflows/ci.yml/badge.svg" alt="CI/CD Workflow"></a>
  <a href="https://github.com/sopherapps/qpi/releases"><img src="https://img.shields.io/github/v/tag/sopherapps/qpi?label=version" alt="GitHub Tag"></a>
  <img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License">
</p>

<p align="center">
  Python client SDK for the <a href="https://github.com/sopherapps/qpi">QPI</a> quantum computing platform.
  Includes both a low-level HTTP client and a Qiskit-compatible backend.
</p>

<p align="center">
  <strong><a href="https://sopherapps.github.io/qpi/clients/python/">📚 Read the Documentation</a></strong>
</p>

---

## Install

```bash
pip install qpi-client
```

Requires **Python ≥ 3.10**.

---

## Quick Start

### Low-level client

```python
from qpi_client import QPIClient

client = QPIClient("http://localhost:8090", api_token="my-token")

# Submit a job
job_id = client.submit_job([
    {"circuit": 'OPENQASM 3.0; include "stdgates.inc"; qubit[2] q; bit[2] c; h q[0]; cx q[0], q[1]; c = measure q;'}
], shots=1024)
print("Job ID:", job_id)

# Wait for completion
job = client.job(job_id)
result = job.result(timeout=120)
print(result.get_counts())
```

### Qiskit integration

```python
from qiskit.circuit import QuantumCircuit
from qpi_client import QPIClient

client = QPIClient("http://localhost:8090", api_token="my-token")
# The name is the QPU's; its qubit count is read from the server.
backend = client.get_backend("mock")

qc = QuantumCircuit(2, 2)
qc.h(0)
qc.cx(0, 1)
qc.measure([0, 1], [0, 1])

job = backend.run(qc, shots=4096)
result = job.result(timeout=120)
print(result.get_counts())
```

### Submit raw QASM with parameters

```python
job = backend.run(
    qasm='OPENQASM 3.0; include "stdgates.inc"; qubit[1] q; bit[1] c; rx({{theta}}) q[0]; c = measure q;',
    parameter_values=[[0.5], [1.0]],
    shots=1024
)
result = job.result()
```

---

## API Overview

### `QPIClient`

| Method | Description |
|--------|-------------|
| `QPIClient(base_url, api_token)` | Create a new client |
| `submit_job(circuits, shots, ...)` | Submit a quantum job |
| `job(job_id)` | Retrieve a job by ID (returns `QPIJob`) |
| `list_jobs()` | List all jobs for the authenticated user |
| `cancel_job(job_id)` | Request job cancellation |
| `get_backend(name)` | Get a Qiskit-compatible backend |

### `QPIBackend` (Qiskit)

| Method | Description |
|--------|-------------|
| `backend.run(circuit, shots)` | Run a `QuantumCircuit` |
| `backend.run(qasm, parameter_values, shots)` | Run raw QASM with parameter bindings |
| `backend.job(job_id)` | Retrieve a past job |

---

## Documentation

- [Main QPI Repository](https://github.com/sopherapps/qpi)
- [PyPI Project Page](https://pypi.org/project/qpi-client/)

---

## License

MIT — see the [main repository](https://github.com/sopherapps/qpi/blob/main/LICENSE) for details.
