use super::*;

#[test]
fn health_locks_release_without_waiting_for_inherited_handles() {
    for exclusive in [false, true] {
        for fail_after_lock in [false, true] {
            let temp = crate::test_tempdir().unwrap();
            let store = EnvironmentStore::open(temp.path().join("environment")).unwrap();
            let path = store.root().join("readiness.json");
            atomic_private_write(&path, b"{}").unwrap();
            let file = crate::storage::open_existing_private(&path, true).unwrap();
            if exclusive {
                fs2::FileExt::try_lock_exclusive(&file).unwrap();
            } else {
                fs2::FileExt::try_lock_shared(&file).unwrap();
            }
            let mut inherited = None;
            let result = (|| -> SetupResultValue<()> {
                let locked = TunnelHealthFileLock { file };
                // dup and fork share an open-file description. An unrelated concurrent
                // spawn can retain this descriptor until exec despite O_CLOEXEC.
                inherited = Some(locked.file.try_clone().unwrap());

                assert!(write_tunnel_health(&path, true, true).is_err());
                assert_eq!(std::fs::read(&path).unwrap(), b"{}");
                let contender = crate::storage::open_existing_private(&path, true).unwrap();
                assert!(fs2::FileExt::try_lock_exclusive(&contender).is_err());
                if fail_after_lock {
                    return Err(SetupDiagnostic::io());
                }
                Ok(())
            })();
            assert_eq!(result.is_err(), fail_after_lock);

            write_embedded_tunnel_observation(
                &path,
                1,
                TunnelState::Failed,
                false,
                false,
                Some("tunnel_restart_uncertain"),
            )
            .unwrap_or_else(|error| {
                panic!("lock survived operation, exclusive={exclusive}: {error:?}")
            });
            let health = read_tunnel_health(&path).unwrap();
            assert_eq!(health.state, Some(TunnelState::Failed));
            assert_eq!(
                health.diagnostic.as_deref(),
                Some("tunnel_restart_uncertain")
            );
            drop(inherited);
        }
    }
}
