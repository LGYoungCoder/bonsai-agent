# Chrome Bridge (optional)

Bonsai controls a browser via the Chrome DevTools Protocol. Two supported
launch modes already cover most needs:

- **attach** — you start Chrome yourself with `--remote-debugging-port=9222`.
  Login state is preserved.
- **managed** — Bonsai spawns an isolated chromium with a dedicated profile.
  No shared login, but zero setup.

This directory contains a **third, optional path**: a Chrome extension that
lets Bonsai drive a Chrome instance **without** requiring the
`--remote-debugging-port` flag. Useful when:

- You can't (or don't want to) relaunch your daily Chrome with debug flags.
- You want Bonsai to operate on tabs you're already logged into, right now.

The extension exposes a local WebSocket bridge that Bonsai's browser tools
can use as an alternative backend — the service worker accepts inbound JS
strings or CDP method calls and routes them to the active tab.

## Install

1. Open `chrome://extensions/` in the Chrome you want Bonsai to drive.
2. Enable **Developer mode** (top right).
3. Click **Load unpacked** and select this directory
   (`<bonsai repo>/assets/chrome_bridge/`).
4. Pin the extension icon to the toolbar. Clicking it opens a popup showing
   the bridge port (default `18765`).

## Use

```
# 1) Install this extension into your daily Chrome (steps above).
# 2) Install bonsai's WS dependency once:
pip install bonsai-agent[bridge]

# 3) Start bonsai. The WS server boots and waits for the extension to
#    auto-reconnect (≤30s):
bonsai chat --browser bridge
```

The extension probes `ws://127.0.0.1:18765` every ~5s, so the order doesn't
matter — start either side first and they'll meet.

## Limitations vs CDP-direct modes

The bridge runs in an MV3 service worker, so we lose access to a few CDP
primitives that `attach` / `managed` get for free:

- `web_scan` returns the pruned-DOM rendering only (no Accessibility tree).
  The model still sees buttons / inputs / links inlined.
- `web_click` and `web_type` treat the `id` argument as a CSS selector
  (best-effort — there is no AX-tree element pool to short-id into).
- For the model: prefer `web_execute_js` for anything more complex, since
  the extension transparently falls back from `executeScript` to `chrome.
  debugger` (CDP) on CSP-restricted pages.

## Why three modes?

| Mode      | Login state    | Setup cost        | Code path       |
|-----------|----------------|-------------------|------------------|
| attach    | yes (user's)   | relaunch Chrome   | CDP (direct WS)  |
| managed   | no (fresh)     | none              | CDP (direct WS)  |
| bridge    | yes (user's)   | install extension | WS to extension  |

The bridge route is most useful when you refuse to relaunch Chrome but
still want to operate on logged-in tabs.
