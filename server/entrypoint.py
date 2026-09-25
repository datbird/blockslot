"""entrypoint - start and watch the two processes of the BlockSlot server.

    garage server        the S3 store: devices on :3900, admin API on 127.0.0.1:3903
    blockslot web app    the web UI and its JSON API on :8761

First start: writes /data/garage.toml with fresh secrets, starts Garage,
gives the single node its layout, creates the "blockslot" bucket and a key
for the web app itself. A /data/garage.toml that already exists is used as
it is (adopting an existing Garage; see blockslot_server/garage.py for how
its paths are mapped without editing it).

Either process that dies is started again, with a growing pause so a crash
loop does not spin. SIGTERM stops both, which is what `docker stop` sends.

Environment, all optional:
    BLOCKSLOT_DATA         /data
    BLOCKSLOT_RUN          /run/blockslot   (the runtime Garage config)
    BLOCKSLOT_GARAGE       /usr/local/bin/garage
    BLOCKSLOT_S3_PORT      3900   } used only when writing a new garage.toml;
    BLOCKSLOT_RPC_PORT     3901   } an adopted one keeps its own ports
    BLOCKSLOT_ADMIN_PORT   3903   }
    BLOCKSLOT_WEB_PORT     8761
"""

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import blockslot_server  # noqa: E402
from blockslot_server import garage  # noqa: E402

log = logging.getLogger("blockslot.entrypoint")

STOP_GRACE = 10
BACKOFF_FIRST = 1
BACKOFF_MAX = 30
HEALTHY_AFTER = 60


def env_int(name, default):
    return int(os.environ.get(name) or default)


def prepare(data_dir, run_dir, s3_port=3900, rpc_port=3901, admin_port=3903):
    """Write or adopt garage.toml and the runtime copy Garage starts with.

    Returns {"runtime", "admin_url", "admin_token", "s3_url", "adopted"}.
    """
    os.makedirs(data_dir, exist_ok=True)
    toml = os.path.join(data_dir, "garage.toml")
    adopted = os.path.isfile(toml)
    if adopted:
        log.info("using the existing %s", toml)
    else:
        for folder in ("meta", "data"):
            os.makedirs(os.path.join(data_dir, folder), exist_ok=True)
        garage.write_new_config(toml, data_dir, s3_port=s3_port, rpc_port=rpc_port,
                                admin_port=admin_port)
        log.info("first start: wrote %s with new secrets", toml)
    with open(toml, "r", encoding="utf-8") as handle:
        text = handle.read()
    runtime_text, port = garage.runtime_config(text, data_dir)
    parsed = garage.tomllib.loads(runtime_text)
    for field in ("metadata_dir", "data_dir"):
        if not os.path.isdir(parsed[field]):
            raise SystemExit("garage.toml names %s = %s, which is not in this container. "
                             "Mount the folder that holds garage.toml, meta/ and data/ "
                             "at %s." % (field, parsed[field], data_dir))
    os.makedirs(run_dir, exist_ok=True)
    runtime = os.path.join(run_dir, "garage.toml")
    fd = os.open(runtime, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(runtime_text)
    return {"runtime": runtime, "admin_url": "http://127.0.0.1:%d" % port,
            "admin_token": garage.admin_token(text),
            "s3_url": "http://127.0.0.1:%d" % garage.s3_port(text), "adopted": adopted}


def _server_json(data_dir):
    return os.path.join(data_dir, "server.json")


def _load_server(data_dir):
    try:
        with open(_server_json(data_dir), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}


def _save_server(data_dir, data):
    path = _server_json(data_dir)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, sort_keys=True, indent=1)
    os.replace(tmp, path)


