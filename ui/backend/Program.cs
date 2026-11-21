using System.Net;
using System.Text.Json;
using System.Text.Json.Serialization;
using MicroserviceScripts.Ui;
using MicroserviceScripts.Ui.Api;
using MicroserviceScripts.Ui.Compose;
using MicroserviceScripts.Ui.Drafting;
using MicroserviceScripts.Ui.Infrastructure;
using MicroserviceScripts.Ui.Load;
using MicroserviceScripts.Ui.Routes;
using MicroserviceScripts.Ui.Run;
using MicroserviceScripts.Ui.Setup;
using MicroserviceScripts.Ui.Spec;

var options = LaunchOptions.Parse(args);
var repo = RepoLocation.Find();
Directory.CreateDirectory(repo.WebRoot);

// Paths come from the clone itself, not the shell's current folder, so the app
// works however it was started.
var builder = WebApplication.CreateBuilder(new WebApplicationOptions
{
    ContentRootPath = repo.BackendDirectory,
    WebRootPath = repo.WebRoot,
});
builder.WebHost.ConfigureKestrel(kestrel => kestrel.Listen(IPAddress.Loopback, options.Port));
builder.Services.ConfigureHttpJsonOptions(json =>
    json.SerializerOptions.Converters.Add(new JsonStringEnumConverter(JsonNamingPolicy.CamelCase)));

builder.Services.AddSingleton(options);
builder.Services.AddSingleton(repo);
builder.Services.AddSingleton<IProcessRunner, ProcessRunner>();
builder.Services.AddSingleton<PythonLocator>();
builder.Services.AddSingleton<Prerequisites>();
builder.Services.AddSingleton<ComposeCli>();
builder.Services.AddSingleton<SpecChecker>();
var routeCatalog = RouteCatalog.LoadDefault();
builder.Services.AddSingleton(_ => new ComponentDrafter(ImageCatalog.LoadDefault(), DockerfileFacts.Read,
    context => routeCatalog.LanguageOf(context).Language));
builder.Services.AddSingleton(_ => new RouteDrafter(routeCatalog, DockerfileFacts.Read, RequestShapes.LoadDefault()));
// No cookie jar: a session cookie from one login try would otherwise ride along on every later Try,
// which then passes while the load test (whose users don't share setup()'s cookies) gets 401s.
builder.Services.AddSingleton(_ => new HttpClient(new SocketsHttpHandler { UseCookies = false })
{
    Timeout = TimeSpan.FromSeconds(15),
});
builder.Services.AddSingleton<RouteTester>();
builder.Services.AddSingleton<AppControl>();
builder.Services.AddSingleton<AppStarter>();
builder.Services.AddSingleton<Monitoring>();
builder.Services.AddSingleton<MonitoringStarter>();
builder.Services.AddSingleton<Preflight>();
builder.Services.AddSingleton<RunManager>();

var app = builder.Build();
app.UseMiddleware<LocalOnlyMiddleware>();
app.UseDefaultFiles();
app.UseStaticFiles();
app.MapSetupApi();
app.MapFallbackToFile("index.html");

app.Lifetime.ApplicationStarted.Register(() =>
{
    Console.WriteLine($"Microservice-Scripts setup is running at {options.Url}");
    if (!File.Exists(Path.Combine(repo.WebRoot, "index.html")))
    {
        Console.WriteLine("The page isn't built yet: start the app with start.sh (or start.ps1) in the repository's root.");
    }

    if (options.OpenBrowser)
    {
        BrowserLauncher.TryOpen(options.Url);
    }
});

app.Run();
