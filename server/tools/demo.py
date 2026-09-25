"""demo - a BlockSlot server full of invented saves, for screenshots.

Builds a throwaway /data in a temp folder, starts the real server on it
(entrypoint.py with a real Garage 2.1.0 binary, on free ports, never the
defaults), and fills the store the way devices would: three devices with
their own keys upload snapshots through slotstore, so the web UI shows real
history, library games and a game with two saves.

Everything in it is made up: the devices, the times (the last few weeks,
counted back from now), the save files (random bytes), and the Cloudflare
and endpoint settings (example.com only). Nothing is copied from a real
store.

    python3 server/tools/demo.py --garage /path/to/garage
        Serve the demo until Ctrl-C. Sign in as admin / demo-password.

    python3 server/tools/demo.py --garage /path/to/garage --shots server/docs/images
        Make the eight Community Applications screenshots (1440x900) with
        Playwright's Chromium, then two of the getting-started guide on a
        second, empty server with no account yet, then stop.

Either way the Garage and the temp folder are thrown away at the end, unless
--keep is given. The Garage binary can also come from BLOCKSLOT_GARAGE.
"""

import argparse
import base64
import datetime
import http.client
import json
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

TOOLS = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.dirname(TOOLS)
sys.path.insert(0, SERVER_DIR)

import blockslot_server  # noqa: E402,F401  (puts engine/ on the path)
from blockslot_server import saves  # noqa: E402
import saveunits as su  # noqa: E402
import slotstore as ss  # noqa: E402

ENTRYPOINT = os.path.join(SERVER_DIR, "entrypoint.py")
USER = "admin"
PASSWORD = "demo-password"
NEW_DEVICE = "Office PC"            # added in the browser for the Devices shot
GUIDE_DEVICE = "Steam Deck"         # added in the guide on the empty server
# The browser is shown the demo at an invented LAN address, so the pairing
# address in the shots reads like a real one. Its requests to that address go
# to the demo's own port.
FAKE_ORIGIN = "http://192.168.1.20:8761"

DEVICES = [("living-room-pc", "Living Room PC"), ("steam-deck", "Steam Deck"),
           ("laptop", "Laptop")]

RETRO = "RetroArch saves"
ONE_GAME = "Bloodborne"

# (title, sessions, base save size in bytes, files, days since last played)
PC_GAMES = [
    ("Elden Ring", 8, 3_100_000, 1, 1),                 # ends with two saves
    ("Hollow Knight", 22, 190_000, 3, 0),               # the long history
    ("Baldur's Gate 3", 9, 7_800_000, 2, 0),
    ("Stardew Valley", 12, 420_000, 2, 2),
    ("Hades II", 10, 260_000, 2, 1),
    ("Balatro", 7, 36_000, 1, 3),
    ("Celeste", 4, 22_000, 3, 18),
    ("Dark Souls III", 5, 2_600_000, 1, 11),
    ("Sekiro: Shadows Die Twice", 4, 2_900_000, 1, 24),
    ("The Witcher 3: Wild Hunt", 6, 5_400_000, 2, 6),
    ("Cyberpunk 2077", 5, 6_200_000, 2, 9),
    ("Disco Elysium", 3, 1_700_000, 1, 27),
    ("Slay the Spire", 8, 64_000, 3, 4),
    ("Terraria", 6, 880_000, 2, 5),
    ("Dead Cells", 4, 310_000, 1, 13),
    ("Vampire Survivors", 5, 48_000, 1, 7),
    ("Portal 2", 2, 140_000, 1, 33),
    ("Outer Wilds", 3, 18_000, 1, 21),
    ("Sea of Stars", 4, 95_000, 2, 15),
    ("Cuphead", 3, 12_000, 1, 30),
    ("Ori and the Will of the Wisps", 3, 520_000, 1, 19),
    ("Lies of P", 5, 3_800_000, 1, 8),
    ("Monster Hunter: World", 6, 9_400_000, 1, 10),
    ("Persona 5 Royal", 7, 1_200_000, 2, 3),
    ("Final Fantasy VII Remake Intergrade", 4, 4_300_000, 1, 16),
]

