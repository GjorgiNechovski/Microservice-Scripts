using System.Globalization;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace MicroserviceScripts.Ui.Load;

/// <summary>One request the load test sends.</summary>
/// <param name="Name">What k6 reports it as.</param>
/// <param name="Path">Relative to the base address, or a full address.</param>
/// <param name="Body">JSON sent with it, if any.</param>
/// <param name="Share">How often it is picked, relative to the others.</param>
public sealed record LoadRequest(string Name, string Method, string Path, string? Body, double Share);

/// <summary>The request that logs in, and where its response keeps the token.</summary>
/// <param name="Body">JSON; <c>{{password}}</c> in it is filled in from LOGIN_PASSWORD when the test runs.</param>
/// <param name="TokenFrom">"auto" (look for it), "body" (the response body is the token) or "field".</param>
/// <param name="TokenField">With "field": where it is in the JSON response, as a.b.0.c.</param>
public sealed record LoginRequest(
    string Method, string Path, string? Body, string TokenFrom = "auto", string? TokenField = null);

/// <summary>How the test's requests say who they are, as in Postman's Authorization tab.</summary>
/// <param name="Type">"login": log in first and send the token it answers with; "token": send a token or API key you already have.</param>
/// <param name="HeaderName">The header every request carries it in.</param>
/// <param name="HeaderPrefix">What goes before it, e.g. "Bearer ".</param>
public sealed record Auth(string Type, string HeaderName, string HeaderPrefix, LoginRequest? Login = null)
{
    public const string TypeLogin = "login";
    public const string TypeToken = "token";

    /// <summary>The environment variable the secret comes from: never written to a file.</summary>
    public string SecretVariable => Type == TypeToken ? LoadScript.TokenVariable : LoadScript.PasswordVariable;
}

/// <summary>A k6 script written from routes the user picked: no code to write.</summary>
public sealed record LoadScript(string BaseUrl, Auth? Auth, IReadOnlyList<LoadRequest> Requests)
{
    public const string PasswordPlaceholder = "{{password}}";
    public const string PasswordVariable = "LOGIN_PASSWORD";
    public const string TokenVariable = "AUTH_TOKEN";

    /// <summary>The only environment variables the app passes secrets in.</summary>
    public static readonly IReadOnlySet<string> SecretVariables = new HashSet<string> { PasswordVariable, TokenVariable };

    private static readonly string[] Methods = ["GET", "POST", "PUT", "PATCH", "DELETE"];

    /// <summary>The full address of a path: as it is when it is one, else after the base address.</summary>
    public string Url(string path) =>
        Regex.IsMatch(path, "^https?://", RegexOptions.IgnoreCase) ? path : BaseUrl.TrimEnd('/') + path;

    /// <summary>What is wrong with it, in words for the user.</summary>
    public IReadOnlyList<string> Problems()
    {
        var problems = new List<string>();
        if (!Uri.TryCreate(BaseUrl, UriKind.Absolute, out var b) || b.Scheme is not ("http" or "https"))
        {
            problems.Add($"'{BaseUrl}' isn't an http:// address.");
        }

        if (Requests.Count == 0)
        {
            problems.Add("Pick at least one route for the load test.");
        }

        foreach (var r in Requests)
        {
            CheckRequest(problems, r.Method, r.Path, r.Body, r.Name);
            if (r.Share <= 0)
            {
                problems.Add($"{r.Name}: its share must be more than 0.");
            }
        }

        problems.AddRange(AuthProblems(Auth));
        return problems;
    }

    public static IReadOnlyList<string> AuthProblems(Auth? auth)
    {
        var problems = new List<string>();
        if (auth is null)
        {
            return problems;
        }

        if (auth.Type is not (Auth.TypeLogin or Auth.TypeToken))
        {
            problems.Add($"'{auth.Type}' isn't a kind of authorization this knows.");
        }

        if (string.IsNullOrWhiteSpace(auth.HeaderName))
        {
            problems.Add("Name the header the token goes in (usually Authorization).");
        }

        if (auth.Type == Auth.TypeLogin)
        {
            if (auth.Login is not { } login)
            {
                problems.Add("Give the request that logs in.");
            }
            else
            {
                CheckRequest(problems, login.Method, login.Path, login.Body, "The login");
                if (login.TokenFrom == "field" && string.IsNullOrWhiteSpace(login.TokenField))
                {
                    problems.Add("Say which field of the login's response holds the token.");
                }
            }
        }

        return problems;
    }

