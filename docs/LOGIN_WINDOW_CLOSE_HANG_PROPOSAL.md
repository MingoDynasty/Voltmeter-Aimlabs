# Proposal v2: `voltmeter login` hangs (un-interruptibly) when the window is closed without logging in

**Status:** Proposal v2 — ready to implement. Small, self-contained change to the shared
auth layer (`aimlabs_auth.py::login_and_capture`). Mirrors the one-PR-off-`main` workflow.
**Found:** 2026-06-17 (Claude Code), from a user report.
**Related:** `aimlabs_auth.py` (`login_and_capture` / `_poll`); pywebview (`webview.start`,
WebView2/winforms `get_cookies`).

---

## Version 2 updates

- Tightens the implementation sketch so pywebview still controls window initialization timing:
  use a short pywebview callback to spawn the daemon poller, then return immediately.
- Tightens the test plan so it covers the load-bearing regression: a poller stuck inside
  `get_cookies()` must not keep `login_and_capture()` or interpreter shutdown alive.

---

## TL;DR

If you run `voltmeter login` and **close the login window without logging in**, the command
prints `login: no session captured …` and then **hangs forever** — Ctrl+C does nothing, and
the console has to be killed.

The cookie-polling loop runs in a background thread that (a) has no idea the window was closed
and (b) is started by pywebview as a **non-daemon** thread. After the window closes it blocks
forever inside `window.get_cookies()`, and because it is non-daemon the interpreter can't exit
and the Ctrl+C is swallowed by the shutdown thread-join.

**Fix:** run the long-lived poller as a **daemon thread we own**, and signal it to stop once
`webview.start()` returns. A tiny pywebview callback may still be used to start that daemon
thread at the right point in pywebview's lifecycle, but the pywebview-created thread must return
immediately. Daemon-ness guarantees the process can always exit; the stop signal lets the
thread exit gracefully in the normal case.

---

## Symptom (as reported)

```
PS> uv run voltmeter login
login: opening the Aim Lab login window -- log in normally; it closes automatically once your session is captured.
login: cookies visible now: [... csrf/state/callback-url + rl_* ...]
login: cookies visible now: ['rl_anonymous_id', ... , 'rl_user_id']
login: no session captured (window closed or timed out before login completed). ... Nothing written.
```

…and then it is frozen. Ctrl+C is ignored; only closing the console terminates it.

## Root cause

In [`login_and_capture`](../aimlabs_auth.py), the cookie watcher `_poll` is handed to
`webview.start(_poll, (login_window,))`. Its only stop conditions are "a `session-token` cookie
appeared" or "the deadline passed" (`while time.time() < deadline`, deadline = now +
`DEFAULT_LOGIN_TIMEOUT_SECONDS`, **300 s**). There is **no subscription to the window-closed
event**, so closing the window satisfies neither condition.

Two facts turn that into a permanent, un-interruptible hang:

1. **`get_cookies()` blocks forever on a dead window.** On Windows/WebView2 the call marshals
   work onto the GUI thread and then does `semaphore.acquire()` with no timeout
   (`webview/platforms/winforms.py`). Once the window is closed the GUI thread is gone, so
   nothing ever releases the semaphore — the poll thread is stuck *inside* `get_cookies()`,
   below any loop-level check.
2. **The poll thread is non-daemon.** pywebview starts it with a bare
   `threading.Thread(target=func, args=args)` — no `daemon=True` (`webview/__init__.py`). At
   interpreter shutdown CPython joins all non-daemon threads, so the main thread (which already
   returned exit code 1) parks forever in that join.

**Why Ctrl+C does nothing:** `KeyboardInterrupt` is delivered only to the main thread, and the
main thread is inside a C-level thread-join during interpreter teardown, which is not
interruptible on Windows. The signal is effectively ignored until the joined thread finishes —
which it never does.

