from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from seeding import prepare_diarization_model, seed_meeting

from asr_studio import worker
from asr_studio.config import Settings
from asr_studio.diarization_runner import DIARIZATION_ERROR_MESSAGES
from asr_studio.runtime import RuntimeProfile
from asr_studio.storage import database_path, initialize
from asr_studio.tasks import (
    TASK_ERROR_MESSAGES,
    TaskError,
    accept_transcription_task,
    cancel_task,
    claim_next_task,
    transcription_state,
)
from asr_studio.worker import (
    acquire_worker_lock,
    is_healthy,
    terminate_and_reap_process_group,
    write_heartbeat,
)


def _worker_task_store(tmp_path: Path, *, meeting_count: int = 1):
    data_dir = tmp_path / "data"
    initialize(data_dir)
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "model.bin").write_bytes(b"model")
    (model_dir / "config.json").write_text(
        json.dumps({"lang_ids": [], "suppress_ids": [], "suppress_ids_begin": []}),
        encoding="utf-8",
    )
    (model_dir / "tokenizer.json").write_text(
        json.dumps({"model": {"type": "BPE"}}), encoding="utf-8"
    )
    (model_dir / "preprocessor_config.json").write_text(
        json.dumps(
            {
                "feature_size": 128,
                "sampling_rate": 16000,
                "hop_length": 160,
                "n_fft": 400,
                "chunk_length": 30,
            }
        ),
        encoding="utf-8",
    )
    diarization_model_dir = prepare_diarization_model(tmp_path / "diarization-model")
    meeting_ids = [
        seed_meeting(data_dir, f"Worker meeting {index}") for index in range(meeting_count)
    ]
    with sqlite3.connect(database_path(data_dir)) as connection:
        for meeting_id in meeting_ids:
            storage_key = connection.execute(
                "SELECT storage_key FROM recordings WHERE meeting_id = ?", (meeting_id,)
            ).fetchone()[0]
            audio_path = data_dir / storage_key
            audio_path.parent.mkdir(parents=True, exist_ok=True)
            audio_path.write_bytes(b"audio")
    settings = Settings(
        data_dir=data_dir,
        auth_dir=tmp_path / "auth",
        asr_model_path=model_dir,
        diarization_model_path=diarization_model_dir,
    )
    lock_fd = acquire_worker_lock(data_dir)
    assert lock_fd is not None
    return data_dir, model_dir, meeting_ids, settings, lock_fd


def _accept_and_claim(data_dir: Path, model_dir: Path, meeting_id: str):
    accepted = accept_transcription_task(
        data_dir,
        meeting_id=meeting_id,
        request_id="worker-task-request-001",
        model_path=model_dir,
        diarization_model_path=data_dir.parent / "diarization-model",
        device="cpu",
        compute_type="int8",
        worker_ready=True,
    )
    claimed = claim_next_task(data_dir, owner_id="worker-test-owner")
    assert claimed and claimed["id"] == accepted["id"]
    return claimed


def _install_runner_shim(
    tmp_path: Path, monkeypatch, source: str, diarization_source: str | None = None
) -> None:
    shim_root = tmp_path / "runner-shim"
    package = shim_root / "asr_studio"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "runner.py").write_text(source, encoding="utf-8")
    if diarization_source is None:
        diarization_source = (
            "import json\n"
            "print(json.dumps({'type': 'complete', 'regular_turn_count': 0, "
            "'exclusive_turn_count': 0}), flush=True)\n"
        )
    (package / "diarization_runner.py").write_text(diarization_source, encoding="utf-8")
    source_root = str(Path(__file__).parents[1] / "src")
    python_path = os.pathsep.join(
        path for path in (str(shim_root), source_root, os.environ.get("PYTHONPATH", "")) if path
    )
    monkeypatch.setenv("PYTHONPATH", python_path)


