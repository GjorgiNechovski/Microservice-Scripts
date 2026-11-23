using System.Text;
using System.Text.RegularExpressions;
using MicroserviceScripts.Ui.Compose;
using YamlDotNet.Serialization;

namespace MicroserviceScripts.Ui.Routes;

/// <summary>
/// One rule of a gateway or proxy: requests whose path starts with
/// <paramref name="Prefix"/> are sent to <paramref name="Host"/>.
/// </summary>
/// <param name="UpstreamPath">
/// What the prefix becomes on the way upstream ("/" when it is cut off);
/// null when the path is passed on unchanged.
/// </param>
/// <param name="Exact">Only the prefix itself matches (nginx <c>location =</c>).</param>
/// <param name="Source">Where the rule was read, for the user.</param>
public sealed record ProxyRule(string Prefix, string Host, string? UpstreamPath, bool Exact, string Source);

/// <summary>The rules read from one gateway's configuration, and what couldn't be read.</summary>
public sealed record ProxyRules(IReadOnlyList<ProxyRule> Rules, IReadOnlyList<string> Notes)
{
    public static readonly ProxyRules None = new([], []);
}

/// <summary>
/// Reads where gateways send requests: nginx (and openresty) configuration,
/// Kong declarative configuration, Traefik labels and YARP (the ASP.NET Core
/// reverse proxy) configuration. Only files the compose file mounts or builds
/// in are read, and nothing is contacted.
/// </summary>
public static class ProxyConfig
{
    /// <summary>The gateways whose configuration is read, for messages.</summary>
    public const string ReadableGateways = "nginx/openresty, Kong, Traefik labels and YARP";

    /// <summary>The rules of one compose service, if it is a gateway this can read.</summary>
    public static ProxyRules Read(ComposeProject project, ComposeService service, string? baseImage)
    {
        var image = $"{service.Image} {baseImage}".ToLowerInvariant();
        var rules = new List<ProxyRule>();
        var notes = new List<string>();

        if (service.BuiltFromSource || (service.Mounts ?? []).Any(m => IsAppSettings(m.Target)))
        {
            var (r, n) = YarpOf(project, service);
            rules.AddRange(r);
            notes.AddRange(n);
        }

        if (image.Contains("nginx") || image.Contains("openresty"))
        {
            foreach (var file in NginxFiles(service))
            {
                var (r, n) = Nginx(SafeRead(file), Relative(project, file));
                rules.AddRange(r);
                notes.AddRange(n);
            }
        }

        if (image.Contains("kong") || service.Environment.ContainsKey("KONG_DECLARATIVE_CONFIG"))
        {
            foreach (var file in KongFiles(service))
            {
                var (r, n) = Kong(SafeRead(file), Relative(project, file));
                rules.AddRange(r);
                notes.AddRange(n);
            }
        }

        if (image.Contains("traefik"))
        {
            rules.AddRange(Traefik(project));
        }

        return new ProxyRules(rules, notes);
    }

    private static string Relative(ComposeProject project, string file)
    {
        var relative = Path.GetRelativePath(project.Directory, file);
        return relative.StartsWith("..", StringComparison.Ordinal) ? file : relative.Replace('\\', '/');
    }

    private static string SafeRead(string file)
    {
        try
        {
            return new FileInfo(file).Length > 1_000_000 ? "" : File.ReadAllText(file);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            return "";
        }
    }

    private static IEnumerable<string> NginxFiles(ComposeService service)
    {
        var files = new List<string>();
        foreach (var mount in service.Mounts ?? [])
        {
            if (File.Exists(mount.Source)
                && (mount.Source.EndsWith(".conf", StringComparison.OrdinalIgnoreCase)
                    || mount.Target.StartsWith("/etc/nginx", StringComparison.Ordinal)
                    || mount.Target.Contains("openresty", StringComparison.Ordinal)))
            {
                files.Add(mount.Source);
            }
            else if (Directory.Exists(mount.Source))
            {
                files.AddRange(ConfFiles(mount.Source, depth: 2));
            }
        }

        if (service.BuildContext is { } context && Directory.Exists(context))
        {
            files.AddRange(ConfFiles(context, depth: 2));
        }

        return files.Distinct();
    }

