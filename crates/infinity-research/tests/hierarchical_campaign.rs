//! Real native conversations and Python/SQLite authority. Only model decisions
//! are scripted: delegation, skill reads, joins, and exact checks use real RPCs.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use infinity_provider_protocol::{
    AssistantContent, CompletionError, CompletionRequest, ErrorClass, FinalResponse, Message,
    ModelEntry, ModelProvider, ModelStream, StreamChunk, ToolCall, ToolResultContent, Usage,
    UserContent,
};
use infinity_research::{
    BoxError, PythonBackend, ResearchBackend, RunOptions, finalize_if_eligible, run_campaign,
};
use serde_json::{Value, json};
use tokio::sync::Notify;

const SETUP: &str = r#"
import json, sys
from pathlib import Path
from infinity_research.generic_api import initialize
root = Path(sys.argv[1])
config = json.loads(sys.argv[2])
task = root / 'task.json'
task.write_text(json.dumps({
    'schema_version': 2, 'task_id': 'native-hierarchy-' + config['mode'],
    'goal': 'Coordinate independent research and inspect actual child results.',
    'brief': {'role': 'root'},
    'delegation': {'max_depth': 2, 'max_children_per_work': 3},
    'capabilities': ['verify_square_packing'],
    'skills': ['research-decomposition', 'geometric-packing'],
    'limits': {'max_workers': config['workers'], 'max_parallel_model_calls': config['models'],
               'max_model_calls': 100, 'max_output_tokens': 2048, 'max_work_items': 13,
               'max_failures': 2, 'max_parallel_tools': 1,
               'max_wall_seconds': 180, 'lease_seconds': 120}
}))
store = initialize(task, root / 'campaign')
print(json.dumps({'task_id': store.spec['task_id']}))
"#;

const REPORT: &str = r#"
import json, sqlite3, sys
from pathlib import Path
from infinity_research.kernel import GenericStore
store = GenericStore(Path(sys.argv[1]))
report = store.report()
with sqlite3.connect(store.path) as db:
    report['bindings'] = [dict(zip(('owner','work_id'), row))
                          for row in db.execute('SELECT owner,work_id FROM worker_bindings')]
print(json.dumps(report))
"#;

fn research_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../research")
}

