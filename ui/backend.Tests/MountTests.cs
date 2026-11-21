using MicroserviceScripts.Ui.Routes;

namespace MicroserviceScripts.Ui.Tests;

/// <summary>Routes declared on a router that another file or block puts under a prefix.</summary>
public sealed class MountTests
{
    private static string Routes(params (string File, string Code)[] files)
    {
        var dir = Directory.CreateTempSubdirectory("mounts-");
        try
        {
            foreach (var (file, code) in files)
            {
                var path = Path.Combine(dir.FullName, file);
                Directory.CreateDirectory(Path.GetDirectoryName(path)!);
                File.WriteAllText(path, code);
            }

            return string.Join("|", RouteCatalog.LoadDefault().Scan(dir.FullName)
                .Select(r => $"{r.Method} {r.Path}")
                .Order(StringComparer.Ordinal));
        }
        finally
        {
            dir.Delete(recursive: true);
        }
    }

    [Fact]
    public void Express_routers_mounted_in_other_files_and_in_the_same_one()
    {
        var found = Routes(
            ("app.js", """
                const express = require('express');
                const app = express();
                const api = require('./routes');
                const orders = require('./routes/orders');
                const health = express.Router();
                health.get('/ping', (req, res) => res.send('ok'));
                app.use('/api', api);
                app.use('/orders', auth, orders);
                app.use('/health', health);
                app.use('/static', express.static('public'));
                app.get('/', home);
                axios.get('/not-a-route');
                """),
            ("routes/index.js", """
                const router = require('express').Router();
                router.use('/books', require('./books'));
                module.exports = router;
                """),
            ("routes/books.js", """
                const router = express.Router();
                router.get('/:id', getBook);
                module.exports = router;
                """),
            ("routes/orders.js", """
                const ordersRouter = express.Router();
                ordersRouter.post('/', placeOrder);
                module.exports = ordersRouter;
                """));

        Assert.Equal("GET /|GET /api/books/{id}|GET /health/ping|POST /orders", found);
    }

    [Fact]
    public void Typescript_imports_hono_and_fastify()
    {
        var found = Routes(
            ("src/server.ts", """
                import booksRouter from './books.js';
                import { reviews as reviewRoutes } from './reviews';
                const app = new Hono();
                app.route('/books', booksRouter);
                app.route('/reviews', reviewRoutes);
                fastify.register(require('./plugins/stock'), { prefix: '/stock' });
                """),
            ("src/books.ts", """
                const router = new Hono();
                router.get('/:isbn', (c) => c.json({}));
                export default router;
                """),
            ("src/reviews.ts", """
                export const reviews = new Hono();
                reviews.post('/', (c) => c.json({}));
                """),
            ("src/plugins/stock.js", """
                module.exports = async function (fastify, opts) {
                  fastify.get('/:sku', async () => ({}));
                };
                """));

        Assert.Equal("GET /books/{isbn}|GET /stock/{sku}|POST /reviews", found);
    }

    [Fact]
    public void Fastapi_routers_included_through_a_router_with_a_settings_prefix()
    {
        var found = Routes(
            ("app/main.py", """
                from fastapi import FastAPI
                from app.core.config import settings
                from app.api import api_router
                app = FastAPI()
                app.include_router(api_router, prefix=settings.API_V1_STR)

                @app.get("/health")
                def health():
                    return {}
                """),
            ("app/core/config.py", """
                class Settings(BaseSettings):
                    API_V1_STR: str = "/api/v1"
                """),
            ("app/api.py", """
                from fastapi import APIRouter
                from app.routers import books, users
                api_router = APIRouter()
                api_router.include_router(books.router, prefix="/books", tags=["books"])
                api_router.include_router(users.router)
                """),
            ("app/routers/__init__.py", ""),
            ("app/routers/books.py", """
                router = APIRouter()
                @router.get("/{book_id}")
                def read(book_id: int):
                    pass
                """),
            ("app/routers/users.py", """
                router = APIRouter(prefix="/users")
                @router.get("/me")
                def me():
                    pass
                """));

        Assert.Equal("GET /api/v1/books/{book_id}|GET /api/v1/users/me|GET /health", found);
    }

    [Fact]
    public void A_flask_blueprints_url_prefix_at_registration_replaces_its_own()
    {
        var dir = Directory.CreateTempSubdirectory("mounts-");
        try
        {
            File.WriteAllText(Path.Combine(dir.FullName, "app.py"), """
                from shop import bp as shop_bp
                from .accounts import accounts
                app = Flask(__name__)
                app.register_blueprint(shop_bp, url_prefix='/store')
                app.register_blueprint(accounts)
                """);
            Directory.CreateDirectory(Path.Combine(dir.FullName, "shop"));
            File.WriteAllText(Path.Combine(dir.FullName, "shop", "__init__.py"), """
                bp = Blueprint('shop', __name__, url_prefix='/shop')
                from . import views
                """);
            File.WriteAllText(Path.Combine(dir.FullName, "shop", "views.py"), """
                from . import bp
                @bp.route('/cart/<int:item>', methods=['DELETE'])
                def remove(item):
                    pass
                """);
            File.WriteAllText(Path.Combine(dir.FullName, "accounts.py"), """
                accounts = Blueprint('accounts', __name__, url_prefix='/accounts')
                @accounts.route('/<name>')
                def show(name):
                    pass
                """);

            var found = RouteCatalog.LoadDefault().Scan(dir.FullName).OrderBy(r => r.Path).ToList();

            Assert.Equal(["/accounts/{name}", "/store/cart/{item}"], found.Select(r => r.Path));
            Assert.Equal("int", found[1].PathTypes!["item"]);
        }
        finally
        {
            dir.Delete(recursive: true);
        }
    }

