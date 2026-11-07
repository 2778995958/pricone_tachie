#!/usr/bin/env python3
r"""
一鍵找出資料夾裡的「漫畫」與「單位背景圖」asset bundle，輸出 PNG。

  comic_comic_l_*.unity3d   → 512x512 橫向壓扁，還原成 682x512
  bg_still_unit_*.unity3d   → 原生 1408x792（16:9），不縮放

適用於下載器產生的「檔名是雜湊」的資料夾（例如 test\a\c75286a1...）：
  1. 讀 manifest.db (key = 原始路徑, value = xxHash64) 反查每個檔案的原始名稱
  2. 依名稱分類後解出 Texture2D
  3. 漫畫還原 4:3，其餘保持原尺寸，輸出 PNG

沒有 manifest.db 也能用：會直接掃 UnityFS 檔頭，看裡面是什麼貼圖。

用法：
  python comic_extractor.py                       # 漫畫 + 背景圖，自動找資料夾
  python comic_extractor.py --types comic         # 只轉漫畫
  python comic_extractor.py --types still         # 只轉 bg_still_unit
  python comic_extractor.py --root test --list    # 只列出，不轉檔
  python comic_extractor.py --ids 140901,140931   # 只處理指定 ID
  python comic_extractor.py --since 261031        # 只處理 2026-10-31 00:00:00 之後的檔案
  python comic_extractor.py --since 261031T1200   # 只處理 2026-10-31 12:00:00 之後的檔案
  python comic_extractor.py --skip-list other.txt # 和指定清單的檔名去重（預設讀 comic_out\list.txt）
  python comic_extractor.py --no-skip-list        # 不看清單，只跳過輸出資料夾已存在的檔
  python comic_extractor.py --raw                 # 漫畫保留原始 512x512
  python comic_extractor.py --include-thumb       # 連 comic_s 縮圖一起
  python comic_extractor.py --overwrite           # 覆蓋同名檔案（預設跳過已存在的）

輸出檔名固定為「貼圖名_寬x高.png」，輸出資料夾已有同名檔時直接跳過，
可重複執行、只補缺的檔案。

--since 用檔案修改時間篩選，日期寫成 YYMMDD（例如 261031 = 2026-10-31），
8 碼則視為 YYYYMMDD；沒給時間就以當天 00:00:00 為界，也可再加上
T / _ / - / 空白 + HHMM、HH:MM、HHMMSS 指定時刻（例如 261031T1200）。

去重比對兩層：
  1. 輸出資料夾根目錄已有同名檔 → 跳過
  2. <out>\list.txt（tree.bat 的 dir /s 清單）出現過的檔名 → 跳過，
     所以檔案被搬進 comic\、unit\ 等子資料夾後也不會重複輸出到根目錄。
     清單每行可以是完整路徑，只取最後一段檔名比對；--no-skip-list 可關閉。
"""

import argparse
import os
import re
import sqlite3
import sys
import warnings
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import UnityPy
from PIL import Image

warnings.filterwarnings("ignore")

try:  # Windows 主控台用 cp950 時避免中文輸出炸掉
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# bundle 標頭第一個字串 "5.x.x" 是固定值，第二個才是真正的引擎版本；
# 這批檔案的 revision 被歸零成 "0.0.0"，所以只能靠 fallback 指定。
# 依遊戲內保留原始 revision 的檔案，實際版本是 Unity 6000.0.75f1。
# （2020.3.34 ~ 6000.x 對這批圖解出來完全相同，指定真實版本是為了其他資產型別。）
UnityPy.config.FALLBACK_UNITY_VERSION = "6000.0.75f1"

UNITYFS_MAGIC = b"UnityFS"
SKIP_DIRS = {"comic_out", "__pycache__", ".git"}
VALID_KINDS = ("comic", "still", "thumb")

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
# 找資料夾 / 讀 manifest
# --------------------------------------------------------------------------- #
def find_root(explicit):
    if explicit:
        return os.path.abspath(explicit)

    here = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__))
    # 目前所在資料夾優先（若裡面有 manifest.db），其次是腳本所在資料夾、test\、上層
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
    """xxHash64(hex) -> 原始路徑，例如 'd1f1c02eb6cc458e' -> 'a/comic_comic_l_140901_01.unity3d'"""
    db_path = os.path.join(root, "manifest.db")
    result = {}
    if not os.path.isfile(db_path):
        return result
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            for key, value in conn.execute("SELECT k, v FROM t"):
                if isinstance(value, str) and len(value) == 16:  # xxHash64
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


def load_known_names(path):
    """讀清單檔（例如 comic_out\\list.txt），回傳已存在的檔名集合。

    清單每行可能是完整路徑（dir /s 的輸出），只取最後一段檔名比對；
    空行、資料夾結尾、註解（# 開頭）都忽略。
    """
    known = set()
    if not path or not os.path.isfile(path):
        return known
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                name = line.strip().strip('"')
                if not name or name.startswith("#") or name.endswith(("\\", "/")):
                    continue
                name = os.path.basename(name.replace("\\", "/"))
                if name:
                    known.add(name)
    except OSError as exc:
        print(f"[warn] 讀取清單失敗：{path}（{exc}）")
    return known


