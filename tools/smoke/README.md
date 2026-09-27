# Smoke tests on the real display

End-to-end runs of the real window and the built AppImage against a fake
OpenAI-compatible endpoint, so nothing costs money and nothing leaves the
machine. Written for the pre-beta review (Sept 2026); reuse before a release.

## The fake endpoint

`fake_server.py PORT TAG` serves `/v1/chat/completions` (SSE, a canned
passage with `TAG` in it; valid JSON for the scene, chronicle, director,
lore-pick and character-scan reads, keyed on their prompts), `/v1/models`,
`/v1/embeddings`, `/v1/image-models` and `/v1/images/generations`. One line
per request goes to stdout, so a run can show which endpoint each call went
to. Run two: one for the story, one as the "private" model.

```bash
.venv/bin/python tools/smoke/fake_server.py 11435 MAIN &
.venv/bin/python tools/smoke/fake_server.py 11436 PRIVATE-ZQX &
sealedlore --data-dir /tmp/smoke configure --base-url http://127.0.0.1:11435/v1 --api-key k --model fake/storyteller
```

Then set `private_provider` in `/tmp/smoke/config.json` to
`http://127.0.0.1:11436/v1`, model `fake/private`, and create a story
(`bundle_from_scenario` over `SampleStories/Doomsville.md`, as
`tests/test_private.py::build` does).

## The window, driven in-process

`drive_window.py DATA_DIR STORY_ID OUT_DIR` opens the real `MainWindow` on
`$DISPLAY`, plays a public turn, a memory-only private scene (checks the
scene and plot controls freeze, that leaving the story asks first, that
nothing from the scene is on disk), approves the summary, plays on, opens
About, and closes. Screenshots of the screen go to `OUT_DIR`; it prints
PASS/FAIL per check and exits non-zero on a failure.

Modal dialogs (`exec()`) nest inside whatever `processEvents()` call opened
them, so the script drives them from `QTimer` callbacks, never by polling.

## The AppImage

`appimage.sh DATA_DIR STORY_ID [APPIMAGE]` launches the built AppImage on the
display (it needs `xdotool`, `wmctrl` and `pstree`), finds its window through
the launcher's process tree, clicks into
the composer, types a turn with real key events, waits for the reply to be
saved, and closes the window cleanly.

**Never find the window by title.** An editor with the project open has
"SealedLore" in its title too; a title match once closed the author's
VSCodium mid-session. `xdotool search --pid` on the process tree, then
check the name. And `xdotool type` without `--window`: synthetic events sent
to a window are ignored by Qt.

`appimage_tls.sh [APPIMAGE]` (offline, no display) checks that the bundled
OpenSSL finds certificate authorities through AppRun's `SSL_CERT_FILE`.
Without it Sigstore's trust-root update fails, and with it every `private/`
model's attestation. Run it after every build: the test suite can't see
this, because the dev venv uses the system's OpenSSL.

`screenshot.py FILE.png` grabs the whole screen with Qt (no ImageMagick
needed).
