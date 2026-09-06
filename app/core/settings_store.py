"""
Client-editable settings: the definitive list, their defaults, and safe access.

WHY THIS FILE MATTERS TO THE CLIENT
===================================
Every value here is a dashboard field. The requirement was explicit: the client
must be able to change how the system behaves without a developer editing code.
So if a behaviour might ever need adjusting, it lives in :data:`DEFAULTS` and
gets a label and help text written for a non-technical reader.

WHY IT MATTERS TO THE NEXT DEVELOPER
====================================
Read :data:`DEFAULTS` and you know everything the system can be told to do.
There is no second place where behaviour is configured.

Adding a setting is three lines in :data:`DEFAULTS` and nothing else -- no
migration, because the table is key/JSON. Deleting one is safe too: unknown
keys in the database are ignored and reported.

RULES
-----
* Reads go through :func:`get` / :func:`get_all`, which fall back to the
  default if a row is missing. A fresh database therefore behaves correctly
  before anybody visits the settings page.
* Writes go through :func:`set_value`, which validates against the declared
  type and bounds and writes an audit event. Nothing else writes the table.
* ``locked=True`` settings can never be changed through the web interface.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import time as dtime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditEvent, Setting

log = logging.getLogger(__name__)


class SettingError(ValueError):
    """A setting value was rejected. The message is shown to the user."""


@dataclass(frozen=True, slots=True)
class Spec:
    """Declaration of one setting: its default, type, bounds and wording."""

    key: str
    default: Any
    value_type: str  # int | float | bool | str | list[str] | time | enum
    label: str
    help_text: str
    category: str
    min_value: float | None = None
    max_value: float | None = None
    choices: list[str] | None = None
    locked: bool = False
    sort_order: int = 100
    #: Marks a setting whose value directly determines what is written to a
    #: live revenue account. The dashboard shows these with a warning style and
    #: requires a confirmation step.
    high_impact: bool = False


# ===========================================================================
# THE COMPLETE LIST OF CLIENT-EDITABLE SETTINGS
# ===========================================================================
# Defaults below encode the client's own stated answers, recorded 2026-09-05:
#   * "it must show the current stock that is available on the vendor"
#         -> safety_buffer = 0
#   * "never more than 15"
#         -> max_quantity = 15
#   * "when it is 0 on the vendor"
#         -> out_of_stock_at = 0
#   * "set it to zero straight away but dont delete it"
#         -> missing_full_feeds_before_zero = 1, and nothing is ever deleted
#   * "every smallest or tiniest one, it must be perfectly synced"
#         -> min_change_to_push = 0
#   * "5000 skus instead of 2000 in one run"
#         -> max_changes_per_run = 5000
#   * scope: "HA-AMS- only"
#         -> sku_prefixes_in_scope = ["HA-AMS-"]

SPECS: list[Spec] = [
    # ------------------------------------------------------------------ MODE
    Spec(
        key="sync_mode",
        default="dry_run",
        value_type="enum",
        choices=["dry_run", "needs_approval", "automatic"],
        label="What the system is allowed to do",
        help_text=(
            "Practice mode works everything out and shows exactly what it would send, "
            "but sends nothing. Ask first prepares the changes and waits for a person "
            "to click Approve. Automatic sends on its own, with the safety rules "
            "watching. Start in practice mode and stay there until you trust it."
        ),
        category="safety",
        sort_order=1,
        high_impact=True,
    ),
    Spec(
        key="paused",
        default=False,
        value_type="bool",
        label="Pause everything",
        help_text=(
            "Stops all activity immediately. Checked before every run, so a paused "
            "system stays paused even if a cycle was already waiting in the queue. "
            "Nothing is lost while paused - the work is simply picked up when you "
            "switch it back on."
        ),
        category="safety",
        sort_order=2,
        high_impact=True,
    ),

    # -------------------------------------------------------------- SCHEDULE
    Spec(
        key="sync_interval_minutes",
        default=60,
        value_type="int",
        min_value=5,
        max_value=1440,
        label="Check the vendor every … minutes",
        help_text=(
            "The vendor publishes a small delta file about every 5 minutes. Start at "
            "60 minutes as planned; once you trust the system, lower this to 15 or "
            "even 5. Amazon's limits are not the constraint - your confidence is."
        ),
        category="schedule",
        sort_order=10,
    ),
    Spec(
        key="timezone",
        default="America/New_York",
        value_type="str",
        label="Which timezone decides what 'today' means",
        help_text=(
            "Used to work out which feed files belong to today. The vendor puts the "
            "date in the filename (for example FULL_FEED_110708_20260904.zip), so "
            "this only decides where the day boundary falls."
        ),
        category="schedule",
        sort_order=11,
    ),
    Spec(
        key="catalog_refresh_hour",
        default=3,
        value_type="int",
        min_value=0,
        max_value=23,
        label="Refresh the Amazon catalogue at … o'clock",
        help_text=(
            "Once a day the system downloads Amazon's All Listings Report to learn "
            "the real SKUs and the quantities Amazon is currently showing. A quiet "
            "hour is best. Uses the timezone above."
        ),
        category="schedule",
        sort_order=12,
    ),
    Spec(
        key="max_file_age_hours",
        default=24,
        value_type="int",
        min_value=1,
        max_value=336,
        label="Ignore vendor files older than … hours",
        help_text=(
            "Old delta files sometimes sit on the vendor's server. Anything older "
            "than this is skipped so yesterday's numbers are never replayed over "
            "today's."
        ),
        category="schedule",
        sort_order=13,
    ),
    Spec(
        key="process_only_today",
        default=True,
        value_type="bool",
        label="Only process files dated today",
        help_text=(
            "On by default, as requested. The date comes from the filename, so this "
            "is exact. Turning it off makes the system process any unseen file that "
            "is newer than the last one it handled - useful for catching up after "
            "an outage, and safe because a file is never processed twice."
        ),
        category="schedule",
        sort_order=14,
    ),

    # ----------------------------------------------------------------- SCOPE
    Spec(
        key="sku_prefixes_in_scope",
        default=["HA-AMS-"],
        value_type="list[str]",
        label="SKU prefixes this system may change",
        help_text=(
            "The system will only ever touch listings whose SKU starts with one of "
            "these. Measured on 4 September 2026: HA-AMS- covers 45,511 listings and "
            "99.5% of them match a barcode in the All Media Supply feed, so it is "
            "unambiguously the AMS catalogue. HA-INGR- (22,523 listings) is Ingram "
            "and matches 0%. The *-OLD- prefixes match 68-85% and are deliberately "
            "left out because that is not certain enough to risk. Everything else on "
            "the account is left completely alone."
        ),
        category="scope",
        sort_order=20,
        high_impact=True,
    ),
    Spec(
        key="skip_inactive_listings",
        default=True,
        value_type="bool",
        label="Skip listings that are not Active",
        help_text=(
            "The account has 15,768 Inactive and 440 Incomplete listings. Writing a "
            "quantity to those achieves nothing and clutters the history, so they "
            "are skipped by default."
        ),
        category="scope",
        sort_order=21,
    ),
    Spec(
        key="skip_fba_listings",
        default=True,
        value_type="bool",
        label="Never touch products stored at Amazon (FBA)",
        help_text=(
            "When Amazon holds the stock, Amazon owns the number and we are not "
            "allowed to change it. This account is entirely merchant-fulfilled, so "
            "this should never trigger - but leave it on so it never can."
        ),
        category="scope",
        sort_order=22,
        locked=True,
    ),
    Spec(
        key="blacklisted_skus",
        default=[],
        value_type="list[str]",
        label="Never-touch list (exact SKUs)",
        help_text=(
            "Products managed by hand, or bought from somewhere else. The system "
            "will never change these, whatever the vendor says."
        ),
        category="scope",
        sort_order=23,
    ),

    # ------------------------------------------------------- QUANTITY RULES
    Spec(
        key="safety_buffer",
        default=0,
        value_type="int",
        min_value=0,
        max_value=50,
        label="Hold back … units as a safety margin",
        help_text=(
            "Subtracted from the vendor's stock before publishing. Set to 0 as you "
            "asked, because the quantity is now refreshed often enough that holding "
            "stock back only costs sales. If you ever start seeing cancellations, "
            "raise this to 1 or 2."
        ),
        category="quantity",
        sort_order=30,
        high_impact=True,
    ),
    Spec(
        key="max_quantity",
        default=15,
        value_type="int",
        min_value=1,
        max_value=999,
        label="Never publish more than … units",
        help_text=(
            "A hard ceiling. The vendor sometimes reports very large numbers - the "
            "highest seen in a real feed was 5,662 - and promising that much on "
            "Amazon is a risk with no upside."
        ),
        category="quantity",
        sort_order=31,
        high_impact=True,
    ),
    Spec(
        key="out_of_stock_at",
        default=0,
        value_type="int",
        min_value=0,
        max_value=20,
        label="Treat as out of stock when vendor stock is … or less",
        help_text=(
            "Set to 0 as you asked, so a product only comes off sale when the vendor "
            "genuinely has none. Raising this to 1 or 2 is the most effective single "
            "protection against cancelled orders if that ever becomes a problem."
        ),
        category="quantity",
        sort_order=32,
        high_impact=True,
    ),
    Spec(
        key="min_change_to_push",
        default=0,
        value_type="int",
        min_value=0,
        max_value=50,
        label="Smallest change worth sending to Amazon",
        help_text=(
            "Set to 0 as you asked, so Amazon stays exactly in step with the vendor. "
            "Raising it to 2 would stop the system spending effort moving a product "
            "from 12 to 11, which matters only if you push very often."
        ),
        category="quantity",
        sort_order=33,
    ),
    Spec(
        key="missing_full_feeds_before_zero",
        default=1,
        value_type="int",
        min_value=1,
        max_value=10,
        label="Set to 0 after a product is missing from … full feeds",
        help_text=(
            "When the vendor stops carrying a product it simply disappears from the "
            "daily full feed. Set to 1 as you asked, so it comes off sale straight "
            "away. The listing is never deleted, from the system or from Amazon - "
            "only its quantity goes to 0, so it can come back instantly."
        ),
        category="quantity",
        sort_order=34,
        high_impact=True,
    ),
    Spec(
        key="allow_quantity_increases",
        default=True,
        value_type="bool",
        label="Allow the system to raise quantities",
        help_text=(
            "Turning this off makes the system only ever reduce a quantity or set it "
            "to 0. That protects account health with no chance of overselling, at the "
            "cost of leaving sales on the table. A useful setting for a nervous first "
            "week."
        ),
        category="quantity",
        sort_order=35,
        high_impact=True,
    ),
    Spec(
        key="format_overrides",
        default={},
        value_type="json",
        label="Different rules per product type",
        help_text=(
            "Optional. Lets you set a different maximum or safety margin for a "
            "particular product type. The vendor uses LP, CD, BD, DVD, SIN, TIN, MC "
            'and others. Example: {"LP": {"max_quantity": 8}}.'
        ),
        category="quantity",
        sort_order=36,
    ),

    # ------------------------------------------------------------ GUARDRAILS
    Spec(
        key="max_changes_per_run",
        default=5000,
        value_type="int",
        min_value=1,
        max_value=200000,
        label="Change at most … products in one run",
        help_text=(
            "A brake, not a limit on the work. Anything above this waits for the next "
            "run, worst cases first. It matters because the first full-feed run has a "
            "large backlog to clear: on 4 September 2026 the gap between the vendor "
            "and Amazon was 38,112 products. At 5,000 per run the backlog clears in "
            "about eight runs, and no single run ever looks like a runaway."
        ),
        category="guardrails",
        sort_order=40,
        high_impact=True,
    ),
    Spec(
        key="guardrail_max_percent_changed",
        default=25.0,
        value_type="float",
        min_value=0.1,
        max_value=100.0,
        label="Stop if more than …% of the catalogue would change",
        help_text=(
            "Protects against a corrupt or wrong-vendor file. If a single run wants "
            "to change more than this share of the in-scope catalogue, it stops and "
            "emails you instead of proceeding."
        ),
        category="guardrails",
        sort_order=41,
    ),
    Spec(
        key="guardrail_max_zeroing",
        default=2000,
        value_type="int",
        min_value=1,
        max_value=100000,
        label="Stop if more than … products would go to zero",
        help_text=(
            "The most damaging thing this system could ever do is switch off the "
            "whole catalogue at once. This is the rule that prevents it. A normal day "
            "zeroes a few dozen to a few hundred products."
        ),
        category="guardrails",
        sort_order=42,
        high_impact=True,
    ),
    Spec(
        key="guardrail_min_feed_rows_percent",
        default=50.0,
        value_type="float",
        min_value=1.0,
        max_value=100.0,
        label="Reject a full feed with fewer than …% of its usual rows",
        help_text=(
            "A truncated download looks exactly like 'everything went out of stock'. "
            "Real full feeds carry about 1.15 million rows. Anything under half that "
            "is treated as a broken file, quarantined, and reported."
        ),
        category="guardrails",
        sort_order=43,
    ),
    Spec(
        key="guardrail_require_known_header",
        default=True,
        value_type="bool",
        label="Reject a feed whose column names have changed",
        help_text=(
            "The expected header is barcode|artist|title|price|stock|format. If the "
            "vendor changes it, stock could end up read from the price column. Better "
            "to stop and tell you."
        ),
        category="guardrails",
        sort_order=44,
    ),
    Spec(
        key="verify_after_push",
        default=True,
        value_type="bool",
        label="Read Amazon back to confirm each change landed",
        help_text=(
            "Amazon can accept a batch and still quietly reject individual rows. With "
            "this on, the system re-reads what it changed and confirms. Anything that "
            "did not stick is retried and shown on the dashboard. Costs a little time "
            "and is worth it."
        ),
        category="guardrails",
        sort_order=45,
    ),

    # --------------------------------------------------------------- PARSING
    Spec(
        key="feed_delimiter",
        default="|",
        value_type="str",
        label="Character that separates the columns",
        help_text=(
            "The vendor's files are named .csv but are actually separated by the | "
            "character. Change this only if the vendor changes their format."
        ),
        category="parsing",
        sort_order=50,
    ),
    Spec(
        key="column_map",
        default={
            "barcode": "barcode",
            "artist": "artist",
            "title": "title",
            "price": "price",
            "stock": "stock",
            "format": "format",
        },
        value_type="json",
        label="Which column is which",
        help_text=(
            "Left side is what the system needs, right side is what the vendor calls "
            "it. If the vendor renames 'stock' to 'qty', change it here - it takes "
            "two minutes and needs no developer."
        ),
        category="parsing",
        sort_order=51,
    ),
    Spec(
        key="sku_prefix_for_new",
        default="HA-AMS-",
        value_type="str",
        label="Prefix used to build a SKU from a barcode",
        help_text=(
            "The system builds the Amazon SKU as this prefix followed by the barcode "
            "padded to 13 digits - exactly what your Google Sheet formula "
            'TEXT(barcode,"0000000000000") does. Padding is not optional: without it '
            "two thirds of the catalogue cannot be found."
        ),
        category="parsing",
        sort_order=52,
    ),

    # ---------------------------------------------------------------- REPORTS
    Spec(
        key="generate_reports_for_delta",
        default=True,
        value_type="bool",
        label="Produce the five report files for delta runs too",
        help_text=(
            "As requested: the five files are generated separately for each delta "
            "update as well as for the daily full feed, so the team can see exactly "
            "what each one changed."
        ),
        category="reports",
        sort_order=60,
    ),
    Spec(
        key="report_retention_days",
        default=90,
        value_type="int",
        min_value=1,
        max_value=3650,
        label="Keep report files for … days",
        help_text="Older report files are deleted to stop the disk filling up. The database records stay.",
        category="reports",
        sort_order=61,
    ),

    # ----------------------------------------------------------------- ALERTS
    Spec(
        key="alert_emails_critical",
        default=[],
        value_type="list[str]",
        label="Email these people about serious problems",
        help_text=(
            "A stopped run, a failed login to the vendor, an expired Amazon token, a "
            "safety rule that fired. These need someone to act."
        ),
        category="alerts",
        sort_order=70,
    ),
    Spec(
        key="alert_emails_summary",
        default=[],
        value_type="list[str]",
        label="Email these people the daily summary",
        help_text="One message a day: what ran, what changed, what needs looking at.",
        category="alerts",
        sort_order=71,
    ),
    Spec(
        key="alert_emails_approval",
        default=[],
        value_type="list[str]",
        label="Email these people when a batch needs approval",
        help_text="Only used when the mode is set to Ask first.",
        category="alerts",
        sort_order=72,
    ),
    Spec(
        key="alert_on_every_run",
        default=False,
        value_type="bool",
        label="Email after every single run",
        help_text=(
            "Off by default. With a 15-minute cycle this would be 96 emails a day and "
            "people stop reading them, which is worse than no alerts at all."
        ),
        category="alerts",
        sort_order=73,
    ),
    Spec(
        key="unmapped_spike_threshold",
        default=90000,
        value_type="int",
        min_value=1,
        max_value=2000000,
        label="Warn if more than … in-stock products cannot be matched",
        help_text=(
            "Counts only products the vendor HAS in stock but which are not listed on "
            "Amazon - those are the ones worth knowing about, and they are the same "
            "products that appear in the New Products report. Measured on 3 September "
            "2026 the normal figure was 74,140: the vendor had 116,093 products in "
            "stock and the client lists about 45,500 of them, so a large number here "
            "is expected and healthy. The default of 90,000 leaves room above that, so "
            "the warning fires only on a real jump - which usually means the vendor "
            "changed their barcode format, or SKUs were renamed in Seller Central."
        ),
        category="alerts",
        sort_order=74,
    ),

    # ------------------------------------------------------------ INVARIANTS
    Spec(
        key="never_send_price",
        default=True,
        value_type="bool",
        label="Never send a price to Amazon",
        help_text=(
            "This cannot be switched off. Prices are yours to set, because you add "
            "shipping, tax and margin. The rule is built into the code with a final "
            "check that refuses to transmit any message containing a price, and this "
            "row exists only so that nobody can create a setting that pretends to "
            "turn it off."
        ),
        category="safety",
        sort_order=3,
        locked=True,
    ),
]

#: Fast lookup by key.
SPEC_BY_KEY: dict[str, Spec] = {s.key: s for s in SPECS}

#: Plain defaults, used when the database has no row yet.
DEFAULTS: dict[str, Any] = {s.key: s.default for s in SPECS}


# ===========================================================================
# Validation
# ===========================================================================

def _coerce(spec: Spec, raw: Any) -> Any:
    """
    Convert and validate ``raw`` against ``spec``. Raises :class:`SettingError`.

    Error messages are written for the person using the dashboard, not for a
    developer reading a stack trace.
    """
    t = spec.value_type

    try:
        if t == "bool":
            if isinstance(raw, bool):
                return raw
            s = str(raw).strip().lower()
            if s in {"true", "1", "yes", "on"}:
                return True
            if s in {"false", "0", "no", "off", ""}:
                return False
            raise SettingError(f"{spec.label}: expected yes or no, got {raw!r}")

        if t == "int":
            value = int(str(raw).strip())
        elif t == "float":
            value = float(str(raw).strip())
        elif t == "str":
            value = str(raw).strip()
        elif t == "enum":
            value = str(raw).strip()
            if spec.choices and value not in spec.choices:
                raise SettingError(
                    f"{spec.label}: must be one of {', '.join(spec.choices)}, got {value!r}"
                )
        elif t == "list[str]":
            if isinstance(raw, str):
                # accept newline or comma separated input from a textarea
                value = [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]
            elif isinstance(raw, (list, tuple)):
                value = [str(p).strip() for p in raw if str(p).strip()]
            else:
                raise SettingError(f"{spec.label}: expected a list, got {type(raw).__name__}")
        elif t == "json":
            if isinstance(raw, str):
                import json

                value = json.loads(raw) if raw.strip() else {}
            else:
                value = raw
            if not isinstance(value, (dict, list)):
                raise SettingError(f"{spec.label}: expected a JSON object or list")
        elif t == "time":
            if isinstance(raw, dtime):
                value = raw.strftime("%H:%M")
            else:
                s = str(raw).strip()
                hh, _, mm = s.partition(":")
                dtime(int(hh), int(mm or 0))  # raises if out of range
                value = f"{int(hh):02d}:{int(mm or 0):02d}"
        else:  # pragma: no cover - guarded by the spec list itself
            raise SettingError(f"unknown value_type {t!r} for {spec.key}")
    except SettingError:
        raise
    except (TypeError, ValueError) as exc:
        raise SettingError(f"{spec.label}: {raw!r} is not a valid {t} ({exc})") from exc

    if spec.min_value is not None and isinstance(value, (int, float)) and value < spec.min_value:
        raise SettingError(f"{spec.label}: must be at least {spec.min_value:g}, got {value:g}")
    if spec.max_value is not None and isinstance(value, (int, float)) and value > spec.max_value:
        raise SettingError(f"{spec.label}: must be at most {spec.max_value:g}, got {value:g}")

    return value


# ===========================================================================
# Read
# ===========================================================================

def get(session: Session, key: str, *, default: Any = None) -> Any:
    """
    One setting, falling back to the declared default when no row exists.

    A missing row is normal, not an error: a fresh database has no settings and
    must still behave correctly.
    """
    spec = SPEC_BY_KEY.get(key)
    row = session.get(Setting, key)
    if row is not None:
        return row.value.get("v") if isinstance(row.value, dict) and "v" in row.value else row.value
    if spec is not None:
        return spec.default
    return default


def get_all(session: Session) -> dict[str, Any]:
    """
    Every setting, defaults merged with stored overrides.

    Call this ONCE at the start of a run and pass the dict around. Re-reading
    mid-run would let a settings change alter behaviour halfway through, which
    makes a run impossible to explain afterwards. The result is also stored on
    ``Run.settings_snapshot`` for exactly that reason.
    """
    values = dict(DEFAULTS)
    for row in session.execute(select(Setting)).scalars():
        if row.key not in SPEC_BY_KEY:
            log.warning("settings table has unknown key %r; ignoring it", row.key)
            continue
        values[row.key] = (
            row.value.get("v") if isinstance(row.value, dict) and "v" in row.value else row.value
        )
    return values


def get_typed(session: Session) -> EffectiveSettings:
    """Settings as an attribute-access object, for readable engine code."""
    return EffectiveSettings(get_all(session))


@dataclass(slots=True)
class EffectiveSettings:
    """
    Thin typed view over the settings dict.

    Exists so the decision engine reads ``s.max_quantity`` rather than
    ``settings["max_quantity"]`` -- fewer chances to typo a key into a silent
    ``None``.
    """

    raw: dict[str, Any] = field(default_factory=dict)

    def __getattr__(self, name: str) -> Any:
        if name in self.raw:
            return self.raw[name]
        if name in DEFAULTS:
            return DEFAULTS[name]
        raise AttributeError(f"no setting named {name!r}")

    def as_dict(self) -> dict[str, Any]:
        return dict(self.raw)


# ===========================================================================
# Write
# ===========================================================================

def set_value(
    session: Session,
    key: str,
    raw_value: Any,
    *,
    actor: str = "system",
    actor_ip: str | None = None,
    allow_locked: bool = False,
) -> Any:
    """
    Validate and store one setting, writing an audit event.

    ``allow_locked`` exists only for the seeding script. A web request must
    never pass it, which is why the router does not expose it.

    Returns the coerced value that was stored.
    """
    spec = SPEC_BY_KEY.get(key)
    if spec is None:
        raise SettingError(f"There is no setting called {key!r}.")
    if spec.locked and not allow_locked:
        raise SettingError(
            f"{spec.label} cannot be changed. It is a built-in safety rule, not an option."
        )

    value = _coerce(spec, raw_value)

    row = session.get(Setting, key)
    old = None
    if row is None:
        row = Setting(
            key=key,
            value={"v": value},
            value_type=spec.value_type,
            label=spec.label,
            help_text=spec.help_text,
            category=spec.category,
            min_value=spec.min_value,
            max_value=spec.max_value,
            choices=spec.choices,
            locked=spec.locked,
            sort_order=spec.sort_order,
            updated_by=actor,
        )
        session.add(row)
    else:
        old = row.value.get("v") if isinstance(row.value, dict) and "v" in row.value else row.value
        row.value = {"v": value}
        row.updated_by = actor
        # keep the descriptive columns in step with the code
        row.label = spec.label
        row.help_text = spec.help_text
        row.category = spec.category
        row.locked = spec.locked

    session.add(
        AuditEvent(
            action="setting.changed",
            actor=actor,
            actor_ip=actor_ip,
            target=key,
            old_value={"v": old},
            new_value={"v": value},
            detail=spec.label,
        )
    )
    log.info("setting %s changed by %s: %r -> %r", key, actor, old, value)
    return value


def seed_defaults(session: Session, *, actor: str = "system") -> int:
    """
    Insert any missing settings rows. Idempotent; never overwrites.

    Run at startup so the dashboard shows the full list with sensible values on
    a brand new deployment.
    """
    existing = {k for (k,) in session.execute(select(Setting.key)).all()}
    created = 0
    for spec in SPECS:
        if spec.key in existing:
            continue
        session.add(
            Setting(
                key=spec.key,
                value={"v": spec.default},
                value_type=spec.value_type,
                label=spec.label,
                help_text=spec.help_text,
                category=spec.category,
                min_value=spec.min_value,
                max_value=spec.max_value,
                choices=spec.choices,
                locked=spec.locked,
                sort_order=spec.sort_order,
                updated_by=actor,
            )
        )
        created += 1
    if created:
        log.info("seeded %d default settings", created)
    return created
