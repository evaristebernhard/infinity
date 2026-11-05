//! Bounded worker launch, event handling, and autonomous continuation.

use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::sync::Arc;
use std::time::Duration;

use futures_util::FutureExt;
use infinity_agent_core::message::InputMessage;
use infinity_agent_core::stores::{InMemoryConversationStore, InMemoryStateStore};
use infinity_agent_core::system::local::ThreadLifecycleState;
use infinity_agent_core::system::{AgentEvent, AgentSystemBuilder, StaticModel};
use infinity_agent_core::traits::InputSender;
use infinity_provider_protocol::ModelProvider;
use rap_protocol::ThreadId;
use serde::Serialize;
use serde_json::{Value, json};
use tokio::sync::mpsc;

use crate::tools::{ToolActivity, campaign_tools};
use crate::{BoxError, BudgetedProvider, ResearchBackend};

/// Runtime bounds, separate from the campaign's persistent global budgets.
#[derive(Clone, Debug)]
pub struct RunOptions {
    pub workers: usize,
    pub max_output_tokens: u64,
    pub poll_interval: Duration,
    pub model_response_timeout: Duration,
    pub model_call_timeout: Duration,
}

impl Default for RunOptions {
    fn default() -> Self {
        Self {
            workers: 4,
            max_output_tokens: 8192,
            poll_interval: Duration::from_secs(2),
            model_response_timeout: Duration::from_secs(180),
            model_call_timeout: Duration::from_secs(900),
        }
    }
}

/// Process-level activity summary. Campaign totals remain in the backend.
#[derive(Debug, Serialize)]
pub struct RunSummary {
    pub workers: usize,
    pub stop_reason: String,
    pub model_calls_this_process: u64,
    pub tool_operations_this_process: u64,
}

fn is_budget_stop(reason: &str) -> bool {
    matches!(
        reason,
        "wall_time_budget_exhausted"
            | "experiment_budget_exhausted"
            | "trial_time_budget_exhausted"
            | "model_call_budget_exhausted"
    )
}

/// Run the one-time final evaluation after a normal budget stop, if eligible.
///
/// This is host policy, never a model tool. Owner stops, interrupted launchers,
/// infrastructure failures, and any previously attempted final run are excluded.
/// A failed final evaluation is propagated and never automatically retried.
pub async fn finalize_if_eligible(
    backend: &dyn ResearchBackend,
    summary: &RunSummary,
) -> Result<Option<Value>, BoxError> {
    if !is_budget_stop(&summary.stop_reason) {
        return Ok(None);
    }
    let context = backend.call("context", json!({})).await?;
    if context.get("run_kind").and_then(Value::as_str) == Some("generic") {
        return Ok(None);
    }
    if !context
        .get("stop_reason")
        .and_then(Value::as_str)
        .is_some_and(is_budget_stop)
        || context.get("final_status") != Some(&Value::Null)
        || context
            .get("best_candidate_id")
            .and_then(Value::as_str)
            .is_none()
    {
        return Ok(None);
    }
    tracing::info!("finalizing the promoted candidate once on the sealed holdout");
    Ok(Some(backend.call("finalize", json!({})).await?))
}

fn positive_limit(context: &Value, name: &str) -> Result<u64, BoxError> {
    context
        .get(name)
        .or_else(|| context.get("budget").and_then(|budget| budget.get(name)))
        .and_then(Value::as_u64)
        .filter(|limit| *limit > 0)
        .ok_or_else(|| format!("campaign context must include a positive {name}").into())
}

fn stop_reason(context: &Value) -> Result<Option<String>, BoxError> {
    match context.get("stop").and_then(Value::as_bool) {
        Some(false) => Ok(None),
        Some(true) => Ok(Some(
            context
                .get("stop_reason")
                .and_then(Value::as_str)
                .unwrap_or("campaign completed or budget exhausted")
                .to_owned(),
        )),
        None => Err("campaign context must include a boolean stop field".into()),
    }
}

