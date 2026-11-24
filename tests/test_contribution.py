"""贡献汇总 API 测试：有效/独家/折算、精确分数、同日来源变化、版本隔离、旧库补建、原子性。"""
from __future__ import annotations

from fractions import Fraction

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("AIS_DB_PATH", db_path)
    monkeypatch.setenv("PORT_MIN_LON", "121.0")
    monkeypatch.setenv("PORT_MIN_LAT", "31.0")
    monkeypatch.setenv("PORT_MAX_LON", "122.0")
    monkeypatch.setenv("PORT_MAX_LAT", "32.0")
    from app import database
    from app.main import app, get_conn

    database.init_db(database.connect(db_path))

    def _override():
        conn = database.connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_conn] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def m(msg, ship, ts, lon=121.5, lat=31.5):
    return {"msg_no": msg, "ship_no": ship, "ts": ts, "lon": lon, "lat": lat}


def submit(client, batch_no, messages, mode="submit"):
    return client.post(f"/batches/{batch_no}?mode={mode}", json={"messages": messages})


def day_of(client, date, version_id=None):
    params = {"date": date}
    if version_id is not None:
        params["version_id"] = version_id
    r = client.get("/contributions", params=params)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    return rows[0]


def frac(row):
    return Fraction(row["apportioned_count"])


def test_single_batch_all_exclusive(client):
    submit(client, "B1", [
        m("a1", "S1", "2026-11-24T08:00:00Z"),
        m("a2", "S1", "2026-11-24T08:05:00Z"),
    ])
    row = day_of(client, "2026-11-24")
    assert row["dedup_total"] == 2
    by = {b["batch_no"]: b for b in row["batches"]}
    assert by["B1"]["valid_message_count"] == 2
    assert by["B1"]["exclusive_message_count"] == 2
    assert frac(by["B1"]) == 2


def test_shared_message_splits_one_between_batches(client):
    # B1 提供 a1,a2；B2 重复提供 a1（同号同内容）
    submit(client, "B1", [
        m("a1", "S1", "2026-11-24T08:00:00Z"),
        m("a2", "S1", "2026-11-24T08:05:00Z"),
    ])
    submit(client, "B2", [m("a1", "S1", "2026-11-24T08:00:00Z")])
    row = day_of(client, "2026-11-24")
    by = {b["batch_no"]: b for b in row["batches"]}
    assert row["dedup_total"] == 2
    assert by["B1"]["valid_message_count"] == 2
    assert by["B1"]["exclusive_message_count"] == 1   # a2
    assert frac(by["B1"]) == Fraction(3, 2)            # 1 + 1/2
    assert by["B2"]["valid_message_count"] == 1
    assert by["B2"]["exclusive_message_count"] == 0
    assert frac(by["B2"]) == Fraction(1, 2)
    assert frac(by["B1"]) + frac(by["B2"]) == row["dedup_total"]


def test_three_batches_split_thirds(client):
    t = "2026-11-24T08:00:00Z"
    for b in ("B1", "B2", "B3"):
        submit(client, b, [m("a1", "S1", t)])
    row = day_of(client, "2026-11-24")
    assert row["dedup_total"] == 1
    assert {b["batch_no"]: frac(b) for b in row["batches"]} == {
        "B1": Fraction(1, 3),
        "B2": Fraction(1, 3),
        "B3": Fraction(1, 3),
    }
    assert sum(frac(b) for b in row["batches"]) == row["dedup_total"]


def test_replace_that_removes_source_updates_other_batches_even_same_total(client):
    # a1 由 B1、B2 共同提供；B2 换交为空不了（至少 1 条），换交为另一条 b9
    submit(client, "B1", [m("a1", "S1", "2026-11-24T08:00:00Z")])
    submit(client, "B2", [m("a1", "S1", "2026-11-24T08:00:00Z")])
    before = day_of(client, "2026-11-24")
    assert before["dedup_total"] == 1
    assert {b["batch_no"]: frac(b) for b in before["batches"]} == {
        "B1": Fraction(1, 2), "B2": Fraction(1, 2)
    }

    # B2 换交为另一条不同消息：去重总数仍为 2? a1 仍有效, b9 新增 -> 2
    r = submit(client, "B2", [m("b9", "S2", "2026-11-24T09:00:00Z")], mode="replace")
    assert r.status_code == 200
    after = day_of(client, "2026-11-24")
    by = {b["batch_no"]: b for b in after["batches"]}
    # a1 现在只剩 B1 提供 -> B1 独占 a1
    assert frac(by["B1"]) == 1
    assert frac(by["B2"]) == 1
    assert after["dedup_total"] == 2


