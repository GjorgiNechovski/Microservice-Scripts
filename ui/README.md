# Setup app

A local web app that sets the analyzer up for your Docker Compose app and starts
it. Point it at your project's folders and it drafts the spec and the load test
for you, so you check and correct instead of writing TOML and k6 by hand. It is
optional: everything it writes is the same spec and script the command line
uses (see the [main README](../README.md)), and the command line works without it.

Not here yet: setting up metrics (instrumentation and the monitoring stack's
settings) and showing the results; the run writes its reports to files.

## Start it

You need the [.NET 10 SDK](https://dotnet.microsoft.com/download) and
[Node.js 20 or newer](https://nodejs.org), besides what the analyzer itself
needs (Docker with Compose v2, Python 3.11 or newer).

```bash
./start.sh                 # Linux and macOS, from the repository's root
.\start.ps1                # Windows (PowerShell)
./start.sh --port 5300     # if 5199 is taken
```

It builds the page the first time, starts a server on this machine only, and
opens your browser at an address with a key in it. Use that address: without
the key the page can't do anything. Stop the app with Ctrl+C.

## What it does

1. **Check** that Docker, Compose, Python and the analyzer are there.
2. **Project.** Add the folder of each repository your app's services live in,
   or one folder that holds them all. Only those folders are searched for compose
   files, skipping `node_modules`, `.git`, build output and the like. You can
   also pick a compose project Docker already knows (one started on this machine at
   least once; one never started isn't listed), which keeps the name it was
   started under (`-p`) and its files together, or point at a compose file
   directly, whatever its name. Compose reads the chosen files itself
   (`docker compose config`), so overlays, profiles and `.env` variables work as
   they do for `docker compose up`. An app split over several repositories becomes
   several compose projects; they must share a docker network, and the app says
   so if they don't. A service that calls one in another of the projects by
   name, over the shared network, depends on it.
3. **Components.** Every compose service is sorted into *service*,
   *infrastructure*, *not modelled* (frontends, admin UIs, monitoring, one-off
   jobs) or *unsure*, with the reason shown. Replicas (`db` and `db-2`) and
   poolers or load balancers in front of a single component fold into it. A copy
   told apart only by a number (`db-1`, `db-2`) whose settings don't name the
   others (as `REPMGR_PARTNER_NODES` or `RABBITMQ_SEED_NODE` do in a cluster) may
   as well be a separate database: for infrastructure the app asks whether it
   takes over when the other fails before counting it as an instance, since
   redundancy that isn't there makes the availability figures too good.
   Readiness ports are guessed from the compose file, the Dockerfile and the
   runtime, and the source of each guess is shown. You correct anything: rename,
   reclassify, take a folded service back out, add one by hand, and decide every
   *unsure* one before going on.
4. **Routes.** First, whether your app is running: trying routes, and the run
   itself, need it up, so better now than later. If it isn't, **Start it for
   me** runs `docker compose up -d` with exactly the files and project name you
   chose, showing Compose's output, until your app's address answers; or copy the
   command and start it yourself. Then the routes each service declares in its code are listed, grouped
   by the service that handles them, with the address they are reached at from
   this machine: through which gateways, under which prefix. Reading routes are
   picked to begin with; one that changes data, checks health or needs a value
   for a `{parameter}` waits for you, with the reason. Each route belongs to a
   **feature** (the spec's operations); give two routes the same feature name to
   make them one. **Authorization** works like Postman's tab of that name: *no
   auth*, a *bearer token* or *API key* you already have, or *log in first*. For
   a login you fill in the request as in Postman (method, path, a Key/Value
   body) and press **Send**; the response is shown with the token marked, and
   any other value can be clicked to be the token instead. The test logs in once
   and sends the token with every request. The locked value (the password, the
   token, the key) is never written to a file: the test reads it from
   `LOGIN_PASSWORD` or `AUTH_TOKEN`, which the app passes to the run. **Try**
   sends one request per picked route and shows how each answered (without
   cookies, as the load test sends them). If a picked route answered anything but
   2xx/3xx (a 401, a 404, a 500, or nothing), Next asks first, with what each
   answer most likely means: sent like that it fails on every request of the load
   test, and a run where many requests fail is marked not to be trusted.
   Continue anyway when you expect the errors (testing how the app handles bad
   requests). Bring your own k6 script instead if you have one; the app passes it
   only `THINK_TIME`.
5. **Features.** Which components each feature needs, which decides every
   availability figure. Each starts from what its requests touch: the gateways
   they pass, the service that handles them, and what that service depends on
   (`depends_on` and the service names in its environment), each with its reason.
   A service no feature reaches stops the step: the load test would send it
   nothing, so it couldn't be measured and the run would be marked not to be
   trusted. Pick one of its routes, add it to a feature, or leave it out of the
   analysis with one click.
6. **Users.** How many virtual users to hold, one level after another (several
   make a sweep), for how long (hours, minutes, seconds, in that order, as the
   analyzer reads it back), and the pause between a user's requests.
7. **Save.** The spec and the load script (`load.js`) are previewed, the spec is
   checked by the analyzer itself (`python3 -m analyzer.spec --json`), and both
   are written to your project's `perf/` folder by default (a whole path: a
   relative one is refused). Saving over an existing file keeps the old one as a
   dated `.bak` file, never over an earlier backup. While a run is going, its
   folder can't be saved into: a sweep reads its load script again at every
   level. On Windows and macOS (Docker Desktop) the spec runs k6 on Docker's own
   network and sends it to `host.docker.internal`, since k6's container can't
   use this machine's network there.
8. **Run.** Your app's status again, and the **monitoring stack** the
   measurements come from (Prometheus, the OpenTelemetry collector, cAdvisor in
   `observability/`). If it isn't running, its settings are filled in from what
   the app already knows: your app's network, metrics only on this machine
   unless you want traces in Honeycomb, any extra scrape jobs. They are shown
   as the `observability/.env` they make (the old one kept as a `.bak`), and
   **Save and start** starts it; once up, every scrape target is shown up or
   down. Then a check of what the run needs, which only reads (it also catches
   a load script that isn't there, and request metrics without response
   times), and **Start**.
   **The full run** is the default and the recommended one: the load test,
   fault injection, and all four analysis layers. Because fault injection
   stops your app's containers for a moment, Start shows the analyzer's own
   plan of what it stops and asks once. *The load test only* skips it, leaving
   the availability figures unmeasured. The output follows live. **Stop** asks
   the analyzer to stop as Ctrl+C does (on Windows, where a program can't send
   Ctrl+C to one it started, through its standard input:
   `analyzer/stop_signal.py`): it starts again a container it had stopped and
   removes its k6 container; afterwards the app also starts again any of your
   app's containers that was running when the run began and was killed since,
   judged by Docker's own clock (a job that finished, or a container stopped
   another way, is left alone). If the app itself goes away mid-run, the analyzer
   stops the same way. While a run is going, the app and the monitoring stack
   can't be started from here. The run
   stays in view, with Stop, even after the page was reloaded (**A run is going**
   at the top leads to it). In a sweep, fault injection comes first, before the
   load levels; in a single run, after the load test. The reports land in
   `analysis/` next to the spec; **See the results** opens them.

**Results** (the switch at the top, open at any time) shows what the analysis
found, without setting anything up first: whether its numbers can be trusted
(the run's trust check, with each check passed or failed), the plain-language
summary section by section, the charts with what each shows (per feature where
they are about one), and the analyzer's own reports at the end. A load sweep
shows its comparison across levels, and each level on its own. It finds results
next to the saved spec, where the run wrote them, in every compose project
Docker knows, in this repository and in any folder you open (a few folders
deep), so results of runs from before, made by the app or by hand, are found
too. Only the run this session started counts as current; anything else is
marked as older results with its date, also when the spec was saved again after
them or a later run there didn't finish. A file or chart left in the folder by
a different run than the report is marked, and such charts are hidden until
asked for.

The wizard keeps what you filled in, in this browser, so reloading the page or
restarting the app (it keeps its address) picks up where you left off, with
**Start over** to clear it. The password, token or key is never kept: you enter
it again. If the compose files or the code changed since, read them again in
step 2.

It starts no containers until you press **Start it for me** (your app), **Save
and start** (the monitoring stack) or **Start** (the run), and edits none of
your app's files; it writes the spec and the load script you save, and
`observability/.env` when you start the monitoring from it. Start runs
`python3 -m analyzer <spec> --skip-chaos`, or without `--skip-chaos` when you
chose fault injection, from this repository.

### How it decides

Only general signals are used, never the name of a particular app: whether a
service is built from source, what its image is, what its name says (`migrate`,
`frontend`, `-lb`, `-pool`), its `depends_on` and the service names in its
environment variables (`CATALOG_URL=http://catalog:8000`). Known images live in
[`backend/Data/infrastructure-images.json`](backend/Data/infrastructure-images.json):
add a database, broker or gateway there and it is recognised, no code change
needed. What it can't place is marked *unsure* rather than guessed.

Routes are found the same way, as data: the patterns in
[`backend/Data/route-patterns.json`](backend/Data/route-patterns.json) (see
[What it can read](#what-it-can-read)); add a framework there. Tests,
dependencies and build output aren't searched. A route's path includes every
prefix put in front of it: its controller's, its router's (a group inside a
group too), and where that router is mounted, in the same file or another one
(`app.use('/api', require('./routes'))`, `app.include_router(books.router,
prefix="/books")`, `path('api/', include('books.urls'))`): imports are followed
to the file the router is declared in, and a prefix kept in a constant
(`prefix=settings.API_V1_STR`) is looked up. Prefixes set for the whole app
outside the routes (NestJS `setGlobalPrefix`, Spring's `context-path`,
Laravel's automatic `/api` for `routes/api.php`) aren't read: correct those
paths on the Routes step. How a route is reached from
outside is read from the gateways' own configuration: nginx and openresty files
the compose file mounts or builds in (`location`, `proxy_pass`, `upstream`,
`set` variables), Kong's declarative configuration (`strip_path`, service paths,
upstreams), and Traefik labels (`PathPrefix`, `stripprefix`). Regular-expression
locations and routes are skipped and said so. A route no published port leads
to is listed with its path inside the service, for you to give its full address.

Each route starts with an example request, as Swagger shows one: the handler's
parameters say what goes in the path, the query and the body, and the types they
name are read from the service's code to build an example body. The example
values for types come from
[`backend/Data/request-types.json`](backend/Data/request-types.json), across
languages, including MongoDB and BSON types (an `ObjectId` is a valid 24-hex
string, a `BsonDocument` is `{}`, `Decimal128` is a number). Field names refine
them (`email`, `password`, `url`, `phone`). Id fields are left out of bodies
(`_id`, `[BsonId]`, `@Id`, an optional `id`): a fixed id sent on every request
fails after the first insert. A path value is a placeholder, and the app says
so; a body field that is usually unique (an email, a user name) gets a warning
that only the first request will succeed.

## What it can read

Keep this table current: it is what the app supports, per language. Change it
with `backend/Data/route-patterns.json`, `backend/Data/request-types.json`,
`backend/Routes/RouteMounts.cs`, `backend/Routes/TypeIndex.cs`,
`backend/Routes/RequestShapes.cs` or `backend/Drafting/PortGuesser.cs`.

| Language | Routes | Prefixes from | Example bodies from | Runtime (from the image) |
|----------|--------|---------------|---------------------|--------------------------|
| Python | FastAPI, Flask, Sanic, aiohttp, Litestar, Quart, Django (`urls.py`, DRF routers), Falcon | `APIRouter(prefix=)`, `Blueprint(url_prefix=)`, `include_router`, `register_blueprint` (its `url_prefix` replaces the blueprint's), `mount`, Django `include`; across files | Pydantic models, dataclasses, TypedDict, enums (`Field(alias=…)`) | yes |
| C# | ASP.NET Core controllers and minimal APIs, FastEndpoints | `[Route]` on a controller, `MapGroup` (also inside another), endpoint extension methods called on a group | classes, records, enums (`[JsonPropertyName]`, `[BsonId]`, `[BsonRepresentation]`, `[JsonIgnore]`); camelCase like ASP.NET | yes, with the .NET version's default port |
| Java | Spring, JAX-RS (Quarkus, Jersey), Micronaut | `@RequestMapping`, `@Path`, `@Controller` on the class | classes, records, enums (`@JsonProperty`, `@Id`, `@JsonIgnore`) | yes |
| Kotlin | Spring, Micronaut, Ktor | as Java; Ktor `route("/x") { }` | data classes, enum classes | from the code |
| TypeScript, JavaScript | Express, Fastify, Koa, Hono, Elysia, hapi, NestJS | `app.use('/x', router)`, Hono `route` and `basePath`, Fastify `register(…, { prefix })`, Elysia `prefix`, NestJS `@Controller`; across files (`require`, `import`) | TypeScript classes, interfaces, enums (NestJS `@Body`, `@Param`, `@Query`) | Node.js, Deno (8000), Bun (3000) |
| Go | net/http (incl. Go 1.22 `"GET /x"`), gin, echo, chi, fiber, gorilla/mux | `Group` (also inside another), `PathPrefix().Subrouter()`, a function called with a group, chi `Route` and `Mount` | structs with `json` tags, read where the handler binds them (`ShouldBindJSON`, `Bind`, `Decode`) | yes, else from the code |
| Rust | Actix Web, Rocket, Axum | Axum `nest("/x", routes())` | serde structs (`rename`, `rename_all`, `skip`, `default`) through `Json`, `Path`, `Query` | yes, else from the code |
| Ruby | Rails `routes.rb` (incl. `resources`), Sinatra, Grape | `namespace`, `scope` | no | yes |
| PHP | Laravel and Lumen (incl. `apiResource`), Slim, Symfony | `Route::prefix()->group`, `Route::group(['prefix' => …])`, Symfony `#[Route]` on the class | no | yes, PHP-FPM 9000, Apache 80 |
| Elixir | Phoenix router (incl. `resources`) | `scope` | no | yes |
| Scala, Java | Play (`conf/routes`) | no | no | Scala (sbt) |
| Swift | Vapor | no | no | yes |
| Dart | shelf_router, dart_frog | no | no | yes |
| C++ | Crow, Drogon, cpp-httplib, Pistache, Oat++ | no | no | gcc, clang |
| Clojure, Groovy | Compojure; Micronaut | Micronaut `@Controller` | no | Clojure |

A service in any other language (Haskell, Zig, Nim, OCaml, Crystal, Perl,
Erlang and more, recognised by their file extensions) is named on the Routes
step: the app says it couldn't read it, and asks you to add its routes by hand
or to bring your own k6 script. The same happens, worded differently, for a
supported language where no route was recognised: a queue consumer has none.

## Development

```
backend/          ASP.NET Core (.NET 10): the API, scanning, drafting, spec writing
backend.Tests/    xUnit; Fixtures/ holds captured compose projects (capture.py)
frontend/         React + TypeScript + Mantine, built with Vite into backend/wwwroot
```

Run the two halves separately to work on the page with live reload:

```bash
SETUP_TOKEN=dev dotnet run --project ui/backend      # the API, with a fixed key
cd ui/frontend && npm run dev                        # then open http://localhost:5173/?token=dev
```

Tests:

```bash
dotnet test ui/backend.Tests      # includes asking the analyzer to accept generated specs
cd ui/frontend && npm test        # the wizard's rules and the components step
```

`backend.Tests/Fixtures/capture.py <project-dir> <name>` records another compose
project as test data, with its paths generalised and passwords masked. The
fixtures also hold the gateways' configuration and trimmed copies of the route
declarations, for the route finder's tests.

The server listens on 127.0.0.1 only, refuses requests whose host name isn't a
local one, and needs the launch key on every API call, because it can run
`docker`, start the analyzer and write files.
