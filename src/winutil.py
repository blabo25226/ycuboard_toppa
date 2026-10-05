"""Windows 固有の補助: PowerShell の実行、常駐プロセスの検出・起動・停止。"""
import base64
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from src.config import BASE_DIR

TASK_NAME = "YCUBoardWatcher"


def powershell(script: str, timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    # 日本語パスが文字化けしないよう UTF-16LE の Base64 で渡す
    script = "$ProgressPreference = 'SilentlyContinue'\n" + script
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        ["powershell", "-NoProfile", "-EncodedCommand", encoded], capture_output=True, text=True, errors="replace",
        timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def find_watchers() -> List[int]:
    """動作中の `python -m src.main --watch` のプロセスID（自分自身は除く）。"""
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -match 'src\\.main\\s+--watch' -and $_.Name -match '^pythonw?\\.exe$' } | "
        "ForEach-Object { $_.ProcessId }"
    )
    result = powershell(script)
    pids = [int(x) for x in result.stdout.split() if x.isdigit()]
    return [p for p in pids if p != os.getpid()]


def task_registered() -> bool:
    r = subprocess.run(["schtasks", "/query", "/tn", TASK_NAME], capture_output=True)
    return r.returncode == 0


def start_watcher() -> str:
    """常駐を開始する。登録済みならタスク経由、未登録なら画面なしで直接起動。戻り値は方法の説明。"""
    if task_registered():
        r = powershell(f"Start-ScheduledTask -TaskName '{TASK_NAME}'")
        if r.returncode == 0:
            return "自動起動タスクを開始しました"
    exe = Path(sys.executable)
    runner = exe.with_name("pythonw.exe")
    runner = runner if runner.exists() else exe
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen([str(runner), "-m", "src.main", "--watch"], cwd=str(BASE_DIR), creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "常駐プロセスを直接起動しました（PC を再起動すると止まります。--install-task で自動起動を登録してください）"


def stop_watchers() -> int:
    pids = find_watchers()
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    return len(pids)
