use super::*;
use serde_json::json;

#[test]
fn read_text_counts_identical_same_path_text_without_persisting_content() {
    let output = json!({"items": [
        {"path": "PRIVATE_PATH", "output": {"text": "PRIVATE_TEXT", "read_revision": 17}},
        {"path": "PRIVATE_PATH", "output": {"text": "PRIVATE_TEXT", "read_revision": 17}},
        {"path": "other.rs", "output": {"text": "PRIVATE_TEXT", "read_revision": 17}},
        {"path": "PRIVATE_PATH", "output": {"text": "different", "read_revision": 17}}
    ]});
    let facts = from_model_result("read_files", &output).unwrap();
    let value = serde_json::to_value(&facts).unwrap();
    assert_eq!(value["kind"], "read_files");
    assert_eq!(value["items"], 4);
    assert_eq!(value["text_bytes"], 12 * 3 + 9);
    assert_eq!(value["identical_same_path_text_items"], 1);
    assert_eq!(value["identical_same_path_text_bytes"], 12);
    assert!(!value.to_string().contains("PRIVATE"));
}

#[test]
fn read_text_malformed_and_failed_items_are_unavailable() {
    let many = vec![json!({"path": "PRIVATE_PATH", "output": {"text": "PRIVATE_TEXT"}}); 9];
    for output in [
        json!({}),
        json!({"items": []}),
        json!({"items": [{"path": "PRIVATE_PATH", "success": false, "output": {"text": "PRIVATE_TEXT"}}]}),
        json!({"items": [{"path": "PRIVATE_PATH", "output": {"text": ["PRIVATE_TEXT"]}}]}),
        json!({"items": many}),
    ] {
        assert!(from_model_result("read_files", &output).is_none());
    }
}

#[test]
fn job_log_bytes_are_broken_down_by_observation_delta_kind() {
    let output = json!({"items": [
        {"log_delta_status": "baseline", "stdout_tail": "PRIVATE_BASE", "stderr_tail": ""},
        {"log_delta_status": "delta", "stdout_tail": "界", "stderr_tail": ""},
        {"log_delta_status": "reset", "stdout_tail": "", "stderr_tail": "PRIVATE_ERR"},
        {"log_delta_status": "unchanged"}
    ]});
    let facts = from_model_result("observe_jobs", &output).unwrap();
    let value = serde_json::to_value(&facts).unwrap();
    assert_eq!(value["kind"], "observe_jobs");
    assert_eq!(value["items"], 4);
    for field in [
        "baseline_items",
        "delta_items",
        "reset_items",
        "unchanged_items",
    ] {
        assert_eq!(value[field], 1);
    }
    assert_eq!(value["baseline_log_bytes"], 12);
    assert_eq!(value["delta_log_bytes"], 3);
    assert_eq!(value["reset_log_bytes"], 11);
    assert_eq!(value["unchanged_log_bytes"], 0);
    assert!(!value.to_string().contains("PRIVATE"));
}

#[test]
fn nested_canonical_job_items_and_strict_fail_closed_parsing() {
    let valid = json!({"items": [
        {"success": true, "output": {"log_delta_status": "delta", "stdout_tail": "one", "stderr_tail": "two"}}
    ]});
    assert_eq!(
        serde_json::to_value(from_model_result("observe_jobs", &valid).unwrap()).unwrap()
            ["delta_log_bytes"],
        6
    );
    for invalid in [
        json!({"items": [{"log_delta_status": "PRIVATE_STATUS", "stdout_tail": "secret"}]}),
        json!({"items": [{"log_delta_status": "delta", "stdout_tail": 123}]}),
        json!({"items": [{"success": false, "output": {"log_delta_status": "delta"}}]}),
    ] {
        assert!(from_model_result("observe_jobs", &invalid).is_none());
    }
}

#[test]
fn final_api_and_mcp_records_are_payload_free_and_equivalent() {
    use crate::tool_runtime::{model_ergonomics_telemetry::ModelErgonomicsTimer, ToolResult};
    for (tool, output) in [
        (
            "read_files",
            json!({"items": [
                {"path": "PRIVATE_PATH", "output": {"text": "PRIVATE_CONTENT"}},
                {"path": "PRIVATE_PATH", "output": {"text": "PRIVATE_CONTENT"}}
            ]}),
        ),
        (
            "observe_jobs",
            json!({"items": [
                {"log_delta_status": "reset", "stdout_tail": "PRIVATE_LOG", "stderr_tail": ""}
            ]}),
        ),
    ] {
        let completion = ModelErgonomicsTimer::start(tool).unwrap().finish();
        let result = ToolResult::ok(output.clone());
        let api = completion.record_for_tool_result(&result).unwrap();
        let mcp = completion
            .record_for_structured_content(&serde_json::to_value(&result).unwrap())
            .unwrap();
        assert_eq!(api, mcp);
        assert_eq!(api.schema_version, 13);
        assert!(api.output_transfer.is_some());
        let serialized = serde_json::to_string(&api).unwrap();
        assert!(!serialized.contains("PRIVATE"));
        assert!(!serialized.contains("read_revision"));
        let pre_result = completion.record_for_pre_result_failure("invalid_arguments");
        assert!(pre_result.output_transfer.is_none());
        let failed = completion
            .record_for_tool_result(&ToolResult::err_with_output("PRIVATE_ERROR", output))
            .unwrap();
        assert!(failed.output_transfer.is_none());
    }
}
