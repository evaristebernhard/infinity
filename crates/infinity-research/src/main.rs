//! Command-line entry point for a local autonomous campaign.

use std::ffi::OsString;
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;

use clap::{Parser, ValueEnum};
use infinity_provider_protocol::{ModelEntry, ModelProvider};
use infinity_provider_rig::RigCompletionModel;
use infinity_research::{
    BoxError, ChatCompletionsProvider, OutputTokenField, PythonBackend, RunOptions,
    finalize_if_eligible, run_campaign,
};
use rig::client::CompletionClient;
use tokio::io::AsyncBufReadExt;

#[derive(Clone, Copy, Debug, ValueEnum)]
enum ProviderKind {
    OpenaiCompatible,
    Remote,
}

#[derive(Parser, Debug)]
#[command(about = "Run bounded autonomous research workers on native Infinity threads")]
struct Args {
    /// Initialized campaign directory, containing the durable SQLite ledger.
    #[arg(long)]
    campaign: PathBuf,
    /// Repository research directory containing infinity_research/rpc.py.
    #[arg(long, default_value = "research")]
    research_root: PathBuf,
    /// Python interpreter with the selected domain's dependencies installed.
    #[arg(long, default_value = "python3")]
    python: OsString,
    /// Number of worker threads, limited by the campaign's max_workers.
    #[arg(long, default_value_t = 4, value_parser = clap::value_parser!(u16).range(1..))]
    workers: u16,
    /// Actual provider model identifier; never inferred from a display name.
    #[arg(long)]
    model: String,
    #[arg(long, value_enum, default_value_t = ProviderKind::OpenaiCompatible)]
    provider: ProviderKind,
    /// Chat Completions API base URL. Falls back to OPENAI_BASE_URL, then OpenAI.
    #[arg(long)]
    api_base: Option<String>,
    /// Name of the environment variable containing the provider key.
    #[arg(long, default_value = "OPENAI_API_KEY")]
    api_key_env: String,
    /// Chat Completions wire cap; newer OpenAI models may need max-completion-tokens.
    #[arg(long, value_enum, default_value_t = OutputTokenField::MaxTokens)]
    output_token_field: OutputTokenField,
    /// Send thinking.type=disabled, needed for DeepSeek through this generic rig adapter.
    #[arg(long)]
    disable_thinking: bool,
    /// Model context size used for Infinity's automatic compaction; 0 disables it.
    #[arg(long, default_value_t = 65_536)]
    context_window: usize,
    /// Per-call output cap, further limited by each durable backend admission.
    #[arg(long, default_value_t = 8192, value_parser = clap::value_parser!(u64).range(1..))]
    max_output_tokens: u64,
    /// JSON argv array for an existing Infinity model-provider executable.
    #[arg(long, conflicts_with = "provider_socket")]
    provider_command_json: Option<String>,
    /// Existing Infinity model-provider Unix socket (remote mode).
    #[arg(long, conflicts_with = "provider_command_json")]
    provider_socket: Option<PathBuf>,
    /// Model request/stream inactivity timeout, not a total experiment deadline.
    #[arg(long, default_value_t = 180, value_parser = clap::value_parser!(u64).range(1..))]
    response_timeout_seconds: u64,
    /// Total model-call deadline, including an active stream; clamped by remaining search time.
    #[arg(long, default_value_t = 900, value_parser = clap::value_parser!(u64).range(1..))]
    model_call_timeout_seconds: u64,
    #[arg(long, default_value_t = 2, value_parser = clap::value_parser!(u64).range(1..))]
    poll_interval_seconds: u64,
    /// Maximum time to drain previous experiments while recovering a campaign.
    #[arg(long, default_value_t = 86_400, value_parser = clap::value_parser!(u64).range(1..))]
    recovery_timeout_seconds: u64,
    /// Leave one-time holdout evaluation for a later explicit host command.
    #[arg(long)]
    no_finalize: bool,
}

