using System.Net;
using System.Text;
using MicroserviceScripts.Ui.Infrastructure;
using MicroserviceScripts.Ui.Load;
using MicroserviceScripts.Ui.Spec;

namespace MicroserviceScripts.Ui.Tests;

public class LoadTests
{
    private static readonly Auth Login = new(
        Auth.TypeLogin, "Authorization", "Bearer ",
        new LoginRequest("POST", "/user/Authentication/login", """{ "email": "test@example.com", "password": "{{password}}" }"""));

    private static LoadScript Finki(Auth? login = null) => new(
        "http://localhost:8000",
        login,
        [
            new LoadRequest("GET /blog/blogs", "GET", "/blog/blogs?skip=0&limit=20", null, 3),
            new LoadRequest("POST /blog/create", "POST", "/blog/create", """{"title": "Микросервиси", "blog_text": "k6"}""", 1),
        ]);

    [Fact]
    public async Task The_script_logs_in_once_and_picks_requests_by_share()
    {
        var js = K6ScriptWriter.Write(Finki(Login), "FINKI blogging platform", new DateTime(2026, 11, 19));
        // a JSON content type only with a body (as Try sends it), and the token looked for where Send looks
        Assert.Contains("r.body === null ? ACCEPT_JSON : JSON_HEADERS", js);
        Assert.Contains("Array.isArray(json) ? []", js);

        Assert.Contains("const BASE_URL = (__ENV.BASE_URL || \"http://localhost:8000\")", js);
        Assert.Contains("{ name: \"GET /blog/blogs\", method: \"GET\", path: \"/blog/blogs?skip=0&limit=20\", body: null, share: 3 },", js);
        // bodies go in compact, readable, with the password left to the environment
        Assert.Contains("body: \"{\\\"title\\\":\\\"Микросервиси\\\",\\\"blog_text\\\":\\\"k6\\\"}\"", js);
        Assert.Contains("\\\"password\\\":\\\"{{password}}\\\"", js);
        Assert.Contains("__ENV.LOGIN_PASSWORD", js);
        Assert.Contains("const TOKEN_FROM = \"auto\";", js);
        Assert.Contains("[\"Authorization\"]: \"Bearer \" + token", js);
        Assert.DoesNotContain("Test123", js);

        await AssertValidJavaScript(js);
        var field = Login with { Login = Login.Login! with { TokenFrom = "field", TokenField = "data.token" } };
        await AssertValidJavaScript(K6ScriptWriter.Write(Finki(field), "x"));
        var apiKey = new Auth(Auth.TypeToken, "X-API-Key", "");
        var keyed = K6ScriptWriter.Write(Finki(apiKey), "api key");
        Assert.Contains("__ENV.AUTH_TOKEN", keyed);
        Assert.Contains("[\"X-API-Key\"]: \"\" + token", keyed);
        await AssertValidJavaScript(keyed);
        await AssertValidJavaScript(K6ScriptWriter.Write(Finki(), "no login"));
    }

    [Fact]
    public void Unfinished_requests_are_named()
    {
        var script = new LoadScript("localhost:8000", Login with { Login = Login.Login! with { Body = "{ not json" } },
        [
            new LoadRequest("GET /blog/blogs/{blog_id}", "GET", "/blog/blogs/{blog_id}", null, 1),
            new LoadRequest("GET /books/:id", "GET", "/books/:id", null, 0),
            new LoadRequest("FETCH /x", "FETCH", "x", null, 1),
        ]);

        var problems = script.Problems();

        Assert.Contains("'localhost:8000' isn't an http:// address.", problems);
        Assert.Contains("GET /blog/blogs/{blog_id}: put a real value in place of {blog_id} in /blog/blogs/{blog_id}.", problems);
        Assert.Contains("GET /books/:id: put a real value in place of :id in /books/:id.", problems);
        Assert.Contains("GET /books/:id: its share must be more than 0.", problems);
        Assert.Contains(problems, p => p.StartsWith("FETCH /x: 'FETCH' isn't one of", StringComparison.Ordinal));
        Assert.Contains("FETCH /x: the path must start with / (or be a full http:// address).", problems);
        Assert.Contains("The login: its body isn't valid JSON.", problems);
        Assert.Empty(Finki(Login).Problems());
    }

