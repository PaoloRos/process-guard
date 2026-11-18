import Darwin
import Foundation

let usage = """
Usage: process-guard [--max-thermal nominal|fair|serious] [--interval seconds]
                     -- command [arguments...]
Defaults: nominal, 5 seconds
"""
let levels = ["nominal", "fair", "serious", "critical"]
func thermal() -> Int { ProcessInfo.processInfo.thermalState.rawValue }
func label(_ state: Int) -> String { levels.indices.contains(state) ? levels[state] : "unknown" }
let environment = ProcessInfo.processInfo.environment
// All output below runs on the main queue, including reads from the child's PTY.
final class TerminalOutput {
    enum Origin { case child, guardMessage }
    enum Escape { case text, introducer, csi, osc, oscEscape }
    let enabled = isatty(STDERR_FILENO) == 1
    private var origin: Origin?
    private var newlines = 0
    private var escape = Escape.text
    private var master: Int32 = -1
    private var slave: Int32 = -1
    private var reader: DispatchSourceRead?

    private func check(_ code: Int32) throws {
        if code != 0 { throw POSIXError(POSIXErrorCode(rawValue: code) ?? .EIO) }
    }

    func prepare(_ actions: inout posix_spawn_file_actions_t?) throws {
        guard enabled else { return }
        var mode = termios()
        var size = winsize()
        if tcgetattr(STDERR_FILENO, &mode) != 0 { try check(errno) }
        if ioctl(STDERR_FILENO, TIOCGWINSZ, &size) != 0 { try check(errno) }
        // The real terminal applies output processing once, after forwarding.
        mode.c_oflag &= ~tcflag_t(OPOST)
        if openpty(&master, &slave, nil, &mode, &size) != 0 { try check(errno) }
        for descriptor in [master, slave] {
            if fcntl(descriptor, F_SETFD, FD_CLOEXEC) == -1 { try check(errno) }
        }
        let flags = fcntl(master, F_GETFL)
        if flags == -1 || fcntl(master, F_SETFL, flags | O_NONBLOCK) == -1 { try check(errno) }
        try check(posix_spawn_file_actions_adddup2(&actions, slave, STDERR_FILENO))
        var stdoutInfo = stat()
        var stderrInfo = stat()
        if isatty(STDOUT_FILENO) == 1,
           fstat(STDOUT_FILENO, &stdoutInfo) == 0, fstat(STDERR_FILENO, &stderrInfo) == 0,
           stdoutInfo.st_rdev == stderrInfo.st_rdev {
            try check(posix_spawn_file_actions_adddup2(&actions, slave, STDOUT_FILENO))
        }
        try check(posix_spawn_file_actions_addclose(&actions, master))
        try check(posix_spawn_file_actions_addclose(&actions, slave))
    }

    func startReading() {
        guard master >= 0 else { return }
        close(slave)
        slave = -1
        let descriptor = master
        let source = DispatchSource.makeReadSource(fileDescriptor: descriptor, queue: .main)
        source.setEventHandler { self.drain() }
        source.setCancelHandler { close(descriptor) }
        reader = source
        source.resume()
    }

    func resize() {
        guard master >= 0 else { return }
        var size = winsize()
        if ioctl(STDERR_FILENO, TIOCGWINSZ, &size) == 0 {
            _ = ioctl(master, TIOCSWINSZ, &size)
        }
    }

    func drain() {
        guard master >= 0 else { return }
        var buffer = [UInt8](repeating: 0, count: 16384)
        // Bound each turn so a continuously writing descendant cannot starve
        // thermal sampling, signals, or completion of the main child.
        var remaining = 256 * 1024
        while remaining > 0 {
            let count = read(master, &buffer, min(buffer.count, remaining))
            if count > 0 {
                write(Data(buffer.prefix(count)), from: .child)
                remaining -= count
            } else if count < 0 && errno == EINTR {
                continue
            } else {
                if count == 0 || (count < 0 && errno != EAGAIN && errno != EWOULDBLOCK) { stop() }
                return
            }
        }
    }

    func stop() {
        if slave >= 0 { close(slave); slave = -1 }
        if let source = reader {
            source.cancel()
            reader = nil
        } else if master >= 0 {
            close(master)
        }
        master = -1
    }

    func write(_ data: Data, from next: Origin) {
        guard !data.isEmpty else { return }
        if enabled, let previous = origin, previous != next, newlines < 2 {
            FileHandle.standardError.write(Data(repeating: 10, count: 2 - newlines))
            newlines = 2
        }
        FileHandle.standardError.write(data)
        origin = next
        // ANSI styling does not occupy a line, even across read boundaries.
        for byte in data {
            switch escape {
            case .text:
                if byte == 27 { escape = .introducer }
                else if byte == 10 { newlines = min(2, newlines + 1) }
                else if byte != 13 { newlines = 0 }
            case .introducer:
                if byte == 91 { escape = .csi }
                else if byte == 93 { escape = .osc }
                else { escape = .text }
            case .csi:
                if byte >= 0x40 && byte <= 0x7e { escape = .text }
            case .osc:
                if byte == 7 { escape = .text }
                else if byte == 27 { escape = .oscEscape }
            case .oscEscape:
                escape = byte == 92 ? .text : .osc
            }
        }
    }
}
let output = TerminalOutput()
let color = output.enabled && environment["TERM"] != "dumb"
    && (environment["NO_COLOR"] ?? "").isEmpty
