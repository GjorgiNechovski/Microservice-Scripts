using MicroserviceScripts.Ui.Infrastructure;

namespace MicroserviceScripts.Ui.Setup;

/// <param name="Required">False for something that only adds extras (figures), or is needed only later.</param>
/// <param name="Fix">What to do about it, when not ok.</param>
/// <param name="Later">Not needed to draft the spec, only to start the app and the run (a stopped Docker engine).</param>
public sealed record PrerequisiteCheck(
    string Id, string Label, bool Required, bool Ok, string Detail, string? Fix, bool Later = false);

/// <summary>What the analyzer needs on this machine, checked before anything else.</summary>
public sealed class Prerequisites(IProcessRunner runner, PythonLocator python, RepoLocation repo)
{
    private static readonly TimeSpan Timeout = TimeSpan.FromSeconds(20);

    public async Task<IReadOnlyList<PrerequisiteCheck>> CheckAsync(CancellationToken cancellationToken)
    {
        var dockerTask = runner.RunAsync(
            "docker", ["version", "--format", "{{.Server.Version}}"], timeout: Timeout,
            cancellationToken: cancellationToken);
        var composeTask = runner.RunAsync(
            "docker", ["compose", "version", "--short"], timeout: Timeout,
            cancellationToken: cancellationToken);
        var pythonTask = python.FindAsync(cancellationToken);
        await Task.WhenAll(dockerTask, composeTask, pythonTask);

        var docker = await dockerTask;
        var compose = await composeTask;
        var py = await pythonTask;
        var checks = new List<PrerequisiteCheck>
        {
            docker.Ok
                ? new("docker", "Docker", true, true, $"Docker Engine {docker.StdOut.Trim()}", null)
                : docker.NotFound
                    ? new("docker", "Docker", true, false, FirstLine(docker.Error),
                        "Install Docker: https://docs.docker.com/get-docker/")
                    // drafting reads only files (Compose reads them without the engine): go on, start it later
                    : new("docker", "Docker", false, false, FirstLine(docker.Error),
                        "Start Docker before you start your app or the run. Drafting the spec only reads files, so "
                        + "you can go on now; healthchecks built into images are seen once it answers.", Later: true),
            compose.Ok
                ? new("compose", "Docker Compose v2", true, true, $"Compose {compose.StdOut.Trim()}", null)
                : new("compose", "Docker Compose v2", true, false, FirstLine(compose.Error),
                    "Install the Docker Compose v2 plugin (the `docker compose` command)."),
        };

        if (py is null)
        {
            checks.Add(new("python", "Python 3.11 or newer", true, false,
                "No python3, python or py found.",
                "Install Python 3.11 or newer: https://www.python.org/downloads/ (or set MS_PYTHON)"));
            return checks;
        }

        if (py.Version < new Version(3, 11))
        {
            checks.Add(new("python", "Python 3.11 or newer", true, false,
                $"Python {py.Version} (`{py.FileName}`)",
                "Install Python 3.11 or newer (the analyzer needs its tomllib), or set MS_PYTHON to one you have."));
            return checks;
        }

        checks.Add(new("python", "Python 3.11 or newer", true, true,
            $"Python {py.Version} (`{py.FileName}`)", null));

        var analyzer = await runner.RunAsync(
            py.FileName, py.Arguments("-c", "import analyzer.spec"), repo.Root, Timeout,
            cancellationToken);
        checks.Add(analyzer.Ok
            ? new("analyzer", "The analyzer", true, true, $"in {repo.Root}", null)
            : new("analyzer", "The analyzer", true, false, FirstLine(analyzer.Error),
                "Run the app from a complete clone of Microservice-Scripts."));

        var figures = await runner.RunAsync(
            py.FileName, py.Arguments("-c", "import numpy, matplotlib"), repo.Root, Timeout,
            cancellationToken);
        checks.Add(new("figures", "Figures (numpy and matplotlib)", false, figures.Ok,
            figures.Ok ? "installed" : "not installed: reports are written without figures",
            figures.Ok ? null : "Optional: pip install numpy matplotlib"));
        return checks;
    }

    private static string FirstLine(string text)
    {
        var line = text.Split('\n', StringSplitOptions.RemoveEmptyEntries).FirstOrDefault();
        return string.IsNullOrWhiteSpace(line) ? "failed with no message" : line.Trim();
    }
}