    [Fact]
    public void Django_includes()
    {
        var found = Routes(
            ("project/urls.py", """
                urlpatterns = [
                    path('admin/', admin.site.urls),
                    path('api/books/', include('books.urls')),
                    path('api/', include(router.urls)),
                ]
                router = DefaultRouter()
                router.register(r'authors', AuthorViewSet)
                """),
            ("books/urls.py", """
                urlpatterns = [
                    path('<int:pk>/', views.detail),
                ]
                """));

        Assert.Equal(
            "DELETE /api/authors/{id}|GET /admin|GET /api/authors|GET /api/authors/{id}|GET /api/books/{pk}"
            + "|PATCH /api/authors/{id}|POST /api/authors|PUT /api/authors/{id}",
            found);
    }

    [Fact]
    public void Aspnet_groups_inside_groups_and_endpoint_extension_methods()
    {
        var found = Routes(
            ("Program.cs", """
                var app = builder.Build();
                var api = app.MapGroup("/api");
                var books = api.MapGroup("/books").WithTags("Books");
                books.MapGet("/{id:int}", (int id) => id);
                app.MapGroup("/orders").RequireAuthorization().MapOrderEndpoints();
                api.MapReviewEndpoints();
                app.MapGet("/health", () => "ok");
                app.MapControllers();
                """),
            ("Endpoints/Orders.cs", """
                public static class OrderEndpoints
                {
                    public static RouteGroupBuilder MapOrderEndpoints(this RouteGroupBuilder group)
                    {
                        group.MapPost("/", Create);
                        return group;
                    }

                    public static void MapReviewEndpoints(this IEndpointRouteBuilder routes)
                    {
                        var reviews = routes.MapGroup("/reviews");
                        reviews.MapGet("/{id}", Show);
                    }
                }
                """));

        Assert.Equal("GET /api/books/{id}|GET /api/reviews/{id}|GET /health|POST /orders", found);
    }

    [Fact]
    public void Go_groups_registration_functions_and_chi()
    {
        var found = Routes(
            ("main.go", """
                func main() {
                    r := gin.Default()
                    v1 := r.Group("/v1")
                    books := v1.Group("/books")
                    books.GET("/:id", h.GetBook)
                    routes.RegisterOrders(v1)
                    fmt.Println(v1)
                }
                """),
            ("routes/orders.go", """
                func RegisterOrders(rg *gin.RouterGroup) {
                    orders := rg.Group("/orders")
                    orders.POST("/", h.Create)
                }
                """),
            ("chi/server.go", """
                func router() http.Handler {
                    r := chi.NewRouter()
                    r.Route("/users", func(r chi.Router) {
                        r.Get("/{id}", getUser)
                    })
                    r.Mount("/admin", adminRouter())
                    r.Get("/", index)
                    return r
                }

                func adminRouter() http.Handler {
                    r := chi.NewRouter()
                    r.Get("/stats", stats)
                    return r
                }
                """));

        Assert.Equal("GET /|GET /admin/stats|GET /users/{id}|GET /v1/books/{id}|POST /v1/orders", found);
    }

    [Fact]
    public void Axum_nest_laravel_rails_and_phoenix_blocks()
    {
        var found = Routes(
            ("src/main.rs", """
                let app = Router::new().nest("/books", books_routes()).route("/", get(index));
                fn books_routes() -> Router {
                    Router::new().route("/:id", get(show))
                }
                """),
            ("routes/api.php", """
                <?php
                Route::prefix('shop')->middleware('auth')->group(function () {
                    Route::get('/cart', [CartController::class, 'show']);
                });
                Route::group(['prefix' => 'admin', 'middleware' => 'auth'], function () {
                    Route::post('/users', [UserController::class, 'store']);
                });
                Route::get('/status', [StatusController::class, 'show']);
                """),
            ("config/routes.rb", """
                Rails.application.routes.draw do
                  namespace :api do
                    get 'novels', to: 'novels#index'
                    scope '/v2' do
                      post 'orders', to: 'orders#create'
                    end
                  end
                  get 'health', to: 'health#show'
                end
                """),
            ("lib/shop_web/router.ex", """
                scope "/api", ShopWeb do
                  pipe_through :api
                  get "/carts", CartController, :index
                end
                """));

        Assert.Equal(
            "GET /|GET /api/carts|GET /api/novels|GET /books/{id}|GET /health|GET /shop/cart|GET /status|POST /admin/users|POST /api/v2/orders",
            found);
    }
}
