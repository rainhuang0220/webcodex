use super::*;

#[test]
fn shared_output_schema_preserves_all_canonical_contracts() {
    for definition in crate::tool_definition::tool_definitions() {
        let expected = build_output_schema_for_tool(definition.name);
        let first = shared_output_schema_for_tool(definition.name);
        let second = shared_output_schema_for_tool(definition.name);
        assert_eq!(first.as_ref(), &expected, "{}", definition.name);
        assert!(matches!(first, Cow::Borrowed(_)), "{}", definition.name);
        assert!(std::ptr::eq(first.as_ref(), second.as_ref()));
        let mut owned = output_schema_for_tool(definition.name);
        owned["private_test_overlay"] = json!(true);
        assert_eq!(
            shared_output_schema_for_tool(definition.name).as_ref(),
            &expected
        );
    }
}

#[test]
fn shared_output_schema_unknown_names_are_not_retained() {
    let _ = shared_output_schema_for_tool("read_files");
    let count = OUTPUT_SCHEMAS.get().unwrap().len();
    for index in 0..128 {
        let name = format!("unknown-output-schema-{index}");
        let schema = shared_output_schema_for_tool(&name);
        assert!(matches!(schema, Cow::Owned(_)));
        assert_eq!(schema.as_ref(), &build_output_schema_for_tool(&name));
        assert!(!OUTPUT_SCHEMAS.get().unwrap().contains_key(name.as_str()));
    }
    assert_eq!(OUTPUT_SCHEMAS.get().unwrap().len(), count);
}

#[test]
fn shared_output_schema_concurrent_readers_share_one_value() {
    let barrier = std::sync::Arc::new(std::sync::Barrier::new(8));
    let readers = (0..8)
        .map(|_| {
            let barrier = barrier.clone();
            std::thread::spawn(move || {
                barrier.wait();
                let schema = shared_output_schema_for_tool("observe_jobs");
                assert_eq!(
                    schema.as_ref(),
                    &build_output_schema_for_tool("observe_jobs")
                );
                schema.as_ref() as *const Value as usize
            })
        })
        .collect::<Vec<_>>();
    let pointers = readers
        .into_iter()
        .map(|reader| reader.join().unwrap())
        .collect::<Vec<_>>();
    assert!(pointers.iter().all(|pointer| *pointer == pointers[0]));
}

#[test]
#[ignore = "opt-in local schema construction comparison; no timing threshold"]
fn shared_output_schema_local_cost_probe() {
    use std::{hint::black_box, time::Instant};
    for name in ["read_files", "observe_jobs", "read_tool_manifest"] {
        let _ = shared_output_schema_for_tool(name);
        let runs = 200;
        let start = Instant::now();
        for _ in 0..runs {
            black_box(build_output_schema_for_tool(black_box(name)));
        }
        let fresh = start.elapsed();
        let start = Instant::now();
        for _ in 0..runs {
            black_box(shared_output_schema_for_tool(black_box(name)));
        }
        eprintln!(
            "{name}: {runs} iterations, fresh_build_us={}, shared_borrow_us={}",
            fresh.as_micros(),
            start.elapsed().as_micros()
        );
    }
}
