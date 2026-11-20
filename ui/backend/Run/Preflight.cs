using System.Text.Json;
using MicroserviceScripts.Ui.Compose;
using MicroserviceScripts.Ui.Infrastructure;

namespace MicroserviceScripts.Ui.Run;

/// <param name="Status">"ok", "wait" (still starting: check again shortly), "warn" (it can still run) or "fail" (it can't).</param>
public sealed record PreflightCheck(string Key, string Label, string Status, string Detail);

/// <param name="FaultPlan">What fault injection would stop (the analyzer's own dry run), as text.</param>
public sealed record PreflightResult(IReadOnlyList<PreflightCheck> Checks, string? FaultPlan, bool CanStart);

/// <summary>
/// What a run needs, checked before starting it: the spec, the app, the
/// monitoring stack and its metrics, the load generator. Everything here only
/// reads; nothing is started or stopped.
/// </summary>
public sealed class Preflight(IProcessRunner runner, PythonLocator python, RepoLocation repo, ComposeCli compose, HttpClient http)
{
    public const string PrometheusUrl = "http://localhost:9090";
    private static readonly TimeSpan PythonTimeout = TimeSpan.FromSeconds(60);

    /// <summary>How long a freshly started cAdvisor gets before its missing readings count as a problem.</summary>
    public static readonly TimeSpan CadvisorWarmUp = TimeSpan.FromMinutes(2);

    public async Task<PreflightResult> CheckAsync(string specPath, string? baseUrl, CancellationToken ct)
    {
        var checks = new List<PreflightCheck>();
        var py = await python.FindAsync(ct);
        if (py is null)
        {
            checks.Add(new("python", "Python", "fail", "Python 3.11 or newer wasn't found."));
            return new PreflightResult(checks, null, false);
        }

        // the spec, as the analyzer reads it
        var spec = await runner.RunAsync(py.FileName, py.Arguments("-m", "analyzer.spec", specPath, "--json"),
            repo.Root, PythonTimeout, ct);
        var projects = new List<string>();
        try
        {
            using var verdict = JsonDocument.Parse(spec.StdOut.Split('\n', StringSplitOptions.RemoveEmptyEntries).LastOrDefault() ?? "{}");
            var root = verdict.RootElement;
            if (root.TryGetProperty("ok", out var ok) && ok.GetBoolean())
            {
                projects.AddRange(root.GetProperty("compose_projects").EnumerateArray().Select(p => p.GetString()!));
                checks.Add(new("spec", "The spec", "ok",
                    $"{specPath}: {root.GetProperty("services").GetInt32()} services, "
                    + $"{root.GetProperty("operations").GetInt32()} features."));
            }
            else
            {
                checks.Add(new("spec", "The spec", "fail",
                    root.TryGetProperty("error", out var e) ? e.GetString() ?? "invalid" : $"Couldn't read {specPath}."));
            }
        }
        catch (JsonException)
        {
            checks.Add(new("spec", "The spec", "fail", $"The analyzer couldn't check it: {spec.Error}"));
        }

        // the app itself
        var running = await compose.ListAsync(ct);
        foreach (var name in projects)
        {
            var project = running.FirstOrDefault(p => p.Name == name);
            checks.Add(project is not null && project.Status.Contains("running", StringComparison.OrdinalIgnoreCase)
                ? new("app:" + name, $"Your app ({name})", "ok", project.Status)
                : new("app:" + name, $"Your app ({name})", "fail", project is null
                    ? $"The compose project '{name}' isn't on this machine: start it (docker compose up -d) first."
                    : $"It is {project.Status}: start it (docker compose up -d) first."));
        }

        if (!string.IsNullOrWhiteSpace(baseUrl))
        {
            checks.Add(await ReachableAsync(baseUrl, ct));
        }

        // the monitoring stack, and what it already sees
        var prometheus = await GetAsync($"{PrometheusUrl}/-/ready", ct);
        if (prometheus is null)
        {
            checks.Add(new("prometheus", "The monitoring stack", "fail",
                "Prometheus doesn't answer at localhost:9090. In Microservice-Scripts, fill in observability/.env "
                + "(see observability/.env.example), then: docker compose -f observability/docker-compose.observability.yaml up -d"));
        }
        else
        {
            checks.Add(new("prometheus", "The monitoring stack", "ok", "Prometheus answers at localhost:9090."));
            checks.AddRange(await MetricsAsync(py, specPath, ct));
        }

        // the load generator's image: pulled on the first run otherwise
        var k6 = await runner.RunAsync("docker", ["image", "inspect", "grafana/k6:latest", "--format", "{{.Id}}"],
            timeout: TimeSpan.FromSeconds(20), cancellationToken: ct);
        checks.Add(k6.Ok
            ? new("k6", "The load generator (k6)", "ok", "Its Docker image is on this machine.")
            : new("k6", "The load generator (k6)", "warn", "Its Docker image (grafana/k6) is downloaded on the first run."));

        var plan = await runner.RunAsync(py.FileName, py.Arguments("-m", "analyzer.chaos", specPath),
            repo.Root, PythonTimeout, ct);
        return new PreflightResult(checks, plan.Ok ? plan.StdOut.TrimEnd() : plan.Error,
            checks.All(c => c.Status != "fail"));
    }

