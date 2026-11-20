using System.Text.Json;

namespace MicroserviceScripts.Ui.Drafting;

public sealed record CatalogEntry(string[] Names, string Category, string Role, string Label, int? Port)
{
    public bool NotModelled => Role == "notModelled";

    /// <summary>Sits in front of other services: a pooler or a proxy.</summary>
    public bool FrontsOthers => Category is "pooler" or "proxy";
}

/// <summary>Known images (Data/infrastructure-images.json) and what they are.</summary>
public sealed class ImageCatalog(IReadOnlyList<CatalogEntry> entries)
{
    private static readonly char[] Separators = ['-', '_', '.'];

    public static ImageCatalog Load(string path)
    {
        using var stream = File.OpenRead(path);
        using var doc = JsonDocument.Parse(stream);
        var options = new JsonSerializerOptions { PropertyNameCaseInsensitive = true };
        var entries = doc.RootElement.GetProperty("entries").Deserialize<List<CatalogEntry>>(options)
            ?? throw new InvalidDataException($"{path}: no entries");
        return new ImageCatalog(entries);
    }

    public static ImageCatalog LoadDefault() =>
        Load(Path.Combine(AppContext.BaseDirectory, "Data", "infrastructure-images.json"));

    /// <summary>
    /// What an image is, or null if the catalogue doesn't know it. The image's
    /// last path part is tried first against every entry (grafana/k6 is k6, not
    /// Grafana), then the parts before it (mcr.microsoft.com/mssql/server).
    /// </summary>
    public CatalogEntry? Match(string? image)
    {
        var parts = PathParts(image);
        for (var i = parts.Count - 1; i >= 0; i--)
        {
            foreach (var entry in entries)
            {
                if (entry.Names.Any(name => WordMatch(parts[i], name)))
                {
                    return entry;
                }
            }
        }

        return null;
    }

    /// <summary>
    /// The image's repository path without registry, tag or digest:
    /// "docker.io/bitnami/postgresql:16" gives ["bitnami", "postgresql"].
    /// </summary>
    public static IReadOnlyList<string> PathParts(string? image)
    {
        if (string.IsNullOrWhiteSpace(image))
        {
            return [];
        }

        var name = image.Trim().ToLowerInvariant();
        var at = name.IndexOf('@');
        if (at >= 0)
        {
            name = name[..at];
        }

        var colon = name.LastIndexOf(':');
        if (colon > name.LastIndexOf('/'))
        {
            name = name[..colon];
        }

        var parts = name.Split('/', StringSplitOptions.RemoveEmptyEntries).ToList();
        if (parts.Count > 1 && (parts[0].Contains('.') || parts[0].Contains(':') || parts[0] == "localhost"))
        {
            parts.RemoveAt(0);   // a registry host
        }

        return parts;
    }

    /// <summary>"cp-kafka" matches "kafka" and "postgresql-repmgr" matches "postgresql", by whole words.</summary>
    public static bool WordMatch(string part, string name)
    {
        if (part == name)
        {
            return true;
        }

        foreach (var sep in Separators)
        {
            if (part.StartsWith(name + sep) || part.EndsWith(sep + name) || part.Contains(sep + name + sep))
            {
                return true;
            }
        }

        return false;
    }
}