    private static void CheckRequest(List<string> problems, string method, string path, string? body, string name)
    {
        if (!Methods.Contains(method))
        {
            problems.Add($"{name}: '{method}' isn't one of {string.Join(", ", Methods)}.");
        }

        if (!path.StartsWith('/') && !Regex.IsMatch(path, "^https?://", RegexOptions.IgnoreCase))
        {
            problems.Add($"{name}: the path must start with / (or be a full http:// address).");
        }

        foreach (Match m in Regex.Matches(path, @"\{(\w+)\}|(?<=/):(\w+)"))
        {
            problems.Add($"{name}: put a real value in place of {m.Value} in {path}.");
        }

        if (!string.IsNullOrWhiteSpace(body))
        {
            try
            {
                using var _ = JsonDocument.Parse(body.Replace(PasswordPlaceholder, "x", StringComparison.Ordinal));
            }
            catch (JsonException)
            {
                problems.Add($"{name}: its body isn't valid JSON.");
            }
        }
    }
}

/// <param name="Field">Where it was found in the JSON response (a.b.0.c); null when the response body is the token.</param>
public sealed record FoundToken(string Value, string? Field);

/// <summary>
/// Finds the token in a login's response, the same way the generated script
/// does: where the user said, or by looking in the usual places.
/// </summary>
public static class TokenFinder
{
    /// <summary>Field names that usually hold the token, best first. The script carries the same list.</summary>
    public static readonly string[] Names =
        ["access_token", "accessToken", "token", "jwt", "id_token", "idToken", "authToken", "auth_token", "sessionToken"];

    private static readonly Regex Jwt = new(@"^[\w-]+\.[\w-]+\.[\w-]+$");

    public static FoundToken? Find(string body, string from, string? field)
    {
        JsonElement? json = null;
        try
        {
            using var doc = JsonDocument.Parse(body);
            json = doc.RootElement.Clone();
        }
        catch (JsonException)
        {
        }

        if (from == "field")
        {
            return json is { } root && At(root, field ?? "") is { ValueKind: JsonValueKind.String or JsonValueKind.Number } v
                && Text(v) is { Length: > 0 } s
                ? new FoundToken(s, field)
                : null;
        }

        if (from == "body" || json is null)
        {
            var whole = json is { ValueKind: JsonValueKind.String } str ? str.GetString()! : body.Trim().Trim('"');
            // looking by itself, only something shaped like a token counts
            return whole.Length > 0 && (from == "body" || (whole.Length >= 16 && !whole.Any(char.IsWhiteSpace)))
                ? new FoundToken(whole, null)
                : null;
        }

        var element = json.Value;
        if (element.ValueKind == JsonValueKind.String)
        {
            return element.GetString() is { Length: > 0 } s ? new FoundToken(s, null) : null;
        }

        // a usual name, at the top or one level down
        var objects = new List<(JsonElement Node, string Path)> { (element, "") };
        if (element.ValueKind == JsonValueKind.Object)
        {
            objects.AddRange(element.EnumerateObject()
                .Where(p => p.Value.ValueKind == JsonValueKind.Object)
                .Select(p => (p.Value, p.Name + ".")));
        }

        foreach (var (node, path) in objects.Where(o => o.Node.ValueKind == JsonValueKind.Object))
        {
            foreach (var name in Names)
            {
                var hit = node.EnumerateObject().FirstOrDefault(p =>
                    p.Name.Equals(name, StringComparison.OrdinalIgnoreCase) && p.Value.ValueKind == JsonValueKind.String
                    && p.Value.GetString()!.Length > 0);
                if (hit.Value.ValueKind == JsonValueKind.String)
                {
                    return new FoundToken(hit.Value.GetString()!, path + hit.Name);
                }
            }
        }

        // anything shaped like a JWT
        return JwtIn(element, "", depth: 0);
    }

