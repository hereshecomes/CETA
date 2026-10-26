"""Generic authorization-audit evaluator of CETA.

Deterministic, three-valued (Kleene), event-time, coverage-qualified evaluation of a compiled
specification over the shared read-only SQL tool and budget. Contains no application names: every table,
column, action value and source name comes from the specification.

The specification language is documented in SPEC_LANGUAGE below; that text is also the only description
of the evaluator that the compiler (compiler.py) ever shows the LLM.
"""
import re
from datetime import datetime, timedelta, timezone

from sqltool import BudgetExhausted

EVIDENCE_CAP = 8
KINDS = ("change_log", "interval", "prior_event")
PRINCIPAL_KINDS = ("column", "binding_event", "binding_table")

SPEC_LANGUAGE = """## Specification language

A specification is one JSON object with the sections `operation`, `principal`, `scopes`, `paths`
and `coverage`. Table and column names must exist in the application schema.

Two kinds of field values must not be confused:
- Fields ending in `_column` take a column name. Fields ending in `_expr` take a column name or a
  JSON extraction from a column: `json_extract(<column>, '$.<key>')` (equivalently
  `<column>->>'key'`). Examples: `"holder_column": "actor_id"`, `"subject_expr":
  "json_extract(data, '$.user')"`. Never put a value reference into a `_column` or `_expr` field.
- Fields ending in `_ref` take a value reference, which names a value known at evaluation time:
  `operation.<column>` (a column of the operation row), `credential` (the operation's credential
  value), `principal` (the accountable principal), `binding.<column>` (a column of the binding
  record that established the principal) or `scope.<name>` (a named scope; always with the
  `scope.` prefix). Examples: `"scope_ref": "scope.team"`, `"subject_ref": "principal"`,
  `"object_ref": "operation.id"`. Never put a column name or an expression into a `_ref` field.

Every record source names its entry in the coverage table through `source` (defaults to the table
name). All fields listed for a section or kind are required unless marked optional.

### operation
`{"table", "id_column", "time_column"}`: the operation under audit is the row whose id_column
equals the alert's record id; t is its time_column (ISO-8601).

### principal (the accountable principal, resolved at most once)
- `{"kind": "column", "column"}`: a column of the operation row holds the principal.
- `{"kind": "binding_event", "credential_column", "table", "action_column", "action_value",
  "credential_expr", "holder_column", "time_column", "id_column", "source",
  optional "scope_column" + "scope_ref"}`: the operation row's credential_column holds a
  credential; the principal is the holder_column of the earliest record in table with
  action_column = action_value and credential_expr = credential (and scope_column = scope_ref
  when given), for example a credential-creation record. credential_expr is evaluated on the
  binding record, so it names where that record stores the credential's id.
- `{"kind": "binding_table", "credential_column", "table", "credential_table_column",
  "holder_column", "source"}`: an immutable credential -> holder table.
If no binding record exists the principal is unknown. Absence of a binding record never
establishes anything (it does not make the operation unauthorized). After the principal is
resolved, every column of the binding record is available as `binding.<column>`.

### scopes
A list of `{"name", "ref"}` (a value reference) or `{"name", "lookup_table", "lookup_key_expr",
"lookup_key_ref", "lookup_value_expr", optional "source"}` (the lookup_value_expr of the row of
lookup_table whose lookup_key_expr equals the referenced value; both expressions may be columns or
JSON extractions). Scopes are resolved in order, so a lookup may reference an earlier scope; scopes
that reference `principal` or `binding.<column>` are resolved after the principal.

### paths
A list of `{"name", "conditions": [...]}`. The operation is authorized iff at least one path holds;
a path holds iff all of its conditions hold. Every condition is evaluated at time t and yields
true, false or unknown; path = AND and verdict = OR under Kleene three-valued logic:
`authorized` if some path is true, `unauthorized` if every path is false, otherwise
`insufficient_evidence` (the unknown conditions are listed).

Condition kinds (every condition has `kind`, `table`, `source`):
- `change_log`: `{"action_column", "grant_value", "revoke_value", "scope_column", "scope_ref",
  "subject_expr", "subject_ref", "time_column", "id_column", optional "current_state_table",
  "current_state_scope_column", "current_state_subject_column", "current_state_source"}`.
  A relation subject-in-scope with grant/revoke change records. State at t = the state after the
  last change at or before t, if the source covers the time from that change to t; else the
  inverse of the first change after t, if the source covers the time from t to that change; else,
  if a current-state table is declared, no change after t is recorded and the source covers the
  time from t to the snapshot, the current-state table (present = true); otherwise unknown.
- `interval`: `{"subject_column", "subject_ref", optional "scope_column" + "scope_ref",
  "from_column", "to_column", "id_column"}`. Records with a validity interval; true if some record
  for the subject (and scope) has from_column <= t <= to_column (null to_column = open end); false
  only if the source covers t; otherwise unknown.
- `prior_event`: `{"action_column", "action_value", "object_column", "object_ref", "time_column",
  "id_column", optional "actor_column" + "actor_must_differ": true, optional "max_age_minutes"}`.
  True if a record with action_column = action_value on the referenced object exists strictly before
  t (by a different actor than the principal if actor_must_differ; no older than max_age_minutes if
  given); false only if the source is complete for that period (covers [t - max_age, t], or, without
  max_age, has no lower bound and covers t); otherwise unknown.

### coverage
`{"table", "source_column", "from_column", "to_column"}`: the table that states, per source, the
window (from, to) inside which the source is complete. A null bound is unbounded. Absence of a
record is informative only inside the window; closed-world reasoning is never applied outside it.

### Abstract example (names are invented; use the application's own)
An operation row in `ops(id, ts, key_id, project)` is performed with an API key; `keys(id, owner)`
binds keys to owners; `contracts(id, user, project, valid_from, valid_to)` lists contract periods;
`log(id, ts, action, object, actor)` records `op.approved` on operations; `windows(source, start,
end)` is the coverage table. Policy: authorized iff the key's owner has a contract for the
operation's project at t, or someone other than the owner approved the operation beforehand.
{"operation": {"table": "ops", "id_column": "id", "time_column": "ts"},
 "principal": {"kind": "binding_table", "credential_column": "key_id", "table": "keys",
               "credential_table_column": "id", "holder_column": "owner", "source": "keys"},
 "scopes": [{"name": "project", "ref": "operation.project"}],
 "paths": [
   {"name": "owner_under_contract", "conditions": [
     {"kind": "interval", "table": "contracts", "source": "contracts", "subject_column": "user",
      "subject_ref": "principal", "scope_column": "project", "scope_ref": "scope.project",
      "from_column": "valid_from", "to_column": "valid_to", "id_column": "id"}]},
   {"name": "approved_by_other", "conditions": [
     {"kind": "prior_event", "table": "log", "source": "log", "action_column": "action",
      "action_value": "op.approved", "object_column": "object", "object_ref": "operation.id",
      "time_column": "ts", "id_column": "id", "actor_column": "actor", "actor_must_differ": true}]}],
 "coverage": {"table": "windows", "source_column": "source", "from_column": "start", "to_column": "end"}}

### Output
`verdict`; `evidence` (records that establish each decided condition, cited as
`{"source": <table>, "id": <row id>}`; a current-state claim as `{"source": <table>, <scope column>:
<scope>, <subject column>: <subject>, "present": true|false}`; coverage references for absence-based
conclusions); `principal`; `unresolved`.
"""

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_JSON_EXPR = re.compile(r"^json_extract\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*'\$\.([A-Za-z_][A-Za-z0-9_]*)'\s*\)$")
_ARROW_EXPR = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*->>\s*'(?:\$\.)?([A-Za-z_][A-Za-z0-9_]*)'$")
_REF = re.compile(r"^(operation\.[A-Za-z_][A-Za-z0-9_]*|credential|principal|binding\.[A-Za-z_][A-Za-z0-9_]*"
                  r"|scope\.[A-Za-z_][A-Za-z0-9_]*)$")


