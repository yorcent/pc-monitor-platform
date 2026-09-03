#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PC 性能监测与优化平台 - 本地服务端 v2
======================================
真实数据采集：psutil + nvidia-smi + WMI + 注册表
真实操作：内存清理(明细)、网络诊断、下载测速、性能诊断、磁盘分析与清理、
          进程结束、启动项管理、已装软件列表、系统快捷入口、网络修复、停止服务
安全约定：所有删除/修复/系统修改均由前端显式按钮触发，本服务不做任何自动危险动作。
"""
import os
import re
import sys
import json
import time
import socket
import threading
import subprocess
import ctypes
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

try:
    import psutil
except ImportError:
    psutil = None
try:
    import winreg
except ImportError:
    winreg = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HOST = "127.0.0.1"
PORT = 8765
_httpd = None  # 供 shutdown 使用
MY_PID = os.getpid()
_tray_icon = None  # 托盘图标引用（防止被 GC）


def _port_in_use(host, port):
    """探测端口是否已被占用（Windows 下 allow_reuse_address 允许重复 bind，
    必须显式探测，否则"再次双击"会重复启动一个监听同端口的新实例）"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            return s.connect_ex((host, port)) == 0
    except Exception:
        return False

# ---------------- 全局状态 ----------------
_now = time.time()
_io_lock = threading.Lock()
_prev_net = psutil.net_io_counters() if psutil else None
_prev_disk = psutil.disk_io_counters() if psutil else None
_prev_time = _now
_cpu_lock = threading.Lock()
_cpu_state = {"per": [], "total": 0.0, "freq": None}
_disk_scan = {"running": False, "path": "", "depth": 2, "progress": "", "folders": [], "files": [], "done": False, "scanned": 0}
_sysinfo_cache = None
_gateway_cache = {"ts": 0, "value": None}
_slow_lock = threading.Lock()
_slow_state = {"gpu": [], "gpu_apps": [], "gateway": None}
_proc_lock = threading.Lock()
_proc_state = {"rows": [], "total": 0, "ts": 0}
_drv_lock = threading.Lock()
_drv_state = {"ts": 0, "items": [], "total": 0, "ok": 0, "error": 0, "unknown": 0, "degraded": 0}
_net_ping = {"ts": 0, "ms": None, "ok": False, "note": ""}


def _fmt(n):
    return f"{n:,.0f}"


def _run(cmd, timeout=10):
    """运行子进程并自适应解码输出（中文系统 cmd 输出为 GBK）"""
    # CREATE_NO_WINDOW：exe(--noconsole) 环境无控制台，必须禁止子进程新建终端窗口，
    # 否则 powershell/cmd/nvidia-smi 等控制台子进程会反复弹窗。
    _nowin = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=_nowin)
    except subprocess.TimeoutExpired:
        return "", 1
    except Exception:
        return "", 1
    text = ""
    for enc in ("gbk", "utf-8"):
        try:
            text = p.stdout.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        text = p.stdout.decode("gbk", errors="replace")
    return text, p.returncode


def _cpu_sampler():
    """独立后台线程：每 1 秒用 interval 采样，线程安全、数值可靠"""
    while True:
        try:
            per = psutil.cpu_percent(interval=1.0, percpu=True)
            freq = psutil.cpu_freq()
            with _cpu_lock:
                _cpu_state["per"] = [round(x, 1) for x in per]
                _cpu_state["total"] = round(sum(per) / len(per), 1) if per else 0.0
                _cpu_state["freq"] = round(freq.current, 0) if freq and freq.current else None
        except Exception:
            pass


threading.Thread(target=_cpu_sampler, daemon=True).start()


# ---------------- 数据采集 ----------------

def get_cpu():
    with _cpu_lock:
        return {
            "percent": _cpu_state["total"],
            "per_core": list(_cpu_state["per"]),
            "logical": psutil.cpu_count(logical=True) if psutil else None,
            "physical": psutil.cpu_count(logical=False) if psutil else None,
            "freq_mhz": _cpu_state["freq"],
            "sampled_sec": 1,
        }


def get_memory():
    v = psutil.virtual_memory()
    s = psutil.swap_memory()
    return {
        "total": v.total, "used": v.used, "available": v.available,
        "percent": round(v.used / v.total * 100, 1) if v.total else 0.0,
        "percent_sys": round(v.percent, 1),
        "swap_total": s.total, "swap_used": s.used, "swap_percent": s.percent,
    }


def get_disk():
    parts = []
    for p in psutil.disk_partitions(all=False):
        try:
            u = psutil.disk_usage(p.mountpoint)
            parts.append({"device": p.device, "mount": p.mountpoint, "fstype": p.fstype,
                          "total": u.total, "used": u.used, "free": u.free, "percent": u.percent})
        except Exception:
            pass
    return parts


def get_io_delta():
    global _prev_net, _prev_disk, _prev_time
    with _io_lock:
        now = time.time()
        dt = max(now - _prev_time, 0.1)
        n = psutil.net_io_counters()
        d = psutil.disk_io_counters()
        ns = nr = dr = dw = 0.0
        if _prev_net:
            ns = max(0.0, (n.bytes_sent - _prev_net.bytes_sent)) / dt
            nr = max(0.0, (n.bytes_recv - _prev_net.bytes_recv)) / dt
        if _prev_disk:
            dr = max(0.0, (d.read_bytes - _prev_disk.read_bytes)) / dt
            dw = max(0.0, (d.write_bytes - _prev_disk.write_bytes)) / dt
        _prev_net, _prev_disk, _prev_time = n, d, now
        return {"net_up": ns, "net_down": nr, "disk_read": dr, "disk_write": dw,
                "net_total_sent": n.bytes_sent, "net_total_recv": n.bytes_recv}


GPU_QUERY = ("name,utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu,"
             "power.draw,power.limit,fan.speed,clocks.sm,clocks.mem,utilization.encoder,driver_version")


def get_gpu():
    gpus = []
    try:
        out, _ = _run(["nvidia-smi", "--query-gpu=" + GPU_QUERY, "--format=csv,noheader,nounits"], timeout=5)
        for line in out.strip().splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) >= 13:
                def f(i):
                    v = p[i]
                    return float(v) if v not in ("", "[N/A]", "N/A") else None
                gpus.append({
                    "vendor": "NVIDIA", "name": p[0],
                    "util": f(1), "mem_util": f(2),
                    "mem_used": f(3), "mem_total": f(4),
                    "temp": f(5), "power": f(6), "power_limit": f(7),
                    "fan": f(8), "clock_graphics": f(9), "clock_mem": f(10),
                    "encoder": f(11), "driver": p[12],
                })
    except Exception:
        pass
    try:
        script = ("Get-CimInstance Win32_VideoController | Where-Object { $_.Name -match 'Intel|AMD|Radeon' } | "
                  "ForEach-Object { [PSCustomObject]@{ Name=$_.Name; VRAM=$_.AdapterRAM; Driver=$_.DriverVersion; Status=$_.Status } } | ConvertTo-Json -Compress")
        out, _ = _run(["powershell", "-NoProfile", "-Command", script], timeout=10)
        txt = out.strip()
        if txt and txt != "null":
            data = json.loads(txt)
            if isinstance(data, dict):
                data = [data]
            for it in data:
                if not any(g["name"] == it.get("Name") for g in gpus):
                    try:
                        vram_gb = round(int(it.get("VRAM") or 0) / 1024 ** 3, 1) or None
                    except Exception:
                        vram_gb = None
                    gpus.append({"vendor": "Intel" if "Intel" in str(it.get("Name")) else "AMD",
                                 "name": it.get("Name"), "util": None, "mem_util": None,
                                 "mem_used": None, "mem_total": vram_gb, "temp": None, "power": None,
                                 "power_limit": None, "fan": None, "clock_graphics": None, "clock_mem": None,
                                 "encoder": None, "driver": it.get("Driver")})
    except Exception:
        pass
    return gpus


def get_gpu_apps():
    apps = []
    try:
        out, _ = _run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader"], timeout=5)
        for line in out.strip().splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) >= 2:
                try:
                    used = float(p[2]) if p[2] not in ("", "[N/A]", "N/A") else None
                except Exception:
                    used = None
                apps.append({"pid": p[0], "name": os.path.basename(p[1]) if p[1] else "?", "mem": used})
    except Exception:
        pass
    return apps


def get_battery():
    try:
        b = psutil.sensors_battery()
        if b is None:
            return None
        return {"percent": b.percent, "plugged": b.power_plugged,
                "seconds_left": b.secsleft if isinstance(b.secsleft, int) else None}
    except Exception:
        return None


def get_network_interfaces():
    ifs = []
    for name, addrs in (psutil.net_if_addrs().items() if psutil else []):
        for a in addrs:
            if a.family == socket.AF_INET:
                ifs.append({"name": name, "ip": a.address, "netmask": a.netmask})
    return ifs


def get_gateway(force=False):
    global _gateway_cache
    now = time.time()
    if not force and _gateway_cache["value"] and now - _gateway_cache["ts"] < 30:
        return _gateway_cache["value"]
    val = None
    out, _ = _run(["powershell", "-NoProfile", "-Command",
                   "(Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Sort-Object RouteMetric | Select-Object -First 1).NextHop"], timeout=10)
    out = (out or "").strip()
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", out):
        val = out
    if not val:
        out2, _ = _run(["powershell", "-NoProfile", "-Command",
                        "(Get-NetRoute -DestinationPrefix '::/0' | Sort-Object RouteMetric | Select-Object -First 1).NextHop"], timeout=10)
        out2 = (out2 or "").strip()
        if out2:
            val = out2
    if val:
        _gateway_cache = {"ts": now, "value": val}
    return val


# ---------------- 后台慢采样线程（GPU / 网关，避免每次轮询拉起子进程） ----------------

def _slow_sampler():
    """独立后台线程：每 3 秒低频刷新 GPU / GPU 进程 / 网关，get_metrics 直接读缓存"""
    while True:
        try:
            gpu = get_gpu()
            apps = get_gpu_apps()
            gw = get_gateway()  # 内部有 30s 缓存，实际每 30s 才跑一次 PowerShell
            with _slow_lock:
                _slow_state["gpu"] = gpu
                _slow_state["gpu_apps"] = apps
                _slow_state["gateway"] = gw
        except Exception:
            pass
        time.sleep(3)


threading.Thread(target=_slow_sampler, daemon=True).start()


