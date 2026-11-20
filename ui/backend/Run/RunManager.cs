using System.Diagnostics;
using System.Text;
using System.Text.Json;
using MicroserviceScripts.Ui.Infrastructure;
using MicroserviceScripts.Ui.Load;
using MicroserviceScripts.Ui.Setup;

namespace MicroserviceScripts.Ui.Run;

public enum RunState
{
    Idle,
    Running,
    Stopping,
    Finished,
    Failed,
    Stopped,
}

/// <param name="Error">It came from the analyzer's error stream (or from the app itself).</param>
public sealed record RunLine(int N, string Text, bool Error);

/// <param name="Lines">The output after the line the caller already has.</param>
/// <param name="Next">The number to ask from next time.</param>
/// <param name="Results">The report files the run wrote, once it has ended.</param>
public sealed record RunStatus(
    RunState State,
    string? SpecPath,
    bool FaultInjection,
    string? OutDir,
    DateTimeOffset? StartedAt,
    DateTimeOffset? EndedAt,
    int? ExitCode,
    IReadOnlyList<RunLine> Lines,
    int Next,
    IReadOnlyList<string> Results);

/// <param name="Secrets">The load script's secrets by variable (LOGIN_PASSWORD, AUTH_TOKEN): passed to it, never written anywhere.</param>
public sealed record StartRequest(string SpecPath, bool FaultInjection, IReadOnlyDictionary<string, string>? Secrets = null);

/// <summary>
/// Runs the analyzer's whole pipeline (<c>python3 -m analyzer</c>) for a saved
/// spec, one run at a time, keeping its output for the page to follow.
/// Stopping it asks the analyzer to stop (Ctrl+C), which puts back a
/// container it stopped and removes its k6 container; afterwards anything the
/// app's projects had running when the run started is started again, in case
/// the analyzer couldn't.
/// </summary>
public sealed class RunManager(PythonLocator python, IProcessRunner runner, RepoLocation repo)
{
    private const int MaxLines = 20_000;
    private static readonly TimeSpan GracePeriod = TimeSpan.FromSeconds(90);

    private readonly Lock gate = new();
    private readonly List<RunLine> lines = [];
    private int dropped;
    private Process? process;
    private int pid;
    private RunState state = RunState.Idle;
    private string? specPath;
    private string? outDir;
    private bool faultInjection;
    private DateTimeOffset? startedAt;
    private DateTimeOffset? endedAt;
    private int? exitCode;
    private IReadOnlyList<string> projects = [];
    private IReadOnlyList<string> runningBefore = [];

    public RunStatus Status(int after)
    {
        lock (gate)
        {
            var from = Math.Max(0, after - dropped);
            var results = state is RunState.Finished or RunState.Failed or RunState.Stopped && outDir is not null
                && Directory.Exists(outDir)
                ? Directory.EnumerateFiles(outDir, "*.txt").Where(f => !f.EndsWith("k6-output.txt", StringComparison.Ordinal))
                    .Order(StringComparer.Ordinal).ToList()
                : [];
            return new RunStatus(state, specPath, faultInjection, outDir, startedAt, endedAt, exitCode,
                lines.Skip(from).ToList(), dropped + lines.Count, results);
        }
    }

