use std::collections::HashMap;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use futures_util::StreamExt;
use infinity_provider_protocol::{
    AssistantContent, CompletionError, CompletionRequest, ErrorClass, FinalResponse, Message,
    ModelEntry, ModelProvider, ModelStream, StreamChunk, ToolCall, Usage, UserContent,
};
use infinity_research::{BoxError, BudgetedProvider, ResearchBackend, RunOptions, run_campaign};
use serde_json::{Value, json};

#[derive(Default)]
struct Ledger {
    reservations: Vec<String>,
    usage: HashMap<String, (u64, u64)>,
    experiments: Vec<Value>,
    findings: Vec<Value>,
    recovered: usize,
}

struct MemoryBackend {
    state: Mutex<Ledger>,
    max_calls: usize,
    max_workers: usize,
    target_findings: usize,
    active_experiments: AtomicUsize,
    peak_experiments: AtomicUsize,
    experiment_slots: tokio::sync::Semaphore,
    experiment_barrier: Option<tokio::sync::Barrier>,
    admission_seconds: f64,
}

impl MemoryBackend {
    fn new(max_calls: usize, workers: usize) -> Self {
        Self {
            state: Mutex::new(Ledger::default()),
            max_calls,
            max_workers: workers,
            target_findings: workers,
            active_experiments: AtomicUsize::new(0),
            peak_experiments: AtomicUsize::new(0),
            experiment_slots: tokio::sync::Semaphore::new(workers),
            experiment_barrier: None,
            admission_seconds: 3600.0,
        }
    }
}

#[async_trait]
impl ResearchBackend for MemoryBackend {
    async fn call(&self, operation: &str, arguments: Value) -> Result<Value, BoxError> {
        let _slot = if operation == "experiment" {
            if let Some(barrier) = &self.experiment_barrier {
                barrier.wait().await;
            }
            Some(self.experiment_slots.acquire().await?)
        } else {
            None
        };
        if operation == "experiment" {
            let active = self.active_experiments.fetch_add(1, Ordering::SeqCst) + 1;
            self.peak_experiments.fetch_max(active, Ordering::SeqCst);
            tokio::time::sleep(Duration::from_millis(40)).await;
            self.active_experiments.fetch_sub(1, Ordering::SeqCst);
        }
        let mut state = self.state.lock().expect("bug: mock ledger poisoned");
        match operation {
            "recover" => {
                state.recovered += 1;
                Ok(json!({"recovered": 0}))
            }
            "context" => Ok(json!({
                "stop": state.findings.len() >= self.target_findings,
                "stop_reason": if state.findings.len() >= self.target_findings { Some("evidence collected") } else { None },
                "max_workers": self.max_workers,
                "max_output_tokens": 64,
                "task": {"goal": "synthetic native integration test"},
                "findings": state.findings,
            })),
            "reserve_model_call" => {
                if state.reservations.len() >= self.max_calls {
                    return Ok(json!({"allowed": false, "reason": "model call budget exhausted"}));
                }
                let id = arguments["request_id"]
                    .as_str()
                    .ok_or("missing request_id")?;
                state.reservations.push(id.to_owned());
                Ok(
                    json!({"allowed": true, "call_index": state.reservations.len(), "max_output_tokens": 64,
                           "search_wall_seconds_remaining": self.admission_seconds}),
                )
            }
            "record_usage" => {
                let id = arguments["request_id"]
                    .as_str()
                    .ok_or("missing request_id")?;
                if !state.reservations.iter().any(|known| known == id) {
                    return Err("usage without reservation".into());
                }
                state.usage.insert(
                    id.to_owned(),
                    (
                        arguments["input_tokens"]
                            .as_u64()
                            .ok_or("missing input count")?,
                        arguments["output_tokens"]
                            .as_u64()
                            .ok_or("missing output count")?,
                    ),
                );
                Ok(json!({"recorded": true}))
            }
            "experiment" => {
                state.experiments.push(arguments);
                Ok(
                    json!({"candidate_id": format!("candidate-{}", state.experiments.len()), "evidence_id": "evidence-1", "score": 0.5}),
                )
            }
            "finding" => {
                state.findings.push(arguments);
                Ok(json!({"recorded": true}))
            }
            _ => Err(format!("unexpected mock operation: {operation}").into()),
        }
    }
}

fn model_entry() -> ModelEntry {
    ModelEntry {
        model_id: "mock".into(),
        display_name: "Deterministic test provider".into(),
        context_window: 0,
        max_output_tokens: Some(1024),
        supports_image_input: false,
    }
}

