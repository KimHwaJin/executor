"""Versioned Agent DB catalog; preserve legacy IDs and execution references.

Revision ID: ew_0003
Revises: ew_0002
"""
from alembic import context, op
import sqlalchemy as sa

revision = "ew_0003"
down_revision = "ew_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Lock before inspecting data: no legacy writer may race the archive.
    op.execute("LOCK TABLE workflow_catalog IN ACCESS EXCLUSIVE MODE")
    bind = op.get_bind()
    if not context.is_offline_mode():
        if bind.execute(sa.text("SELECT to_regclass('workflow_embeddings')")).scalar():
            raise RuntimeError("workflow_embeddings already exists: verify Agent DB / chatapp DB separation")
        if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM workflow_catalog)")).scalar():
            if context.get_x_argument(as_dictionary=True).get("preserve_legacy_catalog") != "true":
                raise RuntimeError(
                    "Existing catalog data requires backup and reviewed ID/metadata mapping. "
                    "Use -x preserve_legacy_catalog=true only after review; no automatic import is performed."
                )
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    # PostgreSQL retains the table OID: execution FKs continue pointing to the
    # exact original catalog rows, not to unrelated UUIDs in the new catalog.
    op.rename_table("workflow_catalog", "workflow_catalog_legacy")
    op.execute("""
        CREATE TABLE workflow_catalog (
            workflow_id UUID CONSTRAINT workflow_catalog_v2_pkey PRIMARY KEY,
            current_revision INTEGER NOT NULL DEFAULT 1 CHECK (current_revision >= 1),
            recommend_yn CHAR(1) NOT NULL DEFAULT 'N' CHECK (recommend_yn IN ('Y', 'N')),
            created_by UUID NOT NULL,
            updated_by UUID NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ,
            deleted_by UUID,
            CHECK ((deleted_at IS NULL) = (deleted_by IS NULL))
        )
    """)
    op.execute("""
        CREATE TABLE workflow_version (
            version_id UUID PRIMARY KEY,
            workflow_id UUID NOT NULL REFERENCES workflow_catalog(workflow_id) ON DELETE RESTRICT,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            workflow JSONB NOT NULL,
            registration_type TEXT NOT NULL CHECK (registration_type IN ('USER', 'AGENT')),
            created_by UUID NOT NULL,
            project_id UUID,
            session_id UUID,
            task_id UUID,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (workflow_id, revision),
            CHECK (jsonb_typeof(workflow) = 'object' AND
                   workflow @> '{"workflow_version":"1.0"}'::jsonb AND
                   jsonb_typeof(workflow->'workflow') IS NOT DISTINCT FROM 'object')
        )
    """)
    op.execute("""
        ALTER TABLE workflow_catalog ADD CONSTRAINT workflow_catalog_current_version_fkey
        FOREIGN KEY (workflow_id, current_revision)
        REFERENCES workflow_version(workflow_id, revision)
        ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED
    """)
    op.execute("""
        CREATE TABLE workflow_embeddings (
            embedding_id UUID PRIMARY KEY,
            version_id UUID NOT NULL REFERENCES workflow_version(version_id) ON DELETE RESTRICT,
            search_text TEXT NOT NULL CHECK (length(btrim(search_text)) > 0),
            embedding_model TEXT NOT NULL CHECK (length(btrim(embedding_model)) > 0),
            embedding VECTOR NOT NULL,
            embedding_dimension INTEGER NOT NULL,
            text_recipe_version TEXT NOT NULL CHECK (length(btrim(text_recipe_version)) > 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (version_id, embedding_model),
            CHECK (embedding_dimension > 0 AND embedding_dimension = vector_dims(embedding))
        )
    """)
    op.execute("""
        CREATE INDEX workflow_catalog_recommendable ON workflow_catalog(workflow_id)
        WHERE recommend_yn = 'Y' AND deleted_at IS NULL
    """)
    op.execute("CREATE INDEX workflow_embeddings_model_dimension ON workflow_embeddings(embedding_model, embedding_dimension)")
    # Pending mappings deliberately have no version: 1.3 snapshots or unknown
    # registering users must never masquerade as valid reusable 1.0 documents.
    op.execute("""
        CREATE TABLE workflow_catalog_legacy_mapping (
            catalog_id UUID PRIMARY KEY REFERENCES workflow_catalog_legacy(catalog_id) ON DELETE RESTRICT,
            version_id UUID REFERENCES workflow_version(version_id) ON DELETE RESTRICT,
            mapped_at TIMESTAMPTZ,
            CHECK ((version_id IS NULL) = (mapped_at IS NULL))
        )
    """)
    op.execute("INSERT INTO workflow_catalog_legacy_mapping(catalog_id) SELECT catalog_id FROM workflow_catalog_legacy")


def downgrade() -> None:
    # Even a soft-deleted row is data. Never discard version/embedding history.
    if context.is_offline_mode():
        raise RuntimeError("Offline downgrade cannot verify data preservation")
    op.execute("LOCK TABLE workflow_catalog, workflow_version, workflow_embeddings IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM workflow_catalog)")).scalar():
        raise RuntimeError("Catalog contains data; downgrade requires a separate reviewed export/restore plan")
    op.drop_table("workflow_catalog_legacy_mapping")
    op.drop_table("workflow_embeddings")
    op.drop_constraint("workflow_catalog_current_version_fkey", "workflow_catalog", type_="foreignkey")
    op.drop_table("workflow_version")
    op.drop_table("workflow_catalog")
    op.rename_table("workflow_catalog_legacy", "workflow_catalog")
    # The extension may be shared by other tables; retain it.
