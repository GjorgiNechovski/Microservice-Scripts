using MicroserviceScripts.Ui.Compose;
using MicroserviceScripts.Ui.Drafting;

namespace MicroserviceScripts.Ui.Tests;

public class ComponentDrafterTests
{
    private static DraftComponent Get(DraftResult draft, string name) =>
        draft.Components.Single(c => c.Name == name);

    private static string[] Names(DraftResult draft, ComponentKind kind) =>
        draft.Components.Where(c => c.Kind == kind).Select(c => c.Name).Order().ToArray();

    [Fact]
    public void Finki_comes_out_as_its_hand_written_spec()
    {
        // systems/finki-blogger.toml was written by hand from these 22 compose services
        var draft = Fixtures.Draft(Fixtures.Project("finki"));

        Assert.Equal(["blog-service", "comment-service", "email-service", "like-service", "user-service"],
            Names(draft, ComponentKind.Service));
        Assert.Equal(["api-gateway", "blogging-db", "rabbitmq", "user-db", "zookeeper"],
            Names(draft, ComponentKind.Infrastructure));
        Assert.Equal(["frontend"], Names(draft, ComponentKind.NotModelled));
        Assert.Empty(Names(draft, ComponentKind.Unsure));

        // replicas, poolers and per-service load balancers fold into what they serve
        Assert.Equal(["finki-blogger-2/blogging-db-2", "finki-blogger-2/blogging-db-pool"],
            Get(draft, "blogging-db").Folded.Select(f => f.Id).Order());
        Assert.Equal("finki-blogger-2/gateway-lb", Assert.Single(Get(draft, "api-gateway").Folded).Id);
        Assert.Equal("in front", Assert.Single(Get(draft, "blog-service").Folded).Role);

        // [instances] of the hand-written spec
        Assert.Equal(2, Get(draft, "api-gateway").Instances);
        Assert.Equal(2, Get(draft, "blogging-db").Instances);
        Assert.Equal(2, Get(draft, "user-db").Instances);
        Assert.Equal(2, Get(draft, "rabbitmq").Instances);
        Assert.Equal(1, Get(draft, "zookeeper").Instances);

        // [readiness] of the hand-written spec, each with where it came from
        Assert.Equal(8080, Get(draft, "user-service").ReadinessPort);
        Assert.Equal(".NET 8 listens on 8080 by default", Get(draft, "user-service").ReadinessSource);
        Assert.Equal(8000, Get(draft, "blog-service").ReadinessPort);
        Assert.Equal(8000, Get(draft, "like-service").ReadinessPort);
        Assert.Equal(8080, Get(draft, "email-service").ReadinessPort);
        Assert.Equal(2181, Get(draft, "zookeeper").ReadinessPort);

        // a pool now stands for its database in what a service depends on
        Assert.Equal(["finki-blogger-2/blogging-db", "finki-blogger-2/zookeeper"],
            Get(draft, "blog-service").DependsOn.Select(d => d.Id).Order());
        Assert.Equal("finki-blogger-backend", draft.Networks[0].Name);
    }

    [Fact]
    public void Bookshop_keeps_a_gateway_that_fronts_several_services()
    {
        var draft = Fixtures.Draft(Fixtures.Project("bookshop"));

        Assert.Equal(["catalog", "orders"], Names(draft, ComponentKind.Service));
        Assert.Equal(["gateway", "redis"], Names(draft, ComponentKind.Infrastructure));
        Assert.Empty(Get(draft, "gateway").Folded);
        Assert.Equal(8000, Get(draft, "catalog").ReadinessPort);
        Assert.Equal(6379, Get(draft, "redis").ReadinessPort);

        // orders finds catalog through CATALOG_URL as well as depends_on
        Assert.Contains(Get(draft, "orders").DependsOn, d => d.Id == "bookshop-pristine/catalog");
    }

