"""Restricted runner for generated programs (codegen.py). Started by the parent as
`python -I -S codegen_runner.py PROGRAM.py` (isolated mode, no site-packages, minimal environment) and
talks JSON lines on stdin/stdout; the parent owns the SQLTool and answers every query request, so the
budget and the observation accounting live in the parent.

What the program gets: the module-level names `query` and `BudgetExhausted`, and a builtins table
without open / exec / eval / compile / input / breakpoint / exit / quit; `__import__` is restricted to
an allowlist of standard-library modules (no os, sys, socket, subprocess, pathlib, io, urllib, ...).
Process limits: RLIMIT_FSIZE 0 (no file can be written), RLIMIT_CPU, RLIMIT_AS; the parent enforces a
wall-clock timeout and kills the process. The program's stdout/stderr are captured, never the pipe.
This is a restricted runner for machine-written audit code, not a security boundary against hostile code.

Requests: {"op": "check"} (import the program, verify decide is defined) or
{"op": "run", "alert": {...}} (import, then call decide(alert, query)).
Replies: {"op": "query", ...} to the parent, final {"op": "result", "value": ...} or
{"op": "error", "phase": "import"|"decide", "type", "message", "traceback"}.
"""
import builtins
import io
import json
import resource
import sys
import traceback
import types

ALLOWED_MODULES = {"json", "datetime", "re", "math", "collections", "itertools", "functools", "operator",
                   "string", "typing", "dataclasses", "enum", "decimal", "bisect", "heapq", "copy", "time",
                   "calendar", "numbers", "abc", "fractions", "statistics", "textwrap", "zoneinfo", "_strptime", "__future__"}
REMOVED_BUILTINS = ("open", "exec", "eval", "compile", "input", "breakpoint", "exit", "quit", "help")

_real_import = builtins.__import__


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    top = name.split(".")[0]
    if level != 0 or top not in ALLOWED_MODULES:
        raise ImportError(f"import of {name!r} is not available in the restricted runner")
    return _real_import(name, globals, locals, fromlist, level)


def main():
    src = open(sys.argv[1], encoding="utf-8").read()
    out, inp = sys.stdout, sys.stdin
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
    resource.setrlimit(resource.RLIMIT_AS, (2 << 30, 2 << 30))
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()

    def send(obj):
        out.write(json.dumps(obj, default=str) + "\n")
        out.flush()

    def recv():
        line = inp.readline()
        if not line:
            raise SystemExit(0)
        return json.loads(line)

    class BudgetExhausted(RuntimeError):
        pass

    def query(sql, params=()):
        send({"op": "query", "sql": sql, "params": list(params) if isinstance(params, (list, tuple)) else params})
        reply = recv()
        if reply.get("budget_exhausted"):
            raise BudgetExhausted(reply.get("error", "query budget exhausted"))
        return reply

    safe = dict(vars(builtins))
    for k in REMOVED_BUILTINS:
        safe.pop(k, None)
    safe["__import__"] = _guarded_import
    mod = types.ModuleType("generated_program")
    mod.__dict__.update({"__builtins__": safe, "__name__": "generated_program", "query": query,
                         "BudgetExhausted": BudgetExhausted})
    sys.modules[mod.__name__] = mod  # as a real import would; dataclasses with postponed annotations need it
    req = recv()
    try:
        code = compile(src, "generated_program.py", "exec")
        exec(code, mod.__dict__)
        fn = mod.__dict__.get("decide")
        if not callable(fn):
            raise TypeError("decide(alert, query) is not defined")
    except BaseException as exc:  # noqa: BLE001 - everything is reported to the parent
        send({"op": "error", "phase": "import", "type": type(exc).__name__, "message": str(exc)[:500],
              "traceback": traceback.format_exc()[-2000:]})
        return
    if req.get("op") == "check":
        send({"op": "result", "value": {"ok": True}})
        return
    try:
        result = fn(req.get("alert"), query)
        payload = json.loads(json.dumps(result, default=str))  # must be JSON-serialisable
    except BaseException as exc:  # noqa: BLE001
        send({"op": "error", "phase": "decide", "type": type(exc).__name__, "message": str(exc)[:500],
              "traceback": traceback.format_exc()[-2000:], "stderr": sys.stderr.getvalue()[-500:]})
        return
    send({"op": "result", "value": payload})


if __name__ == "__main__":
    main()
