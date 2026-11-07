#!/usr/bin/env python3
r"""
把 storydata_spine_full_*.unity3d（Spine 角色立繪）打包成 HTML 檢視器。

特色：
    - 預設 lazy 分檔：viewer HTML 很小，角色資料按需要才載入
    - 左側控制欄：角色切換、表情（skin）、眼睛 / 嘴巴動畫
    - 匯出目前畫面 PNG（保留透明通道）、批量匯出所有表情 ZIP
    - 批量 ZIP 每種表情會包含開眼/閉眼 × 開口/閉口的 4 種組合
    - 角色有 normal 時自動隱藏／不輸出 default（原始）
    - --export-all 可用指令一次匯出所有角色，不需要手動開 HTML
    - 這批 atlas 貼圖是預乘 alpha（PMA），用 PMA 混合才不會讓臉紅變黑

用法：
    python spine_viewer.py --bundle a
    python spine_viewer.py --bundle a --out "C:\viewers\priconne_spine.html"
    python spine_viewer.py --bundle "test2\storydata_spine_full_105934.unity3d"
    python spine_viewer.py --bundle test          # 下載器的 hash 檔名（含 manifest.db）
    python spine_viewer.py --bundle a --names names.json
    python spine_viewer.py --bundle a --single    # 舊版：全部內嵌成一個大 HTML
    python spine_viewer.py --bundle a --export-all
    python spine_viewer.py --bundle a --export-all --export-out "C:\viewers\all_characters.zip"
    python spine_viewer.py --bundle a --since 261031   # 只處理 2026-10-31 00:00:00 之後的檔案

預設輸出：
    <out>.html
    <out>_assets\chars\<角色ID>.js

--export-all 會用 headless Chrome 自動匯出所有角色：
    <viewer檔名>_all_<日期>.zip（例如 spine_viewer_all_261031.zip）
內容是每個角色一個資料夾，每個 skin 通常有 4 張：
    <id>_<skin>_eye_open_mouth_open.png
    <id>_<skin>_eye_open_mouth_close.png
    <id>_<skin>_eye_close_mouth_open.png
    <id>_<skin>_eye_close_mouth_close.png
角色有 normal 時，default 不顯示也不輸出；同一角色內 CRC32 與內容都相同的圖
只保留一張，重複時優先 normal，其次其他非 default，最後才是 default；
同 skin 時 eye_open 優先於 eye_close，mouth_open 優先於 mouth_close。
這個模式需要 Playwright（pip install playwright）與系統 Chrome / Edge。

500 個角色請用預設 lazy 模式；--single 只適合角色數量少的情況。
lazy 模式的 HTML 必須和 <out>_assets 資料夾一起移動，否則角色檔會載入失敗。

預設只收檔名符合 storydata_spine_full_* 的 bundle；若檔名不同，用 --filter 指定，
例如 --filter "spine_full_*" 或 --filter ""（全收）。

--since 用檔案修改時間篩選，日期寫成 YYMMDD（例如 261031 = 2026-10-31），
8 碼則視為 YYYYMMDD；沒給時間就以當天 00:00:00 為界，也可再加上
T / _ / - / 空白 + HHMM、HH:MM、HHMMSS 指定時刻（例如 261031T1200）。
適合每月更新後只處理新下載的檔案。

增量用法（只轉新檔案，但 viewer 仍包含舊角色）：
    python spine_viewer.py --bundle a --since 261031 --export-all
    - lazy 模式會讀回 <out>_assets\chars\*.js 的 id / name / title，
      和新轉好的角色合併後重新產生 viewer HTML，舊資產不會被清掉
      （--single 是全部內嵌，無法疊加，只能整批重跑）
    - --export-all 的 ZIP 預設加上日期：spine_viewer_all_261031.zip
      （有 --since 用指定日期，否則用今天）
    - 匯出時會先讀 <viewer>_export_index.json 和同資料夾先前的
      spine_viewer_all*.zip，裡面已有的檔名不再輸出，
      所以新 ZIP 不會重複原壓縮檔的內容；索引在匯出成功後自動更新
      （同名批次不算在內，所以同一天重跑仍會重新匯出）
    - 想無視既有內容完整重匯，加 --no-export-dedupe

角色名稱可放在 names.json：
    {"1059": "コッコロ", "1278": "ヴルム", "105911": "コッコロ（新年）"}
key 可以是完整角色 ID 或前 4 碼 base ID；完整 ID 優先。沒有 --names 時會自動讀
<bundle 資料夾>\names.json。清單會顯示成「105911_コッコロ」；查不到名稱時只顯示 ID。

若資料夾是下載器的 raw 佈局（<base>\a\<40碼hash>、<base>\manifest\*、
<base>\manifest.db），會自動用 manifest 的 size + xxHash64 反查原始路徑，
不需要先把檔案改名。

第一次執行會自動下載兩個工具到 tools\：
    tools\spine-webgl-3.8.95.js          官方 Spine 3.8.95 runtime
    tools\SpineSkeletonDataConverter.exe  把 3.5~4.2 的 .skel 轉成 3.8 JSON
"""

import argparse
import base64
import fnmatch
import glob
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
import zlib
from datetime import datetime
from pathlib import Path

import UnityPy
import xxhash

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 這些 bundle 的標頭版本被歸零，實際引擎版本是 Unity 6000.0.75f1
UnityPy.config.FALLBACK_UNITY_VERSION = "6000.0.75f1"

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, "tools")
CONVERTER = os.path.join(TOOLS, "SpineSkeletonDataConverter.exe")
RUNTIME = os.path.join(TOOLS, "spine-webgl-3.8.95.js")

CONVERTER_URL = "https://github.com/wang606/SpineSkeletonDataConverter/releases/download/v3.8/SpineSkeletonDataConverter.exe"
RUNTIME_URL = "https://cdn.jsdelivr.net/gh/EsotericSoftware/spine-runtimes@3.8.95/spine-ts/build/spine-webgl.js"
TARGET_VERSION = "3.8.95"

# 261031 / 20261031 / 2026-10-31 / 261031T1200 / 261031-12:30:00 / "261031 12:00"
SINCE_RE = re.compile(
    r"^(?P<y>\d{4}|\d{2})[-/.]?(?P<m>\d{2})[-/.]?(?P<d>\d{2})"
    r"(?:(?:[T_ ]+|-)(?P<H>\d{1,2}):?(?P<M>\d{2})(?::?(?P<S>\d{2}))?)?$"
)


def _since_datetime(year, month, day, hour, minute, second):
    if year < 100:
        year += 2000
    return datetime(year, month, day, int(hour), int(minute), int(second))


def parse_since(text):
    """把 --since 的日期字串解析成 datetime（本地時間）。

    6 碼視為 YYMMDD（20YY），8 碼視為 YYYYMMDD；沒給時間就是當天 00:00:00，
    也可以接上 T / _ / - / 空白 + HHMM、HH:MM、HHMMSS 指定時刻。
    """
    raw = text.strip()
    match = SINCE_RE.match(raw)
    if match:
        parts = match.groupdict()
        try:
            return _since_datetime(
                int(parts["y"]), int(parts["m"]), int(parts["d"]),
                parts["H"] or 0, parts["M"] or 0, parts["S"] or 0,
            )
        except ValueError as exc:
            error = ValueError(f"日期不合法：{text}（{exc}）")
    else:
        error = ValueError("日期格式應為 YYMMDD（例如 261031）或 YYYYMMDD，可再加上時間")

    # 也接受時間直接連寫：YYMMDDHH、YYMMDDHHMM、YYYYMMDDHHMM
    digits = re.sub(r"\D", "", raw)
    if len(digits) in (8, 10, 12):
        head = 4 if len(digits) == 12 else 2
        try:
            return _since_datetime(
                int(digits[:head]), int(digits[head:head + 2]), int(digits[head + 2:head + 4]),
                digits[head + 4:head + 6] or 0, digits[head + 6:head + 8] or 0,
                digits[head + 8:head + 10] or 0,
            )
        except ValueError as exc:
            raise ValueError(f"日期不合法：{text}（{exc}）") from exc
    raise error


def file_mtime(path):
    """檔案的修改時間（秒），讀不到時回傳 0。"""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


# --------------------------------------------------------------------------- #
def ensure_tool(url, path, label):
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    print(f"[download] {label} ...")
    tmp = path + ".part"
    with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f)
    os.replace(tmp, path)
    print(f"[download] 完成：{path}")
    return path


def extract_bundle(path):
    """回傳 dict(atlas, skel, image, tex_name) 或 None。"""
    env = UnityPy.load(path)
    atlas_text = None
    skel_bytes = None
    best = None  # (area, data)

    for obj in env.objects:
        kind = obj.type.name
        if kind == "TextAsset":
            data = obj.read()
            raw = data.m_Script
            if isinstance(raw, str):
                raw = raw.encode("utf-8", "surrogateescape")
            else:
                raw = bytes(raw)
            low = data.m_Name.lower()
            if low.endswith(".atlas"):
                atlas_text = raw.decode("utf-8", "replace")
            elif low.endswith(".skel"):
                skel_bytes = raw
        elif kind == "Texture2D":
            data = obj.read()
            area = data.m_Width * data.m_Height
            if area and (best is None or area > best[0]):
                best = (area, data)

    if not atlas_text or not skel_bytes or best is None:
        return None

    tex = best[1]
    return {
        "atlas": atlas_text,
        "skel": skel_bytes,
        "image": tex.image,
        "tex_name": tex.m_Name,
    }


