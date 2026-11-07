#!/usr/bin/env python3
r"""
一鍵找出資料夾裡的「profile_」相關 asset bundle，輸出 PNG。

適用於下載器產生的「檔名是雜湊」的資料夾（例如 test\a\c75286a1...）：
  1. 讀 manifest.db (key = 原始路徑, value = xxHash64) 反查每個檔案的原始名稱
  2. 依名稱篩選出 profile_ 資源後解出 Texture2D
  3. 維持原生尺寸輸出 PNG

沒有 manifest.db 也能用：會直接掃 UnityFS 檔頭，檢視內部貼圖名稱。

用法：
  python profile_extractor.py                       # 自動搜尋並轉檔
  python profile_extractor.py --root test           # 指定來源目錄
  python profile_extractor.py --root test --list    # 僅列出符合檔案，不轉檔
  python profile_extractor.py --ids 1001,1002       # 僅處理特定 ID
  python profile_extractor.py --overwrite          # 覆蓋已存在的同名檔案
"""

import argparse
import os
import sqlite3
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

import UnityPy
from PIL import Image

warnings.filterwarnings("ignore")

try:  # Windows 主控台編碼相容
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

UnityPy.config.FALLBACK_UNITY_VERSION = "6000.0.75f1"

UNITYFS_MAGIC = b"UnityFS"
SKIP_DIRS = {"profile_out", "comic_out", "__pycache__", ".git"}
VALID_KINDS = ("profile",)


# --------------------------------------------------------------------------- #
# 尋找目錄 / 讀取 manifest
# --------------------------------------------------------------------------- #
def find_root(explicit):
    if explicit:
        return os.path.abspath(explicit)

    here = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__))
    candidates = [os.getcwd(), here, os.path.join(here, "test"), os.path.dirname(here)]

    best, best_score = None, -1
    for c in candidates:
        if not os.path.isfile(os.path.join(c, "manifest.db")):
            continue
        score = 1
        if os.path.isdir(os.path.join(c, "a")):
            score += 1
        if score > best_score:
            best, best_score = c, score
    return best or here


def load_hash_map(root):
    """xxHash64(hex) -> 原始路徑"""
    db_path = os.path.join(root, "manifest.db")
    result = {}
    if not os.path.isfile(db_path):
        return result
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            for key, value in conn.execute("SELECT k, v FROM t"):
                if isinstance(value, str) and len(value) == 16:
                    result[value.lower()] = key.replace("\\", "/")
    except sqlite3.Error as exc:
        print(f"[warn] 讀取 manifest.db 失敗：{exc}")
    return result


def xxh64(path, chunk_size=1 << 20):
    import xxhash

    h = xxhash.xxh64()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# 分類 / 收集 bundle
# --------------------------------------------------------------------------- #
def classify(name):
    """判斷是否為 profile_ 檔案"""
    low = name.lower()
    if "profile_" in low:
        return "profile"
    return None


def is_unityfs(path):
    try:
        with open(path, "rb") as f:
            return f.read(8).startswith(UNITYFS_MAGIC)
    except OSError:
        return False


def texture_names(path):
    env = UnityPy.load(path)
    names = []
    for obj in env.objects:
        if obj.type.name == "Texture2D":
            names.append(obj.read().m_Name)
    return names


def collect(root, hash_map, scan_all=False, exclude=None):
    """回傳 [(原始名稱, 實體路徑, 類型)]"""
    found = {}
    skip_abs = {os.path.abspath(exclude)} if exclude else set()

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS and os.path.abspath(os.path.join(dirpath, d)) not in skip_abs
        ]
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            stem, ext = os.path.splitext(fn)
            name = None

            # 1) 原始檔名比對
            if "profile_" in stem.lower() and ext.lower() in (".unity3d", ".ab", ".bundle"):
                name = fn
            # 2) 透過 manifest.db 反查
            elif hash_map:
                if len(stem) == 16 and stem.lower() in hash_map:
                    name = os.path.basename(hash_map[stem.lower()])
                else:
                    name = os.path.basename(hash_map.get(xxh64(path), "")) or None

            # 3) 直接掃描 UnityFS 內容中的貼圖名稱
            if name is None and (scan_all or not hash_map):
                if is_unityfs(path):
                    try:
                        names = texture_names(path)
                    except Exception:
                        names = []
                    guess = [n for n in names if "profile_" in n.lower()]
                    if guess:
                        name = guess[0] + ".unity3d"

            if not name:
                continue

            kind = classify(name)
            if kind != "profile":
                continue

            found[name] = (path, kind)

    return sorted((n, p, k) for n, (p, k) in found.items())


