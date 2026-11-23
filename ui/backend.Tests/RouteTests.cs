using MicroserviceScripts.Ui.Compose;
using MicroserviceScripts.Ui.Drafting;
using MicroserviceScripts.Ui.Routes;

namespace MicroserviceScripts.Ui.Tests;

public class RouteTests
{
    private static RouteDraft Routes(ComposeProject project) =>
        new RouteDrafter(RouteCatalog.LoadDefault(), DockerfileFacts.Read, RequestShapes.LoadDefault())
            .Draft([project], Fixtures.Draft(project));

    private static DraftRoute Get(RouteDraft draft, string method, string path) =>
        draft.Routes.Single(r => r.Method == method && r.Path == path);

    [Fact]
    public void Finki_routes_are_found_and_reached_through_both_gateways()
    {
        // nginx (gateway-lb) -> Kong (api-gateway, kong.yml) -> per-service nginx -> the service
        var draft = Routes(Fixtures.Project("finki"));

        var blogs = Get(draft, "GET", "/blog/blogs");
        Assert.Equal("http://localhost:8000", blogs.EntryPoint);
        Assert.Equal("finki-blogger-2/blog-service", blogs.Service);
        Assert.Equal(
            ["finki-blogger-2/gateway-lb", "finki-blogger-2/api-gateway", "finki-blogger-2/blog-service-lb"],
            blogs.Through);
        Assert.Equal("/blogs", blogs.ServicePath);
        Assert.Equal("get_blogs", blogs.Handler);
        Assert.Equal("BlogService/main.py:15", blogs.Source);

        Assert.Equal("get_blog", Get(draft, "GET", "/blog/blogs/{blog_id}").Handler);
        Assert.Equal("create", Get(draft, "POST", "/blog/create").Handler);
        Assert.Equal("like_post", Get(draft, "POST", "/likes/like").Handler);
        Assert.Equal("create_comment", Get(draft, "POST", "/comments/create-comment").Handler);

        // ASP.NET: [Route("[controller]")] on the class, [HttpPost("login")] on the method
        var login = Get(draft, "POST", "/user/Authentication/login");
        Assert.Equal("finki-blogger-2/user-service", login.Service);
        Assert.Equal("ASP.NET Core", login.Framework);
        Assert.Equal("GET", Get(draft, "GET", "/user/User").Method);

        // the Spring email service only listens to RabbitMQ, and startup hooks aren't routes
        Assert.DoesNotContain(draft.Routes, r => r.Service.EndsWith("/email-service", StringComparison.Ordinal));
        Assert.DoesNotContain(draft.Routes, r => r.Path.Contains("startup", StringComparison.Ordinal));
        Assert.Empty(draft.Notes);
    }

    [Fact]
    public void Bookshop_routes_go_through_its_nginx_prefixes()
    {
        var draft = Routes(Fixtures.Project("bookshop"));

        var books = Get(draft, "GET", "/catalog/books");
        Assert.Equal("http://localhost:9000", books.EntryPoint);
        Assert.Equal(["bookshop-pristine/gateway"], books.Through);
        Assert.Equal("/books", books.ServicePath);

        Assert.Equal("bookshop-pristine/orders", Get(draft, "POST", "/orders/orders").Service);
        Assert.Equal("get_order", Get(draft, "GET", "/orders/orders/{order_id}").Handler);
        Assert.Equal(2, draft.Routes.Count(r => r.ServicePath == "/health"));
    }

    [Theory]
    [InlineData("/catalog/", "/", "/books", "/catalog/books")]           // nginx proxy_pass http://catalog:8000/;
    [InlineData("/blog", "/", "/blogs", "/blog/blogs")]                  // Kong strip_path
    [InlineData("/api", "/v1", "/v1/users", "/api/users")]               // proxy_pass http://x/v1;
    [InlineData("/api", "/v1", "/v2/users", null)]                       // not behind this rule
    [InlineData("/", null, "/books", "/books")]                          // passed on unchanged
    [InlineData("/orders/", null, "/books", null)]
    public void A_path_inside_a_gateway_is_mapped_back_outside(string prefix, string? upstream, string inside, string? outside)
    {
        Assert.Equal(outside, RouteDrafter.External(new ProxyRule(prefix, "x", upstream, false, "test"), inside));
    }

