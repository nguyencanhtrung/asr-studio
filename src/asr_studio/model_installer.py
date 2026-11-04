"""Install one catalog ASR model into /models: the routine the CLI and the installer service share.

The weights arrive in a staging directory beside the target, are validated there, and reach the
target by one rename, so the worker never sees a half-written model. A prepared model is never
touched, and a target that holds other files is never overwritten.

`python -m asr_studio.model_installer` is the long-running service. It does one job at a time and
runs each download in a child process group, so Cancel can stop it and its helpers at once.
`python -m asr_studio.model_installer fetch <model> <staging>` is that child.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from asr_studio.asr_models import CATALOG, AsrModel, model_path
from asr_studio.config import Settings
from asr_studio.model_install import InstallError, InstallStore
from asr_studio.runner import ModelConfigurationError, prepared_engine_directory

PHASE_FILE = ".phase"
CHILD_EXIT_CODES = {"network": 20, "disk_full": 21, "integrity": 22}
POLL_SECONDS = 0.5
KILL_GRACE_SECONDS = 5.0

Fetch = Callable[[AsrModel, Path], None]


class InstallCanceled(Exception):
    """The operator cancelled the job; nothing is left to report."""


# -- the routine ---------------------------------------------------------------------------


def staging_directory(model: AsrModel, target: Path, install_id: str) -> Path:
    return target.parent / f".install-{model.id}-{install_id}"


def _is_empty_or_absent(path: Path) -> bool:
    return not path.exists() or (path.is_dir() and not any(path.iterdir()))


def install_model(
    model: AsrModel,
    target: Path,
    *,
    install_id: str,
    fetch: Fetch,
    phase: Callable[[str], None] = lambda _phase: None,
) -> bool:
    """Make `target` a prepared model. False when it already was, and nothing was touched."""
    try:
        prepared_engine_directory(model.engine, target)
        return False
    except ModelConfigurationError:
        pass
    if not _is_empty_or_absent(target):
        raise InstallError("target_not_empty")
    staging = staging_directory(model, target, install_id)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(target.parent).free
        if free < model.required_bytes:
            raise InstallError(
                "insufficient_disk", required_bytes=model.required_bytes, free_bytes=free
            )
        staging.mkdir()
        fetch(model, staging)
        phase("verifying")
        try:
            prepared_engine_directory(model.engine, staging / "model")
        except ModelConfigurationError:
            raise InstallError("integrity") from None
        os.rename(staging / "model", target)  # onto an absent or empty directory, or it fails
    except OSError as error:
        raise InstallError(_code_of_os_error(error)) from error
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return True


def _code_of_os_error(error: OSError) -> str:
    if error.errno in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOTDIR):
        return "target_not_empty"
    if error.errno == errno.ENOSPC:
        return "disk_full"
    if error.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        return "models_not_writable"
    return "interrupted"


# -- the download, in this process (CLI) or a child (installer) ---------------------------------


def _set_phase(staging: Path, phase: str) -> None:
    (staging / PHASE_FILE).write_text(phase, encoding="ascii")


def download_model(model: AsrModel, staging: Path) -> None:
    """Fetch the pinned revision into staging/model, converting it when the engine needs that."""
    from huggingface_hub import snapshot_download

    def download(where: Path) -> None:
        try:
            snapshot_download(repo_id=model.repository, revision=model.revision, local_dir=where)
        except OSError as error:
            if error.errno == errno.ENOSPC:
                raise InstallError("disk_full") from error
            raise InstallError(
                "integrity" if "Consistency check" in str(error) else "network"
            ) from error
        except Exception as error:  # httpx, huggingface_hub: all of them are the network's
            raise InstallError("network") from error

    _set_phase(staging, "downloading")
    if model.engine != "whisper_vi":
        download(staging / "model")
        # The downloader's own bookkeeping is not part of the model.
        shutil.rmtree(staging / "model" / ".cache", ignore_errors=True)
        return
    from ctranslate2.converters import TransformersConverter

    download(staging / "source")
    _set_phase(staging, "converting")
    try:
        TransformersConverter(
            str(staging / "source"),
            copy_files=["tokenizer.json", "preprocessor_config.json"],
            load_as_float16=True,
        ).convert(str(staging / "model"), quantization="float16")
    except OSError as error:
        raise InstallError("disk_full" if error.errno == errno.ENOSPC else "integrity") from error
    except Exception as error:
        raise InstallError("integrity") from error
    shutil.rmtree(staging / "source")


def _fetch_main(model_id: str, staging: Path) -> int:
    try:
        download_model(CATALOG[model_id], staging)
    except InstallError as error:
        return CHILD_EXIT_CODES.get(error.code, 22)
    return 0


# -- the installer service ------------------------------------------------------------------


def _directory_bytes(path: Path) -> int:
    total = 0
    for root, _directories, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_size
    return total


def child_argv(model: AsrModel, staging: Path) -> list[str]:
    return [sys.executable, "-m", "asr_studio.model_installer", "fetch", model.id, str(staging)]


class Installer:
    def __init__(
        self,
        settings: Settings,
        store: InstallStore,
        *,
        argv: Callable[[AsrModel, Path], list[str]] = child_argv,
        poll_seconds: float = POLL_SECONDS,
        kill_grace_seconds: float = KILL_GRACE_SECONDS,
    ) -> None:
        self.settings = settings
        self.store = store
        self.argv = argv
        self.poll_seconds = poll_seconds
        self.kill_grace_seconds = kill_grace_seconds
        self.stopping = threading.Event()

    def _model_directories(self) -> set[Path]:
        return {Path(model_path(self.settings, model_id)).parent for model_id in CATALOG}

    def _models_writable(self) -> bool:
        for directory in self._model_directories():
            try:
                directory.mkdir(parents=True, exist_ok=True)
                # An unnamed file: no name means no existing link to follow, and nothing to remove.
                with tempfile.TemporaryFile(dir=directory):
                    pass
            except OSError:
                return False
        return True

    def beat(self) -> None:
        self.store.beat(models_writable=self._models_writable())

    def recover(self) -> None:
        """A restart fails the jobs it interrupted, honours a cancel, sweeps orphan staging."""
        for request in self.store.pending():
            status = self.store.status(request["model"])
            if self.store.cancel_requested(request):
                self.store.finish(request)
            elif (
                status is not None
                and status.get("id") == request["id"]
                and status.get("state") == "running"
            ):
                self.store.finish(request, keep={"state": "failed", "error": "interrupted"})
        self.store.sweep_cancels()
        for directory in self._model_directories():
            if directory.is_dir():
                for orphan in directory.glob(".install-*-*"):
                    shutil.rmtree(orphan, ignore_errors=True)

    def tick(self) -> bool:
        """Beat, then run the oldest queued job to its end. True when one ran."""
        self.beat()
        for request in self.store.pending():
            self._run(request)
            return True
        return False

    def serve(self) -> None:
        self.recover()
        while not self.stopping.is_set():
            if not self.tick():
                self.stopping.wait(1.0)

    def _run(self, request: dict) -> None:
        model = CATALOG[request["model"]]
        if self.store.cancel_requested(request):
            self.store.finish(request)
            return
        self.store.report(
            request, state="running", phase=None, bytes_done=0, bytes_total=model.download_bytes
        )

        def report(phase: str | None, done: int = 0) -> None:
            self.store.report(
                request,
                state="running",
                phase=phase,
                bytes_done=done,
                bytes_total=model.download_bytes,
            )

        try:
            # In the try: a target that cannot resolve (a link loop) ends this job, not the service.
            target = Path(model_path(self.settings, model.id)).expanduser().resolve()
            install_model(
                model,
                target,
                install_id=request["id"],
                fetch=lambda m, staging: self._fetch(request, m, staging, report),
                phase=report,
            )
        except InstallCanceled:
            self.store.finish(request)
        except InstallError as error:
            print(f"install {model.id}: {error.code}", flush=True)
            self.store.finish(
                request, keep={"state": "failed", "error": error.code, **error.details}
            )
        except Exception as error:  # a bug must end the job, not strand it
            print(f"install {model.id}: unexpected {type(error).__name__}", flush=True)
            self.store.finish(request, keep={"state": "failed", "error": "interrupted"})
        else:
            self.store.finish(request)

    def _fetch(self, request: dict, model: AsrModel, staging: Path, report) -> None:
        process = subprocess.Popen(
            self.argv(model, staging),
            start_new_session=True,  # its own process group: Cancel stops the helpers too
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        last = None
        try:
            while (code := process.poll()) is None:
                if self.stopping.is_set():
                    raise InstallError("interrupted")
                if self.store.cancel_requested(request):
                    raise InstallCanceled
                phase = self._phase(staging)
                done = min(_directory_bytes(staging), model.download_bytes)
                if (phase, done) != last:
                    report(phase, done)
                    last = (phase, done)
                self.beat()
                time.sleep(self.poll_seconds)
        except BaseException:
            self._kill(process)
            raise
        if code != 0:
            code_name = {value: key for key, value in CHILD_EXIT_CODES.items()}.get(code)
            raise InstallError(code_name or "interrupted")

    def _phase(self, staging: Path) -> str:
        try:
            phase = (staging / PHASE_FILE).read_text(encoding="ascii")
        except OSError:
            return "downloading"
        return phase if phase in {"downloading", "converting"} else "downloading"

    def _kill(self, process: subprocess.Popen) -> None:
        """Ask the group to stop, then end whatever is left of it, helpers included."""
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=self.kill_grace_seconds)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="asr-model-installer")
    commands = parser.add_subparsers(dest="command")
    fetch = commands.add_parser("fetch", help="download one model into a staging directory")
    fetch.add_argument("model", choices=tuple(CATALOG))
    fetch.add_argument("staging", type=Path)
    arguments = parser.parse_args(argv)
    if arguments.command == "fetch":
        return _fetch_main(arguments.model, arguments.staging)
    settings = Settings.from_env()
    installer = Installer(settings, InstallStore(settings.install_dir))
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: installer.stopping.set())
    installer.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
