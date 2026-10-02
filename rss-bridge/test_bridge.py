"""Unit tests for bridge.py. No network, no wx binary."""

import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest import mock

import bridge

SAMPLE_URL = ("http://mp.weixin.qq.com/s?__biz=MzU2MzA2ODk3Nw==&mid=2248172340&idx=1"
              "&sn=7819afa88d5bbb1a56dc11fe0bc22e2a&chksm=fe0aa12f&scene=0&xtrack=1#rd")


def article(**kw):
    base = {
        "account": "Acct", "account_username": "gh_abc", "title": "Title",
        "url": SAMPLE_URL, "digest": "d", "cover_url": "https://mmbiz.qpic.cn/x",
        "timestamp": 1787744073, "recv_time": 1787744325,
    }
    base.update(kw)
    return base


class FrameParserTest(unittest.TestCase):
    def test_comment_and_frame(self):
        p = bridge.FrameParser()
        frames = p.feed(': connected\n\nevent: notification\ndata: {"a":1}\n\n')
        self.assertEqual(frames, [("notification", '{"a":1}')])

    def test_split_chunks_and_crlf(self):
        p = bridge.FrameParser()
        out = []
        for piece in ["event: notif", "ication\r\nda", "ta: x\r\n", "\r\n"]:
            out += p.feed(piece)
        self.assertEqual(out, [("notification", "x")])

    def test_multiline_data_and_default_event(self):
        p = bridge.FrameParser()
        self.assertEqual(p.feed("data: a\ndata: b\n\n"), [("message", "a\nb")])

    def test_ping_and_blank_lines_ignored(self):
        p = bridge.FrameParser()
        self.assertEqual(p.feed(": ping\n\n\n\n"), [])


class UrlTest(unittest.TestCase):
    def test_normalize(self):
        url, guid = bridge.normalize_wechat_url(SAMPLE_URL)
        self.assertEqual(
            url,
            "https://mp.weixin.qq.com/s?__biz=MzU2MzA2ODk3Nw==&mid=2248172340&idx=1"
            "&sn=7819afa88d5bbb1a56dc11fe0bc22e2a&chksm=fe0aa12f")
        self.assertEqual(guid, "MzU2MzA2ODk3Nw==:2248172340:1")

    def test_param_order_and_optional_sn(self):
        url, _ = bridge.normalize_wechat_url(
            "https://mp.weixin.qq.com/s?idx=2&mid=5&__biz=AB==&foo=bar")
        self.assertEqual(url, "https://mp.weixin.qq.com/s?__biz=AB==&mid=5&idx=2")

    def test_rejected(self):
        bad = [
            "https://example.com/s?__biz=a&mid=1&idx=1",
            "https://mp.weixin.qq.com/other?__biz=a&mid=1&idx=1",
            "https://mp.weixin.qq.com/s?__biz=a&mid=1",
            "ftp://mp.weixin.qq.com/s?__biz=a&mid=1&idx=1",
            "https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=a",
            "", None,
        ]
        for u in bad:
            self.assertIsNone(bridge.normalize_wechat_url(u), u)


class MapArticleTest(unittest.TestCase):
    def local(self, ts):
        return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")

    def test_basic_mapping(self):
        item = bridge.map_article(article())
        self.assertEqual(item["guid"], "MzU2MzA2ODk3Nw==:2248172340:1")
        self.assertEqual(item["channel_id"], "gh_abc")
        self.assertEqual(item["channel"], "Acct")
        self.assertEqual(item["summary"], "d")
        self.assertEqual(item["image_url"], "https://mmbiz.qpic.cn/x")
        self.assertEqual(item["published_at"], self.local(1787744073))
        self.assertEqual(item["received_at"], self.local(1787744325))
        self.assertTrue(item["url"].startswith("https://"))

    def test_numeric_string_timestamps(self):
        item = bridge.map_article(article(timestamp="1787744073", recv_time="1787744325"))
        self.assertEqual(item["published_at"], self.local(1787744073))
        self.assertEqual(item["received_at"], self.local(1787744325))

    def test_received_defaults_to_published(self):
        item = bridge.map_article(article(recv_time=None))
        self.assertEqual(item["received_at"], item["published_at"])

    def test_no_timestamps_dropped(self):
        self.assertIsNone(bridge.map_article(article(timestamp=None, recv_time="")))

    def test_non_wechat_url_dropped(self):
        self.assertIsNone(bridge.map_article(article(url="https://example.com/a")))

    def test_missing_title_dropped(self):
        self.assertIsNone(bridge.map_article(article(title="")))
        self.assertIsNone(bridge.map_article(article(title="   ")))
        a = article()
        del a["title"]
        self.assertIsNone(bridge.map_article(a))

    def test_dedupe_by_guid(self):
        items = bridge.map_articles([article(), article(title="Other")])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Title")


