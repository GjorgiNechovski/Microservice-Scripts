using MicroserviceScripts.Ui.Compose;
using MicroserviceScripts.Ui.Drafting;
using MicroserviceScripts.Ui.Infrastructure;
using MicroserviceScripts.Ui.Load;
using MicroserviceScripts.Ui.Reports;
using MicroserviceScripts.Ui.Routes;
using MicroserviceScripts.Ui.Run;
using MicroserviceScripts.Ui.Setup;
using MicroserviceScripts.Ui.Spec;

namespace MicroserviceScripts.Ui.Api;

public sealed record ScanRequest(IReadOnlyList<string> Folders, IReadOnlyList<string>? ExtraFiles);

public sealed record DraftRequest(IReadOnlyList<ProjectSelection> Projects);

/// <param name="Error">Compose's own message when it couldn't read the project.</param>
public sealed record ProjectReadResult(
    string Directory, IReadOnlyList<string> Files, string? Name, bool Ok, string? Error, int Services);

/// <param name="Routes">The routes found in the services' code, and how each is reached.</param>
public sealed record DraftResponse(IReadOnlyList<ProjectReadResult> Projects, DraftResult? Draft, RouteDraft? Routes = null);

/// <param name="Script">A k6 script to write from routes; null when the spec points at the user's own.</param>
public sealed record PreviewRequest(SpecDocument Spec, LoadScript? Script = null);

/// <param name="Script">Written next to the spec, under the name in Spec.Load.Script.</param>
/// <param name="Secret">The login password or the token, if the script needs one; used for these requests only.</param>
public sealed record TryRequest(LoadScript Script, string? Secret);

/// <param name="Secret">The password, if the login body has {{password}} in it; used for this request only.</param>
public sealed record LoginTryRequest(string BaseUrl, Auth Auth, string? Secret);

/// <param name="ComposeName">The name Compose gave the project when it was read.</param>
public sealed record AppProject(string Directory, IReadOnlyList<string> Files, string? Name, string ComposeName);

/// <param name="BaseUrl">The app's address, to check something answers there.</param>
public sealed record AppRequest(IReadOnlyList<AppProject> Projects, string? BaseUrl);

/// <param name="BaseUrl">Where the load test sends requests, to check something answers there.</param>
public sealed record PreflightRequest(string SpecPath, string? BaseUrl);

public sealed record SaveRequest(SpecDocument Spec, string Directory, string FileName, bool Overwrite, LoadScript? Script = null);

/// <param name="Folders">Where to look besides the app's usual places (the chosen projects, a folder the user opened).</param>
/// <param name="SpecPath">The spec saved in this setup, to tell whether its results are older than it.</param>
public sealed record ResultsRequest(IReadOnlyList<string>? Folders, string? SpecPath);

/// <param name="Searched">The folders looked in (and a few folders deep under each).</param>
public sealed record ResultList(IReadOnlyList<ResultSummary> Results, IReadOnlyList<string> Searched);