    private static IEnumerable<string> ConfFiles(string dir, int depth)
    {
        IEnumerable<string> here;
        try
        {
            here = Directory.EnumerateFiles(dir, "*.conf").Order(StringComparer.Ordinal).ToList();
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            yield break;
        }

        foreach (var f in here)
        {
            yield return f;
        }

        if (depth > 1)
        {
            foreach (var sub in Directory.EnumerateDirectories(dir).Where(d => !Path.GetFileName(d).StartsWith('.')))
            {
                foreach (var f in ConfFiles(sub, depth - 1))
                {
                    yield return f;
                }
            }
        }
    }

    private static IEnumerable<string> KongFiles(ComposeService service)
    {
        var configured = service.Environment.GetValueOrDefault("KONG_DECLARATIVE_CONFIG");
        foreach (var mount in service.Mounts ?? [])
        {
            if (configured is not null && mount.Target == configured && File.Exists(mount.Source))
            {
                yield return mount.Source;
            }
            else if (configured is not null && configured.StartsWith(mount.Target.TrimEnd('/') + "/", StringComparison.Ordinal)
                     && Directory.Exists(mount.Source))
            {
                var inside = Path.Combine(mount.Source, configured[(mount.Target.TrimEnd('/').Length + 1)..]);
                if (File.Exists(inside))
                {
                    yield return inside;
                }
            }
            else if (configured is null && File.Exists(mount.Source)
                     && Regex.IsMatch(mount.Source, @"\.(ya?ml|json)$", RegexOptions.IgnoreCase))
            {
                yield return mount.Source;
            }
        }
    }

    // --- nginx ---------------------------------------------------------------

    private sealed record Directive(string Name, IReadOnlyList<string> Args, IReadOnlyList<Directive>? Block);

    public static (IReadOnlyList<ProxyRule> Rules, IReadOnlyList<string> Notes) Nginx(string text, string source)
    {
        var rules = new List<ProxyRule>();
        var notes = new List<string>();
        List<Directive> directives;
        try
        {
            var tokens = NginxTokens(text);
            var position = 0;
            directives = NginxBlock(tokens, ref position);
        }
        catch (FormatException)
        {
            notes.Add($"{source}: couldn't be read as nginx configuration.");
            return (rules, notes);
        }

        var upstreams = new Dictionary<string, List<string>>();
        void FindUpstreams(IEnumerable<Directive> block)
        {
            foreach (var d in block)
            {
                if (d.Name == "upstream" && d.Args.Count == 1 && d.Block is not null)
                {
                    upstreams[d.Args[0]] = d.Block.Where(s => s.Name == "server" && s.Args.Count > 0)
                        .Select(s => HostOf(s.Args[0])).ToList();
                }
                else if (d.Block is not null)
                {
                    FindUpstreams(d.Block);
                }
            }
        }

        FindUpstreams(directives);

        void Walk(IEnumerable<Directive> block, Dictionary<string, string> sets)
        {
            var local = new Dictionary<string, string>(sets);
            foreach (var d in block.Where(d => d.Name == "set" && d.Args.Count == 2))
            {
                local[d.Args[0].TrimStart('$')] = d.Args[1];
            }

            foreach (var d in block)
            {
                if (d.Block is null)
                {
                    continue;
                }

                if (d.Name == "location")
                {
                    Location(d, local);
                }
                else if (d.Name is "server" or "http")
                {
                    Walk(d.Block, local);
                }
            }
        }

        void Location(Directive location, Dictionary<string, string> sets)
        {
            var args = location.Args;
            var modifier = args.Count == 2 ? args[0] : null;
            var prefix = args.Count > 0 ? args[^1] : "/";
            if (modifier is "~" or "~*" || prefix.StartsWith('@'))
            {
                if (modifier is "~" or "~*")
                {
                    notes.Add($"{source}: the regular-expression location '{prefix}' was skipped.");
                }

                return;
            }

            var local = new Dictionary<string, string>(sets);
            foreach (var d in location.Block!.Where(d => d.Name == "set" && d.Args.Count == 2))
            {
                local[d.Args[0].TrimStart('$')] = d.Args[1];
            }

            var pass = location.Block!.FirstOrDefault(d => d.Name == "proxy_pass" && d.Args.Count == 1);
            if (pass is not null)
            {
                var target = ProxyTarget(pass.Args[0], local);
                if (target is null)
                {
                    notes.Add($"{source}: 'proxy_pass {pass.Args[0]}' uses a variable that couldn't be resolved.");
                }
                else
                {
                    var hosts = upstreams.TryGetValue(target.Value.Host, out var servers) ? servers : [target.Value.Host];
                    rules.AddRange(hosts.Select(h => new ProxyRule(prefix, h, target.Value.Path, modifier == "=", source)));
                }
            }

            foreach (var nested in location.Block!.Where(d => d.Name == "location" && d.Block is not null))
            {
                Location(nested, local);
            }
        }

        Walk(directives, new Dictionary<string, string>());
        return (rules, notes);
    }

