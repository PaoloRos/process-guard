# Process Guard

Run a batch command on macOS and pause its process group when the Mac's thermal pressure exceeds your chosen level. The reading describes the whole Mac, including load from other applications.

## Build and run

Requires macOS 12 or later and a Swift 5.9 or later toolchain (Xcode or Command Line Tools). From the project directory:

```sh
swift build -c release
.build/release/process-guard --help
.build/release/process-guard --max-thermal fair -- python3 train.py
```

Replace `train.py` with your training script. Put guard options before `--` and the command and its arguments after it. Use a separate guard instance for each independent run.

| Option | Default | Meaning |
| --- | --- | --- |
| `--max-thermal` | `nominal` | Highest tolerated level: nominal, fair or serious. Critical always triggers a pause. |
| `--interval` | `5` | Seconds between samples (fractional values supported, from 0.000001 to 2147483647). |
| `--help` | | Print usage and exit. |

If the initial reading exceeds the limit, launch waits for nominal. Once running, equality with the limit is allowed. After a pause, the group stays suspended until a sample reads nominal. Workers that stay in the command's process group pause with it. Use this for unattended commands that do not read interactively from the terminal. Ctrl-C or SIGTERM forwards the signal to the group and wakes a suspended group so it can exit. The guard returns the main child's exit code, or 128 plus its terminating signal number.

## Install in ~/usr/local/bin

With Python 3 available and `~/usr/local/bin` already present, run the following in Terminal. The executable is installed only if the release build succeeds and the integration tests pass.

```sh
cd ~/Develop/frameworks/process-guard &&
  swift build -c release &&
  python3 Tests/integration.py &&
  install -m 755 .build/release/process-guard "$HOME/usr/local/bin/process-guard"
```

## Logs

Example guard messages on stderr:

```text
2026-11-16T10:00:00Z PAUSED thermal=serious max=fair
2026-11-16T10:01:20Z RESUMED thermal=nominal max=fair
```

On a terminal, a blank line separates each switch between child output and guard messages. Consecutive messages from the same source stay together; existing blank lines are preserved. Partial lines and progress updates using carriage returns are completed before a guard message.

The child keeps terminal behavior (including colors and progress bars) through a pseudo-terminal with the terminal's current dimensions. Its stderr is forwarded immediately; stdout joins the same output only when it shares the guard's terminal. Terminal resizes are forwarded to the child's process group. This is intended for noninteractive batch commands, as described above.

Event labels are colored on a terminal. Set `NO_COLOR=1` or `TERM=dumb` for plain output; blank-line separation still applies. Redirect with `2> guard.log` to capture plain guard messages together with the child's own stderr, without added separators. Redirected child stdout passes through unchanged. When guard stderr is redirected, both child streams retain their original destinations and terminal behavior.

## Thermal protection and saving progress

Process Guard only manages thermal pauses and resumes. A pause preserves the running process in memory; it does not save progress to disk. Your script manages any backups, checkpoints and recovery, including their paths. The guard inherits the current working directory and environment without adding checkpoint configuration.
