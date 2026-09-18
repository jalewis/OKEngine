import importlib.util, io, json, sys, tarfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
REPO=Path(__file__).resolve().parent.parent
spec=importlib.util.spec_from_file_location("framework_explain_export",REPO/"scripts/framework_explain_export.py"); m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
def dep(tmp_path):
 d=tmp_path/"dep";(d/"wiki/entities").mkdir(parents=True);(d/"wiki/sources").mkdir();(d/"wiki/assessments").mkdir();
 (d/"schema.yaml").write_text("types:\n  actor:\n    owner: pack\n    fields:\n      origin: {type: string}\n")
 (d/"wiki/sources/report.md").write_text("---\ntype: source\n---\n# Report\n")
 (d/"wiki/entities/actor.md").write_text("---\ntype: actor\norigin: X\nproducer_lane: review\nsources: [sources/report]\n---\n# Actor\n[[sources/report]]\n")
 (d/"wiki/assessments/a.md").write_text("---\ntype: assessment\nrun_id: r1\npolicy_outcome: suspected\n---\n[[sources/report]]\n")
 return d
def call(fn,argv):
 o,e=io.StringIO(),io.StringIO()
 with redirect_stdout(o),redirect_stderr(e):c=fn(argv)
 return c,o.getvalue(),e.getvalue()
def test_explain_page_and_field(tmp_path):
 d=dep(tmp_path);c,o,_=call(m.explain,["page",str(d),"entities/actor","--json"]);p=json.loads(o)
 assert c==0 and p["owner"]=="pack" and p["producer"]=="review" and p["source_lineage"]
 c,o,_=call(m.explain,["field",str(d),"entities/actor","origin","--json"]);assert json.loads(o)["declared"] is True
def test_snapshot_is_content_addressed_and_source_bounded(tmp_path):
 d=dep(tmp_path);c,o,_=call(m.snapshot,["evidence",str(d),"--actor","entities/actor","--json"]);p=json.loads(o)
 assert c==0 and p["files"]==2 and len(p["snapshot_id"])==64
 assert Path(p["manifest"]).is_file()
 assert (Path(p["manifest"]).parent/"wiki/entities/actor.md").is_file()
 assert (Path(p["manifest"]).parent/"wiki/sources/report.md").is_file()
def test_bundle_excludes_runtime_and_contains_manifest(tmp_path):
 d=dep(tmp_path);(d/".hermes-data").mkdir();(d/".hermes-data/secret").write_text("no")
 out=tmp_path/"bundle.tar.gz";c,_,_=call(m.export,[str(d),"--scope","namespace:entities","--format","bundle","--output",str(out)])
 assert c==0
 with tarfile.open(out) as tf:
  names=tf.getnames();assert "manifest.json" in names and "wiki/entities/actor.md" in names and not any("hermes" in n for n in names)
def test_assessment_type_is_enforced(tmp_path):
 d=dep(tmp_path);c,_,e=call(m.explain,["assessment",str(d),"entities/actor"])
 assert c==1 and "not an assessment" in e
def test_config_explanation_redacts_secrets(tmp_path):
 d=dep(tmp_path);(d/"config.yaml").write_text("provider:\n  api_key: super-secret\n")
 c,o,_=call(m.explain,["config",str(d),"provider.api_key","--json"]);p=json.loads(o)
 assert c==0 and p["redacted"] is True and p["value"]=="<redacted>" and "super-secret" not in o

def test_reference_and_deployment_rejections(tmp_path):
 d=dep(tmp_path)
 assert m._ref(d,"wiki/entities/actor")==d/"wiki/entities/actor.md"
 for ref, message in (("../outside","escapes wiki"),("missing","page not found")):
  c,_,e=call(m.explain,["page",str(d),ref]);assert c==1 and message in e
 c,_,e=call(m.explain,["page",str(tmp_path/"not-a-deployment"),"x"])
 assert c==1 and "not an OKEngine deployment" in e

