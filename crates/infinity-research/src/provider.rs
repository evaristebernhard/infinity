//! Model admission on top of Infinity's provider abstraction.

use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use futures_util::StreamExt;
use infinity_provider_protocol::{
    CompletionError, CompletionRequest, ErrorClass, ModelEntry, ModelProvider, ModelStream,
    StreamChunk,
};
use serde_json::{Value, json};
use tokio::sync::Semaphore;

use crate::{BoxError, ResearchBackend};

/// Wire parameter supported by the selected Chat Completions endpoint.
#[derive(Clone, Copy, Debug, clap::ValueEnum)]
pub enum OutputTokenField {
    MaxTokens,
    MaxCompletionTokens,
}

impl OutputTokenField {
    fn as_str(self) -> &'static str {
        match self {
            Self::MaxTokens => "max_tokens",
            Self::MaxCompletionTokens => "max_completion_tokens",
        }
    }
}

/// Apply endpoint-specific wire settings before the existing rig adapter.
///
/// rig 0.31's generic OpenAI Chat Completions converter omits its core
/// `max_tokens` field. Set the actual HTTP parameter through additional_params
/// so the campaign's admitted cap survives that conversion.
pub struct ChatCompletionsProvider {
    inner: Arc<dyn ModelProvider>,
    output_field: OutputTokenField,
    disable_thinking: bool,
}

impl ChatCompletionsProvider {
    pub fn new(
        inner: Arc<dyn ModelProvider>,
        output_field: OutputTokenField,
        disable_thinking: bool,
    ) -> Self {
        Self {
            inner,
            output_field,
            disable_thinking,
        }
    }
}

#[async_trait]
impl ModelProvider for ChatCompletionsProvider {
    async fn list_models(&self) -> Result<Vec<ModelEntry>, BoxError> {
        self.inner.list_models().await
    }

    async fn invoke_model(
        &self,
        model_id: &str,
        mut request: CompletionRequest,
    ) -> Result<ModelStream, CompletionError> {
        let limit = request
            .max_tokens
            .filter(|limit| *limit > 0)
            .ok_or_else(|| {
                CompletionError::provider(
                    ErrorClass::Fatal,
                    "Chat Completions requires an admitted output cap",
                )
            })?;
        let params = request.additional_params.get_or_insert_with(|| json!({}));
        let params = params.as_object_mut().ok_or_else(|| {
            CompletionError::provider(
                ErrorClass::Fatal,
                "provider additional_params must be an object",
            )
        })?;
        params.remove("max_tokens");
        params.remove("max_completion_tokens");
        params.insert(self.output_field.as_str().to_owned(), json!(limit));
        if self.disable_thinking {
            // The generic rig OpenAI adapter does not round-trip DeepSeek's
            // reasoning_content. Use its documented non-thinking tool mode.
            params.insert("thinking".to_owned(), json!({"type": "disabled"}));
        }
        self.inner.invoke_model(model_id, request).await
    }
}

/// Reserves each model call in the durable backend before invoking the provider.
///
/// The call limit survives launcher restarts. Per-request output limits are set
/// on the actual provider request. Reported usage is accounted separately; it
/// is not presented as a strict, pre-tokenized global spending limit.
pub struct BudgetedProvider {
    inner: Arc<dyn ModelProvider>,
    backend: Arc<dyn ResearchBackend>,
    max_output_tokens: u64,
    response_timeout: Duration,
    call_timeout: Duration,
    permits: Arc<Semaphore>,
    stopping: Arc<AtomicBool>,
    reason: Arc<Mutex<Option<String>>>,
    failure: Arc<Mutex<Option<String>>>,
    calls: AtomicU64,
    in_flight: Arc<AtomicUsize>,
}

impl BudgetedProvider {
    /// Wrap a provider with durable call admission and bounded concurrency.
    pub fn new(
        inner: Arc<dyn ModelProvider>,
        backend: Arc<dyn ResearchBackend>,
        max_output_tokens: u64,
        max_concurrency: usize,
        response_timeout: Duration,
    ) -> Result<Self, BoxError> {
        if max_output_tokens == 0
            || max_concurrency == 0
            || response_timeout.is_zero()
            || tokio::time::Instant::now()
                .checked_add(response_timeout)
                .is_none()
        {
            return Err(
                "model output limit, concurrency, and response timeout must be positive".into(),
            );
        }
        Ok(Self {
            inner,
            backend,
            max_output_tokens,
            response_timeout,
            call_timeout: Duration::from_secs(900),
            permits: Arc::new(Semaphore::new(max_concurrency)),
            stopping: Arc::new(AtomicBool::new(false)),
            reason: Arc::new(Mutex::new(None)),
            failure: Arc::new(Mutex::new(None)),
            calls: AtomicU64::new(0),
            in_flight: Arc::new(AtomicUsize::new(0)),
        })
    }

