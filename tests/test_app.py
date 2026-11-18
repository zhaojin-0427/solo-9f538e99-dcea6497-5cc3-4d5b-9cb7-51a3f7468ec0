"""关键场景自测：核心计算、修订规则、事务原子性、网页/API 一致性、重启持久化。

运行：python3 -m unittest discover -s tests -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import core, db  # noqa: E402
from app.app import create_app  # noqa: E402

def epoch(y, mo, d, h, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp())


# 2026-11-17 09:00Z / 10:00Z / 11:00Z
T09, T10, T11 = epoch(2026, 11, 17, 9), epoch(2026, 11, 17, 10), epoch(2026, 11, 17, 11)


def csv_rows(station, device, start_epoch, counters, revision=1):
    """生成连续分钟的 CSV 行（无表头）。"""
    return [
        f"{station},{device},{core.iso_utc(start_epoch + i * 60)},{c},{revision}"
        for i, c in enumerate(counters)
    ]


class CoreParseTests(unittest.TestCase):
    def test_english_header(self):
        text = "station,device,minute,counter,revision\nS1,primary,2026-11-17T10:00:00Z,123,1\n"
        valid, errors = core.parse_csv_text(text)
        self.assertEqual(errors, [])
        self.assertEqual(len(valid), 1)
        r = valid[0][2]
        self.assertEqual((r.station, r.device, r.counter, r.revision), ("S1", "primary", 123, 1))
        self.assertEqual(r.minute_epoch, T10)

    def test_chinese_header_and_aliases(self):
        text = "站点,主备标记,UTC时间,计数,修订号\n站A,主,2026-11-17 10:00,5,2\n站A,备,2026-11-17 10:00,7,2\n"
        valid, errors = core.parse_csv_text(text)
        self.assertEqual(errors, [])
        self.assertEqual([v[2].device for v in valid], ["primary", "backup"])
        self.assertEqual(valid[0][2].minute_epoch, T10)  # 未带时区按 UTC

    def test_no_header(self):
        text = "S1,backup,2026-11-17T10:00Z,9999,3\n"
        valid, errors = core.parse_csv_text(text)
        self.assertEqual(errors, [])
        self.assertEqual(valid[0][2].counter, 9999)

    def test_timezone_offset_converted_to_utc(self):
        text = "S1,primary,2026-11-17T18:00:00+08:00,1,1\n"
        valid, _ = core.parse_csv_text(text)
        self.assertEqual(valid[0][2].minute_epoch, T10)

    def test_bad_rows_rejected_individually(self):
        text = "\n".join([
            "S1,primary,2026-11-17T10:00Z,100,1",      # ok
            "S1,primary,2026-11-17T10:01Z,10000,1",    # 计数超界
            "S1,primary,2026-11-17T10:02Z,-1,1",       # 计数为负
            "S1,primary,2026-11-17T10:03Z,abc,1",      # 计数非整数
            "S1,primary,2026-11-17T10:04:30Z,1,1",     # 秒非 00
            "S1,third,2026-11-17T10:05Z,1,1",          # 设备标记无法识别
            "S1,primary,2026-11-17T10:06Z,1,-2",       # 修订号为负
            "S1,primary,2026-11-17T10:07Z,1",          # 列数不足
            "S1,primary,2026-11-17T10:08Z,1,1,EXTRA",  # 多余数据列
            ",primary,2026-11-17T10:09Z,1,1",          # 站点为空
        ])
        valid, errors = core.parse_csv_text(text)
        self.assertEqual(len(valid), 1)
        self.assertEqual(len(errors), 9)
        self.assertEqual([e.row_no for e in errors], list(range(2, 11)))

    def test_file_level_errors(self):
        with self.assertRaises(core.CsvFormatError):
            core.parse_csv_text("")
        with self.assertRaises(core.CsvFormatError):
            core.parse_csv_text("station,device,minute,counter\n")  # 表头缺修订号列
        with self.assertRaises(core.CsvFormatError):
            core.parse_csv_text("station,device,minute,counter,revision\n")  # 只有表头

    def test_trailing_comma_tolerated(self):
        valid, errors = core.parse_csv_text("S1,primary,2026-11-17T10:00Z,1,1,\n")
        self.assertEqual(errors, [])
        self.assertEqual(len(valid), 1)

    def test_parse_hour_alignment(self):
        self.assertEqual(core.parse_hour("2026-11-17T10:00Z"), T10)
        self.assertEqual(core.parse_hour("2026-11-17 10:00"), T10)
        with self.assertRaises(ValueError):
            core.parse_hour("2026-11-17T10:30Z")


class CoreComputeTests(unittest.TestCase):
    def test_counter_delta_wraparound(self):
        self.assertEqual(core.counter_delta(9998, 3), 5)   # 下降按一次 10000 回绕
        self.assertEqual(core.counter_delta(100, 250), 150)
        self.assertEqual(core.counter_delta(7, 7), 0)

    def test_full_primary_hour(self):
        counters = {("primary", T10 + i * 60): 1000 + i for i in range(61)}
        r = core.compute_hour(counters, "S1", T10)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["total_pulses"], 60)
        self.assertEqual(r["rainfall_mm"], 12.0)  # 60 × 0.2
        self.assertEqual(r["missing_intervals"], 0)
        self.assertTrue(all(i["source"] == "primary" for i in r["intervals"]))

    def test_backup_fallback_and_no_cross_device(self):
        counters = {("primary", T10 + i * 60): 100 + i for i in range(61)}
        # 删掉主计 10:30，并放备计 10:30/10:31
        del counters[("primary", T10 + 30 * 60)]
        counters[("backup", T10 + 30 * 60)] = 500
        counters[("backup", T10 + 31 * 60)] = 504
        r = core.compute_hour(counters, "S1", T10)
        self.assertEqual(r["status"], "incomplete")
        self.assertIsNone(r["rainfall_mm"])
        # 第 29 段（10:29~10:30）：主计缺 10:30、备计缺 10:29，跨设备不可减 -> 缺失
        self.assertEqual(r["intervals"][29]["source"], "missing")
        # 第 30 段（10:30~10:31）：备计两端齐全 -> 备计差值
        self.assertEqual(r["intervals"][30]["source"], "backup")
        self.assertEqual(r["intervals"][30]["pulses"], 4)
        # 第 31 段（10:31~10:32）：主计齐全 -> 主计
        self.assertEqual(r["intervals"][31]["source"], "primary")
        self.assertEqual(r["missing_intervals"], 1)
        self.assertEqual(r["source_counts"]["backup"], 1)

    def test_missing_interval_makes_hour_unavailable(self):
        counters = {("primary", T10 + i * 60): i for i in range(61)}
        del counters[("primary", T10 + 45 * 60)]
        r = core.compute_hour(counters, "S1", T10)
        self.assertEqual(r["status"], "incomplete")
        self.assertIsNone(r["rainfall_mm"])
        self.assertIsNone(r["total_pulses"])
        self.assertEqual(r["missing_intervals"], 2)  # 44~45 与 45~46 两段都缺端点

    def test_wraparound_in_interval(self):
        # 计数器在 10:00~10:01 间从 9998 回绕到 3 -> 5 脉冲，其余分钟不变
        counters = {("primary", T10 + i * 60): 3 for i in range(61)}
        counters[("primary", T10)] = 9998
        r = core.compute_hour(counters, "S1", T10)
        self.assertEqual(r["intervals"][0]["pulses"], 5)
        self.assertEqual(r["rainfall_mm"], 1.0)

    def test_hour_must_align(self):
        with self.assertRaises(ValueError):
            core.compute_hour({}, "S1", T10 + 60)


class DbTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "t.db")
        self.conn = db.connect(self.path)
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def _reading(self, minute, counter, rev, device="primary", station="S1"):
        return core.Reading(station, device, minute, counter, rev)

    def test_revision_rule(self):
        rows = [(1, "raw", self._reading(T10, 100, 1))]
        rep = db.apply_batch(self.conn, rows, [], "a.csv")
        self.assertEqual(rep["accepted_count"], 1)

        # 同修订号、低修订号都被拒绝
        rep = db.apply_batch(self.conn, [(1, "raw", self._reading(T10, 200, 1))], [], "b.csv")
        self.assertEqual(rep["accepted_count"], 0)
        self.assertEqual(rep["rejected_count"], 1)
        rep = db.apply_batch(self.conn, [(1, "raw", self._reading(T10, 300, 0))], [], "c.csv")
        self.assertEqual(rep["accepted_count"], 0)

        # 更高修订号被接受并覆盖
        rep = db.apply_batch(self.conn, [(1, "raw", self._reading(T10, 400, 2))], [], "d.csv")
        self.assertEqual(rep["accepted_count"], 1)
        self.assertEqual(rep["accepted"][0]["action"], "updated")
        got = db.fetch_hour_counters(self.conn, "S1", T10)
        self.assertEqual(got[("primary", T10)], 400)

    def test_same_key_within_one_batch(self):
        rows = [
            (1, "r1", self._reading(T10, 100, 1)),
            (2, "r2", self._reading(T10, 150, 2)),  # 更高修订，覆盖同批前一行
            (3, "r3", self._reading(T10, 120, 2)),  # 同修订号，拒绝
        ]
        rep = db.apply_batch(self.conn, rows, [], "e.csv")
        self.assertEqual(rep["accepted_count"], 2)
        self.assertEqual(rep["rejected_count"], 1)
        got = db.fetch_hour_counters(self.conn, "S1", T10)
        self.assertEqual(got[("primary", T10)], 150)

    def test_atomic_rollback_no_partial_batch(self):
        # 第二行设备标记非法（绕过解析层直接构造），触发 CHECK 约束失败
        bad = core.Reading("S1", "tertiary", T10 + 60, 5, 1)
        rows = [(1, "ok", self._reading(T10, 100, 1)), (2, "bad", bad)]
        with self.assertRaises(sqlite3.Error):
            db.apply_batch(self.conn, rows, [], "f.csv")
        # 回滚：读数与批次记录都不留
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM batch_rows").fetchone()[0], 0)

    def test_persistence_across_reopen(self):
        rows = [(1, "raw", self._reading(T10, 100, 1))]
        db.apply_batch(self.conn, rows, [], "g.csv")
        self.conn.close()
        conn2 = db.connect(self.path)
        try:
            got = db.fetch_hour_counters(conn2, "S1", T10)
            self.assertEqual(got[("primary", T10)], 100)
        finally:
            conn2.close()
        self.conn = db.connect(self.path)  # tearDown 用


class AppTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "app.db")
        self.app = create_app(self.path)
        self.client = self.app.test_client()

    def _upload(self, text):
        return self.client.post("/api/upload", data={"csv_text": text})

    def _hourly(self, station, hour_text):
        resp = self.client.get(f"/api/hourly?station={station}&hour={hour_text}")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def test_upload_and_query_flow(self):
        text = "\n".join(csv_rows("S1", "primary", T10, [1000 + i for i in range(61)]))
        resp = self._upload(text)
        self.assertEqual(resp.status_code, 200)
        rep = resp.get_json()
        self.assertEqual(rep["accepted_count"], 61)
        self.assertEqual(rep["rejected_count"], 0)

        r = self._hourly("S1", "2026-11-17T10:00Z")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["rainfall_mm"], 12.0)

        # 网页与 API 一致
        html = self.client.get("/report?station=S1&hour=2026-11-17T10:00Z").get_data(as_text=True)
        self.assertIn("12.0 mm", html)
        self.assertIn("可供数", html)

    def test_boundary_revision_changes_both_hours_consistently(self):
        # 09:00~11:00 共 121 个主计读数，每分钟 +10 脉冲
        text = "\n".join(csv_rows("S1", "primary", T09, [1000 + 10 * i for i in range(121)]))
        self.assertEqual(self._upload(text).get_json()["accepted_count"], 121)
        self.assertEqual(self._hourly("S1", "2026-11-17T09:00Z")["rainfall_mm"], 120.0)
        self.assertEqual(self._hourly("S1", "2026-11-17T10:00Z")["rainfall_mm"], 120.0)

        # 修订整点 10:00 读数：1600 -> 1595（修订号 2），同时影响前后两小时
        rev = f"S1,primary,{core.iso_utc(T10)},1595,2"
        rep = self._upload(rev).get_json()
        self.assertEqual(rep["accepted_count"], 1)
        self.assertEqual(rep["accepted"][0]["action"], "updated")

        r09 = self._hourly("S1", "2026-11-17T09:00Z")
        r10 = self._hourly("S1", "2026-11-17T10:00Z")
        self.assertEqual(r09["rainfall_mm"], 119.0)  # 末段少 5 脉冲
        self.assertEqual(r10["rainfall_mm"], 121.0)  # 首段多 5 脉冲

        # 网页与 API 对两小时都一致
        h09 = self.client.get("/report?station=S1&hour=2026-11-17T09:00Z").get_data(as_text=True)
        h10 = self.client.get("/report?station=S1&hour=2026-11-17T10:00Z").get_data(as_text=True)
        self.assertIn("119.0 mm", h09)
        self.assertIn("121.0 mm", h10)

    def test_stale_revision_rejected_and_data_unchanged(self):
        text = "\n".join(csv_rows("S1", "primary", T10, [1000 + i for i in range(61)], revision=2))
        self._upload(text)
        before = self._hourly("S1", "2026-11-17T10:00Z")

        stale = f"S1,primary,{core.iso_utc(T10)},5555,1"   # 低修订
        same = f"S1,primary,{core.iso_utc(T10 + 60)},5555,2"  # 同修订
        rep = self._upload("\n".join([stale, same])).get_json()
        self.assertEqual(rep["accepted_count"], 0)
        self.assertEqual(rep["rejected_count"], 2)
        after = self._hourly("S1", "2026-11-17T10:00Z")
        self.assertEqual(before, after)

    def test_incomplete_hour_and_gap_display(self):
        # 缺 10:45 一个端点 -> 两段缺失，整小时不可供数
        counters = [1000 + i for i in range(61)]
        lines = csv_rows("S1", "primary", T10, counters)
        del lines[45]
        self._upload("\n".join(lines))
        r = self._hourly("S1", "2026-11-17T10:00Z")
        self.assertEqual(r["status"], "incomplete")
        self.assertIsNone(r["rainfall_mm"])
        self.assertEqual(r["missing_intervals"], 2)
        html = self.client.get("/report?station=S1&hour=2026-11-17T10:00Z").get_data(as_text=True)
        self.assertIn("不可供数", html)
        self.assertIn("缺口 2 / 60", html)

    def test_bad_file_leaves_nothing(self):
        resp = self._upload("station,device,minute,counter\n")  # 表头缺列
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/api/upload", data={})  # 空上传
        self.assertEqual(resp.status_code, 400)
        batches = self.client.get("/api/batches").get_json()
        self.assertEqual(batches, [])

    def test_partial_rows_atomic_report(self):
        good = csv_rows("S1", "primary", T10, [1000 + i for i in range(61)])
        good[10] = "S1,primary,not-a-time,1,1"  # 一行坏行
        rep = self._upload("\n".join(good)).get_json()
        self.assertEqual(rep["accepted_count"], 60)
        self.assertEqual(rep["rejected_count"], 1)
        self.assertIn("时间", rep["rejected"][0]["reason"])
        # 缺 10:10 端点 -> 不可供数
        r = self._hourly("S1", "2026-11-17T10:00Z")
        self.assertEqual(r["status"], "incomplete")

    def test_restart_persistence(self):
        text = "\n".join(csv_rows("S1", "primary", T10, [1000 + i for i in range(61)]))
        self._upload(text)
        # 模拟重启：同一数据库文件新建应用实例
        app2 = create_app(self.path)
        client2 = app2.test_client()
        r = client2.get("/api/hourly?station=S1&hour=2026-11-17T10:00Z").get_json()
        self.assertEqual(r["rainfall_mm"], 12.0)

    def test_health_and_stations(self):
        self.assertEqual(self.client.get("/api/health").get_json()["status"], "ok")
        text = "\n".join(csv_rows("S1", "backup", T10, [i for i in range(61)]))
        self._upload(text)
        stations = self.client.get("/api/stations").get_json()
        self.assertEqual(stations[0]["station"], "S1")
        self.assertEqual(stations[0]["readings"], 61)

    def test_hour_param_validation(self):
        resp = self.client.get("/api/hourly?station=S1&hour=2026-11-17T10:30Z")
        self.assertEqual(resp.status_code, 400)
        resp = self.client.get("/api/hourly?hour=2026-11-17T10:00Z")
        self.assertEqual(resp.status_code, 400)


class BasinCoreTests(unittest.TestCase):
    """流域权重校验与面雨量计算（纯逻辑）。"""

    def _hour(self, pulses_per_minute):
        counters = {("primary", T10 + i * 60): i * pulses_per_minute for i in range(61)}
        return core.compute_hour(counters, "S", T10)

    def test_weight_parsing_and_formatting(self):
        self.assertEqual(core.weight_to_ppm("0.25"), 250_000)
        self.assertEqual(core.weight_to_ppm("0.000001"), 1)
        self.assertEqual(core.weight_to_ppm("1"), core.WEIGHT_SCALE)
        self.assertEqual(core.weight_to_ppm("0.5000000"), 500_000)  # 末尾零不影响
        self.assertEqual(core.format_weight(250_000), "0.25")
        self.assertEqual(core.format_weight(1_000_000), "1")
        self.assertEqual(core.format_weight(370_000), "0.37")

    def test_invalid_weights_rejected(self):
        for bad in ("0", "-0.1", "1.0001", "abc", "", "0.1234567", "NaN", "Infinity"):
            with self.assertRaises(ValueError, msg=bad):
                core.weight_to_ppm(bad)

    def test_config_requires_2_to_8_unique_stations(self):
        good = [("S1", "0.5"), ("S2", "0.5")]
        self.assertEqual(len(core.parse_basin_config("B", good).members), 2)
        eight = [(f"S{i}", "0.125") for i in range(8)]
        self.assertEqual(len(core.parse_basin_config("B", eight).members), 8)
        for bad in (
            ("B", [("S1", "1")]),                                  # 只有 1 站
            ("B", [(f"S{i}", "0.1") for i in range(9)]),           # 9 站
            ("B", [("S1", "0.5"), ("S1", "0.5")]),                 # 站点重复
            ("B", [("S1", "0.5"), ("S2", "0.4")]),                 # 合计 0.9
            ("B", [("S1", "0.5"), ("S2", "0.6")]),                 # 合计 1.1
            ("B", [("", "0.5"), ("S2", "0.5")]),                   # 空站点
            ("", good),                                            # 空流域名
        ):
            with self.assertRaises(core.BasinConfigError, msg=str(bad)):
                core.parse_basin_config(*bad)

    def test_weight_sum_uses_exact_integer_arithmetic(self):
        # 0.1 + 0.2 + 0.7 在浮点下不精确，ppm 整数比较可精确通过
        cfg = core.parse_basin_config("B", [("A", "0.1"), ("B", "0.2"), ("C", "0.7")])
        self.assertEqual(sum(m.weight_ppm for m in cfg.members), core.WEIGHT_SCALE)

    def test_weighted_basin_result(self):
        cfg = core.parse_basin_config("B", [("S1", "0.4"), ("S2", "0.6")])
        # S1: 60 脉冲 = 12.0 mm；S2: 30 脉冲 = 6.0 mm
        hours = {"S1": self._hour(1), "S2": self._hour(1)}
        hours["S2"]["rainfall_mm"] = 6.0       # 直接构造不完整控制器之外的情形
        r = core.compute_basin_hour(cfg, hours, T10)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["hour_start"], "2026-11-17T10:00:00Z")
        self.assertEqual(r["members"][0]["contribution_mm"], 4.8)    # 12.0 × 0.4
        self.assertEqual(r["members"][1]["contribution_mm"], 3.6)    # 6.0 × 0.6
        self.assertEqual(r["rainfall_mm"], 8.4)
        self.assertEqual(r["unavailable_stations"], [])

    def test_any_unavailable_member_makes_basin_unavailable(self):
        cfg = core.parse_basin_config("B", [("S1", "0.4"), ("S2", "0.6")])
        hours = {"S1": self._hour(1), "S2": self._hour(1)}
        hours["S2"]["status"] = "incomplete"
        hours["S2"]["rainfall_mm"] = None
        r = core.compute_basin_hour(cfg, hours, T10)
        self.assertEqual(r["status"], "incomplete")
        self.assertIsNone(r["rainfall_mm"])
        self.assertEqual(r["unavailable_stations"], ["S2"])
        # 不按其余站点重新分配权重：S1 贡献仍按 0.4，且面雨量不可供数
        self.assertEqual(r["members"][0]["weight_ppm"], 400_000)
        self.assertEqual(r["members"][0]["contribution_mm"], 4.8)
        self.assertIsNone(r["members"][1]["contribution_mm"])

    def test_missing_member_result_treated_as_unavailable(self):
        cfg = core.parse_basin_config("B", [("S1", "0.5"), ("S2", "0.5")])
        r = core.compute_basin_hour(cfg, {"S1": self._hour(1)}, T10)
        self.assertEqual(r["status"], "incomplete")
        self.assertEqual(r["unavailable_stations"], ["S2"])


class BasinDbTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "basin.db")
        self.conn = db.connect(self.path)
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def _config(self, name="B", weights=(("S1", "0.4"), ("S2", "0.6"))):
        return core.parse_basin_config(name, list(weights))

    def test_save_and_version_bump(self):
        first = db.save_basin(self.conn, self._config())
        self.assertEqual(first["version"], 1)
        cfg, version, _ = db.get_basin(self.conn, "B")
        self.assertEqual(version, 1)
        self.assertEqual([m.station for m in cfg.members], ["S1", "S2"])

        repl = self._config(weights=(("S2", "0.5"), ("S3", "0.5")))
        second = db.save_basin(self.conn, repl)
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["basin_id"], first["basin_id"])
        cfg, version, _ = db.get_basin(self.conn, "B")
        self.assertEqual(version, 2)
        self.assertEqual([m.station for m in cfg.members], ["S2", "S3"])

    def test_failed_save_keeps_old_definition(self):
        db.save_basin(self.conn, self._config())
        # 绕过校验层直接构造非法成员，触发 CHECK 约束失败 -> 整体回滚
        bad = core.BasinConfig("B", (core.BasinMember("S9", 0),))
        with self.assertRaises(sqlite3.Error):
            db.save_basin(self.conn, bad)
        cfg, version, _ = db.get_basin(self.conn, "B")
        self.assertEqual(version, 1)
        self.assertEqual([m.station for m in cfg.members], ["S1", "S2"])

    def test_snapshot_is_consistent_under_concurrent_write(self):
        """查询进行中插入新配置与新读数提交，快照仍取同一已提交状态。"""
        db.save_basin(self.conn, self._config(weights=(("S1", "0.4"), ("S2", "0.6"))))
        for s in ("S1", "S2"):
            db.apply_batch(self.conn, [
                (i + 1, "raw", core.Reading(s, "primary", T10 + i * 60, 1000, 1))
                for i in range(61)
            ], [], "old.csv")

        # 另一个连接：在查询中途提交“新配置 + 同批补传（两站都改）”
        other = db.connect(self.path)
        try:
            injected = {"done": False}
            real_fetch = db.fetch_hour_counters

            def hooked(conn, station, hour):
                result = real_fetch(conn, station, hour)
                if not injected["done"]:
                    injected["done"] = True
                    db.save_basin(other, core.parse_basin_config(
                        "B", [("S1", "0.3"), ("S2", "0.7")]
                    ))
                    for s in ("S1", "S2"):
                        db.apply_batch(other, [
                            (i + 1, "raw", core.Reading(s, "primary", T10 + i * 60, 5000, 2))
                            for i in range(61)
                        ], [], "new.csv")
                return result

            original = db.fetch_hour_counters
            db.fetch_hour_counters = hooked
            try:
                snap = db.fetch_basin_hour_snapshot(self.conn, "B", T10)
            finally:
                db.fetch_hour_counters = original

            # 快照必须是旧配置（S1/S2 各 0.5），且两站读数都取补传前的值，
            # 绝不能出现 S1 取旧值、S2 取补传后新值的混用
            self.assertTrue(injected["done"])
            self.assertEqual([m.station for m in snap["basin"].members], ["S1", "S2"])
            self.assertEqual(snap["version"], 1)
            self.assertEqual([m.weight_ppm for m in snap["basin"].members],
                             [400_000, 600_000])
            for s in ("S1", "S2"):
                self.assertEqual(snap["counters"][s][("primary", T10)], 1000)
        finally:
            other.close()

        # 之后的查询看到完整的新状态
        snap = db.fetch_basin_hour_snapshot(self.conn, "B", T10)
        self.assertEqual([m.weight_ppm for m in snap["basin"].members], [300_000, 700_000])
        self.assertEqual(snap["version"], 2)
        for s in ("S1", "S2"):
            self.assertEqual(snap["counters"][s][("primary", T10)], 5000)


class BasinAppTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "basinapp.db")
        self.app = create_app(self.path)
        self.client = self.app.test_client()

    def _upload(self, text):
        return self.client.post("/api/upload", data={"csv_text": text})

    def _save(self, name, members):
        return self.client.post("/api/basins", json={
            "name": name,
            "members": [{"station": s, "weight": w} for s, w in members],
        })

    def _basin_hour(self, name, hour_text="2026-11-17T10:00Z"):
        return self.client.get(f"/api/basin_hourly?basin={name}&hour={hour_text}")

    def _station_hours(self, stations, pulses_per_minute=1, start=T10):
        for s in stations:
            text = "\n".join(csv_rows(
                s, "primary", start, [1000 + pulses_per_minute * i for i in range(61)]
            ))
            self.assertEqual(self._upload(text).get_json()["accepted_count"], 61)

    def test_save_and_query_web_matches_api(self):
        self._station_hours(["S1", "S2"])
        resp = self._save("B1", [("S1", "0.4"), ("S2", "0.6")])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["version"], 1)

        r = self._basin_hour("B1").get_json()
        # 两站均为 60 脉冲 = 12.0 mm；加权：12×0.4 + 12×0.6 = 12.0
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["rainfall_mm"], 12.0)
        self.assertEqual([m["contribution_mm"] for m in r["members"]], [4.8, 7.2])
        self.assertEqual([m["weight_text"] for m in r["members"]], ["0.4", "0.6"])

        html = self.client.get("/basin?name=B1&hour=2026-11-17T10:00Z").get_data(as_text=True)
        self.assertIn("12.00 mm", html)
        self.assertIn("4.80", html)
        self.assertIn("7.20", html)
        self.assertIn("可供数", html)

    def test_different_weights_change_weighted_total(self):
        self._station_hours(["S1", "S2"])
        self._save("B1", [("S1", "0.75"), ("S2", "0.25")])
        r = self._basin_hour("B1").get_json()
        self.assertEqual([m["contribution_mm"] for m in r["members"]], [9.0, 3.0])
        self.assertEqual(r["rainfall_mm"], 12.0)

    def test_any_member_unavailable_makes_basin_unavailable(self):
        # S1 完整；S2 缺 10:45 -> 该小时不可供数
        self._station_hours(["S1"])
        lines = csv_rows("S2", "primary", T10, [1000 + 10 * i for i in range(61)])
        del lines[45]
        self._upload("\n".join(lines))
        self._save("B1", [("S1", "0.4"), ("S2", "0.6")])

        r = self._basin_hour("B1").get_json()
        self.assertEqual(r["status"], "incomplete")
        self.assertIsNone(r["rainfall_mm"])
        self.assertEqual(r["unavailable_stations"], ["S2"])
        self.assertEqual(r["members"][0]["weight_ppm"], 400_000)   # 权重未重分配
        self.assertEqual(r["members"][0]["contribution_mm"], 4.8)
        html = self.client.get("/basin?name=B1&hour=2026-11-17T10:00Z").get_data(as_text=True)
        self.assertIn("不可供数", html)
        self.assertIn("不按其余站点重新分配权重", html)

    def test_backup_fallback_and_revision_rule_carry_over(self):
        # 主计缺 10:30 两端，备计补齐 -> 成员站仍可供数，流域也供数
        primary = csv_rows("S1", "primary", T10, [1000 + i for i in range(61)])
        del primary[30]           # 缺 10:30，影响 29~30 与 30~31 两段
        backup = csv_rows("S1", "backup", T10, [500 + i for i in range(61)])
        self._upload("\n".join(primary))
        self._upload("\n".join(backup))
        self._station_hours(["S2"])
        self._save("B1", [("S1", "0.5"), ("S2", "0.5")])
        r = self._basin_hour("B1").get_json()
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["members"][0]["station_rainfall_mm"], 12.0)

    def test_save_failure_keeps_old_definition(self):
        self._station_hours(["S1", "S2"])
        self._save("B1", [("S1", "0.4"), ("S2", "0.6")])
        before = self._basin_hour("B1").get_json()

        for bad_members in (
            [("S1", "0.4"), ("S2", "0.5")],          # 合计 0.9
            [("S1", "1"), ("S2", "0")],              # 权重 0
            [("S1", "0.5")],                         # 只有 1 站
            [("S1", "0.5"), ("S1", "0.5")],          # 重复站点
        ):
            resp = self._save("B1", bad_members)
            self.assertEqual(resp.status_code, 400)
            self.assertIn("error", resp.get_json())

        after = self._basin_hour("B1").get_json()
        self.assertEqual(before, after)
        self.assertEqual(after["version"], 1)

        # 网页表单保存失败同样不改旧定义
        resp = self.client.post("/basins", data={
            "name": "B1", "station": ["S1", "S2"], "weight": ["0.2", "0.2"],
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("旧定义未改变", resp.get_data(as_text=True))
        self.assertEqual(self._basin_hour("B1").get_json(), before)

    def test_new_config_applies_to_adjacent_hours(self):
        # 09:00~11:00 连续读数，每分钟 +10 脉冲（每小时 120.0 mm）
        for s in ("S1", "S2"):
            text = "\n".join(csv_rows(
                s, "primary", T09, [1000 + 10 * i for i in range(121)]
            ))
            self._upload(text)
        self._save("B1", [("S1", "0.5"), ("S2", "0.5")])
        self.assertEqual(self._basin_hour("B1", "2026-11-17T09:00Z").get_json()["rainfall_mm"], 120.0)
        self.assertEqual(self._basin_hour("B1", "2026-11-17T10:00Z").get_json()["rainfall_mm"], 120.0)

        # 修订整点 10:00 读数（1600 -> 1595）：该端点是 09 小时的末端点、
        # 也是 10 小时的首端点，前后两小时都应更新
        self._upload(f"S1,primary,{core.iso_utc(T10)},1595,2")
        self._upload(f"S2,primary,{core.iso_utc(T10)},1595,2")
        r09 = self._basin_hour("B1", "2026-11-17T09:00Z").get_json()
        r10 = self._basin_hour("B1", "2026-11-17T10:00Z").get_json()
        # 09 小时末段少 5 脉冲 → 119.0 mm；10 小时首段多 5 脉冲 → 121.0 mm
        self.assertEqual(r09["rainfall_mm"], 119.0)
        self.assertEqual(r10["rainfall_mm"], 121.0)
        h09 = self.client.get("/basin?name=B1&hour=2026-11-17T09:00Z").get_data(as_text=True)
        h10 = self.client.get("/basin?name=B1&hour=2026-11-17T10:00Z").get_data(as_text=True)
        self.assertIn("119.00 mm", h09)
        self.assertIn("121.00 mm", h10)

    def test_unconfigured_basin_returns_404(self):
        self.assertEqual(self._basin_hour("NOPE").status_code, 404)
        resp = self.client.get("/basin?name=NOPE&hour=2026-11-17T10:00Z")
        self.assertEqual(resp.status_code, 302)

    def test_basin_list_api_and_validation(self):
        self._save("B1", [("S1", "0.4"), ("S2", "0.6")])
        out = self.client.get("/api/basins").get_json()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "B1")
        self.assertEqual([m["weight_text"] for m in out[0]["members"]], ["0.4", "0.6"])
        # 非法请求体
        self.assertEqual(self.client.post("/api/basins", json={}).status_code, 400)
        self.assertEqual(self._basin_hour("B1", "2026-11-17T10:30Z").status_code, 400)
        self.assertEqual(self.client.get("/api/basin_hourly?basin=B1").status_code, 400)
        self.assertEqual(self.client.get("/api/basin_hourly?hour=2026-11-17T10:00Z").status_code, 400)


if __name__ == "__main__":
    unittest.main()
