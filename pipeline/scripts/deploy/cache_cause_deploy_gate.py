"""cache_cause market-consistency deploy gate.

No failure is swallowed: every check records PASS or FAIL with the observed
value, and an unreadable input is a FAIL, never a skip (clause 2).
Every DB probe names its full key (clause 3).

Run from the repository root so the package imports resolve:
    python3 -m pipeline.scripts.deploy.cache_cause_deploy_gate \
        --expected-digest sha256:... \
        --untouched-file deploy/k8s/ingest-hook/reference/untouched_baseline_20260727.txt \
        --deploy-target deploy/jw-ingest-hook --phase post-deploy

The untouched-spec comparison excludes only the refs named by --deploy-target, and
prints what it excluded and what measures those refs instead. See
pipeline/scripts/deploy/untouched_baseline.py for why that is not a loosening.
"""
import argparse, hashlib, json, subprocess, sys

from pipeline.scripts.deploy.untouched_baseline import (
    exclusion_report,
    parse_baseline,
    partition,
)
from pipeline.scripts.utils.mart_config import resolve_mart_db_name

NS = "llmops"
# Resolved, not pinned: the mart generation name has a single source, and the drift
# gate (tests/deploy/test_mart_db_single_source.py) enforces that. A literal here
# would silently keep gating the old generation after a switch.
DB = resolve_mart_db_name("MARIADB_DATABASE", "DB_NAME")
POD = "galera-mariadb-galera-0"
REG = ("asia-northeast3-docker.pkg.dev/prj-jw-agn-stg-ai/"
       "ar-jw-agn-stg-genos-dev-01/jw-pipeline-orchestrator")

results = []


def record(name, ok, expected, observed):
    results.append((name, ok, expected, observed))


def kubectl(args):
    out = subprocess.run(["kubectl", "-n", NS] + args, capture_output=True, text=True, timeout=90)
    if out.returncode != 0:
        raise RuntimeError("kubectl failed: " + out.stderr.strip()[:200])
    return out.stdout


def sql(query):
    pw = kubectl(["get", "secret", "galera-mariadb-galera",
                  "-o", "jsonpath={.data.mariadb-password}"])
    import base64
    pw = base64.b64decode(pw).decode()
    out = subprocess.run(
        ["kubectl", "-n", NS, "exec", POD, "-c", "mariadb-galera", "--",
         "mysql", "-ullmops", "-p" + pw, "-D", DB, "--batch", "--raw", "-e", query],
        capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
    )
    if out.returncode != 0:
        raise RuntimeError("mysql failed: " + out.stderr.strip()[:200])
    lines = [l for l in out.stdout.strip().splitlines() if l and not l.startswith("mysql: Deprecated")]
    return [l.split("\t") for l in lines]


def check(name, expected, fn):
    """A raised exception is a FAIL with the reason, never a silent skip."""
    try:
        observed = fn()
    except Exception as exc:
        record(name, False, expected, "PROBE_ERROR " + type(exc).__name__ + ": " + str(exc)[:160])
        return
    record(name, observed == expected, expected, observed)


def image_of(kind, name, container):
    obj = json.loads(kubectl(["get", kind, name, "-o", "json"]))
    spec = obj["spec"]
    tmpl = spec["jobTemplate"]["spec"]["template"] if kind == "cronjob" else spec["template"]
    for c in tmpl["spec"]["containers"]:
        if c["name"] == container:
            return c["image"]
    raise RuntimeError("container not found: " + container)


def env_of(kind, name, container, key):
    obj = json.loads(kubectl(["get", kind, name, "-o", "json"]))
    spec = obj["spec"]
    tmpl = spec["jobTemplate"]["spec"]["template"] if kind == "cronjob" else spec["template"]
    for c in tmpl["spec"]["containers"]:
        if c["name"] == container:
            for e in c.get("env", []):
                if e["name"] == key:
                    if "value" not in e:
                        raise RuntimeError(key + " is not a literal value")
                    return e["value"]
    raise RuntimeError("env not found: " + key)