def test_malformed_pages_schema_and_receipts_are_tolerated(tmp_path):
 d=dep(tmp_path)
 broken=d/"wiki/entities/broken.md";broken.write_text("---\n[bad\n---\n[[sources/report]] [[sources/report]]")
 (d/"schema.yaml").write_text("[bad")
 runs=d/".okengine/operations/runs";runs.mkdir(parents=True)
 (runs/"bad.json").write_text("{")
 (runs/"other.json").write_text(json.dumps({"outputs":["elsewhere.md"]}))
 (runs/"match.json").write_text(json.dumps({"run_id":"r","outputs":["entities/broken.md"],"status":"ok"}))
 page=m._page(broken,d)
 assert page["frontmatter"]=={} and page["references"]==["sources/report"]
 assert m._schema(d)=={}
 assert m._production(d,"entities/broken.md")==[{"run_id":"r","operation":None,"status":"ok","finished":None}]
 plain=d/"wiki/entities/plain.md";plain.write_text("plain body")
 assert m._page(plain,d)["body"]=="plain body"

def test_schema_fallback_and_frontmatter_fallback_fields(tmp_path):
 d=dep(tmp_path);(d/"schema.yaml").unlink()
 (d/"composed-schema.yaml").write_text("types:\n  actor:\n    required: [origin]\n")
 p=d/"wiki/entities/fallback.md"
 p.write_text("---\ntype: actor\ngenerated_by: lane\nflags: [old]\nreason: review\ndecision: allow\n---\n[[sources/report]]")
 result=m._explanation(d,p,"origin")
 assert result["producer"]=="lane" and result["quality_flags"]==["old"]
 assert result["review_reason"]=="review" and result["policy_outcome"]=="allow"
 assert result["declared"] is True and result["source_lineage"]==["sources/report"]
 (d/"composed-schema.yaml").unlink()
 assert m._schema(d)=={}

def test_config_missing_file_key_and_nonsecret_value(tmp_path):
 d=dep(tmp_path)
 c,_,e=call(m.explain,["config",str(d),"missing"]);assert c==1 and "config not found" in e
 cfg=d/".hermes-data";cfg.mkdir();(cfg/"config.yaml").write_text("model:\n  name: qwen\n")
 c,o,_=call(m.explain,["config",str(d),"model.name"]);assert c==0 and json.loads(o)["value"]=="qwen"
 c,_,e=call(m.explain,["config",str(d),"model.missing"]);assert c==1 and "key not found" in e
 c,_,e=call(m.explain,["config",str(d),"model.name.child"]);assert c==1 and "key not found" in e

def test_snapshot_assessment_missing_reference_and_idempotency(tmp_path):
 d=dep(tmp_path)
 c,o,_=call(m.snapshot,["evidence",str(d),"--assessment","r1","--json"])
 payload=json.loads(o);assert c==0 and payload["files"]==2
 manifest=Path(payload["manifest"]);before=manifest.stat().st_mtime_ns
 c,o,_=call(m.snapshot,["evidence",str(d),"--assessment","r1","--json"])
 assert c==0 and Path(json.loads(o)["manifest"]).stat().st_mtime_ns==before
 c,_,e=call(m.snapshot,["evidence",str(d),"--assessment","absent"])
 assert c==1 and "assessment run not found" in e
 (d/"wiki/entities/actor.md").write_text(
  "---\ntype: actor\n---\n[[concepts/not-evidence]] [[sources/missing]]")
 assert m._evidence(d,"entities/actor",None)==[(d/"wiki/entities/actor.md").resolve()]

def test_scope_and_all_export_formats(tmp_path):
 d=dep(tmp_path)
 assert m._scope(d,"page:entities/actor")==[d/"wiki/entities/actor.md"]
 for scope,message in (("namespace:../bad","invalid namespace"),("namespace:nope","namespace not found"),("other:x","scope must be")):
  try:m._scope(d,scope)
  except m.ExplainError as exc:assert message in str(exc)
  else:assert False, scope
 out=tmp_path/"export.json";c,o,_=call(m.export,[str(d),"--scope","page:entities/actor","--format","json","--output",str(out)])
 assert c==0 and json.loads(out.read_text())["pages"][0]["path"]=="entities/actor.md"
 md=tmp_path/"export.md";c,_,_=call(m.export,[str(d),"--scope","namespace:entities","--format","md","--output",str(md)])
 assert c==0 and "# Actor" in md.read_text()
 c,o,_=call(m.export,[str(d),"--scope","page:entities/actor","--format","json"])
 assert c==0 and Path(json.loads(o)["output"]).is_file()
 c,_,e=call(m.export,[str(d),"--scope","page:missing","--format","json"])
 assert c==1 and "page not found" in e
