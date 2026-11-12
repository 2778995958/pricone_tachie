# -*- coding: utf-8 -*-
"""把 m 資料夾內的 .usm 影片轉成 m_out 內的 .mp4（同內容只轉一次）。

流程
    1. 掃描 m（含子資料夾）內所有 .usm
    2. 依「大小 → MD5」分組，同內容的檔案只轉一次
    3. 用 usmtoolkit\\UsmToolkit.exe 轉檔
    4. 音軌改用正確金鑰重解（整條無聲就直接不放），影片重編成 H.264 並寫入 BT.601 色彩標記
    5. 同組其他檔名直接複製轉好的 mp4，不重複轉
    6. 已存在且帶有版本標記（= 已用新格式轉過）的 mp4 一律跳過（可中斷後重跑）

為什麼要重編影片
    電腦版（DMM）的 USM 內是 MPEG-1，串流本身沒有任何色彩描述；
    顯示卡硬解（DXVA）對 MPEG-1/2 會固定用 BT.709 轉 RGB，於是開啟
    「硬體解碼」就整片偏色（iOS 版是 H.264，沒有這個問題）。
    實測只有把影片轉成 H.264 才能讓軟解／硬解結果一致，故預設重編。

用法
    python "03. USM to MP4.py"                          # 轉整個 m
    python "03. USM to MP4.py" "m\\t\\event_1000101.usm" # 只轉單一檔案
    python "03. USM to MP4.py" --src m\\t --out m_out_t  # 指定來源／輸出資料夾
    python "03. USM to MP4.py" --dry-run                # 只統計，不轉檔
    python "03. USM to MP4.py" --workers 4              # 調整同時轉檔數
    python "03. USM to MP4.py" --crf 20 --preset medium # 調整 H.264 畫質／速度
    python "03. USM to MP4.py" --encoder h264_nvenc     # 改用顯卡編碼（較快）
    python "03. USM to MP4.py" --copy-video             # 不重編，只補色彩標記（快，但 MPEG-1 硬解仍會偏色）
    python "03. USM to MP4.py" --no-compat              # 保留原始 AC-3 音軌與 MPEG-1 影片
    python "03. USM to MP4.py" --force                  # 忽略既有 mp4 重新轉
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tqdm import tqdm

# ===================== 基本設定 =====================
BASE = Path(__file__).resolve().parent
SRC_DIR = BASE / "m"
OUT_DIR = BASE / "m_out"
TOOL_DIR = BASE / "usmtoolkit"
USM_TOOL = TOOL_DIR / "UsmToolkit.exe"
FFMPEG = TOOL_DIR / "ffmpeg.exe"
VGMSTREAM = TOOL_DIR / "vgmstream" / "test.exe"

# Princess Connect Re:Dive 的 HCA 金鑰。
# UsmToolkit 內建的 vgmstream 金鑰清單沒有這一組，會把加密 HCA 解成雜訊（沙沙聲）；
# 這裡改用 vgmstream CLI 搭配 .hcakey 強制指定金鑰後重新解碼音軌。
HCA_KEY = "000000000030D9E8"
FIX_HCA = True

TMP_ROOT = BASE / "_usm_tmp"          # 轉檔暫存（每個工作一個子資料夾）
CACHE_PATH = BASE / "_usm_hash_cache.json"  # MD5 快取，避免每次重算 70GB

CPU_COUNT = os.cpu_count() or 4
WORKERS = max(1, CPU_COUNT // 2)  # 預設用一半核心；每個 ffmpeg 自己也會多執行緒
RETRY = 2        # 單檔失敗重試次數
TIMEOUT = 3600   # 單檔轉檔逾時（秒）

COMPAT_MODE = True   # True：音訊轉 AAC 並加上 faststart，硬解／瀏覽器相容性最好
AUDIO_BITRATE = "192k"
FORCE_CFR = True     # 把時間戳重寫成固定幀率（PotPlayer 之類的 DXVA 對 VFR 很敏感）
CLEAN_TEMP = True    # 轉完順手刪掉 UsmToolkit 留下的 .m2v / .wav / .adx

# ---- 影片色彩／編碼 ----
# 電腦版 USM 內是 MPEG-1（TMPGEnc 壓的 Sofdec），串流沒有任何色彩描述，
# 顯示卡硬解會固定當成 BT.709 → 開 H/W 就偏色；實測要重編成 H.264 才會正常。
REENCODE_VIDEO = True
VIDEO_CODEC = "libx264"
VIDEO_PRESET = "veryfast"
VIDEO_CRF = 18
# USM 內容實際是 BT.601 矩陣 + TV range（16-235），這裡明確寫進 VUI／colr，
# 讓軟解、硬解都拿到同一組參數，不再靠解析度猜。
COLOR_MATRIX = "smpte170m"
COLOR_PRIMARIES = "bt709"
COLOR_TRC = "bt709"
COLOR_RANGE = "tv"
# 寫進 mp4 udta/comment 的版本標記，用來辨識「已經是新格式」的成品
OUTPUT_TAG = "USM2MP4 h264-601 v3"

# 音軌幾乎無聲時直接不放音軌。
# 遊戲影片常有一整條「全零」的 ADX（v3 沒有音樂，BGM 由遊戲引擎另外播），
# 量化雜訊只有 -50 dB，播放器若開音量正規化就會把它放大成沙沙聲。
DROP_SILENT_AUDIO = True
SILENT_DB = -45.0
# ====================================================


stop_event = threading.Event()
print_lock = threading.Lock()


def log(msg: str):
    with print_lock:
        print(msg, flush=True)


def sigint_handler(sig, frame):
    log("\n[CTRL+C] 偵測中斷，等待進行中的檔案結束後停止（可續跑）")
    stop_event.set()


signal.signal(signal.SIGINT, sigint_handler)


# ===================== MD5 快取 =====================
def load_cache() -> dict:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_cache(cache: dict):
    try:
        CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass


def md5_of(path: Path, cache: dict) -> str:
    """取檔案 MD5，大小與 mtime 沒變就沿用快取。"""
    try:
        st = path.stat()
    except OSError:
        return ""
    key = str(path)
    ent = cache.get(key)
    if ent and ent[0] == st.st_size and ent[1] == st.st_mtime_ns:
        return ent[2]

    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return ""
    digest = h.hexdigest()
    cache[key] = [st.st_size, st.st_mtime_ns, digest]
    return digest


# ===================== 輸出檔名 =====================
def assign_outputs(files: list[Path]) -> list[tuple[Path, Path]]:
    """m 內的檔案一律攤平輸出到 m_out，並避開同名衝突。"""
    used = set()
    pairs = []
    for p in files:
        name = f"{p.stem}.mp4"
        if name in used:
            name = f"{p.parent.name}_{p.stem}.mp4"
            n = 2
            while name in used:
                name = f"{p.parent.name}_{p.stem}_{n}.mp4"
                n += 1
        used.add(name)
        pairs.append((p, OUT_DIR / name))
    return pairs


# ===================== 去重分組 =====================
def build_groups(pairs: list[tuple[Path, Path]], cache: dict) -> list[list[tuple[Path, Path]]]:
    """先依大小分堆，只有同大小的檔案才算 MD5，大幅省下讀檔時間。"""
    by_size: dict[int, list[tuple[Path, Path]]] = {}
    for p, out in pairs:
        try:
            size = p.stat().st_size
        except OSError:
            continue
        by_size.setdefault(size, []).append((p, out))

    groups: list[list[tuple[Path, Path]]] = []
    candidates = [v for v in by_size.values() if len(v) > 1]

    if candidates:
        todo = sum(len(v) for v in candidates)
        with tqdm(total=todo, desc="比對內容", unit="檔", dynamic_ncols=True) as pbar:
            for bucket in candidates:
                by_hash: dict[str, list[tuple[Path, Path]]] = {}
                for p, out in bucket:
                    digest = md5_of(p, cache)
                    by_hash.setdefault(digest, []).append((p, out))
                    pbar.update(1)
                groups.extend(by_hash.values())
        save_cache(cache)

    for size, bucket in by_size.items():
        if len(bucket) == 1:
            groups.append(bucket)

    groups.sort(key=lambda g: g[0][1].name.lower())
    return groups


# ===================== 轉檔 =====================
def is_done(path: Path) -> bool:
    """檔案存在、非空，且（相容模式下）帶有新格式的版本標記。

    舊版腳本產出的 MPEG-1 mp4 沒有標記，重跑時會被視為未完成而自動重做。
    """
    try:
        if not path.exists() or path.stat().st_size <= 0:
            return False
        if not COMPAT_MODE:
            return True
        tag = OUTPUT_TAG.encode("ascii")
        size = path.stat().st_size
        with open(path, "rb") as f:
            if tag in f.read(1024 * 1024):
                return True
            # 保險：moov 若在檔尾（沒有 faststart）也要找得到
            if size > 1024 * 1024:
                f.seek(max(0, size - 1024 * 1024))
                if tag in f.read(1024 * 1024):
                    return True
        return False
    except OSError:
        return False


def cleanup_temp(src: Path):
    """刪掉 UsmToolkit 留下的中間檔（.m2v / .wav / .adx / .bin）。"""
    if not CLEAN_TEMP:
        return
    for ext in (".m2v", ".wav", ".adx", ".bin", ".hca"):
        p = src.with_suffix(ext)
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass


def find_hca(src: Path) -> Path | None:
    """找出 UsmToolkit 從 USM 抽出的加密 HCA 音訊（副檔名會是 .bin 或 .hca）。"""
    for ext in (".bin", ".hca"):
        p = src.with_suffix(ext)
        if p.is_file() and p.stat().st_size > 0:
            try:
                with open(p, "rb") as f:
                    magic = f.read(4)
            except OSError:
                continue
            # HCA 的 magic 是 "HCA\0"，加密版本會是 C8 C3 C1 00
            if magic in (b"HCA\x00", b"\xc8\xc3\xc1\x00"):
                return p
    return None


def decode_hca(hca: Path, out_wav: Path) -> tuple[bool, str]:
    """用 vgmstream 加指定金鑰解碼 HCA，避免解成雜訊。"""
    if not (FIX_HCA and VGMSTREAM.exists()):
        return False, "未啟用 HCA 修正"

    work = Path(tempfile.mkdtemp(prefix="hca_", dir=str(TMP_ROOT)))
    try:
        src = work / "audio.hca"
        shutil.copy2(hca, src)
        # 只有加密 HCA（magic C8 C3 C1 00）才需要指定金鑰
        with open(src, "rb") as f:
            encrypted = f.read(4) == b"\xc8\xc3\xc1\x00"
        if encrypted:
            # vgmstream 會讀取 <檔名>.hcakey
            (work / "audio.hca.hcakey").write_bytes(bytes.fromhex(HCA_KEY))
        r = subprocess.run(
            [str(VGMSTREAM), "-o", str(out_wav), str(src)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=TIMEOUT,
        )
        if r.returncode != 0 or not out_wav.exists() or out_wav.stat().st_size == 0:
            tail = r.stdout.decode("utf-8", "ignore").strip().splitlines()
            return False, tail[-1] if tail else "vgmstream 解碼失敗"
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"HCA 解碼失敗：{e}"
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return True, ""


def run_tool(src: Path, out_dir: Path) -> tuple[bool, str]:
    """呼叫 UsmToolkit 轉檔，成功時 out_dir/<stem>.mp4 會存在。

    不加 -c：要保留抽出的 HCA 音訊，之後才能用正確金鑰重解。
    """
    cmd = [str(USM_TOOL), "convert", "-o", str(out_dir), str(src)]
    try:
        r = subprocess.run(
            cmd,
            cwd=str(TOOL_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, "逾時"
    except OSError as e:
        return False, f"無法執行工具：{e}"

    if r.returncode != 0:
        tail = r.stdout.decode("utf-8", "ignore").strip().splitlines()
        return False, tail[-1] if tail else f"exit {r.returncode}"
    return True, ""


FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*fps")
MAXVOL_RE = re.compile(r"max_volume:\s*(-?(?:\d+(?:\.\d+)?|inf))\s*dB")


def audio_max_db(media: Path) -> float | None:
    """用 volumedetect 取得第一條音軌的最大音量（dBFS）。

    沒有音軌或讀不到時回傳 None。
    """
    try:
        r = subprocess.run(
            [str(FFMPEG), "-hide_banner", "-nostats", "-i", str(media),
             "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    m = MAXVOL_RE.search(r.stdout.decode("utf-8", "ignore"))
    if not m:
        return None
    val = m.group(1)
    return float("-inf") if "inf" in val else float(val)


def detect_fps(mp4: Path) -> str:
    """用 ffmpeg 讀容器資訊取得幀率，失敗就退回 24。"""
    try:
        r = subprocess.run(
            [str(FFMPEG), "-hide_banner", "-i", str(mp4)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
        text = r.stdout.decode("utf-8", "ignore")
        m = FPS_RE.search(text)
        if m:
            return m.group(1)
    except (subprocess.TimeoutExpired, OSError):
        pass
    return "24"


def color_args() -> list[str]:
    """明確指定色彩參數，避免播放器依解析度自行猜矩陣。"""
    return [
        "-color_range", COLOR_RANGE,
        "-colorspace", COLOR_MATRIX,
        "-color_primaries", COLOR_PRIMARIES,
        "-color_trc", COLOR_TRC,
    ]


def video_args() -> list[str]:
    """影片編碼參數：預設重編 H.264 並把 BT.601 寫進 VUI。"""
    if not REENCODE_VIDEO:
        return ["-c:v", "copy"]

    if "nvenc" in VIDEO_CODEC:
        # NVENC 沒有 crf，改用 VBR + CQ（-cq 數值意義接近 crf）
        args = [
            "-c:v", VIDEO_CODEC, "-preset", "p5", "-rc", "vbr",
            "-cq", str(VIDEO_CRF), "-b:v", "0", "-pix_fmt", "yuv420p",
        ]
    elif "qsv" in VIDEO_CODEC:
        args = ["-c:v", VIDEO_CODEC, "-global_quality", str(VIDEO_CRF), "-pix_fmt", "yuv420p"]
    elif "amf" in VIDEO_CODEC:
        args = [
            "-c:v", VIDEO_CODEC, "-rc", "cqp",
            "-qp_i", str(VIDEO_CRF), "-qp_p", str(VIDEO_CRF), "-pix_fmt", "yuv420p",
        ]
    else:
        args = ["-c:v", VIDEO_CODEC, "-preset", VIDEO_PRESET, "-crf", str(VIDEO_CRF), "-pix_fmt", "yuv420p"]

    if VIDEO_CODEC == "libx264":
        # x264 才會把色彩矩陣寫進 H.264 VUI，讓硬解也吃得到
        args += [
            "-x264-params",
            f"colorprim={COLOR_PRIMARIES}:transfer={COLOR_TRC}"
            f":colmatrix={COLOR_MATRIX}:range={COLOR_RANGE}",
            "-threads", str(encode_threads()),
        ]
    return args


def encode_threads() -> int:
    """每個 ffmpeg 可用的執行緒數，避免 WORKERS 個工作把 CPU 搶爆。"""
    return max(1, CPU_COUNT // max(1, WORKERS))


def remux_compat(src_mp4: Path, dst_mp4: Path, audio_wav: Path | None = None) -> tuple[bool, str]:
    """影片轉成 H.264（或直接 copy），音訊轉 AAC，並把 moov 移到檔案前面。

    UsmToolkit 產出的 mp4 時間戳是沿用 USM demux 的封包時間，常常不是嚴格 CFR
    （stts 會有上百段），PotPlayer 這類走 DXVA 的播放器會因此判定「不支援硬體解碼」。
    這裡順便用 -r 重寫成固定幀率。

    影片預設重編成 H.264 並寫入 BT.601 標記：電腦版 USM 是 MPEG-1，
    硬解時會被當成 BT.709 而整片偏色（iOS 版是 H.264 所以沒問題）。

    audio_wav 有值時改用這份（金鑰正確解出的）音訊，取代 UsmToolkit 的雜訊音軌。
    音軌最大音量低於 SILENT_DB 時視為無聲，整個音軌不輸出（避免沙沙聲）。
    """
    tmp = dst_mp4.with_name(dst_mp4.stem + ".compat.tmp.mp4")

    # 音軌幾乎無聲就整條不放，免得播放器把量化雜訊放大成沙沙聲
    keep_audio = True
    if DROP_SILENT_AUDIO:
        db = audio_max_db(audio_wav if audio_wav is not None else src_mp4)
        keep_audio = db is None or db >= SILENT_DB

    cmd = [
        str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src_mp4),
    ]
    if not keep_audio:
        cmd += ["-map", "0:v:0"]
    elif audio_wav is not None:
        cmd += ["-i", str(audio_wav), "-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
    cmd += video_args()
    cmd += ["-c:a", "aac", "-b:a", AUDIO_BITRATE] if keep_audio else ["-an"]
    cmd += color_args()
    # 把版本標記寫進 udta，重跑時靠這個判斷成品已是新格式（不必再解析編碼器）
    cmd += ["-metadata", f"comment={OUTPUT_TAG}"]
    if FORCE_CFR:
        cmd += ["-r", detect_fps(src_mp4)]
    cmd += ["-movflags", "+faststart", "-f", "mp4", str(tmp)]
    try:
        r = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=TIMEOUT
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)

    if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        try:
            tmp.unlink()
        except OSError:
            pass
        tail = r.stdout.decode("utf-8", "ignore").strip().splitlines()
        return False, tail[-1] if tail else "ffmpeg 失敗"

    tmp.replace(dst_mp4)
    return True, ""


def convert_group(group: list[tuple[Path, Path]]) -> tuple[int, str]:
    """group[0] 是代表檔，其餘直接複製它的輸出。

    回傳 (成功轉出的檔數, 錯誤訊息)。
    """
    src, primary = group[0]
    extras = [out for _, out in group[1:]]

    if stop_event.is_set():
        return 0, "已停止"

    for attempt in range(1, RETRY + 1):
        if stop_event.is_set():
            return 0, "已停止"

        primary.parent.mkdir(parents=True, exist_ok=True)
        try:
            if primary.exists():
                primary.unlink()
        except OSError:
            pass

        work = Path(tempfile.mkdtemp(prefix="usm_", dir=str(TMP_ROOT)))
        try:
            ok, err = run_tool(src, work)
            produced = work / f"{src.stem}.mp4"
            if not ok or not produced.exists() or produced.stat().st_size == 0:
                last_err = err or "沒有產出 mp4"
            elif COMPAT_MODE:
                # 加密 HCA 用 UsmToolkit 會被解成雜訊，改用正確金鑰重解
                wav = None
                hca = find_hca(src)
                if hca is not None:
                    wav = work / "hca_audio.wav"
                    ok_hca, _ = decode_hca(hca, wav)
                    if not ok_hca:
                        wav = None
                ok2, err2 = remux_compat(produced, primary, wav)
                last_err = "" if ok2 else err2
                if not ok2:
                    pass
                else:
                    break
            else:
                try:
                    produced.replace(primary)
                except OSError:
                    shutil.move(str(produced), str(primary))
                break
        finally:
            shutil.rmtree(work, ignore_errors=True)

        if attempt == RETRY:
            return 0, last_err
        time.sleep(0.5)

    cleanup_temp(src)

    for extra in extras:
        try:
            extra.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(primary, extra)
        except OSError as e:
            return 1, f"複製 {extra.name} 失敗：{e}"

    return len(group), ""


# ===================== main =====================
def main():
    global COMPAT_MODE, WORKERS, SRC_DIR, OUT_DIR
    global REENCODE_VIDEO, VIDEO_CODEC, VIDEO_PRESET, VIDEO_CRF

    ap = argparse.ArgumentParser(description="USM → MP4（同內容去重）")
    ap.add_argument("input", nargs="?", help="只轉換指定的 .usm 檔（省略則轉整個來源資料夾）")
    ap.add_argument("--src", metavar="DIR", help=f"來源資料夾（預設 {SRC_DIR.name}）")
    ap.add_argument("--out", metavar="DIR", help=f"輸出資料夾（預設 {OUT_DIR.name}）")
    ap.add_argument("--dry-run", action="store_true", help="只統計，不轉檔")
    ap.add_argument("--workers", type=int, default=WORKERS, help=f"同時轉檔數（預設 {WORKERS}）")
    ap.add_argument("--no-compat", action="store_true", help="保留 AC-3 音軌與 MPEG-1 影片，不做相容處理")
    ap.add_argument("--copy-video", action="store_true",
                    help="影片不重編（較快，但 MPEG-1 開硬解仍會偏色）")
    ap.add_argument("--encoder", default=VIDEO_CODEC,
                    help=f"影片編碼器（預設 {VIDEO_CODEC}，可改用 h264_nvenc / h264_qsv / h264_amf）")
    ap.add_argument("--preset", default=VIDEO_PRESET, help=f"編碼速度預設（預設 {VIDEO_PRESET}）")
    ap.add_argument("--crf", type=int, default=VIDEO_CRF, help=f"畫質參數，越小越好（預設 {VIDEO_CRF}）")
    ap.add_argument("--force", action="store_true", help="忽略既有的 mp4，全部重新轉檔")
    args = ap.parse_args()

    if args.src:
        SRC_DIR = Path(args.src)
        if not SRC_DIR.is_absolute():
            SRC_DIR = (BASE / SRC_DIR).resolve()
    if args.out:
        OUT_DIR = Path(args.out)
        if not OUT_DIR.is_absolute():
            OUT_DIR = (BASE / OUT_DIR).resolve()

    COMPAT_MODE = not args.no_compat
    WORKERS = max(1, args.workers)
    REENCODE_VIDEO = COMPAT_MODE and not args.copy_video
    VIDEO_CODEC = args.encoder
    VIDEO_PRESET = args.preset
    VIDEO_CRF = args.crf

    if not SRC_DIR.is_dir():
        print(f"[錯誤] 找不到來源資料夾：{SRC_DIR}")
        return 1
    if not USM_TOOL.exists():
        print(f"[錯誤] 找不到 UsmToolkit：{USM_TOOL}")
        return 1
    if COMPAT_MODE and not FFMPEG.exists():
        print(f"[錯誤] 找不到 ffmpeg：{FFMPEG}")
        return 1

    if COMPAT_MODE:
        if REENCODE_VIDEO:
            print(f"影片編碼：{VIDEO_CODEC}（preset={VIDEO_PRESET}, crf={VIDEO_CRF}）"
                  f" 色彩：{COLOR_MATRIX} / {COLOR_PRIMARIES} / {COLOR_TRC} / {COLOR_RANGE}")
        else:
            print("[提示] --copy-video：影片不重編，MPEG-1 開硬解仍可能偏色")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    TMP_ROOT.mkdir(parents=True, exist_ok=True)

    # 清掉上次中斷留下的半成品
    for p in OUT_DIR.glob("*.compat.tmp.mp4"):
        try:
            p.unlink()
        except OSError:
            pass

    if args.input:
        src = Path(args.input)
        if not src.is_absolute():
            src = (BASE / src).resolve()
        if not src.is_file():
            print(f"[錯誤] 找不到檔案：{src}")
            return 1
        if src.suffix.lower() != ".usm":
            print(f"[提示] {src.name} 不是 .usm，仍嘗試轉換")
        files = [src]
        try:
            shown = src.relative_to(BASE)
        except ValueError:
            shown = src
        print(f"[單檔模式] {shown}")
    else:
        files = sorted(SRC_DIR.rglob("*.usm"))
        if not files:
            print(f"[錯誤] {SRC_DIR} 內沒有 .usm")
            return 1

        others = [
            p for p in SRC_DIR.rglob("*") if p.is_file() and p.suffix.lower() != ".usm"
        ]
        if others:
            print(
                f"[提示] m 內另有 {len(others)} 個非 .usm 檔"
                "（多為上次中斷留下的 .m2v/.wav/.adx）"
            )

    pairs = assign_outputs(files)
    if not args.input:
        print(f"來源 {len(pairs)} 個 .usm，開始比對內容…")
    groups = build_groups(pairs, load_cache())
    if not args.input:
        print(f"實際不同內容 {len(groups)} 組（同內容省下 {len(pairs) - len(groups)} 次轉檔）")

    jobs = []
    skipped = 0
    copied = 0
    for group in groups:
        outs = [out for _, out in group]
        done = [] if args.force else [o for o in outs if is_done(o)]
        if len(done) == len(outs):
            skipped += 1
            continue
        if done:
            for out in outs:
                if is_done(out):
                    continue
                if args.dry_run:
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(done[0], out)
                    copied += 1
                except OSError as e:
                    print(f"[WARN] 複製 {out.name} 失敗：{e}")
            skipped += 1
            continue
        jobs.append(group)

    print(f"已完成 {skipped} 組，還有 {len(jobs)} 組要轉檔。")
    if args.dry_run:
        print("[dry-run] 不執行轉檔。")
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        return 0

    if not jobs:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        print("[ALL DONE] 沒有需要轉檔的內容。")
        return 0

    failed = []
    converted = 0
    start = time.time()

    with tqdm(total=len(jobs), desc="轉檔", unit="組", dynamic_ncols=True) as pbar:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(convert_group, g): g for g in jobs}
            for fut in as_completed(futures):
                group = futures[fut]
                try:
                    n, err = fut.result()
                except Exception as e:  # 保險：單一檔案出錯不影響其他
                    n, err = 0, f"{type(e).__name__}: {e}"
                if n:
                    converted += 1
                    copied += n - 1
                elif err and err != "已停止":
                    failed.append(group[0][0].name)
                    log(f"[失敗] {group[0][0].name}：{err}")
                pbar.update(1)
                pbar.set_postfix_str(group[0][1].name[:40])

    shutil.rmtree(TMP_ROOT, ignore_errors=True)

    if failed:
        (OUT_DIR / "_failed.txt").write_text("\n".join(sorted(failed)), encoding="utf-8")
        print(f"[完成] 失敗 {len(failed)} 個，清單：{OUT_DIR / '_failed.txt'}")
    else:
        print("[完成] 全部成功。")

    print(
        f"[統計] 實際轉檔 {converted} 個內容、複製 {copied} 個檔名"
        f"、略過 {skipped} 組，耗時 {time.time() - start:.1f} 秒"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
