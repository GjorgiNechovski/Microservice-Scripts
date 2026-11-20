using System.ComponentModel;
using System.Diagnostics;
using System.Text;

namespace MicroserviceScripts.Ui.Infrastructure;

public sealed record ProcessResult(
    int ExitCode, string StdOut, string StdErr, bool TimedOut = false, bool NotFound = false)
{
    public bool Ok => ExitCode == 0 && !TimedOut && !NotFound;

    /// <summary>What went wrong, for showing to a person.</summary>
    public string Error => (string.IsNullOrWhiteSpace(StdErr) ? StdOut : StdErr).Trim();
}

public interface IProcessRunner
{
    Task<ProcessResult> RunAsync(
        string fileName,
        IReadOnlyList<string> arguments,
        string? workingDirectory = null,
        TimeSpan? timeout = null,
        CancellationToken cancellationToken = default);
}

/// <summary>
/// Runs a program with an argument list, never through a shell, so nothing a
/// user typed (a path, a project name) can be read as a command.
/// </summary>
public sealed class ProcessRunner : IProcessRunner
{
    public async Task<ProcessResult> RunAsync(
        string fileName,
        IReadOnlyList<string> arguments,
        string? workingDirectory = null,
        TimeSpan? timeout = null,
        CancellationToken cancellationToken = default)
    {
        var info = new ProcessStartInfo(fileName)
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        if (workingDirectory is not null)
        {
            info.WorkingDirectory = workingDirectory;
        }

        foreach (var argument in arguments)
        {
            info.ArgumentList.Add(argument);
        }

        // The analyzer prints μ and ρ; on Windows a redirected Python would
        // otherwise encode them with the ANSI code page.
        info.Environment["PYTHONIOENCODING"] = "utf-8";

        using var process = new Process { StartInfo = info };
        try
        {
            process.Start();
        }
        catch (Win32Exception ex)
        {
            return new ProcessResult(-1, "", $"{fileName}: {ex.Message}", NotFound: true);
        }

        var stdout = process.StandardOutput.ReadToEndAsync(cancellationToken);
        var stderr = process.StandardError.ReadToEndAsync(cancellationToken);
        using var limit = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        limit.CancelAfter(timeout ?? TimeSpan.FromSeconds(60));
        try
        {
            await process.WaitForExitAsync(limit.Token);
        }
        catch (OperationCanceledException)
        {
            try
            {
                process.Kill(entireProcessTree: true);
            }
            catch (InvalidOperationException)
            {
                // already exited
            }

            return new ProcessResult(-1, "", $"{fileName} did not finish in time", TimedOut: true);
        }

        return new ProcessResult(process.ExitCode, await stdout, await stderr);
    }
}
