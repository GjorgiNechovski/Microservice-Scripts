using MicroserviceScripts.Ui.Reports;
using MicroserviceScripts.Ui.Run;

namespace MicroserviceScripts.Ui.Tests;

public sealed class ResultsTests : IDisposable
{
    private readonly DirectoryInfo root = Directory.CreateTempSubdirectory("results-");
    private readonly DateTime now = DateTime.UtcNow.AddMinutes(-30);

    public void Dispose() => root.Delete(recursive: true);

    private string Write(string relative, string text, DateTime? at = null)
    {
        var path = Path.Combine(root.FullName, relative);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.WriteAllText(path, text);
        File.SetLastWriteTimeUtc(path, at ?? now);
        return path;
    }

    private string Single(string folder, string system = "shop", DateTime? at = null)
    {
        // as the analyzer writes it: an empty line first
        Write($"{folder}/report.txt", $"\nANALYSIS REPORT -- {system}\nmeasurements: x\n", at);
        Write($"{folder}/report-explained.txt", $"PLAIN-LANGUAGE SUMMARY — {system}\n", at);
        Write($"{folder}/validity.txt", "Run validity -- single run: VALID\n", (at ?? now).AddMinutes(-5));
        return Path.Combine(root.FullName, folder);
    }

    [Fact]
    public void Runs_and_sweeps_are_found_but_not_dependencies_or_other_reports()
    {
        var single = Single("shop/perf/analysis");
        Write("shop/analysis/scaling-report.txt", "\nLOAD-SWEEP SCALING REPORT -- shop\n", now.AddDays(-2));
        Write("shop/analysis/load-50/report.txt", "ANALYSIS REPORT -- shop\n", now.AddDays(-2));
        Write("shop/analysis/load-10/report.txt", "ANALYSIS REPORT -- shop\n", now.AddDays(-2));
        Write("shop/node_modules/x/analysis/report.txt", "ANALYSIS REPORT -- not this\n");
        Write("shop/docs/report.txt", "Quarterly report\n");

        var found = AnalysisResults.Find([Path.Combine(root.FullName, "shop")]);

        Assert.Equal(2, found.Count);
        Assert.Equal(single, found[0].Folder);                       // newest first
        Assert.Equal(ResultKind.Single, found[0].Kind);
        Assert.Equal("shop", found[0].System);
        var sweep = found[1];
        Assert.Equal(ResultKind.Sweep, sweep.Kind);
        Assert.Equal([10, 50], sweep.Levels);                       // its levels aren't results of their own
        Assert.Null(sweep.UnfinishedAfter);
    }

    [Fact]
    public void A_single_run_is_read_with_each_files_own_time()
    {
        var folder = Single("analysis");
        Write("analysis/k6-output.txt",
            "  execution: local\n\nrunning (0m01.0s), 5/5 VUs, 10 complete\ndefault   [   2% ] 5/5 VUs  0m01.0s/1m00.0s\n\n"
            + "\u001b[32m█ TOTAL RESULTS\u001b[0m\n    http_reqs: 10\n", now.AddMinutes(-6));
        Write("analysis/layer1_utilization.png", "png");
        Write("analysis/layer3_importance_old_operation.png", "png", now.AddDays(-30));   // left from an older run

        var view = AnalysisResults.Read(folder)!;

        Assert.Null(view.Overview);
        var part = Assert.Single(view.Parts);
        Assert.Equal(["report-explained", "report", "validity", "k6-output"], part.Texts.Select(t => t.Name));
        Assert.All(part.Texts, t => Assert.False(t.OtherRun));
        Assert.Equal("  execution: local\n\n█ TOTAL RESULTS\n    http_reqs: 10\n", part.Texts.Single(t => t.Name == "k6-output").Text);
        Assert.False(part.Charts.Single(c => c.Name == "layer1_utilization.png").OtherRun);
        Assert.True(part.Charts.Single(c => c.Name == "layer3_importance_old_operation.png").OtherRun);
    }

    [Fact]
    public void A_later_run_that_didnt_finish_is_noticed()
    {
        var folder = Single("analysis");
        Write("analysis/k6-output.txt", "newer", now.AddMinutes(20));     // the load test of a run stopped later

        var summary = AnalysisResults.Summarize(folder)!;

        Assert.Equal(now.AddMinutes(20), summary.UnfinishedAfter!.Value.UtcDateTime, TimeSpan.FromSeconds(1));
        var k6 = AnalysisResults.Read(folder)!.Parts[0].Texts.Single(t => t.Name == "k6-output");
        Assert.False(k6.OtherRun);                                     // within the hour after: still shown, unflagged
    }

