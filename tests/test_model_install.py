"""Installing an ASR model from Settings: the volume protocol, the installer, and the API.

The installer code is real. Only the download child is a fake script, because a real one
downloads gigabytes; it is a real process in its own process group, so cancel is real too.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import huggingface_hub
import pytest
from httpx import ASGITransport, AsyncClient
from test_asr_models import _qwen_dir, _whisper_dir
from test_transcription_api import _authorize

from asr_studio import model_installer, routes
from asr_studio.app import create_app
from asr_studio.asr_models import CATALOG
from asr_studio.config import Settings
from asr_studio.model_install import STALE_SECONDS, InstallStore
from asr_studio.model_installer import Installer
from asr_studio.runner import prepared_engine_directory
from asr_studio.runtime import RuntimeProfile

PHO = "pho-whisper-large"
QWEN = "qwen3-asr-1.7b"

CHILD = """\
import os, shutil, signal, subprocess, sys, time
from pathlib import Path

mode, source, staging, pids = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])
if mode == "ok":
    shutil.copytree(source, staging / "model")
elif mode == "hang":  # a download that never ends, with a helper process of its own
    (staging / "model").mkdir()
    (staging / "model" / "part").write_bytes(b"x" * 4096)
    # a helper that ignores SIGTERM: only killing the whole group ends it
    helper = subprocess.Popen(["sh", "-c", "trap '' TERM; sleep 60"])
    pids.write_text(str(helper.pid))
    time.sleep(60)
elif mode == "gate":  # downloads some bytes, then waits for the test to open the gate
    (staging / "model").mkdir()
    (staging / "model" / "part").write_bytes(b"x" * 4096)
    while not pids.with_name("gate").exists():  # bytes keep arriving while it waits
        with open(staging / "model" / "part", "ab") as part:
            part.write(b"x" * 1024)
        time.sleep(0.2)
    shutil.rmtree(staging / "model")
    shutil.copytree(source, staging / "model")
elif mode == "convert":
    (staging / ".phase").write_text("converting")
    time.sleep(60)
elif mode == "empty":  # finishes, but what it left is not a model
    (staging / "model").mkdir()
elif mode == "crash":
    sys.exit(1)
elif mode == "oom":
    os.kill(os.getpid(), signal.SIGKILL)
else:
    sys.exit(int(mode))  # the child's own error codes
