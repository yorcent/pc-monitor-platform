#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PC 性能监测与优化平台 - 守护进程
=================================
负责拉起 server.py，并在其异常退出时自动重启，保证平台不会无声挂掉。
用户点击「结束运行本系统」时，server.py 会写 STOP_FLAG，守护进程据此正常退出、不再重启。
"""
import os
import sys
import time
import socket
import webbrowser
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STOP_FLAG = os.path.join(BASE_DIR, ".stop_flag")
SERVER = os.path.join(BASE_DIR, "server.py")
HOST, PORT = "127.0.0.1", 8765


def port_in_use(host, port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            return s.connect_ex((host, port)) == 0
    except Exception:
        return False


def main():
    # 清理可能残留的停止标记
    if os.path.exists(STOP_FLAG):
        try:
            os.remove(STOP_FLAG)
        except OSError:
            pass
    # 服务已在本机运行时：直接打开浏览器即可（避免重复启动导致端口冲突）
    if port_in_use(HOST, PORT):
        print("服务已在运行（端口 %d），直接打开浏览器 …" % PORT)
        webbrowser.open("http://%s:%d" % (HOST, PORT))
        return
    while True:
        print("=" * 52)
        print(" 守护进程启动 server.py ...")
        print("=" * 52)
        restarting = os.path.exists(os.path.join(BASE_DIR, ".restarted"))
        args = [sys.executable, SERVER]
        if restarting:
            args.append("--restarted")
        p = subprocess.Popen(args, cwd=BASE_DIR)
        p.wait()
        if os.path.exists(STOP_FLAG):
            try:
                os.remove(STOP_FLAG)
            except OSError:
                pass
            print("已收到停止标记，守护进程退出。")
            break
        print("server.py 异常退出（代码 %s），2 秒后自动重启 ..." % p.returncode)
        try:
            with open(os.path.join(BASE_DIR, ".restarted"), "w") as f:
                f.write("1")
        except OSError:
            pass
        time.sleep(2)


if __name__ == "__main__":
    main()
