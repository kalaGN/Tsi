"""隔离测量桌面后端到就绪的耗时，不使用真实配置或请求模型。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import selectors
import statistics
import subprocess
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def measure(binary: Path) -> float:
    """使用临时状态目录启动一次，并校验页面和 API 认证后关闭进程。"""

    with tempfile.TemporaryDirectory(prefix="tsi-startup-") as root:
        token = "b" * 32
        env = {**os.environ, "TSI_DESKTOP_TOKEN": token, "TSI_DESKTOP_STATE_ROOT": root}
        started = time.monotonic()
        process = subprocess.Popen(
            [str(binary)], env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            if not selector.select(30):
                raise RuntimeError("后端 30 秒内未就绪。")
            line = process.stdout.readline().decode().strip()
            if not line.startswith("TSI_READY:"):
                raise RuntimeError("后端退出或返回非法就绪消息。")
            elapsed = time.monotonic() - started
            url = f"http://127.0.0.1:{int(line.split(':')[1])}"
            with urlopen(url + "/ui", timeout=5) as response:
                if response.status != 200:
                    raise RuntimeError("页面不可用。")
            try:
                urlopen(url + "/ui/api/bootstrap", timeout=5).close()
            except HTTPError as exc:
                if exc.code != 401:
                    raise
            else:
                raise RuntimeError("缺少令牌的 API 请求未被拒绝。")
            request = Request(url + "/ui/api/bootstrap", headers={"X-Tsi-Desktop-Token": token})
            with urlopen(request, timeout=5) as response:
                json.load(response)
            return elapsed
        finally:
            # 始终先通知正常退出；兜底只终止本脚本创建的隔离进程。
            process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
            selector.close()
            process.stdout.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.runs <= 10:
        parser.error("测量次数必须在 1～10 之间。")
    timings = []
    for number in range(args.runs):
        elapsed = measure(args.binary.resolve(strict=True))
        timings.append(elapsed)
        print(json.dumps({"run": number + 1, "ready_seconds": round(elapsed, 3)}), flush=True)
    print(json.dumps({"median_seconds": round(statistics.median(timings), 3)}))
