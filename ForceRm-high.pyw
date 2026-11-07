# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""
ForceRm 清理工具 (Rubbish Cleaner)
===================================
基于 Python + CustomTkinter 的 Windows 垃圾文件清理工具。

原生引擎 LibRC.dll（C/C++）：
  - 负责底层 Windows 操作：回收站删除、永久删除、管理员提权强制删除、写 WinERR.log。
  - LibRm/LibRm.json 是合并后的精确规则库（单文件加载）；其余软件缓存由智能扫描识别。
  - 若 LibRC.dll 缺失，自动回退到纯 Python 实现（功能一致，仅强制删除能力受限）。

设计原则（安全第一）：
  1. 只清理「白名单」内的临时 / 缓存文件，绝不触碰系统目录。
  2. 清理前必须先「扫描」并手动确认，不会静默删除。
  3. 直接删除（不可恢复），清理前需先扫描并确认。
  4. 自动跳过被占用（正在使用）的文件，不中断整体流程。
  5. 不跟随符号链接 / 目录 junctions，避免误删其它盘的数据。
  6. 无法删除的文件：先尝试管理员提权强制删除，仍失败则跳过并在同级目录写 WinERR.log。

运行依赖：Python 3.10+ 与 customtkinter
    pip install customtkinter
双击 .pyw 即可运行（无控制台窗口）。
"""

import os
import sys
import glob
import json
import shutil
import ctypes
import threading
import subprocess
import datetime
import time
import tkinter.messagebox as mb
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import customtkinter as ctk
except ImportError:
    mb.showerror(
        "缺少依赖",
        "未能导入 customtkinter。\n\n请先安装：\n    pip install customtkinter\n\n"
        "安装后重新双击本程序即可。",
    )
    sys.exit(1)

if os.name != "nt":
    mb.showerror("系统不支持", "ForceRm 清理工具仅支持 Windows 系统。")
    sys.exit(1)

# 火绒风格：固定浅色主题
ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

# 品牌色（取自 logo：深青 / 亮蓝 / 柠檬绿）
C_TEAL = "#146B7E"
C_TEAL_D = "#0F5566"
C_BLUE = "#1E9BD7"
C_BLUE_D = "#1789C0"
C_GREEN = "#9BCB3C"
C_BG = "#F4F6F8"
C_CARD = "#FFFFFF"
C_TXT = "#1F2D3D"
C_SUB = "#8A97A6"
C_BORDER = "#E6EAEF"

# 可选依赖：Pillow（用于显示 logo 与设置窗口图标）
try:
    from PIL import Image
    PIL_OK = True
except ImportError:
    PIL_OK = False

# 资源目录：源码运行时是脚本目录，冻结后是 exe 所在目录
def _resource_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        return os.getcwd()


APP_DIR = _resource_dir()
LOGO_PATH = os.path.join(APP_DIR, "logo.png")

# 加载动画帧（旋转的半圆，Segoe UI 等常见字体均支持）
SPIN_FRAMES = "◐◓◑◒"
SPIN_INTERVAL_MS = 120

# 并发设置：扫描与清理都是 I/O 密集型，用线程池并行加速
CPU_COUNT = os.cpu_count() or 4
MAX_WORKERS = min(16, max(4, CPU_COUNT * 2))
DELETE_CHUNK = 100      # 每个删除任务的批大小（控制任务数量，避免内存膨胀）
LOG_LIMIT = 200         # 单次清理最多输出的跳过多 / 强制删除日志条数

# 保护共享资源：LibRC.dll 内部用静态变量保存错误码，且 WinERR.log 可能被并发追加
_DLL_LOCK = threading.Lock()
_WINERR_LOCK = threading.Lock()


def worker_count() -> int:
    """并发线程数：config.json 的 threads 优先（0 或缺失则自动）。"""
    auto = min(16, max(4, CPU_COUNT * 2))
    try:
        n = int(load_settings().get("threads", 0) or 0)
    except Exception:
        return auto
    return max(1, min(64, n)) if n > 0 else auto


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def human_size(num: int) -> str:
    """把字节数转换为易读字符串。"""
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(num)
    for u in units:
        if f < 1024 or u == "TB":
            if u == "B":
                return f"{int(num)} B"
            return f"{f:.1f} {u}"
        f /= 1024
    return f"{num} B"


def is_link(path: str) -> bool:
    """判断是否为符号链接或目录 junctions（Windows）。"""
    try:
        if os.path.islink(path):
            return True
        if hasattr(os.path, "isjunction"):
            if os.path.isjunction(path):
                return True
    except OSError:
        pass
    return False


# --------------------------------------------------------------------------
# 安全清理目标（白名单 · 系统 / 浏览器 / 通用）
# --------------------------------------------------------------------------
LOCAL = os.environ.get("LOCALAPPDATA", "")
ROAMING = os.environ.get("APPDATA", "")
PROGRAMDATA = os.environ.get("ProgramData", r"C:\ProgramData")
TEMP = os.environ.get("TEMP", "") or os.environ.get("TMP", "")
USERPROFILE = os.environ.get("USERPROFILE", os.path.expanduser("~"))

# --------------------------------------------------------------------------
# 智能软件缓存识别：目录关键字 + 一次遍历，不依赖静态软件名库
#
# 新增软件无需往 LibRm 里加 json：只要它把缓存放在 AppData 下，
# 用 cache / crashpad / temp / logs 这类通用目录名就能被自动识别。
# 关键字集合是代码里的固定常量，规则文件不会随软件数量增长。
# --------------------------------------------------------------------------
def _norm_name(s: str) -> str:
    """目录名归一化：小写、去掉空格/下划线/连字符/点，兼容各种写法。"""
    return "".join(c for c in s.lower() if c.isalnum())


# 命中即判定为缓存目录（归一化后精确匹配）
SMART_DIR_KEYS = {
    "cache", "cache2", "cache3", "cachestorage", "cachedata", "cachedimages",
    "cachedownloads", "cacheindex", "code cache", "gpu cache",
    "shader cache", "dawn cache", "grshader cache", "blob storage",
    "service worker", "webcache", "httpcache", "media cache",
    "media cache files", "manifestcache", "assetcache", "texturecache",
    "fontcache", "imagecache", "networkcache", "diskcache", "downloadcache",
    "installercache", "packagecache", "updatecache", "inetcache",
    "crashpad", "crashdumps", "crashreports", "crashes", "dumps", "minidumps",
    "thumbnails", "thumbnailcache", "iconcache", "previews",
    "temp", "tmp", "tempstate", "tempfiles", "tmpfiles", "temporary",
    "cachetmp", "logs", "log", "logfiles", "diagnostics", "webview2 cache",
}
SMART_DIR_KEYS = {_norm_name(x) for x in SMART_DIR_KEYS}

# 带这些词的目录即使名字像缓存也不碰：
# 前半是配置/账号/凭据，后半是已解压的 SDK / 工具链（删了要重新联网下载）
SMART_BLOCK_WORDS = (
    "config", "settings", "preference", "profile", "account", "credential",
    "password", "wallet", "keys", "certificate", "token", "localstate",
    "bookmark", "favorite", "backup", "restore", "synced",
    "sdk", "runtime", "toolchain", "compiler", "nuget", "gradle",
    "maven", "wheels", "venv", "conda",
)

# 不递归遍历的目录（非缓存或用户数据）
SMART_SKIP_DIRS = {_norm_name(x) for x in (
    "node_modules", ".git", ".svn", "__pycache__", "system32",
    "windows", "$recycle.bin", "winsxs", "servicing", "windowsapps",
    "saves", "savegames", "save games",
    "documents", "projects", "workspace", "vault", "wallet",
    "keystore", "credentials", "identity", "accounts",
)}

# 明确的临时残留后缀；只在名字带「临时」语义的目录里收集
SMART_FILE_EXTS = (
    ".tmp", ".temp", ".old", ".bak", ".dmp", ".chk", ".gid",
    ".crdownload", ".part", ".partial", ".cache",
)
SMART_TRANSIENT_WORDS = (
    "temp", "tmp", "cache", "log", "crash", "dump", "download",
    "update", "install", "junk", "trash",
)

# 无论在哪都不删的文件名（前缀匹配，覆盖 Cookies-journal 等）
SMART_KEEP_FILES = {_norm_name(x) for x in (
    "local state", "login data", "cookies", "history", "bookmarks",
    "preferences", "secure preferences", "web data", "favicons",
    "current session", "last session", "trusted vault",
)}

SMART_MAX_DEPTH = 7            # 单个软件目录最多向下 7 层，避免病态深树
SMART_ROOTS = [LOCAL, ROAMING]  # Programs / Packages 已在 LOCAL 下，不重复扫描

SMART_TARGET = {
    "key": "smart_software_cache",
    "type": "smart",
    "name": "智能软件缓存",
    "desc": "按缓存目录关键字与临时文件后缀自动识别，未知软件同样覆盖",
    "paths": [],
    "patterns": [],
    "risk": "低",
    "default": False,
}


def _is_cache_dir(name: str) -> bool:
    k = _norm_name(name)
    if not k or k in SMART_SKIP_DIRS:
        return False
    if any(w in k for w in SMART_BLOCK_WORDS):
        return False
    if k in SMART_DIR_KEYS:
        return True
    # 兼容软件自带前缀，如 FooCache / FooCrashDumps / INetCache
    if len(k) <= 28 and (k.endswith("cache") or k.endswith("crashdumps")):
        return True
    return False


def _keep_file(path: str) -> bool:
    n = _norm_name(os.path.basename(path))
    return any(n.startswith(k) for k in SMART_KEEP_FILES)


def _precise_roots() -> set:
    """精确规则覆盖的目录：智能扫描跳过，避免同一批文件被重复统计。"""
    out = set()
    for t in JUNK_TARGETS:
        if t.get("type") in ("action", "smart"):
            continue
        for p in t.get("paths", []):
            if p:
                out.add(os.path.normcase(os.path.normpath(os.path.expandvars(p))))
    return out


def _scan_branch(base: str, skip: set) -> set:
    """遍历一个一级软件目录；命中缓存目录后收集子树并剪枝，不重复遍历。"""
    out = set()
    for cur, dirs, fnames in os.walk(base, topdown=True):
        if cur[len(base):].count(os.sep) >= SMART_MAX_DEPTH:
            dirs[:] = []
        kept = []
        for d in dirs:
            p = os.path.join(cur, d)
            n = _norm_name(d)
            if is_link(p) or n in SMART_SKIP_DIRS:
                continue
            if os.path.normcase(os.path.normpath(p)) in skip:
                continue
            kept.append(d)
        dirs[:] = kept

        if _is_cache_dir(os.path.basename(cur)):
            for r2, d2, f2 in os.walk(cur):
                d2[:] = [x for x in d2 if not is_link(os.path.join(r2, x))]
                for fn in f2:
                    fp = os.path.join(r2, fn)
                    if not is_link(fp) and not _keep_file(fp):
                        out.add(fp)
            dirs[:] = []
            continue

        if any(w in _norm_name(os.path.basename(cur)) for w in SMART_TRANSIENT_WORDS):
            for fn in fnames:
                fp = os.path.join(cur, fn)
                if is_link(fp) or _keep_file(fp):
                    continue
                if fn.lower().endswith(SMART_FILE_EXTS):
                    out.add(fp)
    return out


def smart_collect() -> list:
    """扫描本机 AppData，按关键字智能识别所有软件的缓存文件。"""
    skip = _precise_roots()
    branches = []
    for root in SMART_ROOTS:
        if not root or not os.path.isdir(root) or is_link(root):
            continue
        try:
            subs = [
                os.path.join(root, d) for d in os.listdir(root)
                if os.path.isdir(os.path.join(root, d))
                and not is_link(os.path.join(root, d))
                and os.path.normcase(os.path.normpath(os.path.join(root, d))) not in skip
            ]
        except OSError:
            subs = []
        branches.extend(subs or [root])

    files = set()
    n = worker_count()
    with ThreadPoolExecutor(max_workers=n) as ex:
        for fu in as_completed([ex.submit(_scan_branch, b, skip) for b in branches]):
            try:
                files.update(fu.result())
            except Exception:
                pass
    return list(files)


def prune_smart_dirs(files) -> int:
    """智能清理后，删除本次扫描命中的、已清空的缓存目录（不动其它目录）。"""
    roots = set()
    for f in files:
        d = os.path.dirname(f)
        while d:
            if _is_cache_dir(os.path.basename(d)):
                roots.add(d)
                break
            p = os.path.dirname(d)
            if p == d:
                break
            d = p
    for r in roots:
        try:
            remove_empty_dirs(r)
        except Exception:
            pass
    return len(roots)


def load_software_targets(lib_dir: str) -> list:
    """加载规则库：优先一次读合并后的 LibRm.json，缺失或损坏时回退到旧散文件。"""
    targets = []
    if not os.path.isdir(lib_dir):
        return targets

    packed = os.path.join(lib_dir, "LibRm.json")
    if os.path.isfile(packed):
        try:
            with open(packed, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
            items = doc.get("targets") if isinstance(doc, dict) else doc
            if isinstance(items, list):
                for data in items:
                    if isinstance(data, dict) and "key" in data:
                        data["paths"] = [os.path.expandvars(p) for p in data.get("paths", [])]
                        targets.append(data)
                return targets
        except Exception:
            targets = []

    # 兼容旧版：顶层散文件（跳过聚合规则与合并文件本身）
    for fp in sorted(glob.glob(os.path.join(lib_dir, "*.json"))):
        name = os.path.basename(fp)
        if name.startswith("frm_lib_") or name == "LibRm.json":
            continue
        try:
            with open(fp, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict) or "key" not in data:
                continue
            data["paths"] = [os.path.expandvars(p) for p in data.get("paths", [])]
            targets.append(data)
        except Exception:
            continue
    return targets


JUNK_TARGETS = [
    {
        "key": "sys_temp",
        "name": "系统 / 用户临时文件",
        "desc": "TEMP 与 Windows\\Temp 下的临时文件",
        "paths": [TEMP, r"C:\Windows\Temp"],
        "patterns": ["*"],
        "risk": "低",
        "default": True,
    },
    {
        "key": "windows_update",
        "name": "Windows 更新缓存",
        "desc": "SoftwareDistribution\\Download 中的更新安装包",
        "paths": [r"C:\Windows\SoftwareDistribution\Download"],
        "patterns": ["*"],
        "risk": "低",
        "default": True,
    },
    {
        "key": "prefetch",
        "name": "预读取缓存 (Prefetch)",
        "desc": "加速启动用的预读文件，可安全清理",
        "paths": [r"C:\Windows\Prefetch"],
        "patterns": ["*"],
        "risk": "低",
        "default": False,
    },
    {
        "key": "thumbcache",
        "name": "缩略图缓存",
        "desc": "Explorer 缩略图数据库 (*.db)",
        "paths": [os.path.join(LOCAL, "Microsoft", "Windows", "Explorer")],
        "patterns": ["thumbcache_*.db", "iconcache_*.db"],
        "risk": "低",
        "default": True,
    },
    {
        "key": "chrome_cache",
        "name": "Chrome 浏览器缓存",
        "desc": "Google Chrome 的缓存与代码缓存",
        "paths": [
            os.path.join(LOCAL, "Google", "Chrome", "User Data", "Default", "Cache"),
            os.path.join(LOCAL, "Google", "Chrome", "User Data", "Default", "Cache2"),
            os.path.join(LOCAL, "Google", "Chrome", "User Data", "Default", "Code Cache"),
        ],
        "patterns": ["*"],
        "risk": "低",
        "category": "软件垃圾",
        "default": True,
    },
    {
        "key": "edge_cache",
        "name": "Edge 浏览器缓存",
        "desc": "Microsoft Edge 的缓存与代码缓存",
        "paths": [
            os.path.join(LOCAL, "Microsoft", "Edge", "User Data", "Default", "Cache"),
            os.path.join(LOCAL, "Microsoft", "Edge", "User Data", "Default", "Cache2"),
            os.path.join(LOCAL, "Microsoft", "Edge", "User Data", "Default", "Code Cache"),
        ],
        "patterns": ["*"],
        "risk": "低",
        "category": "软件垃圾",
        "default": True,
    },
    {
        "key": "firefox_cache",
        "name": "Firefox 浏览器缓存",
        "desc": "Mozilla Firefox 的缓存目录",
        "paths": [os.path.join(ROAMING, "Mozilla", "Firefox", "Profiles")],
        "patterns": ["cache2", "thumbnails", "cache"],
        "risk": "低",
        "category": "软件垃圾",
        "default": True,
    },
    {
        "key": "crash_dumps",
        "name": "崩溃转储 / 错误报告",
        "desc": "CrashDumps 与 WER 错误报告数据",
        "paths": [
            os.path.join(LOCAL, "CrashDumps"),
            os.path.join(PROGRAMDATA, "Microsoft", "Windows", "WER"),
        ],
        "patterns": ["*"],
        "risk": "低",
        "default": False,
    },
    {
        "key": "pip_cache",
        "name": "pip 缓存",
        "desc": "Python pip 的下载缓存",
        "paths": [os.path.join(LOCAL, "pip", "cache")],
        "patterns": ["*"],
        "risk": "低",
        "category": "软件垃圾",
        "default": False,
    },
    {
        "key": "log_files",
        "name": "临时日志文件 (*.log)",
        "desc": "临时目录下的 .log 日志",
        "paths": [TEMP],
        "patterns": ["*.log"],
        "risk": "低",
        "default": False,
    },
]

# 从 LibRC 目录加载各类软件的独立 .json 定义
LIBRM_DIR = os.path.join(APP_DIR, "LibRm")
if not os.path.isdir(LIBRM_DIR):
    LIBRM_DIR = os.path.join(APP_DIR, "LibRC")
LIBRC_DIR = LIBRM_DIR
JUNK_TARGETS += load_software_targets(LIBRM_DIR)

# --------------------------------------------------------------------------
# 深度清理：系统工具动作（非文件扫描，直接调用系统能力）
# --------------------------------------------------------------------------
DEEP_ACTIONS = [
    {
        "key": "act_empty_recycle", "type": "action", "action": "empty_recycle",
        "name": "清空回收站", "desc": "清空所有磁盘的回收站（不可恢复）",
        "risk": "中", "default": False, "deep": True,
    },
    {
        "key": "act_dism_cleanup", "type": "action", "action": "dism_cleanup",
        "name": "组件存储清理 (WinSxS)",
        "desc": "DISM StartComponentCleanup，清理旧组件版本（耗时较长）",
        "risk": "中", "default": False, "deep": True,
    },
    {
        "key": "act_flush_dns", "type": "action", "action": "flush_dns",
        "name": "刷新 DNS 缓存", "desc": "ipconfig /flushdns",
        "risk": "低", "default": False, "deep": True,
    },
]

JUNK_TARGETS += DEEP_ACTIONS
JUNK_TARGETS.append(SMART_TARGET)

# 深度清理项排到列表末尾（sort 稳定，保持相对顺序）
JUNK_TARGETS.sort(key=lambda t: 1 if (t.get("deep") or t.get("type") == "action") else 0)


# --------------------------------------------------------------------------
# 文件收集（扫描）
# --------------------------------------------------------------------------
def collect_target(target: dict) -> list:
    """返回该清理目标下所有待删文件的绝对路径集合（已排除链接）。"""
    files: set = set()
    for base in target["paths"]:
        if not base or not os.path.isdir(base) or is_link(base):
            continue
        for pat in target.get("patterns", ["*"]):
            if pat == "*":
                # 整棵目录树遍历，跳过符号链接 / junctions
                for root, dirs, fnames in os.walk(base):
                    dirs[:] = [d for d in dirs if not is_link(os.path.join(root, d))]
                    for fn in fnames:
                        fp = os.path.join(root, fn)
                        if not is_link(fp):
                            files.add(fp)
            elif "*" in pat:
                # 文件名通配：递归查找 base 下任意层级的匹配文件
                for match in glob.glob(
                    os.path.join(base, "**", pat), recursive=True
                ):
                    if os.path.isfile(match) and not is_link(match):
                        files.add(match)
            else:
                # 无通配：视为目录名，递归查找 base 下所有同名目录并遍历其中文件
                for root, dirs, fnames in os.walk(base):
                    dirs[:] = [d for d in dirs if not is_link(os.path.join(root, d))]
                    if os.path.basename(root) == pat:
                        for r2, d2, f2 in os.walk(root):
                            d2[:] = [x for x in d2 if not is_link(os.path.join(r2, x))]
                            for fn in f2:
                                fp = os.path.join(r2, fn)
                                if not is_link(fp):
                                    files.add(fp)
    return list(files)


# --------------------------------------------------------------------------
# 纯 Python 回退实现（LibRC.dll 缺失时使用）
# --------------------------------------------------------------------------
class SHFILEOPSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", ctypes.c_bool),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


FO_DELETE = 3
FOF_ALLOWUNDO = 0x40
FOF_NOCONFIRMATION = 0x10
FOF_NOERRORUI = 0x400
FOF_SILENT = 0x4
FOF_FLAGS = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT


def _sh_delete(paths: list) -> int:
    """一次 SHFileOperation 删除多个文件（pFrom 为 \\0 分隔、\\0\\0 结尾的路径串）。"""
    op = SHFILEOPSTRUCT()
    op.wFunc = FO_DELETE
    op.pFrom = "\0".join(paths) + "\0\0"
    op.fFlags = FOF_FLAGS
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))


def move_to_recycle_bin(path: str) -> bool:
    """通过 SHFileOperation 将单个文件移入回收站。成功返回 True（当前未调用）。"""
    try:
        return _sh_delete([path]) == 0
    except Exception:
        return False


# 单次 SHFileOperation 的路径串长度上限，超出则分批提交
SH_BATCH_CHARS = 12000


def bulk_recycle_bin(paths: list) -> list:
    """批量移入回收站：多个文件合并为一次 Shell 调用，返回仍未删除的路径。

    逐个调用 SHFileOperation 时每个文件都要走一遍 Shell 命名空间，开销极大
    （实测约 1 秒/个），合并调用后可成数量级提速。
    """
    if not paths:
        return []
    batch = []
    total = 0
    try:
        for p in paths:
            if total + len(p) + 1 > SH_BATCH_CHARS and batch:
                _sh_delete(batch)
                batch = []
                total = 0
            batch.append(p)
            total += len(p) + 1
        if batch:
            _sh_delete(batch)
    except Exception:
        return list(paths)
    return [p for p in paths if os.path.exists(p)]


def permanent_delete(path: str) -> None:
    """永久删除单个文件（绕过回收站）。"""
    if os.path.isdir(path) and not is_link(path):
        shutil.rmtree(path, ignore_errors=False)
    elif os.path.isfile(path):
        os.remove(path)


def _enable_privileges() -> bool:
    """在进程令牌中启用 SeTakeOwnershipPrivilege / SeRestorePrivilege（需以管理员运行）。"""
    try:
        adv = ctypes.windll.advapi32
        k32 = ctypes.windll.kernel32
        h_token = ctypes.c_void_p()
        k32.GetCurrentProcess.argtypes = ()
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        adv.OpenProcessToken.argtypes = (
            ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)
        )
        adv.OpenProcessToken.restype = ctypes.c_int
        if not adv.OpenProcessToken(k32.GetCurrentProcess(), 0x20 | 0x8, ctypes.byref(h_token)):
            return False

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", ctypes.c_int32), ("HighPart", ctypes.c_int32)]

        class LUID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Luid", LUID), ("Attributes", ctypes.c_uint32)]

        class TOKEN_PRIVILEGES(ctypes.Structure):
            _fields_ = [("PrivilegeCount", ctypes.c_uint32),
                        ("Privileges", LUID_AND_ATTRIBUTES * 1)]

        adv.LookupPrivilegeValueW.argtypes = (
            ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.POINTER(LUID)
        )
        adv.LookupPrivilegeValueW.restype = ctypes.c_int
        adv.AdjustTokenPrivileges.argtypes = (
            ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(TOKEN_PRIVILEGES),
            ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
        )
        adv.AdjustTokenPrivileges.restype = ctypes.c_int

        for name in ("SeTakeOwnershipPrivilege", "SeRestorePrivilege"):
            luid = LUID()
            if adv.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
                tp = TOKEN_PRIVILEGES()
                tp.PrivilegeCount = 1
                tp.Privileges[0].Luid = luid
                tp.Privileges[0].Attributes = 0x2  # SE_PRIVILEGE_ENABLED
                adv.AdjustTokenPrivileges(h_token, False, ctypes.byref(tp), 0, None, None)
        k32.CloseHandle(h_token)
        return True
    except Exception:
        return False


def force_delete(path: str) -> bool:
    """提权强制删除：启用特权 -> 取得所有权 -> 放开 DACL -> 删除。成功返回 True。"""
    try:
        adv = ctypes.windll.advapi32
        _enable_privileges()
        # 取得所有权：将 owner 设为 Administrators 组（S-1-5-32-544）
        sid = ctypes.c_void_p()
        adv.ConvertStringSidToSidW.argtypes = (
            ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)
        )
        adv.ConvertStringSidToSidW.restype = ctypes.c_int
        if not adv.ConvertStringSidToSidW("S-1-5-32-544", ctypes.byref(sid)):
            return False
        adv.SetNamedSecurityInfoW.argtypes = (
            ctypes.c_wchar_p, ctypes.c_int, ctypes.c_int,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        )
        adv.SetNamedSecurityInfoW.restype = ctypes.c_uint
        # OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION = 0x1 | 0x4
        # pDacl=None 表示 NULL DACL（所有人完全控制）；文件随即被删除，暴露窗口极小
        adv.SetNamedSecurityInfoW(path, 1, 0x1 | 0x4, sid, None, None, None)
        if os.path.isdir(path) and not is_link(path):
            shutil.rmtree(path, ignore_errors=False)
        elif os.path.isfile(path):
            os.remove(path)
        return not os.path.exists(path)
    except Exception:
        return False


def write_winerr(file_path: str, error_msg: str) -> None:
    """在 file_path 的同级目录追加 WinERR.log；若无法写入则回退到程序所在目录。"""
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] 删除失败: {file_path} | 错误: {error_msg}\n"
    parent = os.path.dirname(os.path.abspath(file_path))
    with _WINERR_LOCK:
        try:
            with open(os.path.join(parent, "WinERR.log"), "a", encoding="utf-8") as fh:
                fh.write(line)
            return
        except Exception:
            pass
        try:
            here = APP_DIR
            with open(os.path.join(here, "WinERR.log"), "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception:
            pass


# --------------------------------------------------------------------------
# 文件 / 目录占用解锁（基于 Windows Restart Manager，纯 ctypes 调用）
# --------------------------------------------------------------------------
kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi

CCH_RM_SESSION_KEY = 81
CCH_RM_MAX_APP_NAME = 255
CCH_RM_MAX_SVC_NAME = 63
RM_APP_TYPE = {
    0: "RmUnknownApp", 1: "RmMainWindow", 2: "RmOtherWindow",
    3: "RmService", 4: "RmExplorer", 5: "RmConsole", 6: "RmCritical",
}
ERROR_MORE_DATA = 234
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
MOVEFILE_DELAY_UNTIL_REBOOT = 0x4


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32),
                ("dwHighDateTime", ctypes.c_uint32)]


class _RM_UNIQUE_PROCESS(ctypes.Structure):
    _fields_ = [("dwProcessId", ctypes.c_uint32),
                ("ProcessStartTime", _FILETIME)]


class _RM_PROCESS_INFO(ctypes.Structure):
    _fields_ = [
        ("Process", _RM_UNIQUE_PROCESS),
        ("strAppName", ctypes.c_wchar * (CCH_RM_MAX_APP_NAME + 1)),
        ("strServiceShortName", ctypes.c_wchar * (CCH_RM_MAX_SVC_NAME + 1)),
        ("ApplicationType", ctypes.c_int),
        ("AppStatus", ctypes.c_uint32),
        ("TSSessionId", ctypes.c_uint32),
        ("bRestartable", ctypes.c_int),
    ]


def get_process_path(pid: int):
    """根据 PID 取进程可执行文件路径（尽力而为）。"""
    h = kernel32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_uint32(1024)
        if psapi.GetModuleFileNameExW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    except Exception:
        return None
    finally:
        kernel32.CloseHandle(h)


# 加载 Restart Manager API（Windows 自带 rstrtmgr.dll）
try:
    _rm = ctypes.windll.LoadLibrary("rstrtmgr.dll")
except Exception:
    _rm = None

RmStartSession = RmRegisterResources = RmGetList = RmEndSession = None
MoveFileExW = None
if _rm is not None:
    RmStartSession = _rm.RmStartSession
    RmStartSession.argtypes = [
        ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32,
        ctypes.c_wchar * CCH_RM_SESSION_KEY]
    RmStartSession.restype = ctypes.c_uint32

    RmRegisterResources = _rm.RmRegisterResources
    RmRegisterResources.argtypes = [
        ctypes.c_uint32, ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_wchar_p), ctypes.c_uint32,
        ctypes.POINTER(_RM_UNIQUE_PROCESS), ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_wchar_p)]
    RmRegisterResources.restype = ctypes.c_uint32

    RmGetList = _rm.RmGetList
    RmGetList.argtypes = [
        ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(_RM_PROCESS_INFO), ctypes.POINTER(ctypes.c_uint32)]
    RmGetList.restype = ctypes.c_uint32

    RmEndSession = _rm.RmEndSession
    RmEndSession.argtypes = [ctypes.c_uint32]
    RmEndSession.restype = ctypes.c_uint32

    MoveFileExW = kernel32.MoveFileExW
    MoveFileExW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    MoveFileExW.restype = ctypes.c_int


def find_lockers(paths: list):
    """返回占用进程列表（含 PID / 名称 / 路径）与重启原因掩码。"""
    session = ctypes.c_uint32(0)
    key = (ctypes.c_wchar * CCH_RM_SESSION_KEY)()
    if RmStartSession is None:
        raise OSError("Restart Manager 不可用（rstrtmgr.dll 缺失）")
    if RmStartSession(ctypes.byref(session), 0, key) != 0:
        raise ctypes.WinError()
    try:
        n = len(paths)
        file_array = (ctypes.c_wchar_p * n)(*paths)
        if RmRegisterResources(session, n, file_array, 0, None, 0, None) != 0:
            raise ctypes.WinError()
        pn_needed = ctypes.c_uint32(0)
        pn_info = ctypes.c_uint32(0)
        reboot = ctypes.c_uint32(0)
        err = RmGetList(session, ctypes.byref(pn_needed),
                        ctypes.byref(pn_info), None, ctypes.byref(reboot))
        if err not in (0, ERROR_MORE_DATA):
            raise ctypes.WinError(err)
        count = pn_needed.value
        if count == 0:
            return [], reboot.value
        infos = (_RM_PROCESS_INFO * count)()
        pn_info = ctypes.c_uint32(count)
        err = RmGetList(session, ctypes.byref(pn_needed),
                        ctypes.byref(pn_info), infos, ctypes.byref(reboot))
        if err != 0:
            raise ctypes.WinError(err)
        result = []
        for i in range(pn_info.value):
            info = infos[i]
            result.append({
                "pid": info.Process.dwProcessId,
                "app_name": info.strAppName,
                "service_name": info.strServiceShortName,
                "app_type": RM_APP_TYPE.get(info.ApplicationType,
                                            str(info.ApplicationType)),
                "restartable": bool(info.bRestartable),
                "path": get_process_path(info.Process.dwProcessId),
            })
        return result, reboot.value
    finally:
        RmEndSession(session)


def terminate_lockers(lockers: list):
    """精准终止列出的占用进程（仅针对检测到的 PID），返回 [(pid, ok, msg)]。"""
    self_pid = os.getpid()
    results = []
    for lk in lockers:
        pid = lk["pid"]
        if pid == self_pid:
            results.append((pid, False, "跳过自身进程"))
            continue
        h = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not h:
            results.append((pid, False, ctypes.WinError(ctypes.GetLastError()).strerror))
            continue
        ok = kernel32.TerminateProcess(h, 1)
        kernel32.CloseHandle(h)
        if ok:
            results.append((pid, True, "已终止"))
        else:
            results.append((pid, False, ctypes.WinError(ctypes.GetLastError()).strerror))
    return results


def schedule_delete_on_reboot(paths: list):
    """安排下次重启时删除（用于无法立即释放的系统级锁）。返回 [(path, ok, msg)]。"""
    out = []
    for p in paths:
        if MoveFileExW is None:
            out.append((p, False, "MoveFileEx 不可用"))
            continue
        ok = MoveFileExW(p, None, MOVEFILE_DELAY_UNTIL_REBOOT)
        if ok:
            out.append((p, True, "已安排重启删除"))
        else:
            out.append((p, False, ctypes.WinError(ctypes.GetLastError()).strerror))
    return out


def auto_unlock_and_retry(path: str, to_bin: bool):
    """删除失败时自动定位占用进程并精准终止，再重试删除；返回 (status, logs)。

    status: "ok"（重试成功）/ "skip"（仍失败）；logs 为待写入日志的文本列表。
    无论是否发现占用，最后都会再尝试一次删除（处理并行分块时占用进程已被
    其它任务终止的时序情况）。安全边界：跳过系统关键进程（RmCritical）。
    """
    logs = []
    if RmStartSession is None:
        return ("skip", logs)
    try:
        lockers, _ = find_lockers([os.path.abspath(path)])
    except OSError:
        lockers = []
    killable = [lk for lk in lockers if lk["app_type"] != "RmCritical"]
    if lockers and not killable:
        logs.append(f"占用方为系统关键进程，已跳过终止: {path}")
    elif killable:
        logs.append(
            f"自动解锁：{path} 被 {len(killable)} 个进程占用，尝试终止以释放占用"
        )
        for pid, ok, msg in terminate_lockers(killable):
            logs.append(f"  PID {pid}: {msg}")
        time.sleep(0.8)  # 等待系统回收句柄
    status, err = rc_delete(path, to_bin)
    if status.startswith("ok"):
        logs.append(("解锁后删除成功" if killable else "重试删除成功") + f": {path}")
        return ("ok", logs)
    # 仍失败：尝试安排下次重启删除（通常需管理员权限）
    for p, ok, msg in schedule_delete_on_reboot([path]):
        logs.append(f"  -> {p}: {msg}")
    return ("skip", logs)


# --------------------------------------------------------------------------
# 原生 C/C++ 引擎 LibRC.dll（可选，缺失则回退纯 Python）
# --------------------------------------------------------------------------
LibRC = None
_DLL_CANDIDATES = [
    os.path.join(APP_DIR, "LibRC.dll"),
    os.path.join(APP_DIR, "LibRC", "LibRC.dll"),
]
for _dll in _DLL_CANDIDATES:
    try:
        LibRC = ctypes.CDLL(_dll)
        LibRC.LRC_Delete.argtypes = (ctypes.c_wchar_p, ctypes.c_int)
        LibRC.LRC_Delete.restype = ctypes.c_int
        LibRC.LRC_GetLastError.argtypes = (ctypes.c_wchar_p, ctypes.c_int)
        LibRC.LRC_GetLastError.restype = None
        LibRC.LRC_WriteWinErr.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p)
        LibRC.LRC_WriteWinErr.restype = ctypes.c_int
        break
    except OSError:
        LibRC = None


def rc_delete(path: str, to_bin: bool):
    """删除单个文件/目录。优先使用 LibRC.dll（C/C++ 原生实现），否则回退纯 Python。
    返回 ("ok", None) / ("ok_force", None) / ("skipped", 错误信息)。"""
    if LibRC is not None:
        # DLL 内部用静态缓冲保存最后一次错误，多线程下必须串行调用
        with _DLL_LOCK:
            # 0=回收站 1=永久 2=提权强制
            if LibRC.LRC_Delete(path, 0 if to_bin else 1) == 0:
                return ("ok", None)
            if LibRC.LRC_Delete(path, 2) == 0:
                return ("ok_force", None)
            buf = ctypes.create_unicode_buffer(1024)
            LibRC.LRC_GetLastError(buf, 1024)
            LibRC.LRC_WriteWinErr(path, buf.value)
            return ("skipped", buf.value)
    # 纯 Python 回退
    try:
        if to_bin:
            if move_to_recycle_bin(path):
                return ("ok", None)
            permanent_delete(path)
        else:
            permanent_delete(path)
        return ("ok", None)
    except Exception as exc:
        last = exc
    else:
        last = None
    if force_delete(path):
        return ("ok_force", None)
    write_winerr(path, str(last) if last else "权限不足或文件被占用")
    return ("skipped", str(last) if last else "权限不足或文件被占用")


# --------------------------------------------------------------------------
# 深度清理：系统工具动作
# --------------------------------------------------------------------------
CREATE_NO_WINDOW = 0x08000000


def empty_recycle_bin():
    """清空所有磁盘的回收站。返回 (是否成功, 说明)。"""
    try:
        shell32 = ctypes.windll.shell32
        SHERB_NOCONFIRMATION = 0x1
        SHERB_NOPROGRESSUI = 0x2
        SHERB_NOSOUND = 0x4
        flags = SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
        res = shell32.SHEmptyRecycleBinW(None, None, flags)
        if res == 0:
            return (True, "回收站已清空")
        return (False, f"SHEmptyRecycleBin 返回 {res}")
    except Exception as e:
        return (False, str(e))


def recycle_bin_size():
    """查询回收站占用大小与项目数（统计所有驱动器）。返回 (bytes, count)。"""
    try:
        class SHQUERYRBINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint32),
                        ("i64Size", ctypes.c_int64),
                        ("i64NumItems", ctypes.c_int64)]

        info = SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(SHQUERYRBINFO)
        shell32 = ctypes.windll.shell32
        shell32.SHQueryRecycleBinW.argtypes = (
            ctypes.c_wchar_p, ctypes.POINTER(SHQUERYRBINFO)
        )
        shell32.SHQueryRecycleBinW.restype = ctypes.c_int
        # pszRootPath 传 NULL = 统计所有驱动器的回收站
        res = shell32.SHQueryRecycleBinW(None, ctypes.byref(info))
        if res == 0:
            return (int(info.i64Size), int(info.i64NumItems))
        return (0, 0)
    except Exception:
        return (0, 0)


def _run_hidden(cmd, timeout=1800):
    """静默执行系统命令，返回 (是否成功, 输出尾部)。"""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW,
        )
        out = (r.stdout or "") + (r.stderr or "")
        return (r.returncode == 0, out.strip()[-500:] or f"返回码 {r.returncode}")
    except Exception as e:
        return (False, str(e))


def dism_component_cleanup():
    """DISM 组件存储（WinSxS）清理。返回 (是否成功, 说明)。"""
    return _run_hidden(
        ["dism.exe", "/Online", "/Cleanup-Image", "/StartComponentCleanup"]
    )


def flush_dns():
    """刷新 DNS 解析缓存。返回 (是否成功, 说明)。"""
    return _run_hidden(["ipconfig", "/flushdns"], timeout=60)


def run_deep_action(name: str):
    """按名称执行深度清理动作。返回 (是否成功, 说明)。"""
    if name == "empty_recycle":
        return empty_recycle_bin()
    if name == "dism_cleanup":
        return dism_component_cleanup()
    if name == "flush_dns":
        return flush_dns()
    return (False, "未知动作")


# --------------------------------------------------------------------------
# 自动提权（UAC）
# --------------------------------------------------------------------------
CONFIG_PATH = os.path.join(APP_DIR, "config.json")


def load_settings() -> dict:
    """读取本地配置（如自动提权开关）。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    """写入本地配置。"""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass


