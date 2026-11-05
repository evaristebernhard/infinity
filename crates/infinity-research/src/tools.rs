//! Research operations registered as real asynchronous Infinity tools.

use std::collections::HashSet;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};

use async_trait::async_trait;
use infinity_agent_core::message::{InputMessage, InputMessageContent};
use infinity_agent_core::system::local::ChannelSender;
use infinity_agent_core::tools::{Tool, ToolContext};
use infinity_agent_core::traits::InputSender;
use infinity_provider_protocol::message::{Text, ToolResult, ToolResultContent, UserContent};
use rap_protocol::{ProviderCallId, ToolCallId};
use serde::Deserialize;
use serde_json::{Value, json};

use crate::{BoxError, ResearchBackend};

/// Counts outstanding backend work so shutdown can drain admitted experiments.
#[derive(Default)]
pub struct ToolActivity {
    active: AtomicUsize,
    completed: AtomicU64,
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
    let guard = ActivityGuard::new(activity);
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
            tracing::error!(%error, tool_call_id = %id, "failed to deliver persisted tool result");
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
