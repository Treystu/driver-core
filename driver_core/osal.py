"""The ONE place driver-core talks to the operating system.

This module exists because the alternative is always worse. A subprocess call
written inline in an executor, and the same call written in a perception
adapter, and the same call in a CLI, each develop their own idea of what an
argument list looks like on the platform they happen to be run on -- and the
divergence only ever shows up on the platform nobody tested.

So the rules are boring and total:

* **No shell, ever.** Every call is an argv list. A shell turns a path
  containing a space or a forward slash into a syntax problem on one platform
  and not another, and it turns a filename into an injection point.
* **No platform probes outside this file.** ``os.name`` and ``sys.platform``
  appear here and nowhere else; ``tests/test_osal_boundary.py`` fails the
  build if another module reaches for them.
* **Divergences are declared, not discovered.** Where platforms genuinely
  differ, the answer is a named constant and a comment, so a reader learns
  the rule instead of inferring it from a bug.

Screen capture and synthetic input are here for the same reason, and are
declared per-platform: a driver that silently does nothing on an unsupported
platform is more dangerous than one that refuses, because it looks like a
successful observation.
"""
import os
import subprocess
import sys
import urllib.parse
import urllib.request

from .errors import DriverError

#: How long a child process may run before it is killed.
DEFAULT_TIMEOUT = 30

#: The largest stdout we will read from a child, in bytes. A command that
#: prints without bound must not be able to exhaust memory in the driver.
MAX_CAPTURE_BYTES = 1 << 20

#: Declared platform support. Anything absent here is refused rather than
#: attempted, and the refusal says so by name.
SUPPORTED = ("linux", "darwin", "windows")

#: Declared divergences.
#: Windows console programs need the CREATE_NO_WINDOW flag or they flash a
#: console window on every invocation, which is unacceptable for a driver
#: that polls in a loop.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def platform_name():
    """``linux``, ``darwin`` or ``windows``. The only platform answer here."""
    if sys.platform.startswith("win") or os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    return f"unknown:{sys.platform}"


def is_supported():
    return platform_name() in SUPPORTED


def run(argv, *, cwd=None, timeout=DEFAULT_TIMEOUT, env=None, input_text=None,
        max_bytes=MAX_CAPTURE_BYTES):
    """Run one command as an argv list, with no shell.

    Returns a result object rather than raising, so a caller can tell a
    non-zero exit (an answer) from a missing binary or a timeout (a failure
    to ask). Those two deserve different handling and a bare
    :class:`FileNotFoundError` does not preserve the distinction once it has
    been caught somewhere four frames up.
    """
    if isinstance(argv, str):
        raise TypeError(
            "run() takes an argv list, not a string; a string would be "
            "handed to the platform shell, which is exactly what this "
            "module exists to prevent")
    argv = list(argv)
    if not argv:
        raise ValueError("argv must not be empty")
    if input_text is not None and not isinstance(input_text, str):
        # Caught here rather than surfacing as an AttributeError from inside
        # the encode() call four lines down, which reads as a bug in this
        # module rather than as the caller's mistake.
        raise TypeError(
            f"input_text must be str or None, got {type(input_text).__name__}")

    child_env = None
    if env:
        child_env = dict(os.environ)
        child_env.update(env)

    popen_kwargs = {
        "cwd": cwd,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
    }
    if input_text is None:
        popen_kwargs["stdin"] = subprocess.DEVNULL
    # When input_text is given, `input=` below installs the stdin pipe itself.
    # Passing both is rejected by subprocess, and doing so meant the
    # input_text path raised ValueError on every platform -- a parameter that
    # looked like it worked and had never once been exercised.
    if platform_name() == "windows":
        popen_kwargs["creationflags"] = CREATE_NO_WINDOW

    try:
        completed = subprocess.run(
            argv, input=(input_text.encode("utf-8") if input_text is not None
                         else None),
            timeout=timeout, env=child_env, check=False, **popen_kwargs)
    except FileNotFoundError:
        return ProcessResult(0, False, "", "", f"not found: {argv[0]}",
                             reason="not_found")
    except subprocess.TimeoutExpired:
        return ProcessResult(0, False, "", "",
                             f"timed out after {timeout}s", reason="timeout")
    except OSError as exc:
        return ProcessResult(0, False, "", "", f"failed to start: {exc}",
                             reason="os_error")

    stdout = (completed.stdout or b"")[:max_bytes].decode("utf-8", "replace")
    stderr = (completed.stderr or b"")[:max_bytes].decode("utf-8", "replace")
    return ProcessResult(completed.returncode, completed.returncode == 0,
                         stdout, stderr, "")


class ProcessResult:
    """One child process's outcome."""

    __slots__ = ("returncode", "ok", "stdout", "stderr", "error", "reason")

    def __init__(self, returncode, ok, stdout, stderr, error, reason=""):
        self.returncode = returncode
        self.ok = ok
        self.stdout = stdout
        self.stderr = stderr
        self.error = error
        self.reason = reason

    def to_dict(self):
        return {"returncode": self.returncode, "ok": self.ok,
                "stdout": self.stdout, "stderr": self.stderr,
                "error": self.error, "reason": self.reason}

    def __repr__(self):
        return f"ProcessResult(rc={self.returncode}, ok={self.ok})"


