# Worked example: the FINKI blogging platform

The system the analyzer was first built against, and the case study of the
master's thesis „Моделирање и емпириска валидација на перформансите и
надежноста на микросервисна блогинг-платформа". The application itself lives in
its own repository,
[FINKI-blogger-2](https://github.com/GjorgiNechovski/FINKI-blogger-2); everything
needed to *measure* it lives here.

| File | What it is |
|------|------------|
| [`../finki-blogger.toml`](../finki-blogger.toml) | the spec: services, infrastructure, operations |
| [`../finki-blogger.measured.toml`](../finki-blogger.measured.toml) | the last measured values (rewritten by every live run) |
| [`load.js`](load.js) | k6 load script: login, list, create, read, like and comment |
| [`telemetry.compose.yaml`](telemetry.compose.yaml) | overlay on the app's compose file that switches on Kong metrics and trace export |
| [`prometheus/kong.yml`](prometheus/kong.yml) | Prometheus scrape job for the Kong gateway |

## Analyse it without running anything

The measured values are checked in, so the four analysis layers run straight
away:

```bash
python3 -m analyzer.run_all systems/finki-blogger.toml
```

## Measure it live

Clone both repositories next to each other:

```
Desktop/
├── FINKI-blogger-2/
└── Microservice-Scripts/
```

1. **Start the app with telemetry switched on**:
   ```bash
   cd FINKI-blogger-2
   docker compose -f docker-compose.yaml \
       -f ../Microservice-Scripts/systems/finki-blogger/telemetry.compose.yaml \
       up -d --build
   ```
2. **Start the observability stack** with this system's settings in
   `observability/.env`:
   ```bash
   cd ../Microservice-Scripts
   cp observability/.env.example observability/.env
   ```
   ```ini
   HONEYCOMB_API_KEY=<your key>
   APP_NETWORK=finki-blogger-backend
   SCRAPE_DIR=../systems/finki-blogger/prometheus
   BEYLA_SERVICE_NAMESPACE=finki-blogger
   ```
   ```bash
   docker compose -f observability/docker-compose.observability.yaml up -d
   ```
   <http://localhost:9090/targets> should show `kong` (one per gateway
   instance), `cadvisor` and `otel-collector` as UP.
3. **Make sure the load script can log in.** It authenticates as
   `test@example.com` / `Test123!`; on a fresh database, register that user
   first.
4. **Run the full pipeline**:
   ```bash
   python3 -m analyzer systems/finki-blogger.toml
   ```
   That is a load sweep at 50, 100, 200 and 500 virtual users, one fault
   injection pass, and the analysis. It writes `analysis/load-<N>/` per level
   and `analysis/scaling-report.txt`. Add `--skip-chaos` to keep the measured
   repair rates; fault injection really does stop containers.

Tear down in reverse order: the stack first, then the app with the same two
`-f` files.

## Things to know

- **`[instances]` in the spec** must match the architecture you are running.
  It currently declares the redundant gateway, databases and broker
  (`kong = 2`, `blogging-db = 2`, ...). Against the original single-instance
  architecture, remove that table, otherwise the model credits redundancy that
  does not exist.
- **Think-time:** `load.js` reads `THINK_TIME` (default `0`, i.e. no pause
  between iterations, which measures capacity). `THINK_TIME=2` restores the
  human-paced workload. Results from the two are not comparable.
- **Windows/macOS:** k6 can't use `--network host` there; uncomment
  `docker_network` and `env` under `[load]` in the spec.
