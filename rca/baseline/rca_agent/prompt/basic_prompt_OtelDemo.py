cand = """## POSSIBLE ROOT CAUSE REASONS:

- productCatalogFailure
- recommendationCacheFailure
- adManualGc
- adHighCpu
- adFailure
- cartFailure
- paymentFailure
- paymentUnreachable
- imageSlowLoad
- kafkaQueueProblems
- emailMemoryLeak
- loadGeneratorFloodHomepage
- failedReadinessProbe
- llmInaccurateResponse
- llmRateLimitError

## POSSIBLE ROOT CAUSE COMPONENTS:

- accounting
- ad
- cart
- checkout
- currency
- email
- flagd
- fraud-detection
- frontend
- frontend-proxy
- image-provider
- load-generator
- payment
- product-catalog
- product-reviews
- quote
- recommendation
- shipping"""

schema = f"""## TELEMETRY DIRECTORY STRUCTURE:

- You can access the telemetry directory in our microservices system: `dataset/OtelDemo/telemetry/`.

- This directory contains subdirectories organized by a date (e.g., `dataset/OtelDemo/telemetry/2026_04_20/`).

- Within each date-specific directory, you’ll find these subdirectories: `metric`, `trace`, and `log` (e.g., `dataset/OtelDemo/telemetry/2026_04_20/metric/`).

- The telemetry data in those subdirectories is stored in CSV format (e.g., `dataset/OtelDemo/telemetry/2026_04_20/metric/metric_container.csv`).

- Available dates are 2026_04_19 through 2026_04_24. The first and last are partial: data begins 2026-04-19 03:40 UTC and ends 2026-04-24 19:25 UTC.

## DATA SCHEMA

1.  **Metric Files**:

    1. `metric_app.csv`:

        ```csv
        timestamp,rr,sr,cnt,mrt,tc
        1776570000,100.0,100.0,4,15485.75,accounting
        ```

    2. `metric_container.csv`:

        ```csv
        timestamp,cmdb_id,kpi_name,value
        1776570120,ad,app_ads_ad_requests_total-app_ads_ad_request_type=TARGETED-app_ads_ad_response_type=TARGETED,1
        ```

2.  **Trace Files**:

    1. `trace_span.csv`:

        ```csv
        timestamp,cmdb_id,parent_id,span_id,trace_id,duration
        1776569992864,flagd,,3c42728b7b227146,4e82c5aa918f184e215a7bf33421f07c,599997
        ```

3.  **Log Files**:

    1. `log_service.csv`:

        ```csv
        log_id,timestamp,cmdb_id,log_name,value
        68737053aa7a7b7e833bfc2572819044,1776570005,kafka,kafka.utils.Log4jControllerRegistration$,"Registered kafka:type=kafka.Log4jController MBean"
        ```

{cand}

## CLARIFICATION OF TELEMETRY DATA:

1. This microservice system is an e-commerce web store (the OpenTelemetry Astronomy Shop demo): a browser frontend behind an Envoy proxy, with services for the product catalog, cart, checkout, payment, shipping, currency, email, ads, recommendations, product reviews, quotes, accounting and fraud detection, plus a load generator driving synthetic user traffic.

2. Faults in this system are introduced by **feature flags**. Each flag turns on a specific defect in a specific service — for example `paymentFailure` makes the payment service throw on a fraction of charges, and `adManualGc` makes the ad service force a full garbage collection on every ad request. The root cause reason is therefore the name of the feature flag that was switched on, and the root cause component is the service whose behaviour the flag changes. Note that the two are not always the same service: `paymentUnreachable` is read in the **checkout** service, which then cannot reach payment.

3. The `metric_app.csv` file only contains four KPIs: rr, sr, cnt, and mrt. `rr` is the percentage of transactions that returned a response, `sr` the percentage that returned without an error, `cnt` the number of transactions, and `mrt` the mean response time in milliseconds. `tc` is the service that served the transaction. In contrast, `metric_container.csv` records a wide variety of KPIs — the specific names are in the `kpi_name` field.

4. `kpi_name` in `metric_container.csv` is a Prometheus metric name followed by any remaining label values, joined with `-` (e.g. `app_ads_ad_requests_total-app_ads_ad_request_type=TARGETED`). The main families are `system_*` and `container_*` (host and container CPU, memory, disk, network), `http_*` and `rpc_*` (request counts and durations), `jvm_*`, `v8js_*` and `process_*` (per-runtime internals), `kafka_*` and `postgresql_*` (infrastructure), `traces_span_metrics_*` (per-span call counts and durations), and `app_*` (application-specific counters). Histogram bucket series are not included, but every latency metric is present as a `_sum` and a `_count`, whose ratio is the mean.

5. In different telemetry files, the timestamp units and cmdb_id formats may vary:

- Metric: Timestamp units are in seconds (e.g., 1776570120).

- Trace: Timestamp units are in milliseconds (e.g., 1776569992864).

- Log: Timestamp units are in seconds (e.g., 1776570005).

6. `cmdb_id` is the service name and is the same identifier across all three telemetry types, so metrics, traces and logs can be joined on it directly. `metric_container.csv` additionally carries a few infrastructure components that emit no traces (`jaeger`, `otelcol-contrib`, `opensearch`, `prometheus`, `grafana`, `postgresql`, `valkey-cart`) and one host-level pseudo-component (`otel-demo-prod-01`) holding the `system_*` metrics.

7. `parent_id` in `trace_span.csv` is empty for a root span. Spans for the operation `flagd.evaluation.v1.Service/EventStream` are normal ~600 second long-poll reconnection cycles for feature flag streaming; they appear in every observation window regardless of incidents and are background noise, not a symptom.

8. Please use the UTC time zone in all analysis steps. All timestamps in this dataset are UTC."""
