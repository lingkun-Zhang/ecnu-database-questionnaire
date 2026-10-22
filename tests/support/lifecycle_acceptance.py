"""Opt-in Windows start/stop acceptance against an explicitly named local project.

Runs the shipped scripts, fingerprints existing business data, and restores the
service at the end. It creates no users, questionnaires, versions or responses.
The temporary health stub belongs to this process and never writes to MongoDB.
"""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
from urllib.error import URLError
from urllib.request import urlopen

from lifecycle_snapshot import snapshot


def tcp_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def health():
    try:
        with urlopen("http://127.0.0.1:8000/api/health", timeout=3) as response:
            return json.load(response)
    except (OSError, URLError, ValueError):
        return None


class ForeignHealth(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"status":"ok","database":"MongoDB","fixture":"unrelated-health-service"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def main(project, evidence):
    powershell = shutil.which("powershell.exe")
    if not powershell:
        raise RuntimeError("Windows PowerShell is required.")
    date = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    evidence.mkdir(parents=True, exist_ok=True)
    report_path = evidence / f"lifecycle-integration-{date}.json"
    report = {"date": date, "passed": False, "steps": [], "newBusinessDocuments": 0}

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def run(name, script, expected_success=True):
        log = evidence / f"lifecycle-{name}-{date}.txt"
        arguments = [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                     "-File", str(project / "scripts" / script)]
        if script == "start.ps1":
            arguments.append("-NoBrowser")
        started = time.monotonic()
        # File streams prevent an inherited pipe in a background console host
        # from keeping a completed PowerShell command's capture pipe open.
        with log.open("wb") as output:
            completed = subprocess.run(arguments, stdin=subprocess.DEVNULL, stdout=output,
                                       stderr=subprocess.STDOUT, timeout=100,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ok = (completed.returncode == 0) == expected_success
        report["steps"].append({"name": name, "exitCode": completed.returncode,
                                "expectedSuccess": expected_success, "passed": ok,
                                "seconds": round(time.monotonic() - started, 3), "log": log.name})
        save()
        print(f"{name}: {'PASS' if ok else 'FAIL'} (exit {completed.returncode})", flush=True)
        if not ok:
            raise AssertionError(f"{name} failed; see {log.name}")

    def instance():
        return json.loads((project / ".runtime/backend.instance.json").read_text(encoding="utf-8-sig"))

    def record_snapshot(name):
        value = asyncio.run(snapshot(project))
        (evidence / f"lifecycle-{name}-{date}.json").write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        return value

    try:
        run("initial-start", "start.ps1")
        assert health()["status"] == "ok"
        before = record_snapshot("before")
        original = instance()
        run("repeat-start", "start.ps1")
        assert instance() == original, "Repeated start changed the running instance."
        report["repeatStartKeptInstance"] = True
        run("stop", "stop.ps1")
        assert not tcp_open(8000) and not tcp_open(27117)
        assert not (project / ".runtime/backend.instance.json").exists()
        assert not (project / ".runtime/backend.stop.json").exists()
        report["portsClosedAfterStop"] = [8000, 27117]
        backend_log = (project / ".runtime/backend-error.log").read_text(encoding="utf-8", errors="replace")
        assert "Application shutdown complete" in backend_log and "Finished server process" in backend_log
        (evidence / f"lifecycle-graceful-backend-{date}.txt").write_text(backend_log, encoding="utf-8")
        mongo_events = []
        for line in (project / ".runtime/mongod.log").read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if "shutdown" in item.get("msg", "").lower() or item.get("msg") == "WiredTiger closed":
                mongo_events.append({"message": item["msg"], "exitCode": item.get("attr", {}).get("exitCode")})
        assert any(item["message"] == "mongod shutdown complete" for item in mongo_events)
        (evidence / f"lifecycle-graceful-mongo-{date}.json").write_text(
            json.dumps(mongo_events[-30:], ensure_ascii=False, indent=2), encoding="utf-8")
        run("repeat-stop", "stop.ps1")
        assert not tcp_open(8000) and not tcp_open(27117)

        # Both scripts must reject a healthy HTTP service belonging to neither
        # the project runner nor its instance record, and leave it alive.
        stub = ThreadingHTTPServer(("127.0.0.1", 8000), ForeignHealth)
        thread = threading.Thread(target=stub.serve_forever, daemon=True)
        thread.start()
        try:
            assert health()["fixture"] == "unrelated-health-service"
            run("foreign-start-refused", "start.ps1", expected_success=False)
            run("foreign-stop-refused", "stop.ps1", expected_success=False)
            assert health()["fixture"] == "unrelated-health-service" and not tcp_open(27117)
            report["foreignHealthServicePreserved"] = True
        finally:
            stub.shutdown()
            stub.server_close()
            thread.join(timeout=3)

        stale = {"pid": original["pid"], "instanceToken": "stale_" + "x" * 37}
        (project / ".runtime/backend.stop.json").write_text(json.dumps(stale), encoding="utf-8")
        run("restart", "start.ps1")
        current = instance()
        assert current["instanceToken"] != original["instanceToken"]
        assert health()["status"] == "ok"
        # A successful startup is followed by several watcher polls during the
        # snapshot; the old request must remain harmless, rather than be deleted.
        after = record_snapshot("after")
        assert health()["status"] == "ok" and instance() == current
        assert json.loads((project / ".runtime/backend.stop.json").read_text()) == stale
        report["staleSignalIgnored"] = True
        assert before == after, "Business data or configuration changed across restart."
        # The next legitimate stop must atomically replace the stale request,
        # then the runner must consume only its own matching signal.
        run("stop-after-stale", "stop.ps1")
        assert not tcp_open(8000) and not tcp_open(27117)
        assert not (project / ".runtime/backend.stop.json").exists()
        report["staleSignalSafelyReplaced"] = True
        run("final-start", "start.ps1")
        final = record_snapshot("final")
        assert final == before and health()["status"] == "ok"
        report["dataAndConfigurationUnchanged"] = True
        report["counts"] = {name: item["count"] for name, item in after["collections"].items()}
        report["gracefulBackendLogVerified"] = True
        report["gracefulMongoLogVerified"] = True
        report["finalHealth"] = health()
        report["passed"] = True
        save()
        print("Data/configuration fingerprints match; service is running.", flush=True)
    except BaseException as error:
        report["error"] = f"{type(error).__name__}: {error}"
        save()
        raise
    finally:
        if not health():
            try:
                run("restore-start", "start.ps1")
            except BaseException:
                print("Service restoration needs attention; see acceptance logs.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    main(args.project.resolve(), args.evidence.resolve())
