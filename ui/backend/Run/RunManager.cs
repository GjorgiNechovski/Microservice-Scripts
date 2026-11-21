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
    private RunState state = RunState.Idle;
    private string? specPath;
    private string? outDir;
    private bool faultInjection;
    private DateTimeOffset? startedAt;
    private DateTimeOffset? endedAt;
    private int? exitCode;
    private IReadOnlyList<string> projects = [];
    private IReadOnlyList<string> runningBefore = [];
    private string runId = "";

    /// <summary>The analyzer names its k6 containers after this (analyzer.collect.loadgen).</summary>
    public const string RunIdVariable = "MICROSERVICE_SCRIPTS_RUN_ID";

    /// <summary>
    /// The folder of the spec a run is using, while it runs: its spec and load
    /// script must not change under it (a sweep reads the script again per level).
    /// </summary>
    public string? BusyFolder()
    {
        lock (gate)
        {
            return state is RunState.Running or RunState.Stopping && specPath is not null
                ? Path.GetDirectoryName(specPath)
                : null;
        }
    }

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
        // its k6 containers are found by this, not by the pid: a launcher (py -3) runs Python under another one
        var id = Guid.NewGuid().ToString("N")[..12];
        info.Environment[RunIdVariable] = id;
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
            runId = id;
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
        string id;
        lock (gate)
        {
            stopped = state == RunState.Stopping;
            exitCode = exited.ExitCode;
            id = runId;
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
    /// again what of the app fault injection may have left down: containers
    /// running when the run began that were killed (exit 137) since. One that
    /// finished on its own (a seed job) or was stopped otherwise is left alone.
    /// </summary>
    private async Task PutBackAsync(string run)
    {
        var k6 = await runner.RunAsync("docker",
            ["ps", "-aq", "--filter", $"name=microservice-scripts-k6-{run}-"], timeout: TimeSpan.FromSeconds(20));
        foreach (var id in Ids(k6.StdOut))
        {
            await runner.RunAsync("docker", ["rm", "-f", id], timeout: TimeSpan.FromSeconds(30));
            Add($"[setup app] removed the load generator's container {id[..Math.Min(12, id.Length)]}", error: false);
        }

        IReadOnlyList<string> before, wanted;
        DateTimeOffset since;
        lock (gate)
        {
            before = runningBefore;
            wanted = projects;
            since = startedAt ?? DateTimeOffset.MinValue;
        }

        var now = await RunningContainersAsync(wanted, CancellationToken.None);
        foreach (var container in before.Except(now))
        {
            var shortId = container[..Math.Min(12, container.Length)];
            var state = await runner.RunAsync("docker",
                ["inspect", "--format", "{{.State.ExitCode}} {{.State.FinishedAt}}", container], timeout: TimeSpan.FromSeconds(20));
            if (!KilledSince(state.StdOut, since))
            {
                Add($"[setup app] left container {shortId} as it is: it stopped on its own, not killed by the run", error: false);
                continue;
            }

            var started = await runner.RunAsync("docker", ["start", container], timeout: TimeSpan.FromSeconds(60));
            Add(started.Ok
                    ? $"[setup app] started container {shortId} again: it was running when the run began"
                    : $"[setup app] couldn't start container {shortId} again: {started.Error}",
                error: !started.Ok);
        }
    }

    /// <summary>Whether `docker inspect`'s "exit-code finished-at" says it was killed (137) after <paramref name="since"/>.</summary>
    public static bool KilledSince(string inspected, DateTimeOffset since)
    {
        var parts = inspected.Trim().Split(' ', 2);
        if (parts.Length != 2 || parts[0] != "137")
        {
            return false;
        }

        // Docker's times carry nanoseconds, more digits than DateTimeOffset reads
        var at = System.Text.RegularExpressions.Regex.Replace(parts[1].Trim(), @"(\.\d{7})\d+", "$1");
        return DateTimeOffset.TryParse(at, System.Globalization.CultureInfo.InvariantCulture,
                   System.Globalization.DateTimeStyles.AssumeUniversal, out var finished)
               && finished >= since.AddSeconds(-1);
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
