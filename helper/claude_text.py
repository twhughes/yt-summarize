"""Text in, text out: the one place yt spawns the claude CLI.

Every prompt yt sends carries a YouTube transcript, and a transcript is
untrusted text — a video can say "ignore your instructions and run ...". So
claude runs here with NO tools (`--tools ""`), no MCP servers
(`--strict-mcp-config`) and no permission bypass: the worst a hostile
transcript can do is produce a bad summary.

Provenance: the run() contract (prompt on stdin, on_error="raise" folds every
failure into one RuntimeError with a stderr tail) follows the llm.py seam this
helper used before. The spawn flags differ on purpose — do not sync them back.

Python 3 standard library only.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

try:
    import processes
except ImportError:
    from . import processes

STDERR_TAIL = 2000


def claude_bin():
    """The claude CLI: PATH first, then the usual per-user install. Resolved at
    call time so a PATH change (or a test's fake claude) is honored."""
    return shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")


def command(binary, model):
    cmd = [binary, "-p", "--tools", "", "--strict-mcp-config",
           "--setting-sources", "", "--settings", '{"disableAllHooks":true}',
           "--no-session-persistence"]
    if model:
        cmd += ["--model", model]
    return cmd


def run(prompt, *, model=None, timeout=900, on_error="return"):
    """Run one prompt to completion and return the model's text (stripped).

    on_error="return" -> "" on any failure; "raise" -> RuntimeError whose
    message carries the stderr tail."""
    def fail(message):
        if on_error == "raise":
            raise RuntimeError(message)
        return ""

    binary = claude_bin()
    if not os.path.isfile(binary):
        return fail("claude CLI not found (%s)" % binary)

    try:
        with tempfile.TemporaryDirectory(prefix="yt-claude-") as workdir:
            proc = processes.run(
                command(binary, model), cwd=workdir,
                input=prompt.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
    except subprocess.TimeoutExpired:
        return fail("claude did not finish within %ds" % timeout)
    except OSError as exc:
        return fail("could not run claude: %s" % exc)

    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        out = proc.stdout.decode("utf-8", "replace").strip()
        return fail("claude exited %d: %s" % (proc.returncode, (err or out)[-STDERR_TAIL:]))

    return proc.stdout.decode("utf-8", "replace").strip()
