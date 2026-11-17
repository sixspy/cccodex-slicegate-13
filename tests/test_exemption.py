
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

import app as appmod
from app import app, init_db, DB_PATH  # DB_PATH 已被 monkeypatch 指到临时库


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _login(c, username="admin", password="123456"):
    r = c.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _raw(sql, params=()):
    conn = sqlite3.connect(appmod.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _gate(h, sid):
    items = TestClient(app).get("/api/gate-readings", headers=h).json()["items"]
    return next(g for g in items if g["slice_id"] == sid)


def _make_partial(h, slice_no="SL-T-P", biz_date="2026-11-20"):
    r = TestClient(app).post("/api/slices", headers=h, json={
        "slice_no": slice_no, "biz_date": biz_date, "partition_key": "east",
        "row_count": 7, "expected_count": 10,
    })
    assert r.status_code == 200
    return r.json()["id"]


# ---------- 未审批豁免不得当成完整 ----------

def test_pending_exemption_is_not_completeness():
    c = TestClient(app); h = _login(c)
    sid = _make_partial(h)
    # 无豁免：硬拒绝
    assert c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "x"}).status_code == 409
    assert _gate(h, sid)["verdict"] == "硬拒绝"
    # 提交豁免但未审批：依旧硬拒绝，不得当成完整
    r = c.post("/api/exemptions", headers=h, json={
        "slice_id": sid, "caliber": "甲", "reason": "上游延迟，值班担保"})
    assert r.status_code == 200
    g = _gate(h, sid)
    assert g["verdict"] == "硬拒绝"
    assert g["approved_exemption_id"] is None
    assert c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "x"}).status_code == 409
    sl = _raw("SELECT release_status FROM slices WHERE id=?", (sid,))[0]
    assert sl["release_status"] == "未放行"


# ---------- 甲路：审批通过不写放行态，仍须走放行入口 ----------

def test_caliber_a_approve_then_release_entry_only():
    c = TestClient(app); h = _login(c)
    sid = _make_partial(h, "SL-T-A", "2026-11-21")
    r = c.post("/api/exemptions", headers=h, json={
        "slice_id": sid, "caliber": "甲", "reason": "甲路担保"})
    eid = r.json()["id"]
    d = c.post(f"/api/exemptions/{eid}/decision", headers=h, json={"action": "通过", "note": "同意"})
    assert d.status_code == 200
    # 铁律：审批通过不得顺带写成已放行态
    assert d.json()["release_status_unchanged"] == "未放行"
    assert _raw("SELECT release_status FROM slices WHERE id=?", (sid,))[0]["release_status"] == "未放行"
    g = _gate(h, sid)
    assert g["verdict"] == "可放行" and g["consistent"] is True
    # 仍须走放行入口
    rel = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_a"})
    assert rel.status_code == 200
    assert _raw("SELECT release_status FROM slices WHERE id=?", (sid,))[0]["release_status"] == "已放行"
    wm = _raw("SELECT * FROM watermarks WHERE source_slice='SL-T-A'")
    assert len(wm) == 1 and wm[0]["consumer"] == "ads_a"
    note = _raw("SELECT note FROM releases WHERE slice_id=? AND status='成功'", (sid,))[0]["note"]
    assert "甲路豁免" in note and f"#{eid}" in note


# ---------- 乙路：只读预览，正式放行与水位硬拒绝，另开补齐单 ----------

def test_caliber_b_hard_reject_preview_and_backfill():
    c = TestClient(app); h = _login(c)
    sid = _make_partial(h, "SL-T-B", "2026-11-22")
    eid = c.post("/api/exemptions", headers=h, json={
        "slice_id": sid, "caliber": "乙", "reason": "乙路降级"}).json()["id"]
    assert c.post(f"/api/exemptions/{eid}/decision", headers=h, json={"action": "通过"}).status_code == 200
    g = _gate(h, sid)
    assert g["verdict"] == "降级只读"
    # 正式成功放行连同水位硬拒绝
    rel = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_b"})
    assert rel.status_code == 409 and "硬拒绝" in rel.json()["detail"]
    assert _raw("SELECT release_status FROM slices WHERE id=?", (sid,))[0]["release_status"] == "未放行"
    assert _raw("SELECT COUNT(*) n FROM watermarks WHERE source_slice='SL-T-B'")[0]["n"] == 0
    # 降级只读预览：不写放行态、不写水位
    pv = c.post(f"/api/slices/{sid}/exemption-preview", headers=h, json={"consumer": "ads_b"})
    assert pv.status_code == 200
    body = pv.json()
    assert body["read_only"] is True and body["watermark_written"] is False
    assert body["release_status_unchanged"] == "未放行"
    assert _raw("SELECT release_status FROM slices WHERE id=?", (sid,))[0]["release_status"] == "未放行"
    assert _raw("SELECT COUNT(*) n FROM watermarks WHERE source_slice='SL-T-B'")[0]["n"] == 0
    # 另开补齐单
    bf = c.post(f"/api/slices/{sid}/backfill-order", headers=h, json={"consumer": "ads_b"})
    assert bf.status_code == 200 and bf.json()["status"] == "待补齐"
    oid = bf.json()["id"]
    # 幂等：在途补齐单不重复开
    again = c.post(f"/api/slices/{sid}/backfill-order", headers=h, json={"consumer": "ads_b"})
    assert again.json()["changed"] is False and again.json()["id"] == oid
    # 补齐关单后豁免不自动转放行，仍须重走裁决/入口
    assert c.post(f"/api/backfill-orders/{oid}/close", headers=h).status_code == 200
    assert c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_b"}).status_code == 409


