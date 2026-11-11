# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""ForceRm 自启动静默清理入口（无窗口）。

只加载同目录的 ForceRm.pyw 作为清理引擎，不加载 ForceRm-high.pyw，
不创建图形界面，不写入任何日志或结果文件，整体运行时间不超过 10 秒。

运行方式：pythonw.exe autoForceClear.pyw
自启动注册：autoForceClear.cmd
"""
import os
import sys
import time
import types
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.dont_write_bytecode = True

START = time.monotonic()
BUDGET = 9.0
ENGINE_FILE = "ForceRm.pyw"
MAX_FILES_PER_TARGET = 1500

# 不参与静默清理的项目：系统动作、智能全盘扫描与 WinSxS 组件存储
SKIP_KEYS = {
    "act_dism_cleanup", "act_empty_recycle", "act_flush_dns",
    "windows_old", "windows_update_leftover", "event_logs", "system_logs",
    "memory_dumps", "font_cache", "defender_history",
    "delivery_optimization", "smart_software_cache",
}
SKIP_WORDS = (
    "winsxs", "dism", "servicing", "component_cleanup", "panther",
    "sleepstudy", "livekernel", "recycle", "pagefile", "hiberfil", "ver_",
)


class _NoGui:
    """界面依赖替身：可调用、可取任意属性、可作为基类，不创建任何窗口。"""

    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return self

    def __getattr__(self, name):
        return self


def _install_gui_stubs():
    """注入替身模块，使引擎导入阶段不加载 tkinter / customtkinter，也不弹对话框。"""
    tk = types.ModuleType("tkinter")
    msg = types.ModuleType("tkinter.messagebox")
    for name in ("showerror", "showinfo", "showwarning",
                 "askyesno", "askokcancel", "askquestion"):
        setattr(msg, name, lambda *a, **k: False)
    tk.messagebox = msg
    sys.modules["tkinter"] = tk
    sys.modules["tkinter.messagebox"] = msg

    ctk = types.ModuleType("customtkinter")
    ctk.CTk = _NoGui
    setattr(ctk, "__getattr__", lambda name: _NoGui)
    sys.modules["customtkinter"] = ctk


def _load_engine():
    """仅载入 ForceRm.pyw 模块，不执行其主入口，不启动界面。"""
    import importlib.machinery
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, ENGINE_FILE)
    if not os.path.isfile(path):
        return None
    loader = importlib.machinery.SourceFileLoader("frm_silent_engine", path)
    spec = importlib.util.spec_from_loader("frm_silent_engine", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _prepare(eng):
    """关闭引擎的日志、提权与配置写入路径，保证静默且不留文件。"""
    eng.write_winerr = lambda *a, **k: None
    eng.LibRC = None
    eng.force_delete = lambda path: False
    eng.save_settings = lambda *a, **k: None
    eng.run_as_admin = lambda *a, **k: False


def _allow(eng, target):
    """筛选轻量文件类目标，排除系统动作、深度项目与 WinSxS 组件存储。"""
    if target.get("type") in ("action", "smart"):
        return False
    if target.get("deep"):
        return False
    key = str(target.get("key", "")).lower()
    if key in SKIP_KEYS or any(w in key for w in SKIP_WORDS):
        return False
    blob = (key + " " + str(target.get("name", "")) + " "
            + " ".join(str(p) for p in target.get("paths", []))).lower()
    if "winsxs" in blob or "startcomponentcleanup" in blob:
        return False
    return eng.version_allows(target, eng.WIN)


def _clean_one(eng, target, deadline):
    """扫描并直接删除单个目标下的文件，全程受截止时间约束。"""
    if time.monotonic() >= deadline:
        return 0
    try:
        files = eng.collect_target(target)
    except Exception:
        return 0
    done = 0
    for path in files[:MAX_FILES_PER_TARGET]:
        if time.monotonic() >= deadline:
            break
        try:
            if not os.path.exists(path):
                continue
            status, _ = eng.rc_delete(path, False)
            if status.startswith("ok"):
                done += 1
        except Exception:
            continue
    return done


def _run(eng, deadline):
    targets = [t for t in eng.JUNK_TARGETS if _allow(eng, t)]
    if not targets:
        return
    workers = min(8, max(2, os.cpu_count() or 4))
    ex = ThreadPoolExecutor(max_workers=workers)
    futures = [ex.submit(_clean_one, eng, t, deadline) for t in targets]
    try:
        for fut in as_completed(futures,
                                timeout=max(0.05, deadline - time.monotonic())):
            if time.monotonic() >= deadline:
                break
            try:
                fut.result()
            except Exception:
                continue
    except Exception:
        pass
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def _worker():
    deadline = START + BUDGET
    eng = _load_engine()
    if eng is None:
        return
    _prepare(eng)
    _run(eng, deadline)


def _finish():
    """强制结束进程，确保耗时上限不被残留线程突破。"""
    try:
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(0)


def main():
    _install_gui_stubs()
    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    worker.join(timeout=max(0.0, BUDGET - (time.monotonic() - START)))
    _finish()


if __name__ == "__main__":
    main()
