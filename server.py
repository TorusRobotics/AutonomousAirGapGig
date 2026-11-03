#!/usr/bin/env python3
"""
VESC Auto Airgap — database server
Flask + SQLite. Stores every adjustment session in airgap.db.

Run:
    pip install flask flask-cors
    python server.py
Then in index.html set API URL to:  http://localhost:5000/api
(or http://<this-pc-ip>:5000/api from other machines on the LAN)
"""

import os
import sqlite3
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from flask_cors import CORS

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "airgap.db")

# Column name -> SQLite type. This is the single source of truth for the schema.
COLUMNS = {
    "motor_no":                 "TEXT NOT NULL",
    "motor_notes":              "TEXT",
    "record_type":              "TEXT DEFAULT 'auto_tune'",
    "timestamp":                "TEXT",
    "tach_cumulative":          "INTEGER",
    "tach_delta":               "INTEGER",
    "direction":                "TEXT",
    "resistance_mohm":          "REAL",
    "inductance_uh":            "REAL",
    "flux_linkage_initial_mwb": "REAL",
    "flux_initial_ts":          "TEXT",
    "flux_linkage_final_mwb":   "REAL",
    "flux_final_ts":            "TEXT",
    "flux_delta_mwb":           "REAL",
    "flux_linkage_mwb":         "REAL",
    "tach_moves":               "TEXT",   # JSON string
    "auto_iterations":          "INTEGER",
    "fine_iterations":          "INTEGER",
    "converged_in_3":           "INTEGER",
    "initial_flux_mwb":         "REAL",
    "final_flux_mwb":           "REAL",
    "flux_error_final":         "REAL",
    "k_est_final":              "REAL",
    "step_history":             "TEXT",   # JSON string
    "overshoots":               "INTEGER",
    "stick_slips":              "INTEGER",
    "elapsed_sec":              "REAL",
    "verify_flux_mwb":          "REAL",
    "result":                   "TEXT",
    "target_flux_mwb":          "REAL",
    "band_mwb":                 "REAL",
    "rework_count":             "INTEGER DEFAULT 1",
}

app = Flask(__name__)
CORS(app)  # allow the HTML file (any origin) to call this API


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    cols_sql = ",\n  ".join(f"{name} {typ}" for name, typ in COLUMNS.items())
    with get_db() as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS motor_records (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              {cols_sql}
            )
            """
        )
        # migration: add any columns missing from an older airgap.db
        have = {r["name"] for r in conn.execute("PRAGMA table_info(motor_records)")}
        for name, typ in COLUMNS.items():
            if name not in have and "NOT NULL" not in typ:
                try:
                    conn.execute(f"ALTER TABLE motor_records ADD COLUMN {name} {typ}")
                    print(f"[db] migrated: added column {name}")
                except Exception as e:
                    print(f"[db] migrate skip {name}: {e}")
        conn.commit()
    print(f"[db] ready at {DB_PATH}")


def row_to_dict(row):
    import json
    d = dict(row)
    # decode JSON-stored columns back into real arrays/objects for the UI
    for jc in ("tach_moves", "step_history"):
        if isinstance(d.get(jc), str):
            try:
                d[jc] = json.loads(d[jc])
            except Exception:
                pass
    return d


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "db": DB_PATH})


@app.get("/api/records")
def get_records():
    """All records, newest first. Optional ?motor_no= and ?result= filters."""
    where, params = [], []
    if request.args.get("motor_no"):
        where.append("motor_no LIKE ?")
        params.append(f"%{request.args['motor_no']}%")
    if request.args.get("result"):
        where.append("result = ?")
        params.append(request.args["result"])
    sql = "SELECT * FROM motor_records"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return jsonify([row_to_dict(r) for r in rows])


@app.post("/api/record")
def add_record():
    """Upsert by (motor_no, record_type): same motor -> same row, updated in place.
    A new motor creates a new row. Each rework bumps rework_count."""
    import json
    data = request.get_json(force=True) or {}
    if not data.get("timestamp"):
        data["timestamp"] = datetime.now(timezone.utc).isoformat()

    motor_no = data.get("motor_no")
    rtype = data.get("record_type") or "auto_tune"

    def conv(name, v):
        if name in ("tach_moves", "step_history") and not isinstance(v, (str, type(None))):
            return json.dumps(v)
        if isinstance(v, bool):
            return 1 if v else 0
        return v

    with get_db() as conn:
        existing = conn.execute(
            "SELECT id, rework_count FROM motor_records WHERE motor_no = ? AND record_type = ?",
            (motor_no, rtype),
        ).fetchone()

        if existing:
            # ── update the SAME row ──
            rid = existing["id"]
            new_count = (existing["rework_count"] or 1) + 1
            sets, vals = [], []
            for name in COLUMNS:
                if name in data and name != "rework_count":
                    sets.append(f"{name} = ?")
                    vals.append(conv(name, data[name]))
            sets.append("rework_count = ?")
            vals.append(new_count)
            vals.append(rid)
            conn.execute(f"UPDATE motor_records SET {', '.join(sets)} WHERE id = ?", vals)
            conn.commit()
            row = conn.execute("SELECT * FROM motor_records WHERE id = ?", (rid,)).fetchone()
            return jsonify(row_to_dict(row)), 200

        # ── insert a NEW row (new motor) ──
        fields, values = [], []
        for name in COLUMNS:
            if name in data:
                fields.append(name)
                values.append(conv(name, data[name]))
        if "record_type" not in fields:
            fields.append("record_type"); values.append(rtype)
        if "rework_count" not in fields:
            fields.append("rework_count"); values.append(1)
        placeholders = ", ".join("?" for _ in fields)
        cur = conn.execute(
            f"INSERT INTO motor_records ({', '.join(fields)}) VALUES ({placeholders})", values
        )
        conn.commit()
        row = conn.execute("SELECT * FROM motor_records WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(row_to_dict(row)), 201


@app.delete("/api/record/<int:rec_id>")
def delete_record(rec_id):
    with get_db() as conn:
        conn.execute("DELETE FROM motor_records WHERE id = ?", (rec_id,))
        conn.commit()
    return jsonify({"deleted": rec_id})


@app.delete("/api/records")
def clear_records():
    with get_db() as conn:
        conn.execute("DELETE FROM motor_records")
        conn.commit()
    return jsonify({"cleared": True})


if __name__ == "__main__":
    init_db()
    # host=0.0.0.0 so other PCs on the network can reach it by IP
    app.run(host="0.0.0.0", port=5000, debug=False)