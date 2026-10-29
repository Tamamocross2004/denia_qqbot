"""聊天记忆的持久化层。

三张表分工：
  msg    —— 每群只留最近 30 条原文，纯粹当重启缓冲，超了就删
  person —— 群友档案。uid 是主键，昵称只是会变的属性，旧名进 aka
  memory —— 提炼出来的长期记忆，只关联 uid，不存昵称

设计要点：记忆条目的 content 里人名存成 @{uid} 占位符，渲染时再换成
当前昵称。这样群友改名之后，旧记忆里的名字也跟着变。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "memory.db"

# 每群保留多少条原文当重启缓冲
MSG_KEEP = 30

_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS msg(
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    gid  INTEGER NOT NULL,
    uid  INTEGER NOT NULL,
    nick TEXT,
    text TEXT NOT NULL,
    ts   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_gid ON msg(gid, ts);

CREATE TABLE IF NOT EXISTS person(
    uid        INTEGER PRIMARY KEY,
    nick       TEXT,
    aka        TEXT,
    first_seen INTEGER,
    last_seen  INTEGER,
    msg_count  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS memory(
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    gid     INTEGER NOT NULL,
    uid     INTEGER DEFAULT 0,
    kind    TEXT,
    content TEXT NOT NULL,
    ts      INTEGER NOT NULL,
    weight  REAL DEFAULT 0.5,
    used_at INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mem_gid ON memory(gid, uid);

-- 视频转写缓存：同一个视频只跑一次 ASR
CREATE TABLE IF NOT EXISTS video(
    vid        TEXT PRIMARY KEY,      -- B站用 bvid，其它平台用 "平台:id"
    title      TEXT,
    up         TEXT,
    seconds    INTEGER DEFAULT 0,
    transcript TEXT,
    ts         INTEGER NOT NULL
);

-- 对话历史：进程重启后还能接上「你们刚说到哪」
CREATE TABLE IF NOT EXISTS turn(
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    sid     TEXT NOT NULL,            -- 会话键：g<群号>u<QQ> / p<QQ>
    gid     INTEGER DEFAULT 0,
    uid     INTEGER DEFAULT 0,
    role    TEXT NOT NULL,            -- user / assistant
    content TEXT NOT NULL,
    ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turn_sid ON turn(sid, id);
"""


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def close() -> None:
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except sqlite3.Error:
            pass
        _conn = None


# ---------- 原文缓冲 ----------

def save_msg(gid: int, uid: int, nick: str, text: str, ts: int | None = None) -> None:
    """写一条消息，顺手把该群超出 MSG_KEEP 的旧消息删掉。"""
    if not gid or not text:
        return
    ts = ts or int(time.time())
    db = connect()
    db.execute("INSERT INTO msg(gid, uid, nick, text, ts) VALUES(?,?,?,?,?)",
               (gid, uid, nick or "", text[:500], ts))
    # 只留最近 MSG_KEEP 条
    db.execute(
        "DELETE FROM msg WHERE gid=? AND id NOT IN "
        "(SELECT id FROM msg WHERE gid=? ORDER BY id DESC LIMIT ?)",
        (gid, gid, MSG_KEEP))
    db.commit()


