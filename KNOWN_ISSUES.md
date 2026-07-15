# Known Issues

Only verified limitations are listed here.

| ID | Affected version | Symptom | Impact | Workaround | Status |
|---|---|---|---|---|---|
| KI-001 | 0.2.0 | macOS is founder-verified only. | Other platforms have less evidence. | Use a disposable committed repository first. | Open |
| KI-002 | 0.2.0 | Ubuntu and Windows are not verified. | Cross-platform behavior is unknown. | Do not treat them as supported release targets. | Open |
| KI-003 | 0.2.0 | Host-agent behavior affects workflow outcomes. | A correct SkillLayer result does not guarantee a correct edit. | Review plans, diffs, and validation output. | Open |
| KI-004 | 0.2.0 | SkillLayer does not detect every defect or security issue. | Results are not a security certification. | Use normal review and security tooling. | Open |
| KI-005 | 0.2.0 | Update-check depends on public network availability. | Offline checks report unknown status. | Retry when online; do not infer “up to date”. | Open |
| KI-006 | 0.2.0 | Pytest may be unavailable in a project environment. | Test validation can be incomplete. | Follow the advisory remediation command. | Open |
| KI-007 | 0.2.0 | Rollback requires a retained commit, tag, or environment. | Rollback is not guaranteed for every installation. | Preserve the old environment until verification. | Open |
| KI-008 | 0.2.0 | Repository policy v1 supports only a small local schema. | Team/remote governance is not provided. | Use versioned local policy files and normal review. | Open |
