"""The engine that ships with Blockslot, and the copy installed on this device.

Both faces install it and both say whether the installed copy is current. The
rule lives here so that one of them cannot quietly install it differently.
"""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import paths

# The ludusavi release a device downloads when it has none: the same version
# decky/package.json pins for the Deck. Each hash is the one GitHub publishes
# for that asset, and each was checked against a download of it.
LUDUSAVI_VERSION = "0.31.0"
_RELEASE = "https://github.com/mtkennerly/ludusavi/releases/download/v0.31.0/"
LUDUSAVI_WINDOWS_URL = _RELEASE + "ludusavi-v0.31.0-win64.zip"
LUDUSAVI_WINDOWS_SHA256 = (
    "f47a8ad8c708f01d2eb124704973beffab205e292f5287a10fc4a101f8d68706")
LUDUSAVI_WINDOWS_MEMBER = "ludusavi.exe"
# Upstream publishes one Linux build, x86-64, and one Mac build, which is
# arm64 only (a Mach-O for Apple silicon, checked in the tarball). So an Intel
# Mac and an ARM Linux box have no official file to fetch: Rosetta runs Intel
# code on Apple silicon, never the other way round.
LUDUSAVI_ASSETS = {
    ("windows", "x64"): (LUDUSAVI_WINDOWS_URL, LUDUSAVI_WINDOWS_SHA256),
    ("linux", "x64"): (
        _RELEASE + "ludusavi-v0.31.0-linux.tar.gz",
        "7322ff45d41eae7ae064a80d8c9ecccc5b8fb6fc090a603a66369cd4b054068d"),
    ("mac", "arm64"): (
        _RELEASE + "ludusavi-v0.31.0-mac.tar.gz",
        "5787e64d4c795180ab485535cae0b0ef6ee14d4fbe3813797a86913a37cc47f1"),
}
DOWNLOAD_TIMEOUT = 60
# The manifest is ludusavi's list of where every game keeps its saves.
MANIFEST_TIMEOUT = 120

# The store library and the daemon travel with the engine and are installed
# beside it: savepick imports them from its own folder.
COMPANIONS = ("slotstore.py", "slotd.py", "saveunits.py")


def source(extra=None):
    """The engine that came with this copy of Blockslot, or None.

    `extra` is a path to try first, for a face that carries the engine
    somewhere `paths.resource` does not look.
    """
    if extra is not None and extra.is_file():
        return extra
    return paths.resource("engine/" + paths.ENGINE_NAME)


def is_current(shipped):
    """True when the installed engine, and each companion shipped with it,
    is byte for byte the shipped one."""
    installed = paths.engine_path()
    try:
        pairs = [(installed, shipped)] + [
            (installed.parent / name, shipped.parent / name)
            for name in COMPANIONS if (shipped.parent / name).is_file()]
        for have, want in pairs:
            if have.stat().st_size != want.stat().st_size:
                return False
            if have.read_bytes() != want.read_bytes():
                return False
        return True
    except (OSError, AttributeError):
        return False


def install(shipped):
    """Copy the shipped engine to where Steam will start it. Raises OSError."""
    target = paths.engine_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    for name in COMPANIONS:
        if (shipped.parent / name).is_file():
            shutil.copy2(str(shipped.parent / name), str(target.parent / name))
    shutil.copy2(str(shipped), str(target))
    # Steam starts it through python, but a person may start it by hand.
    os.chmod(str(target), 0o755)
    return target


class AlreadyThere(Exception):
    """ludusavi is already installed, and is never replaced from here."""


class WrongFile(OSError):
    """The download is not the file that was pinned."""


def sha256_of(path):
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class NoRelease(OSError):
    """ludusavi publishes no build for this kind of machine."""


def this_machine(system=None, machine=None, translated=None):
    """("windows" | "linux" | "mac", "x64" | "arm64" | the raw name).

    A Mac running an Intel python under Rosetta reports x86_64, but the
    hardware is Apple silicon and runs the arm64 build natively, so the
    kernel's own answer is asked for.
    """
    import platform
    system = system or ("win32" if paths.is_windows() else sys.platform)
    machine = (machine or platform.machine() or "").lower()
    if system == "win32":
        # Windows on ARM runs the x64 build through its own emulation.
        return "windows", "x64"
    kind = "mac" if system == "darwin" else "linux"
    if machine in ("x86_64", "amd64", "x64"):
        arch = "x64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        arch = machine
    if kind == "mac" and arch == "x64":
        if translated is None:
            translated = _rosetta()
        if translated:
            arch = "arm64"
    return kind, arch


