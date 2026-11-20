using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace MicroserviceScripts.Ui.Routes;

/// <summary>A path or query parameter a route takes, with an example value.</summary>
/// <param name="In">"path" or "query".</param>
public sealed record ParamShape(string Name, string In, string Type, string Example, bool Required);

/// <summary>
/// What a route expects, as Swagger would show it, read from the handler's
/// signature and the types it names.
/// </summary>
/// <param name="Body">An example JSON body, indented; null when it takes none, or it couldn't be told.</param>
/// <param name="BodyType">The type the body is read into, e.g. CreateBlogDto.</param>
/// <param name="BodySource">Where that type is declared (file:line).</param>
/// <param name="Notes">What to know about the example (placeholders, values sent on every request).</param>
public sealed record RequestShape(
    IReadOnlyList<ParamShape> Params, string? Body, string? BodyType, string? BodySource, IReadOnlyList<string> Notes);

/// <summary>
/// Builds a route's example request from its handler's parameters and the
/// service's types, with example values from Data/request-types.json.
/// </summary>
public sealed class RequestShapes
{
    private const int MaxDepth = 4;
    private static readonly JsonSerializerOptions Indented = new() { WriteIndented = true };

    private readonly Dictionary<string, (JsonNode? Value, string? Note)> exact = new(StringComparer.Ordinal);
    private readonly Dictionary<string, (JsonNode? Value, string? Note)> loose = new(StringComparer.OrdinalIgnoreCase);
    private readonly List<(Regex Pattern, JsonNode? Value)> fieldHints = [];
    private readonly HashSet<string> skipNames = new(StringComparer.Ordinal);
    private readonly HashSet<string> skipOptionalNames = new(StringComparer.Ordinal);
    private readonly HashSet<string> skipMarkers = new(StringComparer.Ordinal);

