//! Research operations registered as real asynchronous Infinity tools.

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use infinity_agent_core::message::{InputMessage, InputMessageContent};
use infinity_agent_core::system::local::ChannelSender;
use infinity_agent_core::tools::{Tool, ToolContext};
use infinity_agent_core::traits::InputSender;
use infinity_provider_protocol::message::{Text, ToolResult, ToolResultContent, UserContent};
use rap_protocol::{ProviderCallId, ThreadId, ToolCallId};
use serde::Deserialize;
use serde_json::{Value, json};

use crate::{BoxError, ResearchBackend};

/// Counts outstanding backend work so shutdown can drain admitted experiments.
#[derive(Default)]
pub struct ToolActivity {
    active: AtomicUsize,
    completed: AtomicU64,
    failure: Mutex<Option<String>>,
    park_hierarchy_waits: AtomicBool,
    pending_waits: Mutex<HashMap<String, PendingWait>>,
    worker_bindings: Mutex<HashMap<String, String>>,
}

/// A successful durable join relinquishes its lease immediately. Keep only its
/// native callback here, outside the active tool count, until the same logical
/// agent is admitted again. Its conversation remains in Infinity's store.
struct PendingWait {
    work_id: String,
    group_id: ThreadId,
    id: ToolCallId,
    call_id: Option<ProviderCallId>,
    response: Value,
}

impl PendingWait {
    async fn deliver(self, sender: &ChannelSender) -> Result<(), BoxError> {
        let message = tool_reply(self.group_id, &self.id, self.call_id, self.response);
        sender
            .send_to_input_queue(message, self.id.as_str())
            .await
            .map_err(|error| format!("cannot deliver logical agent join: {error}").into())
    }
}

fn tool_reply(
    group_id: ThreadId,
    id: &ToolCallId,
    call_id: Option<ProviderCallId>,
    response: Value,
) -> InputMessage {
    InputMessage {
        content: InputMessageContent::User(UserContent::ToolResult(ToolResult {
            id: id.as_str().to_owned(),
            call_id: call_id.map(ProviderCallId::into_inner),
            content: vec![ToolResultContent::Text(Text {
                text: response.to_string(),
            })],
        })),
        group_id,
        metadata: None,
        synthetic: None,
        display_as: None,
        subscription: false,
    }
}

impl ToolActivity {
    /// Number of operations awaiting a backend response or result delivery.
    pub fn active(&self) -> usize {
        self.active.load(Ordering::SeqCst)
    }

    /// Completed backend operations, including reported failures.
    pub fn completed(&self) -> u64 {
        self.completed.load(Ordering::SeqCst)
    }

    /// A failed host recovery must stop inference instead of retrying forever.
    pub fn failure(&self) -> Option<String> {
        self.failure
            .lock()
            .expect("bug: tool failure state poisoned")
            .clone()
    }

    fn fail(&self, reason: String) {
        self.failure
            .lock()
            .expect("bug: tool failure state poisoned")
            .get_or_insert(reason);
    }

    pub(crate) fn is_parked(&self, owner: &str) -> bool {
        self.pending_waits
            .lock()
            .expect("bug: pending join state poisoned")
            .contains_key(owner)
    }

    pub(crate) fn bind_worker(&self, owner: &str, work_id: &str) -> Result<(), BoxError> {
        let mut bindings = self
            .worker_bindings
            .lock()
            .expect("bug: native worker binding state poisoned");
        if let Some(previous) = bindings.get(owner) {
            if previous != work_id {
                return Err("native logical agent binding cannot change".into());
            }
        } else {
            bindings.insert(owner.to_owned(), work_id.to_owned());
        }
        Ok(())
    }

    /// In hierarchy mode only the supervisor admits a new lease. A model can
    /// reread its host-bound lease but cannot race a retry against a new child
    /// admission and exceed the launcher's smaller execution-slot limit.
    async fn read_bound_claim(
        &self,
        backend: &dyn ResearchBackend,
        envelope: &Value,
    ) -> Result<Value, BoxError> {
        let owner = envelope["owner"]
            .as_str()
            .ok_or("claim requires native owner")?;
        let work_id = self
            .worker_bindings
            .lock()
            .expect("bug: native worker binding state poisoned")
            .get(owner)
            .cloned()
            .ok_or("hierarchy worker is not bound by the host")?;
        let args = envelope["arguments"]
            .as_object()
            .ok_or("claim arguments must be an object")?;
        if args.keys().any(|key| key != "work_id") {
            return Err("unexpected claim arguments".into());
        }
        if args
            .get("work_id")
            .is_some_and(|requested| requested.as_str() != Some(work_id.as_str()))
        {
            return Err("Hierarchy worker is bound to another work item".into());
        }
        let mut read = envelope.clone();
        read["tool"] = json!("read_work");
        read["arguments"] = json!({"work_id": work_id});
        let work = backend.call("invoke_tool", read).await?;
        if work["state"] == "leased"
            && work["lease_owner"] == owner
            && work["lease_token"].as_str().is_some()
        {
            Ok(json!({"claimed": true, "work_id": work_id,
                "lease_token": work["lease_token"], "lease_expires": work["lease_expires"], "work": work}))
        } else {
            Ok(json!({"claimed": false, "work_id": work_id, "reason": "await_supervisor"}))
        }
    }

