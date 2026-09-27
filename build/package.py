# -*- coding: utf-8 -*-
"""把 build/pkg 组装成可分发 ZIP（解压即用）

用法： python build/package.py
产物： 学习通巡检工具.zip（在项目根目录）
"""
import pathlib
import sys
import time
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE / 'pkg'
NAME = '学习通巡检工具'          # 解压后的顶层目录名，固定不变
ZIP_NAME = NAME                  # ZIP 文件名，可用命令行参数覆盖
if len(sys.argv) > 1:
    ZIP_NAME = sys.argv[1]
OUT = HERE.parent / (ZIP_NAME + '.zip')

# 绝不能进分发包的目录：登录态 / 报告 / 缓存
EXCLUDE = {'runtime', '输出', '__pycache__', '.pytest_cache'}

# config.json 里跟「本机 + 本账号」绑定的字段，打包前必须清空
SENSITIVE_KEYS = ('cpi', 'chrome_path')


def _scrub_config():
    """清掉 config.json 里的私有字段。

    原来只靠手工保证，太容易漏：只要有人拿 build/pkg 跑过一次扫描，
    cpi（个人空间标识）就会被写进去，然后跟着分发包发给别人。
    """
    import json
    p = SRC / 'app' / 'config.json'
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return []
    hit = [k for k in SENSITIVE_KEYS if d.get(k)]
    if hit:
        for k in hit:
            d[k] = ''
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding='utf-8')
    return hit


def main():
    if not SRC.exists():
        print('找不到 %s，请先执行 build_all.sh' % SRC)
        return 1
    scrubbed = _scrub_config()
    if scrubbed:
        print('已清空 config.json 里的私有字段：%s' % '、'.join(scrubbed))
    if OUT.exists():
        try:
            OUT.unlink()
        except OSError as e:
            # 沙箱 / 杀软可能拦住删除（文件被占用、或回收站操作被中止）。
            # zipfile 以 'w' 打开时本来就会截断覆盖，所以这里直接降级放行。
            print('提示：旧包没能删除（%s），将直接覆盖写入' % type(e).__name__)
    t0 = time.time()
    n = 0
    skipped = []
    try:
        zf = zipfile.ZipFile(OUT, 'w', zipfile.ZIP_DEFLATED, compresslevel=6)
    except PermissionError:
        print('无法写入 %s：文件正被其他程序占用。' % OUT.name)
        print('常见原因：文件预览面板开着、杀毒软件正在扫描这个大家伙、或正在被解压。')
        print('处理办法：关掉占用它的程序后重试，或换个名字：')
        print('    python build/package.py 学习通巡检工具_v1.1')
        return 2
    with zf as z:
        for p in sorted(SRC.rglob('*')):
            rel = p.relative_to(SRC)
            if any(part in EXCLUDE for part in rel.parts):
                if rel.parts[0] not in skipped:
                    skipped.append(rel.parts[0])
                continue
            if p.is_file():
                z.write(p, str(pathlib.Path(NAME) / rel))
                n += 1
    print('已打包 %d 个文件，耗时 %.1f 秒' % (n, time.time() - t0))
    if skipped:
        print('已排除：%s' % '、'.join(skipped))
    print('输出：%s（%.1f MB）' % (OUT, OUT.stat().st_size / 1048576))
    return 0


if __name__ == '__main__':
    sys.exit(main())
