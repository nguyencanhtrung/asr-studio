# Settings layout verification

Generated screenshots are local-only artifacts under
`/tmp/asr-visual-correction/evidence/issue-284/preserved-e421dfe/`.
The provenance file records before source `a112687`, prepared Settings source
`e421dfe`, conditions and hashes. No artifact upload is implied. The same
synthetic empty workspace and provider API fixtures supply connected sample
accounts; these images do not prove live-provider readiness or model installation.

Each phase covers overview, OpenAI, Google, processing, ASR, devices, appearance
and About at desktop/mobile in both themes and VI/EN. The modal, provider/account
actions and original footer remain. Desktop groups and compact model/effort fields
preserve mobile navigation. Layout and keyboard checks are maintained in
`tests/test_browser_settings_layout.py`, including the 680/681 navigation boundary.

The maintained capture utility requires local Chrome and the test dependencies,
waits for panels, provider controls, fonts and network idle, and rejects output
inside the checkout:

```sh
PYTHONPATH="$PWD/src:$PWD/tests" .venv/bin/python tests/visual/capture_settings.py /tmp/asr-visual-correction/evidence/issue-284/current
```

Use the baseline checkout's `src` and `tests` on `PYTHONPATH` for before images.
Only concise candidate/command/result/environment and artifact locations belong
in the PR summary; generated images and raw output stay outside Git.
