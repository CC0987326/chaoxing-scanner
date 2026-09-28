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


cs.add_sink(_sink)

TASK_NAME = {
    'query': '一键查询（登录 + 扫描全部课程）',
    'scan': '扫描全部课程',
    'list': '获取课程列表',
    'login': '扫码 / 短信登录',
    'doctor': '环境自检',
    'selftest': '学习通兼容性检测',
}


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
            ok = cs.do_login(cfg, auto=False)
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
    except Exception as e:
        cs.log('任务失败：%s' % e, 'err')
        import traceback
        cs.log(traceback.format_exc()[-900:], 'err')
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
  <h1>学习通巡检工具<small>只读 · 不提交任何作业 · 数据只在本机</small></h1>
</header>
<div class="wrap">
  <div class="side">
    <div class="status"><span class="dot" id="dot"></span><span id="stxt">空闲</span></div>

    <div class="lbl">账号（手机号 / 超星号）</div>
    <input type="text" id="phone" placeholder="例 198xxxxxxxx" autocomplete="username">
    <div class="lbl">密码</div>
    <input type="password" id="pwd" placeholder="学习通登录密码" autocomplete="current-password">
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
      <button class="mini" id="bclear">清空日志</button>
    </div>
    <div class="pane" id="res"><div class="empty">还没有结果。填好账号密码后点左侧「一键查询未完成事项」。</div></div>
    <div class="pane" id="logpane" style="display:none"><pre id="log"></pre></div>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
let since = 0, timer = null, closed = false, lastDone = 0;
let rv = -1;            // 已经拿到的结果版本号（回传给服务端，避免重复下发整份报告）
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
  return String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
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

function renderResult(r){
  if (!r) return;
  const hw = r.undone_hw || [], ex = r.undone_exam || [], pg = r.undone_prog || [];
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
         + (it.url ? '<a href="' + esc(it.url) + '" target="_blank">去完成 ↗</a>' : '')
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
    // 每门课一个折叠：课程名 + 进度始终露出，只把章节明细收起来（一门课能拉出十几行）。
    for (let i = 0; i < pg.length; i++){
      const it = pg[i], chs = it.chapters || [], idx = String(i);
      h += '<details class="cbox"' + (openIdx.has(idx) ? ' open' : '')
         + ' data-idx="' + idx + '"><summary>'
         + '<span class="hd"><b>' + esc(it.course) + '</b>　'
         + '<span class="sub">已完成 ' + it.done + '/' + it.total + '（'
         + it.rate.toFixed(1) + '%）</span></span>'
         + '<span class="foldhint">'
         + '<span class="t-closed">'
         + (chs.length ? chs.length + ' 个章节待完成 · 点这里展开' : '点这里展开')
         + '</span><span class="t-open">点这里收起</span></span></summary>';
      if (chs.length){
        h += '<div class="chap">';
        for (const c of chs){
          h += '<div>' + esc(c.name) + '　<b>待完成 ' + (c.count||1) + '</b></div>';
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
  if (closed) return;
  let busy = false;
  try{
    const r = await fetch('/api/poll?since=' + since + '&rv=' + rv);
    const j = await r.json();
    busy = !!j.running;
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
      setText($('bartip'), '查询完成');
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
  if (closed) return;
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
$('bopen').onclick  = () => fetch('/api/open-report', {method:'POST'});
$('bdir').onclick   = () => fetch('/api/open-dir', {method:'POST'});
$('bpause').onclick = () => {
  const on = $('bpause').dataset.paused === '1';
  fetch(on ? '/api/resume' : '/api/pause', {method:'POST'});
};
$('bstop').onclick = () => {
  if (!confirm('停止本次查询？\n\n已扫完的课程会照常出报告，只是会标注为「中止扫描」。'
               + '\n停止后需要几秒生成报告，这几秒里不能开始新查询。')) return;
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

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ('/', '/index.html'):
            return self._send(200, PAGE.encode('utf-8'), 'text/html; charset=utf-8')
        if u.path == '/api/poll':
            q = parse_qs(u.query)
            since = int((q.get('since') or ['0'])[0] or 0)
            rv = int((q.get('rv') or ['-1'])[0] or -1)
            with LOCK:
                payload = {'lines': STATE['lines'][since:],
                           'total': len(STATE['lines']),
                           'running': STATE['running'], 'task': STATE['task'],
                           'paused': STATE['paused'],
                           'phase': STATE['phase'],
                           'done': STATE['done'], 'summary': STATE['summary'],
                           'report': STATE['report'],
                           'courses': STATE['courses'],
                           'login_fail': STATE['login_fail'],
                           'started': STATE['started'],
                           'result_v': STATE['result_v']}
                # 报告本体可能很大（几十门课的明细）。界面已经拿到这一版时就别再
                # 重复下发 —— 否则每次轮询都要把整个报告序列化一遍送过去。
                if rv != STATE['result_v']:
                    payload['result'] = STATE['result']
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode('utf-8'))
        return self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get('Content-Length') or 0)
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
            threading.Thread(target=_worker, args=(action, data), daemon=True).start()
            return self._send(200, b'{"ok":true}')
        if u.path in ('/api/pause', '/api/resume', '/api/cancel'):
            with LOCK:
                busy = STATE['running']
            if not busy:
                return self._send(409, b'{"error":"not running"}')
            if u.path == '/api/pause':
                PAUSE.set()
                with LOCK:
                    STATE['paused'] = True
                cs.log('⏸ 收到暂停请求：当前这门课抓完就停住（一般几秒内）…', 'warn')
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
                cs.log('■ 收到停止请求：当前这门课抓完后收尾，已扫到的结果照常出报告 …', 'warn')
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
