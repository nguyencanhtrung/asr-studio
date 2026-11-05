#!/usr/bin/env python3
"""Capture the public README screenshots, dark theme, in English and Vietnamese.

The command starts a fresh in-process app on an ephemeral loopback port, seeds only the
checked-in fixture into a temporary SQLite data directory, and uses a stub notes provider.
It never reads an existing service, recordings, credentials, or provider state.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
SOURCE_ROOT = Path(os.environ.get("ASR_README_SOURCE_ROOT", ROOT / "src")).resolve()
sys.path.insert(0, str(SOURCE_ROOT))
sys.path.insert(0, str(TESTS))

from notes_helpers import seed_cited_notes, seed_completed_transcript  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from test_browser_notes_provider import mock_providers  # noqa: E402

from asr_studio.app import create_app  # noqa: E402
from asr_studio.auth import AuthStore  # noqa: E402
from asr_studio.config import Settings  # noqa: E402
from asr_studio.model_install import InstallStore  # noqa: E402
from asr_studio.runtime import RuntimeProfile, write_runtime_profile  # noqa: E402
from asr_studio.storage import database_path  # noqa: E402
from asr_studio.worker import acquire_worker_lock, write_heartbeat  # noqa: E402

VIEWPORT = {"width": 1440, "height": 1000}
FIXED_NOW = "2026-10-21T10:00:00+00:00"
LANGUAGES = (("en", ""), ("vi", "-vi"))  # interface language, and the file name's suffix


PAGES = (
    ("#notes", "meeting-notes", "#notes-saved-work"),
    ("#transcript", "transcript", "#transcript-segments > li"),
)


class LocalServer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.url = settings.public_url
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None

    def start(self) -> None:
        parsed = self.url.rsplit(":", 1)
        host, port = parsed[0].removeprefix("http://"), int(parsed[1])
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen()
        self._socket = listener
        self._server = uvicorn.Server(
            uvicorn.Config(
                create_app(self.settings),
                log_config=None,
                log_level="warning",
                access_log=False,
            )
        )
        self._thread = threading.Thread(
            target=self._server.run,
            kwargs={"sockets": [listener]},
            daemon=True,
        )
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started and time.monotonic() < deadline:
            time.sleep(0.025)
        if not self._server.started:
            raise RuntimeError("synthetic README app did not start")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._socket is not None:
            self._socket.close()

    @property
    def access_link(self) -> str:
        return f"{self.url}/access#{AuthStore(self.settings.auth_dir).load().token}"


def load_fixture(path: Path) -> dict[str, Any]:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if fixture.get("schema_version") != 1:
        raise ValueError("unsupported README fixture schema")
    meeting = fixture.get("meeting")
    speakers = fixture.get("speakers")
    segments = fixture.get("segments")
    notes = fixture.get("notes")
    if not isinstance(meeting, dict) or not isinstance(speakers, list):
        raise ValueError("fixture meeting and speakers must be objects/lists")
    if not isinstance(segments, list) or len(segments) != 3:
        raise ValueError("fixture must contain exactly three segments")
    if not isinstance(notes, dict):
        raise ValueError("fixture notes must be an object")
    speaker_ids = {speaker["id"] for speaker in speakers}
    if len(speaker_ids) != len(speakers):
        raise ValueError("fixture speaker IDs must be unique")
    for segment in segments:
        if segment["speaker_id"] not in speaker_ids:
            raise ValueError("fixture segment refers to an unknown speaker")
    return fixture


def seed_fixture(data_dir: Path, fixture: dict[str, Any]) -> str:
    meeting = fixture["meeting"]
    meeting_id, generation_id, seeded_ids = seed_completed_transcript(
        data_dir,
        title=meeting["title"],
        meeting_date=meeting["date"],
    )
    segment_ids = [*seeded_ids, uuid.uuid4().hex]
    segments = fixture["segments"]
    speakers = fixture["speakers"]
    with sqlite3.connect(database_path(data_dir)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        speaker_ranges = {}
        for speaker in speakers:
            speaker_segments = [
                segment for segment in segments if segment["speaker_id"] == speaker["id"]
            ]
            speaker_ranges[speaker["id"]] = (
                min(segment["start_seconds"] for segment in speaker_segments),
                max(segment["end_seconds"] for segment in speaker_segments),
            )
            connection.execute(
                "INSERT OR IGNORE INTO generation_speakers(generation_id, speaker_id, "
                "display_name, first_seen_seconds, last_seen_seconds) VALUES (?, ?, ?, ?, ?)",
                (
                    generation_id,
                    speaker["id"],
                    speaker["name"],
                    *speaker_ranges[speaker["id"]],
                ),
            )
        for sequence, (segment_id, segment) in enumerate(zip(segment_ids, segments, strict=True)):
            if sequence < len(seeded_ids):
                connection.execute(
                    "UPDATE transcript_segments SET sequence = ?, start_seconds = ?, "
                    "end_seconds = ?, text = ?, speaker_id = ? "
                    "WHERE id = ?",
                    (
                        sequence,
                        segment["start_seconds"],
                        segment["end_seconds"],
                        segment["text"],
                        segment["speaker_id"],
                        segment_id,
                    ),
                )
            else:
                connection.execute(
                    "INSERT INTO transcript_segments(id, generation_id, sequence, "
                    "start_seconds, end_seconds, text, original_speaker_id, speaker_id, "
                    "avg_logprob, no_speech_prob, compression_ratio, uncertain, "
                    "review_reasons, overlap, speaker_dominance, assignment_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, -0.2, 0.05, 1.0, 0, '[]', 0, 1.0, 1)",
                    (
                        segment_id,
                        generation_id,
                        sequence,
                        segment["start_seconds"],
                        segment["end_seconds"],
                        segment["text"],
                        segment["speaker_id"],
                        segment["speaker_id"],
                    ),
                )
        connection.execute(
            "UPDATE transcript_generations SET segment_count = ? WHERE id = ?",
            (len(segment_ids), generation_id),
        )
        for speaker in speakers:
            connection.execute(
                "UPDATE generation_speakers SET display_name = ?, first_seen_seconds = ?, "
                "last_seen_seconds = ? WHERE generation_id = ? AND speaker_id = ?",
                (
                    speaker["name"],
                    *speaker_ranges[speaker["id"]],
                    generation_id,
                    speaker["id"],
                ),
            )
        connection.execute(
            "UPDATE recordings SET duration_seconds = ? WHERE meeting_id = ?",
            (meeting["duration_seconds"], meeting_id),
        )

    def build_document(source_ids: list[str]) -> dict[str, Any]:
        def items(section: str) -> list[dict[str, Any]]:
            result = []
            for item in fixture["notes"].get(section, []):
                result.append(
                    {key: value for key, value in item.items() if key != "source_indices"}
                    | {"source_ids": [source_ids[index] for index in item["source_indices"]]}
                )
            return result

        return {
            "schema_version": 1,
            "overview": items("overview"),
            "key_topics": items("key_topics"),
            "decisions": items("decisions"),
            "open_questions": items("open_questions"),
            "action_items": items("action_items"),
            "confirmation_items": items("confirmation_items"),
        }

    seed_cited_notes(
        data_dir,
        build_document=build_document,
        existing=(meeting_id, generation_id, segment_ids),
        output_language=fixture["notes"]["output_language"],
    )
    return meeting_id


def prepare_models(settings: Settings) -> None:
    """A synthetic GPU machine with its default model: the other rows offer their install."""
    model = settings.asr_gpu_model_path
    model.mkdir(parents=True)
    (model / "config.json").write_text('{"model_type": "qwen3_asr"}', encoding="utf-8")
    (model / "processor_config.json").write_text(
        '{"feature_extractor": {"sampling_rate": 16000}}', encoding="utf-8"
    )
    for filename in (
        "generation_config.json",
        "model.safetensors",
        "tokenizer.json",
        "tokenizer_config.json",
        "chat_template.jinja",
    ):
        (model / filename).write_bytes(b"synthetic")
    write_runtime_profile(settings.data_dir, RuntimeProfile("cuda", "float16", model))
    # The seeded transcript came from this default.
    with sqlite3.connect(database_path(settings.data_dir)) as connection:
        for table in ("transcript_generations", "tasks"):
            connection.execute(f"UPDATE {table} SET model_name = ?", (model.name,))
    assert acquire_worker_lock(settings.data_dir) is not None


def keep_alive(settings: Settings) -> None:
    """No worker or installer runs here: the page should still show them as they normally are."""
    write_heartbeat(settings.data_dir, "idle")
    InstallStore(settings.install_dir).beat(models_writable=True)


def available_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def public_path(path: Path, *, kind: str) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return f"<external-{kind}>"


def capture(server: LocalServer, meeting_id: str, output_dir: Path) -> list[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    filenames: list[str] = []
    requested_urls: list[str] = []
    rejected_urls: list[str] = []
    allowed_origin = urlsplit(server.url)

    def handle_route(route: Any) -> None:
        url = urlsplit(route.request.url)
        if (url.scheme, url.netloc) != (
            allowed_origin.scheme,
            allowed_origin.netloc,
        ):
            rejected_urls.append(route.request.url)
            route.abort()
            return
        requested_urls.append(route.request.url)
        route.continue_()

    executable = os.environ.get("CHROME_BIN") or shutil.which("google-chrome")
    launch_options = {"executable_path": executable} if executable else {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, **launch_options)
        for language, suffix in LANGUAGES:
            context = browser.new_context(
                viewport=VIEWPORT,
                color_scheme="dark",
                reduced_motion="reduce",
            )
            context.add_init_script(
                f"localStorage.setItem('asr-studio:lang', '{language}');"
                "localStorage.setItem('asr-studio:theme', 'dark');"
            )
            page = context.new_page()
            page.clock.set_fixed_time(FIXED_NOW)
            page.route("**/*", handle_route)
            page.goto(server.access_link, wait_until="domcontentloaded")
            page.wait_for_url(f"{server.url}/", timeout=10_000)
            # A connected provider, stubbed in the browser: no account or network is involved.
            mock_providers(page, server)
            saved = page.request.put(
                f"{server.url}/api/notes-preferences",
                data={"provider": "chatgpt", "model": "gpt-6.1-sol", "reasoning_effort": "high"},
                headers={"Origin": server.url},
            )
            if not saved.ok:
                raise RuntimeError("synthetic notes preference was refused")

            for fragment, stem, required_selector in PAGES:
                keep_alive(server.settings)
                page.goto(
                    f"{server.url}/meetings/{meeting_id}{fragment}",
                    wait_until="domcontentloaded",
                )
                page.wait_for_selector(required_selector, timeout=10_000)
                page.evaluate("document.fonts.ready")
                filename = f"{stem}{suffix}.png"
                # As tall as the page, so the sidebar reaches the bottom of the picture.
                height = page.evaluate("document.documentElement.scrollHeight")
                page.set_viewport_size({"width": VIEWPORT["width"], "height": height})
                page.wait_for_timeout(200)
                page.screenshot(path=str(output_dir / filename), animations="disabled")
                page.set_viewport_size(VIEWPORT)
                filenames.append(filename)

            keep_alive(server.settings)
            page.set_viewport_size({"width": VIEWPORT["width"], "height": 1500})  # every row fits
            page.locator("#settings-open").click()
            page.locator("#settings-tab-asr").click()
            page.wait_for_selector("#asr-models button[data-action]", timeout=10_000)
            # The dialog scrolls at any height; the picture shows every row instead.
            page.evaluate(
                """() => {
                  const dialog = document.querySelector("#connections-dialog");
                  let node = document.querySelector("#settings-panel-asr");
                  while (node && node !== dialog.parentElement) {
                    node.style.height = "fit-content";
                    node.style.maxHeight = "none";
                    node.style.overflow = "visible";
                    node = node.parentElement;
                  }
                }"""
            )
            page.mouse.move(0, 0)  # no row looks hovered
            page.evaluate("document.fonts.ready")
            filename = f"settings-asr{suffix}.png"
            page.locator("#connections-dialog").screenshot(
                path=str(output_dir / filename), animations="disabled"
            )
            filenames.append(filename)
            context.close()
        browser.close()

    non_local = sorted(
        {
            url
            for url in requested_urls
            if (parsed := urlsplit(url)).scheme != allowed_origin.scheme
            or parsed.netloc != allowed_origin.netloc
        }
    )
    if rejected_urls or non_local:
        raise RuntimeError(
            "synthetic capture rejected "
            f"{len(rejected_urls)} non-matching-origin requests and observed "
            f"{len(non_local)} non-local continued requests"
        )
    return filenames


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        type=Path,
        default=ROOT / "tests/fixtures/readme-demo.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "docs/assets/readme",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    fixture_path = args.fixture.resolve()
    output_dir = args.output_dir.resolve()
    fixture = load_fixture(fixture_path)
    with tempfile.TemporaryDirectory(prefix="asr-readme-capture-") as temporary:
        temporary_root = Path(temporary)
        port = available_port()
        settings = Settings(
            data_dir=temporary_root / "data",
            auth_dir=temporary_root / "auth",
            install_dir=temporary_root / "install",
            public_url=f"http://127.0.0.1:{port}",
            asr_gpu_model_path=temporary_root / "models/qwen3-asr-1.7b",
            asr_cpu_model_path=temporary_root / "models/whisper-small",
            asr_phowhisper_model_path=temporary_root / "models/pho-whisper-large",
            asr_qwen_model_path=temporary_root / "models/qwen3-asr-1.7b",
        )
        meeting_id = seed_fixture(settings.data_dir, fixture)
        prepare_models(settings)
        server = LocalServer(settings)
        server.start()
        try:
            filenames = capture(server, meeting_id, output_dir)
        finally:
            server.stop()
    for filename in filenames:
        target = output_dir / filename
        if not target.is_file() or target.stat().st_size <= 10_000:
            raise RuntimeError(
                f"capture did not produce a usable image: {public_path(target, kind='output')}"
            )
    print(
        json.dumps(
            {
                "status": "pass",
                "fixture": public_path(fixture_path, kind="fixture"),
                "output_dir": public_path(output_dir, kind="output"),
                "screenshots": filenames,
                "viewport": VIEWPORT,
                "provider": "stub-only",
                "network": "loopback-only",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
