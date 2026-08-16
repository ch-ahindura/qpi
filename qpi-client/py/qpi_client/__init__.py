"""QPI Client SDK for Python.

A client library for interacting with the QPI quantum computing platform.
Provides both a low-level HTTP client and Qiskit-compatible backend/job classes.

Usage::

    from qpi_client import QPIClient

    # Low-level client
    client = QPIClient("http://localhost:8090", api_token="my-token")
    job_id = client.submit_job([{"circuit": "OPENQASM 3.0; ..."}])

    # Qiskit integration (preferred)
    backend = client.get_backend(name="mock")
    job = backend.run(circuit=my_circuit, shots=1024)
    result = job.result()

    # Or submit raw QASM
    job = backend.run(qasm="OPENQASM 3.0; ...", params=[[0.5]])
    result = job.result()

    # Retrieve a past job
    past_job = client.job(job_id)
    past_job = backend.job(job_id)
"""

import importlib.metadata

try:
    __version__ = importlib.metadata.version("qpi-client")
except importlib.metadata.PackageNotFoundError:
    __version__ = "0.4.2"

from qpi_client.client import QPIClient
from qpi_client.provider import QPIBackend, QPIJob

__all__ = ["__version__", "QPIClient", "QPIBackend", "QPIJob"]