    [Theory]
    [InlineData("\"eyJhbGciOi.payload.sig\"", "eyJhbGciOi.payload.sig", null)]   // FINKI: the response body is the token
    [InlineData("{\"access_token\": \"abc\", \"token_type\": \"bearer\"}", "abc", "access_token")]
    [InlineData("{\"data\": {\"accessToken\": \"xyz\"}, \"ok\": true}", "xyz", "data.accessToken")]
    [InlineData("{\"user\": {\"id\": 1}, \"session\": [\"aaa.bbb.ccc\"]}", "aaa.bbb.ccc", "session.0")]
    [InlineData("{\"message\": \"welcome\"}", null, null)]
    [InlineData("OK", null, null)]
    [InlineData("0123456789abcdef0123", "0123456789abcdef0123", null)]          // a bare opaque token
    public void The_token_is_looked_for_in_the_usual_places(string body, string? token, string? field)
    {
        var found = TokenFinder.Find(body, "auto", null);
        Assert.Equal(token, found?.Value);
        Assert.Equal(field, found?.Field);
    }

    [Fact]
    public void A_token_the_user_pointed_at_is_read_from_there()
    {
        Assert.Equal("t", TokenFinder.Find("{\"result\": [{\"key\": \"t\"}]}", "field", "result.0.key")?.Value);
        Assert.Null(TokenFinder.Find("{\"result\": []}", "field", "result.0.key"));
        Assert.Equal("{\"a\": 1}", TokenFinder.Find("{\"a\": 1}", "body", null)?.Value);
    }

    [Fact]
    public async Task Trying_the_routes_logs_in_then_sends_each_with_the_token()
    {
        var seen = new List<string>();
        var http = new HttpClient(new Answering(request =>
        {
            seen.Add($"{request.Method} {request.RequestUri} {request.Headers.Authorization}");
            var body = request.Content?.ReadAsStringAsync().Result;
            return request.RequestUri!.AbsolutePath switch
            {
                "/user/Authentication/login" when body!.Contains("\"s3cret\"", StringComparison.Ordinal) =>
                    (HttpStatusCode.OK, "\"the-token\""),
                "/user/Authentication/login" => (HttpStatusCode.BadRequest, "wrong password"),
                "/blog/create" => (HttpStatusCode.UnprocessableEntity, "{\"detail\": \"title is required\"}"),
                _ => (HttpStatusCode.OK, "[]"),
            };
        }));

        var result = await new RouteTester(http).TryAsync(Finki(Login), "s3cret", default);
        var login = await new RouteTester(http).TryLoginAsync("http://localhost:8000", Login, "s3cret", default);
        Assert.Equal(new FoundToken("the-token", null), login.Token);
        Assert.Equal("\"the-token\"", login.Body);

        Assert.True(result.TokenFound);
        Assert.Equal(200, result.Login!.Status);
        Assert.Equal(
        [
            "POST http://localhost:8000/user/Authentication/login ",
            "GET http://localhost:8000/blog/blogs?skip=0&limit=20 Bearer the-token",
            "POST http://localhost:8000/blog/create Bearer the-token",
            "POST http://localhost:8000/user/Authentication/login ",
        ], seen);
        Assert.True(result.Requests[0].Ok);
        Assert.Equal(422, result.Requests[1].Status);
        Assert.Contains("title is required", result.Requests[1].Snippet);

        var wrong = await new RouteTester(http).TryAsync(Finki(Login), "guess", default);
        Assert.False(wrong.TokenFound);
        Assert.Equal("wrong password", wrong.Login!.Snippet);

        // a token the user already has goes straight into the header
        seen.Clear();
        await new RouteTester(http).TryAsync(Finki(new Auth(Auth.TypeToken, "Authorization", "Bearer ")), "abc", default);
        Assert.All(seen, s => Assert.EndsWith("Bearer abc", s, StringComparison.Ordinal));
    }

    [Fact]
    public async Task The_load_section_is_one_the_analyzer_reads()
    {
        var spec = new SpecDocument(
            "Shop", ["shop"],
            [new("api", "shop", "api", 8000)],
            [new("db", "shop", "db", 5432)],
            Operations: new Dictionary<string, IReadOnlyList<string>> { ["browse"] = ["api", "db"] },
            Load: new SpecLoad("load.js", [1, 2, 5], "2m",
                new Dictionary<string, string> { ["BASE_URL"] = "http://localhost:9000", ["THINK_TIME"] = "0" },
                [LoadScript.PasswordVariable]));

        var toml = TomlSpecWriter.Write(spec, dockerDesktop: false);

        Assert.Contains("[load]\nscript = \"load.js\"\nvus = [1, 2, 5]\nduration = \"2m\"\n", toml);
        Assert.Contains("env = { BASE_URL = \"http://localhost:9000\", THINK_TIME = \"0\" }", toml);
        Assert.Contains("env_passthrough = [\"LOGIN_PASSWORD\"]", toml);
        Assert.Contains("browse = [\"api\", \"db\"]", toml);
        Assert.Contains("vus = 5\n", TomlSpecWriter.Write(spec with { Load = spec.Load! with { Users = [5] } }));
        var check = await Fixtures.PythonCheckAsync(toml);
        Assert.True(check is null || check.Ok, check?.Error);
    }

