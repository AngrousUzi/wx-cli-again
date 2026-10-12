# rss-bridge

A small Python daemon (standard library only, Python 3.9+) that feeds WeChat
official-account articles from `wx biz-articles` into the Obsidian reader's RSS
view. It only opens connections; it never listens.

## Flow

```
reader "Refresh"
   -> Obsidian service rings the bridge on the SSE stream
        GET /api/feeds/events?source=wechat   (held open by the bridge)
        frame data: since, channels (JSON array string), roster ("1", optional),
                   cycle (opaque id, echoed back)
   -> bridge cycle:
        1. window [since, now] in 30-day slices, newest first:
           wx biz-articles --json -n 200000 --since "<local time>" [--until "<local time>"]
           (a window of 30 days or less is exactly one call, without --until)
        2. per slice: merge its accounts into the roster (distinct account_username,
           name + latest_at of the newest article), keep only the subscribed
           channels' articles, drop the raw slice; one slice is in memory at a time
        3. items are capped at 20000 per cycle (the server answers 413 beyond that and
           drops the cycle): the newest are kept, a warning says how many days are
           covered, and the remaining slices are still read for the roster
        4. roster set: continue below `since` down to ROSTER_LOOKBACK_DAYS in the same
           30-day slices, keeping only distinct accounts
        5. POST /api/feeds/push: seq 0, 1, 2, ... (<= 500 items, <= 1000 channels each),
           the last one final=true, even with no items
   <- service hands the final result to the waiting reader request
```

Every push body is `{source, since, seq, final, cycle?, truncated?, channels:
[{channel_id, channel, latest_at}], items}`; `latest_at` is RFC3339 UTC. `cycle`
is the frame's `cycle` id, echoed in every push of the cycle that answers it
(absent when the frame had none, and with `--once`). `truncated: true` appears on
the final push only, when the 20000-item cap dropped older items. A frame without a usable
`since` uses `FEED_LOOKBACK_DAYS`; a frame without usable `channels` means none
(roster only, zero items).

Rings that arrive while a cycle is running collapse into one more cycle that
uses the newest frame's parameters; a pending `roster` request survives a later
frame that lacks it; the cycle answers the newest frame's `cycle` id. If `wx` fails, nothing is pushed and the server reports a
snapshot. If a push gets HTTP 409 `out_of_sequence`, the cycle is aborted and
logged (the chunk is not retried); the next frame starts a new cycle at seq 0.

## Setup

1. Mint a service key with scope `obsidian:feed` at `https://xxxxxxyz.xyz/auth/account.html`.
2. Configure:

   ```
   cd ~/wx-cli/rss-bridge
   cp .env.example .env
   $EDITOR .env        # set OBSIDIAN_URL and OBSIDIAN_SERVICE_KEY
   ```

3. Make sure `wx` works on this machine (`wx biz-articles --json -n 1`). If it
   is not on PATH, set `WX_BIN` in `.env`.
4. Install the systemd user unit (expects the repo at `~/wx-cli`):

   ```
   mkdir -p ~/.config/systemd/user
   cp wx-rss-bridge.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now wx-rss-bridge
   loginctl enable-linger "$USER"     # keep it running without a login session
   journalctl --user -u wx-rss-bridge -f
   ```

## One-shot modes

```
python bridge.py --once [--since-days N] [--channels id1,id2] [--roster]     # one cycle, exit 1 on failure
python bridge.py --dry-run [--since-days N] [--channels id1,id2] [--roster]  # no push; print push counts, roster size, samples
```

`--since-days` defaults to `FEED_LOOKBACK_DAYS`. `--channels` is the subscribed
set (default none, so the cycle only reports the roster). `--roster` adds the
`ROSTER_LOOKBACK_DAYS` back scan. Tests: `python -m unittest -v`.

## Configuration

See `.env.example`. `AUTH_TOKEN_URL` unset uses the default token endpoint;
set empty (`AUTH_TOKEN_URL=`) to send no `Authorization` header (local
auth-off service).

## Troubleshooting

- **401**: the service key is wrong, revoked or expired. The bridge invalidates
  the token and retries once, then backs off. Mint a new key.
- **403 (scope)**: the key lacks `obsidian:feed`. Mint a key with that scope.
- **403 with body `error code: 1010`**: Cloudflare blocked the User-Agent. The
  bridge sends `wx-rss-bridge/1.0`; check for a proxy rewriting it.
- **`wx binary not found`**: install `wx` or set `WX_BIN` to its full path. Under
  systemd the PATH is minimal, so a full path is the safe choice.
- **`wx exited with code ...`**: the log shows the first 500 characters of
  stderr. The usual cause is the wx daemon not being initialized (keys not
  extracted). Run `wx init` as the same user and retry `wx biz-articles --json -n 1`.
- **`wx timed out`**: raise `WX_TIMEOUT_SECONDS`, or lower `WX_LIMIT` / `ROSTER_LOOKBACK_DAYS`.
- **`equal to WX_LIMIT ... truncated`**: the result hit the limit, so older
  articles in the window were cut off. Raise `WX_LIMIT` or ask for a shorter window.
- **`cycle aborted ... out_of_sequence`**: the server restarted or another bridge
  pushed in between. Harmless; the next refresh starts over.
- **Nothing happens on refresh**: check the log for `subscribed to feed events`.
  Only one connection per source is kept; a second bridge replaces the first.

## Keeping WeChat itself alive across reboots (Linux host)

The bridge survives a reboot (systemd user unit + linger), but new articles only
arrive while the WeChat client is running and logged in. On the Ubuntu 24.04 /
GNOME host the deb has no autostart setting, and after launch it still waits on
the 进入微信 button even with auto-login approved on the phone. `autostart/`
holds the pieces that close that gap:

- `wechat.desktop` → `~/.config/autostart/`: launches WeChat with the session
  (GDM auto-login is already on for the user).
- `wx-login-tap.desktop` → `~/.config/autostart/`: 10 s later runs
  `~/.local/bin/wx-login-tap.py --max-wait 240`.
- `wx-login-tap.py` → `~/.local/bin/`: screenshots every 3 s (gnome-screenshot),
  waits for button green at (150, 320), then steers a virtual relative mouse
  (uinput, via passwordless `sudo -n`) onto the button center (205, 327) in a
  closed loop (cursor found by diffing screenshots with/without pointer) and
  clicks. Needs `python3-evdev`, `python3-pil`, `gnome-screenshot`.

Why a mouse and a closed loop: mutter on this session drops uinput touchscreens
and keyboards (libinput sees them, the shell never reacts) and only relative
mice get through, with pointer acceleration applied. Verified 2026-10-11: quit +
relaunch logs in with one click. The probe/target assume the 1920x1080 panel
and the login window in the top-left corner; pass `--probe X Y --target X Y`
otherwise. It does nothing while the screen is locked.
