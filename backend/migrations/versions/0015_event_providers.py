"""Provider registry, so a real feed has a legal `source` value to send.

`EventSource` was a two-value `StrEnum` (`commerce_sim`, `fulfillment_sim`)
enforced by a Pydantic `Literal` pin on every inbound event -- the core
ingestion contract was named after the simulator, not defined independently
of it. `processed_events.source` was already an unconstrained `VARCHAR(64)`
with no foreign key, so widening the contract costs nothing at the storage
layer; this migration adds the registry the widened contract validates
against.

Full per-provider credentials (a real secret per registered provider, rather
than the one shared `X-Service-Token` every caller still presents) are
deliberately out of scope here and noted as a follow-up: `credential_hash`
exists on the table so that work has somewhere to land, but nothing in this
phase populates or checks it.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create `event_providers` and seed the two demo sources.

    Seeded with `credential_hash IS NULL` and `is_demo TRUE`: these rows
    exist purely so n8n's hardcoded `"source": "commerce_sim"` /
    `"fulfillment_sim"` payloads keep validating with zero workflow changes.
    `allowed_facility_ids '[]'` means "every facility", matching today's
    unrestricted demo behaviour.
    """
    op.execute(
        """
        CREATE TABLE event_providers (
            provider_id VARCHAR(64) PRIMARY KEY,
            kind VARCHAR(16) NOT NULL CHECK (kind IN ('commerce', 'fulfillment')),
            credential_hash VARCHAR(128),
            allowed_facility_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            is_demo BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        INSERT INTO event_providers (provider_id, kind, is_demo)
        VALUES ('commerce_sim', 'commerce', TRUE),
               ('fulfillment_sim', 'fulfillment', TRUE)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE event_providers")
