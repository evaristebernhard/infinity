//! Bounded worker launch, event handling, and autonomous continuation.

use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::sync::Arc;
use std::time::Duration;

use futures_util::FutureExt;
use infinity_agent_core::message::InputMessage;
use infinity_agent_core::stores::{InMemoryConversationStore, InMemoryStateStore};
use infinity_agent_core::system::local::{
    LaunchingSystem, ThreadLifecycleEvent, ThreadLifecycleState,
};
use infinity_agent_core::system::{AgentEvent, AgentSystemBuilder, NoRapHttp, StaticModel};
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

type NativeSystem = LaunchingSystem<InMemoryConversationStore, InMemoryStateStore, NoRapHttp>;

/// Resident conversations are different from admitted execution slots. Flat
/// campaigns launch a fixed pool; hierarchy campaigns add one conversation per
/// logical agent, including parents parked outside the execution limit.
struct WorkerThreads {
    ids: HashMap<ThreadId, usize>,
    idle: HashSet<ThreadId>,
    events: mpsc::UnboundedSender<(ThreadId, AgentEvent)>,
    tasks: Vec<tokio::task::JoinHandle<()>>,
}

impl WorkerThreads {
    fn new(events: mpsc::UnboundedSender<(ThreadId, AgentEvent)>) -> Self {
        Self {
            ids: HashMap::new(),
            idle: HashSet::new(),
            events,
            tasks: Vec::new(),
        }
    }

    async fn launch(&mut self, system: &NativeSystem) -> ThreadId {
        let mut handle = system.thread_builder().launch().await;
        let id = handle.thread_id().to_owned();
        self.ids.insert(id.clone(), self.ids.len());
        self.idle.insert(id.clone());
        let events = self.events.clone();
        let observed_id = id.clone();
        self.tasks.push(tokio::task::spawn_local(async move {
            while let Some(event) = handle.recv().await {
                // Never copy full transcripts into the supervisor's queue.
                if matches!(event, AgentEvent::ToolCall { .. } | AgentEvent::Info { .. })
                    && events.send((observed_id.clone(), event)).is_err()
                {
                    break;
                }
            }
        }));
        id
    }

    fn lifecycle(&mut self, event: ThreadLifecycleEvent) {
        if self.ids.contains_key(&event.thread_id) {
            match event.state {
                ThreadLifecycleState::Live => {
                    self.idle.remove(&event.thread_id);
                }
                ThreadLifecycleState::Idle => {
                    self.idle.insert(event.thread_id);
                }
            }
        }
    }

    fn drain_lifecycle(&mut self, system: &mut NativeSystem) {
        while let Some(Some(event)) = system.next_lifecycle_event().now_or_never() {
            self.lifecycle(event);
        }
    }
}

struct HierarchyScheduler {
    work_threads: HashMap<String, ThreadId>,
    /// Last observed leases, updated immediately for successful host claims.
    /// Include previous-process owners until their durable leases expire.
    leases: HashMap<String, String>,
    capacity: usize,
    max_contexts: usize,
}

impl HierarchyScheduler {
    fn new(context: &Value, capacity: usize) -> Result<Self, BoxError> {
        let config = &context["delegation"];
        let depth = config["max_depth"]
            .as_u64()
            .filter(|depth| *depth <= 2)
            .ok_or("delegation.max_depth must be an integer in 0..2")?;
        let children = config["max_children_per_work"]
            .as_u64()
            .filter(|children| (1..=32).contains(children))
            .ok_or("delegation.max_children_per_work must be an integer in 1..32")?;
        let tree_limit: u64 = (0..=depth).map(|level| children.pow(level as u32)).sum();
        let max_contexts = tree_limit.min(positive_limit(context, "max_work_items")?) as usize;
        let mut scheduler = Self {
            work_threads: HashMap::new(),
            leases: HashMap::new(),
            capacity,
            max_contexts,
        };
        scheduler.observe_leases(context)?;
        Ok(scheduler)
    }

    fn observe_leases(&mut self, context: &Value) -> Result<(), BoxError> {
        let leases = context["active_leases"]
            .as_array()
            .ok_or("hierarchy context requires active_leases")?;
        self.leases = leases
            .iter()
            .map(|lease| {
                let owner = lease["owner"].as_str().ok_or("lease requires owner")?;
                let work_id = lease["work_id"].as_str().ok_or("lease requires work_id")?;
                Ok((owner.to_owned(), work_id.to_owned()))
            })
            .collect::<Result<_, BoxError>>()?;
        Ok(())
    }

