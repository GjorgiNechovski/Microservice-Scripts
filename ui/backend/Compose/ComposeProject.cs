using System.Text.Json;
using System.Text.RegularExpressions;

namespace MicroserviceScripts.Ui.Compose;

public sealed record ComposeService(
    string Name,
    string? Image,
    string? BuildContext,
    string? Dockerfile,
    IReadOnlyList<string> DependsOn,
    bool HasHealthcheck,
    IReadOnlyList<int> PortTargets,
    IReadOnlyList<int> Expose,
    int? Replicas,
    string? Restart,
    IReadOnlyDictionary<string, string> Environment,
    IReadOnlyList<string> Command,
    IReadOnlyList<string> Entrypoint,
    IReadOnlyList<string> Networks,
    IReadOnlyList<PublishedPort>? Published = null,
    IReadOnlyList<BindMount>? Mounts = null,
    IReadOnlyDictionary<string, string>? Labels = null)
{
    public bool BuiltFromSource => BuildContext is not null;

    /// <summary>The Dockerfile Compose builds this service from, when it is built.</summary>
    public string? DockerfilePath => BuildContext is null
        ? null
        : Path.IsPathRooted(Dockerfile ?? "") ? Dockerfile : Path.Combine(BuildContext, Dockerfile ?? "Dockerfile");
}

public sealed record ComposeNetwork(string Key, string Name, bool External);

/// <summary>A container port reachable from the host, at <paramref name="HostPort"/>.</summary>
public sealed record PublishedPort(int HostPort, int Target);

/// <summary>A host file or folder mounted into the container.</summary>
public sealed record BindMount(string Source, string Target);

