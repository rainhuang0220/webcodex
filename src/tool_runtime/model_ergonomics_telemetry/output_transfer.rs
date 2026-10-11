//! Payload-free per-result measurements of read text and Job-log delivery.
//!
//! Computed from the final model-visible result, without persisting paths,
//! Job identities, hashes or content. This is not cross-call duplication evidence.

use serde::Serialize;
use serde_json::Value;

const MAX_ITEMS: usize = 8;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub(crate) enum OutputTransferFacts {
    ReadFiles {
        items: u64,
        text_bytes: u64,
        identical_same_path_text_items: u64,
        identical_same_path_text_bytes: u64,
    },
    ObserveJobs {
        items: u64,
        baseline_items: u64,
        delta_items: u64,
        reset_items: u64,
        unchanged_items: u64,
        baseline_log_bytes: u64,
        delta_log_bytes: u64,
        reset_log_bytes: u64,
        unchanged_log_bytes: u64,
    },
}

pub(crate) fn from_model_result(tool: &str, output: &Value) -> Option<OutputTransferFacts> {
    match tool {
        "read_files" => read_files(output),
        "observe_jobs" => observe_jobs(output),
        _ => None,
    }
}

fn read_files(output: &Value) -> Option<OutputTransferFacts> {
    let items = output.get("items")?.as_array()?;
    if items.is_empty() || items.len() > MAX_ITEMS {
        return None;
    }
    let mut seen = Vec::<(&str, &str)>::with_capacity(items.len());
    let mut text_bytes = 0u64;
    let mut identical_items = 0u64;
    let mut identical_bytes = 0u64;
    for item in items {
        // Complete sparse reads omit success; partial batches preserve it.
        if item
            .get("success")
            .is_some_and(|flag| flag.as_bool() != Some(true))
        {
            return None;
        }
        let path = item.get("path")?.as_str()?;
        let text = item.get("output")?.get("text")?.as_str()?;
        let bytes = u64::try_from(text.len()).unwrap_or(u64::MAX);
        text_bytes = text_bytes.saturating_add(bytes);
        if seen
            .iter()
            .any(|(prior_path, prior_text)| *prior_path == path && *prior_text == text)
        {
            identical_items += 1;
            identical_bytes = identical_bytes.saturating_add(bytes);
        } else {
            seen.push((path, text));
        }
    }
    Some(OutputTransferFacts::ReadFiles {
        items: items.len() as u64,
        text_bytes,
        identical_same_path_text_items: identical_items,
        identical_same_path_text_bytes: identical_bytes,
    })
}

fn tail_bytes(observation: &Value, name: &str) -> Option<u64> {
    match observation.get(name) {
        None => Some(0), // Sparse results may omit empty/unchanged tails.
        Some(Value::String(text)) => Some(u64::try_from(text.len()).unwrap_or(u64::MAX)),
        _ => None,
    }
}

fn observe_jobs(output: &Value) -> Option<OutputTransferFacts> {
    let items = output.get("items")?.as_array()?;
    if items.is_empty() || items.len() > MAX_ITEMS {
        return None;
    }
    let mut counts = [0u64; 4];
    let mut log_bytes = [0u64; 4];
    for item in items {
        // Normal sparse successes are flat; canonical successes nest output.
        let observation = if item.get("log_delta_status").is_some() {
            item
        } else if item.get("success").and_then(Value::as_bool) == Some(true) {
            item.get("output")?
        } else {
            return None;
        };
        let index = match observation.get("log_delta_status")?.as_str()? {
            "baseline" => 0,
            "delta" => 1,
            "reset" => 2,
            "unchanged" => 3,
            _ => return None,
        };
        let bytes = tail_bytes(observation, "stdout_tail")?
            .saturating_add(tail_bytes(observation, "stderr_tail")?);
        counts[index] += 1;
        log_bytes[index] = log_bytes[index].saturating_add(bytes);
    }
    Some(OutputTransferFacts::ObserveJobs {
        items: items.len() as u64,
        baseline_items: counts[0],
        delta_items: counts[1],
        reset_items: counts[2],
        unchanged_items: counts[3],
        baseline_log_bytes: log_bytes[0],
        delta_log_bytes: log_bytes[1],
        reset_log_bytes: log_bytes[2],
        unchanged_log_bytes: log_bytes[3],
    })
}

#[cfg(test)]
#[path = "output_transfer_tests.rs"]
mod tests;
