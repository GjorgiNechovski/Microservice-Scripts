using System.Text.RegularExpressions;

namespace MicroserviceScripts.Ui.Routes;

/// <summary>A router variable declared in a file: its own prefix and the router it was made from.</summary>
/// <param name="Constant">A name standing for the prefix (settings.API_PREFIX), looked up in the code.</param>
internal sealed record Declaration(int Index, string? Prefix, string? Constant, string? Parent);

/// <summary>Where a file puts routers under a prefix (app.use('/books', books), include_router, a group block).</summary>
/// <param name="Object">The router mounted onto; null when the pattern has none (a block, Axum's nest).</param>
/// <param name="Target">What is mounted, as written: a variable, module.attribute, require('./x') or 'module.path'.</param>
/// <param name="Function">A function whose body declares the mounted routes.</param>
/// <param name="BlockStart">Where the block with the mounted routes starts; -1 when it isn't a block.</param>
internal sealed record MountSite(
    int Index,
    string? Object,
    string? Prefix,
    string? Constant,
    bool ReplacesOwnPrefix,
    string? Target,
    string? Function,
    int BlockStart,
    int BlockEnd);

/// <summary>A route as found, with the router variable it was declared on and where.</summary>
internal sealed record FoundRoute(SourceRoute Route, string? Router, int Index);

/// <summary>What one source file declares: routes, router variables and mounts.</summary>
internal sealed class FileFacts(string path, string text)
{
    public string Path { get; } = path;

    public string Text { get; } = text;

    public Dictionary<string, List<Declaration>> Objects { get; } = new(StringComparer.Ordinal);

    public List<MountSite> Mounts { get; } = [];

    public List<FoundRoute> Routes { get; } = [];

    public void Declare(string name, Declaration declaration)
    {
        if (!Objects.TryGetValue(name, out var list))
        {
            Objects[name] = list = [];
        }

        // two patterns can declare the same variable; the one that read a prefix wins
        var same = list.FindIndex(d => d.Index == declaration.Index);
        if (same >= 0)
        {
            if ((list[same].Prefix ?? list[same].Constant) is null)
            {
                list[same] = declaration;
            }

            return;
        }

        list.Add(declaration);
        list.Sort((a, b) => a.Index.CompareTo(b.Index));
    }

    /// <summary>The declaration in force at a place in the file: the last one before it, else the first.</summary>
    public Declaration? Declared(string name, int at) =>
        Objects.TryGetValue(name, out var list) ? list.LastOrDefault(d => d.Index <= at) ?? list[0] : null;

    /// <summary>The file declares, routes on or mounts onto a variable of this name.</summary>
    public bool Knows(string name) =>
        Objects.ContainsKey(name) || Routes.Any(r => r.Router == name) || Mounts.Any(m => m.Object == name);
}

/// <summary>How each language imports and exports, from Data/route-patterns.json.</summary>
internal sealed record ModuleRules(
    IReadOnlyDictionary<string, IReadOnlyList<Regex>> Imports,
    IReadOnlyDictionary<string, IReadOnlyList<Regex>> Exports,
    IReadOnlyDictionary<string, string> Styles,
    IReadOnlyDictionary<string, Regex> FirstParam,
    IReadOnlyDictionary<string, string> FunctionDefs,
    TimeSpan Timeout);

/// <summary>
/// Puts routes under the prefixes their routers are mounted at: a router variable's own prefix and its
/// parent group's, the router mounted in another file (imports are followed), a function called with a
/// group, a block of routes. It reads what the frameworks' patterns found; nothing is run.
/// </summary>
internal sealed class RouteMounts
{
    private const int MaxDepth = 8;
    private static readonly IReadOnlyList<(IReadOnlyList<string> Pieces, bool Replaces)> Unmounted = [([], false)];
    private static readonly string[] NodeExtensions = [".ts", ".js", ".mjs", ".cjs", ".tsx", ".jsx"];

    private readonly Dictionary<string, FileFacts> files;
    private readonly ModuleRules rules;
    private readonly Dictionary<string, Dictionary<string, Import>> imports = new(PathComparer);
    private readonly Dictionary<string, HashSet<string>> defined = new(StringComparer.OrdinalIgnoreCase);
    private readonly Dictionary<Key, IReadOnlyList<(IReadOnlyList<string> Pieces, bool Replaces)>> mountedAt = [];
    private List<Target>? targets;

    public RouteMounts(IEnumerable<FileFacts> facts, ModuleRules rules)
    {
        files = new Dictionary<string, FileFacts>(PathComparer);
        foreach (var f in facts)
        {
            files.TryAdd(Full(f.Path), f);
        }

        this.rules = rules;
    }

