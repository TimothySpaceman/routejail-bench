import logging
import shlex
import subprocess

log = logging.getLogger("bench")


class CmdError(RuntimeError):
    pass


def run(argv, check=True, input=None, timeout=60):
    """Run a command, capture output, optionally raise on failure."""
    log.debug("$ %s", " ".join(shlex.quote(a) for a in argv))
    try:
        cp = subprocess.run(argv, input=input, text=True, capture_output=True, timeout=timeout)
    except (FileNotFoundError, PermissionError) as e:   # tool not installed (or its PATH entry unreadable): a failed command
        cp = subprocess.CompletedProcess(argv, 127, "", str(e))
    if check and cp.returncode != 0:
        raise CmdError(f"{' '.join(argv)} -> rc={cp.returncode}: {(cp.stderr or cp.stdout).strip()}")
    return cp
