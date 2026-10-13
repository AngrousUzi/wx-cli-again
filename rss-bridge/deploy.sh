#!/usr/bin/env bash
# Deploy this wx-cli checkout on the Linux bridge host.
#
# The checkout is the only source of truth. The systemd unit, the GNOME
# autostart entries and wx-login-tap.py are symlinks into it, every `wx` on
# PATH resolves to ~/.cargo/bin/wx, and this script rebuilds and restarts
# whatever still runs old code. It also installs itself as the checkout's
# post-merge hook, so `git pull` deploys (skip once: WX_NO_DEPLOY=1 git pull).
#
#   rss-bridge/deploy.sh            deploy: build, link, restart what is stale
#   rss-bridge/deploy.sh --pull     git pull --ff-only first, then deploy
#   rss-bridge/deploy.sh --check    report drift only, change nothing; exit 1 on drift
#   rss-bridge/deploy.sh --restart  also restart the wx daemon and the bridge if current
set -euo pipefail
# when run from the git hook, git's variables would point cargo/git at the wrong place
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE

REPO=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)
BRIDGE="$REPO/rss-bridge"
BIN="$HOME/.cargo/bin/wx"
UNIT=wx-rss-bridge.service
STATE_DIR="$HOME/.local/state/wx-cli"
BACKUP_DIR="$STATE_DIR/replaced"
BUILD_STAMP="$STATE_DIR/wx-build.stamp"
HOOK_MARK="wx-cli deploy hook"

# runtime path|file in the checkout
LINKS=(
  "$HOME/.config/systemd/user/$UNIT|$BRIDGE/$UNIT"
  "$HOME/.config/autostart/wechat.desktop|$BRIDGE/autostart/wechat.desktop"
  "$HOME/.config/autostart/wx-login-tap.desktop|$BRIDGE/autostart/wx-login-tap.desktop"
  "$HOME/.local/bin/wx-login-tap.py|$BRIDGE/autostart/wx-login-tap.py"
)
# other places a shell may find `wx`; each must resolve to $BIN
WX_ALIASES=("$HOME/.local/bin/wx" /usr/local/bin/wx)

mode=deploy pull=0 force_restart=0
for arg in "$@"; do
  case $arg in
    --check) mode=check ;;
    --pull) pull=1 ;;
    --restart) force_restart=1 ;;
    -h|--help) sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

drift=0
section() { printf '\n[%s]\n' "$1"; }
ok()      { printf '  ok     %s\n' "$*"; }
stale()   { printf '  STALE  %s\n' "$*"; drift=1; }
fixed()   { printf '  fixed  %s\n' "$*"; }
note()    { printf '  note   %s\n' "$*"; }
short()   { printf '%s' "${1/#"$HOME"/"~"}"; }
same_file() { [ -e "$1" ] && [ -e "$2" ] && [ "$(readlink -f "$1")" = "$(readlink -f "$2")" ]; }
backup() {
  mkdir -p "$BACKUP_DIR"
  mv "$1" "$BACKUP_DIR/$(basename "$1").$(date +%Y%m%d-%H%M%S)"
}

git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 || { echo "$REPO is not a git checkout" >&2; exit 1; }

# cargo is only on PATH where ~/.cargo/env has been sourced
if [ -f "$HOME/.cargo/env" ]; then . "$HOME/.cargo/env"; fi

if [ $mode = deploy ]; then
  mkdir -p "$STATE_DIR"
  exec 9>"$STATE_DIR/deploy.lock"
  flock -w 900 9 || { echo "another deploy is still running" >&2; exit 1; }
fi

if [ $pull = 1 ] && [ $mode = deploy ]; then
  section pull
  WX_NO_DEPLOY=1 git -C "$REPO" pull --ff-only
  args=()
  [ $force_restart = 1 ] && args+=(--restart)
  exec 9>&-
  exec "$BRIDGE/deploy.sh" "${args[@]}"   # the pull may have changed this script
fi

printf 'checkout %s (%s)\n' "$REPO" "$(git -C "$REPO" log -1 --format='%h %s')"

# ── cargo on PATH in every shell ──────────────────────────────────────────────
# ~/.bashrc: above Ubuntu's "not interactive -> return" guard, so non-interactive
# shells (ssh host cmd, coding agents' shell snapshots) get it too.
# ~/.profile: login shells and the GNOME session.
section "cargo on PATH"
command -v cargo >/dev/null || { echo "cargo not found even after sourcing ~/.cargo/env; install rustup" >&2; exit 1; }
bashrc="$HOME/.bashrc"
guard=$(grep -n -m1 -E '^[[:space:]]*\*\)[[:space:]]*return;;|^[[:space:]]*\[ -z "\$PS1" \] && return' "$bashrc" 2>/dev/null | cut -d: -f1 || true)
if awk -v g="${guard:-0}" '(g == 0 || NR < g) && /\.cargo\/env/ { f = 1 } END { exit !f }' "$bashrc" 2>/dev/null; then
  ok "~/.bashrc sources ~/.cargo/env before the interactive guard"