    [Fact]
    public void Nginx_upstream_blocks_variables_and_regex_locations()
    {
        var (rules, notes) = ProxyConfig.Nginx("""
            upstream api_pool { server api-1:8080; server api-2:8080 weight=2; }
            server {
                listen 80;
                set $web http://web:3000;
                location /api/ { proxy_pass http://api_pool/; }
                location / { proxy_pass $web$request_uri; }
                location = /status { proxy_pass http://status:9000; }
                location ~ ^/legacy/(.*)$ { proxy_pass http://legacy:80/$1; }
            }
            """, "nginx.conf");

        Assert.Contains(new ProxyRule("/api/", "api-1", "/", false, "nginx.conf"), rules);
        Assert.Contains(new ProxyRule("/api/", "api-2", "/", false, "nginx.conf"), rules);
        Assert.Contains(new ProxyRule("/", "web", null, false, "nginx.conf"), rules);
        Assert.Contains(new ProxyRule("/status", "status", null, true, "nginx.conf"), rules);
        Assert.Contains(notes, n => n.Contains("/legacy/", StringComparison.Ordinal));
    }

    [Fact]
    public void Kong_service_paths_strip_path_and_top_level_routes()
    {
        var (rules, _) = ProxyConfig.Kong("""
            _format_version: "3.0"
            services:
              - name: orders
                url: http://orders:8080/api
                routes:
                  - paths: [/shop/orders]
              - name: catalog
                host: catalog
                port: 8000
            routes:
              - name: books
                service: { name: catalog }
                paths: [/books]
                strip_path: false
            """, "kong.yml");

        Assert.Contains(new ProxyRule("/shop/orders", "orders", "/api", false, "kong.yml"), rules);
        Assert.Contains(new ProxyRule("/books", "catalog", null, false, "kong.yml"), rules);
        Assert.Equal("/shop/orders/1", RouteDrafter.External(rules[0], "/api/1"));
    }

    [Fact]
    public void Yarp_routes_clusters_transforms_and_the_environment_file_on_top()
    {
        var (rules, notes) = ProxyConfig.Yarp(
        [
            ("""
             {
               // comments and trailing commas, as ASP.NET Core allows
               "ReverseProxy": {
                 "Routes": {
                   "api": { "ClusterId": "api", "Match": { "Path": "/api/{**catch-all}" } },
                   "internal": {
                     "ClusterId": "match",
                     "Match": { "Path": "/internal/am-api/{**rest}" },
                     "Transforms": [ { "PathPattern": "/api/{**rest}" } ],
                   },
                   "strip": {
                     "ClusterId": "api",
                     "Match": { "Path": "/v1/{**rest}" },
                     "Transforms": [ { "PathRemovePrefix": "/v1" } ]
                   },
                   "health": { "ClusterId": "api", "Match": { "Path": "/health" } },
                   "hosts": { "ClusterId": "api", "Match": { "Hosts": [ "example.com" ] } }
                 },
                 "Clusters": {
                   "api": { "Destinations": { "d1": { "Address": "http://localhost:5267" } } },
                   "match": { "Destinations": { "d1": { "Address": "http://localhost:5277" } } }
                 }
               }
             }
             """, "appsettings.json"),
            ("""
             { "ReverseProxy": { "Clusters": {
                 "api": { "Destinations": { "d1": { "Address": "http://api:8080" } } },
                 "match": { "Destinations": { "d1": { "Address": "http://match:8080/base/" } } } } } }
             """, "appsettings.Production.json"),
        ]);

        const string from = "appsettings.json + appsettings.Production.json";
        Assert.Contains(new ProxyRule("/api/", "api", null, false, from), rules);
        Assert.Contains(new ProxyRule("/internal/am-api/", "match", "/base/api", false, from), rules);
        Assert.Contains(new ProxyRule("/v1/", "api", "/", false, from), rules);
        Assert.Contains(new ProxyRule("/health", "api", null, true, from), rules);
        Assert.DoesNotContain(rules, r => r.Host == "localhost");
        Assert.Contains(notes, n => n.Contains("'hosts'", StringComparison.Ordinal));

        var internalRule = rules.Single(r => r.Prefix == "/internal/am-api/");
        Assert.Equal("/internal/am-api/games/1", RouteDrafter.External(internalRule, "/base/api/games/1"));
        Assert.Equal("/v1/books", RouteDrafter.External(rules.Single(r => r.Prefix == "/v1/"), "/books"));
    }

    [Fact]
    public void Yarp_environment_variables_override_the_files_and_broken_json_is_named()
    {
        var (rules, notes) = ProxyConfig.Yarp(
        [
            ("""
             { "ReverseProxy": { "Routes": { "r": { "ClusterId": "c", "Match": { "Path": "/x/{**p}" } } },
               "Clusters": { "c": { "Destinations": { "d": { "Address": "http://old:80" } } } } } }
             """, "appsettings.json"),
            ("{ not json", "appsettings.Development.json"),
        ], new Dictionary<string, string?> { ["ReverseProxy:Clusters:c:Destinations:d:Address"] = "http://new:8080" });

        Assert.Equal([new ProxyRule("/x/", "new", null, false, "appsettings.json")], rules);
        Assert.Contains(notes, n => n.StartsWith("appsettings.Development.json", StringComparison.Ordinal));
    }

