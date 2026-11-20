using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace MicroserviceScripts.Ui.Routes;

/// <summary>A field of a type, as it appears in JSON.</summary>
/// <param name="Markers">Attributes or annotations on it (BsonId, JsonIgnore, Id, …), by name.</param>
public sealed record FieldDef(string JsonName, string Type, bool Optional, IReadOnlyList<string> Markers);

/// <summary>A class, record, model, interface or enum declared in a service's code.</summary>
public sealed record TypeDef(
    string Name, bool IsEnum, IReadOnlyList<FieldDef> Fields, JsonNode? FirstEnumValue, string File, int Line);

/// <summary>
/// The types a service declares, read from its code (Pydantic models and
/// dataclasses, C# classes and records, Java and Kotlin classes and records,
/// TypeScript classes and interfaces, and enums of each), to show what a
/// route expects. Approximate by design: what it can't read, it leaves out.
/// </summary>
public sealed class TypeIndex
{
    private static readonly TimeSpan Timeout = TimeSpan.FromSeconds(2);
    private readonly Dictionary<string, TypeDef> types = new(StringComparer.Ordinal);

    public static TypeIndex Build(IEnumerable<(string Path, string Text)> files)
    {
        var index = new TypeIndex();
        foreach (var (path, text) in files)
        {
            IEnumerable<TypeDef> found;
            try
            {
                found = Path.GetExtension(path).ToLowerInvariant() switch
                {
                    ".py" => Python(text, path),
                    ".cs" => CSharp(text, path),
                    ".java" => Java(text, path),
                    ".kt" => Kotlin(text, path),
                    ".ts" => TypeScript(text, path),
                    ".go" => Go(text, path),
                    ".rs" => Rust(text, path),
                    _ => [],
                };
                found = found.ToList();
            }
            catch (RegexMatchTimeoutException)
            {
                continue;
            }

            foreach (var t in found)
            {
                // the first declaration wins; a richer one replaces an empty one
                if (!index.types.TryGetValue(t.Name, out var known) || (known.Fields.Count == 0 && !known.IsEnum))
                {
                    index.types[t.Name] = t;
                }
            }
        }

        return index;
    }

    public TypeDef? Find(string name) => types.GetValueOrDefault(name);

    public int Count => types.Count;

    private static int LineOf(string text, int index) => 1 + text.AsSpan(0, Math.Min(index, text.Length)).Count('\n');

    // --- Python ----------------------------------------------------------------

    private static IEnumerable<TypeDef> Python(string text, string file)
    {
        var lines = text.Replace("\r", "").Split('\n');
        for (var i = 0; i < lines.Length; i++)
        {
            var m = Regex.Match(lines[i], @"^(?<indent>\s*)class\s+(?<name>\w+)\s*(?:\((?<bases>.*)\))?\s*:", RegexOptions.None, Timeout);
            if (!m.Success)
            {
                continue;
            }

            var indent = m.Groups["indent"].Length;
            var bases = m.Groups["bases"].Value;
            var isEnum = Regex.IsMatch(bases, @"\b(Enum|IntEnum|StrEnum|Flag|IntFlag)\b");
            var members = new List<string>();
            int? bodyIndent = null;
            for (var j = i + 1; j < lines.Length; j++)
            {
                var line = lines[j];
                if (line.Trim().Length == 0 || line.TrimStart().StartsWith('#'))
                {
                    continue;
                }

                var own = line.Length - line.TrimStart().Length;
                if (own <= indent)
                {
                    break;
                }

                bodyIndent ??= own;
                if (own == bodyIndent)
                {
                    members.Add(line.Trim());
                }
            }

            if (isEnum)
            {
                var first = members.Select(l => Regex.Match(l, @"^(?<name>[A-Za-z_]\w*)\s*=\s*(?<value>.+)$")).FirstOrDefault(x => x.Success);
                yield return new TypeDef(m.Groups["name"].Value, true, [],
                    first is null ? null : Literal(first.Groups["value"].Value) ?? JsonValue.Create(first.Groups["name"].Value),
                    file, i + 1);
                continue;
            }

            var fields = new List<FieldDef>();
            foreach (var line in members)
            {
                var f = Regex.Match(line, @"^(?<name>[A-Za-z_]\w*)\s*:\s*(?<rest>.+)$");
                if (!f.Success || f.Groups["name"].Value is "model_config" or "Config" || line.StartsWith("def ", StringComparison.Ordinal))
                {
                    continue;
                }

                var (type, value) = SplitFirst(f.Groups["rest"].Value, '=');
                if (type.StartsWith("ClassVar", StringComparison.Ordinal))
                {
                    continue;
                }

                var name = f.Groups["name"].Value;
                var alias = value is null ? null : Regex.Match(value, @"\balias\s*=\s*['""](?<a>[^'""]+)['""]");
                var required = value is null || Regex.IsMatch(value.Trim(), @"^Field\(\s*\.\.\.");
                var optional = !required || Regex.IsMatch(type, @"Optional\[|\|\s*None|None\s*\|");
                fields.Add(new FieldDef(alias is { Success: true } ? alias.Groups["a"].Value : name, type.Trim(), optional, []));
            }

            yield return new TypeDef(m.Groups["name"].Value, false, fields, null, file, i + 1);
        }
    }

