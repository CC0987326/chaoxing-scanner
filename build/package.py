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

# 绝不能进分发包的文件后缀：运行期日志。
# 早先包里自带 Chrome 时，每次在本机跑都会往它目录写 debug.log（Chromium 的
# crashpad 初始化错误），它不含账号信息，但属于「本机运行痕迹」，不该跟着
# 分发包发给别人（实测第一次带内置 Chrome 出包时，就混进了一个 380B 的）。
# v4.2 起包里已无 chrome/，这条留着守其它运行期日志。
EXCLUDE_SUFFIX = {'.log'}

# config.json 里跟「本机 + 本账号」绑定的字段，打包前必须清空
# （llm_key 是用户的 API Key，随包发出去等于把付费接口送人。
#  v3.9 起视觉模型那把单独存 llm_vision_key —— DPAPI 不可用时它会回落成
#  明文留在 config.json 里，所以必须一起清，不能只顾主模型那把）
SENSITIVE_KEYS = ('cpi', 'chrome_path', 'llm_key', 'llm_vision_key')


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


def _check_fresh():
    """防呆：pkg/app 里的源码必须与开发目录一致，否则打出来是旧版坏包。

    真实事故：只跑 package.py 不跑 build_all.sh 的第 4 步（cp 源码），
    pkg 里还是上一个版本的 chaoxing_scanner.py / webui.py，
    240MB 的包发出去才发现新功能根本不在里面。
    """
    import hashlib
    bad = []
    for name in ('chaoxing_scanner.py', 'webui.py', 'captcha_solver.py'):
        dev = HERE.parent / name
        pkg = SRC / 'app' / name
        if not dev.exists():
            continue
        if not pkg.exists():
            bad.append('%s（pkg 里缺失）' % name)
            continue
        h = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
        if h(dev) != h(pkg):
            bad.append('%s（pkg 里是旧版）' % name)
    if bad:
        print('⛔ 打包中止：build/pkg 里的源码不是最新的：')
        for b in bad:
            print('   - ' + b)
        print('先执行同步（build_all.sh 的第 4/5 步），再跑本脚本：')
        print('    cp chaoxing_scanner.py webui.py captcha_solver.py build/pkg/app/')
        print('    python build/mk_extras.py')
        return False
    return True


# 出包前泄漏扫描：文本文件里出现 API Key 形态的字符串就拒绝出包。
# sk- 开头跟一长串字母数字（OpenAI/DeepSeek 等家的 key 都这个形）。
# 限长度防误伤普通单词；扫描的是将要进包的文件（已排除 EXCLUDE 目录）。
import re
# sk- 前**不能是字母数字**（词边界）：pip 自带许可证库 _spdx.py 里的
# 'asterisk-linking-protocols-exception' 词中含子串 sk-linking-protocols-…，
# 旧正则会把它当 key 误报——2026-10-14 出包实测踩到。真 key（sk-abc…、
# sk-proj-…）的 sk- 前面都是引号/空白，不受影响。
_KEY_RE = re.compile(r'(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}')
_TEXT_SUFFIX = {'.py', '.json', '.txt', '.md', '.js', '.html', '.css',
                '.cfg', '.ini', '.csv', '.xml', '.bat', '.sh'}


def _scan_leaks():
    """出包前最后一道闸：扫包内文件，发现敏感内容拒绝出包（退出码 4）。

    查三样：① sk- 形态的 API Key；② config.json 敏感字段没清干净；
    ③ 登录态文件（runtime/answer_cache.json 也一样不该有——runtime
    整个目录在 EXCLUDE 里，出现在这里说明目录结构被动过）。
    """
    problems = []
    app = SRC / 'app'
    cfg = app / 'config.json'
    try:
        import json
        d = json.loads(cfg.read_text(encoding='utf-8'))
        hit = [k for k in SENSITIVE_KEYS if d.get(k)]
        if hit:
            problems.append('config.json 的 %s 不是空的' % '、'.join(hit))
    except FileNotFoundError:
        problems.append('config.json 不存在')
    except Exception as e:
        problems.append('config.json 读不出来（%s）' % type(e).__name__)
    for p in SRC.rglob('*'):
        rel = p.relative_to(SRC)
        # 排除清单管的是「app 自己的运行期产物」（登录态/报告/缓存）：
        # 只看顶层和 app/ 子树。别扫进 python/、chrome/ 这两个第三方运行时 ——
        # pywebview 的依赖 pythonnet 自带 site-packages/pythonnet/runtime/
        # （Python.Runtime.dll，删了桌面窗口起不来），同名的目录是合法依赖，
        # 不是登录态（v4.0 出包时踩过这个假阳性）。
        _top = rel.parts[0] if rel.parts else ''
        if (_top in EXCLUDE
                or (_top == 'app' and any(pt in EXCLUDE for pt in rel.parts))):
            problems.append('包内出现排除目录的内容：%s' % rel)
            continue
        if not p.is_file() or p.suffix.lower() not in _TEXT_SUFFIX:
            continue
        if p.stat().st_size > 4 * 1048576:
            continue
        try:
            text = p.read_text(encoding='utf-8')
        except Exception:
            continue
        m = _KEY_RE.search(text)
        if m:
            problems.append('%s 里发现疑似 API Key（%s…）'
                            % (rel, m.group(0)[:8]))
    if problems:
        print('⛔ 打包中止：出包前扫描发现敏感内容：')
        for b in problems:
            print('   - ' + b)
        return False
    print('出包前扫描通过：无 API Key、config 干净、无登录态/缓存混入。')
    return True


def main():
    if not SRC.exists():
        print('找不到 %s，请先执行 build_all.sh' % SRC)
        return 1
    if not _check_fresh():
        return 3
    scrubbed = _scrub_config()
    if scrubbed:
        print('已清空 config.json 里的私有字段：%s' % '、'.join(scrubbed))
    if not _scan_leaks():
        return 4
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
            # 与 _scan_leaks 同一把尺：排除清单只管顶层与 app/ 子树，
            # 不扫 python/、chrome/ 内部（pythonnet 自带 runtime/ 目录是
            # 合法依赖，误排会让桌面窗口缺 Python.Runtime.dll 起不来）。
            _top = rel.parts[0] if rel.parts else ''
            if (_top in EXCLUDE
                    or (_top == 'app'
                        and any(pt in EXCLUDE for pt in rel.parts))):
                if rel.parts[0] not in skipped:
                    skipped.append(rel.parts[0])
                continue
            # 第三方运行时的字节码缓存（__pycache__/*.pyc）：只要在本机
            # 跑过一次包内的 python，site-packages 下就会生成一堆。它们是
            # 构建/运行痕迹，不该进分发包（实测出 v4.4 时混进了 51 个，把
            # 包内条目数从 654 顶到 705）。这里全树排掉，不限于 app/。
            if '__pycache__' in rel.parts or p.suffix.lower() == '.pyc':
                continue
            if p.is_file():
                if p.suffix.lower() in EXCLUDE_SUFFIX:
                    skipped.append(str(rel))
                    continue
                z.write(p, str(pathlib.Path(NAME) / rel))
                n += 1
    print('已打包 %d 个文件，耗时 %.1f 秒' % (n, time.time() - t0))
    if skipped:
        print('已排除：%s' % '、'.join(skipped))
    print('输出：%s（%.1f MB）' % (OUT, OUT.stat().st_size / 1048576))
    return 0


if __name__ == '__main__':
    sys.exit(main())
