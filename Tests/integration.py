"""Run with python3 Tests/integration.py from the project directory after a release build."""
import os
import errno
import fcntl
from pathlib import Path
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
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


def open_terminal():
    master, slave = pty.openpty()
    mode = termios.tcgetattr(slave)
    mode[1] &= ~termios.OPOST
    termios.tcsetattr(slave, termios.TCSANOW, mode)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    return master, slave


class TerminalRun:
    """Capture a real terminal without blocking a writer on a full PTY buffer."""
    def __init__(self, exe, root, program, stdout="terminal", env=None, processed=False):
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.base = Path(self.temp.name)
        self.master, slave = open_terminal()
        if processed:
            mode = termios.tcgetattr(slave)
            mode[1] |= termios.OPOST | termios.ONLCR
            termios.tcsetattr(slave, termios.TCSANOW, mode)
        prelude = """
import os, signal, sys, time
from pathlib import Path
base = Path(sys.argv[1])
def wait(name):
    while not (base/name).exists(): time.sleep(.005)
wait('go')
"""
        try:
            self.process = subprocess.Popen(
                [exe, "--max-thermal", "fair", "--interval", ".02",
                 "--", sys.executable, "-c", prelude + program, self.base],
                stdin=subprocess.DEVNULL,
                stdout=slave if stdout == "terminal" else stdout, stderr=slave,
                env={**os.environ, "TERM": "xterm", "NO_COLOR": "1", **(env or {})})
        finally:
            os.close(slave)
        os.set_blocking(self.master, False)
        self.data = bytearray()
        self.eof = False

    def pump(self, timeout=.02):
        if self.eof or not select.select([self.master], [], [], timeout)[0]:
            return False
        try:
            chunk = os.read(self.master, 65536)
        except OSError as error:
            if error.errno != errno.EIO:
                raise
            chunk = b""
        if not chunk:
            self.eof = True
            return False
        self.data.extend(chunk)
        return True

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate():
            assert time.monotonic() < deadline, bytes(self.data[-4096:])
            self.pump()

    def release(self):
        self.wait_for(lambda: b" START " in ansi.sub(b"", self.data))
        (self.base / "go").touch()

    def finish(self):
        self.wait_for(lambda: self.process.poll() is not None, timeout=15)
        while self.pump():
            pass
        return bytes(self.data)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        try:
            if self.process.poll() is None:
                self.process.terminate()
                self.wait_for(lambda: self.process.poll() is not None)
            if self.process.stdout:
                self.process.stdout.close()
        finally:
            os.close(self.master)
            self.temp.cleanup()


ansi = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


