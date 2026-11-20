using System.Diagnostics;

namespace MicroserviceScripts.Ui.Infrastructure;

public static class BrowserLauncher
{
    /// <summary>Opens the URL in the default browser; says so if it can't.</summary>
    public static void TryOpen(string url)
    {
        try
        {
            if (OperatingSystem.IsWindows())
            {
                Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
                return;
            }

            // The browser's own output goes to /dev/null. A Chromium-based browser
            // that is already open hands the address to its window and exits, and
            // on the way out prints harmless errors ("Failed sending zygote boot
            // message: Broken pipe") that would otherwise land in this terminal and
            // look like the app's. Not a pipe to this app: a browser started fresh
            // must not depend on the app staying up. The URL is an argument ($1),
            // never part of the command text.
            var info = new ProcessStartInfo("sh") { UseShellExecute = false };
            info.ArgumentList.Add("-c");
            info.ArgumentList.Add($"{(OperatingSystem.IsMacOS() ? "open" : "xdg-open")} \"$1\" >/dev/null 2>&1");
            info.ArgumentList.Add("sh");
            info.ArgumentList.Add(url);
            Process.Start(info);
        }
        catch (Exception ex) when (ex is System.ComponentModel.Win32Exception or InvalidOperationException)
        {
            Console.WriteLine($"Couldn't open a browser ({ex.Message}); open the address above yourself.");
        }
    }
}
