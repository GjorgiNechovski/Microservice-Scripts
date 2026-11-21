using System.Text.RegularExpressions;

namespace MicroserviceScripts.Ui.Spec;

/// <summary>A component as the spec needs it.</summary>
/// <param name="Name">Its name in the spec (and in its telemetry).</param>
/// <param name="Project">The compose project it runs in.</param>
/// <param name="ComposeService">Its compose service, when that differs from Name.</param>
/// <param name="Instances">Infrastructure only: instances with failover.</param>
public sealed record SpecComponent(
    string Name,
    string Project,
    string ComposeService,
    int? ReadinessPort = null,
    string? ReadinessSource = null,
    bool HasHealthcheck = false,
    int Instances = 1);

/// <summary>The spec's [load] section: which k6 script runs, with how many users, for how long.</summary>
/// <param name="Script">Relative to the spec's folder, or absolute.</param>
/// <param name="Users">Virtual users per level; several make a sweep, one is a single run.</param>
/// <param name="Duration">How long each level is held, in k6's form ("2m", "90s").</param>
/// <param name="Env">Values the script reads from __ENV.</param>
/// <param name="EnvPassthrough">Variables passed through from the environment it runs in (secrets).</param>
public sealed record SpecLoad(
    string Script,
    IReadOnlyList<int> Users,
    string Duration,
    IReadOnlyDictionary<string, string>? Env = null,
    IReadOnlyList<string>? EnvPassthrough = null);

/// <summary>Everything the setup app writes into a spec.</summary>
/// <param name="ProjectServices">
/// Every compose service name per chosen project: a component whose service name
/// appears in two of them gets pinned to its own project.
/// </param>
public sealed record SpecDocument(
    string Name,
    IReadOnlyList<string> ComposeProjects,
    IReadOnlyList<SpecComponent> Services,
    IReadOnlyList<SpecComponent> Infrastructure,
    IReadOnlyDictionary<string, IReadOnlyList<string>>? ProjectServices = null,
    IReadOnlyDictionary<string, IReadOnlyList<string>>? Operations = null,
    SpecLoad? Load = null)
{
    private static readonly Regex ComponentName = new(@"^[A-Za-z0-9][A-Za-z0-9_.-]*$");
    // what the analyzer reads back (validity.duration_seconds): hours, minutes, seconds, in that order
    private static readonly Regex Duration = new(@"^(?=\d)(\d+h)?(\d+m)?(\d+s)?$");

    public IEnumerable<SpecComponent> Components => Services.Concat(Infrastructure);

    /// <summary>What is wrong with it before it's even written, in words for the user.</summary>
    public IReadOnlyList<string> Problems()
    {
        var problems = new List<string>();
        if (string.IsNullOrWhiteSpace(Name))
        {
            problems.Add("Give the system a name.");
        }

        if (Services.Count == 0)
        {
            problems.Add("Pick at least one service: the analysis needs an application tier.");
        }

        foreach (var c in Components)
        {
            if (!ComponentName.IsMatch(c.Name))
            {
                problems.Add($"'{c.Name}': use letters, digits, '-', '_' and '.' only.");
            }

            if (c.ReadinessPort is < 1 or > 65535)
            {
                problems.Add($"{c.Name}: port {c.ReadinessPort} is not a port.");
            }

            if (c.Instances < 1)
            {
                problems.Add($"{c.Name}: it runs at least one instance.");
            }
        }

        foreach (var name in Components.GroupBy(c => c.Name).Where(g => g.Count() > 1).Select(g => g.Key))
        {
            problems.Add($"Two components are called '{name}'.");
        }

        var known = Components.Select(c => c.Name).ToHashSet();
        foreach (var (operation, needs) in Operations ?? new Dictionary<string, IReadOnlyList<string>>())
        {
            if (string.IsNullOrWhiteSpace(operation))
            {
                problems.Add("Every feature needs a name.");
            }

            if (needs.Count == 0)
            {
                problems.Add($"Feature '{operation}': pick the components it needs.");
            }

            foreach (var missing in needs.Where(n => !known.Contains(n)))
            {
                problems.Add($"Feature '{operation}' needs '{missing}', which isn't a service or infrastructure.");
            }
        }

        if (Load is { } load)
        {
            if (string.IsNullOrWhiteSpace(load.Script))
            {
                problems.Add("Choose the k6 script the load test runs.");
            }

            if (load.Users.Count == 0 || load.Users.Any(u => u is < 1 or > 100_000))
            {
                problems.Add("Give at least one number of users, each between 1 and 100000.");
            }

            if (!Duration.IsMatch(load.Duration))
            {
                problems.Add($"'{load.Duration}' isn't a duration the analyzer reads: write it like 2m, 90s or 1h30m (hours, minutes, seconds, in that order).");
            }
        }

        return problems;
    }

    /// <summary>
    /// Whether a component must be pinned to its project: its service name is
    /// shared by several of the app's projects, which the analyzer refuses to
    /// guess between.
    /// </summary>
    public bool NeedsPin(SpecComponent c) =>
        ComposeProjects.Count > 1
        && (ProjectServices ?? new Dictionary<string, IReadOnlyList<string>>())
            .Count(p => ComposeProjects.Contains(p.Key) && p.Value.Contains(c.ComposeService)) > 1;
}