    // --- C# -------------------------------------------------------------------

    private static IEnumerable<TypeDef> CSharp(string text, string file)
    {
        var decl = new Regex(
            @"(?:(?:public|internal|private|protected|sealed|partial|abstract|static|readonly|file|unsafe|new)\s+)*(?<kind>record\s+struct|record\s+class|record|class|struct|enum)\s+(?<name>\w+)(?:<[^>]*>)?",
            RegexOptions.None, Timeout);
        foreach (Match m in decl.Matches(text))
        {
            var pos = m.Index + m.Length;
            var kind = m.Groups["kind"].Value;
            var fields = new List<FieldDef>();
            if (kind != "enum" && Next(text, pos) == '(')
            {
                var parameters = Balanced(text, text.IndexOf('(', pos), '(', ')');
                foreach (var p in SplitTop(parameters, ','))
                {
                    var pm = Regex.Match(p, @"^(?<attrs>(?:\[[^\]]*\]\s*)*)(?<type>.+?)\s+(?<name>\w+)\s*(?:=\s*(?<def>.+))?$", RegexOptions.Singleline);
                    if (pm.Success)
                    {
                        fields.Add(CSharpField(pm.Groups["attrs"].Value, pm.Groups["type"].Value, pm.Groups["name"].Value,
                            pm.Groups["def"].Success));
                    }
                }
            }

            var open = IndexOfBody(text, pos);
            if (open < 0)
            {
                if (fields.Count > 0)
                {
                    yield return new TypeDef(m.Groups["name"].Value, false, fields, null, file, LineOf(text, m.Index));
                }

                continue;
            }

            var body = Balanced(text, open, '{', '}');
            if (kind == "enum")
            {
                var first = SplitTop(Regex.Replace(body, @"\[[^\]]*\]|//[^\n]*", ""), ',').FirstOrDefault(s => s.Trim().Length > 0);
                var value = first is null ? null : Regex.Match(first, @"=\s*(?<v>-?\d+)");
                yield return new TypeDef(m.Groups["name"].Value, true, [],
                    JsonValue.Create(value is { Success: true } ? long.Parse(value.Groups["v"].Value) : 0L), file, LineOf(text, m.Index));
                continue;
            }

            var prop = new Regex(
                @"(?<attrs>(?:\[[^\]]*\]\s*)*)public\s+(?<mods>(?:required\s+|virtual\s+|override\s+|new\s+|static\s+)*)(?<type>[\w.?<>\[\], ]+?)\s+(?<name>\w+)\s*\{\s*(?:get|init|set)[^}]*\}(?<init>\s*=)?",
                RegexOptions.None, Timeout);
            foreach (Match p in prop.Matches(body))
            {
                if (!p.Groups["mods"].Value.Contains("static", StringComparison.Ordinal))
                {
                    fields.Add(CSharpField(p.Groups["attrs"].Value, p.Groups["type"].Value, p.Groups["name"].Value,
                        p.Groups["init"].Success && !p.Groups["mods"].Value.Contains("required", StringComparison.Ordinal)));
                }
            }

            yield return new TypeDef(m.Groups["name"].Value, false, fields, null, file, LineOf(text, m.Index));
        }
    }

