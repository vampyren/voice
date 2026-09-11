# Deferred review findings (phase 1)

Minor items raised during the phase 1 reviews that were judged not to block the branch. Each is real; none affects a happy-path dictation. Pick them up in phase 2 or when touching the file.

## Concurrency and lifecycle
- `voice/daemon.py`: two warmup threads can overlap after a fast profile switch (duplicate "Running on CPU" notification and tray emit; the model lock prevents a double load).
- `voice/ipc.py`: two simultaneous cold starts can both probe "nothing listening", both unlink, and both bind; `stop()` never joins the serve thread; any read error is labelled "timeout".
- `voice/pipeline.py`: `recorder.stop()` runs under the state lock (up to ~4 s); a reserved TRANSCRIBING/INJECTING state has no recovery if the executor dispatch itself raises; `_thread_executor` discards the future.
- `voice/hotkey/evdev_listener.py`: instance-state hazard if `start()` is called again after a timed-out `stop()` join; `_rescan` exceptions propagate out of `start()`; a factory returning the same device object is closed as a duplicate (test doubles only).
- `voice/ui/settings.py`: narrow read-then-write window between the on-disk `stt.active` carry-over and the final save.

## Behaviour and UX
- `voice/ui/tray.py`: old `QAction`s are removed but not `deleteLater()`'d on `set_profiles`.
- `voice/ui/settings.py`: adding an already-present template is a silent no-op; no Cancel/revert button (Close discards).
- `voice/ui/notify.py`: `notify-send` processes are never reaped.
- `voice/inject/portal.py`: `close()` swallows errors without logging; `TokenStore` has a benign TOCTOU.
- `voice/inject/fallback.py` and `clipboard.py`: duplicated subprocess-error mapping; `make_key_sender` ignores `available()` for the portal.
- `voice/history.py`: `keep_audio` stores the array by reference; `_flush` chmods after writing.
- `voice/config.py`: `load()` writes the default file before chmod (no secrets at that point); importing `Config` now pulls in `evdev` via chord validation.
- `voice/doctor.py`: `fallback senders` reports ✔ with none installed; the individual probes have no unit tests.
- `voice/stt/openai_compat.py`: profile-level prompt fallback untested; OpenRouter host match is a substring test.
- `install.sh`: throwaway `sed` pass; the uninstall `|| true` masks a real `rm` failure.
- `tests/ui/test_settings.py::test_close_discards_edits_and_never_touches_the_callers_config` asserts nothing that could fail; strengthen or delete.
- Dictionary tab gives most width to the Flags column.

## Cleanup candidates cut by the final review
- Profile-template duplication between `voice/config.py` `DEFAULT_CONFIG` and `voice/ui/settings.py` `PROFILE_TEMPLATES`.
- Copy-paste between `Daemon.build()` and `Daemon.apply_config()` for the injector.
- Untested portal session lifecycle and daemon `run()`/`shutdown()` paths (boundary-only today).
