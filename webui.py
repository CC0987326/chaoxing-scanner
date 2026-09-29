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
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

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
    'started': 0.0,
    'login_fail': None,     # 密码登录被平台明确回绝 → 'credential'，界面弹红字提示
    'result_v': 0,          # 结果的版本号：变了界面才重画，见 _set_result()
    'ares': None,           # 交卷结果面板（逐份状态，未确认行可「重新核实」）
    'ares_v': 0,
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
    'brush': '自动刷视频（2 倍速静音）',
    'answer': '做作业并交卷（大模型答题）',
    'combo': '刷课 + 刷题（先刷视频再做作业交卷）',
    'verify': '重新核实未确认的作业',
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
    """「💡 熄屏」：立刻强制关闭显示器。

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


def _spawn(fn, *args):
    """任务线程统一入口：运行期间阻止系统自动睡眠（屏幕仍可关）。

    刷课/答题动辄半小时起步，Windows 电源计划到点会把机器睡掉，
    任务就断在半路。这里在任务线程内持有「阻止睡眠」状态，
    结束（含异常）时恢复。SetThreadExecutionState 按线程持有，
    所以必须包在任务线程里开关，不能在主线程调一次完事。
    """
    def run():
        cs.keep_awake(True)
        try:
            fn(*args)
        finally:
            cs.keep_awake(False)
            # 兜底复位：worker 若在进入自己的 try 之前就抛了（如请求里
            # limit 填了非数字），它的 finally 不会执行，running 永远
            # 停在 True，之后所有任务按钮都 409 锁死，只能重启程序。
            # 正常收尾时 running 已是 False，这里是幂等空操作。
            with LOCK:
                if STATE['running']:
                    STATE.update(running=False, done=True,
                                 paused=False, phase='')
                    cs.log('检测到任务异常收尾，已复位任务状态。', 'warn')
    threading.Thread(target=run, daemon=True).start()


def _worker(action, opts):
    phone = (opts.get('phone') or '').strip()
    password = opts.get('password') or ''
    limit = int(opts.get('limit') or 0)
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
        STATE['phase'] = 'listing' if action in ('list', 'selftest') else 'scanning'
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
            # 否则一点按钮就登回旧账号，用户根本没机会扫新码（实测踩过）
            ok = cs.do_login(cfg, auto=False, fresh=True)
            with LOCK:
                STATE['summary'] = {'登录': '成功 ✓' if ok else '未完成'}
            return

        # 「获取课程列表」和「查询」都要先登录，共用同一套逻辑
        if action in ('query', 'list'):
            if phone and password:
                cs.log('使用账号密码登录 …')
                if not cs.do_login_password(cfg, phone, password):
                    if cs.last_login_fail() == 'credential':
                        # 平台明确回绝了这组账号密码。这种失败在界面上用红字讲清楚就够了，
                        # 不必再抛异常打一堆堆栈（那样用户只会看到「任务失败」+ traceback）。
                        # 同时清掉上一次的旧结果：否则结果区里那份旧报告会一直留着，
                        # 用户会以为那是这次查出来的东西。
                        with LOCK:
                            STATE['login_fail'] = 'credential'
                        _set_result(None)
                        cs.log('账号或密码错误。请核对账号（手机号 / 超星号）与密码后重试，'
                               '或改用左侧「扫码 / 短信登录」。', 'err')
                        return
                    raise RuntimeError(
                        '登录未成功。请核对手机号与密码，'
                        '或改用左侧「扫码 / 短信登录」。（密码连续输错可能触发账号保护）')
            else:
                cs.log('未填账号密码，尝试使用已保存的登录会话 …')

        if action == 'list':
            # 只读名录，不注入控制钩子（避免界面按钮给出无效承诺）
            cs.set_control_hook(None)
            out = cs.list_courses(cfg)
            items = out.get('courses') or []
            with LOCK:
                STATE['courses'] = items
                STATE['summary'] = {'课程总数': len(items)}
            if items:
                cs.log('课程列表已就绪：勾选要查询的课程（勾上即生效），再点「一键查询」。', 'ok')
                cs.log('不勾选 = 照旧查全部。')
            return

        out = cs.scan(cfg, limit=limit, only=only,
                      headless=None, open_report=False, deep=deep)
        rep = out.get('report') or {}
        with LOCK:
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
                STATE['summary']['⚠ 页面未加载'] = nf
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
        rate = min(max(float(cfg.get('brush_rate', 2) or 2), 1.0), 16.0)
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
        cs.log('想核对结果：重新点「一键查询」，报告里消失的章节节点就是刷完的。', 'ok')
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
    （原来的「暂存预演」已按需求移除：平台没有「只存答案不记完成」的
    暂存，预演清单反而多一道手续——现在一步到位直接交卷。）
    """
    only = [str(k) for k in (opts.get('only') or []) if k]
    cs.log('')
    cs.log('▶ 任务开始：做作业并交卷（答完直接提交）')
    PAUSE.clear()
    CANCEL.clear()
    with LOCK:
        STATE['paused'] = False
        STATE['phase'] = 'answering'
    try:
        cfg = cs.load_config()
        if not (cfg.get('llm_url') and cfg.get('llm_key') and cfg.get('llm_model')):
            cs.log('还没有配置大模型：请在左侧「大模型答题设置」里填接口地址、'
                   'API Key 和模型名，保存后再试。', 'err')
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

        out = cs.answer_courses(cfg, only, submit=True,
                                progress=progress, control=control)
        with LOCK:
            STATE['summary'] = {'交卷': out.get('submitted', 0),
                                '未确认': out.get('unverified', 0),
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
            for it in ares.get('items', []):
                s = st.get((it.get('key'), it.get('kid')))
                if s:
                    it['status'] = s
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
        if not (cfg.get('llm_url') and cfg.get('llm_key') and cfg.get('llm_model')):
            cs.log('还没有配置大模型：刷视频不受影响，但刷题需要先在左侧'
                   '「大模型答题设置」里填好接口。', 'err')

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
        rate = min(max(float(cfg.get('brush_rate', 2) or 2), 1.0), 16.0)
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


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>学习通巡检工具</title>
<style>
  *{box-sizing:border-box;}
  :root{
    --bg:#0e1416; --panel:#141c1f; --panel2:#111a1c; --line:#22343a;
    --tx:#d9e3e4; --mut:#7f9397; --ac:#4fb3a8; --ac2:#8fd6cd;
    --warn:#d8a657; --err:#e0776f; --ok:#6fbf8f;
  }
  html,body{height:100%;}
  body{margin:0;background:var(--bg);color:var(--tx);
       font:14px/1.6 "Microsoft YaHei","Segoe UI",system-ui,sans-serif;
       display:flex;flex-direction:column;}
  header{padding:16px 26px 13px;border-bottom:1px solid var(--line);
         background:linear-gradient(180deg,#131d20,#0e1416);}
  h1{margin:0;font-size:17px;font-weight:600;letter-spacing:.5px;}
  h1 small{color:var(--mut);font-weight:400;font-size:12px;margin-left:10px;}
  .wrap{flex:1;display:flex;min-height:0;}
  .side{width:316px;flex:0 0 316px;border-right:1px solid var(--line);
        padding:16px;overflow:auto;background:var(--panel2);}
  .main{flex:1;display:flex;flex-direction:column;min-width:0;}
  .btn{display:block;width:100%;padding:11px 14px;margin-bottom:8px;
       background:#1b2629;color:var(--tx);border:1px solid var(--line);
       border-radius:7px;font-size:14px;cursor:pointer;text-align:left;
       transition:.16s;font-family:inherit;}
  .btn:hover:not(:disabled){background:#22323a;border-color:#2f4a52;}
  .btn:disabled{opacity:.4;cursor:not-allowed;}
  .btn.primary{background:linear-gradient(180deg,#1f5a55,#17443f);
               border-color:#2b6f68;color:#e6f5f3;font-weight:600;}
  .btn.primary:hover:not(:disabled){background:linear-gradient(180deg,#246964,#1a4f49);}
  .btn i{font-style:normal;margin-right:8px;opacity:.85;}
  .lbl{font-size:12px;color:var(--mut);margin:14px 0 6px;letter-spacing:.4px;}
  input[type=text],input[type=password],input[type=number]{
       width:100%;padding:9px 11px;background:#0d1517;color:var(--tx);
       border:1px solid var(--line);border-radius:6px;font-size:13px;
       font-family:inherit;outline:none;}
  input:focus{border-color:#2f5a5f;background:#0b1315;}
  .pwdwrap{position:relative;}
  .pwdwrap input{padding-right:38px;}
  .eyebtn{position:absolute;right:5px;top:50%;transform:translateY(-50%);
          width:28px;height:28px;border:none;background:transparent;
          color:var(--mut);font-size:15px;cursor:pointer;border-radius:5px;
          display:flex;align-items:center;justify-content:center;
          filter:grayscale(1) opacity(.75);}
  .eyebtn:hover{background:#152226;color:var(--tx);filter:none;}
  .eyebtn.on{filter:none;}
  .chk{display:flex;align-items:flex-start;gap:8px;font-size:12.5px;
       color:#9fb0b3;margin:10px 0 4px;cursor:pointer;line-height:1.5;}
  .chk input{margin-top:3px;accent-color:#4fb3a8;}
  .status{display:flex;align-items:center;gap:8px;font-size:12.5px;
          color:var(--mut);margin-bottom:14px;}
  .dot{width:8px;height:8px;border-radius:50%;background:#4a5c60;flex:0 0 8px;}
  .dot.on{background:var(--ac);box-shadow:0 0 0 3px rgba(79,179,168,.16);
          animation:pulse 1.4s infinite;}
  @keyframes pulse{50%{opacity:.45;}}
  .hr{border:0;border-top:1px solid var(--line);margin:16px 0 4px;}
  .cards{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:6px;}
  .card{background:#101a1c;border:1px solid var(--line);border-radius:7px;
        padding:9px 11px;}
  .card b{display:block;font-size:19px;font-weight:600;color:var(--ac2);}
  .card span{font-size:11.5px;color:var(--mut);}
  .tabs{display:flex;gap:2px;padding:0 20px;border-bottom:1px solid var(--line);
        background:var(--panel);}
  .tab{padding:11px 16px;font-size:13px;color:var(--mut);cursor:pointer;
       border:0;background:none;border-bottom:2px solid transparent;
       font-family:inherit;}
  .tab.on{color:var(--ac2);border-bottom-color:var(--ac);}
  .pane{flex:1;overflow:auto;min-height:0;}
  #log{padding:14px 22px 40px;margin:0;
       font:12.5px/1.75 "Cascadia Mono",Consolas,"Courier New",monospace;
       white-space:pre-wrap;word-break:break-all;}
  #log div{color:#b9c7c9;}
  #log div.warn{color:var(--warn);}
  #log div.err{color:var(--err);}
  #log div.ok{color:var(--ok);}
  .bar{display:flex;align-items:center;gap:10px;padding:9px 20px;
       border-bottom:1px solid var(--line);background:var(--panel);}
  .bar .ttl{font-size:13px;color:var(--mut);}
  .bar .sp{flex:1;}
  .mini{padding:5px 11px;font-size:12px;background:#1b2629;color:var(--tx);
        border:1px solid var(--line);border-radius:6px;cursor:pointer;
        font-family:inherit;}
  .mini:hover{background:#22323a;}
  #res{padding:18px 22px 40px;}
  #res h2{font-size:15px;margin:0 0 4px;font-weight:600;}
  #res h2 .n{color:var(--ac2);}
  #res .sec{margin-bottom:26px;}
  #res table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px;}
  #res th{text-align:left;font-weight:500;color:var(--mut);font-size:12px;
          padding:7px 9px;border-bottom:1px solid var(--line);}
  #res td{padding:8px 9px;border-bottom:1px solid #1a2528;vertical-align:top;}
  #res tr:hover td{background:#121b1e;}
  #res a{color:var(--ac2);text-decoration:none;white-space:nowrap;}
  #res a:hover{text-decoration:underline;}
  #res .bad{color:var(--err);font-weight:600;}
  #res .sub{font-size:12px;color:var(--mut);}
  #res .chap{margin:6px 0 0 2px;font-size:12.5px;}
  #res .chap div{padding:3px 0;border-bottom:1px dashed #1c272a;color:#b9c7c9;}
  #res .chap b{color:#e2c88a;font-weight:600;}
  #res .empty{color:var(--mut);font-size:13px;padding:6px 0;}
  /* 账号密码错误：结果区的红字提示（跟底部 toast 一起出现） */
  #res .errbox{border:1px solid var(--err);border-radius:8px;padding:11px 13px;
               background:rgba(224,119,111,.08);}
  #res .errbox b{display:block;color:var(--err);font-size:13.5px;margin-bottom:4px;}
  #res .errbox span{color:var(--mut);font-size:12.5px;line-height:1.7;}
  /* 未完成任务点：一门课的章节明细能拉出十几行，所以按「每门课一个折叠」收起明细，
     课程名与进度始终可见——要缩短的是长度，不是把整节藏起来。 */
  #res details.cbox{border:1px solid var(--line);border-radius:8px;
                    padding:10px 13px;margin-top:9px;background:#101a1c;}
  #res details.cbox > summary{display:flex;align-items:center;gap:7px;cursor:pointer;
                              user-select:none;list-style:none;}
  #res details.cbox > summary::-webkit-details-marker{display:none;}
  #res details.cbox > summary::before{content:'▸';color:var(--mut);font-size:11px;
                                      transition:transform .15s;}
  #res details.cbox[open] > summary::before{transform:rotate(90deg);}
  #res details.cbox > summary .hd{flex:1;min-width:0;font-size:13.5px;}
  #res details.cbox > summary:hover .hd b{color:var(--ac2);}
  #res details.cbox .foldhint{flex:none;color:var(--mut);font-size:12px;}
  #res details.cbox .foldhint .t-open{display:none;}
  #res details.cbox[open] .foldhint .t-closed{display:none;}
  #res details.cbox[open] .foldhint .t-open{display:inline;}
  #res .tip{font-size:12px;color:var(--mut);line-height:1.75;}
  /* 刷视频入口：节标题下的一条工具栏 + 每门课折叠条右侧的勾选 */
  #res .brushbar{display:flex;align-items:center;gap:8px;margin:10px 0 2px;flex-wrap:wrap;}
  #res .brushbar .sub{flex:1;min-width:180px;}
  #res details.cbox summary .bkwrap{flex:none;display:flex;align-items:center;gap:5px;
                                    font-size:12px;color:#9fb0b3;cursor:pointer;
                                    padding:3px 8px;border:1px solid var(--line);
                                    border-radius:6px;background:#0d1517;}
  #res details.cbox summary .bkwrap:hover{border-color:var(--ac);color:var(--ac2);}
  #res details.cbox summary .bkwrap input{accent-color:var(--ac);margin:0;cursor:pointer;}
  /* 章节级勾选（课程折叠里每个章节行左侧的小勾） */
  #res .ckw{display:inline-flex;align-items:center;margin-right:6px;cursor:pointer;
            padding:1px 6px;border:1px solid var(--line);border-radius:5px;
            background:#0d1517;vertical-align:middle;}
  #res .ckw:hover{border-color:var(--ac);}
  #res .ckw input{accent-color:var(--ac);margin:0;cursor:pointer;}
  /* 课程勾选面板 */
  .pickhead{display:flex;align-items:center;gap:6px;margin:7px 0 0;
            font-size:11.5px;color:var(--mut);}
  .pickhead .sp{flex:1;}
  .pickrow{display:flex;align-items:center;gap:8px;margin-top:6px;}
  .mini.primary{background:linear-gradient(180deg,#1f5a55,#17443f);
                border-color:#2b6f68;color:#e6f5f3;font-weight:600;}
  .mini.primary:hover{background:linear-gradient(180deg,#246964,#1a4f49);}
  .mini.attn{animation:attn 1s ease-in-out 2;}
  @keyframes attn{50%{box-shadow:0 0 0 3px rgba(79,179,168,.4);}}
  #cok{font-size:12px;line-height:1.4;color:var(--mut);}
  #cok.ok{color:var(--ok);}
  #cok.warn{color:var(--warn);}
  .pick{border:1px solid var(--line);border-radius:7px;background:#0d1517;
        max-height:210px;overflow:auto;margin-top:6px;}
  .pick label{display:flex;gap:8px;align-items:flex-start;padding:7px 10px;
              font-size:12.5px;color:#b9c7c9;cursor:pointer;line-height:1.45;
              border-bottom:1px solid #162124;}
  .pick label:last-child{border-bottom:0;}
  .pick label:hover{background:#121b1e;}
  .pick input{appearance:none;-webkit-appearance:none;flex:0 0 16px;
              width:16px;height:16px;margin:1px 0 0;border:1px solid #3a5257;
              border-radius:4px;background:#0a1113;cursor:pointer;
              position:relative;transition:background .14s,border-color .14s;}
  .pick input:hover{border-color:var(--ac);}
  .pick input:checked{background:var(--ac);border-color:var(--ac);}
  .pick input:checked::after{content:'';position:absolute;left:4.5px;top:1px;
              width:4px;height:9px;border:solid #0e1416;border-width:0 2px 2px 0;
              transform:rotate(45deg);}
  .pick input:focus-visible{outline:2px solid var(--ac2);outline-offset:1px;}
  .pick span{flex:1;word-break:break-word;}
  /* 未勾选的课程压暗，让「这次要查哪几门」一眼看得出来。
     但别压太狠：实测 opacity .4 + #6b7b7e 在深色底上几乎读不出字，
     用户反馈「看不清」。改成温和压暗，区分度靠勾选框本身也够了。 */
  .pick label.dim{opacity:.7;}
  .pick label.dim span{color:#96a6a9;}
  .pick label.dim:hover{opacity:.92;}
  /* 点「置顶已选的课」后把已勾选的排到最前，用分隔线标明下面是不查的 */
  .pick .sep{padding:5px 10px;font-size:11px;color:#5e7074;letter-spacing:.4px;
             background:#101a1c;border-bottom:1px solid #162124;}
  /* 页面内提示条：比 alert 可靠，不会被浏览器「阻止更多对话框」静默吞掉 */
  .toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%) translateY(14px);
         background:#1d2b2f;border:1px solid var(--line);color:var(--tx);
         padding:10px 18px;border-radius:8px;font-size:13px;max-width:72vw;
         opacity:0;pointer-events:none;transition:opacity .2s,transform .2s;
         z-index:99;box-shadow:0 6px 22px rgba(0,0,0,.45);}
  .toast.show{opacity:1;transform:translateX(-50%) translateY(0);}
  .toast.warn{border-color:var(--warn);color:var(--warn);}
  .toast.err{border-color:var(--err);color:var(--err);}
  .toast.ok{border-color:var(--ok);color:var(--ok);}
</style>
</head>
<body>
<header>
  <h1>学习通巡检工具<small>查询只读 · 刷课/答题需手动触发 · 数据只在本机</small></h1>
</header>
<div class="wrap">
  <div class="side">
    <div class="status"><span class="dot" id="dot"></span><span id="stxt">空闲</span></div>

    <div class="lbl">账号（手机号 / 超星号）</div>
    <input type="text" id="phone" placeholder="例 198xxxxxxxx" autocomplete="username">
    <div class="lbl">密码</div>
    <div class="pwdwrap">
      <input type="password" id="pwd" placeholder="学习通登录密码" autocomplete="current-password">
      <button type="button" class="eyebtn" id="beye" title="显示 / 隐藏密码"
              aria-label="显示或隐藏密码">👁</button>
    </div>
    <label class="chk"><input type="checkbox" id="remember"> 记住账号（只记手机号，不记密码）</label>
    <div class="lbl">&nbsp;</div>
    <button class="btn primary" id="bquery"><i>▶</i>一键查询未完成事项</button>
    <label class="chk"><input type="checkbox" id="deep" checked> 下钻到章节，列出未完成任务点</label>

    <hr class="hr">
    <button class="btn" id="blogin"><i>◐</i>扫码 / 短信登录</button>

    <hr class="hr">
    <div class="lbl">只查最近 N 门课（留空 = 全部）</div>
    <input type="number" id="recentTop" min="1" placeholder="例如 5">
    <div class="tip" style="margin-top:6px">
      若在下面勾选了具体课程（勾上即生效），则以勾选为准，这里填的数会被忽略。
    </div>

    <div class="lbl">要查哪些课？（勾上就生效；一门都不勾 = 全部）</div>
    <button class="btn" id="blist"><i>☰</i>获取课程列表</button>
    <div id="cwrap" style="display:none">
      <div class="pickhead">
        <span id="cnum">尚未选择</span>
        <span class="sp"></span>
        <button class="mini" id="call" title="勾选当前显示出来的课程">全选</button>
        <button class="mini" id="cnone" title="取消当前显示出来的勾选">清空</button>
      </div>
      <input type="text" id="cfilter" placeholder="筛选课程名…" style="margin-top:6px">
      <div class="pickrow">
        <button class="mini primary" id="cpin"
                title="把已勾选的课程排到列表最上面，方便核对">置顶已选的课</button>
        <span id="cok"></span>
      </div>
      <div class="pick" id="clist"></div>
    </div>
    <hr class="hr">
    <div class="lbl">运行中控制（随时可用）</div>
    <button class="btn" id="bpause" disabled><i>⏸</i>暂停</button>
    <button class="btn" id="bstop" disabled style="border-color:#a33;color:#e88"><i>■</i>停止本次查询</button>
    <div class="tip" style="margin-top:6px">
      暂停和停止都在这门课抓完后生效（一般几秒内）。
      停止不会丢结果：已扫完的课程照常出报告，只是标注为「中止扫描」。
      <br>停止后会有几秒在生成报告（状态显示「收尾中」），之后即可再次查询。
    </div>

    <div class="lbl">输出</div>
    <button class="btn" id="bopen"><i>▤</i>打开最新报告</button>
    <button class="btn" id="bdir"><i>▣</i>打开输出文件夹</button>

    <hr class="hr">
    <div class="lbl">大模型答题设置</div>
    <input class="inp" id="llm_url" placeholder="接口地址，如 https://api.xx.com/v1" style="margin-bottom:6px">
    <input class="inp" id="llm_key" placeholder="API Key" style="margin-bottom:6px">
    <input class="inp" id="llm_model" placeholder="模型名，如 deepseek-chat" style="margin-bottom:6px">
    <button class="btn" id="bllm"><i>✓</i>保存大模型设置</button>
    <button class="btn" id="bllmremember" style="margin-top:6px"><i>💾</i>记住模型信息（下次打开免填写）</button>
    <button class="btn" id="bllmtest" style="margin-top:6px"><i>⚡</i>验证连通</button>
    <div class="tip" style="margin-top:6px">
      任何 OpenAI 兼容接口都能用（填到 /v1 为止）。Key 只保存在本机 config.json。
      「做作业」用你自己的 Key 按量计费，一份选择题作业通常只花几分钱。
      填完先「验证连通」，通过后「记住模型信息」，以后打开不用再填。
    </div>

    <hr class="hr">
    <div class="lbl">退出</div>
    <button class="btn" id="bquit" style="border-color:#a33;color:#e88"><i>⏻</i>退出程序（释放端口）</button>

    <div id="cards" class="cards" style="display:none"></div>

    <div class="lbl">说明</div>
    <div class="tip">
      首次使用请填账号密码点「一键查询」；若密码登录被平台拦下，
      会自动提示你改用「扫码 / 短信登录」。
      登录一次后会话会保存在本机，之后无需重复登录。
      <br><br>
      <b>只想查几门课：</b>先点「获取课程列表」（约 10 秒，只读名单），
      然后在下方勾选要查的课程，再点「一键查询」。
      一门都不勾 = 照旧查全部。
      <br><br>
      <b>用完怎么退出：</b>点上面的「退出程序」，或者直接关掉那个黑色命令行窗口。
      只关浏览器页面是不会退出的，后台程序还在跑、端口还占着。
      <br><br>
      如果有问题或者建议，可联系 QQ：2030161963<br>
      最新版下载地址：<a href="https://github.com/CC0987326/chaoxing-scanner"
        target="_blank" rel="noopener"
        style="color:var(--ac2);text-decoration:none;overflow-wrap:anywhere;"
        >https://github.com/CC0987326/chaoxing-scanner</a>
    </div>
  </div>

  <div class="main">
    <div class="tabs">
      <button class="tab on" data-t="res">查询结果</button>
      <button class="tab" data-t="log">运行日志</button>
    </div>
    <div class="bar">
      <span class="ttl" id="bartip">尚未查询</span>
      <span class="sp"></span>
      <button class="mini" id="boff">💡 熄屏</button>
      <button class="mini" id="bclear">清空日志</button>
    </div>
    <div class="pane" id="res"><div class="empty">还没有结果。填好账号密码后点左侧「一键查询未完成事项」。</div></div>
    <div class="pane" id="logpane" style="display:none"><pre id="log"></pre></div>
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
let aitems = [];        // 交卷结果面板的数据（重新核实按钮要用）
let cardFp = '';        // 左侧统计卡片的指纹：内容没变就不重建 DOM
const $ = id => document.getElementById(id);
const logEl = $('log');

// 只在文字真的变了才写 DOM：轮询每几百毫秒来一次，无脑赋值会白白触发重排
function setText(el, v){
  const s = String(v == null ? '' : v);
  if (el.textContent !== s) el.textContent = s;
}

function toast(msg, kind){
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast show' + (kind ? ' ' + kind : '');
  clearTimeout(t._h);
  t._h = setTimeout(() => { t.className = 'toast'; }, kind === 'err' ? 7000 : 4500);
}

// 记住手机号
try {
  const saved = localStorage.getItem('cx_phone');
  if (saved) { $('phone').value = saved; $('remember').checked = true; }
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

// ---------- 课程勾选 ----------
// 勾选即生效：勾上哪几门，这次就查哪几门，不需要再点一次确认。
let courseFp = '';

function boxes(){ return [...document.querySelectorAll('#clist input')]; }
function checkedKeys(){ return boxes().filter(b => b.checked).map(b => b.value); }
function picked(){ return checkedKeys(); }        // 提交给后端的课程范围

function setCok(text, kind){
  const el = $('cok');
  el.textContent = text || '';
  el.className = kind || '';
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

  setCok(n ? ('✓ 已选中 ' + n + ' 门课')
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

function renderResult(r){
  if (!r) return;
  // 任务以异常收尾时结果里只有 error：把「查询中…」的占位换掉，
  // 别让用户停在无限转圈的假象里（失败详情在「运行日志」页签）
  if (r.error){
    $('res').innerHTML = '<div class="errbox"><b>任务失败</b>'
      + '<span>' + esc(r.error) + '<br>详情见「运行日志」页签；'
      + '可修正后重试（未登录时先登录）。</span></div>';
    return;
  }
  const hw = r.undone_hw || [], ex = r.undone_exam || [], pg = r.undone_prog || [];
  const sda = document.getElementById('sdafter');
  if (sda) sdAfterOn = sda.checked;
  // 结果区每 900ms 会被轮询整个重绘一次（innerHTML 覆盖），<details> 的展开状态会跟着
  // 一起丢掉——用户点开看一眼，一秒后自己又合上了。重绘前先记下哪些课是展开的，重绘时
  // 按原样补回去，展开与否就变成「用户的决定」而不是「上一次重绘的副作用」。
  // 用索引当 key：课程名会重复，不能当 key。
  const openIdx = new Set();
  document.querySelectorAll('#res details.cbox').forEach(d => {
    if (d.open) openIdx.add(d.getAttribute('data-idx'));
  });
  let h = '';
  if (r.partial){
    h += '<div class="sub" style="color:#d8a657;margin-bottom:10px">'
       + '⚠️ 本次是中止扫描：只检查了 ' + (r.scanned||0) + '/' + (r.course_total||0)
       + ' 门课程，未列出的课程是「没查」而不是「已完成」。</div>';
  } else if (r.selected_only){
    const isRecent = r.scope_kind === 'recent';
    h += '<div class="sub" style="color:#d8a657;margin-bottom:10px">'
       + '⚠️ 本次只检查了' + (isRecent ? '最近学习的 ' : '你勾选的 ')
       + (r.course_total||0) + ' 门课程'
       + '（账号共 ' + (r.account_total||0) + ' 门）。'
       + (isRecent ? '更早的课程没查' : '未勾选的课没查') + '，不在下表范围内。</div>';
  }
  // 页面没加载成功的课必须显式警告——v2.5 的教训是把它们静默报成
  // 「无作业模块」，用户拿着假报告以为没作业。没失败时这块完全不出现。
  if (r.page_failed && r.page_failed.length){
    h += '<div class="sub" style="color:#e06c60;margin-bottom:10px">'
       + '⚠️ 有 ' + r.page_failed.length + ' 门课的页面没有加载成功（'
       + esc(r.page_failed.map(function(x){ return x.course; }).join('、'))
       + '），这些课的作业/考试是「没查到」而不是「没有」，建议稍后重新查询。</div>';
  }
  h += '<div class="sub" style="margin-bottom:16px">生成时间 ' + esc(r.time)
     + '　·　' + (r.partial
        ? ('已扫描 ' + (r.scanned||0) + ' / ' + (r.course_total||0) + ' 门课程')
        : (r.selected_only
           ? ((r.scope_kind === 'recent' ? '已查最近 ' : '已勾选 ')
              + (r.course_total||0) + ' 门（账号共 ' + (r.account_total||0) + ' 门）')
           : ('共扫描 ' + (r.course_total||0) + ' 门课程'))) + '</div>';

  h += '<div class="sec"><h2>一、未完成作业 <span class="n">' + hw.length + '</span> 项</h2>';
  if (hw.length){
    h += '<table><tr><th>课程</th><th>作业名称</th><th>状态</th><th>剩余</th><th></th></tr>';
    for (const it of hw){
      h += '<tr><td>' + esc(it.course) + '</td><td>' + esc(it.title)
         + '</td><td class="bad">' + esc(it.state) + '</td><td class="sub">'
         + esc(it.left || '—') + '</td><td>'
         + (safeUrl(it.url) ? '<a href="' + safeUrl(it.url) + '" target="_blank" rel="noopener">去完成 ↗</a>' : '')
         + '</td></tr>';
    }
    h += '</table>';
  } else { h += '<div class="empty">没有未完成的作业。</div>'; }
  h += '</div>';

  h += '<div class="sec"><h2>二、未完成考试 <span class="n">' + ex.length + '</span> 项</h2>';
  if (ex.length){
    h += '<table><tr><th>课程</th><th>考试名称</th><th>状态</th></tr>';
    for (const it of ex){
      h += '<tr><td>' + esc(it.course) + '</td><td>' + esc(it.title)
         + '</td><td class="bad">' + esc(it.state) + '</td></tr>';
    }
    h += '</table>';
  } else { h += '<div class="empty">没有待完成的考试。</div>'; }
  h += '</div>';

  h += '<div class="sec"><h2>三、未完成任务点 <span class="n">' + pg.length + '</span> 门课</h2>';
  if (pg.length){
    // 任务入口：勾哪门/哪节做哪节。课程勾选框 = 该课全部待完成章节，
    // 章节勾选框 = 只做那一节；两种可以混勾，按钮决定做什么。
    h += '<div class="brushbar"><button class="mini primary" id="bbrush"'
       + ' onclick="startBrush(event)">▶ 刷选中的课的视频</button>'
       + '<button class="mini" onclick="startCombo(event)">▶ 刷课+刷题</button>'
       + '<button class="mini" style="background:#1a7f37;border-color:#1a7f37;color:#fff"'
       + ' onclick="startAnswer(event)">▶ 做作业并交卷（正式提交）</button>'
       + '<button class="mini" onclick="toggleAllBrush(event)">全选</button>'
       + '<button class="mini" onclick="screenOff(event)">💡 熄屏</button>'
       + '<label class="chk" style="margin:0;white-space:nowrap">'
       + '<input type="checkbox" id="sdafter"' + (sdAfterOn ? ' checked' : '')
       + '> 刷完自动关机</label>'
       + '<span class="sub">勾课程=全部章节；展开后可只勾某些章节。'
       + '刷视频=2 倍速静音真实播放；做作业=大模型答题后直接交卷，'
       + '同题干自动复用上次答案不重复花钱；附件/报告题会跳过并提示。'
       + '勾了自动关机：只有正常刷完才关（手动停止 / 出错不关），'
       + '关机前留 60 秒缓冲（cmd 运行 shutdown /a 可取消）。'
       + '熄屏：播放中浏览器会阻止屏幕自动关闭（Video Wake Lock），'
       + '点「熄屏」可立即强制关屏，任务照常在后台跑，动下鼠标就亮。</span></div>';
    // 每门课一个折叠：课程名 + 进度始终露出，只把章节明细收起来（一门课能拉出十几行）。
    for (let i = 0; i < pg.length; i++){
      const it = pg[i], chs = it.chapters || [], idx = String(i);
      const bkey = (it.cid && it.clsid) ? (it.cid + ':' + it.clsid) : '';
      h += '<details class="cbox"' + (openIdx.has(idx) ? ' open' : '')
         + ' data-idx="' + idx + '"><summary>'
         + '<span class="hd"><b>' + esc(it.course) + '</b>　'
         + '<span class="sub">已完成 ' + it.done + '/' + it.total + '（'
         + it.rate.toFixed(1) + '%）</span></span>'
         + '<span class="foldhint">'
         + '<span class="t-closed">'
         + (chs.length ? chs.length + ' 个章节待完成 · 点这里展开' : '点这里展开')
         + '</span><span class="t-open">点这里收起</span></span>'
         // 没有 cid/clsid 的课（旧缓存结果）压根不渲染勾选框——没有定位参数就不能假装能刷
         + (bkey
            ? '<label class="bkwrap" onclick="event.stopPropagation()" title="勾上后点上面的按钮：刷视频 / 做作业 / 刷课+刷题">'
              + '<input type="checkbox" class="bk" value="' + esc(bkey) + '"> 选中</label>'
            : '')
         + '</summary>';
      if (chs.length){
        h += '<div class="chap">';
        for (const c of chs){
          const ckb = (bkey && c.kid)
            ? '<label class="ckw" onclick="event.stopPropagation()"'
              + ' title="只勾这一节：上面的按钮就只处理这一节">'
              + '<input type="checkbox" class="ck" value="' + esc(bkey + '|' + c.kid) + '"></label>'
            : '';
          h += '<div>' + ckb + esc(c.name) + '　<b>待完成 ' + (c.count||1) + '</b></div>';
        }
        h += '</div>';
      } else {
        h += '<div class="sub" style="margin-top:6px">（未取到章节明细）</div>';
      }
      h += '</details>';
    }
  } else {
    h += '<div class="empty">所有课程的任务点都已完成。</div>';
  }
  h += '</div>';

  h += '<div class="sub">完整清单（含全部 ' + (r.course_total||0)
     + ' 门课程总览）已保存为 Markdown，可点左侧「打开最新报告」查看。</div>';
  $('res').innerHTML = h;
}

async function poll(){
  const me = ++pollSeq;          // 新循环上岗，旧的在途循环过时
  if (closed) return;
  let busy = false;
  try{
    const r = await fetch('/api/poll?since=' + since + '&rv=' + rv + '&av=' + av);
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
    if (j.lines && j.lines.length){ append(j.lines); since = j.total; }
    if (j.courses && j.courses.length) renderCourses(j.courses);
    $('dot').className = 'dot' + (j.running ? ' on' : '');
    const paused = !!j.paused;
    const finishing = j.phase === 'finishing';
    const listing = j.phase === 'listing';
    setText($('stxt'), j.running
      ? (paused ? '已暂停 · 点「继续」恢复'
                : (finishing ? '收尾中 · 正在生成报告' : ('运行中 · ' + (j.task||''))))
      : (j.done ? '已完成' : '空闲'));
    ['bquery','blogin','blist'].forEach(i => $(i).disabled = j.running);
    $('bquery').title = j.running
      ? (finishing ? '正在收尾（生成报告），稍等几秒就能再次查询' : '任务进行中，请先暂停或停止')
      : '';
    $('blist').title = j.running ? '任务进行中' : '只读取课程名单（约 10 秒，不抓作业）';
    if (j.running && finishing) $('bartip').textContent = '收尾中：正在生成报告…';
    // 读课程名录是十来秒的只读操作，暂停/停止对它无效，就别让按钮看起来能用
    $('bpause').disabled = !j.running || listing;
    $('bstop').disabled  = !j.running || listing;
    const wasPaused = $('bpause').dataset.paused === '1';
    $('bpause').dataset.paused = paused ? '1' : '0';
    // 按钮文字只在暂停状态真的翻转时才改，省掉每轮一次 innerHTML 重写
    if (wasPaused !== paused) $('bpause').innerHTML = paused ? '<i>▶</i>继续' : '<i>⏸</i>暂停';
    if (j.summary){
      const fp = JSON.stringify(j.summary);
      if (fp !== cardFp){
        cardFp = fp;
        const c = $('cards'); c.style.display = 'grid'; c.innerHTML = '';
        for (const k in j.summary){
          const e = document.createElement('div');
          e.className = 'card';
          e.innerHTML = '<b>' + esc(j.summary[k]) + '</b><span>' + esc(k) + '</span>';
          c.appendChild(e);
        }
      }
    }
    // 结果只在「换了一版」时重画。以前是无条件每轮重绘，长报告要反复重建上百行
    // DOM，正在选中的文字也会被抖掉；现在轮询只负责把新版本交过来。
    if (j.result && !j.running && j.result_v !== rv){
      rv = j.result_v;
      renderResult(j.result);
      setText($('bartip'), j.result.error ? '任务失败' : '查询完成');
    }
    // 交卷结果面板同理：任务结束才生成，换版才重画，重画时切到结果页
    // 让用户直接看到「哪些可交卷」。
    if (j.ares && !j.running && j.ares_v !== av){
      av = j.ares_v;
      renderAnswerResult(j.ares);
      setText($('bartip'), '交卷结果已生成');
      switchTab('res');
    }
    // 任务收尾时明确交代一句。否则重复点「获取课程列表」而名单没变时，
    // DOM 不会重建、界面毫无动静，用户会以为按钮坏了
    if (!j.running && j.done && j.started && j.started !== lastDone){
      lastDone = j.started;
      if (j.login_fail === 'credential'){
        // 平台明确回绝这组账号密码。只在这一种情况下报「账号或密码错误」——
        // 验证码没点对、超时等失败不报，免得把人引到「去改密码」的错路上。
        toast('账号或密码错误', 'err');
        $('res').innerHTML = '<div class="errbox"><b>账号或密码错误</b>'
          + '<span>请核对账号（手机号 / 超星号）与密码后重新查询；'
          + '密码连续输错可能触发账号保护，也可以改用左侧「扫码 / 短信登录」。</span>'
          + '</div>';
        $('bartip').textContent = '账号或密码错误';
        switchTab('res');
      } else if (j.task === '获取课程列表'){
        const n = (j.courses || []).length;
        if (n){
          toast('读到 ' + n + ' 门课程：勾选要查的课（勾上就生效），再点「一键查询」；'
                + '一门都不勾就是查全部', 'ok');
          $('bartip').textContent = '课程列表已就绪（' + n + ' 门）';
        } else {
          toast('没读到任何课程，请确认这个账号下有课程', 'err');
          $('bartip').textContent = '课程列表为空';
        }
      }
    }
    $('bopen').disabled = !j.report;
  }catch(e){}
  if (closed || me !== pollSeq) return;
  // 运行中问得密一点（日志和状态跟得更紧），空闲时疏一点，没必要一直戳
  timer = setTimeout(poll, busy ? 400 : 1200);
}
poll();

async function run(action){
  if ($('remember').checked) {
    try { localStorage.setItem('cx_phone', $('phone').value.trim()); } catch(e){}
  } else {
    try { localStorage.removeItem('cx_phone'); } catch(e){}
  }
  const nPick = picked().length;
  const nTop = parseInt($('recentTop').value || '0', 10) || 0;
  if (action === 'query' && nPick && nTop){
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
  let resp;
  try {
    resp = await fetch('/api/run', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  } catch(e) {
    toast('连不上本地程序，请确认那个黑色命令行窗口还在运行', 'err');
    return;
  }
  // 之前这里不看返回码：服务端回 409「忙」时前端照样切到「查询中」，
  // 实际什么也没跑，用户看到的就只是「点了没反应」。
  if (!resp.ok){
    if (resp.status === 409){
      toast('上一个任务还在收尾（正在生成报告），等状态变成「已完成」再点一次', 'warn');
    } else {
      toast('任务没能启动（HTTP ' + resp.status + '）', 'err');
    }
    return;
  }
  since = 0; logEl.innerHTML = '';
  $('cards').style.display = 'none';
  cardFp = '';   // 卡片已隐藏，指纹一并清掉，否则同样的统计出来时不会重新展开
  if (action === 'query') {
    const n = picked().length;
    $('res').innerHTML = '<div class="empty">'
      + (n ? ('正在查询你勾选的 ' + n + ' 门课程…')
           : (nTop ? ('正在查询最近学习的 ' + nTop + ' 门课程…')
                   : '查询进行中，请稍候…（全量通常 1~2 分钟）')) + '</div>';
    $('bartip').textContent = '查询中…';
    switchTab('res');
  } else if (action === 'list') {
    $('res').innerHTML = '<div class="empty">正在读取课程列表…（约 10 秒，不抓作业）</div>';
    $('bartip').textContent = '读取课程列表…';
    switchTab('log');
  } else {
    switchTab('log');
  }
  // 先掐掉旧轮询链，否则每点一次按钮就多一条并行的轮询，日志会重复
  clearTimeout(timer);
  poll();
}

function switchTab(t){
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('on', b.dataset.t === t));
  $('res').style.display = t === 'res' ? '' : 'none';
  $('logpane').style.display = t === 'log' ? '' : 'none';
}

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
  fetch('/api/brush', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({only: keys, shutdown: !!($('sdafter') && $('sdafter').checked)})}).then(r => {
    if (r.status === 409){ toast('当前有任务在跑，等它结束再刷', 'warn'); return; }
    if (!r.ok){ toast('刷视频没能启动（HTTP ' + r.status + '）', 'err'); return; }
    since = 0; logEl.innerHTML = '';
    $('cards').style.display = 'none'; cardFp = '';
    $('res').innerHTML = '<div class="empty">正在后台刷视频…（2 倍速静音真实播放，'
      + '进度看「运行日志」；可以随时暂停/停止）</div>';
    $('bartip').textContent = '刷视频中…';
    switchTab('log');
    clearTimeout(timer);
    poll();
  }).catch(() => toast('连不上本地程序，请确认那个黑色命令行窗口还在运行', 'err'));
}

// 刷课+刷题：先刷选中范围的视频，再对同一批范围做作业并交卷。
function startCombo(ev){
  if (ev) ev.stopPropagation();
  const keys = taskKeys();
  if (!keys.length){
    toast('先在「未完成任务点」里勾选要做的课程或章节', 'warn');
    return;
  }
  fetch('/api/combo', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({only: keys, shutdown: !!($('sdafter') && $('sdafter').checked)})}).then(r => {
    if (r.status === 409){ toast('当前有任务在跑，等它结束再做', 'warn'); return; }
    if (!r.ok){ toast('任务没能启动（HTTP ' + r.status + '）', 'err'); return; }
    since = 0; logEl.innerHTML = '';
    $('cards').style.display = 'none'; cardFp = '';
    $('res').innerHTML = '<div class="empty">正在后台「刷课+刷题」…（先刷视频，'
      + '再做作业并交卷；进度看「运行日志」，可以随时暂停/停止）</div>';
    $('bartip').textContent = '刷课+刷题中…';
    switchTab('log');
    clearTimeout(timer);
    poll();
  }).catch(() => toast('连不上本地程序，请确认那个黑色命令行窗口还在运行', 'err'));
}

function toggleAllBrush(ev){
  if (ev) ev.stopPropagation();
  const bks = [...document.querySelectorAll('#res .bk')];
  const allOn = bks.length && bks.every(b => b.checked);
  bks.forEach(b => b.checked = !allOn);
}

// 💡 熄屏：强制关显示器（Chrome 播视频的 Video Wake Lock 挡不住
// 显式的 SC_MONITORPOWER），任务照常在后台跑；动下鼠标屏幕就亮。
// 万一黑屏唤不醒：按 Win+Ctrl+Shift+B 重置显卡驱动即可恢复。
function screenOff(ev){
  if (ev) ev.stopPropagation();
  fetch('/api/screen-off', {method:'POST'})
    .then(r => { if (!r.ok) toast('熄屏请求失败（HTTP ' + r.status + '）', 'err'); })
    .catch(() => toast('连不上本地程序', 'err'));
}

// ---------- 大模型答题并交卷 ----------
function startAnswer(ev){
  if (ev) ev.stopPropagation();
  const keys = taskKeys();
  if (!keys.length){
    toast('先在「未完成任务点」里勾选要做作业的课程或章节', 'warn');
    return;
  }
  if (!confirm('确定要「做作业并交卷」吗？\n\n大模型答完会直接正式提交并记录成绩，'
      + '交卷后一般不能再改。')) return;
  fetch('/api/answer', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({only: keys})}).then(r => {
    if (r.status === 409){ toast('当前有任务在跑，等它结束再做', 'warn'); return; }
    if (!r.ok){ toast('答题没能启动（HTTP ' + r.status + '）', 'err'); return; }
    since = 0; logEl.innerHTML = '';
    $('cards').style.display = 'none'; cardFp = '';
    $('res').innerHTML = '<div class="empty">正在后台做作业并交卷…（进度看'
      + '「运行日志」；可以随时暂停/停止）</div>';
    $('bartip').textContent = '答题并交卷中…';
    switchTab('log');
    clearTimeout(timer);
    poll();
  }).catch(() => toast('连不上本地程序，请确认那个黑色命令行窗口还在运行', 'err'));
}

// ---------- 交卷结果面板 ----------
// 答题/交卷任务结束后生成：每份作业列课程、节点、状态、网址。
// 「待平台确认」= 提交请求已发出但当场没核实到受理（平台批阅状态
// 可能延迟几分钟到十几分钟才翻转，实测多次）——点「重新核实」
// 逐份重开作业卡，渲染出「已批阅」页即翻案为已交卷。
const AST = {
  done:       ['✓ 已交卷', '#6fbf8f'],
  already:    ['✓ 已交卷（此前已交）', '#6fbf8f'],
  fail:       ['交卷失败', '#e0776f'],
  unverified: ['⚠ 已发出 · 待平台确认', '#d8a657'],
  still:      ['⚠ 仍未确认', '#d8a657'],
  report:     ['需人工 · 要传附件', '#d8a657'],
  unsupported:['需人工 · 题型不支持', '#d8a657'],
  unsolved:   ['需人工 · 模型没把握', '#d8a657'],
  skip:       ['已跳过', '#7f9397'],
};

function renderAnswerResult(a){
  if (!a || !a.items) return;
  aitems = a.items;
  const uv = aitems.filter(it => it.status === 'unverified' || it.status === 'still');
  const need = aitems.filter(it => ['report','unsupported','unsolved'].includes(it.status));
  let h = '<div class="sec"><h2>交卷结果 <span class="n">' + aitems.length + '</span> 份'
       + (uv.length ? '　·　<span style="color:#d8a657">' + uv.length + ' 份待核实</span>' : '')
       + (need.length ? '　·　<span style="color:#d8a657">' + need.length + ' 份需人工</span>' : '')
       + '</h2>'
       + '<div class="sub" style="margin-bottom:12px">生成时间 ' + esc(a.time)
       + '　·　「待平台确认」= 提交已发出、平台还没翻转状态（可能延迟几分钟到十几分钟），'
       + '点「重新核实」自动翻案；也可以点开作业网址自己确认。</div>';
  if (uv.length){
    h += '<div class="brushbar"><button class="mini" style="background:#8a6d1a;'
       + 'border-color:#8a6d1a;color:#fff;font-weight:600"'
       + ' onclick="verifyAnswer(event)">↻ 重新核实未确认的（' + uv.length + ' 份）</button>'
       + '<span class="sub">逐份重开作业卡查「已批阅」，隔一分钟一轮，最多三轮，可随时停止。</span></div>';
  }
  h += '<table><tr><th>课程</th><th>作业（章节）</th><th>状态</th><th>操作</th></tr>';
  aitems.forEach((it) => {
    const st = AST[it.status] || [it.status, '#7f9397'];
    h += '<tr><td>' + esc(it.course) + '</td><td>' + esc(it.node)
       + '</td><td style="color:' + st[1] + ';font-weight:600">' + esc(st[0])
       + '</td><td style="white-space:nowrap">'
       + (safeUrl(it.url) ? '<a href="' + safeUrl(it.url) + '" target="_blank" rel="noopener">打开作业 ↗</a>' : '')
       + '</td></tr>';
  });
  h += '</table></div>';
  $('res').innerHTML = h;
}

async function verifyAnswer(ev){
  if (ev) ev.stopPropagation();
  const list = aitems.filter(it => it.status === 'unverified' || it.status === 'still');
  if (!list.length){ toast('没有待核实的作业', 'warn'); return; }
  try{
    const r = await fetch('/api/answer-verify', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({items: list.map(it => ({
        key: it.key, kid: it.kid, course: it.course, node: it.node}))})});
    if (r.status === 409){ toast('当前有任务在跑，等它结束再核实', 'warn'); return; }
    if (!r.ok){ toast('核实没能启动（HTTP ' + r.status + '）', 'err'); return; }
    since = 0; logEl.innerHTML = '';
    $('bartip').textContent = '重新核实中…';
    switchTab('log');
    clearTimeout(timer);
    poll();
  }catch(e){
    toast('连不上本地程序，请确认那个黑色命令行窗口还在运行', 'err');
  }
}

function llmForm(){ return {llm_url: $('llm_url').value.trim(),
                             llm_key: $('llm_key').value.trim(),
                             llm_model: $('llm_model').value.trim()}; }
// 保存与「记住」走同一个端点：本机 config.json 本来就是持久保存，
// 区别只在提示语义——「保存」= 本次生效；「记住」= 明确写进本机，
// 下次打开页面自动回填（页面加载时的 /api/llm-config 回填一直都在）。
// 后端保存后会回读校验，写盘失败会返回 5xx，toast 如实报错。
function saveLlm(remember){
  fetch('/api/llm-config', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(llmForm())}).then(r => {
    if (!r.ok){ toast('保存失败：写入本机配置没成功，请重试（HTTP ' + r.status + '）', 'err'); return; }
    toast(remember ? '已记住：以后在这台电脑打开不用再填' : '大模型设置已保存', 'ok');
  }).catch(() => toast('连不上本地程序', 'err'));
}
$('bllm').onclick = () => saveLlm(false);
$('bllmremember').onclick = () => {
  if (!$('llm_url').value.trim() || !$('llm_key').value.trim()
      || !$('llm_model').value.trim()){
    toast('三样都填上再记住：接口地址 / API Key / 模型名', 'warn'); return;
  }
  saveLlm(true);
};
$('bllmtest').onclick = () => {
  const b = $('bllmtest'), old = b.innerHTML;
  b.disabled = true; b.innerHTML = '<i>⏳</i>验证中…（最长约 30 秒）';
  fetch('/api/llm-test', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(llmForm())}).then(async r => {
    const j = await r.json().catch(() => ({}));
    if (r.ok) toast('连通正常（' + $('llm_model').value.trim() + '）', 'ok');
    else toast('连通失败：' + (j.error || ('HTTP ' + r.status)), 'err');
  }).catch(() => toast('连不上本地程序', 'err')).finally(() => {
    b.disabled = false; b.innerHTML = old;
  });
};
// 打开页面就把已有配置带出来（key 是本机文件里的，回显无妨）
fetch('/api/llm-config').then(r => r.json()).then(c => {
  if ($('llm_url'))  $('llm_url').value  = c.llm_url  || '';
  if ($('llm_key'))  $('llm_key').value  = c.llm_key  || '';
  if ($('llm_model'))$('llm_model').value= c.llm_model|| '';
}).catch(() => {});

document.querySelectorAll('.tab').forEach(b => b.onclick = () => switchTab(b.dataset.t));

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
// 💡 熄屏：顶栏常驻，任务跑着随时能点（Chrome 的 Video Wake Lock 挡不住
// 显式 SC_MONITORPOWER），动下鼠标屏幕就亮，再点一下即可。
// 万一黑屏唤不醒：按 Win+Ctrl+Shift+B 重置显卡驱动即可恢复，不必强制关机。
$('boff').onclick = () => {
  fetch('/api/screen-off', {method:'POST'})
    .then(r => { if (!r.ok) toast('熄屏请求失败（HTTP ' + r.status + '）', 'err'); })
    .catch(() => toast('连不上本地程序', 'err'));
};
// 密码框小眼睛：点一下明文核对，再点一下隐藏（不改变输入内容）
$('beye').onclick = () => {
  const pwd = $('pwd'), show = pwd.type === 'password';
  pwd.type = show ? 'text' : 'password';
  $('beye').classList.toggle('on', show);
  $('beye').textContent = show ? '🙈' : '👁';
  pwd.focus();
};
$('bopen').onclick  = () => fetch('/api/open-report', {method:'POST'});
$('bdir').onclick   = () => fetch('/api/open-dir', {method:'POST'});
$('bpause').onclick = () => {
  const on = $('bpause').dataset.paused === '1';
  fetch(on ? '/api/resume' : '/api/pause', {method:'POST'});
};
$('bstop').onclick = () => {
  if (!confirm('停止当前任务？\n\n查询：已扫完的课程会照常出报告，只是标注为「中止扫描」。\n'
               + '刷视频：已刷完的不受影响，没刷完的保持原样。'
               + '\n停止后有几秒收尾时间，这几秒里不能开始新任务。')) return;
  fetch('/api/cancel', {method:'POST'});
};
$('bquit').onclick  = async () => {
  if (!confirm('确定退出程序吗？\n\n后台进程会结束、端口释放；正在跑的查询会被中断。')) return;
  closed = true;
  clearTimeout(timer);
  try { await fetch('/api/quit', {method:'POST'}); } catch(e){}
  document.body.innerHTML =
    '<div style="font-family:system-ui,-apple-system,\'Microsoft YaHei\',sans-serif;'
    + 'background:#141414;color:#ddd;height:100vh;display:flex;flex-direction:column;'
    + 'align-items:center;justify-content:center;gap:10px">'
    + '<div style="font-size:22px">程序已退出</div>'
    + '<div style="color:#888">后台进程已结束，端口已释放，这个页面可以关掉了。</div></div>';
};
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
            return self._send(200, PAGE.encode('utf-8'), 'text/html; charset=utf-8')
        if u.path == '/api/poll':
            q = parse_qs(u.query)
            try:
                since = int((q.get('since') or ['0'])[0] or 0)
                rv = int((q.get('rv') or ['-1'])[0] or -1)
                av = int((q.get('av') or ['-1'])[0] or -1)
            except ValueError:
                # 畸形参数按缺省处理：不给响应会让 socketserver 往
                # 黑色控制台刷 traceback，可被脚本刷屏
                since, rv, av = 0, -1, -1
            with LOCK:
                payload = {'lines': STATE['lines'][since:],
                           'total': len(STATE['lines']),
                           'dropped': STATE['dropped'],
                           'running': STATE['running'], 'task': STATE['task'],
                           'paused': STATE['paused'],
                           'phase': STATE['phase'],
                           'done': STATE['done'], 'summary': STATE['summary'],
                           'report': STATE['report'],
                           'courses': STATE['courses'],
                           'login_fail': STATE['login_fail'],
                           'started': STATE['started'],
                           'result_v': STATE['result_v'],
                           'ares_v': STATE['ares_v']}
                # 报告本体可能很大（几十门课的明细）。界面已经拿到这一版时就别再
                # 重复下发 —— 否则每次轮询都要把整个报告序列化一遍送过去。
                if rv != STATE['result_v']:
                    payload['result'] = STATE['result']
                if av != STATE['ares_v']:
                    payload['ares'] = STATE['ares']
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode('utf-8'))
        if u.path == '/api/llm-config':
            cfg = cs.load_config()
            return self._send(200, json.dumps(
                {k: cfg.get(k, '') for k in ('llm_url', 'llm_key', 'llm_model')},
                ensure_ascii=False).encode('utf-8'))
        return self._send(404, b'{"error":"not found"}')

    # 解析请求体的 POST 接口：必须带 application/json 头。
    # HTML 表单发不出这个头 → 表单型 CSRF 无法伪造这些操作。
    _JSON_PATHS = ('/api/run', '/api/brush', '/api/answer', '/api/combo',
                   '/api/answer-verify', '/api/llm-config', '/api/llm-test')

    def do_POST(self):
        if not self._local_only():
            return self._send(403, b'{"error":"forbidden"}')
        u = urlparse(self.path)
        if u.path in self._JSON_PATHS and 'application/json' not in (
                self.headers.get('Content-Type') or '').lower():
            return self._send(415, b'{"error":"json required"}')
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n else b'{}'
        if u.path == '/api/run':
            try:
                data = json.loads(raw.decode('utf-8') or '{}')
            except Exception:
                data = {}
            action = data.get('action') or 'scan'
            with LOCK:
                if STATE['running']:
                    return self._send(409, b'{"error":"busy"}')
                STATE.update(running=True, task=TASK_NAME.get(action, action),
                             lines=[], done=False, summary=None, started=time.time(),
                             login_fail=None)
            _spawn(_worker, action, data)
            return self._send(200, b'{"ok":true}')
        if u.path == '/api/screen-off':
            # 💡 熄屏：强制关显示器，不影响任务（Chrome 的 Video Wake Lock
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
            with LOCK:
                if STATE['running']:
                    return self._send(409, b'{"error":"busy"}')
                STATE.update(running=True, task=TASK_NAME['brush'],
                             lines=[], done=False, summary=None,
                             started=time.time(), login_fail=None)
            _spawn(_brush_worker, {'only': only,
                                   'shutdown': bool(data.get('shutdown'))})
            return self._send(200, b'{"ok":true}')
        if u.path == '/api/answer':
            # 「做勾选课程的章节作业并交卷」。only 必填，答完直接正式提交。
            try:
                data = json.loads(raw.decode('utf-8') or '{}')
            except Exception:
                data = {}
            only = [str(k) for k in (data.get('only') or []) if k]
            if not only:
                return self._send(400, json.dumps(
                    {'error': '没有勾选要做作业的课程'}).encode('utf-8'))
            with LOCK:
                if STATE['running']:
                    return self._send(409, b'{"error":"busy"}')
                STATE.update(running=True, task=TASK_NAME['answer'],
                             lines=[], done=False, summary=None,
                             started=time.time(), login_fail=None)
            _spawn(_answer_worker, {'only': only})
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
            with LOCK:
                if STATE['running']:
                    return self._send(409, b'{"error":"busy"}')
                STATE.update(running=True, task=TASK_NAME['combo'],
                             lines=[], done=False, summary=None,
                             started=time.time(), login_fail=None)
            _spawn(_combo_worker, {'only': only,
                                   'shutdown': bool(data.get('shutdown'))})
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
            for k in ('llm_url', 'llm_key', 'llm_model'):
                if k in data:
                    cfg[k] = str(data.get(k) or '').strip()
            cs.save_config(cfg)
            # 写完必须回读确认。以前只写不查，「保存成功」的 toast 是
            # 假的：一旦写盘有竞态/权限问题，查询线程读不到 llm 键，
            # 刷作业就会提示「先配置大模型」（用户实测踩过）
            back = cs.load_config()
            ok = all(back.get(k) for k in ('llm_url', 'llm_key', 'llm_model')
                     if str(data.get(k) or '').strip())
            if not ok:
                cs.log('⚠ 模型信息写入后回读校验未通过，请重试保存。', 'err')
                return self._send(500, b'{"error":"save-verify-failed"}')
            cs.log('大模型设置已保存（模型：%s）' % (cfg.get('llm_model') or '未填'))
            return self._send(200, b'{"ok":true}')
        if u.path == '/api/llm-test':
            # 「验证连通」：拿界面当前填的模型信息真调一次 LLM（短问答）。
            # ThreadingHTTPServer 每请求一线程，这里同步等待不阻塞页面轮询。
            try:
                data = json.loads(raw.decode('utf-8') or '{}')
            except Exception:
                data = {}
            tcfg = dict(cs.load_config())
            for k in ('llm_url', 'llm_key', 'llm_model'):
                v = str(data.get(k) or '').strip()
                if v:
                    tcfg[k] = v
            miss = [k for k in ('llm_url', 'llm_key', 'llm_model')
                    if not tcfg.get(k)]
            if miss:
                return self._send(400, json.dumps(
                    {'error': '还没填完：%s' % '、'.join(
                        {'llm_url': '接口地址', 'llm_key': 'API Key',
                         'llm_model': '模型名'}[k] for k in miss)},
                    ensure_ascii=False).encode('utf-8'))
            try:
                # timeout 给短值：连通测试只发「回复两个字」，12 秒足够，
                # 也避免网络不通时用户对着按钮等太久（llm_chat 内部最多重试 3 次）
                reply = cs.llm_chat(tcfg, '请只回复两个字：连通', timeout=12)
                cs.log('大模型连通验证通过（%s）：%s'
                       % (tcfg['llm_model'], (reply or '')[:40]))
                return self._send(200, json.dumps(
                    {'ok': True, 'reply': (reply or '')[:80]},
                    ensure_ascii=False).encode('utf-8'))
            except Exception as e:
                cs.log('大模型连通验证失败：%s' % str(e)[:160], 'err')
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
        if u.path == '/api/open-report':
            p = STATE.get('report') or str(cs.OUT_DIR / '学习通未完成清单.md')
            try:
                os.startfile(p)  # type: ignore[attr-defined]
                return self._send(200, b'{"ok":true}')
            except Exception as e:
                return self._send(500, json.dumps({'error': str(e)}).encode())
        if u.path == '/api/open-dir':
            try:
                cs.OUT_DIR.mkdir(parents=True, exist_ok=True)
                os.startfile(str(cs.OUT_DIR))  # type: ignore[attr-defined]
                return self._send(200, b'{"ok":true}')
            except Exception as e:
                return self._send(500, json.dumps({'error': str(e)}).encode())
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


def _pick_port(start: int, tries: int = 20) -> int:
    for p in range(start, start + tries):
        with socket.socket() as s:
            try:
                s.bind(('127.0.0.1', p))
                return p
            except OSError:
                continue
    raise RuntimeError('找不到可用端口')


def _shutdown_async(delay: float = 0.35):
    """稍等片刻再关停，让 /api/quit 的响应先发出去。

    shutdown() 必须由 serve_forever() 之外的线程调用，所以这里单开线程。
    """
    def _bye():
        time.sleep(delay)
        with LOCK:
            srv = STATE.get('_server')
        if srv is not None:
            try:
                srv.shutdown()
            except Exception:
                pass
    threading.Thread(target=_bye, name='ui-shutdown', daemon=True).start()


def serve(port=8765, open_browser=True):
    port = _pick_port(port)
    cs.RUNTIME.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    with LOCK:
        STATE['_server'] = httpd
    url = 'http://127.0.0.1:%d/' % port
    cs.log('')
    cs.log('图形控制台已启动：%s' % url)
    cs.log('（只监听本机 127.0.0.1，不会对外网开放）')
    cs.log('用完退出：在网页里点「退出程序」，或直接关闭这个命令行窗口。')
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
        try:
            httpd.server_close()
        except Exception:
            pass
        with LOCK:
            STATE['_server'] = None
        cs.log('程序已退出，端口 %d 已释放。' % port)


if __name__ == '__main__':
    serve()
