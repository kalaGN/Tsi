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

target_triple="$(rustc --print host-tuple)"
build_root="$project_root/build/tauri-python"
mkdir -p "$build_root" "$project_root/src-tauri/binaries"
export PYINSTALLER_CONFIG_DIR="$build_root/config"
"$project_root/.venv/bin/python" -m PyInstaller \
  --noconfirm --onefile --name tsi-backend \
  --paths "$project_root" \
  --add-data "$project_root/app/webui/static:app/webui/static" \
  --distpath "$build_root/dist" \
  --workpath "$build_root/work" \
  --specpath "$build_root/spec" \
  "$project_root/app/desktop_backend.py"
cp "$build_root/dist/tsi-backend" "$project_root/src-tauri/binaries/tsi-backend-$target_triple"
chmod 755 "$project_root/src-tauri/binaries/tsi-backend-$target_triple"
cd "$project_root"
"${tauri_command[@]}" build --bundles app
