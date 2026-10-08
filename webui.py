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
    'brush': '自动刷视频（倍速静音）',
    'answer': '做作业并交卷（大模型答题）',
    'combo': '刷课 + 刷题（先刷视频再做作业交卷）',
    'verify': '重新核实未确认的作业',
    'answer_one': '刷题（单份作业，答完交卷）',
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
            cs.save_config(cfg)     # 记住这次选的倍速，下次打开还是它
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
        if not cs.llm_ready(cfg):
            cs.log('还没有配置大模型：请在左侧「模型」里选平台、填 API Key 并保存；'
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


def _answer_single_worker(opts):
    """「刷题」：对未完成作业清单里点中的那一份，直接答题并交卷。

    判别逻辑与「做作业并交卷（正式提交）」完全一致（共用 answer_urls →
    answer_one）：附件题整份跳过、题干读不出跳过、多空填空按空序切分、
    同题干复用上次答案、含不支持题型跳过。
    """
    cs.log('')
    cs.log('▶ 任务开始：刷题（单份作业，答完直接交卷）')
    PAUSE.clear()
    CANCEL.clear()
    with LOCK:
        STATE['paused'] = False
        STATE['phase'] = 'answering'
    try:
        cfg = cs.load_config()
        if not cs.llm_ready(cfg):
            cs.log('还没有配置大模型：请在左侧「模型」里选平台、填 API Key 并保存；'
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
                             submit=True, progress=progress, control=control)
        with LOCK:
            STATE['summary'] = {'交卷': out.get('submitted', 0),
                                '未确认': out.get('unverified', 0),
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
            cs.save_config(cfg)     # 记住这次选的倍速，下次打开还是它
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
    /* 原生控件（勾选框、下拉弹层、数字步进器）也跟着走深色。
       不声明的话 Chrome 会把它们画成亮白底，在深色页面上格外刺眼。 */
    color-scheme:dark;

    /* 地面与层次 */
    --bg:#0d1214;
    --rail:#111a1c;
    --raise:#162124;
    --sunk:#090e10;
    --line:#1d2a2e;
    --line-2:#283a3f;

    /* 墨色（对比度：正文 14.9:1，次级 9.0:1，弱化 5.9:1） */
    --tx:#dce7e8;
    --tx-2:#a6b9bb;
    --mut:#7e9599;

    /* 状态色 */
    --ac:#3fb6a8;
    --ac-hi:#4cc7b8;
    --ac-soft:rgba(63,182,168,.14);
    --warn:#d9a75a;
    --err:#e8837a;
    --ok:#6fc08d;

    /* 尺度 */
    --fs-xs:.6875rem;
    --fs-sm:.75rem;
    --fs-base:.8125rem;
    --fs-md:.875rem;
    --fs-lg:1rem;
    --fs-xl:1.25rem;

    --ease:cubic-bezier(.22,1,.36,1);
    --t:160ms;
    --rail-w:306px;
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
  ::selection{background:rgba(63,182,168,.30);color:#f2fbfa;}
  input,select,textarea{caret-color:var(--ac);}
  :focus{outline:none;}
  :focus-visible{outline:2px solid var(--ac);outline-offset:2px;border-radius:2px;}
  *{scrollbar-width:thin;scrollbar-color:#2a3d42 transparent;}
  ::-webkit-scrollbar{width:11px;height:11px;}
  ::-webkit-scrollbar-track{background:transparent;}
  ::-webkit-scrollbar-thumb{
    background:#25353a;border:3px solid transparent;background-clip:content-box;
    border-radius:999px;}
  ::-webkit-scrollbar-thumb:hover{background:#33494f;background-clip:content-box;}
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
    padding:1px 6px;border:1px solid rgba(63,182,168,.35);
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
    background:#4a5c60;transition:background var(--t) var(--ease);
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
  .side{
    width:var(--rail-w);flex:0 0 var(--rail-w);
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
  .btn:hover:not(:disabled){background:#1c2b2e;border-color:var(--line-2);}
  .btn:hover:not(:disabled) .ic{color:var(--tx);}
  .btn:active:not(:disabled){transform:translateY(.5px);}
  .btn:disabled{opacity:.38;cursor:not-allowed;}
  .btn.pri{
    background:var(--ac);border-color:var(--ac);color:#08110f;font-weight:600;
  }
  .btn.pri .ic{color:#08110f;}
  .btn.pri:hover:not(:disabled){background:var(--ac-hi);border-color:var(--ac-hi);}
  .btn.dgr{color:var(--err);border-color:rgba(232,131,122,.34);}
  .btn.dgr .ic{color:var(--err);}
  .btn.dgr:hover:not(:disabled){background:rgba(232,131,122,.10);
    border-color:rgba(232,131,122,.55);}
  .btn.sm{padding:6px 10px;font-size:var(--fs-sm);gap:6px;}
  .btn.sm .ic{width:14px;height:14px;}

  /* 并排的按钮必须放弃 width:100%，否则三个一起被挤成竖排的窄条
     （中文会在任意字符处折行，看起来就像按钮坏了） */
  .row{display:flex;gap:7px;align-items:center;}
  .row > *{margin-bottom:0;}
  .row > .btn{width:auto;flex:0 0 auto;}
  .row > .btn.grow{flex:1 1 auto;min-width:0;}
  .grow{flex:1;min-width:0;}

  input[type=text],input[type=password],input[type=number],select{
    width:100%;padding:8px 10px;
    background:var(--sunk);color:var(--tx);
    border:1px solid var(--line);border-radius:6px;
    font:inherit;font-size:var(--fs-base);
    transition:border-color var(--t) var(--ease),background var(--t) var(--ease);
  }
  input::placeholder{color:var(--mut);}
  input:hover,select:hover{border-color:var(--line-2);}
  input:focus,select:focus{border-color:var(--ac);background:#0b1113;}
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
  .field .ic-in:hover{color:var(--tx);background:#16252a;}
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
  .mstat.ok{color:var(--ok);border-color:rgba(111,192,141,.30);}
  .mstat.warn{color:var(--warn);border-color:rgba(217,167,90,.30);}
  .mstat.err{color:var(--err);border-color:rgba(232,131,122,.30);}
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
    border-bottom:1px solid #131c1f;
    transition:background var(--t) var(--ease),color var(--t) var(--ease);
  }
  .pick label:last-child{border-bottom:0;}
  .pick label:hover{background:#121b1e;color:var(--tx);}
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
    background:#0f1719;border-bottom:1px solid #131c1f;
  }
  .cok{font-size:var(--fs-sm);color:var(--mut);}
  .cok.ok{color:var(--ok);}
  .cok.warn{color:var(--warn);}

  /* ============================== 主区 ============================== */
  .tabs{
    flex:none;display:flex;gap:2px;padding:0 18px;
    border-bottom:1px solid var(--line);background:var(--rail);
  }
  .tab{
    position:relative;padding:10px 15px;
    font:inherit;font-size:var(--fs-md);color:var(--mut);
    background:none;border:0;cursor:pointer;
    transition:color var(--t) var(--ease);
  }
  .tab::after{
    content:'';position:absolute;left:11px;right:11px;bottom:-1px;height:2px;
    background:var(--ac);transform:scaleX(0);
    transition:transform 220ms var(--ease);
  }
  .tab:hover{color:var(--tx-2);}
  .tab.on{color:var(--tx);font-weight:600;}
  .tab.on::after{transform:scaleX(1);}

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
  .mini.pri{background:var(--ac);border-color:var(--ac);color:#08110f;font-weight:600;}
  .mini.pri .ic{color:#08110f;}
  .mini.pri:hover:not(:disabled){background:var(--ac-hi);border-color:var(--ac-hi);}
  .mini.attn{animation:attn 1s var(--ease) 2;}
  @keyframes attn{50%{box-shadow:0 0 0 3px var(--ac-soft);}}

  .pane{flex:1;overflow:auto;min-height:0;}
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
    padding:1px 7px;border:1px solid rgba(63,182,168,.35);border-radius:2px;
  }
  #res .sec > h2 .n.warn{
    color:var(--warn);border-color:rgba(217,167,90,.35);
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
    background:rgba(217,167,90,.07);border:1px solid rgba(217,167,90,.30);
    color:var(--warn);
  }
  #res .banner.err{
    background:rgba(232,131,122,.07);border-color:rgba(232,131,122,.32);
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
    border:1px solid rgba(232,131,122,.34);border-radius:8px;
    padding:13px 15px;background:rgba(232,131,122,.06);
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
    background:#243338;overflow:hidden;
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
    border-bottom:1px solid #162124;
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
    background:#1b2729;border:1px solid var(--line-2);color:var(--tx);
    padding:11px 16px;border-radius:8px;font-size:var(--fs-base);
    max-width:min(72vw,560px);line-height:1.55;
    box-shadow:0 8px 26px rgba(0,0,0,.5),0 1px 0 rgba(255,255,255,.03) inset;
    opacity:0;pointer-events:none;
    transition:opacity 200ms var(--ease),transform 220ms var(--ease);
  }
  .toast.show{opacity:1;transform:translate(-50%,0);}
  .toast .ic{width:15px;height:15px;margin-top:2px;flex:none;}
  .toast.warn{border-color:rgba(217,167,90,.5);color:var(--warn);}
  .toast.err{border-color:rgba(232,131,122,.5);color:var(--err);}
  .toast.ok{border-color:rgba(111,192,141,.5);color:var(--ok);}

  @media (prefers-reduced-motion:reduce){
    *,*::before,*::after{animation-duration:.01ms !important;
                         animation-iteration-count:1 !important;
                         transition-duration:.01ms !important;}
  }
  @media (max-width:1080px){
    :root{--rail-w:272px;}
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
  <div class="side">

    <section class="blk">
      <h2 class="blk-t">账号</h2>
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
        记住账号（只记手机号，不记密码）</label>
      <label class="chk"><input type="checkbox" id="deep" checked>
        下钻到章节，列出未完成任务点</label>
      <button class="btn pri" id="bquery"><span class="ic" data-i="search"></span>
        一键查询未完成事项</button>
      <button class="btn" id="blogin"><span class="ic" data-i="qr"></span>
        扫码 / 短信登录</button>
      <p class="note">首次使用填账号密码即可；密码登录被平台拦下时会提示改用扫码。</p>
    </section>

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
      <p class="note">勾选优先于上面填的数：勾了课就只查这几门，一门不勾 = 查全部。</p>
    </section>

    <section class="blk">
      <h2 class="blk-t">运行中</h2>
      <div class="row">
        <button class="btn sm grow" id="bpause" disabled>
          <span class="ic" data-i="pause"></span>暂停</button>
        <button class="btn sm grow dgr" id="bstop" disabled>
          <span class="ic" data-i="stop"></span>停止本次查询</button>
      </div>
      <p class="note">暂停 / 停止都在当前这门课抓完后生效（一般几秒内）。
        停止不丢结果：已扫完的照常出报告，只是标注为「中止扫描」。</p>
    </section>

    <section class="blk">
      <h2 class="blk-t">模型</h2>
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
        <p class="note">任何 OpenAI 兼容接口都能用。Key 加密保存在本机
          （Windows 用户级加密，换电脑或换 Windows 用户后需重填），
          不回显在页面上。做作业用你自己的 Key 按量计费，一份选择题通常几分钱。</p>
      </div>
    </section>

    <section class="blk">
      <h2 class="blk-t">输出与退出</h2>
      <button class="btn" id="bopen" disabled>
        <span class="ic" data-i="file"></span>打开最新报告</button>
      <button class="btn" id="bdir"><span class="ic" data-i="folder"></span>
        打开输出文件夹</button>
      <button class="btn dgr" id="bquit"><span class="ic" data-i="power"></span>
        退出程序（释放端口）</button>
    </section>

    <section class="blk" id="cardsblk" style="display:none">
      <h2 class="blk-t">本次统计</h2>
      <div id="cards" class="cards"></div>
    </section>

    <details class="notes">
      <summary><span class="chev" data-i="chev"></span>使用说明与边界规则</summary>
      <p class="note">
        查询是只读的；刷课与答题必须你手动点按钮才会跑。所有数据只留在本机。
        <br><br>
        <b>换账号：</b>直接填新账号的密码点「一键查询」就行。本机若存着别的账号的
        会话，会自动改登你填的这个（旧会话归档到 <code>runtime/profile_old</code>，
        不会丢），绝不会拿旧账号继续跑。顶栏「本机会话」随时显示当前在用哪个账号。
        <br><br>
        <b>用完怎么退出：</b>点「退出程序」，或直接关掉那个黑色命令行窗口。
        只关浏览器页面不会退出，后台程序还在跑、端口还占着。
        <br><br>
        <b>熄屏后唤不醒：</b>按 Win+Ctrl+Shift+B 重置显卡驱动即可恢复。
      </p>
    </details>

    <!-- 联系方式与下载地址放在折叠区外面：v2.5 就定下的「侧栏底部能看到」
         不该被这版重设计顺手藏起来。链接文字写完整网址（含 https://），
         方便用户直接抄下来手打——只写域名会让人不确定前缀是什么。 -->
    <p class="foot">
      有问题或建议请联系 QQ：3547502147<br>
      最新版下载地址：<a href="https://github.com/CC0987326/chaoxing-scanner"
         target="_blank" rel="noopener">https://github.com/CC0987326/chaoxing-scanner</a>
    </p>

  </div>

  <div class="main">
    <div class="tabs">
      <button class="tab on" data-t="res">查询结果</button>
      <button class="tab" data-t="log">运行日志</button>
    </div>
    <div class="bar">
      <span class="ttl" id="bartip">尚未查询</span>
      <span class="sp"></span>
      <button class="mini" id="boff"><span class="ic" data-i="monitor"></span>熄屏</button>
      <button class="mini" id="bclear"><span class="ic" data-i="trash"></span>清空日志</button>
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

// 未完成作业清单。「刷题」按钮按索引取用——结果区每 900ms 整个重绘一次，
// 索引比把对象塞进 onclick 更抗重绘（重绘时 lastHw 同步刷新为同一份数据）。
let lastHw = [];

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
    $('res').innerHTML = '<div class="errbox"><b>任务失败</b>'
      + '<span>' + esc(r.error) + '<br>详情见「运行日志」页签；'
      + '可修正后重试（未登录时先登录）。</span></div>';
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

  h += '<div class="sec"><h2>未完成作业 <span class="n">' + hw.length
     + '</span> 项</h2>';
  lastHw = hw;
  if (hw.length){
    h += '<table><thead><tr><th>课程</th><th>作业名称</th><th>状态</th>'
       + '<th>剩余</th><th class="col-act"></th></tr></thead><tbody>';
    for (let i = 0; i < hw.length; i++){
      const it = hw[i], u = safeUrl(it.url);
      h += '<tr><td>' + esc(it.course) + '</td><td>' + esc(it.title)
         + '</td><td><span class="st err">' + esc(it.state) + '</span></td>'
         + '<td class="num sub">' + esc(it.left || '—') + '</td>'
         + '<td class="col-act">'
         + (u ? '<a href="' + u + '" target="_blank" rel="noopener">去完成</a>' : '')
         + (u ? ' <button class="mini" onclick="answerThis(event,' + i + ')"'
              + ' title="让大模型直接做这一份并交卷：与任务点的「做作业并交卷」同一套'
              + '判别——要上传附件的题整份跳过、题干读不出跳过，不会乱填">刷题</button>'
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
      h += '<tr><td>' + esc(it.course) + '</td><td>' + esc(it.title)
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
       + '<button class="mini pri" onclick="startAnswer(event)">做作业并交卷（正式提交）</button>'
       + '<button class="mini" onclick="toggleAllBrush(event)">全选</button>'
       + '<label class="chk inline" title="播放倍速。只提供播放器官方档位：别的值平台会拨回去，'
       + '来回打架反而频繁卡顿。实际播放会在这档和相邻档之间随机取挡，中途还会换一次挡">倍速 '
       + '<select id="brate" onchange="brateVal=this.value">' + rateOpts() + '</select></label>'
       + '<button class="mini" onclick="screenOff(event)">'
       + '<span class="ic">' + icon('monitor') + '</span>熄屏</button>'
       + '<label class="chk inline"><input type="checkbox" id="sdafter"'
       + (sdAfterOn ? ' checked' : '') + '> 刷完自动关机</label>'
       + '<span class="note">勾课程 = 全部章节；展开后可只勾某些章节。'
       + '刷视频 = 倍速静音真实播放；做作业 = 大模型答题后直接交卷，'
       + '同题干复用上次答案；附件 / 报告题会跳过并提示。'
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
         + '<span class="hd"><b>' + esc(it.course) + '</b>'
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
    // 本机会话是谁的。没有会话（或读不到身份）就不显示，绝不编一个出来。
    // setText 内部做了「值没变就不写 DOM」，每轮轮询不会造成无谓重排。
    setText($('whotxt'), j.login_who ? ('本机会话：' + j.login_who) : '');
    $('who').style.display = j.login_who ? '' : 'none';
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
  setCardsShown(false);
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

// ---------- 未完成作业每行的「刷题」 ----------
// 只做点中的那一份。判别 / 答题 / 交卷与任务点的「做作业并交卷」共用同一条
// 链（answer_urls → answer_one），所以行为一致：附件题整份跳过、题干读不出
// 跳过、多空填空按空序切分、同题干复用上次答案。
function answerThis(ev, i){
  if (ev) ev.stopPropagation();
  const it = (lastHw || [])[i];
  if (!it || !safeUrl(it.url)){ toast('这份作业没有可用的直达链接', 'warn'); return; }
  if (!confirm('让大模型做这一份并交卷？\n\n' + it.title + '\n（' + it.course + '）'
      + '\n\n答完直接正式提交、记录成绩。要求上传附件的题会整份跳过，不会乱填。')) return;
  fetch('/api/answer-one', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({url: it.url, course: it.course, title: it.title})}).then(r => {
    if (r.status === 409){ toast('当前有任务在跑，等它结束再做', 'warn'); return; }
    if (!r.ok){ toast('刷题没能启动（HTTP ' + r.status + '）', 'err'); return; }
    since = 0; logEl.innerHTML = '';
    setCardsShown(false); cardFp = '';
    $('res').innerHTML = '<div class="empty">正在后台刷这一份：' + esc(it.title)
      + '…（进度看「运行日志」，可以随时暂停 / 停止）</div>';
    $('bartip').textContent = '刷题中…';
    switchTab('log');
    clearTimeout(timer);
    poll();
  }).catch(() => toast('连不上本地程序，请确认那个黑色命令行窗口还在运行', 'err'));
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
    body: JSON.stringify({only: keys, shutdown: !!($('sdafter') && $('sdafter').checked),
                          rate: brateVal})}).then(r => {
    if (r.status === 409){ toast('当前有任务在跑，等它结束再刷', 'warn'); return; }
    if (!r.ok){ toast('刷视频没能启动（HTTP ' + r.status + '）', 'err'); return; }
    since = 0; logEl.innerHTML = '';
    setCardsShown(false); cardFp = '';
    $('res').innerHTML = '<div class="empty">正在后台刷视频…（倍速静音真实播放，'
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
    body: JSON.stringify({only: keys, shutdown: !!($('sdafter') && $('sdafter').checked),
                          rate: brateVal})}).then(r => {
    if (r.status === 409){ toast('当前有任务在跑，等它结束再做', 'warn'); return; }
    if (!r.ok){ toast('任务没能启动（HTTP ' + r.status + '）', 'err'); return; }
    since = 0; logEl.innerHTML = '';
    setCardsShown(false); cardFp = '';
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

// 熄屏：强制关显示器（Chrome 播视频的 Video Wake Lock 挡不住
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
    setCardsShown(false); cardFp = '';
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
// 状态 → [文案, 记号类]。颜色交给盘色变量，不在这里写死色值。
const AST = {
  done:       ['已交卷', 'ok'],
  already:    ['已交卷（此前已交）', 'ok'],
  fail:       ['交卷失败', 'err'],
  unverified: ['已发出 · 待平台确认', 'warn'],
  still:      ['仍未确认', 'warn'],
  report:     ['需人工 · 要传附件', 'warn'],
  unsupported:['需人工 · 题型不支持', 'warn'],
  unsolved:   ['需人工 · 模型没把握', 'warn'],
  skip:       ['已跳过', 'mut'],
};

function renderAnswerResult(a){
  if (!a || !a.items) return;
  aitems = a.items;
  const uv = aitems.filter(it => it.status === 'unverified' || it.status === 'still');
  const need = aitems.filter(it => ['report','unsupported','unsolved'].includes(it.status));
  const tags = [[aitems.length, '', '份']];
  if (uv.length) tags.push([uv.length, 'warn', '份待核实']);
  if (need.length) tags.push([need.length, 'warn', '份需人工']);
  let h = '<div class="sec"><h2>交卷结果'
       + tags.map(t => ' <span class="n' + (t[1] ? ' ' + t[1] : '') + '">'
                       + t[0] + '</span> ' + t[2]).join('')
       + '</h2>'
       + '<div class="meta">生成时间 ' + esc(a.time)
       + '　·　「待平台确认」= 提交已发出、平台还没翻转状态（可能延迟几分钟到十几分钟），'
       + '点「重新核实」自动翻案；也可以点开作业网址自己确认。</div>';
  if (uv.length){
    h += '<div class="brushbar"><button class="mini pri"'
       + ' onclick="verifyAnswer(event)"><span class="ic">' + icon('refresh')
       + '</span>重新核实未确认的（' + uv.length + ' 份）</button>'
       + '<span class="note">逐份重开作业卡查「已批阅」，隔一分钟一轮，'
       + '最多三轮，可随时停止。</span></div>';
  }
  h += '<table><thead><tr><th>课程</th><th>作业（章节）</th><th>状态</th>'
     + '<th class="col-act">操作</th></tr></thead><tbody>';
  aitems.forEach((it) => {
    const st = AST[it.status] || [it.status, 'mut'];
    h += '<tr><td>' + esc(it.course) + '</td><td>' + esc(it.node)
       + '</td><td><span class="st ' + st[1] + '">' + esc(st[0]) + '</span></td>'
       + '<td class="col-act">'
       + (safeUrl(it.url) ? '<a href="' + safeUrl(it.url)
                            + '" target="_blank" rel="noopener">打开作业</a>' : '')
       + '</td></tr>';
  });
  h += '</tbody></table></div>';
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
    llmSetState('err', '连不上本地程序，请确认那个黑色命令行窗口还在运行');
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
    body: JSON.stringify(llmForm())}).then(async r => {
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
    llmSetState('err', '连不上本地程序，请确认那个黑色命令行窗口还在运行');
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
// 熄屏：常驻在任务工具条上，任务跑着随时能点（Chrome 的 Video Wake Lock 挡不住
// 显式 SC_MONITORPOWER），动下鼠标屏幕就亮，再点一下即可。
// 万一黑屏唤不醒：按 Win+Ctrl+Shift+B 重置显卡驱动即可恢复，不必强制关机。
$('boff').onclick = () => {
  fetch('/api/screen-off', {method:'POST'})
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
            # PAGE 是 raw 字符串，塞不进 f-string；版本号用占位符在这里替换，
            # 免得版本改了界面还挂着旧号（用户报问题时说不清自己用的哪版）
            html = PAGE.replace('__VERSION__', cs.VERSION)
            return self._send(200, html.encode('utf-8'), 'text/html; charset=utf-8')
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
            # 本机这份会话属于哪个账号。以前界面对此只字不提，用户很容易
            # 「以为在查自己的号，其实查的是上一个登录过的号」（换号时最
            # 容易踩）。每次轮询现读文件，保证显示的和实际用的一致——读不
            # 到就当没有，不猜。
            try:
                who = cs.session_label() if cs.STATE_FILE.exists() else ''
            except Exception:
                who = ''
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
            key = cs.llm_key_of(cfg)
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
            }
            # 顺手把刷课倍速也带给前端（页面加载时回填下拉框的上次选择）
            payload['brush_rate'] = cfg.get('brush_rate', 1.25)
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode('utf-8'))
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
        if u.path == '/api/answer-one':
            # 「刷题」：对未完成作业清单里点中的那一份直接答题并交卷。
            # 没有 http(s) 链接就无从定位这份作业，直接拒掉而不是猜。
            try:
                data = json.loads(raw.decode('utf-8') or '{}')
            except Exception:
                data = {}
            url = str(data.get('url') or '').strip()
            if not url.lower().startswith('http'):
                return self._send(400, json.dumps(
                    {'error': '这份作业没有可用的直达链接'}).encode('utf-8'))
            with LOCK:
                if STATE['running']:
                    return self._send(409, b'{"error":"busy"}')
                STATE.update(running=True, task=TASK_NAME['answer_one'],
                             lines=[], done=False, summary=None,
                             started=time.time(), login_fail=None)
            _spawn(_answer_single_worker,
                   {'url': url, 'course': str(data.get('course') or ''),
                    'title': str(data.get('title') or '')})
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
                                   'shutdown': bool(data.get('shutdown')),
                                   'rate': data.get('rate')})
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
            # 平台：预设平台只接受用户改过的那几项（地址默认用预设的）；
            # 自定义平台则地址与模型都听用户的。
            plat = str(data.get('llm_platform') or '').strip()
            if plat:
                cfg['llm_platform'] = plat
            if 'url' in data:
                cfg['llm_url'] = str(data.get('url') or '').strip()
            if 'model' in data:
                cfg['llm_model'] = str(data.get('model') or '').strip()
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
                cfg['llm_key'] = ''
            elif newkey:
                # 加密存成功 → 明文就不再落 config.json；加密不可用则留在原处兜底
                if cs.save_llm_key(newkey):
                    cfg['llm_key'] = ''
                else:
                    cfg['llm_key'] = newkey
            cs.save_config(cfg)
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
            cs.log('模型设置已保存：%s ｜ %s ｜ Key %s'
                   % (cs.llm_platform(back.get('llm_platform') or 'deepseek')['label'],
                      back.get('llm_model') or '未填',
                      ('已保存 ' + cs.mask_key(key_now)) if key_now else '未填'))
            return self._send(200, json.dumps(
                {'ok': True, 'url': back.get('llm_url') or '',
                 'key_set': bool(key_now), 'key_mask': cs.mask_key(key_now),
                 'ready': cs.llm_ready(back)},
                ensure_ascii=False).encode('utf-8'))
        if u.path == '/api/llm-test':
            # 「测试连接」：拿界面**当前填的**模型信息真调一次 LLM（短问答）。
            # ThreadingHTTPServer 每请求一线程，这里同步等待不阻塞页面轮询。
            try:
                data = json.loads(raw.decode('utf-8') or '{}')
            except Exception:
                data = {}
            tcfg = dict(cs.load_config())
            for k in ('llm_url', 'llm_model'):
                v = str(data.get(k) or '').strip()
                if v:
                    tcfg[k] = v
            # Key 的取值顺序：界面刚填的 → 已保存的（加密文件里那把）。
            # 这一路不写回 url/model，也不落盘：测试通过与否都不该改用户
            # 已存的模型设置。唯一例外是 llm_key_of 发现 config 里还留着
            # 老版本的明文 Key——那会顺手迁成加密存放（这是好事）。
            typed = str(data.get('llm_key') or '').strip()
            tkey = typed or cs.llm_key_of(tcfg)
            miss = []
            if not (tcfg.get('llm_url') or '').strip():
                miss.append('接口地址')
            if not tkey:
                miss.append('API Key')
            if not (tcfg.get('llm_model') or '').strip():
                miss.append('模型名')
            if miss:
                return self._send(400, json.dumps(
                    {'error': '还没填完：%s' % '、'.join(miss)},
                    ensure_ascii=False).encode('utf-8'))
            try:
                # timeout 给短值：连通测试只发「回复两个字」，12 秒足够，
                # 也避免网络不通时用户对着按钮等太久（llm_chat 内部最多重试 3 次）
                reply = cs.llm_chat(tcfg, '请只回复两个字：连通', timeout=12,
                                    key=tkey)
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