    /// <summary>Host and path of a proxy_pass address; null when a variable in its host is unknown.</summary>
    private static (string Host, string? Path)? ProxyTarget(string address, Dictionary<string, string> sets)
    {
        // $request_uri / $uri: the request's own path goes on unchanged
        var unchanged = Regex.IsMatch(address, @"\$(request_uri|uri|document_uri)\b");
        var value = Regex.Replace(address, @"\$(request_uri|uri|document_uri|is_args|args|query_string)\b", "");
        for (var i = 0; i < 5 && value.Contains('$'); i++)
        {
            value = Regex.Replace(value, @"\$\{?(\w+)\}?", m => sets.TryGetValue(m.Groups[1].Value, out var v) ? v : m.Value);
        }

        var m = Regex.Match(value, @"^(?:[a-z]+://)?(?<host>[^/\s]+)(?<path>/\S*)?$", RegexOptions.IgnoreCase);
        if (!m.Success || m.Groups["host"].Value.Contains('$'))
        {
            return null;
        }

        var path = unchanged || !m.Groups["path"].Success ? null : m.Groups["path"].Value;
        return (HostOf(m.Groups["host"].Value), path);
    }

    private static string HostOf(string hostPort) =>
        Regex.Replace(hostPort.Trim(), @":\d+$", "").Trim('[', ']');

    private static List<string> NginxTokens(string text)
    {
        var tokens = new List<string>();
        var i = 0;
        while (i < text.Length)
        {
            var c = text[i];
            if (char.IsWhiteSpace(c))
            {
                i++;
            }
            else if (c == '#')
            {
                while (i < text.Length && text[i] != '\n')
                {
                    i++;
                }
            }
            else if (c is '{' or '}' or ';')
            {
                tokens.Add(c.ToString());
                i++;
            }
            else if (c is '"' or '\'')
            {
                var sb = new StringBuilder();
                i++;
                while (i < text.Length && text[i] != c)
                {
                    if (text[i] == '\\' && i + 1 < text.Length)
                    {
                        i++;
                    }

                    sb.Append(text[i++]);
                }

                i++;
                tokens.Add(sb.ToString());
            }
            else
            {
                var sb = new StringBuilder();
                while (i < text.Length && !char.IsWhiteSpace(text[i]) && text[i] is not (';' or '{' or '}'))
                {
                    if (text[i] == '$' && i + 1 < text.Length && text[i + 1] == '{')
                    {
                        // ${var} inside a word
                        while (i < text.Length && text[i] != '}')
                        {
                            sb.Append(text[i++]);
                        }
                    }

                    if (i < text.Length)
                    {
                        sb.Append(text[i++]);
                    }
                }

                tokens.Add(sb.ToString());
            }
        }

        return tokens;
    }