fn openai_compatible_provider(args: &Args) -> Result<Arc<dyn ModelProvider>, BoxError> {
    if args.provider_command_json.is_some() || args.provider_socket.is_some() {
        return Err("provider-command-json and provider-socket require --provider remote".into());
    }
    let key = std::env::var(&args.api_key_env).map_err(|_| {
        format!(
            "provider key environment variable {} is not set",
            args.api_key_env
        )
    })?;
    if key.trim().is_empty() {
        return Err(format!(
            "provider key environment variable {} is empty",
            args.api_key_env
        )
        .into());
    }
    let base = args
        .api_base
        .clone()
        .or_else(|| std::env::var("OPENAI_BASE_URL").ok())
        .unwrap_or_else(|| "https://api.openai.com/v1".to_owned());
    // In rig 0.31 the OpenAI default uses Responses. Explicitly opt into
    // Chat Completions for DeepSeek and other compatible providers.
    let client: rig::providers::openai::CompletionsClient =
        rig::providers::openai::Client::builder()
            .api_key(&key)
            .base_url(base.trim_end_matches('/'))
            .build()?
            .completions_api();
    let model = client.completion_model(&args.model);
    let inner = Arc::new(RigCompletionModel::new(model).into_provider(ModelEntry {
        model_id: args.model.clone(),
        display_name: args.model.clone(),
        context_window: args.context_window,
        max_output_tokens: Some(args.max_output_tokens),
        supports_image_input: false,
    }));
    Ok(Arc::new(ChatCompletionsProvider::new(
        inner,
        args.output_token_field,
        args.disable_thinking,
    )))
}

#[cfg(unix)]
async fn remote_provider(
    args: &Args,
) -> Result<(Arc<dyn ModelProvider>, Option<tokio::process::Child>), BoxError> {
    use infinity_provider_protocol::remote::RemoteModelProvider;
    if let Some(socket) = &args.provider_socket {
        return Ok((Arc::new(RemoteModelProvider::new(socket)), None));
    }
    let raw = args
        .provider_command_json
        .as_deref()
        .ok_or("remote mode requires --provider-command-json or --provider-socket")?;
    let command: Vec<String> = serde_json::from_str(raw)
        .map_err(|error| format!("provider-command-json must be a JSON argv array: {error}"))?;
    let executable = command
        .first()
        .filter(|name| !name.is_empty())
        .ok_or("provider command must contain a nonempty executable")?;
    let mut child = tokio::process::Command::new(executable)
        .args(&command[1..])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .kill_on_drop(true)
        .spawn()?;
    let stdout = child.stdout.take().ok_or("provider stdout was not piped")?;
    let mut lines = tokio::io::BufReader::new(stdout).lines();
    let socket = tokio::time::timeout(Duration::from_secs(30), lines.next_line())
        .await
        .map_err(|_| "model provider did not print a socket within 30 seconds")??
        .filter(|line| !line.trim().is_empty())
        .ok_or("model provider exited without printing its Unix socket")?;
    tokio::spawn(async move {
        loop {
            match lines.next_line().await {
                Ok(Some(line)) => tracing::debug!("model provider: {line}"),
                Ok(None) => break,
                Err(error) => {
                    tracing::error!(%error, "failed to read model provider output");
                    break;
                }
            }
        }
    });
    Ok((
        Arc::new(RemoteModelProvider::new(socket.trim())),
        Some(child),
    ))
}

#[cfg(not(unix))]
async fn remote_provider(
    _args: &Args,
) -> Result<(Arc<dyn ModelProvider>, Option<tokio::process::Child>), BoxError> {
    Err("remote provider sockets require Unix; use WSL or --provider openai-compatible".into())
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<(), BoxError> {
    let args = Args::parse();
    if args.model.trim().is_empty() {
        return Err("model must not be empty".into());
    }
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "infinity_research=info,warn".into()),
        )
        .try_init()
        .map_err(|error| format!("cannot initialize logging: {error}"))?;
    let backend = Arc::new(
        PythonBackend::new(
            args.python.clone(),
            args.research_root.clone(),
            args.campaign.clone(),
        )?
        .with_recovery_timeout(Duration::from_secs(args.recovery_timeout_seconds)),
    );
    let _launcher_lock = backend.lock_campaign()?;
    let (provider, _provider_process) = match args.provider {
        ProviderKind::OpenaiCompatible => (openai_compatible_provider(&args)?, None),
        ProviderKind::Remote => remote_provider(&args).await?,
    };
    let summary = run_campaign(
        RunOptions {
            workers: usize::from(args.workers),
            max_output_tokens: args.max_output_tokens,
            poll_interval: Duration::from_secs(args.poll_interval_seconds),
            model_response_timeout: Duration::from_secs(args.response_timeout_seconds),
            model_call_timeout: Duration::from_secs(args.model_call_timeout_seconds),
        },
        backend.clone(),
        provider,
        &args.model,
        async {
            if let Err(error) = tokio::signal::ctrl_c().await {
                tracing::error!(%error, "cannot register shutdown signal");
            }
        },
    )
    .await?;
    let final_evaluation = if args.no_finalize {
        None
    } else {
        finalize_if_eligible(backend.as_ref(), &summary).await?
    };
    let mut report = serde_json::to_value(summary)?;
    report["final_evaluation"] = serde_json::to_value(final_evaluation)?;
    println!("{}", serde_json::to_string_pretty(&report)?);
    Ok(())
}