# --------------------------------------------------------------------------- #
# 轉檔處理
# --------------------------------------------------------------------------- #
def pick_texture(path):
    """取出 bundle 內面積最大的 Texture2D 物件"""
    env = UnityPy.load(path)
    best = None
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        data = obj.read()
        if best is None or data.m_Width * data.m_Height > best.m_Width * best.m_Height:
            best = data
    return best


def convert(path, out_dir, overwrite=False):
    """提取並儲存 PNG，保持原尺寸"""
    data = pick_texture(path)
    if data is None:
        return None

    src_size = (data.m_Width, data.m_Height)
    base = f"{data.m_Name}_{src_size[0]}x{src_size[1]}"
    out_path = os.path.join(out_dir, f"{base}.png")

    if os.path.exists(out_path) and not overwrite:
        return src_size, [], [out_path]

    img = data.image
    if img.mode == "RGBA" and img.getchannel("A").getextrema() == (255, 255):
        img = img.convert("RGB")

    os.makedirs(out_dir, exist_ok=True)
    img.save(out_path, optimize=True)

    return src_size, [out_path], []


# --------------------------------------------------------------------------- #
# 主執行流程
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="找出含有 profile_ 的 Unity Bundle 並輸出 PNG")
    ap.add_argument("--root", help="資料夾路徑（包含 manifest.db 或 asset 檔案）")
    ap.add_argument("--out", default=None, help="輸出資料夾（預設為 <root>\\profile_out）")
    ap.add_argument("--ids", default=None, help="只處理指定識別字串或 ID，以逗號分隔（如：101,102）")
    ap.add_argument("--overwrite", action="store_true", help="覆蓋已存在的圖檔")
    ap.add_argument("--list", action="store_true", help="僅列出符合項目，不進行轉檔")
    ap.add_argument("--scan", action="store_true", help="無條件逐檔解析檔頭（沒有 manifest.db 時自動啟用）")
    args = ap.parse_args()

    root = find_root(args.root)
    out_dir = os.path.abspath(args.out) if args.out else os.path.join(root, "profile_out")

    print(f"[root] {root}")
    if not os.path.isdir(root):
        print("[error] 找不到指定資料夾")
        return 1

    hash_map = load_hash_map(root)
    print(f"[manifest] {'找到 ' + str(len(hash_map)) + ' 筆紀錄' if hash_map else '未找到 manifest.db，改用檔案內容掃描'}")
    if not hash_map:
        args.scan = True

    items = collect(root, hash_map, scan_all=args.scan, exclude=out_dir)

    if args.ids:
        wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
        items = [(n, p, k) for n, p, k in items if any(w in n for w in wanted)]

    if not items:
        print("[info] 未找到任何符合 profile_ 的 bundle")
        return 0

    print(f"[found] 找到 {len(items)} 個符合 profile_ 的 bundle\n")
    for name, path, _ in items:
        print(f"  [profile] {name}\n      <- {path}")

    if args.list:
        return 0

    print()
    done = added = skipped = 0
    workers = max(1, (os.cpu_count() or 1) // 2)
    print(f"[threads] 使用 {workers} 個背景執行緒")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(convert, path, out_dir, args.overwrite): name
            for name, path, _ in items
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                res = future.result()
            except Exception as exc:  # noqa: BLE001
                print(f"[error] {name}: {exc}")
                continue

            if res is None:
                print(f"[skip] {name}: 未找到 Texture2D 物件")
                continue

            src_size, written, skipped_files = res
            done += 1
            added += len(written)
            skipped += len(skipped_files)

            if written:
                print(f"[ok] {name} [{src_size[0]}x{src_size[1]}] -> {os.path.basename(written[0])}")
            else:
                print(f"[skip] {name} 已存在，跳過")

    print(f"\n處理完成：共 {done}/{len(items)} 個，新增 {added} 檔、略過 {skipped} 檔")
    print(f"輸出目錄：{out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())