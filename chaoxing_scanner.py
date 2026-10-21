# -*- coding: utf-8 -*-
"""
学习通（超星）未完成事项巡检工具  v6.31
================================================================
把「登录 → 逐门课翻作业/考试/任务点/课堂活动 → 汇总成清单」这套流程固化成本地程序。

    python chaoxing_scanner.py login          首次登录（会弹出浏览器窗口，扫码或短信都行）
    python chaoxing_scanner.py login --auto   全自动短信登录（需 config 里 auto_captcha=true；
                                              无头运行，出现点选验证码会直接停下并提示）
    python chaoxing_scanner.py scan           扫描全部课程并生成清单
    python chaoxing_scanner.py scan --limit 3 只扫前 3 门课（自检用）
    python chaoxing_scanner.py doctor         检查运行环境
    python chaoxing_scanner.py selftest       检测学习通是否改版（逐层验证解析规则）
    python chaoxing_scanner.py desktop        打开桌面窗口（默认入口）
    python chaoxing_scanner.py gui            打开本地网页控制台（备用）

依赖：playwright（浏览器驱动）、pywebview（桌面窗口，v4.0 起）、ddddocr（可选，用于自动过点选验证码）
查询（scan）全程只读；刷视频 / 答题交卷 / 课堂活动作答都要你在界面上手动点按钮才会跑。

v6.11 要点：修课堂活动作答三处真机 bug——① 多空填空题答案全砸进第 1 空
（同名输入框按空序逐个消费）；② 多选答案含分隔符/重复字母时漏填（剔分隔
+去重+按终态校准 checkbox）；③ 「重新核实」认得课堂活动了（在平台上手动
交过后，点「重新核实」面板会翻案成已交卷）。

v6.12 要点：LLM 答案「显示保真」——简答/填空答案里的 LaTeX（\\begin{pmatrix}、
\\frac、$…$）与 Markdown（**、`、#）在解析层统一洗成可正常显示的纯文本
（矩阵 →「[ a, b; c, d ]」式），提示词同时明令禁止；顺带修「答案带花括号
LaTeX 时候选 JSON 被切碎、整题丢失」的老盲区。

v6.13 要点：①简答题答案带过程——先写关键步骤/公式代入，最后一行给结论
（「答：…」），不再只给一个光秃秃的结果。
②手写/纸上书写题自动跳过交人工——「在纸上书写…拍照上传」这类要求写在
题目富文本里的也能认出（stem + li_html 双扫），Python/JS/提示词三层一致。

v6.14 要点：修课堂活动简答题「有答案却填不上去、暂存存不上」——简答正文是
UEditor 富文本编辑器，textarea 只是壳，直接写 value 平台读不到；改走 UE.instants
API（与作业侧 JS_FILL_TEXT 同一套判据），编辑器 iframe 兜底、走不到就计 miss
（安全阀拦下不把空简答存上去）。

v6.15 要点（效率向）：①结果区改多级导航——「主页面（查询总览）」与「任务页
（进度 / 交卷面板）」两层，#resHome / #resTask 来回切，跑任务时不再把刚查到的
总览顶掉；任务页左上角「返回主页面」、主页面「正在进行 / 上次任务」按钮。
②跑完任务**不再自动全量重查**（那趟 1~2 分钟，用户嫌慢）——改为在主页面上挂
「清单可能已变 · 点『刷新清单』更新」，用户自己点 #brefresh 才重查（复用 auto
静默分支）。③登录态常驻——只要本机会话还能用就一律复用，不再因填了别的账号
而换罐重登；「换号」收敛成侧栏「退出登录（换号）」按钮（`logout()`，清会话 +
归档 profile + 删 state.json，下次查询才真重登）。

v6.16 要点（效率向）：①**单门刷新**——以前点「刷新」每次都整轮重查全部课程
（12 门 ≈ 40~50 秒），现在每行课程名旁有一个 ↻ 按钮：点哪门就**只重抓那一门**
（复用常驻登录态、不重读课程列表、不碰其它课），再把结果并回当前清单重建报告，
约 3~4 秒。②「刷新清单」这个全局按钮改为**重新读取课程列表**（课程有增删时的
兜底），全量重查不再占用它。

v6.17 要点：修 v6.16「单门刷新」的四处隐患——①**刷新撞风控不再静默丢
数据**：以前单门刷新失败时，报告里「页面未加载」用的是整轮扫描算的旧
清单，重抓的这一门既没加也没减，于是撞上平台安全验证时，那门课的未完成
项会**从清单里凭空消失、还不报警**（跟工具「页面未加载 ≠ 没有」的铁律
相悖）；现在按合并后的结果重算这个清单，与整轮扫描同一口径。②**单门
刷新增节流**：整轮扫描两门课之间有间隔，单门刷新若一点就来（连点清单
= 几秒内十几次整页导航）等于自己撞风控；现在补上同等间隔，偶尔点一下
仍是秒级。③**刷新的失败不再被当「后台静默」吞掉**：失败时结果面板一定
收口（有旧清单就留着、没有就明确报错），不会停在「正在刷新…」。

v6.18 要点（界面改版，照抄云析布局 + Material3 蓝调）：①**顶部横栏改成左侧竖直
导航**——最左一列竖排「查询 / 日志 / 预览 / 上传链接 / 设置」（设置压最底），顶部只
留一条细顶栏（品牌 / 版本 / 状态胶囊 / 本机会话）。②**新增设置页**收纳账号（登录 /
退出登录）、大模型、外观主题、检查更新 / 导出日志、两个开关等。③「上传链接刷课刷题」
从侧栏挪成独立页。④**查询页两栏**：左列查询操作（开始查询 + 查询范围），右列查询
结果台账；「运行中」（暂停 / 停止）挪到结果区顶部工具条。⑤**主题与外观**：浅色 /
深色 / 跟随系统三态 + 主题色（10 个预设 + 自定义色号），照云析 Material3 换蓝调。
⑥新增**检查更新**（查 GitHub release，只提示 + 给下载链接，不自动下载）与
**导出日志**（另存 `输出/日志`）。⑦新增**引导提示开关**与**刷课时阻止电脑休眠
开关**（默认开）。⑧**删掉「退出程序」按钮**（关窗口 × 仍可退）。

v6.19 要点（修「章节节点多任务卡」死循环）：一个章节节点其实是**一叠卡片**
（`knowledge/cards` 页里若干 `.ans-attach-ct`），视频只是其中一种，还可能是
测验（`modules/work`，章节测验/作业）、文档（`modules/pdf`）等；哪张卡是「任务
点」看它带不带 `.ans-job-icon`，完成没完成看该图标的 `aria-label`（「任务点
已完成」）或卡片 class 里有没有 `ans-job-finished`。以前「刷课」**只认
`modules/video`**，而且光看页面里有没有 `<video>`、**不看这张卡完成没有** →
节点里只要混着测验/文档卡，视频刷完了节点照样「未完成」，于是重跑「刷课」
又把已完成的视频**从头再播一遍**，卡死在这种循环里。本版：①刷课**跳过已完成
的视频**（不再重复播）；②刷完一个节点后**如实列出它还剩哪些非视频任务点**
（测验/作业/文档），提示用「刷课+刷题」或手动完成——不再让用户对着「刷完了
还显示未完成」发懵。测验/文档本身仍**不由「刷课」代做**（语义不变）。

v6.20 要点（真机复现后补的「哑节点」盲区）：v6.19 只堵住了「有残余」的节点——
现在补上「一个视频都没刷到」的哑节点。真机（Linux 课）复现：有的任务点卡
**不带 `.ans-job-icon`**，`_scan_leftover_tasks` 认不出来，于是既没视频可刷、
又报不出残余，日志里那行节点**一片空白**，汇总只写「完成 0 个」——用户看不出
为啥，只能重跑刷课（于是又重播已完成视频，回到死循环）。本版：节点若既没刷到
也没失败，就**明说**「这个节点没有可刷的视频（已完成 / 或不是视频任务点），
仍未完成，多半是章节测验/作业，用『刷课+刷题』或手动完成」。刷课依旧只管视频。

v6.21 要点（真机确诊：章节里的测验根本不在第一格选项卡）：
用户报「章节里的测验/作业一直找不到，刷完视频节点还是未完成」。真机（账号
190…6550 的大学物理-软件学院2026，1.1 理想流体的定常流动）查到底：学生页顶部
有一排**选项卡**（「1视频与讲义 / 2讨论 / 3测试」），每格对应 cards 地址里的
`num` 参数：视频与讲义=0、讨论=1、测试=2。**章节测验 / 单元测试就挂在 num=2
那格里**（1.1 在第 3 格、1.2 在第 2 格、1.7 恰好在第 1 格——逐节点不同）。
以前 `CARDS_URL` 把 `num` **写死成 0**，于是只在第一格里找作业卡：绝大多数
节点的测验永远找不到，日志还一片空白（连「没找到」都不说）。本版：
①答题引擎先读出本章选项卡列表，再**逐格找作业卡**（标题像「测试/测验/作业/
练习」的排前面），找到就用那一格答题；②「刷课」同样**逐格找视频卡**；
③残余任务点按选项卡分别统计；④**找不到时如实说明**（列出扫过哪几格），
不再让那行日志空着——用户才能分清「这节点本来就没作业」和「工具没找到」。

v6.24 要点（成绩与重刷 + 视觉通道的一处真错）：
用户报「6.23 还有题填错/填不上，而且有的刷完不及格」。逐条查下来：
① **视觉通道给字体加密题的选项发乱码**。题干早就隐去了（v6.22），选项没管：
   「惪想惥惦」这种被平台换过码位的选项文字照样发过去，模型被带偏，挑的字母
   与图完全对不上。本版反爬题只把**显示字母**（A/B/C…）告诉模型，让它看图
   回字母，落答侧再按 dl 映射。已加证伪守卫。
② **成绩与重刷**。作业列表只给「已完成/未完成」，分数必须打开作业卡才看得到，
   所以「分数低 / 不及格」以前只能靠用户自己发现。本版新增三个能力：
   查询时顺带逐节点读分（可勾选）；低于阈值（默认 85）的单独列出；
   「查成绩并重刷」自动点平台的重做、重新作答交卷，每份最多 2 次、取最高分。
   重刷**真的会消耗平台的剩余重做次数**，所以只在用户点按钮、且节点确实低于
   阈值时才做。平台重做时会**连题目顺序一起重排**（真机实测：批阅页第 10 题
   在重做后变成第 6 题，选项图 4/4 相同），所以不能照抄上次的答案，必须重答。

v6.31 要点（窗口不入坞 + 一直在桌面上微微闪）：
① 用户装上 v6.30 后报：「之前都是不显示的窗口，现在都变成显示的了」「这个显示
   出来的浏览器窗口一直在微微闪烁，好像卡了一样」「以前浏览器窗口都嵌入主窗口
   预览页面里，现在独立出来了，预览里没东西了」。
② 根因在 `webui._main_hwnd()`：它用 `FindWindowW(None, WIN_TITLE)` 按**标题**
   找主窗口。真机（2026-10-20）实测：Windows 11 的 explorer.exe 有一个标题
   **恰好等于**「学习通巡检工具」的 `Windows.Internal.Shell.TabProxyWindow`
   （任务栏分组/预览用的 135x37 代理窗，pid 11216）。`FindWindowW` 先命中它，
   与本工具的主窗口（`WindowsForms10.Window.8.app.*`，本进程 pid）无关。
③ 后果链：`SetParent(浏览器窗口, explorer 的代理窗)` → 必然失败 →
   `_dock_new_windows` 走兜底分支 `MoveWindow(hwnd, 60, 60, 1200, 800, True)`，
   把**屏外**启动（-32000,-32000）的浏览器硬搬到桌面 (60,60) —— 「以前不显示
   的窗口现在显示了」。守夜线程**每秒**再来一遍（ShowWindow(SW_RESTORE) →
   改窗口样式 → SetParent 失败 → MoveWindow），窗口于是「一直在微微闪」。
   「预览里没东西」是因为 `docked` 永远空着，`dock_count()` 恒为 0。
④ 修法一（别再按标题找）：`_main_hwnd()` 改成 EnumWindows + **本进程 pid** +
   类名前缀 `WindowsForms10.Window.`，同标题的 explorer 代理窗 pid 对不上，
   结构上排除。真机探针：修前返回 explorer 的 28182114，修后返回本进程的
   主窗口；`SetParent` 成功、`parent == main`、`dock_count() == 1`。
⑤ 修法二（失败别再搬窗口）：入坞失败时**只还原样式，绝不 MoveWindow**。
   屏外待坞是正常状态（面板没开就看不见），把窗口甩到桌面上才是 bug。
   同时把失败过的句柄记进 `DESKTOP['_dock_failed']`，主窗口没变就不再每秒重试
   ——这就是「微微闪烁」的消除办法。主窗口换了（重建）才清空重试表。
⑥ 别被「找不到主窗口」骗了：`_main_hwnd()` 现在可能返回 0（桌面窗口还没建
   好/已关闭），那时 `_dock_new_windows` / `_dock_layout` 直接返回，窗口老实
   留在屏外——**绝不能**退回「枚举全部 Chrome 窗口」或「找任意同标题窗口」。

v6.30 要点（真机「窗口弹出来又消失、重复」——把**自己**当孤儿杀了）：
① 用户装上 v6.29 打包版后：「一直弹出来这个浏览器窗口然后消失,重复进行
   这个弹窗口,然后还是没用」。真机 run.log（10:58:55~10:59:11）里，每两秒
   换一个 pid 重复「发现遗留浏览器 → 收掉 → 被已有实例接管 → 重试」：
   25584 → 8940 → 21248 → 24432，最后逐候选全红。
② 根因：启动判定是
     `if ok and proc.poll() is not None:  # 端口有应答，Popen 的 pid 却退了`
     → 判成「参数被别的实例转交」→ `_reap_orphan_browser()` 去收那只实例。
   可 Edge 在某些机器上会先起一个**启动器替身**、自己退出，**真正的浏览器
   另起 pid 接管同一个 DevTools 端口**。端口是本工具随机挑的空闲端口，
   别人的实例不可能来听它——所以那个「别的实例」**就是我们刚拉起来的
   浏览器本身**。于是窗口弹出来 → 被自己杀掉 → 换候选再起一只 → 再杀，
   用户看到的就是「一直弹窗然后消失、重复」。
③ 修法一（认得自己人）：新增 `_port_owner()`（iphlpapi 读 LISTEN 表，
   约 1 毫秒）与 `_port_served_by_us()`——端口属主是浏览器 exe、命令行带
   **本活动 profile** 且带 `--remote-debugging-port=<本端口>`、且不含
   `--type=`，才算「我们的人」。有它就**接管**：`_PidProc` 按 pid 给真身做
   Popen 替身，`_adopt_job_pid()` 把真身也挂进 Job，绝不杀。
   只有「端口在应答、属主却不是我们的人」才判「被别的实例接管」。
④ 修法二（收孤儿前排除自己人）：`_reap_orphan_browser` 先按 `_SPAWN_PORTS`
   把「正在为我们服务的 pid」捞进 `mine`，收孤儿时跳过；复核时
   `_profile_roots() - mine` 为空且锁被自己人占着 → 直接返回 True，
   不再误报「有东西在接力重开浏览器」。
⑤ 修法三（挑端口）：`_free_port()` 只保证「能 bind」，正被监听的口也可能
   被它返回；现在挑完再用 `_port_owner()` 确认没人听，避免把上一轮还没退
   干净的浏览器认成这一轮的。
⑥ 修法四（路径比对）：`_cmd_has_profile()` 统一小写 + 反斜杠后比较——
   Windows 路径不分大小写，Chromium 也可能写正斜杠；以前一个字母大小写
   就能让「自己的浏览器」认不出来，直接退化成「收不掉 → 换罐 → 又闪一次」。
⑦ 修法五（别再起第二只）：`_start_browser_process()` 开头先看
   `_BROWSER['proc']` 是不是还活着，活着就直接返回。两个线程同时切浏览器
   形态时，A 的 `stop_browser()` 之后、`ensure_browser()` 之前，B 可能已经
   又起好一只；A 再起就撞上**自己人**攥着的 Lockfile，三个候选各白等 25 秒
   （真机探针实测 **73.22 秒**），最后还被逼换罐。同时把「停 + 起」整体包进
   `_BROWSER_LOCK`（改成 `RLock`，否则自己会锁死自己），形态切换才真原子。
   注意跳过时**不改** `_BROWSER['hl']`——谎报形态会让上层误判「形态已一致」，
   拿屏幕外的窗口去干需要用户扫码的登录。

v6.29 要点（只收浏览器主进程；黑窗不再闪）：
① **v6.28 收错了对象**。真机日志（2026-10-20 07:21:49~07:22:13）是：
     每轮收掉 4~10 个浏览器进程（pid 3512、10176、12480、24052 …），下一轮
     **又冒出一批新的**，四轮收不干净，用户看到「终端和浏览器一直在重复闪烁」。
   探针实测：杀一个 **renderer** 子进程，主进程 2 秒内立刻补一个（14 个进程
   一个不少）；**只杀主进程**（taskkill /T）→ 1.9 秒后整棵树归零、残留 0。
   根因：`_profile_pids()` 按「命令行带本 profile」找进程，而 renderer/gpu/
   utility **都带** `--user-data-dir`，于是把一整窝子进程都当成「遗留浏览器」
   逐个杀——主进程一直在补人，永远收不完。
② 本版只认**主进程**（新增 `_profile_roots()`：命令行带本 profile 且
   **不含 `--type=`**）。主进程没有 `--type=`，子进程有；杀主进程用
   `taskkill /F /T` 整棵树一次走干净。
③ **黑窗不再闪**：`powershell`（查命令行）和 `taskkill` 都是控制台程序，
   从 pythonw.exe 里起会**弹一个黑窗**——每轮都弹，就是用户看到的「终端在闪」。
   新增 `_run_hidden()` / `_NO_WINDOW`（CREATE_NO_WINDOW），所有控制台子进程
   一律不带窗口；`webui.py` 的 shutdown 调用同样处理。
④ **死 pid 不再被当成「别的软件占着」**：进程刚退出时 exe 名查不到，
   旧代码把它判成「非浏览器进程攥着锁」→ 立刻放弃并换配置目录。真机日志里那
   一串 12796/18380/21492…「未知」就是这么来的。新增 `_pid_alive()`：
   已经退出的 pid 直接忽略，不算占用者。
⑤ 收完再复核：`_profile_free()` **且** `_profile_roots()` 为空才算真放开
   （Lockfile 偶有删除/重建的空窗，光看它不够）。
⑥ **中文安装路径下查不到进程**（本版补齐）：PowerShell 往管道里写的是**系统
   ANSI 代码页**编码（中文系统 GBK），而解压目录本来就叫「学习通巡检工具」，
   命令行里必然带中文。按 UTF-8 解码后整段变替换字符，`prof in cmd` 一个都
   不命中——`_profile_free()` 说「锁被占着」、这边却列出 0 个占用者，只能
   每轮换罐。真机抽验实测：同一份输出 UTF-8 解码命中 **0**，GBK 解码命中
   **15**。修法：PowerShell 侧先 `ToBase64String`（纯 ASCII），Python 侧再
   `b64decode`，彻底不依赖代码页；两个查询合并成 `_browser_procs()`。

v6.28 要点（真机上「杀一个冒一个」时换罐启动）：
① **v6.27 的清理假设错了**。真机日志（2026-10-19 21:31）是这样：
     pid 6368 → 4788 → 20304 → 21036，每轮 RM 报一个、收掉一个，下一轮
     **又冒出一个新的**，四轮之后仍报「进程秒退（profile 被别的实例占着）」。
   Restart Manager 一次只报一个持有人，而 Edge 是十几个进程共用一份 profile；
   更关键的是，真机上还有东西在**接力重开**浏览器（后台驻留/会话恢复），
   所以「杀干净」这条路在那种机器上根本收敛不了——继续加轮数只会让用户
   看着一堆窗口闪进闪出，最后仍然失败。
② 本版不再赌「能杀干净」，改成**能验证 + 能换罐**：
   · `_profile_free()` 直接**独占打开 Lockfile**（CreateFileW 共享=0）来判断
     「到底放开没有」——能打开就是真放开，比「RM 报不报」可靠得多
     （真机探针：RM 空 vs 独占 ERROR_SHARING_VIOLATION 32，后者才是真相）。
   · `_reap_orphan_browser` 改成**有限轮**（最多 3 轮，每轮把命令行里带本
     profile 的进程**全收**，不再只收 RM 报的那一个），每轮之后用
     `_profile_free()` 复核；收不干净就如实返回 False，不再无限杀。
   · `_start_browser_process` 在「秒退/起不来 + profile 确实被占」时，**换一份
     全新的配置目录**（`profile_next`，与 `profile_old` 同级的归档位）再启动。
     登录态不在 profile 里：cookie 走 `runtime/state.json`，`restore_state`
     会重新灌回去，所以换罐**不会把用户踢下线**；换罐后子进程被 `_scavenge`
     清掉，不会留下拖累下次启动的孤儿。
③ 启动参数加 `--disable-background-mode`：掐掉 Edge 的「关窗后驻留后台」，
   从源头减少这种会自己复活的浏览器。
④ `_ACTIVE_PROFILE` 是**这次运行真正在用**的配置目录（默认 `PROFILE_DIR`）；
   换罐成功后它指向新目录，`_browser_argv` / Stop / 预检都跟着它走。

v6.27 要点（上次留下的浏览器不再挡住下次启动）：
① **点查询弹出两个浏览器窗口，接着全部内核启动失败**（2026-10-19 打包版
   真机）。报错原文：「exe(msedge.exe) → 进程秒退（profile 可能被别的实例
   占着）/ chrome（未安装）/ channel(msedge.exe) → DevTools 未就绪」，同时
   预览页签一直「无内容」。根因不在这次启动，而在**上一次运行结束后浏览器
   还活着**：msedge 是独立进程，工具崩溃/被结束/退出路径没走到时它不会跟着
   死，继续攥着 `runtime/profile/Lockfile`。下次启动拿同一份 profile 起新
   进程 → Chromium 发现已有实例，把参数**转交**给它、自己秒退（老实例被
   转交后还会**再开一个窗口**，这就是用户看到的两个窗口）→ 秒退被当成
   「profile 被占」→ 逐候选全红。
   本版两道防线：
   · **提前防**：`_adopt_job` 把浏览器进程挂进一个
     `KILL_ON_JOB_CLOSE` 的 Job Object——本进程无论怎么没的（正常退出、
     崩溃、任务管理器结束），内核都会连带收掉浏览器整棵树，不再产生孤儿。
   · **事后兜**：`_reap_orphan_browser` 在起新进程前问 Restart Manager「谁
     锁着这份 profile 的 Lockfile」，只收 exe 名是 msedge/chrome 的那个，
     别的软件占着就只 warn、绝不动手（本项目的铁律：绝不杀用户的浏览器）。
     转交场景也会先收掉老实例再重试一轮，所以「两个窗口」不会再出现。
   顺带修掉一条把预检变成摆设的老代码：`_profile_locked` 以前 stat 的是
   Windows 上根本不存在的 `SingletonLock`（那是 Linux 的），现在改查真的
   `Lockfile`。

v6.26 要点（假完成能直接补做 + 关窗选择）：
① **查出来的「假完成」以前只能看，不能做**。v6.25 的对账把它们列进清单了，
   但查成绩/重刷那条链只处理「低于阈值」和「已判不及格」，`fakeDone` 与
   `pending` 一律只报告。用户看到「平台标已完成 · 实际没做」却找不到下手的地方
   ——单独勾章节能做，但谁会去逐个点。本版两条路都补上：
   · `query_scores(..., answer_pending=True)`：把「待完成 / 假完成」的卷子
     也纳入作答（默认关，界面上的勾选框才开）。判据是 `pending` 或
     `fakeDone` 且卡片确实没做过，已批阅/有分数的不碰。
   · 结果面板每行给「补做」入口，按 `kid` 直接走 `answer_one`，不用回清单找。
   作答完的份仍按「取最高分」记账，读不到分就如实写「未确认」。
② **任务进行中关窗**：以前点 × 直接拒绝（只打一行日志），用户以为窗口坏了。
   现在弹原生选择框「最小化窗口 / 关闭程序」（可记住），选关闭 = 先停任务
   （CANCEL + 有序收尾）再退。见 `webui._on_window_closing`。

v6.25 要点（「显示任务点已完成、其实没做题」+ 视觉调用省钱）：
① **平台的两个标记会打架**。真机（大学物理·软件学院2026，账号 190****6550）
   章节目录里 4.7 单元测试挂的是 `<div class="catalog_state icon_yiwanc
   catalog_tishi56">已完成</div>`，可点进去是 doHomeWorkNew 答题页、
   15 题一题没答。工具以前只认章节列表的 `.catalog_jindu`，这类节点根本
   进不了待办——用户说「巡检工具还查不到」就是这个。
   本版在查成绩时顺带对账：**测验卡说待完成、章节目录却说已完成** → 标
   `fakeDone`，当未完成任务点列进清单，可以勾选、可以交给「做作业」补做。
   全量实测 25 个作业节点里有 9 个是这种（2.5/3.9/4.6/4.7/5.7/5.8/6.5/7.7/7.8，
   含三份 15~20 题的单元测试）。不额外导航：查成绩本来就会打开每张测验卡。
② **视觉调用省钱，且不减准确率**。真机 4.7 / 5.8 整卷都是图片题（题干一张
   PNG、每选项一张 PNG，DOM 只剩「N 【单选题】」）：
   · 以前 `_need_vision` 把所有填空/简答也送去逐题截图 —— 纯文字主观题看图
     没用，纯烧钱。改成只有真有图/公式/字体反爬才走视觉。
   · 客观题合并成**一次**多图请求（4.7 的 15 题从 15 次降到 1 次）；解析不出
     来自动退回逐题，部分没答上只补问那几题。
   · 新增**题面图片指纹**（`_q_fp` / `_cache_key`）当缓存键。图片地址是内容
     哈希，实测三次加载题干图、选项图、选项顺序全部一致，所以同一串图片 =
     同一道题；指纹不含题号，平台重做时重排题目顺序也不影响。全图片题以前
     题干哈希是空串、**永远命不中缓存**，现在 15/15 都有键。
     ⚠ 纯文字选项也进指纹——4.7 的 Q5/Q9/Q11 四个选项全是文字，只认图片
       的话这几题指纹里的选项部分是空的，选项一换位缓存就答错。
③ **缓存命中前校验选项，防「答错且不报错」**。文字题的缓存键是题干哈希、
   不含选项顺序，而缓存里存的是**显示字母**；平台换卷面实例后一重排，同一个
   字母就指到另一个选项上。以前这里没有任何校验——省钱省出个错答案是最亏的。
   现在缓存值带「显示字母 → 选项内容」，命中时按内容重新对齐字母：对得上
   就换成当前字母（顺序真没变时照样复用），对不上就当没命中、重新问模型。
   老缓存（纯字符串）没有这层信息：文字题照用不倒退，图片题一律当 miss。
   多选逐字母换算、绝不截断；页面这次没读出选项时不判错。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- 控制台编码
# pythonw（无控制台启动，v4.0 桌面端启动器走它）下 stdout/stderr 是
# None：log() 里一个 print 就会把整个程序炸掉。先兜底成空设备。
for _n in ('stdout', 'stderr'):
    if getattr(sys, _n) is None:
        try:
            setattr(sys, _n, open(os.devnull, 'w', encoding='utf-8'))
        except Exception:
            pass
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

# 「任务」页（课程页左侧导航 li[dataname=hd]）背后的课堂活动接口。实测：
#   - 纯 JSON，一次 GET 拿全该课活动（签到 / 随堂练习 / 测验 / 通知 / 讨论…）；
#   - fid 是机构号，从登录 cookie 的 fid 取（本工具用默认 2339 兜底）；
#   - 返回 data.activeList[]，每条含 activeType（类型）、status（0=未开始
#     1=进行中 2=已结束）、userStatus、nameOne（名字/题目）、nameFour（时间）。
ACTIVELIST_URL = ('https://mobilelearn.chaoxing.com/v2/apis/active/student/'
                  'activelist?fid={fid}&courseId={cid}&classId={clsid}'
                  '&showNotStartedActive=1&_={ts}')

# 课堂活动类型（activeType）→ 中文名。只列实测见过的；未知类型原样报「活动(N)」。
# ⚠ 5 与 14 早先标反了（2026-10-14 真机核对平台任务页 activeRowClick 才纠正）：
#   平台 activeRowClick 的注释是 case '5'://主题讨论、case '14'://问卷。而 42
#   是「随堂练习/测验」（测验）。标反会导致「问卷」其实打不开、用户以为工具坏了。
ACTIVITY_TYPES = {
    2: '签到', 4: '抢答', 5: '主题讨论', 14: '问卷', 42: '随堂练习/测验',
    43: '投票', 44: '资料', 45: '通知', 77: 'AI实践',
}
# 「进行中」的活动（status==1）才是要提醒的未完成项：随堂练习/测验/签到都在这。
# status==0 是「老师发了但还没开始」，status==2 是已结束。与平台任务页
# 的「进行中 / 已结束」分桶完全一致（Vue: activeList1=进行中 activeList2=已结束）。

# 版本号：界面侧栏、启动日志、使用说明都引它，别再各处手写一份
VERSION = 'v6.31'

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
    'llm_model': '',        # 模型名（如 gpt-4o-mini / deepseek-flash）
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
    # 任务运行期间阻止系统自动睡眠（设置里可关；默认开）。
    # 桌面/网页 UI 的 _spawn 在起任务线程时读它，决定要不要 SetThreadExecutionState。
    'keep_awake': True,
    # v6.26：任务运行中点窗口 × 时的动作（用户勾了「记住我的选择」才写这里）。
    #   ''       = 每次都问（默认）
    #   'min'    = 直接最小化，任务继续跑
    #   'quit'   = 直接停任务并退出
    # 只认这三个值；别的值一律当没记住（手改坏了不至于让窗口关不掉）。
    'close_action': '',
    # v6.26：「查成绩并重刷」是否连「待完成 / 假完成」的卷子一起做。
    # 默认关——那是在用户账号上真交卷，必须由界面上那个勾选框明确打开。
    'answer_pending': False,
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


# 手改 config 时数值字段很容易写成字符串 / 负数 / 天文数字。不归一的话，
# "page_wait": "3" 会让 int("3"*1000) 得到 10^999 —— 页面渲染不出时
# _poll 永不退出，且循环里没有检查点，界面上的「停止」也停不掉
# （v4.0 二审发现）。在这里统一归一 + 夹回合理区间。
_CFG_NUM = {
    # 键: (类型, 默认值, 下限, 上限)
    'page_wait': (float, 3.0, 0.0, 120.0),
    'course_delay': (float, 3.0, 0.0, 60.0),
    'max_courses': (int, 0, 0, 999),
    'brush_rate': (float, 1.25, 0.5, 4.0),
    'brush_rest_every': (int, 6, 0, 100),
    'brush_rest_seconds': (int, 30, 0, 600),
    'brush_retry': (int, 1, 0, 5),
    'save_every': (int, 5, 1, 50),
    'submit_verify_window': (int, 30, 10, 600),
    'submit_recheck_times': (int, 3, 1, 10),
    'submit_recheck_interval': (int, 10, 3, 300),
}


def _norm_cfg(cfg):
    """数值字段归一：类型不对回落默认，合法但越界夹回区间。"""
    for k, (cast, dft, lo, hi) in _CFG_NUM.items():
        v = cfg.get(k, dft)
        try:
            v = float(v)
        except (TypeError, ValueError):
            v = dft
        # float(v) 可能溢出成 inf（手改 config 写成超长数字串，如 '9'*400），
        # 再 cast 成 int 就是 OverflowError —— 它一路冒到 main() 之外，
        # **整个程序起不来**（双击桌面端即闪退）。非有限值（inf/nan）无法夹取，
        # 一律回落默认值，与「类型不对」同款处置。
        if v != v or v in (float('inf'), float('-inf')):
            v = dft
        cfg[k] = max(lo, min(hi, cast(v)))
    return cfg


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if not CONFIG_FILE.exists():
        save_config(cfg)
        return _norm_cfg(cfg)
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
        return _norm_cfg(cfg)
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
    return _norm_cfg(cfg)


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
    """按优先级列出候选内核路径。

    v4.2 起**不再自带内核**（交付包瘦掉 ~190MB）。分享到别人机器上时靠
    系统已装的浏览器顶上：Windows 10/11 预装 Edge（必定命中），有 Chrome
    更佳。包内 / 开发缓存那几个路径保留在最前，只为兼容仍在用旧包的人
    与开发机测试——干净机器上它们都不存在，会自然落到系统浏览器路径，
    于是 doctor 能报出内核、launch 也无需下载。顺序上「包内优先」对
    开发机零冲击（测试内核不变），分发时又必然落到系统浏览器。
    """
    named = []
    if cfg.get('chrome_path'):
        named.append(Path(cfg['chrome_path']))
    named += [
        # 免安装包内自带内核（旧包兼容；新包已移除 chrome/）
        HERE.parent / 'chrome' / 'chrome-win64' / 'chrome.exe',
        HERE / 'chrome' / 'chrome-win64' / 'chrome.exe',
        CHROME_DIR / 'chrome-win64' / 'chrome.exe',
        CHROME_DIR / 'chrome.exe',
        Path.home() / '.workbuddy' / 'tmp' / 'browser' / 'chrome-win64' / 'chrome.exe',
        # 系统已装浏览器（分发到别人机器时的主力）
        Path(os.environ.get('LOCALAPPDATA', 'C:/')) / 'Google/Chrome/Application/chrome.exe',
        Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),
        Path('C:/Program Files (x86)/Google/Chrome/Application/chrome.exe'),
        # 系统 Edge：Win10/11 预装，几乎必定命中
        Path(os.environ.get('PROGRAMFILES', 'C:/Program Files'))
        / 'Microsoft/Edge/Application/msedge.exe',
        Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)'))
        / 'Microsoft/Edge/Application/msedge.exe',
        Path(os.environ.get('LOCALAPPDATA', 'C:/'))
        / 'Microsoft/Edge/Application/msedge.exe',
    ]
    for p in named:
        try:
            if p and p.exists():
                return p
        except Exception:
            continue
    return None


def _resolve_channel_exe(channel):
    """把 Playwright 的 channel 名解析成真实 exe 路径（拿版本号用）。

    launch 走 `channel='msedge'` 时，候选顺序里的路径可能压根不是 Edge
    （甚至为空），`_exe_version(<channel 名>)` 读不到版本资源 → UA 里会
    退成一个假版本号，和真实内核版本对不上。这里按 channel 找它对应的
    系统 exe，让 UA 版本号落到真实值。找不到就返回 None（fake_user_agent
    自会退回默认主版本）。
    """
    name = str(channel or '').lower()
    if 'edge' in name:
        cands = [
            Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)'))
            / 'Microsoft/Edge/Application/msedge.exe',
            Path(os.environ.get('PROGRAMFILES', 'C:/Program Files'))
            / 'Microsoft/Edge/Application/msedge.exe',
            Path(os.environ.get('LOCALAPPDATA', 'C:/'))
            / 'Microsoft/Edge/Application/msedge.exe',
        ]
    elif 'chrome' in name:
        cands = [
            Path(os.environ.get('LOCALAPPDATA', 'C:/'))
            / 'Google/Chrome/Application/chrome.exe',
            Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),
            Path('C:/Program Files (x86)/Google/Chrome/Application/chrome.exe'),
        ]
    else:
        return None
    for p in cands:
        try:
            if p.exists():
                return p
        except Exception:
            continue
    return None


def download_chrome() -> Path:
    """自动下载 Chrome for Testing（约 150MB，只在首次运行时做一次）"""
    RUNTIME.mkdir(parents=True, exist_ok=True)
    CHROME_DIR.mkdir(parents=True, exist_ok=True)
    banner('本机没找到系统 Edge/Chrome：下载独立内核 Chrome for Testing')
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
    """按真实内核如实生成 UA，只在它暴露「自动化/无头」时才抹平。

    旧版一律套「Chrome/<版本>」假 UA：在自带 Chrome 内核上没问题，但
    v4.2 分发改为用**系统 Edge** 顶内核后就成了自相矛盾——UA 说自己是
    Chrome，内核处处是 Edge 特征（`Edg/` 标志、Edge 私有行为、JS 可见的
    navigator.userAgentData），比不伪装更扎眼。

    所以：**忠实报告真实内核**。内核是 Edge 就带 `Edg/<版本>`，是 Chrome
    就带 `Chrome/<版本>`；版本号取自 exe 的真实版本资源（`_exe_version`），
    于是每台机器的 UA 天然不同（这本身就是比「所有人同一个假 Chrome UA」
    更强的反聚类）。仅当 headless 把 `HeadlessChrome` 写进 UA 时才替换成
    正常 Chrome——那种「无头」记号是真正需要抹掉的自动化特征。
    用户手填了 UA 则以其为准（沿用旧行为）。
    """
    if cfg.get('user_agent'):
        return cfg['user_agent']
    ver = _exe_version(exe) if exe else ''
    major = (ver.split('.')[0] if ver else '') or '131'
    base = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) ')
    is_edge = bool(exe) and 'edge' in str(exe).lower()
    tail = 'Edg/%s.0.0.0' % major if is_edge else 'Chrome/%s.0.0.0' % major
    return base + tail + ' Safari/537.36'



# ================================================================ 浏览器会话
# 桌面模式旗标：webui.serve_desktop 启动时置 True。置位后所有浏览器一律
# 屏幕外启动（--window-position=-32000,-32000），由守夜线程直接从屏外
# 入坞进「预览」页签——否则有头窗口会先在桌面正中闪一下、再被入坞/
# 最小化收走（真机 2026-10-10「点查询弹个窗口又秒没」）。网页模式保持
# False，行为同 v3.9：窗口可见，登录确认后 _quiet_win / brush_minimize
# 收进任务栏。
OFFSCREEN_LAUNCH = False

# 反检测补丁：抹掉最容易被风控识别的自动化特征。全部是「与真实浏览器对齐」
# 的防御性补丁，缺什么补什么，真实值存在时不动（伪装成固定值反而克隆化）。
# v4.3 起由 launch() 挂在**共享上下文**上，对每个新页面生效。
_STEALTH_JS = r"""
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
"""

# ---- 全局常驻浏览器进程（v4.3）-----------------------------------------
# 全进程只跑**一个** chrome 进程，独占 PROFILE_DIR。任务/登录/作业窗口都通过
# connect_over_cdp 连它、各开自己的 page。这样：
#   · profile 由这唯一进程独占 → 结构上消灭「多任务抢 profile」的真机根因；
#   · 不再每任务冷启动一整只浏览器 → 内存/耗时都降。
# 进程一旦起来就常驻（进程死了下次 launch 会重启它）。
_BROWSER = {'proc': None, 'port': None, 'hl': None, 'offscreen': None}
# v6.28：这一次运行**真正在用**的配置目录。默认就是 PROFILE_DIR；只有当它
# 被外部进程接力占住、`_reap_orphan_browser` 收不干净时，才会换成
# profile_next（换罐）。换罐不掉登录：cookie 在 state.json 里，launch 时
# restore_state 会重新灌回上下文。测试与 fresh/restore_profile 会重置它。
_ACTIVE_PROFILE = {'dir': None}
# 只护「确保进程在跑」这一段。用 RLock（不是 Lock）：形态切换要「停 + 起」
# 整体原子，那段自己持锁后还会调进 ensure_browser()（它也要这把锁），
# 普通 Lock 会把自己锁死。RLock 让同一线程重入、别的线程照样等。
_BROWSER_LOCK = threading.RLock()

# v6.30：**最近几次启动**用过的 DevTools 端口。收孤儿前按端口反查属主，
# 把「正在为我们服务的那只浏览器」从「孤儿」名单里排除掉。
# 真机 bug：Edge 启动器退出、真身接管端口，旧代码把这当成「被别的实例
# 转交」并把真身收掉——窗口弹出来又消失，反复一轮轮闪。
# 只留最近 4 个（同一时刻最多一个主进程在用，多留几个是为了覆盖「刚起完
# 还没登记就被别处调用收孤儿」的窗口期）。
_SPAWN_PORTS = []
_PORT_HISTORY = 4
# 端口反查的总闸：默认开。测试套件把它关掉，避免桩数据里的假端口号
# 恰好撞上系统真实监听的端口（比如 --remote-debugging-port=1 是 TCPSVCS）。
_PORT_LOOKUP_ON = True

# 每线程缓存自己的 (playwright, browser, context)。Playwright sync API 必须在
# 创建它的线程里调用；launch() 在哪个线程调就在该线程 connect，同线程复用。
# 约定：ctx 不跨线程传递。
_TLS = threading.local()

# 共享上下文对象（进程内唯一，多个线程的 CDP 连接都指向它）。反检测补丁是
# 上下文级的，只需装一次；用 ctx 对象身份去重。
_LAUNCH_CTX = {'ctx': None}

# 网页模式画布指纹随机化的候选分辨率（见 launch 里 offscreen 分支）。
_VIEWPORTS = [(1440, 900), (1536, 864), (1366, 768), (1600, 900), (1920, 969)]


def browser_proc_pid():
    """常驻浏览器**主进程** PID（入坞线程据此只认工具自己的窗口）。

    用户自己开的 Edge/Chrome 与工具浏览器窗口类名同为 Chrome_WidgetWin_1，
    但属主进程 PID 不同——按 PID 一刀切能精确区分（真机探针实测：工具窗口
    pid == 常驻浏览器主进程 pid，外来浏览器 pid 不同）。没起来返回 None。
    """
    proc = _BROWSER.get('proc')
    return proc.pid if proc is not None and proc.poll() is None else None


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_devtools(port, deadline):
    url = 'http://127.0.0.1:%d/json/version' % port
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if json.loads(r.read().decode('utf-8')).get('webSocketDebuggerUrl'):
                    return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def _active_profile():
    """这一次运行实际使用的浏览器配置目录（v6.28，默认就是 PROFILE_DIR）。"""
    return _ACTIVE_PROFILE.get('dir') or PROFILE_DIR


def _reset_active_profile():
    """回到默认配置目录（fresh_profile / restore_profile 归档改名后必须调）。"""
    _ACTIVE_PROFILE['dir'] = None


def _profile_lockfile():
    """profile 目录里那个被 Chromium 独占打开的锁文件（Windows 上是 Lockfile）。

    为什么拿它当「谁占着 profile」的判据：Edge/Chrome 启动时会把
    `--user-data-dir` 下的 Lockfile 以**独占方式**打开并一直攥着，直到进程
    退出。要问「还有没有别的浏览器实例占着这个 profile」，问 Restart Manager
    「谁锁着这个文件」最直接（`_profile_holders`）。注意 Windows 版**没有**
    SingletonLock（那是 Linux 的东西），别照抄 Linux 的习惯去 stat 一个
    根本不存在的文件——旧版就是这么写的，于是这条预检永远返回 False。
    """
    for name in ('Lockfile', 'lockfile', 'SingletonLock'):
        f = _active_profile() / name
        try:
            if f.exists():
                return f
        except Exception:
            continue
    return None


def _profile_holders():
    """谁正攥着 PROFILE_DIR 的锁文件（Restart Manager 的「文件被谁占用」查询）。

    返回 [(pid, 显示名)]；查不出来就返回 []（绝不抛）。

    为什么要它：v6.26 真机「点查询后弹出两个浏览器窗口，接着所有内核都启动
    失败」。根因是**上一次运行留下的失联浏览器还在用同一个 profile**——
    工具自己死了（崩溃/被关/被结束进程），它拉起的 msedge 并不会跟着死
    （实测：父进程退出后 Edge 照样活着，还听在原来的 DevTools 端口上）。
    下一次启动时 `_start_browser_process` 起的新进程会把参数交给那只老实例、
    自己秒退；「秒退」又被当成「profile 被别的实例占着」，于是逐候选全红。
    更糟的是老实例被转交参数后**又开了一个窗口**——用户看到的就是两个窗口。

    「我们自己的常驻进程」不算占用：调用方（`_reap_orphan_browser`）会跳过
    `_BROWSER['proc']` 那个 pid。
    """
    out = []
    try:
        import ctypes
        from ctypes import wintypes

        f = _profile_lockfile()
        if f is None:
            return out
        rm = ctypes.WinDLL('rstrtmgr', use_last_error=True)
        rm.RmStartSession.argtypes = [ctypes.POINTER(wintypes.DWORD),
                                      wintypes.DWORD, wintypes.LPWSTR]
        rm.RmRegisterResources.argtypes = [
            wintypes.DWORD, wintypes.UINT, ctypes.POINTER(wintypes.LPCWSTR),
            wintypes.UINT, ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p]
        rm.RmGetList.argtypes = [
            wintypes.DWORD, ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(wintypes.UINT), ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD)]
        rm.RmEndSession.argtypes = [wintypes.DWORD]

        class _FT(ctypes.Structure):
            _fields_ = [('dwLowDateTime', wintypes.DWORD),
                        ('dwHighDateTime', wintypes.DWORD)]

        class _UP(ctypes.Structure):
            _fields_ = [('dwProcessId', wintypes.DWORD),
                        ('ProcessStartTime', _FT)]

        class _PI(ctypes.Structure):
            _fields_ = [('Process', _UP), ('strAppName', wintypes.WCHAR * 256),
                        ('strServiceShortName', wintypes.WCHAR * 64),
                        ('ApplicationType', ctypes.c_int),
                        ('AppStatus', wintypes.ULONG),
                        ('TSSessionId', wintypes.DWORD),
                        ('bRestartable', wintypes.BOOL)]

        key = ctypes.create_unicode_buffer(33)
        sess = wintypes.DWORD(0)
        if rm.RmStartSession(ctypes.byref(sess), 0, key):
            return out
        try:
            arr = (wintypes.LPCWSTR * 1)(str(f))
            if rm.RmRegisterResources(sess.value, 1, arr, 0, None, 0, None):
                return out
            need = wintypes.UINT(0)
            have = wintypes.UINT(0)
            reasons = wintypes.DWORD(0)
            rc = rm.RmGetList(sess.value, ctypes.byref(need),
                              ctypes.byref(have), None, ctypes.byref(reasons))
            if rc not in (0, 234) or not need.value:
                return out
            infos = (_PI * need.value)()
            have.value = need.value
            rc = rm.RmGetList(sess.value, ctypes.byref(need),
                              ctypes.byref(have),
                              ctypes.cast(infos, ctypes.c_void_p),
                              ctypes.byref(reasons))
            if rc:
                return out
            out = [(int(infos[i].Process.dwProcessId),
                    str(infos[i].strAppName or ''))
                   for i in range(have.value)]
        finally:
            try:
                rm.RmEndSession(sess.value)
            except Exception:
                pass
    except Exception:
        return out
    return out


# 从 pythonw.exe 里起控制台程序（powershell / taskkill）会**弹一个黑窗**。
# 真机日志里「终端一直闪」就是这个：每轮清孤儿都要跑一次 PowerShell 查命令行。
# CREATE_NO_WINDOW 让子进程不带控制台，用户什么都看不见。
_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)


def _run_hidden(argv, timeout=20):
    """跑控制台程序但**不弹窗**，返回 CompletedProcess（文本模式）。"""
    kw = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
              text=True, encoding='utf-8', errors='replace',
              timeout=timeout, creationflags=_NO_WINDOW)
    try:
        return subprocess.run(argv, **kw)
    except TypeError:            # 极老的 Python 不认 creationflags
        kw.pop('creationflags', None)
        return subprocess.run(argv, **kw)


def _pid_alive(pid):
    """这个 pid 现在还活着吗（进程刚退出时 exe 名查不到，别当成占用者）。"""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                    wintypes.DWORD]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        k32.CloseHandle(ctypes.c_void_p(h))
        return True
    except Exception:
        return False


def _profile_free(timeout=0.0):
    """profile 现在是否真的没人占着（v6.28）。

    判据：**直接独占打开 Lockfile**（CreateFileW 共享模式 = 0）。能打开就说明
    Chromium 已经放开；打不开（ERROR_SHARING_VIOLATION 32 / 拒绝访问 5）就是
    还占着。这比「Restart Manager 报不报持有人」可靠——真机探针实测出现过
    RM 返回空、独占打开却仍然 32 的情况（RM 只报一个，别的进程还在攥着）。

    没有 Lockfile 文件也算放开（首次运行 / 刚换罐）。timeout > 0 时轮询等待。
    """
    deadline = time.time() + max(float(timeout), 0.0)
    while True:
        f = _profile_lockfile()
        if f is None:
            return True
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL('kernel32', use_last_error=True)
            k32.CreateFileW.restype = ctypes.c_void_p
            k32.CreateFileW.argtypes = [
                wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                ctypes.c_void_p]
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            # GENERIC_READ|GENERIC_WRITE, 共享 0 = 独占；OPEN_EXISTING
            h = k32.CreateFileW(str(f), 0x80000000 | 0x40000000, 0, None, 3,
                                0x80, None)
            bad = ctypes.c_void_p(-1).value
            if h and h != bad:
                k32.CloseHandle(h)
                return True
        except Exception:
            # 判断不了就别拦着启动（宁可让后面的启动去报真实错误）
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.2)


def _browser_procs():
    """列出 Edge/Chrome 进程的 (pid, 命令行)。查不到返回 []（v6.29）。

    **必须走 base64**：PowerShell 往管道里写的是**系统 ANSI 代码页**编码的
    文本（中文系统是 GBK），而本工具装在中文路径下极常见——解压出来的目录
    就叫「学习通巡检工具」，cmdline 里必然带它。按 UTF-8 解码会整段变成
    替换字符，`prof in cmd` 永远不命中。

    2026-10-20 真机抽验实测：同一份输出按 UTF-8 解命中 **0** 个进程，按 GBK
    解命中 **15** 个。后果是 `_profile_free()` 说「锁被占着」、这边却一个
    占用者都找不出来，每轮都只能换配置目录——用户看到的就是「一直重复闪烁」。

    base64 的输出只含 ASCII，与代码页无关。同时把查询合并到一处，别让
    `_profile_pids` / `_profile_roots` 各抄一份逻辑再各错一次。
    """
    out = []
    try:
        ps = ("$j = Get-CimInstance Win32_Process -Filter "
              "\"name='msedge.exe' or name='chrome.exe'\" | "
              "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress; "
              "[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("
              "[string]$j))")
        r = _run_hidden(['powershell', '-NoProfile', '-NonInteractive',
                         '-Command', ps], timeout=20)
        txt = (r.stdout or '').strip()
        if not txt:
            return out
        # 只取最后一行：PowerShell 偶尔会先吐一行警告
        txt = txt.splitlines()[-1].strip()
        import base64 as _b64
        txt = _b64.b64decode(txt, validate=False).decode('utf-8', 'replace')
        if not txt.strip():
            return out
        d = json.loads(txt)
        if isinstance(d, dict):
            d = [d]
        for x in d:
            try:
                pid = int(x.get('ProcessId') or 0)
                cmd = str(x.get('CommandLine') or '')
            except Exception:
                continue
            if pid:
                out.append((pid, cmd))
    except Exception:
        return out
    return out


def _profile_pids():
    """命令行里带本工具配置目录的进程 pid（v6.28）。

    为什么需要它：Restart Manager 一次只报**一个**持有人，而 Edge 是十几个
    进程（browser/renderer/gpu/utility）共用同一份 profile。只收 RM 报的那
    一个，下一轮立刻换另一个上报——真机日志里 pid 6368→4788→20304→21036
    就是这个现象。
    只认「命令行里带本 profile 路径」的，绝不按进程名一刀切——用户自己的
    Edge 用的是别的 profile，不能碰。
    """
    out = []
    prof = str(_active_profile() or '')
    for pid, cmd in _browser_procs():
        if _cmd_has_profile(cmd, prof):
            out.append(pid)
    return out


def _profile_roots():
    """命令行里带本活动 profile、且**是浏览器主进程**的 pid（v6.29）。

    为什么只认主进程：Chromium 的 renderer/gpu/utility 子进程**也**带
    `--user-data-dir`，但它们不是占着 profile 的那一个，而且杀了也没用——
    主进程会立刻补一个新的。真机日志（2026-10-20 07:21）就是每轮杀几个
    renderer、下一轮又冒出新的，永远收不干净。主进程没有 `--type=` 参数，
    用这个一眼就分得开；杀主进程（taskkill /T）整棵树一起走。
    """
    out = []
    prof = str(_active_profile() or '')
    for pid, cmd in _browser_procs():
        if _cmd_has_profile(cmd, prof) and '--type=' not in cmd:
            out.append(pid)
    return out

def _exe_name_of(pid):
    """进程 exe 文件名（小写，如 'msedge.exe'）；查不到返回 ''。

    只用 QueryFullProcessImageNameW（PROCESS_QUERY_LIMITED_INFORMATION）——
    读进程命令行要 READ_PROCESS_MEMORY，跨权限常常读不到（本机实测
    ERROR_PARTIAL_COPY 299），靠不住。
    """
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                    wintypes.DWORD]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD)]
        h = k32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return ''
        try:
            n = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(n.value)
            if not k32.QueryFullProcessImageNameW(h, 0, buf,
                                                  ctypes.byref(n)):
                return ''
            return os.path.basename(buf.value).lower()
        finally:
            k32.CloseHandle(h)
    except Exception:
        return ''


def _is_browser_exe(pid):
    """这个 pid 是不是浏览器内核进程（Edge/Chrome），而不是别的什么东西。

    清理孤儿绝不能按「谁锁着 Lockfile」一刀切：同一份 profile 也可能被别的
    软件打开（用户拿文件管理器进去过、杀软在扫）。杀错就是「把用户正在用的
    东西关了」——本项目第一条纪律是**绝不杀用户的浏览器**。所以只认 exe 名
    明确是 msedge/chrome 的进程；别的占用者一律只报告、不动手。
    """
    name = _exe_name_of(pid)
    return bool(name) and name.startswith(('msedge', 'chrome'))


def _profile_locked():
    """profile 目录是否已被某个浏览器实例占着（诊断 + 启动前预检）。

    判据是 Lockfile 被活进程持有（见 `_profile_holders`）；本进程自己养着的
    常驻浏览器也算「占着」，但那种情况走 `ensure_browser` 的幂等短路。
    """
    proc = _BROWSER.get('proc')
    if proc is not None and proc.poll() is None:
        return True
    try:
        return bool(_profile_holders())
    except Exception:
        return False


def _browser_argv(exe, port, hl, offscreen, ua):
    # v6.28：用**这次运行真正在用**的配置目录（换罐后就不是 PROFILE_DIR 了）
    argv = [str(exe), '--remote-debugging-port=%d' % port,
            '--user-data-dir=%s' % _active_profile()]
    argv += [
        '--no-sandbox',
        '--disable-dev-shm-usage',
        '--disable-blink-features=AutomationControlled',
        '--disable-features=Translate,BackForwardCache',
        '--no-first-run',
        '--no-default-browser-check',
        # v6.28：不让 Edge 在最后一个窗口关掉后驻留后台/被会话恢复拉起来。
        # 真机上就是这类进程在「接力」重开浏览器，把 profile 锁重新抢走。
        '--disable-background-mode',
        # 刷课时窗口会最小化到任务栏（brush_minimize，默认开）：这三个开关
        # 保证最小化/被遮挡后计时器与播放不被 Chromium 后台节流，心跳照常
        '--disable-background-timer-throttling',
        '--disable-backgrounding-occluded-windows',
        '--disable-renderer-backgrounding',
        # 非 Playwright 启动的浏览器要靠命令行自报：据实内核的 UA（抹掉
        # HeadlessChrome 记号）+ 中文语言，与旧 launch 的 locale/user_agent 对齐。
        '--user-agent=%s' % ua,
        '--lang=zh-CN',
        '--accept-lang=zh-CN,zh;q=0.9',
    ]
    if offscreen:
        argv += ['--window-position=-32000,-32000', '--window-size=1440,900']
    if hl:
        argv += ['--headless=new']
    return argv


def _kill_tree(proc):
    """杀掉 proc **及其整棵子进程树**，并等它退干净（收不掉就强杀）。

    chromium 的 renderer/gpu/utility 子进程同样带 `--user-data-dir=PROFILE_DIR`
    ——只杀主进程的话它们还攥着 profile 目录，紧接着对同一个目录的**启动/改名/
    删除**都会撞 Windows 占用错误（真机 WinError 183 / 审查发现 WinError 5 的
    共同病根）。凡是要放掉 profile 的场合（换号归档、候选重试）都走这里。
    """
    if proc is None:
        return
    try:
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=15, creationflags=_NO_WINDOW)
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# Windows Job Object 句柄（v6.27）。进程被杀/崩溃时由内核连带收掉里面所有
# 进程——浏览器是独立进程，父进程死了它不会跟着死，这就是「失联浏览器攥着
# profile」的源头。句柄放模块级：一旦被关（含进程退出），内核就杀整棵树。
_JOB = {'handle': None, 'failed': False}


def _cmd_has_profile(cmd, prof=None):
    """这条命令行是不是带着**本工具这次活动**的配置目录（v6.30）。

    比 `prof in cmd` 稳：Windows 路径不分大小写，Chromium 也可能把
    `--user-data-dir=` 写成正斜杠或改大小写。统一成小写 + 反斜杠再比，
    免得「明明是我们的浏览器，却因为一个字母大小写认不出来」——那会直接
    退化成「收不掉 → 换罐 → 又闪一次」。
    """
    prof = str(prof if prof is not None else (_active_profile() or ''))
    if not prof:
        return False
    norm = str(cmd or '').replace('/', '\\').lower()
    return prof.replace('/', '\\').lower() in norm


def _port_served_by_us(port):
    """这个 DevTools 端口是不是**我们刚起的浏览器**在服务（v6.30）。

    判据（三条同时成立才算）：
      ① 端口属主进程还活着，且 exe 名是 msedge/chrome；
      ② 它的命令行带**本工具的 profile**（`_active_profile()`）；
      ③ 它的命令行带 `--remote-debugging-port=<这个端口>`。

    为什么必须这么判（v6.30 真机核心 bug）：`_start_browser_process` 原来
    只看「`_wait_devtools` 有应答 + Popen 的 pid 已经退出」就断定「参数被别的
    实例转交了」，然后**把那个实例收掉**。可 Edge 在某些机器上会先起一个
    启动器替身、自己退出，真身另起 pid 接管同一个端口——端口是我们随机挑的
    空闲端口，别人的实例不可能来听它。于是工具把自己刚拉起来的浏览器当孤儿
    杀掉：窗口弹出来又消失，换候选再来一遍，用户看到的就是「一直弹窗然后
    消失、重复这个动作」，最后报「进程秒退（profile 被别的实例占着）」。
    """
    if not _PORT_LOOKUP_ON:
        return None
    try:
        pid = _port_owner(port)
        if not pid:
            return None
        if not _is_browser_exe(pid):
            return None
        flag = '--remote-debugging-port=%d' % int(port)
        prof = str(_active_profile() or '')
        for p, cmd in _browser_procs():
            if p != pid:
                continue
            if (_cmd_has_profile(cmd, prof) and flag in cmd
                    and '--type=' not in cmd):
                return pid
        return None
    except Exception:
        return None


def _port_owner(port):
    """哪个进程在监听这个 TCP 端口（纯 ctypes iphlpapi，约 1 毫秒）。

    v6.30 用它分辨「新起的进程秒退」到底是怎么回事：
      · 若**同一端口**始终有个浏览器主进程在听，那说明我们启动的只是
        Edge 的启动器替身，真正的浏览器接管了端口——绝不能当成「参数被
        别的实例转交」去杀掉（那正是用户看到的「窗口弹出来又消失」）；
      · 端口属主正好是 `_BROWSER['proc'].pid`，就是最普通的正常情况。

    读法与 _probe 实测一致：`GetExtendedTcpTable(class=3 TCP_TABLE_OWNER_PID_
    LISTEN, sorted=True)`，`dwLocalPort` 是网络字节序的低 16 位。
    """
    try:
        import ctypes
        from ctypes import wintypes

        class _ROW(ctypes.Structure):
            _fields_ = [('dwState', wintypes.DWORD),
                        ('dwLocalAddr', wintypes.DWORD),
                        ('dwLocalPort', wintypes.DWORD),
                        ('dwRemoteAddr', wintypes.DWORD),
                        ('dwRemotePort', wintypes.DWORD),
                        ('dwOwningPid', wintypes.DWORD)]

        ip = ctypes.WinDLL('iphlpapi')
        ip.GetExtendedTcpTable.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
            wintypes.ULONG, wintypes.ULONG, wintypes.DWORD]
        sz = wintypes.DWORD(0)
        ip.GetExtendedTcpTable(None, ctypes.byref(sz), True, 2, 3, 0)
        if not sz.value:
            return None
        buf = ctypes.create_string_buffer(sz.value)
        if ip.GetExtendedTcpTable(buf, ctypes.byref(sz), True, 2, 3, 0):
            return None
        n = wintypes.DWORD.from_buffer(buf, 0).value
        arr = (_ROW * n).from_buffer(buf, 4)
        for r in arr:
            local = ((r.dwLocalPort & 0xFF) << 8) | ((r.dwLocalPort >> 8) & 0xFF)
            # 2 = MIB_TCP_STATE_LISTEN
            if local == int(port) and r.dwState == 2:
                return int(r.dwOwningPid) or None
    except Exception:
        return None
    return None


def _adopt_job(proc):
    """把刚起的浏览器进程放进「本进程一死就跟着死」的 Job Object（v6.27）。

    为什么需要：v6.26 真机的那个 bug（两个窗口 + 全部内核启动失败）根子在
    **上一次运行之后浏览器还活着**——用户关掉/结束工具进程时，它拉起的
    msedge 是独立进程，父进程死了照样跑，还攥着 runtime/profile 不放。
    之后每次启动都会把参数转交给那只老实例（于是多开一个窗口），新进程
    自己秒退（于是报「所有内核启动失败」）。

    现在两道防线：
      · 这条（提前防）：给浏览器挂一个 KILL_ON_JOB_CLOSE 的 Job Object。
        本进程无论怎么没的（正常退出、崩溃、被任务管理器结束），内核都会
        连带把浏览器整棵树收掉，从源头不再产生孤儿；
      · `_reap_orphan_browser`（事后兜）：万一还是留下了（比如旧版本跑过，
        或 Job 挂不上），启动前把它认出来收掉。

    挂不上（受限环境 / 进程已在别的不可嵌套 Job 里）不算错：只记一条
    debug 级日志，照常启动——宁可偶尔留孤儿，也不能因为挂 Job 失败就
    让工具起不来。返回 True 表示这次挂上了。
    """
    if os.name != 'nt':
        return False
    if _JOB['failed']:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p,
                                                 ctypes.c_void_p]

        class _IO(ctypes.Structure):
            _fields_ = [('ReadOperationCount', ctypes.c_ulonglong),
                        ('WriteOperationCount', ctypes.c_ulonglong),
                        ('OtherOperationCount', ctypes.c_ulonglong),
                        ('ReadTransferCount', ctypes.c_ulonglong),
                        ('WriteTransferCount', ctypes.c_ulonglong),
                        ('OtherTransferCount', ctypes.c_ulonglong)]

        class _BASIC(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong),
                        ('PerJobUserTimeLimit', ctypes.c_longlong),
                        ('LimitFlags', wintypes.DWORD),
                        ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t),
                        ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.c_size_t),
                        ('PriorityClass', wintypes.DWORD),
                        ('SchedulingClass', wintypes.DWORD)]

        class _EXT(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', _BASIC), ('IoInfo', _IO),
                        ('ProcessMemoryLimit', ctypes.c_size_t),
                        ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t),
                        ('PeakJobMemoryUsed', ctypes.c_size_t)]

        if _JOB['handle'] is None:
            h = k32.CreateJobObjectW(None, None)
            if not h:
                _JOB['failed'] = True
                return False
            info = _EXT()
            # 0x2000 = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            info.BasicLimitInformation.LimitFlags = 0x2000
            # 9 = JobObjectExtendedLimitInformation
            k32.SetInformationJobObject(h, 9, ctypes.byref(info),
                                        ctypes.sizeof(info))
            _JOB['handle'] = h
        ok = k32.AssignProcessToJobObject(
            ctypes.c_void_p(_JOB['handle']),
            ctypes.c_void_p(int(proc._handle)))
        if not ok:
            # 句柄可能已经死了（进程刚起就退出）：这不是配置错误，别把
            # _JOB['failed'] 置位，后续候选还要接着挂。
            return False
        return True
    except Exception:
        _JOB['failed'] = True
        return False


def _adopt_job_pid(pid):
    """把**已存在的**进程按 pid 挂进同一个 Job（v6.30）。

    为什么需要：v6.30 起「浏览器主进程」不一定等于 Popen 返回的 pid——
    Edge 的启动器替身会退出、真正的浏览器另起一个 pid 接管 DevTools 端口
    （`_port_owner` 反查得到）。替身挂上了 Job 也没用，必须把真身也挂上，
    否则真身死了/本进程被杀时它照样变成孤儿。
    """
    if os.name != 'nt' or _JOB['failed'] or _JOB['handle'] is None:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                    wintypes.DWORD]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p,
                                                 ctypes.c_void_p]
        # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        h = k32.OpenProcess(0x0100 | 0x0001, False, int(pid))
        if not h:
            return False
        try:
            return bool(k32.AssignProcessToJobObject(
                ctypes.c_void_p(_JOB['handle']), ctypes.c_void_p(h)))
        finally:
            k32.CloseHandle(ctypes.c_void_p(h))
    except Exception:
        return False


class _PidProc(object):
    """给「不是本进程 Popen 出来的」浏览器主进程做的最小替身（v6.30）。

    `_BROWSER['proc']` 的既有契约是「有 pid、poll()、句柄」三样：
    `browser_proc_pid()` 要 pid（网页模式按属主 pid 认窗口）、
    `ensure_browser` 要 `poll()` 判活、`stop_browser` 要能收掉它。
    Edge 启动器替身退出后真身是别人起的进程，拿不到 Popen 对象，就用这个
    按 pid 判活的替身顶上——否则「端口明明在应答，却报浏览器没起来」。
    """

    def __init__(self, pid):
        self.pid = int(pid)
        self.returncode = None

    def poll(self):
        if _pid_alive(self.pid):
            return None
        self.returncode = 0
        return self.returncode

    def wait(self, timeout=None):
        deadline = time.time() + (timeout if timeout is not None else 15)
        while time.time() < deadline:
            if self.poll() is not None:
                return self.returncode
            time.sleep(0.1)
        raise subprocess.TimeoutExpired('pid %d' % self.pid, timeout)

    def kill(self):
        _kill_pid_tree(self.pid)


def _kill_pid_tree(pid, timeout=15):
    """按 pid 杀掉整棵进程树（与 _kill_tree 同一套 taskkill /F /T）。

    与 _kill_tree(proc) 的分工：那个收的是本进程 Popen 出来的子进程（有
    proc 句柄、能 wait）；这个收的是**上一次运行遗留**的进程（本进程没有
    它的句柄，只知道 pid）。两者都必须 /T，否则 renderer/gpu 子进程还会
    攥着 profile 目录。
    """
    try:
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(int(pid))],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=timeout, creationflags=_NO_WINDOW)
    except Exception:
        pass


def _reap_orphan_browser(why='profile 被上一次留下的浏览器占着'):
    """收掉**上一次运行遗留**的浏览器进程树，把 profile 放出来（v6.27）。

    真机 2026-10-19（v6.26 打包版）：点「获取课程列表」→ 任务栏冒出**两个**
    Edge 窗口 → 紧接着报「所有浏览器内核都无法启动：exe(msedge.exe) → 进程
    秒退 / channel(msedge.exe) → DevTools 未就绪」，预览页签永远空白。

    根因链（本机复现 + 探针实测）：
      ① 上一次运行结束时浏览器没被收干净（进程崩溃/被结束/退出路径没走到），
         msedge 是独立进程，父进程死了它照样活着，并且**继续占着 profile**
         （Lockfile 一直被它独占打开）；
      ② 下次启动 `_start_browser_process` 用同一份 profile 起新进程 →
         Chromium 发现已有实例，把参数**转交**给它、自己秒退；
      ③ 老实例接到转交后**又开一个窗口**（用户看到的第二个窗口）；
      ④ 新进程秒退被当成「profile 被别的实例占着」→ 逐候选全红 → 报错；
         预览页签里没有属于本进程登记的浏览器，于是「无内容」。
    注意 ④ 的报错文案一直是对的——占着 profile 的**确实是**别的实例，
    只是旧代码只诊断、不处理。

    这里在起新进程**之前**把那只孤儿收掉（`taskkill /F /T` 收整棵树），
    然后才允许启动。安全边界（本项目铁律：绝不杀用户的浏览器）：
      · 只处理锁着**本工具 profile** 的进程，不去扫系统里别的 Edge/Chrome；
      · 只处理 exe 名是 msedge/chrome 的进程（`_is_browser_exe`）；
      · 正在服务本进程的那个 pid 跳过（`_BROWSER['proc']`）；
      · 别的软件占着 Lockfile 时**不动手**，只记一条 warn 并返回 False，
        让调用方照常试——起不来就把真实原因报给用户。
    返回 True 表示「现在 profile 是空的（本来就没被占，或者已收干净）」。
    """
    own = _BROWSER.get('proc')
    own_pid = own.pid if own is not None and own.poll() is None else None
    # v6.30：**正在为本次启动服务的浏览器不算孤儿**。
    # 真机链条是这样的：我们起 Edge → 启动器退出 → 真身接管端口 → 旧代码
    # 判「秒退=被接管」→ 调这里收孤儿 → 把刚拉起来的真身一并杀了 → 窗口
    # 一闪即逝 → 换候选再来一次。所以这里先把「自己人」的 pid 全捞出来。
    mine = set()
    if own_pid:
        mine.add(own_pid)
    try:
        for _p in (_port_served_by_us(port) for port in _SPAWN_PORTS):
            if _p:
                mine.add(_p)
    except Exception:
        pass
    # 先问「真的放开没有」——直接独占打开 Lockfile，比 Restart Manager
    # 报不报持有人可靠（真机探针见过 RM 空、独占仍 32）。
    if _profile_free():
        return True

    killed = set()
    refused = {}                   # pid -> exe 名（去重：别把同一个人报三遍）
    # **只收浏览器主进程**（v6.29）：renderer/gpu/utility 也带 --user-data-dir，
    # 但它们不是占着 profile 的那一个，杀了主进程会立刻补一个新的——真机日志
    # （2026-10-20 07:21:49~07:22:13）就是每轮杀几个子进程、下一轮又冒出来，
    # 收四轮也收不干净，用户看到的是「浏览器一直在闪」。杀主进程用 taskkill /T
    # 整棵树一起走，一次就够。
    #
    # 仍然**有限轮**（v6.28）：真机出现过主进程也被接力重开的情况；三轮还不
    # 放开就如实返回 False，交给上层换一份新配置目录。
    for rnd in range(3):
        pids = set(_profile_roots())
        pids -= mine
        pids -= killed
        pids -= set(refused)
        if not pids:
            break
        killed_now = False
        for pid in sorted(pids):
            if not _is_browser_exe(pid):
                # v6.29：进程已经退出了（exe 名自然查不到）——那不是「别的软件
                # 占着」，别拿它当理由放弃。真机日志里那 9 个「未知」就是查询
                # 结束后正在退出的 renderer，被误报成「非浏览器进程占着」。
                if _pid_alive(pid):
                    refused[pid] = _exe_name_of(pid) or '未知'
                continue
            log('发现上次运行遗留的浏览器进程（pid %d）还占着浏览器配置目录，'
                '先收掉它（第 %d 轮）。' % (pid, rnd + 1), 'warn')
            _kill_pid_tree(pid)
            killed.add(pid)
            killed_now = True
        # 有活着的非浏览器进程攥着：再收多少轮都放不开，立刻如实返回——
        # 让上层换罐，而不是在这儿白等 3 轮 × 3 秒。
        if refused or not killed_now:
            break
        # Windows 释放文件句柄有滞后：等锁真放开再复核。
        if _profile_free(timeout=3.0):
            break

    if refused:
        who = '、'.join('%d(%s)' % (pid, name)
                        for pid, name in sorted(refused.items()))
        log('profile 还被非浏览器进程占着：%s。不动它（绝不杀用户的东西），'
            '改用一份新的浏览器配置目录启动。' % who, 'warn')
        return False
    # 复核：先看文件锁放没放开；再确认没有**别人的**主进程还带着本 profile
    # 活着（Lockfile 偶有删除/重建的空窗，光看它不够）。自己人（`mine`）
    # 不算——它们正是我们接下来要用的那只。
    others = set(_profile_roots()) - mine
    if _profile_free(timeout=1.0) and not others:
        return True
    if _profile_free(timeout=1.0):
        # 锁放开了但主进程还在（多半是刚起来还没建锁 / 正在退出）：等它一下。
        for _ in range(10):
            if not (set(_profile_roots()) - mine):
                return True
            time.sleep(0.2)
    if not others and mine:
        # v6.30：锁被占着，但占着的是**我们自己人**（刚起的浏览器 / 已登记在
        # _BROWSER 里的那只）。这不需要「清场」，更不该说「有东西在接力重开
        # 浏览器」——直接用那只即可。
        #
        # `and mine` 不能省：`others` 只统计浏览器主进程，用户拿别的软件
        # （记事本、杀软、文件管理器）打开过 Lockfile 时它同样是空的，那种
        # 情况必须如实返回 False——否则上层以为「profile 空了」，紧接着的
        # 启动又会被锁挡住，白白多闪一轮。
        return True
    try:
        left = [pid for pid, _n in _profile_holders() if pid not in mine]
    except Exception:
        left = []
    log('收掉 %d 个遗留浏览器进程后 profile 仍被占着（%s）——这台机器上'
        '有东西在接力重开浏览器。改用一份新的浏览器配置目录启动。'
        % (len(killed), '、'.join(str(x) for x in left) or '锁未放开'), 'warn')
    return False


def _switch_profile_dir():
    """换一份全新的浏览器配置目录，并返回是否换成了（v6.28）。

    什么时候走到这里：`_reap_orphan_browser` 连收三轮，`_profile_free()`
    仍说 Lockfile 被占——真机出现过（杀一个、另一个顶上来）。这时候继续
    用同一份 profile 起进程，Chromium 只会把参数转交给老实例、自己秒退，
    永远起不来。换罐是唯一能往前走的路。

    **必须换路径**：原地把旧目录删掉再 mkdir 同一个路径没用——抢锁的老实例
    还攥着那个路径的 Lockfile，新进程照样被转交。所以这里另起 `profile_next`
    （与 `profile_old` 同级），活动目录跟着换过去（`_ACTIVE_PROFILE`）。

    **不掉登录**：cookie/登录态在 `runtime/state.json`（`save_state` /
    `restore_state`），不在 profile 里；launch 之后 `restore_state` 会把
    会话灌回新上下文。所以这里只是丢掉浏览器缓存，不动账号。
    """
    global PROFILE_DIR
    try:
        stop_browser()
    except Exception:
        pass
    # 以**当前活动目录**为基准：第二次换罐时 PROFILE_DIR 早不是在用的那个。
    cur = _active_profile()
    old = cur.with_name('profile_stale')
    _rmtree_retry(old)              # 上一次换罐留下的残骸（句柄滞后时重试）
    # 当前目录能改名就改名归档（改名成功 = 没人占着，最干净）；改不动就
    # 就地弃用——反正这次运行不再用它。
    moved = False
    if cur.exists():
        moved = _rename_retry(cur, old)
        if not moved:
            log('旧配置目录改不动（被占着），就地弃用、另起一份新的。',
                'warn')
    if moved:
        # state.json / identity.json 归 runtime 根部，不在 profile 里；
        # 万一旧版本留在里面，挪回来（与 fresh_profile 的处理一致）。
        for nm, tgt in (('state.json', STATE_FILE),
                        ('identity.json', IDENTITY_FILE)):
            src = old / nm
            try:
                if src.exists() and not tgt.exists():
                    shutil.move(str(src), str(tgt))
            except Exception:
                pass
    # **新目录必须是另一个路径**（v6.28 修）：原地 mkdir 出来还是同一个被锁
    # 的路径，Chromium 照样把参数转交给抢锁的老实例——等于没换。
    new = None
    for i in range(1, 6):
        cand = PROFILE_DIR.with_name('profile_next' if i == 1
                                     else 'profile_next%d' % i)
        if not cand.exists() or _rmtree_retry(cand):
            new = cand
            break
    if new is None:
        log('换新配置目录失败（profile_next* 都被占着）。', 'warn')
        return False
    try:
        new.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log('新建浏览器配置目录失败：%s' % str(e)[:100], 'warn')
        return False
    _ACTIVE_PROFILE['dir'] = new
    log('已启用新的浏览器配置目录（登录态不受影响，启动后会自动恢复会话）。')
    return True


def _start_browser_process(cfg, hl, offscreen):
    """起常驻 chrome 进程（逐候选试），成功就登记到 _BROWSER。

    v6.28：整轮失败且 profile 确实被外部占着时，**换一份新配置目录重来一次**。
    真机（2026-10-19）出现过收不干净的情况：每轮都冒出一个新的浏览器进程
    抢回 Lockfile（pid 6368→4788→20304→21036），沿用同一份 profile 只会
    无限失败。换罐不掉登录（cookie 在 state.json），是唯一能往前走的路。
    """
    # v6.30 补：已经有一只活着就**别再起一只**。两个线程同时切形态时，
    # A 线程 stop_browser() 之后、ensure_browser() 之前，B 线程可能已经又起
    # 好了一只——这时再起，profile 的 Lockfile 被自己人攥着，新进程起不来，
    # 每个候选白等 25 秒（三个候选 = 75 秒），最后还被迫换罐。
    # 真机探针实测过这 73 秒。
    # 注意：**不要**把 _BROWSER['hl'] 改成调用方要的形态——进程实际是什么形态
    # 就登记什么。谎报会让上层「形态比对」误判成一致，拿屏幕外的窗口去干
    # 需要用户扫码的活（登录）。形态不一致由 ensure_browser 的调用方负责
    # stop + 重起。
    _cur = _BROWSER.get('proc')
    if _cur is not None:
        try:
            _alive = _cur.poll() is None
        except Exception:
            _alive = False
        if _alive and _BROWSER.get('port') is not None:
            return
    exe = _chrome_candidates(cfg)
    attempts = []
    if exe:
        attempts.append(('exe', str(exe)))
    attempts += [('channel', 'chrome'), ('channel', 'msedge')]
    errors = []
    for _round in range(2):
        for kind, val in attempts:
            if kind == 'exe':
                real = Path(val)
            else:
                real = _resolve_channel_exe(val)
                if not real:
                    errors.append('%s（未安装）' % val)
                    continue
            # 每个候选最多试两轮：第一轮撞上「上一次运行遗留的浏览器占着
            # profile」时，把遗留进程收掉再来一轮（v6.27）。
            for _try in range(2):
                # UA 按**这一次真用的内核**算：channel 名解析回真实 exe 是为了
                # 拿真实版本号（否则 UA 版本对不上内核）。
                ua = fake_user_agent(cfg, real)
                port = _free_port()
                # v6.30：端口必须**没被任何浏览器听着**。`_free_port()` 只是
                # 「bind 一下能拿到」（TIME_WAIT/SO_REUSEADDR 都会让它返回一个
                # 正被监听的口），紧接着的 `_port_served_by_us()` 就会把**上一轮
                # 那只还没退干净的浏览器**认成「这一轮我们起的」。重挑几次。
                if _PORT_LOOKUP_ON:
                    for _ in range(8):
                        if _port_owner(port) is None:
                            break
                        port = _free_port()
                _SPAWN_PORTS.append(port)
                del _SPAWN_PORTS[:-_PORT_HISTORY]
                # 起进程之前先把占着 profile 的遗留浏览器收掉（我们自己养着的
                # 那个 pid 不会被误伤，见 _reap_orphan_browser 的安全边界）。
                if _try == 0:
                    # 收完还放不开 = 这一份 profile 这次注定用不了：Chromium
                    # 只会把参数转交给占着它的实例、自己秒退。别白等 25 秒的
                    # DevTools 超时（真机 3 个候选 × 2 轮 = 两分半，用户只
                    # 看到窗口闪），直接判这一轮没戏，交给下面的换罐。
                    if not _reap_orphan_browser():
                        errors.append('%s(%s) → profile 被别的进程占着（收不掉）'
                                      % (kind, real.name))
                        break
                try:
                    proc = subprocess.Popen(
                        _browser_argv(real, port, hl, offscreen, ua),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception as e:
                    errors.append('%s(%s) → %s'
                                  % (kind, real.name, str(e)[:80]))
                    break
                _adopt_job(proc)
                ok = _wait_devtools(port, time.time() + 25)
                # v6.30：端口有应答之后，先确认「是谁在应答」。
                #
                # 旧代码在这里只看 `proc.poll() is not None` 就断定「参数被
                # 别的实例转交」，然后把那只实例收掉。真机上 Edge 会先起一个
                # 启动器替身、自己退出，真身另起 pid 接管同一个端口——于是
                # 工具**把自己刚拉起来的浏览器当孤儿杀了**：窗口弹出来又消失，
                # 换候选再闪一次（用户原话「一直弹出来这个浏览器窗口然后消失，
                # 重复进行」）。判据改成按端口反查属主（`_port_served_by_us`）：
                # 端口是我们随机挑的空闲端口 + 属主带本 profile 和本端口参数，
                # 那它就是我们自己人，接管即可；Popen 的 pid 退没退不影响结论。
                owner = _port_served_by_us(port) if ok else None
                if ok and owner:
                    if owner != proc.pid:
                        # 替身退了、真身接管：照着真身登记并挂进 Job。
                        log('%s 由启动器拉起（pid %d → %d），直接接管。'
                            % (real.name, proc.pid, owner))
                        _adopt_job_pid(owner)
                        proc = _PidProc(owner)
                    _BROWSER['proc'], _BROWSER['port'] = proc, port
                    _BROWSER['hl'], _BROWSER['offscreen'] = hl, offscreen
                    log('常驻浏览器已启动（%s%s，%s）' % (
                        '无头' if hl else '有窗口',
                        '·屏幕外' if offscreen else '', real.name))
                    return
                if ok and not owner:
                    # 端口在应答，但不是我们起的浏览器在服务（参数确实被
                    # 转交给了别人）。这才是真正的「被别的实例接管」：收掉
                    # 那只实例再试一轮。
                    _kill_tree(proc)
                    _reap_orphan_browser()
                    if _try == 0:
                        log('%s 的启动参数被已有浏览器实例接管，已收掉那个实例'
                            '后重试。' % real.name, 'warn')
                        continue
                    errors.append('%s(%s) → 进程秒退（profile 被别的实例占着）'
                                  % (kind, real.name))
                    break
                if not ok:
                    _kill_tree(proc)
                    # 起失败也可能留下攥着 profile 的子进程/重新拉起的进程，
                    # 不清掉会拖累下一个候选（真机 2026-10-19 的连环失败）。
                    _reap_orphan_browser()
                    errors.append('%s(%s) → DevTools 未就绪'
                                  % (kind, real.name))
                    break
        # 整轮候选全失败：如果根子在 profile 根本抢不回来，就换罐再来一轮。
        # 只在**确实还占着**时换（不是无脑换，免得白丢缓存）。
        if _round == 0 and not _profile_free():
            if _switch_profile_dir():
                errors.clear()
                continue
        break
    raise RuntimeError('所有浏览器内核都无法启动：\n    ' + '\n    '.join(errors))


def ensure_browser(cfg, hl, offscreen):
    """幂等：确保常驻 chrome 进程在跑。进程活着就直接返回。"""
    proc = _BROWSER['proc']
    if proc is not None and proc.poll() is None:
        return
    with _BROWSER_LOCK:
        proc = _BROWSER['proc']
        if proc is not None and proc.poll() is None:
            return
        _active_profile().mkdir(parents=True, exist_ok=True)
        _start_browser_process(cfg, hl, offscreen)


def browser_mode():
    """返回当前常驻浏览器的 (headless, offscreen)（没起来则为 (None, None)）。"""
    return _BROWSER['hl'], _BROWSER['offscreen']


def browser_port():
    return _BROWSER['port']


def stop_browser():
    """关掉常驻浏览器进程**及其整棵子进程树**（退出/换号归档 profile 时用）。

    必须连子进程一起收：chromium 的 renderer/gpu/utility 子进程同样带
    `--user-data-dir=PROFILE_DIR`，只杀主进程的话它们还攥着 profile 目录，
    紧接着 fresh_profile 的改名就撞 WinError 5（拒绝访问）——真机那个
    WinError 183 与之一脉相承（句柄没放）。/T 杀整棵树，等它们真退干净
    再返回（taskkill /F 之后仍可能滞后释放，故 fresh/restore 端另有重试）。
    """
    proc = _BROWSER['proc']
    _BROWSER['proc'], _BROWSER['port'] = None, None
    _kill_tree(proc)


def _drop_tls():
    """丢弃本线程缓存的 (pw, br, ctx)：收掉本线程的 playwright 实例。

    与 close_ctx 的分工：close_ctx 会**先关本线程开的页面**再收实例；这里
    用在「进程已换、旧连接与旧页面随之失效」的场合，只需让本线程的
    sync_playwright 实例干净退场——**不**去碰可能已死的连接/页面。

    为什么必须 pw.stop()：Playwright sync API 的实例把 asyncio 事件循环挂在
    **本线程**，不收掉就以挂起态留着，紧接着的 sync_playwright().start() 会报
    「It looks like you are using Playwright Sync API inside the asyncio loop」。
    v4.3 登录换罐正是这条路径：launch → stop_browser → fresh_profile → launch。
    """
    pw = getattr(_TLS, 'pw', None)
    if pw is not None:
        try:
            pw.stop()
        except Exception:
            pass
    _TLS.ctx = None
    _TLS.br = None
    _TLS.pw = None
    _TLS.pages = set()


def launch(cfg, headless=None, offscreen=None, auto_download=True):
    """确保全局常驻浏览器在跑，返回**本线程**连上去的 BrowserContext。

    v4.3 起改成「单进程 + 每线程 CDP 连接」：全进程只跑一个 chrome 进程
    （独占 profile），本函数负责确保它起着，并让调用线程 connect_over_cdp
    拿到共享上下文。返回的 context 接口与旧的 launch_persistent_context
    一致（new_page / add_cookies / clear_cookies / storage_state /
    set_default_timeout / add_init_script 都可用），调用方基本不用改。

    与旧行为的差异：
      · 浏览器**不再**在本函数返回时关闭——它是常驻的；收尾只关本线程的
        page 与连接（见 close_ctx）。
      · 首次调用决定 headless/offscreen，后续调用沿用（同一进程只能是一个
        形态），请求不一致时忽略并记一条 warn。

    offscreen=None（默认）= 跟随 OFFSCREEN_LAUNCH 旗标（桌面模式为 True）。
    """
    from playwright.sync_api import sync_playwright  # noqa

    if offscreen is None:
        offscreen = OFFSCREEN_LAUNCH
    hl = cfg.get('headless', False) if headless is None else headless

    if auto_download and _chrome_candidates(cfg) is None:
        try:
            download_chrome()
        except Exception as e:
            log('自动下载失败（将尝试使用系统已装的 Chrome）：%s' % e, 'warn')

    ensure_browser(cfg, hl, offscreen)
    cur_hl, cur_off = _BROWSER['hl'], _BROWSER['offscreen']
    if (cur_hl, cur_off) != (hl, offscreen):
        # 常驻浏览器同一时刻只能是一种形态。请求变了就重启成请求的形态——
        # profile 不变、账号不掉，只是换一只进程。绝不「硬用错形态」：那会把
        # 需要看见的登录窗口变成无头（用户没法扫码/输验证码）。
        log('浏览器形态切换为%s，重启常驻浏览器 …'
            % ('无头' if hl else ('屏幕外有窗口' if offscreen else '有窗口')),
            'warn')
        # v6.30 补：停 + 起必须**整体**持锁。否则两个线程同时切形态时，
        # A 的 stop_browser() 之后、ensure_browser() 之前，B 会看到「没进程」
        # 又起一只；随后 A 也起——第二只撞上自己人攥着的 Lockfile，三个候选
        # 各白等 25 秒（真机探针实测 73 秒），最后被迫换罐。
        with _BROWSER_LOCK:
            stop_browser()
            ensure_browser(cfg, hl, offscreen)

    if getattr(_TLS, 'ctx', None) is not None:
        # 进程可能被别的线程换号时停掉重启过（pid 变了）——旧 _TLS.ctx 会指向
        # 一个已死进程的连接，必须丢弃重连。pid 相同才复用。
        cur_pid = _BROWSER['proc'].pid if _BROWSER['proc'] else None
        if getattr(_TLS.ctx, '_wb_pid', None) == cur_pid:
            return _TLS.ctx
        # 丢弃旧连接时**必须收掉本线程的 playwright 实例**（_drop_tls 里的
        # pw.stop()）——否则它的事件循环以挂起态留在本线程，紧接着的
        # sync_playwright().start() 会报「Playwright Sync API inside the
        # asyncio loop」。登录换罐（stop_browser → fresh_profile → launch）
        # 必走这里，就是真机「查不到东西」的那条错。
        _drop_tls()
    if _BROWSER['port'] is None:
        raise RuntimeError('常驻浏览器未能启动')

    pw = sync_playwright().start()
    last = None
    for attempt in range(2):
        endpoint = 'http://127.0.0.1:%d' % _BROWSER['port']
        try:
            br = pw.chromium.connect_over_cdp(endpoint)
            ctx = br.contexts[0] if br.contexts else br.new_context()
            ctx.set_default_timeout(30000)
            # 反检测补丁只装**一次**：init script 是**上下文级**的，每个线程都
            # 装会把同一段补丁堆 N 份（无害但脏）。用 ctx id 去重（CDP 连到同一个
            # 浏览器时 id 一致——本机实测两个连接拿到同样的 id；万一对不上最坏
            # 只是多装几次）。
            if _LAUNCH_CTX.get('ctx') is not ctx:
                try:
                    ctx.add_init_script(_STEALTH_JS)
                except Exception:
                    pass
                _LAUNCH_CTX['ctx'] = ctx
            ctx._wb_pw = pw        # 本线程的 playwright 实例（close_ctx 收尾用）
            ctx._wb_br = br        # 本线程的 CDP 连接（close_ctx 只断连接，不关进程）
            ctx._wb_pid = _BROWSER['proc'].pid if _BROWSER['proc'] else None
            # 每线程记录「自己经这个共享 ctx 开的页面」。close_ctx 只能关这些，
            # **绝不能**关共享上下文里的全部页面——否则作业窗口线程一收尾就把
            # 并发任务的页面一起关了。new_page 只包一次（挂 ctx._wb_new_page 记住
            # 原方法），包装体在**调用时**读 _TLS 与当前浏览器形态，所以不同线程
            # 各记各的、形态切换也立刻生效（不能把形态缓存进闭包）。
            _TLS.pages = set()
            if not hasattr(ctx, '_wb_new_page'):
                ctx._wb_new_page = ctx.new_page

                def _recorded_new_page(*a, **k):
                    p = ctx._wb_new_page(*a, **k)
                    try:
                        _TLS.pages.add(p)
                    except Exception:
                        pass
                    if not _BROWSER.get('offscreen'):
                        # 网页模式（非桌面）：给这个新页面钉一个随机常见分辨率做
                        # 画布指纹随机化——固定 1440x900 会让同一工具的所有账号
                        # 在平台侧呈同一画布尺寸，方便被划成同一团伙。桌面模式不
                        # 钉（窗口被 SetParent 嵌进「预览」面板，画布必须跟随面板
                        # 回流，否则右边/下边被裁）。
                        try:
                            _w, _h = random.choice(_VIEWPORTS)
                            p.set_viewport_size({'width': _w, 'height': _h})
                        except Exception:
                            pass
                    return p

                try:
                    ctx.new_page = _recorded_new_page
                except Exception:
                    pass
            _TLS.pw, _TLS.br, _TLS.ctx = pw, br, ctx
            return ctx
        except Exception as e:
            last = e
            # 端口可能因进程重启而变；重确保一次再试
            ensure_browser(cfg, hl, offscreen)
            time.sleep(0.5)
    try:
        pw.stop()
    except Exception:
        pass
    raise RuntimeError('连接常驻浏览器失败：%s' % last)


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


def _quiet_win(page, cfg, headless):
    """非登录阶段把浏览器窗口收进任务栏（v4.0）。

    登录/验证码阶段必须可见，所以一律在**登录确认之后**调。刷课那条路径
    早就有同样的动作（brush_minimize），这里把查询、答题、交卷也一并收拢
    —— 否则桌面上一个原生应用窗口 + 一个全屏浏览器，屏幕照样被占着，
    桌面上线也就失去意义了。
    无头模式没有窗口，直接返回（不去白拿一条「最小化失败」的 warn 日志）。
    """
    if OFFSCREEN_LAUNCH:
        # 桌面模式：可见性归守夜入坞线程管。窗口要么已入坞（预览面板
        # 开着就显示），要么屏外待坞——最小化会把已入坞的预览藏掉
        # （ShowWindow SW_SHOWNA 救不回最小化的子窗口）。
        return False
    if headless is True or cfg.get('headless'):
        return False
    if not cfg.get('brush_minimize', True):
        return False
    return minimize_window(page)


def close_ctx(ctx):
    """收尾：关掉本线程经它开的**页面**，再断开本线程的连接。**不关常驻进程**。

    v4.3 起浏览器是全局常驻的（见 launch），ctx 是**共享上下文**。旧语义
    「close_ctx 收掉整只浏览器」现在变成「只关本线程自己开的页面」——三点必须
    同时成立，否则会串线程：
      · 只关 _TLS.pages（本线程经包装的 new_page 记下的），**不碰** ctx.pages
        ——共享 ctx 的 pages 含别的线程的页面，全关会把并发任务的页面带走；
      · 只断本线程这条 CDP 连接（ctx._wb_br），不关进程；
      · 实测（tmp/_probe_session_close.py）断开连接**不会**关页面，所以必须
        显式 page.close() 逐个收，否则任务越跑标签页越多。
    """
    pages = list(getattr(_TLS, 'pages', None) or [])
    for pg in pages:
        try:
            pg.close()
        except Exception:
            pass
    try:
        br = getattr(ctx, '_wb_br', None)
        if br is not None:
            br.close()                       # 只断本线程这条 CDP 连接
    except Exception:
        pass
    # 清掉本线程缓存并**收掉本线程的 playwright 实例**（_drop_tls 里的
    # pw.stop()），下一次 launch 才能干净地 sync_playwright().start()。
    _drop_tls()


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


def logout(cfg=None) -> bool:
    """显式退出登录（清会话），供界面「退出登录（换号）」按钮调用。

    v6.15：登录态默认常驻（见 do_login_password），所以必须给一个「我真的要
    换号」的出口。做三件事：
      · 归档整份浏览器 profile（fresh_profile）——把持久化在磁盘上的 cookie
        / localStorage 全搬走，而不是只擦内存里的 cookie（那会被下次
        storage_state 又灌回来）；
      · 删掉 state.json（cookie 快照）与 identity.json（账号记录）；
      · 停掉常驻浏览器进程，保证下次查询从**干净的新罐**起步、真的重登。
    常驻浏览器开着时 profile 被占用，fresh_profile 会失败 → 先 stop_browser。
    返回 True 表示会话已清干净（可换号登录了）。
    """
    stop_browser()
    try:
        ok = fresh_profile()
    except Exception as e:
        log('退出登录：清理浏览器痕迹失败（%s）' % e, 'warn')
        ok = False
    drop_identity()
    try:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
    except Exception:
        pass
    if ok:
        log('已退出登录（本机登录会话已清除，下次查询需重新登录）')
    else:
        log('已清除账号记录；但浏览器痕迹归档未完成，下次可能仍带着旧会话，'
            '建议关掉本工具重开再登录。', 'warn')
    return bool(ok)


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


def _rmtree_retry(path, tries=6, delay=0.4):
    """删除目录，Windows 上「句柄滞后释放」时重试几次；成功/已不存在返回 True。

    为什么需要：chrome 刚被 stop_browser() 杀掉，它占着 profile 里的
    Lockfile / Network / Cache 等句柄，内核还要几十~几百毫秒才真正释放。
    此时 rmtree(..., ignore_errors=True) 会**静默失败**，紧接着的 rename
    就撞 WinError 183（目标已存在）——真机换号回滚正是这么炸的
    （restore_profile 把 profile_old 改回 profile 时）。所以这里不吞错，
    而是重试到成功或超时，让调用方拿到真实结果再决定。
    """
    p = Path(path)
    for i in range(tries):
        if not p.exists():
            return True
        try:
            shutil.rmtree(p)
            return True
        except Exception:
            time.sleep(delay * (i + 1))
    return not p.exists()


def _rename_retry(src, dst, tries=8, delay=0.35):
    """目录改名，被占用（WinError 5/32/183）时重试；成功返回 True。

    与 _rmtree_retry 同理：chrome 进程树刚被杀，profile 目录句柄滞后释放，
    此刻 rename 会抛 WinError 5（拒绝访问）。等几百毫秒重试即可成功。
    """
    s, d = Path(src), Path(dst)
    for i in range(tries):
        try:
            s.rename(d)
            return True
        except Exception as e:
            if i == tries - 1:
                last = e
            time.sleep(delay * (i + 1))
    try:
        log('目录改名始终失败（%s → %s）：%s' % (s.name, d.name, str(last)[:80]),
            'warn')
    except Exception:
        pass
    return False


def fresh_profile():
    """换号登录前清场：归档浏览器 profile 与登录会话，新账号从零开始。

    为什么：多个账号轮流共用同一个浏览器 profile（同一个 cookie 罐）时，
    平台侧看到的是「同一罐 cookie 里先后出现多个不同 uid」，账号之间
    会被关联分析划成同一团伙。每次换号换新罐，账号间在 cookie 层面
    不再共享任何痕迹。

    做法：profile 目录整体改名归档（同盘 rename，瞬时完成），state.json
    一起挪进去。只保留最近一份归档；改名失败（多半有浏览器进程占用）
    就什么都不动并返回 False，由调用方提示。

    v4.3：常驻浏览器独占 profile 目录，改名之前必须先停掉它（否则 Windows
    下目录被占用、rename 直接失败）。停掉后由调用方的下一次 launch 用全新的
    profile 重启。所有调用方（cs / webui）都走这里，不必各自 stop。
    """
    stop_browser()
    # v6.28：归档**活动目录**，不是写死的 PROFILE_DIR——本次运行若换过罐，
    # 真正的 cookie 罐在 profile_next 里；归档错了旧账号的痕迹会留在原地，
    # 下一步新账号接着用同一罐 cookie（换号共用痕迹，正是这里要防的事）。
    cur = _active_profile()
    old = PROFILE_DIR.with_name('profile_old')
    _rmtree_retry(old)          # 清掉上一份归档（句柄滞后时重试，不静默留残）
    if not cur.exists():
        # 本来就没有 profile（首次使用），只需清掉旧会话文件
        try:
            if STATE_FILE.exists():
                STATE_FILE.unlink()
        except Exception:
            pass
        drop_identity()
        return True
    if not _rename_retry(cur, old):
        log('归档旧浏览器痕迹失败（文件夹被占用，可能有浏览器窗口还开着）。',
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
    _reset_active_profile()
    log('已归档旧账号的浏览器痕迹（runtime/profile_old），本次登录使用全新会话。')
    return True


def restore_profile():
    """fresh_profile() 的逆操作：把归档的浏览器痕迹和会话挪回原位。

    用在新登录没成功的时候。否则用户只是点一下登录、或验证码没点完，
    原来的登录态就被归档走了，下次查询还得重登一遍（反而更容易反复
    撞验证码）——这属于「换个号把老号也搭进去」，必须能回滚。

    调用前要先关掉浏览器（profile 目录被占用时 rename 会失败）。v4.3 起常驻
    浏览器独占 profile 目录，这里显式停掉它；下一次 launch 会用挪回来的
    旧 profile 重启。
    """
    stop_browser()
    tgt = _active_profile()     # v6.28：回滚到本次运行真正在用的目录
    old = PROFILE_DIR.with_name('profile_old')
    if not old.exists():
        return False
    # 目标必须**确实删干净**再改名：chrome 进程树刚被杀，句柄滞后释放时
    # ignore_errors 的 rmtree 会静默留下半个 profile，随后 rename 撞
    # WinError 183。删不掉就如实返回 False，绝不带病改名。
    if not _rmtree_retry(tgt):
        log('回滚旧浏览器痕迹失败：runtime/profile 目录被占用（删不掉）。',
            'warn')
        log('旧痕迹还在 runtime/profile_old，可手动改名回 runtime/profile。',
            'warn')
        return False
    if not _rename_retry(old, tgt):
        log('旧痕迹还在 runtime/profile_old，可手动改名回 runtime/profile。',
            'warn')
        return False
    try:
        if (tgt / 'state.json').exists():
            shutil.move(str(tgt / 'state.json'), str(STATE_FILE))
    except Exception:
        pass
    try:
        if (tgt / 'identity.json').exists():
            shutil.move(str(tgt / 'identity.json'), str(IDENTITY_FILE))
    except Exception:
        pass
    _reset_active_profile()
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
    # 硬上限兜底：任何上游把 timeout 算成天文数字（config 手改坏、
    # 类型没归一）也不能真的挂死 —— 10 分钟到了按超时返回。
    timeout_ms = min(max(0, timeout_ms), 600_000)
    deadline = time.time() + timeout_ms / 1000.0
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
        err = None
        for _ in range(2):            # 偶发被页面自身跳转顶掉（ERR_ABORTED），重试一次
            try:
                page.goto(BASE_URL, wait_until='domcontentloaded')
                err = None
                break
            except Exception as e:
                err = e
                time.sleep(0.8)
        if err is not None:
            # goto 抛 net::ERR_ABORTED 时窗口多半已被页面自己的 JS 跳走——
            # 别急着判「没有登录态」，继续按实际落点（下方 evaluate）判断。
            log('打开个人空间失败：%s（按实际落点继续判断）' % err, 'warn')
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
    try:
        page = ctx.new_page()
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
        log('等待登录中（最多 8 分钟，期间请勿关闭窗口；不想扫了随时点「停止」）…')

        deadline = time.time() + 480
        n = 0
        while time.time() < deadline:
            try:
                page.wait_for_timeout(2000)
            except Exception:
                break        # 浏览器窗口被用户点掉了：不可能再登录成功
            n += 1
            # 每个 2 秒边界把控制权交出去一瞬：登录等待最长 8 分钟，
            # 用户不想扫了、点了「停止」（或暂停后再停止）必须能立刻收尾，
            # 不能傻等到超时——真机踩过「点了停止还在一直转」（2026-10-14）。
            if not checkpoint('login'):
                log('已停止等待登录（未登录成功）。', 'warn')
                return False
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
    try:
        page = ctx.new_page()
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
#   'stopped'    等待期间用户点了「停止」。**绝不能**报成密码错误——
#                用户是主动不登了，不是账号密码有问题（v6.10 加了等待期
#                可停止之后才可能出现这条路，见 do_login_password 等待循环）
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

    ctx = launch(cfg, headless=headless, offscreen=None)
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
        # v6.15「登录态常驻」：只要本机会话还能用就直接复用，**不再**因为
        # 用户填了别的账号（或身份未知）就换罐重登——用户 2026-10-15 明确
        # 「一次登录后一直保持，以效率为准，给个手动退出按钮就行」。换号收敛
        # 成显式动作：点侧栏「退出登录（换号）」清掉会话后，下次查询才会真重登。
        # （v3.7~v6.14 这里是「填的账号 ≠ 本机会话账号 → 换罐重登」，防的是
        #  A 的会话下跑 B 的任务；现在按用户要求以效率优先，改为一律复用，
        #  仅用一句日志说清「当前复用的是哪个账号」，看到不对自己去退出。）
        if restore_state(ctx) and check_login(page, navigate=True):
            have = (load_identity().get('phone') or '')
            if have and have != phone:
                log('本机会话属于账号（%s），与你输入的 %s 不同；按「登录态常驻」'
                    '沿用本机会话（要换号请先点侧栏「退出登录（换号）」）。'
                    % (mask_phone(have), mask_phone(phone)), 'warn')
            elif not have:
                log('本机有可用登录会话（来源未记名，多为扫码登录留下的），'
                    '直接复用 ✓')
            else:
                log('本机已保存该账号（%s）的登录会话，直接复用 ✓'
                    % mask_phone(phone))
            return True

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
        # 常驻浏览器独占 profile：换号必须先把进程停掉，否则 profile 目录
        # 被占用、fresh_profile() 改名会失败。停掉后下面的 launch 会用全新
        # 的 profile 重启一个新的常驻进程。
        stop_browser()
        if not fresh_profile():
            log('旧浏览器痕迹归档失败（可能有浏览器窗口还开着）。', 'err')
            log('请关闭本工具后重新打开，再重新登录一次。', 'err')
            _login_fail = 'other'
            return False
        swapped = True
        ctx = launch(cfg, headless=headless, offscreen=None)
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
            try:
                page.wait_for_timeout(800)
            except Exception:
                break        # 浏览器窗口被用户点掉了：不可能再登录成功
            # 同 do_login：把手填验证码/换号的等待也接上控制信号，
            # 用户点了「停止」就别再傻等（最长仍会等到 100/300 秒上限）。
            if not checkpoint('login_password'):
                log('已停止等待登录（未登录成功）。', 'warn')
                _login_fail = 'stopped'
                return False
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
        # 无论成败都要收尾：收掉本次登录开的页面（close_ctx）。keep_open 开关
        # 早已删除——以前出现过点选验证码就把它置 True 想「把窗口留给用户」，
        # 但函数没返回时窗口本来就开着，等人点那几分钟完全够用，那个变量只
        # 起副作用（漏一个进程、占住 profile）。
        # v4.3：浏览器是全局常驻的，close_ctx 不再关整只浏览器——否则登录一
        # 结束就把常驻浏览器关了，紧接着的「一键查询」还得冷启动一次。旧注释
        # 里「必须关掉持久化上下文否则 profile 被占」的前提已被常驻单进程架构
        # 取代：profile 由那个唯一进程独占，不会再有「两个持久化上下文抢目录」。
        close_ctx(ctx)
        # 换了新罐又没登成 → 把旧账号的痕迹与会话挪回原位，
        # 别让一次没成的登录把原来的登录态也搭进去（restore_profile 内部
        # 会先停掉常驻浏览器再改名）。
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
  const res = { total: null, pending: [], all: [], nodes: 0 };
  const m = (document.body.innerText || '')
        .match(/已完成任务点[:：]?\s*(\d+)\s*\/\s*(\d+)/);
  if (m) res.total = [parseInt(m[1], 10), parseInt(m[2], 10)];

  document.querySelectorAll('.chapter_item').forEach(item => {
    const ct = item.querySelector('.clicktitle');
    let name = ct ? (ct.getAttribute('title') || ct.textContent) : '';
    name = (name || '').replace(/\s+/g, ' ').trim();
    if (!name) return;
    res.nodes++;
    // v6.24：扫描期「逐节点查成绩」要一份**全部**节点清单（不只是欠账的）。
    // 标题带「测试/测验/练习/作业/考试」的才可能是作业卡；视频/文档节点
    // 打开也是白跑，这里先按标题筛掉，省大量无谓导航。
    const _kid = (item.id || '').replace(/^cur/, '');
    if (_kid && /测试|测验|练习|作业|考试|习题/.test(name)) {
      res.all.push({ name: name, kid: _kid,
                     pending: !!item.querySelector('.catalog_jindu') });
    }
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
            'nodes': data.get('nodes', 0),
            # v6.24：全部「像作业卡」的节点（含已完成），扫描期逐节点查成绩用
            'all': data.get('all') or []}


# ================================================================ 抓「任务」页（课堂活动）
# 背景：老师发的「随堂练习 / 测验 / 签到」这些课堂活动，并不在作业模块、
# 也不在章节任务点里，只在课程首页的「任务」页（li[dataname=hd]）。以前
# 只查作业 / 考试 / 章节，所以这类活动一直漏报——就是「有的任务点查不到」的根因。
#
# 实测（2026-10-13，真号）：任务页 iframe 是 mobilelearn 的 Vue 页，数据来自
# 一个纯 JSON 接口（ACTIVELIST_URL）。直接打接口比分页渲染 DOM 快得多也稳得多，
# 且不需要课程的 enc / stuenc（那两个是点击时动态生成、没法拼）。
#   activeType：2=签到 5=问卷 14=讨论 42=随堂练习/测验 44=资料 45=通知 77=AI实践
#   status    ：0=未开始 1=进行中 2=已结束（与任务页「进行中/已结束」分桶一致）
ACT_STATE = {0: '未开始', 1: '进行中', 2: '已结束'}


def _act_kind(active_type) -> str:
    return ACTIVITY_TYPES.get(active_type, '活动(%s)' % active_type)


def get_fid(ctx) -> str:
    """机构号 fid：从登录 cookie 取，取不到用默认 2339。"""
    try:
        for ck in ctx.cookies():
            if ck.get('name') == 'fid' and ck.get('value'):
                return str(ck['value'])
    except Exception:
        pass
    return '2339'


def fetch_activities(page, c, cpi, cfg, fid='2339') -> dict:
    """抓一门课的课堂活动（任务页）。

    返回 {'activities': [...], 'ongoing': [...], 'counts': {...}}；
    page_failed 表示接口没打通（≠ 该课没有活动），调用方据此区分「没查到」
    和「确实没有」，别把失败静默报成「没有活动」。
    """
    ts = int(time.time() * 1000)
    url = ACTIVELIST_URL.format(fid=fid, cid=c['cid'],
                                clsid=c.get('clsid', ''), ts=ts)
    try:
        resp = page.request.get(url, timeout=20000,
                                headers={'Accept': 'application/json'})
        data = resp.json()
    except Exception as e:
        return {'activities': [], 'ongoing': [], 'counts': {},
                'page_failed': True, 'reason': str(e)[:120]}
    if not isinstance(data, dict) or data.get('result') not in (1, '1', True):
        # result!=1：可能是风控 / 掉登录 / 参数变了——按「没查到」处理并带上原因
        msg = ''
        if isinstance(data, dict):
            msg = str(data.get('msg') or data.get('errorMsg') or '')
        return {'activities': [], 'ongoing': [], 'counts': {},
                'page_failed': True,
                'reason': ('接口返回异常：%s' % msg[:80]) if msg else '接口返回异常'}
    raw = (data.get('data') or {}).get('activeList') or []
    acts, ongoing = [], []
    counts = {'进行中': 0, '未开始': 0, '已结束': 0}
    for it in raw:
        atype = it.get('activeType')
        status = it.get('status')
        state = ACT_STATE.get(status, '未知')
        rec = {
            'id': it.get('id'),
            'type': atype,
            'kind': _act_kind(atype),
            'status': status,
            'state': state,
            'name': (it.get('nameOne') or '').strip(),
            'time': (it.get('nameFour') or '').strip(),
            'user_status': it.get('userStatus'),
            'raw': it,
        }
        acts.append(rec)
        if state in counts:
            counts[state] += 1
        if status == 1:                       # 进行中 = 才是真正要提醒的
            ongoing.append(rec)
    return {'activities': acts, 'ongoing': ongoing, 'counts': counts,
            'page_failed': False}


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


_LAST_FETCH_TS = [0.0]     # 上次单门刷新的取数时刻（节流用）


def _throttle_course_fetch(cfg, risky=False) -> bool:
    """单门刷新取数前的节流与边界检查。

    整轮扫描两门课之间有 course_delay（见 scan 的逐门循环）；单门刷新若「一点
    就发、连着点清单几秒内十几次整页导航」，等于自己撞平台风控（scan 里的
    45/90 秒冷却就是为这个设的）。这里补上同等间隔：距上次取数不足 base 秒就
    补足；偶尔点一下（间隔早已超过 base）则零等待，仍是秒级。顺带在边界把控制
    权交出去一瞬。返回 False = 用户要求中止。
    """
    if not checkpoint('refresh_course'):
        return False
    base = _course_wait(cfg.get('course_delay'), risky)
    gap = time.time() - _LAST_FETCH_TS[0]
    if base > gap:
        time.sleep(base - gap)
    _LAST_FETCH_TS[0] = time.time()
    return True



def _fetch_course_row(ctx, page, c, cpi, cfg, fid, deep) -> dict:
    """抓一门课的四类数据（作业 / 考试 / 课堂活动 / 任务点明细），返回 row。

    scan 的逐门循环与「单门刷新」(refresh_course) 共用这一段——保证单门刷新
    和整轮扫描对同一门课产出的字段完全一致（不会因为走了近路而少字段）。
    """
    row = {'name': c['name'], 'cid': c['cid'], 'clsid': c['clsid'],
           'cpi': cpi, 'prog_text': c.get('prog', '')}
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

    # 「任务」页（课堂活动）：随堂练习 / 测验 / 签到这些以前查不到的就在这儿。
    # 一次 JSON 接口，很轻，不做条件跳过——漏一门课的活动比多花几百毫秒严重得多。
    try:
        row['act'] = fetch_activities(page, c, cpi, cfg, fid=fid)
    except Exception as e:
        row['act'] = {'activities': [], 'ongoing': [], 'counts': {},
                      'page_failed': True,
                      'reason': str(e).splitlines()[0][:120]}

    # 任务点章节下钻：只对存在缺口的课程做，避免拖慢扫描
    pm_prog = re.search(r'(\d+)\s*/\s*(\d+)', c.get('prog') or '')
    if deep and pm_prog and int(pm_prog.group(1)) < int(pm_prog.group(2)):
        try:
            row['prog_detail'] = fetch_progress_detail(page, c, cpi, cfg)
        except Exception as e:
            row['prog_detail'] = {
                'no_module': False, 'pending': [], 'total': None,
                'error': str(e).splitlines()[0][:120]}
    return row


def refresh_course(cfg, cid, clsid, name='', prog='', deep=True) -> dict:
    """只重抓一门课，供界面「单门刷新」用。

    与 scan 的区别：**不重读课程列表**（省掉 collect_courses 的滚动读取）、
    不碰其它课程。登录态走常驻会话（check_login/restore_state），所以几乎不花
    时间。返回 {'row': <per-course 结果 dict>}。比整轮 scan 快一个数量级。
    """
    t0 = time.time()
    ctx = launch(cfg)
    try:
        page = ctx.new_page()
        banner('重抓 1 门课')
        if not check_login(page):
            if restore_state(ctx) and check_login(page):
                log('已用本地会话自动登录 ✓')
            else:
                log('本地没有可用登录态，请先登录。', 'err')
                raise NotLoggedIn('未登录')
        _quiet_win(page, cfg, None)
        # 单门刷新的节流：距上次取数不足一个课间间隔就补足（连点清单
        # = 几秒内十几次整页导航，等于自己撞平台风控）；偶尔点一下零等待。
        if not _throttle_course_fetch(cfg):
            raise RuntimeError('已中止单门刷新')
        c = {'cid': cid, 'clsid': clsid,
             'name': name or ('课程%s' % cid), 'prog': prog or ''}
        cpi = cfg.get('cpi') or ''
        fid = get_fid(ctx)
        row = _fetch_course_row(ctx, page, c, cpi, cfg, fid, deep)
    finally:
        save_state(ctx)
        close_ctx(ctx)
    log('已重抓「%s」，用时 %.1f 秒' % (cut(name or cid, 24), time.time() - t0))
    return {'row': row}


def _failed_courses_from_results(results) -> list:
    """从逐课结果提炼「页面没加载成功」的课程清单（报告里要显式警告）。

    整轮 scan 与单门刷新 rebuild 共用这一段，保证两条路对同一份 results 算出
    同一份 page_failed——否则单门刷新撞风控时，报告仍用旧清单，那门课的未完成
    项会静默消失（scan 里这类失败一定会挂红横幅，refresh 不能把这道保护丢了）。
    """
    return [
        {'course': r['name'],
         'what': '、'.join(w for w, d in (('作业', r['hw']), ('考试', r['exam']),
                                          ('章节明细', r.get('prog_detail') or {}),
                                          ('任务页', r.get('act') or {}))
                           if d.get('page_failed')),
         'reason': ((r['hw'].get('page_failed') and r['hw'].get('reason'))
                    or (r['exam'].get('page_failed') and r['exam'].get('reason'))
                    or ((r.get('prog_detail') or {}).get('page_failed')
                        and (r.get('prog_detail') or {}).get('reason'))
                    or ((r.get('act') or {}).get('page_failed')
                        and (r.get('act') or {}).get('reason'))
                    or '页面未加载成功')}
        for r in results
        if r['hw'].get('page_failed') or r['exam'].get('page_failed')
        or (r.get('prog_detail') or {}).get('page_failed')
        or (r.get('act') or {}).get('page_failed')]


def rebuild_with_row(internal, cid, clsid, new_row):
    """把一门课的新 row 并回上次扫描的 (courses, results)，重建报告。

    internal = scan() 返回的 _internal 字典（课程表 + 逐课结果 + 范围参数）。
    按 (cid, clsid) 定位；找不到返回 None（调用方据此提示「请先完整查询一次」）。
    就地更新 internal 里的 courses/results，供连续刷新多门时累积。
    """
    if not internal or new_row is None:
        return None
    courses = list(internal.get('courses') or [])
    results = list(internal.get('results') or [])
    key = (str(cid), str(clsid))
    hit = None
    for i, c in enumerate(courses):
        if (str(c.get('cid')), str(c.get('clsid'))) == key:
            hit = i
            break
    if hit is None or hit >= len(results):
        return None
    results[hit] = new_row
    # 任务点进度随这次重抓更新：让「未完成任务点」的依据与最新状态一致
    if new_row.get('prog_text'):
        courses[hit]['prog'] = new_row['prog_text']
    report = build_report(courses, results,
                          stopped_at=internal.get('stopped_at'),
                          scope_total=internal.get('scope_total'),
                          scope_kind=internal.get('scope_kind') or 'selected',
                          failed_courses=_failed_courses_from_results(results),
                          fid=internal.get('fid', '2339'))
    internal['courses'] = courses
    internal['results'] = results
    return report


def scan(cfg, limit=0, headless=None, open_report=None, deep=True, only=None,
         grades=False, progress=None, control=None) -> dict:
    """扫描并出报告。

    only: 课程 key 列表（course_key() 的返回值）。给了就只扫这些课；
          留空/None 表示扫全部。优先于 limit。
    limit: 只扫最近 N 门。课程列表的顺序就是学习通个人空间的默认顺序
          （最近学习过的在最前），所以「前 N 门」即「最近学过的 N 门」。
    grades: True = 顺带逐节点读测验/作业成绩（v6.24）。作业列表只给
          「已完成/未完成」，分数只在打开作业卡后的批阅页上，所以这一步要
          逐个节点打开——每门课会多花 1~4 分钟。只读，不点重做、不交卷。
    """
    limit = limit or cfg.get('max_courses') or 0
    open_report = cfg.get('open_report', True) if open_report is None else open_report
    t0 = time.time()
    total_all = 0          # 账号内课程总数，用于区分「勾选了一部分」
    scope_kind = ''        # ''=全量 ｜ selected=按勾选 ｜ recent=只查最近 N 门

    ctx = launch(cfg, headless=headless)
    try:
        page = ctx.new_page()
        banner('检查登录状态')
        if not check_login(page):
            if restore_state(ctx) and check_login(page):
                log('已用本地会话自动登录 ✓')
            else:
                log('本地没有可用登录态，请先执行：'
                    'python chaoxing_scanner.py login', 'err')
                raise NotLoggedIn('未登录')
        _quiet_win(page, cfg, headless)

        courses, cpi = collect_courses(page, cfg)
        fid = get_fid(ctx)
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
            # 单门抓取逻辑抽到 _fetch_course_row：与刷新单门共用，保证字段一致
            row = _fetch_course_row(ctx, page, c, cpi, cfg, fid, deep)
            hw, ex = row['hw'], row['exam']
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
            # v6.24：顺带查成绩（默认关）。必须放在这一门课的其它抓取之后，
            # 因为它会在这个 page 上来回导航作业卡；失败不影响主扫描。
            if grades:
                try:
                    row['scores'] = scan_scores(cfg, page, c, cpi,
                                                progress or log,
                                                control=control)
                except Exception as e:
                    row['scores'] = {'items': [], 'failed': 1,
                                     'reason': str(e).splitlines()[0][:120]}
            act = row.get('act') or {}
            n_act = len(act.get('ongoing') or [])
            act_txt = ''
            if act.get('page_failed'):
                act_txt = ' · ⚠任务页未加载'
            elif n_act:
                act_txt = ' · 进行中活动 %d' % n_act
            log('[%2d/%2d] %-34s %s · %s%s%s' % (i, len(courses), cut(c['name'], 34),
                                                 hw_txt, ex_txt, extra, act_txt))
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

        failed_courses = _failed_courses_from_results(results)
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
                          failed_courses=failed_courses, fid=fid)
    paths = write_outputs(report)
    if open_report and not os.environ.get('CS_NO_GUI'):
        # CS_NO_GUI（跑批）下不弹报告：每跑一次测试都会"啪"地打开一个 Markdown。
        try:
            os.startfile(str(paths['md']))  # type: ignore[attr-defined]
        except Exception:
            pass
    # 供界面「单门刷新」(refresh_course/rebuild_with_row) 用：缓存本次的课程表、
    # 逐课结果与范围参数，让重抓一门课后能把结果并回这份清单重建报告，而不整轮重查。
    paths['_internal'] = {
        'courses': courses, 'results': results,
        'stopped_at': len(results) if stopped else None,
        'scope_total': total_all if scope_kind else None,
        'scope_kind': scope_kind or 'selected',
        'failed_courses': failed_courses, 'fid': fid,
    }
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
    try:
        page = ctx.new_page()
        banner('读取课程列表')
        if not check_login(page):
            if restore_state(ctx) and check_login(page):
                log('已用本地会话自动登录 ✓')
            else:
                log('本地没有可用登录态，请先登录。', 'err')
                raise NotLoggedIn('未登录')
        _quiet_win(page, cfg, headless)

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
# ⚠️ num 是选项卡序号，**不是**「第几个任务点」。真机实测（2026-10-17，
# 账号 190…6550 的大学物理）：
#   章节页顶部有一排选项卡（学生页文案是「1视频与讲义 2讨论 3测试」），
#   每个选项卡对应一个 num：视频与讲义=0、讨论=1、测试=2。**测验/作业卡
#   常常不在 num=0**——1.1 的章节测验在 num=2、1.2 的在 num=1，1.7 的恰好在
#   num=0。以前这里把 num 写死成 0，于是只在第一个选项卡里找作业卡，
#   绝大多数「章节里的测验」永远找不到（用户报的「检测不到作业测验」根因）。
#   选项卡数量逐节点不同，要 0,1,2,… 扫到连续空为止（见 _wait_hw_iframe）。
CARDS_URL = ('https://mooc1.chaoxing.com/mooc-ans/knowledge/cards'
             '?clazzid={clsid}&courseid={cid}&knowledgeid={kid}&num={num}&ut=s&cpi={cpi}')

# 扫选项卡的上限。学生页实测最多 3 张（视频与讲义/讨论/测试），留点余量；
# 老师端偶见更多。超过这个数还连续空，就当到头了，免得无限翻。
MAX_CARDS_TABS = 6

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

# 这个视频 frame「还需不需要刷」。false = 已刷过（window.__cx_brushed）
# 或**这张卡平台已判完成**（aria-label「任务点已完成」/ class 含
# ans-job-finished）→ 跳过，重跑不再重复播（v6.19 修死循环的关键）。
# 视频 frame 与主文档同源，window.frameElement 能读到父文档里这张卡的标记。
JS_VIDEO_CLAIM = r"""
() => {
  if (window.__cx_brushed) return false;
  const v = document.querySelector('video');
  if (!v) return false;
  try {
    const fe = window.frameElement;
    const ct = fe && fe.closest ? fe.closest(".ans-attach-ct") : null;
    if (ct) {
      const cls = (ct.className || "").toString();
      const job = ct.querySelector(".ans-job-icon");
      const lbl = job ? (job.getAttribute("aria-label") || "") : "";
      if (/已完成/.test(lbl) || /ans-job-finished/.test(cls)) return false;
    }
  } catch (e) { /* 跨源/取不到就当未完成，保持旧行为 */ }
  return true;
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
#   · LLM 没给出答案（返回 ? / 解析失败）的题 → 已答上的照样填、照样
#     每几道暂存一次，但**整份不交卷**（交上去就是白拿低分）；
#     剩下没答上的由你打开作业网址补完再提交，先前暂存的答案还在
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
    r'|手写|纸上书写|纸上作答|在纸上|书写后拍照|拍照.{0,8}(发送|回传)'
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
  // 上标/下标转普通字符：平台把 10 的 5 次方写成 <sup>5</sup>，textContent
  // 读出来是「6×105Pa」——模型会当成「一百零五」而不是「十的五次方」，
  // 真机实测就栽在这上面（作业 1.2 第 2 题，答案差了一个数量级）。
  // v6.22 起统一转成 ^5 / _2 这种可读写法再拼题干与选项文字。
  const _txt = (node) => {
    if (!node) return '';
    let c;
    try { c = node.cloneNode(true); } catch (e) { return node.textContent || ''; }
    c.querySelectorAll('sup').forEach(s => {
      const t = (s.textContent || '').trim();
      s.replaceWith(document.createTextNode(t.length > 1 ? '^(' + t + ')' : '^' + t));
    });
    c.querySelectorAll('sub').forEach(s => {
      const t = (s.textContent || '').trim();
      s.replaceWith(document.createTextNode(t.length > 1 ? '_(' + t + ')' : '_' + t));
    });
    return c.textContent || '';
  };
  _boxes.forEach((li, idx) => {
    const at = li.querySelector('input[name^=answertype]');
    // 题干提取三级兜底。真机实测踩过：某课作业卡的题干不在 .Zy_TItle 里
    // （结构差异），取到空题干 → 附件规则无从匹配 → 模型只看到「1.【简答】」
    // 就瞎编了一段「进入课程作业中的实验报告，点击上传文件……提交即可」当
    // 答案，工具照填还交了卷，整题 0 分。宁可题干带点噪声，也不能空着。
    // v6.25：题面图片的 URL 顺序。全图片题（题干和选项都是 PNG，DOM 文本
    // 只剩「1 【单选题】」）只能靠它认题——题干哈希对这类题是空的，拿不到
    // 可靠的缓存键。选项顺序实测三轮加载完全稳定（真机 2026-10-18 4.7），
    // 所以同一串图片 = 同一道题、同一套字母映射。
    let mediaSig = '';
    try {
      mediaSig = [...li.querySelectorAll('img')].slice(0, 12)
        .map(im => (im.getAttribute('src') || '').split('/').pop()
                    .split('?')[0])
        .filter(Boolean).join(',');
    } catch (e) { mediaSig = ''; }
    let stem = _txt(li.querySelector('.Zy_TItle'));
    if (!stem.trim()) {
      // .mark_name 是新版每题容器里的题目信息行（题号 + 题型 + 分值）
      stem = _txt(li.querySelector(
        '.mark_name, .Zy_Questions_Con, .TiMu_Title, .quesDetail, .questionCon, p'));
    }
    if (!stem.trim()) {
      // 兜底取整题文本，但要先剔掉富文本编辑器与工具条：它们的占位
      // 文字（「请输入…」）和按钮名会混进题干，把附件规则之类的匹配搅乱
      const clone = li.cloneNode(true);
      clone.querySelectorAll(
        '.edui-default, .eidtDiv, textarea, script, input').forEach(x => x.remove());
      stem = _txt(clone);
    }
    stem = stem.replace(/\s+/g, ' ').trim().slice(0, 500);
    const opts = [], fills = [];
    let li_html = null, li_doc = null;
    // v6.25：选项自己的图片指纹。纯图片选项（真机 4.7 四个选项各是一张
    // PNG、文字只有「A/B/C/D」）在视觉通道和缓存里都得靠它认——只有字母
    // 的话，同一串图换个字母映射就没法核对了。
    const _imgSig = (lab) => {
      const im = lab ? lab.querySelector('img') : null;
      return im ? ((im.getAttribute('src') || '').split('/').pop()
                   .split('?')[0]) : '';
    };
    li.querySelectorAll('input[name^=answer]').forEach(x => {
      if (x.type === 'hidden') return;
      const label = (x.closest('li') || x.parentElement);
      opts.push({name: x.name, val: x.value, type: x.type,
                 label: ((label && label.textContent) || x.value)
                        .replace(/\s+/g, ' ').trim().slice(0, 150),
                 img: _imgSig(label)});
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
        // ⚠ 走「宽松收任何 radio」只在**这一层确实只有一道题**时才允许。
        // 真机 2026-10-17（大学物理 1.3）踩过：第 1 题是填空、容器里没有
        // radio，向上找一层就把**第 2 题**的四个选项收进来了 → 模型按这四
        // 个选项作答、工具再拿第 2 题的 name 去点，题号和答案整体错位。
        // 判据：这一层里 answertype 的个数 —— 多于 1 个就说明跨题了，只认
        // 精确匹配的 qid。
        const nAt = scope.querySelectorAll('input[name^=answertype]').length;
        const allowAny = !qid || nAt <= 1;
        const list = exact.length ? exact
          : (allowAny
             ? [...scope.querySelectorAll('input[type=radio],input[type=checkbox]')]
             : []);
        list.forEach(x => {
          if (x.type === 'hidden') return;
          if (exact.length && qid && x.name !== 'answer' + qid) return;
          const label = (x.closest('li') || x.parentElement);
          opts.push({name: x.name, val: x.value, type: x.type,
                     label: ((label && label.textContent) || x.value)
                            .replace(/\s+/g, ' ').trim().slice(0, 150),
                     img: _imgSig(label)});
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
    // 选项的**显示字母**（真机 2026-10-17，大学物理 1.8，randomOptions=true）：
    //   平台把选项顺序打乱了再渲染，页面上从 A 起按**显示位置**标字母，
    //   而 radio 的 value 仍是打乱**前**的原始字母。实测 Q2：
    //     显示 A 的 value 是 C，显示 C 的 value 是 D……
    //   模型（看截图也好、读题面文字也好）答的一定是**显示字母**，按 value
    //   匹配就会把答案点到别的选项上——用户看到的就是「答案是对的，但填错
    //   位置」，7 道错题里有 6 道与这个错位完全吻合。
    // 所以每个选项都要记下「它显示在第几位、标的是哪个字母」，落答侧据此定位。
    opts.forEach((o, i) => {
      o.pos = i;
      const m = String(o.label || '').trim()
        .match(/^[（(]?\s*([A-Za-z])\s*[)）.．、:：\s]/);
      o.dl = (m ? m[1] : 'ABCDEFGHIJKLMNOP'.charAt(i)).toUpperCase();
    });
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
    // ⚠ 判附件题用的 HTML 必须**剔掉富文本编辑器子树**：UEditor 工具条里
    //   明晃晃写着「上传图片」「上传视频」，拿它去匹配 ATTACH_RE 会把普通
    //   填空题整份判成「要交附件、整份跳过」。
    //   以前这个坑被另一个 bug 盖住了——第 1 题会把**下一题**的选项捞进来，
    //   opts 非空 → li_html 压根不生成 → 规则没机会误判。v6.22 修掉串选项后
    //   它就露头了（test_vision_v39 当场抓红），这里一并修掉。
    let htmlForRule = li_html || '';
    if (htmlForRule) {
      const c3 = li.cloneNode(true);
      c3.querySelectorAll('.edui-default, .edui-editor, .edui-toolbar,'
        + ' .edui-upload, .edui-editor-body, script, style').forEach(x => x.remove());
      htmlForRule = c3.outerHTML.replace(/\s+/g, ' ').slice(0, 6000);
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
    // v6.13：手写/纸上书写题（真机实测：「粘贴答案要求：在纸上书写ok()函数，
    // 拍照上传」——要求写在题目富文本里而非纯题干，所以 stem 和 li_html 都要扫）
    const ATTACH_RE = /上传附件|提交附件|附件上传|附件提交|以附件|附件形式|拍照上传|手写|纸上书写|纸上作答|在纸上|书写后拍照|拍照.{0,8}(发送|回传)|上传.{0,8}(报告|文件|文档|图片|视频|音频|压缩包|作品|附件)|提交.{0,8}(报告|文件|文档|压缩包|作品|附件)|(报告|文件|作品|附件).{0,6}(上传|提交)/;
    const stemUpload = ATTACH_RE.test(stem) || ATTACH_RE.test(htmlForRule);
    const upload = hasUploader || stemUpload;
    // 题干里是否带图/公式：这类题靠纯文本拿不到题面——超星的数学公式常以图片或
    // MathML 渲染，提取出来是空白或乱码。命中就标记，交给视觉模型「亲眼看」
    // （见 llm_solve 的视觉通道）。判据宁可宽一点：多截一张图只是多花几分钱，
    // 漏了则是整题答不出。
    const hasMedia = !!li.querySelector(
      'img, svg, math, .MathJax, .MathJax_Preview, [class*=formula], [class*=math], canvas');
    // 字体反爬（真机 2026-10-17，大学物理 1.3/1.4/1.5 全中）：平台把题干和
    // 选项里的每个字换成**另一个码位的字**（「理想流体」→「惪想惥惦」），再用
    // 自定义字体 font-cxsecret 把真实字形画出来。DOM 文本是乱码，纯文本通道
    // 喂给模型只能得到一段胡编的答案（用户看到的就是「答案填错」）。
    // 判据：题干/选项元素的计算字体族里带 cxsecret（平台上就是这个族名）。
    // ⚠ 不能靠「含私用区字符」判：这些乱码是正常汉字码位，_stem_missing
    //   那套 PUA 判据对它们完全无效（这是本轮真机踩到的坑）。
    let scrambled = false;
    try {
      const fam = e => (e ? (getComputedStyle(e).fontFamily || '') : '').toLowerCase();
      const RE_CX = /cxsecret|font-cx|cx-?secret/;
      const probe = [li.querySelector('.Zy_TItle')].concat(
        [...li.querySelectorAll('.Zy_TItle span, .Zy_TItle p')].slice(0, 4),
        [...li.querySelectorAll('li')].slice(0, 4));
      scrambled = probe.some(e => RE_CX.test(fam(e)));
    } catch (e) { /* 取不到计算样式就当没加密，宁可多走一次文本通道 */ }
    // 供截图按题定位（Playwright 侧用 [data-cx-idx="N"] 抓这一题的图）
    li.setAttribute('data-cx-idx', idx);
    out.push({no: idx + 1, stem: stem.slice(0, 500),
              atype: at ? at.value : '',
              opts, fills, upload, hasMedia, scrambled, li_html, doc_counts: li_doc,
              mediaSig: mediaSig});
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

# 回读：每题当前**真正勾着**的选项值，按 input name 归组。
# 用来核实「点过了」≠「还在」——平台 reload 会静默冲掉未落库的点选
# （真机 2026-10-17：填 15 道、交上去只剩 8 道）。
JS_READ_CHECKED = r"""
() => {
  const boxes = document.querySelectorAll(
    'input[name^=answer][type=radio], input[name^=answer][type=checkbox]');
  // ⚠ 一个可选项都没找到 = 这页面结构我们不认识，**返回 null 表示「读不了」**，
  //   绝不能返回 {}：空对象与「页面上一道都没勾」长得一模一样，调用方会据此
  //   把整份判成答案全丢。宁可漏报也不能谎报。
  if (!boxes.length) return null;
  const out = {};
  boxes.forEach(x => {
    if (!x.checked) return;
    (out[x.name] = out[x.name] || []).push(x.value || '');
  });
  return out;
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
     'url': 'https://api.deepseek.com/v1', 'model': 'deepseek-flash',
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
        # 临时名带线程 id：HTTP 线程保存 Key 与任务线程迁移老明文 Key
        # 可能并发，固定名会互相把对方写一半的文件截断重写
        tmp = path.with_name('%s.%d.tmp' % (path.name, threading.get_ident()))
        tmp.write_bytes(LLM_KEY_MAGIC + b'\x01\x00\x00\x00' + blob)
        os.replace(tmp, path)             # 原子替换：写一半崩掉不会留坏文件
    except Exception as e:
        try:
            tmp.unlink()                  # 别留孤儿 .tmp
        except Exception:
            pass
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


# ---------------------------------------------------------------- 记住密码
# 界面「记住密码」：DPAPI 加密落 runtime/（与 LLM Key 同款基建），
# config 与前端 localStorage 一律不碰——那两处是明文，密码不住那。
# 路径每次现读 RUNTIME（沙箱在 import 后改道它，别把路径收进容器）。
LOGIN_PWD_MAGIC = b'CXPWD1'
_LOGIN_PWD_HEADER = len(LOGIN_PWD_MAGIC) + 4    # 魔数 + 4 字节小版本号
_LOGIN_PWD_NAME = 'password.dpapi'


def _login_pwd_path():
    return RUNTIME / _LOGIN_PWD_NAME


def save_login_pwd(pwd: str) -> bool:
    """加密保存登录密码；返回是否真的存成了（False = 加密不可用）。

    密码比 API Key 更敏感：加密不可用时**绝不留明文**（宁可让用户
    每次手输），与 Key 的「明文兜底」策略刻意不同。
    传空串 = 删除已保存的密码。
    """
    pwd = (pwd or '').strip()
    if not pwd:
        forget_login_pwd()
        return True
    if os.name != 'nt':
        return False
    try:
        blob = _dpapi(pwd.encode('utf-8'), protect=True)
    except Exception as e:
        log('密码加密失败（未保存，仍需每次手输）：%s' % str(e)[:80],
            'warn')
        return False
    path = _login_pwd_path()
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name('%s.%d.tmp' % (path.name,
                                            threading.get_ident()))
        tmp.write_bytes(LOGIN_PWD_MAGIC + b'\x01\x00\x00\x00' + blob)
        os.replace(tmp, path)         # 原子替换：写一半崩不留坏文件
    except Exception as e:
        try:
            tmp.unlink()
        except Exception:
            pass
        log('密码写入失败（未保存）：%s' % str(e)[:80], 'warn')
        return False
    return True


def load_login_pwd() -> str:
    """读已保存的密码。没存过 / 头不对 / 解不开都返回 ''（不抛异常）。"""
    try:
        raw = _login_pwd_path().read_bytes()
    except Exception:
        return ''
    if not raw.startswith(LOGIN_PWD_MAGIC) or len(raw) <= _LOGIN_PWD_HEADER:
        return ''
    try:
        return _dpapi(raw[_LOGIN_PWD_HEADER:], protect=False).decode('utf-8')
    except Exception:
        # 换 Windows 用户或换电脑后解不开——设计使然。文件留着不管，
        # 下次保存会覆盖。
        return ''


def has_login_pwd() -> bool:
    return _login_pwd_path().exists()


def forget_login_pwd():
    try:
        _login_pwd_path().unlink(missing_ok=True)
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
            if vis else '还没填模型名（如 deepseek-flash）')
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
            if not isinstance(data, dict):
                # 网关/代理有时回 JSON 数组或字符串：归类成「填错了」，
                # 别当网络异常白等 2+4 秒重发（带图时还会重传 base64）
                last = '对方返回的不是接口数据（JSON 顶层不是对象）：'\
                       '确认地址填的是接口地址'
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



def _clean_answer_text(v):
    r"""简答/填空答案的「显示保真」清洗：模型吐什么 LaTeX/markdown 都洗成纯文本。

    真机反馈：简答题答案填进卷子是一坨乱码——模型把公式写成 \\begin{pmatrix}…
    \\end{pmatrix} 或 $…$，落进平台输入框原样显示，老师看到的就是 LaTeX 源码。
    提示词禁了它还会漏（推理模型不听话），所以在解析层兜底洗一遍：
      ① pmatrix/matrix/bmatrix… 环境体 → 行按 \\\\ 分、列按 & 分，拼成
         「a, b; c, d」式紧凑文本（矩阵内容还在，可读、可显示）；
      ② 行内/块级数学定界符 $…$、\\[…\]、\\(…\) 摘掉，只留里面的内容；
      ③ 剩余的 \\command（\\frac{a}{b}→(a/b)、\\sqrt{x}→√(x)、\\times→×、
         \\cdot→·、\\le/\\ge→≤/≥ 等常见命令）翻译或剥除；
      ④ markdown 修饰 **bold**、`code` 剥壳留字。
    清洗只对简答('4')与填空('2')生效——选择题答案是字母、判断是对错，
    沾不上这些符号。
    """
    if not v:
        return v
    s = str(v)

    # ① 矩阵/数组环境：\begin{xxx} … \end{xxx}（xxx=pmatrix/bmatrix/matrix/
    #    array/cases…）。行按 \\ 切、列按 & 切 →「a, b; c, d」。
    def _mat(m):
        body = m.group(1)
        rows = [r.strip().strip('{}') for r in re.split(r'\\\\', body)]
        rows = [', '.join(c.strip() for c in r.split('&') if c.strip())
                for r in rows if r.strip()]
        return '[ ' + '; '.join(rows) + ' ]'
    s = re.sub(r'\\begin\{[a-zA-Z*]+\}(.*?)\\end\{[a-zA-Z*]+\}',
               _mat, s, flags=re.S)

    # ② 数学定界符：$…$、\[…\]、\(…\) —— 摘壳留内容（$$ 块同 $$…$$ 一起吃）。
    s = re.sub(r'\$\$?(.+?)\$\$?', r'\1', s, flags=re.S)
    s = re.sub(r'\\\[([\s\S]+?)\\\]', r'\1', s)
    s = re.sub(r'\\\(([\s\S]+?)\\\)', r'\1', s)

    # ③ 常见 LaTeX 命令 → 可显示符号；带参数的翻译成括号形式；其余 \cmd 剥除。
    s = s.replace('\\times', '×').replace('\\cdot', '·').replace('\\div', '÷')
    s = s.replace('\\leq', '≤').replace('\\le', '≤').replace('\\geq', '≥')
    s = s.replace('\\ge', '≥').replace('\\neq', '≠').replace('\\ne', '≠')
    s = s.replace('\\approx', '≈').replace('\\pm', '±').replace('\\infty', '∞')
    s = s.replace('\\alpha', 'α').replace('\\beta', 'β').replace('\\gamma', 'γ')
    s = s.replace('\\delta', 'δ').replace('\\Delta', 'Δ').replace('\\pi', 'π')
    s = s.replace('\\theta', 'θ').replace('\\lambda', 'λ').replace('\\mu', 'μ')
    s = s.replace('\\sigma', 'σ').replace('\\omega', 'ω').replace('\\Omega', 'Ω')
    s = s.replace('\\in', '∈').replace('\\subset', '⊂').replace('\\cup', '∪')
    s = s.replace('\\cap', '∩').replace('\\forall', '∀').replace('\\exists', '∃')
    s = re.sub(r'\\(?:d?)frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}', r'(\1/\2)', s)
    s = re.sub(r'\\sqrt\s*\{([^{}]*)\}', r'√(\1)', s)
    s = re.sub(r'\\(?:text|mathrm|mathbf|mathit|operatorname)\s*\{([^{}]*)\}',
               r'\1', s)
    s = re.sub(r'\\left|\\right|\\displaystyle|\\quad|\\qquad|\\,|\\;|\\:|\\!',
               '', s)
    s = re.sub(r'\\[a-zA-Z]+', '', s)          # 剩下的 \command 一律剥除
    s = s.replace('{', '').replace('}', '')    # 残余花括号（分组痕迹）

    # ④ markdown 修饰：**粗体**、*斜体*、`代码`、行首 # 标记 —— 剥壳留字。
    s = re.sub(r'\*\*(.+?)\*\*', r'\1', s, flags=re.S)
    s = re.sub(r'(?<!\*)\*(?!\*)([^*\n]+)\*(?!\*)', r'\1', s)
    s = re.sub(r'`([^`]*)`', r'\1', s)
    s = re.sub(r'(?m)^#{1,6}\s+', '', s)

    # 洗完把空白压平：换行/多空格 → 单空格（答案要填进单行输入框/编辑器）。
    s = re.sub(r'\s+', ' ', s).strip()
    return s


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
    # 答案里带 LaTeX（\begin{pmatrix}…）时，值内的 {pmatrix} 会被上面的
    # 「无嵌套花括号」正则切碎候选，全部解析失败 → 白花一次模型钱。
    # 兜底：再试「首个 { 到最后一个 }」的整段（贪婪段）。
    extra = []
    i0, i1 = txt.find('{'), txt.rfind('}')
    if i0 != -1 and i1 > i0:
        extra.append(txt[i0:i1 + 1])
    for cand in reversed(cands + extra):
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
            elif t in ('2', '4'):                # 填空/简答：文本，但要做显示
                v = _clean_answer_text(v)        # 保真清洗（LaTeX/markdown→纯文本）
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

    v6.22：先剥掉题号前缀与题型标记再取哈希。真机实测（2026-10-17）：
    答题页的题干是「2 【单选题】…」，同一道题在**已批阅**页是
    「1 【单选题】…」（批阅页每题都显示 1）——不剥前缀，两边算出的键
    对不上，「把正确答案回灌缓存」就永远写不进下次要用的那个键。
    """
    import hashlib
    norm = _stem_norm(stem)
    if not norm:
        return ''
    return hashlib.sha1(norm.encode('utf-8')).hexdigest()[:16]


def _q_fp(t) -> str:
    """题面图片指纹：题干图 + 各选项图，按页面显示顺序拼起来取哈希。

    v6.25 新增。真机 2026-10-18（大学物理 4.7 / 5.8）整卷都是图片题：
    题干一张 PNG、四个选项各一张 PNG，DOM 文字只剩「N 【单选题】」，
    题干哈希是空串——这类题每次重刷都得把 15 张图重新发给模型。

    图片地址是内容哈希（p.ananas.chaoxing.com/star3/origin/<md5>.png），
    同一道题永远同一串地址；选项顺序实测三轮加载完全一致，所以这串指纹
    既能认题、也能安全地复用「字母答案」。

    没有任何图返回 ''（纯文字题继续用题干哈希）。
    """
    if not isinstance(t, dict):
        return ''
    sig = (t.get('mediaSig') or '').strip()
    if not sig:
        # 没有图 = 纯文字题。**必须**返回空串让调用方退回题干哈希：
        # 纯文字题的选项顺序会被平台打乱（v6.23 的 randomOptions），按显示
        # 顺序拼出来的指纹每次都不一样，缓存永远命不中，等于白丢缓存。
        return ''
    opts = []
    for o in (t.get('opts') or []):
        own = str(o.get('img') or '').strip()
        if not own:
            # 纯文字选项：真机 4.7 的 Q5 / Q9 / Q11 四个选项全是文字，
            # Q7 / Q8 各有一个。只认图片不认文字，这几题的指纹里选项部分
            # 就是空的——选项一换位，缓存里的字母就指到别的选项上。
            if t.get('scrambled'):
                # 字体反爬的选项文字是乱码：密文本体跨加载实测一致，但为
                # 稳妥只记「这里有个文字选项」，不把密文塞进指纹。
                own = '#'
            else:
                own = re.sub(r'\s+', '', str(o.get('label') or ''))[:80]
        opts.append(own)
    if any(opts):
        sig += '|' + ','.join(opts)
    if not sig.strip('|,'):
        return ''
    import hashlib
    return 'img:' + hashlib.sha1(sig.encode('utf-8')).hexdigest()[:16]


def _cache_key(t) -> str:
    """这道题在答案缓存里该用哪个键。

    有图片指纹优先用指纹：全图片题题干是空的，只有指纹认得出来；纯文字
    题退回题干哈希。指纹键带 `img:` 前缀，与 sha1 十六进制不会撞。
    """
    fp = _q_fp(t)
    if fp:
        return fp
    return _stem_key(t.get('stem'))


def _stem_norm(stem):
    """题干归一：剥题号/题型前缀、压掉全部空白。空题干返回空串。

    只剥**开头**那一段「1 【单选题】」「2. 」「(3) 」——正文里的数字不动。
    """
    s = (stem or '').strip()
    if not s:
        return ''
    s = re.sub(r'^\s*\d+\s*【[^】]{0,12}】\s*', '', s)
    s = re.sub(r'^\s*[（(]?\d+[）).、]\s*', '', s)
    return re.sub(r'\s+', '', s)


def _opt_map(t):
    """{显示字母: 选项指纹}——按**页面当前渲染**取。

    显示字母用 `dl`（平台打乱后标在页面上的那个字母），不是位置下标：
    真机实测 Q1 显示 A 的选项，radio 的 value 是 C，位置和字母并不是一回事。
    答案字母对的就是 `dl`（落答侧也是按 `dl` 找选项），所以校验也只能用 `dl`。
    """
    out = {}
    for o in (t.get('opts') or []):
        dl = str(o.get('dl') or '').strip().upper()
        if not dl:
            continue
        v = str(o.get('img') or '').strip()
        if not v:
            v = '#' if t.get('scrambled') else re.sub(
                r'\s+', '', str(o.get('label') or ''))[:80]
            # 选项文字自带显示字母（真机：「A P1和P2两点」）。选项一换位
            # 这个前缀就跟着变，不剥掉的话同一个选项永远对不上、缓存全 miss。
            v = re.sub(r'^[（(]?[A-Za-z][)）.．、:：]?', '', v)
        out[dl] = v
    return out


def _cache_get(cache, t):
    """从缓存里取答案；取不到 / 不可信时返回 ''。v6.25。

    见 patch 头注释：文字题的键不含选项顺序，所以**必须**校验字母指向的
    选项还是原来那个，否则会把答案填到别的选项上。
    """
    k = _cache_key(t)
    if not k:
        return ''
    hit = cache.get(k)
    if hit is None:
        return ''
    if not isinstance(hit, dict):
        # 老格式（纯字符串）：只有纯文字题敢用，图片题没法校验
        return '' if _q_fp(t) else str(hit)
    ans = str(hit.get('a') or '')
    if not ans or ans in ('?', '无'):
        return ''
    old = hit.get('o') or []
    if not old:
        return ans
    cur = _opt_map(t)
    if not cur:
        # 页面这次没读出选项（读了一半）——不能拿题目不完整时的状态去判错
        return ans
    old_map = {str(dl).upper(): str(sig) for dl, sig in old
               if str(dl or '').strip()}
    by_sig = {}
    for dl, sig in cur.items():
        by_sig.setdefault(sig, dl)
    out = []
    for ch in ans.upper():
        if not ch.strip() or ch in (' ', '，', ',', '、'):
            continue
        # old_map 里没有这个字母（缓存答案带着当前卷子不存在的选项，比如上次
        # 四选一、这次三选一）→ 这份答案对不上这题，当没命中。不用单独判：
        # get 拿到 None，下面一样返回 ''。
        now = by_sig.get(old_map.get(ch))
        if not now:
            return ''                 # 那个选项没了 / 内容变了 → 重新问
        out.append(now)
    if not out:
        return ''
    return ''.join(sorted(out))


def _cache_put(cache, t, answer):
    """写缓存，连同「显示字母 → 选项指纹」。返回是否写入。"""
    k = _cache_key(t)
    if not k or answer in (None, '?', '', '无'):
        return False
    m = _opt_map(t)
    if m:
        cache[k] = {'a': str(answer), 'o': sorted(m.items())}
    else:
        cache[k] = str(answer)
    return True


def _answer_cache_load():
    """读本地答案缓存（runtime/answer_cache.json）。坏了就当没有。"""
    try:
        d = json.loads(ANSWER_CACHE_FILE.read_text(encoding='utf-8'))
        # 顶层必须是对象：手改成 list/数字时 get/赋值会每题都炸
        return d if isinstance(d, dict) else {}
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

    范围：主观题（填空 / 简答），题干里带图 / 公式的题，以及**字体反爬**的题
    （t['scrambled']：DOM 里是「惪想惥惦」这类替换码位，纯文本喂模型只会得到
    胡编的答案，真机 2026-10-17 实测）。不含图的纯文字选择题不截——给文本
    就够，省流量也省等待。
    """
    # v6.25：主观题**不再无条件**走视觉。真机实测（2026-10-18）：纯文字
    # 填空题/简答题截图发给视觉模型，模型要先把图里的字认一遍再答，既慢
    # 又容易把数字认错，而且每题一次调用——一份 20 题的主观卷就是 20 次
    # 请求。只有题面真有图/公式（hasMedia）或被平台字体加密（scrambled）
    # 时才值得看图。
    return bool(t.get('hasMedia')) or bool(t.get('scrambled'))


def _shot_timu(hw, idx):
    """把第 idx 道题（0 基）整块截成 JPEG 字节；失败返回 None。

    定位靠 JS 解析时打的 data-cx-idx（与 JS_PARSE_TIMUS 的 _boxes 同序）。
    整题连题干带公式带选项一起截，模型才看得全。走 JPEG q60：题面多是文字
    与线条，压完通常几十 KB，比 PNG 小一个数量级，字照样清楚。

    v6.22：元素截图会**偶发卡死**（真机 2026-10-17：`ElementHandle.screenshot:
    Timeout 30000ms exceeded`，同一题前后两次探测都是 0.1 秒成功的）。卡住的
    那一题如果恰好是选项全为图片 / 字体反爬的题，纯文本通道根本答不出来——
    用户看到的就是「第 1 题答案完全空着」。所以这里改成：
      ① 短超时（8 秒）+ 关动画关光标，最多连试两次；
      ② 还不行就退回「整页截图 + 裁剪框」（page.screenshot(clip=…)）——
         实测这条路一直稳，出图内容与元素截图一致；
      ③ 全失败才返回 None，并如实记日志（不再谎称「按纯文本处理」）。
    """
    el = None
    try:
        el = hw.query_selector('[data-cx-idx="%d"]' % idx)
    except Exception:
        el = None
    if not el:
        return None
    kw = dict(type='jpeg', quality=60, timeout=8000,
              animations='disabled', caret='hide')
    for _ in (1, 2):
        try:
            return el.screenshot(**kw)
        except Exception:
            continue
    # ---- 兜底：整页截图 + 裁剪框 ----
    pg = getattr(hw, 'page', None)
    if pg is not None:
        try:
            el.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        try:
            bb = el.bounding_box()
        except Exception:
            bb = None
        if bb:
            try:
                return pg.screenshot(clip=bb, type='jpeg', quality=70,
                                     timeout=10000, animations='disabled')
            except Exception as e:
                log('    第 %d 题截图失败（%s）。' % (idx + 1, str(e)[:70]),
                    'warn')
                return None
    log('    第 %d 题截图失败（元素截图两次超时，裁剪兜底也拿不到）。'
        % (idx + 1), 'warn')
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


def _clean_vision_answer(a: str, atype='') -> str:
    """收拾视觉模型的回答：只留答案本身；'?' 表示没把握（按未答处理）。

    v6.22：按题型归一，与文本通道（_parse_llm_answers）口径一致——
    真机 2026-10-17 踩过：视觉通道把「答案：B」原样交上去，选择题落答时
    拿 'B' 去比选项值比上了，但「B、」「选B」这类就会落不下去；判断题也会
    被原样塞进卷子。选择题只留字母、判断题统一对/错、填空简答洗 LaTeX。
    """
    s = (a or '').strip()
    if not s:
        return ''
    # 只取第一行：模型偶尔会在答案后面补一句解释
    s = s.splitlines()[0].strip()
    s = re.sub(r'^(答案|答|正确答案|最终答案)\s*[:：]\s*', '', s).strip()
    s = s.strip('"\'“”「」『』（）()').strip()
    if s in ('?', '？', '无', '未知'):
        return ''
    if atype in ('0', '1'):
        # 「B」「选B」「答案B」「（B）」→ 'B'；多选「A、C」→ 'AC'
        s = re.sub(r'[^A-Za-z]', '', s).upper()
    elif atype == '3':
        s = '错' if s in ('错', '错误', 'FALSE', 'F', '×', 'false') else '对'
    elif atype in ('2', '4'):
        s = _clean_answer_text(s)
    return s.strip()


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
        hint = ('简答题：200 字内，先写关键步骤/公式代入，最后一行给结论'
                '（如「答：1.1×10^8 J」），不要只给一个数。')
    else:
        hint = '选择题：只输出所选选项的大写字母，多选连写（如 AC）。'
    stem = (t.get('stem') or '').strip()
    parts = ['图片是作业里的一道%s题，请**直接看图**作答。%s' % (tn, hint)]
    # 字体反爬的题：题干是「惪想惥惦」这类替换码位的乱码，喂给模型只会把它
    # 带偏（真机 2026-10-17：把乱码题干一起发过去，模型答非所问）。
    # 这种情况**一个字都不给**，只让它看图。
    if stem and not t.get('scrambled'):
        # 提取到的题干可能因公式渲染而残缺，说明以图为准，免得模型被误导
        parts.append('（页面提取的题干文字，可能与图不一致，以图中的题目为准）：'
                     + stem[:400])
    elif t.get('scrambled'):
        parts.append('（页面文字被平台字体加密，提取出来是乱码，已隐去；'
                     '请完全以图片内容为准。）')
    opts = t.get('opts') or []
    if opts and t['atype'] not in STYPES:
        if t.get('scrambled'):
            # 选项文字和题干一样被平台字体加密（「理想流体」→「惪想惥惦」）。
            # 把乱码选项一起发过去只会把模型带偏：真机 2026-10-18 复现时，
            # 模型挑了个字母但理由与图完全对不上。只保留显示字母——模型只要
            # 知道有几个选项、看图辨认后回字母，落答侧再按 dl 映射。
            parts.append('选项文字被平台字体加密，已隐去（共 %d 个选项，'
                         '按图中从上到下依次标为 A 起）：\n%s'
                         % (len(opts), '\n'.join(
                             '   %s、（内容见图片）' % (o.get('dl') or '?')
                             for o in opts)))
        else:
            parts.append('页面给的选项：\n'
                         + '\n'.join('   ' + o['label'] for o in opts))
    parts.append('只输出答案本身：不要题号、不要解释、不要任何多余文字。'
                 '答案会被原样填进网页输入框：禁止 LaTeX（\\frac、'
                 '\\begin{pmatrix}、$…$）、Markdown（**、#、`）、代码块——'
                 '数学式用普通字符写（如 x^2、√2、≤、矩阵 (1,2; 3,4)）。'
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
            a = _clean_vision_answer(_ask_one_vision(cfg, t), t.get('atype'))
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


def _solve_vision_batch(cfg, timus, progress):
    """把若干**客观题**的截图合并成一次多图请求作答。返回 {题号: 答案}。

    v6.25。真机（2026-10-18，大学物理 4.7 / 5.8）每份 15 题、每题一张整题
    截图，逐题发就是 15 次带图请求。合批只发一次，成本降一个数量级，而且
    模型一次看到全部题面，题号反而更不容易串。

    准确率保护（省钱的实现绝不能拿正确率换）：
      · 每张图前都标「第 N 题」，模型按题号回 JSON；
      · 整批解析不出来 → **退回逐题**重来；
      · 部分题没答上 → 只对那几题逐题补问；
      · 仍按 `_clean_vision_answer` 归一，与单题通道口径一致。
    """
    timus = [t for t in (timus or []) if t.get('shot')]
    if not timus:
        return {}
    if len(timus) == 1:
        return _solve_by_vision(cfg, timus, progress)
    parts = ['下面是同一份作业里的 %d 道题，**每张图就是一整道题**'
             '（题干和它的选项都在同一张图里，按 A、B、C…从上到下排）。'
             % len(timus)]
    imgs = []
    for t in timus:
        tn = ATYPES.get(t['atype']) or '选择题'
        nopt = len(t.get('opts') or [])
        parts.append(('【第 %d 题】%s，%d 个选项。'
                      % (t['no'], tn, nopt)) if nopt
                     else ('【第 %d 题】%s。' % (t['no'], tn)))
        # 题干文字能读（没被字体加密）就一并给，模型对文字比看图更稳
        stem = (t.get('stem') or '').strip()
        if stem and not t.get('scrambled'):
            parts.append('   题干文字（可能与图不一致，以图为准）：'
                         + stem[:300])
        imgs.append(t['shot'])
    parts.append('输出要求：只输出一个 JSON 对象，键为题号数字的字符串'
                 '（"1"、"2"…），值为答案。单选输出一个大写字母；多选连写'
                 '（如 "AC"）；判断题输出 "对" 或 "错"。'
                 '看不清、没把握的题，值写 "?"。'
                 '不要输出 JSON 以外的任何文字，不要解释。')
    try:
        resp = llm_chat(cfg, '\n'.join(parts), timeout=240, images=imgs)
    except RuntimeError as e:
        progress('    批量看图调用失败（%s），改回逐题。'
                 % str(e)[:100], 'warn')
        return _solve_by_vision(cfg, timus, progress)
    got = _parse_llm_answers(resp, max(t['no'] for t in timus),
                             types={t['no']: t['atype'] for t in timus})
    out = {}
    for t in timus:
        a = got.get(t['no'])
        if a:
            c = _clean_vision_answer(a, t.get('atype'))
            if c:
                out[t['no']] = c
    if not out:
        progress('    批量看图的回答解析不出来，改回逐题。', 'warn')
        return _solve_by_vision(cfg, timus, progress)
    miss = [t['no'] for t in timus if t['no'] not in out]
    if miss:
        # 合批没答上的少量题单独补问一次：总代价仍远低于每题都发
        left = [t for t in timus if t['no'] in miss]
        progress('    合批里有 %d 题没答上，逐题补问。' % len(left))
        out.update(_solve_by_vision(cfg, left, progress))
    progress('    合批看图：一次请求答上 %d/%d 题。'
             % (len(out), len(timus)))
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
        # v6.25：有**图片指纹**的题也能查缓存了。真机 4.7 / 5.8 整卷都是图，
        # 题干哈希是空串——以前这类题每次重刷都整卷重问一遍。图片地址是内容
        # 哈希、选项顺序三轮加载一致，所以同一串图 = 同一道题、同一套字母。
        # 没有指纹的带图题仍然不查（截图代表不了题干，老规矩）。
        k = _cache_key(t)
        if not k:
            rest.append(t)
            continue
        # 字体反爬且没指纹：题干是乱码，哈希对得上但语义不可靠，不查
        if t.get('scrambled') and not _q_fp(t):
            rest.append(t)
            continue
        hit = _cache_get(cache, t)
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
    # ⚠ 字体反爬又没截到图的题**不能**走文本通道：题干是「惪想惥惦」这类
    # 替换码位，模型只能照着乱码率性作答，交上去必然错（真机 2026-10-17：
    # 1.5 第 1 题被填成「256」）。这类题一律不答，让 answer_one 按「没答上」
    # 处理并如实提示——错答比空着更糟，空着用户还能自己补。
    blind = [t for t in todo if t.get('scrambled') and not t.get('shot')]
    txt = [t for t in todo if not t.get('shot') and not t.get('scrambled')]
    if blind:
        progress('    第 %s 题的页面文字被平台字体加密、又没截到图，'
                 '发给模型只会得到乱码答案，按没答上处理。'
                 % '、'.join(str(t['no']) for t in blind[:8]), 'warn')
    got = {}
    # ---- 视觉通道 ----
    # v6.25：**纯图片客观题合并成一次请求**。真机 4.7 / 5.8 每份 15 题全是
    # 「题干一张图 + 四个选项各一张图」，逐题发就是 15 次带图请求；合起来
    # 一次发，模型还能同时看到全部题干，反而更不容易串题。
    # 主观题（填空/简答）仍然一题一发：它们的作答要求各不相同，而且历史
    # 上正是混在一起发导致答案错位。
    if vis:
        batch = [t for t in vis if t['atype'] in ATYPES]
        solo = [t for t in vis if t['atype'] not in ATYPES]
        if batch:
            progress('    %d 道图片题合并成一次视觉请求（省调用次数）。'
                     % len(batch))
            got.update(_solve_vision_batch(cfg, batch, progress))
        if solo:
            progress('    %d 道主观题逐题看图。' % len(solo))
            got.update(_solve_by_vision(cfg, solo, progress))
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
                line += ('（简答：200 字内，先写关键步骤/公式代入，'
                         '最后一行给结论，如「答：1.1×10^8 J」）')
            else:
                for o in t['opts']:
                    line += '\n   %s' % o['label']
            lines.append(line)
        prompt = ('你是答题助手。请回答下面的题目。\n'
                  '输出要求：只输出一个 JSON 对象，键为题号（字符串），值为答案。\n'
                  '单选/多选输出大写字母（多选如 "AC"）；判断题输出 "对" 或 "错"；\n'
                  '填空只输出应填内容本身，多空题按空序用 "|" 分隔；'
                  '简答输出答案文本（200 字内）：先写关键步骤/公式代入，'
                  '最后一行给结论（形如「答：…」）；只给一个光秃秃的结果不行。\n'
                  '【显示保真，铁律】答案会被原样填进网页输入框：禁止任何 '
                  'LaTeX（\\frac、\\begin{pmatrix}、$…$ 等）、Markdown '
                  '（**、#、`）、代码块——数学式直接用普通字符写，'
                  '如 x^2、√2、≤、矩阵写成 (1,2; 3,4)。\n'
                  '凡题干要求上传附件/文件/报告、要求手写/在纸上书写后拍照、'
                  '或需要粘贴外部资料的题，输出 "?"。\n'
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
    # v6.22：**看图答的题不写缓存**。截图代表不了一段题干（同一题干换一组
    # 图答案就不同），而 `_stem_key` 只看题干——真机 2026-10-17：1.2 第 1 题
    # （选项全是图片）看图答上的 'B' 被写进缓存，换一份作业同题干再问时会
    # 拿到这条旧答案却看不到图。字体反爬的题同理（题干是乱码，语义不可靠）。
    # v6.25：判据从「不是截图题」改成「有图片指纹 或 不是截图题」。
    # 纯图片题**必须**靠指纹才写得进缓存（题干是空的）；反过来，截图题
    # 若算不出指纹就还是不能写——那正是 v6.22 那条「截图代表不了题干」。
    fresh = {t['no']: t for t in todo
             if ans.get(t['no']) not in (None, '?', '')
             and _cache_key(t)
             and not t.get('scrambled')
             and (_q_fp(t) or not t.get('shot'))}
    if fresh:
        for t in fresh.values():
            _cache_put(cache, t, ans[t['no']])
        _answer_cache_save(cache)
    progress('    大模型返回：%s' % json.dumps(ans, ensure_ascii=False)[:200])
    return ans


_GRADED_TEXT_MARKS = ('本次成绩', '重新测试')


# 已批阅页里「正确答案」的提取。真机（2026-10-17，大学物理 1.2）实测结构：
#   选择题： <span>正确答案： B </span>                  （选项本身是图片时唯一可靠来源）
#   填空题： <p class="font14">正确答案：</p> 之后紧跟
#            <p class="font14">第一空： 8</p>（多空就多行）
# 平台的答案本来就是标准答案，比让模型猜准得多 —— 拿它回灌本地缓存，
# 下次同题干直接命中，既省模型钱又几乎不会错。
JS_GRADED_ANSWERS = r"""
() => {
  const lis = [...document.querySelectorAll('.questionLi')];
  const tms = [...document.querySelectorAll('.TiMu')];
  const boxes = (lis.length && tms.length <= 1) ? lis : tms;
  const out = [];
  boxes.forEach((li, idx) => {
    let right = '';
    // ① 单行写法：正确答案： X    （真机上这一行常常和「我的答案：」挤在同一个
    //    span 里，如「正确答案： B 我的答案：」——所以取到后要在「我的答案」处截断）
    for (const e of li.querySelectorAll('span,p,div,i,b')) {
      const t = (e.innerText || '').replace(/\s+/g, ' ').trim();
      if (!/^正确答案\s*[:：]/.test(t) || e.children.length > 3) continue;
      right = t.replace(/^正确答案\s*[:：]\s*/, '')
               .split(/我的答案|得分\s*[:：]|\s:\s*;/)[0].trim();
      if (right) break;
    }
    // ② 分空写法：「正确答案：」后面跟着一串「第N空： xxx」
    if (!right) {
      const blanks = [];
      let started = false;
      for (const e of li.querySelectorAll('p,span,div')) {
        const t = (e.innerText || '').replace(/\s+/g, ' ').trim();
        if (!t || t.length > 200) continue;
        if (/^正确答案\s*[:：]\s*$/.test(t)) { started = true; continue; }
        if (started && /^第[一二三四五六七八九十\d]+空\s*[:：]/.test(t)) {
          blanks.push(t.replace(/^第[一二三四五六七八九十\d]+空\s*[:：]\s*/, '').trim());
          continue;
        }
        if (started && /^我的答案/.test(t)) break;
      }
      if (blanks.length) right = blanks.join('|');
    }
    // ③ i[aria-label] 写法（判断题）：我的答案：<i aria-label="错">错</i>
    //    正确答案同构时也用这个
    if (!right) {
      const i = li.querySelector('.Py_answer i[aria-label], i.font14[aria-label]');
      const t = (li.innerText || '');
      if (i && /正确答案/.test(t)) {
        const seg = t.slice(t.indexOf('正确答案'));
        const m = seg.match(/正确答案\s*[:：]\s*([^\s]{1,20})/);
        if (m) right = m[1].trim();
      }
    }
    // 回灌缓存要按选项校验（v6.25）：批阅页这份 DOM 就在手边，顺手把选项
    // 带上——否则写进缓存的字母没有校验依据，换份卷子一重排就答错。
    const opts = [];
    li.querySelectorAll('input[name^=answer]').forEach(x => {
      if (x.type === 'hidden') return;
      const lab = x.closest('li') || x.parentElement;
      const im = lab ? lab.querySelector('img') : null;
      opts.push({val: x.value,
                 label: ((lab && lab.innerText) || '').replace(/\s+/g, ' ').trim().slice(0, 120),
                 img: im ? (im.getAttribute('src') || '').split('/').pop() : ''});
    });
    out.push({no: idx + 1, right: right, opts: opts,
              mine: ((li.innerText || '').match(/我的答案\s*[:：]\s*([^\n]{0,80})/) || [])[1] || ''});
  });
  return out;
}
"""


def _graded_answers(frame):
    """从「已批阅」页读出平台的正确答案：[{no, right, mine}]；读不到返回 []。

    读不到不算错：平台改版、题不是标准题型都可能读空。调用方按「没读到就
    照旧走模型」处理，绝不因为这里失败而影响答题主流程。
    """
    try:
        got = frame.evaluate(JS_GRADED_ANSWERS) or []
    except Exception:
        return []
    out = []
    for g in got:
        try:
            out.append({'no': int(g.get('no')), 'right': (g.get('right') or '').strip(),
                        'mine': (g.get('mine') or '').strip()})
        except Exception:
            continue
    return out


def _harvest_graded(hw, log, progress):
    """已批阅的作业：把平台的正确答案读下来回灌本地缓存（v6.22）。

    为什么值得做：这次用户反馈的「答案填错」里，平台其实已经把正确答案写在
    批阅页上（「正确答案： B」「第一空： 8」）。工具以前看到 `already` 就
    直接返回，把这份现成的标准答案白白放掉了——下次遇到同一章重做/同一份
    作业，还得再让模型猜一次，猜错就又是 0 分。

    只做只读提取 + 本地缓存写入：不点按钮、不交卷、不改平台任何状态。
    失败一律静默（平台改版读不到就算了），绝不影响答题主流程。
    """
    try:
        graded = _graded_answers(hw)
    except Exception:
        return 0
    if not graded:
        return 0
    # 题干：能解析到就用解析出的（已剥过题号前缀，与答题页口径一致）；
    # 解析不到就退回批阅页自己那份 DOM，同样走 _stem_key 归一。
    stem_map = {}
    opt_map = {}
    try:
        timus = _poll(hw, JS_PARSE_TIMUS, lambda v: bool(v), 8000, 400) or []
    except Exception:
        timus = []
    for t in timus:
        # 字体反爬的题干是乱码，写进缓存只会污染
        if t.get('scrambled'):
            continue
        stem_map[t['no']] = t.get('stem') or ''
        # v6.25：回灌也要留选项指纹，否则这份答案在缓存里没有校验依据
        if t.get('opts'):
            opt_map[t['no']] = _opt_map(t)
    if not stem_map:
        try:
            rows = hw.evaluate(r"""() => {
              const lis = [...document.querySelectorAll('.questionLi')];
              const tms = [...document.querySelectorAll('.TiMu')];
              const boxes = (lis.length && tms.length <= 1) ? lis : tms;
              return boxes.map((li, i) => ({no: i + 1,
                stem: ((li.querySelector('.Zy_TItle') || {}).textContent || '')}));
            }""") or []
            for r in rows:
                stem_map[int(r['no'])] = r.get('stem') or ''
        except Exception:
            return 0
    # 解析不到题干时退回批阅页 DOM：那份里也有 opts（JS 已顺手带走），
    # 题干和选项要在同一份 DOM 上取，否则字母对不上同一套选项。
    if not opt_map:
        for g in graded:
            if g.get('opts'):
                om = _opt_map({'opts': g['opts']})
                if om:
                    opt_map[g['no']] = om
    try:
        return _cache_graded(stem_map, graded, progress, opt_map)
    except Exception:
        return 0


def _strip_blank_prefix(s):
    """剥掉平台批阅页答案里的「第一空：」前缀，多空用 | 串起来。

    真机实测（2026-10-17，1.2 第 2 题）：批阅页写的是「第一空： 8」，
    原样写进输入框就是错的——空里只能有「8」。多空题会拿到
    「第一空： a|第二空： b」，逐空剥完再用 | 连接，正好对上 _split_fill。
    """
    s = (s or '').strip()
    if not s:
        return ''
    # 多空先处理：按 | 切开逐段剥（不能先走单空那条——「第[一..]空：」的正则
    # 会把后面的「|第二空： b」整段吞进捕获组）
    if '|' in s:
        parts = []
        for p in s.split('|'):
            p = re.sub(r'^\s*第[一二三四五六七八九十\d]+空\s*[:：]\s*', '', p)
            parts.append(p.strip())
        return '|'.join(parts)
    # 单空：整串就是一个「第N空： 值」
    m = re.fullmatch(r'第[一二三四五六七八九十\d]+空\s*[:：]\s*(.*)', s, re.S)
    if m:
        return m.group(1).strip()
    return s


def _cache_graded(stem_map, graded, progress, opt_map=None):
    """把已批阅页的正确答案写回本地答案缓存（按题干归一后的哈希）。

    stem_map: {题号: 题干}。只写**读得到题干且答案非空**的题。
    返回写入条数。字体反爬的题干是乱码，写进去只会污染缓存——调用方
    传进来的 stem_map 已把这类题滤掉。

    opt_map: {题号: {显示字母: 选项指纹}}（v6.25）。写进去的答案要能被
    `_cache_get` 校验；不校验的旧条目在换卷重排后会把答案填到别的选项上。
    没有选项的题（填空/简答）照旧写纯字符串。
    """
    if not graded or not stem_map:
        return 0
    cache = _answer_cache_load()
    n = 0
    for g in graded:
        stem = stem_map.get(g['no'])
        right = _strip_blank_prefix(g.get('right') or '')
        if not stem or not right or right in ('?', '无'):
            continue
        k = _stem_key(stem)
        if not k:
            continue
        m = (opt_map or {}).get(g['no']) or {}
        val = {'a': right, 'o': sorted(m.items())} if m else right
        if cache.get(k) != val:
            cache[k] = val
            n += 1
    if n:
        _answer_cache_save(cache)
        progress('    已把平台给的 %d 条正确答案回灌本地缓存（下次同题直接命中）。'
                 % n)
    return n


# ================================================================ 成绩 / 重做
# 背景（2026-10-18，v6.24）：用户要「扫描时顺带查成绩」，并把低于 85 分的
# 测验自动重刷（最多 2 次，取最高分）。
#
# 真机实测三种页面的可见信号（大学物理 1.7~7.8 全部核过）：
#   ① 已批阅且有成绩：正文有「本次成绩：100 / 最终成绩：100」+「还可以重做5次」
#   ② 未到及格线：正文明说「未达到及格线，请重做」，但**不显示分数**
#   ③ 待完成/已重做：正文「待完成」，没有成绩也没有「未达到及格线」
# 所以「低于 85 分」只能对 ① 精确判定；② 一律当作「不及格、该重刷」。
JS_SCORE = r"""
() => {
  const t = (document.body ? document.body.innerText : '').replace(/\r/g, '');
  const flat = t.replace(/\s+/g, ' ');
  const num = re => { const m = flat.match(re); return m ? parseFloat(m[1]) : null; };
  // 「本次成绩」是这一轮真正生效的分数；「最终成绩」在多次作答后才是最高分，
  // 但页面上两者通常相同。优先本次，取不到再退最终。
  let score = num(/本次成绩\s*[:：]?\s*([\d.]+)/);
  if (score === null) score = num(/最终成绩\s*[:：]?\s*([\d.]+)/);
  const left = num(/还可以重做\s*(\d+)\s*次/);
  // 满分：标题行「共N题,M分」里的 M；取不到就按 100。
  const full = num(/共\s*\d+\s*题\s*[，,]\s*([\d.]+)\s*分/);
  // 「是不是答题页」：有提交按钮或没渲染出批阅信息，都说明还没交。
  // ⚠ 真机 2026-10-18（2.6 未及格）踩过：答题页顶部也写着「未达到及格线，
  //   请重做」——那是提示文案，不是可点的重做入口。拿它当按钮去找一定扑空。
  //   真正能点「重做」的只有**已批阅页**（有本次成绩 + 正确答案 + 重做链接）。
  const submitBtn = !!document.querySelector(
    '#confirmSubmitWork, #form1submit, .btnBlueSubmit, a[onclick*=submit]');
  return {
    score: score,
    full: full || 100,
    left: left,
    canRedo: /重做/.test(flat) && left !== 0,
    graded: /本次成绩|最终成绩|正确答案/.test(flat),
    notPass: /未达到及格线|请重做/.test(flat),
    pending: /待完成/.test(flat),
    done: /已完成/.test(flat),
    isAnswerPage: submitBtn || !/本次成绩|正确答案/.test(flat),
    nQ: document.querySelectorAll('.questionLi,.TiMu').length
  };
}
"""


def read_score(frame) -> dict:
    """读一个作业卡当前页面的成绩/重做状态。读不到返回 {}。

    只读，不点任何按钮。frame 已分离/页面结构不认识时返回 {}，
    调用方据此按「未知」处理——绝不把「读不到」当成「没分」。
    """
    try:
        d = frame.evaluate(JS_SCORE)
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


# 点「重做」→ 等弹窗 → 点「确定」。
# 真机结构（2026-10-18）：批阅页有一个文字为「重做」的按钮，点了弹 #redoWin
# 对话框「之前答题内容会保留，确认重做？」，再点「确定」才真的调 retest()。
# 分两步是为了能如实区分「没有重做按钮」和「弹窗没出来」。
JS_CLICK_REDO = r"""
() => {
  // 真机 2026-10-18（7.8 批阅页）实测的按钮长这样：
  //   <span style="..." onclick="$('.mask').css('display','block');$('#redoWin').show();">重做</span>
  // 它是叶子节点、没有 id/class，只有文本 + onclick 指向 #redoWin。
  // 所以：① 先按「叶子节点且文本正好是重做」找（别用 closest，会把整块
  //        答题区都算进来）；② 再按 onclick 里出现 redoWin / retest 兜底。
  const leaf = e => e && e.children.length === 0;
  const norm = e => ((e.innerText || e.value || '') + '').replace(/\s+/g, '');
  const cands = [...document.querySelectorAll('a,button,span,div,i,em')];
  let b = cands.find(e => leaf(e) && norm(e) === '重做' && (e.offsetWidth || e.offsetHeight));
  if (!b) b = cands.find(e => norm(e) === '重做');
  if (!b) {
    b = cands.find(e => {
      const oc = e.getAttribute('onclick') || '';
      return /redoWin|retest/i.test(oc) && (e.offsetWidth || e.offsetHeight);
    });
  }
  if (!b) return 'none';
  b.click();
  return 'ok';
}
"""

JS_CONFIRM_REDO = r"""
() => {
  // 弹窗结构（真机 7.8）：#redoWin 里两个按钮，文案「确定」「取消」。
  // 找可见的那一个——弹窗本身可能还在 display:none 里闪一下。
  const win = document.querySelector('#redoWin');
  const scope = win || document.querySelector('.layui-layer') || document;
  const leaf = e => e && e.children.length === 0;
  const norm = e => ((e.innerText || e.value || '') + '').replace(/\s+/g, '');
  const ok = [...scope.querySelectorAll('a,button,span,div,input')].find(
    e => norm(e) === '确定' && (e.offsetWidth || e.offsetHeight));
  const b = ok || [...scope.querySelectorAll('a,button,span,div,input')]
    .find(e => norm(e) === '确定');
  if (!b) return 'none';
  b.click();
  return 'ok';
}
"""


def _click_redo(hw) -> str:
    """在批阅页点「重做」并确认。返回 ok / none / no-dialog。"""
    try:
        if hw.evaluate(JS_CLICK_REDO) != 'ok':
            return 'none'
    except Exception:
        return 'none'
    # 弹窗是平台 JS 现渲染的，给它一点时间
    for _ in range(12):
        time.sleep(0.25)
        try:
            if hw.evaluate(JS_CONFIRM_REDO) == 'ok':
                return 'ok'
        except Exception:
            return 'ok'      # 点击后 frame 可能立刻分离（正在导航），算成功
    return 'no-dialog'


def _wait_answer_frame(page, timeout=20.0):
    """重做后等答题页出现。返回 (frame, note)。

    retest() 是 GET {_HOST_CP2_}/work/retest，平台处理完才 302 回
    doHomeWorkNew；网络慢时会先看到一批空 frame。所以就按 URL 找
    doHomeWorkNew，超时返回 (None, 'timeout')。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for f in page.frames:
            if 'doHomeWorkNew' in (f.url or ''):
                return f, 'ok'
        time.sleep(0.4)
    return None, 'timeout'


def _score_of(d) -> float:
    """把 read_score 的结果转成可比较的分数；读不到返回 -1。"""
    try:
        s = d.get('score')
        return float(s) if s is not None else -1.0
    except Exception:
        return -1.0


def needs_retake(d, threshold=85.0) -> bool:
    """看 read_score 的结果，判断这份要不要重刷。

    三种情况：
      · 明确低于阈值（满分不是 100 的按百分比折算）→ 要
      · 平台明说「未达到及格线，请重做」→ 要（此时页面不给分数）
      · 其余（无成绩 / 已达标 / 读不到）→ 不要

    ⚠ 不能只看 score：不及格页面（真机 2.6 / 3.10 / 6.6）**根本不显示分数**，
      只写「未达到及格线，请重做」。漏掉这条会让最该重刷的几份全被跳过。

    ⚠ 也不能只看 notPass：**答过的答题页顶部同样会写「未达到及格线，请重做」**
      （2.6 实测）。那种页面读出来 notPass=true、score=None、isAnswerPage=true，
      但它的实质是「已点过重做、正等你答」——该做的是**接着答**，不是再点一次重做。
      所以：有分数就按分数判；没分数时，只有「已批阅但不及格」才算够格重刷；
      纯答题页交给调用方按「answer」处理。
    """
    if not isinstance(d, dict) or not d:
        return False
    s = _score_of(d)
    if s >= 0:
        try:
            full = float(d.get('full') or 100) or 100.0
        except Exception:
            full = 100.0
        pct = s * 100.0 / full
        return pct < float(threshold or 85.0)
    # 没分数：已批阅 + 未达及格线 → 该重刷
    if d.get('graded') and d.get('notPass'):
        return True
    # 答题页（待完成 / 未及格提示）不算「该重刷」，它本来就在等作答
    return False


def needs_answer(d) -> bool:
    """这份是不是「就等着作答」——平台已判不及格、但页面已经是答题页。

    真机 2026-10-18：2.6 / 3.10 / 6.6 三份都渲染成 doHomeWorkNew 答题页，
    顶部写着「未达到及格线，请重做」，可页面上根本没有可点的「重做」按钮
    （那是已批阅页才有的）。这种份的正确动作是**直接作答交卷**，不是再去找
    一个不存在的按钮。区分它和「从没做过的待完成」：后者 graded 也是 false，
    但正文没有「未达到及格线」。
    """
    return bool(isinstance(d, dict)
                and (d.get('pending') or d.get('notPass'))
                and not d.get('graded'))


def needs_fresh_answer(d) -> bool:
    """这份是不是「从没做过、等着从头答」的卷子（v6.26）。

    与 needs_answer 的区别：needs_answer 也覆盖「已判不及格、页面在等你答」，
    那份上面可能写着「未达到及格线，请重做」；这里只认**干干净净没做过**的：
      · pending=True（答题页顶部的「待完成」）；
      · 或者 fakeDone（章节列表说完成、测验卡说没做，v6.25 的对账结论）。
    已批阅 / 有分数 / 有未及格提示的一律不算——那些归重刷链，不能被
    「补做」再抢一次（重复交卷会白白消耗平台的重做次数）。
    """
    if not isinstance(d, dict) or not d:
        return False
    if d.get('graded') or d.get('score') is not None or d.get('notPass'):
        return False
    return bool(d.get('pending') or d.get('fakeDone'))

# ================================================================ 成绩清单
# 扫描期「顺带查成绩」：作业列表只给「已完成 / 未完成」，不给分数；分数只在
# 打开作业卡之后的批阅页上。所以要对章节页里**像作业卡**的节点逐个打开读一次。
#
# 成本控制（真机 2026-10-18 实测）：
#   · 只碰标题带「测试/测验/练习/作业/考试」的节点（章节列表里已经能判），
#     视频/文档节点一个都不开；
#   · 每个节点先正常打开作业卡（_wait_hw_iframe 会逐格找选项卡）；
#   · 主 frame 的 URL 是 doHomeWorkNew = 还没做的作业，直接跳过不读分；
#     只有 selectWorkQuestionYiPiYue / 正文带「本次成绩」的才读；
#   · 失败一律记「未知」，绝不把没读到当成 0 分。
def _norm_node_name(s) -> str:
    """把「1.8 单元测试」「1.8.1 单元测试」这类节点名归一，用来对齐作业列表。"""
    s = re.sub(r'\s+', ' ', (s or '')).strip()
    return s


def _reconcile_fake_done(rec, listed_pending) -> bool:
    """这份是不是「章节列表说已完成、测验卡说没做」。

    真机 2026-10-18（大学物理 4.7 / 5.8）：章节目录里挂的是
    `icon_yiwanc / 已完成`，可点进去是 doHomeWorkNew 答题页、
    read_score 给出 pending=True、score=None。平台的目录进度和测验卡
    各记各的，目录那份是错的。

    判据只认「卡片明确说没做」：
      · pending=True（答题页顶部的「待完成」）；
      · 或者 nQ>0 且三样都没有（没分数、没批阅、没未及格提示）——
        那是「打开了答题页但从没提交过」。
    章节列表**已经**把它算作欠账的（listed_pending）不算 fake——那种
    节点本来就在待办里，再标一次只会重复。读不到状态（没作业卡）也不标：
    不知道的事不能当成「没做」，否则会平白多出一批假任务让用户去点。
    """
    if not isinstance(rec, dict):
        return False
    if rec.get('inChapterList'):
        return False
    if rec.get('pending'):
        return True
    return bool(rec.get('nQ')) and not (
        rec.get('graded') or rec.get('score') is not None
        or rec.get('notPass') or rec.get('done'))


def scan_scores(cfg, page, c, cpi, progress, control=None, deep_nodes=None) -> dict:
    """逐节点读一份课程里测验/作业的成绩。返回 {'items': [...], 'failed': n}。

    page 必须是**已登录**的一个新标签页（调用方负责 ctx/page 生命周期）。

    items 里每项的字段（界面/报告直接吃）：
      kid / node / score / full / left(剩余重做次数) / graded / notPass /
      pending / done / canRedo / url(该节点的作业直达链接)

    只读：不点重做、不填、不交。
    """
    control = control or (lambda: 'run')
    out = {'items': [], 'failed': 0}
    try:
        pd = fetch_progress_detail(page, c, cpi, cfg)
    except Exception as e:
        return {'items': [], 'failed': 1,
                'reason': str(e).splitlines()[0][:120]}
    if pd.get('page_failed'):
        return {'items': [], 'failed': 1, 'reason': pd.get('reason', '')}
    nodes = pd.get('all') or []
    if deep_nodes:
        nodes = [n for n in nodes if n.get('kid') in deep_nodes]
    if not nodes:
        return out
    # v6.25：章节列表**自己认为欠账**的 kid。作业卡说「待完成」而这里没有它
    # → 平台的目录标记和测验卡对不上，就是用户报的「显示任务点已完成、其实
    # 没做」。见 _reconcile_fake_done 的注释。
    listed_pending = {n.get('kid') for n in (pd.get('pending') or [])
                      if n.get('kid')}

    for n in nodes:
        if control() == 'stop':
            break
        kid, name = n.get('kid'), n.get('name') or ''
        url = _cards_url(c, cpi, kid, 0)
        rec = {'kid': kid, 'node': name, 'url': url,
               'score': None, 'full': 100, 'left': None, 'graded': False,
               'notPass': False, 'pending': False, 'done': False,
               'canRedo': False, 'note': '',
               # v6.25：章节列表说它完成了，测验卡却说待完成 / 从没做过。
               # 这类节点以前**根本不会被扫到**（章节列表不给 .catalog_jindu
               # 就不进待办），用户只能自己一个个点进去才发现。
               'fakeDone': False, 'inChapterList': kid in listed_pending}
        try:
            page.goto(url, wait_until='domcontentloaded')
            page.wait_for_timeout(1000)
            tabs = _cards_tabs(page)
            order = _tab_order(tabs) if tabs else list(range(MAX_CARDS_TABS))
            hw, note = _wait_hw_iframe(page, url, lambda *a: None, tabs=order)
            if not hw:
                if note == 'norender':
                    rec['note'] = '作业卡打不开'
                else:
                    rec['note'] = '没有作业卡'
                out['items'].append(rec)
                continue
            d = read_score(hw)
            if not d:
                rec['note'] = '页面读不到'
                out['items'].append(rec)
                continue
            rec.update({'score': d.get('score'), 'full': d.get('full') or 100,
                        'left': d.get('left'), 'graded': bool(d.get('graded')),
                        'notPass': bool(d.get('notPass')),
                        'pending': bool(d.get('pending')),
                        'done': bool(d.get('done')),
                        'canRedo': bool(d.get('canRedo')),
                        'nQ': d.get('nQ') or 0})
            rec['fakeDone'] = _reconcile_fake_done(rec, listed_pending)
            if d.get('score') is None and not d.get('graded'):
                rec['note'] = ('待完成' if d.get('pending')
                               else ('未及格（无分数）' if d.get('notPass')
                                     else '无成绩'))
                rec['pending'] = bool(d.get('pending'))
            out['items'].append(rec)
            progress('    · %-24s %s' % (
                cut(name, 24),
                ('%s 分' % _fmt_num(rec['score'])) if rec['score'] is not None
                else (rec['note'] or '无成绩')))
        except Exception as e:
            rec['note'] = '读取失败：%s' % str(e).splitlines()[0][:60]
            out['failed'] += 1
            out['items'].append(rec)
        time.sleep(random.uniform(0.6, 1.4))
    return out


def _fmt_num(v) -> str:
    """分数显示：100.0 → 100，73.3 → 73.3，读不到 → —。"""
    if v is None:
        return '—'
    try:
        f = float(v)
        return ('%d' % f) if abs(f - round(f)) < 1e-9 else ('%.1f' % f)
    except Exception:
        return str(v)


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


def _cards_url(c, cpi, kid, num=0):
    """章节卡片页地址。num 见 CARDS_URL 上方注释（选项卡序号）。"""
    return CARDS_URL.format(clsid=c['clsid'], cid=c['cid'], kid=kid,
                            num=num, cpi=cpi)


JS_CARDS_TABS = r"""
() => {
  // 两条路子，任一命中即可：
  //  A. cards 页当着 iframe 挂在学生页里时，选项卡那一排在外层 #prev_tab
  //     （每格 li: id=dct{N}、title=中文名、onclick=changeDisplayContent(N,…)，
  //      N 从 1 起，对应 URL 的 num=N-1）。
  //  B. cards 页被**直接当顶层打开**时（工具一直这么干），外层根本没有
  //     prev_tab，但本页的 AttachmentSetting 会带一个 cardid——**选项卡越界
  //     时 AttachmentSetting 整个不存在**（真机实测：1.7 只有 num=0 有，
  //     num=1/2 都没了）。所以「有没有 cardid」就是「这一格存不存在」。
  const grab = doc => {
    try {
      const pt = doc && doc.getElementById('prev_tab');
      if (!pt) return null;
      const lis = [...pt.querySelectorAll('li')];
      if (!lis.length) return null;
      return lis.map((li, i) => {
        let num = i;
        const oc = li.getAttribute('onclick') || '';
        const m = oc.match(/changeDisplayContent\(\s*(\d+)/);
        if (m) num = parseInt(m[1], 10) - 1;
        return {num: num,
                title: (li.getAttribute('title') || '').trim(),
                text: (li.innerText || '').replace(/\s+/g, ' ').trim()};
      });
    } catch (e) { return null; }
  };
  let r = null;
  try { r = grab(window.parent.document); } catch (e) {}
  if (!r) { try { r = grab(window.top.document); } catch (e) {} }
  if (!r) { try { r = grab(document); } catch (e) {} }
  if (r && r.length) return r;
  // ---- B：只有 cardid，没有选项卡名字 ----
  try {
    const S = (typeof AttachmentSetting !== 'undefined') ? AttachmentSetting : null;
    if (S && S.defaults && S.defaults.cardid) {
      const n = parseInt(location.search.match(/[?&]num=(\d+)/)?.[1] || '0', 10);
      return [{num: n, title: '', text: ''}];
    }
  } catch (e) {}
  return [];
}
"""

# 这一格选项卡是否真实存在（越界的 num 在平台上压根没有）。
# 判据：AttachmentSetting 有没有带 cardid —— 比「连续两格空」准得多，
# 也不会因为某格恰好没作业就误判成到头了。
JS_TAB_EXISTS = r"""
() => {
  try {
    const S = (typeof AttachmentSetting !== 'undefined') ? AttachmentSetting : null;
    return !!(S && S.defaults && S.defaults.cardid);
  } catch (e) { return true; }   // 取不到就当存在，宁可多扫一格
}
"""

# 选项卡名字里可能出现的「放作业」关键词。用来决定先扫哪一格——纯优化，
# 取不到标题时退回 0,1,2… 顺序扫，结果一样。
_TAB_WORK_WORDS = ('测试', '测验', '作业', '练习', '考试', '任务')


def _cards_tabs(page):
    """本节点到底有几张选项卡、各叫什么；拿不到返回 []。

    真机（2026-10-17，大学物理 1.1）实测：3 格 —— 视频与讲义(num=0) /
    讨论(num=1) / 测试(num=2)，**章节测验在第 3 格**。以前 num 写死 0，
    所以只在第一格里找作业卡，测验几乎永远找不到。
    """
    try:
        tabs = page.evaluate(JS_CARDS_TABS) or []
    except Exception:
        return []
    out = []
    for t in tabs:
        try:
            out.append({'num': int(t.get('num')), 'title': t.get('title') or '',
                        'text': t.get('text') or ''})
        except Exception:
            continue
    return out


def _tab_order(tabs, have_first=True):
    """扫选项卡的顺序：放作业的那几格排前面，其余按序号。

    这一步只是省时间（一格里平均要等 6 秒才判「没有作业壳」）。顺序错了
    也不影响正确性——最终会把所有格都扫到。名字拿不到时（顶层打开 cards
    页那种情况）就是 0,1,2,…，照样能扫到。
    """
    nums = [t['num'] for t in tabs]
    if not nums:
        return list(range(MAX_CARDS_TABS))
    head = [t['num'] for t in tabs
            if any(w in (t['title'] + t['text']) for w in _TAB_WORK_WORDS)]
    rest = [n for n in sorted(nums) if n not in head]
    order = head + rest
    if have_first and 0 in order:
        # 0 号（视频与讲义）本来就要打开，排最后能省一次；不过刷课那边已经
        # 在 num=0 上了，这里照顺序无所谓，保持稳定即可。
        pass
    # 再补上未知的后续格（标题拿不到时也靠它兜底）
    for n in range(MAX_CARDS_TABS):
        if n not in order:
            order.append(n)
    return order


def _wait_hw_iframe(page, url, log, tabs=None):
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

    tabs：要扫的选项卡序号（num）。默认 None = 只扫 0（向后兼容旧调用）。
      给 None 之外的值时按顺序扫，**找到作业卡就停**；扫到连续两格空
      （或到 MAX_CARDS_TABS / 已知选项卡数）就收工。返回的 note 里会带
      '|tab=N' 后缀，调用方据此知道这次是在哪个选项卡找到的。
    返回 (hw | None, note)。note ∈ ''|'already'|'nocard'|'norender'|'failed'
        （扫全部选项卡时可能附 '|tab=N'）。
    """
    # 单选项卡 = 老行为（num 已在 url 里写死），保持逐字节等价
    nums = [None] if tabs is None else list(tabs)
    last_note = 'nocard'
    for num in nums:
        u = url
        if num is not None:
            if '{num}' in url:
                u = url.replace('{num}', str(num))
            elif 'num=' in url:
                u = re.sub(r'num=\d+', 'num=%d' % num, url)
            else:
                u = url + ('&' if '?' in url else '?') + 'num=%d' % num
        hw, note = _wait_hw_iframe_one(page, u, log)
        if hw is not None:
            return hw, note if num is None else '%s|tab=%d' % (note, num)
        if note == 'oob':
            # 这一格**压根不存在**（AttachmentSetting 都没了）。选项卡是连续
            # 编号的，越界就说明到头了 —— 这是唯一可靠的收工信号：
            # 1.7 只有 1 格、1.1 有 3 格，而「某格没作业」跟「这格不存在」
            # 长得一模一样，靠数空格子会误判（v6.21 真机踩过：1.1 的测验在
            # 第 3 格，被「连空两格」提前截断，白漏）。
            break
        last_note = note
        if num is None:
            break
        if note == 'failed':
            break
        # 有作业卡但渲染失败 / 这格没作业 → 都继续看下一格，别在这一格死磕
    return None, last_note


def _tab_exists(page, timeout_ms=1500):
    """这一格选项卡是否真实存在（见 JS_TAB_EXISTS）。"""
    try:
        page.wait_for_function("() => (typeof AttachmentSetting !== 'undefined')",
                               timeout=timeout_ms)
    except Exception:
        pass
    try:
        return bool(page.evaluate(JS_TAB_EXISTS))
    except Exception:
        return True      # 判不了就当存在，宁可多扫一格


def _wait_hw_iframe_one(page, url, log):
    """单个选项卡里等 doHomeWorkNew（_wait_hw_iframe 的内层实现）。

    多返回一种 note='oob'（out of bounds）：这个 num 在平台上不存在。
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
        # 第一次进 loop 就先看这格在不在（约 1 秒），不存在就立刻收工，
        # 不用白白等满 6 秒的「连壳都没有」判定。
        if not _tab_exists(page):
            return None, 'oob'
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


def _by_display_letter(t, vals):
    """按选项**显示字母**定位，返回 (names, 该选项的 value 列表)；失败 (set(), None)。

    为什么必须有这一层（真机 2026-10-17，大学物理 1.8 单元测试，15 题）：
    平台开了 `randomOptions=true`，渲染时把选项**顺序打乱**，页面上仍从 A 起
    按显示位置标字母，但 radio 的 `value` 是打乱前的原始字母。实测 Q2：
        显示 A → value=C   显示 B → value=D
        显示 C → value=B   显示 D → value=A
    模型看截图（或被题面文字带着）答的永远是**显示字母**。旧代码按 value
    匹配 → 答 C 实际点到「显示 B」那个选项 → 平台记 B。
    用户原话「大模型返回的答案是对的，但是还是填错位置」；批阅页 7 道错题里
    6 道的「我的答案」与按 value 点出来的结果完全一致，确认无疑。

    与 `_fallback_by_position` 的分工：那个是**位置**语义（第 N 个选项），
    这个是**显示字母**语义（A/B/C/D 标在第几个上）。平台打乱顺序时两者
    恰好等价，但显式读显示字母更稳——将来平台改标法（比如只标数字）也不怕。
    """
    opts = t.get('opts') or []
    if not opts:
        return set(), None
    out_names, out_vals = set(), []
    for ch in vals:
        want = str(ch).strip().upper()
        o = next((x for x in opts if str(x.get('dl') or '').upper() == want), None)
        if o is None:
            i = _POS_LETTERS.find(want)          # 没标字母就按位置兜
            o = opts[i] if 0 <= i < len(opts) else None
        if o is None or not o.get('name') or not o.get('val'):
            return set(), None
        out_names.add(o['name'])
        out_vals.append(o['val'])
    return out_names, out_vals or None


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


def retake_one(page, c, cpi, kid, cfg, progress, threshold=85.0,
               max_retakes=2, control=None) -> dict:
    """对单个节点：读成绩 → 不够就点「重做」，返回状态给调用方接着答题。

    返回 {'kid','before','left','status'}，status ∈
      'ok'(已达标) / 'unknown'(读不到) / 'nohw'(没有作业卡) /
      'nochance'(没重做机会了) / 'noredo'(找不到重做按钮) /
      'redo'(已点重做，页面正在跳答题页) / 'answer'(本来就是重做态，直接答)

    ⚠ 重做后必须**重新答题**而不是搬批阅页的「正确答案」：
      真机 2026-10-18（大学物理 4.7）实测，重做后平台连**题目顺序都重排**
      （批阅页第 10 题在重做页成了第 6 题），只有选项显示字母不变。按题号搬
      必错；按题干匹配又撞上一堆「题干是图片」的题（只剩「1 【单选题】 A B C D」）。
      所以老老实实重答一遍——v6.23 的显示字母落答实测能把 1.8/2.2/马原答到 100。
    """
    control = control or (lambda: 'run')
    out = {'kid': kid, 'before': None, 'left': None, 'status': 'nohw'}
    url = _cards_url(c, cpi, kid, 0)
    try:
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_timeout(1000)
        tabs = _cards_tabs(page)
        order = _tab_order(tabs) if tabs else list(range(MAX_CARDS_TABS))
        hw, _note = _wait_hw_iframe(page, url, log, tabs=order)
    except Exception as e:
        out['status'] = 'error'
        out['reason'] = str(e).splitlines()[0][:80]
        return out
    if not hw:
        return out
    d = read_score(hw)
    out['before'] = d.get('score')
    out['left'] = d.get('left')
    if not d:
        out['status'] = 'unknown'
        return out
    # ⚠ 顺序要紧：先判「已在答题页」再判「已达标」。答题页读出来是
    #   score=None + notPass=True，needs_retake 按设计会返回 False（那是给
    #   query_scores 区分「该不该点重做」用的），早退成 ok 就把这份漏掉了
    #   ——真机 2.6 / 3.10 / 6.6 正是这种页面，会一份都答不上。
    if needs_answer(d) or d.get('pending') or d.get('isAnswerPage'):
        out['status'] = 'answer'
        return out
    if not needs_retake(d, threshold):
        out['status'] = 'ok'
        return out
    if d.get('left') == 0 or not d.get('canRedo'):
        out['status'] = 'nochance'
        return out
    act = _click_redo(hw)
    if act != 'ok':
        out['status'] = 'noredo'
        out['reason'] = act
        return out
    out['status'] = 'redo'
    # 等 retest() 的 302 落到 doHomeWorkNew。真机实测这一步要 3~10 秒
    # （平台先在服务端重置作答记录，再跳答题页）。等不到不算失败：
    # 答题页可能下一屏自己出来，answer_one 会重新导航一次，所以照实返回
    # redo，让调用方接着走答题链。
    f2, how = _wait_answer_frame(page, timeout=20.0)
    out['answered_page'] = bool(f2)
    return out


def _read_node_score(page, c, cpi, kid) -> dict:
    """重新打开某节点的作业卡读一次成绩（重刷交卷后用）。读不到返回 {}。"""
    url = _cards_url(c, cpi, kid, 0)
    try:
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_timeout(1000)
        tabs = _cards_tabs(page)
        order = _tab_order(tabs) if tabs else list(range(MAX_CARDS_TABS))
        hw, _n = _wait_hw_iframe(page, url, lambda *a: None, tabs=order)
    except Exception:
        return {}
    return read_score(hw) if hw else {}


def query_scores(cfg, only, threshold=85.0, retake=True, max_retakes=2,
                 progress=None, control=None, headless=None,
                 answer_pending=False) -> dict:
    """「查成绩 + 低于阈值重刷」总入口（界面「查成绩 / 重刷」按钮用）。

    每门勾选的课：① 章节页取全部「像作业卡」的节点；② 逐个打开只读成绩；
    ③ 低于 threshold（或平台明说未达及格线）且还能重做 → 点重做、重新作答
    并交卷；④ 每份最多尝试 max_retakes 次，记最终成绩。

    answer_pending（v6.26）：把「待完成 / 假完成」的卷子也纳入作答。
      v6.25 的对账能把「平台标已完成、实际没做」查出来，但这条链只动手刷低分，
      查出来就结束了——用户说「只能查出来，该怎么进行做题啊」。打开后：
        · `fakeDone`（章节列表说完成、测验卡说没做）→ 直接作答交卷；
        · `pending`（真·待完成的卷子）→ 同样作答交卷。
      判据只用卡片自己的状态，**已批阅/有分数/有未及格提示的一律不碰**（那些
      走上面的重刷链，两条路不会抢同一份）。默认关：那是在账号上真交卷，
      必须由界面勾选框明确打开。

    ⚠ 重刷会真的消耗平台重做次数并重新交卷——只在用户点按钮、且确实低于阈值时做。
    ⚠ 报告里的分数是**最后一次读完的结果**；平台批阅可能延迟，读不到时如实
      记「未确认」，绝不把读不到写成 0 分。
    """
    progress = progress or (lambda m, *a: log(m, a[0] if a else 'info'))
    control = control or (lambda: 'run')
    t0 = time.time()
    thr = float(threshold or 85.0)
    cap = max(int(max_retakes or 2), 1)
    out = {'items': [], 'retaken': 0, 'improved': 0, 'failed': 0,
           'answered': 0, 'stopped': False, 'threshold': thr,
           'max_retakes': cap}
    ctx = launch(cfg, headless=headless)
    try:
        page = ctx.new_page()
    except Exception:
        close_ctx(ctx)
        raise
    try:
        banner('检查登录状态')
        if not check_login(page):
            if restore_state(ctx) and check_login(page):
                log('已用本地会话自动登录 ✓')
            else:
                raise NotLoggedIn('未登录')
        _quiet_win(page, cfg, headless)
        courses, cpi = collect_courses(page, cfg)
        keep, node_keep = split_only(only)
        picked = [c for c in courses if course_key(c) in keep]
        if not picked:
            raise RuntimeError('要查成绩的课程没匹配到（课程可能有变动）')
        banner('查成绩（%d 门课；%s）'
               % (len(picked),
                  ('低于 %s 分自动重刷，每份最多 %d 次'
                   % (_fmt_num(thr), cap)) if retake else '只看分不重刷'))
        if answer_pending:
            progress('另：把「待完成 / 平台标已完成、实际没做」的卷子也作答交卷'
                     '（已批阅、有分数的不碰）。', 'warn')
        for i, c in enumerate(picked, 1):
            if _wait_control(control, progress) == 'stop':
                out['stopped'] = True
                break
            progress('【%d/%d】%s' % (i, len(picked), c['name']))
            want = node_keep.get(course_key(c))
            try:
                sc = scan_scores(cfg, page, c, cpi, progress, control,
                                 deep_nodes=want)
            except Exception as e:
                progress('  ⚠ 这门课成绩没查成（%s），跳过。'
                         % str(e).splitlines()[0][:80], 'warn')
                out['failed'] += 1
                continue
            for rec in (sc.get('items') or []):
                rec['course'] = c['name']
                rec['key'] = course_key(c)
                out['items'].append(rec)
                if control() == 'stop':
                    break
                if not retake:
                    continue
                # v6.26：待完成 / 假完成 → 直接作答交卷（用户勾了才走这条）。
                # 判据只认「卡片明确说没做过」：已批阅、有分数、有未及格提示
                # 的一律不碰——那些归下面的重刷链，两条路不抢同一份。
                if answer_pending and needs_fresh_answer(rec):
                    progress('    ✎ %s：待完成，开始作答交卷…'
                             % cut(rec.get('node', ''), 20))
                    try:
                        st = answer_one(page, c, cpi, rec['kid'], cfg, True,
                                        progress)
                    except Exception as e:
                        progress('    ✗ 作答失败（%s）。'
                                 % str(e).splitlines()[0][:80], 'warn')
                        out['failed'] += 1
                        continue
                    rec['answerStatus'] = st
                    if st in ('submitted', 'already', 'unverified'):
                        out['answered'] += 1
                    # 交完当场读一次分（批阅可能没跟上，读不到就留空，
                    # 绝不把「读不到」写成 0 分）
                    d = {}
                    for _ in range(3):
                        time.sleep(3)
                        d = _read_node_score(page, c, cpi, rec['kid'])
                        if d and _score_of(d) >= 0:
                            break
                    if d:
                        rec['after'] = d.get('score')
                        rec['left'] = d.get('left')
                        rec['canRedo'] = bool(d.get('canRedo'))
                        if d.get('score') is not None:
                            rec['score'] = d.get('score')
                            rec['graded'] = bool(d.get('graded'))
                    # 已经交出去了就摘掉「假完成」的帽子——哪怕批阅还没跟上、
                    # 这次读不到分。留着标记会让面板再给一行「补做」，
                    # 用户顺手再点一次就是白耗一次重做。
                    if st in ('submitted', 'already', 'unverified'):
                        rec['fakeDone'] = False
                    progress('    %s %s'
                             % ('✓' if st in ('submitted', 'already') else '⚠',
                                {'submitted': '已交卷',
                                 'already': '平台显示已批阅（此前已交）',
                                 'unverified': '已发出，待平台确认',
                                 'partial': '部分已暂存，需补完再交',
                                 'report': '要上传附件，需本人完成',
                                 'unsupported': '题型不支持，需人工',
                                 'unsolved': '模型没答上，需人工',
                                 'norender': '答题页没渲染出来',
                                 'nohw': '没有作业卡'}.get(st, st)))
                    continue
                # 「该重刷」或「平台已判不及格、页面就在等你答」都算要处理。
                # 后者没有重做按钮可点，直接进作答分支（见下面的 answer_first）。
                # ⚠ 只看 notPass，不看 pending：pending 的节点既可能是「从没做过
                #   的作业」，也可能是「点过重做还没交的卷子」，页面分不出来。
                #   用户要的是「不及格的要重刷」，不是「替我把没做的作业全做了」
                #   ——所以纯待完成的只报告，不自动动手。
                answer_first = (not needs_retake(rec, thr)) and \
                    needs_answer(rec) and bool(rec.get('notPass'))
                if not needs_retake(rec, thr) and not answer_first:
                    continue
                if answer_first:
                    pass
                elif not rec.get('canRedo'):
                    progress('    · %s 低于 %s 分，但平台没有重做入口，跳过。'
                             % (cut(rec.get('node', ''), 20), _fmt_num(thr)),
                             'warn')
                    continue
                best = _score_of(rec)
                tries = 0
                while tries < cap and control() != 'stop':
                    tries += 1
                    progress('    ↻ %s：第 %d 次重刷（当前 %s）…'
                             % (cut(rec.get('node', ''), 20), tries,
                                ('%s 分' % _fmt_num(rec.get('score')))
                                if rec.get('score') is not None else '未及格'))
                    r = retake_one(page, c, cpi, rec['kid'], cfg, progress,
                                   threshold=thr, max_retakes=1)
                    if r.get('status') not in ('redo', 'answer'):
                        progress('    ✗ 第 %d 次重刷没进行下去（%s）。'
                                 % (tries, r.get('status')), 'warn')
                        break
                    out['retaken'] += 1
                    # 页面此刻已在答题页：走正常作答链（解析→模型→落答→交卷）
                    try:
                        st = answer_one(page, c, cpi, rec['kid'], cfg, True,
                                        progress)
                    except Exception as e:
                        progress('    ✗ 重刷作答失败（%s）。'
                                 % str(e).splitlines()[0][:80], 'warn')
                        out['failed'] += 1
                        break
                    if st not in ('submitted', 'already', 'unverified'):
                        progress('    ⚠ 重刷后没确认交卷（%s），停止对这题继续重刷。'
                                 % st, 'warn')
                        break
                    d = {}
                    for _ in range(6):
                        time.sleep(3)
                        d = _read_node_score(page, c, cpi, rec['kid'])
                        if d and _score_of(d) >= 0:
                            break
                    cur = _score_of(d) if d else -1
                    if d:
                        # 平台对重做取哪次分不公开，所以自己记：对外一律报
                        # **最高分**（用户选的就是「取最高分」），最后一次另存
                        # last 供排查。读不到（cur<0）不覆盖已知的最好成绩。
                        rec['last'] = d.get('score')
                        rec['left'] = d.get('left')
                        rec['canRedo'] = bool(d.get('canRedo'))
                        if cur >= 0:
                            best = max(best, cur)
                            rec['after'] = best
                    if cur >= thr:
                        progress('    ✓ %s：重刷后 %s 分，达标。'
                                 % (cut(rec.get('node', ''), 20), _fmt_num(cur)))
                        break
                    progress('    · 重刷后 %s，仍未达标。'
                             % (('%s 分' % _fmt_num(cur)) if cur >= 0 else '读不到成绩'))
                rec['attempts'] = tries
                rec['best'] = best if best >= 0 else rec.get('score')
                # 「改善」= 重刷后的最好成绩比动手前那一次高
                if (best >= 0 and rec.get('score') is not None
                        and best > float(rec['score'])):
                    out['improved'] += 1
        progress('查成绩结束：%d 份测验，重刷 %d 次，失败 %d 门，用时 %.1f 分钟。'
                 % (len(out['items']), out['retaken'], out['failed'],
                    (time.time() - t0) / 60))
    finally:
        save_state(ctx)
        close_ctx(ctx)
    return out


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
         | 'partial'(**部分作答已暂存**：已答上的都分批存进平台了，
                    但还有题没答上，所以没交卷——需你补完再提交)
         | 'unsolved'(一道都没答上，没有可保存的内容)
         | 'report'(整份是上传附件型，LLM 替不了)
         | 'failed' | 'norender'。
    """
    from urllib.parse import urlencode  # noqa: F401  (仅说明用)
    direct = bool(url)          # True = 作业列表直链（新版 dowork 页）
    tab_hit = None              # 作业卡是在哪一格选项卡里找到的
    if direct:
        hw, note = _open_work_page(page, url, log)
    else:
        # 章节页有好几格选项卡（视频与讲义 / 讨论 / 测试…），测验/作业卡
        # **不一定在第一格**。先打开 num=0 读出选项卡列表，再按「像放作业
        # 的排前面」的顺序逐格找；全都没有才算这个节点没作业。
        url = _cards_url(c, cpi, kid, 0)
        try:
            page.goto(url, wait_until='domcontentloaded')
            page.wait_for_timeout(1200)
            tabs = _cards_tabs(page)
        except Exception:
            tabs = []
        order = _tab_order(tabs) if tabs else list(range(MAX_CARDS_TABS))
        if tabs and len(tabs) > 1:
            log('    本章有 %d 个选项卡（%s），逐个找作业卡…'
                % (len(tabs),
                   ' / '.join('%d.%s' % (t['num'], t['title'] or '?')
                              for t in tabs)))
        hw, note = _wait_hw_iframe(page, url, log, tabs=order)
        m = re.search(r'\|tab=(\d+)$', note or '')
        if m:
            tab_hit = int(m.group(1))
            note = note[:m.start()]
            if tab_hit:
                log('    作业卡在第 %d 格选项卡（num=%d）里。'
                    % (tab_hit + 1, tab_hit))
    if note == 'already':
        log('    平台显示这份作业已批阅（此前已交过），无需再答再交。')
        _harvest_graded(hw, log, progress)
        return 'already'
    if hw is None:
        if note == 'norender':
            log('    作业卡两次都没渲染出答题页。', 'warn')
            return 'norender'
        # 以前这里一声不吭就 return，日志只有「节点 N/M」两行、汇总是
        # 「交卷 0」，用户看不出是「这节点本来就没作业」还是「工具没找到」。
        # 现在把看到的选项卡如实报出来（v6.21）。
        if not direct:
            if tabs and len(tabs) > 1:
                log('    这个节点的 %d 个选项卡里都没有作业/测验卡（%s），'
                    '跳过。' % (len(tabs),
                                ' / '.join(t['title'] or ('第%d格' % (t['num'] + 1))
                                           for t in tabs)))
            else:
                log('    这个节点没有作业/测验卡，跳过。')
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
                    fills.append({'name': f['name'], 'val': a, 'no': t['no']})
            continue
        if t['atype'] == '3':                # 判断题：统一转平台认的值
            a = '对' if a in ('对', '正确', 'TRUE', 'T', '√') else '错'
            vals = [a]
            if not any(o['val'] == a for o in t['opts']):
                # 有的判断题选项值是 true/false
                vals = ['true' if a == '对' else 'false']
        else:
            vals = list(a)                   # 多选 "AC" → ['A','C']
        # ⚠ 落答顺序（v6.23 起重排）：**显示字母优先**，其次按值，最后按位置。
        #   为什么显示字母排第一（真机 2026-10-17，大学物理 1.8）：
        #   平台随机打乱选项顺序后，页面上仍按显示位置标 A/B/C/D，而 radio 的
        #   value 是**打乱前**的原始字母（实测 Q2：显示 A=value C、显示 C=value B）。
        #   模型答的是它看到的显示字母，按 value 匹配就会点到别的选项 →
        #   用户看到的「答案对、位置错」。批阅页 7 道错题 6 道与错位吻合。
        #   判断题没有字母标号（对/错），继续走位置语义，所以排除 atype=='3'。
        names, pos_vals = set(), None
        if t['atype'] == '3':
            names, pos_vals = _fallback_by_position(t, vals)
        else:
            names, pos_vals = _by_display_letter(t, vals)
            if names:
                # 打乱过的页面：选中的选项「显示字母 ≠ 真实 value」。
                # 如实说一句，用户/排查时一眼就能确认这层在起作用。
                swapped = ['%s→%s' % (str(o.get('dl') or '?'),
                                      str(o.get('val')))
                           for o in t['opts']
                           if str(o.get('dl') or '') != str(o.get('val'))
                           and str(o.get('dl') or '').upper()
                           in {str(v).upper() for v in vals}]
                if swapped:
                    log('    第 %d 题选项顺序被平台打乱过，已按页面显示字母落答'
                        '（%s）。' % (t['no'], '、'.join(swapped[:4])), 'info')
        if not names:
            # 显示字母对不上（页面没标字母 / 标的是数字）→ 退回按 value 匹配，
            # 再退回按位置。两条老路都保留，老页面行为不变。
            names = {o['name'] for o in t['opts']
                     if o['val'] in vals and o.get('type') != 'divradio'}
            if not names:
                # 大小写归一（有的页面 value 是小写字母）
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
            # 'no' 只在本地用来分批（做几道存几道），发给页面前会摘掉
            fills.append({'name': nm, 'vals': pos_vals or vals, 'no': t['no']})
    if not fills:
        # 一道都没答上 = 确实没有可保存的内容，保持「整份不碰」。
        # 注意判据是 fills（真的能填进去的）而不是 missing：
        # 只要有一道答上了，就要往下走，把它存进平台。
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
    # ---- 分批填 + 分批暂存：「做几道存几道」 ----
    # 口径（v3.9.1 起）：
    #   · 答上的题**一律填进去**，并且每 save_every 道调一次平台暂存 ——
    #     中途被停止 / 断网 / 窗口被关，前面已填的答案也不会白填；
    #   · 有题没答上、或主观题没写进答题编辑器 → **只暂存、绝不交卷**
    #     （交上去就是白拿低分），剩下的题打开作业网址补完再点提交，
    #     先前暂存的答案还在，不用重答一遍。
    # 以前是「有一道没答上就整份不填不存」，用户实测的后果是：答案都对，
    # 平台上却什么都没有，等于白做。
    step = max(int(cfg.get('save_every') or 5), 1)
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
        if bi + 1 < len(batches):
            time.sleep(random.uniform(0.8, 2.2))
    # ---- 填完回读：确认点中的选项**真的还在页面上**（v6.23）----
    # 平台任何一次 `location.reload()` 都会把没落库的点选静默冲掉，而
    # `JS_FILL_ANSWERS` 的返回值只代表「点那一下点到了」。不自己回读一遍，
    # 「填了 15 道、交上去只剩 8 道」这种事在日志里完全看不出来（真机踩过）。
    want = {}
    for f in order:
        if 'vals' in f:
            want.setdefault(f['name'], set()).update(
                str(v) for v in (f.get('vals') or []))
    if want:
        try:
            got = hw.evaluate(JS_READ_CHECKED)
        except Exception:
            got = None
        # 读不到（页面结构不认识 / frame 已分离）就**不做判断**：宁可漏报，
        # 也不能因为一次读失败就谎称「答案全丢了」把整份判成没答上。
        # JS 侧用 null 表示「读不了」，{} 表示「读了，一道都没勾」——两者语义
        # 完全不同，这里不能混。
        if isinstance(got, dict):
            lost = [nm for nm, vs in want.items()
                    if not vs.issubset({str(x) for x in (got.get(nm) or [])})]
            if lost:
                op_miss += len(lost)
                log('    ⚠ 有 %d 个选项点完又丢了（页面被平台重载过），'
                    '这些题交上去会是空。' % len(lost), 'warn')
    # ⚠ 暂存**只能放在所有批次填完之后**（v6.23 修正）。
    #   根因（真机 2026-10-17，大学物理 1.8 单元测试，15 题全对却只落下几道）：
    #   平台 saveWork() 成功后自己会 `window.location.reload()`（源码里明写着，
    #   探针实测拦不住 Location 上的 reload）。旧代码每填完一批就暂存一次，
    #   于是「填 → 存 → 整页重载 → 后面批次刚点上的选项全被冲掉」：
    #   日志里三次「已填 5 个选项（未命中 0）」都在，最后交上去的却只剩
    #   几道——用户看到的就是「最后两个甚至没填上」。
    #   「做几道存几道」的本意是「别因为没交卷就全白做」，一次收尾暂存同样
    #   满足；而中途暂存反而会**亲手毁掉**还没落库的那部分。
    saved_last = False
    if (not submit) or bool(missing) or txt_miss or op_miss:
        saved_last = _save_draft()
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
    try:
        page = ctx.new_page()
        banner('检查登录状态')
        if not check_login(page):
            if restore_state(ctx) and check_login(page):
                log('已用本地会话自动登录 ✓')
            else:
                log('本地没有可用登录态，请先登录。', 'err')
                raise NotLoggedIn('未登录')
        _quiet_win(page, cfg, headless)
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


def answer_score_items(cfg, items, submit=True, progress=None, control=None,
                       headless=None) -> dict:
    """对「查成绩」面板里的行按 kid 直接作答（v6.26 每行的「补做」）。

    items: [{'key': 'cid:clsid', 'kid': ..., 'course': ..., 'node': ...}]。
           key 由 course_key() 生成，cid:clsid 拆得回来；拆不出来的行跳过。

    为什么要单独一条入口：查成绩面板里的行是「扫出来的节点」，用户对着它点
    「补做」时不该再回主清单里逐个勾——那正是 v6.25「只能查出来、没法做」
    的体验。判别 / 作答 / 交卷全部复用 answer_one，行为与「做作业」一致。

    只答**确实没做过**的行是调用方的责任（界面上按钮只对 pending/fakeDone
    的行出现）；这里仍按 rec 判断一次，已批阅的在 answer_one 里会走 already。
    """
    progress = progress or (lambda m, *a: log(m, a[0] if a else 'info'))
    control = control or (lambda: 'run')
    cfg = cfg or {}
    t0 = time.time()
    tgt = [it for it in (items or [])
           if isinstance(it, dict) and it.get('kid') and it.get('key')]
    out = {'total': len(tgt), 'submitted': 0, 'unverified': 0, 'saved': 0,
           'skipped': 0, 'fail': 0, 'stopped': False, 'items': []}
    if not tgt:
        raise RuntimeError('没有可补做的作业（缺少课程/节点定位信息）')
    ctx = launch(cfg, headless=headless)
    try:
        page = ctx.new_page()
    except Exception:
        close_ctx(ctx)
        raise
    try:
        banner('检查登录状态')
        if not check_login(page):
            if restore_state(ctx) and check_login(page):
                log('已用本地会话自动登录 ✓')
            else:
                raise NotLoggedIn('未登录')
        _quiet_win(page, cfg, headless)
        courses, cpi = collect_courses(page, cfg)
        by_key = {course_key(c): c for c in courses}
        banner('补做 %d 份待完成 / 假完成的测验（%s）'
               % (len(tgt), '答完交卷' if submit else '答完暂存'))
        for i, it in enumerate(tgt, 1):
            if _wait_control(control, progress) == 'stop':
                out['stopped'] = True
                log('已停止。', 'warn')
                break
            key = str(it.get('key') or '')
            cid, _, clsid = key.partition(':')
            c = by_key.get(key) or {'cid': cid, 'clsid': clsid,
                                    'name': it.get('course') or key}
            nm = it.get('node') or ('节点 %s' % it['kid'])
            progress('【%d/%d】%s ｜ %s'
                     % (i, len(tgt), cut(c.get('name') or '', 18), cut(nm, 26)))
            try:
                r = answer_one(page, c, cpi, str(it['kid']), cfg, submit,
                               progress)
            except Exception as e:
                progress('    这份出错了：%s' % str(e)[:120], 'warn')
                r = 'failed'
            if r in ('submitted', 'already'):
                out['submitted'] += 1
            elif r == 'unverified':
                out['unverified'] += 1
            elif r == 'ok':
                out['saved'] += 1
            elif r in ('nohw', 'norender', 'unsupported', 'unsolved', 'report'):
                out['skipped'] += 1
            else:
                out['fail'] += 1
            out['items'].append({
                'key': key, 'kid': it['kid'],
                'course': it.get('course') or c.get('name'),
                'node': nm, 'status': r})
            time.sleep(max(float(cfg.get('course_delay', 3.0)), 1.0))
        progress('补做结束：交卷 %d，未确认 %d，已暂存 %d，需人工/跳过 %d，'
                 '失败 %d，用时 %.0f 分钟。'
                 % (out['submitted'], out['unverified'], out['saved'],
                    out['skipped'], out['fail'], (time.time() - t0) / 60))
    finally:
        try:
            save_state(ctx)
        except Exception:
            pass
        close_ctx(ctx)
    return out


def _supplement_wanted(nodes, pd, want, progress=None):
    """把用户明确勾选、但不在待完成清单里的 kid 补回节点列表。

    v6.25。背景：平台的章节目录会把「测验卡其实是空的」节点标成已完成
    （真机 2026-10-18 大学物理 9/25 个这样），这类节点不在 pending 里。
    用户是**看着清单勾的**，勾了就该照做——打开后真是做完的，answer_one
    会走 'already' 并把平台正确答案回灌缓存；真是空的就直接答。
    """
    have = {n.get('kid') for n in nodes}
    miss = [k for k in want if k not in have]
    if not miss:
        return nodes
    meta = {n.get('kid'): n for n in (pd.get('all') or []) if n.get('kid')}
    extra = []
    for kid in miss:
        m = meta.get(kid)
        # 章节页的 all 清单里没有这个 kid（页面改版/课程变动）也照做：
        # 用户勾的是 ID，平台按 ID 能打开就行。
        extra.append({'name': (m or {}).get('name') or ('节点 %s' % kid),
                      'kid': kid, 'count': 1, 'fakeDone': True})
    if progress:
        progress('  勾选的 %d 个节点不在待完成清单里（平台标了已完成），'
                 '仍按你的勾选打开确认…' % len(extra))
    return nodes + extra


def answer_courses(cfg, only, submit=False, progress=None, control=None,
                   headless=None) -> dict:
    """按课程自动做章节任务点里的作业。

    only: 课程 key（cid:clsid）与「key|kid」章节规格混用的列表（界面勾选），
          必填。key = 该课全部待完成节点；key|kid = 只做这个章节节点。
    submit: True = 答完直接交卷；False = 平台暂存不交卷（界面「做作业并
            暂存」就是这个模式，见 answer_one 的说明）；'dry' = 预演：
            不落库，收集 items 清单由用户复核交卷。
    """
    progress = progress or (lambda m, *a: log(m, a[0] if a else 'info'))
    control = control or (lambda: 'run')
    t0 = time.time()
    ctx = launch(cfg, headless=headless)
    try:
        page = ctx.new_page()
    except Exception:
        close_ctx(ctx)     # new_page 失败也要收浏览器，别把 profile 占死
        raise
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
        _quiet_win(page, cfg, headless)
        courses, cpi = collect_courses(page, cfg)
        keep, node_keep = split_only(only)
        picked = [c for c in courses if course_key(c) in keep]
        if not picked:
            raise RuntimeError('要做的课程没匹配到（课程可能有变动），'
                               '请重新「获取课程列表」后再试')
        progress('共 %d 门课要做作业：%s' % (
            len(picked), '、'.join(cut(c['name'], 16) for c in picked)))
        banner('逐门课做章节作业（共 %d 门，%s）'
               % (len(picked), {True: '答完交卷', False: '答完暂存（不交卷）',
                                'dry': '预演不落库'}.get(submit, str(submit))))
        if submit is False:
            log('⚠ 章节作业的暂存一旦被平台接受，这个任务点就会立即标记'
                '完成（成绩要等正式交卷才有）。')
        for i, c in enumerate(picked, 1):
            if _wait_control(control, progress) == 'stop':
                out['stopped'] = True
                log('已停止。', 'warn')
                break
            progress('【%d/%d】%s' % (i, len(picked), c['name']))
            try:
                pd = fetch_progress_detail(page, c, cpi, cfg)
            except Exception as e:
                # 单门课的页面故障（网络抖动 / goto 超时）不该炸掉整批
                # 任务：scan() 对同一调用就是这么兜的。降级成 page_failed
                # 走下面已有的「这门课先跳过」分支。
                pd = {'page_failed': True,
                      'reason': str(e).splitlines()[0][:120]}
            if pd.get('page_failed'):
                progress('  ⚠ 页面没加载成功（%s），这门课先跳过。'
                         % pd.get('reason', ''), 'warn')
                out['courses'].append({'name': c['name'], 'note': '页面未加载'})
                continue
            nodes = [n for n in (pd.get('pending') or []) if n.get('kid')]
            want = node_keep.get(course_key(c))
            if want is not None:
                nodes = _supplement_wanted(nodes, pd, want, progress)
                if not nodes:
                    progress('  勾选的章节已经不在待完成清单里（可能已完成）。')
                    continue
            if not nodes:
                # v6.25：整门课模式下列表只来自章节页的 pending。平台上还有
                # 一类节点把任务点标成「已完成」、里面其实是空的（本次报的
                # 问题）。这里不擅自扩展范围——那得逐张卡打开核对，会把时间
                # 翻倍还可能撞限流——但必须让用户知道去哪儿勾。
                progress('  没有待完成的章节节点。'
                         + ('（若这门课有「平台标已完成、实际没做」的测验，'
                            '请展开这一课单独勾选；先「查成绩」可列出它们）'
                            if want is None else ''))
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
                        'url': _cards_url(c, cpi, node['kid']),
                        'answers': det.get('answers', {}) if det else {},
                        'stems': det.get('stems', {}) if det else {},
                        'total': det.get('total', 0) if det else 0,
                        'miss': det.get('miss', []) if det else [],
                        'status': ('ready' if r == 'dry' else
                                   {'report': 'report', 'unsupported': 'unsupported',
                                    'unsolved': 'unsolved'}.get(r, 'failed')),
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
                        'url': _cards_url(c, cpi, node['kid']),
                        'answers': {}, 'stems': {}, 'total': 0, 'miss': [],
                        'status': {'submitted': 'done', 'already': 'already',
                                   'unverified': 'unverified'}.get(r, r),
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
                        'url': _cards_url(c, cpi, node['kid']),
                        'answers': {}, 'stems': {}, 'total': 0, 'miss': [],
                        'status': {'ok': 'ok', 'partial': 'partial',
                                   'already': 'already'}.get(r, r),
                    })
                if r == 'submitted' or (r == 'already' and submit is True):
                    su += 1
                elif r == 'already':
                    sk += 1    # 预演/暂存模式遇到已批阅的：没东西可做，跳过
                elif r == 'unverified' and submit is True:
                    uw += 1    # 请求发出但没核实到结果，单独记账不混进失败
                elif r == 'ok':
                    s += 1
                elif r == 'partial':
                    s += 1     # 已落库的部分作答，算进「已保存」一路
                    pt += 1    # 但另记一笔：这几份还得人工补完才能交
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
        if out.get('partial'):
            progress('⚠ 有 %d 份只暂存了一部分（有几道题模型没答上、或者答案'
                     '没写进答题框）：答案已经存在平台上了，打开作业网址补完'
                     '剩下的题再点提交即可，不用重答一遍。' % out['partial'],
                     'warn')
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
    try:
        page = ctx.new_page()
    except Exception:
        close_ctx(ctx)     # new_page 失败也要收浏览器，别把 profile 占死
        raise
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
        _quiet_win(page, cfg, headless)
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
    活动行（kind='act'，kid=activeId、带 cid/clsid）走答题页 JS_QUIZ_SETTLE
    判「结果页=已提交」，其余按作业卡核。
    返回 {'confirmed': n, 'still': n, 'stopped': bool,
          'results': [{'key','kid','status'}]}   status ∈ already/still
    """
    progress = progress or (lambda m, *a: log(m, a[0] if a else 'info'))
    control = control or (lambda: 'run')
    t0 = time.time()
    ctx = launch(cfg, headless=headless)
    try:
        page = ctx.new_page()
    except Exception:
        close_ctx(ctx)     # new_page 失败也要收浏览器，别把 profile 占死
        raise
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
        _quiet_win(page, cfg, headless)
        fid = get_fid(ctx)      # 活动答题页 URL 要用（作业卡路径用不到）
        # 初始就是 still：核实不中的项最后必须计入「仍未确认」。
        # 旧初始值 'unverified' 不在 already/still 统计里 → 出现过
        # 「确认已交 0 份，仍未确认 0 份」的矛盾账（22:23 实测踩过）
        st = {(it.get('key'), it.get('kid')): 'still' for it in items}
        # 活动行不依赖课程卡，单独先核掉（答题页 SETTLE 判「结果页=已交」）；
        # 不混进作业轮次——那会被拿 activeId 当作业 knowledgeid 去查作业卡，
        # 永远核不到还白占轮次。
        act_items = [it for it in items
                     if it.get('kind') == 'act' and it.get('cid') and it.get('clsid')]
        hw_items = [it for it in items
                    if not (it.get('kind') == 'act' and it.get('cid') and it.get('clsid'))]
        pending = list(hw_items)
        act_done = []
        if act_items:
            progress('先核 %d 条课堂活动（打开答题页看是否已是结果页）…' % len(act_items))
            for i, it in enumerate(act_items, 1):
                if _wait_control(control, progress) == 'stop':
                    out['stopped'] = True
                    break
                progress('  【%d/%d】核实活动：%s'
                         % (i, len(act_items), cut(it.get('node', ''), 30)))
                url = QUIZ_ANSWER_URL.format(aid=it.get('kid'), clsid=it['clsid'],
                                             fid=fid, cid=it['cid'])
                try:
                    page.goto(url, wait_until='domcontentloaded', timeout=30000)
                    got = _poll(page, JS_QUIZ_SETTLE, lambda v: bool(v), 16000, 300) or ''
                except Exception as e:
                    got = ''
                    progress('  ⚠ 打开活动页失败：%s' % str(e)[:80], 'warn')
                if got == 'result':
                    progress('  ✓ 平台显示已提交——这条确实交上了。')
                    st[(it.get('key'), it.get('kid'))] = 'already'
                    act_done.append(it)
                else:
                    progress('  … 活动还没显示已提交，下一轮再看。')
            # 活动行不在 pending（=hw_items）里，无需 remove；
            # 没核掉的活动行保持初始 'still'，最后如实计「仍未确认」。
            out['confirmed'] += len(act_done)
        courses, cpi = collect_courses(page, cfg)
        by_key = {course_key(c): c for c in courses}
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
                # 核实也要扫全部选项卡：交上去的那份测验可能不在第一格
                rurl = _cards_url(c, cpi, it.get('kid'), 0)
                try:
                    page.goto(rurl, wait_until='domcontentloaded')
                    page.wait_for_timeout(900)
                    rtabs = _cards_tabs(page)
                except Exception:
                    rtabs = []
                _, note = _wait_hw_iframe(
                    page, rurl, log,
                    tabs=(_tab_order(rtabs) if rtabs
                          else list(range(MAX_CARDS_TABS))))
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


def _scan_leftover_tasks(page):
    """扫当前 cards 页里「刷课刷不动的非视频任务点」。

    一个章节节点是一叠 `.ans-attach-ct`：带 `.ans-job-icon` 的才是任务点，
    完成态看该图标的 aria-label（含「已完成」）或卡片 class 含 ans-job-finished。
    刷课只负责视频；这里把刷完后**仍未完成**的非视频任务点按种类计数返回，
    好在日志里把「刷完视频为啥节点还显示未完成」说清楚（v6.19）。
    返回 {'测验/作业': n, '文档': n} 之类；空 dict = 没有残留。失败也返回 {}。
    """
    try:
        return page.main_frame.evaluate(JS_LEFTOVER_TASKS) or {}
    except Exception:
        return {}


JS_LEFTOVER_TASKS = r"""
() => {
  const out = {};
  document.querySelectorAll('.ans-attach-ct').forEach(ct => {
    const job = ct.querySelector('.ans-job-icon');
    if (!job) return;                                  // 非任务点卡，不算账
    const lbl = job.getAttribute('aria-label') || '';
    const cls = (ct.className || '').toString();
    if (/已完成/.test(lbl) || /ans-job-finished/.test(cls)) return;  // 已完成
    const ifr = ct.querySelector('iframe');
    const src = ifr ? (ifr.src || '') : '';
    // 视频是刷课自己的活（刷不动有 fail 计数兜底），不算「残余」——
    // 且本函数紧跟在刷完视频之后跑，页面 DOM 未必已刷新成「已完成」，
    // 数它会把刚刷的视频误报成「刷课管不了」。只数非视频任务点。
    if (/modules\/video/.test(src)) return;
    let kind = '其他任务点';
    if (/modules\/work/.test(src)) kind = '测验/作业';
    else if (/modules\/pdf|insertdoc|modules\/doc/.test(src)) kind = '文档';
    else if (/modules\/audio/.test(src)) kind = '音频';
    out[kind] = (out[kind] || 0) + 1;
  });
  return out;
}
"""


def brush_node(page, c, cpi, kid, cfg, progress, control):
    """刷一个章节节点里**未完成**的视频卡；再扫出本节点残余的非视频任务点。

    返回 (完成数, 未完成数, 是否被停止, leftover)：
      leftover = 本节点刷完后仍「未完成」的非视频任务点种类计数，
                 形如 {'测验/作业': 2, '文档': 1}（空 dict = 没有残留）。
                 刷课只负责视频；测验/文档要用户自己用「刷课+刷题」或手动完成。
                 在日志里如实回报，免得用户以为工具坏了、反复重跑（v6.19）。
    """
    # 视频也**不一定在 num=0**：章节页是「视频与讲义 / 讨论 / 测试」多格
    # 选项卡，实测有的节点视频挂在后面几格。先读出选项卡列表，按顺序逐格
    # 刷；每格都是「扫不到新视频卡」才换下一格。
    url = _cards_url(c, cpi, kid, 0)
    try:
        page.goto(url, wait_until='domcontentloaded')
    except Exception as e:
        log('    打开章节页失败：%s' % str(e)[:80], 'warn')
        return 0, 1, False, {}
    try:
        page.wait_for_timeout(1200)
        _tabs = _cards_tabs(page)
    except Exception:
        _tabs = []
    # 顶层打开 cards 页时只能看到「当前这一格」（外层 prev_tab 不在），
    # 所以数量不能只用来决定扫几格——视频可能在任何一格，一直往后扫，
    # 遇到不存在的格（AttachmentSetting 没了）就停。
    _nums = [0] + [t['num'] for t in _tabs if t['num'] != 0]
    _nums += [n for n in range(MAX_CARDS_TABS) if n not in _nums]
    if len(_tabs) > 1:
        log('    本章有 %d 个选项卡（%s），逐格找视频卡…'
            % (len(_tabs),
               ' / '.join('%d.%s' % (t['num'], t['title'] or '?') for t in _tabs)))
    done = fail = 0
    leftover = {}
    for _num in _nums:
        if _num != 0:
            try:
                page.goto(_cards_url(c, cpi, kid, _num),
                          wait_until='domcontentloaded')
                page.wait_for_timeout(400)
            except Exception:
                continue
            if not _tab_exists(page):
                break          # 这一格不存在 → 选项卡到头了
        # 一个节点挂好几张卡（视频/文档/测验混排），视频卡是懒加载的：
        # 刷完一张再扫一遍 frame，扫不到新的才算这一格完事
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
                    return done, fail, True, {}
                progress('    ⚠ 上一次%s（第 %d 次重试）…'
                         % ('卡住' if r == 'stuck' else '超时', attempt), 'warn')
                time.sleep(random.uniform(4.0, 8.0))
                try:
                    vf.evaluate("() => { window.__cx_brushed = false; }")
                except Exception:
                    pass
                r = brush_video_frame(vf, cfg, progress, control)
                if r == 'stopped':
                    return done, fail, True, {}
            if r == 'done':
                done += 1
                log('    ✓ 这个视频刷完了')
            elif r in ('stuck', 'timeout'):
                fail += 1
            # 'novideo'：这卡是文档/音频之类，不算账
        # 残余任务点按选项卡分别统计再取最大值：每个选项卡是一组独立的卡，
        # 只有当前页的 DOM 可见，扫完就没了（v6.21）。
        try:
            _lf = _scan_leftover_tasks(page) or {}
        except Exception:
            _lf = {}
        for _k, _v in _lf.items():
            leftover[_k] = max(leftover.get(_k, 0), _v)
    if leftover:
        parts = '、'.join('%s×%d' % (k, v) for k, v in leftover.items())
        progress('    这个节点还剩 %s 未完成 —— 视频之外的，刷课只管视频；'
                 '用「刷课+刷题」或手动完成。' % parts, 'warn')
    elif done == 0 and fail == 0:
        # 这个节点一个视频都没刷到，两种常态：①视频都已完成（正常跳过）；
        # ②它的任务点是卡片图标没标出的类型——有的「章节测验」卡不带
        # .ans-job-icon，_scan_leftover_tasks 也认不出来，于是既没得刷、
        # 又报不出残余，日志一片空白。无论哪种，它仍然「未完成」，必须
        # 如实说明；否则用户只看到「完成 0 个」就重跑刷课，白白重播
        # 已完成的视频，卡在死循环里（v6.20）。
        progress('    这个节点没有可刷的视频（视频要么已完成、要么本来就不是'
                 '视频任务点）——它仍未完成，多半是章节测验/作业，请用'
                 '「刷课+刷题」或手动完成。', 'warn')
    return done, fail, False, leftover


def _wait_control(control, progress):
    """在课程/节点边界处理暂停。返回 'run' 或 'stop'。"""
    while True:
        act = control()
        if act != 'pause':
            return act
        time.sleep(0.3)


def _leftover_note(out, progress):
    """把累计的「刷课刷不动的非视频任务点」在收尾时如实汇报一次（v6.19）。

    这就是「章节刷完视频仍显示未完成、重跑只会重播已完成视频」的成因；
    在刷课收尾处点名，用户才知道该去用「刷课+刷题」或手动补。
    """
    lo = out.get('leftover') or {}
    if not lo:
        return
    parts = '、'.join('%s %d 个' % (k, v) for k, v in lo.items())
    progress('⚠ 另有 %s 任务点不是视频，刷课管不了（要用「刷课+刷题」或手动完成）'
             '——这就是有些章节刷完视频仍显示「未完成」的原因，重跑刷课只会'
             '重播已完成的视频。' % parts, 'warn')


def brush_videos(cfg, only, progress=None, control=None, headless=None) -> dict:
    """按课程自动刷未完成的「任务点视频」。

    only: 课程 key（cid:clsid）与「key|kid」章节规格混用的列表（界面勾选），
          必填——不提供「全部课程都刷」，防止误触发把几十门课全部挂后台。
          key = 该课全部待完成节点；key|kid = 只刷这个章节节点。
    progress(msg): 进度回调（界面把它写进日志和状态行）。
    control(): 'run' | 'pause' | 'stop'，界面注入；CLI 用默认的「一直 run」。
    返回 {'courses': [...], 'done': n, 'fail': n, 'stopped': bool}
    """
    progress = progress or (lambda m, *a: log(m, a[0] if a else 'info'))
    control = control or (lambda: 'run')
    t0 = time.time()
    ctx = launch(cfg, headless=headless)
    try:
        page = ctx.new_page()
    except Exception:
        close_ctx(ctx)     # new_page 失败也要收浏览器，别把 profile 占死
        raise
    out = {'courses': [], 'done': 0, 'fail': 0, 'stopped': False,
           'leftover': {}}
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
        # 登录确认后再收窗口：登录/验证码阶段必须可见，刷课阶段不需要。
        # 桌面模式例外：窗口已屏外待坞/已入坞，最小化会把预览藏掉。
        if (cfg.get('brush_minimize', True) and not (headless is True)
                and not OFFSCREEN_LAUNCH):
            if minimize_window(page):
                progress('浏览器已最小化到任务栏（后台刷课不影响进度，'
                         '点任务栏图标可随时查看）。')
        for i, c in enumerate(picked, 1):
            if _wait_control(control, progress) == 'stop':
                out['stopped'] = True
                log('已停止。', 'warn')
                break
            progress('【%d/%d】%s' % (i, len(picked), c['name']))
            try:
                pd = fetch_progress_detail(page, c, cpi, cfg)
            except Exception as e:
                # 单门课的页面故障（网络抖动 / goto 超时）不该炸掉整批
                # 任务：scan() 对同一调用就是这么兜的。降级成 page_failed
                # 走下面已有的「这门课先跳过」分支。
                pd = {'page_failed': True,
                      'reason': str(e).splitlines()[0][:120]}
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
                d, f, stopped_here, lf = brush_node(page, c, cpi, node['kid'],
                                                    cfg, progress, control)
                cd += d
                cf += f
                for _k, _v in (lf or {}).items():
                    out['leftover'][_k] = out['leftover'].get(_k, 0) + _v
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
        _leftover_note(out, progress)
        if out['fail']:
            progress('没刷完的多半是「反复暂停卡住」的插问视频或超长视频，'
                     '这些需要你手动看一遍。')
        if not out['stopped']:
            progress('建议重新点「一键查询」核对最新状态——报告里消失的节点就是刷完的。')
    finally:
        save_state(ctx)
        close_ctx(ctx)
    return out


# ================================================================ 课堂活动自动作答（v6.8，v6.9 修类型分流）
# 「进行中的课堂活动」跟作业是**两套答题面**：
#   作业  = mooc2/work 的 doHomeWorkNew 表单（.questionLi / answertype / btnBlueSubmit）
#   活动  = mobilelearn 的 quiz 页（.question-item / input.que-type / submitVote）
# 所以填答、提交都得各写一套；但「题目怎么喂模型、怎么截公式图、怎么落答案」这层
# 完全复用作业那套（llm_solve / _attach_shots / _fallback_by_position …）。
#
# 真机逆向（2026-10-14，用户账号）：
#   作答面 = /page/quiz/stu/answerQuestion?activeId=..&classId=..&fid=..&courseId=..
#            &returnType=0&viewType=2&timetable=0
#     · 学生**已提交**时会 JS 跳到「答题结果」页 quizStudentQuestion——据此判 already。
#     · 每题一个 .question-item；题型在 input.que-type 的 value 里。
#   提交 = 页面函数 submitVote()  → POST /v2/apis/studentQuestion/doQuestion
#   暂存 = 页面函数 saveStuAnswerTemp() → POST /v2/apis/studentQuestion/saveStuAnswerTemp
#          两者共用 #answer_form 序列化。调页面自己的函数 = 等价人工点，最稳。
#   ⚠ 这套答题面**只有 42 测验 / 14 问卷**用（见 ACT_ANSWERABLE_TYPES + activity_open_url）。
#     主题讨论(5)/AI实践(77)/签到(2)… 平台跳的是完全不同的页面；拿这套硬套它们，
#     平台回「非法请求」——v6.8 的「去完成」就是栽在这（v6.9 已按类型分流修掉）。
QUIZ_ANSWER_URL = ('https://mobilelearn.chaoxing.com/page/quiz/stu/answerQuestion'
                   '?activeId={aid}&classId={clsid}&fid={fid}&courseId={cid}'
                   '&returnType=0&viewType=2&timetable=0')
QUIZ_SUBMIT_API = '/v2/apis/studentQuestion/doQuestion'
QUIZ_SAVE_API = '/v2/apis/studentQuestion/saveStuAnswerTemp'

# 只有「随堂练习/测验」(42) 和「问卷」(14) 走 answerQuestion 那套答题面
# （.question-item / submitVote / saveStuAnswerTemp）。**这两种才让大模型作答**。
# 其余类型（主题讨论 5 / AI实践 77 / 签到 2 …）平台跳的是**完全不同**的页面，
# 大模型替不了——以前拿 answerQuestion 硬套 5/77，平台直接回「非法请求」，
# 用户点「去完成」就进不去（2026-10-14 真机复现）。所以：
#   · 「去完成」按类型给对的页面（见 activity_open_url）；
#   · 「作答」只对 42/14 出现，其余类型不给按钮，不做假的自动作答。
ACT_ANSWERABLE_TYPES = frozenset({'42', '14'})
_ML_BASE = 'https://mobilelearn.chaoxing.com'


def activity_open_url(active_type, aid, cid, clsid, fid, raw=None) -> str:
    """按活动类型拼「去完成」要打开的页面。

    平台任务页 activeRowClick 每种类型跳的页面都不一样（2026-10-14 真机核对）：
      2 签到   signIn | 4 抢答 stuAnswer | 5 主题讨论 BBS帖 | 14 问卷 answerQuestion
      42 测验 answerQuestion | 43 投票 answerQvote | 77 AI实践 content.stuUrl
    拼不出就返回 ''（界面据此不渲染「去完成」，跟未完成作业没链接时一个道理）。
    """
    raw = raw or {}
    at = str(active_type)
    q = ('activeId=%s&classId=%s&fid=%s&courseId=%s'
         % (aid, clsid, fid, cid))
    if at in ACT_ANSWERABLE_TYPES:
        return QUIZ_ANSWER_URL.format(aid=aid, clsid=clsid, fid=fid, cid=cid)
    if at == '77':            # AI实践：学生端链接在 content.stuUrl 里（泛雅评测页）
        try:
            cj = json.loads(raw.get('content') or '{}')
            return str(cj.get('stuUrl') or '')
        except Exception:
            return ''
    if at == '5':             # 主题讨论：BBS 帖（v3），extraInfo.openTopic 里有 bbsid+uuid
        try:
            ot = (raw.get('extraInfo') or {}).get('openTopic') or {}
            bbs = ot.get('Groupbbsid') or (ot.get('classInfo') or {}).get('classBbsid')
            uuid = ot.get('uuid')
            if bbs and uuid:
                return ('https://groupweb.chaoxing.com/course/topic/v3/bbs/%s/%s'
                        '/replysList?courseId=%s&classId=%s'
                        % (bbs, uuid, cid, clsid))
        except Exception:
            pass
        return _ML_BASE + '/page/topicDiscuss/student/stu_reply_discuss?' + q
    if at == '2':
        return _ML_BASE + '/page/sign/signIn?' + q
    if at == '4':
        return _ML_BASE + '/page/answer/stuAnswer?' + q
    if at == '43':
        return _ML_BASE + '/page/qvote/stu/answerQvote?' + q
    return ''

# quiz 页题型码 → 作业那套 atype（ATYPES / STYPES 的键）。归一后 llm_solve、
# _parse_llm_answers、_fallback_by_position 全都能直接吃。
#   0单选 1多选 16判断 | 2/9/10填空(分录/资料归填空) | 4/5/6/7/18简答 | 11连线
QUIZ_ATYPES = {
    '0': '0', '1': '1', '16': '3', '3': '3',
    '2': '2', '9': '2', '10': '2',
    '4': '4', '5': '4', '6': '4', '7': '4', '18': '4',
}

# 判断页面处于「答题面」还是「结果页」（已提交）。已提交的随堂练习会先渲染
# 答题面、再由 JS 跳结果页，所以「题 + 结果标志同在」一律当结果页处理。
JS_QUIZ_SETTLE = r"""
() => {
  const result = !!document.querySelector(
    '.aggregateScore, .answer-result, .quiz-result, .result-wrap, .statistics');
  const t = (document.title || '') + ' ' +
            (document.body ? (document.body.innerText || '').slice(0, 400) : '');
  const titled = /答题结果|我的作答|本次成绩|已提交/.test(t);
  const nq = document.querySelectorAll('.question-item').length;
  if ((result || titled) && nq) return 'result';
  if (nq) return 'quiz';
  if (result || titled) return 'result';
  return '';
}
"""

# 读随堂练习题目 → 产出与 JS_PARSE_TIMUS **同构**的 timus（好复用 llm_solve /
# _attach_shots）。每题容器打 data-cx-idx 供 _shot_timu 定位截图。
JS_PARSE_QUIZ = r"""
() => {
  const ATTACH = /上传附件|提交附件|附件上传|附件提交|以附件|附件形式|拍照上传|手写|纸上书写|纸上作答|在纸上|书写后拍照|拍照.{0,8}(发送|回传)|上传.{0,8}(报告|文件|文档|图片|视频|音频|压缩包|作品|附件)|提交.{0,8}(报告|文件|文档|压缩包|作品|附件)|(报告|文件|作品|附件).{0,6}(上传|提交)/;
  const out = [];
  const boxes = [...document.querySelectorAll('.question-item')];
  boxes.forEach((qi, idx) => {
    const tEl = qi.querySelector('.que-type');
    const qtype = tEl ? String(tEl.value || '') : '';
    // 题干：优先常见题干容器；取不到就克隆整题、剔掉编辑器/输入控件后的文本
    let stem = '';
    const cand = qi.querySelector('.mark_name, .question-content, .topic-title,'
      + ' .timu-txt, .questionCon, .Zy_TItle');
    if (cand) stem = cand.textContent || '';
    if (!stem.trim()) {
      const clone = qi.cloneNode(true);
      clone.querySelectorAll('script, style, textarea, input, .edui-default,'
        + ' .edui-editor, .eui-editor').forEach(x => x.remove());
      stem = clone.textContent || '';
    }
    stem = stem.replace(/\s+/g, ' ').trim().slice(0, 500);
    const opts = [], fills = [];
    qi.querySelectorAll('input[name^=answer]').forEach(x => {
      if (x.type === 'hidden') return;
      if (x.type === 'radio' || x.type === 'checkbox') {
        const lab = x.closest('.optionRadio') || x.closest('li') || x.parentElement;
        const _im = lab ? lab.querySelector('img') : null;
        opts.push({name: x.name, val: x.value, type: x.type,
                   label: ((lab && lab.textContent) || x.value)
                          .replace(/\s+/g, ' ').trim().slice(0, 150),
                   img: _im ? ((_im.getAttribute('src') || '').split('/').pop()
                               .split('?')[0]) : ''});
      } else if (x.type === 'text' || x.type === '') {
        if (x.name) fills.push({name: x.name});
      }
    });
    qi.querySelectorAll('textarea').forEach(x => {
      if (x.name) fills.push({name: x.name, tag: 'textarea'});
    });
    // 显示字母（与 JS_PARSE_TIMUS 同一口径，v6.23）：平台可能随机打乱选项
    // 顺序，页面按显示位置标 A/B/C/D，而 value 是打乱前的字母。模型答的是
    // 它看到的显示字母，落答必须按这个来，否则整题点到别的选项上。
    opts.forEach((o, i) => {
      o.pos = i;
      const m = String(o.label || '').trim()
        .match(/^[（(]?\s*([A-Za-z])\s*[)）.．、:：\s]/);
      o.dl = (m ? m[1] : 'ABCDEFGHIJKLMNOP'.charAt(i)).toUpperCase();
    });
    const hasMedia = !!qi.querySelector(
      'img, svg, math, .MathJax, .MathJax_Preview, [class*=formula], [class*=math], canvas');
    const hasUploader = !!qi.querySelector('input[type=file]');
    const upload = hasUploader || ATTACH.test(stem)
      || ATTACH.test(qi.outerHTML.replace(/\s+/g, ' ').slice(0, 6000));
    qi.setAttribute('data-cx-idx', idx);
    out.push({no: idx + 1, stem: stem.slice(0, 500), atype: qtype,
              opts, fills, upload, hasMedia});
  });
  return out;
}
"""

# 把答案写进随堂练习页。客观题点选 radio/checkbox；主观题写 textarea/填空 input。
# ⚠ 主观题必须用**原生 value setter + input/change 事件**：Vue 的 v-model 不认
#   直接 el.value = x 的赋值（拿不到 input 事件就不会同步），手工赋值等于没填。
JS_FILL_QUIZ = r"""
(items) => {
  const setNative = (el, val) => {
    const proto = (el.tagName === 'TEXTAREA')
      ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    try {
      const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
      setter.call(el, val);
    } catch (e) { el.value = val; }
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  };
  const escHtml = (s) => String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  let ok = 0, miss = 0;
  // 多空填空：同一题的 N 个空是**同名** input/textarea（出现顺序=空序）。
  // 不能用 querySelector（永远拿第一个）——Python 按空序发 N 条同名 item，
  // 必须按 name 建「元素池」，每条 item 消费下一个空，顺序才对得上。
  const pools = {};
  const poolFor = (name) => {
    if (!pools[name]) {
      const tas = [...document.querySelectorAll('textarea[name="' + name + '"]')];
      const ins = [...document.querySelectorAll('input[name="' + name + '"]')]
        .filter(x => x.type === 'text' || x.type === '');
      pools[name] = tas.length ? tas : ins;
    }
    return pools[name];
  };
  items.forEach(a => {
    if (a.vals) {
      const els = [...document.querySelectorAll('input[name="' + a.name + '"]')]
        .filter(x => x.type === 'radio' || x.type === 'checkbox');
      const want = new Set(a.vals.map(String));
      els.forEach(el => {
        // checkbox 是开关：页面可能带着上次的旧勾（暂存草稿恢复）。
        // 按终态校准——该勾没勾→点上；不目标却勾着→点掉。radio 天然互斥
        // 点一次即可，这样统一处理不会把已对的勾点反。
        const isOn = !!el.checked;
        const should = want.has(String(el.value));
        if (isOn !== should) { el.click(); ok++; }
      });
      miss += Math.max(0, els.length ? [...want].filter(
        v => !els.some(el => String(el.value) === v)).length : 0);
    } else if (a.val !== undefined) {
      const pool = poolFor(a.name);
      const ta = pool.length ? pool.shift() : null;
      if (!ta) { miss++; return; }
      // 简答题正文是 **UEditor 富文本编辑器**，textarea 只是壳——直接写
      // value 平台 serialize 读不到（=用户看到的「有答案却填不上去、暂存存不上」）。
      // 必须优先走 UE API（UE.instants 的 key = textarea 的 name），与作业侧
      // JS_FILL_TEXT 同一套判据。
      const wrap = ta.closest('.question-item, .TiMu') || document;
      const edFrame = wrap.querySelector(
        '.edui-editor iframe, .edui-editor-body iframe, .eui-editor iframe');
      let done = false;
      try {
        if (window.UE && UE.instants) {
          for (const k in UE.instants) {
            const e = UE.instants[k];
            if ((e.key || k) === a.name) {
              e.setContent('<p>' + escHtml(a.val).replace(/\n/g, '<br>') + '</p>');
              done = !!e.getContent();
              break;
            }
          }
        }
      } catch (e) { /* 编辑器未就绪 → 走下面的兜底 */ }
      if (done) { ok++; return; }
      // 兜底：同容器编辑器 iframe 正文直写（UE 实例拿不到时）。有编辑器壳
      // 却没能写进去 → 计 miss，交卷/暂存侧的安全阀会提醒「文本没写进页面」，
      // 绝不把空简答当成功悄悄存下去。
      if (edFrame) {
        try {
          if (edFrame.contentDocument && edFrame.contentDocument.body) {
            edFrame.contentDocument.body.innerHTML = escHtml(a.val)
              .replace(/\n/g, '<br>');
            ok++; return;
          }
        } catch (e) { /* 跨域/未就绪则放弃 */ }
        miss++; return;
      }
      // 纯 textarea / input[type=text]（普通填空、无编辑器）：原生 setter 有效
      setNative(ta, a.val);
      ok++;
    }
  });
  return {ok, miss};
}
"""

# 调页面自带的提交 / 暂存函数（等价人工点那两个按钮）。
JS_QUIZ_SUBMIT = r"""
() => {
  if (typeof submitVote !== 'function') return 'nofn';
  try { submitVote(); return 'ok'; }
  catch (e) { return 'err:' + String(e).slice(0, 80); }
}
"""
JS_QUIZ_SAVE = r"""
() => {
  if (typeof saveStuAnswerTemp !== 'function') return 'nofn';
  try { saveStuAnswerTemp(); return 'ok'; }
  catch (e) { return 'err:' + String(e).slice(0, 80); }
}
"""


def _answer_one_activity(page, t, cfg, submit, progress, fid):
    """作答一条课堂活动。返回 (状态码, detail)。

    与 answer_one 同口径：附件题跳过、认不出的题型跳过、没答上不提交、
    提交/暂存都监听平台真实请求来核实（不假成功）。
    """
    # 只有 42/14 走 answerQuestion 那套答题面，别的类型大模型替不了——直接报
    # 「需人工」。为防界面漏筛 / 旧参数重放，这里再兜一道（平台对错类型会回
    # 「非法请求」，与其让它当答题面失败，不如如实说这条要本人做）。
    if str(t.get('type')) not in ACT_ANSWERABLE_TYPES:
        log('    这是「%s」，不是随堂练习/测验或问卷，答题面不一样，'
            '大模型替不了，跳过（请点「去完成」自己做）。'
            % (t.get('kind') or ('活动(%s)' % t.get('type'))), 'warn')
        return 'unsupported', {'url': t.get('url') or ''}
    url = QUIZ_ANSWER_URL.format(aid=t['id'], clsid=t['clsid'],
                                 fid=fid, cid=t['cid'])
    page.goto(url, wait_until='domcontentloaded', timeout=30000)
    # 判据用 JS_QUIZ_SETTLE：学生**已提交**时页面会先渲染出答题面、再由 JS 跳
    # 结果页；只看「有没有 .question-item」会把这种瞬间误判成可作答，接着去
    # submitVote() 重交一遍。SETTLE 只认已渲染出的题，且把「题 + 结果标志同在」
    # 视为已作答，避开这个竞态。
    got = _poll(page, JS_QUIZ_SETTLE, lambda v: bool(v), 16000, 300) or ''
    if got == 'result':
        log('    这条此前已作答（打开就是「答题结果」页），跳过。')
        return 'already', {'url': url}
    if got != 'quiz':
        log('    答题页没加载出来（活动可能已结束，或页面结构变了），跳过。',
            'warn')
        return 'norender', {'url': url}
    # 开卷先「读题」停留，跟作业一个道理：开卷即交在时序上太扎眼
    time.sleep(random.uniform(3.0, 8.0))
    timus = page.evaluate(JS_PARSE_QUIZ) or []
    if not timus:
        # 解析时页面可能已经跳到结果页了 —— 再判一次，别当「结构变了」报错
        if page.evaluate(JS_QUIZ_SETTLE) == 'result':
            log('    这条此前已作答，跳过。')
            return 'already', {'url': url}
        log('    答题页打开但没解析到题目（可能结构变了），跳过。', 'warn')
        return 'failed', {'url': url}
    bad = [x['no'] for x in timus
           if QUIZ_ATYPES.get(str(x['atype'])) not in ATYPES
           and QUIZ_ATYPES.get(str(x['atype'])) not in STYPES]
    if bad:
        log('    含 %d 道不支持的题型（第 %s 题），整条跳过。'
            % (len(bad), '、'.join(map(str, bad[:6]))), 'warn')
        return 'unsupported', {'url': url, 'total': len(timus)}
    attach = [x for x in timus if x.get('upload')]
    if attach:
        log('    第 %s 题要求上传附件（报告/文件），大模型替不了，'
            '需要你本人完成，跳过。'
            % '、'.join(str(x['no']) for x in attach[:6]), 'warn')
        return 'report', {'url': url, 'total': len(timus)}
    # 题型码归一成作业那套，再交给复用的 llm_solve
    for x in timus:
        x['atype'] = QUIZ_ATYPES.get(str(x['atype']), str(x['atype']))
    _attach_shots(page, timus, cfg)        # 图片/公式题截图（复用）
    ans = llm_solve(cfg, timus, progress)  # 复用：视觉 + 缓存 + 拒答拦截
    fills, missing = [], []
    op_miss = txt_miss = 0        # 填答后回读：没真正落进页面的个数（≠ 没答上）
    for x in timus:
        a = ans.get(x['no'], '?')
        if not a or a == '?':
            missing.append(x['no'])
            continue
        if x['atype'] in STYPES:            # 主观题：文本
            if not x['fills']:
                missing.append(x['no'])
                continue
            if x['atype'] == '2' and len(x['fills']) > 1:
                parts = _split_fill(a, len(x['fills']))
                if parts is None:
                    log('    第 %d 题多空填空没按空序给（%s），按没把握处理。'
                        % (x['no'], str(a)[:30]), 'warn')
                    missing.append(x['no'])
                    continue
                for f, pv in zip(x['fills'], parts):
                    if f['name']:
                        fills.append({'name': f['name'], 'val': pv,
                                      'no': x['no']})
                continue
            for f in x['fills']:
                if f['name']:
                    fills.append({'name': f['name'], 'val': a, 'no': x['no']})
            continue
        # 客观题：显示字母优先（平台可能随机打乱选项顺序，见 _by_display_letter），
        # 其次按选项值匹配，落不下去再按位置映射。
        if x['atype'] == '3':
            a = '对' if a in ('对', '正确', 'TRUE', 'T', '√') else '错'
            vals = [a]
            if not any(str(o['val']) == a for o in x['opts']):
                vals = ['true' if a == '对' else 'false']
            names, pos_vals = _fallback_by_position(x, vals)
        else:
            # 按字符拆（多选 "AC" → ['A','C']）。先剔掉分隔符/空白（"A、C"、
            # "A C"），再去重保序——checkbox 是开关：重复字母或已勾选项点两次
            # 会把勾取消掉，等于没选/选错位。
            vals = [c for c in str(a) if c not in ' \t、，,；;｜|/']
            vals = list(dict.fromkeys(vals))
            names, pos_vals = _by_display_letter(x, vals)
            if not names:
                names = {o['name'] for o in x['opts']
                         if str(o['val']) in vals}
            if not names:
                up = [str(v).upper() for v in vals]
                names = {o['name'] for o in x['opts']
                         if str(o.get('val', '')).upper() in up}
                pos_vals = None
        if not names:
            log('    第 %d 题模型答了（%s）但选项落不下去（页面里 %d 个选项），'
                '按没答上处理。' % (x['no'], str(a)[:12], len(x['opts'])), 'warn')
            _dump_page_diag(x, 'opts_unmappable')
            missing.append(x['no'])
            continue
        for nm in names:
            fills.append({'name': nm, 'vals': pos_vals or vals, 'no': x['no']})
    detail = {'url': url, 'total': len(timus),
              'answers': {str(k): v for k, v in ans.items() if v not in (None, '?', '')},
              'miss': missing, 'op_miss': op_miss, 'txt_miss': txt_miss}
    if not fills:
        log('    ⚠ 这一整条都没答上（第 %s 题），没有可保存的内容。'
            % '、'.join(map(str, missing[:8])), 'warn')
        return 'unsolved', detail
    # ---- 填答 ----
    op = [{k: v for k, v in f.items() if k != 'no'} for f in fills if 'vals' in f]
    tx = [{k: v for k, v in f.items() if k != 'no'} for f in fills if 'val' in f]
    op_miss = txt_miss = 0
    if op:
        r = page.evaluate(JS_FILL_QUIZ, op)
        op_miss = int(r.get('miss') or 0)
        log('    已填 %d 个选项（未命中 %d）。' % (r.get('ok', 0), op_miss))
    if tx:
        time.sleep(random.uniform(1.0, 3.0))
        r = page.evaluate(JS_FILL_QUIZ, tx)
        txt_miss = int(r.get('miss') or 0)
        log('    已填 %d 处文本（未命中 %d）。' % (r.get('ok', 0), txt_miss))
    # detail 在填答**之前**就建好了（那时 op_miss/txt_miss 还是 0）——int 不可变，
    # 后面重新赋值改不到 detail 里那份，必须回填。不回填的话，交卷面板和
    # answer_activities 的 item 永远显示「0 个选项没点中」，用户看不到是哪一条
    # 有答案没落进页面（2026-10-14 行为测试 test_activity_submit 逮到）。
    detail['op_miss'], detail['txt_miss'] = op_miss, txt_miss
    # ---- 决定这次是「提交」还是「暂存」----
    # 有题没答上、或有答案没真正落进页面（op_miss/txt_miss：平台会读到空），
    # 都**绝不提交**（跟作业一个口径：交上去就是白拿低分）。改存平台草稿，
    # 用户补完再自己提交；已答上的不白做。
    do_submit = submit and not missing and not op_miss and not txt_miss
    if submit and (missing or op_miss or txt_miss):
        why = []
        if missing:
            why.append('第 %s 题没答上' % '、'.join(map(str, missing[:8])))
        if op_miss:
            why.append('%d 个选项没点中' % op_miss)
        if txt_miss:
            why.append('%d 处文本没写进页面' % txt_miss)
        log('    %s，这是「提交」模式也不提交，改存平台草稿，'
            '你补完这些再自己交。' % '；'.join(why), 'warn')
    resp_seen, req_seen, nav_seen = [], [], []
    can = hasattr(page, 'on')

    def _is_api(u):
        return '/' in (u or '') and (QUIZ_SUBMIT_API in (u or '')
                                     or QUIZ_SAVE_API in (u or ''))

    def _on_resp(r):
        if _is_api(r.url):
            try:
                body = (r.text() or '')[:300]
            except Exception:
                body = ''
            resp_seen.append((r.status, body))

    def _on_req(r):
        try:
            if r.method == 'POST' and _is_api(r.url):
                req_seen.append((r.url or '')[-110:])
        except Exception:
            pass

    def _on_nav(f):
        # 提交受理后页面会 location.reload()/跳结果页 —— 导航也是「交上了」的证据
        try:
            if f == page.main_frame:
                nav_seen.append(f.url or '')
        except Exception:
            pass

    if can:
        page.on('response', _on_resp)
        page.on('request', _on_req)
        page.on('framenavigated', _on_nav)
    try:
        time.sleep(random.uniform(2.0, 5.0))
        act = page.evaluate(JS_QUIZ_SUBMIT if do_submit else JS_QUIZ_SAVE)
        if act != 'ok':
            log('    %s调用失败：%s' % ('提交' if do_submit else '暂存', act),
                'warn')
            return 'failed', detail
        if can:
            deadline = time.time() + 20
            while time.time() < deadline:
                time.sleep(1)
                if resp_seen and (req_seen or nav_seen or do_submit is False):
                    break
    finally:
        if can:
            for ev, fn in (('response', _on_resp), ('request', _on_req),
                           ('framenavigated', _on_nav)):
                try:
                    page.remove_listener(ev, fn)
                except Exception:
                    pass
    ok = any(('"result":1' in b or '"result": 1' in b or '"result":"1"' in b
              or '"result": "1"' in b) for _, b in resp_seen)
    if do_submit:
        if ok:
            log('    ✓ 已提交（平台已受理）。')
            return 'submitted', detail
        if nav_seen:
            log('    ✓ 已提交（平台已受理，页面已跳转）。')
            return 'submitted', detail
        if req_seen:
            log('    ⚠ 提交请求已发出，但没当场核实到受理结果，'
                '以活动页状态为准。', 'warn')
            return 'unverified', detail
        log('    ⚠ 没观察到提交请求，这条大概率没交上。', 'warn')
        return 'failed', detail
    # 暂存（含「有缺题 / 有没落进页面所以改暂存」）
    if ok or req_seen:
        if missing or op_miss or txt_miss:
            bits = []
            if missing:
                bits.append('第 %s 题没答上' % '、'.join(map(str, missing[:8])))
            if op_miss:
                bits.append('%d 个选项没点中' % op_miss)
            if txt_miss:
                bits.append('%d 处文本没写进页面' % txt_miss)
            log('    ✓ 已暂存 %d 道答上的题（%s，未提交）。'
                % (len({f['no'] for f in fills}), '；'.join(bits)), 'warn')
            return 'partial', detail
        log('    ✓ 已暂存（平台草稿，未交卷）。答案见下方结果面板。')
        return 'ok', detail
    log('    ⚠ 暂存请求没发出去，这条没落库。', 'warn')
    return 'failed', detail


def answer_activities(cfg, targets, submit=True, progress=None, control=None,
                      headless=None) -> dict:
    """对「进行中的课堂活动」逐条作答（界面上那一节的动作列）。

    targets: [{'cid','clsid','id','type','kind','name','course'}]（查询产物）。
    submit:  True = 答完调 submitVote() 正式提交；
             False = 答完调 saveStuAnswerTemp() 存平台草稿、不交卷
                     （界面「刷题并暂存」，答案一并回传给结果面板供核对）。
    返回与 answer_urls 同构的 out，供 webui 复用交卷结果面板。
    """
    progress = progress or (lambda m, *a: log(m, a[0] if a else 'info'))
    control = control or (lambda: 'run')
    tgt = [t for t in (targets or [])
           if isinstance(t, dict) and t.get('id') and t.get('cid')
           and t.get('clsid')]
    out = {'total': len(tgt), 'submitted': 0, 'unverified': 0, 'report': 0,
           'already': 0, 'skipped': 0, 'saved': 0, 'fail': 0, 'partial': 0,
           'stopped': False, 'items': []}
    if not tgt:
        raise RuntimeError('没有可作答的课堂活动（缺少活动 / 课程定位参数）')
    ctx = launch(cfg, headless=headless)
    try:
        page = ctx.new_page()
        banner('检查登录状态')
        if not check_login(page):
            if restore_state(ctx) and check_login(page):
                log('已用本地会话自动登录 ✓')
            else:
                log('本地没有可用登录态，请先登录。', 'err')
                raise NotLoggedIn('未登录')
        _quiet_win(page, cfg, headless)
        fid = get_fid(ctx)
        banner('逐条作答课堂活动（共 %d 条，%s）'
               % (len(tgt), {True: '答完直接提交', False: '答完暂存（不交卷）'}
                  .get(submit, str(submit))))
        if submit is False:
            log('说明：随堂练习的「暂存」是平台草稿，老师端看不到；'
                '你核对无误后再手动提交。')
        for i, t in enumerate(tgt, 1):
            if _wait_control(control, progress) == 'stop':
                out['stopped'] = True
                log('已停止。', 'warn')
                break
            progress('【%d/%d】%s ｜ %s ｜ %s'
                     % (i, len(tgt), cut(t.get('course') or '（未知课程）', 18),
                        cut(t.get('kind') or '活动', 10),
                        cut(t.get('name') or '（无标题）', 26)))
            det = {}
            try:
                r, det = _answer_one_activity(page, t, cfg, submit, progress, fid)
            except Exception as e:
                r, det = 'failed', {}
                log('    这条出错了：%s' % str(e)[:120], 'warn')
            node = t.get('name') or (t.get('kind') or '活动')
            # kind='act' 显式标记这是课堂活动行（交卷面板据此：操作按钮文案是
            # 「打开活动」、「重新核实」对它走答题页而非作业卡——所以必须带上
            # cid/clsid 供核实拼答题页 URL）。别靠 kid 里有没有冒号猜——
            # kid 就是活动 activeId，本身不含冒号。
            item = {'kind': 'act',
                    'cid': t['cid'], 'clsid': t['clsid'],
                    'key': '%s:%s' % (t['cid'], t['clsid']), 'kid': t['id'],
                    'course': t.get('course') or '',
                    'node': '%s · %s' % (t.get('kind') or '活动', node),
                    'url': det.get('url') or '',
                    'answers': det.get('answers') or {},
                    'total': det.get('total') or 0,
                    'miss': det.get('miss') or [],
                    'op_miss': det.get('op_miss') or 0,
                    'txt_miss': det.get('txt_miss') or 0,
                    'status': r}
            if r == 'submitted':
                out['submitted'] += 1
                item['status'] = 'done'
                out['items'].append(item)
            elif r == 'unverified':
                out['unverified'] += 1
                item['status'] = 'unverified'
                out['items'].append(item)
            elif r == 'ok':
                out['saved'] += 1
                item['status'] = 'ok'
                out['items'].append(item)
            elif r == 'partial':
                out['partial'] += 1
                item['status'] = 'partial'
                out['items'].append(item)
            elif r == 'already':
                out['already'] += 1
                item['status'] = 'already'
                out['items'].append(item)
            elif r == 'unsolved':
                # 模型一道都没答上 = 需人工（没内容可存）。计进 report，
                # 汇总的「需人工」才跟交卷面板里那张卡对得上；不计的话
                # 面板显示「需人工 N 份」而汇总写 0，用户以为白跑了。
                out['report'] += 1
                item['status'] = 'unsolved'
                out['items'].append(item)
            elif r in ('report', 'norender', 'unsupported'):
                out['report'] += 1
                item['status'] = ('report' if r == 'report' else r)
                out['items'].append(item)
            else:
                out['fail'] += 1
                item['status'] = 'fail'
                out['items'].append(item)
            time.sleep(max(float(cfg.get('course_delay', 3.0)), 1.5))
    finally:
        save_state(ctx)
        close_ctx(ctx)
    return out


def brush_activities(cfg, targets, progress=None, control=None, headless=None) -> dict:
    """「课堂活动刷课」：把活动所在课程任务点里的待完成视频刷掉。

    活动本身没有视频，「刷课」= 刷**它所在课程**的待完成章节节点。直接委托
    brush_by_urls（同一条「回课程页 → 逐节点刷视频」链），零新增刷课代码。
    """
    from urllib.parse import urlencode
    seen, urls = set(), []
    for t in (targets or []):
        if not (isinstance(t, dict) and t.get('cid') and t.get('clsid')):
            continue
        key = (t['cid'], t['clsid'])
        if key in seen:
            continue
        seen.add(key)
        urls.append('https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/stu?'
                    + urlencode({'courseid': t['cid'], 'clazzid': t['clsid'],
                                 'cpi': t.get('cpi') or cfg.get('cpi', '')}))
    if not urls:
        raise RuntimeError('这些活动里没有能定位到课程的（缺 cid/clsid）')
    return brush_by_urls(cfg, urls, progress=progress, control=control,
                         headless=headless)


def brush_by_urls(cfg, urls, progress=None, control=None, headless=None) -> dict:
    """「上传链接刷课」：对用户粘进来的课程 / 作业 / 章节链接刷未完成视频。

    与 brush_videos 的区别：不吃课程 key，直接从链接里解析出 cid/clsid，
    再回课程页（fetch_progress_detail）找待完成章节逐个刷——课程页链接
    本来就没有 knowledgeid，逐节点下钻是唯一稳妥路径。
    urls: [链接字符串]，可混课程链接、章节(cards)链接、作业链接（作业链接
          去刷课没意义，跳过并说明）。
    返回 {'courses': [...], 'done': n, 'fail': n, 'stopped': bool}
    """
    from urllib.parse import urlparse as _urlparse, parse_qs as _parse_qs
    progress = progress or (lambda m, *a: log(m, a[0] if a else 'info'))
    control = control or (lambda: 'run')

    # 解析出 (cid, clsid) 去重
    seen, targets = set(), []
    for u in (urls or []):
        q = {}
        try:
            q = {k.lower(): (v[0] if v else '')
                 for k, v in _parse_qs(_urlparse(u).query).items()}
        except Exception:
            q = {}
        cid = q.get('courseid', '')
        clsid = q.get('clazzid', '') or q.get('classid', '')
        if not cid or not clsid:
            progress('  这条链接里没有 courseid / clazzid，跳过：%s' % u[:80], 'warn')
            continue
        if (cid, clsid) in seen:
            continue
        seen.add((cid, clsid))
        targets.append({'cid': cid, 'clsid': clsid,
                        'cpi': q.get('cpi', '') or cfg.get('cpi', ''), 'url': u})

    out = {'courses': [], 'done': 0, 'fail': 0, 'stopped': False,
           'leftover': {}}
    if not targets:
        raise RuntimeError('这些链接里都没解析出课程（需要带 courseid 和 clazzid）')

    ctx = launch(cfg, headless=headless)
    try:
        page = ctx.new_page()
    except Exception:
        close_ctx(ctx)
        raise
    rest_state = {'last': 0}
    try:
        banner('检查登录状态')
        if not check_login(page):
            if restore_state(ctx) and check_login(page):
                log('已用本地会话自动登录 ✓')
            else:
                log('本地没有可用登录态，请先登录。', 'err')
                raise NotLoggedIn('未登录')
        banner('按链接刷视频（共 %d 门）' % len(targets))
        if (cfg.get('brush_minimize', True) and not (headless is True)
                and not OFFSCREEN_LAUNCH):
            if minimize_window(page):
                progress('浏览器已最小化到任务栏（后台刷课不影响进度）。')
        for i, t in enumerate(targets, 1):
            if _wait_control(control, progress) == 'stop':
                out['stopped'] = True
                log('已停止。', 'warn')
                break
            c = {'cid': t['cid'], 'clsid': t['clsid'], 'name': '课程%s' % t['cid']}
            progress('【%d/%d】%s' % (i, len(targets), c['name']))
            try:
                pd = fetch_progress_detail(page, c, t.get('cpi', ''), cfg)
            except Exception as e:
                pd = {'page_failed': True, 'reason': str(e).splitlines()[0][:120]}
            if pd.get('page_failed'):
                progress('  ⚠ 课程页没加载成功（%s），跳过。' % pd.get('reason', ''))
                out['courses'].append({'name': c['name'], 'done': 0, 'fail': 0,
                                       'note': '页面未加载'})
                continue
            if pd.get('no_module') or not pd.get('pending'):
                progress('  没有待完成的章节节点，跳过。')
                out['courses'].append({'name': c['name'], 'done': 0, 'fail': 0,
                                       'note': '无待完成节点'})
                continue
            nodes = [n for n in pd['pending'] if n.get('kid')]
            progress('  待完成节点 %d 个，开始刷…' % len(nodes))
            cd = cf = 0
            stopped_here = False
            for j, node in enumerate(nodes, 1):
                if _wait_control(control, progress) == 'stop':
                    stopped_here = True
                    break
                progress('  ▶ 节点 %d/%d：%s' % (j, len(nodes), cut(node['name'], 30)))
                d, f, stopped_here, lf = brush_node(page, c, t.get('cpi', ''),
                                                    node['kid'], cfg, progress, control)
                cd += d
                cf += f
                for _k, _v in (lf or {}).items():
                    out['leftover'][_k] = out['leftover'].get(_k, 0) + _v
                if stopped_here:
                    break
                if brush_pace_rest(out['done'] + cd, cfg, progress, control, rest_state):
                    stopped_here = True
                    break
                time.sleep(max(float(cfg.get('course_delay', 3.0)), 1.0))
            out['courses'].append({'name': c['name'], 'done': cd, 'fail': cf})
            out['done'] += cd
            out['fail'] += cf
            progress('  ✓ 完成 %d 个节点，未完成 %d 个。' % (cd, cf))
            if stopped_here:
                out['stopped'] = True
                break
            time.sleep(max(float(cfg.get('course_delay', 3.0)), 2.0))
        _leftover_note(out, progress)
    finally:
        save_state(ctx)
        close_ctx(ctx)
    return out


def build_report(courses, results, stopped_at=None, scope_total=None,
                 scope_kind='selected', failed_courses=None, fid='2339') -> dict:
    """scope_total：账号里的课程总数。

    只在「本次只查了一部分课程」时传入。传了它，报告就会显式写明这是
    一次范围受限的检查——否则用户看到清单里没别的课，会误以为都完成了。
    scope_kind：'selected'=用户勾选了一部分；'recent'=只查了最近 N 门。
    两者措辞不同但同样必须标注，理由一致。
    failed_courses：页面没加载成功的课程清单（v2.6 起），报告里要显式警告。
    fid：机构号（登录态里取）。只用来给课堂活动拼「去完成」的答题页链接，
        界面拿它当 href，点一下就走工具浏览器打开（带登录态）。
    """
    def label(i):
        c = courses[i]
        same = [x for x in courses if x['name'] == c['name']]
        return '%s（ID:%s）' % (c['name'], c['cid']) if len(same) > 1 else c['name']

    undone_hw, undone_exam, undone_prog, undone_act, overview = [], [], [], [], []
    grade_rows, low_scores = [], []
    # v6.25：章节目录标「已完成」、测验卡其实待完成的节点（按课归拢）
    fake_done = {}
    for i, r in enumerate(results):
        nm = label(i)
        hw, ex = r['hw'], r['exam']
        for it in hw.get('items', []):
            if is_undone(it):
                # cid/clsid：界面「单门刷新」按它们定位这门课，只刷新它
                undone_hw.append({'course': nm, 'cid': r['cid'],
                                  'clsid': r['clsid'], **it})
        for it in ex.get('items', []):
            if is_undone(it):
                undone_exam.append({'course': nm, 'cid': r['cid'],
                                    'clsid': r['clsid'], **it})
        # 「任务」页里进行中(status=1)的课堂活动：随堂练习 / 测验 / 签到…
        # 带上 cid/clsid/cpi：界面上这节的「刷课 / 刷题」按钮要靠它们定位
        # （拼答题链接、回课程页刷视频），没这几个参数就没法动手。
        # url：答题页链接，给界面「去完成」当 href（跟未完成作业那列一个用法）
        # ——点了走 /api/open-work 用工具自己的浏览器打开，带登录态。
        for it in (r.get('act') or {}).get('ongoing', []):
            rec = {'course': nm, 'cid': r['cid'],
                   'clsid': r['clsid'], 'cpi': r.get('cpi', ''), **it}
            # 「去完成」按类型给对的页面（平台每种活动跳的页面都不同）；拼不出就不给。
            # answerable：只有 42/14 走那套答题面、大模型答得了，界面据此决定是否出
            # 「作答」按钮——别对主题讨论/AI实践给出假的自动作答（2026-10-14 真机）。
            rec['answerable'] = str(it.get('type')) in ACT_ANSWERABLE_TYPES
            if it.get('id') and r.get('clsid'):
                rec['url'] = activity_open_url(
                    it.get('type'), it['id'], r['cid'], r['clsid'],
                    fid or '2339', it.get('raw'))
            undone_act.append(rec)

        # 成绩（只有本轮开了「顺带查成绩」才有 scores 这个键）
        for it in (r.get('scores') or {}).get('items') or []:
            rec = {'course': nm, 'cid': r['cid'], 'clsid': r['clsid'], **it}
            grade_rows.append(rec)
            # v6.25：章节目录标「已完成」、点进去其实是答题页 —— 当欠账算。
            # 这批节点以前根本不出现（章节列表不给 .catalog_jindu），用户
            # 只能自己一个个点进去才发现，正是本次报的那个问题。
            if it.get('fakeDone') and it.get('kid'):
                fake_done.setdefault(i, []).append(it)
            if it.get('score') is not None:
                try:
                    if float(it['score']) < 85.0:
                        low_scores.append(rec)
                except Exception:
                    pass
            elif it.get('notPass'):
                low_scores.append(rec)

        # v6.25：这门课的「假完成」节点。它们不在章节列表的待办里，所以
        # done/total 的进度是满的；但实际有作业没做，必须单独列出来。
        fake = fake_done.get(i) or []
        fake_chs = [{'name': (x.get('node') or ('节点 %s' % x.get('kid'))),
                     'count': 1, 'kid': x.get('kid'), 'fakeDone': True,
                     'nQ': x.get('nQ') or 0} for x in fake]
        pm = re.search(r'(\d+)\s*/\s*(\d+)', r.get('prog_text') or '')
        if pm:
            done, total = int(pm.group(1)), int(pm.group(2))
            if done < total or fake_chs:
                pd = r.get('prog_detail') or {}
                chs = list(pd.get('pending') or [])
                have = {c.get('kid') for c in chs}
                chs += [c for c in fake_chs if c.get('kid') not in have]
                # cid/clsid 是「自动刷视频」的定位参数：界面勾选哪门课要刷，
                # 后端靠这两个 ID 重新找到这门课的章节页。
                undone_prog.append({'course': nm, 'done': done, 'total': total,
                                    'rate': done * 100.0 / total if total else 0,
                                    'cid': r['cid'], 'clsid': r['clsid'],
                                    'chapters': chs,
                                    'fake': len(fake_chs)})
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
        act = r.get('act') or {}
        if act.get('page_failed'):
            act_txt = '⚠ 未加载'
        elif act.get('activities'):
            n_on = len(act.get('ongoing') or [])
            act_txt = '%d 个' % len(act['activities']) + (
                '（进行中 %d）' % n_on if n_on else '')
        else:
            act_txt = '—'
        overview.append({
            'name': nm, 'hw': hw_txt, 'exam': ex_txt, 'act': act_txt,
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
        # v6.25：只含「章节目录说完成、实际没做」的节点（明细，界面提示用）
        'undone_fake': [x for v in fake_done.values() for x in v],
        'undone_act': undone_act,
        'grades': grade_rows,
        'low_scores': low_scores,
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

    # 进行中的课堂活动排最前：老师刚发、有时限，最该先处理（v6.8 起，与界面一致）。
    L += ['## 一、进行中的课堂活动（任务页，%d 项）' % len(rep['undone_act']), '']
    if rep['undone_act']:
        L += ['| # | 课程 | 类型 | 名称 | 时间 | 去完成 |',
              '|---|------|------|------|------|--------|']
        for i, it in enumerate(rep['undone_act'], 1):
            link = '[去完成](%s)' % it['url'] if it.get('url') else '—'
            L.append('| %d | %s | %s | %s | %s | %s |' % (
                i, it['course'], it.get('kind') or '活动',
                it.get('name') or '（无标题）', it.get('time') or '—', link))
        L += ['', '> 这些是老师发的课堂活动，只在课程首页的「任务」页里，'
                  '作业和章节任务点都不含。「去完成」按类型打开对的页面'
                  '（测验 / 问卷 / 主题讨论 / AI实践各有各的页）。',
              '> 「作答」（大模型作答，暂存 = 存平台草稿不交 / 提交 = 直接交卷）'
              '只对「随堂练习/测验」「问卷」出现——这两种才走那套答题面；'
              '主题讨论、AI实践、签到等答题面不一样，请点「去完成」自己做。'
              '整节还有「刷这些课的视频 / 刷课+刷题」。']
    else:
        L.append('无进行中的课堂活动。')
    L.append('')

    # 成绩单（只有本轮开了「顺带查成绩」才非空）
    gr = rep.get('grades') or []
    if gr or rep.get('low_scores'):
        low = rep.get('low_scores') or []
        L += ['## 一·五、测验成绩（%d 份；低于 85 分 %d 份）'
              % (len(gr), len(low)), '']
        L += ['| # | 课程 | 测验 | 成绩 | 满分 | 剩余重做次数 | 状态 |',
              '|---|------|------|------|------|----------------|------|']
        for i, it in enumerate(sorted(
                gr, key=lambda x: (x.get('score') if x.get('score') is not None
                                   else -1)), 1):
            sc = it.get('score')
            sc_txt = ('**%s**' % _fmt_num(sc)) if sc is not None else '—'
            if (sc is not None and float(sc) < 85.0) or it.get('notPass'):
                st = '⚠ 低于 85，建议重刷'
            elif it.get('pending'):
                st = '待完成'
            elif sc is not None:
                st = '已达标'
            else:
                st = it.get('note') or '—'
            L.append('| %d | %s | %s | %s | %s | %s | %s |' % (
                i, it.get('course'), it.get('node'), sc_txt,
                _fmt_num(it.get('full') or 100),
                ('%d 次' % it['left']) if it.get('left') is not None else '—',
                st))
        if low:
            L += ['', '> 低于 85 分的测验可以重刷（工具内「查成绩 / 重刷」按钮）：'
                      '点「重做」后重新作答并交卷，最多重刷 2 次，取最高分。'
                      '平台每次重做会**重排题目顺序**，所以必须重新作答，'
                      '不能照抄上次的答案。']
        L.append('')

    L += ['## 二、未完成作业（%d 项）' % len(rep['undone_hw']), '']
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

    L += ['## 三、未完成考试（%d 项）' % len(rep['undone_exam']), '']
    if rep['undone_exam']:
        L += ['| # | 课程 | 考试名称 | 状态 |', '|---|------|----------|------|']
        for i, it in enumerate(rep['undone_exam'], 1):
            L.append('| %d | %s | %s | **%s** |' % (i, it['course'], it['title'], it['state']))
    else:
        L.append('无。')
    L.append('')

    L += ['## 四、未完成任务点（章节学习，%d 门课）' % len(rep['undone_prog']), '']
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
                    L += ['| 章节 | 待完成任务点 | 备注 |',
                          '|------|--------------|------|']
                    for ch in chs:
                        L.append('| %s | %d | %s |' % (
                            ch['name'], ch.get('count', 1),
                            '⚠ 平台标「已完成」，实际没做' if ch.get('fakeDone')
                            else '—'))
                    L.append('')
                else:
                    L += ['- （未取到章节明细）', '']
        # v6.25：单独说明这批节点，否则「完成率 100% 却出现在欠账区」看着像 bug
        fake_all = rep.get('undone_fake') or []
        if fake_all:
            L += ['> **⚠ 有 %d 个任务点平台标成「已完成」，点进去其实是没做的**：'
                  % len(fake_all),
                  '> %s。' % '、'.join('%s（%s）' % (x.get('node'), x.get('course'))
                                      for x in fake_all[:20]),
                  '> 章节目录的完成图标与测验卡的状态对不上（平台侧不一致）。'
                  '已按「未完成」计入上表；工具里勾上它们点「做作业」即可补做。', '']
    else:
        L.append('无。')
    L.append('')

    L += ['## 附：全部课程状态总览', '',
          '| # | 课程 | 作业 | 考试 | 任务点 | 任务页 |',
          '|---|------|------|------|--------|--------|']
    for i, o in enumerate(rep['overview'], 1):
        L.append('| %d | %s | %s | %s | %s | %s |' % (
            i, o['name'], o['hw'], o['exam'], o['prog'], o.get('act', '—')))
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
        for it in rep.get('undone_act', []):
            w.writerow(['课堂活动', it['course'], it.get('name') or '',
                        it.get('kind') or '', it.get('time') or '',
                        it.get('url') or ''])
        for it in rep.get('grades', []):
            sc = it.get('score')
            w.writerow(['成绩', it.get('course'), it.get('node'),
                        _fmt_num(sc) if sc is not None else (it.get('note') or '无成绩'),
                        ('剩余重做 %s 次' % it['left'])
                        if it.get('left') is not None else '', it.get('url') or ''])

    # ---- JSON 快照，用于和上次对比
    snap_path = SNAPSHOT_DIR / ('%s.json' % stamp)
    snap = {'time': rep['time'],
            'undone': [{'type': '作业', 'course': x['course'], 'title': x['title']}
                       for x in rep['undone_hw']] +
                      [{'type': '考试', 'course': x['course'], 'title': x['title']}
                       for x in rep['undone_exam']] +
                      [{'type': '课堂活动', 'course': x['course'],
                        'title': x.get('name') or ''}
                       for x in rep.get('undone_act', [])]}
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
    log('✅ 未完成作业 %d 项 / 未完成考试 %d 项 / 未完成任务点 %d 门 / 进行中活动 %d 项' % (
        len(rep['undone_hw']), len(rep['undone_exam']), len(rep['undone_prog']),
        len(rep.get('undone_act', []))))
    _fake = rep.get('undone_fake') or []
    if _fake:
        # v6.25：这批节点在章节列表里显示「已完成」，不单独报一句用户根本
        # 不会知道它们其实没做（本次报的就是这个）。
        log('   ⚠ 另有 %d 个任务点平台标了「已完成」，点进去其实是没做的：'
            % len(_fake), 'warn')
        for x in _fake[:12]:
            log('      [假完成] %s ｜ %s'
                % (cut(x.get('course') or '', 22),
                   cut(x.get('node') or '', 30)), 'warn')
        if len(_fake) > 12:
            log('      …… 另有 %d 个，见报告。' % (len(_fake) - 12), 'warn')
    for it in rep['undone_hw']:
        log('   [作业] %s ｜ %s ｜ %s ｜ %s' % (
            cut(it['course'], 22), cut(it['title'], 34), it['state'], it.get('left') or '—'))
    for it in rep['undone_exam']:
        log('   [考试] %s ｜ %s ｜ %s' % (cut(it['course'], 22), cut(it['title'], 34), it['state']))
    for it in rep['undone_prog']:
        log('   [任务点] %s ｜ %d/%d（%.1f%%）' % (
            cut(it['course'], 22), it['done'], it['total'], it['rate']))
    for it in rep.get('undone_act', []):
        log('   [活动] %s ｜ %s ｜ %s ｜ %s' % (
            cut(it['course'], 22), it.get('kind') or '活动',
            cut(it.get('name') or '', 30), it.get('time') or '—'))
    for it in rep.get('low_scores') or []:
        log('   [低分] %s ｜ %s ｜ %s ｜ 可重做 %s 次' % (
            cut(it.get('course') or '', 22), cut(it.get('node') or '', 30),
            (_fmt_num(it['score']) + ' 分') if it.get('score') is not None
            else '未及格', it.get('left') if it.get('left') is not None else '?'))

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
    log('浏览器内核：%s' % (exe if exe else '未找到（将用系统 Edge/Chrome，仍没有才自动下载）'))
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

    p = sub.add_parser('desktop', help='桌面窗口（v4.0 起的默认入口）')
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

    if args.cmd == 'desktop':
        import webui
        return webui.serve_desktop(port=args.port)

    if args.cmd == 'gui':
        import webui
        webui.serve(port=args.port)
        return 0

    ap.print_help()
    return 0


if __name__ == '__main__':
    sys.exit(main())
