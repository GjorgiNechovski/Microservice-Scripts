using System.Globalization;
using System.Text;
using System.Text.Json;
using MicroserviceScripts.Ui.Infrastructure;

namespace MicroserviceScripts.Ui.Compose;

/// <summary>What goes into observability/.env.</summary>
/// <param name="AppNetwork">The docker network of the app, which the stack joins.</param>
/// <param name="Traces">Send traces to Honeycomb; without, everything stays on this machine (metrics only).</param>
/// <param name="HoneycombKey">A new key; null or empty keeps the one already in the file.</param>
/// <param name="ScrapeDir">A folder of extra Prometheus scrape jobs (a gateway's), or null for none.</param>
/// <param name="Namespace">What Beyla reports services under.</param>
public sealed record MonitoringSettings(string AppNetwork, bool Traces, string? HoneycombKey, string? ScrapeDir, string? Namespace);

/// <summary>A Prometheus scrape target and whether it answers.</summary>
public sealed record ScrapeTarget(string Job, string Url, string Health, string? Error);

/// <param name="Settings">What observability/.env says now (the key is never sent back).</param>
/// <param name="HasKey">The file has a Honeycomb key that isn't the example's placeholder.</param>
/// <param name="Joined">The networks the running Prometheus is on: the app's must be one.</param>
/// <param name="Networks">The docker networks on this machine, to pick the app's from.</param>
public sealed record MonitoringStatus(
    bool EnvExists,
    MonitoringSettings? Settings,
    bool HasKey,
    bool PrometheusUp,
    IReadOnlyList<string> Joined,
    IReadOnlyList<ScrapeTarget> Targets,
    IReadOnlyList<string> Networks,
    string EnvPath,
    string Command);

/// <summary>
/// The monitoring stack in observability/: reads and writes its .env, says
/// whether it is up and what it scrapes. Starting it is <see cref="MonitoringStarter"/>'s.
/// </summary>
public sealed class Monitoring(RepoLocation repo, IProcessRunner runner, HttpClient http)
{
    public const string PrometheusUrl = "http://localhost:9090";
    private const string Placeholder = "your_honeycomb_ingest_key_here";
    private static readonly string[] Managed =
        ["APP_NETWORK", "OTEL_COLLECTOR_CONFIG", "HONEYCOMB_API_KEY", "SCRAPE_DIR", "BEYLA_SERVICE_NAMESPACE"];

    public string Directory => Path.Combine(repo.Root, "observability");

    public string EnvPath => Path.Combine(Directory, ".env");

    public ProjectSelection Project =>
        new(Directory, [Path.Combine(Directory, "docker-compose.observability.yaml")]);

    public async Task<MonitoringStatus> StatusAsync(CancellationToken ct)
    {
        var env = ReadEnv();
        var settings = env.Count == 0
            ? null
            : new MonitoringSettings(
                env.GetValueOrDefault("APP_NETWORK", ""),
                env.GetValueOrDefault("OTEL_COLLECTOR_CONFIG", "config.yaml") != "config.local.yaml",
                null,
                env.GetValueOrDefault("SCRAPE_DIR"),
                env.GetValueOrDefault("BEYLA_SERVICE_NAMESPACE"));
        var hasKey = env.TryGetValue("HONEYCOMB_API_KEY", out var key) && key.Length > 0 && key != Placeholder;

        var up = await GetAsync($"{PrometheusUrl}/-/ready", ct) is not null;
        var targets = up ? await TargetsAsync(ct) : [];
        var joined = up ? await JoinedAsync(ct) : [];

        var listed = await runner.RunAsync("docker", ["network", "ls", "--format", "{{.Name}}"],
            timeout: TimeSpan.FromSeconds(20), cancellationToken: ct);
        var networks = listed.StdOut.Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Where(n => n is not ("bridge" or "host" or "none") && !n.StartsWith("microservice-observability", StringComparison.Ordinal))
            .Order(StringComparer.Ordinal)
            .ToList();

        return new MonitoringStatus(File.Exists(EnvPath), settings, hasKey, up, joined, targets, networks, EnvPath,
            AppControl.Command(Project));
    }

    /// <summary>The .env the settings make: the settings' keys, and every other line of the current file kept.</summary>
    public string EnvText(MonitoringSettings settings, DateTime? now = null)
    {
        var current = ReadEnv();
        var key = !string.IsNullOrWhiteSpace(settings.HoneycombKey)
            ? settings.HoneycombKey.Trim()
            : current.GetValueOrDefault("HONEYCOMB_API_KEY", Placeholder);
        var date = (now ?? DateTime.Now).ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);
        var sb = new StringBuilder()
            .AppendLine($"# Written by the Microservice-Scripts setup app on {date}.")
            .AppendLine("# .env.example next to it explains every value. This file is git-ignored.")
            .AppendLine()
            .AppendLine("# The docker network of the app; the stack joins it.")
            .AppendLine($"APP_NETWORK={settings.AppNetwork.Trim()}")
            .AppendLine();
        if (settings.Traces)
        {
            sb.AppendLine("# Traces go to Honeycomb; metrics stay in Prometheus.")
                .AppendLine($"HONEYCOMB_API_KEY={key}");
        }
        else
        {
            sb.AppendLine("# Metrics only, all on this machine: nothing is sent to Honeycomb.")
                .AppendLine("OTEL_COLLECTOR_CONFIG=config.local.yaml");
            if (key is not (Placeholder or ""))
            {
                sb.AppendLine("# Not used while the line above is there; kept for turning traces back on.")
                    .AppendLine($"HONEYCOMB_API_KEY={key}");
            }
        }

