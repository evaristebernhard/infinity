//! Real native threads, Python RPC, leased work, pinned skills, and exact
//! certificates. Only model decisions are scripted; no model API is called.

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

const SETUP: &str = r#"
import json, sys
from pathlib import Path
from infinity_research.generic_api import context, initialize
root = Path(sys.argv[1])
task = root / 'task.json'
task.write_text(json.dumps({
    'schema_version': 2, 'task_id': 'native-generic-packing',
    'goal': 'Delegate two known packing fixtures, verify independently, and join their evidence.',
    'capabilities': ['verify_square_packing'],
    'skills': ['research-decomposition', 'geometric-packing', 'evidence-review'],
    'limits': {'max_workers': 100, 'max_model_calls': 100, 'max_output_tokens': 2048,
               'max_work_items': 10, 'max_failures': 2, 'max_parallel_tools': 2,
               'max_wall_seconds': 300, 'lease_seconds': 120}
}))
store = initialize(task, root / 'campaign')
state = context(store)
print(json.dumps({'ready_work_count': state['ready_work_count'],
    'skill_snapshot_sha256': state['skill_snapshot_sha256'],
    'skill_hashes': {item['name']: item['package_sha256'] for item in state['skill_catalog']}}))
"#;

const REPORT: &str = r#"
import json, sqlite3, sys
from pathlib import Path
from infinity_research.kernel import GenericStore
store = GenericStore(Path(sys.argv[1]))
report = store.report()
with sqlite3.connect(store.path) as db:
    report['transport_requests'] = [
        {'request_key': key, 'response': json.loads(response)}
        for key, response in db.execute('SELECT request_key,response FROM requests')
        if not key.startswith('heartbeat:')]
    report['effect_transport'] = [
        {'owner': owner, 'request_key': key}
        for owner, key in db.execute('SELECT owner,request_key FROM effects')]
print(json.dumps(report))
"#;

struct Receipt {
    name: String,
    result: Value,
}

struct WireReceipt {
    name: String,
    envelope: Value,
}

enum Action {
    Tool(&'static str, Value),
    Done(&'static str),
}

struct GenericFixture {
    calls: AtomicUsize,
    skill_hashes: Value,
    loaded_skills: Mutex<HashSet<String>>,
    claim_owners: Mutex<HashSet<String>>,
}

fn research_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../research")
}

fn wire_receipts(request: &CompletionRequest) -> Result<Vec<WireReceipt>, CompletionError> {
    let mut calls = HashMap::new();
    let mut results = Vec::new();
    for message in &request.chat_history {
        match message {
            Message::Assistant { content } => {
                for part in content {
                    if let AssistantContent::ToolCall(call) = part {
                        calls.insert(call.id.clone(), call.function.name.clone());
                    }
                }
            }
            Message::User { content } => {
                for part in content {
                    if let UserContent::ToolResult(result) = part {
                        let body = result
                            .content
                            .iter()
                            .find_map(|part| match part {
                                ToolResultContent::Text(text) => Some(&text.text),
                                _ => None,
                            })
                            .expect("fixture tool result must include text");
                        let value: Value = serde_json::from_str(body)?;
                        results.push(WireReceipt {
                            name: calls
                                .get(&result.id)
                                .expect("tool receipt must match its recorded call")
                                .clone(),
                            envelope: value,
                        });
                    }
                }
            }
        }
    }
    Ok(results)
}

fn receipts(request: &CompletionRequest) -> Result<Vec<Receipt>, CompletionError> {
    wire_receipts(request)?
        .into_iter()
        .map(|receipt| {
            if receipt.envelope["ok"] != true {
                return Err(CompletionError::provider(
                    ErrorClass::Fatal,
                    format!(
                        "generic fixture tool {} failed: {}",
                        receipt.name, receipt.envelope
                    ),
                ));
            }
            Ok(Receipt {
                name: receipt.name,
                result: receipt.envelope["result"].clone(),
            })
        })
        .collect()
}

fn is_supervisor_continuation(request: &CompletionRequest) -> bool {
    request
        .chat_history
        .iter()
        .rev()
        .find_map(|message| match message {
            Message::User { content } => Some(matches!(content.last(), Some(UserContent::Text(_)))),
            _ => None,
        })
        .unwrap_or(true)
}

fn active_claim(receipts: &[Receipt]) -> (&Value, &[Receipt]) {
    let index = receipts
        .iter()
        .rposition(|receipt| receipt.name == "claim_work" && receipt.result["claimed"] == true)
        .expect("a worker must claim before changing work or invoking a capability");
    (&receipts[index].result, &receipts[index + 1..])
}

fn lease(claim: &Value) -> Value {
    json!({"work_id": claim["work_id"], "lease_token": claim["lease_token"]})
}

fn instance(claim: &Value) -> u64 {
    match claim["work"]["goal"].as_str().expect("fixture goal") {
        "Verify exact packing n=4" => 4,
        "Verify exact packing n=5" => 5,
        other => panic!("unexpected packing fixture goal: {other}"),
    }
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
        squares.push(json!({
            "x": ["1", "1/4"], "y": ["1", "1/4"],
            "cos": ["0", "1/2"], "sin": ["0", "1/2"],
        }));
    }
    json!({"n": n, "side": if n == 4 {json!(["2", "0"])} else {json!(["2", "1/2"])},
           "squares": squares})
}

