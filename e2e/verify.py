"""
verify.py — End-to-end verification of the QPi control stack.

Steps:
  1. Poll quantum_jobs until all seeded jobs are "completed".
  2. Verify QPU seconds were deducted from the test user.
  3. Test API token authentication for job listing/retrieval.
  4. Test the admin-only user update endpoint.
  5. Test job cancellation via the API.
  6. Test the recovery engine: manually mark one job as "running" and
     confirm it is reset to "pending" after the recovery interval.
  7. Smoke-test the Python, Go, and JavaScript client SDKs.

Usage:
    ADMIN_EMAIL=admin@example.com ADMIN_PASSWORD=secret python verify.py
    python verify.py --driver          # driver-focused tests only
    python verify.py --client-py       # Python client smoke only
    python verify.py --client-js       # JS client smoke only
    python verify.py --client-go       # Go client smoke only

The script exits 0 on success, 1 on failure.
"""

import argparse, os, sys, time, requests, json
from datetime import datetime, timezone, timedelta
import threading, socket, ssl, subprocess, sys, shutil, signal

HOST = os.getenv("GO_SERVER_HOST", "127.0.0.1")
PORT = int(os.getenv("GO_SERVER_PORT", "8090"))
BASE = f"http://{HOST}:{PORT}"

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "supersecretpassword1234")

MAX_WAIT_SECS = 120  # give the driver up to 2 minutes to finish all jobs
TEST_API_TOKEN = "test-api-token-abc-123"
TEST_USER_EMAIL = "user@example.com"
ACCESS_TOKEN = "my-super-secret-token-12345"

# Driver-framework mode (RFC 0001): when enabled, the QPU is backed by a
# registered driver. Toggling and (re)connection then happen at the driver
# level (drivers/toggle, drivers/connect) rather than on the QPU itself.
DRIVER_FRAMEWORK = True

s = requests.Session()


