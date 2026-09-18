"""Every failure path of the operation runner API — the 26% that reported no number (okengine#600).

`okengine-operations/app.py` is a live service baked into the deployed image, and it was absent
from the coverage source list, so nothing had ever computed a number for it. Measuring it for the
first time put 41 statements on the board, and one of them was a defect: `pid_alive` reaches
`os.kill(0, 0)` for any request record without a pid, which does not raise — it probes the
caller's own process group and reports an absent worker as alive, so `refresh`'s crash-safety
branch could never fire and the request stayed `running` for good.

tests/test_operation_api.py owns the happy lifecycle (plan -> run -> reap -> terminal receipt).
This file owns what happens when each step goes wrong, because those are the paths an operator
meets on the worst day and the ones nothing was watching.

Doubles appear at exactly one boundary — `subprocess.Popen`, to make a spawn fail — and the
assertion that follows is behavioural: the declared lock becomes acquirable again, which is the
consequence an operator cares about, not that some release method was called.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


class FakeFastAPI:
    """The routes are plain functions; FastAPI itself is not what is under test here.

    Faking the framework rather than driving it through TestClient keeps the suite free of an
    httpx dependency that requirements-dev.txt does not pin — a test that cannot run in CI
    measures nothing.
    """

    def __init__(self, *args, **kwargs):
        pass

    def add_middleware(self, *args, **kwargs):
        pass

    def get(self, *args, **kwargs):
        return lambda fn: fn

    def post(self, *args, **kwargs):
        return lambda fn: fn


class FakeRequest:
    def __init__(self, value):
        self.value = value

    async def json(self):
        return self.value


def load_app():
    fastapi = types.ModuleType("fastapi")
    fastapi.FastAPI, fastapi.HTTPException, fastapi.Request = FakeFastAPI, HTTPException, FakeRequest
    prior = sys.modules.get("fastapi")
    sys.modules["fastapi"] = fastapi
    try:
        spec = importlib.util.spec_from_file_location(
            "operation_api_edges_app", ROOT / "okengine-operations/app.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if prior is None:
            del sys.modules["fastapi"]
        else:
            sys.modules["fastapi"] = prior


WORKER = (
    "import argparse,json,os\n"
    "p=argparse.ArgumentParser();p.add_argument('--target-vault');"
    "p.add_argument('--dry-run',action='store_true');p.add_argument('--all',action='store_true');"
    "a=p.parse_args()\n"
    "print(json.dumps({'operation':'fixture-review',"
    "'run_id':os.environ.get('OKENGINE_OPERATION_RUN_ID') or 'plan',"
    "'status':'planned' if a.dry_run else 'succeeded',"
    "'actor_inventory':[],'dimensions':[]}))\n"
)


@pytest.fixture
def api(tmp_path):
    """A real deployment tree with one allowed operation that declares a lock."""
    app = load_app()
    (tmp_path / "wiki").mkdir()
    (tmp_path / "crons/scripts").mkdir(parents=True)
    op = tmp_path / "operations/fixture"
    op.mkdir(parents=True)
    (op / "operation.yaml").write_text(yaml.safe_dump({
        "operation_api": 1, "name": "fixture-review", "owner": "fixture",
        "title": "Fixture review", "entrypoint": "crons/scripts/fixture.py",
        "mutates": True, "locks": ["fixture-lock"],
        "supports": {"plan": True, "resume": False, "cancel": False},
    }), encoding="utf-8")
    (tmp_path / "crons/scripts/fixture.py").write_text(WORKER, encoding="utf-8")
    app.DEPLOYMENT = tmp_path
    app.REQUESTS = tmp_path / ".okengine/operations/requests"
    app.ALLOWED = {"fixture-review"}
    return app


# --- the allowlist and the manifest --------------------------------------------------------------

def test_an_enabled_name_with_no_manifest_on_disk_is_a_404_not_a_crash(api):
    """403 and 404 answer different questions: "you may not" versus "there is no such thing"."""
    api.ALLOWED = {"fixture-review", "ghost-review"}
    with pytest.raises(HTTPException) as raised:
        api.manifest("ghost-review")
    assert raised.value.status_code == 404


def test_listing_shows_only_the_operations_enabled_for_the_api(api):
    """discover() reads every operation in the deployment; the API must project only the
    allowlisted ones, or the cockpit advertises buttons that always 403."""
    assert [row["name"] for row in api.list_operations()["operations"]] == ["fixture-review"]
    api.ALLOWED = set()
    assert api.list_operations()["operations"] == []


def test_healthz_answers_without_a_deployment(api):
    """The liveness probe must not depend on anything it could be probing the health of."""
    assert api.healthz() == {"ok": True}


# --- request bodies are untrusted ---------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"arguments": "--all"},                     # a string is iterable; that is the trap
    {"arguments": [1]},                         # not a string
    {"arguments": ["x" * 501]},                 # one oversized element
    {"arguments": ["--all"] * 41},              # too many
])
def test_arguments_that_are_not_a_bounded_string_list_are_refused(api, body):
    with pytest.raises(HTTPException) as raised:
        api.arguments(body)
    assert raised.value.status_code == 400


def test_a_body_that_is_not_an_object_yields_no_arguments(api):
    assert api.arguments(["not", "a", "dict"]) == []
    assert api.arguments({"arguments": ["--all"]}) == ["--all"]


@pytest.mark.parametrize("request_id", ["", "../etc/passwd", "ABCDEF", "a" * 31, "a" * 33])
def test_a_request_id_that_is_not_a_hex_uuid_never_becomes_a_path(api, request_id):
    """request_path builds a filesystem path from caller input; the regex is the whole guard."""
    with pytest.raises(HTTPException) as raised:
        api.request_path(request_id)
    assert raised.value.status_code == 400


def test_a_missing_request_is_404_and_an_unreadable_one_is_500(api):
    """Distinguishable on purpose: absent is the caller's problem, corrupt is ours."""
    missing = "a" * 32
    with pytest.raises(HTTPException) as absent:
        api.load_request(missing)
    assert absent.value.status_code == 404

    corrupt = "b" * 32
    api.REQUESTS.mkdir(parents=True, exist_ok=True)
    api.request_path(corrupt).write_text("{not json", encoding="utf-8")
    with pytest.raises(HTTPException) as unreadable:
        api.load_request(corrupt)
    assert unreadable.value.status_code == 500