class ChunkTest(unittest.TestCase):
    def shape(self, n_items, n_channels=0):
        pushes = bridge.build_pushes("wechat", 100, [{}] * n_items, [{}] * n_channels)
        return [(p["seq"], len(p["items"]), len(p["channels"]), p["final"]) for p in pushes]

    def test_zero_item_cycle_still_ends_with_final(self):
        self.assertEqual(self.shape(0), [(0, 0, 0, True)])

    def test_item_sizes(self):
        self.assertEqual(self.shape(1), [(0, 1, 0, True)])
        self.assertEqual(self.shape(500), [(0, 500, 0, True)])
        self.assertEqual(self.shape(501), [(0, 500, 0, False), (1, 1, 0, True)])
        self.assertEqual(self.shape(1200), [(0, 500, 0, False), (1, 500, 0, False), (2, 200, 0, True)])

    def test_channels_only_cycle(self):
        self.assertEqual(self.shape(0, 1000), [(0, 0, 1000, True)])
        self.assertEqual(self.shape(0, 2500),
                         [(0, 0, 1000, False), (1, 0, 1000, False), (2, 0, 500, True)])

    def test_channels_and_items_spread_independently(self):
        self.assertEqual(self.shape(10, 2500),
                         [(0, 10, 1000, False), (1, 0, 1000, False), (2, 0, 500, True)])
        self.assertEqual(self.shape(1200, 3),
                         [(0, 500, 3, False), (1, 500, 0, False), (2, 200, 0, True)])

    def test_every_push_carries_source_and_same_since(self):
        pushes = bridge.build_pushes("wechat", 1790000000, [{}] * 501, [])
        self.assertEqual([p["since"] for p in pushes], [1790000000] * 2)
        self.assertEqual({p["source"] for p in pushes}, {"wechat"})
        self.assertEqual(set(pushes[0]), {"source", "since", "seq", "final", "channels", "items"})

    def test_cycle_echoed_in_every_push_and_absent_without(self):
        pushes = bridge.build_pushes("wechat", 1, [{}] * 1200, [], cycle="c9")
        self.assertEqual([p["cycle"] for p in pushes], ["c9"] * 3)
        for p in bridge.build_pushes("wechat", 1, [{}] * 1200, []):
            self.assertNotIn("cycle", p)
            self.assertNotIn("truncated", p)

    def test_truncated_only_on_final_push(self):
        pushes = bridge.build_pushes("wechat", 1, [{}] * 1200, [], truncated=True)
        self.assertEqual([p.get("truncated") for p in pushes], [None, None, True])
        self.assertEqual(bridge.build_pushes("wechat", 1, [], [], truncated=True)[0]["truncated"], True)
        for p in bridge.build_pushes("wechat", 1, [{}] * 1200, [], truncated=False):
            self.assertNotIn("truncated", p)

    def test_nothing_lost_or_duplicated(self):
        items = [{"n": i} for i in range(1201)]
        chans = [{"c": i} for i in range(2001)]
        pushes = bridge.build_pushes("wechat", 1, items, chans)
        self.assertEqual([i for p in pushes for i in p["items"]], items)
        self.assertEqual([c for p in pushes for c in p["channels"]], chans)
        self.assertEqual([p["final"] for p in pushes], [False, False, True])