class SpecError(ValueError):
    pass


def _ident(x, what="identifier"):
    if not isinstance(x, str) or not _IDENT.match(x.strip()):
        raise SpecError(f"bad {what}: {x!r}")
    return x.strip()


def _expr(e, what="expression"):
    if not isinstance(e, str):
        raise SpecError(f"bad {what}: {e!r}")
    e = e.strip()
    if _IDENT.match(e):
        return e
    m = _JSON_EXPR.match(e) or _ARROW_EXPR.match(e)
    if m:
        return f"json_extract({m.group(1)}, '$.{m.group(2)}')"
    raise SpecError(f"unsupported {what}: {e!r}")


def _ref(r, what="reference"):
    if not isinstance(r, str) or not _REF.match(r.strip()):
        raise SpecError(f"bad {what}: {r!r}")
    return r.strip()


def _need(obj, fields, where):
    for f in fields:
        if f not in obj or obj[f] in (None, ""):
            raise SpecError(f"{where}: missing field {f!r}")


# ---------------------------------------------------------------- validation / normalisation

def validate_spec(spec):
    """Check structure and identifiers; return a normalised copy. Raises SpecError (loudly)."""
    if not isinstance(spec, dict):
        raise SpecError("spec is not an object")
    out = {}
    op = spec.get("operation") or {}
    _need(op, ("table", "id_column", "time_column"), "operation")
    out["operation"] = {k: _ident(op[k], f"operation.{k}") for k in ("table", "id_column", "time_column")}

    pr = spec.get("principal") or {}
    kind = pr.get("kind")
    if kind not in PRINCIPAL_KINDS:
        raise SpecError(f"principal.kind must be one of {PRINCIPAL_KINDS}")
    p = {"kind": kind}
    if kind == "column":
        _need(pr, ("column",), "principal")
        p["column"] = _ident(pr["column"], "principal.column")
    elif kind == "binding_event":
        _need(pr, ("credential_column", "table", "action_column", "action_value", "credential_expr",
                   "holder_column", "time_column", "id_column"), "principal")
        for k in ("credential_column", "table", "action_column", "holder_column", "time_column", "id_column"):
            p[k] = _ident(pr[k], f"principal.{k}")
        p["action_value"] = str(pr["action_value"])
        p["credential_expr"] = _expr(pr["credential_expr"], "principal.credential_expr")
        if pr.get("scope_column") or pr.get("scope_ref"):
            _need(pr, ("scope_column", "scope_ref"), "principal")
            p["scope_column"] = _ident(pr["scope_column"], "principal.scope_column")
            p["scope_ref"] = _ref(pr["scope_ref"], "principal.scope_ref")
    else:  # binding_table
        _need(pr, ("credential_column", "table", "credential_table_column", "holder_column"), "principal")
        for k in ("credential_column", "table", "credential_table_column", "holder_column"):
            p[k] = _ident(pr[k], f"principal.{k}")
    if kind != "column":
        p["source"] = _ident(pr.get("source") or p["table"], "principal.source")
    out["principal"] = p

    scopes = spec.get("scopes")
    if scopes is None:
        scopes = []
    if isinstance(scopes, dict):  # tolerate {name: {...}} form
        scopes = [dict(v, name=k) for k, v in scopes.items()]
    if not isinstance(scopes, list):
        raise SpecError("scopes must be a list")
    out["scopes"] = []
    for i, s in enumerate(scopes):
        _need(s, ("name",), f"scopes[{i}]")
        name = _ident(s["name"], f"scopes[{i}].name")
        if s.get("ref"):
            out["scopes"].append({"name": name, "ref": _ref(s["ref"], f"scope {name} ref")})
        else:
            s = dict(s)  # 2.0 field names are accepted as aliases of the 2.1 expression fields
            s.setdefault("lookup_key_expr", s.get("lookup_key_column"))
            s.setdefault("lookup_value_expr", s.get("lookup_value_column"))
            _need(s, ("lookup_table", "lookup_key_expr", "lookup_key_ref", "lookup_value_expr"), f"scope {name}")
            out["scopes"].append({
                "name": name,
                "lookup_table": _ident(s["lookup_table"], f"scope {name} lookup_table"),
                "lookup_key_expr": _expr(s["lookup_key_expr"], f"scope {name} lookup_key_expr"),
                "lookup_key_ref": _ref(s["lookup_key_ref"], f"scope {name} lookup_key_ref"),
                "lookup_value_expr": _expr(s["lookup_value_expr"], f"scope {name} lookup_value_expr"),
                "source": _ident(s.get("source") or s["lookup_table"], f"scope {name} source"),
            })

    paths = spec.get("paths")
    if not isinstance(paths, list) or not paths:
        raise SpecError("paths must be a non-empty list")
    out["paths"] = []
    for i, path in enumerate(paths):
        conds = path.get("conditions") if isinstance(path, dict) else None
        if not isinstance(conds, list) or not conds:
            raise SpecError(f"paths[{i}]: conditions must be a non-empty list")
        name = str(path.get("name") or f"path_{i}")
        out["paths"].append({"name": name, "conditions": [_validate_condition(c, f"{name}[{j}]")
                                                           for j, c in enumerate(conds)]})

    cov = spec.get("coverage")
    if cov:
        _need(cov, ("table", "source_column", "from_column", "to_column"), "coverage")
        out["coverage"] = {k: _ident(cov[k], f"coverage.{k}") for k in ("table", "source_column", "from_column", "to_column")}
    else:
        out["coverage"] = None
    return out