def test_preview_requires_caliber_b():
    c = TestClient(app); h = _login(c)
    sid = _make_partial(h, "SL-T-PV", "2026-11-23")
    eid = c.post("/api/exemptions", headers=h, json={
        "slice_id": sid, "caliber": "甲", "reason": "x"}).json()["id"]
    c.post(f"/api/exemptions/{eid}/decision", headers=h, json={"action": "通过"})
    r = c.post(f"/api/slices/{sid}/exemption-preview", headers=h, json={"consumer": "x"})
    assert r.status_code == 409 and "降级只读预览" in r.json()["detail"]


# ---------- 策略册：选定即锁，当天不许再换，混路拒绝并记口径偏了 ----------

def test_policy_locked_no_switch_no_mix():
    c = TestClient(app); h = _login(c)
    r1 = c.post("/api/exemption-policy", headers=h, json={"biz_date": "2026-11-24", "caliber": "甲"})
    assert r1.status_code == 200 and r1.json()["changed"] is True
    # 同口径重复落笔幂等
    r2 = c.post("/api/exemption-policy", headers=h, json={"biz_date": "2026-11-24", "caliber": "甲"})
    assert r2.json()["changed"] is False
    # 改选乙路：拒绝
    r3 = c.post("/api/exemption-policy", headers=h, json={"biz_date": "2026-11-24", "caliber": "乙"})
    assert r3.status_code == 409 and "不许再换" in r3.json()["detail"]
    pol = _raw("SELECT * FROM exemption_policy WHERE biz_date='2026-11-24'")
    assert len(pol) == 1 and pol[0]["caliber"] == "甲"
    # 混路申请乙路：拒绝
    sid = _make_partial(h, "SL-T-MIX", "2026-11-24")
    r4 = c.post("/api/exemptions", headers=h, json={
        "slice_id": sid, "caliber": "乙", "reason": "混路试探"})
    assert r4.status_code == 409 and "混路" in r4.json()["detail"]
    assert _raw("SELECT COUNT(*) n FROM exemptions")[0]["n"] == 0
    ev = _raw("SELECT kind FROM exemption_events WHERE kind='豁免口径偏了'")
    assert len(ev) >= 2  # 策略改选一次 + 混路申请一次


# ---------- 偏离豁免策略册：硬拒绝 + 记口径偏了（模拟册单漂移） ----------

def test_drifted_policy_hard_rejects_and_audits():
    c = TestClient(app); h = _login(c)
    sid = _make_partial(h, "SL-T-DRIFT", "2026-11-25")
    eid = c.post("/api/exemptions", headers=h, json={
        "slice_id": sid, "caliber": "甲", "reason": "x"}).json()["id"]
    c.post(f"/api/exemptions/{eid}/decision", headers=h, json={"action": "通过"})
    # 人为制造策略册与豁免单不一致（模拟旁注/册单被改偏）
    conn = sqlite3.connect(appmod.DB_PATH)
    conn.execute("UPDATE exemption_policy SET caliber='乙' WHERE biz_date='2026-11-25'")
    conn.commit(); conn.close()
    g = _gate(h, sid)
    assert g["verdict"] == "硬拒绝" and g["consistent"] is False
    rel = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "x"})
    assert rel.status_code == 409 and "口径偏了" in rel.json()["detail"]
    assert _raw("SELECT release_status FROM slices WHERE id=?", (sid,))[0]["release_status"] == "未放行"
    assert any(e["kind"] == "豁免口径偏了" for e in
               TestClient(app).get("/api/exemption-events", headers=h).json()["items"])


# ---------- 两名值班同时落笔：互斥串行，后手显式争用，禁止悄悄成功 ----------