def is_admin() -> bool:
    """当前进程是否已具备管理员权限。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_as_admin() -> bool:
    """以管理员身份重启本程序（触发 UAC 提示）。成功启动返回 True。"""
    try:
        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.argtypes = (
            ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
            ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int,
        )
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        if getattr(sys, "frozen", False):
            exe = sys.executable
            params = "--elevated"
        else:
            exe = sys.executable or "pythonw.exe"
            params = '"%s" --elevated' % os.path.abspath(__file__)
        # 第 6 个参数 SW_SHOWNORMAL = 1
        ret = shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        return ret is not None and int(ret) > 32
    except Exception:
        return False


# --------------------------------------------------------------------------
# 主界面
# --------------------------------------------------------------------------
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("ForceRm 清理工具")
        self.geometry("760x680")
        self.minsize(640, 540)

        self.results: dict = {}          # key -> {"size":int, "count":int}
        self.check_vars: dict = {}       # key -> BooleanVar
        self.size_labels: dict = {}      # key -> CTkLabel
        self.rows: dict = {}             # key -> 行容器（用于搜索过滤）
        self.file_cache: dict = {}       # key -> 扫描到的文件列表（清理时免重复遍历）
        self.working = False
        self._spin_job = None            # 加载动画的 after 任务
        self._spin_text = ""             # “正在清理 xx”
        self._spin_i = 0
        self._syncing = False            # 防止 主框<->子项 勾选联动死循环

        self._build_ui()

    # ------------------------- UI 构建（火绒风格）-------------------------
    def _build_ui(self):
        self.configure(fg_color=C_BG)

        # 窗口图标
        try:
            if PIL_OK and os.path.exists(LOGO_PATH):
                from PIL import ImageTk
                self._win_icon = ImageTk.PhotoImage(Image.open(LOGO_PATH).resize((64, 64)))
                self.iconphoto(True, self._win_icon)
        except Exception:
            pass

        # ===== 顶栏：logo + 标题 + 权限 =====
        header = ctk.CTkFrame(self, fg_color=C_CARD, corner_radius=0, height=68)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        logo_img = None
        if PIL_OK:
            try:
                _pil = Image.open(LOGO_PATH)
                logo_img = ctk.CTkImage(light_image=_pil, dark_image=_pil, size=(40, 40))
                self._logo_img = logo_img
            except Exception:
                logo_img = None
        if logo_img is not None:
            ctk.CTkLabel(header, text="", image=logo_img).pack(
                side="left", padx=(18, 10), pady=14
            )
        else:
            ctk.CTkLabel(
                header, text="FR", width=40, height=40,
                font=ctk.CTkFont(size=15, weight="bold"),
                fg_color=C_TEAL, text_color="#FFFFFF", corner_radius=8,
            ).pack(side="left", padx=(18, 10), pady=14)

        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.pack(side="left", pady=14)
        ctk.CTkLabel(
            title_box, text="ForceRm", anchor="w",
            font=ctk.CTkFont(size=19, weight="bold"), text_color=C_TXT,
        ).pack(anchor="w")
        ctk.CTkLabel(
            title_box, text="直接删除 · 不可恢复", anchor="w",
            font=ctk.CTkFont(size=11), text_color=C_SUB,
        ).pack(anchor="w")

        self.elevate_btn = ctk.CTkButton(
            header, text="以管理员身份重启", width=132, height=32,
            fg_color="transparent", border_width=1, border_color=C_BORDER,
            text_color=C_TXT, hover_color="#EEF3F6", corner_radius=8,
            command=self._on_elevate_now,
        )
        self.elevate_btn.pack(side="right", padx=(8, 18), pady=18)

        self.admin_lbl = ctk.CTkLabel(
            header, text="", font=ctk.CTkFont(size=12), text_color=C_SUB,
        )
        self.admin_lbl.pack(side="right", padx=4, pady=18)

        # ===== 主体：左侧导航 + 右侧内容 =====
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=14, pady=12)

        # --- 侧边导航 ---
        sidebar = ctk.CTkFrame(body, fg_color=C_CARD, corner_radius=12, width=168)
        sidebar.pack(side="left", fill="y", padx=(0, 12))
        sidebar.pack_propagate(False)

        ctk.CTkLabel(
            sidebar, text="清理范围", anchor="w",
            font=ctk.CTkFont(size=11), text_color=C_SUB,
        ).pack(fill="x", padx=16, pady=(16, 6))

        self.category_filter = "全部项目"
        self.nav_buttons = {}
        for name in ["全部项目", "软件垃圾", "系统垃圾", "深度清理"]:
            b = ctk.CTkButton(
                sidebar, text=name, anchor="w", height=38,
                fg_color="transparent", text_color=C_TXT, hover_color="#EEF3F6",
                corner_radius=8, font=ctk.CTkFont(size=13),
                command=lambda n=name: self._set_category(n),
            )
            b.pack(fill="x", padx=10, pady=2)
            self.nav_buttons[name] = b

        side_bottom = ctk.CTkFrame(sidebar, fg_color="transparent")
        side_bottom.pack(side="bottom", fill="x", padx=16, pady=14)
        self.auto_elevate_var = ctk.BooleanVar(
            value=bool(load_settings().get("auto_elevate", True))
        )
        ctk.CTkCheckBox(
            side_bottom, text="自动提权", variable=self.auto_elevate_var,
            font=ctk.CTkFont(size=12), text_color=C_TXT,
            fg_color=C_TEAL, hover_color=C_TEAL_D,
            checkbox_width=18, checkbox_height=18, corner_radius=5,
            command=self._on_auto_elevate_toggle,
        ).pack(anchor="w", pady=(0, 6))
        ctk.CTkLabel(
            side_bottom, text="仅清理白名单内缓存", anchor="w",
            font=ctk.CTkFont(size=10), text_color=C_SUB,
        ).pack(anchor="w")

        # --- 右侧主区（grid：列表可伸缩、底部日志固定）---
        main = ctk.CTkFrame(body, fg_color="transparent")
        main.pack(side="left", fill="both", expand=True)
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(3, weight=1)

        # 状态卡：已选容量 + 扫描 / 清理 + 清理方式
        status_card = ctk.CTkFrame(main, fg_color=C_CARD, corner_radius=12)
        status_card.grid(row=0, column=0, sticky="ew", pady=(0, 10))

        left = ctk.CTkFrame(status_card, fg_color="transparent")
        left.pack(side="left", fill="both", expand=True, padx=20, pady=16)
        ctk.CTkLabel(
            left, text="已选可清理", anchor="w",
            font=ctk.CTkFont(size=12), text_color=C_SUB,
        ).pack(anchor="w")
        self.total_lbl = ctk.CTkLabel(
            left, text="0 B", anchor="w",
            font=ctk.CTkFont(size=32, weight="bold"), text_color=C_TEAL,
        )
        self.total_lbl.pack(anchor="w", pady=(2, 0))
        self.count_lbl = ctk.CTkLabel(
            left, text="共 0 个文件", anchor="w",
            font=ctk.CTkFont(size=11), text_color=C_SUB,
        )
        self.count_lbl.pack(anchor="w")

        right = ctk.CTkFrame(status_card, fg_color="transparent")
        right.pack(side="right", padx=20, pady=16)
        btn_row = ctk.CTkFrame(right, fg_color="transparent")
        btn_row.pack(anchor="e")
        self.scan_btn = ctk.CTkButton(
            btn_row, text="扫描", width=116, height=40,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=C_BLUE, hover_color=C_BLUE_D, corner_radius=10,
            command=self._on_scan,
        )
        self.scan_btn.pack(side="left", padx=(0, 10))
        self.clean_btn = ctk.CTkButton(
            btn_row, text="清理", width=116, height=40,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=C_TEAL, hover_color=C_TEAL_D, corner_radius=10,
            command=self._on_clean,
        )
        self.clean_btn.pack(side="left")

        mode_row = ctk.CTkFrame(right, fg_color="transparent")
        mode_row.pack(anchor="e", pady=(12, 0))
        ctk.CTkLabel(
            mode_row, text="清理方式", font=ctk.CTkFont(size=11), text_color=C_SUB,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(
            mode_row, text="直接删除", font=ctk.CTkFont(size=12), text_color=C_TXT,
        ).pack(side="left")

        # 搜索 + 计数 + 全选
        flt = ctk.CTkFrame(main, fg_color="transparent")
        flt.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.filter_var = ctk.StringVar()
        self.filter_entry = ctk.CTkEntry(
            flt, textvariable=self.filter_var, placeholder_text="搜索清理项目…",
            height=34, corner_radius=8, border_color=C_BORDER, fg_color=C_CARD,
        )
        self.filter_entry.pack(side="left", fill="x", expand=True)
        self.filter_entry.bind("<KeyRelease>", self._apply_filter)
        self.filter_count_lbl = ctk.CTkLabel(
            flt, text="", width=64, anchor="e",
            font=ctk.CTkFont(size=11), text_color=C_SUB,
        )
        self.filter_count_lbl.pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            flt, text="全选", width=52, height=34, fg_color="transparent",
            border_width=1, border_color=C_BORDER, text_color=C_TXT,
            hover_color="#EEF3F6", corner_radius=8,
            command=lambda: self._select_all(True),
        ).pack(side="left", padx=(8, 6))
        ctk.CTkButton(
            flt, text="全不选", width=64, height=34, fg_color="transparent",
            border_width=1, border_color=C_BORDER, text_color=C_TXT,
            hover_color="#EEF3F6", corner_radius=8,
            command=lambda: self._select_all(False),
        ).pack(side="left")

        # 软件垃圾：单一总勾选（固定插槽，便于按分类显隐）
        self.software_keys = [t["key"] for t in JUNK_TARGETS if t.get("category") == "软件垃圾"]
        self.software_master = ctk.BooleanVar(value=False)
        self.software_slot = ctk.CTkFrame(main, fg_color="transparent")
        self.software_slot.grid(row=2, column=0, sticky="ew")
        self.software_master_frame = ctk.CTkFrame(
            self.software_slot, fg_color=C_CARD, corner_radius=10,
            border_width=1, border_color=C_BORDER,
        )
        self.software_master_frame.pack(fill="x", pady=(0, 8))
        ctk.CTkCheckBox(
            self.software_master_frame,
            text="软件垃圾   全部常用软件缓存 · 一键勾选",
            variable=self.software_master,
            font=ctk.CTkFont(size=13, weight="bold"), text_color=C_TXT,
            fg_color=C_TEAL, hover_color=C_TEAL_D,
            checkbox_width=20, checkbox_height=20, corner_radius=5,
            command=lambda: self._toggle_software(self.software_master.get()),
        ).pack(side="left", anchor="w", padx=16, pady=12)
        ctk.CTkLabel(
            self.software_master_frame, text=f"{len(self.software_keys)} 个软件",
            font=ctk.CTkFont(size=11), text_color=C_SUB,
        ).pack(side="right", padx=16)

        # 可清理项目列表
        self.scroll = ctk.CTkScrollableFrame(
            main, fg_color=C_CARD, corner_radius=12,
        )
        self.scroll.grid(row=3, column=0, sticky="nsew", pady=(0, 8))

        for t in JUNK_TARGETS:
            key = t["key"]
            self.check_vars[key] = ctk.BooleanVar(value=t["default"])
            self.results[key] = {"size": 0, "count": 0}

            if key in self.software_keys:
                # 软件项不单独陈列，仅由“软件垃圾”总框统一控制；
                # 仍保留变量，供扫描统计与清理判定使用。
                self.check_vars[key].set(self.software_master.get())
                continue

            row = ctk.CTkFrame(self.scroll, fg_color="transparent")
            row.pack(fill="x", padx=8, pady=2)

            cb = ctk.CTkCheckBox(
                row, text=f"{t['name']}   ·   风险{t['risk']}",
                variable=self.check_vars[key],
                font=ctk.CTkFont(size=13), text_color=C_TXT,
                fg_color=C_TEAL, hover_color=C_TEAL_D,
                checkbox_width=20, checkbox_height=20, corner_radius=5,
                command=self._update_total,
            )
            cb.pack(side="left", anchor="w", padx=10, pady=8)

            size_lbl = ctk.CTkLabel(
                row, text="未扫描", width=110, anchor="e",
                font=ctk.CTkFont(size=12), text_color=C_SUB,
            )
            size_lbl.pack(side="right", padx=12, pady=8)
            self.size_labels[key] = size_lbl
            self.rows[key] = row

        # 状态行 + 日志（底部固定）
        self.status_lbl = ctk.CTkLabel(
            main, text="就绪", anchor="w",
            font=ctk.CTkFont(size=12), text_color=C_SUB,
        )
        self.status_lbl.grid(row=4, column=0, sticky="ew", padx=6, pady=(0, 4))

        self.log = ctk.CTkTextbox(
            main, height=104, fg_color=C_CARD, corner_radius=12,
            border_width=1, border_color=C_BORDER, text_color=C_TXT,
            font=ctk.CTkFont(size=11),
        )
        self.log.grid(row=5, column=0, sticky="ew")

        # 初始分类（同时完成首次过滤与导航高亮）
        self._set_category("全部项目")

        if is_admin():
            self.admin_lbl.configure(text="管理员", text_color="#2E9E5B")
            self.elevate_btn.configure(state="disabled")
        else:
            self.admin_lbl.configure(text="标准用户")

        self._log("原生引擎：" + ("LibRC.dll" if LibRC is not None else "纯 Python"))
        self._log("权限：" + ("管理员" if is_admin() else "标准用户"))

    def _on_auto_elevate_toggle(self):
        s = load_settings()
        s["auto_elevate"] = bool(self.auto_elevate_var.get())
        save_settings(s)
        self._log(f"自动提权：{'开启' if s['auto_elevate'] else '关闭'}")

    def _on_elevate_now(self):
        if is_admin():
            mb.showinfo("提示", "当前已是管理员权限。")
            return
        self._log("正在申请管理员权限")
        if run_as_admin():
            self._log("提权实例已启动")
            self.after(300, self.destroy)
        else:
            mb.showerror("提权失败", "UAC 被取消或系统禁止提权")


    # ------------------------- 搜索过滤 -------------------------
    def _set_category(self, name):
        """左侧导航切换分类：高亮当前项并重新过滤。"""
        self.category_filter = name
        for n, b in getattr(self, "nav_buttons", {}).items():
            if n == name:
                b.configure(fg_color="#E8F3F7", text_color=C_TEAL,
                            font=ctk.CTkFont(size=13, weight="bold"))
            else:
                b.configure(fg_color="transparent", text_color=C_TXT,
                            font=ctk.CTkFont(size=13))
        self._apply_filter()

    def _apply_filter(self, *args):
        """按分类 + 名称 / 描述 / key 过滤列表行，保持原有顺序。"""
        if not self.rows:
            return
        q = (self.filter_var.get() or "").strip().lower()
        cat = getattr(self, "category_filter", "全部项目")
        shown = 0
        for t in JUNK_TARGETS:
            key = t["key"]
            row = self.rows.get(key)
            if row is None:
                # 软件垃圾项不单独陈列，仅由总框显示
                continue
            row.pack_forget()
            if cat == "软件垃圾" and t.get("type") != "smart":
                continue
            if cat == "系统垃圾" and key in self.software_keys:
                continue
            if cat == "深度清理" and not (t.get("risk") == "高" or not t.get("default", True)):
                continue
            hay = f"{t.get('name','')} {t.get('desc','')} {key}".lower()
            if not q or q in hay:
                row.pack(fill="x", padx=8, pady=2)
                shown += 1
        # 软件垃圾总框：仅“全部项目 / 软件垃圾”分类下显示
        if hasattr(self, "software_master_frame"):
            self.software_master_frame.pack_forget()
            if cat in ("全部项目", "软件垃圾"):
                self.software_master_frame.pack(fill="x", pady=(0, 8))
        total_rows = len(self.rows)
        if q:
            self.filter_count_lbl.configure(text=f"{shown}/{total_rows}")
        elif cat != "全部项目":
            self.filter_count_lbl.configure(text=f"{shown} 项")
        else:
            self.filter_count_lbl.configure(text="")

    # ------------------------- 日志 -------------------------
    def _log(self, msg: str):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.log.insert("end", f"[{ts}] {msg}\n")
        self.log.see("end")

    # ------------------------- 加载动画 -------------------------
    def _start_spinner(self, text: str):
        """在状态行显示 text + 旋转图标。"""
        self._stop_spinner()
        self._spin_text = text
        self._spin_i = 0
        self._tick_spinner()

    def _tick_spinner(self):
        frame = SPIN_FRAMES[self._spin_i % len(SPIN_FRAMES)]
        self._spin_i += 1
        self.status_lbl.configure(text=f"{self._spin_text}  {frame}")
        self._spin_job = self.after(SPIN_INTERVAL_MS, self._tick_spinner)

    def _stop_spinner(self, final_text: str = None):
        """停止动画；传入 final_text 则更新为最终状态。"""
        if self._spin_job is not None:
            try:
                self.after_cancel(self._spin_job)
            except Exception:
                pass
            self._spin_job = None
        if final_text is not None:
            self.status_lbl.configure(text=final_text)

    def _set_progress(self, text: str):
        """更新动画文字但不重启计时器（用于并行任务的进度刷新）。"""
        if self._spin_job is None:
            self._start_spinner(text)
        else:
            self._spin_text = text

    def _update_total(self):
        total = 0
        cnt = 0
        for k in self.results:
            if self.check_vars[k].get():
                total += self.results[k]["size"]
                cnt += self.results[k]["count"]
        if hasattr(self, "total_lbl"):
            self.total_lbl.configure(text=human_size(total))
        if hasattr(self, "count_lbl"):
            self.count_lbl.configure(text=f"共 {cnt} 个文件")

    def _select_all(self, value: bool):
        """一键全选 / 全不选所有清理项目。"""
        for k in self.check_vars:
            self.check_vars[k].set(value)
        self._sync_software_master()
        self._update_total()

    # ------------------------- 软件垃圾 总勾选项 -------------------------
    def _set_master(self, value: bool):
        """在防护标志内设置主框变量，避免触发其 command 造成递归。"""
        self._syncing = True
        try:
            self.software_master.set(value)
        finally:
            self._syncing = False

    def _toggle_software(self, value: bool):
        """“软件垃圾”主框：一键勾选/取消全部软件子项。"""
        if self._syncing:
            return
        self._syncing = True
        try:
            for k in self.software_keys:
                self.check_vars[k].set(value)
        finally:
            self._syncing = False
        self._update_total()
        all_on = all(self.check_vars[k].get() for k in self.software_keys)
        if self.software_master.get() != all_on:
            self._set_master(all_on)

    def _sync_software_master(self):
        """全选/全不选后，让主框反映软件子项的整体勾选状态。"""
        if not self.software_keys:
            return
        all_on = all(self.check_vars[k].get() for k in self.software_keys)
        if self.software_master.get() != all_on:
            self._set_master(all_on)

    # ------------------------- 扫描 -------------------------
    def _scan_one(self, t):
        """扫描单个目标，返回 (key, size, count, files)。在线程池中执行。"""
        key = t["key"]
        if t.get("type") == "action":
            # 系统动作：无文件可扫；回收站可统计真实占用
            if t.get("action") == "empty_recycle":
                sz, cnt = recycle_bin_size()
                return key, sz, cnt, []
            return key, 0, 1, []
        files = smart_collect() if t.get("type") == "smart" else collect_target(t)
        size = 0
        for f in files:
            try:
                size += os.path.getsize(f)
            except OSError:
                pass
        return key, size, len(files), files

    def _on_scan(self):
        if self.working:
            return
        self.working = True
        self.scan_btn.configure(state="disabled")
        self.clean_btn.configure(state="disabled")
        n = worker_count()
        self._log(f"开始扫描（{n} 线程）……")

        def worker():
            with ThreadPoolExecutor(max_workers=n) as ex:
                futs = [ex.submit(self._scan_one, t) for t in JUNK_TARGETS]
                for fu in as_completed(futs):
                    try:
                        key, size, cnt, files = fu.result()
                    except Exception as e:
                        self.after(0, lambda e=e: self._log(f"扫描任务异常：{e}"))
                        continue
                    self.results[key] = {"size": size, "count": cnt}
                    self.file_cache[key] = files
                    self.after(0, self._refresh_row, key)
            self.after(0, self._finish_scan)

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_row(self, key):
        if key not in self.size_labels:
            # 软件垃圾项由总框统一控制，无独立行，仅刷新总计
            self._update_total()
            return
        r = self.results[key]
        t = next(x for x in JUNK_TARGETS if x["key"] == key)
        if t.get("type") == "action":
            if r["size"] > 0:
                self.size_labels[key].configure(
                    text=f"{human_size(r['size'])} · {r['count']}个"
                )
            else:
                self.size_labels[key].configure(text="可执行")
            self._update_total()
            return
        if r["count"] == 0:
            self.size_labels[key].configure(text="无文件")
        else:
            self.size_labels[key].configure(
                text=f"{human_size(r['size'])} · {r['count']}个"
            )
            self._log(f"[{t['name']}] {human_size(r['size'])}")
        self._update_total()

    def _finish_scan(self):
        self.working = False
        self.scan_btn.configure(state="normal")
        self.clean_btn.configure(state="normal")
        self._log("扫描完成")

    # ------------------------- 清理 -------------------------
    def _on_clean(self):
        if self.working:
            return
        selected = [t for t in JUNK_TARGETS if self.check_vars[t["key"]].get()]
        if not selected:
            mb.showinfo("提示", "请先勾选至少一类要清理的项目。")
            return
        total = sum(self.results[t["key"]]["size"] for t in selected)
        count = sum(self.results[t["key"]]["count"] for t in selected)
        if count == 0:
            mb.showinfo("提示", "当前勾选项没有可清理的文件。")
            return

        mode = "直接删除"
        to_bin = False
        ans = mb.askyesno(
            "确认清理",
            f"即将清理 {count} 个文件，约 {human_size(total)}。\n"
            f"方式：{mode}（不可恢复）\n\n确定继续吗？",
        )
        if not ans:
            self._log("已取消清理。")
            return

        self.working = True
        self.scan_btn.configure(state="disabled")
        self.clean_btn.configure(state="disabled")
        self._log(f"开始清理（{mode}）……")

        try:
            is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            is_admin = False
        if not is_admin:
            self._log("未以管理员运行，强制删除可能受限")

        nw = worker_count()
        self._log(f"开始清理（{mode}，{nw} 线程）……")

        def worker():
            freed = 0
            ok = 0
            fail = 0

            # 1) 系统动作（DISM / 清空回收站等）必须串行执行
            for t in selected:
                if t.get("type") != "action":
                    continue
                n = t["name"]
                self.after(0, lambda m=f"执行系统动作：{n}": self._log(m))
                self.after(0, lambda m=n: self._set_progress("正在清理 " + m))
                ok_act, msg_act = run_deep_action(t.get("action", ""))
                if ok_act:
                    ok += 1
                    self.after(0, lambda m=f"完成 [{n}]": self._log(m))
                else:
                    fail += 1
                    self.after(0, lambda m=f"失败 [{n}]：{msg_act}": self._log(m))

            # 2) 文件类目标：复用扫描缓存，切块后并行删除
            file_targets = [t for t in selected if t.get("type") != "action"]
            jobs = []
            for t in file_targets:
                if t.get("type") == "smart":
                    files = self.file_cache.get(t["key"])
                    if files is None:
                        files = smart_collect()
                else:
                    files = self.file_cache.get(t["key"]) or collect_target(t)
                files = [f for f in files if os.path.exists(f)]
                for i in range(0, len(files), DELETE_CHUNK):
                    jobs.append(files[i:i + DELETE_CHUNK])

            total_jobs = len(jobs)
            self.after(0, lambda: self._set_progress(f"正在清理（0/{total_jobs} 项）"))
            logged = [0]

            def delete_chunk(chunk):
                """删除一批文件，返回 (成功数, 失败数, 释放字节数)。"""
                c_ok = c_fail = 0
                c_freed = 0
                sizes = {}
                for f in chunk:
                    try:
                        sizes[f] = os.path.getsize(f)
                    except OSError:
                        sizes[f] = 0

                pending = list(sizes)
                if to_bin and LibRC is None:
                    # 回收站模式：整批合并为一次 Shell 调用
                    failed = bulk_recycle_bin(pending)
                    failed_set = set(failed)
                    for f, sz in sizes.items():
                        if f not in failed_set:
                            c_ok += 1
                            c_freed += sz
                    pending = failed

                for f in pending:
                    status, err = rc_delete(f, to_bin)
                    if status.startswith("ok"):
                        c_ok += 1
                        c_freed += sizes[f]
                        if status == "ok_force" and logged[0] < LOG_LIMIT:
                            logged[0] += 1
                            self.after(0, lambda m=f"强制删除: {f}": self._log(m))
                    else:
                        # 删除失败：自动检测占用并精准终止占用进程后重试
                        st, msgs = auto_unlock_and_retry(f, to_bin)
                        for m in msgs:
                            if logged[0] < LOG_LIMIT:
                                logged[0] += 1
                                mm = m
                                self.after(0, lambda mm=mm: self._log(mm))
                        if st == "ok":
                            c_ok += 1
                            c_freed += sizes[f]
                        else:
                            c_fail += 1
                            if logged[0] < LOG_LIMIT:
                                logged[0] += 1
                                self.after(0, lambda m=f"跳过(无法删除): {f}": self._log(m))
                return c_ok, c_fail, c_freed

            done = 0
            with ThreadPoolExecutor(max_workers=nw) as ex:
                futs = [ex.submit(delete_chunk, c) for c in jobs]
                for fu in as_completed(futs):
                    try:
                        c_ok, c_fail, c_freed = fu.result()
                    except Exception as e:
                        self.after(0, lambda e=e: self._log(f"删除任务异常：{e}"))
                        continue
                    ok += c_ok
                    fail += c_fail
                    freed += c_freed
                    done += 1
                    self.after(
                        0,
                        lambda d=done: self._set_progress(
                            f"正在清理（{d}/{total_jobs} 项）"
                        ),
                    )

            # 3) 清理残留空目录（所有文件删除完成后再执行）
            for t in file_targets:
                for base in t["paths"]:
                    if base and os.path.isdir(base):
                        try:
                            remove_empty_dirs(base)
                        except Exception:
                            pass
                if t.get("type") == "smart":
                    try:
                        prune_smart_dirs(self.file_cache.get(t["key"]) or [])
                    except Exception:
                        pass
                self.after(0, lambda nm=t["name"]: self._log(f"完成 [{nm}]"))

            self.after(0, self._finish_clean, freed, ok, fail)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_clean(self, freed, ok, fail):
        self.working = False
        self.scan_btn.configure(state="normal")
        self.clean_btn.configure(state="normal")
        self._log(f"清理结束：成功 {ok} 个，失败 {fail} 个，释放 {human_size(freed)}。")
        self._stop_spinner("清理完成")
        mb.showinfo("清理完成", f"成功删除 {ok} 个文件，释放 {human_size(freed)}。")
        self._on_scan()


def remove_empty_dirs(base: str) -> None:
    """自底向上删除 base 下已清空的非顶级目录，释放残留空文件夹。"""
    if not os.path.isdir(base) or is_link(base):
        return
    for root, dirs, files in os.walk(base, topdown=False):
        if root == base:
            continue
        try:
            if not dirs and not files:
                os.rmdir(root)
        except OSError:
            pass


# --------------------------------------------------------------------------
if __name__ == "__main__":
    # 隐藏自检：验证冻结发行版的资源路径、规则库与智能扫描
    if "--selftest" in sys.argv:
        _out = {
            "frozen": bool(getattr(sys, "frozen", False)),
            "app_dir": APP_DIR,
            "lib_dir": LIBRM_DIR,
            "lib_exists": os.path.isdir(LIBRM_DIR),
            "lib_rules": len(load_software_targets(LIBRM_DIR)),
            "lib_files": len(glob.glob(os.path.join(LIBRM_DIR, "*.json"))),
            "targets": len(JUNK_TARGETS),
            "smart": any(t.get("type") == "smart" for t in JUNK_TARGETS),
            "frm_lib": sum(1 for t in JUNK_TARGETS if t["key"].startswith("frm_lib_")),
            "smart_files": len(smart_collect()),
        }
        with open(os.path.join(APP_DIR, "_selftest.json"), "w", encoding="utf-8") as _fh:
            json.dump(_out, _fh, ensure_ascii=False, indent=2)
        sys.exit(0)
    # 自动提权：非管理员且本身不是提权实例时，重启自身申请 UAC 后退出
    if "--elevated" not in sys.argv and not is_admin() \
            and bool(load_settings().get("auto_elevate", True)):
        if run_as_admin():
            sys.exit(0)     # 已启动提权实例，当前实例退出
        # UAC 被取消或失败：继续以标准用户运行
    App().mainloop()