def test_replace_removing_shared_source_keeps_total_but_reallocates(client):
    # B1: a1,a2,a3 ; B2: a1,a2（与 B1 共享 a1/a2）
    submit(client, "B1", [
        m("a1", "S1", "2026-11-24T08:00:00Z"),
        m("a2", "S1", "2026-11-24T08:05:00Z"),
        m("a3", "S1", "2026-11-24T08:10:00Z"),
    ])
    submit(client, "B2", [
        m("a1", "S1", "2026-11-24T08:00:00Z"),
        m("a2", "S1", "2026-11-24T08:05:00Z"),
    ])
    before = day_of(client, "2026-11-24")
    assert before["dedup_total"] == 3
    by = {b["batch_no"]: b for b in before["batches"]}
    # B1: a1=1/2 + a2=1/2 + a3=1 -> 2
    # B2: a1=1/2 + a2=1/2          -> 1
    assert frac(by["B1"]) == 2 and frac(by["B2"]) == 1

    # B2 换交为只提供 a1：去重总数仍是 3（a2、a3 仍由 B1 提供），
    # 但 a2 的来源从 {B1,B2} 变为 {B1}，B1 的份额必须上调，B2 下调。
    r = submit(client, "B2", [m("a1", "S1", "2026-11-24T08:00:00Z")], mode="replace")
    assert r.status_code == 200
    after = day_of(client, "2026-11-24")
    by = {b["batch_no"]: b for b in after["batches"]}
    assert after["dedup_total"] == 3 == before["dedup_total"]
    # B1: a1=1/2 + a2=1 + a3=1 -> 5/2；B2: a1=1/2
    assert frac(by["B1"]) == Fraction(5, 2)
    assert frac(by["B2"]) == Fraction(1, 2)
    assert sum(frac(b) for b in after["batches"]) == after["dedup_total"]


def test_days_are_separated_by_utc(client):
    submit(client, "B1", [
        m("a1", "S1", "2026-11-24T23:59:00Z"),
        m("a2", "S1", "2026-11-25T00:01:00Z"),
    ])
    day1 = day_of(client, "2026-11-24")
    day2 = day_of(client, "2026-11-25")
    assert day1["dedup_total"] == day2["dedup_total"] == 1
    all_rows = client.get("/contributions").json()
    assert [r["date"] for r in all_rows] == ["2026-11-24", "2026-11-25"]


def test_timezone_normalized_to_utc_day(client):
    # +08:00 的 2026-11-25T02:00 = UTC 2026-11-24T18:00
    submit(client, "B1", [m("a1", "S1", "2026-11-25T02:00:00+08:00")])
    assert day_of(client, "2026-11-24")["dedup_total"] == 1
    assert client.get("/contributions", params={"date": "2026-11-25"}).json() == []


def test_historical_version_contribution_preserved(client):
    submit(client, "B1", [m("a1", "S1", "2026-11-24T08:00:00Z")])
    v1 = client.get("/versions").json()[-1]["version_id"]
    submit(client, "B2", [m("a1", "S1", "2026-11-24T08:00:00Z")])
    v2 = client.get("/versions").json()[-1]["version_id"]
    assert day_of(client, "2026-11-24", v1)["batches"][0]["apportioned_count"] == "1/1"
    assert day_of(client, "2026-11-24", v2)["batches"][0]["apportioned_count"] == "1/2"


def test_unknown_version_404(client):
    r = client.get("/contributions", params={"version_id": 9999})
    assert r.status_code == 404


