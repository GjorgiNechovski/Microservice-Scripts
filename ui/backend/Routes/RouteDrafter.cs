using MicroserviceScripts.Ui.Compose;
using MicroserviceScripts.Ui.Drafting;

namespace MicroserviceScripts.Ui.Routes;

/// <summary>A route the load test could call, as found in the code and the gateways.</summary>
/// <param name="Id">Unique: the service and the route as it declares it.</param>
/// <param name="Path">As called from outside: relative to <paramref name="EntryPoint"/>.</param>
/// <param name="EntryPoint">The address on this machine it is reached through; null when none leads to it.</param>
/// <param name="Service">The compose service (project/service) that handles it.</param>
/// <param name="Through">The gateways it passes on the way, outermost first (project/service ids).</param>
/// <param name="ServicePath">The path as the service declares it.</param>
/// <param name="Source">Where it is declared, relative to the project folder, with its line.</param>
public sealed record DraftRoute(
    string Id,
    string Method,
    string Path,
    string? EntryPoint,
    string Service,
    IReadOnlyList<string> Through,
    string ServicePath,
    string? Handler,
    string Source,
    string Framework,
    RequestShape? Request = null);

/// <summary>How much of a service's code the route finder could read.</summary>
/// <param name="Service">The compose service (project/service).</param>
/// <param name="Language">The language most of its code is in; null when none was recognised.</param>
/// <param name="Supported">Routes can be read for that language.</param>
/// <param name="Routes">How many routes were found.</param>
/// <param name="GatewayRules">How many rules were read from its gateway configuration (YARP, say): a
/// gateway serves its upstreams' routes, not its own.</param>
/// <param name="Gateway">It looks like a gateway (its image, or a reverse-proxy library it builds with).</param>
public sealed record ServiceCode(string Service, string? Language, bool Supported, int Routes,
    int GatewayRules = 0, bool Gateway = false);

/// <param name="Notes">What couldn't be read, for the user.</param>
/// <param name="Frameworks">The frameworks whose routes can be found.</param>
/// <param name="Code">Per service built from source: its language and how many routes were found in it.</param>
/// <param name="Languages">The languages routes can be read for.</param>
public sealed record RouteDraft(
    IReadOnlyList<DraftRoute> Routes,
    IReadOnlyList<string> Notes,
    IReadOnlyList<string> Frameworks,
    IReadOnlyList<ServiceCode>? Code = null,
    IReadOnlyList<string>? Languages = null);