    fn live_owners(&self, threads: &WorkerThreads, activity: &ToolActivity) -> Vec<String> {
        self.leases
            .keys()
            .filter(|owner| {
                threads
                    .ids
                    .keys()
                    .any(|id| id.as_str() == owner.as_str() && !threads.idle.contains(id))
                    && !activity.is_parked(owner)
            })
            .cloned()
            .collect()
    }

    async fn schedule(
        &mut self,
        system: &mut NativeSystem,
        threads: &mut WorkerThreads,
        context: &Value,
        backend: &dyn ResearchBackend,
        activity: &ToolActivity,
    ) -> Result<(), BoxError> {
        self.observe_leases(context)?;
        threads.drain_lifecycle(system);
        let mut candidates: Vec<String> = self
            .leases
            .iter()
            .filter_map(|(owner, work)| {
                self.work_threads
                    .get(work)
                    .filter(|id| id.as_str() == owner && threads.idle.contains(*id))
                    .map(|_| work.clone())
            })
            .collect();
        let ready = context["ready_work"]
            .as_array()
            .ok_or("hierarchy context requires ready_work")?;
        for work in ready {
            let work_id = work["work_id"]
                .as_str()
                .ok_or("ready work requires work_id")?;
            if !candidates.iter().any(|candidate| candidate == work_id) {
                candidates.push(work_id.to_owned());
            }
        }
        let sender = system.sender();
        for work_id in candidates {
            threads.drain_lifecycle(system);
            let thread_id = if let Some(id) = self.work_threads.get(&work_id) {
                if !threads.idle.contains(id) && !activity.is_parked(id.as_str()) {
                    continue;
                }
                if !self.leases.contains_key(id.as_str()) && self.leases.len() >= self.capacity {
                    continue;
                }
                id.clone()
            } else {
                if self.leases.len() >= self.capacity {
                    continue;
                }
                if self.work_threads.len() >= self.max_contexts {
                    return Err("logical agent contexts exceed the sealed delegation bound".into());
                }
                let id = threads.launch(system).await;
                let binding = backend
                    .call(
                        "bind_worker",
                        json!({
                            "owner": id, "work_id": work_id,
                        }),
                    )
                    .await?;
                if binding["bound"] != true
                    || binding["owner"] != id.as_str()
                    || binding["work_id"] != work_id
                {
                    return Err("host returned an invalid logical agent binding".into());
                }
                activity.bind_worker(id.as_str(), &work_id)?;
                self.work_threads.insert(work_id.clone(), id.clone());
                id
            };
            // Claim before inference. This is host transport authority, never
            // an owner or ancestor chosen by the model. A new context cannot
            // inspect another conversation merely by passing its work ID.
            let claim = backend.call("invoke_tool", json!({
                "tool": "claim_work", "arguments": {"work_id": work_id},
                "owner": thread_id,
                "request_key": format!("{}:supervisor-claim:{}", thread_id, uuid::Uuid::new_v4()),
            })).await?;
            if claim["claimed"] != true {
                continue;
            }
            if claim["work_id"] != work_id
                || claim["work"]["lease_owner"] != thread_id.as_str()
                || claim["lease_token"].as_str().is_none()
            {
                return Err("host returned an invalid logical agent lease".into());
            }
            self.leases.insert(thread_id.as_str().to_owned(), work_id);
            threads.drain_lifecycle(system);
            threads.idle.remove(&thread_id);
            if !activity
                .resume_wait(thread_id.as_str(), &claim, &sender)
                .await?
            {
                let input = InputMessage::user_text(
                    thread_id,
                    hierarchy_assignment_prompt(context, &claim),
                );
                sender
                    .send_to_input_queue(input, &uuid::Uuid::new_v4().to_string())
                    .await?;
            }
        }
        Ok(())
    }
}

