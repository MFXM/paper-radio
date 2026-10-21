-- Pipeline tables. Safe to re-run:
--   docker compose exec -T postgres psql -U n8n -d pipeline < db/init/schema.sql.inc
CREATE EXTENSION IF NOT EXISTS vector;

-- Researchers whose papers get a score boost (managed with /curate-author)
CREATE TABLE IF NOT EXISTS known_authors (
    id              serial PRIMARY KEY,
    author_name     varchar(255) NOT NULL,
    normalized_name varchar(255) NOT NULL UNIQUE,   -- "f_lastname"
    tier            varchar(10)  DEFAULT 'Tier 1',  -- 'Tier 1' | 'Tier 2'
    notes           text,
    created_at      timestamptz  DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_norm_author ON known_authors (normalized_name);

-- Reference papers that define what "relevant" means (managed with /curate)
CREATE TABLE IF NOT EXISTS paper_embeddings (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tier       varchar(10) NOT NULL DEFAULT 'Tier 2',   -- 'Tier 1' | 'Tier 2' | 'Tier 3'
    source     varchar(20) NOT NULL DEFAULT 'manual',
    arxiv_id   varchar(50) UNIQUE,
    doi        varchar(100),
    url        text,
    embedding  vector(768),                             -- dimension of EMBEDDING_MODEL
    payload    jsonb DEFAULT '{}'::jsonb,
    created_at timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_paper_tier ON paper_embeddings (tier);
CREATE INDEX IF NOT EXISTS idx_paper_vector_hnsw ON paper_embeddings
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
