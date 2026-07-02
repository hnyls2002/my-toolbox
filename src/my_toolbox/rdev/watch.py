"""Full-screen remote TUI (nvitop & co.) with auto-reconnect.

Flaky links to devboxes / remote servers drop long-lived interactive ssh
sessions, killing a monitoring TUI mid-watch. This wraps ``ssh -t <host>
<cmd>`` in a reconnect loop: a dropped connection re-launches the TUI by
itself; only a deliberate quit (``q`` in nvitop, Ctrl-C) ends the loop.

Exit-code classification (what the ssh child returned):
- 0            -- clean quit from the TUI (`q`) -> stop.
- 130          -- TUI killed by Ctrl-C on the remote -> user wants out, stop.
- 126 / 127    -- command not executable / not found -> stop with a hint;
                  retrying cannot fix a missing binary.
- anything else -- 255 (ssh client error), 129 (SIGHUP on drop), ... -> retry.

A rapid-failure guard stops the loop when several sessions in a row die
almost immediately: that is a persistent problem (auth, dead host, broken
command), not a link drop worth hammering.
"""

import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

from my_toolbox.ui import dim

# Detect a dead link fast: default ssh waits on TCP timeouts (minutes);
# ServerAlive* declares the connection dead after ~6s of silence.
SSH_WATCH_OPTS = [
    "-o",
    "ServerAliveInterval=3",
    "-o",
    "ServerAliveCountMax=2",
    "-o",
    "ConnectTimeout=10",
]

_CLEAN_RCS = frozenset({0, 130})
_NO_RETRY_HINTS = {
    126: "remote command is not executable",
    127: "remote command not found (pip install nvitop on the host/container?)",
}


@dataclass
class ReconnectPolicy:
    """Decides whether a failed session is worth retrying.

    Sessions shorter than ``min_session_secs`` count as rapid failures;
    ``max_rapid_failures`` of those in a row means the problem is persistent
    and the loop should stop. Any session that lived long enough resets the
    counter (it really was a connection drop).
    """

    delay_secs: float = 3.0
    min_session_secs: float = 10.0
    max_rapid_failures: int = 5
    _rapid: int = field(default=0, repr=False)

    def keep_retrying(self, session_secs: float) -> bool:
        if session_secs >= self.min_session_secs:
            self._rapid = 0
        else:
            self._rapid += 1
        return self._rapid < self.max_rapid_failures


def watch_remote(
    host: str,
    remote_cmd: str,
    *,
    label: str = "nvitop",
    policy: Optional[ReconnectPolicy] = None,
) -> int:
    """Run a full-screen TUI over ``ssh -t``, reconnecting on dropped links.

    ``remote_cmd`` is the full remote invocation (already shell-quoted by the
    caller, e.g. ``docker exec -it ctr bash -lc nvitop``). Returns the final
    session's exit code (0 for a clean quit).
    """
    policy = policy or ReconnectPolicy()
    argv = ["ssh", "-t", *SSH_WATCH_OPTS, host, remote_cmd]
    attempt = 0

    while True:
        attempt += 1
        if attempt > 1:
            print(dim(f"  [{host}] reconnecting {label} (attempt {attempt})..."))
        start = time.monotonic()
        try:
            rc = subprocess.run(argv).returncode
        except KeyboardInterrupt:
            # Ctrl-C landed locally (during connect, before the remote TTY
            # goes raw); once the TUI is up, Ctrl-C goes to the remote instead.
            return 130
        elapsed = time.monotonic() - start

        if rc in _CLEAN_RCS:
            return rc
        if rc in _NO_RETRY_HINTS:
            print(f"  [{host}] {label} exited (rc={rc}): {_NO_RETRY_HINTS[rc]}")
            return rc
        if not policy.keep_retrying(elapsed):
            print(
                f"  [{host}] {label} keeps dying within "
                f"{policy.min_session_secs:.0f}s (rc={rc}); giving up -- "
                f"this looks persistent, not a connection drop."
            )
            return rc

        print(
            dim(
                f"  [{host}] connection lost (rc={rc}); retrying in "
                f"{policy.delay_secs:.0f}s (Ctrl-C to quit)"
            )
        )
        try:
            time.sleep(policy.delay_secs)
        except KeyboardInterrupt:
            return 130
