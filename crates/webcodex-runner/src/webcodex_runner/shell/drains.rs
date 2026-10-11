//! Continuous stdout/stderr drains and bounded collection.

use super::*;

pub(super) struct ContinuousPipeDrain {
    pub(super) stdout_rx: mpsc::Receiver<Result<BoundedPipeTail, String>>,
    pub(super) stderr_rx: mpsc::Receiver<Result<BoundedPipeTail, String>>,
    pub(super) stdout_handle: std::thread::JoinHandle<()>,
    pub(super) stderr_handle: std::thread::JoinHandle<()>,
    pub(super) stdout_limit_reached: Arc<AtomicBool>,
}

#[derive(Clone, Copy)]
pub(super) struct StdoutPrefixLimit {
    pub(super) lines: usize,
    pub(super) bytes: usize,
}

/// Native equivalent of `head -n ... | head -c ...`: retain a bounded prefix,
/// then close the pipe. The executor still owns cancellation and tree cleanup.
pub(super) fn read_bounded_search_prefix(
    mut pipe: impl Read,
    limit: StdoutPrefixLimit,
    reached: &AtomicBool,
) -> Result<BoundedPipeTail, String> {
    let mut output = Vec::with_capacity(limit.bytes.min(8192));
    let mut lines = 0;
    let mut chunk = [0; 8192];
    while output.len() < limit.bytes && lines < limit.lines {
        let read = pipe
            .read(&mut chunk)
            .map_err(|error| format!("failed to read stdout: {error}"))?;
        if read == 0 {
            break;
        }
        for &byte in &chunk[..read] {
            output.push(byte);
            lines += usize::from(byte == b'\n');
            if output.len() == limit.bytes || lines == limit.lines {
                reached.store(true, Ordering::SeqCst);
                break;
            }
        }
    }
    // A byte cap can split a UTF-8 scalar. Preserve the complete prefix and
    // replace only that incomplete suffix before Windows encoding detection;
    // the canonical search parser discards the byte-cut record.
    if std::str::from_utf8(&output).is_err_and(|error| error.error_len().is_none()) {
        output = String::from_utf8_lossy(&output).into_owned().into_bytes();
    }
    let mut validator = super::output::IncrementalUtf8Validator::new();
    validator.push(&output);
    Ok(BoundedPipeTail {
        bytes: output,
        raw_truncated: false,
        encoding: CapturedOutputEncoding {
            full_stream_utf8: validator.finish(),
            leading_bom: LeadingBom::None,
        },
    })
}

impl ContinuousPipeDrain {
    /// Take both child pipes and start independent bounded readers immediately.
    ///
    /// The readers own the OS pipe handles for the full child lifetime. They do
    /// not forward per-chunk data through another bounded queue, so Server/model
    /// observation cannot backpressure either stream. Each reader retains only
    /// the existing bounded tail plus UTF-8/BOM validation state.
    pub(super) fn start_with_prefix(
        child: &mut ManagedChild,
        max_output_bytes: usize,
        prefix: Option<StdoutPrefixLimit>,
    ) -> Result<Self, String> {
        let stdout = child
            .child_mut()
            .stdout
            .take()
            .ok_or_else(|| "stdout pipe missing".to_string())?;
        let stderr = match child.child_mut().stderr.take() {
            Some(stderr) => stderr,
            None => {
                drop(stdout);
                return Err("stderr pipe missing".to_string());
            }
        };
        let (stdout_tx, stdout_rx) = mpsc::sync_channel(1);
        let stdout_limit_reached = Arc::new(AtomicBool::new(false));
        let reached = Arc::clone(&stdout_limit_reached);
        let stdout_handle = std::thread::spawn(move || {
            let output = match prefix {
                Some(limit) => read_bounded_search_prefix(stdout, limit, &reached),
                None => read_bounded_pipe_tail(stdout, max_output_bytes, "stdout"),
            };
            let _ = stdout_tx.send(output);
        });
        let (stderr_tx, stderr_rx) = mpsc::sync_channel(1);
        let stderr_handle = std::thread::spawn(move || {
            let _ = stderr_tx.send(read_bounded_pipe_tail(stderr, max_output_bytes, "stderr"));
        });
        Ok(Self {
            stdout_rx,
            stderr_rx,
            stdout_handle,
            stderr_handle,
            stdout_limit_reached,
        })
    }

