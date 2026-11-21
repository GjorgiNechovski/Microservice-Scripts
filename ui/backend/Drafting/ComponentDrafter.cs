using System.Text.RegularExpressions;
using MicroserviceScripts.Ui.Compose;

namespace MicroserviceScripts.Ui.Drafting;

public enum ComponentKind
{
    /// <summary>An application tier: gets queueing analysis and needs request metrics.</summary>
    Service,

    /// <summary>Shared infrastructure: a gateway, database, cache, broker.</summary>
    Infrastructure,

    /// <summary>Not part of the system under test: a frontend, an admin UI, a one-off job.</summary>
    NotModelled,

    /// <summary>Couldn't tell: the user decides.</summary>
    Unsure,
}

/// <param name="Via">What says so: "depends_on", or the environment variable naming it.</param>
public sealed record Dependency(string Id, string Via);

/// <param name="Role">"replica" (another instance of it) or "in front" (a pooler or proxy for it alone).</param>
/// <param name="Confirmed">
/// False for a copy told apart only by a number (db-1, db-2): that can as well be a second, separate
/// database, so the user says which before its instance is counted.
/// </param>
public sealed record FoldedMember(string Id, string Role, string Reason, bool Confirmed = true);

/// <summary>One component of the draft: a compose service, plus the ones folded into it.</summary>
public sealed record DraftComponent(
    string Id,
    string Name,
    string Project,
    string ComposeService,
    ComponentKind Kind,
    IReadOnlyList<string> Reasons,
    string? Image,
    bool BuiltFromSource,
    string? Language,
    bool HasHealthcheck,
    string? HealthcheckSource,
    string? Restart,
    int? Replicas,
    int? ReadinessPort,
    string? ReadinessSource,
    int Instances,
    string? InstancesReason,
    IReadOnlyList<Dependency> DependsOn,
    IReadOnlyList<FoldedMember> Folded,
    IReadOnlyList<string> Networks);

/// <param name="Components">How many of the draft's components are on it.</param>
public sealed record NetworkSuggestion(string Name, IReadOnlyList<string> Projects, int Components);

/// <param name="Components">The draft, folded, with unique names: what goes into the spec.</param>
/// <param name="Services">Every compose service on its own, by id, for unfolding or adding by hand.</param>
/// <param name="Networks">Candidates for the network the monitoring stack joins, best first.</param>
/// <param name="ProjectServices">Every service name per project: decides when a component needs pinning.</param>
public sealed record DraftResult(
    IReadOnlyList<DraftComponent> Components,
    IReadOnlyDictionary<string, DraftComponent> Services,
    IReadOnlyList<NetworkSuggestion> Networks,
    IReadOnlyDictionary<string, IReadOnlyList<string>> ProjectServices,
    IReadOnlyList<string> Notes);

