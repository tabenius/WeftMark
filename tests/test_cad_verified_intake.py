from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from weftmark.application.cad_verified_intake import import_verified_cad_evidence
from weftmark.application.cad_evidence import CadEvidenceImportError
from test_cad_evidence import Ledger, Workspace, RAW, candidate, verified_event


def test_verified_intake_runs_nostoi_and_matches_source(tmp_path):
    source=tmp_path/"design.kicad_sch"
    source.write_bytes(b"(kicad_sch)")
    report=tmp_path/"erc.json"
    report.write_bytes(RAW)
    chain=tmp_path/"events.jsonl"
    chain.write_text('test fixture, CLI mocked')
    item=candidate()
    item["source_sha256"]=hashlib.sha256(source.read_bytes()).hexdigest()
    record=verified_event()
    record["body"]["source_sha256"]=item["source_sha256"]
    calls=[]
    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[1]=="show":
            return SimpleNamespace(stdout=json.dumps({"verified":True,"record":record}))
        return SimpleNamespace(stdout="")
    ledger=Ledger()
    ident=import_verified_cad_evidence(ledger,Workspace(),item,
        report_path=report,source_path=source,nostoi_ledger=chain,
        observed_at=datetime.now(timezone.utc),runner=runner)
    assert ident=="cad-abc"
    assert [x[1] for x in calls]==["verify","show"]
    assert len(ledger.records)==1


def test_stale_saved_source_refused_before_invoking_nostoi(tmp_path):
    source=tmp_path/"design.kicad_sch"
    source.write_bytes(b"changed")
    report=tmp_path/"erc.json"
    report.write_bytes(RAW)
    chain=tmp_path/"events.jsonl"
    chain.write_bytes(b"")
    calls=[]
    with pytest.raises(CadEvidenceImportError,match="source revision mismatch"):
        import_verified_cad_evidence(Ledger(),Workspace(),candidate(),
            report_path=report,source_path=source,nostoi_ledger=chain,
            observed_at=datetime.now(timezone.utc),
            runner=lambda *a,**k:calls.append(a))
    assert calls==[]

def test_trusted_nostoi_head_checkpoint_must_match(tmp_path):
    source=tmp_path/"design.kicad_sch"
    source.write_bytes(b"(kicad_sch)")
    report=tmp_path/"erc.json"
    report.write_bytes(RAW)
    chain=tmp_path/"events.jsonl"
    chain.write_bytes(b"fixture")
    item=candidate()
    item["source_sha256"]=hashlib.sha256(source.read_bytes()).hexdigest()
    record=verified_event()
    record["body"]["source_sha256"]=item["source_sha256"]
    def runner(argv, **kwargs):
        if argv[1]=="show":
            return SimpleNamespace(stdout=json.dumps({"verified":True,"record":record}))
        if argv[1]=="head":
            return SimpleNamespace(stdout=json.dumps({"seq":1,"digest":"f"*64}))
        return SimpleNamespace(stdout="")
    with pytest.raises(CadEvidenceImportError,match="does not match trusted checkpoint"):
        import_verified_cad_evidence(Ledger(),Workspace(),item,
            report_path=report,source_path=source,nostoi_ledger=chain,
            observed_at=datetime.now(timezone.utc),runner=runner,
            trusted_head=(2,"f"*64))
    ledger=Ledger()
    assert import_verified_cad_evidence(ledger,Workspace(),item,
        report_path=report,source_path=source,nostoi_ledger=chain,
        observed_at=datetime.now(timezone.utc),runner=runner,
        trusted_head=(1,"f"*64))=="cad-abc"