fn continuation_prompt(worker: usize, workers: usize, context: &Value) -> String {
    if context.get("run_kind").and_then(Value::as_str) == Some("generic") {
        return format!(
            "You are worker {} of {} available native workers. Claim useful ready work, \
             or continue your own current lease. Read relevant skills and dependency results. \
             Decompose when independent work would help; use tools to produce evidence and \
             persist results. If no work is ready, end this turn and let the supervisor wait. \
             This persisted state is authoritative after a restart.\n\n{}",
            worker + 1,
            workers,
            context,
        );
    }
    format!(
        "You are research worker {} of {}. Use the latest persisted campaign state below. \
         Choose a useful experiment, execute it, record the evidence, and continue. \
         Coordinate through the shared candidate history and findings. \
         If you have just resumed, this state is authoritative; previous unrecorded reasoning is unavailable.\n\n{}",
        worker + 1,
        workers,
        context,
    )
}

/// Generic campaigns wake only enough idle workers for ready jobs, plus owners
/// of unfinished leases. Existing experiment campaigns retain eager continuation.
fn workers_to_wake(
    idle: &HashSet<ThreadId>,
    context: &Value,
    workers: &HashMap<ThreadId, usize>,
) -> HashSet<ThreadId> {
    if context.get("run_kind").and_then(Value::as_str) != Some("generic") {
        return idle.clone();
    }
    let owners: HashSet<&str> = context
        .get("active_leases")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|lease| lease.get("owner").and_then(Value::as_str))
        .collect();
    let mut selected: HashSet<ThreadId> = idle
        .iter()
        .filter(|id| owners.contains(id.as_str()))
        .cloned()
        .collect();
    let pending_claims = workers
        .keys()
        .filter(|id| !idle.contains(*id) && !owners.contains(id.as_str()))
        .count() as u64;
    let ready = context
        .get("ready_work_count")
        .and_then(Value::as_u64)
        .unwrap_or(0)
        .saturating_sub(pending_claims);
    selected.extend(
        idle.iter()
            .filter(|id| !owners.contains(id.as_str()))
            .take(ready.min(idle.len() as u64) as usize)
            .cloned(),
    );
    selected
}

