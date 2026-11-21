using System.Diagnostics;
using System.Text;
using System.Text.RegularExpressions;

namespace MicroserviceScripts.Ui.Compose;

/// <param name="State">"running", "partly" (some containers aren't), "stopped", or "missing" (never started).</param>
/// <param name="Status">Compose's own words, e.g. "running(10), exited(2)".</param>
public sealed record AppProjectStatus(string Name, string State, int Running, int NotRunning, string Status);

/// <param name="Running">Every project has containers running.</param>
/// <param name="BaseStatus">What the app's address answered; null when nothing answered.</param>
/// <param name="Commands">How to start it by hand, one command per project.</param>
public sealed record AppStatus(
    IReadOnlyList<AppProjectStatus> Projects, bool Running, string? BaseUrl, int? BaseStatus, IReadOnlyList<string> Commands);

/// <summary>Whether the user's app is up, and the command that starts it.</summary>
public sealed class AppControl(ComposeCli compose, HttpClient http)
{
    private static readonly Regex Counts = new(@"(?<state>\w+)\((?<n>\d+)\)");

    /// <param name="projects">The chosen projects with the names Compose gave them when they were read.</param>
    public async Task<AppStatus> StatusAsync(
        IReadOnlyList<(ProjectSelection Selection, string Name)> projects, string? baseUrl, CancellationToken ct)
    {
        var known = await compose.ListAsync(ct);
        var statuses = projects.Select(p =>
        {
            var listed = known.FirstOrDefault(k => k.Name == p.Name);
            if (listed is null)
            {
                return new AppProjectStatus(p.Name, "missing", 0, 0, "not started");
            }

            var counts = Counts.Matches(listed.Status).Select(m => (m.Groups["state"].Value, int.Parse(m.Groups["n"].Value))).ToList();
            var running = counts.Where(c => c.Item1 == "running").Sum(c => c.Item2);
            var other = counts.Where(c => c.Item1 != "running").Sum(c => c.Item2);
            var state = running == 0 ? "stopped" : other > 0 ? "partly" : "running";
            return new AppProjectStatus(p.Name, state, running, other, listed.Status);
        }).ToList();

        int? answered = null;
        if (!string.IsNullOrWhiteSpace(baseUrl) && Uri.TryCreate(baseUrl, UriKind.Absolute, out var uri))
        {
            try
            {
                using var wait = CancellationTokenSource.CreateLinkedTokenSource(ct);
                wait.CancelAfter(TimeSpan.FromSeconds(5));
                using var response = await http.GetAsync(uri, wait.Token);
                answered = (int)response.StatusCode;
            }
            catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
            {
                answered = null;
            }
        }

        return new AppStatus(
            statuses,
            statuses.Count > 0 && statuses.All(s => s.Running > 0),
            baseUrl,
            answered,
            projects.Select(p => Command(p.Selection)).ToList());
    }

    /// <summary>The command a person would type to start a project, from its own folder.</summary>
    public static string Command(ProjectSelection selection)
    {
        static string Quote(string s) => Regex.IsMatch(s, @"^[\w./:@+-]+$") ? s : $"\"{s}\"";
        var files = selection.Files.Select(f =>
        {
            var relative = Path.GetRelativePath(selection.Directory, f);
            return relative.StartsWith("..", StringComparison.Ordinal) ? f : relative.Replace('\\', '/');
        });
        var name = string.IsNullOrWhiteSpace(selection.Name) ? "" : $" -p {Quote(selection.Name)}";
        return $"cd {Quote(selection.Directory)}\ndocker compose {string.Join(" ", files.Select(f => $"-f {Quote(f)}"))}{name} up -d";
    }
}

public enum StartState
{
    Idle,
    Starting,
    Started,
    Failed,
}

public sealed record StartLine(int N, string Text);

public sealed record StartStatus(StartState State, IReadOnlyList<StartLine> Lines, int Next);

/// <summary>Starts the user's app, on their say-so.</summary>
public sealed class AppStarter : ComposeStarter;

/// <summary>Starts the monitoring stack (observability/), on the user's say-so.</summary>
public sealed class MonitoringStarter : ComposeStarter;

/// <summary>
/// Starts compose projects the way a person would (<c>docker compose up -d</c>
/// with the chosen files and project name), one after another, keeping
/// Compose's output for the page.
/// </summary>
public class ComposeStarter
{
    private const int MaxLines = 5000;
    private static readonly TimeSpan Limit = TimeSpan.FromMinutes(20);

    private readonly Lock gate = new();
    private readonly List<StartLine> lines = [];
    private int dropped;
    private StartState state = StartState.Idle;

    public StartStatus Status(int after)
    {
        lock (gate)
        {
            return new StartStatus(state, lines.Skip(Math.Max(0, after - dropped)).ToList(), dropped + lines.Count);
        }
    }

    public bool Starting
    {
        get
        {
            lock (gate)
            {
                return state == StartState.Starting;
            }
        }
    }

    /// <summary>Starts the projects in the background; returns why it couldn't, or null.</summary>
    public string? Start(IReadOnlyList<ProjectSelection> projects)
    {
        lock (gate)
        {
            if (state == StartState.Starting)
            {
                return "It is already being started.";
            }

            lines.Clear();
            dropped = 0;
            state = StartState.Starting;
        }

        _ = Task.Run(async () =>
        {
            var ok = true;
            foreach (var project in projects)
            {
                Add($"$ {AppControl.Command(project).Replace("\n", " && ", StringComparison.Ordinal)}");
                ok = await UpAsync(project);
                if (!ok)
                {
                    break;
                }
            }

            lock (gate)
            {
                state = ok ? StartState.Started : StartState.Failed;
            }
        });
        return null;
    }

    private async Task<bool> UpAsync(ProjectSelection project)
    {
        var args = ComposeCli.ProjectArguments(project);
        args.InsertRange(1, ["--progress", "plain"]);
        args.AddRange(["up", "-d"]);
        var info = new ProcessStartInfo("docker")
        {
            WorkingDirectory = project.Directory,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        foreach (var a in args)
        {
            info.ArgumentList.Add(a);
        }

        using var process = new Process { StartInfo = info };
        process.OutputDataReceived += (_, e) => Add(e.Data);
        process.ErrorDataReceived += (_, e) => Add(e.Data);
        try
        {
            process.Start();
        }
        catch (System.ComponentModel.Win32Exception ex)
        {
            Add($"couldn't run docker: {ex.Message}");
            return false;
        }

        process.BeginOutputReadLine();
        process.BeginErrorReadLine();
        using var limit = new CancellationTokenSource(Limit);
        try
        {
            await process.WaitForExitAsync(limit.Token);
        }
        catch (OperationCanceledException)
        {
            process.Kill(entireProcessTree: true);
            Add($"stopped waiting after {Limit.TotalMinutes:0} minutes");
            return false;
        }

        process.WaitForExit();   // the last output lines
        return process.ExitCode == 0;
    }

    private void Add(string? text)
    {
        if (string.IsNullOrWhiteSpace(text))
        {
            return;
        }

        lock (gate)
        {
            lines.Add(new StartLine(dropped + lines.Count, text));
            if (lines.Count > MaxLines)
            {
                lines.RemoveRange(0, lines.Count - MaxLines);
                dropped = lines[0].N;
            }
        }
    }
}