def skel_to_json(skel_bytes, workdir):
    skel_path = os.path.join(workdir, "skeleton.skel")
    json_path = os.path.join(workdir, "skeleton.json")
    with open(skel_path, "wb") as f:
        f.write(skel_bytes)

    result = subprocess.run(
        [CONVERTER, skel_path, json_path, "-v", TARGET_VERSION],
        cwd=workdir, capture_output=True, text=True, errors="replace",
    )
    if result.returncode != 0 or not os.path.isfile(json_path):
        raise RuntimeError((result.stdout or "") + (result.stderr or ""))

    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _read_manifest_entries(manifest_dir):
    r"""讀 test\manifest\*：每行 <原始路徑>,<xxHash64>,<group>,<size>,"""
    entries = {}
    if not os.path.isdir(manifest_dir):
        return entries

    for name in os.listdir(manifest_dir):
        path = os.path.join(manifest_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.lstrip("\ufeff").rstrip("\r\n").split(",")
                    if len(parts) < 4 or not parts[0]:
                        continue
                    digest = parts[1].strip().lower()
                    try:
                        size = int(parts[3])
                    except ValueError:
                        continue
                    if len(digest) == 16:
                        entries[parts[0].replace("\\", "/")] = (digest, size)
        except OSError:
            continue
    return entries


def _find_raw_layout(folder):
    r"""找 <base>\a（hash 檔）、<base>\manifest、<base>\manifest.db。"""
    folder = os.path.abspath(folder)
    for base in (folder, os.path.dirname(folder)):
        raw_dir = os.path.join(base, "a")
        manifest_dir = os.path.join(base, "manifest")
        db_path = os.path.join(base, "manifest.db")
        if os.path.isdir(raw_dir) and (os.path.isdir(manifest_dir) or os.path.isfile(db_path)):
            return base, raw_dir, manifest_dir, db_path
    return None


def resolve_raw_bundles(folder, pattern):
    """把下載器的 40 碼 hash 檔名反查成 manifest 裡的原始路徑。

    回傳 (base_dir, [(raw_path, logical_path), ...])；不是 raw 佈局則回傳 None。
    先用 manifest 的 size 篩選，再比對檔案內容的 xxHash64，避免只靠大小誤判。
    """
    layout = _find_raw_layout(folder)
    if layout is None:
        return None

    base_dir, raw_dir, manifest_dir, db_path = layout
    entries = _read_manifest_entries(manifest_dir)

    # 沒有 test\manifest\* 時，退回 manifest.db（只有路徑 + hash，沒有 size）
    if not entries and os.path.isfile(db_path):
        try:
            with sqlite3.connect(db_path) as con:
                for logical, digest in con.execute("select k, v from t"):
                    digest = str(digest).strip().lower()
                    if len(digest) == 16:
                        entries[logical.replace("\\", "/")] = (digest, None)
        except sqlite3.Error:
            pass

    wanted = {}
    for logical, (digest, size) in entries.items():
        if not pattern or fnmatch.fnmatch(os.path.basename(logical), pattern):
            wanted.setdefault(digest, []).append((logical, size))

    if not wanted:
        return base_dir, []

    sizes = {size for items in wanted.values() for _, size in items if size is not None}
    hash_all = not sizes  # 沒有 size 資訊時只能全部算 hash

    found = {}
    for name in sorted(os.listdir(raw_dir)):
        path = os.path.join(raw_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            if not hash_all and os.path.getsize(path) not in sizes:
                continue
            h = xxhash.xxh64()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
        except OSError:
            continue

        digest = h.hexdigest()
        if digest in wanted and digest not in found:
            found[digest] = path

    pairs = []
    for digest, items in wanted.items():
        raw_path = found.get(digest)
        if raw_path is None:
            continue
        for logical, _ in items:
            pairs.append((raw_path, logical))

    pairs.sort(key=lambda item: item[1])
    return base_dir, pairs


def js_literal(value):
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def load_names(path):
    """讀 names.json：{"105911": "コッコロ"} 或 {"1059": "コッコロ"}。"""
    if not path:
        return {}
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("names 檔必須是 JSON 物件（id -> 名稱）")
    names = {}
    for key, value in data.items():
        key = str(key).strip()
        value = str(value).strip() if value is not None else ""
        if key and value:
            names[key] = value
    return names


def lookup_name(char_id, names):
    """完整 ID 優先，其次前 4 碼 base ID。"""
    if not names:
        return ""
    return names.get(char_id) or names.get(str(char_id)[:4]) or ""


# --------------------------------------------------------------------------- #
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Spine Viewer</title>
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; overflow: hidden; background: #14161a; color: #e7eaf0;
               font-family: "Segoe UI", "Microsoft JhengHei", system-ui, sans-serif; }
  #app { display: flex; height: 100%; }

  #side { flex: 0 0 272px; min-width: 0; display: flex; flex-direction: column;
          background: #181b21; border-right: 1px solid #262c36;
          overflow-y: auto; padding: 14px 14px 18px; }
  #side .brand { font-size: 15px; font-weight: 600; letter-spacing: .4px; }
  #side .brand small { color: #7f8798; font-weight: 400; font-size: 11px; margin-left: 6px; }

  .sec { margin-top: 15px; }
  .sec-title { font-size: 11px; color: #7f8798; letter-spacing: .8px; margin-bottom: 6px;
               display: flex; justify-content: space-between; align-items: center; }
  .chips { display: flex; flex-wrap: wrap; gap: 5px; }

  button { background: #232833; border: 1px solid #333b49; color: #cfd5df; border-radius: 6px;
           padding: 5px 10px; font-size: 12px; cursor: pointer; transition: .12s; }
  button:hover { background: #2c3340; border-color: #465063; }
  button.active { background: #3f7dfb; border-color: #3f7dfb; color: #fff; }

  .char-list { display: flex; flex-direction: column; gap: 4px; max-height: 280px;
               overflow-y: auto; padding-right: 2px; }
  .char-list button { text-align: left; display: block; white-space: normal;
                      line-height: 1.35; word-break: break-word; }

  .row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .row + .row { margin-top: 6px; }
  .small { font-size: 11.5px; color: #858e9c; }
  .check { display: flex; align-items: center; gap: 4px; font-size: 11.5px;
           color: #858e9c; cursor: pointer; }
  .check input { accent-color: #3f7dfb; }
  input[type=range] { flex: 1 1 90px; min-width: 70px; accent-color: #3f7dfb; }
  input[type=color] { width: 28px; height: 22px; padding: 0; border: 1px solid #333b49;
                      background: #232833; border-radius: 4px; cursor: pointer; }

  #view { position: relative; flex: 1 1 auto; min-width: 0; }
  #stage { position: absolute; inset: 0; width: 100%; height: 100%; display: block; cursor: grab; }
  #stage.dragging { cursor: grabbing; }
  #hint { position: absolute; right: 14px; top: 12px; font-size: 11.5px; color: #5f6875;
          pointer-events: none; }
  #loading, #error { position: absolute; inset: 0; display: flex; align-items: center;
                     justify-content: center; font-size: 13px; color: #9aa3b2;
                     background: rgba(20,22,26,.84); }
  #error { display: none; color: #ff8a8a; white-space: pre-wrap; padding: 24px; text-align: center; }
  #export-msg { color: #6fbf8f; min-height: 16px; word-break: break-all; }
</style>
</head>
<body>
<div id="app">
  <aside id="side">
    <div class="brand">Spine Viewer <small>3.8.95</small></div>

    <div class="sec">
      <div class="sec-title"><span>角色</span><span id="char-count" class="small"></span></div>
      <div id="char-list" class="char-list"></div>
    </div>

    <div class="sec">
      <div class="sec-title"><span>表情</span></div>
      <div id="row-skin" class="chips"></div>
    </div>

    <div class="sec">
      <div class="sec-title"><span>眼睛</span></div>
      <div id="row-eye" class="chips"></div>
    </div>

    <div class="sec">
      <div class="sec-title"><span>嘴巴</span></div>
      <div id="row-mouth" class="chips"></div>
    </div>

    <div class="sec">
      <div class="sec-title"><span>播放</span></div>
      <div class="row">
        <button id="btn-play">暫停</button>
        <span class="small">速度</span>
        <input id="in-speed" type="range" min="0.25" max="2" step="0.05" value="1">
      </div>
      <div class="row">
        <span class="small">縮放</span>
        <input id="in-zoom" type="range" min="0.4" max="3" step="0.05" value="1">
      </div>
      <div class="row">
        <span class="small">背景</span>
        <input id="in-bg" type="color" value="#1b1f27">
        <button id="btn-reset">重置視角</button>
      </div>
    </div>

    <div class="sec">
      <div class="sec-title"><span>輸出</span></div>
      <div class="row">
        <button id="btn-save">儲存 PNG</button>
        <button id="btn-batch" title="每種表情會輸出開眼/閉眼 × 開口/閉口四張">批量表情 ZIP</button>
      </div>
      <div class="row">
        <label class="check"><input id="chk-alpha" type="checkbox" checked> 透明背景（保留 A 通道）</label>
      </div>
      <div id="export-msg" class="small"></div>
    </div>
  </aside>

  <main id="view">
    <canvas id="stage"></canvas>
    <div id="hint">↑↓ 切換角色 · 拖曳平移 · 滾輪縮放 · 雙擊重置</div>
    <div id="loading">載入中…</div>
    <div id="error"></div>
  </main>
</div>

<script>__SPINE_WEBGL__</script>
<script>
(function () {
  "use strict";

  var CHARACTERS = __CHARACTERS_JSON__;
  var CHAR_IDS = __CHAR_IDS_JSON__;
  var LAZY = __LAZY__;
  var ASSET_BASE = __ASSET_BASE__;

  var SKIN_LABELS = { "default": "原始", "normal": "普通", "joy": "開心", "anger": "生氣",
                      "sad": "難過", "shy": "害羞", "surprised": "驚訝",
                      "special_a": "特別 A", "special_b": "特別 B" };
  var EYE_LABELS = { "eye_idle": "待機眨眼", "eye_blink": "眨一次", "eye_open": "張眼", "eye_close": "閉眼" };
  var MOUTH_LABELS = { "mouth_idle": "待機", "mouth_talk": "說話", "mouth_open": "張嘴", "mouth_close": "閉嘴" };
  var LOOPS = { "eye_idle": true, "mouth_talk": true };

  var canvas = document.getElementById("stage");
  var gl = canvas.getContext("webgl", { alpha: true, premultipliedAlpha: true, antialias: true });
  if (!gl) { showError("這個瀏覽器沒有 WebGL，請改用 Chrome / Edge。"); return; }

  var renderer = new spine.webgl.SceneRenderer(canvas, gl, false);
  var active = null;
  var prefs = {};
  var switching = false;
  var playing = true, speed = 1, zoom = 1, panX = 0, panY = 0, bg = "#1b1f27";
  var drag = null;
  var last = performance.now();

  // 只快取最近 3 張已解碼貼圖，避免多個 2048x2048 角色佔滿記憶體
  var imageCache = new Map();
  var IMAGE_CACHE_MAX = 3;

  // lazy 模式：完整角色資料按需要載入；只保留最近幾個，避免 500 角色把記憶體吃滿
  var charData = {};
  var charPromises = {};
  var charLru = [];
  var CHAR_CACHE_MAX = 6;

  window.SPINE_REGISTER_CHARACTER = function (cfg) {
    if (cfg && cfg.id) charData[cfg.id] = cfg;
  };

  var exportCanvas = null, exportGl = null, exportRenderer = null;
  var exportTexture = null, exportAtlas = null, exportData = null;
  var exportSkeleton = null, exportState = null, exportCharId = null;

  init();

  function init() {
    buildCharList();
    bindControls();
    resize();
    window.addEventListener("resize", resize);
    requestAnimationFrame(tick);
    if (CHAR_IDS.length) selectCharacter(CHAR_IDS[0]);
    else showError("這個 HTML 裡沒有角色資料。");
  }

  // ------------------------------------------------------------------ //
  function buildCharList() {
    var list = document.getElementById("char-list");
    list.innerHTML = "";
    document.getElementById("char-count").textContent = CHAR_IDS.length;

    CHAR_IDS.forEach(function (id) {
      var b = document.createElement("button");
      b.dataset.id = id;
      b.textContent = displayName(id);
      b.title = displayName(id) + "\n" + CHARACTERS[id].title;
      b.addEventListener("click", function () { selectCharacter(id); });
      list.appendChild(b);
    });
  }

  function displayName(id) {
    var cfg = CHARACTERS[id];
    return (cfg && cfg.name) ? id + "_" + cfg.name : id;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c];
    });
  }

  function markCharActive() {
    var buttons = document.querySelectorAll("#char-list button");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].classList.toggle("active", !!active && buttons[i].dataset.id === active.id);
    }
  }

  function scrollActiveIntoView() {
    var buttons = document.querySelectorAll("#char-list button");
    for (var i = 0; i < buttons.length; i++) {
      if (buttons[i].classList.contains("active") && buttons[i].scrollIntoView) {
        try { buttons[i].scrollIntoView({ block: "nearest" }); } catch (e) { /* ignore */ }
        return;
      }
    }
  }

  // ------------------------------------------------------------------ //
  function getCharacterData(id) {
    if (!LAZY) return Promise.resolve(CHARACTERS[id]);
    if (charData[id]) return Promise.resolve(charData[id]);
    if (charPromises[id]) return charPromises[id];

    var promise = new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.async = true;
      s.src = ASSET_BASE + "chars/" + id + ".js";
      s.onload = function () {
        if (s.parentNode) s.parentNode.removeChild(s);
        var cfg = charData[id];
        if (!cfg) { reject(new Error("角色資料格式錯誤：" + id)); return; }
        resolve(cfg);
      };
      s.onerror = function () {
        if (s.parentNode) s.parentNode.removeChild(s);
        reject(new Error("角色檔載入失敗：" + id + "（viewer HTML 必須和 " +
                         ASSET_BASE + " 資料夾一起移動）"));
      };
      document.head.appendChild(s);
    });
    charPromises[id] = promise;
    promise.then(function () { delete charPromises[id]; },
                 function () { delete charPromises[id]; });
    return promise;
  }

  function touchCharCache(id) {
    if (!LAZY) return;
    var i = charLru.indexOf(id);
    if (i >= 0) charLru.splice(i, 1);
    charLru.push(id);
    while (charLru.length > CHAR_CACHE_MAX) {
      var old = charLru.shift();
      if (old === id) { charLru.push(old); continue; }
      delete charData[old];
    }
  }

  function getImage(cfg) {
    if (imageCache.has(cfg.id)) {
      var cached = imageCache.get(cfg.id);
      imageCache.delete(cfg.id);
      imageCache.set(cfg.id, cached);
      return Promise.resolve(cached);
    }
    return new Promise(function (resolve, reject) {
      var img = new Image();
      img.onload = function () {
        imageCache.set(cfg.id, img);
        while (imageCache.size > IMAGE_CACHE_MAX) {
          imageCache.delete(imageCache.keys().next().value);
        }
        resolve(img);
      };
      img.onerror = function () { reject(new Error("貼圖載入失敗：" + cfg.id)); };
      img.src = cfg.texture;
    });
  }

  function selectCharacter(id) {
    if (switching || (active && active.id === id)) return;
    if (!CHARACTERS[id]) return;

    hideError();
    switching = true;
    setLoading(true, "載入角色 " + displayName(id) + "…");

    getCharacterData(id).then(function (cfg) {
      return getImage(cfg).then(function (img) {
        try {
          if (active && active.tex) disposeTexture(active.tex);

          var tex = new spine.webgl.GLTexture(gl, img);
          var atlas = new spine.TextureAtlas(cfg.atlas, function () { return tex; });
          var data = new spine.SkeletonJson(new spine.AtlasAttachmentLoader(atlas))
            .readSkeletonData(cfg.skeleton);
          var skeleton = new spine.Skeleton(data);
          var state = new spine.AnimationState(new spine.AnimationStateData(data));
          state.data.defaultMix = 0.1;

          var p = prefs[id] || {};
          var skin = pickSkin(data, p.skin);
          var eye = pickAnim(data, "eye_", p.eye, "eye_idle");
          var mouth = pickAnim(data, "mouth_", p.mouth, "mouth_idle");

          // 一定要先套 skin、更新世界座標，附件才會存在，bounds 才算得出來
          skeleton.setSkinByName(skin);
          skeleton.setSlotsToSetupPose();
          skeleton.updateWorldTransform();
          var bounds = computeBounds(skeleton, data);

          active = {
            id: id, cfg: cfg, image: img, tex: tex, atlas: atlas, data: data,
            skeleton: skeleton, state: state, bounds: bounds,
            skin: skin, eye: eye, mouth: mouth
          };

          touchCharCache(id);
          restartTracks();
          buildAnimButtons();
          markCharActive();
          scrollActiveIntoView();
          document.title = displayName(id) + " - Spine Viewer";
          resetView();
          switching = false;
          setLoading(false);
        } catch (err) {
          switching = false;
          setLoading(false);
          showError(err && err.stack || err);
        }
      });
    }).catch(function (err) {
      switching = false;
      setLoading(false);
      showError(err && err.message || err);
    });
  }

  function visibleSkinNames(data) {
    var names = data.skins.map(function (s) { return s.name; });
    // 有 normal 時，default 只是重複的原始 skin：不顯示、也不匯出
    if (names.indexOf("normal") >= 0) {
      names = names.filter(function (n) { return n !== "default"; });
    }
    return names;
  }

  function pickSkin(data, preferred) {
    var names = visibleSkinNames(data);
    if (preferred && names.indexOf(preferred) >= 0) return preferred;
    if (names.indexOf("normal") >= 0) return "normal";
    if (names.indexOf("default") >= 0) return "default";
    return names[0];
  }

  function pickAnim(data, prefix, preferred, fallback) {
    var names = data.animations.map(function (a) { return a.name; });
    if (preferred && names.indexOf(preferred) >= 0) return preferred;
    if (names.indexOf(fallback) >= 0) return fallback;
    for (var i = 0; i < names.length; i++) {
      if (names[i].indexOf(prefix) === 0) return names[i];
    }
    return names[0];
  }

  // 用實際附件（身體）算出可視範圍；skeleton 宣告的 width/height 往往沒包含 y 位移
  function computeBounds(skel, dataObj) {
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    var verts = new Float32Array(8);
    var found = false;
    for (var i = 0; i < skel.slots.length; i++) {
      var att = skel.slots[i].getAttachment();
      if (!att || !(att instanceof spine.RegionAttachment)) continue;
      att.updateOffset();
      att.computeWorldVertices(skel.slots[i].bone, verts, 0, 2);
      for (var j = 0; j < 8; j += 2) {
        if (verts[j] < minX) minX = verts[j];
        if (verts[j] > maxX) maxX = verts[j];
        if (verts[j + 1] < minY) minY = verts[j + 1];
        if (verts[j + 1] > maxY) maxY = verts[j + 1];
      }
      found = true;
    }
    if (!found) {
      return { x: 0, y: 0, width: dataObj.width || 800, height: dataObj.height || 1000 };
    }
    return { x: minX, y: minY, width: maxX - minX, height: maxY - minY };
  }

  // ------------------------------------------------------------------ //
  function restartTracks() {
    if (!active) return;
    active.state.clearTracks();
    active.state.setAnimation(0, active.eye, LOOPS[active.eye] === true);
    active.state.setAnimation(1, active.mouth, LOOPS[active.mouth] === true);
    active.state.update(0);
    active.state.apply(active.skeleton);
    active.skeleton.updateWorldTransform();
    prefs[active.id] = { skin: active.skin, eye: active.eye, mouth: active.mouth };
    markActive();
  }

  function setSkin(name) {
    if (!active) return;
    active.skin = name;
    active.skeleton.setSkinByName(name);
    active.skeleton.setSlotsToSetupPose();
    restartTracks();
  }

  function setAnim(kind, name) {
    if (!active) return;
    if (kind === "eye") active.eye = name;
    else active.mouth = name;
    restartTracks();
  }

  function buildAnimButtons() {
    if (!active) return;
    var skinRow = document.getElementById("row-skin");
    var eyeRow = document.getElementById("row-eye");
    var mouthRow = document.getElementById("row-mouth");
    skinRow.innerHTML = "";
    eyeRow.innerHTML = "";
    mouthRow.innerHTML = "";

    var skinNames = visibleSkinNames(active.data);
    var ordered = skinNames.indexOf("default") >= 0
      ? ["default"].concat(skinNames.filter(function (n) { return n !== "default"; }))
      : skinNames;

    ordered.forEach(function (name) {
      skinRow.appendChild(makeButton(SKIN_LABELS[name] || name, "skin", name, function () { setSkin(name); }));
    });

    active.data.animations.filter(function (a) { return a.name.indexOf("eye_") === 0; })
      .forEach(function (a) {
        eyeRow.appendChild(makeButton(EYE_LABELS[a.name] || a.name, "eye", a.name, function () { setAnim("eye", a.name); }));
      });

    active.data.animations.filter(function (a) { return a.name.indexOf("mouth_") === 0; })
      .forEach(function (a) {
        mouthRow.appendChild(makeButton(MOUTH_LABELS[a.name] || a.name, "mouth", a.name, function () { setAnim("mouth", a.name); }));
      });

    markActive();
  }

  function makeButton(text, key, value, onClick) {
    var b = document.createElement("button");
    b.textContent = text;
    b.dataset[key] = value;
    b.addEventListener("click", onClick);
    return b;
  }

  function markActive() {
    if (!active) return;
    setActive("row-skin", "skin", active.skin);
    setActive("row-eye", "eye", active.eye);
    setActive("row-mouth", "mouth", active.mouth);
  }

  function setActive(rowId, key, value) {
    var row = document.getElementById(rowId);
    var buttons = row.querySelectorAll("button");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].classList.toggle("active", buttons[i].dataset[key] === value);
    }
  }

  function disposeTexture(tex) {
    if (!tex) return;
    try {
      if (typeof tex.dispose === "function") tex.dispose();
      else if (tex.texture) gl.deleteTexture(tex.texture);
    } catch (e) { /* ignore */ }
  }

  // ------------------------------------------------------------------ //
  function cycleCharacter(delta) {
    if (switching || !CHAR_IDS.length) return;
    var idx = active ? CHAR_IDS.indexOf(active.id) : 0;
    if (idx < 0) idx = 0;
    var next = (idx + delta + CHAR_IDS.length) % CHAR_IDS.length;
    if (active && CHAR_IDS[next] === active.id) return;
    selectCharacter(CHAR_IDS[next]);
  }

  function onKeyDown(e) {
    if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    var t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" ||
              t.tagName === "SELECT" || t.isContentEditable)) return;
    e.preventDefault();
    cycleCharacter(e.key === "ArrowDown" ? 1 : -1);
  }

  function bindControls() {
    var playBtn = document.getElementById("btn-play");
    playBtn.addEventListener("click", function () {
      playing = !playing;
      playBtn.textContent = playing ? "暫停" : "播放";
    });

    document.getElementById("in-speed").addEventListener("input", function (e) {
      speed = parseFloat(e.target.value);
    });

    document.getElementById("in-zoom").addEventListener("input", function (e) {
      zoom = parseFloat(e.target.value);
      updateCamera();
    });

    document.getElementById("in-bg").addEventListener("input", function (e) {
      bg = e.target.value;
    });

    document.getElementById("btn-reset").addEventListener("click", resetView);
    document.getElementById("btn-save").addEventListener("click", saveCurrent);
    document.getElementById("btn-batch").addEventListener("click", exportBatch);
    document.addEventListener("keydown", onKeyDown);

    canvas.addEventListener("mousedown", function (e) {
      drag = { x: e.clientX, y: e.clientY };
      canvas.classList.add("dragging");
    });
    window.addEventListener("mouseup", function () {
      drag = null;
      canvas.classList.remove("dragging");
    });
    window.addEventListener("mousemove", function (e) {
      if (!drag) return;
      var cw = canvas.width, ch = canvas.height;
      panX -= (e.clientX - drag.x) * (renderer.camera.viewportWidth / cw);
      panY += (e.clientY - drag.y) * (renderer.camera.viewportHeight / ch);
      drag = { x: e.clientX, y: e.clientY };
      updateCamera();
    });

    canvas.addEventListener("wheel", function (e) {
      e.preventDefault();
      zoom = clamp(zoom * Math.exp(-e.deltaY * 0.0012), 0.4, 3);
      document.getElementById("in-zoom").value = zoom;
      updateCamera();
    }, { passive: false });

    canvas.addEventListener("dblclick", resetView);
  }

  function resetView() {
    zoom = 1;
    panX = panY = 0;
    document.getElementById("in-zoom").value = 1;
    updateCamera();
  }

  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

  // ------------------------------------------------------------------ //
  function resize() {
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var w = Math.max(1, canvas.clientWidth);
    var h = Math.max(1, canvas.clientHeight);
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    updateCamera();
  }

  function updateCamera() {
    var b = active ? active.bounds : { x: 0, y: 0, width: 800, height: 1000 };
    var cw = canvas.width, ch = canvas.height;
    var pad = 1.06;
    var ratio = cw / ch;
    var vh = b.height * pad, vw = vh * ratio;
    if (vw < b.width * pad) { vw = b.width * pad; vh = vw / ratio; }
    renderer.camera.setViewport(vw / zoom, vh / zoom);
    renderer.camera.position.x = b.x + b.width / 2 + panX;
    renderer.camera.position.y = b.y + b.height / 2 + panY;
    renderer.camera.update();
  }

  function hexToRgb(hex) {
    var n = parseInt(hex.slice(1), 16);
    return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
  }

  // ------------------------------------------------------------------ //
  var lastFrame = performance.now();
  function tick(now) {
    var dt = Math.min((now - lastFrame) / 1000, 0.05);
    lastFrame = now;

    if (active) {
      if (playing) active.state.update(dt * speed);
      active.state.apply(active.skeleton);
      active.skeleton.updateWorldTransform();
    }

    gl.viewport(0, 0, canvas.width, canvas.height);
    var c = hexToRgb(bg);
    gl.clearColor(c[0], c[1], c[2], 1);
    gl.clear(gl.COLOR_BUFFER_BIT);

    if (active) {
      renderer.begin();
      // 貼圖是預乘 alpha，必須用 PMA 混合，否則半透明的臉紅會被再乘一次而變黑
      renderer.drawSkeleton(active.skeleton, true);
      renderer.end();
    }

    requestAnimationFrame(tick);
  }

  // ------------------------------------------------------------------ //
  function setLoading(on, msg) {
    var el = document.getElementById("loading");
    el.textContent = msg || "載入中…";
    el.style.display = on ? "flex" : "none";
  }

  function showError(msg) {
    setLoading(false);
    var e = document.getElementById("error");
    e.textContent = String(msg);
    e.style.display = "flex";
    if (window.console) console.error(msg);
  }

  function hideError() {
    var e = document.getElementById("error");
    e.textContent = "";
    e.style.display = "none";
  }

  function setMsg(text, isError) {
    var el = document.getElementById("export-msg");
    el.textContent = text || "";
    el.style.color = isError ? "#ff8a8a" : "#6fbf8f";
  }

  // ------------------------------------------------------------------ //
  // 匯出：離屏 WebGL + readPixels，手動 unpremultiply 後寫成 straight-alpha PNG
  // ------------------------------------------------------------------ //
  function ensureExportContext() {
    if (exportRenderer) return;
    exportCanvas = document.createElement("canvas");
    exportGl = exportCanvas.getContext("webgl", { alpha: true, premultipliedAlpha: true, antialias: true });
    if (!exportGl) throw new Error("無法建立匯出用 WebGL context");
    exportRenderer = new spine.webgl.SceneRenderer(exportCanvas, exportGl, false);
  }

  function ensureExportData() {
    if (exportCharId === active.id) return;
    if (exportTexture) {
      try { exportTexture.dispose(); } catch (e) { /* ignore */ }
      exportTexture = null;
    }
    exportTexture = new spine.webgl.GLTexture(exportGl, active.image);
    exportAtlas = new spine.TextureAtlas(active.cfg.atlas, function () { return exportTexture; });
    exportData = new spine.SkeletonJson(new spine.AtlasAttachmentLoader(exportAtlas))
      .readSkeletonData(active.cfg.skeleton);
    exportSkeleton = new spine.Skeleton(exportData);
    exportState = new spine.AnimationState(new spine.AnimationStateData(exportData));
    exportState.data.defaultMix = 0;
    exportCharId = active.id;
  }

  function renderExport(opts) {
    if (!active) throw new Error("尚未載入角色");
    ensureExportContext();
    ensureExportData();

    var b = active.bounds;
    var pad = 1.06;
    var w = Math.max(1, Math.round(b.width * pad));
    var h = Math.max(1, Math.round(b.height * pad));

    exportCanvas.width = w;
    exportCanvas.height = h;
    exportGl.viewport(0, 0, w, h);

    exportSkeleton.setSkinByName(opts.skin);
    exportSkeleton.setSlotsToSetupPose();

    exportState.clearTracks();
    exportState.setAnimation(0, opts.eye, LOOPS[opts.eye] === true);
    exportState.setAnimation(1, opts.mouth, LOOPS[opts.mouth] === true);
    exportState.update(0);

    if (opts.syncTime) {
      for (var i = 0; i < exportState.tracks.length && i < active.state.tracks.length; i++) {
        var src = active.state.tracks[i], dst = exportState.tracks[i];
        if (src && dst) {
          dst.trackTime = src.trackTime;
          dst.animationLast = src.trackTime;
        }
      }
    }

    exportState.apply(exportSkeleton);
    exportSkeleton.updateWorldTransform();

    exportRenderer.camera.setViewport(w, h);
    exportRenderer.camera.position.x = b.x + b.width / 2;
    exportRenderer.camera.position.y = b.y + b.height / 2;
    exportRenderer.camera.update();

    if (opts.transparent) {
      exportGl.clearColor(0, 0, 0, 0);
    } else {
      var rgb = hexToRgb(bg);
      exportGl.clearColor(rgb[0], rgb[1], rgb[2], 1);
    }
    exportGl.clear(exportGl.COLOR_BUFFER_BIT);

    exportRenderer.begin();
    exportRenderer.drawSkeleton(exportSkeleton, true);
    exportRenderer.end();

    var px = new Uint8Array(w * h * 4);
    exportGl.readPixels(0, 0, w, h, exportGl.RGBA, exportGl.UNSIGNED_BYTE, px);
    return encodePNG(px, w, h);
  }

  function encodePNG(px, w, h) {
    // readPixels 是 pre-multiplied、且上下顛倒；轉成 straight alpha 並翻正
    var out = new Uint8ClampedArray(w * h * 4);
    for (var y = 0; y < h; y++) {
      var srcRow = (h - 1 - y) * w * 4;
      var dstRow = y * w * 4;
      for (var x = 0; x < w; x++) {
        var si = srcRow + x * 4;
        var di = dstRow + x * 4;
        var a = px[si + 3];
        if (a === 0) {
          out[di] = 0; out[di + 1] = 0; out[di + 2] = 0; out[di + 3] = 0;
        } else if (a === 255) {
          out[di] = px[si]; out[di + 1] = px[si + 1]; out[di + 2] = px[si + 2]; out[di + 3] = 255;
        } else {
          out[di]     = Math.min(255, Math.round(px[si]     * 255 / a));
          out[di + 1] = Math.min(255, Math.round(px[si + 1] * 255 / a));
          out[di + 2] = Math.min(255, Math.round(px[si + 2] * 255 / a));
          out[di + 3] = a;
        }
      }
    }

    var c2 = document.createElement("canvas");
    c2.width = w;
    c2.height = h;
    var ctx2 = c2.getContext("2d");
    var imgData = ctx2.createImageData(w, h);
    imgData.data.set(out);
    ctx2.putImageData(imgData, 0, 0);
    return dataUrlToBytes(c2.toDataURL("image/png"));
  }

  function dataUrlToBytes(url) {
    var b64 = url.slice(url.indexOf(",") + 1);
    var bin = atob(b64);
    var bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
  }

  function saveCurrent() {
    if (!active) return;
    try {
      var transparent = document.getElementById("chk-alpha").checked;
      setMsg("產生 PNG…");
      var bytes = renderExport({
        skin: active.skin, eye: active.eye, mouth: active.mouth,
        transparent: transparent, syncTime: true
      });
      var filename = active.id + "_" + active.skin + ".png";
      downloadBytes(bytes, filename, "image/png");
      setMsg("已儲存 " + filename);
    } catch (err) {
      setMsg("匯出失敗：" + (err && err.message || err), true);
    }
  }

  function exportBatch() {
    if (!active) return;
    try {
      var skins = visibleSkinNames(active.data);

      // 每種表情要包含開眼/閉眼 × 開口/閉口的組合；
      // 若角色缺少整組動畫，才退回目前的眼睛或嘴巴狀態。
      var hasAnim = {};
      active.data.animations.forEach(function (a) { hasAnim[a.name] = true; });

      var eyes = [];
      ["eye_open", "eye_close"].forEach(function (name) {
        if (hasAnim[name]) eyes.push(name);
      });
      if (!eyes.length) eyes.push(active.eye);

      var mouths = [];
      ["mouth_open", "mouth_close"].forEach(function (name) {
        if (hasAnim[name]) mouths.push(name);
      });
      if (!mouths.length) mouths.push(active.mouth);

      var files = [];
      var total = skins.length * eyes.length * mouths.length;
      var done = 0;
      for (var i = 0; i < skins.length; i++) {
        for (var e = 0; e < eyes.length; e++) {
          for (var m = 0; m < mouths.length; m++) {
            done++;
            setMsg("批量匯出 " + done + "/" + total + "…");
            var eye = eyes[e];
            var mouth = mouths[m];
            var bytes = renderExport({
              skin: skins[i], eye: eye, mouth: mouth,
              transparent: true, syncTime: false
            });
            var eyeSuffix = eye.indexOf("eye_") === 0 ? eye.slice(4) : eye;
            var mouthSuffix = mouth.indexOf("mouth_") === 0 ? mouth.slice(6) : mouth;
            files.push({
              name: active.id + "_" + skins[i] + "_eye_" + eyeSuffix +
                    "_mouth_" + mouthSuffix + ".png",
              data: bytes
            });
          }
        }
      }
      var zip = makeZip(files);
      var zipName = active.id + "_expressions.zip";
      downloadBytes(zip, zipName, "application/zip");
      setMsg("已匯出 " + files.length + " 張（每種表情含開眼/閉眼 × 開口/閉口）：" + zipName);
    } catch (err) {
      setMsg("匯出失敗：" + (err && err.message || err), true);
    }
  }

  // ------------------------------------------------------------------ //
  function downloadBytes(bytes, filename, mime) {
    var blob = new Blob([bytes], { type: mime });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () {
      URL.revokeObjectURL(url);
      a.remove();
    }, 1200);
  }

  // 最小 ZIP writer（store，不壓縮；PNG 本身已壓縮）
  var CRC_TABLE = (function () {
    var table = new Uint32Array(256);
    for (var n = 0; n < 256; n++) {
      var c = n;
      for (var k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
      table[n] = c >>> 0;
    }
    return table;
  })();

  function crc32(bytes) {
    var c = 0xFFFFFFFF;
    for (var i = 0; i < bytes.length; i++) {
      c = (CRC_TABLE[(c ^ bytes[i]) & 0xFF] ^ (c >>> 8)) >>> 0;
    }
    return (c ^ 0xFFFFFFFF) >>> 0;
  }

  function makeZip(files) {
    var enc = new TextEncoder();
    var chunks = [];
    var central = [];
    var offset = 0;
    var now = new Date();
    var dosTime = ((now.getHours() << 11) | (now.getMinutes() << 5) | (now.getSeconds() >> 1)) & 0xFFFF;
    var dosDate = (((now.getFullYear() - 1980) << 9) | ((now.getMonth() + 1) << 5) | now.getDate()) & 0xFFFF;

    files.forEach(function (f) {
      var nameBytes = enc.encode(f.name);
      var crc = crc32(f.data);
      var size = f.data.length;

      var local = new Uint8Array(30 + nameBytes.length);
      var lv = new DataView(local.buffer);
      lv.setUint32(0, 0x04034b50, true);
      lv.setUint16(4, 20, true);
      lv.setUint16(6, 0x0800, true);
      lv.setUint16(8, 0, true);
      lv.setUint16(10, dosTime, true);
      lv.setUint16(12, dosDate, true);
      lv.setUint32(14, crc, true);
      lv.setUint32(18, size, true);
      lv.setUint32(22, size, true);
      lv.setUint16(26, nameBytes.length, true);
      lv.setUint16(28, 0, true);
      local.set(nameBytes, 30);
      chunks.push(local, f.data);

      var cen = new Uint8Array(46 + nameBytes.length);
      var cv = new DataView(cen.buffer);
      cv.setUint32(0, 0x02014b50, true);
      cv.setUint16(4, 20, true);
      cv.setUint16(6, 20, true);
      cv.setUint16(8, 0x0800, true);
      cv.setUint16(10, 0, true);
      cv.setUint16(12, dosTime, true);
      cv.setUint16(14, dosDate, true);
      cv.setUint32(16, crc, true);
      cv.setUint32(20, size, true);
      cv.setUint32(24, size, true);
      cv.setUint16(28, nameBytes.length, true);
      cv.setUint16(30, 0, true);
      cv.setUint16(32, 0, true);
      cv.setUint16(34, 0, true);
      cv.setUint16(36, 0, true);
      cv.setUint32(38, 0, true);
      cv.setUint32(42, offset, true);
      cen.set(nameBytes, 46);
      central.push(cen);

      offset += local.length + size;
    });

    var centralSize = central.reduce(function (a, b) { return a + b.length; }, 0);
    var eocd = new Uint8Array(22);
    var ev = new DataView(eocd.buffer);
    ev.setUint32(0, 0x06054b50, true);
    ev.setUint16(4, 0, true);
    ev.setUint16(6, 0, true);
    ev.setUint16(8, files.length, true);
    ev.setUint16(10, files.length, true);
    ev.setUint32(12, centralSize, true);
    ev.setUint32(16, offset, true);
    ev.setUint16(20, 0, true);

    var out = new Uint8Array(offset + centralSize + eocd.length);
    var pos = 0;
    chunks.forEach(function (c) { out.set(c, pos); pos += c.length; });
    central.forEach(function (c) { out.set(c, pos); pos += c.length; });
    out.set(eocd, pos);
    return out;
  }
})();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
def write_character_asset(char, chars_dir):
    """把單一角色的完整資料寫成一個 classic script。"""
    payload = {
        "id": char["id"],
        "name": char["name"],
        "title": char["title"],
        "atlas": char["atlas"],
        "skeleton": char["skeleton"],
        "texture": char["texture"],
    }
    # window.SPINE_REGISTER_CHARACTER 由 viewer 提供，載入後把完整資料放進快取
    text = "window.SPINE_REGISTER_CHARACTER(" + js_literal(payload) + ");\n"
    path = os.path.join(chars_dir, char["id"] + ".js")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return os.path.getsize(path)


