using System.Text.Json.Nodes;
using MicroserviceScripts.Ui.Drafting;
using MicroserviceScripts.Ui.Routes;

namespace MicroserviceScripts.Ui.Tests;

/// <summary>Routes and example requests across languages, and saying when a language can't be read.</summary>
public sealed class LanguageTests : IDisposable
{
    private readonly string _dir = Directory.CreateTempSubdirectory("languages-").FullName;
    private static readonly RouteCatalog Catalog = RouteCatalog.LoadDefault();
    private static readonly RequestShapes Shapes = RequestShapes.LoadDefault();

    public void Dispose() => Directory.Delete(_dir, recursive: true);

    private void Write(string file, string code)
    {
        var path = Path.Combine(_dir, file);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.WriteAllText(path, code);
    }

    private IReadOnlyList<SourceRoute> Routes() => Catalog.Find(Catalog.SourceFiles(_dir));

    private RequestShape Shape(string method, string path)
    {
        var files = Catalog.SourceFiles(_dir);
        var route = Catalog.Find(files).Single(r => r.Method == method && r.Path == path);
        return Shapes.Build(route, TypeIndex.Build(files), Path.GetFileName)!;
    }

    private static JsonNode Json(string? body) => JsonNode.Parse(body!)!;

    [Theory]
    [InlineData("src/main.rs", "#[get(\"/books/{id}\")]\nasync fn get_book(path: web::Path<u32>) -> impl Responder { }", "GET /books/{id} get_book")]
    [InlineData("src/main.rs", "let app = Router::new()\n    .route(\"/users\", get(list_users).post(create_user))\n    .route(\"/users/:id\", delete(remove_user));",
        "DELETE /users/{id} remove_user|GET /users list_users|POST /users create_user")]
    [InlineData("config/routes.rb", "Rails.application.routes.draw do\n  resources :books\n  get '/health', to: 'status#show'\nend",
        "DELETE /books/{id} -|GET /books -|GET /books/{id} -|GET /health show|PATCH /books/{id} -|POST /books -|PUT /books/{id} -")]
    [InlineData("routes/api.php", "<?php\nRoute::apiResource('orders', OrderController::class);\nRoute::post('/login', [AuthController::class, 'login']);",
        "DELETE /orders/{id} -|GET /orders -|GET /orders/{id} -|PATCH /orders/{id} -|POST /login -|POST /orders -|PUT /orders/{id} -")]
    [InlineData("src/Controller/BookController.php",
        "<?php\n#[Route('/api/books')]\nclass BookController {\n  #[Route('/{id}', methods: ['GET'])]\n  public function show(int $id) {}\n}",
        "GET /api/books/{id} show")]
    [InlineData("lib/shop_web/router.ex", "scope \"/api\", ShopWeb do\n  get \"/books/:id\", BookController, :show\n  resources \"/orders\", OrderController\nend",
        "DELETE /orders/{id} -|GET /books/{id} show|GET /orders -|GET /orders/{id} -|PATCH /orders/{id} -|POST /orders -|PUT /orders/{id} -")]
    [InlineData("conf/routes", "GET     /books/:id      controllers.Books.show(id: Long)\nPOST    /books          controllers.Books.create()",
        "GET /books/{id} -|POST /books -")]
    [InlineData("Sources/App/routes.swift", "app.get(\"books\", \":id\") { req in }\napp.post(\"books\") { req in }",
        "GET /books/{id} -|POST /books -")]
    [InlineData("src/main.cpp", "CROW_ROUTE(app, \"/orders\").methods(\"POST\"_method)([](const crow::request& req){});\nsvr.Get(\"/health\", [](auto&, auto& res){});",
        "GET /health -|POST /orders -")]
    [InlineData("main.go", "r := mux.NewRouter()\nr.HandleFunc(\"/books/{id}\", getBook).Methods(\"GET\")\nv1 := router.Group(\"/v1\")\nv1.POST(\"/orders\", createOrder)",
        "GET /books/{id} getBook|POST /v1/orders createOrder")]
    [InlineData("shop/urls.py", "urlpatterns = [\n    path('books/<int:pk>/', views.book_detail),\n]\nrouter.register(r'orders', OrderViewSet)",
        "DELETE /orders/{id} -|GET /books/{pk} views.book_detail|GET /orders -|GET /orders/{id} -|PATCH /orders/{id} -|POST /orders -|PUT /orders/{id} -")]
    [InlineData("src/Application.kt", "routing {\n    get(\"/books\") {\n        call.respond(books)\n    }\n}", "GET /books -")]
    [InlineData("src/BookResource.java", "@Path(\"/books\")\npublic class BookResource {\n  @GET\n  @Path(\"/{id}\")\n  public Book get(@PathParam(\"id\") long id) { }\n}",
        "GET /books/{id} get")]
    [InlineData("src/BookController.java", "@Controller(\"/books\")\npublic class BookController {\n  @Get(\"/{id}\")\n  public Book show(Long id) { }\n}",
        "GET /books/{id} show")]
    [InlineData("lib/server.dart", "final app = Router();\napp.get('/books/<id>', getBook);", "GET /books/{id} -")]
    public void More_frameworks_are_recognised(string file, string code, string expected)
    {
        Write(file, code);

        var found = Routes().OrderBy(r => r.Method).ThenBy(r => r.Path).Select(r => $"{r.Method} {r.Path} {r.Handler ?? "-"}");

        Assert.Equal(expected, string.Join("|", found));
    }

