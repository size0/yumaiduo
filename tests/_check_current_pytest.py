"""Run behavior tests on a disposable copy, without production data or network."""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


NETWORK_GUARD = '''import sys, socket
ports = set()
original_bind = socket.socket.bind
def bind(sock, address):
    result = original_bind(sock, address)
    if isinstance(address, tuple):
        ports.add(sock.getsockname()[1])
    return result
socket.socket.bind = bind
def guard(event, args):
    if event == "socket.getaddrinfo" and args[0] not in ("localhost", "127.0.0.1", "::1", None):
        raise RuntimeError("ISOLATED_TEST_EXTERNAL_NETWORK_DENIED")
    if event == "socket.connect":
        address = args[1]
        if isinstance(address, tuple) and not (
            address[0] in ("localhost", "127.0.0.1", "::1") and address[1] in ports
        ):
            raise RuntimeError("ISOLATED_TEST_EXTERNAL_NETWORK_DENIED")
sys.addaudithook(guard)
'''


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    python = Path(sys.executable)
    if importlib.util.find_spec("pytest") is None:
        python = root / ".checks-venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not python.is_file():
            print("FAIL: pytest is unavailable; provision the isolated .checks-venv first.")
            return 1

    with tempfile.TemporaryDirectory(prefix="ticket-agent-checks-") as tmp:
        work = Path(tmp) / "source"
        work.mkdir()
        for directory in ("application", "domain", "integrations", "prompts", "web", "knowledge"):
            if (root / directory).is_dir():
                shutil.copytree(root / directory, work / directory,
                                ignore=shutil.ignore_patterns("__pycache__", "node_modules", ".env"))
        for pattern in ("*.py", "*.md"):
            for path in root.glob(pattern):
                shutil.copy2(path, work / path.name)
        (work / "tests").mkdir()
        for path in (root / "tests").glob("*.py"):
            shutil.copy2(path, work / "tests" / path.name)
        if (root / "tests" / "fixtures").is_dir():
            shutil.copytree(root / "tests" / "fixtures", work / "tests" / "fixtures",
                            ignore=shutil.ignore_patterns("__pycache__"))
        # The historical migration is imported by a behavior test, never run
        # against its default database. That test supplies an in-memory store.
        migration = Path("release-wanda-no-ttl-20261016/migrate.py")
        (work / migration).parent.mkdir()
        shutil.copy2(root / migration, work / migration)
        guard = Path(tmp) / "guard"
        guard.mkdir()
        (guard / "sitecustomize.py").write_text(NETWORK_GUARD, encoding="utf-8")
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("PYTEST_") and not any(part in key.upper() for part in
                          ("TOKEN", "SECRET", "API_KEY", "WANDA", "LIANGPIAO", "TICKET_", "YUMAIDUO"))}
        env.update(PYTHONPATH=os.pathsep.join((str(guard), str(work))),
                   PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
                   PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                   TICKET_DATA_DIR=str(Path(tmp) / "backend-data"))
        print("Behavior checks: isolated source copy, empty data, Python TCP guarded.", flush=True)
        result = subprocess.run([str(python), "-m", "pytest", "tests", "-q"], cwd=work, env=env, timeout=300)
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