def _get_slow():
    """读取慢采样缓存；首次调用（缓存未就绪）时同步补齐一次，保证首屏有数据"""
    with _slow_lock:
        if _slow_state["gpu"]:
            return list(_slow_state["gpu"]), list(_slow_state["gpu_apps"]), _slow_state["gateway"]
    # 缓存尚未就绪：首屏同步采集一次
    gpu = get_gpu()
    apps = get_gpu_apps()
    gw = get_gateway()
    with _slow_lock:
        if not _slow_state["gpu"]:
            _slow_state["gpu"] = gpu
            _slow_state["gpu_apps"] = apps
            _slow_state["gateway"] = gw
    return gpu, apps, gw


def get_sysinfo(force=False):
    global _sysinfo_cache
    if _sysinfo_cache and not force:
        return _sysinfo_cache
    info = {"hostname": socket.gethostname(), "os_caption": "Windows", "os_version": "", "os_build": "",
            "cpu_model": "Unknown CPU", "cpu_logical": 0, "cpu_physical": 0, "ram_total": 0,
            "boot_time": "", "uptime_sec": 0, "disks": [], "motherboard": "未知", "bios": "", "battery": None}
    try:
        info["cpu_model"] = _run(["powershell", "-NoProfile", "-Command",
                                  "(Get-CimInstance Win32_Processor).Name"], timeout=10)[0].strip() or "Unknown"
    except Exception:
        pass
    try:
        info["cpu_logical"] = psutil.cpu_count(logical=True) or 0
        info["cpu_physical"] = psutil.cpu_count(logical=False) or 0
        info["ram_total"] = psutil.virtual_memory().total
        bt = psutil.boot_time()
        info["boot_time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(bt))
        info["uptime_sec"] = time.time() - bt
    except Exception:
        pass
    try:
        info["os_caption"] = _run(["powershell", "-NoProfile", "-Command",
                                   "(Get-CimInstance Win32_OperatingSystem).Caption"], timeout=10)[0].strip()
    except Exception:
        pass
    try:
        info["os_build"] = _run(["powershell", "-NoProfile", "-Command",
                                 "(Get-CimInstance Win32_OperatingSystem).BuildNumber"], timeout=10)[0].strip()
    except Exception:
        pass
    try:
        info["motherboard"] = _run(["powershell", "-NoProfile", "-Command",
                                    "$b=Get-CimInstance Win32_BaseBoard; \"$($b.Manufacturer) $($b.Product)\""], timeout=10)[0].strip()
    except Exception:
        pass
    try:
        info["bios"] = _run(["powershell", "-NoProfile", "-Command",
                             "$b=Get-CimInstance Win32_BIOS; \"$($b.Manufacturer) $($b.SMBIOSBIOSVersion)\""], timeout=10)[0].strip()
    except Exception:
        pass
    try:
        out, _ = _run(["powershell", "-NoProfile", "-Command",
                       "Get-PhysicalDisk | ForEach-Object { [PSCustomObject]@{Name=$_.FriendlyName; Size=$_.Size; Media=$_.MediaType; Health=$_.HealthStatus} } | ConvertTo-Json -Compress"], timeout=10)
        d = json.loads(out.strip()) if out.strip() and out.strip() != "null" else []
        info["disks"] = d if isinstance(d, list) else [d]
    except Exception:
        info["disks"] = []
    info["battery"] = get_battery()
    info["gpus"] = get_gpu()
    _sysinfo_cache = info
    return info


def get_processes(top=0):
    """返回后台进程采样线程缓存的实时进程列表（按 CPU 降序）；top=0 返回全部"""
    with _proc_lock:
        rows = list(_proc_state["rows"])
    return rows if not top else rows[:top]


def _ps_json():
    """一次 Get-Process 批量取全部进程的 Id/名称/累计CPU/工作集/路径（~0.4s）"""
    out, _ = _run(["powershell", "-NoProfile", "-Command",
                   "Get-Process | Select-Object Id,ProcessName,CPU,WorkingSet64,Path | ConvertTo-Json -Compress"], timeout=10)
    txt = (out or "").strip()
    if not txt or txt == "null":
        return []
    try:
        data = json.loads(txt)
        return data if isinstance(data, list) else [data]
    except Exception:
        return []


def _tasklist_users():
    """tasklist /v 批量取进程用户名（按列位置解析，兼容中英文 locale）"""
    users = {}
    try:
        import csv as _csv
        import io as _io
        out, _ = _run(["tasklist", "/v", "/fo", "csv"], timeout=10)
        for row in _csv.reader(_io.StringIO(out or "")):
            if len(row) >= 8 and row[1].strip().isdigit():
                uname = row[6].strip()
                if uname and uname.lower() not in ("n/a", "暂无"):
                    users[int(row[1])] = uname
    except Exception:
        pass
    return users


def _enrich_users(rows, top=60):
    """对排序后可见的 Top N 进程用 psutil 补全用户名（tasklist /v 在本机易超时，不采用）"""
    seen = 0
    for r in rows:
        if seen >= top:
            break
        if r.get("user") and r["user"] != "—":
            continue
        seen += 1
        try:
            u = psutil.Process(r["pid"]).username()
            if u:
                r["user"] = u.split("\\")[-1]
        except Exception:
            r["user"] = "系统"


def _tasklist_users():
    """tasklist /v 批量取进程用户名（按列位置解析，兼容中英文 locale）。
    注意：本机实测 /v 会因枚举窗口标题而超时，已改用 _enrich_users，本函数保留备用。"""
    users = {}
    try:
        import csv as _csv
        import io as _io
        out, _ = _run(["tasklist", "/v", "/fo", "csv"], timeout=4)
        for row in _csv.reader(_io.StringIO(out or "")):
            if len(row) >= 8 and row[1].strip().isdigit():
                uname = row[6].strip()
                if uname and uname.lower() not in ("n/a", "暂无"):
                    users[int(row[1])] = uname
    except Exception:
        pass
    return users


def _process_sampler():
    """独立后台线程：每 3 秒刷新全量进程表（CPU 用累计秒数差分），接口直接读缓存"""
    prev = {}
    ncpu = psutil.cpu_count(logical=True) or 1
    while True:
        try:
            items = _ps_json()
            now = time.time()
            if not items:
                # PowerShell 批量采样偶发失败：保留上一次结果，避免进程表被清空
                time.sleep(3)
                continue
            rows = []
            for it in items:
                try:
                    pid = int(it.get("Id") or 0)
                    name = (it.get("ProcessName") or "?").strip() or "?"
                    cpu_secs = float(it.get("CPU") or 0)
                    mem = int(it.get("WorkingSet64") or 0)
                    path = (it.get("Path") or "").strip()
                    if pid in prev:
                        psec, ptime = prev[pid]
                        dt = max(now - ptime, 0.5)
                        cpu = max(0.0, (cpu_secs - psec) / dt / ncpu * 100.0)
                    else:
                        cpu = 0.0
                    prev[pid] = (cpu_secs, now)
                    rows.append({"pid": pid, "name": name, "cpu": round(min(cpu, 100.0), 1),
                                 "mem": mem, "user": "—", "path": path})
                except Exception:
                    continue
            rows.sort(key=lambda x: -x["cpu"])
            _enrich_users(rows)
            with _proc_lock:
                _proc_state["rows"] = rows
                _proc_state["total"] = len(rows)
                _proc_state["ts"] = now
            alive = {r["pid"] for r in rows}
            prev = {k: v for k, v in prev.items() if k in alive}
        except Exception:
            pass
        time.sleep(3)


threading.Thread(target=_process_sampler, daemon=True).start()


# ---------------- 驱动扫描（后台线程缓存） ----------------
def _pnp_drivers():
    """Get-PnpDevice 批量取所有即插即用设备（含驱动状态与错误码）"""
    out, _ = _run(["powershell", "-NoProfile", "-Command",
                   "Get-PnpDevice -PresentOnly | Select-Object FriendlyName,Class,Status,InstanceId,ConfigManagerErrorCode | ConvertTo-Json -Compress"],
                  timeout=25)
    txt = (out or "").strip()
    if not txt or txt == "null":
        return []
    try:
        data = json.loads(txt)
    except Exception:
        return []
    data = data if isinstance(data, list) else [data]
    rows = []
    for it in data:
        try:
            st = (it.get("Status") or "Unknown").strip() or "Unknown"
            err = int(it.get("ConfigManagerErrorCode") or 0)
            name = (it.get("FriendlyName") or "").strip() or "(未命名设备)"
            cls = (it.get("Class") or "").strip() or "其他"
            inst = (it.get("InstanceId") or "").strip()
            # 若状态非 Error 但错误码非 0，同样视为异常
            abnormal = (st.lower() == "error") or (err != 0)
            status = "Error" if abnormal else ("Unknown" if st.lower() == "unknown" else ("Degraded" if st.lower() == "degraded" else "OK"))
            rows.append({"name": name, "class": cls, "status": status, "error": err,
                         "instance": inst[-48:] if len(inst) > 48 else inst})
        except Exception:
            continue
    return rows


def _drv_sampler():
    """独立后台线程：每 60 秒刷新一次驱动列表（Get-PnpDevice 约 2-5s）"""
    while True:
        try:
            items = _pnp_drivers()
            if items:
                with _drv_lock:
                    _drv_state["items"] = items
                    _drv_state["total"] = len(items)
                    _drv_state["ok"] = sum(1 for x in items if x["status"] == "OK")
                    _drv_state["error"] = sum(1 for x in items if x["status"] == "Error")
                    _drv_state["unknown"] = sum(1 for x in items if x["status"] == "Unknown")
                    _drv_state["degraded"] = sum(1 for x in items if x["status"] == "Degraded")
                    _drv_state["ts"] = time.time()
        except Exception:
            pass
        time.sleep(60)


threading.Thread(target=_drv_sampler, daemon=True).start()


def get_drivers():
    with _drv_lock:
        return {"ts": _drv_state["ts"], "total": _drv_state["total"], "ok": _drv_state["ok"],
                "error": _drv_state["error"], "unknown": _drv_state["unknown"], "degraded": _drv_state["degraded"],
                "items": list(_drv_state["items"])}


def _ping_gateway():
    """快速 ping 网关（带 60s 缓存），返回 (ok, ms)"""
    if time.time() - _net_ping["ts"] < 60 and _net_ping["ts"] > 0:
        return _net_ping["ok"], _net_ping["ms"], _net_ping["note"]
    gw = _slow_state.get("gateway")
    if not gw:
        _net_ping.update({"ts": time.time(), "ok": False, "ms": None, "note": "未检测到默认网关"})
        return False, None, "未检测到默认网关"
    out, rc = _run(["ping", "-n", "1", "-w", "1200", gw], timeout=3)
    ok, ms = False, None
    if rc == 0 and out:
        low = out.lower()
        import re as _re
        m = _re.search(r"[时间时time]+[=<]\s*([0-9]+)", low)
        if m:
            ms = int(m.group(1))
        ok = "ttl=" in low or "ttl =" in low or "字节=32" in out or "time=" in low or "时间" in out
    note = ("正常，延迟 %sms" % ms) if ok and ms is not None else ("正常" if ok else "网关无响应")
    _net_ping.update({"ts": time.time(), "ok": ok, "ms": ms, "note": note})
    return ok, ms, note


def get_health():
    """综合性能评级：逐项评分 + 综合评分 + 建议事项 + 驱动摘要"""
    cpu = get_cpu()
    mem = get_memory()
    parts = get_disk()
    gpu = _slow_state.get("gpu") or []
    nv = next((g for g in gpu if g.get("vendor") == "NVIDIA" and g.get("util") is not None), None)
    g = nv or (gpu[0] if gpu else {})
    cpu_pct = cpu.get("percent") or 0
    mem_pct = mem.get("percent") or 0
    sys_part = next((p for p in parts if p["mount"].lower() == "c:\\"), None) or (parts[0] if parts else {})
    max_disk_pct = max([p["percent"] for p in parts], default=0)
    gpu_util = g.get("util")
    gpu_temp = g.get("temp")
    net_ok, net_ms, net_note = _ping_gateway()
    with _drv_lock:
        drv_total, drv_err = _drv_state["total"], _drv_state["error"]

    items = []
    # CPU
    s = max(0, min(100, round(100 - cpu_pct * 0.85)))
    lvl = "ok" if s >= 80 else ("warn" if s >= 60 else "bad")
    items.append({"key": "cpu", "name": "CPU 负载", "score": s, "level": lvl,
                  "status": "正常" if lvl == "ok" else ("偏高" if lvl == "warn" else "过高"),
                  "desc": "占用 %s%%（%s 核）" % (round(cpu_pct, 1), cpu.get("logical") or "?")})
    # 内存
    s = max(0, min(100, round(100 - mem_pct)))
    lvl = "ok" if s >= 80 else ("warn" if s >= 60 else "bad")
    items.append({"key": "mem", "name": "内存占用", "score": s, "level": lvl,
                  "status": "充裕" if lvl == "ok" else ("偏紧" if lvl == "warn" else "紧张"),
                  "desc": "使用率 %s%% · 可用 %s" % (round(mem_pct, 1), human_bytes(mem.get("available") or 0))})
    # 磁盘（取占用最高的分区）
    s = max(0, min(100, round(100 - max_disk_pct)))
    lvl = "ok" if s >= 80 else ("warn" if s >= 60 else "bad")
    sys_pct = sys_part.get("percent") or max_disk_pct
    disk_desc = "最满分区 %s%%" % round(max_disk_pct, 1)
    if max_disk_pct > sys_pct + 1:
        disk_desc += "（系统盘 %s%%）" % round(sys_pct, 1)
    items.append({"key": "disk", "name": "磁盘容量", "score": s, "level": lvl,
                  "status": "充足" if lvl == "ok" else ("偏满" if lvl == "warn" else "紧张"),
                  "desc": disk_desc})
    # GPU
    if gpu_util is not None:
        s = max(0, min(100, round(100 - (gpu_util or 0) * 0.7 - max(0, (gpu_temp or 0) - 70) * 1.5)))
        lvl = "ok" if s >= 80 else ("warn" if s >= 60 else "bad")
        items.append({"key": "gpu", "name": "显卡状态", "score": s, "level": lvl,
                      "status": "正常" if lvl == "ok" else ("偏热/高载" if lvl == "warn" else "高负载"),
                      "desc": "占用 %s%% · %s℃" % (round(gpu_util, 0), round(gpu_temp or 0, 0))})
    # 网络
    if net_ok:
        s = 95 if (net_ms is None or net_ms < 60) else (75 if net_ms < 150 else 50)
        items.append({"key": "net", "name": "网络连接", "score": s, "level": "ok" if s >= 80 else "warn",
                      "status": "正常", "desc": net_note})
    else:
        items.append({"key": "net", "name": "网络连接", "score": 45, "level": "bad",
                      "status": "异常/未检测", "desc": net_note})
    # 驱动
    s = 95 if drv_err == 0 else (75 if drv_err <= 2 else 45)
    items.append({"key": "drv", "name": "驱动状态", "score": s, "level": "ok" if s >= 80 else ("warn" if s >= 60 else "bad"),
                  "status": "全部正常" if drv_err == 0 else ("%s 个异常" % drv_err),
                  "desc": "共 %s 个驱动" % (drv_total or "—")})

    score = round(sum(i["score"] for i in items) / len(items)) if items else 60
    grade = "优秀" if score >= 85 else ("良好" if score >= 70 else ("一般" if score >= 55 else "较差"))

    tips = []
    if mem_pct >= 85:
        tips.append({"level": "bad", "text": "内存使用率偏高（%s%%），建议运行「内存清理」释放空间。" % round(mem_pct, 1), "action": "mem"})
    elif mem_pct >= 70:
        tips.append({"level": "warn", "text": "内存占用 %s%%，较紧张时可运行「内存清理」。" % round(mem_pct, 1), "action": "mem"})
    if max_disk_pct >= 85:
        tips.append({"level": "bad", "text": "磁盘占用 %s%%，建议到「磁盘 → 建议清理项 / 专项清理」清理空间。" % round(max_disk_pct, 1), "action": "disk"})
    elif max_disk_pct >= 70:
        tips.append({"level": "warn", "text": "磁盘使用率 %s%%，可查看「建议清理项」释放空间。" % round(max_disk_pct, 1), "action": "disk"})
    if cpu_pct >= 85:
        tips.append({"level": "bad", "text": "CPU 占用 %s%%，建议到「进程」查看并结束高占用进程。" % round(cpu_pct, 1), "action": "proc"})
    if gpu_temp and gpu_temp >= 80:
        tips.append({"level": "warn", "text": "显卡温度 %s℃，注意散热，可降低负载。" % round(gpu_temp, 0), "action": "gpu"})
    if not net_ok and "未检测" not in net_note:
        tips.append({"level": "bad", "text": "网关 %s 无响应，建议运行「网络诊断」。" % (_slow_state.get("gateway") or ""), "action": "net"})
    if net_ms and net_ms >= 150:
        tips.append({"level": "warn", "text": "网络延迟 %sms，可到「网络」页运行诊断与一键修复。" % net_ms, "action": "net"})
    if drv_err > 0:
        tips.append({"level": "warn", "text": "有 %s 个驱动异常，可到「驱动」页查看或打开设备管理器处理。" % drv_err, "action": "drivers"})
    if not tips:
        tips.append({"level": "ok", "text": "各项指标正常，无需处理。可定期查看「磁盘」清理项保持系统整洁。", "action": "disk"})

    return {"ts": time.time(), "score": score, "grade": grade, "items": items, "tips": tips,
            "drivers": {"total": drv_total, "ok": _drv_state["ok"], "error": drv_err,
                        "unknown": _drv_state["unknown"], "degraded": _drv_state["degraded"]},
            "net": {"ok": net_ok, "ms": net_ms, "note": net_note},
            "battery": get_battery(),
            "cpu_pct": round(cpu_pct, 1), "mem_pct": round(mem_pct, 1),
            "disk_pct": round(max_disk_pct, 1)}


def get_metrics():
    io = get_io_delta()
    gpu, gpu_apps, gateway = _get_slow()
    return {"ts": time.time(), "cpu": get_cpu(), "mem": get_memory(), "disk": get_disk(), "io": io,
            "gpu": gpu, "gpu_apps": gpu_apps, "battery": get_battery(),
            "net_if": get_network_interfaces(), "uptime_sec": time.time() - psutil.boot_time() if psutil else 0,
            "proc_count": len(psutil.pids()) if psutil else 0, "gateway": gateway}


# ---------------- 一键智能体检 ----------------
_checkup_hist = []
_checkup_lock = threading.Lock()


def run_checkup():
    """一键完整诊断：健康评级 + 启动项影响 + 历史记录（纯只读，不执行任何操作）"""
    h = get_health()
    startup = _assess_startup_impact(list_startup())
    heavy = [s for s in startup if s.get("impact") == "high" and s.get("enabled")]
    report = {"ts": time.time(),
              "score": h.get("score"), "grade": h.get("grade"),
              "items": h.get("items"), "tips": h.get("tips"),
              "drivers": h.get("drivers"), "net": h.get("net"),
              "startup_total": len(startup),
              "startup_heavy_total": len(heavy),
              "startup_heavy": heavy[:25],
              "cpu_pct": h.get("cpu_pct"), "mem_pct": h.get("mem_pct"), "disk_pct": h.get("disk_pct")}
    with _checkup_lock:
        _checkup_hist.insert(0, report)
        if len(_checkup_hist) > 30:
            del _checkup_hist[30:]
    return report


def checkup_history():
    with _checkup_lock:
        return {"list": list(_checkup_hist)}


# ---------------- 磁盘占用地图（顶层目录可视化） ----------------
_td_cache = {}
_td_lock = threading.Lock()


def _dir_tree_size(root):
    """统计 root 的直接子项大小：目录用递归统计，文件直接取大小；目录并行统计加速"""
    entries = []
    try:
        with os.scandir(root) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        entries.append(("d", e.name, e.path))
                    else:
                        try:
                            sz = e.stat().st_size
                        except Exception:
                            sz = 0
                        entries.append(("f", e.name, e.path, sz))
                except Exception:
                    pass
    except Exception:
        return 0, []
    # 并行统计所有子目录大小
    sizes = {}
    dirs = [en for en in entries if en[0] == "d"]
    if dirs:
        try:
            with ThreadPoolExecutor(max_workers=8) as ex:
                futures = {}
                for en in dirs:
                    futures[ex.submit(dir_size, en[2])] = en
                for fut, en in futures.items():
                    try:
                        sizes[en[2]] = fut.result()
                    except Exception:
                        sizes[en[2]] = 0
        except Exception:
            for en in dirs:
                sizes[en[2]] = dir_size(en[2])
    children = []
    total = 0
    for en in entries:
        if en[0] == "d":
            sz = sizes.get(en[2], 0)
            children.append({"name": en[1], "path": en[2], "is_dir": True, "size": sz})
        else:
            sz = en[3]
            children.append({"name": en[1], "path": en[2], "is_dir": False, "size": sz})
        total += sz
    children.sort(key=lambda x: -x["size"])
    return total, children


def disk_topdirs(path):
    """返回指定目录的直接子项大小（带 5 分钟缓存），供前端环形图展示与钻取"""
    path = path or "D:\\"
    if re.fullmatch(r"[A-Za-z]:", path):
        path += "\\"  # 裸盘符（如 D:）在 Windows 会指向工作目录，必须补全为盘根
    if path == "\\":
        path = os.path.splitdrive(os.getcwd())[0] + "\\"
    cache_key = path.rstrip("\\/") + "\\"
    with _td_lock:
        c = _td_cache.get(cache_key)
        if c and time.time() - c["ts"] < 300:
            return {"path": path, "total": c["total"], "items": c["items"], "cached": True}
    total, items = _dir_tree_size(path)
    with _td_lock:
        _td_cache[cache_key] = {"ts": time.time(), "total": total, "items": items}
    return {"path": path, "total": total, "items": items, "cached": False}


# ---------------- 真实操作 ----------------

# 操作日志中心：记录所有删除/清理/修复等显式操作，可追溯
_oplog = []
_oplog_lock = threading.Lock()
_OPLOG_ACTIONS = {
    "memory_clean": "内存清理", "kill": "结束进程", "network_fix": "网络修复",
    "delete": "删除到回收站", "cleanup": "清理", "wechat_clean": "微信专清",
    "startup": "启动项管理", "disk_scan": "磁盘分析", "open": "打开",
}


def _log_op(action, target="", size=None, note="", ok=True):
    try:
        with _oplog_lock:
            _oplog.append({"ts": time.time(), "action": action,
                           "action_name": _OPLOG_ACTIONS.get(action, action),
                           "target": target[:300], "size": size, "note": note[:200], "ok": ok})
            if len(_oplog) > 300:
                del _oplog[:-300]
    except Exception:
        pass


def get_oplog():
    with _oplog_lock:
        lst = list(reversed(_oplog))
    return {"list": lst}


def clear_oplog():
    with _oplog_lock:
        _oplog.clear()
    return {"ok": True}


def memory_clean():
    """内存清理：逐进程 EmptyWorkingSet，记录每个进程释放量（明细）"""
    if not psutil or os.name != "nt":
        return {"ok": False, "msg": "当前环境不支持该操作"}
    before = psutil.virtual_memory().available
    details = []
    procs = list(psutil.process_iter(["pid", "name"]))
    for p in procs:
        try:
            pid, name = p.info["pid"], p.info["name"]
            try:
                rss_before = psutil.Process(pid).memory_info().rss
            except Exception:
                rss_before = 0
            h = ctypes.windll.kernel32.OpenProcess(0x0400 | 0x0100, False, pid)
            ok = False
            if h:
                ok = bool(ctypes.windll.psapi.EmptyWorkingSet(h))
                ctypes.windll.kernel32.CloseHandle(h)
            if ok:
                try:
                    rss_after = psutil.Process(pid).memory_info().rss
                except Exception:
                    rss_after = 0
                freed = max(0, rss_before - rss_after)
                if freed > 1024 * 1024:  # 只记录释放超过 1MB 的进程
                    details.append({"pid": pid, "name": name, "freed": freed})
        except Exception:
            pass
    after = psutil.virtual_memory().available
    details.sort(key=lambda x: -x["freed"])
    _log_op("memory_clean", note="对 %d 个进程执行工作集释放，可用内存增加 %s" % (len(procs), human_bytes(max(0, after - before))),
            size=max(0, after - before))
    return {"ok": True, "processes": len(procs), "freed_bytes": max(0, after - before),
            "available_before": before, "available_after": after, "details": details[:40],
            "note": "已对 %d 个进程执行工作集释放，可用内存增加 %s" % (len(procs), human_bytes(max(0, after - before)))}


PROTECTED_PROC = {"System Idle Process", "System", "Registry", "Memory Compression"}
PROTECTED_EXE = {"csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe",
                 "smss.exe", "dwm.exe", "svchost.exe", "fontdrvhost.exe", "winlogon.exe"}


def kill_process(pid, tree=False):
    pid = int(pid)
    if pid <= 4 or pid == MY_PID:
        return {"ok": False, "msg": "该进程受保护，无法结束"}
    try:
        p = psutil.Process(pid)
        name = p.name() or ""
        if name in PROTECTED_PROC or name.lower() in PROTECTED_EXE:
            return {"ok": False, "msg": "“%s”为系统关键进程，禁止结束" % name}
        children = []
        if tree:
            try:
                children = [c for c in p.children(recursive=True)]
            except Exception:
                children = []
        for c in reversed(children):
            try:
                c.terminate()
            except Exception:
                pass
        p.terminate()
        try:
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        _log_op("kill", target=name, note="结束进程 PID %s%s" % (pid, "（含子进程）" if tree else ""))
        return {"ok": True, "name": name, "pid": pid, "tree": tree, "children": len(children)}
    except psutil.NoSuchProcess:
        return {"ok": False, "msg": "进程已不存在"}
    except psutil.AccessDenied:
        return {"ok": False, "msg": "权限不足，无法结束该进程（可能需管理员权限）"}
    except Exception as e:
        return {"ok": False, "msg": "结束失败：%s" % e}


def ping(target, count=4):
    try:
        out, _ = _run(["ping", "-n", str(count), target], timeout=25)
        text = out
        loss = None
        # 兼容中英文：中文 "丢失 = 0 (0% 丢失，)" / 英文 "Lost = 0 (0% loss)"
        m = re.search(r"丢失\s*=\s*(\d+)\s*\((\d+)%\s*丢失", text, re.I)
        if m:
            loss = int(m.group(2))
        else:
            m = re.search(r"Lost\s*=\s*(\d+)\s*\((\d+)%\s*loss", text, re.I)
            if m:
                loss = int(m.group(2))
            else:
                ms = re.search(r"已发送\s*=\s*(\d+).*?已接收\s*=\s*(\d+)", text, re.I)
                mr = re.search(r"Sent\s*=\s*(\d+).*?Received\s*=\s*(\d+)", text, re.I)
                mm = ms or mr
                if mm:
                    sent, recv = int(mm.group(1)), int(mm.group(2))
                    loss = round((sent - recv) / sent * 100) if sent else 0
        avg = None
        m = re.search(r"平均\s*=\s*(\d+)ms|Average\s*=\s*(\d+)ms|平均 = (\d+)ms", text, re.I)
        if m:
            avg = int(m.group(1) or m.group(2) or m.group(3) or 0)
        mins = re.search(r"最短\s*=\s*(\d+)ms|Minimum\s*=\s*(\d+)ms", text, re.I)
        maxs = re.search(r"最长\s*=\s*(\d+)ms|Maximum\s*=\s*(\d+)ms", text, re.I)
        return {"target": target,
                "avg_ms": avg,
                "min_ms": int(mins.group(1) or mins.group(2) or 0) if mins else None,
                "max_ms": int(maxs.group(1) or maxs.group(2) or 0) if maxs else None,
                "loss": loss, "raw": text.strip()[-200:]}
    except Exception as e:
        return {"target": target, "error": str(e)}


def network_diag():
    gateway = get_gateway()
    targets = []
    if gateway:
        targets.append(("本机网关(IPv6)" if ":" in gateway else "本机网关", gateway))
    targets += [("阿里 DNS", "223.5.5.5"), ("腾讯 DNS", "119.29.29.29"), ("谷歌 DNS", "8.8.8.8"), ("百度首页", "www.baidu.com")]
    results = []
    for name, t in targets:
        r = ping(t)
        r["name"] = name
        results.append(r)
    return {"gateway": gateway, "results": results, "ts": time.strftime("%H:%M:%S")}


def _download_speed_test():
    # 测速源优先国内可达镜像（中科大/清华对脚本 UA 返回 403，已实测排除）
    urls = [("华为云镜像", "https://mirrors.huaweicloud.com/ubuntu/ls-lR.gz"),
            ("阿里云镜像", "https://mirrors.aliyun.com/ubuntu/ls-lR.gz"),
            ("Cloudflare", "https://speed.cloudflare.com/__down?bytes=20971520")]
    last_err = "未知错误"
    for label, url in urls:
        try:
            start = time.time()
            total = 0
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=45) as r:
                while True:
                    chunk = r.read(131072)
                    if not chunk:
                        break
                    total += len(chunk)
            dur = time.time() - start
            if total == 0:
                continue
            mbps = total * 8 / 1e6 / dur
            return {"ok": True, "source": label, "bytes": total, "duration": round(dur, 2),
                    "mbps": round(mbps, 2), "mbs": round(total / 1e6 / dur, 2)}
        except Exception as e:
            last_err = str(e)
    return {"ok": False, "msg": "下载测速失败: " + last_err}


