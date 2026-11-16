import Darwin
import Foundation

let usage = """
Usage: process-guard [--max-thermal nominal|fair|serious] [--interval seconds]
                     [--checkpoint-dir path] -- command [arguments...]
Defaults: nominal, 5 seconds, ./.checkpoints
"""
let levels = ["nominal", "fair", "serious", "critical"]
func thermal() -> Int { ProcessInfo.processInfo.thermalState.rawValue }
func label(_ state: Int) -> String { levels.indices.contains(state) ? levels[state] : "unknown" }
let environment = ProcessInfo.processInfo.environment
let color = isatty(STDERR_FILENO) == 1 && environment["TERM"] != "dumb"
    && (environment["NO_COLOR"] ?? "").isEmpty
let clock = ISO8601DateFormatter()
func log(_ event: String, _ message: String, _ shade: Int = 36) {
    let tag = color ? "\u{1b}[\(shade)m\(event)\u{1b}[0m" : event
    let line = "\(clock.string(from: Date())) \(tag) thermal=\(label(current)) \(message)\n"
    FileHandle.standardError.write(Data(line.utf8))
}
func fail(_ message: String, _ code: Int32 = 1) -> Never {
    log("ERROR", message, 31)
    exit(code)
}
var current = thermal()
var args = CommandLine.arguments.dropFirst()
var maximum = 0
var interval = 5.0
var directory = "./.checkpoints"
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
    case "--checkpoint-dir":
        guard !value.isEmpty else { fail(usage, 2) }
        directory = value
    default: fail(usage, 2)
    }
}
guard args.popFirst() == "--", let command = args.first, !command.isEmpty else { fail(usage, 2) }
let path = URL(fileURLWithPath: directory).standardizedFileURL
do {
    try FileManager.default.createDirectory(at: path, withIntermediateDirectories: true)
    let probe = path.appendingPathComponent(".write-test-" + UUID().uuidString)
    try Data().write(to: probe, options: .withoutOverwriting)
    try FileManager.default.removeItem(at: probe)
} catch { fail("Cannot prepare checkpoint directory at \(path.path) (error \((error as NSError).code))") }

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
    var mask = sigset_t()
    sigemptyset(&mask)
    for setting in [posix_spawnattr_setsigdefault(&attributes, &defaults),
                    posix_spawnattr_setsigmask(&attributes, &mask),
                    posix_spawnattr_setpgroup(&attributes, 0),
                    posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK))] {
        if setting != 0 { fail(String(cString: strerror(setting))) }
    }
    var env = environment
    env["PROCESS_GUARD_CHECKPOINT_DIR"] = path.path
    let argv = args.map { strdup($0) } + [nil]
    let envp = env.map { strdup("\($0.key)=\($0.value)") } + [nil]
    defer { (argv + envp).forEach { free($0) } }
    result = posix_spawnp(&pid, command, nil, &attributes, argv, envp)
    guard result == 0 else { fail("\(command): \(String(cString: strerror(result)))", result == ENOENT ? 127 : 126) }
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
    exit(code)
}
var sources: [DispatchSourceSignal] = []
for number in [SIGINT, SIGTERM, SIGCHLD] {
    if number != SIGCHLD { signal(number, SIG_IGN) }
    else { signal(number, SIG_DFL) }
    let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
    source.setEventHandler {
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