/// <summary>
/// Turns compose projects into a first draft of the spec's components. It works
/// only from general signals (what an image is, whether a service is built from
/// source, its name, what it depends on) and gives the reason for every
/// decision; nothing in it knows any particular application. Whatever it can't
/// place is marked Unsure for the user to decide.
/// </summary>
/// <param name="sourceLanguage">The language a build folder's code is in, when the image doesn't say (FROM scratch).</param>
public sealed class ComponentDrafter(
    ImageCatalog catalog, Func<string?, DockerfileFacts?> readDockerfile, Func<string, string?>? sourceLanguage = null)
{
    private static readonly HashSet<string> JobWords =
    [
        "migrate", "migration", "migrations", "migrator", "seed", "seeder", "seeding", "init", "setup",
        "bootstrap", "provision", "provisioner", "flyway", "liquibase",
    ];

    private static readonly HashSet<string> FrontendWords =
        ["frontend", "front", "client", "spa", "webapp", "ui", "webui", "storefront"];

    private static readonly HashSet<string> GatewayWords =
        ["gateway", "apigateway", "bff", "proxy", "edge", "ingress"];

    private static readonly HashSet<string> HelperWords =
        ["lb", "loadbalancer", "balancer", "proxy", "pool", "pooler", "bouncer", "router"];

    private static readonly Regex ReplicaSuffix = new(
        @"^(?<base>.+?)(?:[-_]?\d+|[-_](?:replica|secondary|slave|standby|follower|read|readonly|ro|backup|mirror))$");

    private static readonly Regex NumberSuffix = new(@"[-_]?\d+$");

    private sealed class Work(ComposeProject project, ComposeService service)
    {
        public ComposeProject Project { get; } = project;
        public ComposeService Service { get; } = service;
        public string Id => $"{Project.Name}/{Service.Name}";
        public string Name { get; set; } = service.Name;
        public ComponentKind Kind { get; set; }
        public List<string> Reasons { get; } = [];
        public CatalogEntry? Known { get; set; }
        public Runtime? Runtime { get; set; }
        public bool HasHealthcheck { get; set; }
        public string? HealthcheckSource { get; set; }
        public PortGuess? Port { get; set; }
        public int Instances { get; set; } = 1;
        public string? InstancesReason { get; set; }
        public List<Dependency> DependsOn { get; } = [];
        public List<FoldedMember> Folded { get; } = [];
        public Work? FoldedInto { get; set; }

        public Work Primary => FoldedInto?.Primary ?? this;

        public string Identity => Service.Image
            ?? $"build:{Service.BuildContext}|{Service.Dockerfile ?? "Dockerfile"}";
    }

    public DraftResult Draft(IReadOnlyList<ComposeProject> projects, IReadOnlySet<string> imagesWithHealthcheck)
    {
        var works = projects
            .SelectMany(p => p.Services.Select(s => Classify(p, s, imagesWithHealthcheck)))
            .ToList();
        foreach (var project in projects)
        {
            LinkDependencies(works.Where(w => w.Project == project).ToList());
        }

        LinkAcrossProjects(works);

        var unfolded = works.ToDictionary(w => w.Id, w => ToRecord(w, includeFolds: false, w.DependsOn));
        foreach (var project in projects)
        {
            var own = works.Where(w => w.Project == project).ToList();
            FoldReplicas(own);
            FoldHelpers(own);
        }

        var components = works.Where(w => w.FoldedInto is null).ToList();
        foreach (var clash in components.GroupBy(w => w.Name).Where(g => g.Count() > 1))
        {
            foreach (var w in clash)
            {
                w.Reasons.Add($"renamed: {clash.Count()} projects have a '{w.Name}'");
                w.Name = $"{w.Project.Name}-{w.Name}";
            }
        }

        // A component depends on whatever it or anything folded into it depends
        // on, pointed at what those were folded into (a pool is now its database).
        var byId = works.ToDictionary(w => w.Id);
        List<Dependency> DependenciesOf(Work component) => works
            .Where(m => m.Primary == component)
            .SelectMany(m => m.DependsOn)
            .Select(d => d with { Id = byId[d.Id].Primary.Id })
            .Where(d => d.Id != component.Id)
            .GroupBy(d => d.Id)
            .Select(g => g.First())
            .ToList();

        var notes = new List<string>();
        return new DraftResult(
            components.Select(w => ToRecord(w, includeFolds: true, DependenciesOf(w))).ToList(),
            unfolded,
            SuggestNetworks(projects, components, notes),
            projects.ToDictionary(p => p.Name, p => (IReadOnlyList<string>)p.Services.Select(s => s.Name).ToList()),
            notes);
    }

    private Work Classify(ComposeProject project, ComposeService service, IReadOnlySet<string> imagesWithHealthcheck)
    {
        var w = new Work(project, service);
        var dockerfile = service.BuiltFromSource ? readDockerfile(service.DockerfilePath) : null;
        var words = Words(service.Name);
        var imageMatch = service.BuiltFromSource ? null : catalog.Match(service.Image);
        var baseMatch = dockerfile is null ? null : catalog.Match(dockerfile.BaseImage);
        w.Runtime = PortGuesser.RuntimeOf(service.BuiltFromSource ? dockerfile?.BaseImage : service.Image)
                    ?? (service.BuildContext is { } context && sourceLanguage?.Invoke(context) is { } language
                        ? new Runtime(language, null, null)
                        : null);

        if (words.Overlaps(JobWords))
        {
            w.Kind = ComponentKind.NotModelled;
            w.Reasons.Add("looks like a one-off job (its name)");
        }
        else if (imageMatch is not null)
        {
            w.Known = imageMatch;
            w.Kind = imageMatch.NotModelled ? ComponentKind.NotModelled : ComponentKind.Infrastructure;
            w.Reasons.Add($"image {service.Image} is {imageMatch.Label}"
                          + (imageMatch.NotModelled ? ": not part of the system under test" : ""));
        }
        else if (service.BuiltFromSource && words.Overlaps(FrontendWords))
        {
            w.Kind = ComponentKind.NotModelled;
            w.Reasons.Add("looks like a frontend (its name): the load test calls the API behind it");
        }
        else if (baseMatch is { NotModelled: false, FrontsOthers: false })
        {
            w.Known = baseMatch;
            w.Kind = ComponentKind.Infrastructure;
            w.Reasons.Add($"built on {dockerfile!.BaseImage}, which is {baseMatch.Label}");
        }
        else if (baseMatch is { FrontsOthers: true })
        {
            w.Known = baseMatch;
            w.Kind = ComponentKind.Unsure;
            w.Reasons.Add($"built on {baseMatch.Label}: a frontend served as files, or a gateway?");
        }
        else if (service.BuiltFromSource && words.Overlaps(GatewayWords))
        {
            w.Kind = ComponentKind.Unsure;
            w.Reasons.Add("built from source but named like a gateway: one of your services, or infrastructure?");
        }
        else if (service.BuiltFromSource)
        {
            w.Kind = ComponentKind.Service;
            w.Reasons.Add(w.Runtime is null
                ? "built from source"
                : $"built from source ({w.Runtime.Language})");
        }
        else
        {
            w.Kind = ComponentKind.Unsure;
            w.Reasons.Add(service.Image is null
                ? "neither an image nor a build: can't tell what it is"
                : $"image {service.Image} isn't in the catalogue: one of your services pulled from a registry, or infrastructure?");
        }

        if (service.HasHealthcheck)
        {
            (w.HasHealthcheck, w.HealthcheckSource) = (true, "compose healthcheck");
        }
        else if ((service.Image ?? $"{project.Name}-{service.Name}") is var image
                 && (imagesWithHealthcheck.Contains(image) || imagesWithHealthcheck.Contains(image + ":latest")))
        {
            (w.HasHealthcheck, w.HealthcheckSource) = (true, "healthcheck built into the image");
        }

        w.Port = PortGuesser.Guess(service, dockerfile, w.Known, w.Runtime);
        if (service.Replicas is > 1 && w.Kind == ComponentKind.Infrastructure)
        {
            w.Instances = service.Replicas.Value;
            w.InstancesReason = $"deploy.replicas: {service.Replicas}";
        }

        return w;
    }

    /// <summary>
    /// depends_on, plus another service named as a host in an environment
    /// variable (CATALOG_URL=http://catalog:8000, REDIS_HOST=redis), which is how
    /// services usually find each other at run time.
    /// </summary>
    private static void LinkDependencies(List<Work> project)
    {
        var byName = project.ToDictionary(w => w.Service.Name);
        foreach (var w in project)
        {
            foreach (var dep in w.Service.DependsOn.Where(byName.ContainsKey))
            {
                w.DependsOn.Add(new Dependency(byName[dep].Id, "depends_on"));
            }

            foreach (var (key, value) in w.Service.Environment)
            {
                foreach (var other in project.Where(o => o != w && w.DependsOn.All(d => d.Id != o.Id)))
                {
                    if (NamesHost(value, other.Service.Name))
                    {
                        w.DependsOn.Add(new Dependency(other.Id, $"environment {key}"));
                    }
                }
            }
        }
    }

    /// <summary>
    /// An app split over several compose projects calls across them by service
    /// name over a shared network (orders' AUTH_URL=http://authsvc:8000, authsvc
    /// in the other project). A name in the service's own project, or in more
    /// than one other project, isn't taken: it can't be told which one is meant.
    /// </summary>
    private static void LinkAcrossProjects(List<Work> works)
    {
        if (works.Select(w => w.Project).Distinct().Count() < 2)
        {
            return;
        }

        var networks = works.ToDictionary(w => w, w => w.Service.Networks.Select(w.Project.NetworkName).ToHashSet());
        foreach (var w in works)
        {
            var reachable = works
                .Where(o => o.Project != w.Project && networks[o].Overlaps(networks[w]))
                .GroupBy(o => o.Service.Name)
                .Where(g => g.Count() == 1 && w.Project.Services.All(s => s.Name != g.Key))
                .Select(g => g.Single())
                .ToList();
            foreach (var (key, value) in w.Service.Environment)
            {
                foreach (var other in reachable.Where(o => w.DependsOn.All(d => d.Id != o.Id)))
                {
                    if (NamesHost(value, other.Service.Name))
                    {
                        w.DependsOn.Add(new Dependency(other.Id, $"environment {key} (project {other.Project.Name})"));
                    }
                }
            }
        }
    }

    public static bool NamesHost(string value, string host) =>
        Regex.IsMatch(value, $@"(?:^|//|@|[\s,;=]){Regex.Escape(host)}(?::|/|$|[\s,;])");

    /// <summary>
    /// Several compose services that are the same thing run twice: the same
    /// image (or build) and the same name apart from a suffix (db and db-2,
    /// redis-1 and redis-2). They become one component with several instances.
    /// A copy told apart only by a number is left for the user to confirm,
    /// unless the copies name each other (a primary host, partner or seed nodes).
    /// </summary>
    private static void FoldReplicas(List<Work> project)
    {
        var groups = project
            .Where(w => w.Kind != ComponentKind.NotModelled)
            .GroupBy(w => (w.Identity, Base: BaseName(w.Service.Name)))
            .Where(g => g.Count() > 1);
        foreach (var group in groups)
        {
            var members = group.ToList();
            var primary = members.FirstOrDefault(m => m.Service.Name == group.Key.Base)
                ?? members.FirstOrDefault(m => members.Any(o => o.DependsOn.Any(d => d.Id == m.Id)))
                ?? members.OrderBy(m => m.Service.Name, StringComparer.Ordinal).First();
            var clustered = Clustered(members, group.Key.Base);
            foreach (var m in members.Where(m => m != primary))
            {
                // db-replica says what it is; db-2 could as well be a second database of its own
                var numbered = NumberSuffix.IsMatch(m.Service.Name) && !clustered;
                m.FoldedInto = primary;
                primary.Folded.Add(new FoldedMember(m.Id, "replica",
                    $"another copy of {primary.Service.Name} (same {(m.Service.Image is null ? "build" : "image")}, "
                    + $"named {m.Service.Name}): "
                    + (primary.Kind == ComponentKind.Infrastructure
                        ? numbered ? "one more instance, if it can take over when the other fails" : "counted as one more instance"
                        : "analysed together with it"),
                    Confirmed: !numbered));
            }

            if (primary.Kind == ComponentKind.Infrastructure)
            {
                var copies = Math.Max(members.Count, primary.Service.Replicas ?? 1);
                if (copies > primary.Instances)
                {
                    primary.Instances = copies;
                    primary.InstancesReason = $"{members.Count} copies: "
                        + string.Join(", ", members.Select(m => m.Service.Name).Order(StringComparer.Ordinal));
                }
            }
        }
    }

    /// <summary>
    /// The copies' settings name each other as hosts (REPMGR_PARTNER_NODES=db,db-2,
    /// RABBITMQ_SEED_NODE=rabbitmq-1): one clustered thing, not separate ones that look alike.
    /// </summary>
    private static bool Clustered(List<Work> members, string baseName)
    {
        var numbered = new Regex($@"(?:^|//|@|[\s,;=])(?<name>{Regex.Escape(baseName)}[-_]?\d+)(?=:|/|$|[\s,;])");
        foreach (var m in members)
        {
            var settings = m.Service.Environment.Values.Concat(m.Service.Command).Concat(m.Service.Entrypoint).ToList();
            var others = members.Where(o => o != m).Select(o => o.Service.Name).ToList();
            if (settings.Any(v => others.Any(o => NamesHost(v, o))
                                  || numbered.Matches(v).Any(x => x.Groups["name"].Value != m.Service.Name)))
            {
                return true;
            }
        }

        return false;
    }

    /// <summary>
    /// A pooler or proxy that serves exactly one service, and is named after it
    /// or like a helper (blog-db-pool, orders-lb), is part of that service. A
    /// proxy in front of several services, or named on its own (gateway), stays
    /// a component: it is the system's entrance.
    /// </summary>
    private static void FoldHelpers(List<Work> project)
    {
        bool changed;
        do
        {
            changed = false;
            foreach (var w in project.Where(w => w.FoldedInto is null && w.Known is { FrontsOthers: true }
                                                && w.Kind != ComponentKind.NotModelled))
            {
                var targets = w.DependsOn
                    .Where(d => d.Via == "depends_on")
                    .Select(d => project.First(o => o.Id == d.Id).Primary)
                    .Where(t => t != w && t.Kind != ComponentKind.NotModelled)
                    .Distinct()
                    .ToList();
                if (targets.Count != 1)
                {
                    continue;
                }

                var target = targets[0];
                var words = Words(w.Service.Name);
                if (!ImageCatalog.WordMatch(w.Service.Name, target.Service.Name) && !words.Overlaps(HelperWords))
                {
                    continue;
                }

                w.FoldedInto = target;
                var what = w.Known!.Category == "pooler"
                    ? w.Known.Label
                    : $"a load balancer or proxy ({w.Known.Label})";
                target.Folded.Add(new FoldedMember(w.Id, "in front",
                    $"{what} serving only {target.Service.Name}: requests to it pass through here, "
                    + "so it counts as part of it"));
                changed = true;
            }
        }
        while (changed);
    }

    private static List<NetworkSuggestion> SuggestNetworks(
        IReadOnlyList<ComposeProject> projects, List<Work> components, List<string> notes)
    {
        var usage = new Dictionary<string, (HashSet<string> Projects, int Components)>();
        foreach (var w in components.Where(w => w.Kind != ComponentKind.NotModelled))
        {
            var keys = w.Service.Networks.Count > 0 ? w.Service.Networks : ["default"];
            foreach (var name in keys.Select(w.Project.NetworkName).Distinct())
            {
                var entry = usage.GetValueOrDefault(name, ([], 0));
                entry.Projects.Add(w.Project.Name);
                usage[name] = (entry.Projects, entry.Components + 1);
            }
        }

        var shared = usage.Where(u => u.Value.Projects.Count == projects.Count).ToList();
        if (projects.Count > 1 && shared.Count == 0)
        {
            notes.Add("No docker network is shared by every project, so their services can't reach each "
                      + "other by name, and the monitoring stack can join only one network. Put the "
                      + "projects on one shared network first.");
        }

        return usage
            .OrderByDescending(u => u.Value.Projects.Count == projects.Count)
            .ThenByDescending(u => u.Value.Components)
            .ThenBy(u => u.Key, StringComparer.Ordinal)
            .Select(u => new NetworkSuggestion(u.Key, u.Value.Projects.Order(StringComparer.Ordinal).ToList(),
                u.Value.Components))
            .ToList();
    }

    private static DraftComponent ToRecord(Work w, bool includeFolds, IReadOnlyList<Dependency> dependsOn)
    {
        return new DraftComponent(
            w.Id,
            includeFolds ? w.Name : w.Service.Name,
            w.Project.Name,
            w.Service.Name,
            w.Kind,
            w.Reasons.ToList(),
            w.Service.Image,
            w.Service.BuiltFromSource,
            w.Runtime?.Language,
            w.HasHealthcheck,
            w.HealthcheckSource,
            w.Service.Restart,
            w.Service.Replicas,
            w.Port?.Port,
            w.Port?.Source,
            includeFolds ? w.Instances : Math.Max(1, w.Kind == ComponentKind.Infrastructure ? w.Service.Replicas ?? 1 : 1),
            includeFolds ? w.InstancesReason : (w.Service.Replicas is > 1 ? $"deploy.replicas: {w.Service.Replicas}" : null),
            dependsOn,
            includeFolds ? w.Folded.ToList() : [],
            w.Service.Networks.Select(w.Project.NetworkName).ToList());
    }

    private static string BaseName(string name) =>
        ReplicaSuffix.Match(name) is { Success: true } m ? m.Groups["base"].Value : name;

    private static HashSet<string> Words(string name) =>
        name.ToLowerInvariant().Split(['-', '_', '.'], StringSplitOptions.RemoveEmptyEntries).ToHashSet();
}
