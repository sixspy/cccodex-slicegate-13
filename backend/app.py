
import hashlib, secrets, sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

# 两名豁免值班同时落笔时，写锁等待上限（毫秒）；超时即判争用，禁止静默排队写成成功
WRITE_BUSY_MS = 5000
CALIBERS = ("甲", "乙")

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def now_str() -> str:
    return datetime.now().isoformat(timespec="seconds")

def today_str() -> str:
    return datetime.now().date().isoformat()

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "日切放行"
SKELETON = "业务日分区切片登记后裁决完整度，完整才放行下游，并与消费水位对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# ---------- 完整度豁免闭环：表结构 ----------
# exemption_policy  豁免策略册：业务日 -> 甲/乙口径，首落笔锁定，当天不许再换
# exemptions        豁免列表（审批单本体），待审批同切片唯一，审批不写放行态
# exemption_events  策略旁注/审计流水，豁免列表、策略册、审批详情、放行闸四方可核
# backfill_orders   乙路硬拒绝后的补齐单
EXEMPTION_DDL = """
CREATE TABLE IF NOT EXISTS exemption_policy(
  biz_date TEXT PRIMARY KEY,
  caliber TEXT NOT NULL,
  locked_by TEXT NOT NULL,
  locked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exemptions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slice_id INTEGER NOT NULL,
  slice_no TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  caliber TEXT NOT NULL,
  reason TEXT NOT NULL,
  applicant TEXT NOT NULL,
  status TEXT NOT NULL,
  decided_by TEXT,
  decided_at TEXT,
  decision_note TEXT,
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_exemption_open
  ON exemptions(slice_id) WHERE status='待审批';
CREATE TABLE IF NOT EXISTS exemption_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  kind TEXT NOT NULL,
  target TEXT NOT NULL,
  detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backfill_orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slice_id INTEGER NOT NULL,
  slice_no TEXT NOT NULL,
  exemption_id INTEGER,
  consumer TEXT NOT NULL,
  status TEXT NOT NULL,
  opened_by TEXT NOT NULL,
  opened_at TEXT NOT NULL,
  closed_at TEXT
);
"""

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS slices(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_no TEXT UNIQUE NOT NULL,
          biz_date TEXT NOT NULL,
          partition_key TEXT NOT NULL,
          row_count INTEGER NOT NULL,
          expected_count INTEGER NOT NULL,
          completeness TEXT NOT NULL,
          release_status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS releases(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_id INTEGER NOT NULL,
          consumer TEXT NOT NULL,
          status TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS watermarks(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          consumer TEXT NOT NULL,
          biz_date TEXT NOT NULL,
          watermark TEXT NOT NULL,
          source_slice TEXT NOT NULL
        );
        """)
        conn.executescript(EXEMPTION_DDL)
        if conn.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"] == 0:
            rows = [
                ("SL-2026-11-01-east", "2026-11-01", "east", 980, 1000, "部分完整", "未放行"),
                ("SL-2026-11-01-west", "2026-11-01", "west", 1000, 1000, "完整", "已放行"),
                ("SL-2026-11-02-east", "2026-11-02", "east", 0, 1000, "待检", "未放行"),
            ]
            for r in rows:
                conn.execute("INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,completeness,release_status) VALUES (?,?,?,?,?,?,?)", r)
            conn.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (2,'ads_order','成功','完整切片放行','2026-11-01T18:00:00')")
            conn.execute("INSERT INTO watermarks(consumer,biz_date,watermark,source_slice) VALUES ('ads_order','2026-11-01','2026-11-01T18:00:00','SL-2026-11-01-west')")

@app.on_event("startup")
def _start():
    init_db()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "slices": n, "db": str(DB_PATH)}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username, role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row:
        raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok] = row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

# ---------- 互斥串行与审计 ----------

class ExemptionContention(Exception):
    """两名豁免值班同时落笔，后手写锁失败：必须显式判争用，禁止悄悄写成成功。"""

class AuditedReject(Exception):
    """业务拒绝但审计流水必须落库的情形：提交事务后再转 4xx。"""
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail

@contextmanager
def write_tx():
    # IMMEDIATE 立即取写锁：同一时刻只允许一笔豁免落笔，天然串行互斥
    conn = connect()
    conn.execute(f"PRAGMA busy_timeout={WRITE_BUSY_MS}")
    began = False
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
            began = True
        except sqlite3.OperationalError:
            raise ExemptionContention("另一值班正持有豁免写锁")
        yield conn
        conn.commit()
    except AuditedReject:
        conn.commit()  # 拒绝本身连同审计旁注一起落库
        raise
    except Exception:
        if began:
            conn.rollback()
        raise
    finally:
        conn.close()

def record_contention(actor: str, target: str, detail: str):
    # 争用发生在锁外，单独起连接把“豁免争用”写进可核流水
    try:
        with connect() as c:
            c.execute(f"PRAGMA busy_timeout={WRITE_BUSY_MS}")
            c.execute(
                "INSERT INTO exemption_events(ts,actor,kind,target,detail) VALUES (?,?,?,?,?)",
                (now_str(), actor, "豁免争用", target, detail),
            )
    except Exception:
        pass

def audit(conn, actor: str, kind: str, target: str, detail: str):
    conn.execute(
        "INSERT INTO exemption_events(ts,actor,kind,target,detail) VALUES (?,?,?,?,?)",
        (now_str(), actor, kind, target, detail),
    )

def gate_verdict(conn, slice_row: sqlite3.Row):
    """放行闸唯一裁决口径，豁免列表/审批详情/闸读数共用，杜绝三处读数打架。

    返回 (verdict, reason, exemption_row)：
      可放行   —— 本趟完整；或甲路豁免通过且与策略册口径一致（仍须走放行入口）
      降级只读 —— 乙路豁免通过且口径一致：只准只读预览，正式放行与水位硬拒绝
      硬拒绝   —— 未完整且无通过豁免 / 策略册缺失 / 豁免口径偏了
    """
    if slice_row["completeness"] == "完整":
        return "可放行", "完整切片，按正常完整度放行", None
    ex = conn.execute(
        "SELECT * FROM exemptions WHERE slice_id=? AND status='通过' ORDER BY id DESC LIMIT 1",
        (slice_row["id"],),
    ).fetchone()
    if ex is None:
        # 未审批/被拒豁免不得当成完整
        return "硬拒绝", "未完整且无审批通过的豁免，禁止当成完整放行", None
    pol = conn.execute(
        "SELECT * FROM exemption_policy WHERE biz_date=?", (ex["biz_date"],)
    ).fetchone()
    if pol is None:
        return "硬拒绝", f"豁免策略册缺失（业务日 {ex['biz_date']}），豁免无法核对，按硬拒绝处理", ex
    if pol["caliber"] != ex["caliber"]:
        return (
            "硬拒绝",
            f"豁免口径偏了：策略册为{pol['caliber']}路，豁免单为{ex['caliber']}路，两边不得混路",
            ex,
        )
    if ex["caliber"] == "甲":
        return "可放行", "甲路豁免通过：本趟视为完整可放行，但仍须经放行入口裁决，审批路径不改放行态", ex
    return (
        "降级只读",
        "乙路豁免通过：仅允许降级只读放行预览；正式成功放行连同水位仍硬拒绝，须另开补齐单",
        ex,
    )

# ---------- 既有切片/裁决接口（登录、文件库沿用，不动边界） ----------

@app.get("/api/slices")
def list_slices(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM slices ORDER BY id").fetchall()
    return {"items": [dict(r) for r in rows]}

class SliceIn(BaseModel):
    slice_no: str
    biz_date: str
    partition_key: str
    row_count: int
    expected_count: int

@app.post("/api/slices")
def add_slice(body: SliceIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    comp = "完整" if body.row_count >= body.expected_count and body.expected_count > 0 else ("部分完整" if body.row_count > 0 else "待检")
    with connect() as c:
        try:
            cur = c.execute(
                "INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,completeness,release_status) VALUES (?,?,?,?,?,?,?)",
                (body.slice_no, body.biz_date, body.partition_key, body.row_count, body.expected_count, comp, "未放行"),
            )
            return {"id": cur.lastrowid, "completeness": comp}
        except sqlite3.IntegrityError:
            raise HTTPException(409, "切片号已存在")

@app.post("/api/slices/{sid}/evaluate")
def evaluate(sid: int, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "切片不存在")
        comp = "完整" if row["row_count"] >= row["expected_count"] and row["expected_count"] > 0 else ("部分完整" if row["row_count"] > 0 else "待检")
        c.execute("UPDATE slices SET completeness=? WHERE id=?", (comp, sid))
    return {"id": sid, "completeness": comp}

class ReleaseIn(BaseModel):
    consumer: str

@app.post("/api/slices/{sid}/release")
def release(sid: int, body: ReleaseIn, authorization: str | None = Header(default=None)):
    """放行入口：唯一允许把切片写成已放行并写水位的地方。

    甲路豁免通过后也只能在此入口裁决放行；审批接口改放行态属违规，代码上不存在该路径。
    """
    user = require_user(authorization)
    try:
        with write_tx() as c:
            row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
            if not row:
                raise HTTPException(404, "切片不存在")
            verdict, reason, ex = gate_verdict(c, row)
            target = f"slice:{row['slice_no']}"
            if verdict == "硬拒绝":
                kind = "豁免口径偏了" if (ex is not None and "口径偏了" in reason) else "放行拒绝"
                audit(c, user, kind, target, f"放行闸硬拒绝：{reason}")
                c.execute(
                    "INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (?,?,?,?,?)",
                    (sid, body.consumer, "拒绝", f"硬拒绝：{reason}", now_str()),
                )
                raise AuditedReject(409, reason)
            if verdict == "降级只读":
                # 乙路：正式成功放行连同水位仍硬拒绝
                audit(c, user, "放行拒绝", target, f"乙路豁免正式放行硬拒绝、不写水位：{reason}")
                c.execute(
                    "INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (?,?,?,?,?)",
                    (sid, body.consumer, "拒绝", "乙路豁免仅允许降级只读预览，硬拒绝，须另开补齐单", now_str()),
                )
                raise AuditedReject(409, "乙路豁免仅允许降级只读放行预览，正式成功放行连同水位仍硬拒绝，请另开补齐单")
            note = "完整切片放行"
            if ex is not None:
                note = f"甲路豁免通过后经放行入口放行（豁免单#{ex['id']}，口径甲）"
            c.execute("UPDATE slices SET release_status='已放行' WHERE id=?", (sid,))
            c.execute(
                "INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (?,?,?,?,?)",
                (sid, body.consumer, "成功", note, now_str()),
            )
            c.execute(
                "INSERT INTO watermarks(consumer,biz_date,watermark,source_slice) VALUES (?,?,?,?)",
                (body.consumer, row["biz_date"], now_str(), row["slice_no"]),
            )
            audit(c, user, "放行成功", target, note)
            return {"ok": True, "verdict": verdict, "reason": reason,
                    "exemption_id": ex["id"] if ex else None}
    except AuditedReject as e:
        raise HTTPException(e.status_code, e.detail)
    except ExemptionContention:
        record_contention(user, f"slice:{sid}", "放行落笔与另一值班争用，本笔未写入放行态与水位")
        raise HTTPException(409, "豁免争用：另一值班正在落笔，本笔禁止写入，请稍后重试")

@app.get("/api/releases")
def list_releases(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("""SELECT r.*, s.slice_no FROM releases r JOIN slices s ON s.id=r.slice_id ORDER BY r.id DESC""").fetchall()
    return {"items": [dict(r) for r in rows]}

@app.get("/api/watermarks")
def list_wm(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM watermarks ORDER BY id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}

# ---------- 完整度豁免：策略册 ----------

class PolicyIn(BaseModel):
    caliber: Literal["甲", "乙"]
    biz_date: str | None = None

@app.get("/api/exemption-policy")
def get_policy(biz_date: str | None = None, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        if biz_date:
            rows = c.execute("SELECT * FROM exemption_policy WHERE biz_date=? ORDER BY biz_date", (biz_date,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM exemption_policy ORDER BY biz_date").fetchall()
    return {"items": [dict(r) for r in rows]}

@app.post("/api/exemption-policy")
def lock_policy(body: PolicyIn, authorization: str | None = Header(default=None)):
    """选定口径写入豁免策略册，当天不许再换；混路改选直接拒绝并记口径偏了。"""
    user = require_user(authorization)
    biz_date = body.biz_date or today_str()
    target = f"policy:{biz_date}"
    try:
        with write_tx() as c:
            existing = c.execute("SELECT * FROM exemption_policy WHERE biz_date=?", (biz_date,)).fetchone()
            if existing:
                if existing["caliber"] != body.caliber:
                    audit(c, user, "豁免口径偏了", target,
                          f"策略册已锁{existing['caliber']}路，请求改选{body.caliber}路被拒，当天不许再换")
                    raise AuditedReject(409, f"业务日 {biz_date} 豁免口径已锁定为{existing['caliber']}路，当天不许再换，禁止混路")
                audit(c, user, "策略锁定", target, f"重复落笔，口径与在册一致（{body.caliber}路），未变更")
                return {"biz_date": biz_date, "caliber": existing["caliber"], "locked": True, "changed": False}
            c.execute(
                "INSERT INTO exemption_policy(biz_date,caliber,locked_by,locked_at) VALUES (?,?,?,?)",
                (biz_date, body.caliber, user, now_str()),
            )
            audit(c, user, "策略锁定", target, f"豁免口径选定并锁定为{body.caliber}路，当天不许再换")
            return {"biz_date": biz_date, "caliber": body.caliber, "locked": True, "changed": True}
    except AuditedReject as e:
        raise HTTPException(e.status_code, e.detail)
    except ExemptionContention:
        record_contention(user, target, f"策略册落笔争用，{body.caliber}路锁定未写入")
        raise HTTPException(409, "豁免争用：另一值班正在落笔，策略册本笔禁止写入")

# ---------- 完整度豁免：申请/审批/查询 ----------

class ExemptionIn(BaseModel):
    slice_id: int
    caliber: Literal["甲", "乙"]
    reason: str

@app.post("/api/exemptions")
def apply_exemption(body: ExemptionIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    if not body.reason or not body.reason.strip():
        raise HTTPException(400, "豁免事由必填")
    try:
        with write_tx() as c:
            row = c.execute("SELECT * FROM slices WHERE id=?", (body.slice_id,)).fetchone()
            if not row:
                raise HTTPException(404, "切片不存在")
            target = f"slice:{row['slice_no']}"
            if row["completeness"] == "完整":
                raise AuditedReject(409, "切片本就完整，无需豁免审批")
            open_ex = c.execute(
                "SELECT id FROM exemptions WHERE slice_id=? AND status='待审批'", (body.slice_id,)
            ).fetchone()
            if open_ex:
                audit(c, user, "豁免争用", target, f"该切片已有待审批豁免单#{open_ex['id']}，重复申请被拒")
                raise AuditedReject(409, "该切片已有待审批豁免，禁止重复落笔")
            pol = c.execute(
                "SELECT * FROM exemption_policy WHERE biz_date=?", (row["biz_date"],)
            ).fetchone()
            if pol:
                if pol["caliber"] != body.caliber:
                    audit(c, user, "豁免口径偏了", target,
                          f"策略册为{pol['caliber']}路，申请{body.caliber}路被拒，两路不得混路")
                    raise AuditedReject(
                        409,
                        f"业务日 {row['biz_date']} 豁免口径已锁定为{pol['caliber']}路，禁止混路申请{body.caliber}路",
                    )
            else:
                # 首张豁免单落笔即替当天选定口径并锁册
                c.execute(
                    "INSERT INTO exemption_policy(biz_date,caliber,locked_by,locked_at) VALUES (?,?,?,?)",
                    (row["biz_date"], body.caliber, user, now_str()),
                )
                audit(c, user, "策略锁定", f"policy:{row['biz_date']}",
                      f"首张豁免单落笔，口径选定并锁定为{body.caliber}路，当天不许再换")
            cur = c.execute(
                """INSERT INTO exemptions(slice_id,slice_no,biz_date,caliber,reason,applicant,status,created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (body.slice_id, row["slice_no"], row["biz_date"], body.caliber,
                 body.reason.strip(), user, "待审批", now_str()),
            )
            audit(c, user, "豁免申请", target,
                  f"豁免单#{cur.lastrowid} 申请（{body.caliber}路），状态待审批：{body.reason.strip()}")
            return {"id": cur.lastrowid, "slice_id": body.slice_id, "caliber": body.caliber,
                    "biz_date": row["biz_date"], "status": "待审批"}
    except AuditedReject as e:
        raise HTTPException(e.status_code, e.detail)
    except ExemptionContention:
        record_contention(user, f"slice:{body.slice_id}", "豁免申请落笔争用，本笔未写入豁免列表")
        raise HTTPException(409, "豁免争用：另一值班正在落笔，豁免申请本笔禁止写入")

class DecisionIn(BaseModel):
    action: Literal["通过", "拒绝"]
    note: str = ""

@app.post("/api/exemptions/{eid}/decision")
def decide_exemption(eid: int, body: DecisionIn, authorization: str | None = Header(default=None)):
    """审批裁决：只改豁免单状态。铁律——审批通过不得顺带写成已放行态。"""
    user = require_user(authorization)
    try:
        with write_tx() as c:
            ex = c.execute("SELECT * FROM exemptions WHERE id=?", (eid,)).fetchone()
            if not ex:
                raise HTTPException(404, "豁免单不存在")
            target = f"slice:{ex['slice_no']}"
            if ex["status"] != "待审批":
                audit(c, user, "豁免争用", target,
                      f"豁免单#{eid} 已被 {ex['decided_by']} 裁决为{ex['status']}，重复落笔被拒")
                raise AuditedReject(409, "该豁免已裁决，禁止重复落笔")
            before = c.execute(
                "SELECT release_status FROM slices WHERE id=?", (ex["slice_id"],)
            ).fetchone()["release_status"]
            c.execute(
                "UPDATE exemptions SET status=?,decided_by=?,decided_at=?,decision_note=? WHERE id=?",
                (body.action, user, now_str(), body.note, eid),
            )
            # 显式不执行任何 slices.release_status 更新；把“放行态保持不变”写进旁注供核验
            kind = "审批通过" if body.action == "通过" else "审批拒绝"
            tail = f"；{body.note}" if body.note else ""
            audit(c, user, kind, target,
                  f"豁免单#{eid} 裁决{body.action}（{ex['caliber']}路），切片放行态保持「{before}」，"
                  f"放行只能走放行入口{tail}")
            return {"id": eid, "status": body.action, "caliber": ex["caliber"],
                    "release_status_unchanged": before}
    except AuditedReject as e:
        raise HTTPException(e.status_code, e.detail)
    except ExemptionContention:
        record_contention(user, f"exemption:{eid}", "审批裁决落笔争用，本笔未写入审批结果")
        raise HTTPException(409, "豁免争用：另一值班正在落笔，审批本笔禁止写入")

@app.get("/api/exemptions")
def list_exemptions(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("""
            SELECT e.*, s.completeness, s.release_status
            FROM exemptions e JOIN slices s ON s.id=e.slice_id
            ORDER BY e.id DESC
        """).fetchall()
    return {"items": [dict(r) for r in rows]}

@app.get("/api/exemptions/{eid}")
def exemption_detail(eid: int, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        ex = c.execute("""
            SELECT e.*, s.completeness, s.release_status
            FROM exemptions e JOIN slices s ON s.id=e.slice_id
            WHERE e.id=?""", (eid,)).fetchone()
        if not ex:
            raise HTTPException(404, "豁免单不存在")
        pol = c.execute(
            "SELECT * FROM exemption_policy WHERE biz_date=?", (ex["biz_date"],)
        ).fetchone()
        events = c.execute(
            "SELECT * FROM exemption_events WHERE target=? OR target=? ORDER BY id",
            (f"slice:{ex['slice_no']}", f"exemption:{eid}"),
        ).fetchall()
    return {"exemption": dict(ex),
            "policy": dict(pol) if pol else None,
            "events": [dict(r) for r in events]}

@app.get("/api/exemption-events")
def list_events(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM exemption_events ORDER BY id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}

@app.get("/api/gate-readings")
def gate_readings(authorization: str | None = Header(default=None)):
    """放行闸统一读数：豁免列表、策略旁注、审批详情、放行闸一处对齐，全部可核。"""
    require_user(authorization)
    out = []
    with connect() as c:
        for s in c.execute("SELECT * FROM slices ORDER BY id").fetchall():
            verdict, reason, ex = gate_verdict(c, s)
            pol = c.execute(
                "SELECT * FROM exemption_policy WHERE biz_date=?", (s["biz_date"],)
            ).fetchone()
            out.append({
                "slice_id": s["id"],
                "slice_no": s["slice_no"],
                "biz_date": s["biz_date"],
                "completeness": s["completeness"],
                "release_status": s["release_status"],
                "verdict": verdict,
                "reason": reason,
                "approved_exemption_id": ex["id"] if ex else None,
                "exemption_caliber": ex["caliber"] if ex else None,
                "policy_caliber": pol["caliber"] if pol else None,
                "consistent": ex is None or (pol is not None and pol["caliber"] == ex["caliber"]),
            })
    return {"items": out}

# ---------- 乙路：降级只读预览 + 补齐单 ----------

class PreviewIn(BaseModel):
    consumer: str

@app.post("/api/slices/{sid}/exemption-preview")
def exemption_preview(sid: int, body: PreviewIn, authorization: str | None = Header(default=None)):
    """乙路专属：降级只读放行预览。不写放行态、不写水位、不产生成功流水。"""
    user = require_user(authorization)
    try:
        with write_tx() as c:
            row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
            if not row:
                raise HTTPException(404, "切片不存在")
            verdict, reason, ex = gate_verdict(c, row)
            target = f"slice:{row['slice_no']}"
            if verdict != "降级只读":
                audit(c, user, "预览拒绝", target, f"降级只读预览被拒（当前裁决：{verdict}）：{reason}")
                raise AuditedReject(409, f"当前放行裁决为「{verdict}」，不允许进入降级只读预览：{reason}")
            audit(c, user, "降级预览", target,
                  f"乙路豁免单#{ex['id']} 只读预览（consumer={body.consumer}），不写放行态、不写水位")
            return {
                "ok": True,
                "read_only": True,
                "slice_no": row["slice_no"],
                "exemption_id": ex["id"],
                "release_status_unchanged": row["release_status"],
                "watermark_written": False,
                "reason": reason,
            }
    except AuditedReject as e:
        raise HTTPException(e.status_code, e.detail)
    except ExemptionContention:
        record_contention(user, f"slice:{sid}", "降级预览落笔争用，本笔未写入")
        raise HTTPException(409, "豁免争用：另一值班正在落笔，预览本笔禁止写入")

class BackfillIn(BaseModel):
    consumer: str

@app.post("/api/slices/{sid}/backfill-order")
def open_backfill(sid: int, body: BackfillIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    try:
        with write_tx() as c:
            row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
            if not row:
                raise HTTPException(404, "切片不存在")
            target = f"slice:{row['slice_no']}"
            if row["completeness"] == "完整":
                raise AuditedReject(409, "切片已完整，无需开补齐单")
            ex = c.execute(
                "SELECT * FROM exemptions WHERE slice_id=? AND status='通过' ORDER BY id DESC LIMIT 1",
                (sid,),
            ).fetchone()
            existing = c.execute(
                "SELECT id FROM backfill_orders WHERE slice_id=? AND status='待补齐'", (sid,)
            ).fetchone()
            if existing:
                audit(c, user, "补齐单", target, f"补齐单#{existing['id']} 仍在途，重复开单被幂等拦下")
                return {"id": existing["id"], "status": "待补齐", "changed": False}
            cur = c.execute(
                """INSERT INTO backfill_orders(slice_id,slice_no,exemption_id,consumer,status,opened_by,opened_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (sid, row["slice_no"], ex["id"] if ex else None, body.consumer,
                 "待补齐", user, now_str()),
            )
            audit(c, user, "补齐单", target,
                  f"乙路硬拒绝后另开补齐单#{cur.lastrowid}（consumer={body.consumer}），正式放行待补齐后重走放行入口")
            return {"id": cur.lastrowid, "status": "待补齐", "changed": True}
    except AuditedReject as e:
        raise HTTPException(e.status_code, e.detail)
    except ExemptionContention:
        record_contention(user, f"slice:{sid}", "补齐单落笔争用，本笔未写入")
        raise HTTPException(409, "豁免争用：另一值班正在落笔，补齐单本笔禁止写入")

@app.post("/api/backfill-orders/{oid}/close")
def close_backfill(oid: int, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    try:
        with write_tx() as c:
            order = c.execute("SELECT * FROM backfill_orders WHERE id=?", (oid,)).fetchone()
            if not order:
                raise HTTPException(404, "补齐单不存在")
            if order["status"] == "已补齐":
                raise AuditedReject(409, "补齐单已关闭，禁止重复落笔")
            c.execute("UPDATE backfill_orders SET status='已补齐', closed_at=? WHERE id=?",
                      (now_str(), oid))
            audit(c, user, "补齐单", f"slice:{order['slice_no']}",
                  f"补齐单#{oid} 已补齐，切片须重走完整度裁决与放行入口，豁免不自动转放行")
            return {"id": oid, "status": "已补齐"}
    except AuditedReject as e:
        raise HTTPException(e.status_code, e.detail)
    except ExemptionContention:
        record_contention(user, f"backfill:{oid}", "补齐单关单争用，本笔未写入")
        raise HTTPException(409, "豁免争用：另一值班正在落笔，关单本笔禁止写入")

@app.get("/api/backfill-orders")
def list_backfill(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM backfill_orders ORDER BY id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")

app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
