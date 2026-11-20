using MicroserviceScripts.Ui.Drafting;

namespace MicroserviceScripts.Ui.Tests;

public class ImageCatalogTests
{
    private static readonly ImageCatalog Catalog = ImageCatalog.LoadDefault();

    [Theory]
    [InlineData("postgres:16", "PostgreSQL")]
    [InlineData("docker.io/bitnami/postgresql:16", "PostgreSQL")]
    [InlineData("bitnamilegacy/postgresql-repmgr:17.6.0", "PostgreSQL")]
    [InlineData("confluentinc/cp-kafka:7.6.0", "Kafka")]
    [InlineData("rabbitmq:3-management", "RabbitMQ")]
    [InlineData("redis:7-alpine", "Redis")]
    [InlineData("mcr.microsoft.com/mssql/server:2022-latest", "SQL Server")]
    [InlineData("kong:latest", "Kong API gateway")]
    [InlineData("nginx@sha256:0123abcd", "nginx")]
    [InlineData("localhost:5000/team/mongo:7", "MongoDB")]
    public void Knows_infrastructure_whatever_the_registry_tag_or_variant(string image, string label)
    {
        var entry = Catalog.Match(image);
        Assert.NotNull(entry);
        Assert.Equal(label, entry.Label);
        Assert.False(entry.NotModelled);
    }

    [Theory]
    [InlineData("rediscommander/redis-commander", "an admin UI")]
    [InlineData("prometheuscommunity/postgres-exporter", "a metrics exporter")]
    [InlineData("grafana/k6", "a load generator")]
    [InlineData("otel/opentelemetry-collector-contrib:latest", "a monitoring tool")]
    public void Tools_around_the_system_are_not_modelled(string image, string label)
    {
        var entry = Catalog.Match(image);
        Assert.NotNull(entry);
        Assert.Equal(label, entry.Label);
        Assert.True(entry.NotModelled);
    }

    [Theory]
    [InlineData("myorg/billing-service:1.4")]
    [InlineData("acme/vectorizer-app")]
    [InlineData(null)]
    public void Unknown_images_are_not_guessed(string? image) => Assert.Null(Catalog.Match(image));

    [Fact]
    public void Matches_whole_words_only()
    {
        Assert.True(ImageCatalog.WordMatch("cp-kafka", "kafka"));
        Assert.True(ImageCatalog.WordMatch("postgresql-repmgr", "postgresql"));
        Assert.False(ImageCatalog.WordMatch("pgvector", "vector"));
        Assert.False(ImageCatalog.WordMatch("redisson", "redis"));
    }
}