def upload_speed_test():
    """上传测速：POST 数据到 Cloudflare 上传端点（业界标准测速方式）。
    Cloudflare 跨境线路可能不稳定，重试 3 次提高成功率。"""
    payload = os.urandom(8 * 1024 * 1024)  # 8MB
    last_err = "未知错误"
    for attempt in range(3):
        try:
            req = urllib.request.Request("https://speed.cloudflare.com/__up", data=payload,
                                         headers={"User-Agent": "Mozilla/5.0",
                                                  "Content-Type": "application/octet-stream"},
                                         method="POST")
            start = time.time()
            with urllib.request.urlopen(req, timeout=45) as r:
                r.read()
            dur = time.time() - start
            mbps = len(payload) * 8 / 1e6 / dur
            return {"ok": True, "source": "Cloudflare", "bytes": len(payload),
                    "duration": round(dur, 2), "mbps": round(mbps, 2), "mbs": round(len(payload) / 1e6 / dur, 2)}
        except Exception as e:
            last_err = str(e)
            if attempt < 2:
                time.sleep(1)
    return {"ok": False, "msg": "上传测速失败: %s（已重试3次）" % last_err}


def speed_test():
    """下载 + 上传测速（一次返回两个结果）"""
    down = _download_speed_test()
    up = upload_speed_test()
    return {"ok": down.get("ok", False) or up.get("ok", False), "download": down, "upload": up}