def test_reading_a_request_returns_its_refreshed_projection(api):
    request_id = "c" * 32
    api.REQUESTS.mkdir(parents=True, exist_ok=True)
    api.request_path(request_id).write_text(json.dumps({
        "request_id": request_id, "status": "running", "pid": 0}), encoding="utf-8")
    assert api.operation_request(request_id)["status"] == "failed"


# --- pid liveness: the defect okengine#600 surfaced ----------------------------------------------

def test_a_record_with_no_usable_pid_is_not_alive(api):
    """os.kill(0, 0) does not raise — it probes the caller's OWN process group and succeeds, so
    before the guard a request without a pid was reported as having a live worker forever and
    could never be marked failed. os.kill(-1, 0) is the same mistake with a wider blast radius:
    a value read from a JSON file on disk turning a one-pid check into a fleet-wide probe."""
    assert api.pid_alive(0) is False
    assert api.pid_alive(-1) is False


def test_an_out_of_range_persisted_pid_cannot_block_startup_reconciliation(api):
    api.REQUESTS.mkdir(parents=True, exist_ok=True)
    path = api.REQUESTS / "oversized-pid.json"
    path.write_text(json.dumps({
        "request_id": "oversized-pid", "status": "running", "pid": 1 << 128,
    }), encoding="utf-8")

    assert api.reconcile_requests() == {"checked": 1, "reconciled": 1, "errors": 0}
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "failed"


def test_a_positive_pid_is_decided_by_the_operating_system_not_by_the_guard(api, monkeypatch):
    """The guard rejects values that cannot name a process. It must not quietly extend that to
    values that can — a boundary one off in the other direction would report a real worker as
    dead and let the crash-safety branch fail a run that is still going.

    pid 1 is the case that separates the two, and it is why this is asserted through `os.kill`
    rather than through the return value alone: an unprivileged process cannot signal init, so
    `pid_alive(1)` is False either way here. Whether the SYSCALL WAS REACHED is the observable
    difference, and it is the thing actually being claimed.
    """
    asked = []
    monkeypatch.setattr(api.os, "kill", lambda pid, sig: asked.append((pid, sig)))
    assert api.pid_alive(1) is True
    assert asked == [(1, 0)], "the guard answered for a pid the operating system should have"


