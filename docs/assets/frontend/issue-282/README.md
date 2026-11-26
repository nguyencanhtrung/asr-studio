# Notes layout evidence (#282)

Each before/after pair uses the same disposable persisted Notes revision and title.
Baseline assets come from `a112687`; after assets come from the working checkout.
All other frontend modules and backend APIs use the checkout. No live provider or
model is used; provider status/catalog reads use fixtures. Temporary authentication
and data directories are removed after capture.

`measurements.json` records document coordinates from Chromium at desktop/mobile,
light/dark, VI/EN. Full-page images show content; `-focus.png` images show keyboard
focus at the same viewport. The desktop initial viewport is the first 900 pixels.
The unchanged centered Settings/GitHub footer remains above Version.

From the repository root, with its dependencies available:

```sh
PYTHONPATH="$PWD/src:$PWD/tests" .venv/bin/python docs/assets/frontend/issue-282/capture.py
```

The runner expects `/usr/bin/google-chrome`. The author used the shared Python
environment with the task checkout first on `PYTHONPATH`.