# --------------------------------------------------------------------------- #
# 分類 / 找 bundle
# --------------------------------------------------------------------------- #
def classify(name):
    """依 bundle／貼圖名稱判斷類型。"""
    low = name.lower()
    if "comic_l_" in low:
        return "comic"
    if "comic_s_" in low:
        return "thumb"
    if "still_" in low:
        return "still"
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


def collect(root, hash_map, scan_all=False, include_thumb=False, kinds=("comic", "still"), exclude=None,
            since=None):
    """回傳 [(原始名稱, 實體路徑, 類型)]；since 為時間戳時只收修改時間不早於它的檔案。"""
    found = {}
    skip_abs = {os.path.abspath(exclude)} if exclude else set()

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS and os.path.abspath(os.path.join(dirpath, d)) not in skip_abs
        ]
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            if since is not None and file_mtime(path) < since:
                continue
            stem, ext = os.path.splitext(fn)
            name = None

            # 1) 檔名本來就是原名
            if "_" in stem and ext.lower() in (".unity3d", ".ab", ".bundle"):
                name = fn
            # 2) 用 manifest.db 反查（先試檔名就是 hash 的情況，再實際算）
            elif hash_map:
                if len(stem) == 16 and stem.lower() in hash_map:
                    name = os.path.basename(hash_map[stem.lower()])
                else:
                    name = os.path.basename(hash_map.get(xxh64(path), "")) or None

            # 3) 直接看內容
            if name is None and (scan_all or not hash_map):
                if is_unityfs(path):
                    try:
                        names = texture_names(path)
                    except Exception:
                        names = []
                    guess = [n for n in names if n.lower().startswith(("comic_", "still_unit_"))]
                    if guess:
                        name = guess[0] + ".unity3d"

            if not name:
                continue

            kind = classify(name)
            if kind is None or kind not in kinds:
                continue
            if kind == "thumb" and not include_thumb:
                continue

            found[name] = (path, kind)

    return sorted((n, p, k) for n, (p, k) in found.items())


# --------------------------------------------------------------------------- #
# 轉檔
# --------------------------------------------------------------------------- #
def pick_texture(path):
    """取 bundle 中面積最大的 Texture2D（只讀中繼資料，不先解碼）。"""
    env = UnityPy.load(path)
    best = None
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        data = obj.read()
        if best is None or data.m_Width * data.m_Height > best.m_Width * best.m_Height:
            best = data
    return best