# chars\<id>.js 開頭固定是 {"id": ..., "name": ..., "title": ..., "atlas": ...}
CHAR_META_RE = re.compile(
    r'"id":\s*"((?:[^"\\]|\\.)*)",\s*'
    r'"name":\s*"((?:[^"\\]|\\.)*)",\s*'
    r'"title":\s*"((?:[^"\\]|\\.)*)"'
)


def read_existing_chars(chars_dir):
    """讀先前產生的 chars\\<id>.js，回傳 {id: {"id", "name", "title"}}。

    只讀檔頭（id / name / title 都在 payload 最前面），不解析整包 base64，
    所以角色再多也很快。
    """
    existing = {}
    if not chars_dir or not os.path.isdir(chars_dir):
        return existing
    for name in sorted(os.listdir(chars_dir)):
        if not name.endswith(".js"):
            continue
        path = os.path.join(chars_dir, name)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                head = f.read(4096)
        except OSError:
            continue
        match = CHAR_META_RE.search(head)
        if not match:
            continue
        try:
            cid, char_name, title = (json.loads('"' + g + '"') for g in match.groups())
        except ValueError:
            continue
        existing[cid or os.path.splitext(name)[0]] = {
            "id": cid or os.path.splitext(name)[0],
            "name": char_name,
            "title": title,
        }
    return existing


