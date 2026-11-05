//! A bounded, shell-free bridge to the durable Python campaign backend.

use std::ffi::OsString;
use std::fs::{File, OpenOptions, TryLockError};
use std::path::PathBuf;
use std::process::Stdio;
use std::time::Duration;

use async_trait::async_trait;
use serde::Deserialize;
use serde_json::{Value, json};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

use crate::BoxError;

const MAX_REQUEST_BYTES: usize = 1024 * 1024;
const MAX_RESPONSE_BYTES: u64 = 8 * 1024 * 1024;

/// Operations needed by a research worker and its model admission controller.
#[async_trait]
pub trait ResearchBackend: Send + Sync {
    /// Execute an operation and return the unwrapped `result` of its RPC reply.
    async fn call(&self, operation: &str, arguments: Value) -> Result<Value, BoxError>;
}

/// Runs `python -m infinity_research.rpc --campaign PATH OPERATION`.
///
/// The module's working directory is explicit and arguments never pass through
/// a shell. A new process per operation lets the SQLite backend serialize only
/// its short state transitions while long experiments run independently.
#[derive(Clone, Debug)]
pub struct PythonBackend {
    python: OsString,
    research_root: PathBuf,
    campaign: PathBuf,
    control_timeout: Duration,
    experiment_grace: Duration,
    recovery_timeout: Duration,
}

#[derive(Deserialize)]
struct Reply {
    ok: bool,
    result: Option<Value>,
    error: Option<String>,
}

/// An operating-system file lock, automatically released on exit or a crash.
/// The lock file may remain on disk; its existence is not the lock state.
pub struct CampaignLock {
    _file: File,
}

impl PythonBackend {
    /// Enforce one native launcher per campaign on this machine.
    pub fn lock_campaign(&self) -> Result<CampaignLock, BoxError> {
        let path = self.campaign.join(".launcher.lock");
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(&path)?;
        match file.try_lock() {
            Ok(()) => Ok(CampaignLock { _file: file }),
            Err(TryLockError::WouldBlock) => Err(format!(
                "another research launcher holds the campaign lock at {}",
                path.display(),
            )
            .into()),
            Err(TryLockError::Error(error)) => {
                Err(format!("cannot lock campaign {}: {error}", self.campaign.display(),).into())
            }
        }
    }

    /// Resolve the module and campaign directories before any process starts.
    pub fn new(
        python: impl Into<OsString>,
        research_root: PathBuf,
        campaign: PathBuf,
    ) -> Result<Self, BoxError> {
        let python = python.into();
        let python_path = PathBuf::from(&python);
        // Resolve an explicit relative interpreter before changing the child's
        // working directory. Bare names such as python3 still use PATH.
        let python = if python_path.is_relative() && python_path.components().count() > 1 {
            // Do not canonicalize: venv interpreters are often symlinks whose
            // original path is needed to discover pyvenv.cfg and dependencies.
            std::env::current_dir()?.join(python_path).into_os_string()
        } else {
            python
        };
        let research_root = research_root.canonicalize().map_err(|error| {
            format!(
                "cannot resolve research root {}: {error}",
                research_root.display()
            )
        })?;
        if !research_root.join("infinity_research/rpc.py").is_file() {
            return Err(format!(
                "{} does not contain infinity_research/rpc.py; set --research-root to the repository's research directory",
                research_root.display()
            )
            .into());
        }
        let campaign = campaign.canonicalize().map_err(|error| {
            format!(
                "cannot resolve initialized campaign {}: {error}",
                campaign.display()
            )
        })?;
        if !campaign.is_dir() {
            return Err("campaign must be an initialized directory".into());
        }
        Ok(Self {
            python,
            research_root,
            campaign,
            control_timeout: Duration::from_secs(30),
            experiment_grace: Duration::from_secs(120),
            recovery_timeout: Duration::from_secs(86_400),
        })
    }

    /// Override process deadlines, chiefly useful for local tests and slow disks.
    pub fn with_timeouts(mut self, control: Duration, experiment_grace: Duration) -> Self {
        self.control_timeout = control;
        self.experiment_grace = experiment_grace;
        self
    }

    /// Bound restart recovery independently of a single new experiment.
    pub fn with_recovery_timeout(mut self, timeout: Duration) -> Self {
        self.recovery_timeout = timeout;
        self
    }

