using MicroserviceScripts.Ui.Compose;

namespace MicroserviceScripts.Ui.Setup;

public sealed record FolderEntry(string Name, string Path, bool HasCompose);

public sealed record FolderListing(
    string Path, string? Parent, IReadOnlyList<FolderEntry> Folders, IReadOnlyList<string> Roots);

/// <summary>
/// The folder picker's view of the disk. A browser can't hand a page the real
/// path of a folder, so the server lists them instead.
/// </summary>
public static class FolderBrowser
{
    public static FolderListing List(string? path)
    {
        var dir = string.IsNullOrWhiteSpace(path) ? Home() : Path.GetFullPath(ExpandHome(path.Trim()));
        if (!Directory.Exists(dir))
        {
            throw new DirectoryNotFoundException($"No such folder: {dir}");
        }

        var options = new EnumerationOptions { IgnoreInaccessible = true, AttributesToSkip = FileAttributes.System };
        var folders = new DirectoryInfo(dir)
            .EnumerateDirectories("*", options)
            .Where(d => !d.Name.StartsWith('.'))
            .OrderBy(d => d.Name, StringComparer.OrdinalIgnoreCase)
            .Select(d => new FolderEntry(d.Name, d.FullName, ComposeFiles.HasBaseFile(d.FullName)))
            .ToList();
        IReadOnlyList<string> roots = OperatingSystem.IsWindows()
            ? DriveInfo.GetDrives().Where(d => d.IsReady).Select(d => d.RootDirectory.FullName).ToList()
            : ["/", Home()];
        return new FolderListing(dir, Directory.GetParent(dir)?.FullName, folders, roots);
    }

    public static string ExpandHome(string path) =>
        path == "~" || path.StartsWith("~/") || path.StartsWith("~\\")
            ? Path.Combine(Home(), path.Length > 2 ? path[2..] : "")
            : path;

    private static string Home() => Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
}
