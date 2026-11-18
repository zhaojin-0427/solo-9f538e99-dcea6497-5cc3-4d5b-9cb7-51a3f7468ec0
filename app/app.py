"""Flask 网页与查询 API。

网页展示与 API 使用同一个 compute_hour_result() / compute_basin_result()，
保证两边结果一致；小时雨量由 readings 表现算现算，补传/修订整点读数提交后，
前后两小时立即同时反映新值，不存在缓存不一致。

流域查询在单个读事务中同时读取流域定义与该小时全部成员站读数，
因此配置保存与 CSV 补传交错发生时，也不会混用同批补传前后的读数。
"""
from __future__ import annotations

import os

from flask import (
    Flask, flash, g, jsonify, redirect, render_template, request, url_for,
)

from . import core, db


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path or os.environ.get("RAIN_DB", "rainfall.db")
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 上传上限 16MB
    app.secret_key = os.environ.get("SECRET_KEY", os.urandom(32))

    # 启动时建库建表（幂等），保证重启后可直接查询
    conn = db.connect(app.config["DB_PATH"])
    try:
        db.init_db(conn)
    finally:
        conn.close()

    def get_conn() -> db.sqlite3.Connection:
        if "db_conn" not in g:
            g.db_conn = db.connect(app.config["DB_PATH"])
        return g.db_conn

    @app.teardown_appcontext
    def _close_conn(exc):
        conn = g.pop("db_conn", None)
        if conn is not None:
            conn.close()

    @app.template_filter("iso")
    def _iso_filter(epoch):
        return core.iso_utc(int(epoch))

    def compute_hour_result(station: str, hour_epoch: int) -> dict:
        """网页与 API 共用的小时计算入口。"""
        counters = db.fetch_hour_counters(get_conn(), station, hour_epoch)
        return core.compute_hour(counters, station, hour_epoch)

    def compute_basin_result(name: str, hour_epoch: int) -> dict | None:
        """网页与 API 共用的流域计算入口。

        流域定义与全部成员站读数取自同一读事务（同一已提交状态）；
        返回 None 表示流域未配置。
        """
        snapshot = db.fetch_basin_hour_snapshot(get_conn(), name, hour_epoch)
        if snapshot is None:
            return None
        member_hours = {
            station: core.compute_hour(counters, station, hour_epoch)
            for station, counters in snapshot["counters"].items()
        }
        result = core.compute_basin_hour(
            snapshot["basin"], member_hours, hour_epoch
        )
        result["version"] = snapshot["version"]
        result["config_updated_at"] = snapshot["updated_at"]
        return result

    def read_basin_form():
        """从表单读取名称与成员行；完全空白的成员行忽略。"""
        name = (request.form.get("name") or "").strip()
        stations = request.form.getlist("station")
        weights = request.form.getlist("weight")
        members = [
            ((s or "").strip(), (w or "").strip())
            for s, w in zip(stations, weights)
            if (s or "").strip() or (w or "").strip()
        ]
        return name, members

    def basin_form_state(name: str, members):
        """把校验失败的输入回填到表单，便于数据员直接修改。"""
        rows = [{"station": s, "weight": w} for s, w in members]
        while len(rows) < core.MIN_BASIN_STATIONS:
            rows.append({"station": "", "weight": ""})
        return {"name": name, "rows": rows}

    def existing_basin_rows(name: str):
        conn = get_conn()
        basin, version, _ = db.get_basin(conn, name)
        if basin is None:
            return None
        return {
            "name": basin.name,
            "version": version,
            "rows": [{
                "station": m.station,
                "weight": core.format_weight(m.weight_ppm),
            } for m in basin.members],
        }

    def read_upload_text():
        """从 multipart 文件或文本框读取 CSV 文本，返回 (text, filename)。"""
        file = request.files.get("file")
        if file is not None and file.filename:
            try:
                return file.read().decode("utf-8-sig"), file.filename
            except UnicodeDecodeError:
                raise core.CsvFormatError("文件不是有效的 UTF-8 编码")
        text = request.form.get("csv_text", "")
        if text.strip():
            return text, None
        raise core.CsvFormatError("请选择 CSV 文件或粘贴 CSV 文本")

    # ---------------- 网页 ----------------

    @app.get("/")
    def index():
        conn = get_conn()
        return render_template(
            "index.html",
            stations=db.list_stations(conn),
            batches=db.list_batches(conn),
            basins=db.list_basins(conn),
        )

    @app.post("/upload")
    def upload_html():
        try:
            text, filename = read_upload_text()
            valid, errors = core.parse_csv_text(text)
        except core.CsvFormatError as exc:
            flash(f"上传失败：{exc}", "error")
            return redirect(url_for("index"))
        report = db.apply_batch(get_conn(), valid, errors, filename)
        return render_template("batch.html", report=report)

    @app.get("/report")
    def report_html():
        station = request.args.get("station", "").strip()
        hour_text = request.args.get("hour", "").strip()
        if not station or not hour_text:
            flash("请填写站点和整点小时", "error")
            return redirect(url_for("index"))
        try:
            hour = core.parse_hour(hour_text)
        except ValueError as exc:
            flash(f"小时参数无效：{exc}", "error")
            return redirect(url_for("index"))
        result = compute_hour_result(station, hour)
        return render_template(
            "report.html",
            r=result,
            prev_hour=core.iso_utc(hour - core.HOUR_SECONDS),
            next_hour=core.iso_utc(hour + core.HOUR_SECONDS),
        )

    @app.get("/basins")
    def basins_html():
        """流域配置页：列表 + 保存表单（?name= 编辑已有流域）。"""
        edit_name = request.args.get("name", "").strip()
        existing = existing_basin_rows(edit_name) if edit_name else None
        form = existing or basin_form_state(edit_name, [])
        return render_template(
            "basins.html",
            basins=db.list_basins(get_conn()),
            form=form,
            editing=existing is not None,
            max_members=core.MAX_BASIN_STATIONS,
            min_members=core.MIN_BASIN_STATIONS,
        )

    @app.post("/basins")
    def save_basin_html():
        """保存流域定义。校验失败时不写入任何内容，旧定义保持不变。"""
        name, members = read_basin_form()
        try:
            config = core.parse_basin_config(name, members)
        except core.BasinConfigError as exc:
            flash(f"保存失败：{exc}（旧定义未改变）", "error")
            return render_template(
                "basins.html",
                basins=db.list_basins(get_conn()),
                form=basin_form_state(name, members),
                editing=False,
                max_members=core.MAX_BASIN_STATIONS,
                min_members=core.MIN_BASIN_STATIONS,
            ), 400
        saved = db.save_basin(get_conn(), config)
        flash(
            f"已保存流域 {saved['name']}：{len(config.members)} 个站点，"
            f"权重合计 1（版本 {saved['version']}）",
            "info",
        )
        return redirect(url_for("basins_html", name=saved["name"]))

    @app.get("/basin")
    def basin_hour_html():
        name = request.args.get("name", "").strip()
        hour_text = request.args.get("hour", "").strip()
        if not name or not hour_text:
            flash("请填写流域名称和整点小时", "error")
            return redirect(url_for("basins_html"))
        try:
            hour = core.parse_hour(hour_text)
        except ValueError as exc:
            flash(f"小时参数无效：{exc}", "error")
            return redirect(url_for("basins_html", name=name))
        result = compute_basin_result(name, hour)
        if result is None:
            flash(f"流域未配置：{name}", "error")
            return redirect(url_for("basins_html"))
        return render_template(
            "basin_report.html",
            r=result,
            prev_hour=core.iso_utc(hour - core.HOUR_SECONDS),
            next_hour=core.iso_utc(hour + core.HOUR_SECONDS),
        )

    @app.get("/batches/<int:batch_id>")
    def batch_detail(batch_id):
        batch, rows = db.get_batch(get_conn(), batch_id)
        if batch is None:
            flash("批次不存在", "error")
            return redirect(url_for("index"))
        return render_template("batch_detail.html", batch=batch, rows=rows)

    # ---------------- API（与网页同一计算入口，结果一致） ----------------

    @app.post("/api/upload")
    def upload_api():
        try:
            text, filename = read_upload_text()
            valid, errors = core.parse_csv_text(text)
        except core.CsvFormatError as exc:
            return jsonify({"error": str(exc)}), 400
        report = db.apply_batch(get_conn(), valid, errors, filename)
        return jsonify(_report_json(report))

    @app.get("/api/hourly")
    def hourly_api():
        station = request.args.get("station", "").strip()
        if not station:
            return jsonify({"error": "缺少 station 参数"}), 400
        try:
            hour = core.parse_hour(request.args.get("hour", ""))
        except ValueError as exc:
            return jsonify({"error": f"hour 参数无效: {exc}"}), 400
        return jsonify(compute_hour_result(station, hour))

    @app.get("/api/basins")
    def basins_api():
        conn = get_conn()
        out = []
        for row in db.list_basins(conn):
            basin, version, updated_at = db.get_basin(conn, row["name"])
            out.append({
                "name": basin.name,
                "version": version,
                "updated_at": updated_at,
                "members": [{
                    "station": m.station,
                    "weight": m.weight_ppm / core.WEIGHT_SCALE,
                    "weight_text": core.format_weight(m.weight_ppm),
                } for m in basin.members],
            })
        return jsonify(out)

    @app.post("/api/basins")
    def save_basin_api():
        """JSON 保存流域定义；请求体为 {name, members:[{station, weight}]}。"""
        payload = request.get_json(silent=True) or {}
        name = payload.get("name") or ""
        raw_members = payload.get("members") or []
        members = []
        if isinstance(raw_members, list):
            for item in raw_members:
                if isinstance(item, dict):
                    members.append((item.get("station"), item.get("weight")))
        try:
            config = core.parse_basin_config(name, members)
        except core.BasinConfigError as exc:
            return jsonify({"error": str(exc)}), 400
        saved = db.save_basin(get_conn(), config)
        return jsonify({
            "basin_id": saved["basin_id"],
            "name": saved["name"],
            "version": saved["version"],
            "updated_at": saved["updated_at"],
            "members": [{
                "station": m.station,
                "weight": m.weight_ppm / core.WEIGHT_SCALE,
                "weight_text": core.format_weight(m.weight_ppm),
            } for m in config.members],
        })

    @app.get("/api/basin_hourly")
    def basin_hourly_api():
        name = request.args.get("basin", "").strip()
        if not name:
            return jsonify({"error": "缺少 basin 参数"}), 400
        try:
            hour = core.parse_hour(request.args.get("hour", ""))
        except ValueError as exc:
            return jsonify({"error": f"hour 参数无效: {exc}"}), 400
        result = compute_basin_result(name, hour)
        if result is None:
            return jsonify({"error": f"流域未配置: {name}"}), 404
        return jsonify(result)

    @app.get("/api/stations")
    def stations_api():
        out = [{
            "station": r["station"],
            "readings": r["n"],
            "first_minute": core.iso_utc(r["first_minute"]),
            "last_minute": core.iso_utc(r["last_minute"]),
        } for r in db.list_stations(get_conn())]
        return jsonify(out)

    @app.get("/api/batches")
    def batches_api():
        return jsonify([dict(r) for r in db.list_batches(get_conn(), limit=100)])

    @app.get("/api/health")
    def health():
        get_conn().execute("SELECT 1")
        return jsonify({"status": "ok"})

    @app.errorhandler(413)
    def too_large(_):
        return "上传文件过大（上限 16MB）", 413

    return app


def _report_json(report: dict) -> dict:
    return {
        "batch_id": report["batch_id"],
        "filename": report["filename"],
        "uploaded_at": report["uploaded_at"],
        "total_rows": report["total_rows"],
        "accepted_count": report["accepted_count"],
        "rejected_count": report["rejected_count"],
        "accepted": [{
            "row_no": a["row_no"],
            "action": a["action"],
            "station": a["reading"].station,
            "device": a["reading"].device,
            "minute": core.iso_utc(a["reading"].minute_epoch),
            "counter": a["reading"].counter,
            "revision": a["reading"].revision,
        } for a in report["accepted"]],
        "rejected": report["rejected"],
    }


def main():
    app = create_app()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), threaded=True)


if __name__ == "__main__":
    main()