    [Theory]
    [InlineData("Docker Desktop|docker-desktop", true, true)]            // Docker Desktop for Linux too
    [InlineData("OrbStack|orbstack", false, true)]
    [InlineData("Ubuntu 24.04.1 LTS|colima", false, true)]
    [InlineData("Alpine Linux v3.20|lima-rancher-desktop", false, true)]
    [InlineData("Ubuntu 24.04.1 LTS|climate-lab", true, false)]           // a host merely containing "lima"
    [InlineData("Fedora Linux 44 (KDE Plasma)|wpops-laptop", true, false)]
    [InlineData("Some engine|some-host", false, true)]                    // off Linux, Docker always runs in a VM
    public void Docker_in_a_VM_is_told_from_docker_info(string described, bool linux, bool expected) =>
        Assert.Equal(expected, MicroserviceScripts.Ui.Compose.ComposeCli.InVm(described, linux));

    [Fact]
    public async Task On_Docker_Desktop_k6_reaches_the_app_through_the_host()
    {
        var spec = new SpecDocument(
            "Shop", ["shop"], [new("api", "shop", "api", 8000)], [],
            Operations: new Dictionary<string, IReadOnlyList<string>> { ["browse"] = ["api"] },
            Load: new SpecLoad("load.js", [5], "1m",
                new Dictionary<string, string> { ["BASE_URL"] = "http://localhost:9000", ["THINK_TIME"] = "0" }));

        var toml = TomlSpecWriter.Write(spec, dockerDesktop: true);

        Assert.Contains("docker_network = \"bridge\"\n", toml);
        Assert.Contains("env = { BASE_URL = \"http://host.docker.internal:9000\", THINK_TIME = \"0\" }", toml);
        Assert.Equal("http://host.docker.internal:8080/api/", TomlSpecWriter.FromAContainer("http://127.0.0.1:8080/api/"));
        Assert.Equal("https://shop.example.com", TomlSpecWriter.FromAContainer("https://shop.example.com"));
        Assert.DoesNotContain("docker_network", TomlSpecWriter.Write(spec, dockerDesktop: false));
        var check = await Fixtures.PythonCheckAsync(toml);
        Assert.True(check is null || check.Ok, check?.Error);
    }

    [Fact]
    public void Features_and_load_settings_are_checked_before_writing()
    {
        var spec = new SpecDocument(
            "Shop", ["shop"], [new("api", "shop", "api", 8000)], [],
            Operations: new Dictionary<string, IReadOnlyList<string>> { ["browse"] = ["api", "cache"], ["empty"] = [] },
            Load: new SpecLoad("load.js", [0], "two minutes"));

        var problems = spec.Problems();

        Assert.Contains("Feature 'browse' needs 'cache', which isn't a service or infrastructure.", problems);
        Assert.Contains("Feature 'empty': pick the components it needs.", problems);
        Assert.Contains("Give at least one number of users, each between 1 and 100000.", problems);
        // durations the analyzer can read back: hours, minutes, seconds, in that order
        foreach (var odd in new[] { "500ms", "30s1m", "2" })
        {
            Assert.Contains((spec with { Load = spec.Load! with { Duration = odd } }).Problems(),
                p => p.Contains("isn't a duration", StringComparison.Ordinal));
        }

        Assert.DoesNotContain((spec with { Load = spec.Load! with { Users = [5], Duration = "1h30m" } }).Problems(),
            p => p.Contains("isn't a duration", StringComparison.Ordinal));
        Assert.Contains(problems, p => p.StartsWith("'two minutes' isn't a duration", StringComparison.Ordinal));
    }

    /// <summary>Parses the script with node, where it is installed: k6 runs modern JavaScript.</summary>
    private static async Task AssertValidJavaScript(string js)
    {
        var file = Path.Combine(Path.GetTempPath(), $"k6-{Guid.NewGuid():N}.mjs");
        await File.WriteAllTextAsync(file, js);
        try
        {
            var result = await new ProcessRunner().RunAsync("node", ["--check", file], timeout: TimeSpan.FromSeconds(30));
            Assert.True(result.NotFound || result.Ok, result.Error);
        }
        finally
        {
            File.Delete(file);
        }
    }

    private sealed class Answering(Func<HttpRequestMessage, (HttpStatusCode Status, string Body)> answer) : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            var (status, body) = answer(request);
            return Task.FromResult(new HttpResponseMessage(status) { Content = new StringContent(body, Encoding.UTF8) });
        }
    }
}