    public static RequestShapes LoadDefault() =>
        new(File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "Data", "request-types.json")));

    public RequestShapes(string json)
    {
        var root = JsonNode.Parse(json)!;
        foreach (var t in root["types"]!.AsArray())
        {
            var value = t!["value"]?.DeepClone();
            var note = t["note"]?.GetValue<string>();
            foreach (var n in t["names"]!.AsArray())
            {
                exact.TryAdd(n!.GetValue<string>(), (value, note));
                loose.TryAdd(n.GetValue<string>(), (value, note));
            }
        }

        foreach (var f in root["fields"]?.AsArray() ?? [])
        {
            fieldHints.Add((new Regex(f!["pattern"]!.GetValue<string>()), f["value"]?.DeepClone()));
        }

        var skip = root["skipInBodies"];
        foreach (var n in skip?["names"]?.AsArray() ?? [])
        {
            skipNames.Add(n!.GetValue<string>());
        }

        foreach (var n in skip?["optionalNames"]?.AsArray() ?? [])
        {
            skipOptionalNames.Add(n!.GetValue<string>());
        }

        foreach (var n in skip?["markers"]?.AsArray() ?? [])
        {
            skipMarkers.Add(n!.GetValue<string>());
        }
    }

    private sealed record Param(string Name, string Type, string? Default, string Annotations);

    /// <summary>The example request of a route; null when its handler's parameters can't be read.</summary>
    public RequestShape? Build(SourceRoute route, TypeIndex types, Func<string, string> relative)
    {
        var language = Path.GetExtension(route.File).ToLowerInvariant();
        if (language == ".go")
        {
            return Go(route, types, relative);
        }

        if (route.Signature is null)
        {
            return null;
        }

        if (language == ".rs")
        {
            return Rust(route, types, relative);
        }

        var parameters = Parse(route.Signature, language);
        var placeholders = Regex.Matches(route.Path, @"\{(\w+)\}").Select(m => m.Groups[1].Value).ToHashSet();
        var notes = new List<string>();
        var shapes = new List<ParamShape>();
        var bodies = new List<(string Name, JsonNode? Example, TypeDef? Type, bool Embed)>();

        foreach (var p in parameters)
        {
            var (place, name) = Classify(p, language, placeholders, types);
            if (place is "path" or "query")
            {
                var example = Example(p.Type, name, 0, [], notes, types);
                if (example is JsonValue v)
                {
                    shapes.Add(new ParamShape(name, place, Simplify(p.Type), Text(v), place == "path" || IsRequired(p, language)));
                }
                else if (place == "query" && types.Find(BaseName(p.Type)) is { IsEnum: false } dto)
                {
                    // a whole object bound from the query string: one parameter per required field
                    foreach (var f in dto.Fields.Where(f => !f.Optional))
                    {
                        if (Example(f.Type, f.JsonName, 1, [], notes, types) is JsonValue fv)
                        {
                            shapes.Add(new ParamShape(f.JsonName, "query", Simplify(f.Type), Text(fv), true));
                        }
                    }
                }
            }
            else if (place == "body")
            {
                var type = types.Find(BaseName(p.Type));
                bodies.Add((name, Example(p.Type, name, 0, [], notes, types), type is { IsEnum: false } ? type : null,
                    Embedded(p, language)));
            }
        }

        string? body = null, bodyType = null, bodySource = null;
        if (bodies.Count == 1 && !bodies[0].Embed)
        {
            body = bodies[0].Example?.ToJsonString(Indented);
            bodyType = bodies[0].Type?.Name;
            bodySource = bodies[0].Type is { } t ? $"{relative(t.File)}:{t.Line}" : null;
        }
        else if (bodies.Count > 0)
        {
            // FastAPI embeds several body parameters under their names, as does Body(embed=True)
            var o = new JsonObject();
            foreach (var b in bodies)
            {
                o[b.Name] = b.Example?.DeepClone();
            }

            body = o.ToJsonString(Indented);
        }

        AddUniqueNote(route, body, notes);

        foreach (var p in shapes.Where(s => s.In == "path"))
        {
            notes.Add($"{{{p.Name}}} is {p.Example} as a placeholder: use one that exists in your data.");
        }

        return new RequestShape(shapes, body, bodyType, bodySource, notes.Distinct().ToList());
    }

    // --- Go: the request is read inside the handler ---------------------------

    private RequestShape? Go(SourceRoute route, TypeIndex types, Func<string, string> relative)
    {
        if (route.HandlerBody is null)
        {
            return null;
        }

        var notes = new List<string>();
        var shapes = PathPlaceholders(route, notes);
        var code = route.HandlerBody;
        string? body = null, bodyType = null, bodySource = null;
        var bind = Regex.Match(code, @"(?:ShouldBindJSON|BindJSON|ShouldBindWith|ShouldBind|BodyParser|Bind|Decode)\(\s*&?(?<v>\w+)");
        if (bind.Success)
        {
            var v = Regex.Escape(bind.Groups["v"].Value);
            var declared = Regex.Match(code,
                $@"(?:\bvar\s+{v}\s+\*?(?<t>[\w.]+)|\b{v}\s*:?=\s*(?:&|new\()?(?<t>[\w.]+)\s*[{{)])");
            if (declared.Success && types.Find(BaseName(declared.Groups["t"].Value)) is { IsEnum: false } def)
            {
                body = Example(def.Name, bind.Groups["v"].Value, 0, [], notes, types)?.ToJsonString(Indented);
                bodyType = def.Name;
                bodySource = $"{relative(def.File)}:{def.Line}";
            }
        }

        foreach (Match q in Regex.Matches(code, @"\.(?:Query|FormValue|QueryParam)\(\s*""(?<n>\w+)""\s*\)"))
        {
            // gin's c.Query, echo's c.QueryParam: optional by nature, so only noted
            notes.Add($"It reads the query parameter {q.Groups["n"].Value}; add ?{q.Groups["n"].Value}=… to the path if it needs one.");
        }

        AddUniqueNote(route, body, notes);
        return new RequestShape(shapes, body, bodyType, bodySource, notes.Distinct().ToList());
    }

    // --- Rust: extractors in the signature (Axum, Actix, Rocket) ------------------

    private RequestShape Rust(SourceRoute route, TypeIndex types, Func<string, string> relative)
    {
        var notes = new List<string>();
        var placeholders = Regex.Matches(route.Path, @"\{(\w+)\}").Select(m => m.Groups[1].Value).ToList();
        var shapes = new List<ParamShape>();
        string? body = null, bodyType = null, bodySource = null;
        foreach (var part in TypeIndex.SplitTop(route.Signature!, ','))
        {
            var m = Regex.Match(part.Trim(), @"^(?<pat>[^:]+?)\s*:(?!:)\s*(?<type>.+)$", RegexOptions.Singleline);
            if (!m.Success)
            {
                continue;
            }

            var type = Regex.Replace(m.Groups["type"].Value.Trim(), @"^(?:\w+::)*", "");
            var name = Regex.Match(m.Groups["pat"].Value, @"(\w+)\)?\s*$").Groups[1].Value;
            var extractor = Regex.Match(type, @"^(?<e>Json|Form|Path|Query|Data|State|Extension)\s*<(?<inner>.+)>$");
            var inner = extractor.Success ? Regex.Replace(extractor.Groups["inner"].Value, @"^(?:\w+::)*", "") : type;
            switch (extractor.Success ? extractor.Groups["e"].Value : null)
            {
                case "Json" or "Form":
                    var def = types.Find(BaseName(inner));
                    body = Example(inner, name, 0, [], notes, types)?.ToJsonString(Indented);
                    bodyType = def is { IsEnum: false } ? def.Name : null;
                    bodySource = def is { IsEnum: false } ? $"{relative(def.File)}:{def.Line}" : null;
                    break;
                case "Path":
                    // a tuple fills the placeholders in order; a single type the first one
                    var parts = inner.StartsWith('(') ? TypeIndex.SplitTop(inner.Trim('(', ')'), ',') : [inner];
                    for (var i = 0; i < parts.Count && i < placeholders.Count; i++)
                    {
                        if (Example(parts[i], placeholders[i], 0, [], notes, types) is JsonValue v)
                        {
                            shapes.Add(new ParamShape(placeholders[i], "path", parts[i].Trim(), Text(v), true));
                        }
                    }

                    break;
                case "Query":
                    if (types.Find(BaseName(inner)) is { IsEnum: false } q)
                    {
                        foreach (var f in q.Fields.Where(f => !f.Optional))
                        {
                            if (Example(f.Type, f.JsonName, 1, [], notes, types) is JsonValue fv)
                            {
                                shapes.Add(new ParamShape(f.JsonName, "query", f.Type, Text(fv), true));
                            }
                        }
                    }

                    break;
                case null when placeholders.Contains(name):
                    // Rocket: a plain parameter named after a <segment>
                    if (Example(type, name, 0, [], notes, types) is JsonValue pv)
                    {
                        shapes.Add(new ParamShape(name, "path", type, Text(pv), true));
                    }

                    break;
            }
        }

        foreach (var p in shapes.Where(s => s.In == "path"))
        {
            notes.Add($"{{{p.Name}}} is {p.Example} as a placeholder: use one that exists in your data.");
        }

        AddUniqueNote(route, body, notes);
        return new RequestShape(shapes, body, bodyType, bodySource, notes.Distinct().ToList());
    }

    /// <summary>Path placeholders whose type isn't declared anywhere (Go): a number, the usual id.</summary>
    private static List<ParamShape> PathPlaceholders(SourceRoute route, List<string> notes)
    {
        var shapes = Regex.Matches(route.Path, @"\{(\w+)\}")
            .Select(m => new ParamShape(m.Groups[1].Value, "path", "string", "1", true)).ToList();
        foreach (var p in shapes)
        {
            notes.Add($"{{{p.Name}}} is 1 as a placeholder: use one that exists in your data.");
        }

        return shapes;
    }

    private static void AddUniqueNote(SourceRoute route, string? body, List<string> notes)
    {
        if (body is not null && route.Method == "POST" && JsonNode.Parse(body) is JsonObject top)
        {
            var same = top.Select(kv => kv.Key).Where(k => Regex.IsMatch(k, @"(?i)e-?mail|user_?name|login|slug|^code$|^sku$")).ToList();
            if (same.Count > 0)
            {
                notes.Add($"Every request sends the same {string.Join(" and ", same)}: if the API wants it unique, "
                          + "only the first request succeeds.");
            }
        }
    }

    // --- which parameter is what ----------------------------------------------

    private static readonly Regex SkipTypes = new(
        @"^(Request|Response|HTTPConnection|WebSocket|BackgroundTasks|Session|AsyncSession|Connection|Cursor|Db\w*|" +
        @"CancellationToken|HttpContext|HttpRequest|HttpResponse|ClaimsPrincipal|IFormFile|IFormFileCollection|ILogger.*|" +
        @"HttpServletRequest|HttpServletResponse|Principal|Authentication|Model|ModelMap|BindingResult|Errors|Pageable|Sort|" +
        @"UploadFile|ServerWebExchange|Locale)$");

    private (string In, string Name) Classify(Param p, string language, HashSet<string> placeholders, TypeIndex types)
    {
        var a = p.Annotations;
        var named = Regex.Match(a, @"(?:Name\s*=\s*|value\s*=\s*|name\s*=\s*|\(\s*)['""](?<n>[^'""]+)['""]");
        var name = named.Success ? named.Groups["n"].Value : p.Name;
        var simple = BaseName(p.Type);
        switch (language)
        {
            case ".py":
                if (p.Default is { } d && Regex.IsMatch(d, @"^\s*(Depends|Security|Header|Cookie|File|Form)\s*\("))
                {
                    return ("skip", p.Name);
                }

                if (SkipTypes.IsMatch(simple))
                {
                    return ("skip", p.Name);
                }

                if (placeholders.Contains(p.Name) || p.Default?.TrimStart().StartsWith("Path(", StringComparison.Ordinal) == true)
                {
                    return ("path", p.Name);
                }

                if (p.Default?.TrimStart().StartsWith("Body(", StringComparison.Ordinal) == true)
                {
                    return ("body", p.Name);
                }

                if (p.Default?.TrimStart().StartsWith("Query(", StringComparison.Ordinal) == true)
                {
                    return ("query", p.Name);
                }

                return types.Find(simple) is { IsEnum: false } ? ("body", p.Name) : ("query", p.Name);

            case ".cs":
                if (Regex.IsMatch(a, @"\bFrom(Services|Header|Form|Keyed\w*)\b") || SkipTypes.IsMatch(simple))
                {
                    return ("skip", name);
                }

                if (a.Contains("FromBody", StringComparison.Ordinal))
                {
                    return ("body", name);
                }

                if (a.Contains("FromRoute", StringComparison.Ordinal) || placeholders.Contains(name))
                {
                    return ("path", name);
                }

                if (a.Contains("FromQuery", StringComparison.Ordinal))
                {
                    return ("query", name);
                }

                // [ApiController] conventions: a known type is the body, a simple one the query; anything else is a service
                return types.Find(simple) is { IsEnum: false } ? ("body", name)
                    : IsScalar(p.Type) ? ("query", name) : ("skip", name);

            case ".java" or ".kt":
                if (a.Contains("@RequestBody", StringComparison.Ordinal))
                {
                    return ("body", name);
                }

                if (a.Contains("@PathVariable", StringComparison.Ordinal))
                {
                    return ("path", name);
                }

                return a.Contains("@RequestParam", StringComparison.Ordinal) ? ("query", name) : ("skip", name);

            case ".ts":
                if (a.Contains("@Body", StringComparison.Ordinal))
                {
                    return ("body", name);
                }

                if (a.Contains("@Param", StringComparison.Ordinal))
                {
                    return ("path", name);
                }

                return a.Contains("@Query", StringComparison.Ordinal) ? ("query", name) : ("skip", name);
        }

        return ("skip", name);
    }

    private static bool Embedded(Param p, string language) =>
        language == ".py" && p.Default is { } d && Regex.IsMatch(d, @"^\s*Body\(") && !Regex.IsMatch(d, @"embed\s*=\s*False")
        || language == ".ts" && Regex.IsMatch(p.Annotations, @"@Body\(\s*['""]");

    private static bool IsRequired(Param p, string language) => language switch
    {
        ".py" => p.Default is null || Regex.IsMatch(p.Default, @"^\s*Query\(\s*(\.\.\.|default\s*=\s*\.\.\.)") ,
        ".cs" => p.Default is null && !p.Type.TrimEnd().EndsWith('?'),
        ".java" or ".kt" => !Regex.IsMatch(p.Annotations, @"required\s*=\s*false|defaultValue") && !p.Type.StartsWith("Optional<", StringComparison.Ordinal),
        ".ts" => !p.Name.EndsWith('?'),
        _ => true,
    };

    // --- reading signatures ---------------------------------------------------

    private static List<Param> Parse(string signature, string language)
    {
        var parameters = new List<Param>();
        foreach (var raw in TypeIndex.SplitTop(signature, ','))
        {
            var part = Regex.Replace(raw.Trim(), @"\s+", " ");
            Match m;
            switch (language)
            {
                case ".py":
                    m = Regex.Match(part, @"^(?<name>[A-Za-z_]\w*)\s*(?::\s*(?<rest>.+))?$");
                    if (!m.Success || m.Groups["name"].Value is "self" or "cls")
                    {
                        continue;
                    }

                    var rest = m.Groups["rest"].Success ? m.Groups["rest"].Value : "";
                    var (type, def) = rest.Length > 0 ? TypeIndex.SplitFirst(rest, '=') : ("", null);
                    if (rest.Length == 0)
                    {
                        // name = default, no annotation
                        var plain = Regex.Match(part, @"^(?<name>\w+)\s*=\s*(?<def>.+)$");
                        def = plain.Success ? plain.Groups["def"].Value : null;
                    }

                    parameters.Add(new Param(m.Groups["name"].Value, type.Trim(), def?.Trim(), ""));
                    break;
                case ".cs":
                    m = Regex.Match(part, @"^(?<attrs>(?:\[[^\]]*\]\s*)*)(?:(?:this|params|ref|out|in|scoped)\s+)*(?<type>.+?)\s+(?<name>@?\w+)\s*(?:=\s*(?<def>.+))?$");
                    if (m.Success)
                    {
                        parameters.Add(new Param(m.Groups["name"].Value.TrimStart('@'), m.Groups["type"].Value.Trim(),
                            m.Groups["def"].Success ? m.Groups["def"].Value : null, m.Groups["attrs"].Value));
                    }

                    break;
                case ".java":
                    m = Regex.Match(part, @"^(?<annots>(?:@[\w.]+(?:\((?:[^()]|\([^)]*\))*\))?\s*)*)(?:final\s+)?(?<type>.+?)\s+(?<name>\w+)$");
                    if (m.Success)
                    {
                        parameters.Add(new Param(m.Groups["name"].Value, m.Groups["type"].Value.Trim(), null, m.Groups["annots"].Value));
                    }

                    break;
                case ".kt":
                    m = Regex.Match(part, @"^(?<annots>(?:@[\w.]+(?:\((?:[^()]|\([^)]*\))*\))?\s*)*)(?<name>\w+)\s*:\s*(?<type>[^=]+?)\s*(?:=\s*(?<def>.+))?$");
                    if (m.Success)
                    {
                        parameters.Add(new Param(m.Groups["name"].Value, m.Groups["type"].Value.Trim(),
                            m.Groups["def"].Success ? m.Groups["def"].Value : null, m.Groups["annots"].Value));
                    }

                    break;
                case ".ts":
                    m = Regex.Match(part, @"^(?<annots>(?:@\w+\((?:[^()]|\([^)]*\))*\)\s*)*)(?:(?:public|private|readonly)\s+)*(?<name>\w+\??)\s*:\s*(?<type>[^=]+?)\s*(?:=\s*(?<def>.+))?$");
                    if (m.Success)
                    {
                        parameters.Add(new Param(m.Groups["name"].Value, m.Groups["type"].Value.Trim(),
                            m.Groups["def"].Success ? m.Groups["def"].Value : null, m.Groups["annots"].Value));
                    }

                    break;
            }
        }

        return parameters;
    }

    // --- example values -------------------------------------------------------

    private static readonly Regex Wrappers = new(
        @"^(?:Optional|Option|Annotated|Required|NotRequired|ReadOnly|Final|Nullable|Partial|Readonly|Mono|Flux|Task|ValueTask|ActionResult|Promise|Observable|Box|Rc|Arc|Ref|Cow|Json)\s*[\[<](?<inner>.+)[\]>]$");

    private static readonly Regex Lists = new(
        @"^(?:List|list|Sequence|MutableSequence|Set|set|FrozenSet|frozenset|Iterable|Collection|Tuple|tuple|conlist|" +
        @"IList|ICollection|IEnumerable|IReadOnlyList|IReadOnlyCollection|HashSet|ISet|SortedSet|ImmutableList|ImmutableArray|" +
        @"ArrayList|LinkedList|Array|ReadonlyArray|MutableList|Vec|deque|Deque|Queue|Stack)\s*[\[<(](?<inner>.+)[\]>)]$");

    private static readonly Regex Maps = new(
        @"^(?:Dict|dict|Mapping|MutableMapping|DefaultDict|OrderedDict|TypedDict|Map|HashMap|LinkedHashMap|TreeMap|Dictionary|" +
        @"IDictionary|IReadOnlyDictionary|SortedDictionary|ConcurrentDictionary|Record|MutableMap|BTreeMap)\b");

    private JsonNode? Example(string type, string field, int depth, HashSet<string> seen, List<string> notes, TypeIndex types)
    {
        var t = Regex.Replace(type.Trim(), @"^(?:required|final|readonly|const)\s+", "").Trim();
        t = t.Trim('\'', '"');                       // python forward references
        t = Regex.Replace(t, @"^[*&]+(?:'\w+\s+)?(?:mut\s+)?", "");   // Go pointers, Rust references
        if (t.StartsWith("[]", StringComparison.Ordinal))
        {
            return new JsonArray(Example(t[2..], field, depth + 1, seen, notes, types));   // Go slices
        }

        if (Regex.IsMatch(t, @"^map\[") || t is "interface{}" or "any")
        {
            return new JsonObject();
        }
        if (t.EndsWith('?'))
        {
            t = t[..^1];
        }

        // python 3.10 / TypeScript unions: the first that isn't None/null/undefined
        var union = TypeIndex.SplitTop(t, '|').Select(x => x.Trim()).Where(x => x is not ("None" or "null" or "undefined")).ToList();
        if (union.Count > 1)
        {
            var literal = Regex.Match(union[0], @"^['""](?<s>[^'""]*)['""]$");
            return literal.Success ? JsonValue.Create(literal.Groups["s"].Value) : Example(union[0], field, depth, seen, notes, types);
        }

        if (union.Count == 1)
        {
            t = union[0];
        }

        if (Regex.Match(t, @"^Union\[(?<inner>.+)\]$") is { Success: true } u)
        {
            var first = TypeIndex.SplitTop(u.Groups["inner"].Value, ',').Select(x => x.Trim()).FirstOrDefault(x => x != "None");
            return first is null ? null : Example(first, field, depth, seen, notes, types);
        }

        if (Regex.Match(t, @"^Literal\[(?<inner>.+)\]$") is { Success: true } lit)
        {
            return TypeIndex.Literal(TypeIndex.SplitTop(lit.Groups["inner"].Value, ',')[0]);
        }

        if (Wrappers.Match(t) is { Success: true } w)
        {
            return Example(TypeIndex.SplitTop(w.Groups["inner"].Value, ',')[0], field, depth, seen, notes, types);
        }

        if (t.EndsWith("[]", StringComparison.Ordinal))
        {
            return new JsonArray(Example(t[..^2], field, depth + 1, seen, notes, types));
        }

        if (Lists.Match(t) is { Success: true } l)
        {
            return new JsonArray(Example(TypeIndex.SplitTop(l.Groups["inner"].Value, ',')[0], field, depth + 1, seen, notes, types));
        }

        if (Maps.IsMatch(t))
        {
            return new JsonObject();
        }

        var name = BaseName(t);
        if (Constrained(name) is { } constrained)
        {
            name = constrained;
        }

        if ((exact.TryGetValue(name, out var known) || loose.TryGetValue(name, out known)))
        {
            if (known.Note is { } note)
            {
                notes.Add($"{field} is {note}: {known.Value?.ToJsonString()} is a placeholder.");
            }

            if (known.Value is JsonValue s && s.TryGetValue<string>(out var str) && str == "string"
                && fieldHints.FirstOrDefault(h => h.Pattern.IsMatch(field)) is { Value: { } hint })
            {
                return hint.DeepClone();
            }

            return known.Value?.DeepClone();
        }

        if (types.Find(name) is { } def)
        {
            if (def.IsEnum)
            {
                return def.FirstEnumValue?.DeepClone();
            }

            if (depth >= MaxDepth || !seen.Add(def.Name))
            {
                return new JsonObject();
            }

            var o = new JsonObject();
            foreach (var f in def.Fields.Where(f => !SkipInBody(f)))
            {
                o[f.JsonName] = Example(f.Type, f.JsonName, depth + 1, seen, notes, types);
            }

            seen.Remove(def.Name);
            return o;
        }

        // not a type this knows: an object when it looks like a class, else text
        if (name.Length > 0 && char.IsUpper(name[0]))
        {
            notes.Add($"What {name} holds couldn't be read from the code: its example is empty.");
            return new JsonObject();
        }

        return JsonValue.Create("string");
    }

    private bool SkipInBody(FieldDef f) =>
        skipNames.Contains(f.JsonName)
        || f.Optional && skipOptionalNames.Contains(f.JsonName)
        || f.Markers.Any(skipMarkers.Contains);

    /// <summary>Pydantic's constrained types (conint, constr…) are their base type.</summary>
    private static string? Constrained(string name) =>
        Regex.Match(name, @"^con(int|str|float|decimal|bytes|date)$") is { Success: true } m
            ? m.Groups[1].Value switch { "int" => "int", "float" => "float", "decimal" => "decimal", "bytes" => "bytes", "date" => "date", _ => "str" }
            : null;

    /// <summary>The type's own name: without generic arguments, call parentheses or namespace.</summary>
    private static string BaseName(string type)
    {
        var t = Regex.Replace(type.Trim().TrimEnd('?'), @"[\[<(].*$", "").Trim();
        return t.Split('.')[^1];
    }

    private static string Simplify(string type) => Regex.Replace(type.Trim(), @"\s+", " ");

    private bool IsScalar(string type) =>
        exact.TryGetValue(BaseName(type), out var v) && v.Value is JsonValue;

    private static string Text(JsonValue v) =>
        v.TryGetValue<string>(out var s) ? s : v.ToJsonString().Trim('"').ToLowerInvariant();
}