/// <summary>
/// Finds the routes of every service built from source, and how each is
/// reached from this machine: through which gateways, at which address. It
/// reads the code and the gateways' configuration; nothing is contacted.
/// </summary>
public sealed class RouteDrafter(
    RouteCatalog catalog,
    Func<string?, DockerfileFacts?> readDockerfile,
    RequestShapes? shapes = null,
    ImageCatalog? images = null)
{
    private const int MaxHops = 6;

    public RouteDraft Draft(IReadOnlyList<ComposeProject> projects, DraftResult? components)
    {
        var notes = new List<string>();
        var all = projects.SelectMany(p => p.Services.Select(s => (Project: p, Service: s))).ToList();
        string IdOf(ComposeProject p, ComposeService s) => $"{p.Name}/{s.Name}";

        bool Modelled(string id) =>
            components?.Services.GetValueOrDefault(id)?.Kind is not ComponentKind.NotModelled;

        // where each gateway sends requests
        var rules = new Dictionary<string, IReadOnlyList<ProxyRule>>();
        var gateways = new HashSet<string>();
        foreach (var (project, service) in all)
        {
            var id = IdOf(project, service);
            var baseImage = service.BuiltFromSource ? readDockerfile(service.DockerfilePath)?.BaseImage : null;
            var read = ProxyConfig.Read(project, service, baseImage);
            if (read.Rules.Count > 0)
            {
                rules[id] = read.Rules;
                gateways.Add(id);
            }
            else if (Modelled(id) && LooksLikeGateway(service, baseImage))
            {
                gateways.Add(id);
                notes.Add($"{id}: looks like a gateway, but where it sends requests couldn't be read from its "
                          + $"configuration (read: {ProxyConfig.ReadableGateways}). Routes through it aren't known, so "
                          + "the load test would go around it: add its routes with its address, and add it to the "
                          + "features it carries.");
            }

            // infrastructure has no routes of its own to miss
            if (!service.BuiltFromSource && images is not null && images.Match(service.Image) is null
                && components?.Services.GetValueOrDefault(id)?.Kind is null or ComponentKind.Service or ComponentKind.Unsure)
            {
                notes.Add($"{id}: runs the image {service.Image}, which these compose files don't build, so its code "
                          + "isn't read and none of its routes are listed. Add the compose file or overlay that builds "
                          + "it, add its routes by hand, or use your own k6 script.");
            }

            notes.AddRange(read.Notes.Select(n => $"{id}: {n}"));
        }

        string? Resolve(string host, ComposeProject from) =>
            (from.Services.FirstOrDefault(s => s.Name == host) is { } same ? IdOf(from, same) : null)
            ?? all.Where(x => x.Service.Name == host).Select(x => IdOf(x.Project, x.Service)).FirstOrDefault();

        var gatewaysOf = new Dictionary<string, List<(string Gateway, ProxyRule Rule)>>();
        foreach (var (gateway, list) in rules)
        {
            var project = all.First(x => IdOf(x.Project, x.Service) == gateway).Project;
            foreach (var rule in list)
            {
                if (Resolve(rule.Host, project) is { } upstream && upstream != gateway)
                {
                    if (!gatewaysOf.TryGetValue(upstream, out var into))
                    {
                        gatewaysOf[upstream] = into = [];
                    }

                    into.Add((gateway, rule));
                }
            }
        }

        string? EntryOf(string id)
        {
            var (_, service) = all.First(x => IdOf(x.Project, x.Service) == id);
            var published = service.Published ?? [];
            if (published.Count == 0)
            {
                return null;
            }

            // the port it serves on, when the draft knows it (Kong publishes its admin port too)
            var listens = components?.Services.GetValueOrDefault(id)?.ReadinessPort;
            var port = published.FirstOrDefault(p => p.Target == listens) ?? published[0];
            return $"http://localhost:{port.HostPort}";
        }

        IEnumerable<(string Path, List<string> Through, string Entry)> Reach(string id, string path, HashSet<string> seen)
        {
            if (EntryOf(id) is { } entry)
            {
                yield return (path, [], entry);
            }

            if (seen.Count > MaxHops)
            {
                yield break;
            }

            foreach (var (gateway, rule) in gatewaysOf.GetValueOrDefault(id) ?? [])
            {
                if (seen.Contains(gateway) || External(rule, path) is not { } outside)
                {
                    continue;
                }

                foreach (var (p, through, e) in Reach(gateway, outside, [.. seen, gateway]))
                {
                    yield return (p, [.. through, gateway], e);
                }
            }
        }

        // the code: each build folder once, without the folders of services nested inside it
        var contexts = all.Where(x => x.Service.BuildContext is not null)
            .Select(x => (x.Project, x.Service, Context: Path.GetFullPath(x.Service.BuildContext!)))
            .ToList();
        var routes = new List<DraftRoute>();
        var scanned = new Dictionary<string, IReadOnlyList<SourceRoute>>();
        var typesOf = new Dictionary<string, TypeIndex>();
        var languageOf = new Dictionary<string, CodeLanguage>();
        foreach (var group in contexts.GroupBy(c => c.Context))
        {
            if (group.Count() > 1)
            {
                notes.Add($"{string.Join(", ", group.Select(g => g.Service.Name))} are built from the same folder, "
                          + "so each is shown with all of its routes: untick the ones a service doesn't serve.");
            }

            var nested = contexts.Select(c => c.Context)
                .Where(c => c != group.Key && c.StartsWith(group.Key + Path.DirectorySeparatorChar, StringComparison.Ordinal))
                .ToList();
            var files = catalog.SourceFiles(group.Key, nested, out var cut);
            if (cut)
            {
                notes.Add($"{string.Join(", ", group.Select(g => g.Service.Name))}: only the first "
                          + $"{catalog.MaxSourceFiles} source files under {group.Key} were searched, so routes in the "
                          + "others aren't listed. Add them by hand, or build each service from its own folder.");
            }

            scanned[group.Key] = catalog.Find(files);
            typesOf[group.Key] = TypeIndex.Build(files);
            languageOf[group.Key] = catalog.LanguageOf(group.Key, nested);
        }

        foreach (var (project, service, context) in contexts)
        {
            var id = IdOf(project, service);
            string Relative(string file) => Path.GetRelativePath(project.Directory, file).Replace('\\', '/');
            foreach (var route in scanned[context])
            {
                var best = Reach(id, route.Path, [id])
                    .OrderByDescending(r => r.Through.Count)
                    .ThenBy(r => r.Path.Length)
                    .Select(r => ((string Path, List<string> Through, string Entry)?)r)
                    .FirstOrDefault();
                var file = Path.GetRelativePath(project.Directory, route.File).Replace('\\', '/');
                routes.Add(new DraftRoute(
                    $"{id} {route.Method} {route.Path}",
                    route.Method,
                    best?.Path ?? route.Path,
                    best?.Entry,
                    id,
                    best?.Through ?? [],
                    route.Path,
                    route.Handler,
                    $"{file}:{route.Line}",
                    route.Framework,
                    shapes?.Build(route, typesOf[context], Relative)));
            }
        }

        var code = contexts.Select(c => (Id: IdOf(c.Project, c.Service), c.Context))
            .Select(c => new ServiceCode(c.Id, languageOf[c.Context].Language, languageOf[c.Context].Supported,
                routes.Count(r => r.Service == c.Id), rules.GetValueOrDefault(c.Id)?.Count ?? 0,
                gateways.Contains(c.Id)))
            .ToList();
        return new RouteDraft(
            routes.OrderBy(r => r.Service, StringComparer.Ordinal).ThenBy(r => r.Path, StringComparer.Ordinal)
                .ThenBy(r => r.Method, StringComparer.Ordinal).ToList(),
            notes.Distinct().ToList(),
            catalog.FrameworkNames.ToList(),
            code,
            catalog.SupportedLanguages.Order(StringComparer.Ordinal).ToList());
    }

    /// <summary>
    /// Whether a service is a gateway: the image catalogue says so (a gateway or
    /// proxy image, or the base image it is built on), or it is built with a
    /// reverse-proxy library its manifest names (YARP, Ocelot, Spring Cloud Gateway...:
    /// the "gateways" of Data/route-patterns.json).
    /// </summary>
    private bool LooksLikeGateway(ComposeService service, string? baseImage)
    {
        if (images is not null
            && (images.Match(service.Image) ?? images.Match(baseImage)) is { Category: "gateway" or "proxy" })
        {
            return true;
        }

        return service.BuildContext is { } context && Directory.Exists(context)
                                                   && Manifests(context, depth: 3).Any(ManifestNamesGatewayLibrary);
    }

    private bool ManifestNamesGatewayLibrary(string file)
    {
        try
        {
            if (new FileInfo(file).Length > 1_000_000)
            {
                return false;
            }

            var text = File.ReadAllText(file);
            return catalog.GatewayLibraries.Any(l => text.Contains(l, StringComparison.OrdinalIgnoreCase));
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            return false;
        }
    }

    /// <summary>Project manifests (.csproj, package.json, pom.xml, build.gradle...) near the top of a build folder.</summary>
    private IEnumerable<string> Manifests(string dir, int depth)
    {
        List<string> files, subs;
        try
        {
            files = Directory.EnumerateFiles(dir).ToList();
            subs = depth > 1 ? Directory.EnumerateDirectories(dir).ToList() : [];
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            yield break;
        }

        foreach (var f in files)
        {
            if (catalog.GatewayManifests?.IsMatch(Path.GetFileName(f)) == true)
            {
                yield return f;
            }
        }

        foreach (var sub in subs)
        {
            var name = Path.GetFileName(sub);
            if (name.StartsWith('.') || name is "node_modules" or "bin" or "obj" or "build" or "target" or "dist")
            {
                continue;
            }

            foreach (var f in Manifests(sub, depth - 1))
            {
                yield return f;
            }
        }
    }

    /// <summary>The path a request needs outside a gateway to arrive at <paramref name="path"/> inside it.</summary>
    public static string? External(ProxyRule rule, string path)
    {
        if (rule.UpstreamPath is null)
        {
            var passes = rule.Exact ? path == rule.Prefix : path.StartsWith(rule.Prefix, StringComparison.Ordinal)
                                                             || path == rule.Prefix.TrimEnd('/');
            return passes ? path : null;
        }

        var upstream = rule.UpstreamPath;
        string rest;
        if (upstream is "/" or "")
        {
            rest = path;
        }
        else if (path == upstream.TrimEnd('/') || path.StartsWith(upstream.TrimEnd('/') + "/", StringComparison.Ordinal))
        {
            rest = path[upstream.TrimEnd('/').Length..];
        }
        else
        {
            return null;
        }

        var outside = RouteCatalog.Join(rule.Prefix, rest);
        return rule.Exact && outside != RouteCatalog.NormalizePath(rule.Prefix) ? null : outside;
    }
}
