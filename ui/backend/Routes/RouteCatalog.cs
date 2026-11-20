using System.Text.Json;
using System.Text.RegularExpressions;

namespace MicroserviceScripts.Ui.Routes;

/// <summary>A route declared in a service's own code.</summary>
/// <param name="Path">As the service sees it, with parameters written {name}.</param>
/// <param name="Handler">The function that handles it, when the pattern could tell.</param>
/// <param name="File">The file it is declared in.</param>
/// <param name="Signature">The handler's parameter list as written, when it could be found.</param>
/// <param name="HandlerBody">The handler's body, when found (Go and Rust read the request inside it).</param>
public sealed record SourceRoute(
    string Method,
    string Path,
    string? Handler,
    string File,
    int Line,
    string Framework,
    string? Signature = null,
    string? HandlerBody = null);

/// <summary>Which language a folder's code is in, and whether routes can be read from it.</summary>
/// <param name="Language">The language most of its source files are in; null when none was recognised.</param>
/// <param name="Supported">Some framework of that language is in the route patterns.</param>
public sealed record CodeLanguage(string? Language, bool Supported, IReadOnlyDictionary<string, int> Files);

/// <summary>
/// Finds routes in source code with the patterns of Data/route-patterns.json:
/// regular expressions per framework, so supporting another framework is a
/// data change. It reads files only; nothing is run.
/// </summary>
public sealed class RouteCatalog
{
    private const int MaxFiles = 4000;
    private const long MaxFileBytes = 1_000_000;
    private const int MaxBody = 20_000;
    private static readonly string[] Verbs = ["GET", "POST", "PUT", "DELETE", "PATCH"];
    private static readonly TimeSpan RegexTimeout = TimeSpan.FromSeconds(2);

    private readonly IReadOnlyList<Framework> frameworks;
    private readonly HashSet<string> skipFolders;
    private readonly Regex? skipFiles;
    private readonly IReadOnlyDictionary<string, string> languages;
    private readonly IReadOnlyDictionary<string, string> functionDefs;

    private RouteCatalog(
        IReadOnlyList<Framework> frameworks,
        IEnumerable<string> skipFolders,
        string? skipFiles,
        IReadOnlyDictionary<string, string> languages,
        IReadOnlyDictionary<string, string> functionDefs)
    {
        this.frameworks = frameworks;
        this.skipFolders = new HashSet<string>(skipFolders, StringComparer.OrdinalIgnoreCase);
        this.skipFiles = skipFiles is null ? null : new Regex(skipFiles, RegexOptions.None, RegexTimeout);
        this.languages = languages;
        this.functionDefs = functionDefs;
    }

    public IEnumerable<string> FrameworkNames => frameworks.Select(f => f.Name);

    /// <summary>The languages some framework's routes can be read for.</summary>
    public IReadOnlySet<string> SupportedLanguages =>
        frameworks.SelectMany(f => f.Extensions).Select(e => languages.GetValueOrDefault(e)).OfType<string>().ToHashSet();

    public static RouteCatalog LoadDefault() =>
        Load(Path.Combine(AppContext.BaseDirectory, "Data", "route-patterns.json"));

    public static RouteCatalog Load(string path)
    {
        using var doc = JsonDocument.Parse(File.ReadAllText(path));
        var root = doc.RootElement;
        var frameworks = root.GetProperty("frameworks").EnumerateArray().Select(Framework.Parse).ToList();
        var skipFolders = root.TryGetProperty("skipFolders", out var sf)
            ? sf.EnumerateArray().Select(x => x.GetString()!).ToList()
            : [];
        var skipFiles = root.TryGetProperty("skipFiles", out var sfi) ? sfi.GetString() : null;
        static Dictionary<string, string> Map(JsonElement root, string name) =>
            root.TryGetProperty(name, out var m)
                ? m.EnumerateObject().ToDictionary(p => p.Name, p => p.Value.GetString()!, StringComparer.OrdinalIgnoreCase)
                : new Dictionary<string, string>();
        return new RouteCatalog(frameworks, skipFolders, skipFiles, Map(root, "languages"), Map(root, "functionDefs"));
    }