    private static List<Directive> NginxBlock(List<string> tokens, ref int position)
    {
        var block = new List<Directive>();
        var words = new List<string>();
        while (position < tokens.Count)
        {
            var t = tokens[position++];
            switch (t)
            {
                case ";":
                    if (words.Count > 0)
                    {
                        block.Add(new Directive(words[0], words.Skip(1).ToList(), null));
                    }

                    words = [];
                    break;
                case "{":
                    var inner = NginxBlock(tokens, ref position);
                    block.Add(new Directive(words.FirstOrDefault() ?? "", words.Skip(1).ToList(), inner));
                    words = [];
                    break;
                case "}":
                    return block;
                default:
                    words.Add(t);
                    break;
            }
        }

        return block;
    }

    // --- Kong (declarative configuration) -----------------------------------

    public static (IReadOnlyList<ProxyRule> Rules, IReadOnlyList<string> Notes) Kong(string text, string source)
    {
        var rules = new List<ProxyRule>();
        var notes = new List<string>();
        object? root;
        try
        {
            root = new DeserializerBuilder().Build().Deserialize<object>(text);
        }
        catch (YamlDotNet.Core.YamlException)
        {
            notes.Add($"{source}: couldn't be read as Kong configuration.");
            return (rules, notes);
        }

        if (root is not Dictionary<object, object> config || !config.ContainsKey("_format_version"))
        {
            return (rules, notes);
        }

        var upstreams = List(config, "upstreams").OfType<Dictionary<object, object>>()
            .Where(u => Str(u, "name") is not null)
            .ToDictionary(
                u => Str(u, "name")!,
                u => List(u, "targets").OfType<Dictionary<object, object>>()
                    .Select(t => Str(t, "target")).OfType<string>().Select(HostOf).ToList());

        var services = new Dictionary<string, (string Host, string Path)>();
        foreach (var s in List(config, "services").OfType<Dictionary<object, object>>())
        {
            string? host = null;
            var path = "/";
            if (Str(s, "url") is { } url
                && Regex.Match(url, @"^[a-z]+://(?<host>[^/:]+)(?::\d+)?(?<path>/.*)?$", RegexOptions.IgnoreCase) is { Success: true } m)
            {
                host = m.Groups["host"].Value;
                path = m.Groups["path"].Success ? m.Groups["path"].Value : "/";
            }
            else if (Str(s, "host") is { } h)
            {
                host = h;
                path = Str(s, "path") ?? "/";
            }

            if (host is null)
            {
                continue;
            }

            if (Str(s, "name") is { } name)
            {
                services[name] = (host, path);
            }

            foreach (var route in List(s, "routes").OfType<Dictionary<object, object>>())
            {
                AddRoute(route, host, path);
            }
        }

        foreach (var route in List(config, "routes").OfType<Dictionary<object, object>>())
        {
            var reference = route.TryGetValue("service", out var svc)
                ? svc as string ?? (svc as Dictionary<object, object>)?.GetValueOrDefault("name") as string
                : null;
            if (reference is not null && services.TryGetValue(reference, out var target))
            {
                AddRoute(route, target.Host, target.Path);
            }
        }

        void AddRoute(Dictionary<object, object> route, string host, string servicePath)
        {
            var strip = !(route.TryGetValue("strip_path", out var sp) && sp is string s
                          && s.Equals("false", StringComparison.OrdinalIgnoreCase));
            var paths = List(route, "paths").OfType<string>().ToList();
            if (paths.Count == 0)
            {
                paths = ["/"];
            }

            var hosts = upstreams.TryGetValue(host, out var targets) ? targets : [host];
            foreach (var p in paths)
            {
                if (p.StartsWith('~'))
                {
                    notes.Add($"{source}: the regular-expression route '{p}' was skipped.");
                    continue;
                }

                // strip_path cuts the matched prefix off, then Kong puts the service's path in front
                var upstreamPath = strip
                    ? servicePath
                    : servicePath is "/" or "" ? null : RouteCatalog.Join(servicePath, p);
                rules.AddRange(hosts.Select(h => new ProxyRule(p, h, upstreamPath, false, source)));
            }
        }

        return (rules, notes);
    }