    [Fact]
    public void Finki_routes_come_with_the_requests_they_expect()
    {
        var project = Fixtures.Project("finki");
        var draft = new RouteDrafter(Catalog, DockerfileFacts.Read, Shapes).Draft([project], Fixtures.Draft(project));
        DraftRoute Get(string method, string path) => draft.Routes.Single(r => r.Method == method && r.Path == path);

        // FastAPI: the Pydantic model is the body, Depends(...) is skipped
        var create = Get("POST", "/blog/create").Request!;
        Assert.Equal("CreateBlogDto", create.BodyType);
        Assert.Equal("BlogService/Dtos.py:10", create.BodySource);
        Assert.Equal("""{"title":"string","blog_text":"string"}""", Json(create.Body).ToJsonString());

        // a plain int is a required query parameter; one in the path fills the path
        Assert.Equal(new ParamShape("blog_id", "query", "int", "1", true), Assert.Single(Get("POST", "/likes/like").Request!.Params));
        Assert.Equal(new ParamShape("blog_id", "path", "int", "1", true),
            Assert.Single(Get("POST", "/blog/deleteBlog/{blog_id}").Request!.Params));
        Assert.All(Get("GET", "/blog/blogs").Request!.Params, p => Assert.False(p.Required));   // Query(0), Query(20)

        // ASP.NET: [FromBody] LoginDto, camelCase like the app reads it, field names hint the values
        var login = Get("POST", "/user/Authentication/login").Request!;
        Assert.Equal("LoginDto", login.BodyType);
        Assert.Equal("""{"email":"user@example.com","password":"Password123!"}""", Json(login.Body).ToJsonString());
        Assert.Contains(Get("POST", "/user/Authentication/register").Request!.Notes,
            n => n.StartsWith("Every request sends the same userName and email", StringComparison.Ordinal));

        // every service's language is known, and FINKI's are all readable
        Assert.Contains(draft.Code!, c => c.Service == "finki-blogger-2/user-service" && c.Language == "C#" && c.Supported);
        Assert.Contains(draft.Code!, c => c.Service == "finki-blogger-2/blog-service" && c.Language == "Python" && c.Routes == 4);
    }

    [Fact]
    public void Python_mongo_ids_nested_models_and_unusual_types()
    {
        Write("app/models.py", """
            from typing import List, Optional, Literal
            from pydantic import BaseModel, EmailStr, Field
            class Status(str, Enum):
                draft = "draft"
                live = "live"
            class Address(BaseModel):
                city: str
            class Author(BaseModel):
                id: Optional[PyObjectId] = Field(alias="_id", default=None)
                email: EmailStr
                tags: List[str] = []
                address: Address
                status: Status
                kind: Literal["person", "team"]
                extra: dict
                joined: datetime
            """);
        Write("app/main.py", "@app.post(\"/authors\")\nasync def create(author: Author, db = Depends(get_db)):\n    ...");

        var body = Json(Shape("POST", "/authors").Body);

        Assert.Null(body["_id"]);                                     // a fixed _id fails after the first insert
        Assert.Equal("user@example.com", body["email"]!.GetValue<string>());
        Assert.Equal("""["string"]""", body["tags"]!.ToJsonString());
        Assert.Equal("string", body["address"]!["city"]!.GetValue<string>());
        Assert.Equal("draft", body["status"]!.GetValue<string>());
        Assert.Equal("person", body["kind"]!.GetValue<string>());
        Assert.Equal("{}", body["extra"]!.ToJsonString());
        Assert.Equal("2026-01-01T12:00:00Z", body["joined"]!.GetValue<string>());
    }

