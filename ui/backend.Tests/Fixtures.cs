using MicroserviceScripts.Ui.Compose;
using MicroserviceScripts.Ui.Drafting;
using MicroserviceScripts.Ui.Infrastructure;

namespace MicroserviceScripts.Ui.Tests;

/// <summary>The captured compose projects in Fixtures/ (see Fixtures/capture.py).</summary>
public static class Fixtures
{
    public static string Directory(string name) => Path.Combine(AppContext.BaseDirectory, "Fixtures", name);

    /// <summary>A captured project, its folder pointing at the fixture so its Dockerfiles are found.</summary>
    public static ComposeProject Project(string name)
    {
        var dir = Directory(name);
        var json = File.ReadAllText(Path.Combine(dir, "compose.json"))
            .Replace("{PROJECT}", dir.Replace("\\", "\\\\"));
        return ComposeProject.Parse(json, dir, [Path.Combine(dir, "compose.yaml")]);
    }

    public static ComponentDrafter Drafter() => new(ImageCatalog.LoadDefault(), DockerfileFacts.Read);

    public static DraftResult Draft(params ComposeProject[] projects) =>
        Drafter().Draft(projects, new HashSet<string>());

    /// <summary>The analyzer's own verdict on a spec, or null where Python isn't available.</summary>
    public static async Task<Spec.SpecCheck?> PythonCheckAsync(string toml)
    {
        var runner = new ProcessRunner();
        var python = new PythonLocator(runner);
        if (await python.FindAsync() is null)
        {
            return null;
        }

        return await new Spec.SpecChecker(runner, python, RepoLocation.Find()).CheckAsync(toml, default);
    }
}
