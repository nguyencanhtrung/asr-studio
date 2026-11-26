# Transcript reading workspace evidence

The before/after images use disposable databases with the same meeting title,
spoken text, speaker labels and generated sine-wave audio. Internal fixture IDs
differ. The before checkout is `a112687`. After images were refreshed on the candidate
that includes the latest-main Notes merge and the ASR completion focus repair;
the recorded bounds still match the initial `895ef66` capture. No real
recording, provider request, model inference, user credentials or model download
is involved. The capture runner never starts audio playback.

Desktop images use 1440×900; mobile images use 390×844. Each covers light/dark
and Vietnamese/English. `after-context-*` also exposes the contextual segment and
speaker controls; mobile context samples scroll to the native disclosure.
`before.json` and `after.json` record viewport bounds, overflow and page errors.
The after-context pass uses the native context summary and then scrolls the nested
`#speaker-catalog` and its first speaker chip into the viewport, so desktop and
mobile context images show the actual speaker controls rather than a duplicate
base frame. The refreshed matrix reports no horizontal overflow or page errors.

- [Desktop before](before-desktop-light-vi.png)
- [Desktop after](after-desktop-light-vi.png)
- [Desktop context](after-context-desktop-light-vi.png)
- [Mobile context](after-context-mobile-dark-en.png)

From the intended checkout, using its source and the existing test dependencies:

```sh
PYTHONPATH="$PWD/src:$PWD/tests" .venv/bin/python docs/assets/frontend/issue-283/capture.py after /tmp/transcript-capture
```

Use the baseline checkout and phase `before` to capture the previous layout.
The runner requires local Chrome and ffmpeg; both servers and data are temporary.
Existing native player, source links and inline row editor remain the mutation
owners. The new context action focuses that editor and preserves draft identity.
