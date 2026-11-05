//! Autonomous research workers using Infinity's native thread and tool runtime.
//!
//! A durable campaign backend owns experiments, evidence, and admission budgets.
//! Worker conversations are deliberately ephemeral: restarting this launcher
//! creates fresh Infinity threads from the campaign's persisted context.

pub mod backend;
pub mod provider;
pub mod runner;
pub mod tools;

/// Error propagated by launcher, provider, and campaign operations.
pub type BoxError = Box<dyn std::error::Error + Send + Sync>;

pub use backend::{PythonBackend, ResearchBackend};
pub use provider::{BudgetedProvider, ChatCompletionsProvider, OutputTokenField};
pub use runner::{RunOptions, RunSummary, finalize_if_eligible, run_campaign};