fn join_children(claim: &Value, since_claim: &[Receipt]) -> Action {
    let dependencies = claim["work"]["depends_on"]
        .as_array()
        .expect("resumed root has dependencies");
    assert_eq!(dependencies.len(), 2);
    let children: Vec<&Value> = since_claim
        .iter()
        .filter(|receipt| receipt.name == "read_work")
        .map(|receipt| &receipt.result)
        .collect();
    for dependency in dependencies {
        if !children.iter().any(|child| child["id"] == *dependency) {
            return Action::Tool("read_work", json!({"work_id": dependency}));
        }
    }
    assert_eq!(children.len(), 2);
    assert!(children.iter().all(|child| child["state"] == "succeeded"));
    let effects: Vec<Value> = children
        .iter()
        .map(|child| child["effect_ids"][0].clone())
        .collect();
    let outcomes: Vec<Value> = children
        .iter()
        .map(|child| child["result"].clone())
        .collect();
    let mut arguments = lease(claim);
    arguments["effect_ids"] = json!(effects);
    arguments["result"] = json!({"scope": "two known fixtures, not an autonomous mathematical discovery",
                                 "outcomes": outcomes});
    Action::Tool("complete_work", arguments)
}

impl GenericFixture {
    fn next_action(&self, request: &CompletionRequest) -> Result<Action, CompletionError> {
        let receipts = receipts(request)?;
        if is_supervisor_continuation(request) || receipts.is_empty() {
            return Ok(Action::Tool("claim_work", json!({})));
        }
        let last = receipts.last().expect("nonempty receipts checked");
        if last.name == "claim_work" {
            if last.result["claimed"] != true {
                return Ok(Action::Done("No work is ready; the supervisor can wait."));
            }
            let claim = &last.result;
            let owner = claim["work"]["lease_owner"]
                .as_str()
                .expect("native transport owner is returned with its lease");
            uuid::Uuid::parse_str(owner).expect("lease owner must be a native thread UUID");
            self.claim_owners
                .lock()
                .expect("claim owner fixture lock")
                .insert(owner.to_owned());
            let name = if claim["work_id"] != "root" {
                "geometric-packing"
            } else if claim["work"]["attempt_count"] == 1 {
                "research-decomposition"
            } else {
                assert_eq!(claim["work"]["attempt_count"], 2);
                "evidence-review"
            };
            return Ok(Action::Tool("read_skill", json!({"name": name})));
        }

        let (claim, since_claim) = active_claim(&receipts);
        let action = match last.name.as_str() {
            "read_skill" => {
                let name = last.result["name"].as_str().expect("loaded skill name");
                assert_eq!(last.result["package_sha256"], self.skill_hashes[name]);
                assert_eq!(last.result["grants_tools"], false);
                assert!(last.result["content"].as_str().expect("skill body").len() > 500);
                self.loaded_skills
                    .lock()
                    .expect("loaded skill fixture lock")
                    .insert(name.to_owned());
                match name {
                    "research-decomposition" => Action::Tool(
                        "create_work",
                        json!({"goal": "Verify exact packing n=4", "parent_id": "root",
                               "skill_names": ["geometric-packing"]}),
                    ),
                    "geometric-packing" => {
                        let mut arguments = lease(claim);
                        arguments["arguments"] = packing(instance(claim));
                        Action::Tool("verify_square_packing", arguments)
                    }
                    "evidence-review" => join_children(claim, since_claim),
                    other => panic!("unexpected fixture skill: {other}"),
                }
            }
            "create_work" => {
                if last.result["work"]["goal"] == "Verify exact packing n=4" {
                    Action::Tool(
                        "create_work",
                        json!({"goal": "Verify exact packing n=5", "parent_id": "root",
                               "skill_names": ["geometric-packing"]}),
                    )
                } else {
                    let children: Vec<Value> = since_claim
                        .iter()
                        .filter(|receipt| receipt.name == "create_work")
                        .map(|receipt| receipt.result["work_id"].clone())
                        .collect();
                    assert_eq!(children.len(), 2);
                    let mut arguments = lease(claim);
                    arguments["depends_on"] = json!(children);
                    Action::Tool("wait_work", arguments)
                }
            }
            "wait_work" => Action::Done("The root task is waiting for both independent checks."),
            "verify_square_packing" => {
                let effect = &last.result;
                assert_eq!(
                    effect["state"], "succeeded",
                    "exact checker effect: {effect}"
                );
                assert_eq!(effect["result"]["evidence"]["judgement"], "verified");
                assert_eq!(effect["owner"], claim["work"]["lease_owner"]);
                assert_eq!(effect["work_id"], claim["work_id"]);
                Action::Tool(
                    "read_artifact",
                    json!({"effect_id": effect["id"],
                           "path": effect["result"]["artifact_refs"][0]}),
                )
            }
            "read_artifact" => {
                assert_eq!(last.result["origin"], "host_verifier");
                assert_eq!(last.result["content_is_data"], true);
                let certificate: Value = serde_json::from_str(
                    last.result["content"]
                        .as_str()
                        .expect("exact certificate text"),
                )?;
                let n = instance(claim);
                assert_eq!(certificate["n"], n);
                assert_eq!(certificate["feasible"], true);
                assert_eq!(
                    certificate["separating_axes"]
                        .as_array()
                        .expect("pair witnesses")
                        .len(),
                    (n * (n - 1) / 2) as usize
                );
                let effect = since_claim
                    .iter()
                    .find(|receipt| receipt.name == "verify_square_packing")
                    .expect("artifact belongs to the checker effect");
                assert_eq!(
                    last.result["sha256"],
                    effect.result["result"]["evidence"]["certificate_sha256"]
                );
                let mut arguments = lease(claim);
                arguments["effect_ids"] = json!([effect.result["id"]]);
                arguments["result"] = json!({"n": n, "optimality": certificate["global_optimality"],
                                             "certificate_sha256": last.result["sha256"]});
                Action::Tool("complete_work", arguments)
            }
            "read_work" => join_children(claim, since_claim),
            "complete_work" => {
                Action::Done("The work result and actual evidence references are durable.")
            }
            other => panic!("unexpected fixture receipt: {other}"),
        };
        Ok(action)
    }
}