    async fn timeout_for(&self, operation: &str, arguments: &Value) -> Result<Duration, BoxError> {
        match operation {
            "recover" => Ok(self.recovery_timeout),
            "experiment" | "finalize" => {
                if operation == "experiment" {
                    arguments
                        .get("timeout_seconds")
                        .and_then(Value::as_u64)
                        .filter(|seconds| *seconds > 0)
                        .ok_or("experiment timeout_seconds must be a positive integer")?;
                }
                // A research worker may wait behind long GPU jobs. The trial
                // budget starts when execution is claimed, so it cannot also
                // bound the RPC's queue wait. The backend owns that deadline;
                // this outer guard bounds a stuck backend by campaign wall time.
                let context = self.call("context", json!({})).await?;
                let budget = context
                    .get("budget")
                    .ok_or("campaign context has no budget")?;
                let timeout = if operation == "finalize" {
                    let seconds = budget
                        .get("final_trial_seconds")
                        .and_then(Value::as_u64)
                        .filter(|seconds| *seconds > 0)
                        .ok_or(
                            "campaign context must include a positive budget.final_trial_seconds",
                        )?;
                    Duration::from_secs(seconds)
                } else {
                    let seconds = context.get("wall_seconds_remaining")
                        .or_else(|| budget.get("wall_seconds_remaining"))
                        .or_else(|| budget.get("max_wall_seconds"))
                        .and_then(Value::as_f64)
                        .ok_or("campaign context must include remaining wall seconds or budget.max_wall_seconds")?;
                    Duration::try_from_secs_f64(seconds).map_err(
                        |_| "campaign remaining wall time must be finite and nonnegative",
                    )?
                };
                timeout
                    .checked_add(self.experiment_grace)
                    .ok_or_else(|| "campaign wall timeout is too large".into())
            }
            "context" | "finding" | "candidate" | "reserve_model_call" | "record_usage" => {
                Ok(self.control_timeout)
            }
            _ => Err(format!("unsupported research operation: {operation}").into()),
        }
    }
}

#[async_trait]
impl ResearchBackend for PythonBackend {
    async fn call(&self, operation: &str, arguments: Value) -> Result<Value, BoxError> {
        let timeout = self.timeout_for(operation, &arguments).await?;
        let mut request = serde_json::to_vec(&arguments)?;
        if request.len() >= MAX_REQUEST_BYTES {
            return Err("research RPC request exceeds 1 MiB including its newline".into());
        }
        request.push(b'\n');
        let mut child = tokio::process::Command::new(&self.python)
            .arg("-m")
            .arg("infinity_research.rpc")
            .arg("--campaign")
            .arg(&self.campaign)
            .arg(operation)
            .current_dir(&self.research_root)
            .env("PYTHONUNBUFFERED", "1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .kill_on_drop(true)
            .spawn()
            .map_err(|error| format!("cannot start research backend: {error}"))?;
        let mut stdin = child.stdin.take().ok_or("backend stdin was not piped")?;
        let stdout = child.stdout.take().ok_or("backend stdout was not piped")?;

        // Bound both writes and reads. Keeping a streaming read instead of
        // wait_with_output prevents an accidental backend log flood from
        // consuming unlimited launcher memory.
        let operation_future = async {
            stdin.write_all(&request).await?;
            stdin.shutdown().await?;
            drop(stdin);
            let mut bytes = Vec::new();
            stdout
                .take(MAX_RESPONSE_BYTES + 1)
                .read_to_end(&mut bytes)
                .await?;
            if bytes.len() as u64 > MAX_RESPONSE_BYTES {
                return Err::<Value, BoxError>("research RPC response exceeds 8 MiB".into());
            }
            let status = child.wait().await?;
            let reply: Reply = serde_json::from_slice(&bytes).map_err(|error| {
                format!("invalid research RPC JSON for {operation}: {error} (exit {status})")
            })?;
            if !status.success() || !reply.ok {
                return Err(reply
                    .error
                    .unwrap_or_else(|| format!("research RPC {operation} failed with {status}"))
                    .into());
            }
            reply
                .result
                .ok_or_else(|| "successful research RPC reply has no result".into())
        };
        tokio::time::timeout(timeout, operation_future)
            .await
            .map_err(|_| {
                format!(
                    "research RPC {operation} exceeded {} seconds",
                    timeout.as_secs()
                )
            })?
    }
}
