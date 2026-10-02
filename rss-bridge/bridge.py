"""bridge.py - wx-cli RSS bridge daemon
======================================
Holds one SSE connection open to the Obsidian service
(`GET {OBSIDIAN_URL}/api/feeds/events?source=wechat`). When the service rings
(an `obsidian-feed-refresh` frame carrying `since`, `channels` and optionally
`roster`), the bridge runs `wx biz-articles --json`, builds the account roster,
maps the articles of the subscribed channels to generic feed items and POSTs
them in sequenced chunks to `{OBSIDIAN_URL}/api/feeds/push`.

The bridge only opens connections and never listens. Standard library only.
Contract: the "Bridge <-> server" section of the obsidian feeds v2 contract.

    python bridge.py                        run the daemon
    python bridge.py --once [--since-days N] [--channels a,b] [--roster]
                                            one cycle, exit code 1 on failure
    python bridge.py --dry-run [--since-days N] [--channels a,b] [--roster]
                                            fetch + map, print, no push
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, quote, urlsplit

log = logging.getLogger("wx-rss-bridge")

USER_AGENT = "wx-rss-bridge/1.0"  # Cloudflare rejects the default urllib UA (error 1010)
DEFAULT_TOKEN_URL = "https://xxxxxxyz.xyz/auth/token"

PUSH_TIMEOUT = 30  # seconds, per push request
TOKEN_TIMEOUT = 10  # seconds
STREAM_READ_TIMEOUT = 60  # seconds; the server pings every 25 s, so 60 s of silence is dead
TOKEN_SAFETY_MARGIN = 60  # seconds before expiry at which the token is refreshed
BACKOFF_INITIAL = 1.0
BACKOFF_MAX = 300.0
CHUNK_SIZE = 500  # items per push request (server cap is 1000)
CHANNEL_CHUNK_SIZE = 1000  # channels per push request (server cap is 2000)
MAX_CYCLE_ITEMS = 20000  # items per cycle; the server answers 413 and drops the cycle beyond this
ROSTER_SLICE_DAYS = 30  # width of one wx fetch slice (window and roster scan), bounds memory to one slice

REFRESH_FRAME_TYPE = "obsidian-feed-refresh"

# Server-side field limits (contract, POST /api/feeds/push).
MAX_GUID = 200
MAX_TITLE = 1000
MAX_CHANNEL = 200
MAX_SUMMARY = 2000
MAX_URL = 4000


# -- .env loader ------------------------------------------------------------

def parse_env_text(text: str) -> Dict[str, str]:
    """Parse KEY=VALUE lines. Blank lines and `#` comments are skipped, an
    `export ` prefix is tolerated, values may be wrapped in single or double
    quotes, and an unquoted value loses a trailing ` # comment`. `KEY=` yields
    the empty string (kept distinct from an unset key)."""
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            idx = value.find(" #")
            if idx != -1:
                value = value[:idx].rstrip()
        out[key] = value
    return out


def load_env_file(path: str, environ=None) -> None:
    """Load `path` into the environment without overriding existing variables.
    A variable that exists with an empty value counts as set, so an explicitly
    empty AUTH_TOKEN_URL from the real environment survives. Missing file is
    not an error."""
    env = os.environ if environ is None else environ
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return
    for key, value in parse_env_text(text).items():
        if key not in env:
            env[key] = value


@dataclass
class Config:
    obsidian_url: str = ""
    service_key: str = ""
    auth_token_url: str = DEFAULT_TOKEN_URL
    source: str = "wechat"
    wx_bin: str = "wx"
    wx_limit: int = 200000
    lookback_days: float = 7
    roster_lookback_days: float = 365
    wx_timeout: float = 120

    @classmethod
    def from_env(cls, environ=None) -> "Config":
        env = os.environ if environ is None else environ

        def num(name: str, default, cast):
            raw = (env.get(name) or "").strip()
            if not raw:
                return default
            try:
                return cast(raw)
            except ValueError:
                raise SystemExit("invalid %s=%r" % (name, raw))

        return cls(
            obsidian_url=env.get("OBSIDIAN_URL", "").strip().rstrip("/"),
            service_key=env.get("OBSIDIAN_SERVICE_KEY", "").strip(),
            # Only a completely unset variable falls back to the default; an
            # explicit empty value means "send no Authorization header".
            auth_token_url=env.get("AUTH_TOKEN_URL", DEFAULT_TOKEN_URL).strip(),
            source=(env.get("FEED_SOURCE") or "wechat").strip(),
            wx_bin=(env.get("WX_BIN") or "wx").strip(),
            wx_limit=num("WX_LIMIT", 200000, int),
            lookback_days=num("FEED_LOOKBACK_DAYS", 7, float),
            roster_lookback_days=num("ROSTER_LOOKBACK_DAYS", 365, float),
            wx_timeout=num("WX_TIMEOUT_SECONDS", 120, float),
        )

    def problems(self) -> List[str]:
        out = []
        if not self.obsidian_url:
            out.append("OBSIDIAN_URL is not set")
        if self.auth_token_url and not self.service_key:
            out.append("OBSIDIAN_SERVICE_KEY is not set (or set AUTH_TOKEN_URL= empty for an auth-off server)")
        return out


# -- SSE frame parser -------------------------------------------------------

class FrameParser:
    """Incremental SSE parser. Feed arbitrary text chunks; returns completed
    frames as (event, data). Lines starting with `:` are comments (": connected",
    ": ping"), `data:` lines are joined with a newline, a blank line ends a
    frame, `event` defaults to "message", and a trailing CR is stripped."""

    def __init__(self) -> None:
        self._buf = ""
        self._event: Optional[str] = None
        self._data_lines: List[str] = []

    def feed(self, chunk: str) -> List[Tuple[str, str]]:
        frames: List[Tuple[str, str]] = []
        self._buf += chunk
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.endswith("\r"):
                line = line[:-1]
            frame = self._consume_line(line)
            if frame is not None:
                frames.append(frame)
        return frames

    def _consume_line(self, line: str) -> Optional[Tuple[str, str]]:
        if line == "":
            if self._event is None and not self._data_lines:
                return None
            frame = (self._event or "message", "\n".join(self._data_lines))
            self._event = None
            self._data_lines = []
            return frame
        if line.startswith(":"):
            return None
        if line.startswith("event:"):
            self._event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            self._data_lines.append(line[len("data:"):].strip())
        return None


# -- mapping ------------------------------------------------------------------

def normalize_wechat_url(url: str) -> Optional[Tuple[str, str]]:
    """Return (canonical_url, guid) for a WeChat article URL, or None.

    Accepts http or https URLs on mp.weixin.qq.com with path `/s` and query
    params `__biz`, `mid`, `idx`. The canonical URL is forced to https and keeps
    only __biz, mid, idx, sn, chksm in that order (sn/chksm only when present).
    guid = "<__biz>:<mid>:<idx>"."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    if (parts.hostname or "").lower() != "mp.weixin.qq.com":
        return None
    if parts.path != "/s":
        return None
    params: Dict[str, str] = {}
    for key, value in parse_qsl(parts.query, keep_blank_values=False):
        params.setdefault(key, value)
    biz, mid, idx = params.get("__biz"), params.get("mid"), params.get("idx")
    if not biz or not mid or not idx:
        return None
    pairs = [("__biz", biz), ("mid", mid), ("idx", idx)]
    for extra in ("sn", "chksm"):
        if params.get(extra):
            pairs.append((extra, params[extra]))
    query = "&".join("%s=%s" % (k, quote(v, safe="=")) for k, v in pairs)
    return "https://mp.weixin.qq.com/s?" + query, "%s:%s:%s" % (biz, mid, idx)