def _validate_condition(c, where):
    if not isinstance(c, dict) or c.get("kind") not in KINDS:
        raise SpecError(f"{where}: kind must be one of {KINDS}")
    _need(c, ("table",), where)
    n = {"kind": c["kind"], "table": _ident(c["table"], f"{where}.table")}
    n["source"] = _ident(c.get("source") or n["table"], f"{where}.source")
    if c["kind"] == "change_log":
        _need(c, ("action_column", "grant_value", "revoke_value", "scope_column", "scope_ref", "subject_expr",
                  "subject_ref", "time_column", "id_column"), where)
        for k in ("action_column", "scope_column", "time_column", "id_column"):
            n[k] = _ident(c[k], f"{where}.{k}")
        n["grant_value"], n["revoke_value"] = str(c["grant_value"]), str(c["revoke_value"])
        if n["grant_value"] == n["revoke_value"]:
            raise SpecError(f"{where}: grant_value and revoke_value must differ")
        n["scope_ref"] = _ref(c["scope_ref"], f"{where}.scope_ref")
        n["subject_expr"] = _expr(c["subject_expr"], f"{where}.subject_expr")
        n["subject_ref"] = _ref(c["subject_ref"], f"{where}.subject_ref")
        if c.get("current_state_table"):
            _need(c, ("current_state_scope_column", "current_state_subject_column"), where)
            n["current_state"] = {
                "table": _ident(c["current_state_table"], f"{where}.current_state_table"),
                "scope_column": _ident(c["current_state_scope_column"], f"{where}.current_state_scope_column"),
                "subject_column": _ident(c["current_state_subject_column"], f"{where}.current_state_subject_column"),
            }
            n["current_state"]["source"] = _ident(c.get("current_state_source") or n["current_state"]["table"],
                                                  f"{where}.current_state_source")
    elif c["kind"] == "interval":
        _need(c, ("subject_column", "subject_ref", "from_column", "to_column", "id_column"), where)
        for k in ("subject_column", "from_column", "to_column", "id_column"):
            n[k] = _ident(c[k], f"{where}.{k}")
        n["subject_ref"] = _ref(c["subject_ref"], f"{where}.subject_ref")
        if c.get("scope_column") or c.get("scope_ref"):
            _need(c, ("scope_column", "scope_ref"), where)
            n["scope_column"] = _ident(c["scope_column"], f"{where}.scope_column")
            n["scope_ref"] = _ref(c["scope_ref"], f"{where}.scope_ref")
    else:  # prior_event
        _need(c, ("action_column", "action_value", "object_column", "object_ref", "time_column", "id_column"), where)
        for k in ("action_column", "object_column", "time_column", "id_column"):
            n[k] = _ident(c[k], f"{where}.{k}")
        n["action_value"] = str(c["action_value"])
        n["object_ref"] = _ref(c["object_ref"], f"{where}.object_ref")
        if c.get("actor_must_differ"):
            _need(c, ("actor_column",), where)
            n["actor_column"] = _ident(c["actor_column"], f"{where}.actor_column")
            n["actor_must_differ"] = True
        if c.get("max_age_minutes") not in (None, ""):
            try:
                n["max_age_minutes"] = int(c["max_age_minutes"])
            except (TypeError, ValueError):
                raise SpecError(f"{where}: max_age_minutes must be an integer")
            if n["max_age_minutes"] <= 0:
                raise SpecError(f"{where}: max_age_minutes must be positive")
    return n