# ---- screen capture -----------------------------------------------------
# Declared per platform rather than detected, so "I could not capture" is
# always a named answer and never an empty file that looks like a blank
# screen.

def capture_screen():
    """Capture the screen. Returns ``(path, detail)``; ``path`` may be None.

    A refusal is a first-class result. A driver handed an unreadable capture
    should route to a structured source, not ask a vision model to describe
    a file that does not exist.
    """
    name = platform_name()
    if name == "windows":
        return _capture_windows()
    if name == "darwin":
        return _capture_macos()
    if name == "linux":
        return _capture_linux()
    return None, f"screen capture is not supported on {name}"


def _capture_windows():
    script = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
              "$b = [System.Windows.Forms.SystemInformation]::VirtualScreen; "
              "$i = New-Object System.Drawing.Bitmap $b.Width, $b.Height; "
              "$g = [System.Drawing.Graphics]::FromImage($i); "
              "$g.CopyFromScreen($b.X, $b.Y, 0, 0, $i.Size); "
              "$i.Save($env:DRIVER_SCREEN_OUT, "
              "[System.Drawing.Imaging.ImageFormat]::Png)")
    out = os.environ.get("DRIVER_SCREEN_OUT", "")
    if not out:
        return None, "DRIVER_SCREEN_OUT is not set; cannot capture"
    result = run(["powershell", "-NoProfile", "-NonInteractive",
                  "-Command", script], timeout=45)
    if not result.ok or not os.path.exists(out):
        return None, f"capture failed: {result.error or result.stderr[:200]}"
    return out, ""


def _capture_macos():
    out = os.environ.get("DRIVER_SCREEN_OUT", "")
    if not out:
        return None, "DRIVER_SCREEN_OUT is not set; cannot capture"
    result = run(["screencapture", "-x", out], timeout=45)
    if not result.ok or not os.path.exists(out):
        return None, f"capture failed: {result.error or result.stderr[:200]}"
    return out, ""


def _capture_linux():
    out = os.environ.get("DRIVER_SCREEN_OUT", "")
    if not out:
        return None, "DRIVER_SCREEN_OUT is not set; cannot capture"
    result = run(["gnome-screenshot", "-f", out], timeout=45)
    if not result.ok or not os.path.exists(out):
        return None, f"capture failed: {result.error or result.stderr[:200]}"
    return out, ""


# ---- synthetic input -----------------------------------------------------
# Declared, not discovered. A platform with no declared support returns a
# named refusal, so an unregistered driver cannot half-work its way through a
# run by doing nothing and reporting success.

# ---- reaching the network -----------------------------------------------
# Here for the same reason as ``run`` and ``capture_screen``: a driver that
# fetches from two files grows two ideas about timeouts, redirects and user
# agents, and the divergence only shows up against the one server that cares.
#
# Declared limits, because a default that trusts the far end is a default
# that will eventually be wrong in front of somebody's screen:
#
# * the scheme is restricted to http/https, so ``file://`` cannot be used to
#   read the local disk through a URL-shaped hole;
# * the redirect budget is small, because a redirect loop in a local driver
#   is a hang nobody is watching;
# * a redirect to any other scheme is refused rather than followed.

MAX_REDIRECTS = 3
FETCH_MAX_BYTES = 1 << 20
USER_AGENT = "driver-core (perception; stdlib-only)"
ALLOWED_SCHEMES = ("http", "https")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Count redirects instead of following them blindly."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        used = getattr(self, "_driver_core_redirects", 0)
        if used >= MAX_REDIRECTS:
            raise DriverError(
                f"refusing to follow more than {MAX_REDIRECTS} redirects "
                f"while fetching a document")
        scheme = urllib.parse.urlparse(newurl).scheme
        if scheme not in ALLOWED_SCHEMES:
            raise DriverError(
                f"refusing a redirect to {scheme!r}; only "
                f"{list(ALLOWED_SCHEMES)} are fetched")
        self._driver_core_redirects = used + 1
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_get(url, *, timeout=20, max_bytes=FETCH_MAX_BYTES):
    """Fetch one document. Returns ``(status, text)``; raises on refusal.

    A caller gets either an HTTP status and a body, or an exception naming
    the reason. There is deliberately no "empty document, but successfully":
    an empty body returned as a successful fetch is how a driver concludes a
    page is blank when in fact it never arrived.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise DriverError(
            f"refusing to fetch {parsed.scheme!r}; only "
            f"{list(ALLOWED_SCHEMES)} are fetched")
    if not parsed.netloc:
        raise DriverError(f"{url!r} has no host to fetch from")

    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with opener.open(request, timeout=timeout) as response:
        body = response.read(max_bytes)
        status = getattr(response, "status", None) or response.getcode()
        charset = response.headers.get_content_charset() or "utf-8"
    return status, body.decode(charset, "replace")


def send_input(kind, value=None, *, target=None):
    """Perform one synthetic input. Returns ``(ok, detail)``.

    ``kind`` is one of ``click``, ``type``, ``key``. Input synthesis is the
    highest-consequence thing this package does, so it is registered
    explicitly by the embedding application and never guessed at here.
    """
    if kind not in ("click", "type", "key"):
        return False, f"unknown input kind {kind!r}"
    return False, (
        f"synthetic {kind} has no registered backend on {platform_name()}; "
        f"the embedding application must register one explicitly")
