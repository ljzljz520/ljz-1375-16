"""关系库：路线几何（版本化）、线段、公告/规则/修订、命中关系、
矛盾（待核）、版本账本、用户已查看确认、离线计划。

时间采用双时间轴：
- valid_at：规则/公告在现实世界的生效时间
- observed_at：系统收录时间（迟到撤销只改 observed_at，绝不回改历史）
"""
import json
import os
import sqlite3
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
  node_id TEXT PRIMARY KEY,
  name    TEXT NOT NULL,
  x       REAL NOT NULL,
  y       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS routes (
  route_id   TEXT NOT NULL,
  version    INTEGER NOT NULL,
  name       TEXT NOT NULL,
  kind       TEXT DEFAULT '步道',
  note       TEXT,
  current    INTEGER DEFAULT 0,
  valid_from TEXT,
  superseded_at TEXT,
  PRIMARY KEY (route_id, version)
);

CREATE TABLE IF NOT EXISTS segments (
  seg_key     TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  from_node   TEXT NOT NULL,
  to_node     TEXT NOT NULL,
  bends       TEXT DEFAULT '[]',
  attrs       TEXT DEFAULT '{}',
  service_weekly TEXT
);

CREATE TABLE IF NOT EXISTS route_members (
  route_id TEXT NOT NULL,
  version  INTEGER NOT NULL,
  seq      INTEGER NOT NULL,
  seg_key  TEXT NOT NULL,
  reversed INTEGER DEFAULT 0,
  PRIMARY KEY (route_id, version, seq)
);

CREATE TABLE IF NOT EXISTS announcements (
  ann_id       TEXT PRIMARY KEY,
  source       TEXT NOT NULL,
  source_level TEXT DEFAULT '景区物业',
  title        TEXT NOT NULL,
  status       TEXT DEFAULT 'active',           -- active / revoked
  observed_at  TEXT NOT NULL,                    -- 收录时间
  revoked_at   TEXT,                             -- 撤销生效时间
  revoke_observed_at TEXT,                       -- 撤文迟到收录时间
  revision_of  TEXT,                             -- 修订了哪条旧公告
  body         TEXT
);

CREATE TABLE IF NOT EXISTS rules (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  rule_key    TEXT NOT NULL,
  ann_id      TEXT NOT NULL,
  version     INTEGER NOT NULL,
  status      TEXT DEFAULT 'active',             -- active / superseded / revoked
  effect      TEXT NOT NULL,                     -- block / allow / info
  rule_type   TEXT NOT NULL,                     -- construction/control/age/accessibility/shift/info
  title       TEXT NOT NULL,
  geom_type   TEXT DEFAULT 'polyline',           -- polyline / polygon
  geometry    TEXT NOT NULL,                     -- JSON 坐标
  direction   TEXT DEFAULT 'B',
  windows     TEXT,                              -- JSON 绝对生效区间
  weekly      TEXT,                              -- JSON 周期时段
  min_age     INTEGER,
  population  TEXT,                              -- wheelchair / elderly / child / none
  note        TEXT,
  valid_from  TEXT,
  valid_to    TEXT,
  observed_at TEXT NOT NULL,
  superseded_at TEXT,
  UNIQUE (rule_key, ann_id, version)
);

CREATE TABLE IF NOT EXISTS rule_matches (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  rule_id     INTEGER NOT NULL,
  seg_key     TEXT NOT NULL,
  hit         TEXT NOT NULL,                     -- JSON 命中区间
  point_only  INTEGER DEFAULT 0,
  contact_only INTEGER DEFAULT 0,                  -- 仅共享节点接触(不据此封闭)
  UNIQUE (rule_id, seg_key)
);

CREATE TABLE IF NOT EXISTS contradictions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  rule_a INTEGER NOT NULL,
  rule_b INTEGER NOT NULL,
  seg_key TEXT NOT NULL,
  detected_at TEXT NOT NULL,
  resolved_at TEXT,
  note TEXT
);

CREATE TABLE IF NOT EXISTS data_versions (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  bump INTEGER DEFAULT 1,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS acknowledgements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  rule_id INTEGER NOT NULL,
  rule_version INTEGER NOT NULL,
  acknowledged_at TEXT NOT NULL,
  UNIQUE (user_id, rule_id, rule_version)
);

CREATE TABLE IF NOT EXISTS offline_plans (
  plan_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  saved_at TEXT NOT NULL,
  query_time TEXT NOT NULL,
  payload TEXT NOT NULL
);
"""


def connect(path=DB_PATH):
    first = not os.path.exists(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    if first:
        bump(conn)
    return conn


def bump(conn):
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO data_versions(id,bump,updated_at) VALUES(1,1,?) "
        "ON CONFLICT(id) DO UPDATE SET bump=bump+1, updated_at=excluded.updated_at",
        (now,))
    conn.commit()


def version_bump(conn):
    r = conn.execute("SELECT bump, updated_at FROM data_versions WHERE id=1").fetchone()
    return {"bump": r["bump"], "updated_at": r["updated_at"]}


def jloads(s, default):
    if not s:
        return default
    return json.loads(s)
