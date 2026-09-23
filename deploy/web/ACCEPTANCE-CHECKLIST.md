# Web production deployment acceptance checklist

This checklist contains no live infrastructure values. Store completed evidence in the controlled operations system.

- [ ] deployed image digest matches the approved build evidence
- [ ] deployment was approved by the required reviewer
- [ ] application port is reachable only on host loopback
- [ ] public HTTP redirects to HTTPS
- [ ] public TLS certificate and hostname validation pass
- [ ] `/api/health` returns `account.required == true`
- [ ] session cookie includes `Secure`, `HttpOnly`, and `SameSite=Strict`
- [ ] registration requires the current invite code
- [ ] per-user and global LLM quotas are positive and enforced
- [ ] invalid tag and unknown deploy policy entries fail closed
- [ ] deploy user cannot invoke Docker directly or read `.env`
- [ ] backup coverage and a recent restore test are documented
- [ ] rollback to the previous verified digest was exercised
- [ ] secrets, host keys, account IDs, IPs, and raw logs were not copied into Git