    /// Bound a whole admitted call, even while its stream keeps producing data.
    pub fn with_call_timeout(mut self, timeout: Duration) -> Result<Self, BoxError> {
        if timeout.is_zero() || tokio::time::Instant::now().checked_add(timeout).is_none() {
            return Err("total model-call timeout must be positive and representable".into());
        }
        self.call_timeout = timeout;
        Ok(self)
    }

    /// Close admission while allowing already admitted streams to finish.
    pub fn stop(&self, reason: impl Into<String>) {
        mark_stopped(&self.stopping, &self.reason, reason.into());
    }

    /// Whether model admission has been closed.
    pub fn is_stopping(&self) -> bool {
        self.stopping.load(Ordering::SeqCst)
    }

    /// The first reason for closing admission.
    pub fn stop_reason(&self) -> Option<String> {
        self.reason
            .lock()
            .expect("bug: model stop state poisoned")
            .clone()
    }

    /// A fatal provider or persistence error, distinct from a normal budget stop.
    pub fn failure(&self) -> Option<String> {
        self.failure
            .lock()
            .expect("bug: model failure state poisoned")
            .clone()
    }

    fn fail(&self, message: String) {
        mark_failed(&self.stopping, &self.reason, &self.failure, message);
    }

    /// Admitted model calls in this launcher process (not the campaign total).
    pub fn calls_this_process(&self) -> u64 {
        self.calls.load(Ordering::SeqCst)
    }

    /// Live provider streams and requests.
    pub fn in_flight(&self) -> usize {
        self.in_flight.load(Ordering::SeqCst)
    }
}

fn mark_stopped(stopping: &AtomicBool, state: &Mutex<Option<String>>, reason: String) {
    let mut stored = state.lock().expect("bug: model stop state poisoned");
    if stored.is_none() {
        *stored = Some(reason);
    }
    stopping.store(true, Ordering::SeqCst);
}

fn mark_failed(
    stopping: &AtomicBool,
    reason: &Mutex<Option<String>>,
    failure: &Mutex<Option<String>>,
    message: String,
) {
    failure
        .lock()
        .expect("bug: model failure state poisoned")
        .get_or_insert_with(|| message.clone());
    mark_stopped(stopping, reason, message);
}

struct InFlight(Arc<AtomicUsize>);

impl InFlight {
    fn new(counter: Arc<AtomicUsize>) -> Self {
        counter.fetch_add(1, Ordering::SeqCst);
        Self(counter)
    }
}

impl Drop for InFlight {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::SeqCst);
    }
}

#[async_trait]
impl ModelProvider for BudgetedProvider {
    async fn list_models(&self) -> Result<Vec<ModelEntry>, BoxError> {
        tokio::time::timeout(self.response_timeout, self.inner.list_models())
            .await
            .map_err(|_| "model catalog request timed out")?
    }