    private static FieldDef CSharpField(string attrs, string type, string name, bool hasDefault)
    {
        var markers = Regex.Matches(attrs, @"\[\s*(?:\w+:\s*)?(?<a>\w+)").Select(a => Regex.Replace(a.Groups["a"].Value, "Attribute$", "")).ToList();
        var renamed = Regex.Match(attrs, @"Json(?:PropertyName|Property)\s*\(\s*(?:PropertyName\s*=\s*)?""(?<n>[^""]+)""");
        // ASP.NET Core writes and reads JSON in camelCase unless told otherwise
        var json = renamed.Success ? renamed.Groups["n"].Value : char.ToLowerInvariant(name[0]) + name[1..];
        var t = type.Trim();
        if (Regex.IsMatch(attrs, @"BsonRepresentation\s*\(\s*BsonType\.ObjectId"))
        {
            t = "ObjectId";
        }

        return new FieldDef(json, t, hasDefault || type.TrimEnd().EndsWith('?'), markers);
    }

    // --- Java -----------------------------------------------------------------

    private static IEnumerable<TypeDef> Java(string text, string file)
    {
        var decl = new Regex(
            @"(?:(?:public|private|protected|static|final|abstract|sealed|non-sealed)\s+)*(?<kind>class|record|enum|interface)\s+(?<name>\w+)(?:<[^>]*>)?",
            RegexOptions.None, Timeout);
        foreach (Match m in decl.Matches(text))
        {
            var pos = m.Index + m.Length;
            var fields = new List<FieldDef>();
            if (m.Groups["kind"].Value == "record" && Next(text, pos) == '(')
            {
                foreach (var p in SplitTop(Balanced(text, text.IndexOf('(', pos), '(', ')'), ','))
                {
                    var pm = Regex.Match(p.Trim(), @"^(?<annots>(?:@[\w.]+(?:\([^)]*\))?\s*)*)(?<type>.+?)\s+(?<name>\w+)$", RegexOptions.Singleline);
                    if (pm.Success)
                    {
                        fields.Add(JavaField(pm.Groups["annots"].Value, pm.Groups["type"].Value, pm.Groups["name"].Value));
                    }
                }
            }

            var open = IndexOfBody(text, pos);
            if (open < 0)
            {
                continue;
            }

            var body = Balanced(text, open, '{', '}');
            if (m.Groups["kind"].Value == "enum")
            {
                var first = Regex.Match(body, @"^\s*(?:@\w+(?:\([^)]*\))?\s*)*(?<c>[A-Z_][A-Z0-9_]*)\b");
                yield return new TypeDef(m.Groups["name"].Value, true, [],
                    first.Success ? JsonValue.Create(first.Groups["c"].Value) : null, file, LineOf(text, m.Index));
                continue;
            }

            var field = new Regex(
                @"(?<annots>(?:@[\w.]+(?:\([^)]*\))?\s*)*)(?:private|protected|public)\s+(?<mods>(?:static\s+|final\s+|transient\s+|volatile\s+)*)(?<type>[\w.<>\[\], ?]+?)\s+(?<name>\w+)\s*(?:=[^;]*)?;",
                RegexOptions.None, Timeout);
            foreach (Match f in field.Matches(body))
            {
                if (!f.Groups["mods"].Value.Contains("static", StringComparison.Ordinal))
                {
                    fields.Add(JavaField(f.Groups["annots"].Value, f.Groups["type"].Value, f.Groups["name"].Value));
                }
            }

            yield return new TypeDef(m.Groups["name"].Value, false, fields, null, file, LineOf(text, m.Index));
        }
    }

