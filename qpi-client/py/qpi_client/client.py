"""Low-level HTTP client for the QPI server REST API.

This module provides :class:`QPIClient`, a thin wrapper around ``requests.Session``
that handles authentication and serialisation for every QPI API endpoint.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:
    from qpi_client.provider import QPIBackend, QPIJob


class QPIClient:
    """Low-level HTTP wrapper for the QPI server API.

    Args:
        base_url: Root URL of the QPI server (e.g. ``"http://localhost:8090"``).
        api_token: Optional API token used for authentication via the
            ``X-API-Token`` header. When *None*, no token header is sent
            (useful for cookie/JWT-based auth in browser contexts).
    """

    def __init__(self, base_url: str, api_token: str | None = None) -> None:
        self.base_url: str = base_url.rstrip("/")
        self.api_token: str | None = api_token
        self._session: requests.Session = requests.Session()
        self._session.headers["Content-Type"] = "application/json"
        if api_token:
            self._session.headers["X-API-Token"] = api_token

    def submit_job(
        self,
        circuits: list[dict[str, Any]],
        shots: int = 1024,
        meas_level: int = 2,
        meas_return: str = "single",
        qpu_target: str = "",
    ) -> str:
        """Submit a quantum job to the server.

        Args:
            circuits: A list of circuit payload dicts.  Each dict **must**
                contain a ``"circuit"`` key whose value is an OpenQASM 3
                string.  Optional keys: ``"parameter_values"``, ``"shots"``.
            shots: Default number of shots for every circuit.
            meas_level: Measurement level (``2`` = classified bits).
            meas_return: ``"single"`` or ``"avg"``.
            qpu_target: Optional QPU routing hint.

        Returns:
            The server-assigned job ID as a string.

        Raises:
            requests.HTTPError: If the server returns a non-2xx status.
        """
        payload: dict[str, Any] = {
            "circuits": circuits,
            "shots": shots,
            "meas_level": meas_level,
            "meas_return": meas_return,
        }
        if qpu_target:
            payload["qpu_target"] = qpu_target

        resp = self._session.post(f"{self.base_url}/api/jobs", json=payload)
        resp.raise_for_status()
        data = resp.json()

        # The server may return the ID at the top level or nested.
        job_id: str = data.get("id") or data.get("job_id", "")
        if not job_id:
            raise ValueError(f"Server response did not contain a job ID: {data!r}")
        return job_id

    def get_job(self, job_id: str) -> dict[str, Any]:
        """Retrieve full details for *job_id*.

        Returns:
            A dict with at least ``"id"``, ``"status"``, ``"payload"``,
            ``"results"``, ``"created"``, and ``"updated"`` keys.

        Raises:
            requests.HTTPError: If the server returns a non-2xx status.
        """
        resp = self._session.get(f"{self.base_url}/api/jobs/{job_id}")
        resp.raise_for_status()
        return resp.json()

    def list_jobs(self) -> list[dict[str, Any]]:
        """List all jobs belonging to the authenticated user.

        Returns:
            A list of job-record dicts.

        Raises:
            requests.HTTPError: If the server returns a non-2xx status.
        """
        resp = self._session.get(f"{self.base_url}/api/jobs")
        resp.raise_for_status()
        data = resp.json()
        # The response might be a bare list or wrapped in {"jobs": [...]}.
        if isinstance(data, list):
            return data
        return data.get("jobs", [])

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        """Request cancellation of *job_id*.

        Returns:
            The updated job-record dict.

        Raises:
            requests.HTTPError: If the server returns a non-2xx status.
        """
        resp = self._session.post(f"{self.base_url}/api/jobs/{job_id}/cancel")
        resp.raise_for_status()
        return resp.json()

    def get_backend(self, name: str = "qpi") -> "QPIBackend":
        """Return a :class:`QPIBackend` handle for the named QPU.

        Args:
            name: Backend / QPU name (e.g. ``"mock"``, ``"qiskit_aer"``).

        Returns:
            A configured :class:`QPIBackend` instance bound to this client.
        """
        from qpi_client.provider import QPIBackend

        return QPIBackend(self, name=name)

    def job(self, job_id: str) -> "QPIJob":
        """Retrieve an existing job by ID.

        Args:
            job_id: The server-assigned job ID.

        Returns:
            A :class:`QPIJob` handle (backend will be *None*).
        """
        from qpi_client.provider import QPIJob

        return QPIJob(backend=None, job_id=job_id, client=self)

    def list_qpus(self) -> list[dict[str, Any]]:
        """List all online QPUs.

        Returns:
            A list of QPU record dicts.
        """
        resp = self._session.get(f"{self.base_url}/api/qpus")
        resp.raise_for_status()
        return resp.json()

    def get_qpu(self, name: str) -> dict[str, Any]:
        """Retrieve a single QPU by name.

        Args:
            name: The QPU's unique name.

        Returns:
            A QPU record dict.
        """
        resp = self._session.get(f"{self.base_url}/api/qpus/{name}")
        resp.raise_for_status()
        return resp.json()

    def create_qpu(
        self,
        name: str,
        executor_type: str | None = None,
        num_qubits: int | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        """Create a new QPU record (admin-only).

        The server generates a random ``access_token`` and returns it in
        plain text exactly once; only the hash is persisted.

        Args:
            name: QPU name.
            executor_type: Type of executor.
            num_qubits: Number of qubits on the device.
            enabled: Whether the QPU should be enabled (default ``True``).

        Returns:
            A dict containing at least ``id``, ``name``, ``access_token``,
            ``executor_type``, ``status``, and ``enabled``.
        """
        payload: dict[str, Any] = {"name": name}
        if executor_type is not None:
            payload["executor_type"] = executor_type
        if num_qubits is not None:
            payload["num_qubits"] = num_qubits
        if enabled is not None:
            payload["enabled"] = enabled

        resp = self._session.post(f"{self.base_url}/api/op/qpus/create", json=payload)
        resp.raise_for_status()
        return resp.json()

    def connect_qpu(
        self,
        name: str,
        access_token: str,
        executor_type: str | None = None,
        device_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Connect a QPU driver node.

        Args:
            name: QPU name.
            access_token: The access token for the QPU.
            executor_type: Type of executor.
            device_config: Configuration dict for the device.

        Returns:
            The connection response dict with NNG port assignments.
        """
        payload: dict[str, Any] = {
            "name": name,
            "access_token": access_token,
        }
        if executor_type is not None:
            payload["executor_type"] = executor_type
        if device_config is not None:
            payload["device_config"] = device_config

        resp = self._session.post(f"{self.base_url}/api/op/qpus/connect", json=payload)
        resp.raise_for_status()
        return resp.json()

    def toggle_qpu(self, qpu_id: str, enabled: bool) -> dict[str, Any]:
        """Toggle QPU driver state (admin-only).

        Args:
            qpu_id: ID of the QPU.
            enabled: Whether the QPU should be enabled.

        Returns:
            Response dict.
        """
        resp = self._session.post(
            f"{self.base_url}/api/op/qpu/toggle",
            json={"id": qpu_id, "enabled": enabled},
        )
        resp.raise_for_status()
        return resp.json()

    def list_notifications(self) -> list[dict[str, Any]]:
        """List notifications visible to the authenticated user.

        Returns:
            A list of notification record dicts.
        """
        resp = self._session.get(
            f"{self.base_url}/api/collections/notifications/records"
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        return data.get("items", [])

    def dismiss_notification(self, notification_id: str) -> dict[str, Any]:
        """Dismiss a notification for the authenticated user.

        Args:
            notification_id: ID of the notification.

        Returns:
            Response dict.
        """
        resp = self._session.post(
            f"{self.base_url}/api/notifications/{notification_id}/dismiss"
        )
        resp.raise_for_status()
        return resp.json()

    def list_time_slots(self) -> list[dict[str, Any]]:
        """List all booking slots.

        Returns:
            A list of booking slot record dicts.
        """
        resp = self._session.get(f"{self.base_url}/api/collections/time_slots/records")
        resp.raise_for_status()
        data = resp.json()
        return data.get("items", [])

    def create_time_slot(
        self,
        start_time: str,
        end_time: str,
        booked_by: str | None = None,
    ) -> dict[str, Any]:
        """Create a new booking slot.

        Args:
            start_time: Start time RFC3339 string.
            end_time: End time RFC3339 string.
            booked_by: Optional ID of the user booking the slot.

        Returns:
            The created booking slot dict.
        """
        payload = {"start_time": start_time, "end_time": end_time}
        if booked_by is not None:
            payload["booked_by"] = booked_by
        resp = self._session.post(
            f"{self.base_url}/api/collections/time_slots/records", json=payload
        )
        resp.raise_for_status()
        return resp.json()

    def update_time_slot(
        self,
        slot_id: str,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict[str, Any]:
        """Update an existing booking slot.

        Args:
            slot_id: ID of the booking slot.
            start_time: Optional start time RFC3339 string.
            end_time: Optional end time RFC3339 string.

        Returns:
            The updated booking slot dict.
        """
        payload = {}
        if start_time is not None:
            payload["start_time"] = start_time
        if end_time is not None:
            payload["end_time"] = end_time
        resp = self._session.patch(
            f"{self.base_url}/api/collections/time_slots/records/{slot_id}",
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()

    def delete_time_slot(self, slot_id: str) -> None:
        """Delete a booking slot.

        Args:
            slot_id: ID of the booking slot.
        """
        resp = self._session.delete(
            f"{self.base_url}/api/collections/time_slots/records/{slot_id}"
        )
        resp.raise_for_status()

    def list_time_requests(self) -> list[dict[str, Any]]:
        """List QPU time requests.

        Returns:
            A list of QPU time request record dicts.
        """
        resp = self._session.get(
            f"{self.base_url}/api/collections/qpu_time_requests/records"
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("items", [])

    def create_time_request(
        self, seconds: int, requested_reason: str | None = None
    ) -> dict[str, Any]:
        """Create a new QPU time request.

        Args:
            seconds: Requested duration in seconds.
            requested_reason: Optional explanation.

        Returns:
            The created QPU time request record dict.
        """
        payload = {"seconds": seconds}
        if requested_reason is not None:
            payload["requested_reason"] = requested_reason
        resp = self._session.post(
            f"{self.base_url}/api/collections/qpu_time_requests/records", json=payload
        )
        resp.raise_for_status()
        return resp.json()

    def update_time_request(
        self,
        request_id: str,
        status: str,
        rejection_reason: str | None = None,
    ) -> dict[str, Any]:
        """Update/Handle a QPU time request (admin-only).

        Args:
            request_id: ID of the time request.
            status: "approved" or "rejected".
            rejection_reason: Optional explanation if rejected.

        Returns:
            The updated request record dict.
        """
        payload = {"status": status}
        if rejection_reason is not None:
            payload["rejection_reason"] = rejection_reason
        resp = self._session.patch(
            f"{self.base_url}/api/collections/qpu_time_requests/records/{request_id}",
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()

    def list_users(self) -> list[dict[str, Any]]:
        """List all registered users (admin-only).

        Returns:
            A list of user record dicts.
        """
        resp = self._session.get(f"{self.base_url}/api/collections/users/records")
        resp.raise_for_status()
        data = resp.json()
        return data.get("items", [])

    def allocate_qpu_time(self, user_id: str, seconds: int) -> dict[str, Any]:
        """Allocate QPU time to a user (admin-only).

        Args:
            user_id: ID of the user.
            seconds: Total allocated seconds.

        Returns:
            The updated user record dict.
        """
        resp = self._session.patch(
            f"{self.base_url}/api/collections/users/records/{user_id}",
            json={"qpu_seconds": seconds},
        )
        resp.raise_for_status()
        return resp.json()

    def auth_with_password(self, identity: str, password: str) -> dict[str, Any]:
        """Authenticate as a regular user using email/password.

        Args:
            identity: Email or username.
            password: User password.

        Returns:
            The auth response payload including token and record.
        """
        resp = self._session.post(
            f"{self.base_url}/api/collections/users/auth-with-password",
            json={"identity": identity, "password": password},
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("token")
        if token:
            self._session.headers["Authorization"] = f"Bearer {token}"
        return data

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()

    def __enter__(self) -> "QPIClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