    [Fact]
    public void A_sweep_has_an_overview_and_a_part_per_level()
    {
        Write("analysis/scaling-report.txt", "LOAD-SWEEP SCALING REPORT -- shop\n");
        Write("analysis/scaling-report-explained.txt", "PLAIN-LANGUAGE SCALING SUMMARY -- shop\n");
        Write("analysis/scaling_throughput.png", "png");
        Single("analysis/load-5", at: now.AddMinutes(-1));
        Single("analysis/load-20", at: now.AddMinutes(-1));
        Write("analysis/load-20/comparison-explained.txt", "DID IT GET BETTER?\n", now.AddMinutes(-1));

        var view = AnalysisResults.Read(Path.Combine(root.FullName, "analysis"))!;

        Assert.Equal(ResultKind.Sweep, view.Summary.Kind);
        Assert.Equal(["scaling-report-explained", "scaling-report"], view.Overview!.Texts.Select(t => t.Name));
        Assert.Equal("scaling_throughput.png", Assert.Single(view.Overview.Charts).Name);
        Assert.Equal([5, 20], view.Parts.Select(p => p.Users));
        Assert.Contains(view.Parts[1].Texts, t => t.Name == "comparison-explained");
    }

    [Fact]
    public void Only_the_analyzers_charts_are_served()
    {
        var folder = Single("analysis");
        Write("analysis/layer2_downtime.png", "png");
        Write("analysis/secret.txt", "no");
        Write("elsewhere/photo.png", "png");
        Write("sweep/scaling-report.txt", "LOAD-SWEEP SCALING REPORT -- shop\n");
        Write("sweep/load-5/report.txt", "ANALYSIS REPORT -- shop\n");
        Write("sweep/load-5/layer1_utilization.png", "png");

        Assert.NotNull(AnalysisResults.Chart(folder, "layer2_downtime.png"));
        Assert.NotNull(AnalysisResults.Chart(Path.Combine(root.FullName, "sweep", "load-5"), "layer1_utilization.png"));
        Assert.Null(AnalysisResults.Chart(folder, "secret.txt"));
        Assert.Null(AnalysisResults.Chart(folder, "../elsewhere/photo.png"));
        Assert.Null(AnalysisResults.Chart(Path.Combine(root.FullName, "elsewhere"), "photo.png"));
        Assert.Null(AnalysisResults.Chart(folder, "missing.png"));
    }

    private static RunStatus Run(RunState state, string? outDir, DateTimeOffset? started) =>
        new(state, null, true, outDir, started, null, 0, [], 0, []);

    [Fact]
    public void Results_are_current_only_when_this_sessions_run_wrote_them()
    {
        var folder = Single("perf/analysis");
        var spec = Write("perf/shop.toml", "name = 'shop'", now.AddMinutes(-40));
        var result = AnalysisResults.Summarize(folder)!;
        var started = new DateTimeOffset(now.AddMinutes(-10));

        var fresh = ResultFreshness.Mark(result, Run(RunState.Finished, folder, started), spec);
        Assert.True(fresh.Current);
        Assert.Null(fresh.SpecSavedAt);

        // nothing ran in this session (or it ran elsewhere)
        Assert.False(ResultFreshness.Mark(result, Run(RunState.Idle, null, null), spec).Current);
        Assert.False(ResultFreshness.Mark(result, Run(RunState.Finished, Path.Combine(root.FullName, "x"), started), spec).Current);

        // the run before these results was this session's, but they came from an earlier one
        Assert.False(ResultFreshness.Mark(result, Run(RunState.Finished, folder, started.AddHours(1)), spec).Current);

        // a run here was stopped after these results were written
        var stopped = ResultFreshness.Mark(result, Run(RunState.Stopped, folder, started.AddMinutes(20)), spec);
        Assert.False(stopped.Current);
        Assert.Equal(started.AddMinutes(20), stopped.UnfinishedAfter);

        // the spec was saved again after the run
        File.SetLastWriteTimeUtc(spec, now.AddMinutes(5));
        var edited = ResultFreshness.Mark(result, Run(RunState.Finished, folder, started), spec);
        Assert.False(edited.Current);
        Assert.NotNull(edited.SpecSavedAt);
    }
}
