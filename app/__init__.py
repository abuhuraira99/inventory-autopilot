"""
Inventory Autopilot: keeps Amazon stock quantities in step with a vendor feed.

Package layout, in the order data flows:

    config          environment settings; secrets come from here or the database
    models          the database schema, with the reasoning for each table
    db              engine, sessions, and the exclusive run lock

    core/           rules that depend on nothing else
        barcode         the zero-padding rules - read this first
        settings_store  every client-editable setting, with its default

    vendor/         reading the supplier's files
        filename        which day a file belongs to, and full vs delta
        ftp_client      FTPS/SFTP, read-only by construction
        parser          streaming pipe-delimited reader

    amazon/         talking to the Selling Partner API
        guard           refuses to transmit anything price-shaped
        lwa             refresh token -> access token
        client          rate limiting, retries, and the safety gate
        reports         reading the All Listings Report
        listings        patching one SKU's quantity
        feeds           bulk quantity updates

    engine/         the decisions
        mapping         barcode -> confirmed Amazon SKU
        decision        the client's quantity rules
        guardrails      the circuit breakers
        pusher          send, then read back to confirm
        rollback        undo
        report_builder  the five .xlsx files the client's team uses
        pipeline        the orchestrator

    security/       crypto, stored credentials, dashboard login
    routers/        the web interface
"""

__version__ = "1.0.0"
