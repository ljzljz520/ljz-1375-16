"""关系库结构：路线几何、规则、公告修订分别落表。"""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS places(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  x REAL NOT NULL,
  y REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS routes(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT
);
-- 路线几何：每条路线由有序片段组成，改线会产生新版本片段
CREATE TABLE IF NOT EXISTS segments(
  id TEXT PRIMARY KEY,          -- route:vN:seq
  route_id TEXT NOT NULL,
  route_version INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  a TEXT NOT NULL,              -- 起点节点
  b TEXT NOT NULL,              -- 终点节点
  geom TEXT NOT NULL,           -- JSON 折线 [[x,y],...]
  length_m REAL NOT NULL,
  oneway INTEGER NOT NULL DEFAULT 0,   -- 1=仅正向(a->b)
  slope_pct REAL NOT NULL DEFAULT 0,   -- 坡度%
  step_free INTEGER NOT NULL DEFAULT 1,-- 0=有台阶
  width_m REAL NOT NULL DEFAULT 2.0,   -- 通行净宽
  unfenced_water INTEGER NOT NULL DEFAULT 0 -- 1=临水无护栏
);
-- 公告本体（施工/班次/适行限制）
CREATE TABLE IF NOT EXISTS alerts(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,           -- construction | schedule | access_restriction
  source TEXT NOT NULL,         -- 来源
  title TEXT NOT NULL,
  created_at TEXT NOT NULL
);
-- 公告修订：每次变更（含撤销）都是新修订；received_at 与 effective_at 分离以识别"迟到撤销"
CREATE TABLE IF NOT EXISTS alert_revisions(
  alert_id TEXT NOT NULL,
  rev INTEGER NOT NULL,
  geom TEXT,                    -- JSON 几何（polygon/line/point）
  direction TEXT NOT NULL DEFAULT 'both',  -- forward|backward|both（相对片段行进方向）
  starts_at TEXT,               -- 绝对生效窗口
  ends_at TEXT,
  daily_start TEXT,             -- 每日窗口 HH:MM（可跨日，如 22:00-06:00）
  daily_end TEXT,
  rule TEXT NOT NULL DEFAULT '{}',  -- JSON 规则：closed/excludes/service
  note TEXT,
  effective_at TEXT,            -- 来源侧生效时间
  received_at TEXT NOT NULL,    -- 系统收到时间
  revoked INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(alert_id, rev)
);
-- 来源上报的路段状态（用于矛盾检测）
CREATE TABLE IF NOT EXISTS reports(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  segment_id TEXT NOT NULL,
  direction TEXT NOT NULL DEFAULT 'both',
  starts_at TEXT,
  ends_at TEXT,
  state TEXT NOT NULL,          -- open | closed
  received_at TEXT NOT NULL
);
-- 来源矛盾 → 待核，绝不自行拼接成通行保证
CREATE TABLE IF NOT EXISTS conflicts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  segment_id TEXT NOT NULL,
  direction TEXT NOT NULL,
  starts_at TEXT,
  ends_at TEXT,
  sources TEXT NOT NULL,        -- JSON 来源列表
  detail TEXT,
  status TEXT NOT NULL DEFAULT 'pending_review',  -- pending_review | resolved
  created_at TEXT NOT NULL
);
-- 已查看确认：只对应当时版本（alert_id+rev）
CREATE TABLE IF NOT EXISTS acks(
  user_id TEXT NOT NULL,
  alert_id TEXT NOT NULL,
  rev INTEGER NOT NULL,
  acked_at TEXT NOT NULL,
  PRIMARY KEY(user_id, alert_id)
);
-- 离线计划：保存请求、结果与数据快照，供重连比对
CREATE TABLE IF NOT EXISTS plans(
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  req TEXT NOT NULL,
  result TEXT NOT NULL,
  snapshot TEXT NOT NULL,       -- {alert_revs:{}, route_versions:{}}
  created_at TEXT NOT NULL
);
"""


def connect(path=":memory:"):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