def test_concurrent_decisions_mutual_exclusion(monkeypatch):
    monkeypatch.setattr(appmod, "WRITE_BUSY_MS", 100)
    c = TestClient(app); h = _login(c)
    sid = _make_partial(h, "SL-T-LOCK", "2026-11-26")
    eid = c.post("/api/exemptions", headers=h, json={
        "slice_id": sid, "caliber": "甲", "reason": "x"}).json()["id"]
    barrier = threading.Barrier(2)
    results = []

    def worker():
        cc = TestClient(app)
        hh = _login(cc)
        barrier.wait()
        rr = cc.post(f"/api/exemptions/{eid}/decision", headers=hh, json={"action": "通过"})
        results.append(rr.status_code)

    t = threading.Thread(target=worker)
    t.start()
    barrier.wait()
    rr = c.post(f"/api/exemptions/{eid}/decision", headers=h, json={"action": "通过"})
    results.append(rr.status_code)
    t.join()
    assert sorted(results) == [200, 409]  # 恰好一笔成功，另一笔显式失败
    ex = _raw("SELECT * FROM exemptions WHERE id=?", (eid,))[0]
    assert ex["status"] == "通过" and ex["decided_at"] is not None
    # 成功审批事件只能有一条，禁止两笔都写成成功
    ok_events = _raw(
        "SELECT COUNT(*) n FROM exemption_events WHERE target=? AND kind='审批通过'",
        (f"slice:SL-T-LOCK",))
    assert ok_events[0]["n"] == 1


def test_held_write_lock_surfaces_contention(monkeypatch):
    monkeypatch.setattr(appmod, "WRITE_BUSY_MS", 100)
    c = TestClient(app); h = _login(c)
    with appmod.write_tx() as _held:  # 主线程持锁
        def contender():
            cc = TestClient(app)
            hh = _login(cc)
            rr = cc.post("/api/exemption-policy", headers=hh,
                         json={"biz_date": "2026-11-27", "caliber": "甲"})
            contender.status = rr.status_code
            contender.detail = rr.json()["detail"]
        t = threading.Thread(target=contender)
        t.start(); t.join()
    assert contender.status == 409 and "豁免争用" in contender.detail
    # 争用败笔不得落任何策略
    assert _raw("SELECT COUNT(*) n FROM exemption_policy WHERE biz_date='2026-11-27'")[0]["n"] == 0


# ---------- 审批详情/豁免列表/闸读数/旁注四方可核 ----------

def test_four_way_readings_are_verifiable():
    c = TestClient(app); h = _login(c)
    sid = _make_partial(h, "SL-T-AUDIT", "2026-11-28")
    eid = c.post("/api/exemptions", headers=h, json={
        "slice_id": sid, "caliber": "乙", "reason": "留痕核验"}).json()["id"]
    c.post(f"/api/exemptions/{eid}/decision", headers=h, json={"action": "通过", "note": "值班核过"})
    detail = c.get(f"/api/exemptions/{eid}", headers=h).json()
    assert detail["exemption"]["status"] == "通过"
    assert detail["policy"]["caliber"] == "乙"
    assert any(ev["kind"] == "审批通过" and "放行态保持" in ev["detail"] for ev in detail["events"])
    lst = c.get("/api/exemptions", headers=h).json()["items"]
    assert any(x["id"] == eid and x["release_status"] == "未放行" for x in lst)
    g = _gate(h, sid)
    assert g["approved_exemption_id"] == eid and g["exemption_caliber"] == "乙"
    assert g["policy_caliber"] == "乙" and g["consistent"] is True
    kinds = {x["kind"] for x in c.get("/api/exemption-events", headers=h).json()["items"]}
    assert {"策略锁定", "豁免申请", "审批通过"} <= kinds


def test_duplicate_open_application_blocked():
    c = TestClient(app); h = _login(c)
    sid = _make_partial(h, "SL-T-DUP", "2026-11-29")
    assert c.post("/api/exemptions", headers=h, json={
        "slice_id": sid, "caliber": "甲", "reason": "第一笔"}).status_code == 200
    r = c.post("/api/exemptions", headers=h, json={
        "slice_id": sid, "caliber": "甲", "reason": "第二笔"})
    assert r.status_code == 409 and "重复落笔" in r.json()["detail"]
    assert _raw("SELECT COUNT(*) n FROM exemptions WHERE slice_id=?", (sid,))[0]["n"] == 1


def test_complete_slice_needs_no_exemption():
    c = TestClient(app); h = _login(c)
    r = c.post("/api/slices", headers=h, json={
        "slice_no": "SL-T-FULL", "biz_date": "2026-11-30", "partition_key": "west",
        "row_count": 10, "expected_count": 10})
    sid = r.json()["id"]
    rr = c.post("/api/exemptions", headers=h, json={
        "slice_id": sid, "caliber": "甲", "reason": "多此一举"})
    assert rr.status_code == 409


def test_rejected_exemption_does_not_open_gate():
    c = TestClient(app); h = _login(c)
    sid = _make_partial(h, "SL-T-REJ", "2026-12-01")
    eid = c.post("/api/exemptions", headers=h, json={
        "slice_id": sid, "caliber": "甲", "reason": "x"}).json()["id"]
    assert c.post(f"/api/exemptions/{eid}/decision", headers=h,
                  json={"action": "拒绝", "note": "理由不足"}).status_code == 200
    assert _gate(h, sid)["verdict"] == "硬拒绝"
    assert c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "x"}).status_code == 409