def bootstrap(admin, data_dir, timeout=90):
    """Layout, bucket and the web app's own key. Safe to run on every start.

    Waits for Garage to answer first: it takes a moment to open its database.
    A layout change is not usable the instant it is applied, so the bucket
    step is retried until Garage accepts it.
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            admin.status()
            break
        except garage.GarageError as exc:
            last = exc
            time.sleep(0.5)
    else:
        raise garage.GarageError("Garage did not answer within %d s: %s" % (timeout, last))
    capacity = shutil.disk_usage(data_dir).total
    if admin.ensure_layout(capacity):
        log.info("assigned the single-node layout")
    while True:
        try:
            bucket_id, made = admin.ensure_bucket(garage.BUCKET)
            break
        except garage.GarageError as exc:
            if time.time() > deadline:
                raise
            last = exc
            time.sleep(1)
    if made:
        log.info("created the bucket %s", garage.BUCKET)
    data = _load_server(data_dir)
    s3 = data.get("s3") or {}
    known = {key.get("id") for key in admin.list_keys()}
    if not s3.get("access_key") or s3["access_key"] not in known:
        key_id, secret = admin.create_key(garage.SERVER_KEY_NAME, bucket_id, owner=True)
        data["s3"] = {"access_key": key_id, "secret_key": secret}
        _save_server(data_dir, data)
        log.info("made the web app's own key")
    return bucket_id


class Child(object):
    """One supervised process."""

    def __init__(self, name, argv, env=None, cwd=None):
        self.name = name
        self.argv = argv
        self.env = env
        self.cwd = cwd
        self.proc = None
        self.started = 0
        self.backoff = BACKOFF_FIRST
        self.next_start = 0

    def start(self):
        log.info("starting %s", self.name)
        self.proc = subprocess.Popen(self.argv, env=self.env, cwd=self.cwd)
        self.started = time.time()

    def check(self, now):
        """Start it, or start it again if it died and its pause is over."""
        if self.proc is not None and self.proc.poll() is None:
            if now - self.started > HEALTHY_AFTER:
                self.backoff = BACKOFF_FIRST
            return
        if self.proc is not None:
            log.warning("%s exited with code %s; starting it again in %d s",
                        self.name, self.proc.returncode, self.backoff)
            self.proc = None
            self.next_start = now + self.backoff
            self.backoff = min(self.backoff * 2, BACKOFF_MAX)
        if now >= self.next_start:
            self.start()

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()

    def wait(self, until):
        if self.proc is None:
            return
        try:
            self.proc.wait(timeout=max(0.1, until - time.time()))
        except subprocess.TimeoutExpired:
            log.warning("%s did not stop in time; killing it", self.name)
            self.proc.kill()
            self.proc.wait()


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s entrypoint %(levelname)s %(message)s")
    log.info("BlockSlot server %s", blockslot_server.__version__)
    data_dir = os.environ.get("BLOCKSLOT_DATA", "/data")
    run_dir = os.environ.get("BLOCKSLOT_RUN", "/run/blockslot")
    binary = os.environ.get("BLOCKSLOT_GARAGE", "/usr/local/bin/garage")
    info = prepare(data_dir, run_dir,
                   s3_port=env_int("BLOCKSLOT_S3_PORT", 3900),
                   rpc_port=env_int("BLOCKSLOT_RPC_PORT", 3901),
                   admin_port=env_int("BLOCKSLOT_ADMIN_PORT", 3903))
    stop = threading.Event()

    def on_signal(signum, _frame):
        log.info("signal %d: stopping", signum)
        stop.set()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    garage_env = dict(os.environ, RUST_LOG=os.environ.get("RUST_LOG", "garage=info"))
    store = Child("garage", [binary, "-c", info["runtime"], "server"], env=garage_env)
    web_env = dict(os.environ, BLOCKSLOT_DATA=data_dir,
                   BLOCKSLOT_ADMIN_URL=info["admin_url"],
                   BLOCKSLOT_ADMIN_TOKEN=info["admin_token"],
                   BLOCKSLOT_S3_URL=info["s3_url"],
                   PYTHONPATH=HERE + os.pathsep + os.environ.get("PYTHONPATH", ""))
    web = Child("web app", [sys.executable, "-m", "blockslot_server.webapp"],
                env=web_env, cwd=HERE)
    admin = garage.GarageAdmin(info["admin_url"], info["admin_token"])
    ready = False
    next_try = 0
    while not stop.is_set():
        now = time.time()
        store.check(now)
        if not ready and now >= next_try:
            try:
                bootstrap(admin, data_dir, timeout=30)
                ready = True
            except garage.GarageError as exc:
                log.error("setting up Garage failed, retrying in 10 s: %s", exc)
                next_try = time.time() + 10
        if ready:
            web.check(now)
        stop.wait(1)
    for child in (web, store):
        child.stop()
    until = time.time() + STOP_GRACE
    for child in (web, store):
        child.wait(until)
    log.info("stopped")


if __name__ == "__main__":
    main()