def spec_sha(kind, name):
    obj = json.loads(kubectl(["get", kind, name, "-o", "json"]))
    canon = json.dumps(obj.get("spec", {}), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--expected-digest", required=True,
                    help="digest the hook reference points must carry")
    ap.add_argument("--expected-env-digest", default=None,
                    help="INGEST_JOB_IMAGE 기대 digest. 미지정이면 --expected-digest 와 같다. "
                         "★두 값을 다르게 주면 부분 갱신(image 만 바꾸고 env 를 두는 것)을 재현한다")
    ap.add_argument("--baseline-rows", type=int, default=168)
    ap.add_argument("--untouched-file", required=True)
    ap.add_argument("--deploy-target", action="append", default=[],
                    help="ref (kind/name) this deploy intentionally changes; excluded "
                         "from the untouched comparison and reported explicitly. "
                         "Repeatable. A target absent from the baseline is an error, "
                         "never a silent no-op.")
    ap.add_argument("--phase", default="pre")
    args = ap.parse_args()

    want = REG + "@" + args.expected_digest
    want_env = REG + "@" + (args.expected_env_digest or args.expected_digest)

    # --- R: reference points -------------------------------------------------
    check("R1 hook deploy container image", want,
          lambda: image_of("deploy", "jw-ingest-hook", "trigger"))
    check("R2 hook INGEST_JOB_IMAGE env", want_env,
          lambda: env_of("deploy", "jw-ingest-hook", "trigger", "INGEST_JOB_IMAGE"))

    # --- B: live baselines (full-key, deterministic) -------------------------
    check("B1 cache_cause row count", args.baseline_rows,
          lambda: int(sql("SELECT COUNT(*) FROM cache_cause;")[1][0]))
    check("B2 cache_cause distinct brands", 25,
          lambda: int(sql("SELECT COUNT(DISTINCT brand) FROM cache_cause;")[1][0]))
    check("B3 catalog rows/names", "5100/4514",
          lambda: "/".join(sql(
              "SELECT COUNT(*), COUNT(DISTINCT name) FROM catalog_strategic_brand;")[1]))
    check("B4 is_jw names / is_target rows", "25/16",
          lambda: "/".join(sql(
              "SELECT COUNT(DISTINCT CASE WHEN COALESCE(is_jw,0)=1 THEN name END), "
              "SUM(COALESCE(is_target,0)) FROM catalog_strategic_brand;")[1]))
    check("B5 zero-active names carrying jw/target", 0,
          lambda: int(sql(
              "SELECT COUNT(*) FROM catalog_strategic_brand "
              "WHERE (COALESCE(is_jw,0)=1 OR COALESCE(is_target,0)=1) AND name IN "
              "(SELECT name FROM (SELECT name FROM catalog_strategic_brand GROUP BY name "
              "HAVING SUM(CASE WHEN COALESCE(is_excluded,0)=0 AND "
              "COALESCE(is_class_excluded,0)=0 THEN 1 ELSE 0 END)=0) z);")[1][0]))
    check("B6 cache_cause rows whose stored market_id equals the ACTIVE catalog market",
          args.baseline_rows,
          lambda: int(sql(
              "SELECT COUNT(*) FROM cache_cause c JOIN catalog_strategic_brand s "
              "ON CONVERT(s.name USING utf8mb4) COLLATE utf8mb4_unicode_ci = "
              "   CONVERT(c.brand USING utf8mb4) COLLATE utf8mb4_unicode_ci "
              "WHERE COALESCE(s.is_excluded,0)=0 AND COALESCE(s.is_class_excluded,0)=0 "
              "AND CONCAT('strategy_', LPAD(CAST(SUBSTRING(s.ml_id,4) AS UNSIGNED),3,'0')) "
              "    = c.market_id;")[1][0]))
    check("B7 cache_cause provenance columns absent (migration not applied)", 0,
          lambda: int(sql(
              "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='" + DB +
              "' AND table_name='cache_cause' AND column_name IN "
              "('view_source_id','run_id','build_sha','input_manifest_json');")[1][0]))

    # --- H: handoff (★스키마 지정 + root — llmops 는 이 테이블에 grant 가 없다) ------
    def sql_root(query):
        import base64
        rp = base64.b64decode(kubectl(
            ["get", "secret", "galera-mariadb-galera",
             "-o", "jsonpath={.data.mariadb-root-password}"])).decode()
        out = subprocess.run(
            ["kubectl", "-n", NS, "exec", POD, "-c", "mariadb-galera", "--",
             "mysql", "-uroot", "-p" + rp, "--batch", "--raw", "-e", query],
            capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
        )
        if out.returncode != 0:
            raise RuntimeError("mysql(root) failed: " + out.stderr.strip()[:200])
        lines = [l for l in out.stdout.strip().splitlines()
                 if l and not l.startswith("mysql: Deprecated")]
        return [l.split("\t") for l in lines]

    HOFF = "jw_brand_activity_stage.mart_brand_activity_assignment_handoff"
    check("H1 handoff rows (스키마 지정)", 0,
          lambda: int(sql_root("SELECT COUNT(*) FROM " + HOFF + ";")[1][0]))
    check("H2 handoff column count", 13,
          lambda: int(sql_root(
              "SELECT COUNT(*) FROM information_schema.columns WHERE "
              "table_schema='jw_brand_activity_stage' AND "
              "table_name='mart_brand_activity_assignment_handoff';")[1][0]))
    check("H3 handoff PK", "run_id",
          lambda: ",".join(r[4] for r in sql_root(
              "SHOW INDEX FROM " + HOFF + " WHERE Key_name='PRIMARY';")[1:]))
    check("H4 handoff pending index", "axis_status,assignment_status,created_at,run_id",
          lambda: ",".join(r[4] for r in sql_root(
              "SHOW INDEX FROM " + HOFF +
              " WHERE Key_name='idx_topic_assignment_handoff_pending';")[1:]))

    # --- U: untouched targets ------------------------------------------------
    # The deploy target belongs in the baseline (it is captured with everything else) but
    # must not be COMPARED after the deploy: changing it is the deploy. Without this the
    # gate went 26/26 before and 25/1 after, every time, and a gate that always fails is
    # a gate that gets ignored. Exclusions are named by the caller and printed below.
    excluded = {}
    try:
        with open(args.untouched_file) as fh:
            baseline = parse_baseline(fh.read())
        baseline, excluded = partition(baseline, args.deploy_target)
    except Exception as exc:
        record("U0 untouched baseline usable", False, args.untouched_file,
               "READ_ERROR " + type(exc).__name__ + ": " + str(exc)[:160])
        baseline = {}
    if not baseline:
        record("U0 untouched baseline non-empty", False, ">0 entries", "0 entries")
    for ref, want_sha in sorted(baseline.items()):
        kind, name = ref.split("/", 1)
        check("U " + ref, want_sha, lambda k=kind, n=name: spec_sha(k, n))

    print("phase=" + args.phase)
    for line in exclusion_report(
        excluded, measured_by="R1 hook container image + R2 hook INGEST_JOB_IMAGE env"
    ):
        print(line)
    print("=" * 100)
    failed = 0
    for name, ok, expected, observed in results:
        status = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(status + "  " + name)
        print("        expected: " + str(expected))
        print("        observed: " + str(observed))
    print("=" * 100)
    print("TOTAL=" + str(len(results)) + " FAILED=" + str(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
