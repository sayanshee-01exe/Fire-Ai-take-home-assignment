"""Runs validated compiled code in a separate, restricted Python process.

Layers (each independent of the allow-list in safety.py):
- a new interpreter started with `python -I` (isolated: no user site, no PYTHON* variables, no cwd
  on sys.path), an EMPTY environment, and a fresh temporary working directory;
- the code is exec'd with a builtins table holding only the allowed functions;
- RLIMIT_CPU caps CPU seconds; memory is capped with RLIMIT_AS where the OS allows it (Linux).
  macOS refuses memory rlimits, so there a parent-side watchdog kills the worker when its virtual size
  grows more than the limit above its size at start (measured with /bin/ps);
- a wall-clock timeout per call; a timed-out or dead worker is killed and restarted on next use;
- input arrives as JSON on stdin and output leaves as JSON on stdout.
Any violation, exception, timeout or kill comes back as {"ok": False, "error": ...}; nothing raises.

The worker is persistent (one process per engine), so a query costs one pipe round trip, not a
process start: per-query latency is in microseconds plus the round trip.
"""
import json
import os
import selectors
import subprocess
import sys
import tempfile
import time

from project_a.compiler.safety import ALLOWED_CALLS, validate

RUNNER = r'''
import json, resource, sys, time, builtins
cfg = json.loads(sys.stdin.readline())
try:
    resource.setrlimit(resource.RLIMIT_CPU, (cfg["cpu_s"], cfg["cpu_s"] + 1))
except Exception:
    pass
mem = cfg["mem_bytes"]
mem_rlimit = False
for name in ("RLIMIT_AS", "RLIMIT_DATA"):
    try:
        resource.setrlimit(getattr(resource, name), (mem, mem))
        mem_rlimit = True
        break
    except Exception:
        pass
safe = {n: getattr(builtins, n) for n in cfg["allowed"]}
ns = {"__builtins__": safe}
try:
    exec(compile(cfg["source"], "<compiled>", "exec"), ns)
    fn = ns["compute"]
    ready = {"ready": True, "mem_rlimit": mem_rlimit}
except BaseException as e:
    fn, ready = None, {"ready": False, "error": "load failed: %s: %s" % (type(e).__name__, e)}
sys.stdout.write(json.dumps(ready) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    try:
        inp = json.loads(line)
        t0 = time.perf_counter()
        out = fn(inp)
        dt = time.perf_counter() - t0
        msg = json.dumps({"ok": True, "output": out, "compute_s": dt})
    except MemoryError:
        msg = json.dumps({"ok": False, "error": "MemoryError: memory limit exceeded"})
    except BaseException as e:
        msg = json.dumps({"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])})
    sys.stdout.write(msg + "\n"); sys.stdout.flush()
'''


def _vsz_kb(pid):
    try:
        out = subprocess.run(["/bin/ps", "-o", "vsz=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=2, env={})
        return int(out.stdout.strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


class Sandbox:
    def __init__(self, source, timeout_s=2.0, cpu_s=20, mem_mb=512, allow_helpers=False):
        self.source, self.timeout_s, self.cpu_s, self.mem_mb = source, timeout_s, cpu_s, mem_mb
        self.violations = validate(source, allow_helpers)               # defence in depth: never run unvalidated code
        self.proc = None
        self._tmp = None
        self._buf = b""

    # ---- process management --------------------------------------------------------------------
    def _start(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sandbox_")
        self.proc = subprocess.Popen([sys.executable, "-I", "-c", RUNNER], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env={},
                                     cwd=self._tmp.name, close_fds=True)
        self._buf = b""
        cfg = {"source": self.source, "allowed": list(ALLOWED_CALLS), "cpu_s": self.cpu_s,
               "mem_bytes": self.mem_mb * 1024 * 1024}
        ready = self._exchange(json.dumps(cfg), timeout_s=10.0)
        if not ready.get("ready"):
            err = ready.get("error", "sandbox did not start")
            self.close()
            return err
        self.mem_rlimit = ready.get("mem_rlimit", False)
        self._base_vsz = _vsz_kb(self.proc.pid)
        return None

    def close(self):
        if self.proc is not None:
            try:
                self.proc.kill()
            except OSError:
                pass
            self.proc.wait()
            for s in (self.proc.stdin, self.proc.stdout):
                try:
                    s.close()
                except OSError:
                    pass
        self.proc = None
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _exchange(self, line, timeout_s):
        """Sends one JSON line, returns the parsed reply or {"ok": False, "error": ...}."""
        try:
            self.proc.stdin.write((line + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            return self._dead("sandbox process is not accepting input")
        deadline = time.monotonic() + timeout_s
        sel = selectors.DefaultSelector()
        sel.register(self.proc.stdout, selectors.EVENT_READ)
        try:
            while b"\n" not in self._buf:
                left = deadline - time.monotonic()
                if left <= 0:
                    self.close()
                    return {"ok": False, "error": "timeout: no result within %.1f s (process killed)" % timeout_s}
                if sel.select(timeout=min(left, 0.05)):
                    chunk = os.read(self.proc.stdout.fileno(), 65536)
                    if not chunk:
                        return self._dead("sandbox process died")
                    self._buf += chunk
                elif not getattr(self, "mem_rlimit", True) and getattr(self, "_base_vsz", 0):
                    if _vsz_kb(self.proc.pid) - self._base_vsz > self.mem_mb * 1024:
                        self.close()
                        return {"ok": False, "error": "memory limit exceeded: over %d MB (process killed)" % self.mem_mb}
        finally:
            sel.close()
        raw, _, self._buf = self._buf.partition(b"\n")
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            self.close()
            return {"ok": False, "error": "sandbox wrote non-JSON output"}

    def _dead(self, what):
        code = self.proc.poll() if self.proc else None
        self.close()
        return {"ok": False, "error": "%s (exit status %s)" % (what, code)}

    # ---- API -------------------------------------------------------------------------------------
    def run(self, inp):
        """Returns {"ok": True, "output": ..., "compute_s": ..., "roundtrip_s": ...} or {"ok": False, "error": ...}."""
        if self.violations:
            return {"ok": False, "error": "rejected by the allow-list: " + "; ".join(self.violations)}
        if self.proc is None or self.proc.poll() is not None:
            err = self._start()
            if err:
                return {"ok": False, "error": err}
        t0 = time.perf_counter()
        try:
            line = json.dumps(inp)
        except (TypeError, ValueError) as e:
            return {"ok": False, "error": "input is not JSON-serialisable: %s" % e}
        reply = self._exchange(line, self.timeout_s)
        reply["roundtrip_s"] = time.perf_counter() - t0
        return reply


def run_once(source, inp, **limits):
    box = Sandbox(source, **limits)
    try:
        return box.run(inp)
    finally:
        box.close()