/// <summary>
/// One compose project as Compose itself resolved it
/// (<c>docker compose config --format json</c>): overlays merged, variables
/// substituted, project name decided. Nothing here re-implements Compose's rules.
/// </summary>
public sealed record ComposeProject(
    string Name,
    string Directory,
    IReadOnlyList<string> Files,
    IReadOnlyList<ComposeService> Services,
    IReadOnlyList<ComposeNetwork> Networks)
{
    /// <summary>The real network name behind a service's network key.</summary>
    public string NetworkName(string key) =>
        Networks.FirstOrDefault(n => n.Key == key)?.Name ?? $"{Name}_{key}";

    public static ComposeProject Parse(string json, string directory, IReadOnlyList<string> files)
    {
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        var name = Str(root, "name") ?? Path.GetFileName(directory).ToLowerInvariant();

        var networks = new List<ComposeNetwork>();
        if (root.TryGetProperty("networks", out var nets) && nets.ValueKind == JsonValueKind.Object)
        {
            foreach (var net in nets.EnumerateObject())
            {
                var external = net.Value.ValueKind == JsonValueKind.Object
                    && net.Value.TryGetProperty("external", out var ext)
                    && (ext.ValueKind == JsonValueKind.True || ext.ValueKind == JsonValueKind.Object);
                networks.Add(new ComposeNetwork(
                    net.Name, Str(net.Value, "name") ?? $"{name}_{net.Name}", external));
            }
        }

        var services = new List<ComposeService>();
        if (root.TryGetProperty("services", out var svcs) && svcs.ValueKind == JsonValueKind.Object)
        {
            foreach (var svc in svcs.EnumerateObject())
            {
                services.Add(ParseService(svc.Name, svc.Value));
            }
        }

        return new ComposeProject(name, directory, files, services, networks);
    }

    private static ComposeService ParseService(string name, JsonElement s)
    {
        string? buildContext = null, dockerfile = null;
        if (s.TryGetProperty("build", out var build))
        {
            if (build.ValueKind == JsonValueKind.String)
            {
                buildContext = build.GetString();
            }
            else if (build.ValueKind == JsonValueKind.Object)
            {
                buildContext = Str(build, "context");
                dockerfile = Str(build, "dockerfile");
            }
        }

        var healthcheck = s.TryGetProperty("healthcheck", out var hc) && hc.ValueKind == JsonValueKind.Object
            && !(hc.TryGetProperty("disable", out var off) && off.ValueKind == JsonValueKind.True)
            && !(hc.TryGetProperty("test", out var test) && test.ValueKind == JsonValueKind.Array
                 && test.GetArrayLength() > 0 && test[0].GetString() == "NONE");

        var ports = new List<int>();
        var published = new List<PublishedPort>();
        if (s.TryGetProperty("ports", out var portList) && portList.ValueKind == JsonValueKind.Array)
        {
            foreach (var p in portList.EnumerateArray())
            {
                if (p.ValueKind == JsonValueKind.Object && p.TryGetProperty("target", out var t)
                    && t.TryGetInt32(out var target))
                {
                    ports.Add(target);
                    // Compose writes the host port as a string ("8000"), or a range ("8000-8001")
                    var host = Str(p, "published");
                    if (host is not null && int.TryParse(host.Split('-')[0], out var hostPort))
                    {
                        published.Add(new PublishedPort(hostPort, target));
                    }
                }
            }
        }

        var mounts = new List<BindMount>();
        if (s.TryGetProperty("volumes", out var volumes) && volumes.ValueKind == JsonValueKind.Array)
        {
            foreach (var v in volumes.EnumerateArray())
            {
                if (Str(v, "type") == "bind" && Str(v, "source") is { } source && Str(v, "target") is { } target)
                {
                    mounts.Add(new BindMount(source, target));
                }
            }
        }

        var labels = new Dictionary<string, string>();
        if (s.TryGetProperty("labels", out var labelSet) && labelSet.ValueKind == JsonValueKind.Object)
        {
            foreach (var l in labelSet.EnumerateObject())
            {
                labels[l.Name] = l.Value.ValueKind == JsonValueKind.String ? l.Value.GetString()! : l.Value.GetRawText();
            }
        }

        var expose = Strings(s, "expose")
            .Select(e => Regex.Match(e, @"^\d+"))
            .Where(m => m.Success)
            .Select(m => int.Parse(m.Value))
            .ToList();

        int? replicas = null;
        if (s.TryGetProperty("deploy", out var deploy) && deploy.ValueKind == JsonValueKind.Object
            && deploy.TryGetProperty("replicas", out var r) && r.TryGetInt32(out var n))
        {
            replicas = n;
        }

        var environment = new Dictionary<string, string>();
        if (s.TryGetProperty("environment", out var env) && env.ValueKind == JsonValueKind.Object)
        {
            foreach (var e in env.EnumerateObject())
            {
                environment[e.Name] = e.Value.ValueKind == JsonValueKind.String ? e.Value.GetString()! : "";
            }
        }

        var networks = new List<string>();
        if (s.TryGetProperty("networks", out var nets))
        {
            if (nets.ValueKind == JsonValueKind.Object)
            {
                networks.AddRange(nets.EnumerateObject().Select(x => x.Name));
            }
            else if (nets.ValueKind == JsonValueKind.Array)
            {
                networks.AddRange(nets.EnumerateArray().Select(x => x.GetString()!).Where(x => x is not null));
            }
        }

        var dependsOn = new List<string>();
        if (s.TryGetProperty("depends_on", out var deps))
        {
            if (deps.ValueKind == JsonValueKind.Object)
            {
                dependsOn.AddRange(deps.EnumerateObject().Select(x => x.Name));
            }
            else if (deps.ValueKind == JsonValueKind.Array)
            {
                dependsOn.AddRange(deps.EnumerateArray().Select(x => x.GetString()!).Where(x => x is not null));
            }
        }

        return new ComposeService(
            name, Str(s, "image"), buildContext, dockerfile, dependsOn, healthcheck, ports, expose,
            replicas, Str(s, "restart"), environment, Strings(s, "command"), Strings(s, "entrypoint"),
            networks, published, mounts, labels);
    }

    private static string? Str(JsonElement e, string property) =>
        e.ValueKind == JsonValueKind.Object && e.TryGetProperty(property, out var v)
        && v.ValueKind == JsonValueKind.String
            ? v.GetString()
            : null;

    /// <summary>A list property, or a plain string as a one-item list.</summary>
    private static IReadOnlyList<string> Strings(JsonElement e, string property)
    {
        if (e.ValueKind != JsonValueKind.Object || !e.TryGetProperty(property, out var v))
        {
            return [];
        }

        return v.ValueKind switch
        {
            JsonValueKind.Array => v.EnumerateArray()
                .Select(x => x.ValueKind == JsonValueKind.String ? x.GetString()! : x.GetRawText())
                .ToList(),
            JsonValueKind.String => [v.GetString()!],
            JsonValueKind.Number => [v.GetRawText()],
            _ => [],
        };
    }
}