struct ScriptedProvider {
    requests: Mutex<Vec<CompletionRequest>>,
    only_text: bool,
}

impl ScriptedProvider {
    fn new(only_text: bool) -> Self {
        Self {
            requests: Mutex::new(Vec::new()),
            only_text,
        }
    }
}

#[async_trait]
impl ModelProvider for ScriptedProvider {
    async fn list_models(&self) -> Result<Vec<ModelEntry>, BoxError> {
        Ok(vec![model_entry()])
    }

    async fn invoke_model(
        &self,
        _model: &str,
        request: CompletionRequest,
    ) -> Result<ModelStream, CompletionError> {
        let previous_calls = request
            .chat_history
            .iter()
            .flat_map(|message| match message {
                Message::Assistant { content } => content
                    .iter()
                    .filter_map(|content| match content {
                        AssistantContent::ToolCall(call) => Some(call.function.name.as_str()),
                        _ => None,
                    })
                    .collect::<Vec<_>>(),
                _ => vec![],
            })
            .collect::<Vec<_>>();
        let next = if self.only_text {
            StreamChunk::Text("still exploring".into())
        } else if !previous_calls.contains(&"research_context") {
            StreamChunk::ToolCall(ToolCall::new("context", "research_context", json!({})))
        } else if !previous_calls.contains(&"run_experiment") {
            // Deliberately try to choose an idempotency key. The bridge must
            // replace it with thread/call identity, even for malformed model output.
            StreamChunk::ToolCall(ToolCall::new(
                "experiment",
                "run_experiment",
                json!({
                    "idempotency_key": "model-chosen-key",
                    "hypothesis": "a runnable test candidate produces measurable evidence",
                    "family": "baseline",
                    "parent_id": null,
                    "files": {"main.py": "print('candidate')\n"},
                    "timeout_seconds": 1,
                    "seed": 42,
                }),
            ))
        } else if !previous_calls.contains(&"record_finding") {
            StreamChunk::ToolCall(ToolCall::new(
                "finding",
                "record_finding",
                json!({
                    "idempotency_key": "another-model-key",
                    "kind": "observation",
                    "text": "The trusted evaluator returned score 0.5.",
                    "evidence_ids": ["evidence-1"],
                }),
            ))
        } else {
            StreamChunk::Text("evidence recorded".into())
        };
        self.requests
            .lock()
            .expect("bug: request history poisoned")
            .push(request);
        Ok(Box::pin(futures_util::stream::iter(vec![
            Ok(next),
            Ok(StreamChunk::Final(FinalResponse {
                usage: Some(Usage {
                    input_tokens: 10,
                    output_tokens: 5,
                    total_tokens: 15,
                    cached_input_tokens: 0,
                }),
            })),
        ])))
    }
}

fn options(workers: usize) -> RunOptions {
    RunOptions {
        workers,
        max_output_tokens: 512,
        poll_interval: Duration::from_millis(10),
        model_response_timeout: Duration::from_secs(2),
        model_call_timeout: Duration::from_secs(5),
    }
}

#[tokio::test(flavor = "current_thread")]
async fn native_workers_dispatch_concurrently_and_record_provenance() {
    let backend = Arc::new(MemoryBackend::new(40, 2));
    let provider = Arc::new(ScriptedProvider::new(false));
    let summary = tokio::time::timeout(
        Duration::from_secs(5),
        run_campaign(
            options(2),
            backend.clone(),
            provider.clone(),
            "mock",
            std::future::pending(),
        ),
    )
    .await
    .expect("native workers must terminate")
    .expect("run native campaign");
    assert_eq!(summary.workers, 2);
    assert_eq!(summary.stop_reason, "evidence collected");
    assert_eq!(backend.peak_experiments.load(Ordering::SeqCst), 2);
    let state = backend.state.lock().expect("bug: mock ledger poisoned");
    assert_eq!(state.recovered, 1);
    assert_eq!(state.experiments.len(), 2);
    assert_eq!(state.findings.len(), 2);
    let keys = state
        .experiments
        .iter()
        .map(|experiment| {
            experiment["idempotency_key"]
                .as_str()
                .expect("transport key")
        })
        .collect::<Vec<_>>();
    assert_ne!(keys[0], keys[1]);
    assert!(keys.iter().all(|key| key.ends_with(":experiment")));
    assert!(state.findings.iter().all(|finding| {
        finding["idempotency_key"]
            .as_str()
            .expect("finding key")
            .ends_with(":finding")
    }));
    assert_eq!(state.usage.len(), summary.model_calls_this_process as usize);
    drop(state);
    let requests = provider
        .requests
        .lock()
        .expect("bug: request history poisoned");
    assert!(!requests.is_empty());
    for request in requests.iter() {
        assert_eq!(request.max_tokens, Some(64), "backend output limit wins");
        assert_eq!(request.tools.len(), 4);
        assert!(!request.tools.iter().any(|tool| tool.name == "spawn_thread"));
    }
    assert!(
        requests
            .iter()
            .any(|request| request.chat_history.iter().any(|message| {
                matches!(message, Message::User { content } if content.iter().any(|part| {
                    matches!(part, UserContent::ToolResult(result) if result.id == "experiment")
                }))
            })),
        "backend experiment results must return through native tool history"
    );
}

