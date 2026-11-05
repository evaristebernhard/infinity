//! Cross-language integration: real Infinity threads, SQLite, candidate processes,
//! and independent evaluation. Only the model is deterministic; no API is called.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use async_trait::async_trait;
use infinity_provider_protocol::{
    AssistantContent, CompletionError, CompletionRequest, FinalResponse, Message, ModelEntry,
    ModelProvider, ModelStream, StreamChunk, ToolCall, ToolResultContent, Usage, UserContent,
};
use infinity_research::{
    BoxError, PythonBackend, ResearchBackend, RunOptions, finalize_if_eligible, run_campaign,
};
use serde_json::{Value, json};

const SETUP: &str = r#"
import json, sys
from pathlib import Path
from infinity_research.domains import smoke
from infinity_research.spec import file_hash, tree_hash
from infinity_research.store import ResearchStore
root = Path(sys.argv[1])
smoke.prepare(root / 'data')
spec = smoke.task(root / 'data')
spec['budget']['max_model_calls'] = 4
spec['budget']['max_workers'] = 1
spec['budget']['max_parallel_experiments'] = 1
spec['data_sha256'] = tree_hash(root / 'data')
spec['evaluator_sha256'] = file_hash(Path(smoke.__file__))
store = ResearchStore.create(root / 'campaign', spec)
queued = store.propose({'idempotency_key': 'queued-before-launch',
    'hypothesis': 'Recover a baseline admitted before the launcher started',
    'family': 'baseline', 'files': {'main.py': smoke.BASELINE},
    'seed': 42, 'timeout_seconds': 10})
print(json.dumps({'linear': smoke.LINEAR, 'baseline_id': queued['candidate_id']}))
"#;

const QUEUE_ORPHAN: &str = r#"
import sys
from pathlib import Path
from infinity_research.domains import smoke
from infinity_research.store import ResearchStore
store = ResearchStore(Path(sys.argv[1]))
store.propose({'idempotency_key': 'orphan-at-budget-stop',
    'hypothesis': 'A previously admitted replica must drain despite a lost RPC process',
    'family': 'baseline', 'files': {'main.py': smoke.BASELINE},
    'seed': 43, 'timeout_seconds': 10})
"#;

struct FixtureProvider {
    linear: String,
    baseline_id: String,
    calls: AtomicUsize,
    campaign: PathBuf,
}