    private async Task<IEnumerable<PreflightCheck>> MetricsAsync(PythonCommand py, string specPath, CancellationToken ct)
    {
        var result = await runner.RunAsync(py.FileName,
            py.Arguments("-m", "analyzer.collect", specPath, "--discover", "--json"), repo.Root, PythonTimeout, ct);
        try
        {
            using var doc = JsonDocument.Parse(result.StdOut.Split('\n', StringSplitOptions.RemoveEmptyEntries).LastOrDefault() ?? "{}");
            var d = doc.RootElement;
            if (!d.TryGetProperty("request_metric", out var request))
            {
                return [new("metrics", "Request metrics", "warn", $"Couldn't ask Prometheus: {result.Error}")];
            }

            var checks = new List<PreflightCheck>();
            if (d.GetProperty("has_cadvisor").GetBoolean())
            {
                checks.Add(new("cadvisor", "Container metrics (cAdvisor)", "ok", "Replicas and CPU can be measured."));
            }
            else if (await CadvisorUpForAsync(ct) is { } up && up < CadvisorWarmUp)
            {
                // just started: Prometheus hasn't had its first reading yet
                checks.Add(new("cadvisor", "Container metrics (cAdvisor)", "wait",
                    $"Starting (up for {up.TotalSeconds:0} s): its first readings reach Prometheus within a minute."));
            }
            else
            {
                checks.Add(new("cadvisor", "Container metrics (cAdvisor)", "warn",
                    "cAdvisor isn't reporting: replica counts and CPU won't be measured."));
            }
            if (request.ValueKind == JsonValueKind.Null)
            {
                checks.Add(new("metrics", "Request metrics", "warn",
                    "Prometheus has no request metrics yet. Before any traffic that can be normal; if none appear "
                    + "during the run, the services need a metrics-exporting gateway or OpenTelemetry (CLAUDE.md, step 2), "
                    + "and arrival and service rates won't be measured."));
            }
            else
            {
                var seen = d.GetProperty("traffic_services").EnumerateArray().Select(s => s.GetString()).ToList();
                checks.Add(new("metrics", "Request metrics", "ok",
                    $"{request.GetString()}, by {d.GetProperty("service_label").GetString()}"
                    + (seen.Count > 0 ? $"; traffic seen for {string.Join(", ", seen)}." : "; no traffic seen yet.")));
            }

            return checks;
        }
        catch (JsonException)
        {
            return [new("metrics", "Request metrics", "warn", $"Couldn't ask Prometheus: {result.Error}")];
        }
    }

    /// <summary>How long the monitoring stack's cAdvisor has been running; null when it isn't.</summary>
    private async Task<TimeSpan?> CadvisorUpForAsync(CancellationToken ct)
    {
        var id = await runner.RunAsync("docker",
            ["ps", "-q", "--filter", "label=com.docker.compose.project=microservice-observability",
             "--filter", "label=com.docker.compose.service=cadvisor"],
            timeout: TimeSpan.FromSeconds(20), cancellationToken: ct);
        var container = id.StdOut.Split('\n', StringSplitOptions.RemoveEmptyEntries).FirstOrDefault()?.Trim();
        if (container is null)
        {
            return null;
        }

        var started = await runner.RunAsync("docker", ["inspect", container, "--format", "{{.State.StartedAt}}"],
            timeout: TimeSpan.FromSeconds(20), cancellationToken: ct);
        return UpFor(started.StdOut, DateTimeOffset.UtcNow);
    }

    /// <summary>How long ago Docker's StartedAt was; null when it can't be read.</summary>
    public static TimeSpan? UpFor(string startedAt, DateTimeOffset now)
    {
        // Docker writes nanoseconds; DateTimeOffset reads seven digits at most
        var text = System.Text.RegularExpressions.Regex.Replace(startedAt.Trim(), @"(\.\d{7})\d+", "$1");
        return DateTimeOffset.TryParse(text, System.Globalization.CultureInfo.InvariantCulture,
            System.Globalization.DateTimeStyles.AssumeUniversal, out var at)
            ? now - at
            : null;
    }

    private async Task<PreflightCheck> ReachableAsync(string baseUrl, CancellationToken ct)
    {
        if (!Uri.TryCreate(baseUrl, UriKind.Absolute, out var uri))
        {
            return new("base", "Where the load goes", "fail", $"'{baseUrl}' isn't an address.");
        }

        var status = await GetAsync(uri.ToString(), ct);
        return status is null
            ? new("base", "Where the load goes", "fail", $"Nothing answers at {baseUrl}.")
            : new("base", "Where the load goes", "ok", $"{baseUrl} answers ({status}).");
    }

    /// <summary>The status an address answers with, or null when nothing answers.</summary>
    private async Task<int?> GetAsync(string url, CancellationToken ct)
    {
        try
        {
            using var response = await http.GetAsync(url, ct);
            return (int)response.StatusCode;
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            return null;
        }
    }
}
