# -*- coding: utf-8 -*-
"""
学习通巡检工具 · 本地网页控制台
------------------------------------------------------------------
只监听 127.0.0.1，不对外网开放。账号密码只在本机内存中使用，
不写入任何文件（登录成功后保存的是会话 cookie，不含密码）。

用法： python chaoxing_scanner.py gui
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urlsplit


class _StrictHTTPServer(ThreadingHTTPServer):
    """禁用 SO_REUSEADDR 的 HTTP 服务器。

    ThreadingHTTPServer 默认 allow_reuse_address=1，在 Windows 上会让
    套接字**绑上一个已被别的进程监听的端口**（不像 Linux 那样要求端口
    空闲）——于是 _open_server 那个「逐个试端口」形同虚设：端口被占时
    它照样「绑成功」，之后连接被系统随机分给两个监听者，用户看到的是
    别人的页面 / 半死不活的服务（真实踩到：本机残留的
    `python -m http.server 8791` 占了端口，工具以为自己起在 8791 上，
    实测请求却打到了那个 http.server）。
    置 False 后端口被占即如实抛 OSError，逐个试端口才真正生效。
    关闭后立即重绑不受影响（我们是主动 close，没有 TIME_WAIT 线程占用）。
    """
    allow_reuse_address = False

# 便携版（Python embeddable）的 ._pth 会让解释器忽略脚本所在目录
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import chaoxing_scanner as cs

LOCK = threading.Lock()
STATE = {
    'running': False,
    'task': '',
    'lines': [],
    'dropped': 0,           # 日志累计丢弃行数（截断时递增，见 _sink）
    'done': False,
    'paused': False,
    'phase': '',
    'summary': None,
    'result': None,
    'report': '',
    'courses': None,        # 「获取课程列表」的结果，供界面勾选
    'courses_v': 0,         # courses 的版本号：变了才整份下发（机制同 result_v）
    'started': 0.0,
    'login_fail': None,     # 密码登录被平台明确回绝 → 'credential'，界面弹红字提示
    'result_v': 0,          # 结果的版本号：变了界面才重画，见 _set_result()
    'ares': None,           # 交卷结果面板（逐份状态，未确认行可「重新核实」）
    'ares_v': 0,
    # scan() 本次的课程表 + 逐课结果 + 范围参数：界面「单门刷新」靠它把重抓的
    # 一门课并回当前清单、重建报告（见 cs.rebuild_with_row）。
    'scan_internal': None,
}


def _set_result(rep):
    """写入结果并递增版本号。

    界面原来每 900ms 把整块结果重绘一次（innerHTML 整体覆盖），长报告要反复
    重建上百行 DOM，正在选中的文字还会被抖掉。改成用版本号说话之后，轮询只在
    这一版还没交出去时才把报告带上，界面也只在换版时重画一次。
    """
    with LOCK:
        STATE['result'] = rep
        STATE['result_v'] += 1


def _set_ares(rep):
    """交卷结果面板（答题/交卷任务的逐份结果），机制同 _set_result。

    与扫描报告（result）分开放：两种任务的面板长得完全不一样，
    而且可能先后出现——各用各的版本号互不打架。
    """
    with LOCK:
        STATE['ares'] = rep
        STATE['ares_v'] += 1

# 暂停 / 中止信号。扫描器会在每个「课程边界」调用 _control_hook 来检查，
# 所以这两个信号最迟在下一门课开始时生效（一般几秒），不会撕裂正在进行的请求。
PAUSE = threading.Event()
CANCEL = threading.Event()


def _control_hook(stage: str) -> bool:
    """由扫描器在课程边界调用。返回 False 表示中止本次扫描。"""
    if stage == 'reporting':
        # 已进入收尾：无论如何都要把报告写完，同时让界面显示「收尾中」
        with LOCK:
            STATE['phase'] = 'finishing'
        cs.log('扫描结束，正在收尾：生成报告 …')
        return True
    # 登录等待没有「进度」可暂停——它本身就是在等你操作浏览器。若在这里尊重
    # 暂停，会卡住 while 的 480s 截止期、把 running 永远留 True（所有任务按钮
    # 409 锁死），还念一句「点继续接着扫」的扫描专用台词（登录没有「扫」）。
    # 所以登录阶段**只认停止**，暂停对它等于没点（界面也会把暂停键禁用）。
    # 真机审查 2026-10-14 逮到：把控制钩子接进登录等待时漏了这一条。
    if stage.startswith('login'):
        return not CANCEL.is_set()
    if CANCEL.is_set():
        return False
    if PAUSE.is_set():
        cs.log('⏸ 已暂停（进度保留）。点「继续」接着扫，或点「停止」结束并出报告。', 'warn')
        while PAUSE.is_set():
            if CANCEL.is_set():
                break
            time.sleep(0.15)
        if CANCEL.is_set():
            return False
        cs.log('▶ 已继续扫描')
    return True


def _sink(line, level):
    with LOCK:
        STATE['lines'].append({'t': line, 'lvl': level})
        if len(STATE['lines']) > 5000:
            del STATE['lines'][:1500]
            # 累计丢弃行数：前端拿它发现「自己手里的 since 已经被截走」，
            # 清空日志从 0 重拉。不用它的话，截断后前端 since 永远指向
            # 不存在的下标，日志停在半空还会周期性跳段（长任务必踩）。
            STATE['dropped'] = STATE.get('dropped', 0) + 1500


cs.add_sink(_sink)

TASK_NAME = {
    'query': '一键查询（登录 + 扫描全部课程）',
    'scan': '扫描全部课程',
    'list': '获取课程列表',
    'login': '扫码 / 短信登录',
    'doctor': '环境自检',
    'selftest': '学习通兼容性检测',
    'brush': '自动刷视频（倍速静音）',
    'answer': '做作业（大模型答题）',
    'combo': '刷课 + 刷题（先刷视频再做作业交卷）',
    'verify': '重新核实未确认的作业',
    'answer_one': '刷题（单份作业）',
    'refresh_one': '刷新单门课程状态',
    'brush_urls': '上传链接刷课',
    'answer_urls': '上传链接刷题',
    'combo_urls': '上传链接刷课+刷题',
    'brush_act': '课堂活动刷课',
    'answer_act': '课堂活动作答',
    'combo_act': '课堂活动刷课+刷题',
}


def _shutdown_pc():
    """「刷完自动关机」：任务正常跑完才调用。留 60 秒缓冲，
    万一用户正巧在电脑前想取消，运行 shutdown /a 即可撤掉。"""
    if os.name != 'nt':
        cs.log('自动关机目前只支持 Windows，本次跳过。', 'warn')
        return
    cs.log('⚡ 任务完成：60 秒后自动关机（想取消可在 cmd 里运行 shutdown /a）',
           'warn')
    try:
        r = subprocess.run(['shutdown', '/s', '/t', '60'], capture_output=True,
                           text=True, errors='ignore')
        if r.returncode != 0:
            # 关机请求被系统/安全软件拒绝时必须说出来，不能静默装成功
            detail = (r.stderr or '').strip() or ('返回码 %s' % r.returncode)
            cs.log('⚠ 关机请求未成功：%s。电脑将保持开着，请手动关机。' % detail,
                   'err')
    except Exception as e:
        cs.log('发起关机失败：%s。电脑将保持开着，请手动关机。' % e, 'err')


_SCREEN_LOCK = threading.Lock()   # 窗口类名固定，并发熄屏会互抢注册


def _screen_off():
    """「熄屏」：立刻强制关闭显示器。

    Chrome 播视频时会自带 Video Wake Lock（阻止屏幕自动关闭，
    powercfg /requests 里 DISPLAY 栏的 chrome.exe 就是它），关不掉；
    但显式发 SC_MONITORPOWER 仍可强制熄屏，直到下一次鼠标/键盘输入。
    熄屏不影响任务运行——刷课在无头浏览器里继续。

    实现要点（2026-09-29 修正）：不能用 HWND_BROADCAST 广播——广播会把
    「关显示器」塞给每一个顶层窗口，等于连发几十次，跟 Chrome 的
    Video Wake Lock 来回打架，实测会「黑屏 + 有规律闪烁 + 唤不醒」，
    只能强按电源键。现在改为本进程自建一个隐藏窗口，只发这一次。
    极个别显卡驱动即使这样也会卡在黑屏（该 API 的已知毛病），
    那种情况按 Win+Ctrl+Shift+B 重置显卡驱动即可恢复，不用强制关机。
    """
    if os.name != 'nt':
        cs.log('熄屏目前只支持 Windows。', 'warn')
        return
    with _SCREEN_LOCK:      # 连点两次：等第一次注册/注销完再做第二次
        try:
            import ctypes
            import ctypes.wintypes as wt  # 必须显式导入，wintypes 才可用
            user32 = ctypes.windll.user32
            WM_SYSCOMMAND = 0x0112
            SC_MONITORPOWER = 0xF170

            # 64 位下句柄/返回值必须显式定型，ctypes 默认 int 会截断
            WNDPROC = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t, wt.HWND, wt.UINT, ctypes.c_size_t,
                ctypes.c_ssize_t)
            # 不定型的话 ctypes 默认按 32 位 int 转参，64 位句柄会 OverflowError，
            # 窗口过程收到的每条消息都送不进 DefWindowProc
            user32.DefWindowProcW.argtypes = [
                wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
            user32.DefWindowProcW.restype = ctypes.c_ssize_t
            defproc = WNDPROC(user32.DefWindowProcW)  # 引用须存活到发完消息

            class WNDCLASSW(ctypes.Structure):
                _fields_ = [
                    ('style', wt.UINT),
                    ('lpfnWndProc', WNDPROC),
                    ('cbClsExtra', ctypes.c_int),
                    ('cbWndExtra', ctypes.c_int),
                    ('hInstance', wt.HINSTANCE),
                    ('hIcon', wt.HICON),
                    ('hCursor', wt.HANDLE),  # wintypes 没有 HCURSOR，HANDLE 等价
                    ('hbrBackground', wt.HBRUSH),
                    ('lpszMenuName', wt.LPCWSTR),
                    ('lpszClassName', wt.LPCWSTR)]

            cls_name = 'CxScreenOffWnd'
            hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
            user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
            user32.CreateWindowExW.restype = wt.HWND
            wc = WNDCLASSW()
            wc.lpfnWndProc = defproc
            wc.hInstance = hinst
            wc.lpszClassName = cls_name
            if not user32.RegisterClassW(ctypes.byref(wc)):
                raise OSError('RegisterClassW 失败（错误码 %d）'
                              % ctypes.windll.kernel32.GetLastError())
            # 不调 ShowWindow 的隐藏窗口：能收到消息、不进任务栏
            hwnd = user32.CreateWindowExW(
                0, cls_name, cls_name, 0, 0, 0, 0, 0, None, None, hinst, None)
            if not hwnd:
                raise OSError('CreateWindowExW 失败（错误码 %d）'
                              % ctypes.windll.kernel32.GetLastError())
            try:
                user32.SendMessageW(hwnd, WM_SYSCOMMAND, SC_MONITORPOWER, 2)
            finally:
                # 发送本身抛异常也不能留下半注册的窗口类，
                # 否则之后每一次熄屏都失败
                user32.DestroyWindow(hwnd)
                user32.UnregisterClassW(cls_name, hinst)
            cs.log('已熄屏。动下鼠标或按任意键点亮；万一黑屏唤不醒，'
                   '按 Win+Ctrl+Shift+B 重置显卡驱动即可恢复，不必强制关机。')
        except Exception as e:
            cs.log('熄屏失败：%s' % e, 'err')


# 「去完成」常驻作业窗口：一个带登录态的浏览器伺候多次点击。
# 以前把链接丢给系统默认浏览器，用户得重新登录一次；现在改走工具
# 自己的 Chrome（persistent profile + restore_state），点开即已登录。
# 独立线程 + 独立 sync_playwright 实例：sync API 不能跨线程操作，
# 复用活着的浏览器开新标签页比每次重开快，也避免 profile 目录被
# 两个进程同时占住。窗口期间任务互斥由 /api/open-work 的 busy 闸 +
# 用户自觉（日志里已提示）兜底。
_WORK = {'q': None}
_WORK_CLOSE = object()      # 队列哨兵：让作业窗口线程自己优雅收尾


def _close_work_window(timeout=10.0):
    """开新任务前替用户关掉「去完成」的作业窗口。

    作业窗口与任务共用同一个 profile（cookie 罐）：窗口开着时任何
    launch 都会因 profile 被占而秒死——真机 2026-10-10「一键查询报
    所有浏览器内核都无法启动」就是它（自带内核 / msedge 全部
    Target page, context or browser has been closed）。窗口线程还
    活着且持有 ctx，往队列投关闭哨兵让它自己走 finally 收尾最稳。
    """
    q = _WORK['q']
    if q is None:
        return True
    try:
        q.put(_WORK_CLOSE)
    except Exception:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _WORK['q'] is None:
            return True
        time.sleep(0.2)
    return False


def _open_work_async(url):
    q = _WORK['q']
    if q is not None:
        q.put(url)
        cs.log('作业窗口已开着，这次在窗口里新开标签页。')
        return
    q = queue.Queue()
    _WORK['q'] = q

    def run():
        ctx = None
        pages = []          # 本线程开的作业页（v4.3：共享上下文里只认自己的）
        try:
            cfg = cs.load_config()
            ctx = cs.launch(cfg, headless=False)
            cs.restore_state(ctx)
            page = ctx.new_page()
            pages.append(page)
            page.goto(url, wait_until='domcontentloaded', timeout=60000)
            cs.log('已用本机登录态打开作业窗口（不用重新登录）。'
                   '做完直接关掉窗口即可；开「一键查询 / 刷题」任务时'
                   '工具也会自动关闭它。')
            while True:
                try:
                    nxt = q.get(timeout=1.0)
                except queue.Empty:
                    nxt = None
                if nxt is _WORK_CLOSE:   # 任务要开跑，替用户收掉窗口
                    break
                # v4.3：ctx 是全局共享的，ctx.pages 里还有别的线程（并发任务）
                # 的页面——只能看**本线程**开的作业页是否都被用户关了。
                if all(p.is_closed() for p in pages):
                    break
                if nxt:
                    try:
                        p = ctx.new_page()
                        pages.append(p)
                        p.goto(nxt, wait_until='domcontentloaded',
                               timeout=60000)
                        cs.log('已在作业窗口新开标签页。')
                    except Exception as e:
                        cs.log('打开链接失败：%s' % e, 'warn')
        except Exception as e:
            cs.log('作业窗口打开失败：%s' % e, 'warn')
        finally:
            if ctx is not None:
                cs.close_ctx(ctx)   # 只关本线程的作业页 + 断连接，不关常驻浏览器
            _WORK['q'] = None
            cs.log('作业窗口已关闭，「去完成」下次点击会重新开窗。')

    threading.Thread(target=run, daemon=True, name='open-work').start()


def _spawn(fn, *args):
    """任务线程统一入口：运行期间阻止系统自动睡眠（屏幕仍可关）。

    刷课/答题动辄半小时起步，Windows 电源计划到点会把机器睡掉，
    任务就断在半路。这里在任务线程内持有「阻止睡眠」状态，
    结束（含异常）时恢复。SetThreadExecutionState 按线程持有，
    所以必须包在任务线程里开关，不能在主线程调一次完事。
    """
    def run():
        # 运行期是否阻止休眠由配置决定（设置页开关；默认开）。
        # 读配置失败就按默认开处理——宁可多防一次，也别让半小时的刷课断在半路。
        _ka = True
        try:
            _ka = bool(cs.load_config().get('keep_awake', True))
        except Exception:
            _ka = True
        if _ka:
            cs.keep_awake(True)
        try:
            fn(*args)
        finally:
            if _ka:
                cs.keep_awake(False)
            # 兜底复位：worker 若在进入自己的 try 之前就抛了（如请求里
            # limit 填了非数字），它的 finally 不会执行，running 永远
            # 停在 True，之后所有任务按钮都 409 锁死，只能重启程序。
            # 正常收尾时 running 已是 False，这里是幂等空操作。
            with LOCK:
                if STATE['running']:
                    STATE.update(running=False, done=True,
                                 paused=False, phase='')
                    _reset = True
                else:
                    _reset = False
            # cs.log 绝不能放进上面的 with LOCK —— log 会走 _sink，而 _sink
            # 自己也要拿 LOCK，Lock 又不可重入（见 ①b），当场自死锁：任务
            # 线程永久挂死、LOCK 再也不释放，之后每次 poll 全卡（按钮全没
            # 反应 + ×关不掉）。日志一律在锁外打。
            if _reset:
                cs.log('检测到任务异常收尾，已复位任务状态。', 'warn')
    th = threading.Thread(target=run, daemon=True, name='ui-task')
    with LOCK:
        STATE['_thread'] = th    # 退出时要等它收尾，见 _wait_task
    th.start()


def _worker(action, opts):
    phone = (opts.get('phone') or '').strip()
    password = opts.get('password') or ''
    try:
        # 安全转换：opts 来自 HTTP 请求体，limit 可能是任意字符串。
        # 裸 int() 会在 try 之前抛 ValueError，落到 _spawn 的兜底复位分支
        # （以前那里还带一个锁内 log 的死锁，见 _spawn 注释）。
        limit = max(int(opts.get('limit') or 0), 0)
    except (TypeError, ValueError):
        limit = 0
    deep = bool(opts.get('deep', True))
    only = [str(k) for k in (opts.get('only') or []) if k]

    cs.log('')
    cs.log('▶ 任务开始：%s' % TASK_NAME.get(action, action))
    PAUSE.clear()
    CANCEL.clear()
    with LOCK:
        STATE['paused'] = False
        # 读课程名录是十来秒的只读操作，不参与暂停/停止，单独标一个阶段
        # 兼容性体检同理：它是只读探针，内部没有检查点，所以也不能被暂停
        # 登录也单标一个阶段：它只认「停止」不认「暂停」（见 _control_hook），
        # 前端据 phase=='login' 把暂停键置灰，别给用户一个点了没用的按钮。
        if action in ('list', 'selftest'):
            STATE['phase'] = 'listing'
        elif action == 'login':
            STATE['phase'] = 'login'
        else:
            STATE['phase'] = 'scanning'
    cs.set_control_hook(_control_hook)
    try:
        cfg = cs.load_config()

        if action == 'doctor':
            cs.doctor(cfg)
            return

        if action == 'selftest':
            # 逐层探针判断学习通改版后工具是否还适用。只读短任务，不参与暂停/停止
            cs.set_control_hook(None)
            res = cs.selftest(cfg)
            with LOCK:
                STATE['summary'] = {'体检结论': res.get('head', '')}
            return

        if action == 'login':
            # fresh=True：点这个按钮就是要登录/换号——不自动恢复旧会话，
            # 否则一点按钮就登回旧账号，用户根本没机会扫新码（实测踩过）。
            # 同时把旧账号的浏览器 profile 整体归档：多个账号轮流共用
            # 同一个 cookie 罐，平台侧会把它们关联成同一团伙。
            if cs.fresh_profile() is False:
                raise RuntimeError(
                    '旧浏览器痕迹归档失败（可能有浏览器窗口还开着）。'
                    '请关闭本工具后重新打开再点登录。')
            ok = cs.do_login(cfg, auto=False, fresh=True)
            if not ok:
                # 没登成 → 把旧账号的痕迹与会话挪回来。否则用户只是点了一下
                # 登录、或 8 分钟没点完验证码，原来的登录态就白丢了：
                # 下次查询还得重登，反而更容易反复撞验证码。
                cs.restore_profile()
            with LOCK:
                STATE['summary'] = {'登录': '成功' if ok else '未完成'}
            return

        # 「获取课程列表」和「查询」都要先登录，共用同一套逻辑
        if action in ('query', 'list'):
            if phone and password:
                cs.log('使用账号密码登录 …')
                if not cs.do_login_password(cfg, phone, password):
                    if cs.last_login_fail() == 'stopped':
                        # 用户自己点的「停止」：不是失败，别报成密码错误。
                        # 之前这条会落进下面的 RuntimeError，弹「请核对手机号与密码 /
                        # 密码连续输错可能触发账号保护」——把主动停止说成账号有问题，
                        # 白白吓用户一跳（v6.10 加了等待期可停止后才可能出现）。
                        with LOCK:
                            STATE['summary'] = {'登录': '已停止'}
                        cs.log('已按你的要求停止登录（未登录成功）。', 'warn')
                        return
                    if cs.last_login_fail() == 'credential':
                        # 平台明确回绝了这组账号密码。这种失败在界面上用红字讲清楚就够了，
                        # 不必再抛异常打一堆堆栈（那样用户只会看到「任务失败」+ traceback）。
                        # 同时清掉上一次的旧结果：否则结果区里那份旧报告会一直留着，
                        # 用户会以为那是这次查出来的东西。
                        with LOCK:
                            STATE['login_fail'] = 'credential'
                        _set_result(None)
                        cs.log('账号或密码错误。请核对账号（手机号 / 超星号）与密码后重试，'
                               '或改用设置页的「扫码 / 短信登录」。', 'err')
                        return
                    raise RuntimeError(
                        '登录未成功。请核对手机号与密码，'
                        '或改用设置页的「扫码 / 短信登录」。（密码连续输错可能触发账号保护）')
            else:
                cs.log('未填账号密码，尝试使用已保存的登录会话 …')

        if action == 'list':
            # 只读名录，不注入控制钩子（避免界面按钮给出无效承诺）
            cs.set_control_hook(None)
            out = cs.list_courses(cfg)
            items = out.get('courses') or []
            with LOCK:
                STATE['courses'] = items
                STATE['courses_v'] = STATE.get('courses_v', 0) + 1
                STATE['summary'] = {'课程总数': len(items)}
            if items:
                cs.log('课程列表已就绪：勾选要查询的课程（勾上即生效），再点「开始查询」。', 'ok')
                cs.log('不勾选 = 照旧查全部。')
            return

        if action == 'refresh_one':
            # 单门刷新：只重抓这一门课，复用常驻登录态，**不重读课程列表**、
            # 也不碰其它课。比整轮 scan 快一个数量级（3~4 秒 vs 12 门 ~47 秒）。
            # 失败时**不动结果面板**（留着用户原来那份清单），只更新统计卡片 + 日志；
            # 但总要 _set_result 一次，好让界面的「正在刷新…」收掉、↻ 按钮恢复可用。
            cid = str(opts.get('cid') or '').strip()
            clsid = str(opts.get('clsid') or '').strip()
            with LOCK:
                internal = STATE.get('scan_internal')
                last = STATE.get('result')
            if not cid or not internal:
                cs.log('单门刷新失败：还没有可用的完整查询结果，'
                       '请先点「开始查询」跑一遍。', 'err')
                with LOCK:
                    STATE['summary'] = {'提示': '请先完整查询一次，再单门刷新'}
                if last is not None:
                    _set_result(last)
                else:
                    _set_result({'error': '还没有可刷新的结果，'
                                 '请先点「开始查询」完整查询一次'})
                return
            # 课程名与任务点进度从上次扫描的缓存里取（前端只传定位键）
            name, prog = '', ''
            for c in (internal.get('courses') or []):
                if str(c.get('cid')) == cid and str(c.get('clsid')) == clsid:
                    name, prog = c.get('name', ''), c.get('prog', '')
                    break
            try:
                out = cs.refresh_course(cfg, cid, clsid, name=name, prog=prog,
                                        deep=deep)
                rep = cs.rebuild_with_row(internal, cid, clsid, out.get('row'))
                if rep is None:
                    raise RuntimeError('这门课不在上次的查询范围内（课程可能有变动）')
                with LOCK:
                    STATE['scan_internal'] = internal
                paths = cs.write_outputs(rep)
                with LOCK:
                    STATE['report'] = str(paths.get('md') or '')
                    STATE['summary'] = {
                        '课程': rep.get('course_total', 0),
                        '未完成作业': len(rep.get('undone_hw', [])),
                        '未完成考试': len(rep.get('undone_exam', [])),
                        '未完成任务点': len(rep.get('undone_prog', [])),
                    }
                    nf = len(rep.get('page_failed') or [])
                    if nf:
                        STATE['summary']['页面未加载'] = nf
                _set_result(rep)
                cs.log('已刷新「%s」的状态。' % (name or cid))
            except Exception as e:
                cs.log('单门刷新失败：%s' % e, 'err')
                with LOCK:
                    STATE['summary'] = {'提示': '刷新失败：%s' % str(e)[:60]}
                if last is not None:
                    _set_result(last)   # 重发原报告：收回「正在刷新…」、恢复 ↻ 可用
                else:
                    # 连旧报告都没有（如凭据被拒后 result 被清空）：也得发一版，
                    # 否则 result_v 不涨、poll 不重画，界面停在「正在刷新…」永不收口
                    _set_result({'error': '刷新失败：%s' % str(e)[:120]})
            return

        out = cs.scan(cfg, limit=limit, only=only,
                      headless=None, open_report=False, deep=deep)
        rep = out.get('report') or {}
        with LOCK:
            STATE['scan_internal'] = out.get('_internal')
            STATE['report'] = str(out.get('md') or '')
            STATE['summary'] = {
                '课程': rep.get('course_total', 0),
                '未完成作业': len(rep.get('undone_hw', [])),
                '未完成考试': len(rep.get('undone_exam', [])),
                '未完成任务点': len(rep.get('undone_prog', [])),
            }
            # 有页面没加载成功时多给一张卡，明示「结果不可信」；没有就不出现这张卡
            nf = len(rep.get('page_failed') or [])
            if nf:
                STATE['summary']['页面未加载'] = nf
        _set_result(rep)
    except cs.NotLoggedIn as e:
        cs.log('%s' % e, 'err')
        with LOCK:
            STATE['summary'] = {'提示': '未登录，请填账号密码或点「扫码 / 短信登录」'}
        _set_result({'error': str(e) or '未登录'})
    except Exception as e:
        cs.log('任务失败：%s' % e, 'err')
        import traceback
        cs.log(traceback.format_exc()[-900:], 'err')
        # 结果区给一版错误卡片：不然「查询中…」占位永远挂着（rv 没变
        # 前端不会重画），用户只看到无限转圈，失败原因却藏在日志页签
        _set_result({'error': str(e)[:200] or type(e).__name__})
    finally:
        cs.set_control_hook(None)
        PAUSE.clear()
        CANCEL.clear()
        with LOCK:
            STATE['running'] = False
            STATE['done'] = True
            STATE['paused'] = False
            STATE['phase'] = ''
        cs.log('■ 任务结束')


def _apply_rate(opts, cfg):
    """把界面选的倍速写进 cfg（非法值由 cs.g_rate 吸附到最近官方档位兜底）。

    返回 True 表示 cfg 被改过，调用方决定要不要 save_config 记住它。
    """
    r = opts.get('rate')
    if r in (None, ''):
        return False
    try:
        cfg['brush_rate'] = float(r)
    except (TypeError, ValueError):
        return False
    return True


def _brush_worker(opts):
    """「刷选中的课的视频」的后台线程。

    与扫描共用暂停/停止信号（PAUSE/CANCEL），但控制粒度更细：
    刷视频的 control() 会把视频元素本身也暂停，而不是只停任务推进。
    """
    only = [str(k) for k in (opts.get('only') or []) if k]
    cs.log('')
    cs.log('▶ 任务开始：自动刷视频')
    PAUSE.clear()
    CANCEL.clear()
    with LOCK:
        STATE['paused'] = False
        STATE['phase'] = 'brushing'
    try:
        cfg = cs.load_config()
        if _apply_rate(opts, cfg):
            # 只合并倍速这一个字段：整份 save_config(cfg) 会把任务启动时那份
            # 旧配置整体落盘，冲掉这期间用户在界面上保存的模型信息。
            cs.patch_config({'brush_rate': cfg['brush_rate']})
        rate = cs.g_rate(cfg)
        cs.log('播放方式：%sx 倍速 + 静音，真实播放到片尾（不伪造心跳）。' % rate)
        cs.log('只刷视频任务点；测验 / 作业不会替你自动完成。')

        def progress(msg, *a):
            cs.log(msg, a[0] if a else 'info')
            # 状态行跟着走：让用户随时看到「现在刷到哪门课哪个视频」
            with LOCK:
                STATE['task'] = '刷视频 · ' + (msg[:54] if msg else '')

        def control():
            if CANCEL.is_set():
                return 'stop'
            if PAUSE.is_set():
                return 'pause'
            return 'run'

        out = cs.brush_videos(cfg, only, progress=progress, control=control)
        all_done = not out.get('stopped')
        with LOCK:
            STATE['summary'] = {'刷完视频': out.get('done', 0),
                                '未完成': out.get('fail', 0)}
            if out.get('stopped'):
                STATE['summary']['状态'] = '已停止'
        cs.log('想核对结果：重新点「开始查询」，报告里消失的章节节点就是刷完的。', 'ok')
    except cs.NotLoggedIn as e:
        cs.log('%s' % e, 'err')
        with LOCK:
            STATE['summary'] = {'提示': '未登录，请先登录'}
    except Exception as e:
        cs.log('任务失败：%s' % e, 'err')
        import traceback
        cs.log(traceback.format_exc()[-900:], 'err')
        # 结果区给一版错误卡片：不然「查询中…」占位永远挂着（rv 没变
        # 前端不会重画），用户只看到无限转圈，失败原因却藏在日志页签
        _set_result({'error': str(e)[:200] or type(e).__name__})
    finally:
        cs.set_control_hook(None)
        PAUSE.clear()
        CANCEL.clear()
        with LOCK:
            STATE['running'] = False
            STATE['done'] = True
            STATE['paused'] = False
            STATE['phase'] = ''
        cs.log('■ 任务结束')
        if locals().get('all_done') and opts.get('shutdown'):
            _shutdown_pc()


def _answer_worker(opts):
    """「做勾选课程的章节作业并交卷」的后台线程。

    答完直接走平台完整交卷链（正式提交，记录成绩）；每份作业的结果
    （已交卷/未确认/需人工…）放进「交卷结果」面板，未确认的可以在
    面板里「重新核实」（平台批阅状态可能延迟几分钟才翻转，实测多次）。
    （v3.9.5 起另有 draft 暂存模式：答完只调平台「暂存保存」不交卷，
    由用户人工审查后再提交——正式交卷仍是默认。）
    """
    only = [str(k) for k in (opts.get('only') or []) if k]
    draft = bool(opts.get('draft'))
    cs.log('')
    cs.log('▶ 任务开始：做作业（%s）'
           % ('答完暂存不交卷' if draft else '答完直接交卷'))
    PAUSE.clear()
    CANCEL.clear()
    with LOCK:
        STATE['paused'] = False
        STATE['phase'] = 'answering'
    try:
        cfg = cs.load_config()
        if not cs.llm_ready(cfg):
            cs.log('还没有配置大模型：请在「设置 · 大模型」里选平台、填 API Key 并保存；'
                   '可以点「测试连接」先确认通。', 'err')
            with LOCK:
                STATE['summary'] = {'提示': '先配置大模型接口'}
            return
        cs.log('模型：%s ｜ 自动答单选/多选/判断/填空/简答；'
               '要上传附件（实验报告）的整份跳过并提示。' % cfg['llm_model'])

        def progress(msg, *a):
            cs.log(msg, a[0] if a else 'info')
            with LOCK:
                STATE['task'] = '答题 · ' + (msg[:54] if msg else '')

        def control():
            if CANCEL.is_set():
                return 'stop'
            if PAUSE.is_set():
                return 'pause'
            return 'run'

        out = cs.answer_courses(cfg, only, submit=not draft,
                                progress=progress, control=control)
        with LOCK:
            STATE['summary'] = {'交卷': out.get('submitted', 0),
                                '已暂存': out.get('saved', 0),
                                '未确认': out.get('unverified', 0),
                                '部分暂存': out.get('partial', 0),
                                '需人工': out.get('report', 0)
                                          + out.get('skipped', 0),
                                '失败': out.get('fail', 0)}
            if out.get('stopped'):
                STATE['summary']['状态'] = '已停止'
        if out.get('items'):
            _set_ares({'time': time.strftime('%H:%M'), 'items': out['items']})
    except cs.NotLoggedIn as e:
        cs.log('%s' % e, 'err')
        with LOCK:
            STATE['summary'] = {'提示': '未登录，请先登录'}
    except Exception as e:
        cs.log('任务失败：%s' % e, 'err')
        import traceback
        cs.log(traceback.format_exc()[-900:], 'err')
        # 结果区给一版错误卡片：不然「查询中…」占位永远挂着（rv 没变
        # 前端不会重画），用户只看到无限转圈，失败原因却藏在日志页签
        _set_result({'error': str(e)[:200] or type(e).__name__})
    finally:
        cs.set_control_hook(None)
        PAUSE.clear()
        CANCEL.clear()
        with LOCK:
            STATE['running'] = False
            STATE['done'] = True
            STATE['paused'] = False
            STATE['phase'] = ''
        cs.log('■ 任务结束')


def _answer_single_worker(opts):
    """「刷题」：对未完成作业清单里点中的那一份，直接答题并交卷。

    判别逻辑与「做作业并交卷（正式提交）」完全一致（共用 answer_urls →
    answer_one）：附件题整份跳过、题干读不出跳过、多空填空按空序切分、
    同题干复用上次答案、含不支持题型跳过。
    """
    draft = bool(opts.get('draft'))
    cs.log('')
    cs.log('▶ 任务开始：刷题（%s）'
           % ('单份作业，答完暂存不交卷' if draft
              else '单份作业，答完直接交卷'))
    PAUSE.clear()
    CANCEL.clear()
    with LOCK:
        STATE['paused'] = False
        STATE['phase'] = 'answering'
    try:
        cfg = cs.load_config()
        if not cs.llm_ready(cfg):
            cs.log('还没有配置大模型：请在「设置 · 大模型」里选平台、填 API Key 并保存；'
                   '可以点「测试连接」先确认通。', 'err')
            with LOCK:
                STATE['summary'] = {'提示': '先配置大模型接口'}
            return
        cs.log('模型：%s ｜ 只做这一份；要上传附件的题会整份跳过并提示。'
               % cfg['llm_model'])

        def progress(msg, *a):
            cs.log(msg, a[0] if a else 'info')
            with LOCK:
                STATE['task'] = '刷题 · ' + (msg[:54] if msg else '')

        def control():
            if CANCEL.is_set():
                return 'stop'
            if PAUSE.is_set():
                return 'pause'
            return 'run'

        out = cs.answer_urls(cfg, [{'url': opts.get('url'),
                                    'course': opts.get('course'),
                                    'title': opts.get('title')}],
                             submit=not draft, progress=progress, control=control)
        with LOCK:
            STATE['summary'] = {'交卷': out.get('submitted', 0),
                                '已暂存': out.get('saved', 0),
                                '未确认': out.get('unverified', 0),
                                '部分暂存': out.get('partial', 0),
                                '需人工': out.get('report', 0),
                                '此前已交': out.get('already', 0),
                                '这份没做': out.get('fail', 0)}
            if out.get('stopped'):
                STATE['summary']['状态'] = '已停止'
    except cs.NotLoggedIn as e:
        cs.log('%s' % e, 'err')
        with LOCK:
            STATE['summary'] = {'提示': '未登录，请先登录'}
    except Exception as e:
        cs.log('任务失败：%s' % e, 'err')
        import traceback
        cs.log(traceback.format_exc()[-900:], 'err')
        # 结果区给一版错误卡片：不然「查询中…」占位永远挂着（rv 没变
        # 前端不会重画），用户只看到无限转圈，失败原因却藏在日志页签
        _set_result({'error': str(e)[:200] or type(e).__name__})
    finally:
        cs.set_control_hook(None)
        PAUSE.clear()
        CANCEL.clear()
        with LOCK:
            STATE['running'] = False
            STATE['done'] = True
            STATE['paused'] = False
            STATE['phase'] = ''
        cs.log('■ 任务结束')


def _verify_worker(opts):
    """「重新核实」的后台线程：把未确认的作业逐份重开翻案。

    平台批阅状态可能延迟几分钟到十几分钟才翻转（实测三次交卷全部
    延迟生效），这里按轮次重开作业卡，渲染成「已批阅」页即翻案为
    已交卷；核实一轮不中就隔一段时间再核，最多 N 轮（可停）。
    结束后把每份的最新状态合并回交卷结果面板。
    """
    items = [it for it in (opts.get('items') or []) if isinstance(it, dict)]
    cs.log('')
    cs.log('▶ 任务开始：重新核实未确认的作业（共 %d 份）' % len(items))
    PAUSE.clear()
    CANCEL.clear()
    with LOCK:
        STATE['paused'] = False
        STATE['phase'] = 'answering'
    try:
        def progress(msg, *a):
            cs.log(msg, a[0] if a else 'info')
            with LOCK:
                STATE['task'] = '核实 · ' + (msg[:54] if msg else '')

        def control():
            if CANCEL.is_set():
                return 'stop'
            if PAUSE.is_set():
                return 'pause'
            return 'run'

        cfg = cs.load_config()
        out = cs.verify_items(cfg, items, progress=progress, control=control)
        st = {(r.get('key'), r.get('kid')): r.get('status')
              for r in out.get('results', [])}
        with LOCK:
            ares = STATE['ares']
            if ares:
                # 就地改状态也要在锁里：轮询线程可能正拿着同一份数据做
                # json 序列化，跟它抢着改是白送一个偶发崩溃。
                for it in ares.get('items', []):
                    s = st.get((it.get('key'), it.get('kid')))
                    if s:
                        it['status'] = s
        if ares:
            _set_ares(ares)
        with LOCK:
            STATE['summary'] = {'核实确认已交': out.get('confirmed', 0),
                                '仍未确认': out.get('still', 0)}
            if out.get('stopped'):
                STATE['summary']['状态'] = '已停止'
    except cs.NotLoggedIn as e:
        cs.log('%s' % e, 'err')
        with LOCK:
            STATE['summary'] = {'提示': '未登录，请先登录'}
    except Exception as e:
        cs.log('任务失败：%s' % e, 'err')
        import traceback
        cs.log(traceback.format_exc()[-900:], 'err')
        # 这里**不能**照别处那样画一张「任务失败」卡去顶结果区：此刻结果区
        # 挂着的是用户的「交卷结果」面板 —— 哪些交了、哪些没交、还有「重新
        # 核实」按钮。核实失败多半只是会话过期，把这份记录冲掉，用户既丢了
        # 清单又没法再点一次核实，比不提示还糟。所以：有面板就只提示（错误
        # 就在用户此刻正看的「运行日志」页里），没面板才画卡，免得停在
        # 「查询中…」的假象里。
        with LOCK:
            has_ares = bool(STATE.get('ares'))
            if not has_ares:
                STATE['summary'] = {'提示': '核实失败：'
                                  + (str(e)[:60] or type(e).__name__)}
        if not has_ares:
            _set_result({'error': str(e)[:200] or type(e).__name__})
    finally:
        cs.set_control_hook(None)
        PAUSE.clear()
        CANCEL.clear()
        with LOCK:
            STATE['running'] = False
            STATE['done'] = True
            STATE['paused'] = False
            STATE['phase'] = ''
        cs.log('■ 任务结束')


def _combo_worker(opts):
    """「刷课+刷题」的后台线程：先刷选中的视频，再对同一批范围做作业并交卷。

    视频自动刷；作业答完直接走平台完整交卷链（与单独的「做作业并交卷」
    一致），逐份结果进「交卷结果」面板。
    """
    only = [str(k) for k in (opts.get('only') or []) if k]
    cs.log('')
    cs.log('▶ 任务开始：刷课 + 刷题（先刷视频，再做作业并交卷）')
    PAUSE.clear()
    CANCEL.clear()
    with LOCK:
        STATE['paused'] = False
        STATE['phase'] = 'brushing'
    try:
        cfg = cs.load_config()
        if not cs.llm_ready(cfg):
            cs.log('还没有配置大模型：刷视频不受影响，但刷题需要先在左侧'
                   '「模型」里填好平台与 API Key。', 'err')

        def progress(msg, *a):
            cs.log(msg, a[0] if a else 'info')
            with LOCK:
                STATE['task'] = (msg[:54] if msg else '')

        def control():
            if CANCEL.is_set():
                return 'stop'
            if PAUSE.is_set():
                return 'pause'
            return 'run'

        # ---- 第一段：刷视频 ----
        if _apply_rate(opts, cfg):
            cs.patch_config({'brush_rate': cfg['brush_rate']})   # 同上：只落这一个字段
        rate = cs.g_rate(cfg)
        cs.log('【第 1 步】刷视频：%sx 倍速 + 静音，真实播放到片尾。' % rate)
        out = cs.brush_videos(cfg, only, progress=progress, control=control)
        if out.get('stopped'):
            with LOCK:
                STATE['summary'] = {'刷完视频': out.get('done', 0),
                                    '未完成': out.get('fail', 0),
                                    '状态': '已停止（未进入刷题）'}
            cs.log('刷视频被停止，按约定不进入刷题。', 'warn')
            return
        # ---- 第二段：做作业并交卷 ----
        cs.log('')
        cs.log('【第 2 步】做作业并交卷：答完直接提交。')
        with LOCK:
            STATE['phase'] = 'answering'
        out2 = cs.answer_courses(cfg, only, submit=True,
                                 progress=progress, control=control)
        all_done = not out2.get('stopped')
        with LOCK:
            STATE['summary'] = {'刷完视频': out.get('done', 0),
                                '交卷': out2.get('submitted', 0),
                                '未确认': out2.get('unverified', 0),
                                '部分暂存': out2.get('partial', 0),
                                '需人工': out2.get('report', 0)
                                          + out2.get('skipped', 0),
                                '失败': out2.get('fail', 0)}
            if out2.get('stopped'):
                STATE['summary']['状态'] = '已停止'
        if out2.get('items'):
            _set_ares({'time': time.strftime('%H:%M'), 'items': out2['items']})
        cs.log('刷课+刷题完成：视频完成 %d 个；交卷 %d 份、未确认 %d 份、'
               '需人工 %d 份，明细在「交卷结果」面板。'
               % (out.get('done', 0), out2.get('submitted', 0),
                  out2.get('unverified', 0),
                  out2.get('report', 0) + out2.get('skipped', 0)), 'ok')
    except cs.NotLoggedIn as e:
        cs.log('%s' % e, 'err')
        with LOCK:
            STATE['summary'] = {'提示': '未登录，请先登录'}
    except Exception as e:
        cs.log('任务失败：%s' % e, 'err')
        import traceback
        cs.log(traceback.format_exc()[-900:], 'err')
        # 结果区给一版错误卡片：不然「查询中…」占位永远挂着（rv 没变
        # 前端不会重画），用户只看到无限转圈，失败原因却藏在日志页签
        _set_result({'error': str(e)[:200] or type(e).__name__})
    finally:
        cs.set_control_hook(None)
        PAUSE.clear()
        CANCEL.clear()
        with LOCK:
            STATE['running'] = False
            STATE['done'] = True
            STATE['paused'] = False
            STATE['phase'] = ''
        cs.log('■ 任务结束')
        if locals().get('all_done') and opts.get('shutdown'):
            _shutdown_pc()


# ================================================================ 上传链接刷课刷题
# 用户把「页面链接」粘进来直接刷，不依赖先做一遍全量查询。两种链接：
#   · 作业链接（mooc2/work/task…）→ 刷题（复用 answer_urls 的完整判别 / 交卷链）
#   · 课程链接（courseid=..&clazzid=..）→ 刷课：回课程页找待完成章节再刷视频
#     （课程页链接没有 kid，刷课本来就要逐节点下钻；用户给哪个链接都行）
_HW_HOSTS = ('chaoxing.com', 'edu.cn')


def _parse_links(text):
    """把用户粘贴的多行文本拆成 [{'url','course','title'}]。

    支持：纯 URL 行；或「课程名 | 标题 | URL」「标题 | URL」这类带标签的单行。
    去重、按出现顺序保留。
    """
    import re as _re
    out, seen = [], set()
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        # 行里可能有多段被空白 / 竖线 / Tab 分隔；取第一个像 URL 的
        parts = [p.strip() for p in _re.split(r'[|\t]', line) if p.strip()]
        url = next((p for p in parts if p.lower().startswith(('http://', 'https://'))), '')
        if not url:
            # 兜底：整行里正则抠一个 http(s) 链接
            m = _re.search(r'https?://\S+', line)
            url = m.group(0) if m else ''
        if not url:
            continue
        url = url.rstrip('),.;')
        if url in seen:
            continue
        seen.add(url)
        labels = [p for p in parts if p != url]
        course = labels[0] if len(labels) >= 2 else ''
        title = labels[1] if len(labels) >= 2 else (labels[0] if labels else '')
        out.append({'url': url, 'course': course, 'title': title})
    return out


def _link_query(url):
    """解析链接的 query 参数（大小写不敏感）。"""
    try:
        return {k.lower(): (v[0] if v else '')
                for k, v in parse_qs(urlsplit(url).query).items()}
    except Exception:
        return {}


def _link_host_ok(url):
    host = (urlparse(url).netloc or '').lower().split(':')[0]
    return (host == 'chaoxing.com' or host.endswith('.chaoxing.com')
            or host.endswith('.edu.cn'))


def _classify_link(url):
    """返回 'exam' | 'work'(new) | 'dowork'(old) | 'cards'(chapter) | 'course' | 'other'。"""
    u = (url or '').lower()
    if '/exam-ans/' in u or '/exam/test' in u or '/exam/exam' in u:
        return 'exam'
    if 'mooc2/work/task' in u or '/work/task' in u:
        return 'work'
    if 'dohomeworknew' in u or '/work/do' in u:
        return 'dowork'
    if 'knowledge/cards' in u or 'knowledgeid=' in u:
        return 'cards'
    if 'stucoursemiddle' in u or 'mycourse/stu' in u:
        return 'course'
    if 'courseid=' in u and 'clazzid=' in u:
        return 'course'
    return 'other'


def _link_course(url):
    """从课程 / 作业链接里抠出 cid/clsid，用于回课程页找待完成章节。"""
    q = _link_query(url)
    cid = q.get('courseid') or ''
    clsid = q.get('clazzid') or q.get('classid') or ''
    return cid, clsid


def _brush_link_worker(opts):
    """「上传链接 → 刷课」：每条课程链接回课程页找待完成章节刷视频。"""
    links = opts.get('links') or []
    cs.log('')
    cs.log('▶ 任务开始：上传链接刷课（共 %d 条链接）' % len(links))
    PAUSE.clear()
    CANCEL.clear()
    with LOCK:
        STATE['paused'] = False
        STATE['phase'] = 'brushing'
    try:
        cfg = cs.load_config()
        if _apply_rate(opts, cfg):
            cs.patch_config({'brush_rate': cfg['brush_rate']})
        rate = cs.g_rate(cfg)
        cs.log('播放方式：%sx 倍速 + 静音，真实播放到片尾。' % rate)

        def progress(msg, *a):
            cs.log(msg, a[0] if a else 'info')
            with LOCK:
                STATE['task'] = '刷课 · ' + (msg[:54] if msg else '')

        def control():
            if CANCEL.is_set():
                return 'stop'
            if PAUSE.is_set():
                return 'pause'
            return 'run'

        out = cs.brush_by_urls(cfg, [l['url'] for l in links],
                               progress=progress, control=control)
        with LOCK:
            STATE['summary'] = {'刷完视频': out.get('done', 0),
                                '未完成': out.get('fail', 0)}
            if out.get('stopped'):
                STATE['summary']['状态'] = '已停止'
        all_done = not out.get('stopped')
        cs.log('上传链接刷课完成：视频完成 %d 个，未完成 %d 个。'
               % (out.get('done', 0), out.get('fail', 0)), 'ok')
    except cs.NotLoggedIn as e:
        cs.log('%s' % e, 'err')
        with LOCK:
            STATE['summary'] = {'提示': '未登录，请先登录'}
    except Exception as e:
        cs.log('任务失败：%s' % e, 'err')
        import traceback
        cs.log(traceback.format_exc()[-900:], 'err')
        _set_result({'error': str(e)[:200] or type(e).__name__})
    finally:
        cs.set_control_hook(None)
        PAUSE.clear()
        CANCEL.clear()
        with LOCK:
            STATE['running'] = False
            STATE['done'] = True
            STATE['paused'] = False
            STATE['phase'] = ''
        cs.log('■ 任务结束')
        if locals().get('all_done') and opts.get('shutdown'):
            _shutdown_pc()


def _answer_links_worker(opts):
    """「上传链接 → 刷题」：每条作业链接走 answer_urls 的完整判别 / 交卷链。"""
    links = opts.get('links') or []
    draft = bool(opts.get('draft'))
    cs.log('')
    cs.log('▶ 任务开始：上传链接刷题（共 %d 条，%s）'
           % (len(links), '答完暂存不交卷' if draft else '答完直接交卷'))
    PAUSE.clear()
    CANCEL.clear()
    with LOCK:
        STATE['paused'] = False
        STATE['phase'] = 'answering'
    try:
        cfg = cs.load_config()
        if not cs.llm_ready(cfg):
            cs.log('还没有配置大模型：请在「设置 · 大模型」里选平台、填 API Key 并保存。', 'err')
            with LOCK:
                STATE['summary'] = {'提示': '先配置大模型接口'}
            return
        cs.log('模型：%s ｜ 要上传附件的题会整份跳过并提示。' % cfg['llm_model'])

        def progress(msg, *a):
            cs.log(msg, a[0] if a else 'info')
            with LOCK:
                STATE['task'] = '刷题 · ' + (msg[:54] if msg else '')

        def control():
            if CANCEL.is_set():
                return 'stop'
            if PAUSE.is_set():
                return 'pause'
            return 'run'

        out = cs.answer_urls(cfg, links, submit=not draft,
                             progress=progress, control=control)
        with LOCK:
            STATE['summary'] = {'交卷': out.get('submitted', 0),
                                '已暂存': out.get('saved', 0),
                                '未确认': out.get('unverified', 0),
                                '部分暂存': out.get('partial', 0),
                                '需人工': out.get('report', 0) + out.get('skipped', 0),
                                '失败': out.get('fail', 0)}
            if out.get('stopped'):
                STATE['summary']['状态'] = '已停止'
        if out.get('items'):
            _set_ares({'time': time.strftime('%H:%M'), 'items': out['items']})
    except cs.NotLoggedIn as e:
        cs.log('%s' % e, 'err')
        with LOCK:
            STATE['summary'] = {'提示': '未登录，请先登录'}
    except Exception as e:
        cs.log('任务失败：%s' % e, 'err')
        import traceback
        cs.log(traceback.format_exc()[-900:], 'err')
        _set_result({'error': str(e)[:200] or type(e).__name__})
    finally:
        cs.set_control_hook(None)
        PAUSE.clear()
        CANCEL.clear()
        with LOCK:
            STATE['running'] = False
            STATE['done'] = True
            STATE['paused'] = False
            STATE['phase'] = ''
        cs.log('■ 任务结束')


def _link_combo_worker(opts):
    """「上传链接 → 刷课+刷题」：先刷课程链接的视频，再对作业链接交卷。"""
    links = opts.get('links') or []
    cs.log('')
    cs.log('▶ 任务开始：上传链接刷课+刷题（共 %d 条链接）' % len(links))
    PAUSE.clear()
    CANCEL.clear()
    with LOCK:
        STATE['paused'] = False
        STATE['phase'] = 'brushing'
    # 按类型分流：课程链接去刷课，作业链接去刷题（两条都跑完才算完）
    course_links = [l for l in links if _classify_link(l['url'])
                    in ('course', 'cards', 'other')]
    work_links = [l for l in links if _classify_link(l['url'])
                  in ('work', 'dowork', 'exam')]
    try:
        cfg = cs.load_config()
        if _apply_rate(opts, cfg):
            cs.patch_config({'brush_rate': cfg['brush_rate']})

        def progress(msg, *a):
            cs.log(msg, a[0] if a else 'info')
            with LOCK:
                STATE['task'] = (msg[:54] if msg else '')

        def control():
            if CANCEL.is_set():
                return 'stop'
            if PAUSE.is_set():
                return 'pause'
            return 'run'

        vdone = vfail = 0
        if course_links:
            cs.log('【第 1 步】刷课：%d 条课程链接。' % len(course_links))
            out = cs.brush_by_urls(cfg, [l['url'] for l in course_links],
                                   progress=progress, control=control)
            vdone, vfail = out.get('done', 0), out.get('fail', 0)
            if out.get('stopped'):
                with LOCK:
                    STATE['summary'] = {'刷完视频': vdone, '未完成': vfail,
                                        '状态': '已停止（未进入刷题）'}
                cs.log('刷课被停止，按约定不进入刷题。', 'warn')
                return
        else:
            cs.log('（没有课程链接，跳过刷课）')

        if work_links:
            cs.log('')
            cs.log('【第 2 步】刷题：%d 条作业链接，答完直接交卷。' % len(work_links))
            with LOCK:
                STATE['phase'] = 'answering'
            if not cs.llm_ready(cfg):
                cs.log('没有配置大模型，刷题跳过（刷课不受影响）。', 'err')
                out2 = {'submitted': 0, 'unverified': 0, 'partial': 0,
                        'report': 0, 'skipped': 0, 'fail': 0, 'items': []}
            else:
                out2 = cs.answer_urls(cfg, work_links, submit=True,
                                      progress=progress, control=control)
                if out2.get('items'):
                    _set_ares({'time': time.strftime('%H:%M'), 'items': out2['items']})
            with LOCK:
                STATE['summary'] = {'刷完视频': vdone, '交卷': out2.get('submitted', 0),
                                    '未确认': out2.get('unverified', 0),
                                    '部分暂存': out2.get('partial', 0),
                                    '需人工': out2.get('report', 0) + out2.get('skipped', 0),
                                    '失败': (vfail + out2.get('fail', 0))}
        else:
            cs.log('（没有作业链接，跳过刷题）')
            with LOCK:
                STATE['summary'] = {'刷完视频': vdone, '未完成': vfail}
        all_done = True
        cs.log('上传链接刷课+刷题完成。', 'ok')
    except cs.NotLoggedIn as e:
        cs.log('%s' % e, 'err')
        with LOCK:
            STATE['summary'] = {'提示': '未登录，请先登录'}
    except Exception as e:
        cs.log('任务失败：%s' % e, 'err')
        import traceback
        cs.log(traceback.format_exc()[-900:], 'err')
        _set_result({'error': str(e)[:200] or type(e).__name__})
    finally:
        cs.set_control_hook(None)
        PAUSE.clear()
        CANCEL.clear()
        with LOCK:
            STATE['running'] = False
            STATE['done'] = True
            STATE['paused'] = False
            STATE['phase'] = ''
        cs.log('■ 任务结束')
        if locals().get('all_done') and opts.get('shutdown'):
            _shutdown_pc()


# ================================================================ 课堂活动：刷课 / 刷题 / 刷课+刷题
# 「进行中的课堂活动」那一节的动作列。刷课 = 刷该活动所在课程的待完成章节视频
# （复用 cs.brush_activities → brush_by_urls）；刷题 = 大模型作答这条活动
# （复用 cs.answer_activities，提交 / 暂存两模式）。三者与任务点那节的
# 刷视频 / 做作业共用同一套 PAUSE/CANCEL 控制、summary、交卷结果面板。
def _brush_act_worker(opts):
    """「课堂活动 → 刷课」：把活动所在课程的待完成视频刷掉。"""
    targets = opts.get('targets') or []
    cs.log('')
    cs.log('▶ 任务开始：课堂活动刷课（涉及 %d 条活动）' % len(targets))
    PAUSE.clear()
    CANCEL.clear()
    with LOCK:
        STATE['paused'] = False
        STATE['phase'] = 'brushing'
    try:
        cfg = cs.load_config()
        if _apply_rate(opts, cfg):
            cs.patch_config({'brush_rate': cfg['brush_rate']})
        cs.log('播放方式：%sx 倍速 + 静音，真实播放到片尾。' % cs.g_rate(cfg))

        def progress(msg, *a):
            cs.log(msg, a[0] if a else 'info')
            with LOCK:
                STATE['task'] = '刷课 · ' + (msg[:54] if msg else '')

        def control():
            if CANCEL.is_set():
                return 'stop'
            if PAUSE.is_set():
                return 'pause'
            return 'run'

        out = cs.brush_activities(cfg, targets, progress=progress, control=control)
        with LOCK:
            STATE['summary'] = {'刷完视频': out.get('done', 0),
                                '未完成': out.get('fail', 0)}
            if out.get('stopped'):
                STATE['summary']['状态'] = '已停止'
        all_done = not out.get('stopped')
        cs.log('课堂活动刷课完成：视频完成 %d 个，未完成 %d 个。'
               % (out.get('done', 0), out.get('fail', 0)), 'ok')
    except cs.NotLoggedIn as e:
        cs.log('%s' % e, 'err')
        with LOCK:
            STATE['summary'] = {'提示': '未登录，请先登录'}
    except Exception as e:
        cs.log('任务失败：%s' % e, 'err')
        import traceback
        cs.log(traceback.format_exc()[-900:], 'err')
        _set_result({'error': str(e)[:200] or type(e).__name__})
    finally:
        cs.set_control_hook(None)
        PAUSE.clear()
        CANCEL.clear()
        with LOCK:
            STATE['running'] = False
            STATE['done'] = True
            STATE['paused'] = False
            STATE['phase'] = ''
        cs.log('■ 任务结束')
        if locals().get('all_done') and opts.get('shutdown'):
            _shutdown_pc()


def _answer_act_worker(opts):
    """「课堂活动 → 作答」：逐条大模型作答（提交 / 暂存）。"""
    targets = opts.get('targets') or []
    draft = bool(opts.get('draft'))
    cs.log('')
    cs.log('▶ 任务开始：课堂活动作答（共 %d 条，%s）'
           % (len(targets), '答完暂存不交卷' if draft else '答完直接提交'))
    PAUSE.clear()
    CANCEL.clear()
    with LOCK:
        STATE['paused'] = False
        STATE['phase'] = 'answering'
    try:
        cfg = cs.load_config()
        if not cs.llm_ready(cfg):
            cs.log('还没有配置大模型：请在「设置 · 大模型」里选平台、填 API Key 并保存。', 'err')
            with LOCK:
                STATE['summary'] = {'提示': '先配置大模型接口'}
            return
        cs.log('模型：%s ｜ 要上传附件的题会整条跳过并提示；'
               '题干带公式图片的题走视觉模型（若已配置）。' % cfg['llm_model'])

        def progress(msg, *a):
            cs.log(msg, a[0] if a else 'info')
            with LOCK:
                STATE['task'] = '作答 · ' + (msg[:54] if msg else '')

        def control():
            if CANCEL.is_set():
                return 'stop'
            if PAUSE.is_set():
                return 'pause'
            return 'run'

        out = cs.answer_activities(cfg, targets, submit=not draft,
                                   progress=progress, control=control)
        with LOCK:
            STATE['summary'] = {'提交': out.get('submitted', 0),
                                '已暂存': out.get('saved', 0),
                                '未确认': out.get('unverified', 0),
                                '部分暂存': out.get('partial', 0),
                                '需人工': out.get('report', 0),
                                '失败': out.get('fail', 0)}
            if out.get('stopped'):
                STATE['summary']['状态'] = '已停止'
        if out.get('items'):
            _set_ares({'time': time.strftime('%H:%M'), 'items': out['items']})
    except cs.NotLoggedIn as e:
        cs.log('%s' % e, 'err')
        with LOCK:
            STATE['summary'] = {'提示': '未登录，请先登录'}
    except Exception as e:
        cs.log('任务失败：%s' % e, 'err')
        import traceback
        cs.log(traceback.format_exc()[-900:], 'err')
        _set_result({'error': str(e)[:200] or type(e).__name__})
    finally:
        cs.set_control_hook(None)
        PAUSE.clear()
        CANCEL.clear()
        with LOCK:
            STATE['running'] = False
            STATE['done'] = True
            STATE['paused'] = False
            STATE['phase'] = ''
        cs.log('■ 任务结束')


def _combo_act_worker(opts):
    """「课堂活动 → 刷课+刷题」：先刷活动所在课程的视频，再逐条作答。"""
    targets = opts.get('targets') or []
    draft = bool(opts.get('draft'))
    cs.log('')
    cs.log('▶ 任务开始：课堂活动刷课+刷题（共 %d 条活动）' % len(targets))
    PAUSE.clear()
    CANCEL.clear()
    with LOCK:
        STATE['paused'] = False
        STATE['phase'] = 'brushing'
    try:
        cfg = cs.load_config()
        if _apply_rate(opts, cfg):
            cs.patch_config({'brush_rate': cfg['brush_rate']})

        def progress(msg, *a):
            cs.log(msg, a[0] if a else 'info')
            with LOCK:
                STATE['task'] = (msg[:54] if msg else '')

        def control():
            if CANCEL.is_set():
                return 'stop'
            if PAUSE.is_set():
                return 'pause'
            return 'run'

        vdone = vfail = 0
        cs.log('【第 1 步】刷课：%d 条活动所在课程的视频。' % len(targets))
        out = cs.brush_activities(cfg, targets, progress=progress, control=control)
        vdone, vfail = out.get('done', 0), out.get('fail', 0)
        if out.get('stopped'):
            with LOCK:
                STATE['summary'] = {'刷完视频': vdone, '未完成': vfail,
                                    '状态': '已停止（未进入作答）'}
            cs.log('刷课被停止，按约定不进入作答。', 'warn')
            return

        cs.log('')
        cs.log('【第 2 步】作答：%d 条活动，%s。'
               % (len(targets), '答完暂存' if draft else '答完直接提交'))
        with LOCK:
            STATE['phase'] = 'answering'
        if not cs.llm_ready(cfg):
            cs.log('没有配置大模型，作答跳过（刷课不受影响）。', 'err')
            out2 = {'submitted': 0, 'saved': 0, 'unverified': 0, 'report': 0,
                    'fail': 0, 'items': []}
        else:
            out2 = cs.answer_activities(cfg, targets, submit=not draft,
                                        progress=progress, control=control)
            if out2.get('items'):
                _set_ares({'time': time.strftime('%H:%M'), 'items': out2['items']})
        with LOCK:
            STATE['summary'] = {'刷完视频': vdone,
                                '提交': out2.get('submitted', 0),
                                '已暂存': out2.get('saved', 0),
                                '未确认': out2.get('unverified', 0),
                                '部分暂存': out2.get('partial', 0),
                                '需人工': out2.get('report', 0),
                                '失败': (vfail + out2.get('fail', 0))}
        # 跟别的 combo 一样：作答被停止就不算「全跑完」，不触发自动关机。
        all_done = not out2.get('stopped')
        cs.log('课堂活动刷课+刷题完成。', 'ok')
    except cs.NotLoggedIn as e:
        cs.log('%s' % e, 'err')
        with LOCK:
            STATE['summary'] = {'提示': '未登录，请先登录'}
    except Exception as e:
        cs.log('任务失败：%s' % e, 'err')
        import traceback
        cs.log(traceback.format_exc()[-900:], 'err')
        _set_result({'error': str(e)[:200] or type(e).__name__})
    finally:
        cs.set_control_hook(None)
        PAUSE.clear()
        CANCEL.clear()
        with LOCK:
            STATE['running'] = False
            STATE['done'] = True
            STATE['paused'] = False
            STATE['phase'] = ''
        cs.log('■ 任务结束')
        if locals().get('all_done') and opts.get('shutdown'):
            _shutdown_pc()


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>学习通巡检工具</title>
<style>
  /* ==================================================================
     设计基线（Operate 模式 / 深色为主 / 高密度台账）
     · 单一字族 + 固定 rem 尺度，不用流体字号
     · 色彩有分工：青绿=主操作与当前选中，琥珀=需要注意，红=失败，绿=完成
     · 侧栏是第二中性层，主画布是最暗的地面
     · 浏览器原生表面（选中色/光标/滚动条/焦点环/数字）全部按盘色主题化
     · 图标一律自绘 SVG，同一笔画粗细；不用任何 emoji / Unicode 字形
     ================================================================== */
  *,*::before,*::after{box-sizing:border-box;}

  :root{
    /* 尺度与动效（两套主题共用） */
    --fs-xs:.6875rem;
    --fs-sm:.75rem;
    --fs-base:.8125rem;
    --fs-md:.875rem;
    --fs-lg:1rem;
    --fs-xl:1.25rem;
    --ease:cubic-bezier(.22,1,.36,1);
    --t:160ms;
    --rail-w:96px;
    /* 主色上的前景色（深色=近黑，浅色=白，见下方主题盘） */
    --ac-on:#0a1220;
  }
  /* ===== 深色盘（默认；云析 Material3 蓝调，兜底 primary #AAC7FF）=====
     原生控件（勾选框、下拉弹层、数字步进器）也跟主题走：
     不声明 color-scheme 的话 Chrome 会把它们画成亮白底 / 深黑底，和页面冲突。 */
  :root,:root[data-theme="dark"]{
    color-scheme:dark;
    --bg:#111318; --rail:#191c20; --raise:#1d2024; --sunk:#0c0e13;
    --line:#2a2d33; --line-2:#44474e;
    --tx:#e2e2e9; --tx-2:#c4c6d0; --mut:#8e9099;
    --ac:#aac7ff; --ac-hi:#cdddff; --ac-soft:rgba(170,199,255,.16);
    --warn:#e6b566; --err:#ffb4ab; --ok:#8fd6a8;
    --ac-on:#0a1220;
    --hover:rgba(255,255,255,.06); --scrl:#4a4f57;
  }
  /* ===== 浅色盘（云析 Material3 蓝调，primary #415F91）===== */
  :root[data-theme="light"]{
    color-scheme:light;
    --bg:#f9f9ff; --rail:#ededf4; --raise:#ffffff; --sunk:#f3f3fa;
    --line:#e0e2ec; --line-2:#c4c6d0;
    --tx:#191c20; --tx-2:#44474e; --mut:#63666e;
    --ac:#415f91; --ac-hi:#284777; --ac-soft:rgba(65,95,145,.12);
    --warn:#8a5a00; --err:#ba1a1a; --ok:#2f6b45;
    --ac-on:#ffffff;
    --hover:rgba(0,0,0,.05); --scrl:#a8adb8;
  }

  html{font-size:100%;}            /* 固定 rem 尺度：不随视口缩放 */
  html,body{height:100%;}
  body{
    margin:0;background:var(--bg);color:var(--tx);
    font-family:"Microsoft YaHei UI","Microsoft YaHei","PingFang SC",
                "Hiragino Sans GB","Segoe UI",system-ui,-apple-system,sans-serif;
    font-size:var(--fs-md);line-height:1.55;
    display:flex;flex-direction:column;
    overflow:hidden;
    -webkit-font-smoothing:antialiased;
  }

  /* ---------- 浏览器原生表面：最便宜、也最容易被忽略的「被建造」信号 ---------- */
  ::selection{background:var(--ac-soft);color:var(--tx);}
  input,select,textarea{caret-color:var(--ac);}
  :focus{outline:none;}
  :focus-visible{outline:2px solid var(--ac);outline-offset:2px;border-radius:2px;}
  *{scrollbar-width:thin;scrollbar-color:var(--scrl) transparent;}
  ::-webkit-scrollbar{width:11px;height:11px;}
  ::-webkit-scrollbar-track{background:transparent;}
  ::-webkit-scrollbar-thumb{
    background:var(--scrl);border:3px solid transparent;background-clip:content-box;
    border-radius:999px;}
  ::-webkit-scrollbar-thumb:hover{background:var(--line-2);background-clip:content-box;}
  ::-webkit-scrollbar-corner{background:transparent;}
  a{text-underline-offset:2px;}

  /* ================================ 顶栏 ================================ */
  header{
    flex:none;display:flex;align-items:center;gap:14px;
    padding:0 20px;height:52px;
    background:var(--rail);border-bottom:1px solid var(--line);
  }
  .brand{display:flex;align-items:baseline;gap:10px;min-width:0;}
  .brand h1{margin:0;font-size:var(--fs-lg);font-weight:600;letter-spacing:.01em;}
  .ver{
    font-size:var(--fs-xs);color:var(--ac);font-weight:600;
    padding:1px 6px;border:1px solid var(--ac-soft);
    border-radius:3px;font-variant-numeric:tabular-nums;
  }
  .hspace{flex:1;}
  .hstat{display:flex;align-items:center;gap:8px;min-width:0;}
  .pill{
    display:inline-flex;align-items:center;gap:7px;white-space:nowrap;
    font-size:var(--fs-base);color:var(--tx-2);
    background:var(--raise);border:1px solid var(--line);
    padding:4px 11px;border-radius:999px;
  }
  .dot{
    width:7px;height:7px;flex:none;border-radius:50%;
    background:var(--mut);transition:background var(--t) var(--ease);
  }
  .dot.on{
    background:var(--ac);
    box-shadow:0 0 0 3px var(--ac-soft);
    animation:pulse 1.5s var(--ease) infinite;
  }
  @keyframes pulse{50%{opacity:.4;}}
  /* 「本机会话是谁的」——换号时唯一能一眼看出「工具现在用哪个账号」的地方，
     常驻顶栏，不折行不截断 */
  .who{
    display:inline-flex;align-items:center;gap:6px;
    font-size:var(--fs-base);color:var(--mut);
    padding-left:12px;margin-left:2px;border-left:1px solid var(--line);
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  }
  .who .ic{color:var(--mut);}

  /* ================================ 骨架 ================================ */
  .wrap{flex:1;display:flex;min-height:0;}
  /* 查询页左列：查询操作（开始查询 + 查询范围 + 本次统计） */
  #queryOps{
    flex:0 0 300px;width:300px;
    background:var(--rail);border-right:1px solid var(--line);
    overflow-y:auto;overflow-x:hidden;
    padding:14px 14px 32px;
  }
  .main{flex:1;display:flex;flex-direction:column;min-width:0;background:var(--bg);}

  /* ============================ 侧栏分组 ============================ */
  .blk{margin:0 0 20px;}
  .blk:last-child{margin-bottom:0;}
  /* 组标题：上方留白大于下方（spacing：紧邻的下属贴住，组与组之间松开） */
  .blk-t{
    margin:0 0 9px;font-size:var(--fs-xs);font-weight:600;
    color:var(--mut);letter-spacing:.04em;
  }
  .note{font-size:var(--fs-sm);color:var(--mut);line-height:1.65;margin:8px 0 0;}
  .note b{color:var(--tx-2);font-weight:600;}
  .note code{
    font-family:"Cascadia Mono",Consolas,ui-monospace,monospace;
    font-size:.95em;color:var(--tx-2);background:var(--sunk);
    padding:1px 4px;border-radius:3px;
  }

  /* ================================ 控件 ================================ */
  .ic{display:inline-flex;align-items:center;justify-content:center;
      width:16px;height:16px;flex:none;}
  .ic svg{width:100%;height:100%;display:block;}

  .btn{
    display:flex;align-items:center;width:100%;gap:9px;
    padding:9px 12px;margin-bottom:7px;
    background:var(--raise);color:var(--tx);
    border:1px solid var(--line);border-radius:6px;
    font:inherit;font-size:var(--fs-base);text-align:left;
    cursor:pointer;white-space:nowrap;
    transition:background var(--t) var(--ease),border-color var(--t) var(--ease),
               color var(--t) var(--ease);
  }
  .btn .ic{color:var(--tx-2);transition:color var(--t) var(--ease);}
  .btn:hover:not(:disabled){background:var(--hover);border-color:var(--line-2);}
  .btn:hover:not(:disabled) .ic{color:var(--tx);}
  .btn:active:not(:disabled){transform:translateY(.5px);}
  .btn:disabled{opacity:.38;cursor:not-allowed;}
  .btn.pri{
    background:var(--ac);border-color:var(--ac);color:var(--ac-on);font-weight:600;
  }
  .btn.pri .ic{color:var(--ac-on);}
  .btn.pri:hover:not(:disabled){background:var(--ac-hi);border-color:var(--ac-hi);}
  .btn.dgr{color:var(--err);border-color:color-mix(in srgb,var(--err) 34%,transparent);}
  .btn.dgr .ic{color:var(--err);}
  .btn.dgr:hover:not(:disabled){background:color-mix(in srgb,var(--err) 10%,transparent);
    border-color:color-mix(in srgb,var(--err) 55%,transparent);}
  .btn.sm{padding:6px 10px;font-size:var(--fs-sm);gap:6px;}
  .btn.sm .ic{width:14px;height:14px;}

  /* 并排的按钮必须放弃 width:100%，否则三个一起被挤成竖排的窄条
     （中文会在任意字符处折行，看起来就像按钮坏了） */
  .row{display:flex;gap:7px;align-items:center;}
  .row > *{margin-bottom:0;}
  .row > .btn{width:auto;flex:0 0 auto;}
  .row > .btn.grow{flex:1 1 auto;min-width:0;}
  .grow{flex:1;min-width:0;}

  input[type=text],input[type=password],input[type=number],select,textarea{
    width:100%;padding:8px 10px;
    background:var(--sunk);color:var(--tx);
    border:1px solid var(--line);border-radius:6px;
    font:inherit;font-size:var(--fs-base);
    transition:border-color var(--t) var(--ease),background var(--t) var(--ease);
  }
  textarea{resize:vertical;min-height:64px;line-height:1.45;
    font-family:var(--mono,ui-monospace,SFMono-Regular,Menlo,Consolas,monospace);
    font-size:var(--fs-sm,0.86rem);}
  input::placeholder,textarea::placeholder{color:var(--mut);}
  input:hover,select:hover,textarea:hover{border-color:var(--line-2);}
  input:focus,select:focus,textarea:focus{border-color:var(--ac);background:var(--sunk);}
  select{
    cursor:pointer;appearance:none;-webkit-appearance:none;
    padding-right:28px;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' fill='none' stroke='%237e9599' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M4 6.5 8 10.5l4-4'/%3E%3C/svg%3E");
    background-repeat:no-repeat;background-position:right 8px center;
    background-size:14px 14px;
  }
  .field{margin-bottom:7px;position:relative;}
  .field .ic-in{
    position:absolute;right:6px;top:50%;transform:translateY(-50%);
    width:26px;height:26px;display:flex;align-items:center;justify-content:center;
    background:none;border:0;border-radius:5px;cursor:pointer;
    color:var(--mut);transition:color var(--t) var(--ease),background var(--t) var(--ease);
  }
  .field .ic-in:hover{color:var(--tx);background:var(--hover);}
  .field .ic-in[aria-pressed=true]{color:var(--ac);}
  .field.has-in input{padding-right:36px;}

  .chk{
    display:flex;align-items:flex-start;gap:8px;
    font-size:var(--fs-base);color:var(--tx-2);
    margin:9px 0 0;cursor:pointer;line-height:1.5;
  }
  .chk input{
    flex:none;width:15px;height:15px;margin:2px 0 0;
    accent-color:var(--ac);cursor:pointer;
  }
  .chk.inline{margin:0;white-space:nowrap;align-items:center;}
  .chk.inline input{margin:0;}

  /* ========================= 模型接入卡片 ========================= */
  .modelcard{
    background:var(--raise);border:1px solid var(--line);border-radius:8px;
    padding:11px 11px 12px;
  }
  .modelcard .field:last-of-type{margin-bottom:9px;}
  .mstat{
    display:flex;align-items:center;gap:7px;
    font-size:var(--fs-sm);line-height:1.5;
    padding:6px 8px;margin:0 0 9px;border-radius:5px;
    background:var(--sunk);border:1px solid var(--line);color:var(--mut);
  }
  .mstat.ok{color:var(--ok);border-color:color-mix(in srgb,var(--ok) 30%,transparent);}
  .mstat.warn{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 30%,transparent);}
  .mstat.err{color:var(--err);border-color:color-mix(in srgb,var(--err) 30%,transparent);}
  .mstat .ic{width:14px;height:14px;flex:none;}
  .mstat span{min-width:0;overflow-wrap:anywhere;}

  /* ============================== 课程选择 ============================== */
  .pickhead{
    display:flex;align-items:center;gap:6px;margin:9px 0 0;
    font-size:var(--fs-sm);color:var(--mut);
  }
  .pickhead .sp{flex:1;}
  .pick{
    border:1px solid var(--line);border-radius:6px;background:var(--sunk);
    max-height:212px;overflow:auto;margin-top:7px;
  }
  .pick label{
    display:flex;gap:8px;align-items:flex-start;padding:6px 10px;
    font-size:var(--fs-base);color:var(--tx-2);cursor:pointer;line-height:1.45;
    border-bottom:1px solid var(--line);
    transition:background var(--t) var(--ease),color var(--t) var(--ease);
  }
  .pick label:last-child{border-bottom:0;}
  .pick label:hover{background:var(--hover);color:var(--tx);}
  .pick input{
    flex:none;width:15px;height:15px;margin:2px 0 0;
    accent-color:var(--ac);cursor:pointer;
  }
  .pick span{flex:1;word-break:break-word;}
  /* 未勾选的课压暗，让「这次要查哪几门」一眼看得出来。
     别压太狠：实测 opacity .4 + 灰字在深色底上几乎读不出字。 */
  .pick label.dim{opacity:.62;}
  .pick label.dim:hover{opacity:.9;}
  .pick .sep{
    padding:5px 10px;font-size:var(--fs-xs);color:var(--mut);
    background:var(--sunk);border-bottom:1px solid var(--line);
  }
  .cok{font-size:var(--fs-sm);color:var(--mut);}
  .cok.ok{color:var(--ok);}
  .cok.warn{color:var(--warn);}

  /* ============================== 主区 ============================== */
  /* ===== 左侧竖直导航（照抄云析 NavigationRail）===== */
  #rail{
    flex:0 0 var(--rail-w);width:var(--rail-w);display:flex;flex-direction:column;
    gap:4px;padding:10px 8px;background:var(--rail);border-right:1px solid var(--line);
  }
  #rail .rail-sp{flex:1;}
  .tab{
    display:flex;flex-direction:column;align-items:center;justify-content:center;
    gap:5px;padding:10px 4px;border:0;border-radius:10px;background:none;
    font:inherit;font-size:var(--fs-xs);color:var(--mut);cursor:pointer;
    transition:background var(--t) var(--ease),color var(--t) var(--ease);
  }
  .tab .ic{width:20px;height:20px;}
  .tab:hover{background:var(--raise);color:var(--tx-2);}
  .tab.on{background:var(--ac-soft);color:var(--ac);font-weight:600;}

  .bar{
    flex:none;display:flex;align-items:center;gap:9px;
    padding:8px 18px;border-bottom:1px solid var(--line);background:var(--bg);
  }
  .bar .ttl{font-size:var(--fs-base);color:var(--mut);}
  .bar .sp{flex:1;}

  .mini{
    display:inline-flex;align-items:center;gap:6px;
    padding:5px 10px;font:inherit;font-size:var(--fs-sm);
    background:var(--raise);color:var(--tx-2);
    border:1px solid var(--line);border-radius:5px;cursor:pointer;
    white-space:nowrap;
    transition:background var(--t) var(--ease),border-color var(--t) var(--ease),
               color var(--t) var(--ease);
  }
  .mini .ic{width:13px;height:13px;}
  .mini:hover:not(:disabled){background:#1c2b2e;border-color:var(--line-2);color:var(--tx);}
  .mini:disabled{opacity:.38;cursor:not-allowed;}
  .mini.pri{background:var(--ac);border-color:var(--ac);color:var(--ac-on);font-weight:600;}
  .mini.pri .ic{color:var(--ac-on);}
  .mini.pri:hover:not(:disabled){background:var(--ac-hi);border-color:var(--ac-hi);}
  .bmenu{
    position:fixed;z-index:90;min-width:250px;padding:4px;
    background:var(--raise);border:1px solid var(--line-2);border-radius:8px;
    box-shadow:0 12px 30px rgba(0,0,0,.5);
  }
  .bmenu-it{
    display:block;width:100%;text-align:left;border:0;background:none;
    color:var(--tx);padding:8px 10px;border-radius:6px;cursor:pointer;
    font:inherit;font-size:var(--fs-sm);
  }
  .bmenu-it:hover{background:#1c2b2e;}
  .bmenu-it .t{display:block;font-size:var(--fs-base);}
  .bmenu-it .s{display:block;color:var(--mut);font-size:var(--fs-xs);
    margin-top:2px;}
  .mini.attn{animation:attn 1s var(--ease) 2;}
  @keyframes attn{50%{box-shadow:0 0 0 3px var(--ac-soft);}}
  #dockpane{padding:0;position:relative;overflow:hidden;}
  #dockpane .empty{margin:40px 20px;font-size:var(--fs-base);color:var(--mut);padding:7px 0;}
  #docklive{color:var(--warn);font-size:inherit;}

  .pane{flex:1;overflow:auto;min-height:0;}
  /* 结果页多级导航（v6.15）：#res 里分「主页面 / 任务页」两个子视图，各自一条
     小工具条 + 可滚动正文。主页面看总览、任务页看进度，来回切不再互相顶掉。 */
  #res{display:flex;flex-direction:column;}
  #resHome,#resTask{flex:1;display:flex;flex-direction:column;min-height:0;}
  #reshomebody,#resTaskBody{flex:1;overflow:auto;min-height:0;}
  .resbar{flex:none;display:flex;align-items:center;gap:9px;
    padding:8px 18px;border-bottom:1px solid var(--line);background:var(--bg);}
  .resbar .ttl{font-size:var(--fs-base);color:var(--mut);}
  .resbar .sp{flex:1;}
  .resbar.stale{border-bottom-color:var(--ac);}
  .resbar.stale .ttl{color:var(--ac);}
  #log{
    margin:0;padding:14px 20px 44px;
    font-family:"Cascadia Mono",Consolas,ui-monospace,monospace;
    font-size:var(--fs-sm);line-height:1.72;
    font-variant-numeric:tabular-nums;
    white-space:pre-wrap;word-break:break-all;color:var(--tx-2);
  }
  #log div.warn{color:var(--warn);}
  #log div.err{color:var(--err);}
  #log div.ok{color:var(--ok);}

  /* ====================== 结果区：巡检台账 ====================== */
  /* 上下不留内边距：表头 sticky 时若顶部还空着 16px，滚动的内容会从
     表头上方那道缝里钻出来。留白改由首个元素自己的 margin 提供。 */
  #res{padding:0 20px 52px;}
  #res > *:first-child{margin-top:16px;}
  #res .sec{margin-bottom:30px;}
  #res .sec > h2{
    margin:0 0 10px;font-size:var(--fs-lg);font-weight:600;
    letter-spacing:.01em;
  }
  /* 计数用方章而不是胶囊：台账的记号 */
  #res .sec > h2 .n{
    display:inline-block;vertical-align:1px;
    font-size:var(--fs-base);font-weight:600;color:var(--ac);
    font-variant-numeric:tabular-nums;
    padding:1px 7px;border:1px solid color-mix(in srgb,var(--ac) 35%,transparent);border-radius:2px;
  }
  #res .sec > h2 .n.warn{
    color:var(--warn);border-color:color-mix(in srgb,var(--warn) 35%,transparent);
  }
  #res .brushbar{
    display:flex;align-items:center;gap:8px;flex-wrap:wrap;
    margin:0 0 12px;padding:10px 12px;
    background:var(--rail);border:1px solid var(--line);border-radius:8px;
  }
  #res .brushbar .note{flex:1 1 260px;min-width:200px;margin:0;}
  /* 工具条里的下拉不该占满整行（它是这个小工具条的一个部件，不是表单主字段） */
  #res .brushbar select{
    width:auto;min-width:86px;padding:3px 24px 3px 8px;
    background-size:12px 12px;background-position:right 6px center;
  }

  /* 台账本体：表头吸顶、行间只留发丝线、数字右对齐等宽。
     必须用 border-collapse:separate —— collapse 下 Chrome 不给 th 做 sticky
     （表格框会跟着内容一起滚走），这是踩过的坑，不要再改回 collapse。 */
  #res table{
    width:100%;border-collapse:separate;border-spacing:0;
    font-size:var(--fs-base);line-height:1.45;
  }
  #res thead th{
    position:sticky;top:0;z-index:2;
    text-align:left;font-weight:600;font-size:var(--fs-sm);color:var(--mut);
    padding:8px 10px;background:var(--rail);
    border-bottom:1px solid var(--line-2);white-space:nowrap;
  }
  #res tbody td{
    padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top;
    overflow-wrap:anywhere;
  }
  #res tbody tr:hover td{background:var(--rail);}
  #res tbody tr:last-child td{border-bottom-color:transparent;}
  #res .num{font-variant-numeric:tabular-nums;white-space:nowrap;}
  #res .col-act{width:1%;white-space:nowrap;}
  /* 单门刷新 ↻：紧贴课程名的小图标按钮，只重抓这一门 */
  #res .rfc{display:inline-flex;align-items:center;padding:2px 4px;margin-left:5px;
    vertical-align:middle;background:var(--raise);color:var(--mut);
    border:1px solid var(--line);border-radius:4px;cursor:pointer;line-height:0;
    transition:color var(--t) var(--ease),border-color var(--t) var(--ease);}
  #res .rfc .ic{width:11px;height:11px;}
  #res .rfc:hover:not(:disabled){color:var(--ac);border-color:var(--ac);}
  #res .rfc:disabled{opacity:.35;cursor:not-allowed;}
  /* 暂存答案列：逐题「题号 + 答案」，窄列可换行，核对时一目十行 */
  #res .anscell{max-width:320px;font-size:var(--fs-xs);color:var(--mut);}
  #res .ansrow{display:flex;gap:6px;line-height:1.5;}
  #res .ansrow b{color:var(--ac);font-weight:600;min-width:1.6em;}
  /* 行内按钮压紧：它的高度撑着整行，高密度台账不该被按钮撑成 44px */
  #res tbody .mini{padding:3px 8px;font-size:var(--fs-xs);gap:4px;}
  #res a{color:var(--ac);text-decoration:none;}
  #res a:hover{text-decoration:underline;}
  #res .sub{font-size:var(--fs-sm);color:var(--mut);}
  #res .empty{font-size:var(--fs-base);color:var(--mut);padding:7px 0;}

  /* 状态记号：小方章 + 文字，颜色本身承载含义 */
  #res .st{
    display:inline-flex;align-items:center;gap:6px;
    white-space:nowrap;font-weight:600;
  }
  #res .st::before{
    content:'';flex:none;width:6px;height:6px;background:currentColor;
  }
  #res .st.err{color:var(--err);}
  #res .st.warn{color:var(--warn);}
  #res .st.ok{color:var(--ok);}
  #res .st.mut{color:var(--mut);font-weight:400;}

  /* 提示条（中止扫描 / 范围受限 / 页面未加载） */
  #res .banner{
    display:flex;gap:9px;align-items:flex-start;
    font-size:var(--fs-base);line-height:1.6;
    padding:9px 12px;margin:0 0 12px;border-radius:6px;
    background:color-mix(in srgb,var(--warn) 7%,transparent);border:1px solid color-mix(in srgb,var(--warn) 30%,transparent);
    color:var(--warn);
  }
  #res .banner.err{
    background:color-mix(in srgb,var(--err) 7%,transparent);border-color:color-mix(in srgb,var(--err) 32%,transparent);
    color:var(--err);
  }
  #res .banner .ic{width:15px;height:15px;margin-top:2px;flex:none;}
  #res .banner span{min-width:0;}

  #res .meta{
    font-size:var(--fs-sm);color:var(--mut);
    padding-bottom:12px;margin-bottom:16px;border-bottom:1px solid var(--line);
  }

  /* 账号密码错误的说明卡 */
  #res .errbox{
    border:1px solid color-mix(in srgb,var(--err) 34%,transparent);border-radius:8px;
    padding:13px 15px;background:color-mix(in srgb,var(--err) 6%,transparent);
  }
  #res .errbox b{display:block;color:var(--err);font-size:var(--fs-md);margin-bottom:5px;}
  #res .errbox span{color:var(--tx-2);font-size:var(--fs-base);line-height:1.7;}

  /* 每门课一个折叠：课程名 + 完成度常驻，只收起章节明细 */
  #res details.cbox{
    border:1px solid var(--line);border-radius:7px;
    margin-bottom:6px;background:var(--rail);
    transition:border-color var(--t) var(--ease);
  }
  #res details.cbox:hover{border-color:var(--line-2);}
  #res details.cbox[open]{border-color:var(--line-2);}
  #res details.cbox > summary{
    display:flex;align-items:center;gap:10px;
    padding:9px 12px;cursor:pointer;user-select:none;list-style:none;
    transition:background var(--t) var(--ease);
  }
  #res details.cbox > summary::-webkit-details-marker{display:none;}
  #res details.cbox > summary .chev{
    width:14px;height:14px;flex:none;color:var(--mut);
    transition:transform var(--t) var(--ease),color var(--t) var(--ease);
  }
  #res details.cbox[open] > summary .chev{transform:rotate(90deg);color:var(--ac);}
  #res details.cbox > summary:hover{background:var(--raise);}
  #res details.cbox > summary .hd{
    flex:1;min-width:0;display:flex;align-items:center;gap:12px;
    font-size:var(--fs-md);
  }
  #res details.cbox > summary .hd b{
    flex:1;min-width:0;font-weight:600;overflow-wrap:anywhere;
  }
  #res details.cbox > summary:hover .hd b{color:var(--ac);}
  #res details.cbox > summary .foldhint{
    flex:none;color:var(--mut);font-size:var(--fs-sm);
  }
  #res details.cbox .t-open{display:none;}
  #res details.cbox[open] .t-closed{display:none;}
  #res details.cbox[open] .t-open{display:inline;}
  /* 完成度刻度：按比例填充的量尺，方头、无阴影——它是度量不是装饰。
     新一版台账出现时它自己长出来一次：整页只安排这一个动作，不逐块都演一遍。 */
  .meter{
    display:inline-block;width:84px;height:5px;flex:none;
    background:var(--line);overflow:hidden;
  }
  .meter > span{
    display:block;height:100%;background:var(--ac);
    transform-origin:left center;
    animation:meter 520ms var(--ease) both;
  }
  @keyframes meter{from{transform:scaleX(0);}}

  #res .chap{padding:2px 12px 10px 36px;}
  #res .chap > div{
    display:flex;align-items:center;gap:8px;
    padding:5px 0;font-size:var(--fs-base);color:var(--tx-2);
    border-bottom:1px solid var(--line);
  }
  #res .chap > div:last-child{border-bottom:0;}
  #res .chap .ckw{display:inline-flex;align-items:center;flex:none;}
  #res .chap .ckw input{
    width:14px;height:14px;margin:0;accent-color:var(--ac);cursor:pointer;
  }
  #res .chap .cnm{flex:1;min-width:0;overflow-wrap:anywhere;}
  #res .chap .cnt{
    flex:none;font-size:var(--fs-sm);color:var(--warn);font-weight:600;
    font-variant-numeric:tabular-nums;
  }
  #res details.cbox summary .bkwrap{
    flex:none;display:inline-flex;align-items:center;gap:6px;
    font-size:var(--fs-sm);color:var(--tx-2);cursor:pointer;
    padding:3px 9px;border:1px solid var(--line);border-radius:5px;
    background:var(--sunk);white-space:nowrap;
    transition:border-color var(--t) var(--ease),color var(--t) var(--ease);
  }
  #res details.cbox summary .bkwrap:hover{border-color:var(--ac);color:var(--ac);}
  #res details.cbox summary .bkwrap input{
    width:14px;height:14px;margin:0;accent-color:var(--ac);cursor:pointer;
  }

  /* 侧栏统计：紧凑的台账条，不是「大数字+小标签」的模板 */
  .cards{
    border:1px solid var(--line);border-radius:7px;overflow:hidden;
    background:var(--sunk);
  }
  .card{
    display:flex;align-items:baseline;gap:10px;
    padding:7px 11px;border-bottom:1px solid var(--line);
    font-size:var(--fs-base);
  }
  .card:last-child{border-bottom:0;}
  .card span{flex:1;color:var(--mut);min-width:0;}
  .card b{
    font-weight:600;color:var(--tx);font-variant-numeric:tabular-nums;
    white-space:nowrap;
  }

  /* 说明区：默认收起，需要时再展开（不占常驻视线） */
  details.notes{margin-top:2px;}
  details.notes > summary{
    display:flex;align-items:center;gap:7px;cursor:pointer;list-style:none;
    font-size:var(--fs-xs);font-weight:600;color:var(--mut);
    padding:4px 0;
  }
  details.notes > summary::-webkit-details-marker{display:none;}
  details.notes > summary:hover{color:var(--tx-2);}
  details.notes > summary .chev{
    width:13px;height:13px;flex:none;transition:transform var(--t) var(--ease);
  }
  details.notes[open] > summary .chev{transform:rotate(90deg);}
  details.notes .note{margin-top:8px;}
  details.notes a{color:var(--ac);text-decoration:none;overflow-wrap:anywhere;}
  details.notes a:hover{text-decoration:underline;}

  /* 视觉模型（可选）：默认折起，但**做成一张有边框的卡**——旧版只是一行灰字，
     容易被当成说明文字划过去（用户真机反馈「看不太明显」）。现在给它底色 +
     边框 + 主色图标 + 状态徽标，一眼能看出「这里还能加一个模型」。展开后才亮主色。 */
  details.visbox{
    margin-top:12px;border:1px solid var(--line);border-radius:8px;
    background:var(--sunk);overflow:hidden;
  }
  details.visbox[open]{border-color:var(--line-2);}
  details.visbox > summary{
    display:flex;align-items:center;gap:8px;cursor:pointer;list-style:none;
    padding:9px 11px;color:var(--tx-2);
  }
  details.visbox > summary::-webkit-details-marker{display:none;}
  details.visbox > summary:hover{background:var(--raise);}
  details.visbox > summary .chev{
    width:13px;height:13px;flex:none;color:var(--mut);
    transition:transform var(--t) var(--ease);
  }
  details.visbox[open] > summary .chev{transform:rotate(90deg);color:var(--ac);}
  details.visbox .visic{width:15px;height:15px;flex:none;color:var(--ac);}
  details.visbox .vistt{
    flex:1 1 auto;min-width:0;display:flex;flex-direction:column;gap:1px;
  }
  details.visbox .vistt b{
    font-size:var(--fs-sm);font-weight:600;color:var(--tx);line-height:1.4;
  }
  details.visbox > summary:hover .vistt b{color:var(--ac);}
  details.visbox .vishint{font-size:var(--fs-xs);color:var(--mut);font-weight:400;}
  details.visbox .vischip{
    display:inline-block;margin-left:5px;padding:0 5px;border-radius:999px;
    font-size:var(--fs-xs);font-weight:600;vertical-align:1px;
    color:var(--ac);background:var(--ac-soft);border:1px solid color-mix(in srgb,var(--ac) 30%,transparent);
  }
  details.visbox .visbadge{
    flex:none;padding:1px 7px;border-radius:999px;font-size:var(--fs-xs);
    font-weight:600;color:var(--mut);background:var(--raise);
    border:1px solid var(--line);
  }
  details.visbox .visbadge.on{
    color:var(--ok);border-color:color-mix(in srgb,var(--ok) 34%,transparent);
  }
  details.visbox .visbody{padding:0 11px 11px;}
  details.visbox > .visbody{margin-top:9px;}
  details.visbox .note{margin-top:8px;}

  /* 页脚：联系方式与下载地址。常驻可见（不折进说明区），
     但用小字号弱化，不抢操作区的注意力。 */
  .foot{
    margin-top:11px;padding-top:9px;border-top:1px solid var(--line);
    font-size:var(--fs-xs);color:var(--mut);line-height:1.7;
    overflow-wrap:anywhere;
  }
  .foot a{color:var(--ac);text-decoration:none;}
  .foot a:hover{text-decoration:underline;}

  /* ============================== 提示条 ============================== */
  .toast{
    position:fixed;left:50%;bottom:26px;z-index:99;
    transform:translate(-50%,14px);
    display:flex;align-items:flex-start;gap:9px;
    background:var(--raise);border:1px solid var(--line-2);color:var(--tx);
    padding:11px 16px;border-radius:8px;font-size:var(--fs-base);
    max-width:min(72vw,560px);line-height:1.55;
    box-shadow:0 8px 26px rgba(0,0,0,.5),0 1px 0 rgba(255,255,255,.03) inset;
    opacity:0;pointer-events:none;
    transition:opacity 200ms var(--ease),transform 220ms var(--ease);
  }
  .toast.show{opacity:1;transform:translate(-50%,0);}
  .toast .ic{width:15px;height:15px;margin-top:2px;flex:none;}
  .toast.warn{border-color:color-mix(in srgb,var(--warn) 50%,transparent);color:var(--warn);}
  .toast.err{border-color:color-mix(in srgb,var(--err) 50%,transparent);color:var(--err);}
  .toast.ok{border-color:color-mix(in srgb,var(--ok) 50%,transparent);color:var(--ok);}

  /* ===== 视图容器（左栏 5 个页面）===== */
  .view{flex:1;display:flex;flex-direction:column;min-height:0;overflow:auto;}
  #viewQuery{flex-direction:row;overflow:hidden;}
  #viewQuery #res{flex:1;min-width:0;}
  .setwrap{width:100%;max-width:780px;margin:0 auto;padding:22px 26px 64px;}
  .sett{margin:0 0 18px;font-size:var(--fs-xl);font-weight:600;color:var(--tx);}
  .setsec{margin:0 0 26px;}
  .setsec-t{margin:0 0 10px;font-size:var(--fs-xs);font-weight:600;
    color:var(--mut);letter-spacing:.04em;}
  .setrow{display:flex;align-items:center;gap:14px;padding:12px 14px;margin-bottom:8px;
    border:1px solid var(--line);border-radius:9px;background:var(--raise);}
  .setrow .st{flex:1;min-width:0;}
  .setrow .st b{display:block;font-size:var(--fs-base);font-weight:500;color:var(--tx);}
  .setrow .st i{display:block;font-style:normal;font-size:var(--fs-sm);
    color:var(--mut);margin-top:3px;line-height:1.5;}
  .sw{position:relative;flex:none;width:42px;height:24px;padding:0;border:0;
    border-radius:999px;background:var(--line-2);cursor:pointer;
    transition:background var(--t) var(--ease);}
  .sw::after{content:'';position:absolute;top:3px;left:3px;width:18px;height:18px;
    border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.35);
    transition:transform var(--t) var(--ease);}
  .sw.on{background:var(--ac);}
  .sw.on::after{transform:translateX(18px);}
  .seg{display:inline-flex;border:1px solid var(--line);border-radius:9px;overflow:hidden;}
  .seg button{border:0;background:var(--raise);color:var(--tx-2);padding:8px 16px;
    font:inherit;font-size:var(--fs-sm);cursor:pointer;
    transition:background var(--t) var(--ease);}
  .seg button + button{border-left:1px solid var(--line);}
  .seg button.on{background:var(--ac);color:var(--ac-on);font-weight:600;}
  .swatches{display:flex;flex-wrap:wrap;gap:9px;margin-top:4px;}
  .swatch{width:30px;height:30px;padding:0;border-radius:50%;cursor:pointer;
    border:2px solid transparent;transition:border-color var(--t) var(--ease);}
  .swatch.on{border-color:var(--tx);}
  .seedbox{display:flex;align-items:center;gap:8px;margin-top:10px;}
  /* 引导提示开关：关掉后隐藏说明性文字；风险提示（.banner 等）不带 .guide，不受影响 */
  body.guides-off .guide{display:none !important;}

  @media (prefers-reduced-motion:reduce){
    *,*::before,*::after{animation-duration:.01ms !important;
                         animation-iteration-count:1 !important;
                         transition-duration:.01ms !important;}
  }
</style>
</head>
<body>
<header>
  <div class="brand">
    <h1>学习通巡检工具</h1>
    <span class="ver">__VERSION__</span>
  </div>
  <span class="hspace"></span>
  <div class="hstat">
    <span class="pill"><i class="dot" id="dot"></i><span id="stxt">空闲</span></span>
    <span class="who" id="who" style="display:none"><span class="ic"
      data-i="user"></span><span id="whotxt"></span></span>
  </div>
</header>

<div class="wrap">
  <nav id="rail">
    <button class="tab on" data-t="res"><span class="ic" data-i="search"></span>查询</button>
    <button class="tab" data-t="log"><span class="ic" data-i="list"></span>日志</button>
    <button class="tab" data-t="dock"><span class="ic" data-i="monitor"></span>预览<span
      id="docklive" style="display:none">（进行中）</span></button>
    <button class="tab" data-t="link"><span class="ic" data-i="link"></span>上传链接</button>
    <span class="rail-sp"></span>
    <button class="tab" data-t="settings"><span class="ic" data-i="gear"></span>设置</button>
  </nav>

  <div class="main">
    <div class="bar" id="bQueryBar">
      <span class="ttl" id="bartip">尚未查询</span>
      <span class="sp"></span>
      <button class="mini" id="boff"><span class="ic" data-i="monitor"></span>熄屏</button>
      <button class="mini" id="bclear"><span class="ic" data-i="trash"></span>清空日志</button>
    </div>

    <!-- ===== 查询页：左列查询操作 + 右列结果 ===== -->
    <div class="view" id="viewQuery">
      <aside id="queryOps">
        <button class="btn pri" id="bquery"><span class="ic" data-i="search"></span>
          开始查询</button>

        <section class="blk">
          <h2 class="blk-t">查询范围</h2>
          <div class="field"><input type="number" id="recentTop" min="1"
                 placeholder="只查最近 N 门课（留空 = 全部）"></div>
          <button class="btn" id="blist"><span class="ic" data-i="list"></span>
            获取课程列表</button>
          <div id="cwrap" style="display:none">
            <div class="pickhead">
              <span id="cnum">尚未选择</span>
              <span class="sp"></span>
              <button class="mini" id="call" title="勾选当前显示出来的课程">全选</button>
              <button class="mini" id="cnone" title="取消当前显示出来的勾选">清空</button>
            </div>
            <div class="field" style="margin-top:7px">
              <input type="text" id="cfilter" placeholder="筛选课程名…">
            </div>
            <div class="pickhead" style="margin-top:0">
              <button class="mini pri" id="cpin"
                      title="把已勾选的课程排到列表最上面，方便核对">置顶已选的课</button>
              <span class="cok" id="cok"></span>
            </div>
            <div class="pick" id="clist"></div>
          </div>
          <p class="note guide">勾选优先于上面填的数：勾了课就只查这几门，一门不勾 = 查全部。</p>
        </section>

        <section class="blk" id="cardsblk" style="display:none">
          <h2 class="blk-t">本次统计</h2>
          <div id="cards" class="cards"></div>
        </section>
      </aside>

      <section class="pane" id="res">
        <div id="resHome">
          <div class="resbar">
            <span class="ttl" id="resmeta">尚未查询</span>
            <span class="sp"></span>
            <button class="mini" id="bpause" disabled>
              <span class="ic" data-i="pause"></span>暂停</button>
            <button class="mini dgr" id="bstop" disabled>
              <span class="ic" data-i="stop"></span>停止</button>
            <button class="mini" id="bdoing" style="display:none">
              <span class="ic" data-i="monitor"></span>正在进行</button>
            <button class="mini" id="brefresh" disabled
              title="重新读取课程列表（课程有增删时用；只想刷新某一门的状态，点那行课程名旁的 ↻）">
              <span class="ic" data-i="refresh"></span>刷新课程列表</button>
          </div>
          <div id="reshomebody"><div class="empty guide">还没有结果。到左侧「设置」里填好账号密码，回这里点「开始查询」。</div></div>
        </div>
        <div id="resTask" style="display:none">
          <div class="resbar">
            <button class="mini" id="bback"><span class="ic" data-i="back"></span>返回主页面</button>
            <span class="ttl" id="taskhead"></span>
            <span class="sp"></span>
          </div>
          <div id="resTaskBody"></div>
        </div>
      </section>
    </div>

    <div class="pane" id="logpane" style="display:none"><pre id="log"></pre></div>
    <div class="pane" id="dockpane" style="display:none">
      <div class="empty" id="dockempty">无内容</div>
    </div>

    <!-- ===== 上传链接 ===== -->
    <div class="view" id="viewLink" style="display:none">
      <div class="setwrap">
        <section class="blk">
          <h2 class="blk-t">上传链接刷课刷题</h2>
          <div class="field">
            <textarea id="linkbox" rows="6" spellcheck="false"
              placeholder="把学习通页面链接粘进来，一行一个：&#10;课程页 / 任务页链接 → 刷课&#10;作业链接 → 刷题"></textarea>
          </div>
          <div class="row" style="margin-bottom:8px">
            <button class="btn sm pri grow" id="blinkbrush">
              <span class="ic" data-i="play"></span>刷课</button>
            <button class="btn sm grow" id="blinkanswer">
              <span class="ic" data-i="play"></span>刷题</button>
            <button class="btn sm grow" id="blinkcombo">
              <span class="ic" data-i="play"></span>刷课+刷题</button>
          </div>
          <label class="chk"><input type="checkbox" id="linkdraft">
            刷题时只暂存、不交卷（人工审查后再自己交）</label>
          <p class="note guide">不依赖上面的查询结果，直接按你贴的链接干活。刷课认课程页 /
            任务页链接（读 courseid + clazzid，回课程页找未完成章节刷视频）；
            刷题认作业链接（答完交卷，附件 / 报告题整份跳过）；刷课+刷题按链接类型
            自动分流。只接受超星 / 高校域名。刷题要先在「设置 · 大模型」里配好大模型。</p>
        </section>
      </div>
    </div>

    <!-- ===== 设置 ===== -->
    <div class="view" id="viewSettings" style="display:none">
      <div class="setwrap">
        <h1 class="sett">设置</h1>

        <section class="setsec">
          <h2 class="setsec-t">账号</h2>
          <div class="field"><input type="text" id="phone" placeholder="手机号 / 超星号"
                 autocomplete="username"></div>
          <div class="field has-in">
            <input type="password" id="pwd" placeholder="学习通密码"
                   autocomplete="current-password">
            <button type="button" class="ic-in" id="beye" data-for="pwd"
                    aria-pressed="false" title="显示 / 隐藏密码"
                    aria-label="显示或隐藏密码"></button>
          </div>
          <label class="chk"><input type="checkbox" id="remember">
            记住账号</label>
          <label class="chk"><input type="checkbox" id="rememberpwd">
            记住密码（加密存在本机）</label>
          <label class="chk"><input type="checkbox" id="deep" checked>
            下钻到章节，列出未完成任务点</label>
          <button class="btn" id="blogin"><span class="ic" data-i="qr"></span>
            扫码 / 短信登录</button>
          <button class="btn" id="blogout"
                  title="清除本机登录会话，下次查询需重新登录（换号用）">
            <span class="ic" data-i="logout"></span>退出登录（换号）</button>
          <p class="note guide">首次使用填账号密码即可；密码登录被平台拦下时会提示改用扫码。
            查询在左侧「查询」页发起。</p>
        </section>

        <section class="setsec">
          <h2 class="setsec-t">外观</h2>
          <div class="setrow">
            <div class="st"><b>主题与外观</b><i>应用明暗外观，可跟随系统</i></div>
          </div>
          <div class="seg" id="themeMode">
            <button data-v="system">跟随系统</button>
            <button data-v="light">浅色</button>
            <button data-v="dark">深色</button>
          </div>
          <div class="setrow" style="margin-top:8px">
            <div class="st"><b>主题颜色</b><i>选择预设主色，或自定义十六进制色号</i></div>
          </div>
          <div class="swatches" id="themeSeed">
            <button class="swatch" data-v="#415f91" style="background:#415f91" title="蓝"></button>
            <button class="swatch" data-v="#3f51b5" style="background:#3f51b5" title="靛蓝"></button>
            <button class="swatch" data-v="#6750a4" style="background:#6750a4" title="紫"></button>
            <button class="swatch" data-v="#c2185b" style="background:#c2185b" title="玫红"></button>
            <button class="swatch" data-v="#b3261e" style="background:#b3261e" title="红"></button>
            <button class="swatch" data-v="#f4631c" style="background:#f4631c" title="橙"></button>
            <button class="swatch" data-v="#f9a825" style="background:#f9a825" title="金黄"></button>
            <button class="swatch" data-v="#38761d" style="background:#38761d" title="绿"></button>
            <button class="swatch" data-v="#00897b" style="background:#00897b" title="青"></button>
            <button class="swatch" data-v="#0288d1" style="background:#0288d1" title="天蓝"></button>
          </div>
          <div class="seedbox">
            <span style="font-size:var(--fs-sm);color:var(--mut)">自定义</span>
            <input type="text" id="seedHex" placeholder="#RRGGBB" style="max-width:130px">
          </div>
        </section>

        <section class="setsec">
          <h2 class="setsec-t">大模型</h2>
          <div class="modelcard">
            <div class="mstat" id="llmstate">
              <span class="ic" data-i="plug"></span><span>读取中…</span>
            </div>
            <div class="field">
              <select id="llm_platform"></select>
            </div>
            <div class="field">
              <input type="text" id="llm_url" placeholder="接口地址（填到 /v1 即可）">
            </div>
            <div class="field">
              <input type="text" id="llm_model" placeholder="模型名，如 deepseek-chat">
            </div>
            <div class="field has-in">
              <input type="password" id="llm_key" placeholder="API Key">
              <button type="button" class="ic-in" id="bkeyeye" data-for="llm_key"
                      aria-pressed="false" title="显示 / 隐藏 API Key"
                      aria-label="显示或隐藏 API Key"></button>
            </div>
            <div class="row" style="margin-bottom:8px">
              <button class="btn sm pri grow" id="bllm">
                <span class="ic" data-i="check"></span>保存</button>
              <button class="btn sm grow" id="bllmtest">
                <span class="ic" data-i="bolt"></span>测试连接</button>
              <button class="btn sm dgr" id="bllmclear" title="清除本机保存的 API Key">
                <span class="ic" data-i="trash"></span></button>
            </div>
            <p class="note guide">任何 OpenAI 兼容接口都能用。Key 加密保存在本机
              （Windows 用户级加密，换电脑或换 Windows 用户后需重填），
              不回显在页面上。做作业用你自己的 Key 按量计费，一份选择题通常几分钱。</p>

            <details class="visbox">
              <summary>
                <span class="chev" data-i="chev"></span>
                <span class="visic" data-i="eye"></span>
                <span class="vistt">
                  <b>视觉模型（可选）<span class="vischip">加强</span></b>
                  <span class="vishint">让模型「亲眼看」公式题 / 图表题</span>
                </span>
                <span class="visbadge" id="visbadge">未启用</span>
              </summary>
              <div class="visbody">
                <div class="mstat" id="visstate">
                  <span class="ic" data-i="plug"></span><span>读取中…</span>
                </div>
                <div class="row" style="margin:9px 0 8px">
                  <button class="btn sm grow" id="bvisfrommain"
                          title="把上面主模型的地址 / 模型名 / Key 原样搬到这一块">
                    <span class="ic" data-i="copy"></span>复制上面主模型的配置</button>
                </div>
                <div class="field">
                  <select id="vis_platform"></select>
                </div>
                <div class="field">
                  <input type="text" id="vis_url" placeholder="接口地址（填到 /v1 即可）">
                </div>
                <div class="field">
                  <input type="text" id="vis_model" placeholder="视觉模型名，如 qwen-vl-max">
                </div>
                <div class="field has-in">
                  <input type="password" id="vis_key" placeholder="API Key">
                  <button type="button" class="ic-in" id="bviskeyeye" data-for="vis_key"
                          aria-pressed="false" title="显示 / 隐藏 API Key"
                          aria-label="显示或隐藏 API Key"></button>
                </div>
                <div class="row" style="margin-bottom:8px">
                  <button class="btn sm pri grow" id="bvis">
                    <span class="ic" data-i="check"></span>保存</button>
                  <button class="btn sm grow" id="bvistest">
                    <span class="ic" data-i="bolt"></span>测试连接</button>
                  <button class="btn sm dgr" id="bvisclear" title="清除本机保存的视觉模型 Key">
                    <span class="ic" data-i="trash"></span></button>
                </div>
                <p class="note guide">题面是图片或公式的题（超星的数学公式常以图片 / MathML 渲染），
                  纯文字提取拿不到题面，交给它看图作答。留空 = 不启用，仍按纯文本答题；
                  只对主观题和带图 / 公式的题调用，客观选择题不花这份钱。
                  可以跟上面用同一家的同一个 Key —— 直接点「复制上面主模型的配置」即可。</p>
              </div>
            </details>
          </div>
        </section>

        <section class="setsec">
          <h2 class="setsec-t">通用</h2>
          <button class="btn" id="bopen" disabled>
            <span class="ic" data-i="file"></span>打开最新报告</button>
          <button class="btn" id="bdir"><span class="ic" data-i="folder"></span>
            打开输出文件夹</button>
          <button class="btn" id="bcheckupd"><span class="ic" data-i="refresh"></span>
            检查更新</button>
          <button class="btn" id="bexportlog"><span class="ic" data-i="file"></span>
            导出日志</button>
          <div class="setrow">
            <div class="st"><b>刷课时阻止电脑休眠</b>
              <i>任务运行期间防止系统自动睡眠，避免刷到一半断掉</i></div>
            <button class="sw on" id="swKeep" aria-label="刷课时阻止电脑休眠"></button>
          </div>
          <div class="setrow">
            <div class="st"><b>显示引导提示</b>
              <i>页面上的帮助说明类文字，关掉后更清爽</i></div>
            <button class="sw on" id="swGuide" aria-label="显示引导提示"></button>
          </div>
        </section>

        <details class="notes">
          <summary><span class="chev" data-i="chev"></span>使用说明与边界规则</summary>
          <p class="note">
            查询是只读的；刷课与答题必须你手动点按钮才会跑。所有数据只留在本机。
            <br><br>
            <b>换账号：</b>到「设置 · 账号」里直接填新账号的密码、回「查询」页点「开始查询」就行。
            本机若存着别的账号的会话，会自动改登你填的这个（旧会话归档到
            <code>runtime/profile_old</code>，不会丢），绝不会拿旧账号继续跑。顶栏「本机会话」随时显示当前在用哪个账号。
            <br><br>
            <b>用完怎么退出：</b>直接关掉这个窗口（右上角 ×），或关掉那个命令行窗口。
            只关浏览器页面不会退出，后台程序还在跑、端口还占着。
            <br><br>
            <b>熄屏后唤不醒：</b>按 Win+Ctrl+Shift+B 重置显卡驱动即可恢复。
          </p>
        </details>

        <p class="foot">
          有问题或建议请联系 QQ：3547502147<br>
          最新版下载地址：<a href="https://github.com/CC0987326/chaoxing-scanner"
             target="_blank" rel="noopener">https://github.com/CC0987326/chaoxing-scanner</a>
        </p>
      </div>
    </div>

  </div>
</div>
<div class="toast" id="toast"></div>
<script>
let since = 0, timer = null, closed = false, lastDone = 0;
let dropped = 0;        // 服务端已累计丢弃的日志行数（对不上 = 自己的 since 已被截走）
let pollSeq = 0;        // 轮询代际令牌：按钮重启 poll 时使在途的旧循环作废，
                        // 否则旧循环 await 完又会排一个新 timer，多循环并行
                        // 会把同一批日志追加 N 遍（实测出现过 ×2 / ×4）
let rv = -1;            // 已经拿到的结果版本号（回传给服务端，避免重复下发整份报告）
let av = -1;            // 交卷结果面板的版本号（机制同 rv）
let cv = -1;            // 课程勾选清单的版本号（机制同 rv；变了才整份下发）
let aitems = [];        // 交卷结果面板的数据（重新核实按钮要用）
let cardFp = '';        // 左侧统计卡片的指纹：内容没变就不重建 DOM
let lastDockN = 0;      // 预览页签：上一轮 poll 的浏览器窗口数（变了才动 DOM）
// 刷题 / 刷课 / 答题这类任务在启动成功时置位；任务一结束就把界面收回到
// 「查询结果」，并用左侧当前勾选自动重跑一次查询。不这样做的话，结果区会一直
// 挂着启动时那句「正在后台刷这一份…」——因为单份刷题不产生交卷面板
// （_answer_single_worker 只写 summary，不调 _set_ares），没人来覆盖它。
//
// 这条自动刷新的**副作用必须收窄**（2026-10-08 被三个老套件抓出来的）：
// 它是系统自己发的，不是用户点的，所以不能顺手干掉用户正看着的东西——
//   · 不清日志（那是刚才刷题的全过程）
//   · 不动「记住手机号」偏好
//   · 不收起「本次统计」卡片（那正是刚跑完那个任务的结果：刷完几个、是否已停止）
//   · 不把结果面板换成占位/错误卡片（会话过期时最容易踩：刚刷完的清单会被
//     一张「未登录」冲掉，用户白等一场）
// 另外，勾了「刷完自动关机」时不重跑：机器马上要关，再挂一个 1~2 分钟的全量
// 扫描纯属添乱。见 startBrush / startCombo 里的 `autoBack = !shut`。
// 还有一条「撤销」：任务确实产出了交卷面板（做作业并交卷 / 刷课+刷题）时，
// 那块面板本身就是用户要的结果，poll 里会当场把 autoBack 撤掉、不再重跑。
let autoBack = false;
// 自动刷新这一趟是不是还在飞。用于让「自动刷新的失败」只提示、不冲掉面板。
let autoRun = false;
// 这是一次「刷题 / 刷课 / 答题」类任务：结束时要收口状态行。
// 单独一个变量是因为存在「结束但不重查」的路（勾了自动关机），
// 那条路同样不能把「刷视频中…」留在屏幕上。
let brushLike = false;
// 任务页里有没有「东西可看」（进行中的进度 / 刚跑完的交卷面板）。
// v6.15 起任务跑完不再自动弹回主页面，用户可能先点「返回主页面」看总览，
// 之后还想再回去看刚才那份结果——所以「正在进行」按钮不能只在 running 时出现，
// 得在任务页有内容时一直亮着（新一次查询开始时才清掉，那份任务已成历史）。
let taskHas = false;
const $ = id => document.getElementById(id);
const logEl = $('log');

// ---------------------------------------------------------------- 图标
// 全部自绘 SVG，同一套笔画（1.6 / 圆头圆角 / 16 格）。不用 emoji 或 Unicode
// 字形当图标：那些在不同系统上字形不一、粗细不齐，是「拼装」最明显的信号。
// 尺寸由容器决定（width/height 100%），所以只有一处定义。
const ICON_PATHS = {
  search:'<circle cx="7" cy="7" r="4.2"/><path d="M10.2 10.2 13.6 13.6"/>',
  qr:'<rect x="2.6" y="2.6" width="4.4" height="4.4" rx="1"/>'
    +'<rect x="9" y="2.6" width="4.4" height="4.4" rx="1"/>'
    +'<rect x="2.6" y="9" width="4.4" height="4.4" rx="1"/>'
    +'<path d="M9.2 9.2h1.6M12.6 9.2h.6M9.2 11.6h.6M11.6 11.6v2M13.2 10.8v2.8"/>',
  list:'<path d="M5.6 4h8M5.6 8h8M5.6 12h8"/><path d="M2.7 4h.01M2.7 8h.01M2.7 12h.01"/>',
  play:'<path d="M5.6 3.7 12 8l-6.4 4.3z"/>',
  pause:'<path d="M6.2 3.6v8.8M9.8 3.6v8.8"/>',
  stop:'<rect x="4" y="4" width="8" height="8" rx="1.4"/>',
  chev:'<path d="M6 3.6 10.4 8 6 12.4"/>',
  check:'<path d="M3 8.5 6.2 11.7 13 4.9"/>',
  bolt:'<path d="M8.9 1.9 3.7 9h3.2l-.7 5.1L11.4 7H8.2z"/>',
  trash:'<path d="M2.9 4.5h10.2M6.4 4.5V3.1h3.2v1.4"/>'
    +'<path d="M4.3 4.5l.6 8.4h6.2l.6-8.4"/>',
  file:'<path d="M8.9 1.9H4.5a1 1 0 0 0-1 1v10.2a1 1 0 0 0 1 1h7a1 1 0 0 0 1-1V5.6z"/>'
    +'<path d="M8.9 1.9v3.7h3.6"/><path d="M5.9 8.6h4.2M5.9 10.9h2.9"/>',
  folder:'<path d="M1.9 4.3a1 1 0 0 1 1-1h2.8l1.3 1.6h6.1a1 1 0 0 1 1 1v6.2'
    +'a1 1 0 0 1-1 1H2.9a1 1 0 0 1-1-1z"/>',
  power:'<path d="M8 2.3v5.2"/><path d="M11.7 4.6a5.2 5.2 0 1 1-7.4 0"/>',
  monitor:'<rect x="1.9" y="2.9" width="12.2" height="8.4" rx="1.1"/>'
    +'<path d="M5.7 13.9h4.6"/><path d="M3.1 8.3 12.9 4.8"/>',
  plug:'<path d="M6.1 1.9v2.9M9.9 1.9v2.9"/>'
    +'<path d="M4.2 4.8h7.6v2.4a3.8 3.8 0 0 1-7.6 0z"/><path d="M8 11v3"/>',
  back:'<path d="M13 8H3.2"/><path d="M6.9 4.3 3.2 8l3.7 3.7"/>',
  logout:'<path d="M6.2 2.9H3.5a1 1 0 0 0-1 1v8.2a1 1 0 0 0 1 1h2.7"/>'
    +'<path d="M10.4 11.1 13.5 8l-3.1-3.1"/><path d="M13.5 8H6.4"/>',
  eye:'<path d="M1.7 8S4.2 3.9 8 3.9 14.3 8 14.3 8 11.8 12.1 8 12.1 1.7 8 1.7 8z"/>'
    +'<circle cx="8" cy="8" r="1.9"/>',
  eyeoff:'<path d="M6.4 4.3A6.7 6.7 0 0 1 8 4.1c3.8 0 6.3 3.9 6.3 3.9a12 12 0 0 1-2.1 2.5"/>'
    +'<path d="M4.2 5.7A12 12 0 0 0 1.7 8s2.5 3.9 6.3 3.9c1 0 1.9-.3 2.7-.7"/>'
    +'<path d="M2.5 2.5 13.5 13.5"/>',
  alert:'<path d="M8 2.7 14.1 12.9H1.9z"/><path d="M8 6.5v3M8 11.3h.01"/>',
  info:'<circle cx="8" cy="8" r="6"/><path d="M8 7.4v3.4M8 5.5h.01"/>',
  user:'<circle cx="8" cy="5.7" r="2.6"/><path d="M3 13.5a5.2 5.2 0 0 1 10 0"/>',
  refresh:'<path d="M13.4 8A5.4 5.4 0 1 1 11.8 4.2"/><path d="M13.6 2.7v3.1h-3.1"/>',
  external:'<path d="M9.5 6.5 13.6 2.4"/><path d="M10.7 2.4h2.9v2.9"/>'
    +'<path d="M12.1 9.6v3.4a1 1 0 0 1-1 1H3.6a1 1 0 0 1-1-1V5.5a1 1 0 0 1 1-1h3.3"/>',
  copy:'<rect x="5.4" y="5.4" width="8.2" height="8.2" rx="1.4"/>'
    +'<path d="M2.4 10.2V3.8a1.4 1.4 0 0 1 1.4-1.4h6.4"/>',
  link:'<path d="M6.5 9.5 9.5 6.5"/>'
    +'<path d="M6.9 4.5 5.4 6a2.6 2.6 0 0 0 3.7 3.7L10.6 8.2"/>'
    +'<path d="M9.1 11.5 10.6 10a2.6 2.6 0 0 0-3.7-3.7L5.4 7.8"/>',
  gear:'<circle cx="8" cy="8" r="2.3"/>'
    +'<path d="M8 1.6v2M8 12.4v2M1.6 8h2M12.4 8h2'
    +'M3.5 3.5l1.4 1.4M11.1 11.1l1.4 1.4M12.5 3.5l-1.4 1.4M4.9 11.1l-1.4 1.4"/>',
};
function icon(name){
  return '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" '
    + 'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" '
    + 'aria-hidden="true" style="width:100%;height:100%;display:block">'
    + (ICON_PATHS[name] || '') + '</svg>';
}
// 静态 HTML 里只写 <span class="ic" data-i="名字"></span>，加载时统一填一次。
// （:empty 保证不会重复填，也不会覆盖已有的动态内容）
function paintIcons(root){
  (root || document).querySelectorAll('[data-i]:empty').forEach(el => {
    el.innerHTML = icon(el.dataset.i);
  });
}

// 统计条连小标题一起收放：只藏内容会把空标题留在侧栏里。
function setCardsShown(on){
  const blk = $('cardsblk');
  if (blk) blk.style.display = on ? '' : 'none';
}

// 只在文字真的变了才写 DOM：轮询每几百毫秒来一次，无脑赋值会白白触发重排
function setText(el, v){
  const s = String(v == null ? '' : v);
  if (el.textContent !== s) el.textContent = s;
}

function toast(msg, kind){
  const t = $('toast');
  const ic = kind === 'ok' ? 'check' : (kind ? 'alert' : 'info');
  t.innerHTML = '<span class="ic">' + icon(ic) + '</span><span>' + esc(msg) + '</span>';
  t.className = 'toast show' + (kind ? ' ' + kind : '');
  clearTimeout(t._h);
  t._h = setTimeout(() => { t.className = 'toast'; }, kind === 'err' ? 7000 : 4500);
}

// 记住手机号
try {
  const saved = localStorage.getItem('cx_phone');
  if (saved) { $('phone').value = saved; $('remember').checked = true; }

  // 记住密码：勾上即存（后端 DPAPI 加密，本机本用户才解得开），
  // 取消即删。勾选时密码框是空的 = 没东西可记，弹回去让先填。
  $('rememberpwd').onchange = () => {
    if (!$('rememberpwd').checked) {
      fetch('/api/pwd-forget', {method:'POST',
        headers:{'Content-Type':'application/json'}});
      return;
    }
    const pw = $('pwd').value;
    if (!pw) {
      $('rememberpwd').checked = false;
      alert('先填密码，再勾「记住密码」。');
      return;
    }
    fetch('/api/pwd-save', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({password: pw})})
      .then(r => r.json()).then(j => {
        if (!j.ok) {
          $('rememberpwd').checked = false;
          alert('这台机器加密不可用，密码没有保存（仍需每次手输）。');
        }
      });
  };
  // 启动回填：存过密码就填回去并勾上（密码框已有输入时不打扰）。
  fetch('/api/pwd-get').then(r => r.json()).then(j => {
    if (j.has && j.pwd && !$('pwd').value) {
      $('pwd').value = j.pwd;
      $('rememberpwd').checked = true;
    }
  }).catch(() => {});
} catch(e){}

function append(lines){
  for (const l of lines){
    const d = document.createElement('div');
    d.className = l.lvl === 'info' ? '' : l.lvl;
    d.textContent = l.t;
    logEl.appendChild(d);
  }
  if (lines.length) logEl.parentElement.scrollTop = logEl.parentElement.scrollHeight;
}

function esc(s){
  // 引号也要转：抓来的作业标题/课程名可能含双引号，esc 进属性值
  // （href="..."）时不转义就能从属性里逃逸注入事件
  return String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
// 抓来的 URL 只认 http(s)：javascript: 之类的伪协议点了就执行
function safeUrl(u){
  return (/^https?:\/\//i).test(String(u || '')) ? esc(u) : '';
}
// 桌面窗口（v4.0）：页面里的外链都带 target="_blank"。在窗口内跳转会顶掉
// 整个界面、而且没有后退键，所以桌面模式下拦住它，改让后端用系统默认浏览器
// 打开（见 /api/open）。网页模式不拦 —— 那里开新标签页本来就是对的。
// 排除 .go-work：「去完成」由下面自己的处理器走 /api/open-work（带登录态、
// 开进预览），不能被这里抢去开系统浏览器——真机 2026-10-10 两个窗口齐飞的
// 根因就是这个拦截器与 .go-work 处理器在同一张链接上同时命中。
if (__DESKTOP__) {
  document.addEventListener('click', function (ev) {
    var a = ev.target && ev.target.closest
      ? ev.target.closest('a[target="_blank"]:not(.go-work)') : null;
    if (!a || !a.href) return;
    ev.preventDefault();
    fetch('/api/open', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url: a.href})});
  }, true);
}

// ---------- 课程勾选 ----------
// 勾选即生效：勾上哪几门，这次就查哪几门，不需要再点一次确认。
let courseFp = '';

function boxes(){ return [...document.querySelectorAll('#clist input')]; }
function checkedKeys(){ return boxes().filter(b => b.checked).map(b => b.value); }
function picked(){ return checkedKeys(); }        // 提交给后端的课程范围

function setCok(text, kind){
  const el = $('cok');
  el.textContent = text || '';
  el.className = 'cok' + (kind ? ' ' + kind : '');
}
function removeSeps(){
  document.querySelectorAll('#clist .sep').forEach(s => s.remove());
}

function refreshSel(){
  const all = boxes();
  const top = parseInt($('recentTop').value || '0', 10) || 0;
  const n = all.filter(b => b.checked).length;

  // 左侧只交代总门数，勾选数量统一由下面那行说，免得两处各说一遍还打架
  $('cnum').textContent = !all.length ? ''
    : (!n
       ? (top ? ('未勾选 → 将按上方「最近 ' + Math.min(top, all.length) + ' 门」查')
              : ('共 ' + all.length + ' 门课程（不勾选 = 查全部）'))
       : ('共 ' + all.length + ' 门课程'));

  // 勾了的保持原色，没勾的压暗；一门没勾时不压暗（全都暗等于没提示）
  all.forEach(b => b.parentElement.classList.toggle('dim', n > 0 && !b.checked));

  setCok(n ? ('已选中 ' + n + ' 门课')
           : ('未勾选任何课，将查询全部 ' + all.length + ' 门'),
         n ? 'ok' : '');
}
function updateCount(){ refreshSel(); }

// 勾选变了：立即生效，只需刷新明暗与计数
function onSelChange(){
  removeSeps();   // 「已选置顶」的分隔线是按旧数量写的，改动后撤掉，免得数字对不上
  refreshSel();
}
// 把已勾选的课程排到列表最上面，方便一眼核对该查哪几门
function pinPicked(){
  const all = boxes();
  if (!all.length){ toast('请先点上面的「获取课程列表」', 'warn'); return; }
  const n = checkedKeys().length;
  if (!n){ toast('还没勾选任何课程 —— 不勾选就是查全部，不用整理', 'warn'); return; }
  removeSeps();
  const idx = lb => parseInt(lb.dataset.idx || '0', 10);
  const on  = all.filter(b => b.checked).map(b => b.parentElement).sort((a, b) => idx(a) - idx(b));
  const off = all.filter(b => !b.checked).map(b => b.parentElement).sort((a, b) => idx(a) - idx(b));
  const list = $('clist');
  list.innerHTML = '';
  on.forEach(lb => list.appendChild(lb));
  if (off.length){
    const sep = document.createElement('div');
    sep.className = 'sep';
    sep.textContent = '以下 ' + off.length + ' 门未勾选（本次不查）';
    list.appendChild(sep);
  }
  off.forEach(lb => list.appendChild(lb));
  list.scrollTop = 0;
  refreshSel();
  toast('已把选中的 ' + n + ' 门课排到最上面', 'ok');
}
function renderCourses(list){
  // 用指纹判断：列表没变就不重建 DOM，否则会把用户已勾的选项清掉
  const fp = list.map(c => c.key).join('|');
  if (fp === courseFp) return;
  courseFp = fp;
  const box = $('clist');
  box.innerHTML = '';
  list.forEach((c, i) => {
    const lb = document.createElement('label');
    lb.dataset.name = c.name || '';
    lb.dataset.idx = i;              // 记住平台原始顺序，置顶后还能排回去
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = c.key;
    cb.onchange = onSelChange;
    const sp = document.createElement('span');
    sp.textContent = c.name || '(未命名课程)';
    lb.appendChild(cb); lb.appendChild(sp);
    box.appendChild(lb);
  });
  $('cwrap').style.display = list.length ? '' : 'none';
  $('cfilter').value = '';
  refreshSel();
}

// 「刷完自动关机」的勾选状态：结果区每 900ms 重绘一次，勾选框会被重建，
// 必须像折叠状态一样重绘前读出、渲染时回填，否则勾上几秒后自己弹开。
var sdAfterOn = false;

// 倍速选择：同样会被每 900ms 重绘重建，值用全局变量跨重绘保存。
// 初始值等 /api/llm-config 返回后覆盖为本机上次的选择（默认 1.25x）。
// 档位只有 1/1.25/1.5/2 四个——它们是播放器官方档位，别的值平台会拨回去。
var brateVal = '1.25';
function rateOpts(){
  return ['1', '1.25', '1.5', '2'].map(v =>
    '<option value="' + v + '"' + (String(brateVal) === v ? ' selected' : '')
    + '>' + v + 'x</option>').join('');
}

// 单门刷新：每行课程名旁的 ↻。只重抓这门课（复用登录态、不重读课程列表），
// 秒级返回。有 cid 才渲染——老报告 / 无定位信息的行不给假按钮。
function rfBtn(it){
  if (!it || !it.cid) return '';
  return ' <button class="rfc" onclick="refreshCourse(event, this)"'
    + ' title="只刷新「' + esc(it.course || '') + '」这一门的状态'
    + '（不重查其它课程，约 3~4 秒）"'
    + ' data-cid="' + esc(it.cid) + '"'
    + ' data-clsid="' + esc(it.clsid || '') + '"'
    + ' data-name="' + esc(it.course || '') + '">'
    + '<span class="ic">' + icon('refresh') + '</span></button>';
}

// 未完成作业清单。「刷题」按钮按索引取用——结果区每 900ms 整个重绘一次，
// 索引比把对象塞进 onclick 更抗重绘（重绘时 lastHw 同步刷新为同一份数据）。
let lastHw = [];
// 「进行中的课堂活动」清单，同理按索引取用（活动行的「作答」按钮）。
let lastAct = [];
// 活动节的「刷题只暂存」勾选（重绘要保回），以及刷题是否可做（只看能否定位）。
var actDraftOn = false;
function actLocatable(it){ return !!(it && it.cid && it.clsid && it.id); }
// 「作答」只对随堂练习/测验(42)、问卷(14)这类走 answerQuestion 答题面的活动出现
// ——别的类型（主题讨论/AI实践/签到…）平台跳的是完全不同的页面，大模型替不了
// （以前对它们也拼 answerQuestion，平台回「非法请求」，用户点进去就报错）。
// 报告里每条带 answerable；老报告没这个字段时按「有定位就当能做」兜底。
function actAnswerable(it){ return actLocatable(it) && it.answerable !== false; }

// 提示条：中止扫描 / 范围受限 / 页面没加载成功。都是「这份报告你不能全信」
// 的边界说明，所以放在台账最上面，用状态色而不是装饰色。
function bannerBox(kind, html){
  return '<div class="banner' + (kind === 'err' ? ' err' : '') + '">'
    + '<span class="ic">' + icon(kind === 'err' ? 'alert' : 'info') + '</span>'
    + '<span>' + html + '</span></div>';
}

function renderResult(r){
  if (!r) return;
  // 任务以异常收尾时结果里只有 error：把「查询中…」的占位换掉，
  // 别让用户停在无限转圈的假象里（失败详情在「运行日志」页签）
  if (r.error){
    $('reshomebody').innerHTML = '<div class="errbox"><b>任务失败</b>'
      + '<span>' + esc(r.error) + '<br>详情见「运行日志」页签；'
      + '可修正后重试（未登录时先登录）。</span></div>';
    // 工具条那句「生成时间 …」记的是**上一次**成功查询的时间；本次失败后不重写，
    // 就会与下面这张「任务失败」卡并排，旧结果的时间冒充了这次的结果（正是本工具
    // 最忌讳的「旧结果冒充」）。顺手清掉「清单可能已变」的 stale 标记。
    clearHomeStale();
    setText($('resmeta'), '任务失败');
    return;
  }
  const hw = r.undone_hw || [], ex = r.undone_exam || [], pg = r.undone_prog || [];

  // 结果区被整个重绘（innerHTML 覆盖）时，交互状态会跟着一起丢：<details> 的
  // 展开、倍速下拉、自动关机勾选都必须在重绘前读出、渲染时按原样补回，
  // 否则用户点开看一眼、一秒后自己又合上了，勾上的选择也会自己弹开。
  // 折叠用索引当 key：课程名会重复，不能当 key。
  const sda = document.getElementById('sdafter');
  if (sda) sdAfterOn = sda.checked;
  const bsl = document.getElementById('brate');
  if (bsl) brateVal = bsl.value;
  const ad = document.getElementById('actdraft');
  if (ad) actDraftOn = ad.checked;
  const openIdx = new Set();
  document.querySelectorAll('#res details.cbox').forEach(d => {
    if (d.open) openIdx.add(d.getAttribute('data-idx'));
  });

  let h = '';
  if (r.partial){
    h += bannerBox('warn', '本次是中止扫描：只检查了 ' + (r.scanned||0) + '/'
      + (r.course_total||0) + ' 门课程，未列出的课程是「没查」而不是「已完成」。');
  } else if (r.selected_only){
    const isRecent = r.scope_kind === 'recent';
    h += bannerBox('warn', '本次只检查了' + (isRecent ? '最近学习的 ' : '你勾选的 ')
      + (r.course_total||0) + ' 门课程（账号共 ' + (r.account_total||0) + ' 门）。'
      + (isRecent ? '更早的课程没查' : '未勾选的课没查') + '，不在下表范围内。');
  }
  // 页面没加载成功的课必须显式警告——v2.5 的教训是把它们静默报成
  // 「无作业模块」，用户拿着假报告以为没作业。没失败时这块完全不出现。
  if (r.page_failed && r.page_failed.length){
    h += bannerBox('err', '有 ' + r.page_failed.length + ' 门课的页面没有加载成功（'
      + esc(r.page_failed.map(function(x){ return x.course; }).join('、'))
      + '），这些课的作业 / 考试是「没查到」而不是「没有」，建议稍后重新查询。');
  }
  h += '<div class="meta">生成时间 ' + esc(r.time) + '　·　' + (r.partial
       ? ('已扫描 ' + (r.scanned||0) + ' / ' + (r.course_total||0) + ' 门课程')
       : (r.selected_only
          ? ((r.scope_kind === 'recent' ? '已查最近 ' : '已勾选 ')
             + (r.course_total||0) + ' 门（账号共 ' + (r.account_total||0) + ' 门）')
          : ('共扫描 ' + (r.course_total||0) + ' 门课程'))) + '</div>';

  // 进行中的课堂活动放最上面：老师刚发、有时限，最该先处理。
  const ac = r.undone_act || [];
  lastAct = ac;
  h += '<div class="sec"><h2>进行中的课堂活动 <span class="n">' + ac.length
     + '</span> 项</h2>';
  if (ac.length){
    h += '<div class="note" style="margin:-2px 0 10px 2px">这些是老师发的'
       + '课堂活动，只在课程首页的「任务」页里，作业和章节任务点都不含它。'
       + '每条的「去完成」按类型打开对的页面（测验 / 问卷 / 主题讨论 / AI实践'
       + '各有各的页）。刷课 = 把该课任务点的视频刷掉；'
       + '刷题 = 大模型答这条活动（暂存 = 答完存草稿不交，提交 = 直接交）——'
       + '只有「随堂练习/测验」「问卷」这两种大模型能替；主题讨论、AI实践、签到'
       + '那些答题面不一样，只给「去完成」，要你本人做。</div>';
    // 只要有一条活动能定位到课程，就给一排「刷课 / 刷课+刷题」按钮
    const anyAct = ac.some(it => it.cid && it.clsid && it.id);
    if (anyAct){
      h += '<div class="brushbar">'
         + '<button class="mini pri" onclick="startActBrush(event)">'
         + '<span class="ic">' + icon('play') + '</span>刷这些课的视频</button>'
         + '<button class="mini" onclick="startActCombo(event)">'
         + '<span class="ic">' + icon('play') + '</span>刷课+刷题</button>'
         + '<label class="chk inline"><input type="checkbox" id="actdraft"'
         + (actDraftOn ? ' checked' : '') + '> 刷题只暂存（不交卷）</label>'
         + '<span class="note">刷题逐条在右侧「作答」按钮上做；'
         + '勾「只暂存」后这里两个按钮的刷题环节也改成暂存。'
         + '「刷这些课的视频」把这一节里每门课的待完成章节视频刷掉，'
         + '已经刷完的课会跳过。</span>'
         + '</div>';
    }
    h += '<table><thead><tr><th>课程</th><th>类型</th><th>名称</th>'
       + '<th>剩余时间</th><th class="col-act"></th></tr></thead><tbody>';
    for (let i = 0; i < ac.length; i++){
      const it = ac[i], uu = safeUrl(it.url);
      h += '<tr><td>' + esc(it.course) + rfBtn(it) + '</td>'
         + '<td><span class="st warn">' + esc(it.kind || '活动') + '</span></td>'
         + '<td>' + esc(it.name || '（无标题）') + '</td>'
         + '<td class="num sub">' + esc(it.time || '—') + '</td>'
         + '<td class="col-act">'
         + (uu ? '<a href="' + uu + '" class="go-work" rel="noopener">去完成</a>' : '')
         + (actAnswerable(it)
            ? (uu ? ' ' : '')
              + '<button class="mini" onclick="brushMenu(event,\'act\',' + i + ')"'
              + ' title="让大模型答这一条：点开选「暂存」（存平台草稿，人工审查后自己交）'
              + '或「提交」（直接正式交）。要上传附件的题整条跳过，不会乱填">'
              + '<span class="ic">' + icon('chev') + '</span>作答</button>'
            : '')
         + '</td></tr>';
    }
    h += '</tbody></table>';
  } else { h += '<div class="empty">没有进行中的课堂活动。</div>'; }
  h += '</div>';

  h += '<div class="sec"><h2>未完成作业 <span class="n">' + hw.length
     + '</span> 项</h2>';
  lastHw = hw;
  if (hw.length){
    h += '<table><thead><tr><th>课程</th><th>作业名称</th><th>状态</th>'
       + '<th>剩余</th><th class="col-act"></th></tr></thead><tbody>';
    for (let i = 0; i < hw.length; i++){
      const it = hw[i], u = safeUrl(it.url);
      h += '<tr><td>' + esc(it.course) + rfBtn(it) + '</td><td>' + esc(it.title)
         + '</td><td><span class="st err">' + esc(it.state) + '</span></td>'
         + '<td class="num sub">' + esc(it.left || '—') + '</td>'
         + '<td class="col-act">'
         + (u ? '<a href="' + u + '" class="go-work" rel="noopener">去完成</a>' : '')
         + (u ? ' <button class="mini" onclick="brushMenu(event,\'row\',' + i + ')"'
              + ' title="让大模型做这一份：点开选「暂存」（存平台草稿，人工审查后自己交）'
              + '或「提交」（直接正式交卷）。要上传附件的题整份跳过，不会乱填">'
              + '<span class="ic">' + icon('chev') + '</span>刷题</button>'
              : '')
         + '</td></tr>';
    }
    h += '</tbody></table>';
  } else { h += '<div class="empty">没有未完成的作业。</div>'; }
  h += '</div>';

  h += '<div class="sec"><h2>未完成考试 <span class="n">' + ex.length
     + '</span> 项</h2>';
  if (ex.length){
    h += '<table><thead><tr><th>课程</th><th>考试名称</th><th>状态</th>'
       + '</tr></thead><tbody>';
    for (const it of ex){
      h += '<tr><td>' + esc(it.course) + rfBtn(it) + '</td><td>' + esc(it.title)
         + '</td><td><span class="st err">' + esc(it.state) + '</span></td></tr>';
    }
    h += '</tbody></table>';
  } else { h += '<div class="empty">没有待完成的考试。</div>'; }
  h += '</div>';

  h += '<div class="sec"><h2>未完成任务点 <span class="n">' + pg.length
     + '</span> 门课</h2>';
  if (pg.length){
    // 任务入口：勾哪门 / 哪节做哪节。课程勾选框 = 该课全部待完成章节，
    // 章节勾选框 = 只做那一节；两种可以混勾，按钮决定做什么。
    h += '<div class="brushbar">'
       + '<button class="mini pri" id="bbrush" onclick="startBrush(event)">'
       + '<span class="ic">' + icon('play') + '</span>刷选中的课的视频</button>'
       + '<button class="mini" onclick="startCombo(event)">'
       + '<span class="ic">' + icon('play') + '</span>刷课+刷题</button>'
       + '<button class="mini pri" onclick="brushMenu(event,\'tasks\')">'
       + '<span class="ic">' + icon('chev') + '</span>做作业</button>'
       + '<button class="mini" onclick="toggleAllBrush(event)">全选</button>'
       + '<label class="chk inline" title="播放倍速。只提供播放器官方档位：别的值平台会拨回去，'
       + '来回打架反而频繁卡顿。实际播放会在这档和相邻档之间随机取挡，中途还会换一次挡">倍速 '
       + '<select id="brate" onchange="brateVal=this.value">' + rateOpts() + '</select></label>'
       + '<button class="mini" onclick="screenOff(event)">'
       + '<span class="ic">' + icon('monitor') + '</span>熄屏</button>'
       + '<label class="chk inline"><input type="checkbox" id="sdafter"'
       + (sdAfterOn ? ' checked' : '') + '> 刷完自动关机</label>'
       + '<span class="note">勾课程 = 全部章节；展开后可只勾某些章节。'
       + '刷视频 = 倍速静音真实播放；做作业 = 点开选「暂存」（存平台'
       + '草稿不交卷）或「提交」（正式交卷）；同题干复用上次答案，'
       + '附件 / 报告题会跳过并提示。'
       + '自动关机只在正常刷完时触发，关机前留 60 秒缓冲'
       + '（cmd 运行 shutdown /a 可取消）。熄屏后任务照常在后台跑，动下鼠标就亮。</span>'
       + '</div>';
    // 每门课一个折叠：课程名 + 完成度常驻可见，只把章节明细收起来
    // （一门课能拉出十几行，要缩短的是长度，不是把整节藏起来）。
    for (let i = 0; i < pg.length; i++){
      const it = pg[i], chs = it.chapters || [], idx = String(i);
      const bkey = (it.cid && it.clsid) ? (it.cid + ':' + it.clsid) : '';
      const rate = Math.max(0, Math.min(100, Number(it.rate) || 0));
      h += '<details class="cbox"' + (openIdx.has(idx) ? ' open' : '')
         + ' data-idx="' + idx + '"><summary>'
         + '<span class="chev">' + icon('chev') + '</span>'
         + '<span class="hd"><b>' + esc(it.course) + '</b>' + rfBtn(it)
         + '<span class="meter"><span style="width:' + rate + '%"></span></span>'
         + '<span class="sub">已完成 ' + it.done + '/' + it.total
         + '（' + rate.toFixed(1) + '%）</span></span>'
         + '<span class="foldhint">'
         + '<span class="t-closed">'
         + (chs.length ? chs.length + ' 个章节待完成 · 点这里展开' : '点这里展开')
         + '</span><span class="t-open">点这里收起</span></span>'
         // 没有 cid/clsid 的课（旧缓存结果）压根不渲染勾选框——没有定位参数
         // 就不能假装能刷
         + (bkey
            ? '<label class="bkwrap" onclick="event.stopPropagation()"'
              + ' title="勾上后点上面的按钮：刷视频 / 做作业 / 刷课+刷题">'
              + '<input type="checkbox" class="bk" value="' + esc(bkey)
              + '"> 选中</label>'
            : '')
         + '</summary>';
      if (chs.length){
        h += '<div class="chap">';
        for (const c of chs){
          const ckb = (bkey && c.kid)
            ? '<label class="ckw" onclick="event.stopPropagation()"'
              + ' title="只勾这一节：上面的按钮就只处理这一节">'
              + '<input type="checkbox" class="ck" value="'
              + esc(bkey + '|' + c.kid) + '"></label>'
            : '';
          h += '<div>' + ckb + '<span class="cnm">' + esc(c.name) + '</span>'
             + '<span class="cnt">待完成 ' + (c.count||1) + '</span></div>';
        }
        h += '</div>';
      } else {
        h += '<div class="sub" style="padding:2px 12px 10px 36px">'
           + '（未取到章节明细）</div>';
      }
      h += '</details>';
    }
  } else {
    h += '<div class="empty">所有课程的任务点都已完成。</div>';
  }
  h += '</div>';

  h += '<div class="sub">完整清单（含全部 ' + (r.course_total||0)
     + ' 门课程总览）已保存为 Markdown，可到「设置」点「打开最新报告」查看。</div>';
  $('reshomebody').innerHTML = h;
  // 总览一次落地就把它标成「主页面内容」，并清掉「清单可能已变」的提示（这次
  // 就是最新的一份）。**切到主页面子视图**：新到手的清单就该看得见（v6.15 前
  // 只有一个结果容器，新结果天然可见；分成两层后若用户正停在任务页，新总览会
  // 被 display:none 藏住）。页签不动——免得打断用户正在看的日志/预览。
  showRes('home');
  clearHomeStale();
  setText($('resmeta'), r.time ? ('生成时间 ' + r.time) : '查询结果');
}

// 单门刷新：走 run 的静默分支（auto），不清日志、不换占位、不动用户偏好。
// 只发一条 refresh_one，后端只重抓这一门并把它并回当前清单。
function refreshCourse(ev, btn){
  if (ev){ ev.stopPropagation(); ev.preventDefault(); }
  const cid = btn.getAttribute('data-cid') || '';
  if (!cid){ toast('这门课缺少定位信息，无法单独刷新', 'warn'); return; }
  if ($('brefresh').disabled){ toast('有任务正在跑，等状态变「已完成」再刷新这一门', 'warn'); return; }
  btn.classList.add('attn');
  run('refresh_one', {auto: true, user: true, cid: cid,
    clsid: btn.getAttribute('data-clsid') || '',
    name: btn.getAttribute('data-name') || '这一门'});
}

async function poll(){
  const me = ++pollSeq;          // 新循环上岗，旧的在途循环过时
  if (closed) return;
  let busy = false;
  try{
    const r = await fetch('/api/poll?since=' + since + '&rv=' + rv
      + '&av=' + av + '&cv=' + cv);
    if (me !== pollSeq) return;  // 等待期间又有新循环启动了 → 这轮作废
    const j = await r.json();
    busy = !!j.running;
    // 服务端日志超 5000 行会截断：dropped 对不上说明自己手里的 since
    // 已经指向被删掉的区域，清空日志从 0 重拉（本轮拿到的可能不完整，
    // 下一轮 since=0 会把全文补齐）
    if ((j.dropped || 0) !== dropped){
      dropped = j.dropped || 0;
      since = 0; logEl.innerHTML = '';
    }
    // 新任务启动后服务端把 lines 清成 []：total < since 说明手里的游标
    // 已指向不存在的区域，回到 0 重拉。只回游标不清 DOM —— 旧任务的
    // 过程记录留在页面上，新任务的日志接在后面（自动刷新不清日志）。
    if ((j.total || 0) < since) since = 0;
    if (j.lines && j.lines.length){ append(j.lines); since = j.total; }
    if (j.courses_v !== undefined && j.courses_v !== cv){
      cv = j.courses_v;
      if (j.courses && j.courses.length) renderCourses(j.courses);
    }
    $('dot').className = 'dot' + (j.running ? ' on' : '');
    const paused = !!j.paused;
    const finishing = j.phase === 'finishing';
    const listing = j.phase === 'listing';
    setText($('stxt'), j.running
      ? (paused ? '已暂停 · 点「继续」恢复'
                : (finishing ? '收尾中 · 正在生成报告' : ('运行中 · ' + (j.task||''))))
      : (j.done ? '已完成' : '空闲'));
    // 任务页顶栏的标题：跑着显示任务名并带「进行中」，跑完只留任务名（别再挂着
    // 「进行中」误导；文案要让用户知道这是「上次任务」的页面）。
    if (j.running && j.task) setText($('taskhead'), j.task + ' · 进行中');
    else if (taskHas && j.task) setText($('taskhead'), j.task + ' · 已完成');
    // 本机会话是谁的。没有会话（或读不到身份）就不显示，绝不编一个出来。
    // setText 内部做了「值没变就不写 DOM」，每轮轮询不会造成无谓重排。
    setText($('whotxt'), j.login_who ? ('本机会话：' + j.login_who) : '');
    $('who').style.display = j.login_who ? '' : 'none';
    // 「预览」页签：有浏览器窗口嵌着就亮「（进行中）」，面板收起占位。
    // 状态翻转才动 DOM；0→N 的第一下弹提示（不自动切页签，别打断用户）。
    const dn = j.dock_n || 0;
    $('docklive').style.display = dn ? '' : 'none';
    if (dn !== lastDockN){
      $('dockempty').style.display = dn ? 'none' : '';
      if (dn > lastDockN)
        toast('浏览器窗口已就绪：点「预览」页签查看与操作');
      lastDockN = dn;
    }
    ['bquery','blogin','blist','blinkbrush','blinkanswer','blinkcombo']
      .forEach(i => { if ($(i)) $(i).disabled = j.running; });
    $('bquery').title = j.running
      ? (finishing ? '正在收尾（生成报告），稍等几秒就能再次查询' : '任务进行中，请先暂停或停止')
      : '';
    $('blist').title = j.running ? '任务进行中' : '只读取课程名单（约 10 秒，不抓作业）';
    if (j.running && finishing) $('bartip').textContent = '收尾中：正在生成报告…';
    // 读课程名录是十来秒的只读操作，暂停/停止对它无效，就别让按钮看起来能用。
    // 登录只认「停止」不认「暂停」（暂停会把任务挂死，见后端 _control_hook），
    // 所以登录期间也把暂停键置灰——留一个点了没用的按钮只会误导。
    $('bpause').disabled = !j.running || listing || j.phase === 'login';
    $('bstop').disabled  = !j.running || listing;
    // 多级导航按钮：任务在跑时「刷新课程列表」置灰（别和正在跑的任务抢浏览器）、
    // 「正在进行」亮起来（有任务页可看）；闲时反过来。查询本体（bquery）的
    // 禁用逻辑不在这里，它一直可用。
    $('brefresh').disabled = j.running;
    // 单门刷新的 ↻ 同样随运行置灰（同一时刻只跑一个任务）。任务结束后
    // 这一轮轮询会把它们恢复可用（成功时则由 renderResult 重建）。
    document.querySelectorAll('#res .rfc').forEach(b => b.disabled = j.running);
    // 「正在进行」：任务在跑、或任务页还留着刚才那份东西（进度/交卷面板）时亮。    // 同时把标题文字据实翻转——跑完还叫「正在进行」会误导（用户报过的「任务结束了
    // 还显示在刷题」）。只在翻转时写 DOM，省掉每轮一次重排。
    const taskLive = j.running || taskHas || !!j.ares;
    const dLabel = j.running ? '正在进行' : '上次任务';
    if ($('bdoing').style.display !== (taskLive ? '' : 'none'))
      $('bdoing').style.display = taskLive ? '' : 'none';
    if ($('bdoing').dataset.lb !== dLabel){
      $('bdoing').dataset.lb = dLabel;
      $('bdoing').innerHTML = '<span class="ic">' + icon('monitor') + '</span>'
        + dLabel;
    }
    const wasPaused = $('bpause').dataset.paused === '1';
    $('bpause').dataset.paused = paused ? '1' : '0';
    // 按钮文字只在暂停状态真的翻转时才改，省掉每轮一次 innerHTML 重写
    if (wasPaused !== paused) $('bpause').innerHTML = '<span class="ic">'
      + icon(paused ? 'play' : 'pause') + '</span>' + (paused ? '继续' : '暂停');
    if (j.summary){
      const fp = JSON.stringify(j.summary);
      if (fp !== cardFp){
        cardFp = fp;
        const c = $('cards'); c.innerHTML = '';
        setCardsShown(true);
        for (const k in j.summary){
          const e = document.createElement('div');
          e.className = 'card';
          const lb = document.createElement('span');
          lb.textContent = k;
          const vl = document.createElement('b');
          vl.textContent = j.summary[k];
          e.appendChild(lb); e.appendChild(vl);
          c.appendChild(e);
        }
      }
    }
    // 结果只在「换了一版」时重画。以前是无条件每轮重绘，长报告要反复重建上百行
    // DOM，正在选中的文字也会被抖掉；现在轮询只负责把新版本交过来。
    if (j.result && !j.running && j.result_v !== rv){
      rv = j.result_v;
      if (j.result.error && autoRun){
        // 自动刷新是系统自己发的，不是用户点的。它失败时**只提示、不动面板**：
        // 结果面板里那份清单是用户刚拿到手的东西，会话过期时最容易被一张
        // 「未登录」错误卡片冲掉，用户白等一场还丢了清单。
        toast('自动刷新失败：' + j.result.error, 'err');
        setText($('bartip'), '刷新失败 · 详见运行日志');
      } else {
        renderResult(j.result);
        setText($('bartip'), j.result.error ? '任务失败' : '查询完成');
      }
    }
    // 交卷结果面板同理：任务结束才生成，换版才重画，重画时切到结果页
    // 让用户直接看到「哪些可交卷」。
    if (j.ares && !j.running && j.ares_v !== av){
      av = j.ares_v;
      renderAnswerResult(j.ares);
      setText($('bartip'), '交卷结果已生成');
      showRes('task');
      switchTab('res');
      // 交卷面板就是用户要看的「刚才那批结果」：它一出现就别再自动重跑查询了——
      // 重跑会把面板整个冲掉，连面板里「重新核实未确认的」那个按钮一起没了。
      // 四个任务入口启动时都置位了 autoBack，在这里按实际结果撤销。
      autoBack = false;
      brushLike = false;
    }
    // 任务收尾时明确交代一句。否则重复点「获取课程列表」而名单没变时，
    // DOM 不会重建、界面毫无动静，用户会以为按钮坏了
    if (!j.running && j.done && j.started && j.started !== lastDone){
      lastDone = j.started;
      if (j.login_fail === 'credential'){
        // 平台明确回绝这组账号密码。只在这一种情况下报「账号或密码错误」——
        // 验证码没点对、超时等失败不报，免得把人引到「去改密码」的错路上。
        toast('账号或密码错误', 'err');
        $('resTaskBody').innerHTML = '<div class="errbox"><b>账号或密码错误</b>'
          + '<span>请核对账号（手机号 / 超星号）与密码后重新查询；'
          + '密码连续输错可能触发账号保护，也可以改用设置页的「扫码 / 短信登录」。</span>'
          + '</div>';
        // 后端这次把 result 清空了（_set_result(None)），不会再下发新报告，
        // 所以主页面那层必须由这里亲手擦掉：否则上一份查询的清单和时间戳会留在
        // 「主页面」上冒充本次结果（用户点「返回主页面」就看到旧数据）。
        $('reshomebody').innerHTML = '<div class="empty">'
          + '本次登录被平台回绝，没有查到任何数据。</div>';
        showRes('home');
        clearHomeStale();
        setText($('resmeta'), '账号或密码错误');
        $('bartip').textContent = '账号或密码错误';
        switchTab('res');
      } else if (j.task === '获取课程列表'){
        const n = (j.courses || []).length;
        if (n){
          toast('读到 ' + n + ' 门课程：勾选要查的课（勾上就生效），再点「开始查询」；'
                + '一门都不勾就是查全部', 'ok');
          $('bartip').textContent = '课程列表已就绪（' + n + ' 门）';
        } else {
          toast('没读到任何课程，请确认这个账号下有课程', 'err');
          $('bartip').textContent = '课程列表为空';
        }
      }
      // 刷题 / 刷课 / 答题跑完：把界面收回「查询结果」，并用左侧当前勾选重跑一次
      // 查询，让清单反映刚做完的进度（做掉的作业会从清单里消失）。这同时兜掉了
      // 「任务早结束了、结果区还挂着『正在后台刷这一份…』」那个割裂。
      // v6.15：任务跑完**不再自动全量重查**（那趟要 1~2 分钟，用户嫌慢）。
      // 改为在「主页面」挂一个「清单可能已变 · 点课程名旁 ↻」的提示，用户想更新
      // 自己点（#brefresh → run('query',{auto:true})）。任务页仍停在原处，
      // 用户可以从容看刚才的结果，再自己决定回主页面刷新。
      if (autoBack){
        autoBack = false;
        markHomeStale();
      } else {
        autoRun = false;
      }
      // 不管走不走「自动重查」，任务页的状态行都得收口——v6.15 起不再靠那趟
      // 重查（它会把状态行收成「查询完成」）顺带收口，所以必须自己收，否则屏幕
      // 上永远留着「刷题中…」：正是用户报过的「任务结束了还显示在刷题」。
      if (brushLike) setText($('bartip'), '任务已结束');
      // 任务页里那句「正在后台刷…」的启动占位，任务一结束就没人再来覆盖它了
      // （v6.15 起不再自动重查把面板换成新清单）。留着它就等于屏幕永远停在
      // 「任务结束了还显示在刷题」——用户报过的那个。这里直接把它换成一句
      // 收尾说明，保留用户再点单门 ↻ 刷新的动作提示。
      // 交卷面板（renderAnswerResult 会给 #resTaskBody 写带表格的内容）要留住，
      // 所以只替换仍带 .taskph 标记的占位，不动真结果。
      if (brushLike && !j.ares){
        const body = $('resTaskBody');
        const ph = body.querySelector('.taskph');
        if (ph && body.children.length === 1) body.innerHTML =
          '<div class="empty">本次任务已结束 · 结果见「运行日志」；'
          + '要看最新的未完成清单，点上面「返回主页面」后，点对应课程名旁的 ↻ 刷新那一门。</div>';
      }
      brushLike = false;
    }
    $('bopen').disabled = !j.report;
  }catch(e){}
  if (closed || me !== pollSeq) return;
  // 运行中问得密一点（日志和状态跟得更紧），空闲时疏一点，没必要一直戳
  timer = setTimeout(poll, busy ? 400 : 1200);
}
poll();

async function run(action, opts){
  // opts.auto：任务跑完后自动发起的刷新。它是系统自己发的、不是用户点的，
  // 所以除了「切回结果页 + 用当前勾选重查」之外，什么都不该做：
  // 不动用户偏好（手机号记不记）、不弹提示、不清空日志区、不收起本次统计、
  // 不把结果面板换成占位（失败也留着用户正看的那份清单）。
  const auto = !!(opts && opts.auto);
  // user：这次是不是「用户亲手点的」。#brefresh/↻ 这类是用户在动，失败
  // 必须让用户看见（哪怕也走 auto 的静默偏好）；后台自动刷新则不是。
  const user = !!(opts && opts.user);
  if (!auto && __DESKTOP__ && action === 'login'){
    // 扫码 / 短信登录：直接切到「预览」页签——登录窗口会嵌在这里。
    // 入坞那一刻后端就会把键盘焦点交给它，用户在登录页里打字才进得去
    // （真机 2026-10-14：不切过来、焦点不接管，字都落在侧栏账号框）。
    switchTab('dock');
  }
  if (!auto){
    if ($('remember').checked) {
      try { localStorage.setItem('cx_phone', $('phone').value.trim()); } catch(e){}
    } else {
      try { localStorage.removeItem('cx_phone'); } catch(e){}
    }
  }
  const nPick = picked().length;
  const nTop = parseInt($('recentTop').value || '0', 10) || 0;
  if (!auto && action === 'query' && nPick && nTop){
    toast('你勾选了 ' + nPick + ' 门具体课程，「只查最近 N 门」这次不生效（以勾选为准）', 'warn');
  }
  const body = {
    action: action,
    phone: $('phone').value.trim(),
    password: $('pwd').value,
    only: picked(),          // 勾了哪些课；空数组 = 全部
    limit: nTop,             // 只查最近 N 门；勾选非空时后端以勾选为准
    deep: $('deep').checked
  };
  // 单门刷新：带上这门课的定位键（后端据此只重抓这一门）
  if (action === 'refresh_one'){
    body.cid = (opts && opts.cid) || '';
    body.clsid = (opts && opts.clsid) || '';
  }
  // 勾着「记住密码」发起查询：顺手把当前输入的密码存上 ——
  // 这样记住的永远是最近一次真正拿去登录的那把，而不是当初勾选时的。
  if ($('rememberpwd').checked && body.password) {
    fetch('/api/pwd-save', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({password: body.password})});
  }
  let resp;
  try {
    resp = await fetch('/api/run', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  } catch(e) {
    toast('连不上本地程序，请确认程序还在运行', 'err');
    return;
  }
  // 之前这里不看返回码：服务端回 409「忙」时前端照样切到「查询中」，
  // 实际什么也没跑，用户看到的就只是「点了没反应」。
  if (!resp.ok){
    if (resp.status === 409){
      if (!auto || user) toast('上一个任务还在收尾（正在生成报告），等状态变成「已完成」再点一次', 'warn');
    } else if (!auto){
      toast('任务没能启动（HTTP ' + resp.status + '）', 'err');
    }
    return false;
  }
  // 自动刷新时保留日志：那是刚才刷题的过程记录，用户很可能正看着
  if (!auto){ since = 0; logEl.innerHTML = ''; }
  // 「本次统计」卡片展示的正是刚跑完那个任务的结果（刷完几个、是否已停止），
  // 自动刷新不该在用户还没看清时把它收走。新查询的统计一到就会顶掉它
  // （指纹不同 → 重画）。
  // 非自动时连指纹一起清掉：卡片已隐藏，不清指纹的话同样的统计出来时不会重新展开。
  if (!auto){ setCardsShown(false); cardFp = ''; }
  autoRun = auto && !user;
  if (action === 'query') {
    const n = picked().length;
    // 自动刷新**不换掉结果面板**：那里面是用户刚拿到的那份清单，刷新失败时
    // 更要留着（错误由 toast + 状态行交代）。新版结果一到就整块替换。
    if (!auto){
      $('reshomebody').innerHTML = '<div class="empty">'
        + (n ? ('正在查询你勾选的 ' + n + ' 门课程…')
             : (nTop ? ('正在查询最近学习的 ' + nTop + ' 门课程…')
                     : '查询进行中，请稍候…（全量通常 1~2 分钟）')) + '</div>';
      // 工具条那句「生成时间 …」是上一份结果的时间戳，必须跟着一起翻篇：
      // 不重置的话，上面还挂着上次的「生成时间 12:34」，下面已经是「查询进行中…」，
      // 用户会以为新查询用的是旧数据（正是本工具忌讳的「旧结果冒充」）。
      clearHomeStale();
      setText($('resmeta'), '查询中…');
    }
    $('bartip').textContent = auto ? '正在刷新…' : '查询中…';
    // 查询/刷新都是「主页面」的事：切回主页面视图 + 结果页签，让用户看着总览更新。
    // 一次新查询落地就等于「上一份任务结果成了历史」：把「正在进行」按钮收掉
    // （taskHas=false）、顺手清掉任务页里上次的占位 / 错误卡，免得用户点进去
    // 看到的是过期内容（也让结果区不会同时挂着新旧两张卡）。自动刷新不动它
    // （那可能是用户正看着的任务页）。
    if (!auto){ taskHas = false; $('resTaskBody').innerHTML = ''; }
    showRes('home');
    switchTab('res');
  } else if (action === 'list') {
    $('resTaskBody').innerHTML = '<div class="empty">正在读取课程列表…（约 10 秒，不抓作业）</div>';
    $('bartip').textContent = '读取课程列表…';
    // v6.18：课程勾选清单搬进了「查询」页左列，读完后要停在查询页——否则
    // 还按老布局切到「日志」，用户看不到刚读到的名单（.cwrap 被隐藏）。
    switchTab('res');
  } else if (user && action === 'refresh_one') {
    // 单门刷新：静默进行，只更新主页面工具条那句「正在刷新…」，
    // 不动结果面板、不切页签（用户可能正看着日志或预览）。
    setText($('resmeta'), '正在刷新「' + ((opts && opts.name) || '这一门') + '」…');
    showRes('home');
  } else if (!(__DESKTOP__ && action === 'login')) {
    // 兜底的「切到日志页」。**扫码/短信登录例外**：上方已经为它切到「预览」
    // 看登录页，这里若再切回日志，刚嵌进预览的登录窗口会被 _dock_layout
    // 当成「面板没开」而隐藏——用户还是看不见登录页、键盘焦点也拿不到
    // （真机 2026-10-14：光加切换、被这里盖掉，等于没切）。
    switchTab('log');
  }
  // 先掐掉旧轮询链，否则每点一次按钮就多一条并行的轮询，日志会重复
  clearTimeout(timer);
  poll();
}

function switchTab(t){
  const wasDock = $('dockpane').style.display !== 'none';
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('on', b.dataset.t === t));
  $('viewQuery').style.display    = t === 'res' ? '' : 'none';
  $('logpane').style.display      = t === 'log' ? '' : 'none';
  $('dockpane').style.display     = t === 'dock' ? '' : 'none';
  $('viewLink').style.display     = t === 'link' ? '' : 'none';
  $('viewSettings').style.display = t === 'settings' ? '' : 'none';
  postDockRect();
  // 切到「预览」（或已在预览里点了别处又切回来）就把键盘焦点交给内嵌浏览器：
  // 否则在预览里的登录页打字会落到工具侧栏的账号框（真机 2026-10-14）。
  // 焦点接管是「尽力而为」，面板还没矩形/还没入坞时后端会安静返回 false。
  if (t === 'dock' && !wasDock) focusDockSoon();
}
// 结果页多级导航（v6.15）：切「主页面（总览）」与「任务页（进度/交卷面板）」。
// 只动 #res 内部两个子视图的显隐，不碰 switchTab 的三页签语义——多级导航只在
// 用户已经在「查询结果」页签里时才谈得上，别的页签由 switchTab 管。
function showRes(which){
  const home = which !== 'task';
  $('resHome').style.display = home ? '' : 'none';
  $('resTask').style.display = home ? 'none' : '';
  // 每次切到任务页都记一笔「这里现在有东西可看」，「正在进行」按钮据此常亮
  // （见 poll 里的 taskLive）。新一次查询开始时由 run() 把它清掉。
  if (!home) taskHas = true;
}
// 主页面「清单可能过期」的标记：任务跑完不再自动全量重查（慢），改为在主页面上
// 挂个提示，用户点课程名旁的 ↻ 单门刷新。stale=null 时按普通生成时间显示。
let homeStale = null;
function markHomeStale(n){
  homeStale = (n == null) ? true : n;
  const bar = $('resmeta').closest('.resbar');
  if (bar) bar.classList.add('stale');
  $('resmeta').textContent = (typeof homeStale === 'number')
    ? ('本次刚完成 ' + homeStale + ' 项 · 清单可能已变，点课程名旁 ↻ 刷新那一门')
    : '清单可能已变 · 点课程名旁 ↻ 刷新那一门';
}
function clearHomeStale(){
  homeStale = null;
  const bar = $('resmeta').closest('.resbar');
  if (bar) bar.classList.remove('stale');
}
// 请后端把键盘焦点交给预览里的浏览器窗口。跨进程 WS_CHILD 不 AttachThreadInput
// 就设不了焦点，所以这件事只能在原生层做（后端 /api/focus-dock）。
function focusDockSoon(){
  setTimeout(() => {
    fetch('/api/focus-dock', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:'{}'}).catch(() => {});
  }, 80);   // 让面板矩形先到后端（postDockRect 有 60ms 防抖）
}
// 在预览面板上按下鼠标 → 再要一次焦点。用户切页签后可能先点了面板空白、
// 焦点又回到工具界面；这一次点击就顺手把键盘交还给内嵌浏览器。
if ($('dockpane')) $('dockpane').addEventListener('mousedown', () => {
  if ($('dockpane').style.display !== 'none') focusDockSoon();
});

// ---------- 预览页签（内嵌浏览器） ----------
// 后端用 SetParent 把工具开出的 Chrome 挂成主窗口的子窗口，位置对齐
// #dockpane。这里只负责把面板矩形（CSS 像素）实时报给后端：页签切换 /
// 窗口缩放 / 面板自身布局变化（ResizeObserver 兜底）都触发一次推送，
// 后端按窗口 DPI 换算成物理像素。60ms 防抖：拖窗口边缘别把请求打爆。
let dockRectTimer = null;
function postDockRect(){
  const p = $('dockpane');
  if (!p) return;
  const r = p.getBoundingClientRect();
  clearTimeout(dockRectTimer);
  dockRectTimer = setTimeout(() => {
    fetch('/api/dock-rect', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({l: r.left, t: r.top, w: r.width, h: r.height,
                            on: p.style.display !== 'none' && r.width > 0
                                && r.height > 0})}).catch(() => {});
  }, 60);
}
window.addEventListener('resize', postDockRect);
if (window.ResizeObserver)
  new ResizeObserver(postDockRect).observe($('dockpane'));
postDockRect();

// ---------- 未完成作业每行的「刷题」 ----------
// 只做点中的那一份。判别 / 答题 / 交卷与任务点的「做作业并交卷」共用同一条
// 链（answer_urls → answer_one），所以行为一致：附件题整份跳过、题干读不出
// 跳过、多空填空按空序切分、同题干复用上次答案。
function answerThis(ev, i, draft){
  if (ev) ev.stopPropagation();
  const it = (lastHw || [])[i];
  if (!it || !safeUrl(it.url)){ toast('这份作业没有可用的直达链接', 'warn'); return; }
  if (!draft && !confirm('让大模型做这一份并交卷？\n\n' + it.title + '\n（' + it.course + '）'
      + '\n\n答完直接正式提交、记录成绩。要求上传附件的题会整份跳过，不会乱填。')) return;
  fetch('/api/answer-one', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({url: it.url, course: it.course, title: it.title,
                          draft: !!draft})}).then(r => {
    if (r.status === 409){ toast('当前有任务在跑，等它结束再做', 'warn'); return; }
    if (!r.ok){ toast('刷题没能启动（HTTP ' + r.status + '）', 'err'); return; }
    since = 0; logEl.innerHTML = '';
    setCardsShown(false); cardFp = '';
    autoBack = true; brushLike = true;
    $('resTaskBody').innerHTML = '<div class="empty taskph">正在后台'
      + (draft ? '刷题并暂存：' : '刷这一份：') + esc(it.title)
      + '…（进度看「运行日志」，可以随时暂停 / 停止）</div>';
    $('bartip').textContent = '刷题中…';
    showRes('task');
    switchTab('log');
    clearTimeout(timer);
    poll();
  }).catch(() => toast('连不上本地程序，请确认程序还在运行', 'err'));
}

// ---------- 课堂活动：行级作答 / 批刷课 / 刷课+刷题 ----------
// targets 用「活动 key」（cid:clsid#id），后端据此定位活动与所在课程。
// 刷课只认「能定位到课程」（cid/clsid）——任何类型都能刷它所在课的视频；
// 作答那一步后端再按类型挑（只有 42/14 能自动答，其余报「需人工」）。
function actTargets(){
  return (lastAct || []).filter(actLocatable).map(it =>
    ({cid: String(it.cid), clsid: String(it.clsid), id: String(it.id),
      type: it.type, kind: it.kind, name: it.name, course: it.course,
      cpi: it.cpi, url: it.url}));
}
function answerThisAct(ev, i, draft){
  if (ev) ev.stopPropagation();
  const it = (lastAct || [])[i];
  if (!actAnswerable(it)){
    toast(it && it.kind
      ? ('「' + it.kind + '」不是随堂练习/测验或问卷，大模型答不了，'
         + '点「去完成」自己做')
      : '这条活动定位不到作答页', 'warn');
    return;
  }
  if (!draft && !confirm('让大模型答这一条并提交？\n\n' + (it.name || '') + '\n（'
      + it.course + ' · ' + (it.kind || '活动') + '）'
      + '\n\n答完直接正式提交、记录成绩。要求上传附件的题整条跳过，不会乱填。')) return;
  startActJob('/api/answer-act', draft ? '作答并暂存' : '作答并提交',
    {targets: [it].map(x => ({cid: String(x.cid), clsid: String(x.clsid),
       id: String(x.id), type: x.type, kind: x.kind, name: x.name,
       course: x.course, cpi: x.cpi, url: x.url})), draft: !!draft}, true);
}
function startActBrush(ev){
  if (ev) ev.stopPropagation();
  const ts = actTargets();
  if (!ts.length){ toast('没有可刷的课堂活动（活动定位不到课程）', 'warn'); return; }
  const shut = !!($('sdafter') && $('sdafter').checked);
  startActJob('/api/brush-act', '刷课', {targets: ts, shutdown: shut,
    rate: brateVal}, !shut);
}
function startActCombo(ev){
  if (ev) ev.stopPropagation();
  const ts = actTargets();
  if (!ts.length){ toast('没有可刷的课堂活动（活动定位不到课程）', 'warn'); return; }
  const shut = !!($('sdafter') && $('sdafter').checked);
  // 「只暂存」必须**当场读 DOM**，不能读缓存的 actDraftOn —— 那份只有到下一次
  // 900ms 重绘才刷新；用户勾完立刻点，会拿到旧值，结果本该暂存的刷成了提交。
  // 同文件的 startActBrush 读 sdafter 也是当场读 DOM，这里对齐。
  const ad = document.getElementById('actdraft');
  const draft = ad ? !!ad.checked : !!actDraftOn;
  startActJob('/api/combo-act', '刷课+刷题',
    {targets: ts, shutdown: shut, rate: brateVal, draft: draft}, !shut);
}
// 统一的「课堂活动任务」启动：跟刷视频 / 做作业同一套收口（autoBack/brushLike、
// 交卷面板、poll 接管状态行）。back=true 时才自动回到全量查询。
function startActJob(path, label, body, back){
  fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)}).then(r => {
    if (r.status === 409){ toast('当前有任务在跑，等它结束再做', 'warn'); return; }
    if (!r.ok){ toast(label + '没能启动（HTTP ' + r.status + '）', 'err'); return; }
    since = 0; logEl.innerHTML = '';
    setCardsShown(false); cardFp = '';
    autoBack = !!back; brushLike = true;
    if (back){
      $('resTaskBody').innerHTML = '<div class="empty taskph">正在后台' + esc(label)
        + '…（进度看「运行日志」，可以随时暂停 / 停止）</div>';
    }
    $('bartip').textContent = label + '中…';
    showRes('task');
    switchTab('log');
    clearTimeout(timer);
    poll();
  }).catch(() => toast('连不上本地程序，请确认程序还在运行', 'err'));
}

// ---------- 刷题下拉菜单 ----------
// 「刷题」「做作业」都只留一个按钮，点开浮层二选一：暂存（答完存平台草稿，
// 人工审查后自己交）或提交（答完直接正式交卷）。菜单挂在 body 上，
// 不随结果区 900ms 全量重绘消失；点别处 / 滚动 / 改窗口大小即收起。
function closeBrushMenu(){
  const m = document.querySelector('.bmenu');
  if (m) m.remove();
}
function brushMenu(ev, kind, i){
  if (ev) ev.stopPropagation();
  closeBrushMenu();
  const r = (ev.currentTarget || ev.target).getBoundingClientRect();
  const m = document.createElement('div');
  m.className = 'bmenu';
  const items = kind === 'row'
    ? [['draft', '刷题并暂存', '答完存平台草稿，不交卷；人工审查后自己交'],
       ['submit', '刷题并提交', '答完直接正式提交并记录成绩']]
    : (kind === 'act'
    ? [['draft', '作答并暂存', '答完存平台草稿，不交卷；答案列出来供你核对'],
       ['submit', '作答并提交', '答完直接正式提交并记录成绩']]
    : [['draft', '做作业并暂存', '答完存平台草稿，不交卷；人工审查后自己交'],
       ['submit', '做作业并提交', '答完直接正式提交并记录成绩']]);
  for (const mi of items){
    const b = document.createElement('button');
    b.className = 'bmenu-it';
    b.innerHTML = '<span class="t">' + mi[1] + '</span>'
                + '<span class="s">' + mi[2] + '</span>';
    b.addEventListener('click', function(e2){
      closeBrushMenu();
      if (kind === 'row') answerThis(e2, i, mi[0] === 'draft');
      else if (kind === 'act') answerThisAct(e2, i, mi[0] === 'draft');
      else startAnswer(e2, mi[0] === 'draft');
    });
    m.appendChild(b);
  }
  m.style.left = Math.max(6, Math.min(r.left, window.innerWidth - 260)) + 'px';
  m.style.top = (r.bottom + 5) + 'px';
  document.body.appendChild(m);
}
document.addEventListener('click', closeBrushMenu);
document.addEventListener('scroll', closeBrushMenu, true);
window.addEventListener('resize', closeBrushMenu);

// ---------- 刷视频 / 做作业 / 刷课+刷题 ----------
// 勾选有两级（勾上即生效，不用再确认）：
//   课程勾选框 .bk   = cid:clsid        → 该课全部待完成章节；
//   章节勾选框 .ck   = cid:clsid|kid    → 只做这一个章节节点。
// 两级可以混勾，后端按「并集」处理。只有同时拿得到 cid/clsid 的课才显示
// 课程勾选框，章节还要有 kid 才显示章节勾选框——没有定位参数就不能假装能刷。
function taskKeys(){
  const ks = [...document.querySelectorAll('#res .bk:checked')].map(b => b.value).filter(Boolean);
  const kn = [...document.querySelectorAll('#res .ck:checked')].map(b => b.value).filter(Boolean);
  return ks.concat(kn);
}

function startBrush(ev){
  if (ev) ev.stopPropagation();
  const keys = taskKeys();
  if (!keys.length){
    toast('先在「未完成任务点」里勾选要刷的课程或章节', 'warn');
    return;
  }
  const shut = !!($('sdafter') && $('sdafter').checked);
  fetch('/api/brush', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({only: keys, shutdown: shut, rate: brateVal})}).then(r => {
    if (r.status === 409){ toast('当前有任务在跑，等它结束再刷', 'warn'); return; }
    if (!r.ok){ toast('刷视频没能启动（HTTP ' + r.status + '）', 'err'); return; }
    since = 0; logEl.innerHTML = '';
    setCardsShown(false); cardFp = '';
    // 要自动关机就别再重跑一次全量查询：机器马上要关，挂一个 1~2 分钟的
    // 扫描只会在关机倒计时里被掐断，白白多开一次浏览器。
    autoBack = !shut; brushLike = true;
    // 要关机时不写那句占位：任务结束后不会再重跑查询，写了就没人来换掉它，
    // 结果区会永远停在「正在后台刷视频…」——正是用户报过的
    // 「任务结束了还显示在刷题」。非关机时照写，收尾时由自动刷新顶掉。
    if (!shut){
      $('resTaskBody').innerHTML = '<div class="empty taskph">正在后台刷视频…（倍速静音真实播放，'
        + '进度看「运行日志」；可以随时暂停/停止）</div>';
    }
    $('bartip').textContent = '刷视频中…';
    showRes('task');
    switchTab('log');
    clearTimeout(timer);
    poll();
  }).catch(() => toast('连不上本地程序，请确认程序还在运行', 'err'));
}

// 刷课+刷题：先刷选中范围的视频，再对同一批范围做作业并交卷。
function startCombo(ev){
  if (ev) ev.stopPropagation();
  const keys = taskKeys();
  if (!keys.length){
    toast('先在「未完成任务点」里勾选要做的课程或章节', 'warn');
    return;
  }
  const shut = !!($('sdafter') && $('sdafter').checked);
  fetch('/api/combo', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({only: keys, shutdown: shut, rate: brateVal})}).then(r => {
    if (r.status === 409){ toast('当前有任务在跑，等它结束再做', 'warn'); return; }
    if (!r.ok){ toast('任务没能启动（HTTP ' + r.status + '）', 'err'); return; }
    since = 0; logEl.innerHTML = '';
    setCardsShown(false); cardFp = '';
    autoBack = !shut; brushLike = true;   // 同「刷视频」：要关机就别再挂一个全量查询
    if (!shut){                           // 同上：不重跑查询就别留占位
      $('resTaskBody').innerHTML = '<div class="empty taskph">正在后台「刷课+刷题」…（先刷视频，'
        + '再做作业并交卷；进度看「运行日志」，可以随时暂停/停止）</div>';
    }
    $('bartip').textContent = '刷课+刷题中…';
    showRes('task');
    switchTab('log');
    clearTimeout(timer);
    poll();
  }).catch(() => toast('连不上本地程序，请确认程序还在运行', 'err'));
}

function toggleAllBrush(ev){
  if (ev) ev.stopPropagation();
  const bks = [...document.querySelectorAll('#res .bk')];
  const allOn = bks.length && bks.every(b => b.checked);
  bks.forEach(b => b.checked = !allOn);
}

// 熄屏：强制关显示器（Chrome 播视频的 Video Wake Lock 挡不住
// 显式的 SC_MONITORPOWER），任务照常在后台跑；动下鼠标屏幕就亮。
// 万一黑屏唤不醒：按 Win+Ctrl+Shift+B 重置显卡驱动即可恢复。
function screenOff(ev){
  if (ev) ev.stopPropagation();
  fetch('/api/screen-off', {method:'POST',
    headers:{'Content-Type':'application/json'}})
    .then(r => { if (!r.ok) toast('熄屏请求失败（HTTP ' + r.status + '）', 'err'); })
    .catch(() => toast('连不上本地程序', 'err'));
}

// ---------- 大模型答题并交卷 ----------
function startAnswer(ev, draft){
  if (ev) ev.stopPropagation();
  const keys = taskKeys();
  if (!keys.length){
    toast('先在「未完成任务点」里勾选要做作业的课程或章节', 'warn');
    return;
  }
  if (!draft && !confirm('确定要「做作业并交卷」吗？\n\n大模型答完会直接正式提交并记录成绩，'
      + '交卷后一般不能再改。')) return;
  fetch('/api/answer', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({only: keys, draft: !!draft})}).then(r => {
    if (r.status === 409){ toast('当前有任务在跑，等它结束再做', 'warn'); return; }
    if (!r.ok){ toast('答题没能启动（HTTP ' + r.status + '）', 'err'); return; }
    since = 0; logEl.innerHTML = '';
    setCardsShown(false); cardFp = '';
    autoBack = true; brushLike = true;
    $('resTaskBody').innerHTML = '<div class="empty taskph">正在后台做作业…（'
      + (draft ? '答完暂存不交卷' : '答完直接交卷')
      + '；进度看「运行日志」；可以随时暂停/停止）</div>';
    $('bartip').textContent = draft ? '答题暂存中…' : '答题并交卷中…';
    showRes('task');
    switchTab('log');
    clearTimeout(timer);
    poll();
  }).catch(() => toast('连不上本地程序，请确认程序还在运行', 'err'));
}

// ---------- 交卷结果面板 ----------
// 答题/交卷任务结束后生成：每份作业列课程、节点、状态、网址。
// 「待平台确认」= 提交请求已发出但当场没核实到受理（平台批阅状态
// 可能延迟几分钟到十几分钟才翻转，实测多次）——点「重新核实」
// 逐份重开作业卡，渲染出「已批阅」页即翻案为已交卷。
// 状态 → [文案, 记号类]。颜色交给盘色变量，不在这里写死色值。
const AST = {
  done:       ['已交卷', 'ok'],
  already:    ['已交卷（此前已交）', 'ok'],
  ok:         ['已暂存 · 待审查后交', 'ok'],
  fail:       ['交卷失败', 'err'],
  unverified: ['已发出 · 待平台确认', 'warn'],
  still:      ['仍未确认', 'warn'],
  report:     ['需人工 · 要传附件', 'warn'],
  unsupported:['需人工 · 题型不支持', 'warn'],
  unsolved:   ['需人工 · 模型没把握', 'warn'],
  norender:   ['需人工 · 打开没渲染出题目', 'warn'],
  partial:    ['已暂存 · 待补完再交', 'warn'],
  skip:       ['已跳过', 'mut'],
};

// 课堂活动行由后端打 kind:'act'（它的 kid 是活动 activeId，不是作业 knowledgeid）：
// 「重新核实」按作业卡查，对活动永远核实不到，所以只对作业行生效。
function isActItem(it){ return !!(it && it.kind === 'act'); }

function renderAnswerResult(a){
  if (!a || !a.items) return;
  aitems = a.items;
  // 待核实的行：未确认的（unverified/still）+ 已暂存的课堂活动（ok）——
  // 暂存是平台草稿，你在平台上自己交了之后点「重新核实」就能翻案成已交卷。
  const uv = aitems.filter(it =>
    it.status === 'unverified' || it.status === 'still'
    || (it.status === 'ok' && isActItem(it)));
  const uvHw = uv;   // 活动/作业现在都能核（活动走答题页，作业走作业卡）
  const need = aitems.filter(it =>
    ['report','unsupported','unsolved','norender','partial','ok']
      .includes(it.status));
  const tags = [[aitems.length, '', '份']];
  if (uv.length) tags.push([uv.length, 'warn', '份待核实']);
  if (need.length) tags.push([need.length, 'warn', '份需人工']);
  let h = '<div class="sec"><h2>交卷结果'
       + tags.map(t => ' <span class="n' + (t[1] ? ' ' + t[1] : '') + '">'
                       + t[0] + '</span> ' + t[2]).join('')
       + '</h2>'
       + '<div class="meta">生成时间 ' + esc(a.time)
       + '　·　「待平台确认」= 提交已发出、平台还没翻转状态（可能延迟几分钟到十几分钟），'
       + '点「重新核实」自动翻案；也可以点开网址自己确认。</div>';
  if (uvHw.length){
    h += '<div class="brushbar"><button class="mini pri"'
       + ' onclick="verifyAnswer(event)"><span class="ic">' + icon('refresh')
       + '</span>重新核实未确认的（' + uvHw.length + ' 份）</button>'
       + '<span class="note">逐份重开查平台状态（作业查「已批阅」卡、'
       + '课堂活动查答题页），隔一分钟一轮，最多三轮，可随时停止。</span></div>';
  }
  // 有任一条带「逐题答案」就补一列把答案列出来——「暂存」的意义就是答完
  // 给你看、你核对后再自己交（作业交卷结果同样带上，一并供核对）。
  const hasAns = aitems.some(it => it.answers && Object.keys(it.answers).length);
  h += '<table><thead><tr><th>课程</th><th>项目 / 名称</th><th>状态</th>'
     + (hasAns ? '<th>逐题答案（供核对）</th>' : '')
     + '<th class="col-act">操作</th></tr></thead><tbody>';
  aitems.forEach((it) => {
    const st = AST[it.status] || [it.status, 'mut'];
    // 用显式 kind 判，别拿 kid 里有没有冒号猜：课堂活动行的 kid 就是活动
    // activeId（不含冒号），猜的话活动永远被判成作业 → 文案写成「打开作业」。
    const isAct = isActItem(it);
    let ans = '';
    if (hasAns && it.answers && Object.keys(it.answers).length){
      ans = Object.keys(it.answers).map(k =>
        '<div class="ansrow"><b>' + esc(k) + '</b>'
        + '<span>' + esc(String(it.answers[k])) + '</span></div>').join('');
    }
    h += '<tr><td>' + esc(it.course) + '</td><td>' + esc(it.node)
       + '</td><td><span class="st ' + st[1] + '">' + esc(st[0]) + '</span></td>'
       + (hasAns ? '<td class="anscell">' + (ans || '—') + '</td>' : '')
       + '<td class="col-act">'
       + (safeUrl(it.url) ? '<a href="' + safeUrl(it.url)
                            + '" class="go-work" rel="noopener">'
                            + (isAct ? '打开活动' : '打开作业') + '</a>' : '')
       + '</td></tr>';
  });
  h += '</tbody></table></div>';
  $('resTaskBody').innerHTML = h;
}

async function verifyAnswer(ev){
  if (ev) ev.stopPropagation();
  // 活动行（kind='act'）也核：后端 verify_items 对活动走答题页判「结果页=已交」。
  // 必须把 cid/clsid 一并传——活动答题页 URL 靠它们拼。
  const list = aitems.filter(it =>
    it.status === 'unverified' || it.status === 'still'
    || (it.status === 'ok' && isActItem(it)));
  if (!list.length){ toast('没有待核实的作业', 'warn'); return; }
  try{
    const r = await fetch('/api/answer-verify', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({items: list.map(it => ({
        key: it.key, kid: it.kid, course: it.course, node: it.node,
        kind: it.kind, cid: it.cid, clsid: it.clsid}))})});
    if (r.status === 409){ toast('当前有任务在跑，等它结束再核实', 'warn'); return; }
    if (!r.ok){ toast('核实没能启动（HTTP ' + r.status + '）', 'err'); return; }
    since = 0; logEl.innerHTML = '';
    $('bartip').textContent = '重新核实中…';
    switchTab('log');
    clearTimeout(timer);
    poll();
  }catch(e){
    toast('连不上本地程序，请确认程序还在运行', 'err');
  }
}

// ---------- 模型接入 ----------
// 平台预设表由后端 /api/llm-config 下发（LLM_PLATFORMS 是唯一真相源），
// 这里不复制一份，免得前后端两份漂移。
// 状态行的取值：ready / 未配全 / 正在测 / 测失败，四种都写明「下一步做什么」。
let llmPlatforms = [], llmKeySet = false, llmStateMask = '';

function llmForm(){
  // llm_key 留空 = 不动已保存的那把（后端也是这样理解的）
  return {llm_platform: $('llm_platform').value,
          url: $('llm_url').value.trim(),
          model: $('llm_model').value.trim(),
          llm_key: $('llm_key').value.trim()};
}

function llmSetState(kind, text){
  const el = $('llmstate');
  el.className = 'mstat' + (kind ? ' ' + kind : '');
  el.innerHTML = '<span class="ic">' + icon(kind === 'err' ? 'alert'
                : kind === 'ok' ? 'check' : kind === 'warn' ? 'alert' : 'plug')
                + '</span><span>' + esc(text) + '</span>';
}

function llmPlatformOf(key){
  return llmPlatforms.find(p => p.key === key) || llmPlatforms[0] || null;
}

function llmRefreshState(){
  const url = $('llm_url').value.trim(), model = $('llm_model').value.trim();
  const typed = $('llm_key').value.trim();
  const hasKey = !!typed || llmKeySet;
  if (url && model && hasKey){
    const p = llmPlatformOf($('llm_platform').value);
    llmSetState('ok', '已配置 · ' + (p ? p.label : '自定义') + ' · ' + model
      + (typed ? '（Key 待保存）' : ''));
  } else {
    const miss = [];
    if (!url) miss.push('接口地址');
    if (!model) miss.push('模型名');
    if (!hasKey) miss.push('API Key');
    llmSetState('warn', '还没配全：还差' + miss.join('、')
      + '。填好后点「保存」，可以先点「测试连接」确认通。');
  }
  $('llm_key').placeholder = llmKeySet
    ? ('已保存 ' + (llmStateMask || '') + '　留空表示不改动')
    : 'API Key（sk- 开头的那串）';
}

// 选平台 → 带出该平台的地址与常用模型名（自定义平台留给用户自己填）
$('llm_platform').onchange = () => {
  const p = llmPlatformOf($('llm_platform').value);
  if (p && p.url){
    $('llm_url').value = p.url;
    $('llm_model').value = p.model || '';
  }
  llmRefreshState();
};
$('llm_url').oninput = llmRefreshState;
$('llm_model').oninput = llmRefreshState;
$('llm_key').oninput = llmRefreshState;

// 「保存」：合并了原来的「保存」与「记住模型信息」——本机 config.json 本来就是
// 持久保存，两个按钮只差一句提示语，纯属让人多按一次。
$('bllm').onclick = () => {
  const b = $('bllm'), old = b.innerHTML;
  b.disabled = true;
  b.innerHTML = '<span class="ic">' + icon('check') + '</span>保存中…';
  fetch('/api/llm-config', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(llmForm())}).then(async r => {
    const j = await r.json().catch(() => ({}));
    if (!r.ok){
      llmSetState('err', '保存失败：' + (j.error || ('HTTP ' + r.status)));
      toast('保存失败：' + (j.error || ('HTTP ' + r.status)), 'err');
      return;
    }
    llmKeySet = !!j.key_set;
    llmStateMask = j.key_mask || '';
    $('llm_key').value = '';                 // 存好了就把明文从输入框里撤掉
    llmRefreshState();
    llmSetState('ok', '已保存到本机：' + ($('llm_model').value.trim() || '')
      + '　·　以后打开这台电脑不用再填');
    toast('模型设置已保存', 'ok');
  }).catch(() => {
    llmSetState('err', '连不上本地程序，请确认程序还在运行');
    toast('连不上本地程序', 'err');
  }).finally(() => {
    b.disabled = false; b.innerHTML = old;
  });
};

$('bllmtest').onclick = () => {
  const b = $('bllmtest'), old = b.innerHTML;
  b.disabled = true;
  b.innerHTML = '<span class="ic">' + icon('bolt') + '</span>连接中…';
  llmSetState('', '正在连接…（最长约 30 秒）');
  fetch('/api/llm-test', {method:'POST', headers:{'Content-Type':'application/json'},
    // 后端主模型分支读的是 llm_url/llm_model（视觉分支同款）。以前发
    // llmForm()（键名是 url/model）后端根本读不到 —— 界面填了也白填，
    // 测的一直是磁盘上的旧地址（v4.0 二审发现）。
    body: JSON.stringify({
      llm_url: $('llm_url').value.trim(),
      llm_model: $('llm_model').value.trim(),
      llm_key: $('llm_key').value.trim()})}).then(async r => {
    const j = await r.json().catch(() => ({}));
    if (r.ok){
      llmSetState('ok', '连接正常，模型回了：' + (j.reply || '（空）'));
      toast('连接正常（' + ($('llm_model').value.trim() || '') + '）', 'ok');
    } else {
      // 后端的中文错误分类直接透出来：哪一步错了、怎么改，都在这句话里
      llmSetState('err', j.error || ('连接失败（HTTP ' + r.status + '）'));
      toast('连接失败：' + (j.error || ('HTTP ' + r.status)), 'err');
    }
  }).catch(() => {
    llmSetState('err', '连不上本地程序，请确认程序还在运行');
    toast('连不上本地程序', 'err');
  }).finally(() => {
    b.disabled = false; b.innerHTML = old;
  });
};

$('bllmclear').onclick = () => {
  if (!llmKeySet && !$('llm_key').value.trim()){ toast('本来就没有保存过 Key', 'warn'); return; }
  fetch('/api/llm-config', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({clear_key: true})}).then(async r => {
    const j = await r.json().catch(() => ({}));
    if (!r.ok){ toast('清除失败（HTTP ' + r.status + '）', 'err'); return; }
    llmKeySet = !!j.key_set;
    llmStateMask = j.key_mask || '';
    $('llm_key').value = '';
    llmRefreshState();
    toast('已清除本机保存的 API Key', 'ok');
  }).catch(() => toast('连不上本地程序', 'err'));
};

// ---------- 视觉模型（可选） ----------
// 与主模型完全独立的一套配置：地址 / 模型名 / Key 各存各的，互不覆盖。
// 四项全空 = 不启用视觉，答题照旧走纯文本（与之前版本行为一致）。
let visKeySet = false, visStateMask = '';

function visForm(){
  // vis_key 留空 = 不动已保存的那把（后端同样这样理解）
  return {platform: $('vis_platform').value,
          url: $('vis_url').value.trim(),
          model: $('vis_model').value.trim(),
          llm_key: $('vis_key').value.trim()};
}

function visSetState(kind, text){
  const el = $('visstate');
  el.className = 'mstat' + (kind ? ' ' + kind : '');
  el.innerHTML = '<span class="ic">' + icon(kind === 'err' ? 'alert'
                : kind === 'ok' ? 'check' : kind === 'warn' ? 'alert' : 'plug')
                + '</span><span>' + esc(text) + '</span>';
}

function visBadge(on, text){
  const el = $('visbadge');
  if (!el) return;
  el.textContent = text || (on ? '已启用' : '未启用');
  el.className = 'visbadge' + (on ? ' on' : '');
}

function visRefreshState(){
  const url = $('vis_url').value.trim(), model = $('vis_model').value.trim();
  const typed = $('vis_key').value.trim();
  const hasKey = !!typed || visKeySet;
  const p = llmPlatformOf($('vis_platform').value);
  // 「这家平台没有视觉型号」比通用的「还差模型名」具体得多，而且可行动。
  // 必须在这里按状态派生，不能只在 onchange 里喊一句：onchange 末尾还要调
  // 本函数，那一句会被下面的通用提示立刻盖掉 —— 实测选 DeepSeek 只看到
  // 「还差模型名、API Key」，用户会去手填一个不存在的模型名。
  const noVisModel = (p && p.url && !p.vision_model && !model);
  if (!url && !model && !hasKey){
    visSetState('', '未启用 —— 题面是图片 / 公式的题会答不上，其余题不受影响。');
    visBadge(false, '未启用');
  } else if (url && model && hasKey){
    visSetState('ok', '已配置 · ' + (p ? p.label : '自定义') + ' · ' + model
      + (typed ? '（Key 待保存）' : ''));
    visBadge(!typed, typed ? '待保存' : '已启用');
  } else if (noVisModel){
    visSetState('warn', p.label + ' 目前没有视觉模型 —— 换一家'
      + '（通义千问 / 智谱 / OpenAI），或自己把模型名填进去。');
    visBadge(false, '待配置');
  } else {
    const miss = [];
    if (!url) miss.push('接口地址');
    if (!model) miss.push('模型名');
    if (!hasKey) miss.push('API Key');
    visSetState('warn', '还差' + miss.join('、') + ' —— 三项都填上才生效。'
      + '不想用就保持全空。');
    visBadge(false, '待配置');
  }
  $('vis_key').placeholder = visKeySet
    ? ('已保存 ' + (visStateMask || '') + '　留空表示不改动')
    : 'API Key（可以跟主模型用同一把）';
}

$('vis_platform').onchange = () => {
  const p = llmPlatformOf($('vis_platform').value);
  if (p && p.url){
    $('vis_url').value = p.url;
    $('vis_model').value = p.vision_model || '';
  }
  visRefreshState();      // 「这家没有视觉模型」的提示由它按当前状态派生
};
$('vis_url').oninput = visRefreshState;
$('vis_model').oninput = visRefreshState;
$('vis_key').oninput = visRefreshState;

$('bvis').onclick = () => {
  const b = $('bvis'), old = b.innerHTML;
  b.disabled = true;
  b.innerHTML = '<span class="ic">' + icon('check') + '</span>保存中…';
  fetch('/api/llm-config', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({vision: visForm()})}).then(async r => {
    const j = await r.json().catch(() => ({}));
    if (!r.ok){
      visSetState('err', '保存失败：' + (j.error || ('HTTP ' + r.status)));
      toast('视觉模型保存失败：' + (j.error || ('HTTP ' + r.status)), 'err');
      return;
    }
    visKeySet = !!j.vision_key_set;
    visStateMask = j.vision_key_mask || '';
    $('vis_key').value = '';                 // 存好了就把明文从输入框里撤掉
    visRefreshState();
    toast('视觉模型设置已保存', 'ok');
  }).catch(() => {
    visSetState('err', '连不上本地程序，请确认程序还在运行');
    toast('连不上本地程序', 'err');
  }).finally(() => {
    b.disabled = false; b.innerHTML = old;
  });
};

$('bvistest').onclick = () => {
  const b = $('bvistest'), old = b.innerHTML;
  b.disabled = true;
  b.innerHTML = '<span class="ic">' + icon('bolt') + '</span>连接中…';
  visSetState('', '正在连接…（最长约 30 秒）');
  fetch('/api/llm-test', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({vision: true,
                          llm_url: $('vis_url').value.trim(),
                          llm_model: $('vis_model').value.trim(),
                          llm_key: $('vis_key').value.trim()})}).then(async r => {
    const j = await r.json().catch(() => ({}));
    if (r.ok){
      visSetState('ok', '连接正常，模型回了：' + (j.reply || '（空）'));
      toast('视觉模型连接正常', 'ok');
    } else {
      visSetState('err', j.error || ('连接失败（HTTP ' + r.status + '）'));
      toast('视觉模型连接失败：' + (j.error || ('HTTP ' + r.status)), 'err');
    }
  }).catch(() => {
    visSetState('err', '连不上本地程序，请确认程序还在运行');
    toast('连不上本地程序', 'err');
  }).finally(() => {
    b.disabled = false; b.innerHTML = old;
  });
};

$('bvisclear').onclick = () => {
  if (!visKeySet && !$('vis_key').value.trim()){ toast('本来就没有保存过视觉模型的 Key', 'warn'); return; }
  fetch('/api/llm-config', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({vision: {clear_key: true}})}).then(async r => {
    const j = await r.json().catch(() => ({}));
    if (!r.ok){ toast('清除失败（HTTP ' + r.status + '）', 'err'); return; }
    visKeySet = !!j.vision_key_set;
    visStateMask = j.vision_key_mask || '';
    $('vis_key').value = '';
    visRefreshState();
    toast('已清除本机保存的视觉模型 Key', 'ok');
  }).catch(() => toast('连不上本地程序', 'err'));
};

// 「复制上面主模型的配置」：把主模型的平台 / 地址 / 模型名搬到视觉这一块，
// Key 交给后端在服务端一起搬（页面拿不到主模型 Key 的明文，不回显是纪律）。
// 搬完立刻保存，用户点一下就不用再手打一遍。
$('bvisfrommain').onclick = () => {
  const url = $('llm_url').value.trim(), model = $('llm_model').value.trim();
  if (!url && !model){
    toast('上面主模型还没填，先配好主模型再复制', 'warn');
    return;
  }
  const plat = $('llm_platform').value;
  if (plat) $('vis_platform').value = plat;
  $('vis_url').value = url;
  $('vis_model').value = model;
  const b = $('bvisfrommain'), old = b.innerHTML;
  b.disabled = true;
  b.innerHTML = '<span class="ic">' + icon('copy') + '</span>复制中…';
  fetch('/api/llm-config', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({vision: {copy_main: true}})}).then(async r => {
    const j = await r.json().catch(() => ({}));
    if (!r.ok){
      visSetState('err', '复制失败：' + (j.error || ('HTTP ' + r.status)));
      toast('复制失败：' + (j.error || ('HTTP ' + r.status)), 'err');
      return;
    }
    // Key 在服务端搬好了：本地记下「已保存」，占位符跟着更新，
    // 输入框保持空白（不回显 Key）。
    visKeySet = !!j.vision_key_set;
    visStateMask = j.vision_key_mask || '';
    $('vis_key').value = '';
    visRefreshState();
    toast('已把主模型的配置复制到视觉模型', 'ok');
  }).catch(() => {
    visSetState('err', '连不上本地程序，请确认程序还在运行');
    toast('连不上本地程序', 'err');
  }).finally(() => {
    b.disabled = false; b.innerHTML = old;
  });
};

// 打开页面就把已有配置带出来。**Key 不回显**：后端只给「有没有存」和脱敏串，
// 页面上拿不到完整 Key（少一条泄露路径：截图求助、浏览器扩展抓 DOM 都拿不到）。
fetch('/api/llm-config').then(r => r.json()).then(c => {
  llmPlatforms = c.platforms || [];
  const sel = $('llm_platform');
  sel.innerHTML = llmPlatforms.map(p =>
    '<option value="' + esc(p.key) + '"'
    + (p.key === c.llm_platform ? ' selected' : '') + '>' + esc(p.label)
    + '</option>').join('');
  $('llm_url').value = c.llm_url || '';
  $('llm_model').value = c.llm_model || '';
  llmKeySet = !!c.key_set;
  llmStateMask = c.key_mask || '';
  llmRefreshState();
  // 视觉模型（可选）：同一份平台列表，但默认模型名取该平台的视觉型号
  const vsel = $('vis_platform');
  vsel.innerHTML = llmPlatforms.map(p =>
    '<option value="' + esc(p.key) + '"'
    + (p.key === c.vision_platform ? ' selected' : '') + '>' + esc(p.label)
    + '</option>').join('');
  $('vis_url').value = c.vision_url || '';
  $('vis_model').value = c.vision_model || '';
  visKeySet = !!c.vision_key_set;
  visStateMask = c.vision_key_mask || '';
  visRefreshState();
  // 配过就默认展开，省得用户以为「我明明填过」——收起时状态行也看得见，
  // 但展开才能改
  if (c.vision_ready || c.vision_url || c.vision_model){
    const vb = document.querySelector('details.visbox');
    if (vb) vb.open = true;
  }
  if (c.brush_rate !== undefined && c.brush_rate !== null && c.brush_rate !== ''){
    brateVal = String(c.brush_rate);
    // 结果区可能已经按默认值画出了一个下拉：不同步它，下一次重绘的
    // 「重绘前读值」会把旧选择读回去，回填就被冲掉了（测试抓到的竞态）。
    const bs = document.getElementById('brate');
    if (bs) bs.value = brateVal;
  }
}).catch(() => llmSetState('err', '读不到本机配置（后台程序可能已经退出）'));

document.querySelectorAll('.tab').forEach(b => b.onclick = () => switchTab(b.dataset.t));

// 静态 HTML 里的图标占位（左侧按钮、状态记号等）统一在启动时填一次。
paintIcons();

$('bquery').onclick = () => {
  const ph = $('phone').value.trim(), pw = $('pwd').value;
  // 不再用 alert 硬拦：没填密码时后端会自动改用本机已保存的会话。
  // （alert 还可能被浏览器「阻止此页面创建更多对话框」静默吞掉，表现为点了没反应）
  if (!ph && !pw) toast('没填账号密码，将尝试使用本机已保存的登录会话', 'warn');
  else if (ph && !pw) toast('未填密码，将使用本机已保存的登录会话', 'warn');
  run('query');
};
$('blogin').onclick = () => run('login');
$('blist').onclick  = () => {
  const ph = $('phone').value.trim(), pw = $('pwd').value;
  if (!ph && !pw) toast('没填账号密码，将尝试使用本机已保存的登录会话', 'warn');
  run('list');
};

// ---------- 上传链接刷课刷题 ----------
// 把粘贴框里的链接直接发给后端；后端按类型分流到 brush_by_urls /
// answer_urls。不写占位文案到结果区（任务结束会自动刷新结果），
// 但日志页签切过去让用户看到进度。
function startLinkJob(path, label){
  const raw = ($('linkbox') && $('linkbox').value || '').trim();
  if (!raw){ toast('先把学习通页面链接粘到上面的框里', 'warn'); return; }
  const shut = !!($('sdafter') && $('sdafter').checked);
  const draft = !!($('linkdraft') && $('linkdraft').checked);
  if (path === '/api/answer-urls' && !draft &&
      !confirm('确定要「上传链接刷题」吗？\n\n作业链接会答完直接正式提交并记录成绩，'
             + '交卷后一般不能再改。要只暂存就勾上下面的「只暂存」再点。')) return;
  fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({links: raw, shutdown: shut, draft: draft})}).then(r => {
    if (r.status === 409){ toast('当前有任务在跑，等它结束再做', 'warn'); return; }
    if (!r.ok){
      return r.json().then(j => toast(j.error || ('没能启动（HTTP ' + r.status + '）'), 'err'))
              .catch(() => toast('没能启动（HTTP ' + r.status + '）', 'err'));
    }
    since = 0; logEl.innerHTML = '';
    setCardsShown(false); cardFp = '';
    autoBack = !shut; brushLike = true;
    $('bartip').textContent = label + '中…';
    switchTab('log');
    clearTimeout(timer);
    poll();
  }).catch(() => toast('连不上本地程序，请确认程序还在运行', 'err'));
}
if ($('blinkbrush')){
  $('blinkbrush').onclick = () => startLinkJob('/api/brush-urls', '刷课');
  $('blinkanswer').onclick = () => startLinkJob('/api/answer-urls', '刷题');
  $('blinkcombo').onclick = () => startLinkJob('/api/combo-urls', '刷课+刷题');
}
$('call').onclick = () => {
  // 只作用于「当前显示出来」的项，配合筛选就能批量选中某一类课
  document.querySelectorAll('#clist label').forEach(lb => {
    if (lb.style.display !== 'none') lb.querySelector('input').checked = true;
  });
  onSelChange();
};
$('cnone').onclick = () => {
  document.querySelectorAll('#clist label').forEach(lb => {
    if (lb.style.display !== 'none') lb.querySelector('input').checked = false;
  });
  onSelChange();
};
function applyFilter(){
  const q = $('cfilter').value.trim().toLowerCase();
  document.querySelectorAll('#clist label').forEach(lb => {
    const hit = !q || (lb.dataset.name || '').toLowerCase().includes(q);
    lb.style.display = hit ? '' : 'none';
  });
}
$('cfilter').oninput = applyFilter;
$('cpin').onclick = pinPicked;
// 上方「只查最近 N 门课」输入框：改变即刷新勾选面板的文案，回车直接开查
$('recentTop').oninput = updateCount;
$('recentTop').onkeydown = e => { if (e.key === 'Enter') $('bquery').click(); };
$('bclear').onclick = () => { logEl.innerHTML = ''; };
// 结果页多级导航（v6.15）：任务页「返回主页面」、主页面「正在进行」、主页面「刷新课程列表」。
$('bback').onclick = () => showRes('home');
$('bdoing').onclick = () => showRes('task');
// 「刷新课程列表」：重新读取个人空间的课程列表（课程有增删时的兜底）。
// 只刷新某一门课程的状态请点那行课程名旁的 ↻（单门刷新，秒级）。
$('brefresh').onclick = () => {
  toast('重新读取课程列表…（约 10 秒，不抓作业）', 'ok');
  run('list');
};
// 熄屏：常驻在任务工具条上，任务跑着随时能点（Chrome 的 Video Wake Lock 挡不住
// 显式 SC_MONITORPOWER），动下鼠标屏幕就亮，再点一下即可。
// 万一黑屏唤不醒：按 Win+Ctrl+Shift+B 重置显卡驱动即可恢复，不必强制关机。
$('boff').onclick = () => {
  fetch('/api/screen-off', {method:'POST',
    headers:{'Content-Type':'application/json'}})
    .then(r => { if (!r.ok) toast('熄屏请求失败（HTTP ' + r.status + '）', 'err'); })
    .catch(() => toast('连不上本地程序', 'err'));
};
// 密码 / API Key 的小眼睛：点一下明文核对，再点一下隐藏（不改变输入内容）。
// 用 data-for 指定目标输入框，同一个处理函数给两个按钮用。
document.querySelectorAll('.ic-in[data-for]').forEach(btn => {
  btn.innerHTML = icon('eye');
  btn.onclick = () => {
    const inp = $(btn.dataset.for);
    if (!inp) return;
    const show = inp.type === 'password';
    inp.type = show ? 'text' : 'password';
    btn.setAttribute('aria-pressed', show ? 'true' : 'false');
    btn.innerHTML = icon(show ? 'eyeoff' : 'eye');
    inp.focus();
  };
});
// 「去完成」不再走系统默认浏览器（那边没有登录态），改请后端用
// 工具自己的浏览器开——点开即已登录。失败时把原因告诉用户。
document.addEventListener('click', function (ev) {
  const a = ev.target && ev.target.closest
    ? ev.target.closest('a.go-work') : null;
  if (!a) return;
  ev.preventDefault();
  fetch('/api/open-work', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({url: a.getAttribute('href')})})
    .then(r => r.json().then(j => ({ok: r.ok, j})))
    .then(({ok, j}) => { if (!ok) alert(j.error || '打开失败'); })
    .catch(() => alert('打开失败：后台服务未响应'));
});
$('bopen').onclick  = () => fetch('/api/open-report', {method:'POST',
      headers:{'Content-Type':'application/json'}});
$('bdir').onclick   = () => fetch('/api/open-dir', {method:'POST',
      headers:{'Content-Type':'application/json'}});
// 退出登录（换号）：清本机会话 + 归档 profile，下次查询才重新登录。登录态默认
// 常驻（v6.15），这是唯一的「我要换号」出口，所以要二次确认，避免误点把自己的
// 登录态清了。
$('blogout').onclick = async () => {
  if (!confirm('退出登录？\n\n本机保存的登录会话会被清除，下次查询需要重新登录'
             + '（换号就点这个）。')) return;
  $('blogout').disabled = true;
  try {
    const r = await fetch('/api/logout', {method:'POST',
      headers:{'Content-Type':'application/json'}});
    const j = await r.json().catch(() => ({}));
    toast(j.ok ? '已退出登录，下次查询请重新登录' : '退出登录未完全成功，详见日志',
          j.ok ? 'ok' : 'err');
  } catch(e){ toast('退出登录请求失败：' + e, 'err'); }
  $('blogout').disabled = false;
};
$('bpause').onclick = () => {
  const on = $('bpause').dataset.paused === '1';
  fetch(on ? '/api/resume' : '/api/pause', {method:'POST',
      headers:{'Content-Type':'application/json'}});
};
$('bstop').onclick = () => {
  if (!confirm('停止当前任务？\n\n查询：已扫完的课程会照常出报告，只是标注为「中止扫描」。\n'
               + '刷视频：已刷完的不受影响，没刷完的保持原样。'
               + '\n停止后有几秒收尾时间，这几秒里不能开始新任务。')) return;
  fetch('/api/cancel', {method:'POST',
      headers:{'Content-Type':'application/json'}});
};

// ================= v6.18 前端改版：主题外观 / 两个开关 / 检查更新 / 导出日志 =========
(function(){
  const LS = window.localStorage;
  const lsGet = (k, d) => { try { const v = LS.getItem(k); return v == null ? d : v; }
                            catch(e){ return d; } };
  const lsSet = (k, v) => { try { LS.setItem(k, v); } catch(e){} };

  // ---- 主题色派生（不引入完整 HCT：由种子色 tint/shade 出主色三态即可）----
  function hex2rgb(h){
    h = String(h||'').replace('#','').trim();
    if (h.length === 3) h = h.split('').map(c => c + c).join('');
    if (!/^[0-9a-fA-F]{6}$/.test(h)) return [65,95,145];
    const n = parseInt(h, 16);
    return [(n>>16)&255, (n>>8)&255, n&255];
  }
  function rgb2hex(r,g,b){
    const t = v => ('0'+Math.max(0,Math.min(255,Math.round(v))).toString(16)).slice(-2);
    return '#'+t(r)+t(g)+t(b);
  }
  function rgb2hsl(r,g,b){
    r/=255; g/=255; b/=255;
    const mx=Math.max(r,g,b), mn=Math.min(r,g,b), l=(mx+mn)/2;
    let h=0, s=0;
    if (mx!==mn){
      const d=mx-mn; s=l>0.5 ? d/(2-mx-mn) : d/(mx+mn);
      if (mx===r) h=((g-b)/d+(g<b?6:0)); else if (mx===g) h=(b-r)/d+2; else h=(r-g)/d+4;
      h*=60;
    }
    return [h, s, l];
  }
  function hsl2hex(h,s,l){
    const c=(1-Math.abs(2*l-1))*s, x=c*(1-Math.abs((h/60)%2-1)), m=l-c/2;
    let r,g,b;
    if (h<60){r=c;g=x;b=0;} else if (h<120){r=x;g=c;b=0;}
    else if (h<180){r=0;g=c;b=x;} else if (h<240){r=0;g=x;b=c;}
    else if (h<300){r=x;g=0;b=c;} else {r=c;g=0;b=x;}
    return rgb2hex((r+m)*255,(g+m)*255,(b+m)*255);
  }
  function lum(hex){ const c = hex2rgb(hex); return (0.299*c[0]+0.587*c[1]+0.114*c[2])/255; }

  // 由种子色派生主色：保留色相，按明暗调「明度 + 饱和度下限」，
  // 观感贴近云析 Material3（深色用高亮度主色、浅色用种子本色）。
  function applySeed(seed, dark){
    const rgbv = hex2rgb(seed);
    const [h, s0] = rgb2hsl(rgbv[0], rgbv[1], rgbv[2]);
    const s = Math.max(s0, dark ? 0.70 : 0.42);
    const ac   = dark ? hsl2hex(h, s, 0.78) : hsl2hex(h, s, Math.min(0.42, Math.max(s0, 0.36)));
    const acHi = dark ? hsl2hex(h, s, 0.88) : hsl2hex(h, s, 0.32);
    const on   = lum(ac) > 0.6 ? '#0a1220' : '#ffffff';
    const st = document.documentElement.style;
    st.setProperty('--ac', ac);
    st.setProperty('--ac-hi', acHi);
    st.setProperty('--ac-on', on);
    st.setProperty('--ac-soft',
      'rgba(' + rgbv[0] + ',' + rgbv[1] + ',' + rgbv[2] + ',' + (dark ? 0.18 : 0.12) + ')');
  }
  const mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme:dark)') : null;
  function effectiveDark(mode){
    if (mode === 'light') return false;
    if (mode === 'dark') return true;
    return mq ? mq.matches : true;
  }
  function applyTheme(){
    const mode = lsGet('cx_theme', 'dark');
    const seed = lsGet('cx_seed', '#415f91');
    const dark = effectiveDark(mode);
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
    applySeed(seed, dark);
  }
  window.__applyTheme = applyTheme;

  function applyGuides(){
    const on = lsGet('cx_guides', '1') === '1';
    document.body.classList.toggle('guides-off', !on);
    const sw = document.getElementById('swGuide');
    if (sw) sw.classList.toggle('on', on);
  }
  function applyKeepAwake(){
    const on = lsGet('cx_keep', '1') === '1';
    const sw = document.getElementById('swKeep');
    if (sw) sw.classList.toggle('on', on);
  }

  // ---- 外观：明暗模式 ----
  document.querySelectorAll('#themeMode button').forEach(b => {
    b.onclick = () => {
      lsSet('cx_theme', b.dataset.v);
      document.querySelectorAll('#themeMode button')
        .forEach(x => x.classList.toggle('on', x === b));
      applyTheme();
    };
  });
  // ---- 外观：主题色圆点 + 自定义 ----
  function markSwatch(cur){
    document.querySelectorAll('#themeSeed .swatch').forEach(x =>
      x.classList.toggle('on', (x.dataset.v || '').toLowerCase() === (cur||'').toLowerCase()));
  }
  document.querySelectorAll('#themeSeed .swatch').forEach(b => {
    b.onclick = () => { lsSet('cx_seed', b.dataset.v); markSwatch(b.dataset.v);
                        const hx = document.getElementById('seedHex');
                        if (hx) hx.value = b.dataset.v; applyTheme(); };
  });
  const seedHex = document.getElementById('seedHex');
  if (seedHex) seedHex.oninput = e => {
    const v = String(e.target.value || '').trim();
    if (/^#?[0-9a-fA-F]{6}$/.test(v)){
      const h = v[0] === '#' ? v : ('#' + v);
      lsSet('cx_seed', h); markSwatch(h); applyTheme();
    }
  };

  // ---- 两个开关 ----
  const swGuide = document.getElementById('swGuide');
  if (swGuide) swGuide.onclick = () => {
    lsSet('cx_guides', lsGet('cx_guides', '1') === '1' ? '0' : '1'); applyGuides();
  };
  const swKeep = document.getElementById('swKeep');
  if (swKeep) swKeep.onclick = () => {
    const on = lsGet('cx_keep', '1') !== '1';
    lsSet('cx_keep', on ? '1' : '0'); applyKeepAwake();
    fetch('/api/llm-config', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({keep_awake: on})
    }).then(r => r.ok ? null : toast('「阻止休眠」偏好没存上，详见运行日志', 'warn'))
      .catch(() => {});
  };

  // ---- 检查更新（只提示 + 给下载链接，不自动安装）----
  const bChk = document.getElementById('bcheckupd');
  if (bChk) bChk.onclick = async () => {
    bChk.disabled = true;
    toast('正在检查更新…', 'ok');
    try{
      const r = await fetch('/api/check-update', {method:'POST',
        headers:{'Content-Type':'application/json'}, body:'{}'});
      const j = await r.json();
      if (!j.ok){ toast('检查更新失败：' + (j.error || '网络不可用'), 'err'); }
      else if (j.newer){
        toast('发现新版本 ' + j.latest + '（当前 ' + j.current + '）', 'ok');
        if (j.url && confirm('发现新版本 ' + j.latest + '（当前 ' + j.current + '）'
              + '\n\n点「确定」用浏览器打开下载页面。')){
          fetch('/api/open', {method:'POST',
            headers:{'Content-Type':'application/json'},
            body: JSON.stringify({url: j.url})}).catch(() => {});
        }
      } else { toast('已是最新版本 ' + j.current, 'ok'); }
    }catch(e){ toast('检查更新失败：连不上本地程序', 'err'); }
    bChk.disabled = false;
  };

  // ---- 导出日志 ----
  const bExp = document.getElementById('bexportlog');
  if (bExp) bExp.onclick = async () => {
    bExp.disabled = true;
    try{
      const r = await fetch('/api/export-log', {method:'POST',
        headers:{'Content-Type':'application/json'}, body:'{}'});
      const j = await r.json();
      toast(j.ok ? ('日志已导出：' + j.path) : ('导出失败：' + (j.error || '')),
            j.ok ? 'ok' : 'err');
    }catch(e){ toast('导出失败：连不上本地程序', 'err'); }
    bExp.disabled = false;
  };

  // ---- 初始化（读真实偏好，覆盖 CSS 兜底）----
  applyTheme();
  document.querySelectorAll('#themeMode button').forEach(b =>
    b.classList.toggle('on', lsGet('cx_theme', 'dark') === b.dataset.v));
  markSwatch(lsGet('cx_seed', '#415f91'));
  if (seedHex) seedHex.value = lsGet('cx_seed', '#415f91');
  applyGuides();
  applyKeepAwake();
  if (mq && mq.addEventListener)
    mq.addEventListener('change', () => { if (lsGet('cx_theme', 'dark') === 'system') applyTheme(); });
  // 阻止休眠以服务端配置为准，回读一次覆盖本地镜像
  fetch('/api/llm-config').then(r => r.json()).then(j => {
    if (typeof j.keep_awake === 'boolean'){
      lsSet('cx_keep', j.keep_awake ? '1' : '0'); applyKeepAwake();
    }
  }).catch(() => {});
})();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = 'ChaoxingScanUI/2.0'

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype='application/json; charset=utf-8'):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _local_only(self):
        """只服务本机浏览器：Host 头必须是 127.0.0.1/localhost。

        只绑 127.0.0.1 挡不住浏览器发起的跨源请求：恶意网页可以把
        自己的域名解析到 127.0.0.1（DNS rebinding），同源读到
        /api/llm-config 里的 API Key、或用表单 POST 触发退出/扫描。
        校验 Host 一举挡死 rebinding；写接口再校验 Content-Type
        挡掉无 JSON 头的表单 CSRF。
        """
        host = (self.headers.get('Host') or '').strip().lower()
        ok = (host.startswith('127.0.0.1:') or host.startswith('localhost:')
              or host in ('127.0.0.1', 'localhost'))
        return ok

    def do_GET(self):
        if not self._local_only():
            return self._send(403, b'{"error":"forbidden"}')
        u = urlparse(self.path)
        if u.path in ('/', '/index.html'):
            # PAGE 是 raw 字符串，塞不进 f-string；版本号用占位符在这里替换，
            # 免得版本改了界面还挂着旧号（用户报问题时说不清自己用的哪版）
            html = PAGE.replace('__VERSION__', cs.VERSION).replace(
                '__DESKTOP__', 'true' if DESKTOP['mode'] else 'false')
            return self._send(200, html.encode('utf-8'), 'text/html; charset=utf-8')
        if u.path == '/api/pwd-get':
            # 启动回填用：有没有存过 + 明文一次（DPAPI 只认本机本用户，
            # 且只走本机回环；界面拿到后只进密码框，不写日志）。
            return self._send(200, json.dumps({
                'has': cs.has_login_pwd(),
                'pwd': cs.load_login_pwd()}).encode('utf-8'))
        if u.path == '/api/poll':
            q = parse_qs(u.query)
            try:
                since = int((q.get('since') or ['0'])[0] or 0)
                rv = int((q.get('rv') or ['-1'])[0] or -1)
                av = int((q.get('av') or ['-1'])[0] or -1)
                cvq = int((q.get('cv') or ['-1'])[0] or -1)
            except ValueError:
                # 畸形参数按缺省处理：不给响应会让 socketserver 往
                # 黑色控制台刷 traceback，可被脚本刷屏
                since, rv, av, cvq = 0, -1, -1, -1
            # 本机这份会话属于哪个账号。以前界面对此只字不提，用户很容易
            # 「以为在查自己的号，其实查的是上一个登录过的号」（换号时最
            # 容易踩）。每次轮询现读文件，保证显示的和实际用的一致——读不
            # 到就当没有，不猜。
            try:
                who = cs.session_label() if cs.STATE_FILE.exists() else ''
            except Exception:
                who = ''
            # dock_count() 自己也要拿 LOCK——绝不能在下面 with LOCK: 块里调，
            # 否则 Lock 不可重入 = 每次 poll 自我死锁（真机「按钮全没反应」
            # 与「× 关闭卡死」的根因，2026-10-09 真机抓到）。先在锁外算好。
            dn_dock = dock_count()
            with LOCK:
                payload = {'login_who': who,
                           'lines': STATE['lines'][since:],
                           'total': len(STATE['lines']),
                           'dropped': STATE['dropped'],
                           'running': STATE['running'], 'task': STATE['task'],
                           'paused': STATE['paused'],
                           'phase': STATE['phase'],
                           'done': STATE['done'], 'summary': STATE['summary'],
                           'report': STATE['report'],
                           'courses_v': STATE['courses_v'],
                           'login_fail': STATE['login_fail'],
                           'started': STATE['started'],
                           'result_v': STATE['result_v'],
                           'ares_v': STATE['ares_v'],
                           'dock_n': dn_dock}
                # 报告本体可能很大（几十门课的明细）。界面已经拿到这一版时就别再
                # 重复下发 —— 否则每次轮询都要把整个报告序列化一遍送过去。
                # 运行期不下发上一份 result：前端在 running 期间本来就不
                # ack（不更新 rv），这份 dict 会每 400ms 重发一遍，一刷
                # 几小时就是上万次无谓序列化。
                if not STATE['running'] and rv != STATE['result_v']:
                    payload['result'] = STATE['result']
                # courses 同理：客户端拿过这一版就不再整份重发
                if cvq != STATE['courses_v']:
                    payload['courses'] = STATE['courses']
                if av != STATE['ares_v']:
                    payload['ares'] = STATE['ares']
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode('utf-8'))
        if u.path == '/api/llm-config':
            cfg = cs.load_config()
            key = cs.llm_key_of(cfg)
            vkey = cs.llm_key_of(cfg, 'vision')
            payload = {
                'llm_platform': (cfg.get('llm_platform')
                                 or cs.guess_llm_platform(cfg.get('llm_url'))),
                'llm_url': cfg.get('llm_url') or '',
                'llm_model': cfg.get('llm_model') or '',
                # 完整 Key 不回传给页面：页面只拿到「有没有存」+ 脱敏串。
                # 少一条泄露路径（截图求助、浏览器扩展抓 DOM 都拿不到），
                # 要换 Key 就填新的，留空表示不动。
                'key_set': bool(key),
                'key_mask': cs.mask_key(key),
                'ready': cs.llm_ready(cfg),
                'platforms': cs.LLM_PLATFORMS,
                # 视觉模型（可选）：与主模型分开存、分开验，四项全空 = 未启用。
                'vision_platform': (cfg.get('llm_vision_platform')
                                    or cs.guess_llm_platform(
                                        cfg.get('llm_vision_url'))),
                'vision_url': cfg.get('llm_vision_url') or '',
                'vision_model': cfg.get('llm_vision_model') or '',
                'vision_key_set': bool(vkey),
                'vision_key_mask': cs.mask_key(vkey),
                'vision_ready': cs.llm_vision_ready(cfg),
            }
            # 顺手把刷课倍速也带给前端（页面加载时回填下拉框的上次选择）
            payload['brush_rate'] = cfg.get('brush_rate', 1.25)
            payload['keep_awake'] = bool(cfg.get('keep_awake', True))
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode('utf-8'))
        return self._send(404, b'{"error":"not found"}')

    # 解析请求体的 POST 接口：必须带 application/json 头。
    # HTML 表单发不出这个头 → 表单型 CSRF 无法伪造这些操作。
    # v4.0 审查后这道闸升级成「所有 /api POST 都要 JSON 头」（见 do_POST）。
    # 清单保留作对照：曾经只对这里列的端点设闸，/api/answer-one 恰好
    # 漏在名单外 —— 按端点挑名单的维护方式本身就是缺陷。
    _JSON_PATHS = ('/api/run', '/api/brush', '/api/answer', '/api/combo',
                   '/api/answer-one', '/api/answer-verify',
                   '/api/brush-act', '/api/answer-act', '/api/combo-act',
                   '/api/llm-config', '/api/llm-test',
                   # /api/open 带外部可控的 url，必须挡掉无预检的跨源 POST
                   '/api/open')

    def _drain_body(self):
        """把还没读走的请求体读干净，然后再回包。

        403 / 415 这些「还没读 body 就拒」的分支如果直接回包头就关连接，
        Windows 看到 socket 里仍压着没读走的请求体，关闭时会发 RST，客户端
        收到的就不是我们精心写好的 403/415，而是 ConnectionAbortedError
        （CSRF 防线自测里就是这种偶发：同一个用例时过时不过）。先读干净
        再回，状态码才稳定送达，也不再往控制台漏 RST 噪音。上限同正文读取。
        """
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = 0
        if n > 0:
            try:
                self.rfile.read(min(n, 1 << 20))
            except Exception:
                pass

    def do_POST(self):
        if not self._local_only():
            self._drain_body()
            return self._send(403, b'{"error":"forbidden"}')
        u = urlparse(self.path)
        # CSRF 第二道闸：跨源 no-cors POST（text/plain，不触发预检）
        # 也带 Origin，但对不上本机 Host —— 直接拒。同源 fetch 与
        # 不带 Origin 的本机脚本不受影响。没有这道闸，/api/quit 这类
        # 无 body 写端点能被任意网页一条 fetch 杀掉正在跑的任务。
        origin = self.headers.get('Origin') or ''
        if origin and urlsplit(origin).netloc != (
                self.headers.get('Host') or ''):
            self._drain_body()
            return self._send(403, b'{"error":"cross origin"}')
        # 统一闸：所有 /api 的 POST 都必须带 application/json 头
        # （HTML 表单发不出这个头 → 表单型 CSRF 全部失效）。
        if u.path.startswith('/api/') and 'application/json' not in (
                self.headers.get('Content-Type') or '').lower():
            self._drain_body()
            return self._send(415, b'{"error":"json required"}')
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = 0
        if n <= 0:
            raw = b'{}'            # 无长度/非正数：按空请求体处理
        elif n > (1 << 20):        # 1MB 上限：本机界面用不到更大的
            return self._send(413, b'{"error":"too large"}')
        else:
            raw = self.rfile.read(n)
        if u.path == '/api/run':
            try:
                data = json.loads(raw.decode('utf-8') or '{}')
            except Exception:
                data = {}
            action = data.get('action')
            if not isinstance(action, str) or not action:
                # 空 body / 坏 JSON / 缺 action：回 400 而不是默认起
                # 「全量扫描」—— 那是所有动作里最重的一个，不该被兜底触发
                return self._send(400, b'{"error":"action required"}')
            _close_work_window()       # 作业窗口开着会占 profile，先替用户关掉
            with LOCK:
                if STATE['running']:
                    return self._send(409, b'{"error":"busy"}')
                STATE.update(running=True, task=TASK_NAME.get(action, action),
                             lines=[], done=False, summary=None, started=time.time(),
                             login_fail=None)
            _spawn(_worker, action, data)
            return self._send(200, b'{"ok":true}')
        if u.path == '/api/screen-off':
            # 熄屏：强制关显示器，不影响任务（Chrome 的 Video Wake Lock
            # 挡不住显式的 SC_MONITORPOWER 广播）。同步执行，毫秒级返回。
            threading.Thread(target=_screen_off, daemon=True).start()
            return self._send(200, b'{"ok":true}')
        if u.path == '/api/brush':
            # 「刷选中的课的视频」。only 必填：不给「全部都刷」这种选项，
            # 防止误点一次就把几十门课全挂到后台。
            try:
                data = json.loads(raw.decode('utf-8') or '{}')
            except Exception:
                data = {}
            only = [str(k) for k in (data.get('only') or []) if k]
            if not only:
                return self._send(400, json.dumps(
                    {'error': '没有勾选要刷的课程'}).encode('utf-8'))
            _close_work_window()       # 作业窗口开着会占 profile，先替用户关掉
            with LOCK:
                if STATE['running']:
                    return self._send(409, b'{"error":"busy"}')
                STATE.update(running=True, task=TASK_NAME['brush'],
                             lines=[], done=False, summary=None,
                             started=time.time(), login_fail=None)
            _spawn(_brush_worker, {'only': only,
                                   'shutdown': bool(data.get('shutdown')),
                                   'rate': data.get('rate')})
            return self._send(200, b'{"ok":true}')
        if u.path == '/api/answer':
            # 「做勾选课程的章节作业」。only 必填；draft=True 走暂存，
            # 缺省仍是答完正式交卷（v3.9.5 起二选一由下拉决定）。
            try:
                data = json.loads(raw.decode('utf-8') or '{}')
            except Exception:
                data = {}
            only = [str(k) for k in (data.get('only') or []) if k]
            if not only:
                return self._send(400, json.dumps(
                    {'error': '没有勾选要做作业的课程'}).encode('utf-8'))
            _close_work_window()       # 作业窗口开着会占 profile，先替用户关掉
            with LOCK:
                if STATE['running']:
                    return self._send(409, b'{"error":"busy"}')
                STATE.update(running=True, task=TASK_NAME['answer'],
                             lines=[], done=False, summary=None,
                             started=time.time(), login_fail=None)
            _spawn(_answer_worker, {'only': only,
                                    'draft': bool(data.get('draft'))})
            return self._send(200, b'{"ok":true}')
        if u.path == '/api/answer-one':
            # 「刷题」：对未完成作业清单里点中的那一份答题；draft=True
            # 暂存不交卷，缺省答完直接交卷。
            # 没有 http(s) 链接就无从定位这份作业，直接拒掉而不是猜。
            try:
                data = json.loads(raw.decode('utf-8') or '{}')
            except Exception:
                data = {}
            url = str(data.get('url') or '').strip()
            if not url.lower().startswith('http'):
                return self._send(400, json.dumps(
                    {'error': '这份作业没有可用的直达链接'}).encode('utf-8'))
            _close_work_window()       # 作业窗口开着会占 profile，先替用户关掉
            with LOCK:
                if STATE['running']:
                    return self._send(409, b'{"error":"busy"}')
                STATE.update(running=True, task=TASK_NAME['answer_one'],
                             lines=[], done=False, summary=None,
                             started=time.time(), login_fail=None)
            _spawn(_answer_single_worker,
                   {'url': url, 'course': str(data.get('course') or ''),
                    'title': str(data.get('title') or ''),
                    'draft': bool(data.get('draft'))})
            return self._send(200, b'{"ok":true}')
        if u.path == '/api/open-work':
            # 「去完成」免登录：用工具自己的浏览器（带登录态）打开作业页。
            # 只放行超星 / 高校域名，防变成任意网址的跳板。
            try:
                data = json.loads(raw.decode('utf-8') or '{}')
            except Exception:
                data = {}
            url = str(data.get('url') or '').strip()
            host = urlparse(url).netloc.lower()
            host_ok = (host == 'chaoxing.com' or host.endswith('.chaoxing.com')
                       or host.endswith('.edu.cn'))
            if (not url.lower().startswith(('http://', 'https://'))
                    or not host_ok):
                return self._send(400, json.dumps(
                    {'error': '仅支持打开超星 / 高校域名的链接'}).encode('utf-8'))
            with LOCK:
                if STATE['running']:
                    return self._send(409, json.dumps(
                        {'error': '任务运行中，浏览器被占用，'
                                  '等任务结束再点'}).encode('utf-8'))
            _open_work_async(url)
            return self._send(200, b'{"ok":true}')

        if u.path == '/api/combo':
            # 「刷课+刷题」：先刷选中范围的视频，再对同一批范围做作业并交卷。
            try:
                data = json.loads(raw.decode('utf-8') or '{}')
            except Exception:
                data = {}
            only = [str(k) for k in (data.get('only') or []) if k]
            if not only:
                return self._send(400, json.dumps(
                    {'error': '没有勾选要刷的课程/章节'}).encode('utf-8'))
            _close_work_window()       # 作业窗口开着会占 profile，先替用户关掉
            with LOCK:
                if STATE['running']:
                    return self._send(409, b'{"error":"busy"}')
                STATE.update(running=True, task=TASK_NAME['combo'],
                             lines=[], done=False, summary=None,
                             started=time.time(), login_fail=None)
            _spawn(_combo_worker, {'only': only,
                                   'shutdown': bool(data.get('shutdown')),
                                   'rate': data.get('rate')})
            return self._send(200, b'{"ok":true}')
        if u.path in ('/api/brush-urls', '/api/answer-urls', '/api/combo-urls'):
            # 「上传链接刷课刷题」：用户把页面链接粘进来直接刷，不依赖先查询。
            #   brush-urls  = 刷课（每条课程链接回课程页找待完成章节刷视频）
            #   answer-urls = 刷题（每条作业链接走答题交卷链；draft=True 暂存）
            #   combo-urls  = 先刷课再刷题（按链接类型自动分流）
            # 只放行超星 / 高校域名，避免变成任意网址的跳板。
            try:
                data = json.loads(raw.decode('utf-8') or '{}')
            except Exception:
                data = {}
            links = _parse_links(str(data.get('links') or ''))
            bad = [l['url'] for l in links if not _link_host_ok(l['url'])]
            links = [l for l in links if _link_host_ok(l['url'])]
            if not links:
                return self._send(400, json.dumps(
                    {'error': ('没解析到可用的超星 / 高校链接'
                               + ('（有 %d 条不是超星链接，已忽略）' % len(bad)
                                  if bad else ''))},
                    ensure_ascii=False).encode('utf-8'))
            _close_work_window()       # 作业窗口开着会占 profile，先替用户关掉
            action = {('/api/brush-urls'): 'brush_urls',
                      ('/api/answer-urls'): 'answer_urls',
                      ('/api/combo-urls'): 'combo_urls'}[u.path]
            with LOCK:
                if STATE['running']:
                    return self._send(409, b'{"error":"busy"}')
                STATE.update(running=True, task=TASK_NAME[action],
                             lines=[], done=False, summary=None,
                             started=time.time(), login_fail=None)
            worker = {'brush_urls': _brush_link_worker,
                      'answer_urls': _answer_links_worker,
                      'combo_urls': _link_combo_worker}[action]
            _spawn(worker, {'links': links,
                            'shutdown': bool(data.get('shutdown')),
                            'rate': data.get('rate'),
                            'draft': bool(data.get('draft'))})
            return self._send(200, b'{"ok":true}')
        if u.path in ('/api/brush-act', '/api/answer-act', '/api/combo-act'):
            # 「进行中的课堂活动」那一节的动作列：
            #   brush-act  = 刷课（活动所在课程的待完成视频）
            #   answer-act = 作答（大模型答活动本身；draft=True 暂存草稿）
            #   combo-act  = 先刷课再作答
            # targets 是查询产物（每条含 cid/clsid/id）；只收数字定位参数，
            # 不给任意东西留可乘之机。
            try:
                data = json.loads(raw.decode('utf-8') or '{}')
            except Exception:
                data = {}
            targets = [t for t in (data.get('targets') or [])
                       if isinstance(t, dict)
                       and str(t.get('cid') or '').isdigit()
                       and str(t.get('clsid') or '').isdigit()
                       and str(t.get('id') or '').isdigit()]
            if not targets:
                return self._send(400, json.dumps(
                    {'error': '没有可用的课堂活动（缺少活动 / 课程定位参数）'},
                    ensure_ascii=False).encode('utf-8'))
            _close_work_window()       # 作业窗口开着会占 profile，先替用户关掉
            action = {'/api/brush-act': 'brush_act',
                      '/api/answer-act': 'answer_act',
                      '/api/combo-act': 'combo_act'}[u.path]
            with LOCK:
                if STATE['running']:
                    return self._send(409, b'{"error":"busy"}')
                STATE.update(running=True, task=TASK_NAME[action],
                             lines=[], done=False, summary=None,
                             started=time.time(), login_fail=None)
            worker = {'brush_act': _brush_act_worker,
                      'answer_act': _answer_act_worker,
                      'combo_act': _combo_act_worker}[action]
            _spawn(worker, {'targets': targets,
                            'shutdown': bool(data.get('shutdown')),
                            'rate': data.get('rate'),
                            'draft': bool(data.get('draft'))})
            return self._send(200, b'{"ok":true}')
        if u.path == '/api/answer-verify':
            # 「重新核实」：把未确认的作业逐份重开，翻案或继续等平台翻转。
            try:
                data = json.loads(raw.decode('utf-8') or '{}')
            except Exception:
                data = {}
            items = [it for it in (data.get('items') or [])
                     if isinstance(it, dict) and it.get('key') and it.get('kid')]
            if not items:
                return self._send(400, json.dumps(
                    {'error': '没有待核实的作业'}).encode('utf-8'))
            _close_work_window()       # 作业窗口开着会占 profile，先替用户关掉
            with LOCK:
                if STATE['running']:
                    return self._send(409, b'{"error":"busy"}')
                STATE.update(running=True, task=TASK_NAME['verify'],
                             lines=[], done=False, summary=None,
                             started=time.time(), login_fail=None)
            _spawn(_verify_worker, {'items': items})
            return self._send(200, b'{"ok":true}')
        if u.path == '/api/llm-config':
            try:
                data = json.loads(raw.decode('utf-8') or '{}')
            except Exception:
                data = {}
            cfg = cs.load_config()
            # 只把**这次真动过的字段**并进磁盘配置（patch_config），不整份回写：
            # cfg 是刚读的没错，但整份写入会把别的线程这期间改过的东西一起盖掉
            # （典型：查询 / 刷课任务线程手里那份旧配置落盘）。同 scan() 的教训。
            patch = {}
            # 运行期阻止休眠开关（设置页）：单独一个布尔，走同一条 patch 通道。
            if 'keep_awake' in data:
                patch['keep_awake'] = bool(data.get('keep_awake'))
            # 平台：预设平台只接受用户改过的那几项（地址默认用预设的）；
            # 自定义平台则地址与模型都听用户的。
            plat = str(data.get('llm_platform') or '').strip()
            if plat:
                patch['llm_platform'] = plat
            if 'url' in data:
                patch['llm_url'] = str(data.get('url') or '').strip()
            if 'model' in data:
                patch['llm_model'] = str(data.get('model') or '').strip()
            cfg.update(patch)
            newkey = str(data.get('llm_key') or '').strip()
            # 地址当场规范化校验：填 base 或完整地址都行，错在这里就退回，
            # 别等答题跑到一半才发现地址拼错了（那时已经花掉时间和模型钱）
            if cfg.get('llm_url'):
                _norm, _err = cs.normalize_llm_url(cfg['llm_url'])
                if _err:
                    return self._send(400, json.dumps(
                        {'error': '接口地址有问题：' + _err},
                        ensure_ascii=False).encode('utf-8'))
            if data.get('clear_key'):
                cs.clear_llm_key()
                patch['llm_key'] = ''
            elif newkey:
                # 加密存成功 → 明文就不再落 config.json；加密不可用则留在原处兜底
                if cs.save_llm_key(newkey):
                    patch['llm_key'] = ''
                else:
                    patch['llm_key'] = newkey
            cfg.update(patch)
            # ---- 视觉模型（可选）：字段独立成一节，与主模型互不影响 ----
            # 界面只在动视觉那一块时才带 vision，所以保存主模型不会误清视觉配置。
            vis = data.get('vision')
            if isinstance(vis, dict) and vis.get('copy_main'):
                # 「复制上面主模型的配置」：把主模型的地址 / 模型名 / 平台 / Key
                # 原样搬到视觉这一套字段。Key 在服务端也一起搬——否则用户还得
                # 重新手打一遍（他点这个按钮就是为了「不用再填一遍」）。
                mk = cs.llm_key_of(cfg)
                vpatch = {
                    'llm_vision_platform': (cfg.get('llm_platform') or ''),
                    'llm_vision_url': (cfg.get('llm_url') or ''),
                    'llm_vision_model': (cfg.get('llm_model') or ''),
                }
                if mk:
                    if cs.save_llm_key(mk, 'vision'):
                        vpatch['llm_vision_key'] = ''
                    else:
                        vpatch['llm_vision_key'] = mk
                patch.update(vpatch)
                cfg.update(vpatch)
                vis = None            # 已处理，别再走下面按字段覆盖那条路
            if isinstance(vis, dict):
                vpatch = {}
                vplat = str(vis.get('platform') or '').strip()
                if vplat:
                    vpatch['llm_vision_platform'] = vplat
                if 'url' in vis:
                    vpatch['llm_vision_url'] = str(vis.get('url') or '').strip()
                if 'model' in vis:
                    vpatch['llm_vision_model'] = str(vis.get('model') or '').strip()
                patch.update(vpatch)
                cfg.update(vpatch)
                if (cfg.get('llm_vision_url') or '').strip():
                    _vn, _verr = cs.normalize_llm_url(cfg['llm_vision_url'])
                    if _verr:
                        return self._send(400, json.dumps(
                            {'error': '视觉模型接口地址有问题：' + _verr},
                            ensure_ascii=False).encode('utf-8'))
                vnew = str(vis.get('llm_key') or '').strip()
                if vis.get('clear_key'):
                    cs.clear_llm_key('vision')
                    vpatch['llm_vision_key'] = ''
                elif vnew:
                    if cs.save_llm_key(vnew, 'vision'):
                        vpatch['llm_vision_key'] = ''
                    else:
                        vpatch['llm_vision_key'] = vnew
                patch.update(vpatch)
                cfg.update(vpatch)
            cs.patch_config(patch)
            # 写完必须回读确认。以前只写不查，「保存成功」的 toast 是
            # 假的：一旦写盘有竞态/权限问题，查询线程读不到 llm 键，
            # 刷作业就会提示「先配置大模型」（用户实测踩过）。
            # 只校验这次真提交过的那几项，避免「先填一半」时被误判失败。
            back = cs.load_config()
            checks = []
            if str(data.get('url') or '').strip():
                checks.append(bool(back.get('llm_url')))
            if str(data.get('model') or '').strip():
                checks.append(bool(back.get('llm_model')))
            if newkey:
                checks.append(cs.llm_key_of(back) == newkey)
            if not all(checks):
                cs.log('⚠ 模型信息写入后回读校验未通过，请重试保存。', 'err')
                return self._send(500, b'{"error":"save-verify-failed"}')
            key_now = cs.llm_key_of(back)
            vkey_now = cs.llm_key_of(back, 'vision')
            # 只在这次真的动了主模型字段时才打主模型那条日志：保存视觉模型时
            # 也打一遍「模型设置已保存：DeepSeek…」会让人以为改错了东西。
            if any(k in data for k in ('llm_platform', 'url', 'model',
                                       'llm_key', 'clear_key')):
                cs.log('模型设置已保存：%s ｜ %s ｜ Key %s'
                       % (cs.llm_platform(
                              back.get('llm_platform') or 'deepseek')['label'],
                          back.get('llm_model') or '未填',
                          ('已保存 ' + cs.mask_key(key_now)) if key_now else '未填'))
            if (back.get('llm_vision_model') or '').strip():
                cs.log('视觉模型已保存：%s ｜ Key %s'
                       % (back.get('llm_vision_model'),
                          ('已保存 ' + cs.mask_key(vkey_now)) if vkey_now else '未填'))
            return self._send(200, json.dumps(
                {'ok': True, 'url': back.get('llm_url') or '',
                 'key_set': bool(key_now), 'key_mask': cs.mask_key(key_now),
                 'ready': cs.llm_ready(back),
                 'vision_url': back.get('llm_vision_url') or '',
                 'vision_key_set': bool(vkey_now),
                 'vision_key_mask': cs.mask_key(vkey_now),
                 'vision_ready': cs.llm_vision_ready(back),
                 'keep_awake': bool(back.get('keep_awake', True))},
                ensure_ascii=False).encode('utf-8'))
        if u.path == '/api/llm-test':
            # 「测试连接」：拿界面**当前填的**模型信息真调一次 LLM（短问答）。
            # ThreadingHTTPServer 每请求一线程，这里同步等待不阻塞页面轮询。
            try:
                data = json.loads(raw.decode('utf-8') or '{}')
            except Exception:
                data = {}
            tcfg = dict(cs.load_config())
            # 测视觉模型时把界面填的值写进 llm_vision_* 那一套字段，主模型那套
            # 一个都不碰（两边的配置本就是独立的）。
            vis = bool(data.get('vision'))
            purpose = 'vision' if vis else 'main'
            if vis:
                for src, dst in (('llm_url', 'llm_vision_url'),
                                 ('llm_model', 'llm_vision_model')):
                    v = str(data.get(src) or '').strip()
                    if v:
                        tcfg[dst] = v
            else:
                for k in ('llm_url', 'llm_model'):
                    v = str(data.get(k) or '').strip()
                    if v:
                        tcfg[k] = v
            # Key 的取值顺序：界面刚填的 → 已保存的（加密文件里那把）。
            # 这一路不写回 url/model，也不落盘：测试通过与否都不该改用户
            # 已存的模型设置。唯一例外是 llm_key_of 发现 config 里还留着
            # 老版本的明文 Key——那会顺手迁成加密存放（这是好事）。
            typed = str(data.get('llm_key') or '').strip()
            tkey = typed or cs.llm_key_of(tcfg, purpose)
            uf = 'llm_vision_url' if vis else 'llm_url'
            mf = 'llm_vision_model' if vis else 'llm_model'
            miss = []
            if not (tcfg.get(uf) or '').strip():
                miss.append('接口地址')
            if not tkey:
                miss.append('API Key')
            if not (tcfg.get(mf) or '').strip():
                miss.append('模型名')
            if miss:
                return self._send(400, json.dumps(
                    {'error': '还没填完：%s' % '、'.join(miss)},
                    ensure_ascii=False).encode('utf-8'))
            try:
                # timeout 给短值：连通测试只发「回复两个字」，12 秒足够，
                # 也避免网络不通时用户对着按钮等太久（llm_chat 内部最多重试 3 次）
                reply = cs.llm_chat(tcfg, '请只回复两个字：连通', timeout=12,
                                    key=tkey, purpose=purpose)
                cs.log('%s连通验证通过（%s）：%s'
                       % ('视觉模型 ' if vis else '大模型',
                          tcfg[mf], (reply or '')[:40]))
                return self._send(200, json.dumps(
                    {'ok': True, 'reply': (reply or '')[:80]},
                    ensure_ascii=False).encode('utf-8'))
            except Exception as e:
                cs.log('%s连通验证失败：%s'
                       % ('视觉模型 ' if vis else '大模型', str(e)[:160]), 'err')
                return self._send(502, json.dumps(
                    {'error': str(e)[:300]},
                    ensure_ascii=False).encode('utf-8'))
        if u.path in ('/api/pause', '/api/resume', '/api/cancel'):
            with LOCK:
                busy = STATE['running']
            if not busy:
                return self._send(409, b'{"error":"not running"}')
            if u.path == '/api/pause':
                PAUSE.set()
                with LOCK:
                    STATE['paused'] = True
                cs.log('⏸ 收到暂停请求：当前这步做完就停住（查询是这门课抓完，'
                       '刷视频是当前视频暂停）…', 'warn')
            elif u.path == '/api/resume':
                PAUSE.clear()
                with LOCK:
                    STATE['paused'] = False
                cs.log('▶ 收到继续请求 …')
            else:
                CANCEL.set()
                PAUSE.clear()
                with LOCK:
                    STATE['paused'] = False
                cs.log('■ 收到停止请求：当前这步做完就收尾，已完成的进度会保留 …', 'warn')
            return self._send(200, b'{"ok":true}')
        if u.path == '/api/check-update':
            # 查 GitHub 最新 release，和本机 VERSION 比对。只提示，不自动下载。
            # 网络异常一律静默（返回 ok:false），绝不因为查更新把 UI 卡住或报错。
            try:
                import urllib.request as _ur
                req = _ur.Request(
                    'https://api.github.com/repos/CC0987326/chaoxing-scanner'
                    '/releases/latest',
                    headers={'User-Agent': 'chaoxing-scanner/' + cs.VERSION,
                             'Accept': 'application/vnd.github+json'})
                with _ur.urlopen(req, timeout=6) as resp:
                    rel = json.loads(resp.read().decode('utf-8', 'replace'))
                latest = str(rel.get('tag_name') or '').strip()
                url = str(rel.get('html_url') or
                          'https://github.com/CC0987326/chaoxing-scanner/releases')
                cur = cs.VERSION

                def _nums(v):
                    return [int(x) for x in re.findall(r'\d+', v or '')] or [0]
                newer = _nums(latest) > _nums(cur) if latest else False
                self._send(200, json.dumps(
                    {'ok': True, 'current': cur, 'latest': latest,
                     'url': url, 'newer': newer}, ensure_ascii=False).encode('utf-8'))
            except Exception as e:
                cs.log('检查更新失败（不影响使用）：%s' % e, 'warn')
                self._send(200, json.dumps(
                    {'ok': False, 'error': '网络不可用或请求超时',
                     'current': cs.VERSION}, ensure_ascii=False).encode('utf-8'))
            return
        if u.path == '/api/export-log':
            # 把运行日志另存一份带时间戳的副本到「输出/日志」，并把文件夹弹出来。
            try:
                dst = cs.OUT_DIR / '日志'
                dst.mkdir(parents=True, exist_ok=True)
                ts = time.strftime('%Y%m%d_%H%M%S')
                target = dst / ('学习通日志_%s.txt' % ts)
                src = cs.LOG_FILE
                if src.exists():
                    shutil.copyfile(str(src), str(target))
                else:
                    target.write_text('（日志文件还不存在）\n', encoding='utf-8')
                # 跑批的套件也会点这个按钮。CS_NO_GUI 置位时只落副本、不弹资源管理器
                # （否则每跑一次测试就"啪"地弹出一个「输出/日志」文件夹，烦人）。
                try:
                    if os.name == 'nt' and not os.environ.get('CS_NO_GUI'):
                        os.startfile(str(dst))
                except Exception:
                    pass
                self._send(200, json.dumps(
                    {'ok': True, 'path': str(target)},
                    ensure_ascii=False).encode('utf-8'))
            except Exception as e:
                cs.log('导出日志失败：%s' % e, 'err')
                self._send(200, json.dumps(
                    {'ok': False, 'error': str(e)[:120]},
                    ensure_ascii=False).encode('utf-8'))
            return
        if u.path == '/api/open-report':
            p = STATE.get('report') or str(cs.OUT_DIR / '学习通未完成清单.md')
            try:
                if not os.environ.get('CS_NO_GUI'):
                    os.startfile(p)  # type: ignore[attr-defined]
                return self._send(200, b'{"ok":true}')
            except Exception as e:
                return self._send(500, json.dumps({'error': str(e)}).encode())
        if u.path == '/api/open-dir':
            try:
                cs.OUT_DIR.mkdir(parents=True, exist_ok=True)
                if not os.environ.get('CS_NO_GUI'):
                    os.startfile(str(cs.OUT_DIR))  # type: ignore[attr-defined]
                return self._send(200, b'{"ok":true}')
            except Exception as e:
                return self._send(500, json.dumps({'error': str(e)}).encode())
        if u.path == '/api/focus':
            # 用户又双击了一次启动器：把窗口从最小化提出来（单实例的收尾动作）
            ok = focus_window()
            return self._send(200, json.dumps({'ok': ok}).encode('utf-8'))
        if u.path == '/api/dock-rect':
            # 预览页签的面板矩形。前端在页签切换 / 窗口缩放 / 布局变化时
            # 推送，布局线程拿它对齐 Chrome 子窗口。坏值按「没有矩形」处理
            # （布局线程会把子窗口全部藏起来，不会乱摆）。
            try:
                d = json.loads(raw.decode('utf-8') or '{}')
            except Exception:
                d = {}
            try:
                rect = {'l': float(d.get('l') or 0),
                        't': float(d.get('t') or 0),
                        'w': float(d.get('w') or 0),
                        'h': float(d.get('h') or 0)}
            except (TypeError, ValueError):
                rect = None
            with LOCK:
                DESKTOP['dock_rect'] = rect
                DESKTOP['dock_on'] = bool(rect and d.get('on'))
            return self._send(200, b'{"ok":true}')
        if u.path == '/api/focus-dock':
            # 用户切到「预览」页签、点回预览区时，把键盘焦点交给内嵌浏览器。
            # 跨进程 WS_CHILD 不 AttachThreadInput 就设不了焦点，见 _focus_child。
            ok = focus_docked()
            return self._send(200, json.dumps({'ok': ok}).encode('utf-8'))
        if u.path == '/api/pwd-save':
            # 「记住密码」：加密落 runtime/，config 与 localStorage 不碰。
            # 日志绝不提内容 —— 这里自始至终不 log 密码本身。
            try:
                d = json.loads(raw.decode('utf-8') or '{}')
            except Exception:
                d = {}
            ok = cs.save_login_pwd(str(d.get('password') or ''))
            return self._send(200, json.dumps(
                {'ok': ok}).encode('utf-8'))
        if u.path == '/api/pwd-forget':
            cs.forget_login_pwd()
            return self._send(200, b'{"ok":true}')
        if u.path == '/api/logout':
            # 退出登录（换号）：清本机会话 + 归档 profile。登录态默认常驻
            # （v6.15），这是唯一的显式「我要换号」出口。关窗/杀进程在
            # cs.logout() 里做，这里只回结果。
            ok = cs.logout()
            return self._send(200, json.dumps({'ok': ok}).encode('utf-8'))
        if u.path == '/api/open':
            # 桌面窗口里点 target="_blank" 的外链（打开作业 / 去完成 / 项目主页）：
            # 在窗口内跳转会顶掉整个界面、而且没有后退键，所以改交系统默认浏览器。
            # 只放行 http(s) —— 这个接口只在本机监听，但没必要给它开 file:// 之类。
            try:
                d = json.loads(raw.decode('utf-8') or '{}')
            except Exception:
                d = {}
            target = str(d.get('url') or '')
            if not target.lower().startswith(('http://', 'https://')):
                return self._send(400, b'{"error":"bad url"}')
            try:
                webbrowser.open(target)
                return self._send(200, b'{"ok":true}')
            except Exception as e:
                return self._send(
                    500, json.dumps({'error': str(e)}).encode('utf-8'))
        if u.path == '/api/quit':
            # 退出前先置停止信号：让任务线程的收尾逻辑（保存进度/报告）
            # 有机会跑，也避免浏览器进程被硬杀后残留
            CANCEL.set()
            PAUSE.set()      # 解除暂停挂起，别让任务线程卡在暂停循环里
            # 先回包再关停：否则连接会被自己掐断，浏览器报「网络错误」
            self._send(200, b'{"ok":true}')
            _shutdown_async()
            return
        return self._send(404, b'{"error":"not found"}')


# =====================================================================
# 桌面窗口（v4.0）
#
# 以前界面是「起本地服务 + 用系统默认浏览器打开」：用户看到的是一个浏览器
# 标签页，反复双击还会叠出好几个，关掉标签页后台也不退出。v4.0 起默认走
# 桌面窗口 —— 用 Windows 自带的 WebView2 开一个真正的应用窗口（没有地址栏、
# 没有标签页、任务栏一个图标），并保证同一时刻只有一个实例。
#
# 三条纪律（都是实测踩出来的）：
#   1. pywebview 的 start() 必须在主线程，所以 HTTP 服务得挪到后台线程；
#   2. WebView2 的用户数据目录必须显式落在 runtime/ 下：默认位置是临时目录，
#      实测直接报 WebView2 E_ABORT 起不来；而且它属于本机运行痕迹，
#      不能进分发包（runtime/ 本来就在排除清单里）；
#   3. 网页模式（gui 命令）保持原样不动 —— 它是排障时的备用入口。
# =====================================================================

WIN_TITLE = '学习通巡检工具'
WIN_MUTEX = 'Local\\chaoxing-scanner-desktop-v4'

# 桌面窗口的运行时状态。网页模式下 win 恒为 None。
# docked = 已嵌进「预览」页签的浏览器窗口句柄（最新在后）；
# styles = 入坞前各窗口的原样式（留档）；
# dock_rect/dock_on = 前端推送的预览面板矩形与显隐。
# （v4.3 起不再用 tabs_base 基线快照——改用「属主 PID == 常驻浏览器」判据。）
DESKTOP = {'win': None, 'url': '', 'mode': False, 'mutex': None,
           'force': False, 'docked': [], 'styles': {},
           'dock_rect': None, 'dock_on': False, 'quit': False}


def _ui_record():
    """本机界面的端口记录（写端口与 pid，供第二个实例找到它）。

    路径每次现读 cs.RUNTIME：测试沙箱是在 import 之后改道这些常量的，
    预先算成模块级路径会指向真实 runtime（这个坑踩过一次）。
    """
    return cs.RUNTIME / 'ui.json'


def _write_ui_record(port):
    try:
        _ui_record().parent.mkdir(parents=True, exist_ok=True)
        _ui_record().write_text(
            json.dumps({'port': port, 'pid': os.getpid(), 't': time.time()}),
            encoding='utf-8')
    except Exception as e:
        cs.log('写会话记录失败（不影响使用）：%s' % e, 'warn')


def _read_ui_record():
    try:
        d = json.loads(_ui_record().read_text(encoding='utf-8'))
        return int(d.get('port') or 0)
    except Exception:
        return 0


def _read_ui_pid():
    try:
        d = json.loads(_ui_record().read_text(encoding='utf-8'))
        return int(d.get('pid') or 0)
    except Exception:
        return 0


def _pid_alive(pid):
    """pid 是否还活着。探测不了就按活着处理：宁可多试一次唤起。"""
    try:
        import ctypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int,
                                    ctypes.c_uint32]
        h = k32.OpenProcess(0x1000, 0, int(pid))
        if not h:
            return False        # PROCESS_QUERY_LIMITED_INFORMATION
        k32.CloseHandle(h)
        return True
    except Exception:
        return True


def _drop_ui_record():
    try:
        _ui_record().unlink()
    except Exception:
        pass


def _post_local(port, path, timeout=2.0):
    """给本机界面发一个空 POST，只关心通不通。"""
    try:
        import urllib.request
        req = urllib.request.Request(
            'http://127.0.0.1:%d%s' % (port, path), data=b'{}',
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def _claim_single_instance():
    """抢「本机唯一界面」的所有权。True = 该由本进程起窗口。

    用命名互斥体而不是「端口是否被占」：进程怎么死，互斥体都会随之释放；
    端口却可能残留在 TIME_WAIT，或被杀软/代理临时占住，据此判断会误拒。
    拿不到互斥体（受限环境）时按「可以起」处理 —— 宁可多开一个，
    也不能让用户点不开程序。
    """
    try:
        import ctypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                     ctypes.c_wchar_p]
        h = k32.CreateMutexW(None, 0, WIN_MUTEX)
        if not h:
            return True
        if ctypes.get_last_error() == 183:   # 已有实例：句柄不占为己有
            k32.CloseHandle(h)
            return False
        DESKTOP['mutex'] = h
        return True
    except Exception:
        return True


def _set_foreground():
    """按窗口标题把本工具的窗口提到最前。"""
    try:
        import ctypes
        u32 = ctypes.WinDLL('user32', use_last_error=True)
        u32.FindWindowW.restype = ctypes.c_void_p
        u32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        hwnd = u32.FindWindowW(None, WIN_TITLE)
        if not hwnd:
            return False
        u32.ShowWindow(hwnd, 9)          # SW_RESTORE：最小化/最大化都还原
        u32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def _wake_existing(timeout=4.0):
    """把已经开着的窗口叫到前面，成功返回 True。

    两件事缺一不可：
      · POST /api/focus 让**窗口的拥有者**把窗口从最小化恢复出来 ——
        只有它手上有那个窗口对象；
      · 本进程再抢一次前台：此刻的前台进程是被用户刚双击起来的本进程，
        只有前台进程才有把别的窗口提到最前的权限。
    """
    end = time.time() + timeout
    while time.time() < end:
        port = _read_ui_record()
        # 记录里的 pid 已经不在了 = 上次进程被硬杀、没来得及删记录。
        # 端口可能已被无关服务占住，别把陌生的 2xx 当「唤起成功」。
        pid = _read_ui_pid()
        if port and pid and not _pid_alive(pid):
            _drop_ui_record()
            port = 0
        if port and _post_local(port, '/api/focus'):
            _set_foreground()
            return True
        time.sleep(0.25)
    return _set_foreground()     # 记录没读到，也按标题试一次


def focus_window():
    """/api/focus 的实现：把桌面窗口从最小化唤醒并置前。"""
    w = DESKTOP.get('win')
    if w is None:
        return False
    for call in ('restore', 'show'):
        try:
            getattr(w, call)()
        except Exception:
            pass
    return _set_foreground()


def _on_window_closing():
    """点窗口的 × 时：任务还在跑就先别关。

    返回 False 会让 pywebview 取消这次关闭。任务收尾涉及保存进度、关浏览器，
    直接退会把平台侧刷到一半的状态丢在那儿，用户还得从头再来。
    这里**不能**用 evaluate_js 去弹提示：closing 是在 UI 线程同步跑的，
    而 evaluate_js 要等 JS 返回结果，会当场死锁。所以只写日志 + 把窗口唤起。
    """
    if DESKTOP.get('force'):
        return None
    with LOCK:
        running = bool(STATE.get('running'))
    if not running:
        return None
    _set_foreground()
    cs.log('窗口没有关闭：任务还在运行。要退出请先在界面上点「停止」，再关窗口。',
           'warn')
    return False


def _wait_task(delay=0.6, timeout=10.0):
    """退出前给正在跑的任务一点收尾时间。

    不等就走，主线程一退，playwright 的 driver 进程会被留在系统里（实测会挂
    一堆 node.exe），下次启动还可能因为 profile 目录被占而起不来。
    """
    time.sleep(delay)
    with LOCK:
        th = STATE.get('_thread')
    if th is not None and th.is_alive():
        cs.log('正在停止后台任务并收尾，请稍候…')
        th.join(timeout=timeout)


def _teardown_ui():
    """「关窗口」这条路汇到这里：停服务、清记录。"""
    DESKTOP['win'] = None
    DESKTOP['quit'] = True
    h = DESKTOP.get('mutex')
    if h:
        # 互斥体随进程退出本来也会释放；这里主动关，同进程二次
        # serve_desktop 才能重新抢到所有权。
        try:
            import ctypes
            ctypes.WinDLL('kernel32').CloseHandle(h)
        except Exception:
            pass
        DESKTOP['mutex'] = None
    with LOCK:
        srv = STATE.get('_server')
        STATE['_server'] = None
    if srv is not None:
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            srv.server_close()
        except Exception:
            pass
    _drop_ui_record()
    cs.log('界面已关闭。')


# ------------------------------------------------ 浏览器窗口收纳（预览页签，v4.1）
# 用户预期（2026-10-09 拍板）：浏览器不另开窗口——工具跑任务/登录/
# 「去完成」开出来的 Chrome 一律嵌进主窗口的「预览」页签里。
# 判据（v4.3 收紧）——**只认本工具常驻浏览器进程开的窗口**：
#   ① 类名是 Chrome 顶层窗口（Chrome_WidgetWin_1）；
#   ② 属主进程 PID == 常驻浏览器主进程 PID（cs.browser_proc_pid()）。
#      旧版靠「启动基线快照差分」，挡不住用户**运行中**才点开的自己浏览器
#      （2026-10-11 真机：桌面端点开自己的 Edge → 类名一样、又不在基线里、
#      还不是子窗口 → 被误嵌进预览，且重父后输入法上下文丢失、打不了字）。
#      按 PID 一刀切从根上杜绝（真机探针实测工具窗口 pid 与常驻主进程一致）。
#   ③ 还不是任何窗口的子窗口（GetParent 为空，双保险）。
CHROME_WIN_CLASS = 'Chrome_WidgetWin_1'

# 入坞用到的 Win32 常量（_dock_style_apply / _dock_layout 只在这一处用；
# v4.1 首版漏了这组定义，守夜线程 NameError 被静默吞掉、从不入坞——教训：
# 新增模块级名字的补丁必须连带定义一起落，且活体测试要真调一次）。
GWL_STYLE = -16                 # 窗口样式索引
WS_CHILD = 0x40000000           # 子窗口
WS_POPUP = 0x80000000           # 弹出窗（Chrome 顶层窗口自带）
WS_CAPTION = 0x00C00000         # 标题栏
WS_THICKFRAME = 0x00040000      # 可调边框
HWND_TOP = 0                    # SetWindowPos：置顶（Z 序，不抢焦点靠 SWP_NOACTIVATE）
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010


def _u32():
    import ctypes
    return ctypes.windll.user32


def _window_pid(hwnd):
    """窗口所属进程的 PID（GetWindowThreadProcessId）。"""
    import ctypes
    u32 = _u32()
    pid = ctypes.c_ulong()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _enum_chrome_windows(own_pid=None):
    """列出当前可见的 Chrome 顶层窗口句柄。

    own_pid 给定（= cs.browser_proc_pid()）时**只返回该进程开出来的窗口**——
    这是本工具自己的常驻浏览器。用户自己开着/运行中点开的 Edge/Chrome 与其
    窗口类名完全相同，但属主 PID 不同，据此精确排除（v4.3，见顶注 ②）。
    own_pid 为 None（未传）时退回「全部 Chrome 窗口」，仅用于启动时的兼容。
    """
    import ctypes
    u32 = _u32()
    out = []
    CB = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                            ctypes.c_void_p)

    def _on(hwnd, _lparam):
        if u32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(32)
            u32.GetClassNameW(hwnd, buf, 32)
            if buf.value == CHROME_WIN_CLASS:
                if own_pid is None or _window_pid(hwnd) == own_pid:
                    out.append(hwnd)
        return 1

    try:
        u32.EnumWindows(CB(_on), None)
    except Exception:
        pass
    return out


def _main_hwnd():
    """主窗口句柄（找不到返回 0）。"""
    import ctypes
    u32 = _u32()
    u32.FindWindowW.restype = ctypes.c_void_p
    u32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    return u32.FindWindowW(None, WIN_TITLE) or 0


def _is_foreground(hwnd):
    """本工具的窗口此刻是不是前台窗（用户就在它上面）。"""
    import ctypes
    u32 = _u32()
    u32.GetForegroundWindow.restype = ctypes.c_void_p
    return u32.GetForegroundWindow() == hwnd


def dock_count():
    """/api/poll 用：当前嵌在预览里的浏览器窗口数（顺带清掉死句柄）。"""
    if not DESKTOP.get('mode'):
        return 0
    u32 = _u32()
    with LOCK:
        alive = [h for h in DESKTOP['docked'] if u32.IsWindow(h)]
        if len(alive) != len(DESKTOP['docked']):
            for h in list(DESKTOP['docked']):
                if h not in alive:
                    DESKTOP['docked'].remove(h)
                    DESKTOP['styles'].pop(h, None)
        return len(alive)


def _dpi_scale(hwnd):
    """主窗口的 DPI 缩放比（前端矩形是 CSS 像素，乘它才是物理像素）。"""
    try:
        import ctypes
        u32 = ctypes.windll.user32
        try:
            dpi = u32.GetDpiForWindow(hwnd)      # Win10 1607+
            if dpi:
                return dpi / 96.0
        except Exception:
            pass
        hdc = u32.GetDC(hwnd)
        try:
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)   # LOGPIXELSX
            return (dpi or 96) / 96.0
        finally:
            u32.ReleaseDC(hwnd, hdc)
    except Exception:
        return 1.0


def _dock_style_apply(hwnd):
    """嵌为子窗口前的样式改造：去标题栏/可调边框，WS_POPUP 换 WS_CHILD。

    返回旧样式留档（窗口随任务结束关闭，当前版本不做「弹出」）。
    """
    u32 = _u32()
    old = u32.GetWindowLongW(hwnd, GWL_STYLE)
    new = (old & ~(WS_POPUP | WS_CAPTION | WS_THICKFRAME)) | WS_CHILD
    u32.SetWindowLongW(hwnd, GWL_STYLE, new)
    return old


def _focus_child(hwnd):
    """把**键盘焦点**真正交给入坞的 chrome 子窗口（真机 2026-10-14）。

    为什么必须显式做：chrome 被 SetParent 成**跨进程**的 WS_CHILD 后，主窗口
    那个线程根本管不到 chrome 那个线程的输入队列——`SetFocus(chrome)` 会直接
    失败、`GetFocus()` 停在 0，于是用户在「预览」里的登录页打字，按键全落在
    WebView2（默认聚焦的工具侧栏账号框），页面收不到（真机探针 _probe_dock_focus3
    实测：不 Attach 时 GetFocus=0，Attach 后 GetFocus=chrome）。
    跨线程设焦点的官方做法就是先把两个线程的输入队列 Attach 起来，SetFocus
    完再解除。

    返回 True 表示焦点确实落到了这个窗口。任何一步失败都安静返回 False，
    绝不影响窗口本身的显示/布局。
    """
    import ctypes
    u32 = _u32()
    try:
        main = _main_hwnd()
        if not main:
            return False
        u32.SetFocus.restype = ctypes.c_void_p
        u32.SetFocus.argtypes = [ctypes.c_void_p]
        u32.GetFocus.restype = ctypes.c_void_p
        u32.SetForegroundWindow.restype = ctypes.c_int
        u32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        u32.AttachThreadInput.restype = ctypes.c_int
        u32.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong,
                                          ctypes.c_int]
        # GetWindowThreadProcessId 默认 restype 是有符号 int，tid 超过 2^31
        # 会变负数、送进 AttachThreadInput 直接失败 —— 显式当无符号读。
        u32.GetWindowThreadProcessId.restype = ctypes.c_ulong
        u32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p,
                                                 ctypes.c_void_p]
        cur = ctypes.windll.kernel32.GetCurrentThreadId()
        main_th = u32.GetWindowThreadProcessId(main, None)
        tgt_th = u32.GetWindowThreadProcessId(hwnd, None)
        # 主窗得是前台窗，焦点才有意义（用户刚点过按钮/页签，通常本就是）
        try:
            u32.SetForegroundWindow(main)
        except Exception:
            pass
        att = []
        # 自己的线程 → 目标线程；主窗线程 → 目标线程（前台窗必须是焦点窗的祖先）
        for src in (cur, main_th):
            if src and src != tgt_th:
                try:
                    if u32.AttachThreadInput(src, tgt_th, True):
                        att.append(src)
                except Exception:
                    pass
        try:
            u32.SetFocus(hwnd)
            return u32.GetFocus() == hwnd
        finally:
            for src in att:
                try:
                    u32.AttachThreadInput(src, tgt_th, False)
                except Exception:
                    pass
    except Exception:
        return False


def focus_docked():
    """/api/focus-dock 用：把键盘焦点交给预览里当前显示的浏览器窗口。

    只处理**正在显示**的那一个（判据与 _dock_layout 完全一致：面板开着、
    矩形够大——否则该窗在 _dock_layout 里是被 ShowWindow(0) 隐藏的，给它
    设焦点没有意义），没有就不动。
    """
    u32 = _u32()
    with LOCK:
        if not DESKTOP.get('mode'):
            return False
        _rect = DESKTOP.get('dock_rect') or {}
        # 与 _dock_layout 同款判据：宽高都要 > 4 才算「真的显示出来了」，
        # 折叠成不到 5px 的面板下 _dock_layout 会隐藏窗口，这里也别去设焦点。
        on = bool(DESKTOP.get('dock_on') and _rect
                  and _rect.get('w', 0) > 4 and _rect.get('h', 0) > 4)
        docked = [h for h in DESKTOP['docked'] if u32.IsWindow(h)]
    if not (on and docked):
        return False
    return _focus_child(docked[-1])



def _dock_new_windows():
    """发现新的**工具浏览器**窗口（属主 PID == 常驻浏览器 + 尚非子窗口）就嵌进预览面板。

    登录/扫码窗口也是这一只：用户预期「一切要弹浏览器的界面都在预览」。
    嵌入时去掉标题栏（预览页签里不需要浏览器自己的框），Chrome 的标签条
    保留——任务开出的多个页面靠它切换。用户自己开的浏览器属主 PID 不同，
    不会命中（v4.3）。
    """
    import ctypes
    u32 = _u32()
    main = _main_hwnd()
    if not main:
        return
    # 只认常驻浏览器主进程开的窗口。进程没起来 = 还没有「工具自己的窗口」，
    # 直接返回（**绝不能**退回枚举全部 Chrome 窗口——那会把用户自己的浏览器
    # 卷进来，正是 2026-10-11 真机那个 bug）。
    own = cs.browser_proc_pid()
    if own is None:
        return
    u32.SetParent.restype = ctypes.c_void_p
    u32.SetParent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    with LOCK:
        have = set(DESKTOP['docked'])
    for hwnd in _enum_chrome_windows(own):
        if hwnd in have:
            continue
        if u32.GetParent(hwnd):
            continue                      # 已经是子窗口（不会发生，双保险）
        u32.ShowWindow(hwnd, 9)           # SW_RESTORE：最小化的先还原
        old = _dock_style_apply(hwnd)
        if not u32.SetParent(hwnd, main):
            # 入坞失败就还原样式，别把用户的浏览器搞成无框孤儿；
            # 屏外启动的窗口还要退回屏上有窗（否则用户面对一个隐形浏览器）
            u32.SetWindowLongW(hwnd, GWL_STYLE, old)
            u32.MoveWindow(hwnd, 60, 60, 1200, 800, True)
            continue
        with LOCK:
            DESKTOP['docked'].append(hwnd)
            DESKTOP['styles'][hwnd] = old
        # 用户此刻正开着「预览」页签、**而且人就在本工具上** → 立即把键盘
        # 焦点交给这个新窗口，否则他在预览里打字会落到工具侧栏的账号框里
        # （真机 2026-10-14）。加「本工具是前台窗」这一条：dock_on 只反映
        # 「上一次上报时预览是开着的」，用户 Alt-Tab 去别的程序后它是陈旧的
        # True——不能据此把用户从别的程序里抢回来（后台 SetForegroundWindow
        # 通常被系统前台锁挡下，但别去赌这个）。人不在本工具上就不抢焦点，
        # 等他切回预览（switchTab/mousedown）时前端会再要一次。
        with LOCK:
            _show = bool(DESKTOP.get('dock_on') and DESKTOP.get('dock_rect'))
        if _show and _is_foreground(main):
            _focus_child(hwnd)
        cs.log('浏览器窗口已嵌进「预览」页签，点「预览」即可查看与操作。')


def _dock_layout():
    """把该显示的 Chrome 子窗口对齐到预览面板矩形，其余的隐藏。

    子窗口坐标相对主窗口客户区——用户拖动主窗口时子窗口跟着走，不需要
    重排；要重排的只有面板矩形变化（前端推送）与 DPI 变化。这里每轮兜底
    重排一次（MoveWindow 幂等，代价可忽略），保证极端情况下预览也不错位。
    """
    import ctypes
    u32 = _u32()
    main = _main_hwnd()
    if not main:
        return
    with LOCK:
        docked = [h for h in DESKTOP['docked'] if u32.IsWindow(h)]
        rect = DESKTOP['dock_rect']
        on = bool(DESKTOP['dock_on'])
    if not docked:
        return
    cur = docked[-1]                      # 最新入坞的显示在最前
    if not (on and rect and rect.get('w', 0) > 4 and rect.get('h', 0) > 4):
        for h in docked:
            u32.ShowWindow(h, 0)          # 面板没开/没矩形：全部隐藏
        return
    s = _dpi_scale(main)
    x = int(rect['l'] * s)
    y = int(rect['t'] * s)
    w = int(rect['w'] * s)
    hgt = int(rect['h'] * s)
    for h in docked:
        if h == cur:
            u32.ShowWindow(h, 8)          # SW_SHOWNA：显示但不抢前台
            u32.MoveWindow(h, x, y, w, hgt, True)
            u32.SetWindowPos(h, HWND_TOP, 0, 0, 0, 0,
                             SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        else:
            u32.ShowWindow(h, 0)


def _browser_keeper():
    """守夜线程：发现新窗口就入坞，顺带做布局兜底同步。

    任务跑不跑都收（「去完成」的作业窗口、登录窗口都算）；用户自己开的
    Chrome/Edge 由 _dock_new_windows 里的「属主 PID == 常驻浏览器」判据天然
    排除，永远不会进这里。页面/窗口关闭后句柄失效，由 IsWindow 自然清出，
    不需要专门清理。
    """
    import time
    while not DESKTOP.get('quit'):
        time.sleep(1.0)
        try:
            _dock_new_windows()
            _dock_layout()
        except Exception:
            pass


def serve_desktop(port=8765):
    """桌面模式入口：原生窗口装界面，HTTP 服务跑在后台线程。"""
    if not _claim_single_instance():
        cs.log('本工具已经在运行了，正在把它的窗口叫到前面…')
        if _wake_existing():
            cs.log('已唤起已有窗口，本次不重复启动。')
        else:
            cs.log('已有实例在运行但没能唤起，请在任务栏里找「%s」。' % WIN_TITLE,
                   'warn')
        return 0

    try:
        import webview            # 延迟导入：网页模式与命令行不依赖它
    except Exception as e:
        cs.log('缺 pywebview（%s），桌面窗口用不了。' % type(e).__name__, 'err')
        cs.log('退路：python chaoxing_scanner.py gui', 'warn')
        return 1

    # 上一轮（同进程二次启动的潜伏场景）留下的 force 会让关窗守卫
    # 失效，这里统一复位再开张。
    DESKTOP['force'] = False
    # 浏览器收纳：同进程二次启动也要复位。v4.3 起改用「属主 PID == 常驻浏览器」
    # 判据（不再需要启动基线快照——用户运行中点开的浏览器靠 PID 天然排除）。
    with LOCK:
        DESKTOP['quit'] = False
        DESKTOP['docked'] = []
        DESKTOP['styles'] = {}
        DESKTOP['dock_rect'] = None
        DESKTOP['dock_on'] = False
    httpd, port = _open_server(port)
    cs.RUNTIME.mkdir(parents=True, exist_ok=True)
    with LOCK:
        STATE['_server'] = httpd
    threading.Thread(target=httpd.serve_forever, name='ui-http',
                     daemon=True).start()
    url = 'http://127.0.0.1:%d/' % port
    DESKTOP['url'] = url
    DESKTOP['mode'] = True
    # 浏览器一律屏外启动（见 cs.OFFSCREEN_LAUNCH 注释）：有头启动会先在
    # 桌面正中闪一下、再被守夜线程收进预览——真机 2026-10-10「点查询
    # 弹个窗口又秒没」。屏外启动后由守夜线程直接入坞，桌面不再闪窗。
    cs.OFFSCREEN_LAUNCH = True
    _write_ui_record(port)
    threading.Thread(target=_browser_keeper, name='browser-keeper',
                     daemon=True).start()
    cs.log('')
    cs.log('界面已就绪（%s，只监听本机）' % url)
    cs.log('用完退出：直接关掉这个窗口（右上角 ×）。')

    storage = cs.RUNTIME / 'webview'
    storage.mkdir(parents=True, exist_ok=True)
    code = 0
    try:
        # create_window 也罩进 try：此时服务已起、ui.json 已写，
        # 它若抛异常，finally 的 _teardown_ui 必须照样跑（清理记录）。
        win = webview.create_window(WIN_TITLE, url, width=1280, height=820,
                                    min_size=(980, 640))
        win.events.closing += _on_window_closing
        DESKTOP['win'] = win
        webview.start(gui='edgechromium', private_mode=False,
                      storage_path=str(storage))
    except Exception as e:
        # 例如系统缺 WebView2 运行时。说清退路，别让用户对着黑窗口猜。
        cs.log('桌面窗口起不来（%s）。' % type(e).__name__, 'err')
        cs.log(str(e)[:400], 'err')
        cs.log('退路：改用命令  python chaoxing_scanner.py gui  打开网页界面。',
               'warn')
        code = 1
    finally:
        _teardown_ui()
    return code


def _pick_port(start: int, tries: int = 20) -> int:
    for p in range(start, start + tries):
        with socket.socket() as s:
            try:
                s.bind(('127.0.0.1', p))
                return p
            except OSError:
                continue
    raise RuntimeError('找不到可用端口')


def _open_server(start: int, tries: int = 20):
    """直接试绑：探测与正式绑定之间没有可被抢的窗口（_pick_port 的
    「bind 后关闭再重绑」留了 TOCTOU 窗口，端口可能被别的进程抢走，
    让用户对着裸 traceback）。返回 (httpd, port)。"""
    last = None
    for p in range(start, start + tries):
        try:
            return _StrictHTTPServer(('127.0.0.1', p), Handler), p
        except OSError as e:
            last = e
    raise RuntimeError('找不到可用端口（%s…%s）：%s'
                       % (start, start + tries - 1, last))


def _shutdown_async(delay: float = 0.35):
    """稍等片刻再关停，让 /api/quit 的响应先发出去。

    shutdown() 必须由 serve_forever() 之外的线程调用，所以这里单开线程。
    """
    def _bye():
        time.sleep(delay)
        # 桌面模式：先关原生窗口。置 force 是因为「退出程序」是明确意图，
        # 不该再被 _on_window_closing 那条「任务在跑就别关」拦住
        # —— 拦住的后果是窗口关不掉、服务已停，用户对着一个死窗口。
        # CANCEL 在 /api/quit 里已经置过了，任务这边很快就能收尾。
        if DESKTOP.get('win') is not None:
            DESKTOP['force'] = True
        # 网页模式也要等任务收尾：主线程一退，daemon 任务线程被硬杀，
        # playwright 的 driver 进程会留在系统里（见 _wait_task 的说明）。
        _wait_task(delay=0.2)
        # v4.3：浏览器是全局常驻的，退出时必须显式收掉，否则那只 chrome
        # 会一直留在系统里（用户以为「关了工具」，其实还有个进程占着内存）。
        try:
            cs.stop_browser()
        except Exception:
            pass
        if DESKTOP.get('win') is not None:
            try:
                DESKTOP['win'].destroy()
            except Exception:
                pass
        with LOCK:
            srv = STATE.get('_server')
        if srv is not None:
            try:
                srv.shutdown()
            except Exception:
                pass
    threading.Thread(target=_bye, name='ui-shutdown', daemon=True).start()


def serve(port=8765, open_browser=True):
    httpd, port = _open_server(port)
    cs.RUNTIME.mkdir(parents=True, exist_ok=True)
    with LOCK:
        STATE['_server'] = httpd
    url = 'http://127.0.0.1:%d/' % port
    cs.log('')
    cs.log('图形控制台已启动：%s' % url)
    cs.log('（只监听本机 127.0.0.1，不会对外网开放）')
    cs.log('用完退出：直接关闭这个命令行窗口（Ctrl+C）。')
    cs.log('注意：只关浏览器页面不会退出，后台仍在占用端口。')
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        cs.log('收到 Ctrl+C，正在退出…')
    finally:
        # Ctrl+C 直接退出也要给任务线程收尾时间（与 /api/quit 同一套
        # 纪律）：不等就走，playwright 的 driver 会留在系统里。
        _wait_task(delay=0.1, timeout=10.0)
        try:
            httpd.server_close()
        except Exception:
            pass
        with LOCK:
            STATE['_server'] = None
        cs.log('程序已退出，端口 %d 已释放。' % port)


if __name__ == '__main__':
    serve()