#[tokio::test(flavor = "current_thread")]
async fn idle_workers_continue_automatically_but_cannot_overrun_persisted_model_budget() {
    let backend = Arc::new(MemoryBackend::new(3, 1));
    let provider = Arc::new(ScriptedProvider::new(true));
    let summary = tokio::time::timeout(
        Duration::from_secs(5),
        run_campaign(
            options(1),
            backend.clone(),
            provider.clone(),
            "mock",
            std::future::pending(),
        ),
    )
    .await
    .expect("budget stops idle restarts")
    .expect("bounded campaign");
    assert_eq!(summary.model_calls_this_process, 3);
    assert_eq!(summary.stop_reason, "model call budget exhausted");
    assert_eq!(
        provider
            .requests
            .lock()
            .expect("bug: request history poisoned")
            .len(),
        3
    );

    // Start another native launcher against the same campaign ledger. A fresh
    // conversation must not create a fresh budget.
    let second = tokio::time::timeout(
        Duration::from_secs(5),
        run_campaign(
            options(1),
            backend,
            provider,
            "mock",
            std::future::pending(),
        ),
    )
    .await
    .expect("restart respects spent budget")
    .expect("resumed campaign");
    assert_eq!(second.model_calls_this_process, 0);
}

#[tokio::test(flavor = "current_thread")]
async fn campaign_worker_limit_is_checked_before_any_model_call() {
    let backend = Arc::new(MemoryBackend::new(20, 1));
    let provider = Arc::new(ScriptedProvider::new(true));
    let error = run_campaign(
        options(2),
        backend.clone(),
        provider,
        "mock",
        std::future::pending(),
    )
    .await
    .expect_err("worker bound must be enforced");
    assert!(error.to_string().contains("max_workers"));
    assert!(
        backend
            .state
            .lock()
            .expect("bug: mock ledger poisoned")
            .reservations
            .is_empty()
    );
}

#[tokio::test(flavor = "current_thread")]
async fn model_admission_is_atomic_under_concurrent_invocation() {
    let backend = Arc::new(MemoryBackend::new(3, 8));
    let inner = Arc::new(ScriptedProvider::new(true));
    let provider = BudgetedProvider::new(
        inner.clone(),
        backend.clone(),
        512,
        8,
        Duration::from_secs(2),
    )
    .expect("construct budgeted provider");
    let results = futures_util::future::join_all((0..12).map(|_| async {
        let mut stream = provider
            .invoke_model("mock", CompletionRequest::default())
            .await?;
        while let Some(item) = stream.next().await {
            item?;
        }
        Ok::<(), CompletionError>(())
    }))
    .await;
    assert_eq!(results.iter().filter(|result| result.is_ok()).count(), 3);
    assert_eq!(
        inner
            .requests
            .lock()
            .expect("bug: request history poisoned")
            .len(),
        3
    );
    assert_eq!(
        backend
            .state
            .lock()
            .expect("bug: mock ledger poisoned")
            .usage
            .len(),
        3
    );
}

struct FailingProvider;

#[async_trait]
impl ModelProvider for FailingProvider {
    async fn list_models(&self) -> Result<Vec<ModelEntry>, BoxError> {
        Ok(vec![model_entry()])
    }

    async fn invoke_model(
        &self,
        _model: &str,
        _request: CompletionRequest,
    ) -> Result<ModelStream, CompletionError> {
        Err(CompletionError::provider(
            ErrorClass::Fatal,
            "invalid provider key",
        ))
    }
}

