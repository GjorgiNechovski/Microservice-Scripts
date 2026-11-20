using System.Diagnostics;
using System.Text;
using System.Text.Json;

namespace MicroserviceScripts.Ui.Load;

/// <param name="Status">The HTTP status; null when nothing answered.</param>
/// <param name="Snippet">The start of the response body, to see what went wrong.</param>
public sealed record TryResult(
    string Name, string Method, string Url, int? Status, double Milliseconds, string? Snippet, string? Error)
{
    public bool Ok => Status is >= 200 and < 400;
}

/// <param name="Login">How the login went, when there is one.</param>
/// <param name="TokenFound">Whether the login's response held a token where the script looks for it.</param>
public sealed record TryResponse(TryResult? Login, bool? TokenFound, IReadOnlyList<TryResult> Requests);

/// <summary>The login request sent once, shown the way Postman shows a response.</summary>
/// <param name="Body">The response body (up to 20 000 characters).</param>
/// <param name="ContentType">The response's content type, to show the body the right way.</param>
/// <param name="Token">The token, found where the script will look for it; null when it wasn't.</param>
/// <param name="SetsCookie">The response set a cookie: a session, not a token.</param>
public sealed record LoginTry(TryResult Result, string? Body, string? ContentType, FoundToken? Token, bool SetsCookie);

/// <summary>
/// Sends requests the way the generated k6 script will: log in first (or use
/// the given token), then each request with it. One request per route, on the
/// user's say-so, to catch a wrong path or body before a run.
/// </summary>
public sealed class RouteTester(HttpClient http)
{
    private const int SnippetLength = 300;
    private const int BodyLength = 20_000;

    /// <summary>Sends the login request alone, to show its response and where the token is.</summary>
    public async Task<LoginTry> TryLoginAsync(string baseUrl, Auth auth, string? secret, CancellationToken ct)
    {
        var login = auth.Login ?? throw new ArgumentException("There's no login request.");
        var script = new LoadScript(baseUrl, auth, []);
        var body = login.Body?.Replace(LoadScript.PasswordPlaceholder,
            JsonSerializer.Serialize(secret ?? "")[1..^1], StringComparison.Ordinal);
        var sent = await SendAsync("login", login.Method, script.Url(login.Path), body, new(), ct);
        var token = sent.Result.Ok ? TokenFinder.Find(sent.Answer, login.TokenFrom, login.TokenField) : null;
        return new LoginTry(
            sent.Result,
            sent.Answer.Length == 0 ? null : sent.Answer.Length <= BodyLength ? sent.Answer : sent.Answer[..BodyLength] + "…",
            sent.ContentType,
            token,
            sent.SetsCookie);
    }

    public async Task<TryResponse> TryAsync(LoadScript script, string? secret, CancellationToken ct)
    {
        var headers = new Dictionary<string, string>();
        TryResult? login = null;
        bool? tokenFound = null;
        if (script.Auth is { Type: Auth.TypeLogin } auth)
        {
            var tried = await TryLoginAsync(script.BaseUrl, auth, secret, ct);
            login = tried.Result;
            tokenFound = tried.Token is not null;
            if (tried.Token is { } token)
            {
                headers[auth.HeaderName] = auth.HeaderPrefix + token.Value;
            }
        }
        else if (script.Auth is { Type: Auth.TypeToken } given && !string.IsNullOrEmpty(secret))
        {
            headers[given.HeaderName] = given.HeaderPrefix + secret;
        }

        var results = new List<TryResult>();
        foreach (var r in script.Requests)
        {
            results.Add((await SendAsync(r.Name, r.Method, script.Url(r.Path), r.Body, headers, ct)).Result);
        }

        return new TryResponse(login, tokenFound, results);
    }

    private sealed record Sent(TryResult Result, string Answer, string? ContentType, bool SetsCookie);

    private async Task<Sent> SendAsync(
        string name, string method, string url, string? body, Dictionary<string, string> headers, CancellationToken ct)
    {
        var watch = Stopwatch.StartNew();
        try
        {
            using var request = new HttpRequestMessage(new HttpMethod(method), url);
            request.Headers.TryAddWithoutValidation("Accept", "application/json");
            foreach (var (key, value) in headers)
            {
                request.Headers.TryAddWithoutValidation(key, value);
            }

            if (!string.IsNullOrWhiteSpace(body))
            {
                request.Content = new StringContent(body, Encoding.UTF8, "application/json");
            }

            using var response = await http.SendAsync(request, ct);
            var answer = await response.Content.ReadAsStringAsync(ct);
            return new Sent(
                new TryResult(name, method, url, (int)response.StatusCode, watch.Elapsed.TotalMilliseconds,
                    Snippet(answer), null),
                answer,
                response.Content.Headers.ContentType?.MediaType,
                response.Headers.Contains("Set-Cookie"));
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or UriFormatException
                                       or InvalidOperationException)
        {
            var reason = ex is TaskCanceledException && !ct.IsCancellationRequested
                ? "no answer within 15 seconds"
                : ex.InnerException?.Message ?? ex.Message;
            return new Sent(new TryResult(name, method, url, null, watch.Elapsed.TotalMilliseconds, null, reason),
                "", null, false);
        }
    }

    private static string? Snippet(string answer) =>
        answer.Length == 0 ? null : answer.Length <= SnippetLength ? answer : answer[..SnippetLength] + "…";
}