def _to_int(value) -> Optional[int]:
    """Unix-seconds field that may be an int, a float or a numeric string."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = int(float(str(value).strip()))
    except (ValueError, OverflowError):
        return None
    return n if n > 0 else None


def rfc3339_local(ts: int) -> str:
    """RFC3339 with the machine's local UTC offset."""
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def map_article(article: dict) -> Optional[dict]:
    """Map one `wx biz-articles --json` entry to a feed item, or None when the
    entry is not a collectable WeChat article (non-WeChat URL, missing title,
    no usable timestamp)."""
    if not isinstance(article, dict):
        return None
    norm = normalize_wechat_url(article.get("url"))
    if norm is None:
        return None
    url, guid = norm
    title = _text(article.get("title"))
    if not title or len(guid) > MAX_GUID or len(url) > MAX_URL:
        return None
    published = _to_int(article.get("timestamp"))
    received = _to_int(article.get("recv_time"))
    if published is None:
        published = received
    if published is None:
        return None
    if received is None:
        received = published
    return {
        "guid": guid,
        "channel_id": _text(article.get("account_username"))[:MAX_CHANNEL],
        "channel": _text(article.get("account"))[:MAX_CHANNEL],
        "title": title[:MAX_TITLE],
        "url": url,
        "summary": _text(article.get("digest"))[:MAX_SUMMARY],
        "image_url": _text(article.get("cover_url"))[:MAX_URL],
        "published_at": rfc3339_local(published),
        "received_at": rfc3339_local(received),
    }