/// <summary>The HTTP API the setup page calls. Everything under /api needs the launch token.</summary>
public static class SetupApi
{
    public static void MapSetupApi(this WebApplication app)
    {
        var api = app.MapGroup("/api");

        api.MapGet("/prerequisites", (Prerequisites prerequisites, CancellationToken ct) =>
            prerequisites.CheckAsync(ct));

        api.MapGet("/folders", (string? path) =>
        {
            try
            {
                return Results.Ok(FolderBrowser.List(path));
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ArgumentException)
            {
                return Results.BadRequest(new { error = ex.Message });
            }
        });

        api.MapPost("/scan", (ScanRequest request) => FolderScanner.Scan(request.Folders, request.ExtraFiles));

        api.MapGet("/compose/projects", (ComposeCli compose, CancellationToken ct) => compose.ListAsync(ct));

        api.MapPost("/draft", Draft);

        api.MapPost("/spec/preview", (PreviewRequest request) =>
        {
            var problems = request.Spec.Problems().Concat(request.Script?.Problems() ?? []).ToList();
            return problems.Count > 0
                ? Results.UnprocessableEntity(new { problems })
                : Results.Ok(new
                {
                    toml = TomlSpecWriter.Write(request.Spec),
                    script = request.Script is null ? null : K6ScriptWriter.Write(request.Script, request.Spec.Name),
                });
        });

        api.MapPost("/spec/save", Save);

        api.MapPost("/routes/try", async (TryRequest request, RouteTester tester, CancellationToken ct) =>
        {
            var problems = request.Script.Problems();
            return problems.Count > 0
                ? Results.UnprocessableEntity(new { problems })
                : Results.Ok(await tester.TryAsync(request.Script, request.Secret, ct));
        });

        api.MapPost("/login/try", async (LoginTryRequest request, RouteTester tester, CancellationToken ct) =>
        {
            var problems = new LoadScript(request.BaseUrl, request.Auth, [new("x", "GET", "/", null, 1)]).Problems();
            if (request.Auth.Type != Auth.TypeLogin)
            {
                problems = [.. problems, "Only a login request can be sent on its own."];
            }

            return problems.Count > 0
                ? Results.UnprocessableEntity(new { problems })
                : Results.Ok(await tester.TryLoginAsync(request.BaseUrl, request.Auth, request.Secret, ct));
        });

        api.MapPost("/app/status", (AppRequest request, AppControl app, CancellationToken ct) =>
            app.StatusAsync(
                request.Projects.Select(p => (new ProjectSelection(p.Directory, p.Files, p.Name), p.ComposeName)).ToList(),
                request.BaseUrl, ct));

        api.MapPost("/app/start", (AppRequest request, AppStarter starter) =>
            starter.Start(request.Projects.Select(p => new ProjectSelection(p.Directory, p.Files, p.Name)).ToList()) is { } error
                ? Results.UnprocessableEntity(new { problems = new[] { error } })
                : Results.Ok(starter.Status(0)));

        api.MapGet("/app/start", (int? after, AppStarter starter) => starter.Status(after ?? 0));

        api.MapGet("/monitoring", (Monitoring monitoring, CancellationToken ct) => monitoring.StatusAsync(ct));

        api.MapPost("/monitoring/preview", (MonitoringSettings settings, Monitoring monitoring) => Results.Ok(new
        {
            // the key never goes back to the page
            env = System.Text.RegularExpressions.Regex.Replace(monitoring.EnvText(settings),
                @"(?m)^(HONEYCOMB_API_KEY=)(?!your_honeycomb_ingest_key_here$).+$", "$1••••••••"),
            problems = monitoring.Problems(settings),
        }));

        api.MapPost("/monitoring/start", (MonitoringSettings settings, Monitoring monitoring, MonitoringStarter starter) =>
        {
            var problems = monitoring.Problems(settings);
            if (problems.Count > 0)
            {
                return Results.UnprocessableEntity(new { problems });
            }

            try
            {
                monitoring.Write(settings);
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
            {
                return Results.UnprocessableEntity(new { problems = new[] { $"Couldn't write {monitoring.EnvPath}: {ex.Message}" } });
            }

            return starter.Start([monitoring.Project]) is { } error
                ? Results.UnprocessableEntity(new { problems = new[] { error } })
                : Results.Ok(starter.Status(0));
        });

        api.MapGet("/monitoring/start", (int? after, MonitoringStarter starter) => starter.Status(after ?? 0));

        api.MapPost("/run/check", (PreflightRequest request, Preflight preflight, CancellationToken ct) =>
            preflight.CheckAsync(Path.GetFullPath(FolderBrowser.ExpandHome(request.SpecPath)), request.BaseUrl, ct));

        api.MapGet("/run", (int? after, RunManager runs) => runs.Status(after ?? 0));

        api.MapPost("/run/start", async (StartRequest request, RunManager runs, CancellationToken ct) =>
            await runs.StartAsync(request, ct) is { } error
                ? Results.UnprocessableEntity(new { problems = new[] { error } })
                : Results.Ok(runs.Status(0)));

        api.MapPost("/run/stop", (RunManager runs) =>
        {
            // stopping can take a while (the analyzer puts things back first): the page follows it
            _ = runs.StopAsync();
            return Results.Ok(runs.Status(int.MaxValue));
        });

        api.MapPost("/results/find", FindResults);

        api.MapGet("/results", (string folder, string? spec, RunManager runs) =>
            AnalysisResults.Read(folder) is { } view
                ? Results.Ok(view with { Summary = ResultFreshness.Mark(view.Summary, runs.Status(int.MaxValue), FullPath(spec)) })
                : Results.NotFound(new { error = $"There are no analyzer results in {folder}." }));

        api.MapGet("/results/chart", (string folder, string name) =>
            AnalysisResults.Chart(folder, name) is { } path ? Results.File(path, "image/png") : Results.NotFound());
    }

    /// <summary>
    /// Results in the folders the page names, next to the saved spec, where
    /// the app's run wrote, in every compose project Docker knows, and in this
    /// repository (the analyzer's default --out is ./analysis).
    /// </summary>
    private static async Task<ResultList> FindResults(
        ResultsRequest request, ComposeCli compose, RunManager runs, RepoLocation repo, CancellationToken ct)
    {
        var spec = FullPath(request.SpecPath);
        var run = runs.Status(int.MaxValue);
        var roots = new List<string?>();
        roots.AddRange(request.Folders ?? []);
        roots.Add(spec is null ? null : Path.GetDirectoryName(spec));
        roots.Add(run.OutDir);
        roots.AddRange((await compose.ListAsync(ct)).Select(p => p.Directory));
        roots.Add(repo.Root);

        var searched = roots.Select(FullPath).OfType<string>().Distinct(StringComparer.Ordinal).ToList();
        var found = AnalysisResults.Find(searched).Select(r => ResultFreshness.Mark(r, run, spec)).ToList();
        return new ResultList(found, searched);
    }

    private static string? FullPath(string? path)
    {
        if (string.IsNullOrWhiteSpace(path))
        {
            return null;
        }

        try
        {
            return Path.TrimEndingDirectorySeparator(Path.GetFullPath(FolderBrowser.ExpandHome(path.Trim())));
        }
        catch (ArgumentException)
        {
            return null;
        }
    }

    private static async Task<IResult> Draft(
        DraftRequest request, ComposeCli compose, ComponentDrafter drafter, RouteDrafter routeDrafter, CancellationToken ct)
    {
        var reads = await Task.WhenAll(request.Projects.Select(p => compose.ReadAsync(p, ct)));
        var results = new List<ProjectReadResult>();
        var projects = new List<ComposeProject>();
        for (var i = 0; i < reads.Length; i++)
        {
            var (selection, (project, error)) = (request.Projects[i], reads[i]);
            if (project is not null && projects.Any(p => p.Name == project.Name))
            {
                // Compose would treat both as one project and mix their containers.
                error = $"Another chosen folder is also the compose project '{project.Name}'. "
                        + "Give this one its own project name (the name it's started with, `-p`).";
                project = null;
            }

            results.Add(new ProjectReadResult(
                selection.Directory, selection.Files, project?.Name, project is not null, error,
                project?.Services.Count ?? 0));
            if (project is not null)
            {
                projects.Add(project);
            }
        }

        if (projects.Count == 0)
        {
            return Results.Ok(new DraftResponse(results, null));
        }

        var images = projects.SelectMany(p => p.Services.Select(s => s.Image ?? $"{p.Name}-{s.Name}"));
        var withHealthcheck = await compose.ImagesWithHealthcheckAsync(images, ct);
        var draft = drafter.Draft(projects, withHealthcheck);
        return Results.Ok(new DraftResponse(results, draft, routeDrafter.Draft(projects, draft)));
    }

    private static async Task<IResult> Save(SaveRequest request, SpecChecker checker, CancellationToken ct)
    {
        var problems = request.Spec.Problems().Concat(request.Script?.Problems() ?? []).ToList();
        if (problems.Count > 0)
        {
            return Results.UnprocessableEntity(new { problems });
        }

        string path;
        string? scriptPath = null;
        var spec = request.Spec;
        try
        {
            path = SpecStore.Resolve(request.Directory, request.FileName);
            if (request.Script is not null)
            {
                if (spec.Load is null)
                {
                    return Results.UnprocessableEntity(new { problems = new[] { "Say how many users the load test runs." } });
                }

                scriptPath = SpecStore.Resolve(request.Directory, spec.Load.Script, ".js");
                spec = spec with { Load = spec.Load with { Script = Path.GetFileName(scriptPath) } };
            }
            else if (spec.Load is { } own)
            {
                // the user's own script: it must exist, and is written relative to the spec when it can be
                var folder = Path.GetDirectoryName(path)!;
                var full = Path.GetFullPath(Path.Combine(folder, FolderBrowser.ExpandHome(own.Script.Trim())));
                if (!File.Exists(full))
                {
                    return Results.UnprocessableEntity(new { problems = new[] { $"There's no k6 script at {full}." } });
                }

                var relative = Path.GetRelativePath(folder, full);
                spec = spec with
                {
                    Load = own with { Script = relative.StartsWith("..", StringComparison.Ordinal) ? full : relative.Replace('\\', '/') },
                };
            }
        }
        catch (ArgumentException ex)
        {
            return Results.UnprocessableEntity(new { problems = new[] { ex.Message } });
        }

        var existing = new[] { path, scriptPath }.OfType<string>().Where(File.Exists).ToList();
        if (existing.Count > 0 && !request.Overwrite)
        {
            return Results.Conflict(new { exists = true, path = existing[0], paths = existing });
        }

        var toml = TomlSpecWriter.Write(spec);
        var check = await checker.CheckAsync(toml, ct);
        if (!check.Ok)
        {
            return Results.UnprocessableEntity(new { problems = new[] { $"The analyzer rejects it: {check.Error}" } });
        }

        try
        {
            var saved = SpecStore.Write(path, toml);
            if (request.Script is not null && scriptPath is not null)
            {
                var backup = SpecStore.WriteKeepingOld(scriptPath, K6ScriptWriter.Write(request.Script, spec.Name));
                saved = saved with { ScriptPath = scriptPath, ScriptBackup = backup };
            }

            return Results.Ok(saved);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            return Results.UnprocessableEntity(new { problems = new[] { $"Couldn't write {path}: {ex.Message}" } });
        }
    }
}
