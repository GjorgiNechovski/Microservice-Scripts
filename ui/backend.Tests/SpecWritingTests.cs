using MicroserviceScripts.Ui.Drafting;
using MicroserviceScripts.Ui.Spec;

namespace MicroserviceScripts.Ui.Tests;

public sealed class SpecWritingTests : IDisposable
{
    private readonly string _dir = Directory.CreateTempSubdirectory("ms-spec-").FullName;

    public void Dispose() => Directory.Delete(_dir, recursive: true);

    /// <summary>The spec the page would send for a draft, with the user's renames applied.</summary>
    private static SpecDocument FromDraft(string name, DraftResult draft, Dictionary<string, string>? renames = null)
    {
        SpecComponent ToSpec(DraftComponent c) => new(
            renames?.GetValueOrDefault(c.Name) ?? c.Name, c.Project, c.ComposeService,
            c.ReadinessPort, c.ReadinessSource, c.HasHealthcheck, c.Instances);
        return new SpecDocument(
            name,
            draft.ProjectServices.Keys.ToList(),
            draft.Components.Where(c => c.Kind == ComponentKind.Service).Select(ToSpec).ToList(),
            draft.Components.Where(c => c.Kind == ComponentKind.Infrastructure).Select(ToSpec).ToList(),
            draft.ProjectServices);
    }

    [Fact]
    public async Task Finki_draft_makes_a_spec_the_analyzer_accepts()
    {
        var spec = FromDraft("FINKI blogging platform", Fixtures.Draft(Fixtures.Project("finki")),
            new() { ["api-gateway"] = "kong" });
        var toml = TomlSpecWriter.Write(spec, new DateTime(2026, 11, 19));

        Assert.Contains("compose_project = \"finki-blogger-2\"", toml);
        Assert.Contains("[containers]\nkong = \"api-gateway\"\n", toml);
        Assert.Contains("kong = 2\n", toml);
        Assert.Contains("user-service = 8080", toml);
        Assert.DoesNotContain("blogging-db = 5432", toml);   // it has a healthcheck
        Assert.Contains("# none yet", toml);

        var check = await Fixtures.PythonCheckAsync(toml);
        Assert.True(check is null || check.Ok, check?.Error);
    }

    [Fact]
    public async Task Projects_sharing_a_service_name_get_pinned()
    {
        var spec = new SpecDocument(
            "Shop",
            ["shop-catalog", "shop-orders"],
            [new("catalog", "shop-catalog", "catalog", 8000), new("orders", "shop-orders", "orders", 8000)],
            [new("catalog-db", "shop-catalog", "db", 5432), new("orders-db", "shop-orders", "db", 5432)],
            new Dictionary<string, IReadOnlyList<string>>
            {
                ["shop-catalog"] = ["catalog", "db"],
                ["shop-orders"] = ["orders", "db"],
            });
        var toml = TomlSpecWriter.Write(spec);

        Assert.Contains("compose_project = [\n    \"shop-catalog\",\n    \"shop-orders\",\n]", toml);
        Assert.Contains("catalog-db = { project = \"shop-catalog\", service = \"db\" }", toml);
        Assert.Contains("orders-db = { project = \"shop-orders\", service = \"db\" }", toml);
        Assert.DoesNotContain("catalog = {", toml);   // unique name: no pin needed

        var check = await Fixtures.PythonCheckAsync(toml);
        Assert.True(check is null || check.Ok, check?.Error);
    }

    [Fact]
    public void Problems_are_found_before_anything_is_written()
    {
        var spec = new SpecDocument("", ["p"], [], [new("bad name", "p", "x", 70000), new("bad name", "p", "y")]);
        var problems = spec.Problems();
        Assert.Contains(problems, p => p.Contains("name"));
        Assert.Contains(problems, p => p.Contains("at least one service"));
        Assert.Contains(problems, p => p.Contains("70000"));
        Assert.Contains(problems, p => p.Contains("Two components"));
    }

    [Fact]
    public void Strings_are_escaped()
    {
        var toml = TomlSpecWriter.Write(new SpecDocument("Shop \"v2\" \\ test", ["p"], [new("api", "p", "api")], []));
        Assert.Contains("name = \"Shop \\\"v2\\\" \\\\ test\"", toml);
    }

    [Fact]
    public void Saving_over_a_spec_keeps_the_old_one()
    {
        var path = SpecStore.Resolve(_dir, "shop");
        Assert.EndsWith("shop.toml", path);
        Assert.Null(SpecStore.Write(path, "one").Backup);
        var second = SpecStore.Write(path, "two", new DateTime(2026, 11, 19, 15, 30, 0));
        Assert.Equal("two", File.ReadAllText(path));
        Assert.Equal("one", File.ReadAllText(second.Backup!));
        Assert.Throws<ArgumentException>(() => SpecStore.Resolve(_dir, "../escape.toml"));
    }
}