def _nic_errors():
    """网卡错误/丢弃包统计"""
    try:
        out, _ = _run(["netstat", "-e"], timeout=10)
        errors = discards = None
        for line in out.splitlines():
            low = line.lower()
            if "errors" in low or "错误" in line:
                nums = [int(p) for p in line.split() if p.isdigit()]
                if nums:
                    errors = sum(nums)
            elif "discards" in low or "丢弃" in line:
                nums = [int(p) for p in line.split() if p.isdigit()]
                if nums:
                    discards = sum(nums)
        return {"errors": errors, "discards": discards}
    except Exception:
        return {"errors": None, "discards": None}


def network_health():
    """一键网络状态检测：综合诊断 + 详细卡顿原因 + 对应修复动作"""
    gateway = get_gateway()
    diag = network_diag()
    by_name = {r.get("name"): r for r in diag["results"] if not r.get("error")}
    speed = speed_test()
    down = speed.get("download") or {}
    up = speed.get("upload") or {}
    down_mbps = down.get("mbps") if down.get("ok") else None
    up_mbps = up.get("mbps") if up.get("ok") else None
    nic = _nic_errors()

    reasons = []  # {level, title, detail, fixes:[{action,label}]}

    def add(level, title, detail, fixes=None):
        reasons.append({"level": level, "title": title, "detail": detail, "fixes": fixes or []})

    # 1) 默认网关
    gw = by_name.get("本机网关") or by_name.get("本机网关(IPv6)")
    if gw:
        if gw.get("loss") == 100 or gw.get("avg_ms") is None:
            add("bad", "默认网关无响应",
                "本机到路由器（%s）Ping 完全不通，局域网连接可能已中断。" % (gateway or "未知网关"),
                [{"action": "renew", "label": "释放并重新获取 IP"}, {"action": "hint_router", "label": "查看重启路由器指引"}])
        elif gw.get("loss") and gw["loss"] > 10:
            add("bad", "局域网丢包严重",
                "到路由器丢包 %s%%，WiFi 信号弱、网线松动或路由器不稳定，网页和视频会明显卡顿。" % gw["loss"],
                [{"action": "hint_router", "label": "查看重启路由器指引"}, {"action": "hint_wifi", "label": "WiFi 优化建议"}])
        elif gw.get("avg_ms") and gw["avg_ms"] > 100:
            add("warn", "路由器响应偏慢",
                "到默认网关平均 %sms（正常应 <10ms），多为 WiFi 信号差或路由器负载过高。" % gw["avg_ms"],
                [{"action": "hint_router", "label": "查看重启路由器指引"}])
    # 2) DNS
    dns_items = [by_name[k] for k in ("阿里 DNS", "腾讯 DNS", "谷歌 DNS") if k in by_name]
    alive_dns = [r for r in dns_items if r.get("loss") is not None and r["loss"] < 100]
    dns_slow = [r for r in alive_dns if r.get("avg_ms") and r["avg_ms"] > 200]
    dns_loss = [r for r in alive_dns if r.get("loss") and r["loss"] > 5]
    if not alive_dns and dns_items:
        add("bad", "DNS 解析异常",
            "所有公共 DNS 均无法连通，可能导致网页打不开、域名解析失败（常见于 DNS 被劫持或网络策略限制）。",
            [{"action": "flushdns", "label": "刷新 DNS 缓存"}, {"action": "hint_dns", "label": "查看更换 DNS 指引"}])
    else:
        if dns_loss:
            add("warn", "DNS 丢包",
                "%d/%d 个 DNS 服务器存在丢包，域名解析不稳定，打开网页会时快时慢。" % (len(dns_loss), len(dns_items)),
                [{"action": "flushdns", "label": "刷新 DNS 缓存"}, {"action": "hint_dns", "label": "查看更换 DNS 指引"}])
        elif dns_slow:
            add("warn", "DNS 响应慢",
                "DNS 服务器平均延迟最高 %sms（正常应 <50ms），域名解析变慢会让打开网页明显变卡。" % max(r["avg_ms"] for r in dns_slow),
                [{"action": "flushdns", "label": "刷新 DNS 缓存"}, {"action": "hint_dns", "label": "查看更换 DNS 指引"}])
    # 3) 外网（百度）
    ext = by_name.get("百度首页")
    if ext:
        if ext.get("loss") == 100 or ext.get("avg_ms") is None:
            add("bad", "外网连接异常",
                "无法连通百度等外网站点，可能是运营商线路故障、出口受限或 DNS 未解析到有效地址。",
                [{"action": "renew", "label": "释放并重新获取 IP"}, {"action": "winsock", "label": "重置 Winsock"}, {"action": "tcpip", "label": "重置 TCP/IP"}])
        elif ext.get("loss") and ext["loss"] > 5:
            add("warn", "外网丢包",
                "访问外网丢包 %s%%，玩游戏、看视频会出现跳 PING 和卡顿。" % ext["loss"],
                [{"action": "winsock", "label": "重置 Winsock"}, {"action": "tcpip", "label": "重置 TCP/IP"}])
        elif ext.get("avg_ms") and ext["avg_ms"] > 150:
            add("warn", "外网延迟偏高",
                "访问外网平均 %sms，运营商出口线路延迟较大，对延迟敏感的操作会感到卡顿。" % ext["avg_ms"],
                [{"action": "hint_isp", "label": "查看运营商相关建议"}])
    # 4) 带宽
    if down_mbps is not None:
        if down_mbps < 3:
            add("bad", "下载带宽严重不足",
                "实测下载仅 %.1f Mbps，看视频、下载大文件都会很卡，可能被后台程序占满或宽带故障。" % down_mbps,
                [{"action": "hint_bandwidth", "label": "查看带宽排查建议"}])
        elif down_mbps < 10:
            add("warn", "下载带宽偏低",
                "实测下载 %.1f Mbps，可能被后台下载/网盘占用，或宽带套餐本身带宽有限。" % down_mbps,
                [{"action": "hint_bandwidth", "label": "查看带宽排查建议"}])
    if up_mbps is not None:
        if up_mbps < 2:
            add("warn", "上传带宽偏低",
                "实测上传仅 %.1f Mbps，视频通话、上传大文件会明显变慢。" % up_mbps,
                [{"action": "hint_bandwidth", "label": "查看带宽排查建议"}])
    # 5) 网卡错误包
    err_total = nic.get("errors") or 0
    if err_total and err_total > 50:
        add("warn", "网卡错误包过多",
            "网络适配器累计错误包 %s 个，可能存在网卡驱动异常或硬件接触问题。" % err_total,
            [{"action": "hint_driver", "label": "查看网卡驱动建议"}])

    # 综合判定
    bads = [r for r in reasons if r["level"] == "bad"]
    warns = [r for r in reasons if r["level"] == "warn"]
    if bads:
        status, status_txt = "bad", "网络卡顿 / 异常"
    elif warns:
        status, status_txt = "warn", "网络轻度异常"
    else:
        status, status_txt = "ok", "网络状态良好"
    score = max(0, min(100, 100 - len(bads) * 25 - len(warns) * 10))

    all_fixes = {}
    for r in reasons:
        for f in r["fixes"]:
            all_fixes.setdefault(f["action"], f["label"])
    return {"status": status, "status_txt": status_txt, "score": score,
            "gateway": gateway, "ts": time.strftime("%H:%M:%S"),
            "reasons": reasons,
            "fixes": [{"action": k, "label": v} for k, v in all_fixes.items()],
            "speed": {"down_mbps": down_mbps, "up_mbps": up_mbps}}


