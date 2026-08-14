using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Windows.Forms;

[assembly: AssemblyTitle("猫猫")]
[assembly: AssemblyDescription("猫猫私人语音助手启动器")]
[assembly: AssemblyCompany("猫猫")]
[assembly: AssemblyProduct("猫猫")]
[assembly: AssemblyCopyright("Copyright © 2026")]
[assembly: AssemblyVersion("0.0.3.0")]
[assembly: AssemblyFileVersion("0.0.3.0")]

internal static class MaoMaoLauncher
{
    [STAThread]
    private static int Main(string[] args)
    {
        string projectRoot = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(
            Path.DirectorySeparatorChar,
            Path.AltDirectorySeparatorChar
        );
        string portablePythonw = Path.Combine(projectRoot, "runtime", "pythonw.exe");
        string pythonw = File.Exists(portablePythonw)
            ? portablePythonw
            : Path.Combine(projectRoot, ".venv", "Scripts", "pythonw.exe");

        string missing = FindMissingFiles(pythonw);
        if (HasArgument(args, "--check"))
        {
            return missing.Length == 0 ? 0 : 2;
        }
        if (missing.Length != 0)
        {
            MessageBox.Show(
                "猫猫的运行文件不完整：\n\n" + missing
                    + "\n请保留整个项目文件夹，不能只复制猫猫.exe。",
                "无法启动猫猫",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error
            );
            return 2;
        }

        ProcessStartInfo startInfo = new ProcessStartInfo();
        startInfo.FileName = pythonw;
        startInfo.Arguments = "-m assistant_app.qt_quick.app";
        startInfo.WorkingDirectory = projectRoot;
        startInfo.UseShellExecute = false;
        startInfo.CreateNoWindow = true;
        startInfo.WindowStyle = ProcessWindowStyle.Hidden;

        try
        {
            Process.Start(startInfo);
            return 0;
        }
        catch (Win32Exception error)
        {
            MessageBox.Show(
                "启动进程失败：" + error.Message,
                "无法启动猫猫",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error
            );
            return 4;
        }
        catch (Exception error)
        {
            MessageBox.Show(
                "启动失败：" + error.Message,
                "无法启动猫猫",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error
            );
            return 5;
        }
    }

    private static bool HasArgument(string[] args, string expected)
    {
        foreach (string argument in args)
        {
            if (string.Equals(argument, expected, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
        }
        return false;
    }

    private static string FindMissingFiles(string pythonw)
    {
        StringBuilder missing = new StringBuilder();
        if (!File.Exists(pythonw))
        {
            missing.AppendLine("- runtime\\pythonw.exe or .venv\\Scripts\\pythonw.exe");
        }
        return missing.ToString().Trim();
    }
}