# (system, name as the save file names it, save size, sessions, days since last played)
RETRO_GAMES = [
    ("snes", "Super Metroid (USA)", 8192, 5, 2),
    ("snes", "Chrono Trigger (USA)", 8192, 4, 6),
    ("snes", "The Legend of Zelda - A Link to the Past (USA)", 8192, 3, 12),
    ("snes", "Super Mario World (USA)", 2048, 2, 20),
    ("snes", "EarthBound (USA)", 8192, 3, 9),
    ("psx", "Castlevania - Symphony of the Night (USA)", 131072, 4, 3),
    ("psx", "Final Fantasy VII (USA)", 131072, 5, 5),
    ("psx", "Metal Gear Solid (USA)", 131072, 2, 17),
    ("psx", "Spyro the Dragon (USA)", 131072, 2, 25),
    ("gba", "Metroid Fusion (USA)", 32768, 3, 4),
    ("gba", "Pokemon - Emerald Version (USA, Europe)", 131072, 6, 1),
    ("gba", "The Legend of Zelda - The Minish Cap (USA)", 8192, 2, 14),
    ("gba", "Advance Wars (USA)", 65536, 3, 8),
]
RETRO_EXT = {"snes": "srm", "psx": "mcr", "gba": "srm"}

LIBRARIES = {
    RETRO: {"extensions": ["srm", "sav", "mcr"], "system_aliases": [["snes", "sfc"], ["psx", "ps1"]]},
    ONE_GAME: {"one_game": "Bloodborne", "system": "ps4", "label": "shadPS4", "extensions": "*"},
}
ROOTS = {
    "living-room-pc": {RETRO: "D:/Emulation/RetroArch/saves",
                       ONE_GAME: "D:/Emulation/shadPS4/user/savedata/1/CUSA03173"},
    "steam-deck": {RETRO: "/home/deck/Emulation/saves/retroarch/saves",
                   ONE_GAME: "/home/deck/Emulation/saves/shadps4/savedata/1/CUSA03173"},
    "laptop": {RETRO: "C:/RetroArch/saves"},
}


# ------------------------------------------------------------------ plumbing


def free_ports(count):
    socks, ports = [], []
    for _ in range(count):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        socks.append(sock)
        ports.append(sock.getsockname()[1])
    for sock in socks:
        sock.close()
    return ports


class Client(object):
    """The web API, as a signed-in browser calls it."""

    def __init__(self, port):
        self.port = port
        self.cookie = None
        self.csrf = None

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        headers = {"Host": "127.0.0.1:%d" % self.port}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if self.cookie:
            headers["Cookie"] = "blockslot_session=" + self.cookie
        if self.csrf and method != "GET":
            headers["X-CSRF-Token"] = self.csrf
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        payload = response.read()
        cookie = response.getheader("Set-Cookie") or ""
        if cookie.startswith("blockslot_session="):
            self.cookie = cookie.split(";")[0].split("=", 1)[1] or None
        conn.close()
        answer = json.loads(payload) if payload else None
        if response.status != 200:
            raise RuntimeError("%s %s: %s %s" % (method, path, response.status, answer))
        return answer