    private static FoundToken? JwtIn(JsonElement node, string path, int depth)
    {
        if (node.ValueKind == JsonValueKind.String && Jwt.IsMatch(node.GetString()!))
        {
            return new FoundToken(node.GetString()!, path.Length == 0 ? null : path);
        }

        if (depth >= 3)
        {
            return null;
        }

        var children = node.ValueKind switch
        {
            JsonValueKind.Object => node.EnumerateObject().Select(p => (p.Value, p.Name)),
            JsonValueKind.Array => node.EnumerateArray().Select((v, i) => (v, i.ToString(CultureInfo.InvariantCulture))),
            _ => [],
        };
        foreach (var (child, name) in children)
        {
            if (JwtIn(child, path.Length == 0 ? name : $"{path}.{name}", depth + 1) is { } found)
            {
                return found;
            }
        }

        return null;
    }

    /// <summary>The value at a.b.0.c, or null.</summary>
    public static JsonElement? At(JsonElement root, string field)
    {
        var node = root;
        foreach (var part in field.Split('.', StringSplitOptions.RemoveEmptyEntries))
        {
            if (node.ValueKind == JsonValueKind.Object && node.TryGetProperty(part, out var next))
            {
                node = next;
            }
            else if (node.ValueKind == JsonValueKind.Array && int.TryParse(part, out var i) && i >= 0 && i < node.GetArrayLength())
            {
                node = node[i];
            }
            else
            {
                return null;
            }
        }

        return node;
    }

    private static string? Text(JsonElement v) =>
        v.ValueKind == JsonValueKind.String ? v.GetString() : v.GetRawText();
}