    private static FieldDef JavaField(string annots, string type, string name)
    {
        var markers = Regex.Matches(annots, @"@(?<a>[\w.]+)").Select(a => a.Groups["a"].Value.Split('.')[^1]).ToList();
        var renamed = Regex.Match(annots, @"@JsonProperty\s*\(\s*(?:value\s*=\s*)?""(?<n>[^""]+)""");
        var optional = markers.Contains("Nullable") || type.TrimStart().StartsWith("Optional<", StringComparison.Ordinal);
        return new FieldDef(renamed.Success ? renamed.Groups["n"].Value : name, type.Trim(), optional, markers);
    }

    // --- Kotlin ---------------------------------------------------------------

    private static IEnumerable<TypeDef> Kotlin(string text, string file)
    {
        foreach (Match m in Regex.Matches(text, @"(?<enum>enum\s+)?class\s+(?<name>\w+)(?:<[^>]*>)?", RegexOptions.None, Timeout))
        {
            var pos = m.Index + m.Length;
            if (m.Groups["enum"].Success)
            {
                var open = IndexOfBody(text, pos);
                var first = open < 0 ? null : Regex.Match(Balanced(text, open, '{', '}'), @"^\s*(?<c>[A-Z_][A-Z0-9_]*)\b");
                yield return new TypeDef(m.Groups["name"].Value, true, [],
                    first is { Success: true } ? JsonValue.Create(first.Groups["c"].Value) : null, file, LineOf(text, m.Index));
                continue;
            }

            if (Next(text, pos) != '(')
            {
                continue;
            }

            var fields = new List<FieldDef>();
            foreach (var p in SplitTop(Balanced(text, text.IndexOf('(', pos), '(', ')'), ','))
            {
                var pm = Regex.Match(p.Trim(), @"^(?<annots>(?:@[\w.:]+(?:\([^)]*\))?\s*)*)(?:(?:private|public|override)\s+)*(?:val|var)\s+(?<name>\w+)\s*:\s*(?<type>[^=]+?)\s*(?:=\s*(?<def>.+))?$", RegexOptions.Singleline);
                if (pm.Success)
                {
                    var markers = Regex.Matches(pm.Groups["annots"].Value, @"@(?:\w+:)?(?<a>[\w.]+)").Select(a => a.Groups["a"].Value.Split('.')[^1]).ToList();
                    var renamed = Regex.Match(pm.Groups["annots"].Value, @"JsonProperty\s*\(\s*""(?<n>[^""]+)""");
                    fields.Add(new FieldDef(renamed.Success ? renamed.Groups["n"].Value : pm.Groups["name"].Value,
                        pm.Groups["type"].Value.Trim(), pm.Groups["def"].Success || pm.Groups["type"].Value.TrimEnd().EndsWith('?'), markers));
                }
            }

            yield return new TypeDef(m.Groups["name"].Value, false, fields, null, file, LineOf(text, m.Index));
        }
    }

    // --- Go -----------------------------------------------------------------------

    private static IEnumerable<TypeDef> Go(string text, string file)
    {
        foreach (Match m in Regex.Matches(text, @"\btype\s+(?<name>\w+)\s+struct\s*\{", RegexOptions.None, Timeout))
        {
            var body = Balanced(text, m.Index + m.Length - 1, '{', '}');
            var fields = new List<FieldDef>();
            foreach (var line in body.Split('\n'))
            {
                var f = Regex.Match(line, @"^\s*(?<names>[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s+(?<type>[^`\s/][^`/]*?)\s*(?:`(?<tag>[^`]*)`)?\s*(?://.*)?$");
                if (!f.Success)
                {
                    continue;
                }

                var tag = f.Groups["tag"].Value;
                var json = Regex.Match(tag, @"json:""(?<n>[^"",]*)(?<opts>[^""]*)""");
                var bson = Regex.Match(tag, @"bson:""(?<n>[^"",]*)");
                if (json.Success && json.Groups["n"].Value == "-")
                {
                    continue;
                }

                foreach (var name in f.Groups["names"].Value.Split(',').Select(n => n.Trim()))
                {
                    // encoding/json only sees exported fields
                    if (!char.IsUpper(name[0]))
                    {
                        continue;
                    }

                    var markers = new List<string>();
                    if (bson.Success && bson.Groups["n"].Value == "_id")
                    {
                        markers.Add("BsonId");
                    }

                    var type = f.Groups["type"].Value.Trim();
                    var optional = json.Groups["opts"].Value.Contains("omitempty", StringComparison.Ordinal) || type.StartsWith('*');
                    if (Regex.IsMatch(tag, @"binding:""[^""]*required"))
                    {
                        optional = false;
                    }

                    fields.Add(new FieldDef(json.Success && json.Groups["n"].Value.Length > 0 ? json.Groups["n"].Value : name,
                        type, optional, markers));
                }
            }

            yield return new TypeDef(m.Groups["name"].Value, false, fields, null, file, LineOf(text, m.Index));
        }
    }