#[tokio::test(flavor = "current_thread")]
async fn fatal_provider_errors_stop_the_launcher_and_are_not_success_reports() {
    let backend = Arc::new(MemoryBackend::new(20, 1));
    let error = tokio::time::timeout(
        Duration::from_secs(5),
        run_campaign(
            options(1),
            backend.clone(),
            Arc::new(FailingProvider),
            "mock",
            std::future::pending(),
        ),
    )
    .await
    .expect("fatal provider errors must terminate")
    .expect_err("invalid credentials must not produce a successful report");
    assert!(error.to_string().contains("invalid provider key"));
    assert_eq!(
        backend
            .state
            .lock()
            .expect("bug: mock ledger poisoned")
            .reservations
            .len(),
        1
    );
}

#[tokio::test(flavor = "current_thread")]
async fn one_hundred_native_workers_share_four_experiment_slots_and_drain_at_the_call_limit() {
    let backend = Arc::new(MemoryBackend {
        experiment_slots: tokio::sync::Semaphore::new(4),
        // Force every worker to reach the real native tool queue before results
        // can return. This makes the drain-at-budget scenario deterministic.
        experiment_barrier: Some(tokio::sync::Barrier::new(100)),
        ..MemoryBackend::new(250, 100)
    });
    let provider = Arc::new(ScriptedProvider::new(false));
    let summary = tokio::time::timeout(
        Duration::from_secs(10),
        run_campaign(
            options(100),
            backend.clone(),
            provider.clone(),
            "mock",
            std::future::pending(),
        ),
    )
    .await
    .expect("100 native workers must drain and terminate")
    .expect("bounded 100-worker campaign");
    assert_eq!(summary.workers, 100);
    assert_eq!(summary.model_calls_this_process, 250);
    assert_eq!(summary.stop_reason, "model call budget exhausted");
    assert_eq!(backend.peak_experiments.load(Ordering::SeqCst), 4);
    assert_eq!(backend.active_experiments.load(Ordering::SeqCst), 0);
    let ledger = backend.state.lock().expect("bug: mock ledger poisoned");
    assert_eq!(
        ledger.experiments.len(),
        100,
        "every admitted experiment must drain"
    );
    assert_eq!(ledger.reservations.len(), 250);
    assert_eq!(ledger.usage.len(), 250);
    assert_eq!(
        provider
            .requests
            .lock()
            .expect("bug: request history poisoned")
            .len(),
        250
    );
}

struct ContinuouslyStreamingProvider;

#[async_trait]
impl ModelProvider for ContinuouslyStreamingProvider {
    async fn list_models(&self) -> Result<Vec<ModelEntry>, BoxError> {
        Ok(vec![model_entry()])
    }

    async fn invoke_model(
        &self,
        _model: &str,
        _request: CompletionRequest,
    ) -> Result<ModelStream, CompletionError> {
        Ok(Box::pin(async_stream::stream! {
            loop {
                tokio::time::sleep(Duration::from_millis(5)).await;
                yield Ok(StreamChunk::Text("still producing output".into()));
            }
        }))
    }
}

#[tokio::test]
async fn continuous_streams_obey_both_total_call_and_remaining_search_deadlines() {
    for (total, remaining) in [
        (Duration::from_millis(50), 3600.0),
        (Duration::from_secs(2), 0.05),
    ] {
        let backend = Arc::new(MemoryBackend {
            admission_seconds: remaining,
            ..MemoryBackend::new(1, 1)
        });
        let provider = BudgetedProvider::new(
            Arc::new(ContinuouslyStreamingProvider),
            backend.clone(),
            64,
            1,
            Duration::from_millis(100),
        )
        .expect("budgeted provider")
        .with_call_timeout(total)
        .expect("total model deadline");
        let started = tokio::time::Instant::now();
        let mut stream = provider
            .invoke_model("mock", CompletionRequest::default())
            .await
            .expect("admitted model call");
        let mut chunks = 0;
        let mut deadline_error = None;
        while let Some(item) = stream.next().await {
            match item {
                Ok(_) => chunks += 1,
                Err(error) => {
                    deadline_error = Some(error);
                    break;
                }
            }
        }
        drop(stream);
        assert!(
            chunks > 0,
            "the stream remains active before the total deadline"
        );
        assert!(
            started.elapsed() < Duration::from_millis(500),
            "active streams cannot run indefinitely"
        );
        assert!(
            deadline_error
                .expect("total deadline error")
                .to_string()
                .contains("total model-call deadline")
        );
        assert_eq!(provider.in_flight(), 0);
        let ledger = backend.state.lock().expect("bug: mock ledger poisoned");
        assert_eq!(ledger.reservations.len(), 1);
        assert!(
            ledger.usage.is_empty(),
            "unknown usage is not fabricated after cancellation"
        );
    }
}
