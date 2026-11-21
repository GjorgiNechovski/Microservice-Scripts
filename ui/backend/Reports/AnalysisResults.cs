using System.Text.RegularExpressions;

namespace MicroserviceScripts.Ui.Reports;

public enum ResultKind
{
    Single,
    Sweep,
}

/// <summary>A finished analysis found on disk: a single run, or a load sweep over several user counts.</summary>
/// <param name="Folder">The folder the analyzer wrote it to (its <c>--out</c>).</param>
/// <param name="System">The system's name, as the report names it.</param>
/// <param name="WrittenAt">When the report was written: the end of that run.</param>
/// <param name="Levels">A sweep's user counts, one folder (load-N) each.</param>
/// <param name="UnfinishedAfter">When a later run in the same folder started that didn't finish, if one did.</param>
/// <param name="Current">The setup app's own run wrote it since the app started, and nothing it reads changed since.</param>
/// <param name="SpecSavedAt">The spec these results belong to was saved after them, at this time.</param>
public sealed record ResultSummary(
    string Folder,
    ResultKind Kind,
    string? System,
    DateTimeOffset WrittenAt,
    IReadOnlyList<int> Levels,
    DateTimeOffset? UnfinishedAfter,
    bool Current = false,
    DateTimeOffset? SpecSavedAt = null);

/// <param name="OtherRun">Written by a different run than the report next to it (left over, or from a later run).</param>
public sealed record ResultText(string Name, string Text, DateTimeOffset WrittenAt, bool OtherRun);

/// <param name="Name">The image's file name in <see cref="ResultPart.Folder"/>.</param>
public sealed record ResultChart(string Name, DateTimeOffset WrittenAt, bool OtherRun);

/// <summary>A single run, one level of a sweep, or a sweep's comparison across its levels.</summary>
/// <param name="Users">The level's user count; null for a single run and a sweep's overview.</param>
public sealed record ResultPart(string Folder, int? Users, IReadOnlyList<ResultText> Texts, IReadOnlyList<ResultChart> Charts);

/// <param name="Overview">A sweep's comparison across its levels; null for a single run.</param>
/// <param name="Parts">The single run, or one per level of a sweep.</param>
public sealed record ResultView(ResultSummary Summary, ResultPart? Overview, IReadOnlyList<ResultPart> Parts);

/// <summary>
/// Finds and reads what the analyzer wrote: its reports (each with a
/// plain-language version), its trust check and its charts. Only reads, and
/// only files the analyzer names itself.
/// </summary>
public static partial class AnalysisResults
{
    public const string Report = "report.txt";
    public const string ScalingReport = "scaling-report.txt";

    /// <summary>The text files a run or a level writes, in the order the page reads them.</summary>
    public static readonly IReadOnlyList<string> PartTexts =
    [
        "report-explained.txt", "report.txt", "validity-explained.txt", "validity.txt", "hotspots-explained.txt",
        "hotspots.txt", "comparison-explained.txt", "comparison.txt", "k6-output.txt",
    ];

    public static readonly IReadOnlyList<string> SweepTexts = ["scaling-report-explained.txt", "scaling-report.txt"];

    /// <summary>Written by the load test, before the report: a newer one belongs to a later run.</summary>
    private static readonly string[] LoadTestFiles = ["k6-output.txt", "validity.txt", "hotspots.txt", "log-signals.json"];

    /// <summary>
    /// How far a file may be from its report's time and still be the same run's:
    /// the load test comes first, then fault injection (long for many
    /// components), then the report; the charts follow it.
    /// </summary>
    private static readonly TimeSpan Before = TimeSpan.FromHours(6);

    private static readonly TimeSpan After = TimeSpan.FromHours(1);

    private static readonly TimeSpan Slack = TimeSpan.FromMinutes(2);

    private const long MaxText = 2_000_000;

    private static readonly HashSet<string> SkipFolders = new(StringComparer.OrdinalIgnoreCase)
    {
        "node_modules", "bin", "obj", "venv", "__pycache__", "dist", "build", "target", "vendor", "wwwroot",
        "packages", "site-packages", "Pods", "DerivedData",
    };

    [GeneratedRegex(@"^load-(\d+)$")]
    private static partial Regex LevelFolder();

    [GeneratedRegex(@"^levels tested \(virtual users\):\s*([\d,\s]+)$")]
    private static partial Regex TestedLine();

    /// <summary>
    /// A plain file name ending in .png: no folder in it, not hidden. The
    /// analyzer names per-feature charts after the operation, which can hold
    /// spaces or non-ASCII letters.
    /// </summary>
    private static bool IsChartName(string name) =>
        name.EndsWith(".png", StringComparison.OrdinalIgnoreCase)
        && name == Path.GetFileName(name)
        && !name.StartsWith('.')
        && name.IndexOfAny(['/', '\\', '\0']) < 0;

    [GeneratedRegex(@"^\s*(running \(|default\s|\S+\s+\[\s*\d+%\s*\])")]
    private static partial Regex K6Progress();