def test_lifetime_lock_stays_owned_by_runner_child_after_worker_descriptor_closes(tmp_path):
    data_dir = tmp_path / "data"
    lock_fd = acquire_worker_lock(data_dir)
    assert lock_fd is not None
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        pass_fds=(lock_fd,),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.close(lock_fd)
    write_heartbeat(data_dir, "running")
    try:
        assert is_healthy(data_dir)
        assert acquire_worker_lock(data_dir) is None
        assert terminate_and_reap_process_group(
            process, term_grace_seconds=0.5, kill_grace_seconds=0.5
        )
        recovered_lock = acquire_worker_lock(data_dir)
        assert recovered_lock is not None
        os.close(recovered_lock)
    finally:
        if process.poll() is None:
            terminate_and_reap_process_group(
                process, term_grace_seconds=0.2, kill_grace_seconds=0.5
            )


def test_cancellation_terminates_and_reaps_the_complete_runner_process_group(tmp_path):
    script = (
        "import signal, subprocess, sys, time\n"
        "child = None\n"
        "def stop(_signum, _frame):\n"
        "    child.wait(timeout=3)\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert process.stdout is not None
    child_pid = int(process.stdout.readline().strip())
    assert terminate_and_reap_process_group(process, term_grace_seconds=1.0, kill_grace_seconds=1.0)
    process.stdout.close()
    assert process.poll() is not None
    try:
        os.kill(child_pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError("The runner child remained alive after group cancellation.")


def test_worker_publishes_complete_runner_protocol_as_transcript_only(tmp_path, monkeypatch):
    data_dir, model_dir, meeting_ids, settings, lock_fd = _worker_task_store(tmp_path)
    task = _accept_and_claim(data_dir, model_dir, meeting_ids[0])
    _install_runner_shim(
        tmp_path,
        monkeypatch,
        """import json
events = [
    {"type": "stage", "stage": "loading_model"},
    {"type": "stage", "stage": "transcribing"},
    {"type": "segment", "id": "a" * 32, "start_seconds": 0.5, "end_seconds": 1.5,
     "text": "Local transcript.", "avg_logprob": -0.2, "no_speech_prob": 0.1,
     "compression_ratio": 1.0},
    {"type": "complete", "segment_count": 1, "language_probability": 0.91},
]
for event in events:
    print(json.dumps(event), flush=True)
""",
    )
    try:
        worker._run_task(settings, lock_fd, task, threading.Event())
    finally:
        os.close(lock_fd)

    state = transcription_state(data_dir, meeting_ids[0])
    assert state["task"]["status"] == "succeeded"
    assert state["task"]["result_scope"] == "transcript_only"
    assert state["generation"]["segments"][0]["text"] == "Local transcript."


def test_worker_runs_diarization_after_asr_in_a_separate_child_and_saves_speakers(
    tmp_path, monkeypatch
):
    data_dir, model_dir, meeting_ids, settings, lock_fd = _worker_task_store(tmp_path)
    task = _accept_and_claim(data_dir, model_dir, meeting_ids[0])
    phase_log = tmp_path / "phases.log"
    monkeypatch.setenv("ASR_TEST_PHASE_LOG", str(phase_log))
    monkeypatch.setenv("HF_TOKEN", "setup-only-token")
    monkeypatch.setenv("ORT_DISABLE_TELEMETRY", "0")
    _install_runner_shim(
        tmp_path,
        monkeypatch,
        """import json
import os
from pathlib import Path
Path(os.environ["ASR_TEST_PHASE_LOG"]).write_text(
    f"asr-start:{os.getpid()}:ort-{os.environ.get('ORT_DISABLE_TELEMETRY')}\\n", encoding="ascii"
)
events = [
    {"type": "segment", "id": "a" * 32, "start_seconds": 0.5, "end_seconds": 1.5,
     "text": "API gateway đang ready.", "avg_logprob": -0.2, "no_speech_prob": 0.03,
     "compression_ratio": 0.8},
    {"type": "complete", "segment_count": 1, "language_probability": 0.9},
]
for event in events:
    print(json.dumps(event), flush=True)
with Path(os.environ["ASR_TEST_PHASE_LOG"]).open("a", encoding="ascii") as log:
    log.write(f"asr-end:{os.getpid()}\\n")
""",
        """import json
import os
from pathlib import Path
log_path = Path(os.environ["ASR_TEST_PHASE_LOG"])
ort = os.environ.get("ORT_DISABLE_TELEMETRY")
with log_path.open("a", encoding="ascii") as log:
    log.write(f"diarization-start:{os.getpid()}:token-{int('HF_TOKEN' in os.environ)}:ort-{ort}\\n")
turns = [
    {"type": "turn", "kind": kind, "start_seconds": 0.4, "end_seconds": 1.6,
     "speaker_id": "SPEAKER_00"}
    for kind in ("regular", "exclusive")
]
for event in turns:
    print(json.dumps(event), flush=True)
print(json.dumps({"type": "complete", "regular_turn_count": 1,
                  "exclusive_turn_count": 1}), flush=True)
with log_path.open("a", encoding="ascii") as log:
    log.write(f"diarization-end:{os.getpid()}\\n")
""",
    )
    try:
        worker._run_task(settings, lock_fd, task, threading.Event())
    finally:
        os.close(lock_fd)

    lines = phase_log.read_text(encoding="ascii").splitlines()
    assert [line.split(":", maxsplit=1)[0] for line in lines] == [
        "asr-start",
        "asr-end",
        "diarization-start",
        "diarization-end",
    ]
    assert lines[0].split(":")[1] == lines[1].split(":")[1]
    assert lines[2].split(":")[1] == lines[3].split(":")[1]
    assert lines[0].split(":")[1] != lines[2].split(":")[1]
    assert lines[0].endswith(":ort-1")
    assert lines[2].endswith(":token-0:ort-1")
    generation = transcription_state(data_dir, meeting_ids[0])["generation"]
    segment = generation["segments"][0]
    assert segment["text"] == "API gateway đang ready."
    assert (segment["start_seconds"], segment["end_seconds"]) == (0.5, 1.5)
    assert segment["original_speaker_id"] == "SPEAKER_00"
    assert segment["speaker_id"] == "SPEAKER_00"
    assert generation["speakers"] == [
        {
            "speaker_id": "SPEAKER_00",
            "display_name": "Speaker 1",
            "first_seen_seconds": 0.4,
            "last_seen_seconds": 1.6,
        }
    ]


def test_diarization_failure_keeps_the_previous_complete_generation(tmp_path, monkeypatch):
    data_dir, model_dir, meeting_ids, settings, lock_fd = _worker_task_store(tmp_path)
    task = _accept_and_claim(data_dir, model_dir, meeting_ids[0])
    monkeypatch.setenv("ASR_TEST_DIA_FAIL", "0")
    _install_runner_shim(
        tmp_path,
        monkeypatch,
        """import json
print(json.dumps({"type": "complete", "segment_count": 0,
                  "language_probability": 0.9}), flush=True)
""",
        """import json
import os
if os.environ.get("ASR_TEST_DIA_FAIL") == "1":
    print(json.dumps({"type": "error", "code": "diarization_failed",
                      "message": "Local diarization failed."}), flush=True)
else:
    print(json.dumps({"type": "complete", "regular_turn_count": 0,
                      "exclusive_turn_count": 0}), flush=True)
""",
    )
    worker._run_task(settings, lock_fd, task, threading.Event())
    previous = transcription_state(data_dir, meeting_ids[0])["generation"]
    assert previous is not None

    accepted = accept_transcription_task(
        data_dir,
        meeting_id=meeting_ids[0],
        request_id="worker-task-request-002",
        model_path=model_dir,
        diarization_model_path=settings.diarization_model_path,
        device="cpu",
        compute_type="int8",
        worker_ready=True,
    )
    rerun = claim_next_task(data_dir, owner_id="worker-test-owner-2")
    assert rerun and rerun["id"] == accepted["id"]
    monkeypatch.setenv("ASR_TEST_DIA_FAIL", "1")
    worker._run_task(settings, lock_fd, rerun, threading.Event())
    os.close(lock_fd)

    state = transcription_state(data_dir, meeting_ids[0])
    assert state["task"]["status"] == "failed"
    assert state["task"]["error_code"] == "diarization_failed"
    assert state["generation"]["id"] == previous["id"]
    with sqlite3.connect(database_path(data_dir)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM transcript_generations").fetchone() == (1,)


@pytest.mark.parametrize(
    ("child_code", "child_message", "expected_code"),
    [
        (
            "diarization_input_decode_failed",
            DIARIZATION_ERROR_MESSAGES["diarization_input_decode_failed"],
            "diarization_input_decode_failed",
        ),
        (
            "diarization_decoder_unavailable",
            "native error at /private/path/token=secret",
            "diarization_failed",
        ),
        ("vendor_exception", "native error at /private/path/token=secret", "diarization_failed"),
    ],
)
def test_worker_validates_diarization_child_errors_before_persistence(
    tmp_path, monkeypatch, capsys, child_code, child_message, expected_code
):
    data_dir, model_dir, meeting_ids, settings, lock_fd = _worker_task_store(tmp_path)
    task = _accept_and_claim(data_dir, model_dir, meeting_ids[0])
    event = json.dumps({"type": "error", "code": child_code, "message": child_message})
    diarization_source = f"import json\nprint({json.dumps(event)}, flush=True)\n"
    _install_runner_shim(
        tmp_path,
        monkeypatch,
        "import json\nprint(json.dumps({'type': 'complete', 'segment_count': 0, "
        "'language_probability': 0.9}), flush=True)\n",
        diarization_source,
    )
    try:
        worker._run_task(settings, lock_fd, task, threading.Event())
    finally:
        os.close(lock_fd)

    stored = transcription_state(data_dir, meeting_ids[0])["task"]
    assert stored["status"] == "failed"
    assert stored["error_code"] == expected_code
    assert stored["error_message"] == DIARIZATION_ERROR_MESSAGES[expected_code]
    assert "private/path" not in stored["error_message"]
    contexts = [
        json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")
    ]
    assert contexts[-1]["event"] == "diarization_error"
    assert contexts[-1]["code"] == expected_code


@pytest.mark.parametrize("failure_kind", ["task", "sqlite", "os", "unexpected"])
def test_worker_classifies_publication_failures(tmp_path, monkeypatch, capsys, failure_kind):
    data_dir, model_dir, meeting_ids, settings, lock_fd = _worker_task_store(tmp_path)
    task = _accept_and_claim(data_dir, model_dir, meeting_ids[0])
    _install_runner_shim(
        tmp_path,
        monkeypatch,
        "import json\nprint(json.dumps({'type': 'complete', 'segment_count': 0, "
        "'language_probability': 0.9}), flush=True)\n",
        "import json\nprint(json.dumps({'type': 'complete', 'regular_turn_count': 0, "
        "'exclusive_turn_count': 0}), flush=True)\n",
    )

    def fail_publication(*_args, **_kwargs):
        if failure_kind == "task":
            raise TaskError(
                500, "invalid_runner_result", "The completed result could not be saved."
            )
        if failure_kind == "sqlite":
            raise sqlite3.OperationalError("database is locked")
        if failure_kind == "os":
            raise OSError("disk full")
        raise RuntimeError("unexpected persistence detail")

    monkeypatch.setattr(worker, "publish_complete_generation", fail_publication)
    try:
        worker._run_task(settings, lock_fd, task, threading.Event())
    finally:
        os.close(lock_fd)

    stored = transcription_state(data_dir, meeting_ids[0])["task"]
    assert stored["status"] == "failed"
    assert stored["stage"] == "failed"
    assert stored["error_code"] == "publication_failed"
    assert stored["error_message"] == TASK_ERROR_MESSAGES["publication_failed"]
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    contexts = [json.loads(line) for line in captured.err.splitlines() if line.startswith("{")]
    assert contexts[-1] == {
        "event": "publishing_error",
        "stage": "publishing",
        "code": stored["error_code"],
    }


def test_cancellation_during_diarization_reaps_the_child_process_group(tmp_path, monkeypatch):
    data_dir, model_dir, meeting_ids, settings, lock_fd = _worker_task_store(tmp_path)
    task = _accept_and_claim(data_dir, model_dir, meeting_ids[0])
    marker_path = tmp_path / "diarization-child.pid"
    monkeypatch.setenv("ASR_TEST_DIA_CHILD_PID", str(marker_path))
    monkeypatch.setattr(worker, "RUNNER_TERM_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(worker, "RUNNER_KILL_GRACE_SECONDS", 1.0)
    _install_runner_shim(
        tmp_path,
        monkeypatch,
        """import json
print(json.dumps({"type": "complete", "segment_count": 0,
                  "language_probability": 0.9}), flush=True)
""",
        """import json
import os
import subprocess
import sys
import time
from pathlib import Path
child_source = (
    "import os, signal, time; from pathlib import Path; "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "Path(os.environ['ASR_TEST_DIA_CHILD_PID']).write_text(str(os.getpid())); time.sleep(60)"
)
child = subprocess.Popen([sys.executable, "-c", child_source])
print(json.dumps({"type": "stage", "stage": "diarizing"}), flush=True)
time.sleep(60)
""",
    )
    runner_thread = threading.Thread(
        target=worker._run_task,
        args=(settings, lock_fd, task, threading.Event()),
    )
    runner_thread.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not marker_path.exists():
            time.sleep(0.02)
        assert marker_path.exists(), "The diarization process did not start after ASR completed."
        child_pid = int(marker_path.read_text(encoding="ascii"))
        assert cancel_task(data_dir, task["id"])["status"] == "stopping"
        runner_thread.join(timeout=8)
        assert not runner_thread.is_alive()
        assert transcription_state(data_dir, meeting_ids[0])["task"]["status"] == "canceled"
        assert transcription_state(data_dir, meeting_ids[0])["generation"] is None
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        runner_thread.join(timeout=8)
        os.close(lock_fd)


def test_worker_holds_stopping_slot_until_runner_group_is_reaped(tmp_path, monkeypatch):
    data_dir, model_dir, meeting_ids, settings, lock_fd = _worker_task_store(
        tmp_path, meeting_count=2
    )
    task = _accept_and_claim(data_dir, model_dir, meeting_ids[0])
    marker_path = tmp_path / "runner-started"
    child_ready_path = tmp_path / "runner-child-ready"
    monkeypatch.setenv("ASR_TEST_RUNNER_MARKER", str(marker_path))
    monkeypatch.setenv("ASR_TEST_RUNNER_CHILD_READY", str(child_ready_path))
    _install_runner_shim(
        tmp_path,
        monkeypatch,
        """import json
import os
import subprocess
import sys
import time
from pathlib import Path
child_source = (
    "import os, signal, time; from pathlib import Path; "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "Path(os.environ['ASR_TEST_RUNNER_CHILD_READY']).touch(); time.sleep(60)"
)
child = subprocess.Popen([sys.executable, "-c", child_source])
while not Path(os.environ["ASR_TEST_RUNNER_CHILD_READY"]).exists():
    time.sleep(0.01)
Path(os.environ["ASR_TEST_RUNNER_MARKER"]).write_text(str(child.pid))
print(json.dumps({"type": "stage", "stage": "transcribing"}), flush=True)
time.sleep(60)
""",
    )
    reaper_entered = threading.Event()
    release_reaper = threading.Event()
    original_reaper = worker.terminate_and_reap_process_group
    observed_process = []

    def pause_before_reaping(process, **_kwargs):
        observed_process.append(process)
        reaper_entered.set()
        assert release_reaper.wait(timeout=5)
        return original_reaper(
            process,
            term_grace_seconds=0.2,
            kill_grace_seconds=1.0,
        )

    monkeypatch.setattr(worker, "terminate_and_reap_process_group", pause_before_reaping)
    runner_thread = threading.Thread(
        target=worker._run_task,
        args=(settings, lock_fd, task, threading.Event()),
    )
    runner_thread.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not marker_path.exists():
            time.sleep(0.02)
        assert marker_path.exists(), "The local runner did not start its process group."
        child_pid = int(marker_path.read_text(encoding="ascii"))

        assert cancel_task(data_dir, task["id"])["status"] == "stopping"
        assert reaper_entered.wait(timeout=5), "The worker did not begin process-group shutdown."
        with pytest.raises(TaskError) as error:
            accept_transcription_task(
                data_dir,
                meeting_id=meeting_ids[1],
                request_id="worker-task-request-002",
                model_path=model_dir,
                diarization_model_path=settings.diarization_model_path,
                device="cpu",
                compute_type="int8",
                worker_ready=True,
            )
        assert error.value.code == "system_busy"

        release_reaper.set()
        runner_thread.join(timeout=8)
        assert not runner_thread.is_alive()
        assert transcription_state(data_dir, meeting_ids[0])["task"]["status"] == "canceled"
        with sqlite3.connect(database_path(data_dir)) as connection:
            assert connection.execute(
                "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
            ).fetchone() == (None,)
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        release_reaper.set()
        runner_thread.join(timeout=8)
        if runner_thread.is_alive() and observed_process:
            original_reaper(
                observed_process[0],
                term_grace_seconds=0.2,
                kill_grace_seconds=1.0,
            )
        os.close(lock_fd)


def test_runner_eof_with_live_child_is_bounded_and_reaped(tmp_path, monkeypatch):
    data_dir, model_dir, meeting_ids, settings, lock_fd = _worker_task_store(tmp_path)
    task = _accept_and_claim(data_dir, model_dir, meeting_ids[0])
    pid_path = tmp_path / "eof-runner.pid"
    monkeypatch.setenv("ASR_TEST_EOF_RUNNER_PID", str(pid_path))
    monkeypatch.setattr(worker, "RUNNER_POST_EOF_GRACE_SECONDS", 0.2)
    select_calls = 0
    original_select = worker.select.select

    def count_select(*args, **kwargs):
        nonlocal select_calls
        select_calls += 1
        return original_select(*args, **kwargs)

    monkeypatch.setattr(worker.select, "select", count_select)
    _install_runner_shim(
        tmp_path,
        monkeypatch,
        """import json
import os
import time
from pathlib import Path
Path(os.environ["ASR_TEST_EOF_RUNNER_PID"]).write_text(str(os.getpid()), encoding="ascii")
print(json.dumps({"type": "complete", "segment_count": 0, "language_probability": 0.9}), flush=True)
os.close(1)
time.sleep(60)
""",
    )
    try:
        worker._run_task(settings, lock_fd, task, threading.Event())
    finally:
        os.close(lock_fd)

    assert select_calls <= 3
    assert transcription_state(data_dir, meeting_ids[0])["task"]["status"] == "failed"
    assert transcription_state(data_dir, meeting_ids[0])["generation"] is None
    assert transcription_state(data_dir, meeting_ids[0])["task"]["error_code"] == (
        "runner_protocol_error"
    )
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_path.read_text(encoding="ascii")), 0)


def test_worker_suppresses_runner_stderr_without_leaking_child_output(tmp_path, monkeypatch, capfd):
    data_dir, model_dir, meeting_ids, settings, lock_fd = _worker_task_store(tmp_path)
    task = _accept_and_claim(data_dir, model_dir, meeting_ids[0])
    _install_runner_shim(
        tmp_path,
        monkeypatch,
        """import json
import sys
sys.stderr.write("runner diagnostic sentinel token=secret /private/recording\\n" * 4096)
sys.stderr.flush()
event = {"type": "error", "code": "transcription_failed", "message": "failed"}
print(json.dumps(event), flush=True)
""",
    )
    try:
        worker._run_task(settings, lock_fd, task, threading.Event())
    finally:
        os.close(lock_fd)

    captured = capfd.readouterr().err
    assert "runner diagnostic sentinel" not in captured
    assert "token=secret" not in captured
    assert "/private/recording" not in captured
    assert task["id"] not in captured
    assert meeting_ids[0] not in captured
    contexts = [json.loads(line) for line in captured.splitlines() if line.startswith("{")]
    assert contexts[0] == {
        "event": "child_stderr",
        "stage": "child_process",
        "code": "child_stderr_suppressed",
    }
    assert contexts[-1]["event"] == "transcription_error"
    assert contexts[-1]["code"] == "transcription_failed"
    assert transcription_state(data_dir, meeting_ids[0])["task"]["status"] == "failed"


def test_worker_runner_command_never_forces_the_language_hint(tmp_path):
    command = worker._runner_command(
        {
            "model_path": tmp_path / "model",
            "language_hint": "en",
            "device": "cuda",
            "compute_type": "float16",
        },
        tmp_path / "recording.m4a",
    )

    assert "--language" not in command
    assert command == [
        sys.executable,
        "-m",
        "asr_studio.runner",
        "--model",
        str(tmp_path / "model"),
        "--audio",
        str(tmp_path / "recording.m4a"),
        "--device",
        "cuda",
        "--compute-type",
        "float16",
        "--engine",
        "whisper",
    ]


@pytest.mark.parametrize(
    ("asr_model", "engine"),
    [
        ("auto", "whisper"),
        ("whisper-large-v3", "whisper"),
        ("pho-whisper-large", "whisper_vi"),
        ("qwen3-asr-1.7b", "qwen"),
    ],
)
def test_worker_runs_the_engine_of_the_model_chosen_for_the_task(tmp_path, asr_model, engine):
    command = worker._runner_command(
        {
            "model_path": tmp_path / "model",
            "device": "cuda",
            "compute_type": "float16",
            "asr_model": asr_model,
        },
        tmp_path / "recording.m4a",
    )
    assert command[-2:] == ["--engine", engine]


def test_diarization_command_passes_task_and_meeting_diagnostic_ids(tmp_path):
    task_id = "a" * 32
    meeting_id = "b" * 32
    command = worker._diarization_command(
        {
            "id": task_id,
            "meeting_id": meeting_id,
            "diarization_model_path": tmp_path / "community-1",
            "device": "cuda",
        },
        tmp_path / "recording.m4a",
    )

    assert command[-4:] == [
        "--diagnostic-id",
        task_id,
        "--meeting-id",
        meeting_id,
    ]
    assert "--verbose" not in command


def test_torch_cuda_preflight_uses_an_isolated_allocation_probe(monkeypatch):
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    monkeypatch.setenv("ORT_DISABLE_TELEMETRY", "0")

    worker._verify_torch_cuda()

    assert observed["command"][:2] == [sys.executable, "-c"]
    assert "torch.cuda.is_available()" in observed["command"][2]
    assert "device='cuda'" in observed["command"][2]
    assert observed["kwargs"]["timeout"] == 30
    assert observed["kwargs"]["check"] is False
    assert observed["kwargs"]["env"]["ORT_DISABLE_TELEMETRY"] == "1"


def test_torch_cuda_preflight_rejects_an_unusable_torch_runtime(monkeypatch):
    monkeypatch.setattr(
        worker.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1),
    )

    with pytest.raises(worker.RuntimeConfigurationError, match="speaker diarization"):
        worker._verify_torch_cuda()


def test_worker_health_probe_shares_locks_without_claiming_worker_ownership(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    worker.write_heartbeat(data_dir, "idle")
    probe_fd = os.open(worker.worker_lock_path(data_dir), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        worker.fcntl.flock(probe_fd, worker.fcntl.LOCK_SH | worker.fcntl.LOCK_NB)
        assert not is_healthy(data_dir)
    finally:
        worker.fcntl.flock(probe_fd, worker.fcntl.LOCK_UN)
        os.close(probe_fd)


def test_health_check_rejects_stale_heartbeat_while_worker_lock_is_held(tmp_path):
    data_dir = tmp_path / "data"
    lock_fd = acquire_worker_lock(data_dir)
    assert lock_fd is not None
    write_heartbeat(data_dir, "idle")
    try:
        assert not is_healthy(data_dir, now=time.time() + worker.HEARTBEAT_MAX_AGE_SECONDS + 1)
    finally:
        os.close(lock_fd)


def test_worker_publishes_starting_before_recovery_and_idle_after(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    settings = Settings(data_dir=data_dir, auth_dir=tmp_path / "auth")
    worker.write_heartbeat(data_dir, "idle")
    recovery_states = []
    loop_states = []

    def inspect_recovery(_data_dir):
        recovery_states.append(
            worker.heartbeat_path(data_dir).read_text(encoding="ascii").split()[0]
        )
        assert recovery_states[-1] == "starting"
        assert not is_healthy(data_dir)
        return []

    def inspect_loop(_settings, _lock_fd, _stop_event):
        loop_states.append(worker.heartbeat_path(data_dir).read_text(encoding="ascii").split()[0])
        assert loop_states[-1] == "idle"
        assert is_healthy(data_dir)

    monkeypatch.setattr(worker, "recover_abandoned_tasks", inspect_recovery)
    monkeypatch.setattr(worker, "_worker_loop", inspect_loop)
    monkeypatch.setattr(
        worker,
        "select_runtime",
        lambda _settings: RuntimeProfile(
            device="cpu", compute_type="int8", model_path=tmp_path / "model"
        ),
    )
    diarization_model_dir = prepare_diarization_model(tmp_path / "diarization-model")
    settings = Settings(
        data_dir=data_dir,
        auth_dir=tmp_path / "auth",
        diarization_model_path=diarization_model_dir,
    )
    worker.run_worker(settings)

    assert recovery_states == ["starting"]
    assert loop_states == ["idle"]
    assert worker.heartbeat_path(data_dir).read_text(encoding="ascii").startswith("stopped ")
    assert not is_healthy(data_dir)


def test_worker_subprocess_recovers_old_task_and_shuts_down_cleanly(tmp_path):
    data_dir, model_dir, meeting_ids, settings, lock_fd = _worker_task_store(tmp_path)
    _accept_and_claim(data_dir, model_dir, meeting_ids[0])
    os.close(lock_fd)
    environment = os.environ.copy()
    environment.update(
        {
            "ASR_DATA_DIR": str(data_dir),
            "ASR_AUTH_DIR": str(settings.auth_dir),
            "ASR_MODEL_PATH": str(model_dir),
            "ASR_DEVICE": "cpu",
            "ASR_COMPUTE_TYPE": "int8",
            "ASR_DIARIZATION_MODEL_PATH": str(settings.diarization_model_path),
        }
    )
    source_root = str(Path(__file__).parents[1] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (source_root, environment.get("PYTHONPATH", "")) if item
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "asr_studio.worker"],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError("The worker subprocess exited before recovery completed.")
            recovered = transcription_state(data_dir, meeting_ids[0])["task"]
            if recovered and recovered["status"] == "interrupted" and is_healthy(data_dir):
                break
            time.sleep(0.02)
        else:
            raise AssertionError("The worker subprocess did not recover the abandoned task.")

        assert recovered["error_code"] == "worker_restarted"
        with sqlite3.connect(database_path(data_dir)) as connection:
            assert connection.execute(
                "SELECT active_task_id FROM processing_slot WHERE slot_id = 1"
            ).fetchone() == (None,)

        process.terminate()
        assert process.wait(timeout=5) == 0
        assert worker.heartbeat_path(data_dir).read_text(encoding="ascii").startswith("stopped ")
        assert not is_healthy(data_dir)
        recovered_lock = acquire_worker_lock(data_dir)
        assert recovered_lock is not None
        os.close(recovered_lock)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_exclusive_lock_waits_out_a_brief_shared_health_probe(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    probe_fd = os.open(worker.worker_lock_path(data_dir), os.O_CREAT | os.O_RDWR, 0o600)
    worker.fcntl.flock(probe_fd, worker.fcntl.LOCK_SH | worker.fcntl.LOCK_NB)
    sleeps: list[float] = []

    def release_probe(seconds):
        sleeps.append(seconds)
        worker.fcntl.flock(probe_fd, worker.fcntl.LOCK_UN)

    monkeypatch.setattr(worker.time, "sleep", release_probe)
    lock_fd = acquire_worker_lock(data_dir)
    os.close(probe_fd)
    assert lock_fd is not None
    try:
        assert sleeps == [worker.WORKER_LOCK_RETRY_INTERVAL_SECONDS]
    finally:
        os.close(lock_fd)


def test_exclusive_lock_retry_never_admits_a_second_worker(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    first_fd = acquire_worker_lock(data_dir)
    assert first_fd is not None
    sleeps: list[float] = []
    monkeypatch.setattr(worker.time, "sleep", sleeps.append)
    try:
        assert acquire_worker_lock(data_dir, retry_seconds=0.01) is None
        assert acquire_worker_lock(data_dir, retry_seconds=0) is None
    finally:
        os.close(first_fd)
    assert sleeps
