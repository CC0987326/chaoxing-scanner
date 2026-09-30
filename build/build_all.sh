#!/usr/bin/env bash
# =====================================================================
# 从零构建「学习通巡检工具」免安装包
#   1. 下载 Python embeddable + get-pip
#   2. 解压并放开 site-packages（改 _pth）
#   3. 安装 playwright / pillow
#   4. 复制程序文件 + 浏览器内核
#   5. 生成启动器与使用说明（见 mk_extras.py）
# 产物： build/pkg  → 再执行 python build/package.py 得到 ZIP
# =====================================================================
set -e
cd "$(dirname "$0")"
BD="$(pwd)"
PKG="$BD/pkg"
PYVER=3.13.12

# ---------- 1. 下载 ----------
if [ ! -f "$BD/python-embed.zip" ]; then
  echo "[1/5] 下载便携 Python $PYVER"
  curl -L --max-time 900 -o python-embed.zip \
    "https://www.python.org/ftp/python/$PYVER/python-$PYVER-embed-amd64.zip"
  curl -L --max-time 300 -o get-pip.py "https://bootstrap.pypa.io/get-pip.py"
else
  echo "[1/5] 便携 Python 已存在，跳过下载"
fi

# ---------- 2. 解压 + 改 _pth ----------
echo "[2/5] 解压并放开 site-packages"
rm -rf "$PKG"; mkdir -p "$PKG/python"
( cd "$PKG/python" && unzip -q -o "$BD/python-embed.zip" )
cat > "$PKG/python/python313._pth" <<'PTH'
python313.zip
.
Lib\site-packages
import site
PTH

# ---------- 3. 依赖 ----------
echo "[3/5] 安装依赖（playwright / pillow）"
mkdir -p "$PKG/python/Lib/site-packages"
"$PKG/python/python.exe" "$BD/get-pip.py" --no-warn-script-location
"$PKG/python/python.exe" -m pip install --no-warn-script-location playwright pillow

# ---------- 4. 程序 + 内核 ----------
echo "[4/5] 复制程序与浏览器内核"
mkdir -p "$PKG/app"
cp chaoxing_scanner.py webui.py captcha_solver.py "$PKG/app/"
cp config.json "$PKG/app/config.json"
# 分发用配置：清掉个人 cpi
"$PKG/python/python.exe" - <<'PYCFG'
import json, pathlib
p = pathlib.Path(r'./pkg/app/config.json')
cfg = json.loads(p.read_text(encoding='utf-8'))
cfg['cpi'] = ''
cfg['chrome_path'] = ''
p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
print('  config.json 已清理个人参数')
PYCFG

SRC_CHROME="${CHROME_SRC:-$HOME/.workbuddy/tmp/browser/chrome-win64}"
if [ -f "$SRC_CHROME/chrome.exe" ]; then
  mkdir -p "$PKG/chrome"
  cp -r "$SRC_CHROME" "$PKG/chrome/"
  # 精简语言包
  ( cd "$PKG/chrome/chrome-win64/locales" 2>/dev/null && \
    find . -name '*.pak' ! -name 'zh-CN.pak' ! -name 'en-US.pak' -delete || true )
else
  echo "  警告：未找到内核 $SRC_CHROME，请用 CHROME_SRC=... 指定"
fi

# ---------- 5. 启动器 / 说明 ----------
echo "[5/5] 生成启动器与使用说明"
"$PKG/python/python.exe" "$BD/mk_extras.py"

# 清掉运行时残留（登录态、报告、临时 profile 不应分发）
rm -rf "$PKG/app/runtime" "$PKG/app/输出"

echo
echo "构建完成：$PKG"
echo "接着执行：  python build/package.py"
du -sh "$PKG"