#[async_trait]
impl ModelProvider for GenericFixture {
    async fn list_models(&self) -> Result<Vec<ModelEntry>, BoxError> {
        Ok(vec![ModelEntry {
            model_id: "generic-fixture".into(),
            display_name: "Scripted decisions over real tools; no paid model".into(),
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
        assert_eq!(request.max_tokens, Some(1024));
        let names: HashSet<&str> = request
            .tools
            .iter()
            .map(|tool| tool.name.as_str())
            .collect();
        for required in [
            "claim_work",
            "create_work",
            "wait_work",
            "read_skill",
            "verify_square_packing",
            "read_artifact",
        ] {
            assert!(
                names.contains(required),
                "missing real host tool {required}"
            );
        }
        for absent in ["run_experiment", "run_python", "finish_effect"] {
            assert!(
                !names.contains(absent),
                "unconfigured or host-only tool leaked: {absent}"
            );
        }
        let next = match self.next_action(&request)? {
            Action::Tool(name, arguments) => StreamChunk::ToolCall(ToolCall::new(
                format!("fixture-{index}-{name}"),
                name,
                arguments,
            )),
            Action::Done(text) => StreamChunk::Text(text.into()),
        };
        Ok(Box::pin(futures_util::stream::iter(vec![
            Ok(next),
            Ok(StreamChunk::Final(FinalResponse {
                usage: Some(Usage {
                    input_tokens: 20,
                    output_tokens: 10,
                    total_tokens: 30,
                    cached_input_tokens: 0,
                }),
            })),
        ])))
    }
}

async fn python(script: &str, argument: &Path) -> Value {
    let output = tokio::process::Command::new("python3")
        .arg("-c")
        .arg(script)
        .arg(argument)
        .current_dir(research_root())
        .output()
        .await
        .expect("execute real Python fixture");
    assert!(
        output.status.success(),
        "Python fixture failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("Python fixture JSON")
}

#[tokio::test(flavor = "current_thread")]
async fn generic_native_campaign_decomposes_checks_joins_and_resumes_without_idle_call_storm() {
    let temporary = tempfile::tempdir().expect("create generic campaign fixture");
    let fixture = python(SETUP, temporary.path()).await;
    assert_eq!(fixture["ready_work_count"], 1);
    let campaign = temporary.path().join("campaign");
    let provider = Arc::new(GenericFixture {
        calls: AtomicUsize::new(0),
        skill_hashes: fixture["skill_hashes"].clone(),
        loaded_skills: Mutex::new(HashSet::new()),
        claim_owners: Mutex::new(HashSet::new()),
    });
    let backend = Arc::new(
        PythonBackend::new("python3", research_root(), campaign.clone())
            .expect("construct real generic Python bridge"),
    );
    let options = RunOptions {
        workers: 100,
        max_output_tokens: 1024,
        poll_interval: Duration::from_millis(50),
        model_response_timeout: Duration::from_secs(10),
        model_call_timeout: Duration::from_secs(20),
    };
    let summary = tokio::time::timeout(
        Duration::from_secs(60),
        run_campaign(
            options.clone(),
            backend.clone(),
            provider.clone(),
            "generic-fixture",
            std::future::pending(),
        ),
    )
    .await
    .expect("generic native fixture must finish within its test deadline")
    .expect("native generic work graph must run through real tools");
    assert_eq!(summary.workers, 100);
    assert_eq!(summary.stop_reason, "work_graph_completed");
    assert!(
        summary.model_calls_this_process < 60,
        "100 available threads must not each poll an idle queue: {summary:?}"
    );
    assert_eq!(
        summary.model_calls_this_process as usize,
        provider.calls.load(Ordering::SeqCst)
    );
    assert!(
        finalize_if_eligible(backend.as_ref(), &summary)
            .await
            .expect("generic finalization policy")
            .is_none()
    );

    let context = backend
        .call("context", json!({}))
        .await
        .expect("persisted generic context");
    assert_eq!(
        context["skill_snapshot_sha256"],
        fixture["skill_snapshot_sha256"]
    );
    assert_eq!(context["work_counts"]["succeeded"], 3);
    assert_eq!(context["active_tools"], 0);
    assert_eq!(context["ready_work_count"], 0);
    assert_eq!(context["budget"]["calls_without_usage"], 0);
    assert_eq!(
        context["objective_status"],
        "not_inferred_from_work_completion"
    );
    assert_eq!(
        provider.loaded_skills.lock().expect("loaded skills").len(),
        3,
        "all three pinned skills must be loaded through real RPC"
    );

    let report = python(REPORT, &campaign).await;
    let work = report["work"].as_array().expect("persisted work graph");
    assert_eq!(
        work.len(),
        3,
        "the model must create exactly two independent child goals"
    );
    let root = work
        .iter()
        .find(|work| work["id"] == "root")
        .expect("root goal");
    assert_eq!(
        root["attempt_count"], 2,
        "wait and reclaim is a continuation, not a failed trial"
    );
    assert_eq!(root["failure_count"], 0);
    assert_eq!(
        root["depends_on"]
            .as_array()
            .expect("joined children")
            .len(),
        2
    );
    assert_eq!(
        root["result"]["outcomes"]
            .as_array()
            .expect("joined child results")
            .len(),
        2
    );
    assert_eq!(
        root["result_status"],
        "agent_authored_not_independently_verified"
    );
    for child in work.iter().filter(|work| work["id"] != "root") {
        assert_eq!(child["parent_id"], "root");
        assert_eq!(child["attempt_count"], 1);
        assert_eq!(child["failure_count"], 0);
        assert_eq!(
            child["effect_ids"]
                .as_array()
                .expect("checker receipt")
                .len(),
            1
        );
    }
    let effects = report["effects"].as_array().expect("actual tool effects");
    assert_eq!(effects.len(), 2);
    for effect in effects {
        assert_eq!(effect["state"], "succeeded");
        assert_eq!(effect["capability"], "verify_square_packing");
        let evidence = &effect["result"]["evidence"];
        assert_eq!(evidence["judgement"], "verified");
        assert_eq!(evidence["formal_proof_assistant_checked"], false);
        assert_eq!(evidence["kaggle_decimal_submission_validated"], false);
        assert_eq!(
            evidence["optimality"],
            if evidence["scope"]["n"] == 4 {
                "area_bound_closed"
            } else {
                "not_proven"
            }
        );
        assert!(
            provider
                .claim_owners
                .lock()
                .expect("observed native owners")
                .contains(effect["owner"].as_str().expect("effect owner"))
        );
    }
    for request in report["transport_requests"]
        .as_array()
        .expect("native mutation keys")
    {
        let key = request["request_key"].as_str().expect("request key");
        let (owner, call) = key
            .split_once(':')
            .expect("native owner and tool call identity");
        uuid::Uuid::parse_str(owner).expect("native transport assigns a thread UUID");
        assert!(call.starts_with("fixture-"));
        if request["response"]["claimed"] == true {
            assert_eq!(request["response"]["work"]["lease_owner"], owner);
        }
    }
    for effect in report["effect_transport"]
        .as_array()
        .expect("native effect keys")
    {
        let key = effect["request_key"].as_str().expect("effect key");
        assert!(key.starts_with(&format!(
            "{}:fixture-",
            effect["owner"].as_str().expect("owner")
        )));
    }

    let calls_before_restart = provider.calls.load(Ordering::SeqCst);
    let reopened = Arc::new(
        PythonBackend::new("python3", research_root(), campaign)
            .expect("reconstruct generic backend after process restart"),
    );
    let restart = run_campaign(
        options,
        reopened,
        provider.clone(),
        "generic-fixture",
        std::future::pending(),
    )
    .await
    .expect("reopen completed generic campaign");
    assert_eq!(restart.workers, 0);
    assert_eq!(restart.model_calls_this_process, 0);
    assert_eq!(restart.stop_reason, "work_graph_completed");
    assert_eq!(provider.calls.load(Ordering::SeqCst), calls_before_restart);
}

const RECOVERY_SETUP: &str = r#"
import json, sys
from pathlib import Path
from infinity_research.generic_api import initialize
root = Path(sys.argv[1])
task = root / 'task.json'
task.write_text(json.dumps({
    'schema_version': 2, 'task_id': 'native-orphan-recovery',
    'goal': 'Verify the known four-square grid after an interrupted tool RPC.',
    'capabilities': ['verify_square_packing'], 'skills': ['geometric-packing'],
    'limits': {'max_workers': 4, 'max_model_calls': 20, 'max_output_tokens': 2048,
               'max_work_items': 10, 'max_failures': 2, 'max_parallel_tools': 1,
               'max_wall_seconds': 300, 'lease_seconds': 120}
}))
store = initialize(task, root / 'campaign')
print(json.dumps({'campaign': str(store.root)}))
"#;

const RESERVE_BEFORE_REPLY_LOSS: &str = r#"
import json, sys
from pathlib import Path
from infinity_research.kernel import GenericStore
store = GenericStore(Path(sys.argv[1]))
request = json.loads(sys.argv[2])
args = request['arguments']
effect = store.reserve_effect(request['owner'], args['work_id'], args['lease_token'],
    request['tool'], args['arguments'], request['request_key'])
print(json.dumps(effect))
"#;

const CHECK_LATE_COMPLETION: &str = r#"
import json, sys
from pathlib import Path
from infinity_research.kernel import GenericStore
store = GenericStore(Path(sys.argv[1]))
attempt = json.loads(sys.argv[2])
request, effect = attempt['request'], attempt['effect']
args = request['arguments']
try:
    store.finish_effect(effect['id'], request['owner'], args['work_id'], args['lease_token'],
        result={'judgement': 'a late worker must not replace an unknown result'})
except RuntimeError as error:
    late_error = str(error)
else:
    raise AssertionError('Late completion was not fenced')
replay = store.reserve_effect(request['owner'], args['work_id'], args['lease_token'],
    request['tool'], args['arguments'], request['request_key'])
print(json.dumps({'late_error': late_error, 'replay': replay, 'effect': store.effect(effect['id'])}))
"#;

/// Simulate the narrow crash window after real durable admission but before a
/// usable RPC response. Recovery and subsequent execution use real Python RPC.
struct ReplyLossBackend {
    inner: PythonBackend,
    campaign: PathBuf,
    injected: AtomicBool,
    admitted: Mutex<Option<Value>>,
    interruptions: Mutex<Vec<Value>>,
    fail_interruption: bool,
}

async fn python_request(script: &str, campaign: &Path, request: &Value) -> Result<Value, BoxError> {
    let output = tokio::process::Command::new("python3")
        .arg("-c")
        .arg(script)
        .arg(campaign)
        .arg(request.to_string())
        .current_dir(research_root())
        .output()
        .await?;
    if !output.status.success() {
        return Err(format!(
            "Python recovery fixture failed: {}",
            String::from_utf8_lossy(&output.stderr)
        )
        .into());
    }
    Ok(serde_json::from_slice(&output.stdout)?)
}

#[async_trait]
impl ResearchBackend for ReplyLossBackend {
    async fn call(&self, operation: &str, arguments: Value) -> Result<Value, BoxError> {
        if operation == "invoke_tool"
            && arguments["tool"] == "verify_square_packing"
            && !self.injected.swap(true, Ordering::SeqCst)
        {
            let effect =
                python_request(RESERVE_BEFORE_REPLY_LOSS, &self.campaign, &arguments).await?;
            assert_eq!(effect["execute"], true);
            assert_eq!(effect["state"], "running");
            *self.admitted.lock().expect("admitted recovery fixture") =
                Some(json!({"request": arguments, "effect": effect}));
            return Err("simulated_reply_loss after reserve_effect committed".into());
        }
        if operation == "interrupt_tool" {
            self.interruptions
                .lock()
                .expect("interruption fixture log")
                .push(arguments.clone());
            if self.fail_interruption {
                return Err("simulated interruption persistence failure".into());
            }
        }
        self.inner.call(operation, arguments).await
    }
}

struct RecoveryFixture {
    calls: AtomicUsize,
    recovered_errors: AtomicUsize,
    retry_after_loss: bool,
}

#[async_trait]
impl ModelProvider for RecoveryFixture {
    async fn list_models(&self) -> Result<Vec<ModelEntry>, BoxError> {
        Ok(vec![ModelEntry {
            model_id: "recovery-fixture".into(),
            display_name: "Offline crash-recovery decisions".into(),
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
        let responses = wire_receipts(&request)?;
        let action = if is_supervisor_continuation(&request) || responses.is_empty() {
            Action::Tool("claim_work", json!({}))
        } else {
            let last = responses.last().expect("nonempty recovery receipts");
            let claim = &responses
                .iter()
                .find(|receipt| receipt.name == "claim_work")
                .expect("recovery worker first claims the root")
                .envelope["result"];
            assert_eq!(claim["claimed"], true);
            if last.envelope["ok"] != true {
                assert_eq!(last.name, "verify_square_packing");
                assert!(
                    last.envelope["error"]
                        .as_str()
                        .expect("transport error")
                        .contains("simulated_reply_loss")
                );
                if self.retry_after_loss {
                    assert_eq!(
                        self.recovered_errors.fetch_add(1, Ordering::SeqCst),
                        0,
                        "only the injected attempt should fail"
                    );
                    assert_eq!(last.envelope["recovery"]["interrupted"], true);
                    assert_eq!(last.envelope["recovery"]["effect"]["state"], "interrupted");
                    assert_eq!(last.envelope["recovery"]["effect"]["result"], Value::Null);
                    let mut arguments = lease(claim);
                    arguments["arguments"] = packing(4);
                    Action::Tool("verify_square_packing", arguments)
                } else {
                    Action::Done("Host recovery failed; leave the recorded effect unresolved.")
                }
            } else {
                match last.name.as_str() {
                    "claim_work" => {
                        let mut arguments = lease(claim);
                        arguments["arguments"] = packing(4);
                        Action::Tool("verify_square_packing", arguments)
                    }
                    "verify_square_packing" => {
                        let effect = &last.envelope["result"];
                        assert_eq!(
                            effect["state"], "succeeded",
                            "recovery checker result: {effect}"
                        );
                        assert_eq!(effect["result"]["evidence"]["judgement"], "verified");
                        let mut arguments = lease(claim);
                        arguments["effect_ids"] = json!([effect["id"]]);
                        arguments["result"] = json!({"recovered": true, "n": 4});
                        Action::Tool("complete_work", arguments)
                    }
                    "complete_work" => Action::Done("The explicit replacement attempt completed."),
                    other => panic!("unexpected recovery tool reply: {other}"),
                }
            }
        };
        let next = match action {
            Action::Tool(name, arguments) => StreamChunk::ToolCall(ToolCall::new(
                format!("recovery-{index}-{name}"),
                name,
                arguments,
            )),
            Action::Done(text) => StreamChunk::Text(text.into()),
        };
        Ok(Box::pin(futures_util::stream::iter(vec![
            Ok(next),
            Ok(StreamChunk::Final(FinalResponse {
                usage: Some(Usage {
                    input_tokens: 20,
                    output_tokens: 10,
                    total_tokens: 30,
                    cached_input_tokens: 0,
                }),
            })),
        ])))
    }
}

async fn recovery_fixture(
    fail_interruption: bool,
) -> (
    tempfile::TempDir,
    Arc<ReplyLossBackend>,
    Arc<RecoveryFixture>,
    RunOptions,
) {
    let temporary = tempfile::tempdir().expect("create interrupted-RPC campaign");
    python(RECOVERY_SETUP, temporary.path()).await;
    let campaign = temporary.path().join("campaign");
    let backend = Arc::new(ReplyLossBackend {
        inner: PythonBackend::new("python3", research_root(), campaign.clone())
            .expect("recovery Python bridge"),
        campaign,
        injected: AtomicBool::new(false),
        admitted: Mutex::new(None),
        interruptions: Mutex::new(Vec::new()),
        fail_interruption,
    });
    let provider = Arc::new(RecoveryFixture {
        calls: AtomicUsize::new(0),
        recovered_errors: AtomicUsize::new(0),
        retry_after_loss: !fail_interruption,
    });
    let options = RunOptions {
        workers: 4,
        max_output_tokens: 1024,
        poll_interval: Duration::from_millis(50),
        model_response_timeout: Duration::from_secs(10),
        model_call_timeout: Duration::from_secs(20),
    };
    (temporary, backend, provider, options)
}

#[tokio::test(flavor = "current_thread")]
async fn interrupted_tool_rpc_releases_its_slot_before_a_live_worker_retries() {
    let (_temporary, backend, provider, options) = recovery_fixture(false).await;
    let summary = tokio::time::timeout(
        Duration::from_secs(30),
        run_campaign(
            options,
            backend.clone(),
            provider.clone(),
            "recovery-fixture",
            std::future::pending(),
        ),
    )
    .await
    .expect("orphan recovery must not wait for the 120-second work lease")
    .expect("live worker must recover its interrupted effect");
    assert_eq!(summary.stop_reason, "work_graph_completed");
    assert!(
        summary.model_calls_this_process < 12,
        "recovery must not idle-loop: {summary:?}"
    );
    assert_eq!(provider.recovered_errors.load(Ordering::SeqCst), 1);
    let admitted = backend
        .admitted
        .lock()
        .expect("admitted effect")
        .clone()
        .expect("injected crash window");
    let interruptions = backend
        .interruptions
        .lock()
        .expect("interruption log")
        .clone();
    assert_eq!(interruptions.len(), 1);
    assert_eq!(interruptions[0]["owner"], admitted["request"]["owner"]);
    assert_eq!(
        interruptions[0]["request_key"],
        admitted["request"]["request_key"]
    );

    let report = python(REPORT, &backend.campaign).await;
    let effects = report["effects"].as_array().expect("durable effects");
    assert_eq!(effects.len(), 2);
    assert_eq!(effects[0]["id"], admitted["effect"]["id"]);
    assert_eq!(effects[0]["state"], "interrupted");
    assert_eq!(effects[0]["result"], Value::Null);
    assert!(
        effects[0]["error"]
            .as_str()
            .expect("unknown outcome reason")
            .contains("unknown")
    );
    assert_eq!(effects[1]["state"], "succeeded");
    assert_eq!(
        effects[1]["result"]["evidence"]["optimality"],
        "area_bound_closed"
    );
    assert_eq!(effects[0]["arguments"], effects[1]["arguments"]);
    assert_ne!(
        effects[0]["id"], effects[1]["id"],
        "the replacement is a new explicit effect"
    );
    assert_eq!(report["active_tools"], 0);
    assert_eq!(report["work"][0]["state"], "succeeded");
    assert_eq!(
        report["work"][0]["attempt_count"], 1,
        "work lease never needed to expire"
    );
    assert_eq!(report["work"][0]["failure_count"], 0);
    assert_eq!(report["work"][0]["effect_ids"], json!([effects[1]["id"]]));
    assert!(
        !backend
            .campaign
            .join("effects")
            .join(admitted["effect"]["id"].as_str().expect("effect ID"))
            .exists(),
        "the crashed attempt was reserved but never executed"
    );

    let late = python_request(CHECK_LATE_COMPLETION, &backend.campaign, &admitted)
        .await
        .expect("check late completion and same-key replay");
    assert!(
        late["late_error"]
            .as_str()
            .expect("fencing error")
            .contains("late result is fenced")
    );
    assert_eq!(late["replay"]["execute"], false);
    assert_eq!(late["replay"]["duplicate"], true);
    assert_eq!(late["replay"]["state"], "interrupted");
    assert_eq!(late["effect"]["result"], Value::Null);
}

#[tokio::test(flavor = "current_thread")]
async fn failure_to_persist_interruption_stops_the_native_launcher_with_an_error() {
    let (_temporary, backend, provider, options) = recovery_fixture(true).await;
    let result = tokio::time::timeout(
        Duration::from_secs(30),
        run_campaign(
            options,
            backend.clone(),
            provider.clone(),
            "recovery-fixture",
            std::future::pending(),
        ),
    )
    .await
    .expect("failed host recovery must stop promptly");
    let error =
        result.expect_err("unrecorded interruption is a launcher failure, not task success");
    assert!(
        error
            .to_string()
            .contains("cannot recover interrupted tool"),
        "{error}"
    );
    assert!(provider.calls.load(Ordering::SeqCst) < 8);
    assert_eq!(
        backend
            .interruptions
            .lock()
            .expect("interruption log")
            .len(),
        1
    );
    let report = python(REPORT, &backend.campaign).await;
    assert_eq!(
        report["effects"]
            .as_array()
            .expect("unresolved durable effect")
            .len(),
        1
    );
    assert_eq!(
        report["effects"][0]["state"], "running",
        "a failed host write cannot pretend that an unknown effect was fenced"
    );
    assert_eq!(report["effects"][0]["result"], Value::Null);
    assert_eq!(report["work_counts"]["succeeded"], 0);
}