    // --- Rust ---------------------------------------------------------------------

    private static IEnumerable<TypeDef> Rust(string text, string file)
    {
        var decl = new Regex(
            @"(?<attrs>(?:#\[[^\]]*\]\s*)*)(?:pub(?:\([^)]*\))?\s+)?(?<kind>struct|enum)\s+(?<name>\w+)(?:<[^>]*>)?\s*\{",
            RegexOptions.None, Timeout);
        foreach (Match m in decl.Matches(text))
        {
            var attrs = m.Groups["attrs"].Value;
            var renameAll = Regex.Match(attrs, @"rename_all\s*=\s*""(?<r>[^""]+)""").Groups["r"].Value;
            var body = Balanced(text, m.Index + m.Length - 1, '{', '}');
            if (m.Groups["kind"].Value == "enum")
            {
                var first = Regex.Match(body, @"^\s*(?:#\[[^\]]*\]\s*)*(?<v>[A-Z]\w*)", RegexOptions.Multiline);
                var renamed = first.Success ? Regex.Match(body[..(first.Index)], @"rename\s*=\s*""(?<n>[^""]+)""") : null;
                yield return new TypeDef(m.Groups["name"].Value, true, [],
                    first.Success
                        ? JsonValue.Create(renamed is { Success: true } ? renamed.Groups["n"].Value : Rename(first.Groups["v"].Value, renameAll))
                        : null,
                    file, LineOf(text, m.Index));
                continue;
            }

            var fields = new List<FieldDef>();
            foreach (var part in SplitTop(body, ','))
            {
                var f = Regex.Match(part.Trim(), @"^(?<attrs>(?:#\[[^\]]*\]\s*)*)(?:pub(?:\([^)]*\))?\s+)?(?<name>\w+)\s*:\s*(?<type>.+)$", RegexOptions.Singleline);
                if (!f.Success)
                {
                    continue;
                }

                var fa = f.Groups["attrs"].Value;
                if (Regex.IsMatch(fa, @"serde\([^)]*\bskip(_deserializing)?\b"))
                {
                    continue;
                }

                var rename = Regex.Match(fa, @"rename\s*=\s*""(?<n>[^""]+)""");
                var name = rename.Success ? rename.Groups["n"].Value : Rename(f.Groups["name"].Value, renameAll);
                var type = Regex.Replace(f.Groups["type"].Value, @"\s+", " ").Trim();
                var markers = new List<string>();
                if (name == "_id")
                {
                    markers.Add("BsonId");
                }

                fields.Add(new FieldDef(name, type,
                    type.StartsWith("Option<", StringComparison.Ordinal) || Regex.IsMatch(fa, @"serde\([^)]*\bdefault\b"), markers));
            }

            yield return new TypeDef(m.Groups["name"].Value, false, fields, null, file, LineOf(text, m.Index));
        }
    }

