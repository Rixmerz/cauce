"""A livespec index with the tables and columns cauce reads, as livespec 0.32 lays them out."""
from __future__ import annotations

import sqlite3
from pathlib import Path

DDL = """
CREATE TABLE project (id INTEGER PRIMARY KEY, name TEXT NOT NULL, root TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE file (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, path TEXT NOT NULL,
  language TEXT NOT NULL, content_hash TEXT NOT NULL, line_count INTEGER NOT NULL, mtime REAL NOT NULL,
  indexed_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE(project_id, path));
CREATE TABLE symbol (id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, parent_symbol_id INTEGER,
  name TEXT NOT NULL, qualified_name TEXT NOT NULL, kind TEXT NOT NULL, signature TEXT,
  start_line INTEGER NOT NULL, end_line INTEGER NOT NULL);
CREATE TABLE symbol_edge (id INTEGER PRIMARY KEY, src_symbol_id INTEGER NOT NULL, dst_symbol_id INTEGER NOT NULL,
  edge_type TEXT NOT NULL, weight REAL NOT NULL DEFAULT 1.0, origin TEXT NOT NULL DEFAULT 'livespec',
  UNIQUE(src_symbol_id, dst_symbol_id, edge_type));
CREATE TABLE spec (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, spec_id TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'functional_requirement', title TEXT NOT NULL, description TEXT,
  status TEXT NOT NULL DEFAULT 'draft', priority TEXT NOT NULL DEFAULT 'medium');
CREATE TABLE spec_symbol (id INTEGER PRIMARY KEY, spec_id INTEGER NOT NULL, symbol_id INTEGER NOT NULL,
  relation TEXT NOT NULL DEFAULT 'implements', confidence REAL NOT NULL DEFAULT 1.0,
  source TEXT NOT NULL DEFAULT 'manual');
CREATE TABLE chunk (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, source_type TEXT NOT NULL,
  source_id INTEGER, text_kind TEXT NOT NULL, file_path TEXT, start_line INTEGER, end_line INTEGER,
  text TEXT NOT NULL, content_hash TEXT NOT NULL);
CREATE VIRTUAL TABLE chunk_fts USING fts5(text, content='chunk', content_rowid='id', tokenize='unicode61');
CREATE TRIGGER chunk_ai AFTER INSERT ON chunk BEGIN
  INSERT INTO chunk_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TABLE index_run (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL,
  started_at TEXT NOT NULL DEFAULT (datetime('now')), finished_at TEXT);
"""


def build(root: Path, *, indexed_at: str = "2999-01-01 00:00:00", project_root: Path | None = None) -> Path:
    """An index of a small payments codebase: a parser everyone calls, a charge
    function behind a critical spec, and tests."""
    db = root / ".mcp-docs" / "docs.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(DDL)
    conn.execute("INSERT INTO project (id, name, root) VALUES (1, 'r', ?)", (str(project_root or root),))
    files = {"src/parse.py": 1, "src/billing.py": 2, "src/api.py": 3, "tests/test_parse.py": 4}
    for path, fid in files.items():
        conn.execute("INSERT INTO file (id, project_id, path, language, content_hash, line_count, mtime) "
                     "VALUES (?, 1, ?, 'python', 'h', 10, 0)", (fid, path))
    symbols = [
        (1, 1, "parse_amount", "src.parse.parse_amount", "turn a user-typed amount into cents"),
        (2, 2, "charge_card", "src.billing.charge_card", "charge the card for an amount in cents"),
        (3, 3, "checkout", "src.api.checkout", "the checkout endpoint calls charge and parse"),
        (4, 4, "test_parse_amount", "tests.test_parse.test_parse_amount", "test parse amount cents"),
    ]
    for sid, fid, name, qname, text in symbols:
        conn.execute("INSERT INTO symbol (id, file_id, name, qualified_name, kind, start_line, end_line) "
                     "VALUES (?, ?, ?, ?, 'function', ?, ?)", (sid, fid, name, qname, sid * 10, sid * 10 + 5))
        path = next(p for p, f in files.items() if f == fid)
        conn.execute("INSERT INTO chunk (project_id, source_type, source_id, text_kind, file_path, text, content_hash)"
                     " VALUES (1, 'symbol', ?, 'code', ?, ?, 'h')", (sid, path, f"{name} {text}"))
    # checkout -> parse, checkout -> charge, test -> parse; plus many callers of parse
    edges = [(3, 1), (3, 2), (4, 1)]
    for i in range(20):
        sid = 100 + i
        conn.execute("INSERT INTO symbol (id, file_id, name, qualified_name, kind, start_line, end_line) "
                     "VALUES (?, 3, ?, ?, 'function', 1, 2)", (sid, f"handler_{i}", f"src.api.handler_{i}"))
        edges.append((sid, 1))
    for src, dst in edges:
        conn.execute("INSERT INTO symbol_edge (src_symbol_id, dst_symbol_id, edge_type) VALUES (?, ?, 'calls')",
                     (src, dst))
    conn.execute("INSERT INTO spec (id, project_id, spec_id, title, priority) VALUES "
                 "(1, 1, 'SPEC-7', 'Card charges are idempotent', 'critical')")
    conn.execute("INSERT INTO spec_symbol (spec_id, symbol_id) VALUES (1, 2)")
    conn.execute("INSERT INTO index_run (project_id, finished_at) VALUES (1, ?)", (indexed_at,))
    conn.commit()
    conn.close()
    return db
