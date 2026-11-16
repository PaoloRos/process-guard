# Process Guard

Run a batch command on macOS and pause its process group when the Mac's thermal pressure exceeds your chosen level. The reading describes the whole Mac, including load from other applications.

## Build and run

Requires macOS 12 or later and a Swift 5.9 or later toolchain (Xcode or Command Line Tools). From the project directory:

```sh
swift build -c release
.build/release/process-guard --help
.build/release/process-guard --max-thermal fair --checkpoint-dir ./checkpoints/cnn -- python3 train.py
```

Replace `train.py` with your training script. Put guard options before `--` and the command and its arguments after it. Use a separate guard instance and checkpoint directory for each independent run.

| Option | Default | Meaning |
| --- | --- | --- |
| `--max-thermal` | `nominal` | Highest tolerated level: nominal, fair or serious. Critical always triggers a pause. |
| `--interval` | `5` | Seconds between samples (fractional values supported, from 0.000001 to 2147483647). |
| `--checkpoint-dir` | `./.checkpoints` | Writable directory for the training program's checkpoints. Relative paths resolve from the current working directory. |
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

Event labels are colored on a terminal. Set `NO_COLOR=1` or `TERM=dumb` for plain output. Redirect with `2> guard.log` to capture plain guard messages together with the child's own stderr. Child stdout and stderr pass through as produced.

## Recover a training run

A thermal pause preserves the running process in memory. After a crash, recovery starts from the most recent checkpoint your training script successfully wrote. Process Guard creates the directory and passes its absolute path to the child. For example, in Python:

```python
import os
from pathlib import Path

checkpoint_dir = Path(os.environ["PROCESS_GUARD_CHECKPOINT_DIR"])
```

Have the script load its latest completed checkpoint on startup and periodically save the model, optimizer and training progress there. Write each new checkpoint to a temporary file and replace the previous file after the write succeeds. To recover, rerun the same command above with the same checkpoint directory.