    /// <summary>Starts a run; returns why it couldn't, or null.</summary>
    public async Task<string?> StartAsync(StartRequest request, CancellationToken ct)
    {
        lock (gate)
        {
            if (state is RunState.Running or RunState.Stopping)
            {
                return "A run is already going: stop it or wait for it to end.";
            }
        }

        var spec = Path.GetFullPath(FolderBrowser.ExpandHome(request.SpecPath));
        if (!File.Exists(spec))
        {
            return $"There's no spec at {spec}: save it first.";
        }

        var py = await python.FindAsync(ct);
        if (py is null)
        {
            return "Python 3.11 or newer wasn't found.";
        }

        var check = await runner.RunAsync(py.FileName, py.Arguments("-m", "analyzer.spec", spec, "--json"),
            repo.Root, TimeSpan.FromSeconds(60), ct);
        var found = new List<string>();
        try
        {
            using var verdict = JsonDocument.Parse(check.StdOut.Split('\n', StringSplitOptions.RemoveEmptyEntries).LastOrDefault() ?? "{}");
            if (!verdict.RootElement.TryGetProperty("ok", out var ok) || !ok.GetBoolean())
            {
                return $"The analyzer rejects the spec: {check.Error}";
            }

            found.AddRange(verdict.RootElement.GetProperty("compose_projects").EnumerateArray().Select(p => p.GetString()!));
        }
        catch (JsonException)
        {
            return $"The analyzer couldn't read the spec: {check.Error}";
        }

        var before = await RunningContainersAsync(found, ct);
        var output = Path.Combine(Path.GetDirectoryName(spec)!, "analysis");
        var arguments = new List<string> { "-u", "-m", "analyzer", spec, "--out", output };
        if (!request.FaultInjection)
        {
            arguments.Add("--skip-chaos");
        }

        var info = new ProcessStartInfo(py.FileName)
        {
            WorkingDirectory = repo.Root,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        foreach (var a in py.Arguments([.. arguments]))
        {
            info.ArgumentList.Add(a);
        }

        info.Environment["PYTHONUNBUFFERED"] = "1";
        info.Environment["PYTHONIOENCODING"] = "utf-8";
        foreach (var (name, value) in request.Secrets ?? new Dictionary<string, string>())
        {
            // only the variables the generated script reads: nothing else of the environment is settable from the page
            if (LoadScript.SecretVariables.Contains(name) && !string.IsNullOrEmpty(value))
            {
                info.Environment[name] = value;
            }
        }

        var started = new Process { StartInfo = info, EnableRaisingEvents = true };
        started.OutputDataReceived += (_, e) => Add(e.Data, error: false);
        started.ErrorDataReceived += (_, e) => Add(e.Data, error: true);
        started.Exited += (_, _) => _ = OnExitedAsync(started);

        lock (gate)
        {
            if (state is RunState.Running or RunState.Stopping)
            {
                return "A run is already going: stop it or wait for it to end.";
            }

            lines.Clear();
            dropped = 0;
            specPath = spec;
            outDir = output;
            faultInjection = request.FaultInjection;
            startedAt = DateTimeOffset.Now;
            endedAt = null;
            exitCode = null;
            projects = found;
            runningBefore = before;
            process = started;
            state = RunState.Running;
            Append($"[setup app] python3 -m analyzer {spec} --out {output}"
                   + (request.FaultInjection ? "" : " --skip-chaos"), error: false);
        }

        try
        {
            started.Start();
        }
        catch (Exception ex) when (ex is System.ComponentModel.Win32Exception or InvalidOperationException)
        {
            lock (gate)
            {
                state = RunState.Failed;
                endedAt = DateTimeOffset.Now;
                process = null;
                Append($"[setup app] couldn't start Python: {ex.Message}", error: true);
            }

            return null;
        }

        lock (gate)
        {
            pid = started.Id;
        }

        started.BeginOutputReadLine();
        started.BeginErrorReadLine();
        return null;
    }

    /// <summary>Asks the run to stop; it ends within the grace period, forcibly after it.</summary>
    public async Task StopAsync()
    {
        Process? target;
        lock (gate)
        {
            if (state != RunState.Running || process is null)
            {
                return;
            }

            state = RunState.Stopping;
            target = process;
            Append("[setup app] stopping: the analyzer puts back anything it stopped and removes its load generator…", error: false);
        }

        if (!OperatingSystem.IsWindows())
        {
            // Ctrl+C: the analyzer's own clean-up runs (restart a killed container, remove k6)
            await runner.RunAsync("kill", ["-INT", target.Id.ToString(System.Globalization.CultureInfo.InvariantCulture)],
                timeout: TimeSpan.FromSeconds(10));
        }

        using var wait = new CancellationTokenSource(OperatingSystem.IsWindows() ? TimeSpan.Zero : GracePeriod);
        try
        {
            await target.WaitForExitAsync(wait.Token);
        }
        catch (OperationCanceledException)
        {
            try
            {
                target.Kill(entireProcessTree: true);
            }
            catch (InvalidOperationException)
            {
                // it ended in the meantime
            }
        }
    }

    private async Task OnExitedAsync(Process exited)
    {
        // let the last output lines arrive
        try
        {
            await exited.WaitForExitAsync();
        }
        catch (InvalidOperationException)
        {
        }

        bool stopped;
        int id;
        lock (gate)
        {
            stopped = state == RunState.Stopping;
            exitCode = exited.ExitCode;
            id = pid;
        }

        if (stopped || exited.ExitCode != 0)
        {
            await PutBackAsync(id);
        }

        lock (gate)
        {
            state = stopped ? RunState.Stopped : exited.ExitCode == 0 ? RunState.Finished : RunState.Failed;
            endedAt = DateTimeOffset.Now;
            process = null;
            Append(state switch
            {
                RunState.Finished => "[setup app] the run finished.",
                RunState.Stopped => "[setup app] the run was stopped.",
                _ => $"[setup app] the run ended with an error (exit code {exited.ExitCode}).",
            }, error: state == RunState.Failed);
        }

        exited.Dispose();
    }

    /// <summary>
    /// After a stop or a crash: removes the run's k6 container and starts
    /// whatever of the app was running when the run began and isn't now.
    /// </summary>
    private async Task PutBackAsync(int pythonId)
    {
        var k6 = await runner.RunAsync("docker",
            ["ps", "-aq", "--filter", $"name=microservice-scripts-k6-{pythonId}-"], timeout: TimeSpan.FromSeconds(20));
        foreach (var id in Ids(k6.StdOut))
        {
            await runner.RunAsync("docker", ["rm", "-f", id], timeout: TimeSpan.FromSeconds(30));
            Add($"[setup app] removed the load generator's container {id[..Math.Min(12, id.Length)]}", error: false);
        }

        IReadOnlyList<string> before, wanted;
        lock (gate)
        {
            before = runningBefore;
            wanted = projects;
        }

        var now = await RunningContainersAsync(wanted, CancellationToken.None);
        foreach (var id in before.Except(now))
        {
            var started = await runner.RunAsync("docker", ["start", id], timeout: TimeSpan.FromSeconds(60));
            Add(started.Ok
                    ? $"[setup app] started container {id[..Math.Min(12, id.Length)]} again: it was running when the run began"
                    : $"[setup app] couldn't start container {id[..Math.Min(12, id.Length)]} again: {started.Error}",
                error: !started.Ok);
        }
    }

    private async Task<IReadOnlyList<string>> RunningContainersAsync(IEnumerable<string> composeProjects, CancellationToken ct)
    {
        var ids = new List<string>();
        foreach (var project in composeProjects)
        {
            var result = await runner.RunAsync("docker",
                ["ps", "-q", "--no-trunc", "--filter", $"label=com.docker.compose.project={project}"],
                timeout: TimeSpan.FromSeconds(20), cancellationToken: ct);
            ids.AddRange(Ids(result.StdOut));
        }

        return ids;
    }

    private static IEnumerable<string> Ids(string output) =>
        output.Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);

    private void Add(string? text, bool error)
    {
        if (text is null)
        {
            return;
        }

        lock (gate)
        {
            Append(text, error);
        }
    }

    // callers hold the lock
    private void Append(string text, bool error)
    {
        lines.Add(new RunLine(dropped + lines.Count, text, error));
        if (lines.Count > MaxLines)
        {
            lines.RemoveRange(0, lines.Count - MaxLines);
            dropped = lines[0].N;
        }
    }
}