    private static string? Str(Dictionary<object, object> map, string key) =>
        map.TryGetValue(key, out var v) ? v as string : null;

    private static IEnumerable<object> List(Dictionary<object, object> map, string key) =>
        map.TryGetValue(key, out var v) && v is List<object> list ? list : [];

    // --- YARP (appsettings "ReverseProxy") -------------------------------------

    private static readonly HashSet<string> SkippedFolders = new(StringComparer.OrdinalIgnoreCase)
    {
        "bin", "obj", "node_modules", "TestResults", "test", "tests",
    };

    private static bool IsAppSettings(string path) =>
        Regex.IsMatch(Path.GetFileName(path), @"^appsettings(\.[\w-]+)?\.json$", RegexOptions.IgnoreCase);

    /// <summary>
    /// The YARP rules of a service: the ReverseProxy section of the appsettings
    /// files in its build folder (or mounted into it), layered as ASP.NET Core
    /// layers them: appsettings.json, then appsettings.{environment}.json (the
    /// service's ASPNETCORE_ENVIRONMENT, Production when unset), then
    /// ReverseProxy__* environment variables.
    /// </summary>
    private static (List<ProxyRule> Rules, List<string> Notes) YarpOf(ComposeProject project, ComposeService service)
    {
        var rules = new List<ProxyRule>();
        var notes = new List<string>();
        var environment = service.Environment.GetValueOrDefault("ASPNETCORE_ENVIRONMENT")
                          ?? service.Environment.GetValueOrDefault("DOTNET_ENVIRONMENT")
                          ?? "Production";
        var overrides = service.Environment
            .Where(e => e.Key.StartsWith("ReverseProxy__", StringComparison.OrdinalIgnoreCase))
            .ToDictionary(e => e.Key.Replace("__", ":"), e => (string?)e.Value);

        var layers = new List<List<string>>();
        var roots = new[] { service.BuildContext, Path.GetDirectoryName(service.DockerfilePath ?? "") }
            .Where(d => !string.IsNullOrEmpty(d) && Directory.Exists(d))
            .Select(d => Path.GetFullPath(d!))
            .Distinct(StringComparer.OrdinalIgnoreCase);
        foreach (var folder in roots.SelectMany(r => AppSettingsFolders(r, depth: 3)).Distinct(StringComparer.OrdinalIgnoreCase))
        {
            layers.Add(new[] { "appsettings.json", $"appsettings.{environment}.json" }
                .Select(name => FileIn(folder, name)).OfType<string>().ToList());
        }

        // a file mounted over the image's appsettings: its own layer
        foreach (var mount in service.Mounts ?? [])
        {
            if (IsAppSettings(mount.Target) && File.Exists(mount.Source))
            {
                layers.Add([mount.Source]);
            }
        }

        foreach (var files in layers.Where(l => l.Count > 0))
        {
            var texts = files.Select(f => (Text: SafeRead(f), Source: Relative(project, f))).ToList();
            if (!texts.Any(t => t.Text.Contains("ReverseProxy", StringComparison.OrdinalIgnoreCase)))
            {
                continue;
            }

            var (r, n) = Yarp(texts, overrides);
            rules.AddRange(r);
            notes.AddRange(n);
        }

        return (rules, notes);
    }

