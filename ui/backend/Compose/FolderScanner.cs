using MicroserviceScripts.Ui.Setup;

namespace MicroserviceScripts.Ui.Compose;

public sealed record ScannedFile(string Path, string Name, ComposeFileRole Role, bool Selected);

/// <summary>The compose files found in one folder: a candidate compose project.</summary>
public sealed record ScannedProject(string Directory, IReadOnlyList<ScannedFile> Files);

public sealed record ScanResult(
    IReadOnlyList<ScannedProject> Projects, IReadOnlyList<string> Dockerfiles, IReadOnlyList<string> Warnings);

/// <summary>
/// Looks for compose files and Dockerfiles inside the folders the user picked,
/// one per repository or one holding several, and only there. Dependency and
/// build folders are skipped, and the depth is limited, so pointing it at a big
/// folder stays quick.
/// </summary>
public static class FolderScanner
{
    public const int MaxDepth = 6;
    public const int MaxFolders = 20_000;

    private static readonly HashSet<string> Skipped = new(StringComparer.OrdinalIgnoreCase)
    {
        ".git", ".hg", ".svn", "node_modules", "bin", "obj", "dist", "build", "target", "out",
        ".venv", "venv", "__pycache__", ".idea", ".vs", ".vscode", ".next", ".nuxt", ".gradle",
        ".terraform", "vendor", "coverage", ".cache", ".pytest_cache", ".mypy_cache",
    };

    /// <param name="folders">Folders to search.</param>
    /// <param name="extraFiles">
    /// Compose files the user pointed at directly, whatever they're called; they
    /// join their folder's project and are always selected.
    /// </param>
    public static ScanResult Scan(IEnumerable<string> folders, IEnumerable<string>? extraFiles = null)
    {
        var warnings = new List<string>();
        var byDirectory = new SortedDictionary<string, List<ScannedFile>>(StringComparer.Ordinal);
        var dockerfiles = new SortedSet<string>(StringComparer.Ordinal);
        var visited = 0;

        foreach (var folder in folders.Select(f => Path.GetFullPath(f)).Distinct())
        {
            if (!Directory.Exists(folder))
            {
                warnings.Add($"No such folder: {folder}");
                continue;
            }

            var pending = new Stack<(string Dir, int Depth)>();
            pending.Push((folder, 0));
            while (pending.Count > 0)
            {
                var (dir, depth) = pending.Pop();
                if (++visited > MaxFolders)
                {
                    warnings.Add($"Stopped after {MaxFolders} folders: pick a narrower folder.");
                    pending.Clear();
                    break;
                }

                foreach (var file in SafeFiles(dir))
                {
                    var name = Path.GetFileName(file);
                    if (ComposeFiles.IsDockerfile(name))
                    {
                        dockerfiles.Add(file);
                    }
                    else if (ComposeFiles.RoleOf(name) is { } role)
                    {
                        Add(byDirectory, dir, file, role);
                    }
                }

                if (depth >= MaxDepth)
                {
                    continue;
                }

                foreach (var sub in SafeDirectories(dir))
                {
                    var name = Path.GetFileName(sub);
                    if (!Skipped.Contains(name) && !name.StartsWith('.'))
                    {
                        pending.Push((sub, depth + 1));
                    }
                }
            }
        }

        foreach (var extra in (extraFiles ?? []).Select(f => Path.GetFullPath(FolderBrowser.ExpandHome(f.Trim()))))
        {
            if (!File.Exists(extra))
            {
                warnings.Add($"No such file: {extra}");
                continue;
            }

            var dir = Path.GetDirectoryName(extra)!;
            var hasBase = byDirectory.TryGetValue(dir, out var known)
                && known.Any(f => f.Role == ComposeFileRole.Base);
            var role = ComposeFiles.RoleOf(Path.GetFileName(extra))
                ?? (hasBase ? ComposeFileRole.Overlay : ComposeFileRole.Base);
            Add(byDirectory, dir, extra, role, forceSelected: true);
        }

        var projects = byDirectory.Select(kv => new ScannedProject(kv.Key, Order(kv.Value))).ToList();
        return new ScanResult(projects, dockerfiles.ToList(), warnings);
    }

    private static void Add(
        SortedDictionary<string, List<ScannedFile>> byDirectory, string dir, string file,
        ComposeFileRole role, bool forceSelected = false)
    {
        if (!byDirectory.TryGetValue(dir, out var files))
        {
            byDirectory[dir] = files = [];
        }

        var existing = files.FindIndex(f => f.Path == file);
        var scanned = new ScannedFile(file, Path.GetFileName(file), role, forceSelected);
        if (existing >= 0)
        {
            files[existing] = scanned with { Selected = files[existing].Selected || forceSelected };
        }
        else
        {
            files.Add(scanned);
        }
    }

    /// <summary>
    /// Base first, then overrides, then overlays. Selected by default: the base
    /// file Compose itself would pick and the overrides it applies on its own,
    /// which is what <c>docker compose up</c> in that folder runs.
    /// </summary>
    private static IReadOnlyList<ScannedFile> Order(List<ScannedFile> files)
    {
        var ordered = files
            .OrderBy(f => f.Role)
            .ThenBy(f => Array.IndexOf(ComposeFiles.BaseNames, f.Name.ToLowerInvariant()) is var i && i >= 0 ? i : int.MaxValue)
            .ThenBy(f => f.Name, StringComparer.Ordinal)
            .ToList();
        var baseChosen = false;
        for (var i = 0; i < ordered.Count; i++)
        {
            var f = ordered[i];
            var selected = f.Selected || f.Role switch
            {
                ComposeFileRole.Base when !baseChosen => true,
                ComposeFileRole.Override => true,
                _ => false,
            };
            baseChosen |= f.Role == ComposeFileRole.Base && selected;
            ordered[i] = f with { Selected = selected };
        }

        return ordered;
    }

    private static IEnumerable<string> SafeFiles(string dir)
    {
        try
        {
            return Directory.EnumerateFiles(dir).ToList();
        }
        catch (Exception ex) when (ex is UnauthorizedAccessException or IOException)
        {
            return [];
        }
    }

    private static IEnumerable<string> SafeDirectories(string dir)
    {
        try
        {
            return new DirectoryInfo(dir).EnumerateDirectories()
                .Where(d => !d.Attributes.HasFlag(FileAttributes.ReparsePoint))   // no symlink loops
                .Select(d => d.FullName)
                .ToList();
        }
        catch (Exception ex) when (ex is UnauthorizedAccessException or IOException)
        {
            return [];
        }
    }
}