def merge_existing_chars(chars, chars_dir):
    """把既有角色資產疊加到這次的清單，讓 viewer 仍包含之前轉好的角色。

    回傳 (合併後清單, 既有角色 ID 集合)。
    """
    existing = read_existing_chars(chars_dir)
    if not existing:
        return chars, set()
    merged = dict(existing)
    for c in chars:
        merged[c["id"]] = c
    return sorted(merged.values(), key=lambda c: c["id"]), set(existing)


def build_characters(bundles, titles=None, names=None, asset_dir=None):
    chars = []
    seen = set()
    titles = titles or {}
    names = names or {}
    chars_dir = None
    if asset_dir:
        chars_dir = os.path.join(asset_dir, "chars")
        os.makedirs(chars_dir, exist_ok=True)

    for path in bundles:
        title = (titles.get(os.path.abspath(path))
                 or os.path.splitext(os.path.basename(path))[0])
        print(f"[bundle] {title}")
        parts = extract_bundle(path)
        if parts is None:
            print("  [skip] 找不到 .skel / .atlas / Texture2D")
            continue

        char_id = parts["tex_name"]
        if char_id in seen:
            print(f"  [skip] 重複角色：{char_id}")
            continue

        try:
            with tempfile.TemporaryDirectory() as tmp:
                skeleton = skel_to_json(parts["skel"], tmp)
        except Exception as exc:  # noqa: BLE001
            print(f"  [error] skel 轉檔失敗：{exc}")
            continue

        img = parts["image"]
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        tex_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

        char_name = lookup_name(char_id, names)
        record = {
            "id": char_id,
            "name": char_name,
            "title": title,
            "atlas": parts["atlas"],
            "skeleton": skeleton,
            "texture": tex_uri,
        }
        if chars_dir:
            # 分檔模式邊轉邊寫，不要把所有角色的 base64 都留在記憶體裡
            write_character_asset(record, chars_dir)
            chars.append({"id": char_id, "name": char_name, "title": title})
        else:
            chars.append(record)
        seen.add(char_id)

        label = f"{char_id}_{char_name}" if char_name else char_id
        print(f"  [ok] {label}  {img.size[0]}x{img.size[1]}  "
              f"{len(skeleton.get('skins', []))} skins, "
              f"{len(skeleton.get('animations', {}))} animations")

    chars.sort(key=lambda c: c["id"])
    return chars


