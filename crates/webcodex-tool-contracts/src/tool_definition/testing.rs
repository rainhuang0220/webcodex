use super::RunnerCapabilityRequirement::{OwnerOnly, Shell};
use super::ToolVisibility::ModelVisible;
use super::{
    adaptive_runtime_direct, captures_validation_output, def, model_spec, ToolDefinition,
    TOOL_CATEGORY_VALIDATION,
};
use crate::metadata::{
    ToolPathHint::None as NoPath, ToolRisk::JobRun, JOB_RUN, TOOL_PROVIDER_RUNNER,
};

pub(super) const DEFINITIONS: &[ToolDefinition] = &[
    captures_validation_output(model_spec(
        def(
            "cargo_fmt",
            super::ToolAuditPolicy::TYPED_CANONICAL
                .execution(super::ToolAuditExecutionPolicy::TEXT),
            ModelVisible,
            TOOL_CATEGORY_VALIDATION,
            Some(Shell),
            TOOL_PROVIDER_RUNNER,
            super::ToolSemanticContract {
                effect: super::ToolEffect::Execute,
                risk: JobRun,
                approval: super::ToolApprovalPolicy::Standard,
                idempotency: super::ToolIdempotency::NonIdempotent,
            },
            Some(JOB_RUN),
            true,
            NoPath,
            true,
            false,
            super::ToolSessionEvidencePolicy::NONE.validation_identity(super::ToolValidationIdentityKind::CargoFmt),
        ),
        "Use check=false (default) for intentional final formatting after relevant Rust source stabilizes: precheck first, mutate only for a proven rustfmt diff, and use changed/state_changed instead of reproducing rustfmt diffs with edit tools. Do not use cargo_fmt as a per-edit ritual. Use check=true for read-only formatting validation when final formatting proof is needed; only that mode may hand off the same execution as a Job. Omitted sync_wait_secs uses the Runtime early-handoff default in check mode; ensure-format accepts but ignores it and always stays synchronous. Validation intent is intrinsic to this validator and Runtime-derived; do not pass generic execution purpose.",
    )
    .with_execution(super::ToolExecutionContract::new(
        super::ToolExecutionForm::StructuredValidation,
        super::ToolExecutionLifetime::Runner,
        super::ToolExecutionStart::SyncFirst,
        super::ToolExecutionContinuation::ObserveJobs,
    ))),
    adaptive_runtime_direct(
        captures_validation_output(model_spec(
            def(
                "cargo_check",
                super::ToolAuditPolicy::TYPED_CANONICAL
                    .execution(super::ToolAuditExecutionPolicy::TEXT),
                ModelVisible,
                TOOL_CATEGORY_VALIDATION,
                Some(Shell),
                TOOL_PROVIDER_RUNNER,
                super::ToolSemanticContract {
                    effect: super::ToolEffect::Execute,
                    risk: JobRun,
                    approval: super::ToolApprovalPolicy::Standard,
                    idempotency: super::ToolIdempotency::NonIdempotent,
                },
                Some(JOB_RUN),
                true,
                NoPath,
                false,
                false,
                super::ToolSessionEvidencePolicy::NONE.validation_identity(super::ToolValidationIdentityKind::CargoCheck),
            ).with_composition_policy(super::ToolCompositionPolicy::Sequential),
            "Structured cargo check (default --all-targets) for common supported validation with parsed diagnostics, validation identity, bounded projection, and same execution Job handoff. Use package for one workspace package or packages for a set; the selectors are mutually exclusive, and packages runs one Cargo invocation with repeated -p arguments after deterministic sort/dedup canonicalization. Intent is Runtime-derived; scoped flags only. sync_wait_secs controls synchronous handoff grace; omission uses the Runtime early-handoff default and never changes total timeout or retry semantics.",
        ).with_gpt_action_description("Run one structured cargo check with parsed diagnostics. Use package for one workspace package or packages for a sorted/deduplicated set in the same Cargo process. Long validation continues as the same Job; sync_wait_secs changes only handoff grace, not timeout or retry.")
        .with_execution(super::ToolExecutionContract::new(
            super::ToolExecutionForm::StructuredValidation,
            super::ToolExecutionLifetime::Runner,
            super::ToolExecutionStart::SyncFirst,
            super::ToolExecutionContinuation::ObserveJobs,
        ))),
        90,
    ),
    adaptive_runtime_direct(
        captures_validation_output(model_spec(
            def(
                "cargo_test",
                super::ToolAuditPolicy::TYPED_CANONICAL
                    .execution(super::ToolAuditExecutionPolicy::TEST_ASSERTIONS),
                ModelVisible,
                TOOL_CATEGORY_VALIDATION,
                Some(Shell),
                TOOL_PROVIDER_RUNNER,
                super::ToolSemanticContract {
                    effect: super::ToolEffect::Execute,
                    risk: JobRun,
                    approval: super::ToolApprovalPolicy::Standard,
                    idempotency: super::ToolIdempotency::NonIdempotent,
                },
                Some(JOB_RUN),
                true,
                NoPath,
                false,
                false,
                super::ToolSessionEvidencePolicy::NONE.validation_identity(super::ToolValidationIdentityKind::CargoTest),
            ).with_composition_policy(super::ToolCompositionPolicy::Sequential),
            "Structured cargo test for common supported validation with bounded output, executed-test evidence, min_tests/require_tests, validation identity, and same execution Job handoff. Intent is Runtime-derived. lib=true selects Cargo --lib; false/omitted keeps ordinary targets. filter is one Rust substring for `cargo test FILTER`, not Cargo/libtest flags such as `--exact` or `--nocapture`; zero-test results are not validation proof and return recovery guidance. Normal execution requires non-zero executed-test evidence; require_tests=false opts out when min_tests is absent, while require_tests=true/min_tests enforce a proven minimum. no_run=true is compile-only and needs no executed-test proof. sync_wait_secs controls Job-handoff grace; omission uses the Runtime early-handoff default.",
        ).with_gpt_action_description("Run structured cargo tests with bounded output. A successful proof requires executed-test evidence unless explicitly opted out; use min_tests/require_tests when count matters. Long validation continues as the same Job.")
        .with_execution(super::ToolExecutionContract::new(
            super::ToolExecutionForm::StructuredValidation,
            super::ToolExecutionLifetime::Runner,
            super::ToolExecutionStart::SyncFirst,
            super::ToolExecutionContinuation::ObserveJobs,
        ))),
        100,
    ),
    captures_validation_output(model_spec(
            def(
                "go_test",
                super::ToolAuditPolicy::TYPED_CANONICAL
                    .execution(super::ToolAuditExecutionPolicy::TEST_COUNTS),
                ModelVisible,
                TOOL_CATEGORY_VALIDATION,
                Some(OwnerOnly),
                TOOL_PROVIDER_RUNNER,
                super::ToolSemanticContract {
                    effect: super::ToolEffect::Execute,
                    risk: JobRun,
                    approval: super::ToolApprovalPolicy::Standard,
                    idempotency: super::ToolIdempotency::NonIdempotent,
                },
                Some(JOB_RUN),
                true,
                NoPath,
                false,
                false,
                super::ToolSessionEvidencePolicy::NONE.validation_identity(super::ToolValidationIdentityKind::GoTest),
            ),
            "Structured option for common supported go test -json (default ./...) when bounded package scopes, Go JSON test-count evidence, validation identity, or same execution Job handoff are useful. Validation intent is intrinsic to this validator and Runtime-derived; do not pass generic execution purpose. Requires Runner Go JSON validation support; optional sync_wait_secs controls only synchronous grace before the same execution is returned as a Job, omission uses the Runtime early-handoff default, and it never changes total timeout or retry semantics.",
    )
    .with_execution(super::ToolExecutionContract::new(
        super::ToolExecutionForm::StructuredValidation,
        super::ToolExecutionLifetime::Runner,
        super::ToolExecutionStart::SyncFirst,
        super::ToolExecutionContinuation::ObserveJobs,
    ))),
];