# ---------------------------------------------------------------- time handling

def parse_ts(v):
    """ISO-8601 -> aware datetime (naive values are taken as UTC). None/invalid -> None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, tz=timezone.utc)
    s = str(v).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class _Unknown:
    def __repr__(self):
        return "UNKNOWN"


UNKNOWN = _Unknown()


# ---------------------------------------------------------------- evaluator

class Evaluator:
    def __init__(self, tool, spec, closed_world=False, current_state_only=False):
        self.tool, self.spec = tool, spec
        self.closed_world, self.current_state_only = closed_world, current_state_only
        self.decisive, self.context = [], []
        self.unresolved = []
        self.coverage = None  # source -> (from, to) parsed; {} when the table is empty / unreadable
        self.ctx = {"operation": None, "credential": UNKNOWN, "principal": UNKNOWN, "binding": None, "scope": {}}
        self.op_key = None

    # ---- helpers
    def _q(self, sql, params):
        return self.tool.query(sql, tuple(params))

    def _note(self, msg):
        if msg not in self.unresolved and len(self.unresolved) < 5:
            self.unresolved.append(msg)

    def _row_key(self, row, time_col, id_col):
        return (parse_ts(row.get(time_col)) or datetime.min.replace(tzinfo=timezone.utc), row.get(id_col) or 0)

    def resolve(self, ref):
        if ref == "credential":
            return self.ctx["credential"]
        if ref == "principal":
            return self.ctx["principal"]
        if ref.startswith("operation."):
            v = self.ctx["operation"].get(ref[len("operation."):])
            return UNKNOWN if v is None else v
        if ref.startswith("binding."):
            b = self.ctx["binding"]
            v = b.get(ref[len("binding."):]) if b else None
            return UNKNOWN if v is None else v
        if ref.startswith("scope."):
            return self.ctx["scope"].get(ref[len("scope."):], UNKNOWN)
        return UNKNOWN

    def _load_coverage(self):
        cov = self.spec["coverage"]
        self.coverage = {}
        if not cov:
            return
        q = self._q(f"SELECT {cov['source_column']} AS src, {cov['from_column']} AS f, {cov['to_column']} AS t "
                    f"FROM {cov['table']}", ())
        if not q["ok"]:
            self._note(f"coverage table unreadable: {q.get('error')}")
            return
        for r in q["rows"]:
            self.coverage[str(r["src"])] = (parse_ts(r["f"]), parse_ts(r["t"]), r["f"], r["t"])

    def covers(self, source, a, b):
        """Does the declared window of `source` contain the closed interval [a, b]?
        Under the closed-world ablation every absence is informative, so always True."""
        if self.closed_world:
            return True
        w = (self.coverage or {}).get(source)
        if w is None or a is None or b is None:
            return False
        f, t = w[0], w[1]
        return (f is None or f <= a) and (t is None or b <= t)

    def cov_ref(self, source):
        w = (self.coverage or {}).get(source)
        cov = self.spec["coverage"]
        if w is None or not cov:
            return None
        return {"source": cov["table"], "for": source, "covered_from": w[2], "covered_to": w[3], "role": "context"}

    def _cite(self, item, decisive):
        (self.decisive if decisive else self.context).append(item)

    # ---- resolution
    def resolve_operation(self):
        op = self.spec["operation"]
        q = self._q(f"SELECT * FROM {op['table']} WHERE {op['id_column']} = ?", [self.tool.alert["alert"]["audit_log_id"]])
        if not q["ok"] or not q["rows"]:
            self._note("operation record not found")
            return False
        row = q["rows"][0]
        self.ctx["operation"] = row
        self.t = parse_ts(row.get(op["time_column"]))
        if self.t is None:
            self._note("operation time unreadable")
            return False
        self.op_key = (self.t, row.get(op["id_column"]) or 0)
        self._cite({"source": op["table"], "id": row.get(op["id_column"]), "role": "context"}, False)
        pr = self.spec["principal"]
        if pr["kind"] != "column":  # the credential is a value of the operation row; scopes may depend on it
            cred = row.get(pr["credential_column"])
            self.ctx["credential"] = UNKNOWN if cred is None else cred
        return True

    def resolve_principal(self):
        pr = self.spec["principal"]
        if pr["kind"] == "column":
            v = self.ctx["operation"].get(pr["column"])
            if v is None:
                self._note("principal column is null on the operation record")
                return
            self.ctx["principal"] = v
            return
        cred = self.ctx["credential"]
        if cred is UNKNOWN:
            self._note("operation carries no credential")
            return
        if pr["kind"] == "binding_table":
            q = self._q(f"SELECT * FROM {pr['table']} WHERE {pr['credential_table_column']} = ?", [cred])
            if not q["ok"] or not q["rows"] or q["rows"][0].get(pr["holder_column"]) is None:
                self._note("holder binding record not available")
                return
            self.ctx["binding"] = q["rows"][0]
            self.ctx["principal"] = q["rows"][0][pr["holder_column"]]
            self._cite({"source": pr["table"], pr["credential_table_column"]: cred,
                        pr["holder_column"]: self.ctx["principal"], "role": "context"}, False)
            return
        # binding_event
        where = f"{pr['action_column']} = ? AND {pr['credential_expr']} = ?"
        params = [pr["action_value"], cred]
        if "scope_column" in pr:
            scope = self.resolve(pr["scope_ref"])
            if scope is UNKNOWN:
                self._note(f"scope for principal binding ({pr['scope_ref']}) unresolved")
                return
            where += f" AND {pr['scope_column']} = ?"
            params.append(scope)
        q = self._q(f"SELECT * FROM {pr['table']} WHERE {where} ORDER BY {pr['time_column']}, {pr['id_column']}", params)
        if not q["ok"]:
            self._note(f"holder binding query failed: {q.get('error')}")
            return
        if not q["rows"] or q["rows"][0].get(pr["holder_column"]) is None:
            self._note("holder binding record not available")
            return
        self.ctx["binding"] = q["rows"][0]
        self.ctx["principal"] = q["rows"][0][pr["holder_column"]]
        self._cite({"source": pr["table"], "id": q["rows"][0][pr["id_column"]], "role": "context"}, False)

    def _principal_dependent(self):
        """Names of scopes whose value depends (transitively) on the principal."""
        dep = set()
        for s in self.spec["scopes"]:
            r = s.get("ref") or s.get("lookup_key_ref")
            if r == "principal" or r.startswith("binding.") or (r.startswith("scope.") and r[6:] in dep):
                dep.add(s["name"])
        return dep

    def resolve_scopes(self, after_principal):
        """Pass 1 (before the principal is known) resolves principal-independent scopes, pass 2 the rest."""
        dep = self._principal_dependent()
        for s in self.spec["scopes"]:
            if (s["name"] in dep) != after_principal:
                continue
            if "ref" in s:
                v = self.resolve(s["ref"])
            else:
                key = self.resolve(s["lookup_key_ref"])
                if key is UNKNOWN:
                    v = UNKNOWN
                else:
                    q = self._q(f"SELECT {s['lookup_value_expr']} AS v FROM {s['lookup_table']} "
                                f"WHERE {s['lookup_key_expr']} = ?", [key])
                    v = q["rows"][0]["v"] if q["ok"] and q["rows"] and q["rows"][0]["v"] is not None else UNKNOWN
            if v is UNKNOWN:
                self._note(f"scope {s['name']} unresolved")
            self.ctx["scope"][s["name"]] = v

    # ---- conditions
    def _unknown_if_principal(self, refs):
        """Conditions that need an unknown value are unknown; under closed-world they are false."""
        for r in refs:
            if self.resolve(r) is UNKNOWN:
                return False if self.closed_world else None
        return True

    def cond_change_log(self, c):
        ok = self._unknown_if_principal((c["scope_ref"], c["subject_ref"]))
        if ok is not True:
            return ok, f"{c['scope_ref']} or {c['subject_ref']} unresolved"
        scope, subject = self.resolve(c["scope_ref"]), self.resolve(c["subject_ref"])
        src = c["source"]
        before, after = [], []
        if not self.current_state_only:  # the ablation never reads change records
            q = self._q(f"SELECT {c['id_column']} AS id, {c['time_column']} AS ts, {c['action_column']} AS action "
                        f"FROM {c['table']} WHERE {c['action_column']} IN (?, ?) AND {c['scope_column']} = ? "
                        f"AND {c['subject_expr']} = ? ORDER BY {c['time_column']}, {c['id_column']}",
                        [c["grant_value"], c["revoke_value"], scope, subject])
            if not q["ok"]:
                return None, f"query failed: {q.get('error')}"
            if q["truncated"]:
                return None, "change records exceed the row limit"
            rows = [(self._row_key(r, "ts", "id"), r) for r in q["rows"]]
            before = [r for k, r in rows if k < self.op_key]
            after = [r for k, r in rows if k > self.op_key]
            if before:
                r = before[-1]
                if self.covers(src, parse_ts(r["ts"]), self.t):
                    state = r["action"] == c["grant_value"]
                    self._cite({"source": c["table"], "id": r["id"],
                                "role": "supports_authorization" if state else "supports_violation"}, True)
                    self._cite(self.cov_ref(src), False)
                    return state, "last change at or before t"
            if after:
                r = after[0]
                if self.covers(src, self.t, parse_ts(r["ts"])):
                    state = r["action"] == c["revoke_value"]
                    self._cite({"source": c["table"], "id": r["id"],
                                "role": "supports_authorization" if state else "supports_violation"}, True)
                    self._cite(self.cov_ref(src), False)
                    return state, "first change after t"
        cs = c.get("current_state")
        if cs is None:
            if self.closed_world:
                return False, "no change record (closed world)"
            return None, "no usable change record and no current-state table"
        snap = (self.coverage or {}).get(cs["source"])
        snap_time = snap[0] if snap else None
        usable = self.current_state_only or self.closed_world or (
            not after and snap_time is not None and self.covers(src, self.t, snap_time))
        if not usable:
            return None, "current state not usable at t (later change or coverage gap)"
        q = self._q(f"SELECT 1 AS present FROM {cs['table']} WHERE {cs['scope_column']} = ? "
                    f"AND {cs['subject_column']} = ?", [scope, subject])
        if not q["ok"]:
            return None, f"query failed: {q.get('error')}"
        present = bool(q["rows"])
        self._cite({"source": cs["table"], cs["scope_column"]: scope, cs["subject_column"]: subject,
                    "present": present, "role": "supports_authorization" if present else "supports_violation"}, True)
        if not (self.current_state_only or self.closed_world):
            self._cite(self.cov_ref(src), False)
        return present, "current state (no later change)"

    def cond_interval(self, c):
        refs = [c["subject_ref"]] + ([c["scope_ref"]] if "scope_ref" in c else [])
        ok = self._unknown_if_principal(refs)
        if ok is not True:
            return ok, "subject or scope unresolved"
        subject = self.resolve(c["subject_ref"])
        where, params = f"{c['subject_column']} = ?", [subject]
        if "scope_ref" in c:
            where += f" AND {c['scope_column']} = ?"
            params.append(self.resolve(c["scope_ref"]))
        q = self._q(f"SELECT {c['id_column']} AS id, {c['from_column']} AS f, {c['to_column']} AS t FROM {c['table']} "
                    f"WHERE {where} ORDER BY {c['from_column']}, {c['id_column']}", params)
        if not q["ok"]:
            return None, f"query failed: {q.get('error')}"
        covering = []
        for r in q["rows"]:
            f, t = parse_ts(r["f"]), parse_ts(r["t"])
            if f is not None and f <= self.t and (r["t"] is None or (t is not None and self.t <= t)):
                covering.append(r)
        if covering:
            self._cite({"source": c["table"], "id": covering[0]["id"], "role": "supports_authorization"}, True)
            return True, "interval covers t"
        if q["truncated"]:
            return None, "interval records exceed the row limit"
        if self.covers(c["source"], self.t, self.t):
            self._cite(self.cov_ref(c["source"]), True)
            return False, "no interval covers t (source complete for t)"
        return None, "no interval covers t and source not complete for t"

    def cond_prior_event(self, c):
        refs = [c["object_ref"]] + (["principal"] if c.get("actor_must_differ") else [])
        ok = self._unknown_if_principal(refs)
        if ok is not True:
            return ok, "object or principal unresolved"
        obj = self.resolve(c["object_ref"])
        cols = f"{c['id_column']} AS id, {c['time_column']} AS ts"
        if c.get("actor_must_differ"):
            cols += f", {c['actor_column']} AS actor"
        q = self._q(f"SELECT {cols} FROM {c['table']} WHERE {c['action_column']} = ? AND {c['object_column']} = ? "
                    f"ORDER BY {c['time_column']} DESC, {c['id_column']} DESC", [c["action_value"], obj])
        if not q["ok"]:
            return None, f"query failed: {q.get('error')}"
        lo = self.t - timedelta(minutes=c["max_age_minutes"]) if "max_age_minutes" in c else None
        prior = []
        for r in q["rows"]:
            k = self._row_key(r, "ts", "id")
            if k >= self.op_key or (lo is not None and k[0] < lo):
                continue
            if c.get("actor_must_differ") and r.get("actor") == self.ctx["principal"]:
                continue
            prior.append(r)
        if prior:
            self._cite({"source": c["table"], "id": prior[0]["id"], "role": "supports_authorization"}, True)
            return True, "prior event found"
        if q["truncated"]:
            return None, "prior-event records exceed the row limit"
        if self.closed_world:
            return False, "no prior event (closed world)"
        w = (self.coverage or {}).get(c["source"])
        if lo is not None:
            complete = self.covers(c["source"], lo, self.t)
        else:
            complete = w is not None and w[0] is None and (w[1] is None or self.t <= w[1])
        if complete:
            self._cite(self.cov_ref(c["source"]), True)
            return False, "no prior event (source complete for the period)"
        return None, "no prior event and source not complete for the period"

    # ---- top level
    def run(self):
        out = {"verdict": "insufficient_evidence", "evidence": [], "principal": None, "unresolved": [], "paths": []}
        try:
            if not self.resolve_operation():
                return self._finish(out)
            self._load_coverage()
            self.resolve_scopes(after_principal=False)
            self.resolve_principal()
            self.resolve_scopes(after_principal=True)
            path_values = []
            for path in self.spec["paths"]:
                pv, conds = True, []
                for c in path["conditions"]:
                    fn = {"change_log": self.cond_change_log, "interval": self.cond_interval,
                          "prior_event": self.cond_prior_event}[c["kind"]]
                    v, basis = fn(c)
                    conds.append({"kind": c["kind"], "table": c["table"], "value": v, "basis": basis})
                    if v is None:
                        self._note(f"{path['name']}: {c['kind']} on {c['table']} undetermined ({basis})")
                    pv = _and(pv, v)
                out["paths"].append({"name": path["name"], "value": pv, "conditions": conds})
                path_values.append(pv)
            if any(v is True for v in path_values):
                out["verdict"] = "authorized"
            elif all(v is False for v in path_values):
                out["verdict"] = "unauthorized"
        except BudgetExhausted:
            self._note("query budget exhausted")
        return self._finish(out)

    def _finish(self, out):
        p = self.ctx["principal"]
        out["principal"] = None if p is UNKNOWN else p
        seen, ev = set(), []
        for item in self.decisive + self.context:
            if item is None:
                continue
            key = tuple(sorted((k, str(v)) for k, v in item.items()))
            if key not in seen:
                seen.add(key)
                ev.append(item)
        out["evidence"] = ev[:EVIDENCE_CAP]
        out["unresolved"] = self.unresolved[:5]
        return out


def _and(a, b):
    """Kleene AND over {True, False, None}."""
    if a is False or b is False:
        return False
    if a is None or b is None:
        return None
    return True


def evaluate(tool, spec, closed_world=False, current_state_only=False):
    """spec must already be validated (validate_spec). Returns the submission dict."""
    return Evaluator(tool, spec, closed_world=closed_world, current_state_only=current_state_only).run()
