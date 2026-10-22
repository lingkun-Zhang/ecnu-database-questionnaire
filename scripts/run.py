"""Run the local API with a private, file-based graceful-stop handshake.

PowerShell 5 can read ``.runtime/backend.instance.json`` and atomically write
``backend.stop.json`` containing exactly ``pid`` and ``instanceToken`` from that
record. A PID alone never authorizes a stop. Tokens are not written to logs.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
MAX_CONTROL_BYTES = 4096
POLL_SECONDS = 0.2
STOP_KEYS = frozenset({"pid", "instanceToken"})


def _is_link(path):
    """Junctions also redirect directory access on Windows."""
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _safe_runtime(runtime):
    if _is_link(runtime):
        raise OSError("The backend runtime directory must not be a link.")
    runtime.mkdir(exist_ok=True)
    if _is_link(runtime) or not runtime.is_dir():
        raise OSError("The backend runtime directory is not a regular directory.")


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate control-file key.")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("Non-finite JSON value.")


def _read_control_with_identity(path):
    """Read only a small regular file; an invalid file is an absent signal."""
    try:
        if _is_link(path.parent) or _is_link(path):
            return None, None
        if not stat.S_ISREG(path.stat().st_mode):
            return None, None
        flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0))
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_CONTROL_BYTES:
                return None, None
            data = source.read(MAX_CONTROL_BYTES + 1)
            if len(data) > MAX_CONTROL_BYTES:
                return None, None
            if _is_link(path) or _identity(path.stat()) != _identity(info):
                return None, None
        value = json.loads(
            data.decode("utf-8-sig"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
        return (value, _identity(info)) if isinstance(value, dict) else (None, None)
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None, None


def read_control(path):
    return _read_control_with_identity(Path(path))[0]


def matches_stop(request, instance):
    """The strict two-field shape deliberately rejects future/unknown actions."""
    return (
        isinstance(request, dict)
        and isinstance(instance, dict)
        and frozenset(request) == STOP_KEYS
        and type(request.get("pid")) is int
        and type(instance.get("pid")) is int
        and request["pid"] > 0
        and request["pid"] == instance.get("pid")
        and isinstance(request.get("instanceToken"), str)
        and isinstance(instance.get("instanceToken"), str)
        and request["instanceToken"].isascii()
        and instance["instanceToken"].isascii()
        and len(request["instanceToken"]) == 43
        and len(instance["instanceToken"]) == 43
        and secrets.compare_digest(request["instanceToken"], instance["instanceToken"])
    )


def make_instance(project_root=None):
    return {
        "pid": os.getpid(),
        "launcherPid": os.getppid(),
        "instanceToken": secrets.token_urlsafe(32),
        "projectRoot": str(Path(ROOT if project_root is None else project_root).resolve()),
        "runPath": str(Path(__file__).resolve()),
        "pythonPath": str(Path(sys.executable).resolve()),
        "basePythonPath": str(Path(getattr(sys, "_base_executable", sys.executable)).resolve()),
        "startedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


@contextmanager
def _runtime_lock(runtime):
    """Serialize instance publishing and cleanup across controlled runners.

    The small lock file remains on disk; the OS releases its lock on a crash.
    It contains no credential and must not be removed to release a live lock.
    """
    _safe_runtime(runtime)
    lock_path = runtime / "backend.control.lock"
    if _is_link(lock_path):
        raise OSError("The backend control lock must not be a link.")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    with os.fdopen(descriptor, "r+b", buffering=0) as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode) or _is_link(lock_path):
            raise OSError("The backend control lock is not a regular file.")
        if os.fstat(lock.fileno()).st_size == 0:
            lock.write(b"0")
        deadline = time.monotonic() + 2
        while True:
            try:
                lock.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise OSError("The backend control files are busy.") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def publish_instance(runtime, instance):
    runtime = Path(runtime)
    data = json.dumps(instance, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_CONTROL_BYTES:
        raise ValueError("The backend instance record is too large.")
    with _runtime_lock(runtime):
        path = runtime / "backend.instance.json"
        if _is_link(path):
            raise OSError("The backend instance record must not be a link.")
        temporary = runtime / f".backend.instance.{instance['pid']}.{instance['instanceToken']}.tmp"
        created = False
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            descriptor = os.open(temporary, flags, 0o600)
            created = True
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            created = False
        finally:
            if created and not _is_link(temporary):
                temporary.unlink(missing_ok=True)


def _unlink_matching(path, predicate):
    record, identity = _read_control_with_identity(path)
    if record is None or not predicate(record):
        return False
    try:
        # A replaced file is a different owner's record even if its path is equal.
        if _is_link(path) or _identity(path.stat()) != identity:
            return False
        path.unlink()
        return True
    except OSError:
        return False


def cleanup_instance(runtime, instance):
    """A new runner's record and requests for another instance are preserved."""
    runtime = Path(runtime)
    with _runtime_lock(runtime):
        instance_removed = _unlink_matching(
            runtime / "backend.instance.json",
            lambda record: (
                record == instance
                and type(record.get("pid")) is int
                and type(record.get("launcherPid")) is int
            ),
        )
        stop_removed = _unlink_matching(
            runtime / "backend.stop.json", lambda record: matches_stop(record, instance)
        )
        return instance_removed, stop_removed


def watch_stop(server, runtime, instance, finished):
    while not finished.is_set():
        if matches_stop(read_control(Path(runtime) / "backend.stop.json"), instance):
            server.should_exit = True
            return
        finished.wait(POLL_SECONDS)


def run_server():
    # Importing this module for its control-file tests needs only the standard library.
    sys.path.insert(0, str(ROOT / "backend"))
    import uvicorn

    runtime = ROOT / ".runtime"
    instance = make_instance()
    publish_instance(runtime, instance)
    finished = threading.Event()
    watcher = None
    try:
        server = uvicorn.Server(uvicorn.Config("app.main:app", host="127.0.0.1", port=8000, log_level="info"))
        watcher = threading.Thread(
            target=watch_stop, args=(server, runtime, instance, finished), name="backend-stop", daemon=True
        )
        watcher.start()
        server.run()
    finally:
        finished.set()
        if watcher is not None:
            watcher.join(timeout=1)
        try:
            cleanup_instance(runtime, instance)
        except OSError:
            # Keeping an unverified record is safer than deleting a replacement.
            pass


if __name__ == "__main__":
    run_server()