def assert_block(data, child):
    # Ignore styling only when counting existing line endings, never in payloads.
    visible = ansi.sub(b"", child).replace(b"\r", b"")
    trailing = min(2, len(visible) - len(visible.rstrip(b"\n")))
    start = re.match(rb"\A[^\n]+ START [^\n]+\n\n", data)
    assert start, data[:4096]
    end = start.end() + len(child)
    assert data[start.end():end] == child, data[-4096:]
    assert re.fullmatch(b"\n" * (2 - trailing) + rb"[^\n]+ EXIT [^\n]+\n", data[end:]), data[-4096:]


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    # Replace only the sensor in an isolated build; signals and processes remain real.
    sensor = root / "state"
    sensor.write_text("2")
    injected = 'Int((try? String(contentsOfFile: "' + str(sensor) + '", encoding: .utf8)) ?? "") ?? 2'
    main = root / "main.swift"
    main.write_text(source.replace("ProcessInfo.processInfo.thermalState.rawValue", injected))
    exe = root / "guard"
    subprocess.run(["swiftc", str(main), "-module-cache-path", str(root / "cache"), "-o", str(exe)], check=True)
    program = """
import os, sys, time
from pathlib import Path
base = Path(sys.argv[1])
name = 'parent' if os.fork() else 'worker'
while True:
    with (base/name).open('ab') as f: f.write(b'.')
    time.sleep(.01)
"""
    run = subprocess.Popen([exe, "--max-thermal", "fair", "--interval", ".02",
                            "--", sys.executable, "-c", program, temp],
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

    # The child owns its files and receives the caller's environment unchanged.
    script = root / "child.py"
    script.write_text("""import os,sys
assert 'PROCESS_GUARD_CHECKPOINT_DIR' not in os.environ
print(os.environ['PROCESS_GUARD_TEST_VALUE'], flush=True)
if sys.argv[-1]=='crash': os.kill(os.getpid(), 9)
sys.exit(17)
""")
    child_env = dict(os.environ, PROCESS_GUARD_TEST_VALUE="inherited")
    child_env.pop("PROCESS_GUARD_CHECKPOINT_DIR", None)
    child_root = root / "child-workdir"
    child_root.mkdir()
    command = [exe, "--max-thermal", "fair", "--", sys.executable, script]
    level(0)
    for extra, code in [(["crash"], 137), ([], 17)]:
        result = subprocess.run(command + extra, cwd=child_root, env=child_env,
                                capture_output=True, timeout=5)
        assert (result.stdout.strip(), result.returncode) == (b"inherited", code)
        assert b"\x1b[" not in result.stderr
        assert not list(child_root.iterdir())
    help_result = subprocess.run([binary, "--help"], capture_output=True, timeout=5)
    assert help_result.returncode == 0 and b"checkpoint" not in help_result.stdout
    bad = subprocess.run([binary, "--checkpoint-dir", "./old-checkpoints", "--", "/usr/bin/touch", "launched"],
                         cwd=child_root, capture_output=True, timeout=5)
    assert bad.returncode == 2 and b"ERROR" in bad.stderr
    assert not list(child_root.iterdir())
    for term, no_color, colored in [("xterm", "", True), ("xterm", "1", False), ("dumb", "", False)]:
        master, slave = pty.openpty()
        try:
            subprocess.run([binary, "--invalid"], stderr=slave,
                           env=dict(os.environ, TERM=term, NO_COLOR=no_color))
            assert (b"\x1b[" in os.read(master, 4096)) == colored
        finally:
            os.close(master)
            os.close(slave)

    level(0)
    for payload in [b"last", b"last\n", b"last\r", b"last\r\n", b"last\n\n",
                    b"last\n\n\n", b"\x1b[31mlast\x1b[0m\n",
                    b"last\n\n\x1b[0m", b"last\n\n\x1b]0;title\x07"]:
        program = f"os.write(1, b'first\\n'); os.write(2, {payload!r}); sys.exit(17)"
        with TerminalRun(exe, root, program) as run:
            run.release()
            data = run.finish()
            assert run.process.returncode == 17
            assert_block(data, b"first\n" + payload)

    # ANSI sequences and UTF-8 characters can cross read boundaries.
    with TerminalRun(exe, root, r"""
os.write(2, b'\x1b[')
wait('rest')
os.write(2, b'32mgreen\x1b[0m\n\n\xc3')
wait('utf8')
os.write(2, b'\xa8\n')
""") as run:
        run.release()
        run.wait_for(lambda: b"\x1b[" in run.data)
        (run.base / "rest").touch()
        run.wait_for(lambda: run.data.endswith(b"\xc3"))
        (run.base / "utf8").touch()
        assert_block(run.finish(), b"\x1b[32mgreen\x1b[0m\n\n\xc3\xa8\n")

    # Both guard transitions and adjacent guard events, using the fake sensor.
    with TerminalRun(exe, root, "os.write(1, b'before'); wait('after'); os.write(2, b'after\\n')") as run:
        run.release()
        run.wait_for(lambda: run.data.endswith(b"before"))
        level(2)
        run.wait_for(lambda: b" PAUSED " in run.data)
        level(0)
        run.wait_for(lambda: b" RESUMED " in run.data)
        (run.base / "after").touch()
        data = run.finish()
        assert re.search(rb"before\n\n[^\n]+ THERMAL [^\n]+\n[^\n]+ PAUSED ", data), data
        assert re.search(rb" RESUMED [^\n]+\n\nafter\n\n[^\n]+ EXIT ", data), data

    # Formatting is independent of color; the outer terminal translates LF once.
    for term, no_color, colored in [("xterm", "", True), ("xterm", "1", False), ("dumb", "", False)]:
        with TerminalRun(exe, root, "os.write(2, b'child\\n')",
                         env={"TERM": term, "NO_COLOR": no_color}, processed=True) as run:
            run.release()
            data = run.finish()
            assert (b"\x1b[" in data) == colored
            assert b"\r\r\n" not in data
            assert_block(ansi.sub(b"", data).replace(b"\r\n", b"\n"), b"child\n")

    raw = bytes(range(256))
    with TerminalRun(exe, root, f"""
assert not os.isatty(1) and os.isatty(2)
os.write(1, {raw!r})
os.write(2, b'error')
""", stdout=subprocess.PIPE) as run:
        run.release()
        assert_block(run.finish(), b"error")
        assert run.process.stdout.read() == raw

    # A different stdout terminal is not merged into the guard's stderr terminal.
    other_master, other_slave = open_terminal()
    try:
        with TerminalRun(exe, root, r"""
assert os.isatty(1) and os.isatty(2)
assert os.ttyname(1) != os.ttyname(2)
os.write(1, b'separate stdout\n')
os.write(2, b'error\n')
""", stdout=other_slave) as run:
            run.release()
            assert_block(run.finish(), b"error\n")
            assert select.select([other_master], [], [], 5)[0]
            assert os.read(other_master, 4096) == b"separate stdout\n"
    finally:
        os.close(other_master)
        os.close(other_slave)

    # No PTY or separators when guard stderr is redirected, even with stdout on a TTY.
    master, slave = open_terminal()
    try:
        result = subprocess.run([exe, "--", sys.executable, "-c", r"""
import os
assert os.isatty(1) and not os.isatty(2)
os.write(1, b'stdout\rprogress\n')
os.write(2, b'stderr\n')
"""], stdout=slave, stderr=subprocess.PIPE, timeout=5)
        assert result.returncode == 0
        assert b"stderr\n" in result.stderr and b"\n\n" not in result.stderr
        assert select.select([master], [], [], 5)[0]
        assert os.read(master, 4096) == b"stdout\rprogress\n"
    finally:
        os.close(master)
        os.close(slave)
    result = subprocess.run([exe, "--", sys.executable, "-c",
                             f"import os; os.write(1, {raw!r}); os.write(2, b'child\\n')"],
                            capture_output=True, timeout=5)
    assert result.returncode == 0 and result.stdout == raw
    assert b"child\n" in result.stderr and b"\n\n" not in result.stderr

    with TerminalRun(exe, root, """
assert os.isatty(1) and os.isatty(2)
def size(tag):
    width, height = os.get_terminal_size(2)
    os.write(2, f'{tag}={width}x{height}\\n'.encode())
signal.signal(signal.SIGWINCH, lambda *_: size('WINCH'))
size('SIZE')
wait('done')
""") as run:
        run.release()
        run.wait_for(lambda: b"SIZE=80x24\n" in run.data)
        fcntl.ioctl(run.master, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        run.process.send_signal(signal.SIGWINCH)
        run.wait_for(lambda: b"WINCH=100x30\n" in run.data)
        (run.base / "done").touch()
        assert_block(run.finish(), b"SIZE=80x24\nWINCH=100x30\n")

    # Exercise sustained output and drain the final partial line before EXIT.
    bulk = b'x' * (2 * 1024 * 1024) + b'final'
    with TerminalRun(exe, root, "sys.stdout.buffer.write(b'x' * (2 * 1024 * 1024)); sys.stdout.flush(); os.write(2, b'final')") as run:
        run.release()
        assert_block(run.finish(), bulk)

    # A descendant holding the PTY open must not delay the main child's EXIT.
    with TerminalRun(exe, root, """
import subprocess
worker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
(base/'worker').write_text(str(worker.pid))
os.write(2, b'last')
""") as run:
        try:
            run.release()
            assert_block(run.finish(), b"last")
            assert run.process.returncode == 0
        finally:
            worker_file = run.base / "worker"
            if worker_file.exists():
                try:
                    os.kill(int(worker_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
print("Integration checks passed.")