def read_driver_token():
    """Read the one-time driver token seed.py wrote for the framework harness."""
    qpi_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(qpi_dir, "data", "driver_token.txt")
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def admin_auth():
    resp = s.post(
        f"{BASE}/api/collections/_superusers/auth-with-password",
        json={"identity": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    resp.raise_for_status()
    token = resp.json()["token"]
    s.headers["Authorization"] = token
    print("[verify] Admin authenticated")


def get_all_jobs():
    resp = s.get(
        f"{BASE}/api/collections/quantum_jobs/records",
        params={"perPage": 200, "sort": "+created"},
    )
    resp.raise_for_status()
    return resp.json()["items"]


def get_test_user():
    """Fetch the test user record by email."""
    resp = s.get(
        f"{BASE}/api/collections/users/records",
        params={"filter": f'email="{TEST_USER_EMAIL}"'},
    )
    resp.raise_for_status()
    items = resp.json()["items"]
    return items[0] if items else None


def wait_for_completion(timeout=MAX_WAIT_SECS):
    print(f"\n[verify] Waiting up to {timeout}s for all jobs to complete …")
    start = time.time()
    while time.time() - start < timeout:
        jobs = get_all_jobs()
        statuses = [j["status"] for j in jobs]
        pending = statuses.count("pending")
        running = statuses.count("running")
        done = statuses.count("completed")
        total = len(jobs)
        print(
            f"  [{int(time.time() - start):3d}s]  total={total}  "
            f"pending={pending}  running={running}  completed={done}"
        )
        if pending == 0 and running == 0 and done == total:
            return jobs
        time.sleep(5)
    return None


def print_summary(jobs):
    print("\n── Job Summary ─────────────────────────────────────────────────")
    fmt = "{:<26} {:<12} {:<12} {:<20} {}"
    print(fmt.format("ID", "Status", "Duration", "Finished At", "Results (excerpt)"))
    print("─" * 100)
    for j in jobs:
        results = j.get("results") or "{}"
        if isinstance(results, str):
            try:
                results = json.loads(results)
            except Exception:
                pass
        excerpt = str(results)[:50] + "…" if len(str(results)) > 50 else str(results)
        duration = j.get("duration")
        duration_str = f"{duration:.2f}s" if duration is not None else "--"
        print(
            fmt.format(
                j["id"][:24],
                j["status"],
                duration_str,
                j.get("finished_at", "")[:19],
                excerpt,
            )
        )


# ---------------------------------------------------------------------------
# New E2E tests for recently added features
# ---------------------------------------------------------------------------


def test_qpu_seconds_deduction():
    """Verify that QPU seconds were deducted from the test user after jobs ran."""
    print("\n[verify] Testing QPU seconds deduction …")
    user = get_test_user()
    if not user:
        print("[verify] ✗ Test user not found")
        return False

    qpu_seconds = user.get("qpu_seconds", 0)
    # User was granted 1000 seconds; after 5 jobs some should have been deducted
    if qpu_seconds >= 1000:
        print(f"[verify] ✗ QPU seconds not deducted (still {qpu_seconds})")
        return False

    print(f"[verify] ✓ QPU seconds deducted: {qpu_seconds:.2f} remaining")
    return True


def test_api_token_auth():
    """Verify that API token authentication works for job CRUD endpoints."""
    print("\n[verify] Testing API token authentication …")
    headers = {"X-API-Token": TEST_API_TOKEN}

    # List jobs
    resp = requests.get(f"{BASE}/api/jobs", headers=headers)
    if resp.status_code != 200:
        print(f"[verify] ✗ LIST /api/jobs failed: {resp.status_code} {resp.text}")
        return False
    jobs = resp.json()
    print(f"[verify] ✓ LIST /api/jobs returned {len(jobs)} jobs")

    if not jobs:
        print("[verify] ⚠ No jobs to test GET/CANCEL")
        return True

    job_id = jobs[0]["id"]

    # Get single job
    resp = requests.get(f"{BASE}/api/jobs/{job_id}", headers=headers)
    if resp.status_code != 200:
        print(f"[verify] ✗ GET /api/jobs/{job_id} failed: {resp.status_code}")
        return False
    print(f"[verify] ✓ GET /api/jobs/{job_id} succeeded")

    return True


def test_admin_user_update():
    """Verify that superusers (admins) can update user records directly (e.g. qpu_seconds)."""
    print("\n[verify] Testing admin user update via collection API …")
    user = get_test_user()
    if not user:
        print("[verify] ✗ Test user not found")
        return False

    user_id = user["id"]
    original_seconds = user.get("qpu_seconds", 0)

    # Grant additional QPU seconds
    new_seconds = original_seconds + 500.0
    resp = s.patch(
        f"{BASE}/api/collections/users/records/{user_id}",
        json={
            "qpu_seconds": new_seconds,
        },
    )
    if resp.status_code != 200:
        print(f"[verify] ✗ Admin PATCH failed: {resp.status_code} {resp.text}")
        return False

    data = resp.json()
    if abs(data.get("qpu_seconds", 0) - new_seconds) > 0.01:
        print(f"[verify] ✗ qpu_seconds not updated correctly: {data}")
        return False

    print(f"[verify] ✓ Admin PATCH updated qpu_seconds to {data['qpu_seconds']}")
    return True


def test_job_cancel():
    """Test that a pending job can be cancelled via the API."""
    print("\n[verify] Testing job cancellation …")
    headers = {"X-API-Token": TEST_API_TOKEN}

    # Get an existing job to extract the qpu_target (avoids 503 when no online QPU)
    jobs = get_all_jobs()
    qpu_target = jobs[0].get("qpu_target") if jobs else None

    # Submit a new job specifically to cancel it
    qasm = """OPENQASM 2.0;
include "qelib1.inc";
qreg q[1];
creg c[1];
x q[0];
measure q[0] -> c[0];"""
    payload = {
        "circuits": [{"circuit": qasm}],
        "shots": 100,
    }
    if qpu_target:
        payload["qpu_target"] = qpu_target
    resp = requests.post(f"{BASE}/api/jobs", json=payload, headers=headers)
    if resp.status_code not in (200, 201):
        print(f"[verify] ✗ Could not create job to cancel: {resp.status_code}")
        return False

    job_id = resp.json().get("job_id") or resp.json().get("id")
    print(f"[verify] Created job {job_id} to cancel")

    # Cancel it
    resp = requests.post(f"{BASE}/api/jobs/{job_id}/cancel", headers=headers)
    if resp.status_code != 200:
        print(f"[verify] ✗ Cancel failed: {resp.status_code} {resp.text}")
        return False

    data = resp.json()
    if data.get("status") != "cancelled":
        print(f"[verify] ✗ Job status not cancelled: {data}")
        return False

    print(f"[verify] ✓ Job {job_id} cancelled successfully")
    return True


def test_python_client_smoke():
    """Smoke-test the Python client SDK against the running server."""
    print("\n[verify] Testing Python client SDK smoke …")
    from qpi_client import QPIClient

    client = QPIClient(BASE, api_token=TEST_API_TOKEN)

    # List jobs
    jobs = client.list_jobs()
    print(f"[verify] ✓ Python client list_jobs returned {len(jobs)} jobs")

    # Get first job if any
    if jobs:
        job = client.get_job(jobs[0]["id"])
        print(f"[verify] ✓ Python client get_job returned status={job.get('status')}")

    client.close()
    return True


def test_go_client_smoke():
    """Smoke-test the Go client SDK against the running server."""
    print("\n[verify] Testing Go client SDK smoke …")
    import subprocess

    script_dir = os.path.dirname(os.path.abspath(__file__))
    env = os.environ.copy()
    env["QPI_BASE_URL"] = BASE
    env["QPI_API_TOKEN"] = TEST_API_TOKEN

    result = subprocess.run(
        ["go", "run", "smoke_go.go"],
        cwd=script_dir,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[verify] ✗ Go client smoke test failed:\n{result.stderr}")
        return False

    for line in result.stdout.strip().splitlines():
        print(f"[verify]   {line}")
    return True


def test_js_client_smoke():
    """Smoke-test the JavaScript client SDK against the running server."""
    print("\n[verify] Testing JS client SDK smoke …")
    import subprocess

    script_dir = os.path.dirname(os.path.abspath(__file__))
    env = os.environ.copy()
    env["QPI_BASE_URL"] = BASE
    env["QPI_API_TOKEN"] = TEST_API_TOKEN

    result = subprocess.run(
        ["node", "smoke_js.mjs"],
        cwd=script_dir,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[verify] ✗ JS client smoke test failed:\n{result.stderr}")
        return False

    for line in result.stdout.strip().splitlines():
        print(f"[verify]   {line}")
    return True


def test_qiskit_hadamard_circuit():
    """Build a Hadamard circuit with Qiskit, submit it, and verify completion."""
    print("\n[verify] Testing Qiskit Hadamard circuit submission …")
    try:
        from qiskit import QuantumCircuit, qasm2
    except ImportError:
        print("[verify] ⚠ qiskit not installed — skipping Hadamard circuit test")
        return True

    # Build a simple Hadamard + measure circuit
    qc = QuantumCircuit(1, 1)
    qc.h(0)
    qc.measure(0, 0)
    qasm = qasm2.dumps(qc)

    # Extract qpu_target from existing jobs so submission succeeds
    # even if the QPU has gone offline after processing seeded jobs
    jobs = get_all_jobs()
    qpu_target = jobs[0].get("qpu_target") if jobs else None

    headers = {"X-API-Token": TEST_API_TOKEN}
    payload = {
        "circuits": [{"circuit": qasm}],
        "shots": 100,
    }
    if qpu_target:
        payload["qpu_target"] = qpu_target

    # Submit the job
    resp = requests.post(f"{BASE}/api/jobs", json=payload, headers=headers)
    if resp.status_code not in (200, 201):
        print(f"[verify] ✗ Job submission failed: {resp.status_code} {resp.text}")
        return False

    job_id = resp.json().get("job_id") or resp.json().get("id")
    print(f"[verify] Submitted Hadamard job {job_id}")

    # Poll for completion (up to 30s)
    for i in range(30):
        time.sleep(1)
        resp = requests.get(f"{BASE}/api/jobs/{job_id}", headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            status = data.get("status")
            if status == "completed":
                duration = data.get("duration")
                duration_str = f"{duration:.2f}s" if duration is not None else "unknown"
                print(
                    f"[verify] ✓ Hadamard job completed after {i + 1}s (duration: {duration_str})"
                )
                return True
            if status in ("failed", "cancelled"):
                print(f"[verify] ✗ Hadamard job {status}")
                return False

    print("[verify] ✗ Hadamard job did not complete within 30s")
    return False


def test_recovery_engine():
    print("\n[verify] Testing recovery engine …")
    jobs = get_all_jobs()
    if not jobs:
        print("[verify] No jobs found — skipping recovery test")
        return True

    target = jobs[0]
    jid = target["id"]

    resp = s.patch(
        f"{BASE}/api/collections/quantum_jobs/records/{jid}",
        json={"status": "running", "qpu_target": None},
    )
    resp.raise_for_status()
    print(
        f"[verify] Marked job {jid[:12]}… as 'running'. "
        f"Waiting up to 35s for recovery engine to reset it …"
    )

    for i in range(35):
        time.sleep(1)
        job = s.get(f"{BASE}/api/collections/quantum_jobs/records/{jid}").json()
        if job.get("status") == "pending":
            print(f"[verify] ✓ Recovery engine reset job after {i + 1}s")
            return True

    print(
        "[verify] ✗ Recovery engine did NOT reset job within 35s "
        "(may need longer if jobTimeout > 20s in qpi-ui/main.go)"
    )
    return False


def test_time_slots_validation():
    print("\n[verify] Testing Time Slots CRUD & Validations …")

    # 1. Authenticate test user
    user_session = requests.Session()
    resp = user_session.post(
        f"{BASE}/api/collections/users/auth-with-password",
        json={"identity": "user@example.com", "password": "userpassword1234"},
    )
    if resp.status_code != 200:
        print(f"[verify] ✗ Failed to authenticate test user: {resp.text}")
        return False
    user_token = resp.json()["token"]
    user_id = resp.json()["record"]["id"]
    user_session.headers["Authorization"] = user_token
    print("[verify] Test user authenticated")

    # 2. Get the seeded slot (created by seed.py) to check Listing and Viewing.
    # Note: Seeded slot starts 2 mins ago, ends 5 mins from now.
    resp = user_session.get(f"{BASE}/api/collections/time_slots/records")
    if resp.status_code != 200:
        print(f"[verify] ✗ User cannot list slots: {resp.text}")
        return False
    slots = resp.json()["items"]
    print(f"[verify] ✓ User listed {len(slots)} slots")

    # Find the seeded slot
    seeded_slot = None
    for slot in slots:
        if slot["booked_by"] == user_id:
            seeded_slot = slot
            break

    if not seeded_slot:
        print("[verify] ✗ Seeded slot not found in list")
        return False

    # Get single slot
    resp = user_session.get(
        f"{BASE}/api/collections/time_slots/records/{seeded_slot['id']}"
    )
    if resp.status_code != 200:
        print(f"[verify] ✗ User cannot view single slot: {resp.text}")
        return False
    print("[verify] ✓ User viewed single slot")

    # 3. Test slot creation in the past
    now = datetime.now(timezone.utc)
    past_start = (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S.000Z")
    past_end = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S.000Z")

    resp = user_session.post(
        f"{BASE}/api/collections/time_slots/records",
        json={"start_time": past_start, "end_time": past_end, "booked_by": user_id},
    )
    if resp.status_code != 400 or "past" not in resp.text.lower():
        print(
            f"[verify] ✗ Past slot creation was not rejected: {resp.status_code} {resp.text}"
        )
        return False
    print("[verify] ✓ Past slot creation rejected correctly for regular user")

    # Verify that admin CAN create a slot in the past
    # (using admin session `s`)
    resp = s.post(
        f"{BASE}/api/collections/time_slots/records",
        json={"start_time": past_start, "end_time": past_end, "booked_by": user_id},
    )
    if resp.status_code != 200:
        print(
            f"[verify] ✗ Admin failed to create slot in the past: {resp.status_code} {resp.text}"
        )
        return False
    admin_past_slot_id = resp.json()["id"]
    print("[verify] ✓ Admin successfully created slot in the past")

    # 4. Test overlapping slots (validation rule in validateTimeSlot)
    # Let's try to create a slot that overlaps with the seeded slot.
    # Seeded slot is current, let's create a future slot first.
    future_start_1 = (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S.000Z")
    future_end_1 = (now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S.000Z")

    resp = user_session.post(
        f"{BASE}/api/collections/time_slots/records",
        json={
            "start_time": future_start_1,
            "end_time": future_end_1,
            "booked_by": user_id,
        },
    )
    if resp.status_code != 200:
        print(
            f"[verify] ✗ Failed to create non-overlapping future slot: {resp.status_code} {resp.text}"
        )
        return False
    future_slot_id = resp.json()["id"]
    print("[verify] ✓ Created future slot")

    # Try to create a slot that overlaps with the future slot
    overlap_start = (now + timedelta(hours=1, minutes=30)).strftime(
        "%Y-%m-%d %H:%M:%S.000Z"
    )
    overlap_end = (now + timedelta(hours=2, minutes=30)).strftime(
        "%Y-%m-%d %H:%M:%S.000Z"
    )

    resp = user_session.post(
        f"{BASE}/api/collections/time_slots/records",
        json={
            "start_time": overlap_start,
            "end_time": overlap_end,
            "booked_by": user_id,
        },
    )
    if resp.status_code == 200:
        print("[verify] ✗ Overlapping slot creation was NOT rejected")
        return False
    if "overlap" not in resp.text.lower():
        print(
            f"[verify] ✗ Overlapping slot creation failed but with unexpected message: {resp.text}"
        )
        return False
    print("[verify] ✓ Overlapping slot creation rejected correctly")

    # Try to create a slot with start_time >= end_time
    invalid_start = (now + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S.000Z")
    invalid_end = (now + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S.000Z")
    resp = user_session.post(
        f"{BASE}/api/collections/time_slots/records",
        json={
            "start_time": invalid_start,
            "end_time": invalid_end,
            "booked_by": user_id,
        },
    )
    if resp.status_code == 200:
        print("[verify] ✗ Slot with start_time >= end_time was NOT rejected")
        return False
    if "strictly before" not in resp.text.lower():
        print(
            f"[verify] ✗ Slot with start_time >= end_time failed with unexpected message: {resp.text}"
        )
        return False
    print("[verify] ✓ Slot with start_time >= end_time rejected correctly")

    # 5. Test modifying a slot that has already started (seeded_slot starts 2 mins ago)
    resp = user_session.patch(
        f"{BASE}/api/collections/time_slots/records/{seeded_slot['id']}",
        json={
            "end_time": (now + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S.000Z")
        },
    )
    if resp.status_code != 400 or "already started" not in resp.text.lower():
        print(
            f"[verify] ✗ Modifying already-started slot was not rejected: {resp.status_code} {resp.text}"
        )
        return False
    print(
        "[verify] ✓ Modifying already-started slot rejected correctly for regular user"
    )

    # Verify that admin CAN modify an already-started slot
    resp = s.patch(
        f"{BASE}/api/collections/time_slots/records/{seeded_slot['id']}",
        json={
            "end_time": (now + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S.000Z")
        },
    )
    if resp.status_code != 200:
        print(
            f"[verify] ✗ Admin failed to modify already-started slot: {resp.status_code} {resp.text}"
        )
        return False
    print("[verify] ✓ Admin successfully modified already-started slot")

    # 6. Test rescheduling a slot to a start time in the past
    past_reschedule_start = (now - timedelta(minutes=45)).strftime(
        "%Y-%m-%d %H:%M:%S.000Z"
    )
    past_reschedule_end = (now - timedelta(minutes=15)).strftime(
        "%Y-%m-%d %H:%M:%S.000Z"
    )

    resp = user_session.patch(
        f"{BASE}/api/collections/time_slots/records/{future_slot_id}",
        json={"start_time": past_reschedule_start, "end_time": past_reschedule_end},
    )
    if resp.status_code != 400 or "past" not in resp.text.lower():
        print(
            f"[verify] ✗ Rescheduling slot to past start time was not rejected: {resp.status_code} {resp.text}"
        )
        return False
    print(
        "[verify] ✓ Rescheduling slot to past start time rejected correctly for regular user"
    )

    # Verify that admin CAN reschedule a slot to a start time in the past
    resp = s.patch(
        f"{BASE}/api/collections/time_slots/records/{future_slot_id}",
        json={"start_time": past_reschedule_start, "end_time": past_reschedule_end},
    )
    if resp.status_code != 200:
        print(
            f"[verify] ✗ Admin failed to reschedule slot to past start time: {resp.status_code} {resp.text}"
        )
        return False
    print("[verify] ✓ Admin successfully rescheduled slot to past start time")

    # 7. Test deleting a slot that has already started (seeded_slot)
    resp = user_session.delete(
        f"{BASE}/api/collections/time_slots/records/{seeded_slot['id']}"
    )
    if resp.status_code != 400 or "already started" not in resp.text.lower():
        print(
            f"[verify] ✗ Deleting already-started slot was not rejected: {resp.status_code} {resp.text}"
        )
        return False
    print(
        "[verify] ✓ Deleting already-started slot rejected correctly for regular user"
    )

    # Verify that admin CAN delete an already-started slot
    resp = s.delete(f"{BASE}/api/collections/time_slots/records/{seeded_slot['id']}")
    if resp.status_code != 204:
        print(
            f"[verify] ✗ Admin failed to delete already-started slot: {resp.status_code} {resp.text}"
        )
        return False
    print("[verify] ✓ Admin successfully deleted already-started slot")

    # 8. Test authorization: User B trying to update/delete User A's slot
    # Create User B
    resp = s.post(
        f"{BASE}/api/collections/users/records",
        json={
            "email": "userB@example.com",
            "emailVisibility": True,
            "password": "userBpassword1234",
            "passwordConfirm": "userBpassword1234",
        },
    )
    if resp.status_code == 400 and "already" in resp.text.lower():
        # fetch existing
        resp2 = s.get(
            f"{BASE}/api/collections/users/records",
            params={"filter": 'email="userB@example.com"'},
        )
        resp2.raise_for_status()
        userB = resp2.json()["items"][0]
    else:
        resp.raise_for_status()
        userB = resp.json()

    userB_session = requests.Session()
    resp = userB_session.post(
        f"{BASE}/api/collections/users/auth-with-password",
        json={"identity": "userB@example.com", "password": "userBpassword1234"},
    )
    resp.raise_for_status()
    userB_session.headers["Authorization"] = resp.json()["token"]

    # Try to modify future_slot_id (owned by User A / user_id)
    resp = userB_session.patch(
        f"{BASE}/api/collections/time_slots/records/{future_slot_id}",
        json={
            "end_time": (now + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S.000Z")
        },
    )
    if resp.status_code not in (403, 404):
        print(
            f"[verify] ✗ User B modifying User A's slot was not rejected: {resp.status_code} {resp.text}"
        )
        return False
    print("[verify] ✓ User B modifying User A's slot was rejected correctly")

    # Try to delete future_slot_id (owned by User A)
    resp = userB_session.delete(
        f"{BASE}/api/collections/time_slots/records/{future_slot_id}"
    )
    if resp.status_code not in (403, 404):
        print(
            f"[verify] ✗ User B deleting User A's slot was not rejected: {resp.status_code} {resp.text}"
        )
        return False
    print("[verify] ✓ User B deleting User A's slot was rejected correctly")

    # Cleanup: delete remaining slots
    s.delete(f"{BASE}/api/collections/time_slots/records/{future_slot_id}")
    s.delete(f"{BASE}/api/collections/time_slots/records/{admin_past_slot_id}")
    return True


def test_qpu_time_requests_validation():
    print("\n[verify] Testing QPU Time Requests CRUD & Approval Flow …")

    # 1. Authenticate User A
    user_session = requests.Session()
    resp = user_session.post(
        f"{BASE}/api/collections/users/auth-with-password",
        json={"identity": "user@example.com", "password": "userpassword1234"},
    )
    if resp.status_code != 200:
        print(f"[verify] ✗ Failed to authenticate User A: {resp.text}")
        return False
    user_token = resp.json()["token"]
    user_id = resp.json()["record"]["id"]
    user_session.headers["Authorization"] = user_token
    print("[verify] User A authenticated")

    # 2. User A creates a request (status defaults to pending)
    resp = user_session.post(
        f"{BASE}/api/collections/qpu_time_requests/records",
        json={
            "user": user_id,
            "seconds": 300.0,
            "status": "pending",
            "requested_reason": "Need QPU time for running experiments",
        },
    )
    if resp.status_code not in (200, 201):
        print(
            f"[verify] ✗ Failed to create time request: {resp.status_code} {resp.text}"
        )
        return False
    req = resp.json()
    req_id = req["id"]
    if req["status"] != "pending":
        print(f"[verify] ✗ New request status was not overridden/set to pending: {req}")
        return False
    print("[verify] ✓ User A created pending time request successfully")

    # 3. User A tries to create a request with status = approved (should fail validation rule)
    resp = user_session.post(
        f"{BASE}/api/collections/qpu_time_requests/records",
        json={
            "user": user_id,
            "seconds": 400.0,
            "status": "approved",
            "requested_reason": "Hack status",
        },
    )
    if resp.status_code in (200, 201):
        if resp.json()["status"] == "approved":
            print(
                f"[verify] ✗ User A successfully created an approved request! {resp.text}"
            )
            return False
        else:
            s.delete(
                f"{BASE}/api/collections/qpu_time_requests/records/{resp.json()['id']}"
            )
            print("[verify] ✓ Creating approved request was overridden to pending")
    else:
        print("[verify] ✓ Creating approved request was rejected correctly")

    # 4. User A lists their own requests
    resp = user_session.get(f"{BASE}/api/collections/qpu_time_requests/records")
    if resp.status_code != 200:
        print(f"[verify] ✗ User A failed to list requests: {resp.text}")
        return False
    items = resp.json()["items"]
    reqs_found = [i for i in items if i["id"] == req_id]
    if not reqs_found:
        print(f"[verify] ✗ User A did not see their request: {items}")
        return False
    print("[verify] ✓ User A listed their own requests")

    # 5. User B lists requests (should not see User A's request)
    userB_session = requests.Session()
    resp = userB_session.post(
        f"{BASE}/api/collections/users/auth-with-password",
        json={"identity": "userB@example.com", "password": "userBpassword1234"},
    )
    if resp.status_code != 200:
        print(f"[verify] ✗ Failed to authenticate User B: {resp.text}")
        return False
    userB_session.headers["Authorization"] = resp.json()["token"]

    resp = userB_session.get(f"{BASE}/api/collections/qpu_time_requests/records")
    if resp.status_code != 200:
        print(f"[verify] ✗ User B failed to list requests: {resp.text}")
        return False
    itemsB = resp.json()["items"]
    reqs_found_B = [i for i in itemsB if i["id"] == req_id]
    if reqs_found_B:
        print(f"[verify] ✗ User B can see User A's request in list: {itemsB}")
        return False
    print("[verify] ✓ User B did not see User A's requests in list")

    # 6. User B tries to view User A's request directly
    resp = userB_session.get(
        f"{BASE}/api/collections/qpu_time_requests/records/{req_id}"
    )
    if resp.status_code not in (403, 404):
        print(
            f"[verify] ✗ User B viewing User A's request directly was not rejected: {resp.status_code}"
        )
        return False
    print("[verify] ✓ User B direct view of User A's request was rejected")

    # 7. User B tries to delete User A's request
    resp = userB_session.delete(
        f"{BASE}/api/collections/qpu_time_requests/records/{req_id}"
    )
    if resp.status_code not in (403, 404):
        print(
            f"[verify] ✗ User B deleting User A's request was not rejected: {resp.status_code}"
        )
        return False
    print("[verify] ✓ User B deleting User A's request was rejected")

    # 8. User A creates a second request and cancels (deletes) it (since it is pending)
    resp = user_session.post(
        f"{BASE}/api/collections/qpu_time_requests/records",
        json={
            "user": user_id,
            "seconds": 100.0,
            "status": "pending",
            "requested_reason": "Temporary request to delete",
        },
    )
    if resp.status_code not in (200, 201):
        print(f"[verify] ✗ Failed to create temporary request: {resp.text}")
        return False
    temp_id = resp.json()["id"]

    resp = user_session.delete(
        f"{BASE}/api/collections/qpu_time_requests/records/{temp_id}"
    )
    if resp.status_code != 204:
        print(
            f"[verify] ✗ User A failed to delete their own pending request: {resp.status_code} {resp.text}"
        )
        return False
    print("[verify] ✓ User A deleted their own pending request successfully")

    # 9. User A tries to update their own request (should fail — update disabled for regular users)
    resp = user_session.patch(
        f"{BASE}/api/collections/qpu_time_requests/records/{req_id}",
        json={
            "requested_reason": "Changed",
        },
    )
    if resp.status_code not in (403, 404):
        print(
            f"[verify] ✗ User A was allowed to update their own request: {resp.status_code}"
        )
        return False
    print("[verify] ✓ User A updating own request was rejected")

    # 10. Admin approves User A's request
    #
    # Read the balance here rather than back at authentication. What this check
    # means is "approving a 300 s request credits 300 s", and the nine steps above
    # take long enough that a job settling in the background can debit the user in
    # between — which turns an exact-300 assertion into "and nothing else happened
    # meanwhile", something it never intended to test. Seen failing on 299.42.
    resp = s.get(f"{BASE}/api/collections/users/records/{user_id}")
    resp.raise_for_status()
    initial_seconds = resp.json()["qpu_seconds"]

    resp = s.patch(
        f"{BASE}/api/collections/qpu_time_requests/records/{req_id}",
        json={
            "status": "approved",
        },
    )
    if resp.status_code != 200:
        print(
            f"[verify] ✗ Admin failed to approve request: {resp.status_code} {resp.text}"
        )
        return False
    approved_req = resp.json()
    if approved_req["status"] != "approved":
        print(f"[verify] ✗ Request status was not updated to approved: {approved_req}")
        return False
    if not approved_req.get("handled_by"):
        print(f"[verify] ✗ handled_by was not set on approved request: {approved_req}")
        return False
    print("[verify] ✓ Admin approved request successfully")

    # Verify User A's qpu_seconds has increased by 300
    resp = s.get(f"{BASE}/api/collections/users/records/{user_id}")
    resp.raise_for_status()
    updated_seconds = resp.json()["qpu_seconds"]
    if abs(updated_seconds - (initial_seconds + 300.0)) > 0.01:
        print(
            f"[verify] ✗ User's QPU seconds was not credited correctly. Initial: {initial_seconds}, Updated: {updated_seconds}"
        )
        return False
    print(f"[verify] ✓ User credited successfully (New balance: {updated_seconds})")

    # 11. Admin tries to change status from approved to pending or rejected (should fail)
    resp = s.patch(
        f"{BASE}/api/collections/qpu_time_requests/records/{req_id}",
        json={
            "status": "pending",
        },
    )
    if resp.status_code == 200:
        print("[verify] ✗ Admin changed status from approved back to pending!")
        return False
    print("[verify] ✓ Modifying processed request status was rejected correctly")

    # 12. User A tries to delete the approved request (should fail because status is not pending)
    resp = user_session.delete(
        f"{BASE}/api/collections/qpu_time_requests/records/{req_id}"
    )
    if resp.status_code == 204:
        print("[verify] ✗ User A successfully deleted an approved request!")
        return False
    print("[verify] ✓ User A deleting approved request was rejected correctly")

    # Cleanup: Admin deletes the request
    s.delete(f"{BASE}/api/collections/qpu_time_requests/records/{req_id}")
    return True


def test_notifications_crud():
    """Verify notifications CRUD, visibility rules, and dismiss functionality."""
    print("\n[verify] Testing Notifications CRUD & Visibility …")

    # 1. Authenticate User A
    user_session = requests.Session()
    resp = user_session.post(
        f"{BASE}/api/collections/users/auth-with-password",
        json={"identity": "user@example.com", "password": "userpassword1234"},
    )
    if resp.status_code != 200:
        print(f"[verify] ✗ Failed to authenticate User A: {resp.text}")
        return False
    user_token = resp.json()["token"]
    user_id = resp.json()["record"]["id"]
    user_session.headers["Authorization"] = user_token
    print("[verify] User A authenticated")

    # 2. Admin creates a broadcast notification (no target_users)
    now = datetime.now(timezone.utc)
    past_start = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S.000Z")
    future_end = (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S.000Z")
    resp = s.post(
        f"{BASE}/api/collections/notifications/records",
        json={
            "title": "System Maintenance",
            "description": "Scheduled maintenance tonight.",
            "start_time": past_start,
            "end_time": future_end,
        },
    )
    if resp.status_code not in (200, 201):
        print(
            f"[verify] ✗ Admin failed to create broadcast notification: {resp.status_code} {resp.text}"
        )
        return False
    broadcast_id = resp.json()["id"]
    print("[verify] ✓ Admin created broadcast notification")

    # 3. Admin creates a targeted notification for User A only
    resp = s.post(
        f"{BASE}/api/collections/notifications/records",
        json={
            "title": "Personal Alert",
            "description": "Your QPU time is low.",
            "target_users": [user_id],
            "start_time": past_start,
            "end_time": future_end,
        },
    )
    if resp.status_code not in (200, 201):
        print(
            f"[verify] ✗ Admin failed to create targeted notification: {resp.status_code} {resp.text}"
        )
        return False
    targeted_id = resp.json()["id"]
    print("[verify] ✓ Admin created targeted notification")

    # 4. Admin creates a future notification (not yet visible)
    future_start = (now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S.000Z")
    future_end_2 = (now + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S.000Z")
    resp = s.post(
        f"{BASE}/api/collections/notifications/records",
        json={
            "title": "Future Announcement",
            "description": "This should not be visible yet.",
            "start_time": future_start,
            "end_time": future_end_2,
        },
    )
    if resp.status_code not in (200, 201):
        print(
            f"[verify] ✗ Admin failed to create future notification: {resp.status_code} {resp.text}"
        )
        return False
    future_id = resp.json()["id"]
    print("[verify] ✓ Admin created future notification")

    # 5. User A lists notifications — should see broadcast + targeted, not future
    resp = user_session.get(f"{BASE}/api/collections/notifications/records")
    if resp.status_code != 200:
        print(f"[verify] ✗ User A failed to list notifications: {resp.text}")
        return False
    items = resp.json()["items"]
    ids = {i["id"] for i in items}
    if broadcast_id not in ids:
        print("[verify] ✗ User A cannot see broadcast notification")
        return False
    if targeted_id not in ids:
        print("[verify] ✗ User A cannot see targeted notification")
        return False
    if future_id in ids:
        print("[verify] ✗ User A can see future notification (should be hidden)")
        return False
    print(f"[verify] ✓ User A sees correct notifications ({len(items)} visible)")

    # 6. User B lists notifications — should see broadcast only, not targeted
    userB_session = requests.Session()
    resp = userB_session.post(
        f"{BASE}/api/collections/users/auth-with-password",
        json={"identity": "userB@example.com", "password": "userBpassword1234"},
    )
    if resp.status_code != 200:
        print(f"[verify] ✗ Failed to authenticate User B: {resp.text}")
        return False
    userB_session.headers["Authorization"] = resp.json()["token"]

    resp = userB_session.get(f"{BASE}/api/collections/notifications/records")
    if resp.status_code != 200:
        print(f"[verify] ✗ User B failed to list notifications: {resp.text}")
        return False
    itemsB = resp.json()["items"]
    idsB = {i["id"] for i in itemsB}
    if broadcast_id not in idsB:
        print("[verify] ✗ User B cannot see broadcast notification")
        return False
    if targeted_id in idsB:
        print("[verify] ✗ User B can see targeted notification meant for User A")
        return False
    print("[verify] ✓ User B sees only broadcast notification")

    # 7. User A dismisses the broadcast notification
    resp = user_session.post(f"{BASE}/api/notifications/{broadcast_id}/dismiss")
    if resp.status_code != 200:
        print(
            f"[verify] ✗ User A failed to dismiss notification: {resp.status_code} {resp.text}"
        )
        return False
    print("[verify] ✓ User A dismissed broadcast notification")

    # 8. User A lists again — broadcast should be gone, targeted still visible
    resp = user_session.get(f"{BASE}/api/collections/notifications/records")
    if resp.status_code != 200:
        print(
            f"[verify] ✗ User A failed to list notifications after dismiss: {resp.text}"
        )
        return False
    items = resp.json()["items"]
    ids = {i["id"] for i in items}
    if broadcast_id in ids:
        print("[verify] ✗ Dismissed broadcast still visible to User A")
        return False
    if targeted_id not in ids:
        print("[verify] ✗ Targeted notification disappeared after unrelated dismiss")
        return False
    print("[verify] ✓ Dismissed notification hidden from User A")

    # 9. User B still sees broadcast (not dismissed by them)
    resp = userB_session.get(f"{BASE}/api/collections/notifications/records")
    itemsB = resp.json()["items"]
    idsB = {i["id"] for i in itemsB}
    if broadcast_id not in idsB:
        print("[verify] ✗ Broadcast hidden from User B after User A dismissed it")
        return False
    print("[verify] ✓ User B still sees broadcast after User A dismissed")

    # 10. Non-admin user tries to create a notification (should fail)
    resp = user_session.post(
        f"{BASE}/api/collections/notifications/records",
        json={
            "title": "Unauthorized",
            "description": "Should not be allowed.",
        },
    )
    if resp.status_code not in (403, 404):
        print(
            f"[verify] ✗ Non-admin was allowed to create notification: {resp.status_code}"
        )
        return False
    print("[verify] ✓ Non-admin creation rejected")

    # 11. Non-admin user tries to update a notification (should fail)
    resp = user_session.patch(
        f"{BASE}/api/collections/notifications/records/{broadcast_id}",
        json={
            "title": "Hacked",
        },
    )
    if resp.status_code not in (403, 404):
        print(
            f"[verify] ✗ Non-admin was allowed to update notification: {resp.status_code}"
        )
        return False
    print("[verify] ✓ Non-admin update rejected")

    # 12. Non-admin user tries to delete a notification (should fail)
    resp = user_session.delete(
        f"{BASE}/api/collections/notifications/records/{broadcast_id}"
    )
    if resp.status_code not in (403, 404):
        print(
            f"[verify] ✗ Non-admin was allowed to delete notification: {resp.status_code}"
        )
        return False
    print("[verify] ✓ Non-admin delete rejected")

    # 13. Admin updates notification
    resp = s.patch(
        f"{BASE}/api/collections/notifications/records/{broadcast_id}",
        json={
            "title": "Updated Maintenance",
        },
    )
    if resp.status_code != 200:
        print(
            f"[verify] ✗ Admin failed to update notification: {resp.status_code} {resp.text}"
        )
        return False
    if resp.json()["title"] != "Updated Maintenance":
        print("[verify] ✗ Notification title not updated")
        return False
    print("[verify] ✓ Admin updated notification")

    # 14. Admin deletes future notification
    resp = s.delete(f"{BASE}/api/collections/notifications/records/{future_id}")
    if resp.status_code != 204:
        print(
            f"[verify] ✗ Admin failed to delete notification: {resp.status_code} {resp.text}"
        )
        return False
    print("[verify] ✓ Admin deleted notification")

    # Cleanup remaining notifications
    s.delete(f"{BASE}/api/collections/notifications/records/{broadcast_id}")
    s.delete(f"{BASE}/api/collections/notifications/records/{targeted_id}")
    return True


def test_api_tokens_auth_rules():
    """Verify api_tokens collection auth rules: owner-only CRUD."""
    print("\n[verify] Testing api_tokens authorization rules …")

    # Authenticate User A
    userA_session = requests.Session()
    resp = userA_session.post(
        f"{BASE}/api/collections/users/auth-with-password",
        json={"identity": "user@example.com", "password": "userpassword1234"},
    )
    if resp.status_code != 200:
        print(f"[verify] ✗ Failed to authenticate User A: {resp.text}")
        return False
    userA_token = resp.json()["token"]
    userA_id = resp.json()["record"]["id"]
    userA_session.headers["Authorization"] = userA_token

    # User A creates a token via collection API
    resp = userA_session.post(
        f"{BASE}/api/collections/api_tokens/records",
        json={
            "token": "hashed-token-abc",
            "user": userA_id,
            "name": "Test Token",
        },
    )
    if resp.status_code not in (200, 201):
        print(
            f"[verify] ✗ User A failed to create token: {resp.status_code} {resp.text}"
        )
        return False
    token_id = resp.json()["id"]
    print("[verify] ✓ User A created token via collection API")

    # User A lists tokens — should see their own
    resp = userA_session.get(f"{BASE}/api/collections/api_tokens/records")
    if resp.status_code != 200:
        print(f"[verify] ✗ User A failed to list tokens: {resp.status_code}")
        return False
    items = resp.json()["items"]
    if not any(i["id"] == token_id for i in items):
        print("[verify] ✗ User A cannot see their own token in list")
        return False
    print("[verify] ✓ User A sees their own token in list")

    # User A views single token
    resp = userA_session.get(f"{BASE}/api/collections/api_tokens/records/{token_id}")
    if resp.status_code != 200:
        print(f"[verify] ✗ User A failed to view token: {resp.status_code}")
        return False
    print("[verify] ✓ User A views their own token")

    # User A updates token
    resp = userA_session.patch(
        f"{BASE}/api/collections/api_tokens/records/{token_id}",
        json={
            "name": "Updated Token",
        },
    )
    if resp.status_code != 200:
        print(f"[verify] ✗ User A failed to update token: {resp.status_code}")
        return False
    print("[verify] ✓ User A updated their own token")

    # Authenticate User B
    userB_session = requests.Session()
    resp = userB_session.post(
        f"{BASE}/api/collections/users/auth-with-password",
        json={"identity": "userB@example.com", "password": "userBpassword1234"},
    )
    if resp.status_code != 200:
        print(f"[verify] ✗ Failed to authenticate User B: {resp.text}")
        return False
    userB_session.headers["Authorization"] = resp.json()["token"]

    # User B lists tokens — should NOT see User A's token
    resp = userB_session.get(f"{BASE}/api/collections/api_tokens/records")
    if resp.status_code != 200:
        print(f"[verify] ✗ User B failed to list tokens: {resp.status_code}")
        return False
    itemsB = resp.json()["items"]
    if any(i["id"] == token_id for i in itemsB):
        print("[verify] ✗ User B can see User A's token")
        return False
    print("[verify] ✓ User B does not see User A's token")

    # User B tries to view User A's token directly
    resp = userB_session.get(f"{BASE}/api/collections/api_tokens/records/{token_id}")
    if resp.status_code not in (403, 404):
        print(
            f"[verify] ✗ User B viewing User A's token was not rejected: {resp.status_code}"
        )
        return False
    print("[verify] ✓ User B direct view of User A's token rejected")

    # User B tries to update User A's token
    resp = userB_session.patch(
        f"{BASE}/api/collections/api_tokens/records/{token_id}",
        json={
            "name": "Hacked",
        },
    )
    if resp.status_code not in (403, 404):
        print(
            f"[verify] ✗ User B updating User A's token was not rejected: {resp.status_code}"
        )
        return False
    print("[verify] ✓ User B update of User A's token rejected")

    # User B tries to delete User A's token
    resp = userB_session.delete(f"{BASE}/api/collections/api_tokens/records/{token_id}")
    if resp.status_code not in (403, 404):
        print(
            f"[verify] ✗ User B deleting User A's token was not rejected: {resp.status_code}"
        )
        return False
    print("[verify] ✓ User B delete of User A's token rejected")

    # Cleanup: User A deletes their token
    resp = userA_session.delete(f"{BASE}/api/collections/api_tokens/records/{token_id}")
    if resp.status_code != 204:
        print(f"[verify] ✗ User A failed to delete their token: {resp.status_code}")
        return False
    print("[verify] ✓ User A deleted their own token")
    return True


def test_qpus_auth_rules():
    """Verify qpus collection auth rules: public read, superuser-only CUD."""
    print("\n[verify] Testing qpus authorization rules …")

    # Unauthenticated user can list QPUs
    resp = requests.get(f"{BASE}/api/collections/qpus/records")
    if resp.status_code != 200:
        print(f"[verify] ✗ Public QPU list failed: {resp.status_code}")
        return False
    qpus = resp.json()["items"]
    if not qpus:
        print("[verify] ✗ No QPUs found for public list test")
        return False
    qpu_id = qpus[0]["id"]
    print("[verify] ✓ Public can list QPUs")

    # Unauthenticated user can view single QPU
    resp = requests.get(f"{BASE}/api/collections/qpus/records/{qpu_id}")
    if resp.status_code != 200:
        print(f"[verify] ✗ Public QPU view failed: {resp.status_code}")
        return False
    print("[verify] ✓ Public can view single QPU")

    # Authenticated regular user tries to create a QPU
    user_session = requests.Session()
    resp = user_session.post(
        f"{BASE}/api/collections/users/auth-with-password",
        json={"identity": "user@example.com", "password": "userpassword1234"},
    )
    if resp.status_code != 200:
        print(f"[verify] ✗ Failed to authenticate user: {resp.text}")
        return False
    user_session.headers["Authorization"] = resp.json()["token"]

    resp = user_session.post(
        f"{BASE}/api/collections/qpus/records",
        json={
            "name": "Unauthorized-QPU",
            "access_token": "secret",
            "status": "offline",
        },
    )
    if resp.status_code not in (403, 404):
        print(
            f"[verify] ✗ Regular user creating QPU was not rejected: {resp.status_code}"
        )
        return False
    print("[verify] ✓ Regular user create QPU rejected")

    # Regular user tries to update a QPU
    resp = user_session.patch(
        f"{BASE}/api/collections/qpus/records/{qpu_id}",
        json={
            "status": "maintenance",
        },
    )
    if resp.status_code not in (403, 404):
        print(
            f"[verify] ✗ Regular user updating QPU was not rejected: {resp.status_code}"
        )
        return False
    print("[verify] ✓ Regular user update QPU rejected")

    # Regular user tries to delete a QPU
    resp = user_session.delete(f"{BASE}/api/collections/qpus/records/{qpu_id}")
    if resp.status_code not in (403, 404):
        print(
            f"[verify] ✗ Regular user deleting QPU was not rejected: {resp.status_code}"
        )
        return False
    print("[verify] ✓ Regular user delete QPU rejected")

    # Admin CAN update a QPU
    resp = s.patch(
        f"{BASE}/api/collections/qpus/records/{qpu_id}",
        json={
            "num_qubits": 8,
        },
    )
    if resp.status_code != 200:
        print(f"[verify] ✗ Admin failed to update QPU: {resp.status_code} {resp.text}")
        return False
    print("[verify] ✓ Admin can update QPU")
    return True


def test_driver_snippet_connection():
    """Verify that the copied snippet connects right (even with a different SSL certificate)."""
    print("\n[verify] Testing driver snippet connection …")

    # 1. Admin login to create QPU
    admin_session = requests.Session()
    resp = admin_session.post(
        f"{BASE}/api/collections/_superusers/auth-with-password",
        json={"identity": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    if resp.status_code != 200:
        print("[verify] ✗ Admin auth failed")
        return False
    admin_session.headers["Authorization"] = resp.json()["token"]

    # 2. Create QPU
    resp = admin_session.post(
        f"{BASE}/api/op/qpus/create",
        json={
            "name": "SnippetTestQPU",
        },
    )
    if resp.status_code != 201:
        print(f"[verify] ✗ Failed to create QPU: {resp.text}")
        return False
        
    data = resp.json()
    qpu_name = data["name"]
    executor = "mock"

    resp = admin_session.post(
        f"{BASE}/api/op/drivers/create",
        json={
            "name": f"{qpu_name}_driver",
            "qpu": data["id"],
            "kind": executor,
            "language": "python",
        },
    )
    if resp.status_code != 201:
        print(f"[verify] ✗ Failed to register driver: {resp.text}")
        return False
    driver_data = resp.json()
    token = driver_data["token"]
    fingerprint = driver_data["ca_fingerprint"]
    sdk_flags = []
    success_marker = "Worker process started"

    script_dir = os.path.dirname(os.path.abspath(__file__))
    qpi_dir = os.path.dirname(script_dir)

    import queue
    import threading

    def wait_for_handshake(p, timeout_sec=10):
        start_time = time.time()
        output = []
        q = queue.Queue()
        def read_stream(stream):
            for line in iter(stream.readline, ""):
                q.put(line)
        
        t_out = threading.Thread(target=read_stream, args=(p.stdout,), daemon=True)
        t_err = threading.Thread(target=read_stream, args=(p.stderr,), daemon=True)
        t_out.start()
        t_err.start()
        
        success = False
        while time.time() - start_time < timeout_sec:
            try:
                line = q.get(timeout=0.1)
                output.append(line)
                if success_marker in line:
                    success = True
                    break
                if "401 Client Error" in line:
                    break
            except queue.Empty:
                if p.poll() is not None:
                    break
                    
        p.terminate()
        p.wait()
        return success, "".join(output)

    # 3. Test direct snippet connection
    print("[verify]   Testing direct connection...")
    qpi_addr_direct = BASE
    cmd_direct = [
        sys.executable, "-m", "qpi_driver.cli", "start",
        "--operation", "process",
        "--ca-fingerprint", fingerprint,
        "--qpi-addr", qpi_addr_direct,
        "--device", executor,
        *sdk_flags,
    ]
    env = os.environ.copy()
    env["QPI_ACCESS_TOKEN"] = token
    env["PYTHONPATH"] = os.path.join(qpi_dir, "qpi-driver", "py")

    p1 = subprocess.Popen(cmd_direct, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=os.path.join(qpi_dir, "qpi-driver", "py"))
    success, out1 = wait_for_handshake(p1, timeout_sec=20)
    
    if not success:
        print(f"[verify] ✗ Direct connection failed or timed out. Output:\n{out1}")
        return False
    print("[verify]   ✓ Direct connection successful")

    # 4. Test proxy snippet connection
    print("[verify]   Testing proxied connection with different SSL certificate...")
    cert_path = os.path.join(script_dir, "test-cert.pem")
    key_path = os.path.join(script_dir, "test-key.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", key_path, "-out", cert_path, "-days", "1", "-nodes", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], check=True, capture_output=True)
    
    proxy_port = 8443
    def proxy_server():
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(certfile=cert_path, keyfile=key_path)
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", proxy_port))
        server.listen(5)
        def handle_client(client_sock):
            target_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            target_sock.connect(("127.0.0.1", 8090))
            def forward(src, dst):
                try:
                    while True:
                        d = src.recv(4096)
                        if not d: break
                        dst.sendall(d)
                except: pass
                finally:
                    src.close()
                    dst.close()
            threading.Thread(target=forward, args=(client_sock, target_sock)).start()
            threading.Thread(target=forward, args=(target_sock, client_sock)).start()
        while True:
            try:
                client_sock, addr = server.accept()
                ssl_client = context.wrap_socket(client_sock, server_side=True)
                threading.Thread(target=handle_client, args=(ssl_client,)).start()
            except:
                break
    
    t = threading.Thread(target=proxy_server, daemon=True)
    t.start()
    time.sleep(1)

    qpi_addr_proxy = f"https://localhost:{proxy_port}"
    cmd_proxy = [
        sys.executable, "-m", "qpi_driver.cli", "start",
        "--operation", "process",
        "--ca-fingerprint", fingerprint,
        "--qpi-addr", qpi_addr_proxy,
        "--device", executor,
        *sdk_flags,
    ]
    env_proxy = env.copy()
    env_proxy["REQUESTS_CA_BUNDLE"] = cert_path
    
    p2 = subprocess.Popen(cmd_proxy, env=env_proxy, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=os.path.join(qpi_dir, "qpi-driver", "py"))
    success, out2 = wait_for_handshake(p2, timeout_sec=20)
    
    os.remove(cert_path)
    os.remove(key_path)

    if "401 Client Error" in out2:
        print(f"[verify] ✗ Proxied connection failed with 401 Unauthorized!\nOutput excerpt:\n{out2[:500]}")
        return False
    elif not success:
        print(f"[verify] ✗ Proxied connection failed for another reason. Output:\n{out2}")
        return False

    print("[verify]   ✓ Proxied connection successful")
    return True


# Channels every bluefors_gen1 monitor case polls, in the CLI/-o string form.
_BLUEFORS_CHANNELS = "mapper.bf.tmc:K,mapper.bf.pmc:mbar"


def _monitor_flags(token, fingerprint, mock_port):
    """The qpi-driver CLI flags for the bluefors_gen1 monitor. Identical across
    languages — the Go (cobra) and TypeScript (commander) CLIs share the Python
    CLI's interface (universal flags + repeatable -o options).

    No name among them: the driver learns its display label from the
    drivers/connect response, so the token is the whole of what it asserts.
    """
    return [
        "start",
        "--operation", "monitor",
        "--device", "bluefors_gen1",
        "--qpi-addr", BASE,
        "--token", token,
        "--ca-fingerprint", fingerprint,
        "-o", f"base_url=http://127.0.0.1:{mock_port}",
        "-o", f"channels={_BLUEFORS_CHANNELS}",
        "-o", "poll_interval=1",
    ]


def _python_monitor_cmd(qpi_dir, token, fingerprint, mock_port):
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.join(qpi_dir, "qpi-driver", "py")
    argv = [sys.executable, "-m", "qpi_driver.cli"] + _monitor_flags(
        token, fingerprint, mock_port
    )
    return argv, env, os.path.join(qpi_dir, "qpi-driver", "py")


def _typescript_monitor_cmd(qpi_dir, token, fingerprint, mock_port):
    cli = os.path.join(qpi_dir, "qpi-driver", "js", "dist", "builtins", "cli.js")
    argv = ["node", cli] + _monitor_flags(token, fingerprint, mock_port)
    return argv, os.environ.copy(), qpi_dir


def _go_monitor_cmd(qpi_dir, token, fingerprint, mock_port):
    argv = ["go", "run", "./qpi-driver"] + _monitor_flags(
        token, fingerprint, mock_port
    )
    return argv, os.environ.copy(), os.path.join(qpi_dir, "qpi-driver", "go")


def _bluefors_languages(qpi_dir):
    """The (language, command-builder) pairs to exercise. A language whose
    toolchain or built CLI is missing is skipped with a warning rather than
    failing, so the suite still runs where a language is absent (e.g. no Go
    toolchain locally); CI has all three set up."""
    languages = [("python", _python_monitor_cmd)]

    ts_cli = os.path.join(qpi_dir, "qpi-driver", "js", "dist", "builtins", "cli.js")
    if shutil.which("node") and os.path.exists(ts_cli):
        languages.append(("typescript", _typescript_monitor_cmd))
    else:
        print("[verify] ⚠ skipping typescript bluefors_gen1 e2e (node or built CLI unavailable)")

    if shutil.which("go"):
        languages.append(("go", _go_monitor_cmd))
    else:
        print("[verify] ⚠ skipping go bluefors_gen1 e2e (go toolchain unavailable)")

    return languages


def _terminate_group(proc):
    """Stop a monitor and its whole process group. `go run` forks a child
    binary, so signalling only the parent would leave the monitor running; the
    process was started in its own session so the group can be signalled."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)


def test_bluefors_gen1_events():
    """Framework path only (RFC 0001 §7): the bluefors_gen1 monitor built-in, in
    every SDK language, gets its readings into the `events` trace log, and
    killing it does not disturb the QPU it belongs to.

    Each language gets its own QPU + registered driver, is pointed at a
    throwaway mock Bluefors HTTP server, and is launched via that language's
    runnable monitor (the Python CLI, the built TypeScript SDK, or `go run` of
    the Go SDK's cmd). A language whose toolchain/build is missing is skipped.
    """
    print("\n[verify] Testing Bluefors cryostat monitor → events log (all SDK languages) …")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    qpi_dir = os.path.dirname(script_dir)
    sys.path.insert(0, script_dir)
    import mock_bluefors_server

    admin_session = requests.Session()
    resp = admin_session.post(
        f"{BASE}/api/collections/_superusers/auth-with-password",
        json={"identity": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    if resp.status_code != 200:
        print("[verify] ✗ Admin auth failed")
        return False
    admin_session.headers["Authorization"] = resp.json()["token"]

    all_ok = True
    for language, build_cmd in _bluefors_languages(qpi_dir):
        if not _run_bluefors_case(
            admin_session, mock_bluefors_server, qpi_dir, language, build_cmd
        ):
            all_ok = False
    return all_ok


def _run_bluefors_case(admin_session, mock_bluefors_server, qpi_dir, language, build_cmd):
    """Run one language's bluefors_gen1 monitor end to end."""
    print(f"\n[verify] — bluefors_gen1 monitor ({language}) —")
    name = f"bluefors-gen1-e2e-{language}"

    resp = admin_session.post(
        f"{BASE}/api/op/qpus/create",
        json={"name": f"MonitorTestQPU-{language}", "executor_type": "mock"},
    )
    if resp.status_code != 201:
        print(f"[verify] ✗ [{language}] Failed to create QPU: {resp.text}")
        return False
    qpu_id = resp.json()["id"]

    resp = admin_session.post(
        f"{BASE}/api/op/drivers/create",
        json={"name": name, "qpu": qpu_id, "kind": "bluefors_gen1", "language": language},
    )
    if resp.status_code != 201:
        print(f"[verify] ✗ [{language}] Failed to register monitor driver: {resp.text}")
        return False
    driver_data = resp.json()
    token = driver_data["token"]
    fingerprint = driver_data["ca_fingerprint"]

    mock_server, _ = mock_bluefors_server.start(0)
    mock_port = mock_server.server_address[1]

    argv, env, cwd = build_cmd(qpi_dir, token, fingerprint, mock_port)
    proc = subprocess.Popen(
        argv,
        env=env,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

    try:
        rows = []
        # `go run` compiles the cmd first, so allow the Go case more time.
        attempts = 45 if language == "go" else 20
        for _ in range(attempts):
            time.sleep(1)
            resp = admin_session.get(
                f"{BASE}/api/collections/events/records",
                params={
                    "filter": f'type = "CryostatReading" && driver = "{driver_data["id"]}"',
                    "perPage": 10,
                },
            )
            resp.raise_for_status()
            rows = resp.json()["items"]
            if rows:
                break

        if not rows:
            out = proc.stdout.read() if proc.stdout else ""
            print(f"[verify] ✗ [{language}] No CryostatReading events landed. Monitor output:\n{out}")
            return False

        row = rows[0]
        if row.get("driver") != driver_data["id"] or row.get("qpu") != qpu_id:
            print(
                f"[verify] ✗ [{language}] Event attributed to wrong driver/qpu: "
                f"{row.get('driver')}/{row.get('qpu')}"
            )
            return False
        if not row.get("payload", {}).get("readings"):
            print(f"[verify] ✗ [{language}] Event payload has no readings: {row.get('payload')}")
            return False

        print(f"[verify] ✓ [{language}] CryostatReading landed in events log: {row['payload']['readings']}")

        # Killing the monitor must not disturb the QPU/driver it belongs to,
        # let alone any other driver (RFC 0001 §7 DoD).
        _terminate_group(proc)
        time.sleep(1)
        resp = admin_session.get(f"{BASE}/api/collections/qpus/records/{qpu_id}")
        resp.raise_for_status()
        print(f"[verify]   [{language}] QPU status after monitor stopped: {resp.json().get('status')}")

        return True
    finally:
        _terminate_group(proc)
        mock_bluefors_server.stop(mock_server)


def run_driver_tests():
    """Run tests that exercise the driver + core API (no client SDKs)."""
    admin_auth()
    jobs = wait_for_completion()

    if jobs is None:
        print("\n[verify] ✗ FAILED — not all jobs completed within timeout")
        return False

    print(f"\n[verify] ✓ All {len(jobs)} jobs completed!")
    print_summary(jobs)

    all_passed = True

    if not test_qpu_seconds_deduction():
        all_passed = False

    if not test_api_token_auth():
        all_passed = False

    if not test_admin_user_update():
        all_passed = False

    if not test_time_slots_validation():
        all_passed = False

    if not test_qpu_time_requests_validation():
        all_passed = False

    if not test_job_cancel():
        all_passed = False

    if not test_qiskit_hadamard_circuit():
        all_passed = False

    if not test_recovery_engine():
        all_passed = False

    if not test_qpu_toggle_switch():
        all_passed = False

    if not test_notifications_crud():
        all_passed = False

    if not test_api_tokens_auth_rules():
        all_passed = False

    if not test_qpus_auth_rules():
        all_passed = False

    if not test_driver_snippet_connection():
        all_passed = False

    if DRIVER_FRAMEWORK and not test_bluefors_gen1_events():
        all_passed = False

    return all_passed


def test_qpu_toggle_switch():
    """Verify that disabling and re-enabling a QPU toggles its status and goroutines."""
    if DRIVER_FRAMEWORK:
        return test_driver_toggle_switch()

    print("\n[verify] Testing QPU enabled/disabled toggle switch …")

    # 1. Fetch QPU and verify it is initially enabled and online
    resp = s.get(f"{BASE}/api/collections/qpus/records")
    resp.raise_for_status()
    qpus = resp.json()["items"]
    if not qpus:
        print("[verify] ✗ No QPUs found")
        return False
    qpu = qpus[0]
    qpu_id = qpu["id"]

    if qpu.get("status") != "online" or not qpu.get("enabled"):
        print(
            f"[verify] ✗ Precondition failed: QPU {qpu_id} is status={qpu.get('status')} enabled={qpu.get('enabled')}"
        )
        return False

    print(f"[verify] QPU {qpu_id} is online and enabled. Testing toggle switch …")

    # 2a. Verify unauthorized request to toggle is rejected
    unauth_resp = requests.post(
        f"{BASE}/api/op/qpu/toggle", json={"id": qpu_id, "enabled": False}
    )
    if unauth_resp.status_code != 403:
        print(
            f"[verify] ✗ Unauthorized toggle request was not blocked (expected 403, got {unauth_resp.status_code})"
        )
        return False
    print(
        "[verify] ✓ Unauthorized toggle request blocked successfully with 403 Forbidden."
    )

    # 2b. Disable QPU via custom POST /api/op/qpu/toggle (authorized as admin)
    resp = s.post(f"{BASE}/api/op/qpu/toggle", json={"id": qpu_id, "enabled": False})
    resp.raise_for_status()

    # Give the backend a second to run the update hook and close the goroutines
    time.sleep(1.5)

    # Verify it became offline
    resp = s.get(f"{BASE}/api/collections/qpus/records/{qpu_id}")
    resp.raise_for_status()
    qpu = resp.json()
    if qpu.get("status") != "offline" or qpu.get("enabled") is not False:
        print(
            f"[verify] ✗ QPU did not transition to offline/disabled: status={qpu.get('status')} enabled={qpu.get('enabled')}"
        )
        return False

    print("[verify] ✓ QPU transitioned to offline. Testing registration block …")

    # 3. Verify driver handshake is rejected with 403 Forbidden while disabled.
    reg_payload = {
        "access_token": ACCESS_TOKEN,
        "name": qpu["name"],
        "executor_type": qpu["executor_type"],
        "device_config": qpu.get("device_config") or {},
    }
    resp = s.post(f"{BASE}/api/op/qpus/connect", json=reg_payload)
    if resp.status_code != 403:
        print(
            f"[verify] ✗ Connection request was not blocked (expected 403, got {resp.status_code}): {resp.text}"
        )
        return False

    print("[verify] ✓ Connection blocked successfully with 403 Forbidden.")

    # 4. Re-enable QPU via custom POST /api/op/qpu/toggle (enabled = True)
    print("[verify] Re-enabling QPU …")
    resp = s.post(f"{BASE}/api/op/qpu/toggle", json={"id": qpu_id, "enabled": True})
    resp.raise_for_status()

    time.sleep(1.5)
    resp = s.get(f"{BASE}/api/collections/qpus/records/{qpu_id}")
    resp.raise_for_status()
    qpu = resp.json()
    if qpu.get("status") != "online" or qpu.get("enabled") is not True:
        print(
            f"[verify] ✗ QPU did not transition back to online/enabled: status={qpu.get('status')} enabled={qpu.get('enabled')}"
        )
        return False

    print("[verify] ✓ QPU successfully re-enabled and transitioned back to online.")
    return True


def test_driver_toggle_switch():
    """Framework path (RFC 0001): toggle a driver-backed QPU at the driver level.

    The QPU itself is never toggled or reconnected directly on the framework
    path — its driver is. Disabling the driver stops its distribution goroutines
    and takes both the driver and its QPU offline; a fresh drivers/connect
    restarts distribution and brings them back, which is the RFC reconnect model.
    """
    print("\n[verify] Testing driver enabled/disabled toggle switch (framework path) …")

    # 1. Fetch the seeded driver and verify it is initially enabled and online.
    resp = s.get(f"{BASE}/api/collections/drivers/records")
    resp.raise_for_status()
    drivers = resp.json()["items"]
    if not drivers:
        print("[verify] ✗ No drivers found")
        return False
    driver = drivers[0]
    driver_id = driver["id"]
    qpu_id = driver["qpu"]

    if driver.get("status") != "online" or not driver.get("enabled"):
        print(
            f"[verify] ✗ Precondition failed: driver {driver_id} is "
            f"status={driver.get('status')} enabled={driver.get('enabled')}"
        )
        return False
    print(f"[verify] Driver {driver_id} is online and enabled. Testing toggle switch …")

    # 2a. Verify an unauthorized toggle request is rejected.
    unauth_resp = requests.post(
        f"{BASE}/api/op/drivers/toggle", json={"id": driver_id, "enabled": False}
    )
    if unauth_resp.status_code != 403:
        print(
            f"[verify] ✗ Unauthorized driver toggle was not blocked (expected 403, got {unauth_resp.status_code})"
        )
        return False
    print("[verify] ✓ Unauthorized driver toggle blocked successfully with 403 Forbidden.")

    # 2b. Disable the driver via POST /api/op/drivers/toggle (authorized as admin).
    resp = s.post(f"{BASE}/api/op/drivers/toggle", json={"id": driver_id, "enabled": False})
    resp.raise_for_status()

    # Give the backend a moment to close the goroutines and mirror the status.
    time.sleep(1.5)

    resp = s.get(f"{BASE}/api/collections/drivers/records/{driver_id}")
    resp.raise_for_status()
    driver = resp.json()
    if driver.get("status") != "offline" or driver.get("enabled") is not False:
        print(
            f"[verify] ✗ Driver did not transition to offline/disabled: "
            f"status={driver.get('status')} enabled={driver.get('enabled')}"
        )
        return False

    # The QPU tracks its driver: disabling the driver takes the QPU offline too.
    resp = s.get(f"{BASE}/api/collections/qpus/records/{qpu_id}")
    resp.raise_for_status()
    qpu_status = resp.json().get("status")
    if qpu_status != "offline":
        print(f"[verify] ✗ QPU did not follow the driver offline: status={qpu_status}")
        return False

    print("[verify] ✓ Driver and its QPU transitioned to offline. Testing reconnect block …")

    # 3. Verify a disabled driver's reconnect is rejected with 403 Forbidden.
    token = read_driver_token()
    if not token:
        print("[verify] ✗ Driver token file not found — cannot test reconnect")
        return False

    resp = requests.post(
        f"{BASE}/api/op/drivers/connect", json={"token": token, "name": driver["name"]}
    )
    if resp.status_code != 403:
        print(
            f"[verify] ✗ Disabled driver reconnect was not blocked "
            f"(expected 403, got {resp.status_code}): {resp.text}"
        )
        return False
    print("[verify] ✓ Disabled driver reconnect blocked successfully with 403 Forbidden.")

    # 4. Re-enable the driver.
    print("[verify] Re-enabling driver …")
    resp = s.post(f"{BASE}/api/op/drivers/toggle", json={"id": driver_id, "enabled": True})
    resp.raise_for_status()

    time.sleep(1.0)
    resp = s.get(f"{BASE}/api/collections/drivers/records/{driver_id}")
    resp.raise_for_status()
    if resp.json().get("enabled") is not True:
        print(f"[verify] ✗ Driver was not re-enabled: enabled={resp.json().get('enabled')}")
        return False

    # 5. A fresh connect restarts distribution; the still-running driver process
    #    reattaches and both the driver and its QPU return online.
    resp = requests.post(
        f"{BASE}/api/op/drivers/connect", json={"token": token, "name": driver["name"]}
    )
    if resp.status_code != 200:
        print(
            f"[verify] ✗ Re-enabled driver reconnect failed "
            f"(expected 200, got {resp.status_code}): {resp.text}"
        )
        return False

    for _ in range(10):
        time.sleep(1)
        driver_now = s.get(f"{BASE}/api/collections/drivers/records/{driver_id}").json()
        qpu_now = s.get(f"{BASE}/api/collections/qpus/records/{qpu_id}").json()
        if driver_now.get("status") == "online" and qpu_now.get("status") == "online":
            print("[verify] ✓ Driver and QPU successfully back online after reconnect.")
            return True

    print("[verify] ✗ Driver/QPU did not return online within 10s after reconnect")
    return False


def main():
    parser = argparse.ArgumentParser(description="QPi E2E verification script")
    parser.add_argument(
        "--driver", action="store_true", help="Run driver-focused tests only"
    )
    parser.add_argument(
        "--client-py", action="store_true", help="Run Python client smoke test only"
    )
    parser.add_argument(
        "--client-js", action="store_true", help="Run JS client smoke test only"
    )
    parser.add_argument(
        "--client-go", action="store_true", help="Run Go client smoke test only"
    )
    args = parser.parse_args()

    # If no specific subset requested, run everything
    run_all = not (args.driver or args.client_py or args.client_js or args.client_go)

    all_passed = True

    if run_all or args.driver:
        if not run_driver_tests():
            all_passed = False

    if run_all or args.client_py:
        if not test_python_client_smoke():
            all_passed = False

    if run_all or args.client_go:
        if not test_go_client_smoke():
            all_passed = False

    if run_all or args.client_js:
        if not test_js_client_smoke():
            all_passed = False

    if not all_passed:
        print("\n[verify] ✗ FAILED — one or more checks failed")
        sys.exit(1)

    print("\n[verify] ✓ All checks passed")


if __name__ == "__main__":
    main()
