#!/usr/bin/env python3
"""Local backend server for the daily job matcher.

Serves docs/ as static files and exposes /api/run + /api/test as
Server-Sent Events streams that subprocess-run the existing scripts.
The frontend (docs/index.html) detects localhost and calls these
endpoints directly instead of triggering GitHub Actions.

The cron schedule on GitHub Actions still runs job_matcher.py directly
from .github/workflows/daily-job-matcher.yml — this server is only
an alternate trigger path for local development.

Usage:
    python scripts/server.py
    # then open http://localhost:8080
"""

import os
import subprocess
import sys
from pathlib import Path

from flask import Flask, Response, request, send_from_directory

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR     = PROJECT_ROOT / "docs"
SCRIPTS_DIR  = PROJECT_ROOT / "scripts"

ALLOWED_MODES = {"search", "test-connection", "test-write"}

app = Flask(__name__, static_folder=None)


@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app.route("/api/ping")
def ping():
    return {"ok": True}


@app.route("/api/run")
def run():
    mode = request.args.get("mode", "search")
    if mode not in ALLOWED_MODES:
        return {"error": f"Invalid mode: {mode}"}, 400
    verbose = request.args.get("verbose", "0") == "1"
    return _stream_subprocess(
        [sys.executable, "-u", str(SCRIPTS_DIR / "job_matcher.py")],
        env_overrides={
            "RUN_MODE": mode,
            "JM_PROGRESS": "1",
            "JM_VERBOSE": "1" if verbose else "0",
        },
        cwd=str(SCRIPTS_DIR),
    )


@app.route("/api/test")
def test():
    verbose = request.args.get("verbose", "0") == "1"
    return _stream_subprocess(
        [sys.executable, "-u", "-m", "pytest", "tests/", "-v", "--tb=short"],
        env_overrides={"JM_PROGRESS": "1", "JM_VERBOSE": "1" if verbose else "0"},
        cwd=str(PROJECT_ROOT),
    )


@app.route("/", defaults={"path": "index.html"})
@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(str(DOCS_DIR), path)


def _stream_subprocess(cmd, env_overrides=None, cwd=None):
    def generate():
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONUTF8"]       = "1"
        for k, v in (env_overrides or {}).items():
            env[k] = v
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=cwd,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as exc:
            yield f"event: error\ndata: failed to start subprocess: {exc}\n\n"
            return
        for line in proc.stdout:
            yield f"event: line\ndata: {line.rstrip()}\n\n"
        proc.wait()
        yield f"event: done\ndata: {proc.returncode}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "close",
        },
    )


def main():
    port = int(os.environ.get("PORT", "8080"))
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"Daily job matcher UI: http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    app.run(host=host, port=port, debug=False, use_reloader=True, threaded=True)


if __name__ == "__main__":
    main()