    /// <summary>A Rust field or variant name as serde's rename_all writes it.</summary>
    private static string Rename(string name, string rule)
    {
        var words = Regex.Matches(name, @"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])").Select(w => w.Value.ToLowerInvariant()).ToList();
        if (words.Count == 0)
        {
            return name;
        }

        return rule switch
        {
            "camelCase" => words[0] + string.Concat(words.Skip(1).Select(Capital)),
            "PascalCase" => string.Concat(words.Select(Capital)),
            "snake_case" => string.Join('_', words),
            "SCREAMING_SNAKE_CASE" => string.Join('_', words).ToUpperInvariant(),
            "kebab-case" => string.Join('-', words),
            "SCREAMING-KEBAB-CASE" => string.Join('-', words).ToUpperInvariant(),
            "lowercase" => string.Concat(words),
            "UPPERCASE" => string.Concat(words).ToUpperInvariant(),
            _ => name,
        };

        static string Capital(string w) => char.ToUpperInvariant(w[0]) + w[1..];
    }

    // --- TypeScript -------------------------------------------------------------

    private static IEnumerable<TypeDef> TypeScript(string text, string file)
    {
        var decl = new Regex(
            @"(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:const\s+)?(?:(?<kind>class|interface|enum)\s+(?<name>\w+)(?:<[^>]*>)?|type\s+(?<name>\w+)(?:<[^>]*>)?\s*=\s*(?=\{))",
            RegexOptions.None, Timeout);
        foreach (Match m in decl.Matches(text))
        {
            var open = IndexOfBody(text, m.Index + m.Length, allowAnything: true);
            if (open < 0)
            {
                continue;
            }

            var body = Balanced(text, open, '{', '}');
            if (m.Groups["kind"].Value == "enum")
            {
                var first = SplitTop(body, ',').FirstOrDefault(s => s.Trim().Length > 0);
                var value = first is null ? null : Regex.Match(first, @"=\s*(?:['""`](?<s>[^'""`]*)['""`]|(?<n>-?\d+))");
                JsonNode? v = value is { Success: true }
                    ? value.Groups["s"].Success ? JsonValue.Create(value.Groups["s"].Value) : JsonValue.Create(long.Parse(value.Groups["n"].Value))
                    : JsonValue.Create(0);
                yield return new TypeDef(m.Groups["name"].Value, true, [], v, file, LineOf(text, m.Index));
                continue;
            }

            var fields = new List<FieldDef>();
            var pending = new List<string>();
            foreach (var raw in TopLevelMembers(body))
            {
                var line = raw.Trim();
                foreach (Match d in Regex.Matches(line, @"@(?<d>\w+)\s*\([^)]*\)"))
                {
                    pending.Add(d.Groups["d"].Value);
                }

                line = Regex.Replace(line, @"@\w+\s*\((?:[^()]|\([^)]*\))*\)\s*", "").Trim();
                var f = Regex.Match(line, @"^(?:(?:public|private|protected|readonly|declare)\s+)*(?<name>[\w$]+)(?<opt>\?)?!?\s*:\s*(?<type>[^=]+?)\s*(?:=.*)?$", RegexOptions.Singleline);
                if (line.Length == 0)
                {
                    continue;
                }

                if (f.Success && !line.Contains('(', StringComparison.Ordinal))
                {
                    fields.Add(new FieldDef(f.Groups["name"].Value, f.Groups["type"].Value.Trim().TrimEnd(';', ','),
                        f.Groups["opt"].Success || pending.Contains("IsOptional"), pending.ToList()));
                }

                pending.Clear();
            }

            yield return new TypeDef(m.Groups["name"].Value, false, fields, null, file, LineOf(text, m.Index));
        }
    }

    /// <summary>A class body's members: split on ; and newlines at the top level.</summary>
    private static IEnumerable<string> TopLevelMembers(string body)
    {
        var depth = 0;
        var start = 0;
        for (var i = 0; i < body.Length; i++)
        {
            var c = body[i];
            if (c is '{' or '(' or '[' or '<')
            {
                depth++;
            }
            else if (c is '}' or ')' or ']' or '>' && depth > 0)
            {
                depth--;
            }
            else if (depth == 0 && c is ';' or '\n')
            {
                yield return body[start..i];
                start = i + 1;
            }
        }

        yield return body[start..];
    }

