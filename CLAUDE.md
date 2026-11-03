# CLAUDE.md — operating guide for agents

You have been asked to analyse the performance and reliability of someone's
microservice system with this tool. This file is what you need to do that
without hand-holding. **Read all of it before running anything.** The user-facing
overview is [README.md](README.md); this file adds the judgement calls, the
safety rules, and the traps that took this project months to find.

## What the tool does

Given a Docker Compose system, it measures the numbers the models need from the
running system itself (arrival rate λ, service rate μ, replica count c, repair
rate), then runs four analysis layers: M/M/c queueing (speed, bottleneck,
replica sizing), Markov-chain availability per component, a reliability block
diagram + fault tree per user operation (single points of failure), and a
sensitivity analysis (which change buys most). The human supplies **structure
only** (service names, which components each operation needs); you never type
a measured number.

## Rule zero: what you may run without asking

| Tier | Commands | Rule |
|---|---|---|
| **Safe** | reading files; `python3 -m analyzer.tests`; `python3 -m analyzer.run_all <spec>`; `python3 -m analyzer <spec> --no-measure`; `python3 -m analyzer.collect <spec> --discover`; `python3 -m analyzer.collect <spec>` (preview); `python3 -m analyzer.chaos <spec>` (plan only) | Always fine. |
| **Generates load** | `python3 -m analyzer <spec> --skip-chaos`; `python3 -m analyzer.collect <spec> --run-load`; running k6 | Only on an environment the human has said is theirs to hammer (local or dev). Otherwise ask. |
| **Stops containers** | `python3 -m analyzer.chaos <spec> --run`; **`python3 -m analyzer <spec>` with no flags** | **Always ask first, even locally.** Show the output of `python3 -m analyzer.chaos <spec>` with your question so the human sees exactly what would go down. |
| **Changes their app** | editing their source, Dockerfiles or compose; adding instrumentation | Ask first, unless told the app is a throwaway or dev copy. Prefer a separate compose overlay to editing their base file. |

**The bare command `python3 -m analyzer <spec>` is the destructive one.** It
runs the load test *and* fault injection by default, deliberately. It is never
a first step.

## The job, in order

### 1. Survey their system (read-only)

Read their compose file(s) and Dockerfiles. Note:

- services and languages, replicas, the gateway, databases, message brokers,
  and services that receive no HTTP traffic (e.g. queue consumers);
- **the port each service actually listens on** (read the `CMD`; do not assume
  8000: .NET Kestrel and Spring default to 8080);
- which services call each other synchronously (a caller's measured latency
  includes its callees', see [traps](#reading-results-traps));
- restart policies, healthchecks, and which stateful components have no
  persistence. These decide what fault injection measures and what it breaks
  (see step 7).

Once their app is up, `docker network ls` gives the network name and
`docker compose ls` the compose project name; you need both.

### 2. Decide where per-service request rate and latency come from

This step decides whether Layer 1 works at all. The analyzer needs, for every
service in the spec, a **request counter and a latency histogram whose service
label equals the service's name in the spec** (normally its compose service
name). Exactly two sources are known to work:

**A. A gateway that exports per-upstream metrics**, such as Kong with its
Prometheus plugin. Write a scrape-job file and point `SCRAPE_DIR` at its folder
(see [observability/README.md](observability/README.md)). Worked example:
[systems/finki-blogger/](systems/finki-blogger/) (`prometheus/kong.yml` plus
`telemetry.compose.yaml`, which opens Kong's admin API).

**B. OpenTelemetry instrumentation in every service**, exporting HTTP-server
metrics over OTLP to the stack's collector with `OTEL_SERVICE_NAME` set to the
compose service name. Use this when there is no metrics-exporting gateway,
which is most apps; a plain nginx gateway exports nothing per upstream.

**Do not rely on Beyla.** It is in the stack for zero-code traces, but on
newer kernels it fails to load eBPF entirely (check `docker logs
microservice-observability-beyla-1` for `Unable to load eBPF`), and where it
does run it names services after their process (`nginx`, `python3`), which never
matches the spec.

Recipe for **B**, verified twice with this repository on a FastAPI app that had
no instrumentation:

- add `opentelemetry-distro` and `opentelemetry-exporter-otlp` to the
  requirements;
- in the Dockerfile, run `opentelemetry-bootstrap -a install` after
  `pip install`, and prefix the start command with `opentelemetry-instrument`.
  A separate `Dockerfile.otel`, selected by the overlay, leaves theirs untouched;
- set these environment variables in a separate overlay file, so their base
  compose stays untouched:

  ```yaml
  OTEL_SERVICE_NAME: <compose service name>     # must match the spec
  OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4318
  OTEL_EXPORTER_OTLP_PROTOCOL: http/protobuf
  OTEL_METRICS_EXPORTER: otlp                     # not "none"
  OTEL_METRIC_EXPORT_INTERVAL: "5000"             # ms; see below
  OTEL_TRACES_EXPORTER: none                      # "otlp" only if traces are wanted
  OTEL_LOGS_EXPORTER: none
  ```

- **Set the export interval.** The SDK default is 60 s, so a new service takes
  two minutes to produce a measurable rate, and each load level's rate is
  blurred across a minute of the previous one.
- **Leave tracing off for measurement runs** unless the human wants traces.
  Instrumentation costs capacity, and every rate the tool reports describes the
  *instrumented* build. On the test app at 16 users: uninstrumented 679 req/s,
  metrics only 522, metrics and traces about 330. Tell the human which build
  the numbers describe.
- Each replica must report a distinct `service.instance.id`; the SDKs set one
  automatically.

That produced `http_server_duration_milliseconds_*` with
`service_name="catalog"` etc., which the analyzer picked up with no
configuration. For other languages the environment variables are the same;
instrument with the Java agent (`-javaagent:opentelemetry-javaagent.jar`), the
.NET OpenTelemetry SDK or auto-instrumentation, or
`@opentelemetry/auto-instrumentations-node`. Only the Python route has been
verified against this collector, so confirm with `--discover` (step 6).

Note that the FINKI example sets `OTEL_METRICS_EXPORTER=none` because Kong
supplies its metrics; for route B it must be `otlp`.

**After rebuilding or scaling services behind an nginx gateway, restart the
gateway.** nginx resolves upstream addresses once at startup, so new replicas
get no traffic and a changed address fails.

### 3. Write the spec

Copy [systems/TEMPLATE.toml](systems/TEMPLATE.toml); every option is documented
inline. The spec can live in their repository. Its measured values are written
next to it as `<name>.measured.toml`, so if it does, that file lands in their
repo too.

- `compose_project`: their compose project name. Set it. Containers are found by
  compose service name, so without it another project's `redis` on the same
  machine is counted as a replica, and fault injection refuses to run.
- `services`: the application tiers you want queueing analysis for. Each needs
  metrics from step 2.
- `infrastructure`: shared components (gateway, databases, broker, cache).
- `[operations]`: which components each user-facing operation needs. **This is
  a modelling decision, not clerical work**: it drives every availability
  result. Draft it from the code, then confirm it with the human.
- `[containers]`: only where a spec name differs from the compose service name.
- `[readiness]`: for components without a healthcheck, the TCP port meaning
  "the app is serving". Verify the port per language.
- `[instances]`: only for infrastructure that genuinely runs more than one
  instance with failover. Declaring redundancy that does not exist makes the
  availability figures fiction.
- `[failure_rates]`: failure rates cannot be observed before a system has run in
  production, so these are estimates. Label them as estimates to the human.

### 4. Write the k6 load script (if load testing is allowed)

It is always specific to their endpoints, payloads and auth. Cover every
operation in `[operations]`. A relative `[load].script` is read from the spec's
own directory. Log in once in `setup()`, and make sure the user and data it
needs exist; a failed `setup()` makes k6 exit within seconds.

k6 here is a **closed-loop** generator: each virtual user waits for a response
before sending the next request. A pause between iterations ("think-time")
therefore caps the offered load at roughly *users ÷ pause*, whatever the system
could handle, and the results describe the generator rather than the system.
Decide deliberately: no pause to find capacity, a pause to model human users.
Never compare runs made with different settings.
[systems/finki-blogger/load.js](systems/finki-blogger/load.js) reads
`THINK_TIME` for exactly this reason. The request mix (what share of
iterations does each operation) is a guess unless the human gives you one; say
which you used.

Secrets for the script go in `[load].env_passthrough` plus a git-ignored `.env`
in the directory you run from, never in the spec.

**Choose sweep levels by calibrating first.** A few one-minute runs at, say, 1,
2, 5 and 20 users show where throughput stops rising; pick levels that bracket
that point. Without think-time a small app can level off below 5 users, so
FINKI's 50 to 500 would all be deep saturation. Hold each level at least two
minutes.

### 5. Bring everything up

1. **Their app first.** It creates the network the stack joins.
2. `cp observability/.env.example observability/.env`, then set `APP_NETWORK`,
   `SCRAPE_DIR` if you wrote a gateway job, and `HONEYCOMB_API_KEY` from the
   human. Without a key yet, a placeholder still lets metrics flow, which is all
   the analyzer reads; only traces fail to reach Honeycomb. Tell the human.
3. From this repo's root:
   `docker compose -f observability/docker-compose.observability.yaml up -d`.
4. <http://localhost:9090/targets> should show `cadvisor`, `otel-collector` and
   any gateway job as **UP**.

After changing replica counts, wait a minute before measuring: a removed
container keeps being reported for about 60 s.

### 6. Discover, then measure

1. `python3 -m analyzer.collect <spec> --discover`. Every spec service must
   appear under *services seen with traffic*.
2. Drive traffic for **at least a minute** before measuring. A rate needs at
   least two Prometheus scrapes, so straight after setup a service can be
   listed by `--discover` yet read as *not measured*.
3. `python3 -m analyzer.collect <spec>` previews λ, μ and c per service.

If a service is missing: either its telemetry name does not match the spec
(fix `OTEL_SERVICE_NAME` or `[containers]`), or **it is broken. Read its logs.**
A missing service has repeatedly turned out to be crash-looping, not idle,
including after an instrumentation change that broke its build.

### 7. Run the analysis

- `--no-measure`: analyse the saved measurements only. Safe.
- `--skip-chaos`: load test and measure, no containers stopped.
- No flags: the full pipeline, including fault injection. **Ask first.**

A list in `[load].vus` makes it a sweep: one fault-injection pass, then
load, measure and analyse per level, then a cross-level scaling report. Output
goes to `analysis/` (or `--out`): per level, `load-<N>/` holds the reports, the
level's `measured.toml` and k6's own summary in `k6-output.txt`. A sweep does
**not** write `<name>.measured.toml` next to the spec, and `run_all` and
`--no-measure` read only that file; to re-analyse one level, copy its
`measured.toml` there. A sweep takes many minutes. If you redirect its output
to a file, run `python3 -u` or the log stays empty until the end.

**Before asking to run fault injection**, check what it would actually measure,
and include this in your question:

- Recovery means "healthy" for a component with a healthcheck, and "its
  `[readiness]` port opens" for one without. For a stateful component without
  a healthcheck a port opening is not recovery: a cache without persistence
  comes back empty in a second, and whatever depended on its data may stay
  broken.
- With restart policy `no`, nothing in their system restarts a stopped
  container; the tool's own `docker start` does. The recovery time is then the
  tool's, not the system's. Say so in the results.
- Stopping a component without persistence loses its data.

Without fault injection every recovery time is a placeholder, marked
`repair_rate_measured = false` in the measured file, and the reports say
**NOT MEASURED**. Report availability figures from such a run as illustrations
only; the structural results (single points of failure, which operation needs
what) still hold.

## Before you believe any number

Check all of these. If one fails, the run is invalid: say so, and don't report
its numbers.

1. **cAdvisor stayed up for the whole run.** Query Prometheus for
   `avg_over_time(up{job="cadvisor"}[<run length>])`; it must be 1. One missed
   scrape makes a 5-replica service read as 1 replica.
2. **Replica counts** in the measured file match what was running.
3. **k6 ran for its full duration and its checks mostly passed.** Read
   `k6-output.txt`: the duration, the share of checks passed and
   `http_req_failed`. A run of a few seconds means `setup()` failed. Compare
   k6's request rate with the measured arrival rates; they agreed within 1 to 2%
   in testing, slightly lower on the first level, whose window includes k6's
   startup.
4. **Every spec service has λ and μ.**
5. **Comparing two runs?** Same think-time, same dataset size, same load levels,
   same instrumentation, same host conditions. A database that grew between
   runs changes query cost on its own.
6. **Host saturation.** Check per-container CPU. Everything shares one host, and
   `nproc` counts SMT threads, not physical cores.
7. **Recovery measured?** If the report says recovery was NOT MEASURED, its
   availability figures are placeholders.

## Reading results: traps

- **Latency-based μ is poisoned under load.** Mean latency includes queueing,
  so `service_rate` collapses as load rises; feeding it back into a queueing
  model double-counts congestion. The sweep therefore recovers
  `service_rate_effective` from the throughput plateau. A replica
  recommendation shown as `n/a` is deliberate: **never fill one in.**
- **In a closed loop every service plateaus when the bottleneck does.** The
  plateau method then marks *all* of them saturated and recommends replicas for
  each, but only the true bottleneck is saturated. Cross-check with CPU per
  replica: a single-process replica (Python, Node) tops out near one core.
  ```
  sum by (container_label_com_docker_compose_service)
    (rate(container_cpu_usage_seconds_total{container_label_com_docker_compose_project="<project>"}[2m]))
  ```
  divided by the replica count. In testing the tool called a service "maxed out
  at ~97 req/s" while it used 0.45 cores per replica; with the real bottleneck
  doubled, it served 165 req/s. Trust `service_rate_effective` and the "add
  replicas" verdict only for the service whose CPU confirms it.
- **A caller's latency includes its callees'.** A service that calls another
  synchronously reports the other's time, queueing included, as its own, so
  its μ is not its own cost.
- **μ is not constant.** On a shared host the CPU cost per request rose with
  load (contention, clock behaviour); throughput can even fall between levels.
- **"Supports about N users" means N closed-loop virtual users at the tested
  think-time.** With no think-time that is far fewer than real people.
- **The model's response times do not match a closed loop.** M/M/c assumes open
  arrivals; at 8 users it predicted 56 ms where 20 ms was measured. Quote
  measured latency; use the model for comparisons.
- **"Saturated" while the CPU idles** means a serialisation point: a small
  connection pool, a pooler's child limit, a single worker process. Find what
  everything queues behind before adding hardware.
- **The model has no notion of a shared host or connection limits.** Its
  availability predictions have been accurate; its performance predictions have
  missed in both directions. (One controlled A/B test so far: availability
  matched to five decimals, peak throughput was off by 1.9× and 3.5×.) Adding
  replicas on one host can *lower* throughput.
- **Fixing a bottleneck moves it.** Re-sweep after every change.
- **The diagnosis says *scale out* or *optimise*.** The specific cause still
  needs code or traces.

## Looks like a tool bug, isn't

| Symptom | Cause |
|---|---|
| *lambda not measured* right after setup | counters too new; drive traffic a minute and retry |
| stack won't start: `APP_NETWORK` missing | `observability/.env` not filled in, or the app isn't up yet |
| can't rebuild their app: network has active endpoints | the stack is attached to it; stop the stack first |
| `compose service 'x' has containers in N compose projects` | another project on the machine has a service of that name; set `compose_project` in the spec |
| replica count too high just after scaling down | a removed container counts for about 60 s; wait a minute |
| new replicas get no traffic, or 502s after a rebuild | an nginx gateway caches upstream addresses; restart it |
| report says recovery NOT MEASURED | fault injection didn't run (expected with `--skip-chaos`); the availability figures are placeholders |
| every MTTR exactly 60 s | the readiness probe never succeeded: check the `[readiness]` ports. On Windows/macOS the probe runs inside the container through `docker exec`, so it also needs `bash` or busybox `nc` in the image |
| k6 can't reach the app (Windows/macOS) | no `--network host` there; set `[load].docker_network` and pass the base URL in `[load].env` |
| sweep log empty while it runs | Python buffers redirected output; use `python3 -u` |

## Ask the human about

- whether load testing and fault injection are allowed, and on which environment;
- changing their code or images (instrumentation), and whether traces are wanted
  (they cost capacity);
- the operations and which components each needs, i.e. what "down" means;
- the request mix and pacing (think-time), or how many users they expect;
- before fault injection: which components may lose data when stopped;
- whether production is also a single host (the model assumes nothing shared);
- failure-rate estimates, when there is no failure history;
- the Honeycomb API key.

## If you change this repository

- The engine is standard-library only; numpy and matplotlib only inside
  `analyzer/figures/`.
- Never hard-code an application or metric name in the engine; extend the
  candidate lists in `analyzer/collect/collector.py`.
- Every technical report has a plain-language `*-explained.txt` twin.
- Never invent a number: report `unmeasured` or `n/a` with the reason, and mark
  placeholders as such (as `repair_rate_measured = false` does).
- Fault injection only stops and starts containers; it never removes them.
- A new spec option goes into `systems/TEMPLATE.toml` too. For a generic tool,
  the template is the product.
- Secrets only in git-ignored `.env` files.
- Run `python3 -m analyzer.tests` before and after.