def test_a_live_pid_is_alive_and_a_reaped_one_is_not(api):
    assert api.pid_alive(os.getpid()) is True
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    assert api.pid_alive(done.pid) is False


def test_an_exited_but_unreaped_child_is_not_alive(api):
    """A zombie answers os.kill(pid, 0) successfully — the pid is still allocated. Only /proc
    state 'Z' separates it from a running worker, and treating it as alive is how a finished
    operation keeps reporting `running`."""
    zombie = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        os.waitid(os.P_PID, zombie.pid, os.WEXITED | os.WNOWAIT)
        assert api.pid_alive(zombie.pid) is False
    finally:
        zombie.wait()


# --- refresh: projecting the engine-owned receipt ------------------------------------------------

def test_a_request_with_no_run_id_yet_is_projected_unchanged(api, tmp_path):
    """The window between writing the request and the engine writing its receipt."""
    path = tmp_path / "req.json"
    value = api.refresh(path, {"request_id": "x", "status": "running", "pid": os.getpid()})
    assert value["status"] == "running"
    assert "receipt" not in value
    assert json.loads(path.read_text())["status"] == "running", "refresh must persist"


@pytest.mark.parametrize("body, why", [
    ("{not json", "a half-written receipt must not crash the read path"),
    ("{}", "an empty receipt carries no status to project"),
])
def test_an_unusable_receipt_leaves_the_projection_alone(api, tmp_path, body, why):
    run_id = "fixture-review-20260817T000000Z"
    receipt = api.operation_run.receipt_path(api.DEPLOYMENT, "fixture-review", run_id)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(body, encoding="utf-8")
    value = api.refresh(tmp_path / "req.json", {
        "request_id": "x", "operation": "fixture-review", "run_id": run_id,
        "status": "running", "pid": os.getpid()})
    assert value["status"] == "running", why


def test_a_terminal_receipt_supplies_status_progress_and_finish_time(api, tmp_path):
    run_id = "fixture-review-20260817T000001Z"
    receipt = api.operation_run.receipt_path(api.DEPLOYMENT, "fixture-review", run_id)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({
        "status": "succeeded", "finished_at": "2026-08-17T00:00:02Z",
        "result": {"actor_inventory": [{"ref": "a"}, {"ref": "b"}],
                   "dimensions": ["one", "two", "three"],
                   "lanes": [{"status": "succeeded"}, {"status": "not-applicable"},
                             {"status": "failed"}]}}), encoding="utf-8")
    value = api.refresh(tmp_path / "req.json", {
        "request_id": "x", "operation": "fixture-review", "run_id": run_id,
        "status": "running", "pid": 0})
    assert value["status"] == "succeeded"
    assert value["finished_at"] == "2026-08-17T00:00:02Z"
    assert value["progress"] == {"actors": 2, "lanes_complete": 2, "lanes_total": 3}


def test_a_terminal_receipt_without_a_finish_time_is_stamped_now(api, tmp_path):
    run_id = "fixture-review-20260817T000003Z"
    receipt = api.operation_run.receipt_path(api.DEPLOYMENT, "fixture-review", run_id)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({"status": "failed"}), encoding="utf-8")
    value = api.refresh(tmp_path / "req.json", {
        "request_id": "x", "operation": "fixture-review", "run_id": run_id,
        "status": "running", "pid": 0})
    assert value["status"] == "failed" and value["finished_at"].endswith("Z")


def test_a_request_orphaned_by_a_service_restart_is_marked_failed(api, tmp_path):
    """_PROCESSES does not survive a restart, so a `running` request whose worker pid is gone and
    which no live reaper owns would otherwise hang forever."""
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    value = api.refresh(tmp_path / "req.json", {
        "request_id": "orphan", "status": "running", "pid": done.pid})
    assert value["status"] == "failed" and value["finished_at"]