def map_articles(articles: List[dict]) -> List[dict]:
    """Map and de-duplicate by guid (the server upserts, and one statement must
    not touch the same row twice). First occurrence wins."""
    seen = set()
    items: List[dict] = []
    for article in articles:
        item = map_article(article)
        if item is None or item["guid"] in seen:
            continue
        seen.add(item["guid"])
        items.append(item)
    return items


def rfc3339_utc(ts: int) -> str:
    """RFC3339 in UTC with a `Z` suffix."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class RefreshRequest:
    """One cycle's parameters, as carried by a refresh frame."""
    since: int
    channels: Tuple[str, ...] = ()
    roster: bool = False
    cycle: str = ""  # opaque id of the frame, echoed in every push ("" = none)


def parse_refresh_frame(info: dict, now: float, lookback_days: float) -> RefreshRequest:
    """Frame `data` -> RefreshRequest. A missing or unparseable `since` falls
    back to now - lookback_days; `channels` is a JSON array encoded as a string
    (a real list is tolerated) and anything else means no channels; `roster` is
    true only for "1"/"true"; `cycle` is an opaque string ("" when absent)."""
    since = _to_int(info.get("since")) or int(now - lookback_days * 86400)
    raw = info.get("channels")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    channels: List[str] = []
    if isinstance(raw, list):
        for c in raw:
            if isinstance(c, str) and c.strip() and c.strip() not in channels:
                channels.append(c.strip())
    roster = str(info.get("roster") or "").strip().lower() in ("1", "true")
    cycle = info.get("cycle")
    cycle = cycle.strip() if isinstance(cycle, str) else ""
    return RefreshRequest(since, tuple(channels), roster, cycle)


def merge_requests(pending: Optional[RefreshRequest], new: RefreshRequest) -> RefreshRequest:
    """Coalesce two refresh requests: the newest frame's parameters win, except
    that a pending roster request is never dropped. The cycle id is always the
    newest frame's."""
    if pending is not None and pending.roster and not new.roster:
        return RefreshRequest(new.since, new.channels, True, new.cycle)
    return new


def article_time(article: dict) -> Optional[int]:
    """Best timestamp of a raw article: publish time, else receive time."""
    return _to_int(article.get("timestamp")) or _to_int(article.get("recv_time"))


def merge_roster(roster: Dict[str, dict], articles: Iterable[dict]) -> None:
    """Fold raw articles into `roster` ({channel_id: {channel_id, channel,
    latest}}). The display name comes from the newest article of the account
    that has one; `latest` is the newest article time (0 when unknown)."""
    for article in articles:
        if not isinstance(article, dict):
            continue
        cid = _text(article.get("account_username"))[:MAX_CHANNEL]
        if not cid:
            continue
        ts = article_time(article) or 0
        name = _text(article.get("account"))[:MAX_CHANNEL]
        entry = roster.get(cid)
        if entry is None:
            roster[cid] = {"channel_id": cid, "channel": name, "latest": ts}
        elif ts > entry["latest"]:
            entry["latest"] = ts
            if name:
                entry["channel"] = name
        elif not entry["channel"] and name:
            entry["channel"] = name


def roster_channels(roster: Dict[str, dict]) -> List[dict]:
    """Roster -> wire `channels` list, newest account first, ties by id."""
    out = []
    for e in sorted(roster.values(), key=lambda e: (-e["latest"], e["channel_id"])):
        c = {"channel_id": e["channel_id"], "channel": e["channel"]}
        if e["latest"]:
            c["latest_at"] = rfc3339_utc(e["latest"])
        out.append(c)
    return out


