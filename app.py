"""博物馆藏品来源与返还审查系统。"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "provenance.db"
CLAIM_TRANSITIONS = {
    "submitted": {"under_review"},
    "under_review": {"negotiating", "resolved_return", "rejected"},
    # awaiting_materials 由补件要求自动进入，不在此表中手工流转
    "awaiting_materials": {"under_review", "negotiating", "resolved_return", "rejected"},
    "negotiating": {"resolved_return", "rejected"},
    "resolved_return": set(),
    "rejected": set(),
}
CLAIM_STATUS_LABELS = {
    "submitted": "已提交",
    "under_review": "审查中",
    "awaiting_materials": "补件中",
    "negotiating": "协商中",
    "resolved_return": "已完成返还",
    "rejected": "已驳回",
}


class BusinessError(Exception):
    def __init__(self, message, status=400, code="bad_request", details=None):
        super().__init__(message)
        self.message, self.status, self.code, self.details = message, status, code, details


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProvenanceStore:
    def __init__(self, db_path=DEFAULT_DB):
        self.db_path = str(db_path)
        self._lock = threading.Lock()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def init_schema(self):
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('staff','reviewer','claimant','public'))
                );
                CREATE TABLE IF NOT EXISTS sources(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    source_type TEXT NOT NULL, reference TEXT NOT NULL,
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    UNIQUE(name,reference)
                );
                CREATE TABLE IF NOT EXISTS objects(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, inventory_no TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL, object_type TEXT NOT NULL, current_holder TEXT NOT NULL,
                    public_summary TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    event_type TEXT NOT NULL, date_start TEXT NOT NULL, date_end TEXT,
                    place TEXT NOT NULL, description TEXT NOT NULL,
                    source_id INTEGER REFERENCES sources(id),
                    visibility TEXT NOT NULL CHECK(visibility IN ('public','internal')),
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    event_id INTEGER REFERENCES events(id), filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL, size INTEGER NOT NULL, content BLOB NOT NULL,
                    visibility TEXT NOT NULL CHECK(visibility IN ('public','internal')),
                    uploaded_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claims(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    claimant_id TEXT NOT NULL REFERENCES users(id),
                    claimed_by TEXT NOT NULL, desired_outcome TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'submitted'
                        CHECK(status IN ('submitted','under_review','awaiting_materials','negotiating','resolved_return','rejected')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS supplement_requests(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    material_category TEXT NOT NULL, description TEXT NOT NULL,
                    requested_by TEXT NOT NULL REFERENCES users(id),
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','submitted','closed')),
                    review_note TEXT, reviewed_by TEXT REFERENCES users(id),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS supplement_materials(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id INTEGER NOT NULL REFERENCES supplement_requests(id),
                    filename TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
                    content BLOB NOT NULL,
                    uploaded_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claim_reviews(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    old_status TEXT NOT NULL, new_status TEXT NOT NULL,
                    note TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS object_versions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    version INTEGER NOT NULL, snapshot TEXT NOT NULL,
                    changed_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    UNIQUE(object_id,version)
                );
                CREATE TABLE IF NOT EXISTS audit_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, object_id INTEGER REFERENCES objects(id),
                    actor_id TEXT NOT NULL REFERENCES users(id), action TEXT NOT NULL,
                    detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
                """
            )
            # 旧库的 claims 约束缺少 awaiting_materials，需要放宽后重建。
            check_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='claims'"
            ).fetchone()["sql"]
            if "awaiting_materials" not in check_sql:
                conn.executescript(
                    """
                    CREATE TABLE claims_new(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        object_id INTEGER NOT NULL REFERENCES objects(id),
                        claimant_id TEXT NOT NULL REFERENCES users(id),
                        claimed_by TEXT NOT NULL, desired_outcome TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'submitted'
                            CHECK(status IN ('submitted','under_review','awaiting_materials','negotiating','resolved_return','rejected')),
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                    );
                    INSERT INTO claims_new SELECT id,object_id,claimant_id,claimed_by,desired_outcome,status,created_at,updated_at FROM claims;
                    DROP TABLE claims;
                    ALTER TABLE claims_new RENAME TO claims;
                    """
                )

    def seed(self):
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name,role) VALUES(?,?,?)",
                [
                    ("staff", "藏品研究员", "staff"),
                    ("reviewer1", "返还审查员", "reviewer"),
                    ("claimant1", "权利主张人", "claimant"),
                    ("public", "公众访客", "public"),
                ],
            )
        if self.list_objects("staff"):
            return
        obj = self.create_object("staff", "DEMO-1938-3", "青铜簋", "礼器", "市博物馆", "1938 年前后入藏，来源仍在持续核验。")
        self.add_event(
            "staff", obj["id"], "acquisition", "1938-05-01", "", "本市",
            "登记为从私人藏家处购得，原始凭证尚未归档。", None, "public",
        )
        self.create_claim("claimant1", obj["id"], "王氏家族委员会", "请求返还祖传青铜簋")
        claims = self.list_claims_for_seed(obj["id"])
        self.transition_claim("reviewer1", claims[0]["id"], "under_review", "登记受理，启动来源核查。")
        self.create_supplement_request(
            "reviewer1", claims[0]["id"], "亲属关系证明",
            "请提供能够证明主张人与原收藏人关系的户籍或公证材料。",
        )

    def list_claims_for_seed(self, object_id):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT id FROM claims WHERE object_id=? ORDER BY id", (object_id,)).fetchall()]

    def _user(self, conn, user_id, roles=None):
        if not user_id:
            raise BusinessError("缺少 X-User-Id", 401, "authentication_required")
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise BusinessError("用户不存在", 401, "unknown_user")
        if roles and user["role"] not in roles:
            raise BusinessError("当前角色无权执行此操作", 403, "forbidden")
        return user

    def _object(self, conn, object_id):
        row = conn.execute("SELECT * FROM objects WHERE id=?", (object_id,)).fetchone()
        if not row:
            raise BusinessError("藏品不存在", 404, "not_found")
        return row

    def _claim(self, conn, claim_id):
        row = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        if not row:
            raise BusinessError("权利主张不存在", 404, "not_found")
        return row

    def _supplement_request(self, conn, request_id):
        row = conn.execute("SELECT * FROM supplement_requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            raise BusinessError("补件项不存在", 404, "not_found")
        return row

    def _material_meta(self, conn, material_id):
        row = conn.execute(
            "SELECT id,request_id,filename,sha256,size,uploaded_by,created_at FROM supplement_materials WHERE id=?",
            (material_id,),
        ).fetchone()
        if not row:
            raise BusinessError("补件材料不存在", 404, "not_found")
        return row

    def _supplement_payload(self, conn, request_row, include_internals):
        """补件项的分层视图：公众只能看到状态，主张人/审查员可看明细。"""
        if not include_internals:
            return {"id": request_row["id"], "claim_id": request_row["claim_id"], "status": request_row["status"]}
        materials = [
            dict(self._material_meta(conn, r["id"]))
            for r in conn.execute("SELECT id FROM supplement_materials WHERE request_id=? ORDER BY id", (request_row["id"],)).fetchall()
        ]
        return dict(request_row) | {"materials": materials}

    def _claim_supplements(self, conn, claim_id, include_internals):
        rows = conn.execute("SELECT * FROM supplement_requests WHERE claim_id=? ORDER BY id", (claim_id,)).fetchall()
        return [self._supplement_payload(conn, r, include_internals) for r in rows]

    def _open_supplement_categories(self, conn, claim_id):
        return [r["material_category"] for r in conn.execute(
            "SELECT material_category FROM supplement_requests WHERE claim_id=? AND status!='closed' ORDER BY id",
            (claim_id,),
        ).fetchall()]

    def _bump_version_and_snapshot(self, conn, object_id, actor):
        obj = self._object(conn, object_id)
        next_version = obj["version"] + 1
        conn.execute("UPDATE objects SET version=?,updated_at=? WHERE id=?", (next_version, now(), object_id))
        self._snapshot(conn, object_id, actor)
        return next_version

    def _audit(self, conn, object_id, actor, action, detail):
        conn.execute(
            "INSERT INTO audit_log(object_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (object_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), now()),
        )

    def _snapshot(self, conn, object_id, actor):
        row = self._object(conn, object_id)
        snapshot = {
            "object": dict(row),
            "events": [dict(x) for x in conn.execute("SELECT * FROM events WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
            "claims": [dict(x) for x in conn.execute("SELECT * FROM claims WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
            "supplement_requests": [
                dict(r) | {"materials": [
                    dict(m) for m in conn.execute(
                        "SELECT id,request_id,filename,sha256,size,uploaded_by,created_at FROM supplement_materials WHERE request_id=? ORDER BY id",
                        (r["id"],),
                    ).fetchall()
                ]}
                for r in conn.execute(
                    """SELECT sr.* FROM supplement_requests sr JOIN claims c ON sr.claim_id=c.id
                       WHERE c.object_id=? ORDER BY sr.id""",
                    (object_id,),
                ).fetchall()
            ],
        }
        conn.execute(
            "INSERT INTO object_versions(object_id,version,snapshot,changed_by,created_at) VALUES(?,?,?,?,?)",
            (object_id, row["version"], json.dumps(snapshot, ensure_ascii=False, sort_keys=True), actor, now()),
        )

    def create_object(self, user_id, inventory_no, title, object_type, holder, public_summary):
        inventory_no, title = inventory_no.strip(), title.strip()
        if not inventory_no or len(title) < 2:
            raise BusinessError("库存号和标题不能为空", 422, "invalid_object")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff"})
            try:
                cur = conn.execute(
                    """INSERT INTO objects(inventory_no,title,object_type,current_holder,public_summary,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (inventory_no, title, object_type.strip() or "未分类", holder.strip() or "馆藏", public_summary.strip(), user_id, now(), now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("库存号已存在", 409, "inventory_exists")
            object_id = cur.lastrowid
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "object.create", {"inventory_no": inventory_no})
            return {"id": object_id, "inventory_no": inventory_no, "version": 1}

    def update_object(self, user_id, object_id, changes):
        allowed = {"title", "object_type", "current_holder", "public_summary"}
        clean = {k: str(v).strip() for k, v in changes.items() if k in allowed and str(v).strip()}
        if not clean:
            raise BusinessError("没有可更新字段", 422, "empty_update")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff"})
            row = self._object(conn, object_id)
            new_version = row["version"] + 1
            assignments = ",".join(f"{k}=?" for k in clean)
            conn.execute(
                f"UPDATE objects SET {assignments},version=?,updated_at=? WHERE id=?",
                (*clean.values(), new_version, now(), object_id),
            )
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "object.update", {"version": new_version, "changes": clean})
            return {"id": object_id, "version": new_version, "changes": clean}

    def add_source(self, user_id, name, source_type, reference):
        if not name.strip() or not reference.strip():
            raise BusinessError("来源名称和引用不能为空", 422, "invalid_source")
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            try:
                cur = conn.execute(
                    "INSERT INTO sources(name,source_type,reference,created_by,created_at) VALUES(?,?,?,?,?)",
                    (name.strip(), source_type.strip() or "archive", reference.strip(), user_id, now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("来源记录已存在", 409, "source_exists")
            return {"id": cur.lastrowid, "name": name.strip(), "reference": reference.strip()}

    def add_event(self, user_id, object_id, event_type, date_start, date_end, place, description, source_id=None, visibility="internal"):
        if not event_type.strip() or not description.strip() or not place.strip():
            raise BusinessError("事件类型、地点和说明不能为空", 422, "invalid_event")
        try:
            start = date.fromisoformat(date_start)
            end = date.fromisoformat(date_end) if date_end else start
        except ValueError:
            raise BusinessError("事件日期必须是 YYYY-MM-DD", 422, "invalid_date")
        if end < start:
            raise BusinessError("事件结束日期不能早于开始日期", 422, "invalid_date_range")
        if visibility not in {"public", "internal"}:
            raise BusinessError("visibility 必须是 public 或 internal", 422, "invalid_visibility")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff"})
            row = self._object(conn, object_id)
            if source_id and not conn.execute("SELECT 1 FROM sources WHERE id=?", (source_id,)).fetchone():
                raise BusinessError("来源不存在", 404, "source_not_found")
            cur = conn.execute(
                """INSERT INTO events(object_id,event_type,date_start,date_end,place,description,source_id,visibility,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (object_id, event_type.strip(), date_start, date_end or None, place.strip(), description.strip(), source_id, visibility, user_id, now()),
            )
            new_version = row["version"] + 1
            conn.execute("UPDATE objects SET version=?,updated_at=? WHERE id=?", (new_version, now(), object_id))
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "event.add", {"event_id": cur.lastrowid, "version": new_version, "visibility": visibility})
            return {"id": cur.lastrowid, "object_id": object_id, "object_version": new_version}

    def upload_evidence(self, user_id, object_id, filename, content_b64, visibility, event_id=None):
        if not filename.strip():
            raise BusinessError("文件名不能为空", 422, "invalid_filename")
        if visibility not in {"public", "internal"}:
            raise BusinessError("visibility 必须是 public 或 internal", 422, "invalid_visibility")
        try:
            content = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError):
            raise BusinessError("content_b64 不是合法 Base64", 422, "invalid_base64")
        digest = hashlib.sha256(content).hexdigest()
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff", "reviewer"})
            self._object(conn, object_id)
            if event_id and not conn.execute("SELECT 1 FROM events WHERE id=? AND object_id=?", (event_id, object_id)).fetchone():
                raise BusinessError("证据关联的事件不存在", 404, "event_not_found")
            cur = conn.execute(
                """INSERT INTO evidence(object_id,event_id,filename,sha256,size,content,visibility,uploaded_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (object_id, event_id, filename.strip(), digest, len(content), content, visibility, user_id, now()),
            )
            self._audit(conn, object_id, user_id, "evidence.upload", {"evidence_id": cur.lastrowid, "sha256": digest, "visibility": visibility})
            return {"id": cur.lastrowid, "filename": filename.strip(), "sha256": digest, "size": len(content)}

    def create_claim(self, user_id, object_id, claimed_by, desired_outcome):
        if not claimed_by.strip() or not desired_outcome.strip():
            raise BusinessError("主张人和期望结果不能为空", 422, "invalid_claim")
        with self.connect() as conn:
            claimant = self._user(conn, user_id, {"claimant"})
            self._object(conn, object_id)
            cur = conn.execute(
                """INSERT INTO claims(object_id,claimant_id,claimed_by,desired_outcome,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (object_id, user_id, claimed_by.strip(), desired_outcome.strip(), now(), now()),
            )
            self._audit(conn, object_id, user_id, "claim.create", {"claim_id": cur.lastrowid})
            return {"id": cur.lastrowid, "object_id": object_id, "status": "submitted"}

    def transition_claim(self, user_id, claim_id, new_status, note):
        if len(note.strip()) < 5:
            raise BusinessError("阶段审查说明至少 5 字", 422, "review_note_required")
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                reviewer = self._user(conn, user_id, {"reviewer"})
                claim = self._claim(conn, claim_id)
                if new_status not in CLAIM_TRANSITIONS.get(claim["status"], set()):
                    raise BusinessError(f"不能从 {claim['status']} 直接变更为 {new_status}", 409, "invalid_transition")
                # 待补件项未处理完时，协商与完成返还一律拦截；驳回不受影响。
                if new_status in {"negotiating", "resolved_return"}:
                    missing = self._open_supplement_categories(conn, claim_id)
                    if missing:
                        raise BusinessError(
                            "仍有待处理的补件项，缺少：" + "、".join(missing),
                            409,
                            "supplement_pending",
                            {"missing_categories": missing},
                        )
                conn.execute("UPDATE claims SET status=?,updated_at=? WHERE id=?", (new_status, now(), claim_id))
                conn.execute(
                    "INSERT INTO claim_reviews(claim_id,reviewer_id,old_status,new_status,note,created_at) VALUES(?,?,?,?,?,?)",
                    (claim_id, user_id, claim["status"], new_status, note.strip(), now()),
                )
                next_version = self._bump_version_and_snapshot(conn, claim["object_id"], user_id)
                self._audit(conn, claim["object_id"], user_id, "claim.transition", {"claim_id": claim_id, "from": claim["status"], "to": new_status})
                return {"claim_id": claim_id, "old_status": claim["status"], "status": new_status, "object_version": next_version}
            except Exception:
                conn.rollback()
                raise

    def create_supplement_request(self, user_id, claim_id, material_category, description):
        material_category, description = material_category.strip(), description.strip()
        if not material_category:
            raise BusinessError("材料类别不能为空", 422, "invalid_category")
        if len(description) < 5:
            raise BusinessError("补件说明至少 5 字", 422, "supplement_note_required")
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._user(conn, user_id, {"reviewer"})
                claim = self._claim(conn, claim_id)
                if claim["status"] in {"resolved_return", "rejected"}:
                    raise BusinessError("主张已结束，不能再提出补件要求", 409, "claim_closed")
                cur = conn.execute(
                    """INSERT INTO supplement_requests(claim_id,material_category,description,requested_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?)""",
                    (claim_id, material_category, description, user_id, now(), now()),
                )
                if claim["status"] != "awaiting_materials":
                    conn.execute(
                        "UPDATE claims SET status='awaiting_materials',updated_at=? WHERE id=?",
                        (now(), claim_id),
                    )
                    conn.execute(
                        "INSERT INTO claim_reviews(claim_id,reviewer_id,old_status,new_status,note,created_at) VALUES(?,?,?,?,?,?)",
                        (claim_id, user_id, claim["status"], "awaiting_materials",
                         f"提出补件要求：{material_category}", now()),
                    )
                next_version = self._bump_version_and_snapshot(conn, claim["object_id"], user_id)
                self._audit(
                    conn, claim["object_id"], user_id, "supplement.request",
                    {"request_id": cur.lastrowid, "claim_id": claim_id, "category": material_category},
                )
                return {"id": cur.lastrowid, "claim_id": claim_id, "material_category": material_category,
                        "status": "pending", "object_version": next_version}
            except Exception:
                conn.rollback()
                raise

    def upload_supplement_materials(self, user_id, request_id, files):
        """主张人针对一个补件项回应一份或几份材料（可多次补交）。"""
        if not isinstance(files, list) or not files:
            raise BusinessError("请至少上传一份材料", 422, "no_materials")
        parsed = []
        for item in files:
            if not isinstance(item, dict):
                raise BusinessError("材料格式不正确", 422, "invalid_material")
            filename = str(item.get("filename", "")).strip()
            content_b64 = str(item.get("content_b64", ""))
            if not filename:
                raise BusinessError("文件名不能为空", 422, "invalid_filename")
            try:
                content = base64.b64decode(content_b64, validate=True)
            except (binascii.Error, ValueError):
                raise BusinessError(f"{filename} 不是合法 Base64", 422, "invalid_base64")
            if not content:
                raise BusinessError(f"{filename} 内容为空", 422, "empty_material")
            parsed.append((filename, content))
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._user(conn, user_id, {"claimant"})
                request = self._supplement_request(conn, request_id)
                claim = self._claim(conn, request["claim_id"])
                if claim["claimant_id"] != user_id:
                    raise BusinessError("只能回应自己主张下的补件项", 403, "forbidden")
                if request["status"] == "closed":
                    raise BusinessError("该补件项已结束，不能再上传", 409, "request_closed")
                material_ids = []
                for filename, content in parsed:
                    digest = hashlib.sha256(content).hexdigest()
                    cur = conn.execute(
                        """INSERT INTO supplement_materials(request_id,filename,sha256,size,content,uploaded_by,created_at)
                           VALUES(?,?,?,?,?,?,?)""",
                        (request_id, filename, digest, len(content), content, user_id, now()),
                    )
                    material_ids.append(cur.lastrowid)
                conn.execute(
                    "UPDATE supplement_requests SET status='submitted',updated_at=? WHERE id=?",
                    (now(), request_id),
                )
                conn.execute("UPDATE claims SET updated_at=? WHERE id=?", (now(), claim["id"]))
                next_version = self._bump_version_and_snapshot(conn, claim["object_id"], user_id)
                self._audit(
                    conn, claim["object_id"], user_id, "supplement.respond",
                    {"request_id": request_id, "claim_id": claim["id"], "material_ids": material_ids},
                )
                return {"request_id": request_id, "material_ids": material_ids,
                        "status": "submitted", "object_version": next_version}
            except Exception:
                conn.rollback()
                raise

    def close_supplement_request(self, user_id, request_id, review_note):
        review_note = review_note.strip()
        if len(review_note) < 5:
            raise BusinessError("核查意见至少 5 字", 422, "review_note_required")
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._user(conn, user_id, {"reviewer"})
                request = self._supplement_request(conn, request_id)
                if request["status"] == "closed":
                    raise BusinessError("该补件项已经结束", 409, "request_closed")
                count = conn.execute(
                    "SELECT COUNT(*) AS n FROM supplement_materials WHERE request_id=?", (request_id,)
                ).fetchone()["n"]
                if count == 0:
                    raise BusinessError("主张人尚未上传材料，无法核查结束", 422, "no_materials_reviewed")
                conn.execute(
                    "UPDATE supplement_requests SET status='closed',review_note=?,reviewed_by=?,updated_at=? WHERE id=?",
                    (review_note, user_id, now(), request_id),
                )
                claim = self._claim(conn, request["claim_id"])
                conn.execute("UPDATE claims SET updated_at=? WHERE id=?", (now(), claim["id"]))
                next_version = self._bump_version_and_snapshot(conn, claim["object_id"], user_id)
                self._audit(
                    conn, claim["object_id"], user_id, "supplement.close",
                    {"request_id": request_id, "claim_id": claim["id"]},
                )
                return {"request_id": request_id, "status": "closed", "object_version": next_version}
            except Exception:
                conn.rollback()
                raise

    def get_supplement_material(self, user_id, material_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            row = conn.execute("SELECT * FROM supplement_materials WHERE id=?", (material_id,)).fetchone()
            if not row:
                raise BusinessError("补件材料不存在", 404, "not_found")
            request = self._supplement_request(conn, row["request_id"])
            claim = self._claim(conn, request["claim_id"])
            # 公众无权接触补件材料；主张人仅限自己的主张。
            if user["role"] == "public":
                raise BusinessError("当前角色无权查看补件材料", 403, "forbidden")
            if user["role"] == "claimant" and claim["claimant_id"] != user_id:
                raise BusinessError("只能查看自己主张的补件材料", 403, "forbidden")
            return {"id": row["id"], "filename": row["filename"], "sha256": row["sha256"],
                    "size": row["size"], "content_b64": base64.b64encode(row["content"]).decode(),
                    "uploaded_by": row["uploaded_by"], "created_at": row["created_at"]}

    def get_object(self, user_id, object_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            obj = self._object(conn, object_id)
            if user["role"] == "public":
                events = conn.execute(
                    "SELECT id,event_type,date_start,date_end,place,description,visibility,created_at FROM events WHERE object_id=? AND visibility='public' ORDER BY id",
                    (object_id,),
                ).fetchall()
                claims = conn.execute(
                    "SELECT id,claimed_by,desired_outcome,status,created_at FROM claims WHERE object_id=? ORDER BY id", (object_id,)
                ).fetchall()
                # 公众只能知道主张正在补件，看不到材料类别、说明与内部核查意见。
                public_claims = []
                for c in claims:
                    item = dict(c)
                    item["supplement_requests"] = self._claim_supplements(conn, c["id"], include_internals=False)
                    public_claims.append(item)
                return {
                    "id": obj["id"], "inventory_no": obj["inventory_no"], "title": obj["title"],
                    "object_type": obj["object_type"], "public_summary": obj["public_summary"], "version": obj["version"],
                    "events": [dict(e) for e in events], "claims": public_claims,
                }
            result = {
                "id": obj["id"], "inventory_no": obj["inventory_no"], "title": obj["title"],
                "object_type": obj["object_type"], "current_holder": obj["current_holder"],
                "public_summary": obj["public_summary"], "version": obj["version"],
                "events": [dict(x) | {"source": dict(conn.execute("SELECT id,name,source_type,reference FROM sources WHERE id=?", (x["source_id"],)).fetchone()) if x["source_id"] else None,
                                     "evidence": [dict(e) for e in conn.execute("SELECT id,filename,sha256,size,visibility FROM evidence WHERE event_id=? ORDER BY id", (x["id"],)).fetchall()]}
                            for x in conn.execute("SELECT * FROM events WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
                "claims": [dict(c) | {
                    "reviews": [dict(r) for r in conn.execute("SELECT * FROM claim_reviews WHERE claim_id=? ORDER BY id", (c["id"],)).fetchall()],
                    # 非公众视图均展示补件明细；下方再按主张人过滤归属。
                    "supplement_requests": self._claim_supplements(conn, c["id"], include_internals=True),
                }
                           for c in conn.execute("SELECT * FROM claims WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
                "unlinked_evidence": [dict(e) for e in conn.execute("SELECT id,filename,sha256,size,visibility FROM evidence WHERE object_id=? AND event_id IS NULL ORDER BY id", (object_id,)).fetchall()],
            }
            if user["role"] == "claimant":
                # 主张人只看到公开来源事件和自己的主张，不能浏览内部调查材料。
                result["events"] = [e for e in result["events"] if e["visibility"] == "public"]
                result["unlinked_evidence"] = []
                result["claims"] = [c for c in result["claims"] if c["claimant_id"] == user_id]
                for c in result["claims"]:
                    c.pop("claimant_id", None)
            return result

    def list_objects(self, user_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            if user["role"] == "public":
                rows = conn.execute("SELECT id,inventory_no,title,object_type,public_summary,version FROM objects ORDER BY id").fetchall()
            else:
                rows = conn.execute("SELECT * FROM objects ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def object_history(self, user_id, object_id):
        with self.connect() as conn:
            user = self._user(conn, user_id, {"staff", "reviewer"})
            self._object(conn, object_id)
            rows = conn.execute("SELECT id,version,changed_by,created_at FROM object_versions WHERE object_id=? ORDER BY version", (object_id,)).fetchall()
            return [dict(r) for r in rows]

    def history_detail(self, user_id, object_id, version):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            row = conn.execute("SELECT * FROM object_versions WHERE object_id=? AND version=?", (object_id, version)).fetchone()
            if not row:
                raise BusinessError("历史版本不存在", 404, "not_found")
            return dict(row) | {"snapshot": json.loads(row["snapshot"])}


class Handler(BaseHTTPRequestHandler):
    server_version = "Provenance/1.0"

    def _store(self): return self.server.store  # type: ignore[attr-defined]

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise BusinessError("请求体必须是合法 JSON", 400, "invalid_json")
        if not isinstance(data, dict):
            raise BusinessError("JSON 顶层必须是对象", 422, "invalid_json")
        return data

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        parts = [p for p in path.split("/") if p]
        user = self.headers.get("X-User-Id", "")
        if method == "GET" and path == "/":
            body = (BASE_DIR / "web" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if method == "GET" and path == "/health": return self._send(200, {"ok": True})
        store = self._store()
        if parts == ["api", "objects"] and method == "GET": return self._send(200, {"items": store.list_objects(user)})
        if parts == ["api", "objects"] and method == "POST":
            d = self._body(); return self._send(201, store.create_object(user, d.get("inventory_no", ""), d.get("title", ""), d.get("object_type", ""), d.get("current_holder", ""), d.get("public_summary", "")))
        if parts == ["api", "sources"] and method == "POST":
            d = self._body(); return self._send(201, store.add_source(user, d.get("name", ""), d.get("source_type", ""), d.get("reference", "")))
        if len(parts) >= 3 and parts[:2] == ["api", "objects"]:
            object_id = int(parts[2])
            if len(parts) == 3 and method == "GET": return self._send(200, store.get_object(user, object_id))
            if len(parts) == 4 and parts[3] == "update" and method == "POST": return self._send(200, store.update_object(user, object_id, self._body().get("changes", {})))
            if len(parts) == 4 and parts[3] == "events" and method == "POST":
                d = self._body(); return self._send(201, store.add_event(user, object_id, d.get("event_type", ""), d.get("date_start", ""), d.get("date_end", ""), d.get("place", ""), d.get("description", ""), d.get("source_id"), d.get("visibility", "internal")))
            if len(parts) == 4 and parts[3] == "evidence" and method == "POST":
                d = self._body(); return self._send(201, store.upload_evidence(user, object_id, d.get("filename", ""), d.get("content_b64", ""), d.get("visibility", "internal"), d.get("event_id")))
            if len(parts) == 4 and parts[3] == "claims" and method == "POST":
                d = self._body(); return self._send(201, store.create_claim(user, object_id, d.get("claimed_by", ""), d.get("desired_outcome", "")))
            if len(parts) == 4 and parts[3] == "history" and method == "GET": return self._send(200, {"items": store.object_history(user, object_id)})
            if len(parts) == 5 and parts[3] == "history" and method == "GET": return self._send(200, store.history_detail(user, object_id, int(parts[4])))
        if len(parts) == 4 and parts[:2] == ["api", "claims"] and parts[3] == "transition" and method == "POST":
            d = self._body(); return self._send(200, store.transition_claim(user, int(parts[2]), d.get("status", ""), d.get("note", "")))
        if len(parts) == 4 and parts[:2] == ["api", "claims"] and parts[3] == "supplements" and method == "POST":
            d = self._body(); return self._send(201, store.create_supplement_request(user, int(parts[2]), d.get("material_category", ""), d.get("description", "")))
        if len(parts) == 4 and parts[:2] == ["api", "supplements"] and parts[3] == "materials" and method == "POST":
            d = self._body(); return self._send(201, store.upload_supplement_materials(user, int(parts[2]), d.get("files", [])))
        if len(parts) == 4 and parts[:2] == ["api", "supplements"] and parts[3] == "close" and method == "POST":
            d = self._body(); return self._send(200, store.close_supplement_request(user, int(parts[2]), d.get("review_note", "")))
        if len(parts) == 4 and parts[:3] == ["api", "supplements", "materials"] and method == "GET":
            return self._send(200, store.get_supplement_material(user, int(parts[3])))
        raise BusinessError("接口不存在", 404, "not_found")

    def _handle(self, method):
        try: self._dispatch(method)
        except BusinessError as exc:
            payload = {"error": {"code": exc.code, "message": exc.message}}
            if exc.details:
                payload["error"]["details"] = exc.details
            self._send(exc.status, payload)
        except (ValueError, TypeError): self._send(400, {"error": {"code": "invalid_path", "message": "路径参数格式错误"}})
        except Exception as exc: self._send(500, {"error": {"code": "internal_error", "message": str(exc)}})

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def log_message(self, fmt, *args): print(f"{self.address_string()} - {fmt % args}")


class ProvenanceServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, store): self.store = store; super().__init__(address, Handler)


def main():
    parser = argparse.ArgumentParser(description="博物馆藏品来源与返还审查")
    parser.add_argument("--db", default=str(DEFAULT_DB)); parser.add_argument("--port", type=int, default=8103)
    parser.add_argument("--init", action="store_true"); parser.add_argument("--seed", action="store_true")
    args = parser.parse_args(); store = ProvenanceStore(args.db); store.init_schema()
    if args.seed: store.seed()
    if args.init or args.seed: print(f"数据库已初始化: {args.db}"); return
    server = ProvenanceServer(("127.0.0.1", args.port), store)
    print(f"来源审查系统运行于 http://127.0.0.1:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
