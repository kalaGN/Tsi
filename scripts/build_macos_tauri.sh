#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "当前只支持在 Apple Silicon macOS 上构建。" >&2
  exit 1
fi
if [[ ! -x "$project_root/.venv/bin/python" ]]; then
  echo "请先创建 .venv 并安装 requirements-desktop.txt。" >&2
  exit 1
fi
if ! "$project_root/.venv/bin/python" -m PyInstaller --version >/dev/null 2>&1; then
  echo "请先运行 .venv/bin/python -m pip install -r requirements-desktop.txt。" >&2
  exit 1
fi
tauri_command=(cargo tauri)
if [[ -x "$project_root/build/tauri-cli/bin/cargo-tauri" ]]; then
  tauri_command=("$project_root/build/tauri-cli/bin/cargo-tauri")
elif ! cargo tauri --version >/dev/null 2>&1; then
  echo '请先运行 cargo install tauri-cli --version "^2.0.0" --locked。' >&2
  exit 1
fi

build_root="$project_root/build/tauri-python-onedir"
mkdir -p "$build_root"
export PYINSTALLER_CONFIG_DIR="$build_root/config"
"$project_root/.venv/bin/python" -m PyInstaller \
  --noconfirm --onedir --name tsi-backend \
  --paths "$project_root" \
  --add-data "$project_root/app/webui/static:app/webui/static" \
  --distpath "$build_root/dist" \
  --workpath "$build_root/work" \
  --specpath "$build_root/spec" \
  "$project_root/app/desktop_backend.py"
# 保持可执行文件与 _internal 相邻；Tauri 将整个目录作为资源内置，启动时不再解包。
chmod 755 "$build_root/dist/tsi-backend/tsi-backend"
cd "$project_root"
"${tauri_command[@]}" build --bundles app