def _rosetta():
    try:
        done = subprocess.run(["sysctl", "-n", "sysctl.proc_translated"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.stdout.strip() == "1"


def ludusavi_asset(kind=None):
    """(url, sha256) of the official release for this machine.

    Raises NoRelease with a sentence a person can act on.
    """
    kind = kind or this_machine()
    found = LUDUSAVI_ASSETS.get(kind)
    if found:
        return found
    system, arch = kind
    if system == "mac":
        raise NoRelease("ludusavi publishes its Mac build for Apple silicon "
                        "only, and this Mac is Intel. Install ludusavi %s or "
                        "newer yourself, at %s."
                        % (LUDUSAVI_VERSION, paths.ludusavi_path()))
    raise NoRelease("ludusavi publishes no %s build for %s. Install ludusavi "
                    "%s or newer yourself, at %s."
                    % (system.title(), arch, LUDUSAVI_VERSION,
                       paths.ludusavi_path()))


def download_ludusavi(url=None, sha256=None, say=None, opener=None):
    """Fetch ludusavi's official release for this machine, check it, and
    install it. With no `url`, ludusavi_asset picks the file and its hash.

    Only the pinned file is accepted: the archive has to hash to `sha256`
    before anything is taken out of it. A ludusavi that is already there is
    never replaced, and that is decided before anything is downloaded.

    `opener(url)` returns something with read(); the default is urllib over a
    verifying TLS context. `say` hears progress in plain words. Raises
    AlreadyThere, or OSError with a sentence a person can act on.
    """
    say = say or (lambda _text: None)
    target = paths.ludusavi_path()
    if target.exists():
        raise AlreadyThere("ludusavi is already installed at %s" % target)
    if url is None:
        url, sha256 = ludusavi_asset()
    if not sha256:
        raise ValueError("a ludusavi download needs the hash it is pinned to")
    target.parent.mkdir(parents=True, exist_ok=True)
    download = target.parent / "ludusavi-download.blockslot.tmp"
    say("Downloading ludusavi %s ..." % LUDUSAVI_VERSION)
    try:
        try:
            response = (opener or _open)(url)
        except OSError as exc:
            raise OSError("Could not download ludusavi. Check that this "
                          "device is online. (%s)" % _reason(exc))
        got = 0
        with response, open(str(download), "wb") as out:
            while True:
                try:
                    block = response.read(1 << 16)
                except OSError as exc:
                    raise OSError("The ludusavi download stopped part way. "
                                  "Try again. (%s)" % _reason(exc))
                if not block:
                    break
                out.write(block)
                got += len(block)
                if got % (1 << 21) < len(block):
                    say("Downloading ludusavi ... %.0f MB" % (got / 1048576.0))
        say("Checking the download ...")
        install_ludusavi(download, sha256=sha256)
    finally:
        if download.exists():
            download.unlink()
    say("Installed ludusavi at %s" % target)
    return target


def ludusavi_config_dir(binary=None):
    """Where the engine's ludusavi keeps its config and manifest.

    ludusavi's own rule, as seen by the engine: beside the binary when it runs
    portable, else the platform's config folder, which for a Steam snap is
    inside the snap's home (savepick.ludusavi_config_dir, from the inside).
    """
    binary = Path(binary or paths.ludusavi_path())
    if (binary.parent / "ludusavi.portable").is_file():
        return binary.parent
    if paths.is_windows():
        return Path(os.environ.get("APPDATA") or str(paths.home())) / "ludusavi"
    if paths.is_mac():
        return paths.home() / "Library" / "Application Support" / "ludusavi"
    sandbox = paths.steam_sandbox_home()
    if sandbox is not None:
        return sandbox / ".config" / "ludusavi"
    base = os.environ.get("XDG_CONFIG_HOME") or str(paths.home() / ".config")
    return Path(base) / "ludusavi"


def update_ludusavi_manifest(say=None, run=None):
    """Download ludusavi's manifest, the list of where each game saves.

    The engine only ever runs ludusavi with --no-manifest-update, so that a
    launch never waits on the network. A ludusavi that has never fetched it
    knows no game at all. True once the manifest is there.
    """
    say = say or (lambda _text: None)
    binary = paths.ludusavi_path()
    folder = ludusavi_config_dir(binary)
    command = [str(binary)]
    if paths.steam_sandbox_home() is not None:
        # This window runs outside the snap, where ludusavi would read and
        # write the real home's config. The engine's copy is inside it.
        command += ["--config", str(folder)]
    command += ["manifest", "update", "--force"]
    say("Downloading ludusavi's list of games ...")
    try:
        done = (run or subprocess.run)(
            command, capture_output=True, text=True, timeout=MANIFEST_TIMEOUT,
            stdin=subprocess.DEVNULL, **paths.no_window())
    except (OSError, subprocess.SubprocessError) as exc:
        say("Could not download ludusavi's list of games: %s" % exc)
        return False
    if done.returncode != 0 or not (folder / "manifest.yaml").is_file():
        say("Could not download ludusavi's list of games. The first game "
            "you start will try again.")
        return False
    say("ludusavi's list of games is in place.")
    return True


def _open(url):
    import urllib.request
    context = None
    try:
        from . import settings as settings_mod
        context = settings_mod.engine_module("slotstore").tls_context()
    except ImportError:
        # Without the engine's helper, urllib's own default still verifies.
        context = None
    return urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT,
                                  context=context)


def _reason(exc):
    return getattr(exc, "reason", None) or exc


def install_ludusavi(archive, sha256=None):
    """Unpack ludusavi's own release archive to where the engine looks for it.

    `archive` is the .tar.gz or the Windows .zip that ludusavi publishes,
    exactly as downloaded. Blockslot does not carry a copy of ludusavi: each
    device fetches the official file, and this only unpacks it. With `sha256`
    the archive must hash to it first, or WrongFile is raised and nothing is
    written.

    An installed ludusavi is left alone. It may be newer than the pinned
    release, or built by hand, and replacing it would change how saves are
    read on a device that is already syncing.
    """
    target = paths.ludusavi_path()
    if target.exists():
        raise AlreadyThere("ludusavi is already installed at %s" % target)
    if sha256 is not None and sha256_of(archive) != sha256.lower():
        raise WrongFile("The ludusavi download is not the expected file, so "
                        "it was not installed. Try again later.")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.parent / (target.name + ".blockslot.tmp")
    try:
        if str(archive).lower().endswith(".zip") or _is_zip(archive):
            _unzip_member(archive, LUDUSAVI_WINDOWS_MEMBER, temp)
        else:
            _unpack_member(archive, target.name, temp)
        os.chmod(str(temp), 0o755)
        os.replace(str(temp), str(target))
    finally:
        if temp.exists():
            temp.unlink()
    return target


def _is_zip(archive):
    import zipfile
    try:
        return zipfile.is_zipfile(str(archive))
    except OSError:
        return False


def _unzip_member(archive, name, destination):
    """Write one file out of a .zip. Raises OSError when it is not there."""
    import zipfile
    try:
        with zipfile.ZipFile(str(archive)) as bundle:
            try:
                member = bundle.open(name)
            except KeyError:
                raise OSError("%s holds no file called %s" % (archive, name))
            with member, open(str(destination), "wb") as out:
                shutil.copyfileobj(member, out)
    except zipfile.BadZipFile as exc:
        raise OSError("could not unpack %s: %s" % (archive, exc))


def _unpack_member(archive, name, destination):
    """Write one file out of a .tar.gz. Raises OSError when it is not there."""
    try:
        import tarfile
    except ImportError:
        # Decky's python is a frozen build with part of the standard library
        # left out. SteamOS always has tar.
        with open(str(destination), "wb") as out:
            done = subprocess.run(["tar", "-xzOf", str(archive), name],
                                  stdout=out, stderr=subprocess.PIPE)
        if done.returncode != 0 or not destination.stat().st_size:
            raise OSError("tar could not read %s from %s" % (name, archive))
        return
    try:
        with tarfile.open(str(archive), "r:gz") as bundle:
            member = bundle.extractfile(name)
            if member is None:
                raise OSError("%s holds no file called %s" % (archive, name))
            with open(str(destination), "wb") as out:
                shutil.copyfileobj(member, out)
    except (tarfile.TarError, KeyError) as exc:
        raise OSError("could not unpack %s: %s" % (archive, exc))
