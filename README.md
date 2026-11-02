# Microservice-Scripts

A generic performance and reliability analyzer for microservice systems running
on Docker Compose. You describe a system by its names and structure; the
analyzer **measures every number the models need from the running system
itself** (arrival rates, service rates, replica counts, recovery times) and
feeds them through four layers of analysis:

| Layer | Model | Answers |
|-------|-------|---------|
| 1 | M/M/c queueing, per service | How fast is it, where is the bottleneck, how many replicas does each service need? |
| 2 | Continuous-time Markov chains, per component | How available is each component (MTTF, MTTR)? |
| 3 | Reliability block diagram + fault tree, per operation | How available is each user operation, and what are its single points of failure? |
| 4 | Sensitivity analysis | Which change buys the most? |

The engine knows nothing about any particular application, language or gateway.
It was built for, and validated against, the master's thesis „Моделирање и
емпириска валидација на перформансите и надежноста на микросервисна
блогинг-платформа" (Ѓорги Нечовски, 2026), whose case study,
[FINKI-blogger-2](https://github.com/GjorgiNechovski/FINKI-blogger-2), ships here
as a worked example.

## Requirements

- **Python 3.11+**. The analyzer itself uses only the standard library.
- **numpy + matplotlib**, only for figures (`--no-figures` skips them).
- **Docker** with Compose v2, for live measurement. The load generator, k6,
  runs in a container, so it needs no install.

## Layout

```
analyzer/        the engine: models, collectors, fault injection, reports (+ tests)
observability/   the monitoring stack it measures through (Prometheus, Grafana,
                 OTel Collector -> Honeycomb, cAdvisor, Beyla)
systems/         system specs: TEMPLATE.toml to start from, plus two examples
  example-shop.toml          a made-up shop, to show the engine is generic
  finki-blogger.toml         the thesis case study
  finki-blogger/             its load script, telemetry wiring and scrape job
```

## Try it without running anything

Both examples ship with measured values, so the analysis runs immediately:

```bash
python3 -m analyzer.run_all systems/example-shop.toml
python3 -m analyzer.run_all systems/finki-blogger.toml
```

## Analyse your own system

1. **Describe it.** Copy `systems/TEMPLATE.toml` and list your services, your
   shared infrastructure, and which components each user operation needs. Names
   only, no numbers. The spec can live anywhere, including your own project.
2. **Write a k6 load script** for your endpoints (optional, but arrival rate and
   utilization only exist while traffic flows). Point `[load].script` at it; a
   relative path is read from the spec's own directory.
3. **Start your app**, then **start the observability stack** on its network.
   See [observability/README.md](observability/README.md): set `APP_NETWORK`
   in `observability/.env`, and `SCRAPE_DIR` if you have a gateway worth
   scraping.
4. **Check what can be measured:**
   ```bash
   python3 -m analyzer.collect path/to/your-system.toml --discover
   ```
5. **Run it:**
   ```bash
   python3 -m analyzer path/to/your-system.toml
   ```
   Results go to `analysis/` (`--out` to change it), and the measured values
   to `<your-system>.measured.toml` next to the spec.

The FINKI example walks through all of this on a real system:
[systems/finki-blogger/README.md](systems/finki-blogger/README.md).

## Commands

| Command | What it does |
|---------|--------------|
| `python3 -m analyzer <spec>` | full pipeline: load test, fault injection, analysis, report, figures |
| `python3 -m analyzer <spec> --no-measure` | analyse the existing measured values only |
| `python3 -m analyzer <spec> --skip-chaos` | measure under load, but don't stop any containers |
| `python3 -m analyzer.run_all <spec>` | the four analysis layers only, printed to the terminal |
| `python3 -m analyzer.collect <spec> --discover` | what Prometheus can see for this system |
| `python3 -m analyzer.chaos <spec>` | fault-injection plan (add `--run` to actually inject) |
| `python3 -m analyzer.figures <spec>` | the figures only |

Fault injection really does stop containers. Single-instance components go
down briefly while they recover; the plan (`analyzer.chaos` without `--run`)
shows what would happen first.

## Tests

```bash
python3 -m pytest analyzer/tests/ -q
```

Each test module also runs on a bare interpreter, e.g.
`python3 -m analyzer.tests.test_queueing`.