def roster_slices(since: int, now: float, lookback_days: float,
                  slice_days: int = ROSTER_SLICE_DAYS) -> List[Tuple[int, int]]:
    """Inclusive (lo, hi) unix-second ranges, newest first, covering
    [now - lookback_days, since - 1] with no gap and no overlap. `wx --since` and
    `--until` are both inclusive, so each slice ends one second before the
    newer one starts. Empty when the window already reaches the floor."""
    return _slices(int(now - lookback_days * 86400), since - 1, slice_days)


def _slices(floor: int, top: int, slice_days: int = ROSTER_SLICE_DAYS) -> List[Tuple[int, int]]:
    """Inclusive (lo, hi) ranges of at most `slice_days`, newest first, covering
    [floor, top] exactly; each slice ends one second before the newer one starts."""
    width = int(slice_days * 86400)
    out: List[Tuple[int, int]] = []
    hi = top
    while hi >= floor:
        lo = max(hi - width + 1, floor)
        out.append((lo, hi))
        hi = lo - 1
    return out


def window_slices(since: int, now: float,
                  slice_days: int = ROSTER_SLICE_DAYS) -> List[Tuple[int, Optional[int]]]:
    """Slices of the window [since, now], newest first. The newest slice has no
    upper bound (`hi` None), so a window of `slice_days` or less is exactly one
    unbounded wx call."""
    if int(now) - since <= int(slice_days * 86400):
        return [(since, None)]
    out: List[Tuple[int, Optional[int]]] = list(_slices(since, int(now), slice_days))
    out[0] = (out[0][0], None)
    return out


def filter_subscribed(articles: List[dict], channels: Iterable[str]) -> List[dict]:
    """Keep only articles whose `account_username` is a subscribed channel."""
    wanted = set(channels)
    return [a for a in articles
            if isinstance(a, dict) and _text(a.get("account_username")) in wanted]