let clock = ISO8601DateFormatter()
func log(_ event: String, _ message: String, _ shade: Int = 36) {
    output.drain()
    let tag = color ? "\u{1b}[\(shade)m\(event)\u{1b}[0m" : event
    let line = "\(clock.string(from: Date())) \(tag) thermal=\(label(current)) \(message)\n"
    output.write(Data(line.utf8), from: .guardMessage)
}
func fail(_ message: String, _ code: Int32 = 1) -> Never {
    log("ERROR", message, 31)
    exit(code)
}
var current = thermal()
var args = CommandLine.arguments.dropFirst()
var maximum = 0
var interval = 5.0
while let option = args.first, option != "--" {
    args.removeFirst()
    if option == "--help" { print(usage); exit(0) }
    guard let value = args.popFirst() else { fail(usage, 2) }
    switch option {
    case "--max-thermal":
        guard let rank = levels.dropLast().firstIndex(of: value) else { fail(usage, 2) }
        maximum = rank
    case "--interval":
        guard let seconds = Double(value), seconds.isFinite, seconds >= 0.000001,
              seconds <= Double(Int32.max) else { fail("Interval must be 0.000001...2147483647 seconds", 2) }
        interval = seconds
    default: fail(usage, 2)
    }
}
guard args.popFirst() == "--", let command = args.first, !command.isEmpty else { fail(usage, 2) }

var pid: pid_t = 0
var paused = false
var shuttingDown = false
let threshold = "max=\(levels[maximum])"
func launch() {
    var attributes: posix_spawnattr_t?
    var result = posix_spawnattr_init(&attributes)
    guard result == 0 else { fail(String(cString: strerror(result))) }
    defer { posix_spawnattr_destroy(&attributes) }
    var defaults = sigset_t()
    sigemptyset(&defaults)
    sigaddset(&defaults, SIGINT)
    sigaddset(&defaults, SIGTERM)
    sigaddset(&defaults, SIGWINCH)
    var mask = sigset_t()
    sigemptyset(&mask)
    for setting in [posix_spawnattr_setsigdefault(&attributes, &defaults),
                    posix_spawnattr_setsigmask(&attributes, &mask),
                    posix_spawnattr_setpgroup(&attributes, 0),
                    posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK))] {
        if setting != 0 { fail(String(cString: strerror(setting))) }
    }
    let argv = args.map { strdup($0) } + [nil]
    let envp = environment.map { strdup("\($0.key)=\($0.value)") } + [nil]
    defer { (argv + envp).forEach { free($0) } }
    var actions: posix_spawn_file_actions_t?
    result = posix_spawn_file_actions_init(&actions)
    guard result == 0 else { fail(String(cString: strerror(result))) }
    defer { posix_spawn_file_actions_destroy(&actions) }
    do { try output.prepare(&actions) }
    catch {
        output.stop()
        fail("Cannot prepare terminal output: \(error)")
    }
    result = posix_spawnp(&pid, command, &actions, &attributes, argv, envp)
    guard result == 0 else {
        output.stop()
        fail("\(command): \(String(cString: strerror(result)))", result == ENOENT ? 127 : 126)
    }
    output.startReading()
    log("START", "pid=\(pid) \(threshold)", 32)
}
func send(_ number: Int32) -> Bool {
    if kill(-pid, number) == 0 { return true }
    if errno != ESRCH { fail(String(cString: strerror(errno))) }
    return false
}
func reap() {
    guard pid > 0 else { return }
    var status: Int32 = 0
    let result = waitpid(pid, &status, WNOHANG)
    if result < 0 && errno != EINTR { fail(String(cString: strerror(errno))) }
    guard result == pid else { return }
    let signal = status & 0x7f
    let code = signal == 0 ? (status >> 8) & 255 : 128 + signal
    log("EXIT", "status=\(code)")
    output.stop()
    exit(code)
}
var sources: [DispatchSourceSignal] = []
for number in [SIGINT, SIGTERM, SIGCHLD] + (output.enabled ? [SIGWINCH] : []) {
    if number != SIGCHLD { signal(number, SIG_IGN) }
    else { signal(number, SIG_DFL) }
    let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
    source.setEventHandler {
        if number == SIGWINCH {
            output.resize()
            if pid > 0 { _ = send(SIGWINCH) }
            return
        }
        if number == SIGCHLD { reap(); return }
        guard pid > 0 else { log("EXIT", "signal=\(number)"); exit(128 + number) }
        shuttingDown = true
        _ = send(number)
        if paused { _ = send(SIGCONT); paused = false }
        reap()
    }
    source.resume()
    sources.append(source)
}
current = thermal()
if current > maximum { log("WAITING", threshold, 33) }
else { launch() }
let timer = DispatchSource.makeTimerSource(queue: .main)
timer.schedule(deadline: .now() + interval, repeating: interval)
timer.setEventHandler {
    reap()
    let next = thermal()
    if next != current { current = next; log("THERMAL", threshold) }
    guard !shuttingDown else { return }
    if pid == 0 {
        if current == 0 { launch() }
    } else if !paused && current > maximum && send(SIGSTOP) {
        paused = true
        log("PAUSED", threshold, 33)
    } else if paused && current == 0 && send(SIGCONT) {
        paused = false
        log("RESUMED", threshold, 32)
    }
}
timer.resume()
dispatchMain()
