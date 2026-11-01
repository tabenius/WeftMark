"""Optional live Nostoi CLI integration exercised by hosted CAD smoke CI."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
from datetime import datetime, timezone

import pytest

from test_cad_evidence import Ledger, Workspace, RAW
from weftmark.application.cad_verified_intake import import_verified_cad_evidence
from weftmark.application.cad_evidence import CadEvidenceImportError


@pytest.mark.skipif(not os.environ.get("NOSTOI_BIN"), reason="live Nostoi CLI not provisioned")
def test_live_nostoi_ledger_receipt_and_trusted_head(tmp_path):
    cli=os.environ["NOSTOI_BIN"]
    ledger_path=tmp_path/"events.jsonl"
    source=tmp_path/"board.kicad_sch"
    source.write_bytes(b"(kicad_sch)")
    report=tmp_path/"erc.json"
    report.write_bytes(RAW)
    source_sha=hashlib.sha256(source.read_bytes()).hexdigest()
    report_sha=hashlib.sha256(RAW).hexdigest()
    body={
        "schema":"ragbaz.cad.nostoi-evidence/1", "evidence_id":"cad-live",
        "change_set_id":"cs-42", "source_sha256":source_sha,
        "report_sha256":report_sha, "mode":"erc", "verdict":"clear",
        "tool_version":"10.0", "freshness":"saved-source-only",
        "certification":False,
    }
    appended=subprocess.run(
        [cli,"append",str(ledger_path),"--kind","cad.evidence.completed",
         "--actor","cad:headless-coordinator","--subject","cad-live",
         "--body",json.dumps(body,separators=(",",":"))],
        capture_output=True,text=True,check=True,timeout=30,
    )
    receipt=appended.stdout.strip()
    head=json.loads(subprocess.run(
        [cli,"head",str(ledger_path),"--json"],capture_output=True,
        text=True,check=True,timeout=30).stdout)
    candidate={
        "schema":"ragbaz.cad.weftmark-evidence-candidate/1",
        "id":"cad-live", "kind":"test","check":"erc",
        "subject":{"kind":"change_set","id":"cs-42"},
        "state":"passed","tool":"kicad-cli","tool_version":"10.0",
        "source_sha256":source_sha, "report_sha256":report_sha,
        "freshness":"saved-source-only","approval":"none","certification":False,
        "nostoi":{"kind":"cad.evidence.completed","subject":"cad-live","receipt":receipt},
        "artifacts":[{"uri":"artifact:local-kicad-erc","digest":report_sha}],
    }
    stored=Ledger()
    assert import_verified_cad_evidence(
        stored,Workspace(),candidate,report_path=report,source_path=source,
        nostoi_ledger=ledger_path,nostoi_bin=cli,
        trusted_head=(head["seq"],head["digest"]),
        observed_at=datetime.now(timezone.utc))=="cad-live"
    assert stored.records[0][0]=="cad.evidence.imported"
    report.write_bytes(b'{"violations":["tampered"]}')
    with pytest.raises(CadEvidenceImportError,match="digest mismatch"):
        import_verified_cad_evidence(
            Ledger(),Workspace(),candidate,report_path=report,source_path=source,
            nostoi_ledger=ledger_path,nostoi_bin=cli,
            trusted_head=(head["seq"],head["digest"]),
            observed_at=datetime.now(timezone.utc))