elif [ $mode = check ]; then
  stale "~/.bashrc: non-interactive bash has no cargo"
else
  touch "$bashrc"
  tmp=$(mktemp)
  { printf '%s\n' \
      '# cargo on PATH for every bash, non-interactive ones too (ssh host cmd, agents);' \
      '# keep this above the interactive-only return below. Added by wx-cli rss-bridge/deploy.sh.' \
      'if [ -f "$HOME/.cargo/env" ]; then . "$HOME/.cargo/env"; fi' ''
    cat "$bashrc"; } >"$tmp"
  cat "$tmp" >"$bashrc" && rm -f "$tmp"
  fixed "~/.bashrc now sources ~/.cargo/env at the top"
fi
if grep -q '\.cargo/env' "$HOME/.profile" 2>/dev/null; then
  ok "~/.profile sources ~/.cargo/env"
elif [ $mode = check ]; then
  stale "~/.profile: login shells have no cargo"
else
  printf '\n%s\n%s\n' '# cargo (added by wx-cli rss-bridge/deploy.sh)' \
    'if [ -f "$HOME/.cargo/env" ]; then . "$HOME/.cargo/env"; fi' >>"$HOME/.profile"
  fixed "~/.profile now sources ~/.cargo/env (applies from the next login)"
fi

# ── wx binary ─────────────────────────────────────────────────────────────────
section "wx binary"
bin_changed=0
ref="$BUILD_STAMP"; [ -e "$ref" ] || ref="$BIN"
newer=""
[ -e "$ref" ] && newer=$(find "$REPO/src" "$REPO/Cargo.toml" "$REPO/Cargo.lock" -newer "$ref" -print -quit 2>/dev/null || true)
if [ $mode = check ]; then
  if [ ! -e "$BIN" ]; then stale "$BIN missing"
  elif [ -n "$newer" ]; then stale "$BIN is older than ${newer#"$REPO"/} (needs a rebuild)"
  else ok "$BIN is built from the current sources ($("$BIN" --version))"; fi
else
  cargo build --release --locked --manifest-path "$REPO/Cargo.toml"
  if cmp -s "$REPO/target/release/wx" "$BIN"; then
    ok "$BIN unchanged"
  else
    mkdir -p "$(dirname "$BIN")"
    install -m 755 "$REPO/target/release/wx" "$BIN.new.$$"
    mv -f "$BIN.new.$$" "$BIN"   # rename: a running daemon keeps its old inode
    bin_changed=1
    fixed "installed $BIN ($("$BIN" --version))"
  fi
  touch "$BUILD_STAMP"
fi
for alias in "${WX_ALIASES[@]}"; do
  if [ ! -e "$alias" ] && [ ! -L "$alias" ]; then continue; fi
  if same_file "$alias" "$BIN"; then ok "$alias -> $BIN"; continue; fi
  what="$alias is a separate copy ($("$alias" --version 2>/dev/null || echo unknown version)), not $BIN"
  if [ $mode = check ]; then
    stale "$what"
  elif [ -w "$(dirname "$alias")" ]; then
    backup "$alias" && ln -s "$BIN" "$alias"
    fixed "$alias -> $BIN (old copy moved to $BACKUP_DIR)"
  else
    stale "$what; fix with: sudo ln -sfn $BIN $alias"
  fi
done

