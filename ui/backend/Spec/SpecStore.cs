using System.Text;
using MicroserviceScripts.Ui.Setup;

namespace MicroserviceScripts.Ui.Spec;

/// <param name="Backup">Where the file it replaced was copied, if it replaced one.</param>
/// <param name="ScriptPath">The k6 script written next to it, if one was.</param>
public sealed record SaveResult(string Path, string? Backup, string? ScriptPath = null, string? ScriptBackup = null);

/// <summary>Where a spec goes, and writing it without ever losing the previous one.</summary>
public static class SpecStore
{
    /// <summary>The full path for a file name in a folder; refuses names that point elsewhere.</summary>
    public static string Resolve(string directory, string fileName, string extension = ".toml")
    {
        var name = fileName.Trim();
        if (name.Length == 0 || name.IndexOfAny(['/', '\\']) >= 0 || name.StartsWith('.')
            || name.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0)
        {
            throw new ArgumentException($"'{fileName}' isn't a plain file name.");
        }

        if (!name.EndsWith(extension, StringComparison.OrdinalIgnoreCase))
        {
            name += extension;
        }

        // relative to what? The app's own folder would be a surprise: ask for the whole path
        var folder = FolderBrowser.ExpandHome(directory.Trim());
        if (!Path.IsPathFullyQualified(folder))
        {
            throw new ArgumentException($"Give the whole path of the folder to save in, not '{directory}'.");
        }

        return Path.Combine(Path.GetFullPath(folder), name);
    }

    public static SaveResult Write(string path, string toml, DateTime? now = null) =>
        new(path, WriteKeepingOld(path, toml, now));

    /// <summary>Writes a file; the one it replaces is first copied to a dated .bak, whose path is returned.</summary>
    public static string? WriteKeepingOld(string path, string text, DateTime? now = null)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        string? backup = null;
        if (File.Exists(path))
        {
            // never over an earlier backup: two saves in one second would lose the original
            var stamp = $"{path}.{(now ?? DateTime.Now):yyyyMMdd-HHmmss}";
            backup = $"{stamp}.bak";
            for (var n = 2; File.Exists(backup); n++)
            {
                backup = $"{stamp}-{n}.bak";
            }

            File.Copy(path, backup, overwrite: false);
        }

        File.WriteAllText(path, text, new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
        return backup;
    }
}
