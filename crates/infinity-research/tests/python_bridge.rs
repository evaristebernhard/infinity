use std::path::Path;
use std::time::Duration;

use infinity_research::{PythonBackend, ResearchBackend};
use serde_json::json;

fn fake_module(root: &Path, source: &str) -> PythonBackend {
    let module = root.join("infinity_research");
    std::fs::create_dir_all(&module).expect("create test module");
    std::fs::write(module.join("__init__.py"), "").expect("write test package");
    std::fs::write(module.join("rpc.py"), source).expect("write test RPC module");
    let campaign = root.join("campaign with spaces");
    std::fs::create_dir_all(&campaign).expect("create test campaign");
    PythonBackend::new("python3", root.to_owned(), campaign).expect("create Python bridge")
}

#[tokio::test]
async fn rpc_preserves_json_and_paths_without_a_shell() {
    let dir = tempfile::tempdir().expect("test directory");
    let backend = fake_module(
        dir.path(),
        r#"
import json, sys
request = json.loads(sys.stdin.readline())
print(json.dumps({'ok': True, 'result': {'request': request, 'argv': sys.argv[1:]}}))
"#,
    );
    let value = json!({"text": "literal `command` $(command) 中文\nline two"});
    let reply = backend
        .call("context", value.clone())
        .await
        .expect("invoke bridge");
    assert_eq!(reply["request"], value);
    assert_eq!(reply["argv"][0], "--campaign");
    assert!(
        reply["argv"][1]
            .as_str()
            .expect("campaign argument")
            .ends_with("campaign with spaces")
    );
    assert_eq!(reply["argv"][2], "context");
}

#[tokio::test]
async fn rpc_errors_and_invalid_protocol_are_not_successes() {
    let dir = tempfile::tempdir().expect("test directory");
    let backend = fake_module(
        dir.path(),
        "print('{\"ok\":false,\"error\":\"fixture evaluator failed\"}')\n",
    );
    let error = backend
        .call("context", json!({}))
        .await
        .expect_err("backend error propagates");
    assert!(error.to_string().contains("fixture evaluator failed"));
    std::fs::write(
        dir.path().join("infinity_research/rpc.py"),
        "print('log accidentally on stdout')\nprint('{\"ok\":true,\"result\":{}}')\n",
    )
    .expect("change test protocol");
    let error = backend
        .call("context", json!({}))
        .await
        .expect_err("extra stdout is invalid");
    assert!(error.to_string().contains("invalid research RPC JSON"));
}

#[tokio::test]
async fn rpc_has_a_deadline_and_an_operation_allowlist() {
    let dir = tempfile::tempdir().expect("test directory");
    let backend = fake_module(dir.path(), "import time\ntime.sleep(30)\n")
        .with_timeouts(Duration::from_millis(50), Duration::from_millis(50));
    let error = backend
        .call("context", json!({}))
        .await
        .expect_err("RPC timeout");
    assert!(error.to_string().contains("exceeded"));
    let error = backend
        .call("arbitrary-command", json!({}))
        .await
        .expect_err("operation allowlist");
    assert!(error.to_string().contains("unsupported research operation"));
}

#[tokio::test]
async fn queued_experiment_rpc_can_outlive_the_candidate_runtime_budget() {
    let dir = tempfile::tempdir().expect("test directory");
    let backend = fake_module(
        dir.path(),
        r#"
import json, sys, time
request = json.loads(sys.stdin.readline())
if sys.argv[-1] == 'context':
    result = {'budget': {'max_wall_seconds': 3}}
else:
    time.sleep(1.2)
    result = {'state': 'succeeded'}
print(json.dumps({'ok': True, 'result': result}))
"#,
    )
    .with_timeouts(Duration::from_secs(2), Duration::ZERO);
    let result = backend
        .call("experiment", json!({"timeout_seconds": 1}))
        .await
        .expect("queue time is separate from candidate execution time");
    assert_eq!(result["state"], "succeeded");
}

#[test]
fn launcher_lock_is_exclusive_and_releases_on_drop() {
    let dir = tempfile::tempdir().expect("test directory");
    let backend = fake_module(dir.path(), "print('{}')\n");
    let first = backend.lock_campaign().expect("acquire launcher lock");
    assert!(
        backend.lock_campaign().is_err(),
        "a second launcher must be rejected"
    );
    drop(first);
    let _next = backend
        .lock_campaign()
        .expect("lock released without deleting file");
}