def network_fix(action):
    """网络修复：显式触发。管理员操作经 UAC 提权执行。"""
    action = action or ""
    if action == "flushdns":
        out, code = _run(["ipconfig", "/flushdns"], timeout=15)
        ok = "成功" in out or "success" in out.lower() or code == 0
        _log_op("network_fix", note="刷新 DNS 缓存", ok=ok)
        return {"ok": ok, "action": "刷新 DNS 缓存", "output": out.strip()[-300:] or "(无输出)"}
    if action == "renew":
        out1, _ = _run(["ipconfig", "/release"], timeout=20)
        out2, _ = _run(["ipconfig", "/renew"], timeout=30)
        _log_op("network_fix", note="释放并重新获取 IP")
        return {"ok": True, "action": "释放并重新获取 IP", "output": (out1 + out2).strip()[-400:] or "(完成)"}
    if action in ("winsock", "tcpip"):
        cmd = "netsh winsock reset" if action == "winsock" else "netsh int ip reset"
        # 经 UAC 提权，在新窗口中执行并暂停以便查看
        ps = ("Start-Process cmd -Verb RunAs -ArgumentList '/k \"%s\"'" % cmd)
        subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                         creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0)
        _log_op("network_fix", note="重置 " + ("Winsock" if action == "winsock" else "TCP/IP 协议栈") + "（UAC 提权）")
        return {"ok": True, "action": ("重置 Winsock" if action == "winsock" else "重置 TCP/IP 协议栈"),
                "output": "已在系统中触发管理员权限操作，请在 UAC 弹窗点击“是”，命令将在一个新窗口执行并停留显示结果。"}
    return {"ok": False, "msg": "未知操作"}


def perf_diagnose():
    cpu = get_cpu()
    mem = get_memory()
    disks = get_disk()
    battery = get_battery()
    gpus = get_gpu()
    sys = get_sysinfo()
    proc_count = len(psutil.pids()) if psutil else 0
    scores = {}
    tips = []
    s = max(0, 100 - cpu["percent"] * 1.2)
    scores["CPU"] = round(min(100, s), 0)
    if cpu["percent"] > 85:
        tips.append("CPU 持续高占用（%.0f%%），建议关闭后台高占用进程" % cpu["percent"])
    elif cpu["percent"] > 60:
        tips.append("CPU 占用偏高（%.0f%%），可关注后台任务" % cpu["percent"])
    used_pct = mem["percent"]
    scores["内存"] = round(max(0, 100 - used_pct * 1.1), 0)
    if used_pct > 85:
        tips.append("内存使用率 %.0f%%，可用内存紧张，建议执行内存清理或关闭大内存应用" % used_pct)
    elif used_pct > 70:
        tips.append("内存使用率 %.0f%%，属正常偏高，必要时可清理" % used_pct)
    worst = max((d["percent"] for d in disks), default=0)
    scores["磁盘"] = round(max(0, 100 - worst), 0)
    for d in disks:
        if d["percent"] > 90:
            tips.append("磁盘 %s 剩余空间仅 %s，建议清理大文件" % (d["mount"], human_bytes(d["free"])))
    if sys.get("disks"):
        bad = [x for x in sys["disks"] if str(x.get("Health")) != "Healthy"]
        tips.append("物理磁盘健康状态异常: %s" % ", ".join(str(x.get("Name")) for x in bad) if bad else "物理磁盘健康状态正常")
    scores["网络"] = 90
    if battery and not battery["plugged"] and battery["percent"] < 20:
        scores["网络"] -= 10
        tips.append("电池电量偏低（%d%%）且未接电源，可能影响性能" % battery["percent"])
    nv = [g for g in gpus if g.get("vendor") == "NVIDIA" and g.get("util") is not None]
    if nv:
        u = max(g["util"] for g in nv)
        scores["显卡"] = round(max(0, 100 - u * 1.2), 0)
    else:
        scores["显卡"] = 80
    sys_s = 100
    if sys.get("uptime_sec", 0) > 7 * 86400:
        sys_s -= 15
        tips.append("系统已连续运行 %.1f 天，建议重启以释放累积内存碎片" % (sys.get("uptime_sec", 0) / 86400))
    if proc_count > 200:
        sys_s -= 10
        tips.append("后台进程数较多（%d 个），可考虑减少开机自启项" % proc_count)
    scores["系统"] = sys_s
    total = round(sum(scores.values()) / len(scores), 0)
    if not tips:
        tips.append("各项指标正常，无需特别优化")
    if len(tips) > 6:
        tips = tips[:6]
    return {"total": total, "scores": scores, "tips": tips,
            "snapshot": {"cpu": cpu["percent"], "mem": mem["percent"], "disk_worst": worst, "proc_count": proc_count},
            "ts": time.strftime("%Y-%m-%d %H:%M:%S")}