def render_html(chars, lazy=False, asset_base=""):
    with open(RUNTIME, "r", encoding="utf-8") as f:
        runtime_js = f.read()

    if lazy:
        # viewer 只內嵌清單需要的名稱 / 標題；骨架、atlas、貼圖放在 chars\<id>.js
        char_map = {
            c["id"]: {"id": c["id"], "name": c["name"], "title": c["title"]}
            for c in chars
        }
    else:
        char_map = {c["id"]: c for c in chars}
    ids = [c["id"] for c in chars]
    return (HTML_TEMPLATE
            .replace("__LAZY__", "true" if lazy else "false")
            .replace("__ASSET_BASE__", js_literal(asset_base))
            .replace("__SPINE_WEBGL__", runtime_js)
            .replace("__CHARACTERS_JSON__", js_literal(char_map))
            .replace("__CHAR_IDS_JSON__", js_literal(ids)))


def find_chrome():
    """找系統 Chrome / Edge；找不到就讓 Playwright 用內建 Chromium。"""
    candidates = [
        os.environ.get("SPINE_CHROME"),
        os.environ.get("CHROME_PATH"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    for name in ("chrome", "google-chrome", "chromium", "msedge"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _export_name_info(cid, name):
    """拆出檔名裡的 skin / eye / mouth；同時支援舊版只有 _open/_close 的檔名。"""
    match = re.match(
        r"^" + re.escape(cid) + r"_(.+)_eye_(.+)_mouth_(.+)\.png$", name
    )
    if match:
        return {"skin": match.group(1), "eye": match.group(2),
                "mouth": match.group(3)}
    match = re.match(r"^" + re.escape(cid) + r"_(.+)_(open|close)\.png$", name)
    if match:
        return {"skin": match.group(1), "eye": "", "mouth": match.group(2)}
    return {"skin": name, "eye": "", "mouth": ""}


def _export_duplicate_rank(cid, name, order):
    """重複圖的保留優先序：normal > 其他非 default > default；
    同 skin 時 eye_open > 其他眼睛 > eye_close，mouth_open > 其他嘴巴 > mouth_close。"""
    info = _export_name_info(cid, name)
    skin_key = info["skin"].lower()
    skin_rank = 0 if skin_key == "normal" else (2 if skin_key == "default" else 1)
    eye_rank = 0 if info["eye"] == "open" else (2 if info["eye"] == "close" else 1)
    mouth_rank = 0 if info["mouth"] == "open" else (2 if info["mouth"] == "close" else 1)
    return (skin_rank, eye_rank, mouth_rank, order)


def dedupe_export_entries(cid, entries):
    """同一角色內，CRC32 與內容都相同的圖片只留一張。

    entries 是 [(檔名, 內容 bytes, CRC32), ...]。
    重複時優先保留 normal，其次其他非 default，最後才是 default；
    同一個 skin 內 eye_open 優先於其他眼睛、eye_close 最後，
    mouth_open 優先於其他嘴巴、mouth_close 最後。
    """
    groups = {}
    for order, (name, data, crc) in enumerate(entries):
        groups.setdefault((crc, len(data)), []).append((order, name, data))

    kept = []
    report = []
    for (crc, _size), group in groups.items():
        # 先依 CRC32 + 大小分組，再用實際 bytes 確認，避免 CRC32 碰撞誤刪
        exact = {}
        for order, name, data in group:
            exact.setdefault(data, []).append((order, name))
        for data, items in exact.items():
            items.sort(key=lambda item: _export_duplicate_rank(cid, item[1], item[0]))
            order, keep_name = items[0]
            kept.append((order, keep_name, data, crc))
            if len(items) > 1:
                report.append({
                    "crc32": f"{crc:08x}",
                    "kept": keep_name,
                    "removed": [name for _, name in items[1:]],
                })

    kept.sort(key=lambda item: item[0])
    return [(name, data, crc) for _, name, data, crc in kept], report


def export_index_path(out_zip, viewer_stem):
    """匯出索引的位置：和 ZIP 同資料夾，檔名 <viewer>_export_index.json。"""
    return os.path.join(os.path.dirname(os.path.abspath(out_zip)),
                        viewer_stem + "_export_index.json")


def load_export_index(path):
    """讀匯出索引；不存在或壞掉就當作空的（會重建）。"""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return {}
    return {str(k): str(v) for k, v in files.items()}


def save_export_index(path, index):
    """原子寫入匯出索引（先寫 .part 再改名）。"""
    tmp = path + ".part"
    payload = {
        "format": "已匯出到批量 ZIP 的檔名 -> 來源 ZIP，重跑時用來去重",
        "files": index,
    }
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=0, sort_keys=True)
    os.replace(tmp, path)


def collect_exported_names(out_zip, viewer_stem, index_path):
    """回傳「已匯出檔名 -> 來源 ZIP」，用來避免增量 ZIP 內出現重複檔名。

    來源有兩個：匯出索引 JSON，以及同資料夾裡先前的 *_all*.zip。
    ZIP 只用中央目錄的檔名，不會解壓縮；目前要寫入的 out_zip 不算在內，
    這樣重跑同一個日期時仍會重新匯出。
    """
    exported = load_export_index(index_path)
    # 同名批次（例如同一個日期重跑）不算「已匯出」，否則會整批被跳過
    batch_name = os.path.basename(out_zip)
    exported = {name: src for name, src in exported.items() if src != batch_name}
    if exported:
        print(f"[export] 匯出索引：{os.path.basename(index_path)}"
              f"（{len(exported)} 個檔名）")

    folder = os.path.dirname(os.path.abspath(out_zip))
    current = os.path.abspath(out_zip)
    pattern = os.path.join(folder, glob.escape(viewer_stem) + "_all*.zip")
    for path in sorted(glob.glob(pattern)):
        if os.path.abspath(path) == current:
            continue
        try:
            with zipfile.ZipFile(path) as zf:
                found = [os.path.basename(info.filename)
                         for info in zf.infolist()
                         if not info.is_dir() and info.filename != "index.json"]
        except (OSError, zipfile.BadZipFile) as exc:
            print(f"[warn] 無法讀取舊壓縮檔，已略過：{path}（{exc}）")
            continue
        batch = os.path.basename(path)
        for name in found:
            exported.setdefault(name, batch)
        print(f"[export] 既有壓縮檔：{batch}（{len(found)} 個檔名）")
    return exported


def export_all_characters(viewer_path, chars, out_zip, skip_names=None, index_path=None):
    """用 headless Chrome 逐一把每個角色的批量表情 ZIP 匯出，合併成一個 ZIP。

    skip_names 是先前已經匯出過的檔名；對應的角色會直接跳過，
    新 ZIP 裡就不會出現重複的檔名（增量匯出用）。
    index_path 有給的話，匯出成功後會把這次的檔名併進索引。
    """
    try:
        from playwright.sync_api import sync_playwright
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    except ImportError:
        print("[error] --export-all 需要 Playwright：")
        print("        pip install playwright")
        print("        系統有 Chrome / Edge 的話不需要再 playwright install chromium")
        return 1

    viewer_path = os.path.abspath(viewer_path)
    out_zip = os.path.abspath(out_zip)
    os.makedirs(os.path.dirname(out_zip), exist_ok=True)
    try:
        free_gb = shutil.disk_usage(os.path.dirname(out_zip)).free / (1024 ** 3)
    except OSError:
        free_gb = None
    part_zip = out_zip + ".part"
    try:
        os.remove(part_zip)
    except OSError:
        pass
    chrome = find_chrome()
    skip_names = skip_names or set()
    # 檔名開頭就是角色 ID（<id>_<skin>_...png），已經匯出過的角色整批跳過最快
    existing_ids = {name.split("_", 1)[0] for name in skip_names if "_" in name}
    all_ids = [c["id"] for c in chars]
    total = len(all_ids)
    ids = [cid for cid in all_ids if cid not in existing_ids]
    failures = []
    files_by_char = {}
    duplicates_by_char = {}
    has_normal_by_char = {}
    total_files = 0
    total_removed = 0
    total_default_removed = 0
    started = time.monotonic()

    wait_character_js = r"""
    (id) => {
      var err = document.querySelector("#error");
      if (err && err.style.display !== "none") return "error:" + err.textContent;
      var b = document.querySelector("#char-list button.active");
      var l = document.querySelector("#loading");
      return (b && b.dataset.id === id && l && l.style.display === "none") ? "ok" : false;
    }
    """

    print(f"[export] viewer: {viewer_path}")
    print(f"[export] 輸出:   {out_zip}")
    if free_gb is not None:
        print(f"[export] 磁碟剩餘: {free_gb:.1f} GB")
    if total - len(ids):
        print(f"[export] 略過 {total - len(ids)} 個角色（檔名已在既有壓縮檔 / 索引中）")
    if not ids:
        print("[export] 沒有需要匯出的角色，不產生 ZIP")
        return 0
    print(f"[export] {len(ids)} 個角色；每個角色所有 skin × 開眼/閉眼 × 開口/閉口")
    if len(ids) > 100:
        print("[export] 角色多時會跑很久、ZIP 也會很大；過程中請不要關閉視窗或斷電。")

    browser = None
    try:
        with sync_playwright() as p:
            launch_args = [
                "--use-angle=swiftshader",
                "--enable-webgl",
                "--ignore-gpu-blocklist",
            ]
            launch_kwargs = {"headless": True, "args": launch_args}
            if chrome:
                launch_kwargs["executable_path"] = chrome
            try:
                browser = p.chromium.launch(**launch_kwargs)
            except Exception as exc:  # noqa: BLE001
                print(f"[error] 無法啟動 Chrome/Edge：{exc}")
                if not chrome:
                    print("        可設定環境變數 SPINE_CHROME 指向 chrome.exe，"
                          "或執行 playwright install chromium")
                return 1

            page_errors = []
            console_errors = []
            page = browser.new_page(viewport={"width": 1400, "height": 900},
                                    accept_downloads=True)
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            page.on("console",
                    lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

            try:
                page.goto(Path(viewer_path).as_uri(), wait_until="load", timeout=120000)
                page.wait_for_function(
                    "n => document.querySelectorAll('#char-list button').length === n",
                    arg=total, timeout=60000,
                )
                page.wait_for_function(
                    """() => {
                        var l = document.querySelector('#loading');
                        var e = document.querySelector('#error');
                        return (l && l.style.display === 'none') &&
                               (!!document.querySelector('#char-list button.active') ||
                                (e && e.style.display !== 'none'));
                    }""", timeout=120000,
                )
                err_text = page.eval_on_selector(
                    "#error", "el => el.style.display !== 'none' ? el.textContent : ''")
                if err_text:
                    raise RuntimeError(err_text)
            except Exception as exc:  # noqa: BLE001
                print(f"[error] 開啟 viewer 失敗：{exc}")
                return 1

            with tempfile.TemporaryDirectory(prefix="spine_export_") as tmp:
                part_path = os.path.join(tmp, "character.zip")
                with zipfile.ZipFile(part_zip, "w", compression=zipfile.ZIP_STORED,
                                     allowZip64=True) as zout:
                    for index, cid in enumerate(ids, 1):
                        try:
                            page.evaluate(
                                """id => {
                                    var b = Array.prototype.find.call(
                                        document.querySelectorAll('#char-list button'),
                                        function (x) { return x.dataset.id === id; });
                                    if (b) b.click();
                                }""", cid)
                            state = page.wait_for_function(
                                wait_character_js, arg=cid, timeout=180000
                            ).json_value()
                            if state != "ok":
                                raise RuntimeError(str(state))

                            if os.path.exists(part_path):
                                os.remove(part_path)
                            with page.expect_download(timeout=180000) as download_info:
                                page.click("#btn-batch")
                            download_info.value.save_as(part_path)

                            with zipfile.ZipFile(part_path) as zin:
                                names = [n for n in zin.namelist() if not n.endswith("/")]
                                if not names:
                                    raise RuntimeError("批量 ZIP 內沒有圖片")
                                entries = []
                                for name in names:
                                    data = zin.read(name)
                                    entries.append((name, data, zlib.crc32(data) & 0xFFFFFFFF))

                            skin_names = {
                                _export_name_info(cid, name)["skin"].lower()
                                for name, _data, _crc in entries
                            }
                            has_normal = "normal" in skin_names
                            default_removed = 0
                            if has_normal:
                                filtered = [
                                    item for item in entries
                                    if _export_name_info(cid, item[0])["skin"].lower() != "default"
                                ]
                                default_removed = len(entries) - len(filtered)
                                entries = filtered
                            if not entries:
                                raise RuntimeError("移除 default 後沒有圖片")

                            kept, duplicate_report = dedupe_export_entries(cid, entries)
                            # 保險再過濾一次：同角色部分檔名可能已經在既有 ZIP 裡
                            kept = [item for item in kept if item[0] not in skip_names]
                            if not kept:
                                print(f"[export] {index}/{len(ids)} {cid}: "
                                      "[skip] 檔名都已在既有壓縮檔中")
                                continue
                            for name, data, _crc in kept:
                                zout.writestr(f"{cid}/{name}", data)

                            removed_count = sum(
                                len(item["removed"]) for item in duplicate_report
                            )
                            files_by_char[cid] = len(kept)
                            duplicates_by_char[cid] = duplicate_report
                            has_normal_by_char[cid] = has_normal
                            total_files += len(kept)
                            total_removed += removed_count
                            total_default_removed += default_removed
                            elapsed = time.monotonic() - started
                            notes = []
                            if default_removed:
                                notes.append(f"刪除 default {default_removed} 張")
                            if removed_count:
                                notes.append(f"略過 {removed_count} 張重複")
                            extra = f"，{'、'.join(notes)}" if notes else ""
                            print(f"[export] {index}/{len(ids)} {cid}: "
                                  f"{len(kept)} 張{extra}  ({elapsed:.0f}s)")
                        except PlaywrightTimeoutError as exc:
                            failures.append((cid, f"逾時：{exc}"))
                            print(f"[export] {index}/{len(ids)} {cid}: [error] 逾時")
                        except Exception as exc:  # noqa: BLE001
                            failures.append((cid, str(exc)))
                            print(f"[export] {index}/{len(ids)} {cid}: [error] {exc}")

                    manifest_chars = []
                    for c in chars:
                        cid = c["id"]
                        if cid not in files_by_char:
                            continue
                        item = {
                            "id": cid,
                            "name": c.get("name", ""),
                            "title": c.get("title", ""),
                            "folder": cid,
                            "files": files_by_char[cid],
                            "has_normal": has_normal_by_char.get(cid, False),
                        }
                        if duplicates_by_char.get(cid):
                            item["duplicates_removed"] = duplicates_by_char[cid]
                        manifest_chars.append(item)

                    manifest = {
                        "format": "每個角色一個資料夾；檔名通常為 "
                                  "<id>_<skin>_eye_open_mouth_open.png 等 4 種組合"
                                  "（缺少整組眼睛或嘴巴動畫時使用目前狀態）；"
                                  "有 normal 的角色不輸出 default；"
                                  "同一角色內 CRC32 與內容相同的圖只留一張，"
                                  "重複時優先 normal，其次非 default，"
                                  "同 skin 時 eye_open 優先於 eye_close，"
                                  "mouth_open 優先於 mouth_close",
                        "total_duplicates_removed": total_removed,
                        "characters": manifest_chars,
                        "failed": [{"id": cid, "error": err} for cid, err in failures],
                    }
                    zout.writestr("index.json",
                                  json.dumps(manifest, ensure_ascii=False, indent=2))

            if page_errors:
                print(f"[warn] 頁面 JS 錯誤：{page_errors}")
            if console_errors:
                print(f"[warn] console error：{console_errors}")
    except KeyboardInterrupt:
        print(f"\n[export] 已中止；未完成的檔案：{part_zip}")
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"[error] 匯出失敗：{exc}")
        print(f"        未完成的檔案：{part_zip}")
        return 1
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass

    if total_files == 0:
        try:
            os.remove(part_zip)
        except OSError:
            pass
        print("[error] 沒有匯出任何圖片")
        return 1

    try:
        os.replace(part_zip, out_zip)
    except OSError as exc:
        print(f"[error] 無法寫入 {out_zip}：{exc}")
        print(f"        未完成的檔案：{part_zip}")
        return 1

    if index_path:
        # 把這次真正寫進 ZIP 的檔名併進索引，下次增量匯出就不用再讀 ZIP
        try:
            index = load_export_index(index_path)
            batch = os.path.basename(out_zip)
            with zipfile.ZipFile(out_zip) as zf:
                for info in zf.infolist():
                    if info.is_dir() or info.filename == "index.json":
                        continue
                    index[os.path.basename(info.filename)] = batch
            save_export_index(index_path, index)
            print(f"[export] 索引已更新：{index_path}（{len(index)} 個檔名）")
        except (OSError, zipfile.BadZipFile) as exc:
            print(f"[warn] 更新匯出索引失敗：{exc}")

    size_gb = os.path.getsize(out_zip) / (1024 ** 3)
    notes = []
    if total_default_removed:
        notes.append(f"已刪除 default {total_default_removed} 張")
    if total_removed:
        notes.append(f"已省略 {total_removed} 張重複")
    note = f"（{'、'.join(notes)}）" if notes else ""
    print(f"[done] {len(files_by_char)}/{len(ids)} 個角色，{total_files} 張圖"
          f"{note} -> {out_zip} ({size_gb:.2f} GB)")
    if failures:
        print(f"[warn] {len(failures)} 個角色失敗：")
        for cid, err in failures:
            print(f"        {cid}: {err}")
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser(description="Spine 立繪 → HTML 檢視器")
    ap.add_argument("--bundle", required=True,
                    help="storydata_spine_full_*.unity3d、包含它們的資料夾，"
                         "或下載器的 raw 資料夾（內含 manifest.db）")
    ap.add_argument("--out", default=None,
                    help="輸出 HTML 路徑（預設 <資料夾>\\spine_viewer.html；"
                         "lazy 資產會放在 <檔名>_assets\\）")
    ap.add_argument("--single", action="store_true",
                    help="把所有角色內嵌成單一 HTML（角色少時用；預設 lazy 分檔）")
    ap.add_argument("--export-all", action="store_true",
                    help="產生 lazy viewer 後，用 headless Chrome 匯出所有角色"
                         "（每個 skin 含開眼/閉眼 × 開口/閉口）成一個 ZIP")
    ap.add_argument("--export-out", default=None,
                    help="--export-all 的輸出 ZIP 路徑"
                         "（預設 <viewer檔名>_all_<YYMMDD>.zip）")
    ap.add_argument("--filter", default="storydata_spine_full_*",
                    help="只收檔名符合此 glob 的 bundle（預設 storydata_spine_full_*；"
                         "給空字串則全部收）")
    ap.add_argument("--since", default=None,
                    help="只處理指定日期之後（依檔案修改時間）的檔案，"
                         "例如 261031 = 2026-10-31 00:00:00 之後；"
                         "lazy 模式會疊加既有 chars\\ 資產，viewer 仍包含舊角色")
    ap.add_argument("--names", default=None,
                    help="角色名稱 JSON（id 或前 4 碼 base id -> 名稱）。"
                         "預設自動讀 <bundle 資料夾>\\names.json")
    ap.add_argument("--no-export-dedupe", action="store_true",
                    help="完整匯出，不跳過既有壓縮檔 / 匯出索引裡已有的檔名")
    args = ap.parse_args()

    if args.export_out and not args.export_all:
        ap.error("--export-out 必須搭配 --export-all")
    if args.export_all and args.single:
        ap.error("--export-all 需要 lazy 分檔模式，請不要加 --single")

    since_ts = None
    if args.since:
        try:
            since_dt = parse_since(args.since)
        except ValueError as exc:
            ap.error(f"--since {exc}")
        since_ts = since_dt.timestamp()

    target = os.path.abspath(args.bundle)
    titles = {}
    raw_result = None
    if os.path.isdir(target):
        files = sorted(glob.glob(os.path.join(target, "*.unity3d")))
        base_dir = target
        if not files:
            # 下載器產生的 raw 佈局：<base>\a\<40碼hash> + <base>\manifest\*
            raw_result = resolve_raw_bundles(target, args.filter or "*")
            if raw_result is not None:
                base_dir, pairs = raw_result
                files = [path for path, _ in pairs]
                titles = {
                    os.path.abspath(path): os.path.splitext(os.path.basename(logical))[0]
                    for path, logical in pairs
                }
    elif os.path.isfile(target):
        files = [target]
        base_dir = os.path.dirname(target)
    else:
        files = sorted(glob.glob(target))
        base_dir = os.path.dirname(files[0]) if files else os.getcwd()

    if args.filter and raw_result is None:
        files = [p for p in files if fnmatch.fnmatch(os.path.basename(p), args.filter)]

    if since_ts is not None:
        before = len(files)
        files = [p for p in files if file_mtime(p) >= since_ts]
        print(f"[since] 只處理 {since_dt:%Y-%m-%d %H:%M:%S} 之後（含）的檔案："
              f"{len(files)}/{before}")

    if not files:
        if raw_result is not None:
            print("[error] raw 資料夾裡找不到符合條件的 bundle")
            print(f"        manifest 裡沒有符合 {args.filter!r} 的項目，或對應檔案尚未下載")
        else:
            print("[error] 找不到任何符合的 .unity3d")
            print("        若資料夾裡不是 storydata_spine_full_*，可加 --filter \"\" 或 --filter \"spine_full_*\"")
        if since_ts is not None:
            print(f"        也可能是都被 --since {args.since} 篩掉了")
        return 1

    names_path = args.names
    if not names_path:
        candidate = os.path.join(base_dir, "names.json")
        if os.path.isfile(candidate):
            names_path = candidate
    try:
        names = load_names(names_path)
    except Exception as exc:  # noqa: BLE001
        print(f"[error] 讀取名稱檔失敗：{names_path}\n        {exc}")
        return 1
    if names:
        print(f"[names] {len(names)} 筆 <- {names_path}")

    ensure_tool(RUNTIME_URL, RUNTIME, "Spine 3.8.95 runtime")
    ensure_tool(CONVERTER_URL, CONVERTER, "SpineSkeletonDataConverter")

    out_path = os.path.abspath(args.out) if args.out else os.path.join(base_dir, "spine_viewer.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    asset_dir = None if args.single else os.path.splitext(out_path)[0] + "_assets"

    if args.single and len(files) > 100:
        print(f"[warn] --single 會嘗試把 {len(files)} 個角色全部內嵌，"
              "檔案可能非常大且啟動很慢")

    # 分檔模式在 build 時就逐一把角色寫到 chars\，避免 500 角色的 base64 全卡在記憶體
    built = build_characters(files, titles, names, asset_dir=asset_dir)
    chars = built
    if asset_dir:
        # 增量模式只會轉出新角色；把之前轉好的 chars\<id>.js 疊回來，
        # 這樣更新後的 viewer 仍然包含全部角色
        merged, existing_ids = merge_existing_chars(built, os.path.join(asset_dir, "chars"))
        old_ids = existing_ids - {c["id"] for c in built}
        if old_ids:
            print(f"[stack] 疊加 {len(old_ids)} 個既有角色，"
                  f"viewer 共 {len(merged)} 個角色")
        chars = merged

    if not chars:
        print("[error] 沒有任何角色成功轉換")
        return 1
    if asset_dir and not built:
        print("[warn] 這次沒有轉出新角色，viewer 沿用既有資產")

    if args.single:
        html = render_html(chars, lazy=False)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        size_mb = os.path.getsize(out_path) / (1024 * 1024)
        print(f"\n[done] {len(chars)} 個角色 -> {out_path}  ({size_mb:.1f} MB, single HTML)")
    else:
        asset_base = os.path.basename(asset_dir) + "/"
        asset_bytes = sum(
            os.path.getsize(os.path.join(asset_dir, "chars", c["id"] + ".js"))
            for c in chars
        )
        html = render_html(chars, lazy=True, asset_base=asset_base)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        viewer_mb = os.path.getsize(out_path) / (1024 * 1024)
        asset_mb = asset_bytes / (1024 * 1024)
        print(f"\n[done] {len(chars)} 個角色 -> {out_path}  ({viewer_mb:.2f} MB, lazy viewer)")
        print(f"       角色資產 -> {asset_dir}  ({asset_mb:.1f} MB, {len(chars)} 個 .js)")
        print("       開啟 HTML 時只會載入目前角色；HTML 和 _assets 資料夾必須一起移動。")

    if args.export_all:
        viewer_stem = os.path.splitext(os.path.basename(out_path))[0]
        if args.export_out:
            export_zip = os.path.abspath(args.export_out)
        else:
            # 預設把日期放進檔名，例如 spine_viewer_all_261031.zip
            # （--since 用指定日期，沒給就用今天）
            day = since_dt if since_ts is not None else datetime.now()
            export_zip = os.path.splitext(out_path)[0] + f"_all_{day:%y%m%d}.zip"
        skip_names = None
        index_path = None
        if not args.no_export_dedupe:
            index_path = export_index_path(export_zip, viewer_stem)
            skip_names = collect_exported_names(export_zip, viewer_stem, index_path)
            print(f"[export] 已匯出 {len(skip_names)} 個檔名，重複的會自動略過")
        rc = export_all_characters(out_path, chars, export_zip,
                                   skip_names=skip_names, index_path=index_path)
        if rc != 0:
            return rc

    print("       直接用 Chrome / Edge 開啟即可，不需要伺服器。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
