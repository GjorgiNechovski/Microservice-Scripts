using MicroserviceScripts.Ui.Run;

namespace MicroserviceScripts.Ui.Reports;

/// <summary>Whether a result is the one the setup app's run just made, or older than what the user has now.</summary>
public static class ResultFreshness
{
    /// <param name="run">The setup app's run, as it is now (the app remembers only its own, since it started).</param>
    /// <param name="specPath">The spec the user saved in this setup, if any.</param>
    public static ResultSummary Mark(ResultSummary result, RunStatus run, string? specPath)
    {
        var ours = run.OutDir is not null && Same(run.OutDir, result.Folder);
        var fromThisRun = ours && run.StartedAt is { } started && started <= result.WrittenAt
                          && run.State is not (RunState.Running or RunState.Stopping);

        // the app's own run here was stopped or failed before it wrote a new report
        var unfinished = result.UnfinishedAfter;
        if (ours && !fromThisRun && run.State is RunState.Stopped or RunState.Failed
            && run.StartedAt is { } at && at > result.WrittenAt)
        {
            unfinished = unfinished is { } u && u < at ? u : at;
        }

        // the spec the app's run reads (its results go next to it, in analysis/) was saved again since
        DateTimeOffset? specSaved = null;
        if (specPath is not null && File.Exists(specPath)
            && Same(Path.Combine(Path.GetDirectoryName(specPath)!, "analysis"), result.Folder))
        {
            var saved = new DateTimeOffset(File.GetLastWriteTimeUtc(specPath), TimeSpan.Zero);
            specSaved = saved > result.WrittenAt ? saved : null;
        }

        return result with
        {
            Current = fromThisRun && specSaved is null && unfinished is null,
            SpecSavedAt = specSaved,
            UnfinishedAfter = unfinished,
        };
    }

    private static bool Same(string a, string b) =>
        string.Equals(Path.TrimEndingDirectorySeparator(Path.GetFullPath(a)),
            Path.TrimEndingDirectorySeparator(Path.GetFullPath(b)), StringComparison.Ordinal);
}