def build_pushes(source: str, since: int, items: List[dict], channels: List[dict],
                 item_size: int = CHUNK_SIZE,
                 channel_size: int = CHANNEL_CHUNK_SIZE,
                 cycle: str = "", truncated: bool = False) -> List[dict]:
    """Split a cycle into push bodies with seq 0, 1, 2, ...; only the last has
    final=true. Items and channels are chunked independently and spread over as
    many pushes as the larger needs; zero items and zero channels still yield
    one empty final push. A non-empty `cycle` is echoed in every push;
    `truncated` (items dropped at the cap) is set on the final push only."""
    n = max(1, -(-len(items) // item_size), -(-len(channels) // channel_size))
    pushes = []
    for i in range(n):
        body = {
            "source": source, "since": since, "seq": i, "final": i == n - 1,
            "channels": channels[i * channel_size:(i + 1) * channel_size],
            "items": items[i * item_size:(i + 1) * item_size],
        }
        if cycle:
            body["cycle"] = cycle
        if truncated and body["final"]:
            body["truncated"] = True
        pushes.append(body)
    return pushes


def format_since(since_unix: int) -> str:
    """Unix seconds -> local `YYYY-MM-DD HH:MM:SS`, the format `wx --since` takes."""
    return datetime.fromtimestamp(since_unix).strftime("%Y-%m-%d %H:%M:%S")


# -- wx CLI -------------------------------------------------------------------

class FetchError(Exception):
    pass


def fetch_articles(cfg: Config, since_unix: int, until_unix: Optional[int] = None) -> List[dict]:
    """Run `wx biz-articles --json` for [since, until] (both inclusive, `until`
    optional) and return the raw article dicts. Raises FetchError on any
    failure (missing binary, non-zero exit, timeout, bad output). Logs a
    warning when the result length equals WX_LIMIT (the window was truncated)."""
    cmd = [cfg.wx_bin, "biz-articles", "--json", "-n", str(cfg.wx_limit),
           "--since", format_since(since_unix)]
    if until_unix is not None:
        cmd += ["--until", format_since(until_unix)]
    log.debug("running %s", cmd)
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=cfg.wx_timeout)
    except FileNotFoundError:
        raise FetchError("wx binary not found: %r (set WX_BIN or put it on PATH)" % cfg.wx_bin)
    except subprocess.TimeoutExpired:
        raise FetchError("wx timed out after %ss" % cfg.wx_timeout)
    except OSError as e:
        raise FetchError("cannot run wx: %s" % e)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()[:500]
        raise FetchError("wx exited with code %s: %s" % (proc.returncode, err))
    try:
        data = json.loads(proc.stdout.decode("utf-8").lstrip("﻿"))
    except (UnicodeDecodeError, ValueError) as e:
        err = proc.stderr.decode("utf-8", errors="replace").strip()[:500]
        raise FetchError("wx output is not valid JSON (%s); stderr: %s" % (e, err))
    if isinstance(data, dict) and isinstance(data.get("articles"), list):
        data = data["articles"]  # tolerate the daemon's {count, articles} envelope
    if not isinstance(data, list):
        raise FetchError("wx output is not a JSON array")
    if len(data) >= cfg.wx_limit:
        log.warning("wx returned %d articles, equal to WX_LIMIT=%d: the window was truncated "
                    "(raise WX_LIMIT or narrow the window)", len(data), cfg.wx_limit)
    return data


# -- HTTP clients -------------------------------------------------------------

class PushError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class TokenSource:
    """Exchanges the service key for a short-lived bearer token and caches it
    until 60 s before expiry. An empty `token_url` disables auth: get() returns
    None and no Authorization header is sent."""

    def __init__(self, token_url: str, service_key: str):
        self.token_url = token_url
        self.service_key = service_key
        self._token: Optional[str] = None
        self._expiry = 0.0
        self._lock = threading.Lock()

    def get(self) -> Optional[str]:
        if not self.token_url:
            return None
        with self._lock:
            now = time.time()
            if self._token and now < self._expiry - TOKEN_SAFETY_MARGIN:
                return self._token
            req = urllib.request.Request(
                self.token_url, data=b"", method="POST",
                headers={"Authorization": "Bearer " + self.service_key, "User-Agent": USER_AGENT},
            )
            try:
                with urllib.request.urlopen(req, timeout=TOKEN_TIMEOUT) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read(200).decode("utf-8", errors="replace")
                raise PushError("token exchange failed: HTTP %s %s" % (e.code, body), e.code)
            except Exception as e:
                raise PushError("token exchange failed: %s" % e)
            token = data.get("access_token") if isinstance(data, dict) else None
            if not token:
                raise PushError("token exchange response has no access_token")
            try:
                expires_in = float(data.get("expires_in") or 0)
            except (TypeError, ValueError):
                expires_in = 0.0
            self._token = token
            self._expiry = now + expires_in
            return token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expiry = 0.0


class OutOfSequence(PushError):
    """HTTP 409 out_of_sequence: the server lost track of the cycle. Not retried."""


class ObsidianClient:
    """Pushes feed items to the Obsidian service."""

    def __init__(self, cfg: Config, tokens: TokenSource):
        self.cfg = cfg
        self.tokens = tokens

    def _headers(self, extra: Optional[dict] = None) -> dict:
        headers = {"User-Agent": USER_AGENT}
        token = self.tokens.get()
        if token:
            headers["Authorization"] = "Bearer " + token
        if extra:
            headers.update(extra)
        return headers

    def _post_chunk(self, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = self.cfg.obsidian_url + "/api/feeds/push"
        for attempt in (1, 2):
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers=self._headers({"Content-Type": "application/json"}))
            try:
                with urllib.request.urlopen(req, timeout=PUSH_TIMEOUT) as resp:
                    return json.loads(resp.read().decode("utf-8") or "{}")
            except urllib.error.HTTPError as e:
                text = e.read(300).decode("utf-8", errors="replace")
                if e.code == 401 and attempt == 1:
                    log.warning("push got 401, refreshing token and retrying once")
                    self.tokens.invalidate()
                    continue
                if e.code == 409 and "out_of_sequence" in text:
                    raise OutOfSequence("push seq %s rejected: %s" % (payload.get("seq"), text), 409)
                raise PushError("push failed: HTTP %s %s" % (e.code, text), e.code)
            except PushError:
                raise
            except Exception as e:
                raise PushError("push failed: %s" % e)
        raise PushError("push failed")  # unreachable

    def push(self, pushes: List[dict]) -> Tuple[int, int]:
        """Send the push bodies in order. A 409 out_of_sequence raises
        OutOfSequence and the rest of the cycle is not sent. Returns (items,
        channels) received totals."""
        items = channels = 0
        for payload in pushes:
            result = self._post_chunk(payload)
            items += int(result.get("received", 0) or 0)
            channels += int(result.get("channels", 0) or 0)
        return items, channels

    def events_request(self) -> urllib.request.Request:
        url = "%s/api/feeds/events?source=%s" % (self.cfg.obsidian_url, quote(self.cfg.source))
        return urllib.request.Request(url, headers=self._headers({"Accept": "text/event-stream"}))


# -- bridge ---------------------------------------------------------------------

def build_cycle(cfg: Config, req: RefreshRequest, now: Optional[float] = None) -> Tuple[List[dict], int]:
    """Run the wx queries of one cycle and return (push bodies, roster size).
    The window is read in 30-day slices, newest first (a window of 30 days or
    less is one wx call); each slice feeds the roster and its subscribed
    articles are kept (at most MAX_CYCLE_ITEMS, the newest) before the slice is
    dropped. The roster scan (when requested) continues below `since` the same
    way, keeping only distinct accounts. Raises FetchError, in which case
    nothing has been pushed."""
    now = time.time() if now is None else now
    roster: Dict[str, dict] = {}
    items: List[dict] = []
    seen = set()
    dropped = 0
    oldest_kept = 0
    windows = window_slices(req.since, now)
    for i, (lo, hi) in enumerate(windows, 1):
        part = fetch_articles(cfg, lo, hi)
        merge_roster(roster, part)
        mine = filter_subscribed(part, req.channels)
        mine.sort(key=lambda a: article_time(a) or 0, reverse=True)  # newest first, cap keeps the newest
        for a in mine:
            item = map_article(a)
            if item is None or item["guid"] in seen:
                continue
            if len(items) >= MAX_CYCLE_ITEMS:
                dropped += 1
                continue
            seen.add(item["guid"])
            items.append(item)
            oldest_kept = article_time(a) or oldest_kept
        log.debug("window slice %d/%d: %d articles, %d accounts so far, %d items kept",
                  i, len(windows), len(part), len(roster), len(items))
        del part, mine
    if dropped:
        log.warning("cycle capped at %d items, %d older items dropped: items cover only the newest "
                    "%.1f of the %.1f requested days", MAX_CYCLE_ITEMS, dropped,
                    (now - oldest_kept) / 86400, (now - req.since) / 86400)
    scans = roster_slices(req.since, now, cfg.roster_lookback_days) if req.roster else []
    for i, (lo, hi) in enumerate(scans, 1):
        part = fetch_articles(cfg, lo, hi)
        merge_roster(roster, part)
        log.debug("roster slice %d/%d: %d articles, %d accounts so far", i, len(scans), len(part), len(roster))
        del part
    log.info("wx calls: %d window slices, %d roster slices", len(windows), len(scans))
    pushes = build_pushes(cfg.source, req.since, items, roster_channels(roster),
                          cycle=req.cycle, truncated=dropped > 0)
    return pushes, len(roster)


def refresh_once(cfg: Config, client: Optional[ObsidianClient], req: RefreshRequest,
                 dry_run: bool = False) -> List[dict]:
    """One cycle: fetch, map and (unless dry_run) push. Returns the push bodies
    on success. A fetch failure raises FetchError and nothing is pushed, so the
    server reports a snapshot; a push failure raises PushError (OutOfSequence
    aborts the cycle without retrying the chunk)."""
    started = time.monotonic()
    pushes, roster_size = build_cycle(cfg, req)
    n_items = sum(len(p["items"]) for p in pushes)
    if dry_run or client is None:
        log.info("cycle: %d items, %d accounts, %d pushes in %.1fs (dry run)",
                 n_items, roster_size, len(pushes), time.monotonic() - started)
        return pushes
    items, channels = client.push(pushes)
    log.info("refresh: %d items, %d channels in %d pushes (server received %d/%d) in %.1fs",
             n_items, roster_size, len(pushes), items, channels, time.monotonic() - started)
    return pushes


class Bridge:
    """SSE subscribe thread + coalescing refresh worker thread."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.tokens = TokenSource(cfg.auth_token_url, cfg.service_key)
        self.client = ObsidianClient(cfg, self.tokens)
        self.stop_event = threading.Event()
        self._wake = threading.Event()
        self._pending: Optional[RefreshRequest] = None
        self._pending_lock = threading.Lock()
        self._response = None
        self._response_lock = threading.Lock()
        self._consecutive_401 = 0
        self._threads: List[threading.Thread] = []

    # lifecycle
    def start(self) -> None:
        self._threads = [
            threading.Thread(target=self._subscribe_loop, name="subscribe", daemon=True),
            threading.Thread(target=self._refresh_loop, name="refresh", daemon=True),
        ]
        for t in self._threads:
            t.start()
        log.info("bridge started, source=%s, %s", self.cfg.source, self.cfg.obsidian_url)

    def stop(self) -> None:
        self.stop_event.set()
        self._wake.set()
        with self._response_lock:
            if self._response is not None:
                try:
                    self._response.close()
                except Exception:
                    pass
        for t in self._threads:
            t.join(timeout=5)

    # refresh coalescing: any number of rings during a run collapse into one
    # more run, using the newest frame's parameters (a pending roster request
    # is kept).
    def request_refresh(self, req: RefreshRequest) -> None:
        with self._pending_lock:
            self._pending = merge_requests(self._pending, req)
        self._wake.set()

    def _refresh_loop(self) -> None:
        while not self.stop_event.is_set():
            self._wake.wait()
            if self.stop_event.is_set():
                break
            self._wake.clear()
            with self._pending_lock:
                req, self._pending = self._pending, None
            if req is None:
                continue
            try:
                refresh_once(self.cfg, self.client, req)
            except FetchError as e:
                log.error("wx fetch failed, nothing pushed: %s", e)
            except OutOfSequence as e:
                log.error("cycle aborted, the next frame starts a new one: %s", e)
            except PushError as e:
                log.error("%s%s", e, self._hint(e))
            except Exception:
                log.exception("refresh failed")

    @staticmethod
    def _hint(e: PushError) -> str:
        if e.status == 403 and "1010" in str(e):
            return " (Cloudflare 1010: the User-Agent was blocked)"
        if e.status == 403:
            return " (the service key probably lacks scope obsidian:feed)"
        return ""

    # subscribe
    def _subscribe_loop(self) -> None:
        backoff = BACKOFF_INITIAL
        while not self.stop_event.is_set():
            status, got_bytes = self._subscribe_once()
            if self.stop_event.is_set():
                break
            if got_bytes:
                backoff = BACKOFF_INITIAL
            if status == 401:
                self._consecutive_401 += 1
                self.tokens.invalidate()
                if self._consecutive_401 <= 1:
                    log.warning("subscribe got 401, token invalidated, reconnecting now")
                    continue
                log.warning("subscribe got 401 again, retrying in %.0fs (check the service key)", backoff)
            else:
                self._consecutive_401 = 0
                log.warning("subscribe ended (%s), reconnecting in %.0fs", status, backoff)
            self.stop_event.wait(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)

    def _subscribe_once(self) -> Tuple[object, bool]:
        """One connection. Returns (status, got_bytes): status is an HTTP code,
        "connect_error" or "closed"."""
        try:
            req = self.client.events_request()
            response = urllib.request.urlopen(req, timeout=STREAM_READ_TIMEOUT)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return 401, False
            hint = ""
            if e.code == 403:
                body = e.read(200).decode("utf-8", errors="replace")
                hint = " (Cloudflare 1010: User-Agent blocked)" if "1010" in body else \
                    " (service key probably lacks scope obsidian:feed)"
            log.warning("subscribe failed: HTTP %s%s", e.code, hint)
            return e.code, False
        except PushError as e:  # token exchange failure
            log.warning("%s", e)
            return e.status or "token_error", False
        except Exception as e:
            log.warning("subscribe connect error: %s", e)
            return "connect_error", False

        log.info("subscribed to feed events")
        got_bytes = False
        parser = FrameParser()
        with self._response_lock:
            self._response = response
        try:
            while not self.stop_event.is_set():
                try:
                    raw = response.readline()
                except Exception:
                    break  # read stall (>60 s) or closed by stop()
                if not raw:
                    break  # server closed
                got_bytes = True
                for event, data in parser.feed(raw.decode("utf-8", errors="replace")):
                    self._dispatch(event, data)
            return "closed", got_bytes
        finally:
            with self._response_lock:
                self._response = None
            try:
                response.close()
            except Exception:
                pass

    def _dispatch(self, event: str, data: str) -> None:
        if event != "notification" or not data:
            return
        try:
            payload = json.loads(data)
        except ValueError:
            log.warning("frame data is not JSON: %.200s", data)
            return
        if not isinstance(payload, dict):
            return
        info = payload.get("data")
        if not isinstance(info, dict):
            info = {}
        frame_type = info.get("type") or payload.get("notification_type")
        if frame_type != REFRESH_FRAME_TYPE:
            log.info("ignoring frame type %r", frame_type)
            return
        req = parse_refresh_frame(info, time.time(), self.cfg.lookback_days)
        log.info("refresh requested (since=%s, %d channels%s%s)", req.since, len(req.channels),
                 ", roster" if req.roster else "", ", cycle=" + req.cycle if req.cycle else "")
        self.request_refresh(req)


# -- entry point ------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Bridge between `wx biz-articles` and the Obsidian feeds service.")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true",
                      help="run one cycle and push, then exit (non-zero on failure)")
    mode.add_argument("--dry-run", action="store_true",
                      help="run one cycle, print push counts, roster size and sample items, do not push")
    p.add_argument("--since-days", type=float, default=None, metavar="N",
                   help="lookback for --once/--dry-run (default FEED_LOOKBACK_DAYS)")
    p.add_argument("--channels", default="", metavar="ID,ID",
                   help="subscribed channel ids for --once/--dry-run (default none: roster only)")
    p.add_argument("--roster", action="store_true",
                   help="also scan ROSTER_LOOKBACK_DAYS back for the full account roster")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    load_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    cfg = Config.from_env()

    if not args.dry_run:
        problems = cfg.problems()
        if problems:
            for msg in problems:
                log.error("config: %s", msg)
            return 2

    days = args.since_days if args.since_days is not None else cfg.lookback_days
    channels = tuple(dict.fromkeys(c.strip() for c in args.channels.split(",") if c.strip()))
    req = RefreshRequest(int(time.time() - days * 86400), channels, args.roster)

    if args.dry_run:
        try:
            pushes = refresh_once(cfg, None, req, dry_run=True)
        except FetchError as e:
            log.error("wx fetch failed: %s", e)
            return 1
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to a legacy codepage
        roster = [c for p in pushes for c in p["channels"]]
        items = [i for p in pushes for i in p["items"]]
        print("since: %d (%s)" % (req.since, format_since(req.since)))
        print("roster: %d accounts%s" % (len(roster), " (with back scan)" if req.roster else " (window only)"))
        print("items: %d for %d channels" % (len(items), len(req.channels)))
        print("pushes: %d" % len(pushes))
        for p in pushes:
            print("  seq=%d final=%s items=%d channels=%d" % (
                p["seq"], str(p["final"]).lower(), len(p["items"]), len(p["channels"])))
        print("sample channels:")
        print(json.dumps(roster[:3], ensure_ascii=False, indent=2))
        print("sample items:")
        print(json.dumps(items[:3], ensure_ascii=False, indent=2))
        return 0

    if args.once:
        client = ObsidianClient(cfg, TokenSource(cfg.auth_token_url, cfg.service_key))
        try:
            refresh_once(cfg, client, req)
        except FetchError as e:
            log.error("wx fetch failed, nothing pushed: %s", e)
            return 1
        except OutOfSequence as e:
            log.error("cycle aborted: %s", e)
            return 1
        except PushError as e:
            log.error("%s%s", e, Bridge._hint(e))
            return 1
        return 0

    bridge = Bridge(cfg)

    def _shutdown(signum, frame):
        bridge.stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    bridge.start()
    while not bridge.stop_event.wait(1.0):
        pass
    bridge.stop()
    log.info("bridge stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