class Demo(object):
    def __init__(self, garage_bin, keep=False):
        self.garage_bin = garage_bin
        self.keep = keep
        self.dir = tempfile.mkdtemp(prefix="blockslot-demo-")
        self.data = os.path.join(self.dir, "data")
        self.s3, self.rpc, self.admin, self.web = free_ports(4)
        self.proc = None
        self.log = open(os.path.join(self.dir, "server.log"), "wb")
        self.client = Client(self.web)
        self.rng = random.Random(20260925)
        self.now = ss.utc_now().replace(second=0, microsecond=0)
        self.stores = {}
        self.states = {}

    @property
    def url(self):
        return "http://127.0.0.1:%d/" % self.web

    # -- the server

    def start(self):
        env = dict(os.environ, BLOCKSLOT_DATA=self.data, BLOCKSLOT_RUN=os.path.join(self.dir, "run"),
                   BLOCKSLOT_GARAGE=self.garage_bin, BLOCKSLOT_S3_PORT=str(self.s3),
                   BLOCKSLOT_RPC_PORT=str(self.rpc), BLOCKSLOT_ADMIN_PORT=str(self.admin),
                   BLOCKSLOT_WEB_PORT=str(self.web), BLOCKSLOT_WEB_HOST="127.0.0.1",
                   BLOCKSLOT_PUBLIC_S3_PORT=str(self.s3))
        # Its own process group, so a crash here still takes Garage down with it.
        self.proc = subprocess.Popen([sys.executable, ENTRYPOINT], env=env, stdout=self.log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.time() + 90
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise SystemExit("The server exited; see %s/server.log" % self.dir)
            try:
                return Client(self.web).call("GET", "/api/session")
            except (OSError, RuntimeError):
                time.sleep(0.3)
        raise SystemExit("The web UI did not come up within 90 s")

    def stop(self):
        if self.log.closed:
            return                          # stopped already
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
        if self.proc is not None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)   # anything left in its group
            except ProcessLookupError:
                pass
        self.proc = None
        self.log.close()
        if self.keep:
            print("Kept %s" % self.dir)
        else:
            shutil.rmtree(self.dir, ignore_errors=True)

    # -- time

    def ago(self, days, hour=None, minute=0):
        """A time `days` ago, at a plausible hour (UTC) unless one is given."""
        day = self.now - datetime.timedelta(days=days)
        if hour is None:
            hour = self.rng.choice([1, 2, 3, 4, 5, 17, 20, 23])
            minute = self.rng.randrange(0, 60)
        return day.replace(hour=hour, minute=minute)

    # -- devices and uploads

    def setup(self):
        body = self.client.call("POST", "/api/setup", {"username": USER, "password": PASSWORD})
        self.client.csrf = body["csrf"]
        for device, name in DEVICES:
            made = self.client.call("POST", "/api/devices", {"name": name})
            code = json.loads(base64.b64decode(made["setup_code"]))
            self.stores[device] = ss.store_from_settings(code)
            self.states[device] = ss.LocalState(os.path.join(self.dir, "state-" + device))
        self.client.call("PUT", "/api/settings/shared", {
            "libraries": LIBRARIES, "retention": {"per_device": 10, "days": 30}})
        for device, name in DEVICES:
            self.client.call("PUT", "/api/devices/%s/settings" % device,
                             {"name": name, "roots": ROOTS.get(device, {})})

    def files_for(self, title, size, count, device):
        """A ludusavi-style backup folder of random save files."""
        source = tempfile.mkdtemp(prefix="src-", dir=self.dir)
        folder = ss.game_key(title)
        drive = "drive-0/home/deck/.local/share/Steam/steamapps/compatdata" if device == "steam-deck" \
            else "drive-C/Users/player/AppData/Roaming"
        for n in range(count):
            part = max(512, int(size * self.rng.uniform(0.85, 1.15) / count))
            name = "save%d.sav" % n if n else "profile.sav"
            path = os.path.join(source, folder, "backup-1", drive, folder, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(self.rng.randbytes(part))
        with open(os.path.join(source, folder, "mapping.yaml"), "w") as handle:
            handle.write("name: %s\n" % json.dumps(title))
        return source

    def library_files(self, rels, size):
        source = tempfile.mkdtemp(prefix="src-", dir=self.dir)
        for rel in rels:
            path = os.path.join(source, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(self.rng.randbytes(size))
        return source

    def upload(self, game, device, source, start, minutes, parents, mode="game", unit=None):
        end = start + datetime.timedelta(minutes=minutes)
        played = {"start": ss.iso(start), "end": ss.iso(end)}
        state = self.states[device]
        manifest = state.stage(game, device, source, played=played, mode=mode, parents=parents,
                               created=end + datetime.timedelta(seconds=40), unit=unit)
        _committed, error = ss.drain(self.stores[device], state)
        if error:
            raise error
        shutil.rmtree(source, ignore_errors=True)
        return manifest["id"]

    def sessions(self, count, last_days, span_days):
        """Session start times, oldest first, ending `last_days` ago."""
        first = last_days + span_days
        days = sorted({self.rng.uniform(last_days, first) for _ in range(count - 1)}, reverse=True)
        out = [self.ago(int(d)) for d in days] + [self.ago(last_days)]
        out.sort()
        # Two sessions on one evening must not overlap.
        for i in range(1, len(out)):
            if out[i] <= out[i - 1] + datetime.timedelta(hours=4):
                out[i] = out[i - 1] + datetime.timedelta(hours=4, minutes=self.rng.randrange(5, 50))
        # Every session ends before now: move the whole run back if one would not.
        latest = self.now - datetime.timedelta(hours=4)
        if out[-1] > latest:
            shift = out[-1] - latest
            out = [when - shift for when in out]
        return out

    def pick_device(self, weights=(6, 4, 1)):
        return self.rng.choices([d for d, _ in DEVICES], weights=weights)[0]

    def fill(self):
        for title, count, size, files, last in PC_GAMES:
            if title == "Hollow Knight":
                self.long_history(title, count, size, files)
                continue
            starts = self.sessions(count, max(last, 1) if title == "Elden Ring" else last,
                                   self.rng.randrange(8, 26))
            parent = []
            if title == "Elden Ring":
                starts, fork = starts[:-2], starts[-2:]
            for start in starts:
                device = self.pick_device()
                snap = self.upload(title, device, self.files_for(title, size, files, device), start,
                                   self.rng.randrange(25, 210), parent)
                parent = [snap]
            if title == "Elden Ring":
                # Both played on from the same save: two saves, and a person decides.
                self.upload(title, "living-room-pc", self.files_for(title, size, files, "living-room-pc"),
                            fork[0], 142, parent)
                self.upload(title, "steam-deck", self.files_for(title, size, files, "steam-deck"),
                            fork[1], 96, parent)
        for system, name, size, count, last in RETRO_GAMES:
            unit_id = su.unit_id(system, name)
            game = ss.library_unit_name(RETRO, unit_id)
            unit = {"library": RETRO, "id": unit_id, "title": su.display(system, name),
                    "system": system, "label": su.label_for(system)}
            rels = ["%s/%s.%s" % (system, name, RETRO_EXT[system])]
            if system == "psx":
                rels = ["%s/%s.1.mcr" % (system, name)]
            parent = []
            for start in self.sessions(count, last, self.rng.randrange(6, 30)):
                device = self.pick_device((5, 6, 1))
                parent = [self.upload(game, device, self.library_files(rels, size), start,
                                      self.rng.randrange(20, 120), parent, mode="library", unit=unit)]
        unit_id = su.unit_id("ps4", "Bloodborne")
        game = ss.library_unit_name(ONE_GAME, unit_id)
        unit = {"library": ONE_GAME, "id": unit_id, "title": su.display("ps4", "Bloodborne"),
                "system": "ps4", "label": "shadPS4"}
        rels = ["SPRJ0005/userdata%04d" % n for n in range(3)] + ["SPRJ0005/param.sfo"]
        parent = []
        for start in self.sessions(6, 2, 20):
            device = self.pick_device((7, 3, 0))
            parent = [self.upload(game, device, self.library_files(rels, 640_000), start,
                                  self.rng.randrange(40, 160), parent, mode="library", unit=unit)]

    def long_history(self, title, count, size, files):
        """Six weeks of play on two devices, with an older save restored once."""
        starts = self.sessions(count, 0, 42)
        restore_at = count - 2
        parent, chain = [], []
        for i, start in enumerate(starts):
            if i == restore_at:
                # The two sessions before went badly: go back to the save before them.
                store = self.stores["living-room-pc"]
                view = ss.read_game(store, ss.game_key(title))
                made = saves.restore(store, view, chain[-3], now=start - datetime.timedelta(minutes=20))
                parent = [made["id"]]
            device = "steam-deck" if i % 3 == 1 else "living-room-pc"
            snap = self.upload(title, device, self.files_for(title, size, files, device), start,
                               self.rng.randrange(30, 150), parent)
            chain.append(snap)
            parent = [snap]

    def finish(self):
        self.client.call("PUT", "/api/endpoint", {"endpoint": "https://saves.example.com"})
        self.client.call("PUT", "/api/access", {
            "team_domain": "example.cloudflareaccess.com",
            "aud": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "emails": ["player@example.com"]})
        self.client.call("PUT", "/api/storage/keeper", {"keeper": True})
        # The guide is for the empty server; this one is a store in use.
        self.client.call("PUT", "/api/onboarding", {"step": "done", "done": True})
        games = self.client.call("GET", "/api/games?refresh=1")["games"]
        return games


# ------------------------------------------------------------------ screenshots


def new_context(browser, demo):
    """A 1440x900 browser context that shows the demo at FAKE_ORIGIN."""
    real = demo.url.rstrip("/")

    def forward(route, request):
        headers = {k: v for k, v in request.all_headers().items()
                   if k.lower() not in ("origin", "referer", "host") and not k.startswith(":")}
        response = route.fetch(url=request.url.replace(FAKE_ORIGIN, real, 1), headers=headers)
        route.fulfill(response=response)

    context = browser.new_context(viewport={"width": 1440, "height": 900},
                                  device_scale_factor=1, locale="en-US",
                                  timezone_id="America/Los_Angeles", color_scheme="dark")
    context.route(FAKE_ORIGIN + "/**", forward)
    return context


def shoot(demo, games, out_dir):
    from playwright.sync_api import sync_playwright

    os.makedirs(out_dir, exist_ok=True)
    key = {g["title"]: g["key"] for g in games}

    def path(name):
        return os.path.join(out_dir, name)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = new_context(browser, demo).new_page()

            def settle():
                page.evaluate("document.activeElement && document.activeElement.blur()")
                page.mouse.move(1430, 890)
                page.wait_for_timeout(400)

            def visit(hash_, ready):
                page.goto(FAKE_ORIGIN + "/" + hash_)
                page.wait_for_selector(ready)
                settle()

            page.goto(FAKE_ORIGIN + "/")
            page.wait_for_selector(".gate-card")
            settle()
            page.screenshot(path=path("08-sign-in.png"))

            page.fill("input[autocomplete=username]", USER)
            page.fill("input[type=password]", PASSWORD)
            page.click("button[type=submit]")
            page.wait_for_selector(".game-list")

            visit("#/games", ".game-list")
            page.screenshot(path=path("01-games.png"))

            visit("#/game/" + key["Hollow Knight"], ".chain")
            page.screenshot(path=path("02-game-history.png"))

            visit("#/game/" + key["Elden Ring"], ".fork")
            page.screenshot(path=path("03-two-saves.png"))

            visit("#/games", ".game-list")
            page.click(".seg button:has-text('Library games')")
            settle()
            page.screenshot(path=path("04-library.png"))

            visit("#/settings", ".lib-card")
            page.screenshot(path=path("05-settings.png"))

            visit("#/devices", ".device-row")
            page.fill("form.row input", NEW_DEVICE)
            page.click("form.row button[type=submit]")
            page.wait_for_selector(".pair-code")
            page.locator(".device-row").nth(len(DEVICES)).wait_for()
            settle()
            page.screenshot(path=path("06-devices.png"))

            visit("#/storage", ".stats")
            page.click("button:has-text('Preview clean-up')")
            page.wait_for_selector("text=Would remove")
            settle()
            page.screenshot(path=path("07-storage.png"))
        finally:
            browser.close()


def shoot_guide(demo, out_dir):
    """The getting-started guide on an empty server: the first-run page and
    the first device with its pairing code."""
    from playwright.sync_api import sync_playwright

    os.makedirs(out_dir, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = new_context(browser, demo).new_page()

            def settle():
                page.evaluate("document.activeElement && document.activeElement.blur()")
                page.mouse.move(1430, 890)
                page.wait_for_timeout(400)

            page.goto(FAKE_ORIGIN + "/")
            page.wait_for_selector(".gate-intro")
            settle()
            page.screenshot(path=os.path.join(out_dir, "09-welcome.png"))

            page.fill("input[autocomplete=username]", USER)
            page.fill("input[autocomplete=new-password] >> nth=0", PASSWORD)
            page.fill("input[autocomplete=new-password] >> nth=1", PASSWORD)
            page.click("button[type=submit]")
            page.wait_for_selector("text=Where devices reach this server")
            page.fill(".step-panel input", "http://192.168.1.20:3900")
            page.click("button:has-text('Save and continue')")
            page.wait_for_selector(".options")
            page.click(".option[data-id=skip]")
            page.wait_for_selector("text=Add your first device")
            page.fill(".step-panel form input", GUIDE_DEVICE)
            page.click(".step-panel form button[type=submit]")
            page.wait_for_selector(".pair-code")
            settle()
            page.screenshot(path=os.path.join(out_dir, "10-first-device.png"))
        finally:
            browser.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--garage", default=os.environ.get("BLOCKSLOT_GARAGE"),
                        help="a Garage 2.1.0 binary (or set BLOCKSLOT_GARAGE)")
    parser.add_argument("--shots", metavar="DIR", help="write the screenshots here, then stop")
    parser.add_argument("--keep", action="store_true", help="keep the temp folder at the end")
    args = parser.parse_args(argv)
    if not args.garage or not os.path.isfile(args.garage):
        parser.error("give --garage, a Garage 2.1.0 binary")

    demo = Demo(os.path.abspath(args.garage), keep=args.keep)
    empty = None
    try:
        demo.start()
        demo.setup()
        demo.fill()
        games = demo.finish()
        print("%d games on the demo store" % len(games))
        if args.shots:
            shoot(demo, games, args.shots)
            demo.stop()
            empty = Demo(os.path.abspath(args.garage), keep=args.keep)
            empty.start()
            shoot_guide(empty, args.shots)
            print("Screenshots in %s" % args.shots)
        else:
            print("Open %s and sign in as %s / %s. Ctrl-C stops it." % (demo.url, USER, PASSWORD))
            try:
                while demo.proc.poll() is None:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
    finally:
        demo.stop()
        if empty is not None:
            empty.stop()


if __name__ == "__main__":
    main()
