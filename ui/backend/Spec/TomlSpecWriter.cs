using System.Globalization;
using System.Text;
using System.Text.RegularExpressions;

namespace MicroserviceScripts.Ui.Spec;

/// <summary>
/// Writes a spec as TOML a person can read and edit afterwards: commented,
/// in the order of systems/TEMPLATE.toml, optional tables only when they say
/// something.
/// </summary>
public static class TomlSpecWriter
{
    private static readonly Regex BareKey = new(@"^[A-Za-z0-9_-]+$");

    /// <param name="dockerDesktop">
    /// Docker runs in a VM (Docker Desktop on Windows and macOS): k6's container
    /// can't share this machine's network, so it reaches the app through
    /// host.docker.internal instead of localhost. The default is this machine's.
    /// </param>
    public static string Write(SpecDocument spec, DateTime? writtenAt = null, bool? dockerDesktop = null)
    {
        var inVm = dockerDesktop ?? !OperatingSystem.IsLinux();
        var date = (writtenAt ?? DateTime.Now).ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);
        var rule = "# " + new string('=', 77);
        var sb = new StringBuilder();
        sb.AppendLine(rule)
            .AppendLine($"# {OneLine(spec.Name)}")
            .AppendLine($"# Written by the Microservice-Scripts setup app on {date}. Edit it here or in")
            .AppendLine("# the app; systems/TEMPLATE.toml in Microservice-Scripts documents every option.")
            .AppendLine(rule)
            .AppendLine()
            .AppendLine($"name = {Str(spec.Name)}");

        if (spec.ComposeProjects.Count == 1)
        {
            sb.AppendLine()
                .AppendLine("# The compose project it runs as (`docker compose ls`).")
                .AppendLine($"compose_project = {Str(spec.ComposeProjects[0])}");
        }
        else if (spec.ComposeProjects.Count > 1)
        {
            sb.AppendLine()
                .AppendLine("# The compose projects it runs as, one per repository (`docker compose ls`).")
                .AppendLine($"compose_project = {Array(spec.ComposeProjects)}");
        }

        sb.AppendLine()
            .AppendLine("# Application services: each gets queueing analysis and needs request metrics.")
            .AppendLine($"services = {Array(spec.Services.Select(c => c.Name))}")
            .AppendLine()
            .AppendLine("# Shared infrastructure: gateways, databases, caches, brokers.")
            .AppendLine($"infrastructure = {Array(spec.Infrastructure.Select(c => c.Name))}")
            .AppendLine()
            .AppendLine("# User operations (features) and the components each one needs.")
            .AppendLine("[operations]");
        if (spec.Operations is { Count: > 0 } operations)
        {
            foreach (var (op, components) in operations)
            {
                sb.AppendLine($"{Key(op)} = {InlineArray(components)}");
            }
        }
        else
        {
            sb.AppendLine("# none yet: the setup app's Features step fills these in");
        }

        var containers = spec.Components
            .Select(c => spec.NeedsPin(c)
                ? $"{Key(c.Name)} = {{ project = {Str(c.Project)}, service = {Str(c.ComposeService)} }}"
                : c.Name != c.ComposeService ? $"{Key(c.Name)} = {Str(c.ComposeService)}" : null)
            .OfType<string>()
            .ToList();
        if (containers.Count > 0)
        {
            sb.AppendLine()
                .AppendLine("# Components whose compose service has another name, or lives in one")
                .AppendLine("# particular project.")
                .AppendLine("[containers]");
            containers.ForEach(line => sb.AppendLine(line));
        }

        var readiness = spec.Components.Where(c => !c.HasHealthcheck && c.ReadinessPort is not null).ToList();
        if (readiness.Count > 0)
        {
            var width = readiness.Max(c => $"{Key(c.Name)} = {c.ReadinessPort}".Length);
            sb.AppendLine()
                .AppendLine("# For components without a docker healthcheck: the TCP port that means")
                .AppendLine("# \"serving again\" after fault injection. Where each one came from is noted.")
                .AppendLine("[readiness]");
            foreach (var c in readiness)
            {
                var line = $"{Key(c.Name)} = {c.ReadinessPort}";
                sb.AppendLine(c.ReadinessSource is null
                    ? line
                    : $"{line.PadRight(width)}   # {OneLine(c.ReadinessSource)}");
            }
        }