#[async_trait]
impl ModelProvider for FixtureProvider {
    async fn list_models(&self) -> Result<Vec<ModelEntry>, BoxError> {
        Ok(vec![ModelEntry {
            model_id: "fixture".into(),
            display_name: "Offline integration fixture, not a real LLM".into(),
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
        if index == 3 {
            // Simulate a different tool process exiting after durable admission
            // and before taking execution ownership. No native tool handle
            // remains, so only a final ledger drain can discover this work.
            let queued = tokio::process::Command::new("python3")
                .arg("-c")
                .arg(QUEUE_ORPHAN)
                .arg(&self.campaign)
                .current_dir(research_root())
                .output()
                .await
                .expect("queue orphan fixture");
            assert!(
                queued.status.success(),
                "{}",
                String::from_utf8_lossy(&queued.stderr)
            );
        }
        if index == 0 {
            let history = serde_json::to_string(&request.chat_history).expect("serialize history");
            assert!(
                history.contains(&self.baseline_id),
                "recovery must precede model startup"
            );
            assert!(
                history.contains("succeeded"),
                "recovered experiment must be terminal"
            );
        }
        let called = |name: &str| {
            request.chat_history.iter().any(|message| {
                matches!(message, Message::Assistant { content } if content.iter().any(|part| {
                    matches!(part, AssistantContent::ToolCall(call) if call.function.name == name)
                }))
            })
        };
        let next = if !called("run_experiment") {
            StreamChunk::ToolCall(ToolCall::new(
                "fit-line",
                "run_experiment",
                json!({
                    "hypothesis": "Least squares should improve MSE over a constant baseline",
                    "family": "linear", "parent_id": self.baseline_id,
                    "files": {"main.py": self.linear}, "timeout_seconds": 10, "seed": 42,
                }),
            ))
        } else if !called("record_finding") {
            let result = request
                .chat_history
                .iter()
                .find_map(|message| match message {
                    Message::User { content } => content.iter().find_map(|part| match part {
                        UserContent::ToolResult(result) if result.id == "fit-line" => {
                            result.content.iter().find_map(|part| match part {
                                ToolResultContent::Text(text) => Some(
                                    serde_json::from_str::<Value>(&text.text)
                                        .expect("tool result JSON"),
                                ),
                                _ => None,
                            })
                        }
                        _ => None,
                    }),
                    _ => None,
                })
                .expect("experiment result must reach the native model conversation");
            assert_eq!(result["ok"], true);
            assert_eq!(result["result"]["state"], "succeeded");
            StreamChunk::ToolCall(ToolCall::new(
                "record-evidence",
                "record_finding",
                json!({
                    "kind": "observation", "text": "The independent evaluator scored the fitted line.",
                    "evidence_ids": [result["result"]["id"]],
                }),
            ))
        } else {
            StreamChunk::Text("Evidence recorded; another worker turn may continue.".into())
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

fn research_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../research")
}

#[tokio::test(flavor = "current_thread")]
async fn native_campaign_uses_real_python_ledger_executor_evaluator_and_restart_state() {
    let temporary = tempfile::tempdir().expect("create temporary campaign");
    let setup = tokio::process::Command::new("python3")
        .arg("-c")
        .arg(SETUP)
        .arg(temporary.path())
        .current_dir(research_root())
        .output()
        .await
        .expect("run fixture setup with Python 3");
    assert!(
        setup.status.success(),
        "{}",
        String::from_utf8_lossy(&setup.stderr)
    );
    let fixture: Value = serde_json::from_slice(&setup.stdout).expect("fixture setup JSON");
    let provider = Arc::new(FixtureProvider {
        linear: fixture["linear"].as_str().expect("linear code").into(),
        baseline_id: fixture["baseline_id"].as_str().expect("baseline ID").into(),
        calls: AtomicUsize::new(0),
        campaign: temporary.path().join("campaign"),
    });
    let campaign = temporary.path().join("campaign");
    let backend = Arc::new(
        PythonBackend::new("python3", research_root(), campaign.clone())
            .expect("construct Python bridge"),
    );
    let options = RunOptions {
        workers: 1,
        max_output_tokens: 1024,
        poll_interval: Duration::from_millis(50),
        model_response_timeout: Duration::from_secs(5),
        model_call_timeout: Duration::from_secs(10),
    };
    let summary = tokio::time::timeout(
        Duration::from_secs(30),
        run_campaign(
            options.clone(),
            backend.clone(),
            provider.clone(),
            "fixture",
            std::future::pending(),
        ),
    )
    .await
    .expect("offline campaign deadline")
    .expect("native Python integration");
    assert_eq!(summary.model_calls_this_process, 4);
    assert_eq!(summary.stop_reason, "model_call_budget_exhausted");
    let context = backend
        .call("context", json!({}))
        .await
        .expect("read persisted context");
    assert_eq!(context["budget"]["experiments_used"], 3);
    assert_eq!(
        context["active_experiments"], 0,
        "orphaned admitted queue must drain before return"
    );
    assert_eq!(context["budget"]["model_calls_used"], 4);
    assert_eq!(context["budget"]["observed_output_tokens"], 40);
    assert_eq!(context["budget"]["calls_without_usage"], 0);
    assert_eq!(context["findings"].as_array().expect("findings").len(), 1);
    let leaders = context["leaderboard"].as_array().expect("leaderboard");
    assert_eq!(leaders.len(), 3);
    assert_eq!(leaders[0]["family"], "linear");
    assert!(leaders[0]["score"].as_f64().expect("linear score") < 0.01);
    assert!(leaders[0]["score"].as_f64() < leaders[1]["score"].as_f64());

    let final_result = finalize_if_eligible(backend.as_ref(), &summary)
        .await
        .expect("one-time holdout evaluation")
        .expect("eligible budget stop");
    assert_eq!(final_result["evidence"]["split"], "holdout");
    assert!(final_result["score"].as_f64().expect("holdout score") < 0.01);
    assert!(
        finalize_if_eligible(backend.as_ref(), &summary)
            .await
            .expect("finalization guard")
            .is_none(),
        "final evaluation is never repeated"
    );

    // Reconstruct the bridge as after a restart. No in-memory test ledger is reused.
    let reopened = Arc::new(
        PythonBackend::new("python3", research_root(), campaign).expect("reopen durable campaign"),
    );
    let restart = run_campaign(
        options,
        reopened,
        provider.clone(),
        "fixture",
        std::future::pending(),
    )
    .await
    .expect("restart exhausted campaign");
    assert_eq!(restart.workers, 0);
    assert_eq!(restart.model_calls_this_process, 0);
    assert_eq!(provider.calls.load(Ordering::SeqCst), 4);
}