def test_batch_contribution_endpoint(client):
    submit(client, "B1", [
        m("a1", "S1", "2026-11-24T08:00:00Z"),
        m("a2", "S1", "2026-11-25T08:00:00Z"),
    ])
    submit(client, "B2", [m("a1", "S1", "2026-11-24T08:00:00Z")])
    rows = client.get("/batches/B1/contributions").json()
    assert [r["date"] for r in rows] == ["2026-11-24", "2026-11-25"]
    assert rows[0]["apportioned_count"] == "1/2"
    assert rows[1]["apportioned_count"] == "1/1"
    assert client.get("/batches/NOPE/contributions").status_code == 404


def test_write_failure_leaves_contributions_unchanged(client, monkeypatch):
    submit(client, "B1", [
        m("a1", "S1", "2026-11-24T08:00:00Z"),
        m("i1", "S1", "2026-11-24T08:05:00Z", 121.5, 31.5),
        m("a2", "S1", "2026-11-24T08:10:00Z", 123.0, 33.0),
    ])
    before = client.get("/contributions").json()
    from app import service

    def boom(*a, **k):
        raise RuntimeError("simulated")

    monkeypatch.setattr(service, "_insert_run_record", boom)
    r = submit(client, "B2", [m("z1", "S9", "2026-11-24T11:00:00Z")])
    assert r.status_code == 500
    monkeypatch.undo()
    assert client.get("/contributions").json() == before


def test_change_on_one_day_leaves_other_day_untouched(client):
    submit(client, "B1", [
        m("a1", "S1", "2026-11-24T08:00:00Z"),
        m("a2", "S1", "2026-11-25T08:00:00Z"),
    ])
    # 只给 11-25 增加一条共享消息，11-24 应保持原样
    submit(client, "B2", [m("a2", "S1", "2026-11-25T08:00:00Z")])
    d1 = day_of(client, "2026-11-24")
    d2 = day_of(client, "2026-11-25")
    assert d1["batches"][0]["apportioned_count"] == "1/1"
    assert {b["batch_no"]: frac(b) for b in d2["batches"]} == {
        "B1": Fraction(1, 2), "B2": Fraction(1, 2)
    }


def test_sums_equal_dedup_total_across_many_batches(client):
    # 构造 5 批，其中若干消息被 1~5 批共同提供
    shared = "2026-11-24T08:00:00Z"
    batches = [f"B{i}" for i in range(1, 6)]
    for i, b in enumerate(batches, start=1):
        msgs = [m("shared", "S1", shared)]
        for j in range(i):
            msgs.append(m(f"u{b}_{j}", f"S{i}", f"2026-11-24T09:{j:02d}:00Z"))
        r = submit(client, b, msgs)
        assert r.status_code == 200
    row = day_of(client, "2026-11-24")
    total = row["dedup_total"]
    assert total == 1 + sum(range(1, 6))
    assert sum(frac(b) for b in row["batches"]) == total
    by = {b["batch_no"]: b for b in row["batches"]}
    assert frac(by["B1"]) == Fraction(1, 5) + 1
    assert frac(by["B5"]) == Fraction(1, 5) + 5


def test_replace_with_identical_content_keeps_contributions(client):
    msgs = [
        m("a1", "S1", "2026-11-24T08:00:00Z"),
        m("a2", "S1", "2026-11-24T08:05:00Z"),
    ]
    submit(client, "B1", msgs)
    before = client.get("/contributions").json()[0]
    r = submit(client, "B1", msgs, mode="replace")
    assert r.status_code == 200
    after = client.get("/contributions").json()[0]
    # 只应版本号变化，汇总内容不变
    assert after["date"] == before["date"]
    assert after["dedup_total"] == before["dedup_total"]
    assert after["batches"] == before["batches"]


def test_fraction_fields_are_reduced_and_sum_exactly(client):
    t = "2026-11-24T08:00:00Z"
    for b in ("B1", "B2", "B3", "B4"):
        submit(client, b, [m("a1", "S1", t)])
    row = day_of(client, "2026-11-24")
    total = Fraction(0)
    for b in row["batches"]:
        f = Fraction(b["apportioned_numerator"], b["apportioned_denominator"])
        assert b["apportioned_count"] == f"{f.numerator}/{f.denominator}"
        assert b["apportioned"] == b["apportioned_count"] == b["share"]
        total += f
    assert total == row["dedup_total"] == 1