def convert(path, kind, out_dir, width, height, formats, overwrite=False, known=None):
    """回傳 (來源尺寸, 輸出尺寸, 已寫入清單, 已跳過清單)；找不到貼圖回傳 None。

    known 是清單檔裡已有的檔名（例如 comic_out\\list.txt），命中就不重複輸出。
    """
    data = pick_texture(path)
    if data is None:
        return None

    src_size = (data.m_Width, data.m_Height)
    # 漫畫是橫向壓扁的 4:3，要還原；背景圖本來就是正確比例，保持原尺寸。
    target = (width, height) if kind == "comic" else src_size
    base = f"{data.m_Name}_{target[0]}x{target[1]}"

    # 先看輸出資料夾與清單檔，已存在（或清單已有）的同名檔直接跳過，連解碼都省下來。
    written, skipped, pending = [], [], []
    for fmt in formats:
        out_path = os.path.join(out_dir, base + "." + fmt)
        name = os.path.basename(out_path)
        if not overwrite and (os.path.exists(out_path) or (known and name in known)):
            skipped.append(out_path)
        else:
            pending.append((fmt, out_path))

    if pending:
        img = data.image
        out = img if target == src_size else img.resize(target, Image.LANCZOS)
        if out.mode == "RGBA" and out.getchannel("A").getextrema() == (255, 255):
            out = out.convert("RGB")

        os.makedirs(out_dir, exist_ok=True)
        for fmt, out_path in pending:
            out.save(out_path, optimize=True)
            written.append(out_path)

    return src_size, target, written, skipped


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="找出 comic / bg_still_unit bundle，輸出 PNG")
    ap.add_argument("--root", help="資料夾（內含 manifest.db 與 a\\ 或雜湊檔名檔案）")
    ap.add_argument("--out", default=None, help="輸出資料夾（預設 <root>\\comic_out）")
    ap.add_argument("--types", default="comic,still", help="要輸出的類型：comic,still / comic / still（預設兩者）")
    ap.add_argument("--ids", default=None, help="只處理指定 ID，例如 140901,140931")
    ap.add_argument("--since", default=None,
                    help="只處理指定日期之後（依檔案修改時間）的檔案，"
                         "例如 261031 = 2026-10-31 00:00:00 之後")
    ap.add_argument("--skip-list", default="auto", metavar="PATH",
                    help="要和這份清單的檔名去重（每行一個路徑或檔名，只比對檔名）；"
                         "預設 auto = 讀 <out>\\list.txt（存在才用），"
                         "給空字串則不讀")
    ap.add_argument("--no-skip-list", action="store_true",
                    help="不讀 list.txt，只跳過輸出資料夾已存在的檔案")
    ap.add_argument("--include-thumb", action="store_true", help="連 comic_s 縮圖一起輸出")
    ap.add_argument("--raw", action="store_true", help="漫畫保留原始 512x512，不還原比例")
    ap.add_argument("--width", type=int, default=682, help="漫畫還原寬度（預設 682 = 512*4/3）")
    ap.add_argument("--height", type=int, default=512)
    ap.add_argument("--format", default="png", help="輸出格式（只支援 png）")
    ap.add_argument("--overwrite", action="store_true", help="覆蓋已存在的輸出（預設跳過同名檔案）")
    ap.add_argument("--list", action="store_true", help="只列出，不轉檔")
    ap.add_argument("--scan", action="store_true", help="強制掃描檔案內容（沒有 manifest.db 時）")
    args = ap.parse_args()

    kinds = tuple(x.strip() for x in args.types.split(",") if x.strip())
    unknown = [k for k in kinds if k not in VALID_KINDS]
    if unknown or not kinds:
        print(f"[error] --types 只接受 {', '.join(VALID_KINDS)}")
        return 2

    formats = [x.strip().lower() for x in args.format.split(",") if x.strip()]
    bad_formats = [f for f in formats if f != "png"]
    if bad_formats or not formats:
        print("[error] --format 只接受 png")
        return 2

    root = find_root(args.root)
    out_dir = os.path.abspath(args.out) if args.out else os.path.join(root, "comic_out")
    width, height = (512, 512) if args.raw else (args.width, args.height)

    since_ts = None
    if args.since:
        try:
            since_dt = parse_since(args.since)
        except ValueError as exc:
            print(f"[error] --since {exc}")
            return 2
        since_ts = since_dt.timestamp()

    print(f"[root] {root}")
    if not os.path.isdir(root):
        print("[error] 找不到資料夾")
        return 1

    if since_ts is not None:
        print(f"[since] 只處理 {since_dt:%Y-%m-%d %H:%M:%S} 之後（含）的檔案")

    # list.txt：comic_out 的 dir /s 清單（tree.bat 產生）。裡面的檔名代表已經匯出過，
    # 就算檔案被搬到分類子資料夾，也不會再重複輸出到根目錄。
    known = set()
    if not args.no_skip_list and args.skip_list:
        list_path = args.skip_list
        if list_path == "auto":
            list_path = os.path.join(out_dir, "list.txt")
        if os.path.isfile(list_path):
            known = load_known_names(list_path)
            print(f"[skip-list] 載入 {len(known)} 個檔名 <- {list_path}")
        elif args.skip_list != "auto":
            print(f"[warn] 找不到清單檔：{list_path}")

    hash_map = load_hash_map(root)
    print(f"[manifest] {'找到 ' + str(len(hash_map)) + ' 筆' if hash_map else '沒有 manifest.db，改用內容掃描'}")
    if not hash_map:
        args.scan = True

    items = collect(
        root, hash_map,
        scan_all=args.scan,
        include_thumb=args.include_thumb,
        kinds=kinds,
        exclude=out_dir,
        since=since_ts,
    )

    if args.ids:
        wanted = {x.strip() for x in args.ids.split(",")}
        items = [(n, p, k) for n, p, k in items if any(w in n for w in wanted)]

    if not items:
        print("[info] 沒有找到符合的 bundle"
              + ("（可能都被 --since 篩掉了）" if since_ts is not None else ""))
        return 0

    counts = Counter(k for _, _, k in items)
    summary = "、".join(f"{k} {counts[k]}" for k in VALID_KINDS if counts[k])
    print(f"[found] {len(items)} 個 bundle（{summary}）\n")
    for name, path, kind in items:
        print(f"  [{kind}] {name}\n      <- {path}")
    if args.list:
        return 0

    print()
    done = added = skipped = 0
    workers = max(1, (os.cpu_count() or 1) // 2)
    print(f"[threads] {workers}（CPU 執行緒的一半）")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(convert, path, kind, out_dir, width, height, formats,
                            args.overwrite, known): (name, kind)
            for name, path, kind in items
        }
        for future in as_completed(futures):
            name, kind = futures[future]
            try:
                res = future.result()
            except Exception as exc:  # noqa: BLE001
                print(f"[error] {name}: {exc}")
                continue
            if res is None:
                print(f"[skip] {name}: 找不到 Texture2D")
                continue

            src_size, out_size, written, skipped_files = res
            done += 1
            added += len(written)
            skipped += len(skipped_files)

            if written:
                print(f"[ok] {name}  [{kind}] {src_size[0]}x{src_size[1]} -> {out_size[0]}x{out_size[1]}")
                for p in written:
                    print(f"      + {os.path.basename(p)}")
                for p in skipped_files:
                    print(f"      = {os.path.basename(p)}（已存在或清單已有，跳過）")
            else:
                print(f"[skip] {name}  已存在或清單已有，跳過（{len(skipped_files)} 檔）")

    print(f"\n完成 {done}/{len(items)}：新增 {added} 檔、跳過 {skipped} 檔")
    print(f"輸出資料夾：{out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
