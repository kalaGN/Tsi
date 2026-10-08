"""将零依赖页面渲染回归测试纳入项目测试门禁。"""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_web_markdown_rendering_and_stream_cache():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node 未安装，无法执行页面渲染单元测试")
    subprocess.run([node, str(Path(__file__).with_name("web_markdown_test.cjs"))],
                   check=True, capture_output=True, text=True)
