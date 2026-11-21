using MicroserviceScripts.Ui.Compose;

namespace MicroserviceScripts.Ui.Tests;

public sealed class ScanningTests : IDisposable
{
    private readonly string _root = Directory.CreateTempSubdirectory("ms-scan-").FullName;

    public void Dispose() => Directory.Delete(_root, recursive: true);

    private void Touch(string relative)
    {
        var path = Path.Combine(_root, relative);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.WriteAllText(path, "");
    }

    [Theory]
    [InlineData("compose.yaml", ComposeFileRole.Base)]
    [InlineData("docker-compose.yml", ComposeFileRole.Base)]
    [InlineData("docker-compose.override.yml", ComposeFileRole.Override)]
    [InlineData("docker-compose.otel.yml", ComposeFileRole.Overlay)]
    [InlineData("telemetry.compose.yaml", ComposeFileRole.Overlay)]
    [InlineData("compose.metrics-only.yml", ComposeFileRole.Overlay)]
    [InlineData("values.yaml", null)]
    [InlineData("README.md", null)]
    public void Recognises_compose_files_by_name(string name, ComposeFileRole? role) =>
        Assert.Equal(role, ComposeFiles.RoleOf(name));

    [Fact]
    public void Finds_one_project_per_repository_and_skips_dependency_folders()
    {
        // an app split over two repositories, under one parent folder
        Touch("catalog-repo/compose.yaml");
        Touch("catalog-repo/compose.override.yaml");
        Touch("catalog-repo/docker-compose.otel.yml");
        Touch("catalog-repo/src/Dockerfile");
        Touch("orders-repo/deploy/docker-compose.yml");
        Touch("orders-repo/node_modules/some-lib/docker-compose.yml");
        Touch("orders-repo/.git/compose.yaml");

        var result = FolderScanner.Scan([_root]);

        Assert.Equal(2, result.Projects.Count);
        var catalog = result.Projects.Single(p => p.Directory.EndsWith("catalog-repo"));
        Assert.Equal(["compose.yaml", "compose.override.yaml", "docker-compose.otel.yml"],
            catalog.Files.Select(f => f.Name));
        // what `docker compose up` in that folder would read is chosen; extra overlays aren't
        Assert.Equal([true, true, false], catalog.Files.Select(f => f.Selected));
        Assert.EndsWith(Path.Combine("orders-repo", "deploy"), result.Projects.Single(p => p != catalog).Directory);
        Assert.Single(result.Dockerfiles);
        Assert.Empty(result.Warnings);
    }

    [Fact]
    public void A_file_pointed_at_directly_is_used_whatever_its_name()
    {
        Touch("legacy/stack.yml");
        var result = FolderScanner.Scan([], [Path.Combine(_root, "legacy", "stack.yml"), Path.Combine(_root, "nope.yml")]);

        var file = Assert.Single(Assert.Single(result.Projects).Files);
        Assert.Equal(("stack.yml", ComposeFileRole.Base, true), (file.Name, file.Role, file.Selected));
        Assert.Contains(result.Warnings, w => w.Contains("nope.yml"));
    }

    [Fact]
    public void A_project_docker_knows_keeps_its_name_and_its_files_together()
    {
        // started as: docker compose -p shop -f shop-app/compose.yaml -f ops/monitoring.yml up -d
        Touch("shop-app/compose.yaml");
        Touch("ops/monitoring.yml");
        var result = FolderScanner.Scan([], [],
            [new KnownProject("shop", [Path.Combine(_root, "shop-app", "compose.yaml"), Path.Combine(_root, "ops", "monitoring.yml")])]);

        var project = Assert.Single(result.Projects);
        Assert.Equal("shop", project.Name);
        Assert.EndsWith("shop-app", project.Directory);
        Assert.Equal(["compose.yaml", "monitoring.yml"], project.Files.Select(f => f.Name));
        Assert.All(project.Files, f => Assert.True(f.Selected));
    }
}