    [Fact]
    public void Csharp_records_bson_attributes_and_enums()
    {
        Write("Models/Book.cs", """
            public enum Genre { Fiction = 1, Science }
            public class Book
            {
                [BsonId]
                [BsonRepresentation(BsonType.ObjectId)]
                public string? Id { get; set; }
                [JsonPropertyName("book_title")]
                public string Title { get; set; } = "";
                public Decimal128 Price { get; set; }
                public List<string> Tags { get; set; } = new();
                public Genre Genre { get; set; }
                [BsonRepresentation(BsonType.ObjectId)]
                public string AuthorId { get; set; }
                [JsonIgnore]
                public string Secret { get; set; }
            }
            public record Review(Guid BookId, int Stars, string? Text);
            """);
        Write("Program.cs", "app.MapPost(\"/books\", ([FromBody] Book book, AppDb db) => Results.Ok());\napp.MapPost(\"/reviews/{id}\", (int id, Review review) => 1);");

        var book = Json(Shape("POST", "/books").Body);
        Assert.Equal("""{"book_title":"string","price":1.5,"tags":["string"],"genre":1,"authorId":"507f1f77bcf86cd799439011"}""",
            book.ToJsonString());

        var review = Shape("POST", "/reviews/{id}");
        Assert.Equal("""{"bookId":"3fa85f64-5717-4562-b3fc-2c963f66afa6","stars":1,"text":"string"}""", Json(review.Body).ToJsonString());
        Assert.Equal(new ParamShape("id", "path", "int", "1", true), Assert.Single(review.Params));
    }

    [Fact]
    public void Java_spring_records_with_mongo_ids()
    {
        Write("src/Order.java", """
            public record OrderLine(String sku, int qty) {}
            @Document
            public class Order {
                @Id
                private String id;
                @JsonProperty("customer_email")
                private String email;
                private LocalDate due;
                private List<OrderLine> lines;
                private ObjectId warehouseId;
                private static final long serialVersionUID = 1L;
            }
            """);
        Write("src/OrderController.java", """
            @RestController
            @RequestMapping("/orders")
            public class OrderController {
                @PostMapping
                public Order create(@RequestBody @Valid Order order, Principal user) { return order; }
                @GetMapping("/{id}")
                public Order get(@PathVariable("id") String id, @RequestParam(required = false) String expand) { return null; }
            }
            """);

        Assert.Equal("""{"customer_email":"user@example.com","due":"2026-01-01","lines":[{"sku":"string","qty":1}],"warehouseId":"507f1f77bcf86cd799439011"}""",
            Json(Shape("POST", "/orders").Body).ToJsonString());
        var get = Shape("GET", "/orders/{id}");
        Assert.Equal(new ParamShape("id", "path", "String", "string", true), get.Params[0]);
        Assert.False(get.Params[1].Required);
    }

    [Fact]
    public void Typescript_nest_dtos_and_mongoose_ids()
    {
        Write("src/cats/dto.ts", """
            export enum Color { Black = 'black', White = 'white' }
            export class CreateCatDto {
              @IsString()
              name: string;
              @IsOptional()
              age?: number;
              color: Color;
              owner: Types.ObjectId;
              toys: string[];
            }
            """);
        Write("src/cats/cats.controller.ts", """
            @Controller('cats')
            export class CatsController {
              @Post()
              create(@Body() dto: CreateCatDto) {}
              @Get(':id')
              findOne(@Param('id') id: string, @Query('full') full?: boolean) {}
            }
            """);

        Assert.Equal("""{"name":"string","age":1,"color":"black","owner":"507f1f77bcf86cd799439011","toys":["string"]}""",
            Json(Shape("POST", "/cats").Body).ToJsonString());
        Assert.Equal("id", Shape("GET", "/cats/{id}").Params[0].Name);
    }