    pub(super) fn stdout_limit_reached(&self) -> bool {
        self.stdout_limit_reached.load(Ordering::SeqCst)
    }

    pub(super) fn finish_until(
        self,
        deadline: Instant,
    ) -> Result<(BoundedPipeTail, BoundedPipeTail), String> {
        let Self {
            stdout_rx,
            stderr_rx,
            stdout_handle,
            stderr_handle,
            ..
        } = self;
        let receive = |rx: mpsc::Receiver<Result<BoundedPipeTail, String>>,
                       stream_name: &'static str|
         -> Result<BoundedPipeTail, String> {
            let remaining = deadline.saturating_duration_since(Instant::now());
            rx.recv_timeout(remaining).map_err(|_| {
                format!("{stream_name} reader did not finish before cleanup deadline")
            })?
        };
        // Always give both already-running readers the same drain deadline.
        // A read error on one stream must not short-circuit collection of the
        // other stream and turn an otherwise bounded reader into a detached one.
        let stdout = receive(stdout_rx, "stdout");
        let stderr = receive(stderr_rx, "stderr");
        if stdout_handle.is_finished() {
            let _ = stdout_handle.join();
        }
        if stderr_handle.is_finished() {
            let _ = stderr_handle.join();
        }
        match (stdout, stderr) {
            (Ok(stdout), Ok(stderr)) => Ok((stdout, stderr)),
            (Err(stdout), Ok(_)) => Err(stdout),
            (Ok(_), Err(stderr)) => Err(stderr),
            (Err(stdout), Err(stderr)) => Err(format!("{stdout}; {stderr}")),
        }
    }
}

/// Finish one command whose stdout/stderr readers have already been draining
/// continuously since immediately after spawn. Tree termination still happens
/// before waiting for reader EOF so a descendant that inherited a pipe cannot
/// keep the readers alive after direct-child exit, stop, or timeout.
///
/// Tree cleanup/reaping and pipe drain are separate bounded phases. A saturated
/// CI/runtime scheduler can consume the tree-cleanup budget after the child has
/// already exited; reusing that expired deadline for readers would incorrectly
/// turn a known terminal status into `OutcomeUnknown`. stdout/stderr still share
/// one drain deadline, so neither stream receives an independently reset budget.
pub(super) fn terminate_and_collect_pipes(
    mut child: ManagedChild,
    drains: ContinuousPipeDrain,
) -> Result<(std::process::ExitStatus, BoundedPipeTail, BoundedPipeTail), String> {
    let cleanup_deadline = Instant::now() + PROCESS_TREE_CLEANUP_TIMEOUT;
    let cleanup = terminate_child_process_tree_until(&mut child, cleanup_deadline).err();
    let status = wait_child_until(&mut child, cleanup_deadline);
    let drain_deadline = Instant::now() + PROCESS_PIPE_DRAIN_TIMEOUT;
    let output = drains.finish_until(drain_deadline);
    let mut errors = Vec::new();
    if let Some(cleanup) = cleanup {
        errors.push(format!(
            "failed to terminate command process tree: {cleanup}"
        ));
    }
    let status = match status {
        Ok(status) => Some(status),
        Err(error) => {
            errors.push(error);
            None
        }
    };
    let output = match output {
        Ok(output) => Some(output),
        Err(error) => {
            errors.push(format!("failed to collect output: {error}"));
            None
        }
    };
    if !errors.is_empty() {
        return Err(errors.join("; "));
    }
    let (stdout, stderr) = output.expect("output exists when no collection error was recorded");
    Ok((
        status.expect("status exists when no wait error was recorded"),
        stdout,
        stderr,
    ))
}

/// Test/error-path convenience wrapper. Production structured execution starts
/// the drain immediately after spawn rather than waiting until termination.
#[cfg(test)]
pub(super) fn terminate_and_read_pipes(
    mut child: ManagedChild,
    max_output_bytes: usize,
) -> Result<(std::process::ExitStatus, BoundedPipeTail, BoundedPipeTail), String> {
    let drains = match ContinuousPipeDrain::start_with_prefix(&mut child, max_output_bytes, None) {
        Ok(drains) => drains,
        Err(error) => {
            let cleanup = terminate_child_process_tree(&mut child).err();
            return Err(with_cleanup_error(error, cleanup));
        }
    };
    terminate_and_collect_pipes(child, drains)
}
