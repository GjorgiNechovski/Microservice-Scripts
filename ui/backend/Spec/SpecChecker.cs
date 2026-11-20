using System.Text.Json;
using MicroserviceScripts.Ui.Infrastructure;

namespace MicroserviceScripts.Ui.Spec;

public sealed record SpecCheck(bool Ok, string? Error);

/// <summary>
/// Asks the analyzer itself whether a spec is valid
/// (<c>python3 -m analyzer.spec --json</c>), so the app never writes a spec the
/// command line would refuse.
/// </summary>
public sealed class SpecChecker(IProcessRunner runner, PythonLocator python, RepoLocation repo)
{
    public async Task<SpecCheck> CheckAsync(string toml, CancellationToken cancellationToken)
    {
        var py = await python.FindAsync(cancellationToken);
        if (py is null)
        {
            return new SpecCheck(false, "Python wasn't found, so the spec couldn't be checked.");
        }

        var dir = Directory.CreateTempSubdirectory("ms-setup-");
        try
        {
            var path = Path.Combine(dir.FullName, "spec.toml");
            await File.WriteAllTextAsync(path, toml, cancellationToken);
            var result = await runner.RunAsync(
                py.FileName, py.Arguments("-m", "analyzer.spec", path, "--json"), repo.Root,
                TimeSpan.FromSeconds(30), cancellationToken);
            var line = result.StdOut.Split('\n', StringSplitOptions.RemoveEmptyEntries).LastOrDefault();
            if (line is null)
            {
                return new SpecCheck(false, $"The analyzer couldn't check it: {result.Error}");
            }

            using var verdict = JsonDocument.Parse(line);
            var root = verdict.RootElement;
            return root.GetProperty("ok").GetBoolean()
                ? new SpecCheck(true, null)
                : new SpecCheck(false, root.GetProperty("error").GetString());
        }
        catch (JsonException)
        {
            return new SpecCheck(false, "The analyzer's answer couldn't be read.");
        }
        finally
        {
            dir.Delete(recursive: true);
        }
    }
}