        if (!string.IsNullOrWhiteSpace(settings.ScrapeDir))
        {
            sb.AppendLine()
                .AppendLine("# Extra scrape jobs for this system (its gateway's metrics).")
                .AppendLine($"SCRAPE_DIR={settings.ScrapeDir.Trim()}");
        }

        if (!string.IsNullOrWhiteSpace(settings.Namespace))
        {
            sb.AppendLine($"BEYLA_SERVICE_NAMESPACE={settings.Namespace.Trim()}");
        }

        var kept = current.Where(e => !Managed.Contains(e.Key)).ToList();
        if (kept.Count > 0)
        {
            sb.AppendLine().AppendLine("# Kept from the file before.");
            kept.ForEach(e => sb.AppendLine($"{e.Key}={e.Value}"));
        }

        return sb.ToString();
    }

    /// <summary>The problems with the settings, in words for the user.</summary>
    public IReadOnlyList<string> Problems(MonitoringSettings settings)
    {
        var problems = new List<string>();
        if (string.IsNullOrWhiteSpace(settings.AppNetwork) || settings.AppNetwork.Any(char.IsWhiteSpace))
        {
            problems.Add("Pick the docker network your app runs on.");
        }

        if (settings.Traces && string.IsNullOrWhiteSpace(settings.HoneycombKey)
            && ReadEnv().GetValueOrDefault("HONEYCOMB_API_KEY", Placeholder) is Placeholder or "")
        {
            problems.Add("Give a Honeycomb key for the traces, or keep everything on this machine.");
        }

        if (!string.IsNullOrWhiteSpace(settings.ScrapeDir)
            && !System.IO.Directory.Exists(Path.GetFullPath(Path.Combine(Directory, settings.ScrapeDir.Trim()))))
        {
            problems.Add($"There's no folder {settings.ScrapeDir} (it is read from observability/).");
        }

        // each value is one line of the .env: a line break in one would add settings of its own
        foreach (var (name, value) in new[]
                 {
                     ("The Honeycomb key", settings.HoneycombKey), ("The scrape folder", settings.ScrapeDir),
                     ("The service namespace", settings.Namespace),
                 })
        {
            if (value is not null && value.IndexOfAny(['\r', '\n']) >= 0)
            {
                problems.Add($"{name} can't span lines.");
            }
        }

        return problems;
    }

    /// <summary>Writes the .env; the file it replaces is kept as a dated .bak.</summary>
    public string? Write(MonitoringSettings settings) =>
        Spec.SpecStore.WriteKeepingOld(EnvPath, EnvText(settings));

    private Dictionary<string, string> ReadEnv()
    {
        var env = new Dictionary<string, string>();
        if (!File.Exists(EnvPath))
        {
            return env;
        }

        foreach (var raw in File.ReadAllLines(EnvPath))
        {
            var line = raw.Trim();
            var eq = line.IndexOf('=');
            if (line.Length == 0 || line.StartsWith('#') || eq <= 0)
            {
                continue;
            }

            env[line[..eq].Trim()] = line[(eq + 1)..].Trim();
        }

        return env;
    }

    private async Task<IReadOnlyList<ScrapeTarget>> TargetsAsync(CancellationToken ct)
    {
        try
        {
            using var doc = JsonDocument.Parse(await http.GetStringAsync($"{PrometheusUrl}/api/v1/targets", ct));
            return doc.RootElement.GetProperty("data").GetProperty("activeTargets").EnumerateArray()
                .Select(t => new ScrapeTarget(
                    t.GetProperty("labels").TryGetProperty("job", out var job) ? job.GetString() ?? "" : "",
                    t.TryGetProperty("scrapeUrl", out var url) ? url.GetString() ?? "" : "",
                    t.TryGetProperty("health", out var health) ? health.GetString() ?? "unknown" : "unknown",
                    t.TryGetProperty("lastError", out var error) && error.GetString() is { Length: > 0 } e ? e : null))
                .OrderBy(t => t.Job, StringComparer.Ordinal)
                .ToList();
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or JsonException
                                       or KeyNotFoundException or InvalidOperationException)
        {
            return [];
        }
    }

    private async Task<IReadOnlyList<string>> JoinedAsync(CancellationToken ct)
    {
        var id = await runner.RunAsync("docker",
            ["ps", "-q", "--filter", "label=com.docker.compose.project=microservice-observability",
             "--filter", "label=com.docker.compose.service=prometheus"],
            timeout: TimeSpan.FromSeconds(20), cancellationToken: ct);
        var container = id.StdOut.Split('\n', StringSplitOptions.RemoveEmptyEntries).FirstOrDefault()?.Trim();
        if (container is null)
        {
            return [];
        }

        var inspect = await runner.RunAsync("docker",
            ["inspect", container, "--format", "{{json .NetworkSettings.Networks}}"],
            timeout: TimeSpan.FromSeconds(20), cancellationToken: ct);
        try
        {
            using var doc = JsonDocument.Parse(inspect.StdOut);
            return doc.RootElement.EnumerateObject().Select(n => n.Name).ToList();
        }
        catch (JsonException)
        {
            return [];
        }
    }

    private async Task<int?> GetAsync(string url, CancellationToken ct)
    {
        try
        {
            using var wait = CancellationTokenSource.CreateLinkedTokenSource(ct);
            wait.CancelAfter(TimeSpan.FromSeconds(5));
            using var response = await http.GetAsync(url, wait.Token);
            return (int)response.StatusCode;
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            return null;
        }
    }
}
