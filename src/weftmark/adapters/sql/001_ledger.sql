-- Portable SQLite-family schema. Apply explicitly through the host's migration
-- facility; no PRAGMAs, transaction-control statements, JSON UDFs or hash UDFs.
CREATE TABLE weftmark_ledger_schema (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  version INTEGER NOT NULL CHECK(version=1),
  component TEXT NOT NULL CHECK(component='weftmark.ledger-sql.v1')
);
INSERT INTO weftmark_ledger_schema VALUES(1,1,'weftmark.ledger-sql.v1');

CREATE TABLE weftmark_ledger_streams (
  workspace_id TEXT NOT NULL, ledger_id TEXT NOT NULL,
  format TEXT NOT NULL CHECK(format='weftmark-ledger-v1'),
  head_sequence INTEGER NOT NULL DEFAULT 0 CHECK(head_sequence BETWEEN 0 AND 9007199254740991),
  head_digest TEXT NOT NULL CHECK(length(head_digest)=64),
  PRIMARY KEY(workspace_id,ledger_id)
);
CREATE TABLE weftmark_ledger_entries (
  workspace_id TEXT NOT NULL, ledger_id TEXT NOT NULL,
  sequence INTEGER NOT NULL CHECK(sequence BETWEEN 1 AND 9007199254740991),
  previous_digest TEXT NOT NULL CHECK(length(previous_digest)=64),
  digest TEXT NOT NULL CHECK(length(digest)=64),
  kind TEXT NOT NULL, entity_id TEXT NOT NULL, recorded_at TEXT NOT NULL,
  record_json TEXT NOT NULL,
  operation_key TEXT NOT NULL CHECK(length(operation_key)=64),
  request_digest TEXT NOT NULL CHECK(length(request_digest)=64),
  PRIMARY KEY(workspace_id,ledger_id,sequence),
  UNIQUE(workspace_id,ledger_id,operation_key),
  FOREIGN KEY(workspace_id,ledger_id) REFERENCES weftmark_ledger_streams(workspace_id,ledger_id)
);
CREATE TABLE weftmark_ledger_receipts (
  workspace_id TEXT NOT NULL, ledger_id TEXT NOT NULL,
  operation_key TEXT NOT NULL, request_digest TEXT NOT NULL,
  sequence INTEGER NOT NULL, principal_id TEXT NOT NULL, request_json TEXT NOT NULL,
  PRIMARY KEY(workspace_id,ledger_id,operation_key),
  FOREIGN KEY(workspace_id,ledger_id,sequence) REFERENCES weftmark_ledger_entries(workspace_id,ledger_id,sequence)
);
CREATE INDEX weftmark_ledger_entity ON weftmark_ledger_entries(workspace_id,ledger_id,entity_id,sequence);
CREATE INDEX weftmark_ledger_kind ON weftmark_ledger_entries(workspace_id,ledger_id,kind,sequence);

CREATE TRIGGER weftmark_ledger_entries_insert BEFORE INSERT ON weftmark_ledger_entries BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM weftmark_ledger_streams s
    WHERE s.workspace_id=NEW.workspace_id AND s.ledger_id=NEW.ledger_id
      AND NEW.sequence=s.head_sequence+1 AND NEW.previous_digest=s.head_digest
  ) THEN RAISE(ABORT,'ledger insert does not extend head') END;
END;
CREATE TRIGGER weftmark_ledger_entries_update BEFORE UPDATE ON weftmark_ledger_entries BEGIN
  SELECT RAISE(ABORT,'ledger entries are immutable');
END;
CREATE TRIGGER weftmark_ledger_entries_delete BEFORE DELETE ON weftmark_ledger_entries BEGIN
  SELECT RAISE(ABORT,'ledger entries are immutable');
END;
CREATE TRIGGER weftmark_ledger_receipts_update BEFORE UPDATE ON weftmark_ledger_receipts BEGIN
  SELECT RAISE(ABORT,'ledger receipts are immutable');
END;
CREATE TRIGGER weftmark_ledger_receipts_delete BEFORE DELETE ON weftmark_ledger_receipts BEGIN
  SELECT RAISE(ABORT,'ledger receipts are immutable');
END;
CREATE TRIGGER weftmark_ledger_streams_update BEFORE UPDATE ON weftmark_ledger_streams BEGIN
  SELECT CASE WHEN NEW.workspace_id!=OLD.workspace_id OR NEW.ledger_id!=OLD.ledger_id OR NEW.format!=OLD.format
    THEN RAISE(ABORT,'ledger identity is immutable') END;
  SELECT CASE WHEN NEW.head_sequence!=OLD.head_sequence+1 OR NOT EXISTS (
    SELECT 1 FROM weftmark_ledger_entries e JOIN weftmark_ledger_receipts r
      ON r.workspace_id=e.workspace_id AND r.ledger_id=e.ledger_id AND r.operation_key=e.operation_key
    WHERE e.workspace_id=NEW.workspace_id AND e.ledger_id=NEW.ledger_id
      AND e.sequence=NEW.head_sequence AND e.digest=NEW.head_digest AND e.previous_digest=OLD.head_digest
      AND r.sequence=e.sequence AND r.request_digest=e.request_digest
  ) THEN RAISE(ABORT,'invalid ledger head update') END;
END;
CREATE TRIGGER weftmark_ledger_streams_delete BEFORE DELETE ON weftmark_ledger_streams BEGIN
  SELECT RAISE(ABORT,'ledger streams are immutable');
END;
CREATE TRIGGER weftmark_ledger_schema_update BEFORE UPDATE ON weftmark_ledger_schema BEGIN
  SELECT RAISE(ABORT,'ledger schema version is immutable');
END;
CREATE TRIGGER weftmark_ledger_schema_delete BEFORE DELETE ON weftmark_ledger_schema BEGIN
  SELECT RAISE(ABORT,'ledger schema version is immutable');
END;
