using MicroserviceScripts.Ui.Compose;
using MicroserviceScripts.Ui.Infrastructure;

namespace MicroserviceScripts.Ui.Tests;

public sealed class MonitoringTests : IDisposable
{
    private readonly string _root = Directory.CreateTempSubdirectory("ms-monitoring-").FullName;

    public MonitoringTests() => Directory.CreateDirectory(Path.Combine(_root, "observability"));

    public void Dispose() => Directory.Delete(_root, recursive: true);

    private Monitoring Monitoring() =>
        new(new RepoLocation(_root), new ProcessRunner(), new HttpClient { Timeout = TimeSpan.FromSeconds(2) });

    private void Existing(string env) => File.WriteAllText(Path.Combine(_root, "observability", ".env"), env);

    [Fact]
    public void Metrics_only_needs_no_key_and_keeps_everything_local()
    {
        var env = Monitoring().EnvText(new MonitoringSettings("shop_default", false, null, null, "shop"));

        Assert.Contains("APP_NETWORK=shop_default\n", env);
        Assert.Contains("OTEL_COLLECTOR_CONFIG=config.local.yaml\n", env);
        Assert.Contains("BEYLA_SERVICE_NAMESPACE=shop\n", env);
        Assert.DoesNotContain("SCRAPE_DIR", env);
        Assert.DoesNotContain("HONEYCOMB_API_KEY", env);   // no key needed, so none asked for
        Assert.Empty(Monitoring().Problems(new MonitoringSettings("shop_default", false, null, null, null)));
    }

    [Fact]
    public void The_key_and_other_lines_already_there_are_kept()
    {
        Existing("HONEYCOMB_API_KEY=hcaik_real\nAPP_NETWORK=old_net\nBEYLA_OPEN_PORT=9000-9100\n# a comment\n");

        var env = Monitoring().EnvText(new MonitoringSettings("shop_default", true, null, null, null));

        Assert.Contains("HONEYCOMB_API_KEY=hcaik_real\n", env);
        Assert.Contains("APP_NETWORK=shop_default\n", env);
        Assert.DoesNotContain("old_net", env);
        Assert.Contains("BEYLA_OPEN_PORT=9000-9100\n", env);
        Assert.DoesNotContain("OTEL_COLLECTOR_CONFIG", env);
        Assert.Empty(Monitoring().Problems(new MonitoringSettings("shop_default", true, null, null, null)));
    }

    [Fact]
    public void Traces_without_a_key_or_a_missing_scrape_folder_are_named()
    {
        var problems = Monitoring().Problems(new MonitoringSettings(" ", true, null, "../systems/nowhere", null));

        Assert.Contains("Pick the docker network your app runs on.", problems);
        Assert.Contains("Give a Honeycomb key for the traces, or keep everything on this machine.", problems);
        Assert.Contains(problems, p => p.StartsWith("There's no folder ../systems/nowhere", StringComparison.Ordinal));
    }

    [Fact]
    public void Writing_keeps_the_file_it_replaces()
    {
        Existing("APP_NETWORK=old_net\n");

        var backup = Monitoring().Write(new MonitoringSettings("shop_default", false, null, null, null));

        Assert.NotNull(backup);
        Assert.Equal("APP_NETWORK=old_net\n", File.ReadAllText(backup));
        Assert.Contains("APP_NETWORK=shop_default", File.ReadAllText(Path.Combine(_root, "observability", ".env")));
    }
}
