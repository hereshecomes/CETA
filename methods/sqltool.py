"""Shared read-only SQL tool. Every investigation method uses exactly this interface."""
import json
import sqlite3
from pathlib import Path


class BudgetExhausted(RuntimeError):
    pass


class SQLTool:
    def __init__(self, case_dir, max_queries=12, max_rows=40, max_cell_chars=400):
        self.case_dir = Path(case_dir)
        self.alert = json.loads((self.case_dir / "alert.json").read_text())
        self.con = sqlite3.connect(f"file:{self.case_dir / 'episode.sqlite'}?mode=ro", uri=True)
        self.con.row_factory = sqlite3.Row
        self.con.set_authorizer(self._authorize)
        self.max_queries, self.max_rows, self.max_cell_chars = max_queries, max_rows, max_cell_chars
        self.queries = 0
        self.rows_returned = 0
        self.log = []
        self._tables = set()
        self.observed_audit_ids = set()
        self.observed_tables = set()

    def _authorize(self, action, arg1, *args):
        if action == sqlite3.SQLITE_READ and arg1:
            self._tables.add(arg1)
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
        return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

    def schema(self):
        rows = self.con.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return {r["name"]: r["sql"] for r in rows}

    def query(self, sql, params=()):
        if self.queries >= self.max_queries:
            raise BudgetExhausted("query budget exhausted")
        self.queries += 1
        entry = {"n": self.queries, "sql": sql, "params": list(params)}
        self._tables = set()
        try:
            cur = self.con.execute(sql, params)
            raw = cur.fetchmany(self.max_rows + 1)
            truncated = len(raw) > self.max_rows
            rows = []
            for r in raw[: self.max_rows]:
                d = {}
                for k in r.keys():
                    v = r[k]
                    if isinstance(v, str) and len(v) > self.max_cell_chars:
                        v = v[: self.max_cell_chars] + "...[truncated]"
                    d[k] = v
                rows.append(d)
            self.rows_returned += len(rows)
            self.observed_tables |= self._tables
            if "audit_log" in self._tables:
                ids = [r["id"] for r in rows if isinstance(r.get("id"), int)]
                self.observed_audit_ids.update(ids)
                entry["audit_ids"] = ids
            entry.update(ok=True, n_rows=len(rows), truncated=truncated, tables=sorted(self._tables))
            self.log.append(entry)
            return {"ok": True, "rows": rows, "truncated": truncated,
                    "remaining_queries": self.max_queries - self.queries}
        except sqlite3.Error as exc:
            entry.update(ok=False, error=str(exc)[:200])
            self.log.append(entry)
            return {"ok": False, "error": str(exc)[:200], "remaining_queries": self.max_queries - self.queries}

    def cost(self):
        return {"queries": self.queries, "rows_returned": self.rows_returned, "max_queries": self.max_queries}