    /// <summary>Folders under <paramref name="root"/> holding an appsettings.json, skipping build output and tests.</summary>
    private static IEnumerable<string> AppSettingsFolders(string root, int depth)
    {
        if (FileIn(root, "appsettings.json") is not null)
        {
            yield return root;
        }

        if (depth <= 1)
        {
            yield break;
        }

        IEnumerable<string> subs;
        try
        {
            subs = Directory.EnumerateDirectories(root).Order(StringComparer.Ordinal).ToList();
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            yield break;
        }

        foreach (var sub in subs)
        {
            var name = Path.GetFileName(sub);
            if (name.StartsWith('.') || SkippedFolders.Contains(name) || name.EndsWith(".Tests", StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            foreach (var f in AppSettingsFolders(sub, depth - 1))
            {
                yield return f;
            }
        }
    }

    /// <summary>A file in a folder by name, whatever its case (appsettings.Production.json); null when absent.</summary>
    private static string? FileIn(string folder, string name)
    {
        try
        {
            return Directory.EnumerateFiles(folder)
                .FirstOrDefault(f => Path.GetFileName(f).Equals(name, StringComparison.OrdinalIgnoreCase));
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            return null;
        }
    }

    /// <summary>
    /// YARP rules from appsettings files, given in the order ASP.NET Core layers
    /// them, plus configuration overrides (ReverseProxy:Routes:x:..., as from
    /// environment variables). Read with ASP.NET Core's own configuration loader,
    /// so comments, trailing commas and later files overriding earlier keys work
    /// as in the gateway itself.
    /// </summary>
    public static (IReadOnlyList<ProxyRule> Rules, IReadOnlyList<string> Notes) Yarp(
        IReadOnlyList<(string Text, string Source)> files, IReadOnlyDictionary<string, string?>? overrides = null)
    {
        var rules = new List<ProxyRule>();
        var notes = new List<string>();
        var builder = new ConfigurationBuilder();
        var read = new List<string>();
        foreach (var (text, source) in files)
        {
            if (string.IsNullOrWhiteSpace(text))
            {
                continue;
            }

            try
            {
                // each file on its own first, so a broken one is named and left out
                new ConfigurationBuilder().AddJsonStream(Stream(text)).Build();
            }
            catch (Exception ex) when (ex is FormatException or System.Text.Json.JsonException or InvalidDataException)
            {
                notes.Add($"{source}: couldn't be read as JSON configuration.");
                continue;
            }

            builder.AddJsonStream(Stream(text));
            read.Add(source);
        }

        if (overrides is { Count: > 0 })
        {
            builder.AddInMemoryCollection(overrides);
        }

        var proxy = builder.Build().GetSection("ReverseProxy");
        if (!proxy.Exists())
        {
            return (rules, notes);
        }

        var from = read.Count > 0 ? string.Join(" + ", read) : "environment variables";
        var clusters = proxy.GetSection("Clusters").GetChildren().ToDictionary(
            c => c.Key,
            c => c.GetSection("Destinations").GetChildren().Select(d => d["Address"])
                .Where(a => !string.IsNullOrWhiteSpace(a)).Select(a => a!).ToList(),
            StringComparer.OrdinalIgnoreCase);

        foreach (var route in proxy.GetSection("Routes").GetChildren())
        {
            var match = route["Match:Path"];
            if (string.IsNullOrWhiteSpace(match))
            {
                notes.Add($"{from}: the route '{route.Key}' matches by host name only, which requests to this machine don't carry; skipped.");
                continue;
            }

            if (route["ClusterId"] is not { } clusterId || !clusters.TryGetValue(clusterId, out var destinations)
                || destinations.Count == 0)
            {
                notes.Add($"{from}: the route '{route.Key}' names no cluster with a destination address; skipped.");
                continue;
            }

            var (prefix, exact) = TemplatePrefix(match);

            // what the path becomes on the way upstream; null while it is passed on unchanged
            string? upstream = null;
            var skipped = false;
            foreach (var transform in route.GetSection("Transforms").GetChildren())
            {
                if (transform["PathPattern"] is { } pattern)
                {
                    upstream = TemplatePrefix(pattern).Prefix;
                }
                else if (transform["PathRemovePrefix"] is { } remove)
                {
                    var current = upstream ?? prefix;
                    var cut = remove.TrimEnd('/');
                    upstream = current.StartsWith(cut, StringComparison.OrdinalIgnoreCase)
                        ? "/" + current[cut.Length..].TrimStart('/')
                        : current;
                }
                else if (transform["PathPrefix"] is { } add)
                {
                    upstream = RouteCatalog.Join(add, upstream ?? prefix);
                }
                else if (transform["PathSet"] is not null)
                {
                    notes.Add($"{from}: the route '{route.Key}' sends every request to one fixed path (PathSet); skipped.");
                    skipped = true;
                }
            }

            if (skipped)
            {
                continue;
            }

            if (upstream is not null && upstream.TrimEnd('/') == prefix.TrimEnd('/'))
            {
                upstream = null;
            }

            foreach (var address in destinations)
            {
                var m = Regex.Match(address.Trim(), @"^(?:[a-z][a-z0-9+.-]*://)?(?<host>[^/\s]+)(?<path>/\S*)?$", RegexOptions.IgnoreCase);
                if (!m.Success)
                {
                    notes.Add($"{from}: the destination '{address}' of the route '{route.Key}' couldn't be read.");
                    continue;
                }

                // a destination with a path puts it in front of what the route sends on
                var basePath = m.Groups["path"].Success ? m.Groups["path"].Value.TrimEnd('/') : "";
                var up = basePath.Length == 0
                    ? upstream
                    : RouteCatalog.Join(basePath, upstream ?? prefix);
                rules.Add(new ProxyRule(prefix, HostOf(m.Groups["host"].Value), up, exact, from));
            }
        }

        return (rules, notes);
    }

    /// <summary>
    /// The literal start of an ASP.NET route template ("/api/{**catch-all}" is
    /// "/api/"), and whether the template is all literal, so only that path matches.
    /// </summary>
    private static (string Prefix, bool Exact) TemplatePrefix(string template)
    {
        var t = "/" + template.Trim().TrimStart('/');
        var brace = t.IndexOf('{');
        return brace < 0 ? (t, true) : (t[..brace], false);
    }

    private static MemoryStream Stream(string text) => new(Encoding.UTF8.GetBytes(text));

    // --- Traefik (docker labels) -----------------------------------------------

    /// <summary>Rules from the traefik.* labels of every service in the gateway's project.</summary>
    public static IReadOnlyList<ProxyRule> Traefik(ComposeProject project)
    {
        var rules = new List<ProxyRule>();
        foreach (var service in project.Services)
        {
            var labels = service.Labels ?? new Dictionary<string, string>();
            if (labels.TryGetValue("traefik.enable", out var enabled) && enabled.Equals("false", StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            var strips = labels
                .Select(l => Regex.Match(l.Key, @"^traefik\.http\.middlewares\.(?<m>[^.]+)\.stripprefix\.prefixes$"))
                .Where(m => m.Success)
                .ToDictionary(m => m.Groups["m"].Value, m => labels[m.Value].Split(',', StringSplitOptions.TrimEntries));
            foreach (var router in labels.Select(l => Regex.Match(l.Key, @"^traefik\.http\.routers\.(?<r>[^.]+)\.rule$")).Where(m => m.Success))
            {
                var name = router.Groups["r"].Value;
                var prefixes = Regex.Matches(labels[router.Value], @"Path(?:Prefix)?\(\s*[`""'](?<p>[^`""']+)[`""']")
                    .Select(m => m.Groups["p"].Value).ToList();
                if (prefixes.Count == 0)
                {
                    prefixes = ["/"];
                }

                var middlewares = labels.GetValueOrDefault($"traefik.http.routers.{name}.middlewares", "")
                    .Split(',', StringSplitOptions.TrimEntries | StringSplitOptions.RemoveEmptyEntries)
                    .Select(m => m.Split('@')[0]);
                var stripped = middlewares.SelectMany(m => strips.GetValueOrDefault(m, [])).ToHashSet();
                rules.AddRange(prefixes.Select(p => new ProxyRule(
                    p, service.Name, stripped.Contains(p) ? "/" : null, false, $"labels of {service.Name}")));
            }
        }

        return rules;
    }
}