fn hierarchy_assignment_prompt(context: &Value, claim: &Value) -> String {
    // Deliberately exclude recent_work, arbitrary siblings, and the parent's
    // transcript. Explicit briefs and durable dependency/effect reads carry
    // the information that an independent logical agent needs.
    let assignment = json!({
        "logical_agent_id": claim["work_id"], "claim": claim,
        "delegation": context["delegation"], "budget": context["budget"],
        "task_sha256": context["task_sha256"], "capabilities": context["capabilities"],
        "skill_catalog": context["skill_catalog"],
        "skill_snapshot_sha256": context["skill_snapshot_sha256"],
    });
    format!(
        "The supervisor assigned this conversation to one logical agent. Use the claim below; \
         its lease is already active. Continue this agent's goal and inspect its durable effects \
         before repeating earlier work. Delegate only through delegate_work, then use wait_work \
         to join when needed. A completed agent must end its turn.\n\n{assignment}",
    )
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
    let mut hierarchy = if generic_campaign && context.get("delegation").is_some() {
        Some(HierarchyScheduler::new(&context, options.workers)?)
    } else {
        None
    };
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
    let model_concurrency = if context.get("max_parallel_model_calls").is_some()
        || context
            .get("budget")
            .and_then(|budget| budget.get("max_parallel_model_calls"))
            .is_some()
    {
        (options.workers as u64).min(positive_limit(&context, "max_parallel_model_calls")?) as usize
    } else {
        options.workers
    };
    let provider = Arc::new(
        BudgetedProvider::new(
            provider,
            backend.clone(),
            output_limit,
            model_concurrency,
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
        let (events_tx, mut events_rx) = mpsc::unbounded_channel();
        let mut threads = WorkerThreads::new(events_tx);
        if hierarchy.is_none() {
            for index in 0..options.workers {
                let id = threads.launch(&system).await;
                let start_now = !generic_campaign
                    || (index as u64) < context.get("ready_work_count").and_then(Value::as_u64).unwrap_or(0);
                if start_now {
                    threads.drain_lifecycle(&mut system);
                    threads.idle.remove(&id);
                    let input = InputMessage::user_text(id, continuation_prompt(index, options.workers, &context));
                    sender.send_to_input_queue(input, &uuid::Uuid::new_v4().to_string()).await?;
                }
            }
        }
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
                    threads.lifecycle(lifecycle);
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
                        threads.drain_lifecycle(&mut system);
                        let current = async {
                            if generic_campaign {
                                let owners: Vec<String> = match &hierarchy {
                                    Some(scheduler) => scheduler.live_owners(&threads, &activity),
                                    None => threads.ids.keys().filter(|id| !threads.idle.contains(*id))
                                        .map(|id| id.as_str().to_owned()).collect(),
                                };
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
                                    threads.drain_lifecycle(&mut system);
                                    if let Some(scheduler) = &mut hierarchy {
                                        if let Err(error) = scheduler.schedule(
                                            &mut system, &mut threads, &current, backend.as_ref(), &activity,
                                        ).await {
                                            // Admission can close between the
                                            // ready snapshot and the host claim.
                                            // A normal owner/budget/workflow stop
                                            // must keep its authoritative reason.
                                            let stopped = match backend.call("context", json!({})).await {
                                                Ok(latest) => stop_reason(&latest),
                                                Err(context_error) => Err(context_error),
                                            };
                                            match stopped {
                                                Ok(Some(reason)) => provider.stop(reason),
                                                Ok(None) => {
                                                    let message = format!("cannot schedule logical agent: {error}");
                                                    provider.stop(message.clone());
                                                    fatal_error = Some(message);
                                                }
                                                Err(context_error) => {
                                                    let message = format!("cannot schedule logical agent: {error}; cannot confirm stop: {context_error}");
                                                    provider.stop(message.clone());
                                                    fatal_error = Some(message);
                                                }
                                            }
                                        }
                                    } else {
                                        for thread_id in workers_to_wake(&threads.idle, &current, &threads.ids) {
                                            threads.idle.remove(&thread_id);
                                            let index = threads.ids[&thread_id];
                                            let input = InputMessage::user_text(thread_id, continuation_prompt(index, options.workers, &current));
                                            if let Err(error) = sender.send_to_input_queue(input, &uuid::Uuid::new_v4().to_string()).await {
                                                let message = format!("cannot resume research worker: {error}");
                                                provider.stop(message.clone());
                                                fatal_error = Some(message);
                                                break;
                                            }
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
            if let Some(error) = activity.failure() {
                provider.stop(error.clone());
                fatal_error.get_or_insert(error);
            }
            if provider.is_stopping() {
                let reason = provider.stop_reason().unwrap_or_else(|| "campaign stopped".into());
                if let Err(error) = activity.cancel_waits(&reason, &sender).await {
                    fatal_error.get_or_insert_with(|| error.to_string());
                    // The queue itself is unavailable. There is nothing left
                    // to deliver; shutdown will flush the parked drivers.
                    if activity.active() == 0 && provider.in_flight() == 0 {
                        break;
                    }
                }
            }
            if provider.is_stopping()
                && activity.active() == 0
                && provider.in_flight() == 0
                && (system.is_idle() || fatal_error.is_some())
            {
                // A failed native callback can leave an unanswered tool in a
                // live driver. Once admitted effects/streams drain, fatal
                // shutdown must not wait for that impossible reply forever.
                break;
            }
        }
        system.shutdown().await;
        for task in threads.tasks {
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
