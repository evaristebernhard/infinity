//! Verify the real rig HTTP serialization without calling a paid provider.

use std::sync::Arc;
use std::time::Duration;

use futures_util::StreamExt;
use infinity_provider_protocol::{
    CompletionRequest, Message, ModelEntry, ModelProvider, StreamChunk,
};
use infinity_provider_rig::RigCompletionModel;
use infinity_research::{ChatCompletionsProvider, OutputTokenField};
use rig::client::CompletionClient;
use serde_json::{Value, json};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

#[tokio::test]
async fn actual_chat_completions_http_request_preserves_the_output_cap() {
    for (field, name, excluded) in [
        (
            OutputTokenField::MaxTokens,
            "max_tokens",
            "max_completion_tokens",
        ),
        (
            OutputTokenField::MaxCompletionTokens,
            "max_completion_tokens",
            "max_tokens",
        ),
    ] {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("local fixture listener");
        let base = format!(
            "http://{}",
            listener.local_addr().expect("listener address")
        );
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.expect("fixture request");
            let mut bytes = Vec::new();
            let body_start = loop {
                let byte = socket.read_u8().await.expect("HTTP request header");
                bytes.push(byte);
                assert!(bytes.len() < 64 * 1024, "fixture header bound");
                if bytes.ends_with(b"\r\n\r\n") {
                    break bytes.len();
                }
            };
            let header = std::str::from_utf8(&bytes).expect("HTTP header UTF-8");
            assert!(header.starts_with("POST /chat/completions HTTP/1.1\r\n"));
            let length: usize = header
                .lines()
                .find_map(|line| {
                    let (name, value) = line.split_once(':')?;
                    name.eq_ignore_ascii_case("content-length")
                        .then(|| value.trim().parse().expect("body length"))
                })
                .expect("content length");
            assert!(length < 1024 * 1024, "fixture body bound");
            bytes.resize(body_start + length, 0);
            socket
                .read_exact(&mut bytes[body_start..])
                .await
                .expect("HTTP request body");
            let request: Value =
                serde_json::from_slice(&bytes[body_start..]).expect("provider wire JSON");
            let body = concat!(
                "data: {\"id\":\"fixture\",\"object\":\"chat.completion.chunk\",\"created\":0,\"model\":\"fixture\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"ok\"},\"finish_reason\":null}]}\n\n",
                "data: {\"id\":\"fixture\",\"object\":\"chat.completion.chunk\",\"created\":0,\"model\":\"fixture\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n",
                "data: {\"id\":\"fixture\",\"object\":\"chat.completion.chunk\",\"created\":0,\"model\":\"fixture\",\"choices\":[],\"usage\":{\"prompt_tokens\":3,\"completion_tokens\":1,\"total_tokens\":4}}\n\n",
                "data: [DONE]\n\n",
            );
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            socket
                .write_all(response.as_bytes())
                .await
                .expect("fixture stream response");
            socket.shutdown().await.expect("close fixture response");
            request
        });
        let client: rig::providers::openai::CompletionsClient =
            rig::providers::openai::Client::builder()
                .api_key("fixture-key-unused-outside-local-test")
                .base_url(base)
                .build()
                .expect("rig fixture client")
                .completions_api();
        let inner = Arc::new(
            RigCompletionModel::new(client.completion_model("fixture")).into_provider(ModelEntry {
                model_id: "fixture".into(),
                display_name: "fixture".into(),
                context_window: 0,
                max_output_tokens: Some(4096),
                supports_image_input: false,
            }),
        );
        let provider = ChatCompletionsProvider::new(inner, field, true);
        let request = CompletionRequest {
            chat_history: vec![Message::user("hello")],
            max_tokens: Some(17),
            additional_params: Some(json!({"max_tokens": 99999, "max_completion_tokens": 99999})),
            ..CompletionRequest::default()
        };
        let mut stream = tokio::time::timeout(
            Duration::from_secs(3),
            provider.invoke_model("fixture", request),
        )
        .await
        .expect("HTTP fixture deadline")
        .expect("rig invocation");
        let mut usage = None;
        while let Some(chunk) = stream.next().await {
            if let StreamChunk::Final(final_response) = chunk.expect("rig streamed chunk") {
                usage = final_response.usage;
            }
        }
        assert_eq!(
            usage.expect("usage through real rig stream").output_tokens,
            1
        );
        let wire = server.await.expect("fixture server task");
        assert_eq!(
            wire[name], 17,
            "admitted cap must survive rig serialization"
        );
        assert!(
            wire.get(excluded).is_none(),
            "conflicting token limits must be removed"
        );
        assert_eq!(wire["thinking"]["type"], "disabled");
        assert_eq!(wire["stream_options"]["include_usage"], true);
    }
}