    [GeneratedRegex(@"\x1B\[[0-9;?]*[ -/]*[@-~]")]
    private static partial Regex Ansi();

    /// <summary>
    /// Every result in <paramref name="exact"/> (checked as they are, first:
    /// where the app's run writes) and under <paramref name="roots"/>, a few
    /// folders deep, newest first. Each root has its own budget of folders, so
    /// one big folder can't starve the rest; dependency and build folders are
    /// not searched, and a result's own folders aren't searched for more.
    /// </summary>
    public static IReadOnlyList<ResultSummary> Find(
        IEnumerable<string> roots, IEnumerable<string?>? exact = null, int depth = 3, int maxFoldersPerRoot = 3000)
    {
        var found = new Dictionary<string, ResultSummary>(StringComparer.Ordinal);
        foreach (var folder in (exact ?? []).OfType<string>().Where(f => !string.IsNullOrWhiteSpace(f)).Select(Full))
        {
            if (Directory.Exists(folder) && Summarize(folder) is { } result)
            {
                found[folder] = result;
            }
        }

        var seen = new HashSet<string>(found.Keys, StringComparer.Ordinal);
        foreach (var root in roots.Where(r => !string.IsNullOrWhiteSpace(r)).Select(Full).Distinct())
        {
            var visited = 0;
            var queue = new Queue<(string Folder, int Depth)>();
            queue.Enqueue((root, 0));
            while (queue.Count > 0 && visited < maxFoldersPerRoot)
            {
                var (folder, level) = queue.Dequeue();
                if (!seen.Add(folder) || !Directory.Exists(folder))
                {
                    continue;
                }

                visited++;
                if (Summarize(folder) is { } result)
                {
                    found[folder] = result;
                    continue;
                }

                if (level >= depth)
                {
                    continue;
                }

                foreach (var sub in Subfolders(folder))
                {
                    queue.Enqueue((sub, level + 1));
                }
            }
        }

        return found.Values.OrderByDescending(r => r.WrittenAt).ToList();
    }

    /// <summary>What the analyzer left in <paramref name="folder"/>; null when it isn't one of its result folders.</summary>
    public static ResultSummary? Summarize(string folder)
    {
        folder = Full(folder);
        var single = Header(Path.Combine(folder, Report), "ANALYSIS REPORT");
        var sweep = Header(Path.Combine(folder, ScalingReport), "LOAD-SWEEP SCALING REPORT");
        if (single is null && sweep is null)
        {
            return null;
        }

        // both kinds were written here at some point: the newer is the result
        if (sweep is not null && (single is null || sweep.Value.At >= single.Value.At))
        {
            var levels = Levels(folder);
            var loadTests = levels.SelectMany(l => LoadTestFiles.Select(f => Path.Combine(l.Folder, f)));
            return new ResultSummary(folder, ResultKind.Sweep, sweep.Value.System, sweep.Value.At,
                levels.Select(l => l.Users).ToList(), Later(loadTests, sweep.Value.At));
        }

        return new ResultSummary(folder, ResultKind.Single, single!.Value.System, single.Value.At, [],
            Later(LoadTestFiles.Select(f => Path.Combine(folder, f)), single.Value.At));
    }

    /// <summary>Everything the page shows of the result in <paramref name="folder"/>; null when there's none.</summary>
    public static ResultView? Read(string folder)
    {
        if (Summarize(folder) is not { } summary)
        {
            return null;
        }

        if (summary.Kind == ResultKind.Single)
        {
            return new ResultView(summary, null, [Part(summary.Folder, null, PartTexts, summary.WrittenAt)]);
        }

        var levels = Levels(summary.Folder)
            .Select(l => Part(l.Folder, l.Users, PartTexts,
                Written(Path.Combine(l.Folder, Report)) ?? summary.WrittenAt))
            .ToList();
        return new ResultView(summary, Part(summary.Folder, null, SweepTexts, summary.WrittenAt), levels);
    }

    /// <summary>
    /// The path of a chart in a result folder (or one of a sweep's level
    /// folders); null for anything else, so only the analyzer's images are served.
    /// </summary>
    public static string? Chart(string folder, string name)
    {
        if (!IsChartName(name))
        {
            return null;
        }

        folder = Full(folder);
        var isResult = Summarize(folder) is not null
                       || (LevelFolder().IsMatch(Path.GetFileName(folder))
                           && Summarize(Path.GetDirectoryName(folder)!) is { Kind: ResultKind.Sweep });
        var path = Path.Combine(folder, name);
        return isResult && File.Exists(path) ? path : null;
    }

    /// <summary>k6's output without its once-a-second progress lines: what it ran, and its summary.</summary>
    public static string K6Summary(string output)
    {
        var kept = new List<string>();
        foreach (var raw in Ansi().Replace(output, "").Replace("\r", "").Split('\n'))
        {
            var line = raw.TrimEnd();
            if (K6Progress().IsMatch(line) || (line.Length == 0 && kept.Count > 0 && kept[^1].Length == 0))
            {
                continue;
            }

            kept.Add(line);
        }

        return string.Join('\n', kept).Trim('\n') + "\n";
    }