    fn park_wait(&self, wait: PendingWait) -> Result<(), BoxError> {
        let mut pending = self
            .pending_waits
            .lock()
            .expect("bug: pending join state poisoned");
        if let Some(previous) = pending.get(wait.group_id.as_str()) {
            if previous.id == wait.id && previous.work_id == wait.work_id {
                return Ok(());
            }
            return Err("a logical agent already has an unanswered join".into());
        }
        pending.insert(wait.group_id.as_str().to_owned(), wait);
        Ok(())
    }

    /// Deliver the original wait callback, with a newly admitted lease. No user
    /// message interrupts the parent and no child transcript is copied to it.
    pub(crate) async fn resume_wait(
        &self,
        owner: &str,
        claim: &Value,
        sender: &ChannelSender,
    ) -> Result<bool, BoxError> {
        let wait = self
            .pending_waits
            .lock()
            .expect("bug: pending join state poisoned")
            .remove(owner);
        let Some(mut wait) = wait else {
            return Ok(false);
        };
        if claim.get("work_id").and_then(Value::as_str) != Some(wait.work_id.as_str())
            || claim.get("claimed").and_then(Value::as_bool) != Some(true)
            || !wait.response["result"].is_object()
        {
            self.fail("join resume must carry a claim for the same logical agent".into());
            return Err("join resume must carry a claim for the same logical agent".into());
        }
        wait.response["result"]["resume_claim"] = claim.clone();
        if let Err(error) = wait.deliver(sender).await {
            self.fail(error.to_string());
            return Err(error);
        }
        Ok(true)
    }

    /// Native drivers with an unanswered tool are live even when they consume
    /// no inference slot. Settle those callbacks on stop so shutdown can finish.
    pub(crate) async fn cancel_waits(
        &self,
        reason: &str,
        sender: &ChannelSender,
    ) -> Result<(), BoxError> {
        let pending = std::mem::take(
            &mut *self
                .pending_waits
                .lock()
                .expect("bug: pending join state poisoned"),
        );
        let mut failure = None;
        for (_, mut wait) in pending {
            wait.response = json!({
                "ok": false, "stopped": true, "error": reason,
                "wait_result": wait.response["result"],
            });
            if let Err(error) = wait.deliver(sender).await {
                failure.get_or_insert(error);
            }
        }
        if let Some(error) = failure {
            self.fail(error.to_string());
            return Err(error);
        }
        Ok(())
    }
}

struct ActivityGuard(Arc<ToolActivity>);

impl ActivityGuard {
    fn new(activity: Arc<ToolActivity>) -> Self {
        activity.active.fetch_add(1, Ordering::SeqCst);
        Self(activity)
    }
}

impl Drop for ActivityGuard {
    fn drop(&mut self) {
        self.0.completed.fetch_add(1, Ordering::SeqCst);
        self.0.active.fetch_sub(1, Ordering::SeqCst);
    }
}

#[derive(Clone, Copy)]
enum Operation {
    Context,
    Experiment,
    Finding,
    Candidate,
}