    // --- helpers ----------------------------------------------------------------

    /// <summary>A Python literal as JSON: "x", 'x', 12, 1.5, True; null when it isn't one.</summary>
    internal static JsonNode? Literal(string value)
    {
        var v = value.Trim();
        var s = Regex.Match(v, @"^[rbuf]?(['""])(?<s>.*)\1$");
        if (s.Success)
        {
            return JsonValue.Create(s.Groups["s"].Value);
        }

        if (long.TryParse(v, out var n))
        {
            return JsonValue.Create(n);
        }

        if (double.TryParse(v, System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var d))
        {
            return JsonValue.Create(d);
        }

        return v switch
        {
            "True" or "true" => JsonValue.Create(true),
            "False" or "false" => JsonValue.Create(false),
            _ => null,
        };
    }

    private static char Next(string text, int pos)
    {
        while (pos < text.Length && char.IsWhiteSpace(text[pos]))
        {
            pos++;
        }

        return pos < text.Length ? text[pos] : '\0';
    }

    /// <summary>
    /// Where a declaration's body starts: the next '{' before a ';' (base lists,
    /// constraints and positional parameters may come first). -1 when it has none.
    /// </summary>
    private static int IndexOfBody(string text, int pos, bool allowAnything = false)
    {
        var depth = 0;
        for (var i = pos; i < text.Length && i < pos + 2000; i++)
        {
            switch (text[i])
            {
                case '(':
                    depth++;
                    break;
                case ')':
                    depth--;
                    break;
                case '{' when depth <= 0:
                    return i;
                case ';' when depth <= 0 && !allowAnything:
                    return -1;
            }
        }

        return -1;
    }

    /// <summary>What lies between the bracket at <paramref name="open"/> and its match.</summary>
    internal static string Balanced(string text, int open, char left, char right)
    {
        if (open < 0 || open >= text.Length)
        {
            return "";
        }

        var depth = 0;
        char? quote = null;
        for (var i = open; i < text.Length; i++)
        {
            var c = text[i];
            if (quote is not null)
            {
                if (c == '\\')
                {
                    i++;
                }
                else if (c == quote)
                {
                    quote = null;
                }

                continue;
            }

            if (c is '"' or '\'' or '`')
            {
                quote = c;
            }
            else if (c == left)
            {
                depth++;
            }
            else if (c == right && --depth == 0)
            {
                return text[(open + 1)..i];
            }
        }

        return text[(open + 1)..];
    }

    /// <summary>Splits at a separator outside brackets and quotes.</summary>
    internal static List<string> SplitTop(string text, char separator)
    {
        var parts = new List<string>();
        var depth = 0;
        char? quote = null;
        var start = 0;
        for (var i = 0; i < text.Length; i++)
        {
            var c = text[i];
            if (quote is not null)
            {
                if (c == '\\')
                {
                    i++;
                }
                else if (c == quote)
                {
                    quote = null;
                }

                continue;
            }

            switch (c)
            {
                case '"' or '\'' or '`':
                    quote = c;
                    break;
                case '(' or '[' or '{' or '<':
                    depth++;
                    break;
                case ')' or ']' or '}' or '>' when depth > 0:
                    // '=>' and '->' aren't brackets
                    if (!(c == '>' && i > 0 && text[i - 1] is '=' or '-'))
                    {
                        depth--;
                    }

                    break;
                default:
                    if (c == separator && depth == 0)
                    {
                        parts.Add(text[start..i]);
                        start = i + 1;
                    }

                    break;
            }
        }

        parts.Add(text[start..]);
        return parts.Where(p => p.Trim().Length > 0).ToList();
    }

    /// <summary>A Python annotation and its default, split at the first top-level '='.</summary>
    internal static (string Type, string? Default) SplitFirst(string text, char separator)
    {
        var parts = SplitTop(text, separator);
        return parts.Count <= 1 ? (text, null) : (parts[0], string.Join(separator, parts.Skip(1)));
    }
}