    [Fact]
    public void What_it_cannot_place_is_left_to_the_user()
    {
        var project = Inline("app", """
            {
              "name": "app",
              "services": {
                "billing": { "image": "myorg/billing:1.4" },
                "prometheus": { "image": "prom/prometheus" },
                "db-migrate": { "image": "postgres:16", "command": ["psql", "-f", "init.sql"] },
                "api-gateway": { "build": { "context": "/nowhere/gw" } },
                "web": { "build": { "context": "/nowhere/web" } }
              }
            }
            """);
        var draft = Fixtures.Draft(project);

        Assert.Equal(ComponentKind.Unsure, Get(draft, "billing").Kind);
        Assert.Contains("isn't in the catalogue", Get(draft, "billing").Reasons[0]);
        Assert.Equal(ComponentKind.NotModelled, Get(draft, "prometheus").Kind);
        Assert.Equal(ComponentKind.NotModelled, Get(draft, "db-migrate").Kind);   // a job, though postgres
        Assert.Equal(ComponentKind.Unsure, Get(draft, "api-gateway").Kind);
        Assert.Equal(ComponentKind.Service, Get(draft, "web").Kind);
    }

    [Fact]
    public void Several_repositories_each_with_their_own_db()
    {
        var catalog = Inline("shop-catalog", """
            {
              "name": "shop-catalog",
              "networks": { "default": { "name": "shop", "external": true } },
              "services": {
                "catalog": { "build": { "context": "/x/catalog" }, "depends_on": { "db": {} }, "networks": { "default": null } },
                "db": { "image": "postgres:16", "networks": { "default": null } }
              }
            }
            """);
        var orders = Inline("shop-orders", """
            {
              "name": "shop-orders",
              "networks": { "default": { "name": "shop", "external": true } },
              "services": {
                "orders": { "build": { "context": "/x/orders" }, "environment": { "DB_HOST": "db" }, "networks": { "default": null } },
                "db": { "image": "postgres:16", "networks": { "default": null } }
              }
            }
            """);
        var draft = Fixtures.Draft(catalog, orders);

        Assert.Equal(["shop-catalog-db", "shop-orders-db"], Names(draft, ComponentKind.Infrastructure));
        Assert.Contains("renamed", Get(draft, "shop-orders-db").Reasons.Last());
        Assert.Equal("db", Get(draft, "shop-orders-db").ComposeService);
        Assert.Equal(["shop-catalog", "shop-orders"], draft.Networks[0].Projects);
        Assert.Equal("shop", draft.Networks[0].Name);
        Assert.Empty(draft.Notes);
        // each service depends on its own project's db
        Assert.Equal("shop-orders/db", Assert.Single(Get(draft, "orders").DependsOn).Id);
    }

    [Fact]
    public void Says_when_the_projects_share_no_network()
    {
        var a = Inline("a", """{ "name": "a", "services": { "x": { "build": { "context": "/x" } } } }""");
        var b = Inline("b", """{ "name": "b", "services": { "y": { "build": { "context": "/y" } } } }""");
        Assert.Contains("No docker network is shared", Assert.Single(Fixtures.Draft(a, b).Notes));
    }

    [Theory]
    [InlineData("http://catalog:8000/books", "catalog", true)]
    [InlineData("postgresql://u:p@db-pool:5432/x", "db-pool", true)]
    [InlineData("Host=user-db;Port=5432", "user-db", true)]
    [InlineData("redis", "redis", true)]
    [InlineData("http://gateway/catalog/books", "catalog", false)]
    [InlineData("catalog-mode", "catalog", false)]
    public void Recognises_a_service_named_as_a_host(string value, string host, bool expected) =>
        Assert.Equal(expected, ComponentDrafter.NamesHost(value, host));

    private static ComposeProject Inline(string name, string json) =>
        ComposeProject.Parse(json, $"/projects/{name}", [$"/projects/{name}/compose.yaml"]);

    [Fact]
    public void A_service_calling_one_in_another_project_depends_on_it()
    {
        var auth = Inline("shop-auth", """
            {
              "name": "shop-auth",
              "networks": { "default": { "name": "shop", "external": true } },
              "services": {
                "authsvc": { "build": { "context": "/x/auth" }, "networks": { "default": null } }
              }
            }
            """);
        var orders = Inline("shop-orders", """
            {
              "name": "shop-orders",
              "networks": { "default": { "name": "shop", "external": true } },
              "services": {
                "orders": { "build": { "context": "/x/orders" }, "environment": { "AUTH_URL": "http://authsvc:8000" },
                            "networks": { "default": null } }
              }
            }
            """);

        var dependency = Assert.Single(Get(Fixtures.Draft(auth, orders), "orders").DependsOn);

        Assert.Equal("shop-auth/authsvc", dependency.Id);
        Assert.Contains("AUTH_URL", dependency.Via);
    }
}