    private static ResultPart Part(string folder, int? users, IEnumerable<string> names, DateTimeOffset reference)
    {
        bool Other(DateTimeOffset at) => at < reference - Before || at > reference + After;

        var texts = new List<ResultText>();
        foreach (var name in names)
        {
            var path = Path.Combine(folder, name);
            if (Written(path) is not { } at)
            {
                continue;
            }

            var text = ReadText(path);
            texts.Add(new ResultText(Path.GetFileNameWithoutExtension(name),
                name == "k6-output.txt" ? K6Summary(text) : text, at, Other(at)));
        }

        var charts = Directory.EnumerateFiles(folder, "*.png")
            .Select(p => (Name: Path.GetFileName(p), At: Written(p)!.Value))
            .Where(c => IsChartName(c.Name))
            .OrderBy(c => c.Name, StringComparer.Ordinal)
            .Select(c => new ResultChart(c.Name, c.At, Other(c.At)))
            .ToList();
        return new ResultPart(folder, users, texts, charts);
    }

    /// <summary>
    /// A sweep's levels: its load-N folders, only those its scaling report
    /// says it tested when it says so (an earlier sweep's other levels stay in
    /// the folder, and aren't this one's).
    /// </summary>
    private static List<(int Users, string Folder)> Levels(string folder)
    {
        var tested = Tested(Path.Combine(folder, ScalingReport));
        return Subfolders(folder)
            .Select(f => (Match: LevelFolder().Match(Path.GetFileName(f)), Folder: f))
            .Where(l => l.Match.Success && File.Exists(Path.Combine(l.Folder, Report)))
            .Select(l => (Users: int.Parse(l.Match.Groups[1].Value, System.Globalization.CultureInfo.InvariantCulture), l.Folder))
            .Where(l => tested is null || tested.Contains(l.Users))
            .OrderBy(l => l.Users)
            .ToList();
    }

    /// <summary>The "levels tested (virtual users): 5, 10, 20" line of a scaling report; null without one.</summary>
    private static HashSet<int>? Tested(string scalingReport)
    {
        try
        {
            foreach (var line in File.ReadLines(scalingReport).Take(10))
            {
                var m = TestedLine().Match(line);
                if (m.Success)
                {
                    return m.Groups[1].Value.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
                        .Select(v => int.TryParse(v, out var n) ? n : -1)
                        .Where(n => n >= 0)
                        .ToHashSet();
                }
            }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
        }

        return null;
    }

    /// <summary>When the first load-test file after the report was written: a later run that didn't get to write one.</summary>
    private static DateTimeOffset? Later(IEnumerable<string> files, DateTimeOffset report)
    {
        var later = files.Select(Written).OfType<DateTimeOffset>().Where(t => t > report + Slack).ToList();
        return later.Count > 0 ? later.Min() : null;
    }

    /// <summary>The system named in the report's first line, if it is the analyzer's report.</summary>
    private static (string? System, DateTimeOffset At)? Header(string path, string title)
    {
        if (Written(path) is not { } at)
        {
            return null;
        }

        // the analyzer starts its reports with an empty line
        string? first;
        try
        {
            using var reader = new StreamReader(path);
            var skipped = 0;
            do
            {
                first = reader.ReadLine()?.Trim();
            }
            while (first == "" && ++skipped < 5);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            return null;
        }

        if (first is null || !first.StartsWith(title, StringComparison.Ordinal))
        {
            return null;
        }

        var dash = first.IndexOf("--", StringComparison.Ordinal);
        var system = dash < 0 ? null : first[(dash + 2)..].Trim();
        return (string.IsNullOrEmpty(system) ? null : system, at);
    }

    private static string ReadText(string path)
    {
        try
        {
            if (new FileInfo(path).Length <= MaxText)
            {
                return File.ReadAllText(path);
            }

            // only k6's output grows this big: its summary is at the end
            using var stream = File.OpenRead(path);
            stream.Seek(-MaxText, SeekOrigin.End);
            using var reader = new StreamReader(stream);
            return reader.ReadToEnd();
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            return $"(couldn't be read: {ex.Message})";
        }
    }

    private static DateTimeOffset? Written(string path) =>
        File.Exists(path) ? new DateTimeOffset(File.GetLastWriteTimeUtc(path), TimeSpan.Zero) : null;

    private static IEnumerable<string> Subfolders(string folder)
    {
        try
        {
            return new DirectoryInfo(folder).EnumerateDirectories()
                .Where(d => !d.Name.StartsWith('.') && !SkipFolders.Contains(d.Name)
                            && !d.Attributes.HasFlag(FileAttributes.ReparsePoint))   // no symlink loops
                .Select(d => d.FullName)
                .Order(StringComparer.Ordinal)
                .ToList();
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            return [];
        }
    }

    private static string Full(string path) =>
        Path.TrimEndingDirectorySeparator(Path.GetFullPath(Setup.FolderBrowser.ExpandHome(path)));
}