    [Fact]
    public void Go_handlers_bind_their_body_inside()
    {
        Write("models.go", """
            package main
            type CreateUser struct {
                ID       primitive.ObjectID `json:"id,omitempty" bson:"_id,omitempty"`
                Email    string             `json:"email" binding:"required"`
                Age      *int               `json:"age,omitempty"`
                Roles    []string           `json:"roles"`
                internal string
                Meta     map[string]any     `json:"meta"`
                Joined   time.Time          `json:"joined"`
                Skip     string             `json:"-"`
            }
            """);
        Write("main.go", """
            package main
            func main() {
                r := gin.Default()
                r.POST("/users", createUser)
                r.GET("/users/:id", getUser)
            }
            func createUser(c *gin.Context) {
                var req CreateUser
                if err := c.ShouldBindJSON(&req); err != nil { return }
            }
            func getUser(c *gin.Context) { id := c.Param("id") }
            """);

        var create = Shape("POST", "/users");
        Assert.Equal("CreateUser", create.BodyType);
        Assert.Equal("""{"email":"user@example.com","age":1,"roles":["string"],"meta":{},"joined":"2026-01-01T12:00:00Z"}""",
            Json(create.Body).ToJsonString());
        Assert.Equal(new ParamShape("id", "path", "string", "1", true), Assert.Single(Shape("GET", "/users/{id}").Params));
    }

    [Fact]
    public void Rust_extractors_and_serde_renames()
    {
        Write("src/models.rs", """
            #[derive(Deserialize)]
            #[serde(rename_all = "camelCase")]
            pub struct CreateItem {
                pub item_name: String,
                pub unit_price: f64,
                #[serde(rename = "_id")]
                pub id: Option<ObjectId>,
                pub owner: Uuid,
                pub tags: Vec<String>,
                #[serde(skip)]
                pub cache: u64,
            }
            #[derive(Deserialize)]
            pub struct Paging { pub page: u32, pub size: Option<u32> }
            """);
        Write("src/main.rs", """
            let app = Router::new()
                .route("/items", post(create_item))
                .route("/shops/:shop/items/:id", get(get_item));
            async fn create_item(State(db): State<Db>, Json(payload): Json<CreateItem>) -> impl IntoResponse { }
            async fn get_item(Path((shop, id)): Path<(String, i64)>, Query(paging): Query<Paging>) -> String { }
            """);

        Assert.Equal("""{"itemName":"string","unitPrice":1.5,"owner":"3fa85f64-5717-4562-b3fc-2c963f66afa6","tags":["string"]}""",
            Json(Shape("POST", "/items").Body).ToJsonString());
        var get = Shape("GET", "/shops/{shop}/items/{id}");
        Assert.Equal(["shop:string", "id:1", "page:1"], get.Params.Select(p => $"{p.Name}:{p.Example}"));
    }

    [Theory]
    [InlineData("Main.hs", "Haskell", false)]
    [InlineData("server.rb", "Ruby", true)]
    [InlineData("handler.zig", "Zig", false)]
    [InlineData("api.go", "Go", true)]
    public void The_language_of_a_service_and_whether_it_can_be_read(string file, string language, bool supported)
    {
        Write(file, "-- code");
        Write("README.md", "docs");

        var found = Catalog.LanguageOf(_dir);

        Assert.Equal(language, found.Language);
        Assert.Equal(supported, found.Supported);
    }

    [Theory]
    [InlineData("golang:1.23 AS build", "Go")]
    [InlineData("php:8.3-fpm", "PHP")]
    [InlineData("ruby:3.3-slim", "Ruby")]
    [InlineData("rust:1.82", "Rust")]
    [InlineData("denoland/deno:2.0.0", "Deno")]
    [InlineData("swift:6.0", "Swift")]
    public void Runtimes_are_told_by_their_image(string image, string language)
    {
        Assert.Equal(language, PortGuesser.RuntimeOf(image.Split(' ')[0])?.Language);
    }
}