# ---------------- 磁盘分析（后台线程） ----------------

def _disk_scan_worker(path, depth, top):
    global _disk_scan
    _disk_scan = {"running": True, "path": path, "depth": depth, "progress": "扫描中…",
                  "folders": [], "files": [], "done": False, "scanned": 0}
    folder_sizes = {}
    big_files = []
    scanned = [0]

    def walk(p, d):
        if d > depth:
            return
        try:
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if e.is_file(follow_symlinks=False):
                            try:
                                sz = e.stat().st_size
                            except Exception:
                                sz = 0
                            folder_sizes[p] = folder_sizes.get(p, 0) + sz
                            big_files.append({"path": e.path, "size": sz, "name": e.name})
                            scanned[0] += 1
                        elif e.is_dir(follow_symlinks=False) and d < depth:
                            walk(e.path, d + 1)
                    except Exception:
                        pass
        except Exception:
            pass

    try:
        walk(path, 0)
        big_files.sort(key=lambda x: -x["size"])
        folders = [{"path": k, "size": v} for k, v in folder_sizes.items()]
        folders.sort(key=lambda x: -x["size"])
        _disk_scan["folders"] = folders[:200]
        _disk_scan["files"] = big_files[:800]
        _disk_scan["progress"] = "完成，共统计 %s 个文件" % _fmt(scanned[0])
        _disk_scan["done"] = True
    except Exception as e:
        _disk_scan["progress"] = "扫描出错: %s" % e
        _disk_scan["done"] = True
    finally:
        _disk_scan["running"] = False
        _disk_scan["scanned"] = scanned[0]


def start_disk_scan(path, depth=2):
    if _disk_scan["running"]:
        return {"ok": False, "msg": "已有扫描进行中"}
    t = threading.Thread(target=_disk_scan_worker, args=(path, max(1, min(5, int(depth))), 800), daemon=True)
    t.start()
    return {"ok": True}


def disk_scan_status():
    return {k: _disk_scan[k] for k in ("running", "path", "depth", "progress", "folders", "files", "done", "scanned")}


# ---------------- 微信专清 / 类型专清（后台线程） ----------------

_spec_scan = {"running": False, "kind": "", "path": "", "progress": "", "done": False,
              "items": [], "categories": [], "total": 0, "count": 0, "root": ""}


def spec_scan_status():
    return dict(_spec_scan)


def _find_wechat_roots():
    """探测微信数据目录：微信3.x(WeChat Files) / 微信4.x(xwechat_files) 常见位置"""
    user = os.environ.get("USERNAME", "")
    docs = os.path.expanduser("~\\Documents")
    home = os.path.expanduser("~")
    bases = [docs, home, r"C:\Users\%s" % user, r"D:\wechat data", "D:\\", "E:\\", "C:\\"]
    cands = []
    for b in bases:
        for sub in ("WeChat Files", "xwechat_files"):
            p = os.path.join(b, sub)
            if os.path.isdir(p):
                cands.append(p)
    seen, out = set(), []
    for c in cands:
        if c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out


def wechat_scan(root=None):
    global _spec_scan
    if _spec_scan["running"]:
        return {"ok": False, "msg": "已有专项扫描进行中"}
    if not root:
        roots = _find_wechat_roots()
        if not roots:
            return {"ok": False, "msg": "未自动找到微信数据目录，请在输入框手动填写（如 D:\\wechat data\\xwechat_files）"}
        root = roots[0]
    if not os.path.isdir(root):
        return {"ok": False, "msg": "微信数据目录不存在: %s" % root}
    threading.Thread(target=_wechat_scan_worker, args=(root,), daemon=True).start()
    return {"ok": True, "root": root}


def _wechat_cat_of(path):
    low = path.lower().rstrip("\\/")
    if low.endswith("\\cache") or "\\cache\\" in low:
        return "缓存"
    if low.endswith("\\temp") or low.endswith("\\tmp") or "\\temp\\" in low or "\\tmp\\" in low:
        return "临时"
    if low.endswith("\\msg") or "\\msg\\" in low or low.endswith("\\filestorage") or "\\filestorage\\" in low:
        return "聊天记录/文件"
    if low.endswith("\\db") or "\\db\\" in low or "db_storage" in low:
        return "数据库"
    return "其他"


def _wechat_scan_worker(root):
    global _spec_scan
    _spec_scan = {"running": True, "kind": "wechat", "path": root, "progress": "扫描中…",
                  "done": False, "items": [], "categories": [], "total": 0, "count": 0, "root": root}
    cats, cat_files, big = {}, {}, []
    total, count = 0, 0
    try:
        for base, _dirs, files in os.walk(root):
            for f in files:
                try:
                    fp = os.path.join(base, f)
                    sz = os.path.getsize(fp)
                except Exception:
                    continue
                total += sz
                count += 1
                cat = _wechat_cat_of(base)
                cats[cat] = cats.get(cat, 0) + sz
                cat_files[cat] = cat_files.get(cat, 0) + 1
                if sz >= 50 * 1024 * 1024:
                    big.append({"path": fp, "size": sz, "name": f})
    except Exception as e:
        _spec_scan["progress"] = "扫描出错: %s" % e
    big.sort(key=lambda x: -x["size"])
    _spec_scan["categories"] = [{"name": k, "size": cats.get(k, 0), "count": cat_files.get(k, 0)}
                                for k in ("聊天记录/文件", "缓存", "临时", "数据库", "其他") if cats.get(k, 0) > 0]
    _spec_scan["items"] = big[:200]
    _spec_scan["total"] = total
    _spec_scan["count"] = count
    _spec_scan["progress"] = "完成：微信数据共 %s（%s 个文件）" % (human_bytes(total), _fmt(count))
    _spec_scan["done"] = True
    _spec_scan["running"] = False