    async fn invoke_model(
        &self,
        model_id: &str,
        mut request: CompletionRequest,
    ) -> Result<ModelStream, CompletionError> {
        let fatal = |message: String| CompletionError::provider(ErrorClass::Fatal, message);
        let permit = self
            .permits
            .clone()
            .acquire_owned()
            .await
            .map_err(|error| fatal(format!("model admission closed: {error}")))?;
        if self.is_stopping() {
            return Err(fatal(
                self.stop_reason()
                    .unwrap_or_else(|| "research stopping".into()),
            ));
        }
        let in_flight = InFlight::new(self.in_flight.clone());
        let admitted_at = tokio::time::Instant::now();
        let request_id = uuid::Uuid::new_v4().to_string();
        let admission = self
            .backend
            .call("reserve_model_call", json!({"request_id": request_id}))
            .await
            .map_err(|error| {
                self.fail(format!("cannot persist model admission: {error}"));
                fatal(format!("cannot persist model admission: {error}"))
            })?;
        let allowed = admission
            .get("allowed")
            .and_then(Value::as_bool)
            .ok_or_else(|| {
                let message = "model admission did not include a boolean allowed field".to_owned();
                self.fail(message.clone());
                fatal(message)
            })?;
        if !allowed {
            let reason = admission
                .get("reason")
                .and_then(Value::as_str)
                .unwrap_or("campaign refused model admission")
                .to_owned();
            self.stop(reason.clone());
            return Err(fatal(reason));
        }
        let admitted_limit = admission
            .get("max_output_tokens")
            .and_then(Value::as_u64)
            .filter(|limit| *limit > 0)
            .ok_or_else(|| {
                self.fail("model admission did not include a positive max_output_tokens".into());
                fatal("model admission did not include a positive max_output_tokens".into())
            })?;
        let call_limit = if let Some(remaining) = admission.get("search_wall_seconds_remaining") {
            let seconds = remaining.as_f64().ok_or_else(|| {
                let message =
                    "model admission search_wall_seconds_remaining must be numeric".to_owned();
                self.fail(message.clone());
                fatal(message)
            })?;
            let remaining = Duration::try_from_secs_f64(seconds).map_err(|_| {
                let message =
                    "model admission remaining wall time must be finite and nonnegative".to_owned();
                self.fail(message.clone());
                fatal(message)
            })?;
            self.call_timeout.min(remaining)
        } else {
            self.call_timeout
        };
        let deadline = admitted_at + call_limit;
        if tokio::time::Instant::now() >= deadline {
            self.stop("wall_time_budget_exhausted");
            return Err(fatal("wall_time_budget_exhausted".into()));
        }
        request.max_tokens = Some(
            request
                .max_tokens
                .unwrap_or(self.max_output_tokens)
                .min(self.max_output_tokens)
                .min(admitted_limit),
        );
        self.calls.fetch_add(1, Ordering::SeqCst);
        let invocation = tokio::time::timeout_at(
            deadline.min(tokio::time::Instant::now() + self.response_timeout),
            self.inner.invoke_model(model_id, request),
        )
        .await
        .map_err(|_| {
            let message = if tokio::time::Instant::now() >= deadline {
                "total model-call deadline exceeded"
            } else {
                "model request timed out"
            };
            CompletionError::provider(ErrorClass::Transient, message)
        })?;
        let mut stream = match invocation {
            Ok(stream) => stream,
            Err(error) => {
                if error.class() == ErrorClass::Fatal {
                    self.fail(format!("model provider failed: {error}"));
                }
                return Err(error);
            }
        };
        let backend = self.backend.clone();
        let timeout = self.response_timeout;
        let stopping = self.stopping.clone();
        let reason = self.reason.clone();
        let failure = self.failure.clone();
        Ok(Box::pin(async_stream::stream! {
            // Hold concurrency admission until the stream is consumed or dropped.
            let _permit = permit;
            let _in_flight = in_flight;
            loop {
                if tokio::time::Instant::now() >= deadline {
                    yield Err(CompletionError::provider(ErrorClass::Transient, "total model-call deadline exceeded"));
                    break;
                }
                let next_deadline = deadline.min(tokio::time::Instant::now() + timeout);
                let Ok(next) = tokio::time::timeout_at(next_deadline, stream.next()).await else {
                    let message = if tokio::time::Instant::now() >= deadline {
                        "total model-call deadline exceeded"
                    } else {
                        "model stream became inactive"
                    };
                    yield Err(CompletionError::provider(ErrorClass::Transient, message));
                    break;
                };
                let Some(item) = next else { break };
                if let Err(error) = &item
                    && error.class() == ErrorClass::Fatal {
                    mark_failed(&stopping, &reason, &failure, format!("model stream failed: {error}"));
                }
                if let Ok(StreamChunk::Final(final_response)) = &item {
                    if let Some(usage) = final_response.usage
                        && let Err(error) = backend.call("record_usage", json!({
                            "request_id": request_id,
                            "input_tokens": usage.input_tokens,
                            "output_tokens": usage.output_tokens,
                        })).await {
                        let message = format!("cannot persist model usage: {error}");
                        mark_failed(&stopping, &reason, &failure, message.clone());
                        yield Err(CompletionError::provider(ErrorClass::Fatal, message));
                        break;
                    }
                    yield item;
                    break;
                }
                yield item;
            }
        }))
    }
}
