"""Chunk exporter for Assetto Corsa logs: load clean.csv, pick chunks, export.

Run:  .venv/bin/python app/app.py   then open http://127.0.0.1:5055
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.api.types import is_numeric_dtype
from flask import Flask, jsonify, request, send_file, send_from_directory

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent                      # folder that contains the log folders
EXPORT_ROOT = ROOT / "exported"
CONFIG_PATH = ROOT / "config.json"          # column mapping, next to exported/
CHUNKS_PATH = ROOT / "chunks.json"          # {folder_name: [{start, end}]}, next to exported/
PLOT_COLS = ["sim_time", "throttle_ctrl", "brake_ctrl", "delta_ctrl", "vx", "vy",
             "ax", "ay", "psi_dot", "x", "y"]

app = Flask(__name__, static_folder=str(APP_DIR / "static"), static_url_path="/static")
_cache = {}                                # folder -> DataFrame


def folder_from(value):
    p = Path(value).expanduser().resolve()
    if not (p / "clean.csv").is_file():
        raise ValueError(f"{p} has no clean.csv")
    return p


def load_df(folder):
    key = str(folder)
    mtime = (folder / "clean.csv").stat().st_mtime
    if key not in _cache or _cache[key][0] != mtime:
        _cache[key] = (mtime, pd.read_csv(folder / "clean.csv"))
    return _cache[key][1]


def find_video(folder):
    vids = sorted(folder.glob("*.mp4"))
    return vids[0] if vids else None


def video_offset(folder, df):
    """video_time = sim_time + offset, from metadata wall-clock timestamps."""
    try:
        meta = json.loads((folder / "metadata.json").read_text())
        start = meta["video"]["video_start_wall_time"]
        return float(df["time"].iloc[0] - df["sim_time"].iloc[0] - start)
    except Exception:
        return 0.0


@app.errorhandler(ValueError)
def bad(e):
    return jsonify(error=str(e)), 400


@app.get("/")
def index():
    return send_from_directory(APP_DIR / "static", "index.html")


@app.get("/api/config")
def get_config():
    try:
        return jsonify(json.loads(CONFIG_PATH.read_text()))
    except (FileNotFoundError, json.JSONDecodeError):
        return jsonify({})


@app.post("/api/config")
def set_config():
    body = request.get_json()
    cfg = {"dropped": sorted(set(map(str, body.get("dropped", [])))),
           "rename": {str(k): str(v) for k, v in body.get("rename", {}).items() if v}}
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
    return jsonify(cfg)


def read_chunks():
    try:
        return json.loads(CHUNKS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


@app.get("/api/chunks")
def get_chunks():
    folder = folder_from(request.args["folder"])
    return jsonify(chunks=read_chunks().get(folder.name))      # null if never saved


@app.post("/api/chunks")
def set_chunks():
    body = request.get_json()
    folder = folder_from(body["folder"])
    chunks = [{"start": float(c["start"]), "end": float(c["end"])} for c in body["chunks"]]
    allc = read_chunks()
    allc[folder.name] = chunks
    tmp = CHUNKS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(dict(sorted(allc.items())), indent=1) + "\n")
    tmp.replace(CHUNKS_PATH)
    return jsonify(chunks=chunks)


@app.get("/api/folders")
def folders():
    found = sorted(str(p.parent) for p in ROOT.glob("*/clean.csv"))
    return jsonify(root=str(ROOT), folders=found)


@app.get("/api/browse")
def browse():
    """Native folder picker (macOS). Returns the chosen absolute path, or '' if cancelled."""
    start = Path(request.args.get("start", "")).expanduser()
    start = start.parent if start.is_file() else start
    if not start.is_dir():
        start = ROOT
    script = ('POSIX path of (choose folder with prompt "Select a log folder (contains clean.csv)" '
              f'default location POSIX file "{start}")')
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        return jsonify(error="native folder dialog needs macOS (osascript); paste the path instead")
    if r.returncode != 0:
        return jsonify(path="")            # cancelled
    return jsonify(path=r.stdout.strip().rstrip("/"))


def series_of(df, cols):
    return {c: [v if np.isfinite(v) else None for v in df[c].astype(float).tolist()]
            for c in cols if c in df}


@app.get("/api/series")
def series():
    """Arbitrary numeric columns of a folder's clean.csv, for the plot picker."""
    folder = folder_from(request.args["folder"])
    df = load_df(folder)
    cols = [c for c in request.args.get("cols", "").split(",") if c in df and is_numeric_dtype(df[c])]
    return jsonify(series=series_of(df, cols))


@app.get("/api/load")
def load():
    folder = folder_from(request.args["folder"])
    df = load_df(folder)
    cols = df.columns.tolist()
    numeric = [c for c in cols if is_numeric_dtype(df[c])]
    series = series_of(df, PLOT_COLS)
    vid = find_video(folder)
    return jsonify(folder=str(folder), name=folder.name, columns=cols, numeric=numeric, rows=len(df),
                   series=series, has_video=vid is not None,
                   video_offset=video_offset(folder, df),
                   export_dir=str(EXPORT_ROOT / folder.name))


@app.get("/api/video")
def video():
    folder = folder_from(request.args["folder"])
    vid = find_video(folder)
    if vid is None:
        return ("no video", 404)
    return send_file(vid, mimetype="video/mp4", conditional=True)   # supports Range


@app.post("/api/export")
def export():
    body = request.get_json()
    folder = folder_from(body["folder"])
    df = load_df(folder)
    columns = body["columns"]              # [{name, rename}] in output order
    chunks = body["chunks"]                # [{start, end}] in sim_time seconds
    if not columns:
        raise ValueError("no columns selected")
    if not chunks:
        raise ValueError("no chunks defined")
    out_names = [c.get("rename") or c["name"] for c in columns]
    dup = {n for n in out_names if out_names.count(n) > 1}
    if dup:
        raise ValueError(f"duplicate output column names: {sorted(dup)}")
    missing = [c["name"] for c in columns if c["name"] not in df]
    if missing:
        raise ValueError(f"unknown columns: {missing}")

    out_dir = EXPORT_ROOT / folder.name
    out_dir.mkdir(parents=True, exist_ok=True)
    stale = re.compile(rf"(chunk_\d+|c\d+_{re.escape(folder.name)})\.csv")
    for old in out_dir.glob("*.csv"):                # drop stale chunks from earlier exports (old and new naming)
        if stale.fullmatch(old.name):
            old.unlink()

    sub = df[[c["name"] for c in columns]]
    sub.columns = out_names
    written = []
    for i, ch in enumerate(chunks, 1):
        lo, hi = sorted((float(ch["start"]), float(ch["end"])))
        mask = (df["sim_time"] >= lo) & (df["sim_time"] <= hi)
        path = out_dir / f"c{i}_{folder.name}.csv"
        sub[mask].to_csv(path, index=False)
        written.append({"file": path.name, "rows": int(mask.sum())})
    return jsonify(dir=str(out_dir), written=written)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5055
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