def test_a_reaper_still_finalizing_in_this_process_is_not_pre_empted(api, tmp_path):
    """The dead-pid window between the worker exiting and finalize writing the terminal receipt
    is normal. Marking it failed there overwrites a successful handoff."""
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    api._PROCESSES["owned"] = done
    try:
        value = api.refresh(tmp_path / "req.json", {
            "request_id": "owned", "status": "running", "pid": done.pid})
        assert value["status"] == "running"
    finally:
        api._PROCESSES.pop("owned", None)


def test_startup_reconciles_a_persisted_orphan_before_serving(api, tmp_path):
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    api.REQUESTS.mkdir(parents=True, exist_ok=True)
    path = api.REQUESTS / "orphan.json"
    path.write_text(json.dumps({
        "request_id": "orphan", "status": "running", "pid": done.pid,
    }), encoding="utf-8")

    async def enter_startup():
        async with api._lifespan(api.app):
            return json.loads(path.read_text(encoding="utf-8"))

    value = asyncio.run(enter_startup())
    assert value["status"] == "failed"
    assert value["finished_at"]


def test_startup_sweep_handles_an_absent_directory_and_non_object_record(api, capsys):
    assert api.reconcile_requests() == {"checked": 0, "reconciled": 0, "errors": 0}

    api.REQUESTS.mkdir(parents=True)
    (api.REQUESTS / "array.json").write_text("[]", encoding="utf-8")

    assert api.reconcile_requests() == {"checked": 0, "reconciled": 0, "errors": 1}
    assert "array.json" in capsys.readouterr().err


def test_startup_sweep_preserves_current_reaper_and_isolates_bad_records(api, tmp_path, capsys):
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    api.REQUESTS.mkdir(parents=True, exist_ok=True)
    owned = api.REQUESTS / "z-owned.json"
    owned.write_text(json.dumps({
        "request_id": "owned", "status": "running", "pid": done.pid,
    }), encoding="utf-8")
    (api.REQUESTS / "broken.json").write_text("{broken", encoding="utf-8")
    (api.REQUESTS / "a-settled.json").write_text(json.dumps({
        "request_id": "settled", "status": "succeeded", "pid": done.pid,
    }), encoding="utf-8")
    api._PROCESSES["owned"] = done
    try:
        stats = api.reconcile_requests()
    finally:
        api._PROCESSES.pop("owned", None)

    assert stats == {"checked": 1, "reconciled": 0, "errors": 1}
    assert json.loads(owned.read_text())["status"] == "running"
    assert "broken.json" in capsys.readouterr().err


def test_startup_reconciliation_counts_value_changes_not_string_identity_or_order(api, monkeypatch):
    api.REQUESTS.mkdir(parents=True, exist_ok=True)
    path = api.REQUESTS / "running.json"
    path.write_text(json.dumps({
        "request_id": "running", "status": "running", "pid": 1,
    }), encoding="utf-8")

    same_value = "".join(["run", "ning"])
    monkeypatch.setattr(api, "refresh", lambda _path, _value: {"status": same_value})
    assert api.reconcile_requests()["reconciled"] == 0

    monkeypatch.setattr(api, "refresh", lambda _path, _value: {"status": "waiting"})
    assert api.reconcile_requests()["reconciled"] == 1


# --- the reaper ----------------------------------------------------------------------------------

def test_the_reaper_releases_its_locks_even_when_the_request_vanished(api, monkeypatch):
    """A deleted request record must not strand the declared lock — the next run would 409 for
    ever against a lockset nobody holds."""
    finalized = []
    monkeypatch.setattr(api.operation_run, "finalize",
                        lambda *args, **kwargs: finalized.append(kwargs))
    lockset = api.operation_run.acquire_lockset(api.DEPLOYMENT, ["fixture-lock"], "run-1")
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    api._PROCESSES["gone"] = done

    api.reap("gone", done, {"name": "fixture-review"}, "run-1", {}, lockset,
             api.DEPLOYMENT / "absent.log")

    assert finalized, "finalize is the engine-owned terminal and still runs"
    assert "gone" not in api._PROCESSES
    api.operation_run.acquire_lockset(api.DEPLOYMENT, ["fixture-lock"], "run-2").release()


# --- plan and run ---------------------------------------------------------------------------------

