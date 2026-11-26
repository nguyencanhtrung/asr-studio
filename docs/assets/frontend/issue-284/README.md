# Settings layout verification

Settled before/after screenshots are local-only artifacts under
`/tmp/asr-visual-correction/issue-284-formal/evidence/stable-r3/`.
The external provenance record pins baseline `a112687`, the final candidate,
conditions and hashes. Earlier captures are retained separately and are not
the settled comparison evidence. No artifact upload is implied. The same
synthetic empty workspace and provider API fixtures supply connected sample
accounts; these images do not prove live-provider readiness or model installation.

Each phase covers eight destinations: overview, OpenAI, Google, processing, ASR,
devices, appearance and About. Four paired variants produce 32 images per phase:
desktop light VI, desktop dark EN, mobile dark VI and mobile light EN.
The rendered layout tests separately cover both languages and themes at all four
test widths. The modal, provider/account
actions and original footer remain. Desktop groups and compact model/effort fields
preserve mobile navigation. Layout and keyboard checks are maintained in
`tests/test_browser_settings_layout.py`, including the 680/681 navigation boundary.

The maintained capture utility requires local Chrome and the test dependencies,
waits for panels, provider controls, fonts and network idle, disables animations
for the screenshot so finite tab transitions settle, and rejects output
inside the checkout:

```sh
PYTHONPATH="$PWD/src:$PWD/tests" .venv/bin/python tests/visual/capture_settings.py /tmp/asr-visual-correction/issue-284-formal/evidence/stable-r3/after
```

Use the baseline checkout's `src` and `tests` on `PYTHONPATH` for before images.
Only concise candidate/command/result/environment and artifact locations belong
in the PR summary; generated images and raw output stay outside Git.
