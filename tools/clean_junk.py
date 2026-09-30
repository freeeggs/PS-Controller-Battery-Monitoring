#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""清理项目里**可再生产**的中间产物。

默认是 **dry-run：只打印清单，不删任何东西**。确认后再加 ``--apply``。

用法::

    python tools\\clean_junk.py                    # 只看清单
    python tools\\clean_junk.py --apply            # 移到回收站（可恢复）
    python tools\\clean_junk.py --apply --permanent  # 永久删除（立即释放空间）
    python tools\\clean_junk.py --apply --scratch    # 连 _scratch 一起清

**绝不动**：src/ tests/ tools/ docs/ assets/ artifacts/ Release/ README.md
requirements.txt run.py，以及 build/ 下的构建输入（spec / version_info / build.ps1）。
"""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import sys
from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# build/ 里必须保留的构建输入
KEEP_IN_BUILD = {"psbattery.spec", "version_info.txt", "build.ps1", "build.bat"}

# 这些前缀的目录是可再生的打包中间产物
JUNK_DIR_PREFIXES = ("work", "dist_", "dist")


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%.1f %s" % (size, unit)
        size /= 1024.0


def dir_size(path: str) -> int:
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total


def collect(include_scratch: bool):
    """返回 (待删列表, 保留说明)。待删项为 (路径, 字节数, 说明)。"""
    targets = []

    build = os.path.join(ROOT, "build")
    if os.path.isdir(build):
        for name in sorted(os.listdir(build)):
            full = os.path.join(build, name)
            if not os.path.isdir(full):
                continue
            if name in KEEP_IN_BUILD:
                continue
            if name.startswith(JUNK_DIR_PREFIXES):
                targets.append((full, dir_size(full),
                                "PyInstaller 打包中间产物（重新打包即生成）"))

    dist = os.path.join(ROOT, "dist")
    if os.path.isdir(dist):
        targets.append((dist, dir_size(dist),
                        "与 Release/ 里同一份 exe 重复"))

    if include_scratch:
        scratch = os.path.join(ROOT, "_scratch")
        if os.path.isdir(scratch):
            targets.append((scratch, dir_size(scratch),
                            "开发期临时脚本与盘点报告"))
    return targets


# ---------------------------------------------------------------------------
# 移到回收站：直接调 shell32.SHFileOperationW（不走 COM）
# ---------------------------------------------------------------------------
class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040          # ← 移到回收站（可恢复）
FOF_NOERRORUI = 0x0400


def send_to_recycle_bin(paths):
    """把一批路径送进回收站。返回 (是否全部成功, 说明)。"""
    if not paths:
        return True, "无待处理项"
    # pFrom 需要以 \0\0 结尾的双重 NUL 列表
    joined = "\0".join(os.path.abspath(p) for p in paths) + "\0\0"
    op = _SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = joined
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.SHFileOperationW.argtypes = (ctypes.POINTER(_SHFILEOPSTRUCTW),)
    shell32.SHFileOperationW.restype = ctypes.c_int
    rc = shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0:
        return False, "SHFileOperationW 返回错误码 0x%X" % (rc & 0xFFFFFFFF)
    if op.fAnyOperationsAborted:
        return False, "操作被中途放弃（可能超过回收站容量限制）"
    return True, "已送入回收站"


def main() -> int:
    ap = argparse.ArgumentParser(description="清理项目里可再生产的中间产物")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认只打印清单）")
    ap.add_argument("--permanent", action="store_true",
                    help="永久删除而不是移到回收站（不可恢复）")
    ap.add_argument("--scratch", action="store_true",
                    help="连 _scratch/ 一起清理")
    ap.add_argument("--out", metavar="PATH",
                    help="同时把清单以 UTF-8 写入该文件（控制台编码不可靠时用）")
    args = ap.parse_args()

    lines = []
    say = lines.append
    stream = sys.stdout

    def flush_out():
        text = "\n".join(lines)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        try:
            stream.write(text + "\n")
            stream.flush()
        except Exception:
            pass

    targets = collect(args.scratch)

    say("=" * 74)
    say("项目中间产物清理%s" % ("（永久删除）" if args.permanent else "（移到回收站）")
        if args.apply else "项目中间产物清单（dry-run，不会删除任何东西）")
    say("=" * 74)
    if not targets:
        say("没有需要清理的项。")
        flush_out()
        return 0

    total = 0
    for path, size, why in targets:
        total += size
        try:
            shown = os.path.relpath(path, ROOT)
        except ValueError:
            shown = path
        say("  %-28s %10s   %s" % (shown, human(size), why))
    say("-" * 74)
    say("  共 %d 项，合计 %s" % (len(targets), human(total)))
    say("")
    say("保留：src/ tests/ tools/ docs/ assets/ artifacts/ Release/ README.md")
    say("      requirements.txt run.py  build/{psbattery.spec,version_info.txt,build.ps1}")

    if not args.apply:
        say("")
        say("以上仅为清单。确认后执行：")
        say("  python tools\\clean_junk.py --apply            # 移到回收站")
        say("  python tools\\clean_junk.py --apply --permanent  # 永久删除")
        flush_out()
        return 0

    say("")
    if args.permanent:
        say("**警告：永久删除不可恢复。**")
        if os.environ.get("PSBT_CONFIRM_PERMANENT") != "yes":
            say("已中止：请设置环境变量 PSBT_CONFIRM_PERMANENT=yes 再执行。")
            flush_out()
            return 2
        failed = []
        for path, _size, _why in targets:
            try:
                shutil.rmtree(path)
            except Exception as exc:
                failed.append((path, exc))
        ok = not failed
        msg = "已永久删除" if ok else "部分失败"
    else:
        # 分批处理（每批最多 BATCH 项），每批后立即复核，避免"一次性提交一大批"
        # 出问题后无从判断到底哪一项失败。
        BATCH = 10
        ok = True
        msgs = []
        for start in range(0, len(targets), BATCH):
            chunk = targets[start:start + BATCH]
            batch_ok, batch_msg = send_to_recycle_bin([p for p, _s, _w in chunk])
            got = [p for p, _s, _w in chunk if not os.path.exists(p)]
            gone = len(got) == len(chunk)
            msgs.append("第 %d 批（%d 项）：%s，复核 %d/%d 已移走"
                        % (start // BATCH + 1, len(chunk), batch_msg, len(got),
                           len(chunk)))
            ok = ok and batch_ok and gone
            if not gone:
                break
        msg = "；\n".join(msgs)

    for path, _size, _why in targets:
        try:
            mark = "OK " if not os.path.exists(path) else "残留"
        except OSError:
            mark = "?  "
        say("  [%s] %s" % (mark, os.path.relpath(path, ROOT)))
    say("")
    say(msg)
    flush_out()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