class FrameTest(unittest.TestCase):
    NOW = 1790000000

    def parse(self, info):
        return bridge.parse_refresh_frame(info, self.NOW, 7)

    def test_full_frame(self):
        req = self.parse({"since": "1789000000", "channels": '["gh_a","gh_b"]', "roster": "1"})
        self.assertEqual(req, bridge.RefreshRequest(1789000000, ("gh_a", "gh_b"), True))

    def test_roster_absent_or_other_is_false(self):
        self.assertFalse(self.parse({"since": "1789000000"}).roster)
        self.assertFalse(self.parse({"since": "1789000000", "roster": "0"}).roster)
        self.assertFalse(self.parse({"since": "1789000000", "roster": ""}).roster)

    def test_missing_or_bad_since_falls_back_to_lookback(self):
        want = self.NOW - 7 * 86400
        for info in ({}, {"since": ""}, {"since": "soon"}, {"since": None}, {"since": "-5"}):
            self.assertEqual(self.parse(info).since, want, info)

    def test_missing_or_bad_channels_means_none(self):
        for info in ({}, {"channels": ""}, {"channels": "not json"}, {"channels": "{}"},
                     {"channels": '"gh_a"'}, {"channels": "null"}, {"channels": 7}):
            self.assertEqual(self.parse(info).channels, (), info)

    def test_channels_elements_cleaned(self):
        req = self.parse({"channels": '["gh_a", 3, "", " gh_b ", "gh_a", null]'})
        self.assertEqual(req.channels, ("gh_a", "gh_b"))

    def test_empty_channels_list(self):
        self.assertEqual(self.parse({"channels": "[]"}).channels, ())

    def test_coalescing_keeps_newest_parameters(self):
        old = bridge.RefreshRequest(100, ("a",), False)
        new = bridge.RefreshRequest(900, ("b",), False)
        self.assertEqual(bridge.merge_requests(old, new), new)
        self.assertEqual(bridge.merge_requests(None, new), new)

    def test_coalescing_keeps_pending_roster(self):
        old = bridge.RefreshRequest(100, ("a",), True)
        new = bridge.RefreshRequest(900, ("b",), False)
        self.assertEqual(bridge.merge_requests(old, new), bridge.RefreshRequest(900, ("b",), True))

    def test_coalescing_roster_on_newer_frame(self):
        old = bridge.RefreshRequest(100, ("a",), False)
        new = bridge.RefreshRequest(900, ("b",), True)
        self.assertEqual(bridge.merge_requests(old, new), new)

    def test_cycle_parsed(self):
        self.assertEqual(self.parse({"cycle": "abc-1"}).cycle, "abc-1")
        for info in ({}, {"cycle": ""}, {"cycle": None}, {"cycle": 5}):
            self.assertEqual(self.parse(info).cycle, "", info)

    def test_coalescing_uses_newest_cycle_id(self):
        old = bridge.RefreshRequest(100, ("a",), False, "c1")
        new = bridge.RefreshRequest(900, ("b",), False, "c2")
        self.assertEqual(bridge.merge_requests(old, new).cycle, "c2")
        old = bridge.RefreshRequest(100, ("a",), True, "c1")
        merged = bridge.merge_requests(old, bridge.RefreshRequest(900, ("b",), False, "c2"))
        self.assertEqual(merged, bridge.RefreshRequest(900, ("b",), True, "c2"))
        # a newest frame without a cycle id does not inherit the older one
        self.assertEqual(bridge.merge_requests(old, bridge.RefreshRequest(900, (), False)).cycle, "")

    def test_bridge_request_refresh_coalesces(self):
        b = bridge.Bridge(bridge.Config())
        b.request_refresh(bridge.RefreshRequest(100, ("a",), True))
        b.request_refresh(bridge.RefreshRequest(900, ("b",), False))
        self.assertEqual(b._pending, bridge.RefreshRequest(900, ("b",), True))