def test_a_worker_that_cannot_plan_is_a_400(api):
    (api.DEPLOYMENT / "crons/scripts/fixture.py").write_text(
        "import sys; sys.exit(3)\n", encoding="utf-8")
    with pytest.raises(HTTPException) as raised:
        asyncio.run(api.plan_operation("fixture-review", FakeRequest({"arguments": []})))
    assert raised.value.status_code == 400


def test_a_second_run_is_refused_while_the_declared_lock_is_held(api):
    """The lockset is held by this service until the reaper finalizes; a conflict is a 409, not a
    second worker mutating the same vault."""
    plan = asyncio.run(api.plan_operation("fixture-review", FakeRequest({"arguments": []})))
    held = api.operation_run.acquire_lockset(api.DEPLOYMENT, ["fixture-lock"], "other-run")
    try:
        with pytest.raises(HTTPException) as raised:
            asyncio.run(api.run_operation("fixture-review", FakeRequest(
                {"arguments": [], "plan_digest": plan["snapshot_digest"]})))
        assert raised.value.status_code == 409
    finally:
        held.release()


def test_a_failed_spawn_releases_the_lock_it_had_already_taken(api, monkeypatch):
    """The one double in this file, at the OS boundary. The assertion is behavioural: the lock is
    acquirable again afterwards. Without the release, one failed spawn wedges the operation for
    the lifetime of the service."""
    plan = asyncio.run(api.plan_operation("fixture-review", FakeRequest({"arguments": []})))

    def boom(*args, **kwargs):
        raise OSError("no such executable")

    # Rebind app.py's OWN `subprocess` global, not the module's Popen attribute. Patching
    # subprocess.Popen globally makes the plan REVALIDATION on the line above fail instead —
    # operation_run.plan shells out too, and subprocess.run calls Popen underneath — so the test
    # raises OSError from the wrong call, never reaches the spawn, and passes while proving
    # nothing. Coverage is what caught it: the handler stayed unexecuted at 99%.
    shim = types.SimpleNamespace(Popen=boom)
    monkeypatch.setattr(api, "subprocess", shim)
    with pytest.raises(OSError):
        asyncio.run(api.run_operation("fixture-review", FakeRequest(
            {"arguments": [], "plan_digest": plan["snapshot_digest"]})))
    monkeypatch.undo()

    api.operation_run.acquire_lockset(api.DEPLOYMENT, ["fixture-lock"], "after").release()


# --- the bearer middleware ------------------------------------------------------------------------

def test_the_health_probe_is_reachable_without_a_token(api):
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["path"])

    async def receive():
        return {}

    async def send(value):
        raise AssertionError(f"nothing should be sent past the middleware: {value}")

    api.TOKEN = "internal-token"
    asyncio.run(api.BearerAuth(inner)({"type": "http", "path": "/healthz", "headers": []},
                                      receive, send))
    assert seen == ["/healthz"]


def test_a_correct_token_reaches_the_application(api):
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["path"])

    async def receive():
        return {}

    async def send(value):
        raise AssertionError(f"unauthorized response for a valid token: {value}")

    api.TOKEN = "internal-token"
    asyncio.run(api.BearerAuth(inner)(
        {"type": "http", "path": "/operations",
         "headers": [(b"authorization", b"Bearer internal-token")]}, receive, send))
    assert seen == ["/operations"]


def test_an_unset_token_refuses_everything_rather_than_admitting_everything(api):
    """`not TOKEN` is checked first on purpose: an empty configured token must never make an empty
    supplied token a match."""
    sent = []

    async def inner(scope, receive, send):
        raise AssertionError("an unconfigured service must not serve")

    async def receive():
        return {}

    async def send(value):
        sent.append(value)

    api.TOKEN = ""
    asyncio.run(api.BearerAuth(inner)(
        {"type": "http", "path": "/operations", "headers": [(b"authorization", b"Bearer ")]},
        receive, send))
    assert sent[0]["status"] == 401


def test_a_non_http_scope_passes_straight_through(api):
    """Lifespan and websocket scopes carry no authorization header and are not requests."""
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    async def receive():
        return {}

    async def send(value):
        raise AssertionError(value)

    api.TOKEN = "internal-token"
    asyncio.run(api.BearerAuth(inner)({"type": "lifespan"}, receive, send))
    assert seen == ["lifespan"]