# ── runtime files are symlinks into the checkout ─────────────────────────────
section "runtime files"
for pair in "${LINKS[@]}"; do
  dst=${pair%%|*} src=${pair#*|}
  if [ -L "$dst" ] && same_file "$dst" "$src"; then ok "$(short "$dst") -> checkout"; continue; fi
  if [ $mode = check ]; then
    if [ ! -e "$dst" ]; then stale "$(short "$dst") missing"
    elif cmp -s "$dst" "$src"; then stale "$(short "$dst") is a copy (same today, will not follow git pull)"
    else stale "$(short "$dst") is a copy that differs from the checkout"; fi
    continue
  fi
  mkdir -p "$(dirname "$dst")"
  if [ -e "$dst" ] || [ -L "$dst" ]; then
    if cmp -s "$dst" "$src"; then rm -f "$dst"
    else backup "$dst"; note "$(short "$dst") differed from the checkout; old copy moved to $BACKUP_DIR"; fi
  fi
  ln -s "$src" "$dst"
  fixed "$(short "$dst") -> ${src#"$REPO"/}"
done

# ── wx daemon ─────────────────────────────────────────────────────────────────
section "wx daemon"
daemon_pid=$(sed -n 's/.*"pid": *\([0-9][0-9]*\).*/\1/p' "$HOME/.wx-cli/daemon.pid" 2>/dev/null || true)
daemon_exe=""
[ -n "$daemon_pid" ] && daemon_exe=$(readlink "/proc/$daemon_pid/exe" 2>/dev/null || true)
daemon_stale=0
if [ -z "$daemon_exe" ]; then
  ok "not running (the next wx call starts it from $BIN)"
elif [ "$daemon_exe" != "$(readlink -f "$BIN")" ]; then
  daemon_stale=1
  [ $mode = check ] && stale "PID $daemon_pid runs $daemon_exe, not the current $BIN"
else
  ok "PID $daemon_pid runs the current $BIN"
fi
if [ $mode = deploy ]; then
  if [ -n "$daemon_exe" ] && { [ $daemon_stale = 1 ] || [ $bin_changed = 1 ] || [ $force_restart = 1 ]; }; then
    "$BIN" daemon stop && fixed "stopped PID $daemon_pid ($daemon_exe)"
  fi
  # starts the daemon from $BIN if needed and proves the deployed wx can read WeChat
  if out=$(timeout 120 "$BIN" biz-articles --json -n 1 2>&1 >/dev/null); then
    ok "wx biz-articles answers ($("$BIN" daemon status | head -1))"
  else
    note "wx biz-articles failed; WeChat logged in? keys? $(printf '%s' "$out" | tail -1)"
  fi
fi

# ── bridge service ────────────────────────────────────────────────────────────
section "$UNIT"
if [ "$(systemctl --user show -p NeedDaemonReload --value "$UNIT")" = yes ]; then
  if [ $mode = check ]; then stale "unit file changed on disk, systemd still has the old one"
  else systemctl --user daemon-reload && fixed "systemctl --user daemon-reload"; fi
fi
if [ $mode = deploy ]; then
  systemctl --user is-enabled -q "$UNIT" 2>/dev/null || { systemctl --user enable "$UNIT" && fixed "enabled"; }
elif ! systemctl --user is-enabled -q "$UNIT" 2>/dev/null; then
  stale "not enabled (will not start at boot)"
fi
[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" = yes ] \
  || stale "linger is off (the bridge stops at logout); fix with: loginctl enable-linger $USER"
main_pid=$(systemctl --user show -p MainPID --value "$UNIT")
bridge_stale=0
if [ "${main_pid:-0}" -gt 0 ]; then
  started=$(( $(date +%s) - $(ps -o etimes= -p "$main_pid" | tr -d ' ') ))
  changed=$({ stat -L -c '%Y %n' "$BRIDGE/bridge.py" "$BRIDGE/$UNIT" "$BRIDGE/.env" 2>/dev/null || true; } | sort -n | tail -1)
  if [ "${changed%% *}" -gt "$started" ]; then
    bridge_stale=1
    [ $mode = check ] && stale "running since $(date -d "@$started" '+%F %T'), before ${changed#* } changed"
  else
    ok "PID $main_pid started after its last code/config change"
  fi
else
  bridge_stale=1
  [ $mode = check ] && stale "not running"
fi
if [ $mode = deploy ] && { [ $bridge_stale = 1 ] || [ $force_restart = 1 ]; }; then
  systemctl --user restart "$UNIT" && fixed "restarted"
fi

# ── git hook: pull = deploy ───────────────────────────────────────────────────
section "post-merge hook"
hook=$(cd "$REPO" && readlink -f "$(git rev-parse --git-path hooks/post-merge)")
if grep -qs "$HOOK_MARK" "$hook"; then
  ok "git pull in $REPO runs this script"
elif [ -e "$hook" ]; then
  stale "$hook exists and is not ours; add a call to rss-bridge/deploy.sh to it by hand"
elif [ $mode = check ]; then
  stale "missing: git pull does not deploy"
else
  mkdir -p "$(dirname "$hook")"
  cat >"$hook" <<EOF
#!/bin/sh
# $HOOK_MARK (installed by rss-bridge/deploy.sh): redeploy after every pull
# or merge into this checkout. Skip once with: WX_NO_DEPLOY=1 git pull
[ -n "\$WX_NO_DEPLOY" ] && exit 0
exec "\$(git rev-parse --show-toplevel)/rss-bridge/deploy.sh"
EOF
  chmod +x "$hook"
  fixed "installed $hook"
fi

echo
if [ $mode = check ]; then
  [ $drift = 0 ] && echo "no drift" || echo "drift found; run rss-bridge/deploy.sh"
  exit $drift
fi
[ $drift = 0 ] && echo "deployed" || echo "deployed, with items above that need a manual step"
