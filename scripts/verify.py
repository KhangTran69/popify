"""Run one explicit set of PoPify checks without hiding missing prerequisites."""

import argparse
import shutil
import subprocess
import sys
import platform


def run():
    parser = argparse.ArgumentParser(description="Run PoPify verification or the isolated preview.")
    parser.add_argument("target", nargs="?", default="local", choices=("local", "postgres", "demo", "preview", "reviewer"))
    parser.add_argument("--port", type=int, default=8002, help="Port for the disposable preview")
    args = parser.parse_args()

    if args.target in ("postgres", "demo"):
        if not shutil.which("docker"):
            print("UNAVAILABLE: Docker is not installed; start the local Compose stack to run this check.", file=sys.stderr)
            return 2
        daemon = subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if daemon.returncode:
            print("UNAVAILABLE: Docker daemon is not reachable; start it and the local Compose stack, then retry.", file=sys.stderr)
            return 2
        stack = subprocess.run(
            ["docker", "compose", "ps", "--services", "--status", "running"],
            capture_output=True, text=True,
        )
        if stack.returncode:
            print("UNAVAILABLE: Docker Compose could not inspect the local stack; check the Compose installation and project directory.", file=sys.stderr)
            return 2
        required = {"api", "db"} | ({"n8n"} if args.target == "demo" else set())
        missing = required - set(stack.stdout.splitlines())
        if missing:
            print("UNAVAILABLE: required Compose services are not running: " + ", ".join(sorted(missing)) + ".", file=sys.stderr)
            return 2

    commands = {
        "local": [[sys.executable, "-m", "pytest", "-q"],
                  [sys.executable, "-m", "scripts.validate_workflows"]],
        "postgres": [["docker", "compose", "exec", "-T", "api", "python", "-m", "scripts.verify_migrations"],
                     ["docker", "compose", "exec", "-T", "api", "python", "-m", "scripts.verify_postgres"]],
        "demo": [["docker", "compose", "exec", "-T", "api", "python", "-m", "scripts.demo"]],
        "preview": [[sys.executable, "-m", "scripts.preview", "--port", str(args.port)]],
        "reviewer": [[sys.executable, "-m", "pytest", "-q", "tests/test_preview_http.py"],
                     [sys.executable, "-m", "scripts.validate_workflows"]],
    }
    for command in commands[args.target]:
        print("Running: " + " ".join(command), flush=True)
        try:
            result = subprocess.run(command)
        except KeyboardInterrupt:
            print("Stopped by operator.", file=sys.stderr)
            return 130
        if result.returncode:
            print(f"FAILED: command exited {result.returncode}; later checks were not run.", file=sys.stderr)
            return result.returncode
    if args.target == "reviewer":
        revision = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True
        )
        commit = revision.stdout.strip() if revision.returncode == 0 else "unavailable"
        changed = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True)
        if changed.returncode == 0 and changed.stdout.strip():
            commit += " + uncommitted changes"
        docker_ready = bool(shutil.which("docker")) and subprocess.run(
            ["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ).returncode == 0
        print(
            f"PASS reviewer walkthrough | commit {commit} | Python {platform.python_version()} "
            "| disposable SQLite preview | ambiguous execution, operator assertion, "
            "fresh approval, persisted mock result, conflicting later evidence "
            "| three workflow exports valid | live n8n not exercised "
            f"({'Docker available' if docker_ready else 'Docker daemon unavailable'})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(run())