impl Operation {
    fn operation(self) -> &'static str {
        match self {
            Self::Context => "context",
            Self::Experiment => "experiment",
            Self::Finding => "finding",
            Self::Candidate => "candidate",
        }
    }

    fn tool_name(self) -> &'static str {
        match self {
            Self::Context => "research_context",
            Self::Experiment => "run_experiment",
            Self::Finding => "record_finding",
            Self::Candidate => "read_candidate",
        }
    }

    fn description(self) -> &'static str {
        match self {
            Self::Context => {
                "Read the campaign goal, immutable evaluation contract, remaining budgets, candidate leaderboard, failures, shared findings, and suggested research family."
            }
            Self::Experiment => {
                "Submit complete candidate source files and a testable hypothesis. The backend reserves compute, runs the candidate in its configured executor, evaluates predictions independently, and returns evidence. This may take a long time; the runtime waits asynchronously."
            }
            Self::Finding => {
                "Persist a concise hypothesis, observation, failure, or reusable method. Cite actual experiment evidence_ids for measured claims; unsupported claims remain hypotheses."
            }
            Self::Candidate => {
                "Read the source files and provenance of a previous candidate before improving it. Pass its candidate_id as parent_id when submitting a descendant."
            }
        }
    }

    fn parameters(self) -> Value {
        match self {
            Self::Context => json!({
                "type": "object", "properties": {}, "additionalProperties": false,
            }),
            Self::Experiment => json!({
                "type": "object",
                "properties": {
                    "hypothesis": {"type": "string", "description": "What changes, why it may help, and which observation would refute it."},
                    "family": {"type": "string", "description": "Research family from the task/context, such as baseline, representation, augmentation, or postprocessing."},
                    "parent_id": {"type": ["string", "null"], "description": "Prior candidate being changed; null for a new branch."},
                    "files": {
                        "type": "object",
                        "description": "Complete relative-path source files, including main.py. This is a candidate snapshot, not a patch or a command.",
                        "additionalProperties": {"type": "string"},
                    },
                    "timeout_seconds": {"type": "integer", "minimum": 1},
                    "seed": {"type": "integer", "minimum": 0},
                },
                "required": ["hypothesis", "family", "files", "timeout_seconds", "seed"],
                "additionalProperties": false,
            }),
            Self::Finding => json!({
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["hypothesis", "observation", "failure", "method"]},
                    "text": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["kind", "text", "evidence_ids"],
                "additionalProperties": false,
            }),
            Self::Candidate => json!({
                "type": "object",
                "properties": {"candidate_id": {"type": "string"}},
                "required": ["candidate_id"],
                "additionalProperties": false,
            }),
        }
    }
}

struct ResearchTool {
    operation: Operation,
    backend: Arc<dyn ResearchBackend>,
    activity: Arc<ToolActivity>,
}

/// A tool contract supplied by the host backend, never by a worker or a skill.
#[derive(Clone, Deserialize)]
struct ToolDefinition {
    name: String,
    description: String,
    parameters: Value,
}

struct CapabilityTool {
    definition: ToolDefinition,
    backend: Arc<dyn ResearchBackend>,
    activity: Arc<ToolActivity>,
}

fn start_operation(
    operation: &'static str,
    arguments: Value,
    id: ToolCallId,
    call_id: Option<ProviderCallId>,
    context: &ToolContext<ChannelSender>,
    backend: Arc<dyn ResearchBackend>,
    activity: Arc<ToolActivity>,
) {
    let sender = context.message_sender.clone();
    let group_id = context.group_id.clone();
    let guard = ActivityGuard::new(activity.clone());
    tokio::spawn(async move {
        let _guard = guard;
        let parked_work = (activity.park_hierarchy_waits.load(Ordering::SeqCst)
            && operation == "invoke_tool"
            && arguments.get("tool").and_then(Value::as_str) == Some("wait_work"))
        .then(|| {
            arguments["arguments"]["work_id"]
                .as_str()
                .map(str::to_owned)
        })
        .flatten();
        let interrupted_request = (operation == "invoke_tool").then(|| {
            json!({
                "owner": arguments["owner"], "request_key": arguments["request_key"],
            })
        });
        let result = if activity.park_hierarchy_waits.load(Ordering::SeqCst)
            && operation == "invoke_tool"
            && arguments.get("tool").and_then(Value::as_str) == Some("claim_work")
        {
            activity
                .read_bound_claim(backend.as_ref(), &arguments)
                .await
        } else {
            backend.call(operation, arguments).await
        };
        let response = match result {
            Ok(result) => json!({"ok": true, "result": result}),
            Err(error) => {
                tracing::warn!(%group_id, operation, %error, "research operation failed");
                let mut response = json!({"ok": false, "error": error.to_string()});
                if let Some(mut request) = interrupted_request {
                    request["reason"] =
                        Value::String(error.to_string().chars().take(4000).collect());
                    // The RPC may have died after reserving an effect. A live
                    // model owner still gets heartbeats, so work-lease expiry
                    // alone cannot recover that orphan. Preserve a host-owned
                    // unknown receipt before permitting another tool attempt.
                    match backend.call("interrupt_tool", request).await {
                        Ok(recovery) => {
                            response["recovery"] = recovery;
                        }
                        Err(recovery_error) => {
                            let message =
                                format!("cannot recover interrupted tool: {recovery_error}");
                            tracing::error!(%group_id, %message);
                            activity.fail(message.clone());
                            response["recovery_error"] = Value::String(message);
                        }
                    }
                }
                response
            }
        };
        if let Some(work_id) = parked_work.filter(|_| response["ok"] == true) {
            if let Err(error) = activity.park_wait(PendingWait {
                work_id,
                group_id,
                id,
                call_id,
                response,
            }) {
                activity.fail(format!("cannot park logical agent join: {error}"));
            }
            // Dropping the activity guard releases the tool slot. In
            // particular, do not deliver a successful wait yet: that would
            // automatically call the model while its dependencies are pending.
            return;
        }
        let message = tool_reply(group_id, &id, call_id, response);
        if let Err(error) = sender.send_to_input_queue(message, id.as_str()).await {
            tracing::error!(%error, tool_call_id = %id, "failed to deliver persisted tool result");
            activity.fail(format!("cannot deliver persisted tool result: {error}"));
        }
    });
}