"""


@pytest.fixture(autouse=True)
def roomy_disk(monkeypatch):
    monkeypatch.setattr(
        model_installer.shutil, "disk_usage", lambda _path: SimpleNamespace(free=10**12)
    )


class Rig:
    """An installer over a temp volume and temp models folder, with a scriptable child."""

    def __init__(self, tmp_path: Path) -> None:
        self.models = tmp_path / "models"
        self.models.mkdir()
        self.settings = Settings(
            data_dir=tmp_path / "data",
            auth_dir=tmp_path / "auth",
            install_dir=tmp_path / "install",
            asr_gpu_model_path=self.models / "whisper-large-v3",
            asr_cpu_model_path=self.models / "whisper-small",
            asr_phowhisper_model_path=self.models / PHO,
            asr_qwen_model_path=self.models / QWEN,
        )
        self.store = InstallStore(self.settings.install_dir)
        self.script = tmp_path / "child.py"
        self.script.write_text(CHILD, encoding="utf-8")
        self.pids = tmp_path / "pids"
        self.source = _whisper_dir(tmp_path / "source-whisper")
        self.mode = "ok"
        self.spawned = 0
        self.installer = Installer(
            self.settings, self.store, argv=self.argv, poll_seconds=0.02, kill_grace_seconds=2
        )

    def argv(self, model, staging):
        self.spawned += 1
        source = self.source if model.engine != "qwen" else _qwen_dir(self.source.parent / "q")
        return [
            sys.executable,
            str(self.script),
            self.mode,
            str(source),
            str(staging),
            str(self.pids),
        ]

    def target(self, model_id: str = PHO) -> Path:
        return self.models / model_id

    def leftovers(self) -> list[str]:
        return sorted(path.name for path in self.models.glob(".install-*"))

    def run(self, model_id: str = PHO) -> dict:
        self.store.request(model_id)
        self.installer.tick()
        return self.store.job(model_id)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def _wait(condition, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def _running_job(rig: Rig, model_id: str = PHO) -> threading.Thread:
    rig.store.request(model_id)
    thread = threading.Thread(target=rig.installer.tick, daemon=True)
    thread.start()
    return thread


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    if not Path("/proc").is_dir():
        return True
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


# -- AC-05: one job per model -----------------------------------------------------------------


def test_many_simultaneous_requests_for_one_model_make_one_job(rig):
    ids = []
    barrier = threading.Barrier(16)

    def ask():
        barrier.wait()
        ids.append(rig.store.request(PHO))

    threads = [threading.Thread(target=ask) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(set(ids)) == 1
    assert [p.name for p in (rig.settings.install_dir / "requests").iterdir()] == [f"{PHO}.json"]
    assert rig.store.pending()[0]["id"] == ids[0]


def test_a_prepared_model_is_never_touched_and_no_download_starts(rig):
    _whisper_dir(rig.target())
    (rig.target() / "operator-note").write_text("keep", encoding="utf-8")
    before = sorted(p.name for p in rig.target().iterdir())

    job = rig.run()

    assert rig.spawned == 0
    assert job == {"state": "idle"}
    assert sorted(p.name for p in rig.target().iterdir()) == before
    assert rig.leftovers() == []


# -- AC-01 / AC-03: install, progress, atomic publication ----------------------------------------


def test_an_install_publishes_a_prepared_model_and_leaves_nothing_behind(rig):
    assert rig.run() == {"state": "idle"}

    prepared_engine_directory("whisper_vi", rig.target())
    assert rig.leftovers() == []
    assert not (rig.settings.install_dir / "requests" / f"{PHO}.json").exists()
    assert rig.store.status(PHO) is None


def test_the_target_does_not_exist_while_the_download_is_staged(rig):
    rig.mode = "hang"
    thread = _running_job(rig)
    _wait(lambda: rig.pids.exists())

    assert not rig.target().exists()  # the worker has nothing half-written to find
    [staged] = rig.leftovers()
    assert staged == f".install-{PHO}-{rig.store.pending()[0]['id']}"

    rig.store.cancel(PHO)
    thread.join(10)


def test_progress_reports_the_bytes_that_arrived_and_the_converting_phase(rig):
    rig.mode = "hang"
    thread = _running_job(rig)
    _wait(lambda: rig.store.job(PHO).get("bytes_done", 0) >= 4096)
    job = rig.store.job(PHO)
    assert (job["state"], job["phase"]) == ("running", "downloading")
    assert job["bytes_total"] == CATALOG[PHO].download_bytes
    rig.store.cancel(PHO)
    thread.join(10)

    rig.mode = "convert"
    thread = _running_job(rig)
    _wait(lambda: rig.store.job(PHO).get("phase") == "converting")
    rig.store.cancel(PHO)
    thread.join(10)


def test_a_published_model_is_chosen_without_a_restart_once_the_job_ends(rig):
    from asr_studio.asr_models import model_choices

    profile = RuntimeProfile("cuda", "float16", rig.settings.asr_gpu_model_path)
    _whisper_dir(rig.settings.asr_gpu_model_path)
    before = next(c for c in model_choices(rig.settings, profile) if c["id"] == PHO)
    assert before["ready"] is False

    rig.run()

    after = next(c for c in model_choices(rig.settings, profile) if c["id"] == PHO)
    assert after["ready"] is True


# -- AC-04: cancel ----------------------------------------------------------------------------


def test_cancel_stops_the_download_and_its_helpers_and_removes_the_staging(rig):
    rig.mode = "hang"
    thread = _running_job(rig)
    _wait(lambda: rig.pids.exists())
    helper = int(rig.pids.read_text())
    assert _alive(helper)

    assert rig.store.cancel(PHO) is True
    assert rig.store.job(PHO)["state"] == "cancelling"
    thread.join(10)

    assert not thread.is_alive()
    assert not _alive(helper)
    assert rig.leftovers() == []
    assert not rig.target().exists()
    assert rig.store.job(PHO) == {"state": "idle"}
    assert rig.store.cancel(PHO) is False  # nothing left to cancel


def test_a_cancel_made_before_the_job_starts_means_no_download(rig):
    rig.store.request(PHO)
    rig.store.cancel(PHO)

    rig.installer.tick()

    assert rig.spawned == 0
    assert rig.store.job(PHO) == {"state": "idle"}


def test_the_write_check_cannot_follow_a_link_into_a_model(rig, tmp_path):
    weights = tmp_path / "model.bin"
    weights.write_bytes(b"weights")
    for pid_name in (f".install-probe-{os.getpid()}", ".install-probe"):
        (rig.models / pid_name).symlink_to(weights)
    rig.installer.beat()
    assert weights.read_bytes() == b"weights"
    assert rig.store.installer()["models_writable"] is True
    assert sorted(path.name for path in rig.models.iterdir() if not path.is_symlink()) == []


def test_the_downloaders_bookkeeping_is_not_published(tmp_path, monkeypatch):
    def download(*, repo_id, revision, local_dir):
        (Path(local_dir) / ".cache" / "huggingface").mkdir(parents=True)
        (Path(local_dir) / "weights").write_bytes(b"x")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", download)
    model_installer.download_model(CATALOG[QWEN], tmp_path)
    assert [path.name for path in (tmp_path / "model").iterdir()] == ["weights"]


def test_a_target_that_cannot_resolve_ends_its_job_and_the_service_lives(rig):
    (rig.models / PHO).symlink_to(rig.models / PHO)
    rig.store.request(PHO)
    assert rig.installer.tick() is True
    assert rig.store.job(PHO) == {
        "state": "failed",
        "error": "interrupted",
        "required_bytes": None,
        "free_bytes": None,
    }
    assert rig.store.pending() == []
    assert rig.spawned == 0


def test_a_late_cancel_of_an_ended_job_cannot_undo_a_retrys_cancel(rig):
    rig.store.request(PHO)
    first = rig.store.pending()[0]
    rig.store.finish(first)
    rig.store.request(PHO)
    second = rig.store.pending()[0]
    rig.store.cancel(PHO)
    # The delayed writer from the first job lands now.
    rig.store._write(rig.store._path("cancel", PHO, f".{first['id']}"), {})
    assert rig.store.cancel_requested(second)
    rig.store.finish(second)
    rig.store.sweep_cancels()
    assert list((rig.store.root / "cancel").iterdir()) == []


def test_malformed_records_in_the_volume_cannot_break_readers(rig):
    requests = rig.store.root / "requests"
    requests.mkdir(parents=True)
    (requests / f"{PHO}.json").write_text('{"id": "../../evil", "at": []}')
    (requests / f"{QWEN}.json").write_text(f'{{"id": "{"a" * 32}", "at": "soon"}}')
    assert [request["model"] for request in rig.store.pending()] == [QWEN]
    assert rig.store.cancel(PHO) is False
    (rig.store.root / "installer.json").write_text('{"at": "now"}')
    assert rig.store.installer()["available"] is False
    status = rig.store.root / "status"
    status.mkdir()
    (status / f"{QWEN}.json").write_text(
        f'{{"id": "{"a" * 32}", "state": "running", "phase": {{"toString": 1}}}}'
    )
    assert rig.store.job(QWEN)["phase"] is None


def test_a_cancel_left_by_an_earlier_job_does_not_stop_the_next_one(rig):
    rig.mode = "20"
    first = rig.run()  # a job that failed; its model is still not prepared
    assert first["error"] == "network"
    first_id = rig.store.request(PHO)
    rig.store.finish(rig.store.pending()[0])  # that job ends
    # The cancel click arrived just after that job ended: its marker names the old job.
    rig.store._write(rig.store._path("cancel", PHO), {"id": first_id})
    rig.mode = "ok"

    assert rig.run() == {"state": "idle"}

    prepared_engine_directory("whisper_vi", rig.target())  # the new job ran, not cancelled


# -- AC-04: errors and retry -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("20", "network"),
        ("21", "disk_full"),
        ("22", "integrity"),
        ("empty", "integrity"),
        ("crash", "interrupted"),
        ("oom", "interrupted"),
    ],
)
def test_each_failure_names_its_code_cleans_up_and_can_be_retried(rig, mode, code):
    rig.mode = mode

    job = rig.run()

    assert (job["state"], job["error"]) == ("failed", code)
    assert rig.leftovers() == []
    assert not rig.target().exists()

    rig.mode = "ok"  # the network is back
    assert rig.run() == {"state": "idle"}
    prepared_engine_directory("whisper_vi", rig.target())


def test_too_little_disk_is_refused_before_any_download_with_both_numbers(rig, monkeypatch):
    monkeypatch.setattr(
        model_installer.shutil, "disk_usage", lambda _path: SimpleNamespace(free=1_000)
    )

    job = rig.run()

    assert rig.spawned == 0
    assert job["error"] == "insufficient_disk"
    assert (job["required_bytes"], job["free_bytes"]) == (CATALOG[PHO].required_bytes, 1_000)


def test_a_folder_with_other_files_is_never_overwritten(rig):
    rig.target().mkdir()
    (rig.target() / "operator-file").write_text("kept", encoding="utf-8")

    job = rig.run()

    assert rig.spawned == 0
    assert job["error"] == "target_not_empty"
    assert (rig.target() / "operator-file").read_text(encoding="utf-8") == "kept"


def test_a_target_filled_while_downloading_is_not_overwritten(rig, monkeypatch):
    real_argv = rig.argv

    def argv_then_fill(model, staging):
        rig.target().mkdir()
        (rig.target() / "late-file").write_text("kept", encoding="utf-8")
        return real_argv(model, staging)

    rig.installer.argv = argv_then_fill

    job = rig.run()

    assert job["error"] == "target_not_empty"
    assert (rig.target() / "late-file").read_text(encoding="utf-8") == "kept"
    assert rig.leftovers() == []


def test_an_empty_target_folder_is_replaced_by_the_published_model(rig):
    rig.target().mkdir()

    assert rig.run() == {"state": "idle"}

    prepared_engine_directory("whisper_vi", rig.target())


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_models_folder_the_installer_cannot_write_is_reported(rig):
    rig.models.chmod(0o500)
    try:
        job = rig.run()
        assert job["error"] == "models_not_writable"
        rig.installer.beat()
        assert rig.store.installer()["models_writable"] is False
    finally:
        rig.models.chmod(0o700)


# -- AC-04: restart in the middle ------------------------------------------------------------------


def test_a_restart_fails_the_running_job_and_sweeps_its_staging(rig):
    # What a killed installer leaves: a running status, the request, and the staging directory.
    rig.store.request(PHO)
    request = rig.store.pending()[0]
    rig.store.report(request, state="running", phase="downloading", bytes_done=1, bytes_total=2)
    (rig.models / f".install-{PHO}-{request['id']}").mkdir()
    (rig.models / f".install-{PHO}-{request['id']}" / "part").write_bytes(b"x")
    assert rig.store.job(PHO)["state"] == "running"

    restarted = Installer(rig.settings, rig.store, argv=rig.argv, poll_seconds=0.02)
    restarted.recover()

    assert rig.leftovers() == []
    job = rig.store.job(PHO)
    assert (job["state"], job["error"]) == ("failed", "interrupted")
    assert rig.store.pending() == []  # not stuck: a retry can be requested
    rig.store.request(PHO)
    restarted.tick()
    assert rig.store.job(PHO) == {"state": "idle"}
    prepared_engine_directory("whisper_vi", rig.target())


def test_a_restart_honours_a_cancel_and_keeps_a_job_that_never_started(rig):
    rig.store.request(PHO)
    rig.store.cancel(PHO)
    rig.store.request(QWEN)

    rig.installer.recover()

    assert rig.store.job(PHO) == {"state": "idle"}
    assert rig.store.job(QWEN)["state"] == "queued"


def test_stopping_the_service_ends_the_job_as_interrupted(rig):
    rig.mode = "hang"
    thread = _running_job(rig)
    _wait(lambda: rig.pids.exists())
    helper = int(rig.pids.read_text())

    rig.installer.stopping.set()
    thread.join(10)

    assert rig.store.job(PHO)["error"] == "interrupted"
    assert not _alive(helper)
    assert rig.leftovers() == []


# -- the heartbeat ----------------------------------------------------------------------------


def test_a_stale_or_missing_heartbeat_means_no_installer(rig):
    assert rig.store.installer() == {"available": False, "models_writable": False}
    rig.installer.beat()
    assert rig.store.installer() == {"available": True, "models_writable": True}
    assert rig.store.installer(time.time() + STALE_SECONDS + 1)["available"] is False


def test_the_job_view_ignores_the_status_of_an_earlier_job(rig):
    rig.mode = "21"
    assert rig.run()["error"] == "disk_full"

    rig.store.request(PHO)  # the retry has been asked for, not yet picked up

    assert rig.store.job(PHO)["state"] == "queued"


# -- the API --------------------------------------------------------------------------------------


@pytest.fixture
async def api(rig, monkeypatch):
    settings = rig.settings
    _whisper_dir(settings.asr_gpu_model_path)
    profile = {"value": RuntimeProfile("cuda", "float16", settings.asr_gpu_model_path)}
    monkeypatch.setattr(
        routes,
        "worker_is_healthy",
        lambda _data_dir, *, role=None: profile["value"] is not None,
    )
    monkeypatch.setattr(routes, "read_runtime_profile", lambda _data_dir: profile["value"])
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url=settings.public_url) as client,
    ):
        headers = await _authorize(client, settings)
        yield SimpleNamespace(app=app, client=client, headers=headers, profile=profile, rig=rig)


@pytest.mark.anyio
async def test_the_install_routes_need_a_session_and_the_same_origin(api):
    url = f"/api/asr-models/{PHO}/install"
    origin = {"Origin": api.rig.settings.public_url}
    async with AsyncClient(
        transport=ASGITransport(app=api.app), base_url=api.rig.settings.public_url
    ) as anonymous:
        assert (await anonymous.get("/api/asr-models")).status_code == 401
        assert (await anonymous.post(url, headers=origin)).status_code == 401
        assert (await anonymous.delete(url, headers=origin)).status_code == 401
    cross = {"Origin": "http://evil.example"}
    assert (await api.client.post(url, headers=cross)).status_code == 403
    assert (await api.client.delete(url, headers=cross)).status_code == 403
    assert (await api.client.post(url)).status_code == 403  # no Origin at all
    assert api.rig.store.pending() == []


@pytest.mark.anyio
async def test_the_catalog_reports_sizes_state_and_whether_an_installer_is_there(api):
    body = (await api.client.get("/api/asr-models", headers=api.headers)).json()
    assert body["installer"] == {"available": False, "models_writable": False}
    rows = {row["id"]: row for row in body["asr_models"]}
    assert "install" not in rows["auto"]
    pho = rows[PHO]
    assert pho["install"] == {"state": "idle"}
    assert pho["required_bytes"] == CATALOG[PHO].required_bytes
    assert pho["free_bytes"] == shutil.disk_usage(api.rig.models).free
    assert pho["prepare_command"]  # still there when no installer runs


@pytest.mark.anyio
async def test_install_then_poll_then_cancel_through_the_api(api):
    api.rig.installer.beat()
    url = f"/api/asr-models/{PHO}/install"

    first = await api.client.post(url, headers=api.headers)
    second = await api.client.post(url, headers=api.headers)  # a double click
    assert (first.status_code, second.status_code) == (202, 202)
    assert first.json()["install"]["state"] == second.json()["install"]["state"] == "queued"
    assert len(api.rig.store.pending()) == 1

    cancelled = await api.client.delete(url, headers=api.headers)
    assert (cancelled.status_code, cancelled.json()["install"]["state"]) == (202, "cancelling")
    api.rig.installer.tick()
    listing = (await api.client.get("/api/asr-models", headers=api.headers)).json()
    assert next(r for r in listing["asr_models"] if r["id"] == PHO)["install"] == {"state": "idle"}

    nothing = await api.client.delete(url, headers=api.headers)
    assert (nothing.status_code, nothing.json()["detail"]["code"]) == (409, "install_not_active")


@pytest.mark.anyio
async def test_the_model_is_ready_in_the_catalog_after_the_installer_finishes(api):
    api.rig.installer.beat()
    await api.client.post(f"/api/asr-models/{PHO}/install", headers=api.headers)

    api.rig.installer.tick()

    rows = (await api.client.get("/api/asr-models", headers=api.headers)).json()["asr_models"]
    pho = next(row for row in rows if row["id"] == PHO)
    assert (pho["ready"], pho["install"]["state"]) == (True, "idle")


@pytest.mark.anyio
async def test_a_failed_install_is_in_the_catalog_with_its_code(api):
    api.rig.installer.beat()
    api.rig.mode = "20"
    await api.client.post(f"/api/asr-models/{PHO}/install", headers=api.headers)
    api.rig.installer.tick()

    rows = (await api.client.get("/api/asr-models", headers=api.headers)).json()["asr_models"]
    assert next(r for r in rows if r["id"] == PHO)["install"]["error"] == "network"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("model", "setup", "status", "code"),
    [
        ("auto", "ok", 404, "unknown_model"),
        ("whisper-large-v2", "ok", 404, "unknown_model"),
        ("whisper-large-v3", "ok", 409, "model_ready"),  # prepared: never touched
        (QWEN, "cpu", 409, "gpu_required"),  # refused before any download
        (PHO, "no_worker", 409, "worker_unavailable"),
        (PHO, "no_installer", 503, "installer_unavailable"),
        (PHO, "read_only", 409, "models_not_writable"),
    ],
)
async def test_an_install_the_machine_cannot_do_is_refused_and_nothing_is_queued(
    api, model, setup, status, code
):
    if setup != "no_installer":
        api.rig.installer.beat()
    if setup == "cpu":
        api.profile["value"] = RuntimeProfile("cpu", "int8", api.rig.settings.asr_gpu_model_path)
    if setup == "no_worker":
        api.profile["value"] = None
    if setup == "read_only":
        api.rig.store.beat(models_writable=False)

    response = await api.client.post(f"/api/asr-models/{model}/install", headers=api.headers)

    assert (response.status_code, response.json()["detail"]["code"]) == (status, code)
    assert api.rig.store.pending() == []
    assert api.rig.spawned == 0
    if model in CATALOG:
        cancel = await api.client.delete(f"/api/asr-models/{model}/install", headers=api.headers)
        assert cancel.status_code == 409
    else:
        assert (
            await api.client.delete(f"/api/asr-models/{model}/install", headers=api.headers)
        ).status_code == 404


@pytest.mark.anyio
async def test_the_default_model_api_only_takes_a_model_that_can_run_here(api):
    rig = api.rig

    async def put(value):
        return await api.client.put(
            "/api/asr-models/default", json={"asr_model": value}, headers=api.headers
        )

    async def read():
        return (await api.client.get("/api/asr-models", headers=api.headers)).json()

    assert (await read())["default_model"] is None
    assert (await put("nope")).status_code == 400
    assert (await put(1)).status_code == 400
    refused = await put(PHO)
    assert (refused.status_code, refused.json()["detail"]["code"]) == (409, "not_prepared")
    assert (await read())["default_model"] is None

    _whisper_dir(rig.settings.asr_phowhisper_model_path)
    assert (await put(PHO)).json() == {"default_model": PHO}
    body = await read()
    auto = next(item for item in body["asr_models"] if item["id"] == "auto")
    assert (body["default_model"], auto["model_name"], auto["chosen"]) == (PHO, PHO, True)
    assert body["hardware_model"] == "whisper-large-v3"

    assert (await put(None)).json() == {"default_model": None}
    assert (await read())["default_model"] is None

    api.profile["value"] = RuntimeProfile("cpu", "int8", rig.settings.asr_gpu_model_path)
    assert (await put(QWEN)).json()["detail"]["code"] == "gpu_required"

    api.profile["value"] = None
    refused = await put(PHO)
    assert (refused.status_code, refused.json()["detail"]["code"]) == (503, "worker_unavailable")


@pytest.mark.anyio
async def test_the_default_model_route_needs_the_same_origin(api):
    url = "/api/asr-models/default"
    assert (await api.client.put(url, json={"asr_model": None})).status_code == 403
