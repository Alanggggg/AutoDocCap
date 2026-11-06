#!/bin/zsh
# 打包当前芯片架构的 .app 并压缩成 zip：Apple 芯片的 Mac 上得到 AppleSilicon 版，Intel 的 Mac 上得到 Intel 版。
# 用法：./build.sh [版本号]      需先安装 requirements-dev.txt
set -euo pipefail
cd "${0:A:h}"
export DOCCAP_VERSION="${1:-0.1.0}"
PY="${PYTHON:-.venv/bin/python}"
case "$(uname -m)" in
  arm64) ARCH=AppleSilicon ;;
  x86_64) ARCH=Intel ;;
  *) echo "不支持的架构 $(uname -m)"; exit 1 ;;
esac
rm -rf build dist
"$PY" -m PyInstaller --noconfirm --clean doccap.spec
# ditto 打包能保留 .app 内的符号链接和签名；用普通 zip 可能导致下载后提示"已损坏"
ZIP="dist/需求文档截图工具-v${DOCCAP_VERSION}-${ARCH}.zip"
ditto -c -k --keepParent "dist/需求文档截图工具.app" "$ZIP"
echo "完成：$ZIP"
