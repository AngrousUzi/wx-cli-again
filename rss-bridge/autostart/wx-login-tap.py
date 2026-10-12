#!/usr/bin/env python3
"""wx-login-tap: press WeChat Linux's 进入微信 button after an auto-login launch.

Runs as the desktop user. Every INTERVAL seconds it takes a screenshot
(gnome-screenshot, needs the session bus) and checks whether the pixel at
(PROBE_X, PROBE_Y) is WeChat green, which is what the login window's 进入微信
button looks like when the window sits in the top-left corner. When it is, it
drives a virtual relative mouse (uinput, needs root, so it spawns itself through
`sudo -n --mouse-server`) in a closed loop: move, find the cursor by diffing a
screenshot taken with and without the pointer, correct, and click once the
cursor is within TOLERANCE px of the button center. Exits 0 once the button is
gone, 1 on timeout.

Touch and keyboard injection are NOT used: on this GNOME 46 / Wayland session
mutter drops uinput touchscreens and keyboards, only relative mice get through.

    wx-login-tap.py [--probe 150 320] [--target 205 327] [--max-wait 180] [--interval 3]
    sudo wx-login-tap.py --mouse-server      (internal; reads "move dx dy" / "click" / "quit" on stdin)
"""

import argparse
import os
import subprocess
import sys
import time

PROBE_PLAIN = "/tmp/wx-login-probe.png"
PROBE_CURSOR = "/tmp/wx-login-probe-cursor.png"
TOLERANCE = 8  # px; the button is about 168x34 px
MAX_STEPS = 10


def log(msg):
    print(f"wx-login-tap: {time.strftime('%H:%M:%S')} {msg}", flush=True)


def is_wechat_green(rgb):
    r, g, b = rgb[:3]
    return r < 80 and g > 160 and b < 140 and g > r + 80


def screenshot(path, with_pointer=False):
    cmd = ["gnome-screenshot", "-f", path] + (["-p"] if with_pointer else [])
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        log(f"screenshot failed: {e}")
        return None
    from PIL import Image
    return Image.open(path).convert("RGB")


def find_cursor():
    """Return the cursor hotspot (top-left of the arrow) or None."""
    from PIL import ImageChops
    a = screenshot(PROBE_PLAIN)
    b = screenshot(PROBE_CURSOR, with_pointer=True)
    if a is None or b is None or a.size != b.size:
        return None
    bbox = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > 40 else 0).getbbox()
    if not bbox:
        return None
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if w > 80 or h > 80:  # something else changed on screen, not just the pointer
        return None
    return bbox[0], bbox[1]


# -- root side: virtual mouse -------------------------------------------------

def mouse_server():
    from evdev import UInput, ecodes as e
    ui = UInput({e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT], e.EV_REL: [e.REL_X, e.REL_Y]},
                name="wx-login-tap mouse")
    time.sleep(2.5)  # mutter needs a moment to pick the device up
    print("ready", flush=True)
    for line in sys.stdin:
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "move" and len(parts) == 3:
            ui.write(e.EV_REL, e.REL_X, int(parts[1]))
            ui.write(e.EV_REL, e.REL_Y, int(parts[2]))
            ui.syn()
        elif parts[0] == "click":
            ui.write(e.EV_KEY, e.BTN_LEFT, 1); ui.syn()
            time.sleep(0.08)
            ui.write(e.EV_KEY, e.BTN_LEFT, 0); ui.syn()
        elif parts[0] == "quit":
            break
        time.sleep(0.25)
        print("ok", flush=True)
    ui.close()


class Mouse:
    def __init__(self):
        self.p = subprocess.Popen(["sudo", "-n", sys.executable, os.path.abspath(__file__), "--mouse-server"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        if self.p.stdout.readline().strip() != "ready":
            raise RuntimeError("mouse server did not start (sudo -n failed?)")

    def cmd(self, s):
        self.p.stdin.write(s + "\n"); self.p.stdin.flush()
        return self.p.stdout.readline().strip() == "ok"

    def close(self):
        try:
            self.cmd("quit")
        except Exception:
            pass
        self.p.wait(timeout=5)


def click_at(tx, ty):
    """Closed-loop: steer the virtual mouse onto (tx, ty) and click. Returns True on click."""
    m = Mouse()
    try:
        m.cmd("move -5000 -5000")  # park at the top-left corner so acceleration errors stay bounded
        pos = find_cursor()
        for step in range(MAX_STEPS):
            if pos is None:
                log("cursor not found, nudging")
                m.cmd("move 3 3")
                pos = find_cursor()
                continue
            dx, dy = tx - pos[0], ty - pos[1]
            log(f"cursor at {pos}, delta ({dx},{dy})")
            if abs(dx) <= TOLERANCE and abs(dy) <= TOLERANCE:
                m.cmd("click")
                log(f"clicked at {pos}")
                return True
            m.cmd(f"move {dx} {dy}")
            pos = find_cursor()
        log("could not steer the cursor onto the button")
        return False
    finally:
        m.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--probe", nargs=2, type=int, default=[150, 320], metavar=("X", "Y"))
    p.add_argument("--target", nargs=2, type=int, default=[205, 327], metavar=("X", "Y"))
    p.add_argument("--max-wait", type=int, default=180)
    p.add_argument("--interval", type=float, default=3.0)
    p.add_argument("--mouse-server", action="store_true")
    a = p.parse_args()

    if a.mouse_server:
        mouse_server()
        return 0

    deadline = time.time() + a.max_wait
    clicks = 0
    while time.time() < deadline:
        im = screenshot(PROBE_PLAIN)
        if im is None:
            time.sleep(a.interval)
            continue
        px = im.getpixel(tuple(a.probe))
        if is_wechat_green(px):
            if clicks >= 3:
                log(f"button still green after {clicks} clicks, giving up")
                return 1
            log(f"login button at {tuple(a.probe)} is {px}, clicking")
            try:
                if click_at(*a.target):
                    clicks += 1
            except Exception as e:
                log(f"click failed: {e}")
                return 1
            time.sleep(5)
            continue
        if clicks:
            log(f"button gone after {clicks} click(s), done")
            return 0
        time.sleep(a.interval)
    log("timed out waiting for the login button" if not clicks else "timed out after clicking")
    return 1


if __name__ == "__main__":
    sys.exit(main())
