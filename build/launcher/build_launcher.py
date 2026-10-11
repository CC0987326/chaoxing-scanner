# -*- coding: utf-8 -*-
"""编译「学习通巡检工具.exe」图形启动器。

用 Windows 自带的 C# 编译器（.NET Framework 的 csc.exe）把 launcher.cs
编成一个无控制台的 win32 程序。产物只有几 KB，不额外捆绑 Python ——
运行时仍是去拉起同目录的便携 python\\pythonw.exe。

用法：
    python build/launcher/build_launcher.py                 # 编到本目录
    python build/launcher/build_launcher.py --out 目标.exe   # 指定产物路径

被 build/mk_extras.py 调用（把 exe 直接生成进 build/pkg/）。
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
EXE_NAME = '学习通巡检工具'

# csc.exe：优先 64 位，退回 32 位。框架版启动器是 AnyCPU，
# 两个都能编；用 64 位版更常见。
CSC_CANDIDATES = [
    pathlib.Path(r'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'),
    pathlib.Path(r'C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe'),
]


def _find_csc() -> pathlib.Path:
    for p in CSC_CANDIDATES:
        if p.exists():
            return p
    raise FileNotFoundError(
        '找不到 csc.exe（.NET Framework 4 的 C# 编译器）。\n'
        'Windows 10/11 默认自带；若缺失，说明系统精简过 .NET Framework。')


def build(out_exe: pathlib.Path, icon: pathlib.Path | None = None) -> pathlib.Path:
    src = HERE / 'launcher.cs'
    if not src.exists():
        raise FileNotFoundError('找不到 launcher.cs：%s' % src)

    if icon is None:
        icon = HERE / 'launcher.ico'
    if not icon.exists():
        # 顺带把图标也生成了，省得调用方再单独跑一步。
        import make_icon
        icon.parent.mkdir(parents=True, exist_ok=True)
        make_icon.draw(256).save(icon, format='ICO', sizes=make_icon.SIZES)

    csc = _find_csc()
    out_exe.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(csc), '/nologo', '/target:winexe', '/optimize+',
        '/win32icon:' + str(icon),
        '/out:' + str(out_exe),
        str(src),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not out_exe.exists():
        raise RuntimeError(
            'csc 编译失败（退出码 %d）\n%s\n%s'
            % (r.returncode, r.stdout, r.stderr))
    return out_exe


def main() -> int:
    ap = argparse.ArgumentParser(description='编译图形启动器 exe')
    ap.add_argument('--out', default=str(HERE / (EXE_NAME + '.exe')),
                    help='产物 exe 路径')
    args = ap.parse_args()
    out = build(pathlib.Path(args.out))
    print('已生成启动器：%s（%d 字节）' % (out, out.stat().st_size))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