def test_contribution_isolated_per_version_after_replace(client):
    submit(client, "B1", [m("a1", "S1", "2026-11-24T08:00:00Z")])
    submit(client, "B2", [m("a1", "S1", "2026-11-24T08:00:00Z")])
    v2 = client.get("/versions").json()[-1]["version_id"]
    submit(client, "B2", [m("b1", "S2", "2026-11-24T09:00:00Z")], mode="replace")
    v3 = client.get("/versions").json()[-1]["version_id"]

    old = day_of(client, "2026-11-24", v2)
    new = day_of(client, "2026-11-24", v3)
    assert {b["batch_no"]: frac(b) for b in old["batches"]} == {
        "B1": Fraction(1, 2), "B2": Fraction(1, 2)
    }
    assert {b["batch_no"]: frac(b) for b in new["batches"]} == {
        "B1": Fraction(1), "B2": Fraction(1)
    }


def test_deleting_last_source_drops_day_to_zero(client):
    submit(client, "B1", [m("a1", "S1", "2026-11-24T08:00:00Z")])
    # 换交 B1 提供另一天的消息 -> 11-24 归零
    submit(client, "B1", [m("a2", "S1", "2026-11-25T08:00:00Z")], mode="replace")
    d1 = day_of(client, "2026-11-24")
    assert d1["dedup_total"] == 0
    assert d1["batches"] == []
    assert day_of(client, "2026-11-25")["dedup_total"] == 1


def test_single_day_route_and_query_aliases(client):
    submit(client, "B1", [m("a1", "S1", "2026-11-24T08:00:00Z")])
    r = client.get("/contributions/2026-11-24")
    assert r.status_code == 200 and r.json()["dedup_total"] == 1
    assert client.get("/contributions/2026-01-01").status_code == 404
    for param in ("date", "utc_date", "day"):
        resp = client.get("/contributions", params={param: "2026-11-24"})
        assert resp.status_code == 200
        assert len(resp.json()) == 1


def test_bad_date_format_rejected(client):
    assert client.get("/contributions", params={"date": "2026/11/24"}).status_code == 422


def test_backfill_on_existing_db_without_contributions(tmp_path, monkeypatch):
    """旧库：已有版本/消息/来源，但没有贡献汇总表（连表都不存在）。"""
    db_path = str(tmp_path / "legacy.db")
    monkeypatch.setenv("AIS_DB_PATH", db_path)
    monkeypatch.setenv("PORT_MIN_LON", "121.0")
    monkeypatch.setenv("PORT_MIN_LAT", "31.0")
    monkeypatch.setenv("PORT_MAX_LON", "122.0")
    monkeypatch.setenv("PORT_MAX_LAT", "32.0")

    import sqlite3

    from app import database

    conn = database.connect(db_path)
    database.init_db(conn)
    # 模拟旧库：连贡献表都不存在
    conn.execute("DROP TABLE contribution_batch")
    conn.execute("DROP TABLE contribution_day")
    conn.execute("INSERT INTO batch(batch_no, created_at) VALUES('LB','2026-11-24T00:00:00Z')")
    conn.execute(
        "INSERT INTO message(msg_no, ship_no, ts, lon, lat, content_sha, created_batch, active) "
        "VALUES('m1','S1','2026-11-24T08:00:00Z',121.5,31.5,'x','LB',1)"
    )
    conn.execute("INSERT INTO message_source(msg_no, batch_no) VALUES('m1','LB')")
    conn.execute(
        "INSERT INTO result_version(created_at, batch_set, batches, active) "
        "VALUES('2026-11-24T00:00:00Z','h','[\"LB\"]',1)"
    )
    conn.close()

    from app.main import app, get_conn

    def _override():
        c = database.connect(db_path)
        try:
            yield c
        finally:
            c.close()

    app.dependency_overrides[get_conn] = _override
    try:
        # TestClient 启动 lifespan 会 init_db，触发补建
        with TestClient(app) as c:
            rows = c.get("/contributions").json()
    finally:
        app.dependency_overrides.clear()

    assert len(rows) == 1
    assert rows[0]["dedup_total"] == 1
    assert rows[0]["batches"][0]["batch_no"] == "LB"
    assert rows[0]["batches"][0]["apportioned_count"] == "1/1"