/// <summary>Writes a <see cref="LoadScript"/> as a commented k6 script a person can read and change.</summary>
public static class K6ScriptWriter
{
    private static readonly JsonSerializerOptions Literal = new()
    {
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    public static string Write(LoadScript script, string systemName, DateTime? writtenAt = null)
    {
        var date = (writtenAt ?? DateTime.Now).ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);
        var rule = "// " + new string('=', 77);
        var sb = new StringBuilder();
        sb.AppendLine(rule)
            .AppendLine($"// Load test for {systemName.Replace('\n', ' ').Trim()}")
            .AppendLine($"// Written by the Microservice-Scripts setup app on {date}. Saving from the app")
            .AppendLine("// again replaces this file; the old one is kept as a dated .bak copy.")
            .AppendLine("//")
            .AppendLine("// Each iteration, a virtual user sends ONE of the requests below, picked at")
            .AppendLine("// random by its share, then pauses about THINK_TIME seconds. 0 sends the next")
            .AppendLine("// request at once, which measures capacity; a pause models people. The analyzer")
            .AppendLine("// sets the number of users and how long each level lasts; `options` only")
            .AppendLine("// applies when you run this by hand.")
            .AppendLine(rule)
            .AppendLine("import http from 'k6/http';")
            .AppendLine("import { check, sleep } from 'k6';")
            .AppendLine()
            .AppendLine($"const BASE_URL = (__ENV.BASE_URL || {Js(script.BaseUrl)}).replace(/\\/$/, '');")
            .AppendLine("const THINK_TIME = Number(__ENV.THINK_TIME || 0);")
            .AppendLine()
            .AppendLine("export const options = {")
            .AppendLine("  stages: [{ duration: '30s', target: 2 }],")
            .AppendLine("};")
            .AppendLine()
            .AppendLine("// name: how k6 reports it | share: how often it is picked, relative to the others")
            .AppendLine("const REQUESTS = [");
        foreach (var r in script.Requests)
        {
            sb.AppendLine($"  {{ name: {Js(r.Name)}, method: {Js(r.Method)}, path: {Js(r.Path)}, "
                          + $"body: {Body(r.Body)}, share: {r.Share.ToString(CultureInfo.InvariantCulture)} }},");
        }

        sb.AppendLine("];")
            .AppendLine()
            .AppendLine("const JSON_HEADERS = { 'Content-Type': 'application/json', Accept: 'application/json' };")
            .AppendLine("const TOTAL_SHARE = REQUESTS.reduce((sum, r) => sum + r.share, 0);")
            .AppendLine()
            .AppendLine("function url(path) {")
            .AppendLine("  return /^https?:\\/\\//i.test(path) ? path : BASE_URL + path;")
            .AppendLine("}")
            .AppendLine()
            .AppendLine("function pick() {")
            .AppendLine("  let x = Math.random() * TOTAL_SHARE;")
            .AppendLine("  for (const r of REQUESTS) {")
            .AppendLine("    x -= r.share;")
            .AppendLine("    if (x < 0) return r;")
            .AppendLine("  }")
            .AppendLine("  return REQUESTS[REQUESTS.length - 1];")
            .AppendLine("}")
            .AppendLine();

        switch (script.Auth)
        {
            case { Type: Auth.TypeLogin, Login: { } login } auth:
                WriteLogin(sb, auth, login);
                break;
            case { Type: Auth.TypeToken } auth:
                sb.AppendLine($"// Every request sends a token you already have. It comes from the {LoadScript.TokenVariable}")
                    .AppendLine("// environment variable, so it stays out of this file.")
                    .AppendLine("export function setup() {")
                    .AppendLine($"  const token = __ENV.{LoadScript.TokenVariable};")
                    .AppendLine("  if (!token) {")
                    .AppendLine($"    throw new Error('set {LoadScript.TokenVariable} to the token the requests send');")
                    .AppendLine("  }")
                    .AppendLine($"  return {{ headers: {{ [{Js(auth.HeaderName)}]: {Js(auth.HeaderPrefix)} + token }} }};")
                    .AppendLine("}");
                break;
            default:
                sb.AppendLine("// Fails at once, rather than after the whole test, if nothing answers.")
                    .AppendLine("export function setup() {")
                    .AppendLine("  const res = http.get(BASE_URL + '/', { tags: { name: 'reachable' } });")
                    .AppendLine("  if (res.status === 0) {")
                    .AppendLine("    throw new Error(`nothing answers at ${BASE_URL}: ${res.error}`);")
                    .AppendLine("  }")
                    .AppendLine("  return { headers: {} };")
                    .AppendLine("}");
                break;
        }

        sb.AppendLine()
            .AppendLine("export default function (data) {")
            .AppendLine("  const r = pick();")
            .AppendLine("  const headers = Object.assign({}, JSON_HEADERS, data.headers);")
            .AppendLine("  const res = http.request(r.method, url(r.path), r.body, { headers, tags: { name: r.name } });")
            .AppendLine("  check(res, { [`${r.name} answered 2xx/3xx`]: (x) => x.status >= 200 && x.status < 400 });")
            .AppendLine("  if (THINK_TIME > 0) {")
            .AppendLine("    sleep(THINK_TIME * (0.5 + Math.random()));")
            .AppendLine("  }")
            .AppendLine("}");
        return sb.ToString();
    }

