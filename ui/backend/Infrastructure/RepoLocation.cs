namespace MicroserviceScripts.Ui.Infrastructure;

/// <summary>Where the Microservice-Scripts clone this app belongs to lives.</summary>
public sealed record RepoLocation(string Root)
{
    public string BackendDirectory => Path.Combine(Root, "ui", "backend");

    /// <summary>The built web page (ui/frontend builds into it).</summary>
    public string WebRoot => Path.Combine(BackendDirectory, "wwwroot");

    /// <summary>
    /// Walks up from the running program to the clone's root, the folder holding
    /// <c>analyzer/</c>; <c>MS_REPO_ROOT</c> overrides it.
    /// </summary>
    public static RepoLocation Find()
    {
        var configured = Environment.GetEnvironmentVariable("MS_REPO_ROOT");
        if (!string.IsNullOrWhiteSpace(configured))
        {
            return new RepoLocation(Path.GetFullPath(configured));
        }

        for (var dir = new DirectoryInfo(AppContext.BaseDirectory); dir is not null; dir = dir.Parent)
        {
            if (File.Exists(Path.Combine(dir.FullName, "analyzer", "__init__.py"))
                && Directory.Exists(Path.Combine(dir.FullName, "systems")))
            {
                return new RepoLocation(dir.FullName);
            }
        }

        throw new InvalidOperationException(
            "Can't find the Microservice-Scripts folder (the one holding analyzer/) above "
            + AppContext.BaseDirectory + ". Run the app from inside a clone, or set MS_REPO_ROOT.");
    }
}
