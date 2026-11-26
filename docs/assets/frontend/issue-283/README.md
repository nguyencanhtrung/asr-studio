# Transcript workspace verification

Generated screenshots and measurement records are stored outside Git. The local-only
preserved evidence is `/tmp/asr-visual-correction/evidence/issue-283/preserved-5baadbf/`.
It includes the same synthetic meeting before/after on desktop/mobile, light/dark
and Vietnamese/English, plus context/speaker views. `provenance.json` records hashes
and source limits; local paths require access to this workspace. No remote artifact
upload is implied.

Desktop uses 1440×900 and mobile 390×844. The recorded ready-completed desktop
light/VI first-row bottom is 897.28125px, with search at 588.6875px. The records
report no horizontal overflow or page errors. Source IDs differ between disposable
fixtures. No real audio, provider request, inference, credentials or download is used.

The maintained capture utility writes only to an explicitly supplied external
directory and rejects this checkout as its output location:

```sh
PYTHONPATH="$PWD/src:$PWD/tests" .venv/bin/python tests/visual/capture_transcript_workspace.py after /tmp/asr-visual-correction/evidence/issue-283/current
```

Run phase `before` with the baseline checkout's `src`/`tests` on `PYTHONPATH`.
The script requires local Chrome and ffmpeg and never starts audio playback.
Current test and CI results, candidate commit and artifact location belong in
the PR's concise validation summary.