def recent_msgs(gid: int, limit: int = MSG_KEEP) -> list[dict]:
    """按时间正序返回，方便直接当聊天记录用。"""
    rows = connect().execute(
        "SELECT uid, nick, text, ts FROM msg WHERE gid=? ORDER BY id DESC LIMIT ?",
        (gid, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


# ---------- 对话历史 ----------
# 以前 history 只在内存里，进程一重启就断片：她记得群里发生过什么，
# 不记得自己上一句说过什么。落一张表就能接上。

TURN_KEEP = 20          # 每个会话留多少条（= 10 轮）


def save_turn(sid: str, role: str, content: str,
              gid: int = 0, uid: int = 0, keep: int = TURN_KEEP) -> None:
    if not sid or not content:
        return
    db = connect()
    db.execute(
        "INSERT INTO turn(sid, gid, uid, role, content, ts) VALUES(?,?,?,?,?,?)",
        (sid, int(gid or 0), int(uid or 0), role, content[:4000], int(time.time())))
    db.execute(
        "DELETE FROM turn WHERE sid=? AND id NOT IN "
        "(SELECT id FROM turn WHERE sid=? ORDER BY id DESC LIMIT ?)",
        (sid, sid, keep))
    db.commit()


def recent_turns(sid: str, limit: int = TURN_KEEP) -> list[dict]:
    """按时间正序返回 [{'role','content'}]，直接塞进 messages 用。"""
    if not sid:
        return []
    rows = connect().execute(
        "SELECT role, content FROM turn WHERE sid=? ORDER BY id DESC LIMIT ?",
        (sid, limit)).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def clear_turns(sid: str) -> int:
    if not sid:
        return 0
    db = connect()
    n = db.execute("DELETE FROM turn WHERE sid=?", (sid,)).rowcount
    db.commit()
    return n


def ping() -> str:
    """自检用：库在哪、各表多少行。"""
    db = connect()
    parts = []
    for t in ("msg", "person", "memory", "turn"):
        n = db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        parts.append(f"{t}={n}")
    return f"{DB_PATH.name} " + " ".join(parts)


# ---------- 群友档案 ----------

def touch_person(uid: int, nick: str, ts: int | None = None) -> None:
    """登记/更新一个人。改名了就把旧名塞进 aka。"""
    if not uid:
        return
    ts = ts or int(time.time())
    nick = (nick or "").strip()[:40]
    db = connect()
    row = db.execute("SELECT nick, aka FROM person WHERE uid=?", (uid,)).fetchone()

    if row is None:
        db.execute(
            "INSERT INTO person(uid, nick, aka, first_seen, last_seen, msg_count) "
            "VALUES(?,?,?,?,?,1)",
            (uid, nick, json.dumps([], ensure_ascii=False), ts, ts))
    else:
        aka = []
        try:
            aka = [a for a in json.loads(row["aka"] or "[]") if a]
        except (json.JSONDecodeError, TypeError):
            pass
        old = row["nick"] or ""
        if nick and old and nick != old and old not in aka:
            aka.append(old)
            aka = aka[-5:]
        db.execute(
            "UPDATE person SET nick=?, aka=?, last_seen=?, msg_count=msg_count+1 "
            "WHERE uid=?",
            (nick or old, json.dumps(aka, ensure_ascii=False), ts, uid))
    db.commit()


def nick_index() -> dict[str, int]:
    """昵称和所有别名 -> uid。用于把文本里的人名反查成 QQ 号。"""
    out: dict[str, int] = {}
    for row in connect().execute("SELECT uid, nick, aka FROM person").fetchall():
        names = [row["nick"] or ""]
        try:
            names += [a for a in json.loads(row["aka"] or "[]") if a]
        except (json.JSONDecodeError, TypeError):
            pass
        for n in names:
            n = n.strip()
            # 太短的名字（"猫""小"）做反向替换会误伤
            if len(n) >= 2 and n not in out:
                out[n] = row["uid"]
    return out


def person_info(uids: list[int]) -> dict[int, dict]:
    if not uids:
        return {}
    marks = ",".join("?" * len(uids))
    rows = connect().execute(
        f"SELECT uid, nick, aka, msg_count FROM person WHERE uid IN ({marks})",
        list(uids)).fetchall()
    return {r["uid"]: dict(r) for r in rows}


def nick_of(uid: int) -> str:
    row = connect().execute("SELECT nick FROM person WHERE uid=?", (uid,)).fetchone()
    return (row["nick"] if row else "") or str(uid)


# ---------- 记忆条目 ----------

PLACEHOLDER = re.compile(r"@\{(\d+)\}")


def link_names(text: str, index: dict[str, int] | None = None) -> str:
    """把文本里的人名换成 @{uid}。长名字优先，避免「朴」先吃掉「朴俞喆」。"""
    index = nick_index() if index is None else index
    for name in sorted(index, key=len, reverse=True):
        if name in text:
            text = text.replace(name, f"@{{{index[name]}}}")
    return text


def render_names(text: str) -> str:
    """@{uid} -> 当前昵称(uid)。名字变了这里自动跟着变。"""
    def repl(m: re.Match) -> str:
        uid = int(m.group(1))
        return f"{nick_of(uid)}({uid})"
    return PLACEHOLDER.sub(repl, text)


def add_memory(gid: int, uid: int, kind: str, content: str,
               weight: float = 0.5, ts: int | None = None) -> None:
    content = (content or "").strip()[:200]
    if not content:
        return
    ts = ts or int(time.time())
    db = connect()
    # 同一件事别记两遍：同群同人同类型，内容重叠过半就覆盖旧的
    for row in db.execute(
            "SELECT id, content FROM memory WHERE gid=? AND uid=? AND kind=?",
            (gid, uid, kind)).fetchall():
        if _similar(row["content"], content) > 0.6:
            db.execute("UPDATE memory SET content=?, ts=?, weight=?, used_at=0 "
                       "WHERE id=?", (content, ts, weight, row["id"]))
            db.commit()
            return
    db.execute(
        "INSERT INTO memory(gid, uid, kind, content, ts, weight) VALUES(?,?,?,?,?,?)",
        (gid, uid, kind, content, ts, weight))
    db.commit()
    prune(gid)


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    return len(sa & sb) / max(1, len(sa | sb))


# 梗记得太多会挤掉人事：2026-10-29 库里 200 条有 112 条是 meme，person 只剩 56 条。
# 排序和淘汰都乘上类型系数，meme 先被挤出去。
KIND_FACTOR = {"meme": 0.45, "person": 1.15, "event": 1.0}
_KIND_SQL = ("weight * CASE kind WHEN 'meme' THEN 0.45 "
             "WHEN 'person' THEN 1.15 ELSE 1.0 END")


def prune(gid: int, per_person: int = 20, per_group: int = 200) -> None:
    """按「权重 × 类型系数 × 时间衰减」淘汰，防止无限膨胀。"""
    db = connect()

    def drop(rows):
        ids = [r["id"] for r in rows]
        if ids:
            marks = ",".join("?" * len(ids))
            db.execute(f"DELETE FROM memory WHERE id IN ({marks})", ids)

    # 每个人在天花板之外的部分
    # 注意是 DESC：按分数从高到低排，跳过前 per_person 条，剩下的才删。
    # 原来写的是 ASC + OFFSET，删掉的是**分数最高**的那条——正好反了，
    # 越重要的记忆死得越快（2026-10-29 发现，库里 200 条大半是梗）。
    for row in db.execute(
            "SELECT uid FROM memory WHERE gid=? AND uid>0 GROUP BY uid", (gid,)).fetchall():
        extra = db.execute(
            "SELECT id FROM memory WHERE gid=? AND uid=? ORDER BY "
            f"({_KIND_SQL} * (1.0 / (1.0 + (? - ts) / 86400.0))) DESC "
            "LIMIT -1 OFFSET ?",
            (gid, row["uid"], int(time.time()), per_person)).fetchall()
        drop(extra)

    # 群整体的天花板（同上，DESC 才是留强删弱）
    extra = db.execute(
        "SELECT id FROM memory WHERE gid=? ORDER BY "
        f"({_KIND_SQL} * (1.0 / (1.0 + (? - ts) / 86400.0))) DESC "
        "LIMIT -1 OFFSET ?",
        (gid, int(time.time()), per_group)).fetchall()
    drop(extra)
    db.commit()


def _bigrams(text: str, cap: int = 80) -> set[str]:
    """中文按字二元组切，够用且不用分词。"""
    t = "".join((text or "").split())[:cap]
    if len(t) < 2:
        return set(t)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _overlap(hint: str, content: str) -> float:
    """这句话跟这条记忆的字面重合度，0~1。"""
    a, b = _bigrams(hint), _bigrams(content)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a)


def memories_for(gid: int, uids: list[int] | None = None,
                 limit: int = 6, hint: str = "") -> list[dict]:
    """取这个群和指定几个人的记忆。

    排序分三步：基础分（权重 × 类型系数）先粗排，再按这句话的**字面相关度**
    加权，最后「很久没被想起来」的往前顶。原来只按权重和 used_at 排，
    问她一个具体的人或事，取到的经常是几条无关的群梗。
    """
    db = connect()
    uids = [u for u in (uids or []) if u]
    clauses, args = ["gid=?"], [gid]
    if uids:
        marks = ",".join("?" * len(uids))
        clauses.append(f"(uid=0 OR uid IN ({marks}))")
        args += uids
    else:
        clauses.append("uid=0")
    pool = max(limit * 8, 40)
    rows = db.execute(
        "SELECT id, uid, kind, content, ts, "
        f"({_KIND_SQL}) AS base FROM memory WHERE "
        + " AND ".join(clauses)
        + " ORDER BY base DESC, used_at ASC, ts DESC LIMIT ?",
        args + [pool]).fetchall()

    if hint:
        scored = []
        for r in rows:
            # 相关度最多加 0.6，压不过一条明显更重的记忆，但足够把跑题的挤下去
            scored.append((float(r["base"]) + 0.6 * _overlap(hint, r["content"]), r))
        scored.sort(key=lambda x: (-x[0], -x[1]["ts"]))
        rows = [r for _, r in scored][:limit]
    else:
        rows = rows[:limit]

    if rows:
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        db.execute(f"UPDATE memory SET used_at=? WHERE id IN ({marks})",
                   [int(time.time())] + ids)
        db.commit()
    return [{"id": r["id"], "uid": r["uid"], "kind": r["kind"],
             "content": r["content"], "ts": r["ts"]} for r in rows]


# ---------- 视频转写缓存 ----------

def get_video(vid: str, max_age: int = 604800) -> dict | None:
    """取缓存的转写。超过 max_age 秒就当没有（内容会变、也会占地方）。"""
    if not vid:
        return None
    db = connect()
    row = db.execute(
        "SELECT vid, title, up, seconds, transcript, ts FROM video WHERE vid=?",
        (vid,)).fetchone()
    if not row:
        return None
    if max_age and time.time() - int(row["ts"] or 0) > max_age:
        return None
    return dict(row)


def save_video(vid: str, title: str, up: str, seconds: int, transcript: str) -> None:
    if not vid:
        return
    db = connect()
    db.execute(
        "INSERT INTO video(vid, title, up, seconds, transcript, ts) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(vid) DO UPDATE SET title=excluded.title, up=excluded.up, "
        "seconds=excluded.seconds, transcript=excluded.transcript, ts=excluded.ts",
        (vid, (title or "")[:200], (up or "")[:80], int(seconds or 0),
         transcript or "", int(time.time())))
    db.commit()


def prune_video(keep_days: int = 7) -> None:
    if not keep_days:
        return
    db = connect()
    db.execute("DELETE FROM video WHERE ts < ?",
               (int(time.time()) - keep_days * 86400,))
    db.commit()