#[async_trait]
impl Tool<ChannelSender> for CapabilityTool {
    fn name(&self) -> &str {
        &self.definition.name
    }
    fn description(&self) -> &str {
        &self.definition.description
    }
    fn parameters(&self) -> Value {
        self.definition.parameters.clone()
    }

    async fn execute(
        &self,
        arguments: Value,
        id: ToolCallId,
        call_id: Option<ProviderCallId>,
        context: &ToolContext<ChannelSender>,
    ) -> Result<(), BoxError> {
        if !arguments.is_object() {
            return Err("capability tool arguments must be a JSON object".into());
        }
        // Keep transport authority outside the model's arguments. Even a
        // forged `owner` or `request_key` inside arguments cannot replace it.
        let envelope = json!({
            "tool": self.definition.name,
            "arguments": arguments,
            "owner": context.group_id,
            "request_key": format!("{}:{}", context.group_id, id),
        });
        start_operation(
            "invoke_tool",
            envelope,
            id,
            call_id,
            context,
            self.backend.clone(),
            self.activity.clone(),
        );
        Ok(())
    }
}

/// Use a versioned host catalog for generic campaigns; schema-1 campaigns keep
/// their existing tools and wire protocol unchanged.
pub fn campaign_tools(
    context: &Value,
    backend: Arc<dyn ResearchBackend>,
    activity: Arc<ToolActivity>,
) -> Result<Vec<Box<dyn Tool<ChannelSender>>>, BoxError> {
    if context.get("run_kind").and_then(Value::as_str) != Some("generic") {
        return Ok(research_tools(backend, activity));
    }
    activity
        .park_hierarchy_waits
        .store(context.get("delegation").is_some(), Ordering::SeqCst);
    let definitions: Vec<ToolDefinition> = serde_json::from_value(
        context
            .get("tool_catalog")
            .ok_or("generic campaign has no tool_catalog")?
            .clone(),
    )?;
    if definitions.is_empty() || definitions.len() > 64 {
        return Err("host tool catalog must contain 1..64 tools".into());
    }
    let mut seen = HashSet::new();
    let mut result: Vec<Box<dyn Tool<ChannelSender>>> = Vec::new();
    for definition in definitions {
        if definition.name.is_empty()
            || definition.name.len() > 64
            || !definition
                .name
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
            || !seen.insert(definition.name.clone())
            || definition.description.is_empty()
            || definition.description.len() > 4096
            || definition.parameters.get("type").and_then(Value::as_str) != Some("object")
            || serde_json::to_vec(&definition.parameters)?.len() > 65_536
        {
            return Err("invalid or duplicate host tool definition".into());
        }
        result.push(Box::new(CapabilityTool {
            definition,
            backend: backend.clone(),
            activity: activity.clone(),
        }));
    }
    Ok(result)
}

#[async_trait]
impl Tool<ChannelSender> for ResearchTool {
    fn name(&self) -> &str {
        self.operation.tool_name()
    }

    fn description(&self) -> &str {
        self.operation.description()
    }

    fn parameters(&self) -> Value {
        self.operation.parameters()
    }