        var instances = spec.Infrastructure.Where(c => c.Instances > 1).ToList();
        if (instances.Count > 0)
        {
            sb.AppendLine()
                .AppendLine("# Infrastructure running more than one instance WITH failover. Declaring")
                .AppendLine("# redundancy that doesn't exist makes every availability figure fiction.")
                .AppendLine("[instances]");
            foreach (var c in instances)
            {
                sb.AppendLine($"{Key(c.Name)} = {c.Instances}");
            }
        }

        if (spec.Load is { } load)
        {
            sb.AppendLine()
                .AppendLine("# The load test: k6 runs the script, holding each number of users for `duration`.")
                .AppendLine("# Several numbers make a sweep: a full analysis per level, then one comparing")
                .AppendLine("# them. Keep the same settings between runs you want to compare.")
                .AppendLine("[load]")
                .AppendLine($"script = {Str(load.Script)}")
                .AppendLine(load.Users.Count == 1
                    ? $"vus = {load.Users[0]}"
                    : $"vus = [{string.Join(", ", load.Users)}]")
                .AppendLine($"duration = {Str(load.Duration)}");
            var env = load.Env;
            if (inVm)
            {
                sb.AppendLine("# Docker Desktop: k6 can't use this machine's network, so it runs on Docker's")
                    .AppendLine("# own and reaches the app at host.docker.internal.")
                    .AppendLine("docker_network = \"bridge\"");
                env = env?.ToDictionary(e => e.Key, e => e.Key == "BASE_URL" ? FromAContainer(e.Value) : e.Value);
            }

            if (env is { Count: > 0 })
            {
                sb.AppendLine($"env = {{ {string.Join(", ", env.Select(e => $"{Key(e.Key)} = {Str(e.Value)}"))} }}");
            }

            if (load.EnvPassthrough is { Count: > 0 } passthrough)
            {
                sb.AppendLine("# Secrets: passed through from the environment the analyzer runs in, never stored here.")
                    .AppendLine($"env_passthrough = {InlineArray(passthrough)}");
            }
        }

        return sb.ToString();
    }

    public static string Key(string key) => BareKey.IsMatch(key) ? key : Str(key);

    /// <summary>The address as a container sees it on Docker Desktop: this machine is host.docker.internal.</summary>
    public static string FromAContainer(string url)
    {
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri)
            || uri.Host is not ("localhost" or "127.0.0.1" or "[::1]" or "::1"))
        {
            return url;
        }

        var outside = new UriBuilder(uri) { Host = "host.docker.internal" }.Uri.ToString();
        // UriBuilder adds a slash to a bare address; keep the user's form
        return url.EndsWith('/') ? outside : outside.TrimEnd('/');
    }

    /// <summary>A TOML basic string.</summary>
    public static string Str(string value)
    {
        var sb = new StringBuilder("\"");
        foreach (var ch in value)
        {
            sb.Append(ch switch
            {
                '"' => "\\\"",
                '\\' => "\\\\",
                '\n' => "\\n",
                '\r' => "\\r",
                '\t' => "\\t",
                '\b' => "\\b",
                '\f' => "\\f",
                _ when char.IsControl(ch) => $"\\u{(int)ch:X4}",
                _ => ch.ToString(),
            });
        }

        return sb.Append('"').ToString();
    }

    private static string Array(IEnumerable<string> items)
    {
        var list = items.ToList();
        return list.Count == 0
            ? "[]"
            : "[\n" + string.Concat(list.Select(i => $"    {Str(i)},\n")) + "]";
    }

    private static string InlineArray(IEnumerable<string> items) =>
        "[" + string.Join(", ", items.Select(Str)) + "]";

    private static string OneLine(string text) => text.Replace('\r', ' ').Replace('\n', ' ').Trim();
}