    /// <summary>
    /// Every route declared under <paramref name="root"/>, leaving out folders in
    /// <paramref name="exclude"/> (another service's code nested inside this one).
    /// </summary>
    public IReadOnlyList<SourceRoute> Scan(string root, IReadOnlyCollection<string>? exclude = null) =>
        Find(SourceFiles(root, exclude));

    /// <summary>The language a folder's code is mostly in, counting source files by extension.</summary>
    public CodeLanguage LanguageOf(string root, IReadOnlyCollection<string>? exclude = null)
    {
        var counts = new Dictionary<string, int>(StringComparer.Ordinal);
        if (Directory.Exists(root))
        {
            foreach (var file in Files(root, null, exclude ?? []).Take(MaxFiles * 5))
            {
                if (languages.TryGetValue(Path.GetExtension(file), out var language))
                {
                    counts[language] = counts.GetValueOrDefault(language) + 1;
                }
            }
        }

        // a header next to the code doesn't make it C++
        var main = counts.OrderByDescending(c => c.Value).Select(c => c.Key).FirstOrDefault();
        return new CodeLanguage(main, main is not null && SupportedLanguages.Contains(main), counts);
    }

    /// <summary>The source files under a folder this reads, with their text; the same skipping rules as the scan.</summary>
    public IReadOnlyList<(string Path, string Text)> SourceFiles(string root, IReadOnlyCollection<string>? exclude = null)
    {
        var files = new List<(string, string)>();
        if (!Directory.Exists(root))
        {
            return files;
        }

        var extensions = frameworks.SelectMany(f => f.Extensions).ToHashSet(StringComparer.OrdinalIgnoreCase);
        foreach (var file in Files(root, extensions, exclude ?? []).Take(MaxFiles))
        {
            try
            {
                if (new FileInfo(file).Length <= MaxFileBytes)
                {
                    files.Add((file, File.ReadAllText(file)));
                }
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
            {
            }
        }

        return files;
    }

    /// <summary>Every route declared in the given files.</summary>
    public IReadOnlyList<SourceRoute> Find(IReadOnlyList<(string Path, string Text)> files)
    {
        var routes = new List<SourceRoute>();
        foreach (var (file, text) in files)
        {
            var extension = Path.GetExtension(file);
            var name = Path.GetFileName(file);
            foreach (var framework in frameworks.Where(f => f.Extensions.Contains(extension, StringComparer.OrdinalIgnoreCase)
                                                            && (f.FileNames is null || f.FileNames.IsMatch(name))))
            {
                routes.AddRange(framework.Find(text, file));
            }
        }

        // a handler declared elsewhere (Express, Go, Axum): look its function up by name
        var withSignatures = routes.Select(r => r.Signature is not null || r.Handler is null ? r : Lookup(r, files)).ToList();

        // the same route found twice (two patterns, or a file read under two frameworks) counts once
        return withSignatures
            .GroupBy(r => (r.Method, r.Path.ToLowerInvariant()))
            .Select(g => g.OrderByDescending(r => r.Signature is not null).First())
            .ToList();
    }

    private SourceRoute Lookup(SourceRoute route, IReadOnlyList<(string Path, string Text)> files)
    {
        var extension = Path.GetExtension(route.File);
        if (!functionDefs.TryGetValue(extension, out var template))
        {
            return route;
        }

        var name = route.Handler!.Split('.', ':').Last(s => s.Length > 0);
        Regex definition;
        try
        {
            definition = new Regex(template.Replace("{name}", Regex.Escape(name), StringComparison.Ordinal),
                RegexOptions.Multiline, RegexTimeout);
        }
        catch (ArgumentException)
        {
            return route;
        }

        // the route's own file first, then the rest of the service's files in that language
        foreach (var (path, text) in files.Where(f => f.Path == route.File)
                     .Concat(files.Where(f => f.Path != route.File && Path.GetExtension(f.Path) == extension)))
        {
            Match m;
            try
            {
                m = definition.Match(text);
            }
            catch (RegexMatchTimeoutException)
            {
                continue;
            }

            if (!m.Success)
            {
                continue;
            }

            // the definition patterns end at the '(' of the parameter list; Ruby's may have none
            var end = m.Index + m.Length;
            var open = end > 0 && text[end - 1] == '(' ? end - 1 : text.IndexOf('(', end);
            if (open < 0 || open - end > 40 || text[end..Math.Max(end, open)].Contains('\n', StringComparison.Ordinal))
            {
                continue;
            }

            var (signature, body) = SignatureAndBody(text, open);
            return route with { Signature = signature, HandlerBody = body };
        }

        return route;
    }

    /// <summary>A function's parameter list from its '(' and, when it follows, its body in braces.</summary>
    internal static (string Signature, string? Body) SignatureAndBody(string text, int open)
    {
        var signature = TypeIndex.Balanced(text, open, '(', ')');
        var close = open + signature.Length + 1;
        // the body: the next '{' within a short return type, if any
        var brace = text.IndexOf('{', Math.Min(close, text.Length));
        string? body = null;
        if (brace >= 0 && brace - close < 200 && !text[close..brace].Contains(';', StringComparison.Ordinal))
        {
            body = TypeIndex.Balanced(text, brace, '{', '}');
            if (body.Length > MaxBody)
            {
                body = body[..MaxBody];
            }
        }

        return (signature, body);
    }

    private IEnumerable<string> Files(string root, HashSet<string>? extensions, IReadOnlyCollection<string> exclude)
    {
        var extensionless = frameworks.Where(f => f.Extensions.Contains("") && f.FileNames is not null)
            .Select(f => f.FileNames!).ToList();
        var pending = new Stack<string>([root]);
        while (pending.Count > 0)
        {
            var dir = pending.Pop();
            IEnumerable<string> entries;
            try
            {
                entries = Directory.EnumerateFileSystemEntries(dir).ToList();
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
            {
                continue;
            }

            foreach (var entry in entries.Order(StringComparer.Ordinal))
            {
                var name = Path.GetFileName(entry);
                if (Directory.Exists(entry))
                {
                    if (!skipFolders.Contains(name) && !name.StartsWith('.')
                        && !exclude.Any(e => PathsEqual(e, entry)))
                    {
                        pending.Push(entry);
                    }

                    continue;
                }

                if (skipFiles?.IsMatch(name) == true)
                {
                    continue;
                }

                var extension = Path.GetExtension(entry);
                if (extensions is null
                    || (extension.Length > 0 && extensions.Contains(extension))
                    || (extension.Length == 0 && extensionless.Any(r => r.IsMatch(name))))
                {
                    yield return entry;
                }
            }
        }
    }

    private static bool PathsEqual(string a, string b) =>
        string.Equals(Path.GetFullPath(a).TrimEnd(Path.DirectorySeparatorChar),
            Path.GetFullPath(b).TrimEnd(Path.DirectorySeparatorChar),
            OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal);

    /// <summary>A path as written in code, in one form: leading slash, {name} parameters.</summary>
    public static string NormalizePath(string path)
    {
        var p = path.Trim();
        p = Regex.Replace(p, @"(?<=^|/):(\w+)\??", "{$1}");                // express, gin, rails: /books/:id
        p = Regex.Replace(p, @"<(?:\w+:)?(\w+)(?:\.\.)?>", "{$1}");         // flask, rocket: /books/<int:id>, <path..>
        p = Regex.Replace(p, @"\{\*{0,2}(\w+)(?::[^{}]*)?\??\}", "{$1}");  // asp.net, spring: {id:int}, {*rest}
        p = Regex.Replace(p, @"^\^|\$$", "");                               // django re_path anchors
        p = Regex.Replace(p, @"\(\?P<(\w+)>[^)]*\)", "{$1}");               // django named groups
        p = "/" + p.TrimStart('~').Trim('/');
        return Regex.Replace(p, "/{2,}", "/");
    }

    /// <summary>A prefix and a path joined with exactly one slash between them.</summary>
    public static string Join(string prefix, string path)
    {
        var a = prefix.Trim().Trim('/');
        var b = path.Trim().Trim('/');
        return NormalizePath(a.Length == 0 ? b : b.Length == 0 ? a : $"{a}/{b}");
    }

    private sealed record RoutePattern(
        Regex Regex,
        string? DefaultMethod,
        Regex? PathFromArgs,
        bool JoinPaths,
        bool RootedIgnoresPrefix,
        bool NoPrefix,
        bool Crud,
        Regex? MethodsAfter,
        Regex? HandlersFromArgs);

    private sealed record PrefixPattern(Regex Regex, Regex? PrefixFromArgs, bool ObjectScope);

    private sealed record Framework(
        string Name,
        IReadOnlyList<string> Extensions,
        Regex? FileNames,
        IReadOnlyList<RoutePattern> Routes,
        IReadOnlyList<PrefixPattern> Prefixes,
        Regex? Handler,
        Regex? InlineHandler)
    {
        public static Framework Parse(JsonElement f)
        {
            static Regex? Rx(JsonElement e, string property, RegexOptions options = RegexOptions.None) =>
                e.TryGetProperty(property, out var v) && v.GetString() is { } s
                    ? new Regex(s, options, RegexTimeout)
                    : null;
            static bool Flag(JsonElement e, string property) =>
                e.TryGetProperty(property, out var v) && v.ValueKind == JsonValueKind.True;

            var routes = f.GetProperty("routes").EnumerateArray().Select(r => new RoutePattern(
                Rx(r, "regex", RegexOptions.Multiline)!,
                r.TryGetProperty("defaultMethod", out var dm) ? dm.GetString() : null,
                Rx(r, "pathFromArgs"),
                Flag(r, "joinPaths"),
                Flag(r, "rootedIgnoresPrefix"),
                Flag(r, "noPrefix"),
                Flag(r, "crud"),
                Rx(r, "methodsAfter"),
                Rx(r, "handlersFromArgs", RegexOptions.IgnoreCase))).ToList();
            var prefixes = f.TryGetProperty("prefixes", out var ps)
                ? ps.EnumerateArray().Select(p => new PrefixPattern(
                    Rx(p, "regex", RegexOptions.Multiline)!, Rx(p, "prefixFromArgs"),
                    p.TryGetProperty("scope", out var scope) && scope.GetString() == "object")).ToList()
                : [];
            return new Framework(
                f.GetProperty("name").GetString()!,
                f.GetProperty("extensions").EnumerateArray().Select(x => x.GetString()!).ToList(),
                Rx(f, "files"),
                routes, prefixes, Rx(f, "handler"), Rx(f, "inlineHandler"));
        }

        public IEnumerable<SourceRoute> Find(string text, string file)
        {
            var following = new List<(int Index, string Prefix)>();
            var byObject = new Dictionary<string, string>();
            foreach (var pattern in Prefixes)
            {
                foreach (Match m in SafeMatches(pattern.Regex, text))
                {
                    var prefix = m.Groups["prefix"].Success
                        ? m.Groups["prefix"].Value
                        : pattern.PrefixFromArgs?.Match(m.Groups["args"].Value) is { Success: true } pm
                            ? pm.Groups["prefix"].Value
                            : null;
                    if (prefix is null)
                    {
                        continue;
                    }

                    if (m.Groups["controller"].Success)
                    {
                        var controller = Regex.Replace(m.Groups["controller"].Value, "Controller$", "");
                        prefix = prefix.Replace("[controller]", controller, StringComparison.OrdinalIgnoreCase);
                    }

                    if (pattern.ObjectScope && m.Groups["object"].Success)
                    {
                        byObject[m.Groups["object"].Value] = prefix;
                    }
                    else
                    {
                        following.Add((m.Index, prefix));
                    }
                }
            }

            following.Sort((a, b) => a.Index.CompareTo(b.Index));
            var found = new List<SourceRoute>();
            foreach (var pattern in Routes)
            {
                foreach (Match m in SafeMatches(pattern.Regex, text))
                {
                    found.AddRange(RoutesOf(pattern, m, text, file, following, byObject));
                }
            }

            return found;
        }

        private IEnumerable<SourceRoute> RoutesOf(
            RoutePattern pattern, Match m, string text, string file,
            List<(int Index, string Prefix)> following, Dictionary<string, string> byObject)
        {
            var args = m.Groups["args"].Success ? m.Groups["args"].Value : "";
            var path = m.Groups["path"].Success
                ? m.Groups["path"].Value
                : pattern.PathFromArgs is { } pa
                    ? pattern.JoinPaths
                        ? string.Join('/', pa.Matches(args).Select(x => x.Groups["path"].Value))
                        : pa.Match(args) is { Success: true } first ? first.Groups["path"].Value : ""
                    : "";
            var after = m.Index + m.Length;
            var tail = text.Substring(after, Math.Min(800, text.Length - after));

            // the handler, and its parameter list right after it when it is declared there
            var h = Handler?.Match(tail);
            var handler = m.Groups["handler"].Success ? m.Groups["handler"].Value
                : h is { Success: true } ? h.Groups["name"].Value : null;
            string? signature = null, body = null;
            if (h is { Success: true } && !m.Groups["handler"].Success)
            {
                var nameEnd = after + h.Groups["name"].Index + h.Groups["name"].Length;
                var open = text.IndexOf('(', nameEnd);
                if (open >= 0 && open - nameEnd < 80
                    && text[nameEnd..open].All(c => char.IsWhiteSpace(c) || c is '<' or '>' or ',' or ':' || char.IsLetterOrDigit(c)))
                {
                    (signature, body) = SignatureAndBody(text, open);
                }
            }

            // an inline handler (a lambda, func(...) {...}): its parameters and body are right here
            if (signature is null && handler is null && InlineHandler?.Match(tail) is { Success: true } inline)
            {
                (signature, body) = SignatureAndBody(text, after + inline.Index + inline.Length);
            }

            path = path.Replace("[action]", handler ?? "", StringComparison.OrdinalIgnoreCase);
            var prefix = "";
            var rooted = path.StartsWith('/') || path.StartsWith("~/", StringComparison.Ordinal);
            if (!pattern.NoPrefix && !(pattern.RootedIgnoresPrefix && rooted))
            {
                if (m.Groups["object"].Success && byObject.TryGetValue(m.Groups["object"].Value, out var p))
                {
                    prefix = p;
                }
                else if (following.LastOrDefault(f => f.Index < m.Index) is { Prefix: { } fp })
                {
                    prefix = fp;
                }
            }

            var full = Join(prefix, path);
            var line = 1 + text.AsSpan(0, m.Index).Count('\n');

            if (pattern.Crud)
            {
                // a REST resource: list, create, read, replace, update, delete
                var item = Join(full, "{id}");
                foreach (var (method, route) in new[]
                         {
                             ("GET", full), ("POST", full), ("GET", item), ("PUT", item), ("PATCH", item), ("DELETE", item),
                         })
                {
                    yield return new SourceRoute(method, route, null, file, line, Name);
                }

                yield break;
            }

            if (pattern.HandlersFromArgs is { } each)
            {
                foreach (Match hm in each.Matches(args))
                {
                    var method = hm.Groups["method"].Value.ToUpperInvariant();
                    if (Verbs.Contains(method))
                    {
                        yield return new SourceRoute(method, full, hm.Groups["name"].Value, file, line, Name);
                    }
                }

                yield break;
            }

            var methods = m.Groups["method"].Success
                ? [m.Groups["method"].Value.ToUpperInvariant()]
                : Verbs.Where(v => Regex.IsMatch(args, $@"\b{v}\b", RegexOptions.IgnoreCase)).ToList();
            if (methods.Count == 0 && pattern.MethodsAfter?.Match(tail) is { Success: true } ma)
            {
                methods = Verbs.Where(v => Regex.IsMatch(ma.Groups["args"].Value, $@"\b{v}\b", RegexOptions.IgnoreCase)).ToList();
            }

            if (methods.Count == 0)
            {
                methods = [pattern.DefaultMethod?.ToUpperInvariant() ?? "GET"];
            }

            foreach (var method in methods.Where(Verbs.Contains))
            {
                yield return new SourceRoute(method, full, handler, file, line, Name, signature, body);
            }
        }

        private static IEnumerable<Match> SafeMatches(Regex regex, string text)
        {
            var results = new List<Match>();
            try
            {
                for (var m = regex.Match(text); m.Success; m = m.NextMatch())
                {
                    results.Add(m);
                }
            }
            catch (RegexMatchTimeoutException)
            {
                // a pathological file: keep what was found before it
            }

            return results;
        }
    }
}