/// Run real Infinity workers until the campaign stops or `shutdown` resolves.
///
/// Every run creates new conversation threads from the durable campaign context.
/// In-flight tools drain before this function returns, including after a stop
/// signal. The backend independently bounds every experiment's execution time.
pub async fn run_campaign(
    options: RunOptions,
    backend: Arc<dyn ResearchBackend>,
    provider: Arc<dyn ModelProvider>,
    model_id: &str,
    shutdown: impl Future<Output = ()>,
) -> Result<RunSummary, BoxError> {
    if options.workers == 0 || options.poll_interval.is_zero() {
        return Err("workers and poll interval must be positive".into());
    }
    tracing::info!("recovering previously admitted campaign experiments");
    backend.call("recover", json!({})).await?;
    let context = backend.call("context", json!({})).await?;
    if let Some(reason) = stop_reason(&context)? {
        return Ok(RunSummary {
            workers: 0,
            stop_reason: reason,
            model_calls_this_process: 0,
            tool_operations_this_process: 0,
        });
    }
    let generic_campaign = context.get("run_kind").and_then(Value::as_str) == Some("generic");
    if generic_campaign {
        let lease_seconds = context
            .get("budget")
            .and_then(|limits| limits.get("lease_seconds"))
            .and_then(Value::as_f64)
            .ok_or("generic campaign must provide budget.lease_seconds")?;
        if lease_seconds <= options.poll_interval.as_secs_f64() * 3.0 + 5.0 {
            return Err(
                "generic lease_seconds must exceed three poll intervals plus five seconds".into(),
            );
        }
    }
    let max_workers = positive_limit(&context, "max_workers")?;
    if options.workers as u64 > max_workers {
        return Err(format!(
            "requested {} workers exceeds campaign max_workers {max_workers}",
            options.workers,
        )
        .into());
    }
    let output_limit = options
        .max_output_tokens
        .min(positive_limit(&context, "max_output_tokens")?);
    let provider = Arc::new(
        BudgetedProvider::new(
            provider,
            backend.clone(),
            output_limit,
            options.workers,
            options.model_response_timeout,
        )?
        .with_call_timeout(options.model_call_timeout)?,
    );
    let model = StaticModel::new(provider.clone(), model_id).await?;
    let local = tokio::task::LocalSet::new();
    local.run_until(async move {
        let activity = Arc::new(ToolActivity::default());
        let tools = campaign_tools(&context, backend.clone(), activity.clone())?;
        let prompt = if context.get("run_kind").and_then(Value::as_str) == Some("generic") {
            include_str!("generic_worker_prompt.md")
        } else {
            include_str!("worker_prompt.md")
        };
        let mut system = AgentSystemBuilder::new_local(
            InMemoryConversationStore::new(),
            InMemoryStateStore::new(),
            model,
        )
        .without_builtin_tools()
        .tools(tools)
        .extra_system_prompt(prompt)
        .start();
        let sender = system.sender();
        let mut worker_ids = HashMap::<ThreadId, usize>::new();
        let mut idle = HashSet::<ThreadId>::new();
        let (events_tx, mut events_rx) = mpsc::unbounded_channel();
        let mut event_tasks = Vec::new();
        for index in 0..options.workers {
            let mut handle = system.thread_builder().launch().await;
            let id = handle.thread_id().to_owned();
            worker_ids.insert(id.clone(), index);
            let start_now = context.get("run_kind").and_then(Value::as_str) != Some("generic")
                || (index as u64) < context.get("ready_work_count").and_then(Value::as_u64).unwrap_or(0);
            if start_now {
                handle.send_user_text(continuation_prompt(index, options.workers, &context)).await?;
            } else {
                idle.insert(id.clone());
            }
            let events_tx = events_tx.clone();
            event_tasks.push(tokio::task::spawn_local(async move {
                while let Some(event) = handle.recv().await {
                    // Text and reasoning may be large. The supervisor needs only
                    // coarse progress, diagnostics, and usage; do not duplicate
                    // full conversations into another unbounded channel.
                    if matches!(event, AgentEvent::ToolCall { .. } | AgentEvent::Info { .. })
                        && events_tx.send((id.clone(), event)).is_err()
                    {
                        break;
                    }
                }
            }));
        }
        drop(events_tx);
        let mut ticker = tokio::time::interval(options.poll_interval);
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        tokio::pin!(shutdown);
        let mut external_stop_seen = false;
        let mut fatal_error: Option<String> = None;
        loop {
            tokio::select! {
                _ = &mut shutdown, if !external_stop_seen => {
                    external_stop_seen = true;
                    provider.stop("launcher stop requested; admitted tools drained");
                    tracing::info!("stopping new model calls and draining in-flight research tools");
                }
                lifecycle = system.next_lifecycle_event() => {
                    let Some(lifecycle) = lifecycle else {
                        fatal_error = Some("Infinity lifecycle stream unexpectedly closed".to_owned());
                        break;
                    };
                    if worker_ids.contains_key(&lifecycle.thread_id) {
                        match lifecycle.state {
                            ThreadLifecycleState::Live => { idle.remove(&lifecycle.thread_id); }
                            ThreadLifecycleState::Idle => { idle.insert(lifecycle.thread_id); }
                        }
                    }
                }
                Some((thread_id, event)) = events_rx.recv() => {
                    match event {
                        AgentEvent::ToolCall { name, .. } => tracing::info!(%thread_id, tool = %name, "research operation"),
                        AgentEvent::Info { text } => tracing::info!(%thread_id, "{text}"),
                        _ => {}
                    }
                }
                _ = ticker.tick() => {
                    if !provider.is_stopping() {
                        // A model may think longer than one work lease. Renew
                        // leases for live native owners before polling expiry;
                        // model-call deadlines still bound a hung provider.
                        while let Some(Some(event)) = system.next_lifecycle_event().now_or_never() {
                            if worker_ids.contains_key(&event.thread_id) {
                                match event.state {
                                    ThreadLifecycleState::Live => { idle.remove(&event.thread_id); }
                                    ThreadLifecycleState::Idle => { idle.insert(event.thread_id); }
                                }
                            }
                        }
                        let current = async {
                            if generic_campaign {
                                let owners: Vec<&str> = worker_ids.keys()
                                    .filter(|id| !idle.contains(*id)).map(ThreadId::as_str).collect();
                                backend.call("heartbeat", json!({"owners": owners})).await?;
                            }
                            backend.call("context", json!({})).await
                        }.await;
                        match current {
                            Ok(current) => match stop_reason(&current) {
                                Ok(Some(reason)) => provider.stop(reason),
                                Ok(None) => {
                                    // Reading SQLite crosses an await boundary.
                                    // Drain any newer Live events before nudging
                                    // idle workers, or stale Idle state can send
                                    // a user message that cancels an admitted call.
                                    while let Some(Some(event)) = system.next_lifecycle_event().now_or_never() {
                                        if worker_ids.contains_key(&event.thread_id) {
                                            match event.state {
                                                ThreadLifecycleState::Live => { idle.remove(&event.thread_id); }
                                                ThreadLifecycleState::Idle => { idle.insert(event.thread_id); }
                                            }
                                        }
                                    }
                                    for thread_id in workers_to_wake(&idle, &current, &worker_ids) {
                                        idle.remove(&thread_id);
                                        let index = worker_ids[&thread_id];
                                        let input = InputMessage::user_text(thread_id, continuation_prompt(index, options.workers, &current));
                                        if let Err(error) = sender.send_to_input_queue(input, &uuid::Uuid::new_v4().to_string()).await {
                                            let message = format!("cannot resume research worker: {error}");
                                            provider.stop(message.clone());
                                            fatal_error = Some(message);
                                            break;
                                        }
                                    }
                                }
                                Err(error) => {
                                    let message = format!("invalid campaign context: {error}");
                                    provider.stop(message.clone());
                                    fatal_error = Some(message);
                                }
                            },
                            Err(error) => {
                                let message = format!("cannot read persisted campaign context: {error}");
                                provider.stop(message.clone());
                                fatal_error = Some(message);
                            }
                        }
                    }
                }
            }
            if provider.is_stopping()
                && activity.active() == 0
                && provider.in_flight() == 0
                && system.is_idle()
            {
                break;
            }
        }
        system.shutdown().await;
        for task in event_tasks {
            if let Err(error) = task.await {
                tracing::error!(%error, "research event consumer failed");
                fatal_error.get_or_insert_with(|| format!("research event consumer failed: {error}"));
            }
        }
            if let Some(error) = fatal_error.or_else(|| provider.failure()) {
            return Err(error.into());
        }
        let stop_reason = provider.stop_reason().unwrap_or_else(|| "campaign stopped".into());
        if is_budget_stop(&stop_reason) {
            let context = backend.call("context", json!({})).await?;
            if context.get("active_experiments").and_then(Value::as_u64).unwrap_or(0) > 0 {
                // A backend process may die after committing a proposal but
                // before returning its result. Native tool counters cannot
                // see that orphan; drain the authoritative persistent queue.
                tracing::info!("draining remaining admitted experiments from the campaign ledger");
                backend.call("recover", json!({})).await?;
            }
        }
        Ok(RunSummary {
            workers: options.workers,
            stop_reason,
            model_calls_this_process: provider.calls_this_process(),
            tool_operations_this_process: activity.completed(),
        })
    }).await
}
