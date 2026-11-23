using System.Text.Json;
using System.Text.RegularExpressions;

namespace MicroserviceScripts.Ui.Routes;

/// <summary>A route declared in a service's own code.</summary>
/// <param name="Path">As the service sees it, with parameters written {name}.</param>
/// <param name="Handler">The function that handles it, when the pattern could tell.</param>
/// <param name="File">The file it is declared in.</param>
/// <param name="Signature">The handler's parameter list as written, when it could be found.</param>
/// <param name="HandlerBody">The handler's body, when found (Go and Rust read the request inside it).</param>
/// <param name="PathTypes">Types the path itself gives its parameters (Flask's &lt;int:id&gt;, ASP.NET's {id:int}).</param>
public sealed record SourceRoute(
    string Method,
    string Path,
    string? Handler,
    string File,
    int Line,
    string Framework,
    string? Signature = null,
    string? HandlerBody = null,
    IReadOnlyDictionary<string, string>? PathTypes = null);

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

    /// <summary>How many source files under one build folder are searched for routes, at most.</summary>
    public int MaxSourceFiles => MaxFiles;
    private const long MaxFileBytes = 1_000_000;
    private const int MaxBody = 20_000;
    private static readonly string[] Verbs = ["GET", "POST", "PUT", "DELETE", "PATCH"];
    private static readonly TimeSpan RegexTimeout = TimeSpan.FromSeconds(2);

    private readonly IReadOnlyList<Framework> frameworks;
    private readonly HashSet<string> skipFolders;
    private readonly Regex? skipFiles;
    private readonly IReadOnlyDictionary<string, string> languages;
    private readonly IReadOnlyDictionary<string, string> functionDefs;
    private readonly ModuleRules modules;

    private RouteCatalog(
        IReadOnlyList<Framework> frameworks,
        IEnumerable<string> skipFolders,
        string? skipFiles,
        IReadOnlyDictionary<string, string> languages,
        IReadOnlyDictionary<string, string> functionDefs,
        ModuleRules modules)
    {
        this.frameworks = frameworks;
        this.skipFolders = new HashSet<string>(skipFolders, StringComparer.OrdinalIgnoreCase);
        this.skipFiles = skipFiles is null ? null : new Regex(skipFiles, RegexOptions.None, RegexTimeout);
        this.languages = languages;
        this.functionDefs = functionDefs;
        this.modules = modules;
    }

    public IEnumerable<string> FrameworkNames => frameworks.Select(f => f.Name);

    /// <summary>The project manifests (by file name) that say what a service is built with.</summary>
    public Regex? GatewayManifests { get; private init; }

    /// <summary>Reverse-proxy libraries: a service whose manifest names one is a gateway.</summary>
    public IReadOnlyList<string> GatewayLibraries { get; private init; } = [];

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
                ? m.EnumerateObject()
                    .SelectMany(p => p.Name.Split(' ', StringSplitOptions.RemoveEmptyEntries).Select(ext => (ext, p.Value)))
                    .ToDictionary(x => x.ext, x => x.Value.GetString()!, StringComparer.OrdinalIgnoreCase)
                : new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        static Dictionary<string, IReadOnlyList<Regex>> Lists(JsonElement root, string name) =>
            root.TryGetProperty(name, out var m)
                ? m.EnumerateObject()
                    .SelectMany(p => p.Name.Split(' ', StringSplitOptions.RemoveEmptyEntries).Select(ext => (ext, p.Value)))
                    .ToDictionary(
                        x => x.ext,
                        x => (IReadOnlyList<Regex>)x.Value.EnumerateArray()
                            .Select(r => new Regex(r.GetString()!, RegexOptions.Multiline, RegexTimeout)).ToList(),
                        StringComparer.OrdinalIgnoreCase)
                : new Dictionary<string, IReadOnlyList<Regex>>(StringComparer.OrdinalIgnoreCase);

        var functionDefs = Map(root, "functionDefs");
        var modules = new ModuleRules(
            Lists(root, "imports"),
            Lists(root, "exports"),
            Map(root, "modules"),
            Map(root, "firstParam").ToDictionary(
                p => p.Key, p => new Regex(p.Value, RegexOptions.Singleline, RegexTimeout), StringComparer.OrdinalIgnoreCase),
            functionDefs,
            RegexTimeout);
        var gateways = root.TryGetProperty("gateways", out var g) ? g : default;
        return new RouteCatalog(frameworks, skipFolders, skipFiles, Map(root, "languages"), functionDefs, modules)
        {
            GatewayManifests = gateways.ValueKind == JsonValueKind.Object && gateways.TryGetProperty("manifests", out var m)
                ? new Regex(m.GetString()!, RegexOptions.None, RegexTimeout)
                : null,
            GatewayLibraries = gateways.ValueKind == JsonValueKind.Object && gateways.TryGetProperty("libraries", out var l)
                ? l.EnumerateArray().Select(x => x.GetString()!).ToList()
                : [],
        };
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
    public IReadOnlyList<(string Path, string Text)> SourceFiles(string root, IReadOnlyCollection<string>? exclude = null) =>
        SourceFiles(root, exclude, out _);

    /// <param name="cut">More files were there than are read (<see cref="MaxSourceFiles"/>): the rest weren't searched.</param>
    public IReadOnlyList<(string Path, string Text)> SourceFiles(
        string root, IReadOnlyCollection<string>? exclude, out bool cut)
    {
        var files = new List<(string, string)>();
        cut = false;
        if (!Directory.Exists(root))
        {
            return files;
        }

        var extensions = frameworks.SelectMany(f => f.Extensions).ToHashSet(StringComparer.OrdinalIgnoreCase);
        var seen = 0;
        foreach (var file in Files(root, extensions, exclude ?? []).Take(MaxFiles + 1))
        {
            if (++seen > MaxFiles)
            {
                cut = true;
                break;
            }

            try
            {
                if (new FileInfo(file).Length <= MaxFileBytes)
                {
                    // one kind of line end: a pattern's $ matches before \n only, and a checkout
                    // on Windows (git's autocrlf) ends every line with \r\n
                    files.Add((file, File.ReadAllText(file).ReplaceLineEndings("\n")));
                }
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
            {
            }
        }

        return files;
    }

    /// <summary>Every route declared in the given files, under the prefixes its router is mounted at.</summary>
    public IReadOnlyList<SourceRoute> Find(IReadOnlyList<(string Path, string Text)> files)
    {
        var facts = new List<FileFacts>();
        foreach (var (file, text) in files)
        {
            var extension = Path.GetExtension(file);
            var name = Path.GetFileName(file);
            var fileFacts = new FileFacts(file, text);
            foreach (var framework in frameworks.Where(f => f.Extensions.Contains(extension, StringComparer.OrdinalIgnoreCase)
                                                            && (f.FileNames is null || f.FileNames.IsMatch(name))))
            {
                framework.Find(text, file, fileFacts);
            }

            facts.Add(fileFacts);
        }

        // routers mounted under a prefix, in this file or another one
        var mounts = new RouteMounts(facts, modules);
        var routes = facts.SelectMany(f => f.Routes.SelectMany(r => mounts.Place(f, r))).ToList();

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
        var (signature, brace, end) = FunctionBounds(text, open);
        if (brace < 0)
        {
            return (signature, null);
        }

        var body = text[(brace + 1)..Math.Min(end, text.Length)];
        return (signature, body.Length > MaxBody ? body[..MaxBody] : body);
    }

    /// <summary>
    /// A function's parameter list from its '(', and where its body's braces are when one follows
    /// (-1 when none does: a call, or a declaration without a body).
    /// </summary>
    internal static (string Signature, int Brace, int End) FunctionBounds(string text, int open)
    {
        var signature = TypeIndex.Balanced(text, open, '(', ')');
        var close = Math.Min(open + signature.Length + 1, text.Length);
        // the body: the next '{' within a short return type, if any
        var brace = text.IndexOf('{', close);
        if (brace >= 0 && brace - close < 200 && !text[close..brace].Contains(';', StringComparison.Ordinal))
        {
            return (signature, brace, brace + TypeIndex.Balanced(text, brace, '{', '}').Length + 1);
        }

        return (signature, -1, -1);
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
                    // a symlinked folder can loop back to an ancestor or lead out of the code
                    if (!skipFolders.Contains(name) && !name.StartsWith('.')
                        && !File.GetAttributes(entry).HasFlag(FileAttributes.ReparsePoint)
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

    /// <summary>
    /// The types a declared path gives its parameters, before <see cref="NormalizePath"/> drops them:
    /// Flask/Werkzeug converters (&lt;int:id&gt;, &lt;uuid:key&gt;) and ASP.NET constraints ({id:int}, {id:min(1)}).
    /// </summary>
    public static IReadOnlyDictionary<string, string>? PathTypes(string path)
    {
        var types = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (Match m in Regex.Matches(path, @"<(\w+):(\w+)>"))
        {
            types[m.Groups[2].Value] = m.Groups[1].Value;
        }

        foreach (Match m in Regex.Matches(path, @"\{\*{0,2}(\w+):(\w+)"))
        {
            // number ranges are numbers; the length and pattern constraints are text
            types[m.Groups[1].Value] = m.Groups[2].Value.ToLowerInvariant() switch
            {
                "min" or "max" or "range" => "int",
                "minlength" or "maxlength" or "length" or "alpha" or "regex" or "required" => "string",
                var t => t,
            };
        }

        return types.Count > 0 ? types : null;
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

    private sealed record MountPattern(Regex Regex, Regex? PrefixFromArgs, bool ReplacesOwnPrefix, bool TargetIsLastArg, string? Block);

    private sealed record Framework(
        string Name,
        IReadOnlyList<string> Extensions,
        Regex? FileNames,
        IReadOnlyList<RoutePattern> Routes,
        IReadOnlyList<PrefixPattern> Prefixes,
        IReadOnlyList<MountPattern> Mounts,
        Regex? RouteObjects,
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
            var mounts = f.TryGetProperty("mounts", out var ms)
                ? ms.EnumerateArray().Select(p => new MountPattern(
                    Rx(p, "regex", RegexOptions.Multiline)!, Rx(p, "prefixFromArgs"),
                    Flag(p, "replacesOwnPrefix"), Flag(p, "targetIsLastArg"),
                    p.TryGetProperty("block", out var block) ? block.GetString() : null)).ToList()
                : [];
            return new Framework(
                f.GetProperty("name").GetString()!,
                f.GetProperty("extensions").EnumerateArray().Select(x => x.GetString()!).ToList(),
                Rx(f, "files"),
                routes, prefixes, mounts, Rx(f, "routeObjects"), Rx(f, "handler"), Rx(f, "inlineHandler"));
        }

        /// <summary>
        /// The routes, router variables and mounts this framework declares in a file, added to its facts.
        /// A route keeps its path as declared (with a controller's prefix); the prefixes of its router
        /// and of where that router is mounted are added once every file is read.
        /// </summary>
        public void Find(string text, string file, FileFacts facts)
        {
            var following = new List<(int Index, string Prefix)>();
            foreach (var pattern in Prefixes)
            {
                foreach (Match m in SafeMatches(pattern.Regex, text))
                {
                    var (prefix, constant) = PrefixOf(m, pattern.PrefixFromArgs);
                    if (pattern.ObjectScope && m.Groups["object"].Success)
                    {
                        // a router variable, with or without a prefix of its own
                        facts.Declare(m.Groups["object"].Value, new Declaration(
                            m.Index, prefix, constant, m.Groups["parent"].Success ? m.Groups["parent"].Value : null));
                        continue;
                    }

                    if (prefix is null)
                    {
                        continue;
                    }

                    if (m.Groups["controller"].Success)
                    {
                        var controller = Regex.Replace(m.Groups["controller"].Value, "Controller$", "");
                        prefix = prefix.Replace("[controller]", controller, StringComparison.OrdinalIgnoreCase);
                    }

                    following.Add((m.Index, prefix));
                }
            }

            foreach (var pattern in Mounts)
            {
                foreach (Match m in SafeMatches(pattern.Regex, text))
                {
                    if (MountOf(pattern, m, text) is { } mount)
                    {
                        facts.Mounts.Add(mount);
                    }
                }
            }

            following.Sort((a, b) => a.Index.CompareTo(b.Index));
            foreach (var pattern in Routes)
            {
                foreach (Match m in SafeMatches(pattern.Regex, text))
                {
                    facts.Routes.AddRange(RoutesOf(pattern, m, text, file, following, facts));
                }
            }
        }

        private static (string? Prefix, string? Constant) PrefixOf(Match m, Regex? fromArgs)
        {
            if (m.Groups["prefix"].Success)
            {
                return (m.Groups["prefix"].Value, null);
            }

            if (fromArgs?.Match(m.Groups["args"].Value) is { Success: true } a)
            {
                return (a.Groups["prefix"].Success ? a.Groups["prefix"].Value : null,
                    a.Groups["constant"].Success ? a.Groups["constant"].Value : null);
            }

            return (null, null);
        }

        private static MountSite? MountOf(MountPattern pattern, Match m, string text)
        {
            var (prefix, constant) = PrefixOf(m, pattern.PrefixFromArgs);
            var target = m.Groups["target"].Success ? m.Groups["target"].Value
                : pattern.TargetIsLastArg ? TypeIndex.SplitTop(m.Groups["args"].Value, ',').LastOrDefault(a => a.Trim().Length > 0)?.Trim()
                : null;
            var function = m.Groups["function"].Success ? m.Groups["function"].Value : null;
            var (start, end) = pattern.Block switch
            {
                "braces" => Braces(text, m.Index + m.Length - 1),
                "do-end" => DoEnd(text, m.Index + m.Length),
                _ => (-1, -1),
            };
            if ((pattern.Block is not null && start < 0) || (pattern.Block is null && target is null && function is null))
            {
                return null;
            }

            return new MountSite(
                m.Index,
                m.Groups["object"].Success ? m.Groups["object"].Value : null,
                prefix,
                constant,
                pattern.ReplacesOwnPrefix && (prefix ?? constant) is not null,
                target,
                function,
                start,
                end);
        }

        /// <summary>The block in braces starting at or after <paramref name="from"/>.</summary>
        private static (int Start, int End) Braces(string text, int from)
        {
            var brace = text.IndexOf('{', Math.Max(0, from));
            return brace < 0 ? (-1, -1) : (brace, brace + TypeIndex.Balanced(text, brace, '{', '}').Length + 1);
        }

        private static readonly Regex DoEndWords = new(
            @"\b(?:do\b(?!:)|fn\b|end\b)|^[ \t]*(?:def|class|module|begin|case|if|unless|while|until)\b",
            RegexOptions.Multiline, RegexTimeout);

        /// <summary>A Ruby or Elixir block: from just after its 'do' to its matching 'end'.</summary>
        private static (int Start, int End) DoEnd(string text, int from)
        {
            var depth = 1;
            try
            {
                // strings and comments can hold the words too
                var masked = Regex.Replace(text, @"""(?:[^""\\\n]|\\.)*""|'(?:[^'\\\n]|\\.)*'|#[^\n]*",
                    x => new string(' ', x.Length), RegexOptions.None, RegexTimeout);
                for (var w = DoEndWords.Match(masked, from); w.Success; w = w.NextMatch())
                {
                    depth += w.Value.TrimStart() == "end" ? -1 : 1;
                    if (depth == 0)
                    {
                        return (from - 1, w.Index);
                    }
                }
            }
            catch (RegexMatchTimeoutException)
            {
            }

            return (-1, -1);
        }

        private IEnumerable<FoundRoute> RoutesOf(
            RoutePattern pattern, Match m, string text, string file,
            List<(int Index, string Prefix)> following, FileFacts facts)
        {
            var routerName = m.Groups["object"].Success ? m.Groups["object"].Value : null;
            if (routerName is not null && RouteObjects is not null
                && !RouteObjects.IsMatch(routerName) && !facts.Objects.ContainsKey(routerName))
            {
                // axios.get('/x') is a call, not a route
                yield break;
            }

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
            // a controller's prefix; a router variable's own comes later, with where it is mounted
            var prefix = "";
            var rooted = path.StartsWith('/') || path.StartsWith("~/", StringComparison.Ordinal);
            if (!pattern.NoPrefix && !(pattern.RootedIgnoresPrefix && rooted)
                && (routerName is null || !facts.Objects.ContainsKey(routerName))
                && following.LastOrDefault(f => f.Index < m.Index) is { Prefix: { } fp })
            {
                prefix = fp;
            }

            var full = Join(prefix, path);
            var pathTypes = PathTypes($"{prefix}/{path}");
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
                    yield return new FoundRoute(new SourceRoute(method, route, null, file, line, Name), routerName, m.Index);
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
                        yield return new FoundRoute(
                            new SourceRoute(method, full, hm.Groups["name"].Value, file, line, Name), routerName, m.Index);
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
                yield return new FoundRoute(
                    new SourceRoute(method, full, handler, file, line, Name, signature, body, pathTypes), routerName, m.Index);
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
