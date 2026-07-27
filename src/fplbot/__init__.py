"""fplBot - a scheduled Fantasy Premier League transfer-recommendation service.

The package is organised in layers, and the dependency arrows only ever point
downwards:

    handlers/   AWS entry points. Thin - they parse the event and call pipeline.
    pipeline    Orchestration: ingest -> score -> rank -> report -> send.
    report/     Rendering and delivery of the result.
    domain/     Pure logic. No I/O, no AWS, no HTTP. This is the testable core.
    sources/    One module per upstream data provider. All I/O lives here.
    models/     Pydantic schemas shared between sources and domain.
    storage/    DynamoDB and S3 adapters.
    http/       The single hardened HTTP client every source must use.
    config      Settings, tunables and constants.

The reason for the strictness: everything in `domain/` can be unit-tested with
plain Python objects and no mocking, which is where the interesting bugs live.
"""

__version__ = "1.0.0"