async fn python(script: &str, path: &Path, config: Value) -> Value {
    let output = tokio::process::Command::new("python3")
        .arg("-c")
        .arg(script)
        .arg(path)
        .arg(config.to_string())
        .current_dir(research_root())
        .output()
        .await
        .expect("run hierarchy Python fixture");
    assert!(
        output.status.success(),
        "Python fixture failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("hierarchy fixture JSON")
}

struct ObservedBackend {
    inner: PythonBackend,
    campaign: PathBuf,
    peak_leases: AtomicUsize,
    wait_committed: Notify,
    child_claimed: Arc<Notify>,
    bindings: Mutex<HashMap<String, String>>,
    stop_before_claim: AtomicBool,
}

#[async_trait]
impl ResearchBackend for ObservedBackend {
    async fn call(&self, operation: &str, arguments: Value) -> Result<Value, BoxError> {
        if operation == "invoke_tool"
            && arguments["tool"] == "claim_work"
            && self.stop_before_claim.swap(false, Ordering::SeqCst)
        {
            python(
                "import json, sys\nfrom pathlib import Path\nfrom infinity_research.kernel import GenericStore\nGenericStore(Path(sys.argv[1])).stop('fixture_owner_stop')\nprint(json.dumps({'stopped': True}))",
                &self.campaign,
                Value::Null,
            ).await;
        }
        let result = self.inner.call(operation, arguments.clone()).await?;
        if operation == "context" {
            self.peak_leases.fetch_max(
                result["active_leases"]
                    .as_array()
                    .expect("real hierarchy active leases")
                    .len(),
                Ordering::SeqCst,
            );
        }
        if operation == "bind_worker" {
            let owner = arguments["owner"].as_str().expect("binding owner");
            let work = arguments["work_id"].as_str().expect("binding work");
            uuid::Uuid::parse_str(owner).expect("native thread UUID is the actual owner");
            let previous = self
                .bindings
                .lock()
                .expect("fixture binding lock")
                .insert(owner.to_owned(), work.to_owned());
            assert!(previous.is_none_or(|previous| previous == work));
        }
        if operation == "invoke_tool" && arguments["tool"] == "wait_work" {
            assert!(result["lease_token"].is_null());
            self.wait_committed.notify_one();
        }
        if operation == "invoke_tool"
            && arguments["tool"] == "claim_work"
            && result["claimed"] == true
            && result["work"]["brief"]["role"] == "retry-child"
        {
            self.child_claimed.notify_one();
        }
        Ok(result)
    }
}

struct Receipt {
    name: String,
    envelope: Value,
}

fn receipts(request: &CompletionRequest) -> Result<Vec<Receipt>, CompletionError> {
    let mut names = HashMap::new();
    let mut receipts = Vec::new();
    for message in &request.chat_history {
        match message {
            Message::Assistant { content } => {
                for part in content {
                    if let AssistantContent::ToolCall(call) = part {
                        names.insert(call.id.clone(), call.function.name.clone());
                    }
                }
            }
            Message::User { content } => {
                for part in content {
                    if let UserContent::ToolResult(result) = part {
                        let text = result
                            .content
                            .iter()
                            .find_map(|part| match part {
                                ToolResultContent::Text(text) => Some(&text.text),
                                _ => None,
                            })
                            .expect("tool receipt JSON text");
                        receipts.push(Receipt {
                            name: names.get(&result.id).expect("recorded tool call").clone(),
                            envelope: serde_json::from_str(text)?,
                        });
                    }
                }
            }
        }
    }
    Ok(receipts)
}

fn assignment(request: &CompletionRequest) -> Value {
    request
        .chat_history
        .iter()
        .filter_map(|message| match message {
            Message::User { content } => Some(content),
            _ => None,
        })
        .flatten()
        .filter_map(|part| match part {
            UserContent::Text(text) => text
                .text
                .split_once("\n\n")
                .and_then(|(_, body)| serde_json::from_str::<Value>(body).ok())
                .filter(|value| value.get("logical_agent_id").is_some()),
            _ => None,
        })
        .next_back()
        .expect("host preclaimed a focused logical-agent assignment")
}

fn lease(claim: &Value) -> Value {
    json!({"work_id": claim["work_id"], "lease_token": claim["lease_token"]})
}

fn packing(n: u64) -> Value {
    let coordinates = if n == 4 {
        vec![json!(["1/2", "0"]), json!(["3/2", "0"])]
    } else {
        vec![json!(["1/2", "0"]), json!(["3/2", "1/2"])]
    };
    let mut squares = Vec::new();
    for x in &coordinates {
        for y in &coordinates {
            squares.push(json!({"x": x, "y": y, "cos": ["1", "0"], "sin": ["0", "0"]}));
        }
    }
    if n == 5 {
        squares.push(json!({"x": ["1", "1/4"], "y": ["1", "1/4"],
                           "cos": ["0", "1/2"], "sin": ["0", "1/2"]}));
    }
    json!({"n": n, "side": if n == 4 {json!(["2", "0"])} else {json!(["2", "1/2"])},
           "squares": squares})
}

enum Action {
    Tool(&'static str, Value),
    Done,
}

struct ModelCounters {
    active: AtomicUsize,
    peak: AtomicUsize,
}

struct ModelGuard(Arc<ModelCounters>);

impl Drop for ModelGuard {
    fn drop(&mut self) {
        self.0.active.fetch_sub(1, Ordering::SeqCst);
    }
}

struct HierarchyFixture {
    mode: &'static str,
    calls: AtomicUsize,
    counters: Arc<ModelCounters>,
    resumed: Mutex<HashSet<String>>,
    observations: Mutex<HashMap<String, (String, String)>>,
    child_claimed: Arc<Notify>,
}

impl HierarchyFixture {
    fn new(mode: &'static str, child_claimed: Arc<Notify>) -> Self {
        Self {
            mode,
            calls: AtomicUsize::new(0),
            counters: Arc::new(ModelCounters {
                active: AtomicUsize::new(0),
                peak: AtomicUsize::new(0),
            }),
            resumed: Mutex::new(HashSet::new()),
            observations: Mutex::new(HashMap::new()),
            child_claimed,
        }
    }

    fn delegate(&self, claim: &Value, role: &str) -> Action {
        let mut arguments = lease(claim);
        arguments["goal"] = json!(format!("Investigate the scoped {role} fixture."));
        arguments["brief"] =
            json!({"role": role, "deliverable": "Cite actual receipts and open questions."});
        arguments["skill_names"] = json!(["geometric-packing"]);
        Action::Tool("delegate_work", arguments)
    }

    fn join(&self, claim: &Value, receipts: &[Receipt]) -> Action {
        let children: Vec<Value> = receipts
            .iter()
            .filter(|receipt| receipt.name == "delegate_work" && receipt.envelope["ok"] == true)
            .map(|receipt| receipt.envelope["result"]["work_id"].clone())
            .collect();
        let mut arguments = lease(claim);
        arguments["depends_on"] = json!(children);
        arguments["join_mode"] = json!("all_settled");
        Action::Tool("wait_work", arguments)
    }

    fn complete_join(&self, claim: &Value, receipts: &[Receipt]) -> Action {
        let dependencies = claim["work"]["depends_on"]
            .as_array()
            .expect("resumed dependencies");
        let children: Vec<&Value> = receipts
            .iter()
            .filter(|receipt| receipt.name == "read_work")
            .map(|receipt| &receipt.envelope["result"])
            .collect();
        for dependency in dependencies {
            if !children.iter().any(|child| child["id"] == *dependency) {
                return Action::Tool("read_work", json!({"work_id": dependency}));
            }
        }
        assert_eq!(children.len(), dependencies.len());
        assert!(
            children
                .iter()
                .all(|child| matches!(child["state"].as_str(), Some("succeeded" | "failed")))
        );
        let effect_ids: Vec<Value> = children
            .iter()
            .flat_map(|child| {
                child["effect_ids"]
                    .as_array()
                    .expect("child effect references")
                    .clone()
            })
            .collect();
        let mut arguments = lease(claim);
        arguments["effect_ids"] = json!(effect_ids);
        arguments["result"] = json!({
            "scope": "joined actual child workflow results; completion is not a proof",
            "children": children.iter().map(|child| json!({"id": child["id"], "state": child["state"],
                "result": child["result"], "error": child["error"]})).collect::<Vec<_>>(),
        });
        Action::Tool("complete_work", arguments)
    }

    fn next_action(&self, role: &str, claim: &Value, receipts: &[Receipt]) -> Action {
        let Some(last) = receipts.last() else {
            return if role == "failed-branch" {
                let mut arguments = lease(claim);
                arguments["retry"] = json!(false);
                arguments["error"] =
                    json!("Fixture route refuted; parent must inspect this failure.");
                Action::Tool("fail_work", arguments)
            } else {
                Action::Tool(
                    "read_skill",
                    json!({"name": if matches!(role, "root" | "coordinator") {
                        "research-decomposition"
                    } else {
                        "geometric-packing"
                    }}),
                )
            };
        };
        if last.name == "complete_work"
            || (last.name == "fail_work" && !(self.mode == "retry" && role == "root"))
        {
            return Action::Done;
        }
        if self.mode == "retry" && role == "root" {
            if claim["work"]["attempt_count"] == 2 {
                if last.name == "read_work" {
                    assert_eq!(last.envelope["result"]["state"], "succeeded");
                    let mut arguments = lease(claim);
                    arguments["result"] =
                        json!({"scope": "retried only after supervisor admission"});
                    return Action::Tool("complete_work", arguments);
                }
                let child = receipts
                    .iter()
                    .find(|receipt| receipt.name == "delegate_work")
                    .expect("retry retains original child reference");
                return Action::Tool(
                    "read_work",
                    json!({"work_id": child.envelope["result"]["work_id"]}),
                );
            }
            return match last.name.as_str() {
                "read_skill" => self.delegate(claim, "retry-child"),
                "delegate_work" => {
                    let mut arguments = lease(claim);
                    arguments["retry"] = json!(true);
                    arguments["error"] = json!("Fixture retry releases the parent's slot.");
                    Action::Tool("fail_work", arguments)
                }
                "fail_work" => Action::Tool("claim_work", json!({"work_id": claim["work_id"]})),
                "claim_work" => {
                    assert_eq!(last.envelope["ok"], true);
                    assert_eq!(
                        last.envelope["result"]["claimed"], false,
                        "model must not reacquire a lease while its child owns the sole execution slot"
                    );
                    assert_eq!(last.envelope["result"]["reason"], "await_supervisor");
                    Action::Done
                }
                other => panic!("unexpected retry fixture response {other}"),
            };
        }
        match role {
            "root" => {
                let child_roles: &[&str] = match self.mode {
                    "chain" => &["coordinator", "failed-branch"],
                    "parallel" => &["parallel-a", "parallel-b", "parallel-c"],
                    "stop" => &["stop-child"],
                    "fatal" => &["fatal-child"],
                    _ => panic!("unknown fixture mode"),
                };
                match last.name.as_str() {
                    "read_skill" | "delegate_work" => {
                        let delegated = receipts
                            .iter()
                            .filter(|receipt| receipt.name == "delegate_work")
                            .count();
                        if delegated < child_roles.len() {
                            self.delegate(claim, child_roles[delegated])
                        } else {
                            self.join(claim, receipts)
                        }
                    }
                    "wait_work" | "read_work" => self.complete_join(claim, receipts),
                    other => panic!("unexpected root reply {other}"),
                }
            }
            "coordinator" => match last.name.as_str() {
                "read_skill" => self.delegate(claim, "grandchild"),
                "delegate_work" => self.join(claim, receipts),
                "wait_work" | "read_work" => self.complete_join(claim, receipts),
                other => panic!("unexpected coordinator reply {other}"),
            },
            "grandchild" => match last.name.as_str() {
                "read_skill" => Action::Tool("claim_work", json!({"work_id": "root"})),
                "claim_work" => {
                    assert_eq!(last.envelope["ok"], false);
                    assert!(
                        last.envelope["error"]
                            .as_str()
                            .expect("bound-owner rejection")
                            .contains("bound to another")
                    );
                    self.delegate(claim, "forbidden-depth-three")
                }
                "delegate_work" => {
                    assert_eq!(last.envelope["ok"], false);
                    assert!(
                        last.envelope["error"]
                            .as_str()
                            .expect("depth rejection")
                            .contains("depth")
                    );
                    let mut arguments = lease(claim);
                    arguments["arguments"] = packing(4);
                    Action::Tool("verify_square_packing", arguments)
                }
                "verify_square_packing" => {
                    let effects: Vec<&Value> = receipts
                        .iter()
                        .filter(|receipt| receipt.name == "verify_square_packing")
                        .map(|receipt| &receipt.envelope["result"])
                        .collect();
                    assert!(effects.iter().all(|effect| effect["state"] == "succeeded"));
                    if effects.len() == 1 {
                        let mut arguments = lease(claim);
                        arguments["arguments"] = packing(5);
                        Action::Tool("verify_square_packing", arguments)
                    } else {
                        assert_eq!(
                            effects.len(),
                            2,
                            "depth-bounded leaf still performs multiple solver iterations"
                        );
                        let mut arguments = lease(claim);
                        arguments["effect_ids"] = json!(
                            effects
                                .iter()
                                .map(|effect| &effect["id"])
                                .collect::<Vec<_>>()
                        );
                        arguments["result"] =
                            json!({"scope": "two exact known fixtures", "iterations": 2});
                        Action::Tool("complete_work", arguments)
                    }
                }
                other => panic!("unexpected grandchild reply {other}"),
            },
            "parallel-a" | "parallel-b" | "parallel-c" | "stop-child" | "retry-child" => {
                assert_eq!(last.name, "read_skill");
                let mut arguments = lease(claim);
                arguments["effect_ids"] = json!([]);
                arguments["result"] = json!({"scope": "native scheduling fixture only"});
                Action::Tool("complete_work", arguments)
            }
            other => panic!("unexpected logical role {other}"),
        }
    }
}

#[async_trait]
impl ModelProvider for HierarchyFixture {
    async fn list_models(&self) -> Result<Vec<ModelEntry>, BoxError> {
        Ok(vec![ModelEntry {
            model_id: "hierarchy-fixture".into(),
            display_name: "Deterministic logical-agent decisions over real tools".into(),
            context_window: 0,
            max_output_tokens: Some(4096),
            supports_image_input: false,
        }])
    }

    async fn invoke_model(
        &self,
        _model: &str,
        request: CompletionRequest,
    ) -> Result<ModelStream, CompletionError> {
        let index = self.calls.fetch_add(1, Ordering::SeqCst);
        assert!(index < 60, "no idle inference or wait polling storm");
        assert_eq!(request.max_tokens, Some(1024));
        let names: HashSet<&str> = request
            .tools
            .iter()
            .map(|tool| tool.name.as_str())
            .collect();
        for required in [
            "delegate_work",
            "claim_work",
            "wait_work",
            "read_skill",
            "verify_square_packing",
        ] {
            assert!(names.contains(required), "required host tool {required}");
        }
        for absent in [
            "create_work",
            "bind_worker",
            "spawn_thread",
            "report_to_parent",
            "run_python",
            "finish_effect",
        ] {
            assert!(
                !names.contains(absent),
                "unbounded or host-only tool {absent} leaked"
            );
        }
        let assignment = assignment(&request);
        let mut claim = assignment["claim"].clone();
        assert_eq!(claim["claimed"], true);
        let role = claim["work"]["brief"]["role"]
            .as_str()
            .expect("focused role brief")
            .to_owned();
        let owner = claim["work"]["lease_owner"]
            .as_str()
            .expect("host lease owner")
            .to_owned();
        let work_id = claim["work_id"]
            .as_str()
            .expect("logical work ID")
            .to_owned();
        {
            let mut observations = self.observations.lock().expect("fixture observations lock");
            if let Some(previous) =
                observations.insert(role.clone(), (owner.clone(), work_id.clone()))
            {
                assert_eq!(
                    previous,
                    (owner, work_id),
                    "a logical agent must resume the same native context"
                );
            }
        }

        let receipts = receipts(&request)?;
        let serialized = serde_json::to_string(&request.chat_history)?;
        for other_role in [
            "root",
            "coordinator",
            "failed-branch",
            "grandchild",
            "parallel-a",
            "parallel-b",
            "parallel-c",
        ] {
            if other_role != role {
                assert!(
                    !serialized.contains(&format!("LOCAL_NOTE_{other_role}_ONLY")),
                    "private transcript of {other_role} leaked into {role}"
                );
            }
        }
        if !receipts.is_empty() {
            assert!(
                serialized.contains(&format!("LOCAL_NOTE_{role}_ONLY")),
                "own transcript must survive tool calls and parking"
            );
        }
        for receipt in &receipts {
            if receipt.name == "wait_work" {
                let resumed = &receipt.envelope["result"]["resume_claim"];
                assert_eq!(receipt.envelope["ok"], true);
                assert_eq!(resumed["claimed"], true);
                assert_eq!(resumed["work_id"], claim["work_id"]);
                assert_eq!(resumed["work"]["lease_owner"], claim["work"]["lease_owner"]);
                assert_ne!(resumed["lease_token"], claim["lease_token"]);
                assert_eq!(resumed["work"]["attempt_count"], 2);
                claim = resumed.clone();
                self.resumed
                    .lock()
                    .expect("resume observations lock")
                    .insert(role.clone());
                let user_texts = request
                    .chat_history
                    .iter()
                    .filter_map(|message| match message {
                        Message::User { content } => Some(
                            content
                                .iter()
                                .filter(|part| matches!(part, UserContent::Text(_)))
                                .count(),
                        ),
                        _ => None,
                    })
                    .sum::<usize>();
                assert_eq!(
                    user_texts, 1,
                    "join resumes original callback, not an interrupting user message"
                );
            }
            if receipt.envelope["ok"] != true {
                assert!(
                    role == "grandchild"
                        && matches!(receipt.name.as_str(), "claim_work" | "delegate_work"),
                    "unexpected tool failure: {}",
                    receipt.envelope
                );
            }
            if receipt.name == "read_skill" {
                assert_eq!(receipt.envelope["result"]["grants_tools"], false);
                assert_eq!(
                    receipt.envelope["result"]["sha256"]
                        .as_str()
                        .expect("actual pinned skill hash")
                        .len(),
                    64
                );
            }
        }
        if role == "fatal-child" {
            return Err(CompletionError::provider(
                ErrorClass::Fatal,
                "fixture fatal model failure with a parked parent",
            ));
        }
        if self.mode == "retry"
            && role == "root"
            && receipts
                .last()
                .is_some_and(|receipt| receipt.name == "fail_work")
        {
            // Hold this live parent model round until the supervisor has
            // admitted the child. The subsequent model claim used to acquire
            // a second lease when contract.max_workers exceeded --workers.
            self.child_claimed.notified().await;
        }
        let action = self.next_action(&role, &claim, &receipts);
        let first = receipts.is_empty();
        let next = match action {
            Action::Tool(name, arguments) => StreamChunk::ToolCall(ToolCall::new(
                format!("hierarchy-{index}-{name}"),
                name,
                arguments,
            )),
            Action::Done => StreamChunk::Text("This logical agent has ended its work.".into()),
        };
        let active = self.counters.active.fetch_add(1, Ordering::SeqCst) + 1;
        self.counters.peak.fetch_max(active, Ordering::SeqCst);
        let guard = ModelGuard(self.counters.clone());
        let delay =
            if self.mode == "parallel" || matches!(role.as_str(), "stop-child" | "retry-child") {
                Duration::from_millis(300)
            } else {
                Duration::ZERO
            };
        Ok(Box::pin(async_stream::stream! {
            let _guard = guard;
            if !delay.is_zero() { tokio::time::sleep(delay).await; }
            if first { yield Ok(StreamChunk::Text(format!("LOCAL_NOTE_{role}_ONLY"))); }
            yield Ok(next);
            yield Ok(StreamChunk::Final(FinalResponse { usage: Some(Usage {
                input_tokens: 20, output_tokens: 10, total_tokens: 30, cached_input_tokens: 0,
            }) }));
        }))
    }
}

async fn fixture(
    mode: &'static str,
    workers: usize,
    models: usize,
) -> (
    tempfile::TempDir,
    Arc<ObservedBackend>,
    Arc<HierarchyFixture>,
    RunOptions,
) {
    let temporary = tempfile::tempdir().expect("create hierarchy fixture");
    python(
        SETUP,
        temporary.path(),
        json!({"mode": mode, "workers": workers, "models": models}),
    )
    .await;
    let backend = Arc::new(ObservedBackend {
        inner: PythonBackend::new(
            "python3",
            research_root(),
            temporary.path().join("campaign"),
        )
        .expect("real hierarchy Python bridge"),
        campaign: temporary.path().join("campaign"),
        peak_leases: AtomicUsize::new(0),
        wait_committed: Notify::new(),
        child_claimed: Arc::new(Notify::new()),
        bindings: Mutex::new(HashMap::new()),
        stop_before_claim: AtomicBool::new(false),
    });
    let options = RunOptions {
        workers,
        max_output_tokens: 1024,
        poll_interval: Duration::from_millis(40),
        model_response_timeout: Duration::from_secs(10),
        model_call_timeout: Duration::from_secs(20),
    };
    let provider = Arc::new(HierarchyFixture::new(mode, backend.child_claimed.clone()));
    (temporary, backend, provider, options)
}

#[tokio::test(flavor = "current_thread")]
async fn one_slot_runs_root_child_grandchild_and_joins_a_failed_branch_without_context_leaks() {
    let (temporary, backend, provider, options) = fixture("chain", 1, 1).await;
    let summary = tokio::time::timeout(
        Duration::from_secs(45),
        run_campaign(
            options.clone(),
            backend.clone(),
            provider.clone(),
            "hierarchy-fixture",
            std::future::pending(),
        ),
    )
    .await
    .expect("single-slot delegation must not deadlock behind its parked ancestors")
    .expect("hierarchy native campaign");
    assert_eq!(summary.stop_reason, "work_graph_completed");
    assert!(summary.model_calls_this_process < 40);
    assert_eq!(provider.counters.peak.load(Ordering::SeqCst), 1);
    assert_eq!(backend.peak_leases.load(Ordering::SeqCst), 1);
    assert_eq!(
        provider.resumed.lock().expect("resume lock").clone(),
        HashSet::from(["root".to_owned(), "coordinator".to_owned()])
    );
    assert!(
        finalize_if_eligible(backend.as_ref(), &summary)
            .await
            .expect("generic finalizer policy")
            .is_none()
    );
    let report = python(REPORT, &temporary.path().join("campaign"), Value::Null).await;
    let work = report["work"].as_array().expect("persisted work graph");
    assert_eq!(
        work.len(),
        4,
        "depth-three request must not create another logical agent"
    );
    assert_eq!(
        report["bindings"]
            .as_array()
            .expect("native owner bindings")
            .len(),
        4
    );
    let owners: HashSet<&str> = report["bindings"]
        .as_array()
        .expect("bindings")
        .iter()
        .map(|binding| binding["owner"].as_str().expect("owner"))
        .collect();
    assert_eq!(
        owners.len(),
        4,
        "four isolated conversations despite one execution slot"
    );
    for node in work {
        let role = node["brief"]["role"].as_str().expect("durable role");
        assert_eq!(
            node["state"],
            if role == "failed-branch" {
                "failed"
            } else {
                "succeeded"
            }
        );
        assert_eq!(
            node["attempt_count"],
            if matches!(role, "root" | "coordinator") {
                2
            } else {
                1
            }
        );
        assert_eq!(
            node["failure_count"],
            if role == "failed-branch" { 1 } else { 0 }
        );
        assert_eq!(
            node["delegation_depth"],
            if role == "root" {
                0
            } else if role == "grandchild" {
                2
            } else {
                1
            }
        );
    }
    let effects = report["effects"]
        .as_array()
        .expect("real verifier receipts");
    assert_eq!(
        effects.len(),
        2,
        "leaf's internal search is not restricted to two delegation steps"
    );
    assert_eq!(
        effects[0]["result"]["evidence"]["optimality"],
        "area_bound_closed"
    );
    assert_eq!(effects[1]["result"]["evidence"]["optimality"], "not_proven");
    let restarted = run_campaign(
        options,
        backend,
        provider,
        "hierarchy-fixture",
        std::future::pending(),
    )
    .await
    .expect("completed hierarchy restart");
    assert_eq!(restarted.model_calls_this_process, 0);
}

#[tokio::test(flavor = "current_thread")]
async fn independent_logical_agents_share_one_global_model_semaphore() {
    let (_temporary, backend, provider, options) = fixture("parallel", 4, 2).await;
    let summary = tokio::time::timeout(
        Duration::from_secs(45),
        run_campaign(
            options,
            backend.clone(),
            provider.clone(),
            "hierarchy-fixture",
            std::future::pending(),
        ),
    )
    .await
    .expect("parallel hierarchy fixture deadline")
    .expect("parallel logical-agent campaign");
    assert_eq!(summary.stop_reason, "work_graph_completed");
    assert_eq!(
        provider.counters.peak.load(Ordering::SeqCst),
        2,
        "different child contexts must share the configured model limit"
    );
    assert!(backend.peak_leases.load(Ordering::SeqCst) <= 4);
    assert_eq!(
        backend.bindings.lock().expect("binding observations").len(),
        4
    );
    assert_eq!(provider.counters.active.load(Ordering::SeqCst), 0);
}

#[tokio::test(flavor = "current_thread")]
async fn launcher_stop_settles_parked_join_callbacks_without_waiting_for_children() {
    let (_temporary, backend, provider, options) = fixture("stop", 1, 1).await;
    let shutdown_backend = backend.clone();
    let shutdown = async move {
        shutdown_backend.wait_committed.notified().await;
    };
    let summary = tokio::time::timeout(
        Duration::from_secs(20),
        run_campaign(
            options,
            backend,
            provider.clone(),
            "hierarchy-fixture",
            shutdown,
        ),
    )
    .await
    .expect("a parked parent must not hang launcher shutdown")
    .expect("normal launcher stop drains callbacks");
    assert_eq!(
        summary.stop_reason,
        "launcher stop requested; admitted tools drained"
    );
    assert!(
        summary.model_calls_this_process < 8,
        "{summary:?}; actual provider calls {}",
        provider.calls.load(Ordering::SeqCst)
    );
    assert_eq!(provider.counters.active.load(Ordering::SeqCst), 0);
    assert!(
        provider
            .resumed
            .lock()
            .expect("resume observations")
            .is_empty()
    );
}

#[tokio::test(flavor = "current_thread")]
async fn fatal_child_model_error_drains_a_parked_parent_and_returns_failure() {
    let (_temporary, backend, provider, options) = fixture("fatal", 1, 1).await;
    let error = tokio::time::timeout(
        Duration::from_secs(20),
        run_campaign(
            options,
            backend,
            provider.clone(),
            "hierarchy-fixture",
            std::future::pending(),
        ),
    )
    .await
    .expect("fatal inference must not strand a parked ancestor")
    .expect_err("fatal model error is propagated");
    assert!(
        error.to_string().contains("fixture fatal model failure"),
        "{error}"
    );
    assert!(provider.calls.load(Ordering::SeqCst) < 8);
    assert_eq!(provider.counters.active.load(Ordering::SeqCst), 0);
}

#[tokio::test(flavor = "current_thread")]
async fn owner_stop_between_ready_snapshot_and_host_claim_is_a_normal_stop() {
    let (_temporary, backend, provider, options) = fixture("chain", 1, 1).await;
    backend.stop_before_claim.store(true, Ordering::SeqCst);
    let summary = tokio::time::timeout(
        Duration::from_secs(20),
        run_campaign(
            options,
            backend,
            provider.clone(),
            "hierarchy-fixture",
            std::future::pending(),
        ),
    )
    .await
    .expect("racing owner stop deadline")
    .expect("closed admission is a normal campaign stop");
    assert_eq!(summary.stop_reason, "fixture_owner_stop");
    assert_eq!(summary.model_calls_this_process, 0);
    assert_eq!(provider.calls.load(Ordering::SeqCst), 0);
}

#[tokio::test(flavor = "current_thread")]
async fn model_retry_cannot_bypass_a_launcher_slot_smaller_than_the_campaign_limit() {
    let (temporary, backend, provider, mut options) = fixture("retry", 4, 1).await;
    options.workers = 1;
    let summary = tokio::time::timeout(
        Duration::from_secs(30),
        run_campaign(
            options,
            backend.clone(),
            provider.clone(),
            "hierarchy-fixture",
            std::future::pending(),
        ),
    )
    .await
    .expect("retry interleaving must not deadlock")
    .expect("supervisor owns new lease admission");
    assert_eq!(summary.stop_reason, "work_graph_completed");
    assert_eq!(backend.peak_leases.load(Ordering::SeqCst), 1);
    assert_eq!(provider.counters.peak.load(Ordering::SeqCst), 1);
    assert!(summary.model_calls_this_process < 20);
    let report = python(REPORT, &temporary.path().join("campaign"), Value::Null).await;
    let work = report["work"].as_array().expect("retry work report");
    assert_eq!(work.len(), 2);
    let root = work
        .iter()
        .find(|work| work["id"] == "root")
        .expect("root work");
    assert_eq!(root["attempt_count"], 2);
    assert_eq!(root["failure_count"], 1);
    assert_eq!(root["state"], "succeeded");
    assert_eq!(
        report["bindings"].as_array().expect("bound contexts").len(),
        2
    );
}