    async fn execute(
        &self,
        mut arguments: Value,
        id: ToolCallId,
        call_id: Option<ProviderCallId>,
        context: &ToolContext<ChannelSender>,
    ) -> Result<(), BoxError> {
        let object = arguments
            .as_object_mut()
            .ok_or("research tool arguments must be a JSON object")?;
        if matches!(self.operation, Operation::Experiment | Operation::Finding) {
            // Transport identity is authoritative. A model cannot choose an
            // existing transaction's key, nor can redelivery start it twice.
            object.insert(
                "idempotency_key".into(),
                Value::String(format!("{}:{}", context.group_id, id)),
            );
        }
        let backend = self.backend.clone();
        let operation = self.operation.operation();
        let sender = context.message_sender.clone();
        let group_id = context.group_id.clone();
        let guard = ActivityGuard::new(self.activity.clone());
        tokio::spawn(async move {
            let _guard = guard;
            let response = match backend.call(operation, arguments).await {
                Ok(result) => json!({"ok": true, "result": result}),
                Err(error) => {
                    tracing::warn!(%group_id, operation, %error, "research operation failed");
                    json!({"ok": false, "error": error.to_string()})
                }
            };
            let message = InputMessage {
                content: InputMessageContent::User(UserContent::ToolResult(ToolResult {
                    id: id.as_str().to_owned(),
                    call_id: call_id.map(ProviderCallId::into_inner),
                    content: vec![ToolResultContent::Text(Text {
                        text: response.to_string(),
                    })],
                })),
                group_id,
                metadata: None,
                synthetic: None,
                display_as: None,
                subscription: false,
            };
            if let Err(error) = sender.send_to_input_queue(message, id.as_str()).await {
                // The backend already committed the result; a restarted worker
                // can recover it from context even if this process is exiting.
                tracing::error!(%error, tool_call_id = %id, "failed to deliver research tool result");
            }
        });
        Ok(())
    }
}

/// Build the worker's complete toolset. No arbitrary shell or spawning tool is
/// exposed; candidate source executes only through the campaign's executor.
pub fn research_tools(
    backend: Arc<dyn ResearchBackend>,
    activity: Arc<ToolActivity>,
) -> Vec<Box<dyn Tool<ChannelSender>>> {
    [
        Operation::Context,
        Operation::Experiment,
        Operation::Finding,
        Operation::Candidate,
    ]
    .into_iter()
    .map(|operation| {
        Box::new(ResearchTool {
            operation,
            backend: backend.clone(),
            activity: activity.clone(),
        }) as Box<dyn Tool<ChannelSender>>
    })
    .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use infinity_agent_core::stores::{InMemoryConversationStore, InMemoryStateStore};
    use infinity_agent_core::system::{AgentSystemBuilder, ModelSource, ResolvedModel};

    struct NoModel;

    #[async_trait(?Send)]
    impl ModelSource for NoModel {
        async fn resolve(&self, _thread: &ThreadId<str>) -> Result<ResolvedModel, BoxError> {
            Err("this callback-delivery fixture must never infer".into())
        }
    }

    fn pending(owner: &str) -> PendingWait {
        PendingWait {
            work_id: format!("work-{owner}"),
            group_id: owner.to_owned().into(),
            id: format!("wait-{owner}").into(),
            call_id: None,
            response: json!({"ok": true, "result": {"state": "pending"}}),
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn a_closed_native_queue_marks_join_delivery_fatal_and_clears_every_callback() {
        tokio::task::LocalSet::new()
            .run_until(async {
                let system = AgentSystemBuilder::new_local(
                    InMemoryConversationStore::new(),
                    InMemoryStateStore::new(),
                    NoModel,
                )
                .without_builtin_tools()
                .start();
                let sender = system.sender();
                system.shutdown().await;

                let activity = ToolActivity::default();
                activity
                    .park_wait(pending("parent-one"))
                    .expect("first pending join");
                activity
                    .park_wait(pending("parent-two"))
                    .expect("second pending join");
                let error = activity
                    .cancel_waits("fixture shutdown", &sender)
                    .await
                    .expect_err("all deliveries use the actually closed Infinity input queue");
                assert!(error.to_string().contains("input channel closed"));
                assert!(activity.failure().is_some());
                assert!(!activity.is_parked("parent-one"));
                assert!(!activity.is_parked("parent-two"));
                assert_eq!(activity.active(), 0, "parked joins are not executing tools");

                let resume = ToolActivity::default();
                resume
                    .park_wait(pending("resuming-parent"))
                    .expect("resuming join");
                resume
                    .resume_wait(
                        "resuming-parent",
                        &json!({"claimed": true,
                "work_id": "work-resuming-parent", "lease_token": "new-host-token"}),
                        &sender,
                    )
                    .await
                    .expect_err("lost resume callback is fatal too");
                assert!(resume.failure().is_some());
                assert!(!resume.is_parked("resuming-parent"));
            })
            .await;
    }
}