    private static StringComparer PathComparer =>
        OperatingSystem.IsWindows() ? StringComparer.OrdinalIgnoreCase : StringComparer.Ordinal;

    private readonly record struct Key(string File, string? Object, int Index);

    private sealed record Target(string File, string? Object, int Start, int End, string Site, MountSite Mount);

    private sealed record Import(string Module, string? Original);

    /// <summary>The route at each place its router is mounted (once when it isn't), with the whole path.</summary>
    public IEnumerable<SourceRoute> Place(FileFacts file, FoundRoute found)
    {
        var (root, own) = found.Router is null
            ? (new Key(Full(file.Path), null, found.Index), (IReadOnlyList<string>)[])
            : Chain(Identity(Full(file.Path), found.Router, found.Index, 0));
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var (outer, replaces) in MountedAt(root, 0))
        {
            IReadOnlyList<string> pieces = [.. outer, .. replaces ? [] : own];
            var path = RouteCatalog.Join(Joined(pieces), found.Route.Path);
            if (!seen.Add(path))
            {
                continue;
            }

            yield return pieces.Count == 0
                ? found.Route
                : found.Route with { Path = path, PathTypes = Types(pieces, found.Route.PathTypes) };
        }
    }

    private static string Joined(IEnumerable<string> pieces) =>
        pieces.Aggregate("", (path, piece) => RouteCatalog.Join(path, RouteCatalog.NormalizePath(piece)));

    private static IReadOnlyDictionary<string, string>? Types(IEnumerable<string> pieces, IReadOnlyDictionary<string, string>? own)
    {
        var types = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var piece in pieces)
        {
            foreach (var (name, type) in RouteCatalog.PathTypes(piece) ?? new Dictionary<string, string>())
            {
                types[name] = type;
            }
        }

        foreach (var (name, type) in own ?? new Dictionary<string, string>())
        {
            types[name] = type;
        }

        return types.Count > 0 ? types : null;
    }

    /// <summary>
    /// Where a variable is really declared: here, or in the file it is imported from
    /// (a Flask blueprint made in __init__.py and given routes in views.py).
    /// </summary>
    private Key Identity(string file, string name, int index, int depth)
    {
        if (depth > MaxDepth || !files.TryGetValue(file, out var facts) || facts.Objects.ContainsKey(name))
        {
            return new Key(file, name, index);
        }

        if (Imports(file).TryGetValue(name, out var import)
            && ResolveImport(file, import, null) is { Object: { } original } there
            && files.TryGetValue(there.File, out var declaring))
        {
            var at = declaring.Objects.TryGetValue(original, out var list) ? list[0].Index : 0;
            return Identity(there.File, original, at, depth + 1);
        }

        return new Key(file, name, index);
    }

    /// <summary>A router's own prefixes, outermost first, and the router at the top of its groups.</summary>
    private (Key Root, IReadOnlyList<string> Own) Chain(Key key)
    {
        var own = new List<string>();
        for (var i = 0; i <= MaxDepth && key.Object is not null; i++)
        {
            if (!files.TryGetValue(key.File, out var facts) || facts.Declared(key.Object, key.Index) is not { } d)
            {
                break;
            }

            own.Insert(0, PrefixText(key.File, d.Prefix, d.Constant));
            if (d.Parent is null)
            {
                break;
            }

            key = Identity(key.File, d.Parent, d.Index, 0);
        }

        return (key, own);
    }

    /// <summary>
    /// The prefixes a router is mounted under, outermost first; one empty list when it isn't mounted.
    /// Where blocks or functions nest, the innermost one that holds the router counts.
    /// </summary>
    private IReadOnlyList<(IReadOnlyList<string> Pieces, bool Replaces)> MountedAt(Key key, int depth)
    {
        if (depth > MaxDepth)
        {
            return Unmounted;
        }

        if (mountedAt.TryGetValue(key, out var known))
        {
            return known;
        }

        var hits = Targets()
            .Where(t => PathComparer.Equals(t.File, key.File)
                        && (t.Object is null || t.Object == key.Object)
                        && key.Index > t.Start && key.Index < t.End)
            .ToList();
        if (hits.Count == 0)
        {
            return mountedAt[key] = Unmounted;
        }

        var innermost = hits.Min(h => (long)h.End - h.Start);
        var result = new List<(IReadOnlyList<string>, bool)>();
        foreach (var hit in hits.Where(h => (long)h.End - h.Start == innermost))
        {
            var mount = hit.Mount;
            var (mountRoot, mountOwn) = mount.Object is null
                ? (new Key(hit.Site, null, mount.Index), (IReadOnlyList<string>)[])
                : Chain(Identity(hit.Site, mount.Object, mount.Index, 0));
            var here = PrefixText(hit.Site, mount.Prefix, mount.Constant);
            foreach (var (outer, replaces) in MountedAt(mountRoot, depth + 1))
            {
                result.Add(([.. outer, .. replaces ? [] : mountOwn, here], mount.ReplacesOwnPrefix));
            }
        }

        return mountedAt[key] = result;
    }

    /// <summary>What every mount puts under its prefix, resolved to a file, a router and a range in it.</summary>
    private List<Target> Targets()
    {
        if (targets is not null)
        {
            return targets;
        }

        targets = [];
        foreach (var (site, facts) in files)
        {
            foreach (var mount in facts.Mounts)
            {
                if (mount.BlockStart >= 0)
                {
                    targets.Add(new Target(site, null, mount.BlockStart, mount.BlockEnd, site, mount));
                }
                else if (mount.Function is not null)
                {
                    if (FunctionTarget(site, mount.Function) is { } f)
                    {
                        targets.Add(new Target(f.File, f.Param, f.Start, f.End, site, mount));
                    }
                }
                else if (mount.Target is not null && RouterTarget(site, mount.Target) is { } r)
                {
                    targets.Add(new Target(r.File, r.Object, -1, int.MaxValue, site, mount));
                }
            }
        }

        return targets;
    }

    /// <summary>
    /// The file and router variable a mount's target names. A router in another file that can't be
    /// pinned to one variable stands for all the routes in that file.
    /// </summary>
    private (string File, string? Object)? RouterTarget(string site, string target)
    {
        var t = target.Trim();
        string? file;
        string? name;
        if (Regex.Match(t, @"^(?:require|import)\(\s*['""]([^'""]+)['""]\s*\)$") is { Success: true } call)
        {
            file = ResolveModule(site, call.Groups[1].Value);
            name = file is null ? null : Export(file);
        }
        else if (Regex.Match(t, @"^['""]([\w.]+)['""]$") is { Success: true } quoted)
        {
            // Django's include('books.urls')
            file = ResolveModule(site, quoted.Groups[1].Value);
            name = null;
        }
        else if (Regex.IsMatch(t, @"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$"))
        {
            var parts = t.Split('.');
            var head = parts[0];
            var attribute = parts.Length > 1 ? parts[^1] : null;
            if (Imports(site).TryGetValue(head, out var import))
            {
                var resolved = ResolveImport(site, import, attribute);
                file = resolved?.File;
                name = resolved?.Object;
            }
            else
            {
                // a router of this file; router.urls is the router itself
                file = site;
                name = attribute is null || files[site].Knows(head) ? head : attribute;
            }
        }
        else
        {
            return null;
        }

        if (file is null || !files.ContainsKey(file))
        {
            return null;
        }

        if (name is not null)
        {
            (file, name, _) = Identity(file, name, 0, 0);
        }

        if (name is not null && !files[file].Knows(name))
        {
            // a module whose router has another name; never a guess in the file doing the mounting
            if (PathComparer.Equals(file, site))
            {
                return null;
            }

            name = null;
        }

        return (file, name);
    }

    /// <summary>The file an import names, and the variable in it (null: the module itself or its default export).</summary>
    private (string File, string? Object)? ResolveImport(string site, Import import, string? attribute)
    {
        if (import.Original is null)
        {
            // import x / const x = require('./x') / import * as x
            var module = ResolveModule(site, import.Module);
            return module is null ? null : (module, attribute ?? Export(module));
        }

        var submodule = ResolveModule(site, SubModule(site, import.Module, import.Original));
        var file = ResolveModule(site, import.Module);
        if (attribute is not null)
        {
            // from routers import books ... books.router
            return submodule is not null ? (submodule, attribute) : file is not null ? (file, attribute) : null;
        }

        if (file is not null && (files[file].Knows(import.Original) || Imports(file).ContainsKey(import.Original)))
        {
            return (file, import.Original);
        }

        return submodule is not null ? (submodule, Export(submodule))
            : file is not null ? (file, import.Original)
            : null;
    }

    /// <summary>A module inside another: books in "app.routers" (Python) or in "./routes" (Node).</summary>
    private string SubModule(string site, string module, string name) =>
        rules.Styles.GetValueOrDefault(Path.GetExtension(site)) == "python"
            ? module.Length == 0 || module.EndsWith('.') ? module + name : module + "." + name
            : module.TrimEnd('/') + "/" + name;

    /// <summary>The source file a module name means, from the file importing it; null when it isn't in the service.</summary>
    private string? ResolveModule(string site, string module)
    {
        var style = rules.Styles.GetValueOrDefault(Path.GetExtension(site));
        return style switch
        {
            "python" => PythonModule(site, module),
            "node" => NodeModule(site, module),
            _ => null,
        };
    }

    private string? PythonModule(string site, string module)
    {
        var dots = module.TakeWhile(c => c == '.').Count();
        var parts = module[dots..].Split('.', StringSplitOptions.RemoveEmptyEntries);
        if (dots > 0)
        {
            var dir = Path.GetDirectoryName(site);
            for (var i = 1; i < dots && dir is not null; i++)
            {
                dir = Path.GetDirectoryName(dir);
            }

            if (dir is null)
            {
                return null;
            }

            var under = Path.Combine([dir, .. parts]);
            return parts.Length == 0
                ? Existing(Path.Combine(dir, "__init__.py"))
                : Existing(under + ".py") ?? Existing(Path.Combine(under, "__init__.py"));
        }

        if (parts.Length == 0)
        {
            return null;
        }

        // an absolute module: the file whose path ends that way, nearest the importing file
        var sep = Path.DirectorySeparatorChar;
        var tail = sep + string.Join(sep, parts);
        var comparison = OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal;
        return files.Keys
            .Where(f => f.EndsWith(tail + ".py", comparison) || f.EndsWith(tail + sep + "__init__.py", comparison))
            .OrderByDescending(f => CommonPrefix(f, site))
            .ThenBy(f => f.Length)
            .FirstOrDefault();
    }

    private string? NodeModule(string site, string module)
    {
        if (!module.StartsWith('.'))
        {
            return null;    // a package, not the service's own code
        }

        var full = Path.GetFullPath(Path.Combine(Path.GetDirectoryName(site) ?? "", module));
        var stems = new List<string> { full };
        if (Path.GetExtension(full) is ".js" or ".mjs" or ".cjs" or ".jsx")
        {
            stems.Add(full[..^Path.GetExtension(full).Length]);    // TypeScript's './books.js' is books.ts
        }

        foreach (var stem in stems)
        {
            if (Existing(stem) is { } exact)
            {
                return exact;
            }

            foreach (var candidate in NodeExtensions.Select(e => stem + e)
                         .Concat(NodeExtensions.Select(e => Path.Combine(stem, "index" + e))))
            {
                if (Existing(candidate) is { } found)
                {
                    return found;
                }
            }
        }

        return null;
    }

    private string? Existing(string path) => files.ContainsKey(Full(path)) ? Full(path) : null;

    private static int CommonPrefix(string a, string b)
    {
        var n = 0;
        while (n < a.Length && n < b.Length && a[n] == b[n])
        {
            n++;
        }

        return n;
    }

    /// <summary>The names a file imports, each with the module and the name it has there.</summary>
    private Dictionary<string, Import> Imports(string file)
    {
        if (imports.TryGetValue(file, out var known))
        {
            return known;
        }

        var found = new Dictionary<string, Import>(StringComparer.Ordinal);
        if (files.TryGetValue(file, out var facts)
            && rules.Imports.TryGetValue(Path.GetExtension(file), out var patterns))
        {
            foreach (var pattern in patterns)
            {
                foreach (var m in Matches(pattern, facts.Text))
                {
                    var module = m.Groups["module"].Value;
                    if (m.Groups["name"].Success)
                    {
                        found[m.Groups["name"].Value] = new Import(module, null);
                    }

                    if (m.Groups["names"].Success)
                    {
                        foreach (var (local, original) in Names(m.Groups["names"].Value))
                        {
                            found[local] = new Import(module, original);
                        }
                    }
                }
            }
        }

        return imports[file] = found;
    }

    /// <summary>"a, b as c" / "{ a, b: c }" / "(a,\n b)": each local name with the name it is imported by.</summary>
    private static IEnumerable<(string Local, string Original)> Names(string list)
    {
        foreach (var item in list.Trim().Trim('(', ')', '{', '}').Split([',', '\n'], StringSplitOptions.RemoveEmptyEntries))
        {
            var m = Regex.Match(item.Trim(), @"^(?:type\s+)?(?<original>[A-Za-z_$][\w$]*)(?:\s*(?:\bas\b|:)\s*(?<local>[A-Za-z_$][\w$]*))?$");
            if (m.Success)
            {
                var original = m.Groups["original"].Value;
                yield return (m.Groups["local"].Success ? m.Groups["local"].Value : original, original);
            }
        }
    }

    /// <summary>The variable a file exports by default (module.exports = router), when it says.</summary>
    private string? Export(string file) =>
        files.TryGetValue(file, out var facts) && rules.Exports.TryGetValue(Path.GetExtension(file), out var patterns)
            ? patterns.SelectMany(p => Matches(p, facts.Text)).Select(m => m.Groups["object"].Value).LastOrDefault()
            : null;

    /// <summary>
    /// The function a function mount names: its body, and its first parameter when it has one
    /// (the group the routes inside are declared on).
    /// </summary>
    private (string File, string? Param, int Start, int End)? FunctionTarget(string site, string name)
    {
        var extension = Path.GetExtension(site);
        if (!rules.FunctionDefs.TryGetValue(extension, out var template) || !Defined(extension).Contains(name))
        {
            return null;
        }

        Regex definition;
        try
        {
            definition = new Regex(template.Replace("{name}", Regex.Escape(name), StringComparison.Ordinal),
                RegexOptions.Multiline, rules.Timeout);
        }
        catch (ArgumentException)
        {
            return null;
        }

        var sameLanguage = files.Where(f => string.Equals(Path.GetExtension(f.Key), extension, StringComparison.OrdinalIgnoreCase));
        foreach (var (file, facts) in sameLanguage.OrderByDescending(f => PathComparer.Equals(f.Key, site)))
        {
            var text = facts.Text;
            foreach (var m in Matches(definition, text))
            {
                // group.MapBooks() is a call, not the definition
                var before = m.Index - 1;
                while (before >= 0 && char.IsWhiteSpace(text[before]))
                {
                    before--;
                }

                if (before >= 0 && text[before] == '.')
                {
                    continue;
                }

                var end = m.Index + m.Length;
                var open = end > 0 && text[end - 1] == '(' ? end - 1 : text.IndexOf('(', end);
                if (open < 0 || open - end > 40)
                {
                    continue;
                }

                var (signature, brace, close) = RouteCatalog.FunctionBounds(text, open);
                if (brace >= 0)
                {
                    return (file, FirstParam(extension, signature), brace, close);
                }
            }
        }

        return null;
    }

    private string? FirstParam(string extension, string signature)
    {
        var first = TypeIndex.SplitTop(signature, ',').FirstOrDefault()?.Trim();
        if (string.IsNullOrEmpty(first) || !rules.FirstParam.TryGetValue(extension, out var pattern))
        {
            return null;
        }

        return pattern.Match(first) is { Success: true } m ? m.Groups["name"].Value : null;
    }

    /// <summary>The names functions are defined under in a language's files (some patterns also catch calls).</summary>
    private HashSet<string> Defined(string extension)
    {
        if (defined.TryGetValue(extension, out var known))
        {
            return known;
        }

        var names = new HashSet<string>(StringComparer.Ordinal);
        if (rules.FunctionDefs.TryGetValue(extension, out var template))
        {
            var any = new Regex(template.Replace("{name}", @"(?<name>[A-Za-z_]\w*)", StringComparison.Ordinal),
                RegexOptions.Multiline, rules.Timeout);
            foreach (var (file, facts) in files)
            {
                if (string.Equals(Path.GetExtension(file), extension, StringComparison.OrdinalIgnoreCase))
                {
                    names.UnionWith(Matches(any, facts.Text).Select(m => m.Groups["name"].Value));
                }
            }
        }

        return defined[extension] = names;
    }

    /// <summary>A prefix as written, or the string a constant holds somewhere in the service's code.</summary>
    private string PrefixText(string site, string? prefix, string? constant)
    {
        if (prefix is not null || constant is null)
        {
            return prefix ?? "";
        }

        var name = constant.Split('.')[^1];
        var assignment = new Regex(
            $@"\b{Regex.Escape(name)}\s*(?::[^=\n]+)?=\s*[rbuf]?([""'`])(?<value>[^""'`\n]*)\1", RegexOptions.None, rules.Timeout);
        var extension = Path.GetExtension(site);
        foreach (var (_, facts) in files
                     .Where(f => string.Equals(Path.GetExtension(f.Key), extension, StringComparison.OrdinalIgnoreCase))
                     .OrderByDescending(f => PathComparer.Equals(f.Key, site)))
        {
            if (Matches(assignment, facts.Text).FirstOrDefault() is { } m)
            {
                return m.Groups["value"].Value;
            }
        }

        return "";
    }

    private static List<Match> Matches(Regex regex, string text)
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
        }

        return results;
    }

    private static string Full(string path)
    {
        try
        {
            return Path.GetFullPath(path);
        }
        catch (Exception ex) when (ex is ArgumentException or NotSupportedException or PathTooLongException)
        {
            return path;
        }
    }
}
