# -*- coding: utf-8 -*-
"""后端操作全量自测脚本"""
import json, os, sys, time, subprocess, urllib.request

BASE = "http://127.0.0.1:8765"

def get(p):
    with urllib.request.urlopen(BASE + p, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))

def post(p, body=None):
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(BASE + p, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))

ok = 0; fail = 0
NO_DELETE = "--no-delete" in sys.argv  # 用户要求：磁盘删除功能禁止测试，跳过该项
def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print("  [PASS]", name, extra)
    else:
        fail += 1; print("  [FAIL]", name, extra)

print("== 1 GPU 完整数据 ==")
m = get("/api/metrics")
g = m["gpu"]
check("GPU 数量", len(g) >= 1, str([x["name"] for x in g]))
if g and g[0].get("vendor") == "NVIDIA":
    check("GPU util 字段", g[0]["util"] is not None, "util=%s temp=%s vram=%s/%s" % (g[0]["util"], g[0]["temp"], g[0]["mem_used"], g[0]["mem_total"]))
    check("GPU 时钟字段", g[0]["clock_graphics"] is not None, "sm=%s mem=%s" % (g[0]["clock_graphics"], g[0]["clock_mem"]))
check("GPU apps", isinstance(m.get("gpu_apps"), list), "count=%d" % len(m.get("gpu_apps", [])))

print("== 2 CPU 采样（连续3次） ==")
vals = [get("/api/metrics")["cpu"]["percent"] for _ in range(3)]
check("CPU 有值", all(isinstance(v, (int, float)) for v in vals), str([round(v,1) for v in vals]))

print("== 3 全部进程 ==")
procs = get("/api/processes")
check("进程数>50", len(procs) > 50, "count=%d" % len(procs))
check("有CPU字段", all("cpu" in p and "mem" in p for p in procs[:5]))

print("== 4 内存清理(明细) ==")
mc = post("/api/memory/clean")
check("清理ok", mc.get("ok"), "freed=%s" % mc.get("note", ""))
check("明细非空", len(mc.get("details", [])) > 0, "top=%s freed=%s" % (mc["details"][0]["name"] if mc.get("details") else "?", mc["details"][0]["freed"] if mc.get("details") else 0))

print("== 5 网络诊断 ==")
nd = post("/api/network/diag")
check("diag结果", len(nd.get("results", [])) >= 4, "gateway=%s" % nd.get("gateway"))
have_avg = sum(1 for r in nd["results"] if r.get("avg_ms") is not None)
check("多数有延迟", have_avg >= 3, "avg_count=%d" % have_avg)

print("== 6 性能诊断 ==")
pd = post("/api/perf/diagnose")
check("perf total", isinstance(pd.get("total"), (int, float)), "total=%s scores=%s" % (pd.get("total"), pd.get("scores")))
check("perf tips", len(pd.get("tips", [])) > 0)

print("== 7 磁盘分析(深度1) ==")
r = post("/api/disk/analyze", {"path": "C:\\", "depth": 1})
check("启动扫描", r.get("ok"))
time.sleep(9)
st = get("/api/disk/scan/status")
check("扫描完成", st.get("done"), st.get("progress"))
check("有结果", len(st.get("folders", [])) > 0, "folders=%d files=%d" % (len(st.get("folders", [])), len(st.get("files", []))))

print("== 8 进程结束(自建测试进程) ==")
p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
pid = p.pid
time.sleep(0.5)
kp = post("/api/process/kill", {"pid": pid})
check("kill ok", kp.get("ok"), str(kp))
p.wait(timeout=5)
check("进程已结束", p.poll() is not None)

print("== 9 系统保护(杀关键进程应被拒) ==")
kp2 = post("/api/process/kill", {"pid": 4})
check("拒绝pid<=4", not kp2.get("ok"), str(kp2))

print("== 10 启动项切换(用临时测试值) ==")
import winreg
test_name = "_pcmon_test_startup"
with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as k:
    winreg.SetValueEx(k, test_name, 0, winreg.REG_SZ, "notepad.exe")
try:
    r1 = post("/api/startup/toggle", {"name": test_name, "type": "reg", "location": "HKCU", "action": "disable"})
    check("禁用", r1.get("ok"), str(r1))
    # 验证值已移走
    found = False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            winreg.QueryValueEx(k, test_name)
        found = True
    except OSError:
        found = False
    check("原键已移除", not found)
    r2 = post("/api/startup/toggle", {"name": test_name, "type": "reg", "location": "HKCU", "action": "enable"})
    check("重新启用", r2.get("ok"), str(r2))
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
        v, _ = winreg.QueryValueEx(k, test_name)
    check("值已恢复", v == "notepad.exe")
finally:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, test_name)
    except OSError:
        pass
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\PC-Monitor\StartupBackup", 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, test_name)
    except OSError:
        pass

if not NO_DELETE:
    print("== 11 删除到回收站(临时文件) ==")
    tmp = os.path.join(os.environ["TEMP"], "_pcmon_test_del.txt")
    open(tmp, "w").write("test")
    dl = post("/api/disk/delete", {"path": tmp, "is_dir": False})
    check("删除到回收站", dl.get("ok") and dl.get("to_trash"), str(dl))
    check("文件已不在", not os.path.exists(tmp))
else:
    print("== 11 删除到回收站 -- 已跳过（用户明确禁止测试磁盘删除） ==")

print("== 12 建议清理 + 执行(临时目录) ==")
cs = get("/api/cleanup/suggest")
check("建议项", len(cs) > 0, str([x["name"] for x in cs[:4]]))
testdir = os.path.join(os.environ["TEMP"], "_pcmon_test_clean")
os.makedirs(testdir, exist_ok=True)
open(os.path.join(testdir, "a.tmp"), "wb").write(b"x" * 2048)
ce = post("/api/cleanup/execute", {"path": testdir, "mode": "empty"})
check("清理执行", ce.get("ok"), "freed=%s files=%d" % (ce.get("freed"), ce.get("files", 0)))
check("目录内容已清", len(os.listdir(testdir)) == 0)
os.rmdir(testdir)

print("== 13 网络修复(flushdns 安全项) ==")
nf = post("/api/network/fix", {"action": "flushdns"})
check("flushdns", nf.get("ok"), nf.get("output", "")[:60].replace("\n", " "))

print("== 14 系统信息/软件 ==")
si = get("/api/sysinfo")
check("sysinfo", si.get("cpu_model") and si.get("os_caption"), si.get("cpu_model", "?")[:40])
sw = get("/api/software")
check("软件数>10", len(sw) > 10, "count=%d" % len(sw))

print("\n===== 汇总: PASS=%d FAIL=%d =====" % (ok, fail))
sys.exit(0 if fail == 0 else 1)