(Even in the gentler timing where `get_cookies()` raises instead of blocking — caught by the
existing `except` in `_poll` — you'd still wait out the full 300 s before the process exits.)

## Proposed fix

Take ownership of the long-lived poll thread instead of letting pywebview run `_poll` directly:

- Create a `threading.Event` (`stop`).
- Change `_poll` to accept that stop event.
- Add a tiny starter callback, for example `_start_poll(window)`, that creates
  `threading.Thread(target=_poll, args=(window, stop), daemon=True).start()` and then returns.
- Pass the starter callback to `webview.start(_start_poll, (login_window,))` instead of passing
  `_poll` itself. This keeps pywebview's existing initialization sequencing, but the
  pywebview-created non-daemon thread is no longer the one that can wedge inside
  `get_cookies()`.
- After `webview.start()` returns, `stop.set()`.
- In `_poll`, replace `time.sleep(LOGIN_POLL_INTERVAL_SECONDS)` with `stop.wait(interval)` and
  check `stop.is_set()` at the top of the loop, so the thread wakes and exits immediately on
  close in the common case.

Why this is the right fix:

- **Daemon-ness is the load-bearing part.** Even if the thread is wedged inside `get_cookies()`
  on the dead window, a daemon thread is abandoned at interpreter exit — the process always
  terminates on its own, so the user never needs Ctrl+C.
- **The stop event is the graceful part.** When the thread is between polls (the ~999 ms/s
  common case) it exits cleanly rather than being abandoned mid-call.
- **The starter callback preserves pywebview's lifecycle.** The current pywebview callback path
  runs after pywebview has initialized the `Window` object. Keeping a starter callback avoids
  racing the poller against an uninitialized window while still ensuring the long-lived work runs
  only in our daemon thread.
- **Relies only on `webview.start()` returning**, which is the well-defined "GUI loop ended"
  signal — not on a backend-specific close event firing.

### Alternative considered — and why it's insufficient

Subscribing to `window.events.closing` to set a stop flag (the first idea) only fixes the
300 s-timeout variant. The thread is almost always blocked *inside* `get_cookies()` when the
window closes, so it never reaches the flag check; and it's still non-daemon, so the permanent
freeze and the dead Ctrl+C remain in exactly the reported case. It reduces the probability of
the hang without eliminating it. (We don't need this event at all under the daemon approach.)

## Scope / files touched

- `aimlabs_auth.py::login_and_capture` and its nested `_poll` — the thread ownership, the
  `stop` event, and the `stop.wait`/`is_set` checks. No public API or signature changes.
- No change to the success path: on capture, `_poll` still sets `captured["value"]` and calls
  `window.destroy()`, `webview.start()` returns, and the main thread writes `.env` as today.

## Testing

- Unit-test `_poll`'s graceful stop behavior with a fake window object (a stub `get_cookies()`
  plus a `stop` event): assert the loop returns promptly once `stop` is set and never blocks
  past one poll interval.
- Unit-test the load-bearing close-hang regression with a fake webview/window where
  `get_cookies()` enters and then blocks. Have fake `webview.start` invoke the starter callback,
  wait until `get_cookies()` is blocked, and then return as though the user closed the window.
  Assert `login_and_capture()` still returns promptly with no captured session and nothing
  written, and assert the long-lived poll thread was created with `daemon=True`. Release the
  fake block during test cleanup so the daemon does not linger unnecessarily.
- Keep the existing success-path and timeout/no-cookie fake-webview tests passing with the new
  starter callback shape. These protect `.env` writes, identity verification, and the
  "backend hides the httpOnly cookie" hint.
- Manual check on Windows: `voltmeter login`, close the window without logging in → the command
  prints `no session captured …` and the process exits **on its own within ~1 s**, no Ctrl+C
  required.

## Acceptance criteria

- Closing the login window without logging in causes `voltmeter login` to exit cleanly within a
  poll interval (~1 s), with exit code 1 and the existing "no session captured" message.
- The process never depends on Ctrl+C or killing the console to terminate.
- A unit test exercises the stop-event path of `_poll` with a fake window.
- A unit test exercises the blocked-`get_cookies()` path and proves the long-lived poll thread is
  daemon-owned.
- The successful-login capture path is unchanged (cookie captured → `.env` written → verified).
