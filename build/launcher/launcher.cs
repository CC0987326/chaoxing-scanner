// 学习通巡检工具 · 图形启动器（源码）
// ------------------------------------------------------------------
// 作用：一个无控制台窗口的原生 Windows 程序，双击后静默拉起
//       便携 Python 运行桌面端（app\chaoxing_scanner.py desktop）。
//
// 为什么不用 .bat：双击 bat 会闪一个黑色控制台窗、没有图标、
// 且部分安全策略直接禁止运行 .bat。编译成 win32 程序（/target:winexe）
// 后全程无黑窗，可带图标。
//
// 编译： csc /target:winexe /win32icon:launcher.ico /out:学习通巡检工具.exe launcher.cs
// （编译脚本见 build_launcher.py，会顺带生成图标）
//
// 只依赖 mscorlib / System，弹窗用 user32!MessageBoxW，不再引入 WinForms。
using System;
using System.IO;
using System.Diagnostics;
using System.Runtime.InteropServices;

internal static class Launcher
{
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int MessageBoxW(IntPtr hWnd, string text,
                                          string caption, uint type);

    private const uint MB_OK = 0x0;
    private const uint MB_ICONERROR = 0x10;
    private const uint MB_ICONWARNING = 0x30;

    private static int Main()
    {
        // BaseDirectory 一定以分隔符结尾（如 "D:\学习通巡检工具\"）。
        // Path.Combine 自己能处理这个尾部分隔符，**不要**去 TrimEnd 它——
        // 万一程序被放在盘根（"C:\"），TrimEnd 会得到 "C:"，那是个相对
        // 路径（指 C 盘当前目录），Path.Combine 会拼出错误结果。
        string baseDir = AppDomain.CurrentDomain.BaseDirectory;

        // 定位便携解释器：优先 pythonw.exe（无控制台），退回 python.exe。
        string[] pyNames = { "pythonw.exe", "python.exe" };
        string python = null;
        foreach (string n in pyNames)
        {
            string p = Path.Combine(baseDir, "python", n);
            if (File.Exists(p)) { python = p; break; }
        }
        if (python == null)
        {
            Fail("找不到内置的 Python 运行环境。\r\n\r\n"
                 + "请确认「学习通巡检工具.exe」与「python」文件夹在同一个目录下"
                 + "（整个文件夹一起解压，不要只把这个 exe 单独拖出来）。");
            return 2;
        }

        // 定位主程序：标准布局在 app\ 下；兼容直接放在根目录的旧结构。
        string script = null;
        string[] scriptCands = {
            Path.Combine(baseDir, "app", "chaoxing_scanner.py"),
            Path.Combine(baseDir, "chaoxing_scanner.py"),
        };
        foreach (string p in scriptCands)
        {
            if (File.Exists(p)) { script = p; break; }
        }
        if (script == null)
        {
            Fail("找不到程序文件 chaoxing_scanner.py。\r\n\r\n"
                 + "这个文件夹可能不完整了，请重新完整解压一遍。");
            return 3;
        }

        // 工作目录设为主程序所在目录：即便程序内部有相对路径也不会踩空。
        string workDir = Path.GetDirectoryName(script);

        try
        {
            ProcessStartInfo psi = new ProcessStartInfo();
            psi.FileName = python;
            psi.Arguments = Quote(script) + " desktop";
            psi.WorkingDirectory = workDir;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            Process.Start(psi);
        }
        catch (Exception e)
        {
            Fail("启动失败：" + e.Message);
            return 4;
        }
        return 0;
    }

    // 把路径包进双引号（命令行传参）：路径含空格时必需。
    private static string Quote(string s)
    {
        return "\"" + s + "\"";
    }

    private static void Fail(string msg)
    {
        MessageBoxW(IntPtr.Zero, msg, "学习通巡检工具", MB_OK | MB_ICONERROR);
    }
}