    private static void WriteLogin(StringBuilder sb, Auth auth, LoginRequest login)
    {
        sb.AppendLine("// Logs in once; every request then carries the token from the login's response.")
            .AppendLine($"// {{{{password}}}} in the body is filled in from the {LoadScript.PasswordVariable} environment")
            .AppendLine("// variable, so the password stays out of this file.")
            .AppendLine($"const LOGIN = {{ method: {Js(login.Method)}, path: {Js(login.Path)}, body: {Body(login.Body)} }};")
            .AppendLine("// Where the response keeps the token: 'auto' looks in the usual places, 'body' means")
            .AppendLine("// the response body is the token itself, 'field' reads TOKEN_FIELD (a.b.0.c).")
            .AppendLine($"const TOKEN_FROM = {Js(login.TokenFrom)};")
            .AppendLine($"const TOKEN_FIELD = {(login.TokenField is null ? "null" : Js(login.TokenField))};")
            .AppendLine($"const TOKEN_NAMES = {JsonSerializer.Serialize(TokenFinder.Names)};")
            .AppendLine("const JWT = /^[\\w-]+\\.[\\w-]+\\.[\\w-]+$/;")
            .AppendLine()
            .AppendLine("function jwtIn(node, depth) {")
            .AppendLine("  if (typeof node === 'string') return JWT.test(node) ? node : null;")
            .AppendLine("  if (!node || typeof node !== 'object' || depth >= 3) return null;")
            .AppendLine("  for (const v of Object.values(node)) {")
            .AppendLine("    const found = jwtIn(v, depth + 1);")
            .AppendLine("    if (found) return found;")
            .AppendLine("  }")
            .AppendLine("  return null;")
            .AppendLine("}")
            .AppendLine()
            .AppendLine("function findToken(res) {")
            .AppendLine("  let json;")
            .AppendLine("  try {")
            .AppendLine("    json = res.json();")
            .AppendLine("  } catch (e) {")
            .AppendLine("    json = undefined;")
            .AppendLine("  }")
            .AppendLine("  if (TOKEN_FROM === 'field') return json === undefined ? null : res.json(TOKEN_FIELD);")
            .AppendLine("  if (typeof json === 'string') return json;")
            .AppendLine("  const text = String(res.body).trim().replace(/^\"|\"$/g, '');")
            .AppendLine("  if (TOKEN_FROM === 'body') return text;")
            .AppendLine("  if (json === undefined || json === null) return text.length >= 16 && !/\\s/.test(text) ? text : null;")
            .AppendLine("  const objects = [json, ...Object.values(json).filter((v) => v && typeof v === 'object' && !Array.isArray(v))];")
            .AppendLine("  for (const o of objects) {")
            .AppendLine("    for (const name of TOKEN_NAMES) {")
            .AppendLine("      const key = Object.keys(o).find((k) => k.toLowerCase() === name.toLowerCase());")
            .AppendLine("      if (key && typeof o[key] === 'string' && o[key]) return o[key];")
            .AppendLine("    }")
            .AppendLine("  }")
            .AppendLine("  return jwtIn(json, 0);")
            .AppendLine("}")
            .AppendLine()
            .AppendLine("export function setup() {")
            .AppendLine($"  const password = JSON.stringify(__ENV.{LoadScript.PasswordVariable} || '').slice(1, -1);")
            .AppendLine("  const body = LOGIN.body === null ? null : LOGIN.body.split('{{password}}').join(password);")
            .AppendLine("  const res = http.request(LOGIN.method, url(LOGIN.path), body, { headers: JSON_HEADERS, tags: { name: 'login' } });")
            .AppendLine("  if (res.status < 200 || res.status >= 300) {")
            .AppendLine("    throw new Error(`login failed: ${LOGIN.method} ${url(LOGIN.path)} answered ${res.status}: ${String(res.body).slice(0, 200)}`);")
            .AppendLine("  }")
            .AppendLine("  const token = findToken(res);")
            .AppendLine("  if (!token) {")
            .AppendLine("    throw new Error(`login answered ${res.status} but no token was found in its response: ${String(res.body).slice(0, 200)}`);")
            .AppendLine("  }")
            .AppendLine($"  return {{ headers: {{ [{Js(auth.HeaderName)}]: {Js(auth.HeaderPrefix)} + token }} }};")
            .AppendLine("}");
    }

    private static string Js(string value) => JsonSerializer.Serialize(value, Literal);

    // the body as one compact JSON string, or null
    private static string Body(string? body)
    {
        if (string.IsNullOrWhiteSpace(body))
        {
            return "null";
        }

        try
        {
            using var doc = JsonDocument.Parse(body.Replace(LoadScript.PasswordPlaceholder, "__password__", StringComparison.Ordinal));
            var compact = JsonSerializer.Serialize(doc.RootElement, Literal)
                .Replace("__password__", LoadScript.PasswordPlaceholder, StringComparison.Ordinal);
            return Js(compact);
        }
        catch (JsonException)
        {
            return Js(body);
        }
    }
}
