"""Run with python3 Tests/integration.py from the project directory after a release build."""
import os
from pathlib import Path
import pty
import signal
import subprocess
import sys
import tempfile
import time

binary = Path(".build/release/process-guard").resolve()
source = Path("Sources/ProcessGuard/main.swift").read_text()


def until(predicate):
    for _ in range(500):
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("timed out waiting for child state")


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    # Replace only the sensor in an isolated build; signals and processes remain real.
    sensor = root / "state"
    sensor.write_text("2")
    injected = 'Int((try? String(contentsOfFile: "' + str(sensor) + '")) ?? "") ?? 2'
    main = root / "main.swift"
    main.write_text(source.replace("ProcessInfo.processInfo.thermalState.rawValue", injected))
    exe = root / "guard"
    subprocess.run(["swiftc", str(main), "-module-cache-path", str(root / "cache"), "-o", str(exe)], check=True)
    program = """
import os, time
from pathlib import Path
base = Path(os.environ['PROCESS_GUARD_CHECKPOINT_DIR'])
name = 'parent' if os.fork() else 'worker'
while True:
    with (base/name).open('ab') as f: f.write(b'.')
    time.sleep(.01)
"""
    run = subprocess.Popen([exe, "--max-thermal", "fair", "--interval", ".02",
                            "--checkpoint-dir", temp, "--", sys.executable, "-c", program],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    files = [root / "parent", root / "worker"]
    def sizes():
        return [p.stat().st_size for p in files]
    def level(n):
        replacement = root / "next"
        replacement.write_text(str(n))
        replacement.replace(sensor)
    try:
        assert b"WAITING" in run.stderr.readline()
        level(1)
        time.sleep(.12)
        assert not files[0].exists()
        level(0)
        until(lambda: all(p.exists() for p in files))
        for cycle in range(2):
            level(1)
            before = sizes()
            until(lambda: all(a > b for a, b in zip(sizes(), before)))
            level(2)
            until(lambda: b"PAUSED" in run.stderr.readline())
            time.sleep(.05)
            stopped = sizes()
            level(1)
            time.sleep(.12)
            assert sizes() == stopped
            if cycle == 0:
                level(0)
                until(lambda: all(a > b for a, b in zip(sizes(), stopped)))
        run.send_signal(signal.SIGINT)
        assert run.wait(timeout=5) in (130, 1)
        assert stopped == sizes()
    finally:
        if run.poll() is None:
            run.terminate()
            run.wait(timeout=5)

    script = root / "train.py"
    script.write_text("""import os,sys
from pathlib import Path
p = Path(os.environ['PROCESS_GUARD_CHECKPOINT_DIR'])/'epoch'
print(p.read_text() if p.exists() else 'new', flush=True)
p.write_text('saved')
if sys.argv[-1]=='crash': os.kill(os.getpid(), 9)
sys.exit(17)
""")
    command = [binary, "--max-thermal", "fair", "--checkpoint-dir", "./checkpoints/cnn",
               "--", "python3", "train.py"]
    for extra, output, code in [(["crash"], b"new", 137), ([], b"saved", 17)]:
        result = subprocess.run(command + extra, cwd=root, capture_output=True, timeout=30)
        assert (result.stdout.strip(), result.returncode) == (output, code)
        assert b"\x1b[" not in result.stderr
    bad = subprocess.run([binary, "--checkpoint-dir", "/dev/null/checkpoints", "--", "/bin/true"],
                         capture_output=True)
    assert bad.returncode and b"ERROR" in bad.stderr
    for term, no_color, colored in [("xterm", "", True), ("xterm", "1", False), ("dumb", "", False)]:
        master, slave = pty.openpty()
        try:
            subprocess.run([binary, "--invalid"], stderr=slave,
                           env=dict(os.environ, TERM=term, NO_COLOR=no_color))
            assert (b"\x1b[" in os.read(master, 4096)) == colored
        finally:
            os.close(master)
            os.close(slave)
print("Integration checks passed.")