    [Fact]
    public void Traefik_labels_with_a_strip_prefix_middleware()
    {
        var project = ComposeProject.Parse("""
            {
              "name": "shop",
              "services": {
                "traefik": { "image": "traefik:v3.1", "ports": [{ "target": 80, "published": "8080" }] },
                "api": {
                  "build": { "context": "/nowhere/api" },
                  "labels": {
                    "traefik.http.routers.api.rule": "Host(`localhost`) && PathPrefix(`/api`)",
                    "traefik.http.routers.api.middlewares": "strip-api@docker",
                    "traefik.http.middlewares.strip-api.stripprefix.prefixes": "/api"
                  }
                },
                "web": { "image": "nginx", "labels": { "traefik.enable": "false" } }
              }
            }
            """, "/nowhere", []);

        var rules = ProxyConfig.Traefik(project);

        var rule = Assert.Single(rules);
        Assert.Equal(new ProxyRule("/api", "api", "/", false, "labels of api"), rule);
        Assert.Equal("/api/orders", RouteDrafter.External(rule, "/orders"));
    }

    [Theory]
    [InlineData("routes.js", "router.get('/books/:id', getBook);\napp.post(\"/orders\", async (req, res) => {});",
        "GET /books/{id} getBook|POST /orders -")]
    [InlineData("OrderController.java",
        "@RestController\n@RequestMapping(\"/api/orders\")\npublic class OrderController {\n"
        + "  @GetMapping(\"/{id}\")\n  public Order get(@PathVariable long id) { }\n"
        + "  @PostMapping\n  public Order create(@RequestBody Order o) { }\n}",
        "GET /api/orders/{id} get|POST /api/orders create")]
    [InlineData("books.controller.ts",
        "@Controller('books')\nexport class BooksController {\n  @Get(':id')\n  findOne() {}\n  @Post()\n  async create() {}\n}",
        "GET /books/{id} findOne|POST /books create")]
    [InlineData("main.go", "r.GET(\"/books/:id\", h.GetBook)\nhttp.HandleFunc(\"POST /orders\", placeOrder)",
        "GET /books/{id} GetBook|POST /orders placeOrder")]
    [InlineData("app.py",
        "bp = Blueprint('shop', __name__, url_prefix='/shop')\n@bp.route('/cart/<int:item>', methods=['DELETE'])\ndef remove(item):\n    pass",
        "DELETE /shop/cart/{item} remove")]
    [InlineData("routers.py",
        "router = APIRouter(prefix=\"/users\")\n@router.get(\"/{user_id}\")\nasync def read_user(user_id: int):\n    pass",
        "GET /users/{user_id} read_user")]
    [InlineData("Program.cs", "app.MapGet(\"/todos/{id:int}\", (int id) => id);", "GET /todos/{id} -")]
    public void Common_frameworks_are_recognised(string file, string code, string expected)
    {
        var dir = Directory.CreateTempSubdirectory("routes-");
        try
        {
            File.WriteAllText(Path.Combine(dir.FullName, file), code);
            var found = RouteCatalog.LoadDefault().Scan(dir.FullName)
                .OrderBy(r => r.Line)
                .Select(r => $"{r.Method} {r.Path} {r.Handler ?? "-"}");
            Assert.Equal(expected, string.Join("|", found));
        }
        finally
        {
            dir.Delete(recursive: true);
        }
    }

    [Fact]
    public void Tests_and_dependencies_are_not_searched()
    {
        var dir = Directory.CreateTempSubdirectory("routes-");
        try
        {
            Directory.CreateDirectory(Path.Combine(dir.FullName, "node_modules", "express"));
            File.WriteAllText(Path.Combine(dir.FullName, "node_modules", "express", "index.js"), "app.get('/internal', x)");
            File.WriteAllText(Path.Combine(dir.FullName, "app.test.js"), "app.get('/only-in-a-test', x)");
            File.WriteAllText(Path.Combine(dir.FullName, "app.js"), "app.get('/real', x)");

            Assert.Equal("/real", Assert.Single(RouteCatalog.LoadDefault().Scan(dir.FullName)).Path);
        }
        finally
        {
            dir.Delete(recursive: true);
        }
    }
}
