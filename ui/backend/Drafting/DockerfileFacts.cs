using System.Text.Json;
using System.Text.RegularExpressions;

namespace MicroserviceScripts.Ui.Drafting;

/// <summary>
/// What a Dockerfile says about the image it builds: the base image of the
/// final stage, and the CMD, ENTRYPOINT, ENV and EXPOSE it ends up with.
/// </summary>
public sealed record DockerfileFacts(
    string? BaseImage,
    string? Command,
    string? Entrypoint,
    IReadOnlyDictionary<string, string> Environment,
    IReadOnlyList<int> Exposed)
{
    public static DockerfileFacts? Read(string? path)
    {
        if (path is null || !File.Exists(path))
        {
            return null;
        }

        try
        {
            return Parse(File.ReadAllText(path));
        }
        catch (IOException)
        {
            return null;
        }
    }

    private sealed class Stage(string baseImage, string? alias)
    {
        public string BaseImage { get; } = baseImage;
        public string? Alias { get; } = alias;
        public List<(string Instruction, string Arguments)> Lines { get; } = [];
    }

    public static DockerfileFacts Parse(string text)
    {
        var stages = new List<Stage>();
        foreach (var (instruction, rest) in Instructions(text))
        {
            if (instruction == "FROM")
            {
                var words = rest.Split(' ', StringSplitOptions.RemoveEmptyEntries)
                    .Where(w => !w.StartsWith("--"))
                    .ToList();
                if (words.Count == 0)
                {
                    continue;
                }

                var alias = words.Count >= 3 && words[1].Equals("AS", StringComparison.OrdinalIgnoreCase)
                    ? words[2]
                    : null;
                stages.Add(new Stage(words[0], alias));
            }
            else if (stages.Count > 0)
            {
                stages[^1].Lines.Add((instruction, rest));
            }
        }

        if (stages.Count == 0)
        {
            return new DockerfileFacts(null, null, null, new Dictionary<string, string>(), []);
        }

        // The final stage, and the stages it builds on ("FROM build AS final").
        var chain = new List<Stage> { stages[^1] };
        while (stages.LastOrDefault(s => s.Alias is not null && s != chain[0]
                   && s.Alias.Equals(chain[0].BaseImage, StringComparison.OrdinalIgnoreCase)
                   && stages.IndexOf(s) < stages.IndexOf(chain[0])) is { } parent)
        {
            chain.Insert(0, parent);
        }

        string? cmd = null, entrypoint = null;
        var env = new Dictionary<string, string>(StringComparer.Ordinal);
        var exposed = new List<int>();
        foreach (var (instruction, rest) in chain.SelectMany(s => s.Lines))
        {
            switch (instruction)
            {
                case "CMD":
                    cmd = ExecForm(rest);
                    break;
                case "ENTRYPOINT":
                    entrypoint = ExecForm(rest);
                    break;
                case "ENV":
                    foreach (var (key, value) in EnvPairs(rest))
                    {
                        env[key] = value;
                    }

                    break;
                case "EXPOSE":
                    exposed.AddRange(Regex.Matches(rest, @"\b(\d{2,5})\b").Select(m => int.Parse(m.Value)));
                    break;
            }
        }

        return new DockerfileFacts(chain[0].BaseImage, cmd, entrypoint, env, exposed);
    }

    /// <summary>Instructions with line continuations joined and comments dropped.</summary>
    private static IEnumerable<(string Instruction, string Arguments)> Instructions(string text)
    {
        var buffer = "";
        foreach (var raw in text.Replace("\r\n", "\n").Split('\n'))
        {
            var line = raw.Trim();
            if (buffer.Length == 0 && (line.Length == 0 || line.StartsWith('#')))
            {
                continue;
            }

            if (line.EndsWith('\\'))
            {
                buffer += line[..^1] + " ";
                continue;
            }

            var full = (buffer + line).Trim();
            buffer = "";
            var space = full.IndexOfAny([' ', '\t']);
            if (space > 0)
            {
                yield return (full[..space].ToUpperInvariant(), full[(space + 1)..].Trim());
            }
        }
    }

    /// <summary>["uvicorn", "app:app"] becomes "uvicorn app:app"; shell form stays as written.</summary>
    private static string ExecForm(string rest)
    {
        if (!rest.StartsWith('['))
        {
            return rest;
        }

        try
        {
            return string.Join(' ', JsonSerializer.Deserialize<string[]>(rest) ?? []);
        }
        catch (JsonException)
        {
            return rest;
        }
    }

    /// <summary>ENV KEY=value KEY2="two words", or the old ENV KEY value.</summary>
    private static IEnumerable<(string Key, string Value)> EnvPairs(string rest)
    {
        if (!Regex.IsMatch(rest, @"^[A-Za-z_][A-Za-z0-9_]*="))
        {
            var space = rest.IndexOf(' ');
            if (space > 0)
            {
                yield return (rest[..space], rest[(space + 1)..].Trim().Trim('"'));
            }

            yield break;
        }

        foreach (Match m in Regex.Matches(rest, @"([A-Za-z_][A-Za-z0-9_]*)=(""(?:[^""\\]|\\.)*""|'[^']*'|\S*)"))
        {
            yield return (m.Groups[1].Value, m.Groups[2].Value.Trim('"', '\''));
        }
    }
}