class RosterTest(unittest.TestCase):
    def build(self, *articles):
        roster = {}
        bridge.merge_roster(roster, articles)
        return roster

    def test_distinct_accounts_with_newest_name_and_time(self):
        r = self.build(
            article(account="Old", account_username="gh_a", timestamp=100),
            article(account="New", account_username="gh_a", timestamp=300),
            article(account="Mid", account_username="gh_a", timestamp=200),
            article(account="B", account_username="gh_b", timestamp=50))
        self.assertEqual(r["gh_a"], {"channel_id": "gh_a", "channel": "New", "latest": 300})
        self.assertEqual(len(r), 2)

    def test_merge_across_calls_keeps_newest(self):
        roster = {}
        bridge.merge_roster(roster, [article(account="New", account_username="gh_a", timestamp=300)])
        bridge.merge_roster(roster, [article(account="Old", account_username="gh_a", timestamp=100),
                                     article(account="C", account_username="gh_c", timestamp=10)])
        self.assertEqual(roster["gh_a"]["channel"], "New")
        self.assertEqual(roster["gh_a"]["latest"], 300)
        self.assertIn("gh_c", roster)

    def test_blank_username_skipped_and_blank_name_filled_later(self):
        r = self.build(article(account_username=""), article(account_username="gh_a", account="", timestamp=300),
                       article(account_username="gh_a", account="Late", timestamp=100))
        self.assertEqual(list(r), ["gh_a"])
        self.assertEqual(r["gh_a"]["channel"], "Late")
        self.assertEqual(r["gh_a"]["latest"], 300)

    def test_time_falls_back_to_recv_time(self):
        r = self.build(article(timestamp=None, recv_time=777))
        self.assertEqual(r["gh_abc"]["latest"], 777)

    def test_includes_accounts_whose_articles_do_not_map(self):
        r = self.build(article(url="https://example.com/a", title=""))
        self.assertIn("gh_abc", r)

    def test_wire_shape_utc_and_order(self):
        roster = self.build(article(account="A", account_username="gh_a", timestamp=1787744073),
                            article(account="B", account_username="gh_b", timestamp=1787744999),
                            article(account="C", account_username="gh_c", timestamp=None, recv_time=None))
        chans = bridge.roster_channels(roster)
        self.assertEqual([c["channel_id"] for c in chans], ["gh_b", "gh_a", "gh_c"])
        self.assertEqual(chans[1]["latest_at"],
                         datetime.fromtimestamp(1787744073, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertTrue(chans[0]["latest_at"].endswith("Z"))
        self.assertNotIn("latest_at", chans[2])


class SliceTest(unittest.TestCase):
    DAY = 86400

    def test_no_gap_no_overlap_and_exact_cover(self):
        now = 1_800_000_000
        since = now - 7 * self.DAY
        slices = bridge.roster_slices(since, now, 365)
        floor = int(now - 365 * self.DAY)
        self.assertEqual(slices[0][1], since - 1)  # newest slice ends right before the window
        self.assertEqual(slices[-1][0], floor)  # oldest slice starts at the floor
        for (lo, hi), (lo2, hi2) in zip(slices, slices[1:]):
            self.assertEqual(lo - 1, hi2)  # adjacent, nothing shared, nothing skipped
        covered = sum(hi - lo + 1 for lo, hi in slices)
        self.assertEqual(covered, since - floor)

    def test_slice_width_at_most_30_days(self):
        slices = bridge.roster_slices(1_800_000_000 - self.DAY, 1_800_000_000, 365)
        self.assertTrue(all(hi - lo + 1 <= 30 * self.DAY for lo, hi in slices))
        self.assertTrue(all(lo <= hi for lo, hi in slices))
        self.assertEqual([hi - lo + 1 for lo, hi in slices[:-1]], [30 * self.DAY] * (len(slices) - 1))

    def test_edge_second_belongs_to_exactly_one_slice(self):
        now = 1_800_000_000
        slices = bridge.roster_slices(now - 7 * self.DAY, now, 100)
        for lo, hi in slices:
            for t in (lo, hi):
                self.assertEqual(sum(1 for a, b in slices if a <= t <= b), 1)

    def test_window_reaching_floor_means_no_scan(self):
        now = 1_800_000_000
        self.assertEqual(bridge.roster_slices(now - 365 * self.DAY, now, 365), [])
        self.assertEqual(bridge.roster_slices(now - 400 * self.DAY, now, 365), [])
        self.assertEqual(bridge.roster_slices(now - self.DAY, now, 0), [])

    def test_short_remainder_is_one_slice(self):
        now = 1_800_000_000
        slices = bridge.roster_slices(now - 7 * self.DAY, now, 10)
        self.assertEqual(len(slices), 1)
        self.assertEqual(slices[0], (now - 10 * self.DAY, now - 7 * self.DAY - 1))


class SubscribedFilterTest(unittest.TestCase):
    def test_only_subscribed_accounts(self):
        arts = [article(account_username="gh_a"), article(account_username="gh_b"),
                article(account_username="gh_c"), article(account_username=" gh_a ")]
        kept = bridge.filter_subscribed(arts, ["gh_a", "gh_c"])
        self.assertEqual([a["account_username"].strip() for a in kept], ["gh_a", "gh_c", "gh_a"])

    def test_no_channels_no_items(self):
        self.assertEqual(bridge.filter_subscribed([article()], []), [])


class CycleTest(unittest.TestCase):
    """build_cycle with fetch_articles stubbed: no wx binary."""

    def run_cycle(self, req, window, slices=None, now=1_800_000_000, lookback=365):
        cfg = bridge.Config(roster_lookback_days=lookback, source="wechat")
        calls = []

        def fake(cfg_, since, until=None):
            calls.append((since, until))
            if until is None:
                return window
            return (slices or {}).get(since, [])

        with mock.patch.object(bridge, "fetch_articles", fake):
            pushes, size = bridge.build_cycle(cfg, req, now=now)
        return pushes, size, calls

    def test_roster_only_when_no_channels(self):
        window = [article(account_username="gh_a", account="A"),
                  article(account_username="gh_b", account="B", url=SAMPLE_URL.replace("2248172340", "9"))]
        pushes, size, calls = self.run_cycle(bridge.RefreshRequest(1_799_000_000), window)
        self.assertEqual(size, 2)
        self.assertEqual(len(pushes), 1)
        self.assertTrue(pushes[0]["final"])
        self.assertEqual(pushes[0]["items"], [])
        self.assertEqual(len(pushes[0]["channels"]), 2)
        self.assertEqual(calls, [(1_799_000_000, None)])  # no roster scan without the flag

    def test_items_are_subscribed_only_and_roster_covers_all(self):
        window = [article(account_username="gh_a", url=SAMPLE_URL),
                  article(account_username="gh_b", url=SAMPLE_URL.replace("2248172340", "9"))]
        pushes, size, _ = self.run_cycle(bridge.RefreshRequest(1_799_000_000, ("gh_b",)), window)
        self.assertEqual([i["channel_id"] for i in pushes[0]["items"]], ["gh_b"])
        self.assertEqual({c["channel_id"] for c in pushes[0]["channels"]}, {"gh_a", "gh_b"})

    def test_roster_scan_calls_slices_and_adds_old_accounts(self):
        now = 1_800_000_000
        since = now - 7 * 86400
        older = [article(account_username="gh_old", account="Old", timestamp=since - 5)]
        slices_expected = bridge.roster_slices(since, now, 365)
        pushes, size, calls = self.run_cycle(
            bridge.RefreshRequest(since, (), True), [article(account_username="gh_new")],
            slices={slices_expected[0][0]: older}, now=now)
        self.assertEqual(calls[0], (since, None))
        self.assertEqual(calls[1:], slices_expected)
        self.assertEqual({c["channel_id"] for p in pushes for c in p["channels"]}, {"gh_new", "gh_old"})
        self.assertEqual(size, 2)
        self.assertEqual(pushes[0]["items"], [])  # old accounts are roster only

    def test_window_account_keeps_window_name_over_older_slice(self):
        now = 1_800_000_000
        since = now - 7 * 86400
        slices_expected = bridge.roster_slices(since, now, 365)
        pushes, _, _ = self.run_cycle(
            bridge.RefreshRequest(since, (), True),
            [article(account_username="gh_a", account="Current", timestamp=since + 10)],
            slices={slices_expected[0][0]: [article(account_username="gh_a", account="Former", timestamp=since - 10)]},
            now=now)
        self.assertEqual(pushes[0]["channels"][0]["channel"], "Current")

    def test_many_items_and_channels_sequence(self):
        window = []
        for n in range(1200):
            url = SAMPLE_URL.replace("2248172340", str(1000 + n))
            window.append(article(account_username="gh_%d" % (n % 1100), url=url))
        chans = tuple("gh_%d" % i for i in range(1100))
        pushes, size, _ = self.run_cycle(bridge.RefreshRequest(1_799_000_000, chans), window)
        self.assertEqual(size, 1100)
        self.assertEqual([p["seq"] for p in pushes], [0, 1, 2])
        self.assertEqual([p["final"] for p in pushes], [False, False, True])
        self.assertEqual(sum(len(p["items"]) for p in pushes), 1200)
        self.assertEqual(sum(len(p["channels"]) for p in pushes), 1100)
        self.assertTrue(all(len(p["items"]) <= 500 and len(p["channels"]) <= 1000 for p in pushes))


class WindowSliceTest(unittest.TestCase):
    DAY = 86400
    NOW = 1_800_000_000

    def test_short_window_is_one_unbounded_call(self):
        for days in (0.5, 7, 30):
            since = int(self.NOW - days * self.DAY)
            self.assertEqual(bridge.window_slices(since, self.NOW), [(since, None)], days)

    def test_just_over_30_days_is_two_slices(self):
        since = self.NOW - 30 * self.DAY - 1
        slices = bridge.window_slices(since, self.NOW)
        self.assertEqual(len(slices), 2)
        self.assertEqual(slices[1], (since, slices[1][1]))
        self.assertEqual(slices[0][0] - 1, slices[1][1])

    def test_long_window_boundaries(self):
        since = self.NOW - 200 * self.DAY
        slices = bridge.window_slices(since, self.NOW)
        self.assertEqual(len(slices), 7)
        self.assertIsNone(slices[0][1])
        self.assertEqual(slices[-1][0], since)
        his = [self.NOW] + [hi for _, hi in slices[1:]]  # unbounded top stands for NOW
        for (lo, _), hi_next in zip(slices, his[1:]):
            self.assertEqual(lo - 1, hi_next)  # no gap, no shared second
        covered = sum((hi if hi is not None else self.NOW) - lo + 1 for lo, hi in slices)
        self.assertEqual(covered, self.NOW - since + 1)

    def test_future_since(self):
        self.assertEqual(bridge.window_slices(self.NOW + 100, self.NOW), [(self.NOW + 100, None)])

    def test_window_then_roster_scan_are_contiguous(self):
        since = self.NOW - 200 * self.DAY
        win = bridge.window_slices(since, self.NOW)
        scan = bridge.roster_slices(since, self.NOW, 365)
        self.assertEqual(scan[0][1], win[-1][0] - 1)


class LongWindowCycleTest(unittest.TestCase):
    NOW = 1_800_000_000
    DAY = 86400

    def make(self, n, ts, acct="gh_a", name="A"):
        return article(account_username=acct, account=name, timestamp=ts,
                       url=SAMPLE_URL.replace("2248172340", str(ts * 1000 + n)))

    def fake_wx(self, store, calls):
        """Stand-in for wx: returns the stored articles inside [since, until]."""
        def fake(cfg, since, until=None):
            calls.append((since, until))
            hi = until if until is not None else 10 ** 12
            return sorted((a for a in store if since <= a["timestamp"] <= hi),
                          key=lambda a: -a["timestamp"])
        return fake

    def run_cycle(self, store, req, lookback=365, patch_cap=None):
        cfg = bridge.Config(roster_lookback_days=lookback, source="wechat")
        calls = []
        with mock.patch.object(bridge, "fetch_articles", self.fake_wx(store, calls)):
            if patch_cap is not None:
                with mock.patch.object(bridge, "MAX_CYCLE_ITEMS", patch_cap):
                    out = bridge.build_cycle(cfg, req, now=self.NOW)
            else:
                out = bridge.build_cycle(cfg, req, now=self.NOW)
        return out + (calls,)

    def test_multi_slice_window_ordered_and_complete(self):
        since = self.NOW - 100 * self.DAY
        store = [self.make(0, self.NOW - d * self.DAY, "gh_a") for d in (1, 29, 31, 59, 61, 95)]
        store.append(self.make(1, self.NOW - 70 * self.DAY, "gh_other", "Other"))
        pushes, size, calls = self.run_cycle(store, bridge.RefreshRequest(since, ("gh_a",)))
        self.assertEqual(len(calls), 4)  # 100 days = 4 slices of at most 30 days
        self.assertIsNone(calls[0][1])
        items = [i for p in pushes for i in p["items"]]
        self.assertEqual(len(items), 6)
        times = [i["published_at"] for i in items]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual({c["channel_id"] for c in pushes[0]["channels"]}, {"gh_a", "gh_other"})
        self.assertEqual(size, 2)

    def test_edge_second_counted_once(self):
        since = self.NOW - 60 * self.DAY
        slices = bridge.window_slices(since, self.NOW)
        edge = slices[1][1]  # last second of the older slice
        store = [self.make(0, edge), self.make(1, edge + 1), self.make(2, since)]
        pushes, _, _ = self.run_cycle(store, bridge.RefreshRequest(since, ("gh_a",)))
        self.assertEqual(len([i for p in pushes for i in p["items"]]), 3)

    def test_cap_reached_mid_slice_keeps_newest(self):
        since = self.NOW - 100 * self.DAY
        store = [self.make(n, self.NOW - 40 * self.DAY + n) for n in range(10)]  # all in slice 2
        store += [self.make(n, self.NOW - 90 * self.DAY + n) for n in range(5)]
        with self.assertLogs("wx-rss-bridge", level="WARNING") as logs:
            pushes, _, _ = self.run_cycle(store, bridge.RefreshRequest(since, ("gh_a",)), patch_cap=4)
        items = [i for p in pushes for i in p["items"]]
        self.assertEqual(len(items), 4)
        want = [self.make(n, self.NOW - 40 * self.DAY + n) for n in (9, 8, 7, 6)]
        self.assertEqual([i["guid"] for i in items],
                         [bridge.map_article(a)["guid"] for a in want])
        self.assertEqual(sum("capped" in l and "11 older" in l for l in logs.output), 1)

    def test_cap_reached_roster_still_completes(self):
        since = self.NOW - 100 * self.DAY
        store = [self.make(n, self.NOW - 5 * self.DAY + n) for n in range(6)]
        store.append(self.make(0, self.NOW - 95 * self.DAY, "gh_deep", "Deep"))  # last window slice
        store.append(self.make(0, self.NOW - 200 * self.DAY, "gh_old", "Old"))  # roster scan
        with self.assertLogs("wx-rss-bridge", level="WARNING"):
            pushes, size, calls = self.run_cycle(
                store, bridge.RefreshRequest(since, ("gh_a", "gh_deep"), True), patch_cap=3)
        self.assertEqual(len([i for p in pushes for i in p["items"]]), 3)
        self.assertEqual({c["channel_id"] for p in pushes for c in p["channels"]},
                         {"gh_a", "gh_deep", "gh_old"})
        self.assertEqual(len(calls), 4 + len(bridge.roster_slices(since, self.NOW, 365)))

    def test_cycle_and_truncated_flow_through_build_cycle(self):
        since = self.NOW - 10 * self.DAY
        store = [self.make(n, self.NOW - 5 * self.DAY + n) for n in range(6)]
        req = bridge.RefreshRequest(since, ("gh_a",), False, "cyc-1")
        with self.assertLogs("wx-rss-bridge", level="WARNING"):
            pushes, _, _ = self.run_cycle(store, req, patch_cap=5)
        self.assertEqual({p["cycle"] for p in pushes}, {"cyc-1"})
        self.assertEqual([p.get("truncated") for p in pushes], [None] * (len(pushes) - 1) + [True])
        pushes, _, _ = self.run_cycle(store, req)  # under the real cap
        self.assertTrue(all("truncated" not in p for p in pushes))
        pushes, _, _ = self.run_cycle(store, bridge.RefreshRequest(since, ("gh_a",)))
        self.assertTrue(all("cycle" not in p for p in pushes))

    def test_no_warning_below_cap(self):
        since = self.NOW - 100 * self.DAY
        store = [self.make(0, self.NOW - 50 * self.DAY)]
        with self.assertNoLogs("wx-rss-bridge", level="WARNING"):
            self.run_cycle(store, bridge.RefreshRequest(since, ("gh_a",)))

    def test_short_window_is_one_call(self):
        store = [self.make(0, self.NOW - 3 * self.DAY)]
        pushes, _, calls = self.run_cycle(store, bridge.RefreshRequest(self.NOW - 7 * self.DAY, ("gh_a",)))
        self.assertEqual(calls, [(self.NOW - 7 * self.DAY, None)])
        self.assertEqual(len(pushes[0]["items"]), 1)

    def test_cap_constant_mirrors_server(self):
        self.assertEqual(bridge.MAX_CYCLE_ITEMS, 20000)


class FetchTest(unittest.TestCase):
    def run_fetch(self, n, limit, until=None):
        cfg = bridge.Config(wx_bin="wx", wx_limit=limit)
        out = json.dumps([{}] * n).encode()
        proc = mock.Mock(returncode=0, stdout=out, stderr=b"")
        with mock.patch.object(bridge.subprocess, "run", return_value=proc) as run:
            with self.assertLogs("wx-rss-bridge", level="DEBUG") as logs:
                bridge.log.debug("marker")
                bridge.fetch_articles(cfg, 1787744325, until)
        return run.call_args[0][0], logs.output

    def test_truncation_warns(self):
        _, logs = self.run_fetch(5, 5)
        self.assertTrue(any("WARNING" in l and "truncated" in l for l in logs))

    def test_below_limit_does_not_warn(self):
        _, logs = self.run_fetch(4, 5)
        self.assertFalse(any("truncated" in l for l in logs))

    def test_command_has_since_and_optional_until(self):
        cmd, _ = self.run_fetch(1, 5)
        self.assertNotIn("--until", cmd)
        cmd, _ = self.run_fetch(1, 5, until=1787744999)
        self.assertEqual(cmd[cmd.index("--until") + 1], bridge.format_since(1787744999))
        self.assertEqual(cmd[cmd.index("-n") + 1], "5")


class PushClientTest(unittest.TestCase):
    def client(self):
        cfg = bridge.Config(obsidian_url="http://h", auth_token_url="", source="wechat")
        return bridge.ObsidianClient(cfg, bridge.TokenSource("", ""))

    @staticmethod
    def ok(received, channels):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = json.dumps(
            {"received": received, "channels": channels}).encode()
        return resp

    @staticmethod
    def http_error(code, body):
        return urllib.error.HTTPError("http://h", code, "x", {}, io.BytesIO(body.encode()))

    def test_pushes_in_order_and_sums_counts(self):
        pushes = bridge.build_pushes("wechat", 1, [{}] * 600, [{}] * 3)
        sent = []

        def fake(req, timeout=None):
            sent.append(json.loads(req.data))
            return self.ok(len(sent[-1]["items"]), len(sent[-1]["channels"]))

        with mock.patch.object(bridge.urllib.request, "urlopen", fake):
            totals = self.client().push(pushes)
        self.assertEqual(totals, (600, 3))
        self.assertEqual([p["seq"] for p in sent], [0, 1])
        self.assertEqual([p["final"] for p in sent], [False, True])

    def test_409_out_of_sequence_aborts_without_retry(self):
        pushes = bridge.build_pushes("wechat", 1, [{}] * 1200, [])
        calls = []

        def fake(req, timeout=None):
            calls.append(json.loads(req.data)["seq"])
            if len(calls) == 2:
                raise self.http_error(409, '{"error":"out_of_sequence","expected":0}')
            return self.ok(500, 0)

        with mock.patch.object(bridge.urllib.request, "urlopen", fake):
            with self.assertRaises(bridge.OutOfSequence) as cm:
                self.client().push(pushes)
        self.assertEqual(calls, [0, 1])  # failed chunk not retried, chunk 2 never sent
        self.assertEqual(cm.exception.status, 409)

    def test_other_http_error_is_plain_push_error(self):
        with mock.patch.object(bridge.urllib.request, "urlopen",
                               side_effect=self.http_error(500, "boom")):
            with self.assertRaises(bridge.PushError) as cm:
                self.client().push(bridge.build_pushes("wechat", 1, [], []))
        self.assertNotIsInstance(cm.exception, bridge.OutOfSequence)


class EnvLoaderTest(unittest.TestCase):
    def test_parse(self):
        text = ("# comment\n\nA=1\nexport B = two \nC=\"quoted # not comment\"\n"
                "D='single'\nE=val # trailing\nF=\nnoequals\n")
        self.assertEqual(bridge.parse_env_text(text), {
            "A": "1", "B": "two", "C": "quoted # not comment", "D": "single",
            "E": "val", "F": ""})

    def test_real_env_wins_and_empty_is_set(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            with open(path, "w", encoding="utf-8") as f:
                f.write("X=file\nY=file\nAUTH_TOKEN_URL=\nZ=file\n")
            env = {"X": "real", "Y": ""}
            bridge.load_env_file(path, env)
            self.assertEqual(env["X"], "real")
            self.assertEqual(env["Y"], "")  # empty real value is not overridden
            self.assertEqual(env["AUTH_TOKEN_URL"], "")
            self.assertEqual(env["Z"], "file")

    def test_missing_file_ok(self):
        env = {}
        bridge.load_env_file(os.path.join(tempfile.gettempdir(), "no-such-dir", ".env"), env)
        self.assertEqual(env, {})

    def test_auth_token_url_empty_vs_unset(self):
        self.assertEqual(bridge.Config.from_env({}).auth_token_url, bridge.DEFAULT_TOKEN_URL)
        self.assertEqual(bridge.Config.from_env({"AUTH_TOKEN_URL": ""}).auth_token_url, "")

    def test_config_defaults(self):
        c = bridge.Config.from_env({"OBSIDIAN_URL": "https://h/obsidian/"})
        self.assertEqual(c.obsidian_url, "https://h/obsidian")
        self.assertEqual((c.source, c.wx_bin, c.wx_limit, c.lookback_days, c.wx_timeout),
                         ("wechat", "wx", 200000, 7, 120))
        self.assertEqual(c.roster_lookback_days, 365)

    def test_roster_lookback_override(self):
        c = bridge.Config.from_env({"ROSTER_LOOKBACK_DAYS": "90"})
        self.assertEqual(c.roster_lookback_days, 90)


class SinceTest(unittest.TestCase):
    def test_format_roundtrip(self):
        ts = 1787744325
        s = bridge.format_since(ts)
        self.assertRegex(s, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()), ts)


if __name__ == "__main__":
    unittest.main()
