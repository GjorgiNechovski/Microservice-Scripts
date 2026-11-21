using System.Text.Json;
using MicroserviceScripts.Ui.Infrastructure;

namespace MicroserviceScripts.Ui.Compose;

/// <summary>A compose project chosen by the user: a folder, its files, optionally a name.</summary>
/// <param name="Name">
/// Only when the app is started with <c>-p</c>; otherwise Compose decides
/// (a <c>name:</c> in the file, or the folder's name).
/// </param>
public sealed record ProjectSelection(string Directory, IReadOnlyList<string> Files, string? Name = null);

public sealed record RunningProject(string Name, string Status, IReadOnlyList<string> Files, string? Directory);

/// <summary>The few <c>docker compose</c> commands the setup needs to read projects; none starts anything.</summary>
public sealed class ComposeCli(IProcessRunner runner)
{
    private static readonly TimeSpan Timeout = TimeSpan.FromSeconds(60);

    /// <summary>
    /// Reads a project the way Compose itself does. The error, when there is one,
    /// is Compose's own message (a missing variable, a bad file), meant for the user.
    /// </summary>
    public async Task<(ComposeProject? Project, string? Error)> ReadAsync(
        ProjectSelection selection, CancellationToken cancellationToken)
    {
        if (selection.Files.Count == 0)
        {
            return (null, "No compose file chosen.");
        }

        var args = ProjectArguments(selection);
        args.AddRange(["config", "--format", "json"]);
        var result = await runner.RunAsync("docker", args, selection.Directory, Timeout, cancellationToken);
        if (!result.Ok)
        {
            return (null, result.Error);
        }

        try
        {
            return (ComposeProject.Parse(result.StdOut, selection.Directory, selection.Files), null);
        }
        catch (JsonException ex)
        {
            return (null, $"Couldn't read Compose's output: {ex.Message}");
        }
    }

    /// <summary><c>compose</c> and the options that pick exactly the chosen files and project name.</summary>
    public static List<string> ProjectArguments(ProjectSelection selection)
    {
        var args = new List<string> { "compose", "--project-directory", selection.Directory };
        foreach (var file in selection.Files)
        {
            args.Add("-f");
            args.Add(file);
        }

        if (!string.IsNullOrWhiteSpace(selection.Name))
        {
            args.Add("-p");
            args.Add(selection.Name);
        }

        return args;
    }

    /// <summary>Compose projects on this machine, running or stopped, with their files.</summary>
    public async Task<IReadOnlyList<RunningProject>> ListAsync(CancellationToken cancellationToken)
    {
        var result = await runner.RunAsync(
            "docker", ["compose", "ls", "--all", "--format", "json"], timeout: Timeout,
            cancellationToken: cancellationToken);
        if (!result.Ok || string.IsNullOrWhiteSpace(result.StdOut))
        {
            return [];
        }

        var projects = new List<RunningProject>();
        try
        {
            using var doc = JsonDocument.Parse(result.StdOut);
            if (doc.RootElement.ValueKind != JsonValueKind.Array)
            {
                return [];
            }

            foreach (var p in doc.RootElement.EnumerateArray())
            {
                var files = (p.TryGetProperty("ConfigFiles", out var cf) ? cf.GetString() ?? "" : "")
                    .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
                projects.Add(new RunningProject(
                    p.TryGetProperty("Name", out var name) ? name.GetString() ?? "" : "",
                    p.TryGetProperty("Status", out var st) ? st.GetString() ?? "" : "",
                    files,
                    files.Length > 0 ? Path.GetDirectoryName(files[0]) : null));
            }
        }
        catch (JsonException)
        {
            // an older Compose, or a warning printed first: no list rather than an error on every page that asks
            return [];
        }

        return projects;
    }

    /// <summary>
    /// Which images carry a healthcheck of their own (a HEALTHCHECK in their
    /// Dockerfile), which the compose file doesn't show. Only images already on
    /// this machine can be asked; the rest are simply missing from the result.
    /// </summary>
    public async Task<IReadOnlySet<string>> ImagesWithHealthcheckAsync(
        IEnumerable<string> images, CancellationToken cancellationToken)
    {
        var distinct = images.Where(i => !string.IsNullOrWhiteSpace(i)).Distinct().ToList();
        var found = new HashSet<string>(StringComparer.Ordinal);
        if (distinct.Count == 0)
        {
            return found;
        }

        // exits non-zero when any image is missing, but still prints the others
        var result = await runner.RunAsync(
            "docker", ["image", "inspect", .. distinct], timeout: Timeout,
            cancellationToken: cancellationToken);
        if (string.IsNullOrWhiteSpace(result.StdOut))
        {
            return found;
        }

        try
        {
            using var doc = JsonDocument.Parse(result.StdOut);
            foreach (var image in doc.RootElement.EnumerateArray())
            {
                var hasCheck = image.TryGetProperty("Config", out var config)
                    && config.ValueKind == JsonValueKind.Object
                    && config.TryGetProperty("Healthcheck", out var hc) && hc.ValueKind == JsonValueKind.Object
                    && hc.TryGetProperty("Test", out var test) && test.ValueKind == JsonValueKind.Array
                    && test.GetArrayLength() > 0 && test[0].GetString() != "NONE";
                if (!hasCheck || !image.TryGetProperty("RepoTags", out var tags))
                {
                    continue;
                }

                foreach (var tag in tags.EnumerateArray().Select(t => t.GetString()).OfType<string>())
                {
                    found.Add(tag);
                    if (tag.EndsWith(":latest"))
                    {
                        found.Add(tag[..^":latest".Length]);
                    }
                }
            }
        }
        catch (JsonException)
        {
            // no answer is no evidence: treat every image as having none
        }

        return found;
    }
}