TYPE_SPECS = {
    "video": {"label": "视频", "exts": (".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".ts", ".m4v", ".rmvb", ".webm", ".3gp", ".m2ts"),
              "min": 100 * 1024 * 1024},
    "archive": {"label": "压缩包", "exts": (".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".zst", ".lz4"),
                "min": 50 * 1024 * 1024},
    "installer": {"label": "安装包/镜像", "exts": (".exe", ".msi", ".msix", ".appx", ".iso", ".dmg"),
                  "min": 50 * 1024 * 1024},
}


def type_scan(ftype, path="C:\\", depth=6):
    global _spec_scan
    if _spec_scan["running"]:
        return {"ok": False, "msg": "已有专项扫描进行中"}
    if ftype not in TYPE_SPECS:
        return {"ok": False, "msg": "未知类型"}
    if not os.path.isdir(path):
        return {"ok": False, "msg": "路径不存在: %s" % path}
    threading.Thread(target=_type_scan_worker, args=(ftype, path, max(1, min(8, int(depth)))), daemon=True).start()
    return {"ok": True}


def _type_scan_worker(ftype, path, depth):
    global _spec_scan
    spec = TYPE_SPECS[ftype]
    _spec_scan = {"running": True, "kind": "type", "ftype": ftype, "path": path, "progress": "扫描中…",
                  "done": False, "items": [], "categories": [], "total": 0, "count": 0, "root": path}
    found = []
    total, count = 0, 0
    exts, mmin = tuple(spec["exts"]), spec["min"]

    def walk(p, d):
        nonlocal total, count
        if d > depth:
            return
        try:
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if e.is_file(follow_symlinks=False):
                            if e.name.lower().endswith(exts):
                                try:
                                    sz = e.stat().st_size
                                except Exception:
                                    continue
                                if sz >= mmin:
                                    found.append({"path": e.path, "size": sz, "name": e.name})
                                    total += sz
                                    count += 1
                        elif e.is_dir(follow_symlinks=False) and d < depth:
                            walk(e.path, d + 1)
                    except Exception:
                        pass
        except Exception:
            pass

    try:
        walk(path, 0)
        found.sort(key=lambda x: -x["size"])
    except Exception as e:
        _spec_scan["progress"] = "扫描出错: %s" % e
    _spec_scan["items"] = found[:300]
    _spec_scan["total"] = total
    _spec_scan["count"] = count
    _spec_scan["progress"] = "完成：共找到 %s 个大%s（合计 %s）" % (count, spec["label"], human_bytes(total))
    _spec_scan["done"] = True
    _spec_scan["running"] = False


def wechat_clean(cat="缓存"):
    """清理微信缓存/临时目录（与建议清理项一致：empty 模式，运行中占用的文件自动跳过）"""
    roots = _find_wechat_roots()
    if not roots:
        return {"ok": False, "msg": "未找到微信数据目录"}
    root = roots[0]
    low = "cache" if cat in ("缓存", "cache") else "temp"
    targets = []
    try:
        for acc in os.listdir(root):
            p = os.path.join(root, acc)
            if not os.path.isdir(p):
                continue
            for sub in os.listdir(p):
                sp = os.path.join(p, sub)
                if not os.path.isdir(sp):
                    continue
                s = sub.lower()
                if low == "cache" and s == "cache":
                    targets.append(sp)
                elif low == "temp" and s in ("temp", "tmp"):
                    targets.append(sp)
    except Exception as e:
        return {"ok": False, "msg": "读取微信目录失败: %s" % e}
    if not targets:
        return {"ok": False, "msg": "未找到可清理的微信%s目录" % cat}
    freed, files = 0, 0
    for t in targets:
        r = cleanup_execute(t, "empty")
        if r.get("ok"):
            freed += r.get("freed", 0)
            files += r.get("files", 0)
    _log_op("wechat_clean", target="微信" + cat, size=freed,
            note="清理微信%s目录，释放 %s（%d 个文件）" % (cat, human_bytes(freed), files))
    return {"ok": True, "freed": freed, "files": files, "targets": targets}


def _is_protected_path(path):
    """系统关键文件/目录保护：此类路径拒绝删除，防止误操作损坏系统"""
    name = os.path.basename(path).strip().lower()
    if name in ("pagefile.sys", "hiberfil.sys", "swapfile.sys", "bootmgr", "bootmgr.efi",
                "ntldr", "ntdetect.com", "autoexec.bat", "config.sys", "io.sys", "msdos.sys"):
        return True
    p = path.strip().rstrip("\\/").lower()
    sys_prefixes = (
        r"c:\windows\system32", r"c:\windows\syswow64", r"c:\windows\fonts",
        r"c:\windows\winsxs", r"c:\windows\servicing", r"c:\windows\assembly",
        r"c:\program files\windows defender", r"c:\programdata\microsoft\windows defender",
    )
    for pre in sys_prefixes:
        if p == pre or p.startswith(pre + "\\"):
            return True
    return False


def delete_to_trash(path, is_dir=False):
    """删除到回收站（可恢复），显式触发"""
    if not path or not os.path.exists(path):
        return {"ok": False, "msg": "路径不存在: %s" % path}
    if _is_protected_path(path):
        return {"ok": False, "msg": "该路径为系统关键位置，已禁止删除（防止损坏系统）: %s" % path}
    ps_path = path.replace("'", "''")
    if is_dir:
        script = ("Add-Type -AssemblyName Microsoft.VisualBasic; "
                  "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory('%s','OnlyErrorDialogs','SendToRecycleBin')" % ps_path)
    else:
        script = ("Add-Type -AssemblyName Microsoft.VisualBasic; "
                  "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile('%s','OnlyErrorDialogs','SendToRecycleBin')" % ps_path)
    try:
        out, code = _run(["powershell", "-NoProfile", "-Command", script], timeout=60)
        if code == 0 and "Exception" not in out:
            _log_op("delete", path, note="删除到回收站(可恢复)" + ("，目录" if is_dir else "，文件"))
            return {"ok": True, "path": path, "is_dir": is_dir, "to_trash": True}
        _log_op("delete", path, note="删除失败", ok=False)
        return {"ok": False, "msg": "删除失败: %s" % out.strip()[-300:]}
    except Exception as e:
        _log_op("delete", path, note="删除异常: %s" % str(e)[:100], ok=False)
        return {"ok": False, "msg": "删除异常: %s" % e}


def dir_size(path):
    total = 0
    try:
        for root, dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except Exception:
                    pass
    except Exception:
        pass
    return total


def cleanup_suggest():
    """建议清理项：真实统计常见可清理位置的大小"""
    items = []
    user = os.environ.get("USERNAME", "user")
    appdata = os.environ.get("APPDATA", "")
    local = os.environ.get("LOCALAPPDATA", "")
    temp = os.environ.get("TEMP", "")
    targets = [
        ("用户临时文件", temp, "可安全清理（运行中软件占用会跳过）", True),
        ("Windows 临时文件", r"C:\Windows\Temp", "可安全清理", True),
        ("Windows 更新缓存", r"C:\Windows\SoftwareDistribution\Download", "更新后可清理，一般安全", True),
        ("浏览器缓存", os.path.join(local, "Google", "Chrome", "User Data", "Default", "Cache") if os.path.exists(os.path.join(local, "Google")) else None, "关闭浏览器后清理较安全", False),
        ("缩略图缓存", os.path.join(local, "Microsoft", "Windows", "Explorer"), "可安全清理", True),
        ("旧版 Windows 备份", r"C:\Windows.old", "系统回退仅剩此途径，谨慎", False),
    ]
    for name, p, note, safe in targets:
        if not p or not os.path.exists(p):
            continue
        sz = dir_size(p)
        items.append({"name": name, "path": p, "size": sz, "note": note, "safe": safe})
    # 回收站
    try:
        rb = r"C:\$Recycle.Bin"
        items.append({"name": "回收站", "path": rb, "size": dir_size(rb), "note": "清空后不可恢复，请先确认", "safe": False, "recycle": True})
    except Exception:
        pass
    # 下载目录大文件
    dl = os.path.join(os.path.expanduser("~"), "Downloads")
    if os.path.isdir(dl):
        big = []
        try:
            for f in os.listdir(dl):
                fp = os.path.join(dl, f)
                if os.path.isfile(fp):
                    try:
                        sz = os.path.getsize(fp)
                        if sz > 100 * 1024 * 1024:
                            big.append({"name": f, "size": sz, "path": fp})
                    except Exception:
                        pass
        except Exception:
            pass
        big.sort(key=lambda x: -x["size"])
        items.append({"name": "下载目录大文件(>100MB)", "path": dl, "size": sum(x["size"] for x in big),
                      "note": "逐个手动确认，不自动删除", "safe": False, "files": big[:15]})
    items.sort(key=lambda x: -x["size"])
    return items


def cleanup_execute(path, mode="empty"):
    """执行建议清理：显式触发。mode: empty=清空目录内容; delete=整个删除"""
    if not path or not os.path.exists(path):
        return {"ok": False, "msg": "路径不存在"}
    if mode == "empty":
        cnt = 0
        freed = 0
        try:
            for root, dirs, files in os.walk(path):
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        sz = os.path.getsize(fp)
                        os.remove(fp)
                        freed += sz
                        cnt += 1
                    except Exception:
                        pass
                for d in dirs:
                    dp = os.path.join(root, d)
                    try:
                        os.rmdir(dp)
                    except Exception:
                        pass
        except Exception as e:
            _log_op("cleanup", path, note="清理出错: %s" % str(e)[:100], ok=False)
            return {"ok": False, "msg": "清理出错: %s" % e}
        _log_op("cleanup", path, size=freed, note="清空目录内容，释放 %s（%d 个文件）" % (human_bytes(freed), cnt))
        return {"ok": True, "freed": freed, "files": cnt, "path": path}
    if mode == "delete":
        return delete_to_trash(path, is_dir=os.path.isdir(path))
    return {"ok": False, "msg": "未知模式"}


# ---------------- 启动项管理 ----------------

RUN_KEYS = [
    ("HKCU", winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"),
    ("HKLM", winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run"),
    ("HKLM32", winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"),
]
BACKUP_KEY = r"Software\PC-Monitor\StartupBackup"


def _startup_reg_items():
    items = []
    for loc, hive, subkey in RUN_KEYS:
        try:
            with winreg.OpenKey(hive, subkey) as k:
                i = 0
                while True:
                    try:
                        name, val, _ = winreg.EnumValue(k, i)
                        items.append({"type": "reg", "name": name, "command": val,
                                      "location": loc, "key": subkey})
                        i += 1
                    except OSError:
                        break
        except OSError:
            pass
    return items


def _startup_folder_items():
    items = []
    user_startup = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
    all_startup = r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\StartUp"
    for loc, folder in (("USER", user_startup), ("ALL", all_startup)):
        if os.path.isdir(folder):
            try:
                for f in os.listdir(folder):
                    items.append({"type": "folder", "name": f, "path": os.path.join(folder, f), "location": loc})
            except Exception:
                pass
    return items


# 启动项影响评估（按常见高占用软件特征关键词粗评，仅供建议参考）
STARTUP_HEAVY_KW = ["wechat", "weixin", "qq", "tim", "dingtalk", "feishu", "lark", "wps", "office", "word", "excel",
                    "chrome", "edge", "360", "baidu", "ali", "wang", "steam", "epic", "obs", "adobe", "photoshop",
                    "premiere", "driver", "update", "daemon", "helper", "assist", "cloud", "thunder", "xunlei",
                    "baidunet", "browser", "service", "agent", "nvidia", "amd", "intel", "java", "python", "node",
                    "teamviewer", "todesk", "anydesk", "zoom", "onenote", "outlook", "syncthing", "clash", "v2ray",
                    "cuda", "unity", "unreal", "epic", "launcher", "manager", "meeting", "mobile", "appcenter"]


def _assess_startup_impact(items):
    """为每个启动项附加影响等级（high/mid/low），不修改其他字段"""
    for it in items:
        if it.get("impact"):
            continue
        cmd = ((it.get("command") or "") + " " + it.get("name", "")).lower()
        hits = sum(1 for kw in STARTUP_HEAVY_KW if kw in cmd)
        if hits >= 2 or any(k in cmd for k in ("launcher", "updater", "helper", "daemon", "assist", "agent", "service")):
            it["impact"] = "high"
        elif hits == 1:
            it["impact"] = "mid"
        else:
            it["impact"] = "low"
        it["impact_name"] = {"high": "高", "mid": "中", "low": "低"}[it["impact"]]
    return items


def list_startup():
    items = _startup_reg_items() + _startup_folder_items()
    for it in items:
        it["enabled"] = True
    # 标记被禁用(备份区存在)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, BACKUP_KEY) as bk:
            i = 0
            while True:
                try:
                    name, val, _ = winreg.EnumValue(bk, i)
                    for it in items:
                        if it["name"] == name:
                            it["enabled"] = False
                            it["backup_command"] = val
                    i += 1
                except OSError:
                    break
    except OSError:
        pass
    return _assess_startup_impact(items)


def startup_toggle(item):
    """action: disable / enable / delete"""
    name = item.get("name", "")
    action = item.get("action", "")
    itype = item.get("type", "reg")
    if action not in ("disable", "enable", "delete"):
        return {"ok": False, "msg": "未知操作"}
    try:
        if itype == "reg":
            location = item.get("location", "HKCU")
            hive = winreg.HKEY_CURRENT_USER if location == "HKCU" else winreg.HKEY_LOCAL_MACHINE
            keypath = item.get("key") or r"Software\Microsoft\Windows\CurrentVersion\Run"
            if location == "HKLM32":
                keypath = r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, BACKUP_KEY) as bk:
                with winreg.OpenKey(hive, keypath, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as k:
                    if action == "disable":
                        try:
                            cmd, _ = winreg.QueryValueEx(k, name)
                        except OSError:
                            return {"ok": False, "msg": "注册表项已不存在"}
                        winreg.SetValueEx(bk, name, 0, winreg.REG_SZ, cmd)
                        winreg.DeleteValue(k, name)
                    elif action == "enable":
                        try:
                            saved, _ = winreg.QueryValueEx(bk, name)
                        except OSError:
                            return {"ok": False, "msg": "备份中不存在该启动项，无法启用"}
                        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, saved)
                        winreg.DeleteValue(bk, name)
                    else:  # delete
                        try:
                            winreg.DeleteValue(k, name)
                        except OSError:
                            pass
                        try:
                            winreg.DeleteValue(bk, name)
                        except OSError:
                            pass
            _log_op("startup", target=name, note="启动项 %s（注册表 %s）" % (
                {"disable": "禁用", "enable": "启用", "delete": "删除"}.get(action, action), location))
            return {"ok": True, "name": name, "action": action}
        else:  # folder
            folder = item.get("path")
            if not folder or not os.path.exists(folder):
                return {"ok": False, "msg": "启动项文件不存在"}
            backup_dir = os.path.join(os.environ.get("APPDATA", ""), "PC-Monitor", "StartupBackup")
            if action == "delete":
                return delete_to_trash(folder, is_dir=False)
            os.makedirs(backup_dir, exist_ok=True)
            dest = os.path.join(backup_dir, os.path.basename(folder))
            if action == "disable":
                os.rename(folder, dest)
            else:  # enable: 移回启动文件夹
                user_startup = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
                if not os.path.isdir(user_startup):
                    os.makedirs(user_startup, exist_ok=True)
                os.rename(folder, os.path.join(user_startup, os.path.basename(folder)))
            _log_op("startup", target=name, note="启动项 %s（启动文件夹 %s）" % (
                {"disable": "禁用", "enable": "启用", "delete": "删除"}.get(action, action), location))
            return {"ok": True, "name": name, "action": action}
    except PermissionError:
        return {"ok": False, "msg": "权限不足（HKLM 项通常需要管理员权限）"}
    except Exception as e:
        return {"ok": False, "msg": "操作失败: %s" % e}


# ---------------- 已安装软件 ----------------

def list_software():
    apps = {}
    uninstall_paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, subkey in uninstall_paths:
        try:
            with winreg.OpenKey(hive, subkey) as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    try:
                        with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sk:
                            name = ""
                            version = ""
                            publisher = ""
                            for vn, vk in (("DisplayName", 0), ("DisplayVersion", 0), ("Publisher", 0)):
                                try:
                                    val = winreg.QueryValueEx(sk, vn)[0]
                                    if vn == "DisplayName":
                                        name = val
                                    elif vn == "DisplayVersion":
                                        version = val
                                    else:
                                        publisher = val
                                except OSError:
                                    pass
                            if name and name not in apps:
                                apps[name] = {"name": name, "version": version, "publisher": publisher}
                    except OSError:
                        pass
        except OSError:
            pass
    return sorted(apps.values(), key=lambda x: x["name"].lower())


# ---------------- 系统快捷入口 ----------------

OPEN_TARGETS = {
    "control": ("打开控制面板", "control"),
    "explorer": ("打开文件资源管理器", "explorer"),
    "display": ("显示设置", "ms-settings:display"),
    "wallpaper": ("壁纸设置", "ms-settings:personalization-background"),
    "network": ("网络设置", "ms-settings:network"),
    "apps": ("已安装应用", "ms-settings:appsfeatures"),
    "startup": ("启动应用设置", "ms-settings:startupapps"),
    "devmgmt": ("打开设备管理器", "devmgmt.msc"),
}


def open_target(key="", path=""):
    def _log(m):
        try:
            with open(os.path.join(BASE_DIR, ".open.log"), "a", encoding="utf-8") as f:
                f.write(time.strftime("%H:%M:%S ") + m + "\n")
        except Exception:
            pass
    if path:
        # 打开指定目录；若为文件则打开其所在目录并高亮该文件
        path = path.replace('"', "")
        if not os.path.exists(path):
            _log("open-path FAIL(不存在): %s" % path)
            return {"ok": False, "msg": "路径不存在: %s" % path}
        try:
            if os.path.isfile(path):
                # explorer /select 定位文件所在目录并高亮；用字符串形式避免 subprocess 引号干扰
                # CREATE_NO_WINDOW：exe 无控制台环境避免 explorer 弹终端窗口
                _nowin = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
                subprocess.Popen('explorer /select,"%s"' % path, creationflags=_nowin)
                _log("open-file OK: %s" % path)
            else:
                # os.startfile = ShellExecute，Windows 原生最可靠打开目录
                os.startfile(path)
                _log("open-dir OK: %s" % path)
            return {"ok": True, "label": "打开目录", "path": path}
        except Exception as e:
            _log("open-path EXC: %s -> %s" % (path, e))
            return {"ok": False, "msg": "打开失败: %s" % e}
    if key not in OPEN_TARGETS:
        _log("open-key FAIL(未知): %s" % key)
        return {"ok": False, "msg": "未知入口"}
    label, target = OPEN_TARGETS[key]
    try:
        if target.startswith("ms-settings:"):
            subprocess.Popen(["cmd", "/c", "start", "", target],
                             creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0)
        else:
            subprocess.Popen(["cmd", "/c", "start", "", target],
                             creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0)
        _log("open-key OK: %s -> %s" % (key, target))
        return {"ok": True, "label": label}
    except Exception as e:
        _log("open-key EXC: %s -> %s" % (key, e))
        return {"ok": False, "msg": str(e)}


# ---------------- HTTP 服务 ----------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, ctype="application/json; charset=utf-8"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path):
        full = os.path.join(BASE_DIR, path)
        if not os.path.isfile(full):
            self.send_error(404)
            return
        ext = os.path.splitext(full)[1].lower()
        ctype = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml",
                 ".ico": "image/x-icon"}.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p in ("/", "/index.html"):
            self._serve_file("index.html")
        elif p == "/echarts.min.js":
            self._serve_file("echarts.min.js")
        elif p == "/api/metrics":
            self._send(get_metrics())
        elif p == "/api/processes":
            q = u.query
            top = 0
            if "top=" in q:
                try:
                    top = int(q.split("top=")[1].split("&")[0])
                except Exception:
                    top = 0
            self._send(get_processes(top))
        elif p == "/api/sysinfo":
            self._send(get_sysinfo())
        elif p == "/api/disk/scan/status":
            self._send(disk_scan_status())
        elif p == "/api/startup":
            self._send(list_startup())
        elif p == "/api/software":
            self._send(list_software())
        elif p == "/api/cleanup/suggest":
            self._send(cleanup_suggest())
        elif p == "/api/spec/scan/status":
            self._send(spec_scan_status())
        elif p == "/api/drivers":
            self._send(get_drivers())
        elif p == "/api/health":
            self._send(get_health())
        elif p == "/api/checkup/history":
            self._send(checkup_history())
        elif p == "/api/oplog":
            self._send(get_oplog())
        elif p == "/api/disk/topdirs":
            q = u.query
            path = "D:\\"
            if "path=" in q:
                try:
                    from urllib.parse import unquote
                    path = unquote(q.split("path=")[1].split("&")[0])
                except Exception:
                    pass
            self._send(disk_topdirs(path))
        else:
            self.send_error(404)

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            body = {}
        p = u.path
        if p == "/api/memory/clean":
            self._send(memory_clean())
        elif p == "/api/network/diag":
            self._send(network_diag())
        elif p == "/api/network/speedtest":
            self._send(speed_test())
        elif p == "/api/network/health":
            self._send(network_health())
        elif p == "/api/network/fix":
            self._send(network_fix(body.get("action", "")))
        elif p == "/api/perf/diagnose":
            self._send(perf_diagnose())
        elif p == "/api/disk/analyze":
            self._send(start_disk_scan(body.get("path", "C:\\"), body.get("depth", 2)))
        elif p == "/api/process/kill":
            self._send(kill_process(body.get("pid"), body.get("tree", False)))
        elif p == "/api/startup/toggle":
            self._send(startup_toggle(body))
        elif p == "/api/disk/delete":
            self._send(delete_to_trash(body.get("path", ""), body.get("is_dir", False)))
        elif p == "/api/cleanup/execute":
            self._send(cleanup_execute(body.get("path", ""), body.get("mode", "empty")))
        elif p == "/api/open":
            self._send(open_target(body.get("target", ""), body.get("path", "")))
        elif p == "/api/wechat/scan":
            self._send(wechat_scan(body.get("path", "")))
        elif p == "/api/wechat/clean":
            self._send(wechat_clean(body.get("cat", "缓存")))
        elif p == "/api/type/scan":
            self._send(type_scan(body.get("type", ""), body.get("path", "C:\\"), body.get("depth", 6)))
        elif p == "/api/checkup":
            self._send(run_checkup())
        elif p == "/api/oplog/clear":
            self._send(clear_oplog())
        elif p == "/api/shutdown":
            try:
                # 通知守护进程 run.py 正常退出（不再自动重启）
                with open(os.path.join(BASE_DIR, ".stop_flag"), "w") as f:
                    f.write("stop")
            except Exception:
                pass
            self._send({"ok": True, "msg": "服务即将停止"})
            threading.Timer(0.4, _httpd.shutdown).start()
        else:
            self.send_error(404)


def human_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f PB" % n


def _start_tray():
    """exe 打包环境：右下角系统托盘图标，随时可见/打开页面/退出程序。
    源码(非打包)模式不启用托盘，避免干扰开发。"""
    global _tray_icon
    if not getattr(sys, "frozen", False):
        return
    try:
        import pystray
        from PIL import Image

        def _open_page():
            try:
                webbrowser.open("http://%s:%d" % (HOST, PORT))
            except Exception:
                pass

        def _quit():
            try:
                with open(os.path.join(BASE_DIR, ".stop_flag"), "w") as f:
                    f.write("stop")
            except Exception:
                pass
            try:
                _httpd.shutdown()
            except Exception:
                pass

        ic = os.path.join(BASE_DIR, "icon.ico")
        if not os.path.isfile(ic):
            ic = os.path.join(getattr(sys, "_MEIPASS", BASE_DIR), "icon.ico")
        image = Image.open(ic)
        menu = pystray.Menu(
            pystray.MenuItem("打开监测页面", lambda i, it: _open_page(), default=True),
            pystray.MenuItem("退出程序", lambda i, it: _quit()),
        )
        _tray_icon = pystray.Icon("PC-Monitor", image, "PC 性能监测与优化平台", menu)
        threading.Thread(target=_tray_icon.run, daemon=True).start()
        print("托盘图标已启动")
    except Exception:
        _tray_icon = None


def main():
    global _httpd, _tray_icon
    # 显式探测端口占用（Windows 下 allow_reuse_address 允许重复 bind，必须主动探测，
    # 否则"再次双击"会重复启动新实例）。已有服务在运行时：直接打开页面并退出本进程。
    if _port_in_use(HOST, PORT):
        print("端口 %d 已有服务在运行，直接打开已有服务页面 …" % PORT)
        if "--restarted" not in sys.argv:
            try:
                webbrowser.open("http://%s:%d" % (HOST, PORT))
            except Exception:
                pass
        return 0
    try:
        _httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        print("端口 %d 绑定失败: %s" % (PORT, e))
        return 1
    print("=" * 52)
    print(" PC 性能监测与优化平台 v2 已启动")
    print(" 访问地址: http://%s:%d" % (HOST, PORT))
    print(" 按 Ctrl+C 停止服务；页面左下角也可点“结束运行本系统”")
    print("=" * 52)
    _start_tray()
    if "--restarted" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open("http://%s:%d" % (HOST, PORT))).start()
    try:
        _httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        _httpd.shutdown()
    return 0


if __name__ == "__main__":
    main()
