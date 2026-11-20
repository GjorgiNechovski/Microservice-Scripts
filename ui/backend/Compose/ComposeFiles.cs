namespace MicroserviceScripts.Ui.Compose;

public enum ComposeFileRole
{
    /// <summary>The file <c>docker compose</c> reads by default.</summary>
    Base,

    /// <summary><c>compose.override.yaml</c>: Compose applies it automatically.</summary>
    Override,

    /// <summary>Any other compose file: only used when chosen.</summary>
    Overlay,
}

public static class ComposeFiles
{
    /// <summary>The names <c>docker compose</c> looks for, in its own order of preference.</summary>
    public static readonly string[] BaseNames =
        ["compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"];

    /// <summary>The role a file name suggests, or null if it doesn't look like a compose file.</summary>
    public static ComposeFileRole? RoleOf(string fileName)
    {
        var name = fileName.ToLowerInvariant();
        if (BaseNames.Contains(name))
        {
            return ComposeFileRole.Base;
        }

        if (!name.EndsWith(".yaml") && !name.EndsWith(".yml"))
        {
            return null;
        }

        var stem = name[..name.LastIndexOf('.')];
        if (stem is "compose.override" or "docker-compose.override")
        {
            return ComposeFileRole.Override;
        }

        return stem.StartsWith("compose.") || stem.StartsWith("docker-compose.")
            || stem.StartsWith("docker-compose-") || stem.EndsWith(".compose")
            || stem.EndsWith("-compose")
            ? ComposeFileRole.Overlay
            : null;
    }

    public static bool HasBaseFile(string directory) =>
        BaseNames.Any(n => File.Exists(Path.Combine(directory, n)));

    public static bool IsDockerfile(string fileName)
    {
        var name = fileName.ToLowerInvariant();
        return name == "dockerfile" || name.StartsWith("dockerfile.") || name.EndsWith(".dockerfile");
    }
}
