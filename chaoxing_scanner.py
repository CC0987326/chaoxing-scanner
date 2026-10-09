# -*- coding: utf-8 -*-
"""
学习通（超星）未完成事项巡检工具  v1.0
================================================================
把「登录 → 逐门课翻作业/考试/任务点 → 汇总成清单」这套流程固化成本地程序。

    python chaoxing_scanner.py login          首次登录（会弹出浏览器窗口，扫码或短信都行）
    python chaoxing_scanner.py login --auto   全自动短信登录（需 config 里 auto_captcha=true；
                                              无头运行，出现点选验证码会直接停下并提示）
    python chaoxing_scanner.py scan           扫描全部课程并生成清单
    python chaoxing_scanner.py scan --limit 3 只扫前 3 门课（自检用）
    python chaoxing_scanner.py doctor         检查运行环境
    python chaoxing_scanner.py selftest       检测学习通是否改版（逐层验证解析规则）
    python chaoxing_scanner.py gui            打开本地网页控制台

依赖：playwright（浏览器驱动）、ddddocr（可选，用于自动过点选验证码）
本工具只读取数据，不会提交任何作业、不会修改任何课程内容。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- 控制台编码
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8')  # type: ignore[union-attr]
    except Exception:
        pass

# ---------------------------------------------------------------- 路径常量
HERE = Path(__file__).resolve().parent

# 便携版（Python embeddable）里存在 ._pth 时，解释器会忽略「脚本所在目录」，
# 导致 import webui / captcha_solver 全部失败（双击 bat 后界面起不来）。
# 这里显式把本目录补回 sys.path。
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

RUNTIME = HERE / 'runtime'
PROFILE_DIR = RUNTIME / 'profile'
STATE_FILE = RUNTIME / 'state.json'
# 这份会话「属于哪个账号」的本地记录。和 profile / state.json 同生命周期：
# 换号归档、回滚都要一起搬，否则记录会和真实会话对不上。
# 存在的理由：以前工具只知道「有没有登录」，不知道「登录的是谁」——
# 用户填了 A 账号的密码，只要本机还躺着 B 账号的有效会话就直接复用 B，
# 于是「用 A 登录，进去是 B」。见 identity 相关函数。
IDENTITY_FILE = RUNTIME / 'identity.json'
# 大模型答案的本地缓存（按题干哈希复用，省重复问模型的钱）。
# 放 runtime/ 里——打包流程本来就排除 runtime，天然不会带进分发包。
ANSWER_CACHE_FILE = RUNTIME / 'answer_cache.json'
CHROME_DIR = RUNTIME / 'chrome'
SHOT_DIR = RUNTIME / 'screenshots'
LOG_FILE = RUNTIME / 'run.log'
CONFIG_FILE = HERE / 'config.json'
OUT_DIR = HERE / '输出'
SNAPSHOT_DIR = OUT_DIR / '记录'

BASE_URL = 'https://i.chaoxing.com/base?ws=1'
COURSE_PAGE = ('https://mooc1.chaoxing.com/visit/stucoursemiddle'
               '?courseid={cid}&clazzid={clsid}&cpi={cpi}&ismooc2=1&v=2')
EXAM_LIST = ('https://mooc1.chaoxing.com/exam-ans/mooc2/exam/exam-list'
             '?courseid={cid}&clazzid={clsid}&cpi={cpi}&ut=s&t={ts}')
LOGIN_URL = 'https://passport2.chaoxing.com/login?fid=12'

# 版本号：界面侧栏、启动日志、使用说明都引它，别再各处手写一份
VERSION = 'v3.9.6'

# 平台自身算「已完成」的状态；除此之外都视为未完成
DONE_STATES = ('已完成', '已互评', '待批阅', '已提交', '待互评', '已结束', '已过期')

DEFAULT_CONFIG = {
    'chrome_path': '',      # 留空 = 自动探测 / 自动下载
    # 有头模式的行为与真人无差；无头 Chrome 有一堆可检测特征（plugins 为空、
    # 字体/编解码异常等），风控角度默认有头更安全。扫描时窗口会弹到前台，
    # 想后台跑再在 config.json 里手动改回 true。
    'headless': False,
    # 每门课之间的间隔秒数。真人不会 1 秒切一门课；3 秒起步是风控底线，
    # 想更稳可自己调到 5。全量 36 门课约多用 1 分钟。
    # v3.3 起这个值是真会生效的：正常扫描就按它走，撞到风控迹象
    # （页面未加载 / 已判定风险）时在它基础上再放慢一倍。
    # 在此之前正常路径被回 min(base, 0.6) 夹死成固定 0.6 秒，
    # 这颗旋钮是空的——使用说明却写着「至少隔 3 秒」。
    'course_delay': 3.0,
    'page_wait': 3.0,       # 页面加载后的额外等待秒数
    'max_courses': 0,       # 0 = 全部
    'open_report': True,    # 生成后自动打开报告
    'cpi': '',              # 学习通个人参数，留空 = 自动获取
    'user_agent': '',       # 留空 = 自动按内核版本生成
    'llm_platform': '',     # 模型平台（deepseek / openai / qwen / … / custom）
    'llm_url': '',          # 大模型接口地址（OpenAI 兼容，填 base 或完整地址都行）
    # v3.8 起 API Key 默认加密存放在 runtime/llm_key.dpapi（Windows DPAPI，
    # 按当前用户托管，文件拷到别的机器/用户下解不开），这里只在加密不可用时
    # （非 Windows / DPAPI 故障）才留明文兜底。
    'llm_key': '',
    'llm_model': '',        # 模型名（如 gpt-4o-mini / deepseek-chat）
    # ---- 视觉模型（可选）：文本模型看不了图，公式题 / 图表题的题面是图片或
    # MathML，纯文本提取拿到的往往是空白或乱码，得让能看到图的模型「亲眼看」。
    # 四项全空 = 不启用视觉，行为与之前完全一致（纯文本答题）。
    # Key 同样走 DPAPI，存在 runtime/llm_vision_key.dpapi，与主模型那把互不覆盖。
    'llm_vision_platform': '',
    'llm_vision_url': '',
    'llm_vision_key': '',
    'llm_vision_model': '',
    # 点选验证码默认留给用户手点：弹验证码本身就说明已被风控关注，
    # OCR 识别率有限，点错重试反而加重标记。确要自动识别才改 true。
    'auto_captcha': False,
    # 刷课倍速：界面可选 1.0 / 1.25 / 1.5 / 2.0（默认 1.25）。
    # 这四个都是播放器官方档位——v3.3 曾用 ±17% 的任意值（如 1.24/1.76），
    # 平台会把非法档位拨回去、工具再拨回来，两边打架就表现为播放中频繁的
    # 约 1 秒小暂停。实际播放会在所选档位和相邻官方档位之间随机取挡。
    'brush_rate': 1.25,
    'brush_rest_every': 6,     # 每连刷 6 个视频歇一次（0 = 不歇）
    'brush_rest_seconds': 30,  # 歇多久（±40% 随机），模拟人离开一下
    'brush_retry': 1,          # 单个视频卡住/超时后原地重试的次数（0 = 不重试）
    'brush_minimize': True,    # 开刷后把浏览器窗口最小化到任务栏（不挡屏幕，不影响播放）
}

# ================================================================ 日志
_SINKS: list = []


def add_sink(fn):
    """注册日志接收器（网页控制台用）"""
    _SINKS.append(fn)


def log(msg: str = '', level: str = 'info'):
    line = '%s  %s' % (datetime.now().strftime('%H:%M:%S'), msg)
    print(line, flush=True)
    for fn in list(_SINKS):
        try:
            fn(line, level)
        except Exception:
            pass
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def banner(title: str):
    log('')
    log('─' * 62)
    log('  ' + title)
    log('─' * 62)


# ================================================================ 防休眠
_ES_CONTINUOUS = 0x80000000     # 恢复由系统管理（调用即清除本次持有）
_ES_SYSTEM_REQUIRED = 0x00000001  # 阻止系统空闲睡眠 / 休眠（屏幕仍可关）


def keep_awake(on: bool):
    """任务运行期间阻止 Windows 自动睡眠（v2.11.4）。

    只压住「系统空闲睡眠」，**不锁屏幕**——屏幕到点照常关闭，用户合盖前
    记得别选「合盖=睡眠」的电源动作即可。任务线程结束（含异常）时必须
    以 keep_awake(False) 恢复。非 Windows 平台直接忽略。
    注意 SetThreadExecutionState 是按线程持有的，必须在同一线程内开关。

    v2.11.5：开关都写日志——用户报告过「空闲时也不让睡」，日志化后
    谁在什么时候持有、持有多久，一眼可查。
    """
    if os.name != 'nt':
        return
    try:
        import ctypes
        flags = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if on else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
        log('防睡眠：%s' % ('开（任务运行中，屏幕仍可正常关闭）' if on else '关（已恢复系统自主睡眠）'))
    except Exception:
        pass  # 防休眠失败不影响任务本身，只会在电源计划到点时睡掉


# ================================================================ 配置
# 配置文件的「读—改—写」写侧串行锁。
# save_config 已经做到「原子替换 + 临时名带线程 id」，两个并发写不会互相
# 写坏文件；但 patch_config 是「先读、合并、再写」，两个 patch 撞在一起时
# 会各自读到同一份旧值，后写的把先写的改动整个盖掉（丢更新）——真实路径
# 就是「界面点保存模型信息」与「刷课线程给 brush_rate 落盘」同时发生。
# 用 RLock（可重入）：patch_config 内部要调 load_config，而首次建配置时
# load_config 又会调 save_config，同线程重入同一把锁不能死锁。
_CONFIG_LOCK = threading.RLock()


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if not CONFIG_FILE.exists():
        save_config(cfg)
        return cfg
    # 读文件先容忍「瞬时不可读」：Windows 上 os.replace 进行中时，
    # 并发读方会拿到 PermissionError（Python 开文件不带 FILE_SHARE_DELETE）。
    # 这种情况重试就行；当成解析失败会把好端端的 config 归档成 .bad。
    txt = None
    perm_err = None
    for _ in range(5):
        try:
            txt = CONFIG_FILE.read_text(encoding='utf-8')
            break
        except PermissionError as e:
            perm_err = e
            time.sleep(0.05)
        except OSError:
            break
    if txt is None:
        if perm_err:
            log('config.json 暂时不可读（正在被并发写入？），'
                '本次先用默认值：%s' % perm_err, 'warn')
        return cfg
    try:
        cfg.update(json.loads(txt))
    except Exception as e:
        # 解析失败绝不能「静默用默认」就完事：任务线程随后 save_config
        # 会用不含 llm 键的默认 cfg 覆盖写回，用户保存的大模型信息
        # 就这么被抹掉（实测：查询中保存的模型信息莫名消失）。把坏
        # 文件留档，方便排查是并发写坏还是手改坏了。
        bak = CONFIG_FILE.with_suffix('.json.bad')
        try:
            CONFIG_FILE.replace(bak)
            log('config.json 解析失败（%s），已把原文件留档到 %s，'
                '用默认值继续。' % (e, bak.name), 'warn')
        except Exception:
            log('配置文件解析失败，用默认值：%s' % e, 'warn')
    return cfg


def save_config(cfg: dict):
    # 原子写：先写临时文件再替换。直接截断写 config.json 时，并发的
    # load_config 会读到半截 JSON → 解析失败 → 默认值覆盖写回 →
    # 用户保存的大模型信息丢失（v2.11.2 前的实测丢失路径）。
    # 临时名带线程 id：界面保存与任务线程同时落盘也不会互抢同一文件；
    # 再加一道写侧串行锁（_CONFIG_LOCK）——光靠「不撞同一临时文件」还挡不住
    # patch_config 的丢更新（两边各读一份旧值，后写的盖掉先写的）。
    with _CONFIG_LOCK:
        tmp = CONFIG_FILE.with_suffix('.json.tmp%d' % threading.get_ident())
        tmp.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
        # Windows 上目标文件被并发读句柄攥着时，os.replace 会瞬时
        # PermissionError（WinError 5）。短暂重试即可成功；不重试的话，
        # 扫描中途的检查点存盘失败会以异常中止整个任务（实测复现）。
        last = None
        for _ in range(10):
            try:
                os.replace(tmp, CONFIG_FILE)
                return
            except PermissionError as e:
                last = e
                time.sleep(0.06)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise last


def patch_config(patch: dict) -> bool:
    """把指定字段「合并」进磁盘上的配置，而不是整体覆盖。

    为什么不能直接 `save_config(cfg)`：cfg 常常是调用方自己拼的 dict
    （脚本、测试、界面某段只管一个字段的流程），键不全。整体落盘就等于
    把用户其余设置全部抹掉。2026-10-08 真实踩过：scan() 为了缓存 cpi
    把自己的 cfg 存盘，把 config.json 冲成 5 个键，用户的模型信息和
    刷课参数全丢。

    改完返回是否真的落盘（值没变就不写，省一次磁盘抖动）。

    整个「读—改—写」在 _CONFIG_LOCK 里串行：读到的 cur 必须一路用到写回，
    中途放别的线程进来改同一个文件，就退化成「各拿一份旧值、互相盖掉」。
    """
    with _CONFIG_LOCK:
        try:
            cur = load_config()
        except Exception:
            cur = dict(DEFAULT_CONFIG)
        changed = {k: v for k, v in (patch or {}).items() if cur.get(k) != v}
        if not changed:
            return False
        cur.update(changed)
        save_config(cur)
        return True


# ================================================================ 浏览器内核
CFT_INDEX = ('https://googlechromelabs.github.io/chrome-for-testing/'
             'last-known-good-versions-with-downloads.json')


def _chrome_candidates(cfg):
    """按优先级列出候选内核路径"""
    named = []
    if cfg.get('chrome_path'):
        named.append(Path(cfg['chrome_path']))
    named += [
        # 免安装包内自带内核（包根/chrome/chrome-win64/chrome.exe）
        HERE.parent / 'chrome' / 'chrome-win64' / 'chrome.exe',
        HERE / 'chrome' / 'chrome-win64' / 'chrome.exe',
        CHROME_DIR / 'chrome-win64' / 'chrome.exe',
        CHROME_DIR / 'chrome.exe',
        Path.home() / '.workbuddy' / 'tmp' / 'browser' / 'chrome-win64' / 'chrome.exe',
        Path(os.environ.get('LOCALAPPDATA', 'C:/')) / 'Google/Chrome/Application/chrome.exe',
        Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),
        Path('C:/Program Files (x86)/Google/Chrome/Application/chrome.exe'),
    ]
    for p in named:
        try:
            if p and p.exists():
                return p
        except Exception:
            continue
    return None


def download_chrome() -> Path:
    """自动下载 Chrome for Testing（约 150MB，只在首次运行时做一次）"""
    RUNTIME.mkdir(parents=True, exist_ok=True)
    CHROME_DIR.mkdir(parents=True, exist_ok=True)
    banner('首次运行：下载浏览器内核 Chrome for Testing')
    log('这一步只需做一次，之后内核会留在 runtime/chrome 里复用。')
    req = urllib.request.Request(CFT_INDEX, headers={'User-Agent': 'Mozilla/5.0'})
    meta = json.loads(urllib.request.urlopen(req, timeout=60).read().decode('utf-8'))
    url = ''
    ver = ''
    for ch in ('Stable', 'Beta'):
        node = meta.get('channels', {}).get(ch, {})
        for d in node.get('downloads', {}).get('chrome', []):
            if d.get('platform') == 'win64':
                url, ver = d['url'], node.get('version', '')
                break
        if url:
            break
    if not url:
        raise RuntimeError('没找到可用的下载地址，请检查网络')

    zip_path = CHROME_DIR / 'chrome-win64.zip'
    log('版本 %s' % ver)
    log('下载中：%s' % url)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=600) as r, open(zip_path, 'wb') as f:
        total = int(r.headers.get('Content-Length') or 0)
        got = 0
        last = -1
        while True:
            buf = r.read(262144)
            if not buf:
                break
            f.write(buf)
            got += len(buf)
            if total:
                pct = got * 100 // total
                if pct >= last + 5:
                    last = pct
                    log('  … %d%%（%.1f MB / %.1f MB）' % (pct, got / 1048576, total / 1048576))
    log('解压中 …')
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(CHROME_DIR)
    try:
        zip_path.unlink()
    except Exception:
        pass
    exe = CHROME_DIR / 'chrome-win64' / 'chrome.exe'
    if not exe.exists():
        raise RuntimeError('解压后找不到 chrome.exe')
    log('内核就绪：%s' % exe)
    return exe


def browser_attempts(cfg):
    """返回按优先级排序的启动方案 [(kind, value)]，失败自动换下一个"""
    out = []
    exe = _chrome_candidates(cfg)
    if exe:
        out.append(('exe', str(exe)))
    # 兜底：让 Playwright 自己去找系统里的 Chrome / Edge
    out.append(('channel', 'chrome'))
    out.append(('channel', 'msedge'))
    return out


def _exe_version(path) -> str:
    """从 EXE 的版本资源里读出真实版本号（Windows）"""
    try:
        import ctypes
        from ctypes import wintypes

        size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return ''
        buf = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, buf):
            return ''
        ptr = ctypes.c_void_p()
        length = wintypes.UINT()
        if not ctypes.windll.version.VerQueryValueW(
                buf, '\\', ctypes.byref(ptr), ctypes.byref(length)):
            return ''

        class FIXED(ctypes.Structure):
            _fields_ = [('sig', wintypes.DWORD), ('struc', wintypes.DWORD),
                        ('ms', wintypes.DWORD), ('ls', wintypes.DWORD),
                        ('pms', wintypes.DWORD), ('pls', wintypes.DWORD),
                        ('flagsMask', wintypes.DWORD), ('flags', wintypes.DWORD),
                        ('os', wintypes.DWORD), ('type', wintypes.DWORD),
                        ('subtype', wintypes.DWORD), ('dateMS', wintypes.DWORD),
                        ('dateLS', wintypes.DWORD)]

        f = ctypes.cast(ptr, ctypes.POINTER(FIXED)).contents
        ms, ls = f.pms or f.ms, f.pls or f.ls
        return '%d.%d.%d.%d' % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
    except Exception:
        return ''


def fake_user_agent(cfg, exe=None) -> str:
    """把 UA 里的 HeadlessChrome 换成正常 Chrome，降低被风控识别的概率"""
    if cfg.get('user_agent'):
        return cfg['user_agent']
    ver = _exe_version(exe) if exe else ''
    major = (ver.split('.')[0] if ver else '') or '131'
    return ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/%s.0.0.0 Safari/537.36' % major)



# ================================================================ 浏览器会话
def launch(cfg, headless=None, offscreen=False, auto_download=True):
    """启动浏览器，返回 BrowserContext（带独立配置目录，登录态可持久化）"""
    from playwright.sync_api import sync_playwright  # noqa

    if auto_download and _chrome_candidates(cfg) is None:
        try:
            download_chrome()
        except Exception as e:
            log('自动下载失败（将尝试使用系统已装的 Chrome）：%s' % e, 'warn')

    hl = cfg.get('headless', False) if headless is None else headless
    args = [
        '--no-sandbox',
        '--disable-dev-shm-usage',
        '--disable-blink-features=AutomationControlled',
        '--disable-features=Translate,BackForwardCache',
        '--no-first-run',
        '--no-default-browser-check',
        # 刷课时窗口会最小化到任务栏（brush_minimize，默认开）：这三个开关
        # 保证最小化/被遮挡后计时器与播放不被 Chromium 后台节流，心跳照常
        '--disable-background-timer-throttling',
        '--disable-backgrounding-occluded-windows',
        '--disable-renderer-backgrounding',
    ]
    if offscreen:
        args += ['--window-position=-32000,-32000', '--window-size=1440,900']

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    # viewport 每次启动随机抽一个常见分辨率：固定 1440x900 会让「同一工具
    # 的所有账号」在平台侧呈现一模一样的画布尺寸，反而方便划成同一团伙
    vw, vh = random.choice([(1440, 900), (1536, 864), (1366, 768),
                            (1600, 900), (1920, 969)])
    ua = fake_user_agent(cfg, _chrome_candidates(cfg))
    errors = []
    pw = sync_playwright().start()
    for kind, val in browser_attempts(cfg):
        opts = dict(
            user_data_dir=str(PROFILE_DIR),
            headless=hl,
            args=args,
            viewport={'width': vw, 'height': vh},
            locale='zh-CN',
            timezone_id='Asia/Shanghai',
            ignore_https_errors=True,
            user_agent=ua,
        )
        if kind == 'exe':
            opts['executable_path'] = val
        else:
            opts['channel'] = val
        try:
            ctx = pw.chromium.launch_persistent_context(**opts)
            ctx.set_default_timeout(30000)
            # 抹掉最容易被风控识别的自动化特征。补丁对每个 frame 生效；
            # 全部是「与真实浏览器对齐」的防御性补丁，缺什么补什么，
            # 真实值存在时不动（伪装成固定值反而克隆化）。
            ctx.add_init_script(r"""
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
// window.chrome.runtime：部分检测脚本查它在不在；真实 Chrome 自带，
// 只在缺失时补一个无功能的占位，headful 下基本是 no-op
try {
  if (!window.chrome) window.chrome = {};
  if (!window.chrome.runtime) {
    window.chrome.runtime = {
      connect: function () {}, sendMessage: function () {},
      PlatformOs: {MAC: 'mac', WIN: 'win', ANDROID: 'android',
                   CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd'}
    };
  }
} catch (e) {}
// permissions.query 对 notifications 的返回要与 Notification.permission 一致
try {
  if (window.Notification && navigator.permissions
      && navigator.permissions.query) {
    const _q = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (p) => (
      p && p.name === 'notifications'
        ? Promise.resolve({state: Notification.permission})
        : _q(p));
  }
} catch (e) {}
""")
            log('浏览器已启动（%s%s，%s）' % (
                '无头' if hl else '有窗口', '·屏幕外' if offscreen else '',
                Path(val).name if kind == 'exe' else val))
            ctx._wb_pw = pw  # 方便一起关闭
            return ctx
        except Exception as e:
            first = str(e).strip().splitlines()[0][:150] if str(e).strip() else e.__class__.__name__
            errors.append('%s(%s) → %s' % (kind, val, first))
    try:
        pw.stop()
    except Exception:
        pass
    raise RuntimeError('所有浏览器内核都无法启动：\n    ' + '\n    '.join(errors))


def minimize_window(page):
    """把浏览器窗口最小化到任务栏（CDP，只动工具自己的窗口）。

    刷视频全程不需要窗口可见：播放器照常走心跳，进度照常记录，
    只是别把用户屏幕挡住。无头模式没有窗口，会静默跳过。
    """
    try:
        cdp = page.context.new_cdp_session(page)
        wid = cdp.send('Browser.getWindowForTarget')['windowId']
        cdp.send('Browser.setWindowBounds',
                 {'windowId': wid, 'bounds': {'windowState': 'minimized'}})
        return True
    except Exception as e:
        log('最小化浏览器窗口失败（不影响刷课）：%s' % e, 'warn')
        return False


def close_ctx(ctx):
    # pw.stop 必须独立于 ctx.close 的成败：ctx.close 抛异常就跳过 stop
    # 的话，playwright driver 进程会留下来（泄漏，实测内存里挂一堆 node）
    pw = getattr(ctx, '_wb_pw', None)
    try:
        ctx.close()
    except Exception:
        pass
    if pw:
        try:
            pw.stop()
        except Exception:
            pass


def mask_phone(p) -> str:
    """手机号脱敏：138****8888。日志里不出现完整号码。"""
    p = re.sub(r'\D', '', str(p or ''))
    if len(p) < 7:
        return p or '（未知）'
    return p[:3] + '****' + p[-4:]


def save_identity(phone='', via='unknown'):
    """记下「当前这份会话是谁的」。

    via: password（账号密码登录）/ qr（扫码或短信）/ unknown。
    扫码登录拿不到账号名 → phone 存空串，代表「来源未知」。
    未知不等于「就是你要的账号」，所以下次用户填密码时不会被误当成
    同一个号复用（见 do_login_password）。
    """
    RUNTIME.mkdir(parents=True, exist_ok=True)
    try:
        IDENTITY_FILE.write_text(json.dumps({
            'phone': re.sub(r'\D', '', str(phone or '')),
            'via': via,
            'at': time.strftime('%Y-%m-%d %H:%M:%S'),
        }, ensure_ascii=False, indent=1), encoding='utf-8')
    except Exception as e:
        log('会话身份记录写入失败：%s' % e, 'warn')


def load_identity() -> dict:
    """读回「当前这份会话是谁的」。读不到/损坏都返回空 dict。"""
    try:
        if not IDENTITY_FILE.exists():
            return {}
        d = json.loads(IDENTITY_FILE.read_text(encoding='utf-8'))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def drop_identity():
    try:
        if IDENTITY_FILE.exists():
            IDENTITY_FILE.unlink()
    except Exception:
        pass


def session_label() -> str:
    """当前会话身份的展示文案，给日志和界面用。"""
    d = load_identity()
    ph, via = d.get('phone') or '', d.get('via') or ''
    if ph:
        return '账号 %s' % mask_phone(ph)
    if via == 'qr':
        return '扫码/短信登录的账号（本机没记下具体是哪个）'
    return '来源未知的会话'


def restore_state(ctx) -> bool:
    """把上次保存的 cookie 注入会话"""
    if not STATE_FILE.exists():
        return False
    try:
        data = json.loads(STATE_FILE.read_text(encoding='utf-8'))
        ck = data.get('cookies') or []
        if not ck:
            return False
        ctx.add_cookies(ck)
        # 身份一并报出来。这里是一大批「静默复用会话」的必经之路，在此处
        # 说清楚「这份会话是谁的」，用户才可能第一时间发现「用的不是我要
        # 的账号」——而不是等报告出来、发现课程对不上才反应过来。
        log('已载入上次保存的登录会话（%d 条 cookie）｜%s'
            % (len(ck), session_label()))
        return True
    except Exception as e:
        log('会话恢复失败：%s' % e, 'warn')
        return False


def save_state(ctx):
    RUNTIME.mkdir(parents=True, exist_ok=True)
    try:
        ctx.storage_state(path=str(STATE_FILE))
        log('登录会话已保存 → %s' % STATE_FILE)
    except Exception as e:
        log('会话保存失败：%s' % e, 'warn')


def fresh_profile():
    """换号登录前清场：归档浏览器 profile 与登录会话，新账号从零开始。

    为什么：多个账号轮流共用同一个浏览器 profile（同一个 cookie 罐）时，
    平台侧看到的是「同一罐 cookie 里先后出现多个不同 uid」，账号之间
    会被关联分析划成同一团伙。每次换号换新罐，账号间在 cookie 层面
    不再共享任何痕迹。

    做法：profile 目录整体改名归档（同盘 rename，瞬时完成），state.json
    一起挪进去。只保留最近一份归档；改名失败（多半有浏览器进程占用）
    就什么都不动并返回 False，由调用方提示。
    """
    old = PROFILE_DIR.with_name('profile_old')
    try:
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
    except Exception:
        pass
    if not PROFILE_DIR.exists():
        # 本来就没有 profile（首次使用），只需清掉旧会话文件
        try:
            if STATE_FILE.exists():
                STATE_FILE.unlink()
        except Exception:
            pass
        drop_identity()
        return True
    try:
        PROFILE_DIR.rename(old)
    except Exception as e:
        log('归档旧浏览器痕迹失败（可能有浏览器窗口还开着）：%s' % str(e)[:80],
            'warn')
        log('请关闭工具重开后再点登录，否则新旧账号会共用同一个浏览器痕迹。',
            'warn')
        return False
    try:
        if STATE_FILE.exists():
            shutil.move(str(STATE_FILE), str(old / 'state.json'))
    except Exception:
        pass
    try:
        if IDENTITY_FILE.exists():
            shutil.move(str(IDENTITY_FILE), str(old / 'identity.json'))
    except Exception:
        pass
    log('已归档旧账号的浏览器痕迹（runtime/profile_old），本次登录使用全新会话。')
    return True


def restore_profile():
    """fresh_profile() 的逆操作：把归档的浏览器痕迹和会话挪回原位。

    用在新登录没成功的时候。否则用户只是点一下登录、或验证码没点完，
    原来的登录态就被归档走了，下次查询还得重登一遍（反而更容易反复
    撞验证码）——这属于「换个号把老号也搭进去」，必须能回滚。

    调用前要先关掉浏览器（profile 目录被占用时 rename 会失败）。
    """
    old = PROFILE_DIR.with_name('profile_old')
    if not old.exists():
        return False
    try:
        if PROFILE_DIR.exists():
            shutil.rmtree(PROFILE_DIR, ignore_errors=True)
        old.rename(PROFILE_DIR)
    except Exception as e:
        log('回滚旧浏览器痕迹失败：%s' % str(e)[:80], 'warn')
        log('旧痕迹还在 runtime/profile_old，可手动改名回 runtime/profile。',
            'warn')
        return False
    try:
        if (PROFILE_DIR / 'state.json').exists():
            shutil.move(str(PROFILE_DIR / 'state.json'), str(STATE_FILE))
    except Exception:
        pass
    try:
        if (PROFILE_DIR / 'identity.json').exists():
            shutil.move(str(PROFILE_DIR / 'identity.json'), str(IDENTITY_FILE))
    except Exception:
        pass
    log('登录没有成功，已把上一个账号的浏览器痕迹与会话挪回原位。')
    return True


# ================================================================ 页面判定
# ---------------------------------------------------------------- 按需等待
# 学习通这些页面都是「先返回骨架、再异步填内容」：domcontentloaded 之后正文
# 还要等一会儿才出现。改前一律用固定 sleep 兜住（3 秒 / 2.6 秒 / 350ms …），
# 于是页面明明 0.5 秒就绪也要空等满 3 秒 —— 几十门课累积下来就是好几分钟的
# 纯等待，用户感知就是「查得慢」。
#
# 这里统一改成按条件轮询，两个要点保证「抓到的内容一个字都不差」：
#   1. 判定条件是「数据到手了没」（条目出现 / 输入框出现 / 渲染条数变多），
#      不是「页面能不能打开」；
#   2. 等待上限与改前的固定 sleep 取同一个值 —— 平台慢时行为与改前完全一致，
#      用户把 page_wait 调大也照样生效，只是不再无谓地等满。
_POLL_MS = 120


def _poll(page, expr, ok, timeout_ms, interval_ms=_POLL_MS):
    """反复求值 expr，直到 ok(值) 为真或超时；返回最后一次的值。

    超时不抛异常，而是把「最后看到的东西」交回给调用方 —— 调用方原有的兜底
    分支（读不到就当无此模块 / 空列表）因此一行都不用改。
    """
    deadline = time.time() + max(0, timeout_ms) / 1000.0
    val = None
    while True:
        try:
            val = page.evaluate(expr)
        except Exception:
            val = None
        try:
            if ok(val):
                return val
        except Exception:
            pass
        if time.time() >= deadline:
            return val
        try:
            # 轮询间隔带随机抖动：固定节奏的请求间隔是机器特征
            page.wait_for_timeout(int(interval_ms * random.uniform(0.7, 1.6)))
        except Exception:
            return val


def _grown(n):
    """计数类条件的快捷写法：等到比 n 大为止"""
    return lambda v: (v or 0) > n


def _settle(page, expr, quiet_ms=260, cap_ms=1200, interval_ms=_POLL_MS):
    """等到 expr 的取值「连续 quiet_ms 不再变化」，或到 cap_ms 为止。

    用于滚动到底后的收尾：还有卡片没渲染完就继续等，都渲染完就立刻走。
    """
    deadline = time.time() + cap_ms / 1000.0
    last, stable_since = object(), time.time()
    while True:
        try:
            cur = page.evaluate(expr)
        except Exception:
            cur = None
        if cur != last:
            last, stable_since = cur, time.time()
        elif (time.time() - stable_since) * 1000 >= quiet_ms:
            return cur
        if time.time() >= deadline:
            return cur
        try:
            page.wait_for_timeout(int(interval_ms * random.uniform(0.7, 1.6)))
        except Exception:
            return cur


# 页面健康检查：导航没渲染出来时，分清「被风控/掉登录」还是单纯加载慢。
# v2.5 的教训：课程页没打开时把课报成「无作业模块」，用户拿着假报告以为没作业。
JS_PAGE_HEALTH = r"""
() => {
  const navs = [...document.querySelectorAll('li[dataname]')]
        .map(x => x.getAttribute('dataname'));
  const url = location.href;
  const body = (document.body ? document.body.innerText : '').replace(/\s+/g, '');
  let why = '';
  if (/passport|login/i.test(url)) why = '被重定向到登录页，登录态可能已失效';
  else if (/安全验证|验证码|访问异常|过于频繁|请稍后再试/.test(body)) why = '触发平台安全验证';
  else if (!navs.length) why = '页面没有渲染出课程导航';
  return { navs: navs, why: why };
}
"""

# 课程页左侧导航（作业/考试/章节的唯一入口）是否已经渲染出来
JS_HAS_NAV = r"""
() => document.querySelectorAll('li[dataname]').length > 0
"""

# 登录页表单（手机号框 / 手机号输入框 / 登录方式切换字样）是否已经渲染出来
JS_HAS_LOGIN_FORM = r"""
() => !!(document.querySelector('#phone') || document.querySelector('input[type=tel]')
          || /验证码登录|密码登录/.test(document.body ? document.body.innerText : ''))
"""

# 账号密码登录表单里的手机号输入框
JS_HAS_PHONE = r"""
() => !!document.querySelector('#phone')
"""

JS_IS_LOGINED = r"""
() => {
  const b = document.body ? (document.body.innerText || '') : '';
  const hasCourse = !!document.querySelector('a[href*=stucoursemiddle]');
  const hasIframe = [...document.querySelectorAll('iframe')]
        .some(f => /visit\/interaction/.test(f.src || ''));
  return { url: location.href, title: document.title,
           ok: hasCourse || hasIframe || /个人空间/.test(b.slice(0, 3000)) };
}
"""


class NotLoggedIn(Exception):
    pass


def _logined_enough(v):
    """check_login 的轮询条件：登录态特征已出现，或已被明确踢到登录页。

    后一种情况要提前收手 —— 都跳登录页了，再等也不会变成登录态。
    """
    if not isinstance(v, dict):
        return False
    u = v.get('url') or ''
    if 'passport' in u or 'login' in u:
        return True
    return bool(v.get('ok'))


def check_login(page, navigate=True) -> bool:
    if navigate:
        try:
            page.goto(BASE_URL, wait_until='domcontentloaded')
        except Exception as e:
            log('打开个人空间失败：%s' % e, 'warn')
            return False
        # 等「个人空间」的特征出现（课程链接 / 互动 iframe / 页面标题）。
        # 上限仍是改前的 3.5 秒，但特征一到就走。
        _poll(page, JS_IS_LOGINED, _logined_enough, 3500)
    try:
        info = page.evaluate(JS_IS_LOGINED)
    except Exception:
        return False
    u = (info.get('url') or '')
    if 'passport' in u or 'login' in u:
        return False
    return bool(info.get('ok')) and 'chaoxing.com' in u


def ensure_login(cfg, ctx, page, interactive=True):
    """确保处于登录态；未登录则按需拉起登录流程。

    这里只判断「有没有登录」，不判断「是哪个账号」——因为走到这里时
    用户没有输入账号（填了账号密码的走 do_login_password）。但必须把
    身份**显式报出来**：以前这里闷声复用本地会话，用户以为在查自己的
    账号，实际查的是上一个登录过的账号（测试号 / 家人的号）。
    """
    if check_login(page):
        log('登录态有效 ✓（%s）' % session_label())
        return
    if restore_state(ctx) and check_login(page):
        log('已用本地会话自动登录 ✓（%s）' % session_label())
        return
    if not interactive:
        raise NotLoggedIn('未登录且无法自动恢复，请先运行 login')
    log('未登录，转入登录流程 …', 'warn')
    close_ctx(ctx)
    do_login(cfg, auto=False)
    raise NotLoggedIn('登录已完成，请重新运行扫描')


# ================================================================ 登录
def do_login(cfg, auto=False, phone='', fresh=False):
    """登录并保存会话。auto=True 时走短信验证码全自动流程

    fresh=True：强制重新登录（界面「扫码 / 短信登录」按钮换账号用）。
    不注入已保存的会话、也不走「原会话仍然有效」的短路——否则用户
    想换账号时一点登录按钮就自动登回旧号，根本没机会扫码（实测踩过）。
    """
    if auto:
        return _login_auto(cfg, phone)

    ctx = launch(cfg, headless=False)
    page = ctx.new_page()
    try:
        if fresh:
            # 只跳过 restore_state 不够：launch 用的是持久化 profile，
            # 旧账号的 cookie 本来就躺在里面，启动即带——不主动清掉，
            # passport 对已登录用户自动跳转时照样「换号失败登回旧号」。
            try:
                ctx.clear_cookies()
                log('本次为重新登录：已忽略并清除旧登录会话，'
                    '请直接扫码或登录新账号。')
            except Exception as e:
                log('清除旧会话失败（仍会尝试忽略旧号）：%s' % str(e)[:80],
                    'warn')
        else:
            restore_state(ctx)
            page.goto(BASE_URL, wait_until='domcontentloaded')
            # 上限仍是 3 秒；已经是登录态就立刻往下走
            _poll(page, JS_IS_LOGINED, _logined_enough, 3000)
            if check_login(page, navigate=False):
                save_state(ctx)
                log('原有会话仍然有效，无需重新登录 ✓')
                log('  这份会话是：%s' % session_label())
                log('  （若这不是你要的账号，请用界面上的「扫码 / 短信登录」换号，'
                    '或填该账号的密码后点「一键查询」）')
                return True

        page.goto(LOGIN_URL, wait_until='domcontentloaded')
        banner('请在弹出的浏览器窗口中登录')
        log('推荐用手机「学习通」APP 扫右侧二维码，一步到位；')
        log('也可以输手机号+密码，或切到「验证码登录」。')
        log('')
        log('点选汉字验证码、短信验证码都由你自己在窗口里完成，')
        log('本工具不会读取你的密码。登录成功后会自动保存会话并关闭窗口。')
        log('')
        log('等待登录中（最多 8 分钟，期间请勿关闭窗口）…')

        deadline = time.time() + 480
        n = 0
        while time.time() < deadline:
            page.wait_for_timeout(2000)
            n += 1
            try:
                if check_login(page, navigate=False):
                    save_state(ctx)
                    # 顺便勾一下「下次自动登录」，让 cookie 更持久
                    try:
                        page.evaluate(
                            "() => { const e=document.querySelector('#retainlogin');"
                            " if(e && !e.checked) e.click(); }")
                    except Exception:
                        pass
                    # 扫码/短信登录拿不到用户输入的账号，只能记成「来源未知」。
                    # 绝不能沿用旧记录（那会让记录撒谎：明明换成了新号，
                    # 记录还写着上一个号，下次密码登录就会误判为「同一个号」
                    # 而复用错会话）。
                    save_identity('', 'qr')
                    log('登录成功 ✓')
                    log('  ⚠ 这次是扫码/短信登录，本工具不知道具体是哪个账号，'
                        '只记「来源未知」。下次若填账号密码查询，会重新登录一次。')
                    return True
            except Exception:
                pass
            if n % 15 == 0:
                log('  仍在等待…（已 %d 秒）' % (n * 2))
        log('等待超时，未检测到登录成功。', 'warn')
        return False
    finally:
        try:
            page.wait_for_timeout(800)
        except Exception:
            pass
        close_ctx(ctx)


def _login_auto(cfg, phone=''):
    """短信验证码 + 自动识别点选验证码的全自动登录

    失败原因同样写进模块级的 _login_fail，界面和 CLI 都能用
    last_login_fail() 问「上一次为什么没登进去」。
    ⚠ 下面那几处赋值**必须**先声明 global，否则只是在函数里新建一个同名
    局部变量，标记永远不会生效（2026-10-08 审查发现：captcha 那一处一直
    是死代码，等于全自动登录撞验证码后外界完全看不到原因）。
    """
    global _login_fail
    _login_fail = ''
    phone = phone or cfg.get('phone') or ''
    if not phone:
        try:
            phone = input('请输入学习通绑定手机号：').strip()
        except EOFError:
            log('非交互环境，无法输入手机号', 'err')
            return False

    ctx = launch(cfg, headless=True)
    page = ctx.new_page()
    try:
        if restore_state(ctx) and check_login(page):
            have = (load_identity().get('phone') or '')
            if have and have == phone:
                log('本机已保存该账号（%s）的会话 ✓' % mask_phone(phone))
                return True
            # 本机躺着的是别的账号（或来源未知）→ 清掉再登，别把新号
            # 登进装着旧号 cookie 的罐里（同上文 do_login_password 的道理）
            log('本机会话不是 %s，先清掉旧会话再登录 …' % mask_phone(phone),
                'warn')
            try:
                ctx.clear_cookies()
                drop_identity()
            except Exception:
                pass

        page.goto(LOGIN_URL, wait_until='domcontentloaded')
        # 等登录表单渲染出来（上限仍是 3 秒）
        _poll(page, JS_HAS_LOGIN_FORM, lambda v: bool(v), 3000)

        # 切到「验证码登录」
        try:
            page.get_by_text('验证码登录', exact=False).first.click(timeout=8000)
            page.wait_for_timeout(2500)
        except Exception:
            log('找不到「验证码登录」入口，改走手动登录', 'warn')
            close_ctx(ctx)
            return do_login(cfg, auto=False)

        ph = page.query_selector('#phone') or page.query_selector('input[type=tel]')
        if not ph:
            log('找不到手机号输入框，改走手动登录', 'warn')
            close_ctx(ctx)
            return do_login(cfg, auto=False)
        ph.click()
        ph.fill(phone)
        page.wait_for_timeout(600)

        # 点「获取验证码」
        clicked = page.evaluate(r"""
        () => {
          const cands = [...document.querySelectorAll('a,span,div,button')];
          const t = cands.find(e => /获取验证码|发送验证码/.test((e.textContent||'').trim())
                                   && (e.textContent||'').trim().length < 12);
          if (t) { t.click(); return true; }
          return false;
        }""")
        if not clicked:
            log('找不到「获取验证码」按钮', 'warn')
        page.wait_for_timeout(3000)

        # 处理点选验证码。这条链路是「全自动短信登录」，浏览器是**无头**的
        # （见本函数开头的 launch(cfg, headless=True)）——没有窗口可以点。
        # 所以 auto_captcha 关掉时不能像有头链路那样「提示用户手点然后干等」：
        # 那是 180 秒纯空转，界面上还写着一句根本没窗口可点的提示。
        # 这里改成识别不了就立刻停下说清楚，让人改走有头链路。
        try:
            from captcha_solver import read_targets, solve_point_captcha
            if read_targets(page):
                if cfg.get('auto_captcha') and solve_point_captcha(
                        page, interactive=False):
                    log('点选验证码已通过 ✓')
                else:
                    log('出现点选验证码。', 'warn')
                    if not cfg.get('auto_captcha'):
                        log('全自动登录默认不做验证码识别'
                            '（config.json 的 auto_captcha 为 false）。', 'warn')
                    else:
                        log('自动识别没能通过。', 'warn')
                    log('全自动短信登录跑在无头浏览器里，没有窗口可人工点选，'
                        '本次到此为止。', 'err')
                    log('请改在图形界面点「扫码 / 短信登录」（会弹出窗口），'
                        '或把 config.json 的 auto_captcha 设为 true。', 'err')
                    _login_fail = 'captcha'
                    return False
        except Exception as e:
            log('验证码模块不可用：%s' % e, 'warn')
        page.wait_for_timeout(3000)

        body = page.evaluate("() => document.body.innerText.replace(/\\s+/g,' ').slice(0,300)")
        if '验证码发送成功' not in body and '重新获取' not in body and '秒' not in body:
            log('页面反馈：%s' % body[:150], 'warn')

        log('短信已发往 %s，请输入收到的 6 位验证码。' % phone)
        try:
            code = input('验证码：').strip()
        except EOFError:
            log('非交互环境，无法输入验证码', 'err')
            return False
        if not code:
            return False

        page.evaluate("""(c) => {
            const e = document.querySelector('#vercode') || document.querySelector('input[type=text]');
            if (!e) return false;
            e.focus(); e.value = c;
            e.dispatchEvent(new Event('input', {bubbles:true}));
            e.dispatchEvent(new Event('change', {bubbles:true}));
            return true;
        }""", code)
        page.wait_for_timeout(600)
        page.evaluate("""() => {
            const b = document.querySelector('#loginBtn');
            if (b) b.click();
        }""")
        page.wait_for_timeout(7000)

        if check_login(page, navigate=False):
            save_state(ctx)
            save_identity(phone, 'sms')
            log('登录成功 ✓（%s）' % mask_phone(phone))
            return True
        page.goto(BASE_URL, wait_until='domcontentloaded')
        page.wait_for_timeout(4000)
        if check_login(page, navigate=False):
            save_state(ctx)
            save_identity(phone, 'sms')
            log('登录成功 ✓（%s）' % mask_phone(phone))
            return True
        log('自动登录未成功（验证码可能已过期）。建议改用手动登录：'
            'python chaoxing_scanner.py login', 'err')
        _login_fail = 'other'
        return False
    finally:
        close_ctx(ctx)


# 密码登录失败的原因标记。界面靠它决定要不要弹「账号或密码错误」——
# 只有平台**明确回绝**这组账号密码时才算，其它失败（验证码没通过、超时、
# 表单没填上）绝不能报成密码错误，否则会把用户引到「去改密码」的错路上去。
#   ''           没失败 / 没走到判定
#   'credential' 平台明确回绝（密码错误 / 账号不存在 / 已锁定）
#   'captcha'    全自动短信登录撞上点选验证码，而它是无头浏览器、
#                没有窗口可人工点选，只能到此为止
#   'other'      其它原因
_login_fail = ''


def last_login_fail() -> str:
    """上一次 do_login_password() 失败的原因，取值见 _login_fail 的注释。"""
    return _login_fail


def do_login_password(cfg, phone, password, headless=False) -> bool:
    """用「手机号 + 密码」登录。

    流程：打开登录页 → 自动填表提交 → 等待结果。
      · 若直接登录成功 → 保存会话
      · 若弹出点选验证码 → 先尝试自动识别，失败则提示你在窗口里点一下
      · 若返回「密码错误」→ 明确告知并建议改用扫码/短信

    headless=False（默认）会显示窗口，便于处理验证码。
    """
    global _login_fail
    _login_fail = ''
    phone = (phone or '').strip()
    if not phone or not password:
        log('缺少账号或密码', 'err')
        _login_fail = 'other'
        return False

    ctx = launch(cfg, headless=headless, offscreen=False)
    page = ctx.new_page()
    swapped = False          # 本次是否归档了旧痕迹、换成了全新的罐
    logged_in = False        # 是否真的登进去了（决定要不要回滚）
    try:
        # 原来这里先 goto 一次个人空间再等 2 秒，但那发生在注入 cookie **之前**，
        # 纯属白等；直接让下面的 check_login 带着 cookie 打开一次就够。
        #
        # 【必须同时看「登录的是谁」】v3.6 及以前这里是无条件复用的：只要本机
        # 有一份还能用的会话就 return True，完全不管用户刚在界面上填的是哪个
        # 账号。于是「填 A 的密码 → 本机躺着 B 的会话 → 静默以 B 的身份跑」，
        # 用户看到的就是「登录的账号跟我输入的不是一个」。这比看上去严重：
        # 之后的刷课、交卷全都会算到 B 头上，而且不可撤销。
        if restore_state(ctx) and check_login(page, navigate=True):
            have = (load_identity().get('phone') or '')
            if have and have == phone:
                log('本机已保存该账号（%s）的登录会话，直接复用 ✓'
                    % mask_phone(phone))
                return True
            if have:
                log('本机保存的会话属于另一个账号（%s），'
                    '改按你输入的 %s 重新登录 …'
                    % (mask_phone(have), mask_phone(phone)), 'warn')
            else:
                log('本机会话没记下是哪个账号（多半是扫码登录留下的），'
                    '不能确认它就是 %s → 按你输入的账号重新登录 …'
                    % mask_phone(phone), 'warn')
            log('（上一个账号的会话会归档到 runtime/profile_old，不会丢）')

        # 走到这说明 state.json 里没有可用会话 → 这是一次「真的要登录」。
        # 换号登录前先换罐：持久化 profile 里可能还躺着上一个账号的 cookie
        # （启动即带），不换罐的话访问登录页时旧 cookie 就随请求发出去了，
        # 新账号登上去也是在「装着别人 cookie 的罐」里。做法与界面上的
        # 「扫码 / 短信登录」按钮一致。归档必须在浏览器开着之前做，
        # 所以这里先关掉再重开一次。
        #
        # （v3.3 前这里是 ctx.clear_cookies()：只在 cookie 层面擦一把，
        #   profile 里的 localStorage / IndexedDB / 缓存还留着；而且它
        #   在「会话只是瞬时判失败」时也会把仍然有效的 cookie 抹掉，
        #   直接把用户踢下线。换成整体归档就没有这两个问题。）
        try:
            close_ctx(ctx)
        except Exception:
            pass
        if not fresh_profile():
            log('旧浏览器痕迹归档失败（可能有浏览器窗口还开着）。', 'err')
            log('请关闭本工具后重新打开，再重新登录一次。', 'err')
            _login_fail = 'other'
            return False
        swapped = True
        ctx = launch(cfg, headless=headless, offscreen=False)
        page = ctx.new_page()

        page.goto(LOGIN_URL, wait_until='domcontentloaded')
        # 等登录表单渲染出来（上限仍是 2.5 秒）
        _poll(page, JS_HAS_PHONE, lambda v: bool(v), 2500)

        try:
            page.click('#phone', timeout=8000)
            page.fill('#phone', phone)
            page.click('#pwd', timeout=8000)
            page.fill('#pwd', password)
            page.wait_for_timeout(500)
            # 尽量勾上「下次自动登录」，让会话更持久
            try:
                page.evaluate("() => { const e=document.querySelector('#retainlogin');"
                              " if(e && !e.checked) e.click(); }")
            except Exception:
                pass
        except Exception as e:
            log('填写登录表单失败：%s' % e, 'err')
            _login_fail = 'other'
            return False

        try:
            page.click('#loginBtn', timeout=8000)
        except Exception:
            page.evaluate("() => { const b=document.querySelector('#loginBtn'); if(b) b.click(); }")
        log('已提交登录，等待结果 …')

        deadline = time.time() + 100
        human_warned = False
        while time.time() < deadline:
            # 0.8 秒探一次：登录成功、或平台回绝密码，都能更早被认出来，
            # 界面上的红字提示 / 开始扫描也就来得更快
            page.wait_for_timeout(800)
            if check_login(page, navigate=False):
                # 快速判定过了还要真导航复核一次：提交后平台有时把页面
                # 跳到「个人空间」游客态，body 含「个人空间」三个字就骗过
                # 了快速判定，存下来的却是只有 10 条 cookie 的废会话
                # （实测：1 秒「登录成功」→ 会话不可用 → 0 门课程）。
                time.sleep(2)
                if check_login(page, navigate=True):
                    save_state(ctx)
                    save_identity(phone, 'password')
                    logged_in = True
                    log('登录成功 ✓（%s）' % mask_phone(phone))
                    return True
                log('  登录还没真正建立，继续等 …', 'warn')
                continue
            # 点选验证码：默认留给用户手点。弹验证码＝平台已在关注这个会话，
            # OCR 识别率有限，点错重试反而加重标记；自动识别只在
            # config.json 里显式 'auto_captcha': true 时才启用（装了 ddddocr 才有）
            try:
                if cfg.get('auto_captcha'):
                    from captcha_solver import read_targets, solve_point_captcha
                    if read_targets(page):
                        if not human_warned:
                            log('出现点选验证码，尝试自动识别 …')
                        if solve_point_captcha(page, interactive=False):
                            log('验证码已通过 ✓')
                            page.wait_for_timeout(3000)
                            continue
                        if not human_warned:
                            human_warned = True
                            log('自动识别未通过 → 请在弹出的浏览器窗口里手动点选完成验证。', 'warn')
                            log('（窗口会保持打开，完成后自动继续）', 'warn')
                            deadline = time.time() + 300   # 给人操作留足时间
                else:
                    from captcha_solver import read_targets
                    if read_targets(page):
                        if not human_warned:
                            human_warned = True
                            log('出现点选验证码 → 请在弹出的浏览器窗口里手动点选完成验证。', 'warn')
                            log('（窗口会保持打开，完成后自动继续）', 'warn')
                            deadline = time.time() + 300   # 给人操作留足时间
            except Exception:
                pass
            # 服务器明确回绝
            try:
                body = page.evaluate(
                    "() => (document.body.innerText||'').replace(/\\s+/g,' ').slice(0,400)")
            except Exception:
                body = ''
            if re.search(r'密码错误|密码有误|账号不存在|用户不存在|账号已被锁定', body):
                log('服务器返回：%s' % body[:140], 'err')
                log('账号或密码不正确。请核对后重试，或改用窗口扫码 / 短信验证码登录。', 'err')
                _login_fail = 'credential'
                return False

        log('登录未在预期时间内完成（浏览器窗口已关闭：它占着本机的浏览器痕迹'
            '目录，留着会让紧接着的下一次查询起不来）。要重试请再点一次登录。',
            'warn')
        _login_fail = 'other'
        return False
    finally:
        # 无论成败都要关掉这个浏览器。
        # 它用的是**持久化** profile（runtime/profile）：留着不关，那个目录
        # 就一直被占着，而同一个 user_data_dir 再启动一个持久化上下文会直接
        # 失败（TargetClosedError，实测复现）。紧接着的「一键查询」正是拿这
        # 个目录再开一次 —— 于是「刚用账号密码登录成功，紧接着就查不了」。
        # 以前这里按 keep_open 跳过关闭（出现过点选验证码就把 keep_open 置
        # True，想「把窗口留给用户点」），但那个变量从来起不到这个作用：
        # 函数只要还没返回，窗口本来就是开着的，等人点验证码的那几分钟完全
        # 够用；它唯一的效果就是每次带验证码的登录都漏一个浏览器进程。
        close_ctx(ctx)
        # 换了新罐又没登成 → 把旧账号的痕迹与会话挪回原位，
        # 别让一次没成的登录把原来的登录态也搭进去
        if swapped and not logged_in:
            restore_profile()


# ================================================================ 取课程
JS_COURSES = r"""
() => {
  const out = [];
  document.querySelectorAll('a[href*=stucoursemiddle]').forEach(a => {
    const h = a.getAttribute('href') || '';
    const m = h.match(/courseid=(\d+)&clazzid=(\d+)/);
    if (!m) return;
    // 向上找到课程卡片容器（class 里带 learnCourse，
    // 或带 course 且内部恰好只有一个 h3，即课程名）
    let card = a;
    for (let k = 0; k < 8 && card.parentElement; k++) {
      card = card.parentElement;
      const cl = (card.className || '') + '';
      if (/learnCourse/.test(cl) ||
          (/course/i.test(cl) && card.querySelectorAll('h3').length === 1)) break;
    }
    const h3 = card.querySelector('h3');
    let name = h3 ? h3.textContent.trim().replace(/\s+/g, ' ') : '';
    if (!name) {
      const alt = card.querySelector('.course-info h3, .course-title, h2, h4');
      name = alt ? alt.textContent.trim().replace(/\s+/g, ' ') : '';
    }
    // 任务点进度：优先取 .l-txt，兜底再扫叶子文本
    let prog = '';
    const lt = card.querySelector('.l-txt');
    if (lt && /任务点|进度/.test(lt.textContent || '')) {
      prog = lt.textContent.trim().replace(/\s+/g, ' ');
    }
    if (!prog) {
      card.querySelectorAll('*').forEach(e => {
        if (e.children.length || prog) return;
        const t = (e.textContent || '').trim();
        if (/任务点|进度/.test(t) && t.length < 40) prog = t;
      });
    }
    out.push({
      name: name,
      cid: m[1],
      clsid: m[2],
      cpi: (h.match(/cpi=(\d+)/) || ['', ''])[1],
      prog: prog
    });
  });
  return out;
}
"""


def _lazy_scroll(page, step=300, max_steps=90, wait_ms=350, interval_ms=90):
    """小步慢滚到页底，触发卡片内容的懒加载。

    ⚠️ 关键：课程卡片上的「任务点进度」是随滚动逐屏渲染的
    （实测初始 10 条 → 滚到 900px 变 15 条 → 1800px 变 19 条）。
    如果直接 window.scrollTo(0, scrollHeight) 跳到页底，中间被跳过的区域
    永远不会触发渲染，进度就会大面积缺失。

    改前每滚一屏固定等 350ms；现在每屏只等到「这屏确实渲染出来了」
    （任务点条目数变多）就走，最多仍等 350ms。步长与步数上限一律没动，
    所以漏渲染的风险与改前完全相同，只是不再空等。
    """
    page.evaluate("() => window.scrollTo(0, 0)")
    _poll(page, JS_COUNT_PROG, lambda v: bool(v), 800)
    still = 0
    for _ in range(max_steps):
        before = page.evaluate(JS_COUNT_PROG) or 0
        info = page.evaluate("() => [window.scrollY, window.innerHeight, document.body.scrollHeight]")
        y, vh, h = info
        if y + vh >= h - 10:
            still += 1
            if still >= 2:
                break
        # 步长与等待都带随机抖动：等距滚动、固定节奏是机器特征。
        # 抖动只动「等多久、滚多远」，不动「每屏等渲染」的契约（照旧等条目数变多）。
        page.evaluate("() => window.scrollBy(0, %d)"
                      % int(step * random.uniform(0.8, 1.25)))
        # 等到这一屏渲染出来（条目数增加）或等满 wait_ms
        _poll(page, JS_COUNT_PROG, _grown(before),
              int(wait_ms * random.uniform(0.7, 1.3)), interval_ms)
    # 收尾：还有没渲染完的就继续等，渲染完了立刻回页顶
    _settle(page, JS_COUNT_PROG, quiet_ms=240, cap_ms=1200)
    page.evaluate("() => window.scrollTo(0, 0)")
    page.wait_for_timeout(300)


JS_COUNT_PROG = r"""
() => [...document.querySelectorAll('.l-txt')]
        .filter(e => /任务点|进度/.test(e.textContent || '')).length
"""

# 课程卡片链接数量（课程列表是否已经渲染出来的判据）
JS_COUNT_COURSE_LINK = r"""
() => document.querySelectorAll('a[href*=stucoursemiddle]').length
"""

JS_FIND_LIST_IFRAME = r"""
() => {
  const f = [...document.querySelectorAll('iframe')];
  const t = f.find(x => /visit\/interaction/.test(x.src || ''));
  return t ? t.src : '';
}
"""


def collect_courses(page, cfg) -> list:
    """定位到课程列表页，逐屏滚动触发懒加载，再提取全部课程"""
    banner('读取课程列表')

    # 课程列表固定在 visit/interaction 页（个人空间里是 iframe）
    src = page.evaluate(JS_FIND_LIST_IFRAME) or ''
    target = src or 'https://mooc2-ans.chaoxing.com/visit/interaction'
    try:
        page.goto(target, wait_until='domcontentloaded')
    except Exception as e:
        log('打开课程列表页失败：%s' % e, 'warn')
    # 等课程卡片渲染出来（上限仍是 4.5 秒）
    _poll(page, JS_COUNT_COURSE_LINK, lambda v: bool(v), 4500)

    courses = page.evaluate(JS_COURSES) or []
    if not courses:
        # 再兜底一次：回到个人空间，看是不是能直接从文档里读到
        page.goto('https://i.chaoxing.com/base?ws=1', wait_until='domcontentloaded')
        _poll(page, JS_COUNT_COURSE_LINK, lambda v: bool(v), 4500)
        courses = page.evaluate(JS_COURSES) or []

    # 「任务点进度」随滚动逐屏渲染，必须小步慢滚；直接跳页底会漏掉大部分
    n0 = page.evaluate(JS_COUNT_PROG)
    _lazy_scroll(page)
    n1 = page.evaluate(JS_COUNT_PROG)
    if n1 > n0:
        log('逐屏滚动后，带任务点进度的课程由 %d 条增至 %d 条' % (n0, n1))
    after = page.evaluate(JS_COURSES) or []
    if after:
        courses = after

    # 去重 + 清洗（以 courseId + classId 为唯一标识）
    seen, clean = set(), []
    for c in courses:
        if not c.get('cid'):
            continue
        key = (c['cid'], c.get('clsid', ''))
        if key in seen:
            continue
        seen.add(key)
        if not c.get('name'):
            c['name'] = '课程%s' % c['cid']
        clean.append(c)

    cpi = next((c['cpi'] for c in clean if c.get('cpi')), '') or cfg.get('cpi') or ''
    log('共发现 %d 门课程（含已结束课程）' % len(clean))
    return clean, cpi


# ================================================================ 抓作业
JS_CLICK_WORK = r"""
() => {
  let li = document.querySelector('li[dataname=zy]');
  if (!li) {
    li = [...document.querySelectorAll('li[dataname]')]
         .find(x => /作业/.test(x.textContent || ''));
  }
  if (!li) return false;
  const a = li.querySelector('a') || li;
  a.click();
  return true;
}
"""

JS_WORK_IFRAME = r"""
() => {
  const f = [...document.querySelectorAll('iframe')];
  const t = f.find(x => /\/work\/list|frame_content-zy/.test(
        ((x.src || '') + ' ' + (x.id || ''))));
  return t ? (t.src || '') : '';
}
"""

JS_ITEMS = r"""
() => {
  const r = { count: '', items: [], next: false, empty: false };
  const body = document.body ? (document.body.innerText || '') : '';
  r.empty = /暂无考试|暂无作业|暂无任务/.test(body);
  const m = body.match(/(\d+)\s*\/\s*(\d+)/);
  if (m) r.count = m[1] + '/' + m[2];

  document.querySelectorAll('.right-content').forEach(rc => {
    const li = rc.closest('li') || rc;
    const ps = rc.querySelectorAll('p');
    let title = '', state = '';
    const tp = rc.querySelector('p.overHidden2') || (ps.length ? ps[0] : null);
    if (tp) title = (tp.textContent || '').trim().replace(/\s+/g, ' ');
    const sp = rc.querySelector('p.status') || (ps.length > 1 ? ps[1] : null);
    if (sp) state = (sp.textContent || '').trim().replace(/\s+/g, ' ');
    const tm = li.querySelector('div.time');
    const left = tm ? (tm.textContent || '').trim().replace(/\s+/g, ' ') : '';
    const url = li.getAttribute('data') || '';
    if (title) r.items.push({ title: title, state: state, left: left, url: url });
  });

  const n = document.querySelector('.xl-nextPage');
  if (n) {
    const cls = (n.className || '') + '';
    r.next = !/disable/i.test(cls);
  }
  return r;
}
"""

JS_CLICK_NEXT = r"""
() => {
  const n = document.querySelector('.xl-nextPage');
  if (!n || /disable/i.test(n.className || '')) return false;
  n.click();
  return true;
}
"""


def _sig(items):
    return '|'.join(i['title'] + '#' + i['state'] for i in items)


def _items_ready(d):
    """fetch_items_paged 的轮询条件：列表条目已经出来了，或页面明确说暂无。"""
    if not isinstance(d, dict):
        return False
    return bool(d.get('empty')) or bool(d.get('items'))


def fetch_items_paged(page, wait_first=None, max_pages=30) -> dict:
    """在当前页面上循环翻页，收齐全部条目。

    改前不论页面多快都先死等 N 秒再读；现在等到「条目真的出现 / 页面明确说
    暂无」就立刻读，上限仍是 N 秒。翻页同理：点完「下一页」等的是「内容换了
    一批」（条目签名变化），而不是固定 2.6 秒。没有作业的课因此能秒过。
    """
    cfg_wait = wait_first if wait_first is not None else 2.6
    data = _poll(page, JS_ITEMS, _items_ready, int(cfg_wait * 1000))
    items, count, seen_sig = [], '', set()
    for _ in range(max_pages):
        if not isinstance(data, dict):
            data = page.evaluate(JS_ITEMS) or {}
        if data.get('count') and not count:
            count = data['count']
        sig = _sig(data.get('items') or [])
        if sig and sig in seen_sig:
            break
        if sig:
            seen_sig.add(sig)
        items += data.get('items') or []
        if not data.get('next'):
            break
        if not page.evaluate(JS_CLICK_NEXT):
            break
        data = _poll(page, JS_ITEMS,
                     lambda d: _sig((d or {}).get('items') or []) != sig, 2600)
    return {'items': items, 'count': count, 'empty': False}


def _open_course_page(page, c, cpi, cfg) -> dict:
    """打开课程页并等到左侧导航渲染出来。

    导航一次没出来不立刻下结论：先看页面处于什么状态（风控/掉登录/单纯慢），
    自动重试一次。两次都不行就把原因带回去——绝不能把「页面没打开」当成
    「这门课没有作业」，那是误报，比漏报更害人。
    """
    url = COURSE_PAGE.format(cid=c['cid'], clsid=c['clsid'], cpi=cpi)
    why = ''
    for attempt in (1, 2):
        page.goto(url, wait_until='domcontentloaded')
        nav = _poll(page, JS_HAS_NAV, lambda v: bool(v), int(cfg['page_wait'] * 1000))
        if nav:
            return {'ok': True, 'reason': ''}
        why = ((page.evaluate(JS_PAGE_HEALTH) or {}).get('why')) or '页面未加载出课程导航'
        if attempt == 1:
            # 重试前等多久要看原因：单纯慢 2 秒就够；
            # 撞了风控 2 秒根本缓不过来（实测连续 28 门全挂），必须长冷却
            wait = 20.0 if '安全验证' in why or '频繁' in why else 2.0
            log('  ⚠ 课程页没有渲染出导航（%s），隔 %d 秒重试一次…' % (why, wait), 'warn')
            time.sleep(wait)
    return {'ok': False, 'reason': why}


def fetch_homework(ctx, page, c, cpi, cfg) -> dict:
    opened = _open_course_page(page, c, cpi, cfg)
    if not opened['ok']:
        return {'no_module': False, 'items': [], 'count': '',
                'page_failed': True, 'reason': opened['reason']}

    # 「作业」导航项可能是渐进渲染的（智慧课程新版门户先出 AI助教，
    # 其余项晚几步），所以把点击也放进轮询：出现就点。
    # 上限取 max(page_wait, 5 秒)：实测过导航渲染到 3.4 秒才齐的情况，
    # 贴着 page_wait 的下限太险，漏一次作业比多等两秒严重得多。
    if not _poll(page, JS_CLICK_WORK, lambda v: bool(v),
                 max(int(cfg['page_wait'] * 1000), 5000), 300):
        return {'no_module': True, 'items': [], 'count': ''}

    # 作业列表是 ajax 塞进 iframe 的：探得密（100ms），上限仍是 12 秒
    work_url = _poll(page, JS_WORK_IFRAME, lambda v: bool(v), 12000, 100) or ''
    if not work_url:
        # 点进了作业但列表一直没出来 —— 这不是「没有作业」，是页面出了状况
        return {'no_module': False, 'items': [], 'count': '',
                'page_failed': True, 'reason': '作业列表没有加载出来'}

    page.goto(work_url, wait_until='domcontentloaded')
    res = fetch_items_paged(page)
    res['no_module'] = False
    return res


# ================================================================ 抓考试
def fetch_exam(page, c, cpi, cfg) -> dict:
    ts = int(time.time() * 1000)
    url = EXAM_LIST.format(cid=c['cid'], clsid=c['clsid'], cpi=cpi, ts=ts)
    res, body, why = {'items': [], 'count': '', 'empty': False}, '', ''
    for attempt in (1, 2):
        page.goto(url, wait_until='domcontentloaded')
        res = fetch_items_paged(page, wait_first=cfg['page_wait'])
        try:
            body = page.evaluate("() => document.body.innerText.replace(/\\s+/g,' ')")
        except Exception:
            body = ''
        plain = body.replace(' ', '')
        why = ''
        if re.search(r'passport|login', page.url or '', re.I):
            why = '被重定向到登录页，登录态可能已失效'
        elif re.search(r'安全验证|验证码|访问异常|过于频繁|请稍后再试', plain):
            why = '触发平台安全验证'
        # 页面真的加载出来了的判据：有条目 / 平台明说「暂无考试」/ 有实质内容
        # 且没有掉登录或风控迹象。三者皆无 → 视为未加载，重试一次。
        if res['items'] or '暂无考试' in body or (len(body) >= 200 and not why):
            break
        if attempt == 1:
            wait = 20.0 if ('安全验证' in why or '频繁' in why) else 2.0
            log('  ⚠ 考试页疑似没有加载出来（%s），隔 %d 秒重试一次…'
                % (why or '内容异常', wait), 'warn')
            time.sleep(wait)
    else:
        return {'items': [], 'count': '', 'empty': True, 'no_module': False,
                'page_failed': True,
                'reason': why or '考试页没有加载出来'}
    res['empty'] = ('暂无考试' in body) or not res['items']
    res['no_module'] = False
    return res


# ================================================================ 抓任务点明细
JS_CLICK_CHAPTER = r"""
() => {
  let li = document.querySelector('li[dataname=zj]');
  if (!li) {
    li = [...document.querySelectorAll('li[dataname]')]
         .find(x => /章节/.test(x.textContent || ''));
  }
  if (!li) return false;
  (li.querySelector('a') || li).click();
  return true;
}
"""

JS_CHAPTER_IFRAME = r"""
() => {
  const f = [...document.querySelectorAll('iframe')];
  const t = f.find(x => /studentcourse/.test(x.src || ''));
  return t ? t.src : '';
}
"""

# 章节节点数量（判断明细是不是已经渲染齐了）
JS_COUNT_CHAPTER = r"""
() => document.querySelectorAll('.chapter_item').length
"""

JS_PROG_DETAIL = r"""
() => {
  const res = { total: null, pending: [], nodes: 0 };
  const m = (document.body.innerText || '')
        .match(/已完成任务点[:：]?\s*(\d+)\s*\/\s*(\d+)/);
  if (m) res.total = [parseInt(m[1], 10), parseInt(m[2], 10)];

  document.querySelectorAll('.chapter_item').forEach(item => {
    const ct = item.querySelector('.clicktitle');
    let name = ct ? (ct.getAttribute('title') || ct.textContent) : '';
    name = (name || '').replace(/\s+/g, ' ').trim();
    if (!name) return;
    res.nodes++;
    // 未完成的节点带 .catalog_jindu（且通常附加 catalog_tishi120）
    const jd = item.querySelector('.catalog_jindu');
    if (!jd) return;
    const txt = (jd.textContent || '').replace(/\s+/g, ' ').trim();
    if (!(/tishi120/.test(jd.className || '') || /待完成|未完成/.test(txt))) return;
    const pts = jd.querySelector('.catalog_points_yi');
    let n = pts ? parseInt((pts.textContent || '').trim(), 10) : NaN;
    if (isNaN(n)) {
      const mm = txt.match(/(\d+)/);
      n = mm ? parseInt(mm[1], 10) : 1;
    }
    if (n <= 0) return;            // 0 个待完成 = 该节点没欠账，不算未完成
    const tip = jd.querySelector('.bntHoverTips');
    const tipTxt = tip ? (tip.textContent || '').replace(/\s+/g, ' ').trim() : txt;
    // kid = 章节节点的 knowledgeid（.chapter_item 的 id 是 cur<kid>），
    // 「自动刷视频」要靠它直达 knowledge/cards 页面。
    const kid = (item.id || '').replace(/^cur/, '');
    res.pending.push({ name: name, count: n, text: tipTxt, kid: kid });
  });
  return res;
}
"""


def fetch_progress_detail(page, c, cpi, cfg) -> dict:
    """下钻到「章节」页，列出未完成的章节节点（任务点明细）

    实测：章节页每个 .chapter_item 中，未完成的节点带 .catalog_jindu，
    文本形如「N个待完成任务点」；已完成的节点没有该元素。
    """
    opened = _open_course_page(page, c, cpi, cfg)
    if not opened['ok']:
        return {'no_module': False, 'pending': [], 'total': None,
                'page_failed': True, 'reason': opened['reason']}
    # 「章节」导航项同样可能渐进渲染，点击放进轮询（上限同作业：max(page_wait, 5s)）
    if not _poll(page, JS_CLICK_CHAPTER, lambda v: bool(v),
                 max(int(cfg['page_wait'] * 1000), 5000), 300):
        return {'no_module': True, 'pending': [], 'total': None}
    zj = _poll(page, JS_CHAPTER_IFRAME, lambda v: bool(v), 12000, 100) or ''
    if not zj:
        # 点进了章节但页面一直没出来 —— 不是「没有章节」，是出了状况
        return {'no_module': False, 'pending': [], 'total': None,
                'page_failed': True, 'reason': '章节页没有加载出来'}
    page.goto(zj, wait_until='domcontentloaded')
    # 章节节点是分批塞进 DOM 的：「概览文字出现」并不等于「明细已经齐了」，
    # 所以先等概览就位，再等节点数稳定，最后才正式读一次 —— 读到半截明细会
    # 让报告漏掉未完成章节，这里绝不能图快。
    _poll(page, JS_PROG_DETAIL,
          lambda d: bool(d) and (d.get('nodes') or 0) > 0 and bool(d.get('total')),
          int(cfg['page_wait'] * 1000))
    _settle(page, JS_COUNT_CHAPTER, quiet_ms=300, cap_ms=700)
    data = page.evaluate(JS_PROG_DETAIL) or {}
    if not isinstance(data, dict):
        data = {}
    return {'no_module': False,
            'pending': data.get('pending') or [],
            'total': data.get('total'),
            'nodes': data.get('nodes', 0)}


# ================================================================ 扫描主流程
# ================================================================ 运行控制（供 GUI 注入）
# 扫描是个长循环（几十门课要一两分钟），界面需要能在「课程边界」暂停或中止。
# CLI 模式不注入钩子，_control_hook 保持 None，行为与以前完全一致。
_control_hook = None


def set_control_hook(fn):
    """注册控制回调： fn(stage: str) -> bool。返回 False 表示中止本次扫描。"""
    global _control_hook
    _control_hook = fn


def checkpoint(stage: str = '') -> bool:
    """在安全边界把控制权交出去一瞬。

    返回 False 表示用户要求中止。钩子内部抛异常时按「继续」处理，
    避免界面代码的问题把整个扫描带崩。
    """
    fn = _control_hook
    if fn is None:
        return True
    try:
        return fn(stage) is not False
    except Exception:
        return True


def _course_wait(cfg_base, risky):
    """两门课之间睡多久（秒）。

    risky = 这门课页面没加载成功、或已经判定撞了风控。撞了就在基础节奏上
    再放慢一倍，但无论如何不低于 2.5 秒。

    单独抽成函数是为了能被测试直接验：v3.2 那次「course_delay 默认改成 3 秒」
    在正常路径上是假的——一行 min(base, 0.6) 把它夹死成固定的 0.6 秒，
    测试却只 grep 源码里字符串出现过，照样全绿（假绿灯）。
    """
    base = float(cfg_base or 0) or float(DEFAULT_CONFIG['course_delay'])
    if risky:
        return max(base * 2.0, 2.5)
    return max(base, 0.6)


def scan(cfg, limit=0, headless=None, open_report=None, deep=True, only=None) -> dict:
    """扫描并出报告。

    only: 课程 key 列表（course_key() 的返回值）。给了就只扫这些课；
          留空/None 表示扫全部。优先于 limit。
    limit: 只扫最近 N 门。课程列表的顺序就是学习通个人空间的默认顺序
          （最近学习过的在最前），所以「前 N 门」即「最近学过的 N 门」。
    """
    limit = limit or cfg.get('max_courses') or 0
    open_report = cfg.get('open_report', True) if open_report is None else open_report
    t0 = time.time()
    total_all = 0          # 账号内课程总数，用于区分「勾选了一部分」
    scope_kind = ''        # ''=全量 ｜ selected=按勾选 ｜ recent=只查最近 N 门

    ctx = launch(cfg, headless=headless)
    page = ctx.new_page()
    try:
        banner('检查登录状态')
        if not check_login(page):
            if restore_state(ctx) and check_login(page):
                log('已用本地会话自动登录 ✓')
            else:
                log('本地没有可用登录态，请先执行：'
                    'python chaoxing_scanner.py login', 'err')
                raise NotLoggedIn('未登录')

        courses, cpi = collect_courses(page, cfg)
        if not courses:
            log('没有读到任何课程，可能页面结构变了或账号无课程。', 'err')
            return {'courses': [], 'undone': []}
        total_all = len(courses)
        if cpi:
            cfg['cpi'] = cpi
            try:
                # 只把 cpi 合并进磁盘配置。以前是 save_config(cfg) —— 而 cfg
                # 是调用方传进来的，可能只有几个键，一落盘就把用户设置冲掉。
                patch_config({'cpi': cpi})
            except Exception as e:
                # 存 cpi 只是缓存优化，落盘失败不该中止整场扫描
                log('cpi 写回配置失败（不影响本次查询）：%s' % str(e)[:60],
                    'warn')
        if only:
            want = [k for k in only if k]
            keep = set(want)
            picked = [c for c in courses if course_key(c) in keep]
            lost = [k for k in want if k not in {course_key(c) for c in picked}]
            if not picked:
                log('所选课程在本次课程列表里一门都没找到，'
                    '可能课程有变动。请重新点「获取课程列表」再选一次。', 'err')
                raise RuntimeError('所选课程未匹配到任何课程')
            if lost:
                log('有 %d 门所选课程已不在当前课程列表中，已跳过。' % len(lost), 'warn')
            courses = picked
            scope_kind = 'selected'
            log('已按你的选择限定为 %d 门课程' % len(courses))
        elif limit:
            courses = courses[:limit]
            # 只有真的截掉了课程才算「范围受限」；limit ≥ 总数时等同全量，不必标注
            if len(courses) < total_all:
                scope_kind = 'recent'
            log('已限制为最近 %d 门课（按学习通默认顺序，最近学习过的在前）' % len(courses))

        banner('逐门课抓取（共 %d 门）' % len(courses))
        results = []
        stopped = False
        risk_streak = 0   # 连续「页面未加载」的课程数
        seen_risk = False  # 本次扫描是否撞过风控——撞过就全程保持大间隔，不再贴地飞行
        cool_downs = 0    # 已做的全局长冷却次数（最多 2 次，之后止损中止）
        for i, c in enumerate(courses, 1):
            # 课程边界检查点：暂停在此生效，中止在此干净收尾（已抓到的结果不丢）
            if not checkpoint('course:%d/%d' % (i, len(courses))):
                stopped = True
                log('')
                log('已中止：完成 %d/%d 门课，将用已抓到的结果出报告。'
                    % (i - 1, len(courses)), 'warn')
                break
            row = {'name': c['name'], 'cid': c['cid'], 'clsid': c['clsid'],
                   'prog_text': c.get('prog', '')}
            try:
                hw = fetch_homework(ctx, page, c, cpi, cfg)
            except Exception as e:
                hw = {'no_module': False, 'items': [], 'count': '',
                      'error': str(e).splitlines()[0][:120]}
            try:
                ex = fetch_exam(page, c, cpi, cfg)
            except Exception as e:
                ex = {'no_module': False, 'items': [], 'count': '',
                      'empty': True, 'error': str(e).splitlines()[0][:120]}
            row['hw'] = hw
            row['exam'] = ex

            # 任务点章节下钻：只对存在缺口的课程做，避免拖慢全量扫描
            pm_prog = re.search(r'(\d+)\s*/\s*(\d+)', c.get('prog') or '')
            if deep and pm_prog and int(pm_prog.group(1)) < int(pm_prog.group(2)):
                try:
                    row['prog_detail'] = fetch_progress_detail(page, c, cpi, cfg)
                except Exception as e:
                    row['prog_detail'] = {
                        'no_module': False, 'pending': [], 'total': None,
                        'error': str(e).splitlines()[0][:120]}

            results.append(row)

            nh = sum(1 for it in hw['items'] if is_undone(it))
            ne = sum(1 for it in ex['items'] if is_undone(it))
            if hw.get('page_failed'):
                hw_txt = '⚠ 页面未加载'
            elif hw.get('no_module'):
                hw_txt = '无作业模块'
            else:
                hw_txt = '作业 %s（未完成 %d）' % (hw.get('count') or '-', nh)
            if ex.get('page_failed'):
                ex_txt = '⚠ 页面未加载'
            elif ex.get('empty'):
                ex_txt = '无考试'
            else:
                ex_txt = '考试 %d 场（未完成 %d）' % (len(ex['items']), ne)
            pd = row.get('prog_detail')
            extra = ''
            if pd is not None and not pd.get('no_module'):
                extra = ' · 未完成章节 %d' % len(pd.get('pending') or [])
            log('[%2d/%2d] %-34s %s · %s%s' % (i, len(courses), cut(c['name'], 34),
                                               hw_txt, ex_txt, extra))
            # ---- 风控防护 -------------------------------------------------
            # 实测教训：0.6 秒的课间间隔在部分网络下会触发平台安全验证，
            # 且风控一旦触发，2 秒的重试间隔缓不过来，越撞越死（连挂 28 门）。
            # 所以：① 撞过一次风控，本次扫描全程用大间隔；
            #       ② 连续 3 门失败 → 全局长冷却 45/90 秒；
            #       ③ 冷却 2 次仍连挂 → 止损中止，别再消耗账号印象分。
            bad = bool(hw.get('page_failed') or ex.get('page_failed')
                       or (pd or {}).get('page_failed'))
            if bad:
                risk_streak += 1
                seen_risk = True
            else:
                risk_streak = 0
            if bad and risk_streak >= 3:
                if cool_downs >= 2:
                    log('')
                    log('⚠ 连续 %d 门课页面都没加载出来，风控没有解除，'
                        '继续查只会更糟。已中止本次扫描。' % risk_streak, 'err')
                    log('建议：等 10 分钟以上再查；或先只勾选几门重要的课分批查。',
                        'err')
                    stopped = True
                    break
                cool = 45 if cool_downs == 0 else 90
                cool_downs += 1
                log('')
                log('⚠ 连续 %d 门课页面未加载（疑似触发平台风控），'
                    '冷却 %d 秒后再继续…（第 %d 次）' % (risk_streak, cool, cool_downs),
                    'warn')
                risk_streak = 0
                aborted = False
                for _ in range(int(cool)):
                    time.sleep(1.0)
                    if not checkpoint('risk_cool:%d' % cool_downs):
                        aborted = True
                        break
                if aborted:
                    log('冷却期间已中止，将用已抓到的结果出报告。', 'warn')
                    stopped = True
                    break
                log('冷却结束，继续。')
            base = cfg.get('course_delay', 3.0)
            time.sleep(_course_wait(base, bad or seen_risk))

        failed_courses = [
            {'course': r['name'],
             'what': '、'.join(w for w, d in (('作业', r['hw']), ('考试', r['exam']),
                                              ('章节明细', r.get('prog_detail') or {}))
                               if d.get('page_failed')),
             'reason': ((r['hw'].get('page_failed') and r['hw'].get('reason'))
                        or (r['exam'].get('page_failed') and r['exam'].get('reason'))
                        or ((r.get('prog_detail') or {}).get('page_failed')
                            and (r.get('prog_detail') or {}).get('reason'))
                        or '页面未加载成功')}
            for r in results
            if r['hw'].get('page_failed') or r['exam'].get('page_failed')
            or (r.get('prog_detail') or {}).get('page_failed')]
        if failed_courses:
            log('')
            log('⚠ 有 %d 门课的页面没有加载成功（%s），这些课的作业/考试结果不可信，'
                '建议稍后重新查询一次。'
                % (len(failed_courses), '、'.join(x['course'] for x in failed_courses[:6])
                   + ('…' if len(failed_courses) > 6 else '')), 'warn')

        log('')
        if stopped:
            log('本次扫描已中止，用时 %.1f 分钟' % ((time.time() - t0) / 60), 'warn')
        else:
            log('抓取完成，用时 %.1f 分钟' % ((time.time() - t0) / 60))
    finally:
        save_state(ctx)
        close_ctx(ctx)

    # 告诉界面「扫描已结束，正在收尾」——这几秒里按钮是灰的，得让用户知道在等什么
    checkpoint('reporting')
    banner('生成报告')
    report = build_report(courses, results,
                          stopped_at=len(results) if stopped else None,
                          scope_total=total_all if scope_kind else None,
                          scope_kind=scope_kind or 'selected',
                          failed_courses=failed_courses)
    paths = write_outputs(report)
    if open_report:
        try:
            os.startfile(str(paths['md']))  # type: ignore[attr-defined]
        except Exception:
            pass
    return paths


def course_key(c) -> str:
    """课程在本次运行中的唯一标识。

    课程名可能重复（同一门课开多个班），所以用 courseId + classId 组合，
    界面里让用户勾选时传的就是这个 key。
    """
    return '%s:%s' % (c.get('cid', ''), c.get('clsid', ''))


def split_only(only):
    """把任务范围列表拆成（课程 key 集合, {课程 key: kid 集合}）。

    支持两种规格混用：
      「cid:clsid」     整门课（该课全部待完成节点）；
      「cid:clsid|kid」 课程里的某一个章节节点。
    界面上课程勾选框传前者、章节勾选框传后者，两种可以同时勾。
    """
    keys, nodes = set(), {}
    for s in (only or []):
        s = str(s or '')
        if not s:
            continue
        if '|' in s:
            k, kid = s.split('|', 1)
            if k and kid:
                keys.add(k)
                nodes.setdefault(k, set()).add(kid)
        else:
            keys.add(s)
    return keys, nodes


def list_courses(cfg, headless=None) -> dict:
    """只读课程名录（不抓作业 / 考试 / 任务点），供界面做勾选。

    比完整扫描快得多：登录 + 读一次课程列表页即可。
    """
    t0 = time.time()
    ctx = launch(cfg, headless=headless)
    page = ctx.new_page()
    try:
        banner('读取课程列表')
        if not check_login(page):
            if restore_state(ctx) and check_login(page):
                log('已用本地会话自动登录 ✓')
            else:
                log('本地没有可用登录态，请先登录。', 'err')
                raise NotLoggedIn('未登录')

        courses, cpi = collect_courses(page, cfg)
        if cpi:
            cfg['cpi'] = cpi
            try:
                # 只把 cpi 合并进磁盘配置。以前是 save_config(cfg) —— cfg 是
                # 任务线程启动时读的那份，读完名录（十来秒）才落盘，中间用户
                # 在界面上保存的模型信息会被这份旧配置整体冲掉。
                # 与 scan() 里那个坑同源，那里已经改成 patch_config 了。
                patch_config({'cpi': cpi})
            except Exception as e:
                log('cpi 写回配置失败（不影响本次查询）：%s' % str(e)[:60],
                    'warn')
        items = [{'key': course_key(c), 'name': c['name'], 'cid': c['cid'],
                  'clsid': c.get('clsid', ''), 'prog': c.get('prog', '')}
                 for c in courses]
    finally:
        save_state(ctx)
        close_ctx(ctx)

    log('')
    log('课程名录读取完成，共 %d 门，用时 %.1f 秒' % (len(items), time.time() - t0))
    return {'courses': items}


def cut(s, n):
    s = s or ''
    return s if len(s) <= n else s[:n - 1] + '…'


def is_undone(item) -> bool:
    st = (item.get('state') or '').strip()
    if not st:
        return False
    return not any(d in st for d in DONE_STATES)


# ================================================================ 报告
# ================================================================ 自动刷视频
# 实测结论（2026-09-28，真实账号验证过）：
#   1) 章节节点的 knowledgeid 就在 .chapter_item 的 id 里（cur<kid>），
#      凭 courseid/clazzid/kid/cpi 可以直达 knowledge/cards 卡片页，无需 enc；
#   2) 视频卡是 ananas/modules/video iframe + video.js 播放器；
#      静音 + playbackRate=2 真实播放，平台心跳照发，播到片尾任务点即判完成
#      （multimedia/log 响应 {"isPassed":true}，章节列表节点翻绿）；
#   3) 播放中偶发自暂停（无弹窗），再次 play() 即可继续；
#   4) 测验卡（doHomeWorkNew）不是视频，本模块不碰——替考式自动答题不做。
CARDS_URL = ('https://mooc1.chaoxing.com/mooc-ans/knowledge/cards'
             '?clazzid={clsid}&courseid={cid}&knowledgeid={kid}&num=0&ut=s&cpi={cpi}')

# 视频元素就绪（duration 是有限数才算加载完，NaN 是还在缓冲）。
# ⚠️ 学习通的播放器是 preload=none：duration 在开始播放前一直是 NaN，
# 所以「等时长」必须放在 play() 之后，否则所有视频都会被误判成「无视频」。
JS_VIDEO_READY = r"""
() => {
  const vs = [...document.querySelectorAll('video')]
    .filter(v => isFinite(v.duration) && v.duration > 0);
  return vs.length > 0;
}
"""

JS_VIDEO_HAS = "() => !!document.querySelector('video')"

JS_VIDEO_STATE = r"""
() => {
  const v = document.querySelector('video');
  if (!v) return null;
  return {t: v.currentTime, dur: v.duration, paused: v.paused, ended: v.ended,
          rate: v.playbackRate, muted: v.muted};
}
"""

# 静音 + 倍速 + 播放。play() 的 promise 失败（自动播放策略）就静默吞掉，
# 下一轮监控循环会再试
JS_VIDEO_PLAY = r"""
(r) => {
  const v = document.querySelector('video');
  if (!v) return 'novideo';
  v.muted = true;
  v.playbackRate = r;
  const p = v.play();
  if (p && p.catch) p.catch(() => {});
  return 'ok';
}
"""

JS_VIDEO_PAUSE = "() => { const v = document.querySelector('video'); if (v && !v.paused) v.pause(); }"

# 这个 frame 里的视频刷完没有（刷完打标记，防止同一个卡被刷两遍）
JS_VIDEO_CLAIM = r"""
() => {
  if (window.__cx_brushed) return false;
  const v = document.querySelector('video');
  return !!v;
}
"""


def fmt_t(sec):
    try:
        s = int(float(sec))
    except (TypeError, ValueError):
        return '?'
    return '%d:%02d' % (s // 60, s % 60)


def brush_video_frame(vf, cfg, progress, control) -> str:
    """把一个视频 iframe 播到片尾。

    返回 'done' | 'stopped' | 'stuck' | 'novideo' | 'timeout'。
    学习通的心跳是播放器自己发的，我们只负责「让视频真的播完」——
    不伪造心跳请求，平台看到的就是一次真实的观看（静音、倍速）。

    防检测细节（v3.3，v3.4 修正档位来源）：
      · 倍速不取死值：只在播放器官方档位（所选档位 ± 相邻档）之间随机取挡，
        部分视频中途再换一次挡——「几小时恒定同一倍速」是明显的统计特征。
        v3.3 曾用 ±17% 的任意倍速，平台会拨回非法档位造成频繁小暂停；
      · 播放期间每隔随机 25~70 秒在页面里注入一小段鼠标移动（CDP 注入，
        不动用户真实光标）——「零鼠标移动挂几小时」同样是特征。
    """
    base = g_rate(cfg)
    # 档位池 = 所选档位 + 相邻官方档位（1.0 没有更慢的邻档，2.0 没有更快的）。
    # 只在官方档位之间取挡：非法档位会被平台拨回，来回打架就是频繁小暂停。
    _i = BRUSH_RATES.index(base)
    tiers = list(BRUSH_RATES[max(0, _i - 1): _i + 2])
    rate = random.choice(tiers)
    # 部分视频中途换一次挡（换到另一档，不再换回来）
    switch_pending = random.random() < 0.35
    switch_at = None
    if not _poll(vf, JS_VIDEO_HAS, lambda v: bool(v), 15000, 400):
        return 'novideo'
    try:
        vf.evaluate(JS_VIDEO_PLAY, rate)
    except Exception as e:
        log('    播放启动失败：%s' % str(e)[:80], 'warn')
        return 'stuck'
    # duration 只有开播了才会加载（preload=none），先播再等它就绪
    if not _poll(vf, JS_VIDEO_READY, lambda v: bool(v), 25000, 400):
        return 'novideo'
    st = None
    try:
        st = vf.evaluate(JS_VIDEO_STATE)
    except Exception:
        pass
    if not st or not st.get('dur'):
        return 'novideo'
    if switch_pending:
        switch_at = st['t'] + (st['dur'] - st['t']) * random.uniform(0.3, 0.7)
    # 超时上限：剩余内容按倍速折算再放 80% 余量 + 2 分钟，防心跳卡住白等
    # （余量按「可能切到的最低档」折算，中途降速也不会误杀）
    low = min(tiers)
    deadline = time.time() + max((st['dur'] - st['t']) / low * 1.8 + 120, 120)
    last_t = st['t']
    no_progress = 0        # 连续多次「暂停且位置没动过」→ 可能有插问卡死
    lost = 0               # 连续取不到播放器状态的轮数（断网/页面崩溃的信号）
    last_report = st['t']
    last_mouse = time.time()
    mouse_gap = random.uniform(20, 50)
    while time.time() < deadline:
        act = control()
        if act == 'stop':
            return 'stopped'
        if act == 'pause':
            try:
                vf.evaluate(JS_VIDEO_PAUSE)     # 暂停的是视频本身，不只是任务
            except Exception:
                pass
            while control() == 'pause':
                if control() == 'stop':
                    return 'stopped'
                time.sleep(0.3)
            try:
                vf.evaluate(JS_VIDEO_PLAY, rate)
            except Exception:
                pass
        # 随机间隔的轻量鼠标活动：像真人盯着页面偶尔动下鼠标
        if time.time() - last_mouse >= mouse_gap:
            last_mouse = time.time()
            mouse_gap = random.uniform(25, 70)
            try:
                pg = vf.page
                vp = pg.viewport_size or {'width': 1440, 'height': 900}
                mx = random.uniform(40, max(60, vp['width'] - 40))
                my = random.uniform(60, max(80, vp['height'] - 60))
                for _ in range(random.randint(2, 4)):
                    pg.mouse.move(
                        random.uniform(max(10, mx - 35), mx + 35),
                        random.uniform(max(10, my - 25), my + 25))
                    time.sleep(random.uniform(0.03, 0.12))
            except Exception:
                pass
        time.sleep(2)
        try:
            st = vf.evaluate(JS_VIDEO_STATE)
        except Exception:
            lost += 1
            if lost >= 8:
                log('    页面状态连续取不到（可能断网或页面崩溃），按卡住处理。'
                    '重新打开即可重试。', 'warn')
                return 'stuck'
            continue
        if not st:
            lost += 1
            if lost >= 8:
                return 'stuck'
            continue
        lost = 0
        if st.get('ended') or (st['dur'] and st['t'] >= st['dur'] - 1.5):
            time.sleep(3)     # 等播放器把最后一条心跳发出去再走
            return 'done'
        if st['paused']:
            try:
                vf.evaluate(JS_VIDEO_PLAY, rate)
            except Exception:
                pass
            # 位置纹丝不动的暂停连续出现，多半是插问弹窗把播放器卡住了：
            # 刷不动就跳过，别死磕（那一题本来也不是视频能解决的）
            no_progress = no_progress + 1 if abs(st['t'] - last_t) < 3 else 0
            if no_progress >= 5:
                log('    视频在 %s 处反复暂停（可能有插问），跳过。'
                    % fmt_t(st['t']), 'warn')
                return 'stuck'
        else:
            no_progress = 0
        last_t = st['t']
        # 到了随机约定的进度点就换一挡倍速：真人会中途调倍速，
        # 恒定一个速度播完几十个视频不像人
        if switch_pending and switch_at is not None and st['t'] >= switch_at:
            switch_pending = False
            others = [x for x in tiers if abs(x - rate) > 0.01]
            if others:
                rate = random.choice(others)
                try:
                    vf.evaluate(JS_VIDEO_PLAY, rate)
                except Exception:
                    pass
        # 平台要是把倍速/静音拨回去，就再拨回来
        if st['rate'] != rate or not st['muted']:
            try:
                vf.evaluate(JS_VIDEO_PLAY, rate)
            except Exception:
                pass
        if st['t'] - last_report >= 300:
            last_report = st['t']
            progress('    播放中 %s / %s（%sx 静音）'
                     % (fmt_t(st['t']), fmt_t(st['dur']), rate))
    return 'timeout'


# 学习通播放器官方倍速档位。写入 video.playbackRate 的值必须来自这里：
# 非官方档位（如 1.24 / 1.76）会被平台播放器拨回去，工具再拨回来，
# 来回打架 = 播放中频繁出现约 1 秒的小暂停（v3.3 的教训，v3.4 修正）。
BRUSH_RATES = (1.0, 1.25, 1.5, 2.0)


def g_rate(cfg):
    """把配置里的 brush_rate 吸附到最近的官方档位（缺省 1.25）。"""
    r = cfg.get('brush_rate', 1.25)
    try:
        r = float(r or 1.25)
    except (TypeError, ValueError):
        r = 1.25
    return min(BRUSH_RATES, key=lambda x: abs(x - r))


def brush_pace_rest(done_total, cfg, progress, control, state=None):
    """连刷若干个视频后歇一会（可被「停止」打断）。

    静音倍速连刷几个小时、中途零停顿，是刷课最明显的统计特征之一；
    每刷 brush_rest_every 个视频歇 brush_rest_seconds 秒（±40% 抖动），
    节奏更像真人（人总要离开一下）。返回 True 表示被用户停止。

    state：跨节点共享的小本子（{'last': 上次已经歇过的累计数}）。没有它会
    出问题：本函数是在**每个节点之后**调的，而「文档卡 / 音频卡 / 章节页
    打不开」的节点完成数是 0——计数停在原地，一旦正好卡在倍数上，后面每个
    零进度节点都会再歇一次，连续几个节点就是几分钟白睡。
    """
    every = int(cfg.get('brush_rest_every', 6) or 0)
    if every <= 0 or done_total <= 0 or done_total % every:
        return False
    if state is None:
        state = {}
    if done_total <= int(state.get('last', 0)):
        return False                     # 计数没往前走，别再歇一遍
    state['last'] = done_total
    secs = float(cfg.get('brush_rest_seconds', 30) or 30)
    secs = max(5.0, secs) * random.uniform(0.6, 1.4)
    progress('    已连刷 %d 个视频，歇 %.0f 秒再继续（可点停止）…'
             % (done_total, secs))
    end = time.time() + secs
    while time.time() < end:
        time.sleep(1)
        if _wait_control(control, progress) == 'stop':
            return True
    return False


# ================================================================ LLM 自动答题（v2.9，v2.11 改直接交卷）
# 思路：作业卡就是 doHomeWorkNew 的表单页。解析 .TiMu 题目 → 交给用户配置的
# 大模型（OpenAI 兼容接口）作答 → 点选/填入 → 调平台自己的 btnBlueSubmit()
# 完整链交卷（等价于人工点提交），并核实受理结果。安全阀：
#   · LLM 没给出答案（返回 ? / 解析失败）的题 → 整份不填表不交卷
#   · 要上传附件（实验报告）的整份跳过并提示
#   · 交卷结果必须核实（响应监听 + 重开作业卡），核实不到如实报「未确认」
#   · 答案按题干哈希落本地缓存（runtime/answer_cache.json），同题不重复花钱
ATYPES = {'0': '单选', '1': '多选', '3': '判断'}   # 客观题：直接选
STYPES = {'2': '填空', '4': '简答'}                # 主观题：LLM 写文本
# 「上传附件」型主观题（题干要求交实验报告/附件）LLM 替不了，单独跳过并说明。
# ⚠ 这份规则与 JS_PARSE_TIMUS 里的 stemUpload 必须一致（两处一起改）：
#   JS 那份管「一进来就认出附件题」，这份管「模型给了占位答案时兜底回头判」。
#   规则放宽是有意的——漏判=乱填一个答案拿 0 分，误判=整份跳过让用户自己做。
_ATTACH_STEM_RE = re.compile(
    r'上传附件|提交附件|附件上传|附件提交|以附件|附件形式|拍照上传'
    r'|上传.{0,8}(报告|文件|文档|图片|视频|音频|压缩包|作品|附件)'
    r'|提交.{0,8}(报告|文件|文档|压缩包|作品|附件)'
    r'|(报告|文件|作品|附件).{0,6}(上传|提交)')
# 模型拒答特征（题干是反爬乱码时模型可能"猜懂"后拒绝作答，这种话不能进作业）
# 模型拒答 / 元话语特征。乱码题干（平台字体反爬）会诱发这类回复，它们绝不能
# 被当成答案写进作业。原先在 llm_solve 里就地编译，现在文本通道与视觉通道都要
# 用同一套判据，所以提到模块级。
_REFUSE_RE = re.compile(
    r'(无法|不能|不会).{0,8}(代[替理]|提交|上传|完成)'
    r'|(请|需要).{0,8}(自行|亲自|本人).{0,4}(上传|提交|完成)'
    r'|(语言模型|AI\s*助手|作为一个\s*AI)'
    # 元话语：模型在描述「这题该怎么操作」而不是给出答案内容。真机实测：
    # 实验报告题被填了「进入课程作业中的实验报告，点击上传文件，在下拉
    # 菜单中选择要提交的文件，确认后提交即可。」→ 0 分。这类话绝不能进作业。
    r'|(点击|选择|打开|进入).{0,14}(上传|提交|附件)'
    r'|(上传|提交)(文件|附件).{0,10}(即可|就行|然后|最后)'
    r'|下拉菜单.{0,8}选择')


def _stem_missing(stem):
    """题干压根没取到 / 明显不可用（作业卡结构变了、字体反爬的 PUA 痕迹）。

    真机实测事故：某课作业卡的题干不在 .Zy_TItle 里，取到空题干 → 附件规则
    无从匹配 → 模型只看到「1.【简答】」就瞎编一段「进入课程作业中的实验报告，
    点击上传文件……提交即可」当答案，工具照填还交了卷，整题 0 分。
    判据（任一命中）：
      · 空或短得不像题干（< 4 字符）
      · 含 Unicode 私用区（E000-F8FF）/ 替换字符（FFFD）——字体反爬的痕迹
    ⚠ 这里**不能**用「无字母数字」判定：超星里纯中文题干很常见
      （「下列哪项属于…」），那样会把大批正常客观题误判成不可读。
      那条更强的判据只留给主观题（见 answer_one），语义见下。
    """
    s = (stem or '').strip()
    if len(s) < 4:
        return True
    return bool(re.search(r'[\ue000-\uf8ff\ufffd]', s))


def _split_fill(a, nblank):
    """多空填空题：把一个答案串按空序切成 nblank 段。

    模型对多空填空的给法有三种，都要认：
      ① 按约定用 | 分隔（新提示词要求的写法）；
      ② 用顿号/分号/换行分隔（模型的老习惯，如「桥接模式、NAT模式、仅主机模式」）；
      ③ 不切——整段就是对每个空该填的同一个值（如「所有空都填 0」的题）。
    返回长度恰为 nblank 的列表：
      · 用了 | 却切不出 nblank 段 → None（模型没按约定来，判没把握，宁可不填）
      · 其他分隔符切出 nblank 段 → 逐空；切不出 → 整段复制 nblank 份（保持旧行为）
    顿号/逗号放最后试：答案内容本身可能含顿号（单空题答案就是「桥接模式、NAT模式」），
    只有刚好切出 nblank 段才认，否则退回整段。
    """
    if nblank <= 1:
        return [a]
    for sep in ('|', '｜'):
        if sep in a:
            parts = [p.strip() for p in a.split(sep) if p.strip()]
            return parts if len(parts) == nblank else None
    for sep in ('\n', '；', ';', '、', '，', ','):
        if sep in a:
            parts = [p.strip() for p in a.split(sep) if p.strip()]
            if len(parts) == nblank:
                return parts
    return [a] * nblank

# 解析作业题。inputs 里带上隐藏的 answertype（题型码）与可点选项。
JS_PARSE_TIMUS = r"""
() => {
  const out = [];
  // 题目容器两版共用：新版答题页（mooc2/work/dowork）每题一个 .questionLi，
  // 而它外层那个 .TiMu 是「整块答题区」（整页只有一个）；旧版（doHomeWorkNew）
  // 反过来——.TiMu 才是每题容器。判据：有 .questionLi 且 .TiMu 不多于 1 个
  // → 用 .questionLi；否则用 .TiMu。不这样判的话，新版会把 3 道题并成 1 条。
  const _lis = [...document.querySelectorAll('.questionLi')];
  const _tms = [...document.querySelectorAll('.TiMu')];
  const _boxes = (_lis.length && _tms.length <= 1) ? _lis : _tms;
  _boxes.forEach((li, idx) => {
    const at = li.querySelector('input[name^=answertype]');
    // 题干提取三级兜底。真机实测踩过：某课作业卡的题干不在 .Zy_TItle 里
    // （结构差异），取到空题干 → 附件规则无从匹配 → 模型只看到「1.【简答】」
    // 就瞎编了一段「进入课程作业中的实验报告，点击上传文件……提交即可」当
    // 答案，工具照填还交了卷，整题 0 分。宁可题干带点噪声，也不能空着。
    let stem = ((li.querySelector('.Zy_TItle') || {}).textContent || '');
    if (!stem.trim()) {
      // .mark_name 是新版每题容器里的题目信息行（题号 + 题型 + 分值）
      const cand = li.querySelector(
        '.mark_name, .Zy_Questions_Con, .TiMu_Title, .quesDetail, .questionCon, p');
      if (cand) stem = cand.textContent || '';
    }
    if (!stem.trim()) {
      // 兜底取整题文本，但要先剔掉富文本编辑器与工具条：它们的占位
      // 文字（「请输入…」）和按钮名会混进题干，把附件规则之类的匹配搅乱
      const clone = li.cloneNode(true);
      clone.querySelectorAll(
        '.edui-default, .eidtDiv, textarea, script, input').forEach(x => x.remove());
      stem = clone.textContent || '';
    }
    stem = stem.replace(/\s+/g, ' ').trim().slice(0, 500);
    const opts = [], fills = [];
    let li_html = null, li_doc = null;
    li.querySelectorAll('input[name^=answer]').forEach(x => {
      if (x.type === 'hidden') return;
      const label = (x.closest('li') || x.parentElement);
      opts.push({name: x.name, val: x.value, type: x.type,
                 label: ((label && label.textContent) || x.value)
                        .replace(/\s+/g, ' ').trim().slice(0, 150)});
    });
    // 页面变体兜底（真机 2026-10-09：atype/题干都读得到，选项 radio 却不在
    // 题容器子树里——选项另起一块的结构）。向上最多走两层找 radio/checkbox；
    // 有 answertype<qid> 时优先只认 name="answer<qid>"，防止把别的题的选项
    // 捞进来（只有该 scope 里压根没有精确匹配项时才放宽收任何 radio）。
    if (!opts.length) {
      const qid = at ? (at.name || '').replace('answertype', '') : '';
      let scope = li;
      for (let k = 0; k < 3 && scope && !opts.length; k++) {
        const exact = qid
          ? [...scope.querySelectorAll('input[name="answer' + qid + '"]')]
              .filter(y => y.type === 'radio' || y.type === 'checkbox')
          : [];
        (exact.length ? exact
          : [...scope.querySelectorAll('input[type=radio],input[type=checkbox]')])
          .forEach(x => {
            if (x.type === 'hidden') return;
            if (exact.length && qid && x.name !== 'answer' + qid) return;
            const label = (x.closest('li') || x.parentElement);
            opts.push({name: x.name, val: x.value, type: x.type,
                       label: ((label && label.textContent) || x.value)
                              .replace(/\s+/g, ' ').trim().slice(0, 150)});
          });
        scope = scope.parentElement;
      }
    }
    // 页面变体②（真机 2026-10-09 线代B）：整页没有 radio，选项是
    // div[onclick=addChoice] + span[data=真实值]，点选后平台 JS 把值写进
    // input[type=hidden][name=answer<qid>]。⚠ 显示字母与真实值**错位**
    // （第 1 个显示 A 但 data=B）——视觉模型按截图里的**显示字母**作答，
    // 所以必须按 DOM 顺序（=显示顺序）收选项、val 记 span 的 data；
    // 落答侧对这种题禁用按值匹配、只走位置映射（见 answer_one）。
    if (!opts.length) {
      const hid2 = li.querySelector(
        'input[type=hidden][name^=answer]:not([name^=answertype])');
      const nm2 = hid2 ? hid2.name : '';
      if (nm2) {
        li.querySelectorAll('[onclick*=addChoice],[role=radio],[role=checkbox]')
          .forEach(box => {
            const sp = box.querySelector('span[data]');
            if (!sp) return;
            const lb = (box.getAttribute('aria-label') || box.textContent)
              .replace(/\s+/g, ' ').trim().slice(0, 150);
            opts.push({name: nm2, val: sp.getAttribute('data') || '',
                       type: 'divradio', label: lb});
          });
      }
    }
    // 仍拿不到选项：把整题结构转储给 page_diag（iframe/自定义控件一次看清）
    if (!opts.length) {
      const c2 = li.cloneNode(true);
      c2.querySelectorAll('script,style').forEach(x => x.remove());
      c2.querySelectorAll('textarea').forEach(x => { x.textContent = ''; });
      li_doc = {
        radios: document.querySelectorAll('input[type=radio]').length,
        answerInputs: document.querySelectorAll('input[name^=answer]').length,
        questionLi: document.querySelectorAll('.questionLi').length,
        timu: document.querySelectorAll('.TiMu').length,
        liIf: li.querySelectorAll('iframe').length};
      li_html = c2.outerHTML.replace(/\s+/g, ' ').trim().slice(0, 6000);
    }
    li.querySelectorAll('textarea, input[type=text]').forEach(x => {
      fills.push({name: x.name || '', tag: x.tagName.toLowerCase()});
    });
    // 「上传附件」型主观题（LLM 替不了）：两条都查，任一命中即判为附件题。
    // ① 题内有**真正的**文件选择控件。⚠ 判据绝不能放宽成「class/id 含 upload」：
    //    超星答题容器里普遍混着富文本编辑器 UEditor 的上传工具条（.edui-upload
    //    一类），拿它判会把普通填空题整份误判成附件题——真机实测：「设齐次线性
    //    方程组…则 k=( )」这道填空题 atype=2，被当成附件题整份跳过。所以只认
    //    input[type=file]，再把编辑器子树里的 upload 类元素剔掉。真要求传附件的
    //    题，它的 file 控件必然落在题目容器里，不会漏。
    // ② 题干点名要上传/提交材料——表述千变万化（「请提交实验报告」「以附件
    //    形式提交」「上传视频」…），动作词+对象词组合着扫，宁宽勿漏：
    //    漏判的代价是乱填一个占位答案拿 0 分，误判只是整份跳过让用户自己做。
    const hasUploader = !!li.querySelector('input[type=file]')
      || [...li.querySelectorAll('[class*=upload],[id*=upload]')].some(
           e => !e.closest('.edui-default, .edui-editor, .edui-toolbar'));
    const stemUpload = /上传附件|提交附件|附件上传|附件提交|以附件|附件形式|拍照上传|上传.{0,8}(报告|文件|文档|图片|视频|音频|压缩包|作品|附件)|提交.{0,8}(报告|文件|文档|压缩包|作品|附件)|(报告|文件|作品|附件).{0,6}(上传|提交)/.test(stem);
    const upload = hasUploader || stemUpload;
    // 题干里是否带图/公式：这类题靠纯文本拿不到题面——超星的数学公式常以图片或
    // MathML 渲染，提取出来是空白或乱码。命中就标记，交给视觉模型「亲眼看」
    // （见 llm_solve 的视觉通道）。判据宁可宽一点：多截一张图只是多花几分钱，
    // 漏了则是整题答不出。
    const hasMedia = !!li.querySelector(
      'img, svg, math, .MathJax, .MathJax_Preview, [class*=formula], [class*=math], canvas');
    // 供截图按题定位（Playwright 侧用 [data-cx-idx="N"] 抓这一题的图）
    li.setAttribute('data-cx-idx', idx);
    out.push({no: idx + 1, stem: stem.slice(0, 500),
              atype: at ? at.value : '',
              opts, fills, upload, hasMedia, li_html, doc_counts: li_doc});
  });
  return out;
}
"""

# 按答案填表：answers = [{name, vals:[选项值...]}]；用 click() 触发平台自己的监听
JS_FILL_ANSWERS = r"""
(answers) => {
  let ok = 0, miss = 0;
  answers.forEach(a => {
    a.vals.forEach(v => {
      const el = [...document.querySelectorAll('input[name="' + a.name + '"]')]
        .find(x => x.value === v && x.type !== 'hidden');
      if (el) { el.click(); ok++; return; }
      // div 选区变体：点 span[data] 所在的选块，再回读 hidden input 校验
      // 真的写进去了——点了个寂寞也按 miss 计，让安全阀拦住不交低分卷
      const hid = document.querySelector(
        'input[type=hidden][name="' + a.name + '"]');
      const base = hid ? (hid.id || hid.name) : '';
      const qid = /^answer\d+$/.test(base) ? base.replace(/^answer/, '') : null;
      const sp = qid
        ? document.querySelector('span.choice' + qid + '[data="' + v + '"]')
        : null;
      const box = sp && sp.closest(
        '[onclick*=addChoice],[role=radio],[role=checkbox]');
      if (box) {
        box.click();
        const got = (hid.value || '').trim();
        if (got === v || got.split(/[,;，；]/).includes(v)) { ok++; return; }
      }
      miss++;
    });
  });
  return {ok, miss};
}
"""

# 主观题填文本：UEditor 实例化后正文在它自己的 iframe 里，textarea 只是壳，
# 直接设 textarea.value 平台根本读不到（实测 serialize 里是空的）。
# 所以必须优先走 UE API：UE.instants 的 key 就是 textarea 的 name。
JS_FILL_TEXT = r"""
(items) => {
  let ok = 0, miss = 0;
  items.forEach(a => {
    let done = false;
    try {
      if (window.UE && UE.instants) {
        for (const k in UE.instants) {
          const e = UE.instants[k];
          if ((e.key || k) === a.name) {
            e.setContent('<p>' + String(a.val)
              .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
              .replace(/\n/g, '<br>') + '</p>');
            done = !!e.getContent();
            break;
          }
        }
      }
    } catch (e) { /* 编辑器未就绪则退回 textarea */ }
    if (!done) {
      const ta = document.querySelector('textarea[name="' + a.name + '"],'
                      + ' input[type=text][name="' + a.name + '"]');
      if (!ta) { miss++; return; }
      ta.value = a.val;
      ta.dispatchEvent(new Event('input', {bubbles: true}));
      ta.dispatchEvent(new Event('change', {bubbles: true}));
      // 兜底：找同容器的编辑器 iframe body 直接写
      const wrap = ta.closest('.TiMu') || document;
      const body = wrap.querySelector('.edui-editor iframe, .edui-editor-body iframe');
      try {
        if (body && body.contentDocument && body.contentDocument.body) {
          body.contentDocument.body.innerHTML = a.val
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/\n/g, '<br>');
        }
      } catch (e) { /* 跨域/未就绪则放弃 */ }
      // ⚠ 走到兜底 = UE API 没写进去。textarea 只是壳，平台 serialize
      // 读不到（实测），iframe 直写也无法确认平台收没收到——一律计为
      // miss，交卷侧会拦下这份卷子，绝不把空简答悄悄交上去。
      miss++;
      return;
    }
    ok++;
  });
  return {ok, miss};
}
"""

# 提交/暂存前必须补的隐藏字段：#answerwqbid = 题目 ID 逗号清单。
# 不补它服务端回「无效的参数：code-1」，保存直接被拒（实测）。
# 权威来源是页面 toadd() 里硬编码的清单，取不到再从 answertype 隐藏域反推。
JS_HAS_QUESTIONS = r"""
() => {
  const lis = document.querySelectorAll('.questionLi').length;
  if (lis) return lis;                       // 新版：每题一个 .questionLi
  const tms = document.querySelectorAll('.TiMu').length;
  if (tms > 1) return tms;                   // 旧版：.TiMu 每题一个
  if (tms === 1) {                           // 旧版单题 / 新版刚渲染出壳
    const t = document.querySelector('.TiMu');
    if (t && (t.querySelector('input[name^=answertype]')
              || t.querySelector('textarea')
              || t.querySelector('input[name^=answer]'))) return 1;
  }
  return 0;
}
"""


JS_PREP_WQB = r"""
() => {
  let ids = '';
  try {
    const m = toadd.toString().match(/var a = "([0-9,]+)"/);
    if (m) ids = m[1];
  } catch (e) {}
  if (!ids) {
    ids = [...document.querySelectorAll('input[id^="answertype"]')]
      .map(x => x.id.replace(/^answertype/, '')).join(',') + ',';
  }
  const el = document.querySelector('#answerwqbid, input[name=answerwqbid]');
  if (el) el.value = ids;
  return ids;
}
"""

# 正式交卷：走平台完整链路 btnBlueSubmit（validate → toadd 设 enc/answerwqbid
# → 字数校验 → form1submit → confirmSubmitWork）。它是异步链，调用后要等几秒。
JS_DO_SUBMIT = r"""
() => {
  // confirmSubmitWork 是真正 POST form1 的函数（form1submit 是它的别名）。
  // 旧版优先调 btnBlueSubmit（UI 入口）——它的异步链停在「确认提交」
  // 弹窗，没人点确定就永远没有 POST，却照样返回 ok（测试账号实测：
  // 两份作业 unverified、重开仍是待完成，根因即此）。
  // 完整性/字数检查由本工具的填答逻辑保证（缺题不提交）；提交次数
  // 超限或平台要求验证码时服务端会拒绝，响应监听如实上报，不假成功。
  if (typeof confirmSubmitWork === 'function') {
    try { confirmSubmitWork(); return 'ok'; }
    catch (e) { return 'err:' + String(e).slice(0, 80); }
  }
  if (typeof form1submit === 'function') {
    try { form1submit(); return 'ok'; }
    catch (e) { return 'err:' + String(e).slice(0, 80); }
  }
  if (typeof btnBlueSubmit === 'function') {
    try { btnBlueSubmit(); return 'ok'; }
    catch (e) { return 'err:' + String(e).slice(0, 80); }
  }
  return 'nofn';
}
"""

# 暂存（不交卷）：saveWork 是平台自己的「临时保存」
JS_DO_SAVE = r"""
() => {
  if (typeof saveWork !== 'function') return 'nofn';
  try { saveWork(); return 'ok'; }
  catch (e) { return 'err:' + String(e).slice(0, 80); }
}
"""


# ================================================================ 大模型接入（v3.8 重做）
# 参考 ToolKnit 的做法：平台预设 + 地址规范化 + 错误分类 + 密钥加密存放。
# 预设表只有后端这一份真相源，界面的下拉直接用它渲染，避免前后端两份漂移。
LLM_PLATFORMS = [
    {'key': 'deepseek', 'label': 'DeepSeek',
     'url': 'https://api.deepseek.com/v1', 'model': 'deepseek-chat',
     'vision_model': ''},          # DeepSeek 开放平台暂无视觉模型
    {'key': 'openai', 'label': 'OpenAI',
     'url': 'https://api.openai.com/v1', 'model': 'gpt-4o-mini',
     'vision_model': 'gpt-4o'},
    {'key': 'qwen', 'label': '通义千问 Qwen',
     'url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
     'model': 'qwen-plus', 'vision_model': 'qwen-vl-max'},
    {'key': 'moonshot', 'label': 'Moonshot Kimi',
     'url': 'https://api.moonshot.cn/v1', 'model': 'moonshot-v1-8k',
     'vision_model': 'moonshot-v1-8k-vision-preview'},
    {'key': 'glm', 'label': '智谱 GLM',
     'url': 'https://open.bigmodel.cn/api/paas/v4', 'model': 'glm-4-flash',
     'vision_model': 'glm-4v-flash'},
    {'key': 'custom', 'label': '自定义（任何 OpenAI 兼容接口）',
     'url': '', 'model': '', 'vision_model': ''},
]

LLM_KEY_FILE = RUNTIME / 'llm_key.dpapi'
# 视觉模型的 Key 单独一份文件：两个模型可能落在不同平台、各用各的 Key，
# 共用一份必然互相覆盖。
LLM_VISION_KEY_FILE = RUNTIME / 'llm_vision_key.dpapi'
# 「用途」→ 该用途的 Key 文件**属性名**（不是路径本身！见 _llm_key_path）。
# 刻意只存属性名、每次现读 globals()：测试沙箱（tmp/_sandbox.py）是在 import
# **之后**把 LLM_KEY_FILE / LLM_VISION_KEY_FILE 改道到临时目录的，
# import 时就把路径收进容器的话改道改不到 —— 那等于「测试以为隔离了，
# 实际在写真实用户的 runtime/llm_key.dpapi」。2026-10-08 被逐套件日志抓到。
_KEY_FILE_ATTR = {'main': 'LLM_KEY_FILE', 'vision': 'LLM_VISION_KEY_FILE'}
_KEY_FILE_NAME = {'main': 'llm_key.dpapi', 'vision': 'llm_vision_key.dpapi'}
# 自定文件头（照 ToolKnit 的 TKDpapi1 路子）：魔数 + 版本号，后面才是 DPAPI 块。
# 带上头是为了「一眼认出这不是普通文本」，也留出以后换算法/换范围的余地。
LLM_KEY_MAGIC = b'CXLLM1'
_LLM_KEY_HEADER = len(LLM_KEY_MAGIC) + 4      # 魔数 + 4 字节小版本号
_LLM_KEY_CACHE = {}      # 用途 → Key（解密一次就缓存，别每道题都解一遍）
_LLM_KEY_TRIED = set()   # 已经去磁盘找过的用途


def _llm_key_purpose(purpose: str) -> str:
    return purpose if purpose in _KEY_FILE_ATTR else 'main'


def _llm_key_path(purpose: str = 'main'):
    """该用途的 Key 文件路径。

    ⚠️ 必须**每次现读**模块级常量，不能预先收进 dict：沙箱是在 import 之后
    改道这些常量的（见 _KEY_FILE_ATTR 的注释）。
    """
    purpose = _llm_key_purpose(purpose)
    p = globals().get(_KEY_FILE_ATTR[purpose])
    if p is None:                       # 常量还没定义出来（老版本/半加载）
        p = RUNTIME / _KEY_FILE_NAME[purpose]
    return p


def llm_key_files() -> dict:
    """{用途: 路径}。给要一次看两份文件的调用方用；同样现读常量。"""
    return {k: _llm_key_path(k) for k in _KEY_FILE_ATTR}


def _llm_key_field(purpose: str) -> str:
    """该用途在 config.json 里的明文字段名（仅加密不可用时兜底）。"""
    return 'llm_key' if purpose == 'main' else 'llm_vision_key'


def llm_platform(key: str) -> dict:
    for p in LLM_PLATFORMS:
        if p['key'] == key:
            return p
    return LLM_PLATFORMS[0]


def guess_llm_platform(url: str) -> str:
    u = (url or '').strip()
    for p in LLM_PLATFORMS:
        if p['url'] and u.startswith(p['url']):
            return p['key']
    return 'custom' if u else 'deepseek'


# ---------------------------------------------------------------- 密钥加密存放
def _dpapi(data: bytes, protect: bool) -> bytes:
    """Windows DPAPI 加/解密（当前用户范围，CRYPTPROTECT_UI_FORBIDDEN 不弹窗）。

    非 Windows 或调用失败都会抛异常，由调用方兜底成明文——「加密不可用」
    不该让工具没法答题。注意必须给函数设 argtypes/restype：64 位下不设
    指针参数会被截断成 32 位（本项目在 DefWindowProcW 上踩过同一个坑）。
    """
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD),
                    ('pbData', ctypes.POINTER(ctypes.c_ubyte))]

    if not data:
        raise ValueError('空数据')
    src_buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    src = BLOB(len(data), src_buf)
    dst = BLOB()
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.restype = wintypes.BOOL
    fn.argtypes = [ctypes.POINTER(BLOB), ctypes.c_void_p, ctypes.POINTER(BLOB),
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                   ctypes.POINTER(BLOB)]
    if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(dst)):
        raise OSError(ctypes.get_last_error() or -1, 'DPAPI 调用失败')
    try:
        return bytes(ctypes.string_at(dst.pbData, dst.cbData))
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def save_llm_key(key: str, purpose: str = 'main') -> bool:
    """把 Key 加密存到该用途的 dpapi 文件；返回是否真的加密存了。

    返回 False = 加密不可用，调用方继续把 Key 留在 config.json（明文），
    并且**不要**去清掉明文，否则用户会两头落空。
    """
    key = (key or '').strip()
    if not key:
        return True
    if os.name != 'nt':
        return False
    try:
        blob = _dpapi(key.encode('utf-8'), protect=True)
    except Exception as e:
        log('API Key 加密失败（将明文存放在 config.json）：%s' % str(e)[:80], 'warn')
        return False
    path = _llm_key_path(purpose)
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_bytes(LLM_KEY_MAGIC + b'\x01\x00\x00\x00' + blob)
        os.replace(tmp, path)             # 原子替换：写一半崩掉不会留坏文件
    except Exception as e:
        log('API Key 写入失败（将明文存放在 config.json）：%s' % str(e)[:80], 'warn')
        return False
    _LLM_KEY_CACHE[purpose] = key
    return True


def load_llm_key(purpose: str = 'main') -> str:
    """读指定用途的加密 Key。文件不存在 / 头不对 / 解不开都返回 ''（不抛异常）。"""
    try:
        raw = _llm_key_path(purpose).read_bytes()
    except Exception:
        return ''
    if not raw.startswith(LLM_KEY_MAGIC) or len(raw) <= _LLM_KEY_HEADER:
        return ''
    try:
        return _dpapi(raw[_LLM_KEY_HEADER:], protect=False).decode('utf-8')
    except Exception as e:
        # 换 Windows 用户或换电脑后 DPAPI 解不开——这是设计使然，不是 bug，
        # 提示用户重填一次即可（旧文件留着不管，下次保存会覆盖）。
        log('已保存的 API Key 解不开（换了 Windows 用户或换了电脑）：%s；'
            '请在「模型」里重新填一次' % str(e)[:60], 'warn')
        return ''


def clear_llm_key(purpose: str = 'main'):
    """清除该用途的加密文件与内存缓存。"""
    _LLM_KEY_CACHE.pop(purpose, None)
    _LLM_KEY_TRIED.discard(purpose)
    try:
        _llm_key_path(purpose).unlink(missing_ok=True)
    except Exception:
        pass


def forget_llm_keys():
    """只忘掉内存里缓存的 Key，**不删文件**——下次取值会重新去磁盘读。

    给「文件被外部改过，要重新读一遍」的场景用（测试、以及用户手动换过
    Key 文件后的重读）。别让调用方去碰 `_LLM_KEY_CACHE` / `_LLM_KEY_TRIED`
    这两个容器：它们的类型以前改过一次（标量 → 按用途分的 dict/set），
    直接赋值 `= ''` 的地方全炸了（2026-10-08 的跑批就是这么红的）。
    """
    _LLM_KEY_CACHE.clear()
    _LLM_KEY_TRIED.clear()


def llm_key_of(cfg: dict, purpose: str = 'main') -> str:
    """取当前可用的 API Key（按用途：main = 答题模型，vision = 视觉模型）。

    加密文件是唯一真相源；config.json 里的明文只在**加密不可用**时兜底。
    老版本（v3.7 及以前）留下的明文在这里一次性迁移进加密文件并从配置里撤掉
    ——否则明文会一直躺着，加密就成了摆设。
    """
    if _LLM_KEY_CACHE.get(purpose):
        return _LLM_KEY_CACHE[purpose]
    field = _llm_key_field(purpose)
    if purpose not in _LLM_KEY_TRIED:
        _LLM_KEY_TRIED.add(purpose)
        stored = load_llm_key(purpose)
        legacy = (cfg.get(field) or '').strip()
        if legacy:
            if save_llm_key(legacy, purpose):
                _drop_plain_key(cfg, purpose)
                log('已把 API Key 改为加密存放（config.json 里不再留明文）'
                    if purpose == 'main' else
                    '已把视觉模型的 API Key 改为加密存放（config.json 里不再留明文）')
                return legacy
            return legacy                # 加密不可用：明文继续用着，不折腾用户
        if stored:
            _LLM_KEY_CACHE[purpose] = stored
            return stored
    return (cfg.get(field) or '').strip()


def _drop_plain_key(cfg: dict, purpose: str = 'main'):
    """把 config.json 里该用途的明文 Key 撤掉（只在加密确实落盘之后调用）。

    用 patch_config 只动一个字段：cfg 可能只是调用方拼的半个 dict
    （界面「测试连接」那条路径就是），整体 save_config 会顺手把用户其余
    设置抹掉。
    """
    field = _llm_key_field(purpose)
    try:
        patch_config({field: ''})
        cfg[field] = ''
    except Exception as e:
        log('明文 Key 清理失败（不影响使用）：%s' % str(e)[:60], 'warn')


def mask_key(key: str) -> str:
    """脱敏展示：只露前缀和后四位，够用户认出是哪把 Key。"""
    k = (key or '').strip()
    if not k:
        return ''
    if len(k) <= 12:
        return k[:3] + '…'
    return k[:6] + '…' + k[-4:]


def llm_ready(cfg: dict) -> bool:
    """三项齐了才算配置完成——Key 存在加密文件里也要算数。"""
    return bool((cfg.get('llm_url') or '').strip()
                and (cfg.get('llm_model') or '').strip()
                and llm_key_of(cfg))


def llm_vision_ready(cfg: dict) -> bool:
    """视觉模型是否配齐。

    四项**全空** = 用户选择不启用视觉（完全合法，行为退回纯文本答题）；
    只要填了其中任意一项，就必须填全，否则算「配了一半」——这种情况按未启用
    处理，但调用方会给出明确提示，免得用户以为填了个地址就能看图。
    """
    url = (cfg.get('llm_vision_url') or '').strip()
    model = (cfg.get('llm_vision_model') or '').strip()
    if not url and not model and not (cfg.get('llm_vision_platform') or '').strip():
        return False
    return bool(url and model and llm_key_of(cfg, 'vision'))


def llm_vision_configured(cfg: dict) -> bool:
    """用户是否**试图**启用视觉（填了任意一项）——用于给「配了一半」的提示。"""
    return any((cfg.get(k) or '').strip() for k in
               ('llm_vision_platform', 'llm_vision_url',
                'llm_vision_model', 'llm_vision_key')) or bool(
        llm_key_of(cfg, 'vision'))


# ---------------------------------------------------------------- 地址规范化
LLM_LOOPBACK = ('127.0.0.1', 'localhost', '::1', '[::1]')


def _is_private_host(host: str) -> bool:
    """本机 / 内网地址。本地跑 Ollama、LM Studio、vLLM 这类只有 http。"""
    h = (host or '').lower()
    if h in LLM_LOOPBACK:
        return True
    if h.startswith('192.168.') or h.startswith('10.'):
        return True
    if h.startswith('172.'):
        try:
            return 16 <= int(h.split('.')[1]) <= 31
        except (IndexError, ValueError):
            return False
    return False


def normalize_llm_url(raw: str):
    """把用户填的地址变成可直接 POST 的 /chat/completions 地址。

    三种写法都接受（返回 (url, '')）：
      https://api.deepseek.com        → https://api.deepseek.com/v1/chat/completions
      https://api.deepseek.com/v1     → https://api.deepseek.com/v1/chat/completions
      …/v1/chat/completions（完整）    → 原样使用
    出错返回 ('', 中文原因)。

    v3.7 及以前只按「结尾不是 /v1 就补 /v1/chat/completions」猜，用户从
    文档里粘一个完整地址就会拼成 …/chat/completions/v1/chat/completions，
    然后拿到一句 404——地址栏怎么会错，用户根本无从判断。
    """
    from urllib.parse import urlsplit
    u = (raw or '').strip().rstrip('/')
    if not u:
        return '', '请先填接口地址'
    parts = urlsplit(u)
    if parts.scheme not in ('http', 'https'):
        return '', '地址要以 http:// 或 https:// 开头'
    if not parts.netloc:
        return '', '地址不完整，缺少域名'
    # 从浏览器地址栏直接粘过来的地址常带 #/?：拼进请求地址只会得到一个 404，
    # 而且用户看着自己粘的「对」地址完全不知道错在哪。在这里就拦掉并说清楚。
    if parts.fragment:
        return '', '地址里不要带 # 后面的内容（那是网页锚点，不是接口地址）'
    if parts.query:
        return '', '地址里不要带 ? 后面的参数'
    if parts.scheme == 'http' and not _is_private_host(parts.hostname or ''):
        return '', 'http:// 只允许本机或内网地址；公网服务请用 https://'
    path = parts.path.rstrip('/')
    if path.endswith('/chat/completions'):
        return u, ''
    if path.endswith('/v1') or path.endswith('/v4'):
        return u + '/chat/completions', ''
    return u + '/v1/chat/completions', ''


# ---------------------------------------------------------------- 错误分类
# 把 HTTP/网络异常翻成「用户照着能改」的中文。要不要重试也在这里定：
# 4xx 是我们这边填错了（地址/密钥/模型名），重试只是白等，还可能把同一把
# Key 反复撞进平台的失败计数；只有网络、超时、429、5xx 值得重试。
LLM_ERR = {
    400: '请求被拒（多半是模型名不对，或该模型不支持这种调用）',
    401: 'API Key 无效或已失效',
    403: 'API Key 没有权限（额度、白名单或未开通该模型）',
    404: '地址不对：确认填到 /v1 为止（也可以直接粘完整地址）；也可能是模型名不存在',
    408: '对方服务端超时',
    413: '请求内容过大',
    422: '参数不被接受（检查模型名）',
    429: '触发限流，或账户余额/额度不足',
    500: '对方服务端故障',
    502: '对方网关错误',
    503: '对方服务暂时不可用（可能过载）',
    504: '对方网关超时',
}


def _llm_retryable(status) -> bool:
    """status=None 表示网络层异常（超时、连不上）。"""
    return status is None or status == 429 or status >= 500


def _llm_err_text(status: int, body: str) -> str:
    base = LLM_ERR.get(status)
    if base is None:
        base = ('对方服务端错误（HTTP %d）' % status if status >= 500
                else '请求失败（HTTP %d）' % status)
    detail = ''
    try:
        d = json.loads(body or '{}')
        src = d.get('error') if isinstance(d, dict) else None
        if isinstance(src, str):
            detail = src
        elif isinstance(src, dict):
            detail = str(src.get('message') or src.get('msg') or '')
        if not detail and isinstance(d, dict):
            detail = str(d.get('message') or '')
    except Exception:
        t = (body or '').strip()
        detail = '返回的是网页而不是接口数据' if t.startswith('<') else t[:120]
    detail = re.sub(r'\s+', ' ', detail).strip()[:160]
    return (base + '：' + detail) if detail else base


def _llm_net_err_text(e: Exception) -> str:
    s = '%s' % e
    low = s.lower()
    if 'timed out' in low or 'timeout' in low:
        return '连接超时（网络不通，或对方长时间不响应）'
    if ('getaddrinfo' in low or 'name or service' in low
            or 'nodename nor servname' in low):
        return '域名解析不了（地址拼写有误，或本机 DNS/代理有问题）'
    if 'ssl' in low or 'certificate' in low:
        return 'HTTPS 握手失败（对方证书异常，或本机有代理在中间拦）'
    if 'refused' in low:
        return '连接被拒绝（地址或端口不对；本地模型确认服务已启动）'
    return s[:160]


LLM_MAX_BYTES = 2 * 1024 * 1024     # 响应体上限，防对方回超大内容把内存吃满


def llm_chat(cfg, prompt, timeout=120, key=None, images=None, purpose=None):
    """OpenAI 兼容 /chat/completions。返回回复文本；失败抛 RuntimeError。

    v3.8：先规范化地址（base 或完整地址都行）、错误分类成中文、
    4xx 不重试、响应体限长、输出被长度限制截断时给明确提示。

    key 显式传入时直接用它——「测试连接」要能测**刚填进界面、还没保存**
    的 Key；否则 llm_key_of 会优先吐内存里缓存的那把旧 Key，用户改了 Key
    点测试却测的是旧的，测通了再保存反而失败（最坏的一种误导）。

    images：PNG / JPEG 字节列表。一旦带了图就自动改走**视觉模型**那套配置
    （llm_vision_url / llm_vision_model / llm_vision_key）——文本模型看不了
    图，拿文本配置发图片只会被对方回 400。purpose 可强制指定走哪一套
    （「测试连接」要能分别测两边，传 'main' / 'vision'）。
    """
    import urllib.request
    import urllib.error
    import base64
    if purpose is None:
        purpose = 'vision' if images else 'main'
    vis = purpose == 'vision'
    url, err = normalize_llm_url(
        cfg.get('llm_vision_url' if vis else 'llm_url'))
    if err:
        # 视觉模型也走这条函数，报错必须点明是哪一边：只说「接口地址有问题」
        # 会把人引去改主模型的地址，怎么改都不对。
        raise RuntimeError(('视觉模型的接口地址有问题：' if vis
                            else '接口地址有问题：') + err)
    key = (key if key is not None else llm_key_of(cfg, purpose)) or ''
    key = key.strip()
    if not key:
        raise RuntimeError(
            '视觉模型还没填 API Key：请在「模型」的视觉模型里填一次并保存'
            if vis else '没有可用的 API Key：请在「模型」里填一次并保存')
    model = (cfg.get('llm_vision_model' if vis else 'llm_model') or '').strip()
    if not model:
        raise RuntimeError(
            '视觉模型还没填模型名（如 qwen-vl-max / glm-4v-flash）'
            if vis else '还没填模型名（如 deepseek-chat）')
    content = prompt
    if images:
        content = [{'type': 'text', 'text': prompt}]
        for b in images:
            # 按真实字节头挑 MIME：截图走 JPEG（小），也允许外部塞 PNG 进来
            mime = ('image/png' if bytes(b[:8]) == b'\x89PNG\r\n\x1a\n'
                    else 'image/jpeg')
            content.append({'type': 'image_url', 'image_url': {
                'url': 'data:%s;base64,%s'
                       % (mime, base64.b64encode(b).decode('ascii'))}})
    body = json.dumps({
        'model': model,
        'messages': [{'role': 'user', 'content': content}],
        'temperature': 0,
    }).encode('utf-8')
    last = ''
    for attempt in (1, 2, 3):
        status = 0
        try:
            req = urllib.request.Request(url, data=body, method='POST',
                                         headers={'Content-Type': 'application/json',
                                                  'Authorization': 'Bearer ' + key})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read(LLM_MAX_BYTES + 1)
            if len(raw) > LLM_MAX_BYTES:
                # 放这么大一块多半是地址指错了（回了个下载页/日志页）。
                # 别重试：同样会把 2MB 再拉两遍。
                last = '对方返回的内容超过 2MB，已放弃解析'
                break
            try:
                data = json.loads(raw.decode('utf-8', 'replace'))
            except Exception:
                # HTTP 200 但不是 JSON：典型是网关/代理的拦截页，或地址指到了网页。
                # 这属于「填错了」不是「网络抖了」，重试三次只是白等。
                last = '对方返回的不是接口数据（HTTP 200 却是网页或纯文本）：'\
                       '确认地址填的是接口地址，中间没有代理拦截页'
                break
            choice = (data.get('choices') or [{}])[0] or {}
            msg = choice.get('message') or {}
            # 推理模型（如 deepseek-reasoner）可能把最终答案放
            # reasoning_content、content 为空——两者都试。
            txt = (msg.get('content') or '').strip()
            if not txt:
                txt = (msg.get('reasoning_content') or '').strip()
            if txt:
                return txt
            fin = str(choice.get('finish_reason') or '')
            last = ('对方返回了空内容' + ('（输出被长度上限截断：换个模型，'
                                        '或把题干拆短再试）' if fin == 'length' else ''))
            break                      # 200 但没内容：重试几乎没用，别白花钱
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                raw = e.read(LLM_MAX_BYTES + 1)
            except Exception:
                raw = b''
            last = _llm_err_text(status, raw.decode('utf-8', 'replace'))
            if not _llm_retryable(status):
                break
        except RuntimeError:
            raise
        except Exception as e:
            status = 0
            last = _llm_net_err_text(e)
        if attempt < 3:
            time.sleep(2 * attempt)
    raise RuntimeError('大模型调用失败：%s' % last)



def _parse_llm_answers(txt, n, types=None):
    """从 LLM 回复里抠出 {题号: 答案}。容忍 ```json 包裹、单引号、多余文字。

    推理模型的 reasoning_content 可能很长且带干扰花括号：找出全部候选
    JSON 片段，从后往前（答案通常在结尾）取第一个能解析且含数字键的。
    types = {题号: atype}：传入时按题型精确规范化（选择题才大写字母、
    判断题才转对错，简答/填空文本原样保留）；未传时用启发式（≤4 个
    字母才大写化，避免把纯字母的简答文本误当选项）。
    注意 n 是**最大题号**不是题数——交卷复用预演答案时传入的是带空洞
    的子集（如全卷 6 题只重问第 3、6 题），按题数设上限会把模型如实
    返回的「3」「6」全丢掉，这份卷子就永远交不了（实测踩过）。
    """
    import re
    cands = re.findall(r'\{[^{}]*\}', txt, re.S)
    for cand in reversed(cands):
        try:
            raw = json.loads(cand.replace("'", '"'))
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        out = {}
        for k, v in raw.items():
            try:
                i = int(k)
            except (TypeError, ValueError):
                continue
            if not (1 <= i <= n):
                continue
            # 多空填空题模型可能直接给数组（["桥接模式","NAT模式","仅主机模式"]）：
            # 用 | 连接后交给填空切分逐空填；纯数字答案（如判断题给了 1/0）转字符串。
            # 以前这里只认 str，数组会被当解析失败整题丢掉——白花一次模型钱。
            if isinstance(v, list):
                v = '|'.join(str(x).strip() for x in v if str(x).strip())
            elif isinstance(v, bool):
                continue
            elif isinstance(v, (int, float)):
                v = str(v)
            if not isinstance(v, str):
                continue
            v = v.strip()
            t = (types or {}).get(i)
            if t in ('0', '1'):                  # 选择题：大写字母
                v = re.sub(r'[^A-Za-z]', '', v).upper()
            elif t == '3':                       # 判断题：统一对/错
                v = '对' if v in ('对', '正确', 'TRUE', 'T', '√', 'true') else '错'
            elif t in ('2', '4'):                # 填空/简答：文本原样
                pass
            elif re.fullmatch(r'[A-Za-z]{1,4}', v):
                v = v.upper()
            elif v in ('TRUE', 'T', '√', 'true'):
                v = '对'
            elif v in ('FALSE', 'F', 'false'):
                v = '错'
            out[i] = v
        if out:
            return out
    return {}


def _stem_key(stem):
    """题干 → 缓存键。去空白后取 sha1 前 16 位；**空题干返回空串**。

    空题干绝不能给一个固定值：调用方（llm_solve）靠「键非空」判断「这题有
    题干可以代表」，返回 sha1('') 的话所有空题干答案就挤在同一个键上，
    换道题就可能拿旧答案去填 —— 而带图的公式题题干常常正是空的。
    读取侧不受影响：空键永远 cache.get 不到东西。
    （2026-10-08 审查发现：守卫写法 `and _stem_key(...)` 对空串恒为真，
    等于这条守卫从来没生效过。）
    """
    import hashlib
    norm = re.sub(r'\s+', '', stem or '')
    if not norm:
        return ''
    return hashlib.sha1(norm.encode('utf-8')).hexdigest()[:16]


def _answer_cache_load():
    """读本地答案缓存（runtime/answer_cache.json）。坏了就当没有。"""
    try:
        return json.loads(ANSWER_CACHE_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _answer_cache_save(cache):
    # 原子写（tmp + replace）：缓存文件几百 KB 且答题中途随时可能崩，
    # 截断式覆盖一旦写一半损坏，整份缓存就全丢了
    import os
    import threading
    tmp = ANSWER_CACHE_FILE.with_suffix(
        '.json.tmp%d' % threading.get_ident())
    try:
        tmp.write_text(json.dumps(cache, ensure_ascii=False),
                       encoding='utf-8')
        os.replace(tmp, ANSWER_CACHE_FILE)
    except Exception as e:
        log('答案缓存写入失败（不影响答题）：%s' % str(e)[:60], 'warn')
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


# ---------------------------------------------------------------- 视觉识别
# 题面是图片 / 公式的题（超星的数学公式常以图片或 MathML 渲染），纯文本提取拿到
# 的往往是空白或乱码——光靠文字模型怎么也答不出，还可能被判成「题干读不出来」
# 整份跳过。配了视觉模型后，这类题改成「截图 → 让模型亲眼看着题作答」。
def _need_vision(t) -> bool:
    """这题要不要交给视觉模型看。

    范围：主观题（填空 / 简答），或题干里带图 / 公式的题。不含图的客观选择题
    不截——纯文字选择题给文本就够，省流量也省等待。
    """
    return bool(t.get('hasMedia')) or (t.get('atype') in STYPES)


def _shot_timu(hw, idx):
    """把第 idx 道题（0 基）整块截成 JPEG 字节；失败返回 None。

    定位靠 JS 解析时打的 data-cx-idx（与 JS_PARSE_TIMUS 的 _boxes 同序）。
    整题连题干带公式带选项一起截，模型才看得全。走 JPEG q60：题面多是文字
    与线条，压完通常几十 KB，比 PNG 小一个数量级，字照样清楚。
    """
    try:
        el = hw.query_selector('[data-cx-idx="%d"]' % idx)
        if not el:
            return None
        return el.screenshot(type='jpeg', quality=60)
    except Exception as e:
        log('    第 %d 题截图失败（%s），这题按纯文本处理。'
            % (idx + 1, str(e)[:60]), 'warn')
        return None


def _attach_shots(hw, timus, cfg):
    """给需要看图的题挂上截图（t['shot']）。视觉模型没配就原样返回。

    「配了一半」（填了地址却没填 Key 之类）会明确提示一句——否则用户会以为
    填了地址就能看图，白等一场还不知道为什么没生效。
    """
    if not llm_vision_ready(cfg):
        if llm_vision_configured(cfg):
            log('    视觉模型没配全（地址 / 模型名 / Key 要都填上），'
                '这次仍按纯文本答题。', 'warn')
        return timus
    want = [t for t in timus if _need_vision(t)]
    if not want:
        return timus
    got = 0
    for t in want:
        png = _shot_timu(hw, t['no'] - 1)
        if png:
            t['shot'] = png
            got += 1
    if got:
        log('    已给 %d/%d 道题截了图（交给视觉模型 %s 看图作答）。'
            % (got, len(want), (cfg.get('llm_vision_model') or '').strip()))
    else:
        log('    这 %d 道题都没截到图，按纯文本答题。' % len(want), 'warn')
    return timus


def _clean_vision_answer(a: str) -> str:
    """收拾视觉模型的回答：只留答案本身；'?' 表示没把握（按未答处理）。"""
    s = (a or '').strip()
    if not s:
        return ''
    # 只取第一行：模型偶尔会在答案后面补一句解释
    s = s.splitlines()[0].strip()
    s = re.sub(r'^(答案|答)\s*[:：]\s*', '', s).strip()
    s = s.strip('"\'“”「」『』（）()').strip()
    if s in ('?', '？', '无', '未知'):
        return ''
    return s


def _ask_one_vision(cfg, t):
    """把一道题的截图交给视觉模型作答，返回原始回答文本。"""
    tn = ATYPES.get(t['atype']) or STYPES.get(t['atype']) or '其他'
    nb = len(t.get('fills') or [])
    if t['atype'] == '3':
        hint = '判断题：只输出「对」或「错」。'
    elif t['atype'] == '2' and nb > 1:
        hint = ('填空题，共 %d 个空：按横线先后顺序逐个给答案，'
                '空与空之间用 | 分隔。' % nb)
    elif t['atype'] == '2':
        hint = '填空题：只输出空里应填的内容本身。'
    elif t['atype'] == '4':
        hint = '简答题：150 字以内直接作答，不要客套话。'
    else:
        hint = '选择题：只输出所选选项的大写字母，多选连写（如 AC）。'
    stem = (t.get('stem') or '').strip()
    parts = ['图片是作业里的一道%s题，请**直接看图**作答。%s' % (tn, hint)]
    if stem:
        # 提取到的题干可能因公式渲染而残缺，说明以图为准，免得模型被误导
        parts.append('（页面提取的题干文字，可能与图不一致，以图中的题目为准）：'
                     + stem[:400])
    opts = [o['label'] for o in (t.get('opts') or [])]
    if opts and t['atype'] not in STYPES:
        parts.append('页面给的选项：\n' + '\n'.join('   ' + x for x in opts))
    parts.append('只输出答案本身：不要题号、不要解释、不要任何多余文字。'
                 '如果图上是要求上传文件/报告的题，或者你看不清、没把握，'
                 '就只输出一个 ?。')
    return llm_chat(cfg, '\n'.join(parts), timeout=180, images=[t['shot']])


def _solve_by_vision(cfg, timus, progress):
    """逐题看图作答。返回 {题号: 答案}；没把握或调用失败的题不出现。

    逐题发而不是整卷一次发：一题一张图时模型不会把题号串起来，答完也好对应；
    这类题（公式 / 图表）通常就那么一两道，多几次调用换准确率是划算的。
    """
    out = {}
    for t in timus:
        try:
            a = _clean_vision_answer(_ask_one_vision(cfg, t))
        except RuntimeError as e:
            progress('    第 %d 题视觉模型调用失败：%s'
                     % (t['no'], str(e)[:110]), 'warn')
            continue
        if a:
            out[t['no']] = a
            progress('    第 %d 题（看图作答）→ %s' % (t['no'], a[:60]))
        else:
            progress('    第 %d 题看图后仍没把握，按未答处理。' % t['no'])
    return out


def llm_solve(cfg, timus, progress):
    """把整卷发给大模型。返回 {题号: 答案}，没答上的题不出现。

    客观题答案是大写字母/对错；主观题（填空/简答）答案是文本。
    「上传附件」型主观题（upload=True）不发给模型，替不了。
    配了视觉模型时，带了截图（t['shot']）的题改走**视觉通道**——题面是图片或
    公式的题纯文本拿不到内容，只能让模型亲眼看。
    本地答案缓存：按题干哈希查 runtime/answer_cache.json，命中直接复用
    （同题干=同一道题，不重复花模型的钱）；模型答上的新题写回缓存。
    """
    # 题干压根没取到的题不发给模型：模型看不到题干会瞎编一段「这题该怎么
    # 操作」当答案（真机实测被填进卷子拿 0 分），白白花钱还误导。
    # 留给 answer_one 判成「需人工」。
    # ⚠ 例外：已经截了图的题照发——题干提不出来正是公式/图片题的典型症状，
    # 而模型看图就能读题，这批题恰恰是视觉通道要救的。
    todo = [t for t in timus
            if not t.get('upload')
            and (t.get('shot') or not _stem_missing(t.get('stem')))]
    if not todo:
        return {}
    # ---- 先查本地缓存 ----
    cache = _answer_cache_load()
    ans, rest = {}, []
    for t in todo:
        # 有图的题不查缓存也不用缓存：截图内容（图形 / 公式）没法用题干哈希
        # 代表，拿旧答案复用有答错的风险，宁可多花一次调用。
        if not t.get('shot'):
            hit = cache.get(_stem_key(t.get('stem')))
            if hit:
                ans[t['no']] = hit
                continue
        rest.append(t)
    if ans:
        progress('    本地答案缓存命中 %d/%d 题%s。' % (
            len(ans), len(todo), '，其余问模型' if rest else ''))
    if not rest:
        return ans
    todo = rest
    vis = [t for t in todo if t.get('shot')]
    txt = [t for t in todo if not t.get('shot')]
    got = {}
    # ---- 视觉通道：带截图的题，逐题「看图作答」 ----
    if vis:
        progress('    有 %d 道题带截图，走视觉模型。' % len(vis))
        got.update(_solve_by_vision(cfg, vis, progress))
    # ---- 文本通道：其余题照旧整卷一次发 ----
    if txt:
        lines = []
        for t in txt:
            tn = ATYPES.get(t['atype']) or STYPES.get(t['atype']) or '其他'
            line = '%d.【%s】%s' % (t['no'], tn, t['stem'])
            if t['atype'] == '3':
                line += '（判断题：对/错）'
            elif t['atype'] == '2':
                nb = len(t.get('fills') or [])
                if nb > 1:
                    # 多空填空必须点明空数：不点明模型会把「A、B、C」当成一整个
                    # 答案返回，工具再把它灌进每个空 → 3 个空全错（真机实测踩过）。
                    line += ('（填空，共 %d 个空：按横线出现的先后顺序逐个给答案，'
                             '空与空之间用 | 分隔，例如 桥接模式|NAT模式|仅主机模式）'
                             % nb)
                else:
                    line += '（填空：只输出空里应填的内容本身）'
            elif t['atype'] == '4':
                line += '（简答：150 字以内直接作答，不要客套话）'
            else:
                for o in t['opts']:
                    line += '\n   %s' % o['label']
            lines.append(line)
        prompt = ('你是答题助手。请回答下面的题目。\n'
                  '输出要求：只输出一个 JSON 对象，键为题号（字符串），值为答案。\n'
                  '单选/多选输出大写字母（多选如 "AC"）；判断题输出 "对" 或 "错"；\n'
                  '填空只输出应填内容本身，多空题按空序用 "|" 分隔；'
                  '简答输出答案文本（150 字内）。\n'
                  '凡题干要求上传附件/文件/报告、或需要粘贴外部资料的题，输出 "?"。\n'
                  '没有把握的题输出 "?"。不要输出 JSON 以外的任何文字。\n\n'
                  + '\n\n'.join(lines))
        txtresp = llm_chat(cfg, prompt)
        # 上限用真实最大题号而非题数：todo 可能是带空洞的子集
        # （交卷复用预演答案时只重问没答上的题，题号仍是整卷序号）
        got.update(_parse_llm_answers(
            txtresp, max((t['no'] for t in timus), default=0),
            types={t['no']: t['atype'] for t in txt}))
    # 拒答拦截：「无法代为提交…」「我不能替你…」这类话不算答案。
    # 乱码题干（平台字体反爬）会诱发拒答，拒答文本绝不能写进作业。
    for t in todo:
        a = got.get(t['no'])
        if a and t['atype'] in STYPES and _REFUSE_RE.search(str(a)):
            progress('    第 %d 题模型拒答（%s…），按没把握处理。'
                     % (t['no'], str(a)[:24]))
            del got[t['no']]
    ans.update(got)
    # 新答上的题写回缓存（'?' = 没把握，绝不能进缓存）。
    # 带图的题题干常常为空，哈希键为空时跳过，免得把不同题混成一个键。
    fresh = {t['no']: t for t in todo
             if ans.get(t['no']) not in (None, '?', '')
             and _stem_key(t.get('stem'))}
    if fresh:
        for t in fresh.values():
            cache[_stem_key(t.get('stem'))] = ans[t['no']]
        _answer_cache_save(cache)
    progress('    大模型返回：%s' % json.dumps(ans, ensure_ascii=False)[:200])
    return ans


_GRADED_TEXT_MARKS = ('本次成绩', '重新测试')


def _looks_graded(frame):
    """doHomeWorkNew 帧是否其实渲染的是「已批阅」内容。

    平台对已批阅作业不一定 302 到 YiPiYue：强制解析出的最终地址、
    以及部分场景的自然渲染，都是 doHomeWorkNew 的 URL，但页面里
    是批阅详情（本次成绩 x 分 / 重新测试按钮）。只看 URL 会把
    已交上的作业永远判成「还没翻转」（22:23 核实两份 100 分作业
    三轮翻不了案的实测教训），必须看正文。
    """
    try:
        if 'selectWorkQuestionYiPiYue' in (frame.url or ''):
            return True
        txt = frame.evaluate(
            '() => document.body ? document.body.innerText : ""') or ''
    except Exception:
        return False
    return any(m in txt for m in _GRADED_TEXT_MARKS)


def _wait_hw_iframe(page, url, log):
    """打开 cards 页等 doHomeWorkNew 内层 iframe。

    作业卡结构是两跳异步重定向：壳(ananas/modules/work) →
    中间层(api/work?needRedirect=true) → 内层(doHomeWorkNew)。
    中间层的跳转平台侧**每次独立地时灵时不灵**（实测约 1/4 成功率，
    与时间窗/冷却无关），所以光等和重开都靠不住。策略：
      ① 快速路径：6 秒内连壳都没有 → 非作业节点提前收工；
      ② 壳在而内层没出 → 用页面 cookie 直接请求中间层 URL 解析出
         最终答题页地址（请求层不受骰子影响），直接塞给壳内 iframe
         强制加载；
      ③ 仍不出 → 重开一次 cards，再走一遍。
    返回 (hw | None, note)。
    """
    for attempt in (1, 2):
        try:
            page.goto(url, wait_until='domcontentloaded')
        except Exception as e:
            log('    打开章节页失败：%s' % str(e)[:80], 'warn')
            return None, 'failed'
        deadline = time.time() + 25
        shell_dl = time.time() + 6
        forced = False
        while time.time() < deadline:
            hw = next((f for f in page.frames
                       if 'doHomeWorkNew' in (f.url or '')), None)
            if hw:
                # URL 是 doHomeWorkNew 不代表还能作答——已批阅的作业
                # 也可能渲染在这个 URL 下（见 _looks_graded 注释）
                time.sleep(0.8)          # 给正文一点渲染时间
                if _looks_graded(hw):
                    return hw, 'already'
                return hw, ''
            # 已经交过卷的作业，平台渲染的是「已批阅」页而不是答题页——
            # 这不是渲染失败，别让用户对着 ✗失败 以为没交上（实测踩过）
            done = next((f for f in page.frames
                         if 'selectWorkQuestionYiPiYue' in (f.url or '')), None)
            if done:
                return done, 'already'
            shell = next((f for f in page.frames
                          if 'ananas/modules/work' in (f.url or '')), None)
            if time.time() > shell_dl and shell is None:
                return None, 'nocard'    # 无壳 = 视频/文档等非作业节点
            if shell is not None and time.time() > shell_dl and not forced:
                forced = True            # 壳在而内页不出 → 强制解析跳转链
                try:
                    mid = shell.evaluate(
                        "() => { const f = document.querySelector('iframe');"
                        " return f ? f.src : ''; }")
                    if mid:
                        r = page.request.get(mid)      # 共享 cookie，跟随 302
                        final = r.url
                        if 'doHomeWorkNew' in final:
                            log('    内页跳转没发生（平台偶发），已强制解析出'
                                '答题页地址，直接加载…')
                            shell.evaluate(
                                "u => { const f = document.querySelector('iframe');"
                                " f.src = u; }", final)
                except Exception:
                    pass
            time.sleep(0.5)
        if attempt == 1:
            log('    作业卡内页还是没出来，隔 3 秒重开一次…', 'warn')
            time.sleep(3)
    return None, 'norender'


def _open_work_page(page, url, log):
    """打开「作业列表」里的直达链接，等答题区就位。

    与 _wait_hw_iframe 的区别：那是旧链路（cards 页 → 壳 → 中间层 → 内层
    iframe 的 doHomeWorkNew）；这里是作业列表给的 mooc2/work/task 直链——
    平台会 302 到 mooc2/work/dowork，**整页渲染、主 frame 就是答题页**，
    题目容器是 .questionLi（真机只读探测确认）。少了两跳异步重定向，
    所以不再需要「强制解析跳转链」那套兜底。

    返回 (frame | None, note)：'' 就位 ｜ 'already' 已批阅 ｜
    'norender' 两次都没渲染出来 ｜ 'failed' 打开失败。
    """
    for attempt in (1, 2):
        try:
            page.goto(url, wait_until='domcontentloaded')
        except Exception as e:
            log('    打开作业页失败：%s' % str(e)[:80], 'warn')
            return None, 'failed'
        deadline = time.time() + 25
        while time.time() < deadline:
            # 已交过的作业平台渲染的是批阅页而不是答题页——别让用户对着
            # ✗失败 以为没交上（_looks_graded 注释里记着的老教训）
            for f in page.frames:
                try:
                    if 'selectWorkQuestionYiPiYue' in (f.url or ''):
                        return f, 'already'
                except Exception:
                    pass
            try:
                if _looks_graded(page.main_frame):
                    return page.main_frame, 'already'
            except Exception:
                pass
            for f in page.frames:
                try:
                    if f.evaluate(JS_HAS_QUESTIONS):
                        return f, ''
                except Exception:
                    continue
            time.sleep(0.5)
        if attempt == 1:
            log('    答题区还没渲染出来，隔 3 秒重开一次…', 'warn')
            time.sleep(3)
    return None, 'norender'


def _same_stem(a, b):
    """两段题干是否指同一道题（去空白后比前 60 字）。

    交卷复用预演答案时靠它把关：题干对不上宁可重新问模型，
    也不能把上一份的答案填进这一份。
    """
    a = re.sub(r'\s+', '', a or '')
    b = re.sub(r'\s+', '', b or '')
    if not a or not b:
        return False
    return a[:60] == b[:60]


def _submit_ok(resp_seen):
    """提交窗口内抓到的响应里，是否有「平台受理」的标志。"""
    for x in resp_seen:
        if x.get('st') != 200:
            continue
        b = (x.get('body') or '').replace(' ', '')
        if ('"status":true' in b or '"status":1' in b
                or '"status":"true"' in b or '提交成功' in b):
            return True
    return False


# 选项位置兜底用的字母表（测试证伪杠杆：置空即可让位置映射全军覆没）
_POS_LETTERS = 'ABCDEFG'


def _fallback_by_position(t, vals):
    """按选项**位置**把字母答案映射到选项，返回 (names, vals)；失败 (set(), None)。

    背景（2026-10-09 真机实测）：部分答题页选项 radio 的 value 不是 A/B/C/D
    字面量（选项 ID 或被字体反爬搅乱的文本），「按 value 匹配字母」全军覆没，
    模型看图答对的题整份被判「没答上」→ 白白作废。截图里选项的呈现顺序就是
    DOM 顺序，字母序号与选项位置一一对应，按位置取该选项自己的 value 再交给
    页面去点，对 value 的任何形态都成立。判断题只有两个选项：对=第 1 个、
    错=第 2 个（平台判卷按选项值，顺序是固定的）。
    """
    opts = t.get('opts') or []
    if t['atype'] == '3':
        # 判断题：vals 已归一成「对/错」或 true/false，统一回「对/错」再定位
        idx = 0 if ('对' in vals or 'true' in vals) else (
            1 if ('错' in vals or 'false' in vals) else -1)
        if not (0 <= idx < len(opts)) or not opts[idx].get('name') \
                or not opts[idx].get('val'):
            return set(), None
        return {opts[idx]['name']}, [opts[idx]['val']]
    out_names, out_vals = set(), []
    for ch in vals:
        i = _POS_LETTERS.find(str(ch).strip().upper())
        if not (0 <= i < len(opts)):
            return set(), None
        o = opts[i]
        if not o.get('name') or not o.get('val'):
            return set(), None
        out_names.add(o['name'])
        out_vals.append(o['val'])
    return out_names, out_vals or None


def _dump_page_diag(t, why):
    """把「模型答了却落不下去」的题面结构转储到 runtime/page_diag.jsonl。

    这类题十有八九是答题页变体（选项没有标准 radio / value 为空），不拿到
    页面的真实结构就没法根治写答。只追加不覆盖；任何失败都吞掉，绝不影响
    答题主流程。
    """
    try:
        rec = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'ver': VERSION,
               'no': t.get('no'), 'atype': t.get('atype'), 'why': why,
               'stem': (t.get('stem') or '')[:80],
               'opts': [{'name': (o.get('name') or '')[:40],
                         'val': str(o.get('val') or '')[:40],
                         'type': o.get('type'),
                         'label': (o.get('label') or '')[:40]}
                        for o in (t.get('opts') or [])],
               'n_fills': len(t.get('fills') or []), 'shot': bool(t.get('shot'))}
        if t.get('li_html'):
            rec['li_html'] = t['li_html']
        if t.get('doc_counts'):
            rec['doc_counts'] = t['doc_counts']
        with open(RUNTIME / 'page_diag.jsonl', 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except Exception:
        pass


def answer_one(page, c, cpi, kid, cfg, submit, progress, pre=None, detail=None,
               url=None):
    """对单个章节节点：有作业卡就解析-作答-填表。

    submit 三态：
      True   = 答完走平台完整交卷链（正式提交，记录成绩）；
      False  = 平台「暂存保存」，不交卷（界面「暂存」就是这个模式，
               v3.9.5 起）。⚠ 章节作业的暂存被平台接受后任务点同样
               立即标记完成，成绩要等正式交卷才有（answer_courses
               侧会向用户播报这一点）；
      'dry'  = 预演：解析题目 + 大模型作答，但**不填表、不保存、不碰
               平台**，零痕迹。答案通过 detail 交回去，列清单由用户
               复核后手动或一键交卷。
    pre: {'answers': {题号: 答案}, 'stems': {题号: 题干}} —— 交卷时复用
         预演阶段的答案，题干对不上的题重新问模型。
    detail: 传 dict 时收回 {answers, stems, total, miss}（dry 模式的产物）。
    url: 作业直达链接（作业列表给的那种 mooc2/work/task 形态）。传了就直连
         该页（新版整页渲染、主 frame 即答题页）；不传则按 kid 拼 cards 链接
         走旧链。两条入口后面的解析 / 判别 / 填表 / 交卷是同一套逻辑。

    返回 'nohw'(没有作业卡) | 'ok'(已暂存) | 'submitted'(已交卷，已核实)
         | 'dry'(预演完成) | 'already'(平台显示已批阅，此前已交)
         | 'unsupported'(含不支持的题型)
         | 'unsolved'(有题没答上) | 'report'(整份是上传附件型，LLM 替不了)
         | 'failed' | 'norender'。
    """
    from urllib.parse import urlencode  # noqa: F401  (仅说明用)
    direct = bool(url)          # True = 作业列表直链（新版 dowork 页）
    if not direct:
        url = CARDS_URL.format(clsid=c['clsid'], cid=c['cid'], kid=kid, cpi=cpi)
    hw, note = (_open_work_page(page, url, log) if direct
                else _wait_hw_iframe(page, url, log))
    if note == 'already':
        log('    平台显示这份作业已批阅（此前已交过），无需再答再交。')
        return 'already'
    if hw is None:
        if note == 'norender':
            log('    作业卡两次都没渲染出答题页。', 'warn')
            return 'norender'
        return 'nohw'                        # 视频/文档卡，答题模块不碰
    # 开卷先「读题」停留：开卷几秒内做完交卷是老师端和风控端都看得见的
    # 时序特征，停留时长随机、与题数无关地拉开
    time.sleep(random.uniform(4.0, 9.0))
    timus = _poll(hw, JS_PARSE_TIMUS, lambda v: bool(v), 12000, 400) or []
    if not timus:
        log('    作业卡打开但没解析到题目（可能结构变了），跳过。', 'warn')
        return 'failed'
    # 视觉识别（可选）：主观题与带图/公式的题先截好图。没配视觉模型就什么都不做，
    # 后续全走纯文本——行为与之前完全一致。
    _attach_shots(hw, timus, cfg)
    # 题目分三类：客观题（选字母/对错）、文本主观题（LLM 写）、附件题（替不了）
    attach = [t for t in timus if t.get('upload') and t['atype'] in STYPES]
    todo = [t for t in timus if not (t.get('upload') and t['atype'] in STYPES)]
    bad = [t['no'] for t in todo if t['atype'] not in ATYPES
           and t['atype'] not in STYPES]
    if bad:
        log('    含 %d 道不支持的题型（第 %s 题），整份跳过。'
            % (len(bad), '、'.join(map(str, bad[:6]))), 'warn')
        return 'unsupported'
    if attach:
        # 保存会让任务点直接标记完成 → 用户就没法再上传报告了，必须整份不碰
        log('    第 %s 题要求上传附件（实验报告/文件），LLM 替不了，'
            '需要你本人完成，整份跳过。'
            % '、'.join(str(t['no']) for t in attach[:6]), 'warn')
        return 'report'
    if not todo:
        log('    这份作业要求上传附件（实验报告/文件），LLM 替不了，'
            '需要你本人完成，跳过。', 'warn')
        return 'report'

    # ---- 预演模式：只出答案，不碰平台 ----
    if submit == 'dry':
        ans = llm_solve(cfg, todo, progress)
        answered = {t['no']: ans[t['no']] for t in todo
                    if ans.get(t['no']) not in (None, '?', '')}
        miss = [t['no'] for t in todo if t['no'] not in answered]
        if detail is not None:
            detail.update({'answers': {str(k): v for k, v in answered.items()},
                           'stems': {str(t['no']): t['stem'] for t in todo},
                           'total': len(timus), 'miss': miss})
        if not answered:
            log('    ⚠ 这一整份都没答上（模型没把握），列入手动清单。', 'warn')
            return 'unsolved'
        log('    ✓ 预演完成：答上 %d/%d 题（未填表未保存，平台零痕迹）。'
            % (len(answered), len(timus)))
        return 'dry'

    # ---- 填表模式（交卷 / 平台暂存）：优先复用预演答案 ----
    ans = {}
    pre_a = (pre or {}).get('answers') or {}
    pre_s = (pre or {}).get('stems') or {}
    reused = 0
    for t in todo:
        a = pre_a.get(str(t['no']))
        if a and a != '?' and _same_stem(pre_s.get(str(t['no'])), t['stem']):
            ans[t['no']] = a
            reused += 1
    rest = [t for t in todo if t['no'] not in ans]
    if reused:
        log('    复用预演答案 %d/%d 题%s。' % (reused, len(todo),
            '，其余重新问模型' if rest else ''))
    if rest:
        ans.update(llm_solve(cfg, rest, progress))
    fills, missing = [], []
    for t in todo:
        a = ans.get(t['no'], '?')
        if not a or a == '?':
            # 题干没有字母/数字 → 大概率被平台字体反爬加密（此类历来是
            # 实验报告/附件题），归到「需人工」而不是「没把握」
            # 主观题另有一条更强的判据「题干连一个字母数字都没有」：正常的
            # 简答/填空题多少会带点英文或数字（专有名词、公式、代码），一个
            # 都不带的多半是平台把字换了（字体反爬），而这类题历来是实验报告
            # /附件题 → 归「需人工」。⚠ 这条只敢用在主观题上：客观题纯中文
            # 题干太常见，用它会误伤一大片。
            stem = t.get('stem') or ''
            # 已经截了图的题不走这条：题干提不出来正是公式/图片题的症状，而模型
            # 看图就能读题。答不上说明模型确实没把握，落到下面按「没答上」处理，
            # 不该冤枉成附件题让整份跳过。
            if t['atype'] in STYPES and not t.get('shot') and (
                    _stem_missing(stem)
                    or not re.search(r'[A-Za-z0-9]', stem)):
                log('    第 %d 题题干读不出来（平台字体反爬/作业卡结构变化，'
                    '此类多为实验报告附件题），需要你本人完成，整份跳过。'
                    % t['no'], 'warn')
                return 'report'
            missing.append(t['no'])
            continue
        # 语义兜底：题干点名要「提交/上传」报告·文件·作品，模型却只给了极短
        # 占位答案（如 "1"）——这类题的答案区本就是给用户传附件的，填字必得
        # 0 分（真机实测：实验报告题被填「1」整题 0 分）。宁可不碰。
        if (t['atype'] in STYPES and not t.get('shot')
                and len(str(a).strip()) <= 2
                and _ATTACH_STEM_RE.search(t.get('stem') or '')):
            log('    第 %d 题题干要求提交/上传材料，模型只给出占位答案「%s」，'
                '判为附件题，整份跳过（保存会让任务点直接完成，'
                '你就没法再传报告了）。' % (t['no'], str(a)[:12]), 'warn')
            return 'report'
        if t['atype'] in STYPES:             # 主观题：文本 → textarea
            if not t['fills']:
                missing.append(t['no'])
                continue
            if t['atype'] == '2' and len(t['fills']) > 1:
                # 多空填空：按空序把答案切开逐空填。v3.6 前是每个空都灌整串，
                # 真机实测「桥接模式、NAT模式、仅主机模式」题 3 个空全错。
                parts = _split_fill(a, len(t['fills']))
                if parts is None:
                    log('    第 %d 题是多空填空（%d 个空），但模型没按空序给'
                        '（%s），按没把握处理，这份不交。'
                        % (t['no'], len(t['fills']), str(a)[:30]), 'warn')
                    missing.append(t['no'])
                    continue
                for f, pv in zip(t['fills'], parts):
                    if f['name']:
                        fills.append({'name': f['name'], 'val': pv,
                                      'no': t['no']})
                continue
            for f in t['fills']:
                if f['name']:
                    fills.append({'name': f['name'], 'val': a,
                                  'no': t['no']})
            continue
        if t['atype'] == '3':                # 判断题：统一转平台认的值
            a = '对' if a in ('对', '正确', 'TRUE', 'T', '√') else '错'
            vals = [a]
            if not any(o['val'] == a for o in t['opts']):
                # 有的判断题选项值是 true/false
                vals = ['true' if a == '对' else 'false']
        else:
            vals = list(a)                   # 多选 "AC" → ['A','C']
        # div 选区变体（type=divradio）：显示字母与真实值错位，按值匹配
        # 会把模型按截图答的显示字母对到错误选项上，只走位置映射
        names = {o['name'] for o in t['opts']
                 if o['val'] in vals and o.get('type') != 'divradio'}
        if not names:
            # 先试大小写归一（有的页面 value 是小写字母），再按位置兜底
            up = [str(v).upper() for v in vals]
            names = {o['name'] for o in t['opts']
                     if str(o.get('val', '')).upper() in up
                     and o.get('type') != 'divradio'}
        pos_vals = None
        if not names:
            names, pos_vals = _fallback_by_position(t, vals)
            if names:
                peek = ' / '.join(str(o.get('val', ''))[:14]
                                  for o in (t['opts'][:4]))
                log('    第 %d 题选项值不是字母（%s），已按选项位置落答。'
                    % (t['no'], peek), 'info')
        if not names:
            peek = ('%d 个选项' % len(t['opts'])) if t['opts'] \
                else '页面里解析到 0 个选项'
            log('    第 %d 题模型答了（%s）但选项落不下去（%s），'
                '按没答上处理。' % (t['no'], str(a)[:12], peek), 'warn')
            _dump_page_diag(t, 'opts_unmappable')
            missing.append(t['no'])
            continue
        for nm in names:
            fills.append({'name': nm, 'vals': pos_vals or vals,
                          'no': t['no']})
    if not fills:
        # 一道都没答上 = 确实没有可保存的内容，保持「整份不碰」。
        # 注意判据是 fills（真的能填进去的）而不是 missing：
        # 只要有一道答上了，就要往下走，把它存进平台（v3.9.2 起同 v4.0）。
        log('    ⚠ 这一整份都没答上（第 %s 题），没有可保存的内容，'
            '这份需要你手动做。'
            % '、'.join(map(str, missing[:8])), 'warn')
        return 'unsolved'
    ids = hw.evaluate(JS_PREP_WQB)
    if not ids:
        # 不补 answerwqbid，平台会回「无效的参数：code-1」，保存直接被拒
        # （实测）。这种时候填了也存不下来，索性别在页面上留一堆没落库的字。
        log('    ⚠ 没拿到题目清单（answerwqbid），平台会拒绝保存，'
            '这份没法落库。', 'warn')
        return 'failed'
    # ---- 分批填 + 分批暂存：「做几道存几道」（v3.9.2 自 v4.0 移植） ----
    #   · 答上的题**一律填进去**，并且每 save_every 道调一次平台暂存 ——
    #     中途被停止 / 断网 / 窗口被关，前面已填的答案也不会白填；
    #   · 有题没答上 / 选项没点中 / 主观题没进编辑器 → **只暂存、绝不交卷**
    #     （交上去就是白拿低分），剩下的题打开作业网址补完再点提交，
    #     先前暂存的答案还在，不用重答一遍。
    # 以前是「有一道没答上就整份不填不存」，用户实测的后果是：答案都对，
    # 平台上却什么都没有，等于白做。
    try:
        step = max(int(cfg.get('save_every') or 5), 1)
    except Exception:
        step = 5
    order = sorted(fills, key=lambda f: 0 if 'vals' in f else 1)   # 先客观后主观
    batches = [order[i:i + step] for i in range(0, len(order), step)]
    n_q = len({f['no'] for f in fills})
    n_save_fail = 0

    def _save_draft():
        """调平台自己的「临时保存」（saveWork）。返回 True = 调用成功。

        evaluate 包在 try 里：核实环节会整页重新导航，cards 链上的 hw
        是内层 iframe，届时早已分离（Frame was detached）—— 兜底调用
        绝不能让异常冲出去，把状态记账炸成 failed。
        """
        nonlocal n_save_fail
        try:
            act = hw.evaluate(JS_DO_SAVE)
        except Exception as e:
            act = 'err:%s' % type(e).__name__
        if act != 'ok':
            n_save_fail += 1
            log('    ⚠ 暂存没调起来（%s），这批答案可能没落到平台。' % act,
                'warn')
        return act == 'ok'

    def _strip(fs):
        """'no' 只是本地分组用的，不往页面里塞。"""
        return [{k: v for k, v in f.items() if k != 'no'} for f in fs]

    txt_miss = 0
    op_miss = 0
    saved_last = False
    for bi, batch in enumerate(batches):
        op = _strip([f for f in batch if 'vals' in f])
        tx = _strip([f for f in batch if 'val' in f])
        if op:
            r = hw.evaluate(JS_FILL_ANSWERS, op)
            op_miss += int(r.get('miss') or 0)
            log('    已填 %d 个选项（未命中 %d）。'
                % (r.get('ok', 0), r.get('miss', 0)))
        if tx:
            # 客观题与主观题之间隔一会，别一瞬全填完
            time.sleep(random.uniform(1.0, 3.0))
            r = hw.evaluate(JS_FILL_TEXT, tx)
            txt_miss += int(r.get('miss') or 0)
            log('    已填 %d 道主观题（未命中 %d）。'
                % (r.get('ok', 0), r.get('miss', 0)))
        last = (bi + 1) >= len(batches)
        # 还有下一批、或本来就是暂存模式、或已知有题答不上 → 落一次库。
        # 只有「最后一批 + 要交卷 + 没缺题」才省掉这一次：提交马上就到，
        # 多存一次纯属白多一个请求。
        saved_last = False
        if (not last) or (not submit) or bool(missing):
            saved_last = _save_draft()
        if not last:
            time.sleep(random.uniform(0.8, 2.2))
    # 主观题没真正写进编辑器（UE 未就绪等）＝平台会收到空答案。
    # 这是「误交卷」级别的事故，宁可不交也不能悄悄交白卷（实测踩过）。
    if txt_miss or missing or op_miss:
        if not saved_last:
            _save_draft()          # 已填进去的必须落库，别停在页面上
        if n_save_fail:
            log('    ⚠ 暂存调用失败了 %d 次，答案可能没落到平台，'
                '请打开作业网址自己确认。' % n_save_fail, 'warn')
        else:
            log('    ✓ 已对答上的 %d 题调用平台暂存（未交卷）。' % n_q,
                'warn')
        if txt_miss:
            log('    其中 %d 道主观题没写进答题编辑器（平台会读到空），'
                '所以没有交卷。' % txt_miss, 'warn')
        if op_miss:
            log('    其中 %d 个选项没点中（平台会读到空），所以没有交卷。'
                % op_miss, 'warn')
        if missing:
            log('    没答上的：第 %s 题。打开作业网址补完这些题再点提交，'
                '先前暂存的答案还在，不用重答。'
                % '、'.join(map(str, missing[:8])), 'warn')
        return 'partial'
    if not submit:
        log('    ✓ 已执行暂存（未走正式交卷弹窗）。平台是否受理，'
            '以作业页状态为准。')
        return 'ok'
    # 交卷必须「核实后再报成功」：监听平台的提交响应，btnBlueSubmit 是异步链，
    # 调用返回 ok 只代表链路启动了，不代表服务端收下了（实测踩过假 ✓）。
    # 测试桩的 FakePage 没有事件接口，跳过监听、维持旧的直接判定。
    # 注意端点不限于 addStudentWorkNew：实测有一次提交延迟数分钟才生效，
    # 期间响应抓不到——所以捕获放宽到全部 work 请求，核实允许多次重开。
    # ⚠ 判「受理成功」必须同时看到**真实提交 POST 已发出**（req_seen）：
    # btnBlueSubmit 兜底链的状态检查类响应也会带 "status":true，
    # 只看响应不看请求，会把「没交上报成交上」（本项目出过的事故类别）。
    resp_seen = []      # work 相关响应（证据 + 成功判定）
    req_seen = []       # work 相关 POST（判定「请求到底发没发出去」）
    can_listen = hasattr(page, 'on')

    def _is_work(u):
        u = u or ''
        return 'chaoxing.com' in u and ('/work/' in u or 'work/' in u.split('?')[0])

    def _on_resp(r):
        if not _is_work(r.url):
            return
        try:
            body = (r.text() or '')[:300]
        except Exception:
            body = ''
        resp_seen.append({'st': r.status, 'url': (r.url or '')[-110:],
                          'body': body})

    def _on_req(r):
        try:
            if r.method == 'POST' and _is_work(r.url):
                req_seen.append((r.url or '')[-110:])
        except Exception:
            pass

    if can_listen:
        page.on('response', _on_resp)
        page.on('request', _on_req)
    try:
        # 提交前再停一下：填完立刻点提交在时序上太齐整
        time.sleep(random.uniform(2.0, 5.0))
        act = hw.evaluate(JS_DO_SUBMIT)
        if act != 'ok':
            log('    交卷调用失败：%s' % act, 'warn')
            # 交卷没调起来 ≠ 这份白做。先把已填的答案暂存到平台，
            # 用户打开作业网址补点一下「提交」就行，不用从头再答一遍。
            if _save_draft():
                log('    已改为暂存：已调用平台暂存，打开作业网址点提交即可。',
                    'warn')
                return 'partial'
            return 'failed'
        if can_listen:
            # 平台受理可能要几十秒甚至几分钟（实测一次 20 秒内毫无动静、
            # 数分钟后已批阅），轮询着等。窗口可在 config 调（submit_verify_window）。
            vwin = max(int(cfg.get('submit_verify_window') or 30), 10)
            deadline = time.time() + vwin
            while time.time() < deadline:
                time.sleep(2)
                if req_seen and _submit_ok(resp_seen):
                    break
    finally:
        if can_listen:
            try:
                page.remove_listener('response', _on_resp)
                page.remove_listener('request', _on_req)
            except Exception:
                pass
    if not can_listen:
        log('    ✓ 已交卷（平台完整提交流程）。')
        return 'submitted'
    if req_seen and _submit_ok(resp_seen):
        log('    ✓ 已交卷（平台已受理提交）。')
        return 'submitted'
    # 「提交请求没发出去」时，趁作业页还没被核实导航冲掉、答案还在
    # 页面上，先补存一次 —— 重开核实会整页导航：cards 链上的 hw frame
    # 会随之分离，页面上没落库的最后一批答案也会一起消失，到那时
    # 再想存就来不及了。
    saved_post = False
    if not req_seen:
        log('    ⚠ 没观察到平台的提交请求，这份大概率没交上。', 'warn')
        if _save_draft():
            saved_post = True
            log('    已把答案暂存进平台：打开作业网址补点「提交」即可，'
                '不用重答一遍。', 'warn')
    # 响应没抓到 → 重开作业卡核实是否已切「已批阅」。平台翻转状态可能
    # 要几分钟，一次没翻不算数：次数/间隔可在 config 调
    # （submit_recheck_times / submit_recheck_interval）。
    rtimes = max(int(cfg.get('submit_recheck_times') or 3), 1)
    rgap = max(int(cfg.get('submit_recheck_interval') or 10), 3)
    for attempt in range(rtimes):
        if attempt:
            time.sleep(rgap)
        log('    没直接观察到受理响应，重开作业卡核实（第 %d/%d 次）…'
            % (attempt + 1, rtimes))
        _, note2 = (_open_work_page(page, url, log) if direct
                    else _wait_hw_iframe(page, url, log))
        if note2 == 'already':
            log('    ✓ 已交卷（平台显示已批阅）。')
            return 'submitted'
    if req_seen:
        log('    ⚠ 提交请求已发出（%s…）但 %d 秒内没核实到受理结果；'
            '平台可能延迟生效，请稍后打开作业网址确认这份的状态。'
            % (req_seen[0], vwin), 'warn')
    else:
        # 上面趁页面还在时补存过一次（saved_post）；这里是第二次机会：
        # 万一那次没调起来再试一回。frame 多半已随核实导航分离，
        # _save_draft 里的 try 会兜住，不会炸掉状态记账。
        if not saved_post:
            if _save_draft():
                log('    已把答案暂存进平台：打开作业网址补点「提交」即可，'
                    '不用重答一遍。', 'warn')
            else:
                log('    暂存也没调起来，请打开作业网址手动确认这份的状态。',
                    'warn')
    return 'unverified'


def answer_urls(cfg, targets, progress=None, control=None, headless=None,
                submit=True) -> dict:
    """对「未完成作业」清单里指定的作业逐份答题（界面每行的「刷题」按钮）。

    targets: [{'url': 作业直达链接, 'course': 课程名, 'title': 作业名}]。
             url 必填；课程名/作业名只用于日志，不影响定位。
    submit:  True = 答完走正式交卷链（默认，与任务点那个按钮一致）；
             False = 答完调平台「暂存保存」，不交卷（界面「刷题并暂存」）；
             'dry' = 只出答案不落库（预演）。

    判别逻辑**不重复实现**，全部复用 answer_one：附件题整份跳过、题干读不出
    跳过、多空填空按空序切分、同题干复用上次答案、含不支持题型跳过。
    一个浏览器会话里逐份处理，中途可停。
    """
    progress = progress or (lambda m, *a: log(m, a[0] if a else 'info'))
    control = control or (lambda: 'run')
    tgt = [t for t in (targets or [])
           if isinstance(t, dict) and t.get('url')]
    out = {'total': len(tgt), 'submitted': 0, 'unverified': 0, 'report': 0,
           'already': 0, 'skipped': 0, 'saved': 0, 'fail': 0, 'partial': 0,
           'stopped': False, 'items': []}
    if not tgt:
        raise RuntimeError('没有可做的作业（这份清单里缺少作业链接）')
    ctx = launch(cfg, headless=headless)
    page = ctx.new_page()
    try:
        banner('检查登录状态')
        if not check_login(page):
            if restore_state(ctx) and check_login(page):
                log('已用本地会话自动登录 ✓')
            else:
                log('本地没有可用登录态，请先登录。', 'err')
                raise NotLoggedIn('未登录')
        banner('逐份答题（共 %d 份，%s）'
               % (len(tgt), {True: '答完交卷', False: '答完暂存（不交卷）',
                             'dry': '预演不落库'}.get(submit, str(submit))))
        for i, t in enumerate(tgt, 1):
            if _wait_control(control, progress) == 'stop':
                out['stopped'] = True
                log('已停止。', 'warn')
                break
            progress('【%d/%d】%s ｜ %s'
                     % (i, len(tgt), cut(t.get('course') or '（未知课程）', 18),
                        cut(t.get('title') or '（未知作业）', 26)))
            detail = {}
            try:
                r = answer_one(page, None, None, None, cfg, submit, progress,
                               detail=(detail if submit == 'dry' else None),
                               url=t['url'])
            except Exception as e:
                r = 'failed'
                log('    这份出错了：%s' % str(e)[:120], 'warn')
            if r == 'submitted':
                out['submitted'] += 1
            elif r == 'unverified':
                out['unverified'] += 1
            elif r in ('report', 'unsupported'):
                out['report'] += 1
            elif r == 'already':
                out['already'] += 1
            elif r in ('nohw', 'norender', 'dry'):
                out['skipped'] += 1
            elif r == 'partial':
                out['partial'] += 1      # 部分作答已暂存，要人工补完再交
            elif r == 'ok':
                out['saved'] += 1        # submit=False 的纯暂存成功
            else:                        # unsolved / failed
                out['fail'] += 1
            rec = {'course': t.get('course'), 'title': t.get('title'),
                   'url': t['url'], 'status': r}
            if submit == 'dry':
                rec['answers'] = detail.get('answers') or {}
                rec['stems'] = detail.get('stems') or {}
                rec['total'] = detail.get('total')
            out['items'].append(rec)
    finally:
        try:
            save_state(ctx)
        except Exception:
            pass
        close_ctx(ctx)
    return out


def answer_courses(cfg, only, submit=False, progress=None, control=None,
                   headless=None) -> dict:
    """按课程自动做章节任务点里的作业。

    only: 课程 key（cid:clsid）与「key|kid」章节规格混用的列表（界面勾选），
          必填。key = 该课全部待完成节点；key|kid = 只做这个章节节点。
    submit: True = 答完直接交卷；False = 平台暂存（仅 CLI，见 answer_one
            的说明）；'dry' = 预演：不落库，收集 items 清单由用户复核交卷。
    """
    progress = progress or (lambda m, *a: log(m, a[0] if a else 'info'))
    control = control or (lambda: 'run')
    t0 = time.time()
    ctx = launch(cfg, headless=headless)
    page = ctx.new_page()
    out = {'courses': [], 'submitted': 0, 'saved': 0, 'skipped': 0,
           'fail': 0, 'partial': 0, 'stopped': False, 'items': []}
    if submit == 'dry':
        progress('预演模式：只答题不落库，答完列出清单，由你核对后手动或一键交卷。')
    try:
        banner('检查登录状态')
        if not check_login(page):
            if restore_state(ctx) and check_login(page):
                log('已用本地会话自动登录 ✓')
            else:
                log('本地没有可用登录态，请先登录。', 'err')
                raise NotLoggedIn('未登录')
        courses, cpi = collect_courses(page, cfg)
        keep, node_keep = split_only(only)
        picked = [c for c in courses if course_key(c) in keep]
        if not picked:
            raise RuntimeError('要做的课程没匹配到（课程可能有变动），'
                               '请重新「获取课程列表」后再试')
        progress('共 %d 门课要做作业：%s' % (
            len(picked), '、'.join(cut(c['name'], 16) for c in picked)))
        banner('逐门课做章节作业（共 %d 门，%s）'
               % (len(picked), {True: '答完交卷', False: '答完保存（不交卷）',
                                'dry': '预演不落库'}.get(submit, str(submit))))
        if submit is False:
            log('    ⚠ 章节作业的暂存一旦被平台接受，这个任务点就会立即标记'
                '完成（成绩要等正式交卷才有）。')
        for i, c in enumerate(picked, 1):
            if _wait_control(control, progress) == 'stop':
                out['stopped'] = True
                log('已停止。', 'warn')
                break
            progress('【%d/%d】%s' % (i, len(picked), c['name']))
            pd = fetch_progress_detail(page, c, cpi, cfg)
            if pd.get('page_failed'):
                progress('  ⚠ 页面没加载成功（%s），这门课先跳过。'
                         % pd.get('reason', ''), 'warn')
                out['courses'].append({'name': c['name'], 'note': '页面未加载'})
                continue
            nodes = [n for n in (pd.get('pending') or []) if n.get('kid')]
            want = node_keep.get(course_key(c))
            if want is not None:
                nodes = [n for n in nodes if n['kid'] in want]
                if not nodes:
                    progress('  勾选的章节已经不在待完成清单里（可能已完成）。')
                    continue
            if not nodes:
                progress('  没有待完成的章节节点。')
                continue
            progress('  待处理节点 %d 个，逐个找作业卡…' % len(nodes))
            s = su = sk = f = rp = uw = pt = 0
            stopped_here = False
            nr = 0          # 连续「作业卡内页打不开」数（疑似限流的信号）
            cooled = False  # 本门课是否已冷却过一次

            def _cool(seconds):
                """可中断冷却。返回 True = 用户点了停止。"""
                progress('  ⚠ 连续 %d 个作业卡打不开（疑似平台限流），冷却 %d 秒…'
                         '（冷却中可点停止）' % (nr, seconds), 'warn')
                for _ in range(seconds):
                    time.sleep(1.0)
                    if _wait_control(control, progress) == 'stop':
                        return True
                progress('  冷却结束，重试这个节点。')
                return False

            for j, node in enumerate(nodes, 1):
                if _wait_control(control, progress) == 'stop':
                    stopped_here = True
                    break
                progress('  · 节点 %d/%d：%s' % (j, len(nodes),
                                                 cut(node.get('name', ''), 30)))
                det = {} if submit == 'dry' else None
                try:
                    r = answer_one(page, c, cpi, node['kid'], cfg, submit,
                                   progress, detail=det)
                except Exception as e:
                    # 单节点故障（如大模型接口连挂 3 次）不炸整批/整门
                    progress('  ✗ 节点失败（%s），按需人工处理。'
                             % str(e)[:80], 'warn')
                    r = 'failed'
                if r == 'norender':
                    if cooled:
                        progress('  ⚠ 冷却后作业卡仍打不开，限流没解除，这门课先中止'
                                 '（稍后再来）。', 'warn')
                        out['courses'].append({'name': c['name'], 'note': '限流中止'})
                        break
                    nr += 1
                    if nr >= 2:
                        if _cool(max(int(cfg.get('risk_cooldown') or 90), 30)):
                            stopped_here = True
                            break
                        cooled = True
                        nr = 0
                        try:
                            r = answer_one(page, c, cpi, node['kid'], cfg,
                                           submit, progress, detail=det)
                        except Exception as e:
                            progress('  ✗ 节点失败（%s），按需人工处理。'
                                     % str(e)[:80], 'warn')
                            r = 'failed'
                elif r != 'nohw':
                    nr = 0       # 真正处理了作业卡（无论成败）→ 重置嫌疑计数
                if submit == 'dry' and r in ('dry', 'unsolved', 'report',
                                             'unsupported', 'failed', 'norender'):
                    # dry 模式：把每一份都列进清单（含需人工/失败的），交卷入口统一
                    out['items'].append({
                        'key': course_key(c), 'kid': node['kid'],
                        'course': c['name'],
                        'node': node.get('name') or ('节点 %d' % j),
                        'url': CARDS_URL.format(clsid=c['clsid'], cid=c['cid'],
                                                kid=node['kid'], cpi=cpi),
                        'answers': det.get('answers', {}) if det else {},
                        'stems': det.get('stems', {}) if det else {},
                        'total': det.get('total', 0) if det else 0,
                        'miss': det.get('miss', []) if det else [],
                        'status': ('ready' if r == 'dry' else
                                   {'report': 'report', 'unsupported': 'unsupported',
                                    'unsolved': 'unsolved'}.get(r, 'failed')),
                    })
                elif submit is False and r in ('ok', 'partial', 'already',
                                               'report', 'unsupported',
                                               'unsolved', 'failed', 'norender'):
                    # 暂存模式（界面「做作业并暂存」）：逐份结果也要进清单，
                    # 「交卷结果」面板才有得渲染——不然面板停在上一次任务
                    # 的内容，用户只能从汇总数字里猜（审查 2026-10-09 补）。
                    out['items'].append({
                        'key': course_key(c), 'kid': node['kid'],
                        'course': c['name'],
                        'node': node.get('name') or ('节点 %d' % j),
                        'url': CARDS_URL.format(clsid=c['clsid'], cid=c['cid'],
                                                kid=node['kid'], cpi=cpi),
                        'answers': {}, 'stems': {}, 'total': 0, 'miss': [],
                        'status': {'ok': 'ok', 'partial': 'partial',
                                   'already': 'already'}.get(r, r),
                    })
                elif submit is True and r in ('submitted', 'already', 'unverified',
                                              'report', 'unsupported', 'unsolved',
                                              'partial'):
                    # 正式交卷模式：逐份结果也进清单，界面据此渲染「交卷结果」，
                    # 其中未确认的作业可以在面板里「重新核实」。
                    out['items'].append({
                        'key': course_key(c), 'kid': node['kid'],
                        'course': c['name'],
                        'node': node.get('name') or ('节点 %d' % j),
                        'url': CARDS_URL.format(clsid=c['clsid'], cid=c['cid'],
                                                kid=node['kid'], cpi=cpi),
                        'answers': {}, 'stems': {}, 'total': 0, 'miss': [],
                        'status': {'submitted': 'done', 'already': 'already',
                                   'unverified': 'unverified'}.get(r, r),
                    })
                if r == 'submitted' or (r == 'already' and submit is True):
                    su += 1
                elif r == 'already':
                    sk += 1    # 预演/暂存模式遇到已批阅的：没东西可做，跳过
                elif r == 'unverified' and submit is True:
                    uw += 1    # 请求发出但没核实到结果，单独记账不混进失败
                elif r == 'partial':
                    s += 1     # 已落库的部分作答，算进「已保存」一路
                    pt += 1    # 但另记一笔：这几份还得人工补完才能交
                elif r == 'ok':
                    s += 1
                elif r == 'dry':
                    s += 1       # 预演完成，计进「已答」一路的计数
                elif r == 'report':
                    rp += 1     # 上传附件型：需要用户本人完成，不算失败
                elif r in ('nohw', 'unsupported', 'unsolved'):
                    sk += 1
                elif r == 'norender':
                    f += 1
                else:
                    f += 1
                time.sleep(max(float(cfg.get('course_delay', 3.0)), 1.0))
            out['courses'].append({'name': c['name'], 'submitted': su,
                                   'saved': s, 'skipped': sk, 'fail': f,
                                   'report': rp, 'unverified': uw,
                                   'partial': pt})
            out['submitted'] += su
            out['saved'] += s
            out['skipped'] += sk
            out['fail'] += f
            out['report'] = out.get('report', 0) + rp
            out['unverified'] = out.get('unverified', 0) + uw
            out['partial'] = out.get('partial', 0) + pt
            progress('  ✓ %s 做完：交卷 %d，已答/保存 %d，需人工 %d，失败 %d%s。'
                     % (cut(c['name'], 24), su, s, rp, f,
                        '，未确认 %d' % uw if uw else ''))
            if stopped_here:
                out['stopped'] = True
                break
            time.sleep(max(float(cfg.get('course_delay', 3.0)), 2.0))
        if submit == 'dry':
            n_ready = sum(1 for it in out['items'] if it['status'] == 'ready')
            n_need = sum(1 for it in out['items']
                         if it['status'] in ('report', 'unsupported', 'unsolved'))
            progress('预演结束：可交卷 %d 份，需人工 %d 份，失败 %d 份，'
                     '用时 %.0f 分钟。'
                     % (n_ready, n_need, out['fail'], (time.time() - t0) / 60))
            for it in out['items']:
                if it['status'] == 'ready':
                    progress('  ▶ 可交卷：%s · %s（答上 %d/%d 题，未把握：%s）%s'
                             % (cut(it['course'], 16), cut(it['node'], 26),
                                len(it['answers']), it['total'] or len(it['answers']),
                                ('第 ' + '、'.join(map(str, it['miss'][:6])) + ' 题')
                                if it['miss'] else '无',
                                it['url']))
                elif it['status'] in ('report', 'unsupported', 'unsolved'):
                    progress('  ⚠ 需人工：%s · %s（要上传附件/模型没把握/不支持'
                             '的题型，打开网址自己核对）'
                             % (cut(it['course'], 16), cut(it['node'], 26)), 'warn')
        else:
            progress('做作业结束：交卷 %d 份，保存 %d 份，需人工 %d 份，'
                     '失败 %d 份%s，用时 %.0f 分钟。'
                     % (out['submitted'], out['saved'], out.get('report', 0),
                        out['fail'],
                        ('，未确认 %d 份（稍后打开作业网址核对）'
                         % out['unverified']) if out.get('unverified') else '',
                        (time.time() - t0) / 60))
            if out.get('report'):
                names = ['%s（%d 份）' % (it['name'], it['report'])
                         for it in out['courses'] if it.get('report')]
                progress('⚠ 有 %d 份作业需要你本人完成（要上传实验报告/附件，'
                         '或题干被平台字体加密无法读取），涉及：%s。'
                         '工具不代交这类作业，记得手动上传。'
                         % (out['report'], '、'.join(names)), 'warn')
            uv = [it for it in out['items'] if it['status'] == 'unverified']
            out['unverified_items'] = uv
            if uv:
                progress('⚠ 有 %d 份作业没当场核实到结果（平台可能延迟生效）：'
                         '在「交卷结果」面板点「重新核实」，或稍后打开作业网址'
                         '自己确认。' % len(uv), 'warn')
        progress('重新点「一键查询」可核对最新状态。')
    finally:
        save_state(ctx)
        close_ctx(ctx)
    return out


def submit_items(cfg, items, progress=None, control=None, headless=None) -> dict:
    """把预演过的作业逐份正式交卷。

    items: answer_courses(submit='dry') 收集的清单。带 answers/stems 缓存的
           题直接复用（题干比对一致才用），缓存没有或对不上的题重新问模型。
    返回 {'submitted': n, 'fail': n, 'skip': n, 'stopped': bool,
          'results': [{'key','kid','status'}]}
    """
    progress = progress or (lambda m, *a: log(m, a[0] if a else 'info'))
    control = control or (lambda: 'run')
    t0 = time.time()
    ctx = launch(cfg, headless=headless)
    page = ctx.new_page()
    out = {'submitted': 0, 'fail': 0, 'skip': 0, 'unverified': 0,
           'partial': 0, 'stopped': False, 'results': []}
    try:
        banner('检查登录状态')
        if not check_login(page):
            if restore_state(ctx) and check_login(page):
                log('已用本地会话自动登录 ✓')
            else:
                log('本地没有可用登录态，请先登录。', 'err')
                raise NotLoggedIn('未登录')
        courses, cpi = collect_courses(page, cfg)
        by_key = {course_key(c): c for c in courses}
        progress('开始正式交卷：共 %d 份。' % len(items))
        for i, it in enumerate(items, 1):
            if _wait_control(control, progress) == 'stop':
                out['stopped'] = True
                log('已停止。', 'warn')
                break
            c = by_key.get(it.get('key') or '')
            if not c:
                progress('  ⚠ 找不到课程（可能已变动），跳过：%s'
                         % cut(it.get('course', ''), 20), 'warn')
                out['skip'] += 1
                out['results'].append({'key': it.get('key'), 'kid': it.get('kid'),
                                       'status': 'skip'})
                continue
            progress('【%d/%d】交卷：%s · %s' % (i, len(items), cut(c['name'], 18),
                                                 cut(it.get('node', ''), 28)))
            try:
                r = answer_one(page, c, cpi, it.get('kid'), cfg, True, progress,
                               pre={'answers': it.get('answers') or {},
                                    'stems': it.get('stems') or {}})
            except Exception as e:
                # 单份的意外故障（如大模型接口连挂 3 次）不能炸掉整批：
                # 记失败、继续下一份（实测教训：一题断网，后面全不交了）
                progress('  ✗ 这份交卷失败（%s），跳过继续。'
                         % str(e)[:80], 'warn')
                out['fail'] += 1
                out['results'].append({'key': it.get('key'),
                                       'kid': it.get('kid'), 'status': 'fail'})
                time.sleep(max(float(cfg.get('course_delay', 3.0)), 1.5))
                continue
            if r == 'submitted':
                out['submitted'] += 1
                st = 'done'
            elif r == 'already':
                out['submitted'] += 1      # 已批阅 = 这份作业已是完成态
                st = 'already'
            elif r == 'unverified':
                out['unverified'] += 1     # 请求发了但没核实到结果，不算失败
                st = 'unverified'
            elif r == 'partial':
                out['partial'] += 1   # 没交成，但答案已暂存：要人工补完再交
                st = 'partial'
            elif r in ('unsolved', 'report', 'unsupported'):
                out['skip'] += 1
                st = r
            else:
                out['fail'] += 1
                st = 'fail'
            out['results'].append({'key': it.get('key'), 'kid': it.get('kid'),
                                   'status': st})
            time.sleep(max(float(cfg.get('course_delay', 3.0)), 1.5))
        progress('交卷结束：成功 %d 份，未确认 %d 份，失败 %d 份，需人工 %d 份，'
                 '用时 %.0f 分钟。'
                 % (out['submitted'], out['unverified'], out['fail'],
                    out['skip'], (time.time() - t0) / 60))
        if out.get('partial'):
            progress('⚠ 有 %d 份没交成、但答案已暂存到平台（有几道题模型没答上'
                     '或没写进答题框）：打开作业网址补完再点提交即可，'
                     '不用重答。' % out['partial'], 'warn')
    finally:
        save_state(ctx)
        close_ctx(ctx)
    return out


def verify_items(cfg, items, progress=None, control=None, headless=None) -> dict:
    """事后核实「未确认」的作业到底交上没有。

    平台批阅状态可能延迟几分钟到十几分钟才翻转（实测多次），交卷当场
    核实不到不代表没交上。这里把未确认清单逐份重开：作业卡渲染成
    「已批阅」页 = 翻案为已交卷；仍是答题页 = 平台还没翻转（或真没交上），
    隔一段时间再核，最多 verify_rounds 轮（间隔 verify_round_gap 秒，
    都可在 config 调）。

    items: [{'key','kid','course','node'}]（交卷结果面板里的未确认行）。
    返回 {'confirmed': n, 'still': n, 'stopped': bool,
          'results': [{'key','kid','status'}]}   status ∈ already/still
    """
    progress = progress or (lambda m, *a: log(m, a[0] if a else 'info'))
    control = control or (lambda: 'run')
    t0 = time.time()
    ctx = launch(cfg, headless=headless)
    page = ctx.new_page()
    rounds = max(int(cfg.get('verify_rounds') or 3), 1)
    gap = max(int(cfg.get('verify_round_gap') or 120), 15)
    out = {'confirmed': 0, 'still': 0, 'stopped': False, 'results': []}
    try:
        banner('检查登录状态')
        if not check_login(page):
            if restore_state(ctx) and check_login(page):
                log('已用本地会话自动登录 ✓')
            else:
                log('本地没有可用登录态，请先登录。', 'err')
                raise NotLoggedIn('未登录')
        courses, cpi = collect_courses(page, cfg)
        by_key = {course_key(c): c for c in courses}
        # 初始就是 still：核实不中的项最后必须计入「仍未确认」。
        # 旧初始值 'unverified' 不在 already/still 统计里 → 出现过
        # 「确认已交 0 份，仍未确认 0 份」的矛盾账（22:23 实测踩过）
        st = {(it.get('key'), it.get('kid')): 'still' for it in items}
        pending = list(items)
        for rd in range(1, rounds + 1):
            progress('核实第 %d/%d 轮：共 %d 份待确认。' % (rd, rounds, len(pending)))
            confirmed_this = []
            for i, it in enumerate(pending, 1):
                if _wait_control(control, progress) == 'stop':
                    out['stopped'] = True
                    break
                c = by_key.get(it.get('key') or '')
                if not c:
                    progress('  ⚠ 找不到课程，没法核实：%s'
                             % cut(it.get('course', ''), 20), 'warn')
                    st[(it.get('key'), it.get('kid'))] = 'still'
                    continue
                progress('  【%d/%d】核实：%s · %s'
                         % (i, len(pending), cut(c['name'], 18),
                            cut(it.get('node', ''), 28)))
                _, note = _wait_hw_iframe(
                    page, CARDS_URL.format(clsid=c['clsid'], cid=c['cid'],
                                           kid=it.get('kid'), cpi=cpi), log)
                if note == 'already':
                    progress('  ✓ 平台显示已批阅——这份确实交上了。')
                    st[(it.get('key'), it.get('kid'))] = 'already'
                    confirmed_this.append(it)
                else:
                    progress('  … 平台还没翻转状态，下一轮再看。')
            for it in confirmed_this:
                pending.remove(it)
            out['confirmed'] += len(confirmed_this)
            if out['stopped'] or not pending:
                break
            if rd < rounds:
                progress('  还有 %d 份没核实到，隔 %d 秒再核一轮'
                         '（等待中可点停止）…' % (len(pending), gap))
                for _ in range(gap):
                    time.sleep(1.0)
                    if _wait_control(control, progress) == 'stop':
                        out['stopped'] = True
                        break
                if out['stopped']:
                    break
        for it in items:
            s = st.get((it.get('key'), it.get('kid')), 'still')
            out['results'].append({'key': it.get('key'), 'kid': it.get('kid'),
                                   'status': s})
            if s == 'still':
                out['still'] += 1
        progress('核实结束：确认已交 %d 份，仍未确认 %d 份%s，用时 %.0f 分钟。'
                 % (out['confirmed'], out['still'],
                    '（可过几分钟再点一次「重新核实」，或打开作业网址手动确认）'
                    if out['still'] else '',
                    (time.time() - t0) / 60))
    finally:
        save_state(ctx)
        close_ctx(ctx)
    return out


def brush_node(page, c, cpi, kid, cfg, progress, control):
    """刷一个章节节点里的全部视频卡。返回 (完成数, 未完成数, 是否被停止)"""
    url = CARDS_URL.format(clsid=c['clsid'], cid=c['cid'], kid=kid, cpi=cpi)
    try:
        page.goto(url, wait_until='domcontentloaded')
    except Exception as e:
        log('    打开章节页失败：%s' % str(e)[:80], 'warn')
        return 0, 1, False
    done = fail = 0
    # 一个节点挂好几张卡（视频/文档/测验混排），视频卡是懒加载的：
    # 刷完一张再扫一遍 frame，扫不到新的才算这个节点完事
    for _ in range(12):
        vf = None
        for f in page.frames:
            if 'modules/video' not in (f.url or ''):
                continue
            try:
                if f.evaluate(JS_VIDEO_CLAIM):
                    vf = f
                    break
            except Exception:
                continue
        if vf is None:
            time.sleep(2)
            for f in page.frames:
                if 'modules/video' not in (f.url or ''):
                    continue
                try:
                    if f.evaluate(JS_VIDEO_CLAIM):
                        vf = f
                        break
                except Exception:
                    continue
        if vf is None:
            break
        try:
            vf.evaluate("() => { window.__cx_brushed = true; }")
        except Exception:
            pass
        r = brush_video_frame(vf, cfg, progress, control)
        # 卡住/超时后原地重试（网络抖动、临时卡顿最常见）：同一个页面还在，
        # 清掉防重刷标记重跑一遍即可，不用重新导航（重新导航会把已完成的
        # 视频卡也重播一遍，浪费时间还重复心跳）
        retries = max(int(cfg.get('brush_retry', 1) or 0), 0)
        attempt = 0
        while r in ('stuck', 'timeout') and attempt < retries:
            attempt += 1
            if _wait_control(control, progress) == 'stop':
                return done, fail, True
            progress('    ⚠ 上一次%s（第 %d 次重试）…'
                     % ('卡住' if r == 'stuck' else '超时', attempt), 'warn')
            time.sleep(random.uniform(4.0, 8.0))
            try:
                vf.evaluate("() => { window.__cx_brushed = false; }")
            except Exception:
                pass
            r = brush_video_frame(vf, cfg, progress, control)
            if r == 'stopped':
                return done, fail, True
        if r == 'done':
            done += 1
            log('    ✓ 这个视频刷完了')
        elif r in ('stuck', 'timeout'):
            fail += 1
        # 'novideo'：这卡是文档/音频之类，不算账
    return done, fail, False


def _wait_control(control, progress):
    """在课程/节点边界处理暂停。返回 'run' 或 'stop'。"""
    while True:
        act = control()
        if act != 'pause':
            return act
        time.sleep(0.3)


def brush_videos(cfg, only, progress=None, control=None, headless=None) -> dict:
    """按课程自动刷未完成的「任务点视频」。

    only: 课程 key（cid:clsid）与「key|kid」章节规格混用的列表（界面勾选），
          必填——不提供「全部课程都刷」，防止误触发把几十门课全部挂后台。
          key = 该课全部待完成节点；key|kid = 只刷这个章节节点。
    progress(msg): 进度回调（界面把它写进日志和状态行）。
    control(): 'run' | 'pause' | 'stop'，界面注入；CLI 用默认的「一直 run」。
    返回 {'courses': [...], 'done': n, 'fail': n, 'stopped': bool}
    """
    progress = progress or (lambda m: log(m))
    control = control or (lambda: 'run')
    t0 = time.time()
    ctx = launch(cfg, headless=headless)
    page = ctx.new_page()
    out = {'courses': [], 'done': 0, 'fail': 0, 'stopped': False}
    # 连刷歇息的小本子：见 brush_pace_rest 的说明（防零进度节点反复歇）
    rest_state = {'last': 0}
    try:
        banner('检查登录状态')
        if not check_login(page):
            if restore_state(ctx) and check_login(page):
                log('已用本地会话自动登录 ✓')
            else:
                log('本地没有可用登录态，请先登录。', 'err')
                raise NotLoggedIn('未登录')
        courses, cpi = collect_courses(page, cfg)
        keep, node_keep = split_only(only)
        picked = [c for c in courses if course_key(c) in keep]
        if not picked:
            raise RuntimeError('要刷的课程没匹配到（课程可能有变动），'
                               '请重新「获取课程列表」或「一键查询」后再试')
        progress('共 %d 门课要刷：%s' % (
            len(picked), '、'.join(cut(c['name'], 16) for c in picked)))
        banner('逐门课刷视频（共 %d 门，%sx 静音）' % (len(picked), g_rate(cfg)))
        # 登录确认后再收窗口：登录/验证码阶段必须可见，刷课阶段不需要
        if cfg.get('brush_minimize', True) and not (headless is True):
            if minimize_window(page):
                progress('浏览器已最小化到任务栏（后台刷课不影响进度，'
                         '点任务栏图标可随时查看）。')
        for i, c in enumerate(picked, 1):
            if _wait_control(control, progress) == 'stop':
                out['stopped'] = True
                log('已停止。', 'warn')
                break
            progress('【%d/%d】%s' % (i, len(picked), c['name']))
            pd = fetch_progress_detail(page, c, cpi, cfg)
            if pd.get('page_failed'):
                progress('  ⚠ 页面没加载成功（%s），这门课先跳过，稍后重新刷。'
                         % pd.get('reason', ''), )
                out['courses'].append({'name': c['name'], 'done': 0, 'fail': 0,
                                       'note': '页面未加载'})
                continue
            if pd.get('no_module') or not pd.get('pending'):
                progress('  没有待完成的章节节点，跳过。')
                out['courses'].append({'name': c['name'], 'done': 0, 'fail': 0,
                                       'note': '无待完成节点'})
                continue
            nodes = [n for n in pd['pending'] if n.get('kid')]
            want = node_keep.get(course_key(c))
            if want is not None:
                nodes = [n for n in nodes if n['kid'] in want]
                if not nodes:
                    progress('  勾选的章节已经不在待完成清单里（可能已完成），跳过。')
                    out['courses'].append({'name': c['name'], 'done': 0, 'fail': 0,
                                           'note': '勾选章节已完成'})
                    continue
            progress('  待完成节点 %d 个，开始刷…' % len(nodes))
            cd = cf = 0
            stopped_here = False
            for j, node in enumerate(nodes, 1):
                if _wait_control(control, progress) == 'stop':
                    stopped_here = True
                    break
                progress('  ▶ 节点 %d/%d：%s' % (j, len(nodes), cut(node['name'], 30)))
                d, f, stopped_here = brush_node(page, c, cpi, node['kid'],
                                                cfg, progress, control)
                cd += d
                cf += f
                if stopped_here:
                    break
                # 连刷一阵就歇一会：连续几小时零停顿是明显统计特征
                if brush_pace_rest(out['done'] + cd, cfg, progress, control,
                                   rest_state):
                    stopped_here = True
                    break
                time.sleep(max(float(cfg.get('course_delay', 3.0)), 1.0))
            out['courses'].append({'name': c['name'], 'done': cd, 'fail': cf})
            out['done'] += cd
            out['fail'] += cf
            # 一门课刷完立刻汇报一次；中途停止也汇报已刷的部分
            progress('  ✓ %s 刷完：完成 %d 个节点，未完成 %d 个。'
                     % (cut(c['name'], 24), cd, cf))
            if stopped_here:
                out['stopped'] = True
                break
            # 课程之间留足间隔：刷视频的请求密度比扫描高得多，别在课间也贴地飞行
            time.sleep(max(float(cfg.get('course_delay', 3.0)), 2.0))
        progress('刷视频结束：完成 %d 个，未完成 %d 个，用时 %.0f 分钟。'
                 % (out['done'], out['fail'], (time.time() - t0) / 60))
        if out['fail']:
            progress('没刷完的多半是「反复暂停卡住」的插问视频或超长视频，'
                     '这些需要你手动看一遍。')
        if not out['stopped']:
            progress('建议重新点「一键查询」核对最新状态——报告里消失的节点就是刷完的。')
    finally:
        save_state(ctx)
        close_ctx(ctx)
    return out


def build_report(courses, results, stopped_at=None, scope_total=None,
                 scope_kind='selected', failed_courses=None) -> dict:
    """scope_total：账号里的课程总数。

    只在「本次只查了一部分课程」时传入。传了它，报告就会显式写明这是
    一次范围受限的检查——否则用户看到清单里没别的课，会误以为都完成了。
    scope_kind：'selected'=用户勾选了一部分；'recent'=只查了最近 N 门。
    两者措辞不同但同样必须标注，理由一致。
    failed_courses：页面没加载成功的课程清单（v2.6 起），报告里要显式警告。
    """
    def label(i):
        c = courses[i]
        same = [x for x in courses if x['name'] == c['name']]
        return '%s（ID:%s）' % (c['name'], c['cid']) if len(same) > 1 else c['name']

    undone_hw, undone_exam, undone_prog, overview = [], [], [], []
    for i, r in enumerate(results):
        nm = label(i)
        hw, ex = r['hw'], r['exam']
        for it in hw.get('items', []):
            if is_undone(it):
                undone_hw.append({'course': nm, **it})
        for it in ex.get('items', []):
            if is_undone(it):
                undone_exam.append({'course': nm, **it})

        pm = re.search(r'(\d+)\s*/\s*(\d+)', r.get('prog_text') or '')
        if pm:
            done, total = int(pm.group(1)), int(pm.group(2))
            if done < total:
                pd = r.get('prog_detail') or {}
                # cid/clsid 是「自动刷视频」的定位参数：界面勾选哪门课要刷，
                # 后端靠这两个 ID 重新找到这门课的章节页。
                undone_prog.append({'course': nm, 'done': done, 'total': total,
                                    'rate': done * 100.0 / total if total else 0,
                                    'cid': r['cid'], 'clsid': r['clsid'],
                                    'chapters': pd.get('pending') or []})
        if hw.get('page_failed'):
            hw_txt = '⚠ 未加载'
        elif hw.get('no_module'):
            hw_txt = '无作业模块'
        elif hw.get('count'):
            hw_txt = hw['count']
        else:
            hw_txt = '—'
        if ex.get('page_failed'):
            ex_txt = '⚠ 未加载'
        elif ex.get('empty'):
            ex_txt = '无考试'
        elif ex.get('items'):
            ne = sum(1 for it in ex['items'] if is_undone(it))
            ex_txt = '%d 场' % len(ex['items']) + ('（未完成 %d）' % ne if ne else '（已完成）')
        else:
            ex_txt = '—'
        overview.append({
            'name': nm, 'hw': hw_txt, 'exam': ex_txt,
            'prog': ('%s/%s' % (pm.group(1), pm.group(2))) if pm else '—',
            'prog_tuple': (int(pm.group(1)), int(pm.group(2))) if pm else None,
            'cid': r['cid'],
        })

    # 未完成项按课程归拢，便于阅读
    return {
        'time': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'course_total': len(courses),
        'scanned': len(results),
        'partial': stopped_at is not None,
        'selected_only': scope_total is not None,
        'scope_kind': scope_kind if scope_total is not None else None,
        'account_total': scope_total if scope_total is not None else len(courses),
        'undone_hw': undone_hw,
        'undone_exam': undone_exam,
        'undone_prog': undone_prog,
        'overview': overview,
        'page_failed': failed_courses or [],
    }


def write_outputs(rep: dict) -> dict:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M')

    # ---- Markdown
    if rep.get('partial'):
        scope = ('- ⚠️ **本次为中止扫描**：只检查了 %d/%d 门课程，'
                 '未列出的课程是「没查」而不是「已完成」'
                 % (rep.get('scanned', 0), rep['course_total']))
    elif rep.get('selected_only'):
        what = ('最近学习的 %d 门课程' if rep.get('scope_kind') == 'recent'
                else '你勾选的 %d 门课程') % rep['course_total']
        scope = ('- ⚠️ **本次只检查了%s**'
                 '（账号共 %d 门，其余课程未检查，不在下表范围内）'
                 % (what, rep.get('account_total', 0)))
    else:
        scope = '- 课程范围：个人空间全部 %d 门课程（含已结束课程）' % rep['course_total']

    L = ['# 学习通未完成事项清单', '',
         '- 生成时间：%s' % rep['time'],
         scope]
    if rep.get('page_failed'):
        names = '、'.join(x['course'] for x in rep['page_failed'][:6]) \
                + ('…' if len(rep['page_failed']) > 6 else '')
        L.append('- ⚠️ **有 %d 门课的页面没有加载成功（%s），这些课的作业/考试'
                 '结果是「没查到」而不是「没有」，建议稍后重新查询**'
                 % (len(rep['page_failed']), names))
    L += ['- 说明：状态「未交」才是真正未完成；「待批阅/已互评/待互评/已提交」平台均计为已完成', '']

    L += ['## 一、未完成作业（%d 项）' % len(rep['undone_hw']), '']
    if rep['undone_hw']:
        L += ['| # | 课程 | 作业名称 | 状态 | 剩余时间 | 直达链接 |',
              '|---|------|----------|------|----------|----------|']
        for i, it in enumerate(rep['undone_hw'], 1):
            link = '[去完成](%s)' % it['url'] if it.get('url') else '—'
            L.append('| %d | %s | %s | **%s** | %s | %s |' % (
                i, it['course'], it['title'], it['state'], it.get('left') or '—', link))
    else:
        L.append('无。')
    L.append('')

    L += ['## 二、未完成考试（%d 项）' % len(rep['undone_exam']), '']
    if rep['undone_exam']:
        L += ['| # | 课程 | 考试名称 | 状态 |', '|---|------|----------|------|']
        for i, it in enumerate(rep['undone_exam'], 1):
            L.append('| %d | %s | %s | **%s** |' % (i, it['course'], it['title'], it['state']))
    else:
        L.append('无。')
    L.append('')

    L += ['## 三、未完成任务点（章节学习，%d 门课）' % len(rep['undone_prog']), '']
    if rep['undone_prog']:
        L += ['| # | 课程 | 已完成/总数 | 完成率 |', '|---|------|-------------|--------|']
        for i, it in enumerate(rep['undone_prog'], 1):
            L.append('| %d | %s | %d/%d | %.1f%% |' % (
                i, it['course'], it['done'], it['total'], it['rate']))
        if any(it.get('chapters') for it in rep['undone_prog']):
            L += ['', '### 未完成章节明细', '']
            for it in rep['undone_prog']:
                chs = it.get('chapters') or []
                L += ['**%s**　%d/%d' % (it['course'], it['done'], it['total']), '']
                if chs:
                    L += ['| 章节 | 待完成任务点 |', '|------|--------------|']
                    for ch in chs:
                        L.append('| %s | %d |' % (ch['name'], ch.get('count', 1)))
                    L.append('')
                else:
                    L += ['- （未取到章节明细）', '']
    else:
        L.append('无。')
    L.append('')

    L += ['## 附：全部课程状态总览', '',
          '| # | 课程 | 作业 | 考试 | 任务点 |', '|---|------|------|------|--------|']
    for i, o in enumerate(rep['overview'], 1):
        L.append('| %d | %s | %s | %s | %s |' % (i, o['name'], o['hw'], o['exam'], o['prog']))
    L.append('')

    md_path = OUT_DIR / ('学习通未完成清单_%s.md' % stamp)
    md_path.write_text('\n'.join(L), encoding='utf-8')
    latest = OUT_DIR / '学习通未完成清单.md'
    try:
        shutil.copyfile(md_path, latest)
    except Exception:
        pass

    # ---- CSV
    csv_path = OUT_DIR / ('学习通未完成清单_%s.csv' % stamp)
    with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['类型', '课程', '名称', '状态', '剩余时间/进度', '链接'])
        for it in rep['undone_hw']:
            w.writerow(['作业', it['course'], it['title'], it['state'],
                        it.get('left') or '', it.get('url') or ''])
        for it in rep['undone_exam']:
            w.writerow(['考试', it['course'], it['title'], it['state'], '', ''])
        for it in rep['undone_prog']:
            w.writerow(['任务点', it['course'], '章节学习进度', '未完成',
                        '%d/%d' % (it['done'], it['total']), ''])

    # ---- JSON 快照，用于和上次对比
    snap_path = SNAPSHOT_DIR / ('%s.json' % stamp)
    snap = {'time': rep['time'],
            'undone': [{'type': '作业', 'course': x['course'], 'title': x['title']}
                       for x in rep['undone_hw']] +
                      [{'type': '考试', 'course': x['course'], 'title': x['title']}
                       for x in rep['undone_exam']]}
    snap_path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding='utf-8')

    log('')
    if rep.get('partial'):
        log('⚠️ 本次为中止扫描，只检查了 %d/%d 门课，下面的结果不完整。'
            % (rep.get('scanned', 0), rep['course_total']), 'warn')
    elif rep.get('selected_only'):
        what = ('最近学习的 %d 门课' if rep.get('scope_kind') == 'recent'
                else '你勾选的 %d 门课') % rep['course_total']
        log('⚠️ 本次只检查了%s（账号共 %d 门），其余课程没查。'
            % (what, rep.get('account_total', 0)), 'warn')
    log('✅ 未完成作业 %d 项 / 未完成考试 %d 项 / 未完成任务点 %d 门' % (
        len(rep['undone_hw']), len(rep['undone_exam']), len(rep['undone_prog'])))
    for it in rep['undone_hw']:
        log('   [作业] %s ｜ %s ｜ %s ｜ %s' % (
            cut(it['course'], 22), cut(it['title'], 34), it['state'], it.get('left') or '—'))
    for it in rep['undone_exam']:
        log('   [考试] %s ｜ %s ｜ %s' % (cut(it['course'], 22), cut(it['title'], 34), it['state']))
    for it in rep['undone_prog']:
        log('   [任务点] %s ｜ %d/%d（%.1f%%）' % (
            cut(it['course'], 22), it['done'], it['total'], it['rate']))

    diff = compare_previous(snap_path)
    if diff:
        log('')
        log('与上次扫描对比：')
        for line in diff:
            log('   ' + line)

    log('')
    log('报告已生成：')
    log('   Markdown : %s' % md_path)
    log('   CSV      : %s' % csv_path)
    return {'md': md_path, 'csv': csv_path, 'latest': latest, 'json': snap_path,
            'report': rep}


def compare_previous(cur_path: Path):
    """和最近一次快照对比，列出新增/已完成的未完成项"""
    try:
        snaps = sorted([p for p in SNAPSHOT_DIR.glob('*.json') if p != cur_path])
        if not snaps:
            return []
        prev = json.loads(snaps[-1].read_text(encoding='utf-8'))
        cur = json.loads(cur_path.read_text(encoding='utf-8'))
        key = lambda x: (x['type'], x['course'], x['title'])
        a = {key(x) for x in prev.get('undone', [])}
        b = {key(x) for x in cur.get('undone', [])}
        out = []
        for t, c, ti in sorted(b - a):
            out.append('＋ 新增 %s：%s ｜ %s' % (t, c, ti))
        for t, c, ti in sorted(a - b):
            out.append('－ 已消失 %s：%s ｜ %s' % (t, c, ti))
        if not out:
            out.append('无变化')
        return out
    except Exception as e:
        return ['（对比失败：%s）' % e]


# ================================================================ doctor
def doctor(cfg):
    banner('环境自检')
    log('项目目录：%s' % HERE)
    log('Python  ：%s' % sys.version.split()[0])
    try:
        import playwright
        try:
            from importlib.metadata import version as _v
            pv = _v('playwright')
        except Exception:
            pv = ''
        log('Playwright：已安装 %s' % pv)
    except Exception:
        log('Playwright：未安装 → 执行 安装依赖.bat', 'err')
    try:
        from captcha_solver import HAS_DDDDOCR
        log('ddddocr  ：%s' % ('已安装（可自动过点选验证码）' if HAS_DDDDOCR
                              else '未安装（登录需手动，扫描不受影响）'))
    except Exception as e:
        log('ddddocr  ：不可用（%s）' % e, 'warn')
    exe = _chrome_candidates(cfg)
    log('浏览器内核：%s' % (exe if exe else '未找到（首次扫描会自动下载）'))
    log('登录会话：%s' % ('已保存 → %s' % STATE_FILE if STATE_FILE.exists()
                        else '还没有（请先运行 login）'))
    log('输出目录：%s' % OUT_DIR)
    log('')
    log('提示：学习通改版后想知道本工具还适不适用，运行 '
        'chaoxing_scanner.py selftest（逐层自检，约半分钟）。')
    return True


# ================================================================ 平台兼容性体检
# 学习通是别人的平台，页面结构随时会变。与其等一次全量扫描跑出空清单才发现，
# 不如把工具依赖的每一层拆成独立探针，一次跑完就知道「改版打断了哪一环」。
#
# 这些层就是本工具活着的必要条件，断了各对应一个修复动作：
#   L2 掉 → 什么都查不到（选择器 a[href*=stucoursemiddle] 失效）
#   L4/L5 掉 → 作业抓不到（li[dataname=zy] 或 /work/list 结构变了）
#   L7 掉 → 任务点明细丢失（.chapter_item 失效），作业/考试不受影响
SELFTEST_JS_NAV = r"""
() => [...document.querySelectorAll('li[dataname]')]
        .map(x => x.getAttribute('dataname'))
        .filter(x => x)
"""


def _layer(checks, no, name, status, detail=''):
    checks.append({'layer': no, 'name': name, 'status': status, 'detail': detail})
    icon = {'ok': '✅', 'warn': '⚠️ ', 'fail': '❌', 'skip': '⏭ '}.get(status, '·')
    log('%s  L%d %s%s' % (icon, no, name, ('　' + detail) if detail else ''))


def selftest(cfg, headless=None, sample=3) -> dict:
    """逐层探针，判断学习通改版后本工具是否仍然可用。全程只读。

    sample：向下探几门课（默认 3 门，够用且省时间）。
    """
    checks, notes = [], []
    banner('学习通兼容性体检')
    log('只读检测，不会修改任何课程数据，约半分钟。')
    log('')

    # ---------------- L0 本地运行环境（与平台无关） ----------------
    try:
        import playwright  # noqa: F401
    except Exception:
        _layer(checks, 0, '运行环境', 'fail', 'Playwright 未安装 → 双击「安装依赖.bat」')
        return _selftest_finish(checks, notes)
    try:
        exe = _chrome_candidates(cfg)
    except Exception:
        exe = None
    ddd = '不可用'
    try:
        from captcha_solver import HAS_DDDDOCR
        ddd = '可用' if HAS_DDDDOCR else '不可用（登录需手点，不影响扫描）'
    except Exception:
        pass
    _layer(checks, 0, '运行环境', 'ok' if exe else 'warn',
           '浏览器内核 %s ｜ 验证码自动识别 %s'
           % ('已就绪' if exe else '未找到（首次运行会自动下载）', ddd))

    ctx = None
    try:
        ctx = launch(cfg, headless=headless)
        page = ctx.new_page()

        # ---------------- L1 登录会话 ----------------
        if check_login(page) or (restore_state(ctx) and check_login(page)):
            _layer(checks, 1, '登录会话', 'ok', '登录态有效')
        else:
            _layer(checks, 1, '登录会话', 'fail',
                   '登录态已失效 → 先在界面点「扫码 / 短信登录」，再重新体检')
            notes.append('登录失效属于账号态问题，不是平台改版；重新登录后重跑体检即可。')
            return _selftest_finish(checks, notes)

        # ---------------- L2 课程列表 ----------------
        try:
            courses, cpi = collect_courses(page, cfg)
        except Exception as e:
            _layer(checks, 2, '课程列表', 'fail',
                   '读取异常：%s' % str(e).splitlines()[0][:90])
            return _selftest_finish(checks, notes)
        if not courses:
            _layer(checks, 2, '课程列表', 'fail',
                   '一门课都没解析到 → 课程列表页结构已变'
                   '（选择器 a[href*=stucoursemiddle] 失效）')
            notes.append('课程列表读不到，后面所有功能都无从谈起，需优先修复 collect_courses。')
            return _selftest_finish(checks, notes)
        bad = [c for c in courses if re.match(r'^课程\d+$', c.get('name') or '')]
        if bad and len(bad) / len(courses) > 0.3:
            _layer(checks, 2, '课程列表', 'warn',
                   '共 %d 门，其中 %d 门课程名读不出（退化成「课程<id>」）'
                   '→ 课程名选择器要更新' % (len(courses), len(bad)))
            notes.append('课程名读不出不影响能否查出结果，但报告里的课程名会变成一串编号。')
        else:
            _layer(checks, 2, '课程列表', 'ok',
                   '共解析出 %d 门课程，课程名正常' % len(courses))
        if not cpi:
            notes.append('本次没取到 cpi（个人空间标识），课程页可能打开异常；'
                         '如后续层报错，优先查这一项。')

        # ---------------- L3 任务点进度（卡片概览） ----------------
        with_prog = [c for c in courses if (c.get('prog') or '').strip()]
        if not with_prog:
            _layer(checks, 3, '任务点进度', 'warn',
                   '所有课程都读不到「任务点进度 x/y」→ 可能 .l-txt 选择器失效')
            notes.append('任务点进度只影响卡片概览与「是否有缺口」的判断，'
                         '不会让作业/考试漏查。')
        else:
            _layer(checks, 3, '任务点进度', 'ok',
                   '%d/%d 门课读到进度（其余课程本身可能就没有任务点）'
                   % (len(with_prog), len(courses)))

        # ---------------- L4 课程页左侧导航 ----------------
        probe = courses[:max(1, sample)]
        nav_all = set()
        for c in probe:
            try:
                page.goto(COURSE_PAGE.format(cid=c['cid'], clsid=c['clsid'], cpi=cpi),
                          wait_until='domcontentloaded')
                # 等导航渲染出来（上限仍是 page_wait 秒）
                _poll(page, JS_HAS_NAV, lambda v: bool(v), int(cfg['page_wait'] * 1000))
                nav_all |= set(page.evaluate(SELFTEST_JS_NAV) or [])
            except Exception:
                pass
        if not nav_all:
            _layer(checks, 4, '课程页导航', 'fail',
                   '课程页里读不到任何 li[dataname] 导航项（探了 %d 门课）'
                   '→ 课程页结构已变' % len(probe))
            notes.append('导航项是作业/考试/章节的唯一入口，读不到等于全都抓不到。')
        else:
            hit = [k for k in ('zy', 'ks', 'zj') if k in nav_all]
            _layer(checks, 4, '课程页导航', 'ok' if 'zy' in nav_all else 'warn',
                   '读到导航项 %s；关键入口命中 %s'
                   % (','.join(sorted(nav_all)), ','.join(hit) or '无（作业入口可能被改名）'))
            if 'zy' not in nav_all:
                notes.append('没读到 dataname=zy（作业）。若下一层作业列表仍能打开则无碍，'
                             '若也为空，说明入口被改名，需同步更新 JS_CLICK_WORK。')

        # ---------------- L5 作业列表（最核心） ----------------
        hw_url, hw_no_mod, hw_items = 0, 0, 0
        for c in probe:
            try:
                r = fetch_homework(ctx, page, c, cpi, cfg)
            except Exception as e:
                notes.append('作业页异常（%s）：%s'
                             % (cut(c['name'], 12), str(e).splitlines()[0][:70]))
                continue
            if r.get('no_module'):
                hw_no_mod += 1
            else:
                hw_url += 1
                hw_items += len(r.get('items') or [])
        if hw_url:
            _layer(checks, 5, '作业列表', 'ok',
                   '%d/%d 门课成功打开作业页，共解析 %d 条（另 %d 门无作业模块）'
                   % (hw_url, len(probe), hw_items, hw_no_mod))
        elif hw_no_mod == len(probe):
            _layer(checks, 5, '作业列表', 'warn',
                   '所探 %d 门课都没找到作业模块 —— 可能这几门课本身就没作业，'
                   '也可能入口已改名' % len(probe))
            notes.append('建议挑一门确定有作业的课单独复核：'
                         '`scan --only <courseId:classId>`。')
        else:
            _layer(checks, 5, '作业列表', 'fail', '全部探针课都打不开作业页，详见上方日志')

        # ---------------- L6 考试列表 ----------------
        ex_ok = ex_err = 0
        for c in probe[:2]:
            try:
                fetch_exam(page, c, cpi, cfg)
                ex_ok += 1
            except Exception as e:
                ex_err += 1
                notes.append('考试页异常：%s' % str(e).splitlines()[0][:70])
        _layer(checks, 6, '考试列表', 'fail' if (ex_err and not ex_ok) else 'ok',
               '构造式接口正常（%d/%d 门可打开）' % (ex_ok, ex_ok + ex_err))

        # ---------------- L7 章节下钻（任务点明细） ----------------
        deep_c = None
        for c in courses:
            m = re.search(r'(\d+)\s*/\s*(\d+)', c.get('prog') or '')
            if m and int(m.group(1)) < int(m.group(2)):
                deep_c = c
                break
        if deep_c is None:
            _layer(checks, 7, '章节下钻', 'skip',
                   '账号里没有「任务点有缺口」的课，本次跳过（不影响结论）')
        else:
            try:
                d = fetch_progress_detail(page, deep_c, cpi, cfg)
            except Exception as e:
                d = {'error': str(e).splitlines()[0][:70]}
            if d.get('error'):
                _layer(checks, 7, '章节下钻', 'warn',
                       '章节页异常：%s' % d['error'])
            elif d.get('no_module'):
                _layer(checks, 7, '章节下钻', 'warn',
                       '打不开章节页（%s）→ 章节入口可能已变' % cut(deep_c['name'], 14))
            elif d.get('nodes', 0) <= 0:
                _layer(checks, 7, '章节下钻', 'fail',
                       '章节页打开了，但解析到 0 个节点 → .chapter_item 选择器已失效')
                notes.append('后果：任务点明细会全部丢失（作业/考试不受影响）。')
            else:
                _layer(checks, 7, '章节下钻', 'ok',
                       '在「%s」解析到 %d 个章节节点、%d 个未完成'
                       % (cut(deep_c['name'], 14), d.get('nodes', 0),
                          len(d.get('pending') or [])))
    finally:
        if ctx is not None:
            try:
                save_state(ctx)
            except Exception:
                pass
            close_ctx(ctx)

    return _selftest_finish(checks, notes)


def _selftest_finish(checks, notes) -> dict:
    """汇总体检结论，并落一份可留存的报告"""
    fails = [c for c in checks if c['status'] == 'fail']
    warns = [c for c in checks if c['status'] == 'warn']
    skips = [c for c in checks if c['status'] == 'skip']

    if fails:
        head = ('❌ 体检未通过：%d 层失效 → 工具当前可能查不到或查不全，需要更新解析规则'
                % len(fails))
    elif warns:
        head = '⚠️ 体检基本通过：%d 处需要留意，但主要功能可用' % len(warns)
    else:
        head = '✅ 体检通过：学习通当前版本与本工具完全兼容，可放心使用'

    log('')
    log('─' * 62)
    log('  ' + head)
    for c in fails:
        log('    ❌ L%d %s：%s' % (c['layer'], c['name'], c['detail']))
    for c in warns:
        log('    ⚠️  L%d %s：%s' % (c['layer'], c['name'], c['detail']))
    for c in skips:
        log('    ⏭  L%d %s：%s' % (c['layer'], c['name'], c['detail']))
    log('─' * 62)
    if notes:
        log('说明：')
        for n in notes:
            log('  · ' + n)
    log('')
    if fails:
        log('把下面的体检报告文件发给助手，即可定位并更新对应的解析规则。', 'warn')
    else:
        log('结论：学习通改版没有破坏本工具依赖的环节。', 'ok')

    path = None
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        path = OUT_DIR / ('兼容性体检_%s.md' % datetime.now().strftime('%Y%m%d_%H%M'))
        icon = {'ok': '✅ 通过', 'warn': '⚠️ 留意', 'fail': '❌ 失效', 'skip': '⏭ 跳过'}
        lines = ['# 学习通兼容性体检报告', '',
                 '- 时间：%s' % datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                 '- 结论：%s' % head, '',
                 '| 层 | 检查项 | 结果 | 实测情况 |', '|---|---|---|---|']
        for c in checks:
            lines.append('| L%d | %s | %s | %s |'
                         % (c['layer'], c['name'], icon.get(c['status'], c['status']),
                            (c['detail'] or '').replace('|', '\\|')))
        if notes:
            lines += ['', '## 说明'] + ['- ' + n for n in notes]
        lines += ['', '> 本报告由只读检测生成，用于判断「学习通改版后工具是否还适用」。']
        path.write_text('\n'.join(lines), encoding='utf-8')
        log('体检报告：%s' % path)
    except Exception as e:
        log('体检报告写入失败：%s' % e, 'warn')

    return {'verdict': 'fail' if fails else ('warn' if warns else 'ok'),
            'checks': checks, 'notes': notes, 'head': head,
            'md': str(path) if path else ''}


# ================================================================ 入口
def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='chaoxing_scanner',
        description='学习通未完成事项巡检工具（查询只读；刷课/答题需手动触发）')
    sub = ap.add_subparsers(dest='cmd')

    p = sub.add_parser('login', help='登录并保存会话')
    p.add_argument('--auto', action='store_true', help='短信验证码全自动登录')
    p.add_argument('--phone', default='', help='手机号（配合 --auto 或 --password）')
    p.add_argument('--password', default='', help='用手机号+密码登录（无需 --auto）')

    p = sub.add_parser('scan', help='扫描并生成清单')
    p.add_argument('--limit', type=int, default=0,
                   help='只扫最近 N 门课（按学习通默认顺序，最近学习过的在前）')
    p.add_argument('--only', default='',
                   help='只扫指定课程，写课程标识（cid:clsid），多个用逗号分隔')
    p.add_argument('--headed', action='store_true', help='显示浏览器窗口')
    p.add_argument('--no-open', action='store_true', help='生成后不自动打开报告')
    p.add_argument('--no-deep', action='store_true', help='跳过任务点章节下钻（更快）')

    p = sub.add_parser('list', help='只列出课程名录，不抓任何作业（约 10 秒）')
    p.add_argument('--headed', action='store_true', help='显示浏览器窗口')

    p = sub.add_parser('brush',
                       help='自动刷未完成的任务点视频（倍速静音真实播放）')
    p.add_argument('courses', nargs='+',
                   help='课程名（支持部分匹配，可给多个）')
    p.add_argument('--headed', action='store_true', help='显示浏览器窗口')
    p.add_argument('--rate', type=float, default=None,
                   help='播放倍速（默认取 config 的 brush_rate，再默认 1.25；'
                        '只在官方档位 1.0/1.25/1.5/2.0 之间生效，自动吸附最近档）')

    p = sub.add_parser('answer',
                       help='用大模型自动做章节任务点里的作业（先在 config 填 llm_url/llm_key/llm_model）')
    p.add_argument('courses', nargs='+',
                   help='课程名（支持部分匹配，可给多个）')
    p.add_argument('--headed', action='store_true', help='显示浏览器窗口')
    p.add_argument('--submit', action='store_true',
                   help='答完直接交卷（默认预演：只答题不落库，答完列出可交卷清单）')

    sub.add_parser('doctor', help='检查运行环境')
    sub.add_parser('selftest',
                   help='检测学习通是否改版（逐层验证解析规则是否还有效）')

    p = sub.add_parser('gui', help='打开本地网页控制台')
    p.add_argument('--port', type=int, default=8765)

    args = ap.parse_args(argv)
    cfg = load_config()

    # CLI 长任务（刷课 / 答题）同样阻止系统睡眠；进程退出时自动恢复
    if args.cmd in ('brush', 'answer'):
        keep_awake(True)
        import atexit
        atexit.register(keep_awake, False)

    if args.cmd == 'login':
        if args.password:
            ok = do_login_password(cfg, args.phone, args.password)
        else:
            ok = do_login(cfg, auto=args.auto, phone=args.phone)
        return 0 if ok else 1

    if args.cmd == 'list':
        try:
            out = list_courses(cfg, headless=False if args.headed else None)
        except NotLoggedIn as e:
            log('%s' % e, 'err')
            return 1
        log('')
        log('共 %d 门课程：' % len(out['courses']))
        for i, c in enumerate(out['courses'], 1):
            log('%3d. %s%s' % (i, cut(c['name'], 40),
                               ('　[%s]' % c['key']) if c.get('key') else ''))
        return 0

    if args.cmd == 'scan':
        only = [x.strip() for x in (args.only or '').split(',') if x.strip()]
        try:
            scan(cfg, limit=args.limit, only=only,
                 headless=False if args.headed else None,
                 open_report=False if args.no_open else None,
                 deep=not args.no_deep)
            return 0
        except NotLoggedIn as e:
            log(str(e), 'err')
            return 2
        except KeyboardInterrupt:
            log('已中断', 'warn')
            return 130
        except Exception as e:
            log('扫描失败：%s' % e, 'err')
            import traceback
            log(traceback.format_exc()[-1200:], 'err')
            return 1

    if args.cmd == 'brush':
        if args.rate:
            cfg['brush_rate'] = args.rate
        try:
            # 课程名 → course_key：先读一遍名录做部分匹配
            ctx = launch(cfg, headless=False if args.headed else None)
            page = ctx.new_page()
            try:
                if not check_login(page):
                    if not (restore_state(ctx) and check_login(page)):
                        log('未登录，请先 login', 'err')
                        return 1
                courses, cpi = collect_courses(page, cfg)
            finally:
                save_state(ctx)
                close_ctx(ctx)
            keys = []
            for name in args.courses:
                hit = [c for c in courses if name in c['name']]
                if not hit:
                    log('没找到课程：%s（可先跑 list 看名单）' % name, 'warn')
                    continue
                keys += [course_key(c) for c in hit]
            if not keys:
                return 1
            brush_videos(cfg, keys)
            return 0
        except NotLoggedIn as e:
            log(str(e), 'err')
            return 2
        except KeyboardInterrupt:
            log('已中断', 'warn')
            return 130
        except Exception as e:
            log('刷视频失败：%s' % e, 'err')
            import traceback
            log(traceback.format_exc()[-1200:], 'err')
            return 1

    if args.cmd == 'answer':
        try:
            ctx = launch(cfg, headless=False if args.headed else None)
            page = ctx.new_page()
            try:
                if not check_login(page):
                    if not (restore_state(ctx) and check_login(page)):
                        log('未登录，请先 login', 'err')
                        return 1
                courses, cpi = collect_courses(page, cfg)
            finally:
                save_state(ctx)
                close_ctx(ctx)
            keys = []
            for name in args.courses:
                hit = [c for c in courses if name in c['name']]
                if not hit:
                    log('没找到课程：%s（可先跑 list 看名单）' % name, 'warn')
                    continue
                keys += [course_key(c) for c in hit]
            if not keys:
                return 1
            answer_courses(cfg, keys, submit=True if args.submit else 'dry')
            return 0
        except NotLoggedIn as e:
            log(str(e), 'err')
            return 2
        except KeyboardInterrupt:
            log('已中断', 'warn')
            return 130
        except Exception as e:
            log('答题失败：%s' % e, 'err')
            import traceback
            log(traceback.format_exc()[-1200:], 'err')
            return 1

    if args.cmd == 'doctor':
        doctor(cfg)
        return 0

    if args.cmd == 'selftest':
        res = selftest(cfg)
        return 3 if res.get('verdict') == 'fail' else 0

    if args.cmd == 'gui':
        import webui
        webui.serve(port=args.port)
        return 0

    ap.print_help()
    return 0


if __name__ == '__main__':
    sys.exit(main())
